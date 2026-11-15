"""Configurable HTTP adapters. No fixture or invented response is used in production.

``responses`` is the default, using an application-specific SIWC access token
and requiring the terminal ``response.completed`` event. No desktop credential
files are read. ``json`` model protocol:
  POST {role, system_prompt, context, output_schema, model?}
  -> {output: <JSON object>} (or a direct JSON object)
``chat_completions`` protocol is compatible with providers implementing that
HTTP contract; it uses no vendor SDK. ``search`` accepts POST {query, limit}
and returns {sources: [{url, title, excerpt}]}.
"""
from __future__ import annotations

import hashlib
import asyncio
import json
import logging
import math
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Protocol
from urllib.parse import urlsplit

import httpx
from .errors import GatewayError
from .streaming import ResponsesStreamDecoder, decode_json as _decode_json



class ModelGateway(Protocol):
    async def reply(self, *, role: str, system_prompt: str,
                    context: dict[str, Any], output_schema: dict[str, Any]) -> dict[str, Any]: ...


class SearchGateway(Protocol):
    async def search(self, query: str) -> list[dict[str, Any]]: ...


def _endpoint(value: str | None) -> str | None:
    if not value:
        return None
    parsed = urlsplit(value)
    if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Gateway endpoint must be an HTTP(S) URL without embedded credentials")
    return value


@dataclass(frozen=True)
class ModelConfig:
    endpoint: str | None = "https://api.openai.com/v1/responses"
    api_key: str | None = field(default=None, repr=False)
    access_token: str | None = field(default=None, repr=False)
    model: str | None = "gpt-6.1-sol"
    protocol: str = "responses"
    # Some Chat Completions providers support JSON objects but not strict schemas.
    json_object_mode: bool = field(default=False, kw_only=True)
    auth_mode: str = "chatgpt_subscription"
    reasoning_effort: str = "medium"
    service_tier: str = "default"
    timeout_seconds: float = 120.0
    max_response_bytes: int = 4 * 1024 * 1024
    max_event_bytes: int = 8 * 1024 * 1024
    max_stream_bytes: int = 64 * 1024 * 1024
    total_timeout_seconds: float = 300.0

    def __post_init__(self) -> None:
        _endpoint(self.endpoint)
        if self.protocol not in {"json", "chat_completions", "responses"}:
            raise ValueError("Unsupported model protocol")
        if self.auth_mode not in {"chatgpt_subscription", "api_key"}:
            raise ValueError("Unsupported model auth mode")
        if self.auth_mode == "chatgpt_subscription" and (
            self.protocol != "responses" or self.endpoint not in {None, "https://api.openai.com/v1/responses"}
        ):
            raise ValueError("ChatGPT subscription inference requires the public Responses endpoint")
        if (any(not math.isfinite(v) or v <= 0 or v > 3600 for v in
                (self.timeout_seconds, self.total_timeout_seconds)) or
                any(type(v) is not int or v <= 0 for v in
                    (self.max_response_bytes, self.max_event_bytes, self.max_stream_bytes))):
            raise ValueError("Gateway limits must be positive")
        if self.protocol in {"chat_completions", "responses"} and self.endpoint and not self.model:
            raise ValueError("MODEL_NAME is required for the selected protocol")
        if self.reasoning_effort not in {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}:
            raise ValueError("Unsupported reasoning effort")
        if self.service_tier not in {"default", "auto", "flex", "priority"}:
            raise ValueError("Unsupported service tier")

    @classmethod
    def from_env(cls) -> "ModelConfig":
        auth_mode=os.getenv('MODEL_AUTH_MODE','chatgpt_subscription')
        if auth_mode=='api_key':
            api_key=os.environ.get('PET_MODEL_API_KEY')
        else:api_key=None
        return cls(endpoint=os.getenv("MODEL_URL", "https://api.openai.com/v1/responses"),
                   api_key=api_key, access_token=os.getenv("MODEL_ACCESS_TOKEN"),
                   model=os.getenv("MODEL_NAME", "gpt-6.1-sol"), protocol=os.getenv("MODEL_PROTOCOL", "responses"),
                   auth_mode=auth_mode,
                   reasoning_effort=os.getenv("MODEL_REASONING_EFFORT", "medium"),
                   service_tier=os.getenv("MODEL_SERVICE_TIER", "default"),
                   timeout_seconds=float(os.getenv("MODEL_TIMEOUT_SECONDS", "120")),
                   total_timeout_seconds=float(os.getenv("MODEL_TOTAL_TIMEOUT_SECONDS", "300")),
                   max_response_bytes=int(os.getenv("MODEL_MAX_RESPONSE_BYTES", str(4*1024*1024))),
                   max_event_bytes=int(os.getenv("MODEL_MAX_EVENT_BYTES", str(8*1024*1024))),
                   max_stream_bytes=int(os.getenv("MODEL_MAX_STREAM_BYTES", str(64*1024*1024))))


@dataclass(frozen=True)
class SearchConfig:
    endpoint: str | None = None
    api_key: str | None = field(default=None, repr=False)
    timeout_seconds: float = 10.0
    limit: int = 5
    max_response_bytes: int = 1_000_000

    def __post_init__(self) -> None:
        _endpoint(self.endpoint)
        if (not math.isfinite(self.timeout_seconds) or not 0 < self.timeout_seconds <= 3600 or
                type(self.limit) is not int or not 1 <= self.limit <= 20 or
                type(self.max_response_bytes) is not int or self.max_response_bytes <= 0):
            raise ValueError("Invalid search limits")

    @classmethod
    def from_env(cls) -> "SearchConfig":
        return cls(endpoint=os.getenv("SEARCH_URL"), api_key=os.getenv("SEARCH_API_KEY"),
                   timeout_seconds=float(os.getenv("SEARCH_TIMEOUT_SECONDS", "10")),
                   limit=int(os.getenv("SEARCH_LIMIT", "5")),
                   max_response_bytes=int(os.getenv("SEARCH_MAX_RESPONSE_BYTES", "1000000")))


def _strict_wire_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Adapt Pydantic defaults/maps to a closed strict-output provider schema.

    Arbitrary-key dictionaries become [{key, value}], since strict structured
    outputs require closed objects. The gateway restores them before validation.
    """
    def convert(node: Any) -> Any:
        if isinstance(node, list):
            return [convert(item) for item in node]
        if not isinstance(node, dict):
            return node
        result = {key: convert(value) for key, value in node.items() if key != "default"}
        if node.get("type") == "object":
            additional = node.get("additionalProperties")
            if isinstance(additional, dict) and not node.get("properties"):
                return {"type": "array", "description": "Dictionary encoded as unique key/value entries.",
                        "items": {"type": "object", "properties": {"key": {"type": "string"},
                                 "value": convert(additional)}, "required": ["key", "value"],
                                 "additionalProperties": False}}
            result["additionalProperties"] = False
            result["required"] = list(result.get("properties", {}))
        return result
    return convert(schema)


def _restore_wire_maps(value: Any, schema: dict[str, Any], root: dict[str, Any]) -> Any:
    if "$ref" in schema:
        reference = schema["$ref"]
        if not reference.startswith("#/"):
            raise ValueError("Only local schema references are supported")
        target = root
        for part in reference[2:].split("/"):
            target = target[part.replace("~1", "/").replace("~0", "~")]
        return _restore_wire_maps(value, target, root)
    if value is None:
        return None
    for union_key in ("anyOf", "oneOf"):
        if union_key in schema:
            choices = [item for item in schema[union_key] if item.get("type") != "null"]
            if len(choices) == 1:
                return _restore_wire_maps(value, choices[0], root)
    if schema.get("type") == "object":
        additional = schema.get("additionalProperties")
        if isinstance(additional, dict) and not schema.get("properties"):
            if not isinstance(value, list):
                raise ValueError("Strict dictionary must use key/value entries")
            result = {}
            for item in value:
                if not isinstance(item, dict) or set(item) != {"key", "value"} or not isinstance(item["key"], str):
                    raise ValueError("Invalid dictionary entry")
                if item["key"] in result:
                    raise ValueError("Duplicate dictionary key")
                result[item["key"]] = _restore_wire_maps(item["value"], additional, root)
            return result
        if isinstance(value, dict):
            properties = schema.get("properties", {})
            return {key: _restore_wire_maps(item, properties.get(key, {}), root) for key, item in value.items()}
    if schema.get("type") == "array" and isinstance(value, list):
        return [_restore_wire_maps(item, schema.get("items", {}), root) for item in value]
    return value


async def _post(*, endpoint: str | None, api_key: str | None, payload: dict[str, Any],
                timeout: float, max_bytes: int, service: str,
                client: httpx.AsyncClient | None, sse: bool = False,
                max_event_bytes=8*1024*1024, max_stream_bytes=64*1024*1024,
                total_timeout=300.0, diagnostics=None) -> Any:
    if not endpoint:
        raise GatewayError(f"{service}_NOT_CONFIGURED", f"{service} service is not configured")
    headers = {"Content-Type": "application/json", "Accept": "text/event-stream" if sse else "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    own_client = client is None
    transport_client = client or httpx.AsyncClient(follow_redirects=False)
    stats = diagnostics if diagnostics is not None else {}
    decoder = ResponsesStreamDecoder(max_output_bytes=max_bytes, max_event_bytes=max_event_bytes,
                                     max_stream_bytes=max_stream_bytes) if sse else None
    started, first_event = time.monotonic(), None
    stats.update(transport_bytes=0, output_bytes=0, completed=False, transport_phase="connect")
    try:
        async with asyncio.timeout(total_timeout):
            async with transport_client.stream("POST", endpoint, json=payload, headers=headers,
                                               timeout=timeout, follow_redirects=False) as response:
                stats.update(http_status=response.status_code, transport_phase="stream" if sse else "body")
                if response.status_code >= 300:
                    raise GatewayError(f"{service}_HTTP_ERROR", f"{service} service returned HTTP {response.status_code}",
                                       retryable=response.status_code == 429 or response.status_code >= 500)
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    if first_event is None:
                        first_event = round((time.monotonic()-started)*1000)
                        stats['first_byte_ms'] = first_event
                    if decoder is not None:
                        try:
                            result = decoder.feed(chunk)
                        except (ValueError, UnicodeDecodeError, TypeError, KeyError, RecursionError) as exc:
                            raise GatewayError('MODEL_INVALID_RESPONSE', 'Model returned invalid SSE/JSON',
                                               details=decoder.diagnostics()) from exc
                        if result is not None:
                            return result  # Terminal event is sufficient; never wait for socket EOF.
                        continue
                    size += len(chunk)
                    stats['transport_bytes'] = size
                    if size > max_bytes:
                        raise GatewayError(f"{service}_INVALID_RESPONSE", f"{service} response exceeds size limit",
                                           details={'transport_bytes': size, 'limit_bytes': max_bytes})
                    chunks.append(chunk)
                if decoder is not None:
                    try:
                        return decoder.feed(b'', eof=True)
                    except (ValueError, UnicodeDecodeError, TypeError, KeyError, RecursionError) as exc:
                        raise GatewayError('MODEL_INVALID_RESPONSE', 'Model returned invalid SSE/JSON',
                                           details=decoder.diagnostics()) from exc
                try:
                    decoded = b"".join(chunks).decode("utf-8")
                    result = _decode_json(decoded)
                    stats.update(completed=True, output_bytes=size)
                    return result
                except (ValueError, UnicodeDecodeError, RecursionError) as exc:
                    raise GatewayError(f"{service}_INVALID_RESPONSE", f"{service} returned invalid JSON") from exc
    except httpx.TimeoutException as exc:
        raise GatewayError(f"{service}_TIMEOUT", f"{service} request timed out", retryable=True,
                           details={'phase': 'network_inactivity', 'exception_type': type(exc).__name__}) from exc
    except httpx.RequestError as exc:
        raise GatewayError(f"{service}_UNAVAILABLE", f"{service} request failed", retryable=True,
                           details={'exception_type': type(exc).__name__}) from exc
    except TimeoutError as exc:
        raise GatewayError(f"{service}_DEADLINE", f"{service} total request deadline exceeded", retryable=True,
                           details={'phase': 'total_request'}) from exc
    except GatewayError as exc:
        exc.details.update(stats)
        if decoder is not None:
            exc.details.update(decoder.diagnostics())
        raise
    finally:
        if decoder is not None:
            stats.update(decoder.diagnostics())
        stats['latency_ms'] = round((time.monotonic()-started)*1000)
        error = sys.exc_info()[1]
        if isinstance(error, GatewayError):
            error.details.update(stats)
        if own_client:
            await transport_client.aclose()


def _decode_responses_stream(body: str) -> dict[str, Any]:
    decoder = ResponsesStreamDecoder(max_output_bytes=4*1024*1024,
        max_event_bytes=8*1024*1024, max_stream_bytes=64*1024*1024)
    return decoder.feed(body.encode('utf-8')) or decoder.feed(b'', eof=True)


class _ClientOwner:
    def _http_client(self):
        if self.client is not None:
            return self.client
        if self._owned_client is None:
            self._owned_client = httpx.AsyncClient(follow_redirects=False)
        return self._owned_client

    async def aclose(self):
        if self._owned_client is not None:
            await self._owned_client.aclose()
            self._owned_client = None


class HttpModelGateway(_ClientOwner):
    def __init__(self, config: ModelConfig | None = None, *, client: httpx.AsyncClient | None = None,
                 token_provider: Callable[[], Awaitable[str]] | None = None):
        self.config = config or ModelConfig.from_env()
        self.client = client
        self._owned_client = None
        self.token_provider = token_provider

    async def reply(self, *, role: str, system_prompt: str, context: dict[str, Any],
                    output_schema: dict[str, Any], diagnostics_callback=None, images=None) -> dict[str, Any]:
        config = self.config
        wire_schema = output_schema
        compact_planner = False
        access_token = config.access_token
        if config.auth_mode == "chatgpt_subscription":
            if self.token_provider is not None:
                access_token = await self.token_provider()
            if not access_token:
                raise GatewayError("MODEL_NOT_CONFIGURED", "Application ChatGPT sign-in is required")
        if config.protocol == "json":
            payload = {"role": role, "system_prompt": system_prompt, "context": context,
                       "output_schema": wire_schema}
            if config.model:
                payload["model"] = config.model
        elif config.protocol == "chat_completions":
            payload = {
                "model": config.model,
                "messages": [{"role": "system", "content": system_prompt},
                             {"role": "user", "content": json.dumps(context, ensure_ascii=False)}],
                "response_format": {"type": "json_schema", "json_schema": {
                    "name": f"{role}_reply", "strict": True, "schema": _strict_wire_schema(wire_schema)}},
            }
        else:
            payload = {"model": config.model, "instructions": system_prompt,
                       "input": [{"role": "user", "content": json.dumps(context, ensure_ascii=False)}],
                       "reasoning": {"effort": config.reasoning_effort}, "service_tier": config.service_tier,
                       "store": False, "stream": True,
                       "text": {"format": {"type": "json_schema", "name": f"{role}_reply",
                                            "strict": True, "schema": _strict_wire_schema(wire_schema)}}}
        if config.protocol == "chat_completions" and config.json_object_mode:
            payload["response_format"] = {"type": "json_object"}
            payload["messages"][0]["content"] += "\n只输出一个符合下列 JSON Schema 的 JSON 对象。\n" + json.dumps(wire_schema, ensure_ascii=False)
        if images:
            if os.getenv('ALGORITHM_V3_VISION_ENABLED') != '1' or role not in {'V1','M1_OCR','S0'}:
                raise GatewayError('VISION_UNAVAILABLE', 'Vision model capability has not been enabled and evaluated')
            if config.protocol == 'responses':
                payload['input'][0]['content'] = [{'type':'input_text','text':json.dumps(context,ensure_ascii=False)}] + [
                    {'type':'input_image','image_url':image} for image in images]
            elif config.protocol == 'chat_completions':
                payload['messages'][1]['content'] = [{'type':'text','text':json.dumps(context,ensure_ascii=False)}] + [
                    {'type':'image_url','image_url':{'url':image}} for image in images]
            else:
                raise GatewayError('VISION_UNAVAILABLE', 'Configured protocol has no supported image transport')
        credential = access_token if config.auth_mode == "chatgpt_subscription" else config.api_key
        diagnostics = {'planner_execution_state_owned_by_runtime': compact_planner,
                       'wire_schema_sha256': hashlib.sha256(json.dumps(wire_schema, sort_keys=True).encode()).hexdigest()}
        try:
            raw = await _post(endpoint=config.endpoint, api_key=credential, payload=payload,
                          timeout=config.timeout_seconds, max_bytes=config.max_response_bytes,
                          service="MODEL", client=self._http_client(), sse=config.protocol == "responses",
                          max_event_bytes=config.max_event_bytes, max_stream_bytes=config.max_stream_bytes,
                          total_timeout=config.total_timeout_seconds, diagnostics=diagnostics)
            if isinstance(raw,dict):
                usage=raw.get('usage')
                if isinstance(usage,dict):diagnostics['usage']=usage
                diagnostics.update(model=raw.get('model') or config.model,reasoning_effort=config.reasoning_effort,
                    service_tier=raw.get('service_tier') or config.service_tier)
        finally:
            logging.getLogger(__name__).info('Model transport metrics', extra={'model_role': role, 'metrics': diagnostics})
            if diagnostics_callback is not None:
                try:
                    diagnostics_callback(dict(diagnostics))
                except Exception:
                    logging.getLogger(__name__).warning('Model metrics callback failed')
        try:
            if config.protocol == "responses":
                content = [content for item in raw["output"] if item.get("type") == "message"
                           for content in item.get("content", [])]
                if any(item.get("type") == "refusal" for item in content):
                    raise GatewayError("MODEL_REFUSAL", "Model declined to produce the requested draft")
                output = _decode_json("".join(item["text"] for item in content if item.get("type") == "output_text"))
            elif config.protocol == "chat_completions":
                output = raw["choices"][0]["message"]["content"]
                output = _decode_json(output) if isinstance(output, str) else output
            else:
                output = raw.get("output", raw) if isinstance(raw, dict) else raw
            if not isinstance(output, dict):
                raise ValueError("Expected object")
            if config.protocol in {"chat_completions", "responses"} and not config.json_object_mode:
                output = _restore_wire_maps(output, wire_schema, wire_schema)
            return output
        except (KeyError, IndexError, TypeError, ValueError, RecursionError) as exc:
            if config.protocol == "chat_completions" and config.json_object_mode:
                raise GatewayError("MODEL_JSON_OBJECT_MISSING", "Model returned no valid JSON object",
                                   retryable=True, details=diagnostics) from exc
            raise GatewayError("MODEL_INVALID_RESPONSE", "Model returned no structured JSON object",
                               details=diagnostics) from exc



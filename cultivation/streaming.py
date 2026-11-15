"""Incremental Responses SSE framing with independent transport/event/text budgets."""
from collections import Counter
from io import StringIO
import json
import re

from .errors import GatewayError


def decode_json(text):
    def invalid_constant(_):
        raise ValueError('Non-finite JSON number')
    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('Duplicate JSON key')
            result[key] = value
        return result
    return json.loads(text, parse_constant=invalid_constant, object_pairs_hook=unique_pairs)


class ResponsesStreamDecoder:
    def __init__(self, *, max_output_bytes, max_event_bytes, max_stream_bytes):
        self.max_output_bytes, self.max_event_bytes = max_output_bytes, max_event_bytes
        self.max_stream_bytes = max_stream_bytes
        self.buffer, self.scan_from = bytearray(), 0
        self.data, self.has_data, self.first_line = StringIO(), False, True
        self.event_bytes, self.wire_bytes, self.output_bytes = 0, 0, 0
        self.peak_event_bytes, self.peak_buffer_bytes, self.event_count = 0, 0, 0
        self.parts, self.counts, self.completed = {}, Counter(), False

    def diagnostics(self):
        return {'transport_bytes': self.wire_bytes, 'output_bytes': self.output_bytes,
                'peak_event_bytes': self.peak_event_bytes, 'peak_buffer_bytes': self.peak_buffer_bytes,
                'event_count': self.event_count, 'completed': self.completed,
                'event_types': dict(self.counts)}

    def fail(self, code, message, *, retryable=False, **details):
        raise GatewayError(code, message, retryable=retryable,
                           details={**self.diagnostics(), **details})

    def feed(self, chunk, *, eof=False):
        self.wire_bytes += len(chunk)
        if self.wire_bytes > self.max_stream_bytes:
            self.fail('MODEL_STREAM_LIMIT', 'Model transport stream exceeds configured budget',
                      limit_bytes=self.max_stream_bytes)
        for start in range(0, len(chunk), 16384):
            result = self._consume(chunk[start:start+16384])
            if result is not None:
                return result
        if eof:
            return self._consume(b'', eof=True)
        return None

    def _consume(self, chunk, *, eof=False):
        self.buffer.extend(chunk)
        self.peak_buffer_bytes = max(self.peak_buffer_bytes, self.event_bytes + len(self.buffer))
        while True:
            match = re.search(b'\r\n|\r|\n', self.buffer[self.scan_from:])
            if match is None:
                self.scan_from = max(0, len(self.buffer)-1)
                break
            start, end = self.scan_from + match.start(), self.scan_from + match.end()
            if self.buffer[start:end] == b'\r' and end == len(self.buffer) and not eof:
                self.scan_from = start
                break
            raw = bytes(self.buffer[:start])
            del self.buffer[:end]
            self.scan_from = 0
            result = self._line(raw, end)
            if result is not None:
                return result
        pending = self.event_bytes + len(self.buffer)
        self.peak_buffer_bytes = max(self.peak_buffer_bytes, pending)
        if pending > self.max_event_bytes:
            self.fail('MODEL_EVENT_LIMIT', 'Model SSE event exceeds configured budget',
                      limit_bytes=self.max_event_bytes)
        if eof:
            # An unframed trailing event is not a completed SSE message.
            self.fail('MODEL_STREAM_INTERRUPTED', 'Model stream ended before response.completed', retryable=True)
        return None

    def _line(self, raw, framed_bytes):
        self.event_bytes += framed_bytes
        self.peak_event_bytes = max(self.peak_event_bytes, self.event_bytes)
        if self.event_bytes > self.max_event_bytes:
            self.fail('MODEL_EVENT_LIMIT', 'Model SSE event exceeds configured budget',
                      limit_bytes=self.max_event_bytes)
        if self.first_line:
            raw = raw.removeprefix(b'\xef\xbb\xbf')
            self.first_line = False
        if not raw:
            data = self.data.getvalue()
            self.data = StringIO()
            had_data, self.has_data, self.event_bytes = self.has_data, False, 0
            if had_data and data != '[DONE]':
                return self._event(decode_json(data))
        elif raw.startswith(b'data:'):
            value = raw[5:].removeprefix(b' ')
            if self.has_data:
                self.data.write('\n')
            self.data.write(value.decode('utf-8'))
            self.has_data = True
        return None

    def _event(self, event):
        if not isinstance(event, dict):
            raise ValueError('Invalid SSE event')
        kind = event.get('type')
        self.event_count += 1
        label = kind if kind in {'response.created', 'response.output_text.delta', 'response.completed',
            'response.failed', 'response.incomplete', 'error'} else 'other'
        self.counts[label] += 1
        if kind in {'response.failed', 'response.incomplete', 'error'}:
            response = event.get('response') or {}
            if not isinstance(response, dict):
                raise ValueError('Invalid failed response')
            error = response.get('error') or event.get('error') or {}
            code = error.get('code') if isinstance(error, dict) else None
            if code == 'subscription_sharing_usage_limit_exceeded':
                self.fail('MODEL_USAGE_LIMIT', 'ChatGPT subscription usage limit reached', retryable=True)
            if code == 'subscription_sharing_usage_unavailable':
                self.fail('MODEL_SUBSCRIPTION_UNAVAILABLE', 'ChatGPT subscription usage is unavailable', retryable=True)
            self.fail('MODEL_INCOMPLETE' if kind == 'response.incomplete' else 'MODEL_RESPONSE_FAILED',
                      'Model stream did not complete successfully', retryable=True)
        if kind == 'response.output_text.delta':
            delta = event.get('delta')
            if not isinstance(delta, str):
                raise ValueError('Invalid output text delta')
            self.output_bytes += len(delta.encode('utf-8'))
            if self.output_bytes > self.max_output_bytes:
                self.fail('MODEL_OUTPUT_LIMIT', 'Model output text exceeds configured budget',
                          limit_bytes=self.max_output_bytes)
            key = (event.get('output_index', 0), event.get('content_index', 0))
            if any(type(index) is not int or index < 0 for index in key):
                raise ValueError('Invalid output text index')
            if key not in self.parts:
                if len(self.parts) >= 128:
                    self.fail('MODEL_INVALID_RESPONSE', 'Model returned too many text parts')
                self.parts[key] = StringIO()
            self.parts[key].write(delta)
        if kind == 'response.completed':
            response = event.get('response')
            if not isinstance(response, dict) or response.get('status', 'completed') != 'completed':
                raise ValueError('Invalid completed response')
            output = response.get('output', [])
            if not isinstance(output, list):
                raise ValueError('Invalid output items')
            # Keep only application-visible message content, not encrypted reasoning.
            messages = [item for item in output if isinstance(item, dict) and item.get('type') == 'message']
            if any(not isinstance(item.get('content', []), list) for item in messages):
                raise ValueError('Invalid output content')
            content = [c for item in messages for c in item.get('content', [])]
            if any(not isinstance(c, dict) for c in content):
                raise ValueError('Invalid output content part')
            text = ''.join(c['text'] for c in content if c.get('type') == 'output_text')
            size = len(text.encode('utf-8'))
            if size > self.max_output_bytes:
                self.fail('MODEL_OUTPUT_LIMIT', 'Model output text exceeds configured budget',
                          limit_bytes=self.max_output_bytes)
            accumulated = ''.join(part.getvalue() for _, part in sorted(self.parts.items()))
            if text and self.parts and text != accumulated:
                self.fail('MODEL_STREAM_MISMATCH', 'Completed output differs from streamed text')
            if not messages and self.parts:
                messages = [{'type': 'message', 'content': [{'type': 'output_text', 'text': accumulated}]}]
                size = len(accumulated.encode('utf-8'))
            self.output_bytes = size
            self.completed = True
            result={'status': 'completed', 'output': messages}
            # Retain billing/profile metadata while discarding encrypted reasoning
            # and every other non-message provider payload.
            for name in ('usage','model','service_tier'):
                if name in response:result[name]=response[name]
            return result
        return None

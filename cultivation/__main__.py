"""Run a single permission-scoped pet turn in a disposable local job store."""
import argparse
import asyncio
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from style.pet_style import active_cards
from .materials_v3.contracts import Budget
from .materials_v3.jobs import JobStore
from .phase4.contracts import PetInput
from .planning_v3.worker import Worker


def example(text):
    pet, room = str(uuid4()), str(uuid4())
    return PetInput(
        requestId=str(uuid4()), inputRevision=1, asOf=datetime.now(timezone.utc),
        scope=dict(type="pet_reply", petId=pet, conversationId=room),
        petId=pet, conversationId=room, conversationRevision=1,
        message=dict(clientMessageId=str(uuid4()), userMessageId=str(uuid4()),
                     replyMessageId=str(uuid4()), text=text),
        consentSnapshot=dict(consentRevision=1),
        styleSnapshot=dict(activeSkills=active_cards({"personality": "俏皮嘴硬"}, text)),
    )


async def run_turn(request, model=None):
    with tempfile.TemporaryDirectory() as folder:
        store = JobStore(Path(folder) / "turn.sqlite")
        owner = "local-example"
        job = store.create(owner, request.requestId, "pet-reply-jobs",
                           request.model_dump(mode="json"), Budget(modelCalls=6).model_dump())
        await Worker(store, model=model).once()
        row = store.get(owner, job["id"])
        if row["state"] != "SUCCEEDED" or not row["result"].get("canPublish"):
            raise RuntimeError((row.get("error") or {}).get("code") or "PET_REVIEW_REQUIRED")
        return row["result"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--text")
    group.add_argument("--request", type=Path, help="PetInput JSON file")
    args = parser.parse_args()
    if not all(os.getenv(k) for k in ("PET_MODEL_URL", "PET_MODEL_NAME", "PET_MODEL_API_KEY")):
        parser.error("需要设置 PET_MODEL_URL、PET_MODEL_NAME 和 PET_MODEL_API_KEY")
    req = PetInput.model_validate_json(args.request.read_text("utf-8")) if args.request else example(args.text)
    print(json.dumps(asyncio.run(run_turn(req)), ensure_ascii=False, indent=2))

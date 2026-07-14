from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
import sys

import pytest

from companion_daemon.db import CompanionStore
from companion_daemon.engine import seed_user
from companion_daemon.world import WorldKernel

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.audit_world_memory_runtime import run_audit


def test_world_validator_allows_current_user_source_to_support_speaker_rewrite(
    tmp_path: Path,
) -> None:
    store = CompanionStore(tmp_path / "world-memory-validator.sqlite")
    seed_user(store)
    world = WorldKernel(store)
    world_id = world.start_from_seed_file(Path("configs/world_seed.yaml")).world_id
    world.submit(
        {
            "type": "register_user",
            "world_id": world_id,
            "user_id": "user:geoff",
            "name": "geoff",
            "idempotency_key": "register-user:geoff",
        },
        expected_revision=world.revision(world_id),
    )
    first_at = datetime(2026, 7, 14, 9, 0, tzinfo=UTC)
    world.submit(
        {
            "type": "observe_user_message",
            "world_id": world_id,
            "message_id": "qq:geoff:memory-1",
            "user_id": "user:geoff",
            "text": "我人在安徽老家，家里的床很软。",
            "sent_at": first_at.isoformat(),
            "idempotency_key": "memory-1",
        },
        expected_revision=world.revision(world_id),
    )
    world.submit(
        {
            "type": "observe_user_message",
            "world_id": world_id,
            "message_id": "qq:geoff:memory-2",
            "user_id": "user:geoff",
            "text": "我刚睡醒，还有点想赖床。",
            "sent_at": (first_at + timedelta(minutes=1)).isoformat(),
            "idempotency_key": "memory-2",
        },
        expected_revision=world.revision(world_id),
    )

    accepted = world.validate_reply_candidate(
        world_id,
        {
            "reply_text": "你刚睡醒的话，安徽老家那张很软的床确实会让人更想赖一会儿。",
            "mentioned_event_ids": ["message:qq:geoff:memory-1"],
            "proposed_action_ids": [],
            "claims": [
                {
                    "source_id": "message:qq:geoff:memory-1",
                    "text": "我人在安徽老家，家里的床很软",
                    "assertion": "安徽老家那张很软的床",
                }
            ],
        },
        user_id="user:geoff",
    )

    assert accepted["reply_text"].startswith("你刚睡醒的话")
    assert accepted["claims"][0]["assertion"] == "安徽老家那张很软的床"


@pytest.mark.asyncio
async def test_world_memory_runtime_audit_closes_fact_prompt_and_reply_loop() -> None:
    report = await run_audit()

    assert report["ok"] is True
    assert any("安徽" in value for value in report["fact_values"])
    assert report["second_prompt_contains_anhui"] is True
    assert "安徽" in str(report["second_reply"])

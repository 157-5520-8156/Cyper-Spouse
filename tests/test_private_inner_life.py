from datetime import timedelta
import json
from pathlib import Path

import pytest

from companion_daemon.db import CompanionStore
from companion_daemon.engine import CompanionEngine
from companion_daemon.models import IncomingMessage
from companion_daemon.world import WorldError, WorldKernel

from test_world_kernel import NOW, world_seed


def _started_kernel(tmp_path: Path) -> tuple[WorldKernel, str]:
    kernel = WorldKernel(CompanionStore(tmp_path / "inner-life.sqlite"))
    started = kernel.submit({"type": "start_world", "seed": world_seed()}, expected_revision=0)
    registered = kernel.submit(
        {
            "type": "register_user",
            "world_id": started.world_id,
            "user_id": "user:geoff",
            "name": "geoff",
        },
        expected_revision=started.revision,
    )
    kernel.submit(
        {
            "type": "observe_user_message",
            "world_id": started.world_id,
            "message_id": "m:disappointed",
            "user_id": "user:geoff",
            "text": "没事，你先忙。",
            "sent_at": NOW.isoformat(),
        },
        expected_revision=registered.revision,
    )
    return kernel, started.world_id


def test_private_impression_is_replayable_fallible_and_never_becomes_user_fact(
    tmp_path: Path,
) -> None:
    kernel, world_id = _started_kernel(tmp_path)
    committed = kernel.commit_private_impression(
        world_id,
        impression_id="impression:disappointed",
        user_id="user:geoff",
        kind="possible_disappointment",
        summary="我感觉他可能因为刚才没有被接住而失望。",
        confidence=0.78,
        source_event_ids=("message:m:disappointed",),
        expires_at=NOW + timedelta(days=7),
        expected_revision=kernel.revision(world_id),
    )

    state = kernel.snapshot(world_id)
    impression = state["private_impressions"]["impression:disappointed"]

    assert committed.events[-1].event_type == "PrivateImpressionCommitted"
    assert impression["status"] == "active"
    assert impression["confidence"] == pytest.approx(0.78)
    assert impression["source_event_ids"] == ["message:m:disappointed"]
    assert not state["facts"]
    assert kernel.rebuild_projection(world_id, "world_current_state").matches_live is True


def test_private_impression_needs_committed_provenance_and_can_be_contradicted(
    tmp_path: Path,
) -> None:
    kernel, world_id = _started_kernel(tmp_path)

    with pytest.raises(WorldError, match="committed source"):
        kernel.commit_private_impression(
            world_id,
            impression_id="impression:ungrounded",
            user_id="user:geoff",
            kind="possible_disappointment",
            summary="我猜他不高兴。",
            confidence=0.6,
            source_event_ids=("message:missing",),
            expires_at=NOW + timedelta(days=1),
            expected_revision=kernel.revision(world_id),
        )

    created = kernel.commit_private_impression(
        world_id,
        impression_id="impression:disappointed",
        user_id="user:geoff",
        kind="possible_disappointment",
        summary="我感觉他可能失望。",
        confidence=0.7,
        source_event_ids=("message:m:disappointed",),
        expires_at=NOW + timedelta(days=1),
        expected_revision=kernel.revision(world_id),
    )
    observed = kernel.submit(
        {
            "type": "observe_user_message",
            "world_id": world_id,
            "message_id": "m:clarified",
            "user_id": "user:geoff",
            "text": "我不是失望，只是在赶事。",
            "sent_at": NOW.isoformat(),
        },
        expected_revision=created.revision,
    )
    contradicted = kernel.contradict_private_impression(
        world_id,
        impression_id="impression:disappointed",
        source_event_ids=("message:m:clarified",),
        reason="用户明确说明只是暂时忙。",
        expected_revision=observed.revision,
    )

    impression = kernel.snapshot(world_id)["private_impressions"]["impression:disappointed"]
    assert contradicted.events[-1].event_type == "PrivateImpressionContradicted"
    assert impression["status"] == "contradicted"
    assert impression["contradictory_evidence"] == ["message:m:clarified"]


def test_private_commitment_is_not_a_plan_or_completed_experience(tmp_path: Path) -> None:
    kernel, world_id = _started_kernel(tmp_path)
    committed = kernel.commit_private_commitment(
        world_id,
        commitment_id="commitment:listen-later",
        user_id="user:geoff",
        intention="等他愿意时再把刚才没说完的事听完。",
        source_event_ids=("message:m:disappointed",),
        expires_at=NOW + timedelta(days=3),
        priority=65,
        related_thread_id="",
        expected_revision=kernel.revision(world_id),
    )

    state = kernel.snapshot(world_id)
    commitment = state["private_commitments"]["commitment:listen-later"]

    assert committed.events[-1].event_type == "PrivateCommitmentCommitted"
    assert commitment["status"] == "active"
    assert commitment["related_thread_id"] == ""
    assert "commitment:listen-later" not in state["actions"]
    assert not state["experiences"]


def test_private_inner_life_expires_from_logical_clock_without_action_side_effect(
    tmp_path: Path,
) -> None:
    kernel, world_id = _started_kernel(tmp_path)
    impression = kernel.commit_private_impression(
        world_id,
        impression_id="impression:short",
        user_id="user:geoff",
        kind="continuity_note",
        summary="这句可能还有没说完的意思。",
        confidence=0.6,
        source_event_ids=("message:m:disappointed",),
        expires_at=NOW + timedelta(hours=1),
        expected_revision=kernel.revision(world_id),
    )
    commitment = kernel.commit_private_commitment(
        world_id,
        commitment_id="commitment:short",
        user_id="user:geoff",
        intention="等合适时再听他说。",
        source_event_ids=("message:m:disappointed",),
        expires_at=NOW + timedelta(hours=1),
        priority=50,
        expected_revision=impression.revision,
    )

    advanced = kernel.advance(
        world_id,
        NOW + timedelta(hours=2),
        expected_revision=commitment.revision,
    )
    state = kernel.snapshot(world_id)

    assert {event.event_type for event in advanced.events} >= {
        "PrivateImpressionExpired",
        "PrivateCommitmentExpired",
    }
    assert state["private_impressions"]["impression:short"]["status"] == "expired"
    assert state["private_commitments"]["commitment:short"]["status"] == "expired"
    assert not state["actions"]
    assert all(
        item.get("action_id") != "commitment:short"
        for item in state["experiences"].values()
    )


@pytest.mark.asyncio
async def test_world_turn_selectively_commits_inner_impression_and_question_commitment(
    tmp_path: Path,
) -> None:
    class QuestionReplyModel:
        async def complete(self, messages, *, temperature: float) -> str:
            joined = "\n".join(item["content"] for item in messages)
            if "WorldReplyJSON" in joined:
                return json.dumps(
                    {
                        "reply_text": "刚才那句我没接住。你愿意把后面也说完吗？",
                        "mentioned_event_ids": [],
                        "proposed_action_ids": [],
                        "claims": [],
                    },
                    ensure_ascii=False,
                )
            return '{"supported": true, "unsupported_spans": [], "reason": "ok"}'

    kernel, world_id = _started_kernel(tmp_path)
    engine = CompanionEngine(
        kernel.store,
        QuestionReplyModel(),
        "你是沈知栀。",
        world_kernel=kernel,
        world_id=world_id,
    )

    await engine.handle_message(
        IncomingMessage(
            platform="qq",
            platform_user_id="geoff",
            message_id="m:before-disappointment",
            text="我今天有点累。",
            sent_at=NOW,
        )
    )
    reply = await engine.handle_message(
        IncomingMessage(
            platform="qq",
            platform_user_id="geoff",
            message_id="m:explicit-disappointment",
            text="你刚才有点敷衍，我有点失望。",
            sent_at=NOW,
        )
    )
    state = kernel.snapshot(world_id)

    assert reply is not None
    assert any(
        item["kind"] == "possible_disappointment"
        for item in state["private_impressions"].values()
    )
    assert any(
        item["status"] == "active"
        and item["intention"] == "等他愿意时，把刚才没说完的话听完。"
        for item in state["private_commitments"].values()
    )
    assert not any(
        item["kind"] == "outgoing_message"
        and item["action_id"].startswith("commitment:")
        for item in state["actions"].values()
    )

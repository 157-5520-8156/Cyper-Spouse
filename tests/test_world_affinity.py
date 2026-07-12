from datetime import UTC, datetime, timedelta
from pathlib import Path

from companion_daemon.db import CompanionStore
from companion_daemon.world import WorldKernel
from companion_daemon.world_affect import apply_appraisal, decay_affect, initial_affect
from companion_daemon.world_affinity import (
    initial_affinity,
    personality_affect_baseline,
    settle_affinity_interaction,
)


NOW = datetime(2026, 7, 11, 9, 0, tzinfo=UTC)


def _seed() -> dict[str, object]:
    return {
        "world_id": "affinity-world",
        "logical_at": NOW.isoformat(),
        "protagonist": {
            "id": "zhizhi",
            "name": "沈知栀",
            "kind": "companion",
            "stable_traits": ["温和、敏感、观察力强", "慢热，有自己的判断"],
            "values": ["真诚比漂亮话重要"],
        },
    }


def test_personality_baseline_is_deterministic_and_affect_decays_back_to_it() -> None:
    protagonist = _seed()["protagonist"]
    baseline = personality_affect_baseline(protagonist)

    assert baseline == {
        "hurt": 0,
        "anger": 0,
        "sadness": 0,
        "loneliness": 0,
        "anxiety": 0,
        "resentment": 0,
        "warmth": 6,
        "joy": 2,
    }
    initial = initial_affect(NOW.isoformat(), protagonist=protagonist)
    warmed = apply_appraisal(initial, "warmth_received", NOW.isoformat())
    assert warmed.vector["warmth"] == 11

    decayed = decay_affect(
        {**initial, **warmed.__dict__},
        int(timedelta(hours=24).total_seconds()),
        (NOW + timedelta(hours=24)).isoformat(),
    )

    assert decayed.vector["warmth"] == 6
    assert decayed.vector["joy"] == 2


def test_affinity_changes_only_after_repeated_settled_interactions_and_each_change_is_small() -> None:
    state = initial_affinity()

    first = settle_affinity_interaction(
        state,
        user_id="user:geoff",
        appraisal="warmth_received",
        settlement_id="turn:1",
        logical_at=NOW.isoformat(),
    )
    second = settle_affinity_interaction(
        first.state,
        user_id="user:geoff",
        appraisal="warmth_received",
        settlement_id="turn:2",
        logical_at=(NOW + timedelta(minutes=1)).isoformat(),
    )
    third = settle_affinity_interaction(
        second.state,
        user_id="user:geoff",
        appraisal="warmth_received",
        settlement_id="turn:3",
        logical_at=(NOW + timedelta(minutes=2)).isoformat(),
    )

    assert first.delta == {}
    assert second.delta == {}
    assert third.delta == {"warmth": 1}
    assert third.state["vector"] == {"warmth": 1}
    assert max(abs(value) for value in third.delta.values()) <= 1


def test_affinity_settlement_is_idempotent_for_replay() -> None:
    first = settle_affinity_interaction(
        initial_affinity(),
        user_id="user:geoff",
        appraisal="boundary_violation",
        settlement_id="turn:1",
        logical_at=NOW.isoformat(),
    )

    repeated = settle_affinity_interaction(
        first.state,
        user_id="user:geoff",
        appraisal="boundary_violation",
        settlement_id="turn:1",
        logical_at=NOW.isoformat(),
    )

    assert repeated.state == first.state
    assert repeated.delta == {}
    assert repeated.duplicate is True


def test_world_records_affinity_only_when_a_turn_is_delivered(tmp_path: Path) -> None:
    kernel = WorldKernel(CompanionStore(tmp_path / "affinity.sqlite"))
    started = kernel.submit({"type": "start_world", "seed": _seed()}, expected_revision=0)
    kernel.submit(
        {
            "type": "register_user",
            "world_id": started.world_id,
            "user_id": "user:geoff",
            "name": "geoff",
        },
        expected_revision=started.revision,
    )

    for index, status in enumerate(("failed", "delivered", "delivered", "delivered"), start=1):
        message_id = f"message:{index}"
        assert kernel.claim_message_turn(started.world_id, message_id)
        kernel.submit(
            {
                "type": "appraise_turn",
                "world_id": started.world_id,
                "intent_id": f"intent:{index}",
                "message_id": message_id,
                "user_id": "user:geoff",
                "appraisal": "warmth_received",
            },
            expected_revision=kernel.revision(started.world_id),
        )
        kernel.settle_turn(
            started.world_id,
            message_id,
            status=status,
            reason=f"test_{status}",
            expected_revision=kernel.revision(started.world_id),
        )

    affinity = kernel.snapshot(started.world_id)["long_term_affinity"]["user:geoff"]
    assert affinity["vector"] == {"warmth": 1}
    assert affinity["settled_interaction_count"] == 3
    events = [event for event in kernel.events(started.world_id) if event.event_type == "AffinityInteractionSettled"]
    assert len(events) == 3
    assert events[-1].payload["delta"] == {"warmth": 1}
    assert kernel.rebuild_projection(started.world_id, "world_current_state").matches_live is True


def test_adapter_delivery_promotes_deferred_turn_and_settles_affinity(tmp_path: Path) -> None:
    kernel = WorldKernel(CompanionStore(tmp_path / "adapter-affinity.sqlite"))
    started = kernel.submit({"type": "start_world", "seed": _seed()}, expected_revision=0)
    kernel.submit(
        {"type": "register_user", "world_id": started.world_id, "user_id": "user:geoff", "name": "geoff"},
        expected_revision=started.revision,
    )
    message_id = "adapter-turn"
    assert kernel.claim_message_turn(started.world_id, message_id)
    kernel.submit(
        {
            "type": "appraise_turn", "world_id": started.world_id,
            "intent_id": "intent:adapter", "message_id": message_id,
            "user_id": "user:geoff", "appraisal": "warmth_received",
        },
        expected_revision=kernel.revision(started.world_id),
    )
    delivery_id, _, _ = kernel.queue_outgoing_action(
        canonical_user_id="geoff",
        platform="qq",
        text="我听见了。",
        kind="reply",
        expires_at=NOW + timedelta(hours=1),
        trace={
            "world_id": started.world_id,
            "input_message_id": message_id,
            "user_id": "user:geoff",
            "appraisal": "warmth_received",
            "expression_policy": "自然回应",
            "allowed_facts": [],
            "observable_reason": "reply",
        },
    )
    kernel.settle_turn(
        started.world_id,
        message_id,
        status="deferred",
        reason="awaiting_external_delivery",
        expected_revision=kernel.revision(started.world_id),
    )

    segment = kernel.claim_outgoing_segment(
        delivery_id, expected_revision=kernel.revision(started.world_id)
    )
    assert segment is not None
    kernel.settle_outgoing_segment(
        delivery_id,
        segment.segment_id,
        delivered=True,
        expected_revision=kernel.revision(started.world_id),
    )

    snapshot = kernel.snapshot(started.world_id)
    assert snapshot["turns"][message_id]["status"] == "delivered"
    assert snapshot["long_term_affinity"]["user:geoff"]["settled_interaction_count"] == 1

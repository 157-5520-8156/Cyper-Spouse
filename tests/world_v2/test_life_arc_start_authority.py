from datetime import UTC, datetime, timedelta

import pytest

from companion_daemon.world_v2.biographical_lifecycle import LifeArcChangedPayload
from companion_daemon.world_v2.event_identity import domain_idempotency_key
from companion_daemon.world_v2.schemas import EvidenceRef, LifeArcProjection, WorldEvent
from companion_daemon.world_v2.sqlite_ledger import SQLiteWorldLedger


def test_life_arc_start_rejects_clock_as_settled_consequence(tmp_path) -> None:
    world_id = "world:biography:clock-cannot-create-arc"
    logical_at = datetime(2026, 7, 28, 4, 0, tzinfo=UTC)
    ledger = SQLiteWorldLedger(
        path=tmp_path / "clock-cannot-create-arc.sqlite",
        world_id=world_id,
    )
    clock = _event(
        world_id=world_id,
        event_id="clock:cannot-create-life-arc",
        event_type="ClockAdvanced",
        logical_at=logical_at,
        payload={
            "logical_time_from": (logical_at - timedelta(minutes=10)).isoformat(),
            "logical_time_to": logical_at.isoformat(),
        },
    )
    ledger.commit(
        (clock,),
        expected_world_revision=0,
        expected_deliberation_revision=0,
    )
    clock_ref = ledger.project().committed_world_event_refs[-1]
    evidence = EvidenceRef(
        ref_id=clock.event_id,
        evidence_type="committed_world_event",
        claim_purpose="life_transition",
        source_world_revision=clock_ref.world_revision,
        immutable_hash=clock_ref.payload_hash,
    )
    arc = LifeArcProjection(
        arc_id="life-arc:forged-from-clock",
        entity_revision=1,
        owner_actor_ref="actor:companion",
        arc_kind="employment",
        context_pack_ref="life-context:forged",
        context_tags=("role:forged",),
        status="active",
        started_at=logical_at,
        source_event_ref=clock.event_id,
        effect_descriptor_hash="f" * 64,
        privacy_class="personal",
    )
    payload = LifeArcChangedPayload(
        change_id="change:life-arc:forged:start",
        transition_id="transition:life-arc:forged:1",
        expected_entity_revision=0,
        evidence_refs=(evidence,),
        policy_refs=("policy:biographical-lifecycle.1",),
        operation="start",
        arc_before=None,
        arc_after=arc,
    )
    forged = _event(
        world_id=world_id,
        event_id="event:life-arc:forged:start",
        event_type="LifeArcChanged",
        logical_at=logical_at,
        payload=payload.model_dump(mode="json"),
    )
    head = ledger.project()

    with pytest.raises(ValueError, match="settled WorldOccurrence"):
        ledger.commit(
            (forged,),
            expected_world_revision=head.world_revision,
            expected_deliberation_revision=head.deliberation_revision,
        )


def _event(
    *,
    world_id: str,
    event_id: str,
    event_type: str,
    logical_at: datetime,
    payload: dict[str, object],
) -> WorldEvent:
    identity = domain_idempotency_key(
        event_type=event_type,
        world_id=world_id,
        payload=payload,
    )
    return WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id=event_id,
        world_id=world_id,
        event_type=event_type,
        logical_time=logical_at,
        created_at=logical_at,
        actor="worker:test:biography-authority",
        source="test:biography-authority",
        trace_id="trace:biography-authority",
        causation_id="cause:biography-authority",
        correlation_id="correlation:biography-authority",
        idempotency_key=identity or f"identity:{event_id}",
        payload=payload,
    )

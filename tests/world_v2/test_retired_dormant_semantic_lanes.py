"""Derived .52 retirement for character-author lanes removed by ADR 0016."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from companion_daemon.world_v2.event_identity import domain_idempotency_key
from companion_daemon.world_v2.ledger import WorldLedger
from companion_daemon.world_v2.reducers import (
    ReducerState,
    fold_retired_character_author_state,
)
from companion_daemon.world_v2.schemas import (
    CommittedWorldEventRef,
    TriggerProcess,
    WorldEvent,
)


NOW = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)
WORLD = "world:retired-character-authors"


@pytest.mark.parametrize(
    ("process_kind", "source_event_type"),
    (
        ("media_delivery_interaction", "MediaDeliveryShared"),
        ("read_only_tool_deliberation", "ObservationRecorded"),
        ("affect_deliberation", "AppraisalAccepted"),
        ("relationship_deliberation", "AppraisalAccepted"),
        ("interaction_appraisal", "ObservationRecorded"),
        ("external_result_deliberation", "ToolResultAccepted"),
        ("afterthought_author", None),
    ),
)
def test_every_physically_removed_character_author_is_folded_in_derived_head(
    process_kind: str,
    source_event_type: str | None,
) -> None:
    source_ref = f"event:source:{process_kind}" if source_event_type is not None else None
    process = TriggerProcess(
        trigger_id=f"trigger:{process_kind}:legacy",
        trigger_ref=f"legacy:{process_kind}",
        process_kind=process_kind,
        source_evidence_ref=(
            "observation:legacy"
            if process_kind in {"interaction_appraisal", "read_only_tool_deliberation"}
            else source_ref
        ),
        state="open",
    )
    committed = (
        (
            CommittedWorldEventRef(
                event_id=source_ref,
                event_type=source_event_type,
                world_revision=1,
                payload_hash="a" * 64,
                logical_time=NOW,
            ),
        )
        if source_ref is not None
        else ()
    )
    state = ReducerState(
        logical_time=NOW,
        trigger_processes=(process,),
        committed_world_event_refs=committed,
    )

    folded = fold_retired_character_author_state(state)

    terminal = folded.trigger_processes[0]
    assert terminal.state == "terminal"
    assert terminal.runtime_outcome_ref is not None
    assert terminal.runtime_outcome_ref.startswith("retired-technical:")
    assert folded.completed_trigger_ids == (process.trigger_id,)


def _event(event_id: str, event_type: str, payload: dict[str, object]) -> WorldEvent:
    identity = domain_idempotency_key(
        event_type=event_type,
        world_id=WORLD,
        payload=payload,
    )
    return WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id=event_id,
        world_id=WORLD,
        event_type=event_type,
        logical_time=NOW,
        created_at=NOW,
        actor="system:test",
        source="test",
        trace_id="trace:retired-character-authors",
        causation_id="cause:retired-character-authors",
        correlation_id="correlation:retired-character-authors",
        idempotency_key=identity or f"idempotency:{event_id}",
        payload=payload,
    )


def test_dormant_retirement_changes_no_immutable_event_count_order_or_hash() -> None:
    ledger = WorldLedger.in_memory(world_id=WORLD)
    ledger.commit(
        (_event("event:world:start", "WorldStarted", {}),),
        expected_world_revision=0,
        expected_deliberation_revision=0,
    )
    process = TriggerProcess(
        trigger_id="trigger:afterthought:legacy",
        trigger_ref="afterthought:legacy",
        process_kind="afterthought_author",
        state="open",
    )
    opened = _event(
        "event:afterthought:legacy:opened",
        "TriggerProcessOpened",
        {"process": process.model_dump(mode="json")},
    )
    before = ledger.project()
    ledger.commit(
        (opened,),
        expected_world_revision=before.world_revision,
        expected_deliberation_revision=before.deliberation_revision,
    )
    evidence_before = ledger.export_replay_evidence()
    immutable_before = tuple(
        (item.event.event_id, item.event_envelope_hash)
        for item in evidence_before.events
    )

    projection = ledger.project()
    rebuilt = ledger.rebuild()
    evidence_after = ledger.export_replay_evidence()

    terminal = next(
        item
        for item in projection.trigger_processes
        if item.trigger_id == process.trigger_id
    )
    assert terminal.state == "terminal"
    assert rebuilt == projection
    assert tuple(
        (item.event.event_id, item.event_envelope_hash)
        for item in evidence_after.events
        ) == immutable_before

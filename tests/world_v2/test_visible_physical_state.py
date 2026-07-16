from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from companion_daemon.world_v2.event_identity import domain_idempotency_key
from companion_daemon.world_v2.reducers import ReducerState, make_projection, reduce_event
from companion_daemon.world_v2.schemas import CommittedWorldEventRef, WorldEvent
from companion_daemon.world_v2.visible_physical_state import (
    MAX_VISIBLE_PHYSICAL_STATE_LIFETIME,
    VisiblePhysicalCue,
    VisiblePhysicalNegativeCue,
    VisiblePhysicalStateProjection,
    VisiblePhysicalStateRecordCommand,
    visible_physical_state_at,
)
from companion_daemon.world_v2.visible_physical_state_runtime import VisiblePhysicalStateRuntime


NOW = datetime(2026, 7, 16, 23, tzinfo=UTC)
LATER = NOW + timedelta(hours=2)
SUBJECT = "character:companion:primary"


def _source() -> WorldEvent:
    return WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id="event:activity:complete",
        event_type="ActivityCompleted",
        world_id="world:visible-physical",
        logical_time=NOW,
        created_at=NOW,
        actor="agent:companion",
        source="test",
        trace_id="trace:source",
        causation_id="cause:source",
        correlation_id="correlation:source",
        idempotency_key="source:activity",
        payload={},
    )


def _positive() -> tuple[VisiblePhysicalCue, ...]:
    return (
        VisiblePhysicalCue(
            cue_id="perspiration",
            intensity="moderate",
            visible_regions=("neck", "arm"),
            evidence_ref="evidence:activity:recovery",
        ),
    )


def _state_event(
    *,
    source: WorldEvent,
    revision: int = 1,
    at: datetime = NOW,
    positive_cues: tuple[VisiblePhysicalCue, ...] | None = None,
    negative_cues: tuple[VisiblePhysicalNegativeCue, ...] = (),
) -> WorldEvent:
    state = VisiblePhysicalStateProjection(
        physical_state_id="visible-physical:" + SUBJECT,
        subject_ref=SUBJECT,
        entity_revision=revision,
        source_event_ref=source.event_id,
        source_event_payload_hash=source.payload_hash,
        source_event_type=source.event_type,
        valid_from=at,
        valid_until=at + timedelta(hours=4),
        visibility="shareable",
        positive_cues=_positive() if positive_cues is None else positive_cues,
        negative_cues=negative_cues,
    )
    payload = {"state": state.model_dump(mode="json")}
    return WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id=f"event:visible-physical:{revision}",
        event_type="VisiblePhysicalStateRecorded",
        world_id="world:visible-physical",
        logical_time=at,
        created_at=at,
        actor="worker:visible-physical",
        source="test",
        trace_id="trace:visible-physical",
        causation_id=source.event_id,
        correlation_id="correlation:visible-physical",
        idempotency_key=domain_idempotency_key(
            event_type="VisiblePhysicalStateRecorded",
            world_id="world:visible-physical",
            payload=payload,
        )
        or "test:visible-physical",
        payload=payload,
    )


def _state_with_source(source: WorldEvent) -> ReducerState:
    return ReducerState(
        logical_time=NOW,
        committed_world_event_refs=(
            CommittedWorldEventRef(
                event_id=source.event_id,
                event_type=source.event_type,
                world_revision=1,
                payload_hash=source.payload_hash,
                logical_time=source.logical_time,
            ),
        ),
    )


def test_reducer_projects_source_bound_short_lived_state_with_replay_hash() -> None:
    source = _source()
    event = _state_event(source=source)
    reduced = reduce_event(_state_with_source(source), event)
    projection = make_projection(
        world_id="world:visible-physical",
        world_revision=2,
        deliberation_revision=0,
        ledger_sequence=2,
        state=reduced,
    )

    resolved = visible_physical_state_at(
        projection.visible_physical_states, subject_ref=SUBJECT, at_logical_time=NOW
    )
    assert resolved is not None
    assert resolved.positive_cues[0].cue_id == "perspiration"
    replayed = reduce_event(_state_with_source(source), event)
    assert (
        make_projection(
            world_id="world:visible-physical",
            world_revision=2,
            deliberation_revision=0,
            ledger_sequence=2,
            state=replayed,
        ).semantic_hash
        == projection.semantic_hash
    )


def test_negative_only_clear_state_is_resolvable_and_expires() -> None:
    source = _source()
    clear = _state_event(
        source=source,
        positive_cues=(),
        negative_cues=(
            VisiblePhysicalNegativeCue(
                cue_id="settled_breathing",
                visible_regions=("chest",),
            ),
        ),
    )
    reduced = reduce_event(_state_with_source(source), clear)

    active = visible_physical_state_at(
        reduced.visible_physical_states, subject_ref=SUBJECT, at_logical_time=NOW
    )
    assert active is not None
    assert active.has_positive_cues is False
    assert (
        visible_physical_state_at(
            reduced.visible_physical_states,
            subject_ref=SUBJECT,
            at_logical_time=NOW + MAX_VISIBLE_PHYSICAL_STATE_LIFETIME,
        )
        is None
    )


def test_reducer_closes_previous_version_before_historical_query() -> None:
    source = _source()
    first = reduce_event(_state_with_source(source), _state_event(source=source))
    second = reduce_event(
        first.model_copy(update={"logical_time": LATER}),
        _state_event(
            source=source,
            revision=2,
            at=LATER,
            positive_cues=(),
            negative_cues=(
                VisiblePhysicalNegativeCue(cue_id="dry", visible_regions=("arm",)),
            ),
        ),
    )

    assert second.visible_physical_states[0].valid_until == LATER
    assert (
        visible_physical_state_at(
            second.visible_physical_states, subject_ref=SUBJECT, at_logical_time=NOW
        ).entity_revision
        == 1
    )
    assert (
        visible_physical_state_at(
            second.visible_physical_states, subject_ref=SUBJECT, at_logical_time=LATER
        ).entity_revision
        == 2
    )


def test_contract_rejects_overlong_or_overlapping_counter_evidence() -> None:
    source = _source()
    with pytest.raises(ValueError, match="maximum lifetime"):
        VisiblePhysicalStateProjection(
            physical_state_id="visible-physical:" + SUBJECT,
            subject_ref=SUBJECT,
            entity_revision=1,
            source_event_ref=source.event_id,
            source_event_payload_hash=source.payload_hash,
            source_event_type=source.event_type,
            valid_from=NOW,
            valid_until=NOW + timedelta(hours=4, seconds=1),
            visibility="shareable",
            positive_cues=_positive(),
            negative_cues=(),
        )
    with pytest.raises(ValueError, match="positive and negative cues conflict"):
        VisiblePhysicalStateProjection(
            physical_state_id="visible-physical:" + SUBJECT,
            subject_ref=SUBJECT,
            entity_revision=1,
            source_event_ref=source.event_id,
            source_event_payload_hash=source.payload_hash,
            source_event_type=source.event_type,
            valid_from=NOW,
            valid_until=NOW + timedelta(hours=1),
            visibility="shareable",
            positive_cues=_positive(),
            negative_cues=(VisiblePhysicalNegativeCue(cue_id="dry", visible_regions=("arm",)),),
        )


def test_runtime_derives_source_coordinates_visibility_and_default_expiry() -> None:
    source = _source()
    projection = SimpleNamespace(
        world_revision=4,
        deliberation_revision=1,
        ledger_sequence=5,
        logical_time=NOW,
        committed_world_event_refs=(
            CommittedWorldEventRef(
                event_id=source.event_id,
                event_type=source.event_type,
                world_revision=4,
                payload_hash=source.payload_hash,
                logical_time=NOW,
            ),
        ),
        visible_physical_states=(),
        plans=(
            SimpleNamespace(
                authority_origin=SimpleNamespace(accepted_event_ref=source.event_id),
                privacy_class="shareable",
            ),
        ),
        world_occurrences=(),
        experiences=(),
        facts=(),
    )
    commits: list[tuple[object, object, str]] = []
    ledger = SimpleNamespace(
        world_id="world:visible-physical",
        project=lambda: projection,
        lookup_event_commit=lambda event_id: (source, object()) if event_id == source.event_id else None,
        commit_at_cursor=lambda events, expected_cursor, commit_id: commits.append(
            (events, expected_cursor, commit_id)
        ),
    )

    VisiblePhysicalStateRuntime(ledger=ledger).record(
        VisiblePhysicalStateRecordCommand(
            command_id="command:visible-physical:1",
            source_event_ref=source.event_id,
            subject_ref=SUBJECT,
            positive_cues=_positive(),
            negative_cues=(),
        ),
        logical_time=NOW,
        created_at=NOW,
        actor="worker:visible-physical",
        trace_id="trace:visible-physical",
        correlation_id="correlation:visible-physical",
    )

    event = commits[0][0][0]
    state = event.payload()["state"]
    assert event.event_type == "VisiblePhysicalStateRecorded"
    assert state["source_event_payload_hash"] == source.payload_hash
    assert state["source_event_type"] == "ActivityCompleted"
    assert state["visibility"] == "shareable"
    assert datetime.fromisoformat(state["valid_until"]) == NOW + MAX_VISIBLE_PHYSICAL_STATE_LIFETIME

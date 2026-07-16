from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from companion_daemon.world_v2.appearance_state import (
    AppearanceStateRecordCommand,
    VisibleAppearanceAttribute,
    appearance_state_at,
)
from companion_daemon.world_v2.appearance_state_runtime import AppearanceStateRuntime
from companion_daemon.world_v2.event_identity import domain_idempotency_key
from companion_daemon.world_v2.reducers import ReducerState, make_projection, reduce_event
from companion_daemon.world_v2.schemas import CommittedWorldEventRef, WorldEvent


NOW = datetime(2026, 7, 16, 23, tzinfo=UTC)
LATER = NOW + timedelta(hours=2)


def _source() -> WorldEvent:
    return WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id="event:activity:complete",
        event_type="ActivityCompleted",
        world_id="world:appearance",
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


def _state_event(*, source: WorldEvent, revision: int = 1, at: datetime = NOW) -> WorldEvent:
    payload = {
        "state": {
            "appearance_state_id": "appearance:companion:primary",
            "subject_ref": "character:companion:primary",
            "entity_revision": revision,
            "source_event_ref": source.event_id,
            "source_event_payload_hash": source.payload_hash,
            "source_event_type": source.event_type,
            "valid_from": at.isoformat(),
            "valid_until": None,
            "visibility": "shareable",
            "visible_attributes": [{"aspect": "hair_arrangement", "description": "低马尾"}],
        }
    }
    return WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id=f"event:appearance:{revision}",
        event_type="AppearanceStateRecorded",
        world_id="world:appearance",
        logical_time=at,
        created_at=at,
        actor="worker:appearance",
        source="test",
        trace_id="trace:appearance",
        causation_id=source.event_id,
        correlation_id="correlation:appearance",
        idempotency_key=domain_idempotency_key(
            event_type="AppearanceStateRecorded", world_id="world:appearance", payload=payload
        )
        or "test:appearance",
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


def test_reducer_records_source_bound_sparse_state_and_replays_the_same_projection() -> None:
    source = _source()
    state = _state_with_source(source)
    event = _state_event(source=source)

    reduced = reduce_event(state, event)
    projection = make_projection(
        world_id="world:appearance",
        world_revision=2,
        deliberation_revision=0,
        ledger_sequence=2,
        state=reduced,
    )

    assert appearance_state_at(
        projection.appearance_states, subject_ref="character:companion:primary", at_logical_time=NOW
    ).visible_attributes == (
        VisibleAppearanceAttribute(aspect="hair_arrangement", description="低马尾"),
    )
    replayed = reduce_event(_state_with_source(source), event)
    assert (
        make_projection(
            world_id="world:appearance",
            world_revision=2,
            deliberation_revision=0,
            ledger_sequence=2,
            state=replayed,
        ).semantic_hash
        == projection.semantic_hash
    )


def test_reducer_rejects_an_unbound_or_stale_appearance_source() -> None:
    source = _source()
    event = _state_event(source=source)

    with pytest.raises(ValueError, match="appearance state source is not current"):
        reduce_event(ReducerState(logical_time=NOW), event)


def test_new_state_version_closes_the_previous_interval_for_historical_queries() -> None:
    source = _source()
    first = reduce_event(_state_with_source(source), _state_event(source=source))
    second = reduce_event(
        first.model_copy(update={"logical_time": LATER}),
        _state_event(source=source, revision=2, at=LATER),
    )

    assert (
        appearance_state_at(
            second.appearance_states, subject_ref="character:companion:primary", at_logical_time=NOW
        ).entity_revision
        == 1
    )
    assert (
        appearance_state_at(
            second.appearance_states,
            subject_ref="character:companion:primary",
            at_logical_time=LATER,
        ).entity_revision
        == 2
    )
    assert second.appearance_states[0].valid_until == LATER


def test_runtime_derives_source_hash_type_visibility_and_next_revision() -> None:
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
        appearance_states=(),
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
        world_id="world:appearance",
        project=lambda: projection,
        lookup_event_commit=lambda event_id: (
            (source, object()) if event_id == source.event_id else None
        ),
        commit_at_cursor=lambda events, expected_cursor, commit_id: commits.append(
            (events, expected_cursor, commit_id)
        ),
    )
    runtime = AppearanceStateRuntime(ledger=ledger)

    runtime.record(
        AppearanceStateRecordCommand(
            command_id="command:appearance:1",
            source_event_ref=source.event_id,
            subject_ref="character:companion:primary",
            visibility="shareable",
            visible_attributes=(
                VisibleAppearanceAttribute(aspect="outfit", description="深色运动外套"),
            ),
        ),
        logical_time=NOW,
        created_at=NOW,
        actor="worker:appearance",
        trace_id="trace:appearance",
        correlation_id="correlation:appearance",
    )

    event = commits[0][0][0]
    state = event.payload()["state"]
    assert event.event_type == "AppearanceStateRecorded"
    assert state["source_event_payload_hash"] == source.payload_hash
    assert state["source_event_type"] == "ActivityCompleted"
    assert state["entity_revision"] == 1


def test_runtime_refuses_visibility_broader_than_source() -> None:
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
        appearance_states=(),
        plans=(
            SimpleNamespace(
                authority_origin=SimpleNamespace(accepted_event_ref=source.event_id),
                privacy_class="personal",
            ),
        ),
        world_occurrences=(),
        experiences=(),
        facts=(),
    )
    commits: list[object] = []
    ledger = SimpleNamespace(
        world_id="world:appearance",
        project=lambda: projection,
        lookup_event_commit=lambda _event_id: (source, object()),
        commit_at_cursor=lambda *args, **kwargs: commits.append(args),
    )

    with pytest.raises(ValueError, match="appearance visibility exceeds its source"):
        AppearanceStateRuntime(ledger=ledger).record(
            AppearanceStateRecordCommand(
                command_id="command:appearance:private",
                source_event_ref=source.event_id,
                subject_ref="character:companion:primary",
                visibility="shareable",
                visible_attributes=(
                    VisibleAppearanceAttribute(aspect="grooming", description="整理过"),
                ),
            ),
            logical_time=NOW,
            created_at=NOW,
            actor="worker:appearance",
            trace_id="trace:appearance",
            correlation_id="correlation:appearance",
        )
    assert commits == []

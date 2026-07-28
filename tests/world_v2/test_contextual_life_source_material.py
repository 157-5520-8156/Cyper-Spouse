from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
from types import SimpleNamespace

import pytest

from companion_daemon.world_v2.contextual_life_source_material import (
    ContextualLifeSourceMaterialCompiler,
)
from companion_daemon.world_v2.schemas import (
    CommittedWorldEventRef,
    Observation,
    ProjectionCursor,
    WorldEvent,
)


NOW = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)
WORLD_ID = "world:contextual-source-material"


class _PinnedLedger:
    world_id = WORLD_ID

    def __init__(self, *, projection, events: tuple[WorldEvent, ...]) -> None:
        self._projection = projection
        self._events = {event.event_id: event for event in events}

    def project_at(self, cursor: ProjectionCursor):  # type: ignore[no-untyped-def]
        assert cursor == ProjectionCursor(
            world_revision=self._projection.world_revision,
            deliberation_revision=self._projection.deliberation_revision,
            ledger_sequence=self._projection.ledger_sequence,
        )
        return self._projection

    def lookup_event_commit(self, event_id: str):  # type: ignore[no-untyped-def]
        event = self._events.get(event_id)
        if event is None:
            return None
        ref = next(
            item
            for item in self._projection.committed_world_event_refs
            if item.event_id == event_id
        )
        return event, SimpleNamespace(
            world_revision=ref.world_revision,
            deliberation_revision=0,
            ledger_sequence=ref.world_revision,
            event_ids=(event_id,),
        )


def _observation_event(
    *,
    event_id: str,
    actor: str,
    text: str,
    logical_time: datetime,
) -> WorldEvent:
    content_hash = hashlib.sha256(text.encode()).hexdigest()
    observation = Observation(
        schema_version="world-v2.1",
        observation_id=event_id.removeprefix("event:"),
        world_id=WORLD_ID,
        logical_time=logical_time,
        created_at=logical_time,
        trace_id="trace:contextual-source",
        causation_id="cause:contextual-source",
        correlation_id="conversation:contextual-source",
        source="test:contextual-source",
        source_event_id=event_id,
        actor=actor,
        channel="qq:c2c",
        payload_ref="payload:" + event_id,
        payload_hash=content_hash,
        text=text,
        received_at=logical_time,
    )
    return WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id=event_id,
        world_id=WORLD_ID,
        event_type="ObservationRecorded",
        logical_time=logical_time,
        created_at=logical_time,
        actor=actor,
        source="test:contextual-source",
        trace_id=observation.trace_id,
        causation_id=observation.causation_id,
        correlation_id=observation.correlation_id,
        idempotency_key="observation:" + event_id,
        payload=observation.model_dump(mode="json"),
    )


def _ref(event: WorldEvent, revision: int) -> CommittedWorldEventRef:
    return CommittedWorldEventRef(
        event_id=event.event_id,
        event_type=event.event_type,
        world_revision=revision,
        payload_hash=event.payload_hash,
        logical_time=event.logical_time,
    )


def test_exact_old_observation_material_does_not_depend_on_recent_64_tail() -> None:
    old_time = NOW - timedelta(days=29, hours=23)
    text = "很久以前提过：深圳有个小展，我下午可能会去看看。"
    source = _observation_event(
        event_id="event:observation:old-selected-source",
        actor="user:user.1",
        text=text,
        logical_time=old_time,
    )
    source_ref = _ref(source, 1)
    # The selected source is intentionally older than the general dialogue
    # resolver's recent-64 window. Exact-source closure must still rehydrate it.
    newer_refs = tuple(
        CommittedWorldEventRef(
            event_id=f"event:observation:newer:{index}",
            event_type="ObservationRecorded",
            world_revision=index + 2,
            payload_hash=hashlib.sha256(f"newer:{index}".encode()).hexdigest(),
            logical_time=old_time + timedelta(minutes=index + 1),
        )
        for index in range(65)
    )
    projection = SimpleNamespace(
        world_revision=66,
        deliberation_revision=0,
        ledger_sequence=66,
        committed_world_event_refs=(source_ref, *newer_refs),
        facts=(),
        memory_candidates=(),
    )
    ledger = _PinnedLedger(projection=projection, events=(source,))

    material = ContextualLifeSourceMaterialCompiler(ledger=ledger).compile(
        cursor=ProjectionCursor(
            world_revision=66,
            deliberation_revision=0,
            ledger_sequence=66,
        ),
        source_event_ref=source.event_id,
        owner_actor_ref="agent:companion",
    )

    assert material is not None
    assert material.contents[0].text == text
    assert material.contents[0].content_ref == "payload:" + source.event_id
    assert material.authority_bindings[0].event_ref == source.event_id
    assert material.authority_bindings[0].payload_hash == source.payload_hash
    assert material.logical_time == old_time


def test_observation_from_non_user_actor_is_not_character_visible_source() -> None:
    source = _observation_event(
        event_id="event:observation:wrong-actor",
        actor="worker:unrelated",
        text="不属于角色与用户交互范围的内部文本。",
        logical_time=NOW,
    )
    projection = SimpleNamespace(
        world_revision=1,
        deliberation_revision=0,
        ledger_sequence=1,
        committed_world_event_refs=(_ref(source, 1),),
        facts=(),
        memory_candidates=(),
    )
    ledger = _PinnedLedger(projection=projection, events=(source,))

    assert (
        ContextualLifeSourceMaterialCompiler(ledger=ledger).compile(
            cursor=ProjectionCursor(
                world_revision=1,
                deliberation_revision=0,
                ledger_sequence=1,
            ),
            source_event_ref=source.event_id,
            owner_actor_ref="agent:companion",
        )
        is None
    )


@pytest.mark.parametrize(
    ("event_type", "projection_field", "candidate"),
    (
        (
            "FactCommittedV2",
            "facts",
            SimpleNamespace(
                origin=SimpleNamespace(accepted_event_ref="event:source:private"),
                values=SimpleNamespace(status="active", privacy_class="private"),
            ),
        ),
        (
            "MemoryCandidateAccepted",
            "memory_candidates",
            SimpleNamespace(
                origin=SimpleNamespace(accepted_event_ref="event:source:private"),
                values=SimpleNamespace(status="active", privacy_ceiling="private"),
            ),
        ),
    ),
)
def test_private_persistent_source_is_not_exposed(
    event_type: str,
    projection_field: str,
    candidate: object,
) -> None:
    source = WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id="event:source:private",
        world_id=WORLD_ID,
        event_type=event_type,
        logical_time=NOW,
        created_at=NOW,
        actor="worker:test",
        source="test:contextual-source",
        trace_id="trace:private-source",
        causation_id="cause:private-source",
        correlation_id="correlation:private-source",
        idempotency_key="private-source",
        payload={"private": True},
    )
    projection_values = {
        "world_revision": 1,
        "deliberation_revision": 0,
        "ledger_sequence": 1,
        "committed_world_event_refs": (_ref(source, 1),),
        "facts": (),
        "memory_candidates": (),
    }
    projection_values[projection_field] = (candidate,)
    projection = SimpleNamespace(**projection_values)
    ledger = _PinnedLedger(projection=projection, events=(source,))

    assert (
        ContextualLifeSourceMaterialCompiler(ledger=ledger).compile(
            cursor=ProjectionCursor(
                world_revision=1,
                deliberation_revision=0,
                ledger_sequence=1,
            ),
            source_event_ref=source.event_id,
            owner_actor_ref="agent:companion",
        )
        is None
    )

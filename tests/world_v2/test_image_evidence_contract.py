from __future__ import annotations

from datetime import UTC, datetime

import pytest

from companion_daemon.world_v2.image_evidence_contract import (
    ImageEvidenceDeclaredPayload,
    ImageEvidenceV1,
)
from companion_daemon.world_v2.reducers import ReducerState, reduce_event
from companion_daemon.world_v2.schemas import CommittedWorldEventRef, WorldEvent


NOW = datetime(2026, 7, 16, 21, tzinfo=UTC)
SOURCE = "event:activity:completed"
SOURCE_HASH = "a" * 64


def _payload(**changes: object) -> ImageEvidenceDeclaredPayload:
    values: dict[str, object] = {
        "source_event_ref": SOURCE,
        "source_event_payload_hash": SOURCE_HASH,
        "source_event_type": "ActivityCompleted",
        "source_privacy_ceiling": "shareable",
        "image_evidence": ImageEvidenceV1(
            visibility="shareable",
            activity={
                "evidence_visibility": "shareable",
                "id": "activity:walk",
                "kind": "walk",
                "description": "雨后散步",
                "phase": "completed",
            },
        ),
        "declared_at": NOW,
    }
    values.update(changes)
    return ImageEvidenceDeclaredPayload.model_validate(values)


def _event(payload: ImageEvidenceDeclaredPayload) -> WorldEvent:
    return WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id="event:image-evidence:walk",
        event_type="ImageEvidenceDeclared",
        world_id="world:image-evidence",
        logical_time=NOW,
        created_at=NOW,
        actor="worker:image-evidence",
        source="test:image-evidence",
        trace_id="trace:image-evidence",
        causation_id=SOURCE,
        correlation_id="correlation:image-evidence",
        idempotency_key="image-evidence:walk",
        payload=payload.model_dump(mode="json"),
    )


def test_declaration_is_source_bound_and_reducer_accepts_no_new_world_state() -> None:
    state = ReducerState(
        logical_time=NOW,
        committed_world_event_refs=(
            CommittedWorldEventRef(
                event_id=SOURCE,
                event_type="ActivityCompleted",
                world_revision=1,
                payload_hash=SOURCE_HASH,
                logical_time=NOW,
            ),
        ),
    )

    reduced = reduce_event(state, _event(_payload()))

    assert reduced.photo_candidates == state.photo_candidates
    assert reduced.committed_world_event_refs[-1].event_id == "event:image-evidence:walk"


def test_declaration_rejects_private_source_or_empty_visual_evidence() -> None:
    with pytest.raises(ValueError, match="source must be public or shareable"):
        _payload(source_privacy_ceiling="private")
    with pytest.raises(ValueError, match="concrete visual slice"):
        ImageEvidenceV1(visibility="public")


def test_reducer_rejects_a_declaration_with_mismatched_source_bytes() -> None:
    state = ReducerState(
        logical_time=NOW,
        committed_world_event_refs=(
            CommittedWorldEventRef(
                event_id=SOURCE,
                event_type="ActivityCompleted",
                world_revision=1,
                payload_hash=SOURCE_HASH,
                logical_time=NOW,
            ),
        ),
    )

    with pytest.raises(ValueError, match="source is not current"):
        reduce_event(state, _event(_payload(source_event_payload_hash="b" * 64)))

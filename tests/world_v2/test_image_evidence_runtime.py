from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from companion_daemon.world_v2.image_evidence_contract import ImageEvidenceV1
from companion_daemon.world_v2.image_evidence_runtime import (
    ImageEvidenceDeclarationCommand,
    ImageEvidenceDeclarationRuntime,
)
from companion_daemon.world_v2.schemas import CommittedWorldEventRef, WorldEvent


NOW = datetime(2026, 7, 16, 22, tzinfo=UTC)


def _source() -> WorldEvent:
    return WorldEvent.from_payload(
        schema_version="world-v2.1", event_id="event:activity:complete", event_type="ActivityCompleted",
        world_id="world:image-evidence-runtime", logical_time=NOW, created_at=NOW,
        actor="agent:companion", source="test", trace_id="trace:source", causation_id="cause:source",
        correlation_id="correlation:source", idempotency_key="source:activity", payload={},
    )


def _runtime(*, privacy: str = "shareable") -> tuple[ImageEvidenceDeclarationRuntime, list[tuple[object, object, str]]]:
    source = _source()
    projection = SimpleNamespace(
        world_revision=4, deliberation_revision=1, ledger_sequence=5, logical_time=NOW,
        committed_world_event_refs=(CommittedWorldEventRef(
            event_id=source.event_id, event_type=source.event_type, world_revision=4,
            payload_hash=source.payload_hash, logical_time=NOW,
        ),),
        plans=(SimpleNamespace(
            authority_origin=SimpleNamespace(accepted_event_ref=source.event_id), privacy_class=privacy,
        ),),
        world_occurrences=(), experiences=(), facts=(),
    )
    commits: list[tuple[object, object, str]] = []
    ledger = SimpleNamespace(
        world_id="world:image-evidence-runtime",
        project=lambda: projection,
        lookup_event_commit=lambda event_id: (source, object()) if event_id == source.event_id else None,
        commit_at_cursor=lambda events, expected_cursor, commit_id: commits.append((events, expected_cursor, commit_id)),
    )
    return ImageEvidenceDeclarationRuntime(ledger=ledger), commits


def _command() -> ImageEvidenceDeclarationCommand:
    return ImageEvidenceDeclarationCommand(
        command_id="command:image-evidence:walk",
        source_event_ref="event:activity:complete",
        image_evidence=ImageEvidenceV1(
            visibility="shareable",
            activity={
                "evidence_visibility": "shareable", "id": "activity:walk",
                "kind": "walk", "description": "雨后散步", "phase": "completed",
            },
        ),
    )


def test_runtime_derives_source_coordinates_and_never_accepts_them_from_the_command() -> None:
    runtime, commits = _runtime()

    runtime.declare(
        _command(), logical_time=NOW, created_at=NOW, actor="worker:image-evidence",
        trace_id="trace:image-evidence", correlation_id="correlation:image-evidence",
    )

    event = commits[0][0][0]
    payload = event.payload()
    assert event.event_type == "ImageEvidenceDeclared"
    assert event.causation_id == "event:activity:complete"
    assert payload["source_event_ref"] == "event:activity:complete"
    assert payload["source_event_type"] == "ActivityCompleted"
    assert payload["source_privacy_ceiling"] == "shareable"


def test_runtime_refuses_a_private_source_before_writing_a_declaration() -> None:
    runtime, commits = _runtime(privacy="private")

    with pytest.raises(ValueError, match="source must be public or shareable"):
        runtime.declare(
            _command(), logical_time=NOW, created_at=NOW, actor="worker:image-evidence",
            trace_id="trace:image-evidence", correlation_id="correlation:image-evidence",
        )

    assert commits == []

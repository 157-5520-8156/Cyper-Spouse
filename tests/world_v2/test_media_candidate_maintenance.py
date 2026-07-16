from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from companion_daemon.world_v2.media_candidate_maintenance import MediaCandidateMaintenanceRuntime
from companion_daemon.world_v2.media_v2 import MediaEvidenceSource, PhotoCandidate


NOW = datetime(2026, 7, 16, 20, tzinfo=UTC)


def test_maintenance_writes_only_deterministic_expiry_events_for_due_candidates() -> None:
    due = PhotoCandidate(
        candidate_id="candidate:due", source_event_refs=("event:source",), family="life_share",
        privacy_ceiling="shareable", opened_at=NOW - timedelta(hours=1), expires_at=NOW,
        ecology_category="activity_result", ecology_observed_at=NOW - timedelta(hours=1),
        source_events=(MediaEvidenceSource(event_ref="event:source", payload_hash="a" * 64),),
        opened_event_ref="event:candidate:due", opened_event_payload_hash="b" * 64,
    )
    future = due.model_copy(update={"candidate_id": "candidate:future", "expires_at": NOW + timedelta(hours=1)})
    commits: list[tuple[tuple[object, ...], object, str]] = []
    projection = SimpleNamespace(
        world_id="world:media-maintenance", world_revision=3, deliberation_revision=1,
        ledger_sequence=4, logical_time=NOW, photo_candidates=(future, due),
    )
    ledger = SimpleNamespace(
        world_id=projection.world_id,
        project=lambda: projection,
        commit_at_cursor=lambda events, expected_cursor, commit_id: commits.append((events, expected_cursor, commit_id)),
    )

    result = MediaCandidateMaintenanceRuntime(ledger=ledger).expire_once(
        logical_time=NOW, actor="worker:maintenance", trace_id="trace:maintenance",
        correlation_id="correlation:maintenance",
    )

    assert result.status == "expired"
    assert len(result.event_refs) == 1
    assert len(commits) == 1
    event = commits[0][0][0]
    assert event.event_type == "PhotoCandidateExpired"
    assert event.causation_id == due.opened_event_ref

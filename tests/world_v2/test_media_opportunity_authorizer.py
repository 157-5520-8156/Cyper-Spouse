from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from companion_daemon.world_v2.media_opportunity_authorizer import MediaOpportunityAuthorizer
from companion_daemon.world_v2.media_selection import MediaSelection
from companion_daemon.world_v2.media_v2 import MediaEvidenceSource, PhotoCandidate
from companion_daemon.world_v2.schemas import ProjectionCursor


NOW = datetime(2026, 7, 16, tzinfo=UTC)
CURSOR = ProjectionCursor(world_revision=3, deliberation_revision=1, ledger_sequence=4)
CANDIDATE = PhotoCandidate(
    candidate_id="photo-candidate:1",
    source_event_refs=("event:1",),
    family="life_share",
    privacy_ceiling="shareable",
    opened_at=NOW,
    expires_at=NOW + timedelta(hours=1),
    ecology_category="activity_process",
    ecology_observed_at=NOW,
    source_events=(MediaEvidenceSource(event_ref="event:1", payload_hash="a" * 64),),
)


class _Ledger:
    def project_at(self, cursor):  # type: ignore[no-untyped-def]
        assert cursor == CURSOR
        return SimpleNamespace(photo_candidates=(CANDIDATE,), logical_time=NOW)


class _Compiler:
    def __init__(self) -> None:
        self.calls = []

    def compile(self, request):  # type: ignore[no-untyped-def]
        self.calls.append(request)
        return SimpleNamespace(snapshot_ref="sidecar:1", snapshot_hash="sha256:" + "a" * 64)


def test_authorizer_only_compiles_an_existing_public_preview_candidate() -> None:
    compiler = _Compiler()
    opportunity, compiled = MediaOpportunityAuthorizer(ledger=_Ledger(), compiler=compiler, catalog_version="p1").authorize(
        cursor=CURSOR, selection=MediaSelection(candidate_id=CANDIDATE.candidate_id, family="life_share"),
        category="activity_process", observed_at=NOW, expires_at=NOW + timedelta(hours=1),
    )
    assert compiled.snapshot_ref == "sidecar:1"
    assert opportunity.delivery_mode == "preview"
    assert compiler.calls[0].candidate == CANDIDATE


def test_authorizer_rejects_unknown_candidate_before_compiling() -> None:
    compiler = _Compiler()
    with pytest.raises(ValueError, match="candidate_not_available"):
        MediaOpportunityAuthorizer(ledger=_Ledger(), compiler=compiler, catalog_version="p1").authorize(
            cursor=CURSOR, selection=MediaSelection(candidate_id="photo-candidate:missing", family="life_share"),
            category="activity_process", observed_at=NOW, expires_at=NOW + timedelta(hours=1),
        )
    assert compiler.calls == []


@pytest.mark.parametrize(
    ("candidate", "category", "expires_at", "error"),
    [
        (
            CANDIDATE.model_copy(update={"entity_revision": 2, "status": "selected"}),
            "activity_process",
            NOW + timedelta(hours=1),
            "candidate_not_available",
        ),
        (CANDIDATE, "settled_outcome", NOW + timedelta(hours=1), "selection_coordinates"),
        (CANDIDATE, "activity_process", NOW + timedelta(minutes=30), "selection_coordinates"),
    ],
)
def test_authorizer_refuses_a_stale_or_caller_rewritten_candidate_selection(
    candidate: PhotoCandidate, category: str, expires_at: datetime, error: str
) -> None:
    compiler = _Compiler()
    ledger = _Ledger()
    ledger.project_at = lambda cursor: SimpleNamespace(  # type: ignore[method-assign]
        photo_candidates=(candidate,), logical_time=NOW
    )
    with pytest.raises(ValueError, match=error):
        MediaOpportunityAuthorizer(ledger=ledger, compiler=compiler, catalog_version="p1").authorize(
            cursor=CURSOR,
            selection=MediaSelection(candidate_id=CANDIDATE.candidate_id, family="life_share"),
            category=category,
            observed_at=NOW,
            expires_at=expires_at,
        )
    assert compiler.calls == []

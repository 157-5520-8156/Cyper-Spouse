from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from companion_daemon.world_v2.media_opportunity_authorizer import MediaOpportunityAuthorizer
from companion_daemon.world_v2.media_selection import MediaSelection
from companion_daemon.world_v2.media_v2 import PhotoCandidate
from companion_daemon.world_v2.schemas import ProjectionCursor


NOW = datetime(2026, 7, 16, tzinfo=UTC)
CURSOR = ProjectionCursor(world_revision=3, deliberation_revision=1, ledger_sequence=4)
CANDIDATE = PhotoCandidate(candidate_id="photo-candidate:1", source_event_refs=("event:1",), family="life_share", privacy_ceiling="shareable")


class _Ledger:
    def project_at(self, cursor):  # type: ignore[no-untyped-def]
        assert cursor == CURSOR
        return SimpleNamespace(photo_candidates=(CANDIDATE,))


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

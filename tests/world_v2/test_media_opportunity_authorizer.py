from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from companion_daemon.world_v2.media_opportunity_authorizer import MediaOpportunityAuthorizer
from companion_daemon.world_v2.media_selection import MediaSelection
from companion_daemon.world_v2.media_v2 import (
    CharacterMediaCandidateContract,
    CharacterMediaSnapshotAuthorization,
    MediaEvidenceSource,
    PhotoCandidate,
    character_media_contract_digest,
)
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
    opened_event_ref="event:photo-candidate:1",
    opened_event_payload_hash="b" * 64,
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


def test_authorizer_keeps_character_candidate_lineage_separate_from_accepted_snapshot_facts() -> None:
    declaration = MediaEvidenceSource(event_ref="event:declaration", payload_hash="c" * 64)
    activity = MediaEvidenceSource(event_ref="event:activity", payload_hash="d" * 64)
    appearance = MediaEvidenceSource(event_ref="event:appearance", payload_hash="e" * 64)
    contract = CharacterMediaCandidateContract(
        subject_ref="agent:companion",
        kind="selfie",
        allowed_capture_modes=("character_front_camera",),
        allowed_character_visibility=("identifiable",),
        authority_digest=character_media_contract_digest(
            subject_ref="agent:companion", kind="selfie", source_events=(activity, declaration),
            allowed_capture_modes=("character_front_camera",), allowed_character_visibility=("identifiable",),
        ),
    )
    candidate = PhotoCandidate(
        candidate_id="candidate:character", source_event_refs=(activity.event_ref, declaration.event_ref),
        family="character_media", privacy_ceiling="public", opened_at=NOW,
        expires_at=NOW + timedelta(hours=1), ecology_category="character_media:selfie",
        ecology_observed_at=NOW, source_events=(activity, declaration),
        opened_event_ref="event:candidate:character", opened_event_payload_hash="f" * 64,
        character_media_contract=contract,
    )
    authorization = CharacterMediaSnapshotAuthorization(
        candidate_id=candidate.candidate_id, candidate_revision=candidate.entity_revision,
        subject_ref=contract.subject_ref, kind=contract.kind,
        allowed_capture_modes=contract.allowed_capture_modes,
        allowed_character_visibility=contract.allowed_character_visibility,
        authority_digest=contract.authority_digest, source_event_refs=candidate.source_event_refs,
    )
    compiler = _Compiler()
    compiler.compile = lambda request: SimpleNamespace(  # type: ignore[method-assign]
        snapshot_ref="sidecar:character", snapshot_hash="sha256:" + "a" * 64,
        snapshot=SimpleNamespace(
            source_events=(activity, appearance, declaration),
            character_media_authorization=authorization,
        ),
    )
    ledger = _Ledger()
    ledger.project_at = lambda cursor: SimpleNamespace(  # type: ignore[method-assign]
        photo_candidates=(candidate,), logical_time=NOW
    )

    opportunity, _compiled = MediaOpportunityAuthorizer(
        ledger=ledger, compiler=compiler, catalog_version="p2"
    ).authorize(
        cursor=CURSOR, selection=MediaSelection(candidate_id=candidate.candidate_id, family="character_media"),
        category=candidate.ecology_category, observed_at=NOW, expires_at=candidate.expires_at,
    )

    assert opportunity.source_event_refs == tuple(item.event_ref for item in (activity, appearance, declaration))
    assert opportunity.candidate_source_event_refs == candidate.source_event_refs
    assert opportunity.snapshot_source_events == (activity, appearance, declaration)


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

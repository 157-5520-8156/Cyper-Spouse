from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from companion_daemon.world_v2.media_v2 import (
    CharacterMediaCandidateContract,
    MediaEvidenceSource,
    PhotoCandidate,
    character_media_contract_digest,
)


NOW = datetime(2026, 7, 16, 0, 0, tzinfo=UTC)


def _candidate_kwargs(source: MediaEvidenceSource) -> dict[str, object]:
    return {
        "source_event_refs": (source.event_ref,),
        "privacy_ceiling": "public",
        "opened_at": NOW,
        "expires_at": NOW + timedelta(hours=1),
        "ecology_category": "character_media",
        "ecology_observed_at": NOW,
        "source_events": (source,),
    }


def _contract() -> CharacterMediaCandidateContract:
    source = MediaEvidenceSource(event_ref="event:declared:mirror", payload_hash="a" * 64)
    digest = character_media_contract_digest(
        subject_ref="agent:companion",
        kind="mirror",
        source_events=(source,),
        allowed_capture_modes=("mirror",),
        allowed_character_visibility=("identifiable",),
    )
    return CharacterMediaCandidateContract(
        subject_ref="agent:companion",
        kind="mirror",
        allowed_capture_modes=("mirror",),
        allowed_character_visibility=("identifiable",),
        authority_digest=digest,
    )


def test_character_media_candidate_requires_a_closed_frozen_contract() -> None:
    source = MediaEvidenceSource(event_ref="event:declared:mirror", payload_hash="a" * 64)
    with pytest.raises(ValueError, match="character media candidate requires"):
        PhotoCandidate(
            candidate_id="candidate:mirror", family="character_media", **_candidate_kwargs(source),
        )

    candidate = PhotoCandidate(
        candidate_id="candidate:mirror", family="character_media", **_candidate_kwargs(source),
        character_media_contract=_contract(),
    )

    assert candidate.character_media_contract is not None
    assert candidate.character_media_contract.allowed_capture_modes == ("mirror",)


def test_life_share_candidate_rejects_a_character_media_contract() -> None:
    source = MediaEvidenceSource(event_ref="event:declared:mirror", payload_hash="a" * 64)
    with pytest.raises(ValueError, match="life-share candidate may not"):
        PhotoCandidate(
            candidate_id="candidate:life", family="life_share", **_candidate_kwargs(source),
            character_media_contract=_contract(),
        )


def test_contract_digest_binds_kind_subject_sources_and_allowed_visual_space() -> None:
    source = MediaEvidenceSource(event_ref="event:declared:mirror", payload_hash="a" * 64)
    contract = _contract()
    assert contract.authority_digest == character_media_contract_digest(
        subject_ref=contract.subject_ref,
        kind=contract.kind,
        source_events=(source,),
        allowed_capture_modes=contract.allowed_capture_modes,
        allowed_character_visibility=contract.allowed_character_visibility,
    )
    assert contract.authority_digest != character_media_contract_digest(
        subject_ref=contract.subject_ref,
        kind="selfie",
        source_events=(source,),
        allowed_capture_modes=("character_front_camera",),
        allowed_character_visibility=contract.allowed_character_visibility,
    )

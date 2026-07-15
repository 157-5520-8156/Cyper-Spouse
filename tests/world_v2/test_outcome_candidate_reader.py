from __future__ import annotations

from datetime import UTC, datetime, timedelta

from companion_daemon.world_v2.life_content_store import (
    InMemoryImmutableLifeContentStore,
    StoredLifeContent,
    life_content_payload_hash,
)
from companion_daemon.world_v2.outcome_candidate_reader import OutcomeCandidateReader
from companion_daemon.world_v2.schemas import DueWindow, OutcomeCandidateDescriptor, WorldOccurrenceProjection


NOW = datetime(2026, 7, 15, tzinfo=UTC)


def _occurrence(candidate: OutcomeCandidateDescriptor | None) -> WorldOccurrenceProjection:
    refs = (candidate.candidate_result_ref,) if candidate else ("candidate:unknown",)
    return WorldOccurrenceProjection(
        occurrence_id="occurrence:tea",
        entity_revision=2,
        trigger_ref="trigger:tea",
        participant_refs=("actor:companion",),
        location_ref="room:kitchen",
        time_window=DueWindow(opens_at=NOW, closes_at=NOW + timedelta(minutes=10)),
        candidate_outcome_refs=refs,
        candidate_outcomes=(candidate,) if candidate else (),
        visibility="private",
        status="active",
        activated_at=NOW,
    )


def test_candidate_reader_exposes_only_hash_bound_frozen_candidate_text() -> None:
    store = InMemoryImmutableLifeContentStore()
    text = "热水刚好，茶叶舒展开了。"
    content_hash = life_content_payload_hash(text)
    candidate = OutcomeCandidateDescriptor(
        candidate_result_ref="candidate:tea-ready",
        result_id="result:tea-ready",
        result_payload_ref="payload:tea-ready",
        result_payload_hash="result-hash:tea-ready",
        privacy_class="private",
        content_ref="content:candidate:tea-ready",
        content_payload_hash=content_hash,
    )
    store.put_if_absent(
        StoredLifeContent(
            content_ref=candidate.content_ref,
            content_kind="outcome_candidate",
            content_payload_hash=content_hash,
            text=text,
        )
    )

    result = OutcomeCandidateReader(store=store).read(
        occurrence=_occurrence(candidate), viewer_privacy_ceiling="private"
    )

    assert result.suppressions == ()
    assert result.candidates[0].candidate_result_ref == candidate.candidate_result_ref
    assert result.candidates[0].text == text


def test_candidate_reader_does_not_promote_ref_names_when_content_is_unavailable() -> None:
    result = OutcomeCandidateReader(store=InMemoryImmutableLifeContentStore()).read(
        occurrence=_occurrence(None), viewer_privacy_ceiling="private"
    )

    assert result.candidates == ()
    assert result.suppressions[0].reason == "descriptor_missing"

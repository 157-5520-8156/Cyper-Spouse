from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from companion_daemon.world_v2.event_identity import domain_idempotency_key
from companion_daemon.world_v2.media_selection import MediaSelection, media_selection_hash
from companion_daemon.world_v2.media_selection_proposal import (
    MEDIA_SELECTION_PROPOSAL_POLICY_DIGEST,
    MediaSelectionProposalCompiler,
    MediaSelectionProposalRecordedPayload,
    media_candidate_authority_hash,
    media_selection_proposed_change_hash,
)
from companion_daemon.world_v2.media_v2 import (
    MediaEvidenceSource,
    PhotoCandidate,
    PhotoCandidateExpiredPayload,
    PhotoCandidateUnrenderablePayload,
)
from companion_daemon.world_v2.reducers import ReducerState, reduce_event
from companion_daemon.world_v2.schemas import CommittedWorldEventRef, LedgerProjection, WorldEvent


NOW = datetime(2026, 7, 16, 18, tzinfo=UTC)
WORLD = "world:media-selection-proposal"
SOURCE = "event:source:media-selection"


def _event(event_id: str, event_type: str, payload: dict[str, object]) -> WorldEvent:
    identity = domain_idempotency_key(event_type=event_type, world_id=WORLD, payload=payload)
    return WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id=event_id,
        world_id=WORLD,
        event_type=event_type,
        logical_time=NOW,
        created_at=NOW,
        actor="worker:media-selection",
        source="test:media-selection",
        trace_id="trace:media-selection",
        causation_id="cause:" + event_id,
        correlation_id="correlation:media-selection",
        idempotency_key=identity or "identity:" + event_id,
        payload=payload,
    )


def _opened_state() -> ReducerState:
    state = ReducerState(
        logical_time=NOW,
        committed_world_event_refs=(
            CommittedWorldEventRef(
                event_id=SOURCE,
                event_type="ActivityCompleted",
                world_revision=1,
                payload_hash="a" * 64,
                logical_time=NOW,
            ),
        ),
    )
    candidate = PhotoCandidate(
        candidate_id="candidate:selection",
        source_event_refs=(SOURCE,),
        family="life_share",
        privacy_ceiling="shareable",
        opened_at=NOW,
        expires_at=NOW + timedelta(hours=1),
        ecology_category="activity_result",
        ecology_observed_at=NOW,
        source_events=(MediaEvidenceSource(event_ref=SOURCE, payload_hash="a" * 64),),
    )
    return reduce_event(
        state,
        _event("event:candidate:selection", "PhotoCandidateOpened", {"candidate": candidate.model_dump(mode="json")}),
    )


def _proposal(candidate: PhotoCandidate, *, revision: int = 2) -> MediaSelectionProposalRecordedPayload:
    selection = MediaSelection(candidate_id=candidate.candidate_id, family="life_share")
    selection_hash = media_selection_hash(selection)
    change_id = "change:media-selection:1"
    values = {
        "proposal_id": "proposal:media-selection:1",
        "change_id": change_id,
        "evaluated_world_revision": revision,
        "evaluated_deliberation_revision": 0,
        "evaluated_ledger_sequence": revision,
        "candidate_id": candidate.candidate_id,
        "expected_candidate_revision": candidate.entity_revision,
        "candidate_authority_hash": media_candidate_authority_hash(candidate),
        "selection": selection,
        "selection_hash": selection_hash,
        "catalog_version": "media-selection-p1.1",
        "policy_digest": MEDIA_SELECTION_PROPOSAL_POLICY_DIGEST,
        "model": "test-flash",
        "raw_output_hash": "sha256:" + "b" * 64,
        "normalized_output_hash": "sha256:" + "c" * 64,
    }
    values["proposed_change_hash"] = media_selection_proposed_change_hash(
        change_id=change_id,
        candidate_id=candidate.candidate_id,
        expected_candidate_revision=candidate.entity_revision,
        candidate_authority_hash=values["candidate_authority_hash"],  # type: ignore[arg-type]
        evaluated_world_revision=revision,
        evaluated_deliberation_revision=0,
        evaluated_ledger_sequence=revision,
        selection_hash=selection_hash,
        catalog_version="media-selection-p1.1",
    )
    return MediaSelectionProposalRecordedPayload.model_validate(values)


def test_p1_selection_proposal_pins_one_available_candidate_and_its_event_bytes() -> None:
    state = _opened_state()
    candidate = state.photo_candidates[0]
    proposal = _proposal(candidate)

    reduced = reduce_event(
        state,
        _event(
            "event:selection-proposal:1",
            "MediaSelectionProposalRecorded",
            proposal.model_dump(mode="json"),
        ),
    )

    assert reduced.proposal_ids == (proposal.proposal_id,)
    assert reduced.proposal_revisions[0].proposal_event_ref == "event:selection-proposal:1"
    assert reduced.proposal_revisions[0].selection_hash == proposal.selection_hash


def test_p1_selection_proposal_rejects_a_candidate_revision_replayed_after_selection() -> None:
    state = _opened_state()
    candidate = state.photo_candidates[0]
    proposal = _proposal(candidate)
    stale_revision = candidate.entity_revision + 1
    stale = proposal.model_copy(
        update={
            "expected_candidate_revision": stale_revision,
            "proposed_change_hash": media_selection_proposed_change_hash(
                change_id=proposal.change_id,
                candidate_id=proposal.candidate_id,
                expected_candidate_revision=stale_revision,
                candidate_authority_hash=proposal.candidate_authority_hash,
                evaluated_world_revision=proposal.evaluated_world_revision,
                evaluated_deliberation_revision=proposal.evaluated_deliberation_revision,
                evaluated_ledger_sequence=proposal.evaluated_ledger_sequence,
                selection_hash=proposal.selection_hash,
                catalog_version=proposal.catalog_version,
            ),
        }
    )

    with pytest.raises(ValueError, match="candidate is not current"):
        reduce_event(
            state,
            _event("event:selection-proposal:stale", "MediaSelectionProposalRecorded", stale.model_dump(mode="json")),
        )


def test_compiler_derives_candidate_authority_and_cursor_coordinates_from_projection() -> None:
    candidate = _opened_state().photo_candidates[0]
    projection = LedgerProjection.model_construct(
        world_id=WORLD,
        world_revision=2,
        deliberation_revision=0,
        ledger_sequence=2,
        logical_time=NOW,
        photo_candidates=(candidate,),
    )

    proposal = MediaSelectionProposalCompiler(catalog_version="media-selection-p1.1").compile(
        projection=projection,
        selection=MediaSelection(candidate_id=candidate.candidate_id, family="life_share"),
        model="test-flash",
        raw_output_hash="sha256:" + "b" * 64,
        normalized_output_hash="sha256:" + "c" * 64,
    )

    assert proposal.candidate_id == candidate.candidate_id
    assert proposal.expected_candidate_revision == candidate.entity_revision
    assert proposal.evaluated_ledger_sequence == 2


def test_unrenderable_snapshot_closes_the_same_available_candidate_without_substitution() -> None:
    state = _opened_state()
    candidate = state.photo_candidates[0]
    proposal = _proposal(candidate)
    state = reduce_event(
        state,
        _event(
            "event:selection-proposal:unrenderable",
            "MediaSelectionProposalRecorded",
            proposal.model_dump(mode="json"),
        ),
    )

    reduced = reduce_event(
        state,
        _event(
            "event:candidate-unrenderable:1",
            "PhotoCandidateUnrenderable",
            PhotoCandidateUnrenderablePayload(
                candidate_id=candidate.candidate_id,
                expected_entity_revision=candidate.entity_revision,
                reason_code="no_visual_evidence",
            ).model_dump(mode="json"),
        ).model_copy(update={"causation_id": "event:selection-proposal:unrenderable"}),
    )

    assert reduced.photo_candidates == (
        candidate.model_copy(update={"entity_revision": 2, "status": "unrenderable"}),
    )


def test_expiry_closes_an_unaccepted_candidate_after_its_fixed_window() -> None:
    state = _opened_state()
    candidate = state.photo_candidates[0]
    assert candidate.expires_at is not None
    expired_at = candidate.expires_at
    event = _event(
        "event:candidate-expired:1",
        "PhotoCandidateExpired",
        PhotoCandidateExpiredPayload(
            candidate_id=candidate.candidate_id,
            expected_entity_revision=candidate.entity_revision,
        ).model_dump(mode="json"),
    ).model_copy(update={"logical_time": expired_at})

    reduced = reduce_event(state.model_copy(update={"logical_time": expired_at}), event)

    assert reduced.photo_candidates == (
        candidate.model_copy(update={"entity_revision": 2, "status": "expired"}),
    )

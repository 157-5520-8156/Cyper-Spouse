from __future__ import annotations

import pytest
from types import SimpleNamespace
from datetime import UTC, datetime
import json

from companion_daemon.world_v2.media_selection_draft import MediaSelectionDraftAdapter
from companion_daemon.world_v2.media_selection_worker import MediaSelectionWorker
from companion_daemon.world_v2.media_v2 import (
    CharacterMediaCandidateContract,
    MediaEvidenceSource,
    PhotoCandidate,
    character_media_contract_digest,
)

NOW = datetime(2026, 7, 16, tzinfo=UTC)

class _Model:
    model = "test"
    def __init__(self) -> None:
        self.calls = 0
        self.messages = []

    async def complete(self, messages, *, temperature=0.2):  # type: ignore[no-untyped-def]
        self.calls += 1
        self.messages.append(messages)
        return '{"decision":"no_op"}'

class _Ledger:
    def project(self):
        return SimpleNamespace(logical_time=NOW, photo_candidates=())

class _Recorder:
    pass

@pytest.mark.asyncio
async def test_worker_does_not_call_the_model_or_write_when_no_candidate_exists() -> None:
    model = _Model()
    worker = MediaSelectionWorker(
        ledger=_Ledger(), draft_adapter=MediaSelectionDraftAdapter(model=model),
        proposal_recorder=_Recorder(), catalog_version="test.1",
    )
    result = await worker.select_once(logical_time=NOW, actor="worker", trace_id="trace", correlation_id="correlation")
    assert result.status == "no_op"
    assert result.reason_code == "media_selection.no_available_candidates"
    assert model.calls == 0


@pytest.mark.asyncio
async def test_worker_does_not_repeat_a_model_call_for_an_unaccepted_candidate_revision() -> None:
    candidate = PhotoCandidate(
        candidate_id="candidate:pending", source_event_refs=("event:source",), family="life_share",
        privacy_ceiling="shareable", opened_at=NOW, expires_at=NOW.replace(hour=1),
        ecology_category="activity_result", ecology_observed_at=NOW,
        source_events=(MediaEvidenceSource(event_ref="event:source", payload_hash="a" * 64),),
    )
    model = _Model()
    ledger = SimpleNamespace(
        project=lambda: SimpleNamespace(
            logical_time=NOW,
            photo_candidates=(candidate,),
            proposal_revisions=(SimpleNamespace(
                candidate_id=candidate.candidate_id,
                expected_candidate_revision=candidate.entity_revision,
            ),),
        ),
    )
    worker = MediaSelectionWorker(
        ledger=ledger, draft_adapter=MediaSelectionDraftAdapter(model=model),
        proposal_recorder=_Recorder(), catalog_version="test.1",
    )

    result = await worker.select_once(logical_time=NOW, actor="worker", trace_id="trace", correlation_id="correlation")

    assert result.status == "no_op"
    assert result.reason_code == "media_selection.pending_proposal"
    assert model.calls == 0


@pytest.mark.asyncio
async def test_worker_leaves_a_character_candidate_unselected_until_its_p2_acceptance_lane_exists() -> None:
    source = MediaEvidenceSource(event_ref="event:declaration", payload_hash="a" * 64)
    contract = CharacterMediaCandidateContract(
        subject_ref="agent:companion", kind="mirror", allowed_capture_modes=("mirror",),
        allowed_character_visibility=("identifiable",),
        authority_digest=character_media_contract_digest(
            subject_ref="agent:companion", kind="mirror", source_events=(source,),
            allowed_capture_modes=("mirror",), allowed_character_visibility=("identifiable",),
        ),
    )
    candidate = PhotoCandidate(
        candidate_id="candidate:character", source_event_refs=(source.event_ref,), family="character_media",
        privacy_ceiling="public", opened_at=NOW, expires_at=NOW.replace(hour=1),
        ecology_category="character_media:mirror", ecology_observed_at=NOW, source_events=(source,),
        character_media_contract=contract,
    )
    model = _Model()
    ledger = SimpleNamespace(
        project=lambda: SimpleNamespace(
            logical_time=NOW, photo_candidates=(candidate,), proposal_revisions=(),
        ),
    )
    worker = MediaSelectionWorker(
        ledger=ledger, draft_adapter=MediaSelectionDraftAdapter(model=model),
        proposal_recorder=_Recorder(), catalog_version="test.1",
    )

    result = await worker.select_once(logical_time=NOW, actor="worker", trace_id="trace", correlation_id="correlation")

    assert result.reason_code == "media_selection.character_media_not_yet_authorizable"
    assert model.calls == 0


@pytest.mark.asyncio
async def test_worker_gives_the_model_deterministic_non_authoritative_candidate_advisory() -> None:
    candidate = PhotoCandidate(
        candidate_id="candidate:advisory", source_event_refs=("event:declaration", "event:source"),
        family="life_share", privacy_ceiling="shareable", opened_at=NOW, expires_at=NOW.replace(hour=1),
        ecology_category="activity_result", ecology_observed_at=NOW,
        source_events=(
            MediaEvidenceSource(event_ref="event:declaration", payload_hash="b" * 64),
            MediaEvidenceSource(event_ref="event:source", payload_hash="a" * 64),
        ),
    )
    model = _Model()
    ledger = SimpleNamespace(
        project=lambda: SimpleNamespace(
            logical_time=NOW, world_revision=3, deliberation_revision=0, ledger_sequence=3,
            photo_candidates=(candidate,), proposal_revisions=(), media_opportunities=(), budget_accounts=(),
        ),
    )
    worker = MediaSelectionWorker(
        ledger=ledger, draft_adapter=MediaSelectionDraftAdapter(model=model),
        proposal_recorder=_Recorder(), catalog_version="test.1",
    )

    result = await worker.select_once(logical_time=NOW, actor="worker", trace_id="trace", correlation_id="correlation")

    assert result.reason_code == "media_selection.model_declined"
    choice = json.loads(model.messages[0][1]["content"])["candidates"][0]
    assert choice["advisory"] == {
        "category": "activity_result",
        "freshness_bp": 10_000,
        "novelty_bp": 10_000,
        "visual_evidence_bp": 10_000,
        "budget_state": "unconfigured",
        "advisory_score_bp": 8_750,
        "missing_signals": ["emotional_meaning", "existing_media", "user_preference"],
    }
    assert "candidate:advisory" not in model.messages[0][1]["content"]

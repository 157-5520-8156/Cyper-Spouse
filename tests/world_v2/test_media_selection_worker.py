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


class _InvalidModel(_Model):
    async def complete(self, messages, *, temperature=0.2):  # type: ignore[no-untyped-def]
        self.calls += 1
        self.messages.append(messages)
        return "not-json"


class _UnavailableModel(_Model):
    async def complete(self, messages, *, temperature=0.2):  # type: ignore[no-untyped-def]
        self.calls += 1
        self.messages.append(messages)
        raise ConnectionError("provider offline")

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
async def test_worker_recovers_the_current_head_proposal_without_repeating_the_model_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = PhotoCandidate(
        candidate_id="candidate:pending", source_event_refs=("event:source",), family="life_share",
        privacy_ceiling="shareable", opened_at=NOW, expires_at=NOW.replace(hour=1),
        ecology_category="activity_result", ecology_observed_at=NOW,
        source_events=(MediaEvidenceSource(event_ref="event:source", payload_hash="a" * 64),),
    )
    model = _Model()
    proposal = SimpleNamespace(
        proposal_id="proposal:pending",
        candidate_id=candidate.candidate_id,
        expected_candidate_revision=candidate.entity_revision,
    )
    monkeypatch.setattr(
        "companion_daemon.world_v2.media_selection_worker.MediaSelectionProposalRecordedPayload.model_validate_json",
        lambda _payload: proposal,
    )
    projection = SimpleNamespace(
            logical_time=NOW,
            world_revision=3,
            deliberation_revision=5,
            ledger_sequence=8,
            photo_candidates=(candidate,),
            proposal_revisions=(SimpleNamespace(
                proposal_id=proposal.proposal_id,
                candidate_id=candidate.candidate_id,
                expected_candidate_revision=candidate.entity_revision,
                proposal_event_ref="event:proposal:pending",
                proposal_event_payload_hash="b" * 64,
            ),),
    )
    ledger = SimpleNamespace(
        project=lambda: projection,
        lookup_event_commit=lambda _ref: (
            SimpleNamespace(
                event_id="event:proposal:pending",
                event_type="MediaSelectionProposalRecorded",
                payload_hash="b" * 64,
                payload_json="{}",
            ),
            SimpleNamespace(world_revision=3, deliberation_revision=5, ledger_sequence=8),
        ),
    )
    worker = MediaSelectionWorker(
        ledger=ledger, draft_adapter=MediaSelectionDraftAdapter(model=model),
        proposal_recorder=_Recorder(), catalog_version="test.1",
    )

    result = await worker.select_once(logical_time=NOW, actor="worker", trace_id="trace", correlation_id="correlation")

    assert result.status == "proposed"
    assert result.proposal_event_ref == "event:proposal:pending"
    assert result.reason_code == "media_selection.recovered_pending_proposal"
    assert model.calls == 0


@pytest.mark.asyncio
async def test_worker_re_deliberates_when_a_valid_pending_proposal_is_no_longer_at_head(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = PhotoCandidate(
        candidate_id="candidate:stale", source_event_refs=("event:source",), family="life_share",
        privacy_ceiling="shareable", opened_at=NOW, expires_at=NOW.replace(hour=1),
        ecology_category="activity_result", ecology_observed_at=NOW,
        source_events=(MediaEvidenceSource(event_ref="event:source", payload_hash="a" * 64),),
    )
    model = _Model()
    proposal = SimpleNamespace(
        proposal_id="proposal:stale",
        candidate_id=candidate.candidate_id,
        expected_candidate_revision=candidate.entity_revision,
    )
    monkeypatch.setattr(
        "companion_daemon.world_v2.media_selection_worker.MediaSelectionProposalRecordedPayload.model_validate_json",
        lambda _payload: proposal,
    )
    projection = SimpleNamespace(
            logical_time=NOW,
            world_revision=4,
            deliberation_revision=5,
            ledger_sequence=9,
            world_id="world:test",
            photo_candidates=(candidate,),
            proposal_revisions=(SimpleNamespace(
                proposal_id=proposal.proposal_id,
                candidate_id=candidate.candidate_id,
                expected_candidate_revision=candidate.entity_revision,
                proposal_event_ref="event:proposal:stale",
                proposal_event_payload_hash="b" * 64,
            ),),
    )
    def lookup_stale(ref):  # type: ignore[no-untyped-def]
        if ref != "event:proposal:stale":
            return None
        return (
            SimpleNamespace(
                event_id="event:proposal:stale",
                event_type="MediaSelectionProposalRecorded",
                payload_hash="b" * 64,
                payload_json="{}",
            ),
            SimpleNamespace(world_revision=3, deliberation_revision=5, ledger_sequence=8),
        )

    ledger = SimpleNamespace(
        world_id="world:test",
        project=lambda: projection,
        lookup_event_commit=lookup_stale,
    )
    worker = MediaSelectionWorker(
        ledger=ledger, draft_adapter=MediaSelectionDraftAdapter(model=model),
        proposal_recorder=_Recorder(), catalog_version="test.1",
    )
    worker._random = SimpleNamespace(  # type: ignore[assignment]
        draw=lambda **_kwargs: SimpleNamespace(
            draw_id="draw:test-stale",
            selected_candidate_ref=candidate.candidate_id,
            sampler_version="test.1",
        )
    )

    result = await worker.select_once(
        logical_time=NOW, actor="worker", trace_id="trace", correlation_id="correlation"
    )

    assert result.status == "no_op"
    assert result.reason_code == "media_selection.model_declined"
    assert model.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model_type", "first_reason", "second_reason", "expected_status"),
    (
        (_Model, "media_selection.model_declined", "media_selection.recovered_decline", "no_op"),
        (
            _InvalidModel,
            "media_selection.model_not_json",
            "media_selection.model_not_json",
            "blocked",
        ),
    ),
)
async def test_worker_persists_and_recovers_terminal_attempt_at_same_logical_time(
    model_type, first_reason: str, second_reason: str, expected_status: str,
) -> None:  # type: ignore[no-untyped-def]
    candidate = PhotoCandidate(
        candidate_id="candidate:decline", source_event_refs=("event:source",),
        family="life_share", privacy_ceiling="shareable", opened_at=NOW,
        expires_at=NOW.replace(hour=1), ecology_category="activity_result",
        ecology_observed_at=NOW,
        source_events=(MediaEvidenceSource(event_ref="event:source", payload_hash="a" * 64),),
    )
    projection = SimpleNamespace(
        logical_time=NOW, world_id="world:test", world_revision=3,
        deliberation_revision=0, ledger_sequence=3,
        photo_candidates=(candidate,), proposal_revisions=(),
    )
    events = {}

    def lookup(event_id):  # type: ignore[no-untyped-def]
        return events.get(event_id)

    def commit_at_cursor(new_events, *, expected_cursor, commit_id):  # type: ignore[no-untyped-def]
        del commit_id
        assert (
            expected_cursor.world_revision,
            expected_cursor.deliberation_revision,
            expected_cursor.ledger_sequence,
        ) == (
            projection.world_revision,
            projection.deliberation_revision,
            projection.ledger_sequence,
        )
        event = new_events[0]
        if event.event_type == "RandomDrawRecorded":
            projection.world_revision += 1
        else:
            projection.deliberation_revision += 1
        projection.ledger_sequence += 1
        commit = SimpleNamespace(
            world_revision=projection.world_revision,
            deliberation_revision=projection.deliberation_revision,
            ledger_sequence=projection.ledger_sequence,
        )
        events[event.event_id] = (event, commit)
        return commit

    ledger = SimpleNamespace(
        world_id="world:test", project=lambda: projection,
        lookup_event_commit=lookup, commit_at_cursor=commit_at_cursor,
    )
    model = model_type()
    worker = MediaSelectionWorker(
        ledger=ledger, draft_adapter=MediaSelectionDraftAdapter(model=model),
        proposal_recorder=_Recorder(), catalog_version="test.1",
    )

    first = await worker.select_once(
        logical_time=NOW, actor="worker", trace_id="trace", correlation_id="correlation"
    )
    second = await worker.select_once(
        logical_time=NOW, actor="worker", trace_id="trace", correlation_id="correlation"
    )

    assert first.status == expected_status
    assert second.status == expected_status
    assert first.reason_code == first_reason
    assert second.reason_code == second_reason
    assert model.calls == 1
    assert any(
        event.event_type == "MediaSelectionAttemptRecorded"
        for event, _commit in events.values()
    )


@pytest.mark.asyncio
async def test_worker_structures_invalid_model_output_without_writing_a_proposal() -> None:
    candidate = PhotoCandidate(
        candidate_id="candidate:invalid-model", source_event_refs=("event:source",),
        family="life_share", privacy_ceiling="shareable", opened_at=NOW,
        expires_at=NOW.replace(hour=1), ecology_category="activity_result",
        ecology_observed_at=NOW,
        source_events=(MediaEvidenceSource(event_ref="event:source", payload_hash="a" * 64),),
    )
    model = _InvalidModel()
    ledger = SimpleNamespace(
        project=lambda: SimpleNamespace(
            logical_time=NOW, world_revision=3, deliberation_revision=0,
            ledger_sequence=3, photo_candidates=(candidate,), proposal_revisions=(),
        ),
    )
    worker = MediaSelectionWorker(
        ledger=ledger, draft_adapter=MediaSelectionDraftAdapter(model=model),
        proposal_recorder=_Recorder(), catalog_version="test.1",
    )

    result = await worker.select_once(
        logical_time=NOW, actor="worker", trace_id="trace", correlation_id="correlation"
    )

    assert result.status == "blocked"
    assert result.reason_code == "media_selection.model_not_json"
    assert model.calls == 1


@pytest.mark.asyncio
async def test_worker_structures_retryable_model_outage_for_scheduler_isolation() -> None:
    candidate = PhotoCandidate(
        candidate_id="candidate:model-outage", source_event_refs=("event:source",),
        family="life_share", privacy_ceiling="shareable", opened_at=NOW,
        expires_at=NOW.replace(hour=1), ecology_category="activity_result",
        ecology_observed_at=NOW,
        source_events=(MediaEvidenceSource(event_ref="event:source", payload_hash="a" * 64),),
    )
    model = _UnavailableModel()
    worker = MediaSelectionWorker(
        ledger=SimpleNamespace(project=lambda: SimpleNamespace(
            logical_time=NOW, world_revision=3, deliberation_revision=0,
            ledger_sequence=3, photo_candidates=(candidate,), proposal_revisions=(),
        )),
        draft_adapter=MediaSelectionDraftAdapter(model=model),
        proposal_recorder=_Recorder(), catalog_version="test.1",
    )

    result = await worker.select_once(
        logical_time=NOW, actor="worker", trace_id="trace", correlation_id="correlation"
    )

    assert result.status == "blocked"
    assert result.reason_code == "media_selection.model_unavailable"
    assert model.calls == 1


@pytest.mark.asyncio
async def test_worker_blocks_instead_of_deliberating_around_missing_pending_authority() -> None:
    candidate = PhotoCandidate(
        candidate_id="candidate:missing-authority", source_event_refs=("event:source",),
        family="life_share", privacy_ceiling="shareable", opened_at=NOW,
        expires_at=NOW.replace(hour=1), ecology_category="activity_result",
        ecology_observed_at=NOW,
        source_events=(MediaEvidenceSource(event_ref="event:source", payload_hash="a" * 64),),
    )
    model = _Model()
    projection = SimpleNamespace(
        logical_time=NOW, world_revision=4, deliberation_revision=5, ledger_sequence=9,
        world_id="world:test", photo_candidates=(candidate,),
        proposal_revisions=(SimpleNamespace(
            proposal_id="proposal:missing", candidate_id=candidate.candidate_id,
            expected_candidate_revision=candidate.entity_revision,
            proposal_event_ref="event:proposal:missing",
            proposal_event_payload_hash="b" * 64,
        ),),
    )
    ledger = SimpleNamespace(project=lambda: projection, lookup_event_commit=lambda _ref: None)
    worker = MediaSelectionWorker(
        ledger=ledger, draft_adapter=MediaSelectionDraftAdapter(model=model),
        proposal_recorder=_Recorder(), catalog_version="test.1",
    )

    result = await worker.select_once(
        logical_time=NOW, actor="worker", trace_id="trace", correlation_id="correlation"
    )

    assert result.status == "blocked"
    assert result.reason_code == "media_selection.pending_proposal_invalid"
    assert model.calls == 0


@pytest.mark.asyncio
async def test_worker_asks_the_model_about_an_ordinary_character_candidate() -> None:
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
            logical_time=NOW, world_revision=3, deliberation_revision=0, ledger_sequence=3,
            photo_candidates=(candidate,), proposal_revisions=(),
        ),
    )
    worker = MediaSelectionWorker(
        ledger=ledger, draft_adapter=MediaSelectionDraftAdapter(model=model),
        proposal_recorder=_Recorder(), catalog_version="test.1",
    )

    result = await worker.select_once(logical_time=NOW, actor="worker", trace_id="trace", correlation_id="correlation")

    assert result.reason_code == "media_selection.model_declined"
    assert model.calls == 1


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

from __future__ import annotations

from datetime import UTC, datetime
import json
from types import SimpleNamespace

import pytest

from companion_daemon.world_v2.accepted_ledger_batch import AcceptedLedgerBatchIssuer
from companion_daemon.world_v2.errors import ConcurrencyConflict
from companion_daemon.world_v2.event_identity import domain_idempotency_key
from companion_daemon.world_v2.ledger import WorldLedger
from companion_daemon.world_v2.relationship_acceptance_runtime import (
    RelationshipProposalAuthorityReader,
)
from companion_daemon.world_v2.relationship_commitment_acceptance_runtime import (
    RELATIONSHIP_COMMITMENT_ACCEPTANCE_POLICY_DIGEST,
    RelationshipCommitmentAcceptanceRuntime,
    RelationshipCommitmentAtomicRecorder,
    relationship_commitment_mutation_event_id,
)
from companion_daemon.world_v2.relationship_commitment_acceptance_manifest import (
    build_relationship_commitment_acceptance_manifest,
)
from companion_daemon.world_v2.relationship_events import (
    RelationshipCommitmentAcceptedPayload,
    relationship_mutation_hash,
)
from companion_daemon.world_v2.relationship_reducers import (
    RELATIONSHIP_POLICY_DIGEST,
    accept_relationship_commitment,
    relationship_primary_id,
)
from companion_daemon.world_v2.schemas import (
    EvidenceRef,
    RelationshipCommitmentOrigin,
    RelationshipCommitmentDeliveryProof,
    RelationshipCommitmentProjection,
    RelationshipHysteresisProjection,
    ProjectionCursor,
    RelationshipProposalProjection,
    RelationshipProposedMutation,
    RelationshipStateProjection,
    WorldEvent,
)
from companion_daemon.world_v2.sqlite_ledger import SQLiteWorldLedger


NOW = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)
SUBJECT = "user:geoff"
RELATIONSHIP_ID = relationship_primary_id(subject_ref=SUBJECT)
MUTATION_EVENT_ID = "event:relationship-commitment:friend"
WORLD_ID = "world:relationship-commitment"


def _evidence() -> EvidenceRef:
    return EvidenceRef(
        ref_id="observation:friendship-agreement",
        evidence_type="observed_message",
        claim_purpose="private_hypothesis",
        immutable_hash="a" * 64,
    )


def _payload() -> RelationshipCommitmentAcceptedPayload:
    evidence_refs = (_evidence(),)
    policy_refs = ("policy:relationship-v1",)
    commitment = RelationshipCommitmentProjection(
        commitment_id="relationship-commitment:friend",
        relationship_id=RELATIONSHIP_ID,
        subject_ref=SUBJECT,
        stage_before="stranger",
        committed_stage="friend",
        status="active",
        commitment_code="mutual_friendship_acknowledged",
        persistence="durable",
        visible_text_span="那说好了，你是我朋友了",
        delivery_proof=RelationshipCommitmentDeliveryProof(
            expression_proposal_id="proposal:expression:friend",
            expression_acceptance_id="acceptance:expression:friend",
            expression_plan_id="plan:expression:friend",
            plan_event_ref="event:expression-plan:friend",
            plan_event_payload_hash="1" * 64,
            expression_beat_id="beat:expression:friend",
            beat_event_ref="event:expression-beat:friend",
            beat_event_payload_hash="2" * 64,
            message_payload_ref="payload:expression:friend",
            message_payload_hash="sha256:" + "3" * 64,
            stored_payload_event_ref="event:message-payload:friend",
            stored_payload_event_hash="4" * 64,
            action_id="action:expression:friend",
            action_target_ref=SUBJECT,
            action_event_ref="event:action:friend",
            action_event_payload_hash="5" * 64,
            receipt_id="receipt:expression:friend",
            receipt_event_ref="event:receipt:friend",
            receipt_event_payload_hash="6" * 64,
            receipt_world_revision=4,
        ),
        evidence_refs=evidence_refs,
        origin=RelationshipCommitmentOrigin(
            change_id="change:relationship-commitment:friend",
            transition_id="transition:relationship-commitment:friend",
            policy_refs=policy_refs,
            accepted_event_ref=MUTATION_EVENT_ID,
        ),
        committed_at=NOW,
    )
    raw: dict[str, object] = {
        "change_id": commitment.origin.change_id,
        "transition_id": commitment.origin.transition_id,
        "expected_entity_revision": 0,
        "evidence_refs": evidence_refs,
        "policy_refs": policy_refs,
        "acceptance_id": "acceptance:relationship-commitment:friend",
        "proposal_id": "proposal:relationship-commitment:friend",
        "evaluated_world_revision": 4,
        "accepted_change_hash": "0" * 64,
        "relationship_id": RELATIONSHIP_ID,
        "subject_ref": SUBJECT,
        "stage_before": "stranger",
        "stage_after": "friend",
        "hysteresis_before": RelationshipHysteresisProjection(),
        "hysteresis_after": RelationshipHysteresisProjection(),
        "commitment_refs_before": (),
        "commitment": commitment,
        "policy_version": "relationship-policy.1",
        "policy_digest": RELATIONSHIP_POLICY_DIGEST,
    }
    raw["accepted_change_hash"] = relationship_mutation_hash(raw)
    return RelationshipCommitmentAcceptedPayload.model_validate(raw)


def test_typed_relationship_commitment_atomically_projects_commitment_and_stage() -> None:
    payload = _payload()

    commitments, states = accept_relationship_commitment(
        (),
        (),
        payload,
        logical_time=NOW,
        accepted_event_ref=MUTATION_EVENT_ID,
    )

    assert commitments == (payload.commitment,)
    assert len(states) == 1
    state = states[0]
    assert state.relationship_id == RELATIONSHIP_ID
    assert state.subject_ref == SUBJECT
    assert state.entity_revision == 1
    assert state.stage == "friend"
    assert state.commitment_refs == (payload.commitment.commitment_id,)
    assert state.hysteresis == RelationshipHysteresisProjection()
    assert state.origin is not None
    assert state.origin.accepted_event_ref == MUTATION_EVENT_ID
    assert commitments[0].commitment_code == "mutual_friendship_acknowledged"
    assert commitments[0].persistence == "durable"
    assert commitments[0].visible_text_span == "那说好了，你是我朋友了"


def test_relationship_commitment_rejects_uninstalled_intimate_stage() -> None:
    raw = _payload().commitment.model_dump()
    raw["committed_stage"] = "lover"

    with pytest.raises(ValueError, match="ordinary installed stage"):
        RelationshipCommitmentProjection.model_validate(raw)


def test_relationship_commitment_rejects_stale_state_without_partial_projection() -> None:
    commitments: tuple[RelationshipCommitmentProjection, ...] = ()
    states = (
        RelationshipStateProjection(
            relationship_id=RELATIONSHIP_ID,
            subject_ref=SUBJECT,
            entity_revision=1,
            policy_digest=RELATIONSHIP_POLICY_DIGEST,
        ),
    )

    with pytest.raises(ValueError, match="stale relationship commitment"):
        accept_relationship_commitment(
            commitments,
            states,
            _payload(),
            logical_time=NOW,
            accepted_event_ref=MUTATION_EVENT_ID,
        )

    assert commitments == ()
    assert states[0].entity_revision == 1
    assert states[0].stage == "stranger"
    assert states[0].commitment_refs == ()


def test_relationship_commitment_rejects_subject_identity_mismatch() -> None:
    payload = _payload()
    raw = payload.model_dump()
    raw["relationship_id"] = "relationship:forged"
    raw["commitment"] = payload.commitment.model_copy(
        update={"relationship_id": "relationship:forged"}
    )
    raw["accepted_change_hash"] = relationship_mutation_hash(raw)
    forged = RelationshipCommitmentAcceptedPayload.model_validate(raw)

    with pytest.raises(ValueError, match="primary relationship identity"):
        accept_relationship_commitment(
            (),
            (),
            forged,
            logical_time=NOW,
            accepted_event_ref=MUTATION_EVENT_ID,
        )


def test_relationship_commitment_reducer_rejects_uninstalled_stage_skip() -> None:
    payload = _payload()
    raw = payload.model_dump()
    raw["stage_after"] = "close_friend"
    raw["commitment"] = payload.commitment.model_copy(
        update={"committed_stage": "close_friend"}
    )
    raw["accepted_change_hash"] = relationship_mutation_hash(raw)
    crafted = RelationshipCommitmentAcceptedPayload.model_validate(raw)

    with pytest.raises(ValueError, match="stage transition is not installed"):
        accept_relationship_commitment(
            (),
            (),
            crafted,
            logical_time=NOW,
            accepted_event_ref=MUTATION_EVENT_ID,
        )


def test_relationship_commitment_is_effect_once() -> None:
    payload = _payload()
    commitments, states = accept_relationship_commitment(
        (), (), payload, logical_time=NOW, accepted_event_ref=MUTATION_EVENT_ID
    )

    with pytest.raises(ValueError, match="already exists"):
        accept_relationship_commitment(
            commitments,
            states,
            payload,
            logical_time=NOW,
            accepted_event_ref=MUTATION_EVENT_ID,
        )

    assert commitments == (payload.commitment,)
    assert states[0].commitment_refs == (payload.commitment.commitment_id,)


def test_relationship_commitment_cold_rebuild_is_deterministic() -> None:
    payload = _payload()

    first = accept_relationship_commitment(
        (), (), payload, logical_time=NOW, accepted_event_ref=MUTATION_EVENT_ID
    )
    rebuilt = accept_relationship_commitment(
        (), (), payload, logical_time=NOW, accepted_event_ref=MUTATION_EVENT_ID
    )

    assert rebuilt == first


class _CaptureIssuer:
    def __init__(self) -> None:
        self.values: dict[str, object] | None = None

    def issue(self, **values: object) -> object:
        self.values = values
        return object()


class _ProposalLedger:
    def __init__(self, proposal: RelationshipProposalProjection, event: WorldEvent) -> None:
        self.world_id = WORLD_ID
        self._proposal = proposal
        self._event = event

    def project_at(self, cursor: ProjectionCursor) -> object:
        del cursor
        return SimpleNamespace(relationship_proposals=(self._proposal,))

    def lookup_event_commit(self, event_id: str) -> tuple[WorldEvent, object] | None:
        if event_id == self._event.event_id:
            return self._event, SimpleNamespace()
        return None


def _recorded_proposal() -> tuple[RelationshipProposalProjection, WorldEvent]:
    mutation_event_id = relationship_commitment_mutation_event_id(
        world_id=WORLD_ID,
        proposal_id="proposal:relationship-commitment:friend",
        transition_id="transition:relationship-commitment:friend",
    )
    payload = _payload().model_copy(
        update={
            "commitment": _payload().commitment.model_copy(
                update={
                    "origin": _payload().commitment.origin.model_copy(
                        update={"accepted_event_ref": mutation_event_id}
                    )
                }
            )
        }
    )
    # The accepted hash includes the event-bound commitment origin.
    raw = payload.model_dump()
    raw["accepted_change_hash"] = relationship_mutation_hash(raw)
    payload = RelationshipCommitmentAcceptedPayload.model_validate(raw)
    base = RelationshipProposalProjection(
        proposal_id=payload.proposal_id,
        proposal_encoding="typed-authority-v1",
        authority_contract_ref="proposal-contract:relationship.1",
        transition_kind="commitment",
        change_id=payload.change_id,
        transition_id=payload.transition_id,
        evaluated_world_revision=payload.evaluated_world_revision,
        expected_entity_revision=payload.expected_entity_revision,
        proposed_change_hash=payload.accepted_change_hash,
        evidence_refs=payload.evidence_refs,
        policy_refs=payload.policy_refs,
        proposed_mutation=RelationshipProposedMutation(
            event_type="RelationshipCommitmentAccepted",
            payload_json=json.dumps(
                payload.model_dump(mode="json"),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        ),
    )
    event = WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id="event:relationship-commitment-proposal",
        world_id=WORLD_ID,
        event_type="ProposalRecorded",
        logical_time=NOW,
        created_at=NOW,
        actor="character:companion",
        source="character-interior",
        trace_id="trace:relationship-commitment",
        causation_id="event:observation",
        correlation_id="correlation:relationship-commitment",
        idempotency_key="proposal:relationship-commitment:friend",
        payload=base.model_dump(mode="json"),
    )
    return base.model_copy(
        update={
            "recorded_event_ref": event.event_id,
            "recorded_event_payload_hash": event.payload_hash,
        }
    ), event


def test_relationship_commitment_recorder_emits_one_closed_cas_batch(monkeypatch) -> None:
    proposal, event = _recorded_proposal()
    reader = RelationshipProposalAuthorityReader(
        ledger=_ProposalLedger(proposal, event)  # type: ignore[arg-type]
    )
    cursor = ProjectionCursor(
        world_revision=proposal.evaluated_world_revision,
        deliberation_revision=2,
        ledger_sequence=8,
    )
    handle = reader.pin(world_id=WORLD_ID, cursor=cursor, proposal_id=proposal.proposal_id)
    issuer = _CaptureIssuer()
    recorder = RelationshipCommitmentAtomicRecorder(  # type: ignore[arg-type]
        proposal_reader=reader,
        batch_issuer=issuer,
    )
    # Shared identity/catalog registration belongs to the composition owner.
    monkeypatch.setattr(
        "companion_daemon.world_v2.relationship_commitment_acceptance_runtime.domain_idempotency_key",
        lambda **_values: "test:relationship-commitment-identity",
    )

    recorder.prepare_batch(
        handle=handle,
        actor="worker:relationship-commitment",
        source="test:relationship-commitment",
        logical_time=NOW,
        created_at=NOW,
        trace_id="trace:relationship-commitment",
        correlation_id="correlation:relationship-commitment",
    )

    assert issuer.values is not None
    assert issuer.values["expected_cursor"] == cursor
    events = issuer.values["events"]
    assert isinstance(events, tuple)
    assert tuple(item.event_type for item in events) == (
        "AcceptanceRecorded",
        "RelationshipCommitmentAccepted",
    )
    acceptance, mutation = events
    assert mutation.causation_id == acceptance.event_id
    assert mutation.event_id == relationship_commitment_mutation_event_id(
        world_id=WORLD_ID,
        proposal_id=proposal.proposal_id,
        transition_id=proposal.transition_id,
    )
    assert mutation.payload()["commitment"]["committed_stage"] == "friend"


def _ledger_event(
    event_id: str,
    event_type: str,
    payload: dict[str, object],
    *,
    causation_id: str | None = None,
) -> WorldEvent:
    identity = domain_idempotency_key(
        event_type=event_type,
        world_id=WORLD_ID,
        payload=payload,
    )
    return WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id=event_id,
        world_id=WORLD_ID,
        event_type=event_type,
        logical_time=NOW,
        created_at=NOW,
        actor="system:test",
        source="test:relationship-commitment",
        trace_id="trace:relationship-commitment",
        causation_id=causation_id or f"cause:{event_id}",
        correlation_id="correlation:relationship-commitment",
        idempotency_key=identity or f"identity:{event_id}",
        payload=payload,
    )


def _cursor(runtime: RelationshipCommitmentAcceptanceRuntime) -> ProjectionCursor:
    head = runtime.ledger.project()
    return ProjectionCursor(
        world_revision=head.world_revision,
        deliberation_revision=head.deliberation_revision,
        ledger_sequence=head.ledger_sequence,
    )


def _record_ready_commitment_proposal(
    runtime: RelationshipCommitmentAcceptanceRuntime,
) -> RelationshipCommitmentAcceptedPayload:
    ledger = runtime.ledger
    evidence_hash = "b" * 64
    head = ledger.project()
    ledger.commit(
        [
            _ledger_event(
                "event:relationship-commitment-clock",
                "ObservationRecorded",
                {"observation_id": "observation:relationship-commitment-clock"},
            ),
            _ledger_event(
                "event:relationship-commitment-evidence",
                "OperatorObservationRecorded",
                {
                    "observation_id": "operator:relationship-commitment",
                    "observation_hash": evidence_hash,
                },
            ),
        ],
        expected_world_revision=head.world_revision,
        expected_deliberation_revision=head.deliberation_revision,
    )
    evaluated_world_revision = ledger.project().world_revision
    proposal_id = "proposal:relationship-commitment:ledger"
    change_id = "change:relationship-commitment:ledger"
    transition_id = "transition:relationship-commitment:ledger"
    mutation_event_id = relationship_commitment_mutation_event_id(
        world_id=WORLD_ID,
        proposal_id=proposal_id,
        transition_id=transition_id,
    )
    evidence_refs = (
        EvidenceRef(
            ref_id="operator:relationship-commitment",
            evidence_type="operator_observation",
            claim_purpose="private_hypothesis",
            immutable_hash=evidence_hash,
        ),
    )
    policy_refs = ("policy:relationship-v1",)
    commitment = RelationshipCommitmentProjection(
        commitment_id="relationship-commitment:ledger",
        relationship_id=RELATIONSHIP_ID,
        subject_ref=SUBJECT,
        stage_before="stranger",
        committed_stage="friend",
        commitment_code="role_authored_friendship_commitment",
        visible_text_span="那说好了，你是我朋友了",
        delivery_proof=_payload().commitment.delivery_proof,
        evidence_refs=evidence_refs,
        origin=RelationshipCommitmentOrigin(
            change_id=change_id,
            transition_id=transition_id,
            policy_refs=policy_refs,
            accepted_event_ref=mutation_event_id,
        ),
        committed_at=NOW,
    )
    raw: dict[str, object] = {
        "change_id": change_id,
        "transition_id": transition_id,
        "expected_entity_revision": 0,
        "evidence_refs": evidence_refs,
        "policy_refs": policy_refs,
        "acceptance_id": "acceptance:relationship-commitment:ledger",
        "proposal_id": proposal_id,
        "evaluated_world_revision": evaluated_world_revision,
        "accepted_change_hash": "0" * 64,
        "relationship_id": RELATIONSHIP_ID,
        "subject_ref": SUBJECT,
        "stage_before": "stranger",
        "stage_after": "friend",
        "hysteresis_before": RelationshipHysteresisProjection(),
        "hysteresis_after": RelationshipHysteresisProjection(),
        "commitment_refs_before": (),
        "commitment": commitment,
        "policy_version": "relationship-policy.1",
        "policy_digest": RELATIONSHIP_POLICY_DIGEST,
    }
    raw["accepted_change_hash"] = relationship_mutation_hash(raw)
    payload = RelationshipCommitmentAcceptedPayload.model_validate(raw)
    proposal = RelationshipProposalProjection(
        proposal_id=proposal_id,
        proposal_encoding="typed-authority-v1",
        authority_contract_ref="proposal-contract:relationship.1",
        transition_kind="commitment",
        change_id=change_id,
        transition_id=transition_id,
        evaluated_world_revision=evaluated_world_revision,
        expected_entity_revision=0,
        proposed_change_hash=payload.accepted_change_hash,
        evidence_refs=evidence_refs,
        policy_refs=policy_refs,
        proposed_mutation=RelationshipProposedMutation(
            event_type="RelationshipCommitmentAccepted",
            payload_json=json.dumps(
                payload.model_dump(mode="json"),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        ),
    )
    head = ledger.project()
    ledger.commit(
        [
            _ledger_event(
                "event:relationship-commitment-proposal:ledger",
                "ProposalRecorded",
                proposal.model_dump(mode="json"),
            )
        ],
        expected_world_revision=head.world_revision,
        expected_deliberation_revision=head.deliberation_revision,
    )
    return payload


def _runtime(*, sqlite_path=None) -> RelationshipCommitmentAcceptanceRuntime:
    issuer = AcceptedLedgerBatchIssuer()
    ledger = (
        SQLiteWorldLedger(
            path=sqlite_path,
            world_id=WORLD_ID,
            accepted_batch_issuer=issuer,
        )
        if sqlite_path is not None
        else WorldLedger.in_memory(world_id=WORLD_ID, accepted_batch_issuer=issuer)
    )
    return RelationshipCommitmentAcceptanceRuntime(ledger=ledger, batch_issuer=issuer)


def test_relationship_commitment_runtime_commits_one_atomic_projection() -> None:
    runtime = _runtime()
    payload = _record_ready_commitment_proposal(runtime)
    handle = runtime.pin_proposal(cursor=_cursor(runtime), proposal_id=payload.proposal_id)

    first = runtime.accept_runtime_owned(
        handle=handle,
        actor="worker:relationship-commitment",
        source="test:relationship-commitment",
    )
    replay = runtime.accept_runtime_owned(
        handle=handle,
        actor="worker:relationship-commitment",
        source="test:relationship-commitment",
    )

    projection = runtime.ledger.project()
    assert replay == first
    assert projection.relationship_commitments == (payload.commitment,)
    assert projection.relationship_states[0].stage == "friend"
    assert projection.relationship_states[0].commitment_refs == (
        payload.commitment.commitment_id,
    )
    assert projection.relationship_proposals == ()
    assert tuple(
        runtime.ledger.lookup_event_commit(event_id)[0].event_type
        for event_id in first.event_ids
    ) == ("AcceptanceRecorded", "RelationshipCommitmentAccepted")


def test_relationship_commitment_runtime_stale_cas_has_no_partial_projection() -> None:
    runtime = _runtime()
    payload = _record_ready_commitment_proposal(runtime)
    handle = runtime.pin_proposal(cursor=_cursor(runtime), proposal_id=payload.proposal_id)
    head = runtime.ledger.project()
    runtime.ledger.commit(
        [
            _ledger_event(
                "event:relationship-commitment-race",
                "ObservationRecorded",
                {"observation_id": "observation:relationship-commitment-race"},
            )
        ],
        expected_world_revision=head.world_revision,
        expected_deliberation_revision=head.deliberation_revision,
    )

    with pytest.raises(ConcurrencyConflict, match="stale world revision"):
        runtime.accept_runtime_owned(
            handle=handle,
            actor="worker:relationship-commitment",
            source="test:relationship-commitment",
        )

    projection = runtime.ledger.project()
    assert projection.relationship_commitments == ()
    assert projection.relationship_states == ()
    assert len(projection.relationship_proposals) == 1


def test_relationship_commitment_manifest_requires_recorder_capability() -> None:
    runtime = _runtime()
    payload = _record_ready_commitment_proposal(runtime)
    cursor = _cursor(runtime)
    handle = runtime.pin_proposal(cursor=cursor, proposal_id=payload.proposal_id)
    capture = _CaptureIssuer()
    recorder = RelationshipCommitmentAtomicRecorder(  # type: ignore[arg-type]
        proposal_reader=runtime._reader,  # type: ignore[attr-defined]
        batch_issuer=capture,
    )
    recorder.prepare_batch(
        handle=handle,
        actor="worker:relationship-commitment",
        source="test:relationship-commitment",
        logical_time=NOW,
        created_at=NOW,
        trace_id="trace:relationship-commitment",
        correlation_id="correlation:relationship-commitment",
    )
    assert capture.values is not None
    events = capture.values["events"]
    assert isinstance(events, tuple)

    with pytest.raises(
        ValueError,
        match="relationship_commitment_acceptance.recorder_capability_required",
    ):
        runtime.ledger.commit(
            events,
            expected_world_revision=cursor.world_revision,
            expected_deliberation_revision=cursor.deliberation_revision,
        )

    assert runtime.ledger.project().relationship_commitments == ()


def test_relationship_commitment_manifest_rejects_uninstalled_policy_digest() -> None:
    with pytest.raises(ValueError, match="policy digest is not installed"):
        build_relationship_commitment_acceptance_manifest(
            acceptance_id="acceptance:relationship-commitment:forged-policy",
            proposal_id="proposal:relationship-commitment:forged-policy",
            proposal_event_ref="event:relationship-commitment:forged-policy",
            proposal_event_payload_hash="1" * 64,
            evaluated_world_revision=7,
            accepted_change_id="change:relationship-commitment:forged-policy",
            accepted_change_hash="2" * 64,
            mutation_event_id="event:relationship-commitment:mutation:forged-policy",
            mutation_event_type="RelationshipCommitmentAccepted",
            mutation_payload_hash="3" * 64,
            policy_digest="4" * 64,
        )


def test_relationship_commitment_authorized_batch_rejects_any_third_event() -> None:
    issuer = AcceptedLedgerBatchIssuer()
    ledger = WorldLedger.in_memory(
        world_id=WORLD_ID,
        accepted_batch_issuer=issuer,
    )
    runtime = RelationshipCommitmentAcceptanceRuntime(
        ledger=ledger,
        batch_issuer=issuer,
    )
    payload = _record_ready_commitment_proposal(runtime)
    cursor = _cursor(runtime)
    handle = runtime.pin_proposal(cursor=cursor, proposal_id=payload.proposal_id)
    exact = runtime._recorder.prepare_batch(  # type: ignore[attr-defined]
        handle=handle,
        actor="worker:relationship-commitment",
        source="test:relationship-commitment",
        logical_time=NOW,
        created_at=NOW,
        trace_id="trace:relationship-commitment",
        correlation_id="correlation:relationship-commitment",
    )
    events, _ = issuer.verify(
        handle=exact,
        world_id=WORLD_ID,
        expected_cursor=cursor,
    )
    third = _ledger_event(
        "event:relationship-commitment-forged-third",
        "ObservationRecorded",
        {"observation_id": "observation:relationship-commitment-forged-third"},
    )
    malformed = issuer.issue(
        world_id=WORLD_ID,
        expected_cursor=cursor,
        events=(*events, third),
        manifest_hash=str(events[0].payload()["manifest_hash"]),
        registry_digest=RELATIONSHIP_COMMITMENT_ACCEPTANCE_POLICY_DIGEST,
        commit_id="commit:relationship-commitment:forged-third",
    )

    with pytest.raises(
        ValueError,
        match="relationship_commitment_acceptance.accepted_batch_must_be_exact",
    ):
        ledger.commit_accepted(malformed, expected_cursor=cursor)

    projection = ledger.project()
    assert projection.relationship_commitments == ()
    assert projection.relationship_states == ()


def test_relationship_commitment_sqlite_cold_rebuild_is_exact(tmp_path) -> None:
    path = tmp_path / "relationship-commitment.sqlite3"
    runtime = _runtime(sqlite_path=path)
    payload = _record_ready_commitment_proposal(runtime)
    runtime.accept_runtime_owned(
        handle=runtime.pin_proposal(cursor=_cursor(runtime), proposal_id=payload.proposal_id),
        actor="worker:relationship-commitment",
        source="test:relationship-commitment",
    )
    expected = runtime.ledger.project()
    assert runtime.ledger.rebuild() == expected
    runtime.close()

    reopened = SQLiteWorldLedger(path=path, world_id=WORLD_ID)
    assert reopened.project() == expected
    assert reopened.rebuild() == expected
    reopened.close()

from __future__ import annotations

from datetime import UTC, datetime
import json
from types import SimpleNamespace

import pytest

from companion_daemon.world_v2.accepted_ledger_batch import AcceptedLedgerBatchIssuer
from companion_daemon.world_v2.ledger import WorldLedger
from companion_daemon.world_v2.relationship_adjustment_acceptance_manifest import (
    RELATIONSHIP_ADJUSTMENT_ACCEPTANCE_MANIFEST_VERSION,
    RelationshipAdjustmentAcceptanceManifest,
    build_relationship_adjustment_acceptance_manifest,
)
from companion_daemon.world_v2.relationship_adjustment_acceptance_runtime import (
    RELATIONSHIP_ADJUSTMENT_ACCEPTANCE_POLICY_DIGEST,
    RelationshipAdjustmentAcceptanceError,
    RelationshipAdjustmentAtomicRecorder,
    RelationshipAdjustmentAcceptanceRuntime,
    relationship_adjustment_mutation_event_id,
)
from companion_daemon.world_v2.relationship_acceptance_runtime import (
    RelationshipProposalAuthorityReader,
)
from companion_daemon.world_v2.relationship_events import (
    RelationshipSlowVariableAdjustedPayload,
    relationship_mutation_hash,
)
from companion_daemon.world_v2.relationship_reducers import RELATIONSHIP_POLICY_DIGEST
from companion_daemon.world_v2.schemas import (
    EvidenceRef,
    ProjectionCursor,
    RelationshipHysteresisProjection,
    RelationshipProposalProjection,
    RelationshipProposedMutation,
    RelationshipVariableDeltas,
    RelationshipVariablesProjection,
    WorldEvent,
)
from test_relationship_authority import (
    EVIDENCE_HASH as AUTHORITY_EVIDENCE_HASH,
    NOW as AUTHORITY_NOW,
    WORLD as AUTHORITY_WORLD,
    authorized,
    decide_and_mutate,
    event as authority_event,
    new_signal_payload,
    proposal as authority_proposal,
    record_proposal,
)


NOW = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
WORLD_ID = "world-relationship-adjustment-acceptance"


class _CaptureIssuer:
    def __init__(self) -> None:
        self.values: dict[str, object] | None = None

    def issue(self, **values: object) -> object:
        self.values = values
        return object()


class _ProposalLedger:
    def __init__(self, *, proposal: RelationshipProposalProjection, event: WorldEvent) -> None:
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


def _adjustment_payload(*, operation: str = "adjust") -> dict[str, object]:
    refs = (
        EvidenceRef(
            ref_id="operator:relationship",
            evidence_type="operator_observation",
            claim_purpose="private_hypothesis",
            immutable_hash="a" * 64,
        ),
    )
    raw: dict[str, object] = {
        "change_id": "change:relationship-adjustment:1",
        "transition_id": "transition:relationship-adjustment:1",
        "expected_entity_revision": 0,
        "evidence_refs": refs,
        "policy_refs": ("policy:relationship-v1",),
        "acceptance_id": "acceptance:relationship-adjustment:1",
        "proposal_id": "proposal:relationship-adjustment:1",
        "evaluated_world_revision": 7,
        "accepted_change_hash": "0" * 64,
        "relationship_id": "relationship:user:geoff",
        "subject_ref": "user:geoff",
        "adjustment_id": "adjustment:relationship:1",
        "operation": operation,
        "signal_refs": ("signal:relationship:1",),
        "proposed_deltas": RelationshipVariableDeltas(trust_bp=120).model_dump(mode="json"),
        "accepted_deltas": RelationshipVariableDeltas(trust_bp=100).model_dump(mode="json"),
        "variables_before": RelationshipVariablesProjection().model_dump(mode="json"),
        "variables_after": RelationshipVariablesProjection(trust_bp=100).model_dump(mode="json"),
        "stage_before": "stranger",
        "stage_after": "stranger",
        "hysteresis_before": RelationshipHysteresisProjection().model_dump(mode="json"),
        "hysteresis_after": RelationshipHysteresisProjection().model_dump(mode="json"),
        "commitment_refs": (),
        "confidence_bp": 8_000,
        "persistence": "durable",
        "contradiction_group_ref": None,
        "rationale_code": "sustained_reliability",
        "policy_version": "relationship-policy.1",
        "policy_digest": RELATIONSHIP_POLICY_DIGEST,
        "adjusted_at": NOW,
        "compensates_adjustment_id": (
            "adjustment:prior" if operation == "compensate" else None
        ),
    }
    raw["accepted_change_hash"] = relationship_mutation_hash(raw)
    return raw


def _proposal(*, transition_kind: str = "adjust", operation: str = "adjust") -> tuple[
    RelationshipProposalProjection, WorldEvent
]:
    raw_payload = _adjustment_payload(operation=operation)
    mutation = RelationshipSlowVariableAdjustedPayload.model_validate(raw_payload)
    payload = mutation.model_dump(mode="json")
    valid_transition = "compensate" if operation == "compensate" else "adjust"
    base = RelationshipProposalProjection(
        proposal_id=mutation.proposal_id,
        proposal_encoding="typed-authority-v1",
        authority_contract_ref="proposal-contract:relationship.1",
        transition_kind=valid_transition,
        change_id=mutation.change_id,
        transition_id=mutation.transition_id,
        evaluated_world_revision=mutation.evaluated_world_revision,
        expected_entity_revision=mutation.expected_entity_revision,
        proposed_change_hash=mutation.accepted_change_hash,
        evidence_refs=mutation.evidence_refs,
        policy_refs=mutation.policy_refs,
        proposed_mutation=RelationshipProposedMutation(
            event_type="RelationshipSlowVariableAdjusted",
            payload_json=json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        ),
    )
    proposal_without_provenance = (
        base
        if transition_kind == valid_transition
        else RelationshipProposalProjection.model_construct(
            **(base.__dict__ | {"transition_kind": transition_kind})
        )
    )
    event = WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id="event:relationship-adjustment-proposal:1",
        world_id=WORLD_ID,
        event_type="ProposalRecorded",
        logical_time=NOW,
        created_at=NOW,
        actor="test:relationship-adjustment",
        source="test:relationship-adjustment",
        trace_id="trace:relationship-adjustment",
        causation_id="event:source",
        correlation_id="correlation:relationship-adjustment",
        idempotency_key="test:relationship-adjustment-proposal:1",
        payload=proposal_without_provenance.model_dump(mode="json"),
    )
    proposal = proposal_without_provenance.model_copy(
        update={
            "recorded_event_ref": event.event_id,
            "recorded_event_payload_hash": event.payload_hash,
        }
    )
    return proposal, event


def _recorder(*, transition_kind: str = "adjust", operation: str = "adjust"):
    proposal, event = _proposal(transition_kind=transition_kind, operation=operation)
    ledger = _ProposalLedger(proposal=proposal, event=event)
    reader = RelationshipProposalAuthorityReader(ledger=ledger)  # type: ignore[arg-type]
    cursor = ProjectionCursor(world_revision=7, deliberation_revision=1, ledger_sequence=11)
    handle = reader.pin(world_id=WORLD_ID, cursor=cursor, proposal_id=proposal.proposal_id)
    issuer = _CaptureIssuer()
    return RelationshipAdjustmentAtomicRecorder(  # type: ignore[arg-type]
        proposal_reader=reader,
        batch_issuer=issuer,
    ), handle, issuer


def _prepare(recorder, handle):
    return recorder.prepare_batch(
        handle=handle,
        actor="worker:relationship-adjustment",
        source="test:relationship-adjustment",
        logical_time=NOW,
        created_at=NOW,
        trace_id="trace:relationship-adjustment",
        correlation_id="correlation:relationship-adjustment",
    )


def test_adjustment_manifest_is_closed_and_self_hashing() -> None:
    manifest = build_relationship_adjustment_acceptance_manifest(
        acceptance_id="acceptance:1",
        proposal_id="proposal:1",
        proposal_event_ref="event:proposal:1",
        proposal_event_payload_hash="a" * 64,
        evaluated_world_revision=7,
        accepted_change_id="change:1",
        accepted_change_hash="b" * 64,
        mutation_event_id="event:mutation:1",
        mutation_event_type="RelationshipSlowVariableAdjusted",
        mutation_payload_hash="c" * 64,
        policy_digest=RELATIONSHIP_ADJUSTMENT_ACCEPTANCE_POLICY_DIGEST,
    )

    assert manifest.manifest_version == RELATIONSHIP_ADJUSTMENT_ACCEPTANCE_MANIFEST_VERSION
    assert RelationshipAdjustmentAcceptanceManifest.model_validate(
        manifest.model_dump(mode="json"), strict=True
    ) == manifest
    with pytest.raises(ValueError, match="manifest hash"):
        RelationshipAdjustmentAcceptanceManifest.model_validate(
            manifest.model_dump(mode="json") | {"proposal_id": "proposal:forged"},
            strict=True,
        )


def test_adjustment_recorder_emits_one_closed_adjustment_batch(monkeypatch) -> None:
    recorder, handle, issuer = _recorder()
    # The shared event-identity registry is intentionally updated by the
    # integration change, outside this module-only task.  Pin the rest of the
    # boundary here so this test proves the emitted batch shape independently.
    monkeypatch.setattr(
        "companion_daemon.world_v2.relationship_adjustment_acceptance_runtime.domain_idempotency_key",
        lambda **_values: "test:identity",
    )

    _prepare(recorder, handle)

    assert issuer.values is not None
    events = issuer.values["events"]
    assert isinstance(events, tuple)
    acceptance, mutation = events
    manifest = acceptance.payload()
    assert manifest["manifest_version"] == "relationship-adjustment-acceptance.1"
    assert manifest["mutation_event_id"] == mutation.event_id
    assert mutation.event_type == "RelationshipSlowVariableAdjusted"
    assert mutation.event_id == relationship_adjustment_mutation_event_id(
        world_id=WORLD_ID,
        proposal_id="proposal:relationship-adjustment:1",
        transition_id="transition:relationship-adjustment:1",
    )


def test_adjustment_recorder_rejects_a_signal_transition_before_materializing() -> None:
    recorder, handle, _issuer = _recorder(transition_kind="signal")

    with pytest.raises(RelationshipAdjustmentAcceptanceError, match="transition_not_acceptable"):
        _prepare(recorder, handle)


def test_adjustment_recorder_rejects_compensation_payload_before_materializing() -> None:
    recorder, handle, _issuer = _recorder(operation="compensate")

    with pytest.raises(RelationshipAdjustmentAcceptanceError, match="mechanical_mutation_not_acceptable"):
        _prepare(recorder, handle)


def test_adjustment_runtime_commits_and_replays_a_real_accepted_batch() -> None:
    issuer = AcceptedLedgerBatchIssuer()
    ledger = WorldLedger.in_memory(world_id=AUTHORITY_WORLD, accepted_batch_issuer=issuer)
    ledger.commit(
        [
            authority_event("event:init", "ObservationRecorded", {"observation_id": "obs:init"}),
            authority_event(
                "event:init-operator",
                "OperatorObservationRecorded",
                {
                    "observation_id": "operator:relationship",
                    "observation_hash": AUTHORITY_EVIDENCE_HASH,
                },
            ),
        ],
        expected_world_revision=0,
        expected_deliberation_revision=0,
    )
    signal = new_signal_payload()
    record_proposal(ledger, authority_proposal(signal, transition_kind="signal"))
    decide_and_mutate(ledger, signal, "RelationshipSignalAccepted")

    head = ledger.project()
    adjustment = authorized(
        RelationshipSlowVariableAdjustedPayload,
        change_id="change:runtime-adjustment",
        transition_id="transition:runtime-adjustment",
        expected_entity_revision=0,
        policy_refs=("policy:relationship-v1",),
        acceptance_id="acceptance:runtime-adjustment",
        proposal_id="proposal:runtime-adjustment",
        evaluated_world_revision=head.world_revision,
        relationship_id="relationship:user:geoff",
        subject_ref="user:geoff",
        adjustment_id="adjustment:runtime:1",
        operation="adjust",
        signal_refs=(signal.signal.signal_id,),
        proposed_deltas=RelationshipVariableDeltas(trust_bp=120),
        accepted_deltas=RelationshipVariableDeltas(trust_bp=100),
        variables_before=RelationshipVariablesProjection(),
        variables_after=RelationshipVariablesProjection(trust_bp=100),
        stage_before="stranger",
        stage_after="stranger",
        hysteresis_before=RelationshipHysteresisProjection(),
        hysteresis_after=RelationshipHysteresisProjection(),
        confidence_bp=8_000,
        persistence="durable",
        contradiction_group_ref=None,
        rationale_code="sustained_reliability",
        policy_version="relationship-policy.1",
        policy_digest=RELATIONSHIP_POLICY_DIGEST,
        adjusted_at=AUTHORITY_NOW,
    )
    record_proposal(ledger, authority_proposal(adjustment, transition_kind="adjust"))
    runtime = RelationshipAdjustmentAcceptanceRuntime(ledger=ledger, batch_issuer=issuer)
    current = ledger.project()
    cursor = ProjectionCursor(
        world_revision=current.world_revision,
        deliberation_revision=current.deliberation_revision,
        ledger_sequence=current.ledger_sequence,
    )

    result = runtime.accept_runtime_owned(
        handle=runtime.pin_proposal(cursor=cursor, proposal_id=adjustment.proposal_id),
        actor="worker:relationship-adjustment",
        source="test:relationship-adjustment",
    )

    projection = ledger.project()
    acceptance, mutation = (ledger.lookup_event_commit(event_id)[0] for event_id in result.event_ids)
    assert acceptance.payload()["manifest_version"] == "relationship-adjustment-acceptance.1"
    assert mutation.event_type == "RelationshipSlowVariableAdjusted"
    assert projection.relationship_proposals == ()
    assert projection.relationship_adjustments[-1].adjustment_id == adjustment.adjustment_id
    assert ledger.rebuild() == projection

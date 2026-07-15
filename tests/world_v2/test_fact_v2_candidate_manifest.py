from __future__ import annotations

import copy
import pickle
from datetime import UTC, datetime

import pytest

from companion_daemon.world_v2.event_identity import domain_idempotency_key
from companion_daemon.world_v2.fact_proof_backed_evidence import (
    ProofBackedFactEvidenceResolverV2,
)
from companion_daemon.world_v2.fact_proposal_audit_v2 import (
    FactCommitProposalAuthorityReaderV2,
    build_fact_commit_proposal_recorded_event_v2,
)
from companion_daemon.world_v2.fact_v2_acceptance_envelope_authority import (
    FactV2AcceptanceEnvelopeAuthorityError,
    FactV2AcceptanceEnvelopeAuthorityHandle,
    FactV2AcceptanceEnvelopeAuthorityIssuer,
    FactV2AcceptanceEnvelopeRequestV2,
)
from companion_daemon.world_v2.fact_v2_candidate_manifest import (
    FACT_V2_CANDIDATE_EVENT_TYPE,
    FactV2AcceptanceEnvelopeCandidate,
    FactV2CandidateManifest,
    FactV2CandidateManifestBuilder,
    FactV2CandidateManifestError,
    FactV2CandidateManifestHandle,
)
from companion_daemon.world_v2.ledger import ObservationEventLocator
from companion_daemon.world_v2.proposal_envelope_v2 import (
    FactCommitProposalDraftV2,
    FactCommitProposalNormalizationContextV2,
    normalize_fact_commit_proposal_v2,
)
from companion_daemon.world_v2.schemas import ProjectionCursor, WorldEvent
from companion_daemon.world_v2.sealed_fact_commit_adapter_v2 import FactCommitPolicyResolutionV2
from companion_daemon.world_v2.sealed_production_fact_registry_v2 import (
    SealedProductionFactPreparationRegistryV2,
)
from companion_daemon.world_v2.sqlite_ledger import (
    SQLiteProofBackedObservationReader,
    SQLiteWorldLedger,
)


WORLD_ID = "world:fact-candidate"
NOW = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)


def _message() -> WorldEvent:
    payload = {
        "schema_version": "world-v2.1",
        "observation_kind": "message",
        "observation_id": "observation:message:1",
        "world_id": WORLD_ID,
        "logical_time": NOW.isoformat(),
        "created_at": NOW.isoformat(),
        "trace_id": "trace:fact-candidate",
        "causation_id": "cause:fact-candidate",
        "correlation_id": "correlation:fact-candidate",
        "source": "test",
        "source_event_id": "source:fact-candidate",
        "actor": "user:primary",
        "channel": "chat",
        "payload_ref": "payload:message:1",
        "payload_hash": "c" * 64,
        "received_at": NOW.isoformat(),
    }
    return WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id="event:fact-candidate:message",
        world_id=WORLD_ID,
        event_type="ObservationRecorded",
        logical_time=NOW,
        created_at=NOW,
        actor="user:primary",
        source="test",
        trace_id="trace:fact-candidate",
        causation_id="cause:fact-candidate",
        correlation_id="correlation:fact-candidate",
        idempotency_key=domain_idempotency_key(
            event_type="ObservationRecorded", world_id=WORLD_ID, payload=payload
        )
        or "unreachable",
        payload=payload,
    )


def _proposal(*, event_payload_hash: str):
    return normalize_fact_commit_proposal_v2(
        draft=FactCommitProposalDraftV2.model_validate(
            {
                "fact_commit_intents": (
                    {
                        "subject_ref": "user:primary",
                        "predicate_code": "profile.display_name",
                        "value_ref": "value:alice",
                        "value_hash": "sha256:" + "a" * 64,
                        "assertion_source_ref": "observation:message:1",
                        "evidence_uses": (
                            {
                                "evidence_ref": "observation:message:1",
                                "purpose": "current_fact",
                                "anchor": True,
                            },
                        ),
                        "confidence_bp": 9100,
                        "privacy_class": "personal",
                    },
                ),
                "confidence": 9000,
                "brief_rationale": "the message explicitly supplies a display name",
            },
            strict=True,
        ),
        context=FactCommitProposalNormalizationContextV2.model_validate(
            {
                "world_id": WORLD_ID,
                "proposal_id": "proposal:fact-candidate:1",
                "trigger_ref": "observation:message:1",
                "evaluated_world_revision": 1,
                "evidence_refs": (
                    {
                        "ref_id": "observation:message:1",
                        "evidence_kind": "observed_message",
                        "source_world_revision": 1,
                        "immutable_hash": "sha256:" + event_payload_hash,
                    },
                ),
                "policy_refs": ("policy:fact-commit.2",),
            },
            strict=True,
        ),
    )


def _cursor(ledger: SQLiteWorldLedger) -> ProjectionCursor:
    projection = ledger.project()
    return ProjectionCursor(
        world_revision=projection.world_revision,
        deliberation_revision=projection.deliberation_revision,
        ledger_sequence=projection.ledger_sequence,
    )


def _bound(tmp_path):
    ledger = SQLiteWorldLedger(path=tmp_path / "fact-candidate.sqlite3", world_id=WORLD_ID)
    message = _message()
    ledger.commit((message,), expected_world_revision=0, expected_deliberation_revision=0)
    proposal = _proposal(event_payload_hash=message.payload_hash)
    audit_event = build_fact_commit_proposal_recorded_event_v2(
        proposal=proposal,
        world_id=WORLD_ID,
        logical_time=NOW,
        created_at=NOW,
        actor="agent:companion",
        source="test",
        trace_id="trace:fact-candidate",
        causation_id="cause:fact-candidate",
        correlation_id="correlation:fact-candidate",
    )
    ledger.commit((audit_event,), expected_world_revision=1, expected_deliberation_revision=0)
    cursor = _cursor(ledger)
    proposal_reader = FactCommitProposalAuthorityReaderV2(ledger=ledger)
    proposal_handle = proposal_reader.pin(
        world_id=WORLD_ID, cursor=cursor, proposal_id=proposal.proposal_id
    )
    history_reader = SQLiteProofBackedObservationReader(ledger=ledger)
    resolver = ProofBackedFactEvidenceResolverV2(reader=history_reader)
    intent = proposal.proposed_changes[0].payload
    from companion_daemon.world_v2.fact_accepted_contracts import rehydrate_fact_commit_intent_v2_json

    sources = resolver.resolve(
        handle=history_reader.pin(world_id=WORLD_ID, cursor=cursor),
        intent=rehydrate_fact_commit_intent_v2_json(intent.canonical_json),
        locators=(
            ObservationEventLocator.for_message(
                world_id=WORLD_ID,
                observation_id="observation:message:1",
                source="test",
                source_event_id="source:fact-candidate",
            ),
        ),
    )
    registry = SealedProductionFactPreparationRegistryV2(resolver=resolver)
    prepared = registry.prepare_from_pinned_audit(
        proposal_reader=proposal_reader,
        proposal_handle=proposal_handle,
        change_id=proposal.proposed_changes[0].change_id,
        policy=FactCommitPolicyResolutionV2(
            cardinality="single", policy_refs=("policy:fact-commit.2",)
        ),
        world_id=WORLD_ID,
    )
    envelope = FactV2AcceptanceEnvelopeCandidate(
        acceptance_id="acceptance:fact-candidate:1",
        acceptance_event_id="event:acceptance:fact-candidate:1",
        acceptance_causation_id="cause:fact-candidate",
        cursor=cursor,
        world_id=WORLD_ID,
        logical_time=NOW,
        created_at=NOW,
        actor="agent:companion",
        source="test",
        trace_id="trace:fact-candidate",
        correlation_id="correlation:fact-candidate",
    )
    return ledger, registry, proposal_reader, proposal_handle, prepared, sources, envelope, proposal


def test_builds_one_inert_manifest_candidate_from_pinned_fact_authority(tmp_path) -> None:
    ledger, registry, reader, handle, prepared, sources, envelope, _ = _bound(tmp_path)
    builder = FactV2CandidateManifestBuilder(registry=registry, proposal_reader=reader)
    candidate_handle = builder.build(
        envelope=envelope,
        proposal_handle=handle,
        prepared=prepared,
        sources=sources,
    )
    candidate = builder.inspect(handle=candidate_handle)

    assert builder.owns(candidate_handle)
    assert candidate.manifest.manifest_version == "acceptance-manifest.3"
    assert candidate.manifest.proposals[0].audit_contract == "fact-commit-proposal-audit.2"
    assert candidate.manifest.authorized_effects[0].event_type == FACT_V2_CANDIDATE_EVENT_TYPE
    assert candidate.materialized_payload.acceptance_id == envelope.acceptance_id
    assert not hasattr(candidate_handle, "to_world_event")
    assert not hasattr(builder, "commit")
    tampered = candidate.model_copy(update={"candidate_idempotency_key": "forged:key"})
    with pytest.raises(ValueError, match="idempotency key"):
        FactV2CandidateManifest.model_validate(tampered, strict=True)
    ledger.close()


def test_candidate_builder_rejects_unpinned_preparation_and_counterfeit_handles(tmp_path) -> None:
    ledger, registry, reader, handle, prepared, sources, envelope, proposal = _bound(tmp_path)
    builder = FactV2CandidateManifestBuilder(registry=registry, proposal_reader=reader)
    raw_prepared = registry.prepare(
        proposal=proposal,
        change=proposal.proposed_changes[0],
        policy=FactCommitPolicyResolutionV2(
            cardinality="single", policy_refs=("policy:fact-commit.2",)
        ),
        world_id=WORLD_ID,
    )

    with pytest.raises(FactV2CandidateManifestError, match="bound to a proposal audit"):
        builder.build(
            envelope=envelope,
            proposal_handle=handle,
            prepared=raw_prepared,
            sources=sources,
        )
    with pytest.raises(FactV2CandidateManifestError, match="another builder"):
        builder.inspect(handle=FactV2CandidateManifestHandle())
    candidate_handle = builder.build(
        envelope=envelope,
        proposal_handle=handle,
        prepared=prepared,
        sources=sources,
    )
    for operation in (copy.copy, copy.deepcopy, pickle.dumps):
        with pytest.raises(TypeError):
            operation(candidate_handle)
    ledger.close()


def test_candidate_builder_requires_exact_full_cursor(tmp_path) -> None:
    ledger, registry, reader, handle, prepared, sources, envelope, _ = _bound(tmp_path)
    builder = FactV2CandidateManifestBuilder(registry=registry, proposal_reader=reader)
    bad = envelope.model_copy(
        update={
            "cursor": ProjectionCursor(
                world_revision=envelope.cursor.world_revision,
                deliberation_revision=0,
                ledger_sequence=1,
            )
        }
    )

    with pytest.raises(FactV2CandidateManifestError, match="does not match"):
        builder.build(
            envelope=bad, proposal_handle=handle, prepared=prepared, sources=sources
        )
    ledger.close()


def test_production_envelope_is_pinned_to_the_fact_proposal_audit(tmp_path) -> None:
    ledger, _, reader, proposal_handle, _, _, candidate, _ = _bound(tmp_path)
    request = FactV2AcceptanceEnvelopeRequestV2.model_validate(
        candidate.model_dump(mode="python")
        | {"acceptance_causation_id": reader.audit(handle=proposal_handle).event_ref},
        strict=True,
    )
    issuer = FactV2AcceptanceEnvelopeAuthorityIssuer()
    handle = issuer.issue(
        proposal_reader=reader, proposal_handle=proposal_handle, request=request
    )

    envelope = issuer.envelope(handle=handle)
    assert envelope.cursor == candidate.cursor
    assert envelope.proposal_audit_event_ref == request.acceptance_causation_id
    assert issuer.owns(handle)
    with pytest.raises(FactV2AcceptanceEnvelopeAuthorityError, match="proposal audit"):
        issuer.issue(
            proposal_reader=reader,
            proposal_handle=proposal_handle,
            request=request.model_copy(update={"acceptance_id": "acceptance:wrong-cause", "acceptance_causation_id": "cause:forged"}),
        )
    with pytest.raises(FactV2AcceptanceEnvelopeAuthorityError, match="another issuer"):
        FactV2AcceptanceEnvelopeAuthorityIssuer().envelope(handle=handle)
    for operation in (copy.copy, copy.deepcopy, pickle.dumps):
        with pytest.raises(TypeError):
            operation(handle)
    with pytest.raises(FactV2AcceptanceEnvelopeAuthorityError, match="another issuer"):
        issuer.envelope(handle=FactV2AcceptanceEnvelopeAuthorityHandle())
    ledger.close()

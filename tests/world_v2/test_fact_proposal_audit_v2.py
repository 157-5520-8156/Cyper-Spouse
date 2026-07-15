from __future__ import annotations

import copy
import pickle
from datetime import UTC, datetime

import pytest

from companion_daemon.world_v2.fact_proposal_audit_v2 import (
    FACT_COMMIT_PROPOSAL_RECORDED_EVENT_V2,
    FactCommitProposalAuditErrorV2,
    FactCommitProposalAuthorityReaderV2,
    FactCommitProposalRecordedPayloadV2,
    PinnedFactCommitProposalAuthorityHandleV2,
    build_fact_commit_proposal_recorded_event_v2,
    fact_commit_proposal_audit_event_id_v2,
)
from companion_daemon.world_v2.fact_proof_backed_evidence import (
    ProofBackedFactEvidenceResolverV2,
)
from companion_daemon.world_v2.event_identity import domain_idempotency_key
from companion_daemon.world_v2.proposal_envelope_v2 import (
    FactCommitProposalDraftV2,
    FactCommitProposalNormalizationContextV2,
    normalize_fact_commit_proposal_v2,
)
from companion_daemon.world_v2.schemas import ProjectionCursor, WorldEvent
from companion_daemon.world_v2.sealed_production_fact_registry_v2 import (
    SealedProductionFactPreparationRegistryV2,
    SealedProductionFactRegistryErrorV2,
)
from companion_daemon.world_v2.sealed_fact_commit_adapter_v2 import FactCommitPolicyResolutionV2
from companion_daemon.world_v2.sqlite_ledger import SQLiteWorldLedger
from companion_daemon.world_v2.sqlite_ledger import SQLiteProofBackedObservationReader


WORLD_ID = "world:fact-proposal-audit"
NOW = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)


def _proposal():
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
                "proposal_id": "proposal:fact-audit:1",
                "trigger_ref": "observation:message:1",
                "evaluated_world_revision": 1,
                "evidence_refs": (
                    {
                        "ref_id": "observation:message:1",
                        "evidence_kind": "observed_message",
                            "source_world_revision": 1,
                        "immutable_hash": "sha256:" + "b" * 64,
                    },
                ),
                "policy_refs": ("policy:fact-commit.2",),
            },
            strict=True,
        ),
    )


def _event():
    return build_fact_commit_proposal_recorded_event_v2(
        proposal=_proposal(),
        world_id=WORLD_ID,
        logical_time=NOW,
        created_at=NOW,
        actor="agent:companion",
        source="test",
        trace_id="trace:fact-proposal-audit",
        causation_id="cause:fact-proposal-audit",
        correlation_id="correlation:fact-proposal-audit",
    )


def _cursor(ledger: SQLiteWorldLedger) -> ProjectionCursor:
    projection = ledger.project()
    return ProjectionCursor(
        world_revision=projection.world_revision,
        deliberation_revision=projection.deliberation_revision,
        ledger_sequence=projection.ledger_sequence,
    )


def _world_started() -> WorldEvent:
    return WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id="event:fact-proposal-audit:world-started",
        world_id=WORLD_ID,
        event_type="WorldStarted",
        logical_time=NOW,
        created_at=NOW,
        actor="system:world",
        source="test",
        trace_id="trace:fact-proposal-audit",
        causation_id="cause:world-started",
        correlation_id="correlation:fact-proposal-audit",
        idempotency_key=domain_idempotency_key(
            event_type="WorldStarted", world_id=WORLD_ID, payload={}
        )
        or "idempotency:fact-proposal-audit:world-started",
        payload={},
    )


def _record(tmp_path):
    ledger = SQLiteWorldLedger(path=tmp_path / "fact-proposal-audit.sqlite3", world_id=WORLD_ID)
    ledger.commit((_world_started(),), expected_world_revision=0, expected_deliberation_revision=0)
    event = _event()
    ledger.commit((event,), expected_world_revision=1, expected_deliberation_revision=0)
    return ledger, event


def test_fact_v2_proposal_audit_is_durable_and_pinned_to_its_complete_cursor(tmp_path) -> None:
    ledger, event = _record(tmp_path)
    reader = FactCommitProposalAuthorityReaderV2(ledger=ledger)
    handle = reader.pin(world_id=WORLD_ID, cursor=_cursor(ledger), proposal_id="proposal:fact-audit:1")

    assert reader.owns(handle)
    assert reader.proposal(handle=handle).proposal_id == "proposal:fact-audit:1"
    audit = reader.audit(handle=handle)
    assert audit.event_ref == event.event_id
    assert audit.event_payload_hash == event.payload_hash
    assert audit.committed_cursor == _cursor(ledger)
    assert event.event_type == FACT_COMMIT_PROPOSAL_RECORDED_EVENT_V2
    assert event.event_id == fact_commit_proposal_audit_event_id_v2(
        world_id=WORLD_ID, proposal_id="proposal:fact-audit:1"
    )
    ledger.close()


def test_fact_v2_proposal_audit_survives_sqlite_reopen(tmp_path) -> None:
    path = tmp_path / "fact-proposal-audit.sqlite3"
    ledger, _ = _record(tmp_path)
    cursor = _cursor(ledger)
    ledger.close()

    reopened = SQLiteWorldLedger(path=path, world_id=WORLD_ID)
    reader = FactCommitProposalAuthorityReaderV2(ledger=reopened)
    handle = reader.pin(world_id=WORLD_ID, cursor=cursor, proposal_id="proposal:fact-audit:1")
    assert reader.audit(handle=handle).proposal_hash.startswith("sha256:")
    reopened.close()


def test_fact_v2_proposal_audit_rejects_missing_or_later_prefix_and_foreign_reader_handles(tmp_path) -> None:
    ledger, _ = _record(tmp_path)
    reader = FactCommitProposalAuthorityReaderV2(ledger=ledger)
    cursor = _cursor(ledger)
    handle = reader.pin(world_id=WORLD_ID, cursor=cursor, proposal_id="proposal:fact-audit:1")

    with pytest.raises(FactCommitProposalAuditErrorV2, match="outside the pinned cursor"):
        reader.pin(
            world_id=WORLD_ID,
            cursor=ProjectionCursor(world_revision=1, deliberation_revision=0, ledger_sequence=1),
            proposal_id="proposal:fact-audit:1",
        )
    with pytest.raises(FactCommitProposalAuditErrorV2, match="missing"):
        reader.pin(world_id=WORLD_ID, cursor=cursor, proposal_id="proposal:missing")
    other = FactCommitProposalAuthorityReaderV2(ledger=ledger)
    with pytest.raises(FactCommitProposalAuditErrorV2, match="another reader"):
        other.audit(handle=handle)
    with pytest.raises(FactCommitProposalAuditErrorV2, match="another reader"):
        reader.audit(handle=PinnedFactCommitProposalAuthorityHandleV2())
    ledger.close()


def test_fact_v2_proposal_audit_payload_and_handle_are_closed(tmp_path) -> None:
    ledger, event = _record(tmp_path)
    payload = event.payload()
    payload["proposal_hash"] = "sha256:" + "0" * 64
    with pytest.raises(ValueError, match="hash"):
        FactCommitProposalRecordedPayloadV2.model_validate(payload, strict=True)

    reader = FactCommitProposalAuthorityReaderV2(ledger=ledger)
    handle = reader.pin(world_id=WORLD_ID, cursor=_cursor(ledger), proposal_id="proposal:fact-audit:1")
    for operation in (copy.copy, copy.deepcopy, pickle.dumps):
        with pytest.raises(TypeError):
            operation(handle)
    ledger.close()


def test_fact_v2_proposal_audit_event_rejects_a_forged_idempotency_key(tmp_path) -> None:
    ledger = SQLiteWorldLedger(path=tmp_path / "forged-key.sqlite3", world_id=WORLD_ID)
    ledger.commit((_world_started(),), expected_world_revision=0, expected_deliberation_revision=0)
    forged = _event().model_copy(update={"idempotency_key": "forged:proposal-audit"})

    with pytest.raises(ValueError, match="idempotency key"):
        ledger.commit((forged,), expected_world_revision=1, expected_deliberation_revision=0)
    ledger.close()


def test_sealed_registry_derives_complete_durable_authority_only_from_pinned_audit(tmp_path) -> None:
    ledger, _ = _record(tmp_path)
    proposal_reader = FactCommitProposalAuthorityReaderV2(ledger=ledger)
    proposal_handle = proposal_reader.pin(
        world_id=WORLD_ID, cursor=_cursor(ledger), proposal_id="proposal:fact-audit:1"
    )
    resolver = ProofBackedFactEvidenceResolverV2(
        reader=SQLiteProofBackedObservationReader(ledger=ledger)
    )
    registry = SealedProductionFactPreparationRegistryV2(resolver=resolver)

    authority = registry.durable_authority_candidate(
        proposal_reader=proposal_reader, proposal_handle=proposal_handle
    )
    descriptor = registry.descriptor
    assert authority.authority_version == "effect-compiler-authority.1"
    assert authority.proposal_event_ref == proposal_reader.audit(handle=proposal_handle).event_ref
    assert authority.event_catalog_ref == descriptor.event_catalog_ref
    assert authority.reducer_bundle_digest == descriptor.reducer_bundle_digest
    assert authority.predicate_matrix_ref == "matrix:fact-predicate.2"
    assert not hasattr(authority, "to_world_event")
    assert not hasattr(registry, "durable_authority")
    with pytest.raises(SealedProductionFactRegistryErrorV2, match="reader-owned"):
        registry.durable_authority_candidate(
            proposal_reader=proposal_reader,
            proposal_handle=PinnedFactCommitProposalAuthorityHandleV2(),
        )
    ledger.close()


def test_sealed_registry_binds_pinned_audit_and_change_in_one_preparation_capability(tmp_path) -> None:
    ledger, _ = _record(tmp_path)
    proposal_reader = FactCommitProposalAuthorityReaderV2(ledger=ledger)
    proposal_handle = proposal_reader.pin(
        world_id=WORLD_ID, cursor=_cursor(ledger), proposal_id="proposal:fact-audit:1"
    )
    registry = SealedProductionFactPreparationRegistryV2(
        resolver=ProofBackedFactEvidenceResolverV2(
            reader=SQLiteProofBackedObservationReader(ledger=ledger)
        )
    )
    policy = FactCommitPolicyResolutionV2(
        cardinality="single", policy_refs=("policy:fact-commit.2",)
    )
    change_id = proposal_reader.proposal(handle=proposal_handle).proposed_changes[0].change_id

    prepared = registry.prepare_from_pinned_audit(
        proposal_reader=proposal_reader,
        proposal_handle=proposal_handle,
        change_id=change_id,
        policy=policy,
        world_id=WORLD_ID,
    )

    assert registry.owns_preparation(prepared)
    with pytest.raises(SealedProductionFactRegistryErrorV2, match="does not belong"):
        registry.prepare_from_pinned_audit(
            proposal_reader=proposal_reader,
            proposal_handle=proposal_handle,
            change_id="change:" + "0" * 64,
            policy=policy,
            world_id=WORLD_ID,
        )
    with pytest.raises(SealedProductionFactRegistryErrorV2, match="reader-owned"):
        registry.prepare_from_pinned_audit(
            proposal_reader=proposal_reader,
            proposal_handle=PinnedFactCommitProposalAuthorityHandleV2(),
            change_id=change_id,
            policy=policy,
            world_id=WORLD_ID,
        )
    ledger.close()

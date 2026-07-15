from __future__ import annotations

import copy
import pickle
from datetime import UTC, datetime

import pytest

from companion_daemon.world_v2.event_identity import domain_idempotency_key
from companion_daemon.world_v2.fact_accepted_contracts import (
    rehydrate_fact_commit_intent_v2_json,
)
from companion_daemon.world_v2.fact_proof_backed_evidence import (
    ProofBackedFactEvidenceResolverV2,
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
    PreparedFactCommitMaterializationV2,
    SealedProductionFactPreparationRegistryV2,
    SealedProductionFactRegistryErrorV2,
    sealed_fact_commit_install_descriptor_v2,
)
from companion_daemon.world_v2.sqlite_ledger import (
    SQLiteProofBackedObservationReader,
    SQLiteWorldLedger,
)


WORLD_ID = "world:sealed-preparation"
NOW = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)


def _message() -> WorldEvent:
    payload = {
        "schema_version": "world-v2.1",
        "observation_kind": "message",
        "observation_id": "observation:message:1",
        "world_id": WORLD_ID,
        "logical_time": NOW.isoformat(),
        "created_at": NOW.isoformat(),
        "trace_id": "trace:prepared-fact",
        "causation_id": "cause:prepared-fact",
        "correlation_id": "correlation:prepared-fact",
        "source": "test",
        "source_event_id": "source:prepared-fact",
        "actor": "user:primary",
        "channel": "chat",
        "payload_ref": "payload:message:1",
        "payload_hash": "c" * 64,
        "received_at": NOW.isoformat(),
    }
    return WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id="event:prepared-fact",
        world_id=WORLD_ID,
        event_type="ObservationRecorded",
        logical_time=NOW,
        created_at=NOW,
        actor="user:primary",
        source="test",
        trace_id="trace:prepared-fact",
        causation_id="cause:prepared-fact",
        correlation_id="correlation:prepared-fact",
        idempotency_key=domain_idempotency_key(
            event_type="ObservationRecorded", world_id=WORLD_ID, payload=payload
        ) or "unreachable",
        payload=payload,
    )


def _bound(tmp_path):
    ledger = SQLiteWorldLedger(path=tmp_path / "sealed-preparation.sqlite3", world_id=WORLD_ID)
    event = _message()
    ledger.commit((event,), expected_world_revision=0, expected_deliberation_revision=0)
    proposal = normalize_fact_commit_proposal_v2(
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
                "brief_rationale": "the observation explicitly supplies a display name",
            },
            strict=True,
        ),
        context=FactCommitProposalNormalizationContextV2.model_validate(
            {
                "world_id": WORLD_ID,
                "proposal_id": "proposal:prepared-fact:1",
                "trigger_ref": "observation:message:1",
                "evaluated_world_revision": 1,
                "evidence_refs": (
                    {
                        "ref_id": "observation:message:1",
                        "evidence_kind": "observed_message",
                        "source_world_revision": 1,
                        "immutable_hash": "sha256:" + event.payload_hash,
                    },
                ),
                "policy_refs": ("policy:fact-commit.2",),
            },
            strict=True,
        ),
    )
    reader = SQLiteProofBackedObservationReader(ledger=ledger)
    resolver = ProofBackedFactEvidenceResolverV2(reader=reader)
    cursor = ProjectionCursor(world_revision=1, deliberation_revision=0, ledger_sequence=1)
    intent = rehydrate_fact_commit_intent_v2_json(proposal.proposed_changes[0].payload.canonical_json)
    sources = resolver.resolve(
        handle=reader.pin(world_id=WORLD_ID, cursor=cursor),
        intent=intent,
        locators=(
            ObservationEventLocator.for_message(
                world_id=WORLD_ID,
                observation_id="observation:message:1",
                source="test",
                source_event_id="source:prepared-fact",
            ),
        ),
    )
    registry = SealedProductionFactPreparationRegistryV2(resolver=resolver)
    prepared = registry.prepare(
        proposal=proposal,
        change=proposal.proposed_changes[0],
        policy=FactCommitPolicyResolutionV2(
            cardinality="single", policy_refs=("policy:fact-commit.2",)
        ),
        world_id=WORLD_ID,
    )
    return ledger, registry, prepared, sources, proposal, resolver


def test_sealed_descriptor_is_fixed_and_caller_mutation_cannot_change_next_read() -> None:
    first = sealed_fact_commit_install_descriptor_v2()
    changed = first.model_copy(update={"compiler_digest": "0" * 64})
    second = sealed_fact_commit_install_descriptor_v2()

    assert first.event_types == ("FactCommitted",)
    assert first.compiler_key.payload_schema == "fact_commit_intent.v2"
    assert changed.compiler_digest != second.compiler_digest
    assert second == first


def test_registry_prepares_and_reverse_verifies_inert_fact_bytes(tmp_path) -> None:
    ledger, registry, prepared, sources, _, _ = _bound(tmp_path)
    try:
        payload = registry.compile(
            prepared=prepared, acceptance_id="acceptance:prepared:1", sources=sources
        )
        assert payload.payload_contract == "fact-commit-materialized.2"
        assert registry.reverse_verify(
            prepared=prepared,
            acceptance_id="acceptance:prepared:1",
            sources=sources,
            payload=payload,
        ) == payload
        assert registry.owns_preparation(prepared)
        assert not hasattr(prepared, "to_world_event")
        assert not hasattr(registry, "register")
        assert not hasattr(registry, "compile_authority")
    finally:
        ledger.close()


def test_counterfeit_or_foreign_preparation_is_rejected(tmp_path) -> None:
    ledger, registry, prepared, sources, proposal, resolver = _bound(tmp_path)
    try:
        assert registry.owns_preparation(prepared)
        blank = PreparedFactCommitMaterializationV2()
        assert not registry.owns_preparation(blank)
        with pytest.raises(SealedProductionFactRegistryErrorV2, match="another registry"):
            registry.compile(
                prepared=blank, acceptance_id="acceptance:prepared:1", sources=sources
            )
        other = SealedProductionFactPreparationRegistryV2(resolver=resolver)
        assert not other.owns_preparation(prepared)
        with pytest.raises(SealedProductionFactRegistryErrorV2, match="another registry"):
            other.compile(
                prepared=prepared, acceptance_id="acceptance:prepared:1", sources=sources
            )
        with pytest.raises(TypeError, match="exact proof-backed resolver"):
            SealedProductionFactPreparationRegistryV2(
                resolver=object()  # type: ignore[arg-type]
            )
    finally:
        ledger.close()


def test_prepared_capability_cannot_be_copied_or_serialized(tmp_path) -> None:
    ledger, _registry, prepared, _sources, _proposal, _resolver = _bound(tmp_path)
    try:
        for operation in (copy.copy, copy.deepcopy, pickle.dumps):
            with pytest.raises(TypeError):
                operation(prepared)
    finally:
        ledger.close()


def test_sealed_registry_rejects_uninstalled_policy_refs(tmp_path) -> None:
    ledger, registry, _prepared, _sources, proposal, _resolver = _bound(tmp_path)
    try:
        with pytest.raises(SealedProductionFactRegistryErrorV2, match="not admitted"):
            registry.prepare(
                proposal=proposal,
                change=proposal.proposed_changes[0],
                policy=FactCommitPolicyResolutionV2(
                    cardinality="single", policy_refs=("policy:other.2",)
                ),
                world_id=WORLD_ID,
            )
    finally:
        ledger.close()

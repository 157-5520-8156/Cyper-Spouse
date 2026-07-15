from __future__ import annotations

import pytest

from companion_daemon.world_v2.acceptance_manifest import (
    AcceptanceManifestError,
    canonical_acceptance_manifest_hash,
)
from companion_daemon.world_v2.event_catalog import (
    event_contract,
    event_contracts,
)
from companion_daemon.world_v2.reducers import event_definition, event_types


def test_catalog_covers_every_reducer_event_with_stable_revision_metadata() -> None:
    contracts = event_contracts()

    assert frozenset(contracts) == event_types()
    for event_type, contract in contracts.items():
        reducer_definition = event_definition(event_type)
        assert contract.revision_class == reducer_definition.revision_class.value
        assert contract.event_type == event_type
        assert contract.producer
        assert contract.payload_contract
        assert contract.schema_version == "world-v2.1"
        assert contract.reducer_bundle == "world-v2-reducers.25"
        assert contract.upcaster == "world-v2-upcasters.1"
        assert contract.idempotency_identity
        schema = contract.json_schema()
        assert schema.get("required", []) == list(contract.required_fields)
        metadata = schema["x-world-event"]
        assert isinstance(metadata, dict)
        assert metadata["event_type"] == event_type


def test_action_delivery_contract_exposes_lineage_and_compensation() -> None:
    contract = event_contract("ActionDelivered")

    assert contract.allowed_predecessors == (
        "ActionDispatchStarted",
        "ActionDispatchPending",
        "ActionProviderAccepted",
    )
    assert contract.evidence_types == ("provider_receipt", "execution_receipt")
    assert contract.successors == ("BudgetSettled", "TriggerProcessCompleted")
    assert contract.compensations == ("ActionReconciliationRequired",)
    assert contract.idempotency_identity == "provider+source_event_id+delivered"


def test_action_reclaim_contract_preserves_claim_lineage() -> None:
    contract = event_contract("ActionReclaimed")

    assert contract.allowed_predecessors == ("ActionClaimed", "ActionReclaimed")
    assert contract.evidence_types == ("expired_claim_lease",)
    assert contract.successors == (
        "ActionDispatchStarted",
        "ActionCancelled",
        "ActionExpired",
    )
    contracts = event_contracts()
    for successor in contract.successors:
        assert "ActionReclaimed" in contracts[successor].allowed_predecessors


def test_audit_event_does_not_claim_world_revision_or_domain_evidence() -> None:
    contract = event_contract("ProposalRecorded")

    assert contract.revision_class == "deliberation"
    assert contract.evidence_types == ("model_result", "context_capsule")
    assert contract.compensations == ()


def test_acceptance_catalog_fails_closed_for_valid_accepted_manifest_shape() -> None:
    payload: dict[str, object] = {
        "manifest_version": "acceptance-manifest.2",
        "acceptance_id": "acceptance:catalog:accepted",
        "status": "accepted",
        "evaluated_world_revision": 0,
        "proposals": (
            {
                "proposal_id": "proposal:catalog:1",
                "proposal_kind": "decision",
                "audit_contract": "proposal-envelope-audit.1",
                "proposal_event_ref": "event:proposal:catalog:1",
                "proposal_event_payload_hash": "a" * 64,
                "proposal_hash": "sha256:" + "b" * 64,
                "evaluated_world_revision": 0,
                "changes": (
                    {
                        "change_id": "change:catalog:1",
                        "kind": "fact_transition",
                        "target_id": "fact:catalog:1",
                        "transition": "commit",
                        "expected_entity_revision": 0,
                        "evidence_refs": (),
                        "preconditions": (),
                        "policy_refs": (),
                        "payload_schema": "fact_transition.v1",
                        "payload_hash": "sha256:" + "e" * 64,
                        "full_change_authority_hash": "d" * 64,
                    },
                ),
                "action_intents": (),
            },
        ),
        "authorized_effects": (
            {
                "ordinal": 0,
                "role": "domain_mutation",
                "event_id": "event:effect:catalog:1",
                "event_type": "FactCommitted",
                "payload_hash": "c" * 64,
                "authority_refs": (
                    {
                        "proposal_id": "proposal:catalog:1",
                        "authority_kind": "change",
                        "authority_id": "change:catalog:1",
                        "authority_hash": "d" * 64,
                    },
                ),
            },
        ),
    }
    payload["manifest_hash"] = canonical_acceptance_manifest_hash(payload)

    with pytest.raises(AcceptanceManifestError, match="accepted_not_enabled"):
        event_contract("AcceptanceRecorded").validate_payload(payload)


def test_affect_baseline_catalog_matches_installed_evidence_resolvers() -> None:
    contract = event_contract("AffectBaselineAdjusted")

    assert contract.evidence_types == (
        "observed_message",
        "committed_world_event",
        "committed_experience",
        "settled_world_event",
        "settled_external_result",
        "active_plan",
        "operator_observation",
        "clock_observation",
    )


def test_relationship_catalog_matches_resolvers_and_never_implies_acceptance_bypass() -> None:
    expected_evidence = (
        "observed_message",
        "committed_world_event",
        "committed_experience",
        "settled_world_event",
        "settled_external_result",
        "active_plan",
        "operator_observation",
    )
    signal = event_contract("RelationshipSignalAccepted")
    adjustment = event_contract("RelationshipSlowVariableAdjusted")
    boundary = event_contract("BoundaryChanged")

    assert signal.evidence_types == expected_evidence
    assert adjustment.evidence_types == expected_evidence
    assert boundary.evidence_types == expected_evidence
    assert "clock_observation" not in expected_evidence
    assert "committed_fact" not in expected_evidence
    assert signal.successors == ()
    assert adjustment.allowed_predecessors == ("AcceptanceRecorded",)
    assert boundary.allowed_predecessors == ("AcceptanceRecorded",)


def test_machine_payload_contract_rejects_missing_required_fields() -> None:
    contract = event_contract("ActionDispatchStarted")

    with pytest.raises(ValueError, match="started_at"):
        contract.validate_payload(
            {
                "action_id": "action-1",
                "owner_id": "pump-1",
                "attempt_id": "attempt-1",
            }
        )
    with pytest.raises(ValueError, match="string_too_short"):
        contract.validate_payload(
            {
                "action_id": "",
                "owner_id": "pump-1",
                "attempt_id": "attempt-1",
                "started_at": "2026-07-14T12:00:00Z",
            }
        )
    with pytest.raises(ValueError, match="extra_forbidden"):
        contract.validate_payload(
            {
                "action_id": "action-1",
                "owner_id": "pump-1",
                "attempt_id": "attempt-1",
                "started_at": "2026-07-14T12:00:00Z",
                "conflicting_owner": "pump-2",
            }
        )
    schema = contract.json_schema()
    assert schema["additionalProperties"] is False
    assert schema["properties"]["action_id"]["minLength"] == 1


def test_observation_contract_freezes_source_event_identity() -> None:
    contract = event_contract("ObservationRecorded")

    assert contract.idempotency_identity == "source+source_event_id"


def test_character_core_catalog_closes_revision_and_compensation_lifecycle() -> None:
    initialized = event_contract("CharacterCoreInitialized")
    revised = event_contract("CharacterCoreRevised")
    compensated = event_contract("CharacterCoreRevisionCompensated")

    assert initialized.allowed_predecessors == ("AcceptanceRecorded",)
    assert initialized.successors == ("CharacterCoreRevised",)
    assert revised.successors == (
        "CharacterCoreRevised",
        "CharacterCoreRevisionCompensated",
    )
    assert revised.compensations == ("CharacterCoreRevisionCompensated",)
    assert compensated.successors == (
        "CharacterCoreRevised",
        "CharacterCoreRevisionCompensated",
    )
    assert compensated.compensations == ("CharacterCoreRevisionCompensated",)
    assert "CharacterCoreRevisionCompensated" in event_contracts()

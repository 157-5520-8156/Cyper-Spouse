from __future__ import annotations

import pytest

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
        assert contract.reducer_bundle == "world-v2-reducers.5"
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

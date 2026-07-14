from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from companion_daemon.world_v2.batch_invariants import validate_commit_batch
from companion_daemon.world_v2.life_events import outcome_mutation_hash
from companion_daemon.world_v2.schemas import WorldEvent
from companion_daemon.world_v2.typed_proposal_families import (
    INSTALLED_TYPED_PROPOSAL_FAMILIES,
    family_for_mutation,
    family_for_record,
    validate_typed_proposal_family_manifest,
)
from companion_daemon.world_v2.typed_proposals import (
    DuplicateTypedProposalRegistration,
    RecordSelector,
    UnknownTypedProposalContract,
)


def test_installed_family_manifest_owns_all_current_typed_proposals() -> None:
    assert tuple(item.contract_ref for item in INSTALLED_TYPED_PROPOSAL_FAMILIES) == (
        "proposal-contract:affect-legacy.1",
        "proposal-contract:appraisal-legacy.1",
        "proposal-contract:outcome-legacy.1",
        "proposal-contract:relationship.1",
        "proposal-contract:thread.1",
    )
    assert family_for_mutation("AffectEpisodeOpened").contract_ref == (
        "proposal-contract:affect-legacy.1"
    )
    assert family_for_mutation("AffectEpisodeDecayed") is None
    assert family_for_mutation("BoundaryChanged").contract_ref == (
        "proposal-contract:relationship.1"
    )


def test_record_routing_preserves_legacy_and_generic_boundaries() -> None:
    assert family_for_record(
        "ProposalRecorded",
        {"proposal_kind": "appraisal_transition"},
    ).contract_ref == "proposal-contract:appraisal-legacy.1"
    assert family_for_record(
        "ProposalRecorded",
        {"proposal_kind": "future_transition"},
    ) is None
    assert family_for_record(
        "ProposalRecorded",
        {
            "proposal_kind": "relationship_transition",
            "proposal_encoding": "typed-authority-v1",
            "authority_contract_ref": "proposal-contract:relationship.1",
        },
    ).contract_ref == "proposal-contract:relationship.1"

    with pytest.raises(UnknownTypedProposalContract):
        family_for_record(
            "ProposalRecorded",
            {
                "proposal_kind": "future_transition",
                "proposal_encoding": "typed-authority-v1",
                "authority_contract_ref": "proposal-contract:future.1",
            },
        )


@pytest.mark.parametrize(
    ("duplicate", "error_fragment"),
    (
        ({"contract_ref": INSTALLED_TYPED_PROPOSAL_FAMILIES[0].contract_ref}, "contract"),
            (
                {
                    "contract_ref": "proposal-contract:collision.1",
                    "selector": INSTALLED_TYPED_PROPOSAL_FAMILIES[0].selector,
                },
                "record selector",
            ),
            (
                {
                    "contract_ref": "proposal-contract:collision.1",
                    "selector": RecordSelector("ProposalRecorded", "other_transition"),
                "mutation_event_types": INSTALLED_TYPED_PROPOSAL_FAMILIES[
                    0
                ].mutation_event_types,
            },
            "mutation owner",
        ),
    ),
)
def test_family_manifest_rejects_ambiguous_ownership(
    duplicate: dict[str, object], error_fragment: str
) -> None:
    original = INSTALLED_TYPED_PROPOSAL_FAMILIES[0]
    collision = replace(original, **duplicate)

    with pytest.raises(DuplicateTypedProposalRegistration, match=error_fragment):
        validate_typed_proposal_family_manifest((original, collision))


def test_outcome_proposal_keeps_its_legacy_mixed_commit_semantics() -> None:
    now = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)
    change_hash = outcome_mutation_hash(
        change_id="change:outcome",
        occurrence_id="occurrence:1",
        evaluated_entity_revision=1,
        evaluated_world_revision=3,
        candidate_result_ref="candidate:1",
        result_id="result:1",
        result_payload_ref="payload:1",
        result_payload_hash="hash:1",
        observation_refs=("observation:1",),
    )
    proposal = WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id="event:outcome-proposal",
        world_id="world:1",
        event_type="OutcomeProposalRecorded",
        logical_time=now,
        created_at=now,
        actor="system:test",
        source="test",
        trace_id="trace:1",
        causation_id="cause:1",
        correlation_id="correlation:1",
        idempotency_key="identity:outcome-proposal",
        payload={
            "outcome_proposal_id": "proposal:outcome:1",
            "decision_proposal_id": "decision:outcome:1",
            "change_id": "change:outcome",
            "occurrence_id": "occurrence:1",
            "evaluated_entity_revision": 1,
            "evaluated_world_revision": 3,
            "trigger_ref": "trigger:1",
            "candidate_result_ref": "candidate:1",
            "proposed_result_id": "result:1",
            "proposed_result_payload_ref": "payload:1",
            "proposed_result_payload_hash": "hash:1",
            "proposed_change_hash": change_hash,
            "observation_refs": ["observation:1"],
            "evidence_refs": [
                {
                    "ref_id": "operator:1",
                    "evidence_type": "operator_observation",
                    "claim_purpose": "current_fact",
                    "immutable_hash": "a" * 64,
                }
            ],
            "confidence_bp": 8_000,
            "expires_at": (now + timedelta(minutes=5)).isoformat(),
        },
    )
    related_audit = WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id="event:related-audit",
        world_id="world:1",
        event_type="OutcomeObservationRecorded",
        logical_time=now,
        created_at=now,
        actor="system:test",
        source="test",
        trace_id="trace:1",
        causation_id="cause:1",
        correlation_id="correlation:1",
        idempotency_key="identity:related-audit",
        payload={"observation": {"observation_id": "observation:1"}},
    )

    validate_commit_batch((related_audit, proposal), expected_world_revision=3)


@pytest.mark.parametrize(
    ("event_type", "payload", "expected"),
    (
        (
            "AppraisalAccepted",
            {"appraisal": {"appraisal_id": "appraisal:1"}, "transition_id": "transition:1"},
            ("world:1", "appraisal:1", "transition:1"),
        ),
        (
            "AppraisalContradicted",
            {"appraisal_id": "appraisal:1", "transition_id": "transition:2"},
            ("appraisal:1", "transition:2"),
        ),
        (
            "AppraisalSuperseded",
            {"appraisal_id": "appraisal:1", "transition_id": "transition:3"},
            ("appraisal:1", "transition:3"),
        ),
        (
            "AffectEpisodeOpened",
            {"episode": {"episode_id": "affect:1"}, "transition_id": "transition:1"},
            ("world:1", "affect:1", "transition:1"),
        ),
        (
            "AffectEpisodeUpdated",
            {"episode_id": "affect:1", "transition_id": "transition:2"},
            ("affect:1", "transition:2"),
        ),
        (
            "AffectEpisodeResolved",
            {"episode_id": "affect:1", "transition_id": "transition:3"},
            ("affect:1", "transition:3"),
        ),
        (
            "AffectEpisodeSuperseded",
            {
                "episode_id": "affect:1",
                "successor": {"episode_id": "affect:2"},
                "transition_id": "transition:4",
            },
            ("affect:1", "affect:2", "transition:4"),
        ),
        (
            "AffectBaselineAdjusted",
            {
                "dimension": "valence",
                "expected_entity_revision": 2,
                "transition_id": "transition:5",
            },
            ("world:1", "valence", 2, "transition:5"),
        ),
        (
            "WorldOccurrenceSettled",
            {"occurrence_id": "occurrence:1", "result_id": "result:1", "expected_entity_revision": 3},
            ("occurrence:1", "result:1", 3),
        ),
        (
            "RelationshipSignalAccepted",
            {"signal": {"semantic_fingerprint": "signal:fingerprint"}},
            ("world:1", "signal:fingerprint"),
        ),
        (
            "RelationshipSlowVariableAdjusted",
            {
                "relationship_id": "relationship:1",
                "expected_entity_revision": 4,
                "adjustment_id": "adjustment:1",
            },
            ("relationship:1", 4, "adjustment:1"),
        ),
        (
            "BoundaryChanged",
            {
                "boundary": {"boundary_id": "boundary:1"},
                "expected_entity_revision": 5,
                "transition_id": "transition:6",
            },
            ("boundary:1", 5, "transition:6"),
        ),
    ),
)
def test_family_mutation_identity_components_are_unchanged(
    event_type: str,
    payload: dict[str, object],
    expected: tuple[object, ...],
) -> None:
    family = family_for_mutation(event_type)
    assert family is not None
    assert family.codec.mutation_identity(
        world_id="world:1", event_type=event_type, payload=payload
    ) == expected


def test_family_record_identity_components_are_unchanged() -> None:
    appraisal = family_for_record(
        "ProposalRecorded",
        {"proposal_kind": "appraisal_transition"},
    )
    affect = family_for_record(
        "ProposalRecorded",
        {"proposal_kind": "affect_transition"},
    )
    outcome = family_for_record("OutcomeProposalRecorded", {})
    relationship_payload = {
        "proposal_kind": "relationship_transition",
        "proposal_encoding": "typed-authority-v1",
        "authority_contract_ref": "proposal-contract:relationship.1",
        "proposal_id": "proposal:relationship:1",
        "change_id": "change:relationship:1",
    }
    relationship = family_for_record("ProposalRecorded", relationship_payload)
    assert appraisal is not None and affect is not None and outcome is not None
    assert relationship is not None

    assert appraisal.codec.record_identity(
        world_id="world:1",
        event_type="ProposalRecorded",
        payload={"proposal_id": "proposal:appraisal:1", "change_id": "change:appraisal:1"},
    ) == ("world:1", "proposal:appraisal:1", "change:appraisal:1")
    assert affect.codec.record_identity(
        world_id="world:1",
        event_type="ProposalRecorded",
        payload={"proposal_id": "proposal:affect:1", "change_id": "change:affect:1"},
    ) is None
    assert outcome.codec.record_identity(
        world_id="world:1",
        event_type="OutcomeProposalRecorded",
        payload={"outcome_proposal_id": "proposal:outcome:1"},
    ) == ("world:1", "proposal:outcome:1")
    assert relationship.codec.record_identity(
        world_id="world:1",
        event_type="ProposalRecorded",
        payload=relationship_payload,
    ) == (
        "world:1",
        "proposal:relationship:1",
        "change:relationship:1",
        "proposal-contract:relationship.1",
    )

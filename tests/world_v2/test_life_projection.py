from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import json
from pathlib import Path
import sqlite3

import pytest

from legacy_migration_support import strip_v16_state_fields

from companion_daemon.world_v2.batch_invariants import appraisal_trigger_identity
from companion_daemon.world_v2.appraisal_events import appraisal_mutation_hash
from companion_daemon.world_v2.event_identity import domain_idempotency_key
from companion_daemon.world_v2.errors import IdempotencyConflict
from companion_daemon.world_v2.errors import LedgerIntegrityError
from companion_daemon.world_v2.ledger import LedgerPort, WorldLedger
from companion_daemon.world_v2.life_events import outcome_mutation_hash
from companion_daemon.world_v2.experience_events import (
    ExperienceCommittedPayload,
    experience_mutation_hash,
)
from companion_daemon.world_v2.projection import InternalProjectionReader
from companion_daemon.world_v2.reducers import ReducerState, reduce_event
from companion_daemon.world_v2.schemas import (
    AppraisalHypothesis,
    AppraisalOrigin,
    AppraisalProjection,
    ClaimLease,
    CommittedWorldEventRef,
    DueWindow,
    EvidenceRef,
    ExperienceOccurrenceSettlementBinding,
    ExperienceOrigin,
    ExperienceProjection,
    ExperienceProposalProjection,
    ExperienceProposedMutation,
    ExperienceValues,
    NpcProjection,
    OutcomeObservationProjection,
    OutcomeProposalProjection,
    PlanAuthorityOrigin,
    PlanStateProjection,
    ProjectionCursor,
    ProposalRevisionRef,
    RelationshipProposalProjection,
    RelationshipProposedMutation,
    TriggerProcess,
    WorldEvent,
    WorldOccurrenceProjection,
    experience_semantic_fingerprint,
    plan_authority_binding_hash,
    plan_authority_projection_hash,
    validate_plan_authority_state,
)
from companion_daemon.world_v2.sqlite_ledger import SQLiteWorldLedger
from companion_daemon.world_v2.typed_proposals import AmbiguousTypedProposalAuthority


WORLD_ID = "world-v2-life-test"
NOW = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)
LIFE_TIME = NOW + timedelta(minutes=5)
OPERATOR_HASH = "b" * 64


def evidence(
    ref_id: str,
    evidence_type: str,
    claim_purpose: str,
) -> dict[str, object]:
    return EvidenceRef(
        ref_id=ref_id,
        evidence_type=evidence_type,
        claim_purpose=claim_purpose,
        immutable_hash=(OPERATOR_HASH if evidence_type == "operator_observation" else None),
    ).model_dump(mode="json")


def model_hash(value) -> str:
    return hashlib.sha256(
        json.dumps(
            value.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def register_operator_observations(ledger: LedgerPort, *refs: str) -> None:
    commit(
        ledger,
        [
            event(
                f"operator-observation:{ref}",
                "OperatorObservationRecorded",
                {"observation_id": ref, "observation_hash": OPERATOR_HASH},
            )
            for ref in refs
        ],
    )


def world_evidence(ledger: LedgerPort, event_id: str, claim_purpose: str) -> dict[str, object]:
    committed = next(
        ref for ref in ledger.project().committed_world_event_refs if ref.event_id == event_id
    )
    return EvidenceRef(
        ref_id=event_id,
        evidence_type="committed_world_event",
        claim_purpose=claim_purpose,
        source_world_revision=committed.world_revision,
        immutable_hash=committed.payload_hash,
    ).model_dump(mode="json")


def event(
    event_id: str,
    event_type: str,
    payload: dict[str, object],
    *,
    at: datetime = LIFE_TIME,
) -> WorldEvent:
    idempotency_key = domain_idempotency_key(
        event_type=event_type,
        world_id=WORLD_ID,
        payload=payload,
    )
    return WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id=event_id,
        world_id=WORLD_ID,
        event_type=event_type,
        logical_time=at,
        created_at=at,
        actor="system:life-test",
        source="life-test",
        trace_id="trace:life",
        causation_id=f"cause:{event_id}",
        correlation_id="correlation:life",
        idempotency_key=idempotency_key or f"identity:{event_id}",
        payload=payload,
    )


def mutation(
    event_id: str,
    *,
    expected_revision: int,
    evidence_refs: list[dict[str, object]],
) -> dict[str, object]:
    return {
        "change_id": f"change:{event_id}",
        "transition_id": f"transition:{event_id}",
        "expected_entity_revision": expected_revision,
        "evidence_refs": evidence_refs,
        "policy_refs": ["policy:life-v1"],
    }


def commit(
    ledger: LedgerPort,
    events: list[WorldEvent],
) -> None:
    projection = ledger.project()
    ledger.commit(
        events,
        expected_world_revision=projection.world_revision,
        expected_deliberation_revision=projection.deliberation_revision,
    )


def advance_life_clock(ledger: LedgerPort) -> None:
    commit(
        ledger,
        [
            event(
                "clock-life",
                "ClockAdvanced",
                {
                    "logical_time_from": (NOW - timedelta(seconds=1)).isoformat(),
                    "logical_time_to": LIFE_TIME.isoformat(),
                },
            )
        ],
    )


def seed_through_proposal(ledger: LedgerPort) -> str:
    advance_life_clock(ledger)
    commit(
        ledger,
        [
            event(
                "message-plan-tea",
                "ObservationRecorded",
                {
                    "schema_version": "world-v2.1",
                    "observation_kind": "message",
                    "observation_id": "message:plan-tea",
                    "world_id": WORLD_ID,
                    "logical_time": LIFE_TIME.isoformat(),
                    "created_at": LIFE_TIME.isoformat(),
                    "trace_id": "trace:life",
                    "causation_id": "cause:message-plan-tea",
                    "correlation_id": "correlation:life",
                    "source": "life-test",
                    "source_event_id": "source:message-plan-tea",
                    "actor": "system:life-test",
                    "channel": "direct_message",
                    "payload_ref": "payload:message-plan-tea",
                    "payload_hash": "c" * 64,
                    "received_at": LIFE_TIME.isoformat(),
                },
            )
        ],
    )
    register_operator_observations(
        ledger,
        "operator:npc-lin",
        "operator:tea-good",
    )
    npc = NpcProjection(
        npc_id="lin",
        entity_revision=1,
        stable_identity_ref="identity:npc:lin",
        known_trait_refs=("trait:quiet",),
        privacy_class="private",
    )
    commit(
        ledger,
        [
            event(
                "npc-registered",
                "NpcRegistered",
                {
                    **mutation(
                        "npc-registered",
                        expected_revision=0,
                        evidence_refs=[
                            evidence(
                                "operator:npc-lin",
                                "operator_observation",
                                "current_fact",
                            )
                        ],
                    ),
                    "npc": npc.model_dump(mode="json"),
                },
            )
        ],
    )

    message = ledger.project().message_observations[0]
    plan_evidence = EvidenceRef(
        ref_id="message:plan-tea",
        evidence_type="observed_message",
        claim_purpose="future_plan",
        source_world_revision=message.world_revision,
        immutable_hash=message.event_payload_hash,
    )
    plan = PlanStateProjection(
        plan_id="plan-tea",
        activity_id="activity-tea",
        entity_revision=1,
        activity_kind="make_tea",
        evidence_refs=(plan_evidence,),
        status="planned",
        importance_bp=4_000,
        scheduled_window=DueWindow(
            opens_at=NOW + timedelta(minutes=1),
            closes_at=NOW + timedelta(minutes=30),
        ),
        participant_refs=("npc:lin",),
        location_ref="room:kitchen",
        owner_actor_ref="actor:companion",
    )
    commit(
        ledger,
        [
            event(
                "activity-planned",
                "ActivityPlanned",
                {
                    **mutation(
                        "activity-planned",
                        expected_revision=0,
                        evidence_refs=[plan_evidence.model_dump(mode="json")],
                    ),
                    "plan": plan.model_dump(mode="json"),
                },
            )
        ],
    )

    occurrence_evidence = EvidenceRef(
        ref_id="plan-tea",
        evidence_type="active_plan",
        claim_purpose="future_plan",
        immutable_hash=model_hash(ledger.project().plans[0]),
    ).model_dump(mode="json")
    occurrence = WorldOccurrenceProjection(
        occurrence_id="occurrence-tea",
        entity_revision=1,
        trigger_ref="trigger:tea-time",
        participant_refs=("npc:lin",),
        location_ref="room:kitchen",
        time_window=DueWindow(
            opens_at=NOW + timedelta(minutes=1),
            closes_at=NOW + timedelta(minutes=10),
        ),
        precondition_refs=("plan:plan-tea",),
        candidate_outcome_refs=("result:tea-good", "result:tea-spilled"),
        visibility="private",
        status="committed",
    )
    commit(
        ledger,
        [
            event(
                "occurrence-committed",
                "WorldOccurrenceCommitted",
                {
                    **mutation(
                        "occurrence-committed",
                        expected_revision=0,
                        evidence_refs=[occurrence_evidence],
                    ),
                    "occurrence": occurrence.model_dump(mode="json"),
                },
            )
        ],
    )

    commit(
        ledger,
        [
            event(
                "occurrence-activated",
                "WorldOccurrenceActivated",
                {
                    **mutation(
                        "occurrence-activated",
                        expected_revision=1,
                        evidence_refs=[occurrence_evidence],
                    ),
                    "occurrence_id": "occurrence-tea",
                    "activated_at": (NOW + timedelta(minutes=2)).isoformat(),
                    "satisfied_precondition_refs": ["plan:plan-tea"],
                },
                at=LIFE_TIME,
            )
        ],
    )

    observation = OutcomeObservationProjection(
        observation_id="observation-tea",
        occurrence_id="occurrence-tea",
        source_kind="committed_world_event",
        source_refs=("occurrence-activated",),
        observed_payload_ref="payload:tea-brewed",
        observed_payload_hash="sha256:tea-brewed",
        observed_at=NOW + timedelta(minutes=3),
        confidence_bp=9_000,
    )
    observation_evidence = world_evidence(ledger, "occurrence-activated", "current_fact")
    commit(
        ledger,
        [
            event(
                "outcome-observed",
                "OutcomeObservationRecorded",
                {
                    **mutation(
                        "outcome-observed",
                        expected_revision=2,
                        evidence_refs=[observation_evidence],
                    ),
                    "observation": observation.model_dump(mode="json"),
                },
                at=LIFE_TIME,
            )
        ],
    )

    semantic_hash_before_proposal = ledger.project().semantic_hash
    proposed_change_hash = outcome_mutation_hash(
        change_id="change:outcome-proposed",
        occurrence_id="occurrence-tea",
        evaluated_entity_revision=3,
        evaluated_world_revision=7,
        candidate_result_ref="result:tea-good",
        result_id="result-tea-good",
        result_payload_ref="payload:tea-good",
        result_payload_hash="sha256:tea-good",
        observation_refs=("observation-tea",),
    )
    commit(
        ledger,
        [
            event(
                "outcome-proposed",
                "OutcomeProposalRecorded",
                {
                    "outcome_proposal_id": "outcome-proposal-tea",
                    "decision_proposal_id": "decision-proposal-tea",
                    "change_id": "change:outcome-proposed",
                    "occurrence_id": "occurrence-tea",
                    "evaluated_entity_revision": 3,
                    "evaluated_world_revision": 7,
                    "trigger_ref": "trigger:tea-time",
                    "candidate_result_ref": "result:tea-good",
                    "proposed_result_id": "result-tea-good",
                    "proposed_result_payload_ref": "payload:tea-good",
                    "proposed_result_payload_hash": "sha256:tea-good",
                    "proposed_change_hash": proposed_change_hash,
                    "observation_refs": ["observation-tea"],
                    "precondition_refs": ["plan:plan-tea"],
                    "evidence_refs": [observation_evidence],
                    "confidence_bp": 8_500,
                    "expires_at": (NOW + timedelta(minutes=8)).isoformat(),
                },
                at=LIFE_TIME,
            )
        ],
    )
    commit(ledger, [experience_proposal_event()])
    assert ledger.project().semantic_hash == semantic_hash_before_proposal
    return semantic_hash_before_proposal


def test_rejected_outcome_proposal_remains_as_deliberation_audit() -> None:
    ledger = WorldLedger.in_memory(world_id=WORLD_ID)
    seed_through_proposal(ledger)

    commit(
        ledger,
        [
            event(
                "outcome-rejected",
                "AcceptanceRecorded",
                {
                    "status": "rejected",
                    "acceptance_id": "acceptance:outcome-proposal-tea:rejected",
                    "proposal_id": "outcome-proposal-tea",
                    "evaluated_world_revision": 7,
                },
            )
        ],
    )

    projection = ledger.project()
    assert projection.acceptance_decisions[-1].status == "rejected"
    assert tuple(item.outcome_proposal_id for item in projection.outcome_proposals) == (
        "outcome-proposal-tea",
    )


def test_acceptance_fails_closed_when_legacy_and_registered_stores_claim_one_id() -> None:
    proposal_id = "proposal:authority-collision"
    relationship = RelationshipProposalProjection.model_construct(
        proposal_id=proposal_id,
        proposal_kind="relationship_transition",
        proposal_encoding="typed-authority-v1",
        authority_contract_ref="proposal-contract:relationship.1",
        change_id="change:relationship",
        evaluated_world_revision=0,
        expected_entity_revision=0,
        proposed_change_hash="a" * 64,
        proposed_mutation=RelationshipProposedMutation.model_construct(
            event_type="BoundaryChanged",
            payload_json="{}",
        ),
    )
    outcome = OutcomeProposalProjection.model_construct(
        outcome_proposal_id=proposal_id,
        change_id="change:outcome",
        evaluated_entity_revision=1,
        evaluated_world_revision=0,
        proposed_change_hash="b" * 64,
    )
    state = ReducerState.model_construct(
        relationship_proposals=(relationship,),
        outcome_proposals=(outcome,),
        proposal_ids=(proposal_id,),
        proposal_revisions=(
            ProposalRevisionRef(proposal_id=proposal_id, evaluated_world_revision=0),
        ),
    )

    with pytest.raises(AmbiguousTypedProposalAuthority, match="multiple typed stores"):
        reduce_event(
            state,
            event(
                "acceptance:authority-collision",
                "AcceptanceRecorded",
                {
                    "status": "accepted",
                    "acceptance_id": "acceptance:authority-collision",
                    "proposal_id": proposal_id,
                    "evaluated_world_revision": 0,
                    "accepted_change_id": "change:relationship",
                    "accepted_change_hash": "a" * 64,
                },
            ),
        )


def settlement_batch() -> list[WorldEvent]:
    settled_at = NOW + timedelta(minutes=5)
    settled_evidence = evidence("operator:tea-good", "operator_observation", "past_experience")
    accepted_change_hash = outcome_mutation_hash(
        change_id="change:outcome-proposed",
        occurrence_id="occurrence-tea",
        evaluated_entity_revision=3,
        evaluated_world_revision=7,
        candidate_result_ref="result:tea-good",
        result_id="result-tea-good",
        result_payload_ref="payload:tea-good",
        result_payload_hash="sha256:tea-good",
        observation_refs=("observation-tea",),
    )
    settlement_event = event(
        "occurrence-settled",
        "WorldOccurrenceSettled",
        {
            **mutation(
                "occurrence-settled",
                expected_revision=3,
                evidence_refs=[settled_evidence],
            ),
            "change_id": "change:outcome-proposed",
            "acceptance_id": "acceptance:outcome-proposal-tea",
            "evaluated_world_revision": 7,
            "accepted_change_hash": accepted_change_hash,
            "occurrence_id": "occurrence-tea",
            "outcome_proposal_id": "outcome-proposal-tea",
            "candidate_result_ref": "result:tea-good",
            "result_id": "result-tea-good",
            "observation_refs": ["observation-tea"],
            "result_payload_ref": "payload:tea-good",
            "result_payload_hash": "sha256:tea-good",
            "settled_at": settled_at.isoformat(),
            "appraisal_trigger_ref": appraisal_trigger_identity(
                "occurrence-tea", "result-tea-good"
            ),
        },
        at=settled_at,
    )
    source_binding = ExperienceOccurrenceSettlementBinding(
        authority_event_ref=settlement_event.event_id,
        authority_world_revision=9,
        authority_payload_hash=settlement_event.payload_hash,
        occurrence_id="occurrence-tea",
        occurrence_entity_revision=4,
        result_id="result-tea-good",
        result_payload_ref="payload:tea-good",
        result_payload_hash="sha256:tea-good",
    )
    values = ExperienceValues(
        summary_ref="summary:tea-good",
        summary_payload_hash="d" * 64,
        occurred_from=NOW + timedelta(minutes=2),
        occurred_to=settled_at,
        participant_refs=("npc:lin",),
        source_bindings=(source_binding,),
        privacy_class="private",
    )
    origin = ExperienceOrigin(
        change_id="change:experience-tea",
        transition_id="transition:experience-tea",
        policy_refs=("policy:experience-v1",),
        accepted_event_ref="experience-committed",
    )
    experience = ExperienceProjection(
        experience_id="experience-tea",
        entity_revision=1,
        authority_contract_version="experience.1",
        semantic_fingerprint=experience_semantic_fingerprint(
            values=values, policy_refs=origin.policy_refs
        ),
        values=values,
        origin=origin,
    )
    experience_raw = {
        "change_id": origin.change_id,
        "transition_id": origin.transition_id,
        "expected_entity_revision": 0,
        "evidence_refs": (
            EvidenceRef.model_validate_json(json.dumps(settled_evidence)),
        ),
        "policy_refs": origin.policy_refs,
        "acceptance_id": "acceptance:experience-tea",
        "proposal_id": "proposal:experience-tea",
        "evaluated_world_revision": 7,
        "accepted_change_hash": "0" * 64,
        "experience": experience,
    }
    experience_raw["accepted_change_hash"] = experience_mutation_hash(experience_raw)
    experience_payload = ExperienceCommittedPayload.model_validate(experience_raw)
    trigger = TriggerProcess(
        trigger_id=appraisal_trigger_identity("occurrence-tea", "result-tea-good"),
        trigger_ref=appraisal_trigger_identity("occurrence-tea", "result-tea-good"),
        process_kind="npc_world_appraisal",
        source_evidence_ref="occurrence-settled",
        state="open",
    )
    return [
        event(
            "outcome-accepted",
            "AcceptanceRecorded",
            {
                "status": "accepted",
                "acceptance_id": "acceptance:outcome-proposal-tea",
                "proposal_id": "outcome-proposal-tea",
                "evaluated_world_revision": 7,
                "acceptance_kind": "world_occurrence_settlement",
                "accepted_change_id": "change:outcome-proposed",
                "accepted_change_hash": accepted_change_hash,
            },
            at=settled_at,
        ),
        settlement_event,
        event(
            "experience-accepted",
            "AcceptanceRecorded",
            {
                "status": "accepted",
                "acceptance_id": experience_payload.acceptance_id,
                "proposal_id": experience_payload.proposal_id,
                "evaluated_world_revision": experience_payload.evaluated_world_revision,
                "accepted_change_id": experience_payload.change_id,
                "accepted_change_hash": experience_payload.accepted_change_hash,
            },
            at=settled_at,
        ),
        event(
            "experience-committed",
            "ExperienceCommitted",
            experience_payload.model_dump(mode="json"),
            at=settled_at,
        ),
        event(
            "appraisal-triggered",
            "TriggerProcessOpened",
            {"process": trigger.model_dump(mode="json")},
            at=settled_at,
        ),
    ]


def experience_proposal_event() -> WorldEvent:
    payload = ExperienceCommittedPayload.model_validate_json(
        settlement_batch()[3].payload_json
    )
    proposal = ExperienceProposalProjection(
        proposal_id=payload.proposal_id,
        proposal_encoding="typed-authority-v1",
        authority_contract_ref="proposal-contract:experience.1",
        transition_kind="commit",
        change_id=payload.change_id,
        transition_id=payload.transition_id,
        evaluated_world_revision=payload.evaluated_world_revision,
        expected_entity_revision=0,
        proposed_change_hash=payload.accepted_change_hash,
        evidence_refs=payload.evidence_refs,
        policy_refs=payload.policy_refs,
        proposed_mutation=ExperienceProposedMutation(
            payload_json=json.dumps(
                payload.model_dump(mode="json"),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        ),
    )
    return event(
        "experience-proposed",
        "ProposalRecorded",
        proposal.model_dump(mode="json"),
        at=LIFE_TIME,
    )


def assert_completed_vertical(ledger: LedgerPort) -> None:
    projection = ledger.project()
    assert projection.world_occurrences[0].status == "settled"
    assert projection.world_occurrences[0].result_id == "result-tea-good"
    assert projection.experiences[0].experience_id == "experience-tea"
    assert projection.trigger_processes[0].process_kind == "npc_world_appraisal"
    assert (
        ledger.project_at(
            ProjectionCursor(
                world_revision=7,
                deliberation_revision=3,
                ledger_sequence=10,
            )
        )
        .world_occurrences[0]
        .status
        == "active"
    )
    snapshot = InternalProjectionReader(ledger=ledger).snapshot(world_id=WORLD_ID)
    assert snapshot.npcs[0].npc_id == "lin"
    assert snapshot.plans[0].plan_id == "plan-tea"
    assert snapshot.world_occurrences[0].status == "settled"
    assert snapshot.outcome_observations[0].observation_id == "observation-tea"
    assert snapshot.experiences[0].experience_id == "experience-tea"
    available = {window.slice_name for window in snapshot.slice_windows}
    assert {
        "npcs",
        "plans",
        "world_occurrences",
        "outcome_observations",
        "experiences",
    } <= available


def test_lived_world_settlement_creates_experience_and_appraisal_atomically() -> None:
    ledger = WorldLedger.in_memory(world_id=WORLD_ID)
    seed_through_proposal(ledger)
    commit(ledger, settlement_batch())
    assert_completed_vertical(ledger)
    assert ledger.rebuild() == ledger.project()


def test_lived_world_vertical_replays_identically_after_sqlite_restart(
    tmp_path: Path,
) -> None:
    path = tmp_path / "life.sqlite3"
    ledger = SQLiteWorldLedger(path=path, world_id=WORLD_ID)
    seed_through_proposal(ledger)
    commit(ledger, settlement_batch())
    expected = ledger.project()
    assert ledger.rebuild() == expected
    ledger.close()

    reopened = SQLiteWorldLedger(path=path, world_id=WORLD_ID)
    assert reopened.project() == expected
    assert_completed_vertical(reopened)
    reopened.close()


def test_sqlite_state_hash_rejects_tampered_proposal_audit(tmp_path: Path) -> None:
    path = tmp_path / "life-tampered.sqlite3"
    ledger = SQLiteWorldLedger(path=path, world_id=WORLD_ID)
    seed_through_proposal(ledger)
    semantic_hash = ledger.project().semantic_hash
    ledger.close()

    with sqlite3.connect(path) as connection:
        row = connection.execute(
            "SELECT state_json FROM world_v2_heads WHERE world_id = ?", (WORLD_ID,)
        ).fetchone()
        assert row is not None
        state = json.loads(row[0])
        state["outcome_proposals"][0]["candidate_result_ref"] = "result:forged"
        connection.execute(
            "UPDATE world_v2_heads SET state_json = ? WHERE world_id = ?",
            (json.dumps(state, separators=(",", ":")), WORLD_ID),
        )

    with pytest.raises(LedgerIntegrityError, match="state hash"):
        SQLiteWorldLedger(path=path, world_id=WORLD_ID)
    with sqlite3.connect(path) as connection:
        assert (
            connection.execute(
                "SELECT semantic_hash FROM world_v2_heads WHERE world_id = ?", (WORLD_ID,)
            ).fetchone()[0]
            == semantic_hash
        )


def test_occurrence_cannot_settle_without_matching_appraisal_trigger() -> None:
    ledger = WorldLedger.in_memory(world_id=WORLD_ID)
    seed_through_proposal(ledger)

    with pytest.raises(ValueError, match="exactly one matching"):
        commit(ledger, settlement_batch()[:4])

    assert ledger.project().world_occurrences[0].status == "active"
    assert ledger.project().experiences == ()


def test_occurrence_cannot_settle_without_revision_pinned_acceptance() -> None:
    ledger = WorldLedger.in_memory(world_id=WORLD_ID)
    seed_through_proposal(ledger)
    batch = settlement_batch()

    with pytest.raises(ValueError, match="revision-pinned accepted"):
        commit(ledger, batch[1:])

    assert ledger.project().world_occurrences[0].status == "active"


def test_occurrence_can_settle_without_materializing_optional_experience() -> None:
    ledger = WorldLedger.in_memory(world_id=WORLD_ID)
    seed_through_proposal(ledger)
    batch = settlement_batch()

    commit(ledger, [batch[0], batch[1], batch[4]])
    assert ledger.project().world_occurrences[0].status == "settled"
    assert ledger.project().experiences == ()


def test_appraisal_worker_claims_only_after_settlement_opened_trigger() -> None:
    ledger = WorldLedger.in_memory(world_id=WORLD_ID)
    seed_through_proposal(ledger)
    commit(ledger, settlement_batch())
    opened = ledger.project().trigger_processes[0]
    assert opened.state == "open"

    claimed = opened.model_copy(
        update={
            "state": "claimed",
            "claim_lease": ClaimLease(
                owner_id="worker:appraisal",
                attempt_id="attempt:appraisal:1",
                acquired_at=LIFE_TIME,
                expires_at=LIFE_TIME + timedelta(minutes=2),
            ),
            "attempt_ids": ("attempt:appraisal:1",),
        }
    )
    commit(
        ledger,
        [
            event(
                "appraisal-claimed",
                "TriggerProcessClaimed",
                {"process": claimed.model_dump(mode="json")},
            )
        ],
    )
    assert ledger.project().trigger_processes[0].state == "claimed"


def test_claimed_world_trigger_can_commit_multi_hypothesis_appraisal() -> None:
    ledger = WorldLedger.in_memory(world_id=WORLD_ID)
    seed_through_proposal(ledger)
    commit(ledger, settlement_batch())
    opened = ledger.project().trigger_processes[0]
    claimed = opened.model_copy(
        update={
            "state": "claimed",
            "claim_lease": ClaimLease(
                owner_id="worker:appraisal",
                attempt_id="attempt:appraisal:1",
                acquired_at=LIFE_TIME,
                expires_at=LIFE_TIME + timedelta(minutes=2),
            ),
            "attempt_ids": ("attempt:appraisal:1",),
        }
    )
    commit(
        ledger,
        [
            event(
                "appraisal-claimed",
                "TriggerProcessClaimed",
                {"process": claimed.model_dump(mode="json")},
            )
        ],
    )
    settlement_ref = next(
        ref
        for ref in ledger.project().committed_world_event_refs
        if ref.event_id == "occurrence-settled"
    )
    appraisal_evidence = EvidenceRef(
        ref_id=settlement_ref.event_id,
        evidence_type="settled_world_event",
        claim_purpose="private_hypothesis",
        source_world_revision=settlement_ref.world_revision,
        immutable_hash=settlement_ref.payload_hash,
    )
    appraisal = AppraisalProjection(
        appraisal_id="appraisal:tea-result",
        entity_revision=1,
        subject_ref="occurrence:tea-result",
        source_cluster_ref="world:tea-result",
        origin=AppraisalOrigin(
            change_id="change:appraisal:tea-result",
            transition_id="transition:appraisal:tea-result",
            policy_refs=("policy:appraisal-v1",),
            matrix_catalog_version="appraisal-matrix.1",
            clustering_policy_version="source-clustering.1",
            accepted_event_ref="appraisal-accepted",
        ),
        hypotheses=(
            AppraisalHypothesis(
                hypothesis_id="meaning:satisfaction",
                meaning="creative_satisfaction",
                attribution="companion",
                controllability="controllable",
                severity="moderate",
                weight_bp=7_000,
            ),
            AppraisalHypothesis(
                hypothesis_id="meaning:ordinary",
                meaning="ordinary",
                attribution="situation",
                controllability="partly_controllable",
                severity="low",
                weight_bp=3_000,
            ),
        ),
        evidence_refs=(appraisal_evidence,),
        confidence_bp=8_000,
        accepted_at=LIFE_TIME,
        expires_at=LIFE_TIME + timedelta(hours=2),
    )
    current_world_revision = ledger.project().world_revision
    appraisal_payload = {
        "change_id": "change:appraisal:tea-result",
        "transition_id": "transition:appraisal:tea-result",
        "expected_entity_revision": 0,
        "evidence_refs": [appraisal_evidence.model_dump(mode="json")],
        "policy_refs": ["policy:appraisal-v1"],
        "acceptance_id": "acceptance:appraisal:tea-result",
        "proposal_id": "proposal:appraisal:tea-result",
        "evaluated_world_revision": current_world_revision,
        "accepted_change_hash": "0" * 64,
        "trigger_id": opened.trigger_id,
        "appraisal": appraisal.model_dump(mode="json"),
    }
    change_hash = appraisal_mutation_hash(appraisal_payload)
    appraisal_payload["accepted_change_hash"] = change_hash
    commit(
        ledger,
        [
            event(
                "appraisal-proposed",
                "ProposalRecorded",
                {
                    "proposal_id": "proposal:appraisal:tea-result",
                    "proposal_kind": "appraisal_transition",
                    "transition_kind": "accept",
                    "change_id": "change:appraisal:tea-result",
                    "trigger_id": opened.trigger_id,
                    "trigger_ref": opened.trigger_ref,
                    "source_evidence_ref": "occurrence-settled",
                    "evaluated_world_revision": current_world_revision,
                    "expected_entity_revision": 0,
                    "proposed_change_hash": change_hash,
                    "evidence_refs": [appraisal_evidence.model_dump(mode="json")],
                    "policy_refs": ["policy:appraisal-v1"],
                    "proposed_mutation": {
                        "event_type": "AppraisalAccepted",
                        "payload_json": json.dumps(
                            appraisal_payload,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                    },
                },
            )
        ],
    )
    commit(
        ledger,
        [
            event(
                "appraisal-acceptance",
                "AcceptanceRecorded",
                {
                    "status": "accepted",
                    "acceptance_id": "acceptance:appraisal:tea-result",
                    "proposal_id": "proposal:appraisal:tea-result",
                    "evaluated_world_revision": current_world_revision,
                    "accepted_change_id": "change:appraisal:tea-result",
                    "accepted_change_hash": change_hash,
                },
            ),
            event(
                "appraisal-accepted",
                "AppraisalAccepted",
                {
                    **appraisal_payload,
                },
            ),
            event(
                "appraisal-trigger-completed",
                "TriggerProcessCompleted",
                {
                    "trigger_id": opened.trigger_id,
                    "owner_id": "worker:appraisal",
                    "attempt_id": "attempt:appraisal:1",
                    "completed_at": LIFE_TIME.isoformat(),
                    "runtime_outcome_ref": "appraisal:appraisal:tea-result",
                },
            ),
        ],
    )
    assert len(ledger.project().appraisals[0].hypotheses) == 2
    snapshot = InternalProjectionReader(ledger=ledger).snapshot(world_id=WORLD_ID)
    assert snapshot.appraisals[0].appraisal_id == "appraisal:tea-result"


def test_settlement_cannot_open_a_second_appraisal_continuation() -> None:
    ledger = WorldLedger.in_memory(world_id=WORLD_ID)
    seed_through_proposal(ledger)
    commit(ledger, settlement_batch())
    duplicate = TriggerProcess(
        trigger_id="appraisal:duplicate",
        trigger_ref="appraisal:duplicate",
        process_kind="npc_world_appraisal",
        source_evidence_ref="occurrence-settled",
        state="open",
    )

    with pytest.raises(ValueError, match="settled world event"):
        commit(
            ledger,
            [
                event(
                    "duplicate-appraisal-trigger",
                    "TriggerProcessOpened",
                    {"process": duplicate.model_dump(mode="json")},
                )
            ],
        )


def test_occurrence_acceptance_must_precede_settlement_in_the_commit() -> None:
    ledger = WorldLedger.in_memory(world_id=WORLD_ID)
    seed_through_proposal(ledger)
    batch = settlement_batch()

    with pytest.raises(ValueError, match="accepted decision"):
        commit(ledger, [batch[1], batch[3], batch[0], batch[2]])


def test_sqlite_migrates_a_real_v3_life_trigger_with_derived_provenance(
    tmp_path: Path,
) -> None:
    path = tmp_path / "world-v3-life-trigger.sqlite3"
    ledger = SQLiteWorldLedger(path=path, world_id=WORLD_ID)
    seed_through_proposal(ledger)
    commit(ledger, settlement_batch())
    expected = ledger.project()
    ledger.close()

    with sqlite3.connect(path) as connection:
        state_json = connection.execute(
            "SELECT state_json FROM world_v2_heads WHERE world_id = ?", (WORLD_ID,)
        ).fetchone()[0]
        raw_state = json.loads(state_json)
        strip_v16_state_fields(raw_state)
        raw_state.pop("message_observations", None)
        raw_state.pop("operator_observations", None)
        for process in raw_state["trigger_processes"]:
            process.pop("source_evidence_ref", None)
        for ref in raw_state["committed_world_event_refs"]:
            ref.pop("continuation_refs", None)
        legacy_state = ReducerState.model_validate_json(
            json.dumps(raw_state, separators=(",", ":")),
            context={"source_reducer_bundle": "world-v2-reducers.3"},
        )
        legacy_payload = legacy_state.semantic_payload(
            world_id=WORLD_ID,
            world_revision=expected.world_revision,
            reducer_bundle_version="world-v2-reducers.3",
        )
        legacy_payload.pop("appraisals")
        legacy_payload.pop("affect_baselines")
        legacy_payload.pop("affect_episodes")
        legacy_payload.pop("relationship_signals")
        legacy_payload.pop("relationship_adjustments")
        legacy_payload.pop("relationship_states")
        legacy_payload.pop("boundaries")
        legacy_payload.pop("message_observations")
        legacy_payload.pop("operator_observations")
        legacy_payload.pop("actor_authorities")
        legacy_payload.pop("actor_authority_transitions")
        legacy_payload.pop("consumed_actor_root_nonces")
        for key in (
            "capability_grants", "capability_transitions", "consent_grants",
            "consent_transitions", "privacy_policies", "privacy_transitions",
            "consumed_authorization_root_nonces", "consumed_authorization_challenge_ids",
            "consumed_authorization_source_ids",
        ):
            legacy_payload.pop(key)
        for ref in legacy_payload["committed_world_event_refs"]:
            ref.pop("continuation_refs", None)
        legacy_hash = hashlib.sha256(
            json.dumps(
                legacy_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        connection.execute(
            """UPDATE world_v2_heads
               SET state_json = ?, semantic_hash = ?, reducer_bundle_version = ?
               WHERE world_id = ?""",
            (
                json.dumps(raw_state, separators=(",", ":")),
                legacy_hash,
                "world-v2-reducers.3",
                WORLD_ID,
            ),
        )
        event_row = connection.execute(
            "SELECT event_json FROM world_v2_events WHERE event_id = ?",
            ("appraisal-triggered",),
        ).fetchone()
        raw_event = json.loads(event_row[0])
        raw_payload = json.loads(raw_event["payload_json"])
        raw_payload["process"].pop("source_evidence_ref", None)
        raw_event["payload_json"] = json.dumps(
            raw_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        raw_event["payload_hash"] = hashlib.sha256(
            raw_event["payload_json"].encode("utf-8")
        ).hexdigest()
        encoded_event = json.dumps(
            raw_event,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        connection.execute(
            """UPDATE world_v2_events SET event_json = ?, event_hash = ?
               WHERE event_id = ?""",
            (
                encoded_event,
                hashlib.sha256(encoded_event.encode("utf-8")).hexdigest(),
                "appraisal-triggered",
            ),
        )

    reopened = SQLiteWorldLedger(path=path, world_id=WORLD_ID)
    assert reopened.project() == expected
    assert reopened.rebuild() == expected
    reopened.close()


def test_outcome_proposal_cannot_escape_committed_candidate_matrix() -> None:
    ledger = WorldLedger.in_memory(world_id=WORLD_ID)
    seed_through_proposal(ledger)
    projection = ledger.project()
    invalid = event(
        "invalid-outcome-proposed",
        "OutcomeProposalRecorded",
        {
            "outcome_proposal_id": "outcome-proposal-impossible",
            "decision_proposal_id": "decision-proposal-impossible",
            "change_id": "change:invalid-outcome-proposed",
            "occurrence_id": "occurrence-tea",
            "evaluated_entity_revision": 3,
            "evaluated_world_revision": 7,
            "trigger_ref": "trigger:tea-time",
            "candidate_result_ref": "result:not-committed",
            "proposed_result_id": "result-tea-good",
            "proposed_result_payload_ref": "payload:tea-good",
            "proposed_result_payload_hash": "sha256:tea-good",
            "proposed_change_hash": outcome_mutation_hash(
                change_id="change:invalid-outcome-proposed",
                occurrence_id="occurrence-tea",
                evaluated_entity_revision=3,
                evaluated_world_revision=7,
                candidate_result_ref="result:not-committed",
                result_id="result-tea-good",
                result_payload_ref="payload:tea-good",
                result_payload_hash="sha256:tea-good",
                observation_refs=("observation-tea",),
            ),
            "observation_refs": ["observation-tea"],
            "evidence_refs": [
                world_evidence(
                    ledger,
                    "occurrence-activated",
                    "current_fact",
                )
            ],
            "confidence_bp": 8_500,
            "expires_at": (NOW + timedelta(minutes=8)).isoformat(),
        },
    )
    ledger.commit(
        [invalid],
        expected_world_revision=projection.world_revision,
        expected_deliberation_revision=projection.deliberation_revision,
    )
    batch = settlement_batch()
    acceptance_payload = batch[0].payload()
    acceptance_payload.update(
        {
            "acceptance_id": "acceptance:outcome-proposal-impossible",
            "proposal_id": "outcome-proposal-impossible",
            "accepted_change_id": "change:invalid-outcome-proposed",
            "accepted_change_hash": invalid.payload()["proposed_change_hash"],
        }
    )
    settlement_payload = batch[1].payload()
    settlement_payload.update(
        {
            "acceptance_id": "acceptance:outcome-proposal-impossible",
            "outcome_proposal_id": "outcome-proposal-impossible",
            "candidate_result_ref": "result:not-committed",
            "change_id": "change:invalid-outcome-proposed",
            "accepted_change_hash": invalid.payload()["proposed_change_hash"],
        }
    )
    rejected_batch = [
        event("invalid-outcome-accepted", "AcceptanceRecorded", acceptance_payload),
        event("invalid-occurrence-settled", "WorldOccurrenceSettled", settlement_payload),
        event(
            "invalid-appraisal-triggered",
            "TriggerProcessOpened",
            {
                "process": {
                    **batch[4].payload()["process"],
                    "source_evidence_ref": "invalid-occurrence-settled",
                }
            },
        ),
    ]
    projection = ledger.project()
    with pytest.raises(ValueError, match="outside committed candidates"):
        ledger.commit(
            rejected_batch,
            expected_world_revision=projection.world_revision,
            expected_deliberation_revision=projection.deliberation_revision,
        )
    assert any(
        proposal.outcome_proposal_id == "outcome-proposal-impossible"
        for proposal in ledger.project().outcome_proposals
    )


def test_life_event_cannot_forge_a_second_domain_identity() -> None:
    ledger = WorldLedger.in_memory(world_id=WORLD_ID)
    npc = NpcProjection(
        npc_id="lin",
        entity_revision=1,
        stable_identity_ref="identity:npc:lin",
        privacy_class="private",
    )
    payload = {
        **mutation(
            "npc-forged",
            expected_revision=0,
            evidence_refs=[
                evidence(
                    "operator:npc-lin",
                    "operator_observation",
                    "current_fact",
                )
            ],
        ),
        "npc": npc.model_dump(mode="json"),
    }
    forged = WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id="npc-forged",
        world_id=WORLD_ID,
        event_type="NpcRegistered",
        logical_time=NOW,
        created_at=NOW,
        actor="system:life-test",
        source="life-test",
        trace_id="trace:life",
        causation_id="cause:npc-forged",
        correlation_id="correlation:life",
        idempotency_key="caller-chosen-key",
        payload=payload,
    )

    with pytest.raises(ValueError, match="does not match its domain identity"):
        ledger.commit(
            [forged],
            expected_world_revision=0,
            expected_deliberation_revision=0,
        )


def test_same_life_identity_with_different_bytes_conflicts() -> None:
    ledger = WorldLedger.in_memory(world_id=WORLD_ID)
    advance_life_clock(ledger)
    register_operator_observations(ledger, "operator:npc-lin")

    def registration(event_id: str, trait: str) -> WorldEvent:
        npc = NpcProjection(
            npc_id="lin",
            entity_revision=1,
            stable_identity_ref="identity:npc:lin",
            known_trait_refs=(trait,),
            privacy_class="private",
        )
        return event(
            event_id,
            "NpcRegistered",
            {
                **mutation(
                    event_id,
                    expected_revision=0,
                    evidence_refs=[
                        evidence(
                            "operator:npc-lin",
                            "operator_observation",
                            "current_fact",
                        )
                    ],
                ),
                "npc": npc.model_dump(mode="json"),
            },
        )

    commit(ledger, [registration("npc-first", "trait:quiet")])
    projection = ledger.project()
    with pytest.raises(IdempotencyConflict, match="idempotency key"):
        ledger.commit(
            [registration("npc-second", "trait:loud")],
            expected_world_revision=projection.world_revision,
            expected_deliberation_revision=projection.deliberation_revision,
        )


def test_activity_lifecycle_is_revisioned_and_terminal() -> None:
    ledger = WorldLedger.in_memory(world_id=WORLD_ID)
    seed_through_proposal(ledger)
    register_operator_observations(ledger, "operator:activity")
    evidence_refs = [evidence("operator:activity", "operator_observation", "current_fact")]
    transitions = (
        ("ActivityStarted", 1, "activity-started"),
        ("ActivityPaused", 2, "activity-paused"),
        ("ActivityResumed", 3, "activity-resumed"),
        ("ActivityCompleted", 4, "activity-completed"),
    )
    for event_type, revision, event_id in transitions:
        commit(
            ledger,
            [
                event(
                    event_id,
                    event_type,
                    {
                        **mutation(
                            event_id,
                            expected_revision=revision,
                            evidence_refs=evidence_refs,
                        ),
                        "plan_id": "plan-tea",
                        "transitioned_at": LIFE_TIME.isoformat(),
                        "reason_ref": f"reason:{event_id}",
                    },
                )
            ],
        )
    plan = ledger.project().plans[0]
    assert (plan.status, plan.entity_revision) == ("completed", 5)
    assert plan.owner_actor_ref == "actor:companion"
    assert plan.authority_origin is not None
    assert plan.authority_origin.accepted_event_type == "ActivityCompleted"
    assert plan.authority_origin.accepted_event_ref == "activity-completed"
    assert plan.authority_origin.accepted_world_revision == ledger.project().world_revision

    projection = ledger.project()
    with pytest.raises(ValueError, match="cannot transition"):
        ledger.commit(
            [
                event(
                    "activity-abandon-after-complete",
                    "ActivityAbandoned",
                    {
                        **mutation(
                            "activity-abandon-after-complete",
                            expected_revision=5,
                            evidence_refs=evidence_refs,
                        ),
                        "plan_id": "plan-tea",
                        "transitioned_at": LIFE_TIME.isoformat(),
                        "reason_ref": "reason:too-late",
                    },
                )
            ],
            expected_world_revision=projection.world_revision,
            expected_deliberation_revision=projection.deliberation_revision,
        )


def test_plan_authority_allows_later_transition_of_earlier_plan_but_rejects_same_tick_swap() -> None:
    def head(
        plan_id: str,
        *,
        revision: int,
        event: CommittedWorldEventRef,
        status: str,
    ) -> PlanStateProjection:
        owner = "actor:companion"
        transition_id = f"transition:{event.event_id}"
        plan = PlanStateProjection(
            plan_id=plan_id,
            activity_id=f"activity:{plan_id}",
            entity_revision=revision,
            activity_kind="work",
            evidence_refs=(
                EvidenceRef(
                    ref_id=f"evidence:{plan_id}",
                    evidence_type="observed_message",
                    claim_purpose="future_plan",
                ),
            ),
            status=status,
            importance_bp=5_000,
            last_transitioned_at=(event.logical_time if status != "planned" else None),
            owner_actor_ref=owner,
        )
        projection_hash = plan_authority_projection_hash(plan)
        return plan.model_copy(
            update={"authority_origin": PlanAuthorityOrigin(
                transition_id=transition_id,
                accepted_event_type=event.event_type,
                accepted_event_ref=event.event_id,
                accepted_world_revision=event.world_revision,
                accepted_payload_hash=event.payload_hash,
                accepted_at=event.logical_time,
                authority_projection_hash=projection_hash,
                binding_hash=plan_authority_binding_hash(
                    plan_id=plan_id,
                    owner_actor_ref=owner,
                    entity_revision=revision,
                    transition_id=transition_id,
                    event_type=event.event_type,
                    accepted_event_ref=event.event_id,
                    accepted_world_revision=event.world_revision,
                    accepted_payload_hash=event.payload_hash,
                    accepted_at=event.logical_time,
                    projection_hash=projection_hash,
                ),
            )},
        )

    event_b = CommittedWorldEventRef(
        event_id="event:plan-b",
        event_type="ActivityPlanned",
        world_revision=2,
        payload_hash="b" * 64,
        logical_time=LIFE_TIME,
    )
    event_a_later = CommittedWorldEventRef(
        event_id="event:plan-a-started",
        event_type="ActivityStarted",
        world_revision=3,
        payload_hash="a" * 64,
        logical_time=LIFE_TIME,
    )
    plan_a = head("plan:a", revision=2, event=event_a_later, status="active")
    plan_b = head("plan:b", revision=1, event=event_b, status="planned")
    validate_plan_authority_state(
        (plan_a, plan_b),
        (event_b, event_a_later),
        logical_time=LIFE_TIME,
    )
    with pytest.raises(ValueError, match="exactly bind|projection hash|binding hash"):
        validate_plan_authority_state(
            (
                plan_a.model_copy(update={"authority_origin": plan_b.authority_origin}),
                plan_b.model_copy(update={"authority_origin": plan_a.authority_origin}),
            ),
            (event_b, event_a_later),
            logical_time=LIFE_TIME,
        )

    for update in (
        {"activity_kind": "forged"},
        {"importance_bp": 9_999},
        {"participant_refs": ("actor:other",)},
        {"privacy_class": "public"},
        {
            "evidence_refs": (
                EvidenceRef(
                    ref_id="evidence:forged",
                    evidence_type="observed_message",
                    claim_purpose="future_plan",
                ),
            )
        },
    ):
        with pytest.raises(ValueError, match="projection hash"):
            validate_plan_authority_state(
                (plan_a.model_copy(update=update), plan_b),
                (event_b, event_a_later),
                logical_time=LIFE_TIME,
            )


def test_sqlite_migrates_ownerless_v15_plan_lifecycle_and_reopens(tmp_path: Path) -> None:
    path = tmp_path / "legacy-plan-lifecycle.sqlite3"
    ledger = SQLiteWorldLedger(path=path, world_id=WORLD_ID)
    advance_life_clock(ledger)
    commit(
        ledger,
        [
            event(
                "message-legacy-plan",
                "ObservationRecorded",
                {
                    "schema_version": "world-v2.1",
                    "observation_kind": "message",
                    "observation_id": "message:legacy-plan",
                    "world_id": WORLD_ID,
                    "logical_time": LIFE_TIME.isoformat(),
                    "created_at": LIFE_TIME.isoformat(),
                    "trace_id": "trace:life",
                    "causation_id": "cause:message-legacy-plan",
                    "correlation_id": "correlation:life",
                    "source": "life-test",
                    "source_event_id": "source:message-legacy-plan",
                    "actor": "system:life-test",
                    "channel": "direct_message",
                    "payload_ref": "payload:message-legacy-plan",
                    "payload_hash": "d" * 64,
                    "received_at": LIFE_TIME.isoformat(),
                },
            )
        ],
    )
    message = ledger.project().message_observations[0]
    plan_evidence = EvidenceRef(
        ref_id=message.observation_id,
        evidence_type="observed_message",
        claim_purpose="future_plan",
        source_world_revision=message.world_revision,
        immutable_hash=message.event_payload_hash,
    )
    plan = PlanStateProjection(
        plan_id="plan:legacy-lifecycle",
        activity_id="activity:legacy-lifecycle",
        entity_revision=1,
        activity_kind="work",
        evidence_refs=(plan_evidence,),
        status="planned",
        importance_bp=5_000,
        owner_actor_ref="actor:companion",
    )
    commit(
        ledger,
        [
            event(
                "legacy-plan-created",
                "ActivityPlanned",
                {
                    **mutation(
                        "legacy-plan-created",
                        expected_revision=0,
                        evidence_refs=[plan_evidence.model_dump(mode="json")],
                    ),
                    "plan": plan.model_dump(mode="json"),
                },
            )
        ],
    )
    for event_type, revision, event_id in (
        ("ActivityStarted", 1, "legacy-plan-started"),
        ("ActivityPaused", 2, "legacy-plan-paused"),
    ):
        commit(
            ledger,
            [
                event(
                    event_id,
                    event_type,
                    {
                        **mutation(
                            event_id,
                            expected_revision=revision,
                            evidence_refs=[plan_evidence.model_dump(mode="json")],
                        ),
                        "plan_id": plan.plan_id,
                        "transitioned_at": LIFE_TIME.isoformat(),
                        "reason_ref": f"reason:{event_id}",
                    },
                )
            ],
        )
    expected_cursor = ProjectionCursor(
        world_revision=ledger.project().world_revision,
        deliberation_revision=0,
        ledger_sequence=ledger.project().ledger_sequence,
    )
    ledger.close()

    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT event_json FROM world_v2_events WHERE event_id = ?",
            ("legacy-plan-created",),
        ).fetchone()
        raw_event = json.loads(row["event_json"])
        raw_payload = json.loads(raw_event["payload_json"])
        raw_payload["plan"].pop("owner_actor_ref")
        raw_payload["plan"].pop("authority_origin")
        raw_event["payload_json"] = json.dumps(
            raw_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        raw_event["payload_hash"] = hashlib.sha256(
            raw_event["payload_json"].encode()
        ).hexdigest()
        event_json = json.dumps(
            raw_event, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        connection.execute(
            "UPDATE world_v2_events SET event_json = ?, event_hash = ? WHERE event_id = ?",
            (
                event_json,
                hashlib.sha256(event_json.encode()).hexdigest(),
                "legacy-plan-created",
            ),
        )
        head = connection.execute(
            "SELECT state_json FROM world_v2_heads WHERE world_id = ?", (WORLD_ID,)
        ).fetchone()
        raw_state = json.loads(head["state_json"])
        strip_v16_state_fields(raw_state)
        for ref in raw_state["committed_world_event_refs"]:
            if ref["event_id"] == "legacy-plan-created":
                ref["payload_hash"] = raw_event["payload_hash"]
        legacy_state = ReducerState.model_validate_json(
            json.dumps(raw_state, ensure_ascii=False, separators=(",", ":")),
            context={"source_reducer_bundle": "world-v2-reducers.15"},
        )
        legacy_payload = legacy_state.semantic_payload(
            world_id=WORLD_ID,
            world_revision=expected_cursor.world_revision,
            reducer_bundle_version="world-v2-reducers.15",
        )
        legacy_hash = hashlib.sha256(
            json.dumps(
                legacy_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        connection.execute(
            """UPDATE world_v2_heads SET state_json = ?, semantic_hash = ?,
               reducer_bundle_version = ? WHERE world_id = ?""",
            (
                json.dumps(raw_state, ensure_ascii=False, separators=(",", ":")),
                legacy_hash,
                "world-v2-reducers.15",
                WORLD_ID,
            ),
        )

    reopened = SQLiteWorldLedger(path=path, world_id=WORLD_ID)
    migrated = reopened.project()
    assert migrated.plans[0].status == "paused"
    assert migrated.plans[0].owner_actor_ref == "legacy:unknown-owner"
    assert migrated.plans[0].authority_origin is None
    with pytest.raises(ValueError, match="live activity transition"):
        commit(
            reopened,
            [
                event(
                    "live-resume-legacy-plan",
                    "ActivityResumed",
                    {
                        **mutation(
                            "live-resume-legacy-plan",
                            expected_revision=3,
                            evidence_refs=[plan_evidence.model_dump(mode="json")],
                        ),
                        "plan_id": plan.plan_id,
                        "transitioned_at": LIFE_TIME.isoformat(),
                        "reason_ref": "reason:live-resume",
                    },
                )
            ],
        )
    assert reopened.rebuild() == migrated
    assert reopened.project_at(expected_cursor) == migrated
    reopened.close()
    assert SQLiteWorldLedger(path=path, world_id=WORLD_ID).project() == migrated

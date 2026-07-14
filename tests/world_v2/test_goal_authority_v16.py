from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
import hashlib
import json

import pytest

from companion_daemon.world_v2.event_identity import domain_idempotency_key
from companion_daemon.world_v2.event_catalog import event_contract
from companion_daemon.world_v2.goal_authority_events import (
    V2GoalChangedPayload,
    V2GoalExpiredPayload,
    v2_goal_evidence_refs,
    v2_goal_expiry_hash,
    v2_goal_expiry_id,
    v2_goal_mutation_hash,
)
from companion_daemon.world_v2.ledger import WorldLedger
from companion_daemon.world_v2.reducers import ReducerState
from companion_daemon.world_v2.sqlite_ledger import SQLiteWorldLedger
from companion_daemon.world_v2.typed_proposal_families import family_for_mutation
from companion_daemon.world_v2.goal_authority_reducers import (
    V2_GOAL_INTERNAL_BASIS_POLICY_DIGEST,
    V2_GOAL_INTERNAL_BASIS_POLICY_VERSION,
    V2_GOAL_COMPLETION_CONTRACT_POLICY_DIGEST,
    V2_GOAL_EXPIRY_POLICY_DIGEST,
    V2_GOAL_EXPIRY_POLICY_VERSION,
    V2_GOAL_EXPIRY_CORRECTION_POLICY_DIGEST,
    V2_GOAL_EXPIRY_CORRECTION_POLICY_VERSION,
    V2_GOAL_POLICY_DIGEST,
    V2_GOAL_POLICY_REFS,
    V2_GOAL_POLICY_VERSION,
    reduce_v2_goal,
    reduce_v2_goal_expiry,
)
from companion_daemon.world_v2.actor_authority_reducers import (
    ACTOR_AUTHORITY_POLICY_DIGEST,
    ACTOR_AUTHORITY_V2_POLICY_DIGEST,
)
from companion_daemon.world_v2.clock_authority import (
    CLOCK_AUTHORITY_POLICY_DIGEST,
    CLOCK_AUTHORITY_POLICY_VERSION,
)
from companion_daemon.world_v2.goal_situation_schemas import (
    CommittedEvidenceBasis,
    CommittedEvidenceSource,
    CompensationCauseAuthority,
    ClockCauseAuthority,
    DeliberativeCauseAuthority,
    DomainOperatorAuthorityBinding,
    GoalExpiryCorrectionBasis,
    InternalIntentionBasis,
    RandomDrawBinding,
    SettledEventCauseAuthority,
    V2GoalBlocker,
    V2GoalBlockerResolution,
    V2GoalAbandonedTerminalReason,
    V2GoalCompletedTerminalReason,
    V2GoalExpiredTerminalReason,
    V2GoalCompletionContract,
    V2GoalFactCompletionEvidence,
    V2GoalLifecycleReason,
    V2GoalOccurrenceCompletionEvidence,
    V2GoalOrigin,
    V2GoalProgressAssessment,
    V2GoalRationale,
    V2GoalSupersedesAuthority,
    V2GoalTerminalReason,
    V2GoalTransitionProjection,
    V2GoalProjection,
    V2GoalProposalProjection,
    V2GoalProposedMutation,
    V2GoalValues,
    V2GoalDueWindow,
    v2_goal_semantic_fingerprint,
    v2_goal_completion_contract_digest,
)
from companion_daemon.world_v2.schemas import (
    ActorAuthorityOrigin,
    ActorAuthorityProjection,
    ActorAuthorityValues,
    CommittedWorldEventRef,
    ClockTransitionProjection,
    DueWindow,
    EvidenceRef,
    FactAssertionBinding,
    FactOrigin,
    FactProjection,
    FactValues,
    LedgerProjection,
    ProjectionCursor,
    WorldOccurrenceProjection,
    WorldEvent,
    fact_conflict_key,
    fact_semantic_fingerprint,
)


NOW = datetime(2026, 7, 15, 18, 0, tzinfo=UTC)
OPEN_TIME = NOW - timedelta(hours=1)


def canonical_hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def ledger_event(
    event_id: str,
    event_type: str,
    payload: dict[str, object],
    *,
    logical_time: datetime,
) -> WorldEvent:
    identity = domain_idempotency_key(
        event_type=event_type,
        world_id="world:goal-integration",
        payload=payload,
    )
    return WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id=event_id,
        world_id="world:goal-integration",
        event_type=event_type,
        logical_time=logical_time,
        created_at=logical_time,
        actor="actor:companion",
        source="test:goal-integration",
        trace_id="trace:goal-integration",
        causation_id=f"cause:{event_id}",
        correlation_id="correlation:goal-integration",
        idempotency_key=identity or event_id,
        payload=payload,
    )


def proposal_for_goal_payload(
    payload: V2GoalChangedPayload,
    *,
    transition_kind: str,
    event_type: str,
) -> V2GoalProposalProjection:
    proposed_json = json.dumps(
        payload.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return V2GoalProposalProjection(
        proposal_id=payload.proposal_id,
        transition_kind=transition_kind,
        change_id=payload.change_id,
        transition_id=payload.transition_id,
        evaluated_world_revision=payload.evaluated_world_revision,
        expected_entity_revision=payload.expected_entity_revision,
        proposed_change_hash=payload.accepted_change_hash,
        evidence_refs=payload.evidence_refs,
        policy_refs=payload.policy_refs,
        proposed_mutation=V2GoalProposedMutation(
            event_type=event_type,
            payload_json=proposed_json,
        ),
    )


def record_accept_open_goal(
    ledger: WorldLedger | SQLiteWorldLedger,
    *,
    goal_id: str,
    event_id: str,
) -> V2GoalProjection:
    current = ledger.project()
    cause = internal_cause(
        evaluated_world_revision=current.world_revision,
        logical_time=OPEN_TIME,
        trigger_ref=f"trigger:{goal_id}",
        decision_slot=f"goal-open:{goal_id}",
    )
    due = V2GoalDueWindow(starts_at=OPEN_TIME, ends_at=NOW)
    after = goal_projection(
        revision=1,
        values=V2GoalValues(
            outcome_ref=f"outcome:{goal_id}",
            importance_bp=6000,
            progress_bp=0,
            due_window=due,
            privacy_class="private",
            status="active",
        ),
        event_ref=event_id,
        updated_at=OPEN_TIME,
        goal_id=goal_id,
        opened_at=OPEN_TIME,
    )
    payload = goal_payload(
        after,
        operation="open",
        lane="deliberative",
        cause=cause,
        evaluated_world_revision=current.world_revision,
    )
    proposal = proposal_for_goal_payload(
        payload,
        transition_kind="open",
        event_type="V2GoalOpened",
    )
    ledger.commit(
        [
            ledger_event(
                f"proposal-event:{goal_id}",
                "ProposalRecorded",
                proposal.model_dump(mode="json"),
                logical_time=OPEN_TIME,
            )
        ],
        expected_world_revision=current.world_revision,
        expected_deliberation_revision=current.deliberation_revision,
    )
    proposed = ledger.project()
    acceptance = {
        "proposal_id": payload.proposal_id,
        "evaluated_world_revision": payload.evaluated_world_revision,
        "acceptance_id": payload.acceptance_id,
        "status": "accepted",
        "accepted_change_id": payload.change_id,
        "accepted_change_hash": payload.accepted_change_hash,
    }
    ledger.commit(
        [
            ledger_event(
                f"acceptance-event:{goal_id}",
                "AcceptanceRecorded",
                acceptance,
                logical_time=OPEN_TIME,
            ),
            ledger_event(
                event_id,
                "V2GoalOpened",
                payload.model_dump(mode="json"),
                logical_time=OPEN_TIME,
            ),
        ],
        expected_world_revision=proposed.world_revision,
        expected_deliberation_revision=proposed.deliberation_revision,
    )
    return after


def goal_projection(
    *,
    revision: int,
    values: V2GoalValues,
    event_ref: str,
    updated_at: datetime = NOW,
    goal_id: str = "goal:publish-story",
    opened_at: datetime | None = None,
) -> V2GoalProjection:
    origin = V2GoalOrigin(
        change_id=f"change:goal:{revision}:{event_ref}",
        transition_id=f"transition:goal:{revision}:{event_ref}",
        policy_refs=V2_GOAL_POLICY_REFS,
        accepted_event_ref=event_ref,
    )
    return V2GoalProjection(
        goal_id=goal_id,
        actor_ref="actor:companion",
        entity_revision=revision,
        semantic_fingerprint=v2_goal_semantic_fingerprint(
            goal_id=goal_id,
            actor_ref="actor:companion",
            values=values,
            policy_refs=origin.policy_refs,
        ),
        values=values,
        origin=origin,
        opened_at=opened_at or NOW - timedelta(days=1),
        updated_at=updated_at,
        closed_at=(updated_at if values.status in {"completed", "abandoned", "expired"} else None),
    )


def goal_payload(
    after: V2GoalProjection,
    *,
    operation: str,
    lane: str,
    cause: object,
    before: V2GoalProjection | None = None,
    progress_delta_bp: int | None = None,
    progress_assessment: V2GoalProgressAssessment | None = None,
    blocker_resolutions: tuple[V2GoalBlockerResolution, ...] = (),
    selection_mode: str = "direct",
    random_draw_binding: RandomDrawBinding | None = None,
    lifecycle_reason: V2GoalLifecycleReason | None = None,
    completion_evidence: (
        V2GoalOccurrenceCompletionEvidence | V2GoalFactCompletionEvidence | None
    ) = None,
    terminal_reason: V2GoalTerminalReason | None = None,
    removed_blocker_fingerprints: tuple[str, ...] = (),
    revise_kind: str | None = None,
    compensation_target: CompensationCauseAuthority | None = None,
    evaluated_world_revision: int = 7,
) -> V2GoalChangedPayload:
    raw = {
        "change_id": after.origin.change_id,
        "transition_id": after.origin.transition_id,
        "expected_entity_revision": before.entity_revision if before else 0,
        "evidence_refs": (),
        "policy_refs": V2_GOAL_POLICY_REFS,
        "acceptance_id": f"acceptance:{after.origin.transition_id}",
        "proposal_id": f"proposal:{after.origin.transition_id}",
        "evaluated_world_revision": evaluated_world_revision,
        "accepted_change_hash": "0" * 64,
        "operation": operation,
        "authority_lane": lane,
        "selection_mode": selection_mode,
        "goal_before": before,
        "goal_after": after,
        "cause_authority": cause,
        "revise_kind": revise_kind,
        "progress_delta_bp": progress_delta_bp,
        "progress_assessment": progress_assessment,
        "lifecycle_reason": lifecycle_reason,
        "blocker_resolutions": blocker_resolutions,
        "completion_evidence": completion_evidence,
        "terminal_reason": terminal_reason,
        "removed_blocker_fingerprints": removed_blocker_fingerprints,
        "random_draw_binding": random_draw_binding,
        "compensation_target": compensation_target,
        "policy_version": V2_GOAL_POLICY_VERSION,
        "policy_digest": V2_GOAL_POLICY_DIGEST,
    }
    raw["evidence_refs"] = v2_goal_evidence_refs(raw)
    raw["accepted_change_hash"] = v2_goal_mutation_hash(raw)
    return V2GoalChangedPayload.model_validate(raw)


def goal_expiry_payload(
    before: V2GoalProjection,
    after: V2GoalProjection,
    *,
    clock: ClockTransitionProjection,
    terminal: V2GoalExpiredTerminalReason,
) -> V2GoalExpiredPayload:
    cause = ClockCauseAuthority(
        clock_event_ref=clock.clock_event_ref,
        clock_world_revision=clock.computed_world_revision,
        clock_payload_hash=clock.payload_hash,
        logical_time_from=clock.logical_time_from,
        logical_time_to=clock.logical_time_to,
        policy_version=clock.installed_policy_version,
        policy_digest=clock.installed_policy_digest,
    )
    raw = {
        "operation": "expire",
        "authority_lane": "clock_runtime",
        "world_id": "world:goal-integration",
        "expiry_id": v2_goal_expiry_id(
            world_id="world:goal-integration",
            goal_id=after.goal_id,
            expected_entity_revision=before.entity_revision,
            clock_event_ref=clock.clock_event_ref,
            policy_digest=V2_GOAL_EXPIRY_POLICY_DIGEST,
        ),
        "change_id": after.origin.change_id,
        "transition_id": after.origin.transition_id,
        "expected_entity_revision": before.entity_revision,
        "evaluated_world_revision": clock.computed_world_revision,
        "policy_refs": V2_GOAL_POLICY_REFS,
        "goal_before": before,
        "goal_after": after,
        "cause_authority": cause,
        "terminal_reason": terminal,
        "removed_blocker_fingerprints": tuple(
            sorted(item.blocker_semantic_hash for item in before.values.blockers)
        ),
        "policy_version": V2_GOAL_EXPIRY_POLICY_VERSION,
        "policy_digest": V2_GOAL_EXPIRY_POLICY_DIGEST,
        "mechanical_change_hash": "0" * 64,
    }
    raw["mechanical_change_hash"] = v2_goal_expiry_hash(raw)
    return V2GoalExpiredPayload.model_validate(raw)


def settled_occurrence(
    *, index: int, privacy: str = "private"
) -> tuple[CommittedWorldEventRef, WorldOccurrenceProjection, SettledEventCauseAuthority]:
    source = CommittedWorldEventRef(
        event_id=f"event:settled:{index}",
        event_type="WorldOccurrenceSettled",
        world_revision=7,
        payload_hash=format(index, "x")[-1] * 64,
        logical_time=NOW,
    )
    occurrence = WorldOccurrenceProjection(
        occurrence_id=f"occurrence:{index}",
        entity_revision=2,
        trigger_ref=f"trigger:{index}",
        participant_refs=("actor:companion",),
        location_ref="location:study",
        time_window=DueWindow(
            opens_at=NOW - timedelta(hours=1), closes_at=NOW
        ),
        candidate_outcome_refs=("outcome:publish-story",),
        settled_outcome_ref="outcome:publish-story",
        visibility=privacy,
        status="settled",
        result_id=f"result:{index}",
        result_payload_ref=f"payload:{index}",
        result_payload_hash="e" * 64,
        settled_at=NOW,
        settlement_event_ref=source.event_id,
        settlement_world_revision=source.world_revision,
        settlement_payload_hash=source.payload_hash,
    )
    return source, occurrence, SettledEventCauseAuthority(
        event_ref=source.event_id,
        event_type="WorldOccurrenceSettled",
        world_revision=source.world_revision,
        payload_hash=source.payload_hash,
    )


def deliberative_occurrence_cause(
    source: CommittedWorldEventRef, occurrence: WorldOccurrenceProjection
) -> DeliberativeCauseAuthority:
    return DeliberativeCauseAuthority(
        basis=CommittedEvidenceBasis(
            sources=(
                CommittedEvidenceSource(
                    source_kind="settled_world_event",
                    event_ref=source.event_id,
                    world_revision=source.world_revision,
                    payload_hash=source.payload_hash,
                    source_entity_ref=occurrence.occurrence_id,
                    source_entity_revision=occurrence.entity_revision,
                ),
            )
        )
    )


def rationale(text: str, privacy: str = "private") -> V2GoalRationale:
    return V2GoalRationale(text=text, privacy_class=privacy)


def internal_cause(
    *, policy_digest: str = V2_GOAL_INTERNAL_BASIS_POLICY_DIGEST,
    rationale_privacy: str = "private",
    evaluated_world_revision: int = 7,
    logical_time: datetime = NOW,
    trigger_ref: str = "trigger:deliberation:goal",
    decision_slot: str = "goal-governance:1",
) -> DeliberativeCauseAuthority:
    material = {
        "basis_kind": "internal_intention",
        "actor_ref": "actor:companion",
        "trigger_ref": trigger_ref,
        "decision_slot": decision_slot,
        "evaluated_world_revision": evaluated_world_revision,
        "logical_time": logical_time,
        "intention_kind": "goal_governance",
        "intention_class": "self_direction",
        "rationale": rationale(
            "I want to choose this direction for myself.", privacy=rationale_privacy
        ),
        "policy_version": V2_GOAL_INTERNAL_BASIS_POLICY_VERSION,
        "policy_digest": policy_digest,
        "privacy_class": "private",
    }
    basis = InternalIntentionBasis.model_validate(
        {
            **material,
            "intention_material_hash": canonical_hash(
                InternalIntentionBasis.model_construct(**material).model_dump(mode="json")
            ),
        }
    )
    return DeliberativeCauseAuthority(basis=basis)


def blocker(
    *, blocker_id: str, blocker_class: str, basis: object, text: str
) -> V2GoalBlocker:
    material = {
        "blocker_id": blocker_id,
        "blocker_class": blocker_class,
        "basis": basis,
        "rationale": rationale(text),
        "privacy_class": "private",
    }
    return V2GoalBlocker.model_validate(
        {**material, "blocker_semantic_hash": canonical_hash(
            V2GoalBlocker.model_construct(**material).model_dump(mode="json")
        )}
    )


def completion_contract(
    *,
    cutoff: int = 6,
    contract_id: str = "goal-contract:publish-story:1",
    actor_ref: str = "actor:companion",
    completion_kind: str = "settled_occurrence_outcome",
    fact_predicate: str | None = None,
    fact_value_hash: str | None = None,
) -> V2GoalCompletionContract:
    fact_completion = completion_kind == "active_fact_predicate"
    material = {
        "contract_id": contract_id,
        "contract_version": "v2-goal-completion-contract.1",
        "completion_kind": completion_kind,
        "outcome_ref": "outcome:publish-story",
        "expected_actor_ref": actor_ref,
        "allowed_settled_event_types": (
            ("FactCommitted", "FactCorrected")
            if fact_completion
            else ("WorldOccurrenceSettled",)
        ),
        "contract_schema_ref": (
            "goal-contract-schema:active-fact.1"
            if fact_completion
            else "goal-contract-schema:occurrence.1"
        ),
        "completion_parser_ref": (
            "goal-completion:active-fact.1"
            if fact_completion
            else "goal-completion:occurrence.1"
        ),
        "evidence_schema_ref": (
            "fact-authority.1"
            if fact_completion
            else "world-occurrence-settlement.1"
        ),
        "required_fact_predicate": fact_predicate,
        "required_fact_value_hash": fact_value_hash,
        "evidence_cutoff_world_revision": cutoff,
        "policy_version": "v2-goal-completion-contract.1",
        "policy_digest": V2_GOAL_COMPLETION_CONTRACT_POLICY_DIGEST,
        "privacy_class": "private",
    }
    return V2GoalCompletionContract.model_validate(
        {**material, "contract_digest": v2_goal_completion_contract_digest(material)}
    )


def active_completion_fact(
    *,
    event_ref: str,
    predicate: str = "artifact.published",
    value_ref: str = "outcome:publish-story",
    value_hash: str = "f" * 64,
    subject_ref: str = "actor:companion",
    revision: int = 1,
) -> FactProjection:
    source = EvidenceRef(
        ref_id="operator-observation:published-story",
        evidence_type="operator_observation",
        claim_purpose="current_fact",
    )
    binding = FactAssertionBinding(
        source_kind="operator_observation",
        source_ref=source.ref_id,
        asserted_subject_ref=subject_ref,
        content_payload_hash="e" * 64,
    )
    values = FactValues(
        subject_ref=subject_ref,
        predicate_code=predicate,
        cardinality="single",
        conflict_key=fact_conflict_key(
            subject_ref=subject_ref, predicate_code=predicate
        ),
        value_ref=value_ref,
        value_hash=value_hash,
        assertion_binding=binding,
        anchor_evidence_refs=(source,),
        source_evidence_refs=(source,),
        confidence_bp=10_000,
        privacy_class="private",
        status="active",
    )
    origin = FactOrigin(
        change_id=f"change:fact:completion:{revision}",
        transition_id=f"transition:fact:completion:{revision}",
        policy_refs=("policy:fact-authority.1",),
        accepted_event_ref=event_ref,
    )
    return FactProjection(
        fact_id="fact:companion:published-story",
        entity_revision=revision,
        semantic_fingerprint=fact_semantic_fingerprint(
            subject_ref=values.subject_ref,
            predicate_code=values.predicate_code,
            cardinality=values.cardinality,
            conflict_key=values.conflict_key,
            value_hash=values.value_hash,
            assertion_binding=values.assertion_binding,
            anchor_evidence_refs=values.anchor_evidence_refs,
            policy_refs=origin.policy_refs,
        ),
        values=values,
        origin=origin,
        committed_at=NOW - timedelta(minutes=1),
        updated_at=NOW - timedelta(minutes=1),
    )


def operator_authority(
    *, policy_version: str = "actor-authority-policy.2"
) -> tuple[
    ActorAuthorityProjection,
    CommittedWorldEventRef,
    DomainOperatorAuthorityBinding,
]:
    policy_digest = (
        ACTOR_AUTHORITY_V2_POLICY_DIGEST
        if policy_version == "actor-authority-policy.2"
        else ACTOR_AUTHORITY_POLICY_DIGEST
    )
    values = ActorAuthorityValues(
        principal_ref="operator:deployment",
        principal_kind="deployment_operator",
        credential_ref="credential:goal-governance",
        allowed_operations=("v2_goal_governance",),
        valid_from=NOW - timedelta(days=1),
        expires_at=NOW + timedelta(days=1),
        status="active",
    )
    event_ref = CommittedWorldEventRef(
        event_id="event:actor-authority:goal",
        event_type="ActorAuthorityBootstrapped",
        world_revision=5,
        payload_hash="9" * 64,
        logical_time=NOW - timedelta(hours=2),
    )
    authority = ActorAuthorityProjection(
        authority_id="actor-authority:goal",
        entity_revision=1,
        values=values,
        policy_version=policy_version,
        policy_digest=policy_digest,
        origin=ActorAuthorityOrigin(
            transition_id="transition:actor-authority:goal",
            event_ref=event_ref.event_id,
            root_key_id="deployment-root:production-1",
            root_keyset_version="deployment-root-keyset.1",
            root_keyset_digest="a" * 64,
            root_nonce_hash="b" * 64,
            root_proof_hash="c" * 64,
        ),
        updated_at=event_ref.logical_time,
    )
    binding = DomainOperatorAuthorityBinding(
        authority_id=authority.authority_id,
        authority_revision=authority.entity_revision,
        principal_ref=values.principal_ref,
        authority_event_ref=event_ref.event_id,
        authority_world_revision=event_ref.world_revision,
        authority_payload_hash=event_ref.payload_hash,
        authority_values_hash=canonical_hash(values.model_dump(mode="json")),
        authority_policy_digest=policy_digest,
        authorization_contract="deployment-actor-authority:v16-domain.1",
        required_operation="v2_goal_governance",
    )
    return authority, event_ref, binding


def progress_transition_for_compensation(
    *, index: int = 40,
) -> tuple[
    V2GoalProjection,
    V2GoalProjection,
    tuple[V2GoalTransitionProjection, ...],
    CommittedWorldEventRef,
]:
    source, occurrence, _ = settled_occurrence(index=index)
    cause = deliberative_occurrence_cause(source, occurrence)
    before = goal_projection(
        revision=1,
        values=V2GoalValues(
            outcome_ref="outcome:publish-story",
            importance_bp=8000,
            progress_bp=1000,
            privacy_class="private",
            status="active",
        ),
        event_ref=f"event:goal:before-compensation:{index}",
        updated_at=NOW - timedelta(hours=2),
    )
    progressed = goal_projection(
        revision=2,
        values=before.values.model_copy(update={"progress_bp": 3000}),
        event_ref=f"event:goal:progress-to-compensate:{index}",
        updated_at=NOW - timedelta(hours=1),
    )
    payload = goal_payload(
        progressed,
        operation="progress",
        lane="deliberative",
        cause=cause,
        before=before,
        progress_delta_bp=2000,
        progress_assessment=V2GoalProgressAssessment(
            contribution_class="direct_contribution",
            basis=cause.basis,
            rationale=rationale("I initially counted this as substantial progress."),
        ),
    )
    _, history = reduce_v2_goal(
        (before,),
        (),
        payload,
        event_type="V2GoalProgressed",
        event_id=progressed.origin.accepted_event_ref,
        logical_time=progressed.updated_at,
        actor_authorities=(),
        committed_events=(source,),
        random_draws=(),
        world_occurrences=(occurrence,),
    )
    target_event = CommittedWorldEventRef(
        event_id=progressed.origin.accepted_event_ref,
        event_type="V2GoalProgressed",
        world_revision=8,
        payload_hash=f"{index % 10}" * 64,
        logical_time=progressed.updated_at,
    )
    return before, progressed, history, target_event


def test_goal_open_at_full_progress_remains_active() -> None:
    source = CommittedWorldEventRef(
        event_id="event:outcome-source",
        event_type="WorldOccurrenceSettled",
        world_revision=7,
        payload_hash="a" * 64,
        logical_time=NOW,
    )
    occurrence = WorldOccurrenceProjection(
        occurrence_id="occurrence:outcome-source",
        entity_revision=2,
        trigger_ref="trigger:outcome-source",
        participant_refs=("actor:companion",),
        location_ref="location:study",
        time_window=DueWindow(opens_at=NOW - timedelta(hours=1), closes_at=NOW),
        candidate_outcome_refs=("outcome:publish-story",),
        settled_outcome_ref="outcome:publish-story",
        visibility="private",
        status="settled",
        result_id="result:outcome-source",
        result_payload_ref="payload:outcome-source",
        result_payload_hash="b" * 64,
        settled_at=NOW,
        settlement_event_ref=source.event_id,
        settlement_world_revision=source.world_revision,
        settlement_payload_hash=source.payload_hash,
    )
    cause = deliberative_occurrence_cause(source, occurrence)
    values = V2GoalValues(
        outcome_ref="outcome:publish-story",
        importance_bp=8000,
        progress_bp=10_000,
        blockers=(),
        privacy_class="private",
        status="active",
    )
    origin = V2GoalOrigin(
        change_id="change:goal:1",
        transition_id="transition:goal:1",
        policy_refs=V2_GOAL_POLICY_REFS,
        accepted_event_ref="event:goal:opened",
    )
    after = V2GoalProjection(
        goal_id="goal:publish-story",
        actor_ref="actor:companion",
        entity_revision=1,
        semantic_fingerprint=v2_goal_semantic_fingerprint(
            goal_id="goal:publish-story",
            actor_ref="actor:companion",
            values=values,
            policy_refs=origin.policy_refs,
        ),
        values=values,
        origin=origin,
        opened_at=NOW,
        updated_at=NOW,
    )
    raw = {
        "change_id": origin.change_id,
        "transition_id": origin.transition_id,
        "expected_entity_revision": 0,
        "evidence_refs": (),
        "policy_refs": V2_GOAL_POLICY_REFS,
        "acceptance_id": "acceptance:goal:1",
        "proposal_id": "proposal:goal:1",
        "evaluated_world_revision": 7,
        "accepted_change_hash": "0" * 64,
        "operation": "open",
        "authority_lane": "deliberative",
        "selection_mode": "direct",
        "goal_before": None,
        "goal_after": after,
        "cause_authority": cause,
        "revise_kind": None,
        "progress_delta_bp": None,
        "progress_assessment": None,
        "lifecycle_reason": None,
        "blocker_resolutions": (),
        "completion_evidence": None,
        "terminal_reason": None,
        "removed_blocker_fingerprints": (),
        "random_draw_binding": None,
        "compensation_target": None,
        "policy_version": V2_GOAL_POLICY_VERSION,
        "policy_digest": V2_GOAL_POLICY_DIGEST,
    }
    raw["evidence_refs"] = v2_goal_evidence_refs(raw)
    raw["accepted_change_hash"] = v2_goal_mutation_hash(raw)
    payload = V2GoalChangedPayload.model_validate(raw)

    heads, history = reduce_v2_goal(
        (),
        (),
        payload,
        event_type="V2GoalOpened",
        event_id=after.origin.accepted_event_ref,
        logical_time=NOW,
        actor_authorities=(),
        committed_events=(source,),
        random_draws=(),
        world_occurrences=(occurrence,),
    )

    assert heads == (after,)
    assert heads[0].values.progress_bp == 10_000
    assert heads[0].values.status == "active"
    assert history[0].operation == "open"


def test_same_goal_can_open_and_progress_at_the_same_logical_tick() -> None:
    source, occurrence, _ = settled_occurrence(index=31)
    opened = goal_projection(
        revision=1,
        values=V2GoalValues(
            outcome_ref="outcome:publish-story",
            importance_bp=8000,
            progress_bp=0,
            privacy_class="private",
            status="active",
        ),
        event_ref="event:goal:same-tick-open",
        updated_at=NOW,
        opened_at=NOW,
    )
    open_payload = goal_payload(
        opened,
        operation="open",
        lane="deliberative",
        cause=internal_cause(logical_time=NOW),
    )
    goals, history = reduce_v2_goal(
        (),
        (),
        open_payload,
        event_type="V2GoalOpened",
        event_id=opened.origin.accepted_event_ref,
        logical_time=NOW,
        actor_authorities=(),
        committed_events=(source,),
        random_draws=(),
        world_occurrences=(occurrence,),
    )
    cause = deliberative_occurrence_cause(source, occurrence)
    progressed = goal_projection(
        revision=2,
        values=opened.values.model_copy(update={"progress_bp": 1000}),
        event_ref="event:goal:same-tick-progress",
        updated_at=NOW,
        opened_at=NOW,
    )
    progress_payload = goal_payload(
        progressed,
        operation="progress",
        lane="deliberative",
        cause=cause,
        before=opened,
        progress_delta_bp=1000,
        progress_assessment=V2GoalProgressAssessment(
            contribution_class="direct_contribution",
            basis=cause.basis,
            rationale=rationale("I count this as progress within the same tick."),
        ),
    )
    goals, history = reduce_v2_goal(
        goals,
        history,
        progress_payload,
        event_type="V2GoalProgressed",
        event_id=progressed.origin.accepted_event_ref,
        logical_time=NOW,
        actor_authorities=(),
        committed_events=(source,),
        random_draws=(),
        world_occurrences=(occurrence,),
    )
    state = ReducerState(
        logical_time=NOW,
        goals=goals,
        goal_transitions=history,
    )
    assert tuple(item.entity_revision for item in state.goal_transitions) == (1, 2)
    assert tuple(item.accepted_at for item in state.goal_transitions) == (NOW, NOW)


def test_goal_hashes_normalize_equivalent_datetime_offsets() -> None:
    china = timezone(timedelta(hours=8))
    utc_values = V2GoalValues(
        outcome_ref="outcome:timezone",
        importance_bp=5000,
        progress_bp=0,
        due_window=V2GoalDueWindow(starts_at=NOW, ends_at=NOW + timedelta(hours=1)),
        privacy_class="private",
        status="active",
    )
    offset_values = utc_values.model_copy(
        update={
            "due_window": V2GoalDueWindow(
                starts_at=NOW.astimezone(china),
                ends_at=(NOW + timedelta(hours=1)).astimezone(china),
            )
        }
    )
    assert v2_goal_semantic_fingerprint(
        goal_id="goal:timezone",
        actor_ref="actor:companion",
        values=utc_values,
        policy_refs=V2_GOAL_POLICY_REFS,
    ) == v2_goal_semantic_fingerprint(
        goal_id="goal:timezone",
        actor_ref="actor:companion",
        values=offset_values,
        policy_refs=V2_GOAL_POLICY_REFS,
    )
    assert v2_goal_expiry_hash(
        {"deadline": NOW.isoformat(), "goal_id": "goal:timezone"}
    ) == v2_goal_expiry_hash(
        {"deadline": NOW.astimezone(china).isoformat(), "goal_id": "goal:timezone"}
    )


@pytest.mark.parametrize(
    ("status", "blockers"),
    (("paused", ()), ("blocked", ("blocker:editing",))),
)
def test_deliberative_progress_preserves_paused_or_blocked_status(
    status: str, blockers: tuple[str, ...]
) -> None:
    source, occurrence, _ = settled_occurrence(index=2)
    cause = deliberative_occurrence_cause(source, occurrence)
    blocker_values = ()
    if blockers:
        blocker_values = (
            blocker(
                blocker_id=blockers[0],
                blocker_class="resource_constraint",
                basis=cause.basis,
                text="Editing time is currently constrained.",
            ),
        )
    before = goal_projection(
        revision=1,
        values=V2GoalValues(
            outcome_ref="outcome:publish-story",
            importance_bp=8000,
            progress_bp=2000,
            blockers=blocker_values,
            privacy_class="private",
            status=status,
        ),
        event_ref="event:goal:before-progress",
        updated_at=NOW - timedelta(hours=1),
    )
    after = goal_projection(
        revision=2,
        values=before.values.model_copy(update={"progress_bp": 5000}),
        event_ref="event:goal:progressed",
    )
    payload = goal_payload(
        after,
        operation="progress",
        lane="deliberative",
        cause=cause,
        before=before,
        progress_delta_bp=3000,
        progress_assessment=V2GoalProgressAssessment(
            contribution_class="indirect_support",
            basis=cause.basis,
            rationale=rationale("This made the next writing step materially easier."),
        ),
    )

    heads, history = reduce_v2_goal(
        (before,),
        (),
        payload,
        event_type="V2GoalProgressed",
        event_id=after.origin.accepted_event_ref,
        logical_time=NOW,
        actor_authorities=(),
        committed_events=(source,),
        random_draws=(),
        world_occurrences=(occurrence,),
    )

    assert heads == (after,)
    assert heads[0].values.status == status
    assert history[-1].operation == "progress"


def test_unblocking_one_of_multiple_blockers_remains_blocked() -> None:
    cause_event, cause_occurrence, cause = settled_occurrence(index=3)
    blocker_event, blocker_occurrence, _ = settled_occurrence(index=4)
    cause = deliberative_occurrence_cause(cause_event, cause_occurrence)
    blocker_basis = deliberative_occurrence_cause(
        blocker_event, blocker_occurrence
    ).basis
    removed_ref = "blocker:missing-feedback"
    remaining_ref = "blocker:editing-time"
    removed = blocker(
        blocker_id=removed_ref,
        blocker_class="external_dependency",
        basis=blocker_basis,
        text="I was waiting for feedback before continuing.",
    )
    remaining = blocker(
        blocker_id=remaining_ref,
        blocker_class="resource_constraint",
        basis=cause.basis,
        text="I still need enough focused editing time.",
    )
    before = goal_projection(
        revision=1,
        values=V2GoalValues(
            outcome_ref="outcome:publish-story",
            importance_bp=8000,
            progress_bp=2000,
            blockers=tuple(sorted((removed, remaining), key=lambda item: item.blocker_id)),
            privacy_class="private",
            status="blocked",
        ),
        event_ref="event:goal:blocked",
        updated_at=NOW - timedelta(hours=1),
    )
    after = goal_projection(
        revision=2,
        values=before.values.model_copy(
            update={"blockers": (remaining,), "status": "blocked"}
        ),
        event_ref="event:goal:partially-unblocked",
    )
    payload = goal_payload(
        after,
        operation="unblock",
        lane="deliberative",
        cause=cause,
        before=before,
        blocker_resolutions=(
            V2GoalBlockerResolution(
                blocker_id=removed.blocker_id,
                blocker_semantic_hash=removed.blocker_semantic_hash,
                resolution_class="externally_resolved",
                rationale=rationale(
                    "The feedback arrived, but the time constraint remains."
                ),
                basis=cause.basis,
            ),
        ),
    )

    heads, history = reduce_v2_goal(
        (before,),
        (),
        payload,
        event_type="V2GoalUnblocked",
        event_id=after.origin.accepted_event_ref,
        logical_time=NOW,
        actor_authorities=(),
        committed_events=(cause_event, blocker_event),
        random_draws=(),
        world_occurrences=(cause_occurrence, blocker_occurrence),
    )

    assert heads[0].values.status == "blocked"
    assert heads[0].values.blockers == (remaining,)
    assert history[-1].operation == "unblock"


def test_deliberative_block_installs_typed_blocker() -> None:
    source, occurrence, _ = settled_occurrence(index=5)
    cause = deliberative_occurrence_cause(source, occurrence)
    before = goal_projection(
        revision=1,
        values=V2GoalValues(
            outcome_ref="outcome:publish-story",
            importance_bp=8000,
            progress_bp=2000,
            privacy_class="private",
            status="active",
        ),
        event_ref="event:goal:before-block",
        updated_at=NOW - timedelta(hours=1),
    )
    installed = blocker(
        blocker_id="blocker:feedback",
        blocker_class="external_dependency",
        basis=cause.basis,
        text="I want feedback before I choose the final ending.",
    )
    after = goal_projection(
        revision=2,
        values=before.values.model_copy(
            update={"blockers": (installed,), "status": "blocked"}
        ),
        event_ref="event:goal:blocked:typed",
    )
    payload = goal_payload(
        after,
        operation="block",
        lane="deliberative",
        cause=cause,
        before=before,
    )

    heads, history = reduce_v2_goal(
        (before,),
        (),
        payload,
        event_type="V2GoalBlocked",
        event_id=after.origin.accepted_event_ref,
        logical_time=NOW,
        actor_authorities=(),
        committed_events=(source,),
        random_draws=(),
        world_occurrences=(occurrence,),
    )

    assert heads[0].values.blockers == (installed,)
    assert history[-1].selection_mode == "direct"


def test_unblock_rejects_resolution_for_another_blocker_hash() -> None:
    source, occurrence, _ = settled_occurrence(index=6)
    cause = deliberative_occurrence_cause(source, occurrence)
    existing = blocker(
        blocker_id="blocker:feedback",
        blocker_class="external_dependency",
        basis=cause.basis,
        text="I am waiting for feedback.",
    )
    before = goal_projection(
        revision=1,
        values=V2GoalValues(
            outcome_ref="outcome:publish-story",
            importance_bp=8000,
            progress_bp=2000,
            blockers=(existing,),
            privacy_class="private",
            status="blocked",
        ),
        event_ref="event:goal:blocked:hash",
        updated_at=NOW - timedelta(hours=1),
    )
    after = goal_projection(
        revision=2,
        values=before.values.model_copy(update={"blockers": (), "status": "active"}),
        event_ref="event:goal:bad-unblock",
    )
    payload = goal_payload(
        after,
        operation="unblock",
        lane="deliberative",
        cause=cause,
        before=before,
        blocker_resolutions=(
            V2GoalBlockerResolution(
                blocker_id=existing.blocker_id,
                blocker_semantic_hash="f" * 64,
                resolution_class="externally_resolved",
                rationale=rationale("The feedback has now arrived."),
                basis=cause.basis,
            ),
        ),
    )

    with pytest.raises(ValueError, match="exact non-empty blocker diff"):
        reduce_v2_goal(
            (before,),
            (),
            payload,
            event_type="V2GoalUnblocked",
            event_id=after.origin.accepted_event_ref,
            logical_time=NOW,
            actor_authorities=(),
            committed_events=(source,),
            random_draws=(),
            world_occurrences=(occurrence,),
        )


def test_progress_rejects_zero_delta_instead_of_writing_no_change() -> None:
    source, occurrence, _ = settled_occurrence(index=7)
    cause = deliberative_occurrence_cause(source, occurrence)
    before = goal_projection(
        revision=1,
        values=V2GoalValues(
            outcome_ref="outcome:publish-story",
            importance_bp=8000,
            progress_bp=2000,
            privacy_class="private",
            status="active",
        ),
        event_ref="event:goal:before-zero",
        updated_at=NOW - timedelta(hours=1),
    )
    after = goal_projection(
        revision=2,
        values=before.values,
        event_ref="event:goal:zero-progress",
    )
    with pytest.raises(ValueError, match="positive exact delta"):
        goal_payload(
            after,
            operation="progress",
            lane="deliberative",
            cause=cause,
            before=before,
            progress_delta_bp=0,
            progress_assessment=V2GoalProgressAssessment(
                contribution_class="reappraisal",
                basis=cause.basis,
                rationale=rationale("I do not think this changed the goal yet."),
            ),
        )


def test_progress_rejects_privacy_downgrade_and_time_rollback() -> None:
    source, occurrence, _ = settled_occurrence(index=8)
    cause = deliberative_occurrence_cause(source, occurrence)
    before = goal_projection(
        revision=1,
        values=V2GoalValues(
            outcome_ref="outcome:publish-story",
            importance_bp=8000,
            progress_bp=2000,
            privacy_class="private",
            status="active",
        ),
        event_ref="event:goal:before-attacks",
        updated_at=NOW,
    )
    after = goal_projection(
        revision=2,
        values=before.values.model_copy(update={"progress_bp": 3000}),
        event_ref="event:goal:progress-attacks",
        updated_at=NOW,
    )
    payload = goal_payload(
        after,
        operation="progress",
        lane="deliberative",
        cause=cause,
        before=before,
        progress_delta_bp=1000,
        progress_assessment=V2GoalProgressAssessment(
            contribution_class="direct_contribution",
            basis=cause.basis,
            rationale=rationale("This moved the draft forward.", privacy="withhold"),
        ),
    )
    with pytest.raises(ValueError, match="subjective rationale"):
        reduce_v2_goal(
            (before,),
            (),
            payload,
            event_type="V2GoalProgressed",
            event_id=after.origin.accepted_event_ref,
            logical_time=NOW,
            actor_authorities=(),
            committed_events=(source,),
            random_draws=(),
            world_occurrences=(occurrence,),
        )
    rolled_back = goal_projection(
        revision=2,
        values=before.values.model_copy(update={"progress_bp": 3000}),
        event_ref="event:goal:progress-rollback",
        updated_at=NOW - timedelta(seconds=1),
    )
    private_payload = goal_payload(
        rolled_back,
        operation="progress",
        lane="deliberative",
        cause=cause,
        before=before,
        progress_delta_bp=1000,
        progress_assessment=V2GoalProgressAssessment(
            contribution_class="direct_contribution",
            basis=cause.basis,
            rationale=rationale("This moved the draft forward."),
        ),
    )
    with pytest.raises(ValueError, match="cannot move backward"):
        reduce_v2_goal(
            (before,),
            (),
            private_payload,
            event_type="V2GoalProgressed",
            event_id=rolled_back.origin.accepted_event_ref,
            logical_time=rolled_back.updated_at,
            actor_authorities=(),
            committed_events=(source,),
            random_draws=(),
            world_occurrences=(occurrence,),
        )


def test_random_draw_is_fail_closed_even_with_complete_placeholder() -> None:
    source, occurrence, _ = settled_occurrence(index=9)
    cause = deliberative_occurrence_cause(source, occurrence)
    before = goal_projection(
        revision=1,
        values=V2GoalValues(
            outcome_ref="outcome:publish-story",
            importance_bp=8000,
            progress_bp=1000,
            privacy_class="private",
            status="active",
        ),
        event_ref="event:goal:before-random",
        updated_at=NOW - timedelta(hours=1),
    )
    after = goal_projection(
        revision=2,
        values=before.values.model_copy(update={"progress_bp": 2000}),
        event_ref="event:goal:random-progress",
    )
    draw = RandomDrawBinding(
        draw_event_ref="event:random:claimed",
        draw_world_revision=6,
        draw_payload_hash="a" * 64,
        attempt_id="attempt:random:1",
        candidate_set_hash="b" * 64,
        selected_candidate_ref="candidate:progress",
        catalog_version="catalog.1",
        sampler_version="sampler.1",
    )
    payload = goal_payload(
        after,
        operation="progress",
        lane="deliberative",
        cause=cause,
        before=before,
        progress_delta_bp=1000,
        progress_assessment=V2GoalProgressAssessment(
            contribution_class="direct_contribution",
            basis=cause.basis,
            rationale=rationale("This moved the draft forward."),
        ),
        selection_mode="random_draw",
        random_draw_binding=draw,
    )
    with pytest.raises(ValueError, match="RandomAuthority is installed"):
        reduce_v2_goal(
            (before,),
            (),
            payload,
            event_type="V2GoalProgressed",
            event_id=after.origin.accepted_event_ref,
            logical_time=NOW,
            actor_authorities=(),
            committed_events=(source,),
            random_draws=(),
            world_occurrences=(occurrence,),
        )


def test_rationale_rejects_control_characters() -> None:
    with pytest.raises(ValueError, match="trimmed NFC"):
        rationale("quiet\u0000thought")


def test_progress_rejects_settlement_lane_and_stale_before_image() -> None:
    source, occurrence, settled = settled_occurrence(index=10)
    cause = deliberative_occurrence_cause(source, occurrence)
    before = goal_projection(
        revision=1,
        values=V2GoalValues(
            outcome_ref="outcome:publish-story",
            importance_bp=8000,
            progress_bp=1000,
            privacy_class="private",
            status="active",
        ),
        event_ref="event:goal:before-lane",
        updated_at=NOW - timedelta(hours=1),
    )
    after = goal_projection(
        revision=2,
        values=before.values.model_copy(update={"progress_bp": 2000}),
        event_ref="event:goal:progress-lane",
    )
    assessment = V2GoalProgressAssessment(
        contribution_class="direct_contribution",
        basis=cause.basis,
        rationale=rationale("This moved the draft forward."),
    )
    with pytest.raises(ValueError, match="not allowed in authority lane"):
        goal_payload(
            after,
            operation="progress",
            lane="settlement",
            cause=settled,
            before=before,
            progress_delta_bp=1000,
            progress_assessment=assessment,
        )
    payload = goal_payload(
        after,
        operation="progress",
        lane="deliberative",
        cause=cause,
        before=before,
        progress_delta_bp=1000,
        progress_assessment=assessment,
    )
    stale_current = before.model_copy(update={"entity_revision": 2})
    with pytest.raises(ValueError, match="before image"):
        reduce_v2_goal(
            (stale_current,),
            (),
            payload,
            event_type="V2GoalProgressed",
            event_id=after.origin.accepted_event_ref,
            logical_time=NOW,
            actor_authorities=(),
            committed_events=(source,),
            random_draws=(),
            world_occurrences=(occurrence,),
        )


def test_open_rejects_private_other_person_occurrence() -> None:
    source, occurrence, _ = settled_occurrence(index=11)
    occurrence = occurrence.model_copy(update={"participant_refs": ("actor:other",)})
    cause = deliberative_occurrence_cause(source, occurrence)
    after = goal_projection(
        revision=1,
        values=V2GoalValues(
            outcome_ref="outcome:publish-story",
            importance_bp=8000,
            progress_bp=0,
            privacy_class="private",
            status="active",
        ),
        event_ref="event:goal:private-other-open",
        opened_at=NOW,
    )
    payload = goal_payload(after, operation="open", lane="deliberative", cause=cause)
    with pytest.raises(ValueError, match="accessible exact settled occurrence"):
        reduce_v2_goal(
            (),
            (),
            payload,
            event_type="V2GoalOpened",
            event_id=after.origin.accepted_event_ref,
            logical_time=NOW,
            actor_authorities=(),
            committed_events=(source,),
            random_draws=(),
            world_occurrences=(occurrence,),
        )


def test_internal_intention_rejects_uninstalled_policy_and_withhold_downgrade() -> None:
    after = goal_projection(
        revision=1,
        values=V2GoalValues(
            outcome_ref="outcome:publish-story",
            importance_bp=8000,
            progress_bp=0,
            privacy_class="private",
            status="active",
        ),
        event_ref="event:goal:internal-open",
        opened_at=NOW,
    )
    wrong_policy = goal_payload(
        after,
        operation="open",
        lane="deliberative",
        cause=internal_cause(policy_digest="f" * 64),
    )
    with pytest.raises(ValueError, match="internal intention cannot authorize"):
        reduce_v2_goal(
            (),
            (),
            wrong_policy,
            event_type="V2GoalOpened",
            event_id=after.origin.accepted_event_ref,
            logical_time=NOW,
            actor_authorities=(),
            committed_events=(),
            random_draws=(),
            world_occurrences=(),
        )
    withhold = goal_payload(
        after,
        operation="open",
        lane="deliberative",
        cause=internal_cause(rationale_privacy="withhold"),
    )
    with pytest.raises(ValueError, match="subjective rationale"):
        reduce_v2_goal(
            (),
            (),
            withhold,
            event_type="V2GoalOpened",
            event_id=after.origin.accepted_event_ref,
            logical_time=NOW,
            actor_authorities=(),
            committed_events=(),
            random_draws=(),
            world_occurrences=(),
        )


def test_open_rejects_superseding_nonterminal_goal() -> None:
    source, occurrence, _ = settled_occurrence(index=12)
    cause = deliberative_occurrence_cause(source, occurrence)
    target = goal_projection(
        revision=1,
        values=V2GoalValues(
            outcome_ref="outcome:old-story",
            importance_bp=5000,
            progress_bp=2000,
            privacy_class="private",
            status="active",
        ),
        event_ref="event:goal:old-head",
        goal_id="goal:old-story",
        updated_at=NOW - timedelta(hours=1),
    )
    target_event = CommittedWorldEventRef(
        event_id=target.origin.accepted_event_ref,
        event_type="V2GoalOpened",
        world_revision=6,
        payload_hash="c" * 64,
        logical_time=target.updated_at,
    )
    authority = V2GoalSupersedesAuthority(
        goal_id=target.goal_id,
        actor_ref=target.actor_ref,
        entity_revision=target.entity_revision,
        accepted_event_ref=target.origin.accepted_event_ref,
        accepted_world_revision=target_event.world_revision,
        accepted_payload_hash=target_event.payload_hash,
        target_head_semantic_hash=target.semantic_fingerprint,
        privacy_class=target.values.privacy_class,
    )
    after = goal_projection(
        revision=1,
        values=V2GoalValues(
            outcome_ref="outcome:publish-story",
            importance_bp=8000,
            progress_bp=0,
            privacy_class="private",
            status="active",
            supersedes_goal_id=target.goal_id,
            supersedes_goal_authority=authority,
        ),
        event_ref="event:goal:superseding-open",
        goal_id="goal:new-story",
        opened_at=NOW,
    )
    payload = goal_payload(after, operation="open", lane="deliberative", cause=cause)
    with pytest.raises(ValueError, match="supersede lineage"):
        reduce_v2_goal(
            (target,),
            (),
            payload,
            event_type="V2GoalOpened",
            event_id=after.origin.accepted_event_ref,
            logical_time=NOW,
            actor_authorities=(),
            committed_events=(target_event, source),
            random_draws=(),
            world_occurrences=(occurrence,),
        )


def test_abandon_persists_structured_terminal_reason() -> None:
    cause = internal_cause()
    before = goal_projection(
        revision=1,
        values=V2GoalValues(
            outcome_ref="outcome:publish-story",
            importance_bp=8000,
            progress_bp=2000,
            privacy_class="private",
            status="active",
        ),
        event_ref="event:goal:before-abandon",
        updated_at=NOW - timedelta(hours=1),
    )
    reason = V2GoalLifecycleReason(
        reason_kind="values_changed",
        rationale=rationale("I no longer want to tell the story in this form."),
        basis=cause.basis,
        privacy_class="private",
    )
    terminal = V2GoalAbandonedTerminalReason(reason=reason)
    after = goal_projection(
        revision=2,
        values=before.values.model_copy(
            update={"status": "abandoned", "terminal_reason": terminal}
        ),
        event_ref="event:goal:abandoned",
    )
    payload = goal_payload(
        after,
        operation="abandon",
        lane="deliberative",
        cause=cause,
        before=before,
        lifecycle_reason=reason,
        terminal_reason=terminal,
    )

    heads, history = reduce_v2_goal(
        (before,),
        (),
        payload,
        event_type="V2GoalAbandoned",
        event_id=after.origin.accepted_event_ref,
        logical_time=NOW,
        actor_authorities=(),
        committed_events=(),
        random_draws=(),
        world_occurrences=(),
    )
    assert heads[0].values.terminal_reason == terminal


def test_complete_resolves_exact_post_cutoff_occurrence_outcome() -> None:
    source, occurrence, _ = settled_occurrence(index=13)
    occurrence = occurrence.model_copy(
        update={"settled_outcome_ref": "outcome:publish-story"}
    )
    cause = deliberative_occurrence_cause(source, occurrence)
    contract = completion_contract()
    before = goal_projection(
        revision=1,
        values=V2GoalValues(
            outcome_ref="outcome:publish-story",
            importance_bp=8000,
            progress_bp=9000,
            privacy_class="private",
            completion_contract=contract,
            status="active",
        ),
        event_ref="event:goal:before-complete",
        updated_at=NOW - timedelta(hours=1),
    )
    evidence = V2GoalOccurrenceCompletionEvidence(
        evidence_ref=source.event_id,
        evidence_world_revision=source.world_revision,
        evidence_payload_hash=source.payload_hash,
        evidence_schema_ref="world-occurrence-settlement.1",
        occurrence_id=occurrence.occurrence_id,
        occurrence_entity_revision=occurrence.entity_revision,
        resolved_actor_ref="actor:companion",
        resolved_outcome_ref="outcome:publish-story",
        privacy_class="private",
    )
    terminal = V2GoalCompletedTerminalReason(
        contract_id=contract.contract_id,
        contract_digest=contract.contract_digest,
        completion_evidence_ref=evidence.evidence_ref,
        privacy_class="private",
    )
    after = goal_projection(
        revision=2,
        values=before.values.model_copy(
            update={"status": "completed", "terminal_reason": terminal}
        ),
        event_ref="event:goal:completed",
    )
    payload = goal_payload(
        after,
        operation="complete",
        lane="deliberative",
        cause=cause,
        before=before,
        completion_evidence=evidence,
        terminal_reason=terminal,
    )

    heads, history = reduce_v2_goal(
        (before,),
        (),
        payload,
        event_type="V2GoalCompleted",
        event_id=after.origin.accepted_event_ref,
        logical_time=NOW,
        actor_authorities=(),
        committed_events=(source,),
        random_draws=(),
        world_occurrences=(occurrence,),
    )
    assert heads[0].values.status == "completed"
    assert history[-1].completion_evidence == evidence

    correction_event = CommittedWorldEventRef(
        event_id="event:fact:occurrence-completion-correction",
        event_type="FactCommitted",
        world_revision=9,
        payload_hash="3" * 64,
        logical_time=NOW + timedelta(minutes=1),
    )
    correction_fact = active_completion_fact(
        event_ref=correction_event.event_id,
        predicate="occurrence.settlement.disputed",
        value_ref="value:disputed",
        value_hash="3" * 64,
    )
    correction_basis = CommittedEvidenceBasis(
        sources=(
            CommittedEvidenceSource(
                source_kind="fact",
                event_ref=correction_event.event_id,
                world_revision=correction_event.world_revision,
                payload_hash=correction_event.payload_hash,
                source_entity_ref=correction_fact.fact_id,
                source_entity_revision=correction_fact.entity_revision,
            ),
        )
    )
    target_event = CommittedWorldEventRef(
        event_id=after.origin.accepted_event_ref,
        event_type="V2GoalCompleted",
        world_revision=8,
        payload_hash="2" * 64,
        logical_time=NOW,
    )
    compensation = CompensationCauseAuthority(
        target_transition_id=history[-1].transition_id,
        target_entity_revision=history[-1].entity_revision,
        target_accepted_event_ref=target_event.event_id,
        target_accepted_world_revision=target_event.world_revision,
        target_accepted_payload_hash=target_event.payload_hash,
        target_authority_lane="deliberative",
        correction_basis=correction_basis,
        correction_rationale=rationale(
            "The occurrence settlement now has a committed governance dispute."
        ),
    )
    restored = goal_projection(
        revision=3,
        values=before.values,
        event_ref="event:goal:occurrence-completion-compensated",
        updated_at=NOW + timedelta(minutes=1),
    )
    compensation_payload = goal_payload(
        restored,
        operation="compensate",
        lane="compensation",
        cause=compensation,
        before=after,
        compensation_target=compensation,
        evaluated_world_revision=9,
    )
    with pytest.raises(ValueError, match="effective operator lane"):
        reduce_v2_goal(
            (after,),
            history,
            compensation_payload,
            event_type="V2GoalTransitionCompensated",
            event_id=restored.origin.accepted_event_ref,
            logical_time=restored.updated_at,
            actor_authorities=(),
            committed_events=(target_event, correction_event),
            random_draws=(),
            world_occurrences=(),
            facts=(correction_fact,),
        )

    authority, authority_event, operator_binding = operator_authority()
    governed = compensation.model_copy(
        update={"operator_authority": operator_binding}
    )
    governed_payload = goal_payload(
        restored,
        operation="compensate",
        lane="compensation",
        cause=governed,
        before=after,
        compensation_target=governed,
        evaluated_world_revision=9,
    )
    restored_heads, compensated_history = reduce_v2_goal(
        (after,),
        history,
        governed_payload,
        event_type="V2GoalTransitionCompensated",
        event_id=restored.origin.accepted_event_ref,
        logical_time=restored.updated_at,
        actor_authorities=(authority,),
        committed_events=(authority_event, target_event, correction_event),
        random_draws=(),
        world_occurrences=(),
        facts=(correction_fact,),
    )
    assert restored_heads == (restored,)

    first_compensation_event = CommittedWorldEventRef(
        event_id=restored.origin.accepted_event_ref,
        event_type="V2GoalTransitionCompensated",
        world_revision=10,
        payload_hash="1" * 64,
        logical_time=restored.updated_at,
    )
    restore_time = NOW + timedelta(minutes=2)
    restore_cause = CompensationCauseAuthority(
        target_transition_id=compensated_history[-1].transition_id,
        target_entity_revision=compensated_history[-1].entity_revision,
        target_accepted_event_ref=first_compensation_event.event_id,
        target_accepted_world_revision=first_compensation_event.world_revision,
        target_accepted_payload_hash=first_compensation_event.payload_hash,
        target_authority_lane="deliberative",
        correction_basis=correction_basis,
        correction_rationale=rationale(
            "The governed correction was itself mistaken, and settlement remains exact."
        ),
        operator_authority=operator_binding,
    )
    completed_again = goal_projection(
        revision=4,
        values=after.values,
        event_ref="event:goal:occurrence-completion-restored",
        updated_at=restore_time,
    )
    restore_payload = goal_payload(
        completed_again,
        operation="compensate",
        lane="compensation",
        cause=restore_cause,
        before=restored,
        compensation_target=restore_cause,
        evaluated_world_revision=10,
    )
    completed_heads, _ = reduce_v2_goal(
        (restored,),
        compensated_history,
        restore_payload,
        event_type="V2GoalTransitionCompensated",
        event_id=completed_again.origin.accepted_event_ref,
        logical_time=restore_time,
        actor_authorities=(authority,),
        committed_events=(
            authority_event,
            first_compensation_event,
            correction_event,
            source,
        ),
        random_draws=(),
        world_occurrences=(occurrence,),
        facts=(correction_fact,),
    )
    assert completed_heads == (completed_again,)

    attacked_occurrences = (
        (),
        (occurrence, occurrence.model_copy()),
        (occurrence.model_copy(update={"settlement_payload_hash": "0" * 64}),),
        (occurrence.model_copy(update={"status": "active"}),),
        (occurrence.model_copy(update={"settled_outcome_ref": "outcome:other"}),),
    )
    for current_occurrences in attacked_occurrences:
        with pytest.raises(ValueError, match="no longer exact and current"):
            reduce_v2_goal(
                (restored,),
                compensated_history,
                restore_payload,
                event_type="V2GoalTransitionCompensated",
                event_id=completed_again.origin.accepted_event_ref,
                logical_time=restore_time,
                actor_authorities=(authority,),
                committed_events=(
                    authority_event,
                    first_compensation_event,
                    correction_event,
                    source,
                ),
                random_draws=(),
                world_occurrences=current_occurrences,
                facts=(correction_fact,),
            )

def test_progress_can_strengthen_privacy_to_meet_new_private_basis() -> None:
    source, occurrence, _ = settled_occurrence(index=14, privacy="private")
    cause = deliberative_occurrence_cause(source, occurrence)
    before = goal_projection(
        revision=1,
        values=V2GoalValues(
            outcome_ref="outcome:publish-story",
            importance_bp=8000,
            progress_bp=1000,
            privacy_class="public",
            status="active",
        ),
        event_ref="event:goal:before-privacy-upgrade",
        updated_at=NOW - timedelta(hours=1),
    )
    after = goal_projection(
        revision=2,
        values=before.values.model_copy(
            update={"progress_bp": 2000, "privacy_class": "private"}
        ),
        event_ref="event:goal:privacy-upgraded",
    )
    payload = goal_payload(
        after,
        operation="progress",
        lane="deliberative",
        cause=cause,
        before=before,
        progress_delta_bp=1000,
        progress_assessment=V2GoalProgressAssessment(
            contribution_class="direct_contribution",
            basis=cause.basis,
            rationale=rationale("This private experience moved my draft forward."),
        ),
    )

    heads, history = reduce_v2_goal(
        (before,),
        (),
        payload,
        event_type="V2GoalProgressed",
        event_id=after.origin.accepted_event_ref,
        logical_time=NOW,
        actor_authorities=(),
        committed_events=(source,),
        random_draws=(),
        world_occurrences=(occurrence,),
    )
    assert heads[0].values.privacy_class == "private"


def test_unblock_resolves_each_resolution_basis_exactly() -> None:
    source, occurrence, _ = settled_occurrence(index=15)
    cause = deliberative_occurrence_cause(source, occurrence)
    existing = blocker(
        blocker_id="blocker:feedback",
        blocker_class="external_dependency",
        basis=cause.basis,
        text="I am waiting for feedback.",
    )
    before = goal_projection(
        revision=1,
        values=V2GoalValues(
            outcome_ref="outcome:publish-story",
            importance_bp=8000,
            progress_bp=1000,
            blockers=(existing,),
            privacy_class="private",
            status="blocked",
        ),
        event_ref="event:goal:before-bogus-resolution",
        updated_at=NOW - timedelta(hours=1),
    )
    after = goal_projection(
        revision=2,
        values=before.values.model_copy(update={"blockers": (), "status": "active"}),
        event_ref="event:goal:bogus-resolution",
    )
    bogus_basis = CommittedEvidenceBasis(
        sources=(
            CommittedEvidenceSource(
                source_kind="settled_world_event",
                event_ref="event:settled:missing",
                world_revision=6,
                payload_hash="f" * 64,
                source_entity_ref=occurrence.occurrence_id,
                source_entity_revision=occurrence.entity_revision,
            ),
        )
    )
    payload = goal_payload(
        after,
        operation="unblock",
        lane="deliberative",
        cause=cause,
        before=before,
        blocker_resolutions=(
            V2GoalBlockerResolution(
                blocker_id=existing.blocker_id,
                blocker_semantic_hash=existing.blocker_semantic_hash,
                resolution_class="externally_resolved",
                rationale=rationale("The feedback arrived."),
                basis=bogus_basis,
            ),
        ),
    )
    with pytest.raises(ValueError, match="lacks exact committed authority"):
        reduce_v2_goal(
            (before,),
            (),
            payload,
            event_type="V2GoalUnblocked",
            event_id=after.origin.accepted_event_ref,
            logical_time=NOW,
            actor_authorities=(),
            committed_events=(source,),
            random_draws=(),
            world_occurrences=(occurrence,),
        )


def test_complete_rejects_evidence_at_contract_cutoff() -> None:
    source, occurrence, _ = settled_occurrence(index=16)
    occurrence = occurrence.model_copy(
        update={"settled_outcome_ref": "outcome:publish-story"}
    )
    cause = deliberative_occurrence_cause(source, occurrence)
    contract = completion_contract(cutoff=7)
    before = goal_projection(
        revision=1,
        values=V2GoalValues(
            outcome_ref="outcome:publish-story",
            importance_bp=8000,
            progress_bp=9000,
            privacy_class="private",
            completion_contract=contract,
            status="active",
        ),
        event_ref="event:goal:before-cutoff-complete",
        updated_at=NOW - timedelta(hours=1),
    )
    evidence = V2GoalOccurrenceCompletionEvidence(
        evidence_ref=source.event_id,
        evidence_world_revision=source.world_revision,
        evidence_payload_hash=source.payload_hash,
        evidence_schema_ref="world-occurrence-settlement.1",
        occurrence_id=occurrence.occurrence_id,
        occurrence_entity_revision=occurrence.entity_revision,
        resolved_actor_ref="actor:companion",
        resolved_outcome_ref="outcome:publish-story",
        privacy_class="private",
    )
    terminal = V2GoalCompletedTerminalReason(
        contract_id=contract.contract_id,
        contract_digest=contract.contract_digest,
        completion_evidence_ref=evidence.evidence_ref,
        privacy_class="private",
    )
    after = goal_projection(
        revision=2,
        values=before.values.model_copy(
            update={"status": "completed", "terminal_reason": terminal}
        ),
        event_ref="event:goal:cutoff-complete",
    )
    payload = goal_payload(
        after,
        operation="complete",
        lane="deliberative",
        cause=cause,
        before=before,
        completion_evidence=evidence,
        terminal_reason=terminal,
    )
    with pytest.raises(ValueError, match="post-cutoff"):
        reduce_v2_goal(
            (before,),
            (),
            payload,
            event_type="V2GoalCompleted",
            event_id=after.origin.accepted_event_ref,
            logical_time=NOW,
            actor_authorities=(),
            committed_events=(source,),
            random_draws=(),
            world_occurrences=(occurrence,),
        )


def test_complete_rejects_duplicate_current_occurrence_heads() -> None:
    source, occurrence, _ = settled_occurrence(index=17)
    occurrence = occurrence.model_copy(
        update={"settled_outcome_ref": "outcome:publish-story"}
    )
    cause = deliberative_occurrence_cause(source, occurrence)
    contract = completion_contract(cutoff=6)
    before = goal_projection(
        revision=1,
        values=V2GoalValues(
            outcome_ref="outcome:publish-story",
            importance_bp=8000,
            progress_bp=9000,
            privacy_class="private",
            completion_contract=contract,
            status="active",
        ),
        event_ref="event:goal:before-duplicate-complete",
        updated_at=NOW - timedelta(hours=1),
    )
    evidence = V2GoalOccurrenceCompletionEvidence(
        evidence_ref=source.event_id,
        evidence_world_revision=source.world_revision,
        evidence_payload_hash=source.payload_hash,
        evidence_schema_ref="world-occurrence-settlement.1",
        occurrence_id=occurrence.occurrence_id,
        occurrence_entity_revision=occurrence.entity_revision,
        resolved_actor_ref="actor:companion",
        resolved_outcome_ref="outcome:publish-story",
        privacy_class="private",
    )
    terminal = V2GoalCompletedTerminalReason(
        contract_id=contract.contract_id,
        contract_digest=contract.contract_digest,
        completion_evidence_ref=evidence.evidence_ref,
        privacy_class="private",
    )
    after = goal_projection(
        revision=2,
        values=before.values.model_copy(
            update={"status": "completed", "terminal_reason": terminal}
        ),
        event_ref="event:goal:duplicate-complete",
    )
    payload = goal_payload(
        after,
        operation="complete",
        lane="deliberative",
        cause=cause,
        before=before,
        completion_evidence=evidence,
        terminal_reason=terminal,
    )
    with pytest.raises(ValueError, match="occurrence completion"):
        reduce_v2_goal(
            (before,),
            (),
            payload,
            event_type="V2GoalCompleted",
            event_id=after.origin.accepted_event_ref,
            logical_time=NOW,
            actor_authorities=(),
            committed_events=(source,),
            random_draws=(),
            world_occurrences=(occurrence, occurrence.model_copy()),
        )


def test_expire_is_independent_mechanical_clock_transition() -> None:
    due = V2GoalDueWindow(
        starts_at=NOW - timedelta(days=1),
        ends_at=NOW,
    )
    basis = internal_cause().basis
    active_blocker = blocker(
        blocker_id="blocker:expiry",
        blocker_class="resource_constraint",
        basis=basis,
        text="This remained blocked until the due window elapsed.",
    )
    before = goal_projection(
        revision=1,
        values=V2GoalValues(
            outcome_ref="outcome:publish-story",
            importance_bp=8000,
            progress_bp=4000,
            due_window=due,
            blockers=(active_blocker,),
            privacy_class="private",
            status="blocked",
        ),
        event_ref="event:goal:before-expiry",
        updated_at=NOW - timedelta(hours=1),
    )
    clock = ClockTransitionProjection(
        clock_event_ref="event:clock:goal-expiry",
        computed_world_revision=8,
        payload_hash="8" * 64,
        logical_time_from=NOW - timedelta(minutes=2),
        logical_time_to=NOW,
        installed_policy_version=CLOCK_AUTHORITY_POLICY_VERSION,
        installed_policy_digest=CLOCK_AUTHORITY_POLICY_DIGEST,
    )
    clock_authority_event = CommittedWorldEventRef(
        event_id=clock.clock_event_ref,
        event_type="ClockAdvanced",
        world_revision=clock.computed_world_revision,
        payload_hash=clock.payload_hash,
        logical_time=clock.logical_time_to,
    )
    terminal = V2GoalExpiredTerminalReason(
        due_window=due,
        clock_projection_ref=clock.clock_event_ref,
        policy_digest=V2_GOAL_EXPIRY_POLICY_DIGEST,
        privacy_class="private",
    )
    after = goal_projection(
        revision=2,
        values=before.values.model_copy(
            update={
                "status": "expired",
                "blockers": (),
                "terminal_reason": terminal,
            }
        ),
        event_ref="event:goal:expired",
    )
    payload = goal_expiry_payload(before, after, clock=clock, terminal=terminal)
    dumped = payload.model_dump(mode="json")
    assert "proposal_id" not in dumped
    assert "acceptance_id" not in dumped
    assert "evidence_refs" not in dumped
    assert "accepted_change_hash" not in dumped
    identity = domain_idempotency_key(
        event_type="V2GoalExpired",
        world_id="world:test",
        payload=dumped,
    )
    rewritten_audit_ids = {
        **dumped,
        "change_id": "change:rewritten",
        "transition_id": "transition:rewritten",
        "mechanical_change_hash": "0" * 64,
    }
    assert identity == domain_idempotency_key(
        event_type="V2GoalExpired",
        world_id="world:test",
        payload=rewritten_audit_ids,
    )
    other_goal = json.loads(json.dumps(dumped))
    other_goal["goal_after"]["goal_id"] = "goal:another"
    assert identity != domain_idempotency_key(
        event_type="V2GoalExpired",
        world_id="world:test",
        payload=other_goal,
    )

    heads, history = reduce_v2_goal_expiry(
        (before,),
        (),
        payload,
        event_type="V2GoalExpired",
        event_id=after.origin.accepted_event_ref,
        logical_time=NOW,
        clock_transition_history=(clock,),
    )
    assert heads == (after,)
    assert history[-1].authority_lane == "clock_runtime"
    assert history[-1].removed_blocker_fingerprints == (
        active_blocker.blocker_semantic_hash,
    )

    wrong_removed = payload.model_copy(update={"removed_blocker_fingerprints": ()})
    with pytest.raises(ValueError, match="clear exact current blockers"):
        reduce_v2_goal_expiry(
            (before,),
            (),
            wrong_removed,
            event_type="V2GoalExpired",
            event_id=after.origin.accepted_event_ref,
            logical_time=NOW,
            clock_transition_history=(clock,),
        )

    stale_clock = clock.model_copy(
        update={"logical_time_to": NOW - timedelta(seconds=1)}
    )
    with pytest.raises(ValueError, match="not installed or current"):
        reduce_v2_goal_expiry(
            (before,),
            (),
            payload,
            event_type="V2GoalExpired",
            event_id=after.origin.accepted_event_ref,
            logical_time=NOW,
            clock_transition_history=(stale_clock,),
        )

    forged_cause = payload.cause_authority.model_copy(
        update={"clock_payload_hash": "0" * 64}
    )
    forged_payload = payload.model_copy(update={"cause_authority": forged_cause})
    with pytest.raises(ValueError, match="exact latest Clock"):
        reduce_v2_goal_expiry(
            (before,),
            (),
            forged_payload,
            event_type="V2GoalExpired",
            event_id=after.origin.accepted_event_ref,
            logical_time=NOW,
            clock_transition_history=(clock,),
        )

    with pytest.raises(ValueError, match="identity already exists"):
        reduce_v2_goal_expiry(
            (before,),
            history,
            payload,
            event_type="V2GoalExpired",
            event_id=after.origin.accepted_event_ref,
            logical_time=NOW,
            clock_transition_history=(clock,),
        )

    authority, authority_event, operator_binding = operator_authority()
    target_event = CommittedWorldEventRef(
        event_id=after.origin.accepted_event_ref,
        event_type="V2GoalExpired",
        world_revision=9,
        payload_hash="5" * 64,
        logical_time=NOW,
    )
    correction_event = CommittedWorldEventRef(
        event_id="event:fact:expiry-correction",
        event_type="FactCommitted",
        world_revision=10,
        payload_hash="4" * 64,
        logical_time=NOW + timedelta(minutes=1),
    )
    correction_fact = active_completion_fact(
        event_ref=correction_event.event_id,
        predicate="goal.expiry.import_error",
        value_ref="value:expiry-import-error",
        value_hash="4" * 64,
    )
    committed_sources = CommittedEvidenceBasis(
        sources=(
            CommittedEvidenceSource(
                source_kind="fact",
                event_ref=correction_event.event_id,
                world_revision=correction_event.world_revision,
                payload_hash=correction_event.payload_hash,
                source_entity_ref=correction_fact.fact_id,
                source_entity_revision=correction_fact.entity_revision,
            ),
        )
    )
    correction_rationale = rationale(
        "A post-expiry authority record shows the imported due window was wrong."
    )
    expiry_correction = GoalExpiryCorrectionBasis(
        target_expiry_transition_id=history[-1].transition_id,
        target_expiry_event_ref=target_event.event_id,
        target_expiry_world_revision=target_event.world_revision,
        target_expiry_payload_hash=target_event.payload_hash,
        original_clock=payload.cause_authority,
        operator_authority=operator_binding,
        correction_class="operator_import_error",
        sources=committed_sources,
        rationale=correction_rationale,
        privacy_class="private",
        policy_version=V2_GOAL_EXPIRY_CORRECTION_POLICY_VERSION,
        policy_digest=V2_GOAL_EXPIRY_CORRECTION_POLICY_DIGEST,
    )
    compensation = CompensationCauseAuthority(
        target_transition_id=history[-1].transition_id,
        target_entity_revision=history[-1].entity_revision,
        target_accepted_event_ref=target_event.event_id,
        target_accepted_world_revision=target_event.world_revision,
        target_accepted_payload_hash=target_event.payload_hash,
        target_authority_lane="clock_runtime",
        correction_basis=expiry_correction,
        correction_rationale=correction_rationale,
        operator_authority=operator_binding,
    )
    restored = goal_projection(
        revision=3,
        values=before.values,
        event_ref="event:goal:expiry-compensated",
        updated_at=NOW + timedelta(minutes=1),
    )
    compensation_payload = goal_payload(
        restored,
        operation="compensate",
        lane="compensation",
        cause=compensation,
        before=after,
        compensation_target=compensation,
        evaluated_world_revision=10,
    )
    restored_heads, compensated_history = reduce_v2_goal(
        (after,),
        history,
        compensation_payload,
        event_type="V2GoalTransitionCompensated",
        event_id=restored.origin.accepted_event_ref,
        logical_time=restored.updated_at,
        actor_authorities=(authority,),
        committed_events=(
            authority_event,
            clock_authority_event,
            target_event,
            correction_event,
        ),
        random_draws=(),
        world_occurrences=(),
        facts=(correction_fact,),
        clock_transition_history=(clock,),
    )
    assert restored_heads == (restored,)

    generic_cause = compensation.model_copy(
        update={"correction_basis": committed_sources}
    )
    generic_payload = goal_payload(
        restored,
        operation="compensate",
        lane="compensation",
        cause=generic_cause,
        before=after,
        compensation_target=generic_cause,
        evaluated_world_revision=10,
    )
    with pytest.raises(ValueError, match="typed correction"):
        reduce_v2_goal(
            (after,),
            history,
            generic_payload,
            event_type="V2GoalTransitionCompensated",
            event_id=restored.origin.accepted_event_ref,
            logical_time=restored.updated_at,
            actor_authorities=(authority,),
            committed_events=(
                authority_event,
                clock_authority_event,
                target_event,
                correction_event,
            ),
            random_draws=(),
            world_occurrences=(),
            facts=(correction_fact,),
            clock_transition_history=(clock,),
        )

    first_compensation_event = CommittedWorldEventRef(
        event_id=restored.origin.accepted_event_ref,
        event_type="V2GoalTransitionCompensated",
        world_revision=11,
        payload_hash="3" * 64,
        logical_time=restored.updated_at,
    )
    second_correction_event = CommittedWorldEventRef(
        event_id="event:fact:expiry-correction:second",
        event_type="FactCorrected",
        world_revision=12,
        payload_hash="2" * 64,
        logical_time=NOW + timedelta(minutes=2),
    )
    second_origin = correction_fact.origin.model_copy(
        update={
            "change_id": "change:fact:expiry-correction:second",
            "transition_id": "transition:fact:expiry-correction:second",
            "accepted_event_ref": second_correction_event.event_id,
        }
    )
    second_fact = correction_fact.model_copy(
        update={
            "entity_revision": 2,
            "origin": second_origin,
            "updated_at": second_correction_event.logical_time,
        }
    )
    second_sources = CommittedEvidenceBasis(
        sources=(
            CommittedEvidenceSource(
                source_kind="fact",
                event_ref=second_correction_event.event_id,
                world_revision=second_correction_event.world_revision,
                payload_hash=second_correction_event.payload_hash,
                source_entity_ref=second_fact.fact_id,
                source_entity_revision=second_fact.entity_revision,
            ),
        )
    )
    second_rationale = rationale(
        "The first expiry correction was itself an operator import mistake."
    )
    second_expiry_basis = expiry_correction.model_copy(
        update={
            "sources": second_sources,
            "rationale": second_rationale,
        }
    )
    undo_cause = CompensationCauseAuthority(
        target_transition_id=compensated_history[-1].transition_id,
        target_entity_revision=compensated_history[-1].entity_revision,
        target_accepted_event_ref=first_compensation_event.event_id,
        target_accepted_world_revision=first_compensation_event.world_revision,
        target_accepted_payload_hash=first_compensation_event.payload_hash,
        target_authority_lane="clock_runtime",
        correction_basis=second_expiry_basis,
        correction_rationale=second_rationale,
        operator_authority=operator_binding,
    )
    expired_again = goal_projection(
        revision=4,
        values=after.values,
        event_ref="event:goal:expiry-compensation-undone",
        updated_at=second_correction_event.logical_time,
    )
    undo_payload = goal_payload(
        expired_again,
        operation="compensate",
        lane="compensation",
        cause=undo_cause,
        before=restored,
        compensation_target=undo_cause,
        evaluated_world_revision=12,
    )
    expired_heads, _ = reduce_v2_goal(
        (restored,),
        compensated_history,
        undo_payload,
        event_type="V2GoalTransitionCompensated",
        event_id=expired_again.origin.accepted_event_ref,
        logical_time=expired_again.updated_at,
        actor_authorities=(authority,),
        committed_events=(
            authority_event,
            clock_authority_event,
            target_event,
            first_compensation_event,
            second_correction_event,
        ),
        random_draws=(),
        world_occurrences=(),
        facts=(second_fact,),
        clock_transition_history=(clock,),
    )
    assert expired_heads == (expired_again,)
    assert expired_heads[0].closed_at == second_correction_event.logical_time
    assert expired_heads[0].values.terminal_reason == terminal

    for attacked_clock_history in (
        (),
        (clock.model_copy(update={"payload_hash": "0" * 64}),),
    ):
        with pytest.raises(ValueError, match="exact original Clock"):
            reduce_v2_goal(
                (restored,),
                compensated_history,
                undo_payload,
                event_type="V2GoalTransitionCompensated",
                event_id=expired_again.origin.accepted_event_ref,
                logical_time=expired_again.updated_at,
                actor_authorities=(authority,),
                committed_events=(
                    authority_event,
                    clock_authority_event,
                    target_event,
                    first_compensation_event,
                    second_correction_event,
                ),
                random_draws=(),
                world_occurrences=(),
                facts=(second_fact,),
                clock_transition_history=attacked_clock_history,
            )

    for incorrect_privacy in ("public", "withhold"):
        wrong_privacy_basis = expiry_correction.model_copy(
            update={"privacy_class": incorrect_privacy}
        )
        wrong_privacy_cause = compensation.model_copy(
            update={"correction_basis": wrong_privacy_basis}
        )
        wrong_privacy_payload = goal_payload(
            restored,
            operation="compensate",
            lane="compensation",
            cause=wrong_privacy_cause,
            before=after,
            compensation_target=wrong_privacy_cause,
            evaluated_world_revision=10,
        )
        with pytest.raises(ValueError, match="privacy is not exactly derived"):
            reduce_v2_goal(
                (after,),
                history,
                wrong_privacy_payload,
                event_type="V2GoalTransitionCompensated",
                event_id=restored.origin.accepted_event_ref,
                logical_time=restored.updated_at,
                actor_authorities=(authority,),
                committed_events=(
                    authority_event,
                    clock_authority_event,
                    target_event,
                    correction_event,
                ),
                random_draws=(),
                world_occurrences=(),
                facts=(correction_fact,),
                clock_transition_history=(clock,),
            )

    for unsupported_class in ("clock_transition", "policy_application"):
        unsupported_basis = expiry_correction.model_copy(
            update={"correction_class": unsupported_class}
        )
        unsupported_cause = compensation.model_copy(
            update={"correction_basis": unsupported_basis}
        )
        unsupported_payload = goal_payload(
            restored,
            operation="compensate",
            lane="compensation",
            cause=unsupported_cause,
            before=after,
            compensation_target=unsupported_cause,
            evaluated_world_revision=10,
        )
        with pytest.raises(ValueError, match="not exact and post-target"):
            reduce_v2_goal(
                (after,),
                history,
                unsupported_payload,
                event_type="V2GoalTransitionCompensated",
                event_id=restored.origin.accepted_event_ref,
                logical_time=restored.updated_at,
                actor_authorities=(authority,),
                committed_events=(
                    authority_event,
                    clock_authority_event,
                    target_event,
                    correction_event,
                ),
                random_draws=(),
                world_occurrences=(),
                facts=(correction_fact,),
                clock_transition_history=(clock,),
            )

    wrong_authority_basis = expiry_correction.model_copy(
        update={
            "operator_authority": operator_binding.model_copy(
                update={"authority_revision": 2}
            )
        }
    )
    wrong_authority_cause = compensation.model_copy(
        update={"correction_basis": wrong_authority_basis}
    )
    wrong_authority_payload = goal_payload(
        restored,
        operation="compensate",
        lane="compensation",
        cause=wrong_authority_cause,
        before=after,
        compensation_target=wrong_authority_cause,
        evaluated_world_revision=10,
    )
    with pytest.raises(ValueError, match="not exact and post-target"):
        reduce_v2_goal(
            (after,),
            history,
            wrong_authority_payload,
            event_type="V2GoalTransitionCompensated",
            event_id=restored.origin.accepted_event_ref,
            logical_time=restored.updated_at,
            actor_authorities=(authority,),
            committed_events=(
                authority_event,
                clock_authority_event,
                target_event,
                correction_event,
            ),
            random_draws=(),
            world_occurrences=(),
            facts=(correction_fact,),
            clock_transition_history=(clock,),
        )

    stale_source = committed_sources.sources[0].model_copy(
        update={"world_revision": target_event.world_revision}
    )
    stale_basis = expiry_correction.model_copy(
        update={"sources": CommittedEvidenceBasis(sources=(stale_source,))}
    )
    stale_cause = compensation.model_copy(update={"correction_basis": stale_basis})
    stale_payload = goal_payload(
        restored,
        operation="compensate",
        lane="compensation",
        cause=stale_cause,
        before=after,
        compensation_target=stale_cause,
        evaluated_world_revision=10,
    )
    with pytest.raises(ValueError, match="not exact and post-target"):
        reduce_v2_goal(
            (after,),
            history,
            stale_payload,
            event_type="V2GoalTransitionCompensated",
            event_id=restored.origin.accepted_event_ref,
            logical_time=restored.updated_at,
            actor_authorities=(authority,),
            committed_events=(
                authority_event,
                clock_authority_event,
                target_event,
                correction_event,
            ),
            random_draws=(),
            world_occurrences=(),
            facts=(correction_fact,),
            clock_transition_history=(clock,),
        )


@pytest.mark.parametrize("status", ("active", "paused", "blocked"))
def test_expire_supports_each_nonterminal_status_and_rejects_not_due(
    status: str,
) -> None:
    due = V2GoalDueWindow(
        starts_at=NOW - timedelta(days=1),
        ends_at=NOW + timedelta(minutes=1),
    )
    blockers = (
        (
            blocker(
                blocker_id=f"blocker:not-due:{status}",
                blocker_class="resource_constraint",
                basis=internal_cause().basis,
                text="This goal is blocked while its due window remains open.",
            ),
        )
        if status == "blocked"
        else ()
    )
    before = goal_projection(
        revision=1,
        values=V2GoalValues(
            outcome_ref="outcome:publish-story",
            importance_bp=8000,
            progress_bp=4000,
            due_window=due,
            blockers=blockers,
            privacy_class="private",
            status=status,
        ),
        event_ref=f"event:goal:before-not-due:{status}",
        updated_at=NOW - timedelta(hours=1),
    )
    clock = ClockTransitionProjection(
        clock_event_ref=f"event:clock:not-due:{status}",
        computed_world_revision=8,
        payload_hash="7" * 64,
        logical_time_from=NOW - timedelta(minutes=1),
        logical_time_to=NOW,
        installed_policy_version=CLOCK_AUTHORITY_POLICY_VERSION,
        installed_policy_digest=CLOCK_AUTHORITY_POLICY_DIGEST,
    )
    terminal = V2GoalExpiredTerminalReason(
        due_window=due,
        clock_projection_ref=clock.clock_event_ref,
        policy_digest=V2_GOAL_EXPIRY_POLICY_DIGEST,
        privacy_class="private",
    )
    after = goal_projection(
        revision=2,
        values=before.values.model_copy(
            update={"status": "expired", "blockers": (), "terminal_reason": terminal}
        ),
        event_ref=f"event:goal:not-due-expiry:{status}",
    )
    payload = goal_expiry_payload(before, after, clock=clock, terminal=terminal)
    with pytest.raises(ValueError, match="not due"):
        reduce_v2_goal_expiry(
            (before,),
            (),
            payload,
            event_type="V2GoalExpired",
            event_id=after.origin.accepted_event_ref,
            logical_time=NOW,
            clock_transition_history=(clock,),
        )

    due_clock = clock.model_copy(
        update={
            "clock_event_ref": f"event:clock:due:{status}",
            "computed_world_revision": 9,
            "payload_hash": "6" * 64,
            "logical_time_from": NOW,
            "logical_time_to": due.ends_at,
        }
    )
    due_terminal = terminal.model_copy(
        update={"clock_projection_ref": due_clock.clock_event_ref}
    )
    due_after = goal_projection(
        revision=2,
        values=before.values.model_copy(
            update={
                "status": "expired",
                "blockers": (),
                "terminal_reason": due_terminal,
            }
        ),
        event_ref=f"event:goal:due-expiry:{status}",
        updated_at=due.ends_at,
    )
    due_payload = goal_expiry_payload(
        before, due_after, clock=due_clock, terminal=due_terminal
    )
    heads, _ = reduce_v2_goal_expiry(
        (before,),
        (),
        due_payload,
        event_type="V2GoalExpired",
        event_id=due_after.origin.accepted_event_ref,
        logical_time=due.ends_at,
        clock_transition_history=(clock, due_clock),
    )
    assert heads == (due_after,)


def test_complete_resolves_exact_active_fact_predicate() -> None:
    fact_event = CommittedWorldEventRef(
        event_id="event:fact:published-story",
        event_type="FactCommitted",
        world_revision=8,
        payload_hash="d" * 64,
        logical_time=NOW - timedelta(minutes=1),
    )
    fact = active_completion_fact(event_ref=fact_event.event_id)
    basis = CommittedEvidenceBasis(
        sources=(
            CommittedEvidenceSource(
                source_kind="fact",
                event_ref=fact_event.event_id,
                world_revision=fact_event.world_revision,
                payload_hash=fact_event.payload_hash,
                source_entity_ref=fact.fact_id,
                source_entity_revision=fact.entity_revision,
            ),
        )
    )
    cause = DeliberativeCauseAuthority(basis=basis)
    contract = completion_contract(
        cutoff=7,
        completion_kind="active_fact_predicate",
        fact_predicate=fact.values.predicate_code,
        fact_value_hash=fact.values.value_hash,
    )
    before = goal_projection(
        revision=1,
        values=V2GoalValues(
            outcome_ref="outcome:publish-story",
            importance_bp=8000,
            progress_bp=9000,
            privacy_class="private",
            completion_contract=contract,
            status="active",
        ),
        event_ref="event:goal:before-fact-complete",
        updated_at=NOW - timedelta(hours=1),
    )
    evidence = V2GoalFactCompletionEvidence(
        evidence_ref=fact_event.event_id,
        evidence_world_revision=fact_event.world_revision,
        evidence_payload_hash=fact_event.payload_hash,
        evidence_schema_ref="fact-authority.1",
        fact_id=fact.fact_id,
        fact_entity_revision=fact.entity_revision,
        resolved_actor_ref=fact.values.subject_ref,
        resolved_outcome_ref=fact.values.value_ref,
        resolved_fact_predicate=fact.values.predicate_code,
        resolved_fact_value_hash=fact.values.value_hash,
        privacy_class=fact.values.privacy_class,
    )
    terminal = V2GoalCompletedTerminalReason(
        contract_id=contract.contract_id,
        contract_digest=contract.contract_digest,
        completion_evidence_ref=evidence.evidence_ref,
        privacy_class="private",
    )
    after = goal_projection(
        revision=2,
        values=before.values.model_copy(
            update={"status": "completed", "terminal_reason": terminal}
        ),
        event_ref="event:goal:fact-complete",
    )
    payload = goal_payload(
        after,
        operation="complete",
        lane="deliberative",
        cause=cause,
        before=before,
        completion_evidence=evidence,
        terminal_reason=terminal,
        evaluated_world_revision=8,
    )
    heads, history = reduce_v2_goal(
        (before,),
        (),
        payload,
        event_type="V2GoalCompleted",
        event_id=after.origin.accepted_event_ref,
        logical_time=NOW,
        actor_authorities=(),
        committed_events=(fact_event,),
        random_draws=(),
        world_occurrences=(),
        facts=(fact,),
    )
    assert heads == (after,)

    wrong_fact = active_completion_fact(
        event_ref=fact_event.event_id,
        value_hash="a" * 64,
    )
    with pytest.raises(ValueError, match="Fact completion"):
        reduce_v2_goal(
            (before,),
            (),
            payload,
            event_type="V2GoalCompleted",
            event_id=after.origin.accepted_event_ref,
            logical_time=NOW,
            actor_authorities=(),
            committed_events=(fact_event,),
            random_draws=(),
            world_occurrences=(),
            facts=(wrong_fact,),
        )

    withdrawal_event = CommittedWorldEventRef(
        event_id="event:fact:published-story-withdrawn",
        event_type="FactWithdrawn",
        world_revision=10,
        payload_hash="c" * 64,
        logical_time=NOW + timedelta(minutes=1),
    )
    withdrawn_values = fact.values.model_copy(
        update={
            "status": "withdrawn",
            "withdrawal_reason_code": "invalid",
            "withdrawal_evidence_ref": withdrawal_event.event_id,
        }
    )
    withdrawn_origin = FactOrigin(
        change_id="change:fact:completion:withdrawn",
        transition_id="transition:fact:completion:withdrawn",
        policy_refs=fact.origin.policy_refs,
        accepted_event_ref=withdrawal_event.event_id,
    )
    withdrawn = FactProjection(
        fact_id=fact.fact_id,
        entity_revision=2,
        semantic_fingerprint=fact_semantic_fingerprint(
            subject_ref=withdrawn_values.subject_ref,
            predicate_code=withdrawn_values.predicate_code,
            cardinality=withdrawn_values.cardinality,
            conflict_key=withdrawn_values.conflict_key,
            value_hash=withdrawn_values.value_hash,
            assertion_binding=withdrawn_values.assertion_binding,
            anchor_evidence_refs=withdrawn_values.anchor_evidence_refs,
            policy_refs=withdrawn_origin.policy_refs,
        ),
        values=withdrawn_values,
        origin=withdrawn_origin,
        committed_at=fact.committed_at,
        updated_at=withdrawal_event.logical_time,
    )
    target_event = CommittedWorldEventRef(
        event_id=after.origin.accepted_event_ref,
        event_type="V2GoalCompleted",
        world_revision=9,
        payload_hash="b" * 64,
        logical_time=after.updated_at,
    )
    correction_basis = CommittedEvidenceBasis(
        sources=(
            CommittedEvidenceSource(
                source_kind="fact",
                event_ref=withdrawal_event.event_id,
                world_revision=withdrawal_event.world_revision,
                payload_hash=withdrawal_event.payload_hash,
                source_entity_ref=withdrawn.fact_id,
                source_entity_revision=withdrawn.entity_revision,
            ),
        )
    )
    compensation = CompensationCauseAuthority(
        target_transition_id=history[-1].transition_id,
        target_entity_revision=history[-1].entity_revision,
        target_accepted_event_ref=target_event.event_id,
        target_accepted_world_revision=target_event.world_revision,
        target_accepted_payload_hash=target_event.payload_hash,
        target_authority_lane="deliberative",
        correction_basis=correction_basis,
        correction_rationale=rationale(
            "The Fact that satisfied the completion contract was withdrawn."
        ),
    )
    restored = goal_projection(
        revision=3,
        values=before.values,
        event_ref="event:goal:fact-completion-compensated",
        updated_at=NOW + timedelta(minutes=1),
    )
    compensation_payload = goal_payload(
        restored,
        operation="compensate",
        lane="compensation",
        cause=compensation,
        before=after,
        compensation_target=compensation,
        evaluated_world_revision=10,
    )
    restored_heads, compensated_history = reduce_v2_goal(
        (after,),
        history,
        compensation_payload,
        event_type="V2GoalTransitionCompensated",
        event_id=restored.origin.accepted_event_ref,
        logical_time=restored.updated_at,
        actor_authorities=(),
        committed_events=(target_event, withdrawal_event),
        random_draws=(),
        world_occurrences=(),
        facts=(withdrawn,),
    )
    assert restored_heads == (restored,)

    corrected_event = withdrawal_event.model_copy(
        update={
            "event_id": "event:fact:published-story-corrected-but-still-valid",
            "event_type": "FactCorrected",
            "payload_hash": "9" * 64,
        }
    )
    corrected_origin = withdrawn_origin.model_copy(
        update={"accepted_event_ref": corrected_event.event_id}
    )
    corrected = FactProjection(
        fact_id=fact.fact_id,
        entity_revision=2,
        semantic_fingerprint=fact.semantic_fingerprint,
        values=fact.values,
        origin=corrected_origin,
        committed_at=fact.committed_at,
        updated_at=corrected_event.logical_time,
    )
    ineffective_basis = CommittedEvidenceBasis(
        sources=(
            CommittedEvidenceSource(
                source_kind="fact",
                event_ref=corrected_event.event_id,
                world_revision=corrected_event.world_revision,
                payload_hash=corrected_event.payload_hash,
                source_entity_ref=corrected.fact_id,
                source_entity_revision=corrected.entity_revision,
            ),
        )
    )
    ineffective_cause = compensation.model_copy(
        update={"correction_basis": ineffective_basis}
    )
    ineffective_payload = goal_payload(
        restored,
        operation="compensate",
        lane="compensation",
        cause=ineffective_cause,
        before=after,
        compensation_target=ineffective_cause,
        evaluated_world_revision=10,
    )
    with pytest.raises(ValueError, match="does not invalidate"):
        reduce_v2_goal(
            (after,),
            history,
            ineffective_payload,
            event_type="V2GoalTransitionCompensated",
            event_id=restored.origin.accepted_event_ref,
            logical_time=restored.updated_at,
            actor_authorities=(),
            committed_events=(target_event, corrected_event),
            random_draws=(),
            world_occurrences=(),
            facts=(corrected,),
        )

    first_compensation_event = CommittedWorldEventRef(
        event_id=restored.origin.accepted_event_ref,
        event_type="V2GoalTransitionCompensated",
        world_revision=11,
        payload_hash="8" * 64,
        logical_time=restored.updated_at,
    )
    undo_time = NOW + timedelta(minutes=2)
    undo_cause = CompensationCauseAuthority(
        target_transition_id=compensated_history[-1].transition_id,
        target_entity_revision=compensated_history[-1].entity_revision,
        target_accepted_event_ref=first_compensation_event.event_id,
        target_accepted_world_revision=first_compensation_event.world_revision,
        target_accepted_payload_hash=first_compensation_event.payload_hash,
        target_authority_lane="deliberative",
        correction_basis=correction_basis,
        correction_rationale=rationale(
            "I tried to restore completed without binding fresh completion evidence."
        ),
    )
    stale_terminal = goal_projection(
        revision=4,
        values=after.values,
        event_ref="event:goal:stale-fact-completion-restored",
        updated_at=undo_time,
    )
    undo_payload = goal_payload(
        stale_terminal,
        operation="compensate",
        lane="compensation",
        cause=undo_cause,
        before=restored,
        compensation_target=undo_cause,
        evaluated_world_revision=11,
    )
    with pytest.raises(ValueError, match="cannot be rebound"):
        reduce_v2_goal(
            (restored,),
            compensated_history,
            undo_payload,
            event_type="V2GoalTransitionCompensated",
            event_id=stale_terminal.origin.accepted_event_ref,
            logical_time=undo_time,
            actor_authorities=(),
            committed_events=(first_compensation_event, withdrawal_event),
            random_draws=(),
            world_occurrences=(),
            facts=(withdrawn,),
        )


def test_operator_open_requires_current_policy_v2_actor_authority() -> None:
    authority, authority_event, cause = operator_authority()
    after = goal_projection(
        revision=1,
        values=V2GoalValues(
            outcome_ref="outcome:publish-story",
            importance_bp=8000,
            progress_bp=0,
            privacy_class="public",
            status="active",
        ),
        event_ref="event:goal:operator-open",
        opened_at=NOW,
    )
    payload = goal_payload(after, operation="open", lane="operator", cause=cause)
    heads, history = reduce_v2_goal(
        (),
        (),
        payload,
        event_type="V2GoalOpened",
        event_id=after.origin.accepted_event_ref,
        logical_time=NOW,
        actor_authorities=(authority,),
        committed_events=(authority_event,),
        random_draws=(),
        world_occurrences=(),
    )
    assert heads == (after,)
    assert history[-1].authority_lane == "operator"

    legacy, legacy_event, legacy_cause = operator_authority(
        policy_version="actor-authority-policy.1"
    )
    legacy_payload = goal_payload(
        after, operation="open", lane="operator", cause=legacy_cause
    )
    with pytest.raises(ValueError, match="active exact ActorAuthority"):
        reduce_v2_goal(
            (),
            (),
            legacy_payload,
            event_type="V2GoalOpened",
            event_id=after.origin.accepted_event_ref,
            logical_time=NOW,
            actor_authorities=(legacy,),
            committed_events=(legacy_event,),
            random_draws=(),
            world_occurrences=(),
        )


def test_typed_goal_roundtrip_and_two_expiries_share_one_clock_authority(
    tmp_path,
) -> None:
    path = tmp_path / "goal-v16-integration.sqlite3"
    ledger = SQLiteWorldLedger(path=path, world_id="world:goal-integration")
    first_clock = ledger_event(
        "clock:goal-open",
        "ClockAdvanced",
        {
            "logical_time_from": (OPEN_TIME - timedelta(minutes=1)).isoformat(),
            "logical_time_to": OPEN_TIME.isoformat(),
        },
        logical_time=OPEN_TIME,
    )
    ledger.commit(
        [first_clock],
        expected_world_revision=0,
        expected_deliberation_revision=0,
    )
    first = record_accept_open_goal(
        ledger,
        goal_id="goal:integration:first",
        event_id="event:goal-open:first",
    )
    second = record_accept_open_goal(
        ledger,
        goal_id="goal:integration:second",
        event_id="event:goal-open:second",
    )
    assert ledger.project().goal_proposals == ()
    opened = ledger.project()
    opened_cursor = ProjectionCursor(
        world_revision=opened.world_revision,
        deliberation_revision=opened.deliberation_revision,
        ledger_sequence=opened.ledger_sequence,
    )
    duplicate_after = goal_projection(
        revision=1,
        values=first.values,
        event_ref="event:goal-open:duplicate",
        updated_at=OPEN_TIME,
        goal_id=first.goal_id,
        opened_at=OPEN_TIME,
    )
    duplicate_payload = goal_payload(
        duplicate_after,
        operation="open",
        lane="deliberative",
        cause=internal_cause(
            evaluated_world_revision=opened.world_revision,
            logical_time=OPEN_TIME,
            trigger_ref="trigger:duplicate-open",
            decision_slot="goal-open:duplicate",
        ),
        evaluated_world_revision=opened.world_revision,
    )
    duplicate_proposal = proposal_for_goal_payload(
        duplicate_payload,
        transition_kind="open",
        event_type="V2GoalOpened",
    )
    with pytest.raises(ValueError, match="goal identity already exists"):
        ledger.commit(
            [
                ledger_event(
                    "proposal-event:duplicate-open",
                    "ProposalRecorded",
                    duplicate_proposal.model_dump(mode="json"),
                    logical_time=OPEN_TIME,
                )
            ],
            expected_world_revision=opened.world_revision,
            expected_deliberation_revision=opened.deliberation_revision,
        )
    assert ledger.project() == opened

    before_clock = ledger.project()
    due_clock_event = ledger_event(
        "clock:goal-due",
        "ClockAdvanced",
        {
            "logical_time_from": OPEN_TIME.isoformat(),
            "logical_time_to": NOW.isoformat(),
        },
        logical_time=NOW,
    )
    ledger.commit(
        [due_clock_event],
        expected_world_revision=before_clock.world_revision,
        expected_deliberation_revision=before_clock.deliberation_revision,
    )
    after_clock = ledger.project()
    clock = after_clock.clock_transition_history[-1]
    clock_cursor = ProjectionCursor(
        world_revision=after_clock.world_revision,
        deliberation_revision=after_clock.deliberation_revision,
        ledger_sequence=after_clock.ledger_sequence,
    )

    expiry_events = []
    for index, before in enumerate((first, second), start=1):
        terminal = V2GoalExpiredTerminalReason(
            due_window=before.values.due_window,
            clock_projection_ref=clock.clock_event_ref,
            policy_digest=V2_GOAL_EXPIRY_POLICY_DIGEST,
            privacy_class=before.values.privacy_class,
        )
        after = goal_projection(
            revision=2,
            values=before.values.model_copy(
                update={"status": "expired", "terminal_reason": terminal}
            ),
            event_ref=f"event:goal-expired:{index}",
            updated_at=NOW,
            goal_id=before.goal_id,
            opened_at=before.opened_at,
        )
        payload = goal_expiry_payload(
            before,
            after,
            clock=clock,
            terminal=terminal,
        )
        expiry_events.append(
            ledger_event(
                after.origin.accepted_event_ref,
                "V2GoalExpired",
                payload.model_dump(mode="json"),
                logical_time=NOW,
            )
        )

    assert family_for_mutation("V2GoalExpired") is None
    assert "proposal_id" not in event_contract("V2GoalExpired").payload_model.model_fields
    assert event_contract("V2GoalExpired").allowed_predecessors == (
        "ClockAdvanced",
        "V2GoalExpired",
    )
    assert expiry_events[0].idempotency_key != expiry_events[1].idempotency_key

    before_expiry = ledger.project()
    ledger.commit(
        [expiry_events[0]],
        expected_world_revision=before_expiry.world_revision,
        expected_deliberation_revision=before_expiry.deliberation_revision,
    )
    after_first_expiry = ledger.project()
    first_expiry_cursor = ProjectionCursor(
        world_revision=after_first_expiry.world_revision,
        deliberation_revision=after_first_expiry.deliberation_revision,
        ledger_sequence=after_first_expiry.ledger_sequence,
    )
    ledger.commit(
        [expiry_events[1]],
        expected_world_revision=after_first_expiry.world_revision,
        expected_deliberation_revision=after_first_expiry.deliberation_revision,
    )
    expected = ledger.project()
    assert tuple(item.values.status for item in expected.goals) == (
        "expired",
        "expired",
    )
    assert tuple(
        item.cause_authority.clock_event_ref
        for item in expected.goal_transitions
        if item.operation == "expire"
    ) == (clock.clock_event_ref, clock.clock_event_ref)

    at_open = ledger.project_at(opened_cursor)
    assert tuple(item.values.status for item in at_open.goals) == ("active", "active")
    assert len(at_open.goal_transitions) == 2
    assert at_open.clock_transition_history == (opened.clock_transition_history[0],)
    at_clock = ledger.project_at(clock_cursor)
    assert tuple(item.values.status for item in at_clock.goals) == ("active", "active")
    assert at_clock.goal_transitions == opened.goal_transitions
    assert at_clock.clock_transition_history[-1] == clock
    at_first_expiry = ledger.project_at(first_expiry_cursor)
    assert tuple(item.values.status for item in at_first_expiry.goals) == (
        "expired",
        "active",
    )
    assert len(at_first_expiry.goal_transitions) == 3
    assert all(
        item.accepted_event_ref != expiry_events[1].event_id
        for item in at_first_expiry.goal_transitions
    )
    assert ledger.rebuild() == expected

    valid_state = SQLiteWorldLedger._state_from_projection(expected)
    for model, source in (
        (LedgerProjection, expected.model_dump(mode="json")),
        (ReducerState, valid_state.model_dump(mode="json")),
    ):
        missing_latest = json.loads(json.dumps(source))
        missing_latest["goal_transitions"].pop()
        with pytest.raises(ValueError, match="latest transition"):
            model.model_validate_json(json.dumps(missing_latest))

        duplicate_transition = json.loads(json.dumps(source))
        duplicate_transition["goal_transitions"][-1]["transition_id"] = (
            duplicate_transition["goal_transitions"][0]["transition_id"]
        )
        with pytest.raises(ValueError, match="transition ids must be globally unique"):
            model.model_validate_json(json.dumps(duplicate_transition))

        duplicate_proposals = json.loads(json.dumps(source))
        proposed = duplicate_proposal.model_dump(mode="json")
        duplicate_proposals["goal_proposals"] = [proposed, proposed]
        duplicate_proposals["goal_proposal_ids"] = [
            duplicate_proposal.proposal_id,
            duplicate_proposal.proposal_id,
        ]
        duplicate_proposals["proposal_ids"].append(duplicate_proposal.proposal_id)
        with pytest.raises(ValueError, match="proposal ids must be globally unique"):
            model.model_validate_json(json.dumps(duplicate_proposals))

        incomplete_index = json.loads(json.dumps(source))
        incomplete_index["goal_proposals"] = [proposed]
        incomplete_index["goal_proposal_ids"] = []
        incomplete_index["proposal_ids"].append(duplicate_proposal.proposal_id)
        with pytest.raises(ValueError, match="exactly index"):
            model.model_validate_json(json.dumps(incomplete_index))
    ledger.close()

    reopened = SQLiteWorldLedger(path=path, world_id="world:goal-integration")
    assert reopened.project() == expected
    assert reopened.rebuild() == expected
    reopened.close()


def test_occurrence_settled_outcome_shape_is_closed() -> None:
    _, occurrence, _ = settled_occurrence(index=18)
    raw = occurrence.model_dump(mode="json")
    for update in (
        {"settled_outcome_ref": None},
        {"settled_outcome_ref": "outcome:not-a-candidate"},
        {"status": "active"},
    ):
        with pytest.raises(ValueError, match="settled outcome|candidate outcome"):
            WorldOccurrenceProjection.model_validate_json(
                json.dumps({**raw, **update})
            )
    legacy_raw = dict(raw)
    legacy_raw.pop("settled_outcome_ref")
    for context in (
        None,
        {"allow_legacy_missing_settled_outcome": True},
        {"source_reducer_bundle": "world-v2-reducers.16"},
        {"source_reducer_bundle": "world-v2-reducers.unknown"},
    ):
        with pytest.raises(ValueError, match="candidate outcome"):
            WorldOccurrenceProjection.model_validate_json(
                json.dumps(legacy_raw), context=context
            )
    legacy = WorldOccurrenceProjection.model_validate_json(
        json.dumps(legacy_raw),
        context={"source_reducer_bundle": "world-v2-reducers.15"},
    )
    assert legacy.settled_outcome_ref is None


def test_block_canonical_evidence_indexes_outer_and_nested_bases() -> None:
    outer_event, outer_occurrence, _ = settled_occurrence(index=19)
    nested_event, nested_occurrence, _ = settled_occurrence(index=20)
    cause = deliberative_occurrence_cause(outer_event, outer_occurrence)
    nested_basis = deliberative_occurrence_cause(nested_event, nested_occurrence).basis
    before = goal_projection(
        revision=1,
        values=V2GoalValues(
            outcome_ref="outcome:publish-story",
            importance_bp=8000,
            progress_bp=1000,
            privacy_class="private",
            status="active",
        ),
        event_ref="event:goal:before-nested-block",
        updated_at=NOW - timedelta(hours=1),
    )
    added = blocker(
        blocker_id="blocker:nested-basis",
        blocker_class="external_dependency",
        basis=nested_basis,
        text="This separate dependency is blocking the next step.",
    )
    after = goal_projection(
        revision=2,
        values=before.values.model_copy(
            update={"blockers": (added,), "status": "blocked"}
        ),
        event_ref="event:goal:nested-block",
    )
    payload = goal_payload(
        after,
        operation="block",
        lane="deliberative",
        cause=cause,
        before=before,
    )
    assert {item.ref_id for item in payload.evidence_refs} == {
        outer_event.event_id,
        nested_event.event_id,
    }
    raw = payload.model_dump(mode="json")
    raw["evidence_refs"] = raw["evidence_refs"][:1]
    raw["accepted_change_hash"] = v2_goal_mutation_hash(raw)
    with pytest.raises(ValueError, match="EvidenceRefs are not exact"):
        V2GoalChangedPayload.model_validate_json(json.dumps(raw))

    heads, _ = reduce_v2_goal(
        (before,),
        (),
        payload,
        event_type="V2GoalBlocked",
        event_id=after.origin.accepted_event_ref,
        logical_time=NOW,
        actor_authorities=(),
        committed_events=(outer_event, nested_event),
        random_draws=(),
        world_occurrences=(outer_occurrence, nested_occurrence),
    )
    assert heads[0].values.blockers == (added,)


def test_compensation_restores_latest_deliberative_transition_with_privacy_floor() -> None:
    source, occurrence, _ = settled_occurrence(index=21)
    progress_cause = deliberative_occurrence_cause(source, occurrence)
    before = goal_projection(
        revision=1,
        values=V2GoalValues(
            outcome_ref="outcome:publish-story",
            importance_bp=8000,
            progress_bp=1000,
            privacy_class="private",
            status="active",
        ),
        event_ref="event:goal:before-compensation",
        updated_at=NOW - timedelta(hours=2),
    )
    progressed = goal_projection(
        revision=2,
        values=before.values.model_copy(update={"progress_bp": 3000}),
        event_ref="event:goal:progress-to-compensate",
        updated_at=NOW - timedelta(hours=1),
    )
    progress_payload = goal_payload(
        progressed,
        operation="progress",
        lane="deliberative",
        cause=progress_cause,
        before=before,
        progress_delta_bp=2000,
        progress_assessment=V2GoalProgressAssessment(
            contribution_class="direct_contribution",
            basis=progress_cause.basis,
            rationale=rationale("I initially counted this as substantial progress."),
        ),
    )
    _, history = reduce_v2_goal(
        (before,),
        (),
        progress_payload,
        event_type="V2GoalProgressed",
        event_id=progressed.origin.accepted_event_ref,
        logical_time=progressed.updated_at,
        actor_authorities=(),
        committed_events=(source,),
        random_draws=(),
        world_occurrences=(occurrence,),
    )
    target_event = CommittedWorldEventRef(
        event_id=progressed.origin.accepted_event_ref,
        event_type="V2GoalProgressed",
        world_revision=8,
        payload_hash="8" * 64,
        logical_time=progressed.updated_at,
    )
    compensation = CompensationCauseAuthority(
        target_transition_id=history[-1].transition_id,
        target_entity_revision=history[-1].entity_revision,
        target_accepted_event_ref=target_event.event_id,
        target_accepted_world_revision=target_event.world_revision,
        target_accepted_payload_hash=target_event.payload_hash,
        target_authority_lane="deliberative",
        correction_basis=internal_cause(
            evaluated_world_revision=9,
            trigger_ref="trigger:correction:21",
            decision_slot="goal-correction:21",
        ).basis,
        correction_rationale=rationale(
            "I reconsidered how much progress that evidence really represented."
        ),
    )
    restored = goal_projection(
        revision=3,
        values=before.values,
        event_ref="event:goal:progress-compensated",
    )
    payload = goal_payload(
        restored,
        operation="compensate",
        lane="compensation",
        cause=compensation,
        before=progressed,
        compensation_target=compensation,
        evaluated_world_revision=9,
    )

    heads, compensated_history = reduce_v2_goal(
        (progressed,),
        history,
        payload,
        event_type="V2GoalTransitionCompensated",
        event_id=restored.origin.accepted_event_ref,
        logical_time=NOW,
        actor_authorities=(),
        committed_events=(target_event,),
        random_draws=(),
        world_occurrences=(),
    )
    assert heads[0].values == before.values
    assert compensated_history[-1].compensates_transition_id == history[-1].transition_id


def test_compensation_rejects_target_event_as_sole_correction_basis() -> None:
    before, progressed, history, target_event = progress_transition_for_compensation(
        index=41
    )
    self_basis = CommittedEvidenceBasis(
        sources=(
            CommittedEvidenceSource(
                source_kind="world_started",
                event_ref=target_event.event_id,
                world_revision=target_event.world_revision,
                payload_hash=target_event.payload_hash,
            ),
        )
    )
    compensation = CompensationCauseAuthority(
        target_transition_id=history[-1].transition_id,
        target_entity_revision=history[-1].entity_revision,
        target_accepted_event_ref=target_event.event_id,
        target_accepted_world_revision=target_event.world_revision,
        target_accepted_payload_hash=target_event.payload_hash,
        target_authority_lane="deliberative",
        correction_basis=self_basis,
        correction_rationale=rationale("The target event supposedly disproves itself."),
    )
    restored = goal_projection(
        revision=3,
        values=before.values,
        event_ref="event:goal:self-correction-rejected",
    )
    payload = goal_payload(
        restored,
        operation="compensate",
        lane="compensation",
        cause=compensation,
        before=progressed,
        compensation_target=compensation,
        evaluated_world_revision=9,
    )
    with pytest.raises(ValueError, match="sole correction basis"):
        reduce_v2_goal(
            (progressed,),
            history,
            payload,
            event_type="V2GoalTransitionCompensated",
            event_id=restored.origin.accepted_event_ref,
            logical_time=NOW,
            actor_authorities=(),
            committed_events=(target_event,),
            random_draws=(),
            world_occurrences=(),
        )


@pytest.mark.parametrize(
    "attack",
    ("stale", "cross_goal", "wrong_event_type", "wrong_effective_lane"),
)
def test_compensation_target_binding_fails_closed(attack: str) -> None:
    before, progressed, history, target_event = progress_transition_for_compensation(
        index=42
    )
    target_history = history
    target = history[-1]
    if attack == "stale":
        later = target.model_copy(
            update={
                "transition_id": "transition:goal:later",
                "entity_revision": 3,
                "accepted_event_ref": "event:goal:later",
            }
        )
        target_history = (*history, later)
    elif attack == "cross_goal":
        target_history = (
            target.model_copy(update={"goal_id": "goal:somebody-else"}),
        )
    if attack == "wrong_event_type":
        target_event = target_event.model_copy(update={"event_type": "FactCommitted"})
    lane = "operator" if attack == "wrong_effective_lane" else "deliberative"
    compensation = CompensationCauseAuthority(
        target_transition_id=target.transition_id,
        target_entity_revision=target.entity_revision,
        target_accepted_event_ref=target_event.event_id,
        target_accepted_world_revision=target_event.world_revision,
        target_accepted_payload_hash=target_event.payload_hash,
        target_authority_lane=lane,
        correction_basis=internal_cause(
            evaluated_world_revision=9,
            trigger_ref=f"trigger:correction:{attack}",
            decision_slot=f"goal-correction:{attack}",
        ).basis,
        correction_rationale=rationale("I found a reason to correct the transition."),
    )
    restored = goal_projection(
        revision=3,
        values=before.values,
        event_ref=f"event:goal:binding-attack:{attack}",
    )
    payload = goal_payload(
        restored,
        operation="compensate",
        lane="compensation",
        cause=compensation,
        before=progressed,
        compensation_target=compensation,
        evaluated_world_revision=9,
    )
    with pytest.raises(ValueError, match="target is not exact latest"):
        reduce_v2_goal(
            (progressed,),
            target_history,
            payload,
            event_type="V2GoalTransitionCompensated",
            event_id=restored.origin.accepted_event_ref,
            logical_time=NOW,
            actor_authorities=(),
            committed_events=(target_event,),
            random_draws=(),
            world_occurrences=(),
        )


def test_operator_origin_compensation_and_undo_rederive_effective_authority() -> None:
    authority, authority_event, operator_binding = operator_authority()
    before = goal_projection(
        revision=1,
        values=V2GoalValues(
            outcome_ref="outcome:publish-story",
            importance_bp=7000,
            progress_bp=1000,
            privacy_class="private",
            status="active",
        ),
        event_ref="event:goal:before-operator-revise",
        updated_at=NOW - timedelta(hours=2),
    )
    revised = goal_projection(
        revision=2,
        values=before.values.model_copy(update={"importance_bp": 9000}),
        event_ref="event:goal:operator-revised",
        updated_at=NOW - timedelta(hours=1),
    )
    revise_payload = goal_payload(
        revised,
        operation="revise",
        lane="operator",
        cause=operator_binding,
        before=before,
        revise_kind="reprioritize",
    )
    _, history = reduce_v2_goal(
        (before,),
        (),
        revise_payload,
        event_type="V2GoalRevised",
        event_id=revised.origin.accepted_event_ref,
        logical_time=revised.updated_at,
        actor_authorities=(authority,),
        committed_events=(authority_event,),
        random_draws=(),
        world_occurrences=(),
    )
    revise_event = CommittedWorldEventRef(
        event_id=revised.origin.accepted_event_ref,
        event_type="V2GoalRevised",
        world_revision=8,
        payload_hash="4" * 64,
        logical_time=revised.updated_at,
    )
    first_cause = CompensationCauseAuthority(
        target_transition_id=history[-1].transition_id,
        target_entity_revision=history[-1].entity_revision,
        target_accepted_event_ref=revise_event.event_id,
        target_accepted_world_revision=revise_event.world_revision,
        target_accepted_payload_hash=revise_event.payload_hash,
        target_authority_lane="operator",
        correction_basis=internal_cause(
            evaluated_world_revision=9,
            trigger_ref="trigger:operator-correction:1",
            decision_slot="goal-operator-correction:1",
        ).basis,
        correction_rationale=rationale("The operator revision was entered in error."),
        operator_authority=operator_binding,
    )
    restored = goal_projection(
        revision=3,
        values=before.values,
        event_ref="event:goal:operator-revision-compensated",
    )
    first_payload = goal_payload(
        restored,
        operation="compensate",
        lane="compensation",
        cause=first_cause,
        before=revised,
        compensation_target=first_cause,
        evaluated_world_revision=9,
    )
    _, compensated_history = reduce_v2_goal(
        (revised,),
        history,
        first_payload,
        event_type="V2GoalTransitionCompensated",
        event_id=restored.origin.accepted_event_ref,
        logical_time=NOW,
        actor_authorities=(authority,),
        committed_events=(authority_event, revise_event),
        random_draws=(),
        world_occurrences=(),
    )
    cross_event = CommittedWorldEventRef(
        event_id="event:fact:cross-operation-expiry-basis",
        event_type="FactCommitted",
        world_revision=9,
        payload_hash="a" * 64,
        logical_time=NOW,
    )
    cross_fact = active_completion_fact(
        event_ref=cross_event.event_id,
        predicate="goal.expiry.correction",
        value_ref="value:cross-operation",
        value_hash="a" * 64,
    )
    cross_sources = CommittedEvidenceBasis(
        sources=(
            CommittedEvidenceSource(
                source_kind="fact",
                event_ref=cross_event.event_id,
                world_revision=cross_event.world_revision,
                payload_hash=cross_event.payload_hash,
                source_entity_ref=cross_fact.fact_id,
                source_entity_revision=cross_fact.entity_revision,
            ),
        )
    )
    cross_rationale = rationale("This expiry-only authority cannot correct a revise.")
    cross_clock = ClockCauseAuthority(
        clock_event_ref="event:clock:cross-operation",
        clock_world_revision=7,
        clock_payload_hash="7" * 64,
        logical_time_from=NOW - timedelta(minutes=1),
        logical_time_to=NOW,
        policy_version=CLOCK_AUTHORITY_POLICY_VERSION,
        policy_digest=CLOCK_AUTHORITY_POLICY_DIGEST,
    )
    cross_basis = GoalExpiryCorrectionBasis(
        target_expiry_transition_id=history[-1].transition_id,
        target_expiry_event_ref=revise_event.event_id,
        target_expiry_world_revision=revise_event.world_revision,
        target_expiry_payload_hash=revise_event.payload_hash,
        original_clock=cross_clock,
        operator_authority=operator_binding,
        correction_class="operator_import_error",
        sources=cross_sources,
        rationale=cross_rationale,
        privacy_class="private",
        policy_version=V2_GOAL_EXPIRY_CORRECTION_POLICY_VERSION,
        policy_digest=V2_GOAL_EXPIRY_CORRECTION_POLICY_DIGEST,
    )
    cross_cause = first_cause.model_copy(
        update={
            "correction_basis": cross_basis,
            "correction_rationale": cross_rationale,
        }
    )
    cross_payload = goal_payload(
        restored,
        operation="compensate",
        lane="compensation",
        cause=cross_cause,
        before=revised,
        compensation_target=cross_cause,
        evaluated_world_revision=9,
    )
    with pytest.raises(ValueError, match="cannot cross operation domains"):
        reduce_v2_goal(
            (revised,),
            history,
            cross_payload,
            event_type="V2GoalTransitionCompensated",
            event_id=restored.origin.accepted_event_ref,
            logical_time=NOW,
            actor_authorities=(authority,),
            committed_events=(authority_event, revise_event, cross_event),
            random_draws=(),
            world_occurrences=(),
            facts=(cross_fact,),
        )
    first_compensation_event = CommittedWorldEventRef(
        event_id=restored.origin.accepted_event_ref,
        event_type="V2GoalTransitionCompensated",
        world_revision=9,
        payload_hash="5" * 64,
        logical_time=NOW,
    )
    second_cause = CompensationCauseAuthority(
        target_transition_id=compensated_history[-1].transition_id,
        target_entity_revision=compensated_history[-1].entity_revision,
        target_accepted_event_ref=first_compensation_event.event_id,
        target_accepted_world_revision=first_compensation_event.world_revision,
        target_accepted_payload_hash=first_compensation_event.payload_hash,
        target_authority_lane="operator",
        correction_basis=internal_cause(
            evaluated_world_revision=10,
            logical_time=NOW + timedelta(minutes=1),
            trigger_ref="trigger:operator-correction:2",
            decision_slot="goal-operator-correction:2",
        ).basis,
        correction_rationale=rationale("The first correction was itself mistaken."),
    )
    reapplied = goal_projection(
        revision=4,
        values=revised.values,
        event_ref="event:goal:operator-compensation-undone",
        updated_at=NOW + timedelta(minutes=1),
    )
    second_payload = goal_payload(
        reapplied,
        operation="compensate",
        lane="compensation",
        cause=second_cause,
        before=restored,
        compensation_target=second_cause,
        evaluated_world_revision=10,
    )
    with pytest.raises(ValueError, match="effective operator lane"):
        reduce_v2_goal(
            (restored,),
            compensated_history,
            second_payload,
            event_type="V2GoalTransitionCompensated",
            event_id=reapplied.origin.accepted_event_ref,
            logical_time=reapplied.updated_at,
            actor_authorities=(authority,),
            committed_events=(authority_event, first_compensation_event),
            random_draws=(),
            world_occurrences=(),
        )

    reauthorized_cause = second_cause.model_copy(
        update={"operator_authority": operator_binding}
    )
    reauthorized_payload = goal_payload(
        reapplied,
        operation="compensate",
        lane="compensation",
        cause=reauthorized_cause,
        before=restored,
        compensation_target=reauthorized_cause,
        evaluated_world_revision=10,
    )
    heads, final_history = reduce_v2_goal(
        (restored,),
        compensated_history,
        reauthorized_payload,
        event_type="V2GoalTransitionCompensated",
        event_id=reapplied.origin.accepted_event_ref,
        logical_time=reapplied.updated_at,
        actor_authorities=(authority,),
        committed_events=(authority_event, first_compensation_event),
        random_draws=(),
        world_occurrences=(),
    )
    assert heads == (reapplied,)
    assert final_history[-1].compensates_transition_id == compensated_history[-1].transition_id


def test_undo_compensation_restores_terminal_closed_at_at_new_event_time() -> None:
    abandoned_at = NOW - timedelta(hours=1)
    abandon_cause = internal_cause(
        logical_time=abandoned_at,
        trigger_ref="trigger:abandon-before-compensation",
        decision_slot="goal-abandon-before-compensation",
    )
    before = goal_projection(
        revision=1,
        values=V2GoalValues(
            outcome_ref="outcome:publish-story",
            importance_bp=7000,
            progress_bp=1000,
            privacy_class="private",
            status="active",
        ),
        event_ref="event:goal:before-abandon-compensation",
        updated_at=NOW - timedelta(hours=2),
    )
    reason = V2GoalLifecycleReason(
        reason_kind="values_changed",
        rationale=rationale("I no longer want to pursue this outcome."),
        basis=abandon_cause.basis,
        privacy_class="private",
    )
    terminal = V2GoalAbandonedTerminalReason(reason=reason)
    abandoned = goal_projection(
        revision=2,
        values=before.values.model_copy(
            update={"status": "abandoned", "terminal_reason": terminal}
        ),
        event_ref="event:goal:abandoned-before-compensation",
        updated_at=abandoned_at,
    )
    abandon_payload = goal_payload(
        abandoned,
        operation="abandon",
        lane="deliberative",
        cause=abandon_cause,
        before=before,
        lifecycle_reason=reason,
        terminal_reason=terminal,
    )
    _, history = reduce_v2_goal(
        (before,),
        (),
        abandon_payload,
        event_type="V2GoalAbandoned",
        event_id=abandoned.origin.accepted_event_ref,
        logical_time=abandoned_at,
        actor_authorities=(),
        committed_events=(),
        random_draws=(),
        world_occurrences=(),
    )
    abandon_event = CommittedWorldEventRef(
        event_id=abandoned.origin.accepted_event_ref,
        event_type="V2GoalAbandoned",
        world_revision=8,
        payload_hash="6" * 64,
        logical_time=abandoned_at,
    )
    first_cause = CompensationCauseAuthority(
        target_transition_id=history[-1].transition_id,
        target_entity_revision=history[-1].entity_revision,
        target_accepted_event_ref=abandon_event.event_id,
        target_accepted_world_revision=abandon_event.world_revision,
        target_accepted_payload_hash=abandon_event.payload_hash,
        target_authority_lane="deliberative",
        correction_basis=internal_cause(
            evaluated_world_revision=9,
            trigger_ref="trigger:undo-abandon",
            decision_slot="goal-undo-abandon",
        ).basis,
        correction_rationale=rationale("I had abandoned the goal too hastily."),
    )
    reopened = goal_projection(
        revision=3,
        values=before.values,
        event_ref="event:goal:abandon-compensated",
    )
    first_payload = goal_payload(
        reopened,
        operation="compensate",
        lane="compensation",
        cause=first_cause,
        before=abandoned,
        compensation_target=first_cause,
        evaluated_world_revision=9,
    )
    _, compensated_history = reduce_v2_goal(
        (abandoned,),
        history,
        first_payload,
        event_type="V2GoalTransitionCompensated",
        event_id=reopened.origin.accepted_event_ref,
        logical_time=NOW,
        actor_authorities=(),
        committed_events=(abandon_event,),
        random_draws=(),
        world_occurrences=(),
    )
    first_compensation_event = CommittedWorldEventRef(
        event_id=reopened.origin.accepted_event_ref,
        event_type="V2GoalTransitionCompensated",
        world_revision=9,
        payload_hash="7" * 64,
        logical_time=NOW,
    )
    undo_time = NOW + timedelta(minutes=1)
    second_cause = CompensationCauseAuthority(
        target_transition_id=compensated_history[-1].transition_id,
        target_entity_revision=compensated_history[-1].entity_revision,
        target_accepted_event_ref=first_compensation_event.event_id,
        target_accepted_world_revision=first_compensation_event.world_revision,
        target_accepted_payload_hash=first_compensation_event.payload_hash,
        target_authority_lane="deliberative",
        correction_basis=internal_cause(
            evaluated_world_revision=10,
            logical_time=undo_time,
            trigger_ref="trigger:undo-abandon-compensation",
            decision_slot="goal-undo-abandon-compensation",
        ).basis,
        correction_rationale=rationale("The reopening was itself the mistake."),
    )
    terminal_again = goal_projection(
        revision=4,
        values=abandoned.values,
        event_ref="event:goal:abandon-restored",
        updated_at=undo_time,
    )
    second_payload = goal_payload(
        terminal_again,
        operation="compensate",
        lane="compensation",
        cause=second_cause,
        before=reopened,
        compensation_target=second_cause,
        evaluated_world_revision=10,
    )
    heads, _ = reduce_v2_goal(
        (reopened,),
        compensated_history,
        second_payload,
        event_type="V2GoalTransitionCompensated",
        event_id=terminal_again.origin.accepted_event_ref,
        logical_time=undo_time,
        actor_authorities=(),
        committed_events=(first_compensation_event,),
        random_draws=(),
        world_occurrences=(),
    )
    assert heads[0].values.status == "abandoned"
    assert heads[0].closed_at == undo_time


@pytest.mark.parametrize(
    ("operation", "before_status", "after_status", "reason_kind", "event_type"),
    (
        ("pause", "active", "paused", "priority_shift", "V2GoalPaused"),
        ("resume", "paused", "active", "renewed_intent", "V2GoalResumed"),
    ),
)
def test_pause_and_resume_require_typed_deliberative_reason(
    operation: str,
    before_status: str,
    after_status: str,
    reason_kind: str,
    event_type: str,
) -> None:
    cause = internal_cause()
    before = goal_projection(
        revision=1,
        values=V2GoalValues(
            outcome_ref="outcome:publish-story",
            importance_bp=8000,
            progress_bp=3000,
            privacy_class="private",
            status=before_status,
        ),
        event_ref=f"event:goal:before-{operation}",
        updated_at=NOW - timedelta(hours=1),
    )
    after = goal_projection(
        revision=2,
        values=before.values.model_copy(update={"status": after_status}),
        event_ref=f"event:goal:{operation}",
    )
    reason = V2GoalLifecycleReason(
        reason_kind=reason_kind,
        rationale=rationale(f"I chose to {operation} after reconsidering my priorities."),
        basis=cause.basis,
        privacy_class="private",
    )
    payload = goal_payload(
        after,
        operation=operation,
        lane="deliberative",
        cause=cause,
        before=before,
        lifecycle_reason=reason,
    )

    heads, history = reduce_v2_goal(
        (before,),
        (),
        payload,
        event_type=event_type,
        event_id=after.origin.accepted_event_ref,
        logical_time=NOW,
        actor_authorities=(),
        committed_events=(),
        random_draws=(),
        world_occurrences=(),
    )
    assert heads[0].values.status == after_status
    assert history[-1].lifecycle_reason == reason


def test_recontract_installs_new_contract_at_current_cutoff() -> None:
    cause = internal_cause()
    old_contract = completion_contract(cutoff=6)
    new_contract = completion_contract(
        cutoff=7, contract_id="goal-contract:publish-story:2"
    )
    before = goal_projection(
        revision=1,
        values=V2GoalValues(
            outcome_ref="outcome:publish-story",
            importance_bp=8000,
            progress_bp=3000,
            privacy_class="private",
            completion_contract=old_contract,
            status="active",
        ),
        event_ref="event:goal:before-recontract",
        updated_at=NOW - timedelta(hours=1),
    )
    after = goal_projection(
        revision=2,
        values=before.values.model_copy(update={"completion_contract": new_contract}),
        event_ref="event:goal:recontracted",
    )
    payload = goal_payload(
        after,
        operation="revise",
        lane="deliberative",
        cause=cause,
        before=before,
        revise_kind="recontract",
    )

    heads, _ = reduce_v2_goal(
        (before,),
        (),
        payload,
        event_type="V2GoalRevised",
        event_id=after.origin.accepted_event_ref,
        logical_time=NOW,
        actor_authorities=(),
        committed_events=(),
        random_draws=(),
        world_occurrences=(),
    )
    assert heads[0].values.completion_contract == new_contract

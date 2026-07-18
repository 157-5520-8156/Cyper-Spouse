"""Pure lifecycle reducers for plans, NPCs, occurrences, and lived experience."""

from __future__ import annotations

from datetime import datetime

from .life_events import (
    ActivityPlannedPayload,
    ActivityTransitionPayload,
    NpcRegisteredPayload,
    OutcomeObservationRecordedPayload,
    OutcomeProposalRecordedPayload,
    WorldOccurrenceActivatedPayload,
    WorldOccurrenceCommittedPayload,
    WorldOccurrenceSettledPayload,
    WorldOccurrenceTerminalPayload,
)
from .activity_timing import activity_completion_allowed
from .experience_events import ExperienceCommittedPayload, LegacyExperienceCommittedPayload
from .schemas import (
    Action,
    ExperienceAuthorityProjection,
    ExperienceExecutionReceiptBinding,
    ExperienceOccurrenceSettlementBinding,
    ExperienceProjection,
    FactProjection,
    LegacyExperienceProjection,
    CommittedWorldEventRef,
    ExecutionReceipt,
    NpcProjection,
    OutcomeObservationProjection,
    OutcomeProposalProjection,
    PlanAuthorityOrigin,
    PlanStateProjection,
    WorldOccurrenceProjection,
    plan_authority_binding_hash,
    plan_authority_projection_hash,
)


_PRIVACY_RANK = {
    "public": 0,
    "shareable": 1,
    "personal": 2,
    "private": 3,
    "withhold": 4,
}


def register_npc(
    npcs: tuple[NpcProjection, ...], payload: NpcRegisteredPayload
) -> tuple[NpcProjection, ...]:
    if any(npc.npc_id == payload.npc.npc_id for npc in npcs):
        raise ValueError(f"NPC {payload.npc.npc_id!r} already exists")
    if any(npc.stable_identity_ref == payload.npc.stable_identity_ref for npc in npcs):
        raise ValueError("NPC stable identity is already registered")
    return (*npcs, payload.npc)


def plan_activity(
    plans: tuple[PlanStateProjection, ...],
    npcs: tuple[NpcProjection, ...],
    payload: ActivityPlannedPayload,
    *,
    event_ref: str,
    event_payload_hash: str,
    accepted_world_revision: int,
    logical_time: datetime,
    allow_legacy_missing_owner: bool = False,
) -> tuple[PlanStateProjection, ...]:
    if any(plan.plan_id == payload.plan.plan_id for plan in plans):
        raise ValueError(f"plan {payload.plan.plan_id!r} already exists")
    if any(plan.activity_id == payload.plan.activity_id for plan in plans):
        raise ValueError(f"activity {payload.plan.activity_id!r} already exists")
    npc_by_id = {npc.npc_id: npc for npc in npcs if npc.status == "active"}
    for participant_ref in payload.plan.participant_refs:
        if not participant_ref.startswith("npc:"):
            continue
        npc = npc_by_id.get(participant_ref.removeprefix("npc:"))
        if npc is None:
            raise ValueError("plan references an unregistered NPC")
        if _PRIVACY_RANK[payload.plan.privacy_class] < _PRIVACY_RANK[npc.privacy_class]:
            raise ValueError("plan cannot weaken participant NPC privacy")
    if payload.plan.supersedes_plan_id is not None:
        predecessor = next(
            (plan for plan in plans if plan.plan_id == payload.plan.supersedes_plan_id),
            None,
        )
        if predecessor is None or predecessor.status != "abandoned":
            raise ValueError("replacement plan requires an abandoned predecessor")
    owner = payload.plan.owner_actor_ref
    if owner is None:
        if allow_legacy_missing_owner:
            return (
                *plans,
                payload.plan.model_copy(update={"owner_actor_ref": "legacy:unknown-owner"}),
            )
        raise ValueError("ActivityPlanned requires an explicit owner_actor_ref")
    if owner == "legacy:unknown-owner":
        raise ValueError("live ActivityPlanned cannot claim legacy unknown owner")
    if payload.plan.authority_origin is not None:
        raise ValueError("ActivityPlanned draft cannot inject authority_origin")
    projection_hash = plan_authority_projection_hash(payload.plan)
    origin = PlanAuthorityOrigin(
        transition_id=payload.transition_id,
        accepted_event_type="ActivityPlanned",
        accepted_event_ref=event_ref,
        accepted_world_revision=accepted_world_revision,
        accepted_payload_hash=event_payload_hash,
        accepted_at=logical_time,
        authority_projection_hash=projection_hash,
        binding_hash=plan_authority_binding_hash(
            plan_id=payload.plan.plan_id,
            owner_actor_ref=owner,
            entity_revision=payload.plan.entity_revision,
            transition_id=payload.transition_id,
            event_type="ActivityPlanned",
            accepted_event_ref=event_ref,
            accepted_world_revision=accepted_world_revision,
            accepted_payload_hash=event_payload_hash,
            accepted_at=logical_time,
            projection_hash=projection_hash,
        ),
    )
    return (*plans, payload.plan.model_copy(update={"authority_origin": origin}))


def transition_activity(
    plans: tuple[PlanStateProjection, ...],
    payload: ActivityTransitionPayload,
    *,
    target_status: str,
    allowed_statuses: frozenset[str],
    logical_time: datetime,
    event_type: str,
    event_ref: str,
    event_payload_hash: str,
    accepted_world_revision: int,
    allow_legacy_unowned_transition: bool = False,
) -> tuple[PlanStateProjection, ...]:
    index = next(
        (index for index, plan in enumerate(plans) if plan.plan_id == payload.plan_id),
        None,
    )
    if index is None:
        raise ValueError("activity transition references an unknown plan")
    plan = plans[index]
    _expect_revision(plan.entity_revision, payload.expected_entity_revision)
    if plan.status not in allowed_statuses:
        raise ValueError(f"cannot transition {plan.status!r} activity to {target_status!r}")
    if payload.transitioned_at > logical_time:
        raise ValueError("activity transition is ahead of logical time")
    if payload.transitioned_at != logical_time:
        raise ValueError("activity transition must be pinned to authoritative logical time")
    # The catalog's scheduler path must not manufacture an immediate
    # completion merely because a wake occurred.  An explicit host/user
    # transition is still allowed to record a real early finish (for example
    # an activity was interrupted or completed sooner than expected); its
    # reason/evidence is the authority for that decision.  Restrict the
    # elapsed-time floor to the proposal-bound path where the reducer can
    # distinguish an ordinary scheduler wake from such an explicit transition.
    if (
        event_type == "ActivityCompleted"
        and payload.activity_lifecycle_proposal_id is not None
        and not activity_completion_allowed(plan, logical_time=logical_time)
    ):
        raise ValueError("activity cannot complete before its minimum elapsed duration")
    if payload.transitioned_at.tzinfo is None or payload.transitioned_at.utcoffset() is None:
        raise ValueError("activity transition time must be timezone-aware")
    if (
        plan.last_transitioned_at is not None
        and payload.transitioned_at < plan.last_transitioned_at
    ):
        raise ValueError("activity transition time cannot move backwards")
    if plan.owner_actor_ref == "legacy:unknown-owner" and plan.authority_origin is None:
        if not allow_legacy_unowned_transition:
            raise ValueError("live activity transition requires current Plan owner authority")
        updated = plan.model_copy(
            update={
                "entity_revision": plan.entity_revision + 1,
                "status": target_status,
                "last_transitioned_at": payload.transitioned_at,
                "terminal_reason_ref": (
                    payload.reason_ref
                    if target_status in {"completed", "abandoned"}
                    else None
                ),
            }
        )
        return (*plans[:index], updated, *plans[index + 1 :])
    if plan.owner_actor_ref is None or plan.authority_origin is None:
        raise ValueError("activity transition requires installed Plan owner authority")
    next_revision = plan.entity_revision + 1
    updated = plan.model_copy(
        update={
            "entity_revision": next_revision,
            "status": target_status,
            "last_transitioned_at": payload.transitioned_at,
            "terminal_reason_ref": (
                payload.reason_ref if target_status in {"completed", "abandoned"} else None
            ),
        }
    )
    projection_hash = plan_authority_projection_hash(updated)
    origin = PlanAuthorityOrigin(
        transition_id=payload.transition_id,
        accepted_event_type=event_type,
        accepted_event_ref=event_ref,
        accepted_world_revision=accepted_world_revision,
        accepted_payload_hash=event_payload_hash,
        accepted_at=logical_time,
        authority_projection_hash=projection_hash,
        binding_hash=plan_authority_binding_hash(
            plan_id=plan.plan_id,
            owner_actor_ref=plan.owner_actor_ref,
            entity_revision=next_revision,
            transition_id=payload.transition_id,
            event_type=event_type,
            accepted_event_ref=event_ref,
            accepted_world_revision=accepted_world_revision,
            accepted_payload_hash=event_payload_hash,
            accepted_at=logical_time,
            projection_hash=projection_hash,
        ),
    )
    updated = updated.model_copy(update={"authority_origin": origin})
    return (*plans[:index], updated, *plans[index + 1 :])


def commit_occurrence(
    occurrences: tuple[WorldOccurrenceProjection, ...],
    npcs: tuple[NpcProjection, ...],
    plans: tuple[PlanStateProjection, ...],
    payload: WorldOccurrenceCommittedPayload,
) -> tuple[WorldOccurrenceProjection, ...]:
    occurrence = payload.occurrence
    if any(item.occurrence_id == occurrence.occurrence_id for item in occurrences):
        raise ValueError(f"occurrence {occurrence.occurrence_id!r} already exists")
    known_npc_ids = {npc.npc_id for npc in npcs if npc.status == "active"}
    missing_npcs = {
        ref.removeprefix("npc:")
        for ref in occurrence.participant_refs
        if ref.startswith("npc:") and ref.removeprefix("npc:") not in known_npc_ids
    }
    if missing_npcs:
        raise ValueError("occurrence references an unregistered NPC")
    referenced_npcs = {
        npc.npc_id: npc for npc in npcs if f"npc:{npc.npc_id}" in occurrence.participant_refs
    }
    if any(
        _PRIVACY_RANK[occurrence.visibility] < _PRIVACY_RANK[npc.privacy_class]
        for npc in referenced_npcs.values()
    ):
        raise ValueError("occurrence cannot weaken participant NPC privacy")
    known_plan_refs = {plan.plan_id for plan in plans}
    if any(
        ref.startswith("plan:") and ref.removeprefix("plan:") not in known_plan_refs
        for ref in occurrence.precondition_refs
    ):
        raise ValueError("occurrence references an unknown plan precondition")
    return (*occurrences, occurrence)


def activate_occurrence(
    occurrences: tuple[WorldOccurrenceProjection, ...],
    payload: WorldOccurrenceActivatedPayload,
) -> tuple[WorldOccurrenceProjection, ...]:
    index, occurrence = _occurrence(occurrences, payload.occurrence_id)
    _expect_revision(occurrence.entity_revision, payload.expected_entity_revision)
    if occurrence.status != "committed":
        raise ValueError("only a committed occurrence can activate")
    if not (
        occurrence.time_window.opens_at <= payload.activated_at < occurrence.time_window.closes_at
    ):
        raise ValueError("occurrence activation is outside its committed window")
    if not set(occurrence.precondition_refs) <= set(payload.satisfied_precondition_refs):
        raise ValueError("occurrence activation is missing a precondition")
    updated = occurrence.model_copy(
        update={
            "entity_revision": occurrence.entity_revision + 1,
            "status": "active",
            "activated_at": payload.activated_at,
            "satisfied_precondition_refs": payload.satisfied_precondition_refs,
        }
    )
    return _replace(occurrences, index, updated)


def record_outcome_observation(
    occurrences: tuple[WorldOccurrenceProjection, ...],
    observations: tuple[OutcomeObservationProjection, ...],
    committed_world_event_refs: tuple[CommittedWorldEventRef, ...],
    payload: OutcomeObservationRecordedPayload,
    *,
    logical_time: datetime,
) -> tuple[
    tuple[WorldOccurrenceProjection, ...],
    tuple[OutcomeObservationProjection, ...],
]:
    observation = payload.observation
    if any(item.observation_id == observation.observation_id for item in observations):
        raise ValueError("outcome observation already exists")
    index, occurrence = _occurrence(occurrences, observation.occurrence_id)
    _expect_revision(occurrence.entity_revision, payload.expected_entity_revision)
    if occurrence.status != "active":
        raise ValueError("outcome observation requires an active occurrence")
    if occurrence.activated_at is None or observation.observed_at < occurrence.activated_at:
        raise ValueError("outcome observation precedes occurrence activation")
    if observation.observed_at > logical_time:
        raise ValueError("outcome observation is ahead of authoritative logical time")
    expected_evidence_type = {
        "settled_external_result": "settled_external_result",
        "clock_plan_precondition": "active_plan",
        "operator_observation": "operator_observation",
        "committed_world_event": "committed_world_event",
    }[observation.source_kind]
    compatible_refs = {
        evidence.ref_id
        for evidence in payload.evidence_refs
        if evidence.evidence_type == expected_evidence_type
    }
    if not set(observation.source_refs) <= compatible_refs:
        raise ValueError("outcome observation source lacks compatible evidence")
    if observation.source_kind == "committed_world_event":
        authority = {ref.event_id: ref for ref in committed_world_event_refs}
        evidence_by_id = {ref.ref_id: ref for ref in payload.evidence_refs}
        for source_ref in observation.source_refs:
            committed = authority.get(source_ref)
            evidence = evidence_by_id.get(source_ref)
            if (
                committed is None
                or evidence is None
                or evidence.source_world_revision != committed.world_revision
                or evidence.immutable_hash != committed.payload_hash
            ):
                raise ValueError("outcome observation references unverified world evidence")
    updated = occurrence.model_copy(
        update={
            "entity_revision": occurrence.entity_revision + 1,
            "observation_refs": (
                *occurrence.observation_refs,
                observation.observation_id,
            ),
        }
    )
    return _replace(occurrences, index, updated), (*observations, observation)


def settle_occurrence(
    occurrences: tuple[WorldOccurrenceProjection, ...],
    observations: tuple[OutcomeObservationProjection, ...],
    outcome_proposals: tuple[OutcomeProposalProjection, ...],
    payload: WorldOccurrenceSettledPayload,
    *,
    logical_time: datetime,
    settlement_event_ref: str,
    settlement_world_revision: int,
    settlement_payload_hash: str,
) -> tuple[WorldOccurrenceProjection, ...]:
    index, occurrence = _occurrence(occurrences, payload.occurrence_id)
    _expect_revision(occurrence.entity_revision, payload.expected_entity_revision)
    if occurrence.status != "active":
        raise ValueError("only an active occurrence can settle")
    proposal = next(
        (
            item
            for item in outcome_proposals
            if item.outcome_proposal_id == payload.outcome_proposal_id
        ),
        None,
    )
    if proposal is None:
        raise ValueError("occurrence settlement requires a recorded outcome proposal")
    if proposal.occurrence_id != occurrence.occurrence_id:
        raise ValueError("outcome proposal belongs to another occurrence")
    if proposal.evaluated_entity_revision != occurrence.entity_revision:
        raise ValueError("outcome proposal evaluated a stale occurrence revision")
    if proposal.evaluated_world_revision != payload.evaluated_world_revision:
        raise ValueError("outcome proposal evaluated a stale world revision")
    if proposal.change_id != payload.change_id:
        raise ValueError("settlement change ID does not match outcome proposal")
    if proposal.proposed_change_hash != payload.accepted_change_hash:
        raise ValueError("settlement mutation was not accepted from outcome proposal")
    if proposal.candidate_result_ref not in occurrence.candidate_outcome_refs:
        raise ValueError("outcome proposal is outside committed candidates")
    if proposal.candidate_result_ref != payload.candidate_result_ref:
        raise ValueError("settlement candidate does not match outcome proposal")
    if (
        proposal.proposed_result_id != payload.result_id
        or proposal.proposed_result_payload_ref != payload.result_payload_ref
        or proposal.proposed_result_payload_hash != payload.result_payload_hash
    ):
        raise ValueError("settlement result does not match outcome proposal")
    if proposal.trigger_ref != occurrence.trigger_ref:
        raise ValueError("outcome proposal trigger does not match occurrence")
    if set(proposal.precondition_refs) != set(occurrence.satisfied_precondition_refs):
        raise ValueError("outcome proposal preconditions are stale")
    if set(proposal.observation_refs) != set(payload.observation_refs):
        raise ValueError("settlement observations do not match outcome proposal")
    if payload.settled_at >= proposal.expires_at:
        raise ValueError("outcome proposal expired before settlement")
    if occurrence.activated_at is None or payload.settled_at < occurrence.activated_at:
        raise ValueError("occurrence settlement precedes activation")
    if payload.settled_at > logical_time:
        raise ValueError("occurrence settlement is ahead of authoritative logical time")
    known_observation_ids = {
        observation.observation_id
        for observation in observations
        if observation.occurrence_id == occurrence.occurrence_id
    }
    if not set(payload.observation_refs) <= known_observation_ids:
        raise ValueError("occurrence settlement references unknown observations")
    if not set(payload.observation_refs) <= set(occurrence.observation_refs):
        raise ValueError("occurrence settlement observation is not attached")
    updated = occurrence.model_copy(
        update={
            "entity_revision": occurrence.entity_revision + 1,
            "status": "settled",
            "settled_outcome_ref": payload.candidate_result_ref,
            "result_id": payload.result_id,
            "result_payload_ref": payload.result_payload_ref,
            "result_payload_hash": payload.result_payload_hash,
            "settled_at": payload.settled_at,
            "settlement_event_ref": settlement_event_ref,
            "settlement_world_revision": settlement_world_revision,
            "settlement_payload_hash": settlement_payload_hash,
        }
    )
    return _replace(occurrences, index, updated)


def terminate_occurrence(
    occurrences: tuple[WorldOccurrenceProjection, ...],
    payload: WorldOccurrenceTerminalPayload,
    *,
    target_status: str,
    logical_time: datetime,
) -> tuple[WorldOccurrenceProjection, ...]:
    index, occurrence = _occurrence(occurrences, payload.occurrence_id)
    _expect_revision(occurrence.entity_revision, payload.expected_entity_revision)
    if occurrence.status != "committed":
        raise ValueError("only an unactivated occurrence can cancel or expire")
    if payload.effective_at > logical_time:
        raise ValueError("occurrence terminal transition is ahead of logical time")
    if target_status == "expired" and payload.effective_at < occurrence.time_window.closes_at:
        raise ValueError("occurrence cannot expire before its committed window closes")
    updated = occurrence.model_copy(
        update={
            "entity_revision": occurrence.entity_revision + 1,
            "status": target_status,
            "terminal_reason_ref": payload.reason_ref,
        }
    )
    return _replace(occurrences, index, updated)


def record_outcome_proposal(
    outcome_proposals: tuple[OutcomeProposalProjection, ...],
    payload: OutcomeProposalRecordedPayload,
) -> tuple[OutcomeProposalProjection, ...]:
    if any(item.outcome_proposal_id == payload.outcome_proposal_id for item in outcome_proposals):
        raise ValueError("outcome proposal already exists")
    proposal = OutcomeProposalProjection.model_validate(payload.model_dump())
    return (*outcome_proposals, proposal)


def commit_experience(
    experiences: tuple[ExperienceAuthorityProjection, ...],
    occurrences: tuple[WorldOccurrenceProjection, ...],
    plans: tuple[PlanStateProjection, ...],
    committed_events: tuple[CommittedWorldEventRef, ...],
    execution_receipts: tuple[ExecutionReceipt, ...],
    actions: tuple[Action, ...],
    facts: tuple[FactProjection, ...],
    payload: ExperienceCommittedPayload,
    *,
    logical_time: datetime,
) -> tuple[ExperienceAuthorityProjection, ...]:
    experience = payload.experience
    if any(item.experience_id == experience.experience_id for item in experiences):
        raise ValueError(f"experience {experience.experience_id!r} already exists")
    if experience.values.occurred_to > logical_time:
        raise ValueError("experience is ahead of authoritative logical time")
    _validate_experience_evidence_privacy(
        payload, experiences, facts, occurrences, plans
    )
    identities = {
        (item.source_kind, item.authority_event_ref)
        if isinstance(item, ExperienceOccurrenceSettlementBinding)
        else (item.source_kind, item.receipt_id)
        for candidate in experiences
        if isinstance(candidate, ExperienceProjection)
        for item in candidate.values.source_bindings
    }
    proposed_identities = {
        (item.source_kind, item.authority_event_ref)
        if isinstance(item, ExperienceOccurrenceSettlementBinding)
        else (item.source_kind, item.receipt_id)
        for item in experience.values.source_bindings
    }
    if identities & proposed_identities:
        raise ValueError("experience source authority is already committed elsewhere")
    participants: set[str] = set()
    for binding in experience.values.source_bindings:
        if isinstance(binding, ExperienceOccurrenceSettlementBinding):
            committed = next(
                (
                    item
                    for item in committed_events
                    if item.event_id == binding.authority_event_ref
                ),
                None,
            )
            occurrence = next(
                (
                    item
                    for item in occurrences
                    if item.occurrence_id == binding.occurrence_id
                ),
                None,
            )
            if (
                committed is None
                or committed.event_type != "WorldOccurrenceSettled"
                or committed.world_revision != binding.authority_world_revision
                or committed.payload_hash != binding.authority_payload_hash
                or occurrence is None
                or occurrence.status != "settled"
                or occurrence.entity_revision != binding.occurrence_entity_revision
                or occurrence.result_id != binding.result_id
                or occurrence.result_payload_ref != binding.result_payload_ref
                or occurrence.result_payload_hash != binding.result_payload_hash
                or occurrence.settlement_event_ref != binding.authority_event_ref
                or occurrence.settlement_world_revision != binding.authority_world_revision
                or occurrence.settlement_payload_hash != binding.authority_payload_hash
            ):
                raise ValueError("experience occurrence binding does not resolve exact settlement authority")
            if (
                occurrence.activated_at is None
                or occurrence.settled_at is None
                or experience.values.occurred_from > occurrence.activated_at
                or experience.values.occurred_to < occurrence.settled_at
            ):
                raise ValueError("experience time window does not cover occurrence authority")
            if _PRIVACY_RANK[experience.values.privacy_class] < _PRIVACY_RANK[
                occurrence.visibility
            ]:
                raise ValueError("experience cannot weaken occurrence privacy")
            participants.update(occurrence.participant_refs)
            continue
        if not isinstance(binding, ExperienceExecutionReceiptBinding):
            raise TypeError("unsupported experience source binding")
        receipt = next(
            (item for item in execution_receipts if item.receipt_id == binding.receipt_id),
            None,
        )
        action = next((item for item in actions if item.action_id == binding.action_id), None)
        if (
            receipt is None
            or not receipt.is_terminal
            or receipt.action_id != binding.action_id
            or receipt.result_id != binding.result_id
            or receipt.observed_state != binding.observed_state
            or receipt.raw_payload_hash != binding.raw_payload_hash
            or _canonical_model_hash(receipt) != binding.receipt_hash
            or action is None
            or action.payload_hash != binding.action_payload_hash
            or action.state != binding.observed_state
        ):
            raise ValueError("experience receipt binding does not resolve exact receipt authority")
        if (
            receipt.received_at < action.logical_time
            or receipt.received_at < action.created_at
        ):
            raise ValueError("experience receipt authority chronology is reversed")
        if (
            experience.values.occurred_from > action.logical_time
            or experience.values.occurred_to < action.logical_time
            or experience.values.occurred_from > receipt.received_at
            or experience.values.occurred_to < receipt.received_at
        ):
            raise ValueError("experience time window does not cover action and receipt authority")
        participants.add(action.actor)
        if _PRIVACY_RANK[experience.values.privacy_class] < _PRIVACY_RANK["private"]:
            raise ValueError("external-result experience must remain private")
    if set(experience.values.participant_refs) != participants:
        raise ValueError("experience participants must exactly match source authority actors")
    return (*experiences, experience)


def _validate_experience_evidence_privacy(
    payload: ExperienceCommittedPayload,
    experiences: tuple[ExperienceAuthorityProjection, ...],
    facts: tuple[FactProjection, ...],
    occurrences: tuple[WorldOccurrenceProjection, ...],
    plans: tuple[PlanStateProjection, ...],
) -> None:
    source_minimum = {
        "committed_fact": 0,
        "committed_world_event": 0,
        "settled_world_event": 0,
        "clock_observation": 0,
        "observed_message": 2,
        "committed_experience": 2,
        "settled_external_result": 2,
        "active_plan": 2,
        "operator_observation": 3,
    }
    purpose_minimum = {
        "current_fact": 0,
        "past_experience": 2,
        "future_plan": 2,
        "conversation_continuity": 2,
        "private_hypothesis": 3,
        "action_authorization": 3,
    }
    requirements: list[int] = []
    for item in payload.evidence_refs:
        source_floor = source_minimum.get(item.evidence_type, 4)
        if item.evidence_type == "committed_fact":
            fact = next(
                (
                    candidate
                    for candidate in facts
                    if candidate.origin.accepted_event_ref == item.ref_id
                ),
                None,
            )
            if fact is None:
                raise ValueError("experience privacy cannot resolve committed fact")
            source_floor = max(source_floor, _PRIVACY_RANK[fact.values.privacy_class])
        elif item.evidence_type == "committed_experience":
            prior = next(
                (
                    candidate
                    for candidate in experiences
                    if isinstance(candidate, ExperienceProjection)
                    and candidate.origin.accepted_event_ref == item.ref_id
                ),
                None,
            )
            if prior is None:
                raise ValueError("experience privacy cannot resolve committed experience")
            source_floor = max(
                source_floor, _PRIVACY_RANK[prior.values.privacy_class]
            )
        elif item.evidence_type == "active_plan":
            plan = next(
                (candidate for candidate in plans if candidate.plan_id == item.ref_id),
                None,
            )
            if plan is None:
                raise ValueError("experience privacy cannot resolve active plan")
            source_floor = max(source_floor, _PRIVACY_RANK[plan.privacy_class])
        elif item.evidence_type in {
            "committed_world_event",
            "settled_world_event",
        }:
            occurrence = next(
                (
                    candidate
                    for candidate in occurrences
                    if candidate.settlement_event_ref == item.ref_id
                ),
                None,
            )
            if item.evidence_type == "settled_world_event" and occurrence is None:
                raise ValueError("experience privacy cannot resolve settled occurrence")
            if occurrence is not None:
                source_floor = max(
                    source_floor, _PRIVACY_RANK[occurrence.visibility]
                )
        requirements.append(
            max(source_floor, purpose_minimum[item.claim_purpose])
        )
    required = max(requirements)
    if _PRIVACY_RANK[payload.experience.values.privacy_class] < required:
        raise ValueError("experience evidence/privacy matrix rejects broad visibility")


def commit_legacy_experience(
    experiences: tuple[ExperienceAuthorityProjection, ...],
    payload: LegacyExperienceCommittedPayload,
) -> tuple[ExperienceAuthorityProjection, ...]:
    experience: LegacyExperienceProjection = payload.experience
    if any(item.experience_id == experience.experience_id for item in experiences):
        raise ValueError("legacy experience identity already exists")
    return (*experiences, experience)


def _canonical_model_hash(value: ExecutionReceipt) -> str:
    import hashlib
    import json

    encoded = json.dumps(
        value.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _expect_revision(actual: int, expected: int) -> None:
    if actual != expected:
        raise ValueError(f"stale entity revision: expected {expected}, current {actual}")


def _occurrence(
    occurrences: tuple[WorldOccurrenceProjection, ...], occurrence_id: str
) -> tuple[int, WorldOccurrenceProjection]:
    for index, occurrence in enumerate(occurrences):
        if occurrence.occurrence_id == occurrence_id:
            return index, occurrence
    raise ValueError(f"occurrence {occurrence_id!r} does not exist")


def _replace(
    values: tuple[WorldOccurrenceProjection, ...],
    index: int,
    value: WorldOccurrenceProjection,
) -> tuple[WorldOccurrenceProjection, ...]:
    return (*values[:index], value, *values[index + 1 :])

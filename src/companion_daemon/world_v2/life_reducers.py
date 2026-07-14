"""Pure lifecycle reducers for plans, NPCs, occurrences, and lived experience."""

from __future__ import annotations

from datetime import datetime

from .life_events import (
    ActivityPlannedPayload,
    ActivityTransitionPayload,
    ExperienceCommittedPayload,
    NpcRegisteredPayload,
    OutcomeObservationRecordedPayload,
    OutcomeProposalRecordedPayload,
    WorldOccurrenceActivatedPayload,
    WorldOccurrenceCommittedPayload,
    WorldOccurrenceSettledPayload,
    WorldOccurrenceTerminalPayload,
)
from .schemas import (
    ExperienceProjection,
    CommittedWorldEventRef,
    ExecutionReceipt,
    NpcProjection,
    OutcomeObservationProjection,
    OutcomeProposalProjection,
    PlanStateProjection,
    WorldOccurrenceProjection,
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
    if any(
        npc.stable_identity_ref == payload.npc.stable_identity_ref for npc in npcs
    ):
        raise ValueError("NPC stable identity is already registered")
    return (*npcs, payload.npc)


def plan_activity(
    plans: tuple[PlanStateProjection, ...],
    npcs: tuple[NpcProjection, ...],
    payload: ActivityPlannedPayload,
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
            (
                plan
                for plan in plans
                if plan.plan_id == payload.plan.supersedes_plan_id
            ),
            None,
        )
        if predecessor is None or predecessor.status != "abandoned":
            raise ValueError("replacement plan requires an abandoned predecessor")
    return (*plans, payload.plan)


def transition_activity(
    plans: tuple[PlanStateProjection, ...],
    payload: ActivityTransitionPayload,
    *,
    target_status: str,
    allowed_statuses: frozenset[str],
    logical_time: datetime,
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
    if payload.transitioned_at.tzinfo is None or payload.transitioned_at.utcoffset() is None:
        raise ValueError("activity transition time must be timezone-aware")
    if (
        plan.last_transitioned_at is not None
        and payload.transitioned_at < plan.last_transitioned_at
    ):
        raise ValueError("activity transition time cannot move backwards")
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
        npc.npc_id: npc
        for npc in npcs
        if f"npc:{npc.npc_id}" in occurrence.participant_refs
    }
    if any(
        _PRIVACY_RANK[occurrence.visibility]
        < _PRIVACY_RANK[npc.privacy_class]
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
        occurrence.time_window.opens_at
        <= payload.activated_at
        < occurrence.time_window.closes_at
    ):
        raise ValueError("occurrence activation is outside its committed window")
    if not set(occurrence.precondition_refs) <= set(
        payload.satisfied_precondition_refs
    ):
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
                raise ValueError(
                    "outcome observation references unverified world evidence"
                )
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
    if set(proposal.precondition_refs) != set(
        occurrence.satisfied_precondition_refs
    ):
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
            "result_id": payload.result_id,
            "result_payload_ref": payload.result_payload_ref,
            "result_payload_hash": payload.result_payload_hash,
            "settled_at": payload.settled_at,
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
    if any(
        item.outcome_proposal_id == payload.outcome_proposal_id
        for item in outcome_proposals
    ):
        raise ValueError("outcome proposal already exists")
    proposal = OutcomeProposalProjection.model_validate(payload.model_dump())
    return (*outcome_proposals, proposal)


def commit_experience(
    experiences: tuple[ExperienceProjection, ...],
    occurrences: tuple[WorldOccurrenceProjection, ...],
    execution_receipts: tuple[ExecutionReceipt, ...],
    payload: ExperienceCommittedPayload,
    *,
    logical_time: datetime,
) -> tuple[ExperienceProjection, ...]:
    experience = payload.experience
    if any(item.experience_id == experience.experience_id for item in experiences):
        raise ValueError(f"experience {experience.experience_id!r} already exists")
    if experience.occurred_to > logical_time:
        raise ValueError("experience is ahead of authoritative logical time")
    settled_occurrences = {
        occurrence.occurrence_id: occurrence
        for occurrence in occurrences
        if occurrence.status == "settled"
    }
    occurrence_participants: set[str] = set()
    for occurrence_ref in experience.occurrence_refs:
        occurrence = settled_occurrences.get(occurrence_ref)
        if occurrence is None:
            raise ValueError("experience references an occurrence that is not settled")
        if occurrence.result_id not in experience.result_refs:
            raise ValueError("experience omits its occurrence settlement result")
        occurrence_participants.update(occurrence.participant_refs)
        if (
            occurrence.activated_at is None
            or occurrence.settled_at is None
            or experience.occurred_from > occurrence.activated_at
            or experience.occurred_to < occurrence.settled_at
        ):
            raise ValueError("experience time window does not cover occurrence")
        if _PRIVACY_RANK[experience.privacy_class] < _PRIVACY_RANK[occurrence.visibility]:
            raise ValueError("experience cannot weaken occurrence privacy")
    if experience.occurrence_refs and set(experience.participant_refs) != occurrence_participants:
        raise ValueError("experience participants must match source occurrences")
    if not experience.occurrence_refs:
        terminal_result_ids = {
            receipt.result_id for receipt in execution_receipts if receipt.is_terminal
        }
        if not set(experience.result_refs) <= terminal_result_ids:
            raise ValueError("experience has no verified settled source")
        if not any(
            evidence.evidence_type == "settled_external_result"
            for evidence in experience.evidence_refs
        ):
            raise ValueError("external-result experience lacks matching evidence")
    return (*experiences, experience)


def _expect_revision(actual: int, expected: int) -> None:
    if actual != expected:
        raise ValueError(
            f"stale entity revision: expected {expected}, current {actual}"
        )


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

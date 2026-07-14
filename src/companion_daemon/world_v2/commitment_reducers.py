"""Deterministic authority for private commitments, with no behavioral effects."""

from __future__ import annotations

from datetime import datetime
import hashlib

from .commitment_events import CommitmentChangedPayload, CommitmentClockTransitionPayload
from .schemas import (
    Action,
    CommittedWorldEventRef,
    CommitmentProjection,
    CommitmentTransitionProjection,
    ExecutionReceipt,
    MessageObservationRef,
    ThreadProjection,
    ThreadTransitionProjection,
)


ACTIVE_COMMITMENT_STATUSES = frozenset({"open", "due"})
TERMINAL_COMMITMENT_STATUSES = frozenset({"fulfilled", "broken", "released"})
COMMITMENT_DEADLINE_POLICY_VERSION = "commitment-deadline-policy.1"
COMMITMENT_DEADLINE_POLICY_DIGEST = hashlib.sha256(
    COMMITMENT_DEADLINE_POLICY_VERSION.encode()
).hexdigest()
_PRIVACY_RANK = {"public": 0, "shareable": 1, "personal": 2, "private": 3, "withhold": 4}


def reduce_commitment(
    commitments: tuple[CommitmentProjection, ...],
    history: tuple[CommitmentTransitionProjection, ...],
    payload: CommitmentChangedPayload,
    *,
    event_type: str,
    logical_time: datetime,
    committed_events: tuple[CommittedWorldEventRef, ...],
    execution_receipts: tuple[ExecutionReceipt, ...],
    actions: tuple[Action, ...],
    threads: tuple[ThreadProjection, ...],
    thread_history: tuple[ThreadTransitionProjection, ...],
    message_observations: tuple[MessageObservationRef, ...],
) -> tuple[tuple[CommitmentProjection, ...], tuple[CommitmentTransitionProjection, ...]]:
    expected_operation = {
        "PrivateCommitmentOpened": "open",
        "PrivateCommitmentFulfilled": "fulfill",
        "PrivateCommitmentBroken": "break",
        "PrivateCommitmentReleased": "release",
    }[event_type]
    if payload.operation != expected_operation:
        raise ValueError("commitment event type does not match operation")
    after = payload.commitment_after
    current = next(
        (item for item in commitments if item.commitment_id == after.commitment_id), None
    )
    if payload.operation == "open":
        if current is not None:
            raise ValueError("commitment identity already exists")
        if after.values.status != "open":
            raise ValueError("commitment open must create open responsibility")
        if after.opened_at != logical_time or after.updated_at != logical_time:
            raise ValueError("commitment open timestamps must match logical time")
        if after.values.due_window.closes_at <= logical_time:
            raise ValueError("commitment deadline must remain in the future")
        if any(
            item.values.status in ACTIVE_COMMITMENT_STATUSES
            and item.semantic_fingerprint == after.semantic_fingerprint
            for item in commitments
        ):
            raise ValueError("active semantic fingerprint already exists")
        _validate_open_contract_is_future(
            after,
            execution_receipts=execution_receipts,
            threads=threads,
        )
        _validate_predecessor_lineage(
            commitments, after, committed_events=committed_events
        )
        _validate_privacy(after)
        updated = (*commitments, after)
    else:
        if current is None or payload.commitment_before != current:
            raise ValueError("commitment before image does not match current authority")
        if current.entity_revision != payload.expected_entity_revision:
            raise ValueError("commitment entity revision compare-and-swap failed")
        if current.values.status in TERMINAL_COMMITMENT_STATUSES:
            raise ValueError("terminal commitment cannot reopen or resettle")
        _validate_immutable_authority(current, after, logical_time=logical_time)
        _validate_terminal_transition(
            current,
            after,
            operation=payload.operation,
            committed_events=committed_events,
            execution_receipts=execution_receipts,
            actions=actions,
            threads=threads,
            thread_history=thread_history,
            commitment_history=history,
            message_observations=message_observations,
        )
        _validate_privacy(after)
        updated = tuple(
            after if item.commitment_id == after.commitment_id else item
            for item in commitments
        )
    transition = CommitmentTransitionProjection(
        transition_id=payload.transition_id,
        commitment_id=after.commitment_id,
        entity_revision=after.entity_revision,
        operation=payload.operation,
        values_before=payload.commitment_before.values if payload.commitment_before else None,
        values_after=after.values,
        change_id=payload.change_id,
        authority_mode="accepted_proposal",
        accepted_event_ref=after.origin.accepted_event_ref,
        accepted_at=logical_time,
    )
    if any(item.transition_id == transition.transition_id for item in history):
        raise ValueError("commitment transition identity already exists")
    return updated, (*history, transition)


def reduce_commitment_clock(
    commitments: tuple[CommitmentProjection, ...],
    history: tuple[CommitmentTransitionProjection, ...],
    payload: CommitmentClockTransitionPayload,
    *,
    logical_time: datetime,
) -> tuple[tuple[CommitmentProjection, ...], tuple[CommitmentTransitionProjection, ...]]:
    """Reduce a Phase-5 advance proposal; this authority module never schedules its own tick."""
    current = next(
        (
            item
            for item in commitments
            if item.commitment_id == payload.commitment_after.commitment_id
        ),
        None,
    )
    if current is None or current != payload.commitment_before:
        raise ValueError("commitment clock before image does not match current authority")
    if current.entity_revision != payload.expected_entity_revision:
        raise ValueError("commitment clock entity revision compare-and-swap failed")
    if current.values.status not in ACTIVE_COMMITMENT_STATUSES:
        raise ValueError("terminal commitment cannot transition on clock")
    if (
        payload.policy_version != COMMITMENT_DEADLINE_POLICY_VERSION
        or payload.policy_digest != COMMITMENT_DEADLINE_POLICY_DIGEST
    ):
        raise ValueError("commitment deadline policy artifact is not installed")
    after = payload.commitment_after
    _validate_immutable_authority(current, after, logical_time=logical_time)
    expected_sources = (
        current.values.source_evidence_refs
        if payload.clock_evidence_ref in current.values.source_evidence_refs
        else (*current.values.source_evidence_refs, payload.clock_evidence_ref)
    )
    if payload.operation == "due":
        if current.values.status != "open":
            raise ValueError("commitment can become due only once")
        if logical_time < current.values.due_window.opens_at:
            raise ValueError("commitment due transition is outside its due window")
        expected_values = current.values.model_copy(
            update={"source_evidence_refs": expected_sources, "status": "due"}
        )
    else:
        if current.values.status != "due":
            raise ValueError("commitment deadline break requires due status")
        if logical_time < current.values.due_window.closes_at:
            raise ValueError("commitment cannot break before its deadline")
        expected_values = current.values.model_copy(
            update={
                "source_evidence_refs": expected_sources,
                "status": "broken",
                "settlement_evidence_ref": payload.clock_evidence_ref.ref_id,
                "settlement_reason_code": "deadline_elapsed",
            }
        )
    if (
        after.values != expected_values
        or after.semantic_fingerprint != current.semantic_fingerprint
        or after.opened_at != current.opened_at
        or after.origin.authority_mode != "mechanical_clock"
        or after.origin.change_id != payload.change_id
        or after.origin.transition_id != payload.transition_id
        or after.origin.policy_refs != current.origin.policy_refs
    ):
        raise ValueError("commitment clock after image is not the mechanical transition")
    transition = CommitmentTransitionProjection(
        transition_id=payload.transition_id,
        commitment_id=after.commitment_id,
        entity_revision=after.entity_revision,
        operation=payload.operation,
        values_before=current.values,
        values_after=after.values,
        change_id=payload.change_id,
        authority_mode="mechanical_clock",
        accepted_event_ref=after.origin.accepted_event_ref,
        accepted_at=logical_time,
    )
    if any(item.transition_id == transition.transition_id for item in history):
        raise ValueError("commitment transition identity already exists")
    updated = tuple(
        after if item.commitment_id == after.commitment_id else item for item in commitments
    )
    return updated, (*history, transition)


def _validate_open_contract_is_future(
    commitment: CommitmentProjection,
    *,
    execution_receipts: tuple[ExecutionReceipt, ...],
    threads: tuple[ThreadProjection, ...],
) -> None:
    contract = commitment.values.fulfillment_contract
    if contract.contract_kind == "execution_receipt" and any(
        item.action_id == contract.expected_action_id
        and item.observed_state == "delivered"
        for item in execution_receipts
    ):
        raise ValueError("commitment cannot open after its action was delivered")
    if contract.contract_kind == "thread_resolution":
        thread = next(
            (item for item in threads if item.thread_id == contract.expected_thread_id), None
        )
        if thread is not None and thread.values.status == "resolved":
            raise ValueError("commitment cannot open after its thread was resolved")


def _validate_immutable_authority(
    current: CommitmentProjection,
    after: CommitmentProjection,
    *,
    logical_time: datetime,
) -> None:
    if (
        after.semantic_fingerprint != current.semantic_fingerprint
        or after.opened_at != current.opened_at
        or after.updated_at != logical_time
        or after.entity_revision != current.entity_revision + 1
    ):
        raise ValueError("commitment transition changed immutable identity")
    before, values = current.values, after.values
    immutable = (
        "owner_ref",
        "subject_ref",
        "content_ref",
        "content_hash",
        "anchor_evidence_refs",
        "importance_bp",
        "due_window",
        "persistence_level",
        "fulfillment_contract",
        "predecessor_commitment_ref",
        "lineage_kind",
    )
    if any(getattr(before, name) != getattr(values, name) for name in immutable):
        raise ValueError("commitment transition changed frozen responsibility")
    if values.source_evidence_refs[: len(before.source_evidence_refs)] != before.source_evidence_refs:
        raise ValueError("commitment source evidence is append-only")
    if _PRIVACY_RANK[values.privacy_class] < _PRIVACY_RANK[before.privacy_class]:
        raise ValueError("commitment privacy cannot be loosened")


def _validate_terminal_transition(
    current: CommitmentProjection,
    after: CommitmentProjection,
    *,
    operation: str,
    committed_events: tuple[CommittedWorldEventRef, ...],
    execution_receipts: tuple[ExecutionReceipt, ...],
    actions: tuple[Action, ...],
    threads: tuple[ThreadProjection, ...],
    thread_history: tuple[ThreadTransitionProjection, ...],
    commitment_history: tuple[CommitmentTransitionProjection, ...],
    message_observations: tuple[MessageObservationRef, ...],
) -> None:
    expected_status = {"fulfill": "fulfilled", "break": "broken", "release": "released"}[
        operation
    ]
    if after.values.status != expected_status:
        raise ValueError("commitment terminal operation has wrong status")
    new_sources = after.values.source_evidence_refs[len(current.values.source_evidence_refs) :]
    settlement = next(
        (
            item
            for item in new_sources
            if item.ref_id == after.values.settlement_evidence_ref
        ),
        None,
    )
    if settlement is None:
        raise ValueError("commitment settlement must use newly appended evidence")
    if operation == "fulfill":
        if after.values.settlement_reason_code != "evidence_satisfied":
            raise ValueError("fulfilled commitment requires satisfied evidence reason")
        _validate_fulfillment_contract(
            current,
            settlement,
            committed_events=committed_events,
            execution_receipts=execution_receipts,
            actions=actions,
            threads=threads,
            thread_history=thread_history,
        )
    elif operation == "break":
        if after.values.settlement_reason_code != "authoritative_failure":
            raise ValueError("accepted commitment break requires authoritative failure reason")
        _validate_evidence_bound_failure(
            current, settlement, execution_receipts=execution_receipts, actions=actions
        )
    elif after.values.settlement_reason_code not in {
        "user_withdrew",
        "obsolete",
        "precondition_failed",
        "boundary_or_safety_conflict",
        "operator_correction",
    }:
        raise ValueError("released commitment requires an explicit release reason")
    else:
        _validate_release_chronology(
            current,
            settlement,
            commitment_history=commitment_history,
            committed_events=committed_events,
            execution_receipts=execution_receipts,
            message_observations=message_observations,
        )


def _validate_fulfillment_contract(
    commitment: CommitmentProjection,
    evidence,
    *,
    committed_events,
    execution_receipts,
    actions,
    threads,
    thread_history,
) -> None:
    contract = commitment.values.fulfillment_contract
    if evidence.evidence_type != contract.evidence_type:
        raise ValueError("commitment fulfillment contract evidence type does not match")
    if contract.contract_kind == "execution_receipt":
        receipt = next(
            (
                item
                for item in execution_receipts
                if evidence.ref_id in {item.receipt_id, item.result_id, item.source_event_id}
            ),
            None,
        )
        action = next(
            (item for item in actions if item.action_id == contract.expected_action_id), None
        )
        if (
            receipt is None
            or action is None
            or receipt.action_id != contract.expected_action_id
            or action.payload_hash != contract.expected_action_payload_hash
            or action.state != "delivered"
            or receipt.observed_state != "delivered"
            or receipt.received_at < commitment.opened_at
            or (contract.expected_result_id and receipt.result_id != contract.expected_result_id)
        ):
            raise ValueError("commitment fulfillment contract receipt does not match")
        return
    committed = next((item for item in committed_events if item.event_id == evidence.ref_id), None)
    transition = next(
        (item for item in thread_history if item.accepted_event_ref == evidence.ref_id), None
    )
    thread = next(
        (item for item in threads if item.thread_id == contract.expected_thread_id), None
    )
    if (
        committed is None
        or committed.event_type != "ThreadResolved"
        or transition is None
        or transition.thread_id != contract.expected_thread_id
        or transition.operation != "resolve"
        or transition.accepted_at < commitment.opened_at
        or thread is None
        or thread.values.status != "resolved"
        or (contract.expected_ref_id and evidence.ref_id != contract.expected_ref_id)
        or (
            contract.expected_world_revision
            and evidence.source_world_revision != contract.expected_world_revision
        )
        or (contract.expected_immutable_hash and evidence.immutable_hash != contract.expected_immutable_hash)
    ):
        raise ValueError("commitment fulfillment contract thread resolution does not match")


def _validate_evidence_bound_failure(
    commitment: CommitmentProjection,
    evidence,
    *,
    execution_receipts: tuple[ExecutionReceipt, ...],
    actions: tuple[Action, ...],
) -> None:
    contract = commitment.values.fulfillment_contract
    if contract.contract_kind != "execution_receipt":
        raise ValueError("commitment accepted break requires a target-bound execution receipt")
    receipt = next(
        (
            item
            for item in execution_receipts
            if evidence.ref_id in {item.receipt_id, item.result_id, item.source_event_id}
        ),
        None,
    )
    action = next((item for item in actions if item.action_id == contract.expected_action_id), None)
    if (
        receipt is None
        or action is None
        or receipt.action_id != contract.expected_action_id
        or action.payload_hash != contract.expected_action_payload_hash
        or action.state not in {"failed", "cancelled", "expired"}
        or action.state != receipt.observed_state
        or receipt.observed_state not in {"failed", "cancelled", "expired"}
        or receipt.received_at < commitment.opened_at
    ):
        raise ValueError("commitment break evidence does not match its target action")


def _validate_privacy(commitment: CommitmentProjection) -> None:
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
    required = max(
        3,
        *(
            max(source_minimum.get(item.evidence_type, 4), purpose_minimum[item.claim_purpose])
            for item in commitment.values.source_evidence_refs
        ),
    )
    if _PRIVACY_RANK[commitment.values.privacy_class] < required:
        raise ValueError("commitment evidence/privacy matrix rejects broad visibility")


def _validate_release_chronology(
    commitment: CommitmentProjection,
    evidence,
    *,
    commitment_history: tuple[CommitmentTransitionProjection, ...],
    committed_events: tuple[CommittedWorldEventRef, ...],
    execution_receipts: tuple[ExecutionReceipt, ...],
    message_observations: tuple[MessageObservationRef, ...],
) -> None:
    opening = next(
        (
            item
            for item in commitment_history
            if item.commitment_id == commitment.commitment_id and item.operation == "open"
        ),
        None,
    )
    open_event = (
        next(
            (
                item
                for item in committed_events
                if opening is not None and item.event_id == opening.accepted_event_ref
            ),
            None,
        )
        if opening is not None
        else None
    )
    if open_event is None:
        raise ValueError("commitment release cannot resolve opening chronology")
    if evidence.evidence_type == "observed_message":
        message = next(
            (item for item in message_observations if item.observation_id == evidence.ref_id), None
        )
        valid = message is not None and message.world_revision > open_event.world_revision
    elif evidence.evidence_type in {"committed_world_event", "settled_world_event"}:
        event = next((item for item in committed_events if item.event_id == evidence.ref_id), None)
        valid = event is not None and event.world_revision > open_event.world_revision
    elif evidence.evidence_type == "settled_external_result":
        receipt = next(
            (
                item
                for item in execution_receipts
                if evidence.ref_id in {item.receipt_id, item.result_id, item.source_event_id}
            ),
            None,
        )
        receipt_event = (
            next(
                (
                    item
                    for item in committed_events
                    if receipt is not None and item.event_id == receipt.source_event_id
                ),
                None,
            )
            if receipt is not None
            else None
        )
        valid = (
            receipt is not None
            and receipt_event is not None
            and receipt_event.world_revision > open_event.world_revision
            and receipt.received_at >= commitment.opened_at
        )
    else:
        valid = False
    if not valid:
        raise ValueError("commitment release evidence does not postdate opening authority")


def _validate_predecessor_lineage(
    commitments: tuple[CommitmentProjection, ...],
    candidate: CommitmentProjection,
    *,
    committed_events: tuple[CommittedWorldEventRef, ...],
) -> None:
    predecessor_ref = candidate.values.predecessor_commitment_ref
    if predecessor_ref is None:
        return
    if predecessor_ref == candidate.commitment_id:
        raise ValueError("commitment cannot succeed itself")
    predecessor = next(
        (item for item in commitments if item.commitment_id == predecessor_ref), None
    )
    if predecessor is None or predecessor.values.status not in TERMINAL_COMMITMENT_STATUSES:
        raise ValueError("commitment predecessor must be an existing terminal responsibility")
    lineage_kind = candidate.values.lineage_kind
    if lineage_kind == "correction" and not (
        predecessor.values.status == "released"
        and predecessor.values.settlement_reason_code == "operator_correction"
    ):
        raise ValueError("commitment correction requires an operator-corrected release")
    if lineage_kind == "replacement" and not (
        predecessor.values.status == "broken"
        or (
            predecessor.values.status == "released"
            and predecessor.values.settlement_reason_code != "operator_correction"
        )
    ):
        raise ValueError("commitment replacement requires broken or withdrawn predecessor")
    if lineage_kind == "renewal" and predecessor.values.status != "fulfilled":
        raise ValueError("commitment renewal requires fulfilled predecessor")
    if (
        predecessor.values.owner_ref != candidate.values.owner_ref
        or predecessor.values.subject_ref != candidate.values.subject_ref
        or _PRIVACY_RANK[candidate.values.privacy_class]
        < _PRIVACY_RANK[predecessor.values.privacy_class]
    ):
        raise ValueError("commitment predecessor authority is incompatible")
    evidence = next(
        (
            item
            for item in candidate.values.anchor_evidence_refs
            if item.ref_id == predecessor.origin.accepted_event_ref
            and item.evidence_type == "committed_world_event"
        ),
        None,
    )
    authority = next(
        (
            item
            for item in committed_events
            if item.event_id == predecessor.origin.accepted_event_ref
        ),
        None,
    )
    if (
        evidence is None
        or authority is None
        or evidence.source_world_revision != authority.world_revision
        or evidence.immutable_hash != authority.payload_hash
    ):
        raise ValueError("commitment predecessor requires its terminal event evidence")
    visited = {candidate.commitment_id}
    cursor = predecessor
    while cursor.values.predecessor_commitment_ref is not None:
        if cursor.commitment_id in visited:
            raise ValueError("commitment predecessor cycle is forbidden")
        visited.add(cursor.commitment_id)
        next_ref = cursor.values.predecessor_commitment_ref
        next_item = next(
            (item for item in commitments if item.commitment_id == next_ref), None
        )
        if next_item is None:
            break
        cursor = next_item

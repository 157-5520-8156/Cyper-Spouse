"""Pure reducers for conversation-thread bookkeeping; no response policy lives here."""

from __future__ import annotations

from datetime import datetime
import hashlib

from .schemas import ThreadProjection, ThreadTransitionProjection
from .thread_events import ThreadChangedPayload, ThreadExpiredPayload


TERMINAL_THREAD_STATUSES = frozenset({"resolved", "superseded", "cancelled", "expired"})
_EVENT_OPERATION = {
    "ThreadOpened": "open",
    "ThreadUpdated": "update",
    "ThreadResolved": "resolve",
    "ThreadCancelled": "cancel",
    "ThreadSuperseded": "supersede",
    "ThreadCompensated": "compensate",
    # Dedicated delivered-media authority lane.  It shares the pure state
    # transition reducer, not the generic Thread proposal/acceptance path.
    "MediaDeliveryThreadOpened": "open",
    "MediaDeliveryThreadUpdated": "update",
}
_PRIVACY_RANK = {"public": 0, "shareable": 1, "personal": 2, "private": 3, "withhold": 4}
THREAD_EXPIRY_POLICY_VERSION = "thread-expiry-policy.1"
THREAD_EXPIRY_POLICY_DIGEST = hashlib.sha256(THREAD_EXPIRY_POLICY_VERSION.encode()).hexdigest()


def reduce_thread(
    threads: tuple[ThreadProjection, ...],
    history: tuple[ThreadTransitionProjection, ...],
    payload: ThreadChangedPayload,
    *,
    event_type: str,
    logical_time: datetime,
) -> tuple[tuple[ThreadProjection, ...], tuple[ThreadTransitionProjection, ...]]:
    if _EVENT_OPERATION[event_type] != payload.operation:
        raise ValueError("thread event type does not match operation")
    after = payload.thread_after
    current = next((item for item in threads if item.thread_id == after.thread_id), None)
    if payload.operation == "open":
        if current is not None:
            raise ValueError("thread identity already exists")
        if after.values.status != "open":
            raise ValueError("thread open must create an open matter")
        if after.opened_at != logical_time or after.updated_at != logical_time:
            raise ValueError("thread open timestamps must match logical time")
        if after.values.expires_at is not None and after.values.expires_at <= logical_time:
            raise ValueError("thread expiry must be in the future when opened")
        if after.values.due_window is not None and (
            after.values.due_window.opens_at < logical_time
            or after.values.due_window.closes_at <= logical_time
        ):
            raise ValueError("thread due window cannot precede its opening")
        if any(
            item.values.status == "open" and item.semantic_fingerprint == after.semantic_fingerprint
            for item in threads
        ):
            raise ValueError("active semantic fingerprint already exists")
        _validate_privacy(after)
        updated = (*threads, after)
    else:
        if current is None:
            raise ValueError("thread transition requires an existing entity")
        if current.entity_revision != payload.expected_entity_revision:
            raise ValueError("thread entity revision compare-and-swap failed")
        if payload.thread_before != current:
            raise ValueError("thread before image does not match current entity")
        if (
            after.semantic_fingerprint != current.semantic_fingerprint
            or after.origin.policy_refs != current.origin.policy_refs
        ):
            raise ValueError("thread fingerprint and policy authority are immutable")
        if current.values.status in TERMINAL_THREAD_STATUSES:
            raise ValueError("terminal thread cannot transition or reopen")
        if after.opened_at != current.opened_at or after.updated_at != logical_time:
            raise ValueError("thread transition timestamps are not authoritative")
        if payload.operation == "compensate":
            _validate_compensation(history, current, payload)
        else:
            _validate_forward_transition(threads, current, payload, logical_time=logical_time)
        _validate_privacy(after)
        if any(
            item.thread_id != after.thread_id
            and item.values.status == "open"
            and after.values.status == "open"
            and item.semantic_fingerprint == after.semantic_fingerprint
            for item in threads
        ):
            raise ValueError("active semantic fingerprint already exists")
        updated = tuple(after if item.thread_id == after.thread_id else item for item in threads)
    transition = ThreadTransitionProjection(
        transition_id=payload.transition_id,
        thread_id=after.thread_id,
        entity_revision=after.entity_revision,
        operation=payload.operation,
        values_before=payload.thread_before.values if payload.thread_before else None,
        values_after=after.values,
        change_id=payload.change_id,
        policy_refs=payload.policy_refs,
        accepted_event_ref=after.origin.accepted_event_ref,
        accepted_at=logical_time,
        compensates_transition_id=payload.compensates_transition_id,
    )
    if any(item.transition_id == transition.transition_id for item in history):
        raise ValueError("thread transition identity already exists")
    return updated, (*history, transition)


def _validate_forward_transition(
    threads: tuple[ThreadProjection, ...],
    current: ThreadProjection,
    payload: ThreadChangedPayload,
    *,
    logical_time: datetime,
) -> None:
    before, after = current.values, payload.thread_after.values
    if (
        after.kind,
        after.subject_ref,
        after.conversation_ref,
        after.anchor_evidence_refs,
        after.resolution_contract_ref,
    ) != (
        before.kind,
        before.subject_ref,
        before.conversation_ref,
        before.anchor_evidence_refs,
        before.resolution_contract_ref,
    ):
        raise ValueError("thread semantic anchor is immutable")
    old_refs = before.source_evidence_refs
    if after.source_evidence_refs[: len(old_refs)] != old_refs:
        raise ValueError("thread source evidence is append-only")
    if _PRIVACY_RANK[after.privacy_class] < _PRIVACY_RANK[before.privacy_class]:
        raise ValueError("thread privacy cannot be loosened")
    if payload.operation == "update":
        if after.status != "open":
            raise ValueError("thread update must remain open")
        if after.expires_at is not None and after.expires_at <= logical_time:
            raise ValueError("thread update cannot install a retroactive expiry")
        if after.due_window is not None and after.due_window.closes_at <= logical_time:
            raise ValueError("thread update cannot install a closed due window")
        if after == before:
            raise ValueError("thread update cannot be a no-op")
    elif payload.operation == "resolve":
        if after.status != "resolved":
            raise ValueError("thread resolve requires resolved status")
        _validate_closure_is_narrow(before, after)
        new_ref_ids = {item.ref_id for item in after.source_evidence_refs[len(old_refs) :]}
        if after.resolution_ref not in new_ref_ids:
            raise ValueError("thread resolution must reference newly appended authority evidence")
    elif payload.operation == "cancel":
        if after.status != "cancelled":
            raise ValueError("thread cancel requires cancelled status")
        _validate_closure_is_narrow(before, after)
        new_ref_ids = {item.ref_id for item in after.source_evidence_refs[len(old_refs) :]}
        if after.cancellation_evidence_ref not in new_ref_ids:
            raise ValueError("thread cancellation must reference newly appended authority evidence")
    elif payload.operation == "supersede":
        if after.status != "superseded":
            raise ValueError("thread supersede requires superseded status")
        if after.superseded_by_thread_ref == current.thread_id:
            raise ValueError("thread cannot supersede itself")
        _validate_closure_is_narrow(before, after)
        successor = next(
            (item for item in threads if item.thread_id == after.superseded_by_thread_ref), None
        )
        if successor is None or successor.values.status != "open":
            raise ValueError("thread supersede requires an existing active successor")
        if (
            successor.values.subject_ref != before.subject_ref
            or successor.values.conversation_ref != before.conversation_ref
            or _PRIVACY_RANK[successor.values.privacy_class] < _PRIVACY_RANK[before.privacy_class]
            or current.thread_id not in successor.values.predecessor_thread_refs
        ):
            raise ValueError("thread successor is not structurally linked to its predecessor")
        cursor = successor
        visited: set[str] = set()
        while cursor.values.superseded_by_thread_ref:
            if (
                cursor.thread_id in visited
                or cursor.values.superseded_by_thread_ref == current.thread_id
            ):
                raise ValueError("thread supersession cycle is forbidden")
            visited.add(cursor.thread_id)
            next_item = next(
                (
                    item
                    for item in threads
                    if item.thread_id == cursor.values.superseded_by_thread_ref
                ),
                None,
            )
            if next_item is None:
                break
            cursor = next_item


def _validate_compensation(
    history: tuple[ThreadTransitionProjection, ...],
    current: ThreadProjection,
    payload: ThreadChangedPayload,
) -> None:
    lineage = tuple(item for item in history if item.thread_id == current.thread_id)
    if not lineage or lineage[-1].transition_id != payload.compensates_transition_id:
        raise ValueError("thread compensation must target the latest transition")
    target = lineage[-1]
    if target.operation != "update" or target.values_before is None:
        raise ValueError("thread compensation can only restore the latest update")
    if payload.thread_after.values != target.values_before:
        raise ValueError("thread compensation must exactly restore the before image")
    before, restored = current.values, payload.thread_after.values
    if _PRIVACY_RANK[restored.privacy_class] < _PRIVACY_RANK[before.privacy_class]:
        raise ValueError("thread compensation cannot loosen privacy")
    if restored.importance_bp > before.importance_bp:
        raise ValueError("thread compensation cannot increase importance")
    if restored.due_window != before.due_window or restored.expires_at != before.expires_at:
        raise ValueError("thread compensation cannot expand temporal authority")


def _validate_closure_is_narrow(before, after) -> None:
    if (
        after.importance_bp != before.importance_bp
        or after.due_window != before.due_window
        or after.expires_at != before.expires_at
        or after.predecessor_thread_refs != before.predecessor_thread_refs
    ):
        raise ValueError("thread closure cannot alter scheduling or importance")


def _validate_privacy(thread: ThreadProjection) -> None:
    # This is disclosure authority only. Neither a producer-controlled purpose
    # label nor a broad requested class may weaken the source's minimum privacy.
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
        max(source_minimum.get(item.evidence_type, 4), purpose_minimum[item.claim_purpose])
        for item in thread.values.source_evidence_refs
    )
    if _PRIVACY_RANK[thread.values.privacy_class] < required:
        raise ValueError("thread evidence/privacy matrix rejects broad visibility")


def expire_thread(
    threads: tuple[ThreadProjection, ...],
    history: tuple[ThreadTransitionProjection, ...],
    payload: ThreadExpiredPayload,
    *,
    logical_time: datetime,
) -> tuple[tuple[ThreadProjection, ...], tuple[ThreadTransitionProjection, ...]]:
    current = next(
        (item for item in threads if item.thread_id == payload.thread_after.thread_id), None
    )
    if current is None or current != payload.thread_before:
        raise ValueError("thread expiry before image does not match current entity")
    if current.entity_revision != payload.expected_entity_revision:
        raise ValueError("thread expiry entity revision compare-and-swap failed")
    if current.values.status != "open":
        raise ValueError("terminal thread cannot expire")
    if current.values.expires_at is None or payload.expires_at != current.values.expires_at:
        raise ValueError("thread expiry requires its frozen expiry instant")
    if logical_time < payload.expires_at:
        raise ValueError("thread cannot expire before authoritative logical time")
    after = payload.thread_after
    expected_sources = (*current.values.source_evidence_refs, payload.clock_evidence_ref)
    expected_values = current.values.model_copy(
        update={"source_evidence_refs": expected_sources, "status": "expired"}
    )
    if (
        after.values != expected_values
        or after.semantic_fingerprint != current.semantic_fingerprint
        or after.opened_at != current.opened_at
        or after.updated_at != logical_time
        or after.origin.authority_mode != "mechanical_clock"
        or after.origin.policy_refs != current.origin.policy_refs
        or after.origin.change_id != payload.change_id
        or after.origin.transition_id != payload.transition_id
    ):
        raise ValueError("thread expiry after image is not the mechanical transition")
    if (
        payload.policy_version != THREAD_EXPIRY_POLICY_VERSION
        or payload.policy_digest != THREAD_EXPIRY_POLICY_DIGEST
    ):
        raise ValueError("thread expiry policy artifact is not installed")
    transition = ThreadTransitionProjection(
        transition_id=payload.transition_id,
        thread_id=after.thread_id,
        entity_revision=after.entity_revision,
        operation="expire",
        values_before=current.values,
        values_after=after.values,
        change_id=payload.change_id,
        policy_refs=("policy:thread-expiry-v1",),
        accepted_event_ref=after.origin.accepted_event_ref,
        accepted_at=logical_time,
    )
    if any(item.transition_id == transition.transition_id for item in history):
        raise ValueError("thread transition identity already exists")
    updated = tuple(after if item.thread_id == after.thread_id else item for item in threads)
    return updated, (*history, transition)

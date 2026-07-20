"""Operator-approved Media v2 automatic delivery.

Preview remains the default.  This module is the deliberately small exception
path: an operator freezes a passed inspection into a short-lived approval,
then an Action can send precisely that immutable artifact.  It owns no media
rendering and never turns a failed/unknown receipt into a share claim.
"""

from __future__ import annotations

from datetime import datetime

from .event_identity import domain_idempotency_key
from .ledger import LedgerPort
from .media_v2 import (
    MediaAutomaticDeliveryApproval,
    MediaAutomaticDeliveryApprovedPayload,
    MediaDeliveryShared,
    MediaDeliverySharedPayload,
    media_delivery_action_id,
    media_delivery_id,
    media_delivery_reservation_id,
)
from .schemas import (
    Action, BudgetReservation, ExecutionReceipt, LedgerProjection,
    MediaDeliveryApprovalBinding, ProjectionCursor, WorldEvent,
)


class MediaDeliveryError(ValueError):
    pass


def _event_id(role: str, stable: str) -> str:
    return f"event:media-delivery:{role}:{stable}"


class MediaDeliveryRuntime:
    """Ledger adapter for approval revisioning and one delivery Action."""

    def __init__(self, *, ledger: LedgerPort) -> None:
        self._ledger = ledger

    @staticmethod
    def _cursor(projection: LedgerProjection) -> ProjectionCursor:
        return ProjectionCursor(
            world_revision=projection.world_revision,
            deliberation_revision=projection.deliberation_revision,
            ledger_sequence=projection.ledger_sequence,
        )

    def approve(
        self, *, approval: MediaAutomaticDeliveryApproval, trace_id: str,
        correlation_id: str, causation_id: str,
    ) -> MediaAutomaticDeliveryApproval:
        projection = self._ledger.project()
        existing = next(
            (
                item for item in projection.media_delivery_approvals
                if item.approval_id == approval.approval_id
                and item.entity_revision == approval.entity_revision
            ),
            None,
        )
        if existing is not None:
            if existing != approval:
                raise MediaDeliveryError("delivery approval revision is already bound to other bytes")
            return existing
        payload = MediaAutomaticDeliveryApprovedPayload(approval=approval).model_dump(mode="json")
        event = self._event(
            event_type="MediaAutomaticDeliveryApproved", stable=f"{approval.approval_id}:{approval.entity_revision}",
            payload=payload, actor=approval.operator_ref, logical_time=approval.approved_at,
            trace_id=trace_id, correlation_id=correlation_id, causation_id=causation_id,
        )
        self._ledger.commit_at_cursor(
            (event,), expected_cursor=self._cursor(projection),
            commit_id="commit:media-delivery-approval:" + approval.approval_id + ":" + str(approval.entity_revision),
        )
        return approval

    def authorize_delivery(
        self, *, approval_id: str, approval_revision: int, actor: str, target: str,
        account_id: str, amount_limit: int, logical_time: datetime, trace_id: str,
        correlation_id: str,
    ) -> Action:
        projection = self._ledger.project()
        approval = next(
            (
                item for item in projection.media_delivery_approvals
                if item.approval_id == approval_id and item.entity_revision == approval_revision
            ),
            None,
        )
        latest = max(
            (item.entity_revision for item in projection.media_delivery_approvals if item.approval_id == approval_id),
            default=0,
        )
        if approval is None or latest != approval_revision or logical_time >= approval.expires_at:
            raise MediaDeliveryError("media delivery approval is missing, superseded, or expired")
        expected_target = approval.delivery_target_ref or approval.recipient_ref
        if target != expected_target:
            raise MediaDeliveryError(
                "media delivery target is not the operator-approved recipient address"
            )
        inspection = next((item for item in projection.media_inspections if item.inspection_id == approval.inspection_id), None)
        artifact = next((item for item in projection.media_artifacts if item.artifact_id == approval.artifact_id), None)
        if inspection is None or artifact is None or not inspection.passed or artifact.artifact_hash != approval.artifact_hash:
            raise MediaDeliveryError("media delivery approval no longer binds a passed immutable artifact")
        action_id = media_delivery_action_id(
            world_id=self._ledger.world_id, approval_id=approval_id, approval_revision=approval_revision,
        )
        existing = next((item for item in projection.actions if item.action_id == action_id), None)
        if existing is not None:
            return existing
        reservation = BudgetReservation(
            reservation_id=media_delivery_reservation_id(
                world_id=self._ledger.world_id, approval_id=approval_id, approval_revision=approval_revision,
            ),
            account_id=account_id, action_id=action_id, category="image", amount_limit=amount_limit,
        )
        action = Action(
            schema_version="world-v2.1", action_id=action_id, world_id=self._ledger.world_id,
            logical_time=logical_time, created_at=logical_time, trace_id=trace_id,
            causation_id=_event_id("MediaAutomaticDeliveryApproved", f"{approval_id}:{approval_revision}"),
            correlation_id=correlation_id, kind="media_delivery", layer="external_action",
            intent_ref=approval.inspection_id, actor=actor, target=target,
            payload_ref=artifact.artifact_ref, payload_hash=artifact.artifact_hash,
            media_delivery_approval=MediaDeliveryApprovalBinding(
                approval_id=approval_id, approval_revision=approval_revision,
            ),
            idempotency_key=f"media-delivery:{approval_id}:{approval_revision}",
            budget_reservation_id=reservation.reservation_id, state="authorized", recovery_policy="effect_once",
        )
        events = (
            self._event(event_type="BudgetReserved", stable=reservation.reservation_id,
                        payload={"reservation": reservation.model_dump(mode="json")}, actor=actor,
                        logical_time=logical_time, trace_id=trace_id, correlation_id=correlation_id,
                        causation_id=action.causation_id),
            self._event(event_type="ActionAuthorized", stable=action_id,
                        payload={"action": action.model_dump(mode="json")}, actor=actor,
                        logical_time=logical_time, trace_id=trace_id, correlation_id=correlation_id,
                        causation_id=_event_id("BudgetReserved", reservation.reservation_id)),
        )
        self._ledger.commit_at_cursor(
            events, expected_cursor=self._cursor(projection),
            commit_id="commit:media-delivery-authorize:" + action_id,
        )
        return action

    def _event(
        self, *, event_type: str, stable: str, payload: dict[str, object], actor: str,
        logical_time: datetime, trace_id: str, correlation_id: str, causation_id: str,
    ) -> WorldEvent:
        key = domain_idempotency_key(event_type=event_type, world_id=self._ledger.world_id, payload=payload)
        if key is None:
            # Generic lifecycle events (``BudgetReserved``/``ActionAuthorized``)
            # have no public domain-id function; bind them to their exact
            # payload bytes exactly like the media planning batch does.
            import hashlib as _hashlib
            import json as _json

            key = "world-v2:media-delivery:" + _hashlib.sha256(
                _json.dumps(
                    {"event_type": event_type, "world_id": self._ledger.world_id, "payload": payload},
                    ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
        return WorldEvent.from_payload(
            schema_version="world-v2.1", event_id=_event_id(event_type, stable), event_type=event_type,
            world_id=self._ledger.world_id, logical_time=logical_time, created_at=logical_time,
            actor=actor, source="world-v2:media-delivery", trace_id=trace_id,
            causation_id=causation_id, correlation_id=correlation_id, idempotency_key=key, payload=payload,
        )


class MediaDeliveryReceiptLifecycle:
    """Pure receipt-to-share materializer used by the generic settlement UoW."""

    def events_for_terminal_receipt(
        self, *, projection: LedgerProjection, action: Action, receipt: ExecutionReceipt,
    ) -> tuple[tuple[str, str, dict[str, object]], ...]:
        if action.kind != "media_delivery" or receipt.observed_state != "delivered":
            return ()
        binding = action.media_delivery_approval
        if binding is None:
            raise MediaDeliveryError("media delivery receipt has no approval binding")
        approval = next(
            (
                item for item in projection.media_delivery_approvals
                if item.approval_id == binding.approval_id and item.entity_revision == binding.approval_revision
            ),
            None,
        )
        if approval is None:
            raise MediaDeliveryError("media delivery receipt approval is unavailable")
        delivery = MediaDeliveryShared(
            delivery_id=media_delivery_id(action_id=action.action_id, receipt_id=receipt.receipt_id),
            approval_id=approval.approval_id, approval_revision=approval.entity_revision,
            plan_id=approval.plan_id, inspection_id=approval.inspection_id,
            artifact_id=approval.artifact_id, artifact_hash=approval.artifact_hash,
            recipient_ref=approval.recipient_ref, action_id=action.action_id, receipt_id=receipt.receipt_id,
        )
        if any(item.delivery_id == delivery.delivery_id for item in projection.media_deliveries):
            return ()
        return (("MediaDeliveryShared", delivery.delivery_id,
                 MediaDeliverySharedPayload(delivery=delivery).model_dump(mode="json")),)


def require_current_media_delivery_approval(*, action: Action, projection: LedgerProjection, logical_time: datetime) -> MediaAutomaticDeliveryApproval:
    """Final ActionPump gate: revisions and expiry invalidate pre-dispatch work."""

    binding = action.media_delivery_approval
    if action.kind != "media_delivery" or binding is None:
        raise MediaDeliveryError("delivery authority verifier received a non-media-delivery Action")
    approvals = tuple(item for item in projection.media_delivery_approvals if item.approval_id == binding.approval_id)
    exact = next((item for item in approvals if item.entity_revision == binding.approval_revision), None)
    if exact is None or max((item.entity_revision for item in approvals), default=0) != binding.approval_revision:
        raise MediaDeliveryError("media delivery operator approval revision is stale")
    if logical_time >= exact.expires_at:
        raise MediaDeliveryError("media delivery operator approval has expired")
    return exact


__all__ = [
    "MediaDeliveryError", "MediaDeliveryReceiptLifecycle", "MediaDeliveryRuntime",
    "require_current_media_delivery_approval",
]

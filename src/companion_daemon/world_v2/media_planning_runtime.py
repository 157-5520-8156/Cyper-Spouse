"""Source-bound planning orchestration for the first Media v2 lane.

This runtime never constructs prompts.  It writes immutable opportunity bytes
to the sidecar before the ledger command, creates one deterministic planning
Action, and records exactly one terminal planner result.  A retry joins the
same Action/result; it never creates a replacement opportunity or re-plans.
"""

from __future__ import annotations

from datetime import datetime
import json

from .event_identity import domain_idempotency_key
from .ledger import LedgerPort
from .media_v2 import (
    FrozenMediaEvidenceSnapshot, ImmutableMediaPayloadStore, MediaNotRenderableRecordedPayload,
    MediaOpportunity, MediaOpportunityFrozenPayload, MediaPlanRecordedPayload,
    MediaPlanner, MediaPlanningResult, PhotoCandidate, PhotoCandidateOpenedPayload,
    StoredMediaPayload, continuation_trigger_id, media_digest, media_payload_hash, planning_request_id,
)
from .schemas import (
    Action, BudgetReservation, ExecutionReceipt, ExternalObservation,
    ProjectionCursor, ProviderMediaGrantBinding, TriggerProcess, WorldEvent,
)


class MediaPlanningError(ValueError):
    pass


def _event_id(*, role: str, stable: str) -> str:
    return "event:media-v2:" + role + ":" + media_digest({"role": role, "stable": stable})


def _idempotency(*, event_type: str, world_id: str, payload: dict[str, object]) -> str:
    value = domain_idempotency_key(event_type=event_type, world_id=world_id, payload=payload)
    if value is None:
        raise MediaPlanningError(f"missing installed media event identity for {event_type}")
    return value


class MediaPlanningRuntime:
    def __init__(self, *, ledger: LedgerPort, sidecar: ImmutableMediaPayloadStore) -> None:
        self._ledger, self._sidecar = ledger, sidecar

    @staticmethod
    def _cursor(projection) -> ProjectionCursor:
        return ProjectionCursor(
            world_revision=projection.world_revision,
            deliberation_revision=projection.deliberation_revision,
            ledger_sequence=projection.ledger_sequence,
        )

    def freeze_and_authorize(
        self,
        *, candidate: PhotoCandidate, opportunity: MediaOpportunity, snapshot_body: str,
        actor: str, grant: ProviderMediaGrantBinding, account_id: str, amount_limit: int,
        logical_time: datetime, trace_id: str, correlation_id: str,
    ):
        if candidate.candidate_id != opportunity.candidate_id:
            raise MediaPlanningError("opportunity does not bind candidate")
        if opportunity.delivery_mode != "preview":
            raise MediaPlanningError("Media v2 planning is preview-only until operator approval is implemented")
        if opportunity.event_snapshot_hash != media_payload_hash(snapshot_body):
            raise MediaPlanningError("frozen snapshot hash does not bind supplied sidecar bytes")
        try:
            snapshot = FrozenMediaEvidenceSnapshot.model_validate(json.loads(snapshot_body))
        except Exception as exc:
            raise MediaPlanningError("frozen snapshot bytes do not satisfy the evidence contract") from exc
        projection = self._ledger.project()
        committed_hashes = {item.event_id: item.payload_hash for item in projection.committed_world_event_refs}
        if (
            tuple(item.event_ref for item in snapshot.source_events) != opportunity.source_event_refs
            or tuple(item.event_ref for item in snapshot.source_events) != candidate.source_event_refs
            or any(committed_hashes.get(item.event_ref) != item.payload_hash for item in snapshot.source_events)
        ):
            raise MediaPlanningError("frozen snapshot is not exactly bound to committed source evidence")
        # The v5 image machine reads these richer values solely from the
        # hashed sidecar.  Refuse a partial/mismatched snapshot rather than
        # letting a later worker fill it from mutable projection state.
        complete = snapshot.complete_candidate
        if complete is not None and complete.get("candidate_id") not in {None, candidate.candidate_id}:
            raise MediaPlanningError("complete candidate snapshot is not bound to candidate")
        recipient = snapshot.recipient_context
        if opportunity.recipient_ref is not None:
            if recipient is None or recipient.get("recipient_ref") != opportunity.recipient_ref:
                raise MediaPlanningError("private/media recipient must be frozen in snapshot")
        elif recipient is not None and recipient.get("recipient_ref") is not None:
            raise MediaPlanningError("snapshot recipient must match opportunity recipient")
        for label, value in (("location", snapshot.location), ("visible_physical_state", snapshot.visible_physical_state)):
            if value is not None and not isinstance(value, dict):
                raise MediaPlanningError(f"frozen snapshot {label} must be an object")
        self._sidecar.put_if_absent(StoredMediaPayload(
            payload_ref=opportunity.event_snapshot_ref, payload_hash=opportunity.event_snapshot_hash,
            content_type="application/vnd.world-v2.media-opportunity+json", body=snapshot_body,
        ))
        cursor = self._cursor(projection)
        request_id = planning_request_id(opportunity.opportunity_id)
        action_id = "action:media-planning:" + media_digest({"world_id": self._ledger.world_id, "request_id": request_id})
        reservation_id = "reservation:media-planning:" + media_digest({"world_id": self._ledger.world_id, "request_id": request_id})
        reservation = BudgetReservation(reservation_id=reservation_id, account_id=account_id, action_id=action_id, category="image", amount_limit=amount_limit)
        action = Action(
            schema_version="world-v2.1", action_id=action_id, world_id=self._ledger.world_id,
            logical_time=logical_time, created_at=logical_time, trace_id=trace_id,
            causation_id=_event_id(role="opportunity", stable=opportunity.opportunity_id), correlation_id=correlation_id,
            kind="media_planning", layer="media_action", intent_ref=opportunity.opportunity_id,
            actor=actor, target="provider:media-planner", payload_ref=opportunity.event_snapshot_ref,
            payload_hash=opportunity.event_snapshot_hash, provider_media_grant=grant,
            idempotency_key=request_id, budget_reservation_id=reservation_id, state="authorized", recovery_policy="effect_once",
        )
        existing_opportunity = next(
            (item for item in projection.media_opportunities if item.opportunity_id == opportunity.opportunity_id), None
        )
        if existing_opportunity is not None:
            if existing_opportunity != opportunity:
                raise MediaPlanningError("opportunity id is already bound to different frozen evidence")
            existing_action = next((item for item in projection.actions if item.action_id == action_id), None)
            if existing_action != action:
                raise MediaPlanningError("existing opportunity has no matching deterministic planning Action")
            located = self._ledger.lookup_event_commit(_event_id(role="ActionAuthorized", stable=action_id))
            if located is None:
                raise MediaPlanningError("existing planning Action event is unavailable")
            return located[1]
        payloads: tuple[tuple[str, dict[str, object], str], ...] = (
            ("PhotoCandidateOpened", PhotoCandidateOpenedPayload(candidate=candidate).model_dump(mode="json"), candidate.candidate_id),
            ("MediaOpportunityFrozen", MediaOpportunityFrozenPayload(opportunity=opportunity).model_dump(mode="json"), opportunity.opportunity_id),
            ("BudgetReserved", {"reservation": reservation.model_dump(mode="json")}, reservation_id),
            ("ActionAuthorized", {"action": action.model_dump(mode="json")}, action_id),
        )
        events: list[WorldEvent] = []
        for event_type, payload, stable in payloads:
            events.append(WorldEvent.from_payload(
                schema_version="world-v2.1", event_id=_event_id(role=event_type, stable=stable), event_type=event_type,
                world_id=self._ledger.world_id, logical_time=logical_time, created_at=logical_time,
                actor=actor, source="world-v2:media-planning", trace_id=trace_id,
                causation_id=events[-1].event_id if events else action.causation_id, correlation_id=correlation_id,
                idempotency_key=_idempotency(event_type=event_type, world_id=self._ledger.world_id, payload=payload), payload=payload,
            ))
        return self._ledger.commit_at_cursor(events, expected_cursor=cursor, commit_id="commit:media-freeze:" + media_digest([event.event_id for event in events]))

    async def execute_planning_once(self, *, action_id: str, planner: MediaPlanner):
        """Call the legacy planner only for a dispatched immutable Action.

        Dispatch/recovery ownership remains external; this method is deliberately
        easy to call after a crash because it asks the old planner for the same
        request id before making a second RPC.
        """
        projection = self._ledger.project()
        action = next((item for item in projection.actions if item.action_id == action_id), None)
        if action is None or action.kind != "media_planning":
            raise MediaPlanningError("planning Action is unavailable")
        opportunity = next((item for item in projection.media_opportunities if item.opportunity_id == action.intent_ref), None)
        if opportunity is None:
            raise MediaPlanningError("planning Action lacks frozen opportunity")
        prior = next((item for item in projection.media_plans if item.opportunity_id == opportunity.opportunity_id), None)
        if prior is not None:
            return MediaPlanningResult(plan=prior)
        if opportunity.opportunity_id in projection.media_unrenderable_opportunity_ids:
            located = self._ledger.lookup_event_commit(_event_id(role="MediaNotRenderableRecorded", stable=opportunity.opportunity_id))
            if located is None:
                raise MediaPlanningError("unrenderable media result event is unavailable")
            return MediaPlanningResult(not_renderable=MediaNotRenderableRecordedPayload.model_validate_json(located[0].payload_json).result)
        if action.state != "dispatch_started":
            raise MediaPlanningError("planning Action is not dispatch-started")
        result = await planner.lookup(planning_request_id=action.idempotency_key)
        if result is None:
            result = await planner.plan(opportunity=opportunity, planning_request_id=action.idempotency_key)
        return self.record_terminal_result(action_id=action_id, result=result, logical_time=projection.logical_time or action.logical_time)

    def record_terminal_result(self, *, action_id: str, result: MediaPlanningResult, logical_time: datetime):
        projection = self._ledger.project()
        action = next((item for item in projection.actions if item.action_id == action_id), None)
        if action is None or action.kind != "media_planning":
            raise MediaPlanningError("planning Action is unavailable")
        opportunity = next((item for item in projection.media_opportunities if item.opportunity_id == action.intent_ref), None)
        if opportunity is None:
            raise MediaPlanningError("planning Action lacks frozen opportunity")
        receipt_id = "receipt:media-planning:" + media_digest({"action": action_id, "request": action.idempotency_key})
        if any(item.receipt_id == receipt_id for item in projection.execution_receipts):
            return projection
        if action.state != "dispatch_started":
            raise MediaPlanningError("planning Action is not dispatch-started")
        if result.plan is not None:
            plan = result.plan
            if (plan.opportunity_id != opportunity.opportunity_id or plan.planning_request_id != action.idempotency_key
                or plan.event_snapshot_hash != opportunity.event_snapshot_hash or plan.family != opportunity.family
                or plan.media_machine_version != opportunity.media_machine_version
                or plan.inspection_contract_version != opportunity.inspection_contract_version
                or plan.media_lane != opportunity.media_lane):
                raise MediaPlanningError("planner returned a plan outside frozen opportunity")
            if opportunity.media_lane == "explicit_reserved":
                raise MediaPlanningError("explicit-reserved media must be recorded as NotRenderable")
            if result.plan_payload is not None:
                self._sidecar.put_if_absent(result.plan_payload)
            stored_plan = self._sidecar.read_exact(payload_ref=plan.plan_payload_ref)
            if (
                stored_plan is None
                or stored_plan.payload_hash != plan.plan_payload_hash
                or stored_plan.content_type != "application/vnd.world-v2.media-plan+json"
            ):
                raise MediaPlanningError("planner result has no exact immutable plan sidecar")
            domain_payload: dict[str, object] = MediaPlanRecordedPayload(action_id=action_id, receipt_id=receipt_id, plan=plan).model_dump(mode="json")
            domain_type, stable = "MediaPlanRecorded", plan.plan_id
        else:
            assert result.not_renderable is not None
            not_renderable = result.not_renderable
            if (not_renderable.opportunity_id != opportunity.opportunity_id or not_renderable.planning_request_id != action.idempotency_key
                or not_renderable.event_snapshot_hash != opportunity.event_snapshot_hash):
                raise MediaPlanningError("not-renderable result is outside frozen opportunity")
            domain_payload = MediaNotRenderableRecordedPayload(action_id=action_id, receipt_id=receipt_id, result=not_renderable).model_dump(mode="json")
            domain_type, stable = "MediaNotRenderableRecorded", opportunity.opportunity_id
        source_event_id = "media-planning:" + action.idempotency_key
        result_id = "result:media-planning:" + media_digest({"action": action_id, "receipt": receipt_id})
        external = ExternalObservation(schema_version="world-v2.1", result_id=result_id, world_id=self._ledger.world_id,
            logical_time=logical_time, created_at=logical_time, trace_id=action.trace_id, causation_id=action_id,
            correlation_id=action.correlation_id, kind="media_result", source="provider:media-planner", source_event_id=source_event_id,
            action_id=action_id, idempotency_key=action.idempotency_key, status="delivered", provider_ref=action.idempotency_key,
            artifact_refs=(), cost_actual=0, error_class=None, raw_payload_hash="sha256:" + media_digest(domain_payload))
        receipt = ExecutionReceipt(receipt_id=receipt_id, result_id=result_id, action_id=action_id, provider="provider:media-planner",
            provider_ref=action.idempotency_key, source_event_id=source_event_id, receipt_kind="terminal", observed_state="delivered",
            is_terminal=True, cost_actual=0, received_at=logical_time, raw_payload_hash=external.raw_payload_hash)
        reservation = next((item for item in projection.budget_reservations if item.reservation_id == action.budget_reservation_id), None)
        if reservation is None:
            raise MediaPlanningError("planning Action has no active reservation")
        settlement = {"settlement": {"schema_version": "world-v2.1", "settlement_id": "budget-settlement:" + receipt_id,
            "reservation_id": reservation.reservation_id, "action_id": action_id, "result_id": result_id, "state": "settled",
            "settlement_kind": "terminal", "previous_cost": reservation.settled_cost, "cost_actual": 0, "cost_delta": -reservation.settled_cost}}
        payloads: list[tuple[str, dict[str, object], str]] = [
            ("ActionDelivered", external.model_dump(mode="json"), action_id),
            ("ExecutionReceiptRecorded", {"receipt": receipt.model_dump(mode="json")}, receipt_id),
            ("BudgetSettled", settlement, reservation.reservation_id),
            (domain_type, domain_payload, stable),
        ]
        if result.plan is not None:
            trigger_id = continuation_trigger_id(result.plan)
            process = TriggerProcess(trigger_id=trigger_id, trigger_ref=trigger_id, process_kind="media_continuation",
                source_evidence_ref=_event_id(role=domain_type, stable=stable), state="open")
            payloads.append(("TriggerProcessOpened", {"process": process.model_dump(mode="json")}, trigger_id))
        events: list[WorldEvent] = []
        for event_type, payload, stable_id in payloads:
            events.append(WorldEvent.from_payload(schema_version="world-v2.1", event_id=_event_id(role=event_type, stable=stable_id), event_type=event_type,
                world_id=self._ledger.world_id, logical_time=logical_time, created_at=logical_time, actor="system:media-planning",
                source="world-v2:media-planning", trace_id=action.trace_id, causation_id=events[-1].event_id if events else action_id,
                correlation_id=action.correlation_id, idempotency_key=_idempotency(event_type=event_type, world_id=self._ledger.world_id, payload=payload), payload=payload))
        return self._ledger.commit_at_cursor(events, expected_cursor=self._cursor(projection), commit_id="commit:media-plan:" + media_digest([event.event_id for event in events]))


__all__ = ["MediaPlanningError", "MediaPlanningRuntime"]

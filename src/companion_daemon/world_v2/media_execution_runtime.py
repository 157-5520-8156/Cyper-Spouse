"""Render/inspection continuation for Media v2.

This is intentionally a narrow ledger adapter.  The image machine receives
only a verified frozen plan sidecar and returns opaque artifact/inspection
bytes; it never obtains a World projection or a ledger writer.  The runtime
materializes those bytes *after* the durable provider Action receipt, and
only emits a ``MediaPreviewGenerated`` record.  There is no delivery event in
this lane.
"""

from __future__ import annotations

from datetime import datetime, timedelta
import base64
import json
from typing import Protocol

from .event_identity import domain_idempotency_key
from .ledger import LedgerPort
from .media_provider_results import (
    MediaProviderArtifactResult, MediaProviderResultTransport,
    media_provider_result_hash,
)
from .media_v2 import (
    ImmutableMediaPayloadStore, MediaArtifact, MediaInspectionRecord,
    MediaInspectionRecordedPayload, MediaPreview, MediaPreviewFailedPayload,
    MediaPreviewGeneratedPayload, MediaRenderArtifactRecordedPayload,
    MediaPlan, MediaRepairAuthorization, MediaRepairAuthorizedPayload,
    StoredMediaPayload, media_digest, media_repair_action_id, media_repair_attempt_id,
    media_repair_reservation_id, media_repair_trigger_id,
)
from .schemas import Action, BudgetReservation, ClaimLease, ExecutionReceipt, ProjectionCursor, ProviderMediaGrantBinding, TriggerProcess, WorldEvent


class MediaExecutionError(ValueError):
    pass


class MediaExecutionAdapter(Protocol):
    """Public seam implemented by an ``event_media`` bridge.

    The bridge is deliberately given exact immutable bytes rather than a
    ``LedgerProjection``.  Artifact and inspection payloads are returned to
    the runtime, which hashes/stores them before it emits their descriptors.
    """

    async def render(self, *, plan_payload: StoredMediaPayload, request_id: str) -> StoredMediaPayload: ...

    async def inspect(
        self, *, plan_payload: StoredMediaPayload, artifact_payload: StoredMediaPayload,
        request_id: str,
    ) -> tuple[bool, str, str | None, StoredMediaPayload]: ...

    async def repair_once(
        self, *, plan_payload: StoredMediaPayload, failed_artifact_payload: StoredMediaPayload,
        inspection_payload: StoredMediaPayload, request_id: str,
    ) -> StoredMediaPayload: ...


class EventMediaExecutionAdapter:
    """Small bridge to the legacy image machine's public ``MediaRenderer``.

    It neither imports a World v2 projection nor has a ledger reference.  The
    planned JSON is parsed through ``event_media.MediaPlan.from_payload`` and
    the renderer is invoked exactly with that frozen plan.  Its inspection is
    cached only between the paired render/inspection provider calls; durable
    recovery remains the responsibility of the provider transport/sidecar.
    """

    def __init__(self, *, renderer) -> None:
        self._renderer = renderer
        self._inspection_by_request: dict[str, object] = {}

    async def render(self, *, plan_payload: StoredMediaPayload, request_id: str) -> StoredMediaPayload:
        from companion_daemon.event_media import MediaPlan as LegacyMediaPlan, MediaRenderFailure

        if plan_payload.content_type != "application/vnd.world-v2.media-plan+json":
            raise MediaExecutionError("legacy renderer requires a frozen media-plan sidecar")
        try:
            plan = LegacyMediaPlan.from_payload(json.loads(plan_payload.body))
        except Exception as exc:
            raise MediaExecutionError("frozen MediaPlan bytes are not accepted by event_media") from exc
        result = await self._renderer.render(plan)
        if isinstance(result, MediaRenderFailure):
            raise MediaExecutionError("media_render_failed:" + result.reason)
        image_bytes = result.path.read_bytes()
        body = json.dumps({
            "encoding": "base64", "artifact_hash": result.artifact_hash,
            "bytes": base64.b64encode(image_bytes).decode("ascii"),
        }, sort_keys=True, separators=(",", ":"))
        record = StoredMediaPayload(
            payload_ref="sidecar:media-artifact:" + media_digest({"request": request_id, "hash": result.artifact_hash}),
            payload_hash="sha256:" + media_digest(body),
            content_type="application/vnd.world-v2.media-artifact+json", body=body,
        )
        self._inspection_by_request[request_id] = result.inspection
        return record

    async def inspect(
        self, *, plan_payload: StoredMediaPayload, artifact_payload: StoredMediaPayload,
        request_id: str,
    ) -> tuple[bool, str, str | None, StoredMediaPayload]:
        inspection = self._inspection_by_request.pop(request_id, None)
        if inspection is None:
            raise MediaExecutionError("inspection result unavailable for recovery; query provider receipt")
        payload = inspection.to_payload()
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        record = StoredMediaPayload(
            payload_ref="sidecar:media-inspection:" + media_digest({"request": request_id, "artifact": artifact_payload.payload_hash}),
            payload_hash="sha256:" + media_digest(body),
            content_type="application/vnd.world-v2.media-inspection+json", body=body,
        )
        return bool(inspection.passed), str(inspection.reason), inspection.observed_summary, record

    async def repair_once(
        self, *, plan_payload: StoredMediaPayload, failed_artifact_payload: StoredMediaPayload,
        inspection_payload: StoredMediaPayload, request_id: str,
    ) -> StoredMediaPayload:
        repair = getattr(self._renderer, "repair_once", None)
        if repair is None:
            raise MediaExecutionError("legacy renderer has no bounded repair_once seam")
        # The legacy seam receives only independently verified immutable
        # payloads.  It remains free to use its private visual contract but
        # cannot substitute world evidence or a new plan.
        result = await repair(plan_payload, failed_artifact_payload, inspection_payload, request_id=request_id)
        if not isinstance(result, StoredMediaPayload):
            raise MediaExecutionError("repair_once must return an immutable media artifact payload")
        return result


def _event_id(role: str, stable: str) -> str:
    return "event:media-v2:" + role + ":" + media_digest({"role": role, "stable": stable})


def _idempotency(event_type: str, world_id: str, payload: dict[str, object]) -> str:
    value = domain_idempotency_key(event_type=event_type, world_id=world_id, payload=payload)
    if value is None:
        raise MediaExecutionError(f"missing event identity for {event_type}")
    return value


class MediaExecutionRuntime:
    """Issue render/inspection Actions and materialize their terminal outputs.

    ActionPump owns provider dispatch and generic receipts.  A host calls the
    idempotent ``record_*`` methods after the matching Action reaches
    ``delivered``.  This makes crash recovery a re-read of immutable action
    ids and sidecar refs, never a second semantic planning decision.
    """

    def __init__(self, *, ledger: LedgerPort, sidecar: ImmutableMediaPayloadStore) -> None:
        self._ledger, self._sidecar = ledger, sidecar

    @staticmethod
    def _cursor(projection) -> ProjectionCursor:
        return ProjectionCursor(world_revision=projection.world_revision, deliberation_revision=projection.deliberation_revision, ledger_sequence=projection.ledger_sequence)

    def authorize_render(
        self, *, plan_id: str, actor: str, grant: ProviderMediaGrantBinding, account_id: str,
        amount_limit: int, logical_time: datetime, trace_id: str, correlation_id: str,
    ):
        projection = self._ledger.project()
        plan = next((item for item in projection.media_plans if item.plan_id == plan_id), None)
        if plan is None:
            raise MediaExecutionError("render requires a frozen MediaPlan")
        if plan.opportunity_id in projection.media_unrenderable_opportunity_ids:
            raise MediaExecutionError("unrenderable opportunity cannot render")
        existing = next((item for item in projection.actions if item.intent_ref == plan_id and item.kind == "media_render"), None)
        if existing is not None:
            return existing
        continuation = "media-continuation:" + media_digest({"plan_id": plan.plan_id, "step": "plan_to_render"})
        if not any(item.trigger_id == continuation and item.state in {"open", "claimed"} for item in projection.trigger_processes):
            raise MediaExecutionError("render requires the exact open continuation of its frozen plan")
        payload = self._require_plan_payload(plan)
        action_id = "action:media-render:" + media_digest({"world": self._ledger.world_id, "plan": plan_id})
        reservation = BudgetReservation(
            reservation_id="reservation:media-render:" + media_digest({"world": self._ledger.world_id, "plan": plan_id}),
            account_id=account_id, action_id=action_id, category="image", amount_limit=amount_limit,
        )
        action = Action(schema_version="world-v2.1", action_id=action_id, world_id=self._ledger.world_id,
            logical_time=logical_time, created_at=logical_time, trace_id=trace_id,
            causation_id=_event_id("MediaPlanRecorded", plan_id), correlation_id=correlation_id,
            kind="media_render", layer="media_action", intent_ref=plan_id, actor=actor,
            target="provider:media-renderer", payload_ref=payload.payload_ref, payload_hash=payload.payload_hash,
            provider_media_grant=grant, idempotency_key="media-render:" + plan_id,
            budget_reservation_id=reservation.reservation_id, state="authorized", recovery_policy="effect_once")
        items = (("BudgetReserved", {"reservation": reservation.model_dump(mode="json")}, reservation.reservation_id),
                 ("ActionAuthorized", {"action": action.model_dump(mode="json")}, action_id))
        events = self._events(items=items, actor=actor, logical_time=logical_time, trace_id=trace_id, correlation_id=correlation_id, causation_id=action.causation_id)
        self._ledger.commit_at_cursor(events, expected_cursor=self._cursor(projection), commit_id="commit:media-render-authorize:" + media_digest([event.event_id for event in events]))
        return action

    def record_rendered_artifact(self, *, action_id: str, receipt_id: str, artifact_payload: StoredMediaPayload, logical_time: datetime) -> MediaArtifact:
        projection = self._ledger.project()
        action, plan = self._action_and_plan(projection, action_id, {"media_render", "media_repair"})
        existing = next((item for item in projection.media_artifacts if item.render_action_id == action_id), None)
        if existing is not None:
            return existing
        if action.state != "delivered" or not any(item.receipt_id == receipt_id and item.action_id == action_id for item in projection.execution_receipts):
            raise MediaExecutionError("render artifact requires delivered Action and exact receipt")
        self._sidecar.put_if_absent(artifact_payload)
        attempt = 2 if action.kind == "media_repair" else 1
        artifact = MediaArtifact(artifact_id="artifact:media:" + media_digest({"action": action_id, "ref": artifact_payload.payload_ref}),
            plan_id=plan.plan_id, render_action_id=action_id, artifact_ref=artifact_payload.payload_ref,
            artifact_hash=artifact_payload.payload_hash, media_type=artifact_payload.content_type, attempts=attempt)
        payload = MediaRenderArtifactRecordedPayload(action_id=action_id, receipt_id=receipt_id, artifact=artifact).model_dump(mode="json")
        self._commit_one("MediaRenderArtifactRecorded", payload, artifact.artifact_id, logical_time, action.trace_id, action.correlation_id, action_id)
        return artifact

    def record_render_failure(self, *, action_id: str, reason_code: str, logical_time: datetime) -> None:
        """Close the preview lane after a terminal render or repair failure.

        This is intentionally not an attempt to select a new opportunity or
        retry with changed semantics.  Transient retry is owned by the frozen
        provider request; once ActionPump records a terminal non-delivery the
        v2 opportunity is visibly failed and effect-once recovery joins it.
        """
        projection = self._ledger.project()
        action, plan = self._action_and_plan(projection, action_id, {"media_render", "media_repair"})
        if plan.plan_id in projection.media_failed_plan_ids:
            return
        if action.state not in {"failed", "unknown", "expired", "cancelled"}:
            raise MediaExecutionError("render failure requires a terminal non-delivered Action")
        payload = MediaPreviewFailedPayload(plan_id=plan.plan_id, reason_code=reason_code).model_dump(mode="json")
        self._commit_one("MediaPreviewFailed", payload, plan.plan_id, logical_time, action.trace_id, action.correlation_id, action_id)

    def authorize_inspection(self, *, artifact_id: str, actor: str, grant: ProviderMediaGrantBinding, account_id: str, amount_limit: int, logical_time: datetime, trace_id: str, correlation_id: str):
        projection = self._ledger.project()
        artifact = next((item for item in projection.media_artifacts if item.artifact_id == artifact_id), None)
        if artifact is None:
            raise MediaExecutionError("inspection requires immutable artifact")
        existing = next((item for item in projection.actions if item.intent_ref == artifact_id and item.kind == "media_inspection"), None)
        if existing is not None:
            return existing
        self._require_payload(artifact.artifact_ref, artifact.artifact_hash)
        action_id = "action:media-inspection:" + media_digest({"world": self._ledger.world_id, "artifact": artifact_id})
        reservation = BudgetReservation(reservation_id="reservation:media-inspection:" + media_digest({"world": self._ledger.world_id, "artifact": artifact_id}), account_id=account_id, action_id=action_id, category="image", amount_limit=amount_limit)
        action = Action(schema_version="world-v2.1", action_id=action_id, world_id=self._ledger.world_id, logical_time=logical_time, created_at=logical_time, trace_id=trace_id, causation_id=_event_id("MediaRenderArtifactRecorded", artifact_id), correlation_id=correlation_id, kind="media_inspection", layer="media_action", intent_ref=artifact_id, actor=actor, target="provider:media-inspector", payload_ref=artifact.artifact_ref, payload_hash=artifact.artifact_hash, provider_media_grant=grant, idempotency_key="media-inspection:" + artifact_id, budget_reservation_id=reservation.reservation_id, state="authorized", recovery_policy="effect_once")
        events = self._events(items=(("BudgetReserved", {"reservation": reservation.model_dump(mode="json")}, reservation.reservation_id), ("ActionAuthorized", {"action": action.model_dump(mode="json")}, action_id)), actor=actor, logical_time=logical_time, trace_id=trace_id, correlation_id=correlation_id, causation_id=action.causation_id)
        self._ledger.commit_at_cursor(events, expected_cursor=self._cursor(projection), commit_id="commit:media-inspection-authorize:" + media_digest([event.event_id for event in events]))
        return action

    def record_inspection(self, *, action_id: str, receipt_id: str, passed: bool, reason_code: str, observed_summary: str | None, inspection_payload: StoredMediaPayload, logical_time: datetime, repairable: bool = False, repair_scope: tuple[str, ...] = ()) -> MediaInspectionRecord:
        projection = self._ledger.project()
        action = next((item for item in projection.actions if item.action_id == action_id), None)
        artifact = next((item for item in projection.media_artifacts if item.artifact_id == (action.intent_ref if action else None)), None)
        if action is None or action.kind != "media_inspection" or artifact is None:
            raise MediaExecutionError("inspection Action or artifact is unavailable")
        prior = next((item for item in projection.media_inspections if item.artifact_id == artifact.artifact_id), None)
        if prior is not None:
            return prior
        if action.state != "delivered" or not any(item.receipt_id == receipt_id and item.action_id == action_id for item in projection.execution_receipts):
            raise MediaExecutionError("inspection record requires delivered Action and exact receipt")
        self._sidecar.put_if_absent(inspection_payload)
        record = MediaInspectionRecord(inspection_id="inspection:media:" + media_digest({"artifact": artifact.artifact_id}), plan_id=artifact.plan_id, artifact_id=artifact.artifact_id, inspection_action_id=action_id, passed=passed, reason_code=reason_code, observed_summary=observed_summary, inspection_payload_ref=inspection_payload.payload_ref, inspection_payload_hash=inspection_payload.payload_hash, repairable=repairable, repair_scope=repair_scope)
        payload = MediaInspectionRecordedPayload(action_id=action_id, receipt_id=receipt_id, inspection=record).model_dump(mode="json")
        items: list[tuple[str, dict[str, object], str]] = [("MediaInspectionRecorded", payload, record.inspection_id)]
        if not passed and repairable and not any(item.kind == "media_repair" and item.intent_ref == artifact.plan_id for item in projection.actions):
            trigger_id = media_repair_trigger_id(world_id=self._ledger.world_id, inspection_id=record.inspection_id)
            process = TriggerProcess(trigger_id=trigger_id, trigger_ref=f"media-repair:{record.inspection_id}", process_kind="media_repair", source_evidence_ref=f"inspection:{record.inspection_id}", state="open")
            items.append(("TriggerProcessOpened", {"process": process.model_dump(mode="json")}, trigger_id))
        events = self._events(items=tuple(items), actor="system:media-execution", logical_time=logical_time, trace_id=action.trace_id, correlation_id=action.correlation_id, causation_id=action_id)
        self._ledger.commit_at_cursor(events, expected_cursor=self._cursor(projection), commit_id="commit:media-inspection:" + media_digest([event.event_id for event in events]))
        return record

    def accept_repair(
        self, *, inspection_id: str, actor: str, grant: ProviderMediaGrantBinding, account_id: str,
        amount_limit: int, owner_id: str, logical_time: datetime, trace_id: str, correlation_id: str,
    ) -> Action:
        """Atomically accept one repair and create its effect-once provider Action.

        This is deliberately the acceptance seam: callers may decide to
        abandon the open trigger, but they cannot create a repair Action by
        directly invoking a renderer or by mutating the original plan.
        """
        projection = self._ledger.project()
        inspection = next((item for item in projection.media_inspections if item.inspection_id == inspection_id), None)
        if inspection is None or inspection.passed or not inspection.repairable:
            raise MediaExecutionError("repair requires one failed repairable inspection")
        artifact = next((item for item in projection.media_artifacts if item.artifact_id == inspection.artifact_id), None)
        plan = next((item for item in projection.media_plans if item.plan_id == inspection.plan_id), None)
        if artifact is None or plan is None or artifact.plan_id != plan.plan_id:
            raise MediaExecutionError("repair evidence is unavailable")
        trigger_id = media_repair_trigger_id(world_id=self._ledger.world_id, inspection_id=inspection_id)
        trigger = next((item for item in projection.trigger_processes if item.trigger_id == trigger_id), None)
        repair_id = media_repair_attempt_id(plan_id=plan.plan_id, failed_artifact_hash=artifact.artifact_hash)
        action_id = media_repair_action_id(world_id=self._ledger.world_id, repair_attempt_id=repair_id)
        existing = next((item for item in projection.actions if item.action_id == action_id), None)
        if existing is not None:
            return existing
        if trigger is None or trigger.state != "open":
            raise MediaExecutionError("repair requires its exact open inspection trigger")
        if any(item.kind == "media_repair" and item.intent_ref == plan.plan_id for item in projection.actions):
            raise MediaExecutionError("a MediaPlan may be repaired at most once")
        # Resolving all three sidecars before accepting prevents a crash/retry
        # from silently changing visual evidence.
        self._require_plan_payload(plan)
        self._require_payload(artifact.artifact_ref, artifact.artifact_hash)
        self._require_payload(inspection.inspection_payload_ref, inspection.inspection_payload_hash)
        reservation = BudgetReservation(reservation_id=media_repair_reservation_id(world_id=self._ledger.world_id, repair_attempt_id=repair_id), account_id=account_id, action_id=action_id, category="repair", amount_limit=amount_limit)
        action = Action(schema_version="world-v2.1", action_id=action_id, world_id=self._ledger.world_id, logical_time=logical_time, created_at=logical_time, trace_id=trace_id, causation_id=_event_id("MediaInspectionRecorded", inspection_id), correlation_id=correlation_id, kind="media_repair", layer="media_action", intent_ref=plan.plan_id, actor=actor, target="provider:media-renderer", payload_ref=inspection.inspection_payload_ref, payload_hash=inspection.inspection_payload_hash, provider_media_grant=grant, idempotency_key=repair_id, budget_reservation_id=reservation.reservation_id, state="authorized", recovery_policy="effect_once")
        repair = MediaRepairAuthorization(repair_attempt_id=repair_id, trigger_id=trigger_id, plan_id=plan.plan_id, opportunity_id=plan.opportunity_id, event_snapshot_hash=plan.event_snapshot_hash, failed_artifact_id=artifact.artifact_id, failed_artifact_hash=artifact.artifact_hash, inspection_id=inspection_id, inspection_payload_hash=inspection.inspection_payload_hash, defect_scope=inspection.repair_scope, action_id=action_id, reservation_id=reservation.reservation_id)
        lease = ClaimLease(owner_id=owner_id, attempt_id="attempt:media-repair:" + media_digest({"trigger": trigger_id, "repair": repair_id}), acquired_at=logical_time, expires_at=logical_time + timedelta(minutes=5))
        claimed = trigger.model_copy(update={"state": "claimed", "claim_lease": lease, "attempt_ids": (lease.attempt_id,)})
        completed = {"trigger_id": trigger_id, "owner_id": owner_id, "attempt_id": lease.attempt_id, "completed_at": logical_time.isoformat(), "runtime_outcome_ref": repair_id}
        items = (
            ("TriggerProcessClaimed", {"process": claimed.model_dump(mode="json")}, lease.attempt_id),
            ("MediaRepairAuthorized", MediaRepairAuthorizedPayload(repair=repair).model_dump(mode="json"), repair_id),
            ("BudgetReserved", {"reservation": reservation.model_dump(mode="json")}, reservation.reservation_id),
            ("ActionAuthorized", {"action": action.model_dump(mode="json")}, action_id),
            ("TriggerProcessCompleted", completed, repair_id),
        )
        events = self._events(items=items, actor=actor, logical_time=logical_time, trace_id=trace_id, correlation_id=correlation_id, causation_id=action.causation_id)
        self._ledger.commit_at_cursor(events, expected_cursor=self._cursor(projection), commit_id="commit:media-repair-accept:" + media_digest([event.event_id for event in events]))
        return action

    def materialize_preview(self, *, inspection_id: str, logical_time: datetime, trace_id: str, correlation_id: str):
        projection = self._ledger.project()
        inspection = next((item for item in projection.media_inspections if item.inspection_id == inspection_id), None)
        if inspection is None:
            raise MediaExecutionError("preview requires inspection")
        existing = next((item for item in projection.media_previews if item.inspection_id == inspection_id), None)
        if existing is not None:
            return existing
        if inspection.plan_id in projection.media_failed_plan_ids:
            return None
        if not inspection.passed:
            # A repairable first failure leaves the preview lane open for the
            # distinct accepted repair Action.  A second failure (or a choice
            # not to repair) is terminal and never retries again.
            if inspection.repairable and not any(item.kind == "media_repair" and item.intent_ref == inspection.plan_id for item in projection.actions):
                return None
            payload = MediaPreviewFailedPayload(plan_id=inspection.plan_id, artifact_id=inspection.artifact_id, inspection_id=inspection_id, reason_code=inspection.reason_code).model_dump(mode="json")
            self._commit_one("MediaPreviewFailed", payload, inspection.plan_id, logical_time, trace_id, correlation_id, inspection_id)
            return None
        plan = next((item for item in projection.media_plans if item.plan_id == inspection.plan_id), None)
        if plan is None:
            raise MediaExecutionError("inspection plan is unavailable")
        opportunity = next((item for item in projection.media_opportunities if item.opportunity_id == plan.opportunity_id), None)
        if opportunity is None or opportunity.delivery_mode != "preview":
            raise MediaExecutionError("Media v2 may materialize previews only; delivery is forbidden")
        preview = MediaPreview(preview_id="preview:media:" + media_digest({"inspection": inspection_id}), plan_id=plan.plan_id, artifact_id=inspection.artifact_id, inspection_id=inspection_id, recipient_ref=opportunity.recipient_ref)
        self._commit_one("MediaPreviewGenerated", MediaPreviewGeneratedPayload(preview=preview).model_dump(mode="json"), preview.preview_id, logical_time, trace_id, correlation_id, inspection_id)
        return preview

    def _require_plan_payload(self, plan: MediaPlan) -> StoredMediaPayload:
        return self._require_payload(plan.plan_payload_ref, plan.plan_payload_hash)

    def _require_payload(self, ref: str, expected_hash: str) -> StoredMediaPayload:
        record = self._sidecar.read_exact(payload_ref=ref)
        if record is None or record.payload_hash != expected_hash:
            raise MediaExecutionError("exact immutable media sidecar payload is unavailable")
        return record

    @staticmethod
    def _action_and_plan(projection, action_id: str, kind: str | set[str]) -> tuple[Action, MediaPlan]:
        action = next((item for item in projection.actions if item.action_id == action_id), None)
        plan = next((item for item in projection.media_plans if item.plan_id == (action.intent_ref if action else None)), None)
        kinds = {kind} if isinstance(kind, str) else kind
        if action is None or action.kind not in kinds or plan is None:
            raise MediaExecutionError("media Action or plan is unavailable")
        return action, plan

    def _events(self, *, items, actor: str, logical_time: datetime, trace_id: str, correlation_id: str, causation_id: str) -> tuple[WorldEvent, ...]:
        events: list[WorldEvent] = []
        for event_type, payload, stable in items:
            events.append(WorldEvent.from_payload(schema_version="world-v2.1", event_id=_event_id(event_type, stable), event_type=event_type, world_id=self._ledger.world_id, logical_time=logical_time, created_at=logical_time, actor=actor, source="world-v2:media-execution", trace_id=trace_id, causation_id=events[-1].event_id if events else causation_id, correlation_id=correlation_id, idempotency_key=_idempotency(event_type, self._ledger.world_id, payload), payload=payload))
        return tuple(events)

    def _commit_one(self, event_type: str, payload: dict[str, object], stable: str, logical_time: datetime, trace_id: str, correlation_id: str, causation_id: str) -> None:
        projection = self._ledger.project()
        event = self._events(items=((event_type, payload, stable),), actor="system:media-execution", logical_time=logical_time, trace_id=trace_id, correlation_id=correlation_id, causation_id=causation_id)[0]
        self._ledger.commit_at_cursor((event,), expected_cursor=self._cursor(projection), commit_id="commit:media-execution:" + event.event_id)


class MediaExecutionWorker:
    """Materialize exactly one already-receipted provider media result.

    The worker deliberately cannot dispatch an Action and cannot invent a
    result.  It only joins a durable terminal Action receipt to the provider's
    idempotency-keyed sidecar contract.  It is therefore safe to rerun after a
    process restart: an absent result remains pending, and an existing domain
    record is returned without another media provider call.
    """

    def __init__(
        self, *, runtime: MediaExecutionRuntime, ledger: LedgerPort,
        transport: MediaProviderResultTransport,
    ) -> None:
        self._runtime, self._ledger, self._transport = runtime, ledger, transport

    async def drain_once(self, *, logical_time: datetime) -> str | None:
        projection = self._ledger.project()
        for action in projection.actions:
            if action.kind not in {"media_render", "media_repair", "media_inspection"}:
                continue
            if action.kind in {"media_render", "media_repair"} and any(
                item.render_action_id == action.action_id for item in projection.media_artifacts
            ):
                continue
            if action.kind == "media_inspection" and any(
                item.inspection_action_id == action.action_id for item in projection.media_inspections
            ):
                continue
            if action.state in {"failed", "unknown", "expired", "cancelled"}:
                if action.kind in {"media_render", "media_repair"}:
                    self._runtime.record_render_failure(
                        action_id=action.action_id,
                        reason_code="provider_" + action.state,
                        logical_time=logical_time,
                    )
                    return "render_failed"
                continue
            if action.state != "delivered":
                continue
            receipt = _terminal_receipt(projection.execution_receipts, action.action_id)
            if receipt is None:
                raise MediaExecutionError("delivered media Action has no terminal receipt")
            result = await self._transport.lookup_execution_result(
                action_id=action.action_id,
                idempotency_key=action.idempotency_key,
                request_fingerprint=_request_fingerprint_from_receipt(receipt),
            )
            if result is None:
                return None
            self._verify_result(action=action, receipt=receipt, result=result)
            if isinstance(result, MediaProviderArtifactResult):
                if action.kind not in {"media_render", "media_repair"}:
                    raise MediaExecutionError("inspection Action cannot materialize an artifact result")
                self._runtime.record_rendered_artifact(
                    action_id=action.action_id,
                    receipt_id=receipt.receipt_id,
                    artifact_payload=result.artifact_payload(),
                    logical_time=logical_time,
                )
                return "artifact_recorded"
            if action.kind != "media_inspection":
                raise MediaExecutionError("render Action cannot materialize an inspection result")
            inspection = self._runtime.record_inspection(
                action_id=action.action_id,
                receipt_id=receipt.receipt_id,
                passed=result.passed,
                reason_code=result.reason_code,
                observed_summary=result.observed_summary,
                inspection_payload=result.inspection_payload(),
                logical_time=logical_time,
                repairable=result.repairable,
                repair_scope=result.repair_scope,
            )
            self._runtime.materialize_preview(
                inspection_id=inspection.inspection_id,
                logical_time=logical_time,
                trace_id=action.trace_id,
                correlation_id=action.correlation_id,
            )
            return "inspection_recorded"
        return None

    @staticmethod
    def _verify_result(*, action: Action, receipt: ExecutionReceipt, result) -> None:
        if result.action_id != action.action_id or result.idempotency_key != action.idempotency_key:
            raise MediaExecutionError("provider media result does not bind delivered Action")
        request_fingerprint = _request_fingerprint_from_receipt(receipt)
        if result.request_fingerprint != request_fingerprint:
            raise MediaExecutionError("provider media result does not bind dispatched request fingerprint")
        if media_provider_result_hash(result) != receipt.raw_payload_hash:
            raise MediaExecutionError("provider media result bytes do not bind terminal receipt hash")


def _terminal_receipt(receipts: tuple[ExecutionReceipt, ...], action_id: str) -> ExecutionReceipt | None:
    matches = tuple(item for item in receipts if item.action_id == action_id and item.is_terminal)
    if len(matches) > 1:
        raise MediaExecutionError("media Action has ambiguous terminal receipts")
    return matches[0] if matches else None


def _request_fingerprint_from_receipt(receipt: ExecutionReceipt) -> str:
    """The provider ref is not a request identity; receipts must carry it.

    The generic v2 ``ExecutionReceipt`` intentionally predates the media
    provider contract and has no fingerprint field.  The provider result
    transport gets the canonical dispatch fingerprint via this reserved,
    immutable artifact reference.  Older/non-media receipts have no such
    value and are rejected rather than guessed.
    """

    matches = tuple(value.removeprefix("request:") for value in receipt.artifact_refs if value.startswith("request:sha256:"))
    if len(matches) != 1:
        raise MediaExecutionError("media terminal receipt lacks one request fingerprint evidence ref")
    return matches[0]


__all__ = ["EventMediaExecutionAdapter", "MediaExecutionAdapter", "MediaExecutionError", "MediaExecutionRuntime", "MediaExecutionWorker"]

from contextlib import asynccontextmanager
from datetime import datetime
import hashlib
import json
from pathlib import Path
import secrets
from typing import Literal

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from companion_daemon.config import get_settings
from companion_daemon.companion_turn import (
    CompanionTurn,
    ExternalObservation,
    TurnEnvelope,
    TurnOptions,
)
from companion_daemon.dashboard_ui import DASHBOARD_HTML
from companion_daemon.world_console_ui import WORLD_CONSOLE_HTML
from companion_daemon.models import CompanionReply, IncomingMessage, ProactiveDecision
from companion_daemon.qq_official import (
    QQ_CALLBACK_VALIDATION_OP,
    ack_response,
    incoming_message_from_payload,
    validation_response,
    verify_callback_signature,
)
from companion_daemon.world import ConcurrencyConflict, WorldError, WorldKernel
from companion_daemon.qq_delivery import QQDelivery
from companion_daemon.turn_transports import CaptureTurnTransport
from companion_daemon.time import utc_now
from companion_daemon.world_v2.http_capture_host import (
    HttpV2CaptureHost,
    build_http_v2_capture_host,
)
from companion_daemon.world_v2.errors import IdempotencyConflict


@asynccontextmanager
async def lifespan(_app: FastAPI):
    yield
    if http_v2_capture is not None:
        await http_v2_capture.aclose()
    await engine.aclose()


app = FastAPI(title="Girl Agent Companion Daemon", lifespan=lifespan)
app.mount("/assets", StaticFiles(directory=Path(__file__).resolve().parents[2] / "assets"), name="assets")
app.mount(
    "/dashboard-static",
    StaticFiles(directory=Path(__file__).resolve().parent / "static"),
    name="dashboard-static",
)


class _LazyArchiveEngine:
    """Keep archive-only Engine construction out of the selected v2 ingress.

    ``app`` still exposes explicit archive/debug routes while their migration
    is incomplete.  Constructing their Engine when this ASGI module is merely
    imported would nevertheless make every HTTP v2 request acquire a second
    runtime authority.  The proxy creates that archived runtime only when an
    archive route actually dereferences it.  Tests may continue to replace the
    public ``engine`` binding with a concrete fixture.
    """

    def __init__(self) -> None:
        self._instance: object | None = None

    def _resolve(self) -> object:
        if self._instance is None:
            from companion_daemon.runtime import build_companion_engine

            self._instance = build_companion_engine()
        return self._instance

    def __getattr__(self, name: str) -> object:
        return getattr(self._resolve(), name)

    async def aclose(self) -> None:
        if self._instance is None:
            return
        closer = getattr(self._instance, "aclose", None)
        if closer is not None:
            await closer()


engine: object = _LazyArchiveEngine()
# HTTP is the first real platform migration.  QQ and the legacy operator
# archive routes remain on their existing adapter during the staged cutover,
# but ``/messages`` never reaches this Engine.
http_v2_capture: HttpV2CaptureHost | None = None


def _http_v2_capture(*, bootstrap_at: datetime | None = None) -> HttpV2CaptureHost:
    global http_v2_capture
    if http_v2_capture is None:
        http_v2_capture = build_http_v2_capture_host(
            settings=get_settings(), bootstrap_at=bootstrap_at
        )
    return http_v2_capture


def _http_v2_ingress_evidence(message: IncomingMessage) -> tuple[tuple[str, ...], dict[str, object]]:
    """Freeze the complete HTTP attachment/source evidence into v2 ingress.

    The Action/LLM path receives only stable opaque attachment references, but
    their complete descriptors stay inside the immutable observation hash.
    Retrying a provider message id with a changed attachment therefore fails
    closed instead of silently joining the earlier observation.
    """

    if len(message.attachments) > 16:
        raise ValueError("HTTP capture supports at most 16 attachments per message")
    attachment_evidence = [
        {
            "message_id": message.message_id,
            "index": index,
            "attachment": attachment.model_dump(mode="json"),
        }
        for index, attachment in enumerate(message.attachments)
    ]
    attachment_refs = tuple(
        "attachment:http:"
        + hashlib.sha256(
            json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        for item in attachment_evidence
    )
    metadata: dict[str, object] = {}
    if attachment_evidence:
        metadata["attachments"] = attachment_evidence
    if message.channel_id:
        metadata["channel_id"] = message.channel_id
    if message.emoji:
        metadata["emoji"] = list(message.emoji)
    if message.sticker_kind:
        metadata["sticker_kind"] = message.sticker_kind
    if message.reply_target:
        metadata["reply_target"] = message.reply_target
    if message.source_message_ids:
        metadata["source_message_ids"] = list(message.source_message_ids)
    if message.source_messages:
        metadata["source_messages"] = [
            source.model_dump(mode="json") for source in message.source_messages
        ]
    return attachment_refs, metadata


def _require_world_v2_internal_access(token: str | None) -> None:
    """Gate scheduler/recovery controls behind the existing operator secret.

    A dedicated scheduler credential can replace this at deployment time; the
    v2 host deliberately starts disabled rather than exposing clock/action
    control to arbitrary HTTP clients.
    """

    configured = (get_settings().delivery_reconciliation_token or "").strip()
    if not configured:
        raise HTTPException(
            status_code=503,
            detail="World v2 internal scheduler is disabled until an operator token is configured",
        )
    if not token or not secrets.compare_digest(token, configured):
        raise HTTPException(status_code=403, detail="invalid World v2 internal scheduler token")


class StatePatch(BaseModel):
    updates: dict[str, object] = Field(default_factory=dict)


class MemoryPatch(BaseModel):
    kind: str
    content: str
    confidence: float = 0.7
    source: str = "dashboard"


class WorldCommandRequest(BaseModel):
    expected_revision: int
    command: dict[str, object]


class WorldClockRequest(BaseModel):
    expected_revision: int
    target_logical_at: str


class WorldV2ClockTickRequest(BaseModel):
    tick_id: str = Field(min_length=1, max_length=256)
    logical_time_from: datetime
    logical_time_to: datetime
    observed_at: datetime
    trace_id: str = Field(min_length=1, max_length=256)
    causation_id: str = Field(min_length=1, max_length=256)
    correlation_id: str = Field(min_length=1, max_length=256)
    reason: str = Field(min_length=1, max_length=256)
    policy_version: str | None = Field(default=None, max_length=128)
    policy_digest: str | None = Field(default=None, max_length=256)


class WorldV2DrainRequest(BaseModel):
    max_action_units: int = Field(default=8, ge=0, le=64)
    max_background_units: int = Field(default=8, ge=0, le=64)


class DeliveryReconciliationRequest(BaseModel):
    expected_revision: int = Field(ge=0)
    status: Literal["delivered", "failed"]
    evidence_kind: Literal["platform_receipt", "operator_verification"]
    external_receipt: str = Field(min_length=1, max_length=500)
    # This is an operator-declared audit identity.  The configured token is a
    # shared break-glass credential, not proof of a separate human identity.
    reviewer_id: str = Field(min_length=1, max_length=100)
    review_note: str = Field(min_length=1, max_length=1000)
    failure_reason: str | None = Field(default=None, max_length=500)
    segment_id: str | None = Field(default=None, max_length=200)
    cancel_remaining: bool = False
    cancel_remaining_reason: str | None = Field(default=None, max_length=500)


# The browser is an operator surface, not a transport adapter.  In particular
# it must never manufacture a delivery/model receipt or turn arbitrary text
# into a confirmed fact.  Those commands remain available only through the
# in-process WorldKernel adapters that own the corresponding external result.
WORLD_OPERATOR_COMMANDS = frozenset({
    "set_clock_mode", "register_npc", "register_user", "plan_activity",
    "review_activity", "review_goal", "change_relationship", "change_need", "cancel_action",
})


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard() -> str:
    return DASHBOARD_HTML


@app.get("/world-console", response_class=HTMLResponse)
def world_console() -> str:
    """Operator-only world view; the pixel-room projection remains separate."""
    return WORLD_CONSOLE_HTML


@app.post("/messages", response_model=None)
async def post_message(message: IncomingMessage) -> CompanionReply | JSONResponse:
    if not message.text.strip():
        raise HTTPException(status_code=400, detail="text is required")
    if not message.message_id:
        raise HTTPException(status_code=400, detail="message_id is required for idempotent delivery")
    attachment_refs, metadata = _http_v2_ingress_evidence(message)
    try:
        result = await _http_v2_capture(bootstrap_at=message.sent_at).respond(
            platform=message.platform,
            platform_user_id=message.platform_user_id,
            platform_message_id=message.message_id,
            text=message.text,
            observed_at=message.sent_at,
            attachment_refs=attachment_refs,
            coalescing_metadata=metadata,
        )
    except IdempotencyConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if result.text is None:
        return JSONResponse(
            status_code=202,
            content={
                "status": result.status,
                "message_id": message.message_id,
                "world_action_id": result.action_id,
            },
        )
    return CompanionReply(
        canonical_user_id=result.canonical_user_id,
        mood="calm",
        text=result.text,
        text_parts=[result.text],
        world_action_id=result.action_id,
    )


@app.post("/internal/world-v2/tick")
async def world_v2_tick(
    request: WorldV2ClockTickRequest,
    x_world_v2_internal_token: str | None = Header(default=None),
) -> dict[str, object]:
    """Scheduler-only logical clock ingress for the migrated HTTP v2 lane."""

    _require_world_v2_internal_access(x_world_v2_internal_token)
    try:
        status = await _http_v2_capture(bootstrap_at=request.logical_time_from).tick(
            tick_id=request.tick_id,
            logical_time_from=request.logical_time_from,
            logical_time_to=request.logical_time_to,
            observed_at=request.observed_at,
            trace_id=request.trace_id,
            causation_id=request.causation_id,
            correlation_id=request.correlation_id,
            reason=request.reason,
            policy_version=request.policy_version,
            policy_digest=request.policy_digest,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": status, "tick_id": request.tick_id}


@app.post("/internal/world-v2/drain")
async def world_v2_drain(
    request: WorldV2DrainRequest,
    x_world_v2_internal_token: str | None = Header(default=None),
) -> dict[str, list[str]]:
    """Run bounded capture Action/background recovery without an Engine call."""

    _require_world_v2_internal_access(x_world_v2_internal_token)
    result = await _http_v2_capture().drain(
        max_action_units=request.max_action_units,
        max_background_units=request.max_background_units,
    )
    return {
        "action_statuses": list(result.action_statuses),
        "background_statuses": list(result.background_statuses),
    }


@app.get("/internal/world-v2/dashboard-room")
def world_v2_dashboard_room(
    x_world_v2_internal_token: str | None = Header(default=None),
) -> dict[str, object]:
    """Return the operator-authorized, public-only World v2 room DTO.

    This is intentionally separate from the legacy dashboard overview.  Its
    fixed composition request can neither read private state nor ask the v2
    projection compiler for diagnostic/operator fields.
    """

    _require_world_v2_internal_access(x_world_v2_internal_token)
    # A dashboard GET must not be the process that opens and bootstraps a
    # writable World v2 application.  The selected ingress/scheduler host
    # owns that lifecycle; until it exists this reader is deliberately
    # unavailable rather than turning a supposedly read-only request into a
    # WorldStarted/BudgetAccountConfigured commit.
    if http_v2_capture is None:
        raise HTTPException(
            status_code=503,
            detail="World v2 dashboard capture is unavailable until the platform host is initialized",
        )
    try:
        return http_v2_capture.dashboard_room().to_payload()
    except PermissionError as exc:
        # A composition/configuration error must not downgrade into a legacy
        # overview or return an unredacted diagnostic projection.
        raise HTTPException(status_code=403, detail="World v2 dashboard projection denied") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.post("/proactive/{canonical_user_id}", response_model=ProactiveDecision)
async def proactive(canonical_user_id: str) -> ProactiveDecision:
    return await engine.proactive_tick(canonical_user_id)


@app.get("/debug/{canonical_user_id}/context")
def debug_context(
    canonical_user_id: str,
    preview_text: str = Query(default=""),
    platform: str = Query(default="qq"),
) -> dict[str, object]:
    return engine.debug_snapshot(
        canonical_user_id,
        preview_text=preview_text,
        platform=platform,
    )


@app.get("/debug/users")
def debug_users() -> dict[str, list[str]]:
    return {"users": engine.store.canonical_users()}


@app.post("/debug/{canonical_user_id}/state")
def debug_update_state(canonical_user_id: str, patch: StatePatch) -> dict[str, object]:
    raise HTTPException(status_code=409, detail="world runtime forbids direct state mutation")


@app.post("/debug/{canonical_user_id}/memories")
def debug_upsert_memory(canonical_user_id: str, patch: MemoryPatch) -> dict[str, object]:
    raise HTTPException(status_code=409, detail="world runtime forbids direct memory mutation")


@app.delete("/debug/{canonical_user_id}/memories")
def debug_delete_memory(
    canonical_user_id: str,
    kind: str = Query(...),
    content: str = Query(...),
) -> dict[str, object]:
    raise HTTPException(status_code=409, detail="world runtime forbids direct memory mutation")


@app.get("/world/{world_id}")
def world_snapshot(world_id: str) -> dict[str, object]:
    try:
        return WorldKernel(engine.store).snapshot(world_id)
    except WorldError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/world/{world_id}/events")
def world_events(world_id: str) -> list[dict[str, object]]:
    try:
        return WorldKernel(engine.store).export_ledger(world_id)
    except WorldError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/world/{world_id}/integrity")
def world_integrity(world_id: str) -> dict[str, object]:
    try:
        return WorldKernel(engine.store).verify_ledger(world_id)
    except WorldError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/world/{world_id}/commands")
def world_command(world_id: str, request: WorldCommandRequest) -> dict[str, object]:
    command = {**request.command, "world_id": world_id}
    command_type = str(command.get("type") or "")
    if command_type not in WORLD_OPERATOR_COMMANDS:
        raise HTTPException(
            status_code=403,
            detail="browser command is not allowed to create facts or settle external results",
        )
    try:
        decision = WorldKernel(engine.store).submit(command, expected_revision=request.expected_revision)
    except ConcurrencyConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except WorldError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "world_id": decision.world_id,
        "revision": decision.revision,
        "events": [event.event_type for event in decision.events],
        "state_hash": decision.state_hash,
    }


@app.post("/world/{world_id}/advance")
def world_advance(world_id: str, request: WorldClockRequest) -> dict[str, object]:
    from datetime import datetime

    try:
        decision = WorldKernel(engine.store).advance(
            world_id,
            datetime.fromisoformat(request.target_logical_at),
            expected_revision=request.expected_revision,
        )
    except ConcurrencyConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (ValueError, WorldError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"world_id": decision.world_id, "revision": decision.revision, "events": [event.event_type for event in decision.events]}


@app.post("/world/{world_id}/rebuild")
def world_rebuild(world_id: str) -> dict[str, object]:
    try:
        return WorldKernel(engine.store).rebuild_projection(world_id, "world_current_state").__dict__
    except WorldError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/world/{world_id}/enablement")
def world_enablement(world_id: str) -> dict[str, object]:
    try:
        report = WorldKernel(engine.store).audit_enablement(
            world_id,
            delivery_receipts_supported=QQDelivery(get_settings()).supports_delivery_receipts(),
        )
    except WorldError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "world_id": report.world_id,
        "ready": report.ready,
        "delivery_receipts_supported": report.delivery_receipts_supported,
        "open_action_ids": list(report.open_action_ids),
        "unknown_action_ids": list(report.unknown_action_ids),
        "invariant_errors": list(report.invariant_errors),
        "projections": [item.__dict__ for item in report.projection_reports],
    }


@app.get("/world-runtime/enablement")
def active_world_enablement() -> dict[str, object]:
    if not engine.world_kernel or not engine.world_id:
        return {"enabled": False}
    report = engine.world_kernel.audit_enablement(
        engine.world_id,
        delivery_receipts_supported=QQDelivery(get_settings()).supports_delivery_receipts(),
    )
    return {
        "enabled": True, "world_id": report.world_id, "ready": report.ready,
        "delivery_receipts_supported": report.delivery_receipts_supported,
        "open_action_ids": list(report.open_action_ids), "unknown_action_ids": list(report.unknown_action_ids),
        "invariant_errors": list(report.invariant_errors),
        "projections": [item.__dict__ for item in report.projection_reports],
    }


@app.get("/world-runtime/overview")
def active_world_overview() -> dict[str, object]:
    """Bounded console projection for the active world epoch."""
    if not engine.world_kernel or not engine.world_id:
        return {"enabled": False}
    return {"enabled": True, **engine.world_kernel.dashboard_overview(engine.world_id)}


@app.post("/world/{world_id}/deliveries/{delivery_id}/reconcile")
async def reconcile_unknown_delivery(
    world_id: str,
    delivery_id: int,
    request: DeliveryReconciliationRequest,
    x_delivery_reconciliation_token: str | None = Header(default=None),
) -> dict[str, object]:
    """Forensically reconcile one *unknown* delivery after manual review.

    Normal receipts belong to their platform adapter.  This token-gated route
    is only the crash-recovery exception for a receipt path that was lost; it
    carries review evidence through ``CompanionTurn.settle`` and cannot
    dispatch another planned beat as a side effect.
    """
    configured_token = (get_settings().delivery_reconciliation_token or "").strip()
    if not configured_token:
        raise HTTPException(
            status_code=503,
            detail="delivery reconciliation is disabled until its operator token is configured",
        )
    if not x_delivery_reconciliation_token or not secrets.compare_digest(
        x_delivery_reconciliation_token, configured_token
    ):
        raise HTTPException(status_code=403, detail="invalid delivery reconciliation token")
    if (
        not request.external_receipt.strip()
        or not request.reviewer_id.strip()
        or not request.review_note.strip()
    ):
        raise HTTPException(
            status_code=400,
            detail="receipt, reviewer, and review note must contain non-whitespace evidence",
        )
    if request.status == "failed" and not (request.failure_reason or "").strip():
        raise HTTPException(
            status_code=400,
            detail="failed reconciliation requires a failure reason",
        )
    if request.cancel_remaining and not (request.cancel_remaining_reason or "").strip():
        raise HTTPException(
            status_code=400,
            detail="cancelling remaining beats requires an audited reason",
        )

    if engine.world_kernel is None or engine.world_id != world_id:
        raise HTTPException(
            status_code=409,
            detail="operator reconciliation is limited to the active CompanionTurn World",
        )
    kernel = engine.world_kernel
    try:
        snapshot = kernel.snapshot(world_id)
    except WorldError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    action_id = kernel.action_id_for_delivery(world_id, delivery_id)
    if not action_id:
        raise HTTPException(
            status_code=404,
            detail=f"delivery {delivery_id} is not an outgoing action in world {world_id}",
        )
    action = snapshot["actions"][action_id]
    segments = action.get("segment_state", {}).get("segments", [])
    current_status = str(action.get("status") or "")
    selected_segment_id = request.segment_id
    if selected_segment_id:
        segment = next(
            (
                item
                for item in segments
                if isinstance(item, dict) and item.get("segment_id") == selected_segment_id
            ),
            None,
        )
        if not isinstance(segment, dict):
            raise HTTPException(status_code=404, detail="segment is not part of this delivery")
        if segment.get("status") == "delivered":
            if (
                request.status != "delivered"
                or str(segment.get("external_receipt") or "")
                != request.external_receipt.strip()
            ):
                raise HTTPException(
                    status_code=409,
                    detail="delivered segment receipt conflicts with reconciliation evidence",
                )
            return {
                "world_id": world_id,
                "delivery_id": delivery_id,
                "action_id": action_id,
                "segment_id": selected_segment_id,
                "status": "delivered",
                "reconciled": False,
                "revision": kernel.revision(world_id),
            }
        unresolved_segments = [
            item
            for item in segments
            if isinstance(item, dict)
            and item.get("status") in {"planned", "unknown"}
        ]
        is_one_unclaimed_unknown_action = (
            current_status == "unknown"
            and segment.get("status") == "planned"
            and len(unresolved_segments) == 1
        )
        if segment.get("status") != "unknown" and not is_one_unclaimed_unknown_action:
            raise HTTPException(
                status_code=409,
                detail="segment reconciliation requires an unknown segment or one unclaimed unknown Action",
            )
    if current_status != "unknown":
        if current_status == request.status:
            result = action.get("result", {})
            stored_receipt = (
                str(result.get("external_receipt") or "")
                if isinstance(result, dict)
                else ""
            )
            if stored_receipt != request.external_receipt.strip():
                raise HTTPException(
                    status_code=409,
                    detail="terminal delivery receipt conflicts with reconciliation evidence",
                )
            return {
                "world_id": world_id,
                "delivery_id": delivery_id,
                "action_id": action_id,
                "status": current_status,
                "reconciled": False,
                "revision": kernel.revision(world_id),
            }
        raise HTTPException(
            status_code=409,
            detail=f"delivery is already terminal with status {current_status}",
        )
    if kernel.revision(world_id) != request.expected_revision:
        raise HTTPException(
            status_code=409,
            detail=(
                f"expected revision {request.expected_revision}, "
                f"got {kernel.revision(world_id)}"
            ),
        )
    if not selected_segment_id:
        unresolved_segments = [
            item
            for item in segments
            if isinstance(item, dict)
            and item.get("status") in {"planned", "unknown"}
        ]
        if len(unresolved_segments) != 1:
            raise HTTPException(
                status_code=409,
                detail=(
                    "reconciliation requires one exact unresolved segment; "
                    "specify segment_id for a multi-segment delivery"
                ),
            )
        selected_segment_id = str(unresolved_segments[0]["segment_id"])

    evidence = {
        "kind": request.evidence_kind,
        "source": "operator_reconciliation",
        "reference": request.external_receipt.strip(),
        "reviewer_id": request.reviewer_id.strip(),
        "review_note": request.review_note.strip(),
        **(
            {"cancel_remaining_reason": request.cancel_remaining_reason.strip()}
            if request.cancel_remaining_reason and request.cancel_remaining_reason.strip()
            else {}
        ),
    }
    try:
        settlement = await CompanionTurn(
            engine,
            CaptureTurnTransport(receipt_namespace="operator-reconciliation"),
        ).settle(
            ExternalObservation(
                action_id=action_id,
                delivery_id=delivery_id,
                segment_id=selected_segment_id,
                status=request.status,
                kind="platform_receipt",
                observed_at=utc_now(),
                idempotency_key=(
                    f"operator-reconcile:{world_id}:{delivery_id}:{selected_segment_id}:"
                    f"{request.status}:{request.external_receipt.strip()}"
                ),
                world_id=world_id,
                external_receipt=request.external_receipt.strip(),
                reason=(request.failure_reason or "").strip() or None,
                reconciliation_evidence=evidence,
                cancel_remaining=request.cancel_remaining,
                settlement_origin="operator_reconciliation",
                expected_revision=request.expected_revision,
            ),
        )
    except ConcurrencyConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except WorldError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    settled_action = kernel.snapshot(world_id)["actions"][action_id]
    settled_segments = settled_action.get("segment_state", {}).get("segments", [])
    settled_segment = next(
        (
            item
            for item in settled_segments
            if isinstance(item, dict) and item.get("segment_id") == selected_segment_id
        ),
        {},
    )
    return {
        "world_id": world_id,
        "delivery_id": delivery_id,
        "action_id": action_id,
        "segment_id": selected_segment_id,
        "status": settled_segment.get("status", request.status),
        "action_status": settled_action.get("status"),
        "reconciled": True,
        "revision": settlement.committed_revision,
    }


@app.post("/qq/webhook")
async def qq_webhook(
    request: Request,
    x_signature_ed25519: str | None = Header(default=None),
    x_signature_timestamp: str | None = Header(default=None),
) -> JSONResponse:
    settings = get_settings()
    raw_body = await request.body()
    payload = await request.json()

    if settings.qq_verify_signatures and settings.qq_bot_secret:
        if not x_signature_ed25519 or not x_signature_timestamp:
            raise HTTPException(status_code=401, detail="missing QQ signature headers")
        if not verify_callback_signature(
            settings.qq_bot_secret,
            x_signature_timestamp,
            raw_body,
            x_signature_ed25519,
        ):
            raise HTTPException(status_code=401, detail="invalid QQ callback signature")

    if payload.get("op") == QQ_CALLBACK_VALIDATION_OP:
        if not settings.qq_bot_secret:
            raise HTTPException(status_code=500, detail="QQ_BOT_SECRET is required")
        return JSONResponse(validation_response(settings.qq_bot_secret, payload))

    incoming = incoming_message_from_payload(payload)
    if incoming:
        # This webhook has no outbound owner, so it must only observe.  Keep
        # the inbound event inside the same bounded seam as a replying turn:
        # the World receives a frozen platform/user/cadence envelope, while
        # the WebSocket/OneBot owner remains solely responsible for replies.
        turn_context = engine.freeze_turn_context(incoming)
        await CompanionTurn(
            engine,
            CaptureTurnTransport(receipt_namespace="qq-webhook-observe"),
        ).observe_only(
            TurnEnvelope.from_message(
                incoming,
                idempotency_key=(
                    f"{incoming.platform}:{incoming.platform_user_id}:{incoming.message_id}"
                ),
                world_id=engine.world_id,
                canonical_user_id=engine.store.resolve_user(
                    incoming.platform, incoming.platform_user_id
                ),
                frozen_cadence=turn_context.cadence.heat,
            ),
            mark_unread=True,
            options=TurnOptions(turn_context=turn_context),
        )

    return JSONResponse(ack_response())


def main() -> None:
    import uvicorn

    settings = get_settings()
    uvicorn.run("companion_daemon.app:app", host=settings.host, port=settings.port, reload=False)


LEGACY_DASHBOARD_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>沈知栀 Daemon 面板</title>
  <style>
    :root { color-scheme: light dark; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
    body { margin: 0; background: #f4f1ea; color: #202124; }
    header { padding: 18px 22px; background: #263238; color: white; display: flex; gap: 16px; align-items: center; }
    header h1 { font-size: 20px; margin: 0; font-weight: 650; }
    main { padding: 18px; display: grid; grid-template-columns: 340px 1fr; gap: 16px; }
    section, aside { background: #fffaf0; border: 1px solid #d8d0c1; border-radius: 8px; padding: 14px; }
    h2 { font-size: 15px; margin: 0 0 10px; }
    label { display: block; font-size: 12px; color: #5f6368; margin-top: 10px; }
    input, select, textarea, button { font: inherit; }
    input, select, textarea { width: 100%; box-sizing: border-box; border: 1px solid #c9c1b2; border-radius: 6px; padding: 7px; background: white; color: #202124; }
    textarea { min-height: 84px; resize: vertical; }
    button { border: 0; border-radius: 6px; padding: 8px 11px; background: #2f6f73; color: white; cursor: pointer; }
    button.secondary { background: #6d6258; }
    .grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }
    .toolbar { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
    .cards { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; }
    .card { background: white; border: 1px solid #ded6c8; border-radius: 8px; padding: 12px; min-height: 120px; overflow: auto; }
    pre { white-space: pre-wrap; word-break: break-word; margin: 0; font-size: 12px; }
    .list { display: flex; flex-direction: column; gap: 8px; }
    .item { border: 1px solid #ded6c8; border-radius: 6px; padding: 8px; background: white; }
    .muted { color: #6f6a60; font-size: 12px; }
    @media (max-width: 900px) { main { grid-template-columns: 1fr; } .cards { grid-template-columns: 1fr; } }
  </style>
</head>
<body>
  <header>
    <h1>沈知栀 Daemon 面板</h1>
    <span class="muted">daemon 是本体，面板只调状态和上下文</span>
  </header>
  <main>
    <aside>
      <h2>控制</h2>
      <label>用户</label>
      <select id="user"></select>
      <label>Prompt 预览输入</label>
      <textarea id="preview">你在干嘛</textarea>
      <div class="toolbar" style="margin-top:10px">
        <button onclick="loadContext()">刷新</button>
        <button class="secondary" onclick="runProactive()">主动 tick</button>
      </div>
      <h2 style="margin-top:18px">状态调节</h2>
      <div id="stateForm" class="grid"></div>
      <button style="margin-top:10px" onclick="saveState()">保存状态</button>
      <h2 style="margin-top:18px">新增记忆</h2>
      <label>kind</label><input id="memoryKind" value="note" />
      <label>content</label><textarea id="memoryContent"></textarea>
      <label>confidence</label><input id="memoryConfidence" type="number" min="0" max="1" step="0.05" value="0.7" />
      <button style="margin-top:10px" onclick="addMemory()">加入/更新记忆</button>
    </aside>
    <section>
      <div class="cards">
        <div class="card"><h2>当前状态</h2><pre id="state"></pre></div>
        <div class="card"><h2>生活运行时</h2><pre id="lifeRuntime"></pre></div>
        <div class="card"><h2>社交事务</h2><pre id="socialTasks"></pre></div>
        <div class="card"><h2>最近聊天</h2><div id="recent" class="list"></div></div>
        <div class="card"><h2>注入记忆</h2><div id="memories" class="list"></div></div>
      </div>
      <section style="margin-top:12px">
        <h2>Prompt 预览</h2>
        <pre id="prompt"></pre>
      </section>
      <section style="margin-top:12px">
        <h2>操作结果</h2>
        <pre id="result"></pre>
      </section>
    </section>
  </main>
  <script>
    const numericFields = ["intimacy","trust","attachment","patience","security","curiosity","initiative","emotional_charge","boundary_level"];
    let snapshot = null;
    async function init() {
      const users = await fetch("/debug/users").then(r => r.json());
      const select = document.getElementById("user");
      select.innerHTML = users.users.map(u => `<option>${u}</option>`).join("");
      if (!select.value) select.innerHTML = "<option>geoff</option>";
      await loadContext();
    }
    async function loadContext() {
      const user = document.getElementById("user").value || "geoff";
      const preview = encodeURIComponent(document.getElementById("preview").value);
      snapshot = await fetch(`/debug/${user}/context?preview_text=${preview}`).then(r => r.json());
      render();
    }
    function render() {
      document.getElementById("state").textContent = JSON.stringify(snapshot.state, null, 2);
      document.getElementById("lifeRuntime").textContent = JSON.stringify(snapshot.life_runtime, null, 2);
      document.getElementById("socialTasks").textContent = JSON.stringify(snapshot.recent_social_tasks, null, 2);
      document.getElementById("recent").innerHTML = snapshot.recent.map(x => `<div class="item">${escapeHtml(x)}</div>`).join("");
      document.getElementById("memories").innerHTML = snapshot.memories.map(x => `<div class="item">${escapeHtml(x)}<br><button class="secondary" onclick="deleteMemoryFromLine(this)">删除</button></div>`).join("");
      document.getElementById("prompt").textContent = snapshot.preview_prompt.map(m => `[${m.role}]\\n${m.content}`).join("\\n\\n---\\n\\n");
      const form = document.getElementById("stateForm");
      form.innerHTML = [
        `<label>mood<input data-state="mood" value="${snapshot.state.mood}"></label>`,
        `<label>relationship_stage<input data-state="relationship_stage" value="${snapshot.state.relationship_stage}"></label>`,
        ...numericFields.map(k => `<label>${k}<input data-state="${k}" type="number" min="0" max="100" value="${snapshot.state[k]}"></label>`),
        `<label style="grid-column:1/-1">unresolved_emotion<textarea data-state="unresolved_emotion">${snapshot.state.unresolved_emotion || ""}</textarea></label>`
      ].join("");
    }
    async function saveState() {
      const updates = {};
      document.querySelectorAll("[data-state]").forEach(el => {
        const key = el.dataset.state;
        updates[key] = numericFields.includes(key) ? Number(el.value) : (el.value || null);
      });
      const user = document.getElementById("user").value || "geoff";
      const res = await fetch(`/debug/${user}/state`, {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({updates})}).then(r => r.json());
      document.getElementById("result").textContent = JSON.stringify(res, null, 2);
      await loadContext();
    }
    async function addMemory() {
      const user = document.getElementById("user").value || "geoff";
      const payload = {kind: memoryKind.value, content: memoryContent.value, confidence: Number(memoryConfidence.value), source: "dashboard"};
      const res = await fetch(`/debug/${user}/memories`, {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(payload)}).then(r => r.json());
      document.getElementById("result").textContent = JSON.stringify(res, null, 2);
      await loadContext();
    }
    async function deleteMemoryFromLine(button) {
      const text = button.parentElement.firstChild.textContent;
      const match = text.match(/^- \\[([^\\]]+)\\] (.*)$/);
      if (!match) return;
      const user = document.getElementById("user").value || "geoff";
      const url = `/debug/${user}/memories?kind=${encodeURIComponent(match[1])}&content=${encodeURIComponent(match[2])}`;
      const res = await fetch(url, {method:"DELETE"}).then(r => r.json());
      document.getElementById("result").textContent = JSON.stringify(res, null, 2);
      await loadContext();
    }
    async function runProactive() {
      const user = document.getElementById("user").value || "geoff";
      const res = await fetch(`/proactive/${user}`, {method:"POST"}).then(r => r.json());
      document.getElementById("result").textContent = JSON.stringify(res, null, 2);
      await loadContext();
    }
    function escapeHtml(text) {
      return String(text).replace(/[&<>"']/g, c => {
        if (c === "&") return "&amp;";
        if (c === "<") return "&lt;";
        if (c === ">") return "&gt;";
        if (c === '"') return "&quot;";
        return "&#39;";
      });
    }
    init();
  </script>
</body>
</html>"""

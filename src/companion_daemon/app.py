from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
import asyncio
import hashlib
import json
import logging
from pathlib import Path
import secrets
import time
from urllib.parse import parse_qs

from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from companion_daemon.config import Settings, get_settings
from companion_daemon.models import CompanionReply, IncomingMessage
from companion_daemon.world_v2.http_capture_host import (
    HttpV2CaptureHost,
    build_http_v2_capture_host,
)
from companion_daemon.world_v2.errors import IdempotencyConflict
from companion_daemon.world_v2.platform_action_executor import MediaProviderTransport
from companion_daemon.world_v2.production_turn_application import MediaPreviewDeployment
from companion_daemon.world_v2.world_v2_dashboard_ui import (
    DASHBOARD_APP_JS,
    DASHBOARD_HTML,
    DASHBOARD_SESSION_COOKIE,
    DASHBOARD_SESSION_TTL_SECONDS,
    DashboardSessionCodec,
    LOGIN_HTML,
    UNAVAILABLE_HTML,
)


_LOG = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class HttpV2ASGIDeployment:
    """Immutable deployment-owned dependencies for the real HTTP entry."""

    settings: Settings
    media_preview: MediaPreviewDeployment | None = None
    media_transport: MediaProviderTransport | None = None

    def __post_init__(self) -> None:
        if (self.media_preview is None) != (self.media_transport is None):
            raise ValueError(
                "HTTP media preview deployment and durable transport must be supplied together"
            )

    def build_capture(self, *, bootstrap_at: datetime | None) -> HttpV2CaptureHost:
        return build_http_v2_capture_host(
            settings=self.settings,
            bootstrap_at=bootstrap_at,
            media_preview=self.media_preview,
            media_transport=self.media_transport,
        )


@asynccontextmanager
async def lifespan(_app: FastAPI):
    yield
    capture = _existing_http_v2_capture(_app)
    if capture is not None:
        await capture.aclose()


# Route declarations below are collected first and split into two explicit
# deployment compositions after the final handler is registered.  The public
# ``app`` binding is rebound to the V2-only composition at module completion;
# the catalog itself never escapes as a supported server entry point.
app = FastAPI(title="Girl Agent Companion Daemon route catalog", lifespan=lifespan)
app.state.dashboard_session_secret = secrets.token_bytes(32)
app.mount(
    "/assets", StaticFiles(directory=Path(__file__).resolve().parents[2] / "assets"), name="assets"
)
app.mount(
    "/dashboard-static",
    StaticFiles(directory=Path(__file__).resolve().parent / "static"),
    name="dashboard-static",
)
# The pixel-home prototype is mounted as-is (no copy) so the room keeps
# receiving its own workstream's updates; it serves only public art/JS.
app.mount(
    "/pixel-home",
    StaticFiles(directory=Path(__file__).resolve().parents[2] / "prototypes" / "pixel-home"),
    name="pixel-home",
)


http_v2_capture: HttpV2CaptureHost | None = None

# Building a production capture may perform a cold, immutable ledger replay.
# The replay is allowed to continue in its single warmup task, but an ingress
# request must not sit on the uvloop thread (or hold an HTTP connection) for
# minutes waiting for it.  A caller that arrives during this bounded window
# receives a retryable 503; no observation is written until a capture exists,
# so retrying the same message id cannot lose or duplicate an event.
_HTTP_V2_MESSAGE_READY_WAIT_SECONDS = 5.0


class HttpV2NotReady(RuntimeError):
    """The World-v2 capture is still warming and no ingress was accepted."""


def _existing_http_v2_capture(asgi_app: FastAPI) -> HttpV2CaptureHost | None:
    # Preserve the original module-level replacement seam for the shipped
    # singleton app (and its tests/operators).  Isolated factory apps own
    # their capture exclusively through ``app.state``.
    if asgi_app is app:
        return http_v2_capture
    return getattr(asgi_app.state, "http_v2_capture", None)


def _http_v2_capture(
    *,
    asgi_app: FastAPI | None = None,
    bootstrap_at: datetime | None = None,
) -> HttpV2CaptureHost:
    global http_v2_capture
    target_app = asgi_app or app
    existing = _existing_http_v2_capture(target_app)
    if existing is not None:
        return existing
    started = time.perf_counter()
    _LOG.warning("HTTP World v2 capture build started bootstrap_at=%s", bootstrap_at)
    deployment = getattr(target_app.state, "http_v2_deployment", None)
    capture = (
        deployment.build_capture(bootstrap_at=bootstrap_at)
        if isinstance(deployment, HttpV2ASGIDeployment)
        else build_http_v2_capture_host(settings=get_settings(), bootstrap_at=bootstrap_at)
    )
    target_app.state.http_v2_capture = capture
    if target_app is app:
        http_v2_capture = capture
    _LOG.warning(
        "HTTP World v2 capture build completed bootstrap_at=%s duration_ms=%.1f",
        bootstrap_at,
        (time.perf_counter() - started) * 1000,
    )
    return capture


def _schedule_http_v2_warmup(*, asgi_app: FastAPI) -> None:
    """Start one shared off-loop capture build for readiness/first ingress."""

    if _existing_http_v2_capture(asgi_app) is not None:
        return
    existing_task = getattr(asgi_app.state, "http_v2_warmup_task", None)
    if existing_task is not None and not existing_task.done():
        return
    task = asyncio.create_task(asyncio.to_thread(_http_v2_capture, asgi_app=asgi_app))

    def report_warmup_failure(completed: asyncio.Task[object]) -> None:
        if not completed.cancelled():
            error = completed.exception()
            if error is not None:
                _LOG.error("HTTP World v2 capture warmup failed", exc_info=error)

    task.add_done_callback(report_warmup_failure)
    asgi_app.state.http_v2_warmup_task = task


async def _http_v2_capture_async(
    *,
    asgi_app: FastAPI,
    bootstrap_at: datetime | None = None,
    wait_timeout_seconds: float | None = None,
) -> HttpV2CaptureHost:
    """Await readiness-triggered warmup without blocking uvloop."""

    existing = _existing_http_v2_capture(asgi_app)
    if existing is not None:
        return existing
    task = getattr(asgi_app.state, "http_v2_warmup_task", None)
    if task is None or task.done():
        task = asyncio.create_task(
            asyncio.to_thread(_http_v2_capture, asgi_app=asgi_app, bootstrap_at=bootstrap_at)
        )
        asgi_app.state.http_v2_warmup_task = task
    # ``shield`` is essential: timing out the caller must not cancel the
    # process-wide warmup task.  The worker continues its immutable replay and
    # the next retry joins the same single-flight task.
    if wait_timeout_seconds is None:
        await task
    else:
        try:
            async with asyncio.timeout(wait_timeout_seconds):
                await asyncio.shield(task)
        except TimeoutError as exc:
            raise HttpV2NotReady("World-v2 capture is still warming") from exc
    return _existing_http_v2_capture(asgi_app) or _http_v2_capture(
        asgi_app=asgi_app, bootstrap_at=bootstrap_at
    )


def create_http_asgi_app(
    *,
    settings: Settings,
    media_preview: MediaPreviewDeployment | None = None,
    media_transport: MediaProviderTransport | None = None,
) -> FastAPI:
    """Create an isolated HTTP ASGI composition with explicit media authority.

    Supplying neither media dependency preserves the unavailable conductor;
    supplying a partial pair is rejected before the server can start.
    """

    deployment = HttpV2ASGIDeployment(
        settings=settings,
        media_preview=media_preview,
        media_transport=media_transport,
    )
    configured = FastAPI(
        title="Girl Agent Companion Daemon (World v2)",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    configured.router.routes.extend(app.router.routes)
    configured.exception_handlers.update(app.exception_handlers)
    configured.state.http_v2_deployment = deployment
    configured.state.http_v2_capture = None
    configured.state.dashboard_session_secret = secrets.token_bytes(32)
    return configured


def _http_v2_ingress_evidence(
    message: IncomingMessage,
) -> tuple[tuple[str, ...], dict[str, object]]:
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
        + str(item["attachment"].get("kind") or "unknown")
        + ":sha256:"
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


def _http_v2_settings(asgi_app: FastAPI) -> Settings:
    deployment = getattr(asgi_app.state, "http_v2_deployment", None)
    return deployment.settings if isinstance(deployment, HttpV2ASGIDeployment) else get_settings()


def _dashboard_session_codec(asgi_app: FastAPI) -> DashboardSessionCodec | None:
    token = (_http_v2_settings(asgi_app).delivery_reconciliation_token or "").strip()
    secret = getattr(asgi_app.state, "dashboard_session_secret", None)
    if not token or not isinstance(secret, bytes):
        return None
    return DashboardSessionCodec(operator_token=token, instance_secret=secret)


def _dashboard_session_is_valid(request: Request) -> bool:
    codec = _dashboard_session_codec(request.app)
    return codec is not None and codec.verify(request.cookies.get(DASHBOARD_SESSION_COOKIE))


_LOCAL_DASHBOARD_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def _is_local_dashboard_request(request: Request) -> bool:
    client = request.client
    host = client.host.strip().lower() if client is not None else ""
    return host in _LOCAL_DASHBOARD_HOSTS


def _require_world_v2_internal_access(token: str | None, *, asgi_app: FastAPI = app) -> None:
    """Gate scheduler/recovery controls behind the existing operator secret.

    A dedicated scheduler credential can replace this at deployment time; the
    v2 host deliberately starts disabled rather than exposing clock/action
    control to arbitrary HTTP clients.
    """

    configured = (_http_v2_settings(asgi_app).delivery_reconciliation_token or "").strip()
    if not configured:
        raise HTTPException(
            status_code=503,
            detail="World v2 internal scheduler is disabled until an operator token is configured",
        )
    if not token or not secrets.compare_digest(token, configured):
        raise HTTPException(status_code=403, detail="invalid World v2 internal scheduler token")


def _require_world_v2_dashboard_access(request: Request, token: str | None) -> None:
    if _dashboard_session_is_valid(request):
        return
    _require_world_v2_internal_access(token, asgi_app=request.app)


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


@app.get("/health")
async def health(request: Request) -> dict[str, str]:
    # The first World-v2 capture build performs a one-time immutable ledger
    # verification.  Do it behind the readiness probe, off the uvloop thread,
    # so the first real chat does not unexpectedly pay the cold-start cost.
    if _existing_http_v2_capture(request.app) is None:
        _schedule_http_v2_warmup(asgi_app=request.app)
    return {"status": "ok"}


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request) -> HTMLResponse:
    # The daemon panel is a local operator surface.  Keep the legacy session
    # gate for non-loopback deployments so the page cannot expose live memory,
    # mood, and life-state data when the daemon is bound beyond localhost.
    if not _is_local_dashboard_request(request) and not _dashboard_session_is_valid(request):
        return HTMLResponse(LOGIN_HTML, headers={"Cache-Control": "no-store"})
    if _existing_http_v2_capture(request.app) is None:
        return HTMLResponse(
            UNAVAILABLE_HTML,
            status_code=503,
            headers={"Cache-Control": "no-store"},
        )
    return HTMLResponse(DASHBOARD_HTML, headers={"Cache-Control": "no-store"})


@app.post("/world-v2/dashboard/session")
async def world_v2_dashboard_login(request: Request) -> Response:
    codec = _dashboard_session_codec(request.app)
    if codec is None:
        return HTMLResponse(
            UNAVAILABLE_HTML,
            status_code=503,
            headers={"Cache-Control": "no-store"},
        )
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != (
        "application/x-www-form-urlencoded"
    ):
        return HTMLResponse(LOGIN_HTML, status_code=415, headers={"Cache-Control": "no-store"})
    body = await request.body()
    if len(body) > 4096:
        return HTMLResponse(LOGIN_HTML, status_code=413, headers={"Cache-Control": "no-store"})
    try:
        submitted = parse_qs(
            body.decode("utf-8"),
            keep_blank_values=True,
            strict_parsing=True,
        ).get("operator_token", [""])[0]
    except (UnicodeDecodeError, ValueError):
        submitted = ""
    configured = (_http_v2_settings(request.app).delivery_reconciliation_token or "").strip()
    if not submitted or not secrets.compare_digest(submitted, configured):
        return HTMLResponse(LOGIN_HTML, status_code=401, headers={"Cache-Control": "no-store"})
    response = Response(
        status_code=303, headers={"Location": "/dashboard", "Cache-Control": "no-store"}
    )
    response.set_cookie(
        DASHBOARD_SESSION_COOKIE,
        codec.issue(),
        max_age=DASHBOARD_SESSION_TTL_SECONDS,
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="strict",
        path="/",
    )
    return response


@app.post("/world-v2/dashboard/logout")
def world_v2_dashboard_logout() -> Response:
    response = Response(
        status_code=303, headers={"Location": "/dashboard", "Cache-Control": "no-store"}
    )
    response.delete_cookie(DASHBOARD_SESSION_COOKIE, path="/", httponly=True, samesite="strict")
    return response


@app.get("/world-v2/dashboard/app.js")
def world_v2_dashboard_script() -> Response:
    return Response(
        DASHBOARD_APP_JS,
        media_type="application/javascript",
        headers={"Cache-Control": "public, max-age=300"},
    )


@app.post("/messages", response_model=None)
async def post_message(
    message: IncomingMessage,
    request: Request,
) -> CompanionReply | JSONResponse:
    if not message.text.strip() and not message.attachments:
        raise HTTPException(status_code=400, detail="text or attachment is required")
    if not message.message_id:
        raise HTTPException(
            status_code=400, detail="message_id is required for idempotent delivery"
        )
    attachment_refs, metadata = _http_v2_ingress_evidence(message)
    try:
        capture = await _http_v2_capture_async(
            asgi_app=request.app,
            bootstrap_at=message.sent_at,
            wait_timeout_seconds=_HTTP_V2_MESSAGE_READY_WAIT_SECONDS,
        )
        result = await capture.respond(
            platform=message.platform,
            platform_user_id=message.platform_user_id,
            platform_message_id=message.message_id,
            text=message.text.strip() or None,
            observed_at=message.sent_at,
            attachment_refs=attachment_refs,
            coalescing_metadata=metadata,
        )
        # Background appraisal/fact/proactive work is driven by the scheduler
        # or the explicit internal drain endpoint.  Starting it here would
        # let a durable SQLite drain contend with the next visible reply and
        # turn a warm chat into an unbounded Context wait.
    except IdempotencyConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except HttpV2NotReady as exc:
        # No capture means no ledger write occurred.  The client can safely
        # retry this exact message id after the warmup task completes.
        raise HTTPException(
            status_code=503,
            detail="World-v2 is warming; retry the same message id",
            headers={"Retry-After": "1"},
        ) from exc
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
        # Older injected capture seams (and third-party adapters) may not
        # expose the optional mood projection; preserve the route's
        # historical calm fallback without touching the ledger.
        mood=getattr(result, "mood", "calm"),
        text=result.text,
        text_parts=[result.text],
        world_action_id=result.action_id,
    )


@app.post("/internal/world-v2/tick")
async def world_v2_tick(
    request: WorldV2ClockTickRequest,
    http_request: Request,
    x_world_v2_internal_token: str | None = Header(default=None),
) -> dict[str, object]:
    """Scheduler-only logical clock ingress for the migrated HTTP v2 lane."""

    _require_world_v2_internal_access(x_world_v2_internal_token, asgi_app=http_request.app)
    try:
        status = await (
            await _http_v2_capture_async(
                asgi_app=http_request.app,
                bootstrap_at=request.logical_time_from,
            )
        ).tick(
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
    http_request: Request,
    x_world_v2_internal_token: str | None = Header(default=None),
) -> dict[str, list[str]]:
    """Run bounded capture Action/background recovery without an Engine call."""

    _require_world_v2_internal_access(x_world_v2_internal_token, asgi_app=http_request.app)
    result = await (await _http_v2_capture_async(asgi_app=http_request.app)).drain(
        max_action_units=request.max_action_units,
        max_background_units=request.max_background_units,
    )
    return {
        "action_statuses": list(result.action_statuses),
        "background_statuses": list(result.background_statuses),
    }


@app.get("/internal/world-v2/dashboard-room")
def world_v2_dashboard_room(
    request: Request,
    x_world_v2_internal_token: str | None = Header(default=None),
) -> dict[str, object]:
    """Return the operator-authorized, public-only World v2 room DTO.

    This is intentionally separate from the legacy dashboard overview.  Its
    fixed composition request can neither read private state nor ask the v2
    projection compiler for diagnostic/operator fields.
    """

    _require_world_v2_internal_access(
        x_world_v2_internal_token,
        asgi_app=request.app,
    )
    # A dashboard GET must not be the process that opens and bootstraps a
    # writable World v2 application.  The selected ingress/scheduler host
    # owns that lifecycle; until it exists this reader is deliberately
    # unavailable rather than turning a supposedly read-only request into a
    # WorldStarted/BudgetAccountConfigured commit.
    capture = _existing_http_v2_capture(request.app)
    if capture is None:
        raise HTTPException(
            status_code=503,
            detail="World v2 dashboard capture is unavailable until the platform host is initialized",
        )
    try:
        return capture.dashboard_room().to_payload()
    except PermissionError as exc:
        # A composition/configuration error must not downgrade into a legacy
        # overview or return an unredacted diagnostic projection.
        raise HTTPException(status_code=403, detail="World v2 dashboard projection denied") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/world-v2/room")
def world_v2_public_room(request: Request) -> dict[str, object]:
    """Return the public-only Room DTO for a read-only room renderer.

    Unlike the internal dashboard endpoint this route is deliberately safe for
    a local Godot/browser renderer to poll without acquiring an operator
    credential.  It exposes exactly the same fixed ``room_renderer`` DTO: no
    raw world identifier, ledger data, affect, diagnostic view, or legacy
    dashboard state.  A cold reader never bootstraps a writable v2 host and a
    missing capture never falls back to the archived Engine.
    """

    capture = _existing_http_v2_capture(request.app)
    if capture is None:
        raise HTTPException(
            status_code=503,
            detail="World v2 room projection is unavailable until the platform host is initialized",
        )
    try:
        return capture.dashboard_room().to_payload()
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail="World v2 room projection denied") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/world-v2/life-state")
async def world_v2_life_state() -> dict[str, object]:
    """Read the QQ world's factual life state through its owning adapter.

    The QQ C2C world ledger is owned by the adapter process, so the dashboard
    must not open a second writable ledger handle here.  This endpoint only
    relays the adapter's already-redacted read-only health projection; when
    the adapter is down the panel honestly reports that instead of inventing
    a life state from the archived engine.
    """

    import httpx

    settings = get_settings()
    url = settings.qq_c2c_adapter_url.rstrip("/") + "/health"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(url)
            response.raise_for_status()
            payload = response.json()
    except Exception as exc:
        raise HTTPException(
            status_code=503, detail=f"QQ world adapter is unreachable: {type(exc).__name__}"
        ) from exc
    scheduler = payload.get("scheduler") if isinstance(payload, dict) else None
    if not isinstance(scheduler, dict):
        raise HTTPException(status_code=502, detail="QQ world adapter returned no scheduler state")
    return {
        "adapter_status": scheduler.get("status"),
        "world_activity": scheduler.get("world_activity", {}),
        "mechanisms": scheduler.get("mechanisms", {}),
        "initiative": scheduler.get("initiative", {}),
    }


@app.get("/world-v2/dashboard")
def world_v2_dashboard_public(
    request: Request,
    if_none_match: str | None = Header(default=None),
    x_world_v2_internal_token: str | None = Header(default=None),
) -> Response:
    """Read the fixed public Dashboard DTO without bootstrapping World v2.

    This is operator-gated during the staged browser cutover.  Authentication
    grants only access to this already-redacted DTO; request parameters never
    choose world, cursor, viewer policy, or diagnostic permissions.
    """

    _require_world_v2_dashboard_access(request, x_world_v2_internal_token)
    capture = _existing_http_v2_capture(request.app)
    if capture is None:
        raise HTTPException(
            status_code=503,
            detail="World v2 dashboard projection is unavailable until the platform host is initialized",
        )
    try:
        payload = capture.dashboard_public().to_payload()
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail="World v2 dashboard projection denied") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    etag = f'"{payload["projection_hash"]}"'
    headers = {"Cache-Control": "no-store", "ETag": etag}
    if if_none_match == etag:
        return Response(status_code=304, headers=headers)
    return JSONResponse(content=payload, headers=headers)


_DEFAULT_V2_EXACT_PATHS = frozenset(
    {
        "/health",
        "/dashboard",
        "/messages",
        "/assets",
        "/dashboard-static",
        "/pixel-home",
    }
)
_DEFAULT_V2_PREFIXES = ("/world-v2/", "/internal/world-v2/")


def _is_default_v2_route_path(path: str) -> bool:
    return path in _DEFAULT_V2_EXACT_PATHS or any(
        path.startswith(prefix) for prefix in _DEFAULT_V2_PREFIXES
    )


def _compose_asgi_app(
    *,
    source: FastAPI,
    title: str,
    include_default_v2_routes: bool,
) -> FastAPI:
    """Build the deployed ASGI graph from the declaration catalog.

    The archive composition was retired with the legacy runtime; every
    registered route must now pass the explicit World-v2 allowlist.  Keeping
    the filter (and this composition seam) means a route added outside the
    allowlist stays out of the deployed app instead of shipping by accident.
    """

    configured = FastAPI(
        title=title,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    configured.router.routes.extend(
        route
        for route in source.router.routes
        if _is_default_v2_route_path(str(getattr(route, "path", ""))) is include_default_v2_routes
    )
    configured.exception_handlers.update(source.exception_handlers)
    configured.state.http_v2_capture = None
    configured.state.dashboard_session_secret = secrets.token_bytes(32)
    return configured


# Freeze the declaration catalog before rebinding the supported ASGI entry.
_route_catalog_app = app
app = _compose_asgi_app(
    source=_route_catalog_app,
    title="Girl Agent Companion Daemon (World v2)",
    include_default_v2_routes=True,
)


def main() -> None:
    import uvicorn

    settings = get_settings()
    uvicorn.run("companion_daemon.app:app", host=settings.host, port=settings.port, reload=False)

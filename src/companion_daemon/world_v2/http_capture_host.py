"""HTTP capture adapter for the production World v2 platform lane.

This module is deliberately a platform composition boundary.  It normalizes
the local HTTP simulator's input, asks :class:`WorldV2PlatformHost` to ingest
it, and captures an already-authorized Action receipt.  It has no dependency
on the legacy Engine, WorldKernel, CompanionTurn, or their storage.

The HTTP transport is an intentionally local/debug transport: a successful
dispatch means that this process captured the immutable payload and recorded
its receipt.  It does *not* claim that QQ or any other remote platform
delivered the message.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import logging
from pathlib import Path
import secrets
import time
from typing import Final

from companion_daemon.config import Settings
from .affect_chat_model_adapter import AffectDraftDeliberationAdapter
from .relationship_draft_deliberation_adapter import RelationshipDraftDeliberationAdapter
from .chat_model_deliberation_adapter import ChatCompletionModel
from .deliberation import DeliberationModelAdapter
from .perception_executor import PerceptionTransport
from .perception_input_source import PerceptionInputSource
from .platform_action_executor import (
    MediaProviderTransport,
    PlatformDispatchReceipt,
    PlatformDispatchRequest,
)
from .platform_host import PlatformClockTick, PlatformInbound, WorldV2PlatformHost
from .dashboard_projection_adapter import (
    DashboardProjectionAdapter,
    DashboardPublicProjectionAdapter,
    DashboardPublicProjectionDTO,
    DashboardPublicRouteCatalog,
    DashboardRoomProjectionDTO,
    DashboardRoomRouteCatalog,
)
from .projection import (
    AuthenticatedProjectionPrincipal,
    ProjectionAuthority,
    ProjectionCapabilityIssuer,
    ProjectionGrant,
)
from .production_turn_application import (
    LifeEcologyComposition,
    MediaPreviewDeployment,
    WorldV2TurnApplicationConfig,
    build_sqlite_world_v2_turn_application,
)
from .schemas import ProjectionRequest
from .semantic_chat_composition import (
    SemanticChatComposition,
    build_semantic_chat_composition,
)


_LOG = logging.getLogger(__name__)


_DASHBOARD_VIEWER_ID: Final = "dashboard:http-v2-room"
_DASHBOARD_PUBLIC_VIEWER_ID: Final = "dashboard:http-v2-public"


class _HttpDashboardPrincipalVerifier:
    """Authenticate the composition-only room reader, never an HTTP caller.

    HTTP operator authentication happens in ``app.py``.  This smaller
    credential merely prevents a platform adapter from manufacturing a signed
    projection request after it receives the host object.
    """

    def __init__(self, *, world_id: str, principal_id: str = _DASHBOARD_VIEWER_ID) -> None:
        self._world_id = world_id
        self._principal_id = principal_id
        self._credential = object()

    @property
    def credential(self) -> object:
        return self._credential

    def authenticate(self, credential: object) -> AuthenticatedProjectionPrincipal:
        if credential is not self._credential:
            raise PermissionError("dashboard projection credential is not composition-owned")
        return AuthenticatedProjectionPrincipal(
            principal_id=self._principal_id,
            world_id=self._world_id,
            authentication_context="world-v2:http-dashboard-composition.1",
        )


class _HttpDashboardRequestIssuer:
    """Mint exactly one fixed viewer capability owned by HTTP composition."""

    def __init__(
        self,
        *,
        world_id: str,
        issuer: ProjectionCapabilityIssuer,
        credential: object,
        viewer_id: str,
        viewer_kind: str,
        redaction_policy: str,
    ) -> None:
        self._world_id = world_id
        self._issuer = issuer
        self._credential = credential
        self._viewer_id = viewer_id
        self._viewer_kind = viewer_kind
        self._redaction_policy = redaction_policy

    def issue(self) -> ProjectionRequest:
        nonce = secrets.token_hex(16)
        request = ProjectionRequest(
            schema_version="world-v2.1",
            request_id=f"request:http-v2-{self._viewer_kind}:{nonce}",
            world_id=self._world_id,
            viewer_kind=self._viewer_kind,
            viewer_id=self._viewer_id,
            permissions=frozenset(),
            trace_id=f"trace:http-v2-{self._viewer_kind}:{nonce}",
            redaction_policy=self._redaction_policy,
        )
        return self._issuer.bind(request, credential=self._credential)


class HttpCaptureIdentityResolver:
    """Resolve the one configured local HTTP simulator identity.

    The v2 composition currently owns one companion/user relationship.  A
    second HTTP user must get a separate v2 world composition rather than be
    silently mapped to the configured user's facts and relationship.
    """

    def __init__(self, *, primary_user_id: str) -> None:
        if not primary_user_id:
            raise ValueError("primary_user_id is required for HTTP capture")
        self._primary_user_id = primary_user_id

    def resolve(self, *, platform: str, platform_user_id: str) -> tuple[str, str]:
        if not platform or not platform_user_id:
            raise ValueError("HTTP capture requires a platform and platform user id")
        if platform_user_id != self._primary_user_id:
            raise ValueError("HTTP capture user is not configured for this World v2 host")
        reference = f"user:{self._primary_user_id}"
        return reference, reference


class HttpCaptureTransport:
    """Idempotently capture local HTTP delivery receipts by Action identity."""

    provider = "http:capture"

    def __init__(self) -> None:
        self._receipts: dict[str, PlatformDispatchReceipt] = {}
        self._bodies_by_action: dict[str, str] = {}

    async def send(self, request: PlatformDispatchRequest) -> PlatformDispatchReceipt:
        # The local HTTP capture is also the durable observation point for
        # scheduler-created follow-ups/proactive messages.  They are still
        # text-only in this transport, but rejecting them merely because they
        # did not originate from an inbound turn would strand a successfully
        # authorized initiative in a terminal capability failure.  Reactions
        # and media remain explicit unsupported capabilities.
        if request.kind not in {"reply", "followup", "proactive_message"} or request.content_type != "text/plain":
            identity = hashlib.sha256(request.fingerprint.encode("utf-8")).hexdigest()
            return PlatformDispatchReceipt(
                provider_receipt_id=f"receipt:http-capture:unsupported:{identity}",
                provider_ref=f"http-capture:unsupported:{identity}",
                status="failed",
                error_class="http_capture_capability_unavailable",
                received_at=datetime.now(UTC),
                raw_payload_hash="sha256:" + hashlib.sha256(request.body.encode("utf-8")).hexdigest(),
                idempotency_key=request.idempotency_key,
                request_fingerprint=request.fingerprint,
            )
        existing = self._receipts.get(request.idempotency_key)
        if existing is not None:
            if existing.request_fingerprint != request.fingerprint:
                raise ValueError("HTTP capture idempotency key conflicts with the original payload")
            return existing
        identity = hashlib.sha256(request.fingerprint.encode("utf-8")).hexdigest()
        receipt = PlatformDispatchReceipt(
            provider_receipt_id=f"receipt:http-capture:{identity}",
            provider_ref=f"http-capture:{identity}",
            status="delivered",
            received_at=datetime.now(UTC),
            raw_payload_hash="sha256:" + hashlib.sha256(request.body.encode("utf-8")).hexdigest(),
            idempotency_key=request.idempotency_key,
            request_fingerprint=request.fingerprint,
        )
        self._receipts[request.idempotency_key] = receipt
        self._bodies_by_action[request.action_id] = request.body
        return receipt

    async def lookup(
        self, *, idempotency_key: str, request_fingerprint: str
    ) -> PlatformDispatchReceipt | None:
        receipt = self._receipts.get(idempotency_key)
        if receipt is not None and receipt.request_fingerprint != request_fingerprint:
            raise ValueError("HTTP capture lookup fingerprint conflicts with the original dispatch")
        return receipt

    def captured_body(self, action_id: str | None) -> str | None:
        return self._bodies_by_action.get(action_id or "")


@dataclass(frozen=True, slots=True)
class HttpCaptureResult:
    """The bounded HTTP response projection of one v2 ingress attempt."""

    status: str
    action_id: str | None
    text: str | None
    canonical_user_id: str
    mood: str = "calm"


@dataclass(frozen=True, slots=True)
class HttpDrainResult:
    action_statuses: tuple[str, ...]
    background_statuses: tuple[str, ...]


class HttpV2CaptureHost:
    """A small HTTP-facing facade over a clean platform-neutral v2 host."""

    def __init__(
        self,
        *,
        host: WorldV2PlatformHost,
        transport: HttpCaptureTransport,
        primary_user_id: str,
        dashboard_request_issuer: _HttpDashboardRequestIssuer | None = None,
        dashboard_public_request_issuer: _HttpDashboardRequestIssuer | None = None,
        semantic_chat: SemanticChatComposition | None = None,
    ) -> None:
        if not primary_user_id:
            raise ValueError("primary_user_id is required")
        self._host = host
        self._transport = transport
        self._primary_user_id = primary_user_id
        self._dashboard_request_issuer = dashboard_request_issuer
        self._dashboard_public_request_issuer = dashboard_public_request_issuer
        self._semantic_chat = semantic_chat
        self._lock = asyncio.Lock()
        # A local capture transport knows the visible text as soon as the
        # provider dispatch call returns, while the durable Action settlement
        # still has several ledger transitions left to write.  Keep those
        # targeted drains alive so the HTTP caller can receive the captured
        # body at first visibility without allowing a second ingress/tick to
        # race the same single-writer ledger.  The next serialized operation
        # joins any pending drains before advancing the world again.
        self._pending_targeted_drains: set[asyncio.Task[object]] = set()
        self._background_drain_task: asyncio.Task[object] | None = None
        self._wal_maintenance_task: asyncio.Task[object] | None = None
        self._closed = False

    async def _join_pending_targeted_drains(self) -> None:
        pending = tuple(self._pending_targeted_drains)
        if not pending:
            return
        results = await asyncio.gather(*pending, return_exceptions=True)
        self._pending_targeted_drains.difference_update(pending)
        for result in results:
            if isinstance(result, BaseException):
                _LOG.error("HTTP capture targeted Action settlement failed", exc_info=result)

    def _start_targeted_drain(self, action_id: str) -> asyncio.Task[object]:
        task = asyncio.create_task(self._host.drain_action(action_id))
        self._pending_targeted_drains.add(task)
        return task

    def schedule_background_drain(
        self, *, max_action_units: int = 0, max_background_units: int = 1
    ) -> None:
        """Request one non-blocking cognitive upkeep pass after visible reply.

        A real scheduler may still call :meth:`drain` with a larger budget.
        This tiny coalesced pass prevents an HTTP-only deployment from
        accumulating one open fact/appraisal/proactive trigger per message
        when no external scheduler is running, without putting that work on
        the response critical path.
        """

        task = self._background_drain_task
        if task is not None and not task.done():
            return
        self._background_drain_task = asyncio.create_task(
            self.drain(
                max_action_units=max_action_units,
                max_background_units=max_background_units,
            )
        )

    async def _join_background_drain(self) -> None:
        task = self._background_drain_task
        if task is None:
            return
        self._background_drain_task = None
        result = await asyncio.gather(task, return_exceptions=True)
        if result and isinstance(result[0], BaseException):
            _LOG.error("HTTP capture background drain failed", exc_info=result[0])

    def _schedule_wal_maintenance(self) -> None:
        """Coalesce one passive WAL checkpoint onto the scheduler lane."""

        if not callable(getattr(self._host, "maintain_wal_once", None)):
            return
        task = self._wal_maintenance_task
        if task is not None and not task.done():
            return

        async def run() -> None:
            try:
                # Do not acquire the Host lock here: a visible ingress must
                # never queue behind maintenance.  The ledger's non-blocking
                # writer lock makes an active commit win and lets the next
                # scheduler wake retry.  The SQLite operation itself runs in
                # a worker thread, so it cannot block the event loop.
                result = await self._host.maintain_wal_once()
                if result is not None and getattr(result, "status", "skipped") != "skipped":
                    _LOG.info(
                        "world v2 WAL maintenance status=%s before_bytes=%s after_bytes=%s log_frames=%s checkpointed_frames=%s",
                        result.status,
                        result.wal_bytes_before,
                        result.wal_bytes_after,
                        result.log_frames,
                        result.checkpointed_frames,
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                _LOG.exception("world v2 WAL maintenance failed")

        self._wal_maintenance_task = asyncio.create_task(run())

    async def _join_wal_maintenance(self) -> None:
        task = self._wal_maintenance_task
        if task is None:
            return
        result = await asyncio.gather(task, return_exceptions=True)
        if result and isinstance(result[0], BaseException):
            _LOG.error("HTTP capture WAL maintenance failed", exc_info=result[0])

    async def respond(
        self,
        *,
        platform: str,
        platform_user_id: str,
        platform_message_id: str,
        text: str | None,
        observed_at: datetime,
        attachment_refs: tuple[str, ...] = (),
        coalescing_metadata: dict[str, object] | None = None,
    ) -> HttpCaptureResult:
        """Ingest exactly one HTTP event, then advance one visible Action.

        The lock is process-local serialization for this capture transport. It
        avoids letting two simultaneous HTTP requests race the same v2
        ActionPump while the ledger remains the cross-process authority.
        """

        inbound = PlatformInbound(
            platform=platform,
            platform_user_id=platform_user_id,
            platform_message_id=platform_message_id,
            text=text,
            observed_at=observed_at,
            trace_id=f"trace:http-v2:{platform}:{platform_user_id}:{platform_message_id}",
            attachment_refs=attachment_refs,
            coalescing_metadata=coalescing_metadata,
        )
        async with self._lock:
            await self._join_pending_targeted_drains()
            started = time.perf_counter()
            outcome = await self._host.inbound(inbound)
            after_inbound = time.perf_counter()
            action_id = next(
                iter((*outcome.authorized_action_ids, *outcome.scheduled_action_ids)), None
            )
            delivery = None
            drain_task: asyncio.Task[object] | None = None
            if action_id is not None:
                drain_task = self._start_targeted_drain(action_id)
                # The capture transport records the immutable visible body in
                # ``send`` before the Action's terminal settlement batch.  A
                # real provider adapter may therefore return the body while
                # the durable receipt work continues in the background.  If a
                # test/provider cannot expose an early body, retain the old
                # fully awaited behavior.
                while not drain_task.done():
                    if self._transport.captured_body(action_id) is not None:
                        break
                    await asyncio.sleep(0.01)
                if self._transport.captured_body(action_id) is None:
                    delivery = await drain_task
            after_drain = time.perf_counter()
            if delivery is not None and delivery.action_id not in {None, action_id}:
                raise RuntimeError("targeted HTTP capture drain returned a different Action")
            visible_mood = getattr(self._host, "visible_mood", None)
            mood = str(visible_mood()) if callable(visible_mood) else "calm"
            _LOG.warning(
                "http v2 response phases trace=%s action=%s inbound_ms=%.1f drain_ms=%.1f total_ms=%.1f status=%s",
                inbound.trace_id,
                action_id,
                (after_inbound - started) * 1000,
                (after_drain - after_inbound) * 1000,
                (time.perf_counter() - started) * 1000,
                outcome.status,
            )
            return HttpCaptureResult(
                status=outcome.status,
                action_id=action_id,
                text=self._transport.captured_body(action_id),
                canonical_user_id=self._primary_user_id,
                mood=mood,
            )

    async def tick(
        self,
        *,
        tick_id: str,
        logical_time_from: datetime,
        logical_time_to: datetime,
        observed_at: datetime,
        trace_id: str,
        causation_id: str,
        correlation_id: str,
        reason: str,
        policy_version: str | None = None,
        policy_digest: str | None = None,
    ) -> str:
        async with self._lock:
            await self._join_pending_targeted_drains()
            outcome = await self._host.tick(
                PlatformClockTick(
                    tick_id=tick_id,
                    logical_time_from=logical_time_from,
                    logical_time_to=logical_time_to,
                    observed_at=observed_at,
                    trace_id=trace_id,
                    causation_id=causation_id,
                    correlation_id=correlation_id,
                    reason=reason,
                    policy_version=policy_version,
                    policy_digest=policy_digest,
                )
            )
            return outcome.status

    async def drain(
        self, *, max_action_units: int = 8, max_background_units: int = 8
    ) -> HttpDrainResult:
        if not 0 <= max_action_units <= 64 or not 0 <= max_background_units <= 64:
            raise ValueError("HTTP capture drain limits must be between 0 and 64")
        # Joining a targeted Action and running model-backed background work
        # can both take an unbounded provider round trip.  Runtime-level
        # claims/CAS make these operations safe to run beside ingress; the
        # adapter lock must not turn a passive scheduler into a visible-chat
        # outage.  Inbound/tick still join pending targeted drains inside
        # their short serialization section before mutating the world.
        await self._join_pending_targeted_drains()
        drained = await self._host.drain_scheduled_work(
            max_action_units=max_action_units,
            max_background_units=max_background_units,
            media_preview_trace_id="trace:http-v2:media-preview",
            media_preview_correlation_id=(
                f"correlation:http-v2:media-preview:{self._primary_user_id}"
            ),
        )
        # Checkpointing is deliberately scheduled after this bounded
        # scheduler pass.  The HTTP reply path never awaits this task.
        self._schedule_wal_maintenance()
        return HttpDrainResult(
            action_statuses=drained.action_statuses,
            background_statuses=drained.background_statuses,
        )

    def dashboard_room(self) -> DashboardRoomProjectionDTO:
        """Return the fixed, public-only Room DTO for the operator route.

        The caller cannot select a world, cursor, viewer kind, permission, or
        redaction policy.  Those values stay in the composition-owned request
        issuer so an HTTP reader never becomes a general ledger viewer.
        """

        if self._dashboard_request_issuer is None:
            raise RuntimeError("World v2 dashboard capture is not configured")
        return self._host.capture_dashboard_room(self._dashboard_request_issuer.issue())

    def dashboard_public(self) -> DashboardPublicProjectionDTO:
        """Return the fixed, separately authorized public Dashboard DTO."""

        if self._dashboard_public_request_issuer is None:
            raise RuntimeError("World v2 dashboard public capture is not configured")
        return self._host.capture_dashboard_public(self._dashboard_public_request_issuer.issue())

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._join_pending_targeted_drains()
        await self._join_background_drain()
        await self._join_wal_maintenance()
        self._host.close()
        if self._semantic_chat is not None:
            await self._semantic_chat.aclose()


def build_http_v2_capture_host(
    *,
    settings: Settings,
    bootstrap_at: datetime | None = None,
    model: ChatCompletionModel | None = None,
    thinking_model: ChatCompletionModel | None = None,
    advisory_model: ChatCompletionModel | None = None,
    media_transport: MediaProviderTransport | None = None,
    media_preview: MediaPreviewDeployment | None = None,
    perception_model: DeliberationModelAdapter | None = None,
    perception_input_source: PerceptionInputSource | None = None,
    perception_transport: PerceptionTransport | None = None,
    perception_budget_limit: int = 0,
) -> HttpV2CaptureHost:
    """Compose the HTTP v2 lane without granting it legacy media authority.

    ``media_transport`` is intentionally an explicit deployment-owned
    dependency.  A provider transport must persist idempotency-keyed result
    bytes and implement ``lookup_execution_result`` for render/inspection
    recovery before it is supplied here.  The legacy image-machine bridge is
    not such a transport: its in-process inspection cache cannot prove a
    result after restart.  Leaving this argument unset therefore preserves
    preview safety by making media provider Actions undispatchable instead of
    silently falling back to the legacy image path.
    """

    build_started = time.perf_counter()
    _LOG.warning("HTTP World v2 semantic composition started")
    semantic_chat = build_semantic_chat_composition(
        settings=settings,
        flash_model=model,
        thinking_model=thinking_model,
        advisory_model=advisory_model,
        model_id_prefix="http-v2",
    )
    _LOG.warning(
        "HTTP World v2 semantic composition ready duration_ms=%.1f",
        (time.perf_counter() - build_started) * 1000,
    )
    model = semantic_chat.flash_model
    background_model = semantic_chat.background_model
    primary_user_id = settings.primary_user_id
    transport = HttpCaptureTransport()
    world_id = f"world:companion-v2:{primary_user_id}"
    dashboard_principal = _HttpDashboardPrincipalVerifier(world_id=world_id)
    dashboard_public_principal = _HttpDashboardPrincipalVerifier(
        world_id=world_id, principal_id=_DASHBOARD_PUBLIC_VIEWER_ID
    )
    projection_authority = ProjectionAuthority(
        grants=(
            ProjectionGrant(
                world_id=world_id,
                viewer_id=_DASHBOARD_VIEWER_ID,
                viewer_kind="room_renderer",
                permissions=frozenset(),
                redaction_policy="room-public-v1",
            ),
            ProjectionGrant(
                world_id=world_id,
                viewer_id=_DASHBOARD_PUBLIC_VIEWER_ID,
                viewer_kind="dashboard_public",
                permissions=frozenset(),
                redaction_policy="dashboard-public-v1",
            ),
        )
    )
    dashboard_requests = _HttpDashboardRequestIssuer(
        world_id=world_id,
        issuer=ProjectionCapabilityIssuer(
            authority=projection_authority,
            principal_verifier=dashboard_principal,
        ),
        credential=dashboard_principal.credential,
        viewer_id=_DASHBOARD_VIEWER_ID,
        viewer_kind="room_renderer",
        redaction_policy="room-public-v1",
    )
    dashboard_public_requests = _HttpDashboardRequestIssuer(
        world_id=world_id,
        issuer=ProjectionCapabilityIssuer(
            authority=projection_authority,
            principal_verifier=dashboard_public_principal,
        ),
        credential=dashboard_public_principal.credential,
        viewer_id=_DASHBOARD_PUBLIC_VIEWER_ID,
        viewer_kind="dashboard_public",
        redaction_policy="dashboard-public-v1",
    )
    # Explicit test/operator database paths remain authoritative.  The
    # production `.env` HTTP split is only selected when the legacy default
    # archive path is still in effect; otherwise a fixture would accidentally
    # share the live room ledger merely because the process environment
    # contains WORLD_V2_HTTP_DATABASE_PATH.
    configured_http_path = settings.world_v2_http_database_path
    http_path = (
        configured_http_path
        if configured_http_path is not None
        and Path(settings.database_path) == Path("data/companion.sqlite")
        else settings.database_path
    )
    _LOG.warning("HTTP World v2 application composition started path=%s", http_path)
    application = build_sqlite_world_v2_turn_application(
        path=Path(http_path),
        config=WorldV2TurnApplicationConfig(
            world_id=world_id,
            companion_actor_ref="agent:companion",
            reply_target=f"user:{primary_user_id}",
            action_pump_owner="pump:http-v2-capture",
            life_ecology=LifeEcologyComposition.production_v1(),
            immediate_emotion_signal_gate=True,
            media_selection_acceptance=(
                media_preview.acceptance if media_preview is not None else None
            ),
            media_continuation=(
                media_preview.continuation if media_preview is not None else None
            ),
            perception_budget_limit=perception_budget_limit,
        ),
        identities=HttpCaptureIdentityResolver(primary_user_id=primary_user_id),
        router=semantic_chat.router,
        main_model=semantic_chat.main_model,
        quick_recovery=semantic_chat.main_model,
        transport=transport,
        media_transport=media_transport,
        media_planner=(media_preview.planner if media_preview is not None else None),
        advisory_compiler=semantic_chat.advisory_compiler,
        appraisal_model=semantic_chat.appraisal_model,
        affect_model=AffectDraftDeliberationAdapter(model=background_model),
        perception_model=perception_model,
        perception_input_source=perception_input_source,
        perception_transport=perception_transport,
        relationship_model=RelationshipDraftDeliberationAdapter(model=background_model),
        # World outcomes use a separate opaque-candidate selector.  The
        # adapter derives settlement bytes from pinned authority, so this is
        # not a generic chat reply pretending to be a life event.
        outcome_draft_model=background_model,
        # Fact/Memory run only on the durable background queue; wiring them
        # here preserves the interactive reply budget while allowing later
        # turns to retrieve accepted user facts.
        fact_model=background_model,
        private_impression_model=background_model,
        memory_model=background_model,
        proactive_model=background_model,
        # A scheduler-only, bounded selection over already legal activities.
        # Invalid provider output terminates the ecology wake fail-safe.
        activity_lifecycle_model=background_model,
        media_selection_model=(
            media_preview.selection_model if media_preview is not None else None
        ),
        projection_authority=projection_authority,
        # HTTP parsing happens before lazy composition.  Pinning the first
        # bootstrap to that already-observed ingress avoids rejecting the
        # process's very first message merely because it was parsed a few
        # milliseconds before the SQLite lane was opened.
        now=bootstrap_at or datetime.now(UTC),
    )
    _LOG.warning(
        "HTTP World v2 application composition ready duration_ms=%.1f",
        (time.perf_counter() - build_started) * 1000,
    )
    return HttpV2CaptureHost(
        host=WorldV2PlatformHost(
            application=application,
                dashboard_capture=DashboardProjectionAdapter(
                source=application,
                # These are renderer route names, not world facts.  Only
                # public labels represented by the shipped room are mapped;
                # all unknown/private labels stay on unavailable/idle.
                routes=DashboardRoomRouteCatalog(
                    location_routes={
                        "location:studio": "zhizhi-home-legacy",
                        "location:apartment": "zhizhi-home-legacy",
                    },
                    activity_routes={
                        "focused_work": "study",
                        "relax": "relax",
                    },
                    ),
                ),
                dashboard_public_capture=DashboardPublicProjectionAdapter(
                    source=application,
                    routes=DashboardPublicRouteCatalog(
                        room_routes=DashboardRoomRouteCatalog(
                            location_routes={
                                "location:studio": "zhizhi-home-legacy",
                                "location:apartment": "zhizhi-home-legacy",
                            },
                            activity_routes={"focused_work": "study", "relax": "relax"},
                        ),
                        activity_labels={"focused_work": "在看资料", "relax": "放松一下"},
                    ),
                ),
        ),
        transport=transport,
        primary_user_id=primary_user_id,
        dashboard_request_issuer=dashboard_requests,
        dashboard_public_request_issuer=dashboard_public_requests,
        semantic_chat=semantic_chat,
    )


__all__ = [
    "HttpCaptureIdentityResolver",
    "HttpCaptureResult",
    "HttpCaptureTransport",
    "HttpDrainResult",
    "HttpV2CaptureHost",
    "build_http_v2_capture_host",
]

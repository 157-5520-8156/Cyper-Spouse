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
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import logging
from pathlib import Path
import secrets
import time
from typing import Final

from companion_daemon.config import Settings
from .model_completion import ChatCompletionModel
from .expression_draft import PRODUCTION_TEXT_ONLY_EXPRESSION_CAPABILITIES
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
from .life_development_model_adapter import RoleBoundLifeDevelopmentModelAdapter
from .schemas import ProjectionRequest
from .recall_embedding import configured_recall_embedding
from .semantic_chat_composition import (
    SemanticChatComposition,
    build_semantic_chat_composition,
    unavailable_life_source_authority_health,
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
        if (
            request.kind not in {"reply", "followup", "proactive_message"}
            or request.content_type != "text/plain"
        ):
            identity = hashlib.sha256(request.fingerprint.encode("utf-8")).hexdigest()
            return PlatformDispatchReceipt(
                provider_receipt_id=f"receipt:http-capture:unsupported:{identity}",
                provider_ref=f"http-capture:unsupported:{identity}",
                status="failed",
                error_class="http_capture_capability_unavailable",
                received_at=datetime.now(UTC),
                raw_payload_hash="sha256:"
                + hashlib.sha256(request.body.encode("utf-8")).hexdigest(),
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
        # The facade lock is a clock-ordering mutex only.  Visible cognition and
        # provider handoff must stay outside it: WorldRuntime owns the short
        # Observation/CAS phase, and ActionPump owns exact-action effect-once.
        self._lock = asyncio.Lock()
        self._scheduled_work_lock = asyncio.Lock()
        # A local capture transport knows the visible text as soon as the
        # provider dispatch call returns, while the durable Action settlement
        # still has several ledger transitions left to write. Keep those exact
        # Action drains process-owned after the HTTP body becomes visible;
        # ActionPump's claim/CAS and provider idempotency are the concurrency
        # authority, so unrelated ingress and clock observations stay free.
        # Concurrent delivery retries for the same immutable Action join one
        # process-owned drain.  Different Action identities remain independent.
        self._pending_targeted_drains: dict[str, asyncio.Task[object]] = {}
        # A caller may abandon its HTTP wait while the character model is still
        # finishing.  Keep that exact response process-owned, both to preserve
        # durable recovery semantics and to give shutdown a complete task set.
        self._active_response_tasks: set[asyncio.Task[HttpCaptureResult]] = set()
        self._background_drain_task: asyncio.Task[object] | None = None
        self._wal_maintenance_task: asyncio.Task[object] | None = None
        self._deferred_semantic_close_task: asyncio.Task[None] | None = None
        self._close_task: asyncio.Task[None] | None = None
        self._closed = False

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeError("HTTP capture host is closing")

    def _track_response_task(
        self,
        task: asyncio.Task[HttpCaptureResult],
    ) -> asyncio.Task[HttpCaptureResult]:
        self._active_response_tasks.add(task)

        def finished(completed: asyncio.Task[HttpCaptureResult]) -> None:
            self._active_response_tasks.discard(completed)
            if not completed.cancelled():
                # Retrieve a late exception when the HTTP waiter was cancelled;
                # an ordinary waiter still receives the same exception.
                completed.exception()

        task.add_done_callback(finished)
        return task

    async def _join_pending_targeted_drains(self) -> None:
        pending = tuple(self._pending_targeted_drains.items())
        if not pending:
            return
        results = await asyncio.gather(
            *(task for _action_id, task in pending),
            return_exceptions=True,
        )
        for action_id, task in pending:
            if self._pending_targeted_drains.get(action_id) is task:
                self._pending_targeted_drains.pop(action_id, None)
        for result in results:
            if isinstance(result, BaseException):
                _LOG.error("HTTP capture targeted Action settlement failed", exc_info=result)

    def _start_targeted_drain(self, action_id: str) -> asyncio.Task[object]:
        existing = self._pending_targeted_drains.get(action_id)
        if existing is not None:
            return existing
        task = asyncio.create_task(
            self._host.drain_action(action_id),
            name=f"http-v2-targeted-action-drain:{action_id}",
        )
        self._pending_targeted_drains[action_id] = task

        def finished(completed: asyncio.Task[object]) -> None:
            if self._pending_targeted_drains.get(action_id) is completed:
                self._pending_targeted_drains.pop(action_id, None)
            if completed.cancelled():
                _LOG.error(
                    "HTTP capture targeted Action drain was cancelled action_id=%s",
                    action_id,
                )
                return
            error = completed.exception()
            if error is not None:
                _LOG.error(
                    "HTTP capture targeted Action drain failed action_id=%s error=%s",
                    action_id,
                    type(error).__name__,
                    exc_info=(type(error), error, error.__traceback__),
                )

        task.add_done_callback(finished)
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

        self._require_open()
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
        """Ingest exactly one HTTP event, then advance its visible Action.

        WorldRuntime serializes only the Observation/CAS phase and can
        supersede an older unanswered episode.  This facade therefore owns the
        response task but never holds its clock mutex across character/model or
        provider work.
        """

        self._require_open()
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
        task = self._track_response_task(
            asyncio.create_task(
                self._respond_owned(inbound),
                name=f"http-v2-visible-response:{platform_message_id}",
            )
        )
        return await asyncio.shield(task)

    async def _respond_owned(self, inbound: PlatformInbound) -> HttpCaptureResult:
        """Run one visible turn without an adapter-wide model mutex."""

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
            # ``send`` before the Action's terminal settlement batch.  A real
            # provider adapter may therefore return the body while durable
            # receipt work continues in the process-owned task.
            while not drain_task.done():
                if self._transport.captured_body(action_id) is not None:
                    break
                await asyncio.sleep(0.01)
            if self._transport.captured_body(action_id) is None:
                delivery = await asyncio.shield(drain_task)
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
        self._require_open()
        async with self._lock:
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
                    # Keep only the clock/deferred-event CAS inside the facade
                    # mutex. Life may span several provider attempts and runs
                    # below on the independent scheduler lane.
                    run_life_ecology=False,
                )
            )
        async with self._scheduled_work_lock:
            await self._advance_life_ecology_for_committed_tick(
                tick_id=tick_id,
                trace_id=trace_id,
                correlation_id=correlation_id,
            )
        return outcome.status

    async def _advance_life_ecology_for_committed_tick(
        self,
        *,
        tick_id: str,
        trace_id: str,
        correlation_id: str,
    ) -> object | None:
        """Run one exact Life wake without owning the HTTP clock mutex."""

        advance = getattr(self._host, "advance_life_ecology_once", None)
        if not callable(advance):
            # Production WorldV2PlatformHost always exposes the seam.  Narrow
            # clock-only integrations retain their previous compatibility.
            return None
        return await advance(
            wake_event_ref=f"event:trigger:clock:{tick_id}",
            trace_id=trace_id,
            correlation_id=correlation_id,
        )

    async def drain(
        self, *, max_action_units: int = 8, max_background_units: int = 8
    ) -> HttpDrainResult:
        self._require_open()
        if not 0 <= max_action_units <= 64 or not 0 <= max_background_units <= 64:
            raise ValueError("HTTP capture drain limits must be between 0 and 64")
        # Passive drains and Life share only this background lane. Neither
        # blocks visible ingress nor its exact targeted Action. Acquiring the
        # lane before the first await also lets close rendezvous with every
        # operation that passed the open gate.
        async with self._scheduled_work_lock:
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

    def proactive_source_authority_health(self) -> dict[str, object]:
        """Expose deployment authority without inspecting character decisions."""

        if self._semantic_chat is None:
            return {
                "status": "unavailable",
                "warning": True,
                "warning_reasons": ["proactive_source_authority.composition_unavailable"],
                "independent_reviewer": False,
                "fact_effects_available": False,
                "subjective_expression_available": False,
            }
        return self._semantic_chat.proactive_source_authority_health()

    def character_interior_health(self) -> dict[str, object]:
        """Expose the single protagonist-author topology without model work."""

        if self._semantic_chat is None:
            return {
                "status": "unavailable",
                "installed": False,
                "semantic_author_count": 0,
                "primary_author_model": None,
                "primary_author_route": None,
                "parallel_character_author_conflicts": 0,
                "legacy_interface_invocations": 0,
                "dual_write_conflicts": 0,
            }
        return self._semantic_chat.character_interior_health()

    def life_source_authority_health(self) -> dict[str, object]:
        """Expose isolated Life reviewer state without invoking a model."""

        if self._semantic_chat is None:
            return unavailable_life_source_authority_health()
        return self._semantic_chat.life_source_authority_health()

    async def aclose(self) -> None:
        close_task = self._close_task
        if close_task is None:
            # Publish one shielded owner before the first await. Concurrent or
            # cancelled lifecycle callers therefore join the same cleanup
            # instead of treating the early `_closed` gate as quiescence.
            self._closed = True
            close_task = asyncio.create_task(
                self._aclose_owned(),
                name="http-v2-capture-host-close",
            )
            self._close_task = close_task
        await asyncio.shield(close_task)

    async def _aclose_owned(self) -> None:
        responses = tuple(self._active_response_tasks)
        if responses:
            results = await asyncio.gather(*responses, return_exceptions=True)
            for result in results:
                if isinstance(result, BaseException):
                    _LOG.error(
                        "HTTP capture visible response failed during close",
                        exc_info=result,
                    )
        # A response may return at first visible body while its process-owned
        # Action settlement continues.  Snapshot this set only after every
        # response has finished, now that the closed gate prevents new work.
        await self._join_pending_targeted_drains()
        await self._join_background_drain()
        # ``tick`` takes the clock mutex before it can enter the scheduler lane;
        # ``drain`` takes the scheduler lane before its first await.  With the
        # task-creation gate already closed, these sequential rendezvous cover
        # every operation admitted before shutdown without nesting locks.
        async with self._lock:
            pass
        async with self._scheduled_work_lock:
            pass
        # An admitted drain schedules WAL maintenance immediately after leaving
        # the scheduler lane.  Join it only after the rendezvous so that late
        # task is included before the World/SQLite owner is closed.
        await self._join_wal_maintenance()
        close_world = getattr(self._host, "aclose", None)
        if callable(close_world):
            await close_world()
        else:
            self._host.close()
        if self._semantic_chat is not None:
            world_quiescence = getattr(
                self._host,
                "wait_for_shutdown_quiescence",
                None,
            )
            if (
                getattr(self._host, "shutdown_pending_task_count", 0) > 0
                and callable(world_quiescence)
            ):
                deferred = asyncio.create_task(
                    self._close_semantic_after_world_quiescence(world_quiescence),
                    name="http-v2-deferred-semantic-close",
                )
                self._deferred_semantic_close_task = deferred
                deferred.add_done_callback(self._observe_deferred_semantic_close)
            else:
                await self._semantic_chat.aclose()

    async def _close_semantic_after_world_quiescence(
        self,
        wait_for_world: Callable[[], Awaitable[None]],
    ) -> None:
        await wait_for_world()
        assert self._semantic_chat is not None
        await self._semantic_chat.aclose()

    @staticmethod
    def _observe_deferred_semantic_close(task: asyncio.Task[None]) -> None:
        if not task.cancelled():
            task.exception()

    @property
    def shutdown_pending_task_count(self) -> int:
        """Dependencies retained after the facade's bounded close."""

        deferred = self._deferred_semantic_close_task
        if deferred is not None and not deferred.done():
            return 1
        return int(
            getattr(self._host, "shutdown_pending_task_count", 0) > 0
            or (
                self._semantic_chat is not None
                and getattr(
                    self._semantic_chat,
                    "shutdown_pending_task_count",
                    0,
                )
                > 0
            )
        )

    async def wait_for_shutdown_quiescence(self) -> None:
        """Wait for deferred World and semantic dependency cleanup."""

        close_task = self._close_task
        if close_task is not None:
            await asyncio.shield(close_task)
        deferred = self._deferred_semantic_close_task
        if deferred is not None:
            await asyncio.shield(deferred)
        else:
            world_wait = getattr(self._host, "wait_for_shutdown_quiescence", None)
            if (
                getattr(self._host, "shutdown_pending_task_count", 0) > 0
                and callable(world_wait)
            ):
                await world_wait()
        semantic = self._semantic_chat
        semantic_wait = getattr(semantic, "wait_for_shutdown_quiescence", None)
        if (
            semantic is not None
            and getattr(semantic, "shutdown_pending_task_count", 0) > 0
            and callable(semantic_wait)
        ):
            await semantic_wait()


def build_http_v2_capture_host(
    *,
    settings: Settings,
    bootstrap_at: datetime | None = None,
    model: ChatCompletionModel | None = None,
    thinking_model: ChatCompletionModel | None = None,
    world_support_model: ChatCompletionModel | None = None,
    source_closure_model: ChatCompletionModel | None = None,
    life_source_closure_model: ChatCompletionModel | None = None,
    candidate_external_proposition_inventory_model: ChatCompletionModel | None = None,
    media_transport: MediaProviderTransport | None = None,
    media_preview: MediaPreviewDeployment | None = None,
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
        world_support_model=world_support_model,
        source_closure_model=source_closure_model,
        life_source_closure_model=life_source_closure_model,
        candidate_external_proposition_inventory_model=(
            candidate_external_proposition_inventory_model
        ),
        model_id_prefix="http-v2",
    )
    _LOG.warning(
        "HTTP World v2 semantic composition ready duration_ms=%.1f",
        (time.perf_counter() - build_started) * 1000,
    )
    background_model = semantic_chat.world_support_model
    life_world_author = RoleBoundLifeDevelopmentModelAdapter(
        model=background_model,
        role="world_author",
    )
    life_world_author_source_rewriter = RoleBoundLifeDevelopmentModelAdapter(
        model=background_model,
        role="world_author",
    )
    life_source_closure_reviewer = (
        RoleBoundLifeDevelopmentModelAdapter(
            model=semantic_chat.life_source_closure_model,
            role="world_author_source_reviewer",
        )
        if semantic_chat.life_source_closure_model is not None
        else None
    )
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
            expression_episode_mode=settings.world_v2_expression_episode_mode,
            expression_capabilities=PRODUCTION_TEXT_ONLY_EXPRESSION_CAPABILITIES,
            life_ecology=LifeEcologyComposition.production_v1(),
            media_selection_acceptance=(
                media_preview.acceptance if media_preview is not None else None
            ),
            media_continuation=(media_preview.continuation if media_preview is not None else None),
            perception_budget_limit=perception_budget_limit,
        ),
        identities=HttpCaptureIdentityResolver(primary_user_id=primary_user_id),
        router=semantic_chat.router,
        character_interior=semantic_chat.character_interior,
        semantic_recall_embedding=configured_recall_embedding(settings),
        transport=transport,
        media_transport=media_transport,
        media_planner=(media_preview.planner if media_preview is not None else None),
        perception_input_source=perception_input_source,
        perception_transport=perception_transport,
        # Fact/Memory run only on the durable background queue; wiring them
        # here preserves the interactive reply budget while allowing later
        # turns to retrieve accepted user facts.
        fact_model=background_model,
        proactive_source_closure_model=semantic_chat.proactive_source_closure_model,
        proactive_candidate_external_proposition_inventory_model=(
            semantic_chat.candidate_external_proposition_inventory_model
        ),
        # A scheduler-only, bounded selection over already legal activities.
        # Invalid provider output terminates the ecology wake fail-safe.
        npc_actor_model=background_model,
        life_world_author_model=life_world_author,
        life_world_author_source_rewriter=life_world_author_source_rewriter,
        life_source_closure_reviewer=life_source_closure_reviewer,
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

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
from pathlib import Path

from companion_daemon.config import Settings
from companion_daemon.llm import DeepSeekChatModel, FakeCompanionModel

from .affect_chat_model_adapter import AffectDraftDeliberationAdapter
from .appraisal_chat_model_adapter import AppraisalDraftDeliberationAdapter
from .chat_model_deliberation_adapter import ChatCompletionModel, RoutedChatModelDeliberationAdapter
from .deliberation import ModelRoute, RouteRequest
from .platform_action_executor import PlatformDispatchReceipt, PlatformDispatchRequest
from .platform_host import PlatformClockTick, PlatformInbound, WorldV2PlatformHost
from .production_turn_application import (
    WorldV2TurnApplicationConfig,
    build_sqlite_world_v2_turn_application,
)


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


class _HttpCaptureRouter:
    """Keep the HTTP hot path on Flash; thinking remains a separate route."""

    async def route(self, _request: RouteRequest) -> ModelRoute:
        return ModelRoute(
            tier="flash",
            reason_code="http_capture_hot_path",
            router_version="world-v2-http-capture-router.1",
        )


class HttpCaptureTransport:
    """Idempotently capture local HTTP delivery receipts by Action identity."""

    provider = "http:capture"

    def __init__(self) -> None:
        self._receipts: dict[str, PlatformDispatchReceipt] = {}
        self._bodies_by_action: dict[str, str] = {}

    async def send(self, request: PlatformDispatchRequest) -> PlatformDispatchReceipt:
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
        owned_model: DeepSeekChatModel | None = None,
    ) -> None:
        if not primary_user_id:
            raise ValueError("primary_user_id is required")
        self._host = host
        self._transport = transport
        self._primary_user_id = primary_user_id
        self._owned_model = owned_model
        self._lock = asyncio.Lock()
        self._closed = False

    async def respond(
        self,
        *,
        platform: str,
        platform_user_id: str,
        platform_message_id: str,
        text: str,
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
            outcome = await self._host.inbound(inbound)
            action_id = next(
                iter((*outcome.authorized_action_ids, *outcome.scheduled_action_ids)), None
            )
            delivery = (
                await self._host.drain_action(action_id) if action_id is not None else None
            )
            if delivery is not None and delivery.action_id not in {None, action_id}:
                raise RuntimeError("targeted HTTP capture drain returned a different Action")
            return HttpCaptureResult(
                status=outcome.status,
                action_id=action_id,
                text=self._transport.captured_body(action_id),
                canonical_user_id=self._primary_user_id,
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
        async with self._lock:
            actions: list[str] = []
            for _ in range(max_action_units):
                result = await self._host.drain_actions_once()
                if result is None or result.status == "idle":
                    break
                actions.append(result.status)
            background: list[str] = []
            for _ in range(max_background_units):
                result = await self._host.drain_background_once()
                if result is None:
                    break
                background.append(str(getattr(result, "work_status", "processed")))
            return HttpDrainResult(
                action_statuses=tuple(actions), background_statuses=tuple(background)
            )

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._host.close()
        if self._owned_model is not None:
            await self._owned_model.aclose()


def build_http_v2_capture_host(
    *,
    settings: Settings,
    bootstrap_at: datetime | None = None,
    model: ChatCompletionModel | None = None,
) -> HttpV2CaptureHost:
    """Compose the first real HTTP migration without touching the legacy Engine."""

    owned_model: DeepSeekChatModel | None = None
    if model is None:
        if settings.deepseek_api_key:
            model = DeepSeekChatModel(
                api_key=settings.deepseek_api_key,
                base_url=settings.deepseek_base_url,
                model=settings.deepseek_model,
                thinking_enabled=False,
            )
            owned_model = model
        else:
            model = FakeCompanionModel()
    adapter = RoutedChatModelDeliberationAdapter(
        flash_model=model,
        flash_model_id=str(getattr(model, "model", "http-v2-flash")),
    )
    primary_user_id = settings.primary_user_id
    transport = HttpCaptureTransport()
    application = build_sqlite_world_v2_turn_application(
        path=Path(settings.database_path),
        config=WorldV2TurnApplicationConfig(
            world_id=f"world:companion-v2:{primary_user_id}",
            companion_actor_ref="agent:companion",
            reply_target=f"user:{primary_user_id}",
            action_pump_owner="pump:http-v2-capture",
        ),
        identities=HttpCaptureIdentityResolver(primary_user_id=primary_user_id),
        router=_HttpCaptureRouter(),
        main_model=adapter,
        quick_recovery=adapter,
        transport=transport,
        appraisal_model=AppraisalDraftDeliberationAdapter(model=model),
        affect_model=AffectDraftDeliberationAdapter(model=model),
        # HTTP parsing happens before lazy composition.  Pinning the first
        # bootstrap to that already-observed ingress avoids rejecting the
        # process's very first message merely because it was parsed a few
        # milliseconds before the SQLite lane was opened.
        now=bootstrap_at or datetime.now(UTC),
    )
    return HttpV2CaptureHost(
        host=WorldV2PlatformHost(application=application),
        transport=transport,
        primary_user_id=primary_user_id,
        owned_model=owned_model,
    )


__all__ = [
    "HttpCaptureIdentityResolver",
    "HttpCaptureResult",
    "HttpCaptureTransport",
    "HttpDrainResult",
    "HttpV2CaptureHost",
    "build_http_v2_capture_host",
]

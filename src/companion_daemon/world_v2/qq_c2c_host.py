"""QQ/OneBot C2C text-only composition for the World v2 application lane.

This is deliberately not a compatibility layer around ``CompanionEngine`` or
``QQMessageCoalescer``.  A configured, single C2C recipient is mapped to one
World v2 reply target and all ingress, dispatch and restart recovery cross the
``WorldV2PlatformHost`` seam.  Group messages, attachments, stickers and media
are outside this first migration and must be rejected by the provider adapter
rather than silently falling back to a legacy write path.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from companion_daemon.config import Settings
from companion_daemon.llm import DeepSeekChatModel, FakeCompanionModel
from companion_daemon.qq_delivery import QQDelivery

from .affect_chat_model_adapter import AffectDraftDeliberationAdapter
from .appraisal_chat_model_adapter import AppraisalDraftDeliberationAdapter
from .chat_model_deliberation_adapter import ChatCompletionModel, RoutedChatModelDeliberationAdapter
from .deliberation import ModelRoute, RouteRequest
from .platform_host import PlatformClockTick, PlatformInbound, WorldV2PlatformHost
from .production_turn_application import (
    WorldV2TurnApplicationConfig,
    build_sqlite_world_v2_turn_application,
)
from .platform_action_executor import MediaProviderTransport
from .qq_c2c_transport import QQC2CDelivery, QQC2CPlatformTransport


class QQC2CIdentityResolver:
    """Resolve exactly one configured QQ C2C recipient into one v2 world."""

    def __init__(self, *, recipient_id: str, canonical_user_id: str) -> None:
        if not recipient_id or not canonical_user_id:
            raise ValueError("QQ C2C identity requires recipient and canonical user ids")
        self._recipient_id = recipient_id
        self._canonical_user_id = canonical_user_id

    def resolve(self, *, platform: str, platform_user_id: str) -> tuple[str, str]:
        if platform != "qq" or platform_user_id != self._recipient_id:
            raise ValueError("QQ C2C ingress is not configured for this World v2 host")
        return (
            f"user:{self._canonical_user_id}",
            qq_c2c_target(self._recipient_id),
        )


def qq_c2c_target(recipient_id: str) -> str:
    if not recipient_id:
        raise ValueError("QQ C2C recipient id is required")
    return f"conversation:qq:c2c:{recipient_id}"


class _QQC2CRouter:
    """Keep C2C text on flash; higher-cost work remains worker-owned."""

    async def route(self, _request: RouteRequest) -> ModelRoute:
        return ModelRoute(
            tier="flash",
            reason_code="qq_c2c_text_hot_path",
            router_version="world-v2-qq-c2c-router.1",
        )


@dataclass(frozen=True, slots=True)
class QQC2CIngressResult:
    status: str
    action_id: str | None
    canonical_user_id: str


@dataclass(frozen=True, slots=True)
class QQC2CDrainResult:
    action_statuses: tuple[str, ...]
    background_statuses: tuple[str, ...]


class QQC2CHost:
    """Small C2C-only facade over a durable :class:`WorldV2PlatformHost`.

    The process-local lock only serializes one adapter process.  The ledger
    remains the authority for duplicate ingress and restart recovery.
    """

    def __init__(
        self,
        *,
        host: WorldV2PlatformHost,
        recipient_id: str,
        canonical_user_id: str,
        owned_model: DeepSeekChatModel | None = None,
    ) -> None:
        if not recipient_id or not canonical_user_id:
            raise ValueError("QQ C2C host requires recipient and canonical user ids")
        self._host = host
        self._recipient_id = recipient_id
        self._canonical_user_id = canonical_user_id
        self._owned_model = owned_model
        self._lock = asyncio.Lock()
        self._closed = False

    async def inbound_text(
        self,
        *,
        message_id: str,
        recipient_id: str,
        text: str,
        observed_at: datetime,
    ) -> QQC2CIngressResult:
        """Ingest one authorized C2C text message and drain only its Action."""

        if not message_id or not text.strip():
            raise ValueError("QQ C2C v2 ingress requires a message id and non-empty text")
        if recipient_id != self._recipient_id:
            raise ValueError("QQ C2C recipient is not configured for this World v2 host")
        inbound = PlatformInbound(
            platform="qq",
            platform_user_id=recipient_id,
            platform_message_id=message_id,
            text=text.strip(),
            observed_at=observed_at,
            trace_id=f"trace:qq-c2c-v2:{recipient_id}:{message_id}",
            coalescing_metadata={"adapter": "onebot-c2c", "content_kind": "text"},
        )
        async with self._lock:
            outcome = await self._host.inbound(inbound)
            action_id = next(
                iter((*outcome.authorized_action_ids, *outcome.scheduled_action_ids)), None
            )
            if action_id is not None:
                result = await self._host.drain_action(action_id)
                if result is not None and result.action_id not in {None, action_id}:
                    raise RuntimeError("targeted QQ C2C drain returned a different Action")
            return QQC2CIngressResult(
                status=outcome.status,
                action_id=action_id,
                canonical_user_id=self._canonical_user_id,
            )

    async def tick(
        self,
        *,
        tick_id: str,
        logical_time_from: datetime,
        logical_time_to: datetime,
        observed_at: datetime,
        reason: str,
    ) -> str:
        """Advance a caller-owned durable scheduler interval through the v2 host."""

        async with self._lock:
            outcome = await self._host.tick(
                PlatformClockTick(
                    tick_id=tick_id,
                    logical_time_from=logical_time_from,
                    logical_time_to=logical_time_to,
                    observed_at=observed_at,
                    trace_id=f"trace:qq-c2c-v2:tick:{tick_id}",
                    causation_id=f"scheduler:qq-c2c-v2:{tick_id}",
                    correlation_id=f"clock:qq-c2c-v2:{self._recipient_id}",
                    reason=reason,
                )
            )
            return outcome.status

    async def drain(self, *, max_action_units: int = 8, max_background_units: int = 8) -> QQC2CDrainResult:
        """Run restart-safe Action recovery and bounded background work once."""

        if not 0 <= max_action_units <= 64 or not 0 <= max_background_units <= 64:
            raise ValueError("QQ C2C drain limits must be between 0 and 64")
        async with self._lock:
            actions: list[str] = []
            for _ in range(max_action_units):
                result = await self._host.drain_actions_once()
                if result is None or result.status == "idle":
                    break
                actions.append(result.status)
            background: list[str] = []
            for _ in range(max_action_units):
                result = await self._host.drain_media_planning_once()
                if result.status in {"idle", "unavailable", "in_progress"}:
                    if result.status != "idle":
                        background.append("media-plan:" + result.status)
                    break
                background.append("media-plan:" + result.status)
            logical_time = await self._host.current_logical_time()
            if logical_time is not None:
                for _ in range(max_action_units):
                    result = await self._host.drain_media_results_once(logical_time=logical_time)
                    if result is None:
                        break
                    background.append("media:" + result)
            for _ in range(max_background_units):
                result = await self._host.drain_background_once()
                if result is None:
                    break
                background.append(str(getattr(result, "work_status", "processed")))
            return QQC2CDrainResult(
                action_statuses=tuple(actions), background_statuses=tuple(background)
            )

    async def scheduler_once(
        self,
        *,
        observed_at: datetime,
        max_action_units: int = 8,
        max_background_units: int = 8,
    ) -> QQC2CDrainResult:
        """Continue the durable clock and run recovery after a host restart.

        The ``from`` timestamp comes from the v2 application rather than a
        process-local variable, so a restart cannot invent a stale interval.
        """

        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise ValueError("QQ C2C scheduler time must be timezone-aware")
        if not 0 <= max_action_units <= 64 or not 0 <= max_background_units <= 64:
            raise ValueError("QQ C2C scheduler drain limits must be between 0 and 64")
        async with self._lock:
            logical_from = await self._host.current_logical_time()
            if logical_from is not None and observed_at > logical_from:
                tick_id = "tick:qq-c2c-v2:" + observed_at.isoformat()
                outcome = await self._host.tick(
                    PlatformClockTick(
                        tick_id=tick_id,
                        logical_time_from=logical_from,
                        logical_time_to=observed_at,
                        observed_at=observed_at,
                        trace_id=f"trace:qq-c2c-v2:{tick_id}",
                        causation_id=f"scheduler:qq-c2c-v2:{tick_id}",
                        correlation_id=f"clock:qq-c2c-v2:{self._recipient_id}",
                        reason="qq_c2c_scheduler",
                    )
                )
                if outcome.status not in {"observed_only", "deferred"}:
                    raise RuntimeError("QQ C2C scheduler clock was not accepted")
            # Inline the bounded drain while retaining this single scheduler
            # lock; invoking ``drain`` would recursively acquire it.
            actions: list[str] = []
            for _ in range(max_action_units):
                result = await self._host.drain_actions_once()
                if result is None or result.status == "idle":
                    break
                actions.append(result.status)
            background: list[str] = []
            for _ in range(max_action_units):
                result = await self._host.drain_media_planning_once()
                if result.status in {"idle", "unavailable", "in_progress"}:
                    if result.status != "idle":
                        background.append("media-plan:" + result.status)
                    break
                background.append("media-plan:" + result.status)
            logical_time = await self._host.current_logical_time()
            if logical_time is not None:
                for _ in range(max_action_units):
                    result = await self._host.drain_media_results_once(logical_time=logical_time)
                    if result is None:
                        break
                    background.append("media:" + result)
            for _ in range(max_background_units):
                result = await self._host.drain_background_once()
                if result is None:
                    break
                background.append(str(getattr(result, "work_status", "processed")))
            return QQC2CDrainResult(
                action_statuses=tuple(actions), background_statuses=tuple(background)
            )

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._host.close()
        if self._owned_model is not None:
            await self._owned_model.aclose()


def build_qq_c2c_host(
    *,
    settings: Settings,
    recipient_id: str,
    bootstrap_at: datetime | None = None,
    model: ChatCompletionModel | None = None,
    delivery: QQC2CDelivery | None = None,
    media_transport: MediaProviderTransport | None = None,
) -> QQC2CHost:
    """Compose the C2C lane without importing legacy chat/runtime code.

    Media remains opt-in: a caller may provide only a transport that durably
    binds result bytes to idempotency keys and supports recovery lookup.  QQ
    delivery itself is deliberately text-only and is never used as an image
    provider fallback.
    """

    if not recipient_id:
        raise ValueError("QQ C2C v2 requires one configured private recipient")
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
        flash_model_id=str(getattr(model, "model", "qq-c2c-v2-flash")),
    )
    delivery = delivery or QQDelivery(settings)
    transport = QQC2CPlatformTransport(
        delivery=delivery,
        recipients_by_target={qq_c2c_target(recipient_id): recipient_id},
        now=lambda: datetime.now(UTC),
    )
    application = build_sqlite_world_v2_turn_application(
        path=Path(settings.database_path),
        config=WorldV2TurnApplicationConfig(
            world_id=f"world:companion-v2:qq-c2c:{settings.primary_user_id}",
            companion_actor_ref="agent:companion",
            reply_target=qq_c2c_target(recipient_id),
            action_pump_owner="pump:qq-c2c-v2",
        ),
        identities=QQC2CIdentityResolver(
            recipient_id=recipient_id, canonical_user_id=settings.primary_user_id
        ),
        router=_QQC2CRouter(),
        main_model=adapter,
        quick_recovery=adapter,
        transport=transport,
        media_transport=media_transport,
        appraisal_model=AppraisalDraftDeliberationAdapter(model=model),
        affect_model=AffectDraftDeliberationAdapter(model=model),
        now=bootstrap_at or datetime.now(UTC),
    )
    return QQC2CHost(
        host=WorldV2PlatformHost(application=application),
        recipient_id=recipient_id,
        canonical_user_id=settings.primary_user_id,
        owned_model=owned_model,
    )


__all__ = [
    "QQC2CDrainResult",
    "QQC2CDelivery",
    "QQC2CHost",
    "QQC2CIdentityResolver",
    "QQC2CIngressResult",
    "QQC2CPlatformTransport",
    "build_qq_c2c_host",
    "qq_c2c_target",
]

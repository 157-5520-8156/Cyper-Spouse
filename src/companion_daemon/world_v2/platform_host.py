"""Clean, platform-neutral process host for a World v2 application lane.

The host is intentionally shallow.  It translates a provider's inbound
envelope and scheduler tick into application primitives, then asks the
application to advance its already-authorized workers.  It owns neither a
ledger, a reducer, an Engine, nor an outbound provider client: delivery remains
an ActionPump concern inside :class:`WorldV2TurnApplication`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
from typing import Literal, Mapping, Protocol

from .dashboard_projection_adapter import DashboardPublicProjectionDTO, DashboardRoomProjectionDTO
from .production_turn_application import WorldV2TurnApplication
from .schemas import ProjectionRequest


def _require_nonempty(**values: str) -> None:
    missing = tuple(name for name, value in values.items() if not value)
    if missing:
        raise ValueError(f"platform host fields must not be empty: {', '.join(missing)}")


def _require_aware(name: str, value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


@dataclass(frozen=True, slots=True)
class PlatformInbound:
    """Normalized inbound provider envelope, before domain identity resolution."""

    platform: str
    platform_user_id: str
    platform_message_id: str
    text: str | None
    observed_at: datetime
    trace_id: str
    attachment_refs: tuple[str, ...] = ()
    coalescing_metadata: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        _require_nonempty(
            platform=self.platform,
            platform_user_id=self.platform_user_id,
            platform_message_id=self.platform_message_id,
            trace_id=self.trace_id,
        )
        if self.text == "":
            raise ValueError("text must not be empty when supplied")
        if any(not ref for ref in self.attachment_refs):
            raise ValueError("attachment_refs must not contain an empty ref")
        metadata = dict(self.coalescing_metadata or {})
        try:
            json.dumps(metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            raise ValueError("coalescing_metadata must be JSON-serializable") from exc
        if self.text is None and not self.attachment_refs and not metadata:
            raise ValueError("inbound must carry text, an attachment, or coalescing metadata")
        object.__setattr__(self, "attachment_refs", tuple(self.attachment_refs))
        object.__setattr__(self, "coalescing_metadata", metadata)
        _require_aware("observed_at", self.observed_at)


@dataclass(frozen=True, slots=True)
class PlatformClockTick:
    """A scheduler observation; its event identity remains application-owned."""

    tick_id: str
    logical_time_from: datetime
    logical_time_to: datetime
    observed_at: datetime
    trace_id: str
    causation_id: str
    correlation_id: str
    reason: str
    policy_version: str | None = None
    policy_digest: str | None = None

    def __post_init__(self) -> None:
        _require_nonempty(
            tick_id=self.tick_id,
            trace_id=self.trace_id,
            causation_id=self.causation_id,
            correlation_id=self.correlation_id,
            reason=self.reason,
        )
        for name, value in (
            ("logical_time_from", self.logical_time_from),
            ("logical_time_to", self.logical_time_to),
            ("observed_at", self.observed_at),
        ):
            _require_aware(name, value)
        if self.logical_time_to <= self.logical_time_from:
            raise ValueError("logical_time_to must be after logical_time_from")
        if (self.policy_version is None) != (self.policy_digest is None):
            raise ValueError("clock policy version and digest must be supplied together")


class PlatformInboundTransport(Protocol):
    """One-way provider ingress used only by a host polling loop."""

    async def receive(self) -> PlatformInbound | None: ...


@dataclass(frozen=True, slots=True)
class PlatformReceipt:
    """Normalized asynchronous provider receipt, before World v2 settlement."""

    source: str
    source_event_id: str
    action_id: str
    idempotency_key: str
    status: Literal[
        "provider_accepted", "delivered", "failed", "cancelled", "expired", "unknown"
    ]
    provider_ref: str
    observed_at: datetime
    trace_id: str
    causation_id: str
    correlation_id: str
    raw_payload_hash: str
    kind: Literal[
        "provider_ack",
        "execution_receipt",
        "tool_result",
        "media_result",
        "reconciliation_result",
    ] = "execution_receipt"
    artifact_refs: tuple[str, ...] = ()
    cost_actual: int = 0
    error_class: str | None = None
    retryability: Literal["retryable", "not_retryable", "unknown"] | None = None

    def __post_init__(self) -> None:
        _require_nonempty(
            source=self.source,
            source_event_id=self.source_event_id,
            action_id=self.action_id,
            idempotency_key=self.idempotency_key,
            status=self.status,
            provider_ref=self.provider_ref,
            trace_id=self.trace_id,
            causation_id=self.causation_id,
            correlation_id=self.correlation_id,
            raw_payload_hash=self.raw_payload_hash,
        )
        _require_aware("observed_at", self.observed_at)
        if self.cost_actual < 0:
            raise ValueError("cost_actual must not be negative")
        if any(not ref for ref in self.artifact_refs):
            raise ValueError("artifact_refs must not contain an empty ref")
        object.__setattr__(self, "artifact_refs", tuple(self.artifact_refs))


class PlatformReceiptTransport(Protocol):
    """One-way provider receipt ingress used by a host callback/poll loop."""

    async def receive_receipt(self) -> PlatformReceipt | None: ...


class DashboardProjectionCapture(Protocol):
    """A transport-free dashboard snapshot capability owned by composition."""

    def capture(self, request: ProjectionRequest) -> DashboardRoomProjectionDTO: ...


class DashboardPublicProjectionCapture(Protocol):
    """Composition-owned public Dashboard read capability."""

    def capture(self, request: ProjectionRequest) -> DashboardPublicProjectionDTO: ...


class WorldV2PlatformHost:
    """A platform process facade with one dependency: ``WorldV2TurnApplication``.

    This class deliberately has no send method.  A received message may
    authorize an Action, but the composition-owned ActionPump selects, claims,
    dispatches, and settles that Action through its configured executor.
    """

    def __init__(
        self,
        *,
        application: WorldV2TurnApplication,
        dashboard_capture: DashboardProjectionCapture | None = None,
        dashboard_public_capture: DashboardPublicProjectionCapture | None = None,
    ) -> None:
        self._application = application
        self._dashboard_capture = dashboard_capture
        self._dashboard_public_capture = dashboard_public_capture

    async def inbound(self, message: PlatformInbound):
        """Process a normalized provider message exactly once by source identity."""

        return await self.respond(message)

    async def respond(self, message: PlatformInbound):
        """Forward a message without granting the host runtime or ledger access."""

        return await self._application.inbound(
            platform=message.platform,
            platform_user_id=message.platform_user_id,
            platform_message_id=message.platform_message_id,
            text=message.text,
            observed_at=message.observed_at,
            trace_id=message.trace_id,
            attachment_refs=message.attachment_refs,
            coalescing_metadata=message.coalescing_metadata,
        )

    async def tick(self, tick: PlatformClockTick):
        """Advance logical time through the application-owned clock command."""

        return await self._application.tick(
            tick_id=tick.tick_id,
            logical_time_from=tick.logical_time_from,
            logical_time_to=tick.logical_time_to,
            observed_at=tick.observed_at,
            trace_id=tick.trace_id,
            causation_id=tick.causation_id,
            correlation_id=tick.correlation_id,
            reason=tick.reason,
            policy_version=tick.policy_version,
            policy_digest=tick.policy_digest,
        )

    async def receipt(self, receipt: PlatformReceipt):
        """Forward an asynchronous provider callback to application settlement."""

        return await self._application.receipt(
            source=receipt.source,
            source_event_id=receipt.source_event_id,
            action_id=receipt.action_id,
            idempotency_key=receipt.idempotency_key,
            status=receipt.status,
            provider_ref=receipt.provider_ref,
            observed_at=receipt.observed_at,
            trace_id=receipt.trace_id,
            causation_id=receipt.causation_id,
            correlation_id=receipt.correlation_id,
            raw_payload_hash=receipt.raw_payload_hash,
            kind=receipt.kind,
            artifact_refs=receipt.artifact_refs,
            cost_actual=receipt.cost_actual,
            error_class=receipt.error_class,
            retryability=receipt.retryability,
        )

    async def drain_inbound_once(self, transport: PlatformInboundTransport):
        """Poll one normalized ingress envelope; no message means no work."""

        message = await transport.receive()
        if message is None:
            return None
        return await self.inbound(message)

    async def drain_receipts_once(self, transport: PlatformReceiptTransport):
        """Poll one external receipt; no callback means no settlement work."""

        receipt = await transport.receive_receipt()
        if receipt is None:
            return None
        return await self.receipt(receipt)

    async def drain_actions_once(self):
        """Advance one durable Action through the application's ActionPump."""

        return await self._application.drain_actions_once()

    async def drain_action(self, action_id: str):
        """Advance a specific ingress-authorized Action only."""

        return await self._application.drain_action(action_id)

    async def drain_media_results_once(self, *, logical_time: datetime) -> str | None:
        """Materialize one receipt-bound media result after provider dispatch.

        This is intentionally a distinct scheduler phase.  It cannot send an
        image, invent a plan, or use a platform transport as a provider
        fallback; the composition-owned application verifies the terminal
        media receipt before it writes a preview/inspection continuation.
        """

        return await self._application.drain_media_results_once(logical_time=logical_time)

    async def drain_media_planning_once(self):
        """Advance one frozen media-planning Action through the v2 scheduler.

        The host cannot supply a candidate, snapshot, or provider request;
        composition only drains a prior source-bound Action.
        """

        return await self._application.drain_media_planning_once()

    async def drain_background_once(self):
        """Advance one separately scheduled, non-visible World v2 work unit."""

        return await self._application.drain_background_once()

    async def current_logical_time(self) -> datetime | None:
        """Read the one scheduler scalar needed to continue a durable clock."""

        return await self._application.current_logical_time()

    def capture_dashboard_room(self, request: ProjectionRequest) -> DashboardRoomProjectionDTO:
        """Capture one authorized viewer DTO; HTTP/WebSocket remains outside this host."""

        if self._dashboard_capture is None:
            raise RuntimeError("dashboard capture is not configured for this platform host")
        return self._dashboard_capture.capture(request)

    def capture_dashboard_public(self, request: ProjectionRequest) -> DashboardPublicProjectionDTO:
        """Capture the dedicated public Dashboard contract at one cursor."""

        if self._dashboard_public_capture is None:
            raise RuntimeError("dashboard public capture is not configured for this platform host")
        return self._dashboard_public_capture.capture(request)

    def close(self) -> None:
        """Close the composition-owned persistent application once the host stops."""

        self._application.close()


__all__ = [
    "PlatformClockTick",
    "DashboardProjectionCapture",
    "DashboardPublicProjectionCapture",
    "PlatformInbound",
    "PlatformInboundTransport",
    "PlatformReceipt",
    "PlatformReceiptTransport",
    "WorldV2PlatformHost",
]

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
from typing import Protocol

from .production_turn_application import WorldV2TurnApplication


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
    text: str
    observed_at: datetime
    trace_id: str

    def __post_init__(self) -> None:
        _require_nonempty(
            platform=self.platform,
            platform_user_id=self.platform_user_id,
            platform_message_id=self.platform_message_id,
            text=self.text,
            trace_id=self.trace_id,
        )
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


class WorldV2PlatformHost:
    """A platform process facade with one dependency: ``WorldV2TurnApplication``.

    This class deliberately has no send method.  A received message may
    authorize an Action, but the composition-owned ActionPump selects, claims,
    dispatches, and settles that Action through its configured executor.
    """

    def __init__(self, *, application: WorldV2TurnApplication) -> None:
        self._application = application

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

    async def drain_inbound_once(self, transport: PlatformInboundTransport):
        """Poll one normalized ingress envelope; no message means no work."""

        message = await transport.receive()
        if message is None:
            return None
        return await self.inbound(message)

    async def drain_actions_once(self):
        """Advance one durable Action through the application's ActionPump."""

        return await self._application.drain_actions_once()

    async def drain_background_once(self):
        """Advance one separately scheduled, non-visible World v2 work unit."""

        return await self._application.drain_background_once()

    def close(self) -> None:
        """Close the composition-owned persistent application once the host stops."""

        self._application.close()


__all__ = [
    "PlatformClockTick",
    "PlatformInbound",
    "PlatformInboundTransport",
    "WorldV2PlatformHost",
]

"""Platform-neutral ingress seam for the first World v2 chat path."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
from typing import Protocol

from .runtime import WorldRuntime
from .schemas import Observation, RuntimeOutcome


class InboundIdentityResolver(Protocol):
    def resolve(self, *, platform: str, platform_user_id: str) -> tuple[str, str]: ...


@dataclass(frozen=True, slots=True)
class InboundTurn:
    platform: str
    platform_user_id: str
    platform_message_id: str
    text: str
    observed_at: datetime
    trace_id: str


class WorldTurnRuntime:
    """Turn ingress with no Engine, WorldKernel, or platform delivery writes."""

    def __init__(self, *, runtime: WorldRuntime, identities: InboundIdentityResolver) -> None:
        self._runtime = runtime
        self._identities = identities

    async def respond(self, turn: InboundTurn) -> RuntimeOutcome:
        actor, target = self._identities.resolve(
            platform=turn.platform, platform_user_id=turn.platform_user_id
        )
        source_event_id = f"{turn.platform}:{turn.platform_user_id}:{turn.platform_message_id}"
        payload_hash = hashlib.sha256(turn.text.encode("utf-8")).hexdigest()
        return await self._runtime.ingest(
            Observation(
                schema_version="world-v2.1",
                observation_id=f"observation:{source_event_id}",
                world_id=self._runtime.world_id,
                logical_time=turn.observed_at,
                created_at=turn.observed_at,
                trace_id=turn.trace_id,
                causation_id=source_event_id,
                correlation_id=source_event_id,
                source=f"platform:{turn.platform}",
                source_event_id=source_event_id,
                actor=actor,
                channel=turn.platform,
                payload_ref=f"ingress:{source_event_id}",
                payload_hash=payload_hash,
                text=turn.text,
                received_at=turn.observed_at,
                reply_context={"target": target},
            )
        )


__all__ = ["InboundIdentityResolver", "InboundTurn", "WorldTurnRuntime"]

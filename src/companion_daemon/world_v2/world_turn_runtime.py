"""Platform-neutral ingress seam for the first World v2 chat path."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
from typing import Protocol

from .runtime import WorldRuntime
from .action_pump import ActionPumpResult
from .affect_trigger_runtime import AffectTriggerRunResult
from .interaction_appraisal_trigger_runtime import AppraisalTriggerRunResult
from .schemas import ClockObservation, Observation, RuntimeOutcome


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

    async def advance(self, clock: ClockObservation) -> RuntimeOutcome:
        """Advance the durable world clock without exposing a ledger writer.

        Platform hosts and schedulers use the same runtime seam for message
        ingress and time progression.  They cannot manufacture lifecycle
        events, run reducers, or access the legacy engine directly.
        """

        return await self._runtime.advance(clock)

    async def drain_actions_once(self) -> ActionPumpResult | None:
        """Advance one already-authorized delivery without exposing the ledger.

        A platform host normally invokes this from its durable delivery worker
        immediately after :meth:`respond` and again during recovery.  Keeping
        that call on the same platform-neutral seam prevents a host from
        reaching into the legacy Engine or a ``WorldLedger`` to deliver text.
        It intentionally does not invent a second dispatch loop: scheduling,
        claiming, receipt settlement, and recovery remain ``WorldRuntime``
        responsibilities.
        """

        return await self._runtime.drain_actions_once()

    async def drain_background_once(
        self,
    ) -> AppraisalTriggerRunResult | AffectTriggerRunResult | None:
        """Advance one durable low-priority affect job, if this host configured one.

        This remains explicitly separate from ``respond``: a host scheduler can
        keep the visible reply lane latency-bounded while still allowing a
        persistent appraisal/affect worker to finish after the interaction.
        """

        return await self._runtime.drain_background_once()


__all__ = ["InboundIdentityResolver", "InboundTurn", "WorldTurnRuntime"]

"""Production bridge from one accepted perception batch to one Life wake.

The acceptance runtime remains the sole World writer.  This bridge selects one
of the batch's already-committed perception events only as a deterministic
wake identity; it does not inspect the signal text or choose any consequence.
Life Ecology's durable trigger store supplies effect-once behavior when a
process crashes after the World commit but before this call returns.
"""

from __future__ import annotations

import asyncio
from typing import Protocol


class AtomicExternalPerceptionAcceptance(Protocol):
    def accept_delivery(self, delivery: object) -> object: ...


class LifeEcologyWakePort(Protocol):
    async def advance_life_ecology_once(
        self,
        *,
        wake_event_ref: str,
        trace_id: str,
        correlation_id: str,
    ) -> object: ...


class ProducerBackedExternalPerceptionAcceptance:
    """Hide producer capabilities behind the live coordinator's one call."""

    def __init__(self, *, producer: object, runtime: object) -> None:
        if not callable(getattr(producer, "prepare", None)) or not callable(
            getattr(runtime, "accept", None)
        ):
            raise TypeError("live acceptance requires producer and runtime capabilities")
        self._producer = producer
        self._runtime = runtime

    def accept_delivery(self, delivery: object) -> object:
        return self._runtime.accept(self._producer.prepare(delivery))


class LifeWakingExternalPerceptionAcceptance:
    """Accept one atomic batch, then offer exactly one sourced Life opportunity."""

    def __init__(
        self,
        *,
        acceptance: AtomicExternalPerceptionAcceptance,
        life: LifeEcologyWakePort,
        trace_prefix: str = "trace:external-perception",
    ) -> None:
        if not trace_prefix:
            raise ValueError("external perception Life trace prefix is required")
        self._acceptance = acceptance
        self._life = life
        self._trace_prefix = trace_prefix

    async def accept(self, delivery: object) -> object:
        receipt = await asyncio.to_thread(self._acceptance.accept_delivery, delivery)
        attempt_id = getattr(receipt, "attention_attempt_id", None)
        perceptions = getattr(receipt, "perceptions", None)
        if not isinstance(attempt_id, str) or not attempt_id:
            raise ValueError("external perception acceptance has no attempt identity")
        if not isinstance(perceptions, tuple) or not perceptions:
            raise ValueError("external perception acceptance has no perception wake")
        wake_event_ref = getattr(perceptions[0], "perception_event_ref", None)
        if not isinstance(wake_event_ref, str) or not wake_event_ref:
            raise ValueError("external perception acceptance wake is invalid")
        await self._life.advance_life_ecology_once(
            wake_event_ref=wake_event_ref,
            trace_id=f"{self._trace_prefix}:{attempt_id}",
            correlation_id=f"external-perception:{attempt_id}",
        )
        return receipt

    async def accept_external_perception(self, delivery: object) -> object:
        """Satisfy the live attention port without exposing producer handles."""

        return await self.accept(delivery)


__all__ = [
    "AtomicExternalPerceptionAcceptance",
    "LifeEcologyWakePort",
    "LifeWakingExternalPerceptionAcceptance",
    "ProducerBackedExternalPerceptionAcceptance",
]

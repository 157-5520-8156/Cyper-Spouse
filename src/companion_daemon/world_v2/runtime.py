from __future__ import annotations

import asyncio

from .action_lifecycle import settlement_event_type
from .errors import ActionIdentityMismatch, UnknownAction
from .ledger import LedgerPort, WorldLedger
from .schemas import (
    ClockObservation,
    ExternalObservation,
    Observation,
    ProjectionRequest,
    RuntimeOutcome,
    WorldEvent,
    WorldProjection,
)


class WorldRuntime:
    """World v2's only application-facing runtime seam.

    Runtime owns orchestration only. WorldLedger is the sole event, revision, idempotency,
    and projection authority.
    """

    def __init__(self, *, world_id: str, ledger: LedgerPort | None = None) -> None:
        if not world_id:
            raise ValueError("world_id must not be empty")
        if ledger is not None and ledger.world_id != world_id:
            raise ValueError("ledger belongs to another world")
        self._world_id = world_id
        self._ledger = ledger or WorldLedger.in_memory(world_id=world_id)
        self._lock = asyncio.Lock()

    @classmethod
    def in_memory(cls, *, world_id: str) -> WorldRuntime:
        return cls(world_id=world_id)

    async def _project_for_write(self):
        if self._ledger.blocks_event_loop:
            return await asyncio.to_thread(self._ledger.project)
        return self._ledger.project()

    async def _commit(self, events: list[WorldEvent], *, world_revision: int, deliberation_revision: int):
        if self._ledger.blocks_event_loop:
            return await asyncio.to_thread(
                self._ledger.commit,
                events,
                expected_world_revision=world_revision,
                expected_deliberation_revision=deliberation_revision,
            )
        return self._ledger.commit(
            events,
            expected_world_revision=world_revision,
            expected_deliberation_revision=deliberation_revision,
        )

    async def ingest(self, observation: Observation) -> RuntimeOutcome:
        if observation.world_id != self._world_id:
            raise ValueError(
                f"observation world_id {observation.world_id!r} does not match "
                f"runtime world_id {self._world_id!r}"
            )
        trigger_id = f"trigger:observation:{observation.source}:{observation.source_event_id}"
        event = WorldEvent.from_payload(
            schema_version=observation.schema_version,
            event_id=f"event:{trigger_id}",
            world_id=self._world_id,
            event_type="ObservationRecorded",
            logical_time=observation.logical_time,
            created_at=observation.created_at,
            actor=observation.actor,
            source=observation.source,
            trace_id=observation.trace_id,
            causation_id=observation.causation_id,
            correlation_id=observation.correlation_id,
            idempotency_key=f"observation:{observation.source}:{observation.source_event_id}",
            payload=observation.model_dump(mode="json"),
        )
        async with self._lock:
            before = await self._project_for_write()
            committed = await self._commit(
                [event], world_revision=before.world_revision,
                deliberation_revision=before.deliberation_revision,
            )
        return RuntimeOutcome(
            outcome_id=f"outcome:{trigger_id}",
            trigger_id=trigger_id,
            observation_ref=observation.observation_id,
            committed_world_revision=committed.world_revision,
            ledger_sequence=committed.ledger_sequence,
            status="observed_only",
            projection_hint=f"world-revision:{committed.world_revision}",
        )

    async def advance(self, clock: ClockObservation) -> RuntimeOutcome:
        if clock.world_id != self._world_id:
            raise ValueError("clock belongs to another world")
        if clock.logical_time_to <= clock.logical_time_from:
            raise ValueError("logical time cannot move backwards")
        trigger_id = f"trigger:clock:{clock.tick_id}"
        event = WorldEvent.from_payload(
            schema_version=clock.schema_version,
            event_id=f"event:{trigger_id}",
            world_id=self._world_id,
            event_type="ClockAdvanced",
            logical_time=clock.logical_time_to,
            created_at=clock.created_at,
            actor="system:clock",
            source="scheduler",
            trace_id=clock.trace_id,
            causation_id=clock.causation_id,
            correlation_id=clock.correlation_id,
            idempotency_key=f"clock:{clock.tick_id}",
            payload=clock.model_dump(mode="json"),
        )
        async with self._lock:
            before = await self._project_for_write()
            committed = await self._commit(
                [event], world_revision=before.world_revision,
                deliberation_revision=before.deliberation_revision,
            )
        return RuntimeOutcome(
            outcome_id=f"outcome:{trigger_id}",
            trigger_id=trigger_id,
            committed_world_revision=committed.world_revision,
            ledger_sequence=committed.ledger_sequence,
            status="observed_only",
            projection_hint=f"world-revision:{committed.world_revision}",
        )

    async def settle(self, result: ExternalObservation) -> RuntimeOutcome:
        if result.world_id != self._world_id:
            raise ValueError("external observation belongs to another world")
        trigger_id = f"trigger:settlement:{result.source}:{result.source_event_id}"
        async with self._lock:
            before = await self._project_for_write()
            action = next(
                (
                    candidate
                    for candidate in before.actions
                    if candidate.action_id == result.action_id
                ),
                None,
            )
            if action is None:
                raise UnknownAction(f"action {result.action_id!r} does not exist")
            if action.idempotency_key != result.idempotency_key:
                raise ActionIdentityMismatch(
                    f"result idempotency key does not match action {action.action_id!r}"
                )
            event = WorldEvent.from_payload(
                schema_version=result.schema_version,
                event_id=f"event:{trigger_id}",
                world_id=self._world_id,
                event_type=settlement_event_type(result.status),
                logical_time=result.logical_time,
                created_at=result.created_at,
                actor=f"provider:{result.source}",
                source=result.source,
                trace_id=result.trace_id,
                causation_id=result.causation_id,
                correlation_id=result.correlation_id,
                idempotency_key=(
                    f"external-observation:{result.source}:{result.source_event_id}"
                ),
                payload=result.model_dump(mode="json"),
            )
            committed = await self._commit(
                [event], world_revision=before.world_revision,
                deliberation_revision=before.deliberation_revision,
            )
        return RuntimeOutcome(
            outcome_id=f"outcome:{trigger_id}",
            trigger_id=trigger_id,
            observation_ref=result.result_id,
            committed_world_revision=committed.world_revision,
            ledger_sequence=committed.ledger_sequence,
            status="action_executed",
            projection_hint=f"action:{result.action_id}:{result.status}",
        )

    def project(self, viewer: ProjectionRequest) -> WorldProjection:
        projection = self._ledger.project()
        if viewer.at_world_revision not in (None, projection.world_revision):
            raise ValueError("historical projection is not implemented in this vertical slice")
        debug_refs: tuple[str, ...] = ()
        if viewer.include_debug_refs and "world:debug" in viewer.permissions:
            debug_refs = projection.observation_refs
        return WorldProjection(
            world_id=self._world_id,
            world_revision=projection.world_revision,
            ledger_sequence=projection.ledger_sequence,
            semantic_hash=projection.semantic_hash,
            logical_time=projection.logical_time,
            debug_observation_refs=debug_refs,
        )

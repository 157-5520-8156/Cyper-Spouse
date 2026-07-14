from __future__ import annotations

import asyncio

from .ledger import WorldLedger
from .schemas import Observation, ProjectionRequest, RuntimeOutcome, WorldEvent, WorldProjection


class WorldRuntime:
    """World v2's only application-facing runtime seam.

    Runtime owns orchestration only. WorldLedger is the sole event, revision, idempotency,
    and projection authority.
    """

    def __init__(self, *, world_id: str, ledger: WorldLedger | None = None) -> None:
        if not world_id:
            raise ValueError("world_id must not be empty")
        self._world_id = world_id
        self._ledger = ledger or WorldLedger.in_memory(world_id=world_id)
        self._lock = asyncio.Lock()

    @classmethod
    def in_memory(cls, *, world_id: str) -> WorldRuntime:
        return cls(world_id=world_id)

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
            before = self._ledger.project()
            committed = self._ledger.commit(
                [event],
                expected_world_revision=before.world_revision,
                expected_deliberation_revision=before.deliberation_revision,
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

from __future__ import annotations

import asyncio

from .affect_math import DecayAnchor, DecayProfile, decay_intensity_bp
from .errors import ConcurrencyConflict, IdempotencyConflict
from .ledger import LedgerPort, WorldLedger
from .event_identity import domain_idempotency_key
from .clock_authority import append_clock_transition, resolve_latest_clock
from .goal_expiry_runtime import build_due_goal_expiry_events
from .pinned_turn import PinnedTurnCompiler
from .projection import ProjectionAuthority, ProjectionCompiler
from .settlement import SettlementPlanner
from .schemas import (
    ClockObservation,
    CommitResult,
    ExternalObservation,
    Observation,
    ProjectionCursor,
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

    def __init__(
        self,
        *,
        world_id: str,
        ledger: LedgerPort | None = None,
        projection_authority: ProjectionAuthority | None = None,
        pinned_turn: PinnedTurnCompiler | None = None,
    ) -> None:
        if not world_id:
            raise ValueError("world_id must not be empty")
        if ledger is not None and ledger.world_id != world_id:
            raise ValueError("ledger belongs to another world")
        self._world_id = world_id
        self._ledger = ledger or WorldLedger.in_memory(world_id=world_id)
        self._settlement = SettlementPlanner(world_id=world_id)
        self._projection = ProjectionCompiler(authority=projection_authority)
        self._pinned_turn = pinned_turn
        self._lock = asyncio.Lock()

    @classmethod
    def in_memory(
        cls,
        *,
        world_id: str,
        projection_authority: ProjectionAuthority | None = None,
    ) -> WorldRuntime:
        return cls(world_id=world_id, projection_authority=projection_authority)

    async def _project_for_write(self):
        if self._ledger.blocks_event_loop:
            return await asyncio.to_thread(self._ledger.project)
        return self._ledger.project()

    async def _commit(
        self,
        events: list[WorldEvent],
        *,
        world_revision: int,
        deliberation_revision: int,
        commit_id: str | None = None,
    ):
        if self._ledger.blocks_event_loop:
            return await asyncio.to_thread(
                self._ledger.commit,
                events,
                expected_world_revision=world_revision,
                expected_deliberation_revision=deliberation_revision,
                commit_id=commit_id,
            )
        return self._ledger.commit(
            events,
            expected_world_revision=world_revision,
            expected_deliberation_revision=deliberation_revision,
            commit_id=commit_id,
        )

    async def _lookup_event_commit(self, event_id: str):
        if self._ledger.blocks_event_loop:
            return await asyncio.to_thread(self._ledger.lookup_event_commit, event_id)
        return self._ledger.lookup_event_commit(event_id)

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
            idempotency_key=domain_idempotency_key(
                event_type="ObservationRecorded",
                world_id=self._world_id,
                payload=observation.model_dump(mode="json"),
            )
            or f"observation:{observation.source}:{observation.source_event_id}",
            payload=observation.model_dump(mode="json"),
        )
        async with self._lock:
            existing = await self._lookup_event_commit(event.event_id)
            if existing is not None:
                persisted, original_commit = existing
                if persisted != event:
                    raise IdempotencyConflict(
                        "observation trigger was already committed with different content"
                    )
                return RuntimeOutcome(
                    outcome_id=f"outcome:{trigger_id}",
                    trigger_id=trigger_id,
                    observation_ref=observation.observation_id,
                    committed_world_revision=original_commit.world_revision,
                    ledger_sequence=original_commit.ledger_sequence,
                    status="observed_only",
                    projection_hint=f"world-revision:{original_commit.world_revision}",
                )
            before = await self._project_for_write()
            committed = await self._commit(
                [event],
                world_revision=before.world_revision,
                deliberation_revision=before.deliberation_revision,
            )
            if self._pinned_turn is not None:
                await self._pinned_turn.audit_observation(
                    observation=observation,
                    observation_event=event,
                    cursor=ProjectionCursor(
                        world_revision=committed.world_revision,
                        deliberation_revision=committed.deliberation_revision,
                        ledger_sequence=committed.ledger_sequence,
                    ),
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

    def _affect_decay_events(self, projection, clock: ClockObservation) -> list[WorldEvent]:
        events: list[WorldEvent] = []
        baselines = {item.dimension: item.baseline_bp for item in projection.affect_baselines}
        for episode in projection.affect_episodes:
            if episode.status != "active":
                continue
            results: list[dict[str, object]] = []
            changed = False
            for component in episode.components:
                profile = component.decay_profile
                after = decay_intensity_bp(
                    DecayAnchor(
                        intensity_bp=component.decay_anchor_intensity_bp,
                        anchored_at=component.decay_anchor_at,
                        baseline_bp=baselines.get(component.dimension, 0),
                        residue_bp=component.residue_bp,
                        decay_not_before=component.decay_not_before,
                    ),
                    DecayProfile(
                        half_life_seconds=profile.half_life_seconds,
                        floor_bp=profile.floor_bp,
                        delay_seconds=profile.delay_seconds,
                        config_version=profile.config_version,
                        kind=profile.kind,
                    ),
                    clock.logical_time_to,
                )
                changed = changed or after != component.intensity_bp
                results.append(
                    {
                        "component_id": component.component_id,
                        "before_intensity_bp": component.intensity_bp,
                        "after_intensity_bp": after,
                        "config_version": profile.config_version,
                        "table_digest": profile.table_digest,
                        "config_digest": profile.config_digest,
                    }
                )
            if not changed:
                continue
            payload = {
                "change_id": f"change:affect-decay:{episode.episode_id}:{clock.tick_id}",
                "transition_id": f"transition:affect-decay:{episode.episode_id}:{clock.tick_id}",
                "expected_entity_revision": episode.entity_revision,
                "evidence_refs": [
                    {
                        "ref_id": f"clock:{clock.logical_time_to.isoformat()}",
                        "evidence_type": "clock_observation",
                        "claim_purpose": "current_fact",
                    }
                ],
                "appraisal_refs": [],
                "policy_refs": ["policy:affect-v1"],
                "episode_id": episode.episode_id,
                "from_logical_time": episode.updated_at.isoformat(),
                "to_logical_time": clock.logical_time_to.isoformat(),
                "component_results": results,
            }
            event_type = "AffectEpisodeDecayed"
            events.append(
                WorldEvent.from_payload(
                    schema_version=clock.schema_version,
                    event_id=f"event:affect-decay:{episode.episode_id}:{clock.tick_id}",
                    world_id=self._world_id,
                    event_type=event_type,
                    logical_time=clock.logical_time_to,
                    created_at=clock.created_at,
                    actor="system:affect-clock",
                    source="scheduler",
                    trace_id=clock.trace_id,
                    causation_id=f"event:trigger:clock:{clock.tick_id}",
                    correlation_id=clock.correlation_id,
                    idempotency_key=domain_idempotency_key(
                        event_type=event_type, world_id=self._world_id, payload=payload
                    )
                    or f"affect-decay:{episode.episode_id}:{clock.tick_id}",
                    payload=payload,
                )
            )
        return events

    def _goal_expiry_events(
        self,
        projection,
        clock: ClockObservation,
        *,
        clock_event: WorldEvent,
    ) -> list[WorldEvent]:
        clock_transition = append_clock_transition(
            projection.clock_transition_history,
            event=clock_event,
            current_logical_time=projection.logical_time,
            computed_world_revision=projection.world_revision + 1,
        )[-1]
        return build_due_goal_expiry_events(
            world_id=self._world_id,
            goals=projection.goals,
            clock=clock,
            clock_transition=clock_transition,
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
            existing = await self._lookup_event_commit(event.event_id)
            if existing is not None:
                persisted, original_commit = existing
                original_outcome = self._clock_retry_outcome(
                    event=event,
                    persisted=persisted,
                    original_commit=original_commit,
                    trigger_id=trigger_id,
                    tick_id=clock.tick_id,
                )
                return await self._recover_goal_expiries(
                    clock=clock,
                    clock_event=persisted,
                    original_outcome=original_outcome,
                    trigger_id=trigger_id,
                )
            before = await self._project_for_write()
            events = [
                event,
                *self._goal_expiry_events(before, clock, clock_event=event),
                *self._affect_decay_events(before, clock),
            ]
            try:
                committed = await self._commit(
                    events,
                    world_revision=before.world_revision,
                    deliberation_revision=before.deliberation_revision,
                )
            except IdempotencyConflict:
                raced = await self._lookup_event_commit(event.event_id)
                if raced is None:
                    raise
                persisted, original_commit = raced
                original_outcome = self._clock_retry_outcome(
                    event=event,
                    persisted=persisted,
                    original_commit=original_commit,
                    trigger_id=trigger_id,
                    tick_id=clock.tick_id,
                )
                return await self._recover_goal_expiries(
                    clock=clock,
                    clock_event=persisted,
                    original_outcome=original_outcome,
                    trigger_id=trigger_id,
                )
        return RuntimeOutcome(
            outcome_id=f"outcome:{trigger_id}",
            trigger_id=trigger_id,
            committed_world_revision=committed.world_revision,
            ledger_sequence=committed.ledger_sequence,
            status="observed_only",
            projection_hint=f"world-revision:{committed.world_revision}",
        )

    async def _recover_goal_expiries(
        self,
        *,
        clock: ClockObservation,
        clock_event: WorldEvent,
        original_outcome: RuntimeOutcome,
        trigger_id: str,
    ) -> RuntimeOutcome:
        """Idempotently supplement due Goals omitted after an exact latest Clock."""

        for _attempt in range(3):
            current = await self._project_for_write()
            try:
                latest = resolve_latest_clock(
                    current.clock_transition_history,
                    current_logical_time=current.logical_time,
                )
            except ValueError:
                return original_outcome
            if (
                latest.clock_event_ref != clock_event.event_id
                or latest.payload_hash != clock_event.payload_hash
            ):
                return original_outcome
            events = build_due_goal_expiry_events(
                world_id=self._world_id,
                goals=current.goals,
                clock=clock,
                clock_transition=latest,
            )
            if not events:
                return original_outcome
            try:
                committed = await self._commit(
                    events,
                    world_revision=current.world_revision,
                    deliberation_revision=current.deliberation_revision,
                )
            except (ConcurrencyConflict, IdempotencyConflict):
                joined = [await self._lookup_event_commit(item.event_id) for item in events]
                if all(item is not None for item in joined):
                    persisted = [item for item in joined if item is not None]
                    if all(
                        stored_event == expected
                        for (stored_event, _commit), expected in zip(
                            persisted, events, strict=True
                        )
                    ) and len({commit for _event, commit in persisted}) == 1:
                        return self._runtime_outcome_for_commit(
                            trigger_id=trigger_id,
                            committed=persisted[0][1],
                        )
                continue
            return self._runtime_outcome_for_commit(
                trigger_id=trigger_id,
                committed=committed,
            )
        raise ConcurrencyConflict("Goal expiry recovery did not converge")

    @staticmethod
    def _runtime_outcome_for_commit(
        *, trigger_id: str, committed: CommitResult
    ) -> RuntimeOutcome:
        return RuntimeOutcome(
            outcome_id=f"outcome:{trigger_id}",
            trigger_id=trigger_id,
            committed_world_revision=committed.world_revision,
            ledger_sequence=committed.ledger_sequence,
            status="observed_only",
            projection_hint=f"world-revision:{committed.world_revision}",
        )

    @staticmethod
    def _clock_retry_outcome(
        *,
        event: WorldEvent,
        persisted: WorldEvent,
        original_commit: CommitResult,
        trigger_id: str,
        tick_id: str,
    ) -> RuntimeOutcome:
        if persisted != event:
            raise IdempotencyConflict(
                f"clock tick {tick_id!r} was already committed with different content"
            )
        return RuntimeOutcome(
            outcome_id=f"outcome:{trigger_id}",
            trigger_id=trigger_id,
            committed_world_revision=original_commit.world_revision,
            ledger_sequence=original_commit.ledger_sequence,
            status="observed_only",
            projection_hint=f"world-revision:{original_commit.world_revision}",
        )

    async def settle(self, result: ExternalObservation) -> RuntimeOutcome:
        if result.world_id != self._world_id:
            raise ValueError("external observation belongs to another world")
        trigger_id = f"trigger:settlement:{result.source}:{result.source_event_id}"
        async with self._lock:
            before = await self._project_for_write()
            recording_events = self._settlement.recording_events(result, trigger_id=trigger_id)
            await self._commit(
                list(recording_events),
                world_revision=before.world_revision,
                deliberation_revision=before.deliberation_revision,
                commit_id=f"commit:{trigger_id}:inbox",
            )
            after_inbox = await self._project_for_write()
            plan = self._settlement.plan(
                result,
                trigger_id=trigger_id,
                projection=after_inbox,
            )
            committed = await self._commit(
                list(plan.events),
                world_revision=after_inbox.world_revision,
                deliberation_revision=after_inbox.deliberation_revision,
                commit_id=f"commit:{trigger_id}:settlement",
            )
        return RuntimeOutcome(
            outcome_id=f"outcome:{trigger_id}",
            trigger_id=trigger_id,
            observation_ref=result.result_id,
            committed_world_revision=committed.world_revision,
            ledger_sequence=committed.ledger_sequence,
            status=plan.runtime_status,
            deferred_refs=(plan.deferred_ref,) if plan.deferred_ref else (),
            projection_hint=plan.projection_hint,
        )

    def project(self, viewer: ProjectionRequest) -> WorldProjection:
        if viewer.world_id != self._world_id:
            raise PermissionError("projection request belongs to another world")
        self._projection.authorize(viewer)
        projection = (
            self._ledger.project()
            if viewer.at_cursor is None
            else self._ledger.project_at(viewer.at_cursor)
        )
        return self._projection.compile(projection, viewer)

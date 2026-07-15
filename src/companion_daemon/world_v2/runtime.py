from __future__ import annotations

import asyncio
import hashlib
import json

from .affect_math import DecayAnchor, DecayProfile, decay_intensity_bp
from .errors import ConcurrencyConflict, IdempotencyConflict
from .ledger import LedgerPort, WorldLedger
from .event_identity import domain_idempotency_key
from .clock_authority import append_clock_transition, resolve_latest_clock
from .goal_expiry_runtime import build_due_goal_expiry_events
from .pinned_turn import PinnedTurnCompiler
from .projection import ProjectionAuthority, ProjectionCompiler
from .settlement import SettlementPlanner
from .replay_evaluator import ReplayEvaluation, ReplayEvaluator
from .minimal_reply_acceptance import (
    MinimalReplyAcceptanceError,
    ReplyBudgetPolicy,
    derive_minimal_reply_material,
)
from .minimal_reply_atomic_recorder import MinimalReplyAtomicRecorder
from .minimal_reply_events import minimal_reply_event_id
from .appraisal_trigger import interaction_appraisal_trigger_events
from .batch_invariants import interaction_appraisal_trigger_identity
from .appraisal_acceptance_runtime import (
    AppraisalAcceptanceError,
    AppraisalAcceptanceRuntime,
)
from .appraisal_proposal_worker import AppraisalProposalWorker
from .affect_trigger import affect_deliberation_trigger_events
from .affect_acceptance_runtime import AffectAcceptanceError, AffectAcceptanceRuntime
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
        reply_policy: ReplyBudgetPolicy | None = None,
        reply_recorder: MinimalReplyAtomicRecorder | None = None,
        interaction_appraisal_owner: str | None = None,
        appraisal_acceptance: AppraisalAcceptanceRuntime | None = None,
        appraisal_acceptance_actor: str | None = None,
        appraisal_worker: AppraisalProposalWorker | None = None,
        affect_deliberation_owner: str | None = None,
        affect_acceptance: AffectAcceptanceRuntime | None = None,
        affect_acceptance_actor: str | None = None,
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
        if (reply_policy is None) != (reply_recorder is None):
            raise ValueError("minimal reply policy and recorder must be configured together")
        self._reply_policy = reply_policy
        self._reply_recorder = reply_recorder
        if interaction_appraisal_owner is not None and not interaction_appraisal_owner:
            raise ValueError("interaction appraisal owner must not be empty")
        self._interaction_appraisal_owner = interaction_appraisal_owner
        if (appraisal_acceptance is None) != (appraisal_acceptance_actor is None):
            raise ValueError("appraisal acceptance runtime and actor must be configured together")
        if appraisal_acceptance is not None and appraisal_acceptance.ledger is not self._ledger:
            raise ValueError("appraisal acceptance runtime must own this exact ledger")
        self._appraisal_acceptance = appraisal_acceptance
        self._appraisal_acceptance_actor = appraisal_acceptance_actor
        if appraisal_worker is not None and appraisal_worker.ledger is not self._ledger:
            raise ValueError("appraisal worker must own this exact ledger")
        if appraisal_worker is not None and interaction_appraisal_owner is None:
            raise ValueError("appraisal worker requires interaction appraisal triggers")
        self._appraisal_worker = appraisal_worker
        if affect_deliberation_owner is not None and not affect_deliberation_owner:
            raise ValueError("affect deliberation owner must not be empty")
        self._affect_deliberation_owner = affect_deliberation_owner
        if (affect_acceptance is None) != (affect_acceptance_actor is None):
            raise ValueError("affect acceptance runtime and actor must be configured together")
        if affect_acceptance is not None and affect_acceptance.ledger is not self._ledger:
            raise ValueError("affect acceptance runtime must own this exact ledger")
        self._affect_acceptance = affect_acceptance
        self._affect_acceptance_actor = affect_acceptance_actor
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

    async def _commit_accepted(self, batch, *, cursor: ProjectionCursor):
        if self._ledger.blocks_event_loop:
            return await asyncio.to_thread(self._ledger.commit_accepted, batch, expected_cursor=cursor)
        return self._ledger.commit_accepted(batch, expected_cursor=cursor)

    async def evaluate_replay(self, *, evaluator: ReplayEvaluator | None = None) -> ReplayEvaluation:
        """Run deterministic diagnostics without model calls or side effects."""

        evidence_export = getattr(self._ledger, "export_replay_evidence", None)
        if callable(evidence_export):
            if self._ledger.blocks_event_loop:
                evidence = await asyncio.to_thread(evidence_export)
            else:
                evidence = evidence_export()
            return (evaluator or ReplayEvaluator()).evaluate(evidence=evidence)
        rebuild = getattr(self._ledger, "rebuild", None)
        if not callable(rebuild):
            raise ValueError("configured ledger does not expose deterministic replay")
        if self._ledger.blocks_event_loop:
            projection = await asyncio.to_thread(self._ledger.project)
            replay = await asyncio.to_thread(rebuild)
        else:
            projection, replay = self._ledger.project(), rebuild()
        return (evaluator or ReplayEvaluator()).evaluate(projection=projection, replay=replay)

    async def accept_appraisal_proposal(self, proposal_id: str) -> RuntimeOutcome:
        """Atomically consume one already-persisted appraisal proposal.

        Proposal production remains outside this method; it may use an LLM or
        a deterministic continuation, but it cannot materialize an accepted
        effect.  This Runtime seam pins the exact current cursor and delegates
        only to the opaque Appraisal acceptance recorder.
        """

        if self._appraisal_acceptance is None or self._appraisal_acceptance_actor is None:
            raise ValueError("appraisal acceptance is not configured")
        if not proposal_id:
            raise ValueError("appraisal proposal id must not be empty")
        async with self._lock:
            projection = await self._project_for_write()
            existing = next(
                (item for item in projection.acceptance_decisions if item.proposal_id == proposal_id),
                None,
            )
            if existing is not None:
                located = await self._lookup_event_commit(existing.acceptance_event_ref or "")
                if located is None:
                    raise RuntimeError("accepted appraisal decision has no durable manifest")
                manifest = located[0].payload()
                trigger_id = manifest.get("trigger_id")
                if not isinstance(trigger_id, str) or not trigger_id:
                    raise RuntimeError("accepted appraisal manifest has no trigger identity")
                proposal_event_ref = manifest.get("proposal_event_ref")
                proposal_payload_hash = manifest.get("proposal_event_payload_hash")
                if not isinstance(proposal_event_ref, str) or not isinstance(proposal_payload_hash, str):
                    raise RuntimeError("accepted appraisal manifest has no proposal provenance")
                proposal_located = await self._lookup_event_commit(proposal_event_ref)
                if proposal_located is None or proposal_located[0].payload_hash != proposal_payload_hash:
                    raise RuntimeError("accepted appraisal proposal provenance is not durable")
                source_evidence_ref = proposal_located[0].payload().get("source_evidence_ref")
                if not isinstance(source_evidence_ref, str) or not source_evidence_ref:
                    raise RuntimeError("accepted appraisal proposal has no source evidence")
                return RuntimeOutcome(
                    outcome_id=f"outcome:appraisal:{proposal_id}",
                    trigger_id=trigger_id,
                    observation_ref=source_evidence_ref,
                    committed_world_revision=projection.world_revision,
                    ledger_sequence=projection.ledger_sequence,
                    status="observed_only",
                    projection_hint=f"world-revision:{projection.world_revision}",
                )
            proposal = next(
                (item for item in projection.appraisal_proposals if item.proposal_id == proposal_id),
                None,
            )
            if proposal is None:
                return RuntimeOutcome(
                    outcome_id=f"outcome:appraisal:{proposal_id}",
                    trigger_id=f"trigger:appraisal:{proposal_id}",
                    committed_world_revision=projection.world_revision,
                    ledger_sequence=projection.ledger_sequence,
                    status="deferred",
                    deferred_refs=("appraisal.proposal_unavailable",),
                    projection_hint=f"world-revision:{projection.world_revision}",
                )
            cursor = ProjectionCursor(
                world_revision=projection.world_revision,
                deliberation_revision=projection.deliberation_revision,
                ledger_sequence=projection.ledger_sequence,
            )
            try:
                handle = self._appraisal_acceptance.pin_proposal(cursor=cursor, proposal_id=proposal_id)
                if self._ledger.blocks_event_loop:
                    committed = await asyncio.to_thread(
                        self._appraisal_acceptance.accept_runtime_owned,
                        handle=handle,
                        actor=self._appraisal_acceptance_actor,
                        source="world-runtime:appraisal-acceptance",
                    )
                else:
                    committed = self._appraisal_acceptance.accept_runtime_owned(
                        handle=handle,
                        actor=self._appraisal_acceptance_actor,
                        source="world-runtime:appraisal-acceptance",
                    )
            except (AppraisalAcceptanceError, ConcurrencyConflict) as exc:
                code = exc.code if isinstance(exc, AppraisalAcceptanceError) else "appraisal.stale_cursor"
                return RuntimeOutcome(
                    outcome_id=f"outcome:appraisal:{proposal_id}",
                    trigger_id=proposal.trigger_id,
                    observation_ref=proposal.source_evidence_ref,
                    committed_world_revision=projection.world_revision,
                    ledger_sequence=projection.ledger_sequence,
                    status="deferred",
                    deferred_refs=(code,),
                    projection_hint=f"world-revision:{projection.world_revision}",
                )
        return RuntimeOutcome(
            outcome_id=f"outcome:appraisal:{proposal_id}",
            trigger_id=proposal.trigger_id,
            observation_ref=proposal.source_evidence_ref,
            committed_world_revision=committed.world_revision,
            ledger_sequence=committed.ledger_sequence,
            status="observed_only",
            projection_hint=f"world-revision:{committed.world_revision}",
        )

    async def accept_affect_proposal(self, proposal_id: str) -> RuntimeOutcome:
        """Atomically consume one persisted Affect proposal at its exact cursor."""

        if self._affect_acceptance is None or self._affect_acceptance_actor is None:
            raise ValueError("affect acceptance is not configured")
        if not proposal_id:
            raise ValueError("affect proposal id must not be empty")
        async with self._lock:
            projection = await self._project_for_write()
            existing = next(
                (item for item in projection.acceptance_decisions if item.proposal_id == proposal_id),
                None,
            )
            if existing is not None:
                if existing.status != "accepted":
                    return RuntimeOutcome(
                        outcome_id=f"outcome:affect:{proposal_id}",
                        trigger_id=f"affect:{proposal_id}",
                        committed_world_revision=projection.world_revision,
                        ledger_sequence=projection.ledger_sequence,
                        status="observed_only",
                        terminal_errors=(f"affect.proposal_{existing.status}",),
                        projection_hint=f"world-revision:{projection.world_revision}",
                    )
                if existing.manifest_version != "affect-acceptance.1":
                    return RuntimeOutcome(
                        outcome_id=f"outcome:affect:{proposal_id}",
                        trigger_id=f"affect:{proposal_id}",
                        committed_world_revision=projection.world_revision,
                        ledger_sequence=projection.ledger_sequence,
                        status="failed_safe",
                        terminal_errors=("affect.acceptance_not_runtime_owned",),
                        projection_hint=f"world-revision:{projection.world_revision}",
                    )
                located = await self._lookup_event_commit(existing.acceptance_event_ref or "")
                if located is None:
                    raise RuntimeError("accepted affect decision has no durable manifest")
                manifest = located[0].payload()
                proposal_event_ref = manifest.get("proposal_event_ref")
                proposal_payload_hash = manifest.get("proposal_event_payload_hash")
                if not isinstance(proposal_event_ref, str) or not isinstance(proposal_payload_hash, str):
                    raise RuntimeError("accepted affect manifest has no proposal provenance")
                proposal_located = await self._lookup_event_commit(proposal_event_ref)
                if proposal_located is None or proposal_located[0].payload_hash != proposal_payload_hash:
                    raise RuntimeError("accepted affect proposal provenance is not durable")
                proposal_payload = proposal_located[0].payload()
                if (
                    proposal_payload.get("proposal_id") != proposal_id
                    or proposal_payload.get("proposal_kind") != "affect_transition"
                ):
                    raise RuntimeError("accepted affect proposal provenance has the wrong identity")
                return RuntimeOutcome(
                    outcome_id=f"outcome:affect:{proposal_id}",
                    trigger_id=f"affect:{proposal_id}",
                    committed_world_revision=projection.world_revision,
                    ledger_sequence=projection.ledger_sequence,
                    status="observed_only",
                    projection_hint=f"world-revision:{projection.world_revision}",
                )
            proposal = next(
                (item for item in projection.affect_proposals if item.proposal_id == proposal_id),
                None,
            )
            if proposal is None:
                return RuntimeOutcome(
                    outcome_id=f"outcome:affect:{proposal_id}",
                    trigger_id=f"affect:{proposal_id}",
                    committed_world_revision=projection.world_revision,
                    ledger_sequence=projection.ledger_sequence,
                    status="deferred",
                    deferred_refs=("affect.proposal_unavailable",),
                    projection_hint=f"world-revision:{projection.world_revision}",
                )
            cursor = ProjectionCursor(
                world_revision=projection.world_revision,
                deliberation_revision=projection.deliberation_revision,
                ledger_sequence=projection.ledger_sequence,
            )
            try:
                handle = self._affect_acceptance.pin_proposal(cursor=cursor, proposal_id=proposal_id)
                if self._ledger.blocks_event_loop:
                    committed = await asyncio.to_thread(
                        self._affect_acceptance.accept_runtime_owned,
                        handle=handle,
                        actor=self._affect_acceptance_actor,
                        source="world-runtime:affect-acceptance",
                    )
                else:
                    committed = self._affect_acceptance.accept_runtime_owned(
                        handle=handle,
                        actor=self._affect_acceptance_actor,
                        source="world-runtime:affect-acceptance",
                    )
            except (AffectAcceptanceError, ConcurrencyConflict) as exc:
                code = exc.code if isinstance(exc, AffectAcceptanceError) else "affect.stale_cursor"
                return RuntimeOutcome(
                    outcome_id=f"outcome:affect:{proposal_id}",
                    trigger_id=f"affect:{proposal_id}",
                    committed_world_revision=projection.world_revision,
                    ledger_sequence=projection.ledger_sequence,
                    status="deferred",
                    deferred_refs=(code,),
                    projection_hint=f"world-revision:{projection.world_revision}",
                )
        return RuntimeOutcome(
            outcome_id=f"outcome:affect:{proposal_id}",
            trigger_id=f"affect:{proposal_id}",
            committed_world_revision=committed.world_revision,
            ledger_sequence=committed.ledger_sequence,
            status="observed_only",
            projection_hint=f"world-revision:{committed.world_revision}",
        )

    async def reject_affect_proposal(self, proposal_id: str) -> RuntimeOutcome:
        """Record a no-Affect decision without granting a mutation write path.

        A current proposal is rejected; a proposal pinned before a later world
        change is recorded as stale.  Both decisions are durable and discard
        the proposal through the existing typed-proposal reducer registry.
        """

        if not proposal_id:
            raise ValueError("affect proposal id must not be empty")
        async with self._lock:
            projection = await self._project_for_write()
            existing = next(
                (item for item in projection.acceptance_decisions if item.proposal_id == proposal_id),
                None,
            )
            if existing is not None:
                return RuntimeOutcome(
                    outcome_id=f"outcome:affect:{proposal_id}",
                    trigger_id=f"affect:{proposal_id}",
                    committed_world_revision=projection.world_revision,
                    ledger_sequence=projection.ledger_sequence,
                    status="observed_only" if existing.status != "accepted" else "failed_safe",
                    terminal_errors=(f"affect.proposal_{existing.status}",),
                    projection_hint=f"world-revision:{projection.world_revision}",
                )
            proposal = next(
                (item for item in projection.affect_proposals if item.proposal_id == proposal_id),
                None,
            )
            if proposal is None:
                return RuntimeOutcome(
                    outcome_id=f"outcome:affect:{proposal_id}",
                    trigger_id=f"affect:{proposal_id}",
                    committed_world_revision=projection.world_revision,
                    ledger_sequence=projection.ledger_sequence,
                    status="deferred",
                    deferred_refs=("affect.proposal_unavailable",),
                    projection_hint=f"world-revision:{projection.world_revision}",
                )
            decision_status = (
                "rejected"
                if proposal.evaluated_world_revision == projection.world_revision
                else "stale"
            )
            proposal_located = await self._lookup_event_commit(proposal.recorded_event_ref or "")
            if (
                proposal_located is None
                or proposal.recorded_event_payload_hash != proposal_located[0].payload_hash
            ):
                raise RuntimeError("affect proposal provenance is not durable")
            proposal_event = proposal_located[0]
            material = {
                "world_id": self._world_id,
                "proposal_id": proposal_id,
                "evaluated_world_revision": proposal.evaluated_world_revision,
                "status": decision_status,
            }
            digest = hashlib.sha256(
                json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            payload = {
                "acceptance_id": f"acceptance:affect-decision:{digest}",
                "status": decision_status,
                "proposal_id": proposal_id,
                "evaluated_world_revision": proposal.evaluated_world_revision,
                "accepted_change_id": None,
                "accepted_change_hash": None,
            }
            idempotency_key = domain_idempotency_key(
                event_type="AcceptanceRecorded", world_id=self._world_id, payload=payload
            )
            if idempotency_key is None:
                raise RuntimeError("affect decision has no installed event identity")
            event = WorldEvent.from_payload(
                schema_version="world-v2.1",
                event_id=f"event:affect-decision:{digest}",
                world_id=self._world_id,
                event_type="AcceptanceRecorded",
                logical_time=proposal_event.logical_time,
                created_at=proposal_event.created_at,
                actor="world-runtime:affect-decision",
                source="world-runtime:affect-decision",
                trace_id=proposal_event.trace_id,
                causation_id=proposal_event.event_id,
                correlation_id=proposal_event.correlation_id,
                idempotency_key=idempotency_key,
                payload=payload,
            )
            committed = await self._commit(
                [event],
                world_revision=projection.world_revision,
                deliberation_revision=projection.deliberation_revision,
                commit_id=f"commit:affect-decision:{digest}",
            )
        return RuntimeOutcome(
            outcome_id=f"outcome:affect:{proposal_id}",
            trigger_id=f"affect:{proposal_id}",
            committed_world_revision=committed.world_revision,
            ledger_sequence=committed.ledger_sequence,
            status="observed_only",
            terminal_errors=(f"affect.proposal_{decision_status}",),
            projection_hint=f"world-revision:{committed.world_revision}",
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
            idempotency_key=domain_idempotency_key(
                event_type="ObservationRecorded",
                world_id=self._world_id,
                payload=observation.model_dump(mode="json"),
            )
            or f"observation:{observation.source}:{observation.source_event_id}",
            payload=observation.model_dump(mode="json"),
        )
        reply_authorized = False
        authorized_action_ids: tuple[str, ...] = ()
        reply_deferred_refs: tuple[str, ...] = ()
        reply_terminal_errors: tuple[str, ...] = ()
        audited = None
        async with self._lock:
            existing = await self._lookup_event_commit(event.event_id)
            if existing is not None:
                persisted, original_commit = existing
                if persisted != event:
                    raise IdempotencyConflict(
                        "observation trigger was already committed with different content"
                    )
                return await self._existing_observation_outcome(
                    observation=observation,
                    observation_event=persisted,
                    original_commit=original_commit,
                    trigger_id=trigger_id,
                )
            before = await self._project_for_write()
            committed = await self._commit(
                [event],
                world_revision=before.world_revision,
                deliberation_revision=before.deliberation_revision,
            )
            if self._pinned_turn is not None:
                audited = await self._pinned_turn.audit_observation(
                    observation=observation,
                    observation_event=event,
                    cursor=ProjectionCursor(
                        world_revision=committed.world_revision,
                        deliberation_revision=committed.deliberation_revision,
                        ledger_sequence=committed.ledger_sequence,
                    ),
                )
            if self._interaction_appraisal_owner is not None:
                trigger_events = interaction_appraisal_trigger_events(
                    observation=observation,
                    observation_event=event,
                    owner_id=self._interaction_appraisal_owner,
                )
                trigger_head = await self._project_for_write()
                committed = await self._commit(
                    list(trigger_events),
                    world_revision=trigger_head.world_revision,
                    deliberation_revision=trigger_head.deliberation_revision,
                )
            if self._appraisal_worker is not None and audited is not None and audited.proposal_id:
                after_audit = await self._project_for_write()
                audit = next(
                    (
                        item
                        for item in after_audit.proposal_audits
                        if item.proposal_id == audited.proposal_id
                    ),
                    None,
                )
                if audit is not None and audit.proposal_kind == "decision":
                    try:
                        cursor = ProjectionCursor(
                            world_revision=committed.world_revision,
                            deliberation_revision=committed.deliberation_revision,
                            ledger_sequence=committed.ledger_sequence,
                        )
                        if self._ledger.blocks_event_loop:
                            work = await asyncio.to_thread(
                                self._appraisal_worker.process,
                                world_id=self._world_id,
                                cursor=cursor,
                                proposal_id=audited.proposal_id,
                            )
                        else:
                            work = self._appraisal_worker.process(
                                world_id=self._world_id,
                                cursor=cursor,
                                proposal_id=audited.proposal_id,
                            )
                        if (
                            self._affect_deliberation_owner is not None
                            and work.status == "accepted"
                            and work.acceptance_commit is not None
                        ):
                            appraisal_event = next(
                                (
                                    located[0]
                                    for event_id in work.acceptance_commit.event_ids
                                    if (located := self._ledger.lookup_event_commit(event_id)) is not None
                                    and located[0].event_type == "AppraisalAccepted"
                                ),
                                None,
                            )
                            if appraisal_event is None:
                                raise RuntimeError("accepted appraisal has no durable mutation event")
                            trigger_head = await self._project_for_write()
                            committed = await self._commit(
                                list(
                                    affect_deliberation_trigger_events(
                                        appraisal_event=appraisal_event,
                                        owner_id=self._affect_deliberation_owner,
                                    )
                                ),
                                world_revision=trigger_head.world_revision,
                                deliberation_revision=trigger_head.deliberation_revision,
                            )
                    except (AppraisalAcceptanceError, ConcurrencyConflict, ValueError) as exc:
                        code = getattr(exc, "code", "appraisal.worker_failed")
                        reply_deferred_refs = (*reply_deferred_refs, str(code))
            if self._pinned_turn is not None and audited is not None:
                if self._reply_policy is not None and audited.proposal_id is not None:
                    after_audit = await self._project_for_write()
                    audit = next(
                        (item for item in after_audit.proposal_audits if item.proposal_id == audited.proposal_id),
                        None,
                    )
                    account = next(
                        (item for item in after_audit.budget_accounts if item.account_id == self._reply_policy.account_id),
                        None,
                    )
                    if audit is not None and audit.proposal_kind == "minimal":
                        if account is None:
                            reply_deferred_refs = (
                                f"reply-budget-account:{self._reply_policy.account_id}",
                            )
                        else:
                            try:
                                material = derive_minimal_reply_material(
                                    audit=audit,
                                    cursor=ProjectionCursor(
                                        world_revision=after_audit.world_revision,
                                        deliberation_revision=after_audit.deliberation_revision,
                                        ledger_sequence=after_audit.ledger_sequence,
                                    ),
                                    world_id=self._world_id,
                                    policy=self._reply_policy,
                                    account=account,
                                    logical_time=after_audit.logical_time or observation.logical_time,
                                    created_at=observation.created_at,
                                    trace_id=observation.trace_id,
                                    correlation_id=observation.correlation_id,
                                )
                            except MinimalReplyAcceptanceError as exc:
                                if exc.code in {
                                    "minimal_reply_acceptance.budget_unavailable",
                                    "minimal_reply_acceptance.budget_account_unavailable",
                                }:
                                    reply_deferred_refs = (exc.code,)
                                else:
                                    reply_terminal_errors = (exc.code,)
                            else:
                                assert self._reply_recorder is not None
                                batch = self._reply_recorder.prepare_batch(
                                    acceptance_id=f"acceptance:minimal-reply:{audit.proposal_id}",
                                    material=material,
                                    actor=self._reply_policy.actor,
                                    source="world-runtime:acceptance",
                                )
                                committed = await self._commit_accepted(batch, cursor=material.cursor)
                                reply_authorized = True
                                authorized_action_ids = (material.action.action_id,)
        if reply_authorized:
            status = "action_authorized"
        elif reply_terminal_errors:
            status = "failed_safe"
        elif reply_deferred_refs:
            status = "deferred"
        else:
            status = "observed_only"
        return RuntimeOutcome(
            outcome_id=f"outcome:{trigger_id}",
            trigger_id=trigger_id,
            observation_ref=observation.observation_id,
            committed_world_revision=committed.world_revision,
            ledger_sequence=committed.ledger_sequence,
            status=status,
            authorized_action_ids=authorized_action_ids if reply_authorized else (),
            deferred_refs=reply_deferred_refs,
            terminal_errors=reply_terminal_errors,
            projection_hint=f"world-revision:{committed.world_revision}",
        )

    async def _existing_observation_outcome(
        self,
        *,
        observation: Observation,
        observation_event: WorldEvent,
        original_commit: CommitResult,
        trigger_id: str,
    ) -> RuntimeOutcome:
        """Join a completed minimal-reply acceptance without repeating model work.

        The Observation itself commits before its deliberation and acceptance
        follow-ups.  On ingress retry, the durable minimal manifest is the
        authority for the final visible outcome; returning the Observation's
        old cursor would incorrectly erase an already-authorized reply.
        """

        projection = await self._project_for_write()
        manifest = next(
            (
                item
                for item in projection.minimal_reply_manifests
                if any(
                    audit.proposal_id == item.proposal_id
                    and audit.event_ref == item.proposal_event_ref
                    and audit.trigger_ref == observation_event.event_id
                    for audit in projection.proposal_audits
                )
            ),
            None,
        )
        if manifest is None:
            if self._interaction_appraisal_owner is not None and any(
                item.trigger_id
                == interaction_appraisal_trigger_identity(self._world_id, observation.observation_id)
                for item in projection.trigger_processes
            ):
                return RuntimeOutcome(
                    outcome_id=f"outcome:{trigger_id}",
                    trigger_id=trigger_id,
                    observation_ref=observation.observation_id,
                    committed_world_revision=projection.world_revision,
                    ledger_sequence=projection.ledger_sequence,
                    status="observed_only",
                    projection_hint=f"world-revision:{projection.world_revision}",
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
        action_event_id = minimal_reply_event_id(
            manifest_hash=manifest.manifest_hash,
            role="action",
            stable_id=manifest.action_id,
        )
        persisted = await self._lookup_event_commit(action_event_id)
        if persisted is None:
            raise RuntimeError("minimal reply manifest has no durable action event")
        action_event, committed = persisted
        if action_event.event_type != "ActionAuthorized":
            raise RuntimeError("minimal reply action identity resolves to another event type")
        return RuntimeOutcome(
            outcome_id=f"outcome:{trigger_id}",
            trigger_id=trigger_id,
            observation_ref=observation.observation_id,
            committed_world_revision=committed.world_revision,
            ledger_sequence=committed.ledger_sequence,
            status="action_authorized",
            authorized_action_ids=(manifest.action_id,),
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

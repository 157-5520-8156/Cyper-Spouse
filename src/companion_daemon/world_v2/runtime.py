from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from uuid import uuid4
from datetime import UTC, datetime, timedelta

from .affect_math import DecayAnchor, DecayProfile, decay_intensity_bp
from .errors import ConcurrencyConflict, IdempotencyConflict
from .ledger import LedgerPort, WorldLedger
from .event_identity import domain_idempotency_key
from .acceptance_manifest import (
    AcceptanceManifestV2,
    canonical_acceptance_manifest_hash,
    derive_acceptance_manifest_proposal_v2,
)
from .clock_authority import append_clock_transition, resolve_latest_clock
from .goal_expiry_runtime import build_due_goal_expiry_events
from .occurrence_clock_continuation import build_occurrence_clock_events
from .outcome_observation_runtime import build_outcome_observation_event
from .pinned_turn import PinnedTurnCompiler
from .expression_episode_lifecycle import (
    EXPRESSION_FRESH_CONTEXT_REPIN_LIMIT,
    due_expression_retry_processes,
    expression_episode_claim_event,
    expression_episode_complete_event,
    expression_episode_has_authorized_action,
    expression_episode_open_event,
    expression_episode_repin_reservation_event,
    expression_episode_technical_failure_count,
    expression_episode_trigger_id,
    expression_episode_work_due,
)
from .expression_cadence import CadenceDraw, record_cadence_draws
from .interactive_turn_budget import (
    InteractiveTurnBudget,
    InteractiveTurnBudgetPolicy,
)
from .production_latency_trace import ProductionLatencyRecorder
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
from .expression_plan_acceptance import (
    ExpressionPlanAcceptanceError,
    ExpressionPlanBudgetPolicy,
    derive_expression_plan_material,
)
from .expression_plan_atomic_recorder import ExpressionPlanAtomicRecorder, expression_plan_event_id
from .expression_payload_store import ImmutableExpressionPayloadStore
from .appraisal_trigger import (
    CHARACTER_INTERIOR_INBOUND_ATTEMPT_PREFIX,
    DEFAULT_INTERACTION_APPRAISAL_LEASE_SECONDS,
    interaction_appraisal_folded_event,
    interaction_appraisal_trigger_events,
    is_interaction_appraisal_audit,
)
from .fact_trigger import interaction_fact_trigger_event
from .fact_draft_adapter import FactObservationProposalAdapter
from .fact_memory_candidate_lifecycle import FactMemoryCandidateLifecycle
from .fact_v2_acceptance_runtime import FactV2AcceptanceRuntime
from .interaction_fact_trigger_runtime import FactTriggerRunResult, InteractionFactTriggerRuntime
from .character_interior import CharacterInterior
from .character_interior.inbound_relationship import InboundRelationshipSignalWorker
from .batch_invariants import interaction_appraisal_trigger_identity
from .appraisal_acceptance_runtime import (
    AppraisalAcceptanceError,
    AppraisalAcceptanceRuntime,
)
from .appraisal_proposal_worker import AppraisalProposalWorker
from .immediate_emotion_proposal_worker import ImmediateEmotionProposalWorker
from .affect_acceptance_runtime import AffectAcceptanceError, AffectAcceptanceRuntime
from .relationship_adjustment_worker import RelationshipAdjustmentWorker
from .relationship_adjustment_trigger_runtime import RelationshipAdjustmentTriggerRuntime
from .outcome_deliberation_turn import OutcomeDeliberationTurn
from .outcome_proposal_worker import OutcomeProposalWorker
from .outcome_trigger_runtime import OutcomeTriggerRunResult, OutcomeTriggerRuntime
from .outcome_trigger import outcome_deliberation_trigger_event, outcome_deliberation_trigger_id
from .action_pump import (
    ActionExecutor,
    ActionPump,
    ActionPumpResult,
    ProviderAcceptedReconciliationGate,
)
from .bounded_decision_vertical import AnchoredRunResult
from .expression_reconsideration import expression_reconsideration_events_for_observation
from .random_authority import RandomAuthority
from .perception_trigger import perception_trigger_event
from .perception_trigger_runtime import PerceptionTriggerRunResult, PerceptionTriggerRuntime
from .social_action_worker import SocialActionRunResult, SocialActionWorker
from .memory_withdrawal_review import (
    MemoryWithdrawalReviewRunResult,
    MemoryWithdrawalReviewRuntime,
)
from .proposal_audit import ProposalAuditCommit
from .proposal_envelope import DecisionProposal, MinimalProposal, validate_proposal_envelope
from .unified_inbound_decision import inspect_unified_inbound_decision
from .response_expectation_view import pending_response_expectation_manifest
from .schemas import (
    ClockObservation,
    CommitResult,
    ExternalObservation,
    OutcomeObservation,
    Observation,
    ProjectionCursor,
    ProjectionRequest,
    ResponseExpectationAssessedPayload,
    RuntimeOutcome,
    ClaimLease,
    TriggerProcess,
    WorldEvent,
    WorldProjection,
)


# Immutable V2 ledgers may contain proposals written by the retired independent
# quick-reaction author.  The prefix remains only as replay/join discrimination;
# no live worker, model port or composition switch is retained.
_HISTORICAL_QUICK_REACTION_PROPOSAL_PREFIX = "proposal:quick-reaction:"


_LOG = logging.getLogger(__name__)
_INGRESS_CAS_MAX_ATTEMPTS = 8


def _user_perceived_ms(observation: Observation) -> str | None:
    """Wall-clock elapsed since the user's first fragment arrived, if known.

    The QQ ingress store stamps ``window_opened_at`` (first fragment
    ``received_at``) into the batch metadata.  This is observability only —
    never an authority input — so a missing or malformed stamp reads as None.
    """

    opened_raw = (observation.coalescing_metadata or {}).get("window_opened_at")
    if not isinstance(opened_raw, str):
        return None
    try:
        opened = datetime.fromisoformat(opened_raw)
    except ValueError:
        return None
    if opened.tzinfo is None or opened.utcoffset() is None:
        return None
    return f"{(datetime.now(UTC) - opened).total_seconds() * 1000:.1f}"


def _matches_outcome_observation_command(
    event: WorldEvent, observation: OutcomeObservation
) -> bool:
    """Compare the immutable command image without re-resolving current state."""

    if (
        event.event_type != "OutcomeObservationRecorded"
        or event.world_id != observation.world_id
        or event.logical_time != observation.logical_time
        or event.created_at != observation.created_at
        or event.trace_id != observation.trace_id
        or event.causation_id != observation.causation_id
        or event.correlation_id != observation.correlation_id
    ):
        return False
    return event.payload().get("observation") == observation.as_projection().model_dump(mode="json")


def _observation_ingress_payload_hash(observation: Observation) -> str:
    """Recompute the transport digest from the exact persisted ingress fields."""

    if (
        observation.text is not None
        and not observation.attachment_refs
        and not observation.coalescing_metadata
    ):
        return hashlib.sha256(observation.text.encode("utf-8")).hexdigest()
    payload = {
        "text": observation.text,
        "attachment_refs": observation.attachment_refs,
        "coalescing_metadata": observation.coalescing_metadata,
    }
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


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
        expression_episode_owner: str | None = None,
        expression_retry_budget_policy: InteractiveTurnBudgetPolicy | None = None,
        reply_policy: ReplyBudgetPolicy | None = None,
        reply_recorder: MinimalReplyAtomicRecorder | None = None,
        expression_policy: ExpressionPlanBudgetPolicy | None = None,
        expression_recorder: ExpressionPlanAtomicRecorder | None = None,
        expression_payload_store: ImmutableExpressionPayloadStore | None = None,
        inbound_state_owner: str | None = None,
        appraisal_acceptance: AppraisalAcceptanceRuntime | None = None,
        appraisal_acceptance_actor: str | None = None,
        appraisal_worker: AppraisalProposalWorker | None = None,
        immediate_emotion_worker: ImmediateEmotionProposalWorker | None = None,
        inbound_relationship_worker: InboundRelationshipSignalWorker | None = None,
        outcome_deliberation_turn: OutcomeDeliberationTurn | None = None,
        outcome_worker: OutcomeProposalWorker | None = None,
        outcome_deliberation_owner: str | None = None,
        interaction_fact_owner: str | None = None,
        fact_acceptance: FactV2AcceptanceRuntime | None = None,
        fact_adapter: FactObservationProposalAdapter | None = None,
        fact_memory_lifecycle: FactMemoryCandidateLifecycle | None = None,
        fact_memory_actor_ref: str | None = None,
        character_interior: CharacterInterior | None = None,
        reflection_scheduler: object | None = None,
        relationship_adjustment_owner: str | None = None,
        relationship_adjustment_worker: RelationshipAdjustmentWorker | None = None,
        action_executor: ActionExecutor | None = None,
        action_pump_owner: str | None = None,
        action_pump_excluded_kinds: frozenset[str] = frozenset(),
        affect_acceptance: AffectAcceptanceRuntime | None = None,
        affect_acceptance_actor: str | None = None,
        social_action_worker: SocialActionWorker | None = None,
        memory_withdrawal_review: MemoryWithdrawalReviewRuntime | None = None,
        perception_owner: str | None = None,
        perception_trigger_runtime: PerceptionTriggerRuntime | None = None,
        latency_recorder: ProductionLatencyRecorder | None = None,
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
        if expression_episode_owner is not None and not expression_episode_owner:
            raise ValueError("expression episode owner must not be empty")
        # This lease owner identifies one live Runtime instance, not merely a
        # deployment role.  A process-stable constant makes another daemon
        # indistinguishable from the worker that is still inside its provider
        # call, so a second runtime can generate the same reply concurrently.
        self._expression_episode_owner = expression_episode_owner or (
            f"worker:world-v2:expression-episode:{uuid4().hex}"
        )
        self._expression_retry_budget_policy = (
            expression_retry_budget_policy or InteractiveTurnBudgetPolicy()
        )
        if (reply_policy is None) != (reply_recorder is None):
            raise ValueError("minimal reply policy and recorder must be configured together")
        self._reply_policy = reply_policy
        self._reply_recorder = reply_recorder
        if (expression_policy is None) != (expression_recorder is None):
            raise ValueError("expression plan policy and recorder must be configured together")
        if expression_payload_store is not None and expression_policy is None:
            raise ValueError("expression payload store requires expression plan acceptance")
        self._expression_policy = expression_policy
        self._expression_recorder = expression_recorder
        self._expression_payload_store = expression_payload_store
        if inbound_state_owner is not None and not inbound_state_owner:
            raise ValueError("inbound state owner must not be empty")
        self._inbound_state_owner = inbound_state_owner
        if (appraisal_acceptance is None) != (appraisal_acceptance_actor is None):
            raise ValueError("appraisal acceptance runtime and actor must be configured together")
        if appraisal_acceptance is not None and appraisal_acceptance.ledger is not self._ledger:
            raise ValueError("appraisal acceptance runtime must own this exact ledger")
        self._appraisal_acceptance = appraisal_acceptance
        self._appraisal_acceptance_actor = appraisal_acceptance_actor
        if appraisal_worker is not None and appraisal_worker.ledger is not self._ledger:
            raise ValueError("appraisal worker must own this exact ledger")
        if (
            appraisal_worker is not None
            and inbound_state_owner is None
            and pinned_turn is None
        ):
            raise ValueError(
                "appraisal worker requires an inbound proposal or appraisal triggers"
            )
        self._appraisal_worker = appraisal_worker
        if immediate_emotion_worker is not None:
            if appraisal_worker is None:
                raise ValueError("immediate emotion worker requires an appraisal authority")
            if pinned_turn is None:
                raise ValueError(
                    "immediate emotion worker requires the unified inbound proposal lane"
                )
            if immediate_emotion_worker.ledger is not self._ledger:
                raise ValueError("immediate emotion worker must own this exact ledger")
        self._immediate_emotion_worker = immediate_emotion_worker
        if (
            inbound_relationship_worker is not None
            and inbound_relationship_worker.ledger is not self._ledger
        ):
            raise ValueError("inbound relationship worker must own this exact ledger")
        self._inbound_relationship_worker = inbound_relationship_worker
        if outcome_deliberation_owner is not None and not outcome_deliberation_owner:
            raise ValueError("outcome deliberation owner must not be empty")
        if outcome_worker is not None and outcome_worker.ledger is not self._ledger:
            raise ValueError("outcome worker must own this exact ledger")
        if (outcome_deliberation_turn is None) != (outcome_worker is None):
            raise ValueError("outcome deliberation turn and worker must be configured together")
        if outcome_worker is not None and outcome_deliberation_owner is None:
            raise ValueError("outcome worker requires an outcome deliberation owner")
        self._outcome_deliberation_turn = outcome_deliberation_turn
        self._outcome_worker = outcome_worker
        self._outcome_deliberation_owner = outcome_deliberation_owner
        if interaction_fact_owner is not None and not interaction_fact_owner:
            raise ValueError("interaction fact owner must not be empty")
        if (fact_acceptance is None) != (fact_adapter is None):
            raise ValueError("Fact acceptance and adapter must be configured together")
        if (fact_acceptance is None) != (interaction_fact_owner is None):
            raise ValueError("Fact acceptance requires an interaction fact worker owner")
        if fact_acceptance is not None and fact_acceptance.ledger is not self._ledger:
            raise ValueError("Fact acceptance runtime must own this exact ledger")
        self._interaction_fact_owner = interaction_fact_owner
        self._fact_acceptance = fact_acceptance
        self._fact_adapter = fact_adapter
        if fact_memory_lifecycle is not None and character_interior is None:
            raise ValueError("Fact memory lifecycle requires CharacterInterior")
        if fact_memory_lifecycle is not None and not fact_memory_actor_ref:
            raise ValueError("Fact memory lifecycle requires its character actor")
        if fact_memory_lifecycle is None and fact_memory_actor_ref is not None:
            raise ValueError("Fact memory actor requires the memory lifecycle")
        self._fact_memory_lifecycle = fact_memory_lifecycle
        self._fact_memory_actor_ref = fact_memory_actor_ref
        if character_interior is not None and not character_interior._is_bound_to(  # noqa: SLF001
            self._ledger
        ):
            raise ValueError("CharacterInterior must own this exact ledger")
        self._character_interior = character_interior
        self._reflection_scheduler = reflection_scheduler
        if relationship_adjustment_owner is not None and not relationship_adjustment_owner:
            raise ValueError("relationship adjustment owner must not be empty")
        self._relationship_adjustment_owner = relationship_adjustment_owner
        if (
            relationship_adjustment_worker is not None
            and relationship_adjustment_worker.ledger is not self._ledger
        ):
            raise ValueError("relationship adjustment worker must own this exact ledger")
        if relationship_adjustment_worker is not None and relationship_adjustment_owner is None:
            raise ValueError("relationship adjustment worker requires an adjustment owner")
        self._relationship_adjustment_worker = relationship_adjustment_worker
        if (action_executor is None) != (action_pump_owner is None):
            raise ValueError("action executor and action pump owner must be configured together")
        if action_pump_owner is not None and not action_pump_owner:
            raise ValueError("action pump owner must not be empty")
        self._action_executor = action_executor
        self._action_pump_owner = action_pump_owner
        self._action_pump_excluded_kinds = action_pump_excluded_kinds
        if (affect_acceptance is None) != (affect_acceptance_actor is None):
            raise ValueError("affect acceptance runtime and actor must be configured together")
        if affect_acceptance is not None and affect_acceptance.ledger is not self._ledger:
            raise ValueError("affect acceptance runtime must own this exact ledger")
        self._affect_acceptance = affect_acceptance
        self._affect_acceptance_actor = affect_acceptance_actor
        if social_action_worker is not None and social_action_worker.ledger is not self._ledger:
            raise ValueError("social action worker must own this exact ledger")
        self._social_action_worker = social_action_worker
        if (
            memory_withdrawal_review is not None
            and memory_withdrawal_review.ledger is not self._ledger
        ):
            raise ValueError("memory withdrawal review must own this exact ledger")
        self._memory_withdrawal_review = memory_withdrawal_review
        if (perception_owner is None) != (perception_trigger_runtime is None):
            raise ValueError("perception owner and trigger runtime must be configured together")
        if (
            perception_trigger_runtime is not None
            and perception_trigger_runtime.ledger is not self._ledger
        ):
            raise ValueError("perception trigger runtime must own this exact ledger")
        self._perception_owner = perception_owner
        self._perception_trigger_runtime = perception_trigger_runtime
        self._latency = latency_recorder
        self._lock = asyncio.Lock()
        # A durable expression claim may be observed concurrently by ingress
        # and the retry scheduler.  This process-local table is the atomic join
        # seam for the provider phase of one exact attempt.  The tiny lock only
        # protects task publication; it is never held while Context compilation
        # or a provider call is running.
        self._expression_attempt_task_lock = asyncio.Lock()
        self._expression_attempt_tasks: dict[
            str, asyncio.Task[ProposalAuditCommit]
        ] = {}
        # Background cognition is serialized with itself, but must not hold
        # the world mutation lock while an external model is thinking.  The
        # visible inbound lane can then commit/answer while affect, memory,
        # appraisal, and proactive workers continue on a stale-safe cursor.
        self._background_lock = asyncio.Lock()

    @property
    def world_id(self) -> str:
        """Stable identity exposed to platform-neutral ingress adapters."""

        return self._world_id

    async def current_logical_time(self):
        """Return the durable Clock authority used to pin an ingress envelope."""

        return (await self._project_for_write()).logical_time

    async def _audit_expression_attempt_once(
        self,
        *,
        observation: Observation,
        observation_event: WorldEvent,
        cursor: ProjectionCursor,
        expression_attempt_id: str,
        turn_budget: InteractiveTurnBudget | None = None,
        recorded_cadence_draws: tuple[CadenceDraw, ...] = (),
    ) -> ProposalAuditCommit:
        """Join one process-local provider invocation for an exact attempt.

        Ledger CAS remains the durable authority across processes.  This join
        closes the narrower in-process race where ingress acquires the world
        lock after the scheduler has already observed it as free.
        """

        if self._pinned_turn is None:
            raise ValueError("expression attempt audit requires a pinned turn")
        if not expression_attempt_id:
            raise ValueError("expression attempt audit requires an attempt id")
        async with self._expression_attempt_task_lock:
            task = self._expression_attempt_tasks.get(expression_attempt_id)
            owns_invocation = task is None
            if task is None:
                task = asyncio.create_task(
                    self._pinned_turn.audit_observation(
                        observation=observation,
                        observation_event=observation_event,
                        cursor=cursor,
                        turn_budget=turn_budget,
                        recorded_cadence_draws=recorded_cadence_draws,
                        expression_attempt_id=expression_attempt_id,
                    ),
                    name=f"expression-attempt:{expression_attempt_id}",
                )
                self._expression_attempt_tasks[expression_attempt_id] = task
        try:
            # A joining caller must not cancel the shared provider invocation.
            # The creator still owns cancellation so an interrupted ingest can
            # leave the same durable no-audit claim recoverable immediately.
            result = await task if owns_invocation else await asyncio.shield(task)
        except BaseException:
            async with self._expression_attempt_task_lock:
                if (
                    self._expression_attempt_tasks.get(expression_attempt_id)
                    is task
                    and task.done()
                ):
                    self._expression_attempt_tasks.pop(expression_attempt_id, None)
            raise

        async with self._expression_attempt_task_lock:
            # Retain recent successful tasks so a stale concurrent caller that
            # arrives just after completion still joins the exact result.
            # Bound this operational cache; durable audits remain authoritative.
            if len(self._expression_attempt_tasks) > 256:
                for key, candidate in tuple(self._expression_attempt_tasks.items()):
                    if len(self._expression_attempt_tasks) <= 256:
                        break
                    if key != expression_attempt_id and candidate.done():
                        self._expression_attempt_tasks.pop(key, None)
        return result

    async def _forget_expression_attempt_task(self, attempt_id: str) -> None:
        """Drop a completed local join after its Proposal becomes stale."""

        async with self._expression_attempt_task_lock:
            task = self._expression_attempt_tasks.get(attempt_id)
            if task is not None and task.done():
                self._expression_attempt_tasks.pop(attempt_id, None)

    async def _expression_attempt_task_is_live(self, attempt_id: str) -> bool:
        """Return whether this Runtime is already inside the exact provider attempt."""

        async with self._expression_attempt_task_lock:
            task = self._expression_attempt_tasks.get(attempt_id)
            return task is not None and not task.done()

    async def _cancel_superseded_expression_attempt_tasks(
        self,
        attempt_ids: tuple[str, ...],
    ) -> None:
        """Release provider work after newer inbound durably superseded it.

        The ledger commit is the authority for supersession.  This method is
        only process-local cleanup after that commit: it cannot choose which
        turn wins and it never touches an episode that already authorized an
        Action.  Without this cleanup, obsolete author/reviewer calls retain
        the shared provider ceiling and can make the newest user turn fail.
        """

        if not attempt_ids:
            return
        async with self._expression_attempt_task_lock:
            tasks = tuple(
                task
                for attempt_id in attempt_ids
                if (task := self._expression_attempt_tasks.get(attempt_id)) is not None
                and not task.done()
            )
            for task in tasks:
                task.cancel()
        if not tasks:
            return
        # Normal HTTP transports unwind cancellation immediately.  Give their
        # callbacks one bounded scheduling window to release provider slots,
        # but never let a cancellation-suppressing transport delay the newest
        # visible turn.
        await asyncio.wait(tasks, timeout=0.05)

    async def drain_background_once(self):
        """Run one background job and turn an expected cursor race into a retry."""

        try:
            return await self._drain_background_once_impl()
        except ConcurrencyConflict:
            # A visible inbound turn may win the ledger cursor while a
            # background provider call is in flight.  That is normal after
            # separating the locks; leave the durable claim for recovery and
            # let the next scheduler wake retry it instead of surfacing a
            # scheduler exception.
            _LOG.info("background cognition lost a cursor race; retrying later")
            return None

    async def _drain_background_once_impl(
        self,
    ) -> (
        OutcomeTriggerRunResult
        | FactTriggerRunResult
        | PerceptionTriggerRunResult
        | SocialActionRunResult
        | MemoryWithdrawalReviewRunResult
        | AnchoredRunResult
        | RuntimeOutcome
        | None
    ):
        """Run one low-priority mental-state job without delaying an interactive turn.

        Hosts call this from their durable worker loop.  It is intentionally
        separate from :meth:`ingest`: an affect reflection may use a thinking
        route, while the visible reply path must stay latency-bounded.
        """

        # Do not use the world mutation lock here.  Every worker below owns a
        # durable claim/acceptance seam and can lose a cursor race cleanly;
        # holding ``_lock`` across its provider call would make a slow
        # low-priority thought block the next user message.
        async with self._background_lock:
            # A user-visible turn that failed for technical reasons outranks
            # advisory cognition once its recorded retry lease expires.  The
            # process is event-sourced and source-bound; this does not turn a
            # model-authored `silent` choice into a retry.
            expression_retry = await self._drain_expression_retry_once()
            if expression_retry is not None:
                return expression_retry
            inbound_state = await self._drain_inbound_state_settlement_once()
            if inbound_state is not None:
                return inbound_state
            if self._perception_trigger_runtime is not None:
                perception = await self._perception_trigger_runtime.drain_one()
                if perception.status != "idle":
                    return perception
            if self._character_interior is not None:
                reconsideration = await self._character_interior._drain_reconsideration_once()  # noqa: SLF001
                if reconsideration is not None:
                    return reconsideration
            # Initiative is time-sensitive: an eligible silence or explicit
            # response gap should not sit behind an arbitrarily large backlog
            # of per-observation semantic jobs.  The compiler only exposes an
            # evidence-bound opportunity; the model still owns now/later/
            # silent.  Before its opening window this check is idle and costs
            # no authority, so ordinary appraisal/fact work keeps its order.
            if self._character_interior is not None:
                proactive = await self._character_interior._drain_proactive_once()  # noqa: SLF001
                if proactive is not None:
                    return proactive
            if self._outcome_deliberation_turn is not None:
                assert self._outcome_worker is not None
                assert self._outcome_deliberation_owner is not None
                outcome = await OutcomeTriggerRuntime(
                    ledger=self._ledger,
                    turn=self._outcome_deliberation_turn,
                    worker=self._outcome_worker,
                    owner_id=self._outcome_deliberation_owner,
                ).drain_one()
                if outcome.status != "idle":
                    return outcome
            # Settle source-bound user Facts before the larger appraisal/NPC
            # backlog can consume a bounded scheduler pass. This keeps names
            # and preferences available to the next recall turn without
            # adding work to the visible reply lane.
            if self._fact_acceptance is not None:
                assert self._fact_adapter is not None
                assert self._interaction_fact_owner is not None
                fact = await InteractionFactTriggerRuntime(
                    ledger=self._fact_acceptance.ledger,
                    acceptance=self._fact_acceptance,
                    adapter=self._fact_adapter,
                    character_interior=self._character_interior,
                    memory_lifecycle=self._fact_memory_lifecycle,
                    memory_actor_ref=self._fact_memory_actor_ref,
                    owner_id=self._interaction_fact_owner,
                ).drain_one()
                if fact.status not in {"idle", "owned_elsewhere"}:
                    return fact
            if self._reflection_scheduler is not None:
                reflection = self._reflection_scheduler.open_once(
                    trace_id="trace:reflection-scheduler",
                    correlation_id="correlation:reflection-scheduler",
                )
                if reflection.opened:
                    return None
            if self._character_interior is not None:
                stimulus = await self._character_interior._drain_world_stimulus_once()  # noqa: SLF001
                if stimulus is not None:
                    return stimulus
            if self._inbound_relationship_worker is not None:
                relationship = await self._inbound_relationship_worker.drain_one()
                if relationship is not None and relationship.status != "owned_elsewhere":
                    return relationship
            if self._relationship_adjustment_worker is not None:
                assert self._relationship_adjustment_owner is not None
                adjustment = await RelationshipAdjustmentTriggerRuntime(
                    ledger=self._ledger,
                    worker=self._relationship_adjustment_worker,
                    owner_id=self._relationship_adjustment_owner,
                ).drain_one()
                if adjustment.status != "idle":
                    return adjustment
            if self._character_interior is not None:
                impression = await self._character_interior._drain_private_impression_once()  # noqa: SLF001
                if impression is not None:
                    return impression
            if self._memory_withdrawal_review is not None:
                memory_review = await self._memory_withdrawal_review.drain_one()
                if memory_review.status != "idle":
                    return memory_review
            # A delayed social effect is useful, but it must not starve the
            # same observation's appraisal, fact, relationship or affect
            # consumers. Immediate and silent decisions are already final in
            # the shared proposal audit and are filtered by the worker.
            if self._social_action_worker is not None:
                social_action = await self._social_action_worker.drain_one()
                if social_action.status != "idle":
                    return social_action
            return None

    async def drain_actions_once(
        self,
        *,
        provider_accepted_reconciliation_gate: (
            ProviderAcceptedReconciliationGate | None
        ) = None,
    ) -> ActionPumpResult | None:
        """Dispatch one authorized external Action through the durable pump.

        This deliberately does not hold ``_lock`` while calling an executor:
        ``ActionDispatchStarted`` is already durable before that call and a
        receipt comes back through :meth:`settle`, which owns its own lock.
        Concurrent pump instances race only on ledger CAS and must retry.
        """

        if self._action_executor is None:
            return None
        assert self._action_pump_owner is not None
        return await ActionPump(
            ledger=self._ledger,
            executor=self._action_executor,
            settle=self.settle,
            owner_id=self._action_pump_owner,
            excluded_action_kinds=self._action_pump_excluded_kinds,
        ).drain_once(
            provider_accepted_reconciliation_gate=(
                provider_accepted_reconciliation_gate
            )
        )

    async def drain_action(self, action_id: str) -> ActionPumpResult | None:
        """Advance one ingress-bound Action without selecting a sibling."""

        if self._action_executor is None:
            return None
        assert self._action_pump_owner is not None
        return await ActionPump(
            ledger=self._ledger,
            executor=self._action_executor,
            settle=self.settle,
            owner_id=self._action_pump_owner,
            excluded_action_kinds=self._action_pump_excluded_kinds,
        ).drain_action(action_id)

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

    async def _commit_at_cursor(
        self,
        events: list[WorldEvent],
        *,
        cursor: ProjectionCursor,
        commit_id: str | None = None,
    ):
        if self._ledger.blocks_event_loop:
            return await asyncio.to_thread(
                self._ledger.commit_at_cursor,
                events,
                expected_cursor=cursor,
                commit_id=commit_id,
            )
        return self._ledger.commit_at_cursor(
            events,
            expected_cursor=cursor,
            commit_id=commit_id,
        )

    async def _lookup_event_commit(self, event_id: str):
        if self._ledger.blocks_event_loop:
            return await asyncio.to_thread(self._ledger.lookup_event_commit, event_id)
        return self._ledger.lookup_event_commit(event_id)

    async def _record_response_expectation_assessment(
        self,
        *,
        proposal: DecisionProposal | MinimalProposal,
        observation: Observation,
        observation_event: WorldEvent,
    ) -> bool:
        assessment = proposal.response_expectation_assessment
        if assessment is None:
            return False
        identity_material = {
            "inbound_observation_id": observation.observation_id,
            "observation_event_ref": observation_event.event_id,
        }
        digest = hashlib.sha256(
            json.dumps(
                identity_material,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        for retry_ordinal in range(2):
            projection = await self._project_for_write()
            if any(
                item.inbound_observation_id == observation.observation_id
                for item in projection.response_expectation_assessments
            ):
                return True
            observation_ref = next(
                (
                    item
                    for item in projection.committed_world_event_refs
                    if item.event_id == observation_event.event_id
                ),
                None,
            )
            if observation_ref is None:
                raise RuntimeError(
                    "inbound observation is missing from the committed projection"
                )
            manifest = pending_response_expectation_manifest(
                projection,
                before_world_revision=observation_ref.world_revision,
                at_logical_time=observation.logical_time,
            )
            # Reconstruct the exact historical advisory at this Observation:
            # later deliveries cannot qualify by revision, and wall-clock
            # expiry cannot erase the source during delayed reconciliation.
            if manifest is None:
                return False
            payload = ResponseExpectationAssessedPayload(
                assessment_id=f"assessment:response-expectation:{digest}",
                source_plan_id=manifest.plan_id,
                source_acceptance_event_ref=manifest.acceptance_event_ref,
                inbound_observation_id=observation.observation_id,
                inbound_observation_event_ref=observation_event.event_id,
                status=assessment.status,
                reason=assessment.reason,
                assessed_at=observation.logical_time,
            )
            event = WorldEvent.from_payload(
                schema_version="world-v2.1",
                event_id=f"event:response-expectation-assessed:{digest}",
                world_id=self._world_id,
                event_type="ResponseExpectationAssessed",
                logical_time=observation.logical_time,
                created_at=observation.created_at,
                actor="agent:companion",
                source="world-runtime:inbound-cognition",
                trace_id=observation.trace_id,
                causation_id=observation_event.event_id,
                correlation_id=observation.correlation_id,
                idempotency_key=domain_idempotency_key(
                    event_type="ResponseExpectationAssessed",
                    world_id=self._world_id,
                    payload=payload.model_dump(mode="json"),
                )
                or f"response-expectation-assessed:{digest}",
                payload=payload.model_dump(mode="json"),
            )
            try:
                await self._commit(
                    [event],
                    world_revision=projection.world_revision,
                    deliberation_revision=projection.deliberation_revision,
                    commit_id=(
                        f"commit:response-expectation-assessed:{digest}:"
                        f"{retry_ordinal}"
                    ),
                )
            except ConcurrencyConflict:
                continue
            return True
        _LOG.warning(
            "response expectation assessment deferred after CAS conflict "
            "observation=%s",
            observation.observation_id,
        )
        return False

    async def reconcile_response_expectation_assessment(self) -> bool:
        """Retry one durable audited assessment that lost its post-reply CAS."""

        projection = await self._project_for_write()
        recorded_observations = {
            item.inbound_observation_id
            for item in projection.response_expectation_assessments
        }
        for audit in reversed(projection.proposal_audits):
            if audit.proposal_kind not in {"decision", "minimal"}:
                continue
            try:
                proposal = validate_proposal_envelope(json.loads(audit.proposal_json))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if (
                not isinstance(proposal, (DecisionProposal, MinimalProposal))
                or proposal.response_expectation_assessment is None
            ):
                continue
            located = await self._lookup_event_commit(audit.trigger_ref)
            if located is None or located[0].event_type != "ObservationRecorded":
                continue
            try:
                observation = Observation.model_validate_json(located[0].payload_json)
            except ValueError:
                continue
            if observation.observation_id in recorded_observations:
                continue
            if await self._record_response_expectation_assessment(
                proposal=proposal,
                observation=observation,
                observation_event=located[0],
            ):
                return True
        return False

    async def _commit_accepted(self, batch, *, cursor: ProjectionCursor):
        if self._ledger.blocks_event_loop:
            return await asyncio.to_thread(
                self._ledger.commit_accepted, batch, expected_cursor=cursor
            )
        return self._ledger.commit_accepted(batch, expected_cursor=cursor)

    async def _commit_visible_acceptance(
        self,
        *,
        recorder: MinimalReplyAtomicRecorder | ExpressionPlanAtomicRecorder,
        acceptance_id: str,
        material,
        actor: str,
        source: str,
        trace_id: str,
    ):
        """Measure only the real accepted-batch preparation and CAS commit."""

        trace = self._latency.get(trace_id) if self._latency is not None else None
        if trace is None:
            batch = recorder.prepare_batch(
                acceptance_id=acceptance_id,
                material=material,
                actor=actor,
                source=source,
            )
            return await self._commit_accepted(batch, cursor=material.cursor)
        async with trace.measure("acceptance"):
            batch = recorder.prepare_batch(
                acceptance_id=acceptance_id,
                material=material,
                actor=actor,
                source=source,
            )
            return await self._commit_accepted(batch, cursor=material.cursor)

    async def _record_reply_acceptance_failure(
        self,
        *,
        audit,
        observation: Observation,
        failure_code: str,
    ) -> str:
        """Close one unusable reply Proposal without pretending the model was silent.

        A stale Proposal must be reconsidered immediately at the new World
        cursor.  A current Proposal that cannot cross a hard Acceptance
        boundary (most commonly budget/account availability) is rejected and
        retried on the expression lifecycle's 10/30/120 technical schedule.
        The immutable Proposal remains in the ledger for audit and replay.
        """

        projection = await self._project_for_write()
        existing = next(
            (
                decision
                for decision in projection.acceptance_decisions
                if decision.proposal_id == audit.proposal_id
            ),
            None,
        )
        if existing is not None:
            return existing.status
        if audit.evaluated_world_revision > projection.world_revision:
            raise ConcurrencyConflict(
                "reply Proposal evaluates a future World revision"
            )
        status = (
            "stale"
            if audit.evaluated_world_revision < projection.world_revision
            else "rejected"
        )
        binding = derive_acceptance_manifest_proposal_v2(
            proposal_json=audit.proposal_json,
            proposal_event_ref=audit.event_ref,
            proposal_event_payload_hash=audit.event_payload_hash,
        )
        identity_material = {
            "contract": "expression-acceptance-failure.1",
            "world_id": self._world_id,
            "proposal_id": audit.proposal_id,
            "status": status,
            "failure_code": failure_code,
        }
        digest = hashlib.sha256(
            json.dumps(
                identity_material,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        acceptance_id = f"acceptance:expression-retry:{digest}"
        raw: dict[str, object] = {
            "manifest_version": "acceptance-manifest.2",
            "acceptance_id": acceptance_id,
            "status": status,
            "evaluated_world_revision": audit.evaluated_world_revision,
            "proposals": (binding.model_dump(mode="json"),),
            "authorized_effects": (),
        }
        raw["manifest_hash"] = canonical_acceptance_manifest_hash(raw)
        manifest = AcceptanceManifestV2.model_validate(raw)
        payload = manifest.model_dump(mode="json")
        idempotency_key = domain_idempotency_key(
            event_type="AcceptanceRecorded",
            world_id=self._world_id,
            payload=payload,
        )
        if idempotency_key is None:
            raise RuntimeError(
                "reply Acceptance failure has no installed event identity"
            )
        at = projection.logical_time or observation.logical_time
        event = WorldEvent.from_payload(
            schema_version="world-v2.1",
            event_id=f"event:expression-acceptance-failure:{digest}",
            world_id=self._world_id,
            event_type="AcceptanceRecorded",
            logical_time=at,
            created_at=observation.created_at,
            actor="system:expression-reliability",
            source="world-runtime:expression-reliability",
            trace_id=observation.trace_id,
            causation_id=audit.event_ref,
            correlation_id=observation.correlation_id,
            idempotency_key=idempotency_key,
            payload=payload,
        )
        await self._commit(
            [event],
            world_revision=projection.world_revision,
            deliberation_revision=projection.deliberation_revision,
            commit_id=f"commit:expression-acceptance-failure:{digest}",
        )
        _LOG.warning(
            "reply acceptance deferred proposal=%s status=%s code=%s",
            audit.proposal_id,
            status,
            failure_code,
        )
        if status == "stale":
            # This exact Proposal is now terminal and the current World cursor
            # requires fresh deliberation.  Releasing the completed local join
            # does not weaken the concurrent-call mutex: the stale Acceptance
            # is durable before another invocation can begin.
            await self._forget_expression_attempt_task(audit.attempt_id)
        return status

    async def _settle_unified_inbound_state(
        self,
        *,
        observation: Observation,
        observation_event: WorldEvent,
        audit_cursor: ProjectionCursor,
        proposal_id: str,
    ) -> tuple[str, ...]:
        """Settle every inner-state facet from one audited inbound decision.

        Expression acceptance may advance World revision first.  The typed
        compilers authenticate the same role bytes at the immutable audit
        cursor and deterministically rebase their candidates to the current
        prefix.  No second role call or local semantic inference is introduced.
        """

        projection = await self._project_for_write()
        audit = next(
            (item for item in projection.proposal_audits if item.proposal_id == proposal_id),
            None,
        )
        if audit is None or audit.proposal_kind != "decision":
            return ()
        await self._renew_inline_appraisal_claim_for_retry(
            observation=observation,
            observation_event=observation_event,
            proposal_event_ref=audit.event_ref,
        )
        projection = await self._project_for_write()
        proposal = validate_proposal_envelope(json.loads(audit.proposal_json))
        if not isinstance(proposal, DecisionProposal):
            return ()
        shape = inspect_unified_inbound_decision(proposal)
        deferred: list[str] = []

        if shape.appraisal is None:
            await self._finish_inline_appraisal_trigger(
                observation=observation,
                observation_event=observation_event,
                proposal_event_ref=audit.event_ref,
                outcome_ref=f"outcome:{proposal_id}:no-appraisal",
                rejection=None,
            )
        elif self._immediate_emotion_worker is None:
            deferred.append("character_interior.inbound_state_authority_unavailable")
        else:
            current = ProjectionCursor(
                world_revision=projection.world_revision,
                deliberation_revision=projection.deliberation_revision,
                ledger_sequence=projection.ledger_sequence,
            )
            try:
                if self._ledger.blocks_event_loop:
                    emotion = await asyncio.to_thread(
                        self._immediate_emotion_worker.process,
                        world_id=self._world_id,
                        audit_cursor=audit_cursor,
                        current_cursor=current,
                        proposal_id=proposal_id,
                    )
                else:
                    emotion = self._immediate_emotion_worker.process(
                        world_id=self._world_id,
                        audit_cursor=audit_cursor,
                        current_cursor=current,
                        proposal_id=proposal_id,
                    )
            except ConcurrencyConflict:
                # The claimed trigger remains durable and retryable. A cursor
                # race is technical failure, never authority to invent or erase
                # the role-authored appraisal or Affect.
                deferred.append("character_interior.inbound_state_cursor_conflict")
            except ValueError as exc:
                failure_code = str(
                    getattr(exc, "code", "advisory_validation_rejected")
                )
                await self._finish_inline_appraisal_trigger(
                    observation=observation,
                    observation_event=observation_event,
                    proposal_event_ref=audit.event_ref,
                    outcome_ref=f"outcome:{proposal_id}:advisory-rejected",
                    rejection=(proposal_id, failure_code, exc),
                )
                deferred.append(failure_code)
            else:
                if emotion.appraisal.status == "no_change":
                    await self._finish_inline_appraisal_trigger(
                        observation=observation,
                        observation_event=observation_event,
                        proposal_event_ref=audit.event_ref,
                        outcome_ref=f"outcome:{proposal_id}:no-appraisal",
                        rejection=None,
                    )

        # Relationship is optional and independent of whether this turn also
        # changed Affect. It is still authored by the exact same CharacterInterior
        # proposal and settled only through typed relationship authorities.
        if shape.relationship is not None:
            if self._inbound_relationship_worker is None:
                deferred.append(
                    "character_interior.inbound_relationship.authority_unavailable"
                )
            else:
                relationship_head = await self._project_for_write()
                relationship_cursor = ProjectionCursor(
                    world_revision=relationship_head.world_revision,
                    deliberation_revision=relationship_head.deliberation_revision,
                    ledger_sequence=relationship_head.ledger_sequence,
                )
                try:
                    relationship = await self._inbound_relationship_worker.process(
                        world_id=self._world_id,
                        audit_cursor=audit_cursor,
                        current_cursor=relationship_cursor,
                        proposal_id=proposal_id,
                        source_event=observation_event,
                    )
                except ConcurrencyConflict:
                    deferred.append(
                        "character_interior.inbound_relationship.cursor_conflict"
                    )
                except ValueError as exc:
                    deferred.append(
                        str(
                            getattr(
                                exc,
                                "code",
                                "character_interior.inbound_relationship.validation_failure",
                            )
                        )
                    )
                except Exception:  # pragma: no cover - defensive provider-free boundary
                    _LOG.exception(
                        "unified inbound relationship settlement failed proposal=%s",
                        proposal_id,
                    )
                    deferred.append(
                        "character_interior.inbound_relationship.runtime_failure"
                    )
                else:
                    if relationship.status == "owned_elsewhere":
                        deferred.append(
                            "character_interior.inbound_relationship.owned_elsewhere"
                        )
        return tuple(dict.fromkeys(deferred))

    async def _renew_inline_appraisal_claim_for_retry(
        self,
        *,
        observation: Observation,
        observation_event: WorldEvent,
        proposal_event_ref: str,
    ) -> None:
        """Reclaim the same inner-state lifecycle after a durable reply retry.

        The inbound Observation opens both the visible-expression lifecycle and
        the same-turn inner-state lifecycle.  A technical expression failure
        may be retried ten minutes later, well after the original two-minute
        inner-state lease.  The successful retry still owns one unified role
        result, so it must append a new claim attempt before settling that
        result; completing the expired attempt would violate the ledger's
        effect-once proof.

        This method only renews deterministic execution authority.  It neither
        asks another model nor makes a semantic decision.
        """

        trigger_id = interaction_appraisal_trigger_identity(
            self._world_id, observation.observation_id
        )
        for _ in range(3):
            projection = await self._project_for_write()
            process = next(
                (
                    item
                    for item in projection.trigger_processes
                    if item.trigger_id == trigger_id
                ),
                None,
            )
            if process is None or process.state == "terminal":
                return
            lease = process.claim_lease
            if lease is None:
                raise RuntimeError("unified inbound state trigger is not claimed")
            at = projection.logical_time or observation.logical_time
            if at <= lease.expires_at:
                return
            if self._inbound_state_owner is None:
                raise RuntimeError("unified inbound state owner is unavailable")

            attempt_ordinal = len(process.attempt_ids) + 1
            attempt_id = CHARACTER_INTERIOR_INBOUND_ATTEMPT_PREFIX + hashlib.sha256(
                json.dumps(
                    [
                        self._world_id,
                        process.trigger_id,
                        proposal_event_ref,
                        attempt_ordinal,
                    ],
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
            reclaimed = process.model_copy(
                update={
                    "state": "claimed",
                    "claim_lease": ClaimLease(
                        owner_id=self._inbound_state_owner,
                        attempt_id=attempt_id,
                        acquired_at=at,
                        expires_at=at
                        + timedelta(
                            seconds=DEFAULT_INTERACTION_APPRAISAL_LEASE_SECONDS
                        ),
                    ),
                    "attempt_ids": (*process.attempt_ids, attempt_id),
                }
            )
            payload = {"process": reclaimed.model_dump(mode="json")}
            identity = domain_idempotency_key(
                event_type="TriggerProcessReclaimed",
                world_id=self._world_id,
                payload=payload,
            )
            if identity is None:
                raise RuntimeError("unified inbound state reclaim has no identity")
            event = WorldEvent.from_payload(
                schema_version="world-v2.1",
                event_id="event:character-interior-inbound-state:reclaimed:"
                + hashlib.sha256(
                    json.dumps(
                        [process.trigger_id, attempt_id],
                        separators=(",", ":"),
                    ).encode()
                ).hexdigest(),
                world_id=self._world_id,
                event_type="TriggerProcessReclaimed",
                logical_time=at,
                created_at=observation.created_at,
                actor=self._inbound_state_owner,
                source="world-runtime:character-interior-state",
                trace_id=observation.trace_id,
                causation_id=proposal_event_ref,
                correlation_id=observation.correlation_id,
                idempotency_key=identity,
                payload=payload,
            )
            try:
                await self._commit(
                    [event],
                    world_revision=projection.world_revision,
                    deliberation_revision=projection.deliberation_revision,
                    commit_id="commit:character-interior-inbound-state:reclaim:"
                    + hashlib.sha256(attempt_id.encode()).hexdigest(),
                )
                return
            except (ConcurrencyConflict, IdempotencyConflict):
                await asyncio.sleep(0)
        raise ConcurrencyConflict("unified inbound state reclaim remained contended")

    async def _finish_inline_appraisal_trigger(
        self,
        *,
        observation: Observation,
        observation_event: WorldEvent,
        proposal_event_ref: str,
        outcome_ref: str,
        rejection: tuple[str, str, Exception] | None,
    ) -> None:
        """Audit advisory rejection and terminalize an unmutated trigger once."""

        trigger_id = interaction_appraisal_trigger_identity(
            self._world_id, observation.observation_id
        )
        for _ in range(3):
            projection = await self._project_for_write()
            process = next(
                (item for item in projection.trigger_processes if item.trigger_id == trigger_id),
                None,
            )
            if process is None:
                raise RuntimeError("unified inbound state trigger is unavailable")
            events: list[WorldEvent] = []
            at = projection.logical_time or observation.logical_time
            if rejection is not None:
                rejected_proposal_id, failure_code, exc = rejection
                failure_fingerprint = hashlib.sha256(
                    json.dumps(
                        {
                            "exception_type": type(exc).__name__,
                            "message": str(exc)[:240],
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode()
                ).hexdigest()
                payload = {
                    "proposal_id": rejected_proposal_id,
                    "source_event_ref": proposal_event_ref,
                    "advisory_kind": "appraisal_affect",
                    "stage": "immediate_emotion_acceptance",
                    "reason_code": "advisory_validation_rejected",
                    "failure_fingerprint": failure_fingerprint,
                }
                rejection_event_id = "event:advisory-acceptance-rejected:" + hashlib.sha256(
                    json.dumps(
                        [
                            rejected_proposal_id,
                            "immediate_emotion_acceptance",
                            failure_fingerprint,
                        ],
                        separators=(",", ":"),
                    ).encode()
                ).hexdigest()
                if await self._lookup_event_commit(rejection_event_id) is None:
                    identity = domain_idempotency_key(
                        event_type="AdvisoryAcceptanceRejected",
                        world_id=self._world_id,
                        payload=payload,
                    )
                    if identity is None:
                        raise RuntimeError("advisory rejection has no identity")
                    events.append(
                        WorldEvent.from_payload(
                            schema_version="world-v2.1",
                            event_id=rejection_event_id,
                            world_id=self._world_id,
                            event_type="AdvisoryAcceptanceRejected",
                            logical_time=at,
                            created_at=observation.created_at,
                            actor=self._inbound_state_owner
                            or "worker:character-interior-state",
                            source="world-runtime:character-interior-state",
                            trace_id=observation.trace_id,
                            causation_id=proposal_event_ref,
                            correlation_id=observation.correlation_id,
                            idempotency_key=identity,
                            payload=payload,
                        )
                    )
            if process.state != "terminal":
                lease = process.claim_lease
                if lease is None:
                    raise RuntimeError("unified inbound state trigger is not claimed")
                completion_payload = {
                    "trigger_id": process.trigger_id,
                    "owner_id": lease.owner_id,
                    "attempt_id": lease.attempt_id,
                    "completed_at": at.isoformat(),
                    "runtime_outcome_ref": outcome_ref,
                }
                completion_identity = (
                    "world-v2:character-interior-inbound-state:completion:"
                    + hashlib.sha256(
                        json.dumps(
                            [self._world_id, process.trigger_id, lease.attempt_id],
                            separators=(",", ":"),
                        ).encode()
                    ).hexdigest()
                )
                events.append(
                    WorldEvent.from_payload(
                        schema_version="world-v2.1",
                        event_id="event:character-interior-inbound-state:completed:"
                        + hashlib.sha256(
                            json.dumps(
                                [process.trigger_id, lease.attempt_id],
                                separators=(",", ":"),
                            ).encode()
                        ).hexdigest(),
                        world_id=self._world_id,
                        event_type="TriggerProcessCompleted",
                        logical_time=at,
                        created_at=observation.created_at,
                        actor=lease.owner_id,
                        source="world-runtime:character-interior-state",
                        trace_id=observation.trace_id,
                        causation_id=observation_event.event_id,
                        correlation_id=observation.correlation_id,
                        idempotency_key=completion_identity,
                        payload=completion_payload,
                    )
                )
            if not events:
                return
            try:
                await self._commit(
                    events,
                    world_revision=projection.world_revision,
                    deliberation_revision=projection.deliberation_revision,
                    commit_id="commit:character-interior-inbound-state:"
                    + hashlib.sha256(
                        json.dumps(
                            [event.event_id for event in events],
                            separators=(",", ":"),
                        ).encode()
                    ).hexdigest(),
                )
                return
            except (ConcurrencyConflict, IdempotencyConflict):
                await asyncio.sleep(0)
        raise ConcurrencyConflict("unified inbound state terminalization remained contended")

    async def evaluate_replay(
        self, *, evaluator: ReplayEvaluator | None = None
    ) -> ReplayEvaluation:
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

    def expression_episode_diagnostics(self) -> dict[str, object]:
        """Return text-free process aggregates for health endpoints."""

        if self._pinned_turn is None:
            return {"mode": "off"}
        return self._pinned_turn.expression_episode_diagnostics()

    async def cancel_superseded_expression_streams(
        self, current_trigger_ref: str
    ) -> None:
        """Invalidate unsent stream units when provider ingress shifts attention."""

        if self._pinned_turn is not None:
            await self._pinned_turn.cancel_superseded_expression_streams(
                current_trigger_ref
            )

    async def _claim_expression_episode(
        self, observation: Observation
    ) -> tuple[TriggerProcess | None, ProjectionCursor | None]:
        if self._pinned_turn is None:
            return None, None
        projection = await self._project_for_write()
        trigger_id = expression_episode_trigger_id(
            self._world_id, observation.observation_id
        )
        process = next(
            (item for item in projection.trigger_processes if item.trigger_id == trigger_id),
            None,
        )
        if process is None or process.state == "terminal":
            return process, None
        at = projection.logical_time or observation.logical_time
        work_due = expression_episode_work_due(
            projection,
            process,
            owner_id=self._expression_episode_owner,
        )
        if work_due is not None and at < work_due:
            # The short provider lease may already have expired while a
            # recorded technical failure is still inside its independent
            # 10/30/120-minute backoff. Neither duplicate ingress nor another
            # continuation path may reclaim early.
            return process, None
        if process.state == "claimed":
            if (
                process.claim_lease is None
                or at < process.claim_lease.expires_at
            ):
                # A duplicate ingress joins the durable failure state but may
                # not bypass its retry schedule.  The background worker
                # reclaims the process after the lease expires.
                return process, None
        event, claimed = expression_episode_claim_event(
            world_id=self._world_id,
            process=process,
            owner_id=self._expression_episode_owner,
            at=at,
            trace_id=observation.trace_id,
            correlation_id=observation.correlation_id,
            technical_failure_count=expression_episode_technical_failure_count(
                projection, process
            ),
        )
        committed = await self._commit(
            [event],
            world_revision=projection.world_revision,
            deliberation_revision=projection.deliberation_revision,
            commit_id=f"commit:{trigger_id}:claim:{claimed.claim_lease.attempt_id}",
        )
        return claimed, ProjectionCursor(
            world_revision=committed.world_revision,
            deliberation_revision=committed.deliberation_revision,
            ledger_sequence=committed.ledger_sequence,
        )

    async def _ensure_expression_retry_process(
        self,
        *,
        observation: Observation,
        observation_event: WorldEvent,
    ) -> TriggerProcess | None:
        """Open the durable retry lifecycle only after a technical failure.

        Expression Episode shadow/on modes already open the lifecycle beside
        ingress.  Production currently keeps that feature mode off, so a
        failed model result must create the same event-sourced recovery seam
        without adding two lifecycle events to every successful turn.
        """

        if self._pinned_turn is None:
            return None
        projection = await self._project_for_write()
        trigger_id = expression_episode_trigger_id(
            self._world_id, observation.observation_id
        )
        existing = next(
            (
                item
                for item in projection.trigger_processes
                if item.trigger_id == trigger_id
            ),
            None,
        )
        if existing is not None:
            return existing
        opened_event = expression_episode_open_event(
            observation=observation,
            observation_event=observation_event,
        )
        opened = TriggerProcess.model_validate_json(
            json.dumps(opened_event.payload()["process"])
        )
        claimed_event, claimed = expression_episode_claim_event(
            world_id=self._world_id,
            process=opened,
            owner_id=self._expression_episode_owner,
            at=projection.logical_time or observation.logical_time,
            trace_id=observation.trace_id,
            correlation_id=observation.correlation_id,
            technical_failure_count=0,
        )
        await self._commit(
            [opened_event, claimed_event],
            world_revision=projection.world_revision,
            deliberation_revision=projection.deliberation_revision,
            commit_id=f"commit:{trigger_id}:technical-retry-open",
        )
        return claimed

    async def _expression_retry_source(
        self,
        *,
        projection,
        process: TriggerProcess,
    ) -> tuple[Observation, WorldEvent, CommitResult, int] | None:
        """Resolve the exact committed Observation authority for one retry."""

        source = next(
            (
                item
                for item in projection.message_observations
                if item.observation_id == process.source_evidence_ref
            ),
            None,
        )
        if (
            source is None
            or source.world_revision < 1
            or source.world_revision > len(projection.committed_world_event_refs)
        ):
            return None
        event_ref = projection.committed_world_event_refs[source.world_revision - 1]
        if (
            event_ref.event_type != "ObservationRecorded"
            or event_ref.payload_hash != source.event_payload_hash
        ):
            return None
        persisted = await self._lookup_event_commit(event_ref.event_id)
        if (
            persisted is None
            or persisted[0].event_type != "ObservationRecorded"
            or persisted[1].world_revision != source.world_revision
        ):
            return None
        try:
            observation = Observation.model_validate_json(persisted[0].payload_json)
        except ValueError:
            return None
        if observation.observation_id != source.observation_id:
            return None
        return observation, persisted[0], persisted[1], source.world_revision

    async def _drain_inbound_state_settlement_once(self) -> RuntimeOutcome | None:
        """Resume typed settlement from one already-audited InnerTurn.

        Appraisal/Affect acceptance follows visible expression authorization so
        both consume the same CharacterInterior result. Appraisal Acceptance
        may terminalize its narrow typed trigger before Affect or Relationship
        wins a later CAS. The immutable generic proposal is therefore the
        settlement journal: this worker finds an incomplete facet from that
        exact audit and retries only typed authority writes. It never re-enters
        PinnedTurn, opens the retired Affect author, or asks a second character
        for a fresh interpretation.
        """

        if (
            self._appraisal_worker is None
            or self._immediate_emotion_worker is None
            or self._lock.locked()
        ):
            return None
        projection = await self._project_for_write()
        terminal_proposal_ids = {
            item.proposal_id
            for item in projection.acceptance_decisions
            if item.status in {"rejected", "stale"}
        }
        for selected in reversed(projection.proposal_audits):
            if (
                selected.proposal_kind != "decision"
                or selected.proposal_id in terminal_proposal_ids
                or selected.proposal_id.startswith(
                    _HISTORICAL_QUICK_REACTION_PROPOSAL_PREFIX
                )
            ):
                continue
            try:
                proposal = validate_proposal_envelope(
                    json.loads(selected.proposal_json)
                )
                if not isinstance(proposal, DecisionProposal):
                    continue
                shape = inspect_unified_inbound_decision(proposal)
            except (TypeError, ValueError):
                continue
            located_observation = await self._lookup_event_commit(
                selected.trigger_ref
            )
            if (
                located_observation is None
                or located_observation[0].event_type != "ObservationRecorded"
            ):
                # World-stimulus and proactive decisions have their own
                # source-bound settlement workers.  This recovery seam owns
                # inbound CharacterInterior turns only.
                continue
            try:
                observation = Observation.model_validate_json(
                    located_observation[0].payload_json
                )
            except ValueError:
                continue
            observation_event = located_observation[0]
            appraisal_process = next(
                (
                    item
                    for item in projection.trigger_processes
                    if item.process_kind == "interaction_appraisal"
                    and item.source_evidence_ref == observation.observation_id
                ),
                None,
            )
            appraisal_accepted = shape.appraisal is None or any(
                item.origin.change_id == shape.appraisal.change_id
                for item in projection.appraisals
            )
            if (
                shape.appraisal is not None
                and not appraisal_accepted
                and appraisal_process is not None
                and appraisal_process.state == "terminal"
            ):
                # A terminal source without its mutation is a durable
                # rejection/fold, not retry authority.
                continue
            # Affect/relationship proposals vanish from their pending
            # projections once accepted, so acceptance must be judged from
            # the settled state (components reference the appraisal change;
            # signals keep their semantic identity), never from the pending
            # proposal list.
            affect_accepted = shape.affect is None or (
                shape.appraisal is not None
                and any(
                    ref.accepted_change_id == shape.appraisal.change_id
                    for episode in projection.affect_episodes
                    for component in episode.components
                    for ref in component.appraisal_refs
                )
            )
            relationship_accepted = True
            if shape.relationship is not None:
                relationship_payload = shape.relationship.payload.value()
                relationship_accepted = any(
                    signal.subject_ref == relationship_payload["subject_ref"]
                    and signal.signal_code == relationship_payload["signal_code"]
                    and signal.persistence == relationship_payload["persistence"]
                    and signal.rationale_code == relationship_payload["rationale_code"]
                    for signal in projection.relationship_signals
                )
            if appraisal_accepted and affect_accepted and relationship_accepted:
                continue
            located = await self._lookup_event_commit(selected.event_ref)
            if located is None or located[0].event_type != "ProposalRecorded":
                continue
            audit_commit = located[1]
            try:
                deferred = await self._settle_unified_inbound_state(
                    observation=observation,
                    observation_event=observation_event,
                    audit_cursor=ProjectionCursor(
                        world_revision=audit_commit.world_revision,
                        deliberation_revision=audit_commit.deliberation_revision,
                        ledger_sequence=audit_commit.ledger_sequence,
                    ),
                    proposal_id=selected.proposal_id,
                )
            except ConcurrencyConflict:
                deferred = (
                    "character_interior.inbound_state_cursor_conflict",
                )
            current = await self._project_for_write()
            current_process = next(
                (
                    item
                    for item in current.trigger_processes
                    if item.process_kind == "interaction_appraisal"
                    and item.source_evidence_ref == observation.observation_id
                ),
                None,
            )
            trigger_id = (
                current_process.trigger_id
                if current_process is not None
                else f"character-interior-settlement:{selected.proposal_id}"
            )
            terminal = current_process is not None and current_process.state == "terminal"
            return RuntimeOutcome(
                outcome_id=f"outcome:character-interior-settlement:{selected.proposal_id}",
                trigger_id=trigger_id,
                observation_ref=observation.observation_id,
                committed_world_revision=current.world_revision,
                ledger_sequence=current.ledger_sequence,
                status="observed_only" if terminal and not deferred else "deferred",
                deferred_refs=(
                    deferred
                    if deferred
                    else (
                        ()
                        if terminal
                        else ("character_interior.inbound_state_pending",)
                    )
                ),
                projection_hint=f"world-revision:{current.world_revision}",
            )
        return None

    async def _drain_expression_retry_once(self) -> RuntimeOutcome | None:
        """Reconsider one technically failed reply after its durable backoff."""

        if self._pinned_turn is None:
            return None
        if self._lock.locked():
            # Fast path only.  Correctness does not depend on this observation:
            # ingress may acquire the lock immediately after it, in which case
            # both callers atomically join ``_audit_expression_attempt_once``.
            return None
        projection = await self._project_for_write()
        at = projection.logical_time
        if at is None:
            return None
        due = due_expression_retry_processes(
            projection,
            at=at,
            owner_id=self._expression_episode_owner,
        )
        if not due:
            return None
        process = due[0]
        if (
            process.claim_lease is not None
            and await self._expression_attempt_task_is_live(
                process.claim_lease.attempt_id
            )
        ):
            # The short ingress mutation lock is intentionally released before
            # provider work.  A local claimed/no-audit shape therefore is not a
            # crash when its exact process-local task is still running.
            return None
        source = await self._expression_retry_source(
            projection=projection,
            process=process,
        )
        if source is None:
            return None
        observation, observation_event, original_commit, source_world_revision = source
        work_due = expression_episode_work_due(
            projection,
            process,
            owner_id=self._expression_episode_owner,
        )
        local_no_result = (
            process.state == "claimed"
            and process.claim_lease is not None
            and process.claim_lease.owner_id == self._expression_episode_owner
            and at < process.claim_lease.expires_at
            and not any(
                item.attempt_id == process.claim_lease.attempt_id
                for item in projection.model_result_audits
            )
        )
        if (
            local_no_result
            or (
                process.state == "claimed"
                and process.claim_lease is not None
                and work_due is not None
                and work_due <= at < process.claim_lease.expires_at
            )
        ):
            assert process.claim_lease is not None
            current_attempt_has_result = any(
                item.attempt_id == process.claim_lease.attempt_id
                for item in projection.model_result_audits
            )
            if (
                process.claim_lease.owner_id != self._expression_episode_owner
                and not current_attempt_has_result
            ):
                # Another Runtime may still be inside the provider call.  Its
                # no-audit shape is not evidence of a crash until its durable
                # lease expires.
                return None
            # The active claim has either not reached a bound model result yet
            # (process death after claim), or already owns a durable Proposal
            # whose exact continuation remains. Only the original owner may
            # resume generation before expiry; an immutable Proposal may be
            # continued by any Runtime under CAS/effect-once. Reclaiming here
            # would falsely count a crash as another failure.
            claimed = process
            cursor = ProjectionCursor(
                world_revision=projection.world_revision,
                deliberation_revision=projection.deliberation_revision,
                ledger_sequence=projection.ledger_sequence,
            )
        else:
            claimed, cursor = await self._claim_expression_episode(observation)
        if claimed is None or cursor is None:
            return None

        # A newer inbound message owns the current conversational moment.
        # Replaying an old answer after it would be less human and can
        # duplicate a later turn's meaning, so terminate the stale technical
        # retry without asking deterministic code to invent a replacement.
        current = await self._project_for_write()
        if any(
            item.world_revision > source_world_revision
            for item in current.message_observations
        ):
            await self._complete_expression_episode(
                observation=observation,
                process=claimed,
                outcome_ref="expression-episode:superseded-by-newer-inbound",
            )
            settled = await self._project_for_write()
            return RuntimeOutcome(
                outcome_id=f"outcome:expression-retry:{process.trigger_id}",
                trigger_id=process.trigger_id,
                observation_ref=observation.observation_id,
                committed_world_revision=settled.world_revision,
                ledger_sequence=settled.ledger_sequence,
                status="observed_only",
                deferred_refs=("expression_episode.superseded",),
                projection_hint=f"world-revision:{settled.world_revision}",
            )

        return await self._existing_observation_outcome(
            observation=observation,
            observation_event=observation_event,
            original_commit=original_commit,
            trigger_id=(
                f"trigger:observation:{observation.source}:"
                f"{observation.source_event_id}"
            ),
            retry_process=claimed,
            retry_cursor=cursor,
            retry_turn_budget=self._expression_retry_budget_policy.start(),
        )

    async def _complete_expression_episode(
        self,
        *,
        observation: Observation,
        process: TriggerProcess | None,
        outcome_ref: str,
    ) -> None:
        if process is None or process.state != "claimed":
            return
        projection = await self._project_for_write()
        current = next(
            (
                item
                for item in projection.trigger_processes
                if item.trigger_id == process.trigger_id
            ),
            None,
        )
        if current is None or current.state == "terminal":
            return
        if (
            process.claim_lease is None
            or current.claim_lease is None
            or current.claim_lease.attempt_id != process.claim_lease.attempt_id
        ):
            raise ConcurrencyConflict(
                "expression episode completion lost its claim ownership"
            )
        event = expression_episode_complete_event(
            world_id=self._world_id,
            process=current,
            at=projection.logical_time or observation.logical_time,
            trace_id=observation.trace_id,
            correlation_id=observation.correlation_id,
            outcome_ref=outcome_ref,
        )
        await self._commit(
            [event],
            world_revision=projection.world_revision,
            deliberation_revision=projection.deliberation_revision,
            commit_id=f"commit:{process.trigger_id}:complete",
        )

    async def _expression_episode_was_superseded(
        self,
        process: TriggerProcess | None,
    ) -> bool:
        """Recognize the stale provider race authorized by a newer inbound."""

        if process is None:
            return False
        projection = await self._project_for_write()
        current = next(
            (
                item
                for item in projection.trigger_processes
                if item.trigger_id == process.trigger_id
            ),
            None,
        )
        return (
            current is not None
            and current.state == "terminal"
            and current.runtime_outcome_ref
            == "expression-episode:superseded-by-newer-inbound"
        )

    async def _cancelled_expression_attempt_was_superseded(
        self,
        process: TriggerProcess | None,
    ) -> bool:
        """Distinguish local stale-work cleanup from caller cancellation."""

        current_task = asyncio.current_task()
        if current_task is not None and current_task.cancelling():
            # Shutdown or an explicit caller cancellation must keep unwinding,
            # even if a newer inbound happened to supersede the same episode.
            return False
        return await self._expression_episode_was_superseded(process)

    async def _expression_attempt_repin_cursor(
        self,
        *,
        observation: Observation,
        observation_event: WorldEvent,
        expression_attempt_id: str,
    ) -> ProjectionCursor | None:
        """Return a fresh cursor only while the exact unanswered attempt still owns work."""

        projection = await self._project_for_write()
        process = next(
            (
                item
                for item in projection.trigger_processes
                if item.trigger_id
                == expression_episode_trigger_id(
                    self._world_id,
                    observation.observation_id,
                )
            ),
            None,
        )
        if (
            process is None
            or process.state != "claimed"
            or process.source_evidence_ref != observation.observation_id
            or process.claim_lease is None
            or process.claim_lease.attempt_id != expression_attempt_id
            or not process.attempt_ids
            or process.attempt_ids[-1] != expression_attempt_id
        ):
            return None
        source = next(
            (
                item
                for item in projection.message_observations
                if item.observation_id == observation.observation_id
            ),
            None,
        )
        if source is None or any(
            item.world_revision > source.world_revision
            for item in projection.message_observations
        ):
            return None
        if any(
            item.trigger_ref == observation_event.event_id
            and item.attempt_id == expression_attempt_id
            for item in (
                *projection.model_result_audits,
                *projection.proposal_audits,
            )
        ):
            return None
        return ProjectionCursor(
            world_revision=projection.world_revision,
            deliberation_revision=projection.deliberation_revision,
            ledger_sequence=projection.ledger_sequence,
        )

    async def _reserve_expression_attempt_repin_cursor(
        self,
        *,
        observation: Observation,
        observation_event: WorldEvent,
        expression_attempt_id: str,
    ) -> tuple[ProjectionCursor, int] | None:
        """CAS-reserve one durable fresh-context provider call.

        The reservation is committed before model I/O. A process crash may
        conservatively consume a slot, but can never forget it and mint an
        unbounded fourth call on same-owner recovery.
        """

        for _ in range(4):
            projection = await self._project_for_write()
            process = next(
                (
                    item
                    for item in projection.trigger_processes
                    if item.trigger_id
                    == expression_episode_trigger_id(
                        self._world_id,
                        observation.observation_id,
                    )
                ),
                None,
            )
            if (
                process is None
                or process.state != "claimed"
                or process.source_evidence_ref != observation.observation_id
                or process.claim_lease is None
                or process.claim_lease.attempt_id != expression_attempt_id
                or process.attempt_ids[-1] != expression_attempt_id
                or len(process.expression_repin_reservation_ids)
                >= EXPRESSION_FRESH_CONTEXT_REPIN_LIMIT
            ):
                return None
            source = next(
                (
                    item
                    for item in projection.message_observations
                    if item.observation_id == observation.observation_id
                ),
                None,
            )
            if source is None or any(
                item.world_revision > source.world_revision
                for item in projection.message_observations
            ):
                return None
            if any(
                item.trigger_ref == observation_event.event_id
                and item.attempt_id == expression_attempt_id
                for item in (
                    *projection.model_result_audits,
                    *projection.proposal_audits,
                )
            ):
                return None
            cursor = ProjectionCursor(
                world_revision=projection.world_revision,
                deliberation_revision=projection.deliberation_revision,
                ledger_sequence=projection.ledger_sequence,
            )
            event, replacement = expression_episode_repin_reservation_event(
                world_id=self._world_id,
                process=process,
                cursor=cursor,
                at=projection.logical_time or observation.logical_time,
                trace_id=observation.trace_id,
                correlation_id=observation.correlation_id,
            )
            try:
                committed = await self._commit_at_cursor(
                    [event],
                    cursor=cursor,
                    commit_id=f"commit:{event.event_id}",
                )
            except ConcurrencyConflict:
                continue
            return (
                ProjectionCursor(
                    world_revision=committed.world_revision,
                    deliberation_revision=committed.deliberation_revision,
                    ledger_sequence=committed.ledger_sequence,
                ),
                len(replacement.expression_repin_reservation_ids),
            )
        raise ConcurrencyConflict(
            "expression repin reservation CAS did not converge"
        )

    async def _audit_expression_retry_with_durable_repins(
        self,
        *,
        observation: Observation,
        observation_event: WorldEvent,
        process: TriggerProcess,
        cursor: ProjectionCursor,
        turn_budget: InteractiveTurnBudget | None,
    ) -> ProposalAuditCommit | None:
        """Resume one attempt without renewing its durable re-pin allowance."""

        if self._pinned_turn is None or process.claim_lease is None:
            return None
        expression_attempt_id = process.claim_lease.attempt_id
        audited: ProposalAuditCommit | None = None

        # With no prior reservation this is recovery of the base call that may
        # have died immediately after claiming. Once any re-pin is durable,
        # every further provider call must first consume the next shared slot.
        if not process.expression_repin_reservation_ids:
            try:
                audited = await self._audit_expression_attempt_once(
                    observation=observation,
                    observation_event=observation_event,
                    cursor=cursor,
                    turn_budget=turn_budget,
                    expression_attempt_id=expression_attempt_id,
                )
            except asyncio.CancelledError:
                if await self._cancelled_expression_attempt_was_superseded(process):
                    return None
                raise
            except ConcurrencyConflict:
                if await self._expression_episode_was_superseded(process):
                    return None

        while audited is None:
            reservation = await self._reserve_expression_attempt_repin_cursor(
                observation=observation,
                observation_event=observation_event,
                expression_attempt_id=expression_attempt_id,
            )
            if reservation is None:
                exhausted_cursor = await self._expression_attempt_repin_cursor(
                    observation=observation,
                    observation_event=observation_event,
                    expression_attempt_id=expression_attempt_id,
                )
                if exhausted_cursor is None:
                    return None
                try:
                    return (
                        await self._pinned_turn.record_expression_repin_exhausted(
                            observation=observation,
                            observation_event=observation_event,
                            cursor=exhausted_cursor,
                            expression_attempt_id=expression_attempt_id,
                        )
                    )
                except ConcurrencyConflict:
                    if await self._expression_episode_was_superseded(process):
                        return None
                    raise
            retry_cursor, stale_ordinal = reservation
            _LOG.warning(
                "world v2 expression recovery repin trace=%s ordinal=%s "
                "to=%s/%s/%s",
                observation.trace_id,
                stale_ordinal,
                retry_cursor.world_revision,
                retry_cursor.deliberation_revision,
                retry_cursor.ledger_sequence,
            )
            try:
                audited = await self._audit_expression_attempt_once(
                    observation=observation,
                    observation_event=observation_event,
                    cursor=retry_cursor,
                    turn_budget=turn_budget,
                    expression_attempt_id=expression_attempt_id,
                )
            except asyncio.CancelledError:
                if await self._cancelled_expression_attempt_was_superseded(process):
                    return None
                raise
            except ConcurrencyConflict:
                if await self._expression_episode_was_superseded(process):
                    return None
        return audited

    async def _settle_expression_episode_for_action(
        self, result: ExternalObservation
    ) -> None:
        if result.status not in {"provider_accepted", "delivered", "failed", "unknown"}:
            return
        projection = await self._project_for_write()
        action = next(
            (item for item in projection.actions if item.action_id == result.action_id),
            None,
        )
        if action is None or action.expression_plan_id is None:
            return
        manifest = next(
            (
                item
                for item in projection.expression_plan_manifests
                if item.plan_id == action.expression_plan_id
            ),
            None,
        )
        if manifest is None:
            return
        audit = next(
            (
                item
                for item in projection.proposal_audits
                if item.proposal_id == manifest.proposal_id
            ),
            None,
        )
        if audit is None:
            return
        located = await self._lookup_event_commit(audit.trigger_ref)
        if located is None or located[0].event_type != "ObservationRecorded":
            return
        try:
            observation = Observation.model_validate_json(located[0].payload_json)
        except ValueError:
            return
        process = next(
            (
                item
                for item in projection.trigger_processes
                if item.trigger_id
                == expression_episode_trigger_id(
                    self._world_id, observation.observation_id
                )
                and item.state != "terminal"
            ),
            None,
        )
        if (
            action.kind == "typing"
            and self._pinned_turn is not None
            and self._pinned_turn.expression_episode_mode == "stream"
            and self._pinned_turn.has_expression_episode_tail(located[0].event_id)
        ):
            # The typing prelude has already crossed the provider boundary and
            # its receipt is durably settled before this hook runs.  Do not
            # hold the next authored beat behind the same stream's tail audit:
            # the visible beat's own receipt will join that audit, while the
            # typing pulse remains best-effort and non-semantic.
            return
        if process is not None and (
            process.state == "open"
            or process.claim_lease is None
            or (projection.logical_time or observation.logical_time)
            >= process.claim_lease.expires_at
        ):
            # Provider receipts may arrive long after the model-attempt lease.
            # Reclaim the exact lifecycle before completing it; otherwise the
            # Action can settle durably while TriggerProcessCompleted is
            # rejected as using an expired owner.
            reclaimed, _ = await self._claim_expression_episode(observation)
            if reclaimed is not None:
                process = reclaimed
                projection = await self._project_for_write()
        disposition = "complete_without_more"
        tail_audit = next(
            (
                item
                for item in projection.proposal_audits
                if item.trigger_ref == located[0].event_id
                and ":episode-append:" in item.proposal_id
            ),
            None,
        )
        if tail_audit is not None:
            try:
                tail_proposal = validate_proposal_envelope(
                    json.loads(tail_audit.proposal_json)
                )
            except (TypeError, ValueError):
                tail_audit = None
            else:
                if isinstance(tail_proposal, DecisionProposal):
                    disposition = (
                        tail_proposal.episode_disposition
                        or "complete_without_more"
                    )
        if self._pinned_turn is not None and process is not None:
            if tail_audit is None:
                tail_commit, disposition = (
                    await self._pinned_turn.audit_expression_episode_tail(
                        observation=observation,
                        observation_event=located[0],
                    )
                )
                if tail_commit is not None and tail_commit.proposal_id is not None:
                    refreshed = await self._project_for_write()
                    tail_audit = next(
                        (
                            item
                            for item in refreshed.proposal_audits
                            if item.proposal_id == tail_commit.proposal_id
                        ),
                        None,
                    )
            if (
                disposition == "append"
                and tail_audit is not None
                and self._expression_policy is not None
                and self._expression_recorder is not None
            ):
                tail_projection = await self._project_for_write()
                account = next(
                    (
                        item
                        for item in tail_projection.budget_accounts
                        if item.account_id == self._expression_policy.account_id
                    ),
                    None,
                )
                if tail_audit is not None and account is not None:
                    try:
                        material = derive_expression_plan_material(
                            audit=tail_audit,
                            cursor=ProjectionCursor(
                                world_revision=tail_projection.world_revision,
                                deliberation_revision=tail_projection.deliberation_revision,
                                ledger_sequence=tail_projection.ledger_sequence,
                            ),
                            world_id=self._world_id,
                            policy=self._expression_policy,
                            account=account,
                            logical_time=tail_projection.logical_time
                            or observation.logical_time,
                            created_at=observation.created_at,
                            trace_id=observation.trace_id,
                            correlation_id=observation.correlation_id,
                            payload_store=self._expression_payload_store,
                            source_observation=observation,
                        )
                    except ExpressionPlanAcceptanceError:
                        disposition = "complete_without_more"
                    else:
                        await self._commit_visible_acceptance(
                            recorder=self._expression_recorder,
                            acceptance_id=(
                                "acceptance:expression-plan:"
                                + tail_audit.proposal_id
                            ),
                            material=material,
                            actor=self._expression_policy.actor,
                            source="world-runtime:expression-episode-append",
                            trace_id=observation.trace_id,
                        )
        await self._complete_expression_episode(
            observation=observation,
            process=process,
            outcome_ref=f"expression-episode:{disposition}:{result.status}",
        )

    def _expression_episode_tail_is_runtime_owned(self, trigger_ref: str) -> bool:
        """Whether this runtime can still settle a visible Episode tail.

        A streamed tail is deliberately process-local: after restart the
        already-sent head is immutable and the missing tail must complete
        without regeneration.
        """

        if self._pinned_turn is None:
            return False
        return (
            self._pinned_turn.expression_episode_mode == "stream"
            and self._pinned_turn.has_expression_episode_tail(trigger_ref)
        )

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
                (
                    item
                    for item in projection.acceptance_decisions
                    if item.proposal_id == proposal_id
                ),
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
                if not isinstance(proposal_event_ref, str) or not isinstance(
                    proposal_payload_hash, str
                ):
                    raise RuntimeError("accepted appraisal manifest has no proposal provenance")
                proposal_located = await self._lookup_event_commit(proposal_event_ref)
                if (
                    proposal_located is None
                    or proposal_located[0].payload_hash != proposal_payload_hash
                ):
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
                (
                    item
                    for item in projection.appraisal_proposals
                    if item.proposal_id == proposal_id
                ),
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
                handle = self._appraisal_acceptance.pin_proposal(
                    cursor=cursor, proposal_id=proposal_id
                )
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
                code = (
                    exc.code
                    if isinstance(exc, AppraisalAcceptanceError)
                    else "appraisal.stale_cursor"
                )
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
                (
                    item
                    for item in projection.acceptance_decisions
                    if item.proposal_id == proposal_id
                ),
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
                if not isinstance(proposal_event_ref, str) or not isinstance(
                    proposal_payload_hash, str
                ):
                    raise RuntimeError("accepted affect manifest has no proposal provenance")
                proposal_located = await self._lookup_event_commit(proposal_event_ref)
                if (
                    proposal_located is None
                    or proposal_located[0].payload_hash != proposal_payload_hash
                ):
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
                handle = self._affect_acceptance.pin_proposal(
                    cursor=cursor, proposal_id=proposal_id
                )
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
                (
                    item
                    for item in projection.acceptance_decisions
                    if item.proposal_id == proposal_id
                ),
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
                json.dumps(
                    material, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ).encode()
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

    async def _commit_ingress_observation(
        self,
        *,
        observation: Observation,
        event: WorldEvent,
        _retry_ordinal: int = 0,
    ) -> tuple[
        Observation,
        WorldEvent,
        CommitResult,
        TriggerProcess | None,
        bool,
        tuple[str, ...],
        tuple[str, ...],
    ]:
        """Commit one Observation and its source-owned triggers under a short lock.

        The returned boolean distinguishes an idempotent existing Observation.
        No model, context compilation, acceptance, or external effect is allowed
        inside this phase: a newer inbound must be able to commit while an older
        provider invocation is still thinking.
        """

        async with self._lock:
            existing = await self._lookup_event_commit(event.event_id)
            if existing is not None:
                persisted, original_commit = existing
                try:
                    persisted_observation = Observation.model_validate_json(
                        persisted.payload_json
                    )
                except (TypeError, ValueError) as exc:
                    raise IdempotencyConflict(
                        "committed observation cannot be decoded"
                    ) from exc
                if persisted != event:
                    # A locked-head clock rebase and the process-local endpoint
                    # estimate are the only permitted retry differences. The
                    # estimate is advisory-only and is deliberately forgotten
                    # after the first ingress claim, so recovery must reuse the
                    # exact version already frozen in the Observation.
                    normalized_metadata = dict(observation.coalescing_metadata)
                    persisted_advisory = persisted_observation.coalescing_metadata.get(
                        "turn_attention_advisory"
                    )
                    if persisted_advisory is None:
                        normalized_metadata.pop("turn_attention_advisory", None)
                    else:
                        normalized_metadata["turn_attention_advisory"] = persisted_advisory
                    incoming_reply = dict(observation.reply_context or {})
                    persisted_reply = dict(persisted_observation.reply_context or {})
                    incoming_reply.pop("platform_message_id", None)
                    persisted_reply.pop("platform_message_id", None)
                    if incoming_reply != persisted_reply:
                        raise IdempotencyConflict(
                            "observation trigger was already committed with different reply authority"
                        )
                    if observation.payload_hash != persisted_observation.payload_hash and (
                        observation.payload_hash != _observation_ingress_payload_hash(observation)
                        or persisted_observation.payload_hash
                        != _observation_ingress_payload_hash(persisted_observation)
                    ):
                        raise IdempotencyConflict(
                            "observation trigger was already committed with different payload proof"
                        )
                    normalized_observation = observation.model_copy(
                        update={
                            "logical_time": persisted_observation.logical_time,
                            "coalescing_metadata": normalized_metadata,
                            # The live process may retain the original platform
                            # fragment id while restart recovery only has the
                            # durable coalesced batch id. The committed reply
                            # target is authoritative for this same batch.
                            "reply_context": persisted_observation.reply_context,
                            # The transport payload digest historically
                            # included the process-local attention advisory.
                            # Once every substantive field above matches, the
                            # first committed digest remains the batch truth.
                            "payload_hash": persisted_observation.payload_hash,
                        }
                    )
                    if (
                        normalized_observation != persisted_observation
                    ):
                        raise IdempotencyConflict(
                            "observation trigger was already committed with different content"
                        )
                return (
                    persisted_observation,
                    persisted,
                    original_commit,
                    None,
                    True,
                    (),
                    (),
                )

            before = await self._project_for_write()
            # WorldTurnRuntime resolves the clock just before entering this
            # lock. A concurrent scheduler tick can win that gap. Bind a new
            # Observation to the locked head; an idempotent retry above keeps
            # its original immutable envelope.
            locked_logical_time = before.logical_time
            if (
                locked_logical_time is not None
                and observation.logical_time != locked_logical_time
            ):
                observation = observation.model_copy(
                    update={"logical_time": locked_logical_time}
                )
                event = WorldEvent.from_payload(
                    schema_version=observation.schema_version,
                    event_id=event.event_id,
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

            # Observation, reconsideration, and source-owned trigger openings
            # are one ingress fact. Commit them as one CAS batch so a newer
            # Observation can atomically supersede a still-unanswered episode.
            superseded_expression_processes = tuple(
                process
                for process in before.trigger_processes
                if process.process_kind == "expression_episode"
                and process.state == "claimed"
                and process.claim_lease is not None
                and (
                    (
                        self._pinned_turn is not None
                        and self._pinned_turn.expression_episode_mode == "stream"
                    )
                    or not expression_episode_has_authorized_action(before, process)
                )
            )
            superseded_expression_events = tuple(
                expression_episode_complete_event(
                    world_id=self._world_id,
                    process=process,
                    at=before.logical_time or observation.logical_time,
                    trace_id=observation.trace_id,
                    correlation_id=observation.correlation_id,
                    outcome_ref="expression-episode:superseded-by-newer-inbound",
                    superseding_observation_event_ref=event.event_id,
                )
                for process in superseded_expression_processes
            )
            superseded_expression_attempt_ids = tuple(
                process.claim_lease.attempt_id
                for process in superseded_expression_processes
            )
            superseded_expression_source_refs = {
                process.source_evidence_ref
                for process in superseded_expression_processes
                if process.source_evidence_ref is not None
            }
            appraisal_audit_trigger_refs = {
                audit.trigger_ref
                for audit in before.proposal_audits
                if is_interaction_appraisal_audit(audit)
            }
            observation_event_ref_by_id = {
                source.observation_id: authority.event_id
                for source in before.message_observations
                if 0 < source.world_revision <= len(before.committed_world_event_refs)
                and (
                    authority := before.committed_world_event_refs[
                        source.world_revision - 1
                    ]
                ).event_type
                == "ObservationRecorded"
                and authority.payload_hash == source.event_payload_hash
            }
            folded_appraisal_events = tuple(
                interaction_appraisal_folded_event(
                    process=process,
                    superseding_observation=observation,
                    superseding_observation_event=event,
                )
                for process in before.trigger_processes
                if process.process_kind == "interaction_appraisal"
                and process.state == "claimed"
                and process.claim_lease is not None
                and process.source_evidence_ref
                in superseded_expression_source_refs
                and observation_event_ref_by_id.get(process.source_evidence_ref)
                not in appraisal_audit_trigger_refs
            )
            folded_appraisal_trigger_ids = tuple(
                event.payload()["trigger_id"]
                for event in folded_appraisal_events
            )
            ingress_events = [
                event,
                *superseded_expression_events,
                *folded_appraisal_events,
                *expression_reconsideration_events_for_observation(
                    projection=before,
                    observation=observation,
                    source_event=event,
                ),
            ]
            episode_process: TriggerProcess | None = None
            if self._pinned_turn is not None:
                # Reliability lifecycle is distinct from the optional
                # speculative Episode feature. Open and claim it with the
                # Observation so a crash after this short phase is recoverable.
                opened_event = expression_episode_open_event(
                    observation=observation,
                    observation_event=event,
                )
                opened_process = TriggerProcess.model_validate_json(
                    json.dumps(opened_event.payload()["process"])
                )
                claimed_event, episode_process = expression_episode_claim_event(
                    world_id=self._world_id,
                    process=opened_process,
                    owner_id=self._expression_episode_owner,
                    at=before.logical_time or observation.logical_time,
                    trace_id=observation.trace_id,
                    correlation_id=observation.correlation_id,
                    technical_failure_count=0,
                )
                ingress_events.extend((opened_event, claimed_event))
            if self._inbound_state_owner is not None:
                ingress_events.extend(
                    interaction_appraisal_trigger_events(
                        observation=observation,
                        observation_event=event,
                        owner_id=self._inbound_state_owner,
                    )
                )
            if self._interaction_fact_owner is not None:
                ingress_events.append(
                    interaction_fact_trigger_event(
                        observation=observation,
                        observation_event=event,
                    )
                )
            if self._perception_owner is not None and observation.attachment_refs:
                ingress_events.append(
                    perception_trigger_event(
                        observation=observation,
                        observation_event=event,
                    )
                )
            try:
                committed = await self._commit(
                    ingress_events,
                    world_revision=before.world_revision,
                    deliberation_revision=before.deliberation_revision,
                )
            except ConcurrencyConflict:
                if _retry_ordinal + 1 >= _INGRESS_CAS_MAX_ATTEMPTS:
                    raise
            else:
                return (
                    observation,
                    event,
                    committed,
                    episode_process,
                    False,
                    superseded_expression_attempt_ids,
                    folded_appraisal_trigger_ids,
                )

        # ``self._lock`` coordinates only this Runtime instance. Background
        # workers and another Runtime over the same SQLite World may still win
        # the shared ledger head. Re-enter the whole short phase so logical
        # time, superseded episodes, and every source-owned trigger are rebuilt
        # from one fresh projection; no partially stale batch is reused.
        return await self._commit_ingress_observation(
            observation=observation,
            event=event,
            _retry_ordinal=_retry_ordinal + 1,
        )

    async def ingest(
        self,
        observation: Observation,
        *,
        turn_budget: InteractiveTurnBudget | None = None,
    ) -> RuntimeOutcome:
        started = time.perf_counter()
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
        acceptance_technical_failure = False
        expression_superseded_by_inbound = False
        assessment_proposal: DecisionProposal | MinimalProposal | None = None
        episode_process: TriggerProcess | None = None
        (
            observation,
            event,
            committed,
            episode_process,
            existing_observation,
            superseded_expression_attempt_ids,
            _folded_appraisal_trigger_ids,
        ) = await self._commit_ingress_observation(
            observation=observation,
            event=event,
        )
        if self._pinned_turn is not None:
            await self._pinned_turn.cancel_superseded_expression_streams(
                event.event_id
            )
        await self._cancel_superseded_expression_attempt_tasks(
            superseded_expression_attempt_ids
        )
        if existing_observation:
            return await self._existing_observation_outcome(
                observation=observation,
                observation_event=event,
                original_commit=committed,
                trigger_id=trigger_id,
                retry_turn_budget=turn_budget,
            )
        else:
            _LOG.warning(
                "world v2 ingest phase trace=%s phase=ingress_commit_ms value=%.1f",
                observation.trace_id,
                (time.perf_counter() - started) * 1000,
            )
            # The sole CharacterInterior ordinary-inbound turn owns every
            # visible expression beat.  Nothing may author or commit a side
            # reaction between ingress and this cursor pin.
            reply_cursor = ProjectionCursor(
                world_revision=committed.world_revision,
                deliberation_revision=committed.deliberation_revision,
                ledger_sequence=committed.ledger_sequence,
            )
            if self._pinned_turn is not None:
                episode_process, episode_cursor = await self._claim_expression_episode(
                    observation
                )
                if episode_cursor is not None:
                    reply_cursor = episode_cursor
                cadence_draws = ()
                if self._pinned_turn.recorded_cadence_mode != "off":
                    draw_head = await self._project_for_write()
                    draw_kwargs = dict(
                        authority=RandomAuthority(ledger=self._ledger),
                        attempt_id=f"attempt:expression-cadence:{event.event_id}",
                        beat_count=8,
                        logical_time=draw_head.logical_time or observation.logical_time,
                        actor="system:expression-cadence",
                        trace_id=observation.trace_id,
                        correlation_id=observation.correlation_id,
                    )
                    cadence_draws = (
                        await asyncio.to_thread(record_cadence_draws, **draw_kwargs)
                        if self._ledger.blocks_event_loop
                        else record_cadence_draws(**draw_kwargs)
                    )
                    draw_head = await self._project_for_write()
                    reply_cursor = ProjectionCursor(
                        world_revision=draw_head.world_revision,
                        deliberation_revision=draw_head.deliberation_revision,
                        ledger_sequence=draw_head.ledger_sequence,
                    )
                expression_attempt_id = (
                    episode_process.claim_lease.attempt_id
                    if episode_process is not None
                    and episode_process.claim_lease is not None
                    else None
                )
                try:
                    if expression_attempt_id is None:
                        audited = await self._pinned_turn.audit_observation(
                            observation=observation,
                            observation_event=event,
                            cursor=reply_cursor,
                            turn_budget=turn_budget,
                            recorded_cadence_draws=cadence_draws,
                        )
                    else:
                        audited = await self._audit_expression_attempt_once(
                            observation=observation,
                            observation_event=event,
                            cursor=reply_cursor,
                            turn_budget=turn_budget,
                            recorded_cadence_draws=cadence_draws,
                            expression_attempt_id=expression_attempt_id,
                        )
                except asyncio.CancelledError:
                    # A newer Observation first terminalizes this episode in
                    # the ledger and only then cancels its obsolete local
                    # provider task.  Treat that exact cancellation as the
                    # already-recorded supersession; unrelated caller or
                    # shutdown cancellation must still propagate.
                    if await self._cancelled_expression_attempt_was_superseded(
                        episode_process
                    ):
                        expression_superseded_by_inbound = True
                    else:
                        raise
                except ConcurrencyConflict as initial_stale:
                    # A newer Observation may deliberately terminate this
                    # still-unanswered episode while its provider is in flight.
                    # The old candidate then has no audit/Action authority.
                    if await self._expression_episode_was_superseded(
                        episode_process
                    ):
                        expression_superseded_by_inbound = True
                    elif expression_attempt_id is None:
                        raise
                    else:
                        # Action receipts and other independent workers may
                        # advance the head while the model is composing. A
                        # content-bearing draft can never be rebased. Discard
                        # it and give the same character a bounded chance to
                        # author again against the current complete Context.
                        # If the head remains busy, leave the durable episode
                        # retryable rather than surfacing a transport error or
                        # inventing a fallback reply.
                        # Repinning is still the same user-visible turn.  Its
                        # author may use only the original absolute deadline;
                        # a cursor race is not authority to mint another model
                        # budget. A later durable retry gets its own budget only
                        # after the event-sourced retry lifecycle says it is due.
                        stale_repin_budget = turn_budget
                        for _ in range(EXPRESSION_FRESH_CONTEXT_REPIN_LIMIT):
                            reservation = (
                                await self._reserve_expression_attempt_repin_cursor(
                                    observation=observation,
                                    observation_event=event,
                                    expression_attempt_id=expression_attempt_id,
                                )
                            )
                            if reservation is None:
                                if await self._expression_episode_was_superseded(
                                    episode_process
                                ):
                                    expression_superseded_by_inbound = True
                                break
                            retry_cursor, stale_ordinal = reservation
                            _LOG.warning(
                                "world v2 expression repin trace=%s ordinal=%s "
                                "from=%s/%s/%s to=%s/%s/%s",
                                observation.trace_id,
                                stale_ordinal,
                                reply_cursor.world_revision,
                                reply_cursor.deliberation_revision,
                                reply_cursor.ledger_sequence,
                                retry_cursor.world_revision,
                                retry_cursor.deliberation_revision,
                                retry_cursor.ledger_sequence,
                            )
                            try:
                                repin_budget_exhausted = (
                                    stale_repin_budget is not None
                                    and stale_repin_budget.author_remaining() <= 0
                                )
                                audited = await self._audit_expression_attempt_once(
                                    observation=observation,
                                    observation_event=event,
                                    cursor=retry_cursor,
                                    turn_budget=stale_repin_budget,
                                    recorded_cadence_draws=cadence_draws,
                                    expression_attempt_id=expression_attempt_id,
                                )
                                if (
                                    repin_budget_exhausted
                                    and audited.proposal_id is None
                                ):
                                    reply_deferred_refs = (
                                        *reply_deferred_refs,
                                        "expression_episode.repin_budget_exhausted",
                                    )
                                break
                            except ConcurrencyConflict:
                                if await self._expression_episode_was_superseded(
                                    episode_process
                                ):
                                    expression_superseded_by_inbound = True
                                    break
                                continue
                        if (
                            audited is None
                            and not expression_superseded_by_inbound
                        ):
                            exhausted_cursor = (
                                await self._expression_attempt_repin_cursor(
                                    observation=observation,
                                    observation_event=event,
                                    expression_attempt_id=expression_attempt_id,
                                )
                            )
                            if exhausted_cursor is not None:
                                audited = (
                                    await self._pinned_turn.record_expression_repin_exhausted(
                                        observation=observation,
                                        observation_event=event,
                                        cursor=exhausted_cursor,
                                        expression_attempt_id=expression_attempt_id,
                                    )
                                )
                                reply_deferred_refs = (
                                    *reply_deferred_refs,
                                    "expression_episode.repin_budget_exhausted",
                                )
                            else:
                                reply_deferred_refs = (
                                    *reply_deferred_refs,
                                    "expression_episode.stale_repin_pending",
                                )
                                _LOG.warning(
                                    "world v2 expression repin deferred trace=%s "
                                    "error=%s",
                                    observation.trace_id,
                                    type(initial_stale).__name__,
                                )
                _LOG.warning(
                    "world v2 ingest phase trace=%s phase=reply_audit_ms value=%.1f",
                    observation.trace_id,
                    (time.perf_counter() - started) * 1000,
                )
            # CharacterInterior's one audited inbound decision is consumed by
            # specialized authorities in strict order.  Appraisal/Affect wait
            # until the visible Expression has crossed its own acceptance so
            # no sibling authority makes that cursor stale.  This is the
            # canonical inbound path, not a compatibility worker or a second
            # semantic author lane.
            inline_inbound_state = (
                (audited.cursor, audited.proposal_id)
                if (
                self._appraisal_worker is not None
                and audited is not None
                and audited.proposal_id
                )
                else None
            )
            if audited is not None and audited.proposal_id is not None:
                assessment_head = await self._project_for_write()
                assessment_audit = next(
                    (
                        item
                        for item in assessment_head.proposal_audits
                        if item.proposal_id == audited.proposal_id
                        and item.proposal_kind in {"decision", "minimal"}
                    ),
                    None,
                )
                if assessment_audit is not None:
                    assessment_proposal = validate_proposal_envelope(
                        json.loads(assessment_audit.proposal_json)
                    )
                    if not isinstance(
                        assessment_proposal, (DecisionProposal, MinimalProposal)
                    ):
                        assessment_proposal = None
            if self._pinned_turn is not None and audited is not None:
                if self._reply_policy is not None and audited.proposal_id is not None:
                    after_audit = await self._project_for_write()
                    audit = next(
                        (
                            item
                            for item in after_audit.proposal_audits
                            if item.proposal_id == audited.proposal_id
                        ),
                        None,
                    )
                    account = next(
                        (
                            item
                            for item in after_audit.budget_accounts
                            if item.account_id == self._reply_policy.account_id
                        ),
                        None,
                    )
                    if audit is not None and audit.proposal_kind == "minimal":
                        proposal = validate_proposal_envelope(json.loads(audit.proposal_json))
                        timing_choice = (
                            "silent"
                            if isinstance(proposal, MinimalProposal)
                            and not proposal.proposed_changes
                            and not proposal.action_intents
                            else "later"
                            if isinstance(proposal, MinimalProposal)
                            and len(proposal.action_intents) == 1
                            and proposal.action_intents[0].kind == "followup"
                            else "now"
                        )
                        if timing_choice == "later":
                            if self._social_action_worker is None:
                                reply_deferred_refs = ("social_action.deferred_pending",)
                            else:
                                social = await self._social_action_worker.run_observation(
                                    observation.observation_id
                                )
                                if social.status in {"deferred", "duplicate"}:
                                    reply_deferred_refs = (
                                        f"social_action.deferred:{social.action_id}",
                                    )
                                elif social.status == "budget_exhausted":
                                    reply_terminal_errors = (
                                        social.reason_code or "social_action.budget_exhausted",
                                    )
                                else:
                                    reply_terminal_errors = (
                                        social.reason_code or f"social_action.{social.status}",
                                    )
                        elif timing_choice == "silent":
                            pass
                        elif account is None:
                            failure_code = (
                                "minimal_reply_acceptance."
                                "budget_account_unavailable"
                            )
                            reply_deferred_refs = (failure_code,)
                            acceptance_technical_failure = True
                            await self._record_reply_acceptance_failure(
                                audit=audit,
                                observation=observation,
                                failure_code=failure_code,
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
                                    logical_time=after_audit.logical_time
                                    or observation.logical_time,
                                    created_at=observation.created_at,
                                    trace_id=observation.trace_id,
                                    correlation_id=observation.correlation_id,
                                )
                            except MinimalReplyAcceptanceError as exc:
                                reply_deferred_refs = (exc.code,)
                                acceptance_technical_failure = True
                                await self._record_reply_acceptance_failure(
                                    audit=audit,
                                    observation=observation,
                                    failure_code=exc.code,
                                )
                            else:
                                assert self._reply_recorder is not None
                                committed = await self._commit_visible_acceptance(
                                    recorder=self._reply_recorder,
                                    acceptance_id=f"acceptance:minimal-reply:{audit.proposal_id}",
                                    material=material,
                                    actor=self._reply_policy.actor,
                                    source="world-runtime:acceptance",
                                    trace_id=observation.trace_id,
                                )
                                reply_authorized = True
                                authorized_action_ids = (material.action.action_id,)
                    elif (
                        audit is not None
                        and audit.proposal_kind == "decision"
                        and self._expression_policy is not None
                    ):
                        proposal = validate_proposal_envelope(json.loads(audit.proposal_json))
                        timing_choice = (
                            proposal.timing_choice
                            if isinstance(proposal, DecisionProposal)
                            else "now"
                        )
                        if timing_choice == "later":
                            if self._social_action_worker is None:
                                reply_deferred_refs = ("social_action.deferred_pending",)
                            else:
                                social = await self._social_action_worker.run_observation(
                                    observation.observation_id
                                )
                                if social.status in {"deferred", "duplicate"}:
                                    reply_deferred_refs = (
                                        f"social_action.deferred:{social.action_id}",
                                    )
                                elif social.status == "budget_exhausted":
                                    reply_terminal_errors = (
                                        social.reason_code or "social_action.budget_exhausted",
                                    )
                                else:
                                    reply_terminal_errors = (
                                        social.reason_code or f"social_action.{social.status}",
                                    )
                        elif timing_choice != "silent":
                            account = next(
                                (
                                    item
                                    for item in after_audit.budget_accounts
                                    if item.account_id == self._expression_policy.account_id
                                ),
                                None,
                            )
                            if account is None:
                                failure_code = (
                                    "expression_plan_acceptance."
                                    "budget_account_unavailable"
                                )
                                reply_deferred_refs = (failure_code,)
                                acceptance_technical_failure = True
                                await self._record_reply_acceptance_failure(
                                    audit=audit,
                                    observation=observation,
                                    failure_code=failure_code,
                                )
                            else:
                                try:
                                    material = derive_expression_plan_material(
                                        audit=audit,
                                        cursor=ProjectionCursor(
                                            world_revision=after_audit.world_revision,
                                            deliberation_revision=after_audit.deliberation_revision,
                                            ledger_sequence=after_audit.ledger_sequence,
                                        ),
                                        world_id=self._world_id,
                                        policy=self._expression_policy,
                                        account=account,
                                        logical_time=after_audit.logical_time
                                        or observation.logical_time,
                                        created_at=observation.created_at,
                                        trace_id=observation.trace_id,
                                        correlation_id=observation.correlation_id,
                                        payload_store=self._expression_payload_store,
                                        source_observation=observation,
                                    )
                                except ExpressionPlanAcceptanceError as exc:
                                    reply_deferred_refs = (exc.code,)
                                    acceptance_technical_failure = True
                                    await self._record_reply_acceptance_failure(
                                        audit=audit,
                                        observation=observation,
                                        failure_code=exc.code,
                                    )
                                else:
                                    assert self._expression_recorder is not None
                                    committed = await self._commit_visible_acceptance(
                                        recorder=self._expression_recorder,
                                        acceptance_id=f"acceptance:expression-plan:{audit.proposal_id}",
                                        material=material,
                                        actor=self._expression_policy.actor,
                                        source="world-runtime:expression-acceptance",
                                        trace_id=observation.trace_id,
                                    )
                                    reply_authorized = True
                                    authorized_action_ids = tuple(
                                        item.action.action_id for item in material.beats
                                    )
        if inline_inbound_state is not None:
            advisory_deferred = await self._settle_unified_inbound_state(
                observation=observation,
                observation_event=event,
                audit_cursor=inline_inbound_state[0],
                proposal_id=inline_inbound_state[1],
            )
            reply_deferred_refs = (*reply_deferred_refs, *advisory_deferred)

        if reply_authorized:
            status = "action_authorized"
        elif reply_terminal_errors:
            status = "failed_safe"
        elif reply_deferred_refs:
            status = "deferred"
        else:
            status = "observed_only"
        technical_expression_failure = self._pinned_turn is not None and (
            not expression_superseded_by_inbound
            and (
                audited is None
                or audited.proposal_id is None
                or acceptance_technical_failure
            )
        )
        if technical_expression_failure:
            episode_process = await self._ensure_expression_retry_process(
                observation=observation,
                observation_event=event,
            )
            status = "deferred"
            if not reply_deferred_refs:
                reply_deferred_refs = (
                    "expression_episode.technical_retry_pending",
                )
        # This advisory ledger record must never sit in front of visible reply
        # authorization. Its helper is effect-once and absorbs a bounded CAS
        # race, so background World progress cannot turn a valid reply into
        # "typing, then silence".
        if assessment_proposal is not None:
            await self._record_response_expectation_assessment(
                proposal=assessment_proposal,
                observation=observation,
                observation_event=event,
            )
        episode_tail_pending = (
            reply_authorized
            and self._expression_episode_tail_is_runtime_owned(event.event_id)
        )
        if (
            not episode_tail_pending
            and not technical_expression_failure
            and not expression_superseded_by_inbound
        ):
            await self._complete_expression_episode(
                observation=observation,
                process=episode_process,
                outcome_ref=f"expression-episode:{status}",
            )
        final_projection = await self._project_for_write()
        _LOG.warning(
            "world v2 ingest phase trace=%s phase=complete_ms value=%.1f status=%s",
            observation.trace_id,
            (time.perf_counter() - started) * 1000,
            status,
        )
        return RuntimeOutcome(
            outcome_id=f"outcome:{trigger_id}",
            trigger_id=trigger_id,
            observation_ref=observation.observation_id,
            committed_world_revision=final_projection.world_revision,
            ledger_sequence=final_projection.ledger_sequence,
            status=status,
            authorized_action_ids=authorized_action_ids if reply_authorized else (),
            deferred_refs=reply_deferred_refs,
            terminal_errors=reply_terminal_errors,
            projection_hint=f"world-revision:{final_projection.world_revision}",
        )

    async def _existing_observation_outcome(
        self,
        *,
        observation: Observation,
        observation_event: WorldEvent,
        original_commit: CommitResult,
        trigger_id: str,
        retry_process: TriggerProcess | None = None,
        retry_cursor: ProjectionCursor | None = None,
        retry_turn_budget: InteractiveTurnBudget | None = None,
    ) -> RuntimeOutcome:
        """Join a completed reply acceptance without repeating model work.

        The Observation itself commits before its deliberation and acceptance
        follow-ups.  On ingress retry, the durable minimal manifest is the
        authority for the final visible outcome; returning the Observation's
        old cursor would incorrectly erase an already-authorized reply.
        """

        projection = await self._project_for_write()
        episode = next(
            (
                item
                for item in projection.trigger_processes
                if item.trigger_id
                == expression_episode_trigger_id(
                    self._world_id, observation.observation_id
                )
            ),
            None,
        )

        def reply_audit_from(current_projection):
            decided_proposal_ids = {
                decision.proposal_id
                for decision in current_projection.acceptance_decisions
                if decision.status in {"rejected", "stale"}
            }
            for candidate in reversed(current_projection.proposal_audits):
                if (
                    candidate.trigger_ref != observation_event.event_id
                    or candidate.proposal_id.startswith(
                        _HISTORICAL_QUICK_REACTION_PROPOSAL_PREFIX
                    )
                    or candidate.proposal_id in decided_proposal_ids
                ):
                    continue
                try:
                    proposal = validate_proposal_envelope(
                        json.loads(candidate.proposal_json)
                    )
                except (TypeError, ValueError):
                    continue
                bound_attempt = (
                    episode is not None
                    and candidate.attempt_id in episode.attempt_ids
                )
                expression_family = candidate.proposal_id.startswith(
                    ("proposal:expression:", "proposal:chat-reply:")
                )
                if (
                    isinstance(proposal, (DecisionProposal, MinimalProposal))
                    and (bound_attempt or expression_family)
                ):
                    return candidate, proposal
            return None

        async def acceptance_retry_outcome(audit, failure_code: str) -> RuntimeOutcome:
            decision_status = await self._record_reply_acceptance_failure(
                audit=audit,
                observation=observation,
                failure_code=failure_code,
            )
            current = await self._project_for_write()
            return RuntimeOutcome(
                outcome_id=f"outcome:{trigger_id}",
                trigger_id=trigger_id,
                observation_ref=observation.observation_id,
                committed_world_revision=current.world_revision,
                ledger_sequence=current.ledger_sequence,
                status="deferred",
                deferred_refs=(
                    failure_code,
                    f"expression_acceptance.{decision_status}",
                ),
                projection_hint=f"world-revision:{current.world_revision}",
            )

        async def settle_recovered_inbound_state(audit) -> None:
            if (
                self._appraisal_worker is None
                or audit.proposal_kind != "decision"
            ):
                return
            located = await self._lookup_event_commit(audit.event_ref)
            if located is None:
                raise RuntimeError("recovered inbound proposal audit event is unavailable")
            audit_commit = located[1]
            await self._settle_unified_inbound_state(
                observation=observation,
                observation_event=observation_event,
                audit_cursor=ProjectionCursor(
                    world_revision=audit_commit.world_revision,
                    deliberation_revision=audit_commit.deliberation_revision,
                    ledger_sequence=audit_commit.ledger_sequence,
                ),
                proposal_id=audit.proposal_id,
            )

        reply_audit = reply_audit_from(projection)
        if (
            reply_audit is None
            and episode is not None
            and episode.state != "terminal"
            and self._pinned_turn is not None
        ):
            if retry_process is not None and retry_cursor is not None:
                claimed, cursor = retry_process, retry_cursor
            elif (
                episode.state == "claimed"
                and episode.claim_lease is not None
                and episode.claim_lease.owner_id
                == self._expression_episode_owner
                and (projection.logical_time or observation.logical_time)
                < episode.claim_lease.expires_at
                and (
                    (
                        (
                            due_at := expression_episode_work_due(
                                projection,
                                episode,
                                owner_id=self._expression_episode_owner,
                            )
                        )
                        is not None
                        and due_at
                        <= (projection.logical_time or observation.logical_time)
                    )
                    or not any(
                        item.trigger_ref == observation_event.event_id
                        and item.attempt_id == episode.claim_lease.attempt_id
                        for item in projection.model_result_audits
                    )
                )
            ):
                # The process may have committed its claim and then crashed
                # before its reply-lane provider result was recorded. Other
                # lane audits on the same Observation do not change that
                # fact. There is no failed attempt to back off from, so the
                # duplicate on the same live Runtime resumes under the same
                # durable claim. A foreign Runtime must wait for expiry.
                claimed = episode
                cursor = ProjectionCursor(
                    world_revision=projection.world_revision,
                    deliberation_revision=projection.deliberation_revision,
                    ledger_sequence=projection.ledger_sequence,
                )
            else:
                claimed, cursor = await self._claim_expression_episode(observation)
            # A duplicate ingress before the retry lease expires is a pure
            # join.  Only the scheduler-owned reclaim receives a fresh cursor
            # and may invoke the model again.
            if cursor is not None:
                if claimed.claim_lease is None:
                    raise ConcurrencyConflict(
                        "expression episode retry lost its claim lease"
                    )
                await self._audit_expression_retry_with_durable_repins(
                    observation=observation,
                    observation_event=observation_event,
                    process=claimed,
                    cursor=cursor,
                    turn_budget=retry_turn_budget,
                )
                projection = await self._project_for_write()
                reply_audit = reply_audit_from(projection)
            episode = claimed or episode
        if (
            episode is not None
            and episode.state != "terminal"
            and (
                episode.state == "open"
                or episode.claim_lease is None
                or (projection.logical_time or observation.logical_time)
                >= episode.claim_lease.expires_at
            )
        ):
            # Proposal/manifest continuation also owns a lease.  If the
            # process died after recording its exact model result and that
            # lease later expired, reclaim before completing rather than
            # borrowing the stale attempt or regenerating prose.
            claimed, _ = await self._claim_expression_episode(observation)
            if claimed is not None:
                episode = claimed
                projection = await self._project_for_write()
                reply_audit = reply_audit_from(projection)
        # A quick-reaction manifest shares the observation trigger but is not
        # the visible answer; retry joining must resolve the reply lane only.
        manifest = next(
            (
                item
                for item in projection.minimal_reply_manifests
                if any(
                    audit.proposal_id == item.proposal_id
                    and audit.event_ref == item.proposal_event_ref
                    and audit.trigger_ref == observation_event.event_id
                    and not audit.proposal_id.startswith(
                        _HISTORICAL_QUICK_REACTION_PROPOSAL_PREFIX
                    )
                    for audit in projection.proposal_audits
                )
            ),
            None,
        )
        generic_manifest = next(
            (
                item
                for item in projection.expression_plan_manifests
                if any(
                    audit.proposal_id == item.proposal_id
                    and audit.event_ref == item.proposal_event_ref
                    and audit.trigger_ref == observation_event.event_id
                    and not audit.proposal_id.startswith(
                        _HISTORICAL_QUICK_REACTION_PROPOSAL_PREFIX
                    )
                    for audit in projection.proposal_audits
                )
            ),
            None,
        )
        if generic_manifest is not None:
            generic_audit = next(
                item
                for item in projection.proposal_audits
                if item.proposal_id == generic_manifest.proposal_id
                and item.event_ref == generic_manifest.proposal_event_ref
            )
            await settle_recovered_inbound_state(generic_audit)
            projection = await self._project_for_write()
            committed = original_commit
            for beat in generic_manifest.beats:
                persisted = await self._lookup_event_commit(
                    expression_plan_event_id(
                        manifest_hash=generic_manifest.manifest_hash,
                        role="action",
                        stable_id=beat.action.action_id,
                    )
                )
                if persisted is None:
                    # Social deferred acceptance deliberately owns a separate
                    # event-identity namespace while projecting the same
                    # immutable expression manifest. Recover by exact action
                    # id from committed ActionAuthorized authority.
                    for ref in reversed(projection.committed_world_event_refs):
                        if ref.event_type != "ActionAuthorized":
                            continue
                        candidate = await self._lookup_event_commit(ref.event_id)
                        if candidate is None:
                            continue
                        action_raw = candidate[0].payload().get("action")
                        if (
                            isinstance(action_raw, dict)
                            and action_raw.get("action_id") == beat.action.action_id
                        ):
                            persisted = candidate
                            break
                if persisted is None or persisted[0].event_type != "ActionAuthorized":
                    raise RuntimeError("expression plan manifest has no durable action event")
                committed = persisted[1]
            deferred = all(item.action.kind == "followup" for item in generic_manifest.beats)
            if (
                deferred
                or not self._expression_episode_tail_is_runtime_owned(
                    observation_event.event_id
                )
            ):
                await self._complete_expression_episode(
                    observation=observation,
                    process=episode,
                    outcome_ref=(
                        "expression-episode:deferred"
                        if deferred
                        else "expression-episode:action_authorized"
                    ),
                )
                projection = await self._project_for_write()
            return RuntimeOutcome(
                outcome_id=f"outcome:{trigger_id}",
                trigger_id=trigger_id,
                observation_ref=observation.observation_id,
                committed_world_revision=projection.world_revision,
                ledger_sequence=projection.ledger_sequence,
                status=(
                    "deferred"
                    if deferred
                    else "action_authorized"
                ),
                authorized_action_ids=(
                    ()
                    if deferred
                    else tuple(item.action.action_id for item in generic_manifest.beats)
                ),
                deferred_refs=(
                    tuple(
                        f"social_action.deferred:{item.action.action_id}"
                        for item in generic_manifest.beats
                    )
                    if deferred
                    else ()
                ),
                projection_hint=f"world-revision:{committed.world_revision}",
            )
        if manifest is None and reply_audit is not None:
            audit, proposal = reply_audit
            source_ref = next(
                (
                    item
                    for item in projection.message_observations
                    if item.observation_id == observation.observation_id
                ),
                None,
            )
            if source_ref is not None and any(
                item.world_revision > source_ref.world_revision
                for item in projection.message_observations
            ):
                # The provider result is durable, but it was overtaken before
                # gaining Action authority.  Preserve the audit and terminate
                # the old lifecycle; never send old prose after a newer turn.
                await self._complete_expression_episode(
                    observation=observation,
                    process=episode,
                    outcome_ref="expression-episode:superseded-by-newer-inbound",
                )
                projection = await self._project_for_write()
                return RuntimeOutcome(
                    outcome_id=f"outcome:{trigger_id}",
                    trigger_id=trigger_id,
                    observation_ref=observation.observation_id,
                    committed_world_revision=projection.world_revision,
                    ledger_sequence=projection.ledger_sequence,
                    status="observed_only",
                    deferred_refs=("expression_episode.superseded",),
                    projection_hint=f"world-revision:{projection.world_revision}",
                )

            timing_choice = (
                proposal.timing_choice
                if isinstance(proposal, DecisionProposal)
                else "silent"
                if not proposal.proposed_changes and not proposal.action_intents
                else "later"
                if len(proposal.action_intents) == 1
                and proposal.action_intents[0].kind == "followup"
                else "now"
            )
            if timing_choice == "silent":
                await settle_recovered_inbound_state(audit)
                await self._complete_expression_episode(
                    observation=observation,
                    process=episode,
                    outcome_ref="expression-episode:model-silent",
                )
                projection = await self._project_for_write()
                return RuntimeOutcome(
                    outcome_id=f"outcome:{trigger_id}",
                    trigger_id=trigger_id,
                    observation_ref=observation.observation_id,
                    committed_world_revision=projection.world_revision,
                    ledger_sequence=projection.ledger_sequence,
                    status="observed_only",
                    projection_hint=f"world-revision:{projection.world_revision}",
                )
            if timing_choice == "later":
                if self._social_action_worker is None:
                    return RuntimeOutcome(
                        outcome_id=f"outcome:{trigger_id}",
                        trigger_id=trigger_id,
                        observation_ref=observation.observation_id,
                        committed_world_revision=projection.world_revision,
                        ledger_sequence=projection.ledger_sequence,
                        status="deferred",
                        deferred_refs=("social_action.deferred_pending",),
                        projection_hint=f"world-revision:{projection.world_revision}",
                    )
                social = await self._social_action_worker.run_observation(
                    observation.observation_id
                )
                await settle_recovered_inbound_state(audit)
                if social.status in {"deferred", "duplicate"}:
                    await self._complete_expression_episode(
                        observation=observation,
                        process=episode,
                        outcome_ref="expression-episode:deferred",
                    )
                    projection = await self._project_for_write()
                    action_ids = social.action_ids or (
                        (social.action_id,) if social.action_id is not None else ()
                    )
                    return RuntimeOutcome(
                        outcome_id=f"outcome:{trigger_id}",
                        trigger_id=trigger_id,
                        observation_ref=observation.observation_id,
                        committed_world_revision=projection.world_revision,
                        ledger_sequence=projection.ledger_sequence,
                        status="deferred",
                        deferred_refs=tuple(
                            f"social_action.deferred:{action_id}"
                            for action_id in action_ids
                        ),
                        projection_hint=f"world-revision:{projection.world_revision}",
                    )
                if social.status == "stale":
                    await self._complete_expression_episode(
                        observation=observation,
                        process=episode,
                        outcome_ref="expression-episode:superseded-by-newer-inbound",
                    )
                    projection = await self._project_for_write()
                    return RuntimeOutcome(
                        outcome_id=f"outcome:{trigger_id}",
                        trigger_id=trigger_id,
                        observation_ref=observation.observation_id,
                        committed_world_revision=projection.world_revision,
                        ledger_sequence=projection.ledger_sequence,
                        status="observed_only",
                        deferred_refs=(
                            social.reason_code or "social_action.cursor_stale",
                        ),
                        projection_hint=f"world-revision:{projection.world_revision}",
                    )
                return RuntimeOutcome(
                    outcome_id=f"outcome:{trigger_id}",
                    trigger_id=trigger_id,
                    observation_ref=observation.observation_id,
                    committed_world_revision=projection.world_revision,
                    ledger_sequence=projection.ledger_sequence,
                    status=(
                        "failed_safe"
                        if social.status == "budget_exhausted"
                        else "deferred"
                    ),
                    deferred_refs=(
                        ()
                        if social.status == "budget_exhausted"
                        else (
                            social.reason_code
                            or f"social_action.{social.status}",
                        )
                    ),
                    terminal_errors=(
                        (
                            social.reason_code
                            or "social_action.budget_exhausted"
                        ),
                    )
                    if social.status == "budget_exhausted"
                    else (),
                    projection_hint=f"world-revision:{projection.world_revision}",
                )

            if isinstance(proposal, MinimalProposal):
                if self._reply_policy is None or self._reply_recorder is None:
                    return await acceptance_retry_outcome(
                        audit,
                        "minimal_reply_acceptance.unconfigured",
                    )
                account = next(
                    (
                        item
                        for item in projection.budget_accounts
                        if item.account_id == self._reply_policy.account_id
                    ),
                    None,
                )
                if account is None:
                    return await acceptance_retry_outcome(
                        audit,
                        "minimal_reply_acceptance.budget_account_unavailable",
                    )
                try:
                    material = derive_minimal_reply_material(
                        audit=audit,
                        cursor=ProjectionCursor(
                            world_revision=projection.world_revision,
                            deliberation_revision=projection.deliberation_revision,
                            ledger_sequence=projection.ledger_sequence,
                        ),
                        world_id=self._world_id,
                        policy=self._reply_policy,
                        account=account,
                        logical_time=projection.logical_time or observation.logical_time,
                        created_at=observation.created_at,
                        trace_id=observation.trace_id,
                        correlation_id=observation.correlation_id,
                    )
                except MinimalReplyAcceptanceError as exc:
                    return await acceptance_retry_outcome(audit, exc.code)
                committed = await self._commit_visible_acceptance(
                    recorder=self._reply_recorder,
                    acceptance_id=f"acceptance:minimal-reply:{audit.proposal_id}",
                    material=material,
                    actor=self._reply_policy.actor,
                    source="world-runtime:minimal-reply-recovery",
                    trace_id=observation.trace_id,
                )
                action_ids = (material.action.action_id,)
            else:
                if self._expression_policy is None or self._expression_recorder is None:
                    return await acceptance_retry_outcome(
                        audit,
                        "expression_plan_acceptance.unconfigured",
                    )
                account = next(
                    (
                        item
                        for item in projection.budget_accounts
                        if item.account_id == self._expression_policy.account_id
                    ),
                    None,
                )
                if account is None:
                    return await acceptance_retry_outcome(
                        audit,
                        "expression_plan_acceptance.budget_account_unavailable",
                    )
                try:
                    material = derive_expression_plan_material(
                        audit=audit,
                        cursor=ProjectionCursor(
                            world_revision=projection.world_revision,
                            deliberation_revision=projection.deliberation_revision,
                            ledger_sequence=projection.ledger_sequence,
                        ),
                        world_id=self._world_id,
                        policy=self._expression_policy,
                        account=account,
                        logical_time=projection.logical_time or observation.logical_time,
                        created_at=observation.created_at,
                        trace_id=observation.trace_id,
                        correlation_id=observation.correlation_id,
                        payload_store=self._expression_payload_store,
                        source_observation=observation,
                    )
                except ExpressionPlanAcceptanceError as exc:
                    return await acceptance_retry_outcome(audit, exc.code)
                committed = await self._commit_visible_acceptance(
                    recorder=self._expression_recorder,
                    acceptance_id=f"acceptance:expression-plan:{audit.proposal_id}",
                    material=material,
                    actor=self._expression_policy.actor,
                    source="world-runtime:expression-recovery",
                    trace_id=observation.trace_id,
                )
                action_ids = tuple(item.action.action_id for item in material.beats)

            await settle_recovered_inbound_state(audit)

            if not self._expression_episode_tail_is_runtime_owned(
                observation_event.event_id
            ):
                await self._complete_expression_episode(
                    observation=observation,
                    process=episode,
                    outcome_ref="expression-episode:action_authorized",
                )
            projection = await self._project_for_write()
            return RuntimeOutcome(
                outcome_id=f"outcome:{trigger_id}",
                trigger_id=trigger_id,
                observation_ref=observation.observation_id,
                committed_world_revision=projection.world_revision,
                ledger_sequence=projection.ledger_sequence,
                status="action_authorized",
                authorized_action_ids=action_ids,
                projection_hint=f"world-revision:{committed.world_revision}",
            )
        if manifest is None:
            # A reply lane may terminate with only durable deliberation audit
            # evidence (for example, main and quick-recovery both fail
            # validation).  The first ingress reports the cursor after those
            # audit events.  A duplicate must join that same completed work,
            # rather than regress to the earlier Observation commit cursor.
            # This remains read-only recovery: no model or reducer is invoked.
            has_bound_deliberation = any(
                item.trigger_ref == observation_event.event_id
                for item in (
                    *projection.model_result_audits,
                    *projection.proposal_audits,
                )
            )
            has_appraisal_trigger = self._inbound_state_owner is not None and any(
                item.trigger_id
                == interaction_appraisal_trigger_identity(
                    self._world_id, observation.observation_id
                )
                for item in projection.trigger_processes
            )
            if has_bound_deliberation or has_appraisal_trigger:
                # No Proposal means a technical model/validation failure, not
                # a character choice to stay silent.  Keep the claimed
                # lifecycle recoverable; a successful, source-bound `silent`
                # proposal is handled and terminalized above.
                projection = await self._project_for_write()
                technical_retry_pending = has_bound_deliberation
                return RuntimeOutcome(
                    outcome_id=f"outcome:{trigger_id}",
                    trigger_id=trigger_id,
                    observation_ref=observation.observation_id,
                    committed_world_revision=projection.world_revision,
                    ledger_sequence=projection.ledger_sequence,
                    status=(
                        "deferred" if technical_retry_pending else "observed_only"
                    ),
                    deferred_refs=(
                        ("expression_episode.technical_retry_pending",)
                        if technical_retry_pending
                        else ()
                    ),
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
        if (
            not self._expression_episode_tail_is_runtime_owned(
                observation_event.event_id
            )
        ):
            await self._complete_expression_episode(
                observation=observation,
                process=episode,
                outcome_ref="expression-episode:action_authorized",
            )
            projection = await self._project_for_write()
        return RuntimeOutcome(
            outcome_id=f"outcome:{trigger_id}",
            trigger_id=trigger_id,
            observation_ref=observation.observation_id,
            committed_world_revision=projection.world_revision,
            ledger_sequence=projection.ledger_sequence,
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

    def _occurrence_clock_events(
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
        return build_occurrence_clock_events(
            world_id=self._world_id,
            projection=projection,
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
                *self._occurrence_clock_events(before, clock, clock_event=event),
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

    async def record_outcome_observation(self, observation: OutcomeObservation) -> RuntimeOutcome:
        """Record an externally observed result for one active occurrence.

        The host supplies the observation payload and source references only.
        Exact evidence is derived from the pinned ledger projection by the
        runtime, which keeps platform adapters out of the ledger authority lane.
        """

        if observation.world_id != self._world_id:
            raise ValueError("outcome observation belongs to another world")
        trigger_id = f"trigger:outcome-observation:{observation.observation_id}"
        event_id = f"event:outcome-observation:{observation.observation_id}"
        async with self._lock:
            existing = await self._lookup_event_commit(event_id)
            if existing is not None:
                persisted, commit = existing
                if not _matches_outcome_observation_command(persisted, observation):
                    raise IdempotencyConflict(
                        "outcome observation identity was already committed with different content"
                    )
                return await self._ensure_outcome_deliberation_trigger(
                    observation=observation,
                    source_event=persisted,
                    original_commit=commit,
                    runtime_trigger_id=trigger_id,
                )
            before = await self._project_for_write()
            event = build_outcome_observation_event(
                world_id=self._world_id,
                projection=before,
                observation=observation,
            )
            try:
                committed = await self._commit(
                    [event],
                    world_revision=before.world_revision,
                    deliberation_revision=before.deliberation_revision,
                )
            except IdempotencyConflict:
                raced = await self._lookup_event_commit(event.event_id)
                if raced is None:
                    raise
                persisted, commit = raced
                if persisted != event:
                    raise
                return await self._ensure_outcome_deliberation_trigger(
                    observation=observation,
                    source_event=persisted,
                    original_commit=commit,
                    runtime_trigger_id=trigger_id,
                )
        return await self._ensure_outcome_deliberation_trigger(
            observation=observation,
            source_event=event,
            original_commit=committed,
            runtime_trigger_id=trigger_id,
        )

    async def _ensure_outcome_deliberation_trigger(
        self,
        *,
        observation: OutcomeObservation,
        source_event: WorldEvent,
        original_commit: CommitResult,
        runtime_trigger_id: str,
    ) -> RuntimeOutcome:
        """Open the background-work opportunity only after its source is durable."""

        for _attempt in range(3):
            projection = await self._project_for_write()
            recorded = next(
                (
                    item
                    for item in projection.outcome_observations
                    if item.observation_id == observation.observation_id
                ),
                None,
            )
            if recorded is None:
                raise RuntimeError("committed outcome observation is absent from the projection")
            trigger_id = outcome_deliberation_trigger_id(
                world_id=self._world_id,
                occurrence_id=recorded.occurrence_id,
                observation_id=recorded.observation_id,
            )
            if any(item.trigger_id == trigger_id for item in projection.trigger_processes):
                existing_trigger = await self._lookup_event_commit(
                    "event:outcome-deliberation-trigger-opened:"
                    + trigger_id.removeprefix("trigger:")
                )
                return self._runtime_outcome_for_commit(
                    trigger_id=runtime_trigger_id,
                    committed=(
                        existing_trigger[1] if existing_trigger is not None else original_commit
                    ),
                )
            trigger_event = outcome_deliberation_trigger_event(
                world_id=self._world_id,
                source_event=source_event,
                observation=recorded,
            )
            try:
                committed = await self._commit(
                    [trigger_event],
                    world_revision=projection.world_revision,
                    deliberation_revision=projection.deliberation_revision,
                )
            except (ConcurrencyConflict, IdempotencyConflict):
                existing = await self._lookup_event_commit(trigger_event.event_id)
                if existing is None or existing[0] != trigger_event:
                    continue
                committed = existing[1]
            return self._runtime_outcome_for_commit(
                trigger_id=runtime_trigger_id, committed=committed
            )
        raise ConcurrencyConflict("outcome deliberation trigger recovery did not converge")

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
                    if (
                        all(
                            stored_event == expected
                            for (stored_event, _commit), expected in zip(
                                persisted, events, strict=True
                            )
                        )
                        and len({commit for _event, commit in persisted}) == 1
                    ):
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
    def _runtime_outcome_for_commit(*, trigger_id: str, committed: CommitResult) -> RuntimeOutcome:
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
            completed_process = next(
                (
                    candidate
                    for candidate in after_inbox.trigger_processes
                    if candidate.trigger_id == trigger_id and candidate.state == "terminal"
                ),
                None,
            )
            if completed_process is not None:
                prior_reconciliation = next(
                    (
                        candidate
                        for candidate in after_inbox.reconciliations
                        if candidate.result_id == result.result_id
                    ),
                    None,
                )
                prior_receipt = next(
                    (
                        candidate
                        for candidate in after_inbox.execution_receipts
                        if candidate.result_id == result.result_id
                    ),
                    None,
                )
                if prior_reconciliation is not None:
                    runtime_status = "deferred"
                    deferred_ref = prior_reconciliation.reconciliation_id
                    projection_hint = deferred_ref
                elif (
                    prior_receipt is not None
                    and prior_receipt.action_id == result.action_id
                    and prior_receipt.provider == result.source
                    and prior_receipt.provider_ref == result.provider_ref
                    and prior_receipt.source_event_id == result.source_event_id
                    and prior_receipt.raw_payload_hash == result.raw_payload_hash
                    and prior_receipt.observed_state == result.status
                ):
                    runtime_status = "action_executed"
                    deferred_ref = None
                    projection_hint = f"action:{result.action_id}:{result.status}"
                else:
                    raise IdempotencyConflict(
                        f"completed settlement {trigger_id!r} has no equivalent terminal result"
                    )
                return RuntimeOutcome(
                    outcome_id=f"outcome:{trigger_id}",
                    trigger_id=trigger_id,
                    observation_ref=result.result_id,
                    committed_world_revision=after_inbox.world_revision,
                    ledger_sequence=after_inbox.ledger_sequence,
                    status=runtime_status,
                    deferred_refs=(deferred_ref,) if deferred_ref else (),
                    projection_hint=projection_hint,
                )
            plan = self._settlement.plan(
                result,
                trigger_id=trigger_id,
                projection=after_inbox,
            )
            await self._commit(
                list(plan.events),
                world_revision=after_inbox.world_revision,
                deliberation_revision=after_inbox.deliberation_revision,
                commit_id=f"commit:{trigger_id}:settlement",
            )
            await self._settle_expression_episode_for_action(result)
            committed_projection = await self._project_for_write()
        return RuntimeOutcome(
            outcome_id=f"outcome:{trigger_id}",
            trigger_id=trigger_id,
            observation_ref=result.result_id,
            committed_world_revision=committed_projection.world_revision,
            ledger_sequence=committed_projection.ledger_sequence,
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

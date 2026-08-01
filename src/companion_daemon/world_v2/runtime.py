from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from uuid import uuid4
from datetime import UTC, datetime

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
    expression_episode_cancel_events,
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
from .appraisal_trigger import interaction_appraisal_trigger_events
from .fact_trigger import interaction_fact_trigger_event
from .fact_draft_adapter import FactObservationProposalAdapter
from .fact_memory_candidate_lifecycle import FactMemoryCandidateLifecycle
from .fact_memory_draft import FactMemoryDraftAdapter
from .fact_v2_acceptance_runtime import FactV2AcceptanceRuntime
from .interaction_fact_trigger_runtime import FactTriggerRunResult, InteractionFactTriggerRuntime
from .private_impression_producer import (
    PrivateImpressionDraftAdapter,
    PrivateImpressionRunResult,
    PrivateImpressionTriggerOpener,
    PrivateImpressionTriggerRuntime,
)
from .batch_invariants import interaction_appraisal_trigger_identity
from .appraisal_acceptance_runtime import (
    AppraisalAcceptanceError,
    AppraisalAcceptanceRuntime,
)
from .appraisal_proposal_worker import AppraisalProposalWorker
from .immediate_emotion_proposal_worker import ImmediateEmotionProposalWorker
from .affect_trigger import affect_deliberation_trigger_events
from .affect_acceptance_runtime import AffectAcceptanceError, AffectAcceptanceRuntime
from .affect_deliberation_worker import AffectDeliberationWorker
from .affect_trigger_runtime import AffectTriggerRunResult, AffectTriggerRuntime
from .relationship_deliberation_worker import RelationshipDeliberationWorker
from .relationship_trigger_runtime import RelationshipTriggerRuntime
from .relationship_adjustment_worker import RelationshipAdjustmentWorker
from .relationship_adjustment_trigger_runtime import RelationshipAdjustmentTriggerRuntime
from .interaction_appraisal_trigger_runtime import (
    AppraisalTriggerRunResult,
    InteractionAppraisalTriggerRuntime,
)
from .npc_world_appraisal_trigger_runtime import NpcWorldAppraisalTriggerRuntime
from .plan_disruption_appraisal_trigger import PlanDisruptionAppraisalTriggerOpener
from .plan_disruption_appraisal_trigger_runtime import (
    PlanDisruptionAppraisalTriggerRuntime,
    PlanDisruptionAppraisalTurn,
)
from .silence_appraisal_trigger import SilenceAppraisalTriggerOpener
from .silence_appraisal_trigger_runtime import SilenceAppraisalTriggerRuntime, SilenceAppraisalTurn
from .outcome_deliberation_turn import OutcomeDeliberationTurn
from .outcome_proposal_worker import OutcomeProposalWorker
from .outcome_trigger_runtime import OutcomeTriggerRunResult, OutcomeTriggerRuntime
from .outcome_trigger import outcome_deliberation_trigger_event, outcome_deliberation_trigger_id
from .interaction_bid_deliberation_turn import InteractionBidDeliberationTurn
from .interaction_bid_proposal_worker import InteractionBidProposalWorker
from .interaction_bid_trigger_runtime import (
    InteractionBidTriggerRunResult,
    InteractionBidTriggerRuntime,
)
from .settled_world_appraisal_turn import SettledWorldAppraisalTurn
from .action_pump import (
    ActionExecutor,
    ActionPump,
    ActionPumpResult,
    ProviderAcceptedReconciliationGate,
)
from .bounded_decision_vertical import AnchoredRunResult, InlineOnceRunResult
from .expression_reconsideration import expression_reconsideration_events_for_observation
from .expression_reconsideration_runtime import (
    ExpressionReconsiderationReviewer,
    ExpressionReconsiderationRunResult,
    ExpressionReconsiderationRuntime,
)
from .random_authority import RandomAuthority
from .external_result_trigger_runtime import (
    ExternalResultTriggerRunResult,
    ExternalResultTriggerRuntime,
    ToolResultDeliberator,
)
from .read_only_tool_trigger import read_only_tool_trigger_event
from .read_only_tool_trigger_runtime import (
    ReadOnlyToolTriggerRunResult,
    ReadOnlyToolTriggerRuntime,
)
from .perception_result_trigger_runtime import (
    PerceptionResultDeliberator,
    PerceptionResultTriggerRunResult,
    PerceptionResultTriggerRuntime,
)
from .perception_trigger import perception_trigger_event
from .perception_trigger_runtime import PerceptionTriggerRunResult, PerceptionTriggerRuntime
from .social_action_worker import SocialActionRunResult, SocialActionWorker
from .quick_reaction import (
    QUICK_REACTION_PROPOSAL_PREFIX,
    QuickReactionRunResult,
    QuickReactionWorker,
)
from .quick_reaction_vertical import QuickReactionVerticalWorker
from .proactive_action import ProactiveActionRunResult, ProactiveActionRuntime
from .memory_withdrawal_review import (
    MemoryWithdrawalReviewRunResult,
    MemoryWithdrawalReviewRuntime,
)
from .proposal_audit import ProposalAuditCommit
from .proposal_envelope import DecisionProposal, MinimalProposal, validate_proposal_envelope
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
    TriggerProcess,
    WorldEvent,
    WorldProjection,
)


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
        interaction_appraisal_owner: str | None = None,
        appraisal_acceptance: AppraisalAcceptanceRuntime | None = None,
        appraisal_acceptance_actor: str | None = None,
        appraisal_worker: AppraisalProposalWorker | None = None,
        interaction_appraisal_turn: PinnedTurnCompiler | None = None,
        immediate_emotion_worker: ImmediateEmotionProposalWorker | None = None,
        npc_world_appraisal_turn: SettledWorldAppraisalTurn | None = None,
        silence_appraisal_turn: SilenceAppraisalTurn | None = None,
        silence_appraisal_idle_seconds: int | None = None,
        plan_disruption_appraisal_turn: PlanDisruptionAppraisalTurn | None = None,
        plan_disruption_appraisal_enabled: bool = True,
        outcome_deliberation_turn: OutcomeDeliberationTurn | None = None,
        outcome_worker: OutcomeProposalWorker | None = None,
        outcome_deliberation_owner: str | None = None,
        interaction_bid_turn: InteractionBidDeliberationTurn | None = None,
        interaction_bid_worker: InteractionBidProposalWorker | None = None,
        interaction_bid_owner: str | None = None,
        interaction_fact_owner: str | None = None,
        fact_acceptance: FactV2AcceptanceRuntime | None = None,
        fact_adapter: FactObservationProposalAdapter | None = None,
        fact_memory_adapter: FactMemoryDraftAdapter | None = None,
        fact_memory_lifecycle: FactMemoryCandidateLifecycle | None = None,
        private_impression_owner: str | None = None,
        private_impression_adapter: PrivateImpressionDraftAdapter | None = None,
        affect_deliberation_owner: str | None = None,
        affect_worker: AffectDeliberationWorker | None = None,
        relationship_deliberation_owner: str | None = None,
        relationship_worker: RelationshipDeliberationWorker | None = None,
        relationship_adjustment_owner: str | None = None,
        relationship_adjustment_worker: RelationshipAdjustmentWorker | None = None,
        action_executor: ActionExecutor | None = None,
        action_pump_owner: str | None = None,
        action_pump_excluded_kinds: frozenset[str] = frozenset(),
        affect_acceptance: AffectAcceptanceRuntime | None = None,
        affect_acceptance_actor: str | None = None,
        expression_reconsideration_owner: str | None = None,
        expression_reconsideration_reviewer: ExpressionReconsiderationReviewer | None = None,
        social_action_worker: SocialActionWorker | None = None,
        quick_reaction_worker: QuickReactionWorker | QuickReactionVerticalWorker | None = None,
        proactive_action_runtime: ProactiveActionRuntime | None = None,
        memory_withdrawal_review: MemoryWithdrawalReviewRuntime | None = None,
        external_result_owner: str | None = None,
        external_result_deliberator: ToolResultDeliberator | None = None,
        read_only_tool_owner: str | None = None,
        read_only_tool_trigger_runtime: ReadOnlyToolTriggerRuntime | None = None,
        perception_owner: str | None = None,
        perception_trigger_runtime: PerceptionTriggerRuntime | None = None,
        perception_result_owner: str | None = None,
        perception_result_deliberator: PerceptionResultDeliberator | None = None,
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
        if interaction_appraisal_turn is not None and appraisal_worker is None:
            raise ValueError("interaction appraisal turn requires an appraisal worker")
        self._interaction_appraisal_turn = interaction_appraisal_turn
        if immediate_emotion_worker is not None:
            if interaction_appraisal_turn is None or appraisal_worker is None:
                raise ValueError("immediate emotion worker requires the interaction appraisal lane")
            if immediate_emotion_worker.ledger is not self._ledger:
                raise ValueError("immediate emotion worker must own this exact ledger")
        self._immediate_emotion_worker = immediate_emotion_worker
        if npc_world_appraisal_turn is not None and appraisal_worker is None:
            raise ValueError("NPC world appraisal turn requires an appraisal worker")
        if npc_world_appraisal_turn is not None and interaction_appraisal_owner is None:
            raise ValueError("NPC world appraisal turn requires an appraisal worker owner")
        self._npc_world_appraisal_turn = npc_world_appraisal_turn
        if silence_appraisal_turn is not None and appraisal_worker is None:
            raise ValueError("silence appraisal turn requires an appraisal worker")
        if silence_appraisal_turn is not None and interaction_appraisal_owner is None:
            raise ValueError("silence appraisal turn requires an appraisal worker owner")
        if silence_appraisal_idle_seconds is not None and silence_appraisal_idle_seconds < 0:
            raise ValueError("silence appraisal idle threshold must not be negative")
        self._silence_appraisal_turn = silence_appraisal_turn
        # ``0``/``None`` disables opening new silence triggers; already-open
        # triggers still drain so a config change never strands durable work.
        self._silence_appraisal_idle_seconds = (
            silence_appraisal_idle_seconds if silence_appraisal_idle_seconds else None
        )
        if plan_disruption_appraisal_turn is not None and appraisal_worker is None:
            raise ValueError("plan disruption appraisal turn requires an appraisal worker")
        if plan_disruption_appraisal_turn is not None and interaction_appraisal_owner is None:
            raise ValueError("plan disruption appraisal turn requires an appraisal worker owner")
        self._plan_disruption_appraisal_turn = plan_disruption_appraisal_turn
        # Disabling stops opening new disruption triggers; already-open
        # triggers still drain so a config change never strands durable work.
        self._plan_disruption_appraisal_enabled = bool(plan_disruption_appraisal_enabled)
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
        if interaction_bid_owner is not None and not interaction_bid_owner:
            raise ValueError("interaction bid owner must not be empty")
        if interaction_bid_worker is not None and interaction_bid_worker.ledger is not self._ledger:
            raise ValueError("interaction bid worker must own this exact ledger")
        if (interaction_bid_turn is None) != (interaction_bid_worker is None):
            raise ValueError("interaction bid turn and worker must be configured together")
        if interaction_bid_worker is not None and interaction_bid_owner is None:
            raise ValueError("interaction bid worker requires an interaction bid owner")
        self._interaction_bid_turn = interaction_bid_turn
        self._interaction_bid_worker = interaction_bid_worker
        self._interaction_bid_owner = interaction_bid_owner
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
        if (fact_memory_adapter is None) != (fact_memory_lifecycle is None):
            raise ValueError("Fact memory adapter and lifecycle must be configured together")
        self._fact_memory_adapter = fact_memory_adapter
        self._fact_memory_lifecycle = fact_memory_lifecycle
        if (private_impression_adapter is None) != (private_impression_owner is None):
            raise ValueError(
                "private impression adapter and worker owner must be configured together"
            )
        if private_impression_owner is not None and not private_impression_owner:
            raise ValueError("private impression owner must not be empty")
        self._private_impression_owner = private_impression_owner
        self._private_impression_adapter = private_impression_adapter
        self._private_impression_runtime = (
            PrivateImpressionTriggerRuntime(
                ledger=self._ledger,
                adapter=private_impression_adapter,
                owner_id=private_impression_owner,
            )
            if private_impression_adapter is not None
            and private_impression_owner is not None
            else None
        )
        if affect_deliberation_owner is not None and not affect_deliberation_owner:
            raise ValueError("affect deliberation owner must not be empty")
        self._affect_deliberation_owner = affect_deliberation_owner
        if affect_worker is not None and affect_worker.ledger is not self._ledger:
            raise ValueError("affect worker must own this exact ledger")
        if affect_worker is not None and affect_deliberation_owner is None:
            raise ValueError("affect worker requires affect deliberation triggers")
        self._affect_worker = affect_worker
        if relationship_deliberation_owner is not None and not relationship_deliberation_owner:
            raise ValueError("relationship deliberation owner must not be empty")
        self._relationship_deliberation_owner = relationship_deliberation_owner
        if relationship_worker is not None and relationship_worker.ledger is not self._ledger:
            raise ValueError("relationship worker must own this exact ledger")
        if relationship_worker is not None and relationship_deliberation_owner is None:
            raise ValueError("relationship worker requires relationship deliberation triggers")
        self._relationship_worker = relationship_worker
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
        if expression_reconsideration_owner is not None and not expression_reconsideration_owner:
            raise ValueError("expression reconsideration owner must not be empty")
        if (
            expression_reconsideration_reviewer is not None
            and expression_reconsideration_owner is None
        ):
            raise ValueError("expression reconsideration reviewer requires a worker owner")
        self._expression_reconsideration_owner = expression_reconsideration_owner
        self._expression_reconsideration_reviewer = expression_reconsideration_reviewer
        if social_action_worker is not None and social_action_worker.ledger is not self._ledger:
            raise ValueError("social action worker must own this exact ledger")
        self._social_action_worker = social_action_worker
        if quick_reaction_worker is not None and quick_reaction_worker.ledger is not self._ledger:
            raise ValueError("quick reaction worker must own this exact ledger")
        self._quick_reaction_worker = quick_reaction_worker
        if (
            proactive_action_runtime is not None
            and proactive_action_runtime.ledger is not self._ledger
        ):
            raise ValueError("proactive action runtime must own this exact ledger")
        self._proactive_action_runtime = proactive_action_runtime
        if (
            memory_withdrawal_review is not None
            and memory_withdrawal_review.ledger is not self._ledger
        ):
            raise ValueError("memory withdrawal review must own this exact ledger")
        self._memory_withdrawal_review = memory_withdrawal_review
        if (external_result_owner is None) != (external_result_deliberator is None):
            raise ValueError("external result owner and deliberator must be configured together")
        self._external_result_owner = external_result_owner
        self._external_result_deliberator = external_result_deliberator
        if (read_only_tool_owner is None) != (read_only_tool_trigger_runtime is None):
            raise ValueError("read-only tool owner and trigger runtime must be configured together")
        if (
            read_only_tool_trigger_runtime is not None
            and read_only_tool_trigger_runtime.ledger is not self._ledger
        ):
            raise ValueError("read-only tool trigger runtime must own this exact ledger")
        self._read_only_tool_owner = read_only_tool_owner
        self._read_only_tool_trigger_runtime = read_only_tool_trigger_runtime
        if (perception_owner is None) != (perception_trigger_runtime is None):
            raise ValueError("perception owner and trigger runtime must be configured together")
        if (
            perception_trigger_runtime is not None
            and perception_trigger_runtime.ledger is not self._ledger
        ):
            raise ValueError("perception trigger runtime must own this exact ledger")
        self._perception_owner = perception_owner
        self._perception_trigger_runtime = perception_trigger_runtime
        if (perception_result_owner is None) != (perception_result_deliberator is None):
            raise ValueError("perception result owner and deliberator must be configured together")
        self._perception_result_owner = perception_result_owner
        self._perception_result_deliberator = perception_result_deliberator
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
        skip_advisories: bool = False,
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
                        skip_advisories=skip_advisories,
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
        AppraisalTriggerRunResult
        | OutcomeTriggerRunResult
        | InteractionBidTriggerRunResult
        | AffectTriggerRunResult
        | FactTriggerRunResult
        | PrivateImpressionRunResult
        | ExpressionReconsiderationRunResult
        | ExternalResultTriggerRunResult
        | ReadOnlyToolTriggerRunResult
        | PerceptionTriggerRunResult
        | PerceptionResultTriggerRunResult
        | SocialActionRunResult
        | MemoryWithdrawalReviewRunResult
        | ProactiveActionRunResult
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
            if self._perception_result_owner is not None:
                assert self._perception_result_deliberator is not None
                perception_result = await PerceptionResultTriggerRuntime(
                    ledger=self._ledger,
                    deliberator=self._perception_result_deliberator,
                    owner_id=self._perception_result_owner,
                ).drain_one()
                if perception_result.status != "idle":
                    return perception_result
            if self._perception_trigger_runtime is not None:
                perception = await self._perception_trigger_runtime.drain_one()
                if perception.status != "idle":
                    return perception
            if self._read_only_tool_trigger_runtime is not None:
                tool = await self._read_only_tool_trigger_runtime.drain_one()
                if tool.status != "idle":
                    return tool
            if self._external_result_owner is not None:
                assert self._external_result_deliberator is not None
                external_result = await ExternalResultTriggerRuntime(
                    ledger=self._ledger,
                    deliberator=self._external_result_deliberator,
                    owner_id=self._external_result_owner,
                ).drain_one()
                if external_result.status != "idle":
                    return external_result
            if self._expression_reconsideration_owner is not None:
                reconsideration = await ExpressionReconsiderationRuntime(
                    ledger=self._ledger,
                    owner_id=self._expression_reconsideration_owner,
                    reviewer=self._expression_reconsideration_reviewer,
                ).drain_one()
                if reconsideration.status != "idle":
                    return reconsideration
            # Initiative is time-sensitive: an eligible silence or explicit
            # response gap should not sit behind an arbitrarily large backlog
            # of per-observation semantic jobs.  The compiler only exposes an
            # evidence-bound opportunity; the model still owns now/later/
            # silent.  Before its opening window this check is idle and costs
            # no authority, so ordinary appraisal/fact work keeps its order.
            if self._proactive_action_runtime is not None:
                proactive = await self._proactive_action_runtime.drain_one()
                if proactive.status != "idle":
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
            if self._interaction_bid_turn is not None:
                assert self._interaction_bid_worker is not None
                assert self._interaction_bid_owner is not None
                interaction_bid = await InteractionBidTriggerRuntime(
                    ledger=self._ledger,
                    turn=self._interaction_bid_turn,
                    worker=self._interaction_bid_worker,
                    owner_id=self._interaction_bid_owner,
                ).drain_one()
                if interaction_bid.status != "idle":
                    return interaction_bid
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
                    memory_adapter=self._fact_memory_adapter,
                    memory_lifecycle=self._fact_memory_lifecycle,
                    owner_id=self._interaction_fact_owner,
                ).drain_one()
                if fact.status not in {"idle", "owned_elsewhere"}:
                    return fact
            appraisal_result: AppraisalTriggerRunResult | None = None
            if self._npc_world_appraisal_turn is not None:
                assert self._appraisal_worker is not None
                assert self._interaction_appraisal_owner is not None
                appraisal = await NpcWorldAppraisalTriggerRuntime(
                    ledger=self._ledger,
                    turn=self._npc_world_appraisal_turn,
                    worker=self._appraisal_worker,
                    owner_id=self._interaction_appraisal_owner,
                    affect_owner_id=self._affect_deliberation_owner,
                    relationship_owner_id=self._relationship_deliberation_owner,
                ).drain_one()
                if appraisal.status not in {"idle", "owned_elsewhere"}:
                    return appraisal
                appraisal_result = appraisal
            if self._interaction_appraisal_turn is not None:
                assert self._appraisal_worker is not None
                assert self._interaction_appraisal_owner is not None
                appraisal = await InteractionAppraisalTriggerRuntime(
                    ledger=self._ledger,
                    pinned_turn=self._interaction_appraisal_turn,
                    worker=self._appraisal_worker,
                    owner_id=self._interaction_appraisal_owner,
                    affect_owner_id=self._affect_deliberation_owner,
                    relationship_owner_id=self._relationship_deliberation_owner,
                    immediate_emotion_worker=self._immediate_emotion_worker,
                ).drain_one()
                if appraisal.status not in {"idle", "owned_elsewhere"}:
                    return appraisal
                appraisal_result = appraisal
            if self._silence_appraisal_turn is not None:
                assert self._appraisal_worker is not None
                assert self._interaction_appraisal_owner is not None
                # The opener is a cheap deterministic check; running it on
                # every background pass keeps the per-silence trigger current
                # without a dedicated scheduler, while its identity keeps
                # repeated passes idempotent.
                if self._silence_appraisal_idle_seconds is not None:
                    try:
                        await SilenceAppraisalTriggerOpener(
                            ledger=self._ledger,
                            owner_id=self._interaction_appraisal_owner,
                            idle_seconds_threshold=self._silence_appraisal_idle_seconds,
                        ).open_once()
                    except (ConcurrencyConflict, IdempotencyConflict):
                        # A concurrent ingress won the cursor between the
                        # opener's read and its commit.  The next background
                        # pass re-derives the same deterministic opportunity,
                        # so losing this race must not fail the whole pass.
                        pass
                appraisal = await SilenceAppraisalTriggerRuntime(
                    ledger=self._ledger,
                    turn=self._silence_appraisal_turn,
                    worker=self._appraisal_worker,
                    owner_id=self._interaction_appraisal_owner,
                    affect_owner_id=self._affect_deliberation_owner,
                    relationship_owner_id=self._relationship_deliberation_owner,
                ).drain_one()
                if appraisal.status not in {"idle", "owned_elsewhere"}:
                    return appraisal
                appraisal_result = appraisal
            if self._plan_disruption_appraisal_turn is not None:
                assert self._appraisal_worker is not None
                assert self._interaction_appraisal_owner is not None
                # Like the silence lane: the opener is a cheap deterministic
                # projection check on every background pass, and its per-
                # abandonment identity keeps repeated passes idempotent.
                if self._plan_disruption_appraisal_enabled:
                    try:
                        await PlanDisruptionAppraisalTriggerOpener(
                            ledger=self._ledger,
                            owner_id=self._interaction_appraisal_owner,
                        ).open_once()
                    except (ConcurrencyConflict, IdempotencyConflict):
                        # A concurrent ingress won the cursor between the
                        # opener's read and its commit.  The next background
                        # pass re-derives the same deterministic opportunity,
                        # so losing this race must not fail the whole pass.
                        pass
                appraisal = await PlanDisruptionAppraisalTriggerRuntime(
                    ledger=self._ledger,
                    turn=self._plan_disruption_appraisal_turn,
                    worker=self._appraisal_worker,
                    owner_id=self._interaction_appraisal_owner,
                    affect_owner_id=self._affect_deliberation_owner,
                    relationship_owner_id=self._relationship_deliberation_owner,
                ).drain_one()
                if appraisal.status not in {"idle", "owned_elsewhere"}:
                    return appraisal
                appraisal_result = appraisal
            if self._relationship_worker is not None:
                assert self._relationship_deliberation_owner is not None
                relationship = await RelationshipTriggerRuntime(
                    ledger=self._ledger,
                    worker=self._relationship_worker,
                    owner_id=self._relationship_deliberation_owner,
                ).drain_one()
                if relationship.status not in {"idle", "owned_elsewhere"}:
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
            affect_result = None
            if self._affect_worker is not None:
                assert self._affect_deliberation_owner is not None
                affect_result = await AffectTriggerRuntime(
                    ledger=self._ledger,
                    worker=self._affect_worker,
                    owner_id=self._affect_deliberation_owner,
                ).drain_one()
                if affect_result.status not in {"idle", "owned_elsewhere"}:
                    return affect_result
            # Private impressions consolidate already-accepted appraisals into
            # her internal-only reading of the user/relationship.  The opener
            # is a cheap deterministic projection check; the identity of each
            # per-appraisal trigger keeps repeated passes idempotent.
            if self._private_impression_adapter is not None:
                assert self._private_impression_owner is not None
                try:
                    await PrivateImpressionTriggerOpener(
                        ledger=self._ledger,
                        owner_id=self._private_impression_owner,
                    ).open_once()
                except (ConcurrencyConflict, IdempotencyConflict):
                    # A concurrent ingress won the cursor between the opener's
                    # read and its commit; the next pass re-derives the same
                    # deterministic opportunity.
                    pass
                assert self._private_impression_runtime is not None
                impression = await self._private_impression_runtime.drain_one()
                if impression.status not in {"idle", "owned_elsewhere"}:
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
            return affect_result or appraisal_result

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
        if self._interaction_appraisal_turn is not None and any(
            item.process_kind == "interaction_appraisal"
            and item.source_evidence_ref == observation.observation_id
            and item.state == "claimed"
            and item.claim_lease is not None
            and item.claim_lease.owner_id != self._interaction_appraisal_owner
            for item in projection.trigger_processes
        ):
            # If the same-turn emotion lane was selected but temporarily owned
            # elsewhere, its accepted/no-change result must land before reply
            # cognition is pinned.  Leaving the expression lifecycle active
            # lets the ordinary appraisal lane finish it later in this
            # background pass without losing the user's message.
            return None
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

    async def _resolve_expression_episode_before_dispatch(
        self,
        *,
        observation: Observation,
        observation_event: WorldEvent,
        process: TriggerProcess | None,
    ) -> str | None:
        if (
            process is None
            or self._pinned_turn is None
            or self._pinned_turn.expression_episode_mode != "on"
        ):
            return None
        tail = await self._pinned_turn.await_expression_episode_tail(
            observation_event.event_id
        )
        if tail is None:
            # A restart loses the in-memory full-tail task, but the winning
            # model Proposal already carries its model-authored disposition.
            # Recover that exact choice so cancel/supersede cannot be bypassed
            # merely because Action Acceptance committed just before a crash.
            projection = await self._project_for_write()
            decided = {
                item.proposal_id for item in projection.acceptance_decisions
            }
            durable_disposition = None
            for audit in reversed(projection.proposal_audits):
                if (
                    audit.trigger_ref != observation_event.event_id
                    or audit.proposal_id in decided
                    or not audit.proposal_id.startswith(
                        ("proposal:expression:", "proposal:chat-reply:")
                    )
                    or (
                        process.claim_lease is not None
                        and audit.attempt_id not in process.attempt_ids
                    )
                ):
                    continue
                try:
                    proposal = validate_proposal_envelope(
                        json.loads(audit.proposal_json)
                    )
                except (TypeError, ValueError):
                    continue
                if isinstance(proposal, DecisionProposal):
                    durable_disposition = proposal.episode_disposition
                    break
            if durable_disposition is None:
                return None
            disposition = durable_disposition
        else:
            disposition = tail.disposition
        if disposition == "append":
            return disposition
        if disposition in {"cancel_pending", "supersede_pending"}:
            projection = await self._project_for_write()
            events = expression_episode_cancel_events(
                world_id=self._world_id,
                projection=projection,
                process=process,
                observation=observation,
                observation_event_ref=observation_event.event_id,
                superseded=disposition == "supersede_pending",
            )
            if events:
                await self._commit(
                    list(events),
                    world_revision=projection.world_revision,
                    deliberation_revision=projection.deliberation_revision,
                    commit_id=f"commit:{process.trigger_id}:{disposition}",
                )
        await self._complete_expression_episode(
            observation=observation,
            process=process,
            outcome_ref=f"expression-episode:{disposition}",
        )
        return disposition

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
    ) -> tuple[Observation, WorldEvent, CommitResult, TriggerProcess | None, bool]:
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
                    # A locked-head clock rebase is the one permitted
                    # difference between a retry's client envelope and the
                    # committed observation. All other content remains a
                    # genuine idempotency conflict.
                    normalized_observation = observation.model_copy(
                        update={"logical_time": persisted_observation.logical_time}
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
                for process in before.trigger_processes
                if process.process_kind == "expression_episode"
                and process.state == "claimed"
                and process.claim_lease is not None
                and not expression_episode_has_authorized_action(before, process)
            )
            ingress_events = [
                event,
                *superseded_expression_events,
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
            if self._interaction_appraisal_owner is not None:
                ingress_events.extend(
                    interaction_appraisal_trigger_events(
                        observation=observation,
                        observation_event=event,
                        owner_id=self._interaction_appraisal_owner,
                    )
                )
            if self._interaction_fact_owner is not None:
                ingress_events.append(
                    interaction_fact_trigger_event(
                        observation=observation,
                        observation_event=event,
                    )
                )
            if self._read_only_tool_owner is not None:
                ingress_events.append(
                    read_only_tool_trigger_event(
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
                return observation, event, committed, episode_process, False

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
        ) = await self._commit_ingress_observation(
            observation=observation,
            event=event,
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
            # Fast tail of the human response distribution: while the main
            # deliberation below is still being prepared, a bounded worker may
            # place one QQ reaction on the message that just committed. It is
            # awaited *before* any reply cursor is pinned: its world-revision
            # writes must land before reply deliberation evaluates the world,
            # otherwise the reply Acceptance would become legitimately stale.
            quick_reaction_task: (
                asyncio.Task[QuickReactionRunResult]
                | asyncio.Task[InlineOnceRunResult]
                | None
            ) = None
            if self._quick_reaction_worker is not None:
                quick_reaction_task = asyncio.create_task(
                    self._quick_reaction_worker.run_observation(
                        observation=observation,
                        observation_event=event,
                        source_world_revision=committed.world_revision,
                    )
                )
            quick_reaction: QuickReactionRunResult | InlineOnceRunResult | None = None
            if quick_reaction_task is not None:
                # The worker owns hard internal budgets and never raises.
                quick_reaction = await quick_reaction_task
                _LOG.warning(
                    "world v2 ingest phase trace=%s phase=quick_reaction_ms value=%.1f "
                    "status=%s reaction=%s user_perceived_quick_reaction_ms=%s",
                    observation.trace_id,
                    quick_reaction.total_ms or 0.0,
                    quick_reaction.status,
                    quick_reaction.reaction_id,
                    _user_perceived_ms(observation),
                )
            if quick_reaction is not None and quick_reaction.ledger_advanced:
                # The quick lane committed after the ingress batch; the reply
                # must be pinned at the true head, not the stale ingress cursor.
                head = await self._project_for_write()
                reply_cursor = ProjectionCursor(
                    world_revision=head.world_revision,
                    deliberation_revision=head.deliberation_revision,
                    ledger_sequence=head.ledger_sequence,
                )
            else:
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
            # Compatibility for existing composition roots that provide only
            # the old inline worker. New production composition provides a
            # dedicated interaction turn, whose durable trigger is drained
            # outside this latency-critical lock.
            if (
                self._appraisal_worker is not None
                and self._interaction_appraisal_turn is None
                and audited is not None
                and audited.proposal_id
            ):
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
                        cursor = audited.cursor
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
                                    if (located := self._ledger.lookup_event_commit(event_id))
                                    is not None
                                    and located[0].event_type == "AppraisalAccepted"
                                ),
                                None,
                            )
                            if appraisal_event is None:
                                raise RuntimeError(
                                    "accepted appraisal has no durable mutation event"
                                )
                            trigger_head = await self._project_for_write()
                            committed = await self._commit(
                                list(
                                    affect_deliberation_trigger_events(
                                        appraisal_event=appraisal_event,
                                        owner_id=self._affect_deliberation_owner,
                                        claimed_at=trigger_head.logical_time,
                                    )
                                ),
                                world_revision=trigger_head.world_revision,
                                deliberation_revision=trigger_head.deliberation_revision,
                            )
                    except (AppraisalAcceptanceError, ConcurrencyConflict, ValueError) as exc:
                        code = getattr(exc, "code", "appraisal.worker_failed")
                        reply_deferred_refs = (*reply_deferred_refs, str(code))
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
        pre_dispatch_disposition = None
        if reply_authorized:
            pre_dispatch_disposition = (
                await self._resolve_expression_episode_before_dispatch(
                    observation=observation,
                    observation_event=event,
                    process=episode_process,
                )
            )
            if pre_dispatch_disposition in {
                "cancel_pending",
                "supersede_pending",
            }:
                reply_authorized = False
                authorized_action_ids = ()
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
            self._pinned_turn is not None
            and self._pinned_turn.expression_episode_mode == "on"
            and reply_authorized
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
                    or candidate.proposal_id.startswith(QUICK_REACTION_PROPOSAL_PREFIX)
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
                    and not audit.proposal_id.startswith(QUICK_REACTION_PROPOSAL_PREFIX)
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
                    and not audit.proposal_id.startswith(QUICK_REACTION_PROPOSAL_PREFIX)
                    for audit in projection.proposal_audits
                )
            ),
            None,
        )
        if generic_manifest is not None:
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
            recovered_disposition = None
            if not deferred:
                recovered_disposition = (
                    await self._resolve_expression_episode_before_dispatch(
                        observation=observation,
                        observation_event=observation_event,
                        process=episode,
                    )
                )
            if (
                deferred
                or self._pinned_turn is None
                or self._pinned_turn.expression_episode_mode != "on"
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
            cancelled = recovered_disposition in {
                "cancel_pending",
                "supersede_pending",
            }
            if cancelled:
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
                    else "observed_only"
                    if cancelled
                    else "action_authorized"
                ),
                authorized_action_ids=(
                    ()
                    if deferred or cancelled
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

            recovered_disposition = (
                await self._resolve_expression_episode_before_dispatch(
                    observation=observation,
                    observation_event=observation_event,
                    process=episode,
                )
            )
            cancelled = recovered_disposition in {
                "cancel_pending",
                "supersede_pending",
            }
            if (
                not cancelled
                and (
                    self._pinned_turn is None
                    or self._pinned_turn.expression_episode_mode != "on"
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
                status="observed_only" if cancelled else "action_authorized",
                authorized_action_ids=() if cancelled else action_ids,
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
            has_appraisal_trigger = self._interaction_appraisal_owner is not None and any(
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
        if (
            self._pinned_turn is None
            or self._pinned_turn.expression_episode_mode != "on"
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

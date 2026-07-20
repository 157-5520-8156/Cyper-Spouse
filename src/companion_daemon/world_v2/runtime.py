from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from datetime import UTC, datetime

from .affect_math import DecayAnchor, DecayProfile, decay_intensity_bp
from .errors import ConcurrencyConflict, IdempotencyConflict
from .ledger import LedgerPort, WorldLedger
from .event_identity import domain_idempotency_key
from .clock_authority import append_clock_transition, resolve_latest_clock
from .goal_expiry_runtime import build_due_goal_expiry_events
from .occurrence_clock_continuation import build_occurrence_clock_events
from .outcome_observation_runtime import build_outcome_observation_event
from .pinned_turn import PinnedTurnCompiler
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
from .immediate_emotion_gate import (
    SemanticImmediateEmotionGate,
    resolve_immediate_emotion_gate,
)
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
from .action_pump import ActionExecutor, ActionPump, ActionPumpResult
from .afterthought_author import AfterthoughtAuthorRuntime, AfterthoughtRunResult
from .expression_reconsideration import expression_reconsideration_events_for_observation
from .expression_reconsideration_runtime import (
    ExpressionReconsiderationReviewer,
    ExpressionReconsiderationRunResult,
    ExpressionReconsiderationRuntime,
)
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
from .proactive_action import ProactiveActionRunResult, ProactiveActionRuntime
from .memory_withdrawal_review import (
    MemoryWithdrawalReviewRunResult,
    MemoryWithdrawalReviewRuntime,
)
from .proposal_envelope import DecisionProposal, MinimalProposal, validate_proposal_envelope
from .schemas import (
    ClockObservation,
    CommitResult,
    ExternalObservation,
    OutcomeObservation,
    Observation,
    ProjectionCursor,
    ProjectionRequest,
    RuntimeOutcome,
    WorldEvent,
    WorldProjection,
)


_LOG = logging.getLogger(__name__)

# This is a scheduling gate, not an emotion classifier.  It only decides
# whether the expensive, synchronous inner-appraisal lane is worth paying for
# before the visible reply.  The appraisal LLM still owns meaning, intensity,
# attribution, suppression and persistence.  Ordinary sharing continues on
# the fast expression lane while its already-open appraisal trigger is drained
# in the background; material relational signals take the full same-turn path.
_IMMEDIATE_EMOTION_CUES = (
    "失望",
    "敷衍",
    "不高兴",
    "生气",
    "愤怒",
    "难过",
    "伤心",
    "委屈",
    "冒犯",
    "讨厌",
    "不想聊",
    "不想理",
    "别理我",
    "滚",
    "骗子",
    "背叛",
    # Relational withdrawal and boundary language often carries more signal
    # than an explicit emotion label.  Keep these as scheduling cues only;
    # the appraisal model still decides whether the user is disappointed,
    # hurt, uncomfortable, joking, or simply changing topic.
    "算了",
    "没认真听",
    "当我没说",
    "不舒服",
    "没事",
    # Dehumanization, repair negotiation, and explicit relationship framing
    # are high-signal even when the user never names an emotion directly.
    "程序",
    "复读",
    "只会",
    "原谅",
    "信任",
    "对不起",
    "抱歉",
    "谢谢你",
    "喜欢你",
    "想你",
    "想念",
    "在乎",
    "你还记得",
    "你是不是",
    "为什么不回",
    "怎么不回",
)


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


def _requires_immediate_emotion(observation: Observation) -> bool:
    """Conservatively select high-signal turns for same-turn emotion work."""

    text = observation.text
    if not isinstance(text, str) or not text.strip():
        # An attachment is evidence that perception may be useful, not an
        # emotional signal by itself.  Waiting for the same-turn appraisal
        # lane here made a pure sticker/image message block visible reply
        # generation while the model tried to interpret an image it could not
        # yet see.  The attachment still opens durable perception and
        # interaction-appraisal triggers; those workers can consume the
        # completed visual result on the next bounded background pass.
        return False
    normalized = "".join(text.lower().split())
    return any(cue in normalized for cue in _IMMEDIATE_EMOTION_CUES)


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
        immediate_emotion_signal_gate: bool = False,
        immediate_emotion_semantic_gate: SemanticImmediateEmotionGate | None = None,
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
        quick_reaction_worker: QuickReactionWorker | None = None,
        proactive_action_runtime: ProactiveActionRuntime | None = None,
        afterthought_author: AfterthoughtAuthorRuntime | None = None,
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
        self._immediate_emotion_signal_gate = bool(immediate_emotion_signal_gate)
        # Optional semantic upgrade of the keyword scheduling gate.  It only
        # participates when the signal gate itself is enabled; keyword hits
        # still short-circuit before any model call.
        self._immediate_emotion_semantic_gate = immediate_emotion_semantic_gate
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
        if afterthought_author is not None and afterthought_author.ledger is not self._ledger:
            raise ValueError("afterthought author must own this exact ledger")
        self._afterthought_author = afterthought_author
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
        | AfterthoughtRunResult
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
            # The afterthought window opens seconds after her reply settles,
            # so its bounded consideration must not queue behind the larger
            # appraisal/fact backlog.  Outside its short receipt horizon the
            # check is a cheap projection read and costs no authority.
            if self._afterthought_author is not None:
                afterthought = await self._afterthought_author.drain_one()
                if afterthought.status != "idle":
                    return afterthought
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
                impression = await PrivateImpressionTriggerRuntime(
                    ledger=self._ledger,
                    adapter=self._private_impression_adapter,
                    owner_id=self._private_impression_owner,
                ).drain_one()
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

    async def drain_actions_once(self) -> ActionPumpResult | None:
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
        ).drain_once()

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

    async def _lookup_event_commit(self, event_id: str):
        if self._ledger.blocks_event_loop:
            return await asyncio.to_thread(self._ledger.lookup_event_commit, event_id)
        return self._ledger.lookup_event_commit(event_id)

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

    async def ingest(self, observation: Observation) -> RuntimeOutcome:
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
        async with self._lock:
            existing = await self._lookup_event_commit(event.event_id)
            if existing is not None:
                persisted, original_commit = existing
                if persisted != event:
                    # A locked-head clock rebase is the one permitted
                    # difference between a retry's client envelope and the
                    # committed observation. All other content remains a
                    # genuine idempotency conflict.
                    try:
                        persisted_observation = Observation.model_validate_json(
                            persisted.payload_json
                        )
                        normalized_observation = observation.model_copy(
                            update={"logical_time": persisted_observation.logical_time}
                        )
                    except (TypeError, ValueError):
                        persisted_observation = None
                        normalized_observation = None
                    if (
                        persisted_observation is None
                        or normalized_observation != persisted_observation
                    ):
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
            # WorldTurnRuntime resolves the clock just before entering this
            # lock.  A concurrent scheduler tick can win that gap.  For a new
            # observation, bind the event to the locked head rather than
            # allowing a stale logical_time to violate the Observation reducer
            # invariant.  Existing idempotent observations returned above keep
            # their original immutable envelope.
            locked_logical_time = before.logical_time
            if locked_logical_time is not None and observation.logical_time != locked_logical_time:
                observation = observation.model_copy(update={"logical_time": locked_logical_time})
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
            # Observation, reconsideration, and source-owned trigger openings
            # are one ingress fact.  Commit them as one batch so the durable
            # prefix proof and SQLite transaction are paid once; splitting
            # these identical-cursor writes made warm-chat latency grow with
            # every background lane while providing no additional authority.
            ingress_events = [
                event,
                *expression_reconsideration_events_for_observation(
                    projection=before,
                    observation=observation,
                    source_event=event,
                ),
            ]
            if self._interaction_appraisal_owner is not None:
                trigger_events = interaction_appraisal_trigger_events(
                    observation=observation,
                    observation_event=event,
                    owner_id=self._interaction_appraisal_owner,
                )
                ingress_events.extend(trigger_events)
            if self._interaction_fact_owner is not None:
                fact_event = interaction_fact_trigger_event(
                    observation=observation,
                    observation_event=event,
                )
                ingress_events.append(fact_event)
            if self._read_only_tool_owner is not None:
                tool_event = read_only_tool_trigger_event(
                    observation=observation, observation_event=event
                )
                ingress_events.append(tool_event)
            if self._perception_owner is not None and observation.attachment_refs:
                perception_event = perception_trigger_event(
                    observation=observation, observation_event=event
                )
                ingress_events.append(perception_event)
            committed = await self._commit(
                ingress_events,
                world_revision=before.world_revision,
                deliberation_revision=before.deliberation_revision,
            )
            _LOG.warning(
                "world v2 ingest phase trace=%s phase=ingress_commit_ms value=%.1f",
                observation.trace_id,
                (time.perf_counter() - started) * 1000,
            )
            # Fast tail of the human response distribution: while the main
            # deliberation below is still being prepared, a bounded worker may
            # place one QQ reaction on the message that just committed.  It
            # runs concurrently with the immediate-emotion scheduling gate
            # (neither writes during that overlap), and is awaited *before*
            # any reply cursor is pinned: its world-revision writes must land
            # before the reply deliberation evaluates the world, otherwise
            # the reply Acceptance would become legitimately stale.
            quick_reaction_task: asyncio.Task[QuickReactionRunResult] | None = None
            if self._quick_reaction_worker is not None:
                quick_reaction_task = asyncio.create_task(
                    self._quick_reaction_worker.run_observation(
                        observation=observation,
                        observation_event=event,
                        source_world_revision=committed.world_revision,
                    )
                )
            # A significant emotional shift is part of the current human-like
            # reaction, not a post-reply bookkeeping job.  The dedicated
            # appraisal model decides whether the shift warrants persistence;
            # when it does, the same audited result supplies both Appraisal and
            # Affect.  Only after that durable pass do we compile the visible
            # expression against the new cursor.
            # Scheduling decision order: keyword cue table first (free), then
            # the bounded semantic gate on a keyword miss, then fall back to
            # the keyword verdict on any gate failure.  The durable
            # interaction-appraisal trigger was already opened unconditionally
            # in the ingress batch above, so this gate only chooses same-turn
            # versus background timing and its worst-case cost is the gate's
            # own timeout (2.5s), paid only on keyword-miss turns.  Her own
            # recent texts would sharpen the contrast for cold-withdrawal
            # detection, but reading expression payloads back from the sidecar
            # store here would add I/O to the reply-critical path, so the gate
            # judges the inbound message alone.
            immediate_emotion_selected = self._immediate_emotion_worker is not None and (
                not self._immediate_emotion_signal_gate
                or await resolve_immediate_emotion_gate(
                    keyword_hit=_requires_immediate_emotion(observation),
                    text=observation.text,
                    gate=self._immediate_emotion_semantic_gate,
                )
            )
            quick_reaction: QuickReactionRunResult | None = None
            if quick_reaction_task is not None:
                # The worker owns hard internal budgets and never raises; this
                # await is bounded by the local gate timeout plus one provider
                # round trip, most of which already overlapped the scheduling
                # gate above.
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
            emotion_ready = True
            if immediate_emotion_selected:
                assert self._interaction_appraisal_turn is not None
                assert self._appraisal_worker is not None
                assert self._interaction_appraisal_owner is not None
                immediate_result = await InteractionAppraisalTriggerRuntime(
                    ledger=self._ledger,
                    pinned_turn=self._interaction_appraisal_turn,
                    worker=self._appraisal_worker,
                    owner_id=self._interaction_appraisal_owner,
                    affect_owner_id=self._affect_deliberation_owner,
                    relationship_owner_id=self._relationship_deliberation_owner,
                    immediate_emotion_worker=self._immediate_emotion_worker,
                ).run_observation(observation.observation_id)
                emotion_ready = immediate_result.status in {"processed", "completed_existing"}
                if not emotion_ready:
                    reply_deferred_refs = (
                        *reply_deferred_refs,
                        f"immediate_emotion.{immediate_result.status}",
                    )
                head = await self._project_for_write()
                reply_cursor = ProjectionCursor(
                    world_revision=head.world_revision,
                    deliberation_revision=head.deliberation_revision,
                    ledger_sequence=head.ledger_sequence,
                )
                _LOG.warning(
                    "world v2 ingest phase trace=%s phase=immediate_emotion_ms value=%.1f status=%s",
                    observation.trace_id,
                    (time.perf_counter() - started) * 1000,
                    immediate_result.status,
                )
            elif self._immediate_emotion_worker is not None:
                _LOG.warning(
                    "world v2 ingest phase trace=%s phase=immediate_emotion_skipped reason=low_signal",
                    observation.trace_id,
                )
                head = await self._project_for_write()
                reply_cursor = ProjectionCursor(
                    world_revision=head.world_revision,
                    deliberation_revision=head.deliberation_revision,
                    ledger_sequence=head.ledger_sequence,
                )
            else:
                emotion_ready = True
                if quick_reaction is not None and quick_reaction.ledger_advanced:
                    # The quick lane committed after the ingress batch; the
                    # reply must be pinned at the true head, not the stale
                    # ingress commit cursor.
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
            if self._pinned_turn is not None and emotion_ready:
                audited = await self._pinned_turn.audit_observation(
                    observation=observation,
                    observation_event=event,
                    cursor=reply_cursor,
                    skip_advisories=(
                        self._immediate_emotion_worker is not None
                        and not immediate_emotion_selected
                    ),
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
                                    logical_time=after_audit.logical_time
                                    or observation.logical_time,
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
                                reply_deferred_refs = (
                                    f"expression-budget-account:{self._expression_policy.account_id}",
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
                                    if exc.code in {
                                        "expression_plan_acceptance.budget_unavailable",
                                        "expression_plan_acceptance.budget_account_unavailable",
                                    }:
                                        reply_deferred_refs = (exc.code,)
                                    else:
                                        reply_terminal_errors = (exc.code,)
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
        if reply_authorized:
            status = "action_authorized"
        elif reply_terminal_errors:
            status = "failed_safe"
        elif reply_deferred_refs:
            status = "deferred"
        else:
            status = "observed_only"
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
    ) -> RuntimeOutcome:
        """Join a completed reply acceptance without repeating model work.

        The Observation itself commits before its deliberation and acceptance
        follow-ups.  On ingress retry, the durable minimal manifest is the
        authority for the final visible outcome; returning the Observation's
        old cursor would incorrectly erase an already-authorized reply.
        """

        projection = await self._project_for_write()
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
            return RuntimeOutcome(
                outcome_id=f"outcome:{trigger_id}",
                trigger_id=trigger_id,
                observation_ref=observation.observation_id,
                committed_world_revision=committed.world_revision,
                ledger_sequence=committed.ledger_sequence,
                status="deferred" if deferred else "action_authorized",
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

"""Production composition root for the first platform-neutral World v2 turn lane.

This module is intentionally the only place that knows how the persistent
ledger, accepted-batch issuer, deliberation adapters, payload reader and
platform Action executor fit together.  Platform hosts receive the much
smaller :class:`WorldV2TurnApplication` interface and cannot reintroduce a
second Engine or Ledger write path.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, UTC
import hashlib
import json
import logging
from pathlib import Path
import time
from typing import Literal, Mapping

from companion_daemon import event_media

from .accepted_ledger_batch import AcceptedLedgerBatchIssuer
from .action_pump import ActionExecutor, ActionPumpResult
from .activity_plan_runtime import (
    ActivityPlanCommand,
    ActivityPlanRuntime,
    ActivityPlanTransitionCommand,
)
from .activity_lifecycle_draft import ActivityLifecycleDraftAdapter, ActivityLifecycleDraftModel
from .activity_lifecycle_runtime import (
    ActivityLifecycleAcceptanceRuntime,
    ActivityLifecycleProposalRecorder,
)
from .activity_lifecycle_worker import ActivityLifecycleWorker
from .life_ecology_activity import ActivityOpeningCatalog
from .deferred_reply_runtime import DeferredReplyRuntime
from .affect_trigger_runtime import AffectTriggerRunResult
from .fact_draft_adapter import FactDraftChatModel, FactObservationProposalAdapter
from .fact_memory_candidate_lifecycle import FactMemoryCandidateLifecycle
from .experience_memory_candidate_lifecycle import ExperienceMemoryCandidateLifecycle
from .fact_memory_draft import FactMemoryDraftChatModel, FactMemoryDraftAdapter
from .fact_v2_acceptance_runtime import FactV2AcceptanceRuntime
from .interaction_fact_trigger_runtime import FactTriggerRunResult
from .affect_acceptance_runtime import AffectAcceptanceRuntime
from .affect_deliberation_worker import AffectDeliberationWorker
from .affect_proposal_compiler import AffectProposalCompiler
from .relationship_acceptance_runtime import RelationshipAcceptanceRuntime
from .relationship_adjustment_acceptance_runtime import (
    RelationshipAdjustmentAcceptanceRuntime,
)
from .relationship_adjustment_compiler import RelationshipAdjustmentCompiler
from .relationship_adjustment_worker import RelationshipAdjustmentWorker
from .relationship_deliberation_worker import RelationshipDeliberationWorker
from .relationship_proposal_compiler import RelationshipProposalCompiler
from .appraisal_acceptance_runtime import AppraisalAcceptanceRuntime
from .appraisal_chat_model_adapter import AppraisalDraftDeliberationAdapter
from .appraisal_proposal_compiler import AppraisalProposalCompiler
from .appraisal_proposal_worker import AppraisalProposalWorker
from .immediate_emotion_proposal_worker import ImmediateEmotionProposalWorker
from .interaction_appraisal_trigger_runtime import AppraisalTriggerRunResult
from .outcome_acceptance_runtime import OutcomeAcceptanceRuntime
from .outcome_candidate_reader import OutcomeCandidateReader
from .outcome_draft_deliberation_adapter import OutcomeDraftDeliberationAdapter
from .outcome_deliberation_turn import OutcomeDeliberationTurn
from .outcome_proposal_compiler import OutcomeProposalCompiler
from .outcome_proposal_worker import OutcomeProposalWorker
from .outcome_trigger_runtime import OutcomeTriggerRunResult
from .outcome_selection_draft import OutcomeSelectionModel
from .interaction_bid_acceptance_runtime import InteractionBidAcceptanceRuntime
from .interaction_bid_deliberation_turn import InteractionBidDeliberationTurn
from .interaction_bid_proposal_compiler import InteractionBidProposalCompiler
from .interaction_bid_proposal_worker import InteractionBidProposalWorker
from .interaction_bid_trigger_runtime import InteractionBidTriggerRunResult
from .media_thread_acceptance_runtime import MediaDeliveryThreadAcceptanceRuntime
from .media_thread_proposal_compiler import MediaDeliveryThreadProposalCompiler
from .advisory_compiler import AdvisoryCompiler
from .deliberation import (
    DeliberationModelAdapter,
    ModelRouterAdapter,
    QuickRecoveryAdapter,
)
from .production_proposal_grammar import compose_production_deliberation
from .ledger_context_resolver import (
    ContextRelevanceScope,
    context_capsule_compiler_from_ledger,
    fact_recall_items,
)
from .change_phase_view import change_phase_reading_prose, change_phase_readings
from .mood_view import MOOD_LABELS
from .npc_relationship_view import npc_relationship_readings
from .context_capsule import ContextCapsuleBudgetPolicy, SliceBudget
from .ledger_payload_reader import LedgerAuthorizedPayloadReader
from .local_chronology import LocalChronology
from .life_content_store import SQLiteImmutableLifeContentStore
from .situation_compiler import SituationCompiler
from .expression_payload_store import SQLiteImmutableExpressionPayloadStore
from .media_v2 import MediaPlanner, SQLiteImmutableMediaPayloadStore
from .event_media_planner_adapter import (
    EventMediaPlannerAdapter,
    EventMediaPlanningResultStore,
)
from .media_evidence_snapshot import MediaEvidenceSnapshotCompiler
from .event_ecology_media import (
    EcologySourceTaxon,
    EcologyDrainResult,
    EcologyPolicy,
    EventEcologyMediaCandidateRuntime,
)
from .life_ecology_runtime import (
    LifeEcologyAvailability,
    LifeEcologyRunResult,
    LifeEcologyRuntime,
)
from .aspiration_runtime import AspirationRuntime
from .private_impression_producer import (
    PrivateImpressionChatModel,
    PrivateImpressionDraftAdapter,
)
from .shared_private_invitation import SharedPrivateInvitationRuntime
from .npc_initiative import NpcInitiativeRuntime
from .open_world_event_draft import OpenWorldEventModel
from .open_world_event_runtime import (
    ActivePlanSituationSource,
    OpenWorldEventRuntime,
)
from .life_ecology_trigger_store import LedgerLifeEcologyTriggerStore
from .future_life_author import FutureLifeAuthorRuntime
from .life_author_runtime import LifeAuthorRuntime
from .life_author_seed import ReviewedLifeSeedCatalog
from .life_aftermath_runtime import LifeAftermathRuntime
from .life_visual_evidence_author import LifeVisualEvidenceAuthor
from .life_events import LIFE_PAYLOAD_MODELS, NpcRegisteredPayload
from .event_identity import domain_idempotency_key
from .test_economy import CostProfile
from .media_execution_runtime import MediaExecutionRuntime, MediaExecutionWorker
from .media_continuation_runtime import (
    MediaContinuationActionPolicy,
    MediaContinuationRuntime,
    MediaContinuationWorker,
)
from .media_planning_runtime import MediaPlanningRuntime
from .media_planning_worker import MediaPlanningRunResult, MediaPlanningWorker
from .media_candidate_maintenance import (
    MediaCandidateMaintenanceResult,
    MediaCandidateMaintenanceRuntime,
)
from .image_evidence_runtime import (
    ImageEvidenceDeclarationCommand,
    ImageEvidenceDeclarationRuntime,
)
from .private_image_evidence_runtime import (
    RecipientScopedImageEvidenceDeclarationCommand,
    RecipientScopedImageEvidenceDeclarationRuntime,
)
from .appearance_state import AppearanceStateRecordCommand
from .appearance_state_runtime import AppearanceStateRuntime
from .visible_physical_state import VisiblePhysicalStateRecordCommand
from .visible_physical_state_runtime import VisiblePhysicalStateRuntime
from .visual_fact import VisualFactRecordCommand, VisualFactRuntime
from .character_media_fact_binder import CharacterMediaCandidateRuntime
from .media_selection_acceptance_runtime import MediaSelectionProposalRecorder
from .media_selection_acceptance_runtime import MediaSelectionAcceptanceRuntime
from .media_opportunity_authorizer import MediaOpportunityAuthorizer
from .media_selection_draft import MediaSelectionDraftAdapter, MediaSelectionDraftModel
from .media_selection_worker import MediaSelectionRunResult, MediaSelectionWorker
from .media_preview_conductor import (
    MediaPreviewAcceptanceOutcome,
    MediaPreviewConductor,
    MediaPreviewConductorResult,
)
from .media_auto_delivery import (
    MediaAutoDeliveryComposition,
    MediaAutoDeliveryRunResult,
    MediaAutoDeliveryWorker,
)
from .media_payload_reader import MediaSidecarPayloadReader, PlatformAndMediaPayloadReader
from .media_delivery_runtime import MediaDeliveryRuntime
from .media_v2 import MediaAutomaticDeliveryApproval
from .occurrence_content_coordinator import (
    OccurrenceContentCommitRequest,
    OccurrenceContentCoordinator,
)
from .minimal_reply_acceptance import ReplyBudgetPolicy
from .minimal_reply_atomic_recorder import MinimalReplyAtomicRecorder
from .expression_plan_acceptance import ExpressionPlanBudgetPolicy
from .expression_plan_atomic_recorder import ExpressionPlanAtomicRecorder
from .expression_reconsideration_model_adapter import (
    ExpressionReconsiderationChatModelAdapter,
)
from .expression_reconsideration_runtime import (
    ExpressionReconsiderationReviewer,
    ExpressionReconsiderationRunResult,
)
from .pinned_turn import PinnedTurnCompiler
from .production_latency_trace import (
    ProductionLatencyRecorder,
    ProductionLatencySample,
    TraceEnvironment,
)
from .production_performance_evidence import (
    ProductionPerformanceEvidence,
    ProductionPerformanceEvidenceReader,
)
from .expression_draft import QQ_NAPCAT_EXPRESSION_CAPABILITIES
from .social_action_acceptance import SocialDeferredPolicy
from .social_action_worker import SocialActionRunResult, SocialActionWorker
from .quick_reaction import QuickReactionWorker
from .chat_model_deliberation_adapter import ChatCompletionModel, CompanionIdentityFrame
from .afterthought_author import AfterthoughtAuthorRuntime, AfterthoughtRunResult
from .proactive_action import (
    ProactiveActionRuntime,
    ProactiveDeliberationTurn,
    ProactiveDraftAdapter,
)
from .recent_dialogue import RecentDialogueCompiler
from .social_initiative import (
    SocialInitiativeCompiler,
    SocialInitiativeContextPolicy,
    SocialInitiativePolicy,
    social_initiative_attempt_id,
)
from .random_authority import RandomDrawRecordedPayload
from .memory_withdrawal_review import (
    MemoryWithdrawalReviewAdapter,
    MemoryWithdrawalReviewRunResult,
    MemoryWithdrawalReviewRuntime,
)
from .settled_world_appraisal_turn import SettledWorldAppraisalTurn
from .plan_disruption_appraisal_trigger_runtime import PlanDisruptionAppraisalTurn
from .silence_appraisal_trigger_runtime import SilenceAppraisalTurn
from .platform_action_executor import (
    PlatformActionExecutor, PlatformTransport, MediaProviderTransport,
    ProviderMediaActionExecutor, RoutedActionExecutor,
)
from .read_only_tool_deliberation import compose_injected_read_only_tool_deliberation
from .read_only_tool_authorization_resolver import ProjectionReadOnlyToolAuthorizationResolver
from .read_only_tool_executor import ReadOnlyToolActionExecutor, ReadOnlyToolTransport
from .read_only_tool_proposal_compiler import ReadOnlyToolProposalCompiler
from .read_only_tool_query_reader import AuditedReadOnlyToolQueryReader
from .read_only_tool_trigger_runtime import ReadOnlyToolTriggerRuntime
from .external_result_trigger_runtime import NoopToolResultDeliberator
from .perception_authorization_resolver import ProjectionPerceptionAuthorizationResolver
from .perception_deliberation import compose_injected_perception_deliberation
from .perception_executor import PerceptionActionExecutor, PerceptionTransport
from .perception_input_source import PerceptionInputSource
from .perception_proposal_compiler import PerceptionProposalCompiler
from .perception_result_trigger_runtime import NoopPerceptionResultDeliberator
from .perception_trigger_runtime import PerceptionTriggerRuntime
from .runtime import WorldRuntime
from .projection import ProjectionAuthority
from .replay_evidence import ReplayEvidence
from .schemas import (
    BudgetAccount,
    ClockObservation,
    CommitResult,
    EvidenceRef,
    ExternalObservation,
    OutcomeObservation,
    ProjectionCursor,
    ProjectionRequest,
    ProviderMediaGrantBinding,
    NpcProjection,
    RuntimeOutcome,
    WorldEvent,
    WorldProjection,
)
from .sqlite_ledger import SQLiteWalMaintenanceResult, SQLiteWorldLedger
from .world_turn_runtime import InboundIdentityResolver, InboundTurn, WorldTurnRuntime


_LOG = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class LifeEcologyComposition:
    """Explicit production profile for the durable Life Ecology worker.

    A profile owns both the source-bound media policy and the ledger-backed
    trigger identity.  Leaving it absent keeps embedded hosts and fixtures
    visibly unavailable instead of silently creating background world work.
    """

    catalog_version: str
    media_policy: EcologyPolicy
    seed_catalog_path: Path = Path("configs/world_seed.yaml")
    worker_actor: str = "worker:world-v2:life-ecology"
    lease_seconds: int = 120

    @classmethod
    def production_v1(
        cls, *, seed_catalog_path: Path = Path("configs/world_seed.yaml")
    ) -> "LifeEcologyComposition":
        return cls(
            catalog_version="life-ecology.1",
            seed_catalog_path=seed_catalog_path,
            # Production P1 publishes evidence-backed candidates only.  An
            # opportunity, budget reservation, and planning Action can arise
            # only from the separately accepted selection path; the old
            # direct-freeze route remains an explicit migration/test switch.
            media_policy=EcologyPolicy(direct_preview_compatibility=False),
        )

    def __post_init__(self) -> None:
        if (
            not self.catalog_version or not self.worker_actor or self.lease_seconds <= 0
            or not str(self.seed_catalog_path)
        ):
            raise ValueError("life ecology composition is invalid")


@dataclass(frozen=True, slots=True)
class MediaSelectionAcceptanceComposition:
    """Explicit provider grant and image-budget facts for P1 Acceptance."""

    grant: ProviderMediaGrantBinding
    account_id: str
    account_window_id: str
    account_limit: int
    amount_limit: int
    actor: str = "worker:world-v2:media-selection-acceptance"

    def __post_init__(self) -> None:
        if (
            not self.account_id
            or not self.account_window_id
            or not self.actor
            or self.amount_limit < 0
            or self.account_limit < self.amount_limit
        ):
            raise ValueError("media selection acceptance composition is invalid")


@dataclass(frozen=True, slots=True)
class MediaContinuationComposition:
    """Separate render/inspection provider authority and budget envelopes."""

    render_grant: ProviderMediaGrantBinding
    render_account_id: str
    render_window_id: str
    render_account_limit: int
    render_amount_limit: int
    inspection_grant: ProviderMediaGrantBinding
    inspection_account_id: str
    inspection_window_id: str
    inspection_account_limit: int
    inspection_amount_limit: int
    actor: str = "worker:world-v2:media-continuation"
    owner_id: str = "worker:world-v2:media-continuation"

    def __post_init__(self) -> None:
        if (
            not self.actor or not self.owner_id
            or not self.render_account_id or not self.render_window_id
            or not self.inspection_account_id or not self.inspection_window_id
            or self.render_account_id == self.inspection_account_id
            or self.render_amount_limit < 0
            or self.inspection_amount_limit < 0
            or self.render_account_limit < self.render_amount_limit
            or self.inspection_account_limit < self.inspection_amount_limit
        ):
            raise ValueError("media continuation composition is invalid")


@dataclass(frozen=True, slots=True)
class MediaPreviewDeployment:
    """Opt-in dependencies for the media preview lane.

    Each supplied stage is complete and explicit.  Grant bindings must already
    refer to independently provisioned enforcement authority; this composition
    never manufactures or signs that authority.
    ``continuation=None`` intentionally installs only the candidate-to-plan
    prefix.  It is not a complete preview pipeline and cannot render or
    inspect; full preview requires the separate render/inspection authority.
    ``auto_delivery`` installs the world-owned delivery policy: the send
    decision is the already-accepted media selection, and this composition
    only adds operational guardrails (daily cap, minimum gap).  Absent, the
    lane stops at ``MediaPreviewGenerated``.
    """

    selection_model: MediaSelectionDraftModel
    planner: MediaPlanner
    acceptance: MediaSelectionAcceptanceComposition
    continuation: MediaContinuationComposition | None = None
    auto_delivery: MediaAutoDeliveryComposition | None = None

    def __post_init__(self) -> None:
        if self.selection_model is None or self.planner is None or self.acceptance is None:
            raise ValueError("media preview deployment requires selector, planner and acceptance")
        if self.continuation is not None and self.acceptance.account_id in {
            self.continuation.render_account_id,
            self.continuation.inspection_account_id,
        }:
            raise ValueError("selection, render and inspection require separate budget accounts")
        if self.auto_delivery is not None and self.continuation is None:
            raise ValueError("media auto-delivery requires the render/inspection continuation")


@dataclass(frozen=True, slots=True)
class WorldV2TurnApplicationConfig:
    """Composition-owned facts for one persistent companion world."""

    world_id: str
    companion_actor_ref: str
    reply_target: str
    action_pump_owner: str
    counterpart_actor_ref: str | None = None
    local_timezone: str = "Asia/Shanghai"
    chat_account_id: str = "account:world-v2:chat"
    chat_window_id: str = "window:world-v2:chat"
    chat_budget_limit: int = 10_000
    reply_budget_amount: int = 10
    reply_recovery_policy: str = "effect_once"
    expression_action_kinds: frozenset[str] = frozenset(
        {"reply", "followup", "proactive_message"}
    )
    appraisal_worker_owner: str = "worker:world-v2:appraisal"
    affect_worker_owner: str = "worker:world-v2:affect"
    relationship_worker_owner: str = "worker:world-v2:relationship"
    relationship_adjustment_worker_owner: str = "worker:world-v2:relationship-adjustment"
    fact_worker_owner: str = "worker:world-v2:fact"
    private_impression_worker_owner: str = "worker:world-v2:private-impression"
    memory_review_worker_owner: str = "worker:world-v2:memory-review"
    outcome_worker_owner: str = "worker:world-v2:outcome"
    interaction_bid_worker_owner: str = "worker:world-v2:interaction-bid"
    expression_reconsideration_owner: str = "worker:world-v2:expression-reconsideration"
    social_action_worker_owner: str = "worker:world-v2:social-action"
    media_planning_worker_owner: str = "worker:world-v2:media-planning"
    event_ecology_worker_actor: str = "worker:world-v2:event-ecology"
    media_selection_worker_actor: str = "worker:world-v2:media-selection"
    media_candidate_maintenance_actor: str = "worker:world-v2:media-candidate-maintenance"
    media_selection_acceptance: MediaSelectionAcceptanceComposition | None = None
    media_continuation: MediaContinuationComposition | None = None
    media_auto_delivery: MediaAutoDeliveryComposition | None = None
    event_ecology_policy: EcologyPolicy | None = None
    life_ecology: LifeEcologyComposition | None = None
    media_cost_profile: CostProfile | None = None
    tool_account_id: str = "account:world-v2:tool"
    tool_window_id: str = "window:world-v2:tool"
    tool_budget_limit: int = 0
    tool_worker_owner: str = "worker:world-v2:read-only-tool"
    perception_account_id: str = "account:world-v2:perception"
    perception_window_id: str = "window:world-v2:perception"
    perception_budget_limit: int = 0
    perception_worker_owner: str = "worker:world-v2:perception"
    trace_environment: TraceEnvironment = "offline_in_process"
    proactive_account_id: str = "account:world-v2:proactive"
    proactive_window_id: str = "window:world-v2:proactive"
    proactive_budget_limit: int = 1_000
    proactive_amount_per_action: int = 10
    proactive_worker_owner: str = "worker:world-v2:proactive"
    social_initiative_policy: SocialInitiativePolicy = SocialInitiativePolicy()
    # Afterthought lane (v1 "事后补充话" port): after her reply settles with
    # the provider, one recorded low-mass draw plus one bounded background
    # confirmation may schedule a single short ``followup`` tail
    # (quick_continue 12-30s / topic_drift 75-180s).  It shares the proactive
    # budget/grammar and this switch is the operational kill toggle.
    afterthought_enabled: bool = True
    afterthought_worker_owner: str = "worker:world-v2:afterthought"
    # Production platform hosts may gate the expensive same-turn emotion lane
    # to high-signal relational turns; fixtures keep the historical eager
    # behavior unless they opt in.
    immediate_emotion_signal_gate: bool = False
    # When the signal gate is on, keyword misses may additionally consult the
    # local small appraisal model (bounded, ~2.5s worst case) so unlabeled
    # relational signals (cold withdrawal, sarcasm, sudden distance) still get
    # same-turn emotion work.  ``False`` restores the pure keyword gate.  The
    # durable interaction-appraisal trigger is unaffected either way.
    semantic_immediate_emotion_gate: bool = True
    # Same-turn quick reaction lane: before the visible reply compiles, a
    # recorded act/hold draw plus one bounded local-model confirmation may
    # place a single QQ reaction on the triggering message.  It only composes
    # where the ``reaction`` expression capability is closed end-to-end and
    # the local appraisal checkpoint is installed; this switch is the
    # operational kill toggle.
    quick_reaction_enabled: bool = True
    # How long the user must stay quiet after her delivered reply before she
    # gets one chance to appraise the silence.  ``0``/``None`` disables the
    # lane; the QQ composition keeps the default enabled.
    silence_appraisal_idle_seconds: int | None = 3_600
    # Every committed plan abandonment leaves her one chance to appraise what
    # losing that plan means (regret, relief, nothing).  Disabling stops
    # opening new triggers; already-open ones still drain.
    plan_disruption_appraisal_enabled: bool = True
    # The future calendar lane: at most one successful multi-day plan per
    # companion-local day, written by the Future Life Author from the seed
    # catalog's reviewed ``future_openings``.  It shares the life ecology
    # worker and model, so it only exists where life_ecology is installed.
    future_life_author_enabled: bool = True
    # NPC light autonomy: reviewed ``npc_initiated_events`` may enter her day
    # uninvited through a recorded probability draw plus a bounded model
    # confirmation (at most two checks and one occurrence per local day).  It
    # shares the life ecology worker and model like the future author lane.
    npc_initiative_enabled: bool = True
    # The aspiration layer: reviewed ``aspiration_seeds`` may sprout into
    # low-stakes wishes (no due window, no lifecycle pipeline) through one
    # recorded low-probability draw plus a bounded model confirmation per
    # companion-local day; the same daily check probabilistically reinforces
    # or quietly fades existing wishes.  Active wishes surface in the chat
    # capsule as a ledger-backed read-only advisory.
    aspiration_enabled: bool = True
    # A wish untouched by related material for this many local days becomes
    # fade-eligible; each daily check then rolls the recorded fade chance.
    aspiration_fade_idle_days: int = 14
    aspiration_fade_chance_bp: int = 1_000
    # A supported active wish whose seed names a reviewed crystallization
    # target rolls this recorded per-day chance to become a concrete future
    # plan (then still needs the bounded model's confirmation).
    aspiration_crystallize_chance_bp: int = 1_500
    # shared_private invitations: enabled only when the catalog reviews a
    # shared_private future opening AND the composition names the counterpart
    # user actor.  The daily recorded chance keeps the ask rare.
    shared_private_invitation_enabled: bool = True
    shared_private_invite_chance_bp: int = 2_000

    def __post_init__(self) -> None:
        for name in (
            "world_id",
            "companion_actor_ref",
            "reply_target",
            "action_pump_owner",
            "appraisal_worker_owner",
            "affect_worker_owner",
            "relationship_worker_owner",
            "relationship_adjustment_worker_owner",
            "fact_worker_owner",
            "private_impression_worker_owner",
            "memory_review_worker_owner",
            "outcome_worker_owner",
            "interaction_bid_worker_owner",
            "expression_reconsideration_owner",
            "social_action_worker_owner",
            "media_planning_worker_owner",
            "event_ecology_worker_actor",
            "media_selection_worker_actor",
            "media_candidate_maintenance_actor",
            "tool_worker_owner",
            "perception_worker_owner",
            "proactive_worker_owner",
            "afterthought_worker_owner",
        ):
            if not getattr(self, name):
                raise ValueError(f"{name} must not be empty")
        if not self.chat_account_id or not self.chat_window_id:
            raise ValueError("chat account identity must not be empty")
        if self.counterpart_actor_ref is not None and not self.counterpart_actor_ref:
            raise ValueError("counterpart_actor_ref must be absent or non-empty")
        LocalChronology(self.local_timezone)
        if not 0 <= self.reply_budget_amount <= self.chat_budget_limit <= 10_000_000:
            raise ValueError("chat budget limits are invalid")
        if not self.reply_recovery_policy:
            raise ValueError("reply recovery policy must not be empty")
        if not self.expression_action_kinds:
            raise ValueError("expression action capability set must not be empty")
        if not self.tool_account_id or not self.tool_window_id or self.tool_budget_limit < 0:
            raise ValueError("tool budget config is invalid")
        if (
            not self.perception_account_id
            or not self.perception_window_id
            or self.perception_budget_limit < 0
        ):
            raise ValueError("perception budget config is invalid")
        if self.trace_environment not in {"offline_in_process", "real_transport"}:
            raise ValueError("trace environment is invalid")
        if (
            not self.proactive_account_id
            or not self.proactive_window_id
            or not 0 <= self.proactive_amount_per_action <= self.proactive_budget_limit <= 10_000_000
        ):
            raise ValueError("proactive budget config is invalid")
        if (
            self.life_ecology is not None
            and self.event_ecology_policy is not None
            and self.event_ecology_policy != self.life_ecology.media_policy
        ):
            raise ValueError("life ecology and event ecology policies must agree")
        if self.silence_appraisal_idle_seconds is not None and self.silence_appraisal_idle_seconds < 0:
            raise ValueError("silence appraisal idle threshold must not be negative")


class WorldV2TurnApplication:
    """Small host-facing interface for the persistent single-reply v2 lane."""

    def __init__(
        self,
        *,
        turns: WorldTurnRuntime,
        ledger: SQLiteWorldLedger,
        life_content_store: SQLiteImmutableLifeContentStore,
        expression_payload_store: SQLiteImmutableExpressionPayloadStore,
        media_payload_store: SQLiteImmutableMediaPayloadStore,
        media_execution: MediaExecutionRuntime,
        media_execution_worker: MediaExecutionWorker | None,
        media_continuation_worker: MediaContinuationWorker | None,
        media_planning: MediaPlanningRuntime,
        media_planning_worker: MediaPlanningWorker,
        media_ecology: EventEcologyMediaCandidateRuntime | None,
        life_ecology: LifeEcologyRuntime | None,
        event_ecology_worker_actor: str,
        media_selection_worker: MediaSelectionWorker | None,
        media_selection_worker_actor: str,
        media_candidate_maintenance: MediaCandidateMaintenanceRuntime,
        media_candidate_maintenance_actor: str,
        character_media_candidates: CharacterMediaCandidateRuntime,
        image_evidence: ImageEvidenceDeclarationRuntime,
        recipient_scoped_image_evidence: RecipientScopedImageEvidenceDeclarationRuntime,
        appearance_states: AppearanceStateRuntime,
        visible_physical_states: VisiblePhysicalStateRuntime,
        visual_facts: VisualFactRuntime,
        media_selection_acceptance: MediaSelectionAcceptanceRuntime | None,
        media_selection_acceptance_config: MediaSelectionAcceptanceComposition | None,
        media_preview_conductor_enabled: bool,
        media_delivery: MediaDeliveryRuntime,
        media_auto_delivery: MediaAutoDeliveryComposition | None = None,
        occurrence_content: OccurrenceContentCoordinator,
        activity_plans: ActivityPlanRuntime,
        deferred_replies: DeferredReplyRuntime,
        latency_recorder: ProductionLatencyRecorder,
        trace_environment: TraceEnvironment,
        social_initiative_policy: SocialInitiativePolicy,
    ) -> None:
        self._turns = turns
        self._ledger = ledger
        self._life_content_store = life_content_store
        self._expression_payload_store = expression_payload_store
        self._media_payload_store = media_payload_store
        self.media_execution = media_execution
        self._media_execution_worker = media_execution_worker
        self._media_continuation_worker = media_continuation_worker
        self._media_planning = media_planning
        self._media_planning_worker = media_planning_worker
        self._media_ecology = media_ecology
        self._life_ecology = life_ecology
        self._event_ecology_worker_actor = event_ecology_worker_actor
        self._media_selection_worker = media_selection_worker
        self._media_selection_worker_actor = media_selection_worker_actor
        self._media_candidate_maintenance = media_candidate_maintenance
        self._media_candidate_maintenance_actor = media_candidate_maintenance_actor
        self._character_media_candidates = character_media_candidates
        self._image_evidence = image_evidence
        self._recipient_scoped_image_evidence = recipient_scoped_image_evidence
        self._appearance_states = appearance_states
        self._visible_physical_states = visible_physical_states
        self._visual_facts = visual_facts
        self._media_selection_acceptance = media_selection_acceptance
        self._media_selection_acceptance_config = media_selection_acceptance_config
        self._media_preview_conductor = (
            MediaPreviewConductor(
                select=self._select_media_preview_candidate,
                accept=self._accept_media_preview_selection,
                planning=media_planning_worker,
            )
            if (
                media_preview_conductor_enabled
                and media_selection_worker is not None
                and media_selection_acceptance is not None
                and media_selection_acceptance_config is not None
            )
            else None
        )
        self._media_delivery = media_delivery
        self._media_auto_delivery = (
            MediaAutoDeliveryWorker(
                application=self, ledger=ledger, composition=media_auto_delivery
            )
            if media_auto_delivery is not None
            else None
        )
        self._occurrence_content = occurrence_content
        self._activity_plans = activity_plans
        self._deferred_replies = deferred_replies
        self._latency = latency_recorder
        self._trace_environment = trace_environment
        self._social_initiative_policy = social_initiative_policy

    async def respond(self, inbound: InboundTurn) -> RuntimeOutcome:
        return await self._turns.respond(inbound)

    async def inbound(
        self,
        *,
        platform: str,
        platform_user_id: str,
        platform_message_id: str,
        text: str | None,
        observed_at: datetime,
        trace_id: str,
        attachment_refs: tuple[str, ...] = (),
        coalescing_metadata: Mapping[str, object] | None = None,
    ) -> RuntimeOutcome:
        """Accept one platform-neutral message through the sole v2 ingress seam.

        A platform host owns parsing provider envelopes, but not construction of
        runtime or ledger commands.  Keeping this small primitive interface on
        the application means a host can depend only on this composition root,
        rather than importing ``WorldTurnRuntime`` or a ledger implementation.
        """

        self._start_ingress_trace(
            trace_id=trace_id,
            coalescing_metadata=coalescing_metadata,
        )
        return await self.respond(
            InboundTurn(
                platform=platform,
                platform_user_id=platform_user_id,
                platform_message_id=platform_message_id,
                text=text,
                observed_at=observed_at,
                trace_id=trace_id,
                attachment_refs=attachment_refs,
                coalescing_metadata=dict(coalescing_metadata or {}),
            )
        )

    def _start_ingress_trace(
        self, *, trace_id: str, coalescing_metadata: Mapping[str, object] | None
    ):
        metadata = dict(coalescing_metadata or {})
        opened = _parse_trace_time(metadata.get("window_opened_at"))
        closed = _parse_trace_time(metadata.get("window_closed_at"))
        processing = _parse_trace_time(metadata.get("processing_started_at"))
        coalescing_ms = 0.0
        queue_ms = 0.0
        if opened is not None and closed is not None and closed >= opened:
            coalescing_ms = (closed - opened).total_seconds() * 1_000
            now = processing.astimezone(UTC) if processing is not None else datetime.now(UTC)
            queue_ms = max(0.0, (now - closed.astimezone(UTC)).total_seconds() * 1_000)
        trace = self._latency.start_ingress(
            trace_id=trace_id,
            environment=self._trace_environment,
            elapsed_before_registration_ms=coalescing_ms + queue_ms,
        )
        # Zero is real evidence for an application ingress with no configured
        # coalescer or pre-runtime queue; it is not a model/provider estimate.
        if not any(sample.segment == "coalescing" for sample in trace.samples()):
            trace.record_duration("coalescing", duration_ms=coalescing_ms)
        if not any(sample.segment == "queue" for sample in trace.samples()):
            trace.record_duration("queue", duration_ms=queue_ms)
        return trace

    def latency_samples(self) -> tuple[ProductionLatencySample, ...]:
        return self._latency.samples()

    def visible_mood(self) -> str:
        """Project the strongest accepted affect into the HTTP mood vocabulary.

        This is a read-only presentation mapping.  Affect episodes remain the
        World authority; the legacy-compatible ``mood`` field must not be
        hard-coded to calm after an accepted hurt/anger transition.
        """

        projection = self._ledger.project()
        weights: dict[str, int] = {}
        for episode in projection.affect_episodes:
            if episode.status != "active":
                continue
            for component in episode.components:
                weights[component.dimension] = max(
                    weights.get(component.dimension, 0), component.intensity_bp
                )
        if not weights:
            return "calm"
        dimension, intensity = max(weights.items(), key=lambda item: item[1])
        if intensity < 1_800:
            return "calm"
        return {
            "anger": "hurt",
            "resentment": "sulking",
            "hurt": "hurt",
            "sadness": "worried",
            "loneliness": "miss_you",
            "anxiety": "worried",
            "warmth": "affectionate",
            "joy": "happy",
        }.get(dimension, "calm")

    def performance_evidence(self) -> ProductionPerformanceEvidence:
        return ProductionPerformanceEvidenceReader(
            ledger=self._ledger, latency_recorder=self._latency
        ).capture()

    async def advance(self, clock: ClockObservation) -> RuntimeOutcome:
        """Advance logical time through the sole World v2 host seam."""
        before = (
            await asyncio.to_thread(self._ledger.project)
            if self._ledger.blocks_event_loop
            else self._ledger.project()
        )
        outcome = await self._turns.advance(clock)
        clock_event_id = f"event:trigger:clock:{clock.tick_id}"
        located = (
            await asyncio.to_thread(self._ledger.lookup_event_commit, clock_event_id)
            if self._ledger.blocks_event_loop
            else self._ledger.lookup_event_commit(clock_event_id)
        )
        if located is None:
            raise RuntimeError("clock outcome has no durable clock event")
        clock_event, _clock_commit = located
        events = self._deferred_replies.clock_events(projection=before, clock_event=clock_event)
        if events:
            existing = (
                await asyncio.to_thread(self._ledger.lookup_event_commit, events[0].event_id)
                if self._ledger.blocks_event_loop
                else self._ledger.lookup_event_commit(events[0].event_id)
            )
            if existing is None:
                current = (
                    await asyncio.to_thread(self._ledger.project)
                    if self._ledger.blocks_event_loop
                    else self._ledger.project()
                )
                kwargs = dict(events=events, expected_cursor=ProjectionCursor(
                    world_revision=current.world_revision,
                    deliberation_revision=current.deliberation_revision,
                    ledger_sequence=current.ledger_sequence,
                ), commit_id="reply-later:clock:" + clock.tick_id)
                if self._ledger.blocks_event_loop:
                    await asyncio.to_thread(self._ledger.commit_at_cursor, **kwargs)
                else:
                    self._ledger.commit_at_cursor(**kwargs)
        return outcome

    async def tick(
        self,
        *,
        tick_id: str,
        logical_time_from: datetime,
        logical_time_to: datetime,
        observed_at: datetime,
        trace_id: str,
        causation_id: str,
        correlation_id: str,
        reason: str,
        policy_version: str | None = None,
        policy_digest: str | None = None,
    ) -> RuntimeOutcome:
        """Create a validated clock command without exposing World v2 schema internals."""

        outcome = await self.advance(
            ClockObservation(
                schema_version="world-v2.1",
                tick_id=tick_id,
                world_id=self._ledger.world_id,
                logical_time=logical_time_to,
                created_at=observed_at,
                trace_id=trace_id,
                causation_id=causation_id,
                correlation_id=correlation_id,
                logical_time_from=logical_time_from,
                logical_time_to=logical_time_to,
                reason=reason,
                policy_version=policy_version,
                policy_digest=policy_digest,
            )
        )
        if self._life_ecology is not None:
            await self.advance_life_ecology_once(
                wake_event_ref=f"event:trigger:clock:{tick_id}",
                trace_id=trace_id,
                correlation_id=correlation_id,
            )
        return outcome

    async def receipt(
        self,
        *,
        source: str,
        source_event_id: str,
        action_id: str,
        idempotency_key: str,
        status: Literal[
            "provider_accepted", "delivered", "failed", "cancelled", "expired", "unknown"
        ],
        provider_ref: str,
        observed_at: datetime,
        trace_id: str,
        causation_id: str,
        correlation_id: str,
        raw_payload_hash: str,
        kind: Literal[
            "provider_ack",
            "execution_receipt",
            "tool_result",
            "media_result",
            "reconciliation_result",
        ] = "execution_receipt",
        artifact_refs: tuple[str, ...] = (),
        cost_actual: int = 0,
        error_class: str | None = None,
        retryability: Literal["retryable", "not_retryable", "unknown"] | None = None,
    ) -> RuntimeOutcome:
        """Settle one provider callback without exposing the runtime or ledger.

        ``source + source_event_id`` is the callback's immutable idempotency
        identity.  The host cannot select a world, reducer, or settlement
        handler: all it can supply is the provider evidence it received.
        """

        result = ExternalObservation(
            schema_version="world-v2.1",
            result_id=f"result:{source}:{source_event_id}",
            world_id=self._ledger.world_id,
            logical_time=observed_at,
            created_at=observed_at,
            trace_id=trace_id,
            causation_id=causation_id,
            correlation_id=correlation_id,
            kind=kind,
            source=source,
            source_event_id=source_event_id,
            action_id=action_id,
            idempotency_key=idempotency_key,
            status=status,
            provider_ref=provider_ref,
            artifact_refs=artifact_refs,
            cost_actual=cost_actual,
            observed_at=observed_at,
            error_class=error_class,
            retryability=retryability,
            raw_payload_hash=raw_payload_hash,
        )
        outcome = await self._turns.settle(result)
        if status in {"delivered", "failed", "cancelled", "expired", "unknown"}:
            if self._ledger.blocks_event_loop:
                await asyncio.to_thread(
                    self._deferred_replies.settle_terminal_action,
                    action_id=action_id, logical_time=observed_at, created_at=observed_at,
                    trace_id=trace_id, causation_id=causation_id, correlation_id=correlation_id,
                )
            else:
                self._deferred_replies.settle_terminal_action(
                    action_id=action_id, logical_time=observed_at, created_at=observed_at,
                    trace_id=trace_id, causation_id=causation_id, correlation_id=correlation_id,
                )
        return outcome

    async def record_outcome_observation(
        self, observation: OutcomeObservation
    ) -> RuntimeOutcome:
        """Record a verified world observation without exposing the ledger."""

        return await self._turns.record_outcome_observation(observation)

    def project(self, viewer: ProjectionRequest) -> WorldProjection:
        """Expose the capability-authorized read seam without a ledger handle."""

        return self._turns.project(viewer)

    async def commit_occurrence(self, request: OccurrenceContentCommitRequest) -> CommitResult:
        """Author a new occurrence through the sidecar-first production seam.

        Hosts cannot submit a semantic candidate matrix directly to the ledger:
        this method requires complete candidate text so its immutable hash and
        descriptor are frozen with the occurrence in one ledger commit.
        """

        if self._ledger.blocks_event_loop:
            return await asyncio.to_thread(self._occurrence_content.commit, request)
        return self._occurrence_content.commit(request)

    async def plan_activity(
        self,
        command: ActivityPlanCommand,
        *,
        logical_time: datetime,
        created_at: datetime,
        trace_id: str,
        causation_id: str,
        correlation_id: str,
    ) -> CommitResult:
        """Create one source-bound Activity plan through the public v2 seam."""

        kwargs = dict(
            command=command,
            logical_time=logical_time,
            created_at=created_at,
            trace_id=trace_id,
            causation_id=causation_id,
            correlation_id=correlation_id,
        )
        if self._ledger.blocks_event_loop:
            return await asyncio.to_thread(self._activity_plans.plan, **kwargs)
        return self._activity_plans.plan(**kwargs)

    async def transition_activity(
        self,
        command: ActivityPlanTransitionCommand,
        *,
        logical_time: datetime,
        created_at: datetime,
        trace_id: str,
        causation_id: str,
        correlation_id: str,
    ) -> CommitResult:
        """Move/cancel an ActivityPlan without giving a host ledger access."""
        kwargs = dict(command=command, logical_time=logical_time, created_at=created_at,
                      trace_id=trace_id, causation_id=causation_id, correlation_id=correlation_id)
        if self._ledger.blocks_event_loop:
            return await asyncio.to_thread(self._activity_plans.transition, **kwargs)
        return self._activity_plans.transition(**kwargs)

    async def declare_image_evidence(
        self,
        command: ImageEvidenceDeclarationCommand,
        *,
        logical_time: datetime,
        created_at: datetime,
        trace_id: str,
        correlation_id: str,
    ) -> CommitResult:
        """Append a source-bound visual declaration through the World seam.

        The command has no source hash, event type, or privacy field: the
        runtime derives all three from the pinned life projection before it
        writes the declaration.
        """

        kwargs = dict(
            command=command,
            logical_time=logical_time,
            created_at=created_at,
            actor=self._event_ecology_worker_actor,
            trace_id=trace_id,
            correlation_id=correlation_id,
        )
        if self._ledger.blocks_event_loop:
            return await asyncio.to_thread(self._image_evidence.declare, **kwargs)
        return self._image_evidence.declare(**kwargs)

    async def record_visual_fact(
        self,
        command: VisualFactRecordCommand,
        *,
        logical_time: datetime,
        created_at: datetime,
        trace_id: str,
        correlation_id: str,
    ) -> CommitResult:
        """Record one trusted object/food slice without exposing source hashes.

        The exact JSON is persisted in the immutable media sidecar before its
        descriptor is ledger-visible.  Later media code resolves that same
        ref/hash, rather than interpreting a fact value or asking a model to
        fill in visual details.
        """

        kwargs = dict(
            command=command,
            logical_time=logical_time,
            created_at=created_at,
            actor=self._event_ecology_worker_actor,
            trace_id=trace_id,
            correlation_id=correlation_id,
        )
        if self._ledger.blocks_event_loop:
            return await asyncio.to_thread(self._visual_facts.record, **kwargs)
        return self._visual_facts.record(**kwargs)

    async def declare_recipient_scoped_image_evidence(
        self,
        command: RecipientScopedImageEvidenceDeclarationCommand,
        *,
        logical_time: datetime,
        created_at: datetime,
        trace_id: str,
        correlation_id: str,
    ) -> CommitResult:
        """Write P3 visual evidence without exposing source bytes to the host.

        The separate method keeps P0/P2 public evidence unable to acquire a
        recipient or private visibility merely through a new optional field.
        """

        kwargs = dict(
            command=command,
            logical_time=logical_time,
            created_at=created_at,
            actor=self._event_ecology_worker_actor,
            trace_id=trace_id,
            correlation_id=correlation_id,
        )
        if self._ledger.blocks_event_loop:
            return await asyncio.to_thread(self._recipient_scoped_image_evidence.declare, **kwargs)
        return self._recipient_scoped_image_evidence.declare(**kwargs)

    async def record_appearance_state(
        self,
        command: AppearanceStateRecordCommand,
        *,
        logical_time: datetime,
        created_at: datetime,
        trace_id: str,
        correlation_id: str,
    ) -> CommitResult:
        """Append one sparse, source-bound visible state through the host seam.

        Hosts may identify a source event and visible attributes, but cannot
        supply its payload hash, source type, visibility ceiling or revision;
        the appearance runtime resolves each of those from the ledger.
        """

        kwargs = dict(
            command=command,
            logical_time=logical_time,
            created_at=created_at,
            actor=self._event_ecology_worker_actor,
            trace_id=trace_id,
            correlation_id=correlation_id,
        )
        if self._ledger.blocks_event_loop:
            return await asyncio.to_thread(self._appearance_states.record, **kwargs)
        return self._appearance_states.record(**kwargs)

    async def record_visible_physical_state(
        self,
        command: VisiblePhysicalStateRecordCommand,
        *,
        logical_time: datetime,
        created_at: datetime,
        trace_id: str,
        correlation_id: str,
    ) -> CommitResult:
        """Record short-lived visible evidence with ledger-derived source coordinates.

        The host can name a committed source and structured positive/negative
        cues only.  It cannot forge source bytes, source privacy or revisions;
        expiry is bounded and defaulted by the physical-state runtime.
        """

        kwargs = dict(
            command=command,
            logical_time=logical_time,
            created_at=created_at,
            actor=self._event_ecology_worker_actor,
            trace_id=trace_id,
            correlation_id=correlation_id,
        )
        if self._ledger.blocks_event_loop:
            return await asyncio.to_thread(self._visible_physical_states.record, **kwargs)
        return self._visible_physical_states.record(**kwargs)

    async def replace_activity(
        self,
        command: ActivityPlanCommand,
        *,
        predecessor_plan_id: str,
        logical_time: datetime,
        created_at: datetime,
        trace_id: str,
        causation_id: str,
        correlation_id: str,
    ) -> CommitResult:
        """Atomically substitute an unfinished plan; it never asserts completion."""
        kwargs = dict(command=command, predecessor_plan_id=predecessor_plan_id,
                      logical_time=logical_time, created_at=created_at, trace_id=trace_id,
                      causation_id=causation_id, correlation_id=correlation_id)
        if self._ledger.blocks_event_loop:
            return await asyncio.to_thread(self._activity_plans.replace, **kwargs)
        return self._activity_plans.replace(**kwargs)

    async def drain_actions_once(self) -> ActionPumpResult | None:
        result = await self._turns.drain_actions_once()
        await self._join_deferred_terminal_action(result)
        return result

    async def drain_action(self, action_id: str) -> ActionPumpResult | None:
        """Drain an ingress-bound Action without globally scheduling siblings."""

        result = await self._turns.drain_action(action_id)
        await self._join_deferred_terminal_action(result)
        return result

    async def _join_deferred_terminal_action(
        self, result: ActionPumpResult | None
    ) -> None:
        """Join a pump-written terminal receipt to its reply-later Commitment.

        The Action pump owns dispatch and receipt settlement.  This application
        seam owns the platform-neutral continuation join so hosts cannot forget
        it and a restart cannot strand a delivered promise as still due.
        """

        if result is None or result.action_id is None:
            return
        projection = (
            await asyncio.to_thread(self._ledger.project)
            if self._ledger.blocks_event_loop
            else self._ledger.project()
        )
        action = next(
            (item for item in projection.actions if item.action_id == result.action_id), None
        )
        if action is None or action.state not in {
            "delivered", "failed", "cancelled", "expired", "unknown"
        }:
            return
        receipt = next(
            (
                item
                for item in reversed(projection.execution_receipts)
                if item.action_id == action.action_id and item.is_terminal
            ),
            None,
        )
        if receipt is None:
            return
        logical_time = projection.logical_time or receipt.received_at
        kwargs = dict(
            action_id=action.action_id,
            logical_time=logical_time,
            created_at=receipt.received_at,
            trace_id=action.trace_id,
            causation_id=receipt.receipt_id,
            correlation_id=action.correlation_id,
        )
        if self._ledger.blocks_event_loop:
            await asyncio.to_thread(self._deferred_replies.settle_terminal_action, **kwargs)
        else:
            self._deferred_replies.settle_terminal_action(**kwargs)

    async def drain_media_results_once(self, *, logical_time: datetime) -> str | None:
        """Materialize one verified Media v2 provider result sidecar.

        This is intentionally separate from Action dispatch: the ActionPump
        first records its terminal receipt, then this recovery-safe worker
        joins only the result bytes that hash-bind to that receipt.  It never
        sends an image and cannot produce a delivery event.
        """

        if self._media_execution_worker is None:
            return None
        return await self._media_execution_worker.drain_once(logical_time=logical_time)

    async def drain_media_continuation_once(
        self, *, logical_time: datetime, trace_id: str, correlation_id: str,
    ) -> str | None:
        if self._media_continuation_worker is None:
            return None
        return self._media_continuation_worker.drain_once(
            logical_time=logical_time, trace_id=trace_id, correlation_id=correlation_id,
        )

    async def drain_media_planning_once(self) -> MediaPlanningRunResult:
        """Advance one already-frozen Media v2 planning Action.

        This is scheduler-only: it does not select a candidate or construct a
        snapshot.  A missing composition-owned planner is visible as an
        ``unavailable`` result and cannot fall back to the legacy image path.
        """

        return await self._media_planning_worker.drain_once()

    async def drain_media_ecology_once(
        self, *, wake_event_ref: str, logical_time: datetime, trace_id: str,
        correlation_id: str,
    ) -> EcologyDrainResult | None:
        """Freeze source-bound life-media opportunities after a durable wake.

        This is intentionally a scheduler-only seam.  It accepts an exact
        committed life/clock event ref, not an inbound message or a free-form
        media request.  The ecology may open preview opportunities only; it
        neither chooses one for planning nor authorizes, renders, or sends it.
        If the composition did not explicitly inject an ecology policy, it is
        unavailable rather than falling back to any legacy image mechanism.
        """

        if self._media_ecology is None:
            return None
        kwargs = dict(
            wake_event_ref=wake_event_ref,
            logical_time=logical_time,
            actor=self._event_ecology_worker_actor,
            trace_id=trace_id,
            correlation_id=correlation_id,
        )
        if self._ledger.blocks_event_loop:
            return await asyncio.to_thread(self._media_ecology.drain_once, **kwargs)
        return self._media_ecology.drain_once(**kwargs)

    async def drain_character_media_candidates_once(
        self, *, wake_event_ref: str, logical_time: datetime, trace_id: str,
        correlation_id: str,
    ) -> tuple[str, ...]:
        """Open source-bound character-media candidates after a declaration.

        This is separate from the life-share ecology because it has a distinct
        proof matrix (presence and capture capability).  It accepts the
        isolated recipient-scoped P3 declaration wire as well as P2; it still
        only opens candidates, leaving selection, Acceptance, planning and
        delivery to separate scheduler seams.
        """

        kwargs = dict(
            wake_event_ref=wake_event_ref,
            logical_time=logical_time,
            actor=self._event_ecology_worker_actor,
            trace_id=trace_id,
            correlation_id=correlation_id,
        )
        if self._ledger.blocks_event_loop:
            return await asyncio.to_thread(self._character_media_candidates.open_once, **kwargs)
        return self._character_media_candidates.open_once(**kwargs)

    async def drain_media_selection_once(
        self, *, logical_time: datetime, trace_id: str, correlation_id: str,
    ) -> MediaSelectionRunResult | None:
        """Ask the bounded preview selector whether an available candidate matters.

        This is intentionally a proposal-only scheduler seam.  A model can
        select one opaque candidate token or decline; it cannot authorize a
        preview, reserve budget, construct an evidence snapshot, render, or
        deliver media.  Those consequences remain behind the separate
        acceptance runtime and its capability-bound grant checks.
        """

        if self._media_selection_worker is None:
            return None
        return await self._media_selection_worker.select_once(
            logical_time=logical_time,
            actor=self._media_selection_worker_actor,
            trace_id=trace_id,
            correlation_id=correlation_id,
        )

    async def accept_media_selection_once(
        self, *, proposal_event_ref: str, logical_time: datetime, trace_id: str, correlation_id: str,
    ) -> CommitResult | None:
        """Accept one pinned ordinary-preview proposal under explicit grant/budget config."""

        runtime, config = self._media_selection_acceptance, self._media_selection_acceptance_config
        if runtime is None or config is None:
            return None
        kwargs = dict(
            runtime=runtime,
            config=config,
            proposal_event_ref=proposal_event_ref,
            logical_time=logical_time,
            trace_id=trace_id,
            correlation_id=correlation_id,
        )
        if self._ledger.blocks_event_loop:
            return await asyncio.to_thread(self._accept_media_selection, **kwargs)
        return self._accept_media_selection(**kwargs)

    async def _select_media_preview_candidate(
        self, *, logical_time: datetime, trace_id: str, correlation_id: str,
    ) -> MediaSelectionRunResult:
        """Adapt the configured selector to the conductor's small Interface."""

        result = await self.drain_media_selection_once(
            logical_time=logical_time, trace_id=trace_id, correlation_id=correlation_id,
        )
        if result is None:
            # The conductor is composed only when a selector exists.  This is
            # an invariant breach rather than a reason to quietly skip a
            # candidate or use a legacy image path.
            raise RuntimeError("media preview conductor lost its selection worker")
        return result

    async def _accept_media_preview_selection(
        self, *, proposal_event_ref: str, logical_time: datetime, trace_id: str,
        correlation_id: str,
    ) -> MediaPreviewAcceptanceOutcome | None:
        """Translate Acceptance's durable batch into conductor semantics."""

        commit = await self.accept_media_selection_once(
            proposal_event_ref=proposal_event_ref,
            logical_time=logical_time,
            trace_id=trace_id,
            correlation_id=correlation_id,
        )
        if commit is None:
            return None
        event_types: list[str] = []
        for event_id in commit.event_ids:
            located = (
                await asyncio.to_thread(self._ledger.lookup_event_commit, event_id)
                if self._ledger.blocks_event_loop
                else self._ledger.lookup_event_commit(event_id)
            )
            if located is None:
                raise RuntimeError("media preview Acceptance event is unavailable")
            event_types.append(located[0].event_type)
        if event_types == ["PhotoCandidateUnrenderable"]:
            disposition = "not_renderable"
        elif event_types == [
            "AcceptanceRecorded", "MediaOpportunityFrozen", "BudgetReserved", "ActionAuthorized",
        ]:
            disposition = "planning_authorized"
        else:
            raise RuntimeError("media preview Acceptance produced an unknown event batch")
        return MediaPreviewAcceptanceOutcome(
            disposition=disposition, event_ids=commit.event_ids,
        )

    async def drain_media_preview_once(
        self, *, trace_id: str, correlation_id: str,
    ) -> MediaPreviewConductorResult:
        """Advance the bounded candidate → preview-plan prefix once.

        This deep scheduler seam is deliberately unavailable unless the
        composition has injected a selector, acceptance grant/budget and a
        durable planner together.  It neither renders nor sends media.
        """

        if self._media_preview_conductor is None:
            return MediaPreviewConductorResult(
                status="blocked", reason_code="media_preview.conductor_unavailable",
            )
        logical_time = await self.current_logical_time()
        if logical_time is None:
            return MediaPreviewConductorResult(
                status="idle", reason_code="media_preview.logical_time_unavailable",
            )
        return await self._media_preview_conductor.advance_once(
            logical_time=logical_time, trace_id=trace_id, correlation_id=correlation_id,
        )

    def _accept_media_selection(
        self,
        *,
        runtime: MediaSelectionAcceptanceRuntime,
        config: MediaSelectionAcceptanceComposition,
        proposal_event_ref: str,
        logical_time: datetime,
        trace_id: str,
        correlation_id: str,
    ) -> CommitResult:
        """Pin and accept inside one synchronous ledger turn.

        The cursor must be derived in the same worker turn as the pin/commit;
        callers may therefore offload the whole method for SQLite without
        exposing a stale cursor window across the event-loop boundary.
        """

        projection = self._ledger.project()
        cursor = ProjectionCursor(
            world_revision=projection.world_revision,
            deliberation_revision=projection.deliberation_revision,
            ledger_sequence=projection.ledger_sequence,
        )
        return runtime.accept(
            handle=runtime.pin_proposal(cursor=cursor, proposal_event_ref=proposal_event_ref),
            actor=config.actor, source="world-v2:media-selection-acceptance", logical_time=logical_time,
            created_at=logical_time, trace_id=trace_id, correlation_id=correlation_id,
            grant=config.grant, account_id=config.account_id, amount_limit=config.amount_limit,
        )

    async def expire_media_candidates_once(
        self, *, logical_time: datetime, trace_id: str, correlation_id: str,
    ) -> MediaCandidateMaintenanceResult:
        """Close only due, still-available candidates at the authoritative clock.

        This maintenance seam cannot select a candidate or authorize media; it
        keeps stale proposal attempts from leaving permanently available
        aggregates in the ledger.
        """

        kwargs = dict(
            logical_time=logical_time,
            actor=self._media_candidate_maintenance_actor,
            trace_id=trace_id,
            correlation_id=correlation_id,
        )
        if self._ledger.blocks_event_loop:
            return await asyncio.to_thread(self._media_candidate_maintenance.expire_once, **kwargs)
        return self._media_candidate_maintenance.expire_once(**kwargs)

    async def advance_life_ecology_once(
        self, *, wake_event_ref: str, trace_id: str, correlation_id: str,
    ) -> LifeEcologyRunResult:
        """Advance the explicit, ledger-backed life ecology after one wake.

        This remains a scheduler-only seam.  It is never called from inbound
        message processing, and production publishes only source-bound media
        candidates from durable world evidence.
        """

        if self._life_ecology is None:
            return LifeEcologyRunResult(
                status="unavailable",
                reason_code="life_ecology.not_configured",
            )
        return await self._life_ecology.advance_once(
            wake_event_ref=wake_event_ref,
            trace_id=trace_id,
            correlation_id=correlation_id,
        )

    def event_ecology_source_taxonomy(self) -> tuple[EcologySourceTaxon, ...]:
        """Expose source richness separately from visual declaration eligibility."""

        if self._media_ecology is None:
            return ()
        return self._media_ecology.discover_source_taxonomy()

    async def approve_media_automatic_delivery(
        self, *, approval: MediaAutomaticDeliveryApproval, trace_id: str,
        correlation_id: str, causation_id: str,
    ) -> MediaAutomaticDeliveryApproval:
        """Record an explicit short-lived operator exception to preview-only media."""

        kwargs = dict(approval=approval, trace_id=trace_id, correlation_id=correlation_id, causation_id=causation_id)
        if self._ledger.blocks_event_loop:
            return await asyncio.to_thread(self._media_delivery.approve, **kwargs)
        return self._media_delivery.approve(**kwargs)

    async def authorize_media_delivery(
        self, *, approval_id: str, approval_revision: int, actor: str, target: str,
        account_id: str, amount_limit: int, logical_time: datetime, trace_id: str,
        correlation_id: str,
    ):
        """Authorize one approved immutable artifact; no preview auto-sends."""

        kwargs = dict(approval_id=approval_id, approval_revision=approval_revision, actor=actor,
                      target=target, account_id=account_id, amount_limit=amount_limit,
                      logical_time=logical_time, trace_id=trace_id, correlation_id=correlation_id)
        if self._ledger.blocks_event_loop:
            return await asyncio.to_thread(self._media_delivery.authorize_delivery, **kwargs)
        return self._media_delivery.authorize_delivery(**kwargs)

    async def deliver_approved_media_once(
        self, *, approval_id: str, approval_revision: int, actor: str, target: str,
        account_id: str, amount_limit: int, logical_time: datetime, trace_id: str,
        correlation_id: str,
    ) -> ActionPumpResult | None:
        """Authorize one approved artifact, then drain only its Action.

        This is the production seam for the full media hand-off.  The
        operator approval and recipient are checked before the Action exists;
        the targeted pump then records the provider receipt and settlement is
        the only place that can derive ``MediaDeliveryShared``.  ``None`` is
        an explicit unavailable-provider result, never a simulated delivery.
        """

        action = await self.authorize_media_delivery(
            approval_id=approval_id,
            approval_revision=approval_revision,
            actor=actor,
            target=target,
            account_id=account_id,
            amount_limit=amount_limit,
            logical_time=logical_time,
            trace_id=trace_id,
            correlation_id=correlation_id,
        )
        return await self.drain_action(action.action_id)

    def media_preview_operator(self, *, preview_dir: Path | None = None):
        """Return the read-only media observation service.

        The service lists generated/delivered media and materializes preview
        PNGs for human viewing.  It has no approval or veto verb: delivery is
        decided by the world's own selection/acceptance chain plus the
        composed auto-delivery guardrails.
        """

        from .media_preview_operator import MediaPreviewOperatorService

        if preview_dir is None:
            return MediaPreviewOperatorService(
                ledger=self._ledger,
                sidecar=self._media_payload_store,
            )
        return MediaPreviewOperatorService(
            ledger=self._ledger,
            sidecar=self._media_payload_store,
            preview_dir=preview_dir,
        )

    async def drain_media_auto_delivery_once(
        self, *, trace_id: str, correlation_id: str
    ) -> MediaAutoDeliveryRunResult | None:
        """Advance at most one inspection-passed preview into delivery.

        This is the world-owned delivery policy seam: the send decision was
        already made by bounded selection and Acceptance; this drain applies
        only the deployment's operational guardrails (daily cap, minimum gap)
        and the existing approval-gated delivery Action.  ``None`` means the
        composition did not install an auto-delivery policy.
        """

        if self._media_auto_delivery is None:
            return None
        return await self._media_auto_delivery.drain_once(
            trace_id=trace_id, correlation_id=correlation_id
        )

    async def drain_background_once(
        self,
    ) -> (
        AppraisalTriggerRunResult
        | OutcomeTriggerRunResult
        | InteractionBidTriggerRunResult
        | AffectTriggerRunResult
        | FactTriggerRunResult
        | MemoryWithdrawalReviewRunResult
        | ExpressionReconsiderationRunResult
        | AfterthoughtRunResult
        | SocialActionRunResult
        | None
    ):
        """Run one separately scheduled mental-state or memory work unit."""

        return await self._turns.drain_background_once()

    async def current_logical_time(self) -> datetime | None:
        """Return the current durable logical clock through the application seam.

        Platform schedulers need the previous committed logical timestamp to
        create a valid next tick after a process restart.  Returning this one
        scalar does not expose a ledger writer or a projection capability.
        """

        projection = (
            await asyncio.to_thread(self._ledger.project)
            if self._ledger.blocks_event_loop
            else self._ledger.project()
        )
        return projection.logical_time

    async def world_health_diagnostics(self) -> dict[str, object]:
        """Return deterministic read-only liveness evidence for health checks.

        The projection supplies current state; exact committed draw payloads
        are looked up only to distinguish a due spontaneous candidate from an
        already-recorded ``act`` decision.  This seam never deliberates, draws
        randomness, claims work, or appends ledger events.
        """

        projection = (
            await asyncio.to_thread(self._ledger.project)
            if self._ledger.blocks_event_loop
            else self._ledger.project()
        )
        proactive_processes = tuple(
            item
            for item in projection.trigger_processes
            if item.process_kind == "proactive_action_deliberation"
        )
        processed_sources = {
            item.source_evidence_ref
            for item in proactive_processes
            if item.source_evidence_ref is not None
        }
        opportunity_sources = {
            item.settlement_event_ref
            for item in projection.world_occurrences
            if item.status == "settled"
            and item.visibility in {"public", "shareable"}
            and item.settlement_event_ref is not None
        }
        spontaneous_candidate_due = False
        logical_time = projection.logical_time
        if logical_time is not None:
            for thread in projection.threads:
                values = thread.values
                if (
                    values.status == "open"
                    and values.due_window is not None
                    and values.due_window.opens_at
                    <= logical_time
                    < values.due_window.closes_at
                ):
                    transition = next(
                        (
                            item
                            for item in reversed(projection.thread_transitions)
                            if item.thread_id == thread.thread_id
                            and item.entity_revision == thread.entity_revision
                        ),
                        None,
                    )
                    if transition is not None:
                        opportunity_sources.add(transition.accepted_event_ref)

            policy = self._social_initiative_policy
            recent_contact = max(
                (
                    item.logical_time
                    for item in projection.actions
                    if item.kind in {"proactive_message", "followup"}
                    and item.state not in {"failed", "cancelled", "expired"}
                ),
                default=None,
            )
            contact_on_cooldown = recent_contact is not None and (
                logical_time - recent_contact
            ).total_seconds() < policy.contact_cooldown_seconds
            response_gaps: list[tuple[datetime, str]] = []
            if not contact_on_cooldown:
                for manifest in projection.expression_plan_manifests:
                    expectation = manifest.response_expectation
                    if expectation is None or not (
                        expectation.not_before <= logical_time < expectation.expires_at
                    ):
                        continue
                    plan = next(
                        (
                            item
                            for item in projection.expression_plans
                            if item.plan_id == manifest.plan_id
                        ),
                        None,
                    )
                    beat = next(
                        (
                            item
                            for item in manifest.beats
                            if item.beat_id == expectation.source_beat_id
                        ),
                        None,
                    )
                    action = next(
                        (
                            item
                            for item in projection.actions
                            if beat is not None
                            and item.action_id == beat.action.action_id
                        ),
                        None,
                    )
                    accepted = any(
                        item.action_id == action.action_id
                        and item.observed_state in {"provider_accepted", "delivered"}
                        for item in projection.execution_receipts
                    ) if action is not None else False
                    delivery_ready = action is not None and accepted and (
                        action.state == "delivered"
                        if expectation.delivery_requirement == "confirmed_delivered"
                        else action.state in {"provider_accepted", "delivered"}
                    )
                    answered = any(
                        item.world_revision > manifest.recorded_at_world_revision
                        for item in projection.message_observations
                    )
                    if (
                        plan is not None
                        and plan.state in {"authorized", "completed"}
                        and beat is not None
                        and delivery_ready
                        and not answered
                    ):
                        response_gaps.append(
                            (expectation.not_before, manifest.acceptance_event_ref)
                        )
                if response_gaps:
                    opportunity_sources.add(min(response_gaps)[1])
                elif projection.message_observations:
                    latest_message = projection.message_observations[-1]
                    source_ref = next(
                        (
                            item
                            for item in projection.committed_world_event_refs
                            if item.world_revision == latest_message.world_revision
                            and item.event_type == "ObservationRecorded"
                        ),
                        None,
                    )
                    if source_ref is not None:
                        idle_seconds = (
                            logical_time - source_ref.logical_time
                        ).total_seconds()
                        profile = SocialInitiativeContextPolicy(
                            policy=policy
                        ).compile(
                            projection=projection,
                            logical_time=logical_time,
                        )
                        if (
                            profile.not_before_seconds
                            <= idle_seconds
                            < policy.spontaneous_expiry_seconds
                        ):
                            spontaneous_candidate_due = True
                            expected_attempt_id = social_initiative_attempt_id(
                                source_event_ref=source_ref.event_id,
                                profile=profile,
                            )
                            for draw_ref in projection.committed_world_event_refs:
                                if draw_ref.event_type != "RandomDrawRecorded":
                                    continue
                                located = (
                                    await asyncio.to_thread(
                                        self._ledger.lookup_event_commit,
                                        draw_ref.event_id,
                                    )
                                    if self._ledger.blocks_event_loop
                                    else self._ledger.lookup_event_commit(draw_ref.event_id)
                                )
                                if located is None:
                                    continue
                                draw = RandomDrawRecordedPayload.model_validate_json(
                                    located[0].payload_json
                                )
                                if (
                                    draw.attempt_id == expected_attempt_id
                                    and draw.sampler_version == "random-authority.2"
                                    and draw.weight_policy_version
                                    == SocialInitiativeContextPolicy.version
                                    and draw.candidate_refs == ("act", "hold")
                                    and draw.selected_candidate_ref == "act"
                                ):
                                    opportunity_sources.add(source_ref.event_id)
                                    break
            for commitment in projection.commitments:
                values = commitment.values
                if (
                    values.status in {"open", "due"}
                    and values.due_window.opens_at
                    <= logical_time
                    < values.due_window.closes_at
                    and not any(
                        action.action_id
                        == values.fulfillment_contract.expected_action_id
                        for action in projection.actions
                    )
                ):
                    transition = next(
                        (
                            item
                            for item in reversed(projection.commitment_transitions)
                            if item.commitment_id == commitment.commitment_id
                            and item.entity_revision == commitment.entity_revision
                        ),
                        None,
                    )
                    if transition is not None:
                        opportunity_sources.add(transition.accepted_event_ref)

        latest = proactive_processes[-1] if proactive_processes else None
        last_status: str | None = latest.state if latest is not None else None
        last_reason: str | None = None
        if latest is not None and latest.runtime_outcome_ref:
            outcome = latest.runtime_outcome_ref.removeprefix("proactive:")
            status, separator, reason = outcome.partition(":")
            last_status = status.replace("-", "_")
            last_reason = reason if separator else None

        # Registration and proposal/audit records prove infrastructure, not
        # that the character has actually lived through anything.
        lived_world_event_types = frozenset(LIFE_PAYLOAD_MODELS) - {
            "NpcRegistered",
            "ActivityLifecycleProposalRecorded",
            "OutcomeObservationRecorded",
            "OutcomeProposalRecorded",
        }
        life_event_count = sum(
            item.event_type in lived_world_event_types
            for item in projection.committed_world_event_refs
        )
        occurrence_count = len(projection.world_occurrences)
        experience_count = len(projection.experiences)
        plans_by_status = Counter(item.status for item in projection.plans)
        active_plans = tuple(item for item in projection.plans if item.status == "active")
        trigger_counts = Counter(item.process_kind for item in projection.trigger_processes)
        pending_trigger_counts = Counter(
            item.process_kind
            for item in projection.trigger_processes
            if item.state != "terminal"
        )
        def _activity_view(plan) -> dict[str, object]:
            window = plan.scheduled_window
            return {
                "activity_kind": plan.activity_kind,
                "status": plan.status,
                "location_ref": plan.location_ref,
                "participant_refs": list(plan.participant_refs),
                "window_opens_at": (
                    window.opens_at.isoformat() if window is not None else None
                ),
                "window_closes_at": (
                    window.closes_at.isoformat() if window is not None else None
                ),
                "last_transitioned_at": (
                    plan.last_transitioned_at.isoformat()
                    if plan.last_transitioned_at is not None
                    else None
                ),
            }

        def _latest_transitioned(status: str):
            candidates = [
                item
                for item in projection.plans
                if item.status == status and item.last_transitioned_at is not None
            ]
            if not candidates:
                return None
            return max(candidates, key=lambda item: item.last_transitioned_at)

        upcoming_planned = sorted(
            (
                item
                for item in projection.plans
                if item.status == "planned" and item.scheduled_window is not None
            ),
            key=lambda item: item.scheduled_window.opens_at,
        )
        # The viewer-facing calendar shows every accepted plan whose window
        # opens within the next seven days (the future author's horizon),
        # not just the single nearest one.
        calendar_horizon = (
            logical_time + timedelta(days=7) if logical_time is not None else None
        )
        upcoming_calendar = [
            item
            for item in upcoming_planned
            if calendar_horizon is None
            or item.scheduled_window.opens_at <= calendar_horizon
        ]
        # A bounded viewer-facing trace of the lived day: every plan whose
        # window opened in the last 24 hours, terminal or not.  This is what
        # lets the dashboard show "today" honestly instead of only a single
        # latest-completed item.  Selection keeps the newest 16, but the
        # output is chronological so the viewer reads a day, not a stack.
        recent_day_activities = tuple(reversed(sorted(
            (
                item
                for item in projection.plans
                if item.scheduled_window is not None
                and logical_time is not None
                and timedelta(0)
                <= (logical_time - item.scheduled_window.opens_at)
                <= timedelta(hours=24)
            ),
            key=lambda item: item.scheduled_window.opens_at,
            reverse=True,
        )[:16]))
        latest_completed = _latest_transitioned("completed")
        mechanisms = {
            # This is deliberately a read-only projection of what reached the
            # ledger.  It distinguishes "the mechanism has no state yet" from
            # "the mechanism has state but its trigger is still pending".
            "current_situation": {
                "logical_time": (
                    logical_time.isoformat() if logical_time is not None else None
                ),
                "active_activity_count": len(active_plans),
                "active_activity_kinds": sorted(
                    {item.activity_kind for item in active_plans}
                ),
                "planned_activity_count": plans_by_status.get("planned", 0),
                "paused_activity_count": plans_by_status.get("paused", 0),
                # Viewer-facing factual life state: what she is doing right
                # now, what is scheduled next, and what she finished last.
                # These are exact ledger projections, never model guesses.
                "active_activities": [
                    _activity_view(item) for item in active_plans
                ],
                "next_planned_activity": (
                    _activity_view(upcoming_planned[0]) if upcoming_planned else None
                ),
                "upcoming_activities": [
                    _activity_view(item) for item in upcoming_calendar
                ],
                "today_activities": [
                    _activity_view(item) for item in recent_day_activities
                ],
                "last_completed_activity": (
                    _activity_view(latest_completed)
                    if latest_completed is not None
                    else None
                ),
            },
            "life_ecology": {
                "plans_by_status": dict(sorted(plans_by_status.items())),
                "world_occurrence_count": occurrence_count,
                "experience_count": experience_count,
            },
            "affect": {
                "active_episode_count": sum(
                    item.status == "active" for item in projection.affect_episodes
                ),
                "episode_count": len(projection.affect_episodes),
                "appraisal_count": len(projection.appraisals),
            },
            "memory": {
                "fact_count": sum(
                    item.values.status == "active" for item in projection.facts
                ),
                "candidate_count": len(projection.memory_candidates),
                "active_candidate_count": sum(
                    item.values.status == "active"
                    for item in projection.memory_candidates
                ),
            },
            "relationship": {
                "state_count": len(projection.relationship_states),
                "signal_count": len(projection.relationship_signals),
                "adjustment_count": len(projection.relationship_adjustments),
            },
            "npc": {
                "registered_count": len(projection.npcs),
                "world_appraisal_count": trigger_counts.get("npc_world_appraisal", 0),
            },
            "triggers": {
                "by_kind": dict(sorted(trigger_counts.items())),
                "pending_by_kind": dict(sorted(pending_trigger_counts.items())),
            },
        }
        # Per-item viewer detail (bounded lists, clipped text).  The fact
        # recall and content-store reads are synchronous SQLite work, so they
        # share the projection's off-loop discipline.
        details = (
            await asyncio.to_thread(self._mechanism_detail_sections, projection)
            if self._ledger.blocks_event_loop
            else self._mechanism_detail_sections(projection)
        )
        mechanisms["affect"].update(details["affect"])
        mechanisms["memory"].update(details["memory"])
        mechanisms["relationship"].update(details["relationship"])
        mechanisms["life_ecology"].update(details["life_ecology"])
        mechanisms["inner"] = details["inner"]
        return {
            "initiative_last_status": last_status,
            "initiative_last_reason": last_reason,
            "pending_proactive_opportunity_count": len(
                opportunity_sources - processed_sources
            ),
            "pending_proactive_process_count": sum(
                item.state != "terminal" for item in proactive_processes
            ),
            "pending_proactive_action_count": sum(
                item.kind in {"proactive_message", "followup"}
                for item in projection.pending_actions
            ),
            "spontaneous_candidate_due": spontaneous_candidate_due,
            "life_event_count": life_event_count,
            "occurrence_count": occurrence_count,
            "experience_count": experience_count,
            "starved": not (life_event_count or occurrence_count or experience_count),
            "mechanisms": mechanisms,
        }

    def _mechanism_detail_sections(self, projection) -> dict[str, dict[str, object]]:
        """Compile bounded per-item mechanism detail for the viewer dashboard.

        Everything here is a read of committed authority (projection entities,
        the fact-recall closure, and immutable content-store bytes).  Nothing
        deliberates, draws, or writes; texts are clipped so the health payload
        stays small.
        """

        def _clip(text: str, limit: int = 80) -> str:
            text = text.strip()
            return text if len(text) <= limit else text[: limit - 1] + "…"

        def _iso(value) -> str | None:
            return value.isoformat() if isinstance(value, datetime) else None

        logical_time = projection.logical_time

        episodes = [
            {
                "status": episode.status,
                "opened_at": _iso(episode.opened_at),
                "updated_at": _iso(episode.updated_at),
                "components": [
                    {
                        "dimension": component.dimension,
                        "label": MOOD_LABELS.get(
                            component.dimension, component.dimension
                        ),
                        "intensity_bp": component.intensity_bp,
                        "anchor_intensity_bp": component.decay_anchor_intensity_bp,
                        "decaying": component.intensity_bp
                        < component.decay_anchor_intensity_bp,
                    }
                    for component in episode.components
                ],
            }
            for episode in sorted(
                projection.affect_episodes,
                key=lambda item: item.updated_at,
                reverse=True,
            )[:8]
        ]
        phase_readings = (
            change_phase_readings(
                tuple(projection.affect_episodes), logical_time=logical_time
            )
            if isinstance(logical_time, datetime)
            else ()
        )
        change_phases = [
            {
                "dimension": reading.dimension,
                "label": MOOD_LABELS.get(reading.dimension, reading.dimension),
                "phase": reading.phase,
                "intensity_bp": reading.intensity_bp,
                "prose": change_phase_reading_prose(reading),
            }
            for reading in phase_readings[:8]
        ]

        active_facts = tuple(
            sorted(
                (
                    item
                    for item in projection.facts
                    if item.values.status == "active"
                ),
                key=lambda item: item.updated_at,
                reverse=True,
            )[:8]
        )
        recalled = {
            item.fact_id: item
            for item in fact_recall_items(
                ledger=self._ledger, projection=projection, facts=active_facts
            )
        }
        facts = [
            {
                "predicate_code": fact.values.predicate_code,
                "value_excerpt": (
                    _clip(recalled[fact.fact_id].source_excerpt)
                    if fact.fact_id in recalled
                    else None
                ),
                "confidence_bp": fact.values.confidence_bp,
                "committed_at": _iso(fact.committed_at),
            }
            for fact in active_facts
        ]

        memory_items = []
        for candidate in sorted(
            (
                item
                for item in projection.memory_candidates
                if item.values.status in {"active", "pending"}
            ),
            key=lambda item: item.updated_at,
            reverse=True,
        )[:8]:
            stored = self._life_content_store.read_exact(
                content_ref=candidate.values.summary_ref
            )
            salience = candidate.values.salience
            highlights = sorted(
                (
                    (name.removesuffix("_bp"), getattr(salience, name))
                    for name in (
                        "autobiographical_relevance_bp",
                        "relationship_relevance_bp",
                        "emotional_residue_bp",
                        "unfinished_business_bp",
                        "recurrence_bp",
                        "novelty_bp",
                        "future_utility_bp",
                        "world_continuity_bp",
                    )
                ),
                key=lambda item: item[1],
                reverse=True,
            )[:2]
            memory_items.append({
                "cue_kind": candidate.values.cue_kind,
                "status": candidate.values.status,
                "source_kinds": sorted({
                    binding.source_kind
                    for binding in candidate.values.source_bindings
                }),
                "summary_excerpt": _clip(stored.text) if stored is not None else None,
                "salience_highlights": [
                    {"dimension": name, "bp": value} for name, value in highlights
                ],
                "retrieval_strength_bp": candidate.values.retrieval_strength_bp,
                "updated_at": _iso(candidate.updated_at),
            })

        hypothesis_meanings = {
            (appraisal.appraisal_id, hypothesis.hypothesis_id): hypothesis.meaning
            for appraisal in projection.appraisals
            for hypothesis in appraisal.hypotheses
        }
        impressions = []
        for impression in sorted(
            (
                item
                for item in projection.private_impressions
                if item.status == "active"
            ),
            key=lambda item: item.last_supported,
            reverse=True,
        )[:8]:
            meanings = []
            for ref in impression.interpretation_refs:
                parts = ref.split(":", 2)
                if len(parts) == 3 and parts[0] == "appraisal":
                    meaning = hypothesis_meanings.get((parts[1], parts[2]))
                    if meaning is not None:
                        meanings.append(meaning)
            impressions.append({
                "subject_ref": impression.subject_ref,
                "meanings": meanings,
                "confidence_bp": impression.confidence_bp,
                "first_seen": _iso(impression.first_seen),
                "expiry_condition": impression.expiry_condition,
            })

        aspirations = [
            {
                "text": _clip(item.text),
                "status": item.status,
                "planted_at": _iso(item.planted_at),
                "reinforcement_count": item.reinforcement_count,
            }
            for item in sorted(
                projection.aspirations,
                key=lambda item: item.planted_at,
                reverse=True,
            )[:8]
        ]

        user_state = None
        if projection.relationship_states:
            latest = max(
                projection.relationship_states,
                key=lambda item: (
                    item.last_adjusted_at is not None,
                    item.last_adjusted_at or datetime.min.replace(tzinfo=UTC),
                    item.entity_revision,
                ),
            )
            user_state = {
                "subject_ref": latest.subject_ref,
                "stage": latest.stage,
                "temperature": latest.temperature,
                "variables": latest.variables.model_dump(mode="json"),
                "last_adjusted_at": _iso(latest.last_adjusted_at),
            }
        npc_names = {
            f"npc:{npc.npc_id}": npc.npc_id for npc in projection.npcs
        }
        npc_states = [
            {
                "npc_ref": reading.npc_ref,
                "npc_id": npc_names.get(reading.npc_ref, reading.npc_ref),
                "closeness_bp": reading.closeness_bp,
                "familiarity_bp": reading.familiarity_bp,
                "friction_bp": reading.friction_bp,
                "settled_shared_count": reading.settled_shared_count,
                "last_shared_at": _iso(reading.last_shared_at),
            }
            for reading in npc_relationship_readings(projection)[:8]
        ]

        recent_experiences = []
        for experience in sorted(
            projection.experiences,
            key=lambda item: (
                getattr(getattr(item, "values", None), "occurred_to", None)
                or getattr(item, "occurred_to", None)
            ),
            reverse=True,
        )[:8]:
            values = getattr(experience, "values", None)
            summary_ref = (
                values.summary_ref if values is not None
                else getattr(experience, "summary_ref", None)
            )
            occurred_to = (
                values.occurred_to if values is not None
                else getattr(experience, "occurred_to", None)
            )
            stored = (
                self._life_content_store.read_exact(content_ref=summary_ref)
                if summary_ref
                else None
            )
            recent_experiences.append({
                "occurred_to": _iso(occurred_to),
                "summary_excerpt": _clip(stored.text) if stored is not None else None,
            })
        recent_experiences.reverse()

        return {
            "affect": {"episodes": episodes, "change_phases": change_phases},
            "memory": {"facts": facts, "candidates": memory_items},
            "relationship": {"user_state": user_state, "npc_states": npc_states},
            "life_ecology": {"recent_experiences": recent_experiences},
            "inner": {"impressions": impressions, "aspirations": aspirations},
        }

    async def maintain_wal_once(self) -> SQLiteWalMaintenanceResult:
        """Run one bounded SQLite WAL maintenance pass off the event loop.

        This is scheduler upkeep only.  It never participates in an inbound
        reply and does not mutate World authority; passive checkpointing merely
        compacts already-committed WAL frames.
        """

        return await asyncio.to_thread(self._ledger.maintain_wal_if_needed)

    def export_replay_evidence(self) -> ReplayEvidence:
        """Export a cursor-consistent, read-only replay snapshot for evaluation.

        Hosts and offline scenario runners need evidence, not ledger mutation
        access.  Keeping this operation on the application seam preserves the
        invariant that platform-facing code never writes through the ledger.
        """

        return self._ledger.export_replay_evidence()

    def close(self) -> None:
        self._life_content_store.close()
        self._expression_payload_store.close()
        self._media_payload_store.close()
        self._ledger.close()


def build_sqlite_world_v2_turn_application(
    *,
    path: str | Path,
    config: WorldV2TurnApplicationConfig,
    identities: InboundIdentityResolver,
    router: ModelRouterAdapter,
    main_model: DeliberationModelAdapter,
    quick_recovery: QuickRecoveryAdapter,
    transport: PlatformTransport,
    media_transport: MediaProviderTransport | None = None,
    media_planner: MediaPlanner | None = None,
    legacy_event_media_planner: event_media.MediaPlanner | None = None,
    event_media_result_store: EventMediaPlanningResultStore | None = None,
    advisory_compiler: AdvisoryCompiler | None = None,
    appraisal_model: DeliberationModelAdapter | None = None,
    affect_model: DeliberationModelAdapter | None = None,
    relationship_model: DeliberationModelAdapter | None = None,
    outcome_model: DeliberationModelAdapter | None = None,
    outcome_draft_model: OutcomeSelectionModel | None = None,
    interaction_bid_model: DeliberationModelAdapter | None = None,
    fact_model: FactDraftChatModel | None = None,
    private_impression_model: PrivateImpressionChatModel | None = None,
    memory_model: FactMemoryDraftChatModel | None = None,
    activity_lifecycle_model: ActivityLifecycleDraftModel | None = None,
    open_world_event_model: OpenWorldEventModel | None = None,
    media_selection_model: MediaSelectionDraftModel | None = None,
    read_only_tool_model: DeliberationModelAdapter | None = None,
    read_only_tool_transport: ReadOnlyToolTransport | None = None,
    perception_model: DeliberationModelAdapter | None = None,
    perception_input_source: PerceptionInputSource | None = None,
    perception_transport: PerceptionTransport | None = None,
    proactive_model: ChatCompletionModel | None = None,
    proactive_identity_frame: CompanionIdentityFrame | None = None,
    expression_reconsideration_reviewer: ExpressionReconsiderationReviewer | None = None,
    quick_reaction_model: ChatCompletionModel | None = None,
    now: datetime,
    projection_authority: ProjectionAuthority | None = None,
    latency_recorder: ProductionLatencyRecorder | None = None,
) -> WorldV2TurnApplication:
    """Build one durable v2 chat lane without importing the legacy application.

    Bootstrap is idempotent and configures the sole ledger-owned chat budget
    before any message can be ingested.  The platform receives only immutable
    dispatch requests; it never receives a runtime or ledger writer.
    """

    if media_planner is not None and legacy_event_media_planner is not None:
        raise ValueError("inject either a World v2 media planner or legacy event-media planner, not both")
    if config.media_continuation is not None and media_transport is None:
        raise ValueError("media continuation composition requires durable media transport")
    if config.media_auto_delivery is not None and config.media_continuation is None:
        raise ValueError("media auto-delivery requires the render/inspection continuation")
    if outcome_model is not None and outcome_draft_model is not None:
        raise ValueError("inject either an outcome proposal adapter or an outcome draft model, not both")
    build_started = time.perf_counter()
    issuer = AcceptedLedgerBatchIssuer()
    latency = latency_recorder or ProductionLatencyRecorder()
    ledger = SQLiteWorldLedger(
        path=path,
        world_id=config.world_id,
        accepted_batch_issuer=issuer,
        latency_recorder=latency,
    )
    _LOG.warning(
        "world v2 application ledger ready world=%s duration_ms=%.1f",
        config.world_id,
        (time.perf_counter() - build_started) * 1000,
    )
    life_content_store = SQLiteImmutableLifeContentStore(path=str(path), world_id=config.world_id)
    expression_payload_store = SQLiteImmutableExpressionPayloadStore(path=str(path), world_id=config.world_id)
    media_payload_store = SQLiteImmutableMediaPayloadStore(path=str(path), world_id=config.world_id)
    _LOG.warning(
        "world v2 application sidecars ready world=%s duration_ms=%.1f",
        config.world_id,
        (time.perf_counter() - build_started) * 1000,
    )
    try:
        occurrence_content = OccurrenceContentCoordinator(
            ledger=ledger, store=life_content_store
        )
        tool_requested = read_only_tool_model is not None or read_only_tool_transport is not None
        if (read_only_tool_model is None) != (read_only_tool_transport is None):
            raise ValueError("read-only tool model and transport must be explicitly injected together")
        if tool_requested and config.tool_budget_limit <= 0:
            raise ValueError("injected read-only tool lane needs a positive deployment budget")
        perception_dependencies = (
            perception_model,
            perception_input_source,
            perception_transport,
        )
        perception_requested = any(item is not None for item in perception_dependencies)
        if perception_requested and not all(item is not None for item in perception_dependencies):
            raise ValueError(
                "perception model, durable input source and lookup-capable transport "
                "must be explicitly injected together"
            )
        if perception_requested and config.perception_budget_limit <= 0:
            raise ValueError("injected perception lane needs a positive deployment budget")
        life_seed_catalog = (
            ReviewedLifeSeedCatalog.from_yaml(
                path=config.life_ecology.seed_catalog_path,
                chronology=LocalChronology(config.local_timezone),
            )
            if config.life_ecology is not None
            else None
        )
        _bootstrap(
            ledger=ledger,
            config=config,
            now=now,
            include_tool=tool_requested,
            include_perception=perception_requested,
            include_proactive=proactive_model is not None,
            life_seed_catalog=life_seed_catalog,
        )
        _LOG.warning(
            "world v2 application bootstrap ready world=%s duration_ms=%.1f",
            config.world_id,
            (time.perf_counter() - build_started) * 1000,
        )
        # Background appraisal/relationship/proactive turns are triggered by
        # domain events rather than the original Observation, so their scope
        # cannot be rediscovered from the current trigger.  Legacy hosts whose
        # reply target is already a canonical actor retain that as the safe
        # fallback; transports such as QQ must provide the distinct counterpart.
        relevance_scope = ContextRelevanceScope(
            actor_ref=config.companion_actor_ref,
            related_subject_refs=(config.counterpart_actor_ref or config.reply_target,),
        )
        capsules = context_capsule_compiler_from_ledger(
            ledger=ledger,
            situation_compiler=SituationCompiler(
                local_chronology=LocalChronology(config.local_timezone)
            ),
            relevance_scope=relevance_scope,
            life_content_store=life_content_store,
            perception_result_reader=perception_transport,
            expression_payload_store=expression_payload_store,
        )
        chat_capsules = context_capsule_compiler_from_ledger(
            ledger=ledger,
            situation_compiler=SituationCompiler(
                local_chronology=LocalChronology(config.local_timezone)
            ),
            policy=ContextCapsuleBudgetPolicy(
                # Preserve dialogue/world/affect continuity even when their
                # complete proof envelopes coincide.  Chat still trims low-
                # value capability and accounting slices below.
                hard_max_characters=32_000,
                available_capabilities=SliceBudget(
                    max_items=4, max_fields=48, max_characters=1_200
                ),
                action_budget=SliceBudget(
                    max_items=4, max_fields=40, max_characters=1_200
                ),
            ),
            relevance_scope=relevance_scope,
            life_content_store=life_content_store,
            perception_result_reader=perception_transport,
            expression_payload_store=expression_payload_store,
        )
        pinned = PinnedTurnCompiler(
            ledger=ledger,
            capsule_compiler=chat_capsules,
            deliberation=compose_production_deliberation(
                lane_id="chat_reply",
                router=router,
                main_model=main_model,
                quick_recovery=quick_recovery,
                # The main budget carries one full provider completion plus,
                # on a world-claim near-miss, one bounded corrective retry.
                # Observed steady-state provider latency is 3-8s; the old 6s
                # cap regularly cancelled an otherwise complete honest answer
                # and then spent *more* wall time delivering a canned
                # failsafe.  A real reply ten seconds late reads human; a
                # scripted acknowledgement eight seconds late does not.
                # Recovery stays compact: when the primary misses even this
                # deadline, another long provider wait would not help.
                main_timeout_seconds=12.0,
                quick_timeout_seconds=1.0,
                expression_action_kinds=config.expression_action_kinds,
            ),
            companion_actor_ref=config.companion_actor_ref,
            advisory_compiler=advisory_compiler,
            latency_recorder=latency,
            # Her active aspirations (ledger-backed wishes) may flow into what
            # she says — "我一直想去日本" needs the reply model to see them.
            aspiration_advisory=config.aspiration_enabled,
            # Expression should feel whether she is departing from or
            # returning toward baseline (Change Phase), advisory only.
            change_phase_advisory=True,
            # And how close she currently is to each registered NPC, derived
            # from committed shared history.
            npc_relationship_advisory=True,
            # A pending shared_private invitation she may still need to voice.
            shared_private_invitation_advisory=True,
            # Where her attention actually is relative to the phone (asleep,
            # focused, browsing, wanting space) so timing_choice can be a real
            # presence decision.  Advisory texture only, never a veto.
            attention_advisory=True,
            attention_chronology=LocalChronology(config.local_timezone),
        )
        social_action_worker = SocialActionWorker(
            ledger=ledger,
            pinned_turn=None,
            batch_issuer=issuer,
            policy=SocialDeferredPolicy(
                expression=ExpressionPlanBudgetPolicy(
                    account_id=config.chat_account_id,
                    amount_limit_per_action=config.reply_budget_amount,
                    actor=config.companion_actor_ref,
                    allowed_targets=(config.reply_target,),
                    recovery_policy=config.reply_recovery_policy,
                )
            ),
            actor=config.companion_actor_ref,
            source=config.social_action_worker_owner,
        )
        proactive_runtime = None
        if proactive_model is not None:
            proactive_adapter = ProactiveDraftAdapter(
                model=proactive_model,
                target=config.reply_target,
                identity_frame=proactive_identity_frame,
            )
            proactive_runtime = ProactiveActionRuntime(
                ledger=ledger,
                turn=ProactiveDeliberationTurn(
                    ledger=ledger,
                    capsule_compiler=capsules,
                    deliberation=compose_production_deliberation(
                        lane_id="proactive",
                        router=router,
                        main_model=proactive_adapter,
                        quick_recovery=proactive_adapter,
                    ),
                    companion_actor_ref=config.companion_actor_ref,
                ),
                batch_issuer=issuer,
                policy=ExpressionPlanBudgetPolicy(
                    account_id=config.proactive_account_id,
                    amount_limit_per_action=config.proactive_amount_per_action,
                    actor=config.companion_actor_ref,
                    allowed_targets=(config.reply_target,),
                    recovery_policy=config.reply_recovery_policy,
                    category="proactive",
                ),
                owner_id=config.proactive_worker_owner,
                social_initiative=SocialInitiativeCompiler(
                    ledger=ledger,
                    policy=config.social_initiative_policy,
                ),
            )
        # User interjections open durable expression-reconsideration gates on
        # every un-dispatched beat, but a gate without a reviewer is never
        # claimed and its frozen beat never dispatches (observed in the QQ
        # ledger as an Opened-only backlog).  Production therefore composes
        # the bounded closed-grammar reviewer from the same background channel
        # that carries proactive/fact/memory cognition, unless the caller
        # injected an explicit reviewer (tests do).
        if expression_reconsideration_reviewer is None and proactive_model is not None:
            expression_reconsideration_reviewer = ExpressionReconsiderationChatModelAdapter(
                model=proactive_model,
            )
        afterthought_runtime = None
        if config.afterthought_enabled and proactive_model is not None:
            # The afterthought lane rides the same background model and the
            # proactive budget/grammar: its one optional tail is a ``followup``
            # Action whose due window the generic pump owns.
            afterthought_runtime = AfterthoughtAuthorRuntime(
                ledger=ledger,
                model=proactive_model,
                policy=ExpressionPlanBudgetPolicy(
                    account_id=config.proactive_account_id,
                    amount_limit_per_action=config.proactive_amount_per_action,
                    actor=config.companion_actor_ref,
                    allowed_targets=(config.reply_target,),
                    recovery_policy=config.reply_recovery_policy,
                    category="proactive",
                ),
                batch_issuer=issuer,
                owner_id=config.afterthought_worker_owner,
                target=config.reply_target,
                companion_actor_ref=config.companion_actor_ref,
                counterpart_actor_ref=(
                    config.counterpart_actor_ref or config.reply_target
                ),
                chronology=LocalChronology(config.local_timezone),
                identity_frame=proactive_identity_frame,
                dialogue_compiler=RecentDialogueCompiler(
                    ledger=ledger,
                    expression_payload_store=expression_payload_store,
                ),
            )
        appraisal_acceptance = (
            AppraisalAcceptanceRuntime(ledger=ledger, batch_issuer=issuer)
            if appraisal_model is not None
            else None
        )
        appraisal_worker = (
            AppraisalProposalWorker(
                compiler=AppraisalProposalCompiler(
                    ledger=ledger,
                    world_appraisal_subject_ref=config.companion_actor_ref,
                ),
                acceptance=appraisal_acceptance,
                actor=config.appraisal_worker_owner,
            )
            if appraisal_acceptance is not None
            else None
        )
        appraisal_turn = (
            PinnedTurnCompiler(
                ledger=ledger,
                capsule_compiler=capsules,
                deliberation=compose_production_deliberation(
                    lane_id="interaction_appraisal",
                    router=router,
                    main_model=appraisal_model,
                    quick_recovery=appraisal_model,
                    # The combined pass drafts both the appraisal and the
                    # visible expression in one provider call, so its budget
                    # carries the entire turn: observed steady-state provider
                    # latency is 3-8s plus one bounded world-claim corrective
                    # retry, and a 6s cap regularly cut off an otherwise
                    # complete answer, cascading into a canned failsafe that
                    # arrived *later* than the honest reply would have.  The
                    # compact 1s quick budget still avoids a second long wait.
                    main_timeout_seconds=12.0,
                    quick_timeout_seconds=1.0,
                ),
                companion_actor_ref=config.companion_actor_ref,
                # The combined inbound cognition pass also drafts the later
                # expression.  Give it the same non-authoritative semantic
                # matrix advice as the ordinary reply lane; acceptance still
                # happens before any cached draft can become an Action.
                advisory_compiler=advisory_compiler,
                latency_recorder=latency,
                # When she declared a response expectation earlier, the
                # appraisal of the message that finally arrives should know
                # what she was waiting for.
                pending_expectation_advisory=True,
                # The combined inbound-cognition pass also drafts the later
                # expression, so it needs the same wish texture as the reply
                # lane for "我一直想…" to surface naturally.
                aspiration_advisory=config.aspiration_enabled,
                # And the same Change Phase reading: appraising a message
                # while "刚陷入低落" differs from while "正在走出低落".
                change_phase_advisory=True,
                npc_relationship_advisory=True,
                shared_private_invitation_advisory=True,
                # The paired pass drafts the visible expression too, so its
                # timing_choice needs the same phone-attention reading as the
                # ordinary reply lane.
                attention_advisory=True,
                attention_chronology=LocalChronology(config.local_timezone),
            )
            if appraisal_model is not None
            else None
        )
        npc_world_appraisal_turn = (
            SettledWorldAppraisalTurn(
                ledger=ledger,
                capsule_compiler=capsules,
                deliberation=compose_production_deliberation(
                    lane_id="settled_world_appraisal",
                    router=router,
                    main_model=appraisal_model,
                    quick_recovery=appraisal_model,
                ),
                companion_actor_ref=config.companion_actor_ref,
            )
            if appraisal_model is not None
            else None
        )
        silence_appraisal_turn = (
            SilenceAppraisalTurn(
                ledger=ledger,
                capsule_compiler=capsules,
                deliberation=compose_production_deliberation(
                    lane_id="silence_appraisal",
                    router=router,
                    main_model=appraisal_model,
                    quick_recovery=appraisal_model,
                ),
                companion_actor_ref=config.companion_actor_ref,
            )
            if appraisal_model is not None and config.silence_appraisal_idle_seconds
            else None
        )
        plan_disruption_appraisal_turn = (
            PlanDisruptionAppraisalTurn(
                ledger=ledger,
                capsule_compiler=capsules,
                deliberation=compose_production_deliberation(
                    lane_id="plan_disruption_appraisal",
                    router=router,
                    main_model=appraisal_model,
                    quick_recovery=appraisal_model,
                ),
                companion_actor_ref=config.companion_actor_ref,
            )
            if appraisal_model is not None and config.plan_disruption_appraisal_enabled
            else None
        )
        outcome_reader = OutcomeCandidateReader(store=life_content_store)
        outcome_deliberation_model = (
            outcome_model
            if outcome_model is not None
            else (
                OutcomeDraftDeliberationAdapter(
                    ledger=ledger, candidate_reader=outcome_reader, model=outcome_draft_model
                )
                if outcome_draft_model is not None
                else None
            )
        )
        outcome_acceptance = (
            OutcomeAcceptanceRuntime(ledger=ledger, batch_issuer=issuer)
            if outcome_deliberation_model is not None
            else None
        )
        outcome_turn = (
            OutcomeDeliberationTurn(
                ledger=ledger,
                capsule_compiler=capsules,
                deliberation=compose_production_deliberation(
                    lane_id="outcome",
                    router=router,
                    main_model=outcome_deliberation_model,
                    quick_recovery=outcome_deliberation_model,
                ),
                candidate_reader=outcome_reader,
                companion_actor_ref=config.companion_actor_ref,
            )
            if outcome_deliberation_model is not None
            else None
        )
        outcome_worker = (
            OutcomeProposalWorker(
                compiler=OutcomeProposalCompiler(ledger=ledger, candidate_reader=outcome_reader),
                acceptance=outcome_acceptance,
                actor=config.outcome_worker_owner,
            )
            if outcome_acceptance is not None
            else None
        )
        interaction_bid_acceptance = (
            InteractionBidAcceptanceRuntime(ledger=ledger, batch_issuer=issuer)
            if interaction_bid_model is not None
            else None
        )
        media_thread_acceptance = (
            MediaDeliveryThreadAcceptanceRuntime(ledger=ledger, batch_issuer=issuer)
            if interaction_bid_model is not None
            else None
        )
        interaction_bid_turn = (
            InteractionBidDeliberationTurn(
                ledger=ledger,
                capsule_compiler=capsules,
                deliberation=compose_production_deliberation(
                    lane_id="interaction_bid",
                    router=router,
                    main_model=interaction_bid_model,
                    quick_recovery=interaction_bid_model,
                ),
                companion_actor_ref=config.companion_actor_ref,
            )
            if interaction_bid_model is not None
            else None
        )
        interaction_bid_worker = (
            InteractionBidProposalWorker(
                compiler=InteractionBidProposalCompiler(ledger=ledger),
                acceptance=interaction_bid_acceptance,
                media_thread_compiler=MediaDeliveryThreadProposalCompiler(ledger=ledger),
                media_thread_acceptance=media_thread_acceptance,
                actor=config.interaction_bid_worker_owner,
            )
            if interaction_bid_acceptance is not None and media_thread_acceptance is not None
            else None
        )
        immediate_emotion_enabled = isinstance(
            appraisal_model, AppraisalDraftDeliberationAdapter
        ) or getattr(appraisal_model, "supports_immediate_emotion", False) is True
        affect_acceptance = (
            AffectAcceptanceRuntime(ledger=ledger, batch_issuer=issuer)
            if affect_model is not None or immediate_emotion_enabled
            else None
        )
        affect_turn = (
            PinnedTurnCompiler(
                ledger=ledger,
                capsule_compiler=capsules,
                deliberation=compose_production_deliberation(
                    lane_id="affect",
                    router=router,
                    main_model=affect_model,
                    quick_recovery=affect_model,
                ),
                companion_actor_ref=config.companion_actor_ref,
            )
            if affect_model is not None
            else None
        )
        affect_worker = (
            AffectDeliberationWorker(
                ledger=ledger,
                pinned_turn=affect_turn,
                compiler=AffectProposalCompiler(ledger=ledger),
                acceptance=affect_acceptance,
                actor=config.affect_worker_owner,
            )
            if affect_acceptance is not None and affect_turn is not None
            else None
        )
        immediate_emotion_worker = (
            ImmediateEmotionProposalWorker(
                appraisal_worker=appraisal_worker,
                affect_compiler=AffectProposalCompiler(ledger=ledger),
                affect_acceptance=affect_acceptance,
                actor=config.affect_worker_owner,
            )
            if immediate_emotion_enabled
            and appraisal_worker is not None
            and affect_acceptance is not None
            else None
        )
        relationship_acceptance = (
            RelationshipAcceptanceRuntime(ledger=ledger, batch_issuer=issuer)
            if relationship_model is not None
            else None
        )
        relationship_turn = (
            PinnedTurnCompiler(
                ledger=ledger,
                capsule_compiler=capsules,
                deliberation=compose_production_deliberation(
                    lane_id="relationship",
                    router=router,
                    main_model=relationship_model,
                    quick_recovery=relationship_model,
                ),
                companion_actor_ref=config.companion_actor_ref,
                relationship_evaluation=True,
            )
            if relationship_model is not None
            else None
        )
        relationship_worker = (
            RelationshipDeliberationWorker(
                ledger=ledger,
                pinned_turn=relationship_turn,
                compiler=RelationshipProposalCompiler(ledger=ledger),
                acceptance=relationship_acceptance,
                actor=config.relationship_worker_owner,
            )
            if relationship_acceptance is not None and relationship_turn is not None
            else None
        )
        relationship_adjustment_acceptance = (
            RelationshipAdjustmentAcceptanceRuntime(ledger=ledger, batch_issuer=issuer)
            if relationship_acceptance is not None
            else None
        )
        relationship_adjustment_worker = (
            RelationshipAdjustmentWorker(
                ledger=ledger,
                compiler=RelationshipAdjustmentCompiler(ledger=ledger),
                acceptance=relationship_adjustment_acceptance,
                actor=config.relationship_adjustment_worker_owner,
            )
            if relationship_adjustment_acceptance is not None
            else None
        )
        fact_acceptance = (
            FactV2AcceptanceRuntime.compose(ledger=ledger, batch_issuer=issuer)
            if fact_model is not None
            else None
        )
        tool_turn = (
            PinnedTurnCompiler(
                ledger=ledger,
                capsule_compiler=capsules,
                deliberation=compose_injected_read_only_tool_deliberation(
                    router=router, model=read_only_tool_model
                ),
                companion_actor_ref=config.companion_actor_ref,
            )
            if read_only_tool_model is not None
            else None
        )
        tool_trigger_runtime = (
            ReadOnlyToolTriggerRuntime(
                ledger=ledger,
                turn=tool_turn,  # type: ignore[arg-type]
                compiler=ReadOnlyToolProposalCompiler(
                    ledger=ledger,
                    authorization_resolver=ProjectionReadOnlyToolAuthorizationResolver(),
                    actor_ref=config.companion_actor_ref,
                    budget_account_id=config.tool_account_id,
                    budget_limit=config.tool_budget_limit,
                ),
                owner_id=config.tool_worker_owner,
            )
            if tool_turn is not None
            else None
        )
        perception_turn = (
            PinnedTurnCompiler(
                ledger=ledger,
                capsule_compiler=capsules,
                deliberation=compose_injected_perception_deliberation(
                    router=router, model=perception_model
                ),
                companion_actor_ref=config.companion_actor_ref,
            )
            if perception_model is not None
            else None
        )
        perception_trigger_runtime = (
            PerceptionTriggerRuntime(
                ledger=ledger,
                turn=perception_turn,  # type: ignore[arg-type]
                compiler=PerceptionProposalCompiler(
                    ledger=ledger,
                    authorization_resolver=ProjectionPerceptionAuthorizationResolver(),
                    actor_ref=config.companion_actor_ref,
                    budget_account_id=config.perception_account_id,
                    budget_limit=config.perception_budget_limit,
                    input_source=perception_input_source,  # type: ignore[arg-type]
                ),
                owner_id=config.perception_worker_owner,
            )
            if perception_turn is not None
            else None
        )
        platform_executor = build_platform_action_executor(
            ledger=ledger, transport=transport, expression_payload_store=expression_payload_store,
            media_payload_store=media_payload_store,
            latency_recorder=latency,
        )
        action_executor: ActionExecutor = platform_executor
        tool_executor = (
            ReadOnlyToolActionExecutor(
                queries=AuditedReadOnlyToolQueryReader(ledger=ledger),
                transport=read_only_tool_transport,
            )
            if read_only_tool_transport is not None
            else None
        )
        perception_executor = (
            PerceptionActionExecutor(
                inputs=perception_input_source,  # type: ignore[arg-type]
                transport=perception_transport,  # type: ignore[arg-type]
            )
            if perception_transport is not None
            else None
        )
        if media_transport is not None or tool_executor is not None or perception_executor is not None:
            action_executor = RoutedActionExecutor(
                platform=platform_executor,
                media=(ProviderMediaActionExecutor(
                    payloads=MediaSidecarPayloadReader(store=media_payload_store), transport=media_transport,
                ) if media_transport is not None else None),
                tool=tool_executor,
                perception=perception_executor,
            )
        expression_policy = ExpressionPlanBudgetPolicy(
            account_id=config.chat_account_id,
            amount_limit_per_action=config.reply_budget_amount,
            actor=config.companion_actor_ref,
            allowed_targets=(config.reply_target,),
            recovery_policy=config.reply_recovery_policy,
        )
        expression_recorder = ExpressionPlanAtomicRecorder(batch_issuer=issuer)
        # Same-turn quick reaction lane.  It composes only when every seam is
        # proven: the deployment's expression closure includes ``reaction``
        # (today that is the NapCat dialect exactly) and a bounded local
        # checkpoint is installed.  Production reuses the appraisal adapter's
        # local model exactly like ``immediate_emotion_gate`` (no second
        # client); tests may inject a fixture model directly.
        if quick_reaction_model is None:
            quick_reaction_model = getattr(appraisal_model, "local_appraisal_model", None)
        quick_reaction_worker = (
            QuickReactionWorker(
                ledger=ledger,
                model=quick_reaction_model,
                capabilities=QQ_NAPCAT_EXPRESSION_CAPABILITIES,
                expression_policy=expression_policy,
                expression_recorder=expression_recorder,
                executor=action_executor,
                pump_owner=f"{config.action_pump_owner}:quick-reaction",
                actor=config.companion_actor_ref,
            )
            if (
                config.quick_reaction_enabled
                and "reaction" in config.expression_action_kinds
                and quick_reaction_model is not None
            )
            else None
        )
        runtime = WorldRuntime(
            world_id=config.world_id,
            ledger=ledger,
            projection_authority=projection_authority,
            latency_recorder=latency,
            pinned_turn=pinned,
            reply_policy=ReplyBudgetPolicy(
                account_id=config.chat_account_id,
                amount_limit=config.reply_budget_amount,
                actor=config.companion_actor_ref,
                target=config.reply_target,
                recovery_policy=config.reply_recovery_policy,
            ),
            reply_recorder=MinimalReplyAtomicRecorder(batch_issuer=issuer),
            expression_policy=expression_policy,
            expression_recorder=expression_recorder,
            expression_payload_store=expression_payload_store,
            interaction_appraisal_owner=(
                config.appraisal_worker_owner if appraisal_turn is not None else None
            ),
            appraisal_acceptance=appraisal_acceptance,
            appraisal_acceptance_actor=(
                config.appraisal_worker_owner if appraisal_acceptance is not None else None
            ),
            appraisal_worker=appraisal_worker,
            interaction_appraisal_turn=appraisal_turn,
            immediate_emotion_worker=immediate_emotion_worker,
            immediate_emotion_signal_gate=config.immediate_emotion_signal_gate,
            # The semantic gate reuses the local appraisal model instance that
            # SingleCallInboundCognition already owns; its adapter exposes the
            # gate so composition never builds a second local-model client.
            immediate_emotion_semantic_gate=(
                getattr(appraisal_model, "immediate_emotion_gate", None)
                if config.semantic_immediate_emotion_gate
                else None
            ),
            npc_world_appraisal_turn=npc_world_appraisal_turn,
            silence_appraisal_turn=silence_appraisal_turn,
            silence_appraisal_idle_seconds=config.silence_appraisal_idle_seconds,
            plan_disruption_appraisal_turn=plan_disruption_appraisal_turn,
            plan_disruption_appraisal_enabled=config.plan_disruption_appraisal_enabled,
            outcome_deliberation_turn=outcome_turn,
            outcome_worker=outcome_worker,
            outcome_deliberation_owner=(
                config.outcome_worker_owner if outcome_worker is not None else None
            ),
            interaction_bid_turn=interaction_bid_turn,
            interaction_bid_worker=interaction_bid_worker,
            interaction_bid_owner=(
                config.interaction_bid_worker_owner
                if interaction_bid_worker is not None
                else None
            ),
            interaction_fact_owner=(
                config.fact_worker_owner if fact_acceptance is not None else None
            ),
            fact_acceptance=fact_acceptance,
            fact_adapter=(
                FactObservationProposalAdapter(model=fact_model)
                if fact_model is not None
                else None
            ),
            fact_memory_adapter=(
                FactMemoryDraftAdapter(model=memory_model)
                if fact_acceptance is not None and memory_model is not None
                else None
            ),
            fact_memory_lifecycle=(
                FactMemoryCandidateLifecycle(
                    ledger=ledger,
                    actor=config.fact_worker_owner,
                    source="world-v2:fact-memory-lifecycle",
                )
                if fact_acceptance is not None and memory_model is not None
                else None
            ),
            private_impression_owner=(
                config.private_impression_worker_owner
                if private_impression_model is not None
                else None
            ),
            private_impression_adapter=(
                PrivateImpressionDraftAdapter(model=private_impression_model)
                if private_impression_model is not None
                else None
            ),
            memory_withdrawal_review=(
                MemoryWithdrawalReviewRuntime(
                    ledger=ledger,
                    reviewer=MemoryWithdrawalReviewAdapter(model=memory_model),
                    owner_id=config.memory_review_worker_owner,
                )
                if memory_model is not None
                else None
            ),
            affect_deliberation_owner=(
                config.affect_worker_owner if affect_worker is not None else None
            ),
            affect_worker=affect_worker,
            affect_acceptance=affect_acceptance,
            affect_acceptance_actor=(
                config.affect_worker_owner if affect_acceptance is not None else None
            ),
            relationship_deliberation_owner=(
                config.relationship_worker_owner if relationship_worker is not None else None
            ),
            relationship_worker=relationship_worker,
            relationship_adjustment_owner=(
                config.relationship_adjustment_worker_owner
                if relationship_adjustment_worker is not None
                else None
            ),
            relationship_adjustment_worker=relationship_adjustment_worker,
            action_executor=action_executor,
            action_pump_owner=config.action_pump_owner,
            # Planning settles a MediaPlan/NotRenderable domain result in one
            # receipt-bound batch.  Its dedicated scheduler is the only
            # executor permitted to take these Actions; generic delivery must
            # not hand their snapshot bytes to a render/provider transport.
            action_pump_excluded_kinds=frozenset({"media_planning"}),
            expression_reconsideration_owner=(
                config.expression_reconsideration_owner
                if expression_reconsideration_reviewer is not None
                else None
            ),
            expression_reconsideration_reviewer=expression_reconsideration_reviewer,
            social_action_worker=social_action_worker,
            quick_reaction_worker=quick_reaction_worker,
            proactive_action_runtime=proactive_runtime,
            afterthought_author=afterthought_runtime,
            read_only_tool_owner=(config.tool_worker_owner if tool_trigger_runtime is not None else None),
            read_only_tool_trigger_runtime=tool_trigger_runtime,
            perception_owner=(
                config.perception_worker_owner if perception_trigger_runtime is not None else None
            ),
            perception_trigger_runtime=perception_trigger_runtime,
            external_result_owner=(config.tool_worker_owner if tool_trigger_runtime is not None else None),
            external_result_deliberator=(NoopToolResultDeliberator() if tool_trigger_runtime is not None else None),
            perception_result_owner=(
                config.perception_worker_owner if perception_trigger_runtime is not None else None
            ),
            perception_result_deliberator=(
                NoopPerceptionResultDeliberator()
                if perception_trigger_runtime is not None
                else None
            ),
        )
        media_execution = MediaExecutionRuntime(
            ledger=ledger,
            sidecar=media_payload_store,
            cost_profile=config.media_cost_profile,
        )
        media_execution_worker = (
            MediaExecutionWorker(
                runtime=media_execution,
                ledger=ledger,
                transport=media_transport,  # type: ignore[arg-type]
            )
            if media_transport is not None and hasattr(media_transport, "lookup_execution_result")
            else None
        )
        media_continuation_worker = (
            MediaContinuationWorker(
                runtime=MediaContinuationRuntime(
                    ledger=ledger, execution=media_execution, batch_issuer=issuer,
                ),
                ledger=ledger,
                render_policy=MediaContinuationActionPolicy(
                    actor=config.media_continuation.actor,
                    owner_id=config.media_continuation.owner_id,
                    grant=config.media_continuation.render_grant,
                    account_id=config.media_continuation.render_account_id,
                    amount_limit=config.media_continuation.render_amount_limit,
                ),
                inspection_policy=MediaContinuationActionPolicy(
                    actor=config.media_continuation.actor,
                    owner_id=config.media_continuation.owner_id,
                    grant=config.media_continuation.inspection_grant,
                    account_id=config.media_continuation.inspection_account_id,
                    amount_limit=config.media_continuation.inspection_amount_limit,
                ),
            )
            if config.media_continuation is not None and media_transport is not None
            else None
        )
        media_planning = MediaPlanningRuntime(ledger=ledger, sidecar=media_payload_store)
        composed_media_planner = media_planner
        if legacy_event_media_planner is not None and event_media_result_store is not None:
            composed_media_planner = EventMediaPlannerAdapter(
                sidecar=media_payload_store,
                legacy_planner=legacy_event_media_planner,
                result_store=event_media_result_store,
            )
        ecology_policy = (
            config.life_ecology.media_policy
            if config.life_ecology is not None
            else config.event_ecology_policy
        )
        media_ecology = (
            EventEcologyMediaCandidateRuntime(
                ledger=ledger,
                sidecar=media_payload_store,
                policy=ecology_policy,
                compiler=MediaEvidenceSnapshotCompiler(
                    ledger=ledger, visual_fact_sidecar=media_payload_store,
                ),
            )
            if ecology_policy is not None
            else None
        )
        media_selection_worker = (
            MediaSelectionWorker(
                ledger=ledger,
                draft_adapter=MediaSelectionDraftAdapter(model=media_selection_model),
                proposal_recorder=MediaSelectionProposalRecorder(ledger=ledger),
                catalog_version=ecology_policy.catalog_version + ":selection.1",
            )
            if ecology_policy is not None and media_selection_model is not None
            else None
        )
        media_selection_acceptance = (
            MediaSelectionAcceptanceRuntime(
                ledger=ledger,
                authorizer=MediaOpportunityAuthorizer(
                    ledger=ledger,
                    compiler=MediaEvidenceSnapshotCompiler(
                        ledger=ledger, visual_fact_sidecar=media_payload_store,
                    ),
                    catalog_version=ecology_policy.catalog_version,
                ),
                sidecar=media_payload_store,
                batch_issuer=issuer,
            )
            if ecology_policy is not None and config.media_selection_acceptance is not None
            else None
        )
        activity_lifecycle = (
            ActivityLifecycleWorker(
                ledger=ledger,
                catalog=ActivityOpeningCatalog(owner_actor_ref=config.companion_actor_ref),
                draft_adapter=ActivityLifecycleDraftAdapter(model=activity_lifecycle_model),
                proposal_recorder=ActivityLifecycleProposalRecorder(ledger=ledger),
                acceptance_runtime=ActivityLifecycleAcceptanceRuntime(
                    ledger=ledger, batch_issuer=issuer
                ),
                ecology_catalog_version=config.life_ecology.catalog_version,
            )
            if config.life_ecology is not None and activity_lifecycle_model is not None
            else None
        )
        life_author = (
            LifeAuthorRuntime(
                ledger=ledger,
                catalog=life_seed_catalog,
                model=activity_lifecycle_model,
                owner_actor_ref=config.companion_actor_ref,
                actor=config.life_ecology.worker_actor,
            )
            if config.life_ecology is not None and activity_lifecycle_model is not None
            else None
        )
        future_life_author = (
            FutureLifeAuthorRuntime(
                ledger=ledger,
                catalog=life_seed_catalog,
                model=activity_lifecycle_model,
                owner_actor_ref=config.companion_actor_ref,
                actor=config.life_ecology.worker_actor,
            )
            if (
                config.life_ecology is not None
                and activity_lifecycle_model is not None
                and config.future_life_author_enabled
            )
            else None
        )
        life_aftermath = (
            LifeAftermathRuntime(
                ledger=ledger,
                catalog=life_seed_catalog,
                occurrence_content=occurrence_content,
                content_store=life_content_store,
                owner_actor_ref=config.companion_actor_ref,
                actor=config.life_ecology.worker_actor,
                experience_memory_lifecycle=(
                    ExperienceMemoryCandidateLifecycle(
                        ledger=ledger,
                        actor=config.life_ecology.worker_actor,
                        source="world-v2:experience-memory-lifecycle",
                        content_store=life_content_store,
                    )
                    if memory_model is not None
                    else None
                ),
                outcome_selection_model=outcome_draft_model,
                memory_adapter=(
                    FactMemoryDraftAdapter(model=memory_model)
                    if memory_model is not None
                    else None
                ),
            )
            if config.life_ecology is not None and life_seed_catalog is not None
            else None
        )
        npc_initiative = (
            NpcInitiativeRuntime(
                ledger=ledger,
                catalog=life_seed_catalog,
                model=activity_lifecycle_model,
                occurrence_content=occurrence_content,
                owner_actor_ref=config.companion_actor_ref,
                actor=config.life_ecology.worker_actor,
            )
            if (
                config.life_ecology is not None
                and life_seed_catalog is not None
                and activity_lifecycle_model is not None
                and config.npc_initiative_enabled
            )
            else None
        )
        aspiration = (
            AspirationRuntime(
                ledger=ledger,
                catalog=life_seed_catalog,
                model=activity_lifecycle_model,
                owner_actor_ref=config.companion_actor_ref,
                actor=config.life_ecology.worker_actor,
                fade_idle_days=config.aspiration_fade_idle_days,
                fade_chance_bp=config.aspiration_fade_chance_bp,
                crystallize_chance_bp=config.aspiration_crystallize_chance_bp,
            )
            if (
                config.life_ecology is not None
                and life_seed_catalog is not None
                and activity_lifecycle_model is not None
                and config.aspiration_enabled
            )
            else None
        )
        shared_private_invitation = (
            SharedPrivateInvitationRuntime(
                ledger=ledger,
                catalog=life_seed_catalog,
                model=activity_lifecycle_model,
                owner_actor_ref=config.companion_actor_ref,
                user_participant_ref=config.counterpart_actor_ref,
                actor=config.life_ecology.worker_actor,
                invite_chance_bp=config.shared_private_invite_chance_bp,
            )
            if (
                config.life_ecology is not None
                and life_seed_catalog is not None
                and activity_lifecycle_model is not None
                and config.shared_private_invitation_enabled
                and config.counterpart_actor_ref is not None
                and config.counterpart_actor_ref.startswith("user:")
                and any(
                    item.social_shape == "shared_private"
                    for item in life_seed_catalog.reviewed_future_openings
                )
            )
            else None
        )
        open_world_event = (
            OpenWorldEventRuntime(
                ledger=ledger,
                content_store=life_content_store,
                model=open_world_event_model,
                situation_source=ActivePlanSituationSource(
                    owner_actor_ref=config.companion_actor_ref
                ),
                owner_actor_ref=config.companion_actor_ref,
                actor=config.life_ecology.worker_actor,
            )
            if config.life_ecology is not None and open_world_event_model is not None
            else None
        )
        visual_evidence_author = (
            LifeVisualEvidenceAuthor(
                ledger=ledger,
                catalog=life_seed_catalog,
                content_store=life_content_store,
                character_ref=config.companion_actor_ref,
                recipient_ref=config.counterpart_actor_ref,
                actor=config.life_ecology.worker_actor,
            )
            if config.life_ecology is not None and life_seed_catalog is not None
            else None
        )
        life_ecology = (
            LifeEcologyRuntime(
                ledger=ledger,
                trigger_store=LedgerLifeEcologyTriggerStore(
                    ledger=ledger,
                    owner_id=config.life_ecology.worker_actor,
                    lease_seconds=config.life_ecology.lease_seconds,
                ),
                media_followup=media_ecology,
                life_author_followup=life_author,
                future_life_author_followup=future_life_author,
                activity_followup=activity_lifecycle,
                aftermath_followup=life_aftermath,
                npc_initiative_followup=npc_initiative,
                aspiration_followup=aspiration,
                shared_private_followup=shared_private_invitation,
                open_world_followup=open_world_event,
                visual_evidence_followup=visual_evidence_author,
                availability=LifeEcologyAvailability(
                    state="installed_and_active",
                    catalog_version=config.life_ecology.catalog_version,
                ),
                actor=config.life_ecology.worker_actor,
            )
            if config.life_ecology is not None and media_ecology is not None
            else None
        )
        return WorldV2TurnApplication(
            turns=WorldTurnRuntime(runtime=runtime, identities=identities),
            ledger=ledger,
            life_content_store=life_content_store,
            expression_payload_store=expression_payload_store,
            media_payload_store=media_payload_store,
            media_execution=media_execution,
            media_execution_worker=media_execution_worker,
            media_continuation_worker=media_continuation_worker,
            media_planning=media_planning,
            media_planning_worker=MediaPlanningWorker(
                ledger=ledger,
                runtime=media_planning,
                # An injected legacy planner is not executable without a
                # durable lookup store; leaving this unavailable is safer than
                # retrying an untracked provider call after a restart.
                planner=composed_media_planner,
                owner_id=config.media_planning_worker_owner,
            ),
            media_ecology=media_ecology,
            life_ecology=life_ecology,
            event_ecology_worker_actor=config.event_ecology_worker_actor,
            media_selection_worker=media_selection_worker,
            media_selection_worker_actor=config.media_selection_worker_actor,
            media_candidate_maintenance=MediaCandidateMaintenanceRuntime(ledger=ledger),
            media_candidate_maintenance_actor=config.media_candidate_maintenance_actor,
            character_media_candidates=CharacterMediaCandidateRuntime(ledger=ledger),
            image_evidence=ImageEvidenceDeclarationRuntime(ledger=ledger),
            recipient_scoped_image_evidence=RecipientScopedImageEvidenceDeclarationRuntime(ledger=ledger),
            appearance_states=AppearanceStateRuntime(ledger=ledger),
            visible_physical_states=VisiblePhysicalStateRuntime(ledger=ledger),
            visual_facts=VisualFactRuntime(ledger=ledger, sidecar=media_payload_store),
            media_selection_acceptance=media_selection_acceptance,
            media_selection_acceptance_config=config.media_selection_acceptance,
            media_preview_conductor_enabled=(
                media_selection_worker is not None
                and media_selection_acceptance is not None
                and composed_media_planner is not None
            ),
            media_delivery=MediaDeliveryRuntime(ledger=ledger),
            media_auto_delivery=config.media_auto_delivery,
            occurrence_content=occurrence_content,
            activity_plans=ActivityPlanRuntime(
                ledger=ledger,
                owner_actor_ref=config.companion_actor_ref,
            ),
            deferred_replies=DeferredReplyRuntime(
                ledger=ledger,
                actor=config.companion_actor_ref,
            ),
            latency_recorder=latency,
            trace_environment=config.trace_environment,
            social_initiative_policy=config.social_initiative_policy,
        )
    except Exception:
        life_content_store.close()
        expression_payload_store.close()
        media_payload_store.close()
        ledger.close()
        raise


def build_platform_action_executor(
    *, ledger: SQLiteWorldLedger, transport: PlatformTransport,
    expression_payload_store: SQLiteImmutableExpressionPayloadStore | None = None,
    media_payload_store: SQLiteImmutableMediaPayloadStore | None = None,
    latency_recorder: ProductionLatencyRecorder | None = None,
) -> ActionExecutor:
    """Bind the platform executor to a read-only accepted-payload capability."""

    payloads = LedgerAuthorizedPayloadReader(
        ledger=ledger, expression_payload_store=expression_payload_store
    )
    if media_payload_store is not None:
        payloads = PlatformAndMediaPayloadReader(
            platform=payloads, media=MediaSidecarPayloadReader(store=media_payload_store),
        )
    return PlatformActionExecutor(
        payloads=payloads, transport=transport, latency_recorder=latency_recorder
    )


def _parse_trace_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def _bootstrap(
    *, ledger: SQLiteWorldLedger, config: WorldV2TurnApplicationConfig, now: datetime,
    include_tool: bool = False, include_perception: bool = False,
    include_proactive: bool = False,
    life_seed_catalog: ReviewedLifeSeedCatalog | None = None,
) -> None:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("World v2 bootstrap time must be timezone-aware")
    projection = ledger.project()
    accounts = [
        BudgetAccount(account_id=config.chat_account_id, category="chat",
                      window_id=config.chat_window_id, limit=config.chat_budget_limit),
    ]
    if include_tool:
        accounts.append(BudgetAccount(account_id=config.tool_account_id, category="tool",
                                      window_id=config.tool_window_id, limit=config.tool_budget_limit))
    if include_perception:
        accounts.append(
            BudgetAccount(
                account_id=config.perception_account_id,
                category="tool",
                window_id=config.perception_window_id,
                limit=config.perception_budget_limit,
            )
        )
    if include_proactive:
        accounts.append(BudgetAccount(
            account_id=config.proactive_account_id,
            category="proactive",
            window_id=config.proactive_window_id,
            limit=config.proactive_budget_limit,
        ))
    if config.media_selection_acceptance is not None:
        media = config.media_selection_acceptance
        accounts.append(BudgetAccount(
            account_id=media.account_id,
            category="image",
            window_id=media.account_window_id,
            limit=media.account_limit,
        ))
    if config.media_continuation is not None:
        continuation = config.media_continuation
        accounts.extend((
            BudgetAccount(
                account_id=continuation.render_account_id, category="image",
                window_id=continuation.render_window_id,
                limit=continuation.render_account_limit,
            ),
            BudgetAccount(
                account_id=continuation.inspection_account_id, category="image",
                window_id=continuation.inspection_window_id,
                limit=continuation.inspection_account_limit,
            ),
        ))
    missing: list[BudgetAccount] = []
    for account in accounts:
        existing = next(
            (item for item in projection.budget_accounts if item.account_id == account.account_id), None
        )
        if existing is None:
            missing.append(account)
        elif existing.category != account.category or existing.window_id != account.window_id or existing.limit != account.limit:
            raise ValueError("existing World v2 budget conflicts with composition config")
    existing_npcs = {item.npc_id: item for item in projection.npcs}
    missing_npcs = [] if life_seed_catalog is None else [
        item for item in life_seed_catalog.reviewed_npcs if item.npc_id not in existing_npcs
    ]
    if life_seed_catalog is not None:
        locations = {item.id: item for item in life_seed_catalog.reviewed_locations}
        for item in life_seed_catalog.reviewed_npcs:
            current = existing_npcs.get(item.npc_id)
            expected_location = (
                locations[item.location_id].location_ref if item.location_id is not None else None
            )
            if current is not None and (
                current.stable_identity_ref != item.stable_identity_ref
                or current.known_trait_refs != item.known_trait_refs
                or current.privacy_class != item.privacy
                or current.current_location_ref != expected_location
                or current.status != "active"
            ):
                raise ValueError("existing reviewed NPC conflicts with life seed catalog")
    if not missing and not missing_npcs:
        return
    if projection.world_revision and not any(
        item.event_type == "WorldStarted" for item in projection.committed_world_event_refs
    ):
        raise ValueError("World v2 ledger has state but no WorldStarted authority")
    events: list[WorldEvent] = []
    world_started = next(
        (
            item for item in projection.committed_world_event_refs
            if item.event_type == "WorldStarted"
        ),
        None,
    )
    if projection.world_revision == 0:
        started_event = _bootstrap_event(
            config=config, now=now, event_type="WorldStarted", payload={}
        )
        events.append(started_event)
        world_started_ref = EvidenceRef(
            ref_id=started_event.event_id,
            evidence_type="committed_world_event",
            claim_purpose="current_fact",
            source_world_revision=1,
            immutable_hash=started_event.payload_hash,
        )
    else:
        assert world_started is not None
        world_started_ref = EvidenceRef(
            ref_id=world_started.event_id,
            evidence_type="committed_world_event",
            claim_purpose="current_fact",
            source_world_revision=world_started.world_revision,
            immutable_hash=world_started.payload_hash,
        )
    events.extend(
        _bootstrap_event(config=config, now=now, event_type="BudgetAccountConfigured",
                         payload={"account": account.model_dump(mode="json")})
        for account in missing
    )
    if missing_npcs:
        assert life_seed_catalog is not None
        locations = {item.id: item for item in life_seed_catalog.reviewed_locations}
        event_time = projection.logical_time or now
        for item in missing_npcs:
            location_ref = (
                locations[item.location_id].location_ref if item.location_id is not None else None
            )
            payload = NpcRegisteredPayload(
                change_id=f"change:life-seed:npc:{item.npc_id}",
                transition_id=f"transition:life-seed:npc:{item.npc_id}",
                expected_entity_revision=0,
                evidence_refs=(world_started_ref,),
                policy_refs=(
                    f"policy:life-author-catalog:{life_seed_catalog.version}",
                    f"catalog-hash:{life_seed_catalog.catalog_hash}",
                ),
                npc=NpcProjection(
                    npc_id=item.npc_id,
                    entity_revision=1,
                    stable_identity_ref=item.stable_identity_ref,
                    known_trait_refs=item.known_trait_refs,
                    privacy_class=item.privacy,
                    current_location_ref=location_ref,
                    status="active",
                ),
            )
            events.append(
                _bootstrap_event(
                    config=config,
                    now=event_time,
                    event_type="NpcRegistered",
                    payload=payload.model_dump(mode="json"),
                )
            )
    ledger.commit(
        events,
        expected_world_revision=projection.world_revision,
        expected_deliberation_revision=projection.deliberation_revision,
    )


def _bootstrap_event(
    *, config: WorldV2TurnApplicationConfig, now: datetime, event_type: str, payload: dict[str, object]
) -> WorldEvent:
    material = json.dumps(
        {"world_id": config.world_id, "event_type": event_type, "payload": payload},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    idempotency_key = domain_idempotency_key(
        event_type=event_type, world_id=config.world_id, payload=payload
    )
    return WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id=f"event:world-v2-bootstrap:{event_type}:{digest}",
        world_id=config.world_id,
        event_type=event_type,
        logical_time=now,
        created_at=now,
        actor="system:world-v2-bootstrap",
        source="world-v2:composition",
        trace_id=f"trace:world-v2-bootstrap:{digest}",
        causation_id=f"bootstrap:{config.world_id}",
        correlation_id=f"bootstrap:{config.world_id}",
        idempotency_key=idempotency_key or f"world-v2:bootstrap:{event_type}:{digest}",
        payload=payload,
    )


__all__ = [
    "LifeEcologyComposition",
    "MediaAutoDeliveryComposition",
    "MediaPreviewDeployment",
    "MediaContinuationComposition",
    "MediaSelectionAcceptanceComposition",
    "WorldV2TurnApplication",
    "WorldV2TurnApplicationConfig",
    "build_platform_action_executor",
    "build_sqlite_world_v2_turn_application",
]

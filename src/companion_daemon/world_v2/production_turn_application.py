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
from typing import Awaitable, Literal, Mapping, Protocol

from .accepted_ledger_batch import AcceptedLedgerBatchIssuer
from .action_pump import (
    ActionExecutor,
    ActionPumpResult,
    ProviderAcceptedReconciliationGate,
)
from .activity_plan_runtime import (
    ActivityPlanCommand,
    ActivityPlanRuntime,
    ActivityPlanTransitionCommand,
)
from .activity_lifecycle_runtime import (
    ActivityLifecycleAcceptanceRuntime,
    ActivityLifecycleProposalRecorder,
)
from .activity_lifecycle_worker import ActivityLifecycleWorker
from .life_ecology_activity import ActivityOpeningCatalog
from .deferred_reply_runtime import DeferredReplyRuntime
from .reflection_scheduler import ReflectionScheduler
from .fact_draft_adapter import FactDraftChatModel, FactObservationProposalAdapter
from .fact_memory_candidate_lifecycle import FactMemoryCandidateLifecycle
from .experience_memory_candidate_lifecycle import ExperienceMemoryCandidateLifecycle
from .experience_memory_decision import (
    ExperienceMemoryDecisionRecordedPayload,
    experience_memory_decision_event_id,
)
from .fact_v2_acceptance_runtime import FactV2AcceptanceRuntime
from .interaction_fact_trigger_runtime import FactTriggerRunResult
from .interaction_act_acceptance_runtime import InteractionActAcceptanceRuntime
from .interaction_act_proposal_compiler import InteractionActProposalCompiler
from .interaction_act_worker import InteractionActWorker, InteractionActWorkResult
from .affect_acceptance_runtime import AffectAcceptanceRuntime
from .affect_proposal_compiler import AffectProposalCompiler
from .relationship_acceptance_runtime import RelationshipAcceptanceRuntime
from .relationship_commitment_acceptance_runtime import (
    RelationshipCommitmentAcceptanceRuntime,
)
from .relationship_commitment_worker import (
    RelationshipCommitmentWorker,
    RelationshipCommitmentWorkResult,
)
from .relationship_proposal_compiler import RelationshipProposalCompiler
from .relationship_adjustment_acceptance_runtime import (
    RelationshipAdjustmentAcceptanceRuntime,
)
from .relationship_adjustment_compiler import RelationshipAdjustmentCompiler
from .relationship_adjustment_worker import RelationshipAdjustmentWorker
from .appraisal_acceptance_runtime import AppraisalAcceptanceRuntime
from .appraisal_proposal_compiler import AppraisalProposalCompiler
from .appraisal_proposal_worker import AppraisalProposalWorker
from .immediate_emotion_proposal_worker import ImmediateEmotionProposalWorker
from .character_interior.run_result import CharacterInteriorRunResult
from .outcome_acceptance_runtime import OutcomeAcceptanceRuntime
from .outcome_candidate_reader import OutcomeCandidateReader
from .outcome_deliberation_turn import OutcomeDeliberationTurn
from .outcome_proposal_compiler import OutcomeProposalCompiler
from .outcome_proposal_worker import OutcomeProposalWorker
from .outcome_trigger_runtime import OutcomeTriggerRunResult
from .character_interior.outcome_materialization import (
    _CharacterInteriorOutcomeMaterializer,
)
from .character_interior import CharacterInterior
from .character_interior.inbound_relationship import InboundRelationshipSignalWorker
from .character_interior.inbound_turn import (
    compose_character_interior_inbound_deliberation,
)
from .character_interior.production import (
    _bind_production_character_interior,
)
from .deliberation import ModelRouterAdapter
from .production_proposal_grammar import compose_production_deliberation
from .expression_episode import ExpressionEpisodeDiagnostics
from .expression_episode_lifecycle import (
    expression_episode_technical_failure_count,
    expression_episode_work_due,
)
from .ledger_context_resolver import (
    ContextRelevanceScope,
    context_capsule_compiler_from_ledger,
    fact_recall_items,
)
from .change_phase_view import change_phase_reading_prose, change_phase_readings
from .mood_view import MOOD_LABELS
from .npc_relationship_view import npc_relationship_readings
from .npc_ecology_health import npc_ecology_health_snapshot
from .npc_identity_view import npc_identity_views
from .context_capsule import ContextCapsuleBudgetPolicy, SliceBudget
from .ledger_payload_reader import LedgerAuthorizedPayloadReader
from .local_chronology import LocalChronology
from .life_content_store import (
    SQLiteImmutableLifeContentStore,
    StoredLifeContent,
    life_content_payload_hash,
)
from .situation_compiler import SituationCompiler
from .expression_payload_store import SQLiteImmutableExpressionPayloadStore
from .media_v2 import MediaPlanner, SQLiteImmutableMediaPayloadStore
from .recall_index import (
    FeatureHashRecallEmbedding,
    RecallEmbedding,
    SQLiteRecallIndex,
)
from .recall_embedding import SQLiteCachedRecallEmbedding
from .recall_runtime import RecallCoordinator
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
from .npc_ecology import NpcEcology, NpcEcologyModel
from .open_world_event_draft import OpenWorldEventModel
from .open_world_event_runtime import (
    ActivePlanSituationSource,
    OpenWorldEventRuntime,
)
from .life_ecology_trigger_store import LedgerLifeEcologyTriggerStore
from .life_author_seed import ReviewedLifeSeedCatalog
from .life_development_capability import ProjectionLifeCapabilityManifestCompiler
from .life_development_runtime import LifeDevelopmentModel, LifeDevelopmentRuntime
from .life_aftermath_runtime import LifeAftermathRuntime
from .biographical_lifecycle import BiographicalLifecycleCatalog
from .biographical_lifecycle_runtime import BiographicalLifecycleRuntime
from .biographical_timeline_authority import (
    BiographicalTimelineConfiguredPayload,
)
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
from .media_selection_worker import MediaSelectionRunResult, MediaSelectionWorker
from .media_preview_conductor import (
    MediaPreviewAcceptanceOutcome,
    MediaPreviewConductor,
    MediaPreviewConductorResult,
)
from .media_request_runtime import MediaRequestRuntime
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
from .pinned_turn import PinnedTurnCompiler
from .interactive_turn_budget import InteractiveTurnBudgetPolicy
from .production_latency_trace import (
    ProductionLatencyRecorder,
    ProductionLatencySample,
    TraceEnvironment,
)
from .production_performance_evidence import (
    ProductionPerformanceEvidence,
    ProductionPerformanceEvidenceReader,
)
from .expression_draft import (
    ExpressionDraftCapabilities,
    PRODUCTION_TEXT_ONLY_EXPRESSION_CAPABILITIES,
)
from .social_action_acceptance import SocialDeferredPolicy
from .social_action_worker import SocialActionRunResult, SocialActionWorker
from .model_completion import ChatCompletionModel
from .bounded_decision_vertical import AnchoredRunResult
from .vertical_registry import assert_bounded_vertical_coverage
from .proactive_action import (
    ProactiveActionRuntime,
    proactive_technical_retry_states,
)
from .proposal_envelope import DecisionProposal, validate_proposal_envelope
from .social_initiative import (
    SITUATION_STIMULUS_EVENT_TYPES,
    SocialInitiativeContextPolicy,
    SocialInitiativePolicy,
    social_initiative_attempt_id,
    social_initiative_consideration_id,
)
from .random_authority import RandomDrawRecordedPayload
from .memory_withdrawal_review import (
    MemoryWithdrawalReviewRunResult,
    MemoryWithdrawalReviewRuntime,
)
from .platform_action_executor import (
    PlatformActionExecutor,
    PlatformTransport,
    MediaProviderTransport,
    ProviderMediaActionExecutor,
    RoutedActionExecutor,
)
from .perception_authorization_resolver import ProjectionPerceptionAuthorizationResolver
from .perception_deliberation import (
    compose_character_interior_perception_deliberation,
)
from .perception_executor import PerceptionActionExecutor, PerceptionTransport
from .perception_input_source import PerceptionInputSource
from .perception_proposal_compiler import PerceptionProposalCompiler
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
    LedgerProjection,
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

_EXPRESSION_RETRY_WARNING_GRACE = timedelta(minutes=2)


class _AsyncCloseable(Protocol):
    """The lifecycle surface retained from factory-owned deliberations."""

    async def aclose(self) -> None: ...


def _expression_retry_health(
    projection: LedgerProjection,
) -> dict[str, object]:
    """Project durable technical-retry liveness without process-local state.

    The scheduler-owned lifecycle helpers remain the single definition of
    which open/claimed episode needs continuation and when.  Health only
    summarizes that immutable projection, so process restart cannot reset it.
    """

    pending_with_due = []
    for process in projection.trigger_processes:
        due_at = expression_episode_work_due(projection, process)
        if due_at is not None:
            pending_with_due.append((process, due_at))
    pending_with_due.sort(
        key=lambda item: (
            item[1],
            item[0].trigger_id,
        )
    )

    logical_time = projection.logical_time
    due = [
        item for item in pending_with_due if logical_time is not None and item[1] <= logical_time
    ]
    overdue = [
        item
        for item in due
        if logical_time is not None and logical_time - item[1] > _EXPRESSION_RETRY_WARNING_GRACE
    ]
    warning_reasons = ["expression_retry_overdue"] if overdue else []
    earliest_due = pending_with_due[0][1] if pending_with_due else None
    max_attempt_ordinal = max(
        (len(process.attempt_ids) for process, _ in pending_with_due),
        default=0,
    )
    consecutive_technical_failures = max(
        (
            expression_episode_technical_failure_count(projection, process)
            for process, _ in pending_with_due
        ),
        default=0,
    )
    locator_limit = 8
    return {
        "state": "due" if due else ("waiting" if pending_with_due else "idle"),
        "pending_count": len(pending_with_due),
        "waiting_count": len(pending_with_due) - len(due),
        "due_count": len(due),
        "overdue_count": len(overdue),
        "earliest_due_at": earliest_due.isoformat() if earliest_due is not None else None,
        "max_attempt_ordinal": max_attempt_ordinal,
        "consecutive_technical_failures": consecutive_technical_failures,
        "pending_source_observation_refs": [
            process.source_evidence_ref for process, _ in pending_with_due[:locator_limit]
        ],
        "pending_trigger_ids": [
            process.trigger_id for process, _ in pending_with_due[:locator_limit]
        ],
        "locators_truncated": len(pending_with_due) > locator_limit,
        "warning": bool(warning_reasons),
        "warning_reasons": warning_reasons,
    }


def _proactive_reliability_health(
    projection: LedgerProjection,
    *,
    window: timedelta = timedelta(hours=24),
) -> dict[str, object]:
    """Summarize the durable proactive funnel without conflating silence and failure."""

    as_of = projection.logical_time
    cutoff = as_of - window if as_of is not None else None
    attempts = []
    for process in projection.trigger_processes:
        considered_at = process.claim_lease.acquired_at if process.claim_lease is not None else None
        if (
            process.process_kind != ProactiveActionRuntime.PROCESS_KIND
            or process.state != "terminal"
            or considered_at is None
            or (cutoff is not None and considered_at < cutoff)
        ):
            continue
        attempts.append(process)

    latest_by_consideration = {}
    for process in attempts:
        latest_by_consideration[process.trigger_ref] = process
    considerations = tuple(latest_by_consideration.values())
    actions_by_id = {item.action_id: item for item in projection.actions}
    audits_by_result = {item.model_result_ref: item for item in projection.model_result_audits}

    technical_attempts = tuple(
        item
        for item in attempts
        if str(item.runtime_outcome_ref).startswith("proactive:deliberation-failed:")
    )
    technical_considerations = tuple(
        item
        for item in considerations
        if str(item.runtime_outcome_ref).startswith("proactive:deliberation-failed:")
    )
    silent = tuple(
        item for item in considerations if item.runtime_outcome_ref == "proactive:silent"
    )
    grounding_rejected = tuple(
        item
        for item in considerations
        if item.runtime_outcome_ref == "proactive:grounding-rejected"
    )
    authorized = tuple(
        item
        for item in considerations
        if str(item.runtime_outcome_ref).startswith("proactive:authorized:")
    )
    authorized_actions = tuple(
        actions_by_id.get(str(item.runtime_outcome_ref).removeprefix("proactive:authorized:"))
        for item in authorized
    )
    delivered_count = sum(
        item is not None and item.state == "delivered" for item in authorized_actions
    )
    non_delivered_terminal_count = sum(
        item is not None and item.state in {"failed", "cancelled", "expired", "unknown"}
        for item in authorized_actions
    )
    delivery_pending_count = len(authorized_actions) - (
        delivered_count + non_delivered_terminal_count
    )
    failure_codes: Counter[str] = Counter()
    for process in technical_attempts:
        result_ref = str(process.runtime_outcome_ref).removeprefix("proactive:deliberation-failed:")
        audit = audits_by_result.get(result_ref)
        audit_failure_code = None
        if audit is not None:
            try:
                parsed_audit = json.loads(audit.audit_json)
            except (TypeError, ValueError, json.JSONDecodeError):
                parsed_audit = None
            if isinstance(parsed_audit, dict):
                candidate_failure_code = parsed_audit.get("failure_code")
                if isinstance(candidate_failure_code, str) and candidate_failure_code:
                    audit_failure_code = candidate_failure_code
        failure_codes[audit_failure_code or "unknown_technical_failure"] += 1

    consideration_count = len(considerations)
    terminal_delivery_count = delivered_count + non_delivered_terminal_count

    def rate(numerator: int, denominator: int) -> float | None:
        return round(numerator / denominator, 4) if denominator else None

    warning_reasons = []
    # A recovered retry must not erase evidence that the provider lane failed
    # earlier in the same durable consideration.  The consideration-level rate
    # describes final outcomes; availability warnings and the attempt rate use
    # every terminal attempt in the rolling window.
    if technical_attempts:
        warning_reasons.append("technical_failures_24h")
    if non_delivered_terminal_count:
        warning_reasons.append("delivery_failures_24h")
    if len(grounding_rejected) >= 3:
        warning_reasons.append("repeated_grounding_rejections_24h")
    return {
        "window_hours": int(window.total_seconds() // 3_600),
        "as_of": as_of.isoformat() if as_of is not None else None,
        "attempt_count": len(attempts),
        "consideration_count": consideration_count,
        "technical_failure_attempt_count": len(technical_attempts),
        "technical_failure_consideration_count": len(technical_considerations),
        "model_silent_count": len(silent),
        "grounding_rejected_count": len(grounding_rejected),
        "authorized_count": len(authorized),
        "delivered_count": delivered_count,
        "delivery_pending_count": delivery_pending_count,
        "delivery_non_delivered_terminal_count": non_delivered_terminal_count,
        "model_decision_success_rate": rate(len(authorized) + len(silent), consideration_count),
        "technical_failure_rate": rate(len(technical_considerations), consideration_count),
        "technical_failure_attempt_rate": rate(len(technical_attempts), len(attempts)),
        "visible_authorization_rate": rate(len(authorized), consideration_count),
        "visible_delivery_rate": rate(delivered_count, consideration_count),
        "delivery_success_rate": rate(delivered_count, terminal_delivery_count),
        "technical_failure_codes": dict(sorted(failure_codes.items())),
        "warning": bool(warning_reasons),
        "warning_reasons": warning_reasons,
    }


def external_perception_downstream_health(
    projection: LedgerProjection,
) -> dict[str, object] | None:
    """Correlate the latest perception with its exact Life/Social outcomes."""

    if not projection.external_perceptions:
        return None
    perception = projection.external_perceptions[-1]
    batch = tuple(
        item
        for item in projection.external_perceptions
        if item.attention_attempt_id == perception.attention_attempt_id
    )
    perception_refs = frozenset(item.accepted_event_ref for item in batch)
    latest_message_revision = (
        projection.message_observations[-1].world_revision if projection.message_observations else 0
    )
    stimulus_refs = tuple(
        sorted(
            (
                item
                for item in projection.committed_world_event_refs
                if item.world_revision > latest_message_revision
                and item.event_type in SITUATION_STIMULUS_EVENT_TYPES
            ),
            key=lambda item: (item.logical_time, item.world_revision, item.event_id),
        )
    )
    social_anchor_refs = set(perception_refs)
    clusters: list[list[object]] = []
    for ref in stimulus_refs:
        if not clusters or ref.logical_time - clusters[-1][0].logical_time >= timedelta(minutes=10):
            clusters.append([ref])
        else:
            clusters[-1].append(ref)
    for cluster in clusters:
        if any(item.event_id in perception_refs for item in cluster):
            social_anchor_refs.add(cluster[0].event_id)
    related = tuple(
        item
        for item in projection.trigger_processes
        if item.process_kind == "proactive_action_deliberation"
        and item.source_evidence_ref in social_anchor_refs
    )
    latest_related = related[-1] if related else None
    social_state = "opportunity_pending"
    if latest_related is not None and latest_related.state != "terminal":
        social_state = "considering"
    elif latest_related is not None:
        outcome = str(latest_related.runtime_outcome_ref or "")
        if outcome == "proactive:silent":
            social_state = "model_silent_no_action"
        elif outcome == "proactive:grounding-rejected":
            social_state = "grounding_rejected_no_action"
        elif outcome.startswith("proactive:deliberation-failed:"):
            social_state = "technical_failure_retry"
        elif outcome.startswith("proactive:authorized:"):
            action_id = outcome.removeprefix("proactive:authorized:")
            action = next(
                (item for item in projection.actions if item.action_id == action_id),
                None,
            )
            social_state = (
                "action_pending"
                if action is not None
                and action.state not in {"delivered", "failed", "cancelled", "expired"}
                else "action_settled"
            )
        else:
            social_state = "considered_no_action"
    life_processes = tuple(
        item
        for item in projection.trigger_processes
        if item.process_kind == "life_ecology" and item.source_evidence_ref in perception_refs
    )
    life_state = "opportunity_pending"
    if any(item.state == "terminal" for item in life_processes):
        life_state = "considered"
    elif life_processes:
        life_state = "considering"
    return {
        "perception_event_ref": batch[0].accepted_event_ref,
        "perception_event_refs": [item.accepted_event_ref for item in batch],
        "attention_attempt_id": perception.attention_attempt_id,
        "life_state": life_state,
        "social_state": social_state,
    }


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
            not self.catalog_version
            or not self.worker_actor
            or self.lease_seconds <= 0
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
            not self.actor
            or not self.owner_id
            or not self.render_account_id
            or not self.render_window_id
            or not self.inspection_account_id
            or not self.inspection_window_id
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
    never manufactures or signs that authority.  Character-owned candidate
    selection is deliberately absent from this deployment value: the
    application offers the capability through its sole ``CharacterInterior``.
    ``continuation=None`` intentionally installs only the candidate-to-plan
    prefix.  It is not a complete preview pipeline and cannot render or
    inspect; full preview requires the separate render/inspection authority.
    ``auto_delivery`` installs the world-owned delivery policy: the send
    decision is the already-accepted media selection, and this composition
    only adds operational guardrails (daily cap, minimum gap).  Absent, the
    lane stops at ``MediaPreviewGenerated``.
    """

    planner: MediaPlanner
    acceptance: MediaSelectionAcceptanceComposition
    continuation: MediaContinuationComposition | None = None
    auto_delivery: MediaAutoDeliveryComposition | None = None

    def __post_init__(self) -> None:
        if self.planner is None or self.acceptance is None:
            raise ValueError("media preview deployment requires planner and acceptance")
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
    interactive_turn_budget_policy: InteractiveTurnBudgetPolicy = InteractiveTurnBudgetPolicy()
    # ``on`` was the retired provisional/full two-author race.  Immutable
    # events from that contract remain replayable, but no live application may
    # create new work through it.
    expression_episode_mode: Literal["off", "shadow", "stream"] = "off"
    recorded_cadence_mode: Literal["off", "shadow", "on"] = "off"
    expression_action_kinds: frozenset[str] = frozenset({"reply", "followup", "proactive_message"})
    # Capability is a transport fact shared with proactive authoring.  It is
    # not a behavior policy and replaces proactive's historical text-only
    # side interface.
    expression_capabilities: ExpressionDraftCapabilities = (
        PRODUCTION_TEXT_ONLY_EXPRESSION_CAPABILITIES
    )
    inner_state_settlement_owner: str = "worker:world-v2:inner-state-settlement"
    # Production leaves this unset so each WorldRuntime instance claims with a
    # process-unique owner (a second daemon must not be mistaken for the same
    # in-flight provider lease).  Deterministic consumers (offline scenario
    # suites, replay evidence) must pass a fixed owner so claim payloads and
    # event identities are byte-identical across runs.
    expression_episode_owner: str | None = None
    affect_settlement_owner: str = "worker:world-v2:affect-settlement"
    relationship_settlement_owner: str = "worker:world-v2:relationship-settlement"
    relationship_adjustment_worker_owner: str = "worker:world-v2:relationship-adjustment"
    fact_worker_owner: str = "worker:world-v2:fact"
    private_impression_worker_owner: str = "worker:world-v2:private-impression"
    memory_review_worker_owner: str = "worker:world-v2:memory-review"
    # Capability deployment switch, not a retention policy.  When enabled,
    # Fact/Experience retention and withdrawal review all use the one
    # CharacterInterior author at an exact ledger cursor.
    character_memory_enabled: bool = True
    outcome_worker_owner: str = "worker:world-v2:outcome"
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
    # How long the user must stay quiet after her delivered reply before she
    # gets one chance to appraise the silence.  ``0``/``None`` disables the
    # lane; the QQ composition keeps the default enabled.
    silence_appraisal_idle_seconds: int | None = 3_600
    # Every committed plan abandonment leaves her one chance to appraise what
    # losing that plan means (regret, relief, nothing).  Disabling stops
    # opening new triggers; already-open ones still drain.
    plan_disruption_appraisal_enabled: bool = True
    # Compatibility name for the model-owned NPC Ecology lane.  The old
    # reviewed-candidate/random-act implementation is no longer composed.
    npc_ecology_enabled: bool = True

    def __post_init__(self) -> None:
        for name in (
            "world_id",
            "companion_actor_ref",
            "reply_target",
            "action_pump_owner",
            "inner_state_settlement_owner",
            "affect_settlement_owner",
            "relationship_settlement_owner",
            "relationship_adjustment_worker_owner",
            "fact_worker_owner",
            "private_impression_worker_owner",
            "memory_review_worker_owner",
            "outcome_worker_owner",
            "expression_reconsideration_owner",
            "social_action_worker_owner",
            "media_planning_worker_owner",
            "event_ecology_worker_actor",
            "media_selection_worker_actor",
            "media_candidate_maintenance_actor",
            "perception_worker_owner",
            "proactive_worker_owner",
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
        if self.expression_episode_mode not in {"off", "shadow", "stream"}:
            raise ValueError("expression episode mode must be off, shadow, or stream")
        if self.recorded_cadence_mode not in {"off", "shadow", "on"}:
            raise ValueError("recorded cadence mode must be off, shadow, or on")
        if not self.expression_action_kinds:
            raise ValueError("expression action capability set must not be empty")
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
            or not 0
            <= self.proactive_amount_per_action
            <= self.proactive_budget_limit
            <= 10_000_000
        ):
            raise ValueError("proactive budget config is invalid")
        if (
            self.life_ecology is not None
            and self.event_ecology_policy is not None
            and self.event_ecology_policy != self.life_ecology.media_policy
        ):
            raise ValueError("life ecology and event ecology policies must agree")
        if (
            self.silence_appraisal_idle_seconds is not None
            and self.silence_appraisal_idle_seconds < 0
        ):
            raise ValueError("silence appraisal idle threshold must not be negative")


class WorldV2TurnApplication:
    """Small host-facing interface for the persistent single-reply v2 lane."""

    def __init__(
        self,
        *,
        turns: WorldTurnRuntime,
        character_interior: CharacterInterior,
        companion_actor_ref: str,
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
        visual_evidence_author: LifeVisualEvidenceAuthor | None,
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
        reviewed_npc_identity_summaries: dict[str, str] | None = None,
        recall_index: SQLiteRecallIndex | None = None,
        recall_coordinator: RecallCoordinator | None = None,
        owned_deliberations: tuple[_AsyncCloseable, ...] = (),
    ) -> None:
        if not companion_actor_ref:
            raise ValueError("production application requires companion actor identity")
        self._turns = turns
        self._character_interior = character_interior
        self._companion_actor_ref = companion_actor_ref
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
        self._media_request_runtime = (
            MediaRequestRuntime(
                ledger=ledger,
                conductor=self._media_preview_conductor,
                candidate_supplier=visual_evidence_author,
            )
            if self._media_preview_conductor is not None
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
        self._reviewed_npc_identity_summaries = reviewed_npc_identity_summaries or {}
        self._recall_index = recall_index
        self._recall_coordinator = recall_coordinator
        self._owned_deliberations = owned_deliberations
        self._closed = False
        self._close_task: asyncio.Task[None] | None = None
        self._deferred_store_close_task: asyncio.Task[None] | None = None
        self._last_character_outcome: str | None = None

    async def respond(self, inbound: InboundTurn) -> RuntimeOutcome:
        outcome = await self._turns.respond(inbound)
        self._last_character_outcome = outcome.status
        return outcome

    async def cancel_superseded_expression_streams(self, current_trigger_ref: str) -> None:
        """Drop only process-local, not-yet-visible units for newer ingress."""

        await self._turns.cancel_superseded_expression_streams(current_trigger_ref)

    async def delivered_text_character_count(self, action_id: str) -> int | None:
        """Resolve the exact immutable text behind one delivered Action."""

        projection = (
            await asyncio.to_thread(self._ledger.project)
            if self._ledger.blocks_event_loop
            else self._ledger.project()
        )
        actions = tuple(item for item in projection.actions if item.action_id == action_id)
        if len(actions) != 1 or actions[0].kind not in {
            "reply",
            "followup",
            "proactive_message",
        }:
            return None
        payload = await LedgerAuthorizedPayloadReader(
            ledger=self._ledger,
            expression_payload_store=self._expression_payload_store,
        ).resolve(actions[0])
        if not payload.content_type.startswith("text/"):
            return None
        return len(payload.body)

    async def media_request_for_actions(self, action_ids: tuple[str, ...]) -> bool:
        """Resolve the durable role-owned media wake bound to visible Actions."""
        if self._media_request_runtime is None:
            return False
        return await self._media_request_runtime.request_for_actions(action_ids)

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
                kwargs = dict(
                    events=events,
                    expected_cursor=ProjectionCursor(
                        world_revision=current.world_revision,
                        deliberation_revision=current.deliberation_revision,
                        ledger_sequence=current.ledger_sequence,
                    ),
                    commit_id="reply-later:clock:" + clock.tick_id,
                )
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
        run_life_ecology: bool = True,
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
        if run_life_ecology and self._life_ecology is not None:
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
                    action_id=action_id,
                    logical_time=observed_at,
                    created_at=observed_at,
                    trace_id=trace_id,
                    causation_id=causation_id,
                    correlation_id=correlation_id,
                )
            else:
                self._deferred_replies.settle_terminal_action(
                    action_id=action_id,
                    logical_time=observed_at,
                    created_at=observed_at,
                    trace_id=trace_id,
                    causation_id=causation_id,
                    correlation_id=correlation_id,
                )
        return outcome

    async def record_outcome_observation(self, observation: OutcomeObservation) -> RuntimeOutcome:
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
        kwargs = dict(
            command=command,
            logical_time=logical_time,
            created_at=created_at,
            trace_id=trace_id,
            causation_id=causation_id,
            correlation_id=correlation_id,
        )
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
        kwargs = dict(
            command=command,
            predecessor_plan_id=predecessor_plan_id,
            logical_time=logical_time,
            created_at=created_at,
            trace_id=trace_id,
            causation_id=causation_id,
            correlation_id=correlation_id,
        )
        if self._ledger.blocks_event_loop:
            return await asyncio.to_thread(self._activity_plans.replace, **kwargs)
        return self._activity_plans.replace(**kwargs)

    async def drain_actions_once(
        self,
        *,
        provider_accepted_reconciliation_gate: (ProviderAcceptedReconciliationGate | None) = None,
    ) -> ActionPumpResult | None:
        result = await self._turns.drain_actions_once(
            provider_accepted_reconciliation_gate=(provider_accepted_reconciliation_gate)
        )
        await self._join_deferred_terminal_action(result)
        return result

    async def drain_action(self, action_id: str) -> ActionPumpResult | None:
        """Drain an ingress-bound Action without globally scheduling siblings."""

        result = await self._turns.drain_action(action_id)
        await self._join_deferred_terminal_action(result)
        return result

    async def _join_deferred_terminal_action(self, result: ActionPumpResult | None) -> None:
        """Join a pump-written terminal receipt to its reply-later Commitment.

        The Action pump owns dispatch and receipt settlement.  This application
        seam owns the platform-neutral continuation join so hosts cannot forget
        it and a restart cannot strand a delivered promise as still due.
        """

        if result is None or result.action_id is None:
            if self._ledger.blocks_event_loop:
                await asyncio.to_thread(self._deferred_replies.recover_one_terminal_commitment)
            else:
                self._deferred_replies.recover_one_terminal_commitment()
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
            "delivered",
            "failed",
            "cancelled",
            "expired",
            "unknown",
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
        self,
        *,
        logical_time: datetime,
        trace_id: str,
        correlation_id: str,
    ) -> str | None:
        if self._media_continuation_worker is None:
            return None
        return self._media_continuation_worker.drain_once(
            logical_time=logical_time,
            trace_id=trace_id,
            correlation_id=correlation_id,
        )

    async def drain_media_planning_once(self) -> MediaPlanningRunResult:
        """Advance one already-frozen Media v2 planning Action.

        This is scheduler-only: it does not select a candidate or construct a
        snapshot.  A missing composition-owned planner is visible as an
        ``unavailable`` result and cannot fall back to the legacy image path.
        """

        return await self._media_planning_worker.drain_once()

    async def drain_media_ecology_once(
        self,
        *,
        wake_event_ref: str,
        logical_time: datetime,
        trace_id: str,
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
        self,
        *,
        wake_event_ref: str,
        logical_time: datetime,
        trace_id: str,
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
        self,
        *,
        logical_time: datetime,
        trace_id: str,
        correlation_id: str,
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
        self,
        *,
        proposal_event_ref: str,
        logical_time: datetime,
        trace_id: str,
        correlation_id: str,
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
        self,
        *,
        logical_time: datetime,
        trace_id: str,
        correlation_id: str,
    ) -> MediaSelectionRunResult:
        """Adapt the configured selector to the conductor's small Interface."""

        result = await self.drain_media_selection_once(
            logical_time=logical_time,
            trace_id=trace_id,
            correlation_id=correlation_id,
        )
        if result is None:
            # The conductor is composed only when a selector exists.  This is
            # an invariant breach rather than a reason to quietly skip a
            # candidate or use a legacy image path.
            raise RuntimeError("media preview conductor lost its selection worker")
        return result

    async def _accept_media_preview_selection(
        self,
        *,
        proposal_event_ref: str,
        logical_time: datetime,
        trace_id: str,
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
            "AcceptanceRecorded",
            "MediaOpportunityFrozen",
            "BudgetReserved",
            "ActionAuthorized",
        ]:
            disposition = "planning_authorized"
        else:
            raise RuntimeError("media preview Acceptance produced an unknown event batch")
        return MediaPreviewAcceptanceOutcome(
            disposition=disposition,
            event_ids=commit.event_ids,
        )

    async def drain_media_preview_once(
        self,
        *,
        trace_id: str,
        correlation_id: str,
    ) -> MediaPreviewConductorResult:
        """Advance the bounded candidate → preview-plan prefix once.

        This deep scheduler seam is deliberately unavailable unless the
        composition has injected a selector, acceptance grant/budget and a
        durable planner together.  It neither renders nor sends media.
        """

        if self._media_preview_conductor is None:
            return MediaPreviewConductorResult(
                status="blocked",
                reason_code="media_preview.conductor_unavailable",
            )
        logical_time = await self.current_logical_time()
        if logical_time is None:
            return MediaPreviewConductorResult(
                status="idle",
                reason_code="media_preview.logical_time_unavailable",
            )
        if self._media_request_runtime is not None:
            request = await self._media_request_runtime.advance_once(
                logical_time=logical_time,
                trace_id=trace_id,
                correlation_id=correlation_id,
            )
            if request.handled:
                if request.preview is not None:
                    return request.preview
                return MediaPreviewConductorResult(
                    status=("blocked" if request.status == "blocked" else "in_progress"),
                    reason_code=request.reason_code,
                )
        return await self._media_preview_conductor.advance_once(
            logical_time=logical_time,
            trace_id=trace_id,
            correlation_id=correlation_id,
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
            actor=config.actor,
            source="world-v2:media-selection-acceptance",
            logical_time=logical_time,
            created_at=logical_time,
            trace_id=trace_id,
            correlation_id=correlation_id,
            grant=config.grant,
            account_id=config.account_id,
            amount_limit=config.amount_limit,
        )

    async def expire_media_candidates_once(
        self,
        *,
        logical_time: datetime,
        trace_id: str,
        correlation_id: str,
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
        self,
        *,
        wake_event_ref: str,
        trace_id: str,
        correlation_id: str,
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
        self,
        *,
        approval: MediaAutomaticDeliveryApproval,
        trace_id: str,
        correlation_id: str,
        causation_id: str,
    ) -> MediaAutomaticDeliveryApproval:
        """Record an explicit short-lived operator exception to preview-only media."""

        kwargs = dict(
            approval=approval,
            trace_id=trace_id,
            correlation_id=correlation_id,
            causation_id=causation_id,
        )
        if self._ledger.blocks_event_loop:
            return await asyncio.to_thread(self._media_delivery.approve, **kwargs)
        return self._media_delivery.approve(**kwargs)

    async def authorize_media_delivery(
        self,
        *,
        approval_id: str,
        approval_revision: int,
        actor: str,
        target: str,
        account_id: str,
        amount_limit: int,
        logical_time: datetime,
        trace_id: str,
        correlation_id: str,
    ):
        """Authorize one approved immutable artifact; no preview auto-sends."""

        kwargs = dict(
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
        if self._ledger.blocks_event_loop:
            return await asyncio.to_thread(self._media_delivery.authorize_delivery, **kwargs)
        return self._media_delivery.authorize_delivery(**kwargs)

    async def deliver_approved_media_once(
        self,
        *,
        approval_id: str,
        approval_revision: int,
        actor: str,
        target: str,
        account_id: str,
        amount_limit: int,
        logical_time: datetime,
        trace_id: str,
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
        CharacterInteriorRunResult
        | OutcomeTriggerRunResult
        | RelationshipCommitmentWorkResult
        | InteractionActWorkResult
        | FactTriggerRunResult
        | MemoryWithdrawalReviewRunResult
        | AnchoredRunResult
        | SocialActionRunResult
        | RuntimeOutcome
        | None
    ):
        """Run one separately scheduled mental-state or memory work unit."""

        await self._turns.reconcile_response_expectation_assessment()
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

    async def action_due_projection(self):
        """Return the read-only authority used to rebuild a process timer."""

        return (
            await asyncio.to_thread(self._ledger.project)
            if self._ledger.blocks_event_loop
            else self._ledger.project()
        )

    async def world_health_diagnostics(self) -> dict[str, object]:
        """Return deterministic read-only liveness evidence for health checks.

        The projection supplies current state; exact committed cadence draws
        are read only to report when the next model-owned consideration is
        due.  This seam never deliberates, draws randomness, claims work, or
        appends ledger events.
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
        spontaneous_pending = False
        initiative_state = "waiting_context"
        next_consideration_at: datetime | None = None
        cadence_reason_codes: tuple[str, ...] = ()
        logical_time = projection.logical_time
        if logical_time is not None:
            for thread in projection.threads:
                values = thread.values
                if (
                    values.status == "open"
                    and values.due_window is not None
                    and values.due_window.opens_at <= logical_time < values.due_window.closes_at
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
            contact_on_cooldown = (
                recent_contact is not None
                and (logical_time - recent_contact).total_seconds()
                < policy.contact_cooldown_seconds
            )
            if not contact_on_cooldown:
                # Response expectations remain advisory model context. V3 only
                # opens proactive consideration from real situation changes or
                # the ambient cadence, never from an unanswered expression.
                if projection.message_observations:
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
                        idle_seconds = (logical_time - source_ref.logical_time).total_seconds()
                        profile = SocialInitiativeContextPolicy(policy=policy).compile(
                            projection=projection,
                            logical_time=logical_time,
                        )
                        cadence_reason_codes = profile.reason_codes
                        expected_attempt_id = social_initiative_attempt_id(
                            source_event_ref=source_ref.event_id,
                            profile=profile,
                        )
                        cadence_draw = None
                        for draw_ref in reversed(projection.committed_world_event_refs):
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
                                and all(item.startswith("delay:") for item in draw.candidate_refs)
                            ):
                                cadence_draw = draw
                                break
                        delay_seconds = (
                            int(cadence_draw.selected_candidate_ref.removeprefix("delay:"))
                            if cadence_draw is not None
                            else profile.delay_candidates_seconds[0]
                        )
                        epoch = max(0, int(idle_seconds // delay_seconds) - 1)
                        source_kind = (
                            "ambient_presence"
                            if idle_seconds >= policy.spontaneous_expiry_seconds
                            else "spontaneous_contact"
                        )
                        consideration_id = social_initiative_consideration_id(
                            attempt_id=expected_attempt_id,
                            delay_seconds=delay_seconds,
                            epoch=epoch,
                            source_kind=source_kind,
                        )
                        scheduled_for = source_ref.logical_time + timedelta(
                            seconds=delay_seconds * (epoch + 1)
                        )
                        next_consideration_at = scheduled_for
                        spontaneous_candidate_due = logical_time >= scheduled_for
                        current_processes = tuple(
                            item
                            for item in proactive_processes
                            if item.trigger_ref == "proactive-consideration:" + consideration_id
                        )
                        current = current_processes[-1] if current_processes else None
                        if current is None:
                            initiative_state = (
                                "consideration_due"
                                if spontaneous_candidate_due
                                else "waiting_context"
                            )
                            spontaneous_pending = spontaneous_candidate_due
                        elif current.state != "terminal":
                            initiative_state = "considering"
                        elif str(current.runtime_outcome_ref).startswith(
                            "proactive:deliberation-failed:"
                        ):
                            failures = sum(
                                item.state == "terminal"
                                and str(item.runtime_outcome_ref).startswith(
                                    "proactive:deliberation-failed:"
                                )
                                for item in current_processes
                            )
                            backoff = ProactiveActionRuntime.FAILURE_BACKOFF_SECONDS[
                                min(
                                    max(0, failures - 1),
                                    len(ProactiveActionRuntime.FAILURE_BACKOFF_SECONDS) - 1,
                                )
                            ]
                            failed_at = (
                                current.claim_lease.acquired_at
                                if current.claim_lease is not None
                                else scheduled_for
                            )
                            next_consideration_at = failed_at + timedelta(seconds=backoff)
                            initiative_state = (
                                "retry_wait"
                                if logical_time < next_consideration_at
                                else "consideration_due"
                            )
                            spontaneous_pending = logical_time >= next_consideration_at
                        elif current.runtime_outcome_ref == "proactive:silent":
                            initiative_state = "model_silent"
                            next_consideration_at = source_ref.logical_time + timedelta(
                                seconds=delay_seconds * (epoch + 2)
                            )
                        elif str(current.runtime_outcome_ref).startswith("proactive:authorized:"):
                            action_id = str(current.runtime_outcome_ref).removeprefix(
                                "proactive:authorized:"
                            )
                            action = next(
                                (
                                    item
                                    for item in projection.actions
                                    if item.action_id == action_id
                                ),
                                None,
                            )
                            initiative_state = (
                                "action_pending"
                                if action is not None
                                and action.state
                                not in {
                                    "delivered",
                                    "failed",
                                    "cancelled",
                                    "expired",
                                }
                                else "cooldown"
                            )
                        else:
                            initiative_state = "cooldown"
            for commitment in projection.commitments:
                values = commitment.values
                if (
                    values.status in {"open", "due"}
                    and values.due_window.opens_at <= logical_time < values.due_window.closes_at
                    and not any(
                        action.action_id == values.fulfillment_contract.expected_action_id
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
        last_considered_at = (
            latest.claim_lease.acquired_at
            if latest is not None and latest.claim_lease is not None
            else None
        )
        last_model_decision = None
        last_impulse_summary = None
        last_grounding_outcome = None
        if latest is not None and latest.runtime_outcome_ref == "proactive:silent":
            last_model_decision = "silent"
        elif latest is not None and latest.runtime_outcome_ref == "proactive:grounding-rejected":
            last_model_decision = "grounding_rejected"
        elif latest is not None and str(latest.runtime_outcome_ref).startswith(
            "proactive:deliberation-failed:"
        ):
            last_model_decision = "technical_failure"
            last_reason = None
        elif latest is not None and str(latest.runtime_outcome_ref).startswith(
            "proactive:authorized:"
        ):
            action_id = str(latest.runtime_outcome_ref).removeprefix("proactive:authorized:")
            action = next(
                (item for item in projection.actions if item.action_id == action_id),
                None,
            )
            last_model_decision = (
                "later" if action is not None and action.kind == "followup" else "now"
            )
        if (
            latest is not None
            and latest.source_evidence_ref is not None
            and last_model_decision in {"now", "later", "silent", "grounding_rejected"}
        ):
            decision_audit = next(
                (
                    item
                    for item in reversed(projection.proposal_audits)
                    if item.proposal_kind == "decision"
                    and item.proposal_id.startswith("proposal:proactive:")
                    and item.trigger_ref == latest.source_evidence_ref
                ),
                None,
            )
            if decision_audit is not None:
                try:
                    decision = validate_proposal_envelope(json.loads(decision_audit.proposal_json))
                except (TypeError, ValueError, json.JSONDecodeError):
                    decision = None
                if isinstance(decision, DecisionProposal):
                    last_reason = decision.brief_rationale
                    last_impulse_summary = decision.impulse_summary
                    last_grounding_outcome = decision.proactive_grounding_outcome
        retry_states = proactive_technical_retry_states(projection)
        active_retry = retry_states[-1] if retry_states else None
        consecutive_technical_failures = (
            active_retry.consecutive_technical_failures if active_retry is not None else 0
        )
        last_failure_code = active_retry.last_failure_code if active_retry is not None else None
        if active_retry is not None:
            # Cadence recomputation can legitimately move to a later ambient
            # epoch, but it cannot replace the deadline owned by an unresolved
            # durable technical failure (including situation-triggered ones).
            next_consideration_at = active_retry.next_retry_at
            cadence_reason_codes = ("technical_failure:retry",)
            if active_retry.retry_process_state in {"open", "claimed"}:
                initiative_state = "considering"
                spontaneous_pending = False
            else:
                initiative_state = (
                    "retry_wait"
                    if logical_time is not None and logical_time < active_retry.next_retry_at
                    else "consideration_due"
                )
                spontaneous_pending = (
                    logical_time is not None and logical_time >= active_retry.next_retry_at
                )
        initiative_reliability_24h = _proactive_reliability_health(projection)
        warning_reasons: list[str] = list(initiative_reliability_24h["warning_reasons"])
        if consecutive_technical_failures >= 3:
            warning_reasons.append("repeated_technical_failures")
        if consecutive_technical_failures and initiative_state not in {
            "retry_wait",
            "consideration_due",
            "considering",
        }:
            warning_reasons.append("technical_failure_not_scheduled")
        stimulus_source_count = sum(
            item.event_type in SITUATION_STIMULUS_EVENT_TYPES
            and (logical_time is None or logical_time - item.logical_time <= timedelta(minutes=10))
            for item in projection.committed_world_event_refs
        )
        expectation_status_counts = Counter(
            item.status for item in projection.response_expectation_assessments
        )
        terminal_expectation_plans = {
            item.source_plan_id
            for item in projection.response_expectation_assessments
            if item.status in {"fulfilled", "superseded"}
        }
        pending_expectation_count = sum(
            item.response_expectation is not None
            and item.plan_id not in terminal_expectation_plans
            and (logical_time is None or logical_time < item.response_expectation.expires_at)
            for item in projection.expression_plan_manifests
        )
        proactive_grounding_counts: Counter[str] = Counter()
        for audit_item in projection.proposal_audits:
            if audit_item.proposal_kind != "decision" or not audit_item.proposal_id.startswith(
                "proposal:proactive:"
            ):
                continue
            try:
                audited_decision = validate_proposal_envelope(json.loads(audit_item.proposal_json))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if (
                isinstance(audited_decision, DecisionProposal)
                and audited_decision.proactive_grounding_outcome is not None
            ):
                proactive_grounding_counts[audited_decision.proactive_grounding_outcome] += 1

        # Registration and proposal/audit records prove infrastructure, not
        # that the character has actually lived through anything.
        lived_world_event_types = (frozenset(LIFE_PAYLOAD_MODELS) | {"LifeArcChanged"}) - {
            "NpcRegistered",
            "NpcStatusChanged",
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
        expression_retry = _expression_retry_health(projection)
        plans_by_status = Counter(item.status for item in projection.plans)
        active_plans = tuple(item for item in projection.plans if item.status == "active")
        trigger_counts = Counter(item.process_kind for item in projection.trigger_processes)
        pending_trigger_counts = Counter(
            item.process_kind for item in projection.trigger_processes if item.state != "terminal"
        )
        memory_status_counts = Counter(item.values.status for item in projection.memory_candidates)
        experience_memory_decision_counts: Counter[str] = Counter()
        for experience in projection.experiences:
            located_decision = self._ledger.lookup_event_commit(
                experience_memory_decision_event_id(
                    experience_authority_event_ref=(experience.origin.accepted_event_ref)
                )
            )
            if located_decision is None:
                continue
            decision_event, _ = located_decision
            try:
                experience_memory_decision_counts[
                    ExperienceMemoryDecisionRecordedPayload.model_validate_json(
                        decision_event.payload_json
                    ).decision_kind
                ] += 1
            except ValueError:
                experience_memory_decision_counts["invalid"] += 1
        memory_source_candidate_counts: Counter[str] = Counter()
        for candidate in projection.memory_candidates:
            for source_kind in {
                binding.source_kind for binding in candidate.values.source_bindings
            }:
                memory_source_candidate_counts[source_kind] += 1

        def _activity_view(plan) -> dict[str, object]:
            window = plan.scheduled_window
            return {
                "activity_kind": plan.activity_kind,
                "status": plan.status,
                "location_ref": plan.location_ref,
                "participant_refs": list(plan.participant_refs),
                "window_opens_at": (window.opens_at.isoformat() if window is not None else None),
                "window_closes_at": (window.closes_at.isoformat() if window is not None else None),
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
        calendar_horizon = logical_time + timedelta(days=7) if logical_time is not None else None
        upcoming_calendar = [
            item
            for item in upcoming_planned
            if calendar_horizon is None or item.scheduled_window.opens_at <= calendar_horizon
        ]
        # A bounded viewer-facing trace of the lived day: every plan whose
        # window opened in the last 24 hours, terminal or not.  This is what
        # lets the dashboard show "today" honestly instead of only a single
        # latest-completed item.  Selection keeps the newest 16, but the
        # output is chronological so the viewer reads a day, not a stack.
        recent_day_activities = tuple(
            reversed(
                sorted(
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
                )[:16]
            )
        )
        latest_completed = _latest_transitioned("completed")
        experience_memory_retries = sorted(
            (
                item
                for item in projection.contextual_life_retries
                if item.lane == "experience_memory"
            ),
            key=lambda item: item.failed_at,
            reverse=True,
        )[:8]
        npc_views = npc_identity_views(
            projection,
            content_store=self._life_content_store,
            relationships=npc_relationship_readings(
                projection,
                protagonist_actor_ref=self._companion_actor_ref,
            ),
            reviewed_identity_summaries=self._reviewed_npc_identity_summaries,
        )
        npc_observability = (
            await asyncio.to_thread(
                npc_ecology_health_snapshot,
                projection=projection,
                ledger=self._ledger,
                identity_views=npc_views,
                protagonist_actor_ref=self._companion_actor_ref,
            )
            if self._ledger.blocks_event_loop
            else npc_ecology_health_snapshot(
                projection=projection,
                ledger=self._ledger,
                identity_views=npc_views,
                protagonist_actor_ref=self._companion_actor_ref,
            )
        )
        described_npc_refs = {item.npc_ref for item in npc_views}
        repeatedly_referenced_npc_refs = {
            ref
            for ref in described_npc_refs
            if sum(
                ref in occurrence.participant_refs
                for occurrence in projection.world_occurrences
                if occurrence.status == "settled"
            )
            > 1
        }
        mechanisms = {
            # This is deliberately a read-only projection of what reached the
            # ledger.  It distinguishes "the mechanism has no state yet" from
            # "the mechanism has state but its trigger is still pending".
            "current_situation": {
                "logical_time": (logical_time.isoformat() if logical_time is not None else None),
                "active_activity_count": len(active_plans),
                "active_activity_kinds": sorted({item.activity_kind for item in active_plans}),
                "planned_activity_count": plans_by_status.get("planned", 0),
                "paused_activity_count": plans_by_status.get("paused", 0),
                # Viewer-facing factual life state: what she is doing right
                # now, what is scheduled next, and what she finished last.
                # These are exact ledger projections, never model guesses.
                "active_activities": [_activity_view(item) for item in active_plans],
                "next_planned_activity": (
                    _activity_view(upcoming_planned[0]) if upcoming_planned else None
                ),
                "upcoming_activities": [_activity_view(item) for item in upcoming_calendar],
                "today_activities": [_activity_view(item) for item in recent_day_activities],
                "last_completed_activity": (
                    _activity_view(latest_completed) if latest_completed is not None else None
                ),
            },
            "life_ecology": {
                "plans_by_status": dict(sorted(plans_by_status.items())),
                "world_occurrence_count": occurrence_count,
                "experience_count": experience_count,
                "active_life_arc_count": sum(
                    item.status == "active" for item in projection.life_arcs
                ),
                "life_arcs": [
                    {
                        "arc_id": item.arc_id,
                        "arc_kind": item.arc_kind,
                        "context_pack_ref": item.context_pack_ref,
                        "context_tags": list(item.context_tags),
                        "status": item.status,
                        "started_at": item.started_at.isoformat(),
                        "ends_at": (item.ends_at.isoformat() if item.ends_at is not None else None),
                        "source_event_ref": item.source_event_ref,
                    }
                    for item in projection.life_arcs[-8:]
                ],
                "schedule": (
                    projection.life_ecology_schedule.model_dump(mode="json")
                    if projection.life_ecology_schedule is not None
                    else None
                ),
            },
            "affect": {
                "active_episode_count": sum(
                    item.status == "active" for item in projection.affect_episodes
                ),
                "episode_count": len(projection.affect_episodes),
                "appraisal_count": len(projection.appraisals),
            },
            "memory": {
                "fact_count": sum(item.values.status == "active" for item in projection.facts),
                "candidate_count": len(projection.memory_candidates),
                "active_candidate_count": sum(
                    item.values.status == "active" for item in projection.memory_candidates
                ),
                "candidate_status_counts": dict(sorted(memory_status_counts.items())),
                "candidate_source_counts": dict(sorted(memory_source_candidate_counts.items())),
                "experience_decision_counts": dict(
                    sorted(experience_memory_decision_counts.items())
                ),
                "experience_memory_retry_count": len(experience_memory_retries),
                "experience_memory_retries": [
                    {
                        "source_event_ref": item.source_event_ref,
                        "retry_ordinal": item.retry_ordinal,
                        "consecutive_technical_failures": (item.consecutive_technical_failures),
                        "failure_code": item.failure_code,
                        "failed_at": item.failed_at.isoformat(),
                        "next_retry_at": item.next_retry_at.isoformat(),
                    }
                    for item in experience_memory_retries
                ],
                "last_candidate_transition_at": (
                    max(item.updated_at for item in projection.memory_candidates).isoformat()
                    if projection.memory_candidates
                    else None
                ),
            },
            "relationship": {
                "state_count": len(projection.relationship_states),
                "signal_count": len(projection.relationship_signals),
                "adjustment_count": len(projection.relationship_adjustments),
            },
            "npc": {
                "registered_count": len(projection.npcs),
                "active_count": sum(item.status == "active" for item in projection.npcs),
                "dormant_count": sum(item.status == "dormant" for item in projection.npcs),
                "departed_count": sum(item.status == "departed" for item in projection.npcs),
                "retired_count": sum(item.status == "retired" for item in projection.npcs),
                "subjective_state_count": sum(
                    item.subjective_state is not None for item in projection.npcs
                ),
                "source_closed_descriptor_count": len(npc_views),
                "orphan_descriptor_count": len(projection.npcs) - len(npc_views),
                "repeatedly_referenced_count": len(repeatedly_referenced_npc_refs),
                "last_evolved_at": (
                    max(
                        item.subjective_state.evolved_at
                        for item in projection.npcs
                        if item.subjective_state is not None
                    ).isoformat()
                    if any(item.subjective_state is not None for item in projection.npcs)
                    else None
                ),
                "world_appraisal_count": trigger_counts.get("npc_world_appraisal", 0),
                **npc_observability,
            },
            "triggers": {
                "by_kind": dict(sorted(trigger_counts.items())),
                "pending_by_kind": dict(sorted(pending_trigger_counts.items())),
            },
            "expression_retry": expression_retry,
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
        recall_semantic = (
            self._recall_coordinator.semantic_health()
            if self._recall_coordinator is not None
            else {"enabled": False}
        )
        turn_summary = recall_semantic.get("turn_summary")
        if isinstance(turn_summary, dict):
            recall_semantic["turn_summary"] = {
                **turn_summary,
                "character_outcome": self._last_character_outcome or "unavailable",
            }
        external_perception_downstream = external_perception_downstream_health(projection)
        return {
            "character_interior": self._character_interior.runtime_health(),
            "initiative_last_status": last_status,
            "initiative_last_reason": last_reason,
            "pending_proactive_opportunity_count": len(opportunity_sources - processed_sources)
            + int(spontaneous_pending),
            "pending_proactive_process_count": sum(
                item.state != "terminal" for item in proactive_processes
            ),
            "pending_proactive_action_count": sum(
                item.kind in {"proactive_message", "followup"}
                for item in projection.pending_actions
            ),
            "spontaneous_candidate_due": spontaneous_candidate_due,
            "initiative_state": initiative_state,
            "initiative_last_considered_at": (
                last_considered_at.isoformat() if last_considered_at is not None else None
            ),
            "initiative_last_model_decision": last_model_decision,
            "initiative_last_decision_reason": last_reason,
            "initiative_last_impulse_summary": last_impulse_summary,
            "initiative_last_grounding_outcome": last_grounding_outcome,
            "initiative_grounding_corrected_count": proactive_grounding_counts["corrected"],
            "initiative_grounding_rejected_count": proactive_grounding_counts["rejected"],
            "initiative_stimulus_source_count": stimulus_source_count,
            "initiative_stimulus_merge_window_seconds": 600,
            "initiative_pending_expectation_count": pending_expectation_count,
            "initiative_expectation_status_counts": dict(expectation_status_counts),
            "initiative_next_consideration_at": (
                next_consideration_at.isoformat() if next_consideration_at is not None else None
            ),
            "initiative_cadence_reason_codes": list(cadence_reason_codes),
            "initiative_consecutive_technical_failures": (consecutive_technical_failures),
            "initiative_retry_ordinal": consecutive_technical_failures,
            "initiative_last_failure_code": last_failure_code,
            "initiative_reliability_24h": initiative_reliability_24h,
            "initiative_warning": bool(warning_reasons),
            "initiative_warning_reasons": warning_reasons,
            "external_perception_downstream": external_perception_downstream,
            "life_event_count": life_event_count,
            "occurrence_count": occurrence_count,
            "experience_count": experience_count,
            "starved": not (life_event_count or occurrence_count or experience_count),
            "expression_episode": self._turns.expression_episode_diagnostics(),
            "expression_retry": expression_retry,
            "recall_semantic": recall_semantic,
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
                        "label": MOOD_LABELS.get(component.dimension, component.dimension),
                        "intensity_bp": component.intensity_bp,
                        "anchor_intensity_bp": component.decay_anchor_intensity_bp,
                        "decaying": component.intensity_bp < component.decay_anchor_intensity_bp,
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
            change_phase_readings(tuple(projection.affect_episodes), logical_time=logical_time)
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
                (item for item in projection.facts if item.values.status == "active"),
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
            stored = self._life_content_store.read_exact(content_ref=candidate.values.summary_ref)
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
            memory_items.append(
                {
                    "cue_kind": candidate.values.cue_kind,
                    "status": candidate.values.status,
                    "source_kinds": sorted(
                        {binding.source_kind for binding in candidate.values.source_bindings}
                    ),
                    "summary_excerpt": _clip(stored.text) if stored is not None else None,
                    "salience_highlights": [
                        {"dimension": name, "bp": value} for name, value in highlights
                    ],
                    "retrieval_strength_bp": candidate.values.retrieval_strength_bp,
                    "updated_at": _iso(candidate.updated_at),
                }
            )

        hypothesis_meanings = {
            (appraisal.appraisal_id, hypothesis.hypothesis_id): hypothesis.meaning
            for appraisal in projection.appraisals
            for hypothesis in appraisal.hypotheses
        }
        impressions = []
        for impression in sorted(
            (item for item in projection.private_impressions if item.status == "active"),
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
            impressions.append(
                {
                    "subject_ref": impression.subject_ref,
                    "meanings": meanings,
                    "confidence_bp": impression.confidence_bp,
                    "first_seen": _iso(impression.first_seen),
                    "expiry_condition": impression.expiry_condition,
                }
            )
        reflection_processes = [
            item
            for item in projection.trigger_processes
            if item.process_kind == "private_impression_deliberation"
        ]
        active_reflection = next(
            (item for item in reversed(reflection_processes) if item.state != "terminal"),
            None,
        )
        last_reflection_audit = None
        if active_reflection is not None:
            last_reflection_audit = next(
                (
                    item
                    for item in reversed(projection.model_result_audits)
                    if item.trigger_ref == active_reflection.source_evidence_ref
                ),
                None,
            )
        reflection_failure_code = None
        if last_reflection_audit is not None:
            try:
                reflection_failure_code = json.loads(last_reflection_audit.audit_json).get(
                    "failure_code"
                )
            except (TypeError, json.JSONDecodeError):
                reflection_failure_code = "audit_unreadable"
            if (
                reflection_failure_code is None
                and active_reflection is not None
                and last_reflection_audit.evaluated_world_revision < projection.world_revision
            ):
                reflection_failure_code = "context_advanced"
        reflection_status = (
            "retry_wait"
            if active_reflection is not None
            and active_reflection.state == "claimed"
            and last_reflection_audit is not None
            else active_reflection.state
            if active_reflection is not None
            else "idle"
        )
        reflection_health = {
            "state": reflection_status,
            "pending_count": sum(item.state != "terminal" for item in reflection_processes),
            "last_failure_code": reflection_failure_code,
            "next_retry_at": (
                _iso(active_reflection.claim_lease.expires_at)
                if reflection_status == "retry_wait"
                and active_reflection is not None
                and active_reflection.claim_lease is not None
                else None
            ),
        }

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
        npc_names = {f"npc:{npc.npc_id}": npc.npc_id for npc in projection.npcs}
        npc_by_ref = {f"npc:{npc.npc_id}": npc for npc in projection.npcs}
        npc_states = [
            {
                "npc_ref": reading.npc_ref,
                "npc_id": npc_names.get(reading.npc_ref, reading.npc_ref),
                "closeness_bp": reading.closeness_bp,
                "familiarity_bp": reading.familiarity_bp,
                "settled_shared_count": reading.settled_shared_count,
                "last_shared_at": _iso(reading.last_shared_at),
                "lifecycle_state": npc_by_ref[reading.npc_ref].status,
                "npc_to_protagonist": (
                    npc_by_ref[reading.npc_ref].subjective_state.relationship_to_subject.model_dump(
                        mode="json"
                    )
                    if npc_by_ref[reading.npc_ref].subjective_state is not None
                    else None
                ),
                "npc_private_state_available": (
                    npc_by_ref[reading.npc_ref].subjective_state is not None
                ),
                "npc_last_evolved_at": (
                    _iso(npc_by_ref[reading.npc_ref].subjective_state.evolved_at)
                    if npc_by_ref[reading.npc_ref].subjective_state is not None
                    else None
                ),
            }
            for reading in npc_relationship_readings(
                projection,
                protagonist_actor_ref=self._companion_actor_ref,
            )[:8]
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
                values.summary_ref
                if values is not None
                else getattr(experience, "summary_ref", None)
            )
            occurred_to = (
                values.occurred_to
                if values is not None
                else getattr(experience, "occurred_to", None)
            )
            stored = (
                self._life_content_store.read_exact(content_ref=summary_ref)
                if summary_ref
                else None
            )
            recent_experiences.append(
                {
                    "occurred_to": _iso(occurred_to),
                    "summary_excerpt": _clip(stored.text) if stored is not None else None,
                }
            )
        recent_experiences.reverse()

        return {
            "affect": {"episodes": episodes, "change_phases": change_phases},
            "memory": {"facts": facts, "candidates": memory_items},
            "relationship": {"user_state": user_state, "npc_states": npc_states},
            "life_ecology": {"recent_experiences": recent_experiences},
            "inner": {
                "impressions": impressions,
                "aspirations": aspirations,
                "reflection": reflection_health,
            },
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

    def _close_stores(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._recall_coordinator is not None:
            self._recall_coordinator.close()
        if self._recall_index is not None:
            self._recall_index.close()
        self._life_content_store.close()
        self._expression_payload_store.close()
        self._media_payload_store.close()
        self._ledger.close()

    def close(self) -> None:
        """Close persistent stores for synchronous/offline composition users."""

        self._close_stores()

    async def aclose(self) -> None:
        """Join detached deliberation work before closing its shared resources."""

        close_task = self._close_task
        if close_task is None:
            close_task = asyncio.create_task(
                self._aclose_owned(),
                name="world-v2-turn-application-close",
            )
            self._close_task = close_task
        await asyncio.shield(close_task)

    async def _aclose_owned(self) -> None:
        close_failures: tuple[BaseException, ...] = ()
        if self._owned_deliberations:
            results = await asyncio.gather(
                *(item.aclose() for item in self._owned_deliberations),
                return_exceptions=True,
            )
            close_failures = tuple(
                result for result in results if isinstance(result, BaseException)
            )
        quiescence_waiters = tuple(
            waiter()
            for item in self._owned_deliberations
            if getattr(item, "shutdown_pending_task_count", 0) > 0
            if callable(waiter := getattr(item, "wait_for_shutdown_quiescence", None))
        )
        if quiescence_waiters:
            deferred = asyncio.create_task(
                self._close_stores_after_quiescence(quiescence_waiters),
                name="world-v2-turn-application-deferred-store-close",
            )
            self._deferred_store_close_task = deferred
            deferred.add_done_callback(self._observe_deferred_store_close)
        else:
            self._close_stores()
        if close_failures:
            raise close_failures[0]

    async def _close_stores_after_quiescence(
        self,
        waiters: tuple[Awaitable[None], ...],
    ) -> None:
        try:
            await asyncio.gather(*waiters, return_exceptions=True)
        finally:
            self._close_stores()

    @staticmethod
    def _observe_deferred_store_close(task: asyncio.Task[None]) -> None:
        if not task.cancelled():
            task.exception()

    @property
    def shutdown_pending_task_count(self) -> int:
        """Whether shared stores remain leased to detached deliberation work."""

        task = self._deferred_store_close_task
        return int(task is not None and not task.done())

    async def wait_for_shutdown_quiescence(self) -> None:
        """Wait until any deferred deliberation lease has released the stores."""

        task = self._deferred_store_close_task
        if task is not None:
            await asyncio.shield(task)


def build_sqlite_world_v2_turn_application(
    *,
    path: str | Path,
    config: WorldV2TurnApplicationConfig,
    identities: InboundIdentityResolver,
    router: ModelRouterAdapter,
    character_interior: CharacterInterior,
    transport: PlatformTransport,
    media_transport: MediaProviderTransport | None = None,
    media_planner: MediaPlanner | None = None,
    fact_model: FactDraftChatModel | None = None,
    npc_actor_model: NpcEcologyModel | None = None,
    open_world_event_model: OpenWorldEventModel | None = None,
    life_world_author_model: LifeDevelopmentModel | None = None,
    life_world_author_source_rewriter: LifeDevelopmentModel | None = None,
    life_source_closure_reviewer: LifeDevelopmentModel | None = None,
    perception_input_source: PerceptionInputSource | None = None,
    perception_transport: PerceptionTransport | None = None,
    proactive_source_closure_model: ChatCompletionModel | None = None,
    proactive_candidate_external_proposition_inventory_model: ChatCompletionModel | None = None,
    semantic_recall_embedding: RecallEmbedding | None = None,
    now: datetime,
    projection_authority: ProjectionAuthority | None = None,
    latency_recorder: ProductionLatencyRecorder | None = None,
) -> WorldV2TurnApplication:
    """Build one durable v2 chat lane without importing the legacy application.

    Bootstrap is idempotent and configures the sole ledger-owned chat budget
    before any message can be ingested.  The platform receives only immutable
    dispatch requests; it never receives a runtime or ledger writer.
    """

    if config.media_continuation is not None and media_transport is None:
        raise ValueError("media continuation composition requires durable media transport")
    if config.media_auto_delivery is not None and config.media_continuation is None:
        raise ValueError("media auto-delivery requires the render/inspection continuation")
    # Ordinary inbound semantics have one public author operation.  The
    # returned Deliberation port calls CharacterInterior.consider and carries
    # Expression + Appraisal/Affect in one cursor-pinned DecisionProposal.
    # No Appraisal/Affect/relationship role adapter is extracted here.
    inbound_model = compose_character_interior_inbound_deliberation(
        interior=character_interior,
        world_id=config.world_id,
        actor_ref=config.companion_actor_ref,
    )
    has_proactive_author = "proactive_contact" in set(
        character_interior.runtime_health()["purpose_faculties"]
    )
    # CharacterInterior always owns the protagonist's life faculty, including
    # deployments that do not enable open-life development.  Only a supplied
    # World Author activates that lane; an idle character faculty is not a
    # second/partial composition path.
    open_life_requested = life_world_author_model is not None
    if open_life_requested and config.life_ecology is None:
        raise ValueError("open life development requires Life Ecology")
    if life_source_closure_reviewer is not None and not open_life_requested:
        raise ValueError(
            "life-development source closure requires World Author and Character Model"
        )
    if life_world_author_source_rewriter is not None and not open_life_requested:
        raise ValueError(
            "life-development World Author source rewrite requires open life development"
        )
    # Refuse to start when the vertical registry and the scattered process-kind
    # enumerations disagree: a missing reviewer/owner must fail here by name,
    # not surface later as an Opened-only trigger backlog in the ledger.
    assert_bounded_vertical_coverage()
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

    def read_private_reflection_content(content_ref: str) -> str | None:
        stored = life_content_store.read_exact(content_ref=content_ref)
        return stored.text if stored is not None else None

    expression_payload_store = SQLiteImmutableExpressionPayloadStore(
        path=str(path), world_id=config.world_id
    )
    media_payload_store = SQLiteImmutableMediaPayloadStore(path=str(path), world_id=config.world_id)
    recall_index = SQLiteRecallIndex(
        path=path,
        world_id=config.world_id,
        embedding=FeatureHashRecallEmbedding(),
    )
    cached_semantic_embedding = (
        SQLiteCachedRecallEmbedding(
            path=path,
            world_id=config.world_id,
            delegate=semantic_recall_embedding,
        )
        if semantic_recall_embedding is not None
        else None
    )
    recall_coordinator = RecallCoordinator(
        index=recall_index,
        semantic_embedding=cached_semantic_embedding,
    )
    # CharacterInterior binds its private selective-recall Faculty when the
    # ledger-backed runtime effects are composed below.  The application never
    # receives the underlying expression/appraisal adapters.
    _LOG.warning(
        "world v2 application sidecars ready world=%s duration_ms=%.1f",
        config.world_id,
        (time.perf_counter() - build_started) * 1000,
    )
    try:
        occurrence_content = OccurrenceContentCoordinator(ledger=ledger, store=life_content_store)
        perception_dependencies = (perception_input_source, perception_transport)
        perception_requested = any(item is not None for item in perception_dependencies)
        if perception_requested and not all(item is not None for item in perception_dependencies):
            raise ValueError(
                "perception durable input source and lookup-capable transport must be "
                "explicitly injected together"
            )
        if perception_requested and config.perception_budget_limit <= 0:
            raise ValueError("injected perception lane needs a positive deployment budget")
        if perception_requested and not all(
            callable(getattr(perception_transport, method, None))
            for method in ("dispatched_count_since", "has_result_for_input")
        ):
            raise ValueError(
                "perception transport must expose durable dispatch evidence for "
                "CharacterInterior capability restraint"
            )
        life_seed_catalog = (
            ReviewedLifeSeedCatalog.from_yaml(
                path=config.life_ecology.seed_catalog_path,
                chronology=LocalChronology(config.local_timezone),
            )
            if config.life_ecology is not None
            else None
        )
        if life_seed_catalog is not None:
            for reviewed_npc in life_seed_catalog.reviewed_npcs:
                if reviewed_npc.identity_summary is None:
                    continue
                life_content_store.put_if_absent(
                    StoredLifeContent(
                        content_ref=reviewed_npc.stable_identity_ref,
                        content_kind="provisional_npc_introduction",
                        content_payload_hash=life_content_payload_hash(
                            reviewed_npc.identity_summary
                        ),
                        text=reviewed_npc.identity_summary,
                    )
                )
        if (
            open_life_requested
            and life_seed_catalog is not None
            and life_seed_catalog.story_candidate_role != "legacy_replay_and_fixture"
        ):
            raise ValueError(
                "production story candidates must be marked "
                "legacy_replay_and_fixture before open life development is installed"
            )
        biographical_timeline = (
            BiographicalTimelineConfiguredPayload.from_yaml(
                path=config.life_ecology.seed_catalog_path,
                timezone_name=config.local_timezone,
            )
            if config.life_ecology is not None
            else None
        )
        biographical_context_catalog = (
            BiographicalLifecycleCatalog.from_yaml(
                path=config.life_ecology.seed_catalog_path,
                timezone_name=config.local_timezone,
            )
            if config.life_ecology is not None and biographical_timeline is not None
            else None
        )
        _bootstrap(
            ledger=ledger,
            config=config,
            now=now,
            include_perception=perception_requested,
            include_proactive=has_proactive_author,
            life_seed_catalog=life_seed_catalog,
            biographical_timeline=biographical_timeline,
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
            # Immediate interaction appraisal may use the paired cognition
            # adapter that also owns the eventual expression.  Its Context
            # must therefore prepare the same cursor-pinned recall corpus as
            # the reply lane; otherwise a character-chosen recall during the
            # paired call has no index despite having a valid Capsule.
            recall_coordinator=recall_coordinator,
            biographical_catalog=biographical_context_catalog,
            biographical_timezone_name=(
                config.local_timezone if biographical_context_catalog is not None else None
            ),
            biographical_timeline=biographical_timeline,
            reviewed_npc_identity_summaries=(
                {
                    item.stable_identity_ref: item.identity_summary
                    for item in life_seed_catalog.reviewed_npcs
                    if item.identity_summary is not None
                }
                if life_seed_catalog is not None
                else None
            ),
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
                #
                # 40k, not 32k: the global eviction loop removes the lowest-
                # ranked items first and Facts rank below fresh dialogue, so
                # at 32k the 30-turn recall eval pinned relevant_facts at its
                # two-item deep-eviction floor every turn while six more
                # committed facts stayed invisible.  The provider prompt is
                # the compacted view (~12k), so this only grows the internal
                # verified capsule.
                hard_max_characters=40_000,
                available_capabilities=SliceBudget(
                    max_items=4, max_fields=48, max_characters=1_200
                ),
                action_budget=SliceBudget(max_items=4, max_fields=40, max_characters=1_200),
            ),
            relevance_scope=relevance_scope,
            life_content_store=life_content_store,
            perception_result_reader=perception_transport,
            expression_payload_store=expression_payload_store,
            recall_coordinator=recall_coordinator,
            biographical_catalog=biographical_context_catalog,
            biographical_timezone_name=(
                config.local_timezone if biographical_context_catalog is not None else None
            ),
            biographical_timeline=biographical_timeline,
            reviewed_npc_identity_summaries=(
                {
                    item.stable_identity_ref: item.identity_summary
                    for item in life_seed_catalog.reviewed_npcs
                    if item.identity_summary is not None
                }
                if life_seed_catalog is not None
                else None
            ),
        )
        expression_episode_diagnostics = ExpressionEpisodeDiagnostics(
            mode=config.expression_episode_mode
        )
        chat_deliberation = compose_production_deliberation(
            lane_id="chat_reply",
            router=router,
            main_model=inbound_model,
            main_timeout_seconds=config.interactive_turn_budget_policy.total_seconds,
            quick_timeout_seconds=config.interactive_turn_budget_policy.total_seconds,
            expression_action_kinds=config.expression_action_kinds,
            expression_episode_mode=config.expression_episode_mode,
            expression_episode_diagnostics=expression_episode_diagnostics,
        )
        pinned = PinnedTurnCompiler(
            ledger=ledger,
            capsule_compiler=chat_capsules,
            deliberation=chat_deliberation,
            companion_actor_ref=config.companion_actor_ref,
            latency_recorder=latency,
            # Expression should feel whether she is departing from or
            # returning toward baseline (Change Phase), advisory only.
            change_phase_advisory=True,
            # And how close she currently is to each registered NPC, derived
            # from committed shared history.
            npc_relationship_advisory=True,
            # A pending shared_private invitation she may still need to voice.
            shared_private_invitation_advisory=True,
            pending_expectation_advisory=True,
            recorded_cadence_mode=config.recorded_cadence_mode,
        )
        social_action_worker = SocialActionWorker(
            ledger=ledger,
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
        # One audited inbound DecisionProposal is consumed by the existing
        # deterministic Appraisal/Affect authorities.  There is no second
        # interaction-appraisal PinnedTurn and no background role call for the
        # same Observation.
        appraisal_acceptance = AppraisalAcceptanceRuntime(
            ledger=ledger,
            batch_issuer=issuer,
        )
        appraisal_worker = AppraisalProposalWorker(
            compiler=AppraisalProposalCompiler(
                ledger=ledger,
                world_appraisal_subject_ref=config.companion_actor_ref,
            ),
            acceptance=appraisal_acceptance,
            actor=config.inner_state_settlement_owner,
        )
        outcome_reader = OutcomeCandidateReader(store=life_content_store)
        outcome_deliberation_model = _CharacterInteriorOutcomeMaterializer(
            ledger=ledger,
            candidate_reader=outcome_reader,
            character_interior=character_interior,
            actor_ref=config.companion_actor_ref,
        )
        outcome_acceptance = OutcomeAcceptanceRuntime(
            ledger=ledger,
            batch_issuer=issuer,
        )
        outcome_turn = OutcomeDeliberationTurn(
            ledger=ledger,
            capsule_compiler=capsules,
            deliberation=compose_production_deliberation(
                lane_id="outcome",
                router=router,
                main_model=outcome_deliberation_model,
            ),
            candidate_reader=outcome_reader,
            companion_actor_ref=config.companion_actor_ref,
        )
        outcome_worker = OutcomeProposalWorker(
            compiler=OutcomeProposalCompiler(
                ledger=ledger,
                candidate_reader=outcome_reader,
            ),
            acceptance=outcome_acceptance,
            actor=config.outcome_worker_owner,
        )
        affect_acceptance = AffectAcceptanceRuntime(
            ledger=ledger,
            batch_issuer=issuer,
        )
        immediate_emotion_worker = ImmediateEmotionProposalWorker(
            appraisal_worker=appraisal_worker,
            affect_compiler=AffectProposalCompiler(ledger=ledger),
            affect_acceptance=affect_acceptance,
            actor=config.affect_settlement_owner,
        )
        # The canonical subjective_relationship facet participates in the
        # inbound role call. A second relationship model must not reinterpret
        # that same Observation. Existing accepted relationship signals still
        # use the deterministic adjustment authority below.
        relationship_compiler = RelationshipProposalCompiler(ledger=ledger)
        relationship_acceptance = RelationshipAcceptanceRuntime(
            ledger=ledger,
            batch_issuer=issuer,
        )
        inbound_relationship_worker = InboundRelationshipSignalWorker(
            ledger=ledger,
            compiler=relationship_compiler,
            acceptance=relationship_acceptance,
            owner_id=config.relationship_settlement_owner,
        )
        relationship_commitment_worker = RelationshipCommitmentWorker(
            ledger=ledger,
            compiler=relationship_compiler,
            acceptance=RelationshipCommitmentAcceptanceRuntime(
                ledger=ledger,
                batch_issuer=issuer,
            ),
            actor=config.relationship_settlement_owner,
        )
        interaction_act_worker = InteractionActWorker(
            ledger=ledger,
            compiler=InteractionActProposalCompiler(ledger=ledger),
            acceptance=InteractionActAcceptanceRuntime(
                ledger=ledger,
                batch_issuer=issuer,
            ),
            actor=config.inner_state_settlement_owner,
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
        perception_turn = (
            PinnedTurnCompiler(
                ledger=ledger,
                capsule_compiler=capsules,
                deliberation=compose_character_interior_perception_deliberation(
                    router=router,
                    character_interior=character_interior,
                    input_source=perception_input_source,  # type: ignore[arg-type]
                    dispatch_evidence=perception_transport,  # type: ignore[arg-type]
                    budget_account_id=config.perception_account_id,
                    budget_limit=config.perception_budget_limit,
                    daily_limit=config.perception_budget_limit,
                    local_timezone=config.local_timezone,
                ),
                companion_actor_ref=config.companion_actor_ref,
            )
            if perception_requested
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
            ledger=ledger,
            transport=transport,
            expression_payload_store=expression_payload_store,
            media_payload_store=media_payload_store,
            latency_recorder=latency,
        )
        action_executor: ActionExecutor = platform_executor
        perception_executor = (
            PerceptionActionExecutor(
                inputs=perception_input_source,  # type: ignore[arg-type]
                transport=perception_transport,  # type: ignore[arg-type]
            )
            if perception_transport is not None
            else None
        )
        if media_transport is not None or perception_executor is not None:
            action_executor = RoutedActionExecutor(
                platform=platform_executor,
                media=(
                    ProviderMediaActionExecutor(
                        payloads=MediaSidecarPayloadReader(store=media_payload_store),
                        transport=media_transport,
                    )
                    if media_transport is not None
                    else None
                ),
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
        _bind_production_character_interior(
            interior=character_interior,
            ledger=ledger,
            proactive_capsules=capsules,
            router=router,
            recall_coordinator=recall_coordinator,
            batch_issuer=issuer,
            companion_actor_ref=config.companion_actor_ref,
            reply_target=config.reply_target,
            expression_capabilities=config.expression_capabilities,
            proactive_source_closure_model=proactive_source_closure_model,
            proactive_candidate_external_proposition_inventory_model=(
                proactive_candidate_external_proposition_inventory_model
            ),
            interactive_turn_budget_policy=config.interactive_turn_budget_policy,
            proactive_account_id=config.proactive_account_id,
            proactive_amount_per_action=config.proactive_amount_per_action,
            reply_recovery_policy=config.reply_recovery_policy,
            proactive_worker_owner=config.proactive_worker_owner,
            social_initiative_policy=config.social_initiative_policy,
            private_impression_worker_owner=config.private_impression_worker_owner,
            private_reflection_content_reader=read_private_reflection_content,
            expression_reconsideration_owner=config.expression_reconsideration_owner,
            immediate_emotion_worker=immediate_emotion_worker,
            inner_state_settlement_owner=config.inner_state_settlement_owner,
            silence_appraisal_idle_seconds=config.silence_appraisal_idle_seconds,
            plan_disruption_appraisal_enabled=config.plan_disruption_appraisal_enabled,
            perception_result_reader=perception_transport,
        )
        runtime = WorldRuntime(
            world_id=config.world_id,
            ledger=ledger,
            projection_authority=projection_authority,
            latency_recorder=latency,
            pinned_turn=pinned,
            expression_retry_budget_policy=config.interactive_turn_budget_policy,
            expression_episode_owner=config.expression_episode_owner,
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
            inbound_state_owner=config.inner_state_settlement_owner,
            appraisal_acceptance=appraisal_acceptance,
            appraisal_acceptance_actor=(
                config.inner_state_settlement_owner if appraisal_acceptance is not None else None
            ),
            appraisal_worker=appraisal_worker,
            reflection_scheduler=(
                ReflectionScheduler(
                    ledger=ledger,
                    actor=config.life_ecology.worker_actor,
                )
                if config.life_ecology is not None
                else None
            ),
            immediate_emotion_worker=immediate_emotion_worker,
            inbound_relationship_worker=inbound_relationship_worker,
            relationship_commitment_worker=relationship_commitment_worker,
            interaction_act_worker=interaction_act_worker,
            outcome_deliberation_turn=outcome_turn,
            outcome_worker=outcome_worker,
            outcome_deliberation_owner=config.outcome_worker_owner,
            interaction_fact_owner=(
                config.fact_worker_owner if fact_acceptance is not None else None
            ),
            fact_acceptance=fact_acceptance,
            fact_adapter=(
                FactObservationProposalAdapter(model=fact_model) if fact_model is not None else None
            ),
            fact_memory_lifecycle=(
                FactMemoryCandidateLifecycle(
                    ledger=ledger,
                    actor=config.fact_worker_owner,
                    source="world-v2:fact-memory-lifecycle",
                )
                if fact_acceptance is not None and config.character_memory_enabled
                else None
            ),
            fact_memory_actor_ref=(
                config.companion_actor_ref
                if fact_acceptance is not None and config.character_memory_enabled
                else None
            ),
            character_interior=character_interior,
            memory_withdrawal_review=(
                MemoryWithdrawalReviewRuntime(
                    ledger=ledger,
                    character_interior=character_interior,
                    actor_ref=config.companion_actor_ref,
                    owner_id=config.memory_review_worker_owner,
                )
                if config.character_memory_enabled
                else None
            ),
            affect_acceptance=affect_acceptance,
            affect_acceptance_actor=(
                config.affect_settlement_owner if affect_acceptance is not None else None
            ),
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
            social_action_worker=social_action_worker,
            perception_owner=(
                config.perception_worker_owner if perception_trigger_runtime is not None else None
            ),
            perception_trigger_runtime=perception_trigger_runtime,
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
                    ledger=ledger,
                    execution=media_execution,
                    batch_issuer=issuer,
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
                    ledger=ledger,
                    visual_fact_sidecar=media_payload_store,
                ),
            )
            if ecology_policy is not None
            else None
        )
        media_selection_worker = (
            MediaSelectionWorker(
                ledger=ledger,
                character_interior=character_interior,
                character_actor_ref=config.companion_actor_ref,
                proposal_recorder=MediaSelectionProposalRecorder(ledger=ledger),
                catalog_version=ecology_policy.catalog_version + ":selection.1",
            )
            if ecology_policy is not None and config.media_selection_acceptance is not None
            else None
        )
        media_selection_acceptance = (
            MediaSelectionAcceptanceRuntime(
                ledger=ledger,
                authorizer=MediaOpportunityAuthorizer(
                    ledger=ledger,
                    compiler=MediaEvidenceSnapshotCompiler(
                        ledger=ledger,
                        visual_fact_sidecar=media_payload_store,
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
                character_interior=character_interior,
                owner_actor_ref=config.companion_actor_ref,
                proposal_recorder=ActivityLifecycleProposalRecorder(ledger=ledger),
                acceptance_runtime=ActivityLifecycleAcceptanceRuntime(
                    ledger=ledger, batch_issuer=issuer
                ),
                ecology_catalog_version=config.life_ecology.catalog_version,
            )
            if config.life_ecology is not None
            else None
        )
        life_aftermath = (
            LifeAftermathRuntime(
                ledger=ledger,
                catalog=life_seed_catalog,
                occurrence_content=occurrence_content,
                content_store=life_content_store,
                owner_actor_ref=config.companion_actor_ref,
                character_interior=character_interior,
                actor=config.life_ecology.worker_actor,
                experience_memory_lifecycle=(
                    ExperienceMemoryCandidateLifecycle(
                        ledger=ledger,
                        actor=config.life_ecology.worker_actor,
                        source="world-v2:experience-memory-lifecycle",
                        content_store=life_content_store,
                    )
                    if config.character_memory_enabled
                    else None
                ),
            )
            if config.life_ecology is not None and life_seed_catalog is not None
            else None
        )
        biographical_lifecycle = (
            BiographicalLifecycleRuntime(
                ledger=ledger,
                catalog=life_seed_catalog,
                owner_actor_ref=config.companion_actor_ref,
                content_store=life_content_store,
                actor=config.life_ecology.worker_actor,
            )
            if config.life_ecology is not None and life_seed_catalog is not None
            else None
        )
        npc_initiative = (
            NpcEcology(
                ledger=ledger,
                content_store=life_content_store,
                occurrence_content=occurrence_content,
                # This author speaks as the selected NPC, not as the
                # protagonist.  Keep the low-cost NPC actor outside the
                # protagonist's private CharacterInterior.
                actor_model=npc_actor_model,
                world_author=life_world_author_model,
                protagonist_actor_ref=config.companion_actor_ref,
                catalog=life_seed_catalog,
                worker_actor=config.life_ecology.worker_actor,
            )
            if (
                open_life_requested
                and config.life_ecology is not None
                and config.npc_ecology_enabled
                and npc_actor_model is not None
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
            if (
                not open_life_requested
                and config.life_ecology is not None
                and open_world_event_model is not None
            )
            else None
        )
        life_development = (
            LifeDevelopmentRuntime(
                ledger=ledger,
                content_store=life_content_store,
                world_author=life_world_author_model,
                world_author_source_rewriter=life_world_author_source_rewriter,
                character_interior=character_interior,
                source_closure_reviewer=life_source_closure_reviewer,
                capsule_compiler=capsules,
                capability_manifest_compiler=ProjectionLifeCapabilityManifestCompiler(
                    owner_actor_ref=config.companion_actor_ref,
                    catalog=life_seed_catalog,
                    content_store=life_content_store,
                ),
                owner_actor_ref=config.companion_actor_ref,
                actor=config.life_ecology.worker_actor,
            )
            if (
                open_life_requested
                and config.life_ecology is not None
                and life_seed_catalog is not None
            )
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
                activity_followup=activity_lifecycle,
                aftermath_followup=life_aftermath,
                biographical_followup=biographical_lifecycle,
                life_development_followup=life_development,
                npc_initiative_followup=npc_initiative,
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
            turns=WorldTurnRuntime(
                runtime=runtime,
                identities=identities,
                interactive_turn_budget_policy=config.interactive_turn_budget_policy,
                latency_recorder=latency,
            ),
            character_interior=character_interior,
            companion_actor_ref=config.companion_actor_ref,
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
                planner=composed_media_planner,
                owner_id=config.media_planning_worker_owner,
            ),
            media_ecology=media_ecology,
            life_ecology=life_ecology,
            visual_evidence_author=visual_evidence_author,
            event_ecology_worker_actor=config.event_ecology_worker_actor,
            media_selection_worker=media_selection_worker,
            media_selection_worker_actor=config.media_selection_worker_actor,
            media_candidate_maintenance=MediaCandidateMaintenanceRuntime(ledger=ledger),
            media_candidate_maintenance_actor=config.media_candidate_maintenance_actor,
            character_media_candidates=CharacterMediaCandidateRuntime(ledger=ledger),
            image_evidence=ImageEvidenceDeclarationRuntime(ledger=ledger),
            recipient_scoped_image_evidence=RecipientScopedImageEvidenceDeclarationRuntime(
                ledger=ledger
            ),
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
            reviewed_npc_identity_summaries=(
                {
                    item.stable_identity_ref: item.identity_summary
                    for item in life_seed_catalog.reviewed_npcs
                    if item.identity_summary is not None
                }
                if life_seed_catalog is not None
                else None
            ),
            recall_index=recall_index,
            recall_coordinator=recall_coordinator,
            owned_deliberations=(chat_deliberation,),
        )
    except Exception:
        recall_index.close()
        recall_coordinator.close()
        life_content_store.close()
        expression_payload_store.close()
        media_payload_store.close()
        ledger.close()
        raise


def build_platform_action_executor(
    *,
    ledger: SQLiteWorldLedger,
    transport: PlatformTransport,
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
            platform=payloads,
            media=MediaSidecarPayloadReader(store=media_payload_store),
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
    *,
    ledger: SQLiteWorldLedger,
    config: WorldV2TurnApplicationConfig,
    now: datetime,
    include_perception: bool = False,
    include_proactive: bool = False,
    life_seed_catalog: ReviewedLifeSeedCatalog | None = None,
    biographical_timeline: BiographicalTimelineConfiguredPayload | None = None,
) -> None:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("World v2 bootstrap time must be timezone-aware")
    projection = ledger.project()
    timeline_refs = tuple(
        item
        for item in projection.committed_world_event_refs
        if item.event_type == "BiographicalTimelineConfigured"
    )
    if len(timeline_refs) > 1:
        raise ValueError("World v2 ledger has multiple biographical timeline authorities")
    if timeline_refs:
        if biographical_timeline is None:
            raise ValueError(
                "World v2 ledger has a biographical timeline but deployment configuration does not"
            )
        located = ledger.lookup_event_commit(timeline_refs[0].event_id)
        if located is None:
            raise ValueError("biographical timeline authority is not readable")
        timeline_event, timeline_commit = located
        try:
            recorded_timeline = BiographicalTimelineConfiguredPayload.model_validate_json(
                timeline_event.payload_json
            )
        except ValueError as exc:
            raise ValueError("biographical timeline authority payload is invalid") from exc
        if (
            timeline_commit.world_revision < timeline_refs[0].world_revision
            or timeline_event.event_id not in timeline_commit.event_ids
            or timeline_event.payload_hash != timeline_refs[0].payload_hash
            or recorded_timeline != biographical_timeline
        ):
            raise ValueError("configured biographical timeline differs from ledger authority")
    missing_biographical_timeline = biographical_timeline is not None and not timeline_refs
    accounts = [
        BudgetAccount(
            account_id=config.chat_account_id,
            category="chat",
            window_id=config.chat_window_id,
            limit=config.chat_budget_limit,
        ),
    ]
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
        accounts.append(
            BudgetAccount(
                account_id=config.proactive_account_id,
                category="proactive",
                window_id=config.proactive_window_id,
                limit=config.proactive_budget_limit,
            )
        )
    if config.media_selection_acceptance is not None:
        media = config.media_selection_acceptance
        accounts.append(
            BudgetAccount(
                account_id=media.account_id,
                category="image",
                window_id=media.account_window_id,
                limit=media.account_limit,
            )
        )
    if config.media_continuation is not None:
        continuation = config.media_continuation
        accounts.extend(
            (
                BudgetAccount(
                    account_id=continuation.render_account_id,
                    category="image",
                    window_id=continuation.render_window_id,
                    limit=continuation.render_account_limit,
                ),
                BudgetAccount(
                    account_id=continuation.inspection_account_id,
                    category="image",
                    window_id=continuation.inspection_window_id,
                    limit=continuation.inspection_account_limit,
                ),
            )
        )
    missing: list[BudgetAccount] = []
    for account in accounts:
        existing = next(
            (item for item in projection.budget_accounts if item.account_id == account.account_id),
            None,
        )
        if existing is None:
            missing.append(account)
        elif (
            existing.category != account.category
            or existing.window_id != account.window_id
            or existing.limit != account.limit
        ):
            raise ValueError("existing World v2 budget conflicts with composition config")
    existing_npcs = {item.npc_id: item for item in projection.npcs}
    bootstrap_biography = (
        life_seed_catalog.biographical_context_at(
            instant=projection.logical_time or now,
            life_arcs=projection.life_arcs,
            biographical_coordinates=getattr(projection, "biographical_coordinates", ()),
        )
        if life_seed_catalog is not None
        else None
    )
    missing_npcs = (
        []
        if life_seed_catalog is None
        else [
            item
            for item in life_seed_catalog.reviewed_npcs
            if (
                (
                    not item.requires_all_context_tags
                    or (
                        bootstrap_biography is not None
                        and item.eligible_in_context(bootstrap_biography)
                    )
                )
                and item.npc_id not in existing_npcs
            )
        ]
    )
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
                or (
                    current.status
                    != (
                        "active"
                        if (
                            not item.requires_all_context_tags
                            or (
                                bootstrap_biography is not None
                                and item.eligible_in_context(bootstrap_biography)
                            )
                        )
                        else current.status
                    )
                )
            ):
                raise ValueError("existing reviewed NPC conflicts with life seed catalog")
    if not missing and not missing_npcs and not missing_biographical_timeline:
        return
    if projection.world_revision and not any(
        item.event_type == "WorldStarted" for item in projection.committed_world_event_refs
    ):
        raise ValueError("World v2 ledger has state but no WorldStarted authority")
    events: list[WorldEvent] = []
    world_started = next(
        (
            item
            for item in projection.committed_world_event_refs
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
    if missing_biographical_timeline:
        assert biographical_timeline is not None
        events.append(
            _bootstrap_event(
                config=config,
                now=projection.logical_time or now,
                event_type="BiographicalTimelineConfigured",
                payload=biographical_timeline.model_dump(mode="json"),
            )
        )
    events.extend(
        _bootstrap_event(
            config=config,
            now=now,
            event_type="BudgetAccountConfigured",
            payload={"account": account.model_dump(mode="json")},
        )
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
    *,
    config: WorldV2TurnApplicationConfig,
    now: datetime,
    event_type: str,
    payload: dict[str, object],
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

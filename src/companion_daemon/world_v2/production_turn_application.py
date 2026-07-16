"""Production composition root for the first platform-neutral World v2 turn lane.

This module is intentionally the only place that knows how the persistent
ledger, accepted-batch issuer, deliberation adapters, payload reader and
platform Action executor fit together.  Platform hosts receive the much
smaller :class:`WorldV2TurnApplication` interface and cannot reintroduce a
second Engine or Ledger write path.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from pathlib import Path
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
from .deferred_reply_runtime import DeferredReplyRuntime, ReplyLaterCommand
from .affect_trigger_runtime import AffectTriggerRunResult
from .fact_draft_adapter import FactDraftChatModel, FactObservationProposalAdapter
from .fact_memory_candidate_lifecycle import FactMemoryCandidateLifecycle
from .fact_memory_draft import FactMemoryDraftChatModel, FactMemoryDraftAdapter
from .fact_v2_acceptance_runtime import FactV2AcceptanceRuntime
from .interaction_fact_trigger_runtime import FactTriggerRunResult
from .affect_acceptance_runtime import AffectAcceptanceRuntime
from .affect_deliberation_worker import AffectDeliberationWorker
from .affect_proposal_compiler import AffectProposalCompiler
from .appraisal_acceptance_runtime import AppraisalAcceptanceRuntime
from .appraisal_proposal_compiler import AppraisalProposalCompiler
from .appraisal_proposal_worker import AppraisalProposalWorker
from .interaction_appraisal_trigger_runtime import AppraisalTriggerRunResult
from .outcome_acceptance_runtime import OutcomeAcceptanceRuntime
from .outcome_candidate_reader import OutcomeCandidateReader
from .outcome_deliberation_turn import OutcomeDeliberationTurn
from .outcome_proposal_compiler import OutcomeProposalCompiler
from .outcome_proposal_worker import OutcomeProposalWorker
from .outcome_trigger_runtime import OutcomeTriggerRunResult
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
from .ledger_context_resolver import ContextRelevanceScope, context_capsule_compiler_from_ledger
from .ledger_payload_reader import LedgerAuthorizedPayloadReader
from .life_content_store import SQLiteImmutableLifeContentStore
from .expression_payload_store import SQLiteImmutableExpressionPayloadStore
from .media_v2 import MediaPlanner, SQLiteImmutableMediaPayloadStore
from .event_media_planner_adapter import (
    EventMediaPlannerAdapter,
    EventMediaPlanningResultStore,
)
from .media_evidence_snapshot import MediaEvidenceSnapshotCompiler
from .event_ecology_media import (
    EcologyDrainResult,
    EcologyPolicy,
    EventEcologyMediaCandidateRuntime,
)
from .life_ecology_runtime import (
    LifeEcologyAvailability,
    LifeEcologyRunResult,
    LifeEcologyRuntime,
)
from .life_ecology_trigger_store import LedgerLifeEcologyTriggerStore
from .test_economy import CostProfile
from .media_execution_runtime import MediaExecutionRuntime, MediaExecutionWorker
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
from .media_selection_acceptance_runtime import MediaSelectionProposalRecorder
from .media_selection_acceptance_runtime import MediaSelectionAcceptanceRuntime
from .media_opportunity_authorizer import MediaOpportunityAuthorizer
from .media_selection_draft import MediaSelectionDraftAdapter, MediaSelectionDraftModel
from .media_selection_worker import MediaSelectionRunResult, MediaSelectionWorker
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
from .expression_reconsideration_runtime import (
    ExpressionReconsiderationReviewer,
    ExpressionReconsiderationRunResult,
)
from .pinned_turn import PinnedTurnCompiler
from .settled_world_appraisal_turn import SettledWorldAppraisalTurn
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
from .runtime import WorldRuntime
from .projection import ProjectionAuthority
from .replay_evidence import ReplayEvidence
from .schemas import (
    BudgetAccount,
    ClockObservation,
    CommitResult,
    ExternalObservation,
    OutcomeObservation,
    ProjectionCursor,
    ProjectionRequest,
    ProviderMediaGrantBinding,
    RuntimeOutcome,
    WorldEvent,
    WorldProjection,
)
from .sqlite_ledger import SQLiteWorldLedger
from .world_turn_runtime import InboundIdentityResolver, InboundTurn, WorldTurnRuntime


@dataclass(frozen=True, slots=True)
class LifeEcologyComposition:
    """Explicit production profile for the durable Life Ecology worker.

    A profile owns both the source-bound media policy and the ledger-backed
    trigger identity.  Leaving it absent keeps embedded hosts and fixtures
    visibly unavailable instead of silently creating background world work.
    """

    catalog_version: str
    media_policy: EcologyPolicy
    worker_actor: str = "worker:world-v2:life-ecology"
    lease_seconds: int = 120

    @classmethod
    def production_v1(cls) -> "LifeEcologyComposition":
        return cls(
            catalog_version="life-ecology.1",
            # Production P1 publishes evidence-backed candidates only.  An
            # opportunity, budget reservation, and planning Action can arise
            # only from the separately accepted selection path; the old
            # direct-freeze route remains an explicit migration/test switch.
            media_policy=EcologyPolicy(direct_preview_compatibility=False),
        )

    def __post_init__(self) -> None:
        if not self.catalog_version or not self.worker_actor or self.lease_seconds <= 0:
            raise ValueError("life ecology composition is invalid")


@dataclass(frozen=True, slots=True)
class MediaSelectionAcceptanceComposition:
    """Explicit provider grant and image-budget facts for P1 Acceptance."""

    grant: ProviderMediaGrantBinding
    account_id: str
    amount_limit: int
    actor: str = "worker:world-v2:media-selection-acceptance"

    def __post_init__(self) -> None:
        if not self.account_id or not self.actor or self.amount_limit < 0:
            raise ValueError("media selection acceptance composition is invalid")


@dataclass(frozen=True, slots=True)
class WorldV2TurnApplicationConfig:
    """Composition-owned facts for one persistent companion world."""

    world_id: str
    companion_actor_ref: str
    reply_target: str
    action_pump_owner: str
    chat_account_id: str = "account:world-v2:chat"
    chat_window_id: str = "window:world-v2:chat"
    chat_budget_limit: int = 10_000
    reply_budget_amount: int = 10
    reply_recovery_policy: str = "effect_once"
    appraisal_worker_owner: str = "worker:world-v2:appraisal"
    affect_worker_owner: str = "worker:world-v2:affect"
    fact_worker_owner: str = "worker:world-v2:fact"
    outcome_worker_owner: str = "worker:world-v2:outcome"
    interaction_bid_worker_owner: str = "worker:world-v2:interaction-bid"
    expression_reconsideration_owner: str = "worker:world-v2:expression-reconsideration"
    media_planning_worker_owner: str = "worker:world-v2:media-planning"
    event_ecology_worker_actor: str = "worker:world-v2:event-ecology"
    media_selection_worker_actor: str = "worker:world-v2:media-selection"
    media_candidate_maintenance_actor: str = "worker:world-v2:media-candidate-maintenance"
    media_selection_acceptance: MediaSelectionAcceptanceComposition | None = None
    event_ecology_policy: EcologyPolicy | None = None
    life_ecology: LifeEcologyComposition | None = None
    media_cost_profile: CostProfile | None = None
    tool_account_id: str = "account:world-v2:tool"
    tool_window_id: str = "window:world-v2:tool"
    tool_budget_limit: int = 0
    tool_worker_owner: str = "worker:world-v2:read-only-tool"

    def __post_init__(self) -> None:
        for name in (
            "world_id",
            "companion_actor_ref",
            "reply_target",
            "action_pump_owner",
            "appraisal_worker_owner",
            "affect_worker_owner",
            "fact_worker_owner",
            "outcome_worker_owner",
            "interaction_bid_worker_owner",
            "expression_reconsideration_owner",
            "media_planning_worker_owner",
            "event_ecology_worker_actor",
            "media_selection_worker_actor",
            "media_candidate_maintenance_actor",
            "tool_worker_owner",
        ):
            if not getattr(self, name):
                raise ValueError(f"{name} must not be empty")
        if not self.chat_account_id or not self.chat_window_id:
            raise ValueError("chat account identity must not be empty")
        if not 0 <= self.reply_budget_amount <= self.chat_budget_limit <= 10_000_000:
            raise ValueError("chat budget limits are invalid")
        if not self.reply_recovery_policy:
            raise ValueError("reply recovery policy must not be empty")
        if not self.tool_account_id or not self.tool_window_id or self.tool_budget_limit < 0:
            raise ValueError("tool budget config is invalid")
        if (
            self.life_ecology is not None
            and self.event_ecology_policy is not None
            and self.event_ecology_policy != self.life_ecology.media_policy
        ):
            raise ValueError("life ecology and event ecology policies must agree")


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
        media_planning: MediaPlanningRuntime,
        media_planning_worker: MediaPlanningWorker,
        media_ecology: EventEcologyMediaCandidateRuntime | None,
        life_ecology: LifeEcologyRuntime | None,
        event_ecology_worker_actor: str,
        media_selection_worker: MediaSelectionWorker | None,
        media_selection_worker_actor: str,
        media_candidate_maintenance: MediaCandidateMaintenanceRuntime,
        media_candidate_maintenance_actor: str,
        image_evidence: ImageEvidenceDeclarationRuntime,
        media_selection_acceptance: MediaSelectionAcceptanceRuntime | None,
        media_selection_acceptance_config: MediaSelectionAcceptanceComposition | None,
        media_delivery: MediaDeliveryRuntime,
        occurrence_content: OccurrenceContentCoordinator,
        activity_plans: ActivityPlanRuntime,
        deferred_replies: DeferredReplyRuntime,
    ) -> None:
        self._turns = turns
        self._ledger = ledger
        self._life_content_store = life_content_store
        self._expression_payload_store = expression_payload_store
        self._media_payload_store = media_payload_store
        self.media_execution = media_execution
        self._media_execution_worker = media_execution_worker
        self._media_planning = media_planning
        self._media_planning_worker = media_planning_worker
        self._media_ecology = media_ecology
        self._life_ecology = life_ecology
        self._event_ecology_worker_actor = event_ecology_worker_actor
        self._media_selection_worker = media_selection_worker
        self._media_selection_worker_actor = media_selection_worker_actor
        self._media_candidate_maintenance = media_candidate_maintenance
        self._media_candidate_maintenance_actor = media_candidate_maintenance_actor
        self._image_evidence = image_evidence
        self._media_selection_acceptance = media_selection_acceptance
        self._media_selection_acceptance_config = media_selection_acceptance_config
        self._media_delivery = media_delivery
        self._occurrence_content = occurrence_content
        self._activity_plans = activity_plans
        self._deferred_replies = deferred_replies

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

    async def defer_reply(
        self,
        command: ReplyLaterCommand,
        *,
        logical_time: datetime,
        created_at: datetime,
        trace_id: str,
        causation_id: str,
        correlation_id: str,
    ) -> CommitResult:
        """Open exactly one source-bound reply-later commitment and Action."""
        kwargs = dict(command=command, logical_time=logical_time, created_at=created_at,
                      trace_id=trace_id, causation_id=causation_id, correlation_id=correlation_id)
        if self._ledger.blocks_event_loop:
            return await asyncio.to_thread(self._deferred_replies.defer, **kwargs)
        return self._deferred_replies.defer(**kwargs)

    async def drain_actions_once(self) -> ActionPumpResult | None:
        return await self._turns.drain_actions_once()

    async def drain_action(self, action_id: str) -> ActionPumpResult | None:
        """Drain an ingress-bound Action without globally scheduling siblings."""

        return await self._turns.drain_action(action_id)

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

    async def drain_media_selection_once(
        self, *, logical_time: datetime, trace_id: str, correlation_id: str,
    ) -> MediaSelectionRunResult | None:
        """Ask the bounded P1 selector whether an available candidate matters.

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
        """Accept one pinned P1 proposal only under explicit grant/budget config."""

        runtime, config = self._media_selection_acceptance, self._media_selection_acceptance_config
        if runtime is None or config is None:
            return None
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

    async def drain_background_once(
        self,
    ) -> (
        AppraisalTriggerRunResult
        | OutcomeTriggerRunResult
        | InteractionBidTriggerRunResult
        | AffectTriggerRunResult
        | FactTriggerRunResult
        | ExpressionReconsiderationRunResult
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
    outcome_model: DeliberationModelAdapter | None = None,
    interaction_bid_model: DeliberationModelAdapter | None = None,
    fact_model: FactDraftChatModel | None = None,
    memory_model: FactMemoryDraftChatModel | None = None,
    activity_lifecycle_model: ActivityLifecycleDraftModel | None = None,
    media_selection_model: MediaSelectionDraftModel | None = None,
    read_only_tool_model: DeliberationModelAdapter | None = None,
    read_only_tool_transport: ReadOnlyToolTransport | None = None,
    expression_reconsideration_reviewer: ExpressionReconsiderationReviewer | None = None,
    now: datetime,
    projection_authority: ProjectionAuthority | None = None,
) -> WorldV2TurnApplication:
    """Build one durable v2 chat lane without importing the legacy application.

    Bootstrap is idempotent and configures the sole ledger-owned chat budget
    before any message can be ingested.  The platform receives only immutable
    dispatch requests; it never receives a runtime or ledger writer.
    """

    if media_planner is not None and legacy_event_media_planner is not None:
        raise ValueError("inject either a World v2 media planner or legacy event-media planner, not both")
    issuer = AcceptedLedgerBatchIssuer()
    ledger = SQLiteWorldLedger(path=path, world_id=config.world_id, accepted_batch_issuer=issuer)
    life_content_store = SQLiteImmutableLifeContentStore(path=str(path), world_id=config.world_id)
    expression_payload_store = SQLiteImmutableExpressionPayloadStore(path=str(path), world_id=config.world_id)
    media_payload_store = SQLiteImmutableMediaPayloadStore(path=str(path), world_id=config.world_id)
    try:
        occurrence_content = OccurrenceContentCoordinator(
            ledger=ledger, store=life_content_store
        )
        tool_requested = read_only_tool_model is not None or read_only_tool_transport is not None
        if (read_only_tool_model is None) != (read_only_tool_transport is None):
            raise ValueError("read-only tool model and transport must be explicitly injected together")
        if tool_requested and config.tool_budget_limit <= 0:
            raise ValueError("injected read-only tool lane needs a positive deployment budget")
        _bootstrap(ledger=ledger, config=config, now=now, include_tool=tool_requested)
        capsules = context_capsule_compiler_from_ledger(
            ledger=ledger,
            relevance_scope=ContextRelevanceScope(
                actor_ref=config.companion_actor_ref,
                related_subject_refs=(config.reply_target,),
            ),
            life_content_store=life_content_store,
        )
        pinned = PinnedTurnCompiler(
            ledger=ledger,
            capsule_compiler=capsules,
            deliberation=compose_production_deliberation(
                lane_id="chat_reply",
                router=router,
                main_model=main_model,
                quick_recovery=quick_recovery,
            ),
            companion_actor_ref=config.companion_actor_ref,
            advisory_compiler=advisory_compiler,
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
                ),
                companion_actor_ref=config.companion_actor_ref,
                advisory_compiler=advisory_compiler,
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
        outcome_reader = OutcomeCandidateReader(store=life_content_store)
        outcome_acceptance = (
            OutcomeAcceptanceRuntime(ledger=ledger, batch_issuer=issuer)
            if outcome_model is not None
            else None
        )
        outcome_turn = (
            OutcomeDeliberationTurn(
                ledger=ledger,
                capsule_compiler=capsules,
                deliberation=compose_production_deliberation(
                    lane_id="outcome",
                    router=router,
                    main_model=outcome_model,
                    quick_recovery=outcome_model,
                ),
                candidate_reader=outcome_reader,
                companion_actor_ref=config.companion_actor_ref,
            )
            if outcome_model is not None
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
        affect_acceptance = (
            AffectAcceptanceRuntime(ledger=ledger, batch_issuer=issuer)
            if affect_model is not None
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
        platform_executor = build_platform_action_executor(
            ledger=ledger, transport=transport, expression_payload_store=expression_payload_store,
            media_payload_store=media_payload_store,
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
        if media_transport is not None or tool_executor is not None:
            action_executor = RoutedActionExecutor(
                platform=platform_executor,
                media=(ProviderMediaActionExecutor(
                    payloads=MediaSidecarPayloadReader(store=media_payload_store), transport=media_transport,
                ) if media_transport is not None else None),
                tool=tool_executor,
            )
        runtime = WorldRuntime(
            world_id=config.world_id,
            ledger=ledger,
            projection_authority=projection_authority,
            pinned_turn=pinned,
            reply_policy=ReplyBudgetPolicy(
                account_id=config.chat_account_id,
                amount_limit=config.reply_budget_amount,
                actor=config.companion_actor_ref,
                target=config.reply_target,
                recovery_policy=config.reply_recovery_policy,
            ),
            reply_recorder=MinimalReplyAtomicRecorder(batch_issuer=issuer),
            expression_policy=ExpressionPlanBudgetPolicy(
                account_id=config.chat_account_id,
                amount_limit_per_action=config.reply_budget_amount,
                actor=config.companion_actor_ref,
                allowed_targets=(config.reply_target,),
                recovery_policy=config.reply_recovery_policy,
            ),
            expression_recorder=ExpressionPlanAtomicRecorder(batch_issuer=issuer),
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
            npc_world_appraisal_turn=npc_world_appraisal_turn,
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
            affect_deliberation_owner=(
                config.affect_worker_owner if affect_worker is not None else None
            ),
            affect_worker=affect_worker,
            affect_acceptance=affect_acceptance,
            affect_acceptance_actor=(
                config.affect_worker_owner if affect_acceptance is not None else None
            ),
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
            read_only_tool_owner=(config.tool_worker_owner if tool_trigger_runtime is not None else None),
            read_only_tool_trigger_runtime=tool_trigger_runtime,
            external_result_owner=(config.tool_worker_owner if tool_trigger_runtime is not None else None),
            external_result_deliberator=(NoopToolResultDeliberator() if tool_trigger_runtime is not None else None),
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
                    ledger=ledger, compiler=MediaEvidenceSnapshotCompiler(ledger=ledger),
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
            image_evidence=ImageEvidenceDeclarationRuntime(ledger=ledger),
            media_selection_acceptance=media_selection_acceptance,
            media_selection_acceptance_config=config.media_selection_acceptance,
            media_delivery=MediaDeliveryRuntime(ledger=ledger),
            occurrence_content=occurrence_content,
            activity_plans=ActivityPlanRuntime(
                ledger=ledger,
                owner_actor_ref=config.companion_actor_ref,
            ),
            deferred_replies=DeferredReplyRuntime(
                ledger=ledger,
                actor=config.companion_actor_ref,
            ),
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
) -> ActionExecutor:
    """Bind the platform executor to a read-only accepted-payload capability."""

    payloads = LedgerAuthorizedPayloadReader(
        ledger=ledger, expression_payload_store=expression_payload_store
    )
    if media_payload_store is not None:
        payloads = PlatformAndMediaPayloadReader(
            platform=payloads, media=MediaSidecarPayloadReader(store=media_payload_store),
        )
    return PlatformActionExecutor(payloads=payloads, transport=transport)


def _bootstrap(
    *, ledger: SQLiteWorldLedger, config: WorldV2TurnApplicationConfig, now: datetime,
    include_tool: bool = False,
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
    missing: list[BudgetAccount] = []
    for account in accounts:
        existing = next(
            (item for item in projection.budget_accounts if item.account_id == account.account_id), None
        )
        if existing is None:
            missing.append(account)
        elif existing.category != account.category or existing.window_id != account.window_id or existing.limit != account.limit:
            raise ValueError("existing World v2 budget conflicts with composition config")
    if not missing:
        return
    if projection.world_revision and not any(
        item.event_type == "WorldStarted" for item in projection.committed_world_event_refs
    ):
        raise ValueError("World v2 ledger has state but no WorldStarted authority")
    events: list[WorldEvent] = []
    if projection.world_revision == 0:
        events.append(_bootstrap_event(config=config, now=now, event_type="WorldStarted", payload={}))
    events.extend(
        _bootstrap_event(config=config, now=now, event_type="BudgetAccountConfigured",
                         payload={"account": account.model_dump(mode="json")})
        for account in missing
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
        idempotency_key=f"world-v2:bootstrap:{event_type}:{digest}",
        payload=payload,
    )


__all__ = [
    "LifeEcologyComposition",
    "MediaSelectionAcceptanceComposition",
    "WorldV2TurnApplication",
    "WorldV2TurnApplicationConfig",
    "build_platform_action_executor",
    "build_sqlite_world_v2_turn_application",
]

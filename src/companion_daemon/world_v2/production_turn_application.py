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

from .accepted_ledger_batch import AcceptedLedgerBatchIssuer
from .action_pump import ActionExecutor, ActionPumpResult
from .activity_plan_runtime import (
    ActivityPlanCommand,
    ActivityPlanRuntime,
    ActivityPlanTransitionCommand,
)
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
from .media_v2 import SQLiteImmutableMediaPayloadStore
from .test_economy import CostProfile
from .media_execution_runtime import MediaExecutionRuntime, MediaExecutionWorker
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
from .runtime import WorldRuntime
from .replay_evidence import ReplayEvidence
from .schemas import (
    BudgetAccount,
    ClockObservation,
    CommitResult,
    ExternalObservation,
    OutcomeObservation,
    ProjectionCursor,
    ProjectionRequest,
    RuntimeOutcome,
    WorldEvent,
    WorldProjection,
)
from .sqlite_ledger import SQLiteWorldLedger
from .world_turn_runtime import InboundIdentityResolver, InboundTurn, WorldTurnRuntime


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
    media_cost_profile: CostProfile | None = None

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
        ):
            if not getattr(self, name):
                raise ValueError(f"{name} must not be empty")
        if not self.chat_account_id or not self.chat_window_id:
            raise ValueError("chat account identity must not be empty")
        if not 0 <= self.reply_budget_amount <= self.chat_budget_limit <= 10_000_000:
            raise ValueError("chat budget limits are invalid")
        if not self.reply_recovery_policy:
            raise ValueError("reply recovery policy must not be empty")


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

        return await self.advance(
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
    advisory_compiler: AdvisoryCompiler | None = None,
    appraisal_model: DeliberationModelAdapter | None = None,
    affect_model: DeliberationModelAdapter | None = None,
    outcome_model: DeliberationModelAdapter | None = None,
    interaction_bid_model: DeliberationModelAdapter | None = None,
    fact_model: FactDraftChatModel | None = None,
    memory_model: FactMemoryDraftChatModel | None = None,
    expression_reconsideration_reviewer: ExpressionReconsiderationReviewer | None = None,
    now: datetime,
) -> WorldV2TurnApplication:
    """Build one durable v2 chat lane without importing the legacy application.

    Bootstrap is idempotent and configures the sole ledger-owned chat budget
    before any message can be ingested.  The platform receives only immutable
    dispatch requests; it never receives a runtime or ledger writer.
    """

    issuer = AcceptedLedgerBatchIssuer()
    ledger = SQLiteWorldLedger(path=path, world_id=config.world_id, accepted_batch_issuer=issuer)
    life_content_store = SQLiteImmutableLifeContentStore(path=str(path), world_id=config.world_id)
    expression_payload_store = SQLiteImmutableExpressionPayloadStore(path=str(path), world_id=config.world_id)
    media_payload_store = SQLiteImmutableMediaPayloadStore(path=str(path), world_id=config.world_id)
    try:
        occurrence_content = OccurrenceContentCoordinator(
            ledger=ledger, store=life_content_store
        )
        _bootstrap(ledger=ledger, config=config, now=now)
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
        platform_executor = build_platform_action_executor(
            ledger=ledger, transport=transport, expression_payload_store=expression_payload_store,
            media_payload_store=media_payload_store,
        )
        action_executor: ActionExecutor = platform_executor
        if media_transport is not None:
            action_executor = RoutedActionExecutor(
                platform=platform_executor,
                media=ProviderMediaActionExecutor(
                    payloads=MediaSidecarPayloadReader(store=media_payload_store), transport=media_transport,
                ),
            )
        runtime = WorldRuntime(
            world_id=config.world_id,
            ledger=ledger,
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
            expression_reconsideration_owner=(
                config.expression_reconsideration_owner
                if expression_reconsideration_reviewer is not None
                else None
            ),
            expression_reconsideration_reviewer=expression_reconsideration_reviewer,
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
        return WorldV2TurnApplication(
            turns=WorldTurnRuntime(runtime=runtime, identities=identities),
            ledger=ledger,
            life_content_store=life_content_store,
            expression_payload_store=expression_payload_store,
            media_payload_store=media_payload_store,
            media_execution=media_execution,
            media_execution_worker=media_execution_worker,
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
    *, ledger: SQLiteWorldLedger, config: WorldV2TurnApplicationConfig, now: datetime
) -> None:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("World v2 bootstrap time must be timezone-aware")
    projection = ledger.project()
    account = BudgetAccount(
        account_id=config.chat_account_id,
        category="chat",
        window_id=config.chat_window_id,
        limit=config.chat_budget_limit,
    )
    existing = next(
        (
            item
            for item in projection.budget_accounts
            if item.account_id == account.account_id and item.window_id == account.window_id
        ),
        None,
    )
    if existing is not None:
        if existing.category != account.category or existing.limit != account.limit:
            raise ValueError("existing World v2 chat budget conflicts with composition config")
        return
    if projection.world_revision and not any(
        item.event_type == "WorldStarted" for item in projection.committed_world_event_refs
    ):
        raise ValueError("World v2 ledger has state but no WorldStarted authority")
    events: list[WorldEvent] = []
    if projection.world_revision == 0:
        events.append(_bootstrap_event(config=config, now=now, event_type="WorldStarted", payload={}))
    events.append(
        _bootstrap_event(
            config=config,
            now=now,
            event_type="BudgetAccountConfigured",
            payload={"account": account.model_dump(mode="json")},
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
    "WorldV2TurnApplication",
    "WorldV2TurnApplicationConfig",
    "build_platform_action_executor",
    "build_sqlite_world_v2_turn_application",
]

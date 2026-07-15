"""Production composition root for the first platform-neutral World v2 turn lane.

This module is intentionally the only place that knows how the persistent
ledger, accepted-batch issuer, deliberation adapters, payload reader and
platform Action executor fit together.  Platform hosts receive the much
smaller :class:`WorldV2TurnApplication` interface and cannot reintroduce a
second Engine or Ledger write path.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from pathlib import Path

from .accepted_ledger_batch import AcceptedLedgerBatchIssuer
from .action_pump import ActionExecutor, ActionPumpResult
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
from .advisory_compiler import AdvisoryCompiler
from .deliberation import (
    Deliberation,
    DeliberationModelAdapter,
    ModelRouterAdapter,
    QuickRecoveryAdapter,
)
from .ledger_context_resolver import ContextRelevanceScope, context_capsule_compiler_from_ledger
from .ledger_payload_reader import LedgerAuthorizedPayloadReader
from .life_content_store import SQLiteImmutableLifeContentStore
from .minimal_reply_acceptance import ReplyBudgetPolicy
from .minimal_reply_atomic_recorder import MinimalReplyAtomicRecorder
from .pinned_turn import PinnedTurnCompiler
from .settled_world_appraisal_turn import SettledWorldAppraisalTurn
from .platform_action_executor import PlatformActionExecutor, PlatformTransport
from .runtime import WorldRuntime
from .schemas import (
    BudgetAccount,
    ClockObservation,
    OutcomeObservation,
    RuntimeOutcome,
    WorldEvent,
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
    ) -> None:
        self._turns = turns
        self._ledger = ledger
        self._life_content_store = life_content_store

    async def respond(self, inbound: InboundTurn) -> RuntimeOutcome:
        return await self._turns.respond(inbound)

    async def advance(self, clock: ClockObservation) -> RuntimeOutcome:
        """Advance logical time through the sole World v2 host seam."""

        return await self._turns.advance(clock)

    async def record_outcome_observation(
        self, observation: OutcomeObservation
    ) -> RuntimeOutcome:
        """Record a verified world observation without exposing the ledger."""

        return await self._turns.record_outcome_observation(observation)

    async def drain_actions_once(self) -> ActionPumpResult | None:
        return await self._turns.drain_actions_once()

    async def drain_background_once(
        self,
    ) -> AppraisalTriggerRunResult | OutcomeTriggerRunResult | AffectTriggerRunResult | FactTriggerRunResult | None:
        """Run one separately scheduled mental-state or memory work unit."""

        return await self._turns.drain_background_once()

    def close(self) -> None:
        self._life_content_store.close()
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
    advisory_compiler: AdvisoryCompiler | None = None,
    appraisal_model: DeliberationModelAdapter | None = None,
    affect_model: DeliberationModelAdapter | None = None,
    outcome_model: DeliberationModelAdapter | None = None,
    fact_model: FactDraftChatModel | None = None,
    memory_model: FactMemoryDraftChatModel | None = None,
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
    try:
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
            deliberation=Deliberation(
                router=router, main_model=main_model, quick_recovery=quick_recovery
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
                deliberation=Deliberation(
                    router=router, main_model=appraisal_model, quick_recovery=appraisal_model
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
                deliberation=Deliberation(
                    router=router, main_model=appraisal_model, quick_recovery=appraisal_model
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
                deliberation=Deliberation(
                    router=router, main_model=outcome_model, quick_recovery=outcome_model
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
        affect_acceptance = (
            AffectAcceptanceRuntime(ledger=ledger, batch_issuer=issuer)
            if affect_model is not None
            else None
        )
        affect_turn = (
            PinnedTurnCompiler(
                ledger=ledger,
                capsule_compiler=capsules,
                deliberation=Deliberation(
                    router=router, main_model=affect_model, quick_recovery=affect_model
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
            action_executor=build_platform_action_executor(ledger=ledger, transport=transport),
            action_pump_owner=config.action_pump_owner,
        )
        return WorldV2TurnApplication(
            turns=WorldTurnRuntime(runtime=runtime, identities=identities),
            ledger=ledger,
            life_content_store=life_content_store,
        )
    except Exception:
        life_content_store.close()
        ledger.close()
        raise


def build_platform_action_executor(
    *, ledger: SQLiteWorldLedger, transport: PlatformTransport
) -> ActionExecutor:
    """Bind the platform executor to a read-only accepted-payload capability."""

    return PlatformActionExecutor(payloads=LedgerAuthorizedPayloadReader(ledger=ledger), transport=transport)


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

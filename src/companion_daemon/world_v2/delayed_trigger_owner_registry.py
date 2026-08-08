"""Traceable runtime names used to check delayed-trigger declarations.

These references prove that declared callables, model contracts and Pydantic
fields exist.  They do not prove reachability through a public host and are not
production qualification evidence.  This module contains no execution code.
"""

from __future__ import annotations

from dataclasses import dataclass

from .action_pump import ActionPump
from .activity_lifecycle_worker import ActivityLifecycleWorker
from .deferred_reply_runtime import DeferredReplyRuntime
from .expression_reconsideration import expression_reconsideration_events_for_observation
from .external_world_perception.hub import SQLiteWorldPerceptionHub
from .goal_expiry_runtime import build_due_goal_expiry_events
from .life_aftermath_runtime import LifeAftermathRuntime
from .life_development_runtime import LifeDevelopmentRuntime
from .life_ecology_runtime import LifeEcologyRuntime
from .media_execution_runtime import MediaExecutionWorker
from .media_planning_worker import MediaPlanningWorker
from .npc_ecology import NpcEcology
from .open_world_event_runtime import OpenWorldEventRuntime
from .reflection_scheduler import ReflectionScheduler
from .runtime import WorldRuntime
from .silence_appraisal_trigger import SilenceAppraisalTriggerOpener
from .situation_compiler import SituationCompiler
from .expression_action_capabilities import production_expression_action_kinds
from .delayed_trigger_policies import (
    TECHNICAL_RETRY_BACKOFF_SECONDS,
    TECHNICAL_RETRY_POLICY_VERSION,
)
from .external_world_perception.contracts import SourceHealthSnapshot
from .goal_situation_schemas import V2GoalValues
from .media_provider_grants import PROVIDER_MEDIA_ACTION_KINDS
from .media_delivery_runtime import (
    MEDIA_DELIVERY_ACTION_KIND,
    MediaDeliveryReceiptLifecycle,
    MediaDeliveryRuntime,
)
from .media_v2 import MediaOpportunity
from .platform_host import WorldV2PlatformHost
from .proactive_action import (
    ProactiveActionRuntime,
    ProactiveOpportunity,
    ProactiveTechnicalRetryState,
)
from .qq_c2c_host import QQC2CHost
from .schemas import (
    Action,
    AffectComponentProjection,
    ClaimLease,
    CommitmentValues,
    ExpressionPlanManifestBeatRef,
    LifeEcologyScheduleProjection,
    PlanStateProjection,
    ResponseExpectationAuthority,
    WorldOccurrenceProjection,
)
from .silence_appraisal_trigger import SilenceOpportunity
from .character_interior import structured_role as _structured_role


def _public_seam(owner: type, method: str) -> str:
    surface = getattr(owner, method, None)
    if method.startswith("_") or not callable(surface):
        raise RuntimeError(f"delayed trigger declared seam is not public: {owner}.{method}")
    return f"{owner.__name__}.{method}"


PUBLIC_QUALIFICATION_SEAMS = frozenset(
    {
        _public_seam(QQC2CHost, "inbound_text"),
        _public_seam(QQC2CHost, "inbound_fragment"),
        _public_seam(QQC2CHost, "tick"),
        _public_seam(QQC2CHost, "drain"),
        _public_seam(WorldV2PlatformHost, "receipt"),
    }
)

_INBOUND_CLOCK_SEAMS = (
    _public_seam(QQC2CHost, "inbound_text"),
    _public_seam(QQC2CHost, "inbound_fragment"),
    _public_seam(QQC2CHost, "tick"),
    _public_seam(QQC2CHost, "drain"),
)
_CLOCK_SEAMS = (
    _public_seam(QQC2CHost, "tick"),
    _public_seam(QQC2CHost, "drain"),
)
_INBOUND_CLOCK_RECEIPT_SEAMS = (
    *_INBOUND_CLOCK_SEAMS,
    _public_seam(WorldV2PlatformHost, "receipt"),
)
_CLOCK_RECEIPT_SEAMS = (
    *_CLOCK_SEAMS,
    _public_seam(WorldV2PlatformHost, "receipt"),
)


def _field(model: type, name: str) -> str:
    if name not in model.model_fields:
        raise RuntimeError(f"delayed trigger owner registry references {model.__name__}.{name}")
    return f"{model.__name__}.{name}"


INSTALLED_PROJECTION_DUE_FIELDS = frozenset(
    {
        _field(Action, "not_before"),
        _field(Action, "expires_at"),
        _field(ClaimLease, "expires_at"),
        _field(ProactiveOpportunity, "scheduled_for"),
        _field(ProactiveTechnicalRetryState, "next_retry_at"),
        _field(LifeEcologyScheduleProjection, "next_consideration_at"),
        _field(ExpressionPlanManifestBeatRef, "not_before"),
        _field(ExpressionPlanManifestBeatRef, "expires_at"),
        _field(CommitmentValues, "due_window"),
        _field(ResponseExpectationAuthority, "not_before"),
        _field(ResponseExpectationAuthority, "expires_at"),
        _field(PlanStateProjection, "scheduled_window"),
        _field(WorldOccurrenceProjection, "time_window"),
        _field(AffectComponentProjection, "decay_not_before"),
        _field(SilenceOpportunity, "anchored_at"),
        _field(SilenceOpportunity, "idle_seconds"),
        _field(V2GoalValues, "due_window"),
        _field(SourceHealthSnapshot, "next_refresh_at"),
        _field(MediaOpportunity, "expires_at"),
    }
)

INSTALLED_DELAYED_ACTION_KINDS = frozenset(
    {
        *production_expression_action_kinds(),
        *PROVIDER_MEDIA_ACTION_KINDS,
        MEDIA_DELIVERY_ACTION_KIND,
    }
)


def _installed_contract(purpose: str) -> tuple[str, str]:
    """Resolve the actual installed StructuredRole purpose registry."""

    matches = tuple(
        item for item in _structured_role._BUILTIN_CONTRACTS if item.purpose == purpose
    )
    if len(matches) != 1:
        raise RuntimeError(f"delayed trigger owner has no unique role contract for {purpose}")
    return purpose, matches[0].payload_contract


@dataclass(frozen=True, slots=True)
class DelayedTriggerOwner:
    mechanism_id: str
    runtime_owner: object | None = None
    supporting_runtime_owners: tuple[object, ...] = ()
    producer_dependencies: tuple[str, ...] = ()
    trigger_mode: str = "clock_due"
    public_seams: tuple[str, ...] = ()
    projection_due_fields: tuple[str, ...] = ()
    action_kinds: tuple[str, ...] = ()
    action_owner_bindings: tuple[tuple[str, object], ...] = ()
    model_contract: tuple[str, str] | None = None
    retry_policy: tuple[str, tuple[int, ...]] | None = None

    @property
    def runtime_owners(self) -> tuple[object, ...]:
        primary = () if self.runtime_owner is None else (self.runtime_owner,)
        return (*primary, *self.supporting_runtime_owners)

    @property
    def action_kind_owners(self) -> tuple[tuple[str, object], ...]:
        if self.action_owner_bindings:
            return self.action_owner_bindings
        if self.runtime_owner is None:
            return ()
        return tuple((kind, self.runtime_owner) for kind in self.action_kinds)


_PROACTIVE_CONTRACT = _installed_contract("proactive_contact")
_TECHNICAL_RETRY = (
    TECHNICAL_RETRY_POLICY_VERSION,
    TECHNICAL_RETRY_BACKOFF_SECONDS,
)


DELAYED_TRIGGER_OWNERS: tuple[DelayedTriggerOwner, ...] = (
    *(
        DelayedTriggerOwner(
            mechanism_id=mechanism_id,
            runtime_owner=ProactiveActionRuntime.drain_one,
            public_seams=_INBOUND_CLOCK_SEAMS if mechanism_id == "proactive.event_driven" else _CLOCK_SEAMS,
            projection_due_fields=(_field(ProactiveOpportunity, "scheduled_for"),),
            action_kinds=("proactive_message",),
            model_contract=_PROACTIVE_CONTRACT,
            retry_policy=_TECHNICAL_RETRY,
        )
        for mechanism_id in (
            "proactive.event_driven",
            "proactive.ambient",
            "proactive.post_silent",
        )
    ),
    DelayedTriggerOwner(
        mechanism_id="proactive.technical_retry",
        runtime_owner=ProactiveActionRuntime.drain_one,
        public_seams=_CLOCK_SEAMS,
        projection_due_fields=(_field(ProactiveTechnicalRetryState, "next_retry_at"),),
        action_kinds=("proactive_message",),
        model_contract=_PROACTIVE_CONTRACT,
        retry_policy=_TECHNICAL_RETRY,
    ),
    DelayedTriggerOwner(
        mechanism_id="life.ecology",
        runtime_owner=LifeEcologyRuntime.advance_once,
        public_seams=_INBOUND_CLOCK_SEAMS,
        projection_due_fields=(_field(LifeEcologyScheduleProjection, "next_consideration_at"),),
        retry_policy=_TECHNICAL_RETRY,
    ),
    DelayedTriggerOwner(
        mechanism_id="npc.ecology",
        runtime_owner=NpcEcology.advance_once,
        public_seams=_CLOCK_SEAMS,
        trigger_mode="event_triggered",
    ),
    DelayedTriggerOwner(
        mechanism_id="reflection.life",
        runtime_owner=ReflectionScheduler.open_once,
        public_seams=_INBOUND_CLOCK_SEAMS,
        trigger_mode="event_triggered",
    ),
    DelayedTriggerOwner(
        mechanism_id="memory.candidate_consolidation",
    ),
    DelayedTriggerOwner(
        mechanism_id="action.authorized_due",
        runtime_owner=ActionPump.drain_once,
        supporting_runtime_owners=(
            MediaPlanningWorker.drain_once,
            MediaExecutionWorker.drain_once,
        ),
        public_seams=_CLOCK_RECEIPT_SEAMS,
        projection_due_fields=(
            _field(Action, "not_before"),
            _field(Action, "expires_at"),
            _field(ClaimLease, "expires_at"),
        ),
        action_kinds=tuple(sorted(INSTALLED_DELAYED_ACTION_KINDS)),
        action_owner_bindings=tuple(
            (
                kind,
                MediaPlanningWorker.drain_once
                if kind == "media_planning"
                else MediaExecutionWorker.drain_once
                if kind in {"media_render", "media_inspection", "media_repair"}
                else ActionPump.drain_once,
            )
            for kind in sorted(INSTALLED_DELAYED_ACTION_KINDS)
        ),
    ),
    *(
        DelayedTriggerOwner(
            mechanism_id=mechanism_id,
            runtime_owner=WorldRuntime.advance,
            public_seams=_INBOUND_CLOCK_RECEIPT_SEAMS,
            projection_due_fields=(
                _field(ExpressionPlanManifestBeatRef, "not_before"),
                _field(ExpressionPlanManifestBeatRef, "expires_at"),
            ),
            action_kinds=action_kinds,
        )
        for mechanism_id, action_kinds in (
            ("expression.deferred_reply", ("followup",)),
            (
                "expression.multibeat",
                ("reply", "followup", "reaction", "sticker", "typing"),
            ),
        )
    ),
    DelayedTriggerOwner(
        mechanism_id="expression.reconsideration",
        runtime_owner=expression_reconsideration_events_for_observation,
        public_seams=_INBOUND_CLOCK_RECEIPT_SEAMS,
        trigger_mode="event_triggered",
        model_contract=_installed_contract("expression_reconsideration"),
    ),
    DelayedTriggerOwner(
        mechanism_id="conversation.commitment_due",
        runtime_owner=DeferredReplyRuntime.clock_events,
        public_seams=_CLOCK_RECEIPT_SEAMS,
        projection_due_fields=(_field(CommitmentValues, "due_window"),),
        action_kinds=("followup",),
    ),
    DelayedTriggerOwner(
        mechanism_id="conversation.thread_expiry",
        trigger_mode="replay_only",
    ),
    DelayedTriggerOwner(
        mechanism_id="conversation.expectation_expiry",
        runtime_owner=SituationCompiler.compile,
        public_seams=(
            _public_seam(QQC2CHost, "inbound_text"),
            _public_seam(QQC2CHost, "inbound_fragment"),
            _public_seam(QQC2CHost, "tick"),
        ),
        trigger_mode="derived_formula",
        projection_due_fields=(
            _field(ResponseExpectationAuthority, "not_before"),
            _field(ResponseExpectationAuthority, "expires_at"),
        ),
    ),
    DelayedTriggerOwner(
        mechanism_id="life.activity_occurrence",
        runtime_owner=LifeEcologyRuntime.advance_once,
        producer_dependencies=(
            "life.development",
            "life.open_world_generation",
        ),
        public_seams=_CLOCK_SEAMS,
        projection_due_fields=(
            _field(PlanStateProjection, "scheduled_window"),
            _field(WorldOccurrenceProjection, "time_window"),
        ),
    ),
    DelayedTriggerOwner(
        mechanism_id="life.activity_lifecycle",
        runtime_owner=ActivityLifecycleWorker.advance_once,
        public_seams=_CLOCK_SEAMS,
        trigger_mode="event_triggered",
        model_contract=_installed_contract("activity_lifecycle_choice"),
        retry_policy=_TECHNICAL_RETRY,
    ),
    DelayedTriggerOwner(
        mechanism_id="life.aftermath_outcome",
        runtime_owner=LifeAftermathRuntime.advance_once,
        public_seams=_CLOCK_SEAMS,
        trigger_mode="event_triggered",
        model_contract=_installed_contract("outcome_selection"),
        retry_policy=_TECHNICAL_RETRY,
    ),
    DelayedTriggerOwner(
        mechanism_id="life.aftermath_memory",
        runtime_owner=LifeAftermathRuntime.advance_once,
        public_seams=_CLOCK_SEAMS,
        trigger_mode="event_triggered",
        model_contract=_installed_contract("experience_memory_retention"),
        retry_policy=_TECHNICAL_RETRY,
    ),
    DelayedTriggerOwner(
        mechanism_id="life.development",
        runtime_owner=LifeDevelopmentRuntime.advance_once,
        public_seams=_CLOCK_SEAMS,
        trigger_mode="event_triggered",
        model_contract=_installed_contract("life_development_choice"),
        retry_policy=_TECHNICAL_RETRY,
    ),
    DelayedTriggerOwner(
        mechanism_id="life.open_world_generation",
        runtime_owner=OpenWorldEventRuntime.advance_once,
        public_seams=_CLOCK_SEAMS,
        trigger_mode="event_triggered",
    ),
    DelayedTriggerOwner(
        mechanism_id="affect.decay",
        runtime_owner=WorldRuntime.advance,
        public_seams=_INBOUND_CLOCK_SEAMS,
        projection_due_fields=(_field(AffectComponentProjection, "decay_not_before"),),
    ),
    DelayedTriggerOwner(
        mechanism_id="relationship.silence_aftermath",
        runtime_owner=SilenceAppraisalTriggerOpener.open_once,
        public_seams=_CLOCK_RECEIPT_SEAMS,
        trigger_mode="derived_formula",
        model_contract=_installed_contract("world_stimulus_appraisal"),
    ),
    DelayedTriggerOwner(
        mechanism_id="goal.expiry",
        runtime_owner=build_due_goal_expiry_events,
        public_seams=(_public_seam(QQC2CHost, "tick"),),
        trigger_mode="replay_only",
        projection_due_fields=(_field(V2GoalValues, "due_window"),),
    ),
    DelayedTriggerOwner(
        mechanism_id="perception.refresh_attention",
        runtime_owner=SQLiteWorldPerceptionHub.advance_once,
        public_seams=_CLOCK_SEAMS,
        projection_due_fields=(_field(SourceHealthSnapshot, "next_refresh_at"),),
        model_contract=_installed_contract("external_perception_attention"),
        retry_policy=_TECHNICAL_RETRY,
    ),
    DelayedTriggerOwner(
        mechanism_id="media.planning",
        runtime_owner=MediaPlanningWorker.drain_once,
        public_seams=_CLOCK_SEAMS,
        projection_due_fields=(
            _field(Action, "not_before"),
            _field(Action, "expires_at"),
            _field(MediaOpportunity, "expires_at"),
        ),
        action_kinds=("media_planning",),
    ),
    DelayedTriggerOwner(
        mechanism_id="media.execution",
        runtime_owner=MediaExecutionWorker.drain_once,
        public_seams=_CLOCK_RECEIPT_SEAMS,
        projection_due_fields=(
            _field(Action, "not_before"),
            _field(Action, "expires_at"),
        ),
        action_kinds=("media_render", "media_inspection", "media_repair"),
    ),
    DelayedTriggerOwner(
        mechanism_id="media.delivery",
        runtime_owner=MediaDeliveryRuntime.authorize_delivery,
        supporting_runtime_owners=(
            MediaDeliveryReceiptLifecycle.events_for_terminal_receipt,
        ),
        public_seams=_CLOCK_RECEIPT_SEAMS,
        projection_due_fields=(
            _field(Action, "not_before"),
            _field(Action, "expires_at"),
        ),
        action_kinds=(MEDIA_DELIVERY_ACTION_KIND,),
    ),
)


__all__ = [
    "DELAYED_TRIGGER_OWNERS",
    "INSTALLED_DELAYED_ACTION_KINDS",
    "INSTALLED_PROJECTION_DUE_FIELDS",
    "PUBLIC_QUALIFICATION_SEAMS",
    "DelayedTriggerOwner",
]

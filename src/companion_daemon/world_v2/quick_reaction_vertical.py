"""Quick-reaction vertical, framework edition (BoundedDecisionVertical pilot).

Semantics are preserved byte-for-byte from ``quick_reaction.py`` (the frozen
hand-written implementation, kept in tree for hot rollback): the restraint
policy, mood/activity weight compiler, closed-catalog gate prompt, strict
verdict parser and proposal materialization below are verbatim copies.  All
lifecycle ceremony — audit-prefix dedupe, the recorded draw, the bounded gate
call, audit wrapping, ExpressionPlan acceptance, inline pump dispatch and
private settlement — lives in :mod:`bounded_decision_vertical`.

The shadow-replay suite drives this edition and the hand-written one on the
same input stream and holds every commit to zero byte difference; the
composition root switches between them via ``WORLD_V2_BDV_PILOT_DISABLED``.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Literal

from pydantic import Field

from .action_pump import ActionExecutor
from .bounded_decision_vertical import (
    AuditContextTimes,
    BoundedModelStep,
    DrawContext,
    DrawPlan,
    DrawStep,
    ExpressionAcceptanceBinding,
    InlineIdentityTemplates,
    InlineOnceLifecycle,
    InlineOnceRunResult,
    InlineOnceVerticalWorker,
    InlineSkip,
    ModelStepContext,
    SingleCallAuditTemplate,
    VerticalSpec,
)
from .chat_model_deliberation_adapter import ChatCompletionModel
from .deliberation import ModelRoute
from .expression_draft import ExpressionDraftCapabilities
from .expression_plan_acceptance import ExpressionPlanBudgetPolicy
from .expression_plan_atomic_recorder import ExpressionPlanAtomicRecorder
from .ledger import LedgerPort
from .model_json import extract_json_object_text
from .mood_view import active_mood_intensities
from .proposal_envelope import (
    CanonicalTypedPayload,
    DecisionProposal,
    ProposalActionIntent,
    ProposalEvidenceRef,
    TypedChange,
)
from .schema_core import FrozenModel
from .schemas import Observation, WorldEvent


QUICK_REACTION_PROPOSAL_PREFIX = "proposal:quick-reaction:"

# Kinds that prove the visible text answer already reached the provider; a
# reaction arriving after that is socially meaningless and is abandoned.
_REPLY_ACTION_KINDS = frozenset({"reply"})
_REPLY_DELIVERED_STATES = frozenset({"provider_accepted", "delivered"})

_MAX_MESSAGE_CHARS = 600


def _canonical_json(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    )


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


class QuickReactionPolicy(FrozenModel):
    """Installed restraint constants; none of them select a reaction token."""

    # People do not tap an emoji on every bubble.  This is the pre-context
    # act mass out of 10_000 before mood/activity/backoff adjustments.
    base_act_bp: int = Field(default=3_200, ge=100, le=9_000)
    # A second quick reaction shortly after a previous one reads as spammy;
    # halve the act mass inside this window (projection-derived, no state).
    reaction_backoff_seconds: int = Field(default=600, ge=30, le=86_400)
    # Hard budget for the local semantic gate.  On timeout the lane gives up;
    # it never blocks or degrades the visible reply turn.
    gate_timeout_seconds: float = Field(default=0.8, gt=0.0, le=5.0)


class QuickReactionDecisionProfile(FrozenModel):
    """Explainable act/hold mass compiled from authoritative projections."""

    candidate_weights: dict[Literal["act", "hold"], int]
    reason_codes: tuple[str, ...]


class QuickReactionContextPolicy:
    """Translate mood, current activity, and recent reactions into soft mass."""

    version = "quick-reaction-context.1"

    # Being on the phone already is exactly when a fast tap happens.
    _PHONE_ADJACENT_ACTIVITY_KINDS = frozenset({"leisure.digital_browse"})
    _FOCUSED_ACTIVITY_PREFIXES = ("study.",)

    def __init__(self, *, policy: QuickReactionPolicy) -> None:
        self._policy = policy

    def compile(self, *, projection, logical_time: datetime) -> QuickReactionDecisionProfile:
        act = self._policy.base_act_bp
        reasons: list[str] = []

        mood = active_mood_intensities(projection.affect_episodes)
        approach = max((mood.get(dimension, 0) for dimension in ("warmth", "joy")), default=0)
        guarded = max(
            (
                value
                for dimension, value in mood.items()
                if dimension not in {"warmth", "joy"}
            ),
            default=0,
        )
        if guarded >= 5_000:
            act = act * 2 // 5
            reasons.append("mood:guarded")
        elif guarded >= 3_000:
            act = act * 7 // 10
            reasons.append("mood:reserved")
        elif approach >= 5_000:
            act += 1_200
            reasons.append("mood:approach")
        elif approach >= 3_000:
            act += 600
            reasons.append("mood:warm")
        else:
            reasons.append("mood:neutral")

        active_kinds = tuple(
            plan.activity_kind
            for plan in projection.plans
            if getattr(plan, "status", None) == "active"
        )
        if any(kind in self._PHONE_ADJACENT_ACTIVITY_KINDS for kind in active_kinds):
            act += 1_500
            reasons.append("activity:on_phone")
        elif any(
            kind.startswith(self._FOCUSED_ACTIVITY_PREFIXES) for kind in active_kinds
        ):
            act = act * 2 // 5
            reasons.append("activity:focused")
        elif active_kinds:
            act -= 500
            reasons.append("activity:engaged")
        else:
            reasons.append("activity:available")

        recent_reaction = any(
            action.kind == "reaction"
            and action.state not in {"failed", "cancelled", "expired"}
            and (logical_time - action.logical_time).total_seconds()
            < self._policy.reaction_backoff_seconds
            for action in projection.actions
        )
        if recent_reaction:
            act //= 2
            reasons.append("backoff:recent_reaction")
        else:
            reasons.append("backoff:none")

        act = min(max(act, 200), 8_000)
        return QuickReactionDecisionProfile(
            candidate_weights={"act": act, "hold": 10_000 - act},
            reason_codes=tuple(reasons),
        )


def quick_reaction_attempt_id(
    *,
    source_event_ref: str,
    profile: QuickReactionDecisionProfile,
    policy_version: str = QuickReactionContextPolicy.version,
) -> str:
    """Stable draw identity: same source and compiled profile replay one draw."""

    return "quick-reaction:" + _digest(
        {
            "source_event_ref": source_event_ref,
            "policy_version": policy_version,
            "candidate_weights": profile.candidate_weights,
            "reason_codes": profile.reason_codes,
        }
    )


def _gate_system_prompt(catalog: str) -> str:
    return (
        "你是她的聊天表情回应把关员。她刚收到一条消息，正准备打字回复；在文字回复之前，"
        "她可以先在那条消息上贴一个 QQ 表情回应（类似轻轻点个赞或笑脸）。"
        "你只判断这条消息适不适合贴表情、贴哪一个，不生成任何文字回复。\n"
        "只有内容轻松、正面、有趣或普通日常分享时才贴（比如完成了某件事、趣闻、美食、"
        "可爱的东西、无伤大雅的玩笑）。\n"
        '以下情况一律不贴，输出 {"react":false}：\n'
        "- 对方在倾诉痛苦、难过、疲惫、焦虑、委屈等负面情绪\n"
        "- 对方在生气、抱怨、争执、指责，或语气冷淡疏远、阴阳怪气\n"
        "- 对方提出严肃问题、重要请求、需要认真讨论或安慰的事\n"
        "- 含义模糊、可能是反讽，或者你拿不准\n"
        "先贴笑脸再慢慢回复一条坏消息是灾难性的，所以宁可不贴。\n"
        "表情只能从下面目录的 option_id 里选：\n"
        f"{catalog}\n"
        '只输出一个 JSON 对象：{"react":false} 或 {"react":true,"reaction_id":"<option_id>"}。'
        "禁止 Markdown、解释和任何其他字段。"
    )


def parse_quick_reaction_verdict(raw: object, *, option_ids: frozenset[str]) -> str | None:
    """Strictly extract one in-catalog token; anything else means no reaction."""

    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        value = json.loads(extract_json_object_text(raw))
    except (TypeError, ValueError):
        return None
    if not isinstance(value, dict):
        return None
    react = value.get("react")
    if react is False:
        return None
    if react is not True:
        return None
    reaction_id = value.get("reaction_id")
    if not isinstance(reaction_id, str) or reaction_id not in option_ids:
        return None
    return reaction_id


def materialize_quick_reaction_proposal(
    *,
    observation: Observation,
    observation_event: WorldEvent,
    source_world_revision: int,
    evaluated_world_revision: int,
    reply_target: str,
    provider_message_id: str,
    reaction_id: str,
    capabilities: ExpressionDraftCapabilities,
    recorded_draw_ref: str,
) -> DecisionProposal:
    """Bind one gate verdict to the committed trigger as an inert proposal.

    The proposal id deliberately uses its own ``proposal:quick-reaction:``
    namespace: the main chat lane audits the same trigger, and read models
    that assume one expression audit per trigger (for example the social
    action worker) must be able to tell the two families apart.
    """

    if reaction_id not in {item.option_id for item in capabilities.reaction_options}:
        raise ValueError("quick reaction token is not installed in this deployment")
    identity = _digest(
        {
            "contract": "quick-reaction-materialization.1",
            "capability_profile": capabilities.profile_id,
            "trigger_ref": observation_event.event_id,
            "world_revision": evaluated_world_revision,
            "reply_target": reply_target,
            "reaction_id": reaction_id,
            "recorded_draw_ref": recorded_draw_ref,
        }
    )
    body = _canonical_json(
        {
            "provider_message_id": provider_message_id,
            "reaction_id": reaction_id,
            "version": "expression-reaction.1",
        }
    )
    payload_hash = "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()
    change_id = f"change:quick-reaction:{identity}"
    plan_id = f"plan:quick-reaction:{identity}"
    beat_id = f"beat:quick-reaction:{identity}:1"
    intent_id = f"intent:quick-reaction:{identity}:1"
    payload_ref = f"payload:quick-reaction:{identity}:1"
    change = TypedChange(
        change_id=change_id,
        kind="expression_plan_transition",
        target_id=plan_id,
        transition="accept",
        payload=CanonicalTypedPayload.from_value(
            payload_schema="expression_plan_transition.v1",
            value={
                "plan_id": plan_id,
                "overall_intent": "expression:quick_reaction",
                "ordering_policy": "dependencies",
                "terminal_policy": "settle",
                "beat_drafts": [
                    {
                        "beat_id": beat_id,
                        "inline_text": body,
                        "materialized_payload_ref": payload_ref,
                        "payload_hash": payload_hash,
                        "content_type": "application/vnd.world-v2.reaction+json",
                        "dependency_beat_ids": [],
                        "delay_window": None,
                        "cancel_policy": "cancel-before-dispatch",
                        "reconsider_policy": "reconsider-on-new-observation",
                        "merge_policy": "model-reconsider",
                    }
                ],
                "response_expectation": None,
            },
        ),
    )
    return DecisionProposal(
        proposal_id=f"{QUICK_REACTION_PROPOSAL_PREFIX}{identity}",
        trigger_ref=observation_event.event_id,
        evaluated_world_revision=evaluated_world_revision,
        evidence_refs=(
            ProposalEvidenceRef(
                ref_id=observation.observation_id,
                evidence_kind="observed_message",
                source_world_revision=source_world_revision,
                immutable_hash="sha256:" + observation_event.payload_hash,
            ),
        ),
        proposed_changes=(change,),
        action_intents=(
            ProposalActionIntent(
                intent_id=intent_id,
                kind="reaction",
                layer="external_action",
                target=reply_target,
                payload_ref=payload_ref,
                payload_hash=payload_hash,
                causal_change_id=change_id,
                beat_ref=beat_id,
            ),
        ),
        confidence=6_000,
        brief_rationale="Quick pre-reply reaction on the incoming message.",
        behavior_tendency="respond",
        stance="light_acknowledgement",
        display_strategy="quick_reaction",
        timing_choice="now",
    )


def reply_already_delivered(projection, trigger_audits) -> bool:
    """Projection-pure check that the visible answer already went out."""

    reply_proposal_ids = tuple(
        audit.proposal_id
        for audit in trigger_audits
        if not audit.proposal_id.startswith(QUICK_REACTION_PROPOSAL_PREFIX)
    )
    if not reply_proposal_ids:
        return False
    return any(
        action.kind in _REPLY_ACTION_KINDS
        and action.state in _REPLY_DELIVERED_STATES
        and any(
            action.intent_ref.startswith(proposal_id + ":")
            for proposal_id in reply_proposal_ids
        )
        for action in projection.actions
    )


class QuickReactionOpportunity(FrozenModel):
    """One eligible just-committed inbound message (semantic payload only)."""

    text: str
    reply_target: str
    provider_message_id: str
    source_event_ref: str


def quick_reaction_spec(
    *,
    capabilities: ExpressionDraftCapabilities,
    expression_policy: ExpressionPlanBudgetPolicy,
    policy: QuickReactionPolicy,
    executor_installed: bool,
) -> VerticalSpec:
    """Declare the quick-reaction vertical; every string here is frozen."""

    context = QuickReactionContextPolicy(policy=policy)
    options = tuple(
        (item.option_id, item.label) for item in capabilities.reaction_options
    )
    option_ids = frozenset(option_id for option_id, _label in options)
    catalog = "\n".join(f"- {option_id}（{label}）" for option_id, label in options)

    def opportunity(
        *, observation: Observation, observation_event: WorldEvent
    ) -> QuickReactionOpportunity | InlineSkip:
        text = observation.text
        if not isinstance(text, str) or not text.strip():
            return InlineSkip("quick_reaction.no_text")
        if not executor_installed:
            return InlineSkip("quick_reaction.no_executor")
        reply_context = observation.reply_context or {}
        reply_target = reply_context.get("target")
        provider_message_id = reply_context.get("platform_message_id")
        if not isinstance(reply_target, str) or not reply_target:
            return InlineSkip("quick_reaction.no_reply_target")
        if not isinstance(provider_message_id, str) or not provider_message_id:
            return InlineSkip("quick_reaction.no_provider_message_binding")
        if reply_target not in expression_policy.allowed_targets:
            return InlineSkip("quick_reaction.target_not_allowed")
        return QuickReactionOpportunity(
            text=text,
            reply_target=reply_target,
            provider_message_id=provider_message_id,
            source_event_ref=observation_event.event_id,
        )

    def plan_draw(context_inputs: DrawContext) -> DrawPlan:
        profile = context.compile(
            projection=context_inputs.projection,
            logical_time=context_inputs.logical_time,
        )
        opportunity = context_inputs.opportunity
        assert isinstance(opportunity, QuickReactionOpportunity)
        return DrawPlan(
            attempt_id=quick_reaction_attempt_id(
                source_event_ref=opportunity.source_event_ref, profile=profile
            ),
            candidate_refs=("act", "hold"),
            candidate_weights=profile.candidate_weights,
        )

    def gate_messages(context_inputs: ModelStepContext) -> list[dict[str, str]]:
        opportunity = context_inputs.opportunity
        assert isinstance(opportunity, QuickReactionOpportunity)
        return [
            {"role": "system", "content": _gate_system_prompt(catalog)},
            {
                "role": "user",
                "content": "只判断这条当前用户消息：\n"
                + opportunity.text.strip()[:_MAX_MESSAGE_CHARS],
            },
        ]

    def parse(raw: object, _context: ModelStepContext) -> str | None:
        return parse_quick_reaction_verdict(raw, option_ids=option_ids)

    def compile_proposal(
        *,
        opportunity: QuickReactionOpportunity,
        observation: Observation,
        observation_event: WorldEvent,
        source_world_revision: int,
        evaluated_world_revision: int,
        verdict: str,
        recorded_draw_ref: str,
    ) -> DecisionProposal:
        return materialize_quick_reaction_proposal(
            observation=observation,
            observation_event=observation_event,
            source_world_revision=source_world_revision,
            evaluated_world_revision=evaluated_world_revision,
            reply_target=opportunity.reply_target,
            provider_message_id=opportunity.provider_message_id,
            reaction_id=verdict,
            capabilities=capabilities,
            recorded_draw_ref=recorded_draw_ref,
        )

    def prompt_material(
        _context: ModelStepContext, proposal: DecisionProposal
    ) -> dict[str, object]:
        return {
            "contract": "quick-reaction-gate.1",
            "trigger_ref": proposal.trigger_ref,
            "capability_profile": capabilities.profile_id,
            "options": sorted(option_ids),
        }

    def audit_times(anchor, head) -> AuditContextTimes:
        return AuditContextTimes(
            logical_time=head.logical_time or anchor.observation.logical_time,
            created_at=anchor.observation.created_at,
        )

    return VerticalSpec(
        lane_id="quick_reaction",
        lifecycle=InlineOnceLifecycle(
            identity=InlineIdentityTemplates(namespace="quick-reaction"),
            proposal_prefix=QUICK_REACTION_PROPOSAL_PREFIX,
            abandon_when=reply_already_delivered,
            abandon_status="abandoned_reply_delivered",
            abandon_reason="quick_reaction.reply_already_delivered",
            failure_contract="silent",
        ),
        grammar_lane="quick_reaction",
        opportunity=opportunity,
        draws=(
            DrawStep(
                step_id="act_hold",
                plan=plan_draw,
                catalog_version="quick-reaction-act-hold.1",
                weight_policy_version=QuickReactionContextPolicy.version,
                halt_unless="act",
                halt_outcome="held",
                halt_reason="quick_reaction.draw_hold",
            ),
        ),
        model=BoundedModelStep(
            messages=gate_messages,
            parse=parse,
            timeout_seconds=policy.gate_timeout_seconds,
            temperature=0.0,
            failure_policy="decline_quietly",
            audit=SingleCallAuditTemplate(
                call_namespace="quick-reaction",
                route=ModelRoute(
                    tier="flash",
                    reason_code="quick_reaction_gate",
                    router_version="quick-reaction.1",
                ),
                model_version="quick-reaction-gate.1",
                fallback_model_id="local-quick-reaction",
            ),
            prompt_material=prompt_material,
        ),
        compile=compile_proposal,
        acceptance=ExpressionAcceptanceBinding(
            policy=expression_policy,
            dispatch="inline_pump_with_private_settlement",
            batch_actor="worker_actor",
            include_source_observation=True,
        ),
        audit_times=audit_times,
        random_source="world-v2:quick-reaction-random",
        draw_actor="system:quick-reaction",
        worker_source="world-v2:quick-reaction",
    )


class QuickReactionVerticalWorker:
    """Framework-edition quick-reaction worker (drop-in for the pilot class).

    Constructor signature matches ``QuickReactionWorker`` exactly so the
    composition root switch is a one-symbol change.
    """

    def __init__(
        self,
        *,
        ledger: LedgerPort,
        model: ChatCompletionModel,
        capabilities: ExpressionDraftCapabilities,
        expression_policy: ExpressionPlanBudgetPolicy,
        expression_recorder: ExpressionPlanAtomicRecorder,
        executor: ActionExecutor | None,
        pump_owner: str,
        policy: QuickReactionPolicy | None = None,
        actor: str = "agent:companion",
        source: str = "world-v2:quick-reaction",
    ) -> None:
        if "reaction" not in capabilities.modalities or not capabilities.reaction_options:
            raise ValueError("quick reaction lane requires an installed reaction capability")
        if not pump_owner:
            raise ValueError("quick reaction lane requires a pump owner id")
        if source != "world-v2:quick-reaction":
            raise ValueError("quick reaction identity source string is frozen")
        spec = quick_reaction_spec(
            capabilities=capabilities,
            expression_policy=expression_policy,
            policy=policy or QuickReactionPolicy(),
            executor_installed=executor is not None,
        )
        self._worker = InlineOnceVerticalWorker(
            spec=spec,
            ledger=ledger,
            model=model,
            expression_recorder=expression_recorder,
            executor=executor,
            pump_owner=pump_owner,
            worker_actor=actor,
        )

    @property
    def ledger(self) -> LedgerPort:
        return self._worker.ledger

    async def run_observation(
        self,
        *,
        observation: Observation,
        observation_event: WorldEvent,
        source_world_revision: int,
    ) -> InlineOnceRunResult:
        return await self._worker.run_observation(
            observation=observation,
            observation_event=observation_event,
            source_world_revision=source_world_revision,
        )


__all__ = [
    "QUICK_REACTION_PROPOSAL_PREFIX",
    "QuickReactionContextPolicy",
    "QuickReactionDecisionProfile",
    "QuickReactionOpportunity",
    "QuickReactionPolicy",
    "QuickReactionVerticalWorker",
    "materialize_quick_reaction_proposal",
    "parse_quick_reaction_verdict",
    "quick_reaction_attempt_id",
    "quick_reaction_spec",
    "reply_already_delivered",
]

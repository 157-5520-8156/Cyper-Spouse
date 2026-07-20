"""Same-turn quick reaction lane: a fast, ledger-authorized message reaction.

A person often taps an emoji on the incoming bubble first and types the real
answer afterwards.  This lane reproduces that fast tail of the human response
distribution: while the main deliberation is still compiling, one bounded
local-model decision may authorize exactly one platform ``reaction`` Action on
the message that was just committed.

Discipline, in order:

1. The trigger evidence is the already-committed ``ObservationRecorded`` event;
   nothing here runs before ingress commit.
2. Whether she even feels like reacting is a recorded, mood/activity-weighted
   ``act``/``hold`` draw (RandomAuthority) whose attempt identity binds the
   compiled decision profile, so scheduler or ingress retries replay the same
   draw instead of re-rolling.
3. Only an ``act`` draw consults the bounded local model, which owns the
   social-safety judgement (never react to distress, anger, conflict, or a
   serious request) and picks one token from the closed deployment catalog.
4. A positive verdict becomes an ordinary ``DecisionProposal`` -> audit ->
   ExpressionPlan Acceptance -> authorized Action -> ActionPump dispatch ->
   receipt settlement.  Every failure mode gives up silently: a quick
   reaction is an opportunity, never a debt, so nothing is retried later.

Concurrency contract: the caller (``WorldRuntime.ingest``) must await this
worker *before* it pins the visible-reply cursor.  The whole lane runs inside
the visible turn, so its world-revision writes land before the reply
deliberation evaluates the world — a mid-deliberation write would make the
reply Acceptance legitimately stale.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from datetime import datetime
from typing import Literal

from pydantic import Field

from .action_pump import ActionExecutor, ActionPump
from .chat_model_deliberation_adapter import ChatCompletionModel
from .deliberation import (
    DeliberationResult,
    ModelResultAudit,
    ModelRoute,
    _digest as _deliberation_digest,
    _model_result_ref,
)
from .errors import ConcurrencyConflict, IdempotencyConflict
from .expression_draft import ExpressionDraftCapabilities
from .expression_plan_acceptance import (
    ExpressionPlanAcceptanceError,
    ExpressionPlanBudgetPolicy,
    derive_expression_plan_material,
)
from .expression_plan_atomic_recorder import ExpressionPlanAtomicRecorder
from .ledger import LedgerPort
from .model_json import extract_json_object_text
from .mood_view import active_mood_intensities
from .production_proposal_grammar import (
    ProductionProposalGrammarError,
    production_proposal_grammar,
)
from .proposal_audit import ProposalAuditContext, ProposalAuditRecorder
from .proposal_envelope import (
    CanonicalTypedPayload,
    DecisionProposal,
    ProposalActionIntent,
    ProposalEvidenceRef,
    TypedChange,
)
from .random_authority import RandomAuthority
from .schema_core import FrozenModel
from .schemas import ExternalObservation, Observation, ProjectionCursor, WorldEvent
from .settlement import SettlementPlanner


_LOG = logging.getLogger(__name__)

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


class QuickReactionSemanticGate:
    """Bounded local-model confirmation that owns the social-safety boundary.

    ``assess`` returns an in-catalog reaction token or ``None`` for *every*
    other outcome — an explicit ``{"react": false}``, a timeout, a transport
    failure, or garbage output.  The lane treats all of those identically:
    give up quietly, never delay or fail the visible turn.
    """

    def __init__(
        self,
        *,
        model: ChatCompletionModel,
        options: tuple[tuple[str, str], ...],
        timeout_seconds: float,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("quick reaction gate timeout must be positive")
        if not options:
            raise ValueError("quick reaction gate requires a closed reaction catalog")
        self._model = model
        self._options = options
        self._option_ids = frozenset(option_id for option_id, _label in options)
        self._timeout_seconds = timeout_seconds

    @property
    def option_ids(self) -> frozenset[str]:
        return self._option_ids

    def messages(self, *, text: str) -> list[dict[str, str]]:
        catalog = "\n".join(
            f"- {option_id}（{label}）" for option_id, label in self._options
        )
        return [
            {"role": "system", "content": _gate_system_prompt(catalog)},
            {
                "role": "user",
                "content": "只判断这条当前用户消息：\n" + text.strip()[:_MAX_MESSAGE_CHARS],
            },
        ]

    async def assess(self, *, text: str) -> tuple[str | None, str | None]:
        """Return ``(reaction_id, raw_response)``; both ``None`` on failure."""

        try:
            async with asyncio.timeout(self._timeout_seconds):
                raw = await self._model.complete(self.messages(text=text), temperature=0.0)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _LOG.warning(
                "quick reaction gate unavailable: %s: %s",
                type(exc).__name__,
                str(exc)[:240],
            )
            return None, None
        return (
            parse_quick_reaction_verdict(raw, option_ids=self._option_ids),
            raw if isinstance(raw, str) else None,
        )


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


class QuickReactionRunResult(FrozenModel):
    status: Literal[
        # A reaction was authorized and its dispatch settled with the provider.
        "reacted",
        # Authorized and dispatch attempted, but the provider outcome is not a
        # settled acceptance; recovery owns the Action from here.
        "dispatch_incomplete",
        # The recorded act/hold draw held the impulse.
        "held",
        # The local model said this message should not get a reaction.
        "declined",
        # The lane was not applicable (no text, no provider binding, ...).
        "skipped",
        # This observation already has a quick-reaction audit.
        "duplicate",
        # The visible text reply already reached the provider; a late
        # reaction is meaningless.
        "abandoned_reply_delivered",
        # Any infrastructure failure; the lane gave up silently.
        "failed",
    ]
    reaction_id: str | None = None
    proposal_id: str | None = None
    action_id: str | None = None
    reason_code: str | None = None
    dispatch_status: str | None = None
    # True whenever this run committed anything (draw, audit, acceptance,
    # lifecycle, receipt); the caller must re-project before pinning cursors.
    ledger_advanced: bool = False
    gate_ms: float | None = None
    total_ms: float | None = None


class QuickReactionWorker:
    """One inline, bounded, effect-once quick-reaction attempt per Observation.

    The worker is invoked from inside the visible ingest turn (which holds the
    runtime lock), so it must never call back into a locked ``WorldRuntime``
    seam.  Dispatch receipts settle through a private ``SettlementPlanner``
    with the same commit identities as ``WorldRuntime.settle``, so a later
    generic recovery of the same receipt converges idempotently.
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
        self._ledger = ledger
        self._policy = policy or QuickReactionPolicy()
        self._context = QuickReactionContextPolicy(policy=self._policy)
        self._gate = QuickReactionSemanticGate(
            model=model,
            options=tuple(
                (item.option_id, item.label) for item in capabilities.reaction_options
            ),
            timeout_seconds=self._policy.gate_timeout_seconds,
        )
        self._model = model
        self._capabilities = capabilities
        self._expression_policy = expression_policy
        self._expression_recorder = expression_recorder
        self._executor = executor
        self._pump_owner = pump_owner
        self._actor = actor
        self._source = source
        self._random = RandomAuthority(ledger=ledger, source="world-v2:quick-reaction-random")
        self._audits = ProposalAuditRecorder(ledger=ledger)
        self._grammar = production_proposal_grammar("quick_reaction")
        self._settlement = SettlementPlanner(world_id=ledger.world_id)

    @property
    def ledger(self) -> LedgerPort:
        return self._ledger

    async def run_observation(
        self,
        *,
        observation: Observation,
        observation_event: WorldEvent,
        source_world_revision: int,
    ) -> QuickReactionRunResult:
        """Attempt one quick reaction; every failure is silent by contract."""

        started = time.perf_counter()
        try:
            return await self._run(
                observation=observation,
                observation_event=observation_event,
                source_world_revision=source_world_revision,
                started=started,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the lane must never fail a turn
            _LOG.warning(
                "quick reaction lane gave up trace=%s error=%s: %s",
                observation.trace_id,
                type(exc).__name__,
                str(exc)[:240],
            )
            return QuickReactionRunResult(
                status="failed",
                reason_code=f"quick_reaction.{type(exc).__name__}",
                # Conservative: a failure mid-pipeline may have committed.
                ledger_advanced=True,
                total_ms=(time.perf_counter() - started) * 1000,
            )

    async def _run(
        self,
        *,
        observation: Observation,
        observation_event: WorldEvent,
        source_world_revision: int,
        started: float,
    ) -> QuickReactionRunResult:
        def done(
            status: str,
            *,
            reason: str | None = None,
            reaction_id: str | None = None,
            proposal_id: str | None = None,
            action_id: str | None = None,
            dispatch_status: str | None = None,
            ledger_advanced: bool = False,
            gate_ms: float | None = None,
        ) -> QuickReactionRunResult:
            return QuickReactionRunResult(
                status=status,  # type: ignore[arg-type]
                reason_code=reason,
                reaction_id=reaction_id,
                proposal_id=proposal_id,
                action_id=action_id,
                dispatch_status=dispatch_status,
                ledger_advanced=ledger_advanced,
                gate_ms=gate_ms,
                total_ms=(time.perf_counter() - started) * 1000,
            )

        text = observation.text
        if not isinstance(text, str) or not text.strip():
            return done("skipped", reason="quick_reaction.no_text")
        if self._executor is None:
            return done("skipped", reason="quick_reaction.no_executor")
        reply_context = observation.reply_context or {}
        reply_target = reply_context.get("target")
        provider_message_id = reply_context.get("platform_message_id")
        if not isinstance(reply_target, str) or not reply_target:
            return done("skipped", reason="quick_reaction.no_reply_target")
        if not isinstance(provider_message_id, str) or not provider_message_id:
            return done("skipped", reason="quick_reaction.no_provider_message_binding")
        if reply_target not in self._expression_policy.allowed_targets:
            return done("skipped", reason="quick_reaction.target_not_allowed")

        projection = await self._project()
        logical_time = projection.logical_time
        if logical_time is None:
            return done("skipped", reason="quick_reaction.no_logical_time")

        trigger_audits = tuple(
            audit
            for audit in projection.proposal_audits
            if audit.trigger_ref == observation_event.event_id
        )
        if any(
            audit.proposal_id.startswith(QUICK_REACTION_PROPOSAL_PREFIX)
            for audit in trigger_audits
        ):
            return done("duplicate", reason="quick_reaction.already_attempted")
        if self._reply_already_delivered(projection, trigger_audits):
            return done(
                "abandoned_reply_delivered", reason="quick_reaction.reply_already_delivered"
            )

        profile = self._context.compile(projection=projection, logical_time=logical_time)
        attempt_id = quick_reaction_attempt_id(
            source_event_ref=observation_event.event_id, profile=profile
        )
        draw_kwargs = dict(
            attempt_id=attempt_id,
            candidate_refs=("act", "hold"),
            candidate_weights=profile.candidate_weights,
            weight_policy_version=self._context.version,
            catalog_version="quick-reaction-act-hold.1",
            logical_time=logical_time,
            seed_instant=observation_event.logical_time,
            actor="system:quick-reaction",
            trace_id=observation.trace_id,
            correlation_id=observation.correlation_id,
        )
        draw = (
            await asyncio.to_thread(self._random.draw, **draw_kwargs)
            if self._ledger.blocks_event_loop
            else self._random.draw(**draw_kwargs)
        )
        if draw.selected_candidate_ref != "act":
            return done("held", reason="quick_reaction.draw_hold", ledger_advanced=True)

        gate_started = time.perf_counter()
        reaction_id, raw_response = await self._gate.assess(text=text)
        gate_ms = (time.perf_counter() - gate_started) * 1000
        if reaction_id is None:
            return done(
                "declined",
                reason=(
                    "quick_reaction.gate_declined"
                    if raw_response is not None
                    else "quick_reaction.gate_unavailable"
                ),
                ledger_advanced=True,
                gate_ms=gate_ms,
            )
        assert raw_response is not None

        head = await self._project()
        cursor = ProjectionCursor(
            world_revision=head.world_revision,
            deliberation_revision=head.deliberation_revision,
            ledger_sequence=head.ledger_sequence,
        )
        proposal = materialize_quick_reaction_proposal(
            observation=observation,
            observation_event=observation_event,
            source_world_revision=source_world_revision,
            evaluated_world_revision=cursor.world_revision,
            reply_target=reply_target,
            provider_message_id=provider_message_id,
            reaction_id=reaction_id,
            capabilities=self._capabilities,
            recorded_draw_ref=draw.draw_id,
        )
        try:
            self._grammar.validate(proposal)
        except ProductionProposalGrammarError:
            return done(
                "failed",
                reason="quick_reaction.grammar_rejected",
                ledger_advanced=True,
                gate_ms=gate_ms,
            )
        result = self._deliberation_result(proposal=proposal, raw_response=raw_response)
        context = ProposalAuditContext(
            world_id=self._ledger.world_id,
            trigger_ref=observation_event.event_id,
            logical_time=head.logical_time or observation.logical_time,
            created_at=observation.created_at,
            actor=self._actor,
            source=self._source,
            trace_id=observation.trace_id,
            causation_id=observation_event.event_id,
            correlation_id=observation.correlation_id,
            evaluated_world_revision=cursor.world_revision,
            expected_commit_world_revision=cursor.world_revision,
            expected_deliberation_revision=cursor.deliberation_revision,
        )
        try:
            if self._ledger.blocks_event_loop:
                await asyncio.to_thread(self._audits.record, result, context)
            else:
                self._audits.record(result, context)
        except (ConcurrencyConflict, IdempotencyConflict):
            return done(
                "failed",
                reason="quick_reaction.audit_race",
                ledger_advanced=True,
                gate_ms=gate_ms,
            )

        after_audit = await self._project()
        audit = next(
            (
                item
                for item in after_audit.proposal_audits
                if item.proposal_id == proposal.proposal_id
            ),
            None,
        )
        account = next(
            (
                item
                for item in after_audit.budget_accounts
                if item.account_id == self._expression_policy.account_id
            ),
            None,
        )
        if audit is None or account is None:
            return done(
                "failed",
                reason="quick_reaction.acceptance_material_unavailable",
                ledger_advanced=True,
                gate_ms=gate_ms,
            )
        acceptance_cursor = ProjectionCursor(
            world_revision=after_audit.world_revision,
            deliberation_revision=after_audit.deliberation_revision,
            ledger_sequence=after_audit.ledger_sequence,
        )
        try:
            material = derive_expression_plan_material(
                audit=audit,
                cursor=acceptance_cursor,
                world_id=self._ledger.world_id,
                policy=self._expression_policy,
                account=account,
                logical_time=after_audit.logical_time or observation.logical_time,
                created_at=observation.created_at,
                trace_id=observation.trace_id,
                correlation_id=observation.correlation_id,
                source_observation=observation,
            )
        except ExpressionPlanAcceptanceError as exc:
            return done(
                "failed", reason=exc.code, ledger_advanced=True, gate_ms=gate_ms
            )
        batch = self._expression_recorder.prepare_batch(
            acceptance_id=f"acceptance:quick-reaction:{proposal.proposal_id}",
            material=material,
            actor=self._actor,
            source=self._source,
        )
        try:
            if self._ledger.blocks_event_loop:
                await asyncio.to_thread(
                    self._ledger.commit_accepted, batch, expected_cursor=material.cursor
                )
            else:
                self._ledger.commit_accepted(batch, expected_cursor=material.cursor)
        except (ConcurrencyConflict, IdempotencyConflict):
            return done(
                "failed",
                reason="quick_reaction.acceptance_race",
                ledger_advanced=True,
                gate_ms=gate_ms,
            )
        action_id = material.beats[0].action.action_id

        pump = ActionPump(
            ledger=self._ledger,
            executor=self._executor,
            settle=self._settle,
            owner_id=self._pump_owner,
            source="world-v2:quick-reaction-pump",
        )
        try:
            dispatched = await pump.drain_action(action_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - dispatch failure stays silent
            _LOG.warning(
                "quick reaction dispatch gave up trace=%s action=%s error=%s: %s",
                observation.trace_id,
                action_id,
                type(exc).__name__,
                str(exc)[:240],
            )
            return done(
                "dispatch_incomplete",
                reason="quick_reaction.dispatch_failed",
                reaction_id=reaction_id,
                proposal_id=proposal.proposal_id,
                action_id=action_id,
                ledger_advanced=True,
                gate_ms=gate_ms,
            )
        settled = dispatched.status == "settled"
        return done(
            "reacted" if settled else "dispatch_incomplete",
            reason=None if settled else f"quick_reaction.pump_{dispatched.status}",
            reaction_id=reaction_id,
            proposal_id=proposal.proposal_id,
            action_id=action_id,
            dispatch_status=dispatched.status,
            ledger_advanced=True,
            gate_ms=gate_ms,
        )

    @staticmethod
    def _reply_already_delivered(projection, trigger_audits) -> bool:
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

    def _deliberation_result(
        self, *, proposal: DecisionProposal, raw_response: str
    ) -> DeliberationResult:
        """Wrap the bounded gate call as one auditable single-attempt result."""

        prompt_material = {
            "contract": "quick-reaction-gate.1",
            "trigger_ref": proposal.trigger_ref,
            "capability_profile": self._capabilities.profile_id,
            "options": sorted(self._gate.option_ids),
        }
        call_id = f"model-call:quick-reaction:{_digest(prompt_material)}"
        response_hash = _digest(raw_response)
        audit = ModelResultAudit(
            model_call_id=call_id,
            model_result_ref=_model_result_ref(call_id, response_hash),
            attempt_id=f"attempt:quick-reaction:{_digest([call_id, response_hash])}",
            route=ModelRoute(
                tier="flash",
                reason_code="quick_reaction_gate",
                router_version="quick-reaction.1",
            ),
            model_id=str(getattr(self._model, "model", "local-quick-reaction")),
            model_version="quick-reaction-gate.1",
            request_hash=_digest(prompt_material),
            response_hash=response_hash,
            status="proposal_validated",
        )
        capsule_id = _digest(prompt_material)
        identity = {
            "capsule_id": capsule_id,
            "proposal_hash": proposal.proposal_hash,
            "attempt_audits": (audit.model_dump(mode="json"),),
        }
        return DeliberationResult(
            result_id=f"deliberation:{_deliberation_digest(identity)}",
            capsule_id=capsule_id,
            proposal=proposal,
            audit=audit,
            attempt_audits=(audit,),
        )

    async def _settle(self, result: ExternalObservation) -> None:
        """Settle one dispatch receipt without re-entering the locked runtime.

        Commit identities match ``WorldRuntime.settle`` exactly, so if this
        worker crashes mid-settlement the generic recovery path converges on
        the same events instead of duplicating the receipt.
        """

        trigger_id = f"trigger:settlement:{result.source}:{result.source_event_id}"
        before = await self._project()
        await self._commit(
            list(self._settlement.recording_events(result, trigger_id=trigger_id)),
            world_revision=before.world_revision,
            deliberation_revision=before.deliberation_revision,
            commit_id=f"commit:{trigger_id}:inbox",
        )
        after_inbox = await self._project()
        plan = self._settlement.plan(result, trigger_id=trigger_id, projection=after_inbox)
        await self._commit(
            list(plan.events),
            world_revision=after_inbox.world_revision,
            deliberation_revision=after_inbox.deliberation_revision,
            commit_id=f"commit:{trigger_id}:settlement",
        )

    async def _project(self):
        if self._ledger.blocks_event_loop:
            return await asyncio.to_thread(self._ledger.project)
        return self._ledger.project()

    async def _commit(
        self,
        events,
        *,
        world_revision: int,
        deliberation_revision: int,
        commit_id: str,
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


__all__ = [
    "QUICK_REACTION_PROPOSAL_PREFIX",
    "QuickReactionContextPolicy",
    "QuickReactionDecisionProfile",
    "QuickReactionPolicy",
    "QuickReactionRunResult",
    "QuickReactionSemanticGate",
    "QuickReactionWorker",
    "materialize_quick_reaction_proposal",
    "parse_quick_reaction_verdict",
    "quick_reaction_attempt_id",
]

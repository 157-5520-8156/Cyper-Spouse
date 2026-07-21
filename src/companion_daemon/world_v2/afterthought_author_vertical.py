"""Afterthought vertical, framework edition (BoundedDecisionVertical pilot).

Semantics are preserved byte-for-byte from ``afterthought_author.py`` (the
frozen hand-written implementation, kept in tree for hot rollback): the
restraint policy, mood/reply/daypart weight compiler, joint mode×delay grid,
gate prompt, strict verdict parser with the v1 overlap guard, the opportunity
predicate and the proposal materialization below are verbatim copies.  All
lifecycle ceremony — open/claim/lease/complete, recorded draws, the bounded
gate call, audit wrapping and ExpressionPlan acceptance — lives in
:mod:`bounded_decision_vertical`.

The shadow-replay suite drives this edition and the hand-written one on the
same input stream and holds every commit to zero byte difference; the
composition root switches between them via ``WORLD_V2_BDV_PILOT_DISABLED``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import datetime, timedelta
from typing import Literal

from pydantic import Field, model_validator

from .accepted_ledger_batch import AcceptedLedgerBatchIssuer
from .bounded_decision_vertical import (
    AnchoredIdentityTemplates,
    AnchoredRunResult,
    AnchoredTriggerLifecycle,
    AnchoredVerticalRuntime,
    AuditContextTimes,
    BoundedModelStep,
    DrawContext,
    DrawPlan,
    DrawStep,
    ExpressionAcceptanceBinding,
    ModelStepContext,
    SingleCallAuditTemplate,
    VerticalSpec,
    digest as _framework_digest,
)
from .chat_model_deliberation_adapter import ChatCompletionModel, CompanionIdentityFrame
from .deliberation import ModelRoute
from .expression_plan_acceptance import ExpressionPlanBudgetPolicy
from .ledger import LedgerPort
from .local_chronology import LocalChronology
from .model_json import extract_json_object_text
from .mood_view import active_mood_intensities
from .proposal_envelope import (
    CanonicalTypedPayload,
    DecisionProposal,
    ProposalActionIntent,
    ProposalEvidenceRef,
    TypedChange,
)
from .recent_dialogue import RecentDialogueCompiler
from .schema_core import FrozenModel


_LOG = logging.getLogger(__name__)

AFTERTHOUGHT_PROPOSAL_PREFIX = "proposal:afterthought:"

AfterthoughtMode = Literal["quick_continue", "topic_drift"]

_REPLY_ACTION_KINDS = frozenset({"reply"})
_REPLY_SETTLED_STATES = frozenset({"provider_accepted", "delivered"})
# A pending outbound initiative means she already owes the channel something;
# stacking an afterthought on top reads as spam, never as spontaneity.
_PENDING_INITIATIVE_KINDS = frozenset({"followup", "proactive_message"})
_PENDING_ACTION_STATES = frozenset({"authorized", "scheduled", "claimed", "dispatch_started"})


def _canonical_json(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    )


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


class AfterthoughtPolicy(FrozenModel):
    """Installed restraint constants; none of them author a single word."""

    # Most replies must not grow a tail.  Pre-context act mass out of 10_000.
    base_act_bp: int = Field(default=2_000, ge=100, le=9_000)
    act_floor_bp: int = Field(default=50, ge=1, le=9_000)
    act_ceiling_bp: int = Field(default=4_500, ge=100, le=9_990)
    # Mode preference mass, ported from v1's 0.26 / 0.14 pulse split.
    quick_continue_weight_bp: int = Field(default=6_500, ge=1, le=10_000)
    topic_drift_weight_bp: int = Field(default=3_500, ge=1, le=10_000)
    # Window grids: the drawn delay is one recorded candidate inside the
    # mode's v1 interval, so replay reproduces the exact seconds.
    quick_continue_min_seconds: int = Field(default=12, ge=1, le=600)
    quick_continue_max_seconds: int = Field(default=30, ge=2, le=1_200)
    quick_continue_step_seconds: int = Field(default=3, ge=1, le=60)
    topic_drift_min_seconds: int = Field(default=75, ge=1, le=3_600)
    topic_drift_max_seconds: int = Field(default=180, ge=2, le=7_200)
    topic_drift_step_seconds: int = Field(default=15, ge=1, le=600)
    # How long a due afterthought stays sendable before it is simply stale.
    dispatch_slack_seconds: int = Field(default=600, ge=30, le=7_200)
    # A receipt older than this no longer produces an impulse at all: the
    # conversational moment has passed (and a deploy never resurrects one).
    opportunity_horizon_seconds: int = Field(default=240, ge=30, le=3_600)
    # v1: "A normal short turn has already received its answer."  A reply
    # shorter than this never grows a tail; it was already complete.
    min_reply_chars: int = Field(default=10, ge=1, le=200)
    max_text_chars: int = Field(default=120, ge=20, le=1_000)
    gate_timeout_seconds: float = Field(default=8.0, gt=0.0, le=30.0)
    # Local quiet hours [start, end): the act mass is halved, not zeroed.
    night_start_hour: int = Field(default=1, ge=0, le=23)
    night_end_hour: int = Field(default=7, ge=0, le=24)

    @model_validator(mode="after")
    def windows_are_ordered(self) -> "AfterthoughtPolicy":
        if self.quick_continue_max_seconds <= self.quick_continue_min_seconds:
            raise ValueError("quick_continue window must move forward")
        if self.topic_drift_max_seconds <= self.topic_drift_min_seconds:
            raise ValueError("topic_drift window must move forward")
        if self.act_ceiling_bp < self.act_floor_bp:
            raise ValueError("act ceiling must not undercut the floor")
        return self

    def delay_candidates(self, mode: AfterthoughtMode) -> tuple[int, ...]:
        if mode == "quick_continue":
            lo, hi, step = (
                self.quick_continue_min_seconds,
                self.quick_continue_max_seconds,
                self.quick_continue_step_seconds,
            )
        else:
            lo, hi, step = (
                self.topic_drift_min_seconds,
                self.topic_drift_max_seconds,
                self.topic_drift_step_seconds,
            )
        grid = list(range(lo, hi + 1, step))
        if grid[-1] != hi:
            grid.append(hi)
        return tuple(grid)


class AfterthoughtDecisionProfile(FrozenModel):
    """Explainable act/hold mass compiled from accepted mood, reply, daypart."""

    candidate_weights: dict[Literal["act", "hold"], int]
    reason_codes: tuple[str, ...]


class AfterthoughtContextPolicy:
    """Translate mood, the reply's own texture, and local daypart into mass."""

    version = "afterthought-context.1"

    def __init__(self, *, policy: AfterthoughtPolicy, chronology: LocalChronology) -> None:
        self._policy = policy
        self._chronology = chronology

    def compile(
        self, *, projection, logical_time: datetime, reply_text: str | None
    ) -> AfterthoughtDecisionProfile:
        act = self._policy.base_act_bp
        reasons: list[str] = []

        mood = active_mood_intensities(projection.affect_episodes)
        approach = max((mood.get(dimension, 0) for dimension in ("warmth", "joy")), default=0)
        guarded = max(
            (value for dimension, value in mood.items() if dimension not in {"warmth", "joy"}),
            default=0,
        )
        if guarded >= 5_000:
            act = act * 2 // 5
            reasons.append("mood:guarded")
        elif guarded >= 3_000:
            act = act * 7 // 10
            reasons.append("mood:reserved")
        elif approach >= 5_000:
            act += 700
            reasons.append("mood:approach")
        elif approach >= 3_000:
            act += 350
            reasons.append("mood:warm")
        else:
            reasons.append("mood:neutral")

        # The reply's own texture is a bounded signal: a substantial message
        # plausibly leaves a loose thread; a terse one was already complete.
        length = len(reply_text.strip()) if reply_text else 0
        if length >= 30:
            act += 500
            reasons.append("reply:substantial")
        elif 0 < length < 10:
            act -= 600
            reasons.append("reply:terse")
        else:
            reasons.append("reply:moderate")

        local = self._chronology.localize(logical_time)
        assert local is not None
        if self._policy.night_start_hour <= local.hour < self._policy.night_end_hour:
            act //= 2
            reasons.append("daypart:late_night")
        else:
            reasons.append("daypart:normal")

        act = min(max(act, self._policy.act_floor_bp), self._policy.act_ceiling_bp)
        return AfterthoughtDecisionProfile(
            candidate_weights={"act": act, "hold": 10_000 - act},
            reason_codes=tuple(reasons),
        )


def afterthought_attempt_id(
    *,
    receipt_event_ref: str,
    profile: AfterthoughtDecisionProfile,
    policy_version: str = AfterthoughtContextPolicy.version,
) -> str:
    """Stable draw identity: same receipt and compiled profile replay one draw."""

    return "afterthought:" + _digest(
        {
            "receipt_event_ref": receipt_event_ref,
            "policy_version": policy_version,
            "candidate_weights": profile.candidate_weights,
            "reason_codes": profile.reason_codes,
        }
    )


class AfterthoughtVerdict(FrozenModel):
    mode: AfterthoughtMode
    text: str = Field(min_length=1)


def parse_afterthought_verdict(
    raw: object, *, drawn_mode: AfterthoughtMode, max_chars: int
) -> AfterthoughtVerdict | None:
    """Strictly extract one confirmation; anything else means no afterthought.

    The mode was already drawn by recorded random authority; the model only
    confirms it and authors the line.  A mismatched echo, oversized text, a
    multi-line answer, or any structural surprise declines quietly.
    """

    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        value = json.loads(extract_json_object_text(raw))
    except (TypeError, ValueError):
        return None
    if not isinstance(value, dict):
        return None
    flagged = value.get("afterthought")
    if flagged is False:
        return None
    if flagged is not True:
        return None
    if value.get("mode") != drawn_mode:
        return None
    text = value.get("text")
    if not isinstance(text, str):
        return None
    text = text.strip()
    if not text or len(text) > max_chars or "\n" in text or "\r" in text:
        return None
    return AfterthoughtVerdict(mode=drawn_mode, text=text)


def _texts_overlap(candidate: str, earlier: str) -> bool:
    """Ported from v1: keep one continuation from paraphrasing its own reply."""

    def normalize(text: str) -> str:
        return "".join(
            char for char in text.lower() if char not in " \t，。！？!?、~～\u3000"
        )

    left, right = normalize(candidate), normalize(earlier)
    if not left or not right:
        return False
    return (
        left in right
        or right in left
        or len(set(left) & set(right)) / max(len(set(left)), 1) >= 0.82
    )


class AfterthoughtOpportunity(FrozenModel):
    """One settled reply receipt still inside its afterthought window."""

    action_id: str
    plan_id: str
    receipt_event_ref: str
    receipt_event_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    receipt_world_revision: int = Field(ge=1)
    anchored_at: datetime
    reply_text: str | None
    trace_id: str
    correlation_id: str

    @property
    def anchor_event_ref(self) -> str:
        return self.receipt_event_ref


def materialize_afterthought_proposal(
    *,
    opportunity: AfterthoughtOpportunity,
    evaluated_world_revision: int,
    target: str,
    mode: AfterthoughtMode,
    delay_seconds: int,
    dispatch_slack_seconds: int,
    text: str,
    recorded_draw_refs: tuple[str, ...],
) -> DecisionProposal:
    """Bind one confirmed afterthought to its receipt as an inert proposal.

    The action intent reuses the existing ``followup`` kind with a due window
    anchored at the receipt moment, so the generic ActionPump's ``not_before``
    mechanism owns the delay; no new action kind and no private scheduler.
    """

    identity = _digest(
        {
            "contract": "afterthought-materialization.1",
            "trigger_ref": opportunity.receipt_event_ref,
            "world_revision": evaluated_world_revision,
            "target": target,
            "mode": mode,
            "delay_seconds": delay_seconds,
            "recorded_draw_refs": recorded_draw_refs,
        }
    )
    not_before = opportunity.anchored_at + timedelta(seconds=delay_seconds)
    expires_at = not_before + timedelta(seconds=dispatch_slack_seconds)
    payload_hash = "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
    change_id = f"change:afterthought:{identity}"
    plan_id = f"plan:afterthought:{identity}"
    beat_id = f"beat:afterthought:{identity}:1"
    intent_id = f"intent:afterthought:{identity}:1"
    payload_ref = f"payload:afterthought:{identity}:1"
    change = TypedChange(
        change_id=change_id,
        kind="expression_plan_transition",
        target_id=plan_id,
        transition="accept",
        payload=CanonicalTypedPayload.from_value(
            payload_schema="expression_plan_transition.v1",
            value={
                "plan_id": plan_id,
                "overall_intent": f"expression:afterthought:{mode}",
                "ordering_policy": "dependencies",
                "terminal_policy": "settle",
                "beat_drafts": [
                    {
                        "beat_id": beat_id,
                        "inline_text": text,
                        "materialized_payload_ref": payload_ref,
                        "payload_hash": payload_hash,
                        "content_type": "text/plain",
                        "dependency_beat_ids": [],
                        "delay_window": {
                            "not_before": not_before.isoformat(),
                            "expires_at": expires_at.isoformat(),
                        },
                        # A user message inside the window opens the normal
                        # expression-reconsideration gate; the reviewer may
                        # cancel this frozen tail before dispatch.
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
        proposal_id=f"{AFTERTHOUGHT_PROPOSAL_PREFIX}{identity}",
        trigger_ref=opportunity.receipt_event_ref,
        evaluated_world_revision=evaluated_world_revision,
        evidence_refs=(
            ProposalEvidenceRef(
                ref_id=opportunity.receipt_event_ref,
                evidence_kind="committed_world_event",
                source_world_revision=opportunity.receipt_world_revision,
                immutable_hash="sha256:" + opportunity.receipt_event_hash,
            ),
        ),
        proposed_changes=(change,),
        action_intents=(
            ProposalActionIntent(
                intent_id=intent_id,
                kind="followup",
                layer="external_action",
                target=target,
                payload_ref=payload_ref,
                payload_hash=payload_hash,
                causal_change_id=change_id,
                beat_ref=beat_id,
                due_window=(not_before, expires_at),
            ),
        ),
        confidence=5_500,
        brief_rationale=f"Recorded {mode} afterthought impulse after her settled reply.",
        behavior_tendency="continue_thought",
        stance="spontaneous_addition",
        display_strategy=mode,
        timing_choice="later",
    )


PROCESS_KIND = "afterthought_author"


def next_afterthought_opportunity(
    projection,
    *,
    policy: AfterthoughtPolicy,
    companion_actor_ref: str,
    target: str,
) -> AfterthoughtOpportunity | None:
    """The eligibility predicate, verbatim from the hand-written pilot."""

    logical_time = projection.logical_time
    # Restraint: while any authored initiative is still pending toward the
    # channel, a fresh tail must not stack on top of it.
    for action in projection.actions:
        if (
            action.kind in _PENDING_INITIATIVE_KINDS
            and action.state in _PENDING_ACTION_STATES
        ):
            return None
    receipts_by_action: dict[str, object] = {}
    for receipt in projection.execution_receipts:
        if receipt.observed_state in _REPLY_SETTLED_STATES:
            receipts_by_action[receipt.action_id] = receipt
    candidates = []
    for action in projection.actions:
        if action.kind not in _REPLY_ACTION_KINDS:
            continue
        if action.state not in _REPLY_SETTLED_STATES:
            continue
        if action.actor != companion_actor_ref or action.target != target:
            continue
        receipt = receipts_by_action.get(action.action_id)
        if receipt is None or action.expression_plan_id is None:
            continue
        candidates.append((action, receipt))
    if not candidates:
        return None
    refs = {item.event_id: item for item in projection.committed_world_event_refs}
    # Only the latest settled reply may grow a tail: an older reply is
    # conversationally superseded, never a second chance.
    action, receipt = max(
        candidates, key=lambda item: (item[1].received_at, item[0].action_id)
    )
    receipt_event_ref = refs.get(
        f"event:trigger:settlement:{receipt.provider}:{receipt.source_event_id}"
        ":execution-receipt"
    )
    if receipt_event_ref is None:
        return None
    if any(
        item.process_kind == PROCESS_KIND
        and item.state == "terminal"
        and item.source_evidence_ref == receipt_event_ref.event_id
        for item in projection.trigger_processes
    ):
        # This reply's one consideration already happened (held, declined
        # or authorized); the lane goes quiet instead of reporting work.
        return None
    # 克制协同: a reply that itself asked for a response owns the floor
    # through its accepted response expectation; the response-gap
    # initiative lane already watches that exact silence.  Adding a tail
    # on top would compete with her own question.
    if any(
        manifest.plan_id == action.expression_plan_id
        and manifest.response_expectation is not None
        for manifest in projection.expression_plan_manifests
    ):
        return None
    anchored_at = receipt_event_ref.logical_time
    elapsed = (logical_time - anchored_at).total_seconds()
    # The consideration itself waits for the quick window to open: v1
    # fired its model at pulse time, not at send time, and a user answer
    # arriving within those first seconds voids the moment before any
    # draw or model call is spent on it.
    if elapsed < policy.quick_continue_min_seconds:
        return None
    if elapsed > policy.opportunity_horizon_seconds:
        return None
    # The user answering inside the window hands the floor to the next
    # inbound turn; the afterthought moment is void.
    if any(
        observation.actor != companion_actor_ref
        and observation.world_revision > receipt_event_ref.world_revision
        for observation in projection.message_observations
    ):
        return None
    reply_text = next(
        (
            stored.text
            for stored in projection.stored_message_payloads
            if stored.payload_ref == action.payload_ref
            and stored.payload_hash == action.payload_hash
        ),
        None,
    )
    # v1's short-turn rule: a brief acknowledgement was already complete;
    # scheduling a tail makes it too easy for her to answer herself.
    if reply_text is not None and len(reply_text.strip()) < policy.min_reply_chars:
        return None
    return AfterthoughtOpportunity(
        action_id=action.action_id,
        plan_id=action.expression_plan_id,
        receipt_event_ref=receipt_event_ref.event_id,
        receipt_event_hash=receipt_event_ref.payload_hash,
        receipt_world_revision=receipt_event_ref.world_revision,
        anchored_at=anchored_at,
        reply_text=reply_text,
        trace_id=action.trace_id,
        correlation_id=action.correlation_id,
    )


def timing_candidates(policy: AfterthoughtPolicy) -> tuple[tuple[str, ...], dict[str, int]]:
    """One recorded draw jointly selects mode and its in-window seconds."""

    weights: dict[str, int] = {}
    refs: list[str] = []
    for mode, mass in (
        ("quick_continue", policy.quick_continue_weight_bp),
        ("topic_drift", policy.topic_drift_weight_bp),
    ):
        grid = policy.delay_candidates(mode)  # type: ignore[arg-type]
        per_candidate = max(1, mass // len(grid))
        for seconds in grid:
            ref = f"{mode}:{seconds}"
            refs.append(ref)
            weights[ref] = per_candidate
    return tuple(refs), weights


def parse_timing(ref: str) -> tuple[AfterthoughtMode, int]:
    mode, _, seconds = ref.rpartition(":")
    if mode not in {"quick_continue", "topic_drift"}:
        raise ValueError("afterthought timing draw returned an unknown mode")
    return mode, int(seconds)  # type: ignore[return-value]


def afterthought_gate_messages(
    *,
    policy: AfterthoughtPolicy,
    identity_frame: CompanionIdentityFrame | None,
    mode: AfterthoughtMode,
    reply_text: str,
    dialogue: tuple[dict[str, str], ...],
    local_time_label: str,
) -> list[dict[str, str]]:
    """The gate prompt, verbatim from the hand-written pilot."""

    mode_hint = (
        "紧接着她刚发出的那句话补一个小尾巴：想起漏说的半句、补一个细节、轻轻收个尾。"
        if mode == "quick_continue"
        else "由刚才说的内容自然联想到的另一件小事：一次轻轻的跳跃，不是开新话题轰炸。"
    )
    system = (
        "她刚刚在私聊里发出了一条回复。过一小会儿，人有时会自然地又想起什么，补一句。"
        "你判断此刻是否真的有那么一句值得补，值得时替她写出来。"
        f"本次给定的模式是 {mode}：{mode_hint}模式已定，不可更换。"
        "要求：像她本人的口吻，口语、一句话、不换行，"
        f"不超过{policy.max_text_chars}个字符；不得重复或改写她已经说过的内容；"
        "不问候、不客套、不总结、不解释自己为什么补充。"
        "多数时候其实没有值得补的话——拿不准就不补，宁缺毋滥。"
        '只输出一个 JSON 对象：{"afterthought":false} 或 '
        '{"afterthought":true,"mode":"' + mode + '","text":"..."}。'
        "禁止 Markdown、注释和任何其他字段。"
    )
    if identity_frame is not None:
        system += (
            " 她的稳定身份（仅用于口吻，不得复述）："
            + _canonical_json(identity_frame.model_dump(mode="json"))
        )
    user = _canonical_json(
        {
            "recent_dialogue": list(dialogue),
            "her_reply_just_sent": reply_text,
            "local_time": local_time_label,
            "mode": mode,
        }
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


class AfterthoughtVerticalRuntime:
    """Framework-edition afterthought runtime (drop-in for the pilot class).

    Constructor signature matches ``AfterthoughtAuthorRuntime`` exactly so
    the composition root switch is a one-symbol change.
    """

    PROCESS_KIND = PROCESS_KIND

    def __init__(
        self,
        *,
        ledger: LedgerPort,
        model: ChatCompletionModel,
        policy: ExpressionPlanBudgetPolicy,
        batch_issuer: AcceptedLedgerBatchIssuer,
        owner_id: str,
        target: str,
        companion_actor_ref: str,
        counterpart_actor_ref: str,
        chronology: LocalChronology,
        afterthought_policy: AfterthoughtPolicy | None = None,
        identity_frame: CompanionIdentityFrame | None = None,
        dialogue_compiler: RecentDialogueCompiler | None = None,
        lease_seconds: int = 120,
        source: str = "world-v2:afterthought-author",
    ) -> None:
        if not owner_id or lease_seconds <= 0:
            raise ValueError("afterthought runtime needs owner and positive lease")
        if not target or target not in policy.allowed_targets:
            raise ValueError("afterthought target must be in the budget policy allow-list")
        if not companion_actor_ref or not counterpart_actor_ref:
            raise ValueError("afterthought runtime needs companion and counterpart refs")
        if source != "world-v2:afterthought-author":
            raise ValueError("afterthought identity source string is frozen")
        self.ledger = ledger
        self._policy = afterthought_policy or AfterthoughtPolicy()
        self._chronology = chronology
        self._identity_frame = identity_frame
        self._dialogue = dialogue_compiler
        self._companion = companion_actor_ref
        self._counterpart = counterpart_actor_ref
        self._target = target
        spec = self._build_spec(expression_policy=policy, lease_seconds=lease_seconds)
        self._runtime = AnchoredVerticalRuntime(
            spec=spec,
            ledger=ledger,
            model=model,
            batch_issuer=batch_issuer,
            owner_id=owner_id,
            worker_actor=companion_actor_ref,
        )

    async def drain_one(self) -> AnchoredRunResult:
        return await self._runtime.drain_one()

    def _build_spec(
        self, *, expression_policy: ExpressionPlanBudgetPolicy, lease_seconds: int
    ) -> VerticalSpec:
        policy = self._policy
        context = AfterthoughtContextPolicy(policy=policy, chronology=self._chronology)

        def opportunity(projection) -> AfterthoughtOpportunity | None:
            return next_afterthought_opportunity(
                projection,
                policy=policy,
                companion_actor_ref=self._companion,
                target=self._target,
            )

        def plan_gate_draw(context_inputs: DrawContext) -> DrawPlan:
            opp = context_inputs.opportunity
            assert isinstance(opp, AfterthoughtOpportunity)
            profile = context.compile(
                projection=context_inputs.projection,
                logical_time=context_inputs.logical_time,
                reply_text=opp.reply_text or "",
            )
            return DrawPlan(
                attempt_id=afterthought_attempt_id(
                    receipt_event_ref=opp.receipt_event_ref, profile=profile
                ),
                candidate_refs=("act", "hold"),
                candidate_weights=profile.candidate_weights,
            )

        def plan_timing_draw(context_inputs: DrawContext) -> DrawPlan:
            refs, weights = timing_candidates(policy)
            return DrawPlan(
                attempt_id=context_inputs.previous["gate"].attempt_id + ":timing",
                candidate_refs=refs,
                candidate_weights=weights,
            )

        async def gate_messages(
            context_inputs: ModelStepContext,
        ) -> list[dict[str, str]]:
            opp = context_inputs.opportunity
            assert isinstance(opp, AfterthoughtOpportunity)
            mode, _delay = context_inputs.interpretations["timing"]
            dialogue = await self._dialogue_frame(projection=context_inputs.projection)
            local = self._chronology.localize(
                context_inputs.projection.logical_time or opp.anchored_at
            )
            assert local is not None
            return afterthought_gate_messages(
                policy=policy,
                identity_frame=self._identity_frame,
                mode=mode,
                reply_text=opp.reply_text or "",
                dialogue=dialogue,
                local_time_label=local.strftime("%H:%M"),
            )

        def parse(raw: object, context_inputs: ModelStepContext) -> AfterthoughtVerdict | None:
            opp = context_inputs.opportunity
            assert isinstance(opp, AfterthoughtOpportunity)
            mode, _delay = context_inputs.interpretations["timing"]
            verdict = parse_afterthought_verdict(
                raw, drawn_mode=mode, max_chars=policy.max_text_chars
            )
            if verdict is not None and _texts_overlap(verdict.text, opp.reply_text or ""):
                # v1's episode overlap guard: a tail that paraphrases the
                # reply it follows is noise, not an afterthought.
                return None
            return verdict

        def compile_proposal(
            *,
            opportunity: AfterthoughtOpportunity,
            evaluated_world_revision: int,
            verdict: AfterthoughtVerdict,
            draws,
            interpretations,
        ) -> DecisionProposal:
            mode, delay_seconds = interpretations["timing"]
            return materialize_afterthought_proposal(
                opportunity=opportunity,
                evaluated_world_revision=evaluated_world_revision,
                target=self._target,
                mode=mode,
                delay_seconds=delay_seconds,
                dispatch_slack_seconds=policy.dispatch_slack_seconds,
                text=verdict.text,
                recorded_draw_refs=(draws["gate"].draw_id, draws["timing"].draw_id),
            )

        def prompt_material(
            context_inputs: ModelStepContext, proposal: DecisionProposal
        ) -> dict[str, object]:
            mode, _delay = context_inputs.interpretations["timing"]
            return {
                "contract": "afterthought-gate.1",
                "trigger_ref": proposal.trigger_ref,
                "mode": mode,
            }

        def audit_times(opportunity, head) -> AuditContextTimes:
            moment = head.logical_time or opportunity.anchored_at
            return AuditContextTimes(logical_time=moment, created_at=moment)

        return VerticalSpec(
            lane_id="afterthought",
            lifecycle=AnchoredTriggerLifecycle(
                process_kind=PROCESS_KIND,
                identity=AnchoredIdentityTemplates(namespace="afterthought"),
                trigger_id=lambda world_id, opp: "trigger:afterthought:"
                + _framework_digest({"world": world_id, "receipt": opp.receipt_event_ref}),
                trigger_ref=lambda opp: f"afterthought:{opp.action_id}",
                lease_seconds=lease_seconds,
            ),
            grammar_lane="proactive",
            opportunity=opportunity,
            draws=(
                DrawStep(
                    step_id="gate",
                    plan=plan_gate_draw,
                    catalog_version="afterthought-act-hold.1",
                    weight_policy_version=AfterthoughtContextPolicy.version,
                    halt_unless="act",
                    halt_outcome="held",
                    halt_reason="afterthought.draw_hold",
                ),
                DrawStep(
                    step_id="timing",
                    plan=plan_timing_draw,
                    catalog_version="afterthought-mode-delay.1",
                    weight_policy_version=AfterthoughtContextPolicy.version,
                    interpret=parse_timing,
                ),
            ),
            model=BoundedModelStep(
                messages=gate_messages,
                parse=parse,
                timeout_seconds=policy.gate_timeout_seconds,
                temperature=0.7,
                failure_policy="decline_quietly",
                audit=SingleCallAuditTemplate(
                    call_namespace="afterthought",
                    route=ModelRoute(
                        tier="flash",
                        reason_code="afterthought_gate",
                        router_version="afterthought.1",
                    ),
                    model_version="afterthought-gate.1",
                    fallback_model_id="background-afterthought",
                ),
                prompt_material=prompt_material,
            ),
            compile=compile_proposal,
            acceptance=ExpressionAcceptanceBinding(
                policy=expression_policy,
                dispatch="generic_action_pump",
                batch_actor="policy_actor",
            ),
            audit_times=audit_times,
            random_source="world-v2:afterthought-random",
            draw_actor="system:afterthought",
            worker_source="world-v2:afterthought-author",
        )

    async def _dialogue_frame(self, *, projection) -> tuple[dict[str, str], ...]:
        if self._dialogue is None:
            return ()
        try:
            if self.ledger.blocks_event_loop:
                items = await asyncio.to_thread(
                    self._dialogue.compile,
                    projection=projection,
                    actor_ref=self._companion,
                    subject_refs=frozenset({self._counterpart, self._target}),
                )
            else:
                items = self._dialogue.compile(
                    projection=projection,
                    actor_ref=self._companion,
                    subject_refs=frozenset({self._counterpart, self._target}),
                )
        except Exception:  # noqa: BLE001 - context is advisory, never blocking
            _LOG.warning("afterthought dialogue frame unavailable", exc_info=True)
            return ()
        ordered = tuple(reversed(items))[-8:]
        return tuple(
            {
                "speaker": "she" if item.speaker == "companion" else "user",
                "text": item.text[:200],
            }
            for item in ordered
        )


__all__ = [
    "AFTERTHOUGHT_PROPOSAL_PREFIX",
    "AfterthoughtContextPolicy",
    "AfterthoughtDecisionProfile",
    "AfterthoughtMode",
    "AfterthoughtOpportunity",
    "AfterthoughtPolicy",
    "AfterthoughtVerdict",
    "AfterthoughtVerticalRuntime",
    "afterthought_attempt_id",
    "afterthought_gate_messages",
    "materialize_afterthought_proposal",
    "next_afterthought_opportunity",
    "parse_afterthought_verdict",
    "parse_timing",
    "timing_candidates",
]

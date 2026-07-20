"""Afterthought lane: a small, restrained "又想起一句" after her settled reply.

v1's cherished design (``qq_websocket.py``): shortly after a reply goes out,
she may naturally add one more line.  Two ported modes:

- ``quick_continue`` (12-30s): a tail directly continuing what she just said;
- ``topic_drift`` (75-180s): one small associated thought.

v1's third mode (``silence_react``, 240-600s: reacting to *your* silence) is
deliberately not ported: World v2 already owns that exact situation through
the accepted response-expectation / response-gap opportunity compiled by
``SocialInitiativeCompiler`` plus the silence-appraisal trigger, both of which
route through the same proactive deliberation lane.  A second silence author
would double-send on the same quiet gap.

Discipline, in order:

1. The trigger evidence is the committed ``ExecutionReceiptRecorded`` event of
   her reply (``provider_accepted``/``delivered``).  Nothing runs before the
   reply actually reached the provider.
2. Whether she even has an afterthought impulse is one recorded, low-mass
   act/hold draw (RandomAuthority) whose attempt identity binds the receipt
   event and the compiled mood/reply/daypart profile.  ``hold`` is terminal
   for that reply: the moment passes, at most one consideration per reply.
3. Mode and the exact delay seconds are one recorded draw over a bounded
   candidate grid inside the mode window, so replay reproduces the same
   timing without a scheduler thread.
4. Only then does one bounded background-model call confirm and author the
   text under a closed JSON contract (``{"afterthought":false}`` or
   ``{"afterthought":true,"mode":...,"text":<=120 chars}``).  Most replies
   should decline.
5. A positive verdict becomes an ordinary ``DecisionProposal`` (proactive
   grammar, ``followup`` action kind with a due window) -> audit ->
   ExpressionPlan acceptance -> authorized Action.  The generic ActionPump's
   ``not_before`` due mechanism owns dispatch; a user message inside the
   window opens the normal expression-reconsideration gate on the frozen
   beat (``reconsider-on-new-observation``), which may cancel it.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import hashlib
import json
import logging
from typing import Literal

from pydantic import Field, model_validator

from .accepted_ledger_batch import AcceptedLedgerBatchIssuer
from .chat_model_deliberation_adapter import ChatCompletionModel, CompanionIdentityFrame
from .deliberation import (
    DeliberationResult,
    ModelResultAudit,
    ModelRoute,
    _digest as _deliberation_digest,
    _model_result_ref,
)
from .errors import ConcurrencyConflict, IdempotencyConflict
from .event_identity import domain_idempotency_key
from .expression_plan_acceptance import (
    ExpressionPlanAcceptanceError,
    ExpressionPlanBudgetPolicy,
    derive_expression_plan_material,
)
from .expression_plan_atomic_recorder import ExpressionPlanAtomicRecorder
from .ledger import LedgerPort
from .local_chronology import LocalChronology
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
from .recent_dialogue import RecentDialogueCompiler
from .schema_core import FrozenModel
from .schemas import ClaimLease, ProjectionCursor, TriggerProcess, WorldEvent


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


class AfterthoughtGate:
    """Bounded background-model confirmation that owns the "值得补吗" judgement.

    ``assess`` returns a verdict or ``None`` for *every* other outcome — an
    explicit ``{"afterthought": false}``, a timeout, transport failure, or
    malformed output.  All of those mean the same thing: no tail this time.
    """

    VERSION = "afterthought-gate.1"

    def __init__(
        self,
        *,
        model: ChatCompletionModel,
        policy: AfterthoughtPolicy,
        identity_frame: CompanionIdentityFrame | None = None,
        temperature: float = 0.7,
    ) -> None:
        if not 0 <= temperature <= 2:
            raise ValueError("afterthought gate temperature must be between 0 and 2")
        self._model = model
        self._policy = policy
        self._identity_frame = identity_frame
        self._temperature = temperature

    def messages(
        self,
        *,
        mode: AfterthoughtMode,
        reply_text: str,
        dialogue: tuple[dict[str, str], ...],
        local_time_label: str,
    ) -> list[dict[str, str]]:
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
            f"不超过{self._policy.max_text_chars}个字符；不得重复或改写她已经说过的内容；"
            "不问候、不客套、不总结、不解释自己为什么补充。"
            "多数时候其实没有值得补的话——拿不准就不补，宁缺毋滥。"
            '只输出一个 JSON 对象：{"afterthought":false} 或 '
            '{"afterthought":true,"mode":"' + mode + '","text":"..."}。'
            "禁止 Markdown、注释和任何其他字段。"
        )
        if self._identity_frame is not None:
            system += (
                " 她的稳定身份（仅用于口吻，不得复述）："
                + _canonical_json(self._identity_frame.model_dump(mode="json"))
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

    async def assess(
        self,
        *,
        mode: AfterthoughtMode,
        reply_text: str,
        dialogue: tuple[dict[str, str], ...],
        local_time_label: str,
    ) -> tuple[AfterthoughtVerdict | None, str | None]:
        """Return ``(verdict, raw_response)``; both ``None`` on failure."""

        try:
            async with asyncio.timeout(self._policy.gate_timeout_seconds):
                raw = await self._model.complete(
                    self.messages(
                        mode=mode,
                        reply_text=reply_text,
                        dialogue=dialogue,
                        local_time_label=local_time_label,
                    ),
                    temperature=self._temperature,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the lane declines quietly
            _LOG.warning(
                "afterthought gate unavailable: %s: %s", type(exc).__name__, str(exc)[:240]
            )
            return None, None
        verdict = parse_afterthought_verdict(
            raw, drawn_mode=mode, max_chars=self._policy.max_text_chars
        )
        if verdict is not None and _texts_overlap(verdict.text, reply_text):
            # v1's episode overlap guard: a tail that paraphrases the reply it
            # follows is noise, not an afterthought.
            return None, raw if isinstance(raw, str) else None
        return verdict, raw if isinstance(raw, str) else None


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


class AfterthoughtRunResult(FrozenModel):
    status: Literal[
        # No eligible settled reply right now.
        "idle",
        # A durable trigger process was opened; the next unit deliberates.
        "opened",
        "owned_elsewhere",
        # The recorded act/hold draw held the impulse (terminal per reply).
        "held",
        # The bounded model declined (or was unavailable); terminal per reply.
        "declined",
        # One followup Action carrying the tail is authorized with its window.
        "authorized",
        "budget_exhausted",
        # A cursor race; the durable claim resumes on the next pass.
        "stale",
        "completed_existing",
    ]
    trigger_id: str | None = None
    mode: AfterthoughtMode | None = None
    delay_seconds: int | None = None
    action_id: str | None = None
    proposal_id: str | None = None
    reason_code: str | None = None


class AfterthoughtAuthorRuntime:
    """Recovery-safe receipt -> recorded impulse -> bounded author -> Action."""

    PROCESS_KIND = "afterthought_author"

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
        self.ledger = ledger
        self._policy = afterthought_policy or AfterthoughtPolicy()
        self._context = AfterthoughtContextPolicy(policy=self._policy, chronology=chronology)
        self._chronology = chronology
        self._gate = AfterthoughtGate(
            model=model, policy=self._policy, identity_frame=identity_frame
        )
        self._model = model
        self._expression_policy = policy
        self._recorder = ExpressionPlanAtomicRecorder(batch_issuer=batch_issuer)
        self._owner = owner_id
        self._target = target
        self._actor = companion_actor_ref
        self._counterpart = counterpart_actor_ref
        self._dialogue = dialogue_compiler
        self._lease_seconds = lease_seconds
        self._source = source
        self._random = RandomAuthority(ledger=ledger, source="world-v2:afterthought-random")
        self._audits = ProposalAuditRecorder(ledger=ledger)
        self._grammar = production_proposal_grammar("proactive")

    async def drain_one(self) -> AfterthoughtRunResult:
        projection = await self._project()
        if projection.logical_time is None:
            return AfterthoughtRunResult(status="idle")
        opportunity = self._next_opportunity(projection)
        if opportunity is None:
            return AfterthoughtRunResult(status="idle")
        trigger_id = "trigger:afterthought:" + _digest(
            {"world": self.ledger.world_id, "receipt": opportunity.receipt_event_ref}
        )
        process = next(
            (item for item in projection.trigger_processes if item.trigger_id == trigger_id),
            None,
        )
        if process is None:
            await self._open(
                opportunity=opportunity, trigger_id=trigger_id, cursor=self._cursor(projection)
            )
            return AfterthoughtRunResult(status="opened", trigger_id=trigger_id)
        if process.state == "terminal":
            return AfterthoughtRunResult(status="completed_existing", trigger_id=trigger_id)
        active = await self._claim(
            process=process, opportunity=opportunity, projection=projection
        )
        if active is None:
            return AfterthoughtRunResult(status="owned_elsewhere", trigger_id=trigger_id)

        current = await self._project()
        existing_audit = next(
            (
                item
                for item in current.proposal_audits
                if item.trigger_ref == opportunity.receipt_event_ref
                and item.proposal_id.startswith(AFTERTHOUGHT_PROPOSAL_PREFIX)
            ),
            None,
        )
        if existing_audit is not None:
            # Crash recovery: the model already spoke durably; only the
            # acceptance may still be missing.
            return await self._accept(
                process=active, opportunity=opportunity, audit=existing_audit
            )

        reply_text = opportunity.reply_text or ""
        profile = self._context.compile(
            projection=current,
            logical_time=current.logical_time or opportunity.anchored_at,
            reply_text=reply_text,
        )
        attempt_id = afterthought_attempt_id(
            receipt_event_ref=opportunity.receipt_event_ref, profile=profile
        )
        gate_draw = await self._draw(
            attempt_id=attempt_id,
            candidate_refs=("act", "hold"),
            candidate_weights=profile.candidate_weights,
            catalog_version="afterthought-act-hold.1",
            opportunity=opportunity,
        )
        if gate_draw.selected_candidate_ref != "act":
            await self._complete(process=active, opportunity=opportunity, outcome="held")
            return AfterthoughtRunResult(
                status="held", trigger_id=trigger_id, reason_code="afterthought.draw_hold"
            )

        timing_refs, timing_weights = self._timing_candidates()
        timing_draw = await self._draw(
            attempt_id=attempt_id + ":timing",
            candidate_refs=timing_refs,
            candidate_weights=timing_weights,
            catalog_version="afterthought-mode-delay.1",
            opportunity=opportunity,
        )
        mode, delay_seconds = self._parse_timing(timing_draw.selected_candidate_ref)

        dialogue = await self._dialogue_frame(projection=current)
        local = self._chronology.localize(current.logical_time or opportunity.anchored_at)
        assert local is not None
        verdict, raw_response = await self._gate.assess(
            mode=mode,
            reply_text=reply_text,
            dialogue=dialogue,
            local_time_label=local.strftime("%H:%M"),
        )
        if verdict is None:
            await self._complete(process=active, opportunity=opportunity, outcome="declined")
            return AfterthoughtRunResult(
                status="declined",
                trigger_id=trigger_id,
                mode=mode,
                delay_seconds=delay_seconds,
                reason_code=(
                    "afterthought.gate_declined"
                    if raw_response is not None
                    else "afterthought.gate_unavailable"
                ),
            )
        assert raw_response is not None

        head = await self._project()
        proposal = materialize_afterthought_proposal(
            opportunity=opportunity,
            evaluated_world_revision=head.world_revision,
            target=self._target,
            mode=mode,
            delay_seconds=delay_seconds,
            dispatch_slack_seconds=self._policy.dispatch_slack_seconds,
            text=verdict.text,
            recorded_draw_refs=(gate_draw.draw_id, timing_draw.draw_id),
        )
        try:
            self._grammar.validate(proposal)
        except ProductionProposalGrammarError:
            await self._complete(
                process=active, opportunity=opportunity, outcome="grammar-rejected"
            )
            return AfterthoughtRunResult(
                status="declined",
                trigger_id=trigger_id,
                mode=mode,
                delay_seconds=delay_seconds,
                reason_code="afterthought.grammar_rejected",
            )
        result = self._deliberation_result(
            proposal=proposal, raw_response=raw_response, mode=mode
        )
        context = ProposalAuditContext(
            world_id=self.ledger.world_id,
            trigger_ref=opportunity.receipt_event_ref,
            logical_time=head.logical_time or opportunity.anchored_at,
            created_at=head.logical_time or opportunity.anchored_at,
            actor=self._actor,
            source=self._source,
            trace_id=opportunity.trace_id,
            causation_id=opportunity.receipt_event_ref,
            correlation_id=opportunity.correlation_id,
            evaluated_world_revision=head.world_revision,
            expected_commit_world_revision=head.world_revision,
            expected_deliberation_revision=head.deliberation_revision,
        )
        try:
            if self.ledger.blocks_event_loop:
                await asyncio.to_thread(self._audits.record, result, context)
            else:
                self._audits.record(result, context)
        except (ConcurrencyConflict, IdempotencyConflict):
            return AfterthoughtRunResult(
                status="stale",
                trigger_id=trigger_id,
                mode=mode,
                delay_seconds=delay_seconds,
                reason_code="afterthought.audit_race",
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
        if audit is None:
            raise RuntimeError("afterthought deliberation produced no durable audit")
        outcome = await self._accept(process=active, opportunity=opportunity, audit=audit)
        return outcome.model_copy(update={"mode": mode, "delay_seconds": delay_seconds})

    async def _accept(
        self, *, process: TriggerProcess, opportunity: AfterthoughtOpportunity, audit
    ) -> AfterthoughtRunResult:
        current = await self._project()
        existing = next(
            (
                item
                for item in current.actions
                if item.intent_ref.startswith(audit.proposal_id + ":")
            ),
            None,
        )
        if existing is not None:
            await self._complete(
                process=process,
                opportunity=opportunity,
                outcome=f"authorized:{existing.action_id}",
            )
            return AfterthoughtRunResult(
                status="completed_existing",
                trigger_id=process.trigger_id,
                proposal_id=audit.proposal_id,
                action_id=existing.action_id,
            )
        account = next(
            (
                item
                for item in current.budget_accounts
                if item.account_id == self._expression_policy.account_id
            ),
            None,
        )
        if account is None:
            await self._complete(
                process=process,
                opportunity=opportunity,
                outcome="budget-exhausted:account-unavailable",
            )
            return AfterthoughtRunResult(
                status="budget_exhausted",
                trigger_id=process.trigger_id,
                proposal_id=audit.proposal_id,
                reason_code="afterthought.budget_account_unavailable",
            )
        cursor = self._cursor(current)
        projection_time = current.logical_time or opportunity.anchored_at
        try:
            material = derive_expression_plan_material(
                audit=audit,
                cursor=cursor,
                world_id=self.ledger.world_id,
                policy=self._expression_policy,
                account=account,
                logical_time=projection_time,
                created_at=projection_time,
                trace_id=opportunity.trace_id,
                correlation_id=opportunity.correlation_id,
            )
        except ExpressionPlanAcceptanceError as exc:
            if exc.code in {
                "expression_plan_acceptance.budget_unavailable",
                "expression_plan_acceptance.budget_account_unavailable",
            }:
                await self._complete(
                    process=process, opportunity=opportunity, outcome="budget-exhausted"
                )
                return AfterthoughtRunResult(
                    status="budget_exhausted",
                    trigger_id=process.trigger_id,
                    proposal_id=audit.proposal_id,
                    reason_code=exc.code,
                )
            if exc.code == "expression_plan_acceptance.stale_revision":
                return AfterthoughtRunResult(
                    status="stale",
                    trigger_id=process.trigger_id,
                    proposal_id=audit.proposal_id,
                    reason_code=exc.code,
                )
            raise
        handle = self._recorder.prepare_batch(
            acceptance_id="acceptance:afterthought:" + _digest(audit.proposal_id),
            material=material,
            actor=self._expression_policy.actor,
            source=self._source,
        )
        try:
            if self.ledger.blocks_event_loop:
                await asyncio.to_thread(
                    self.ledger.commit_accepted, handle, expected_cursor=cursor
                )
            else:
                self.ledger.commit_accepted(handle, expected_cursor=cursor)
        except (ConcurrencyConflict, IdempotencyConflict):
            raced = await self._project()
            existing = next(
                (
                    item
                    for item in raced.actions
                    if item.intent_ref.startswith(audit.proposal_id + ":")
                ),
                None,
            )
            if existing is None:
                return AfterthoughtRunResult(
                    status="stale",
                    trigger_id=process.trigger_id,
                    proposal_id=audit.proposal_id,
                    reason_code="afterthought.acceptance_race",
                )
        action_id = (
            existing.action_id if existing is not None else material.beats[0].action.action_id
        )
        await self._complete(
            process=process, opportunity=opportunity, outcome=f"authorized:{action_id}"
        )
        return AfterthoughtRunResult(
            status="authorized",
            trigger_id=process.trigger_id,
            proposal_id=audit.proposal_id,
            action_id=action_id,
        )

    def _next_opportunity(self, projection) -> AfterthoughtOpportunity | None:
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
            if action.actor != self._actor or action.target != self._target:
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
            item.process_kind == self.PROCESS_KIND
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
        if elapsed < self._policy.quick_continue_min_seconds:
            return None
        if elapsed > self._policy.opportunity_horizon_seconds:
            return None
        # The user answering inside the window hands the floor to the next
        # inbound turn; the afterthought moment is void.
        if any(
            observation.actor != self._actor
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
        if reply_text is not None and len(reply_text.strip()) < self._policy.min_reply_chars:
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

    def _timing_candidates(self) -> tuple[tuple[str, ...], dict[str, int]]:
        """One recorded draw jointly selects mode and its in-window seconds."""

        weights: dict[str, int] = {}
        refs: list[str] = []
        for mode, mass in (
            ("quick_continue", self._policy.quick_continue_weight_bp),
            ("topic_drift", self._policy.topic_drift_weight_bp),
        ):
            grid = self._policy.delay_candidates(mode)  # type: ignore[arg-type]
            per_candidate = max(1, mass // len(grid))
            for seconds in grid:
                ref = f"{mode}:{seconds}"
                refs.append(ref)
                weights[ref] = per_candidate
        return tuple(refs), weights

    @staticmethod
    def _parse_timing(ref: str) -> tuple[AfterthoughtMode, int]:
        mode, _, seconds = ref.rpartition(":")
        if mode not in {"quick_continue", "topic_drift"}:
            raise ValueError("afterthought timing draw returned an unknown mode")
        return mode, int(seconds)  # type: ignore[return-value]

    async def _draw(
        self,
        *,
        attempt_id: str,
        candidate_refs: tuple[str, ...],
        candidate_weights: dict[str, int],
        catalog_version: str,
        opportunity: AfterthoughtOpportunity,
    ):
        projection = await self._project()
        draw_kwargs = dict(
            attempt_id=attempt_id,
            candidate_refs=candidate_refs,
            candidate_weights=candidate_weights,
            weight_policy_version=self._context.version,
            catalog_version=catalog_version,
            logical_time=projection.logical_time,
            seed_instant=opportunity.anchored_at,
            actor="system:afterthought",
            trace_id=opportunity.trace_id,
            correlation_id=opportunity.correlation_id,
        )
        if self.ledger.blocks_event_loop:
            return await asyncio.to_thread(self._random.draw, **draw_kwargs)
        return self._random.draw(**draw_kwargs)

    async def _dialogue_frame(self, *, projection) -> tuple[dict[str, str], ...]:
        if self._dialogue is None:
            return ()
        try:
            if self.ledger.blocks_event_loop:
                items = await asyncio.to_thread(
                    self._dialogue.compile,
                    projection=projection,
                    actor_ref=self._actor,
                    subject_refs=frozenset({self._counterpart, self._target}),
                )
            else:
                items = self._dialogue.compile(
                    projection=projection,
                    actor_ref=self._actor,
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

    def _deliberation_result(
        self, *, proposal: DecisionProposal, raw_response: str, mode: AfterthoughtMode
    ) -> DeliberationResult:
        """Wrap the bounded gate call as one auditable single-attempt result."""

        prompt_material = {
            "contract": AfterthoughtGate.VERSION,
            "trigger_ref": proposal.trigger_ref,
            "mode": mode,
        }
        call_id = f"model-call:afterthought:{_digest(prompt_material)}"
        response_hash = _digest(raw_response)
        audit = ModelResultAudit(
            model_call_id=call_id,
            model_result_ref=_model_result_ref(call_id, response_hash),
            attempt_id=f"attempt:afterthought:{_digest([call_id, response_hash])}",
            route=ModelRoute(
                tier="flash",
                reason_code="afterthought_gate",
                router_version="afterthought.1",
            ),
            model_id=str(getattr(self._model, "model", "background-afterthought")),
            model_version=AfterthoughtGate.VERSION,
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

    async def _open(
        self, *, opportunity: AfterthoughtOpportunity, trigger_id: str, cursor: ProjectionCursor
    ) -> None:
        process = TriggerProcess(
            trigger_id=trigger_id,
            trigger_ref=f"afterthought:{opportunity.action_id}",
            process_kind=self.PROCESS_KIND,
            source_evidence_ref=opportunity.receipt_event_ref,
            state="open",
        )
        payload = {"process": process.model_dump(mode="json")}
        await self._commit_event(
            event_type="TriggerProcessOpened",
            payload=payload,
            event_id="event:afterthought:opened:" + _digest(payload),
            idempotency_key=domain_idempotency_key(
                event_type="TriggerProcessOpened",
                world_id=self.ledger.world_id,
                payload=payload,
            ),
            opportunity=opportunity,
            cursor=cursor,
            commit_id="commit:afterthought:opened:" + _digest(payload),
        )

    async def _claim(
        self, *, process: TriggerProcess, opportunity: AfterthoughtOpportunity, projection
    ) -> TriggerProcess | None:
        at = projection.logical_time or opportunity.anchored_at
        if process.state == "claimed" and process.claim_lease is not None:
            if process.claim_lease.owner_id == self._owner and at <= process.claim_lease.expires_at:
                return process
            if at < process.claim_lease.expires_at:
                return None
        attempt_id = "attempt:afterthought-worker:" + _digest(
            {"trigger": process.trigger_id, "attempt": len(process.attempt_ids) + 1}
        )
        claimed = process.model_copy(
            update={
                "state": "claimed",
                "claim_lease": ClaimLease(
                    owner_id=self._owner,
                    attempt_id=attempt_id,
                    acquired_at=at,
                    expires_at=at + timedelta(seconds=self._lease_seconds),
                ),
                "attempt_ids": (*process.attempt_ids, attempt_id),
            }
        )
        event_type = (
            "TriggerProcessClaimed" if process.state == "open" else "TriggerProcessReclaimed"
        )
        payload = {"process": claimed.model_dump(mode="json")}
        try:
            await self._commit_event(
                event_type=event_type,
                payload=payload,
                event_id="event:afterthought:claim:" + _digest(payload),
                idempotency_key=(
                    domain_idempotency_key(
                        event_type=event_type, world_id=self.ledger.world_id, payload=payload
                    )
                    or "world-v2:afterthought-claim:" + _digest(payload)
                ),
                opportunity=opportunity,
                cursor=self._cursor(projection),
                commit_id="commit:afterthought:claim:" + _digest(payload),
            )
        except ConcurrencyConflict:
            return None
        return claimed

    async def _complete(
        self, *, process: TriggerProcess, opportunity: AfterthoughtOpportunity, outcome: str
    ) -> None:
        if process.claim_lease is None:
            raise ValueError("afterthought completion requires a claimed process")
        projection = await self._project()
        current = next(
            (
                item
                for item in projection.trigger_processes
                if item.trigger_id == process.trigger_id
            ),
            None,
        )
        if current is not None and current.state == "terminal":
            return
        payload = {
            "trigger_id": process.trigger_id,
            "owner_id": process.claim_lease.owner_id,
            "attempt_id": process.claim_lease.attempt_id,
            "completed_at": (projection.logical_time or opportunity.anchored_at).isoformat(),
            "runtime_outcome_ref": f"afterthought:{outcome}",
        }
        await self._commit_event(
            event_type="TriggerProcessCompleted",
            payload=payload,
            event_id="event:afterthought:completed:" + _digest(payload),
            idempotency_key="world-v2:afterthought-completed:"
            + _digest({"world": self.ledger.world_id, "payload": payload}),
            opportunity=opportunity,
            cursor=self._cursor(projection),
            commit_id="commit:afterthought:completed:" + _digest(payload),
        )

    async def _commit_event(
        self,
        *,
        event_type: str,
        payload: dict[str, object],
        event_id: str,
        idempotency_key: str | None,
        opportunity: AfterthoughtOpportunity,
        cursor: ProjectionCursor,
        commit_id: str,
    ) -> None:
        if idempotency_key is None:
            raise ValueError("afterthought lifecycle event lacks identity")
        projection_time = (await self._project()).logical_time or opportunity.anchored_at
        event = WorldEvent.from_payload(
            schema_version="world-v2.1",
            event_id=event_id,
            world_id=self.ledger.world_id,
            event_type=event_type,
            logical_time=projection_time,
            created_at=projection_time,
            actor=self._owner,
            source=self._source,
            trace_id=opportunity.trace_id,
            causation_id=opportunity.receipt_event_ref,
            correlation_id=opportunity.correlation_id,
            idempotency_key=idempotency_key,
            payload=payload,
        )
        kwargs = dict(events=(event,), expected_cursor=cursor, commit_id=commit_id)
        if self.ledger.blocks_event_loop:
            await asyncio.to_thread(self.ledger.commit_at_cursor, **kwargs)
        else:
            self.ledger.commit_at_cursor(**kwargs)

    async def _project(self):
        if self.ledger.blocks_event_loop:
            return await asyncio.to_thread(self.ledger.project)
        return self.ledger.project()

    @staticmethod
    def _cursor(projection) -> ProjectionCursor:
        return ProjectionCursor(
            world_revision=projection.world_revision,
            deliberation_revision=projection.deliberation_revision,
            ledger_sequence=projection.ledger_sequence,
        )


__all__ = [
    "AFTERTHOUGHT_PROPOSAL_PREFIX",
    "AfterthoughtAuthorRuntime",
    "AfterthoughtContextPolicy",
    "AfterthoughtDecisionProfile",
    "AfterthoughtGate",
    "AfterthoughtMode",
    "AfterthoughtOpportunity",
    "AfterthoughtPolicy",
    "AfterthoughtRunResult",
    "AfterthoughtVerdict",
    "afterthought_attempt_id",
    "materialize_afterthought_proposal",
    "parse_afterthought_verdict",
]

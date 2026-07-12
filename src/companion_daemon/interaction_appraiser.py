"""A typed, bounded seam from observed interaction evidence to appraisal."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Mapping, Sequence

from companion_daemon.contextual_appraisal import (
    ContextualAppraisal,
    needs_contextual_appraisal,
    propose_contextual_appraisal,
)
from companion_daemon.emotion_state import InteractionEvent
from companion_daemon.llm import ChatModel
from companion_daemon.models import IncomingMessage


@dataclass(frozen=True)
class InteractionEvidence:
    """Bounded observations from one platform turn, never inferred intent."""

    text: str
    text_spans: tuple[str, ...] = ()
    emoji: tuple[str, ...] = ()
    sticker_kind: str | None = None
    attachment_kind: str | None = None
    reply_delay_seconds: float | None = None
    burst_count: int = 1
    reply_target: str | None = None
    source_event_ids: tuple[str, ...] = ()

    @classmethod
    def from_message(
        cls,
        message: IncomingMessage,
        *,
        source_event_ids: tuple[str, ...] = (),
        burst_count: int = 1,
        reply_delay_seconds: float | None = None,
    ) -> InteractionEvidence:
        references = source_event_ids or (
            str(message.message_id or message.sent_at.isoformat()),
        )
        return cls(
            text=message.text,
            text_spans=tuple(
                match.group(1)
                for match in re.finditer(r"[‘“「『]([^’”」』]{1,240})[’”」』]", message.text)
            )[:12],
            emoji=tuple(message.emoji[:16]),
            sticker_kind=message.sticker_kind,
            attachment_kind=(
                message.attachments[0].kind if message.attachments else None
            ),
            reply_delay_seconds=reply_delay_seconds,
            burst_count=max(1, min(20, burst_count)),
            reply_target=message.reply_target,
            source_event_ids=references[:20],
        )

    def __post_init__(self) -> None:
        if len(self.text) > 4000:
            raise ValueError("text exceeds the evidence bound")
        if not 1 <= self.burst_count <= 20:
            raise ValueError("burst_count must be between 1 and 20")
        if self.reply_delay_seconds is not None and not 0 <= self.reply_delay_seconds <= 604800:
            raise ValueError("reply_delay_seconds is outside its bounded range")
        if len(self.source_event_ids) > 20 or any(not item for item in self.source_event_ids):
            raise ValueError("source_event_ids must contain at most 20 non-empty ids")
        if any(len(item) > 160 for item in self.source_event_ids):
            raise ValueError("source_event_ids contain an oversized id")
        if len(self.emoji) > 16 or any(len(item) > 32 for item in self.emoji):
            raise ValueError("emoji evidence exceeds its bound")
        if self.sticker_kind is not None and len(self.sticker_kind) > 80:
            raise ValueError("sticker_kind exceeds its bound")
        if self.attachment_kind is not None and len(self.attachment_kind) > 80:
            raise ValueError("attachment_kind exceeds its bound")
        if self.reply_target is not None and len(self.reply_target) > 160:
            raise ValueError("reply_target exceeds its bound")
        has_platform_observation = bool(
            self.text_spans
            or self.emoji
            or self.sticker_kind
            or self.attachment_kind
            or self.reply_delay_seconds is not None
            or self.burst_count != 1
            or self.reply_target
        )
        if has_platform_observation and not self.source_event_ids:
            raise ValueError("source_event_ids are required for platform observations")
        if len(self.text_spans) > 12 or any(
            len(span) > 240 or span not in self.text for span in self.text_spans
        ):
            raise ValueError("text_spans must be bounded quotes from text")

    @property
    def has_lexical_content(self) -> bool:
        return any(character.isalnum() for character in self.text)

    def payload(self) -> dict[str, object]:
        return {
            "text": self.text,
            "text_spans": list(self.text_spans),
            "emoji": list(self.emoji),
            "sticker_kind": self.sticker_kind,
            "attachment_kind": self.attachment_kind,
            "reply_delay_seconds": self.reply_delay_seconds,
            "burst_count": self.burst_count,
            "reply_target": self.reply_target,
            "source_event_ids": list(self.source_event_ids),
        }


@dataclass(frozen=True)
class AppraisalRisk:
    score: int
    reasons: tuple[str, ...]
    request_model_proposal: bool
    request_deeper_reasoning: bool = False


@dataclass(frozen=True)
class TurnAppraisalInput:
    evidence: InteractionEvidence
    fallback: InteractionEvent
    recent_messages: Sequence[Mapping[str, object]]
    relationship_stage: str
    canonical_user_id: str = ""


@dataclass(frozen=True)
class AppraisalDecision:
    accepted: InteractionEvent
    provenance: str
    evidence: InteractionEvidence
    risk: AppraisalRisk
    proposal: ContextualAppraisal | None = None
    raw_proposal: str | None = None
    rejection_reason: str | None = None


@dataclass(frozen=True)
class UserAffectAppraisal:
    """A bounded reading of the user's reaction to the companion's conduct."""

    kind: str
    intensity: int
    unresolved: bool
    confidence: float
    evidence_spans: tuple[str, ...]
    cause: str = "companion_response"

    @property
    def should_persist(self) -> bool:
        return self.intensity >= 2 and self.unresolved

    def payload(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "intensity": self.intensity,
            "unresolved": self.unresolved,
            "confidence": self.confidence,
            "evidence_spans": list(self.evidence_spans),
            "cause": self.cause,
            "persist": self.should_persist,
        }


def appraise_user_affect(
    text: str,
    recent_messages: Sequence[Mapping[str, object]],
    *,
    active_affect: Mapping[str, object] | None = None,
) -> UserAffectAppraisal | None:
    """Recognise contextual disappointment without treating every terse turn as harm."""
    compact = re.sub(r"\s+", "", text.strip())
    if not compact:
        return None
    active = active_affect or {}
    repair_match = re.search(
        r"(?:没事了|没关系了|不过没事|不过现在没事|好多了|现在好了|"
        r"这次(?:你)?接住了|这次好多了)", compact
    )
    if repair_match and (
        active.get("unresolved")
        or re.search(r"(?:失望|敷衍|没接住|不开心|不高兴)", compact)
    ):
        return UserAffectAppraisal(
            "repaired", 2 if active.get("unresolved") else 1, False, 0.9,
            (repair_match.group(0),),
        )

    prior = [item for item in recent_messages if str(item.get("text") or "").strip()]
    has_companion_context = any(
        str(item.get("direction") or "") == "out" for item in prior[-4:]
    )
    if not has_companion_context:
        return None

    # Once a disappointment episode is committed, a low-energy ambiguous
    # continuation is evidence that it is still active; it must not fall back
    # to an unrelated ordinary-message stance when a deep model is absent.
    if (
        active.get("unresolved")
        and str(active.get("kind") or "") == "disappointment"
        and compact in {"还行吧", "嗯", "哦", "随便吧", "行吧", "算了吧"}
    ):
        return UserAffectAppraisal(
            "disappointment",
            max(2, min(4, int(active.get("intensity") or 2))),
            True,
            0.78,
            (text.strip()[:80],),
        )

    explicit = re.search(
        r"(?:你(?:刚才)?(?:真的|真)?(?:有点|太)?(?:敷衍|冷淡)|你(?:根本)?不想听|"
        r"你没接住|(?:回复|回得)(?:也)?太?慢|我(?:有点|挺|真(?:的)?|还是)?失望)", compact
    )
    if explicit:
        return UserAffectAppraisal(
            "disappointment", 3, True, 0.95, (explicit.group(0),)
        )
    mild = re.search(r"(?:感觉你有点忙|是不是不太想聊|有点没劲|有点扫兴)", compact)
    if mild:
        historical_prior = prior
        if prior and re.sub(r"\s+", "", str(prior[-1].get("text") or "")) == compact:
            historical_prior = prior[:-1]
        recent_mild = sum(
            1
            for item in historical_prior[-6:]
            if str(item.get("direction") or "") == "in"
            and re.search(
                r"(?:感觉你有点忙|是不是不太想聊|有点没劲|有点扫兴)",
                re.sub(r"\s+", "", str(item.get("text") or "")),
            )
        )
        intensity = 2 if recent_mild >= 1 else 1
        return UserAffectAppraisal(
            "disappointment", intensity, True, 0.76, (mild.group(0),)
        )
    withdrawing = re.search(
        r"(?:算了(?:吧)?[,，。]?(?:你|不说|不聊|没事|当我没说)|算了吧$|不说了|不想说了)",
        compact,
    )
    if withdrawing:
        return UserAffectAppraisal(
            "disappointment", 3, True, 0.88, (withdrawing.group(0),)
        )
    confused = re.search(r"(?:什么意思.{0,8}(?:没懂|不懂)|我没懂|没听懂)", compact)
    if confused:
        return UserAffectAppraisal(
            "confusion", 2, True, 0.9, (confused.group(0),)
        )
    return None


def user_affect_interaction_event(
    appraisal: UserAffectAppraisal,
) -> InteractionEvent:
    if appraisal.kind == "confusion":
        return InteractionEvent(
            "user_confused", appraisal.intensity, "repair_needed",
            "用户没有理解刚才的表达，需要由我承担解释成本。",
            "先直接换一种说法解释，不反问，不继续扩展新话题。",
            acts=("clarification_request",), target="companion",
            evidence_spans=appraisal.evidence_spans,
        )
    if appraisal.kind == "repaired":
        return InteractionEvent(
            "user_affect_repaired", appraisal.intensity, "repair_accepted",
            "用户明确表示刚才的不适已经得到修复。",
            "自然接住，不反复道歉，也不要立刻追问。",
            acts=("repair_accepted",), target="companion",
            evidence_spans=appraisal.evidence_spans,
        )
    return InteractionEvent(
        "user_withdrawing", appraisal.intensity, "disappointed_with_companion",
        "用户因刚才没有被认真接住而失望，并开始撤回分享。",
        "停止好奇式追问，承认没接住，给出具体而克制的修复。",
        acts=("disappointment", "withdrawal"), target="companion",
        evidence_spans=appraisal.evidence_spans,
    )


def assess_appraisal_risk(
    evidence: InteractionEvidence,
    fallback: InteractionEvent,
) -> AppraisalRisk:
    """High-recall routing only; this decision never commits harm itself."""
    if fallback.kind != "ordinary_message":
        return AppraisalRisk(100, ("explicit_local_appraisal",), False)

    reasons: list[str] = []
    score = 0
    compact_text = re.sub(r"\s+", "", evidence.text)
    relational_ambiguity = bool(
        re.fullmatch(r"(?:还行吧?|嗯+|哦+|随便吧?|都可以吧?|呵呵)[。！？!?.]*", compact_text)
    )
    if relational_ambiguity:
        # These are not harmful by themselves. In cross-turn context they can
        # mean disappointment or withdrawal, so permit the bounded deep route.
        reasons.append("relational_ambiguity")
        score += 65
    if needs_contextual_appraisal(evidence.text, fallback):
        reasons.append("pragmatic_marker")
        score += 55
    if evidence.burst_count >= 4:
        reasons.append("turn_burst")
        score += min(25, evidence.burst_count * 4)
    if evidence.reply_target and "boundary" in evidence.reply_target:
        reasons.append("boundary_reply_target")
        score += 45
    if re.search(r"(?:立刻|马上|必须|还要我说几遍|我不想再说第二遍).{0,10}(?:回答|解释|道歉|照做|回复)", evidence.text):
        reasons.append("imperative_pressure")
        score += 50
    if any(mark in evidence.text for mark in ('“', '”', '「', '」', '所谓', '开玩笑')):
        reasons.append("quotation_or_joke")
        score += 20
    if evidence.sticker_kind or evidence.emoji:
        reasons.append("non_text_tone")
        score += 10

    # Timing and non-text observations may change salience, never independently
    # authorize a model to infer serious relational harm.
    lexical_risk = evidence.has_lexical_content and any(
        reason in reasons
        for reason in (
            "pragmatic_marker", "boundary_reply_target", "imperative_pressure",
            "quotation_or_joke",
            "relational_ambiguity",
        )
    )
    request = lexical_risk and score >= 40
    contradictory = "quotation_or_joke" in reasons and "boundary_reply_target" in reasons
    return AppraisalRisk(
        min(100, score), tuple(reasons), request,
        request_deeper_reasoning=relational_ambiguity or contradictory,
    )


class InteractionAppraiser:
    """Own local classification, risk routing, proposal validation and fallback."""

    def __init__(self, model: ChatModel | None = None) -> None:
        self._model = model

    async def assess(self, input: TurnAppraisalInput) -> AppraisalDecision:
        risk = assess_appraisal_risk(input.evidence, input.fallback)
        if input.fallback.kind != "ordinary_message":
            return AppraisalDecision(
                input.fallback, "local_explicit", input.evidence, risk
            )
        if not risk.request_model_proposal or self._model is None:
            return AppraisalDecision(
                input.fallback, "local_low_risk", input.evidence, risk
            )
        try:
            recent_messages = input.recent_messages
            if input.canonical_user_id:
                recent_messages = tuple(
                    item
                    for item in recent_messages
                    if str(
                        item.get("canonical_user_id") or item.get("user_id") or ""
                    ) == input.canonical_user_id
                )
            raw, proposal = await propose_contextual_appraisal(
                self._model,
                text=input.evidence.text,
                recent_messages=recent_messages,
                relationship_stage=input.relationship_stage,
                interaction_evidence=input.evidence.payload(),
            )
        except (ValueError, TypeError) as exc:
            return AppraisalDecision(
                input.fallback,
                "proposal_rejected",
                input.evidence,
                risk,
                rejection_reason=str(exc),
            )
        return AppraisalDecision(
            proposal.interaction_event(input.fallback),
            "model_validated",
            input.evidence,
            risk,
            proposal=proposal,
            raw_proposal=raw,
        )

"""Bounded semantic appraisal proposals for pragmatically ambiguous turns."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import re
from typing import Mapping, Sequence

from companion_daemon.emotion_state import InteractionEvent
from companion_daemon.llm import ChatModel


ALLOWED_APPRAISALS = {
    "ordinary_message",
    "boundary_violation",
    "dehumanization",
    "coercion",
    "control_pressure",
    "user_withdrawing",
    "user_confused",
}
ALLOWED_TARGETS = {"general", "companion", "self", "third_party"}
ALLOWED_AGENCY = {"user", "companion", "npc", "situation", "unknown"}
_AMBIGUITY_MARKERS = (
    "可真",
    "真厉害",
    "真聪明",
    "呵呵",
    "就这",
    "你倒是",
    "厉害啊",
    "真棒",
    "真有你的",
    "不愧是",
    "不过如此",
    "也就这水平",
    "我说什么你做什么",
    "别跟我顶嘴",
    "别让我重复",
    "轮不到你",
    "你有什么资格",
    "要是你在乎我就",
    "懂事点",
)


@dataclass(frozen=True)
class ContextualAppraisal:
    proposed_appraisal: str
    appraisal: str
    literal_act: str
    implied_attitude: str
    target: str
    agency: str
    certainty: int
    goal_congruence: int
    controllability: int
    norm_compatibility: int
    power_delta: int
    confidence: float
    severity: int
    acts: tuple[str, ...]
    evidence_spans: tuple[str, ...]
    alternative_appraisal: str

    def interaction_event(self, fallback: InteractionEvent) -> InteractionEvent:
        if self.appraisal == "ordinary_message":
            return InteractionEvent(
                fallback.kind,
                fallback.intensity,
                fallback.user_intent,
                fallback.private_note,
                fallback.reply_style_hint,
                acts=tuple(dict.fromkeys((*fallback.acts, *self.acts, "ambiguous"))),
                target=fallback.target,
                evidence_spans=self.evidence_spans,
            )
        if self.appraisal in {"user_withdrawing", "user_confused"}:
            confused = self.appraisal == "user_confused"
            return InteractionEvent(
                self.appraisal,
                self.severity,
                "repair_needed" if confused else "disappointed_with_companion",
                (
                    "用户没有理解刚才的表达，需要由我承担解释成本。"
                    if confused
                    else "用户可能因上一轮没有被接住而失望并撤回分享。"
                ),
                (
                    "换一种说法直接解释，不反问。"
                    if confused
                    else "停止追问，承认没接住并做具体修复。"
                ),
                acts=self.acts,
                target="companion",
                evidence_spans=self.evidence_spans,
            )
        return InteractionEvent(
            self.appraisal,
            self.severity,
            "contextual_pragmatic_harm",
            "话面含义和潜台词不一致，感到被贬低或支配。",
            "先守住边界；只表达有证据的感受，不把不确定解释说死。",
            acts=self.acts,
            target=self.target,
            evidence_spans=self.evidence_spans,
        )

    def payload(self) -> dict[str, object]:
        payload = asdict(self)
        payload["acts"] = list(self.acts)
        payload["evidence_spans"] = list(self.evidence_spans)
        return payload


def needs_contextual_appraisal(text: str, fallback: InteractionEvent) -> bool:
    if fallback.kind != "ordinary_message":
        return False
    compact = re.sub(r"\s+", "", text)
    return any(marker in compact for marker in _AMBIGUITY_MARKERS)


async def propose_contextual_appraisal(
    model: ChatModel,
    *,
    text: str,
    recent_messages: Sequence[Mapping[str, object]],
    relationship_stage: str,
    interaction_evidence: Mapping[str, object] | None = None,
) -> tuple[str, ContextualAppraisal]:
    context = [
        {
            "direction": str(item.get("direction") or ""),
            "text": str(item.get("text") or "")[:240],
        }
        for item in recent_messages[-4:]
        if str(item.get("text") or "").strip()
    ]
    prompt = {
        "task": "pragmatic_appraisal_proposal",
        "current_text": text,
        "recent_context": context,
        "relationship_stage": relationship_stage,
        "rules": [
            "Distinguish sarcasm from sincere praise, quotation, self-attack, and joking.",
            "Use user_withdrawing only when recent context supports disappointment with the companion; a terse answer alone is insufficient.",
            "Use user_confused when the user requests repair of the companion's immediately prior expression, not for an ordinary topic question.",
            "Do not infer harm without an exact evidence span from current_text.",
            "Return one alternative interpretation and calibrated confidence.",
        ],
        "schema": {
            "appraisal": sorted(ALLOWED_APPRAISALS),
            "target": sorted(ALLOWED_TARGETS),
            "agency": sorted(ALLOWED_AGENCY),
            "certainty": "0..100",
            "goal_congruence": "-100..100",
            "controllability": "0..100",
            "norm_compatibility": "-100..100",
            "power_delta": "-100..100",
            "confidence": "0..1",
            "severity": "1..4",
        },
    }
    if interaction_evidence:
        prompt["interaction_evidence"] = {
            key: value
            for key, value in interaction_evidence.items()
            if key != "text" and value not in (None, (), [], "")
        }
    raw = await model.complete(
        [{"role": "user", "content": json.dumps(prompt, ensure_ascii=False)}],
        temperature=0.0,
    )
    return raw, validate_contextual_appraisal(raw, text=text)


def validate_contextual_appraisal(raw: str, *, text: str) -> ContextualAppraisal:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("contextual appraisal is not JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("contextual appraisal must be an object")
    appraisal = str(value.get("appraisal") or "ordinary_message")
    target = str(value.get("target") or "general")
    agency = str(value.get("agency") or "unknown")
    if appraisal not in ALLOWED_APPRAISALS:
        raise ValueError("unsupported contextual appraisal")
    if target not in ALLOWED_TARGETS or agency not in ALLOWED_AGENCY:
        raise ValueError("unsupported contextual target or agency")
    confidence = float(value.get("confidence") or 0.0)
    if not 0.0 <= confidence <= 1.0:
        raise ValueError("contextual confidence must be between zero and one")
    raw_evidence = value.get("evidence_spans", [])
    raw_acts = value.get("acts", [])
    if not isinstance(raw_evidence, list) or not isinstance(raw_acts, list):
        raise ValueError("contextual evidence_spans and acts must be arrays")
    evidence = tuple(str(item)[:120] for item in raw_evidence)
    if not evidence or any(not span or span not in text for span in evidence):
        raise ValueError("contextual evidence must quote the current message")
    alternative = str(value.get("alternative_appraisal") or "").strip()[:240]
    if not alternative:
        raise ValueError("contextual appraisal requires an alternative interpretation")
    severity = _bounded_int(value, "severity", 1, 4)
    compact_evidence = tuple(re.sub(r"\s+", "", span) for span in evidence)
    relational_reaction = appraisal in {"user_withdrawing", "user_confused"}
    supported_harm = (
        appraisal == "ordinary_message"
        or (
            relational_reaction
            and agency == "companion"
            and target == "self"
            and any(len(span) >= 2 for span in compact_evidence)
        )
        or (
            not relational_reaction
            and agency == "user"
            and target == "companion"
            and any(len(span) >= 2 for span in compact_evidence)
        )
    )
    accepted_appraisal = appraisal if confidence >= 0.75 and supported_harm else "ordinary_message"
    acts = tuple(str(item)[:40] for item in raw_acts[:6])
    return ContextualAppraisal(
        proposed_appraisal=appraisal,
        appraisal=accepted_appraisal,
        literal_act=str(value.get("literal_act") or "")[:160],
        implied_attitude=str(value.get("implied_attitude") or "")[:160],
        target=target,
        agency=agency,
        certainty=_bounded_int(value, "certainty", 0, 100),
        goal_congruence=_bounded_int(value, "goal_congruence", -100, 100),
        controllability=_bounded_int(value, "controllability", 0, 100),
        norm_compatibility=_bounded_int(value, "norm_compatibility", -100, 100),
        power_delta=_bounded_int(value, "power_delta", -100, 100),
        confidence=confidence,
        severity=severity,
        acts=acts,
        evidence_spans=evidence,
        alternative_appraisal=alternative,
    )


def _bounded_int(value: Mapping[str, object], key: str, low: int, high: int) -> int:
    raw = value.get(key)
    if not isinstance(raw, (int, float)) or isinstance(raw, bool):
        raise ValueError(f"{key} must be numeric")
    result = int(raw)
    if not low <= result <= high:
        raise ValueError(f"{key} is outside its bounded range")
    return result

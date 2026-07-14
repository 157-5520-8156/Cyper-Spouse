"""Semantic advisory for rare companion-side conversational interruption.

The normal turn-taking policy decides whether a user burst looks complete.
This module is deliberately narrower: when the user looks mid-flow, a small
model may advise that a human companion would plausibly jump in because the
current partial utterance is interesting, wrong, emotionally charged, or needs
immediate grounding.  It never dispatches text and never writes World state.
"""

from __future__ import annotations

from dataclasses import dataclass
import asyncio
import json
from typing import Mapping, Protocol

from companion_daemon.llm import ChatModel


@dataclass(frozen=True)
class CompanionInterruptionContext:
    pending_count: int
    latest_text: str
    merged_text: str
    cadence_heat: str
    relationship_stage: str = "unknown"
    base_wait_seconds: float = 0.0
    base_reason: str = ""


@dataclass(frozen=True)
class CompanionInterruptionAdvice:
    should_interrupt: bool
    motive: str
    confidence: float
    wait_seconds: float
    evidence_spans: tuple[str, ...]
    rationale: str = ""

    @property
    def reason(self) -> str:
        motive = self.motive.strip() or "semantic"
        return f"semantic_companion_interruption:{motive}"


class CompanionInterruptionAdvisor(Protocol):
    async def advise(
        self, context: CompanionInterruptionContext
    ) -> CompanionInterruptionAdvice | None: ...


class ModelCompanionInterruptionAdvisor:
    """Small-model adapter for companion-side turn-taking impulses."""

    _ALLOWED_MOTIVES = {
        "interest",
        "disagreement",
        "misunderstanding",
        "boundary",
        "safety",
        "emotional_resonance",
        "none",
    }

    def __init__(
        self,
        model: ChatModel,
        *,
        timeout_seconds: float = 0.35,
        min_confidence: float = 0.68,
    ) -> None:
        self.model = model
        self.timeout_seconds = max(0.05, timeout_seconds)
        self.min_confidence = max(0.0, min(1.0, min_confidence))

    async def advise(
        self, context: CompanionInterruptionContext
    ) -> CompanionInterruptionAdvice | None:
        try:
            async with asyncio.timeout(self.timeout_seconds):
                raw = await self.model.complete(
                    [
                        {
                            "role": "system",
                            "content": (
                                "Return strict JSON only. Decide whether a human "
                                "companion would briefly jump in before the user "
                                "finishes the current IM burst. This is an advisory "
                                "for timing, not reply content."
                            ),
                        },
                        {
                            "role": "user",
                            "content": json.dumps(
                                {
                                    "latest_text": context.latest_text,
                                    "merged_text": context.merged_text,
                                    "pending_count": context.pending_count,
                                    "cadence_heat": context.cadence_heat,
                                    "relationship_stage": context.relationship_stage,
                                    "base_wait_seconds": context.base_wait_seconds,
                                    "base_reason": context.base_reason,
                                    "allowed_motives": sorted(self._ALLOWED_MOTIVES),
                                    "schema": {
                                        "should_interrupt": "boolean",
                                        "motive": "interest|disagreement|misunderstanding|boundary|safety|emotional_resonance|none",
                                        "confidence": "0..1",
                                        "wait_seconds": "0..1.2",
                                        "evidence_spans": "quoted substrings from latest_text",
                                        "rationale": "short",
                                    },
                                },
                                ensure_ascii=False,
                                separators=(",", ":"),
                            ),
                        },
                    ],
                    temperature=0.15,
                )
        except Exception:
            return None
        return interruption_advice_from_model_json(
            context,
            raw,
            min_confidence=self.min_confidence,
        )


def interruption_advice_from_model_json(
    context: CompanionInterruptionContext,
    raw: str,
    *,
    min_confidence: float = 0.68,
) -> CompanionInterruptionAdvice | None:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, Mapping):
        return None
    should_interrupt = bool(parsed.get("should_interrupt"))
    motive = str(parsed.get("motive") or "none").strip()
    if not should_interrupt or motive == "none":
        return None
    if motive not in ModelCompanionInterruptionAdvisor._ALLOWED_MOTIVES:
        return None
    try:
        confidence = float(parsed.get("confidence") or 0.0)
    except (TypeError, ValueError):
        return None
    if confidence < min_confidence or confidence > 1.0:
        return None
    spans = tuple(
        dict.fromkeys(
            span.strip()[:120]
            for span in parsed.get("evidence_spans", [])
            if isinstance(span, str)
            and span.strip()
            and span.strip() in context.latest_text
        )
    )
    if not spans:
        return None
    try:
        wait_seconds = float(parsed.get("wait_seconds") or 0.0)
    except (TypeError, ValueError):
        wait_seconds = 0.0
    return CompanionInterruptionAdvice(
        should_interrupt=True,
        motive=motive,
        confidence=confidence,
        wait_seconds=max(0.0, min(1.2, wait_seconds)),
        evidence_spans=spans,
        rationale=str(parsed.get("rationale") or "")[:160],
    )

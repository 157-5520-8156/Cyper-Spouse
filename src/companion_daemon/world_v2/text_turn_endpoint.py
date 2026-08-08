"""Advisory, model-backed endpoint timing for typed conversation.

An endpoint estimate answers only one transport question: how likely is the
counterpart to add another bubble soon?  It has no authority over character
silence, wording, stance, interruption, or Action creation.  The controller
turns that probability and provider-observed timing evidence into one bounded
listening opportunity; the role model still makes the conversational choice.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import math
from time import perf_counter
from typing import Protocol


def _basis_points(name: str, value: int) -> None:
    if isinstance(value, bool) or not 0 <= value <= 10_000:
        raise ValueError(f"{name} must be integer basis points")


@dataclass(frozen=True, slots=True)
class TextTurnEndpointEvidence:
    """Provider-local evidence available before a World turn is committed."""

    batch_texts: tuple[str, ...]
    recent_gap_seconds: tuple[float, ...]
    typing_active: bool
    burst_fragment_count: int
    recent_message_character_counts: tuple[int, ...]
    recent_character_message_character_counts: tuple[int, ...] = ()
    recent_exchange_user_bubble_counts: tuple[int, ...] = ()
    recent_exchange_character_bubble_counts: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        if not 1 <= len(self.batch_texts) <= 8:
            raise ValueError("endpoint evidence requires one to eight text fragments")
        if any(not item or len(item) > 12_000 for item in self.batch_texts):
            raise ValueError("endpoint evidence text is empty or too large")
        if len(self.recent_gap_seconds) > 16 or any(
            not math.isfinite(item) or item <= 0 or item > 30
            for item in self.recent_gap_seconds
        ):
            raise ValueError("endpoint cadence evidence is out of bounds")
        if not 1 <= self.burst_fragment_count <= 16:
            raise ValueError("endpoint burst size is out of bounds")
        if len(self.recent_message_character_counts) > 16 or any(
            isinstance(item, bool) or not 1 <= item <= 12_000
            for item in self.recent_message_character_counts
        ):
            raise ValueError("endpoint message-length evidence is out of bounds")
        if len(self.recent_character_message_character_counts) > 16 or any(
            isinstance(item, bool) or not 1 <= item <= 12_000
            for item in self.recent_character_message_character_counts
        ):
            raise ValueError("endpoint character message-length evidence is out of bounds")
        for name, counts in (
            ("recent_exchange_user_bubble_counts", self.recent_exchange_user_bubble_counts),
            (
                "recent_exchange_character_bubble_counts",
                self.recent_exchange_character_bubble_counts,
            ),
        ):
            if len(counts) > 16 or any(
                isinstance(item, bool) or not 0 <= item <= 16 for item in counts
            ):
                raise ValueError(f"endpoint {name} evidence is out of bounds")
        if len(self.recent_exchange_user_bubble_counts) != len(
            self.recent_exchange_character_bubble_counts
        ):
            raise ValueError("endpoint exchange-shape evidence must be paired")


@dataclass(frozen=True, slots=True)
class SemanticEndpointPrediction:
    """One inert probability estimate produced by a semantic endpoint model."""

    continuation_probability_bp: int
    confidence_bp: int
    evidence_summary: str
    model_id: str
    compact_reply_hint_bp: int = 5_000

    def __post_init__(self) -> None:
        _basis_points("continuation_probability_bp", self.continuation_probability_bp)
        _basis_points("confidence_bp", self.confidence_bp)
        _basis_points("compact_reply_hint_bp", self.compact_reply_hint_bp)
        if not self.evidence_summary or len(self.evidence_summary) > 512:
            raise ValueError("endpoint evidence summary is empty or too large")
        if not self.model_id or len(self.model_id) > 256:
            raise ValueError("endpoint model id is empty or too large")


class SemanticEndpointModel(Protocol):
    async def predict(
        self, evidence: TextTurnEndpointEvidence
    ) -> SemanticEndpointPrediction: ...


class _EndpointChatModel(Protocol):
    async def complete(
        self, messages: list[dict[str, str]], *, temperature: float = 0.0
    ) -> str: ...


class ChatSemanticEndpointModel:
    """Use one small chat model as a probability estimator, never a character."""

    _OUTPUT_KEYS = frozenset(
        {
            "continuation_probability_bp",
            "confidence_bp",
            "evidence_summary",
            "compact_reply_hint_bp",
        }
    )

    def __init__(self, model: _EndpointChatModel) -> None:
        self._model = model
        self.model = str(getattr(model, "model", type(model).__name__))[:256]

    async def predict(
        self, evidence: TextTurnEndpointEvidence
    ) -> SemanticEndpointPrediction:
        messages = [
            {
                "role": "system",
                "content": (
                    "You are a typed-conversation endpoint estimator. Estimate only the "
                    "probability that the same user will add another message bubble to the "
                    "current thought soon, and how suitable a short single reply would be. "
                    "You have no authority over whether or how the "
                    "character replies, waits, interrupts, or stays silent. Return exactly "
                    "one JSON object with continuation_probability_bp (integer 0..10000), "
                    "confidence_bp (integer 0..10000), evidence_summary (brief text), and "
                    "compact_reply_hint_bp (integer 0..10000: how well a short single text "
                    "reply fits this message; high for greetings, small talk, and simple "
                    "questions, low for questions that need a factual answer or a story, "
                    "or messages with complex content). "
                    "Do not return a reply decision, response wording, motive, or character state."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "batch_texts": evidence.batch_texts,
                        "recent_gap_seconds": evidence.recent_gap_seconds,
                        "typing_active": evidence.typing_active,
                        "burst_fragment_count": evidence.burst_fragment_count,
                        "recent_message_character_counts": (
                            evidence.recent_message_character_counts
                        ),
                        "recent_character_message_character_counts": (
                            evidence.recent_character_message_character_counts
                        ),
                        "recent_exchange_user_bubble_counts": (
                            evidence.recent_exchange_user_bubble_counts
                        ),
                        "recent_exchange_character_bubble_counts": (
                            evidence.recent_exchange_character_bubble_counts
                        ),
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            },
        ]
        complete_json = getattr(self._model, "complete_json", None)
        raw = await (
            complete_json(messages, temperature=0.0)
            if callable(complete_json)
            else self._model.complete(messages, temperature=0.0)
        )
        try:
            value = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("endpoint model must return JSON") from exc
        if not isinstance(value, dict) or set(value) != self._OUTPUT_KEYS:
            raise ValueError("endpoint model must return exactly the endpoint probability fields")
        probability = value["continuation_probability_bp"]
        confidence = value["confidence_bp"]
        summary = value["evidence_summary"]
        compact_hint = value["compact_reply_hint_bp"]
        if not isinstance(probability, int) or isinstance(probability, bool):
            raise ValueError("endpoint continuation probability must be integer basis points")
        if not isinstance(confidence, int) or isinstance(confidence, bool):
            raise ValueError("endpoint confidence must be integer basis points")
        if not isinstance(summary, str):
            raise ValueError("endpoint evidence summary must be text")
        if not isinstance(compact_hint, int) or isinstance(compact_hint, bool):
            raise ValueError("endpoint compact reply hint must be integer basis points")
        return SemanticEndpointPrediction(
            continuation_probability_bp=probability,
            confidence_bp=confidence,
            evidence_summary=summary.strip(),
            model_id=self.model,
            compact_reply_hint_bp=compact_hint,
        )


@dataclass(frozen=True, slots=True)
class TextTurnEndpointSchedule:
    """Bounded opportunity timing; deliberately contains no reply decision."""

    status: str
    wait_ms: int
    semantic_continuation_probability_bp: int | None
    semantic_confidence_bp: int | None
    model_id: str | None
    reason_codes: tuple[str, ...]
    evaluated_in_ms: float
    failure_code: str | None = None
    semantic_evidence_summary: str | None = None
    semantic_compact_reply_hint_bp: int | None = None


class TextTurnEndpointController:
    """Fuse semantic probability with observed cadence without scripting behavior."""

    MIN_WAIT_MS = 100
    MAX_WAIT_MS = 2_500
    TYPING_FLOOR_MS = 1_500

    def __init__(
        self,
        *,
        model: SemanticEndpointModel | None,
        timeout_seconds: float = 0.20,
    ) -> None:
        if not 0.01 <= timeout_seconds <= 1.0:
            raise ValueError("endpoint timeout must be between 10ms and one second")
        self._model = model
        self._timeout_seconds = timeout_seconds
        self._evaluations = 0
        self._model_successes = 0
        self._fallbacks = 0
        self._last: TextTurnEndpointSchedule | None = None
        self._active_prediction: asyncio.Task[SemanticEndpointPrediction] | None = None
        self._closed = False

    async def schedule(
        self, evidence: TextTurnEndpointEvidence
    ) -> TextTurnEndpointSchedule:
        started = perf_counter()
        if self._closed:
            raise RuntimeError("text endpoint controller is closed")
        self._evaluations += 1
        prediction: SemanticEndpointPrediction | None = None
        failure_code: str | None = None
        if self._model is None:
            failure_code = "model_unavailable"
        else:
            active = self._active_prediction
            if active is not None and not active.done():
                # A stale estimate is not evidence for the new batch. Do not
                # queue another request behind it; fail open until the single
                # provider request reaches a terminal state.
                failure_code = "model_busy"
            else:
                active = asyncio.create_task(
                    self._model.predict(evidence),
                    name="text-turn-endpoint-prediction",
                )
                self._active_prediction = active

                def observe(
                    completed: asyncio.Task[SemanticEndpointPrediction],
                ) -> None:
                    if self._active_prediction is completed:
                        self._active_prediction = None
                    if not completed.cancelled():
                        completed.exception()

                active.add_done_callback(observe)
                try:
                    async with asyncio.timeout(self._timeout_seconds):
                        # Timing out the listener must not cancel a request
                        # which may still be running in the serial local model.
                        # Cancellation would poison its conservative capacity
                        # lease and suppress later endpoint predictions for minutes.
                        prediction = await asyncio.shield(active)
                except TimeoutError:
                    failure_code = "timeout"
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    failure_code = type(exc).__name__[:64]

        reason_codes: list[str] = []
        if evidence.typing_active:
            reason_codes.append("typing_active")
        if evidence.recent_gap_seconds:
            reason_codes.append("personal_cadence")
        if evidence.burst_fragment_count > 1:
            reason_codes.append("observed_burst")

        if prediction is None:
            self._fallbacks += 1
            wait_ms = self._fallback_wait_ms(evidence)
            reason_codes.append("semantic_endpoint_fallback")
            schedule = TextTurnEndpointSchedule(
                status="fallback",
                wait_ms=wait_ms,
                semantic_continuation_probability_bp=None,
                semantic_confidence_bp=None,
                model_id=None,
                reason_codes=tuple(reason_codes),
                evaluated_in_ms=max(0.0, (perf_counter() - started) * 1_000),
                failure_code=failure_code,
                semantic_evidence_summary=None,
                semantic_compact_reply_hint_bp=None,
            )
        else:
            self._model_successes += 1
            wait_ms = self._semantic_wait_ms(prediction, evidence)
            reason_codes.insert(0, "semantic_endpoint")
            schedule = TextTurnEndpointSchedule(
                status="predicted",
                wait_ms=wait_ms,
                semantic_continuation_probability_bp=(
                    prediction.continuation_probability_bp
                ),
                semantic_confidence_bp=prediction.confidence_bp,
                model_id=prediction.model_id,
                reason_codes=tuple(reason_codes),
                evaluated_in_ms=max(0.0, (perf_counter() - started) * 1_000),
                semantic_evidence_summary=prediction.evidence_summary,
                semantic_compact_reply_hint_bp=prediction.compact_reply_hint_bp,
            )
        self._last = schedule
        return schedule

    @classmethod
    def _semantic_wait_ms(
        cls,
        prediction: SemanticEndpointPrediction,
        evidence: TextTurnEndpointEvidence,
    ) -> int:
        probability = prediction.continuation_probability_bp
        # Treat a <=10% estimate as a confident endpoint. Above that point the
        # listening opportunity grows smoothly; no semantic label or message
        # wording is inspected by deterministic code.
        if probability <= 1_000:
            wait_ms = cls.MIN_WAIT_MS
        else:
            normalized = (probability - 1_000) / 9_000
            wait_ms = round(
                cls.MIN_WAIT_MS
                + (cls.MAX_WAIT_MS - cls.MIN_WAIT_MS) * normalized**1.35
            )
        cadence_floor = cls._cadence_floor_ms(evidence)
        if probability >= 3_500 and cadence_floor is not None:
            wait_ms = max(wait_ms, cadence_floor)
        if evidence.typing_active:
            wait_ms = max(wait_ms, cls.TYPING_FLOOR_MS)
        return min(cls.MAX_WAIT_MS, max(cls.MIN_WAIT_MS, wait_ms))

    @classmethod
    def _fallback_wait_ms(cls, evidence: TextTurnEndpointEvidence) -> int:
        wait_ms = cls.MIN_WAIT_MS
        # A missing semantic model must not make every ordinary bubble slow.
        # Only provider-observed continuation/typing can extend the fallback.
        if evidence.burst_fragment_count > 1:
            wait_ms = max(wait_ms, cls._cadence_floor_ms(evidence) or 650)
        if evidence.typing_active:
            wait_ms = max(wait_ms, cls.TYPING_FLOOR_MS)
        return min(cls.MAX_WAIT_MS, wait_ms)

    @classmethod
    def _cadence_floor_ms(cls, evidence: TextTurnEndpointEvidence) -> int | None:
        if not evidence.recent_gap_seconds:
            return None
        ordered = sorted(evidence.recent_gap_seconds)
        median = ordered[len(ordered) // 2]
        return min(cls.MAX_WAIT_MS, max(cls.MIN_WAIT_MS, round(median * 1_200)))

    def health_snapshot(self) -> dict[str, object]:
        last = self._last
        return {
            "enabled": self._model is not None,
            "status": (
                "not_measured"
                if last is None
                else "ok"
                if last.status == "predicted"
                else "degraded"
            ),
            "evaluations": self._evaluations,
            "model_successes": self._model_successes,
            "fallbacks": self._fallbacks,
            "prediction_in_flight": (
                self._active_prediction is not None
                and not self._active_prediction.done()
            ),
            "last_schedule": (
                None
                if last is None
                else {
                    "status": last.status,
                    "wait_ms": last.wait_ms,
                    "semantic_continuation_probability_bp": (
                        last.semantic_continuation_probability_bp
                    ),
                    "semantic_confidence_bp": last.semantic_confidence_bp,
                    "model_id": last.model_id,
                    "reason_codes": last.reason_codes,
                    "evaluated_in_ms": last.evaluated_in_ms,
                    "failure_code": last.failure_code,
                    "semantic_evidence_summary": last.semantic_evidence_summary,
                }
            ),
        }

    async def aclose(self) -> None:
        """Cancel the one owned prediction only while the host is shutting down."""

        self._closed = True
        active = self._active_prediction
        if active is not None and not active.done():
            active.cancel()
            done, pending = await asyncio.wait((active,), timeout=0.05)
            if pending:
                # The provider may suppress cancellation. Its existing done
                # callback observes the eventual terminal state and clears the
                # reference; daemon shutdown must not wait indefinitely.
                return
            for task in done:
                if not task.cancelled():
                    task.exception()


__all__ = [
    "ChatSemanticEndpointModel",
    "SemanticEndpointModel",
    "SemanticEndpointPrediction",
    "TextTurnEndpointController",
    "TextTurnEndpointEvidence",
    "TextTurnEndpointSchedule",
]

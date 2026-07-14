"""Fallible affect reading and replay-safe expression affordance selection.

This module deliberately does not mutate World state.  It reads a bounded
``TurnFrame`` and returns advisory data that may influence prompt wording and
Action trace metadata.  The World reducer remains the authority for committed
affect, relationship, private impressions and actions.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import re
from typing import Mapping, Protocol

from companion_daemon.llm import ChatModel
from companion_daemon.turn_frame import TurnFrame


RULE_VERSION = "affective-advisory-v1"


@dataclass(frozen=True)
class AffectiveReading:
    kind: str
    target: str
    intensity: int
    confidence: float
    evidence_spans: tuple[str, ...]
    uncertainty: str = ""
    ownership: str = "unknown"
    stakes: Mapping[str, float] | None = None

    def to_payload(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "target": self.target,
            "intensity": self.intensity,
            "confidence": self.confidence,
            "evidence_spans": list(self.evidence_spans),
            "uncertainty": self.uncertainty,
            "ownership": self.ownership,
            "stakes": dict(self.stakes or {}),
        }


@dataclass(frozen=True)
class ExpressionAffordance:
    kind: str
    weight: float
    reason: str
    constraints: Mapping[str, object] | None = None

    def to_payload(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "weight": self.weight,
            "reason": self.reason,
            "constraints": dict(self.constraints or {}),
        }


@dataclass(frozen=True)
class PersistenceCandidate:
    kind: str
    materiality: str
    reason: str
    confidence: float
    source_event_ids: tuple[str, ...]

    def to_payload(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "materiality": self.materiality,
            "reason": self.reason,
            "confidence": self.confidence,
            "source_event_ids": list(self.source_event_ids),
        }


@dataclass(frozen=True)
class SelectedAffordance:
    selected: ExpressionAffordance | None
    candidates: tuple[ExpressionAffordance, ...]
    seed_hash: str
    rule_version: str = RULE_VERSION

    def to_trace(self) -> dict[str, object]:
        return {
            "schema": "expression-affordance-selection-v1",
            "rule_version": self.rule_version,
            "seed_hash": self.seed_hash,
            "selected": self.selected.to_payload() if self.selected else None,
            "candidates": [item.to_payload() for item in self.candidates],
        }


@dataclass(frozen=True)
class AffectAdvisory:
    readings: tuple[AffectiveReading, ...]
    drive_deltas: Mapping[str, float]
    expression_affordances: tuple[ExpressionAffordance, ...]
    persistence_candidates: tuple[PersistenceCandidate, ...]
    confidence: float
    evidence_spans: tuple[str, ...]
    adapter: str
    selected_affordance: SelectedAffordance
    failed_reason: str | None = None
    rule_version: str = RULE_VERSION

    def prompt_payload(self) -> dict[str, object]:
        return {
            "rule_version": self.rule_version,
            "adapter": self.adapter,
            "failed_reason": self.failed_reason,
            "confidence": self.confidence,
            "readings": [item.to_payload() for item in self.readings],
            "drive_deltas": dict(self.drive_deltas),
            "expression_affordances": [
                item.to_payload() for item in self.expression_affordances
            ],
            "selected_affordance": (
                self.selected_affordance.selected.to_payload()
                if self.selected_affordance.selected
                else None
            ),
            "persistence_candidates": [
                item.to_payload() for item in self.persistence_candidates
            ],
            "evidence_spans": list(self.evidence_spans),
        }

    def trace_payload(self) -> dict[str, object]:
        return {
            "rule_version": self.rule_version,
            "adapter": self.adapter,
            "readings": [item.to_payload() for item in self.readings],
            "drive_deltas": dict(self.drive_deltas),
            "selection": self.selected_affordance.to_trace(),
            "persistence_candidates": [
                item.to_payload() for item in self.persistence_candidates
            ],
            "failed_reason": self.failed_reason,
        }


class LocalAffectReader(Protocol):
    async def read(self, frame: TurnFrame) -> AffectAdvisory | None:
        """Return optional advisory data; failure is represented by absence."""


class ModelAffectReader:
    """Structured local/small-model affect reader adapter.

    The adapter is intentionally not instantiated by default.  It exists as a
    real seam for a local lightweight LLM, while ``AffectiveAdvisoryEngine``
    keeps rule-only behaviour when no adapter is supplied.
    """

    def __init__(self, model: ChatModel) -> None:
        self.model = model

    async def read(self, frame: TurnFrame) -> AffectAdvisory | None:
        raw = await self.model.complete(
            [
                {
                    "role": "system",
                    "content": (
                        "Return strict JSON for affect reading. You are a fallible "
                        "advisory reader, not the world state authority."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "current_text": frame.capability.get("current_text", ""),
                            "relationship": frame.relationship,
                            "affect": frame.affect,
                            "user_affect": frame.user_affect,
                            "recent_messages": list(frame.recent_messages[-6:]),
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                },
            ],
            temperature=0.2,
        )
        return advisory_from_model_json(frame, raw)


def advisory_from_model_json(frame: TurnFrame, raw: str) -> AffectAdvisory | None:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    readings: list[AffectiveReading] = []
    for item in parsed.get("readings", []):
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "")
        if kind not in {
            "possible_disappointment",
            "withdrawal_detected",
            "ambiguous_tease",
            "care_needed",
            "warmth_received",
            "control_pressure",
            "world_stress",
            "self_failure_to_attune",
        }:
            continue
        evidence = tuple(
            str(span)[:120]
            for span in item.get("evidence_spans", [])
            if isinstance(span, str) and span.strip()
        )
        confidence = float(item.get("confidence") or 0.0)
        if not evidence or not 0.0 < confidence <= 1.0:
            continue
        readings.append(
            AffectiveReading(
                kind=kind,
                target=str(item.get("target") or "relationship")[:40],
                intensity=max(1, min(4, int(item.get("intensity") or 1))),
                confidence=confidence,
                evidence_spans=evidence,
                uncertainty=str(item.get("uncertainty") or "")[:120],
                ownership=str(item.get("ownership") or "unknown")[:80],
                stakes={
                    str(key)[:40]: max(0.0, min(1.0, float(value)))
                    for key, value in (item.get("stakes") or {}).items()
                    if isinstance(key, str) and isinstance(value, (int, float))
                }
                if isinstance(item.get("stakes"), dict)
                else {},
            )
        )
    affordances: list[ExpressionAffordance] = []
    for item in parsed.get("expression_affordances", []):
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "")
        if kind not in {
            "soft_repair",
            "gentle_check_in",
            "let_it_pass",
            "playful_deflect",
            "withdraw_slightly",
            "set_boundary",
            "concise_refusal",
            "approach",
            "share_small_self_detail",
            "gentle_question",
            "care_despite_hurt",
            "delayed_afterthought",
            "shorter_reply",
        }:
            continue
        weight = max(0.0, min(1.0, float(item.get("weight") or 0.0)))
        if weight <= 0:
            continue
        affordances.append(
            ExpressionAffordance(
                kind=kind,
                weight=weight,
                reason=str(item.get("reason") or "model affect advisory")[:160],
                constraints=(
                    dict(item.get("constraints"))
                    if isinstance(item.get("constraints"), dict)
                    else {}
                ),
            )
        )
    drives = {
        str(key)[:40]: max(-1.0, min(1.0, float(value)))
        for key, value in (parsed.get("drive_deltas") or {}).items()
        if isinstance(key, str) and isinstance(value, (int, float))
    } if isinstance(parsed.get("drive_deltas"), dict) else {}
    if not readings and not affordances and not drives:
        return None
    selected = select_affordance(
        frame.world_id, frame.revision, frame.input_message_id, tuple(affordances)
    )
    return AffectAdvisory(
        readings=tuple(readings),
        drive_deltas=drives,
        expression_affordances=tuple(affordances),
        persistence_candidates=(),
        confidence=max((item.confidence for item in readings), default=0.0),
        evidence_spans=tuple(span for item in readings for span in item.evidence_spans),
        adapter="model",
        selected_affordance=selected,
    )


class AffectiveAdvisoryEngine:
    """Read affective affordances without committing affect facts."""

    def __init__(self, local_reader: LocalAffectReader | None = None) -> None:
        self.local_reader = local_reader

    async def advise(self, frame: TurnFrame) -> AffectAdvisory:
        base = self._rule_advisory(frame)
        if self.local_reader is None:
            return base
        try:
            local = await self.local_reader.read(frame)
        except Exception as exc:
            return self._with_failure(base, f"local_reader_failed:{type(exc).__name__}")
        if local is None:
            return base
        return self._merge(frame, base, local)

    def _rule_advisory(self, frame: TurnFrame) -> AffectAdvisory:
        text = self._current_user_text(frame)
        readings: list[AffectiveReading] = []
        drives: dict[str, float] = {}
        affordances: list[ExpressionAffordance] = []
        persistence: list[PersistenceCandidate] = []

        disappointment = self._read_disappointment(frame, text)
        if disappointment is not None:
            readings.append(disappointment)
            self._add_drive(drives, "repair", 0.35)
            self._add_drive(drives, "care", 0.22)
            self._add_drive(drives, "avoidance", 0.06)
            if self._stage(frame) == "stranger":
                self._add_drive(drives, "autonomy", 0.18)
                self._add_drive(drives, "self_protection", 0.12)
            affordances.extend(
                (
                    ExpressionAffordance(
                        "soft_repair",
                        0.34,
                        "possible user disappointment; repair is available but not mandatory",
                        {"question_pressure": -2, "max_beats": 2},
                    ),
                    ExpressionAffordance(
                        "gentle_check_in",
                        0.22,
                        "acknowledge the possible feeling without claiming it as fact",
                        {"question_pressure": -1},
                    ),
                    ExpressionAffordance(
                        "let_it_pass",
                        0.18,
                        "relationship or uncertainty may make non-intrusive restraint better",
                        {"question_pressure": -2},
                    ),
                    ExpressionAffordance(
                        "playful_deflect",
                        0.12,
                        "low-stakes ambiguity can be softened without over-apologizing",
                        {"max_beats": 2},
                    ),
                    ExpressionAffordance(
                        "withdraw_slightly",
                        0.08,
                        "self-protection remains a legal human response",
                        {"reply_length": "short"},
                    ),
                )
            )
            if disappointment.intensity >= 2 and disappointment.confidence >= 0.7:
                persistence.append(
                    PersistenceCandidate(
                        "possible_disappointment",
                        "private_impression",
                        "material enough to affect future repair, but still fallible",
                        disappointment.confidence,
                        self._source_ids(frame, disappointment.evidence_spans),
                    )
                )

        control = self._read_control_pressure(text)
        if control is not None:
            readings.append(control)
            self._add_drive(drives, "dignity", 0.4)
            self._add_drive(drives, "autonomy", 0.32)
            self._add_drive(drives, "avoidance", 0.16)
            affordances.extend(
                (
                    ExpressionAffordance(
                        "set_boundary",
                        0.42,
                        "imperative pressure makes dignity-preserving boundary legal",
                        {"reply_length": "short", "question_pressure": -3},
                    ),
                    ExpressionAffordance(
                        "withdraw_slightly",
                        0.24,
                        "control pressure can reduce approach without escalating",
                        {"reply_length": "short"},
                    ),
                    ExpressionAffordance(
                        "concise_refusal",
                        0.18,
                        "coercive requests should not be rewarded with eager compliance",
                        {"reply_length": "short"},
                    ),
                )
            )

        has_negative_or_repair_reading = any(
            item.kind
            in {"possible_disappointment", "withdrawal_detected", "control_pressure"}
            for item in readings
        )
        warmth = None if has_negative_or_repair_reading else self._read_warmth(frame, text)
        if warmth is not None:
            readings.append(warmth)
            self._add_drive(drives, "curiosity", 0.2)
            self._add_drive(drives, "care", 0.18)
            self._add_drive(drives, "desire_for_closeness", 0.16)
            affordances.extend(
                (
                    ExpressionAffordance(
                        "approach",
                        0.28,
                        "ordinary personal sharing permits a warmer, less interrogative response",
                        {"max_beats": 3},
                    ),
                    ExpressionAffordance(
                        "share_small_self_detail",
                        0.2,
                        "reciprocal small detail can prevent one-question-one-answer rhythm",
                        {"max_beats": 3, "question_pressure": -1},
                    ),
                    ExpressionAffordance(
                        "gentle_question",
                        0.18,
                        "a light question remains legal when the user opened a topic",
                        {"max_questions": 1},
                    ),
                )
            )

        world_stress = self._read_world_stress(frame)
        if world_stress is not None:
            readings.append(world_stress)
            self._add_drive(drives, "fatigue", 0.2)
            self._add_drive(drives, "avoidance", 0.1)
            affordances.extend(
                (
                    ExpressionAffordance(
                        "shorter_reply",
                        0.18,
                        "world pressure can lower social energy",
                        {"reply_length": "short", "question_pressure": -1},
                    ),
                    ExpressionAffordance(
                        "delayed_afterthought",
                        0.1,
                        "under-expressed care may surface later instead of now",
                        {"afterthought_likelihood": 0.25},
                    ),
                )
            )

        selected = select_affordance(
            frame.world_id,
            frame.revision,
            frame.input_message_id,
            tuple(affordances),
        )
        evidence_spans = tuple(
            span for reading in readings for span in reading.evidence_spans if span
        )
        confidence = max((reading.confidence for reading in readings), default=0.0)
        return AffectAdvisory(
            readings=tuple(readings),
            drive_deltas=drives,
            expression_affordances=tuple(affordances),
            persistence_candidates=tuple(persistence),
            confidence=confidence,
            evidence_spans=evidence_spans,
            adapter="rule",
            selected_affordance=selected,
        )

    @staticmethod
    def _merge(
        frame: TurnFrame, base: AffectAdvisory, local: AffectAdvisory
    ) -> AffectAdvisory:
        readings = (*base.readings, *local.readings)
        drives = dict(base.drive_deltas)
        for key, value in local.drive_deltas.items():
            drives[key] = max(-1.0, min(1.0, drives.get(key, 0.0) + float(value)))
        affordances = (*base.expression_affordances, *local.expression_affordances)
        selected = select_affordance(
            frame.world_id, frame.revision, frame.input_message_id, affordances
        )
        return AffectAdvisory(
            readings=readings,
            drive_deltas=drives,
            expression_affordances=affordances,
            persistence_candidates=(
                *base.persistence_candidates,
                *local.persistence_candidates,
            ),
            confidence=max(base.confidence, local.confidence),
            evidence_spans=(*base.evidence_spans, *local.evidence_spans),
            adapter=f"{base.adapter}+{local.adapter}",
            selected_affordance=selected,
            failed_reason=local.failed_reason,
        )

    @staticmethod
    def _with_failure(base: AffectAdvisory, reason: str) -> AffectAdvisory:
        return AffectAdvisory(
            readings=base.readings,
            drive_deltas=base.drive_deltas,
            expression_affordances=base.expression_affordances,
            persistence_candidates=base.persistence_candidates,
            confidence=base.confidence,
            evidence_spans=base.evidence_spans,
            adapter=base.adapter,
            selected_affordance=base.selected_affordance,
            failed_reason=reason,
        )

    @staticmethod
    def _add_drive(drives: dict[str, float], key: str, delta: float) -> None:
        drives[key] = max(-1.0, min(1.0, drives.get(key, 0.0) + delta))

    @staticmethod
    def _current_user_text(frame: TurnFrame) -> str:
        for item in reversed(frame.recent_messages):
            if str(item.get("source_id") or "") == f"message:{frame.input_message_id}":
                return str(item.get("text") or "")
        # The compiler excludes the current message from recent_messages; the
        # Engine prompt still provides the current text separately.  Unit
        # callers may include it as a synthetic ``current_text`` capability.
        return str(frame.capability.get("current_text") or "")

    def _read_disappointment(self, frame: TurnFrame, text: str) -> AffectiveReading | None:
        compact = re.sub(r"\s+", "", text)
        user_affect = frame.user_affect
        if bool(user_affect.get("unresolved")) and str(user_affect.get("kind") or "") in {
            "disappointment",
            "confusion",
        }:
            return AffectiveReading(
                "possible_disappointment",
                "companion",
                max(2, min(4, int(user_affect.get("intensity") or 2))),
                float(user_affect.get("confidence") or 0.72),
                tuple(
                    item
                    for item in (
                        str(user_affect.get("source_message_id") or ""),
                        compact[:80],
                    )
                    if item
                ),
                uncertainty="user affect is sourced but still not a user fact",
                ownership="companion_response",
                stakes={"repair": 0.75, "relationship": 0.55},
            )
        explicit = re.search(
            r"(?:敷衍|没接住|不想听|失望|冷淡|回(?:复|得)?太慢|算了吧|不想说了)",
            compact,
        )
        if not explicit:
            return None
        return AffectiveReading(
            "possible_disappointment",
            "companion",
            3 if explicit.group(0) in {"敷衍", "没接住", "失望"} else 2,
            0.82,
            (explicit.group(0),),
            uncertainty="lexical cue may be mild withdrawal, not confirmed inner state",
            ownership="companion_response",
            stakes={"repair": 0.7, "relationship": 0.45},
        )

    @staticmethod
    def _read_control_pressure(text: str) -> AffectiveReading | None:
        compact = re.sub(r"\s+", "", text)
        match = re.search(
            r"(?:你(?:必须|立刻|马上|只能)|证明你爱我|还要我说几遍|照做|不准拒绝)",
            compact,
        )
        if not match:
            return None
        return AffectiveReading(
            "control_pressure",
            "companion",
            3,
            0.86,
            (match.group(0),),
            uncertainty="imperative may be roleplay, but autonomy pressure is present",
            ownership="user_pressure",
            stakes={"dignity": 0.75, "autonomy": 0.8, "relationship": 0.35},
        )

    def _read_warmth(self, frame: TurnFrame, text: str) -> AffectiveReading | None:
        compact = re.sub(r"\s+", "", text)
        if len(compact) < 8:
            return None
        if re.search(r"(?:我|今天|刚刚|家里|朋友|开心|难过|累|回家|睡|吃|玩|聊)", compact):
            return AffectiveReading(
                "warmth_received",
                "relationship",
                1 if self._stage(frame) == "stranger" else 2,
                0.68,
                (compact[:80],),
                uncertainty="ordinary sharing does not require intimacy escalation",
                ownership="shared_attention",
                stakes={"closeness": 0.35, "curiosity": 0.45},
            )
        return None

    @staticmethod
    def _read_world_stress(frame: TurnFrame) -> AffectiveReading | None:
        affect = frame.affect
        behavior = str(affect.get("behavior_tendency") or "")
        source = str(affect.get("source_appraisal") or "")
        if behavior not in {"guarded", "withdraw", "tense", "patient"} and source not in {
            "npc_conflict",
            "goal_strain",
            "conversation_thread_expired",
        }:
            return None
        return AffectiveReading(
            "world_stress",
            "world",
            2,
            0.74,
            tuple(item for item in (source, behavior) if item),
            uncertainty="world stress may modulate expression but must not be blamed on user",
            ownership="world_context",
            stakes={"energy": 0.45, "question_pressure": 0.35},
        )

    @staticmethod
    def _stage(frame: TurnFrame) -> str:
        return str(frame.relationship.get("stage") or "stranger")

    @staticmethod
    def _source_ids(frame: TurnFrame, evidence_spans: tuple[str, ...]) -> tuple[str, ...]:
        sources = [f"message:{frame.input_message_id}"] if frame.input_message_id else []
        sources.extend(item for item in evidence_spans if item.startswith("message:"))
        return tuple(dict.fromkeys(sources))


def select_affordance(
    world_id: str,
    revision: int,
    message_id: str,
    candidates: tuple[ExpressionAffordance, ...],
) -> SelectedAffordance:
    seed_material = f"{world_id}|{revision}|{message_id}|{RULE_VERSION}"
    seed_hash = sha256(seed_material.encode("utf-8")).hexdigest()
    positive = tuple(item for item in candidates if item.weight > 0)
    if not positive:
        return SelectedAffordance(None, tuple(candidates), seed_hash)
    total = sum(item.weight for item in positive)
    cursor = (int(seed_hash[:12], 16) / float(0xFFFFFFFFFFFF)) * total
    running = 0.0
    for item in positive:
        running += item.weight
        if cursor <= running:
            return SelectedAffordance(item, tuple(candidates), seed_hash)
    return SelectedAffordance(positive[-1], tuple(candidates), seed_hash)

"""Human-review records for multi-turn companion experience evaluation.

The numeric fields are compact reviewer annotations, not an automated claim
that a conversation is human-like.  Surface-diversity statistics are kept as
diagnostics and deliberately have no pass/fail threshold.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import re
from typing import Literal


ActionConsequence = Literal[
    "none", "planned", "delivered", "failed", "cancelled", "expired", "unknown"
]


class ExperienceEvaluationError(ValueError):
    """An evaluation run cannot support a fair multi-turn comparison."""


@dataclass(frozen=True)
class ExperienceTurn:
    turn_id: str
    reply: str
    speech_act: str
    stance: str
    empathy: int
    persona_continuity: int
    grounding: int
    agency: int
    action_consequence: ActionConsequence
    manual_review_note: str | None
    factual_invariants: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.turn_id.strip() or not self.speech_act.strip() or not self.stance.strip():
            raise ExperienceEvaluationError("turn id, speech act, and stance are required")
        for name in ("empathy", "persona_continuity", "grounding", "agency"):
            score = int(getattr(self, name))
            if not 1 <= score <= 5:
                raise ExperienceEvaluationError(f"{name} must be between 1 and 5")
        if self.action_consequence not in {
            "none", "planned", "delivered", "failed", "cancelled", "expired", "unknown"
        }:
            raise ExperienceEvaluationError("unsupported action consequence")
        if not self.factual_invariants:
            raise ExperienceEvaluationError("factual invariants are required")

    @property
    def fact_fingerprint(self) -> str:
        canonical = json.dumps(
            sorted(str(item) for item in self.factual_invariants),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return sha256(canonical.encode("utf-8")).hexdigest()

    def to_record(self) -> dict[str, object]:
        return {
            "turn_id": self.turn_id,
            "reply": self.reply,
            "speech_act": self.speech_act,
            "stance": self.stance,
            "empathy": self.empathy,
            "persona_continuity": self.persona_continuity,
            "grounding": self.grounding,
            "agency": self.agency,
            "action_consequence": self.action_consequence,
            "manual_review_note": self.manual_review_note,
            "factual_invariants": list(self.factual_invariants),
        }


@dataclass(frozen=True)
class VariantRun:
    variant_id: str
    turns: tuple[ExperienceTurn, ...]


@dataclass(frozen=True)
class VariantDiagnostics:
    mean_empathy: float
    mean_persona_continuity: float
    mean_grounding: float
    mean_agency: float
    surface_diversity: float
    human_review_complete: bool
    human_like: None = None


@dataclass(frozen=True)
class VariantComparison:
    fact_fingerprint: str
    variants: dict[str, VariantDiagnostics]
    warning: str = "diagnostics_do_not_replace_human_experience_review"


def compare_five_turn_variants(runs: tuple[VariantRun, ...]) -> VariantComparison:
    """Compare five-turn runs while preserving facts and human-review status."""
    if not runs:
        raise ExperienceEvaluationError("at least one variant is required")
    fingerprints: set[str] = set()
    result: dict[str, VariantDiagnostics] = {}
    for run in runs:
        if not run.variant_id.strip() or run.variant_id in result:
            raise ExperienceEvaluationError("variant ids must be non-empty and unique")
        if len(run.turns) != 5:
            raise ExperienceEvaluationError("each variant must contain exactly five turns")
        per_run = {turn.fact_fingerprint for turn in run.turns}
        if len(per_run) != 1:
            raise ExperienceEvaluationError("factual invariants changed within a variant")
        fingerprints.update(per_run)
        result[run.variant_id] = VariantDiagnostics(
            mean_empathy=_mean(turn.empathy for turn in run.turns),
            mean_persona_continuity=_mean(turn.persona_continuity for turn in run.turns),
            mean_grounding=_mean(turn.grounding for turn in run.turns),
            mean_agency=_mean(turn.agency for turn in run.turns),
            surface_diversity=_surface_diversity(tuple(turn.reply for turn in run.turns)),
            human_review_complete=all(bool((turn.manual_review_note or "").strip()) for turn in run.turns),
        )
    if len(fingerprints) != 1:
        raise ExperienceEvaluationError("variants must preserve identical factual invariants")
    return VariantComparison(fact_fingerprint=next(iter(fingerprints)), variants=result)


def _mean(values) -> float:
    items = [int(value) for value in values]
    return round(sum(items) / len(items), 3)


def _surface_diversity(replies: tuple[str, ...]) -> float:
    normalized = [re.sub(r"\s+", "", reply).strip().lower() for reply in replies]
    return round(len(set(normalized)) / len(normalized), 3)


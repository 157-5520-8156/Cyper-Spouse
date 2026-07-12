"""Human-review records for multi-turn companion experience evaluation.

The numeric fields are compact reviewer annotations, not an automated claim
that a conversation is human-like.  Surface-diversity statistics are kept as
diagnostics and deliberately have no pass/fail threshold.
"""

from __future__ import annotations

from dataclasses import dataclass
import fcntl
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Literal, Mapping


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

    def to_record(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "variant_id": self.variant_id,
            "turns": [turn.to_record() for turn in self.turns],
        }


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


def append_variant_run_jsonl(path: str | Path, run: VariantRun) -> None:
    """Validate and append one five-turn annotated variant to an audit ledger."""
    compare_five_turn_variants((run,))
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            handle.seek(0)
            existing = _load_variant_lines(handle)
            if any(item.variant_id == run.variant_id for item in existing):
                raise ExperienceEvaluationError(
                    f"variant id {run.variant_id!r} already exists in the ledger"
                )
            if existing:
                compare_five_turn_variants((*existing, run))
            handle.seek(0, 2)
            handle.write(
                json.dumps(run.to_record(), ensure_ascii=False, sort_keys=True) + "\n"
            )
            handle.flush()
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def load_variant_runs_jsonl(path: str | Path) -> tuple[VariantRun, ...]:
    """Load annotated variants from a JSONL ledger and validate every record."""
    source = Path(path)
    with source.open("r", encoding="utf-8") as handle:
        return _load_variant_lines(handle)


def _load_variant_lines(handle) -> tuple[VariantRun, ...]:
    runs: list[VariantRun] = []
    for line_number, raw_line in enumerate(handle, start=1):
        if not raw_line.strip():
            continue
        try:
            record = json.loads(raw_line)
            run = variant_run_from_record(record)
            compare_five_turn_variants((run,))
        except (json.JSONDecodeError, TypeError, KeyError, ExperienceEvaluationError) as exc:
            raise ExperienceEvaluationError(
                f"invalid evaluation ledger record at line {line_number}: {exc}"
            ) from exc
        runs.append(run)
    compare_five_turn_variants(tuple(runs)) if runs else None
    return tuple(runs)


def variant_run_from_record(record: object) -> VariantRun:
    if not isinstance(record, Mapping):
        raise ExperienceEvaluationError("variant record must be an object")
    if record.get("schema_version") != 1:
        raise ExperienceEvaluationError("unsupported evaluation schema version")
    variant_id = record.get("variant_id")
    if not isinstance(variant_id, str) or not variant_id.strip():
        raise ExperienceEvaluationError("variant id must be a non-empty string")
    raw_turns = record.get("turns")
    if not isinstance(raw_turns, list):
        raise ExperienceEvaluationError("turns must be a list")
    turns: list[ExperienceTurn] = []
    for raw_turn in raw_turns:
        if not isinstance(raw_turn, Mapping):
            raise ExperienceEvaluationError("each turn must be an object")
        values = dict(raw_turn)
        for field in ("turn_id", "reply", "speech_act", "stance"):
            if not isinstance(values.get(field), str):
                raise ExperienceEvaluationError(f"{field} must be a string")
        for field in ("empathy", "persona_continuity", "grounding", "agency"):
            if not isinstance(values.get(field), int) or isinstance(values.get(field), bool):
                raise ExperienceEvaluationError(f"{field} must be an integer")
        note = values.get("manual_review_note")
        if note is not None and not isinstance(note, str):
            raise ExperienceEvaluationError("manual_review_note must be a string or null")
        invariants = values.get("factual_invariants")
        if not isinstance(invariants, list) or any(
            not isinstance(item, str) for item in invariants
        ):
            raise ExperienceEvaluationError("factual_invariants must be a list of strings")
        values["factual_invariants"] = tuple(invariants)
        try:
            turns.append(ExperienceTurn(**values))
        except (AttributeError, TypeError, ValueError) as exc:
            raise ExperienceEvaluationError(f"invalid experience turn: {exc}") from exc
    return VariantRun(variant_id=variant_id, turns=tuple(turns))


def _mean(values) -> float:
    items = [int(value) for value in values]
    return round(sum(items) / len(items), 3)


def _surface_diversity(replies: tuple[str, ...]) -> float:
    normalized = [re.sub(r"\s+", "", reply).strip().lower() for reply in replies]
    return round(len(set(normalized)) / len(normalized), 3)

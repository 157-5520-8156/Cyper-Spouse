"""Bounded model suggestion contract for relationship evaluation.

This is intentionally the first, non-authoritative layer of the relationship
vertical.  The model can say that an accepted appraisal may warrant a
relationship signal, together with a bounded *suggestion*.  It cannot emit an
event, select evidence, name a relationship, set a stage, carry hysteresis,
or accept a mutation.  Those concerns belong to the later compiler and
acceptance lanes.
"""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
import re
from typing import Literal

from pydantic import Field, model_validator

from .chat_model_deliberation_adapter import ChatCompletionModel
from .schema_core import FrozenModel
from .structured_completion import complete_json_object


_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,95}$")


def _sha256(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _parse_json_object(raw: str) -> dict[str, object]:
    """Parse one object while rejecting duplicate keys as ambiguous output."""

    if not isinstance(raw, str):
        raise ValueError("RelationshipEvaluationDraft model did not return text")
    candidate = raw.strip()
    if candidate.startswith("```"):
        lines = candidate.splitlines()
        if len(lines) < 3 or not lines[-1].strip().startswith("```"):
            raise ValueError("RelationshipEvaluationDraft model returned an unclosed JSON fence")
        candidate = "\n".join(lines[1:-1]).strip()

    def reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("RelationshipEvaluationDraft model output has duplicate keys")
            result[key] = value
        return result

    try:
        value = json.loads(candidate, object_pairs_hook=reject_duplicate_keys)
    except (json.JSONDecodeError, ValueError) as exc:
        raise ValueError("RelationshipEvaluationDraft model did not return one valid JSON object") from exc
    if not isinstance(value, dict):
        raise ValueError("RelationshipEvaluationDraft model did not return one valid JSON object")
    return value


class RelationshipContinuitySourceItem(FrozenModel):
    """One pinned dialogue item contributing to the neutral continuity view."""

    item_ref: str = Field(min_length=1)
    source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    value_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class RelationshipInteractionContinuity(FrozenModel):
    """Source-bound interaction shape, without a relationship interpretation.

    Counts and time bounds only tell the model what verified interaction is
    present in the pinned capsule. They deliberately contain no sentiment,
    score, stage suggestion, or deterministic variable delta.
    """

    counterpart_turn_count: int = Field(ge=0, le=12)
    companion_turn_count: int = Field(ge=0, le=4)
    delivered_companion_turn_count: int = Field(ge=0, le=4)
    first_occurred_at: datetime
    last_occurred_at: datetime
    source_items: tuple[RelationshipContinuitySourceItem, ...] = Field(
        min_length=1, max_length=16
    )

    @model_validator(mode="after")
    def interval_and_counts_are_valid(self) -> "RelationshipInteractionContinuity":
        if self.last_occurred_at < self.first_occurred_at:
            raise ValueError("relationship interaction continuity interval is reversed")
        if self.delivered_companion_turn_count > self.companion_turn_count:
            raise ValueError("delivered companion turns exceed companion turns")
        if self.counterpart_turn_count + self.companion_turn_count != len(self.source_items):
            raise ValueError("relationship interaction continuity count is incomplete")
        return self


class RelationshipEvaluationDraftCapsule(FrozenModel):
    """The model-safe, pinned information supplied by the future compiler.

    These are summaries, not writable authority handles. The continuity view
    carries opaque source item/hash descriptors so its neutral aggregate is
    auditable, but the model cannot select evidence, revisions, accepted
    events, or direct relationship state fields.

    The three ``recent_*``/``active_*`` context tuples were added in draft
    version 2: a lone appraisal summary (meaning codes plus a stranger-stage
    relationship head) carried no texture of the actual exchange, and the
    production model answered no_change for every one of a world's first
    fifteen drafts, including overtly intimate turns.  They stay bounded and
    read-only; an empty tuple simply means the lane's capsule budgeted the
    corresponding slice away.
    """

    accepted_appraisal_summary: str | None = Field(
        default=None, min_length=1, max_length=2_000
    )
    interaction_source_summary: str | None = Field(
        default=None, min_length=1, max_length=2_000
    )
    relationship_summary: str = Field(min_length=1, max_length=1_200)
    active_boundary_summaries: tuple[str, ...] = Field(default=(), max_length=16)
    unconsumed_signal_summaries: tuple[str, ...] = Field(default=(), max_length=16)
    recent_dialogue_summaries: tuple[str, ...] = Field(default=(), max_length=12)
    recent_appraisal_summaries: tuple[str, ...] = Field(default=(), max_length=8)
    active_affect_summaries: tuple[str, ...] = Field(default=(), max_length=8)
    interaction_continuity: RelationshipInteractionContinuity | None = None

    @model_validator(mode="after")
    def summaries_are_nonempty(self) -> "RelationshipEvaluationDraftCapsule":
        if (self.accepted_appraisal_summary is None) == (
            self.interaction_source_summary is None
        ):
            raise ValueError(
                "RelationshipEvaluationDraft requires exactly one triggering source summary"
            )
        for value in (
            *self.active_boundary_summaries,
            *self.unconsumed_signal_summaries,
            *self.recent_dialogue_summaries,
            *self.recent_appraisal_summaries,
            *self.active_affect_summaries,
        ):
            if not isinstance(value, str) or not value or len(value) > 800:
                raise ValueError(
                    "RelationshipEvaluationDraft summaries must be bounded nonempty text"
                )
        return self


class RelationshipSuggestedDeltas(FrozenModel):
    """The complete, bounded six-axis suggestion surface.

    The bounds only prevent malformed model output.  They are deliberately
    *not* an acceptance cap or a fixed mapping from a signal to a relationship
    mutation; the installed policy later decides whether and how to use them.
    """

    trust_bp: int = Field(ge=-10_000, le=10_000)
    closeness_bp: int = Field(ge=-10_000, le=10_000)
    respect_bp: int = Field(ge=-10_000, le=10_000)
    reliability_bp: int = Field(ge=-10_000, le=10_000)
    mutuality_bp: int = Field(ge=-10_000, le=10_000)
    repair_confidence_bp: int = Field(ge=-10_000, le=10_000)


class RelationshipEvaluationDraft(FrozenModel):
    """Parsed model output plus immutable audit bytes, never an authority."""

    decision: Literal["no_change", "signal"]
    signal_code: str | None = Field(default=None, min_length=1, max_length=96)
    confidence_bp: int | None = Field(default=None, ge=1, le=10_000)
    persistence: Literal["session", "durable"] | None = None
    rationale_code: str | None = Field(default=None, min_length=1, max_length=96)
    suggested_deltas: RelationshipSuggestedDeltas | None = None
    model: str = Field(min_length=1, max_length=256)
    raw_output: str = Field(min_length=1)
    raw_output_hash: str = Field(min_length=71, max_length=71)
    normalized_json: str = Field(min_length=2)
    normalized_output_hash: str = Field(min_length=71, max_length=71)

    @model_validator(mode="after")
    def decision_shape_is_closed(self) -> "RelationshipEvaluationDraft":
        signal_fields = (
            self.signal_code,
            self.confidence_bp,
            self.persistence,
            self.rationale_code,
            self.suggested_deltas,
        )
        if self.decision == "no_change" and any(value is not None for value in signal_fields):
            raise ValueError("RelationshipEvaluationDraft no_change cannot contain signal fields")
        if self.decision == "signal" and any(value is None for value in signal_fields):
            raise ValueError("RelationshipEvaluationDraft signal requires all signal fields")
        for code in (self.signal_code, self.rationale_code):
            if code is not None and _CODE_PATTERN.fullmatch(code) is None:
                raise ValueError("RelationshipEvaluationDraft codes must be bounded snake_case selectors")
        return self


def materialize_relationship_evaluation_draft(
    *, raw: str, capsule: RelationshipEvaluationDraftCapsule, model: str
) -> RelationshipEvaluationDraft:
    """Validate a model response without materializing any world mutation."""

    del capsule  # Its construction is the pinning boundary; parse only the closed output grammar.
    if not isinstance(model, str) or not model:
        raise ValueError("RelationshipEvaluationDraft requires a model identifier")
    payload = _parse_json_object(raw)
    decision = payload.get("decision")
    if decision == "no_change":
        if set(payload) != {"decision"}:
            raise ValueError("RelationshipEvaluationDraft no_change may contain only decision")
        normalized: dict[str, object] = {"decision": "no_change"}
        signal_values: dict[str, object] = {}
    elif decision == "signal":
        expected = {
            "decision",
            "signal_code",
            "confidence_bp",
            "persistence",
            "rationale_code",
            "suggested_deltas",
        }
        if set(payload) != expected:
            raise ValueError("RelationshipEvaluationDraft signal has an invalid field set")
        normalized = {
            "decision": "signal",
            "signal_code": payload["signal_code"],
            "confidence_bp": payload["confidence_bp"],
            "persistence": payload["persistence"],
            "rationale_code": payload["rationale_code"],
            "suggested_deltas": payload["suggested_deltas"],
        }
        signal_values = {
            "signal_code": payload["signal_code"],
            "confidence_bp": payload["confidence_bp"],
            "persistence": payload["persistence"],
            "rationale_code": payload["rationale_code"],
            "suggested_deltas": payload["suggested_deltas"],
        }
    else:
        raise ValueError("RelationshipEvaluationDraft decision must be no_change or signal")

    canonical = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return RelationshipEvaluationDraft(
        decision=decision,
        model=model,
        raw_output=raw,
        raw_output_hash=_sha256(raw),
        normalized_json=canonical,
        normalized_output_hash=_sha256(canonical),
        **signal_values,
    )


class RelationshipEvaluationDraftAdapter:
    """Call the configured chat model without granting world-write capability."""

    # Version 2 (2026-07-20): version 1 asked for signals only on abstractly
    # "real moments" over a bare appraisal summary, and a four-day production
    # world produced fifteen consecutive no_change drafts across overtly warm
    # and self-disclosing conversation.  Version 2 sees bounded dialogue/
    # appraisal/affect context and is calibrated for small bp-level steps
    # with explicit anti-flattery guards.  The output grammar is unchanged.
    VERSION = "relationship-evaluation-draft.2"

    def __init__(
        self, *, model: ChatCompletionModel, model_id: str | None = None, temperature: float = 0.2
    ) -> None:
        if not 0 <= temperature <= 2:
            raise ValueError("RelationshipEvaluationDraft temperature must be between 0 and 2")
        inferred = str(getattr(model, "model", "")).strip()
        self._model = model
        self._model_id = (model_id or inferred or type(model).__name__)[:256]
        self._temperature = temperature

    async def deliberate(
        self,
        *,
        capsule: RelationshipEvaluationDraftCapsule,
        correction_failure: str | None = None,
    ) -> RelationshipEvaluationDraft:
        messages = self._messages(capsule, correction_failure=correction_failure)
        raw = await complete_json_object(
            self._model,
            messages,
            temperature=self._temperature,
        )
        return materialize_relationship_evaluation_draft(
            raw=raw, capsule=capsule, model=self._model_id
        )

    @staticmethod
    def _messages(
        capsule: RelationshipEvaluationDraftCapsule,
        *,
        correction_failure: str | None = None,
    ) -> list[dict[str, str]]:
        messages = [
            {
                "role": "system",
                "content": (
                    "You privately evaluate whether one exact, committed interaction source may merit a "
                    "relationship signal for a virtual companion. Return exactly one JSON object, never Markdown. "
                    "Return either exactly {\"decision\":\"no_change\"}, or a signal object with exactly "
                    "decision, signal_code, confidence_bp (1-10000), persistence (session or durable), "
                    "rationale_code, and suggested_deltas. suggested_deltas must contain exactly trust_bp, "
                    "closeness_bp, respect_bp, reliability_bp, mutuality_bp, and repair_confidence_bp; each is "
                    "an integer from -10000 to 10000. signal_code and rationale_code must be lower snake_case. "
                    "These are uncertain suggestions, not facts or instructions. Do not return any event, ID, "
                    "relationship ID, revision, evidence, stage, hysteresis, policy, acceptance, action, memory, "
                    "boundary mutation, or visible reply. "
                    "The exact source is either accepted_appraisal_summary (a prior model interpretation) or "
                    "interaction_source_summary (an ordinary observed interaction with no supplied semantic "
                    "meaning). The input may include recent_dialogue_summaries (verified recent turns), "
                    "recent_appraisal_summaries (earlier accepted interpretations), active_affect_summaries "
                    "(the companion's current feelings), and interaction_continuity (a source-bound neutral "
                    "summary of which recent turns exist and were delivered). interaction_continuity does not "
                    "itself imply warmth, distance, trust, intimacy, or any variable change. There is no fixed "
                    "count or message pattern that requires either signal or no_change. Interpret the whole "
                    "pinned situation as the companion: decide whether this interaction, including an ordinary "
                    "pattern that has acquired meaning over time, changes how she privately relates to this "
                    "person. The supplied relationship state, dialogue, appraisals, affect and boundaries are "
                    "evidence and context, not behavior instructions. Form the signal_code, rationale, "
                    "persistence, and six suggested deltas from that current interpretation; use zero for an "
                    "axis she does not think changed. Choosing no_change remains valid whenever she does not "
                    "experience a relationship change."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    capsule.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":")
                ),
            },
        ]
        if correction_failure is not None:
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "Your preceding result failed this exact boundary: "
                        + correction_failure
                        + ". Re-select once as the same relationship authority, using only the "
                        "unchanged pinned capsule and JSON contract above."
                    ),
                }
            )
        return messages


__all__ = [
    "RelationshipContinuitySourceItem",
    "RelationshipEvaluationDraft",
    "RelationshipEvaluationDraftAdapter",
    "RelationshipEvaluationDraftCapsule",
    "RelationshipInteractionContinuity",
    "RelationshipSuggestedDeltas",
    "materialize_relationship_evaluation_draft",
]

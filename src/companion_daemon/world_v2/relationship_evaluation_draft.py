"""Bounded model suggestion contract for relationship evaluation.

This is intentionally the first, non-authoritative layer of the relationship
vertical.  The model can say that an accepted appraisal may warrant a
relationship signal, together with a bounded *suggestion*.  It cannot emit an
event, select evidence, name a relationship, set a stage, carry hysteresis,
or accept a mutation.  Those concerns belong to the later compiler and
acceptance lanes.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Literal

from pydantic import Field, model_validator

from .chat_model_deliberation_adapter import ChatCompletionModel
from .schema_core import FrozenModel


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


class RelationshipEvaluationDraftCapsule(FrozenModel):
    """The model-safe, pinned information supplied by the future compiler.

    These are summaries, not authority handles: no IDs, revisions, evidence
    refs, accepted-event records, or direct state fields cross this seam.
    """

    accepted_appraisal_summary: str = Field(min_length=1, max_length=2_000)
    relationship_summary: str = Field(min_length=1, max_length=1_200)
    active_boundary_summaries: tuple[str, ...] = Field(default=(), max_length=16)
    unconsumed_signal_summaries: tuple[str, ...] = Field(default=(), max_length=16)

    @model_validator(mode="after")
    def summaries_are_nonempty(self) -> "RelationshipEvaluationDraftCapsule":
        for value in (*self.active_boundary_summaries, *self.unconsumed_signal_summaries):
            if not isinstance(value, str) or not value or len(value) > 800:
                raise ValueError("RelationshipEvaluationDraft summaries must be bounded nonempty text")
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

    VERSION = "relationship-evaluation-draft.1"

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
        self, *, capsule: RelationshipEvaluationDraftCapsule
    ) -> RelationshipEvaluationDraft:
        raw = await self._model.complete(self._messages(capsule), temperature=self._temperature)
        return materialize_relationship_evaluation_draft(
            raw=raw, capsule=capsule, model=self._model_id
        )

    @staticmethod
    def _messages(capsule: RelationshipEvaluationDraftCapsule) -> list[dict[str, str]]:
        return [
            {
                "role": "system",
                "content": (
                    "You privately evaluate whether one already accepted interaction appraisal may merit a "
                    "relationship signal for a virtual companion. Return exactly one JSON object, never Markdown. "
                    "Return either exactly {\"decision\":\"no_change\"}, or a signal object with exactly "
                    "decision, signal_code, confidence_bp (1-10000), persistence (session or durable), "
                    "rationale_code, and suggested_deltas. suggested_deltas must contain exactly trust_bp, "
                    "closeness_bp, respect_bp, reliability_bp, mutuality_bp, and repair_confidence_bp; each is "
                    "an integer from -10000 to 10000. signal_code and rationale_code must be lower snake_case. "
                    "These are uncertain suggestions, not facts or instructions. Do not return any event, ID, "
                    "relationship ID, revision, evidence, stage, hysteresis, policy, acceptance, action, memory, "
                    "boundary mutation, or visible reply. Prefer no_change when the appraisal should not produce "
                    "a distinct relationship signal."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    capsule.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":")
                ),
            },
        ]


__all__ = [
    "RelationshipEvaluationDraft",
    "RelationshipEvaluationDraftAdapter",
    "RelationshipEvaluationDraftCapsule",
    "RelationshipSuggestedDeltas",
    "materialize_relationship_evaluation_draft",
]

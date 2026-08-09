"""Typed materialization for source-bound retrieval-memory decisions.

The sole CharacterInterior role authors retention choices.  This module is a
pure wire validator plus the durable draft type consumed by reducers and
memory authorities; it intentionally exposes no independently callable model
adapter or protagonist faculty port.
"""

from __future__ import annotations

import json
import math

from .model_json import extract_json_object_text
from .schema_core import FrozenModel
from .schemas import (
    MEMORY_SALIENCE_MATRIX_DIGEST,
    MemoryCueKind,
    MemoryRetentionRationale,
    MemorySalienceVector,
)


class FactMemoryRetentionDraft(FrozenModel):
    cue_kind: MemoryCueKind
    retention_rationales: tuple[MemoryRetentionRationale, ...]
    salience: MemorySalienceVector


class FactMemoryDraftTechnicalFailure(RuntimeError):
    """The bounded retention route failed to produce a valid decision."""

    def __init__(self, failure_code: str) -> None:
        super().__init__(failure_code)
        self.failure_code = failure_code


def _parse(raw: object) -> dict[str, object]:
    if not isinstance(raw, str):
        raise ValueError("Fact-memory model result was not JSON text")
    try:
        value = json.loads(extract_json_object_text(raw))
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("Fact-memory model did not return one JSON object") from exc
    if not isinstance(value, dict):
        raise ValueError("Fact-memory model did not return one JSON object")
    return value


def materialize_fact_memory_draft(raw: object) -> FactMemoryRetentionDraft | None:
    """Validate the narrow, non-authoritative part of a retention decision."""

    value = _parse(raw)
    retain = value.get("retain")
    if not isinstance(retain, bool):
        raise ValueError("Fact-memory retain must be boolean")
    if not retain:
        if set(value) != {"retain"}:
            raise ValueError("Fact-memory no-change may contain only retain")
        return None
    if set(value) != {"retain", "cue_kind", "retention_rationales", "salience"}:
        raise ValueError("Fact-memory retained draft has unsupported fields")
    cue_kind = value["cue_kind"]
    rationales = value["retention_rationales"]
    salience = value["salience"]
    if (
        not isinstance(cue_kind, str)
        or not isinstance(rationales, list)
        or not isinstance(salience, dict)
    ):
        raise ValueError("Fact-memory retained draft has invalid field types")
    try:
        # The role model often expresses salience as probabilities in [0, 1]
        # instead of basis points; normalize that natural scale exactly like
        # the chat-side appraisal wire so a draft never fails solely on this
        # representation.
        normalized: dict[str, object] = {}
        for field, value in salience.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                normalized[field] = value
            elif isinstance(value, float):
                if not math.isfinite(value):
                    normalized[field] = value
                elif 0.0 <= value <= 1.0:
                    normalized[field] = int(round(value * 10_000))
                else:
                    normalized[field] = int(round(value))
            else:
                normalized[field] = value
        result = FactMemoryRetentionDraft(
            cue_kind=cue_kind,
            retention_rationales=tuple(rationales),
            salience=MemorySalienceVector(
                **normalized,
                matrix_digest=MEMORY_SALIENCE_MATRIX_DIGEST,
            ),
        )
    except Exception as exc:
        raise ValueError("Fact-memory retained draft violates the installed matrix") from exc
    if not result.retention_rationales or len(set(result.retention_rationales)) != len(
        result.retention_rationales
    ):
        raise ValueError("Fact-memory retention rationales must be nonempty and unique")
    return result


__all__ = [
    "FactMemoryDraftTechnicalFailure",
    "FactMemoryRetentionDraft",
    "materialize_fact_memory_draft",
]

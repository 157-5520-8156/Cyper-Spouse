"""Bounded model classification for Fact-backed retrieval memory.

The model never names a candidate, source event, privacy ceiling, or summary
payload.  It only classifies whether an already accepted Fact deserves a
source-bound retrieval candidate and supplies the salience matrix that the
memory policy already understands.
"""

from __future__ import annotations

import json
from typing import Protocol

from .schema_core import FrozenModel
from .schemas import (
    MEMORY_SALIENCE_MATRIX_DIGEST,
    MemoryCueKind,
    MemoryRetentionRationale,
    MemorySalienceVector,
)


class FactMemoryDraftChatModel(Protocol):
    model: str

    async def complete(self, messages: list[dict[str, str]], *, temperature: float = 0.2) -> str: ...


class FactMemoryRetentionDraft(FrozenModel):
    cue_kind: MemoryCueKind
    retention_rationales: tuple[MemoryRetentionRationale, ...]
    salience: MemorySalienceVector


def _parse(raw: str) -> dict[str, object]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("Fact-memory model did not return one JSON object") from exc
    if not isinstance(value, dict):
        raise ValueError("Fact-memory model did not return one JSON object")
    return value


class FactMemoryDraftAdapter:
    """Ask a model only for bounded retention classification of a Fact source."""

    VERSION = "fact-memory-draft.1"

    def __init__(self, *, model: FactMemoryDraftChatModel, temperature: float = 0.15) -> None:
        if not 0 <= temperature <= 2:
            raise ValueError("Fact-memory temperature must be between 0 and 2")
        self._model = model
        self._temperature = temperature

    async def classify(
        self, *, predicate_code: str, source_text: str
    ) -> FactMemoryRetentionDraft | None:
        raw = await self._model.complete(
            self._messages(predicate_code=predicate_code, source_text=source_text),
            temperature=self._temperature,
        )
        return materialize_fact_memory_draft(raw)

    @staticmethod
    def _messages(*, predicate_code: str, source_text: str) -> list[dict[str, str]]:
        return [
            {
                "role": "system",
                "content": (
                    "Decide whether one already verified user Fact should become a retrieval memory. "
                    "Return exactly one JSON object. Retain=false for low future usefulness, one-off "
                    "facts, or facts that need no conversational continuity. If retain=true return "
                    "cue_kind, retention_rationales, and salience. salience must contain exactly "
                    "autobiographical_relevance_bp, relationship_relevance_bp, emotional_residue_bp, "
                    "unfinished_business_bp, recurrence_bp, novelty_bp, future_utility_bp, and "
                    "world_continuity_bp as integers 0..10000. Do not return summaries, ids, hashes, "
                    "privacy, source refs, actions, or behaviour instructions."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {"predicate_code": predicate_code, "verified_source_text": source_text},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            },
        ]


def materialize_fact_memory_draft(raw: str) -> FactMemoryRetentionDraft | None:
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
    if not isinstance(cue_kind, str) or not isinstance(rationales, list) or not isinstance(salience, dict):
        raise ValueError("Fact-memory retained draft has invalid field types")
    try:
        result = FactMemoryRetentionDraft(
            cue_kind=cue_kind,
            retention_rationales=tuple(rationales),
            salience=MemorySalienceVector(
                **salience,
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
    "FactMemoryDraftAdapter",
    "FactMemoryDraftChatModel",
    "FactMemoryRetentionDraft",
    "materialize_fact_memory_draft",
]

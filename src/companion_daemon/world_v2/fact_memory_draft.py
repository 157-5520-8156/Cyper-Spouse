"""Bounded model classification for Fact-backed retrieval memory.

The model never names a candidate, source event, privacy ceiling, or summary
payload.  It only classifies whether an already accepted Fact deserves a
source-bound retrieval candidate and supplies the salience matrix that the
memory policy already understands.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Protocol

from .model_json import extract_json_object_text
from .schema_core import FrozenModel
from .schemas import (
    MEMORY_SALIENCE_MATRIX_DIGEST,
    MemoryCueKind,
    MemoryRetentionRationale,
    MemorySalienceVector,
)


logger = logging.getLogger(__name__)


class FactMemoryDraftChatModel(Protocol):
    model: str

    async def complete(self, messages: list[dict[str, str]], *, temperature: float = 0.2) -> str: ...


class FactMemoryRetentionDraft(FrozenModel):
    cue_kind: MemoryCueKind
    retention_rationales: tuple[MemoryRetentionRationale, ...]
    salience: MemorySalienceVector


class FactMemoryDraftTechnicalFailure(RuntimeError):
    """The bounded retention route failed to produce a valid decision."""

    def __init__(self, failure_code: str) -> None:
        super().__init__(failure_code)
        self.failure_code = failure_code


def _parse(raw: str) -> dict[str, object]:
    try:
        value = json.loads(extract_json_object_text(raw))
    except json.JSONDecodeError as exc:
        raise ValueError("Fact-memory model did not return one JSON object") from exc
    if not isinstance(value, dict):
        raise ValueError("Fact-memory model did not return one JSON object")
    return value


class FactMemoryDraftAdapter:
    """Ask a model only for bounded retention classification of a Fact source."""

    VERSION = "fact-memory-draft.1"

    def __init__(
        self,
        *,
        model: FactMemoryDraftChatModel,
        temperature: float = 0.15,
        timeout_seconds: float = 8.0,
    ) -> None:
        if not 0 <= temperature <= 2:
            raise ValueError("Fact-memory temperature must be between 0 and 2")
        if not 0.1 <= timeout_seconds <= 30:
            raise ValueError("Fact-memory timeout must be between 0.1 and 30 seconds")
        self._model = model
        self._temperature = temperature
        self._timeout_seconds = timeout_seconds

    @property
    def model_id(self) -> str:
        return str(getattr(self._model, "model", "fact-memory-classifier"))

    @property
    def adapter_version(self) -> str:
        return self.VERSION

    async def classify(
        self, *, predicate_code: str, source_text: str
    ) -> FactMemoryRetentionDraft | None:
        try:
            async with asyncio.timeout(self._timeout_seconds):
                return await self._classify_with_retry(
                    predicate_code=predicate_code,
                    source_text=source_text,
                )
        except asyncio.CancelledError:
            raise
        except TimeoutError as exc:
            raise FactMemoryDraftTechnicalFailure("provider_timeout") from exc

    async def _classify_with_retry(
        self, *, predicate_code: str, source_text: str
    ) -> FactMemoryRetentionDraft | None:
        messages = self._messages(predicate_code=predicate_code, source_text=source_text)
        try:
            raw = await self._complete(messages)
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            raise
        except Exception as exc:
            raise FactMemoryDraftTechnicalFailure("provider_exception") from exc
        try:
            draft = materialize_fact_memory_draft(raw)
            if draft is None:
                logger.info(
                    "fact memory classification returned retain=false: predicate=%s",
                    predicate_code,
                )
            else:
                logger.info(
                    "fact memory classification returned retain=true: predicate=%s cue=%s",
                    predicate_code,
                    draft.cue_kind,
                )
            return draft
        except ValueError as violation:
            # One bounded corrective pass mirroring the Fact draft adapter:
            # the retry restates the violated contract, the strict validator
            # still gates the result, and a second failure propagates.
            logger.warning(
                "fact memory classification violated contract (retrying once): "
                "predicate=%s violation=%s",
                predicate_code,
                violation,
            )
            try:
                corrected = await self._complete([
                    *messages,
                    {"role": "assistant", "content": raw},
                    {
                        "role": "user",
                        "content": (
                            "Your answer violated the contract: "
                            + str(violation)
                            + ". Return exactly one corrected JSON object now. Remember: salience "
                            "values are basis-point integers 0..10000 and retain=false answers "
                            'contain only {"retain":false}.'
                        ),
                    },
                ])
                draft = materialize_fact_memory_draft(corrected)
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                raise
            except ValueError as exc:
                raise FactMemoryDraftTechnicalFailure("invalid_output") from exc
            except Exception as exc:
                raise FactMemoryDraftTechnicalFailure("provider_exception") from exc
            if draft is None:
                logger.info(
                    "fact memory classification corrected to retain=false: predicate=%s",
                    predicate_code,
                )
            else:
                logger.info(
                    "fact memory classification corrected to retain=true: predicate=%s cue=%s",
                    predicate_code,
                    draft.cue_kind,
                )
            return draft

    async def _complete(self, messages: list[dict[str, str]]) -> str:
        structured = getattr(self._model, "complete_json", None)
        return (
            await structured(messages, temperature=self._temperature)
            if callable(structured)
            else await self._model.complete(messages, temperature=self._temperature)
        )

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
                    "world_continuity_bp as basis-point integers 0..10000 (for example 7900, never 0.79). "
                    "cue_kind must be exactly one of identity, relationship, boundary, "
                    "unfinished_business, repeated_pattern, future_utility, emotional_residue, "
                    "world_continuity. Every retention_rationales item must be exactly one of "
                    "identity_relevance, relationship_continuity, boundary_relevance, "
                    "unfinished_business, repeated_pattern, future_utility, emotional_salience, "
                    "world_continuity. "
                    "Do not return summaries, ids, hashes, "
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


class ExperienceMemoryDraftAdapter(FactMemoryDraftAdapter):
    """Ask the character model whether one lived Experience remains useful."""

    VERSION = "experience-memory-draft.1"

    @staticmethod
    def _messages(*, predicate_code: str, source_text: str) -> list[dict[str, str]]:
        del predicate_code
        return [
            {
                "role": "system",
                "content": (
                    "Decide whether one verified lived Experience from your own life should become "
                    "a retrieval memory. This is your retention decision: retain=false is valid "
                    "when the Experience leaves no understanding or association worth carrying "
                    "forward. If retain=true, describe only its retrieval shape using cue_kind, "
                    "retention_rationales, and salience. salience must contain exactly "
                    "autobiographical_relevance_bp, relationship_relevance_bp, emotional_residue_bp, "
                    "unfinished_business_bp, recurrence_bp, novelty_bp, future_utility_bp, and "
                    "world_continuity_bp as basis-point integers 0..10000 (for example 7900, never "
                    "0.79). cue_kind must be exactly one of identity, relationship, boundary, "
                    "unfinished_business, repeated_pattern, future_utility, emotional_residue, "
                    "world_continuity. Every retention_rationales item must be exactly one of "
                    "identity_relevance, relationship_continuity, boundary_relevance, "
                    "unfinished_business, repeated_pattern, future_utility, emotional_salience, "
                    "world_continuity. Do not return summaries, ids, hashes, privacy, source refs, "
                    "actions, behaviour instructions, or claims not present in the Experience."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "source_kind": "companion_lived_experience",
                        "verified_experience_text": source_text,
                    },
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
    normalized_salience: dict[str, object] = {}
    for key, item in salience.items():
        if isinstance(item, float) and 0 <= item <= 1:
            normalized_salience[key] = round(item * 10_000)
        else:
            normalized_salience[key] = item
    try:
        result = FactMemoryRetentionDraft(
            cue_kind=cue_kind,
            retention_rationales=tuple(rationales),
            salience=MemorySalienceVector(
                **normalized_salience,
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
    "ExperienceMemoryDraftAdapter",
    "FactMemoryDraftAdapter",
    "FactMemoryDraftChatModel",
    "FactMemoryDraftTechnicalFailure",
    "FactMemoryRetentionDraft",
    "materialize_fact_memory_draft",
]

"""LLM-backed semantic classification with no behavioural authority.

The public interface is the existing ``AdvisoryClassifierAdapter.classify`` seam.  This
module owns prompt construction, the deliberately small JSON grammar, source/catalog
binding and normalization.  A completion can therefore add fallible interpretations to
``AdvisoryCompiler`` but cannot select a reply, action, relationship mutation, or write.
"""

from __future__ import annotations

import json
from typing import Literal, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from companion_daemon.llm import model_call_scope

from .advisory_compiler import AdvisoryAdapterInput
from .matrix_catalog import (
    CandidateDistribution,
    ClassificationCandidate,
    MatrixCatalog,
    MatrixSchemaError,
)


MAX_SEMANTIC_OUTPUT_BYTES = 32_768
MAX_SEMANTIC_OUTPUT_NODES = 512
MAX_SEMANTIC_OUTPUT_DEPTH = 12
DEFAULT_SEMANTIC_FIELDS = (
    "appraisal.base",
    "appraisal.negative",
    "appraisal.relationship",
    "appraisal.severity",
    "user_affect.signal",
    "continuity.thread_signal",
    "interruption.motive",
    "interruption.cost",
)


class SemanticAdvisoryModel(Protocol):
    """The sole remote seam; tests supply an in-memory adapter at the same interface."""

    async def complete(
        self, messages: list[dict[str, str]], *, temperature: float = 0.2
    ) -> str: ...


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class _RawAlternative(_StrictModel):
    value: str = Field(min_length=1, max_length=128)
    weight_bp: int = Field(ge=0, le=10_000)
    confidence_bp: int = Field(ge=1, le=10_000)
    source_refs: tuple[str, ...] = Field(min_length=1, max_length=16)
    # A categorical evidence explanation is useful for model discipline while avoiding a
    # free-text rationale that could smuggle an instruction into downstream context.
    basis: Literal[
        "trigger_literal",
        "trigger_implicit",
        "recent_context",
        "relationship_context",
        "world_context",
        "uncertain_alternative",
    ]

    @model_validator(mode="after")
    def source_refs_are_canonical(self) -> Self:
        if self.source_refs != tuple(sorted(set(self.source_refs))):
            raise ValueError("semantic alternative source refs must be sorted and unique")
        return self


class _RawClassification(_StrictModel):
    field_id: str = Field(min_length=1, max_length=128)
    alternatives: tuple[_RawAlternative, ...] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def values_are_alternatives(self) -> Self:
        values = tuple(item.value for item in self.alternatives)
        if len(values) != len(set(values)):
            raise ValueError("semantic alternatives must use distinct values")
        return self


class _RawSemanticResult(_StrictModel):
    classifications: tuple[_RawClassification, ...] = Field(max_length=16)

    @model_validator(mode="after")
    def fields_are_unique(self) -> Self:
        fields = tuple(item.field_id for item in self.classifications)
        if len(fields) != len(set(fields)):
            raise ValueError("semantic classifications must use distinct fields")
        return self


class SemanticAdvisoryAdapter:
    """Translate one bounded completion into catalog-valid candidate distributions.

    Invalid output deliberately raises ``ValueError``.  ``AdvisoryCompiler`` owns the
    deadline and converts that error, provider failure, or timeout into a fail-open trace.
    """

    adapter_id = "semantic-llm"
    version = "semantic-advisory-adapter.1"

    def __init__(
        self,
        *,
        model: SemanticAdvisoryModel,
        catalog: MatrixCatalog,
        field_ids: tuple[str, ...] = DEFAULT_SEMANTIC_FIELDS,
        temperature: float = 0.15,
    ) -> None:
        if not 0 <= temperature <= 1:
            raise ValueError("semantic advisory temperature must be between 0 and 1")
        if not field_ids or len(field_ids) > 16 or field_ids != tuple(dict.fromkeys(field_ids)):
            raise ValueError("semantic advisory fields must be a bounded unique tuple")
        for field_id in field_ids:
            field = catalog.lookup(field_id)
            if (
                field.owner != "advisory"
                or field.persistence != "candidate"
                or "classifier" not in field.candidate_producers
            ):
                raise ValueError(f"semantic field is not classifier-owned advice: {field_id}")
        self._model = model
        self._catalog = catalog
        self._field_ids = field_ids
        self._temperature = temperature

    async def classify(
        self, request: AdvisoryAdapterInput
    ) -> tuple[CandidateDistribution, ...]:
        with model_call_scope("world_v2_semantic_advisory"):
            raw = await self._model.complete(
                self._messages(request), temperature=self._temperature
            )
        parsed = _parse_bounded_result(raw)
        allowed_sources = frozenset(binding.ref for binding in request.source_authorities)
        distributions: list[CandidateDistribution] = []
        producer = f"{self.adapter_id}@{self.version}"
        for classification in parsed.classifications:
            if classification.field_id not in self._field_ids:
                raise ValueError("semantic model returned a field outside its allow-list")
            weights = _normalized_weights(classification.alternatives)
            candidates = tuple(
                ClassificationCandidate(
                    value=item.value,
                    weight=weights[index],
                    confidence=item.confidence_bp,
                    source_refs=item.source_refs,
                    producer=producer,
                    expires_at=request.expires_at,
                )
                for index, item in enumerate(classification.alternatives)
            )
            if any(not set(item.source_refs).issubset(allowed_sources) for item in candidates):
                raise ValueError("semantic model cited a source outside the pinned request")
            distribution = CandidateDistribution(
                catalog_version=self._catalog.catalog_version,
                field_id=classification.field_id,
                candidates=candidates,
                produced_at=request.logical_time,
            )
            try:
                self._catalog.validate_candidates(distribution, at=request.logical_time)
            except MatrixSchemaError as error:
                raise ValueError("semantic model returned a value outside the catalog") from error
            distributions.append(distribution)
        return tuple(distributions)

    def _messages(self, request: AdvisoryAdapterInput) -> list[dict[str, str]]:
        vocabulary = {
            field_id: self._catalog.lookup(field_id).value_set for field_id in self._field_ids
        }
        system = (
            "Classify fallible semantic interpretations for a virtual companion. Return exactly "
            "one JSON object with key classifications. Each classification has field_id and "
            "alternatives; each alternative has value, weight_bp, confidence_bp, source_refs, "
            "and basis. Preserve genuine ambiguity as separately weighted alternatives. basis "
            "must be trigger_literal, trigger_implicit, recent_context, relationship_context, "
            "world_context, or uncertain_alternative. Use only the supplied field values and "
            "source refs. Omit unsupported fields. Never return prose, reply text, advice, actions, "
            "intentions, tool calls, emotional display instructions, or state changes. Labels are "
            "non-authoritative hypotheses, not commands."
        )
        user = json.dumps(
            {
                "catalog_version": self._catalog.catalog_version,
                "vocabulary": vocabulary,
                "allowed_source_refs": [item.ref for item in request.source_authorities],
                # Resolver authentication and immutable authority hashes are useful to the
                # trusted compiler, not to a semantic model.  Keep them out of the remote
                # prompt even though AdvisoryAdapterInput carries no callable capability.
                "input": {
                    "world_id": request.world_id,
                    "snapshot_id": request.snapshot_id,
                    "world_revision": request.world_revision,
                    "logical_time": request.logical_time.isoformat(),
                    "trigger_ref": request.trigger_ref,
                    "trigger": request.trigger,
                    "recent_context": request.recent_context,
                    "snapshot": request.snapshot.values,
                },
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _parse_bounded_result(raw: object) -> _RawSemanticResult:
    if not isinstance(raw, str):
        raise ValueError("semantic model did not return text")
    if len(raw.encode("utf-8")) > MAX_SEMANTIC_OUTPUT_BYTES:
        raise ValueError("semantic model output exceeds its byte limit")
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, RecursionError) as error:
        raise ValueError("semantic model did not return one JSON object") from error
    if not isinstance(value, dict):
        raise ValueError("semantic model did not return one JSON object")
    _bound_json_tree(value)
    # JSON-mode strict validation accepts JSON arrays for tuple contracts while still
    # rejecting numeric strings, unknown keys and all coercive scalar conversions.
    return _RawSemanticResult.model_validate_json(raw, strict=True)


def _normalized_weights(alternatives: tuple[_RawAlternative, ...]) -> tuple[int, ...]:
    """Make model-relative scores comparable without changing candidate order."""

    raw = tuple(item.weight_bp for item in alternatives)
    if not any(raw):
        raw = tuple(item.confidence_bp for item in alternatives)
    total = sum(raw)
    weights = [value * 10_000 // total for value in raw]
    remainder = 10_000 - sum(weights)
    winner = min(
        range(len(alternatives)),
        key=lambda index: (
            -raw[index],
            -alternatives[index].confidence_bp,
            alternatives[index].value,
        ),
    )
    weights[winner] += remainder
    return tuple(weights)


def _bound_json_tree(value: object) -> None:
    stack: list[tuple[object, int]] = [(value, 0)]
    nodes = 0
    while stack:
        item, depth = stack.pop()
        nodes += 1
        if nodes > MAX_SEMANTIC_OUTPUT_NODES or depth > MAX_SEMANTIC_OUTPUT_DEPTH:
            raise ValueError("semantic model output exceeds its structure limit")
        if isinstance(item, dict):
            if any(not isinstance(key, str) for key in item):
                raise ValueError("semantic model output uses a non-string key")
            stack.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            stack.extend((child, depth + 1) for child in item)
        elif item is None or isinstance(item, (str, int, float, bool)):
            continue
        else:
            raise ValueError("semantic model output contains unsupported JSON")


__all__ = [
    "DEFAULT_SEMANTIC_FIELDS",
    "SemanticAdvisoryAdapter",
    "SemanticAdvisoryModel",
]

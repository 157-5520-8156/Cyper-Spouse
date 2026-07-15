"""Read-only, source-bound text retrieval for active MemoryCandidates.

``MemoryCandidate`` is intentionally only a retrieval-control authority.  This
module is the separate read seam that turns an eligible Fact-backed candidate
into a bounded excerpt of its exact persisted assertion message.  It never
changes candidate strength, writes events, or treats a model summary/ref as a
fact.
"""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import Field

from .ledger import LedgerPort, ObservationEventLocator
from .memory_reducers import evaluate_memory_retrieval
from .schema_core import FrozenModel, PrivacyClass
from .schemas import (
    FactTransitionProjection,
    MemoryCandidateProjection,
    MemorySourceBinding,
    Observation,
    ProjectionCursor,
)


class MemorySourceExcerpt(FrozenModel):
    """A bounded source-text view, pinned to a Memory source authority."""

    source_kind: Literal["fact"]
    source_id: str = Field(min_length=1)
    source_entity_revision: int = Field(ge=1)
    authority_event_ref: str = Field(min_length=1)
    authority_world_revision: int = Field(ge=1)
    authority_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_values_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    excerpt_ref: str = Field(min_length=1)
    excerpt_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    text: str = Field(min_length=1)
    truncated: bool


class MemoryRetrievalItem(FrozenModel):
    """Model-visible retrieval material without any new claim authority."""

    candidate_id: str = Field(min_length=1)
    cue_kind: str = Field(min_length=1)
    retention_rationales: tuple[str, ...] = Field(min_length=1)
    retrieval_strength_bp: int = Field(ge=1, le=10_000)
    source_excerpts: tuple[MemorySourceExcerpt, ...] = Field(min_length=1)
    truncated: bool


class MemoryRetrievalSuppression(FrozenModel):
    """Trace-only reason why an active-looking candidate supplied no text."""

    candidate_id: str = Field(min_length=1)
    reasons: tuple[
        Literal[
            "not_active",
            "stale_source",
            "privacy_ceiling",
            "content_unavailable",
            "source_proof_failed",
        ],
        ...,
    ] = Field(min_length=1)


class MemoryRetrievalResult(FrozenModel):
    items: tuple[MemoryRetrievalItem, ...]
    suppressions: tuple[MemoryRetrievalSuppression, ...]


class MemoryRetrievalCompiler:
    """Deep read module for source-bound memory excerpts.

    The caller needs only a complete cursor, a candidate set already scoped by
    Context, a viewer privacy ceiling, and a fixed per-source text budget.
    All source validation and observation reads remain private to this module.
    """

    def __init__(self, *, ledger: LedgerPort, max_excerpt_characters: int = 480) -> None:
        if max_excerpt_characters <= 0:
            raise ValueError("memory excerpt budget must be positive")
        self._ledger = ledger
        self._max_excerpt_characters = max_excerpt_characters

    def compile(
        self,
        *,
        cursor: ProjectionCursor,
        candidates: tuple[MemoryCandidateProjection, ...],
        viewer_privacy_ceiling: PrivacyClass,
    ) -> MemoryRetrievalResult:
        projection = self._ledger.project_at(cursor)
        decisions = {
            item.candidate_id: item
            for item in evaluate_memory_retrieval(
                candidates,
                facts=projection.facts,
                fact_history=projection.fact_transitions,
                experiences=projection.experiences,
                experience_history=projection.experience_transitions,
                threads=projection.threads,
                thread_history=projection.thread_transitions,
                committed_events=projection.committed_world_event_refs,
                viewer_privacy_ceiling=viewer_privacy_ceiling,
            )
        }
        items: list[MemoryRetrievalItem] = []
        suppressions: list[MemoryRetrievalSuppression] = []
        for candidate in candidates:
            decision = decisions[candidate.candidate_id]
            if not decision.eligible:
                suppressions.append(
                    MemoryRetrievalSuppression(
                        candidate_id=candidate.candidate_id,
                        reasons=decision.suppression_reasons,
                    )
                )
                continue
            excerpts: list[MemorySourceExcerpt] = []
            unavailable = False
            for binding in candidate.values.source_bindings:
                if binding.source_kind != "fact":
                    unavailable = True
                    continue
                excerpt = self._fact_excerpt(
                    binding=binding,
                    cursor=cursor,
                    projection=projection,
                )
                if excerpt is None:
                    unavailable = True
                else:
                    excerpts.append(excerpt)
            if not excerpts:
                suppressions.append(
                    MemoryRetrievalSuppression(
                        candidate_id=candidate.candidate_id,
                        reasons=("content_unavailable",),
                    )
                )
                continue
            if unavailable:
                suppressions.append(
                    MemoryRetrievalSuppression(
                        candidate_id=candidate.candidate_id,
                        reasons=("content_unavailable",),
                    )
                )
            items.append(
                MemoryRetrievalItem(
                    candidate_id=candidate.candidate_id,
                    cue_kind=candidate.values.cue_kind,
                    retention_rationales=candidate.values.retention_rationales,
                    retrieval_strength_bp=candidate.values.retrieval_strength_bp,
                    source_excerpts=tuple(excerpts),
                    truncated=any(item.truncated for item in excerpts),
                )
            )
        return MemoryRetrievalResult(items=tuple(items), suppressions=tuple(suppressions))

    def _fact_excerpt(
        self,
        *,
        binding: MemorySourceBinding,
        cursor: ProjectionCursor,
        projection,
    ) -> MemorySourceExcerpt | None:
        transition = next(
            (
                item
                for item in projection.fact_transitions
                if item.fact_id == binding.source_id
                and item.entity_revision == binding.source_entity_revision
                and item.accepted_event_ref == binding.authority_event_ref
            ),
            None,
        )
        if transition is None or not self._fact_binding_matches(transition, binding):
            return None
        assertion = transition.values_after.assertion_binding
        if assertion.source_kind != "observed_message":
            return None
        reference = next(
            (item for item in projection.message_observations if item.observation_id == assertion.source_ref),
            None,
        )
        if reference is None or (
            reference.actor != assertion.actor_ref
            or reference.channel != assertion.channel
            or reference.payload_ref != assertion.payload_ref
            or reference.content_payload_hash != assertion.content_payload_hash
        ):
            return None
        events = self._ledger.observation_events_at(
            (
                ObservationEventLocator.for_message(
                    world_id=self._ledger.world_id,
                    observation_id=reference.observation_id,
                    source=reference.source,
                    source_event_id=reference.source_event_id,
                ),
            ),
            cursor=cursor,
        )
        if len(events) != 1:
            return None
        event = events[0].event
        try:
            observation = Observation.model_validate_json(event.payload_json)
        except ValueError:
            return None
        if (
            event.event_type != "ObservationRecorded"
            or event.payload_hash != reference.event_payload_hash
            or observation.observation_id != reference.observation_id
            or observation.actor != assertion.actor_ref
            or observation.channel != assertion.channel
            or observation.payload_ref != assertion.payload_ref
            or observation.payload_hash != assertion.content_payload_hash
            or observation.text is None
        ):
            return None
        source_text = observation.text
        text = source_text[: self._max_excerpt_characters]
        return MemorySourceExcerpt(
            source_kind="fact",
            source_id=binding.source_id,
            source_entity_revision=binding.source_entity_revision,
            authority_event_ref=binding.authority_event_ref,
            authority_world_revision=binding.authority_world_revision,
            authority_payload_hash=binding.authority_payload_hash,
            source_values_hash=binding.source_values_hash,
            excerpt_ref=observation.observation_id,
            excerpt_payload_hash=hashlib.sha256(source_text.encode("utf-8")).hexdigest(),
            text=text,
            truncated=text != source_text,
        )

    @staticmethod
    def _fact_binding_matches(
        transition: FactTransitionProjection, binding: MemorySourceBinding
    ) -> bool:
        # ``evaluate_memory_retrieval`` already proves full lifecycle/current
        # eligibility. This repeats only the content-path identity check before
        # a raw observation is opened.
        material = transition.values_after.model_dump(mode="json")
        values_hash = hashlib.sha256(
            json.dumps(
                material, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
        return values_hash == binding.source_values_hash


__all__ = [
    "MemoryRetrievalCompiler",
    "MemoryRetrievalItem",
    "MemoryRetrievalResult",
    "MemoryRetrievalSuppression",
    "MemorySourceExcerpt",
]

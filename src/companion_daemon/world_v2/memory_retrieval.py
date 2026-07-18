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
from .life_content_store import ImmutableLifeContentStore
from .memory_reducers import evaluate_memory_retrieval
from .schema_core import FrozenModel, PrivacyClass
from .schemas import (
    ExperienceProjection,
    ExperienceTransitionProjection,
    LifeContentDescriptorProjection,
    FactTransitionProjection,
    MemoryCandidateProjection,
    MemorySourceBinding,
    Observation,
    ProjectionCursor,
)


class MemorySourceExcerpt(FrozenModel):
    """A bounded source-text view, pinned to a Memory source authority."""

    source_kind: Literal["fact", "experience"]
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
    privacy_ceiling: PrivacyClass
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

    def __init__(
        self,
        *,
        ledger: LedgerPort,
        life_content_store: ImmutableLifeContentStore | None = None,
        max_excerpt_characters: int = 480,
    ) -> None:
        if max_excerpt_characters <= 0:
            raise ValueError("memory excerpt budget must be positive")
        self._ledger = ledger
        self._life_content_store = life_content_store
        self._max_excerpt_characters = max_excerpt_characters

    def compile(
        self,
        *,
        cursor: ProjectionCursor,
        candidates: tuple[MemoryCandidateProjection, ...],
        viewer_privacy_ceiling: PrivacyClass,
        projection=None,
    ) -> MemoryRetrievalResult:
        projection = projection if projection is not None else self._ledger.project_at(cursor)
        if (
            projection.world_revision != cursor.world_revision
            or projection.deliberation_revision != cursor.deliberation_revision
            or projection.ledger_sequence != cursor.ledger_sequence
        ):
            raise ValueError("memory retrieval projection does not match its pinned cursor")
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

        # A context compile can expose several retained facts.  Reading each
        # source through ``observation_events_at`` separately repeats the
        # pinned-boundary proof and SQLite transaction for every candidate.
        # Collect the exact locators first and open the immutable history in
        # one batch.  This is only a read coalescing optimization: every
        # locator remains source-bound and the ledger still performs all proof
        # checks.
        source_locators: dict[str, ObservationEventLocator] = {}
        for candidate in candidates:
            decision = decisions[candidate.candidate_id]
            if not decision.eligible:
                continue
            for binding in candidate.values.source_bindings:
                if binding.source_kind != "fact":
                    continue
                locator = self._fact_locator(
                    binding=binding,
                    cursor=cursor,
                    projection=projection,
                )
                if locator is not None:
                    source_locators[locator.idempotency_key] = locator
        historical_by_identity = {}
        if source_locators:
            locators = tuple(
                sorted(
                    source_locators.values(),
                    key=lambda item: (
                        item.observation_id,
                        item.event_type,
                        item.idempotency_key,
                    ),
                )
            )
            for item in self._ledger.observation_events_at(locators, cursor=cursor):
                historical_by_identity[item.event.idempotency_key] = item
                # Test/durable LedgerPort implementations may expose the
                # same exact observation under a different event idempotency
                # key; the payload's observation id is still the projection's
                # source identity and is safe to use as a local join key.
                try:
                    observation = Observation.model_validate_json(item.event.payload_json)
                except ValueError:
                    continue
                historical_by_identity[observation.observation_id] = item
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
                if binding.source_kind == "fact":
                    excerpt = self._fact_excerpt(
                        binding=binding,
                        cursor=cursor,
                        projection=projection,
                        historical_by_identity=historical_by_identity,
                    )
                elif binding.source_kind == "experience":
                    excerpt = self._experience_excerpt(
                        binding=binding,
                        projection=projection,
                    )
                else:
                    excerpt = None
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
                    privacy_ceiling=candidate.values.privacy_ceiling,
                    retrieval_strength_bp=candidate.values.retrieval_strength_bp,
                    source_excerpts=tuple(excerpts),
                    truncated=any(item.truncated for item in excerpts),
                )
            )
        return MemoryRetrievalResult(items=tuple(items), suppressions=tuple(suppressions))

    def _experience_excerpt(
        self,
        *,
        binding: MemorySourceBinding,
        projection,
    ) -> MemorySourceExcerpt | None:
        """Read one exact Experience summary through its descriptor and sidecar.

        A memory candidate may retain an Experience after it leaves the small
        ``recent_experiences`` context window.  The candidate does not grant
        access to prose by itself: the Experience transition, committed
        source event, LifeContent descriptor, and immutable sidecar bytes must
        all still agree at this projection cursor.
        """

        if self._life_content_store is None:
            return None
        experience = next(
            (
                item
                for item in projection.experiences
                if isinstance(item, ExperienceProjection)
                and item.experience_id == binding.source_id
            ),
            None,
        )
        transition = next(
            (
                item
                for item in projection.experience_transitions
                if isinstance(item, ExperienceTransitionProjection)
                and item.experience_id == binding.source_id
                and item.entity_revision == binding.source_entity_revision
                and item.accepted_event_ref == binding.authority_event_ref
            ),
            None,
        )
        committed = next(
            (
                item
                for item in projection.committed_world_event_refs
                if item.event_id == binding.authority_event_ref
                and item.event_type == "ExperienceCommitted"
            ),
            None,
        )
        if experience is None or transition is None or committed is None:
            return None
        if (
            experience.entity_revision != binding.source_entity_revision
            or experience.origin.accepted_event_ref != binding.authority_event_ref
            or committed.world_revision != binding.authority_world_revision
            or committed.payload_hash != binding.authority_payload_hash
            or transition.values_after != experience.values
            or transition.semantic_fingerprint_after != experience.semantic_fingerprint
            or self._canonical_hash(transition.values_after) != binding.source_values_hash
        ):
            return None
        descriptor = next(
            (
                item
                for item in projection.life_content_descriptors
                if isinstance(item, LifeContentDescriptorProjection)
                and item.source_kind == "experience"
                and item.source_entity_id == experience.experience_id
                and item.source_entity_revision == experience.entity_revision
                and item.source_event_ref == binding.authority_event_ref
                and item.source_world_revision == binding.authority_world_revision
                and item.source_payload_hash == binding.authority_payload_hash
                and item.content_ref == experience.values.summary_ref
                and item.content_payload_hash == experience.values.summary_payload_hash
            ),
            None,
        )
        if descriptor is None or descriptor.privacy_class != experience.values.privacy_class:
            return None
        descriptor_event = next(
            (
                item
                for item in projection.committed_world_event_refs
                if item.event_id == descriptor.descriptor_event_ref
                and item.event_type == "LifeContentRecorded"
                and item.world_revision == descriptor.descriptor_world_revision
                and item.payload_hash == descriptor.descriptor_payload_hash
            ),
            None,
        )
        if descriptor_event is None:
            return None
        stored = self._life_content_store.read_exact(content_ref=descriptor.content_ref)
        if stored is None or (
            stored.content_kind != "experience_summary"
            or stored.content_payload_hash != descriptor.content_payload_hash
        ):
            return None
        text = stored.text[: self._max_excerpt_characters]
        return MemorySourceExcerpt(
            source_kind="experience",
            source_id=binding.source_id,
            source_entity_revision=binding.source_entity_revision,
            authority_event_ref=binding.authority_event_ref,
            authority_world_revision=binding.authority_world_revision,
            authority_payload_hash=binding.authority_payload_hash,
            source_values_hash=binding.source_values_hash,
            excerpt_ref=descriptor.content_ref,
            excerpt_payload_hash=descriptor.content_payload_hash,
            text=text,
            truncated=text != stored.text,
        )

    @staticmethod
    def _canonical_hash(value) -> str:
        return hashlib.sha256(
            json.dumps(
                value.model_dump(mode="json"),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()

    def _fact_excerpt(
        self,
        *,
        binding: MemorySourceBinding,
        cursor: ProjectionCursor,
        projection,
        historical_by_identity=None,
    ) -> MemorySourceExcerpt | None:
        locator = self._fact_locator(binding=binding, cursor=cursor, projection=projection)
        if locator is None:
            return None
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
        if transition is None:
            return None
        assertion = transition.values_after.assertion_binding
        historical = (
            historical_by_identity.get(locator.idempotency_key)
            if historical_by_identity is not None
            else None
        )
        if historical is None and historical_by_identity is not None:
            historical = historical_by_identity.get(assertion.source_ref)
        if historical_by_identity is None:
            events = self._ledger.observation_events_at((locator,), cursor=cursor)
            historical = events[0] if len(events) == 1 else None
        if historical is None:
            return None
        event = historical.event
        reference = self._message_reference_for_binding(binding=binding, projection=projection)
        if reference is None:
            return None
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

    def _fact_locator(
        self,
        *,
        binding: MemorySourceBinding,
        cursor: ProjectionCursor,
        projection,
    ) -> ObservationEventLocator | None:
        del cursor  # Kept in the seam for callers that pin all source reads.
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
        reference = self._message_reference_for_binding(binding=binding, projection=projection)
        if reference is None:
            return None
        return ObservationEventLocator.for_message(
            world_id=self._ledger.world_id,
            observation_id=reference.observation_id,
            source=reference.source,
            source_event_id=reference.source_event_id,
        )

    @staticmethod
    def _message_reference_for_binding(*, binding: MemorySourceBinding, projection):
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
        if transition is None:
            return None
        assertion = transition.values_after.assertion_binding
        if assertion.source_kind != "observed_message":
            return None
        reference = next(
            (
                item
                for item in projection.message_observations
                if item.observation_id == assertion.source_ref
            ),
            None,
        )
        if reference is None or (
            reference.actor != assertion.actor_ref
            or reference.channel != assertion.channel
            or reference.payload_ref != assertion.payload_ref
            or reference.content_payload_hash != assertion.content_payload_hash
        ):
            return None
        return reference

    @staticmethod
    def _fact_binding_matches(
        transition: FactTransitionProjection, binding: MemorySourceBinding
    ) -> bool:
        # ``evaluate_memory_retrieval`` already proves full lifecycle/current
        # eligibility. This repeats only the content-path identity check before
        # a raw observation is opened.
        material = transition.values_after.model_dump(mode="json")
        values_hash = hashlib.sha256(
            json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return values_hash == binding.source_values_hash


__all__ = [
    "MemoryRetrievalCompiler",
    "MemoryRetrievalItem",
    "MemoryRetrievalResult",
    "MemoryRetrievalSuppression",
    "MemorySourceExcerpt",
]

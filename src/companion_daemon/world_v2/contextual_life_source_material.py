"""Exact, privacy-bounded source material for contextual life inspiration.

The inspiration model must not infer the selected source from whichever
dialogue items happened to survive a general Context budget.  This compiler
rehydrates that one source at the same pinned ledger cursor, proves its event
authority, and exposes only content the character-facing life lane may read.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

from .ledger_context_resolver import fact_recall_items
from .life_content_store import ImmutableLifeContentStore
from .memory_retrieval import MemoryRetrievalCompiler
from .schema_core import FrozenModel, PrivacyClass
from .schemas import Observation, ProjectionCursor


_PRIVACY_RANK: dict[PrivacyClass, int] = {
    "public": 0,
    "shareable": 1,
    "personal": 2,
    "private": 3,
    "withhold": 4,
}
_PERSISTENT_SOURCE_PRIVACY_CEILING: PrivacyClass = "personal"
_MAX_EXCERPT_CHARACTERS = 960


class ContextualLifeSourceAuthority(FrozenModel):
    role: Literal["selected_source", "content_source"]
    event_ref: str = Field(min_length=1)
    event_type: str = Field(min_length=1)
    world_revision: int = Field(ge=1)
    payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class ContextualLifeSourceContent(FrozenModel):
    content_ref: str = Field(min_length=1)
    content_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    text: str = Field(min_length=1, max_length=_MAX_EXCERPT_CHARACTERS)
    truncated: bool


class ContextualLifeSourceMaterial(FrozenModel):
    event_ref: str = Field(min_length=1)
    event_type: Literal[
        "ObservationRecorded",
        "FactCommittedV2",
        "MemoryCandidateAccepted",
    ]
    actor_ref: str = Field(min_length=1)
    subject_ref: str | None = Field(default=None, min_length=1)
    logical_time: datetime
    privacy_class: PrivacyClass
    contents: tuple[ContextualLifeSourceContent, ...] = Field(
        min_length=1,
        max_length=8,
    )
    authority_bindings: tuple[ContextualLifeSourceAuthority, ...] = Field(
        min_length=1,
        max_length=16,
    )


class ContextualLifeSourceMaterialCompiler:
    """Resolve one selected event without relying on the recent-dialogue tail."""

    def __init__(
        self,
        *,
        ledger,
        life_content_store: ImmutableLifeContentStore | None = None,
    ) -> None:
        self._ledger = ledger
        self._memories = MemoryRetrievalCompiler(
            ledger=ledger,
            life_content_store=life_content_store,
            max_excerpt_characters=_MAX_EXCERPT_CHARACTERS,
        )

    def compile(
        self,
        *,
        cursor: ProjectionCursor,
        source_event_ref: str,
        owner_actor_ref: str,
    ) -> ContextualLifeSourceMaterial | None:
        projection = self._ledger.project_at(cursor)
        if (
            projection.world_revision != cursor.world_revision
            or projection.deliberation_revision != cursor.deliberation_revision
            or projection.ledger_sequence != cursor.ledger_sequence
        ):
            raise ValueError("contextual source projection is not pinned to its cursor")
        ref = next(
            (
                item
                for item in projection.committed_world_event_refs
                if item.event_id == source_event_ref
            ),
            None,
        )
        located = self._ledger.lookup_event_commit(source_event_ref)
        if ref is None or located is None:
            return None
        event, commit = located
        if (
            event.event_id != ref.event_id
            or event.event_type != ref.event_type
            or event.payload_hash != ref.payload_hash
            # CommitResult describes the cursor after the whole atomic batch,
            # while a committed event ref keeps its event-level revision.
            or event.event_id not in commit.event_ids
            or commit.world_revision < ref.world_revision
            or commit.deliberation_revision > cursor.deliberation_revision
            or commit.ledger_sequence > cursor.ledger_sequence
        ):
            return None
        if event.event_type == "ObservationRecorded":
            return self._observation(event=event, ref=ref, owner_actor_ref=owner_actor_ref)
        if event.event_type == "FactCommittedV2":
            return self._fact(
                projection=projection,
                ref=ref,
                owner_actor_ref=owner_actor_ref,
            )
        if event.event_type == "MemoryCandidateAccepted":
            return self._memory(
                projection=projection,
                cursor=cursor,
                ref=ref,
                owner_actor_ref=owner_actor_ref,
            )
        return None

    def _observation(
        self,
        *,
        event,
        ref,
        owner_actor_ref: str,
    ) -> ContextualLifeSourceMaterial | None:
        try:
            observation = Observation.model_validate_json(event.payload_json)
        except ValueError:
            return None
        if (
            observation.world_id != self._ledger.world_id
            or observation.actor != event.actor
            or observation.actor == owner_actor_ref
            or not observation.actor.startswith("user:")
            or not observation.text
        ):
            return None
        content = self._content(
            content_ref=observation.payload_ref,
            content_payload_hash=observation.payload_hash,
            text=observation.text,
        )
        return ContextualLifeSourceMaterial(
            event_ref=ref.event_id,
            event_type="ObservationRecorded",
            actor_ref=observation.actor,
            subject_ref=observation.actor,
            logical_time=ref.logical_time,
            privacy_class="private",
            contents=(content,),
            authority_bindings=(self._authority(ref, role="selected_source"),),
        )

    def _fact(
        self,
        *,
        projection,
        ref,
        owner_actor_ref: str,
    ) -> ContextualLifeSourceMaterial | None:
        fact = next(
            (
                item
                for item in projection.facts
                if item.origin.accepted_event_ref == ref.event_id
                and item.values.status == "active"
            ),
            None,
        )
        if fact is None or not self._persistent_privacy_is_readable(
            fact.values.privacy_class
        ):
            return None
        assertion = fact.values.assertion_binding
        if (
            assertion.source_kind != "observed_message"
            or assertion.actor_ref is None
            or not assertion.actor_ref.startswith("user:")
            or fact.values.subject_ref
            not in {owner_actor_ref, assertion.actor_ref}
        ):
            return None
        recalls = fact_recall_items(
            ledger=self._ledger,
            projection=projection,
            facts=(fact,),
        )
        recall = next(
            (
                item
                for item in recalls
                if item.fact_id == fact.fact_id
                and item.accepted_fact_event_ref == ref.event_id
            ),
            None,
        )
        if recall is None:
            return None
        observation_ref = next(
            (
                item
                for item in projection.committed_world_event_refs
                if item.event_id == recall.observation_event_ref
                and item.world_revision == recall.observation_world_revision
                and item.payload_hash == recall.observation_event_payload_hash
            ),
            None,
        )
        if observation_ref is None:
            return None
        return ContextualLifeSourceMaterial(
            event_ref=ref.event_id,
            event_type="FactCommittedV2",
            actor_ref=assertion.actor_ref,
            subject_ref=fact.values.subject_ref,
            logical_time=ref.logical_time,
            privacy_class=fact.values.privacy_class,
            contents=(
                self._content(
                    content_ref=recall.assertion_payload_ref,
                    content_payload_hash=recall.assertion_payload_hash,
                    text=recall.source_excerpt,
                ),
            ),
            authority_bindings=(
                self._authority(ref, role="selected_source"),
                self._authority(observation_ref, role="content_source"),
            ),
        )

    def _memory(
        self,
        *,
        projection,
        cursor: ProjectionCursor,
        ref,
        owner_actor_ref: str,
    ) -> ContextualLifeSourceMaterial | None:
        candidate = next(
            (
                item
                for item in projection.memory_candidates
                if item.origin.accepted_event_ref == ref.event_id
                and item.values.status == "active"
            ),
            None,
        )
        if candidate is None or not self._persistent_privacy_is_readable(
            candidate.values.privacy_ceiling
        ):
            return None
        if not self._memory_sources_belong_to_character_context(
            candidate=candidate,
            projection=projection,
            owner_actor_ref=owner_actor_ref,
        ):
            return None
        result = self._memories.compile(
            cursor=cursor,
            candidates=(candidate,),
            viewer_privacy_ceiling=_PERSISTENT_SOURCE_PRIVACY_CEILING,
            projection=projection,
        )
        item = next(
            (
                value
                for value in result.items
                if value.candidate_id == candidate.candidate_id
            ),
            None,
        )
        if item is None:
            return None
        source_refs = {
            source.authority_event_ref: source
            for source in item.source_excerpts
        }
        authorities = [self._authority(ref, role="selected_source")]
        for event_ref, source in sorted(source_refs.items()):
            authority = next(
                (
                    value
                    for value in projection.committed_world_event_refs
                    if value.event_id == event_ref
                    and value.world_revision == source.authority_world_revision
                    and value.payload_hash == source.authority_payload_hash
                ),
                None,
            )
            if authority is None:
                return None
            authorities.append(self._authority(authority, role="content_source"))
        actor_refs = {
            self._memory_source_actor(binding=binding, projection=projection)
            for binding in candidate.values.source_bindings
        }
        actor_refs.discard(None)
        return ContextualLifeSourceMaterial(
            event_ref=ref.event_id,
            event_type="MemoryCandidateAccepted",
            actor_ref=(
                sorted(actor_refs)[0]
                if actor_refs
                else owner_actor_ref
            ),
            subject_ref=owner_actor_ref,
            logical_time=ref.logical_time,
            privacy_class=candidate.values.privacy_ceiling,
            contents=tuple(
                ContextualLifeSourceContent(
                    content_ref=source.excerpt_ref,
                    content_payload_hash=source.excerpt_payload_hash,
                    text=source.text,
                    truncated=source.truncated,
                )
                for source in item.source_excerpts
            ),
            authority_bindings=tuple(authorities),
        )

    @staticmethod
    def _memory_sources_belong_to_character_context(
        *,
        candidate,
        projection,
        owner_actor_ref: str,
    ) -> bool:
        for binding in candidate.values.source_bindings:
            if binding.source_kind == "fact":
                fact = next(
                    (
                        item
                        for item in projection.facts
                        if item.fact_id == binding.source_id
                        and item.entity_revision == binding.source_entity_revision
                    ),
                    None,
                )
                if fact is None:
                    return False
                actor = fact.values.assertion_binding.actor_ref
                if (
                    actor is None
                    or not actor.startswith("user:")
                    or fact.values.subject_ref not in {owner_actor_ref, actor}
                ):
                    return False
            elif binding.source_kind == "experience":
                experience = next(
                    (
                        item
                        for item in projection.experiences
                        if item.experience_id == binding.source_id
                        and item.entity_revision == binding.source_entity_revision
                    ),
                    None,
                )
                if (
                    experience is None
                    or owner_actor_ref not in experience.values.participant_refs
                ):
                    return False
            else:
                return False
        return True

    @staticmethod
    def _memory_source_actor(*, binding, projection) -> str | None:
        if binding.source_kind != "fact":
            return None
        fact = next(
            (
                item
                for item in projection.facts
                if item.fact_id == binding.source_id
                and item.entity_revision == binding.source_entity_revision
            ),
            None,
        )
        return (
            fact.values.assertion_binding.actor_ref
            if fact is not None
            else None
        )

    @staticmethod
    def _persistent_privacy_is_readable(privacy: PrivacyClass) -> bool:
        return (
            privacy != "withhold"
            and _PRIVACY_RANK[privacy]
            <= _PRIVACY_RANK[_PERSISTENT_SOURCE_PRIVACY_CEILING]
        )

    @staticmethod
    def _content(
        *,
        content_ref: str,
        content_payload_hash: str,
        text: str,
    ) -> ContextualLifeSourceContent:
        excerpt = text[:_MAX_EXCERPT_CHARACTERS]
        return ContextualLifeSourceContent(
            content_ref=content_ref,
            content_payload_hash=content_payload_hash,
            text=excerpt,
            truncated=excerpt != text,
        )

    @staticmethod
    def _authority(ref, *, role: Literal["selected_source", "content_source"]):
        return ContextualLifeSourceAuthority(
            role=role,
            event_ref=ref.event_id,
            event_type=ref.event_type,
            world_revision=ref.world_revision,
            payload_hash=ref.payload_hash,
        )


__all__ = [
    "ContextualLifeSourceAuthority",
    "ContextualLifeSourceContent",
    "ContextualLifeSourceMaterial",
    "ContextualLifeSourceMaterialCompiler",
]

"""Source-bound retrieval memory lifecycle for committed life Experiences.

Facts and lived Experiences share the same MemoryCandidate authority.  This
adapter only supplies the different source proof; proposal, acceptance,
privacy, salience and replay semantics remain in
``FactMemoryCandidateLifecycle``.
"""

from __future__ import annotations

from datetime import datetime
import hashlib
import json

from .fact_memory_candidate_lifecycle import FactMemoryCandidateLifecycle
from .fact_memory_draft import FactMemoryRetentionDraft
from .life_content_store import ImmutableLifeContentStore
from .schemas import (
    ExperienceProjection,
    ExperienceTransitionProjection,
    LifeContentDescriptorProjection,
    MemoryCandidateProjection,
    MemorySourceBinding,
    WorldEvent,
)


def _canonical_hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class ExperienceMemoryCandidateLifecycle(FactMemoryCandidateLifecycle):
    """Accept one source-bound pending→active candidate for an Experience."""

    def __init__(
        self,
        *,
        ledger,
        actor: str,
        source: str,
        content_store: ImmutableLifeContentStore | None = None,
    ) -> None:
        super().__init__(ledger=ledger, actor=actor, source=source)
        self._content_store = content_store

    def accept(
        self,
        *,
        experience: ExperienceProjection,
        transition: ExperienceTransitionProjection,
        experience_event: WorldEvent,
        experience_world_revision: int,
        draft: FactMemoryRetentionDraft,
        logical_time: datetime,
        created_at: datetime,
        trace_id: str,
        correlation_id: str,
    ) -> MemoryCandidateProjection | None:
        source = self._source_binding(
            experience=experience,
            transition=transition,
            experience_event=experience_event,
            experience_world_revision=experience_world_revision,
        )
        candidate_id = "memory:experience:" + _canonical_hash(source)
        projection = self._ledger.project()
        if any(
            item.candidate_id == candidate_id
            or source.authority_event_ref
            in {binding.authority_event_ref for binding in item.values.source_bindings}
            for item in projection.memory_candidates
        ):
            return None
        opened_event_id = f"event:memory:opened:{_digest(candidate_id)}"
        opened = self._candidate(
            candidate_id=candidate_id,
            source=source,
            draft=draft,
            privacy_ceiling=experience.values.privacy_class,
            entity_revision=1,
            status="pending",
            opened_at=logical_time,
            updated_at=logical_time,
            reviewed_at=None,
            accepted_event_ref=opened_event_id,
        )
        self._record_and_accept(
            after=opened,
            before=None,
            operation="open",
            logical_time=logical_time,
            created_at=created_at,
            trace_id=trace_id,
            correlation_id=correlation_id,
        )
        active = self._candidate(
            candidate_id=candidate_id,
            source=source,
            draft=draft,
            privacy_ceiling=experience.values.privacy_class,
            entity_revision=2,
            status="active",
            opened_at=opened.opened_at,
            updated_at=logical_time,
            reviewed_at=logical_time,
            accepted_event_ref=f"event:memory:accepted:{_digest(candidate_id)}",
        )
        self._record_and_accept(
            after=active,
            before=opened,
            operation="accept",
            logical_time=logical_time,
            created_at=created_at,
            trace_id=trace_id,
            correlation_id=correlation_id,
        )
        return active

    def _source_binding(
        self,
        *,
        experience: ExperienceProjection,
        transition: ExperienceTransitionProjection,
        experience_event: WorldEvent,
        experience_world_revision: int,
    ) -> MemorySourceBinding:
        if (
            experience_event.event_type != "ExperienceCommitted"
            or transition.experience_id != experience.experience_id
            or transition.entity_revision != experience.entity_revision
            or transition.values_after != experience.values
            or transition.accepted_event_ref != experience_event.event_id
            or experience_world_revision < 1
        ):
            raise ValueError("memory lifecycle requires one exact accepted Experience transition")
        projection = self._ledger.project()
        committed = next(
            (
                item
                for item in projection.committed_world_event_refs
                if item.event_id == experience_event.event_id
            ),
            None,
        )
        projected_transition = next(
            (
                item
                for item in projection.experience_transitions
                if item.transition_id == transition.transition_id
            ),
            None,
        )
        if (
            committed is None
            or committed.world_revision != experience_world_revision
            or committed.payload_hash != experience_event.payload_hash
            or projected_transition != transition
        ):
            raise ValueError("memory lifecycle Experience authority is no longer current")
        if self._content_store is not None:
            descriptor = next(
                (
                    item
                    for item in projection.life_content_descriptors
                    if isinstance(item, LifeContentDescriptorProjection)
                    and item.content_kind == "experience_summary"
                    and item.source_kind == "experience"
                    and item.source_entity_id == experience.experience_id
                    and item.source_entity_revision == experience.entity_revision
                    and item.source_event_ref == experience_event.event_id
                    and item.source_world_revision == committed.world_revision
                    and item.source_payload_hash == committed.payload_hash
                    and item.content_ref == experience.values.summary_ref
                    and item.content_payload_hash == experience.values.summary_payload_hash
                    and item.privacy_class == experience.values.privacy_class
                ),
                None,
            )
            if descriptor is None:
                raise ValueError(
                    "memory lifecycle Experience has no matching LifeContentRecorded descriptor"
                )
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
            stored = self._content_store.read_exact(content_ref=descriptor.content_ref)
            if descriptor_event is None or stored is None or (
                stored.content_kind != "experience_summary"
                or stored.content_payload_hash != descriptor.content_payload_hash
            ):
                raise ValueError(
                    "memory lifecycle Experience content sidecar is not source-bound"
                )
        return MemorySourceBinding(
            source_kind="experience",
            source_id=experience.experience_id,
            source_entity_revision=experience.entity_revision,
            authority_event_ref=experience_event.event_id,
            authority_world_revision=committed.world_revision,
            authority_payload_hash=committed.payload_hash,
            source_values_hash=_canonical_hash(transition.values_after),
        )


__all__ = ["ExperienceMemoryCandidateLifecycle"]

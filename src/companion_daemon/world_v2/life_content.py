"""Pinned, source-bound excerpts for settled lived-world content."""

from __future__ import annotations

from pydantic import Field

from .life_content_store import ImmutableLifeContentStore
from .schema_core import FrozenModel, PrivacyClass
from .schemas import LedgerProjection, ProjectionCursor


_PRIVACY_RANK = {"public": 0, "shareable": 1, "personal": 2, "private": 3, "withhold": 4}


class LifeContentBudget(FrozenModel):
    max_item_characters: int = Field(default=480, ge=1, le=12_000)
    max_total_characters: int = Field(default=1_440, ge=1, le=24_000)


class LifeContentExcerpt(FrozenModel):
    content_id: str = Field(min_length=1)
    content_kind: str = Field(min_length=1)
    content_ref: str = Field(min_length=1)
    content_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    text: str = Field(min_length=1)
    truncated: bool
    privacy_class: PrivacyClass
    source_entity_id: str = Field(min_length=1)
    source_entity_revision: int = Field(ge=1)
    authority_event_ref: str = Field(min_length=1)
    authority_world_revision: int = Field(ge=1)
    authority_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    descriptor_event_ref: str = Field(min_length=1)
    descriptor_world_revision: int = Field(ge=1)
    descriptor_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class LifeContentSuppression(FrozenModel):
    content_id: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class LifeContentResult(FrozenModel):
    settled_items: tuple[LifeContentExcerpt, ...]
    experience_items: tuple[LifeContentExcerpt, ...]
    suppressions: tuple[LifeContentSuppression, ...]


class LifeContentCompiler:
    """The sole Context read interface for sidecar-backed lived-world prose."""

    def __init__(self, *, store: ImmutableLifeContentStore | None) -> None:
        self._store = store

    def compile(
        self,
        *,
        cursor: ProjectionCursor,
        actor_ref: str,
        viewer_privacy_ceiling: PrivacyClass,
        budget: LifeContentBudget = LifeContentBudget(),
        projection: LedgerProjection,
    ) -> LifeContentResult:
        if (
            projection.world_revision != cursor.world_revision
            or projection.deliberation_revision != cursor.deliberation_revision
            or projection.ledger_sequence != cursor.ledger_sequence
        ):
            raise ValueError("life content projection does not match its pinned cursor")
        committed = {item.event_id: item for item in projection.committed_world_event_refs}
        owned_plans = {
            item.plan_id
            for item in projection.plans
            if item.owner_actor_ref == actor_ref and item.authority_origin is not None
        }
        occurrences = {item.occurrence_id: item for item in projection.world_occurrences}
        experiences = {
            item.experience_id: item
            for item in projection.experiences
            if hasattr(item, "origin")
        }
        candidate_rows: list[tuple[int, str, LifeContentExcerpt | None, LifeContentSuppression | None]] = []
        for descriptor in projection.life_content_descriptors:
            source = committed.get(descriptor.source_event_ref)
            descriptor_event = committed.get(descriptor.descriptor_event_ref)
            if source is None or descriptor_event is None or (
                source.world_revision != descriptor.source_world_revision
                or source.payload_hash != descriptor.source_payload_hash
                or descriptor_event.world_revision != descriptor.descriptor_world_revision
                or descriptor_event.payload_hash != descriptor.descriptor_payload_hash
                or descriptor_event.event_type != "LifeContentRecorded"
            ):
                candidate_rows.append((0, descriptor.content_id, None, LifeContentSuppression(content_id=descriptor.content_id, reason="source_proof_failed")))
                continue
            if _PRIVACY_RANK[descriptor.privacy_class] > _PRIVACY_RANK[viewer_privacy_ceiling] or descriptor.privacy_class == "withhold":
                candidate_rows.append((0, descriptor.content_id, None, LifeContentSuppression(content_id=descriptor.content_id, reason="privacy_ceiling")))
                continue
            if descriptor.source_kind == "occurrence_settlement":
                occurrence = occurrences.get(descriptor.source_entity_id)
                related = occurrence is not None and (
                    actor_ref in occurrence.participant_refs
                    or any(ref.removeprefix("plan:") in owned_plans for ref in occurrence.precondition_refs if ref.startswith("plan:"))
                )
                valid = related and occurrence is not None and occurrence.status == "settled" and (
                    occurrence.entity_revision == descriptor.source_entity_revision
                    and occurrence.settlement_event_ref == descriptor.source_event_ref
                    and occurrence.settlement_world_revision == descriptor.source_world_revision
                    and occurrence.settlement_payload_hash == descriptor.source_payload_hash
                    and occurrence.result_payload_ref == descriptor.content_ref
                    and occurrence.result_payload_hash == descriptor.content_payload_hash
                )
                rank = int(occurrence.settled_at.timestamp()) if valid and occurrence.settled_at else 0
            else:
                experience = experiences.get(descriptor.source_entity_id)
                related = experience is not None and actor_ref in experience.values.participant_refs
                valid = related and experience is not None and (
                    experience.entity_revision == descriptor.source_entity_revision
                    and experience.origin.accepted_event_ref == descriptor.source_event_ref
                    and experience.values.summary_ref == descriptor.content_ref
                    and experience.values.summary_payload_hash == descriptor.content_payload_hash
                )
                rank = int(experience.values.occurred_to.timestamp()) if valid else 0
            if not valid:
                candidate_rows.append((0, descriptor.content_id, None, LifeContentSuppression(content_id=descriptor.content_id, reason="not_related" if not related else "source_proof_failed")))
                continue
            stored = self._store.read_exact(content_ref=descriptor.content_ref) if self._store else None
            if stored is None:
                candidate_rows.append((rank, descriptor.content_id, None, LifeContentSuppression(content_id=descriptor.content_id, reason="content_missing")))
                continue
            if stored.content_kind != descriptor.content_kind or stored.content_payload_hash != descriptor.content_payload_hash:
                candidate_rows.append((rank, descriptor.content_id, None, LifeContentSuppression(content_id=descriptor.content_id, reason="hash_mismatch")))
                continue
            text = stored.text[: budget.max_item_characters]
            candidate_rows.append((rank, descriptor.content_id, LifeContentExcerpt(
                content_id=descriptor.content_id, content_kind=descriptor.content_kind, content_ref=descriptor.content_ref,
                content_payload_hash=descriptor.content_payload_hash, text=text, truncated=text != stored.text,
                privacy_class=descriptor.privacy_class, source_entity_id=descriptor.source_entity_id,
                source_entity_revision=descriptor.source_entity_revision, authority_event_ref=descriptor.source_event_ref,
                authority_world_revision=descriptor.source_world_revision, authority_payload_hash=descriptor.source_payload_hash,
                descriptor_event_ref=descriptor.descriptor_event_ref, descriptor_world_revision=descriptor.descriptor_world_revision,
                descriptor_payload_hash=descriptor.descriptor_payload_hash,
            ), None))
        remaining = budget.max_total_characters
        settled: list[LifeContentExcerpt] = []
        experiences_out: list[LifeContentExcerpt] = []
        suppressions: list[LifeContentSuppression] = []
        for _, _, item, suppression in sorted(candidate_rows, key=lambda row: (-row[0], row[1])):
            if suppression is not None:
                suppressions.append(suppression)
            elif item is not None:
                if remaining <= 0:
                    suppressions.append(LifeContentSuppression(content_id=item.content_id, reason="budget_exhausted"))
                else:
                    view = item.model_copy(update={"text": item.text[:remaining], "truncated": item.truncated or len(item.text) > remaining})
                    remaining -= len(view.text)
                    (settled if view.content_kind == "occurrence_result" else experiences_out).append(view)
        return LifeContentResult(settled_items=tuple(settled), experience_items=tuple(experiences_out), suppressions=tuple(suppressions))


__all__ = ["LifeContentBudget", "LifeContentCompiler", "LifeContentExcerpt", "LifeContentResult", "LifeContentSuppression"]

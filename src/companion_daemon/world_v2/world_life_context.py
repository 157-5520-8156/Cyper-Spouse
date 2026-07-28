"""Read-only, source-bound life context for a companion's settled world events.

``WorldOccurrenceProjection`` is ledger authority, but it is intentionally not
model input on its own.  This module is the narrow read seam between the two:
it selects only settled occurrences that can be attributed to the companion
through either an explicit participant reference or one of the companion-owned
plans used as a precondition.  It never turns an opaque result reference into
prose, and it never writes or advances an occurrence.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import Field, model_validator

from .biographical_lifecycle import BiographicalLifecycleCatalog
from .life_content import LifeContentCompiler, LifeContentExcerpt
from .schema_core import FrozenModel, PrivacyClass
from .schemas import LedgerProjection, ProjectionCursor


class WorldLifeSourceBinding(FrozenModel):
    """Exact settled-occurrence authority consumed by a Context item."""

    authority_event_ref: str = Field(min_length=1)
    authority_world_revision: int = Field(ge=1)
    authority_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class WorldLifeContextItem(FrozenModel):
    """Bounded model-visible facts about one settled companion life event.

    ``result_payload_ref`` is deliberately retained only as an opaque
    authority reference.  A later content-reader vertical may supply a
    separately hash-bound excerpt, but cannot silently promote this ref into
    a claimed narrative.
    """

    occurrence_id: str = Field(min_length=1)
    occurrence_entity_revision: int = Field(ge=1)
    participant_refs: tuple[str, ...] = Field(min_length=1)
    # ``None`` is an authoritative absence, not an invitation to infer where
    # the activity happened.
    location_ref: str | None = Field(default=None, min_length=1)
    result_id: str = Field(min_length=1)
    result_payload_ref: str = Field(min_length=1)
    result_payload_hash: str = Field(min_length=1)
    settled_at: datetime
    privacy_class: PrivacyClass
    source: WorldLifeSourceBinding
    content: LifeContentExcerpt | None = None


class ActiveLifeArcContext(FrozenModel):
    """The model-visible, non-narrative coordinates of one active Life Arc."""

    arc_id: str = Field(min_length=1)
    arc_kind: Literal[
        "academic",
        "employment",
        "residence",
        "travel",
        "personal",
        "dynamic",
    ]
    context_pack_ref: str = Field(min_length=1)
    context_tags: tuple[str, ...]
    started_at: datetime
    ends_at: datetime | None = None
    source_event_ref: str = Field(min_length=1)
    accepted_event_ref: str = Field(min_length=1)
    context_summary_ref: str | None = Field(default=None, min_length=1)
    context_summary_payload_hash: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    context_summary: str | None = Field(default=None, min_length=1, max_length=480)

    @model_validator(mode="after")
    def dynamic_summary_binding_is_complete(self) -> "ActiveLifeArcContext":
        values = (
            self.context_summary_ref,
            self.context_summary_payload_hash,
            self.context_summary,
        )
        if any(value is not None for value in values) and any(
            value is None for value in values
        ):
            raise ValueError("active Life Arc summary binding must be complete")
        return self


class BiographicalWorldContextItem(FrozenModel):
    """One source-bound reading of biography at the pinned Logical Time.

    Reviewed chronology and accepted Life Arcs provide facts, not behavioral
    instructions.  The item deliberately exposes context tags and Arc
    coordinates rather than generating prose about what the companion should
    say or do.
    """

    context_kind: Literal["biographical_context"] = "biographical_context"
    biography_id: str = Field(min_length=1)
    reviewed_timeline_ref: str = Field(min_length=1)
    timeline_source_event_ref: str = Field(min_length=1)
    logical_at: datetime
    age: int | None = Field(default=None, ge=0, le=150)
    academic_phase: str | None = None
    academic_year: int | None = Field(default=None, ge=1, le=12)
    season: Literal["spring", "summer", "autumn", "winter"]
    calendar_context_tags: tuple[str, ...]
    current_residence_context_tags: tuple[str, ...]
    active_life_arcs: tuple[ActiveLifeArcContext, ...]
    confidence_bp: Literal[10000] = 10_000
    privacy_class: PrivacyClass = "personal"
    source_bindings: tuple[WorldLifeSourceBinding, ...] = Field(min_length=1)


WorldLifeModelContextItem = WorldLifeContextItem | BiographicalWorldContextItem


class WorldLifeContextCompiler:
    """Compile settled world state, optionally attaching descriptor-bound prose."""

    def __init__(
        self,
        *,
        life_content: LifeContentCompiler | None = None,
        biography: BiographicalLifecycleCatalog | None = None,
        biography_timezone: ZoneInfo | None = None,
    ) -> None:
        self._life_content = life_content
        self._biography = biography
        self._biography_timezone = biography_timezone

    def compile(
        self,
        *,
        projection: LedgerProjection,
        actor_ref: str,
        cursor: ProjectionCursor | None = None,
        viewer_privacy_ceiling: PrivacyClass = "private",
        biographical_timeline_source: WorldLifeSourceBinding | None = None,
    ) -> tuple[WorldLifeModelContextItem, ...]:
        excerpts = {}
        if self._life_content is not None and cursor is not None:
            excerpts = {
                item.source_entity_id: item
                for item in self._life_content.compile(
                    cursor=cursor,
                    actor_ref=actor_ref,
                    viewer_privacy_ceiling=viewer_privacy_ceiling,
                    projection=projection,
                ).settled_items
            }
        owned_plan_ids = {
            plan.plan_id
            for plan in projection.plans
            if plan.owner_actor_ref == actor_ref and plan.authority_origin is not None
        }
        committed = {
            event.event_id: event for event in projection.committed_world_event_refs
        }
        items: list[WorldLifeContextItem] = []
        for occurrence in projection.world_occurrences:
            if occurrence.status != "settled":
                continue
            associated_by_plan = any(
                ref.removeprefix("plan:") in owned_plan_ids
                for ref in occurrence.precondition_refs
                if ref.startswith("plan:")
            )
            if actor_ref not in occurrence.participant_refs and not associated_by_plan:
                continue
            if (
                occurrence.settlement_event_ref is None
                or occurrence.settlement_world_revision is None
                or occurrence.settlement_payload_hash is None
                or occurrence.result_id is None
                or occurrence.result_payload_ref is None
                or occurrence.result_payload_hash is None
                or occurrence.settled_at is None
            ):
                # A partial settlement head is never useful model context.
                continue
            settlement = committed.get(occurrence.settlement_event_ref)
            if settlement is None or (
                settlement.event_type != "WorldOccurrenceSettled"
                or settlement.world_revision != occurrence.settlement_world_revision
                or settlement.payload_hash != occurrence.settlement_payload_hash
            ):
                # The owning ledger/reducer normally prevents this.  The
                # defensive read seam fails closed rather than accepting a
                # stale or substituted occurrence head.
                continue
            items.append(
                WorldLifeContextItem(
                    occurrence_id=occurrence.occurrence_id,
                    occurrence_entity_revision=occurrence.entity_revision,
                    participant_refs=tuple(sorted(occurrence.participant_refs)),
                    location_ref=occurrence.location_ref,
                    result_id=occurrence.result_id,
                    result_payload_ref=occurrence.result_payload_ref,
                    result_payload_hash=occurrence.result_payload_hash,
                    settled_at=occurrence.settled_at,
                    privacy_class=occurrence.visibility,
                    source=WorldLifeSourceBinding(
                        authority_event_ref=settlement.event_id,
                        authority_world_revision=settlement.world_revision,
                        authority_payload_hash=settlement.payload_hash,
                    ),
                    content=excerpts.get(occurrence.occurrence_id),
                )
            )
        biography = self._biographical_item(
            projection,
            timeline_source=biographical_timeline_source,
        )
        settled = tuple(
            sorted(items, key=lambda item: (-item.settled_at.timestamp(), item.occurrence_id))
        )
        return ((biography,) if biography is not None else ()) + settled

    def _biographical_item(
        self,
        projection: LedgerProjection,
        *,
        timeline_source: WorldLifeSourceBinding | None,
    ) -> BiographicalWorldContextItem | None:
        if (
            self._biography is None
            or projection.logical_time is None
            or timeline_source is None
        ):
            return None
        clocks = tuple(
            item
            for item in projection.committed_world_event_refs
            if item.event_type == "ClockAdvanced"
            and item.logical_time <= projection.logical_time
        )
        if not clocks:
            return None
        clock = max(
            clocks,
            key=lambda item: (
                item.logical_time,
                item.world_revision,
                item.event_id,
            ),
        )
        reading = self._biography.context_at(
            projection.logical_time,
            life_arcs=projection.life_arcs,
        )
        active_ids = set(reading.active_life_arc_ids)
        active_arcs = tuple(
            item for item in projection.life_arcs if item.arc_id in active_ids
        )
        committed = {
            item.event_id: item for item in projection.committed_world_event_refs
        }
        if (
            timeline_source.authority_event_ref not in committed
            or committed[timeline_source.authority_event_ref].event_type
            != "BiographicalTimelineConfigured"
            or committed[timeline_source.authority_event_ref].world_revision
            != timeline_source.authority_world_revision
            or committed[timeline_source.authority_event_ref].payload_hash
            != timeline_source.authority_payload_hash
        ):
            return None
        arc_refs = {item.source_event_ref for item in active_arcs}
        if any(
            ref not in committed
            or committed[ref].event_type != "WorldOccurrenceSettled"
            for ref in arc_refs
        ):
            return None
        accepted_arc_refs = {
            item.accepted_event_ref
            for item in active_arcs
            if item.accepted_event_ref is not None
        }
        if (
            len(accepted_arc_refs) != len(active_arcs)
            or any(
                ref not in committed
                or committed[ref].event_type != "LifeArcChanged"
                for ref in accepted_arc_refs
            )
            or any(
                item.accepted_event_ref is None
                or committed[item.accepted_event_ref].world_revision
                <= committed[item.source_event_ref].world_revision
                for item in active_arcs
            )
        ):
            return None
        source_refs = {clock.event_id, *arc_refs, *accepted_arc_refs}
        bindings = (timeline_source,) + tuple(
            WorldLifeSourceBinding(
                authority_event_ref=committed[ref].event_id,
                authority_world_revision=committed[ref].world_revision,
                authority_payload_hash=committed[ref].payload_hash,
            )
            for ref in sorted(source_refs)
        )
        if self._biography_timezone is None:
            return None
        local_month = reading.logical_at.astimezone(self._biography_timezone).month
        season: Literal["spring", "summer", "autumn", "winter"] = (
            "spring"
            if 3 <= local_month <= 5
            else "summer"
            if 6 <= local_month <= 8
            else "autumn"
            if 9 <= local_month <= 11
            else "winter"
        )
        return BiographicalWorldContextItem(
            biography_id=f"biography:{self._biography.document_hash}",
            reviewed_timeline_ref=(
                f"reviewed-biography:{self._biography.document_hash}"
            ),
            timeline_source_event_ref=timeline_source.authority_event_ref,
            logical_at=reading.logical_at,
            age=reading.age,
            academic_phase=reading.academic_phase,
            academic_year=reading.academic_year,
            season=season,
            calendar_context_tags=tuple(
                item
                for item in reading.context_tags
                if item.startswith(("academic:", "calendar:"))
            ),
            current_residence_context_tags=tuple(
                item
                for item in reading.context_tags
                if item.startswith("residence:")
            ),
            active_life_arcs=tuple(
                ActiveLifeArcContext(
                    arc_id=item.arc_id,
                    arc_kind=item.arc_kind,
                    context_pack_ref=item.context_pack_ref,
                    context_tags=item.context_tags,
                    started_at=item.started_at,
                    ends_at=item.ends_at,
                    source_event_ref=item.source_event_ref,
                    accepted_event_ref=item.accepted_event_ref,
                    **self._dynamic_arc_summary(
                        projection=projection,
                        arc=item,
                    ),
                )
                for item in active_arcs
                if item.accepted_event_ref is not None
            ),
            source_bindings=bindings,
        )

    def _dynamic_arc_summary(
        self,
        *,
        projection: LedgerProjection,
        arc,
    ) -> dict[str, str]:
        if (
            arc.arc_kind != "dynamic"
            or arc.effect_descriptor_hash is None
            or self._life_content is None
        ):
            return {}
        occurrence = next(
            (
                item
                for item in projection.world_occurrences
                if item.status == "settled"
                and item.settlement_event_ref == arc.source_event_ref
            ),
            None,
        )
        candidate = (
            next(
                (
                    item
                    for item in occurrence.candidate_outcomes
                    if item.candidate_result_ref
                    == occurrence.settled_outcome_ref
                ),
                None,
            )
            if occurrence is not None
            else None
        )
        descriptor = (
            candidate.dynamic_life_arc_context
            if candidate is not None
            else None
        )
        if (
            descriptor is None
            or descriptor.descriptor_hash != arc.effect_descriptor_hash
            or descriptor.summary_content_ref != arc.context_pack_ref
        ):
            return {}
        text = self._life_content.read_exact_bound_text(
            content_ref=descriptor.summary_content_ref,
            content_payload_hash=descriptor.summary_payload_hash,
            content_kind="dynamic_life_arc_context",
        )
        if text is None:
            return {}
        return {
            "context_summary_ref": descriptor.summary_content_ref,
            "context_summary_payload_hash": descriptor.summary_payload_hash,
            "context_summary": text,
        }


__all__ = [
    "ActiveLifeArcContext",
    "BiographicalWorldContextItem",
    "WorldLifeContextCompiler",
    "WorldLifeContextItem",
    "WorldLifeModelContextItem",
    "WorldLifeSourceBinding",
]

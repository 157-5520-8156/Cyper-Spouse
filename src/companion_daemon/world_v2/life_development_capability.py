"""Production Capability Manifest compilation for open life development."""

from __future__ import annotations

from datetime import timedelta

from .life_author_seed import ReviewedLifeSeedCatalog
from .life_development_draft import (
    LifeDevelopmentCapabilityManifest,
    LifeDevelopmentLocationCapability,
)
from .schemas import ProjectionCursor, WorldEvent


_CAPSULE_SLICE_NAMES = (
    "character_core",
    "current_situation",
    "recent_dialogue",
    "relationship_slice",
    "appraisals",
    "affect_episodes",
    "open_threads",
    "relevant_facts",
    "recent_experiences",
    "world_life",
    "perception_results",
    "active_memory_candidates",
    "available_capabilities",
    "action_budget",
    "private_impressions",
    "advisories",
)

_CURRENT_PRESENCE_AUTHORITY_HORIZON = timedelta(minutes=5)


class ProjectionLifeCapabilityManifestCompiler:
    """Expose pinned facts and affordances without choosing a story candidate."""

    def __init__(
        self,
        *,
        owner_actor_ref: str,
        catalog: ReviewedLifeSeedCatalog,
        max_future_days: int = 30,
        max_window_minutes: int = 12 * 60,
    ) -> None:
        if not owner_actor_ref:
            raise ValueError("life capability compiler requires an owner")
        self._owner = owner_actor_ref
        self._catalog = catalog
        self._max_future_days = max_future_days
        self._max_window_minutes = max_window_minutes

    def compile(
        self,
        *,
        projection: object,
        wake: object,
        capsule: object,
    ) -> LifeDevelopmentCapabilityManifest:
        if not isinstance(wake, WorldEvent):
            raise ValueError("life capability manifest requires an exact wake")
        model_content = getattr(capsule, "model_content_json", None)
        if not isinstance(model_content, str):
            raise ValueError("life capability manifest requires a pinned Context Capsule")

        committed_refs = tuple(
            item
            for item in getattr(projection, "committed_world_event_refs", ())
            if isinstance(getattr(item, "event_id", None), str)
        )
        committed_ids = {item.event_id for item in committed_refs}
        available_slices = tuple(
            value
            for name in _CAPSULE_SLICE_NAMES
            if (value := getattr(capsule, name, None)) is not None
            and getattr(value, "availability", None) == "available"
        )
        visible_refs = {
            ref
            for slice_ in available_slices
            for ref in getattr(slice_, "source_refs", ())
            if ref in committed_ids
        }
        # The exact scheduler wake is verified separately by the runtime and
        # remains a legal anchor even when compact Context framing omits its
        # identifier from prose.
        visible_refs.add(wake.event_id)
        if wake.event_id not in visible_refs:
            raise ValueError("life capability manifest wake is outside the pinned prefix")

        location_capabilities: list[LifeDevelopmentLocationCapability] = [
            LifeDevelopmentLocationCapability(
                location_ref=item.values.location_ref,
                privacy_class=item.values.privacy_class,
                availability_kind="current_presence",
                timezone_name=self._catalog.timezone_name,
                available_from=item.values.since,
                available_to=(
                    wake.logical_time + _CURRENT_PRESENCE_AUTHORITY_HORIZON
                ),
                authority_refs=tuple(
                    sorted(
                        {
                            item.origin.accepted_event_ref,
                            wake.event_id,
                        }
                    )
                ),
            )
            for item in getattr(projection, "locations", ())
            if getattr(item, "actor_ref", None) == self._owner
            and isinstance(
                getattr(getattr(item, "values", None), "location_ref", None),
                str,
            )
        ]
        biography = self._catalog.biographical_context_at(
            instant=wake.logical_time,
            life_arcs=tuple(getattr(projection, "life_arcs", ())),
        )
        location_capabilities.extend(
            LifeDevelopmentLocationCapability(
                location_ref=item.location_ref,
                privacy_class=item.privacy,
                availability_kind="reviewed_schedule",
                timezone_name=self._catalog.timezone_name,
                local_windows=item.local_windows,
                weekdays=item.weekdays,
                now_allowed=item.eligible_in_context(biography),
                authority_refs=(
                    "policy:life-author-catalog:"
                    + self._catalog.version
                    + ":"
                    + self._catalog.catalog_hash,
                ),
            )
            for item in self._catalog.reviewed_locations
            if item.eligible_in_context(biography)
            or item.future_entry_authorized
        )
        location_capabilities.extend(
            LifeDevelopmentLocationCapability(
                location_ref=location_ref,
                privacy_class=plan.privacy_class,
                availability_kind="accepted_plan",
                timezone_name=self._catalog.timezone_name,
                available_from=plan.scheduled_window.opens_at,
                available_to=plan.scheduled_window.closes_at,
                now_allowed=False,
                authority_refs=(
                    (
                        plan.authority_origin.accepted_event_ref
                        if plan.authority_origin is not None
                        else plan.plan_id
                    ),
                ),
            )
            for plan in getattr(projection, "plans", ())
            if getattr(plan, "owner_actor_ref", None) == self._owner
            and getattr(plan, "status", None) in {"planned", "active", "paused"}
            and getattr(plan, "scheduled_window", None) is not None
            and isinstance((location_ref := getattr(plan, "location_ref", None)), str)
            and location_ref
        )
        unique_location_capabilities = {
            item.model_dump_json(): item for item in location_capabilities
        }
        entity_refs = {
            f"npc:{item.npc_id}"
            for item in getattr(projection, "npcs", ())
            if getattr(item, "status", None) == "active"
            and isinstance(getattr(item, "npc_id", None), str)
        }
        grounding_refs = tuple(sorted(visible_refs))
        current_situation_refs = {
            ref
            for slice_ in available_slices
            if slice_ is getattr(capsule, "current_situation", None)
            for ref in getattr(slice_, "source_refs", ())
            if ref in committed_ids
        }
        active_plan_refs = {
            evidence.ref_id
            for plan in getattr(projection, "plans", ())
            if getattr(plan, "owner_actor_ref", None) == self._owner
            and getattr(plan, "status", None) in {"planned", "active", "paused"}
            for evidence in getattr(plan, "evidence_refs", ())
            if getattr(evidence, "ref_id", None) in committed_ids
        }
        anchor_refs = tuple(
            sorted(
                {
                    wake.event_id,
                    *current_situation_refs,
                    *active_plan_refs,
                }
            )
        )
        return LifeDevelopmentCapabilityManifest(
            version="life-development-capability.production.1",
            pinned_cursor=ProjectionCursor(
                world_revision=getattr(projection, "world_revision"),
                deliberation_revision=getattr(projection, "deliberation_revision"),
                ledger_sequence=getattr(projection, "ledger_sequence"),
            ),
            anchor_refs=anchor_refs,
            grounding_refs=grounding_refs,
            location_capabilities=tuple(
                sorted(
                    unique_location_capabilities.values(),
                    key=lambda item: (
                        item.location_ref,
                        item.availability_kind,
                        item.model_dump_json(),
                    ),
                )
            ),
            entity_refs=tuple(sorted(entity_refs)),
            max_future_days=self._max_future_days,
            max_window_minutes=self._max_window_minutes,
        )


__all__ = ["ProjectionLifeCapabilityManifestCompiler"]

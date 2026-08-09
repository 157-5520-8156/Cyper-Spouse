"""Source-closed NPC identity and social-history read model.

The registration reducer materializes an exact immutable promotion edge.  This
module turns that edge into model-visible identity without inventing a second
database or using opaque content references as names.  Missing or ambiguous
promotion closure and missing descriptor bytes fail closed.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import Field

from .life_content_store import ImmutableLifeContentStore
from .npc_relationship_view import (
    NpcRelationshipReading,
    SharedHistoryEvidence,
    npc_relationship_by_ref,
)
from .schema_core import FrozenModel
from .schemas import NpcSocialVariables


class NpcIdentityView(FrozenModel):
    npc_ref: str = Field(pattern=r"^npc:")
    lifecycle_state: str = Field(min_length=1)
    descriptor: str = Field(min_length=1, max_length=4_000)
    descriptor_content_ref: str = Field(min_length=1)
    descriptor_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    promotion_event_ref: str = Field(min_length=1)
    provisional_entity_ref: str | None = None
    first_occurrence_ref: str | None = None
    shared_experience_refs: tuple[str, ...] = ()
    shared_experience_summaries: tuple[str, ...] = ()
    active_plan_refs: tuple[str, ...] = ()
    current_location_ref: str | None = None
    protagonist_relationship: NpcRelationshipReading | None = None
    npc_relationship_to_protagonist: NpcSocialVariables | None = None
    inner_state: str | None = Field(default=None, max_length=4_000)
    goal_content_refs: tuple[str, ...] = ()
    goal_summaries: tuple[str, ...] = ()
    organization_refs: tuple[str, ...] = ()
    life_arc_refs: tuple[str, ...] = ()
    source_refs: tuple[str, ...] = Field(min_length=1)
    private_source_refs: tuple[str, ...] = ()
    last_shared_at: datetime | None = None
    shared_history_with_protagonist: SharedHistoryEvidence | None = None


def npc_identity_views(
    projection: object,
    *,
    content_store: ImmutableLifeContentStore,
    relationships: tuple[NpcRelationshipReading, ...] | None = None,
    shared_history: tuple[SharedHistoryEvidence, ...] | None = None,
    reviewed_identity_summaries: dict[str, str] | None = None,
    npc_refs: tuple[str, ...] | None = None,
) -> tuple[NpcIdentityView, ...]:
    """Resolve stable NPC identities from exact content and ledger sources."""

    relationship_by_ref = npc_relationship_by_ref(
        relationships if relationships is not None else ()
    )
    shared_history_by_ref = {
        item.npc_ref: item for item in (shared_history if shared_history is not None else ())
    }
    occurrences = tuple(getattr(projection, "world_occurrences", ()))
    experiences = tuple(getattr(projection, "experiences", ()))
    plans = tuple(getattr(projection, "plans", ()))
    all_npcs = tuple(getattr(projection, "npcs", ()))
    requested_refs = None if npc_refs is None else frozenset(npc_refs)
    npcs = tuple(
        item
        for item in all_npcs
        if requested_refs is None or f"npc:{item.npc_id}" in requested_refs
    )
    dynamic_edges = tuple(
        item.promotion_edge
        for item in all_npcs
        if getattr(item, "source_event_ref", None) is not None
        and getattr(item, "promotion_edge", None) is not None
    )
    duplicate_provisional_refs = {
        edge.provisional_entity_ref
        for edge in dynamic_edges
        if sum(item.provisional_entity_ref == edge.provisional_entity_ref for item in dynamic_edges)
        != 1
    }
    duplicate_origins = {
        (edge.origin_settlement_event_ref, edge.descriptor_hash)
        for edge in dynamic_edges
        if sum(
            (
                item.origin_settlement_event_ref,
                item.descriptor_hash,
            )
            == (edge.origin_settlement_event_ref, edge.descriptor_hash)
            for item in dynamic_edges
        )
        != 1
    }
    result: list[NpcIdentityView] = []
    for npc in npcs:
        npc_ref = f"npc:{npc.npc_id}"
        stable_identity_ref = getattr(npc, "stable_identity_ref", None)
        if not isinstance(stable_identity_ref, str) or not stable_identity_ref:
            continue
        stored = content_store.read_exact(content_ref=stable_identity_ref)
        if stored is None:
            continue
        elif stored.content_kind == "provisional_npc_introduction":
            descriptor = stored.text
            descriptor_hash = stored.content_payload_hash
        else:
            continue
        source_event_ref = getattr(npc, "source_event_ref", None)
        edge = getattr(npc, "promotion_edge", None)
        first = None
        provisional_entity_ref = None
        if source_event_ref is not None:
            if (
                edge is None
                or edge.provisional_entity_ref in duplicate_provisional_refs
                or (
                    edge.origin_settlement_event_ref,
                    edge.descriptor_hash,
                )
                in duplicate_origins
            ):
                continue
            matching_occurrences = tuple(
                item
                for item in occurrences
                if item.status == "settled"
                and item.settlement_event_ref == edge.origin_settlement_event_ref
            )
            if len(matching_occurrences) != 1:
                continue
            first = matching_occurrences[0]
            selected = tuple(
                item for item in first.candidate_outcomes if item.result_id == first.result_id
            )
            if len(selected) != 1:
                continue
            introductions = tuple(
                item
                for item in selected[0].provisional_npc_introductions
                if item.provisional_entity_ref == edge.provisional_entity_ref
                and item.summary_content_ref == edge.descriptor_content_ref
                and item.descriptor_hash == edge.descriptor_hash
                and item.summary_payload_hash == descriptor_hash
            )
            if len(introductions) != 1:
                continue
            provisional_entity_ref = edge.provisional_entity_ref
            promotion_ref = edge.registration_event_ref
        else:
            promotion_ref = getattr(npc, "registration_event_ref", None)
            if promotion_ref is None:
                continue
            first = next(
                (
                    item
                    for item in occurrences
                    if item.status == "settled" and npc_ref in item.participant_refs
                ),
                None,
            )
        shared_experiences = tuple(
            sorted(
                (
                    item
                    for item in experiences
                    if isinstance(
                        getattr(getattr(item, "origin", None), "accepted_event_ref", None),
                        str,
                    )
                    and (
                        npc_ref in getattr(getattr(item, "values", None), "participant_refs", ())
                        or (
                            provisional_entity_ref is not None
                            and provisional_entity_ref
                            in getattr(getattr(item, "values", None), "participant_refs", ())
                        )
                    )
                ),
                key=lambda item: item.experience_id,
            )
        )
        shared_experience_refs = tuple(item.experience_id for item in shared_experiences)
        shared_experience_summaries = tuple(
            stored_experience.text
            for item in shared_experiences
            if (
                (stored_experience := content_store.read_exact(content_ref=item.values.summary_ref))
                is not None
                and stored_experience.content_kind == "experience_summary"
                and stored_experience.content_payload_hash == item.values.summary_payload_hash
            )
        )[-4:]
        active_plan_refs = tuple(
            sorted(
                item.plan_id
                for item in plans
                if item.status in {"planned", "active", "paused"}
                and (item.owner_actor_ref == npc_ref or npc_ref in item.participant_refs)
            )
        )
        relationship = relationship_by_ref.get(npc_ref)
        subjective = getattr(npc, "subjective_state", None)
        inner_state = None
        if subjective is not None:
            inner_content = content_store.read_exact(content_ref=subjective.inner_state_content_ref)
            if (
                inner_content is not None
                and inner_content.content_kind == "npc_inner_state"
                and inner_content.content_payload_hash == subjective.inner_state_payload_hash
            ):
                inner_state = inner_content.text
        goal_summaries: list[str] = []
        valid_goal_refs: list[str] = []
        if subjective is not None and inner_state is not None:
            for ref, expected_hash in zip(
                subjective.goal_content_refs,
                subjective.goal_content_hashes,
                strict=True,
            ):
                goal = content_store.read_exact(content_ref=ref)
                if (
                    goal is not None
                    and goal.content_kind == "npc_goal"
                    and goal.content_payload_hash == expected_hash
                ):
                    valid_goal_refs.append(ref)
                    goal_summaries.append(goal.text)
        sources = tuple(
            dict.fromkeys(
                value
                for value in (
                    promotion_ref,
                    source_event_ref,
                    getattr(first, "settlement_event_ref", None),
                    *(relationship.source_event_refs if relationship is not None else ()),
                    *(item.origin.accepted_event_ref for item in shared_experiences),
                    *(
                        item.authority_origin.accepted_event_ref
                        for item in plans
                        if item.plan_id in active_plan_refs and item.authority_origin is not None
                    ),
                )
                if value
            )
        )
        private_source_refs = (
            subjective.source_event_refs
            if subjective is not None and inner_state is not None
            else ()
        )
        result.append(
            NpcIdentityView(
                npc_ref=npc_ref,
                lifecycle_state=("departed" if npc.status == "retired" else npc.status),
                descriptor=descriptor,
                descriptor_content_ref=stable_identity_ref,
                descriptor_payload_hash=descriptor_hash,
                promotion_event_ref=promotion_ref,
                provisional_entity_ref=provisional_entity_ref,
                first_occurrence_ref=(first.occurrence_id if first is not None else None),
                shared_experience_refs=shared_experience_refs,
                shared_experience_summaries=shared_experience_summaries,
                active_plan_refs=active_plan_refs,
                current_location_ref=getattr(npc, "current_location_ref", None),
                protagonist_relationship=relationship,
                shared_history_with_protagonist=shared_history_by_ref.get(npc_ref),
                npc_relationship_to_protagonist=(
                    subjective.relationship_to_subject
                    if subjective is not None and inner_state is not None
                    else None
                ),
                inner_state=inner_state,
                goal_content_refs=tuple(valid_goal_refs),
                goal_summaries=tuple(goal_summaries),
                organization_refs=(
                    subjective.organization_refs
                    if subjective is not None and inner_state is not None
                    else ()
                ),
                life_arc_refs=(
                    subjective.life_arc_refs
                    if subjective is not None and inner_state is not None
                    else ()
                ),
                source_refs=sources,
                private_source_refs=private_source_refs,
                last_shared_at=(relationship.last_shared_at if relationship else None),
            )
        )
    return tuple(sorted(result, key=lambda item: item.npc_ref))


__all__ = ["NpcIdentityView", "npc_identity_views"]

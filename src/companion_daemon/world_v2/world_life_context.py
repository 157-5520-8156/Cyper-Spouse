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

from pydantic import Field

from .schema_core import FrozenModel, PrivacyClass
from .schemas import LedgerProjection


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
    location_ref: str = Field(min_length=1)
    result_id: str = Field(min_length=1)
    result_payload_ref: str = Field(min_length=1)
    result_payload_hash: str = Field(min_length=1)
    settled_at: datetime
    privacy_class: PrivacyClass
    source: WorldLifeSourceBinding


class WorldLifeContextCompiler:
    """Compile bounded settled world state without an additional authority."""

    def compile(
        self, *, projection: LedgerProjection, actor_ref: str
    ) -> tuple[WorldLifeContextItem, ...]:
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
                )
            )
        return tuple(sorted(items, key=lambda item: (-item.settled_at.timestamp(), item.occurrence_id)))


__all__ = [
    "WorldLifeContextCompiler",
    "WorldLifeContextItem",
    "WorldLifeSourceBinding",
]

"""Strict payload contracts for the aspiration authority (低兑现度心愿).

Aspirations share the lived-world mutation discipline (``DomainMutationPayload``
with mandatory, reducer-verified evidence refs) rather than the commitment
authority: a Private Commitment is a dated responsibility whose reducer forces
its due window to resolve, while an aspiration has *no* due window and never
rots mechanically.  Reusing commitments would have required fabricating a fake
deadline and a fake fulfillment contract, corrupting both semantics.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import Field, model_validator

from .life_events import DomainMutationPayload
from .schemas import AspirationProjection


ASPIRATION_POLICY_REF = "policy:aspiration.1"


class AspirationPlantedPayload(DomainMutationPayload):
    aspiration: AspirationProjection

    @model_validator(mode="after")
    def creates_revision_one_active_wish(self) -> AspirationPlantedPayload:
        if self.expected_entity_revision != 0 or self.aspiration.entity_revision != 1:
            raise ValueError("AspirationPlanted must create entity revision one")
        if self.aspiration.status != "active":
            raise ValueError("AspirationPlanted requires an active aspiration")
        if self.aspiration.reinforcement_count != 0:
            raise ValueError("new aspiration cannot claim prior reinforcement")
        # The wish must name the exact committed material it grew out of; the
        # reducer separately proves that ref against committed authority.
        if self.aspiration.source_event_ref not in {
            item.ref_id for item in self.evidence_refs
        }:
            raise ValueError("aspiration source material must be bound as evidence")
        if not set(self.aspiration.tension_source_refs).issubset(
            {item.ref_id for item in self.evidence_refs}
        ):
            raise ValueError("aspiration tension sources must be bound as evidence")
        return self


class AspirationReinforcedPayload(DomainMutationPayload):
    aspiration_id: str = Field(min_length=1)
    reinforced_at: datetime
    reinforcement_evidence_ref: str = Field(min_length=1)

    @model_validator(mode="after")
    def reinforcement_material_is_bound(self) -> AspirationReinforcedPayload:
        if self.reinforcement_evidence_ref not in {
            item.ref_id for item in self.evidence_refs
        }:
            raise ValueError("aspiration reinforcement material must be bound as evidence")
        return self


class AspirationRevisedPayload(DomainMutationPayload):
    """One free, character-authored revision of an active direction."""

    aspiration_before: AspirationProjection
    aspiration_after: AspirationProjection

    @model_validator(mode="after")
    def revision_is_source_closed(self) -> "AspirationRevisedPayload":
        before = self.aspiration_before
        after = self.aspiration_after
        if (
            self.expected_entity_revision != before.entity_revision
            or after.entity_revision != before.entity_revision + 1
            or before.status != "active"
            or after.status != "active"
        ):
            raise ValueError("AspirationRevised must advance one active revision")
        immutable = (
            "aspiration_id",
            "owner_actor_ref",
            "seed_id",
            "origin_kind",
            "planted_at",
            "planted_event_ref",
            "source_event_ref",
            "last_reinforced_at",
            "reinforcement_count",
        )
        if any(getattr(before, name) != getattr(after, name) for name in immutable):
            raise ValueError("AspirationRevised changed immutable planting history")
        if after.last_revised_at is None or after.revision_event_ref is None:
            raise ValueError("AspirationRevised needs revision time and authority ref")
        changed = (
            before.text != after.text
            or before.privacy_class != after.privacy_class
            or before.tension_summary != after.tension_summary
            or before.tension_source_refs != after.tension_source_refs
        )
        if not changed:
            raise ValueError("AspirationRevised must change the character-owned direction")
        evidence = {item.ref_id for item in self.evidence_refs}
        if before.planted_event_ref not in evidence:
            raise ValueError("AspirationRevised must bind the planting authority")
        if not set(after.tension_source_refs).issubset(evidence):
            raise ValueError("aspiration tension sources must be bound as evidence")
        return self


class AspirationAbandonedPayload(DomainMutationPayload):
    """The character explicitly stops treating one aspiration as her direction."""

    aspiration_before: AspirationProjection
    aspiration_after: AspirationProjection

    @model_validator(mode="after")
    def abandonment_is_source_closed(self) -> "AspirationAbandonedPayload":
        before = self.aspiration_before
        after = self.aspiration_after
        if (
            self.expected_entity_revision != before.entity_revision
            or after.entity_revision != before.entity_revision + 1
            or before.status != "active"
            or after.status != "abandoned"
        ):
            raise ValueError("AspirationAbandoned must terminalize one active revision")
        mutable_terminal = {
            "entity_revision",
            "status",
            "abandoned_at",
            "abandonment_event_ref",
            "abandonment_summary",
            "abandonment_source_refs",
        }
        fields = AspirationProjection.model_fields
        if any(
            getattr(before, name) != getattr(after, name)
            for name in fields
            if name not in mutable_terminal
        ):
            raise ValueError("AspirationAbandoned changed non-terminal aspiration material")
        if after.abandoned_at is None or after.abandonment_event_ref is None:
            raise ValueError("AspirationAbandoned needs terminal time and authority ref")
        evidence = {item.ref_id for item in self.evidence_refs}
        if before.planted_event_ref not in evidence:
            raise ValueError("AspirationAbandoned must bind the planting authority")
        if not set(after.abandonment_source_refs).issubset(evidence):
            raise ValueError("aspiration abandonment sources must be bound as evidence")
        return self


class AspirationFadedPayload(DomainMutationPayload):
    aspiration_id: str = Field(min_length=1)
    faded_at: datetime


class AspirationCrystallizedPayload(DomainMutationPayload):
    """Phase-one interface only: no runtime emits this yet.

    When the crystallization lane lands, the aspiration's conditions have
    become concrete enough that a real calendar plan (``ActivityPlanned``)
    exists; this event closes the wish by pointing at that plan.
    """

    aspiration_id: str = Field(min_length=1)
    crystallized_at: datetime
    plan_ref: str = Field(min_length=1)

    @model_validator(mode="after")
    def plan_ref_is_canonical(self) -> AspirationCrystallizedPayload:
        if not self.plan_ref.startswith("plan:"):
            raise ValueError("aspiration crystallization must reference a plan ref")
        return self


ASPIRATION_PAYLOAD_MODELS = {
    "AspirationPlanted": AspirationPlantedPayload,
    "AspirationReinforced": AspirationReinforcedPayload,
    "AspirationRevised": AspirationRevisedPayload,
    "AspirationAbandoned": AspirationAbandonedPayload,
    "AspirationFaded": AspirationFadedPayload,
    "AspirationCrystallized": AspirationCrystallizedPayload,
}

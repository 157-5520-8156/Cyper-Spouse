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
    "AspirationFaded": AspirationFadedPayload,
    "AspirationCrystallized": AspirationCrystallizedPayload,
}

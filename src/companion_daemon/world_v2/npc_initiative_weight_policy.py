"""Pure replayable weighting retained from the retired scripted NPC lane.

Production NPC choices are authored by :mod:`npc_ecology`.  This module owns
only deterministic probability material used by historical analysis and
relationship tests; it has no model, ledger, scheduler, or commit port.
"""

from __future__ import annotations

from .life_author_seed import NpcInitiativeCandidate
from .mood_view import active_mood_intensities
from .npc_relationship_view import (
    RESTING_CLOSENESS_BP,
    NpcRelationshipReading,
    npc_relationship_by_ref,
)


NOTHING_CANDIDATE_REF = "nothing:npc-initiative"


class NpcInitiativeWeightPolicy:
    """Replayable probability mass from the historical NPC initiative lane."""

    version = "npc-initiative-weight.2"

    def compile(
        self,
        *,
        candidates: tuple[NpcInitiativeCandidate, ...],
        affect_episodes: tuple[object, ...] = (),
        npc_relationships: tuple[NpcRelationshipReading, ...] = (),
    ) -> dict[str, int]:
        mood = active_mood_intensities(affect_episodes)
        relationships = npc_relationship_by_ref(npc_relationships)
        weights: dict[str, int] = {}
        total = 0
        for candidate in candidates:
            multiplier = self._multiplier_bp(
                mood=mood,
                initiative_kind=candidate.event.initiative_kind,
                relationship=relationships.get(candidate.npc_ref),
            )
            mass = max(1, candidate.event.base_chance_bp * multiplier // 10_000)
            weights[candidate.token] = mass
            total += mass
        weights[NOTHING_CANDIDATE_REF] = max(10_000 - total, 0)
        return weights

    @staticmethod
    def _multiplier_bp(
        *,
        mood: dict[str, int],
        initiative_kind: str,
        relationship: NpcRelationshipReading | None = None,
    ) -> int:
        multiplier = 10_000
        if mood:
            warmth = mood.get("warmth", 0)
            loneliness = mood.get("loneliness", 0)
            unresolved = max(mood.get("resentment", 0), mood.get("anger", 0))
            multiplier += loneliness * 2_500 // 10_000
            if initiative_kind == "shared_time":
                multiplier += warmth * 2_000 // 10_000
            if initiative_kind == "small_favor":
                multiplier += warmth * 1_000 // 10_000
            if initiative_kind == "friction":
                multiplier += unresolved * 1_500 // 10_000
        if relationship is not None:
            closeness_delta = relationship.closeness_bp - RESTING_CLOSENESS_BP
            if initiative_kind in {"shared_time", "small_favor"}:
                multiplier += closeness_delta * 2_000 // 10_000
            if initiative_kind == "friction":
                multiplier -= max(0, closeness_delta) * 500 // 10_000
        return max(6_000, min(14_000, multiplier))


__all__ = ["NOTHING_CANDIDATE_REF", "NpcInitiativeWeightPolicy"]

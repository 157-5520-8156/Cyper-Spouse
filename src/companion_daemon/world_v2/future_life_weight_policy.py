"""Historical replay policy for catalog-authored future-opening draws.

The executable Future Life Author was retired by the CharacterInterior
cutover.  This module retains only the deterministic weight calculation used
to explain and test already-recorded draw evidence; it has no ledger, model,
or acceptance authority.
"""

from __future__ import annotations

from .life_author_seed import ReviewedLifeSeedFutureCandidate
from .mood_view import active_mood_intensities


class FutureLifeAuthorWeightPolicy:
    """Recompute the recorded ``future-life-author-weight.2`` preference mass."""

    version = "future-life-author-weight.2"

    def compile(
        self,
        *,
        candidates: tuple[ReviewedLifeSeedFutureCandidate, ...],
        affect_episodes: tuple[object, ...] = (),
    ) -> dict[str, int]:
        mood = active_mood_intensities(affect_episodes)
        weights: dict[str, int] = {}
        for candidate in candidates:
            mass = max(1_000, candidate.opening.importance_bp)
            mass = max(1, mass * candidate.context_fit_bp // 10_000)
            mass = max(
                1,
                mass * self._proximity_multiplier_bp(candidate.day_offset) // 10_000,
            )
            mass = max(
                1,
                mass
                * self._mood_multiplier_bp(
                    mood=mood,
                    candidate_domain=candidate.opening.domain,
                )
                // 10_000,
            )
            weights[candidate.token] = mass
        return weights

    @staticmethod
    def _proximity_multiplier_bp(day_offset: int) -> int:
        if day_offset <= 3:
            return 10_000
        if day_offset <= 5:
            return 8_500
        return 7_000

    @staticmethod
    def _mood_multiplier_bp(*, mood: dict[str, int], candidate_domain: str) -> int:
        if not mood:
            return 10_000
        heaviness = max(
            mood.get("sadness", 0),
            mood.get("hurt", 0),
            mood.get("anxiety", 0),
            mood.get("anger", 0),
            mood.get("resentment", 0),
        )
        loneliness = mood.get("loneliness", 0)
        brightness = max(mood.get("joy", 0), mood.get("warmth", 0))
        multiplier = 10_000
        if candidate_domain == "family_roommate_friend":
            multiplier += loneliness * 3_000 // 10_000
            multiplier -= max(0, heaviness - loneliness) * 3_000 // 10_000
        if candidate_domain in {"commute_walk", "creative_photo_writing"}:
            multiplier += brightness * 2_000 // 10_000
            multiplier -= heaviness * 1_500 // 10_000
        return max(6_500, min(13_500, multiplier))


__all__ = ["FutureLifeAuthorWeightPolicy"]

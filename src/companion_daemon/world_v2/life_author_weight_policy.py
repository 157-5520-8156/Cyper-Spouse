"""Replayable opportunity weighting retained after retiring LifeAuthorRuntime."""

from __future__ import annotations

from datetime import datetime, timedelta

from .change_phase_view import change_phase_by_dimension, change_phase_readings
from .life_author_seed import ReviewedLifeSeedCandidate


class LifeAuthorWeightPolicy:
    """Compile generic preference mass from committed projections.

    This policy changes only the recorded probability mass of environmental
    opportunities.  It neither chooses a character action nor owns a model.
    """

    version = "life-author-weight.5"

    def __init__(self, *, recent_window: timedelta = timedelta(days=7)) -> None:
        if recent_window <= timedelta(0):
            raise ValueError("life author recent window must be positive")
        self._recent_window = recent_window

    def compile(
        self,
        *,
        candidates: tuple[ReviewedLifeSeedCandidate, ...],
        plans: tuple[object, ...],
        logical_time: datetime,
        recent_domain_by_activity: dict[str, str] | None = None,
        affect_episodes: tuple[object, ...] = (),
    ) -> dict[str, int]:
        recent = tuple(
            plan for plan in plans if self._is_recent(plan=plan, logical_time=logical_time)
        )
        recent_social_count = sum(bool(getattr(plan, "participant_refs", ())) for plan in recent)
        previous_domain = self._latest_domain(
            recent=recent,
            domain_by_activity=recent_domain_by_activity or {},
        )
        mood = self._mood_intensities(affect_episodes)
        try:
            phases = change_phase_by_dimension(
                change_phase_readings(tuple(affect_episodes), logical_time=logical_time)
            )
        except (TypeError, ValueError):
            phases = {}
        weights: dict[str, int] = {}
        for candidate in candidates:
            same_kind_count = sum(
                getattr(plan, "activity_kind", None) == candidate.opening.activity_kind
                for plan in recent
            )
            mass = max(1_000, candidate.opening.importance_bp)
            mass = max(1, mass * candidate.daypart_fit_bp // 10_000)
            mass = max(1, mass * candidate.context_fit_bp // 10_000)
            mass = max(1, mass // (1 + same_kind_count))
            if candidate.participant_ref is not None and recent_social_count == 0:
                mass = max(1, mass * 3 // 2)
            mass = max(
                1,
                mass
                * self._rhythm_multiplier_bp(
                    previous_domain=previous_domain,
                    candidate_domain=candidate.opening.domain,
                )
                // 10_000,
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
            mass = max(
                1,
                mass
                * self._change_phase_multiplier_bp(
                    phases=phases,
                    candidate_domain=candidate.opening.domain,
                )
                // 10_000,
            )
            weights[candidate.token] = mass
        return weights

    @staticmethod
    def _mood_intensities(affect_episodes: tuple[object, ...]) -> dict[str, int]:
        intensities: dict[str, int] = {}
        for episode in affect_episodes:
            if getattr(episode, "status", None) != "active":
                continue
            for component in getattr(episode, "components", ()):  # type: ignore[attr-defined]
                dimension = str(getattr(component, "dimension", ""))
                intensity = getattr(component, "intensity_bp", 0)
                if isinstance(intensity, int) and 0 <= intensity <= 10_000:
                    intensities[dimension] = max(intensities.get(dimension, 0), intensity)
        return intensities

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
        restorative = {"rest_recovery", "sleep_wake", "digital_leisure"}
        demanding = {"study_class", "creative_photo_writing", "errand_household"}
        outgoing = {"commute_walk", "creative_photo_writing", "family_roommate_friend"}
        social = {"family_roommate_friend"}
        if candidate_domain in restorative:
            multiplier += heaviness * 3_500 // 10_000
        if candidate_domain in demanding:
            multiplier -= heaviness * 3_000 // 10_000
        if candidate_domain in outgoing:
            multiplier += brightness * 2_500 // 10_000
        if candidate_domain in social:
            multiplier += loneliness * 3_000 // 10_000
            multiplier -= max(0, heaviness - loneliness) * 1_500 // 10_000
        return max(4_000, min(16_000, multiplier))

    @staticmethod
    def _change_phase_multiplier_bp(*, phases: dict[str, str], candidate_domain: str) -> int:
        if not phases:
            return 10_000
        heavy = ("sadness", "hurt", "anxiety", "anger", "resentment", "loneliness")
        departing_heavy = any(phases.get(dimension) == "departing" for dimension in heavy)
        returning_heavy = (
            any(phases.get(dimension) in {"returning", "recovering"} for dimension in heavy)
            and not departing_heavy
        )
        restorative = {"rest_recovery", "sleep_wake", "digital_leisure"}
        demanding = {"study_class", "creative_photo_writing", "errand_household"}
        outgoing = {"commute_walk", "creative_photo_writing", "family_roommate_friend"}
        multiplier = 10_000
        if departing_heavy:
            if candidate_domain in restorative:
                multiplier += 1_500
            if candidate_domain in demanding or candidate_domain in outgoing:
                multiplier -= 1_500
        elif returning_heavy and (candidate_domain in demanding or candidate_domain in outgoing):
            multiplier += 1_200
        return max(8_000, min(12_000, multiplier))

    @staticmethod
    def _latest_domain(
        *,
        recent: tuple[object, ...],
        domain_by_activity: dict[str, str],
    ) -> str | None:
        ordered = sorted(
            recent,
            key=lambda plan: getattr(getattr(plan, "authority_origin", None), "accepted_at"),
            reverse=True,
        )
        for plan in ordered:
            domain = domain_by_activity.get(str(getattr(plan, "activity_kind", "")))
            if domain is not None:
                return domain
        return None

    @staticmethod
    def _rhythm_multiplier_bp(
        *,
        previous_domain: str | None,
        candidate_domain: str,
    ) -> int:
        focus = {"study_class", "creative_photo_writing"}
        restorative = {"commute_walk", "rest_recovery", "sleep_wake"}
        if previous_domain in focus and candidate_domain in restorative:
            return 12_500
        if previous_domain in focus and candidate_domain in focus:
            return 8_500
        if previous_domain in restorative and candidate_domain in focus:
            return 11_000
        return 10_000

    def _is_recent(self, *, plan: object, logical_time: datetime) -> bool:
        accepted_at = getattr(getattr(plan, "authority_origin", None), "accepted_at", None)
        return (
            isinstance(accepted_at, datetime)
            and accepted_at.tzinfo is not None
            and accepted_at.utcoffset() is not None
            and logical_time - self._recent_window <= accepted_at <= logical_time
        )


__all__ = ["LifeAuthorWeightPolicy"]

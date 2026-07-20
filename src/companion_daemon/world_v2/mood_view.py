"""Compact, model-safe prose view of accepted, active Affect state.

Behaviour lanes (activity lifecycle, outcome selection, media intent) need a
sense of "how she feels right now" without receiving episode IDs, refs, or
revisions.  This view renders only already-accepted feeling dimensions as
short prose; it is advisory colour for a bounded model choice, never a rule
or a second write path.
"""

from __future__ import annotations


MOOD_LABELS = {
    "hurt": "受伤", "anger": "生气", "sadness": "低落", "loneliness": "孤独",
    "anxiety": "不安", "resentment": "郁结", "warmth": "温暖", "joy": "愉快",
}

# Below this accepted intensity a feeling is background noise, not something
# the companion would consciously weigh while choosing what to do.
_NOTICEABLE_BP = 2_000


def active_mood_intensities(affect_episodes: tuple[object, ...]) -> dict[str, int]:
    """Aggregate active accepted Affect components to one bounded reading."""

    intensities: dict[str, int] = {}
    for episode in affect_episodes:
        if getattr(episode, "status", None) != "active":
            continue
        for component in getattr(episode, "components", ()):
            dimension = str(getattr(component, "dimension", ""))
            intensity = getattr(component, "intensity_bp", 0)
            if (
                dimension in MOOD_LABELS
                and isinstance(intensity, int)
                and 0 <= intensity <= 10_000
            ):
                intensities[dimension] = max(intensities.get(dimension, 0), intensity)
    return intensities


def mood_summary_prose(affect_episodes: tuple[object, ...]) -> str:
    """Render the strongest current feelings as one short advisory sentence.

    Returns an empty string when nothing rises above the noticeable floor, so
    callers can omit the line entirely instead of asserting calmness.
    """

    intensities = {
        dimension: value
        for dimension, value in active_mood_intensities(affect_episodes).items()
        if value >= _NOTICEABLE_BP
    }
    if not intensities:
        return ""
    strongest = sorted(intensities.items(), key=lambda item: (-item[1], item[0]))[:3]
    described = "、".join(
        f"{MOOD_LABELS[dimension]}({'强' if value >= 6_000 else '中' if value >= 4_000 else '轻'})"
        for dimension, value in strongest
    )
    return f"她此刻可感的情绪：{described}。"


__all__ = ["MOOD_LABELS", "active_mood_intensities", "mood_summary_prose"]

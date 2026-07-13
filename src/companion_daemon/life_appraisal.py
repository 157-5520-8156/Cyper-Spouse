"""Deterministic cognitive appraisal for already committed life outcomes."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Mapping, Sequence


_RESTORATIVE_APPRAISALS = frozenset(
    {
        "social_warmth",
        "family_connection",
        "creative_satisfaction",
        "restorative_solitude",
    }
)
_NEGATIVE_LIFE_APPRAISALS = frozenset({"npc_conflict", "goal_strain"})
_MAX_CONTEXT_SOURCES = 8


@dataclass(frozen=True)
class LifeAppraisalContext:
    """Bounded, event-sourced context for a settled life outcome.

    The context deliberately contains references and counts only.  It is
    derived from the world projection at the outcome's logical time, so a
    replay reaches the same appraisal without depending on an LLM summary or
    wall-clock history.
    """

    recurrence_count: int = 0
    unresolved_related_count: int = 0
    restorative_context: int = 0
    source_event_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in (
            "recurrence_count",
            "unresolved_related_count",
            "restorative_context",
        ):
            value = getattr(self, name)
            if not 0 <= value <= 3:
                raise ValueError(f"{name} must be between 0 and 3")
        if len(self.source_event_ids) > _MAX_CONTEXT_SOURCES or any(
            not item or len(item) > 160 for item in self.source_event_ids
        ):
            raise ValueError("life appraisal source_event_ids are outside their bound")

    def payload(self) -> dict[str, object]:
        return {
            "recurrence_count": self.recurrence_count,
            "unresolved_related_count": self.unresolved_related_count,
            "restorative_context": self.restorative_context,
            "source_event_ids": list(self.source_event_ids),
        }


@dataclass(frozen=True)
class LifeOutcomeAppraisal:
    agency: str
    certainty: int
    goal_congruence: int
    controllability: int
    norm_compatibility: int
    power_delta: int
    relationship_value: int
    salience: int
    social_exposure: int
    context: LifeAppraisalContext = LifeAppraisalContext()
    rule_version: str = "life-appraisal-v3"

    def payload(self) -> dict[str, object]:
        return asdict(self)


def appraise_committed_life_outcome(
    outcome: Mapping[str, object],
    *,
    needs: Mapping[str, object],
    npc_relationship: Mapping[str, object],
    goal_importance: int,
    context: LifeAppraisalContext | None = None,
) -> LifeOutcomeAppraisal:
    """Interpret one settled fact through current, likewise sourced context."""
    kind = str(outcome.get("appraisal") or "")
    intensity = _bounded(outcome.get("intensity"), 50)
    energy = _bounded(needs.get("energy"), 50)
    security = _bounded(needs.get("security"), 50)
    relationship_value = _bounded(npc_relationship.get("closeness"), 0)
    importance = _bounded(goal_importance, 0)
    context = context or LifeAppraisalContext()
    positive = kind in {
        "social_warmth",
        "family_connection",
        "goal_progress",
        "goal_completed",
        "creative_satisfaction",
        "restorative_solitude",
    }
    negative = kind in {"npc_conflict", "goal_strain"}
    if positive:
        goal_congruence = min(100, 25 + intensity // 2 + importance // 4)
    elif negative:
        goal_congruence = max(-100, -(20 + intensity // 3 + importance // 2))
    else:
        goal_congruence = 0
    controllability = max(
        5,
        min(95, 20 + energy // 2 + security // 4 + context.restorative_context * 3),
    )
    norm_compatibility = (
        -65 - min(15, context.recurrence_count * 4 + context.unresolved_related_count * 3)
        if kind == "npc_conflict"
        else 20
        if positive
        else -20
        if negative
        else 0
    )
    salience = min(
        100,
        max(
            1,
            intensity
            + (100 - energy) // 5
            + relationship_value // 5
            + importance // 5
            + context.recurrence_count * 9
            + context.unresolved_related_count * 5
            - context.restorative_context * 8,
        ),
    )
    if negative:
        goal_congruence = max(
            -100,
            goal_congruence - context.recurrence_count * 6 - context.unresolved_related_count * 4,
        )
    return LifeOutcomeAppraisal(
        agency="npc" if outcome.get("npc_id") else "situation",
        certainty=100,
        goal_congruence=goal_congruence,
        controllability=controllability,
        norm_compatibility=norm_compatibility,
        power_delta=-20 if kind == "npc_conflict" and security < 40 else 0,
        relationship_value=relationship_value,
        salience=salience,
        social_exposure=35 if outcome.get("npc_id") else 0,
        context=context,
    )


def derive_life_appraisal_context(
    outcome: Mapping[str, object],
    *,
    prior_outcomes: Mapping[str, object],
    experiences: Mapping[str, object],
    active_episodes: Sequence[object],
) -> LifeAppraisalContext:
    """Derive recurrence and regulation context from committed world facts.

    An earlier outcome qualifies as recurrent only when both the appraisal and
    the concrete NPC/goal target match.  Active episodes are intentionally
    used only for unresolved same-target harm and recent restorative support.
    This makes life spillover explainable without attributing world events to
    the current user.
    """
    appraisal = str(outcome.get("appraisal") or "")
    npc_id = str(outcome.get("npc_id") or "")
    goal_id = str(outcome.get("goal_id") or "")
    target = _target(npc_id=npc_id, goal_id=goal_id)
    current_source = str(outcome.get("source_reference") or outcome.get("outcome_id") or "")

    recurrent_sources: list[str] = []
    for raw_experience in experiences.values():
        experience = raw_experience if isinstance(raw_experience, Mapping) else {}
        source = str(experience.get("source_outcome_id") or "")
        if not source or source == current_source:
            continue
        historical = prior_outcomes.get(source)
        historical = historical if isinstance(historical, Mapping) else {}
        if (
            str(experience.get("affect_appraisal") or "") == appraisal
            and str(historical.get("npc_id") or "") == npc_id
            and str(historical.get("goal_id") or "") == goal_id
        ):
            recurrent_sources.append(source)

    unresolved_sources: list[str] = []
    restorative_sources: list[str] = []
    for raw_episode in active_episodes:
        episode = raw_episode if isinstance(raw_episode, Mapping) else {}
        source = str(episode.get("source_reference") or "")
        if not source or source == current_source:
            continue
        episode_target = str(episode.get("target") or "")
        if (
            target != "world"
            and episode_target == target
            and int(episode.get("valence") or 0) < 0
            and str(episode.get("appraisal") or "") in _NEGATIVE_LIFE_APPRAISALS
        ):
            unresolved_sources.append(source)
        if (
            int(episode.get("valence") or 0) > 0
            and str(episode.get("appraisal") or "") in _RESTORATIVE_APPRAISALS
        ):
            restorative_sources.append(source)

    # Sorted de-duplication prevents incidental dict insertion order from
    # changing event payloads during replay/import.
    sources = tuple(
        sorted(
            {current_source, *recurrent_sources, *unresolved_sources, *restorative_sources} - {""}
        )
    )
    return LifeAppraisalContext(
        recurrence_count=min(3, len(set(recurrent_sources))),
        unresolved_related_count=min(3, len(set(unresolved_sources))),
        restorative_context=min(3, len(set(restorative_sources))),
        source_event_ids=sources[:_MAX_CONTEXT_SOURCES],
    )


def _target(*, npc_id: str, goal_id: str) -> str:
    if npc_id:
        return f"npc:{npc_id}"
    if goal_id:
        return f"goal:{goal_id}"
    return "world"


def _bounded(value: object, default: int) -> int:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return default
    return max(0, min(100, int(value)))

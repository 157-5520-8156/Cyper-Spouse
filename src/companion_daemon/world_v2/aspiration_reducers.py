"""Pure lifecycle reducers for the aspiration authority.

Structural law only lives here: identity, revision compare-and-swap, and the
legal status graph (active → reinforced* → faded | crystallized).  *When* a
wish may fade (idle days) or how likely planting is are runtime policy owned
by ``AspirationRuntime`` and its recorded RandomAuthority draws, deliberately
kept out of the reducer so replay never re-litigates probability.
"""

from __future__ import annotations

from datetime import datetime

from .aspiration_events import (
    AspirationCrystallizedPayload,
    AspirationFadedPayload,
    AspirationPlantedPayload,
    AspirationReinforcedPayload,
)
from .schemas import AspirationProjection, PlanStateProjection


def plant_aspiration(
    aspirations: tuple[AspirationProjection, ...],
    payload: AspirationPlantedPayload,
    *,
    event_ref: str,
    logical_time: datetime,
) -> tuple[AspirationProjection, ...]:
    aspiration = payload.aspiration
    if any(item.aspiration_id == aspiration.aspiration_id for item in aspirations):
        raise ValueError(f"aspiration {aspiration.aspiration_id!r} already exists")
    # One reviewed seed grows at most one wish per owner, ever (phase one):
    # a faded wish stays quietly faded instead of respawning every check.
    if any(
        item.seed_id == aspiration.seed_id
        and item.owner_actor_ref == aspiration.owner_actor_ref
        for item in aspirations
    ):
        raise ValueError("aspiration seed was already planted for this owner")
    if aspiration.planted_at != logical_time:
        raise ValueError("aspiration planting must be pinned to authoritative logical time")
    if aspiration.planted_event_ref != event_ref:
        raise ValueError("aspiration planted event ref does not identify its mutation event")
    return (*aspirations, aspiration)


def reinforce_aspiration(
    aspirations: tuple[AspirationProjection, ...],
    payload: AspirationReinforcedPayload,
    *,
    logical_time: datetime,
) -> tuple[AspirationProjection, ...]:
    index, current = _aspiration(aspirations, payload.aspiration_id)
    _expect_revision(current.entity_revision, payload.expected_entity_revision)
    if current.status != "active":
        raise ValueError("only an active aspiration can be reinforced")
    if payload.reinforced_at != logical_time:
        raise ValueError("aspiration reinforcement must be pinned to authoritative logical time")
    updated = current.model_copy(
        update={
            "entity_revision": current.entity_revision + 1,
            "last_reinforced_at": payload.reinforced_at,
            "reinforcement_count": current.reinforcement_count + 1,
        }
    )
    return _replace(aspirations, index, updated)


def fade_aspiration(
    aspirations: tuple[AspirationProjection, ...],
    payload: AspirationFadedPayload,
    *,
    logical_time: datetime,
) -> tuple[AspirationProjection, ...]:
    index, current = _aspiration(aspirations, payload.aspiration_id)
    _expect_revision(current.entity_revision, payload.expected_entity_revision)
    if current.status != "active":
        raise ValueError("only an active aspiration can fade")
    if payload.faded_at != logical_time:
        raise ValueError("aspiration fade must be pinned to authoritative logical time")
    updated = current.model_copy(
        update={
            "entity_revision": current.entity_revision + 1,
            "status": "faded",
            "faded_at": payload.faded_at,
        }
    )
    return _replace(aspirations, index, updated)


def crystallize_aspiration(
    aspirations: tuple[AspirationProjection, ...],
    plans: tuple[PlanStateProjection, ...],
    payload: AspirationCrystallizedPayload,
    *,
    logical_time: datetime,
) -> tuple[AspirationProjection, ...]:
    index, current = _aspiration(aspirations, payload.aspiration_id)
    _expect_revision(current.entity_revision, payload.expected_entity_revision)
    if current.status != "active":
        raise ValueError("only an active aspiration can crystallize")
    if payload.crystallized_at != logical_time:
        raise ValueError("aspiration crystallization must be pinned to authoritative logical time")
    plan_id = payload.plan_ref.removeprefix("plan:")
    if not any(plan.plan_id == plan_id for plan in plans):
        raise ValueError("aspiration crystallization references an unknown plan")
    updated = current.model_copy(
        update={
            "entity_revision": current.entity_revision + 1,
            "status": "crystallized",
            "crystallized_plan_ref": payload.plan_ref,
        }
    )
    return _replace(aspirations, index, updated)


def _aspiration(
    aspirations: tuple[AspirationProjection, ...], aspiration_id: str
) -> tuple[int, AspirationProjection]:
    for index, item in enumerate(aspirations):
        if item.aspiration_id == aspiration_id:
            return index, item
    raise ValueError(f"aspiration {aspiration_id!r} does not exist")


def _expect_revision(actual: int, expected: int) -> None:
    if actual != expected:
        raise ValueError(f"stale aspiration revision: expected {expected}, current {actual}")


def _replace(
    values: tuple[AspirationProjection, ...],
    index: int,
    value: AspirationProjection,
) -> tuple[AspirationProjection, ...]:
    return (*values[:index], value, *values[index + 1 :])

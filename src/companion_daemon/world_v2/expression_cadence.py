"""Versioned, replay-stable timing policy for recorded expression beats."""

from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
from typing import Literal

from pydantic import Field

from .schema_core import FrozenModel


CadenceProfile = Literal["rapid", "conversational", "hesitant", "escalating"]
CADENCE_POLICY_VERSION = "expression-cadence.1"
_EXPIRY_SLACK_SECONDS = 120.0


class CadenceDraw(FrozenModel):
    """The replay material needed from one recorded RandomAuthority draw."""

    draw_ref: str = Field(min_length=1, max_length=256)
    beat_position: int = Field(ge=2, le=8)
    fraction_ppm: int = Field(ge=0, le=1_000_000)
    policy_version: Literal["expression-cadence.1"] = CADENCE_POLICY_VERSION


def cadence_gap_bounds(
    *, profile: CadenceProfile, beat_position: int, beat_count: int
) -> tuple[float, float]:
    """Return frozen v1 bounds for the gap immediately before one beat."""

    if not 2 <= beat_position <= beat_count <= 8:
        raise ValueError("cadence position must identify a subsequent beat")
    if profile == "rapid":
        return 0.35, 1.1
    if profile == "conversational":
        return 0.8, 2.5
    if profile == "hesitant":
        return 2.0, 7.0
    # Start close, then increasingly leave room for interruption.  Bounds,
    # rather than emotion labels, are policy: Affect and Relationship only
    # advise the model's profile selection.
    progress = (beat_position - 2) / max(1, beat_count - 2)
    return 0.45 + 1.55 * progress, 1.2 + 3.8 * progress


def cadence_windows(
    *,
    origin: datetime,
    profile: CadenceProfile,
    beat_count: int,
    draws: tuple[CadenceDraw, ...],
) -> tuple[tuple[datetime, datetime] | None, ...]:
    """Expand recorded fractions into cumulative absolute Action due windows."""

    if origin.tzinfo is None or origin.utcoffset() is None:
        raise ValueError("cadence origin must be timezone-aware")
    if not 1 <= beat_count <= 8:
        raise ValueError("cadence supports one to eight beats")
    if beat_count == 1:
        if draws:
            raise ValueError("a single beat cannot consume cadence draws")
        return (None,)
    if tuple(item.beat_position for item in draws) != tuple(range(2, beat_count + 1)):
        raise ValueError("cadence draws must exactly cover subsequent beats in order")
    if any(item.policy_version != CADENCE_POLICY_VERSION for item in draws):
        raise ValueError("cadence draw policy version is not replayable here")

    cursor = origin
    windows: list[tuple[datetime, datetime] | None] = [None]
    for draw in draws:
        lower, upper = cadence_gap_bounds(
            profile=profile,
            beat_position=draw.beat_position,
            beat_count=beat_count,
        )
        gap = lower + (upper - lower) * draw.fraction_ppm / 1_000_000
        cursor += timedelta(seconds=gap)
        windows.append((cursor, cursor + timedelta(seconds=_EXPIRY_SLACK_SECONDS)))
    return tuple(windows)


def record_cadence_draws(
    *,
    authority,
    attempt_id: str,
    beat_count: int,
    logical_time: datetime,
    actor: str,
    trace_id: str,
    correlation_id: str,
) -> tuple[CadenceDraw, ...]:
    """Record the bounded fractions once; replay callers reuse returned values."""

    if not 1 <= beat_count <= 8:
        raise ValueError("cadence supports one to eight beats")
    if beat_count == 1:
        return ()
    candidates = tuple(f"cadence-vector:{index:02d}" for index in range(64))
    recorded = authority.draw(
        attempt_id=f"{attempt_id}:cadence",
        candidate_refs=candidates,
        catalog_version=CADENCE_POLICY_VERSION,
        logical_time=logical_time,
        actor=actor,
        trace_id=trace_id,
        correlation_id=correlation_id,
    )
    draws: list[CadenceDraw] = []
    for position in range(2, beat_count + 1):
        fraction = int(
            hashlib.sha256(
                f"{recorded.seed_hash}:{recorded.selected_candidate_ref}:{position}".encode()
            ).hexdigest(),
            16,
        ) % 1_000_001
        draws.append(
            CadenceDraw(
                draw_ref=recorded.draw_id,
                beat_position=position,
                fraction_ppm=fraction,
            )
        )
    return tuple(draws)


__all__ = [
    "CADENCE_POLICY_VERSION",
    "CadenceDraw",
    "CadenceProfile",
    "cadence_gap_bounds",
    "cadence_windows",
    "record_cadence_draws",
]

"""Observed-time conversation heat shared by transport and world attention."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Mapping


@dataclass(frozen=True)
class ConversationCadence:
    heat: str
    observed_gap_seconds: float | None
    alternating_turns: int
    reason: str
    profile_version: str = "rhythm-v1"


@dataclass(frozen=True)
class RhythmProfile:
    """Versioned observed-time thresholds with exit hysteresis."""

    version: str = "rhythm-v1"
    hot_enter_seconds: float = 90.0
    hot_exit_seconds: float = 120.0
    warm_enter_seconds: float = 600.0
    warm_exit_seconds: float = 720.0
    max_future_skew_seconds: float = 5.0


@dataclass(frozen=True)
class FrozenTurnContext:
    """Immutable adapter-owned observation context for one complete turn."""

    turn_id: str
    world_id: str | None
    user_id: str
    observed_at: datetime
    cadence: ConversationCadence

    def usage_dimensions(self) -> dict[str, str]:
        return {
            "turn_id": self.turn_id,
            "world_id": self.world_id or "",
            "cadence": self.cadence.heat,
            "rhythm_profile": self.cadence.profile_version,
        }


DEFAULT_RHYTHM_PROFILE = RhythmProfile()


def derive_conversation_cadence(
    state: Mapping[str, object],
    *,
    user_id: str,
    observed_at: datetime,
    previous_heat: str | None = None,
    profile: RhythmProfile = DEFAULT_RHYTHM_PROFILE,
) -> ConversationCadence:
    """Classify chat heat from observed transport time, never virtual life time."""
    recent: list[tuple[str, datetime]] = []
    raw_messages = state.get("recent_messages")
    if isinstance(raw_messages, list):
        for raw in raw_messages:
            if not isinstance(raw, Mapping):
                continue
            item_user = str(raw.get("user_id") or "")
            if item_user and item_user != user_id:
                continue
            direction = str(raw.get("direction") or "")
            observed = str(raw.get("observed_at") or "")
            if direction not in {"in", "out"} or not observed:
                continue
            try:
                at = datetime.fromisoformat(observed)
            except ValueError:
                continue
            recent.append((direction, at))
    if not recent:
        return ConversationCadence("cold", None, 0, "no_recent_delivered_exchange", profile.version)
    timestamps = [item[1] for item in recent]
    if any(
        (at - observed_at).total_seconds() > profile.max_future_skew_seconds for at in timestamps
    ):
        return ConversationCadence("cold", None, 0, "future_observation", profile.version)
    if any(later < earlier for earlier, later in zip(timestamps, timestamps[1:])):
        return ConversationCadence("cold", None, 0, "out_of_order_observation", profile.version)
    recent.sort(key=lambda item: item[1])
    gap = max(0.0, (observed_at - recent[-1][1]).total_seconds())
    alternating = 1
    for index in range(len(recent) - 1, 0, -1):
        if recent[index][0] == recent[index - 1][0]:
            break
        alternating += 1
    hot_limit = profile.hot_exit_seconds if previous_heat == "hot" else profile.hot_enter_seconds
    if gap <= hot_limit and alternating >= 2:
        reason = (
            "hot_hysteresis"
            if previous_heat == "hot" and gap > profile.hot_enter_seconds
            else "active_back_and_forth"
        )
        return ConversationCadence("hot", gap, alternating, reason, profile.version)
    warm_limit = (
        profile.warm_exit_seconds
        if previous_heat in {"hot", "warm"}
        else profile.warm_enter_seconds
    )
    if gap <= warm_limit:
        return ConversationCadence("warm", gap, alternating, "recent_conversation", profile.version)
    return ConversationCadence("cold", gap, alternating, "observed_gap_exceeded", profile.version)


def freeze_turn_context(
    state: Mapping[str, object],
    *,
    user_id: str,
    observed_at: datetime,
    turn_id: str,
    world_id: str | None = None,
    previous_heat: str | None = None,
    profile: RhythmProfile = DEFAULT_RHYTHM_PROFILE,
) -> FrozenTurnContext:
    """Freeze wall-clock observation and its derived cadence once at the adapter seam."""
    return FrozenTurnContext(
        turn_id=turn_id,
        world_id=world_id,
        user_id=user_id,
        observed_at=observed_at,
        cadence=derive_conversation_cadence(
            state,
            user_id=user_id,
            observed_at=observed_at,
            previous_heat=previous_heat,
            profile=profile,
        ),
    )

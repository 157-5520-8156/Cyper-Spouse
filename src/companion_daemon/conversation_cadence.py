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


def derive_conversation_cadence(
    state: Mapping[str, object], *, user_id: str, observed_at: datetime
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
        return ConversationCadence("cold", None, 0, "no_recent_delivered_exchange")
    recent.sort(key=lambda item: item[1])
    gap = max(0.0, (observed_at - recent[-1][1]).total_seconds())
    alternating = 1
    for index in range(len(recent) - 1, 0, -1):
        if recent[index][0] == recent[index - 1][0]:
            break
        alternating += 1
    if gap <= 90 and alternating >= 2:
        return ConversationCadence("hot", gap, alternating, "active_back_and_forth")
    if gap <= 600:
        return ConversationCadence("warm", gap, alternating, "recent_conversation")
    return ConversationCadence("cold", gap, alternating, "observed_gap_exceeded")

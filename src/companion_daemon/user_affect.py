"""Logical-time lifetime and turn relevance for inferred user affect.

User affect is a fallible reading of one interaction, not a permanent trait of
the user.  Keeping these rules independent from wall time makes replay and
simulation deterministic while preventing a stale repair posture from leaking
into unrelated conversations.
"""
from __future__ import annotations

from datetime import datetime, timedelta
import re
from typing import Mapping


_TTL_BY_INTENSITY = {
    2: timedelta(days=3),
    3: timedelta(days=7),
    4: timedelta(days=14),
}
_IMMEDIATE_REPAIR_WINDOW = timedelta(hours=12)
_RELATIONAL_MARKERS = (
    "你",
    "刚才",
    "刚刚",
    "上次",
    "之前",
    "敷衍",
    "冷淡",
    "失望",
    "没接住",
    "不开心",
    "不高兴",
    "没懂",
    "不懂",
    "什么意思",
    "解释",
    "算了",
    "没事",
    "好多了",
)


def expiry_for_user_affect(*, intensity: int, appraised_at: datetime) -> datetime:
    """Return the bounded logical lifetime for one unresolved episode."""
    return appraised_at + _TTL_BY_INTENSITY.get(
        max(2, min(4, int(intensity))), timedelta(days=3)
    )


def active_user_affect_for_turn(
    affect: Mapping[str, object] | None,
    *,
    logical_at: datetime | None,
    message_text: str,
) -> dict[str, object]:
    """Select a live, relevant affect episode for this particular turn.

    An unresolved episode can remain in the ledger long enough for a user to
    explicitly return to it, but it only steers ordinary generation during the
    immediate repair window or when the current input is relationally relevant.
    Legacy records without logical metadata remain readable until they are
    naturally superseded, preserving old event logs without inventing a wall
    clock migration.
    """
    if not affect:
        return {}
    episodes = affect.get("active_episodes")
    candidates = (
        [item for item in episodes if isinstance(item, Mapping)]
        if isinstance(episodes, list)
        else [affect]
    )
    live = [
        dict(item)
        for item in candidates
        if bool(item.get("unresolved"))
        and str(item.get("kind") or "") in {"disappointment", "confusion"}
        and not _expired(item, logical_at)
        and _relevant_to_turn(item, logical_at, message_text)
    ]
    if not live:
        return {}
    return max(live, key=_episode_order)


def _expired(episode: Mapping[str, object], logical_at: datetime | None) -> bool:
    if logical_at is None:
        return False
    expires_at = _parse_datetime(episode.get("expires_at"))
    return expires_at is not None and expires_at <= logical_at


def _relevant_to_turn(
    episode: Mapping[str, object], logical_at: datetime | None, message_text: str
) -> bool:
    appraised_at = _parse_datetime(episode.get("appraised_logical_at"))
    # Records produced before the logical-lifetime version have no trustworthy
    # logical anchor. Keep them compatible rather than deriving one from wall
    # time or changing historical replay.
    if logical_at is None or appraised_at is None:
        return True
    if logical_at - appraised_at <= _IMMEDIATE_REPAIR_WINDOW:
        return True
    compact = re.sub(r"\s+", "", message_text)
    return any(marker in compact for marker in _RELATIONAL_MARKERS)


def _episode_order(episode: Mapping[str, object]) -> tuple[str, str]:
    return (
        str(episode.get("appraised_logical_at") or ""),
        str(episode.get("source_message_id") or ""),
    )


def _parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None

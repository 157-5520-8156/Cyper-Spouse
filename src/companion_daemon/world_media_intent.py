"""Pure selection of a human-like reason for a world-grounded photo."""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import Literal


PhotoIntent = Literal[
    "atmosphere_record", "check_in_pose", "companion_candid", "playful_share",
    "outfit_mirror", "unfiltered_moment", "private_keepsake",
]


@dataclass(frozen=True)
class WorldPhotoIntent:
    intent: PhotoIntent
    reason: str


class WorldMediaIntentPolicy:
    """Choose a bounded photo intention from settled world facts only."""

    def choose(self, snapshot: dict[str, object], *, request_id: str) -> WorldPhotoIntent | None:
        media = snapshot.get("media")
        if isinstance(media, dict) and any(
            isinstance(item, dict) and item.get("status") in {"requested", "generated"}
            for item in media.values()
        ):
            return None
        activity = _active_activity(snapshot)
        if not activity:
            return None
        template_id = str(activity.get("template_id") or "")
        companions = activity.get("companions") or snapshot.get("current_companions") or ()
        if isinstance(companions, (list, tuple)) and companions:
            return WorldPhotoIntent("companion_candid", "registered_companion_present")
        candidates: tuple[PhotoIntent, ...]
        if template_id in {"photo_portfolio", "photo_review_disagreement", "family_bookstore_call"}:
            candidates = ("check_in_pose", "atmosphere_record", "playful_share")
        elif template_id in {"campus_walk", "literature_reading", "course_notes"}:
            candidates = ("atmosphere_record", "check_in_pose", "playful_share")
        else:
            candidates = ("atmosphere_record", "check_in_pose")
        index = int.from_bytes(sha256(request_id.encode("utf-8")).digest()[:4], "big") % len(candidates)
        return WorldPhotoIntent(candidates[index], "active_world_activity")


def _active_activity(snapshot: dict[str, object]) -> dict[str, object]:
    agenda = snapshot.get("agenda")
    if not isinstance(agenda, dict):
        return {}
    active = [item for item in agenda.values() if isinstance(item, dict) and item.get("status") == "active"]
    return dict(sorted(active, key=lambda item: str(item.get("activity_id") or ""))[0]) if active else {}

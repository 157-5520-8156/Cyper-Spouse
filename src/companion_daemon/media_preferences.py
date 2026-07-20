"""User-controlled preferences for proactive personal media."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class MediaPreferences:
    allow_proactive_images: bool = True
    allow_unfiltered_media: bool = True
    unfiltered_cooldown_days: int = 7


def load_media_preferences(store, canonical_user_id: str) -> MediaPreferences:
    row = store.latest_memory(canonical_user_id, kind="media_preferences")
    if row is None:
        return MediaPreferences()
    try:
        raw = json.loads(str(row["content"]))
        return MediaPreferences(
            allow_proactive_images=bool(raw.get("allow_proactive_images", True)),
            allow_unfiltered_media=bool(raw.get("allow_unfiltered_media", True)),
            unfiltered_cooldown_days=max(7, min(30, int(raw.get("unfiltered_cooldown_days", 7)))),
        )
    except (TypeError, ValueError, json.JSONDecodeError):
        return MediaPreferences()


def update_media_preferences_from_text(text: str, current: MediaPreferences) -> MediaPreferences | None:
    normalized = text.replace(" ", "")
    if any(token in normalized for token in ("关闭主动照片", "不要主动发图", "别主动发照片")):
        return MediaPreferences(False, current.allow_unfiltered_media, current.unfiltered_cooldown_days)
    if any(token in normalized for token in ("开启主动照片", "可以主动发图", "允许主动照片")):
        return MediaPreferences(True, current.allow_unfiltered_media, current.unfiltered_cooldown_days)
    if any(token in normalized for token in ("不要发丑照", "关闭非精致照", "别发狼狈照")):
        return MediaPreferences(current.allow_proactive_images, False, current.unfiltered_cooldown_days)
    if any(token in normalized for token in ("可以发丑照", "开启非精致照", "允许非精致照")):
        return MediaPreferences(current.allow_proactive_images, True, current.unfiltered_cooldown_days)
    if any(token in normalized for token in ("照片少一点", "图片少一点", "低频发图")):
        return MediaPreferences(current.allow_proactive_images, current.allow_unfiltered_media, 14)
    if any(token in normalized for token in ("照片正常", "默认发图频率")):
        return MediaPreferences(current.allow_proactive_images, current.allow_unfiltered_media, 7)
    return None


def persist_media_preferences(store, canonical_user_id: str, preferences: MediaPreferences, *, source: str) -> None:
    store.upsert_memory(
        canonical_user_id,
        kind="media_preferences",
        content=json.dumps(asdict(preferences), sort_keys=True),
        source=source,
        confidence=1.0,
    )

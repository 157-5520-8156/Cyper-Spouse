from companion_daemon.db import CompanionStore
from companion_daemon.engine import seed_user
from companion_daemon.media_preferences import (
    MediaPreferences,
    load_media_preferences,
    persist_media_preferences,
    update_media_preferences_from_text,
)


def test_media_preferences_can_disable_proactive_images_and_unfiltered_media(tmp_path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    current = load_media_preferences(store, "geoff")
    disabled = update_media_preferences_from_text("不要主动发图，也不要发丑照", current)

    assert disabled is not None
    assert disabled.allow_proactive_images is False
    assert disabled.allow_unfiltered_media is True

    disabled_unfiltered = update_media_preferences_from_text("不要发丑照", disabled)
    assert disabled_unfiltered is not None
    persist_media_preferences(store, "geoff", disabled_unfiltered, source="test")
    assert load_media_preferences(store, "geoff") == MediaPreferences(False, False, 7)


def test_media_preferences_support_a_lower_frequency_mode() -> None:
    updated = update_media_preferences_from_text("照片少一点", MediaPreferences())

    assert updated is not None
    assert updated.unfiltered_cooldown_days == 14

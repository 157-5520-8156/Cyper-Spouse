from companion_daemon.stickers import load_stickers
from companion_daemon.models import IncomingMessage, MoodState
from companion_daemon.reply_stickers import choose_reply_sticker


def test_sticker_catalog_selects_by_mood() -> None:
    catalog = load_stickers("configs/stickers.yaml")

    sticker = catalog.choose("miss_you")

    assert sticker is not None
    assert sticker.category == "miss_you"
    assert str(sticker.path).endswith("rin-miss-you.png")


def test_sulking_mood_does_not_attach_sticker_to_every_ordinary_reply() -> None:
    catalog = load_stickers("configs/stickers.yaml")

    ordinary = choose_reply_sticker(
        catalog,
        MoodState(mood="sulking", emotional_charge=60),
        IncomingMessage(platform="qq", platform_user_id="geoff", text="你中午吃什么"),
    )
    tone_related = choose_reply_sticker(
        catalog,
        MoodState(mood="sulking", emotional_charge=60),
        IncomingMessage(platform="qq", platform_user_id="geoff", text="干嘛这个语气"),
    )

    assert ordinary is None
    assert tone_related is not None

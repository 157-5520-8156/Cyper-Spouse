from companion_daemon.stickers import load_stickers


def test_sticker_catalog_selects_by_mood() -> None:
    catalog = load_stickers("configs/stickers.yaml")

    sticker = catalog.choose("miss_you")

    assert sticker is not None
    assert sticker.category == "miss_you"
    assert str(sticker.path).endswith("rin-miss-you.png")

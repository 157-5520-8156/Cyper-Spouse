from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel

from companion_daemon.models import Mood


class Sticker(BaseModel):
    id: str
    category: str
    mood: Mood
    intent: str
    path: Path


class StickerCatalog(BaseModel):
    stickers: list[Sticker]

    def choose(self, mood: Mood, intent: str | None = None) -> Sticker | None:
        effective_mood = _sticker_mood(mood)
        if intent:
            for sticker in self.stickers:
                if sticker.mood == effective_mood and sticker.intent == intent:
                    return sticker
        for sticker in self.stickers:
            if sticker.mood == effective_mood:
                return sticker
        return self.stickers[0] if self.stickers else None


def _sticker_mood(mood: Mood) -> Mood:
    return {
        "guarded": "sulking",
        "hurt": "sulking",
        "affectionate": "happy",
        "curious": "calm",
    }.get(mood, mood)


@lru_cache
def load_stickers(path: str) -> StickerCatalog:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return StickerCatalog(**data)

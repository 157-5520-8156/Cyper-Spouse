import re

from companion_daemon.models import MoodState


def split_reply_text(text: str, state: MoodState) -> list[str]:
    cleaned = text.strip()
    if not cleaned:
        return []
    if len(cleaned) <= 28 or state.mood in {"hurt", "guarded"}:
        return [cleaned]
    parts = _sentence_parts(cleaned)
    if len(parts) <= 1:
        parts = _split_by_comma(cleaned)
    max_parts = 3 if state.mood in {"happy", "affectionate", "miss_you", "curious"} else 2
    result = parts[:max_parts]
    if len(parts) > max_parts:
        result[-1] = "".join([result[-1], *parts[max_parts:]])
    return result or [cleaned]


def _split_by_comma(text: str) -> list[str]:
    if len(text) < 46:
        return [text]
    parts = [part.strip() for part in re.split(r"(?<=[，,])", text) if part.strip()]
    if len(parts) <= 1:
        return [text]
    return parts


def _sentence_parts(text: str) -> list[str]:
    matches = re.findall(r"[^。！？!?]+[。！？!?]?", text)
    return [match.strip() for match in matches if match.strip()]

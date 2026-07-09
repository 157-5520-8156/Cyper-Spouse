import re

_STAGE_DIRECTION_RE = re.compile(r"[（(][^（）()]{1,80}[）)]")
_ASTERISK_ACTION_RE = re.compile(r"\*[^*]{1,80}\*")


def sanitize_chat_text(text: str) -> str:
    """Remove roleplay-style stage directions from IM replies."""
    cleaned_lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if _looks_like_pure_stage_direction(stripped):
            continue
        stripped = _STAGE_DIRECTION_RE.sub("", stripped)
        stripped = _ASTERISK_ACTION_RE.sub("", stripped)
        stripped = re.sub(r"\s{2,}", " ", stripped).strip()
        if stripped:
            cleaned_lines.append(stripped)
    return "\n".join(cleaned_lines).strip()


def _looks_like_pure_stage_direction(text: str) -> bool:
    return (
        (text.startswith("（") and text.endswith("）"))
        or (text.startswith("(") and text.endswith(")"))
        or (text.startswith("*") and text.endswith("*"))
    )

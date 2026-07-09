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
            cleaned_lines.append(_soften_assistantese(stripped))
    return _limit_questions("\n".join(cleaned_lines).strip())


def _looks_like_pure_stage_direction(text: str) -> bool:
    return (
        (text.startswith("（") and text.endswith("）"))
        or (text.startswith("(") and text.endswith(")"))
        or (text.startswith("*") and text.endswith("*"))
    )


def _soften_assistantese(text: str) -> str:
    text = re.sub(r"^(我理解你的意思[，,。]?\s*)", "", text)
    text = re.sub(r"^(听起来你(?:是|有点)?[^，。！？]{0,18}[，,。]\s*)", "", text)
    text = re.sub(r"(这个问题(?:确实)?(?:很|挺)?(?:有意思|重要)[，,。]\s*)", "", text)
    text = re.sub(r"(我有(?:个|一个)?(?:朋友|同学|室友)[^。！？]{0,36}(?:。|！|？)?)", "", text)
    return text.strip()


def _limit_questions(text: str) -> str:
    question_marks = [match.start() for match in re.finditer(r"[？?]", text)]
    if len(question_marks) <= 1:
        return text
    cutoff = question_marks[1]
    tail = re.sub(r"[？?]", "。", text[cutoff:])
    return text[:cutoff] + tail

import re

_STAGE_DIRECTION_RE = re.compile(r"[（(][^（）()]{1,80}[）)]")
_ASTERISK_ACTION_RE = re.compile(r"\*[^*]{1,80}\*")

_ASSISTANTESE_PATTERNS = [
    re.compile(r"^(我理解你的意思[，,。]?\s*)"),
    re.compile(r"^(听起来你(?:是|有点)?[^，。！？]{0,18}[，,。]\s*)"),
    re.compile(r"(这个问题(?:确实)?(?:很|挺)?(?:有意思|重要)[，,。]\s*)"),
    re.compile(r"(我有(?:个|一个)?(?:朋友|同学|室友)[^。！？]{0,36}(?:。|！|？)?)"),
    re.compile(r"^(说实话[，,]?\s*)"),
    re.compile(r"^(讲真[，,]?\s*)"),
    re.compile(r"^(其实吧[，,]?\s*)"),
    re.compile(r"^(不得不说[，,]?\s*)"),
    re.compile(r"^(不得不说的是[，,]?\s*)"),
    re.compile(r"^(有趣的是[，,]?\s*)"),
    re.compile(r"^(值得一提的是[，,]?\s*)"),
    re.compile(r"^(忍不住想说[，,]?\s*)"),
    re.compile(r"^(作为(?:一个)?(?:过来人|朋友)[，,]?\s*)"),
    re.compile(r"^(总的来说[，,]?\s*)"),
    re.compile(r"^(总的来说[，,]?\s*说[，,]?\s*)"),
    re.compile(r"(不过话说回来[，,]?\s*)"),
    re.compile(r"^(说到这个[，,]?\s*)"),
    re.compile(r"^(说到这儿[，,]?\s*)"),
    re.compile(r"^(你知道(?:吗|的)[，,]?\s*)"),
    re.compile(r"^(让我想想[，,。]?\s*)"),
    re.compile(r"^(嗯[，,]?\s*让我想想[，,。]?\s*)"),
    re.compile(r"^(哈哈[，,]?\s*这个问题[，,]?\s*)"),
    re.compile(r"^(好问题[！!]?[，,]?\s*)"),
    re.compile(r"^(这是个好问题[。！]?[，,]?\s*)"),
]


def sanitize_chat_text(text: str) -> str:
    """Remove roleplay-style stage directions and AI-ish patterns from IM replies."""
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
    for pattern in _ASSISTANTESE_PATTERNS:
        text = pattern.sub("", text)
    return text.strip()


def _limit_questions(text: str) -> str:
    """Keep at most one question mark; convert extras to periods."""
    question_marks = list(re.finditer(r"[？?]", text))
    if len(question_marks) <= 1:
        return text
    result = list(text)
    for match in question_marks[1:]:
        result[match.start()] = "。"
    return "".join(result)

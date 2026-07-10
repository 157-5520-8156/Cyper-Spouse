import re
from dataclasses import dataclass

from companion_daemon.models import IncomingMessage, MessageAttachment


@dataclass(frozen=True)
class ExtractedMemory:
    kind: str
    content: str
    confidence: float = 0.7


@dataclass(frozen=True)
class MemoryCandidate:
    text: str
    kind: str
    label: str
    confidence: float = 0.68


def extract_memories(message: IncomingMessage) -> list[ExtractedMemory]:
    text = message.text.strip()
    memories: list[ExtractedMemory] = []

    patterns = [
        ("name", r"(?:我叫|叫我)([^，。！？\n]{1,16})"),
        ("preference", r"我(?:喜欢|爱)([^，。！？\n]{1,32})"),
        ("dislike", r"我(?:不喜欢|讨厌)([^，。！？\n]{1,32})"),
        ("status", r"我是([^，。！？\n]{1,24})"),
        ("schedule", r"我(?:明天|后天|今晚|今天|周末)(?:要|准备|打算|得|需要|计划)([^，。！？\n]{1,40})"),
    ]
    for kind, pattern in patterns:
        for match in re.finditer(pattern, text):
            value = match.group(1).strip()
            if value:
                memories.append(ExtractedMemory(kind=kind, content=value))

    for candidate in detect_memory_candidates(text):
        memories.append(
            ExtractedMemory(
                kind=candidate.kind,
                content=candidate.text,
                confidence=candidate.confidence,
            )
        )

    for attachment in message.attachments:
        summary = memory_from_attachment(attachment)
        if summary:
            memories.append(summary)

    return memories


MEMORY_CANDIDATE_PATTERNS: list[tuple[str, str, str, list[str]]] = [
    (
        "life_fact",
        "Life Fact",
        "custom",
        [
            r"我(?:住在|搬到|出生在|人在|在)([^，。！？\n]{2,32})",
            r"我是(?:一名|一个|个)?([^，。！？\n]{2,32}(?:学生|老师|程序员|工程师|设计师|医生|律师|作家|摄影师|研究生))",
            r"我(?:在|就读于|毕业于)([^，。！？\n]{2,40})",
            r"我家(?:在|住在)([^，。！？\n]{2,32})",
        ],
    ),
    (
        "favorite_thing",
        "Favorite Thing",
        "favorite_thing",
        [
            r"我(?:最近|一直|平时)?(?:最喜欢|特别喜欢|一直喜欢|超喜欢)([^，。！？\n]{2,40})",
            r"(?:最近|一直|平时)(?:最喜欢|特别喜欢|超喜欢)([^，。！？\n]{2,40})",
            r"我(?:不喜欢|讨厌|受不了)([^，。！？\n]{2,40})",
            r"我的(?:最爱|本命|白月光)是([^，。！？\n]{2,40})",
        ],
    ),
    (
        "hobby",
        "Hobby / Interest",
        "hobby",
        [
            r"我(?:最近|一直|平时)?(?:喜欢|爱|沉迷|经常)(?:玩|看|听|读|写|拍|做)?([^，。！？\n]{3,40})",
            r"我(?:最近| lately)?(?:在学|开始学|练习)([^，。！？\n]{2,40})",
        ],
    ),
    (
        "person",
        "Important Person",
        "person",
        [
            r"我的(?:朋友|同学|室友|老板|同事|妈妈|爸爸|姐姐|妹妹|哥哥|弟弟)([^，。！？\n]{0,24})",
            r"([^，。！？\n]{2,10})(?:刚刚|昨天|今天)?(?:跟我说|给我发|问我|约我)",
        ],
    ),
    (
        "shared_moment",
        "Shared Moment",
        "shared_moment",
        [
            r"(?:还记得|记不记得|上次|那次|昨天我们|之前我们)([^。！？\n]{6,80})",
            r"我一直记得([^。！？\n]{6,80})",
        ],
    ),
    (
        "recent_event",
        "Recent Event",
        "shared_moment",
        [
            r"我(?:刚刚|刚|最近|今天|昨天)(?:去了|买了|开始|结束|搬了|换了|决定|发现)([^。！？\n]{4,80})",
            r"我(?:明天|后天|这周|下周|周末)(?:要|准备|打算)([^。！？\n]{4,80})",
        ],
    ),
]


def detect_memory_candidates(text: str, *, max_candidates: int = 3) -> list[MemoryCandidate]:
    if not text or len(text.strip()) < 4:
        return []
    candidates: list[MemoryCandidate] = []
    seen: set[str] = set()
    for _, label, kind, patterns in MEMORY_CANDIDATE_PATTERNS:
        if len(candidates) >= max_candidates:
            break
        for pattern in patterns:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if not match:
                continue
            match_text = match.group(0).strip()[:120]
            if len(match_text) < 4 or match_text in seen:
                continue
            if label == "Important Person" and _looks_like_pronoun_person_noise(match_text):
                continue
            seen.add(match_text)
            candidates.append(MemoryCandidate(text=match_text, kind=kind, label=label))
            break
    return candidates


def memory_from_attachment(attachment: MessageAttachment) -> ExtractedMemory | None:
    if attachment.kind == "image":
        detail = attachment.filename or attachment.content_type or "一张图片"
        return ExtractedMemory("shared_image", f"用户发过图片：{detail}", confidence=0.4)
    if attachment.kind == "audio":
        detail = attachment.filename or attachment.content_type or "一段语音"
        return ExtractedMemory("shared_audio", f"用户发过语音：{detail}", confidence=0.4)
    if attachment.kind == "file":
        detail = attachment.filename or attachment.content_type or "一个文件"
        return ExtractedMemory("shared_file", f"用户发过文件：{detail}", confidence=0.4)
    return None


def _exclude_from_reply_memory(kind: str) -> bool:
    return kind in {
        "life_continuity",
        "tone_inertia",
        "inner_subtext",
        "proactive_response",
        "withheld_proactive_impulse",
        "own_question_answered",
        "own_question_skipped",
        "afterthought_blocked",
        "memory_maintenance_blocked",
        "image_request_blocked",
        "proactive_image_blocked",
        "consolidation_log",
        "interaction_pattern",
    }


def _looks_like_pronoun_person_noise(text: str) -> bool:
    return text.startswith(("你", "我", "他", "她", "它", "这", "那"))

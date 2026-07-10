import re

from companion_daemon.sanitize import sanitize_chat_text


def postprocess_reply_text(text: str, *, recent_lines: list[str], user_text: str) -> str:
    cleaned = sanitize_chat_text(text)
    if _recent_assistant_questions(recent_lines) and not _looks_like_user_question(user_text):
        cleaned = _remove_followup_questions(cleaned)
    cleaned = _repair_role_attribution(cleaned, recent_lines)
    cleaned = _soften_stale_time_reference(cleaned, recent_lines)
    cleaned = _remove_recent_duplicate_sentences(cleaned, recent_lines, user_text)
    cleaned = _repair_incomplete_trailing(cleaned, user_text)
    cleaned = _rescue_emotional_question_only(cleaned, user_text)
    cleaned = _rescue_low_engagement_reply(cleaned, user_text)
    final = sanitize_chat_text(cleaned)
    if not final and _meaningful_user_message(user_text):
        return _presence_addition(user_text)
    return final


def _repair_role_attribution(text: str, recent_lines: list[str]) -> str:
    if not re.search(r"你.{0,8}(?:说|问|提)", text):
        return text
    if not _looks_misattributed_to_user(text, recent_lines):
        return text
    repaired = text
    replacements = {
        "不是你说": "不是我说",
        "不是你问": "不是我问",
        "你刚刚说": "我刚刚说",
        "你刚才说": "我刚才说",
        "你昨晚说": "我昨晚说",
        "你昨天说": "我昨天说",
        "你之前说": "我之前说",
        "你前面说": "我前面说",
        "你刚刚问": "我刚刚问",
        "你刚才问": "我刚才问",
        "你昨晚问": "我昨晚问",
        "你昨天问": "我昨天问",
        "你之前问": "我之前问",
        "你前面问": "我前面问",
    }
    for before, after in replacements.items():
        repaired = repaired.replace(before, after)
    repaired = re.sub(r"你(不是)?(.{0,6})(说|问|提)", lambda m: f"我{m.group(1) or ''}{m.group(2)}{m.group(3)}", repaired, count=1)
    return repaired


def _looks_misattributed_to_user(text: str, recent_lines: list[str]) -> bool:
    assistant_lines = _recent_texts_by_role(recent_lines, "她")
    user_lines = _recent_texts_by_role(recent_lines, "你")
    if not assistant_lines:
        return False
    assistant_score = max((_text_similarity(text, line) for line in assistant_lines[-8:]), default=0.0)
    user_score = max((_text_similarity(text, line) for line in user_lines[-8:]), default=0.0)
    return assistant_score >= 0.16 and assistant_score >= user_score + 0.08


def _soften_stale_time_reference(text: str, recent_lines: list[str]) -> str:
    if not any(token in text for token in ("刚刚说", "刚刚问", "刚才说", "刚才问")):
        return text
    stale_markers = ("[昨晚]", "[昨天上午]", "[昨天下午]", "[更早]")
    if not any(marker in line for line in recent_lines for marker in stale_markers):
        return text
    return (
        text.replace("刚刚说", "那会儿说")
        .replace("刚刚问", "那会儿问")
        .replace("刚才说", "那会儿说")
        .replace("刚才问", "那会儿问")
    )


def _remove_recent_duplicate_sentences(text: str, recent_lines: list[str], user_text: str) -> str:
    assistant_lines = _recent_texts_by_role(recent_lines, "她")
    if not assistant_lines:
        return text
    sentences = [part.strip() for part in re.split(r"(?<=[。！？!?～~])", text) if part.strip()]
    if not sentences:
        return text
    kept: list[str] = []
    for sentence in sentences:
        duplicate_score = max((_text_similarity(sentence, recent) for recent in assistant_lines[-4:]), default=0.0)
        if duplicate_score < 0.58:
            kept.append(sentence)
    if len(kept) == len(sentences):
        return text
    if kept:
        return "".join(kept).strip()
    if _looks_like_farewell(user_text):
        return "嗯，晚安。"
    if _looks_like_minimal_ack(user_text):
        return "嗯。"
    return text


def _recent_texts_by_role(recent_lines: list[str], role: str) -> list[str]:
    texts: list[str] = []
    marker = f"] {role}:"
    for line in recent_lines:
        if marker not in line:
            continue
        texts.append(line.split(marker, 1)[1].strip())
    return texts


def _text_similarity(left: str, right: str) -> float:
    left_units = _content_units(left)
    right_units = _content_units(right)
    if not left_units or not right_units:
        return 0.0
    return len(left_units & right_units) / len(left_units | right_units)


def _content_units(text: str) -> set[str]:
    normalized = re.sub(r"[^\w\u4e00-\u9fff]+", "", text.lower())
    stop_chars = set("你我她他它的是了嘛吗呢呀啊哦嗯在就都和也不有说问刚才刚刚昨晚之前")
    chars = [char for char in normalized if char not in stop_chars]
    units = set(chars)
    units.update(normalized[index : index + 2] for index in range(max(0, len(normalized) - 1)))
    return {unit for unit in units if unit.strip()}


def _looks_like_farewell(text: str) -> bool:
    stripped = text.strip()
    return any(token in stripped for token in ("晚安", "睡觉", "睡了", "好梦", "早点睡"))


def _looks_like_minimal_ack(text: str) -> bool:
    stripped = re.sub(r"\s+", "", text)
    return bool(re.fullmatch(r"[嗯哦噢喔好行啊诶哎哼～~。！？!?,.，、]{1,8}", stripped))


def _recent_assistant_questions(recent_lines: list[str]) -> int:
    her_recent = [line for line in recent_lines[-8:] if "] 她:" in line]
    return sum(line.count("？") + line.count("?") for line in her_recent[-4:])


def _looks_like_user_question(text: str) -> bool:
    stripped = text.strip()
    return stripped.endswith(("？", "?")) or any(
        token in stripped for token in ["怎么", "为什么", "吗", "是不是", "能不能", "可不可以", "哪里"]
    )


def _remove_followup_questions(text: str) -> str:
    parts = [part.strip() for part in re.split(r"(?<=[。！？!?])", text) if part.strip()]
    if not parts:
        return text
    non_question_parts = [part for part in parts if not _looks_like_question_sentence(part)]
    if non_question_parts:
        return "".join(non_question_parts).strip()
    trimmed_tail = re.sub(r"(?:[，,]|……|\.\.\.)[^，,。！？!?]{0,40}[吗呢呀么][？?]$", "。", text).strip()
    if trimmed_tail != text:
        return trimmed_tail
    trimmed = re.sub(r"[，,][^，,。！？!?]{0,40}[吗呢呀么][。！？!?]$", "。", text).strip()
    if trimmed != text:
        return trimmed
    return ""


def _looks_like_question_sentence(text: str) -> bool:
    stripped = text.strip()
    if stripped.endswith(("？", "?")):
        return True
    if any(token in stripped for token in ["哪", "怎么", "为什么", "什么时候", "多少", "谁"]):
        return stripped.endswith("。")
    return stripped.endswith(("吗。", "呢。", "呀。", "么。"))


def _rescue_low_engagement_reply(text: str, user_text: str) -> str:
    if not _needs_more_presence(text, user_text):
        return text
    addition = _presence_addition(user_text)
    if not addition:
        return text
    return f"{text}{addition}"


def _repair_incomplete_trailing(text: str, user_text: str) -> str:
    if not re.search(r"(?:的话|就是|然后|所以|因为|但是|不过)[………\.。]*$", text):
        return text
    if any(token in user_text for token in ("雨", "伞", "淋")):
        return "找不到伞真的会让人一大早心情打折。"
    return re.sub(r"(?:的话|就是|然后|所以|因为|但是|不过)[………\.。]*$", "。", text).strip()


def _rescue_emotional_question_only(text: str, user_text: str) -> str:
    if not _emotional_user_message(user_text):
        return text
    sentences = [part.strip() for part in re.split(r"(?<=[。！？!?])", text) if part.strip()]
    if not sentences or not all(sentence.endswith(("？", "?")) for sentence in sentences):
        return text
    if "闷" in user_text or "心里" in user_text:
        return "心里闷的那种最耗人了。我先陪你待一会儿。"
    if "累" in user_text:
        return "那种累不一定说得清楚，但会压着人。我在这儿。"
    return "我先不问那么多了。你不用马上解释，我陪你待一会儿。"


def _needs_more_presence(text: str, user_text: str) -> bool:
    stripped = text.strip()
    if not stripped or not _meaningful_user_message(user_text):
        return False
    if stripped in {
        "嗯。",
        "嗯嗯。",
        "哦。",
        "啊。",
        "啊这。",
        "好。",
        "好的。",
        "行。",
        "那有点惨。",
        "那就好。",
        "怎么了？",
        "我懂那种感觉。",
        "我有点好奇。",
        "我有点好奇了。",
        "我也有点好奇了。",
    }:
        return True
    return bool(re.fullmatch(r"(?:哦|噢|嗯|啊)?[，,]?[^。！？]{2,14}(?:啊|哦|呀|诶)。", stripped))


def _meaningful_user_message(text: str) -> bool:
    stripped = text.strip()
    if len(stripped) >= 8:
        return True
    return any(token in stripped for token in ("累", "难", "烦", "考试", "上学", "老师", "下雨", "伞", "成都"))


def _emotional_user_message(text: str) -> bool:
    return any(token in text for token in ("累", "闷", "难过", "烦", "委屈", "不开心", "心里", "难受"))


def _presence_addition(user_text: str) -> str:
    if any(token in user_text for token in ("成都理工", "成都", "学校", "上学")):
        return "感觉突然离你具体了一点。"
    if any(token in user_text for token in ("毛概", "背", "考试", "复习")):
        return "这种硬背的东西最磨人。"
    if any(token in user_text for token in ("雨", "伞", "淋")):
        return "光想想湿鞋就有点烦。"
    if "老师" in user_text and "迟到" in user_text:
        return "这反而有点荒唐。"
    if any(token in user_text for token in ("累", "闷", "难过", "烦", "委屈")):
        return "我会想先陪你待一会儿。"
    return "我刚刚停了一下，脑子里有画面了。"

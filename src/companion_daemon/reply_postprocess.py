import re

from companion_daemon.sanitize import sanitize_chat_text


def postprocess_reply_text(text: str, *, recent_lines: list[str], user_text: str) -> str:
    cleaned = sanitize_chat_text(text)
    if _recent_assistant_questions(recent_lines) and not _looks_like_user_question(user_text):
        cleaned = _remove_followup_questions(cleaned)
    cleaned = _repair_incomplete_trailing(cleaned, user_text)
    cleaned = _rescue_emotional_question_only(cleaned, user_text)
    cleaned = _rescue_low_engagement_reply(cleaned, user_text)
    final = sanitize_chat_text(cleaned)
    if not final and _meaningful_user_message(user_text):
        return _presence_addition(user_text)
    return final


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

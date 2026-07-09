import re

from companion_daemon.sanitize import sanitize_chat_text


def postprocess_reply_text(text: str, *, recent_lines: list[str], user_text: str) -> str:
    cleaned = sanitize_chat_text(text)
    if _recent_assistant_questions(recent_lines) and not _looks_like_user_question(user_text):
        cleaned = _remove_followup_questions(cleaned)
    return sanitize_chat_text(cleaned)


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
    trimmed = re.sub(r"[，,][^，,。！？!?]{0,40}[吗呢呀么][。！？!?]$", "。", text).strip()
    if trimmed != text:
        return trimmed
    return re.sub(r"[？?]", "。", text).strip()


def _looks_like_question_sentence(text: str) -> bool:
    stripped = text.strip()
    if stripped.endswith(("？", "?")):
        return True
    if any(token in stripped for token in ["哪", "怎么", "为什么", "什么时候", "多少", "谁"]):
        return stripped.endswith("。")
    return stripped.endswith(("吗。", "呢。", "呀。", "么。"))

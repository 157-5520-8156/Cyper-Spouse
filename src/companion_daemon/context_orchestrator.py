import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from companion_daemon.human_rhythm import human_rhythm_snapshot
from companion_daemon.memory import _exclude_from_reply_memory
from companion_daemon.models import IncomingMessage, MoodState
from companion_daemon.time import utc_now


@dataclass(frozen=True)
class ContextPackage:
    user_intent: str
    reply_focus: str
    forbidden_old_topics: list[str]
    memory_lines: list[str]
    life_context: str
    emotion_context: str
    prompt_summary: str

    def prompt_block(self) -> str:
        forbidden = "；".join(self.forbidden_old_topics) if self.forbidden_old_topics else "无"
        memories = "；".join(self.memory_lines) if self.memory_lines else "无高相关长期记忆"
        return (
            "上下文编排:\n"
            f"- 当前用户意图: {self.user_intent}\n"
            f"- 本轮接话焦点: {self.reply_focus}\n"
            f"- 禁止误用的旧话: {forbidden}\n"
            f"- 相关长期记忆: {memories}\n"
            f"- 她自己的当前生活状态: {self.life_context}\n"
            f"- 情绪/关系影响: {self.emotion_context}\n"
            f"- 最终 prompt 摘要: {self.prompt_summary}"
        )


def build_context_package(
    message: IncomingMessage,
    state: MoodState,
    recent_rows: list[dict[str, str]],
    memory_rows: list[Any],
    *,
    max_memories: int = 5,
    now: datetime | None = None,
) -> ContextPackage:
    user_intent = infer_user_intent(message.text, has_attachments=bool(message.attachments))
    reply_focus = choose_reply_focus(message.text, user_intent)
    forbidden = forbidden_old_topics(recent_rows)
    memories = select_relevant_memories(
        memory_rows,
        message.text,
        recent_rows,
        max_memories=max_memories,
    )
    life_context = current_life_context(state, now=now)
    emotion_context = state_effect_summary(state)
    prompt_summary = (
        f"只回应用户当前这条里的“{reply_focus}”；"
        "历史和记忆只用于避免忘事、错认说话人和瞎编，不要逐条复盘。"
    )
    return ContextPackage(
        user_intent=user_intent,
        reply_focus=reply_focus,
        forbidden_old_topics=forbidden,
        memory_lines=memories,
        life_context=life_context,
        emotion_context=emotion_context,
        prompt_summary=prompt_summary,
    )


def infer_user_intent(text: str, *, has_attachments: bool = False) -> str:
    stripped = text.strip()
    if has_attachments and len(stripped) < 6:
        return "发来附件，期待她看见并自然反应"
    if _contains(stripped, ["在吗", "睡了吗", "忙吗", "人呢"]):
        return "试探她是否在线或想重新打开聊天"
    if _contains(stripped, ["难受", "崩溃", "烦", "累", "闷", "委屈", "emo", "不开心", "想哭"]):
        return "表达情绪，需要先被接住"
    if _contains(stripped, ["哈哈", "笑死", "乐", "草", "绷不住"]):
        return "开玩笑或分享轻松反应"
    if _looks_like_question(stripped):
        return "提出问题，期待回答或态度"
    if len(stripped) >= 80 or stripped.count("\n") >= 2:
        return "连续讲一件事，需要她抓重点而不是审问"
    if _contains(stripped, ["嗯", "哦", "好吧", "行吧", "晚安", "睡了"]):
        return "可能在收尾、敷衍、疲惫或暂时不想展开"
    return "普通私聊推进，期待自然接话"


def choose_reply_focus(text: str, user_intent: str) -> str:
    stripped = _compact_space(text)
    if not stripped:
        return user_intent
    if user_intent == "表达情绪，需要先被接住":
        return "先回应他的情绪，再轻轻接住具体事情"
    if user_intent == "提出问题，期待回答或态度":
        return _first_question_clause(stripped)
    if user_intent == "连续讲一件事，需要她抓重点而不是审问":
        return _summarize_long_message_focus(stripped)
    if user_intent == "可能在收尾、敷衍、疲惫或暂时不想展开":
        return "尊重收尾或低能量，不强行追问"
    return stripped[:48]


def forbidden_old_topics(recent_rows: list[dict[str, str]]) -> list[str]:
    forbidden: list[str] = [
        "不要把'她:'说过的话说成用户说过",
        "不要把隔夜或超过一小时的事称为刚刚",
    ]
    if recent_rows and recent_rows[-1].get("direction") == "out":
        forbidden.append("如果最近最后一句是她说的，引用时说'我刚才/我之前'，不要说'你刚才'")
    if _has_unanswered_assistant_question(recent_rows):
        forbidden.append("不要追讨她上一个问题；用户换话题就顺着当前话题")
    return forbidden[:4]


def select_relevant_memories(
    rows: list[Any],
    message_text: str,
    recent_rows: list[dict[str, str]],
    *,
    max_memories: int = 5,
    char_budget: int = 700,
) -> list[str]:
    query = " ".join([message_text, *[row.get("text", "") for row in recent_rows[-3:]]])
    query_terms = _terms(query)
    scored: list[tuple[float, int, str]] = []
    for index, row in enumerate(rows):
        kind = _row_get(row, "kind")
        if _exclude_from_reply_memory(kind):
            continue
        content = _row_get(row, "content")
        confidence = _row_float(row, "confidence", 0.7)
        overlap = len(query_terms & _terms(content))
        kind_bonus = _kind_bonus(kind)
        recency = max(0.0, 0.22 - index * 0.025)
        direct_bonus = 0.25 if overlap else 0.0
        score = confidence + kind_bonus + recency + direct_bonus + min(0.3, overlap * 0.08)
        scored.append((score, index, f"- [{kind}] {content}"))
    scored.sort(key=lambda item: (-item[0], item[1]))
    lines: list[str] = []
    total = 0
    for _, _, line in scored:
        if len(lines) >= max_memories:
            break
        if total + len(line) > char_budget and lines:
            break
        lines.append(line)
        total += len(line)
    return lines


def current_life_context(state: MoodState, *, now: datetime | None = None) -> str:
    rhythm = human_rhythm_snapshot(state, now or utc_now())
    return (
        f"{rhythm.phase}，像是在{rhythm.private_activity}；"
        f"注意力={rhythm.attention_mode}；回复节奏={rhythm.reply_guidance}"
    )


def state_effect_summary(state: MoodState) -> str:
    parts = [
        f"心情={state.mood}",
        f"关系={state.relationship_stage}",
        f"亲密={state.intimacy}",
        f"信任={state.trust}",
    ]
    if state.emotional_charge >= 20:
        parts.append("有情绪余波，别立刻装没事")
    if state.boundary_level >= 25:
        parts.append("边界较高，回复要克制")
    if state.initiative >= 55:
        parts.append("主动欲望偏高，可以更有分享欲")
    if state.unresolved_emotion:
        parts.append(f"未消化情绪={state.unresolved_emotion}")
    return "；".join(parts)


def _contains(text: str, tokens: list[str]) -> bool:
    return any(token in text for token in tokens)


def _looks_like_question(text: str) -> bool:
    return "?" in text or "？" in text or any(token in text for token in ["吗", "怎么", "为什么", "是不是", "能不能", "要不要"])


def _first_question_clause(text: str) -> str:
    for delimiter in ["？", "?", "。", "！", "!", "\n"]:
        if delimiter in text:
            return text.split(delimiter, 1)[0][:48] + ("？" if delimiter in {"？", "?"} else "")
    return text[:48]


def _summarize_long_message_focus(text: str) -> str:
    sentences = [part.strip() for part in re.split(r"[。！？!?\n]+", text) if part.strip()]
    if not sentences:
        return text[:56]
    if len(sentences[0]) < 12 and len(sentences) > 1:
        return sentences[1][:56]
    return sentences[0][:56]


def _has_unanswered_assistant_question(recent_rows: list[dict[str, str]]) -> bool:
    for row in reversed(recent_rows[-6:]):
        text = row.get("text", "")
        if row.get("direction") == "in":
            return False
        if row.get("direction") == "out" and ("?" in text or "？" in text):
            return True
    return False


def _kind_bonus(kind: str) -> float:
    if kind in {"name", "life_fact", "favorite_thing", "person", "self_core"}:
        return 0.18
    if kind in {"shared_moment", "recent_event", "user_visual_anchor"}:
        return 0.12
    if kind in {"image_insight", "audio_insight", "file_insight"}:
        return 0.08
    return 0.0


def _terms(text: str) -> set[str]:
    normalized = _compact_space(text.lower())
    latin_terms = set(re.findall(r"[a-z0-9_]{2,}", normalized))
    cjk = re.findall(r"[\u4e00-\u9fff]", normalized)
    cjk_terms = {"".join(cjk[index:index + 2]) for index in range(max(0, len(cjk) - 1))}
    keywords = {
        token
        for token in re.split(r"[，。！？、\s,.!?;；:：()（）]+", normalized)
        if len(token) >= 2
    }
    return latin_terms | cjk_terms | keywords


def _compact_space(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _row_get(row: Any, key: str) -> str:
    try:
        return str(row[key])
    except (KeyError, IndexError, TypeError):
        if isinstance(row, dict):
            return str(row.get(key, ""))
    return ""


def _row_float(row: Any, key: str, default: float) -> float:
    value = _row_get(row, key)
    try:
        return float(value)
    except ValueError:
        return default

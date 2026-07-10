import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from companion_daemon.emotion_core import emotion_snapshot
from companion_daemon.human_rhythm import human_rhythm_snapshot
from companion_daemon.impression import impression_summary
from companion_daemon.memory import _exclude_from_reply_memory
from companion_daemon.models import IncomingMessage, MoodState
from companion_daemon.time import utc_now


@dataclass(frozen=True)
class ContextPackage:
    user_intent: str
    reply_focus: str
    forbidden_old_topics: list[str]
    memory_lines: list[str]
    self_fact_lines: list[str]
    life_context: str
    emotion_context: str
    impression_context: str
    reply_policy: str
    continuity_hint: str
    subtext_hint: str
    prompt_summary: str

    def prompt_block(self) -> str:
        forbidden = "；".join(self.forbidden_old_topics) if self.forbidden_old_topics else "无"
        user_facts = "；".join(self.memory_lines) if self.memory_lines else "无高相关用户长期记忆"
        self_facts = "；".join(self.self_fact_lines) if self.self_fact_lines else "无额外已发生自我事实"
        return (
            "上下文编排:\n"
            f"- 当前用户意图: {self.user_intent}\n"
            f"- 本轮接话焦点: {self.reply_focus}\n"
            f"- 禁止误用的旧话: {forbidden}\n"
            f"- 可用用户事实: {user_facts}\n"
            f"- 可用知栀事实: {self_facts}\n"
            "- 事实归属: 当前用户消息和可用用户事实只能归给用户；可用知栀事实只能归给知栀。"
            "最近聊天仅用于理解话题与语气，不是新增事实来源；可用知栀事实是可验证自我陈述的完整账本。"
            "没有凭据就保持不确定，不要补写。\n"
            "- 联想边界: 可以有短暂、非定案的感受或想象；但不得把联想写成带具体时间、地点、所有权、"
            "姓名、数量或结果的亲历事实，也不得在之后把它当作记忆引用。\n"
            f"- 她自己的当前生活状态: {self.life_context}\n"
            "- 状态解释边界: 生活状态只提供明确写出的事实；活动大类、注意力或手机状态不能自行细化成"
            "具体动作、地点、人物或经历。\n"
            f"- 情绪/关系影响: {self.emotion_context}\n"
            f"- 她对用户的当前印象: {self.impression_context}\n"
            f"- 本轮回复策略: {self.reply_policy}\n"
            f"- 连续性约束: {self.continuity_hint}\n"
            f"- 内在倾向: {self.subtext_hint}\n"
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
    continuity_hint: str | None = None,
    subtext_hint: str | None = None,
    life_context_override: str | None = None,
    self_fact_lines: list[str] | None = None,
) -> ContextPackage:
    user_intent = infer_user_intent(message.text, has_attachments=bool(message.attachments))
    reply_focus = choose_reply_focus(message.text, user_intent)
    forbidden = forbidden_old_topics(recent_rows)
    current_time = now or utc_now()
    memories = select_relevant_memories(
        memory_rows,
        message.text,
        recent_rows,
        max_memories=max_memories,
        now=current_time,
    )
    life_context = life_context_override or current_life_context(state, now=current_time)
    emotion_context = state_effect_summary(state)
    impression_context = impression_summary(state)
    reply_policy = build_reply_policy(user_intent, state)
    prompt_summary = (
        f"只回应用户当前这条里的“{reply_focus}”；"
        "历史和记忆只用于避免忘事、错认说话人和瞎编，不要逐条复盘。"
    )
    return ContextPackage(
        user_intent=user_intent,
        reply_focus=reply_focus,
        forbidden_old_topics=forbidden,
        memory_lines=memories,
        self_fact_lines=self_fact_lines or [],
        life_context=life_context,
        emotion_context=emotion_context,
        impression_context=impression_context,
        reply_policy=reply_policy,
        continuity_hint=continuity_hint or "保持最近的语气，不要突然大幅变调",
        subtext_hint=subtext_hint or "无额外潜台词，别强行演情绪",
        prompt_summary=prompt_summary,
    )


def infer_user_intent(text: str, *, has_attachments: bool = False) -> str:
    stripped = text.strip()
    if has_attachments and len(stripped) < 6:
        return "发来附件，期待她看见并自然反应"
    if _is_reply_timing_complaint(stripped):
        return "对她断续回复感到不舒服，需要先承认这次失联感"
    if _contains(stripped, ["难受", "崩溃", "烦", "累", "闷", "委屈", "emo", "不开心", "想哭"]):
        return "表达情绪，需要先被接住"
    if _contains(stripped, ["在吗", "睡了吗", "忙吗", "人呢"]):
        return "试探她是否在线或想重新打开聊天"
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
    if user_intent == "对她断续回复感到不舒服，需要先承认这次失联感":
        return "先承认让他等到了，再回应他此刻真正问的事"
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
    now: datetime | None = None,
) -> list[str]:
    """Retrieve only memories that help with *this* turn.

    This is intentionally a hybrid lexical retriever instead of an embedding API
    call. It is deterministic, cheap, inspectable in the daemon panel, and keeps
    irrelevant profile facts out of routine replies. The scoring boundary is kept
    here so an embedding/reranker can later replace only this function.
    """
    current_time = now or utc_now()
    current_terms = _terms(message_text)
    current_terms -= _deprioritized_terms(message_text)
    recent_terms = _terms(" ".join(row.get("text", "") for row in recent_rows[-3:]))
    candidates = _drop_conflicting_and_expired_memories(rows, now=current_time)
    scored: list[tuple[float, int, set[str], str]] = []
    for index, row in candidates:
        kind = _row_get(row, "kind")
        if _exclude_from_reply_memory(kind):
            continue
        content = _row_get(row, "content")
        confidence = _row_float(row, "confidence", 0.7)
        memory_terms = _terms(content)
        current_overlap = current_terms & memory_terms
        recent_overlap = recent_terms & memory_terms
        # Old conversation can help interpret a short acknowledgement, but it
        # must never outweigh the text the user just sent.
        relevance = len(current_overlap) + min(0.35, len(recent_overlap) * 0.12)
        if relevance <= 0:
            continue
        recency = _recency_score(row, index, now=current_time)
        score = (
            relevance * 1.2
            + confidence * 0.22
            + _kind_bonus(kind)
            + recency * 0.12
        )
        scored.append((score, index, memory_terms, f"- [{kind}] {content}"))
    scored.sort(key=lambda item: (-item[0], item[1]))
    lines: list[str] = []
    selected_terms: list[set[str]] = []
    total = 0
    for _, _, memory_terms, line in scored:
        if len(lines) >= max_memories:
            break
        if total + len(line) > char_budget and lines:
            break
        if any(_term_overlap(memory_terms, previous) >= 0.72 for previous in selected_terms):
            continue
        lines.append(line)
        selected_terms.append(memory_terms)
        total += len(line)
    return lines


def current_life_context(state: MoodState, *, now: datetime | None = None) -> str:
    rhythm = human_rhythm_snapshot(state, now or utc_now())
    return (
        f"生活节律={rhythm.phase}，像是在{rhythm.private_activity}；"
        f"注意力={rhythm.attention_mode}；回复节奏={rhythm.reply_guidance}"
    )


def state_effect_summary(state: MoodState) -> str:
    parts = [f"关系阶段={state.relationship_stage}"]
    if state.emotional_charge >= 20:
        parts.append("情绪余波")
    if state.boundary_level >= 25:
        parts.append("边界收紧")
    if state.initiative >= 55:
        parts.append("分享欲偏高")
    if state.unresolved_emotion:
        parts.append("有未消化的事")
    # Carry the Plutchik vector into ordinary replies as behavioral guidance,
    # never as raw numbers; proactive prompts already do this separately.
    snapshot = emotion_snapshot(state)
    if snapshot.value >= 25:
        parts.append(snapshot.guidance.removeprefix("情绪指导: ").removesuffix(" 不要直接报出这些内部数值。"))
    return "；".join(parts)


def build_reply_policy(user_intent: str, state: MoodState) -> str:
    event_policy = {
        "boundary_violation": "明确表示不舒服，短而坚定；不要讨好、不要立刻原谅",
        "control_pressure": "清楚拒绝被命令，语气平静但有边界",
        "premature_intimacy": "轻轻挡回过早亲密，强调慢慢来",
        "repair_attempt": "承认听见道歉，但保留一点观察，不立刻翻篇",
    }.get(state.last_interaction_event)
    if event_policy:
        return event_policy

    if state.perceived_respect < 35:
        return "保持距离和边界，不要讨好；只回应当前必要内容"
    if state.perceived_responsiveness < 35:
        return "别追问或索取回应，语气自然收住"

    if user_intent == "对她断续回复感到不舒服，需要先承认这次失联感":
        return (
            "先承认回应断掉让他不舒服；解释只能使用当前生活状态里的已知情况，"
            "没有记录就直接道歉，不编临时动作当借口，也别轻飘飘许诺以后绝不会这样"
        )

    if user_intent == "表达情绪，需要先被接住":
        base = "先接住情绪，再回应具体事情；不急着给建议"
    elif user_intent == "提出问题，期待回答或态度":
        base = "先回答当前问句"
    elif user_intent == "连续讲一件事，需要她抓重点而不是审问":
        base = "抓住一个最重要的点回应，少总结、少追问"
    elif user_intent == "可能在收尾、敷衍、疲惫或暂时不想展开":
        base = "尊重低能量和收尾，不强行续话"
    else:
        base = "先自然接住当前这句话"

    if state.emotional_charge >= 35 or state.unresolved_emotion:
        return f"{base}；保留一点情绪，但不翻旧账、不演独白"
    if state.boundary_level >= 35:
        return f"{base}；语气克制，别硬拉近关系"
    if state.initiative >= 60:
        return f"{base}；可以多一点自己的反应，但别抢话"
    return base


def _is_reply_timing_complaint(text: str) -> bool:
    """Recognize a relationship-repair act rather than one exact phrase."""
    rhythm_cues = ["不回", "没回", "消失", "不见", "说话说到一半", "晾着", "爱回不回", "已读不回"]
    discomfort_cues = ["生气", "不高兴", "烦", "不爽", "难受", "怎么", "老是", "又"]
    return _contains(text, rhythm_cues) and _contains(text, discomfort_cues)


def _drop_conflicting_and_expired_memories(
    rows: list[Any],
    *,
    now: datetime,
) -> list[tuple[int, Any]]:
    newest_by_conflict_key: dict[str, tuple[datetime, int]] = {}
    prepared: list[tuple[int, Any, datetime | None, str | None]] = []
    for index, row in enumerate(rows):
        kind = _row_get(row, "kind")
        content = _row_get(row, "content")
        updated_at = _row_datetime(row, "updated_at")
        if _is_expired_ephemeral_memory(kind, content, updated_at, now):
            continue
        conflict_key = _memory_conflict_key(kind, content)
        prepared.append((index, row, updated_at, conflict_key))
        if conflict_key:
            timestamp = updated_at or (now - timedelta(seconds=index))
            current = newest_by_conflict_key.get(conflict_key)
            if current is None or timestamp > current[0]:
                newest_by_conflict_key[conflict_key] = (timestamp, index)

    return [
        (index, row)
        for index, row, _, conflict_key in prepared
        if not conflict_key or newest_by_conflict_key[conflict_key][1] == index
    ]


def _is_expired_ephemeral_memory(
    kind: str,
    content: str,
    updated_at: datetime | None,
    now: datetime,
) -> bool:
    if updated_at is None:
        return False
    age = now - updated_at
    if kind == "schedule" and age > timedelta(hours=36):
        return True
    if any(token in content for token in ("今天", "今晚", "明天", "后天")) and age > timedelta(hours=36):
        return True
    return kind == "recent_event" and age > timedelta(days=21)


def _memory_conflict_key(kind: str, content: str) -> str | None:
    if kind in {"life_fact", "custom", "status"} and re.search(r"(?:住在|人在|搬到|来自|定居|在)\S{0,2}(?:成都|上海|北京|广州|深圳|杭州|南京|武汉)", content):
        return "user_location"
    if kind in {"name", "status"}:
        return f"singleton:{kind}"
    return None


def _row_datetime(row: Any, key: str) -> datetime | None:
    value = _row_get(row, key)
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _recency_score(row: Any, index: int, *, now: datetime) -> float:
    updated_at = _row_datetime(row, "updated_at")
    if updated_at is None:
        return max(0.0, 0.18 - index * 0.02)
    age_hours = max(0.0, (now - updated_at).total_seconds() / 3600)
    return 1.0 / (1.0 + age_hours / 72)


def _term_overlap(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / min(len(left), len(right))


def _deprioritized_terms(text: str) -> set[str]:
    """Remove a topic the user explicitly asked to set aside this turn."""
    suppressed: set[str] = set()
    for match in re.finditer(r"(?:先不说|不想聊|别聊|不聊)([^，。！？\n]{1,16})", text):
        suppressed |= _terms(match.group(1))
    return suppressed


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
    # "custom" is the legacy storage kind for life facts written before the
    # extraction/retrieval kinds were unified; keep rewarding old rows.
    if kind in {"name", "life_fact", "custom", "favorite_thing", "person", "self_core"}:
        return 0.18
    if kind in {"shared_moment", "recent_event", "hobby", "user_visual_anchor"}:
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
    stop_terms = {"用户", "今天", "最近", "这个", "那个", "有点", "一下", "怎么", "什么", "不是", "然后", "就是"}
    return (latin_terms | cjk_terms | keywords) - stop_terms


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

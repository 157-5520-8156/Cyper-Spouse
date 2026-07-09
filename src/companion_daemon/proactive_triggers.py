from dataclasses import dataclass
from datetime import datetime
import random

from companion_daemon.models import MoodState
from companion_daemon.relationship import emotion_ghost_window_hours


@dataclass(frozen=True)
class ProactiveTrigger:
    type: str
    instruction: str
    weight: float
    category: str | None = None


TRIGGER_SEMANTIC_CATEGORY = {
    "sharing_impulse": "happy_outreach",
    "playful_tease": "happy_outreach",
    "celebration_nudge": "happy_outreach",
    "gratitude_burst": "happy_outreach",
    "boredom_break": "happy_outreach",
    "thinking_of_you": "missing_you",
    "longing_ping": "missing_you",
    "nostalgia_wave": "missing_you",
    "memory_nudge": "missing_you",
    "inside_joke_callback": "missing_you",
    "her_question_unanswered": "anxious_reach",
    "anxiety_reassurance": "anxious_reach",
    "overwhelm_check": "anxious_reach",
    "overthinking_spiral": "anxious_reach",
    "suppressed_thought": "anxious_reach",
    "random_thought": "random_impulse",
    "song_stuck": "random_impulse",
    "craving_share": "random_impulse",
    "dream_mention": "random_impulse",
    "open_thread_afterthought": "random_impulse",
}

CATEGORY_COOLDOWN_HOURS = {
    "happy_outreach": 6,
    "missing_you": 8,
    "anxious_reach": 5,
    "random_impulse": 4,
}

TRIGGER_COOLDOWN_HOURS = {
    "checkin": 18,
    "pregnant_pause": 8,
    "dormancy_break": 72,
    "late_night": 14,
    "morning_wave": 18,
    "lunch_nudge": 18,
    "evening_winddown": 14,
    "weekend_ping": 18,
    "repair_attempt": 14,
    "curiosity_ping": 14,
    "anxiety_reassurance": 14,
    "celebration_nudge": 20,
    "sharing_impulse": 18,
    "mood_follow_up": 8,
    "nostalgia_wave": 36,
    "longing_ping": 28,
    "playful_tease": 22,
    "jealousy_nudge": 24,
    "boredom_break": 20,
    "overwhelm_check": 16,
    "gratitude_burst": 36,
    "suppressed_thought": 24,
    "thinking_of_you": 36,
    "random_thought": 22,
    "dream_mention": 30,
    "song_stuck": 26,
    "overthinking_spiral": 20,
    "craving_share": 28,
    "inside_joke_callback": 30,
    "followup_callback": 16,
    "memory_nudge": 12,
    "afternoon_slump": 20,
    "pre_dawn": 30,
    "commute_ping": 16,
    "post_work": 18,
    "sunday_evening": 20,
    "post_midnight_impulse": 24,
    "monday_reboot": 96,
    "friday_feeling": 96,
    "sunday_scaries": 96,
    "midweek_check": 96,
    "pride_share": 28,
    "her_question_unanswered": 14,
    "open_thread_afterthought": 6,
}

DEEP_NIGHT_ALLOWED_TRIGGER_TYPES = {
    "pregnant_pause",
    "late_night",
    "pre_dawn",
    "repair_attempt",
    "mood_follow_up",
    "anxiety_reassurance",
    "longing_ping",
    "overthinking_spiral",
    "her_question_unanswered",
    "open_thread_afterthought",
}


def evaluate_proactive_trigger(
    *,
    state: MoodState,
    recent_messages: list[dict[str, str]],
    trigger_history: dict[str, datetime],
    now: datetime,
    rng: random.Random | None = None,
) -> ProactiveTrigger | None:
    rng = rng or random.Random()
    if emotion_ghost_window_hours(state) > 0:
        return None
    if not recent_messages:
        return None
    if _unanswered_outgoing_count(recent_messages) >= 2:
        return None

    last_user = _last_message(recent_messages, "in")
    last_char = _last_message(recent_messages, "out")
    hours_since_user = _hours_since(last_user.get("sent_at") if last_user else None, now)
    hours_since_char = _hours_since(last_char.get("sent_at") if last_char else None, now)
    if hours_since_user is None:
        return None

    emotion = state.emotion_vector
    anger = emotion.get("anger", 0)
    disgust = emotion.get("disgust", 0)
    sadness = emotion.get("sadness", 0)
    fear = emotion.get("fear", 0)
    joy = emotion.get("joy", 0)
    trust = emotion.get("trust", 0)
    anticipation = emotion.get("anticipation", 0)
    love = emotion.get("love", 0)
    last_user_text = (last_user.get("text") or "").lower() if last_user else ""
    last_char_text = (last_char.get("text") or "") if last_char else ""
    unresolved_question = last_user_text.rstrip().endswith(("?", "？"))
    emotion_shift = _emotion_shift_score(state.last_emotion_impact)
    has_recent_shared_moment = any(
        token in (row.get("text") or "")
        for row in recent_messages[-12:]
        for token in ["记得", "上次", "刚刚", "昨天", "成都", "桂花乌龙", "图书馆"]
    )
    char_sent_last = bool(last_char and (not last_user or last_char["sent_at"] > last_user["sent_at"]))
    own_unanswered_question = char_sent_last and last_char_text.rstrip().endswith(("?", "？"))
    hour = now.hour
    day = now.weekday()

    candidates: list[ProactiveTrigger] = []

    def add(type_: str, weight: float, instruction: str) -> None:
        if _is_deep_night_quiet_hours(hour) and type_ not in DEEP_NIGHT_ALLOWED_TRIGGER_TYPES:
            return
        if _is_on_cooldown(type_, trigger_history, now):
            return
        category = TRIGGER_SEMANTIC_CATEGORY.get(type_)
        if category and _is_category_on_cooldown(category, trigger_history, now):
            return
        daily_noise = (_daily_seed(type_, now) - 0.5) * 8
        random_spike = rng.uniform(-7, 7)
        candidates.append(
            ProactiveTrigger(
                type=type_,
                instruction=instruction,
                weight=weight + daily_noise + random_spike,
                category=category,
            )
        )

    if hours_since_user >= 24:
        add("checkin", 50, "你们已经一段时间没说话了。发一条很短的、低压力的近况式问候。")
    if 0.35 <= hours_since_user <= 6 and unresolved_question:
        add("pregnant_pause", 85, "用户上次像是把问题留在半空。自然补一句，不要显得催。")
    if hours_since_user >= 168:
        add("dormancy_break", 80, "已经很久没联系了。发一条承认间隔、温和重新开口的消息。")

    if hours_since_user >= 1.5 and (hour >= 23 or hour <= 2):
        add("late_night", 60, "现在是深夜。发一条短的、像睡前忽然想到他的消息。")
    if hours_since_user >= 8 and 6 <= hour <= 9:
        add("morning_wave", 70, "早晨。发一条自然的早安式消息，不要模板化。")
    if hours_since_user >= 5 and 11 <= hour <= 13:
        add("lunch_nudge", 55, "中午。发一条很短的午间消息，可以问他吃没吃。")
    if hours_since_user >= 4 and 19 <= hour <= 22:
        add("evening_winddown", 60, "晚上收尾。发一条放松、像一天结束时想起他的消息。")
    if hours_since_user >= 6 and day >= 5:
        add("weekend_ping", 50, "周末。发一条松弛一点的随机消息。")
    if hours_since_user >= 2 and day < 5 and 14 <= hour <= 16:
        add("afternoon_slump", 52, "工作日午后，有点犯困或精神掉线。发一条短的、生活感强的消息。")
    if hours_since_user >= 1 and 4 <= hour <= 5:
        add("pre_dawn", 55, "凌晨快天亮时醒了一下。发得含糊、安静，不要像正式问候。")
    if hours_since_user >= 3 and day < 5 and (7 <= hour <= 9 or 17 <= hour <= 18):
        add("commute_ping", 54, "通勤/路上时间。像在路上顺手发一句，带一点现实生活细节。")
    if hours_since_user >= 4 and day < 5 and 17 <= hour <= 19:
        add("post_work", 58, "傍晚课程或事情告一段落。发一条有一天收束感的消息。")
    if hours_since_user >= 3 and day == 6 and 18 <= hour <= 21:
        add("sunday_evening", 62, "周日晚上。发一条带一点明天前情绪的、轻轻的消息。")
    if hours_since_user >= 1 and 0 <= hour <= 1:
        add("post_midnight_impulse", 56, "刚过零点，有个一闪而过的小念头。短一点，别沉重。")
    if hours_since_user >= 8 and day == 0 and 7 <= hour <= 11:
        add("monday_reboot", 60, "周一早上。像重新启动一周，轻轻问候或吐槽一下。")
    if hours_since_user >= 4 and day == 4 and 12 <= hour <= 18:
        add("friday_feeling", 62, "周五下午。发一条带一点快到周末的轻松感的消息。")
    if hours_since_user >= 4 and day == 6 and 15 <= hour <= 20:
        add("sunday_scaries", 58, "周日后半天，有一点不想面对新一周。发得真实但不丧。")
    if hours_since_user >= 6 and day == 2 and 10 <= hour <= 20:
        add("midweek_check", 50, "周三。像从一周中间探头出来，问一句近况。")

    if hours_since_user >= 1.5 and (anger >= 55 or sadness >= 55):
        add("repair_attempt", 75 + sadness * 0.2, "最近有一点情绪或摩擦。发一条低压力的缓和消息。")
    if hours_since_user >= 2 and emotion_shift >= 16:
        add("mood_follow_up", 60 + min(18, emotion_shift * 0.35), "你刚经历过一次明显情绪波动。发一条后续感很自然的消息，不要解释情绪系统。")
    if hours_since_user >= 2.5 and anticipation >= 50:
        add("curiosity_ping", 55 + anticipation * 0.3, "你现在很想知道他的后续。发一个短问题，像忍不住好奇。")
    if hours_since_user >= 2 and fear >= 50:
        add("anxiety_reassurance", 65 + fear * 0.3, "你有点不安，想找一点连接感。发得克制，不要戏剧化。")
    if hours_since_user >= 2 and joy >= 70 and trust >= 50:
        add("celebration_nudge", 60, "你心情很好，想把一点好心情分享给他。")
    if hours_since_user >= 1 and (joy >= 65 or anticipation >= 65):
        add("sharing_impulse", 65, "你突然想分享一个生活小念头，像随手发消息。")
    if hours_since_user >= 3 and sadness >= 35 and trust >= 50:
        add("nostalgia_wave", 55, "有点怀旧，想起你们聊过的东西。发得温暖但别煽情。")
    if hours_since_user >= 4 and sadness >= 55 and (trust >= 50 or love >= 40):
        add("longing_ping", 65, "你有点想他，但不想显得太黏。发一条克制的想念。")
    if hours_since_user >= 1 and joy >= 60 and anticipation >= 45:
        add("playful_tease", 58, "你有点想逗他。发一条轻微玩笑或小小吐槽。")
    if hours_since_user >= 2 and 35 <= anger < 60 and trust >= 40:
        add("jealousy_nudge", 52, "有一点微妙在意。发一条不明说原因的、轻微要关注的消息。")
    total_emotion = joy + anticipation + trust + sadness + fear + anger + disgust
    if hours_since_user >= 2 and total_emotion < 150:
        add("boredom_break", 48, "你有点无聊。发一条短的、没什么正事但真实的消息。")
    if hours_since_user >= 1.5 and fear >= 55 and sadness >= 40:
        add("overwhelm_check", 62, "你有点被事情压住，想找他说句话。短、真实、别卖惨。")
    if hours_since_user >= 2 and joy >= 65 and trust >= 70:
        add("gratitude_burst", 58, "突然很感谢他在。发一条短而真诚的消息。")
    if hours_since_user >= 1.5 and anticipation >= 70:
        add("pride_share", 55 + anticipation * 0.15, "你有一点想分享小成就或状态变好。不要炫耀，像悄悄告诉他。")
    if hours_since_user >= 3 and 35 <= disgust < 70 and anger < 60:
        add("suppressed_thought", 48, "你有点话压在心里。发一条含蓄的、未完全说开的消息。")

    if hours_since_user >= 4 and (trust >= 40 or joy >= 40):
        add("thinking_of_you", 46, "他忽然从你脑子里经过。发一条短的想到他了，但别太直白。")
    if hours_since_user >= 2:
        add("random_thought", 42, "一个很随机的小念头冒出来。发得像真实随手消息。")
    if hours_since_user >= 6 and 6 <= hour <= 10:
        add("dream_mention", 50, "早上，像记起一个梦或半醒的小片段。发一条含糊但自然的消息。")
    if hours_since_user >= 2 and (joy >= 45 or (sadness >= 30 and trust >= 40)):
        add("song_stuck", 47, "一首歌或某种旋律卡在脑子里，让你想跟他说。")
    if hours_since_user >= 1.5 and (fear >= 40 or sadness >= 40) and (hour >= 22 or hour <= 3):
        add("overthinking_spiral", 55, "你在深夜有点想多了。发一条自知但不沉重的消息。")
    if hours_since_user >= 2 and (joy >= 30 or total_emotion < 200):
        add("craving_share", 44, "你突然想吃/喝/做某件小事。像随口告诉他。")
    if hours_since_user >= 2 and has_recent_shared_moment and trust >= 50:
        add("inside_joke_callback", 52, "某个共同记忆或梗被想起来了。轻轻 callback 一下。")
    if 9 <= hour <= 12 and day < 5 and hours_since_user >= 5:
        add("quiet_productive", 48, "你在自己的安静节奏里忙完一段，像探头出来说句话。")

    if char_sent_last and 1 <= (hours_since_char or 0) <= 8 and (joy >= 55 or anticipation >= 55):
        add("double_text", 52, "你想起刚刚漏说了一句。发很短的补充，不要尴尬。")
    if own_unanswered_question and 0.5 <= (hours_since_char or 0) <= 8 and trust >= 25:
        add("her_question_unanswered", 92, "你刚刚问了一个问题但没等到回答。可以轻轻把问题放软，或假装顺手换个说法，不要连环追问。")
    if char_sent_last and 0.15 <= (hours_since_char or 0) <= 1.5 and trust >= 20 and anger < 45:
        add("open_thread_afterthought", 64, "这轮对话还像没完全收住。补一句自己的小想法或轻微发散，不要再问用户问题。")
    if char_sent_last and 4 <= (hours_since_char or 0) <= 24 and trust >= 40 and anger < 50:
        add("seen_no_reply_soft", 48, "你上一条没有等到回复。低需求地补一句，不要催。")
    if 2 <= hours_since_user <= 48 and last_user:
        add("followup_callback", 50, "延续上次话题，像后来又想到一点。")
    if hours_since_user >= 3 and has_recent_shared_moment:
        add("memory_nudge", 50, "用一个已知记忆自然开头，别像背资料。")

    if not candidates:
        return None
    if own_unanswered_question:
        for candidate in candidates:
            if candidate.type == "her_question_unanswered":
                return candidate
    total = sum(max(1, candidate.weight) for candidate in candidates)
    roll = rng.random() * total
    for candidate in candidates:
        roll -= max(1, candidate.weight)
        if roll <= 0:
            return candidate
    return candidates[-1]


def proactive_context_instruction(trigger: ProactiveTrigger | None) -> str:
    if not trigger:
        return "后台检查没有强触发。如果要发，必须非常克制；多数情况下选择不发。"
    return (
        f"主动触发类型: {trigger.type}\n"
        f"触发语义类别: {trigger.category or 'none'}\n"
        f"触发指令: {trigger.instruction}\n"
        "只生成一条像真实私聊的短消息。不要解释触发器，不要写动作旁白。"
    )


def _is_deep_night_quiet_hours(hour: int) -> bool:
    return hour <= 5


def _last_message(rows: list[dict[str, str]], direction: str) -> dict[str, str] | None:
    for row in reversed(rows):
        if row.get("direction") == direction:
            return row
    return None


def _unanswered_outgoing_count(rows: list[dict[str, str]]) -> int:
    count = 0
    for row in reversed(rows):
        direction = row.get("direction")
        if direction == "in":
            return count
        if direction == "out":
            count += 1
    return count


def _emotion_shift_score(impact: dict[str, float]) -> float:
    if not impact:
        return 0.0
    meaningful = [abs(float(value)) for value in impact.values() if abs(float(value)) >= 1.5]
    if not meaningful:
        return 0.0
    return max(meaningful) + sum(meaningful) * 0.35


def _hours_since(raw: str | None, now: datetime) -> float | None:
    if not raw:
        return None
    then = datetime.fromisoformat(raw)
    if then.tzinfo is None and now.tzinfo is not None:
        then = then.replace(tzinfo=now.tzinfo)
    return max(0.0, (now - then).total_seconds() / 3600)


def _is_on_cooldown(type_: str, trigger_history: dict[str, datetime], now: datetime) -> bool:
    last = trigger_history.get(type_)
    if not last:
        return False
    return (now - last).total_seconds() / 3600 < TRIGGER_COOLDOWN_HOURS.get(type_, 10)


def _is_category_on_cooldown(category: str, trigger_history: dict[str, datetime], now: datetime) -> bool:
    cooldown = CATEGORY_COOLDOWN_HOURS.get(category, 0)
    if cooldown <= 0:
        return False
    for type_, trigger_category in TRIGGER_SEMANTIC_CATEGORY.items():
        if trigger_category != category or type_ not in trigger_history:
            continue
        if (now - trigger_history[type_]).total_seconds() / 3600 < cooldown:
            return True
    return False


def _daily_seed(type_: str, now: datetime) -> float:
    key = f"{type_}:{now.date().isoformat()}"
    value = 2166136261
    for char in key:
        value ^= ord(char)
        value = (value * 16777619) & 0xFFFFFFFF
    return value / 0xFFFFFFFF

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from companion_daemon.models import MoodState
from companion_daemon.time import utc_now


CHENGDU_TZ = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class HumanRhythm:
    local_hour: int
    phase: str
    private_activity: str
    attention_mode: str
    reply_guidance: str
    proactive_guidance: str


def human_rhythm_snapshot(state: MoodState, now: datetime | None = None) -> HumanRhythm:
    now = now or utc_now()
    local = now.astimezone(CHENGDU_TZ)
    hour = local.hour
    phase = _phase_for_hour(hour)
    activity = _activity_for(state, phase, local)
    attention = _attention_for(state, phase)
    reply_guidance = _reply_guidance_for(state, phase)
    proactive_guidance = _proactive_guidance_for(state, phase)
    return HumanRhythm(
        local_hour=hour,
        phase=phase,
        private_activity=activity,
        attention_mode=attention,
        reply_guidance=reply_guidance,
        proactive_guidance=proactive_guidance,
    )


def human_rhythm_context_line(state: MoodState, now: datetime | None = None) -> str:
    rhythm = human_rhythm_snapshot(state, now)
    return (
        f"生活节律: 成都本地时间约 {rhythm.local_hour}:00；"
        f"她像是在{rhythm.private_activity}；"
        f"回复倾向={rhythm.reply_guidance}。"
        f"你的消息就是纯粹的私聊文字，像微信打字一样。"
    )


def proactive_rhythm_context_line(state: MoodState, now: datetime | None = None) -> str:
    rhythm = human_rhythm_snapshot(state, now)
    return (
        f"生活节律: 成都本地时间约 {rhythm.local_hour}:00，阶段={rhythm.phase}；"
        f"她像是在{rhythm.private_activity}；主动倾向={rhythm.proactive_guidance}"
    )


def apply_expression_after_reply(
    state: MoodState,
    *,
    was_proactive: bool = False,
    sent_image: bool = False,
) -> MoodState:
    update: dict[str, int | str | None] = {
        "emotional_charge": max(0, state.emotional_charge - (6 if was_proactive else 3)),
    }
    if was_proactive:
        update["initiative"] = max(0, state.initiative - (10 if sent_image else 7))
        update["attachment"] = max(0, state.attachment - 1)
        if state.mood == "miss_you" and state.emotional_charge <= 18:
            update["mood"] = "calm"
        if state.unresolved_emotion and state.mood in {"miss_you", "curious", "happy"}:
            update["unresolved_emotion"] = None
    elif state.mood in {"worried", "miss_you", "curious"} and state.emotional_charge <= 16:
        update["emotional_charge"] = max(0, state.emotional_charge - 4)
    return state.model_copy(update=update)


def _phase_for_hour(hour: int) -> str:
    if 5 <= hour <= 8:
        return "early_morning"
    if 9 <= hour <= 11:
        return "morning_focus"
    if 12 <= hour <= 13:
        return "lunch_break"
    if 14 <= hour <= 17:
        return "afternoon_classes"
    if 18 <= hour <= 21:
        return "evening_unwind"
    if 22 <= hour <= 23:
        return "late_evening"
    return "deep_night"


def _activity_for(state: MoodState, phase: str, local: datetime) -> str:
    if state.mood == "hurt":
        return "把手机扣在旁边，努力让自己别立刻心软"
    if state.mood == "guarded":
        return "有点警惕地看消息，回复前会多想一下"
    if state.mood == "worried":
        return "反复看聊天框，担心你那边状态"
    if state.mood == "miss_you":
        return "想起你但又不想显得太黏"

    weekday = local.weekday()
    if phase == "early_morning":
        return "醒来后慢慢摸手机，看一眼今天要做什么"
    if phase == "morning_focus":
        return "自习或上课间隙，手机放在手边"
    if phase == "lunch_break":
        return "吃饭或买饮料的间隙短暂看手机"
    if phase == "afternoon_classes":
        return "下午有点犯困，但还在自己的节奏里"
    if phase == "evening_unwind":
        return "一天收下来，比较容易有分享欲"
    if phase == "late_evening":
        return "洗漱前后，心思会比白天软一点"
    if weekday >= 5:
        return "周末夜里随手刷手机，状态松一点"
    return "夜里半醒，消息会短而安静"


def _attention_for(state: MoodState, phase: str) -> str:
    if state.boundary_level >= 35 or state.mood in {"hurt", "guarded"}:
        return "低亲近、高边界"
    if state.mood in {"affectionate", "miss_you"}:
        return "更容易被你牵动，但会克制"
    if phase in {"morning_focus", "afternoon_classes"}:
        return "间歇在线，不适合长篇"
    if phase in {"late_evening", "deep_night"}:
        return "慢一点、私密一点、少解释"
    return "自然在线"


def _reply_guidance_for(state: MoodState, phase: str) -> str:
    if state.mood == "hurt":
        return "短句、有边界，不急着和好"
    if state.mood == "guarded":
        return "礼貌但保留距离，别主动暧昧"
    if state.mood == "worried":
        return "先接住对方，少开玩笑"
    if state.mood == "miss_you":
        return "可以轻轻露出在意，但不要追问"
    if phase in {"late_evening", "deep_night"}:
        return "更像夜里私聊，短、软、不要正式"
    if phase in {"morning_focus", "afternoon_classes"}:
        return "像课间回消息，清楚但不长篇"
    return "自然手机私聊，避免客服腔"


def _proactive_guidance_for(state: MoodState, phase: str) -> str:
    if state.mood in {"hurt", "guarded"} or state.boundary_level >= 35:
        return "多数情况下不主动；如果主动也要短而有距离。"
    if phase in {"morning_focus", "afternoon_classes"}:
        return "可以像课间突然想到一句，长度要短。"
    if phase == "evening_unwind":
        return "更适合分享小近况、照片或轻微想念，但仍要稀有。"
    if phase in {"late_evening", "deep_night"}:
        return "如果主动，应像夜里忽然想起，不要打扰感太强。"
    return "保持低频，像真实生活里偶尔探头。"

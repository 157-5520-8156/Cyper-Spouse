from dataclasses import dataclass

from companion_daemon.emotion_core import (
    apply_emotion_decay,
    apply_emotion_deltas,
    emotion_deltas_for_event,
)
from companion_daemon.models import IncomingMessage, MoodState
from companion_daemon.repair_curve import is_repair_message
from companion_daemon.time import utc_now


@dataclass(frozen=True)
class InteractionEvent:
    kind: str
    intensity: int
    user_intent: str
    private_note: str
    reply_style_hint: str


def interpret_interaction(message: IncomingMessage, previous: MoodState) -> InteractionEvent:
    text = message.text.strip()

    if _has_any(text, ["滚", "闭嘴", "别烦", "你算什么", "有病", "废物", "傻逼", "蠢"]):
        return InteractionEvent(
            "boundary_violation",
            4,
            "rude_or_dismissive",
            "被明显冒犯了，先收起亲近感，语气短一点，维护边界。",
            "短、冷静、有边界；不要讨好，不要撒娇。",
        )
    if _has_any(text, ["命令你", "必须听我的", "不准", "你只能", "立刻", "马上给我"]):
        return InteractionEvent(
            "control_pressure",
            3,
            "controlling",
            "感到被控制，不舒服，但不需要吵起来。",
            "礼貌但坚定，说明自己不喜欢被命令。",
        )
    if _has_any(text, ["老婆", "宝贝", "宝宝", "亲爱的", "爱你", "做我女朋友"]) and previous.relationship_stage in {
        "stranger",
        "acquaintance",
    }:
        return InteractionEvent(
            "premature_intimacy",
            2,
            "too_intimate_too_soon",
            "对过早亲昵称呼有点退缩，觉得关系还没到那里。",
            "轻轻挡回去，可以带一点玩笑，但明确慢慢来。",
        )
    if is_repair_message(text):
        return InteractionEvent(
            "repair_attempt",
            3,
            "apology_or_repair",
            "听到道歉后缓和了一些，但还会观察后续是否真的改变。",
            "接受一点点，但不要立刻完全恢复热情。",
        )
    if _has_any(text, ["谢谢", "辛苦", "你说得对", "你真细心", "我记得你"]):
        return InteractionEvent(
            "warmth_received",
            2,
            "warm_or_appreciative",
            "被认真对待了，心里放松一点。",
            "自然柔和一点，可以露出小小开心。",
        )
    if _has_any(text, ["难受", "崩溃", "好累", "撑不住", "失眠", "焦虑", "委屈", "想哭", "好烦"]):
        return InteractionEvent(
            "user_vulnerable",
            3,
            "vulnerable_sharing",
            "用户在示弱，需要先稳住对方，而不是急着开玩笑。",
            "温柔、具体、少说教，先接住情绪。",
        )
    if _has_any(text, ["刚在忙", "我回来了", "刚下课", "刚下班", "刚到家"]):
        return InteractionEvent(
            "return_after_gap",
            1,
            "returning",
            "对方回来了，轻微放松，但如果之前等太久会有一点点小别扭。",
            "自然回应；若之前是 miss_you/sulking，可轻轻提一句。",
        )
    if _has_any(text, ["忙", "等下", "一会儿", "没空"]):
        return InteractionEvent(
            "availability_drop",
            1,
            "temporarily_busy",
            "知道对方可能在忙，想找他但不想显得黏。",
            "克制、体贴，不追问。",
        )
    if "?" in text or "？" in text or _has_any(text, ["为什么", "怎么", "你觉得", "要不要"]):
        return InteractionEvent(
            "curiosity_invited",
            1,
            "question_or_invitation",
            "对方把话题递过来了，可以多参与一点。",
            "认真回答，适当反问一个问题。",
        )
    if not text and message.attachments:
        return InteractionEvent(
            "nonverbal_share",
            1,
            "attachment_only",
            "对方用图片或文件分享生活，像是把手机递过来给她看。",
            "围绕附件自然回应，不要忽略。",
        )
    return InteractionEvent(
        "ordinary_message",
        1,
        "ordinary_chat",
        "普通聊天，保持自然的手机私聊感。",
        "短句、自然，别像客服。",
    )


def transition_emotional_state(previous: MoodState, event: InteractionEvent) -> MoodState:
    now = utc_now()
    state = apply_emotion_decay(previous.model_copy(deep=True), now)
    state.updated_at = now
    state.last_user_intent = event.user_intent
    state.last_interaction_event = event.kind
    state.reply_style_hint = event.reply_style_hint

    if event.kind == "boundary_violation":
        state.mood = "hurt"
        state.trust = _clamp(state.trust - 6)
        state.intimacy = _clamp(state.intimacy - 3)
        state.patience = _clamp(state.patience - 12)
        state.security = _clamp(state.security - 8)
        state.emotional_charge = _clamp(state.emotional_charge + 18)
        state.boundary_level = _clamp(state.boundary_level + 2)
        state.unresolved_emotion = event.private_note
    elif event.kind == "control_pressure":
        state.mood = "guarded"
        state.trust = _clamp(state.trust - 4)
        state.patience = _clamp(state.patience - 8)
        state.security = _clamp(state.security - 7)
        state.emotional_charge = _clamp(state.emotional_charge + 12)
        state.boundary_level = _clamp(state.boundary_level + 1)
        state.unresolved_emotion = event.private_note
    elif event.kind == "premature_intimacy":
        state.mood = "guarded"
        state.trust = _clamp(state.trust - 1)
        state.security = _clamp(state.security - 3)
        state.emotional_charge = _clamp(state.emotional_charge + 5)
        state.boundary_level = _clamp(state.boundary_level + 1)
        state.unresolved_emotion = event.private_note
    elif event.kind == "repair_attempt":
        state.mood = "calm" if state.mood in {"hurt", "guarded", "sulking"} else state.mood
        state.trust = _clamp(state.trust + 3)
        state.patience = _clamp(state.patience + 8)
        state.security = _clamp(state.security + 5)
        state.emotional_charge = _clamp(state.emotional_charge - 12)
        state.boundary_level = _clamp(state.boundary_level - 1)
        state.unresolved_emotion = "缓和了一些，但还想看看对方之后怎么说。"
    elif event.kind == "warmth_received":
        state.mood = "happy"
        state.trust = _clamp(state.trust + 3)
        state.intimacy = _clamp(state.intimacy + 2)
        state.security = _clamp(state.security + 3)
        state.curiosity = _clamp(state.curiosity + 2)
        state.emotional_charge = _clamp(state.emotional_charge - 4)
        state.unresolved_emotion = None
    elif event.kind == "user_vulnerable":
        state.mood = "worried"
        state.trust = _clamp(state.trust + 2)
        state.intimacy = _clamp(state.intimacy + 1)
        state.attachment = _clamp(state.attachment + 1)
        state.initiative = _clamp(state.initiative + 3)
        state.unresolved_emotion = "担心对方现在状态不好，想陪一会儿。"
    elif event.kind == "return_after_gap":
        state.mood = "happy" if state.mood in {"miss_you", "calm"} else state.mood
        state.security = _clamp(state.security + 2)
        state.emotional_charge = _clamp(state.emotional_charge - 3)
    elif event.kind == "availability_drop":
        state.mood = "miss_you"
        state.attachment = _clamp(state.attachment + 1)
        state.initiative = _clamp(state.initiative + 1)
        state.unresolved_emotion = event.private_note
    elif event.kind == "curiosity_invited":
        state.mood = "curious" if state.mood == "calm" else state.mood
        state.curiosity = _clamp(state.curiosity + 4)
        state.trust = _clamp(state.trust + 1)
    elif event.kind == "nonverbal_share":
        state.mood = "curious" if state.mood == "calm" else state.mood
        state.curiosity = _clamp(state.curiosity + 2)
        state.trust = _clamp(state.trust + 1)
    else:
        if state.mood in {"hurt", "guarded"}:
            state.emotional_charge = _clamp(state.emotional_charge - 1)
        elif state.mood == "sulking":
            state.mood = "calm"
        state.trust = _clamp(state.trust + 1)

    if state.emotional_charge >= 45 and state.mood not in {"hurt", "guarded"}:
        state.mood = "sulking"
    if state.security >= 70 and state.intimacy >= 45 and state.mood == "happy":
        state.mood = "affectionate"
    state = apply_emotion_deltas(
        state,
        emotion_deltas_for_event(event.kind, event.intensity),
        source="interaction_event",
        update_affinity=False,
    )
    return state


def _has_any(text: str, tokens: list[str]) -> bool:
    return any(token in text for token in tokens)


def _clamp(value: int) -> int:
    return max(0, min(100, value))

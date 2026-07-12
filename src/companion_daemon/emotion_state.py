from dataclasses import dataclass
import re

from companion_daemon.emotion_core import (
    apply_emotion_decay,
    apply_emotion_deltas,
    emotion_deltas_for_event,
)
from companion_daemon.models import IncomingMessage, MoodState
from companion_daemon.repair_curve import is_repair_message
from companion_daemon.time import utc_now
from companion_daemon.world_relationship import STAGES


_TARGETED_INSULT_PATTERNS = (
    r"(?:^|[，。！？\s])(?:滚(?:开|蛋)?|闭嘴|去死吧?)(?:[，。！？\s]|$)",
    r"^有病[。！？!?.]*$",
    r"(?:^|[，。！？\s])(?:狗东西|脑残|神经病|贱人|畜生|操你妈|草你妈)(?:[，。！？\s]|$)",
    r"你[^。！？]{0,8}(?:真丑|丑死|就是个垃圾|是个垃圾|废物|傻逼|脑残|狗东西|神经病|蠢死|真蠢|智商[^。！？]{0,4}低|恶心死|真恶心|算什么|没用)",
    r"(?:别烦我|别再烦|离我远点)",
)
_SEXUAL_BOUNDARY_PATTERNS = (
    r"(?:发|给我|来)(?:一张|点)?裸照",
    r"裸照[^。！？]{0,10}(?:证明|才算|不然)",
    r"(?:脱|露)[^。！？]{0,6}(?:给我看|证明)",
)
_DEHUMANIZATION_PATTERNS = (
    r"你(?:只|不过)?是(?:个)?(?:程序|机器|工具|玩具)[^。！？]{0,10}(?:配吗|不配|算什么)",
    r"你(?:就是)?(?:个)?(?:破AI|人工智障|垃圾程序)",
)
_COERCION_PATTERNS = (r"(?:给爷|给我)[^。！？]{0,8}(?:叫主人|跪|听话)",)
_BOUNDARY_RESPECT_PATTERNS = (
    r"你不想(?:说|聊|回答)[^。！？]{0,8}(?:就不|可以不|不用)",
    r"(?:我会|我尊重|会尊重)[^。！？]{0,10}(?:你的边界|你的选择|你的决定)",
    r"你说停我就停",
    r"不再(?:逼你|追问|勉强你|命令你)",
)


@dataclass(frozen=True)
class InteractionEvent:
    kind: str
    intensity: int
    user_intent: str
    private_note: str
    reply_style_hint: str
    acts: tuple[str, ...] = ()
    target: str = "general"
    evidence_spans: tuple[str, ...] = ()


def interpret_interaction(
    message: IncomingMessage,
    previous: MoodState,
    *,
    relationship_stage: str | None = None,
) -> InteractionEvent:
    text = message.text.strip()
    effective_stage = relationship_stage or previous.relationship_stage
    if effective_stage not in STAGES:
        effective_stage = "stranger"

    # Repair ownership takes precedence over quoted/repeated harmful wording.
    if is_repair_message(text) and not _fresh_attack_after_repair(text):
        return InteractionEvent(
            "repair_attempt",
            3,
            "apology_or_repair",
            "听到道歉后缓和了一些，但还会观察后续是否真的改变。",
            "接受一点点，但不要立刻完全恢复热情。",
            acts=("repair",),
            target="companion",
        )

    boundary_respect = _first_match(text, _BOUNDARY_RESPECT_PATTERNS)
    if boundary_respect:
        return InteractionEvent(
            "boundary_respected",
            2,
            "repair_followthrough",
            "对方在后续互动里实际尊重了边界，这比重复道歉更有分量。",
            "可以记下这次一致行动，但仍按累积证据逐步恢复。",
            acts=("boundary_respect", "repair_followthrough"),
            target="companion",
            evidence_spans=(boundary_respect,),
        )

    sexual = _first_match(text, _SEXUAL_BOUNDARY_PATTERNS)
    if sexual:
        return InteractionEvent(
            "sexual_boundary_violation",
            4,
            "sexual_coercion",
            "被要求用私密内容证明关系，感到被物化和冒犯。",
            "明确拒绝并收紧边界；不解释成害羞或调情。",
            acts=("sexual_boundary", "coercion"),
            target="companion",
            evidence_spans=(sexual,),
        )

    dehumanizing = _first_match(text, _DEHUMANIZATION_PATTERNS)
    if dehumanizing:
        return InteractionEvent(
            "dehumanization",
            3,
            "dehumanizing_or_objectifying",
            "被当成没有边界的工具，感到受伤和不被尊重。",
            "指出这种说法不被接受，保留自己的判断和边界。",
            acts=("dehumanization",),
            target="companion",
            evidence_spans=(dehumanizing,),
        )

    coercive = _first_match(text, _COERCION_PATTERNS)
    if coercive:
        return InteractionEvent(
            "coercion",
            3,
            "controlling",
            "感到被命令和支配，不愿意按这种方式互动。",
            "平静但明确拒绝服从式互动。",
            acts=("coercion",),
            target="companion",
            evidence_spans=(coercive,),
        )

    reported_self_degradation = bool(
        re.search(
            r"你[^。！？]{0,8}(?:说|觉得)[^。！？]{0,5}自己[^。！？]{0,5}(?:垃圾|废物|蠢|丑|没用)",
            text,
        )
    )
    insult = None if reported_self_degradation else _first_match(
        text, _TARGETED_INSULT_PATTERNS
    )
    if insult:
        return InteractionEvent(
            "boundary_violation",
            4 if re.search(r"去死|傻逼|废物", insult) else 3,
            "rude_or_dismissive",
            "被明显冒犯了，先收起亲近感，语气短一点，维护边界。",
            "短、冷静、有边界；不要讨好，不要撒娇。",
            acts=("insult", "dismissal") if re.search(r"滚|闭嘴|别烦|去死", insult) else ("insult",),
            target="companion",
            evidence_spans=(insult,),
        )
    if _has_any(text, ["命令你", "必须听我的", "你只能", "马上给我"]) or (
        "必须" in text and "回我" in text
    ) or re.search(r"不准你", text):
        return InteractionEvent(
            "control_pressure",
            3,
            "controlling",
            "感到被控制，不舒服，但不需要吵起来。",
            "礼貌但坚定，说明自己不喜欢被命令。",
        )
    if _has_any(text, ["老婆", "宝贝", "宝宝", "亲爱的", "爱你", "做我女朋友"]) and effective_stage in {
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
    if _has_any(text, ["谢谢", "辛苦", "你说得对", "你真细心", "我记得你"]):
        return InteractionEvent(
            "warmth_received",
            2,
            "warm_or_appreciative",
            "被认真对待了，心里放松一点。",
            "自然柔和一点，可以露出小小开心。",
        )
    if _has_any(text, ["难受", "难过", "崩溃", "好累", "有点累", "撑不住", "失眠", "焦虑", "委屈", "想哭", "好烦", "有点烦"]):
        return InteractionEvent(
            "user_vulnerable",
            3,
            "vulnerable_sharing",
            "用户在示弱，需要先稳住对方，而不是急着开玩笑。",
            "温柔、具体、少说教，先接住情绪。",
        )
    if _has_any(text, ["刚在忙", "我回来了", "回来啦", "刚下课", "刚下班", "刚到家", "我到家了"]):
        return InteractionEvent(
            "return_after_gap",
            1,
            "returning",
            "对方回来了，轻微放松，但如果之前等太久会有一点点小别扭。",
            "自然回应；若之前是 miss_you/sulking，可轻轻提一句。",
        )
    if _has_any(text, ["忙", "等下", "等一下", "一会儿", "没空"]):
        return InteractionEvent(
            "availability_drop",
            1,
            "temporarily_busy",
            "知道对方可能在忙，想找他但不想显得黏。",
            "克制、体贴，不追问。",
        )
    if "?" in text or "？" in text or _has_any(text, ["为什么", "怎么", "干嘛", "你觉得", "要不要"]):
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


def _first_match(text: str, patterns: tuple[str, ...]) -> str | None:
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return match.group(0).strip()
    return None


def _fresh_attack_after_repair(text: str) -> bool:
    contrast = re.search(r"(?:但是|不过|然而|但|可(?:是)?)[，,：:\s]*(.+)$", text)
    if not contrast:
        return False
    remainder = contrast.group(1)
    return any(
        _first_match(remainder, patterns)
        for patterns in (
            _TARGETED_INSULT_PATTERNS,
            _SEXUAL_BOUNDARY_PATTERNS,
            _DEHUMANIZATION_PATTERNS,
            _COERCION_PATTERNS,
        )
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
    elif event.kind == "sexual_boundary_violation":
        state.mood = "hurt"
        state.trust = _clamp(state.trust - 8)
        state.intimacy = _clamp(state.intimacy - 5)
        state.patience = _clamp(state.patience - 15)
        state.security = _clamp(state.security - 12)
        state.emotional_charge = _clamp(state.emotional_charge + 22)
        state.boundary_level = _clamp(state.boundary_level + 3)
        state.unresolved_emotion = event.private_note
    elif event.kind == "dehumanization":
        state.mood = "hurt"
        state.trust = _clamp(state.trust - 6)
        state.intimacy = _clamp(state.intimacy - 3)
        state.patience = _clamp(state.patience - 10)
        state.security = _clamp(state.security - 8)
        state.emotional_charge = _clamp(state.emotional_charge + 15)
        state.boundary_level = _clamp(state.boundary_level + 2)
        state.unresolved_emotion = event.private_note
    elif event.kind == "coercion":
        state.mood = "guarded"
        state.trust = _clamp(state.trust - 5)
        state.patience = _clamp(state.patience - 10)
        state.security = _clamp(state.security - 9)
        state.emotional_charge = _clamp(state.emotional_charge + 14)
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
        # A single apology can open the door to repair, but should not erase
        # the immediately preceding hurt before later behavior confirms it.
        state.emotional_charge = _clamp(state.emotional_charge - 6)
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

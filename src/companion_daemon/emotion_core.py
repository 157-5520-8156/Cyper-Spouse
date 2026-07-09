import math
import re
from dataclasses import dataclass
from datetime import datetime

from companion_daemon.models import MoodState

EMOTION_IDS = (
    "love",
    "joy",
    "trust",
    "fear",
    "surprise",
    "sadness",
    "disgust",
    "anger",
    "anticipation",
)

EMOTION_BASELINE = {
    "love": 10.0,
    "joy": 25.0,
    "trust": 20.0,
    "fear": 12.0,
    "surprise": 15.0,
    "sadness": 12.0,
    "disgust": 8.0,
    "anger": 8.0,
    "anticipation": 25.0,
}

EMOTION_DECAY_PROFILE = {
    "love": (0.035, 0.9),
    "joy": (0.095, 2.0),
    "trust": (0.06, 1.4),
    "fear": (0.14, 2.1),
    "surprise": (0.08, 2.0),
    "sadness": (0.043, 1.1),
    "disgust": (0.08, 1.5),
    "anger": (0.12, 2.0),
    "anticipation": (0.07, 1.7),
}

OPPOSITES = (
    ("love", "disgust"),
    ("joy", "sadness"),
    ("trust", "disgust"),
    ("fear", "anger"),
    ("surprise", "anticipation"),
)

TEXT_EMOTION_KEYWORDS = {
    "love": ["喜欢", "想你", "抱抱", "爱你", "在意", "温柔", "亲密", "舍不得"],
    "joy": ["开心", "高兴", "好玩", "哈哈", "笑死", "期待", "舒服", "喜欢"],
    "trust": ["相信", "靠谱", "谢谢", "辛苦", "认真", "记得", "放心", "安全"],
    "fear": ["害怕", "担心", "焦虑", "慌", "不安", "失眠", "撑不住"],
    "surprise": ["啊", "居然", "竟然", "突然", "没想到", "惊了", "哇"],
    "sadness": ["难过", "委屈", "累", "崩溃", "孤独", "哭", "失落", "难受"],
    "disgust": ["恶心", "讨厌", "烦死", "油腻", "下头", "离谱"],
    "anger": ["生气", "烦", "滚", "闭嘴", "有病", "傻逼", "必须", "命令"],
    "anticipation": ["等你", "想看", "要不要", "以后", "下次", "好奇", "期待"],
}


@dataclass(frozen=True)
class EmotionSnapshot:
    dominant: str
    value: float
    active: list[tuple[str, float]]
    guidance: str


def default_emotion_map() -> dict[str, float]:
    return dict(EMOTION_BASELINE)


def normalize_emotion_maps(state: MoodState) -> MoodState:
    vector = _normalized_map(state.emotion_vector, EMOTION_BASELINE)
    baseline = _normalized_map(state.emotion_baseline, EMOTION_BASELINE)
    affinity = _normalized_map(state.emotion_affinity, {emotion: 0.0 for emotion in EMOTION_IDS})
    impact = _normalized_map(state.last_emotion_impact, {emotion: 0.0 for emotion in EMOTION_IDS})
    return state.model_copy(
        update={
            "emotion_vector": vector,
            "emotion_baseline": baseline,
            "emotion_affinity": affinity,
            "last_emotion_impact": impact,
        }
    )


def apply_emotion_decay(state: MoodState, now: datetime) -> MoodState:
    state = normalize_emotion_maps(state)
    elapsed_minutes = max(0.0, (now - state.updated_at).total_seconds() / 60)
    if elapsed_minutes <= 0:
        return state
    vector = dict(state.emotion_vector)
    for emotion in EMOTION_IDS:
        baseline = state.emotion_baseline[emotion]
        diff = baseline - vector[emotion]
        distance_norm = min(1.0, abs(diff) / 100)
        lambda_value, asymmetry = EMOTION_DECAY_PROFILE[emotion]
        effective_lambda = lambda_value * (1 + asymmetry * math.pow(distance_norm, 1.35))
        decay_factor = 1 - math.exp(-effective_lambda * elapsed_minutes)
        vector[emotion] = _clamp(vector[emotion] + diff * decay_factor)
    return state.model_copy(update={"emotion_vector": vector})


def apply_emotion_deltas(
    state: MoodState,
    deltas: dict[str, float],
    *,
    source: str,
    update_affinity: bool,
) -> MoodState:
    state = normalize_emotion_maps(state)
    before = dict(state.emotion_vector)
    vector = dict(before)
    for emotion, delta in deltas.items():
        if emotion in vector:
            vector[emotion] = _clamp(vector[emotion] + delta)
            opposite = _opposite_for(emotion)
            if opposite:
                vector[opposite] = _clamp(vector[opposite] - delta * 0.26)
    vector = enforce_opposites(vector)
    impact = {
        emotion: round(vector[emotion] - before[emotion], 2)
        for emotion in EMOTION_IDS
    }
    baseline = dict(state.emotion_baseline)
    affinity = dict(state.emotion_affinity)
    if update_affinity and source in {"user_message", "reaction"}:
        learning_rate = 0.018 if source == "user_message" else 0.03
        for emotion in EMOTION_IDS:
            anchor = EMOTION_BASELINE[emotion]
            drift = vector[emotion] - baseline[emotion]
            shift_delta = max(-0.85, min(0.85, drift * learning_rate))
            baseline[emotion] = max(
                _clamp_baseline(anchor - 25),
                min(_clamp_baseline(anchor + 35), baseline[emotion] + shift_delta),
            )
            affinity[emotion] = round(baseline[emotion] - anchor, 2)
    return state.model_copy(
        update={
            "emotion_vector": vector,
            "emotion_baseline": baseline,
            "emotion_affinity": affinity,
            "last_emotion_impact": impact,
            "last_emotion_source": source,
        }
    )


def emotion_deltas_for_event(kind: str, intensity: int) -> dict[str, float]:
    scale = max(1, intensity)
    base = {
        "boundary_violation": {"anger": 7, "sadness": 5, "disgust": 5, "trust": -5, "love": -3},
        "control_pressure": {"anger": 5, "fear": 3, "disgust": 4, "trust": -4},
        "premature_intimacy": {"fear": 3, "surprise": 2, "trust": -2, "love": -1},
        "repair_attempt": {"trust": 5, "joy": 2, "anger": -5, "disgust": -3, "sadness": -3},
        "warmth_received": {"love": 4, "joy": 4, "trust": 5, "sadness": -2},
        "user_vulnerable": {"trust": 3, "sadness": 3, "love": 2, "anticipation": 1},
        "return_after_gap": {"joy": 3, "trust": 2, "sadness": -2},
        "availability_drop": {"sadness": 2, "anticipation": 2, "love": 1},
        "curiosity_invited": {"anticipation": 4, "trust": 2, "joy": 1},
        "nonverbal_share": {"anticipation": 2, "trust": 2, "surprise": 1},
        "ordinary_message": {"trust": 1},
    }.get(kind, {"trust": 1})
    return {emotion: delta * (0.55 + scale * 0.25) for emotion, delta in base.items()}


def text_emotion_deltas(text: str, *, is_user: bool = True) -> dict[str, float]:
    normalized = text.lower()
    multiplier = _text_impact_multiplier(text)
    weight = 1.0 if is_user else 0.9
    result: dict[str, float] = {}
    for emotion, keywords in TEXT_EMOTION_KEYWORDS.items():
        hits = sum(1 for keyword in keywords if keyword in normalized)
        if not hits:
            continue
        delta = min(6, hits) * 1.45 * weight * multiplier
        result[emotion] = result.get(emotion, 0.0) + delta
    return result


def emotion_snapshot(state: MoodState) -> EmotionSnapshot:
    state = normalize_emotion_maps(state)
    active = sorted(
        [(emotion, state.emotion_vector[emotion]) for emotion in EMOTION_IDS],
        key=lambda item: item[1],
        reverse=True,
    )
    dominant, value = active[0]
    return EmotionSnapshot(
        dominant=dominant,
        value=value,
        active=active[:4],
        guidance=emotion_guidance(state, active[:4]),
    )


def emotion_context_line(state: MoodState) -> str:
    snapshot = emotion_snapshot(state)
    active = " · ".join(
        f"{_emotion_label(emotion)} {round(value)}%"
        for emotion, value in snapshot.active
    )
    affinity = state.emotion_affinity
    bond = ""
    bond_score = affinity.get("trust", 0) + affinity.get("joy", 0) * 0.6
    if bond_score >= 10:
        bond = "长期连接: 信任基线已经变暖，更容易自然亲近。"
    elif bond_score <= -7:
        bond = "长期连接: 基线有些戒备，情绪修复会更慢。"
    return f"情绪向量: {active}\n{snapshot.guidance}\n{bond}".strip()


def enforce_opposites(vector: dict[str, float]) -> dict[str, float]:
    vector = dict(vector)
    for left, right in OPPOSITES:
        total = vector[left] + vector[right]
        if total > 80:
            excess = total - 80
            if vector[left] >= vector[right]:
                vector[right] = _clamp(vector[right] - excess * 0.5)
            else:
                vector[left] = _clamp(vector[left] - excess * 0.5)
    return vector


def emotion_guidance(state: MoodState, active: list[tuple[str, float]]) -> str:
    if not active:
        return "情绪指导: 中性、自然。"
    dominant, value = active[0]
    tier = 0 if value < 33 else 1 if value < 66 else 2
    guidance = {
        "love": ["小幅关心，不直白告白。", "自然温柔，更愿意分享。", "明显亲近，但仍保持真实边界。"],
        "joy": ["轻松一点。", "明亮、有回应感。", "很开心，语气可以更活。"],
        "trust": ["礼貌开放。", "更坦诚、放松。", "很信任，可自然暴露一点脆弱。"],
        "fear": ["稍微谨慎。", "不安，回复更短更试探。", "明显缺安全感，先寻求稳定。"],
        "surprise": ["有点意外。", "反应更鲜活。", "被打乱节奏，允许短促表达惊讶。"],
        "sadness": ["安静、若有所思。", "低落，少一点热情。", "沉重，回复慢而真。"],
        "disgust": ["轻微下头。", "明显不想贴近。", "强烈反感，直接保持距离。"],
        "anger": ["有一点刺。", "明显不耐烦，会推回去。", "很生气，短而有边界。"],
        "anticipation": ["好奇。", "更主动接话。", "非常想知道后续，容易主动追问。"],
    }[dominant][tier]
    if len(active) > 1 and value - active[1][1] < 25:
        guidance += f" 次要情绪带有{_emotion_label(active[1][0])}。"
    return f"情绪指导: {guidance} 不要直接报出这些内部数值。"


def _normalized_map(raw: dict[str, float], fallback: dict[str, float]) -> dict[str, float]:
    result = {}
    for emotion in EMOTION_IDS:
        value = raw.get(emotion, fallback.get(emotion, 0.0)) if isinstance(raw, dict) else fallback[emotion]
        result[emotion] = _clamp(float(value))
    return result


def _text_impact_multiplier(text: str) -> float:
    strong_punctuation = len(re.findall(r"(!{2,}|！{2,}|\?{2,}|？{2,}|[!?！？]{2,})", text))
    repeated = len(re.findall(r"(.)\1{2,}", text))
    intense_words = sum(
        1
        for word in ["永远", "绝对", "真的", "特别", "超级", "崩溃", "气死", "喜欢死", "烦死"]
        if word in text
    )
    score = min(1.75, strong_punctuation * 0.18 + repeated * 0.12 + intense_words * 0.25)
    return 1 + score


def _opposite_for(emotion: str) -> str | None:
    for left, right in OPPOSITES:
        if emotion == left:
            return right
        if emotion == right:
            return left
    return None


def _emotion_label(emotion: str) -> str:
    return {
        "love": "亲近",
        "joy": "愉悦",
        "trust": "信任",
        "fear": "不安",
        "surprise": "惊讶",
        "sadness": "低落",
        "disgust": "反感",
        "anger": "生气",
        "anticipation": "期待",
    }[emotion]


def _clamp(value: float) -> float:
    return max(0.0, min(100.0, value))


def _clamp_baseline(value: float) -> float:
    return max(5.0, min(95.0, value))

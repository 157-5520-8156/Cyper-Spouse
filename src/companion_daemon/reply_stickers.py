from companion_daemon.models import IncomingMessage, MoodState
from companion_daemon.stickers import Sticker, StickerCatalog


MOOD_INTENT = {
    "happy": "greeting",
    "sulking": "soft_complaint",
    "miss_you": "reaching_out",
    "jealous_soft": "mild_jealousy",
    "sleepy": "tired",
    "hurt": "soft_complaint",
    "guarded": "boundary",
    "affectionate": "greeting",
}


def choose_reply_sticker(
    catalog: StickerCatalog | None,
    state: MoodState,
    message: IncomingMessage,
    *,
    suggested_reaction: str | None = None,
) -> Sticker | None:
    if not catalog:
        return None
    intent = _intent_for_message(state, message.text, suggested_reaction)
    if not intent:
        return None
    return catalog.choose(state.mood, intent=intent)


def _intent_for_message(
    state: MoodState,
    text: str,
    suggested_reaction: str | None,
) -> str | None:
    mood_intent = MOOD_INTENT.get(state.mood)
    impact = state.last_emotion_impact or {}
    joy = impact.get("joy", 0) + impact.get("trust", 0) * 0.5
    sadness = impact.get("sadness", 0)
    anger = impact.get("anger", 0) + impact.get("disgust", 0)
    normalized = text.lower()

    if state.mood in {"guarded", "hurt"} and (anger >= 4 or state.boundary_level >= 35):
        return "boundary"
    if state.mood in {"miss_you", "affectionate"} and state.attachment >= 12:
        return mood_intent
    if state.mood == "sleepy":
        return "tired"
    if any(token in normalized for token in ["哈哈", "笑死", "开心", "好玩", "可爱"]):
        return "teasing" if suggested_reaction in {"haha", "star"} else "greeting"
    if any(token in normalized for token in ["累", "难受", "崩溃", "不开心", "失眠"]):
        return "comfort"
    if state.mood == "sulking":
        if any(token in normalized for token in ["语气", "口气", "生气", "不开心", "吃醋", "阴阳怪气", "怪怪"]):
            return "soft_complaint"
        if anger >= 5:
            return "soft_complaint"
        return None
    if state.mood in {"hurt", "guarded"}:
        if anger >= 5 or state.boundary_level >= 35:
            return "boundary" if state.mood == "guarded" else "soft_complaint"
        return None
    if joy >= 5 and suggested_reaction in {"heart", "like", "star", "haha"}:
        return "teasing" if suggested_reaction == "haha" else "greeting"
    if sadness >= 5 and state.trust >= 25:
        return "comfort"
    if anger >= 5:
        return "soft_complaint"
    return mood_intent if state.emotional_charge >= 18 else None

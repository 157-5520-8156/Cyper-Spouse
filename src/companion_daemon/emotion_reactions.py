from dataclasses import dataclass

from companion_daemon.emotion_core import text_emotion_deltas
from companion_daemon.models import MoodState


# OneBot/NapCat `set_msg_emoji_like` accepts unicode code points in decimal as
# emoji ids (e.g. 128077 = thumbs-up). Keep the map here so the abstract
# reaction ids stay adapter-agnostic while QQ gets a concrete emoji.
QQ_EMOJI_IDS = {
    "heart": "10084",   # 红心
    "haha": "128514",   # 笑哭
    "wow": "128558",    # 惊讶
    "sad": "128546",    # 流泪
    "fire": "128293",   # 火
    "like": "128077",   # 赞
    "star": "11088",    # 星星
    "bolt": "9889",     # 闪电
}


def qq_emoji_id(reaction_id: str | None) -> str | None:
    if not reaction_id:
        return None
    return QQ_EMOJI_IDS.get(reaction_id)


REACTION_EMOTION_MAP = {
    "heart": {"love": 2.5, "joy": 1.2, "trust": 1.0, "sadness": -0.8, "disgust": -0.6},
    "haha": {"joy": 2.1, "surprise": 0.9, "sadness": -1.0},
    "wow": {"surprise": 2.0, "anticipation": 1.0},
    "sad": {"sadness": 2.1, "joy": -1.1, "trust": -0.7},
    "fire": {"anticipation": 1.8, "anger": 0.8, "joy": 0.6},
    "like": {"trust": 2.0, "joy": 0.9, "disgust": -0.8},
    "star": {"love": 0.8, "joy": 1.6, "trust": 1.2, "anticipation": 0.7},
    "bolt": {"surprise": 1.9, "anticipation": 1.2, "fear": 0.5},
}


@dataclass(frozen=True)
class SuggestedReaction:
    reaction_id: str
    probability: float
    magnitude: float


def select_character_reaction(user_message: str, state: MoodState) -> SuggestedReaction | None:
    deltas = text_emotion_deltas(user_message, is_user=True)
    if not deltas:
        return None
    magnitude = sum(abs(value) for value in deltas.values())
    if magnitude < 2.0:
        return None

    best_id: str | None = None
    best_score = 0.0
    for reaction_id, influences in REACTION_EMOTION_MAP.items():
        score = sum(deltas.get(emotion, 0.0) * weight for emotion, weight in influences.items())
        if score > best_score:
            best_score = score
            best_id = reaction_id

    if best_score < 0.8 or not best_id:
        return None

    emotion = state.emotion_vector
    probability = 0.28
    if magnitude > 8:
        probability += 0.12
    elif magnitude > 4:
        probability += 0.06
    if best_score > 4:
        probability += 0.08
    if (
        emotion.get("joy", 0) > 45
        or emotion.get("love", 0) > 45
        or emotion.get("anticipation", 0) > 50
    ):
        probability += 0.08
    if emotion.get("anger", 0) > 50 or emotion.get("disgust", 0) > 45:
        probability -= 0.08
    if emotion.get("sadness", 0) > 55:
        probability -= 0.05
    return SuggestedReaction(best_id, max(0.05, min(0.65, probability)), magnitude)

from companion_daemon.character import CharacterProfile
from companion_daemon.emotion_core import EMOTION_BASELINE, EMOTION_IDS, enforce_opposites
from companion_daemon.models import MoodState


MBTI_TRAIT_BASELINE_DELTA: dict[str, dict[str, float]] = {
    "E": {"joy": 8, "trust": 5, "anticipation": 6, "sadness": -3, "fear": -2, "love": 4},
    "I": {"joy": -4, "trust": 2, "anticipation": -3, "sadness": 3, "fear": 2, "surprise": -1},
    "N": {"anticipation": 6, "surprise": 4, "fear": 1, "trust": -1},
    "S": {"trust": 3, "anticipation": -2, "surprise": -2},
    "T": {"trust": -2, "disgust": 2, "anger": 2, "sadness": -1, "love": -4},
    "F": {"trust": 5, "joy": 3, "sadness": 2, "anger": -1, "disgust": -1, "love": 8},
    "J": {"anticipation": 2, "trust": 2, "surprise": -2},
    "P": {"surprise": 4, "anticipation": 2, "trust": -1},
}


def extract_mbti(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    letters = "".join(char for char in value.upper() if char in "EINSFTJP")
    for index in range(0, max(0, len(letters) - 3)):
        candidate = letters[index : index + 4]
        if (
            candidate[0] in "EI"
            and candidate[1] in "NS"
            and candidate[2] in "TF"
            and candidate[3] in "JP"
        ):
            return candidate
    return None


def personality_baseline(character: CharacterProfile) -> dict[str, float]:
    baseline = dict(EMOTION_BASELINE)
    mbti = extract_mbti(character.identity.get("mbti"))
    if not mbti:
        return baseline
    for letter in mbti:
        for emotion, delta in MBTI_TRAIT_BASELINE_DELTA.get(letter, {}).items():
            baseline[emotion] = _clamp_baseline(baseline[emotion] + delta)
    return enforce_opposites(baseline)


def initial_mood_for_character(character: CharacterProfile) -> MoodState:
    baseline = personality_baseline(character)
    return MoodState(
        emotion_vector={emotion: baseline[emotion] for emotion in EMOTION_IDS},
        emotion_baseline=baseline,
        emotion_affinity={emotion: 0.0 for emotion in EMOTION_IDS},
    )


def mbti_temperament_note(character: CharacterProfile) -> str | None:
    mbti = extract_mbti(character.identity.get("mbti"))
    if not mbti:
        return None
    energy = (
        "表达更外放，互动会让她更有精神"
        if mbti[0] == "E"
        else "表达更克制，会先观察再回应"
    )
    perceive = (
        "更容易注意意义、隐喻和言外之意"
        if mbti[1] == "N"
        else "更重视具体细节和眼前事实"
    )
    decide = (
        "先感受对方的情绪，再组织判断"
        if mbti[2] == "F"
        else "先整理逻辑，再表达关心"
    )
    structure = (
        "表达相对稳定，有自己的节奏"
        if mbti[3] == "J"
        else "更随性，容易被聊天氛围带动"
    )
    return f"气质锚点({mbti}): {energy}；{perceive}；{decide}；{structure}。"


def _clamp_baseline(value: float) -> float:
    return max(5.0, min(95.0, value))

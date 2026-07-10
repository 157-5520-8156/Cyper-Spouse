"""Guard concrete personal history until it has a daemon-owned source."""
from __future__ import annotations

import re


_SENTENCE_RE = re.compile(r"[^。！？!?]+[。！？!?]?")
_PAST_PET_CLAIM_RE = re.compile(
    r"(?:我(?:小时候|以前|曾经|上次|去年)?|(?:小时候|以前|曾经|上次|去年)(?:的我)?)"
    r"[^。！？!?]{0,36}(?:养过|养死过|养了)[^。！？!?]*"
)
_FOLLOW_ON_PET_HISTORY_RE = re.compile(r"^(?:后来|之后|从那以后)[^。！？!?]{0,48}(?:养|宠物)")
_HISTORY_MARKERS = {"小时候", "以前", "曾经", "上次", "去年", "养过", "养死过", "养了", "后来"}
_PET_OR_PLANT_ENTITIES = ("金鱼", "乌龟", "仙人掌", "仓鼠", "兔子", "小狗", "狗狗", "小猫", "猫猫", "鹦鹉")
_USER_OWNERSHIP_CUES = ("你", "战绩", "养死", "养过", "宠物", "全军覆没")


def redact_ungrounded_past_self_history(text: str, known_self_history: str = "") -> str:
    """Remove unsupported concrete past pet stories without blocking present reactions.

    The daemon may only establish her past through the character profile, self-core,
    or recorded life. A generated childhood anecdote otherwise becomes a false fact
    in the next turn's short-term context.
    """
    result: list[str] = []
    removed_claim = False
    for sentence in _SENTENCE_RE.findall(text):
        claim = _PAST_PET_CLAIM_RE.search(sentence)
        if claim and not _claim_is_grounded(claim.group(0), known_self_history):
            prefix = sentence[:claim.start()].rstrip("，, ")
            if prefix:
                result.append(prefix + _terminal_punctuation(sentence))
            removed_claim = True
            continue
        if removed_claim and _FOLLOW_ON_PET_HISTORY_RE.search(sentence.strip()):
            continue
        result.append(sentence)
    return "".join(result).strip()


def redact_unattributed_user_pet_entities(text: str, user_history: str) -> str:
    """Keep pet/plant facts attached to the user only when the user supplied them."""
    result: list[str] = []
    for sentence in _SENTENCE_RE.findall(text):
        if any(cue in sentence for cue in _USER_OWNERSHIP_CUES):
            for entity in _PET_OR_PLANT_ENTITIES:
                if entity in sentence and entity not in user_history:
                    sentence = sentence.replace(entity, "")
        result.append(sentence)
    return "".join(result).strip()


def _claim_is_grounded(claim: str, known_self_history: str) -> bool:
    normalized_known = known_self_history.replace(" ", "")
    candidates = [
        word
        for word in re.findall(r"[\u4e00-\u9fff]{2,}", claim)
        if word not in _HISTORY_MARKERS
    ]
    return any(word in normalized_known for word in candidates)


def _terminal_punctuation(sentence: str) -> str:
    return sentence[-1] if sentence.endswith(("。", "！", "？", "!", "?")) else "。"

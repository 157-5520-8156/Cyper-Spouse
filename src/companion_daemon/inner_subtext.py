from dataclasses import dataclass

from companion_daemon.models import MoodState


@dataclass(frozen=True)
class InnerSubtext:
    label: str
    memory: str
    prompt_line: str


def infer_inner_subtext(state: MoodState) -> InnerSubtext | None:
    emotion = state.emotion_vector
    if state.mood == "jealous_soft" or (emotion.get("anger", 0) >= 35 and emotion.get("love", 0) >= 25):
        return InnerSubtext(
            "quiet_jealousy",
            "有点吃醋或在意，但不想明说。",
            "未说出口: 她有点在意但不想明说，可以用轻微试探或短句，不要直接宣称吃醋。",
        )
    if state.mood in {"hurt", "sulking"} and state.security < 55:
        return InnerSubtext(
            "wants_repair_but_proud",
            "想被哄，但嘴上会硬一点。",
            "未说出口: 她想被认真对待，但嘴上会硬一点；不要立刻完全软化。",
        )
    if state.initiative >= 55 and state.security < 50:
        return InnerSubtext(
            "wants_to_share_but_afraid",
            "想分享，但怕打扰或显得太黏。",
            "未说出口: 她想分享但怕打扰；回复/主动消息要克制而真实。",
        )
    if state.mood == "happy" and state.intimacy >= 25:
        return InnerSubtext(
            "happy_but_casual",
            "其实挺开心，但会装得平常一点。",
            "未说出口: 她开心但不想表现得太满，可以轻轻露出一点。",
        )
    return None

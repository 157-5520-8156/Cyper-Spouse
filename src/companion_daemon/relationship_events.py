from dataclasses import dataclass

from companion_daemon.models import IncomingMessage, MoodState


@dataclass(frozen=True)
class KeyRelationshipEvent:
    kind: str
    memory: str
    prompt_line: str


def detect_key_relationship_event(message: IncomingMessage) -> KeyRelationshipEvent | None:
    text = message.text
    if any(token in text for token in ["你还记得", "我记得你", "记得你喜欢", "记得你说过"]):
        return KeyRelationshipEvent(
            "remembered_detail",
            "用户记得她的小事，这比普通聊天更能增加安全感。",
            "关键事件: 用户记得她的小事；沈知栀会觉得被认真对待，可以更放松一点。",
        )
    if any(token in text for token in ["刚才在忙", "刚刚在忙", "刚才没回", "不是故意不回", "手机没看到"]):
        return KeyRelationshipEvent(
            "explained_absence",
            "用户解释了冷场或没回，不是直接消失。",
            "关键事件: 用户解释了为什么没回；她会松一口气，小别扭会少一点。",
        )
    return None


def apply_key_relationship_event(state: MoodState, event: KeyRelationshipEvent | None) -> MoodState:
    if not event:
        return state
    if event.kind == "remembered_detail":
        return state.model_copy(
            update={
                "trust": _clamp(state.trust + 4),
                "intimacy": _clamp(state.intimacy + 3),
                "security": _clamp(state.security + 5),
                "emotional_charge": _clamp(state.emotional_charge - 4),
            }
        )
    if event.kind == "explained_absence":
        return state.model_copy(
            update={
                "security": _clamp(state.security + 4),
                "patience": _clamp(state.patience + 3),
                "emotional_charge": _clamp(state.emotional_charge - 6),
                "unresolved_emotion": None if state.mood != "hurt" else state.unresolved_emotion,
            }
        )
    return state


def _clamp(value: int) -> int:
    return max(0, min(100, value))

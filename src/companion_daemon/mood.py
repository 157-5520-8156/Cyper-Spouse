from companion_daemon.emotion_core import apply_emotion_deltas, text_emotion_deltas
from companion_daemon.emotion_state import interpret_interaction, transition_emotional_state
from companion_daemon.models import IncomingMessage, MoodState
from companion_daemon.multimodal_analysis import AttachmentInsight
from companion_daemon.time import utc_now


def update_mood_for_message(previous: MoodState, message: IncomingMessage) -> MoodState:
    text = message.text
    event = interpret_interaction(message, previous)
    state = transition_emotional_state(previous, event)

    if any(token in text for token in ["想你", "抱抱", "喜欢你"]):
        if state.relationship_stage in {"stranger", "acquaintance"} and state.mood == "guarded":
            return state.model_copy(update={"last_platform": message.platform})
        state.mood = "affectionate" if state.relationship_stage in {"ambiguous", "lover"} else "happy"
        state.intimacy = min(100, state.intimacy + 2)
        state.attachment = min(100, state.attachment + 1)
        state.unresolved_emotion = None
    deltas = text_emotion_deltas(text, is_user=True)
    if deltas:
        state = apply_emotion_deltas(
            state,
            deltas,
            source="user_message",
            update_affinity=True,
        )

    state.last_platform = message.platform
    return state


def platform_context(previous: MoodState, message: IncomingMessage) -> str | None:
    if previous.last_platform and previous.last_platform != message.platform:
        return f"刚刚在 {previous.last_platform} 聊，现在切到了 {message.platform}。"
    return None


def update_mood_for_attachment_insight(
    previous: MoodState,
    insight: AttachmentInsight,
) -> MoodState:
    state = previous.model_copy(deep=True)
    state.updated_at = utc_now()
    if insight.kind in {"image", "audio"} and insight.confidence >= 0.7:
        state.trust = min(100, state.trust + 1)
        state.intimacy = min(100, state.intimacy + 1)
    elif insight.kind == "file" and insight.confidence >= 0.65:
        state.trust = min(100, state.trust + 1)
    return state

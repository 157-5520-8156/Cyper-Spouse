from datetime import datetime

from companion_daemon.impression import apply_user_impression
from companion_daemon.models import MoodState
from companion_daemon.time import utc_now


def apply_waiting_after_proactive(
    state: MoodState,
    *,
    last_sent_iso: str | None,
    incoming_since: int,
) -> MoodState:
    if not last_sent_iso or incoming_since > 0:
        return state
    last_sent = datetime.fromisoformat(last_sent_iso)
    hours = (utc_now() - last_sent).total_seconds() / 3600
    if hours < 0.5:
        return state
    early_stage = state.relationship_stage in {"stranger", "acquaintance"}
    if hours < 3:
        note = "她刚主动找过你，短时间里会有一点等你回应的心思。"
        if state.unresolved_emotion == note:
            return state
        return apply_user_impression(state.model_copy(
            update={
                "initiative": _clamp(state.initiative + 2),
                "emotional_charge": _clamp(state.emotional_charge + 2),
                "unresolved_emotion": note,
            }
        ), event_kind="proactive_timeout_short")
    if hours < 12:
        note = "主动消息没等到回应，她会把分享欲收住，先回到自己的节奏。"
        if state.unresolved_emotion == note:
            return state
        return apply_user_impression(state.model_copy(
            update={
                # A new acquaintance does not turn silence into longing.  The
                # same wait becomes more personal only after the relationship
                # has earned that interpretation.
                "mood": "miss_you" if not early_stage and state.mood == "calm" else state.mood,
                "security": _clamp(state.security - (2 if early_stage else 3)),
                "initiative": _clamp(state.initiative - (5 if early_stage else 2)),
                "emotional_charge": _clamp(state.emotional_charge + 5),
                "unresolved_emotion": note,
            }
        ), event_kind="proactive_timeout_medium")
    note = "主动找你很久没回应，她会把期待压下去，不再追着补话。"
    if state.unresolved_emotion == note:
        return state
    return apply_user_impression(state.model_copy(
        update={
            "security": _clamp(state.security - (3 if early_stage else 5)),
            "initiative": _clamp(state.initiative - (12 if early_stage else 6)),
            "emotional_charge": _clamp(state.emotional_charge + (2 if early_stage else 3)),
            "unresolved_emotion": note,
        }
    ), event_kind="proactive_timeout_long")


def _clamp(value: int) -> int:
    return max(0, min(100, value))

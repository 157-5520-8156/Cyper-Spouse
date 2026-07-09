from dataclasses import dataclass

from companion_daemon.models import MoodState


@dataclass(frozen=True)
class WithheldImpulse:
    reason: str
    memory_content: str


def build_withheld_impulse(
    *,
    trigger_type: str | None,
    private_thought: str,
) -> WithheldImpulse | None:
    if not trigger_type:
        return None
    thought = private_thought.strip()[:120] or "她想主动找你，但最后忍住了。"
    return WithheldImpulse(
        reason=trigger_type,
        memory_content=f"想主动找你但忍住了；触发={trigger_type}；念头={thought}",
    )


def apply_withheld_impulse(state: MoodState, impulse: WithheldImpulse) -> MoodState:
    initiative_gain = 4
    charge_gain = 3
    if impulse.reason in {"longing_ping", "thinking_of_you", "nostalgia_wave"}:
        initiative_gain += 2
        charge_gain += 2
    mood = state.mood
    if mood == "calm" and state.relationship_stage in {"friend", "close_friend", "ambiguous", "lover"}:
        mood = "miss_you"
    return state.model_copy(
        update={
            "mood": mood,
            "initiative": _clamp(state.initiative + initiative_gain),
            "attachment": _clamp(state.attachment + 1),
            "emotional_charge": _clamp(state.emotional_charge + charge_gain),
            "unresolved_emotion": "她刚才有话想发给你，但忍住了，所以心里还留着一点尾巴。",
        }
    )


def _clamp(value: int) -> int:
    return max(0, min(100, value))

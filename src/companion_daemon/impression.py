from companion_daemon.models import MoodState


def apply_user_impression(state: MoodState, *, event_kind: str, question_response: str | None = None) -> MoodState:
    respect_delta, reliability_delta, responsiveness_delta = {
        "boundary_violation": (-18, -4, 0),
        "control_pressure": (-13, -3, 0),
        "premature_intimacy": (-4, 0, 0),
        "repair_attempt": (5, 4, 0),
        "warmth_received": (6, 2, 1),
        "return_after_gap": (1, 5, 4),
        "availability_drop": (0, 0, -1),
    }.get(event_kind, (0, 0, 0))
    if question_response == "answered":
        responsiveness_delta += 4
    elif question_response == "skipped":
        responsiveness_delta -= 5
    elif question_response == "meta":
        responsiveness_delta += 1
    return state.model_copy(
        update={
            "perceived_respect": _clamp(state.perceived_respect + respect_delta),
            "perceived_reliability": _clamp(state.perceived_reliability + reliability_delta),
            "perceived_responsiveness": _clamp(state.perceived_responsiveness + responsiveness_delta),
        }
    )


def impression_summary(state: MoodState) -> str:
    parts = []
    if state.perceived_respect < 35:
        parts.append("最近感到不被尊重")
    elif state.perceived_respect >= 65:
        parts.append("觉得对方尊重自己")
    if state.perceived_reliability < 35:
        parts.append("对承诺和解释会更谨慎")
    elif state.perceived_reliability >= 65:
        parts.append("觉得对方比较可靠")
    if state.perceived_responsiveness < 35:
        parts.append("不想反复追着要回应")
    return "；".join(parts) if parts else "对用户的印象稳定，按当前关系自然相处"


def _clamp(value: int) -> int:
    return max(0, min(100, value))

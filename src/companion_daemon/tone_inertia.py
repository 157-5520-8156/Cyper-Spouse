from dataclasses import dataclass

from companion_daemon.models import MoodState


@dataclass(frozen=True)
class ToneInertia:
    label: str
    memory: str
    prompt_line: str


def build_tone_inertia(state: MoodState, recent_lines: list[str]) -> ToneInertia:
    recent_her_lines = [line for line in recent_lines[-5:] if "她:" in line]
    joined = "\n".join(recent_her_lines)
    if state.mood in {"hurt", "guarded"} or "边界" in joined:
        label = "reserved"
        guidance = "刚刚偏克制或有边界，下一句不要突然热情。"
    elif state.mood in {"affectionate", "happy"} or any(token in joined for token in ["呀", "嘿", "想"]):
        label = "soft"
        guidance = "刚刚比较柔和，下一句可以延续一点点，但不要过度撒娇。"
    elif state.mood == "sulking":
        label = "sulking"
        guidance = "刚刚还有小别扭，下一句可以嘴硬一点但别伤人。"
    else:
        label = "natural"
        guidance = "保持自然私聊，不要突然大幅变调。"
    return ToneInertia(label, guidance, f"语气惯性: {guidance}")


def classify_outgoing_tone(text: str, state: MoodState) -> str:
    if state.mood in {"hurt", "guarded"}:
        return "reserved"
    if state.mood == "sulking":
        return "sulking"
    if any(token in text for token in ["呀", "嘛", "想你", "在等你"]):
        return "soft"
    return "natural"

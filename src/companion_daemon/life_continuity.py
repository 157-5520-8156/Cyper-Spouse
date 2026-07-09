import re
from dataclasses import dataclass

from companion_daemon.human_rhythm import human_rhythm_snapshot
from companion_daemon.models import MoodState


@dataclass(frozen=True)
class LifeContinuity:
    content: str
    prompt_line: str


def build_life_continuity(
    state: MoodState,
    *,
    previous_content: str | None = None,
) -> LifeContinuity:
    rhythm = human_rhythm_snapshot(state)
    plan = daily_life_plan(rhythm.phase)
    previous_phase = _phase_from(previous_content)
    if previous_phase and previous_phase != rhythm.phase:
        transition = f"上一段状态是 {previous_phase}，现在转到 {rhythm.phase}。"
    elif previous_phase == rhythm.phase:
        transition = f"还延续在 {rhythm.phase} 这段生活节奏里。"
    else:
        transition = f"当前生活阶段是 {rhythm.phase}。"
    content = (
        f"phase={rhythm.phase}; activity={rhythm.private_activity}; "
        f"attention={rhythm.attention_mode}; hour={rhythm.local_hour}"
    )
    return LifeContinuity(
        content=content,
        prompt_line=(
            "生活连续性: "
            f"{transition} 她像是在{rhythm.private_activity}；"
            f"当天轻量日程线={plan}；"
            "回复时可以顺着这个生活状态，但不要解释状态机。"
        ),
    )


def daily_life_plan(phase: str) -> str:
    plans = {
        "early_morning": "醒来、看课表、慢慢进入状态",
        "morning_focus": "上午自习或课间，回复偏短",
        "lunch_break": "午饭/买饮料，可以有生活碎片",
        "afternoon_classes": "下午课或自习，容易犯困",
        "evening_unwind": "一天收尾，分享欲更强",
        "late_evening": "洗漱前后，心思更软",
        "deep_night": "夜里半醒或睡前，消息短而安静",
    }
    return plans.get(phase, "普通校园日常")


def _phase_from(content: str | None) -> str | None:
    if not content:
        return None
    match = re.search(r"phase=([^;]+)", content)
    if not match:
        return None
    return match.group(1).strip()

"""Pure, bounded emotion programs driven by committed appraisal dimensions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Mapping


Agency = Literal["user", "companion", "npc", "third_party", "situation", "unknown"]


@dataclass(frozen=True)
class EmotionProgramInput:
    event: str
    agency: Agency
    target: str
    certainty: int
    goal_congruence: int
    controllability: int
    norm_compatibility: int
    power_delta: int
    self_evaluation: str = "specific_action"
    social_exposure: int = 0
    relationship_value: int = 0
    comparison_salience: int = 0
    comparison_target: str = ""
    source_event_ids: tuple[str, ...] = ()
    expression_safety: int = 100
    unresolved: bool = False
    attention_capture: int = 0

    def __post_init__(self) -> None:
        for name in (
            "certainty", "controllability", "social_exposure", "relationship_value",
            "comparison_salience", "expression_safety", "attention_capture",
        ):
            value = getattr(self, name)
            if not 0 <= value <= 100:
                raise ValueError(f"{name} must be between 0 and 100")
        for name in ("goal_congruence", "norm_compatibility", "power_delta"):
            value = getattr(self, name)
            if not -100 <= value <= 100:
                raise ValueError(f"{name} must be between -100 and 100")
        if len(self.source_event_ids) > 16 or any(
            not item or len(item) > 160 for item in self.source_event_ids
        ):
            raise ValueError("source_event_ids are outside their bound")
        if len(self.comparison_target) > 160:
            raise ValueError("comparison_target exceeds its bound")


@dataclass(frozen=True)
class EmotionProgramResult:
    primary: str
    components: Mapping[str, int]
    coping: str
    processes: tuple[str, ...]
    process_effects: Mapping[str, float]
    invented_stimulus: bool = False
    program_version: str = "emotion-programs-v1"


def evaluate_emotion_program(input: EmotionProgramInput) -> EmotionProgramResult:
    """Map evidence-backed appraisal dimensions to emotion/coping tendencies."""
    loss = max(0, -input.goal_congruence)
    norm_breach = max(0, -input.norm_compatibility)
    low_control = 100 - input.controllability
    uncertainty = 100 - input.certainty
    components: dict[str, int] = {}

    if input.agency == "companion" and input.self_evaluation == "global_negative":
        components["shame"] = _bounded(
            (loss + norm_breach + input.social_exposure + max(0, -input.power_delta)) / 4
        )
    elif input.agency == "companion" and norm_breach > 0:
        components["guilt"] = _bounded(
            (loss + norm_breach + input.certainty + input.controllability) / 4
        )

    has_sourced_relationship_threat = (
        input.target == "valued_relationship"
        and input.relationship_value > 0
        and input.comparison_salience > 0
        and bool(input.comparison_target)
        and bool(input.source_event_ids)
        and input.agency != "unknown"
    )
    if has_sourced_relationship_threat:
        components["jealousy"] = _bounded(
            (loss + input.relationship_value + input.comparison_salience + uncertainty) / 4
        )

    if not components and loss:
        if input.controllability >= 55 and input.agency not in {"unknown", "situation"}:
            components["anger"] = _bounded((loss + norm_breach + input.controllability) / 3)
        elif input.certainty < 55:
            components["anxiety"] = _bounded((loss + low_control + uncertainty) / 3)
        else:
            components["sadness"] = _bounded((loss + low_control + input.certainty) / 3)
    if not components:
        components["neutral"] = 0

    primary = max(components, key=components.__getitem__)
    if primary == "guilt":
        coping = "repair" if input.controllability >= 40 else "acknowledge_harm"
    elif primary == "shame":
        coping = "conceal_or_withdraw"
    elif primary == "jealousy":
        coping = "seek_clarity_without_control"
    elif primary == "anger":
        coping = "assert_or_problem_solve"
    elif primary == "anxiety":
        coping = "seek_information"
    elif primary == "sadness":
        coping = "withdraw_or_seek_support"
    else:
        coping = "continue"

    processes: list[str] = []
    display_multiplier = 1.0
    decay_multiplier = 1.0
    if input.expression_safety <= 30 and max(components.values()) > 0:
        processes.append("suppression")
        display_multiplier = max(0.2, input.expression_safety / 100)
    if input.unresolved and input.attention_capture >= 65 and max(components.values()) > 0:
        processes.append("rumination")
        # Slower decay only; a caller may reinforce the sourced episode but this
        # pure program never creates a new stimulus or unbounded self-excitation.
        decay_multiplier = max(0.45, 1.0 - input.attention_capture / 200)
    return EmotionProgramResult(
        primary,
        components,
        coping,
        tuple(processes),
        {
            "display_multiplier": display_multiplier,
            "decay_multiplier": decay_multiplier,
        },
    )


def _bounded(value: float) -> int:
    return max(1, min(100, round(value)))

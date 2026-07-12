"""Plan regulated affect display from sourced emotion episodes."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Mapping

from companion_daemon.expression_plan import compile_expression_plan


@dataclass(frozen=True)
class AffectDisplayPlan:
    primary_appraisal: str
    secondary_appraisal: str
    mixed: bool
    approach_avoidance: str
    regulation_strategy: str
    attribution_target: str
    leakage: int
    directness: int
    prompt_line: str
    rule_version: str = "affect-display-v1"

    def payload(self) -> dict[str, object]:
        return asdict(self)


def plan_affect_display(
    affect: Mapping[str, object],
    relationship: Mapping[str, object],
    needs: Mapping[str, object],
    *,
    current_appraisal: str,
) -> AffectDisplayPlan:
    expression = compile_expression_plan(
        affect,
        relationship,
        needs,
        current_appraisal=current_appraisal,
    )
    spec = expression.policy_spec
    if spec.regulation_strategy == "contain_spillover":
        approach_avoidance = "regulated_contact"
    elif spec.regulation_strategy == "boundary_expression":
        approach_avoidance = "approach_avoidance" if spec.mixed else "protective_distance"
    elif spec.mixed:
        approach_avoidance = "approach_avoidance"
    else:
        approach_avoidance = "approach"
    return AffectDisplayPlan(
        primary_appraisal=spec.primary_appraisal,
        secondary_appraisal=spec.secondary_appraisal,
        mixed=spec.mixed,
        approach_avoidance=approach_avoidance,
        regulation_strategy=spec.regulation_strategy,
        attribution_target=spec.attribution_target,
        leakage=spec.leakage,
        directness=spec.directness,
        prompt_line=expression.prompt_fragment,
        rule_version=spec.rule_version,
    )

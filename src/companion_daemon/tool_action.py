"""Permission-bound tool execution values and a side-effect-free adapter."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Mapping


ToolResultStatus = Literal["delivered", "failed", "cancelled"]


@dataclass(frozen=True)
class ToolExecutionRequest:
    action_id: str
    proposal_id: str
    tool_name: str
    arguments: dict[str, object]


@dataclass(frozen=True)
class FakeToolOutcome:
    status: ToolResultStatus
    detail: str
    output: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolExecutionResult:
    status: ToolResultStatus
    detail: str
    output: dict[str, object]
    execution_mode: Literal["fake"] = "fake"
    effect_scope: Literal["none"] = "none"

    def to_world_result(self) -> dict[str, object]:
        return {
            "kind": "tool_execution",
            "status": self.status,
            "execution_mode": self.execution_mode,
            "effect_scope": self.effect_scope,
            "detail": self.detail,
            "output": dict(self.output),
        }


class FakeToolAdapter:
    """Deterministic fake: returns configured data and performs no real operation."""

    def __init__(self, *, outcomes: Mapping[str, FakeToolOutcome] | None = None):
        self._outcomes = dict(outcomes or {})

    def execute(self, request: ToolExecutionRequest) -> ToolExecutionResult:
        outcome = self._outcomes.get(request.tool_name)
        if outcome is None:
            outcome = FakeToolOutcome(
                status="failed",
                detail=f"fake adapter has no outcome for {request.tool_name}",
            )
        return ToolExecutionResult(
            status=outcome.status,
            detail=outcome.detail,
            output=dict(outcome.output),
        )

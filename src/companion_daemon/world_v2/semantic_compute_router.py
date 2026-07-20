"""Deterministic semantic compute budgeting; never a reply or behaviour policy."""

from __future__ import annotations

from .deliberation import ModelRoute, RouteRequest


class SemanticComputeRouter:
    """Choose Flash or Thinking from categorical, source-bound complexity hints."""

    VERSION = "semantic-compute-router.1"

    def __init__(self, *, thinking_available: bool = False) -> None:
        self._thinking_available = thinking_available

    async def route(self, request: RouteRequest) -> ModelRoute:
        hints = request.route_hints
        reason: str | None = None
        # A fallible ambiguity advisory alone is not enough to make a user wait
        # for Thinking. Escalation is reserved for high severity or a durable,
        # multi-signal conflict below.
        if hints.severity in {"high", "acute"}:
            reason = "high_severity"
        elif hints.conflict_complexity == "complex" and hints.continuity == "persistent":
            reason = "persistent_complex_conflict"
        if reason is None:
            return ModelRoute(
                tier="flash", reason_code="ordinary_compute", router_version=self.VERSION
            )
        if not self._thinking_available:
            return ModelRoute(
                tier="flash", reason_code="thinking_unavailable", router_version=self.VERSION
            )
        return ModelRoute(tier="thinking", reason_code=reason, router_version=self.VERSION)


__all__ = ["SemanticComputeRouter"]

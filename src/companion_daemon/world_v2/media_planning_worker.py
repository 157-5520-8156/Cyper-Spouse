"""Scheduler-owned execution of already-frozen Media v2 planning Actions.

The world selection layer is deliberately *not* in this module.  It receives
neither a World projection nor a candidate matrix: its only input is an
existing ``media_planning`` Action that has already been bound to a frozen
``MediaOpportunity`` by :class:`MediaPlanningRuntime`.

Planning is a slightly unusual provider operation because the terminal
provider result is itself a domain object (``MediaPlan`` or
``MediaNotRenderable``).  ``MediaPlanningRuntime`` records that atomic
result/receipt bundle after ActionPump has made ``dispatch_started`` durable.
This worker uses the same ActionPump lifecycle as other effects, but reports
the post-commit domain outcome rather than exposing the pump's interim
``pending`` return from the self-recording adapter.
"""

from __future__ import annotations

from typing import Literal

from .action_pump import ActionExecutor, ActionPump
from .media_planning_runtime import MediaPlanningRuntime
from .media_provider_grants import require_provider_media_grant
from .media_v2 import MediaPlanner
from .schema_core import FrozenModel
from .schemas import Action, LedgerProjection


class MediaPlanningRunResult(FrozenModel):
    """One bounded scheduler attempt, with no implied opportunity creation."""

    status: Literal["idle", "unavailable", "planned", "not_renderable", "in_progress"]
    action_id: str | None = None


class _PlanningActionExecutor(ActionExecutor):
    """ActionPump adapter for a planner that atomically records its result.

    The adapter intentionally returns ``None`` after the runtime commits the
    result bundle.  Calling generic settlement as well would create a second
    receipt for the same effect.  :class:`MediaPlanningWorker` immediately
    re-reads the sole ledger authority and translates that self-recorded
    terminal state into a scheduler result.
    """

    def __init__(self, *, runtime: MediaPlanningRuntime, planner: MediaPlanner) -> None:
        self._runtime = runtime
        self._planner = planner

    async def assert_dispatch_authorized(
        self, *, action: Action, projection: LedgerProjection
    ) -> None:
        if action.kind != "media_planning":
            raise ValueError("media planning scheduler received a non-planning Action")
        require_provider_media_grant(
            action=action,
            projection=projection,
            logical_time=projection.logical_time or action.logical_time,
        )

    async def dispatch(self, action: Action):
        if action.kind != "media_planning":
            raise ValueError("media planning scheduler received a non-planning Action")
        await self._runtime.execute_planning_once(
            action_id=action.action_id, planner=self._planner
        )
        return None

    async def lookup_result(self, action: Action):
        # ``execute_planning_once`` first asks the injected planner for the
        # immutable idempotency-keyed result.  If that result was already
        # recorded, it simply returns it.  No new opportunity or request key
        # can be created on recovery.
        return await self.dispatch(action)


class MediaPlanningWorker:
    """Drain at most one existing Media v2 planning Action.

    The worker cannot author candidates, snapshots, opportunities, grants,
    budgets, render Actions, or delivery Actions.  It only advances a prior
    source-bound planning Action through its effect-once lifecycle.
    """

    def __init__(
        self,
        *,
        ledger,
        runtime: MediaPlanningRuntime,
        planner: MediaPlanner | None,
        owner_id: str,
    ) -> None:
        if not owner_id:
            raise ValueError("media planning worker requires an owner")
        self._ledger = ledger
        self._runtime = runtime
        self._planner = planner
        self._owner_id = owner_id

    async def drain_once(self) -> MediaPlanningRunResult:
        projection = self._ledger.project()
        candidates = tuple(
            action
            for action in projection.actions
            if action.kind == "media_planning"
            and action.state
            in {"authorized", "scheduled", "claimed", "dispatch_started", "provider_accepted"}
        )
        if not candidates:
            return MediaPlanningRunResult(status="idle")
        action = candidates[0]
        if self._planner is None:
            # Fail closed: an unavailable planner leaves the frozen Action
            # unchanged for an operator/configured provider.  It never falls
            # back to the legacy world/image path or manufactures a result.
            return MediaPlanningRunResult(status="unavailable", action_id=action.action_id)

        pump = ActionPump(
            ledger=self._ledger,
            executor=_PlanningActionExecutor(runtime=self._runtime, planner=self._planner),
            settle=self._unexpected_generic_settlement,
            owner_id=self._owner_id,
            source="world-v2:media-planning-worker",
        )
        await pump.drain_action(action.action_id)
        after = self._ledger.project()
        current = next((item for item in after.actions if item.action_id == action.action_id), None)
        if current is None:
            raise RuntimeError("media planning Action disappeared after scheduler attempt")
        if any(item.opportunity_id == current.intent_ref for item in after.media_plans):
            return MediaPlanningRunResult(status="planned", action_id=current.action_id)
        if current.intent_ref in after.media_unrenderable_opportunity_ids:
            return MediaPlanningRunResult(status="not_renderable", action_id=current.action_id)
        return MediaPlanningRunResult(status="in_progress", action_id=current.action_id)

    async def _unexpected_generic_settlement(self, _result) -> object:
        # Planning result settlement is atomic inside MediaPlanningRuntime so
        # plan sidecar, receipt, budget, and continuation remain one bundle.
        raise RuntimeError("media planning must not use generic receipt settlement")


__all__ = ["MediaPlanningRunResult", "MediaPlanningWorker"]

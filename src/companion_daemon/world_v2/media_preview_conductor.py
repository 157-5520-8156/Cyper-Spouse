"""One bounded conductor for the safe media-preview prefix.

The conductor owns only the transition from an already-opened candidate to a
planned or explicitly unrenderable preview.  It cannot discover evidence,
render, inspect, approve delivery, or send media.  Those remain separate
modules with their own authority and recovery contracts.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Literal

from .errors import ConcurrencyConflict
from .media_selection_acceptance_runtime import MediaSelectionAcceptanceError
from .media_planning_worker import MediaPlanningRunResult, MediaPlanningWorker
from .media_selection_worker import MediaSelectionRunResult
from .schema_core import FrozenModel


class MediaPreviewConductorResult(FrozenModel):
    """Auditable result of one selection → acceptance → planning attempt."""

    status: Literal["idle", "blocked", "planned", "not_renderable", "in_progress"]
    selection: MediaSelectionRunResult | None = None
    planning: MediaPlanningRunResult | None = None
    reason_code: str | None = None


class MediaPreviewAcceptanceOutcome(FrozenModel):
    """The only two legal consequences of media-selection Acceptance."""

    disposition: Literal["planning_authorized", "not_renderable"]
    event_ids: tuple[str, ...]


MediaSelectionRunner = Callable[..., Awaitable[MediaSelectionRunResult]]
MediaSelectionAcceptor = Callable[..., Awaitable[MediaPreviewAcceptanceOutcome | None]]


class MediaPreviewConductor:
    """Hide a three-module preview prefix behind one scheduler-facing Interface.

    The constructor receives already-composed implementations; the public
    ``advance_once`` Interface accepts only the durable clock and trace
    coordinates.  This keeps platform hosts from learning proposal IDs,
    grant/budget fields, candidate data, or planner details.
    """

    def __init__(
        self,
        *,
        select: MediaSelectionRunner,
        accept: MediaSelectionAcceptor,
        planning: MediaPlanningWorker,
    ) -> None:
        self._select = select
        self._accept = accept
        self._planning = planning

    async def advance_once(
        self, *, logical_time: datetime, trace_id: str, correlation_id: str
    ) -> MediaPreviewConductorResult:
        # Recovery has priority over new selection.  An accepted proposal may
        # already have created a planning Action before the process stopped;
        # selecting another candidate first would reorder durable work and
        # leave the existing Action stranded.
        try:
            pending_planning = await self._planning.drain_once()
        except ConcurrencyConflict:
            return MediaPreviewConductorResult(
                status="blocked", reason_code="media_preview.planning_cursor_stale"
            )
        if pending_planning.status != "idle":
            return self._planning_result(planning=pending_planning)
        selection = await self._select(
            logical_time=logical_time,
            trace_id=trace_id,
            correlation_id=correlation_id,
        )
        if selection.status == "no_op":
            return MediaPreviewConductorResult(
                status="idle", selection=selection, reason_code=selection.reason_code
            )
        if selection.status != "proposed" or selection.proposal_event_ref is None:
            return MediaPreviewConductorResult(
                status="blocked", selection=selection,
                reason_code=selection.reason_code or "media_preview.selection_blocked",
            )
        try:
            accepted = await self._accept(
                proposal_event_ref=selection.proposal_event_ref,
                logical_time=logical_time,
                trace_id=trace_id,
                correlation_id=correlation_id,
            )
        except ConcurrencyConflict:
            return MediaPreviewConductorResult(
                status="blocked", selection=selection,
                reason_code="media_preview.acceptance_cursor_stale",
            )
        except MediaSelectionAcceptanceError as exc:
            if exc.code not in {
                "media_selection_acceptance.proposal_stale",
                "media_selection_acceptance.proposal_event_unavailable",
                "media_selection_acceptance.proposal_candidate_not_current",
            }:
                raise
            return MediaPreviewConductorResult(
                status="blocked", selection=selection, reason_code=exc.code
            )
        if accepted is None:
            # Construction must never permit this state: callers should not
            # create a conductor without the configured acceptance authority.
            return MediaPreviewConductorResult(
                status="blocked", selection=selection,
                reason_code="media_preview.acceptance_unavailable",
            )
        if accepted.disposition == "not_renderable":
            return MediaPreviewConductorResult(
                status="not_renderable", selection=selection,
                reason_code="media_preview.acceptance_not_renderable",
            )
        try:
            planning = await self._planning.drain_once()
        except ConcurrencyConflict:
            return MediaPreviewConductorResult(
                status="blocked", selection=selection,
                reason_code="media_preview.planning_cursor_stale",
            )
        return self._planning_result(selection=selection, planning=planning)

    @staticmethod
    def _planning_result(
        *, planning: MediaPlanningRunResult,
        selection: MediaSelectionRunResult | None = None,
    ) -> MediaPreviewConductorResult:
        if planning.status == "planned":
            status: Literal["planned", "not_renderable", "in_progress", "blocked"] = "planned"
        elif planning.status == "not_renderable":
            status = "not_renderable"
        elif planning.status == "in_progress":
            status = "in_progress"
        else:
            status = "blocked"
        return MediaPreviewConductorResult(
            status=status,
            selection=selection,
            planning=planning,
            reason_code=(
                None if status != "blocked" else "media_preview.planning_" + planning.status
            ),
        )


__all__ = [
    "MediaPreviewAcceptanceOutcome",
    "MediaPreviewConductor",
    "MediaPreviewConductorResult",
]

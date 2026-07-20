"""Contract tests for the bounded media-preview scheduler module."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from companion_daemon.world_v2.errors import ConcurrencyConflict
from companion_daemon.world_v2.media_selection_acceptance_runtime import (
    MediaSelectionAcceptanceError,
)
from companion_daemon.world_v2.media_planning_worker import MediaPlanningRunResult
from companion_daemon.world_v2.media_preview_conductor import (
    MediaPreviewAcceptanceOutcome,
    MediaPreviewConductor,
)
from companion_daemon.world_v2.media_selection_worker import MediaSelectionRunResult


NOW = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)


class _Planning:
    def __init__(self, *results: MediaPlanningRunResult) -> None:
        self._results = list(results)
        self.calls = 0

    async def drain_once(self) -> MediaPlanningRunResult:
        self.calls += 1
        return self._results.pop(0)


@pytest.mark.asyncio
async def test_conductor_hides_candidate_and_acceptance_details_behind_one_advance() -> None:
    calls: list[tuple[str, object]] = []

    async def select(**kwargs: object) -> MediaSelectionRunResult:
        calls.append(("select", kwargs))
        return MediaSelectionRunResult(status="proposed", proposal_event_ref="proposal:media:1")

    async def accept(**kwargs: object) -> MediaPreviewAcceptanceOutcome:
        calls.append(("accept", kwargs))
        return MediaPreviewAcceptanceOutcome(
            disposition="planning_authorized", event_ids=("event:acceptance",)
        )

    planning = _Planning(
        MediaPlanningRunResult(status="idle"),
        MediaPlanningRunResult(status="planned", action_id="action:media:1"),
    )
    result = await MediaPreviewConductor(
        select=select, accept=accept, planning=planning,  # type: ignore[arg-type]
    ).advance_once(logical_time=NOW, trace_id="trace:preview", correlation_id="correlation:preview")

    assert result.status == "planned"
    assert result.selection is not None and result.selection.proposal_event_ref == "proposal:media:1"
    assert result.planning is not None and result.planning.action_id == "action:media:1"
    assert planning.calls == 2
    assert calls == [
        ("select", {
            "logical_time": NOW, "trace_id": "trace:preview", "correlation_id": "correlation:preview",
        }),
        ("accept", {
            "proposal_event_ref": "proposal:media:1", "logical_time": NOW,
            "trace_id": "trace:preview", "correlation_id": "correlation:preview",
        }),
    ]


@pytest.mark.asyncio
async def test_conductor_does_not_accept_or_plan_after_noop_or_blocked_selection() -> None:
    accepted = False

    async def accept(**_kwargs: object) -> MediaPreviewAcceptanceOutcome:
        nonlocal accepted
        accepted = True
        return MediaPreviewAcceptanceOutcome(
            disposition="planning_authorized", event_ids=("event:acceptance",)
        )

    for selection in (
        MediaSelectionRunResult(status="no_op", reason_code="media_selection.model_declined"),
        MediaSelectionRunResult(status="blocked", reason_code="media_selection.logical_time_not_current"),
    ):
        async def select(**_kwargs: object) -> MediaSelectionRunResult:
            return selection

        planning = _Planning(MediaPlanningRunResult(status="idle"))
        result = await MediaPreviewConductor(
            select=select, accept=accept, planning=planning,  # type: ignore[arg-type]
        ).advance_once(logical_time=NOW, trace_id="trace:preview", correlation_id="correlation:preview")
        assert result.status == ("idle" if selection.status == "no_op" else "blocked")
        assert result.reason_code == selection.reason_code
        assert planning.calls == 1
    assert accepted is False


@pytest.mark.asyncio
async def test_conductor_resumes_existing_planning_before_selecting_another_candidate() -> None:
    selected = False
    accepted = False

    async def select(**_kwargs: object) -> MediaSelectionRunResult:
        nonlocal selected
        selected = True
        return MediaSelectionRunResult(status="no_op")

    async def accept(**_kwargs: object) -> MediaPreviewAcceptanceOutcome:
        nonlocal accepted
        accepted = True
        return MediaPreviewAcceptanceOutcome(
            disposition="planning_authorized", event_ids=("event:acceptance",)
        )

    planning = _Planning(
        MediaPlanningRunResult(status="not_renderable", action_id="action:recover:1")
    )
    result = await MediaPreviewConductor(
        select=select, accept=accept, planning=planning,  # type: ignore[arg-type]
    ).advance_once(
        logical_time=NOW, trace_id="trace:preview:recovery",
        correlation_id="correlation:preview:recovery",
    )

    assert result.status == "not_renderable"
    assert result.selection is None
    assert result.planning is not None and result.planning.action_id == "action:recover:1"
    assert selected is False
    assert accepted is False


@pytest.mark.asyncio
async def test_conductor_reports_acceptance_not_renderable_without_planning() -> None:
    async def select(**_kwargs: object) -> MediaSelectionRunResult:
        return MediaSelectionRunResult(
            status="proposed", proposal_event_ref="event:proposal:not-renderable"
        )

    async def accept(**_kwargs: object) -> MediaPreviewAcceptanceOutcome:
        return MediaPreviewAcceptanceOutcome(
            disposition="not_renderable", event_ids=("event:candidate:unrenderable",)
        )

    planning = _Planning(MediaPlanningRunResult(status="idle"))
    result = await MediaPreviewConductor(
        select=select, accept=accept, planning=planning,  # type: ignore[arg-type]
    ).advance_once(
        logical_time=NOW, trace_id="trace:preview:not-renderable",
        correlation_id="correlation:preview:not-renderable",
    )

    assert result.status == "not_renderable"
    assert result.reason_code == "media_preview.acceptance_not_renderable"
    # One initial recovery probe only; Acceptance did not authorize planning.
    assert planning.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "reason_code"),
    (
        (ConcurrencyConflict("stale"), "media_preview.acceptance_cursor_stale"),
        (
            MediaSelectionAcceptanceError("proposal_stale"),
            "media_selection_acceptance.proposal_stale",
        ),
    ),
)
async def test_conductor_structures_expected_acceptance_races(
    failure: Exception, reason_code: str,
) -> None:
    async def select(**_kwargs: object) -> MediaSelectionRunResult:
        return MediaSelectionRunResult(
            status="proposed", proposal_event_ref="event:proposal:stale"
        )

    async def accept(**_kwargs: object) -> MediaPreviewAcceptanceOutcome:
        raise failure

    result = await MediaPreviewConductor(
        select=select, accept=accept,  # type: ignore[arg-type]
        planning=_Planning(MediaPlanningRunResult(status="idle")),
    ).advance_once(
        logical_time=NOW, trace_id="trace:race", correlation_id="correlation:race"
    )

    assert result.status == "blocked"
    assert result.reason_code == reason_code


@pytest.mark.asyncio
async def test_conductor_does_not_hide_acceptance_integrity_failure() -> None:
    async def select(**_kwargs: object) -> MediaSelectionRunResult:
        return MediaSelectionRunResult(
            status="proposed", proposal_event_ref="event:proposal:forged"
        )

    async def accept(**_kwargs: object) -> MediaPreviewAcceptanceOutcome:
        raise MediaSelectionAcceptanceError("proposal_lineage_invalid")

    with pytest.raises(
        MediaSelectionAcceptanceError, match="proposal_lineage_invalid"
    ):
        await MediaPreviewConductor(
            select=select, accept=accept,  # type: ignore[arg-type]
            planning=_Planning(MediaPlanningRunResult(status="idle")),
        ).advance_once(
            logical_time=NOW,
            trace_id="trace:integrity",
            correlation_id="correlation:integrity",
        )

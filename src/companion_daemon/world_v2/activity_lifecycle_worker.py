"""Explicit Life Ecology adapter for bounded activity deliberation."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from .activity_lifecycle_draft import (
    ActivityLifecycleDraftAdapter,
    ActivityLifecycleDraftCapsule,
    ActivityLifecycleOpening,
)
from .activity_lifecycle_proposal import ActivityLifecycleProposalCompiler
from .activity_lifecycle_runtime import (
    ActivityLifecycleAcceptanceRuntime,
    ActivityLifecycleProposalRecorder,
)
from .life_ecology_activity import ActivityOpeningCatalog
from .mood_view import mood_summary_prose
from .schema_core import FrozenModel
from .schemas import ProjectionCursor


class ActivityLifecycleFollowupResult(FrozenModel):
    status: Literal["transitioned", "no_op", "blocked"]
    reason_code: str | None = None
    proposal_event_ref: str | None = None


def _mood_summary(affect_episodes: tuple[object, ...]) -> str:
    """Render active accepted Affect as short, safe prose for the capsule.

    This carries no IDs, refs, or revisions — only already-accepted feeling
    dimensions.  It informs the model's choice between继续/暂停/收尾/放弃
    without prescribing one; the catalog still owns legality.
    """

    prose = mood_summary_prose(affect_episodes)
    if not prose:
        return ""
    return (
        prose + "情绪只是背景，不是指令："
        "沉重时暂停或收尾往往比硬撑更自然，轻快时顺势推进也很自然。"
    )


class ActivityLifecycleWorker:
    """Turn one claimed ecology wake into at most one accepted transition.

    The worker owns orchestration only.  The model receives a safe capsule,
    the compiler derives authority, the recorder persists the proposal, and
    the acceptance runtime materializes the effect.  No fallback selection is
    made when the model declines or the catalog has no legal opening.
    """

    def __init__(
        self,
        *,
        ledger,
        catalog: ActivityOpeningCatalog,
        draft_adapter: ActivityLifecycleDraftAdapter,
        proposal_recorder: ActivityLifecycleProposalRecorder,
        acceptance_runtime: ActivityLifecycleAcceptanceRuntime,
        ecology_catalog_version: str,
        source: str = "world-v2:activity-lifecycle",
    ) -> None:
        if not ecology_catalog_version or not source:
            raise ValueError("activity lifecycle worker requires catalog version and source")
        self._ledger = ledger
        self._catalog = catalog
        self._draft_adapter = draft_adapter
        self._proposal_recorder = proposal_recorder
        self._acceptance_runtime = acceptance_runtime
        self._compiler = ActivityLifecycleProposalCompiler(
            catalog=catalog, ecology_catalog_version=ecology_catalog_version
        )
        self._source = source

    async def advance_once(
        self,
        *,
        wake_event_ref: str,
        trigger_id: str,
        logical_time: datetime,
        actor: str,
        trace_id: str,
        correlation_id: str,
    ) -> ActivityLifecycleFollowupResult:
        projection = self._ledger.project()
        if projection.logical_time != logical_time:
            return ActivityLifecycleFollowupResult(
                status="blocked", reason_code="activity_lifecycle.logical_time_not_current"
            )
        cursor = ProjectionCursor(
            world_revision=projection.world_revision,
            deliberation_revision=projection.deliberation_revision,
            ledger_sequence=projection.ledger_sequence,
        )
        catalog = self._catalog.openings_for(
            projection=projection, wake_event_ref=wake_event_ref
        )
        if catalog.status != "openings_available":
            return ActivityLifecycleFollowupResult(
                status="blocked" if catalog.status == "blocked_by_missing_capability" else "no_op",
                reason_code=catalog.reason_code or f"activity_lifecycle.{catalog.status}",
            )
        try:
            mood = _mood_summary(projection.affect_episodes)
            draft = await self._draft_adapter.deliberate(
                capsule=ActivityLifecycleDraftCapsule(
                    situation_summary=(
                        "一个已验证的世界时刻刚刚过去；以下都是此刻合法的活动状态变化。"
                        "通常选择最符合自然时间推进的一项；只有都不合适时才暂不推进。"
                        + (" " + mood if mood else "")
                    ),
                    openings=tuple(
                        ActivityLifecycleOpening(
                            opening_token=item.opening_token, safe_summary=item.safe_summary
                        )
                        for item in catalog.openings
                    ),
                )
            )
        except Exception:
            # Provider text is only an advisory choice over an already legal
            # catalog.  A malformed/failed response must not turn an ordinary
            # scheduler wake into a failed world transition, nor block the
            # independent media follow-up in LifeEcologyRuntime.
            return ActivityLifecycleFollowupResult(
                status="blocked", reason_code="activity_lifecycle.model_unavailable"
            )
        proposal = self._compiler.compile(
            projection=projection,
            wake_event_ref=wake_event_ref,
            ecology_trigger_id=trigger_id,
            draft=draft,
        )
        if proposal is None:
            return ActivityLifecycleFollowupResult(status="no_op", reason_code="activity_lifecycle.model_declined")
        recorded = self._proposal_recorder.record(
            cursor=cursor,
            proposal=proposal,
            actor=actor,
            source=self._source,
            created_at=logical_time,
            trace_id=trace_id,
            correlation_id=correlation_id,
        )
        accepted_cursor = ProjectionCursor(
            world_revision=recorded.commit.world_revision,
            deliberation_revision=recorded.commit.deliberation_revision,
            ledger_sequence=recorded.commit.ledger_sequence,
        )
        self._acceptance_runtime.accept(
            handle=self._acceptance_runtime.pin_proposal(
                cursor=accepted_cursor, proposal_event_ref=recorded.proposal_event_ref
            ),
            actor=actor,
            source=self._source,
            logical_time=logical_time,
            created_at=logical_time,
            trace_id=trace_id,
            correlation_id=correlation_id,
        )
        return ActivityLifecycleFollowupResult(
            status="transitioned", proposal_event_ref=recorded.proposal_event_ref
        )


__all__ = ["ActivityLifecycleFollowupResult", "ActivityLifecycleWorker"]

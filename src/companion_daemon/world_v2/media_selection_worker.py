"""Orchestrate one bounded P1 candidate choice into a persisted Proposal."""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Literal

from .media_selection import MediaSelection
from .media_selection_acceptance_runtime import MediaSelectionProposalRecorder
from .media_selection_draft import (
    MediaCandidateChoice,
    MediaSelectionCapsule,
    MediaSelectionDraftAdapter,
)
from .media_selection_proposal import MediaSelectionProposalCompiler
from .media_candidate_advisory import MediaCandidateAdvisoryCompiler
from .random_authority import RandomAuthority
from .schema_core import FrozenModel
from .schemas import ProjectionCursor


class MediaSelectionRunResult(FrozenModel):
    status: Literal["proposed", "no_op", "blocked"]
    proposal_event_ref: str | None = None
    reason_code: str | None = None


class MediaSelectionWorker:
    """Persist a model's bounded selection; Acceptance remains a separate seam."""

    def __init__(self, *, ledger, draft_adapter: MediaSelectionDraftAdapter, proposal_recorder: MediaSelectionProposalRecorder, catalog_version: str, source: str = "world-v2:media-selection") -> None:  # type: ignore[no-untyped-def]
        self._ledger, self._draft, self._recorder = ledger, draft_adapter, proposal_recorder
        self._compiler, self._source = MediaSelectionProposalCompiler(catalog_version=catalog_version), source
        self._advisory = MediaCandidateAdvisoryCompiler()
        self._random = RandomAuthority(ledger=ledger)

    async def select_once(self, *, logical_time: datetime, actor: str, trace_id: str, correlation_id: str) -> MediaSelectionRunResult:
        projection = self._ledger.project()
        if projection.logical_time != logical_time:
            return MediaSelectionRunResult(status="blocked", reason_code="media_selection.logical_time_not_current")
        eligible = tuple(
            item
            for item in projection.photo_candidates
            if item.status == "available"
            and item.opened_at is not None
            and item.expires_at is not None
            and item.expires_at > logical_time
            and item.source_events
        )
        pending = {
            (item.candidate_id, item.expected_candidate_revision)
            for item in getattr(projection, "proposal_revisions", ())
            if getattr(item, "candidate_id", None) is not None
            and getattr(item, "expected_candidate_revision", None) is not None
        }
        # A proposal deliberately leaves a candidate ``available`` until
        # Acceptance.  Do not call the model again for the same aggregate
        # revision while that proposal is waiting to be accepted or closed.
        # P2 may already discover ordinary character-media candidates, but it
        # must not let the P1 proposal/Acceptance lane select them until the
        # matching snapshot compiler and bridge are installed.  Filtering at
        # this one seam makes the unavailable phase visible without allowing a
        # model token to reach an authorization contract that cannot honor it.
        selectable = tuple(item for item in eligible if item.family == "life_share")
        candidates = tuple(
            item for item in sorted(selectable, key=lambda value: value.candidate_id)
            if (item.candidate_id, item.entity_revision) not in pending
        )[:32]
        if not candidates:
            return MediaSelectionRunResult(
                status="no_op",
                reason_code=(
                    "media_selection.pending_proposal"
                    if selectable and any(
                        (item.candidate_id, item.entity_revision) in pending for item in selectable
                    )
                    else (
                        "media_selection.character_media_not_yet_authorizable"
                        if eligible
                        else "media_selection.no_available_candidates"
                    )
                ),
            )
        cursor = ProjectionCursor(world_revision=projection.world_revision, deliberation_revision=projection.deliberation_revision, ledger_sequence=projection.ledger_sequence)
        # The token is deliberately distinct from the candidate id: model text
        # cannot become an authority-bearing identifier by coincidence.
        tokens = {"media-candidate:" + hashlib.sha256(item.candidate_id.encode()).hexdigest(): item for item in candidates}
        draw_suggestion = None
        # Production ledgers persist the draw before Deliberation.  Narrow
        # embedded test adapters without durable lookup retain a no-draw path;
        # they cannot claim replayable variability.
        if callable(getattr(self._ledger, "lookup_event_commit", None)):
            draw = self._random.draw(
                attempt_id="media-selection:" + hashlib.sha256(
                    (projection.world_id + logical_time.isoformat() + ":" + ",".join(sorted(item.candidate_id for item in candidates))).encode()
                ).hexdigest(),
                candidate_refs=tuple(item.candidate_id for item in candidates),
                catalog_version="media-selection-random.1", logical_time=logical_time,
                actor=actor, trace_id=trace_id, correlation_id=correlation_id,
            )
            suggested_token = next(token for token, item in tokens.items() if item.candidate_id == draw.selected_candidate_ref)
            draw_suggestion = {"selected_token": suggested_token, "sampler_version": draw.sampler_version}
            projection = self._ledger.project()
            cursor = ProjectionCursor(
                world_revision=projection.world_revision,
                deliberation_revision=projection.deliberation_revision,
                ledger_sequence=projection.ledger_sequence,
            )
        draft = await self._draft.deliberate(capsule=MediaSelectionCapsule(candidates=tuple(
            MediaCandidateChoice(
                token=token,
                safe_summary="一件已确认、可选择但不必分享的生活事件",
                advisory=self._advisory.compile(projection=projection, candidate=tokens[token]).model_material(),
            )
            for token in sorted(tokens)
        ), draw_suggestion=draw_suggestion))
        if draft.decision == "no_op":
            return MediaSelectionRunResult(status="no_op", reason_code="media_selection.model_declined")
        assert draft.token is not None and draft.model and draft.raw_output_hash and draft.normalized_output_hash
        candidate = tokens[draft.token]
        proposal = self._compiler.compile(projection=projection, selection=MediaSelection(candidate_id=candidate.candidate_id, family="life_share"), model=draft.model, raw_output_hash=draft.raw_output_hash, normalized_output_hash=draft.normalized_output_hash)
        recorded = self._recorder.record(cursor=cursor, proposal=proposal, actor=actor, source=self._source, created_at=logical_time, trace_id=trace_id, correlation_id=correlation_id)
        return MediaSelectionRunResult(status="proposed", proposal_event_ref=recorded.proposal_event_ref)


__all__ = ["MediaSelectionRunResult", "MediaSelectionWorker"]

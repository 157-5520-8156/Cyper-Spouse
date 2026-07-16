"""P1 public-preview authorizer: selection plus pinned evidence, no prompt authority."""
from __future__ import annotations
from datetime import datetime
from .media_evidence_snapshot import MediaEvidenceCompileRequest, MediaEvidenceSnapshotCompiler
from .media_selection import MediaSelection
from .media_v2 import MediaOpportunity, media_digest
from .schemas import ProjectionCursor


class MediaOpportunityAuthorizer:
    def __init__(self, *, ledger, compiler: MediaEvidenceSnapshotCompiler, catalog_version: str) -> None:
        self._ledger, self._compiler, self._catalog_version = ledger, compiler, catalog_version

    def authorize(self, *, cursor: ProjectionCursor, selection: MediaSelection, category: str,
                  observed_at: datetime, expires_at: datetime) -> tuple[MediaOpportunity, object]:
        projection = self._ledger.project_at(cursor)
        candidate = next((x for x in projection.photo_candidates if x.candidate_id == selection.candidate_id), None)
        if candidate is None:
            raise ValueError("media_authorizer.candidate_not_available")
        if candidate.status != "available":
            raise ValueError("media_authorizer.candidate_not_available")
        if (
            candidate.opened_at is None
            or candidate.expires_at is None
            or candidate.ecology_category is None
            or candidate.ecology_observed_at is None
            or not candidate.source_events
        ):
            raise ValueError("media_authorizer.candidate_is_not_p1_source_bound")
        if getattr(projection, "logical_time", None) is None or projection.logical_time >= candidate.expires_at:
            raise ValueError("media_authorizer.candidate_expired")
        if (
            category != candidate.ecology_category
            or observed_at != candidate.ecology_observed_at
            or expires_at != candidate.expires_at
        ):
            raise ValueError("media_authorizer.selection_coordinates_do_not_match_candidate")
        if (selection.family, selection.delivery_mode, selection.media_privacy_ceiling,
            selection.expression_charge_ceiling) != ("life_share", "preview", "ordinary", "none"):
            raise ValueError("media_authorizer.p1_public_preview_only")
        compiled = self._compiler.compile(MediaEvidenceCompileRequest(candidate=candidate, category=category, cursor=cursor))
        opportunity = MediaOpportunity(
            opportunity_id="media-opportunity:p1:" + media_digest({"candidate": candidate.candidate_id, "snapshot": compiled.snapshot_hash}),
            candidate_id=candidate.candidate_id, family="life_share", delivery_mode="preview",
            privacy_ceiling=candidate.privacy_ceiling, media_privacy_ceiling="ordinary",
            event_snapshot_ref=compiled.snapshot_ref, event_snapshot_hash=compiled.snapshot_hash,
            source_event_refs=candidate.source_event_refs, catalog_version=self._catalog_version,
            ecology_category=candidate.ecology_category,
            ecology_observed_at=candidate.ecology_observed_at, expires_at=candidate.expires_at,
        )
        return opportunity, compiled


__all__ = ["MediaOpportunityAuthorizer"]

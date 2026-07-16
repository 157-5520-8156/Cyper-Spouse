"""Authorize a public preview from one selected, source-bound candidate."""
from __future__ import annotations
from datetime import datetime
from .media_evidence_snapshot import MediaEvidenceCompileRequest, MediaEvidenceSnapshotCompiler
from .private_media_evidence_snapshot import (
    PrivateMediaEvidenceCompileRequest,
    PrivateMediaEvidenceSnapshotCompiler,
)
from .relationship_media_context import RelationshipMediaContextResolver
from .media_selection import MediaSelection
from .media_v2 import MediaOpportunity, media_digest
from .schemas import ProjectionCursor


class MediaOpportunityAuthorizer:
    def __init__(
        self, *, ledger, compiler: MediaEvidenceSnapshotCompiler, catalog_version: str,
        private_compiler: PrivateMediaEvidenceSnapshotCompiler | None = None,
        relationship_context_resolver: RelationshipMediaContextResolver | None = None,
    ) -> None:
        self._ledger, self._compiler, self._catalog_version = ledger, compiler, catalog_version
        self._private_compiler = private_compiler or PrivateMediaEvidenceSnapshotCompiler(ledger=ledger)
        self._relationship_context_resolver = relationship_context_resolver or RelationshipMediaContextResolver()

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
            or candidate.opened_event_ref is None
            or candidate.opened_event_payload_hash is None
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
        p1 = (selection.family, selection.delivery_mode, selection.media_privacy_ceiling,
              selection.expression_charge_ceiling) == ("life_share", "preview", "ordinary", "none")
        p2 = (
            (selection.family, selection.delivery_mode, selection.media_privacy_ceiling,
             selection.expression_charge_ceiling) == ("character_media", "preview", "ordinary", "none")
            and selection.recipient_ref is None
            and selection.private_expression_basis_ref is None
            and candidate.character_media_contract is not None
        )
        p3 = (
            selection.family == "character_media"
            and selection.delivery_mode == "preview"
            and selection.media_privacy_ceiling == "intimate"
            and selection.expression_charge_ceiling in {"subtle", "charged", "veiled"}
            and selection.recipient_ref is not None
            and selection.private_expression_basis_ref is not None
            and candidate.character_media_contract is not None
            and candidate.privacy_ceiling == "private"
        )
        if not p1 and not p2 and not p3:
            raise ValueError("media_authorizer.public_preview_only")
        if candidate.family != selection.family:
            raise ValueError("media_authorizer.selection_family_does_not_match_candidate")
        if p3:
            return self._authorize_p3(
                cursor=cursor, projection=projection, candidate=candidate, selection=selection,
                category=category, observed_at=observed_at, expires_at=expires_at,
            )
        compiled = self._compiler.compile(MediaEvidenceCompileRequest(candidate=candidate, category=category, cursor=cursor))
        if p2:
            authorization = getattr(compiled.snapshot, "character_media_authorization", None)
            contract = candidate.character_media_contract
            sources = tuple(compiled.snapshot.source_events)
            source_refs = tuple(item.event_ref for item in sources)
            if (
                authorization is None
                or contract is None
                or authorization.candidate_id != candidate.candidate_id
                or authorization.candidate_revision != candidate.entity_revision
                or authorization.subject_ref != contract.subject_ref
                or authorization.kind != contract.kind
                or authorization.allowed_capture_modes != contract.allowed_capture_modes
                or authorization.allowed_character_visibility != contract.allowed_character_visibility
                or authorization.authority_digest != contract.authority_digest
                or authorization.source_event_refs != candidate.source_event_refs
                or not set(candidate.source_event_refs).issubset(source_refs)
            ):
                raise ValueError("media_authorizer.character_snapshot_lineage_invalid")
            opportunity = MediaOpportunity(
                opportunity_id="media-opportunity:p2:" + media_digest({
                    "candidate": candidate.candidate_id, "snapshot": compiled.snapshot_hash,
                    "contract": contract.authority_digest,
                }),
                candidate_id=candidate.candidate_id, family="character_media", delivery_mode="preview",
                privacy_ceiling=candidate.privacy_ceiling, media_privacy_ceiling="ordinary",
                event_snapshot_ref=compiled.snapshot_ref, event_snapshot_hash=compiled.snapshot_hash,
                source_event_refs=source_refs, candidate_source_event_refs=candidate.source_event_refs,
                snapshot_source_events=sources, catalog_version=self._catalog_version,
                ecology_category=candidate.ecology_category,
                ecology_observed_at=candidate.ecology_observed_at, expires_at=candidate.expires_at,
            )
            return opportunity, compiled
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

    def _authorize_p3(
        self, *, cursor: ProjectionCursor, projection, candidate, selection: MediaSelection,
        category: str, observed_at: datetime, expires_at: datetime,
    ) -> tuple[MediaOpportunity, object]:
        contract = candidate.character_media_contract
        assert contract is not None
        resolution = self._relationship_context_resolver.resolve(
            projection=projection, character_ref=contract.subject_ref,
            recipient_ref=selection.recipient_ref or "", at_logical_time=projection.logical_time,
            required_charge="subtle",
        )
        context = resolution.context
        if context is None:
            raise ValueError("media_authorizer.p3_" + (resolution.reason_code or "context_unavailable"))
        if selection.private_expression_basis_ref != context.private_expression_basis.basis_id:
            raise ValueError("media_authorizer.p3_private_basis_not_current")
        lane, maximum = self._p3_lane_for_stage(context.audience.relationship_stage)
        ranks = {"subtle": 1, "charged": 2, "veiled": 3}
        if ranks[selection.expression_charge_ceiling] > ranks[maximum]:
            raise ValueError("media_authorizer.p3_expression_charge_exceeds_relationship_bound")
        compiled = self._private_compiler.compile(
            PrivateMediaEvidenceCompileRequest(
                candidate=candidate, category=category, cursor=cursor, relationship_context=context,
                media_lane=lane, expression_charge_ceiling=selection.expression_charge_ceiling,
            )
        )
        authorization = getattr(compiled.snapshot, "private_media_authorization", None)
        sources = tuple(compiled.snapshot.source_events)
        if (
            authorization is None
            or authorization.candidate_id != candidate.candidate_id
            or authorization.candidate_revision != candidate.entity_revision
            or authorization.recipient_ref != selection.recipient_ref
            or authorization.media_lane != lane
            or authorization.expression_charge_ceiling != selection.expression_charge_ceiling
            or authorization.candidate_contract_digest != contract.authority_digest
            or authorization.relationship_context_digest != context.authority_digest
            or authorization.private_basis_digest != context.private_expression_basis.basis_digest
            or authorization.source_event_refs != tuple(item.event_ref for item in sources)
        ):
            raise ValueError("media_authorizer.p3_snapshot_authorization_invalid")
        return MediaOpportunity(
            opportunity_id="media-opportunity:p3:" + media_digest({
                "candidate": candidate.candidate_id, "snapshot": compiled.snapshot_hash,
                "authorization": authorization.authorization_digest,
            }),
            candidate_id=candidate.candidate_id, family="character_media", delivery_mode="preview",
            privacy_ceiling="private", media_privacy_ceiling="intimate", media_lane=lane,
            recipient_ref=selection.recipient_ref,
            private_expression_basis_ref=context.private_expression_basis.basis_id,
            p3_authorization_digest=authorization.authorization_digest,
            event_snapshot_ref=compiled.snapshot_ref, event_snapshot_hash=compiled.snapshot_hash,
            source_event_refs=tuple(item.event_ref for item in sources),
            candidate_source_event_refs=candidate.source_event_refs, snapshot_source_events=sources,
            catalog_version=self._catalog_version, ecology_category=candidate.ecology_category,
            ecology_observed_at=observed_at, expires_at=expires_at,
        ), compiled

    @staticmethod
    def _p3_lane_for_stage(stage: str) -> tuple[str, str]:
        """Relationship stage constrains expression; it never creates a basis."""

        if stage == "close_friend":
            return "alluring_life", "subtle"
        if stage == "ambiguous":
            return "alluring_life", "charged"
        if stage == "lover":
            # The current World v2 basis module intentionally has no proven
            # coverage/private-transition authority, so even this stage stays
            # in the bounded alluring lane until that fact domain exists.
            return "alluring_life", "charged"
        raise ValueError("media_authorizer.p3_relationship_stage_not_eligible")


__all__ = ["MediaOpportunityAuthorizer"]

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
from .private_image_evidence_contract import RecipientScopedImageEvidenceDeclaredPayload
from .relationship_media_context import (
    PrivateTransitionEvidenceV1,
    RelationshipMediaContextResolver,
)
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
        self._relationship_context_resolver = RelationshipMediaContextResolver()

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
        # Both public life-share and P2 ordinary character candidates are
        # selectable here; the compiler and authorizer independently close
        # their permitted authority shapes before any opportunity is frozen.
        selections = {
            item.candidate_id: selection
            for item in eligible
            if (selection := self._derive_selection(projection=projection, candidate=item)) is not None
        }
        selectable = tuple(item for item in eligible if item.candidate_id in selections)
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
                    else "media_selection.no_available_candidates"
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
        proposal = self._compiler.compile(
            projection=projection,
            selection=selections[candidate.candidate_id],
            model=draft.model,
            raw_output_hash=draft.raw_output_hash,
            normalized_output_hash=draft.normalized_output_hash,
        )
        recorded = self._recorder.record(cursor=cursor, proposal=proposal, actor=actor, source=self._source, created_at=logical_time, trace_id=trace_id, correlation_id=correlation_id)
        return MediaSelectionRunResult(status="proposed", proposal_event_ref=recorded.proposal_event_ref)

    def _derive_selection(self, *, projection, candidate) -> MediaSelection | None:  # type: ignore[no-untyped-def]
        """Derive all P3 authority from ledger facts, never from model output."""

        if candidate.family != "character_media" or candidate.privacy_ceiling != "private":
            return MediaSelection(candidate_id=candidate.candidate_id, family=candidate.family)
        contract = candidate.character_media_contract
        if contract is None or projection.logical_time is None:
            return None
        declarations: list[tuple[RecipientScopedImageEvidenceDeclaredPayload, object]] = []
        for source in candidate.source_events:
            located = self._ledger.lookup_event_commit(source.event_ref)
            if located is None or located[0].payload_hash != source.payload_hash:
                return None
            event, _commit = located
            if event.event_type != "RecipientScopedImageEvidenceDeclared":
                continue
            try:
                declaration = RecipientScopedImageEvidenceDeclaredPayload.model_validate_json(event.payload_json)
            except ValueError:
                return None
            if (
                declaration.source_privacy_ceiling == "private"
                and declaration.image_evidence.character_media is not None
                and declaration.image_evidence.character_media.character_ref == contract.subject_ref
            ):
                declarations.append((declaration, event))
        if len(declarations) != 1:
            return None
        declaration, declaration_event = declarations[0]
        recipient_ref = declaration.recipient_ref
        transition = self._private_transition(
            declaration=declaration, declaration_event=declaration_event,
            valid_until=candidate.expires_at,
        )
        context = self._relationship_context_resolver.resolve(
            projection=projection, character_ref=contract.subject_ref,
            recipient_ref=recipient_ref, at_logical_time=projection.logical_time,
            basis_kind=("private_transition" if transition is not None else "embodied_state"),
            private_transition=transition,
        ).context
        if context is None:
            return None
        return MediaSelection(
            candidate_id=candidate.candidate_id, family="character_media", media_privacy_ceiling="intimate",
            expression_charge_ceiling="subtle", recipient_ref=recipient_ref,
            private_expression_basis_ref=context.private_expression_basis.basis_id,
        )

    @staticmethod
    def _private_transition(*, declaration, declaration_event, valid_until):  # type: ignore[no-untyped-def]
        activity = declaration.image_evidence.activity
        if (
            not isinstance(activity, dict)
            or activity.get("private_transition") is not True
            or not isinstance(activity.get("id"), str)
            or not isinstance(activity.get("kind"), str)
            or valid_until is None
        ):
            return None
        return PrivateTransitionEvidenceV1(
            declaration_event_ref=declaration_event.event_id,
            declaration_event_payload_hash=declaration_event.payload_hash,
            recipient_ref=declaration.recipient_ref, activity_id=activity["id"],
            activity_kind=activity["kind"], valid_until=valid_until,
        )


__all__ = ["MediaSelectionRunResult", "MediaSelectionWorker"]

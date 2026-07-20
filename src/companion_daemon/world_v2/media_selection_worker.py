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
    MediaSelectionDraftError,
)
from .media_selection_proposal import (
    MediaSelectionProposalCompiler,
    MediaSelectionProposalRecordedPayload,
)
from .media_selection_attempt import (
    MediaSelectionCandidateRevision,
    MediaSelectionAttemptRecordedPayload,
    media_selection_attempt_id,
)
from .errors import ConcurrencyConflict
from .event_identity import domain_idempotency_key
from .private_image_evidence_contract import RecipientScopedImageEvidenceDeclaredPayload
from .relationship_media_context import (
    PrivateTransitionEvidenceV1,
    RelationshipMediaContextResolver,
)
from .media_candidate_advisory import MediaCandidateAdvisoryCompiler
from .random_authority import RandomAuthority
from .schema_core import FrozenModel
from .schemas import ProjectionCursor, WorldEvent


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
            (item.candidate_id, item.expected_candidate_revision): item
            for item in getattr(projection, "proposal_revisions", ())
            if getattr(item, "candidate_id", None) is not None
            and getattr(item, "expected_candidate_revision", None) is not None
            and getattr(item, "proposal_event_ref", None) is not None
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
        recoverable: list[str] = []
        invalid_pending = False
        lookup = getattr(self._ledger, "lookup_event_commit", None)
        for item in eligible:
            key = (item.candidate_id, item.entity_revision)
            revision = pending.get(key)
            if item.candidate_id not in selections or revision is None:
                continue
            located = lookup(revision.proposal_event_ref) if callable(lookup) else None
            if located is None:
                invalid_pending = True
                continue
            event, commit = located
            try:
                proposal = MediaSelectionProposalRecordedPayload.model_validate_json(
                    event.payload_json
                )
            except ValueError:
                invalid_pending = True
                continue
            authority_matches = (
                event.event_type == "MediaSelectionProposalRecorded"
                and event.payload_hash == revision.proposal_event_payload_hash
                and proposal.proposal_id == revision.proposal_id
                and proposal.candidate_id == item.candidate_id
                and proposal.expected_candidate_revision == item.entity_revision
            )
            if not authority_matches:
                invalid_pending = True
                continue
            if (
                commit.world_revision == projection.world_revision
                and commit.deliberation_revision == projection.deliberation_revision
                and commit.ledger_sequence == projection.ledger_sequence
            ):
                recoverable.append(event.event_id)
        if recoverable:
            # A crash may occur after ProposalRecorded but before Acceptance.
            # Reuse that immutable head proposal without another model call.
            return MediaSelectionRunResult(
                status="proposed",
                proposal_event_ref=sorted(recoverable)[0],
                reason_code="media_selection.recovered_pending_proposal",
            )
        if invalid_pending:
            # A missing or mismatched audit record is an authority failure,
            # not a stale decision.  Never deliberate around corrupted
            # proposal lineage.
            return MediaSelectionRunResult(
                status="blocked", reason_code="media_selection.pending_proposal_invalid"
            )
        # A valid non-head proposal is immutable stale audit history.  It can
        # no longer be accepted at the current cursor, so Phase 4's fresh-only
        # rule permits a new deliberation.  Proposal identity includes the new
        # complete cursor and therefore cannot collide with the stale record.
        candidates = tuple(
            item for item in sorted(selectable, key=lambda value: value.candidate_id)
        )[:32]
        if not candidates:
            return MediaSelectionRunResult(
                status="no_op",
                reason_code="media_selection.no_available_candidates",
            )
        durable_lookup = callable(getattr(self._ledger, "lookup_event_commit", None))
        world_id = getattr(self._ledger, "world_id", getattr(projection, "world_id", None))
        if durable_lookup and world_id is None:
            return MediaSelectionRunResult(
                status="blocked", reason_code="media_selection.ledger_identity_unavailable"
            )
        candidate_revisions = tuple(
            MediaSelectionCandidateRevision(
                candidate_id=item.candidate_id, entity_revision=item.entity_revision,
            )
            for item in candidates
        )
        attempt_id = media_selection_attempt_id(
            world_id=world_id or "embedded-nondurable", logical_time=logical_time,
            candidates=candidate_revisions,
        )
        attempt_event_id = "event:media-selection-attempt:" + attempt_id
        prior_terminal = (
            self._ledger.lookup_event_commit(attempt_event_id)
            if durable_lookup
            else None
        )
        if prior_terminal is not None:
            event, _commit = prior_terminal
            try:
                terminal = MediaSelectionAttemptRecordedPayload.model_validate_json(
                    event.payload_json
                )
            except ValueError:
                return MediaSelectionRunResult(
                    status="blocked",
                    reason_code="media_selection.decline_audit_invalid",
                )
            expected = tuple(
                MediaSelectionCandidateRevision(
                    candidate_id=item.candidate_id,
                    entity_revision=item.entity_revision,
                )
                for item in candidates
            )
            if (
                event.event_type != "MediaSelectionAttemptRecorded"
                or terminal.attempt_id != attempt_id
                or terminal.candidates != expected
            ):
                return MediaSelectionRunResult(
                    status="blocked",
                    reason_code="media_selection.decline_audit_invalid",
                )
            return MediaSelectionRunResult(
                status="no_op" if terminal.outcome == "declined" else "blocked",
                reason_code=(
                    "media_selection.recovered_decline"
                    if terminal.outcome == "declined"
                    else terminal.failure_code
                ),
            )
        cursor = ProjectionCursor(world_revision=projection.world_revision, deliberation_revision=projection.deliberation_revision, ledger_sequence=projection.ledger_sequence)
        # The token is deliberately distinct from the candidate id: model text
        # cannot become an authority-bearing identifier by coincidence.
        tokens = {"media-candidate:" + hashlib.sha256(item.candidate_id.encode()).hexdigest(): item for item in candidates}
        draw_suggestion = None
        attempt_causation_id: str | None = None
        # Production ledgers persist the draw before Deliberation.  Narrow
        # embedded test adapters without durable lookup retain a no-draw path;
        # they cannot claim replayable variability.
        if durable_lookup:
            try:
                draw = self._random.draw(
                attempt_id=attempt_id,
                candidate_refs=tuple(item.candidate_id for item in candidates),
                catalog_version="media-selection-random.1", logical_time=logical_time,
                actor=actor, trace_id=trace_id, correlation_id=correlation_id,
                )
            except ConcurrencyConflict:
                return MediaSelectionRunResult(
                    status="blocked", reason_code="media_selection.cursor_stale"
                )
            suggested_token = next(token for token, item in tokens.items() if item.candidate_id == draw.selected_candidate_ref)
            draw_suggestion = {"selected_token": suggested_token, "sampler_version": draw.sampler_version}
            attempt_causation_id = "event:random-draw:" + draw.draw_id
            projection = self._ledger.project()
            cursor = ProjectionCursor(
                world_revision=projection.world_revision,
                deliberation_revision=projection.deliberation_revision,
                ledger_sequence=projection.ledger_sequence,
            )
        try:
            draft = await self._draft.deliberate(capsule=MediaSelectionCapsule(candidates=tuple(
                MediaCandidateChoice(
                    token=token,
                    safe_summary="一件已确认、可选择但不必分享的生活事件",
                    advisory=self._advisory.compile(projection=projection, candidate=tokens[token]).model_material(),
                )
                for token in sorted(tokens)
            ), draw_suggestion=draw_suggestion))
        except MediaSelectionDraftError as exc:
            if not durable_lookup or not callable(getattr(self._ledger, "commit_at_cursor", None)):
                return MediaSelectionRunResult(status="blocked", reason_code=exc.code)
            assert attempt_causation_id is not None
            return self._record_terminal_attempt(
                cursor=cursor, candidates=candidates, attempt_id=attempt_id,
                event_id=attempt_event_id, logical_time=logical_time,
                actor=actor, trace_id=trace_id, correlation_id=correlation_id,
                outcome="invalid", model=exc.model,
                raw_output_hash=exc.raw_output_hash,
                normalized_output_hash=None, failure_code=exc.code,
                causation_id=attempt_causation_id,
            )
        except Exception:
            # Provider timeouts/outages are retryable and therefore are not
            # written as semantic declines.  They still become a structured
            # scheduler result so an independent result/background queue can
            # continue in the same bounded drain.
            return MediaSelectionRunResult(
                status="blocked", reason_code="media_selection.model_unavailable"
            )
        if draft.decision == "no_op":
            if durable_lookup and callable(getattr(self._ledger, "commit_at_cursor", None)):
                assert draft.model and draft.raw_output_hash and draft.normalized_output_hash
                assert attempt_causation_id is not None
                return self._record_terminal_attempt(
                    cursor=cursor, candidates=candidates, attempt_id=attempt_id,
                    event_id=attempt_event_id, logical_time=logical_time,
                    actor=actor, trace_id=trace_id, correlation_id=correlation_id,
                    outcome="declined", model=draft.model,
                    raw_output_hash=draft.raw_output_hash,
                    normalized_output_hash=draft.normalized_output_hash,
                    failure_code=None,
                    causation_id=attempt_causation_id,
                )
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

    def _record_terminal_attempt(
        self, *, cursor: ProjectionCursor, candidates, attempt_id: str,
        event_id: str, logical_time: datetime, actor: str, trace_id: str,
        correlation_id: str, outcome: Literal["declined", "invalid"], model: str,
        raw_output_hash: str, normalized_output_hash: str | None,
        failure_code: str | None, causation_id: str,
    ) -> MediaSelectionRunResult:  # type: ignore[no-untyped-def]
        payload = MediaSelectionAttemptRecordedPayload(
            attempt_id=attempt_id,
            candidates=tuple(
                MediaSelectionCandidateRevision(
                    candidate_id=item.candidate_id,
                    entity_revision=item.entity_revision,
                )
                for item in candidates
            ),
            outcome=outcome, model=model, raw_output_hash=raw_output_hash,
            normalized_output_hash=normalized_output_hash, failure_code=failure_code,
        )
        event_payload = payload.model_dump(mode="json")
        event = WorldEvent.from_payload(
            schema_version="world-v2.1", event_id=event_id,
            event_type="MediaSelectionAttemptRecorded", world_id=self._ledger.world_id,
            logical_time=logical_time, created_at=logical_time, actor=actor,
            source=self._source, trace_id=trace_id,
            causation_id=causation_id,
            correlation_id=correlation_id,
            idempotency_key=domain_idempotency_key(
                event_type="MediaSelectionAttemptRecorded",
                world_id=self._ledger.world_id, payload=event_payload,
            ) or "media-selection-attempt:" + attempt_id,
            payload=event_payload,
        )
        try:
            self._ledger.commit_at_cursor(
                (event,), expected_cursor=cursor,
                commit_id="commit:media-selection-attempt:" + attempt_id,
            )
        except ConcurrencyConflict:
            return MediaSelectionRunResult(
                status="blocked", reason_code="media_selection.cursor_stale"
            )
        return MediaSelectionRunResult(
            status="no_op" if outcome == "declined" else "blocked",
            reason_code="media_selection.model_declined" if outcome == "declined" else failure_code,
        )

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

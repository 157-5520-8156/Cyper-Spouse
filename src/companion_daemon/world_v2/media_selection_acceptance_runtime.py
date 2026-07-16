"""Pinned P1 acceptance: selection proposal → frozen preview planning Action."""

from __future__ import annotations

from datetime import datetime
import hashlib
import json

from .accepted_ledger_batch import AcceptedLedgerBatchHandle, AcceptedLedgerBatchIssuer
from .event_identity import domain_idempotency_key
from .ledger import LedgerPort
from .media_opportunity_authorizer import MediaOpportunityAuthorizer
from .media_evidence_snapshot import MediaEvidenceNotRenderable
from .media_selection_acceptance_manifest import (
    build_media_selection_acceptance_manifest,
)
from .media_selection_proposal import (
    MediaSelectionProposalRecordedPayload,
    media_candidate_authority_hash,
)
from .media_v2 import (
    ImmutableMediaPayloadStore,
    MediaOpportunityFrozenPayload,
    PhotoCandidateUnrenderablePayload,
    StoredMediaPayload,
    media_digest,
    planning_request_id,
)
from .schemas import (
    Action,
    BudgetReservation,
    CommitResult,
    ProjectionCursor,
    ProviderMediaGrantBinding,
    WorldEvent,
)


_CONTRACT = "media-selection-acceptance-runtime.1"


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _event_id(*, role: str, stable: str) -> str:
    return "event:media-selection:" + role + ":" + _digest({"role": role, "stable": stable})


def _identity(*, event_type: str, world_id: str, payload: dict[str, object]) -> str:
    exact = domain_idempotency_key(event_type=event_type, world_id=world_id, payload=payload)
    if exact is not None:
        return exact
    return "world-v2:media-selection:" + _digest(
        {"event_type": event_type, "world_id": world_id, "payload": payload}
    )


class MediaSelectionAcceptanceError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = f"media_selection_acceptance.{code}"
        super().__init__(self.code)


class MediaSelectionProposalRecord:
    __slots__ = ("proposal", "proposal_event_ref", "proposal_event_payload_hash", "commit")

    def __init__(self, *, proposal, proposal_event_ref: str, proposal_event_payload_hash: str, commit: CommitResult) -> None:
        self.proposal = proposal
        self.proposal_event_ref = proposal_event_ref
        self.proposal_event_payload_hash = proposal_event_payload_hash
        self.commit = commit


class MediaSelectionProposalRecorder:
    def __init__(self, *, ledger: LedgerPort) -> None:
        self._ledger = ledger

    def record(
        self,
        *,
        cursor: ProjectionCursor,
        proposal: MediaSelectionProposalRecordedPayload,
        actor: str,
        source: str,
        created_at: datetime,
        trace_id: str,
        correlation_id: str,
    ) -> MediaSelectionProposalRecord:
        if (
            proposal.evaluated_world_revision != cursor.world_revision
            or proposal.evaluated_deliberation_revision != cursor.deliberation_revision
            or proposal.evaluated_ledger_sequence != cursor.ledger_sequence
        ):
            raise MediaSelectionAcceptanceError("proposal_cursor_mismatch")
        projection = self._ledger.project_at(cursor)
        if projection.logical_time is None:
            raise MediaSelectionAcceptanceError("logical_time_missing")
        payload = proposal.model_dump(mode="json")
        event = WorldEvent.from_payload(
            schema_version="world-v2.1",
            event_id=_event_id(role="proposal", stable=proposal.proposal_id),
            event_type="MediaSelectionProposalRecorded",
            world_id=self._ledger.world_id,
            logical_time=projection.logical_time,
            created_at=created_at,
            actor=actor,
            source=source,
            trace_id=trace_id,
            causation_id=(
                next(
                    item.opened_event_ref
                    for item in projection.photo_candidates
                    if item.candidate_id == proposal.candidate_id
                )
                or "missing"
            ),
            correlation_id=correlation_id,
            idempotency_key=_identity(
                event_type="MediaSelectionProposalRecorded", world_id=self._ledger.world_id, payload=payload
            ),
            payload=payload,
        )
        commit = self._ledger.commit_at_cursor(
            (event,),
            expected_cursor=cursor,
            commit_id="commit:media-selection-proposal:"
            + _digest({"cursor": cursor.model_dump(mode="json"), "proposal": proposal.proposal_id}),
        )
        return MediaSelectionProposalRecord(
            proposal=proposal,
            proposal_event_ref=event.event_id,
            proposal_event_payload_hash=event.payload_hash,
            commit=commit,
        )


class _PinnedMediaSelectionProposal:
    __slots__ = ("__proposal", "__event", "__cursor", "__issuer")

    def __init__(self, *, proposal, event: WorldEvent, cursor: ProjectionCursor, issuer: object) -> None:
        self.__proposal, self.__event, self.__cursor, self.__issuer = proposal, event, cursor, issuer

    def issued_by(self, issuer: object) -> bool:
        return self.__issuer is issuer

    def __reduce__(self):
        raise TypeError("pinned media selection proposal cannot be serialized")


class MediaSelectionProposalAuthorityReader:
    def __init__(self, *, ledger: LedgerPort) -> None:
        self._ledger, self._issuer = ledger, object()

    def owns(self, handle: _PinnedMediaSelectionProposal) -> bool:
        return type(handle) is _PinnedMediaSelectionProposal and handle.issued_by(self._issuer)

    def pin(self, *, cursor: ProjectionCursor, proposal_event_ref: str) -> _PinnedMediaSelectionProposal:
        projection = self._ledger.project_at(cursor)
        located = self._ledger.lookup_event_commit(proposal_event_ref)
        if located is None:
            raise MediaSelectionAcceptanceError("proposal_event_missing")
        event, commit = located
        if (
            event.world_id != self._ledger.world_id
            or event.event_type != "MediaSelectionProposalRecorded"
            or commit.world_revision != cursor.world_revision
            or commit.deliberation_revision != cursor.deliberation_revision
            or commit.ledger_sequence != cursor.ledger_sequence
        ):
            raise MediaSelectionAcceptanceError("proposal_event_unavailable")
        proposal = MediaSelectionProposalRecordedPayload.model_validate_json(event.payload_json)
        if (
            proposal.evaluated_world_revision != cursor.world_revision
            or proposal.evaluated_deliberation_revision + 1 != cursor.deliberation_revision
            or proposal.evaluated_ledger_sequence + 1 != cursor.ledger_sequence
            or proposal.proposal_id not in projection.proposal_ids
        ):
            raise MediaSelectionAcceptanceError("proposal_stale")
        candidate = next(
            (item for item in projection.photo_candidates if item.candidate_id == proposal.candidate_id), None
        )
        if (
            candidate is None
            or candidate.status != "available"
            or candidate.entity_revision != proposal.expected_candidate_revision
            or candidate.expires_at is None
            or projection.logical_time is None
            or candidate.expires_at <= projection.logical_time
            or media_candidate_authority_hash(candidate) != proposal.candidate_authority_hash
        ):
            raise MediaSelectionAcceptanceError("candidate_not_current")
        return _PinnedMediaSelectionProposal(
            proposal=proposal, event=event, cursor=cursor, issuer=self._issuer
        )


class MediaSelectionAtomicRecorder:
    def __init__(
        self,
        *,
        ledger: LedgerPort,
        reader: MediaSelectionProposalAuthorityReader,
        authorizer: MediaOpportunityAuthorizer,
        sidecar: ImmutableMediaPayloadStore,
        batch_issuer: AcceptedLedgerBatchIssuer,
    ) -> None:
        self._ledger, self._reader, self._authorizer, self._sidecar, self._issuer = (
            ledger,
            reader,
            authorizer,
            sidecar,
            batch_issuer,
        )

    def prepare_batch(
        self,
        *,
        handle: _PinnedMediaSelectionProposal,
        actor: str,
        source: str,
        logical_time: datetime,
        created_at: datetime,
        trace_id: str,
        correlation_id: str,
        grant: ProviderMediaGrantBinding,
        account_id: str,
        amount_limit: int,
    ) -> AcceptedLedgerBatchHandle:
        if not self._reader.owns(handle):
            raise MediaSelectionAcceptanceError("proposal_handle_untrusted")
        proposal = object.__getattribute__(handle, "_PinnedMediaSelectionProposal__proposal")
        proposal_event = object.__getattribute__(handle, "_PinnedMediaSelectionProposal__event")
        cursor = object.__getattribute__(handle, "_PinnedMediaSelectionProposal__cursor")
        projection = self._ledger.project_at(cursor)
        candidate = next(
            (item for item in projection.photo_candidates if item.candidate_id == proposal.candidate_id), None
        )
        if candidate is None or candidate.ecology_category is None or candidate.ecology_observed_at is None or candidate.expires_at is None:
            raise MediaSelectionAcceptanceError("candidate_not_p1_source_bound")
        opportunity, compiled = self._authorizer.authorize(
            cursor=cursor,
            selection=proposal.selection,
            category=candidate.ecology_category,
            observed_at=candidate.ecology_observed_at,
            expires_at=candidate.expires_at,
        )
        opportunity = opportunity.model_copy(
            update={
                "selection_proposal_id": proposal.proposal_id,
                "selection_hash": proposal.selection_hash,
                "selected_candidate_revision": proposal.expected_candidate_revision,
            }
        )
        self._sidecar.put_if_absent(
            StoredMediaPayload(
                payload_ref=compiled.snapshot_ref,
                payload_hash=compiled.snapshot_hash,
                content_type="application/vnd.world-v2.media-opportunity+json",
                body=compiled.snapshot_body,
            )
        )
        request_id = planning_request_id(opportunity.opportunity_id)
        action_id = "action:media-planning:" + _digest(
            {"world_id": self._ledger.world_id, "request_id": request_id}
        )
        reservation_id = "reservation:media-planning:" + _digest(
            {"world_id": self._ledger.world_id, "request_id": request_id}
        )
        acceptance_id = "acceptance:media-selection:" + _digest(
            {"proposal_id": proposal.proposal_id, "change_id": proposal.change_id}
        )
        acceptance_event_id = _event_id(role="acceptance", stable=acceptance_id)
        opportunity_payload = MediaOpportunityFrozenPayload(opportunity=opportunity).model_dump(mode="json")
        reservation = BudgetReservation(
            reservation_id=reservation_id,
            account_id=account_id,
            action_id=action_id,
            category="image",
            amount_limit=amount_limit,
        )
        reservation_payload = {"reservation": reservation.model_dump(mode="json")}
        opportunity_event_id = _event_id(role="opportunity", stable=opportunity.opportunity_id)
        reservation_event_id = _event_id(role="reservation", stable=reservation_id)
        action_event_id = _event_id(role="action", stable=action_id)
        action = Action(
            schema_version="world-v2.1",
            action_id=action_id,
            world_id=self._ledger.world_id,
            logical_time=logical_time,
            created_at=created_at,
            trace_id=trace_id,
            causation_id=reservation_event_id,
            correlation_id=correlation_id,
            kind="media_planning",
            layer="media_action",
            intent_ref=opportunity.opportunity_id,
            actor=actor,
            target="provider:media-planner",
            payload_ref=opportunity.event_snapshot_ref,
            payload_hash=opportunity.event_snapshot_hash,
            provider_media_grant=grant,
            idempotency_key=request_id,
            budget_reservation_id=reservation_id,
            state="authorized",
            recovery_policy="effect_once",
        )
        action_payload = {"action": action.model_dump(mode="json")}
        manifest = build_media_selection_acceptance_manifest(
            acceptance_id=acceptance_id,
            acceptance_event_ref=acceptance_event_id,
            proposal_id=proposal.proposal_id,
            proposal_event_ref=proposal_event.event_id,
            proposal_event_payload_hash=proposal_event.payload_hash,
            evaluated_world_revision=cursor.world_revision,
            accepted_change_id=proposal.change_id,
            accepted_change_hash=proposal.proposed_change_hash,
            candidate_id=proposal.candidate_id,
            expected_candidate_revision=proposal.expected_candidate_revision,
            candidate_authority_hash=proposal.candidate_authority_hash,
            selection_hash=proposal.selection_hash,
            opportunity_event_id=opportunity_event_id,
            opportunity_payload_hash=hashlib.sha256(
                json.dumps(opportunity_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
            opportunity_id=opportunity.opportunity_id,
            snapshot_ref=opportunity.event_snapshot_ref,
            snapshot_hash=opportunity.event_snapshot_hash,
            reservation_event_id=reservation_event_id,
            reservation_payload_hash=hashlib.sha256(
                json.dumps(reservation_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
            action_event_id=action_event_id,
            action_payload_hash=hashlib.sha256(
                json.dumps(action_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
            policy_digest=proposal.policy_digest,
        )
        common = {
            "schema_version": "world-v2.1",
            "world_id": self._ledger.world_id,
            "logical_time": logical_time,
            "created_at": created_at,
            "actor": actor,
            "source": source,
            "trace_id": trace_id,
            "correlation_id": correlation_id,
        }
        acceptance_payload = manifest.model_dump(mode="json")
        events = (
            WorldEvent.from_payload(
                **common,
                event_id=acceptance_event_id,
                event_type="AcceptanceRecorded",
                causation_id=proposal_event.event_id,
                idempotency_key=_identity(event_type="AcceptanceRecorded", world_id=self._ledger.world_id, payload=acceptance_payload),
                payload=acceptance_payload,
            ),
            WorldEvent.from_payload(
                **common,
                event_id=opportunity_event_id,
                event_type="MediaOpportunityFrozen",
                causation_id=acceptance_event_id,
                idempotency_key=_identity(event_type="MediaOpportunityFrozen", world_id=self._ledger.world_id, payload=opportunity_payload),
                payload=opportunity_payload,
            ),
            WorldEvent.from_payload(
                **common,
                event_id=reservation_event_id,
                event_type="BudgetReserved",
                causation_id=opportunity_event_id,
                idempotency_key=_identity(event_type="BudgetReserved", world_id=self._ledger.world_id, payload=reservation_payload),
                payload=reservation_payload,
            ),
            WorldEvent.from_payload(
                **common,
                event_id=action_event_id,
                event_type="ActionAuthorized",
                causation_id=reservation_event_id,
                idempotency_key=_identity(event_type="ActionAuthorized", world_id=self._ledger.world_id, payload=action_payload),
                payload=action_payload,
            ),
        )
        return self._issuer.issue(
            world_id=self._ledger.world_id,
            expected_cursor=cursor,
            events=events,
            manifest_hash=manifest.manifest_hash,
            registry_digest=proposal.policy_digest,
            commit_id="commit:media-selection-acceptance:"
            + _digest({"cursor": cursor.model_dump(mode="json"), "manifest": manifest.manifest_hash}),
        )

    def record_unrenderable(
        self,
        *,
        handle: _PinnedMediaSelectionProposal,
        actor: str,
        source: str,
        logical_time: datetime,
        created_at: datetime,
        trace_id: str,
        correlation_id: str,
        reason_code: str,
    ) -> CommitResult:
        """Close the exact candidate when frozen evidence cannot be compiled.

        This is a mechanical terminal fact, not an Acceptance substitute: no
        opportunity, budget, or provider Action was authorized.  Keeping it
        outside the accepted four-effect batch also means a failed compiler
        can never leave an orphaned reservation.
        """

        if not self._reader.owns(handle):
            raise MediaSelectionAcceptanceError("proposal_handle_untrusted")
        proposal = object.__getattribute__(handle, "_PinnedMediaSelectionProposal__proposal")
        proposal_event = object.__getattribute__(handle, "_PinnedMediaSelectionProposal__event")
        cursor = object.__getattribute__(handle, "_PinnedMediaSelectionProposal__cursor")
        projection = self._ledger.project_at(cursor)
        candidate = next(
            (item for item in projection.photo_candidates if item.candidate_id == proposal.candidate_id), None
        )
        if (
            candidate is None
            or candidate.status != "available"
            or candidate.entity_revision != proposal.expected_candidate_revision
            or projection.logical_time != logical_time
        ):
            raise MediaSelectionAcceptanceError("candidate_not_current")
        payload = PhotoCandidateUnrenderablePayload(
            candidate_id=candidate.candidate_id,
            expected_entity_revision=candidate.entity_revision,
            reason_code=reason_code,
        ).model_dump(mode="json")
        event = WorldEvent.from_payload(
            schema_version="world-v2.1",
            event_id=_event_id(
                role="candidate-unrenderable",
                stable=_digest({
                    "candidate_id": candidate.candidate_id,
                    "reason_code": reason_code,
                    "revision": candidate.entity_revision,
                }),
            ),
            event_type="PhotoCandidateUnrenderable",
            world_id=self._ledger.world_id,
            logical_time=logical_time,
            created_at=created_at,
            actor=actor,
            source=source,
            trace_id=trace_id,
            causation_id=proposal_event.event_id,
            correlation_id=correlation_id,
            idempotency_key=_identity(
                event_type="PhotoCandidateUnrenderable", world_id=self._ledger.world_id, payload=payload
            ),
            payload=payload,
        )
        return self._ledger.commit_at_cursor(
            (event,),
            expected_cursor=cursor,
            commit_id="commit:media-selection-unrenderable:" + _digest(
                {"cursor": cursor.model_dump(mode="json"), "event_id": event.event_id}
            ),
        )


class MediaSelectionAcceptanceRuntime:
    def __init__(
        self,
        *,
        ledger: LedgerPort,
        authorizer: MediaOpportunityAuthorizer,
        sidecar: ImmutableMediaPayloadStore,
        batch_issuer: AcceptedLedgerBatchIssuer,
    ) -> None:
        self.ledger = ledger
        reader = MediaSelectionProposalAuthorityReader(ledger=ledger)
        self._reader = reader
        self._recorder = MediaSelectionAtomicRecorder(
            ledger=ledger,
            reader=reader,
            authorizer=authorizer,
            sidecar=sidecar,
            batch_issuer=batch_issuer,
        )

    def pin_proposal(self, *, cursor: ProjectionCursor, proposal_event_ref: str) -> _PinnedMediaSelectionProposal:
        return self._reader.pin(cursor=cursor, proposal_event_ref=proposal_event_ref)

    def accept(
        self,
        *,
        handle: _PinnedMediaSelectionProposal,
        actor: str,
        source: str,
        logical_time: datetime,
        created_at: datetime,
        trace_id: str,
        correlation_id: str,
        grant: ProviderMediaGrantBinding,
        account_id: str,
        amount_limit: int,
    ) -> CommitResult:
        cursor = object.__getattribute__(handle, "_PinnedMediaSelectionProposal__cursor")
        try:
            batch = self._recorder.prepare_batch(
                handle=handle,
                actor=actor,
                source=source,
                logical_time=logical_time,
                created_at=created_at,
                trace_id=trace_id,
                correlation_id=correlation_id,
                grant=grant,
                account_id=account_id,
                amount_limit=amount_limit,
            )
        except MediaEvidenceNotRenderable as exc:
            return self._recorder.record_unrenderable(
                handle=handle,
                actor=actor,
                source=source,
                logical_time=logical_time,
                created_at=created_at,
                trace_id=trace_id,
                correlation_id=correlation_id,
                reason_code=exc.reason_code,
            )
        return self.ledger.commit_accepted(batch, expected_cursor=cursor)


__all__ = [
    "MediaSelectionAcceptanceError",
    "MediaSelectionAcceptanceRuntime",
    "MediaSelectionAtomicRecorder",
    "MediaSelectionProposalAuthorityReader",
    "MediaSelectionProposalRecord",
    "MediaSelectionProposalRecorder",
]

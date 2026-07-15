"""Pinned atomic acceptance for the dedicated delivered-media Thread lane."""

from __future__ import annotations

from datetime import datetime
import hashlib
import json

from .accepted_ledger_batch import AcceptedLedgerBatchHandle, AcceptedLedgerBatchIssuer
from .event_identity import domain_idempotency_key
from .ledger import LedgerPort
from .media_thread_acceptance_manifest import (
    build_media_thread_acceptance_manifest,
    canonical_media_thread_value_hash,
)
from .media_thread_events import MediaDeliveryThreadChangedPayload
from .schemas import CommitResult, ProjectionCursor, WorldEvent


_POLICY_VERSION = "media-delivery-thread-acceptance-policy.1"
_POLICY_DIGEST = hashlib.sha256(
    json.dumps(
        {
            "contract": _POLICY_VERSION,
            "source": "MediaDeliveryShared",
            "effect": "MediaDeliveryThreadChanged",
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
).hexdigest()


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class MediaThreadAcceptanceError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = f"media_thread_acceptance.{code}"
        super().__init__(self.code)


class PinnedMediaThreadProposalHandle:
    __slots__ = ("__proposal", "__event", "__cursor", "__issuer")

    def __init__(
        self, *, proposal, event: WorldEvent, cursor: ProjectionCursor, issuer: object
    ) -> None:
        self.__proposal, self.__event, self.__cursor, self.__issuer = (
            proposal,
            event,
            cursor,
            issuer,
        )

    def issued_by(self, issuer: object) -> bool:
        return self.__issuer is issuer

    def __reduce__(self):
        raise TypeError("pinned media thread handles cannot be serialized")


class MediaThreadProposalAuthorityReader:
    __slots__ = ("_ledger", "_issuer")

    def __init__(self, *, ledger: LedgerPort) -> None:
        self._ledger, self._issuer = ledger, object()

    def owns(self, handle: PinnedMediaThreadProposalHandle) -> bool:
        return type(handle) is PinnedMediaThreadProposalHandle and handle.issued_by(self._issuer)

    def pin(
        self, *, world_id: str, cursor: ProjectionCursor, proposal_id: str
    ) -> PinnedMediaThreadProposalHandle:
        if world_id != self._ledger.world_id:
            raise MediaThreadAcceptanceError("authority_world_mismatch")
        projection = self._ledger.project_at(cursor)
        proposal = next(
            (
                item
                for item in projection.media_thread_proposals
                if item.media_thread_proposal_id == proposal_id
            ),
            None,
        )
        if proposal is None or proposal.evaluated_world_revision != cursor.world_revision:
            raise MediaThreadAcceptanceError("proposal_not_current")
        source = self._ledger.lookup_event_commit(proposal.delivery_event_ref)
        trigger = next(
            (
                item
                for item in projection.trigger_processes
                if item.trigger_id == proposal.deliberation_trigger_id
            ),
            None,
        )
        if (
            source is None
            or source[0].event_type != "MediaDeliveryShared"
            or source[0].payload_hash != proposal.delivery_event_payload_hash
            or source[1].world_revision > cursor.world_revision
            or trigger is None
            or trigger.process_kind != "media_delivery_interaction"
            or trigger.state != "claimed"
            or trigger.claim_lease is None
            or trigger.source_evidence_ref != proposal.delivery_event_ref
        ):
            raise MediaThreadAcceptanceError("delivery_source_unavailable")
        identity = _digest(
            {
                "contract": "media-delivery-thread-proposal-compiler.1",
                "audit": self._audit_ref(projection, proposal.decision_proposal_id),
                "change": proposal.change_id,
            }
        )
        recorded = self._ledger.lookup_event_commit(
            f"event:media-delivery-thread-proposal:{identity}"
        )
        if (
            recorded is None
            or recorded[0].event_type != "MediaDeliveryThreadProposalRecorded"
            or recorded[0].payload() != proposal.model_dump(mode="json")
        ):
            raise MediaThreadAcceptanceError("proposal_event_missing")
        return PinnedMediaThreadProposalHandle(
            proposal=proposal, event=recorded[0], cursor=cursor, issuer=self._issuer
        )

    @staticmethod
    def _audit_ref(projection, proposal_id: str) -> str:
        audit = next(
            (item for item in projection.proposal_audits if item.proposal_id == proposal_id), None
        )
        if audit is None:
            raise MediaThreadAcceptanceError("source_audit_missing")
        return audit.event_ref


class MediaThreadAtomicRecorder:
    def __init__(
        self, *, reader: MediaThreadProposalAuthorityReader, batch_issuer: AcceptedLedgerBatchIssuer
    ) -> None:
        self._reader, self._issuer = reader, batch_issuer

    def prepare_batch(
        self,
        *,
        handle: PinnedMediaThreadProposalHandle,
        actor: str,
        source: str,
        logical_time: datetime,
        created_at: datetime,
        trace_id: str,
        correlation_id: str,
    ) -> AcceptedLedgerBatchHandle:
        if not self._reader.owns(handle):
            raise MediaThreadAcceptanceError("proposal_handle_untrusted")
        proposal = object.__getattribute__(handle, "_PinnedMediaThreadProposalHandle__proposal")
        proposal_event = object.__getattribute__(handle, "_PinnedMediaThreadProposalHandle__event")
        cursor = object.__getattribute__(handle, "_PinnedMediaThreadProposalHandle__cursor")
        if proposal.evaluated_world_revision != cursor.world_revision:
            raise MediaThreadAcceptanceError("proposal_stale")
        acceptance_id = "acceptance:media-delivery-thread:" + _digest(
            {"proposal": proposal.media_thread_proposal_id, "change": proposal.change_id}
        )
        acceptance_event_id = "event:media-delivery-thread-acceptance:" + _digest(
            {"proposal": proposal.media_thread_proposal_id}
        )
        event_type = (
            "MediaDeliveryThreadOpened"
            if proposal.operation == "open"
            else "MediaDeliveryThreadUpdated"
        )
        thread_event_id = proposal.thread_after.origin.accepted_event_ref
        changed_payload = MediaDeliveryThreadChangedPayload(
            change_id=proposal.change_id,
            transition_id=proposal.transition_id,
            acceptance_id=acceptance_id,
            proposal_id=proposal.media_thread_proposal_id,
            operation=proposal.operation,
            expected_entity_revision=proposal.expected_entity_revision,
            evaluated_world_revision=cursor.world_revision,
            accepted_change_hash=proposal.proposed_change_hash,
            evidence_refs=proposal.evidence_refs,
            policy_refs=proposal.policy_refs,
            thread_before=proposal.thread_before,
            thread_after=proposal.thread_after,
        ).model_dump(mode="json")
        manifest = build_media_thread_acceptance_manifest(
            acceptance_id=acceptance_id,
            proposal_id=proposal.media_thread_proposal_id,
            proposal_event_ref=proposal_event.event_id,
            proposal_event_payload_hash=proposal_event.payload_hash,
            evaluated_world_revision=cursor.world_revision,
            accepted_change_id=proposal.change_id,
            accepted_change_hash=proposal.proposed_change_hash,
            delivery_id=proposal.delivery_id,
            delivery_event_ref=proposal.delivery_event_ref,
            delivery_event_payload_hash=proposal.delivery_event_payload_hash,
            deliberation_trigger_id=proposal.deliberation_trigger_id,
            thread_event_id=thread_event_id,
            thread_event_type=event_type,
            thread_payload_hash=canonical_media_thread_value_hash(changed_payload),
            policy_digest=_POLICY_DIGEST,
        )
        common = {
            "schema_version": "world-v2.1",
            "world_id": proposal_event.world_id,
            "logical_time": logical_time,
            "created_at": created_at,
            "actor": actor,
            "source": source,
            "trace_id": trace_id,
            "correlation_id": correlation_id,
        }
        acceptance_payload = manifest.model_dump(mode="json")
        akey = domain_idempotency_key(
            event_type="AcceptanceRecorded",
            world_id=proposal_event.world_id,
            payload=acceptance_payload,
        )
        tkey = domain_idempotency_key(
            event_type=event_type, world_id=proposal_event.world_id, payload=changed_payload
        )
        if akey is None or tkey is None:
            raise MediaThreadAcceptanceError("event_identity_missing")
        events = (
            WorldEvent.from_payload(
                **common,
                event_id=acceptance_event_id,
                event_type="AcceptanceRecorded",
                causation_id=proposal_event.event_id,
                idempotency_key=akey,
                payload=acceptance_payload,
            ),
            WorldEvent.from_payload(
                **common,
                event_id=thread_event_id,
                event_type=event_type,
                causation_id=acceptance_event_id,
                idempotency_key=tkey,
                payload=changed_payload,
            ),
        )
        return self._issuer.issue(
            world_id=proposal_event.world_id,
            expected_cursor=cursor,
            events=events,
            manifest_hash=manifest.manifest_hash,
            registry_digest=_POLICY_DIGEST,
            commit_id="commit:media-delivery-thread-acceptance:"
            + _digest(
                {"cursor": cursor.model_dump(mode="json"), "manifest": manifest.manifest_hash}
            ),
        )


class MediaDeliveryThreadAcceptanceRuntime:
    def __init__(self, *, ledger: LedgerPort, batch_issuer: AcceptedLedgerBatchIssuer) -> None:
        self.ledger = ledger
        self._reader = MediaThreadProposalAuthorityReader(ledger=ledger)
        self._recorder = MediaThreadAtomicRecorder(reader=self._reader, batch_issuer=batch_issuer)

    def pin_proposal(
        self, *, cursor: ProjectionCursor, proposal_id: str
    ) -> PinnedMediaThreadProposalHandle:
        return self._reader.pin(
            world_id=self.ledger.world_id, cursor=cursor, proposal_id=proposal_id
        )

    def accept(
        self,
        *,
        handle: PinnedMediaThreadProposalHandle,
        actor: str,
        source: str,
        logical_time: datetime,
        created_at: datetime,
        trace_id: str,
        correlation_id: str,
    ) -> CommitResult:
        cursor = object.__getattribute__(handle, "_PinnedMediaThreadProposalHandle__cursor")
        return self.ledger.commit_accepted(
            self._recorder.prepare_batch(
                handle=handle,
                actor=actor,
                source=source,
                logical_time=logical_time,
                created_at=created_at,
                trace_id=trace_id,
                correlation_id=correlation_id,
            ),
            expected_cursor=cursor,
        )

    def accept_runtime_owned(
        self, *, handle: PinnedMediaThreadProposalHandle, actor: str, source: str
    ) -> CommitResult:
        event = object.__getattribute__(handle, "_PinnedMediaThreadProposalHandle__event")
        return self.accept(
            handle=handle,
            actor=actor,
            source=source,
            logical_time=event.logical_time,
            created_at=event.created_at,
            trace_id=event.trace_id,
            correlation_id=event.correlation_id,
        )


__all__ = [
    "MediaDeliveryThreadAcceptanceRuntime",
    "MediaThreadAcceptanceError",
    "MediaThreadAtomicRecorder",
    "MediaThreadProposalAuthorityReader",
    "PinnedMediaThreadProposalHandle",
]

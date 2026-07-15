"""Production acceptance vertical for persisted, typed Appraisal proposals.

The public module is deliberately small: pin an appraisal proposal at a cursor,
then accept that opaque handle.  The implementation owns event identity,
manifest material, trigger completion and the recorder capability; callers
never receive a mutable event sequence.
"""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
from pathlib import Path

from .accepted_ledger_batch import (
    AcceptedLedgerBatchHandle,
    AcceptedLedgerBatchIssuer,
)
from .appraisal_acceptance_manifest import (
    build_appraisal_acceptance_manifest,
    canonical_appraisal_acceptance_value_hash,
)
from .appraisal_events import (
    AppraisalAcceptedPayload,
    AppraisalContradictedPayload,
    AppraisalSupersededPayload,
)
from .event_identity import domain_idempotency_key
from .ledger import LedgerPort, WorldLedger
from .schemas import CommitResult, ProjectionCursor, TriggerProcess, WorldEvent
from .sqlite_ledger import SQLiteWorldLedger


APPRAISAL_ACCEPTANCE_POLICY_VERSION = "appraisal-acceptance-policy.1"


def _canonical_json(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    )


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


APPRAISAL_ACCEPTANCE_POLICY_DIGEST = _digest(
    {
        "contract": APPRAISAL_ACCEPTANCE_POLICY_VERSION,
        "mutation_event_types": (
            "AppraisalAccepted",
            "AppraisalContradicted",
            "AppraisalSuperseded",
        ),
        "requires_trigger_completion": True,
    }
)


class AppraisalAcceptanceError(ValueError):
    """Stable failure at the Appraisal Acceptance seam."""

    def __init__(self, code: str) -> None:
        self.code = f"appraisal_acceptance.{code}"
        super().__init__(self.code)


class PinnedAppraisalProposalAuthorityHandle:
    """Reader-owned proof of one current, persisted appraisal proposal."""

    __slots__ = ("__proposal", "__proposal_event", "__trigger", "__cursor", "__issuer")

    def __init__(
        self,
        *,
        proposal: object,
        proposal_event: WorldEvent,
        trigger: TriggerProcess,
        cursor: ProjectionCursor,
        issuer: object,
    ) -> None:
        self.__proposal = proposal
        self.__proposal_event = proposal_event
        self.__trigger = trigger
        self.__cursor = cursor
        self.__issuer = issuer

    def issued_by(self, issuer: object) -> bool:
        return self.__issuer is issuer

    def __reduce__(self) -> object:
        raise TypeError("pinned Appraisal proposal handles cannot be serialized")

    def __copy__(self) -> object:
        raise TypeError("pinned Appraisal proposal handles cannot be copied")

    def __deepcopy__(self, memo: object) -> object:
        del memo
        raise TypeError("pinned Appraisal proposal handles cannot be copied")


class AppraisalProposalAuthorityReader:
    """Deep read seam: pin and fully verify one typed Appraisal proposal."""

    __slots__ = ("__ledger", "__issuer")

    def __init__(self, *, ledger: LedgerPort) -> None:
        self.__ledger = ledger
        self.__issuer = object()

    def pin(
        self, *, world_id: str, cursor: ProjectionCursor, proposal_id: str
    ) -> PinnedAppraisalProposalAuthorityHandle:
        if world_id != self.__ledger.world_id:
            raise AppraisalAcceptanceError("authority_world_mismatch")
        projection = self.__ledger.project_at(cursor)
        proposal = next(
            (item for item in projection.appraisal_proposals if item.proposal_id == proposal_id),
            None,
        )
        if proposal is None:
            raise AppraisalAcceptanceError("proposal_not_persisted")
        trigger = next(
            (item for item in projection.trigger_processes if item.trigger_id == proposal.trigger_id),
            None,
        )
        if trigger is None or trigger.state != "claimed" or trigger.claim_lease is None:
            raise AppraisalAcceptanceError("trigger_not_claimed")
        # A ledger indexes events by event id, not proposal id.  Scan only the
        # immutable committed reference list to locate the proposal event, then
        # re-open its exact envelope for full-byte verification.
        proposal_ref = next(
            (ref.event_id for ref in projection.committed_world_event_refs if ref.event_type == "ProposalRecorded"),
            None,
        )
        # Deliberation events are intentionally absent from committed_world_event_refs.
        # Use the ledger's own audit projection for deterministic event identity instead.
        audit = next(
            (item for item in projection.proposal_audits if item.proposal_id == proposal_id), None
        )
        if audit is not None:
            proposal_ref = audit.event_ref
        if proposal_ref is None:
            # Legacy typed proposals have no separate audit projection.  Their
            # durable idempotency identity is the only exact lookup available.
            # The read is performed by a dedicated query in the concrete adapters
            # below rather than accepting an unverified DTO.
            proposal_event = _find_legacy_appraisal_event(self.__ledger, proposal_id, cursor=cursor)
        else:
            located_event = self.__ledger.lookup_event_commit(proposal_ref)
            proposal_event = located_event[0] if located_event is not None else None
        if proposal_event is None or proposal_event.event_type != "ProposalRecorded":
            raise AppraisalAcceptanceError("proposal_event_missing")
        if proposal_event.payload() != proposal.model_dump(mode="json"):
            raise AppraisalAcceptanceError("proposal_event_mismatch")
        return PinnedAppraisalProposalAuthorityHandle(
            proposal=proposal,
            proposal_event=proposal_event,
            trigger=trigger,
            cursor=cursor,
            issuer=self.__issuer,
        )

    def owns(self, handle: PinnedAppraisalProposalAuthorityHandle) -> bool:
        return type(handle) is PinnedAppraisalProposalAuthorityHandle and handle.issued_by(
            self.__issuer
        )


def _find_legacy_appraisal_event(
    ledger: LedgerPort, proposal_id: str, *, cursor: ProjectionCursor
) -> WorldEvent | None:
    """Adapter-specific exact lookup for legacy typed deliberation proposals.

    Legacy Appraisal projections predate ``ProposalAuditProjection``.  Rather
    than broaden ``LedgerPort`` with an unbounded event search, both shipped
    adapters expose this internal read only to the production reader.
    """

    finder = getattr(ledger, "_find_appraisal_proposal_event", None)
    if finder is None:
        raise AppraisalAcceptanceError("proposal_lookup_unsupported")
    event = finder(proposal_id=proposal_id, cursor=cursor)
    if event is not None and type(event) is not WorldEvent:
        raise AppraisalAcceptanceError("proposal_event_invalid")
    return event


def appraisal_acceptance_event_id(*, world_id: str, proposal_id: str, change_id: str) -> str:
    return "event:appraisal-acceptance:" + _digest(
        {"world_id": world_id, "proposal_id": proposal_id, "change_id": change_id}
    )


def appraisal_mutation_event_id(
    *, world_id: str, proposal_id: str, transition_id: str, event_type: str
) -> str:
    return "event:appraisal-mutation:" + _digest(
        {
            "world_id": world_id,
            "proposal_id": proposal_id,
            "transition_id": transition_id,
            "event_type": event_type,
        }
    )


def _completion_event_id(*, manifest_hash: str) -> str:
    return f"event:appraisal-trigger-completed:{manifest_hash}"


def _private_idempotency_key(*, world_id: str, manifest_hash: str, role: str) -> str:
    return f"world-v2:appraisal-acceptance:{role}:" + _digest(
        {"world_id": world_id, "manifest_hash": manifest_hash, "role": role}
    )


class AppraisalAtomicRecorder:
    """Sole event materializer for the Appraisal accepted-manifest lane."""

    __slots__ = ("__reader", "__batch_issuer")

    def __init__(
        self,
        *,
        proposal_reader: AppraisalProposalAuthorityReader,
        batch_issuer: AcceptedLedgerBatchIssuer,
    ) -> None:
        self.__reader = proposal_reader
        self.__batch_issuer = batch_issuer

    def prepare_batch(
        self,
        *,
        handle: PinnedAppraisalProposalAuthorityHandle,
        actor: str,
        source: str,
        logical_time: datetime,
        created_at: datetime,
        trace_id: str,
        correlation_id: str,
        completed_at: datetime,
    ) -> AcceptedLedgerBatchHandle:
        if not self.__reader.owns(handle):
            raise AppraisalAcceptanceError("proposal_handle_untrusted")
        proposal = object.__getattribute__(handle, "_PinnedAppraisalProposalAuthorityHandle__proposal")
        proposal_event = object.__getattribute__(
            handle, "_PinnedAppraisalProposalAuthorityHandle__proposal_event"
        )
        trigger = object.__getattribute__(handle, "_PinnedAppraisalProposalAuthorityHandle__trigger")
        cursor = object.__getattribute__(handle, "_PinnedAppraisalProposalAuthorityHandle__cursor")
        if proposal.evaluated_world_revision != cursor.world_revision:
            raise AppraisalAcceptanceError("proposal_stale")
        mutation_type = proposal.proposed_mutation.event_type
        mutation_payload = json.loads(proposal.proposed_mutation.payload_json)
        mutation_model = {
            "AppraisalAccepted": AppraisalAcceptedPayload,
            "AppraisalContradicted": AppraisalContradictedPayload,
            "AppraisalSuperseded": AppraisalSupersededPayload,
        }[mutation_type]
        mutation = mutation_model.model_validate_json(proposal.proposed_mutation.payload_json)
        if (
            mutation.proposal_id != proposal.proposal_id
            or mutation.change_id != proposal.change_id
            or mutation.trigger_id != trigger.trigger_id
            or mutation.evaluated_world_revision != cursor.world_revision
            or mutation.accepted_change_hash != proposal.proposed_change_hash
        ):
            raise AppraisalAcceptanceError("proposal_mutation_mismatch")
        mutation_event_id = appraisal_mutation_event_id(
            world_id=proposal_event.world_id,
            proposal_id=proposal.proposal_id,
            transition_id=mutation.transition_id,
            event_type=mutation_type,
        )
        if isinstance(mutation, AppraisalAcceptedPayload):
            accepted_event_ref = mutation.appraisal.origin.accepted_event_ref
        elif isinstance(mutation, AppraisalSupersededPayload):
            accepted_event_ref = mutation.successor.origin.accepted_event_ref
        else:
            accepted_event_ref = mutation_event_id
        if accepted_event_ref != mutation_event_id:
            raise AppraisalAcceptanceError("mutation_event_identity_not_bound")
        outcome_ref = (
            f"appraisal:{mutation.appraisal.appraisal_id}"
            if isinstance(mutation, AppraisalAcceptedPayload)
            else f"appraisal:{mutation.successor.appraisal_id}"
            if isinstance(mutation, AppraisalSupersededPayload)
            else f"appraisal:{mutation.appraisal_id}:contradicted"
        )
        completion_payload = {
            "trigger_id": trigger.trigger_id,
            "owner_id": trigger.claim_lease.owner_id,
            "attempt_id": trigger.claim_lease.attempt_id,
            "completed_at": completed_at.isoformat(),
            "runtime_outcome_ref": outcome_ref,
        }
        provisional = {
            "acceptance_id": mutation.acceptance_id,
            "proposal_id": proposal.proposal_id,
            "proposal_event_ref": proposal_event.event_id,
            "proposal_event_payload_hash": proposal_event.payload_hash,
            "evaluated_world_revision": cursor.world_revision,
            "accepted_change_id": mutation.change_id,
            "accepted_change_hash": mutation.accepted_change_hash,
            "trigger_id": trigger.trigger_id,
            "mutation_event_id": mutation_event_id,
            "mutation_event_type": mutation_type,
            "mutation_payload_hash": canonical_appraisal_acceptance_value_hash(mutation_payload),
            "completion_event_id": "pending",
            "completion_payload_hash": canonical_appraisal_acceptance_value_hash(completion_payload),
            "policy_digest": APPRAISAL_ACCEPTANCE_POLICY_DIGEST,
        }
        # A self-referential manifest/event id would be unstable.  Derive the
        # completion identity only from immutable proposal authority instead.
        completion_id = "event:appraisal-trigger-completed:" + _digest(
            {"world_id": proposal_event.world_id, "proposal_id": proposal.proposal_id, "trigger_id": trigger.trigger_id}
        )
        manifest = build_appraisal_acceptance_manifest(
            **{**provisional, "completion_event_id": completion_id}
        )
        acceptance_event_id = appraisal_acceptance_event_id(
            world_id=proposal_event.world_id,
            proposal_id=proposal.proposal_id,
            change_id=mutation.change_id,
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
        acceptance_identity = domain_idempotency_key(
            event_type="AcceptanceRecorded", world_id=proposal_event.world_id, payload=acceptance_payload
        )
        mutation_identity = domain_idempotency_key(
            event_type=mutation_type, world_id=proposal_event.world_id, payload=mutation_payload
        )
        if acceptance_identity is None or mutation_identity is None:
            raise AppraisalAcceptanceError("event_identity_missing")
        events = (
            WorldEvent.from_payload(
                **common,
                event_id=acceptance_event_id,
                event_type="AcceptanceRecorded",
                causation_id=proposal_event.event_id,
                idempotency_key=acceptance_identity,
                payload=acceptance_payload,
            ),
            WorldEvent.from_payload(
                **common,
                event_id=mutation_event_id,
                event_type=mutation_type,
                causation_id=acceptance_event_id,
                idempotency_key=mutation_identity,
                payload=mutation_payload,
            ),
            WorldEvent.from_payload(
                **common,
                event_id=completion_id,
                event_type="TriggerProcessCompleted",
                causation_id=mutation_event_id,
                idempotency_key=_private_idempotency_key(
                    world_id=proposal_event.world_id, manifest_hash=manifest.manifest_hash, role="completion"
                ),
                payload=completion_payload,
            ),
        )
        if (
            events[1].payload_hash != manifest.mutation_payload_hash
            or events[2].payload_hash != manifest.completion_payload_hash
        ):
            raise AppraisalAcceptanceError("effect_hash_mismatch")
        commit_id = "commit:appraisal-acceptance:" + _digest(
            {
                "world_id": proposal_event.world_id,
                "cursor": cursor.model_dump(mode="json"),
                "manifest_hash": manifest.manifest_hash,
                "events": tuple(event.model_dump(mode="json") for event in events),
            }
        )
        return self.__batch_issuer.issue(
            world_id=proposal_event.world_id,
            expected_cursor=cursor,
            events=events,
            manifest_hash=manifest.manifest_hash,
            registry_digest=APPRAISAL_ACCEPTANCE_POLICY_DIGEST,
            commit_id=commit_id,
        )


class AppraisalAcceptanceRuntime:
    """Composition root for the isolated production Appraisal acceptance lane."""

    __slots__ = ("ledger", "_reader", "_recorder")

    def __init__(self, *, ledger: LedgerPort, batch_issuer: AcceptedLedgerBatchIssuer) -> None:
        self.ledger = ledger
        self._reader = AppraisalProposalAuthorityReader(ledger=ledger)
        self._recorder = AppraisalAtomicRecorder(
            proposal_reader=self._reader, batch_issuer=batch_issuer
        )

    @classmethod
    def in_memory(cls, *, world_id: str) -> AppraisalAcceptanceRuntime:
        issuer = AcceptedLedgerBatchIssuer()
        return cls(ledger=WorldLedger.in_memory(world_id=world_id, accepted_batch_issuer=issuer), batch_issuer=issuer)

    @classmethod
    def open(cls, *, path: Path, world_id: str) -> AppraisalAcceptanceRuntime:
        issuer = AcceptedLedgerBatchIssuer()
        return cls(
            ledger=SQLiteWorldLedger(path=path, world_id=world_id, accepted_batch_issuer=issuer),
            batch_issuer=issuer,
        )

    def close(self) -> None:
        close = getattr(self.ledger, "close", None)
        if close is not None:
            close()

    def pin_proposal(
        self, *, cursor: ProjectionCursor, proposal_id: str
    ) -> PinnedAppraisalProposalAuthorityHandle:
        return self._reader.pin(world_id=self.ledger.world_id, cursor=cursor, proposal_id=proposal_id)

    def accept(
        self,
        *,
        handle: PinnedAppraisalProposalAuthorityHandle,
        actor: str,
        source: str,
        logical_time: datetime,
        created_at: datetime,
        trace_id: str,
        correlation_id: str,
        completed_at: datetime,
    ) -> CommitResult:
        cursor = object.__getattribute__(handle, "_PinnedAppraisalProposalAuthorityHandle__cursor")
        batch = self._recorder.prepare_batch(
            handle=handle,
            actor=actor,
            source=source,
            logical_time=logical_time,
            created_at=created_at,
            trace_id=trace_id,
            correlation_id=correlation_id,
            completed_at=completed_at,
        )
        return self.ledger.commit_accepted(batch, expected_cursor=cursor)


__all__ = [
    "APPRAISAL_ACCEPTANCE_POLICY_DIGEST",
    "APPRAISAL_ACCEPTANCE_POLICY_VERSION",
    "AppraisalAcceptanceError",
    "AppraisalAcceptanceRuntime",
    "AppraisalAtomicRecorder",
    "AppraisalProposalAuthorityReader",
    "PinnedAppraisalProposalAuthorityHandle",
    "appraisal_acceptance_event_id",
    "appraisal_mutation_event_id",
]

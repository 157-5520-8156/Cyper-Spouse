"""CAS acceptance vertical for explicit, role-authored relationship commitments."""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
from pathlib import Path

from .accepted_ledger_batch import AcceptedLedgerBatchHandle, AcceptedLedgerBatchIssuer
from .event_identity import domain_idempotency_key
from .ledger import LedgerPort, WorldLedger
from .relationship_acceptance_runtime import (
    PinnedRelationshipProposalAuthorityHandle,
    RelationshipProposalAuthorityReader,
)
from .relationship_commitment_acceptance_manifest import (
    RELATIONSHIP_COMMITMENT_ACCEPTANCE_POLICY_DIGEST,
    RELATIONSHIP_COMMITMENT_ACCEPTANCE_POLICY_VERSION,
    build_relationship_commitment_acceptance_manifest,
    canonical_relationship_commitment_acceptance_value_hash,
)
from .relationship_events import (
    RELATIONSHIP_PAYLOAD_MODELS,
    RelationshipCommitmentAcceptedPayload,
)
from .schemas import CommitResult, ProjectionCursor, WorldEvent
from .sqlite_ledger import SQLiteWorldLedger


def _canonical_json(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    )


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


class RelationshipCommitmentAcceptanceError(ValueError):
    """Stable failure at the relationship commitment acceptance seam."""

    def __init__(self, code: str) -> None:
        self.code = f"relationship_commitment_acceptance.{code}"
        super().__init__(self.code)


def relationship_commitment_acceptance_event_id(
    *, world_id: str, proposal_id: str, change_id: str
) -> str:
    return "event:relationship-commitment-acceptance:" + _digest(
        {"world_id": world_id, "proposal_id": proposal_id, "change_id": change_id}
    )


def relationship_commitment_mutation_event_id(
    *, world_id: str, proposal_id: str, transition_id: str
) -> str:
    return "event:relationship-commitment-mutation:" + _digest(
        {
            "world_id": world_id,
            "proposal_id": proposal_id,
            "transition_id": transition_id,
            "event_type": "RelationshipCommitmentAccepted",
        }
    )


class RelationshipCommitmentAtomicRecorder:
    """Sole materializer for one closed commitment-and-stage mutation batch."""

    __slots__ = ("__reader", "__batch_issuer")

    def __init__(
        self,
        *,
        proposal_reader: RelationshipProposalAuthorityReader,
        batch_issuer: AcceptedLedgerBatchIssuer,
    ) -> None:
        self.__reader = proposal_reader
        self.__batch_issuer = batch_issuer

    def prepare_batch(
        self,
        *,
        handle: PinnedRelationshipProposalAuthorityHandle,
        actor: str,
        source: str,
        logical_time: datetime,
        created_at: datetime,
        trace_id: str,
        correlation_id: str,
    ) -> AcceptedLedgerBatchHandle:
        if not self.__reader.owns(handle):
            raise RelationshipCommitmentAcceptanceError("proposal_handle_untrusted")
        proposal = object.__getattribute__(
            handle, "_PinnedRelationshipProposalAuthorityHandle__proposal"
        )
        proposal_event = object.__getattribute__(
            handle, "_PinnedRelationshipProposalAuthorityHandle__proposal_event"
        )
        cursor = object.__getattribute__(
            handle, "_PinnedRelationshipProposalAuthorityHandle__cursor"
        )
        if proposal.evaluated_world_revision != cursor.world_revision:
            raise RelationshipCommitmentAcceptanceError("proposal_stale")
        if proposal.transition_kind != "commitment":
            raise RelationshipCommitmentAcceptanceError("transition_not_acceptable")
        mutation_type = proposal.proposed_mutation.event_type
        if mutation_type != "RelationshipCommitmentAccepted":
            raise RelationshipCommitmentAcceptanceError("mechanical_mutation_not_acceptable")
        mutation_model = RELATIONSHIP_PAYLOAD_MODELS[mutation_type]
        mutation = mutation_model.model_validate_json(proposal.proposed_mutation.payload_json)
        if not isinstance(mutation, RelationshipCommitmentAcceptedPayload):
            raise RelationshipCommitmentAcceptanceError("mechanical_mutation_not_acceptable")
        if (
            mutation.proposal_id != proposal.proposal_id
            or mutation.change_id != proposal.change_id
            or mutation.transition_id != proposal.transition_id
            or mutation.evaluated_world_revision != cursor.world_revision
            or mutation.expected_entity_revision != proposal.expected_entity_revision
            or mutation.accepted_change_hash != proposal.proposed_change_hash
            or mutation.evidence_refs != proposal.evidence_refs
            or mutation.policy_refs != proposal.policy_refs
        ):
            raise RelationshipCommitmentAcceptanceError("proposal_mutation_mismatch")
        mutation_event_id = relationship_commitment_mutation_event_id(
            world_id=proposal_event.world_id,
            proposal_id=proposal.proposal_id,
            transition_id=mutation.transition_id,
        )
        if mutation.commitment.origin.accepted_event_ref != mutation_event_id:
            raise RelationshipCommitmentAcceptanceError("mutation_event_identity_not_bound")
        mutation_payload = json.loads(proposal.proposed_mutation.payload_json)
        manifest = build_relationship_commitment_acceptance_manifest(
            acceptance_id=mutation.acceptance_id,
            proposal_id=proposal.proposal_id,
            proposal_event_ref=proposal_event.event_id,
            proposal_event_payload_hash=proposal_event.payload_hash,
            evaluated_world_revision=cursor.world_revision,
            accepted_change_id=mutation.change_id,
            accepted_change_hash=mutation.accepted_change_hash,
            mutation_event_id=mutation_event_id,
            mutation_event_type=mutation_type,
            mutation_payload_hash=canonical_relationship_commitment_acceptance_value_hash(
                mutation_payload
            ),
            policy_digest=RELATIONSHIP_COMMITMENT_ACCEPTANCE_POLICY_DIGEST,
        )
        acceptance_event_id = relationship_commitment_acceptance_event_id(
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
            event_type="AcceptanceRecorded",
            world_id=proposal_event.world_id,
            payload=acceptance_payload,
        )
        mutation_identity = domain_idempotency_key(
            event_type=mutation_type,
            world_id=proposal_event.world_id,
            payload=mutation_payload,
        )
        if acceptance_identity is None or mutation_identity is None:
            raise RelationshipCommitmentAcceptanceError("event_identity_missing")
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
        )
        if events[1].payload_hash != manifest.mutation_payload_hash:
            raise RelationshipCommitmentAcceptanceError("effect_hash_mismatch")
        commit_id = "commit:relationship-commitment-acceptance:" + _digest(
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
            registry_digest=RELATIONSHIP_COMMITMENT_ACCEPTANCE_POLICY_DIGEST,
            commit_id=commit_id,
        )


class RelationshipCommitmentAcceptanceRuntime:
    """Composition root for the isolated relationship commitment lane."""

    __slots__ = ("ledger", "_reader", "_recorder")

    def __init__(self, *, ledger: LedgerPort, batch_issuer: AcceptedLedgerBatchIssuer) -> None:
        self.ledger = ledger
        self._reader = RelationshipProposalAuthorityReader(ledger=ledger)
        self._recorder = RelationshipCommitmentAtomicRecorder(
            proposal_reader=self._reader,
            batch_issuer=batch_issuer,
        )

    @classmethod
    def in_memory(cls, *, world_id: str) -> RelationshipCommitmentAcceptanceRuntime:
        issuer = AcceptedLedgerBatchIssuer()
        return cls(
            ledger=WorldLedger.in_memory(world_id=world_id, accepted_batch_issuer=issuer),
            batch_issuer=issuer,
        )

    @classmethod
    def open(
        cls, *, path: Path, world_id: str
    ) -> RelationshipCommitmentAcceptanceRuntime:
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
    ) -> PinnedRelationshipProposalAuthorityHandle:
        return self._reader.pin(
            world_id=self.ledger.world_id,
            cursor=cursor,
            proposal_id=proposal_id,
        )

    def accept(
        self,
        *,
        handle: PinnedRelationshipProposalAuthorityHandle,
        actor: str,
        source: str,
        logical_time: datetime,
        created_at: datetime,
        trace_id: str,
        correlation_id: str,
    ) -> CommitResult:
        cursor = object.__getattribute__(
            handle, "_PinnedRelationshipProposalAuthorityHandle__cursor"
        )
        batch = self._recorder.prepare_batch(
            handle=handle,
            actor=actor,
            source=source,
            logical_time=logical_time,
            created_at=created_at,
            trace_id=trace_id,
            correlation_id=correlation_id,
        )
        return self.ledger.commit_accepted(batch, expected_cursor=cursor)

    def accept_runtime_owned(
        self,
        *,
        handle: PinnedRelationshipProposalAuthorityHandle,
        actor: str,
        source: str,
    ) -> CommitResult:
        proposal_event = object.__getattribute__(
            handle, "_PinnedRelationshipProposalAuthorityHandle__proposal_event"
        )
        return self.accept(
            handle=handle,
            actor=actor,
            source=source,
            logical_time=proposal_event.logical_time,
            created_at=proposal_event.created_at,
            trace_id=proposal_event.trace_id,
            correlation_id=proposal_event.correlation_id,
        )


__all__ = [
    "RELATIONSHIP_COMMITMENT_ACCEPTANCE_POLICY_DIGEST",
    "RELATIONSHIP_COMMITMENT_ACCEPTANCE_POLICY_VERSION",
    "RelationshipCommitmentAcceptanceError",
    "RelationshipCommitmentAcceptanceRuntime",
    "RelationshipCommitmentAtomicRecorder",
    "relationship_commitment_acceptance_event_id",
    "relationship_commitment_mutation_event_id",
]

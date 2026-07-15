"""Production acceptance vertical for persisted, typed Affect proposals.

This narrow Module consumes only a reader-issued handle pinned to one ledger
cursor.  It owns the accepted manifest, event identities and opaque atomic
batch; it is not a general event-writing adapter.
"""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
from pathlib import Path

from .accepted_ledger_batch import AcceptedLedgerBatchHandle, AcceptedLedgerBatchIssuer
from .affect_acceptance_manifest import (
    build_affect_acceptance_manifest,
    canonical_affect_acceptance_value_hash,
)
from .affect_events import AFFECT_PAYLOAD_MODELS, AffectAuthorizedMutationPayload
from .event_identity import domain_idempotency_key
from .ledger import LedgerPort, WorldLedger
from .schemas import CommitResult, ProjectionCursor, WorldEvent
from .sqlite_ledger import SQLiteWorldLedger


AFFECT_ACCEPTANCE_POLICY_VERSION = "affect-acceptance-policy.1"


def _canonical_json(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    )


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


AFFECT_ACCEPTANCE_POLICY_DIGEST = _digest(
    {
        "contract": AFFECT_ACCEPTANCE_POLICY_VERSION,
        "mutation_event_types": (
            "AffectBaselineAdjusted",
            "AffectEpisodeOpened",
            "AffectEpisodeResolved",
            "AffectEpisodeSuperseded",
            "AffectEpisodeUpdated",
        ),
        "requires_trigger_completion": False,
    }
)


class AffectAcceptanceError(ValueError):
    """Stable failure at the Affect acceptance seam."""

    def __init__(self, code: str) -> None:
        self.code = f"affect_acceptance.{code}"
        super().__init__(self.code)


class PinnedAffectProposalAuthorityHandle:
    """Reader-owned proof of one current, persisted Affect proposal."""

    __slots__ = ("__proposal", "__proposal_event", "__cursor", "__issuer")

    def __init__(
        self,
        *,
        proposal: object,
        proposal_event: WorldEvent,
        cursor: ProjectionCursor,
        issuer: object,
    ) -> None:
        self.__proposal = proposal
        self.__proposal_event = proposal_event
        self.__cursor = cursor
        self.__issuer = issuer

    def issued_by(self, issuer: object) -> bool:
        return self.__issuer is issuer

    def __reduce__(self) -> object:
        raise TypeError("pinned Affect proposal handles cannot be serialized")

    def __copy__(self) -> object:
        raise TypeError("pinned Affect proposal handles cannot be copied")

    def __deepcopy__(self, memo: object) -> object:
        del memo
        raise TypeError("pinned Affect proposal handles cannot be copied")


class AffectProposalAuthorityReader:
    """Deep read seam: pin and verify one typed Affect proposal at one cursor."""

    __slots__ = ("__ledger", "__issuer")

    def __init__(self, *, ledger: LedgerPort) -> None:
        self.__ledger = ledger
        self.__issuer = object()

    def pin(
        self, *, world_id: str, cursor: ProjectionCursor, proposal_id: str
    ) -> PinnedAffectProposalAuthorityHandle:
        if world_id != self.__ledger.world_id:
            raise AffectAcceptanceError("authority_world_mismatch")
        projection = self.__ledger.project_at(cursor)
        proposal = next(
            (item for item in projection.affect_proposals if item.proposal_id == proposal_id), None
        )
        if proposal is None:
            raise AffectAcceptanceError("proposal_not_persisted")
        proposal_event = _find_affect_proposal_event(self.__ledger, proposal_id, cursor=cursor)
        if proposal_event is None or proposal_event.event_type != "ProposalRecorded":
            raise AffectAcceptanceError("proposal_event_missing")
        if (
            proposal.recorded_event_ref != proposal_event.event_id
            or proposal.recorded_event_payload_hash != proposal_event.payload_hash
            or proposal_event.payload()
            != proposal.model_dump(
                mode="json",
                exclude={"recorded_event_ref", "recorded_event_payload_hash"},
                exclude_none=True,
            )
        ):
            raise AffectAcceptanceError("proposal_event_mismatch")
        return PinnedAffectProposalAuthorityHandle(
            proposal=proposal,
            proposal_event=proposal_event,
            cursor=cursor,
            issuer=self.__issuer,
        )

    def owns(self, handle: PinnedAffectProposalAuthorityHandle) -> bool:
        return type(handle) is PinnedAffectProposalAuthorityHandle and handle.issued_by(
            self.__issuer
        )


def _find_affect_proposal_event(
    ledger: LedgerPort, proposal_id: str, *, cursor: ProjectionCursor
) -> WorldEvent | None:
    finder = getattr(ledger, "_find_affect_proposal_event", None)
    if finder is None:
        raise AffectAcceptanceError("proposal_lookup_unsupported")
    event = finder(proposal_id=proposal_id, cursor=cursor)
    if event is not None and type(event) is not WorldEvent:
        raise AffectAcceptanceError("proposal_event_invalid")
    return event


def affect_acceptance_event_id(*, world_id: str, proposal_id: str, change_id: str) -> str:
    return "event:affect-acceptance:" + _digest(
        {"world_id": world_id, "proposal_id": proposal_id, "change_id": change_id}
    )


def affect_mutation_event_id(
    *, world_id: str, proposal_id: str, transition_id: str, event_type: str
) -> str:
    return "event:affect-mutation:" + _digest(
        {
            "world_id": world_id,
            "proposal_id": proposal_id,
            "transition_id": transition_id,
            "event_type": event_type,
        }
    )


def _private_idempotency_key(*, world_id: str, manifest_hash: str, role: str) -> str:
    return f"world-v2:affect-acceptance:{role}:" + _digest(
        {"world_id": world_id, "manifest_hash": manifest_hash, "role": role}
    )


class AffectAtomicRecorder:
    """Sole event materializer for the Affect accepted-manifest lane."""

    __slots__ = ("__reader", "__batch_issuer")

    def __init__(
        self, *, proposal_reader: AffectProposalAuthorityReader, batch_issuer: AcceptedLedgerBatchIssuer
    ) -> None:
        self.__reader = proposal_reader
        self.__batch_issuer = batch_issuer

    def prepare_batch(
        self,
        *,
        handle: PinnedAffectProposalAuthorityHandle,
        actor: str,
        source: str,
        logical_time: datetime,
        created_at: datetime,
        trace_id: str,
        correlation_id: str,
    ) -> AcceptedLedgerBatchHandle:
        if not self.__reader.owns(handle):
            raise AffectAcceptanceError("proposal_handle_untrusted")
        proposal = object.__getattribute__(handle, "_PinnedAffectProposalAuthorityHandle__proposal")
        proposal_event = object.__getattribute__(
            handle, "_PinnedAffectProposalAuthorityHandle__proposal_event"
        )
        cursor = object.__getattribute__(handle, "_PinnedAffectProposalAuthorityHandle__cursor")
        if proposal.evaluated_world_revision != cursor.world_revision:
            raise AffectAcceptanceError("proposal_stale")
        mutation_type = proposal.proposed_mutation.event_type
        mutation_model = AFFECT_PAYLOAD_MODELS[mutation_type]
        mutation = mutation_model.model_validate_json(proposal.proposed_mutation.payload_json)
        if not isinstance(mutation, AffectAuthorizedMutationPayload):
            raise AffectAcceptanceError("mechanical_mutation_not_acceptable")
        if (
            mutation.proposal_id != proposal.proposal_id
            or mutation.change_id != proposal.change_id
            or mutation.transition_id != proposal.transition_id
            or mutation.evaluated_world_revision != cursor.world_revision
            or mutation.expected_entity_revision != proposal.expected_entity_revision
            or mutation.accepted_change_hash != proposal.proposed_change_hash
            or mutation.evidence_refs != proposal.evidence_refs
            or mutation.appraisal_refs != proposal.appraisal_refs
            or mutation.policy_refs != proposal.policy_refs
        ):
            raise AffectAcceptanceError("proposal_mutation_mismatch")
        mutation_event_id = affect_mutation_event_id(
            world_id=proposal_event.world_id,
            proposal_id=proposal.proposal_id,
            transition_id=mutation.transition_id,
            event_type=mutation_type,
        )
        origin = getattr(getattr(mutation, "episode", None), "origin", None)
        if origin is None:
            origin = getattr(getattr(mutation, "successor", None), "origin", None)
        if origin is not None and origin.accepted_event_ref != mutation_event_id:
            raise AffectAcceptanceError("mutation_event_identity_not_bound")
        mutation_payload = json.loads(proposal.proposed_mutation.payload_json)
        manifest = build_affect_acceptance_manifest(
            acceptance_id=mutation.acceptance_id,
            proposal_id=proposal.proposal_id,
            proposal_event_ref=proposal_event.event_id,
            proposal_event_payload_hash=proposal_event.payload_hash,
            evaluated_world_revision=cursor.world_revision,
            accepted_change_id=mutation.change_id,
            accepted_change_hash=mutation.accepted_change_hash,
            mutation_event_id=mutation_event_id,
            mutation_event_type=mutation_type,
            mutation_payload_hash=canonical_affect_acceptance_value_hash(mutation_payload),
            policy_digest=AFFECT_ACCEPTANCE_POLICY_DIGEST,
        )
        acceptance_event_id = affect_acceptance_event_id(
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
            raise AffectAcceptanceError("event_identity_missing")
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
            raise AffectAcceptanceError("effect_hash_mismatch")
        commit_id = "commit:affect-acceptance:" + _digest(
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
            registry_digest=AFFECT_ACCEPTANCE_POLICY_DIGEST,
            commit_id=commit_id,
        )


class AffectAcceptanceRuntime:
    """Composition root for the isolated production Affect acceptance lane."""

    __slots__ = ("ledger", "_reader", "_recorder")

    def __init__(self, *, ledger: LedgerPort, batch_issuer: AcceptedLedgerBatchIssuer) -> None:
        self.ledger = ledger
        self._reader = AffectProposalAuthorityReader(ledger=ledger)
        self._recorder = AffectAtomicRecorder(
            proposal_reader=self._reader, batch_issuer=batch_issuer
        )

    @classmethod
    def in_memory(cls, *, world_id: str) -> AffectAcceptanceRuntime:
        issuer = AcceptedLedgerBatchIssuer()
        return cls(
            ledger=WorldLedger.in_memory(world_id=world_id, accepted_batch_issuer=issuer),
            batch_issuer=issuer,
        )

    @classmethod
    def open(cls, *, path: Path, world_id: str) -> AffectAcceptanceRuntime:
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
    ) -> PinnedAffectProposalAuthorityHandle:
        return self._reader.pin(world_id=self.ledger.world_id, cursor=cursor, proposal_id=proposal_id)

    def accept(
        self,
        *,
        handle: PinnedAffectProposalAuthorityHandle,
        actor: str,
        source: str,
        logical_time: datetime,
        created_at: datetime,
        trace_id: str,
        correlation_id: str,
    ) -> CommitResult:
        cursor = object.__getattribute__(handle, "_PinnedAffectProposalAuthorityHandle__cursor")
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
        self, *, handle: PinnedAffectProposalAuthorityHandle, actor: str, source: str
    ) -> CommitResult:
        proposal_event = object.__getattribute__(
            handle, "_PinnedAffectProposalAuthorityHandle__proposal_event"
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
    "AFFECT_ACCEPTANCE_POLICY_DIGEST",
    "AFFECT_ACCEPTANCE_POLICY_VERSION",
    "AffectAcceptanceError",
    "AffectAcceptanceRuntime",
    "AffectAtomicRecorder",
    "AffectProposalAuthorityReader",
    "PinnedAffectProposalAuthorityHandle",
    "affect_acceptance_event_id",
    "affect_mutation_event_id",
]

"""Durable, cursor-pinned audit authority for Fact commit proposals v2.

This is deliberately separate from the legacy ``ProposalRecorded`` audit
contract.  The v2 Fact envelope has a different schema registry and its
canonical bytes require the proposal's world identity to validate deterministic
Fact IDs.  Treating it as a variant of the v1 proposal DTO would allow a
consumer to parse the wrong authority contract.

The module can build a deliberation-audit event and can later read that exact
event at a complete ledger cursor.  It neither compiles a domain payload nor
materializes an accepted event.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING
from weakref import WeakKeyDictionary

from pydantic import Field, model_validator

from .event_identity import domain_idempotency_key
from .proposal_envelope_v2 import (
    FactCommitProposalEnvelopeV2,
    canonical_fact_commit_proposal_v2_hash,
    canonical_fact_commit_proposal_v2_json,
    validate_fact_commit_proposal_v2,
)
from .schema_core import FrozenModel
from .schemas import ProjectionCursor, WorldEvent

if TYPE_CHECKING:
    from .ledger import LedgerPort


FACT_COMMIT_PROPOSAL_AUDIT_CONTRACT_V2 = "fact-commit-proposal-audit.2"
FACT_COMMIT_PROPOSAL_RECORDED_EVENT_V2 = "FactCommitProposalRecorded"


class FactCommitProposalAuditErrorV2(ValueError):
    """Stable failure while creating or pinning Fact-v2 proposal authority."""


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def fact_commit_proposal_audit_event_id_v2(*, world_id: str, proposal_id: str) -> str:
    """Derive the only event identity permitted for one durable Fact-v2 audit."""

    if type(world_id) is not str or not world_id or type(proposal_id) is not str or not proposal_id:
        raise FactCommitProposalAuditErrorV2("Fact proposal audit identity is invalid")
    digest = hashlib.sha256(
        _canonical_json(
            {
                "contract": FACT_COMMIT_PROPOSAL_AUDIT_CONTRACT_V2,
                "world_id": world_id,
                "proposal_id": proposal_id,
            }
        ).encode("utf-8")
    ).hexdigest()
    return f"event:fact-commit-proposal:{digest}"


class FactCommitProposalRecordedPayloadV2(FrozenModel):
    """Closed audit bytes for one normalized ``FactCommitProposalEnvelopeV2``."""

    audit_contract: str = Field(default=FACT_COMMIT_PROPOSAL_AUDIT_CONTRACT_V2)
    proposal_schema_registry: str = Field(default="world-v2-proposals.2")
    proposal_world_id: str = Field(min_length=1, max_length=512)
    proposal_id: str = Field(min_length=1, max_length=256)
    evaluated_world_revision: int = Field(ge=0)
    proposal_json: str = Field(min_length=2, max_length=262_144)
    proposal_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def exact_v2_proposal_bytes_are_bound(self) -> FactCommitProposalRecordedPayloadV2:
        if self.audit_contract != FACT_COMMIT_PROPOSAL_AUDIT_CONTRACT_V2:
            raise ValueError("Fact proposal audit contract is not supported")
        if self.proposal_schema_registry != "world-v2-proposals.2":
            raise ValueError("Fact proposal audit registry is not supported")
        if len(self.proposal_json.encode("utf-8")) > 262_144:
            raise ValueError("Fact proposal audit exceeds byte limit")
        try:
            raw_proposal = json.loads(self.proposal_json)
            if type(raw_proposal) is not dict:
                raise ValueError("Fact proposal audit must contain an object")
            proposal = validate_fact_commit_proposal_v2(
                raw_proposal, world_id=self.proposal_world_id
            )
        except Exception as exc:
            raise ValueError("Fact proposal audit contains an invalid normalized proposal") from exc
        canonical = canonical_fact_commit_proposal_v2_json(
            proposal, world_id=self.proposal_world_id
        )
        if self.proposal_json != canonical:
            raise ValueError("Fact proposal audit bytes are not canonical")
        if self.proposal_hash != canonical_fact_commit_proposal_v2_hash(
            proposal, world_id=self.proposal_world_id
        ):
            raise ValueError("Fact proposal audit hash is not canonical")
        if (
            proposal.proposal_id != self.proposal_id
            or proposal.evaluated_world_revision != self.evaluated_world_revision
        ):
            raise ValueError("Fact proposal audit lineage does not match its envelope")
        return self


class FactCommitProposalAuditProjectionV2(FactCommitProposalRecordedPayloadV2):
    """The durable audit value plus its authenticated ledger location."""

    event_ref: str = Field(min_length=1, max_length=512)
    event_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    committed_cursor: ProjectionCursor


class PinnedFactCommitProposalAuthorityHandleV2:
    """Opaque reader-issued capability for one Fact-v2 proposal at one cursor."""

    __slots__ = ("__weakref__",)

    def __reduce__(self) -> object:
        raise TypeError("pinned Fact proposal authority handles cannot be serialized")

    def __copy__(self) -> object:
        raise TypeError("pinned Fact proposal authority handles cannot be copied")

    def __deepcopy__(self, memo: object) -> object:
        del memo
        raise TypeError("pinned Fact proposal authority handles cannot be copied")


@dataclass(frozen=True, slots=True)
class _PinnedFactCommitProposalAuthorityV2:
    audit: FactCommitProposalAuditProjectionV2
    proposal: FactCommitProposalEnvelopeV2
    cursor: ProjectionCursor
    world_id: str


class FactCommitProposalAuthorityReaderV2:
    """Issue Fact-v2 authority only from one exact committed audit event.

    The input cursor is first resolved through ``LedgerPort.project_at``.  This
    rejects fabricated mixed revision/sequence tuples before the event location
    is considered.  The reader then binds the event's commit cursor to that
    complete prefix and stores all state behind an issuer-owned weak registry.
    """

    __slots__ = ("__ledger", "__handles")

    def __init__(self, *, ledger: LedgerPort) -> None:
        self.__ledger = ledger
        self.__handles: WeakKeyDictionary[
            PinnedFactCommitProposalAuthorityHandleV2, _PinnedFactCommitProposalAuthorityV2
        ] = WeakKeyDictionary()

    def pin(
        self,
        *,
        world_id: str,
        cursor: ProjectionCursor,
        proposal_id: str,
    ) -> PinnedFactCommitProposalAuthorityHandleV2:
        if type(world_id) is not str or world_id != self.__ledger.world_id:
            raise FactCommitProposalAuditErrorV2("Fact proposal reader belongs to another world")
        if type(cursor) is not ProjectionCursor:
            raise FactCommitProposalAuditErrorV2("Fact proposal cursor must use its exact contract")
        if type(proposal_id) is not str or not proposal_id:
            raise FactCommitProposalAuditErrorV2("Fact proposal id is invalid")
        try:
            resolved = self.__ledger.project_at(cursor)
        except Exception as exc:
            raise FactCommitProposalAuditErrorV2("Fact proposal cursor is not a ledger prefix") from exc
        if (
            resolved.world_revision != cursor.world_revision
            or resolved.deliberation_revision != cursor.deliberation_revision
            or resolved.ledger_sequence != cursor.ledger_sequence
        ):
            raise FactCommitProposalAuditErrorV2("Fact proposal cursor is not complete")
        event_id = fact_commit_proposal_audit_event_id_v2(
            world_id=world_id, proposal_id=proposal_id
        )
        located = self.__ledger.lookup_event_commit(event_id)
        if located is None:
            raise FactCommitProposalAuditErrorV2("Fact proposal audit event is missing")
        event, commit = located
        committed_cursor = ProjectionCursor(
            world_revision=commit.world_revision,
            deliberation_revision=commit.deliberation_revision,
            ledger_sequence=commit.ledger_sequence,
        )
        if (
            event.world_id != world_id
            or event.event_type != FACT_COMMIT_PROPOSAL_RECORDED_EVENT_V2
            or committed_cursor.ledger_sequence > cursor.ledger_sequence
            or committed_cursor.world_revision > cursor.world_revision
            or committed_cursor.deliberation_revision > cursor.deliberation_revision
        ):
            raise FactCommitProposalAuditErrorV2("Fact proposal audit is outside the pinned cursor")
        try:
            payload = FactCommitProposalRecordedPayloadV2.model_validate(event.payload(), strict=True)
        except Exception as exc:
            raise FactCommitProposalAuditErrorV2("Fact proposal audit payload is invalid") from exc
        if (
            payload.proposal_world_id != world_id
            or payload.proposal_id != proposal_id
            or payload.evaluated_world_revision != commit.world_revision
        ):
            raise FactCommitProposalAuditErrorV2("Fact proposal audit does not bind its ledger event")
        try:
            raw_proposal = json.loads(payload.proposal_json)
            if type(raw_proposal) is not dict:
                raise ValueError("Fact proposal audit must contain an object")
            proposal = validate_fact_commit_proposal_v2(raw_proposal, world_id=world_id)
        except Exception as exc:
            raise FactCommitProposalAuditErrorV2("Fact proposal audit proposal is invalid") from exc
        audit = FactCommitProposalAuditProjectionV2(
            **payload.model_dump(mode="json"),
            event_ref=event.event_id,
            event_payload_hash=event.payload_hash,
            committed_cursor=committed_cursor,
        )
        handle = PinnedFactCommitProposalAuthorityHandleV2()
        self.__handles[handle] = _PinnedFactCommitProposalAuthorityV2(
            audit=audit, proposal=proposal, cursor=cursor, world_id=world_id
        )
        return handle

    def owns(self, value: object) -> bool:
        return type(value) is PinnedFactCommitProposalAuthorityHandleV2 and value in self.__handles

    def audit(
        self, *, handle: PinnedFactCommitProposalAuthorityHandleV2
    ) -> FactCommitProposalAuditProjectionV2:
        return self.__material(handle).audit

    def proposal(
        self, *, handle: PinnedFactCommitProposalAuthorityHandleV2
    ) -> FactCommitProposalEnvelopeV2:
        return self.__material(handle).proposal

    def cursor(self, *, handle: PinnedFactCommitProposalAuthorityHandleV2) -> ProjectionCursor:
        return self.__material(handle).cursor

    def __material(
        self, handle: PinnedFactCommitProposalAuthorityHandleV2
    ) -> _PinnedFactCommitProposalAuthorityV2:
        if type(handle) is not PinnedFactCommitProposalAuthorityHandleV2:
            raise FactCommitProposalAuditErrorV2("Fact proposal handle belongs to another reader")
        material = self.__handles.get(handle)
        if material is None:
            raise FactCommitProposalAuditErrorV2("Fact proposal handle belongs to another reader")
        return material


def build_fact_commit_proposal_recorded_event_v2(
    *,
    proposal: FactCommitProposalEnvelopeV2,
    world_id: str,
    logical_time: datetime,
    created_at: datetime,
    actor: str,
    source: str,
    trace_id: str,
    causation_id: str,
    correlation_id: str,
) -> WorldEvent:
    """Build the deliberation-audit event; callers still commit through LedgerPort."""

    strict = validate_fact_commit_proposal_v2(proposal, world_id=world_id)
    payload = FactCommitProposalRecordedPayloadV2(
        proposal_world_id=world_id,
        proposal_id=strict.proposal_id,
        evaluated_world_revision=strict.evaluated_world_revision,
        proposal_json=canonical_fact_commit_proposal_v2_json(strict, world_id=world_id),
        proposal_hash=canonical_fact_commit_proposal_v2_hash(strict, world_id=world_id),
    )
    payload_data = payload.model_dump(mode="json")
    return WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id=fact_commit_proposal_audit_event_id_v2(
            world_id=world_id, proposal_id=strict.proposal_id
        ),
        world_id=world_id,
        event_type=FACT_COMMIT_PROPOSAL_RECORDED_EVENT_V2,
        logical_time=logical_time,
        created_at=created_at,
        actor=actor,
        source=source,
        trace_id=trace_id,
        causation_id=causation_id,
        correlation_id=correlation_id,
        idempotency_key=(
            domain_idempotency_key(
                event_type=FACT_COMMIT_PROPOSAL_RECORDED_EVENT_V2,
                world_id=world_id,
                payload=payload_data,
            )
            or "fact-proposal-audit:"
            + hashlib.sha256(
                _canonical_json(
                    {
                        "world_id": world_id,
                        "proposal_id": strict.proposal_id,
                        "proposal_hash": payload.proposal_hash,
                    }
                ).encode("utf-8")
            ).hexdigest()
        ),
        payload=payload_data,
    )


__all__ = [
    "FACT_COMMIT_PROPOSAL_AUDIT_CONTRACT_V2",
    "FACT_COMMIT_PROPOSAL_RECORDED_EVENT_V2",
    "FactCommitProposalAuditErrorV2",
    "FactCommitProposalAuditProjectionV2",
    "FactCommitProposalAuthorityReaderV2",
    "FactCommitProposalRecordedPayloadV2",
    "PinnedFactCommitProposalAuthorityHandleV2",
    "build_fact_commit_proposal_recorded_event_v2",
    "fact_commit_proposal_audit_event_id_v2",
]

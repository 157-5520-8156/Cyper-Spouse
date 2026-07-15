"""Audit-pinned authority for a Fact-v2 accepted-manifest envelope.

An acceptance envelope has intentionally different authority from an inert
candidate DTO.  In particular, its causation is an exact durable proposal-audit
event, and its world/cursor must equal the cursor pinned by the proposal
reader.  The resulting handle is the only value a future production Fact plan
may consume.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from weakref import WeakKeyDictionary

from pydantic import Field, model_validator

from .fact_proposal_audit_v2 import (
    FactCommitProposalAuditProjectionV2,
    FactCommitProposalAuthorityReaderV2,
    PinnedFactCommitProposalAuthorityHandleV2,
)
from .schema_core import FrozenModel
from .schemas import ProjectionCursor


class FactV2AcceptanceEnvelopeAuthorityError(ValueError):
    """Stable failure at the trusted Fact acceptance-envelope boundary."""


class FactV2AcceptanceEnvelopeRequestV2(FrozenModel):
    """Untrusted request data that an installed envelope issuer must bind."""

    acceptance_id: str = Field(min_length=1, max_length=256)
    acceptance_event_id: str = Field(min_length=1, max_length=512)
    acceptance_causation_id: str = Field(min_length=1, max_length=512)
    cursor: ProjectionCursor
    world_id: str = Field(min_length=1, max_length=512)
    logical_time: datetime
    created_at: datetime
    actor: str = Field(min_length=1, max_length=512)
    source: str = Field(min_length=1, max_length=512)
    trace_id: str = Field(min_length=1, max_length=512)
    correlation_id: str = Field(min_length=1, max_length=512)

    @model_validator(mode="after")
    def logical_time_is_not_later_than_creation(self) -> FactV2AcceptanceEnvelopeRequestV2:
        if self.logical_time > self.created_at:
            raise ValueError("acceptance logical time cannot be later than creation")
        return self


class FactV2AcceptanceEnvelopeAuthorityV2(FactV2AcceptanceEnvelopeRequestV2):
    """Inspectable, audit-bound envelope value; never a write capability."""

    proposal_audit_event_ref: str = Field(min_length=1, max_length=512)
    proposal_audit_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    proposal_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class FactV2AcceptanceEnvelopeAuthorityHandle:
    """Opaque issuer-owned reference to one accepted-envelope authority."""

    __slots__ = ("__weakref__",)

    def __reduce__(self) -> object:
        raise TypeError("Fact v2 acceptance envelope handles cannot be serialized")

    def __copy__(self) -> object:
        raise TypeError("Fact v2 acceptance envelope handles cannot be copied")

    def __deepcopy__(self, memo: object) -> object:
        del memo
        raise TypeError("Fact v2 acceptance envelope handles cannot be copied")


@dataclass(frozen=True, slots=True)
class _EnvelopeMaterial:
    envelope: FactV2AcceptanceEnvelopeAuthorityV2
    audit: FactCommitProposalAuditProjectionV2


class FactV2AcceptanceEnvelopeAuthorityIssuer:
    """Issue envelope authority only from one exact proposal-audit pin."""

    __slots__ = ("__handles", "__issued_acceptance_ids")

    def __init__(self) -> None:
        self.__handles: WeakKeyDictionary[
            FactV2AcceptanceEnvelopeAuthorityHandle, _EnvelopeMaterial
        ] = WeakKeyDictionary()
        self.__issued_acceptance_ids: set[str] = set()

    def issue(
        self,
        *,
        proposal_reader: FactCommitProposalAuthorityReaderV2,
        proposal_handle: PinnedFactCommitProposalAuthorityHandleV2,
        request: FactV2AcceptanceEnvelopeRequestV2,
    ) -> FactV2AcceptanceEnvelopeAuthorityHandle:
        if type(proposal_reader) is not FactCommitProposalAuthorityReaderV2:
            raise FactV2AcceptanceEnvelopeAuthorityError(
                "Fact acceptance envelope requires its exact proposal reader"
            )
        if type(request) is not FactV2AcceptanceEnvelopeRequestV2:
            raise FactV2AcceptanceEnvelopeAuthorityError(
                "Fact acceptance envelope request must use its exact contract"
            )
        try:
            cursor = proposal_reader.cursor(handle=proposal_handle)
            audit = proposal_reader.audit(handle=proposal_handle)
        except ValueError as exc:
            raise FactV2AcceptanceEnvelopeAuthorityError(
                "Fact acceptance envelope proposal authority is invalid"
            ) from exc
        if (
            request.world_id != audit.proposal_world_id
            or request.cursor != cursor
            or request.acceptance_causation_id != audit.event_ref
        ):
            raise FactV2AcceptanceEnvelopeAuthorityError(
                "Fact acceptance envelope does not bind its proposal audit authority"
            )
        if request.acceptance_id in self.__issued_acceptance_ids:
            raise FactV2AcceptanceEnvelopeAuthorityError(
                "Fact acceptance identity has already been issued"
            )
        envelope = FactV2AcceptanceEnvelopeAuthorityV2(
            **request.model_dump(mode="python"),
            proposal_audit_event_ref=audit.event_ref,
            proposal_audit_payload_hash=audit.event_payload_hash,
            proposal_hash=audit.proposal_hash,
        )
        handle = FactV2AcceptanceEnvelopeAuthorityHandle()
        self.__handles[handle] = _EnvelopeMaterial(envelope=envelope, audit=audit)
        self.__issued_acceptance_ids.add(request.acceptance_id)
        return handle

    def owns(self, value: object) -> bool:
        return type(value) is FactV2AcceptanceEnvelopeAuthorityHandle and value in self.__handles

    def envelope(
        self, *, handle: FactV2AcceptanceEnvelopeAuthorityHandle
    ) -> FactV2AcceptanceEnvelopeAuthorityV2:
        return self.__material(handle).envelope.model_copy(deep=True)

    def audit(
        self, *, handle: FactV2AcceptanceEnvelopeAuthorityHandle
    ) -> FactCommitProposalAuditProjectionV2:
        return self.__material(handle).audit.model_copy(deep=True)

    def __material(self, handle: FactV2AcceptanceEnvelopeAuthorityHandle) -> _EnvelopeMaterial:
        if type(handle) is not FactV2AcceptanceEnvelopeAuthorityHandle:
            raise FactV2AcceptanceEnvelopeAuthorityError(
                "Fact acceptance envelope handle belongs to another issuer"
            )
        material = self.__handles.get(handle)
        if material is None:
            raise FactV2AcceptanceEnvelopeAuthorityError(
                "Fact acceptance envelope handle belongs to another issuer"
            )
        return material


__all__ = [
    "FactV2AcceptanceEnvelopeAuthorityError",
    "FactV2AcceptanceEnvelopeAuthorityHandle",
    "FactV2AcceptanceEnvelopeAuthorityIssuer",
    "FactV2AcceptanceEnvelopeAuthorityV2",
    "FactV2AcceptanceEnvelopeRequestV2",
]

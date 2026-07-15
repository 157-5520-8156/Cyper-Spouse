"""Opaque production-plan issuance for the first Fact-v2 accepted effect.

This is deliberately separate from the inert candidate-manifest builder.  A
plan binds one issuer-owned acceptance envelope, proposal-audit pin, sealed
preparation capability and proof-backed source capability.  It validates the
sealed compile and reverse-verify path both on issuance and revalidation, but
does not construct ``WorldEvent`` values or write a ledger.
"""

from __future__ import annotations

from dataclasses import dataclass
from weakref import WeakKeyDictionary

from .accepted_effect_contracts import DurableEffectCompilerAuthorityV1
from .fact_accepted_contracts import FactCommitMaterializedPayloadV2
from .fact_proof_backed_evidence import ResolvedFactCommitSourcesV2
from .fact_proposal_audit_v2 import (
    FactCommitProposalAuditProjectionV2,
    FactCommitProposalAuthorityReaderV2,
    PinnedFactCommitProposalAuthorityHandleV2,
)
from .fact_v2_acceptance_envelope_authority import (
    FactV2AcceptanceEnvelopeAuthorityHandle,
    FactV2AcceptanceEnvelopeAuthorityIssuer,
    FactV2AcceptanceEnvelopeAuthorityV2,
)
from .schema_core import FrozenModel
from .sealed_production_fact_registry_v2 import (
    FactPinnedCompilationCandidateV2,
    PreparedFactCommitMaterializationV2,
    SealedProductionFactPreparationRegistryV2,
    SealedProductionFactRegistryErrorV2,
)


class FactV2ProductionPlanError(ValueError):
    """Stable failure at the Fact-v2 production-plan boundary."""


class FactV2ProductionExecutionPlan(FrozenModel):
    """Inspectable evidence of an issued Fact plan; not a write capability."""

    envelope: FactV2AcceptanceEnvelopeAuthorityV2
    proposal_audit: FactCommitProposalAuditProjectionV2
    payload: FactCommitMaterializedPayloadV2
    durable_authority: DurableEffectCompilerAuthorityV1


class FactV2ProductionExecutionPlanHandle:
    """Opaque issuer-owned plan capability consumed only by ManifestBuilder."""

    __slots__ = ("__weakref__",)

    def __reduce__(self) -> object:
        raise TypeError("Fact v2 production plan handles cannot be serialized")

    def __copy__(self) -> object:
        raise TypeError("Fact v2 production plan handles cannot be copied")

    def __deepcopy__(self, memo: object) -> object:
        del memo
        raise TypeError("Fact v2 production plan handles cannot be copied")


@dataclass(frozen=True, slots=True)
class _FactV2ProductionPlanMaterial:
    envelope_handle: FactV2AcceptanceEnvelopeAuthorityHandle
    proposal_handle: PinnedFactCommitProposalAuthorityHandleV2
    prepared: PreparedFactCommitMaterializationV2
    sources: ResolvedFactCommitSourcesV2
    plan: FactV2ProductionExecutionPlan


class FactV2ProductionPlanIssuer:
    """Issue and revalidate first-vertical Fact production plan handles."""

    __slots__ = ("__registry", "__reader", "__envelope_issuer", "__handles")

    def __init__(
        self,
        *,
        registry: SealedProductionFactPreparationRegistryV2,
        proposal_reader: FactCommitProposalAuthorityReaderV2,
        envelope_issuer: FactV2AcceptanceEnvelopeAuthorityIssuer,
    ) -> None:
        if type(registry) is not SealedProductionFactPreparationRegistryV2:
            raise TypeError("Fact production plan requires the exact sealed registry")
        if type(proposal_reader) is not FactCommitProposalAuthorityReaderV2:
            raise TypeError("Fact production plan requires the exact proposal reader")
        if type(envelope_issuer) is not FactV2AcceptanceEnvelopeAuthorityIssuer:
            raise TypeError("Fact production plan requires the exact envelope issuer")
        self.__registry = registry
        self.__reader = proposal_reader
        self.__envelope_issuer = envelope_issuer
        self.__handles: WeakKeyDictionary[
            FactV2ProductionExecutionPlanHandle, _FactV2ProductionPlanMaterial
        ] = WeakKeyDictionary()

    def issue(
        self,
        *,
        envelope_handle: FactV2AcceptanceEnvelopeAuthorityHandle,
        proposal_handle: PinnedFactCommitProposalAuthorityHandleV2,
        prepared: PreparedFactCommitMaterializationV2,
        sources: ResolvedFactCommitSourcesV2,
    ) -> FactV2ProductionExecutionPlanHandle:
        try:
            envelope = self.__envelope_issuer.envelope(handle=envelope_handle)
            compiled = self.__compile_and_reverse_verify(
                envelope=envelope,
                proposal_handle=proposal_handle,
                prepared=prepared,
                sources=sources,
            )
        except (SealedProductionFactRegistryErrorV2, ValueError) as exc:
            raise FactV2ProductionPlanError(str(exc)) from exc
        plan = FactV2ProductionExecutionPlan(
            envelope=envelope,
            proposal_audit=compiled.proposal_audit,
            payload=compiled.payload,
            durable_authority=compiled.durable_authority,
        )
        handle = FactV2ProductionExecutionPlanHandle()
        self.__handles[handle] = _FactV2ProductionPlanMaterial(
            envelope_handle=envelope_handle,
            proposal_handle=proposal_handle,
            prepared=prepared,
            sources=sources,
            plan=plan,
        )
        return handle

    def inspect(
        self, *, handle: FactV2ProductionExecutionPlanHandle
    ) -> FactV2ProductionExecutionPlan:
        return self.__material(handle).plan.model_copy(deep=True)

    def revalidate(
        self, *, handle: FactV2ProductionExecutionPlanHandle
    ) -> FactV2ProductionExecutionPlan:
        """Recompute compilation from the original opaque capabilities."""

        material = self.__material(handle)
        try:
            envelope = self.__envelope_issuer.envelope(handle=material.envelope_handle)
            compiled = self.__compile_and_reverse_verify(
                envelope=envelope,
                proposal_handle=material.proposal_handle,
                prepared=material.prepared,
                sources=material.sources,
            )
        except (SealedProductionFactRegistryErrorV2, ValueError) as exc:
            raise FactV2ProductionPlanError(str(exc)) from exc
        current = FactV2ProductionExecutionPlan(
            envelope=envelope,
            proposal_audit=compiled.proposal_audit,
            payload=compiled.payload,
            durable_authority=compiled.durable_authority,
        )
        if current != material.plan:
            raise FactV2ProductionPlanError(
                "Fact production plan no longer matches its sealed capabilities"
            )
        return current.model_copy(deep=True)

    def owns(self, value: object) -> bool:
        return type(value) is FactV2ProductionExecutionPlanHandle and value in self.__handles

    def __compile_and_reverse_verify(
        self,
        *,
        envelope: FactV2AcceptanceEnvelopeAuthorityV2,
        proposal_handle: PinnedFactCommitProposalAuthorityHandleV2,
        prepared: PreparedFactCommitMaterializationV2,
        sources: ResolvedFactCommitSourcesV2,
    ) -> FactPinnedCompilationCandidateV2:
        audit = self.__reader.audit(handle=proposal_handle)
        cursor = self.__reader.cursor(handle=proposal_handle)
        if (
            envelope.world_id != audit.proposal_world_id
            or envelope.cursor != cursor
            or envelope.proposal_audit_event_ref != audit.event_ref
            or envelope.proposal_audit_payload_hash != audit.event_payload_hash
            or envelope.proposal_hash != audit.proposal_hash
        ):
            raise FactV2ProductionPlanError(
                "Fact production envelope does not match its proposal audit pin"
            )
        compiled = self.__registry.compile_from_pinned_audit(
            prepared=prepared,
            proposal_reader=self.__reader,
            proposal_handle=proposal_handle,
            acceptance_id=envelope.acceptance_id,
            sources=sources,
        )
        verified = self.__registry.reverse_verify(
            prepared=prepared,
            acceptance_id=envelope.acceptance_id,
            sources=sources,
            payload=compiled.payload,
        )
        if verified != compiled.payload:
            raise FactV2ProductionPlanError("Fact production payload failed reverse verification")
        return compiled

    def __material(
        self, handle: FactV2ProductionExecutionPlanHandle
    ) -> _FactV2ProductionPlanMaterial:
        if type(handle) is not FactV2ProductionExecutionPlanHandle:
            raise FactV2ProductionPlanError("Fact production plan handle belongs to another issuer")
        material = self.__handles.get(handle)
        if material is None:
            raise FactV2ProductionPlanError("Fact production plan handle belongs to another issuer")
        return material


__all__ = [
    "FactV2ProductionExecutionPlan",
    "FactV2ProductionExecutionPlanHandle",
    "FactV2ProductionPlanError",
    "FactV2ProductionPlanIssuer",
]

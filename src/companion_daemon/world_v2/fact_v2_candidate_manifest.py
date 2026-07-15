"""Inert candidate manifests for the first Fact-v2 accepted-effect vertical.

This is deliberately before the production ManifestBuilder and AtomicRecorder.
It has no ledger, event-catalog, reducer, runtime, or legacy compiler import.
The builder only produces a process-local handle containing canonical DTOs;
neither that handle nor its inspected value can materialize a ``WorldEvent``.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from weakref import WeakKeyDictionary

from pydantic import Field, model_validator

from .accepted_effect_contracts import (
    EFFECT_AUTHORITY_VERSION,
    AcceptanceAuthorizedEffectV3,
    AcceptanceChangeAuthorityV3,
    AcceptanceManifestProposalV3,
    AcceptanceManifestV3,
    EffectAuthorityRefV3,
    canonical_acceptance_manifest_v3_hash,
)
from .fact_accepted_contracts import FactCommitMaterializedPayloadV2, fact_commit_event_payload_hash
from .fact_proof_backed_evidence import ResolvedFactCommitSourcesV2
from .fact_proposal_audit_v2 import (
    FactCommitProposalAuthorityReaderV2,
    PinnedFactCommitProposalAuthorityHandleV2,
)
from .proposal_envelope_v2 import (
    FactCommitProposalEnvelopeV2,
    canonical_full_change_authority_hash_v2,
)
from .schema_core import FrozenModel
from .schemas import ProjectionCursor
from .sealed_production_fact_registry_v2 import (
    FactPinnedCompilationCandidateV2,
    PreparedFactCommitMaterializationV2,
    SealedProductionFactPreparationRegistryV2,
    SealedProductionFactRegistryErrorV2,
)


FACT_V2_CANDIDATE_EVENT_TYPE = "FactCommittedV2"


class FactV2CandidateManifestError(ValueError):
    """Stable failure at the pre-recorder Fact-v2 manifest boundary."""


def _canonical_json(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    )


class FactV2AcceptanceEnvelopeCandidate(FrozenModel):
    """Trusted upstream envelope inputs, without authority to record them."""

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
    def time_is_ordered_and_aware(self) -> FactV2AcceptanceEnvelopeCandidate:
        if (
            self.logical_time.tzinfo is None
            or self.created_at.tzinfo is None
            or self.logical_time.utcoffset() is None
            or self.created_at.utcoffset() is None
        ):
            raise ValueError("Fact candidate envelope times must be timezone-aware")
        return self


class FactV2CandidateManifest(FrozenModel):
    """Inspectable, inert candidate state held behind the issuer handle."""

    envelope: FactV2AcceptanceEnvelopeCandidate
    materialized_payload: FactCommitMaterializedPayloadV2
    manifest: AcceptanceManifestV3
    candidate_event_id: str = Field(min_length=1, max_length=512)
    candidate_idempotency_key: str = Field(min_length=1, max_length=512)

    @model_validator(mode="after")
    def manifest_and_effect_are_exact(self) -> FactV2CandidateManifest:
        if self.manifest.acceptance_id != self.envelope.acceptance_id:
            raise ValueError("Fact candidate manifest does not bind acceptance id")
        if self.manifest.evaluated_world_revision != self.envelope.cursor.world_revision:
            raise ValueError("Fact candidate manifest does not bind full cursor")
        if len(self.manifest.authorized_effects) != 1:
            raise ValueError("Fact candidate requires exactly one effect")
        effect = self.manifest.authorized_effects[0]
        if (
            effect.event_type != FACT_V2_CANDIDATE_EVENT_TYPE
            or effect.event_id != self.candidate_event_id
            or effect.payload_hash != fact_commit_event_payload_hash(self.materialized_payload)
        ):
            raise ValueError("Fact candidate effect does not bind materialized payload")
        if self.candidate_idempotency_key != _candidate_idempotency(
            payload=self.materialized_payload, world_id=self.envelope.world_id
        ):
            raise ValueError("Fact candidate idempotency key does not bind its world and payload")
        return self


class FactV2CandidateManifestHandle:
    """Opaque builder-issued capability; it cannot become a ledger event."""

    __slots__ = ("__weakref__",)

    def __reduce__(self) -> object:
        raise TypeError("Fact candidate manifest handles cannot be serialized")

    def __copy__(self) -> object:
        raise TypeError("Fact candidate manifest handles cannot be copied")

    def __deepcopy__(self, memo: object) -> object:
        del memo
        raise TypeError("Fact candidate manifest handles cannot be copied")


@dataclass(frozen=True, slots=True)
class _FactV2CandidateManifestMaterial:
    candidate: FactV2CandidateManifest


class FactV2CandidateManifestBuilder:
    """Build one audit-bound Fact-v2 manifest candidate without write authority."""

    __slots__ = ("__registry", "__reader", "__handles")

    def __init__(
        self,
        *,
        registry: SealedProductionFactPreparationRegistryV2,
        proposal_reader: FactCommitProposalAuthorityReaderV2,
    ) -> None:
        if type(registry) is not SealedProductionFactPreparationRegistryV2:
            raise TypeError("Fact candidate builder requires the exact sealed Fact registry")
        if type(proposal_reader) is not FactCommitProposalAuthorityReaderV2:
            raise TypeError("Fact candidate builder requires the exact Fact proposal reader")
        self.__registry = registry
        self.__reader = proposal_reader
        self.__handles: WeakKeyDictionary[
            FactV2CandidateManifestHandle, _FactV2CandidateManifestMaterial
        ] = WeakKeyDictionary()

    def build(
        self,
        *,
        envelope: FactV2AcceptanceEnvelopeCandidate,
        proposal_handle: PinnedFactCommitProposalAuthorityHandleV2,
        prepared: PreparedFactCommitMaterializationV2,
        sources: ResolvedFactCommitSourcesV2,
    ) -> FactV2CandidateManifestHandle:
        if type(envelope) is not FactV2AcceptanceEnvelopeCandidate:
            raise FactV2CandidateManifestError("Fact candidate envelope must use its exact contract")
        try:
            pinned_cursor = self.__reader.cursor(handle=proposal_handle)
            proposal = self.__reader.proposal(handle=proposal_handle)
            compiled = self.__registry.compile_from_pinned_audit(
                prepared=prepared,
                proposal_reader=self.__reader,
                proposal_handle=proposal_handle,
                acceptance_id=envelope.acceptance_id,
                sources=sources,
            )
        except (SealedProductionFactRegistryErrorV2, ValueError) as exc:
            raise FactV2CandidateManifestError(str(exc)) from exc
        if envelope.world_id != compiled.proposal_audit.proposal_world_id or envelope.cursor != pinned_cursor:
            raise FactV2CandidateManifestError(
                "Fact candidate envelope does not match its pinned proposal cursor"
            )
        candidate = _build_candidate(envelope=envelope, proposal=proposal, compiled=compiled)
        handle = FactV2CandidateManifestHandle()
        self.__handles[handle] = _FactV2CandidateManifestMaterial(candidate=candidate)
        return handle

    def inspect(self, *, handle: FactV2CandidateManifestHandle) -> FactV2CandidateManifest:
        """Return a fresh defensive value; no event or write method is exposed."""

        material = self.__material(handle)
        return material.candidate.model_copy(deep=True)

    def owns(self, value: object) -> bool:
        return type(value) is FactV2CandidateManifestHandle and value in self.__handles

    def __material(self, handle: FactV2CandidateManifestHandle) -> _FactV2CandidateManifestMaterial:
        if type(handle) is not FactV2CandidateManifestHandle:
            raise FactV2CandidateManifestError("Fact candidate handle belongs to another builder")
        material = self.__handles.get(handle)
        if material is None:
            raise FactV2CandidateManifestError("Fact candidate handle belongs to another builder")
        return material


def _build_candidate(
    *,
    envelope: FactV2AcceptanceEnvelopeCandidate,
    proposal: FactCommitProposalEnvelopeV2,
    compiled: FactPinnedCompilationCandidateV2,
) -> FactV2CandidateManifest:
    # ``proposal`` comes only from the exact Fact proposal reader above.  Keep
    # its structural interpretation here narrow and reconstruct each manifest
    # authority field rather than persisting an opaque proposal blob.
    changes = tuple(
        AcceptanceChangeAuthorityV3(
            change_id=change.change_id,
            kind=change.kind,
            target_id=change.target_id,
            transition=change.transition,
            expected_entity_revision=change.expected_entity_revision,
            evidence_refs=change.evidence_refs,
            preconditions=change.preconditions,
            policy_refs=change.policy_refs,
            payload_schema=change.payload.payload_schema,
            payload_version=change.payload.payload_version,
            payload_hash=change.payload.payload_hash,
            full_change_authority_hash=canonical_full_change_authority_hash_v2(change),
        )
        for change in proposal.proposed_changes
    )
    audit = compiled.proposal_audit
    summary = AcceptanceManifestProposalV3(
        proposal_id=proposal.proposal_id,
        proposal_kind=proposal.proposal_kind,
        proposal_schema_registry=proposal.schema_registry_version,
        audit_contract=audit.audit_contract,
        proposal_event_ref=audit.event_ref,
        proposal_event_payload_hash=audit.event_payload_hash,
        proposal_hash=audit.proposal_hash,
        evaluated_world_revision=proposal.evaluated_world_revision,
        changes=changes,
        action_intents=(),
    )
    change = changes[0]
    payload_hash = fact_commit_event_payload_hash(compiled.payload)
    effect_id = _candidate_effect_id(
        envelope=envelope,
        payload_hash=payload_hash,
        proposal_id=summary.proposal_id,
        change=change,
    )
    effect = AcceptanceAuthorizedEffectV3(
        effect_authority_version=EFFECT_AUTHORITY_VERSION,
        ordinal=0,
        role="domain_mutation",
        event_id=effect_id,
        event_type=FACT_V2_CANDIDATE_EVENT_TYPE,
        payload_hash=payload_hash,
        authority_refs=(
            EffectAuthorityRefV3(
                proposal_id=summary.proposal_id,
                authority_kind="change",
                authority_id=change.change_id,
                authority_hash=change.full_change_authority_hash,
            ),
        ),
        domain_compiler_authority=compiled.durable_authority,
    )
    manifest_data: dict[str, object] = {
        "manifest_version": "acceptance-manifest.3",
        "acceptance_id": envelope.acceptance_id,
        "status": "accepted",
        "evaluated_world_revision": envelope.cursor.world_revision,
        "proposals": (summary,),
        "authorized_effects": (effect,),
    }
    manifest_data["manifest_hash"] = canonical_acceptance_manifest_v3_hash(manifest_data)
    manifest = AcceptanceManifestV3(
        manifest_version="acceptance-manifest.3",
        acceptance_id=envelope.acceptance_id,
        status="accepted",
        evaluated_world_revision=envelope.cursor.world_revision,
        proposals=(summary,),
        authorized_effects=(effect,),
        manifest_hash=str(manifest_data["manifest_hash"]),
    )
    return FactV2CandidateManifest(
        envelope=envelope,
        materialized_payload=compiled.payload,
        manifest=manifest,
        candidate_event_id=effect_id,
        candidate_idempotency_key=_candidate_idempotency(
            payload=compiled.payload, world_id=envelope.world_id
        ),
    )


def _candidate_effect_id(
    *,
    envelope: FactV2AcceptanceEnvelopeCandidate,
    payload_hash: str,
    proposal_id: str,
    change: AcceptanceChangeAuthorityV3,
) -> str:
    digest = hashlib.sha256(
        _canonical_json(
            {
                "contract": "fact-v2-candidate-effect-id.1",
                "world_id": envelope.world_id,
                "cursor": envelope.cursor.model_dump(mode="json"),
                "acceptance_event_id": envelope.acceptance_event_id,
                "ordinal": 0,
                "event_type": FACT_V2_CANDIDATE_EVENT_TYPE,
                "payload_hash": payload_hash,
                "proposal_id": proposal_id,
                "change_id": change.change_id,
                "full_change_authority_hash": change.full_change_authority_hash,
            }
        ).encode("utf-8")
    ).hexdigest()
    return f"event:accepted-effect-v3:{digest}"


def _candidate_idempotency(*, payload: FactCommitMaterializedPayloadV2, world_id: str) -> str:
    digest = hashlib.sha256(
        _canonical_json(
            {
                "contract": "fact-v2-candidate-idempotency.1",
                "world_id": world_id,
                "payload_contract": payload.payload_contract,
                "fact_id": payload.fact_id,
                "transition_id": payload.transition_id,
                "materialized_change_hash": payload.materialized_change_hash,
            }
        ).encode("utf-8")
    ).hexdigest()
    return f"fact-v2-candidate:{digest}"


__all__ = [
    "FACT_V2_CANDIDATE_EVENT_TYPE",
    "FactV2AcceptanceEnvelopeCandidate",
    "FactV2CandidateManifest",
    "FactV2CandidateManifestBuilder",
    "FactV2CandidateManifestError",
    "FactV2CandidateManifestHandle",
]

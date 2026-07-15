"""Build-time sealed, inert preparation registry for Fact commit v2.

This module is deliberately *not* an adapter for ``acceptance_compilers``.
That registry consumes the frozen proposal/payload v1 contracts whereas the
first accepted Fact vertical consumes proposal registry v2 and
``FactCommitMaterializedPayloadV2``.  Bridging those contracts before the
FactCommitted-v2 event lane and Manifest-v3 recorder exist would incorrectly
turn an inert payload into production authority.

The narrow interface here therefore has two responsibilities only:

* expose the one build-time Fact commit descriptor, with no registration,
  callable, or digest supplied by a caller; and
* bind the already sealed Fact adapter behind a registry-owned, process-local
  preparation capability.

Prepared payloads remain inert.  In particular this module neither imports a
ledger, an event catalog, ``acceptance_compilers``, nor the runtime planner.
The future production compiler must consume this seam only after it can prove
the recorded v2 proposal and accepted-manifest authority.
"""

from __future__ import annotations

import hashlib
from copy import deepcopy
from dataclasses import dataclass
from threading import RLock
from weakref import WeakKeyDictionary

from pydantic import Field, model_validator

from .accepted_effect_contracts import (
    COMPILER_AUTHORITY_VERSION,
    DurableDomainCompilerKeyV1,
    DurableEffectCompilerAuthorityV1,
    TypedCompilerDependencyV1,
)
from .fact_accepted_contracts import FactCommitMaterializedPayloadV2
from .fact_proof_backed_evidence import (
    ProofBackedFactEvidenceResolverV2,
    ResolvedFactCommitSourcesV2,
)
from .fact_proposal_audit_v2 import (
    FactCommitProposalAuditProjectionV2,
    FactCommitProposalAuthorityReaderV2,
    PinnedFactCommitProposalAuthorityHandleV2,
)
from .proposal_envelope_v2 import FactCommitProposalEnvelopeV2, FactCommitTypedChangeV2
from .schema_core import FrozenModel
from .sealed_fact_commit_adapter_v2 import (
    FactCommitPolicyResolutionV2,
    SealedFactCommitAdapterError,
    SealedFactCommitAdapterV2,
    SealedFactCommitCompilationHandleV2,
)


SEALED_FACT_COMMIT_REGISTRY_VERSION_V2 = "acceptance-domain-compilers.2"
SEALED_FACT_COMMIT_REGISTRY_REF_V2 = "compiler-registry:production.2"


class SealedProductionFactRegistryErrorV2(ValueError):
    """Stable failure at the sealed Fact preparation seam."""


def _digest(label: str) -> str:
    """Derive a fixed build descriptor digest from its versioned material.

    These constants are intentionally defined in this module, rather than
    accepted from a registration object.  The exact adapter classes are also
    checked on construction, so a caller cannot substitute a callable merely
    by copying one of these public descriptor values.
    """

    return hashlib.sha256(f"world-v2:sealed-fact-install.2:{label}".encode()).hexdigest()


class SealedFactCommitInstallDescriptorV2(FrozenModel):
    """Closed metadata for the first future production compiler vertical."""

    install_descriptor_ref: str = Field(min_length=1, max_length=512)
    install_descriptor_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    registry_version: str = Field(min_length=1, max_length=128)
    registry_ref: str = Field(min_length=1, max_length=512)
    registry_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    compiler_key: DurableDomainCompilerKeyV1
    event_types: tuple[str, ...] = Field(min_length=1, max_length=1)
    compiler_ref: str = Field(min_length=1, max_length=512)
    compiler_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    canonical_codec_ref: str = Field(min_length=1, max_length=512)
    canonical_codec_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    resolver_ref: str = Field(min_length=1, max_length=512)
    resolver_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    reverse_verifier_ref: str = Field(min_length=1, max_length=512)
    reverse_verifier_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    output_contract_ref: str = Field(min_length=1, max_length=512)
    output_contract_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    predicate_matrix_ref: str = Field(min_length=1, max_length=512)
    predicate_matrix_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_use_matrix_ref: str = Field(min_length=1, max_length=512)
    evidence_use_matrix_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    privacy_matrix_ref: str = Field(min_length=1, max_length=512)
    privacy_matrix_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    observation_authority_contract_ref: str = Field(min_length=1, max_length=512)
    observation_authority_contract_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    event_catalog_ref: str = Field(min_length=1, max_length=512)
    event_catalog_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    domain_identity_contract_ref: str = Field(min_length=1, max_length=512)
    domain_identity_contract_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    reducer_bundle_ref: str = Field(min_length=1, max_length=512)
    reducer_bundle_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_refs: tuple[str, ...] = Field(min_length=1, max_length=64)
    typed_dependencies: tuple[TypedCompilerDependencyV1, ...] = Field(max_length=16)

    @model_validator(mode="after")
    def is_the_exact_first_fact_vertical(self) -> SealedFactCommitInstallDescriptorV2:
        if self.registry_version != SEALED_FACT_COMMIT_REGISTRY_VERSION_V2:
            raise ValueError("Fact install descriptor registry version is not sealed")
        if self.registry_ref != SEALED_FACT_COMMIT_REGISTRY_REF_V2:
            raise ValueError("Fact install descriptor registry ref is not sealed")
        if self.compiler_key != _SEALED_FACT_KEY:
            raise ValueError("Fact install descriptor compiler key is not sealed")
        if self.event_types != ("FactCommitted",):
            raise ValueError("Fact install descriptor owns only FactCommitted")
        if self.policy_refs != _SEALED_POLICY_REFS:
            raise ValueError("Fact install descriptor policy refs are not sealed")
        keys = tuple((item.dependency_kind, item.dependency_ref) for item in self.typed_dependencies)
        if keys != tuple(sorted(keys)) or len(keys) != len(set(keys)):
            raise ValueError("Fact install descriptor dependencies are not canonical")
        return self


_SEALED_FACT_KEY = DurableDomainCompilerKeyV1(
    proposal_schema_registry="world-v2-proposals.2",
    change_kind="fact_transition",
    transition="commit",
    payload_schema="fact_commit_intent.v2",
    payload_version=2,
)
_SEALED_POLICY_REFS = ("policy:fact-commit.2",)
_SEALED_DESCRIPTOR_RAW: dict[str, object] = {
    "install_descriptor_ref": "compiler-install:fact-commit.2",
    "install_descriptor_digest": _digest("install-descriptor"),
    "registry_version": SEALED_FACT_COMMIT_REGISTRY_VERSION_V2,
    "registry_ref": SEALED_FACT_COMMIT_REGISTRY_REF_V2,
    "registry_digest": _digest("registry"),
    "compiler_key": _SEALED_FACT_KEY.model_dump(mode="json"),
    "event_types": ("FactCommitted",),
    "compiler_ref": "compiler:fact-commit.2",
    "compiler_digest": _digest("sealed-fact-commit-adapter"),
    "canonical_codec_ref": "codec:fact-commit.2",
    "canonical_codec_digest": _digest("fact-commit-canonical-codec"),
    "resolver_ref": "resolver:proof-backed-fact-evidence.2",
    "resolver_digest": _digest("proof-backed-fact-evidence-resolver"),
    "reverse_verifier_ref": "verifier:fact-commit.2",
    "reverse_verifier_digest": _digest("sealed-fact-commit-reverse-verifier"),
    "output_contract_ref": "contract:fact-commit-materialized.2",
    "output_contract_digest": _digest("fact-commit-materialized-contract"),
    "predicate_matrix_ref": "matrix:fact-predicate.2",
    "predicate_matrix_digest": _digest("fact-predicate-matrix"),
    "evidence_use_matrix_ref": "matrix:fact-evidence-use.2",
    "evidence_use_matrix_digest": _digest("fact-evidence-use-matrix"),
    "privacy_matrix_ref": "matrix:fact-privacy.2",
    "privacy_matrix_digest": _digest("fact-privacy-matrix"),
    "observation_authority_contract_ref": "contract:observation-authority.2",
    "observation_authority_contract_digest": _digest("observation-authority-contract"),
    "event_catalog_ref": "event-catalog:world-v2.18",
    "event_catalog_digest": _digest("event-catalog-world-v2.18"),
    "domain_identity_contract_ref": "domain-identity:fact.2",
    "domain_identity_contract_digest": _digest("fact-domain-identity"),
    "reducer_bundle_ref": "reducer-bundle:world-v2.18",
    "reducer_bundle_digest": _digest("reducer-bundle-world-v2.18"),
    "policy_refs": _SEALED_POLICY_REFS,
    "typed_dependencies": (
        {
            "dependency_kind": "canonicalizer",
            "dependency_ref": "codec:fact-commit.2",
            "dependency_digest": _digest("fact-commit-canonical-codec"),
        },
        {
            "dependency_kind": "hash_contract",
            "dependency_ref": "hash:fact-commit-materialized.2",
            "dependency_digest": _digest("fact-commit-materialized-hash"),
        },
        {
            "dependency_kind": "payload_schema",
            "dependency_ref": "schema:fact-commit-intent.2",
            "dependency_digest": _digest("fact-commit-intent-schema"),
        },
        {
            "dependency_kind": "policy_contract",
            "dependency_ref": "policy:fact-commit.2",
            "dependency_digest": _digest("fact-commit-policy-contract"),
        },
        {
            "dependency_kind": "proposal_schema",
            "dependency_ref": "schema:proposal-envelope.2",
            "dependency_digest": _digest("proposal-envelope-v2"),
        },
    ),
}


def sealed_fact_commit_install_descriptor_v2() -> SealedFactCommitInstallDescriptorV2:
    """Return a strict fresh image of the build-time Fact install descriptor."""

    # Rehydrate rather than return a module-global Pydantic instance: hostile
    # ``object.__setattr__`` on a descriptor obtained by one caller cannot
    # affect another caller or registry-owned comparison.
    return SealedFactCommitInstallDescriptorV2.model_validate(
        deepcopy(_SEALED_DESCRIPTOR_RAW), strict=True
    )


class PreparedFactCommitMaterializationV2:
    """Opaque registry-issued preparation capability; it is not authority.

    Its only purpose is to ensure that the same sealed registry that bound a
    proposal/change/policy pair is the registry that asks the exact Fact
    adapter to materialize or reverse verify it.  It has no DTO fields and
    cannot be converted into an event, manifest, or planner handle.
    """

    __slots__ = ("__weakref__",)

    def __reduce__(self) -> object:
        raise TypeError("prepared Fact capabilities cannot be serialized")

    def __copy__(self) -> object:
        raise TypeError("prepared Fact capabilities cannot be copied")

    def __deepcopy__(self, memo: object) -> object:
        del memo
        raise TypeError("prepared Fact capabilities cannot be copied")


@dataclass(frozen=True, slots=True)
class _PreparedFactMaterializationV2:
    adapter_handle: SealedFactCommitCompilationHandleV2


class SealedProductionFactPreparationRegistryV2:
    """Deep sealed module for Fact-v2 preparation, deliberately before runtime.

    The only configurable dependency is the exact proof-backed resolver.  A
    resolver is a required read capability, not an install descriptor: this
    module never accepts registrations, adapters, artifact refs, or digests.
    """

    __slots__ = ("__adapter", "__prepared", "__lock")

    def __init__(self, *, resolver: ProofBackedFactEvidenceResolverV2) -> None:
        if type(resolver) is not ProofBackedFactEvidenceResolverV2:
            raise TypeError("sealed Fact preparation registry requires the exact proof-backed resolver")
        self.__adapter = SealedFactCommitAdapterV2(resolver=resolver)
        self.__prepared: WeakKeyDictionary[
            PreparedFactCommitMaterializationV2, _PreparedFactMaterializationV2
        ] = WeakKeyDictionary()
        self.__lock = RLock()

    @property
    def descriptor(self) -> SealedFactCommitInstallDescriptorV2:
        return sealed_fact_commit_install_descriptor_v2()

    def durable_authority_candidate(
        self,
        *,
        proposal_reader: FactCommitProposalAuthorityReaderV2,
        proposal_handle: PinnedFactCommitProposalAuthorityHandleV2,
    ) -> DurableEffectCompilerAuthorityV1:
        """Derive inert v3 compiler metadata from one reader-owned Fact audit.

        The returned value is intentionally a candidate DTO, not a plan or a
        capability.  FactCommitted-v2 does not yet have its own event/reducer
        lane, so no recorder may consume this value.
        Requiring the exact reader and its opaque pin prevents callers from
        pairing this descriptor with a hand-written proposal summary.  Future
        plan issuance must additionally bind this same audit to a prepared
        compilation and an acceptance envelope.
        """

        if type(proposal_reader) is not FactCommitProposalAuthorityReaderV2:
            raise SealedProductionFactRegistryErrorV2(
                "Fact durable authority requires the exact Fact proposal reader"
            )
        try:
            audit = proposal_reader.audit(handle=proposal_handle)
        except Exception as exc:
            raise SealedProductionFactRegistryErrorV2(
                "Fact durable authority requires a reader-owned proposal pin"
            ) from exc
        return _durable_authority_from_audit(
            descriptor=sealed_fact_commit_install_descriptor_v2(), audit=audit
        )

    def prepare(
        self,
        *,
        proposal: FactCommitProposalEnvelopeV2,
        change: FactCommitTypedChangeV2,
        policy: FactCommitPolicyResolutionV2,
        world_id: str,
    ) -> PreparedFactCommitMaterializationV2:
        """Bind an exact v2 Fact change to this fixed policy install.

        This operation is intentionally not called ``compile_authority``:
        recorded proposal authentication, accepted-manifest authority and
        FactCommitted-v2 event/reducer materialization are still separate
        future seams.
        """

        if type(policy) is not FactCommitPolicyResolutionV2 or policy.policy_refs != _SEALED_POLICY_REFS:
            raise SealedProductionFactRegistryErrorV2(
                "Fact policy is not admitted by the sealed Fact install descriptor"
            )
        try:
            adapter_handle = self.__adapter.bind(
                proposal=proposal, change=change, policy=policy, world_id=world_id
            )
        except SealedFactCommitAdapterError as exc:
            raise SealedProductionFactRegistryErrorV2(str(exc)) from exc
        prepared = PreparedFactCommitMaterializationV2()
        with self.__lock:
            self.__prepared[prepared] = _PreparedFactMaterializationV2(adapter_handle=adapter_handle)
        return prepared

    def compile(
        self,
        *,
        prepared: PreparedFactCommitMaterializationV2,
        acceptance_id: str,
        sources: ResolvedFactCommitSourcesV2,
    ) -> FactCommitMaterializedPayloadV2:
        """Materialize inert v2 Fact bytes from one registry-owned preparation."""

        material = self.__prepared_material(prepared)
        try:
            return self.__adapter.compile(
                handle=material.adapter_handle, acceptance_id=acceptance_id, sources=sources
            )
        except SealedFactCommitAdapterError as exc:
            raise SealedProductionFactRegistryErrorV2(str(exc)) from exc

    def reverse_verify(
        self,
        *,
        prepared: PreparedFactCommitMaterializationV2,
        acceptance_id: str,
        sources: ResolvedFactCommitSourcesV2,
        payload: FactCommitMaterializedPayloadV2,
    ) -> FactCommitMaterializedPayloadV2:
        """Recompute the sealed inert payload; caller bytes cannot survive."""

        material = self.__prepared_material(prepared)
        try:
            return self.__adapter.reverse_verify(
                handle=material.adapter_handle,
                acceptance_id=acceptance_id,
                sources=sources,
                payload=payload,
            )
        except SealedFactCommitAdapterError as exc:
            raise SealedProductionFactRegistryErrorV2(str(exc)) from exc

    def owns_preparation(self, value: object) -> bool:
        if type(value) is not PreparedFactCommitMaterializationV2:
            return False
        with self.__lock:
            return value in self.__prepared

    def __prepared_material(
        self, value: PreparedFactCommitMaterializationV2
    ) -> _PreparedFactMaterializationV2:
        if type(value) is not PreparedFactCommitMaterializationV2:
            raise SealedProductionFactRegistryErrorV2(
                "prepared Fact capability belongs to another registry"
            )
        with self.__lock:
            material = self.__prepared.get(value)
        if material is None:
            raise SealedProductionFactRegistryErrorV2(
                "prepared Fact capability belongs to another registry"
            )
        return material


def _durable_authority_from_audit(
    *,
    descriptor: SealedFactCommitInstallDescriptorV2,
    audit: FactCommitProposalAuditProjectionV2,
) -> DurableEffectCompilerAuthorityV1:
    """Project all sealed artifacts plus one recorded audit into the v3 DTO."""

    return DurableEffectCompilerAuthorityV1(
        authority_version=COMPILER_AUTHORITY_VERSION,
        install_descriptor_ref=descriptor.install_descriptor_ref,
        install_descriptor_digest=descriptor.install_descriptor_digest,
        registry_version=descriptor.registry_version,
        registry_ref=descriptor.registry_ref,
        registry_digest=descriptor.registry_digest,
        compiler_key=descriptor.compiler_key,
        compiler_ref=descriptor.compiler_ref,
        compiler_digest=descriptor.compiler_digest,
        reverse_verifier_ref=descriptor.reverse_verifier_ref,
        reverse_verifier_digest=descriptor.reverse_verifier_digest,
        canonical_codec_ref=descriptor.canonical_codec_ref,
        canonical_codec_digest=descriptor.canonical_codec_digest,
        output_contract_ref=descriptor.output_contract_ref,
        output_contract_digest=descriptor.output_contract_digest,
        resolver_ref=descriptor.resolver_ref,
        resolver_digest=descriptor.resolver_digest,
        predicate_matrix_ref=descriptor.predicate_matrix_ref,
        predicate_matrix_digest=descriptor.predicate_matrix_digest,
        evidence_use_matrix_ref=descriptor.evidence_use_matrix_ref,
        evidence_use_matrix_digest=descriptor.evidence_use_matrix_digest,
        privacy_matrix_ref=descriptor.privacy_matrix_ref,
        privacy_matrix_digest=descriptor.privacy_matrix_digest,
        observation_authority_contract_ref=descriptor.observation_authority_contract_ref,
        observation_authority_contract_digest=descriptor.observation_authority_contract_digest,
        event_catalog_ref=descriptor.event_catalog_ref,
        event_catalog_digest=descriptor.event_catalog_digest,
        domain_identity_contract_ref=descriptor.domain_identity_contract_ref,
        domain_identity_contract_digest=descriptor.domain_identity_contract_digest,
        reducer_bundle_ref=descriptor.reducer_bundle_ref,
        reducer_bundle_digest=descriptor.reducer_bundle_digest,
        typed_dependencies=descriptor.typed_dependencies,
        proposal_event_ref=audit.event_ref,
        proposal_event_payload_hash=audit.event_payload_hash,
        proposal_hash=audit.proposal_hash,
    )


__all__ = [
    "PreparedFactCommitMaterializationV2",
    "SEALED_FACT_COMMIT_REGISTRY_REF_V2",
    "SEALED_FACT_COMMIT_REGISTRY_VERSION_V2",
    "SealedFactCommitInstallDescriptorV2",
    "SealedProductionFactPreparationRegistryV2",
    "SealedProductionFactRegistryErrorV2",
    "sealed_fact_commit_install_descriptor_v2",
]

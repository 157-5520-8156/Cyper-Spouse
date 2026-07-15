"""Sealed, inert Fact-commit materialization seam.

This module is deliberately narrower than an Acceptance compiler.  It binds a
single normalized Fact change to its normalized proposal, accepts only the
proof-backed Fact evidence result, and deterministically produces the closed
``FactCommitMaterializedPayloadV2`` bytes.  Its output is still an inert DTO:
it neither installs a compiler nor constructs a ledger event.

The private handle is important even in this pre-runtime seam.  Callers cannot
mix a change from proposal A with proposal B, nor replace the policy result
after binding.  A future production Acceptance registry must issue an
equivalent capability only after it has authenticated the recorded proposal
and installed policy matrix; it must not make this module's DTO an authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from weakref import WeakKeyDictionary

from pydantic import Field, model_validator

from .fact_accepted_contracts import (
    FactAssertionBindingV2,
    FactCommitIntentV2,
    FactCommitMaterializedPayloadV2,
    FactCommitValuesV2,
    ResolvedFactEvidenceV2,
    canonical_fact_commit_materialized_json,
    canonical_fact_commit_materialized_hash,
    fact_commit_transition_id_v2,
    rehydrate_fact_commit_intent_v2_json,
    rehydrate_fact_commit_materialized_v2,
)
from .fact_proof_backed_evidence import (
    FactEvidenceResolutionError,
    ProofBackedFactEvidenceResolverV2,
    ResolvedFactCommitSourcesV2,
)
from .proposal_envelope_v2 import (
    FactCommitProposalEnvelopeV2,
    FactCommitTypedChangeV2,
    canonical_full_change_authority_hash_v2,
    validate_fact_commit_proposal_v2,
)
from .schema_core import FrozenModel
from .schemas import fact_conflict_key


class SealedFactCommitAdapterError(ValueError):
    """Stable error emitted by the inert Fact materialization seam."""


class FactCommitPolicyResolutionV2(FrozenModel):
    """Explicit policy result required before a Fact payload can be shaped.

    It is a value, not policy authority.  Keeping the decision explicit avoids
    hiding a rule such as ``all facts are single`` inside the materializer.
    """

    cardinality: Literal["single", "set"]
    policy_refs: tuple[str, ...] = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def refs_are_canonical(self) -> FactCommitPolicyResolutionV2:
        if self.policy_refs != tuple(sorted(set(self.policy_refs))) or any(
            not value or len(value) > 512 for value in self.policy_refs
        ):
            raise ValueError("Fact policy refs must be canonical and unique")
        return self


class SealedFactCommitCompilationHandleV2:
    """Opaque adapter-issued capability for one bound Fact compilation.

    All inputs are held only by the issuing adapter's weak registry.  Direct
    construction creates an unowned blank capability, which is rejected by
    ``compile`` and ``reverse_verify``.
    """

    __slots__ = ("__weakref__",)

    def __reduce__(self) -> object:
        raise TypeError("sealed Fact compilation handles cannot be serialized")

    def __copy__(self) -> object:
        raise TypeError("sealed Fact compilation handles cannot be copied")

    def __deepcopy__(self, memo: object) -> object:
        del memo
        raise TypeError("sealed Fact compilation handles cannot be copied")


@dataclass(frozen=True, slots=True)
class _SealedFactCommitCompilationInputsV2:
    proposal: FactCommitProposalEnvelopeV2
    change: FactCommitTypedChangeV2
    policy: FactCommitPolicyResolutionV2
    world_id: str


class SealedFactCommitAdapterV2:
    """Deep, side-effect-free module for one sealed Fact commit compilation."""

    __slots__ = ("__handles", "__resolver")

    def __init__(self, *, resolver: ProofBackedFactEvidenceResolverV2) -> None:
        if type(resolver) is not ProofBackedFactEvidenceResolverV2:
            raise TypeError("sealed Fact adapter requires an exact proof-backed Fact resolver")
        self.__resolver = resolver
        self.__handles: WeakKeyDictionary[
            SealedFactCommitCompilationHandleV2, _SealedFactCommitCompilationInputsV2
        ] = WeakKeyDictionary()

    def bind(
        self,
        *,
        proposal: FactCommitProposalEnvelopeV2,
        change: FactCommitTypedChangeV2,
        policy: FactCommitPolicyResolutionV2,
        world_id: str,
    ) -> SealedFactCommitCompilationHandleV2:
        """Strictly bind one change that belongs to the supplied proposal.

        Proposal audit persistence and policy authorization intentionally remain
        outside this module; later Acceptance owns those side effects.
        """

        strict_proposal = _strict_proposal(proposal=proposal, world_id=world_id)
        strict_change = _find_change(strict_proposal, change)
        strict_policy = _strict_policy(policy)
        if strict_change.policy_refs != strict_policy.policy_refs:
            raise SealedFactCommitAdapterError(
                "Fact policy resolution does not exactly bind change policy refs"
            )
        handle = SealedFactCommitCompilationHandleV2()
        self.__handles[handle] = _SealedFactCommitCompilationInputsV2(
            proposal=strict_proposal,
            change=strict_change,
            policy=strict_policy,
            world_id=world_id,
        )
        return handle

    def compile(
        self,
        *,
        handle: SealedFactCommitCompilationHandleV2,
        acceptance_id: str,
        sources: ResolvedFactCommitSourcesV2,
    ) -> FactCommitMaterializedPayloadV2:
        proposal, change, policy, world_id = self._inputs(handle)
        if type(acceptance_id) is not str or not acceptance_id or len(acceptance_id) > 256:
            raise SealedFactCommitAdapterError("Fact acceptance id is invalid")
        intent = rehydrate_fact_commit_intent_v2_json(change.payload.canonical_json)
        evidence, assertion = _strict_sources(
            resolver=self.__resolver,
            sources=sources,
            intent=intent,
            proposal=proposal,
        )
        full_change_authority_hash = canonical_full_change_authority_hash_v2(change)
        values = FactCommitValuesV2(
            subject_ref=intent.subject_ref,
            predicate_code=intent.predicate_code,
            cardinality=policy.cardinality,
            conflict_key=fact_conflict_key(
                subject_ref=intent.subject_ref, predicate_code=intent.predicate_code
            ),
            value_ref=intent.value_ref,
            value_hash=intent.value_hash.removeprefix("sha256:"),
            assertion_binding=assertion,
            anchor_evidence_refs=tuple(
                item
                for item, use in zip(evidence, intent.evidence_uses, strict=True)
                if use.anchor
            ),
            source_evidence_refs=evidence,
            confidence_bp=intent.confidence_bp,
            privacy_class=intent.privacy_class,
            status="active",
            withdrawal_reason_code=None,
            withdrawal_evidence_ref=None,
        )
        material: dict[str, object] = {
            "payload_contract": "fact-commit-materialized.2",
            "change_id": change.change_id,
            "transition_id": fact_commit_transition_id_v2(
                world_id=world_id,
                proposal_id=proposal.proposal_id,
                change_id=change.change_id,
                full_change_authority_hash=full_change_authority_hash,
                fact_id=change.target_id,
            ),
            "fact_id": change.target_id,
            "expected_entity_revision": change.expected_entity_revision,
            "evidence_refs": tuple(item.model_dump(mode="json") for item in evidence),
            "policy_refs": change.policy_refs,
            "acceptance_id": acceptance_id,
            "proposal_id": proposal.proposal_id,
            "evaluated_world_revision": proposal.evaluated_world_revision,
            "full_change_authority_hash": full_change_authority_hash,
            "values": values.model_dump(mode="json"),
        }
        material["materialized_change_hash"] = canonical_fact_commit_materialized_hash(material)
        return rehydrate_fact_commit_materialized_v2(material)

    def reverse_verify(
        self,
        *,
        handle: SealedFactCommitCompilationHandleV2,
        acceptance_id: str,
        sources: ResolvedFactCommitSourcesV2,
        payload: FactCommitMaterializedPayloadV2,
    ) -> FactCommitMaterializedPayloadV2:
        """Recompute canonical bytes; no caller-controlled payload fields survive."""

        if type(payload) is not FactCommitMaterializedPayloadV2:
            raise SealedFactCommitAdapterError("Fact materialized payload must use its exact contract")
        expected = self.compile(handle=handle, acceptance_id=acceptance_id, sources=sources)
        try:
            actual = rehydrate_fact_commit_materialized_v2(payload)
        except Exception as exc:
            raise SealedFactCommitAdapterError("Fact materialized payload is structurally invalid") from exc
        if canonical_fact_commit_materialized_json(actual) != canonical_fact_commit_materialized_json(expected):
            raise SealedFactCommitAdapterError("Fact materialized payload does not match sealed inputs")
        return actual

    def _inputs(
        self, handle: SealedFactCommitCompilationHandleV2
    ) -> tuple[FactCommitProposalEnvelopeV2, FactCommitTypedChangeV2, FactCommitPolicyResolutionV2, str]:
        if type(handle) is not SealedFactCommitCompilationHandleV2:
            raise SealedFactCommitAdapterError("Fact compilation handle belongs to another adapter")
        inputs = self.__handles.get(handle)
        if inputs is None:
            raise SealedFactCommitAdapterError("Fact compilation handle belongs to another adapter")
        return inputs.proposal, inputs.change, inputs.policy, inputs.world_id


def _strict_proposal(
    *, proposal: FactCommitProposalEnvelopeV2, world_id: str
) -> FactCommitProposalEnvelopeV2:
    if type(proposal) is not FactCommitProposalEnvelopeV2:
        raise SealedFactCommitAdapterError("Fact proposal must use its exact v2 contract")
    try:
        return validate_fact_commit_proposal_v2(proposal, world_id=world_id)
    except Exception as exc:
        raise SealedFactCommitAdapterError("Fact proposal failed canonical validation") from exc


def _find_change(
    proposal: FactCommitProposalEnvelopeV2, candidate: FactCommitTypedChangeV2
) -> FactCommitTypedChangeV2:
    if type(candidate) is not FactCommitTypedChangeV2:
        raise SealedFactCommitAdapterError("Fact change must use its exact v2 contract")
    try:
        exact = next(item for item in proposal.proposed_changes if item.change_id == candidate.change_id)
    except StopIteration as exc:
        raise SealedFactCommitAdapterError("Fact change does not belong to the sealed proposal") from exc
    if exact != candidate:
        raise SealedFactCommitAdapterError("Fact change differs from its sealed proposal image")
    return exact


def _strict_policy(value: FactCommitPolicyResolutionV2) -> FactCommitPolicyResolutionV2:
    if type(value) is not FactCommitPolicyResolutionV2:
        raise SealedFactCommitAdapterError("Fact policy must use its exact v2 contract")
    try:
        return FactCommitPolicyResolutionV2.model_validate(value.model_dump(), strict=True)
    except Exception as exc:
        raise SealedFactCommitAdapterError("Fact policy resolution is invalid") from exc


def _strict_sources(
    *,
    resolver: ProofBackedFactEvidenceResolverV2,
    sources: ResolvedFactCommitSourcesV2,
    intent: object,
    proposal: FactCommitProposalEnvelopeV2,
) -> tuple[tuple[ResolvedFactEvidenceV2, ...], FactAssertionBindingV2]:
    if type(intent) is not FactCommitIntentV2:
        raise SealedFactCommitAdapterError("sealed Fact intent is invalid")
    if type(resolver) is not ProofBackedFactEvidenceResolverV2:
        raise SealedFactCommitAdapterError("Fact sources require the bound proof-backed resolver")
    if type(sources) is not ResolvedFactCommitSourcesV2:
        raise SealedFactCommitAdapterError("Fact sources must come from the proof-backed resolver result")
    # ``intent`` is rehydrated immediately above; this guard prevents accepting
    # a duck-typed proposal object as a source of evidence ordering.
    expected_refs = tuple(use.evidence_ref for use in intent.evidence_uses)  # type: ignore[union-attr]
    try:
        material = resolver._sealed_material(sources=sources, intent=intent)  # noqa: SLF001
        evidence = tuple(
            ResolvedFactEvidenceV2.model_validate(item.model_dump(), strict=True)
            for item in material.evidence_refs
        )
        assertion = FactAssertionBindingV2.model_validate(
            material.assertion_binding.model_dump(), strict=True
        )
    except FactEvidenceResolutionError as exc:
        raise SealedFactCommitAdapterError(str(exc)) from exc
    except Exception as exc:
        raise SealedFactCommitAdapterError("proof-backed Fact sources are structurally invalid") from exc
    if tuple(item.ref_id for item in evidence) != expected_refs:
        raise SealedFactCommitAdapterError("proof-backed Fact sources do not exactly match intent evidence")
    if assertion.source_ref != intent.assertion_source_ref:
        raise SealedFactCommitAdapterError("proof-backed assertion does not match Fact assertion source")
    if assertion.asserted_subject_ref != intent.subject_ref:
        raise SealedFactCommitAdapterError("proof-backed assertion does not match Fact subject")
    uses_by_ref = {item.evidence_ref: item for item in intent.evidence_uses}
    proposal_by_ref = {item.ref_id: item for item in proposal.evidence_refs}
    for item in evidence:
        use = uses_by_ref.get(item.ref_id)
        proposed = proposal_by_ref.get(item.ref_id)
        if use is None or item.claim_purpose != use.purpose:
            raise SealedFactCommitAdapterError("proof-backed Fact source purpose does not match intent")
        if proposed is None:
            raise SealedFactCommitAdapterError("proof-backed Fact source is absent from sealed proposal")
        if (
            item.evidence_type != proposed.evidence_kind
            or item.source_world_revision != proposed.source_world_revision
            or item.immutable_hash != proposed.immutable_hash.removeprefix("sha256:")
        ):
            raise SealedFactCommitAdapterError(
                "proof-backed Fact source does not match sealed proposal evidence"
            )
    asserted = next(item for item in evidence if item.ref_id == assertion.source_ref)
    if assertion.source_kind != asserted.evidence_type:
        raise SealedFactCommitAdapterError("proof-backed assertion kind does not match its evidence")
    return evidence, assertion


__all__ = [
    "FactCommitPolicyResolutionV2",
    "SealedFactCommitAdapterError",
    "SealedFactCommitAdapterV2",
    "SealedFactCommitCompilationHandleV2",
]

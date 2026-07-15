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

from typing import Literal

from pydantic import Field, model_validator

from .fact_accepted_contracts import (
    FactAssertionBindingV2,
    FactCommitMaterializedPayloadV2,
    FactCommitValuesV2,
    ResolvedFactEvidenceV2,
    canonical_fact_commit_materialized_json,
    canonical_fact_commit_materialized_hash,
    fact_commit_transition_id_v2,
    rehydrate_fact_commit_intent_v2_json,
    rehydrate_fact_commit_materialized_v2,
)
from .fact_proof_backed_evidence import ResolvedFactCommitSourcesV2
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
    """Non-durable adapter-issued binding of one Fact change and policy result."""

    __slots__ = ("__authority", "__change", "__policy", "__proposal", "__world_id")

    def __init__(
        self,
        *,
        authority: object | None = None,
        proposal: FactCommitProposalEnvelopeV2 | None = None,
        change: FactCommitTypedChangeV2 | None = None,
        policy: FactCommitPolicyResolutionV2 | None = None,
        world_id: str | None = None,
    ) -> None:
        if authority is None or proposal is None or change is None or policy is None or world_id is None:
            raise TypeError("sealed Fact compilation handles are adapter-issued")
        self.__authority = authority
        self.__proposal = proposal
        self.__change = change
        self.__policy = policy
        self.__world_id = world_id

    def issued_by(self, authority: object) -> bool:
        return self.__authority is authority

    def inputs(
        self,
    ) -> tuple[FactCommitProposalEnvelopeV2, FactCommitTypedChangeV2, FactCommitPolicyResolutionV2, str]:
        return self.__proposal, self.__change, self.__policy, self.__world_id

    def __reduce__(self) -> object:
        raise TypeError("sealed Fact compilation handles cannot be serialized")

    def __copy__(self) -> object:
        raise TypeError("sealed Fact compilation handles cannot be copied")

    def __deepcopy__(self, memo: object) -> object:
        del memo
        raise TypeError("sealed Fact compilation handles cannot be copied")


class SealedFactCommitAdapterV2:
    """Deep, side-effect-free module for one sealed Fact commit compilation."""

    __slots__ = ("__authority",)

    def __init__(self) -> None:
        self.__authority = object()

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
        return SealedFactCommitCompilationHandleV2(
            authority=self.__authority,
            proposal=strict_proposal,
            change=strict_change,
            policy=strict_policy,
            world_id=world_id,
        )

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
        evidence, assertion = _strict_sources(sources=sources, intent= intent)
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
        if type(handle) is not SealedFactCommitCompilationHandleV2 or not handle.issued_by(
            self.__authority
        ):
            raise SealedFactCommitAdapterError("Fact compilation handle belongs to another adapter")
        return handle.inputs()


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
    *, sources: ResolvedFactCommitSourcesV2, intent: object
) -> tuple[tuple[ResolvedFactEvidenceV2, ...], FactAssertionBindingV2]:
    if type(sources) is not ResolvedFactCommitSourcesV2:
        raise SealedFactCommitAdapterError("Fact sources must come from the proof-backed resolver result")
    # ``intent`` is rehydrated immediately above; this guard prevents accepting
    # a duck-typed proposal object as a source of evidence ordering.
    expected_refs = tuple(use.evidence_ref for use in intent.evidence_uses)  # type: ignore[union-attr]
    try:
        evidence = tuple(
            ResolvedFactEvidenceV2.model_validate(item.model_dump(), strict=True)
            for item in sources.evidence_refs
        )
        assertion = FactAssertionBindingV2.model_validate(
            sources.assertion_binding.model_dump(), strict=True
        )
    except Exception as exc:
        raise SealedFactCommitAdapterError("proof-backed Fact sources are structurally invalid") from exc
    if tuple(item.ref_id for item in evidence) != expected_refs:
        raise SealedFactCommitAdapterError("proof-backed Fact sources do not exactly match intent evidence")
    if assertion.source_ref != intent.assertion_source_ref:
        raise SealedFactCommitAdapterError("proof-backed assertion does not match Fact assertion source")
    return evidence, assertion


__all__ = [
    "FactCommitPolicyResolutionV2",
    "SealedFactCommitAdapterError",
    "SealedFactCommitAdapterV2",
    "SealedFactCommitCompilationHandleV2",
]

from __future__ import annotations

from dataclasses import replace

import pytest

from companion_daemon.world_v2.fact_accepted_contracts import (
    FactAssertionBindingV2,
    ResolvedFactEvidenceV2,
)
from companion_daemon.world_v2.fact_proof_backed_evidence import ResolvedFactCommitSourcesV2
from companion_daemon.world_v2.proposal_envelope_v2 import (
    FactCommitProposalDraftV2,
    FactCommitProposalNormalizationContextV2,
    normalize_fact_commit_proposal_v2,
)
from companion_daemon.world_v2.sealed_fact_commit_adapter_v2 import (
    FactCommitPolicyResolutionV2,
    SealedFactCommitAdapterError,
    SealedFactCommitAdapterV2,
    SealedFactCommitCompilationHandleV2,
)


WORLD_ID = "world:sealed-fact"


def _proposal():
    return normalize_fact_commit_proposal_v2(
        draft=FactCommitProposalDraftV2.model_validate(
            {
                "fact_commit_intents": (
                    {
                        "subject_ref": "user:primary",
                        "predicate_code": "profile.display_name",
                        "value_ref": "value:alice",
                        "value_hash": "sha256:" + "a" * 64,
                        "assertion_source_ref": "observation:message:1",
                        "evidence_uses": (
                            {
                                "evidence_ref": "observation:message:1",
                                "purpose": "current_fact",
                                "anchor": True,
                            },
                        ),
                        "confidence_bp": 9100,
                        "privacy_class": "personal",
                    },
                ),
                "confidence": 9000,
                "brief_rationale": "the observation explicitly supplies a display name",
            },
            strict=True,
        ),
        context=FactCommitProposalNormalizationContextV2.model_validate(
            {
                "world_id": WORLD_ID,
                "proposal_id": "proposal:sealed-fact:1",
                "trigger_ref": "observation:message:1",
                "evaluated_world_revision": 12,
                "evidence_refs": (
                    {
                        "ref_id": "observation:message:1",
                        "evidence_kind": "observed_message",
                        "source_world_revision": 12,
                        "immutable_hash": "sha256:" + "b" * 64,
                    },
                ),
                "policy_refs": ("policy:fact-commit.2",),
            },
            strict=True,
        ),
    )


def _sources() -> ResolvedFactCommitSourcesV2:
    evidence = ResolvedFactEvidenceV2(
        ref_id="observation:message:1",
        evidence_type="observed_message",
        claim_purpose="current_fact",
        source_world_revision=12,
        immutable_hash="b" * 64,
    )
    return ResolvedFactCommitSourcesV2(
        evidence_refs=(evidence,),
        assertion_binding=FactAssertionBindingV2(
            source_kind="observed_message",
            source_ref="observation:message:1",
            asserted_subject_ref="user:primary",
            actor_ref="user:primary",
            channel="chat",
            payload_ref="payload:message:1",
            content_payload_hash="c" * 64,
        ),
    )


def _bound():
    proposal = _proposal()
    adapter = SealedFactCommitAdapterV2()
    handle = adapter.bind(
        proposal=proposal,
        change=proposal.proposed_changes[0],
        policy=FactCommitPolicyResolutionV2(
            cardinality="single", policy_refs=("policy:fact-commit.2",)
        ),
        world_id=WORLD_ID,
    )
    return adapter, handle


def test_compile_and_reverse_verify_proof_backed_fact_payload() -> None:
    adapter, handle = _bound()
    payload = adapter.compile(
        handle=handle, acceptance_id="acceptance:sealed:1", sources=_sources()
    )

    assert payload.payload_contract == "fact-commit-materialized.2"
    assert payload.fact_id.startswith("fact:")
    assert payload.values.cardinality == "single"
    assert payload.values.source_evidence_refs[0].immutable_hash == "b" * 64
    assert adapter.reverse_verify(
        handle=handle,
        acceptance_id="acceptance:sealed:1",
        sources=_sources(),
        payload=payload,
    ) == payload


def test_rejects_sources_not_exactly_resolved_for_the_change() -> None:
    adapter, handle = _bound()
    sources = _sources()
    wrong = replace(
        sources,
        evidence_refs=(
            ResolvedFactEvidenceV2(
                ref_id="observation:other",
                evidence_type="observed_message",
                claim_purpose="current_fact",
                source_world_revision=12,
                immutable_hash="b" * 64,
            ),
        ),
    )

    with pytest.raises(SealedFactCommitAdapterError, match="exactly match"):
        adapter.compile(handle=handle, acceptance_id="acceptance:sealed:1", sources=wrong)


def test_handle_is_issuer_bound_and_nonconstructible() -> None:
    adapter, handle = _bound()
    other = SealedFactCommitAdapterV2()
    with pytest.raises(SealedFactCommitAdapterError, match="another adapter"):
        other.compile(handle=handle, acceptance_id="acceptance:sealed:1", sources=_sources())
    with pytest.raises(TypeError, match="adapter-issued"):
        SealedFactCommitCompilationHandleV2()


def test_reverse_verifier_rejects_changed_canonical_payload() -> None:
    adapter, handle = _bound()
    payload = adapter.compile(
        handle=handle, acceptance_id="acceptance:sealed:1", sources=_sources()
    )
    tampered = payload.model_copy(update={"acceptance_id": "acceptance:other"})
    with pytest.raises(SealedFactCommitAdapterError, match="structurally invalid|does not match"):
        adapter.reverse_verify(
            handle=handle,
            acceptance_id="acceptance:sealed:1",
            sources=_sources(),
            payload=tampered,
        )

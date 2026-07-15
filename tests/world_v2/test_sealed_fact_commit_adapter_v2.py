from __future__ import annotations

from datetime import UTC, datetime

import pytest

from companion_daemon.world_v2.fact_accepted_contracts import (
    rehydrate_fact_commit_intent_v2_json,
)
from companion_daemon.world_v2.event_identity import domain_idempotency_key
from companion_daemon.world_v2.fact_proof_backed_evidence import (
    ProofBackedFactEvidenceResolverV2,
    ResolvedFactCommitSourcesV2,
)
from companion_daemon.world_v2.ledger import ObservationEventLocator
from companion_daemon.world_v2.proposal_envelope_v2 import (
    FactCommitProposalDraftV2,
    FactCommitProposalNormalizationContextV2,
    normalize_fact_commit_proposal_v2,
)
from companion_daemon.world_v2.schemas import ProjectionCursor, WorldEvent
from companion_daemon.world_v2.sealed_fact_commit_adapter_v2 import (
    FactCommitPolicyResolutionV2,
    SealedFactCommitAdapterError,
    SealedFactCommitAdapterV2,
    SealedFactCommitCompilationHandleV2,
)
from companion_daemon.world_v2.sqlite_ledger import (
    SQLiteProofBackedObservationReader,
    SQLiteWorldLedger,
)


WORLD_ID = "world:sealed-fact"
NOW = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)


def _proposal(*, event_payload_hash: str, source_world_revision: int):
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
                "evaluated_world_revision": source_world_revision,
                "evidence_refs": (
                    {
                        "ref_id": "observation:message:1",
                        "evidence_kind": "observed_message",
                        "source_world_revision": source_world_revision,
                        "immutable_hash": "sha256:" + event_payload_hash,
                    },
                ),
                "policy_refs": ("policy:fact-commit.2",),
            },
            strict=True,
        ),
    )


def _message() -> WorldEvent:
    observation_id = "observation:message:1"
    payload = {
        "schema_version": "world-v2.1",
        "observation_kind": "message",
        "observation_id": observation_id,
        "world_id": WORLD_ID,
        "logical_time": NOW.isoformat(),
        "created_at": NOW.isoformat(),
        "trace_id": "trace:sealed-fact",
        "causation_id": "cause:sealed-fact",
        "correlation_id": "correlation:sealed-fact",
        "source": "test",
        "source_event_id": "source:sealed-fact",
        "actor": "user:primary",
        "channel": "chat",
        "payload_ref": "payload:message:1",
        "payload_hash": "c" * 64,
        "received_at": NOW.isoformat(),
    }
    return WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id="event:sealed-fact",
        world_id=WORLD_ID,
        event_type="ObservationRecorded",
        logical_time=NOW,
        created_at=NOW,
        actor="user:primary",
        source="test",
        trace_id="trace:sealed-fact",
        causation_id="cause:sealed-fact",
        correlation_id="correlation:sealed-fact",
        idempotency_key=domain_idempotency_key(
            event_type="ObservationRecorded", world_id=WORLD_ID, payload=payload
        ) or "unreachable",
        payload=payload,
    )


def _cursor(ledger: SQLiteWorldLedger) -> ProjectionCursor:
    projection = ledger.project()
    return ProjectionCursor(
        world_revision=projection.world_revision,
        deliberation_revision=projection.deliberation_revision,
        ledger_sequence=projection.ledger_sequence,
    )


def _bound(tmp_path):
    ledger = SQLiteWorldLedger(path=tmp_path / "sealed.sqlite3", world_id=WORLD_ID)
    event = _message()
    ledger.commit((event,), expected_world_revision=0, expected_deliberation_revision=0)
    proposal = _proposal(event_payload_hash=event.payload_hash, source_world_revision=1)
    reader = SQLiteProofBackedObservationReader(ledger=ledger)
    resolver = ProofBackedFactEvidenceResolverV2(reader=reader)
    intent = rehydrate_fact_commit_intent_v2_json(proposal.proposed_changes[0].payload.canonical_json)
    sources = resolver.resolve(
        handle=reader.pin(world_id=WORLD_ID, cursor=_cursor(ledger)),
        intent=intent,
        locators=(
            ObservationEventLocator.for_message(
                world_id=WORLD_ID,
                observation_id="observation:message:1",
                source="test",
                source_event_id="source:sealed-fact",
            ),
        ),
    )
    adapter = SealedFactCommitAdapterV2(resolver=resolver)
    handle = adapter.bind(
        proposal=proposal,
        change=proposal.proposed_changes[0],
        policy=FactCommitPolicyResolutionV2(
            cardinality="single", policy_refs=("policy:fact-commit.2",)
        ),
        world_id=WORLD_ID,
    )
    return adapter, handle, sources, resolver, ledger


def test_compile_and_reverse_verify_proof_backed_fact_payload(tmp_path) -> None:
    adapter, handle, sources, _, ledger = _bound(tmp_path)
    payload = adapter.compile(
        handle=handle, acceptance_id="acceptance:sealed:1", sources=sources
    )

    assert payload.payload_contract == "fact-commit-materialized.2"
    assert payload.fact_id.startswith("fact:")
    assert payload.values.cardinality == "single"
    assert payload.values.source_evidence_refs[0].immutable_hash
    assert adapter.reverse_verify(
        handle=handle,
        acceptance_id="acceptance:sealed:1",
        sources=sources,
        payload=payload,
    ) == payload
    ledger.close()


def test_rejects_forged_or_other_resolver_source_capability(tmp_path) -> None:
    adapter, handle, sources, _, ledger = _bound(tmp_path)
    with pytest.raises(SealedFactCommitAdapterError, match="not owned|proof-backed resolver"):
        adapter.compile(
            handle=handle,
            acceptance_id="acceptance:sealed:1",
            sources=ResolvedFactCommitSourcesV2(),
        )
    assert adapter.compile(
        handle=handle, acceptance_id="acceptance:sealed:1", sources=sources
    ).fact_id
    ledger.close()


def test_handle_is_issuer_bound_and_blank_handles_are_unowned(tmp_path) -> None:
    adapter, handle, sources, resolver, ledger = _bound(tmp_path)
    other = SealedFactCommitAdapterV2(resolver=resolver)
    with pytest.raises(SealedFactCommitAdapterError, match="another adapter"):
        other.compile(handle=handle, acceptance_id="acceptance:sealed:1", sources=sources)
    with pytest.raises(SealedFactCommitAdapterError, match="another adapter"):
        adapter.compile(
            handle=SealedFactCommitCompilationHandleV2(),
            acceptance_id="acceptance:sealed:1",
            sources=sources,
        )
    ledger.close()


def test_reverse_verifier_rejects_changed_canonical_payload(tmp_path) -> None:
    adapter, handle, sources, _, ledger = _bound(tmp_path)
    payload = adapter.compile(
        handle=handle, acceptance_id="acceptance:sealed:1", sources=sources
    )
    tampered = payload.model_copy(update={"acceptance_id": "acceptance:other"})
    with pytest.raises(SealedFactCommitAdapterError, match="structurally invalid|does not match"):
        adapter.reverse_verify(
            handle=handle,
            acceptance_id="acceptance:sealed:1",
            sources=sources,
            payload=tampered,
        )
    ledger.close()

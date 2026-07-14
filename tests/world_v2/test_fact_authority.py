from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
import sqlite3

import pytest

from companion_daemon.world_v2.errors import LedgerIntegrityError
from companion_daemon.world_v2.event_identity import domain_idempotency_key
from companion_daemon.world_v2.fact_events import FactChangedPayload, fact_mutation_hash
from companion_daemon.world_v2.ledger import WorldLedger
from companion_daemon.world_v2.reducers import ReducerState
from companion_daemon.world_v2.schemas import (
    EvidenceRef,
    FactAssertionBinding,
    FactOrigin,
    FactProjection,
    FactProposalProjection,
    FactProposedMutation,
    FactValues,
    WorldEvent,
    fact_conflict_key,
    fact_semantic_fingerprint,
)
from companion_daemon.world_v2.sqlite_ledger import SQLiteWorldLedger


NOW = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)
WORLD = "world-fact-authority"
POLICY = ("policy:fact-v1",)


def event(event_id: str, event_type: str, payload: dict[str, object]) -> WorldEvent:
    return WorldEvent.from_payload(
        schema_version="world-v2.1", event_id=event_id, world_id=WORLD,
        event_type=event_type, logical_time=NOW, created_at=NOW,
        actor="system:test", source="test", trace_id="trace:fact",
        causation_id=f"cause:{event_id}", correlation_id="correlation:fact",
        idempotency_key=domain_idempotency_key(
            event_type=event_type, world_id=WORLD, payload=payload
        ) or f"identity:{event_id}", payload=payload,
    )


def operator(ref_id: str = "operator:fact", digest: str = "a" * 64) -> EvidenceRef:
    return EvidenceRef(
        ref_id=ref_id, evidence_type="operator_observation",
        claim_purpose="current_fact", immutable_hash=digest,
    )


def message_evidence(
    ref_id: str = "message:fact", *, world_revision: int, event_hash: str
) -> EvidenceRef:
    return EvidenceRef(
        ref_id=ref_id,
        evidence_type="observed_message",
        claim_purpose="current_fact",
        source_world_revision=world_revision,
        immutable_hash=event_hash,
    )


def committed_fact_evidence(ledger, value: FactProjection) -> EvidenceRef:
    authority = next(
        item
        for item in ledger.project().committed_world_event_refs
        if item.event_id == value.origin.accepted_event_ref
    )
    encoded = json.dumps(
        value.values.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return EvidenceRef(
        ref_id=authority.event_id,
        evidence_type="committed_fact",
        claim_purpose="current_fact",
        source_world_revision=authority.world_revision,
        immutable_hash=hashlib.sha256(encoded).hexdigest(),
    )


def fact(
    *, fact_id: str = "fact:user-location", revision: int = 1,
    value_ref: str = "value:user-location:chengdu", value_hash: str = "c" * 64,
    status: str = "active", sources: tuple[EvidenceRef, ...] | None = None,
    transition_id: str | None = None, withdrawal_reason_code: str | None = None,
    withdrawal_evidence_ref: str | None = None,
    predicate_code: str = "location.current", cardinality: str = "single",
    binding: FactAssertionBinding | None = None,
    anchors: tuple[EvidenceRef, ...] | None = None,
    confidence_bp: int = 9000, privacy_class: str = "private",
) -> FactProjection:
    refs = sources or (operator(),)
    anchors = anchors or (operator(),)
    binding = binding or FactAssertionBinding(
        source_kind="operator_observation", source_ref="operator:fact",
        asserted_subject_ref="subject:user", content_payload_hash="a" * 64,
    )
    values = FactValues(
        subject_ref="subject:user", predicate_code=predicate_code,
        cardinality=cardinality, conflict_key=fact_conflict_key(
            subject_ref="subject:user", predicate_code=predicate_code
        ),
        value_ref=value_ref, value_hash=value_hash,
        assertion_binding=binding,
        anchor_evidence_refs=anchors, source_evidence_refs=refs,
        confidence_bp=confidence_bp, privacy_class=privacy_class, status=status,
        withdrawal_reason_code=withdrawal_reason_code,
        withdrawal_evidence_ref=withdrawal_evidence_ref,
    )
    transition_id = transition_id or f"transition:{fact_id}:{revision}"
    origin = FactOrigin(
        change_id=f"change:{transition_id}", transition_id=transition_id,
        policy_refs=POLICY, accepted_event_ref=f"event:{transition_id}",
    )
    return FactProjection(
        fact_id=fact_id, entity_revision=revision,
        semantic_fingerprint=fact_semantic_fingerprint(
            subject_ref=values.subject_ref, predicate_code=values.predicate_code,
            cardinality=values.cardinality, conflict_key=values.conflict_key,
            value_hash=values.value_hash,
            assertion_binding=values.assertion_binding,
            anchor_evidence_refs=values.anchor_evidence_refs,
            policy_refs=origin.policy_refs,
        ), values=values, origin=origin, committed_at=NOW, updated_at=NOW,
    )


def mutation(
    *, operation: str, before: FactProjection | None, after: FactProjection,
    proposal_id: str, evaluated_world_revision: int,
    compensates_transition_id: str | None = None,
) -> FactChangedPayload:
    raw = {
        "change_id": after.origin.change_id,
        "transition_id": after.origin.transition_id,
        "expected_entity_revision": before.entity_revision if before else 0,
        "evidence_refs": after.values.source_evidence_refs,
        "policy_refs": POLICY,
        "acceptance_id": f"acceptance:{proposal_id}", "proposal_id": proposal_id,
        "evaluated_world_revision": evaluated_world_revision,
        "accepted_change_hash": "0" * 64, "operation": operation,
        "fact_before": before, "fact_after": after,
        "compensates_transition_id": compensates_transition_id,
    }
    raw["accepted_change_hash"] = fact_mutation_hash(raw)
    return FactChangedPayload.model_validate(raw)


def proposal(value: FactChangedPayload) -> FactProposalProjection:
    event_type = {
        "commit": "FactCommitted", "correct": "FactCorrected",
        "withdraw": "FactWithdrawn", "compensate": "FactCorrectionCompensated",
    }[value.operation]
    return FactProposalProjection(
        proposal_id=value.proposal_id, proposal_encoding="typed-authority-v1",
        authority_contract_ref="proposal-contract:fact.1",
        transition_kind=value.operation, change_id=value.change_id,
        transition_id=value.transition_id,
        evaluated_world_revision=value.evaluated_world_revision,
        expected_entity_revision=value.expected_entity_revision,
        proposed_change_hash=value.accepted_change_hash,
        evidence_refs=value.evidence_refs, policy_refs=value.policy_refs,
        proposed_mutation=FactProposedMutation(
            event_type=event_type,
            payload_json=json.dumps(value.model_dump(mode="json"), ensure_ascii=False,
                                    sort_keys=True, separators=(",", ":")),
        ),
    )


def initialized(kind=WorldLedger.in_memory):
    ledger = kind(world_id=WORLD)
    ledger.commit([event("world:start", "WorldStarted", {})],
                  expected_world_revision=0, expected_deliberation_revision=0)
    ledger.commit([event("clock:start", "ClockAdvanced", {
        "logical_time_from": "2026-07-15T11:59:00+00:00",
        "logical_time_to": NOW.isoformat(),
    })], expected_world_revision=1, expected_deliberation_revision=0)
    ledger.commit([event("operator:fact", "OperatorObservationRecorded", {
        "observation_id": "operator:fact", "observation_hash": "a" * 64,
    })], expected_world_revision=2, expected_deliberation_revision=0)
    return ledger


def record_message(
    ledger,
    *,
    observation_id: str = "message:fact",
    event_id: str | None = None,
) -> None:
    event_id = event_id or observation_id
    payload = {
        "schema_version": "world-v2.1",
        "observation_kind": "message",
        "observation_id": observation_id,
        "world_id": WORLD,
        "logical_time": NOW.isoformat(),
        "created_at": NOW.isoformat(),
        "trace_id": "trace:fact",
        "causation_id": f"cause:{observation_id}",
        "correlation_id": "correlation:fact",
        "source": "test",
        "source_event_id": f"source:{observation_id}",
        "actor": "user:primary",
        "channel": "chat",
        "payload_ref": f"payload:{observation_id}",
        "payload_hash": "b" * 64,
        "received_at": NOW.isoformat(),
    }
    projected = ledger.project()
    message_event = WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id=event_id,
        world_id=WORLD,
        event_type="ObservationRecorded",
        logical_time=NOW,
        created_at=NOW,
        actor="user:primary",
        source="test",
        trace_id="trace:fact",
        causation_id=f"cause:{observation_id}",
        correlation_id="correlation:fact",
        idempotency_key=domain_idempotency_key(
            event_type="ObservationRecorded", world_id=WORLD, payload=payload
        ) or f"identity:{observation_id}",
        payload=payload,
    )
    ledger.commit(
        [message_event],
        expected_world_revision=projected.world_revision,
        expected_deliberation_revision=projected.deliberation_revision,
    )


def record_accept_mutate(ledger, value: FactChangedPayload) -> None:
    projected = ledger.project()
    ledger.commit([event(f"event:{value.proposal_id}", "ProposalRecorded",
                         proposal(value).model_dump(mode="json"))],
                  expected_world_revision=projected.world_revision,
                  expected_deliberation_revision=projected.deliberation_revision)
    projected = ledger.project()
    acceptance = {
        "acceptance_id": value.acceptance_id, "status": "accepted",
        "proposal_id": value.proposal_id,
        "evaluated_world_revision": value.evaluated_world_revision,
        "accepted_change_id": value.change_id,
        "accepted_change_hash": value.accepted_change_hash,
    }
    ledger.commit([
        event(f"event:{value.acceptance_id}", "AcceptanceRecorded", acceptance),
        event(value.fact_after.origin.accepted_event_ref,
              proposal(value).proposed_mutation.event_type,
              value.model_dump(mode="json")),
    ], expected_world_revision=projected.world_revision,
       expected_deliberation_revision=projected.deliberation_revision)


def test_fact_commit_is_typed_persistent_and_zero_cascade() -> None:
    ledger = initialized()
    value = mutation(operation="commit", before=None, after=fact(),
                     proposal_id="proposal:fact-commit", evaluated_world_revision=2)
    record_accept_mutate(ledger, value)
    projected = ledger.project()
    assert projected.facts == (value.fact_after,)
    assert len(projected.fact_transitions) == 1
    assert projected.actions == projected.experiences == projected.threads == ()
    assert projected.commitments == projected.affect_episodes == projected.relationship_states == ()


def test_fact_rejects_duplicate_active_semantics_and_single_value_conflict() -> None:
    ledger = initialized()
    first = mutation(operation="commit", before=None, after=fact(),
                     proposal_id="proposal:first", evaluated_world_revision=2)
    record_accept_mutate(ledger, first)
    duplicate = fact(fact_id="fact:duplicate", transition_id="transition:duplicate")
    raw = mutation(operation="commit", before=None, after=duplicate,
                   proposal_id="proposal:duplicate",
                   evaluated_world_revision=ledger.project().world_revision)
    projected = ledger.project()
    with pytest.raises(ValueError, match="semantic fingerprint|conflict key"):
        ledger.commit([event("event:proposal:duplicate", "ProposalRecorded",
                             proposal(raw).model_dump(mode="json"))],
                      expected_world_revision=projected.world_revision,
                      expected_deliberation_revision=projected.deliberation_revision)


def test_fact_message_authority_binds_the_whole_retained_envelope() -> None:
    ledger = initialized()
    record_message(ledger)
    observed = ledger.project().message_observations[-1]
    evidence = message_evidence(
        world_revision=observed.world_revision,
        event_hash=observed.event_payload_hash,
    )
    binding = FactAssertionBinding(
        source_kind="observed_message",
        source_ref=observed.observation_id,
        asserted_subject_ref="subject:user",
        actor_ref=observed.actor,
        channel=observed.channel,
        payload_ref=observed.payload_ref,
        content_payload_hash=observed.content_payload_hash,
    )
    value = mutation(
        operation="commit",
        before=None,
        after=fact(
            fact_id="fact:message-location",
            transition_id="transition:message-location",
            sources=(evidence,),
            anchors=(evidence,),
            binding=binding,
            privacy_class="personal",
        ),
        proposal_id="proposal:message-location",
        evaluated_world_revision=len(ledger.project().committed_world_event_refs),
    )
    record_accept_mutate(ledger, value)
    assert ledger.project().facts[-1] == value.fact_after

    wrong = binding.model_copy(update={"channel": "voice"})
    rejected = mutation(
        operation="commit",
        before=None,
        after=fact(
            fact_id="fact:wrong-envelope",
            transition_id="transition:wrong-envelope",
            sources=(evidence,),
            anchors=(evidence,),
            binding=wrong,
                privacy_class="personal",
                value_hash="2" * 64,
                predicate_code="profile.display_name",
        ),
        proposal_id="proposal:wrong-envelope",
        evaluated_world_revision=len(ledger.project().committed_world_event_refs),
    )
    projected = ledger.project()
    with pytest.raises(ValueError, match="observed message provenance"):
        ledger.commit(
            [
                event(
                    "event:proposal:wrong-envelope",
                    "ProposalRecorded",
                    proposal(rejected).model_dump(mode="json"),
                )
            ],
            expected_world_revision=projected.world_revision,
            expected_deliberation_revision=projected.deliberation_revision,
        )


def test_fact_cardinality_is_installed_authority_and_set_content_is_unique() -> None:
    ledger = initialized()
    invalid = fact(
        fact_id="fact:false-set",
        transition_id="transition:false-set",
        cardinality="set",
    )
    value = mutation(
        operation="commit",
        before=None,
        after=invalid,
        proposal_id="proposal:false-set",
        evaluated_world_revision=len(ledger.project().committed_world_event_refs),
    )
    projected = ledger.project()
    with pytest.raises(ValueError, match="installed predicate authority"):
        ledger.commit(
            [event("event:proposal:false-set", "ProposalRecorded", proposal(value).model_dump(mode="json"))],
            expected_world_revision=projected.world_revision,
            expected_deliberation_revision=projected.deliberation_revision,
        )

    first = mutation(
        operation="commit",
        before=None,
        after=fact(
            fact_id="fact:likes-tea",
            transition_id="transition:likes-tea",
            predicate_code="preference.likes",
            cardinality="set",
            value_ref="value:tea",
            value_hash="e" * 64,
        ),
        proposal_id="proposal:likes-tea",
        evaluated_world_revision=len(ledger.project().committed_world_event_refs),
    )
    record_accept_mutate(ledger, first)
    duplicate = mutation(
        operation="commit",
        before=None,
        after=fact(
            fact_id="fact:likes-tea-again",
            transition_id="transition:likes-tea-again",
            predicate_code="preference.likes",
            cardinality="set",
            value_ref="value:tea:alias",
            value_hash="e" * 64,
        ),
        proposal_id="proposal:likes-tea-again",
        evaluated_world_revision=len(ledger.project().committed_world_event_refs),
    )
    projected = ledger.project()
    with pytest.raises(ValueError, match="semantic fingerprint|content identity"):
        ledger.commit(
            [event("event:proposal:likes-tea-again", "ProposalRecorded", proposal(duplicate).model_dump(mode="json"))],
            expected_world_revision=projected.world_revision,
            expected_deliberation_revision=projected.deliberation_revision,
        )


def test_fact_correction_requires_current_head_and_new_bound_assertion() -> None:
    ledger = initialized()
    initial = mutation(
        operation="commit",
        before=None,
        after=fact(),
        proposal_id="proposal:correction-base",
        evaluated_world_revision=len(ledger.project().committed_world_event_refs),
    )
    record_accept_mutate(ledger, initial)
    projected = ledger.project()
    ledger.commit(
        [event("operator:correction", "OperatorObservationRecorded", {
            "observation_id": "operator:correction", "observation_hash": "d" * 64,
        })],
        expected_world_revision=projected.world_revision,
        expected_deliberation_revision=projected.deliberation_revision,
    )
    head = committed_fact_evidence(ledger, initial.fact_after)
    correction_source = operator("operator:correction", "d" * 64)
    sources = (*initial.fact_after.values.source_evidence_refs, head, correction_source)
    corrected = fact(
        revision=2,
        value_ref="value:user-location:shanghai",
        value_hash="f" * 64,
        sources=sources,
        anchors=initial.fact_after.values.anchor_evidence_refs,
        binding=FactAssertionBinding(
            source_kind="operator_observation",
            source_ref="operator:correction",
            asserted_subject_ref="subject:user",
            content_payload_hash="d" * 64,
        ),
        transition_id="transition:correction",
    )
    good = mutation(
        operation="correct",
        before=initial.fact_after,
        after=corrected,
        proposal_id="proposal:correction",
        evaluated_world_revision=len(ledger.project().committed_world_event_refs),
    )
    record_accept_mutate(ledger, good)
    assert ledger.project().facts[0] == corrected

    stale_sources = (
        *corrected.values.source_evidence_refs,
        operator("operator:correction-2", "9" * 64),
    )
    projected = ledger.project()
    ledger.commit(
        [event("operator:correction-2", "OperatorObservationRecorded", {
            "observation_id": "operator:correction-2", "observation_hash": "9" * 64,
        })],
        expected_world_revision=projected.world_revision,
        expected_deliberation_revision=projected.deliberation_revision,
    )
    stale = fact(
        revision=3,
        value_ref="value:user-location:beijing",
        value_hash="1" * 64,
        sources=stale_sources,
        anchors=corrected.values.anchor_evidence_refs,
        binding=FactAssertionBinding(
            source_kind="operator_observation",
            source_ref="operator:correction-2",
            asserted_subject_ref="subject:user",
            content_payload_hash="9" * 64,
        ),
        transition_id="transition:stale-correction",
    )
    rejected = mutation(
        operation="correct",
        before=corrected,
        after=stale,
        proposal_id="proposal:stale-correction",
        evaluated_world_revision=len(ledger.project().committed_world_event_refs),
    )
    projected = ledger.project()
    with pytest.raises(ValueError, match="exact prior committed-fact"):
        ledger.commit(
            [event("event:proposal:stale-correction", "ProposalRecorded", proposal(rejected).model_dump(mode="json"))],
            expected_world_revision=projected.world_revision,
            expected_deliberation_revision=projected.deliberation_revision,
        )


def test_fact_withdrawal_freezes_claim_and_closes_authority() -> None:
    ledger = initialized()
    initial = mutation(
        operation="commit",
        before=None,
        after=fact(),
        proposal_id="proposal:withdraw-base",
        evaluated_world_revision=len(ledger.project().committed_world_event_refs),
    )
    record_accept_mutate(ledger, initial)
    projected = ledger.project()
    ledger.commit(
        [event("operator:withdraw", "OperatorObservationRecorded", {
            "observation_id": "operator:withdraw", "observation_hash": "8" * 64,
        })],
        expected_world_revision=projected.world_revision,
        expected_deliberation_revision=projected.deliberation_revision,
    )
    head = committed_fact_evidence(ledger, initial.fact_after)
    withdrawal_source = operator("operator:withdraw", "8" * 64)
    sources = (*initial.fact_after.values.source_evidence_refs, head, withdrawal_source)
    bad = fact(
        revision=2,
        status="withdrawn",
        sources=sources,
        anchors=initial.fact_after.values.anchor_evidence_refs,
        binding=initial.fact_after.values.assertion_binding,
        confidence_bp=100,
        transition_id="transition:bad-withdraw",
        withdrawal_reason_code="user_request",
        withdrawal_evidence_ref="operator:withdraw",
    )
    rejected = mutation(
        operation="withdraw",
        before=initial.fact_after,
        after=bad,
        proposal_id="proposal:bad-withdraw",
        evaluated_world_revision=len(ledger.project().committed_world_event_refs),
    )
    projected = ledger.project()
    with pytest.raises(ValueError, match="freeze claim content"):
        ledger.commit(
            [event("event:proposal:bad-withdraw", "ProposalRecorded", proposal(rejected).model_dump(mode="json"))],
            expected_world_revision=projected.world_revision,
            expected_deliberation_revision=projected.deliberation_revision,
        )

    withdrawn = fact(
        revision=2,
        status="withdrawn",
        sources=sources,
        anchors=initial.fact_after.values.anchor_evidence_refs,
        binding=initial.fact_after.values.assertion_binding,
        transition_id="transition:withdraw",
        withdrawal_reason_code="user_request",
        withdrawal_evidence_ref="operator:withdraw",
    )
    accepted = mutation(
        operation="withdraw",
        before=initial.fact_after,
        after=withdrawn,
        proposal_id="proposal:withdraw",
        evaluated_world_revision=len(ledger.project().committed_world_event_refs),
    )
    record_accept_mutate(ledger, accepted)
    assert ledger.project().facts[0].values.status == "withdrawn"


def test_fact_correction_compensation_restores_exact_latest_before_image(tmp_path) -> None:
    path = tmp_path / "fact-compensation.sqlite3"
    ledger = initialized(
        lambda *, world_id: SQLiteWorldLedger(path=path, world_id=world_id)
    )
    initial = mutation(
        operation="commit",
        before=None,
        after=fact(),
        proposal_id="proposal:compensate-base",
        evaluated_world_revision=len(ledger.project().committed_world_event_refs),
    )
    record_accept_mutate(ledger, initial)
    projected = ledger.project()
    ledger.commit(
        [event("operator:compensate-correction", "OperatorObservationRecorded", {
            "observation_id": "operator:compensate-correction",
            "observation_hash": "7" * 64,
        })],
        expected_world_revision=projected.world_revision,
        expected_deliberation_revision=projected.deliberation_revision,
    )
    corrected = fact(
        revision=2,
        value_ref="value:user-location:hangzhou",
        value_hash="6" * 64,
        sources=(
            *initial.fact_after.values.source_evidence_refs,
            committed_fact_evidence(ledger, initial.fact_after),
            operator("operator:compensate-correction", "7" * 64),
        ),
        anchors=initial.fact_after.values.anchor_evidence_refs,
        binding=FactAssertionBinding(
            source_kind="operator_observation",
            source_ref="operator:compensate-correction",
            asserted_subject_ref="subject:user",
            content_payload_hash="7" * 64,
        ),
        transition_id="transition:compensate-correction",
    )
    correction = mutation(
        operation="correct",
        before=initial.fact_after,
        after=corrected,
        proposal_id="proposal:compensate-correction",
        evaluated_world_revision=len(ledger.project().committed_world_event_refs),
    )
    record_accept_mutate(ledger, correction)

    restored = fact(revision=3, transition_id="transition:compensate")
    compensation = mutation(
        operation="compensate",
        before=corrected,
        after=restored,
        proposal_id="proposal:compensate",
        evaluated_world_revision=len(ledger.project().committed_world_event_refs),
        compensates_transition_id=corrected.origin.transition_id,
    )
    record_accept_mutate(ledger, compensation)
    assert ledger.project().facts[0].values == initial.fact_after.values
    assert ledger.project().fact_transitions[-1].operation == "compensate"
    assert (
        ledger.project().fact_transitions[-1].compensates_transition_id
        == corrected.origin.transition_id
    )
    expected = ledger.project()
    ledger.close()
    reopened = SQLiteWorldLedger(path=path, world_id=WORLD)
    assert reopened.project() == expected
    reopened.close()
    with sqlite3.connect(path) as connection:
        raw = json.loads(connection.execute(
            "SELECT state_json FROM world_v2_heads WHERE world_id = ?", (WORLD,)
        ).fetchone()[0])
        raw["fact_transitions"][-1]["compensates_transition_id"] = "transition:missing"
        connection.execute(
            "UPDATE world_v2_heads SET state_json = ? WHERE world_id = ?",
            (json.dumps(raw, separators=(",", ":")), WORLD),
        )
    with pytest.raises(LedgerIntegrityError):
        SQLiteWorldLedger(path=path, world_id=WORLD)


def test_fact_privacy_and_cross_type_anchor_fail_closed() -> None:
    ledger = initialized()
    too_broad = mutation(
        operation="commit",
        before=None,
        after=fact(privacy_class="personal"),
        proposal_id="proposal:too-broad",
        evaluated_world_revision=len(ledger.project().committed_world_event_refs),
    )
    projected = ledger.project()
    with pytest.raises(ValueError, match="privacy matrix"):
        ledger.commit(
            [event("event:proposal:too-broad", "ProposalRecorded", proposal(too_broad).model_dump(mode="json"))],
            expected_world_revision=projected.world_revision,
            expected_deliberation_revision=projected.deliberation_revision,
        )


def test_fact_correction_rejects_another_facts_valid_head() -> None:
    ledger = initialized()
    first = mutation(
        operation="commit",
        before=None,
        after=fact(),
        proposal_id="proposal:cross-head-first",
        evaluated_world_revision=len(ledger.project().committed_world_event_refs),
    )
    record_accept_mutate(ledger, first)
    second = mutation(
        operation="commit",
        before=None,
        after=fact(
            fact_id="fact:cross-head-second",
            transition_id="transition:cross-head-second",
            predicate_code="preference.likes",
            cardinality="set",
            value_ref="value:coffee",
            value_hash="4" * 64,
        ),
        proposal_id="proposal:cross-head-second",
        evaluated_world_revision=len(ledger.project().committed_world_event_refs),
    )
    record_accept_mutate(ledger, second)
    projected = ledger.project()
    ledger.commit(
        [event("operator:cross-head", "OperatorObservationRecorded", {
            "observation_id": "operator:cross-head", "observation_hash": "3" * 64,
        })],
        expected_world_revision=projected.world_revision,
        expected_deliberation_revision=projected.deliberation_revision,
    )
    wrong_head = committed_fact_evidence(ledger, second.fact_after)
    after = fact(
        revision=2,
        value_ref="value:user-location:wuhan",
        value_hash="5" * 64,
        sources=(
            *first.fact_after.values.source_evidence_refs,
            wrong_head,
            operator("operator:cross-head", "3" * 64),
        ),
        anchors=first.fact_after.values.anchor_evidence_refs,
        binding=FactAssertionBinding(
            source_kind="operator_observation",
            source_ref="operator:cross-head",
            asserted_subject_ref="subject:user",
            content_payload_hash="3" * 64,
        ),
        transition_id="transition:cross-head",
    )
    rejected = mutation(
        operation="correct",
        before=first.fact_after,
        after=after,
        proposal_id="proposal:cross-head",
        evaluated_world_revision=len(ledger.project().committed_world_event_refs),
    )
    projected = ledger.project()
    with pytest.raises(ValueError, match="exact prior committed-fact"):
        ledger.commit(
            [event("event:proposal:cross-head", "ProposalRecorded", proposal(rejected).model_dump(mode="json"))],
            expected_world_revision=projected.world_revision,
            expected_deliberation_revision=projected.deliberation_revision,
        )


def test_fact_commit_rejects_same_ref_from_wrong_anchor_type() -> None:
    ledger = initialized()
    record_message(
        ledger,
        observation_id="operator:fact",
        event_id="event:message:same-ref",
    )
    observed = ledger.project().message_observations[-1]
    message_ref = message_evidence(
        "operator:fact",
        world_revision=observed.world_revision,
        event_hash=observed.event_payload_hash,
    )
    cross_type = mutation(
        operation="commit",
        before=None,
        after=fact(
            fact_id="fact:cross-type-anchor",
            transition_id="transition:cross-type-anchor",
            sources=(operator(), message_ref),
            anchors=(message_ref,),
        ),
        proposal_id="proposal:cross-type-anchor",
        evaluated_world_revision=len(ledger.project().committed_world_event_refs),
    )
    projected = ledger.project()
    with pytest.raises(ValueError, match="canonical anchor evidence"):
        ledger.commit(
            [event("event:proposal:cross-type-anchor", "ProposalRecorded", proposal(cross_type).model_dump(mode="json"))],
            expected_world_revision=projected.world_revision,
            expected_deliberation_revision=projected.deliberation_revision,
        )


def test_sqlite_fact_roundtrip_rebuild_tamper_and_v11_migration(tmp_path) -> None:
    path = tmp_path / "fact.sqlite3"
    ledger = initialized(lambda *, world_id: SQLiteWorldLedger(path=path, world_id=world_id))
    value = mutation(operation="commit", before=None, after=fact(),
                     proposal_id="proposal:sqlite", evaluated_world_revision=2)
    record_accept_mutate(ledger, value)
    expected = ledger.project()
    ledger.close()
    reopened = SQLiteWorldLedger(path=path, world_id=WORLD)
    assert reopened.project() == expected
    assert reopened.rebuild() == expected
    reopened.close()
    with sqlite3.connect(path) as connection:
        raw = json.loads(connection.execute(
            "SELECT state_json FROM world_v2_heads WHERE world_id = ?", (WORLD,)
        ).fetchone()[0])
        raw["facts"][0]["values"]["value_hash"] = "0" * 64
        connection.execute("UPDATE world_v2_heads SET state_json = ? WHERE world_id = ?",
                           (json.dumps(raw, separators=(",", ":")), WORLD))
    with pytest.raises(LedgerIntegrityError):
        SQLiteWorldLedger(path=path, world_id=WORLD)

    legacy_path = tmp_path / "fact-v11.sqlite3"
    legacy = SQLiteWorldLedger(path=legacy_path, world_id=WORLD)
    legacy.commit([event("world:legacy", "WorldStarted", {})],
                  expected_world_revision=0, expected_deliberation_revision=0)
    record_message(legacy, observation_id="message:legacy")
    legacy.close()
    with sqlite3.connect(legacy_path) as connection:
        state_json = connection.execute(
            "SELECT state_json FROM world_v2_heads WHERE world_id = ?", (WORLD,)
        ).fetchone()[0]
        raw_state = json.loads(state_json)
        retained = raw_state["message_observations"][0]
        for field in ("actor", "channel", "payload_ref"):
            retained.pop(field)
        state = ReducerState.model_validate_json(json.dumps(raw_state, separators=(",", ":")))
        semantic = state.semantic_payload(
            world_id=WORLD, world_revision=2,
            reducer_bundle_version="world-v2-reducers.11",
        )
        digest = hashlib.sha256(json.dumps(
            semantic, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()).hexdigest()
        connection.execute(
            "UPDATE world_v2_heads SET state_json = ?, semantic_hash = ?, reducer_bundle_version = ? "
            "WHERE world_id = ?",
            (
                json.dumps(raw_state, ensure_ascii=False, separators=(",", ":")),
                digest,
                "world-v2-reducers.11",
                WORLD,
            ),
        )
    migrated = SQLiteWorldLedger(path=legacy_path, world_id=WORLD)
    assert migrated.project().reducer_bundle_version == "world-v2-reducers.15"
    assert migrated.project().facts == ()
    migrated_message = migrated.project().message_observations[0]
    assert (migrated_message.actor, migrated_message.channel, migrated_message.payload_ref) == (
        "user:primary", "chat", "payload:message:legacy",
    )
    migrated.close()

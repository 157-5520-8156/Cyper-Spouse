from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import json
import sqlite3

import pytest

from legacy_migration_support import strip_v16_state_fields

from companion_daemon.world_v2.event_identity import domain_idempotency_key
from companion_daemon.world_v2.fact_events import FactChangedPayload, fact_mutation_hash
from companion_daemon.world_v2.ledger import WorldLedger
from companion_daemon.world_v2.memory_events import (
    MemoryCandidateChangedPayload,
    MemoryClockForgetAuthority,
    MemoryCompressionForgetAuthority,
    MemoryDeliberativeForgetAuthority,
    MemoryEvidenceForgetAuthority,
    memory_forget_scope_hash,
    memory_candidate_mutation_hash,
    memory_source_evidence,
)
from companion_daemon.world_v2.memory_reducers import (
    MEMORY_POLICY_DIGEST,
    MEMORY_POLICY_REFS,
    MEMORY_POLICY_VERSION,
    evaluate_memory_retrieval,
    reduce_memory_candidate,
)
from companion_daemon.world_v2.schemas import (
    CommittedWorldEventRef,
    EvidenceRef,
    ExperienceExecutionReceiptBinding,
    ExperienceOrigin,
    ExperienceProjection,
    ExperienceTransitionProjection,
    ExperienceValues,
    FactAssertionBinding,
    FactOrigin,
    FactProposalProjection,
    FactProposedMutation,
    FactProjection,
    FactTransitionProjection,
    FactValues,
    MEMORY_SALIENCE_MATRIX_DIGEST,
    MemoryCandidateOrigin,
    MemoryCandidateProjection,
    MemoryCandidateProposedMutation,
    MemoryCandidateProposalProjection,
    MemoryCandidateValues,
    MemorySalienceVector,
    MemorySourceBinding,
    LegacyExperienceEvidenceRef,
    LegacyExperienceProjection,
    ThreadOrigin,
    ThreadProjection,
    ThreadTransitionProjection,
    ThreadValues,
    WorldEvent,
    experience_semantic_fingerprint,
    fact_conflict_key,
    fact_semantic_fingerprint,
    memory_candidate_semantic_fingerprint,
    memory_retrieval_strength_bp,
    memory_source_authority_id,
    memory_source_cluster_fingerprint,
    thread_semantic_fingerprint,
)
from companion_daemon.world_v2.reducers import ReducerState
from companion_daemon.world_v2.sqlite_ledger import SQLiteWorldLedger


NOW = datetime(2026, 7, 15, 15, 0, tzinfo=UTC)
WORLD = "world-memory-authority"


def canonical_hash(value) -> str:
    return hashlib.sha256(json.dumps(
        value.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()).hexdigest()


def fact_authority(
    *,
    revision: int = 1,
    event_ref: str = "event:fact:1",
    privacy: str = "private",
    status: str = "active",
    value_hash: str = "a" * 64,
) -> tuple[FactProjection, FactTransitionProjection, CommittedWorldEventRef]:
    evidence = EvidenceRef(
        ref_id="operator:fact",
        evidence_type="operator_observation",
        claim_purpose="current_fact",
        immutable_hash="b" * 64,
    )
    assertion = FactAssertionBinding(
        source_kind="operator_observation",
        source_ref=evidence.ref_id,
        asserted_subject_ref="subject:user",
        content_payload_hash="b" * 64,
    )
    values = FactValues(
        subject_ref="subject:user",
        predicate_code="location.current",
        cardinality="single",
        conflict_key=fact_conflict_key(
            subject_ref="subject:user", predicate_code="location.current"
        ),
        value_ref=f"value:topic:{revision}",
        value_hash=value_hash,
        assertion_binding=assertion,
        anchor_evidence_refs=(evidence,),
        source_evidence_refs=(evidence,),
        confidence_bp=9000,
        privacy_class=privacy,
        status=status,
        withdrawal_reason_code=("invalid" if status == "withdrawn" else None),
        withdrawal_evidence_ref=(evidence.ref_id if status == "withdrawn" else None),
    )
    origin = FactOrigin(
        change_id=f"change:fact:{revision}",
        transition_id=f"transition:fact:{revision}",
        policy_refs=("policy:fact-v1",),
        accepted_event_ref=event_ref,
    )
    fact = FactProjection(
        fact_id="fact:topic",
        entity_revision=revision,
        semantic_fingerprint=fact_semantic_fingerprint(
            subject_ref=values.subject_ref,
            predicate_code=values.predicate_code,
            cardinality=values.cardinality,
            conflict_key=values.conflict_key,
            value_hash=values.value_hash,
            assertion_binding=values.assertion_binding,
            anchor_evidence_refs=values.anchor_evidence_refs,
            policy_refs=origin.policy_refs,
        ),
        values=values,
        origin=origin,
        committed_at=NOW - timedelta(minutes=5),
        updated_at=NOW - timedelta(minutes=5 - revision),
    )
    transition = FactTransitionProjection(
        transition_id=origin.transition_id,
        fact_id=fact.fact_id,
        entity_revision=revision,
        operation="commit" if revision == 1 else "correct",
        values_before=None,
        values_after=values,
        semantic_fingerprint_after=fact.semantic_fingerprint,
        change_id=origin.change_id,
        policy_refs=origin.policy_refs,
        accepted_event_ref=event_ref,
        accepted_at=fact.updated_at,
    )
    committed = CommittedWorldEventRef(
        event_id=event_ref,
        event_type="FactCommitted" if revision == 1 else "FactCorrected",
        world_revision=revision,
        payload_hash=(str(revision) * 64),
        logical_time=fact.updated_at,
    )
    return fact, transition, committed


def binding(
    fact: FactProjection,
    transition: FactTransitionProjection,
    committed: CommittedWorldEventRef,
) -> MemorySourceBinding:
    return MemorySourceBinding(
        source_kind="fact",
        source_id=fact.fact_id,
        source_entity_revision=transition.entity_revision,
        authority_event_ref=committed.event_id,
        authority_world_revision=committed.world_revision,
        authority_payload_hash=committed.payload_hash,
        source_values_hash=canonical_hash(transition.values_after),
    )


def hardened_experience_authority() -> tuple[
    ExperienceProjection,
    ExperienceTransitionProjection,
    CommittedWorldEventRef,
    MemorySourceBinding,
]:
    values = ExperienceValues(
        summary_ref="summary:experience",
        summary_payload_hash="4" * 64,
        occurred_from=NOW - timedelta(minutes=5),
        occurred_to=NOW - timedelta(minutes=1),
        participant_refs=("actor:companion",),
        source_bindings=(
            ExperienceExecutionReceiptBinding(
                receipt_id="receipt:experience",
                receipt_hash="5" * 64,
                action_id="action:experience",
                action_payload_hash="6" * 64,
                result_id="result:experience",
                observed_state="delivered",
                raw_payload_hash="7" * 64,
            ),
        ),
        privacy_class="private",
    )
    origin = ExperienceOrigin(
        change_id="change:experience",
        transition_id="transition:experience",
        policy_refs=("policy:experience-v1",),
        accepted_event_ref="event:experience",
    )
    experience = ExperienceProjection(
        experience_id="experience:1",
        semantic_fingerprint=experience_semantic_fingerprint(
            values=values, policy_refs=origin.policy_refs
        ),
        values=values,
        origin=origin,
    )
    transition = ExperienceTransitionProjection(
        transition_id=origin.transition_id,
        experience_id=experience.experience_id,
        values_after=values,
        semantic_fingerprint_after=experience.semantic_fingerprint,
        change_id=origin.change_id,
        policy_refs=origin.policy_refs,
        accepted_event_ref=origin.accepted_event_ref,
        accepted_at=NOW,
    )
    committed = CommittedWorldEventRef(
        event_id=origin.accepted_event_ref,
        event_type="ExperienceCommitted",
        world_revision=1,
        payload_hash="8" * 64,
        logical_time=NOW,
    )
    source = MemorySourceBinding(
        source_kind="experience",
        source_id=experience.experience_id,
        source_entity_revision=1,
        authority_event_ref=committed.event_id,
        authority_world_revision=committed.world_revision,
        authority_payload_hash=committed.payload_hash,
        source_values_hash=canonical_hash(values),
    )
    return experience, transition, committed, source


def thread_authority(*, terminal: bool) -> tuple[
    ThreadProjection,
    ThreadTransitionProjection,
    CommittedWorldEventRef,
    MemorySourceBinding,
]:
    evidence = EvidenceRef(
        ref_id="operator:thread",
        evidence_type="operator_observation",
        claim_purpose="private_hypothesis",
        immutable_hash="a" * 64,
    )
    status = "resolved" if terminal else "open"
    values = ThreadValues(
        kind="topic_open",
        subject_ref="subject:user-day",
        conversation_ref="conversation:1",
        anchor_evidence_refs=(evidence,),
        source_evidence_refs=(evidence,),
        importance_bp=6500,
        resolution_contract_ref="resolution-contract:topic-understood",
        privacy_class="private",
        status=status,
        resolution_kind="answered" if terminal else None,
        resolution_ref="message:answer" if terminal else None,
    )
    origin = ThreadOrigin(
        change_id=f"change:thread:{status}",
        transition_id=f"transition:thread:{status}",
        policy_refs=("policy:thread-v1",),
        accepted_event_ref=f"event:thread:{status}",
    )
    thread = ThreadProjection(
        thread_id="thread:1",
        entity_revision=2 if terminal else 1,
        semantic_fingerprint=thread_semantic_fingerprint(
            kind=values.kind,
            subject_ref=values.subject_ref,
            conversation_ref=values.conversation_ref,
            anchor_evidence_refs=values.anchor_evidence_refs,
            resolution_contract_ref=values.resolution_contract_ref,
            policy_refs=origin.policy_refs,
        ),
        values=values,
        origin=origin,
        opened_at=NOW - timedelta(minutes=5),
        updated_at=NOW,
    )
    transition = ThreadTransitionProjection(
        transition_id=origin.transition_id,
        thread_id=thread.thread_id,
        entity_revision=thread.entity_revision,
        operation="resolve" if terminal else "open",
        values_before=None,
        values_after=values,
        change_id=origin.change_id,
        policy_refs=origin.policy_refs,
        accepted_event_ref=origin.accepted_event_ref,
        accepted_at=NOW,
    )
    committed = CommittedWorldEventRef(
        event_id=origin.accepted_event_ref,
        event_type="ThreadResolved" if terminal else "ThreadOpened",
        world_revision=thread.entity_revision,
        payload_hash=("b" if terminal else "c") * 64,
        logical_time=NOW,
    )
    source = MemorySourceBinding(
        source_kind="terminal_thread",
        source_id=thread.thread_id,
        source_entity_revision=thread.entity_revision,
        authority_event_ref=committed.event_id,
        authority_world_revision=committed.world_revision,
        authority_payload_hash=committed.payload_hash,
        source_values_hash=canonical_hash(values),
    )
    return thread, transition, committed, source


def salience(**updates) -> MemorySalienceVector:
    raw = {
        "autobiographical_relevance_bp": 6000,
        "relationship_relevance_bp": 4000,
        "emotional_residue_bp": 3000,
        "unfinished_business_bp": 5000,
        "recurrence_bp": 4000,
        "novelty_bp": 2000,
        "future_utility_bp": 7000,
        "world_continuity_bp": 3000,
        "matrix_digest": MEMORY_SALIENCE_MATRIX_DIGEST,
    }
    raw.update(updates)
    return MemorySalienceVector(**raw)


def candidate(
    source: MemorySourceBinding,
    *,
    candidate_id: str = "memory:topic",
    revision: int = 1,
    status: str = "pending",
    summary_hash: str = "c" * 64,
    accepted_event_ref: str = "event:memory:open",
    opened_at: datetime = NOW,
    updated_at: datetime = NOW,
    reviewed_at: datetime | None = None,
    forgotten_at: datetime | None = None,
    cluster_lineage: tuple[str, ...] | None = None,
    vector: MemorySalienceVector | None = None,
    consumed: tuple[str, ...] | None = None,
    sources: tuple[MemorySourceBinding, ...] | None = None,
    reinforcement_count: int = 0,
    last_reinforced_at: datetime | None = None,
    review_due_at: datetime | None = None,
    privacy_ceiling: str = "private",
) -> MemoryCandidateProjection:
    vector = vector or salience()
    sources = sources or (source,)
    consumed = consumed or tuple(memory_source_authority_id(item) for item in sources)
    strength = (
        0 if status in {"rejected", "forgotten"}
        else memory_retrieval_strength_bp(vector)
    )
    values = MemoryCandidateValues(
        summary_ref="summary:topic",
        summary_payload_hash=summary_hash,
        cue_kind="future_utility",
        source_bindings=sources,
        consumed_source_authority_ids=consumed,
        retention_rationales=("future_utility",),
        future_use_refs=("conversation:future",),
        privacy_ceiling=privacy_ceiling,
        salience=vector,
        status=status,
        retrieval_strength_bp=strength,
        reinforcement_count=reinforcement_count,
        last_reinforced_at=last_reinforced_at,
        reviewed_at=reviewed_at,
        forgotten_at=forgotten_at,
        review_due_at=review_due_at,
    )
    origin = MemoryCandidateOrigin(
        change_id=f"change:{candidate_id}:{revision}",
        transition_id=f"transition:{candidate_id}:{revision}",
        policy_refs=MEMORY_POLICY_REFS,
        accepted_event_ref=accepted_event_ref,
    )
    cluster = memory_source_cluster_fingerprint(
        values=values, policy_refs=origin.policy_refs
    )
    return MemoryCandidateProjection(
        candidate_id=candidate_id,
        entity_revision=revision,
        semantic_fingerprint=memory_candidate_semantic_fingerprint(
            values=values, policy_refs=origin.policy_refs
        ),
        source_cluster_fingerprint=cluster,
        source_cluster_lineage=(
            (*cluster_lineage, cluster) if cluster_lineage else (cluster,)
        ),
        values=values,
        origin=origin,
        opened_at=opened_at,
        updated_at=updated_at,
    )


def mutation(
    after: MemoryCandidateProjection,
    *,
    operation: str,
    before: MemoryCandidateProjection | None = None,
    revise_kind: str | None = None,
    reinforcement_reason: str | None = None,
    rejection_reason: str | None = None,
    forget_authority=None,
    evaluated_world_revision: int = 1,
) -> MemoryCandidateChangedPayload:
    raw = {
        "change_id": after.origin.change_id,
        "transition_id": after.origin.transition_id,
        "expected_entity_revision": before.entity_revision if before else 0,
        "evidence_refs": tuple(
            memory_source_evidence(item) for item in after.values.source_bindings
        ),
        "policy_refs": MEMORY_POLICY_REFS,
        "acceptance_id": f"acceptance:{after.origin.transition_id}",
        "proposal_id": f"proposal:{after.origin.transition_id}",
        "evaluated_world_revision": evaluated_world_revision,
        "accepted_change_hash": "0" * 64,
        "operation": operation,
        "candidate_before": before,
        "candidate_after": after,
        "revise_kind": revise_kind,
        "reinforcement_reason": reinforcement_reason,
        "rejection_reason": rejection_reason,
        "forget_authority": forget_authority,
        "strength_before_bp": (
            before.values.retrieval_strength_bp
            if before and operation in {"reinforce", "forget"}
            else None
        ),
        "strength_after_bp": (
            after.values.retrieval_strength_bp
            if operation in {"reinforce", "forget"}
            else None
        ),
        "reinforcement_count_before": (
            before.values.reinforcement_count
            if before and operation in {"reinforce", "forget"}
            else None
        ),
        "reinforcement_count_after": (
            after.values.reinforcement_count
            if operation in {"reinforce", "forget"}
            else None
        ),
        "policy_version": (
            MEMORY_POLICY_VERSION if operation in {"reinforce", "forget"} else None
        ),
        "policy_digest": (
            MEMORY_POLICY_DIGEST if operation in {"reinforce", "forget"} else None
        ),
    }
    raw["accepted_change_hash"] = memory_candidate_mutation_hash(raw)
    return MemoryCandidateChangedPayload.model_validate(raw)


def event(event_id: str, event_type: str, payload: dict[str, object]) -> WorldEvent:
    return WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id=event_id,
        world_id=WORLD,
        event_type=event_type,
        logical_time=NOW,
        created_at=NOW,
        actor="system:test",
        source="test",
        trace_id="trace:memory",
        causation_id=f"cause:{event_id}",
        correlation_id="correlation:memory",
        idempotency_key=domain_idempotency_key(
            event_type=event_type, world_id=WORLD, payload=payload
        )
        or f"identity:{event_id}",
        payload=payload,
    )


def memory_proposal(value: MemoryCandidateChangedPayload) -> MemoryCandidateProposalProjection:
    event_type = {
        "open": "MemoryCandidateOpened",
        "accept": "MemoryCandidateAccepted",
        "reject": "MemoryCandidateRejected",
        "revise": "MemoryCandidateRevised",
        "reinforce": "MemoryCandidateReinforced",
        "forget": "MemoryCandidateForgotten",
    }[value.operation]
    return MemoryCandidateProposalProjection(
        proposal_id=value.proposal_id,
        proposal_encoding="typed-authority-v1",
        authority_contract_ref="proposal-contract:memory-candidate.1",
        transition_kind=value.operation,
        change_id=value.change_id,
        transition_id=value.transition_id,
        evaluated_world_revision=value.evaluated_world_revision,
        expected_entity_revision=value.expected_entity_revision,
        proposed_change_hash=value.accepted_change_hash,
        evidence_refs=value.evidence_refs,
        policy_refs=value.policy_refs,
        proposed_mutation=MemoryCandidateProposedMutation(
            event_type=event_type,
            payload_json=json.dumps(
                value.model_dump(mode="json"),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        ),
    )


def acceptance(value: MemoryCandidateChangedPayload, *, status: str = "accepted") -> dict[str, object]:
    return {
        "acceptance_id": value.acceptance_id,
        "status": status,
        "proposal_id": value.proposal_id,
        "evaluated_world_revision": value.evaluated_world_revision,
        "accepted_change_id": value.change_id if status == "accepted" else None,
        "accepted_change_hash": value.accepted_change_hash if status == "accepted" else None,
    }


def record_memory_proposal(ledger: WorldLedger, value: MemoryCandidateChangedPayload) -> None:
    projected = ledger.project()
    proposal = memory_proposal(value)
    ledger.commit(
        [event(f"event:{value.proposal_id}", "ProposalRecorded", proposal.model_dump(mode="json"))],
        expected_world_revision=projected.world_revision,
        expected_deliberation_revision=projected.deliberation_revision,
    )


def record_memory_accept_mutate(ledger: WorldLedger, value: MemoryCandidateChangedPayload) -> None:
    record_memory_proposal(ledger, value)
    projected = ledger.project()
    proposed = memory_proposal(value).proposed_mutation
    ledger.commit(
        [
            event(f"event:{value.acceptance_id}", "AcceptanceRecorded", acceptance(value)),
            event(
                value.candidate_after.origin.accepted_event_ref,
                proposed.event_type,
                value.model_dump(mode="json"),
            ),
        ],
        expected_world_revision=projected.world_revision,
        expected_deliberation_revision=projected.deliberation_revision,
    )


def record_privacy_message(ledger: WorldLedger | SQLiteWorldLedger) -> EvidenceRef:
    payload = {
        "schema_version": "world-v2.1",
        "observation_kind": "message",
        "observation_id": "message:forget-memory",
        "world_id": WORLD,
        "logical_time": NOW.isoformat(),
        "created_at": NOW.isoformat(),
        "trace_id": "trace:memory",
        "causation_id": "cause:message:forget-memory",
        "correlation_id": "correlation:memory",
        "source": "test",
        "source_event_id": "source:message:forget-memory",
        "actor": "user:primary",
        "channel": "chat",
        "payload_ref": "payload:message:forget-memory",
        "payload_hash": "d" * 64,
        "received_at": NOW.isoformat(),
    }
    message_event = WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id="event:message:forget-memory",
        world_id=WORLD,
        event_type="ObservationRecorded",
        logical_time=NOW,
        created_at=NOW,
        actor="user:primary",
        source="test",
        trace_id="trace:memory",
        causation_id="cause:message:forget-memory",
        correlation_id="correlation:memory",
        idempotency_key=domain_idempotency_key(
            event_type="ObservationRecorded", world_id=WORLD, payload=payload
        )
        or "identity:message:forget-memory",
        payload=payload,
    )
    projected = ledger.project()
    ledger.commit(
        [message_event],
        expected_world_revision=projected.world_revision,
        expected_deliberation_revision=projected.deliberation_revision,
    )
    retained = ledger.project().message_observations[-1]
    return EvidenceRef(
        ref_id=retained.observation_id,
        evidence_type="observed_message",
        claim_purpose="conversation_continuity",
        source_world_revision=retained.world_revision,
        immutable_hash=retained.event_payload_hash,
    )


def initialized_ledger_with_fact(
    ledger: WorldLedger | SQLiteWorldLedger | None = None,
) -> tuple[WorldLedger | SQLiteWorldLedger, MemorySourceBinding]:
    ledger = ledger or WorldLedger.in_memory(world_id=WORLD)
    ledger.commit(
        [event("world:start", "WorldStarted", {})],
        expected_world_revision=0,
        expected_deliberation_revision=0,
    )
    ledger.commit(
        [
            event(
                "clock:start",
                "ClockAdvanced",
                {
                    "logical_time_from": (NOW - timedelta(minutes=1)).isoformat(),
                    "logical_time_to": NOW.isoformat(),
                },
            )
        ],
        expected_world_revision=1,
        expected_deliberation_revision=0,
    )
    ledger.commit(
        [
            event(
                "operator:fact",
                "OperatorObservationRecorded",
                {"observation_id": "operator:fact", "observation_hash": "b" * 64},
            )
        ],
        expected_world_revision=2,
        expected_deliberation_revision=0,
    )
    fact_value, _, _ = fact_authority()
    fact_value = fact_value.model_copy(update={"committed_at": NOW, "updated_at": NOW})
    raw = {
        "change_id": fact_value.origin.change_id,
        "transition_id": fact_value.origin.transition_id,
        "expected_entity_revision": 0,
        "evidence_refs": fact_value.values.source_evidence_refs,
        "policy_refs": fact_value.origin.policy_refs,
        "acceptance_id": "acceptance:fact:1",
        "proposal_id": "proposal:fact:1",
        "evaluated_world_revision": 2,
        "accepted_change_hash": "0" * 64,
        "operation": "commit",
        "fact_before": None,
        "fact_after": fact_value,
        "compensates_transition_id": None,
    }
    raw["accepted_change_hash"] = fact_mutation_hash(raw)
    fact_payload = FactChangedPayload.model_validate(raw)
    fact_proposal = FactProposalProjection(
        proposal_id=fact_payload.proposal_id,
        proposal_encoding="typed-authority-v1",
        authority_contract_ref="proposal-contract:fact.1",
        transition_kind="commit",
        change_id=fact_payload.change_id,
        transition_id=fact_payload.transition_id,
        evaluated_world_revision=fact_payload.evaluated_world_revision,
        expected_entity_revision=0,
        proposed_change_hash=fact_payload.accepted_change_hash,
        evidence_refs=fact_payload.evidence_refs,
        policy_refs=fact_payload.policy_refs,
        proposed_mutation=FactProposedMutation(
            event_type="FactCommitted",
            payload_json=json.dumps(
                fact_payload.model_dump(mode="json"),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        ),
    )
    projected = ledger.project()
    ledger.commit(
        [event("event:proposal:fact:1", "ProposalRecorded", fact_proposal.model_dump(mode="json"))],
        expected_world_revision=projected.world_revision,
        expected_deliberation_revision=projected.deliberation_revision,
    )
    projected = ledger.project()
    ledger.commit(
        [
            event(
                "event:acceptance:fact:1",
                "AcceptanceRecorded",
                {
                    "acceptance_id": fact_payload.acceptance_id,
                    "status": "accepted",
                    "proposal_id": fact_payload.proposal_id,
                    "evaluated_world_revision": fact_payload.evaluated_world_revision,
                    "accepted_change_id": fact_payload.change_id,
                    "accepted_change_hash": fact_payload.accepted_change_hash,
                },
            ),
            event(
                fact_value.origin.accepted_event_ref,
                "FactCommitted",
                fact_payload.model_dump(mode="json"),
            ),
        ],
        expected_world_revision=projected.world_revision,
        expected_deliberation_revision=projected.deliberation_revision,
    )
    projected = ledger.project()
    committed = next(
        item for item in projected.committed_world_event_refs
        if item.event_id == fact_value.origin.accepted_event_ref
    )
    return ledger, binding(projected.facts[0], projected.fact_transitions[0], committed)


def reduce(
    candidates,
    history,
    payload,
    *,
    facts,
    fact_history,
    committed,
):
    return reduce_memory_candidate(
        candidates,
        history,
        payload,
        event_type={
            "open": "MemoryCandidateOpened",
            "accept": "MemoryCandidateAccepted",
            "reject": "MemoryCandidateRejected",
            "revise": "MemoryCandidateRevised",
            "reinforce": "MemoryCandidateReinforced",
            "forget": "MemoryCandidateForgotten",
        }[payload.operation],
        event_id=payload.candidate_after.origin.accepted_event_ref,
        logical_time=payload.candidate_after.updated_at,
        facts=facts,
        fact_history=fact_history,
        experiences=(),
        experience_history=(),
        threads=(),
        thread_history=(),
        committed_events=committed,
    )


def test_memory_open_is_source_bound_and_zero_cascade() -> None:
    fact, transition, committed = fact_authority()
    source = binding(fact, transition, committed)
    opened = candidate(source)
    heads, history = reduce(
        (), (), mutation(opened, operation="open"),
        facts=(fact,), fact_history=(transition,), committed=(committed,),
    )
    assert heads == (opened,)
    assert len(history) == 1
    assert history[0].operation == "open"
    assert history[0].values_after.source_bindings == (source,)


def test_reducer_state_rejects_orphan_and_discontinuous_memory_lineage() -> None:
    fact, transition, committed = fact_authority()
    source = binding(fact, transition, committed)
    opened = candidate(source)
    heads, history = reduce(
        (),
        (),
        mutation(opened, operation="open"),
        facts=(fact,),
        fact_history=(transition,),
        committed=(committed,),
    )
    ReducerState(
        memory_candidates=heads,
        memory_candidate_transitions=history,
    )
    with pytest.raises(ValueError, match="no projected head"):
        ReducerState(
            memory_candidates=heads,
            memory_candidate_transitions=(
                history[0].model_copy(update={"candidate_id": "memory:orphan"}),
            ),
        )
    with pytest.raises(ValueError, match="revisions must be contiguous"):
        ReducerState(
            memory_candidates=(opened.model_copy(update={"entity_revision": 2}),),
            memory_candidate_transitions=(
                history[0].model_copy(update={"entity_revision": 2}),
            ),
        )


def test_summary_cannot_be_substituted_for_source_evidence() -> None:
    fact, transition, committed = fact_authority()
    source = binding(fact, transition, committed)
    opened = candidate(source)
    raw = mutation(opened, operation="open").model_dump()
    raw["evidence_refs"] = (EvidenceRef(
        ref_id=opened.values.summary_ref,
        evidence_type="committed_fact",
        claim_purpose="conversation_continuity",
        source_world_revision=committed.world_revision,
        immutable_hash=opened.values.summary_payload_hash,
    ),)
    raw["accepted_change_hash"] = memory_candidate_mutation_hash(raw)
    with pytest.raises(ValueError, match="derived only from source bindings"):
        MemoryCandidateChangedPayload.model_validate(raw)


def test_same_source_cluster_cannot_reopen_with_different_summary_after_rejection() -> None:
    fact, transition, committed = fact_authority()
    source = binding(fact, transition, committed)
    terminal = candidate(
        source,
        status="rejected",
        revision=2,
        reviewed_at=NOW,
        accepted_event_ref="event:memory:rejected",
    )
    duplicate = candidate(
        source,
        candidate_id="memory:duplicate",
        summary_hash="d" * 64,
        accepted_event_ref="event:memory:duplicate",
    )
    with pytest.raises(ValueError, match="source cluster"):
        reduce(
            (terminal,), (), mutation(duplicate, operation="open"),
            facts=(fact,), fact_history=(transition,), committed=(committed,),
        )


def test_pending_and_stale_candidates_are_suppressed_without_state_write() -> None:
    fact1, transition1, committed1 = fact_authority()
    source = binding(fact1, transition1, committed1)
    pending = candidate(source)
    decisions = evaluate_memory_retrieval(
        (pending,), facts=(fact1,), fact_history=(transition1,),
        experiences=(), experience_history=(), threads=(), thread_history=(),
        committed_events=(committed1,), viewer_privacy_ceiling="private",
    )
    assert decisions[0].eligible is False
    assert decisions[0].suppression_reasons == ("not_active",)

    active = candidate(
        source, revision=2, status="active", reviewed_at=NOW,
        accepted_event_ref="event:memory:accepted",
    )
    fact2, transition2, committed2 = fact_authority(
        revision=2, event_ref="event:fact:2", value_hash="e" * 64
    )
    decisions = evaluate_memory_retrieval(
        (active,), facts=(fact2,), fact_history=(transition1, transition2),
        experiences=(), experience_history=(), threads=(), thread_history=(),
        committed_events=(committed1, committed2), viewer_privacy_ceiling="private",
    )
    assert decisions[0].eligible is False
    assert decisions[0].stale_source_ids == (fact1.fact_id,)
    assert decisions[0].review_required is True
    assert active.values.status == "active"


def test_active_correction_replaces_stale_source_without_reinforcement() -> None:
    fact1, transition1, committed1 = fact_authority()
    source1 = binding(fact1, transition1, committed1)
    active = candidate(
        source1, revision=2, status="active", reviewed_at=NOW,
        accepted_event_ref="event:memory:accepted",
    )
    fact2, transition2, committed2 = fact_authority(
        revision=2, event_ref="event:fact:2", value_hash="e" * 64
    )
    source2 = binding(fact2, transition2, committed2)
    corrected = candidate(
        source2,
        revision=3,
        status="active",
        summary_hash="f" * 64,
        accepted_event_ref="event:memory:corrected",
        opened_at=active.opened_at,
        updated_at=NOW + timedelta(minutes=1),
        reviewed_at=NOW + timedelta(minutes=1),
        consumed=(
            *active.values.consumed_source_authority_ids,
            memory_source_authority_id(source2),
        ),
    )
    heads, history = reduce(
        (active,), (),
        mutation(corrected, operation="revise", before=active, revise_kind="correct"),
        facts=(fact2,), fact_history=(transition1, transition2),
        committed=(committed1, committed2),
    )
    assert heads[0].values.reinforcement_count == 0
    assert history[0].revise_kind == "correct"


def test_reinforcement_strength_cannot_be_arbitrarily_reported() -> None:
    fact1, transition1, committed1 = fact_authority()
    source1 = binding(fact1, transition1, committed1)
    active = candidate(
        source1, revision=2, status="active", reviewed_at=NOW,
        accepted_event_ref="event:memory:accepted",
    )
    fact2, transition2, committed2 = fact_authority(
        revision=1, event_ref="event:fact:other", value_hash="e" * 64
    )
    fact2 = fact2.model_copy(update={"fact_id": "fact:other"})
    transition2 = transition2.model_copy(update={"fact_id": "fact:other"})
    source2 = binding(fact2, transition2, committed2)
    forged_vector = salience(
        recurrence_bp=10_000,
        future_utility_bp=10_000,
    )
    forged = candidate(
        source1,
        revision=3,
        status="active",
        accepted_event_ref="event:memory:reinforced",
        opened_at=active.opened_at,
        updated_at=NOW + timedelta(minutes=1),
        reviewed_at=NOW + timedelta(minutes=1),
        vector=forged_vector,
        sources=(source1, source2),
        consumed=(
            *active.values.consumed_source_authority_ids,
            memory_source_authority_id(source2),
        ),
        cluster_lineage=active.source_cluster_lineage,
        reinforcement_count=1,
        last_reinforced_at=NOW + timedelta(minutes=1),
    )
    with pytest.raises(ValueError, match="salience|reinforcement"):
        reduce(
            (active,), (),
            mutation(
                forged, operation="reinforce", before=active,
                reinforcement_reason="future_utility",
            ),
            facts=(fact1, fact2), fact_history=(transition1, transition2),
            committed=(committed1, committed2),
        )


def test_reinforcement_rejects_exact_source_replay_and_authority_alias() -> None:
    fact, transition, committed = fact_authority()
    source = binding(fact, transition, committed)
    active = candidate(
        source,
        revision=2,
        status="active",
        reviewed_at=NOW,
        accepted_event_ref="event:memory:accepted",
    )
    with pytest.raises(ValueError, match="source aliases|unique"):
        candidate(
            source,
            revision=3,
            status="active",
            reviewed_at=NOW + timedelta(minutes=1),
            last_reinforced_at=NOW + timedelta(minutes=1),
            reinforcement_count=1,
            accepted_event_ref="event:memory:replayed",
            opened_at=active.opened_at,
            updated_at=NOW + timedelta(minutes=1),
            sources=(source, source),
        )

    alias = source.model_copy(update={"source_id": "fact:authority-alias"})
    with pytest.raises(ValueError, match="authority events must not be aliased"):
        candidate(
            source,
            revision=3,
            status="active",
            reviewed_at=NOW + timedelta(minutes=1),
            last_reinforced_at=NOW + timedelta(minutes=1),
            reinforcement_count=1,
            accepted_event_ref="event:memory:aliased",
            opened_at=active.opened_at,
            updated_at=NOW + timedelta(minutes=1),
            sources=(source, alias),
            consumed=(
                *active.values.consumed_source_authority_ids,
                memory_source_authority_id(alias),
            ),
            cluster_lineage=active.source_cluster_lineage,
        )


def test_deliberative_forget_does_not_fabricate_clock_authority() -> None:
    fact, transition, committed = fact_authority()
    source = binding(fact, transition, committed)
    active = candidate(
        source, revision=2, status="active", reviewed_at=NOW,
        accepted_event_ref="event:memory:accepted",
    )
    forgotten = candidate(
        source,
        revision=3,
        status="forgotten",
        accepted_event_ref="event:memory:forgotten",
        opened_at=active.opened_at,
        updated_at=NOW + timedelta(minutes=1),
        reviewed_at=NOW + timedelta(minutes=1),
        forgotten_at=NOW + timedelta(minutes=1),
    )
    heads, history = reduce(
        (active,), (),
        mutation(
            forgotten,
            operation="forget",
            before=active,
            forget_authority=MemoryDeliberativeForgetAuthority(),
        ),
        facts=(fact,), fact_history=(transition,), committed=(committed,),
    )
    assert heads[0].values.status == "forgotten"
    assert history[0].forget_reason == "low_future_utility"


def test_clock_forget_requires_latest_exact_clock_at_or_after_frozen_due() -> None:
    fact, transition, committed = fact_authority()
    source = binding(fact, transition, committed)
    due = NOW + timedelta(minutes=5)
    active = candidate(
        source,
        revision=2,
        status="active",
        reviewed_at=NOW,
        accepted_event_ref="event:memory:accepted",
        review_due_at=due,
    )
    forgotten = candidate(
        source,
        revision=3,
        status="forgotten",
        accepted_event_ref="event:memory:clock-forgotten",
        opened_at=active.opened_at,
        updated_at=due,
        reviewed_at=due,
        forgotten_at=due,
        review_due_at=due,
    )
    early_clock = CommittedWorldEventRef(
        event_id="clock:early",
        event_type="ClockAdvanced",
        world_revision=2,
        payload_hash="2" * 64,
        logical_time=due - timedelta(seconds=1),
    )
    authority = MemoryClockForgetAuthority(
        reason="scheduled_decay",
        clock_event_ref=early_clock.event_id,
        clock_world_revision=early_clock.world_revision,
        clock_payload_hash=early_clock.payload_hash,
    )
    with pytest.raises(ValueError, match="Clock authority|before frozen review"):
        reduce(
            (active,),
            (),
            mutation(forgotten, operation="forget", before=active, forget_authority=authority),
            facts=(fact,),
            fact_history=(transition,),
            committed=(committed, early_clock),
        )

    due_clock = early_clock.model_copy(
        update={"event_id": "clock:due", "payload_hash": "3" * 64, "logical_time": due}
    )
    valid = authority.model_copy(
        update={
            "clock_event_ref": due_clock.event_id,
            "clock_payload_hash": due_clock.payload_hash,
        }
    )
    heads, _ = reduce(
        (active,),
        (),
        mutation(forgotten, operation="forget", before=active, forget_authority=valid),
        facts=(fact,),
        fact_history=(transition,),
        committed=(committed, due_clock),
    )
    assert heads[0].values.status == "forgotten"


def test_compression_forget_rejects_self_and_requires_covering_active_target() -> None:
    fact1, transition1, committed1 = fact_authority()
    source1 = binding(fact1, transition1, committed1)
    fact2, transition2, committed2 = fact_authority(
        event_ref="event:fact:other", value_hash="e" * 64
    )
    fact2 = fact2.model_copy(update={"fact_id": "fact:other"})
    transition2 = transition2.model_copy(update={"fact_id": "fact:other"})
    source2 = binding(fact2, transition2, committed2)
    active = candidate(
        source1,
        revision=2,
        status="active",
        reviewed_at=NOW,
        accepted_event_ref="event:memory:accepted",
    )
    target = candidate(
        source1,
        candidate_id="memory:target",
        revision=2,
        status="active",
        reviewed_at=NOW,
        accepted_event_ref="event:memory:target",
        sources=(source1, source2),
    )
    target_event = CommittedWorldEventRef(
        event_id=target.origin.accepted_event_ref,
        event_type="MemoryCandidateAccepted",
        world_revision=3,
        payload_hash="9" * 64,
        logical_time=NOW,
    )
    forgotten = candidate(
        source1,
        revision=3,
        status="forgotten",
        reviewed_at=NOW + timedelta(minutes=1),
        forgotten_at=NOW + timedelta(minutes=1),
        opened_at=active.opened_at,
        updated_at=NOW + timedelta(minutes=1),
        accepted_event_ref="event:memory:compressed",
    )

    def compression_authority(candidate_value: MemoryCandidateProjection) -> MemoryCompressionForgetAuthority:
        return MemoryCompressionForgetAuthority(
            target_candidate_id=candidate_value.candidate_id,
            target_entity_revision=candidate_value.entity_revision,
            target_event_ref=candidate_value.origin.accepted_event_ref,
            target_world_revision=target_event.world_revision,
            target_payload_hash=target_event.payload_hash,
        )

    with pytest.raises(ValueError, match="compression target"):
        reduce(
            (active, target),
            (),
            mutation(
                forgotten,
                operation="forget",
                before=active,
                forget_authority=compression_authority(active),
            ),
            facts=(fact1, fact2),
            fact_history=(transition1, transition2),
            committed=(committed1, committed2, target_event),
        )
    uncovered = candidate(
        source2,
        candidate_id="memory:target",
        revision=2,
        status="active",
        reviewed_at=NOW,
        accepted_event_ref="event:memory:target",
    )
    with pytest.raises(ValueError, match="compression target"):
        reduce(
            (active, uncovered),
            (),
            mutation(
                forgotten,
                operation="forget",
                before=active,
                forget_authority=compression_authority(uncovered),
            ),
            facts=(fact1, fact2),
            fact_history=(transition1, transition2),
            committed=(committed1, committed2, target_event),
        )
    heads, _ = reduce(
        (active, target),
        (),
        mutation(
            forgotten,
            operation="forget",
            before=active,
            forget_authority=compression_authority(target),
        ),
        facts=(fact1, fact2),
        fact_history=(transition1, transition2),
        committed=(committed1, committed2, target_event),
    )
    assert heads[0].values.status == "forgotten"


def test_correction_cannot_drop_current_source_but_may_drop_stale_source() -> None:
    fact1, transition1, committed1 = fact_authority()
    source1 = binding(fact1, transition1, committed1)
    fact2, transition2, committed2 = fact_authority(
        event_ref="event:fact:other", value_hash="e" * 64
    )
    fact2 = fact2.model_copy(update={"fact_id": "fact:other"})
    transition2 = transition2.model_copy(update={"fact_id": "fact:other"})
    source2 = binding(fact2, transition2, committed2)
    active = candidate(
        source1,
        revision=2,
        status="active",
        reviewed_at=NOW,
        accepted_event_ref="event:memory:accepted",
        sources=(source1, source2),
    )
    dropped = candidate(
        source2,
        revision=3,
        status="active",
        reviewed_at=NOW + timedelta(minutes=1),
        opened_at=active.opened_at,
        updated_at=NOW + timedelta(minutes=1),
        accepted_event_ref="event:memory:corrected",
        consumed=active.values.consumed_source_authority_ids,
        cluster_lineage=active.source_cluster_lineage,
    )
    with pytest.raises(ValueError, match="cannot delete or replace a current source"):
        reduce(
            (active,),
            (),
            mutation(dropped, operation="revise", before=active, revise_kind="correct"),
            facts=(fact1, fact2),
            fact_history=(transition1, transition2),
            committed=(committed1, committed2),
        )

    fact1_new, transition1_new, committed1_new = fact_authority(
        revision=2, event_ref="event:fact:2", value_hash="f" * 64
    )
    heads, _ = reduce(
        (active,),
        (),
        mutation(dropped, operation="revise", before=active, revise_kind="correct"),
        facts=(fact1_new, fact2),
        fact_history=(transition1, transition1_new, transition2),
        committed=(committed1, committed1_new, committed2),
    )
    assert heads[0].values.source_bindings == (source2,)


def test_withheld_memory_is_never_returned_by_ordinary_selector() -> None:
    fact, transition, committed = fact_authority(privacy="withhold")
    source = binding(fact, transition, committed)
    active = candidate(
        source,
        revision=2,
        status="active",
        reviewed_at=NOW,
        accepted_event_ref="event:memory:accepted",
        privacy_ceiling="withhold",
    )
    decision = evaluate_memory_retrieval(
        (active,),
        facts=(fact,),
        fact_history=(transition,),
        experiences=(),
        experience_history=(),
        threads=(),
        thread_history=(),
        committed_events=(committed,),
        viewer_privacy_ceiling="withhold",
    )[0]
    assert decision.eligible is False
    assert decision.suppression_reasons == ("privacy_ceiling",)


def test_retrieval_accepts_only_exact_hardened_experience_authority() -> None:
    experience, transition, committed, source = hardened_experience_authority()
    active = candidate(
        source,
        revision=2,
        status="active",
        reviewed_at=NOW,
        accepted_event_ref="event:memory:accepted-experience",
    )

    def decision_for(current, candidate_value=active):
        return evaluate_memory_retrieval(
            (candidate_value,),
            facts=(),
            fact_history=(),
            experiences=(current,),
            experience_history=(transition,),
            threads=(),
            thread_history=(),
            committed_events=(committed,),
            viewer_privacy_ceiling="private",
        )[0]

    assert decision_for(experience).eligible is True
    legacy = LegacyExperienceProjection(
        experience_id=experience.experience_id,
        entity_revision=1,
        summary_ref="summary:legacy",
        evidence_refs=(
            LegacyExperienceEvidenceRef(
                ref_id="event:legacy",
                evidence_type="committed_world_event",
                claim_purpose="past_experience",
                source_world_revision=1,
                immutable_hash="d" * 64,
            ),
        ),
        occurred_from=NOW - timedelta(minutes=5),
        occurred_to=NOW - timedelta(minutes=1),
        participant_refs=("actor:companion",),
        occurrence_refs=("occurrence:legacy",),
        privacy_class="private",
    )
    legacy_decision = decision_for(legacy)
    assert legacy_decision.eligible is False
    assert legacy_decision.suppression_reasons == ("stale_source",)

    wrong_hash_source = source.model_copy(update={"source_values_hash": "0" * 64})
    wrong_hash = candidate(
        wrong_hash_source,
        revision=2,
        status="active",
        reviewed_at=NOW,
        accepted_event_ref="event:memory:wrong-experience-hash",
    )
    assert decision_for(experience, wrong_hash).eligible is False


def test_retrieval_accepts_only_exact_current_terminal_thread() -> None:
    open_thread, open_transition, open_event, open_source = thread_authority(terminal=False)
    open_memory = candidate(
        open_source,
        revision=2,
        status="active",
        reviewed_at=NOW,
        accepted_event_ref="event:memory:open-thread",
    )
    open_decision = evaluate_memory_retrieval(
        (open_memory,),
        facts=(),
        fact_history=(),
        experiences=(),
        experience_history=(),
        threads=(open_thread,),
        thread_history=(open_transition,),
        committed_events=(open_event,),
        viewer_privacy_ceiling="private",
    )[0]
    assert open_decision.eligible is False
    assert open_decision.suppression_reasons == ("stale_source",)

    terminal, terminal_transition, terminal_event, terminal_source = thread_authority(
        terminal=True
    )
    terminal_memory = candidate(
        terminal_source,
        revision=2,
        status="active",
        reviewed_at=NOW,
        accepted_event_ref="event:memory:terminal-thread",
    )
    terminal_decision = evaluate_memory_retrieval(
        (terminal_memory,),
        facts=(),
        fact_history=(),
        experiences=(),
        experience_history=(),
        threads=(terminal,),
        thread_history=(terminal_transition,),
        committed_events=(terminal_event,),
        viewer_privacy_ceiling="private",
    )[0]
    assert terminal_decision.eligible is True
    wrong_revision_source = terminal_source.model_copy(
        update={"source_entity_revision": terminal.entity_revision - 1}
    )
    wrong_revision_memory = candidate(
        wrong_revision_source,
        revision=2,
        status="active",
        reviewed_at=NOW,
        accepted_event_ref="event:memory:wrong-thread-revision",
    )
    wrong_revision = evaluate_memory_retrieval(
        (wrong_revision_memory,),
        facts=(),
        fact_history=(),
        experiences=(),
        experience_history=(),
        threads=(terminal,),
        thread_history=(terminal_transition,),
        committed_events=(terminal_event,),
        viewer_privacy_ceiling="private",
    )[0]
    assert wrong_revision.eligible is False


def test_privacy_forget_binds_exact_message_actor_content_and_target() -> None:
    ledger, source = initialized_ledger_with_fact()
    opened = candidate(source)
    record_memory_accept_mutate(
        ledger,
        mutation(
            opened,
            operation="open",
            evaluated_world_revision=ledger.project().world_revision,
        ),
    )
    active = candidate(
        source,
        revision=2,
        status="active",
        accepted_event_ref="event:memory:accepted",
        opened_at=opened.opened_at,
        updated_at=NOW,
        reviewed_at=NOW,
    )
    record_memory_accept_mutate(
        ledger,
        mutation(
            active,
            operation="accept",
            before=opened,
            evaluated_world_revision=ledger.project().world_revision,
        ),
    )
    decision_evidence = record_privacy_message(ledger)
    forgotten = candidate(
        source,
        revision=3,
        status="forgotten",
        accepted_event_ref="event:memory:privacy-forgotten",
        opened_at=active.opened_at,
        updated_at=NOW,
        reviewed_at=NOW,
        forgotten_at=NOW,
    )

    def authority(
        *, subject: str = "user:primary", content_hash: str = "d" * 64,
        target: str = active.candidate_id,
    ) -> MemoryEvidenceForgetAuthority:
        return MemoryEvidenceForgetAuthority(
            reason="privacy_request",
            decision_evidence_ref=decision_evidence,
            target_candidate_id=target,
            decision_subject_ref=subject,
            decision_content_hash=content_hash,
            decision_scope_hash=memory_forget_scope_hash(
                reason="privacy_request",
                target_candidate_id=target,
                decision_subject_ref=subject,
                decision_evidence_ref=decision_evidence,
                decision_content_hash=content_hash,
            ),
        )

    for forged in (
        authority(subject="user:attacker"),
        authority(content_hash="e" * 64),
        authority(target="memory:other"),
    ):
        forged_payload = mutation(
            forgotten,
            operation="forget",
            before=active,
            forget_authority=forged,
            evaluated_world_revision=ledger.project().world_revision,
        )
        with pytest.raises(ValueError, match="principal message scope|targets another candidate"):
            record_memory_proposal(ledger, forged_payload)

    valid = mutation(
        forgotten,
        operation="forget",
        before=active,
        forget_authority=authority(),
        evaluated_world_revision=ledger.project().world_revision,
    )
    record_memory_accept_mutate(ledger, valid)
    assert ledger.project().memory_candidates[0].values.status == "forgotten"


def test_ledger_memory_open_accept_is_typed_and_zero_cascade() -> None:
    ledger, source = initialized_ledger_with_fact()
    baseline = ledger.project()
    opened = candidate(source)
    opened_payload = mutation(
        opened,
        operation="open",
        evaluated_world_revision=baseline.world_revision,
    )
    record_memory_accept_mutate(ledger, opened_payload)
    after_open = ledger.project()
    assert after_open.memory_candidates == (opened,)
    assert after_open.memory_candidate_transitions[-1].operation == "open"
    assert after_open.memory_candidate_proposals == ()
    for field in (
        "facts",
        "fact_transitions",
        "experiences",
        "experience_transitions",
        "threads",
        "thread_transitions",
        "actions",
        "affect_episodes",
        "relationship_states",
        "commitments",
    ):
        assert getattr(after_open, field) == getattr(baseline, field)

    accepted = candidate(
        source,
        revision=2,
        status="active",
        accepted_event_ref="event:memory:accepted",
        opened_at=opened.opened_at,
        updated_at=NOW,
        reviewed_at=NOW,
    )
    accepted_payload = mutation(
        accepted,
        operation="accept",
        before=opened,
        evaluated_world_revision=after_open.world_revision,
    )
    record_memory_accept_mutate(ledger, accepted_payload)
    projected = ledger.project()
    assert projected.memory_candidates == (accepted,)
    assert tuple(item.operation for item in projected.memory_candidate_transitions) == (
        "open",
        "accept",
    )


def test_ledger_memory_mutation_rejects_rejected_and_nonadjacent_acceptance() -> None:
    ledger, source = initialized_ledger_with_fact()
    opened = candidate(source)
    value = mutation(
        opened,
        operation="open",
        evaluated_world_revision=ledger.project().world_revision,
    )
    record_memory_proposal(ledger, value)
    projected = ledger.project()
    ledger.commit(
        [event("event:acceptance:rejected", "AcceptanceRecorded", acceptance(value, status="rejected"))],
        expected_world_revision=projected.world_revision,
        expected_deliberation_revision=projected.deliberation_revision,
    )
    projected = ledger.project()
    with pytest.raises(ValueError, match="persisted typed proposal|accepted authority|adjacent revision-pinned"):
        ledger.commit(
            [
                event(
                    opened.origin.accepted_event_ref,
                    "MemoryCandidateOpened",
                    value.model_dump(mode="json"),
                )
            ],
            expected_world_revision=projected.world_revision,
            expected_deliberation_revision=projected.deliberation_revision,
        )

    ledger, source = initialized_ledger_with_fact()
    opened = candidate(source)
    value = mutation(
        opened,
        operation="open",
        evaluated_world_revision=ledger.project().world_revision,
    )
    record_memory_proposal(ledger, value)
    projected = ledger.project()
    with pytest.raises(ValueError, match="immediately after|adjacent"):
        ledger.commit(
            [
                event("event:acceptance:memory", "AcceptanceRecorded", acceptance(value)),
                event("world:intervening", "WorldStarted", {}),
                event(
                    opened.origin.accepted_event_ref,
                    "MemoryCandidateOpened",
                    value.model_dump(mode="json"),
                ),
            ],
            expected_world_revision=projected.world_revision,
            expected_deliberation_revision=projected.deliberation_revision,
        )


def test_sqlite_migrates_nonempty_v13_head_to_v14_and_rebuilds(tmp_path) -> None:
    path = tmp_path / "memory-v13.sqlite3"
    ledger = SQLiteWorldLedger(path=path, world_id=WORLD)
    ledger, _ = initialized_ledger_with_fact(ledger)
    before = ledger.project()
    assert before.facts
    ledger.close()

    with sqlite3.connect(path) as connection:
        row = connection.execute(
            "SELECT world_revision, state_json FROM world_v2_heads WHERE world_id = ?",
            (WORLD,),
        ).fetchone()
        assert row is not None
        world_revision, state_json = row
        state = ReducerState.model_validate_json(state_json)
        legacy_semantic = state.semantic_payload(
            world_id=WORLD,
            world_revision=int(world_revision),
            reducer_bundle_version="world-v2-reducers.13",
        )
        legacy_hash = hashlib.sha256(
            json.dumps(
                legacy_semantic,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        raw_state = state.model_dump(mode="json")
        strip_v16_state_fields(raw_state)
        for field in (
            "memory_candidates",
            "memory_candidate_transitions",
            "memory_candidate_proposals",
            "memory_candidate_proposal_ids",
        ):
            raw_state.pop(field)
        connection.execute(
            "UPDATE world_v2_heads SET state_json = ?, semantic_hash = ?, "
            "reducer_bundle_version = ?, state_hash = '' WHERE world_id = ?",
            (
                json.dumps(raw_state, ensure_ascii=False, separators=(",", ":")),
                legacy_hash,
                "world-v2-reducers.13",
                WORLD,
            ),
        )

    migrated = SQLiteWorldLedger(path=path, world_id=WORLD)
    projected = migrated.project()
    assert projected.reducer_bundle_version == "world-v2-reducers.30"
    assert projected.facts == before.facts
    assert projected.memory_candidates == ()
    assert migrated.rebuild() == projected
    migrated.close()
    reopened = SQLiteWorldLedger(path=path, world_id=WORLD)
    assert reopened.project() == projected
    reopened.close()

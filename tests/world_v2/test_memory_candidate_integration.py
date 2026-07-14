from __future__ import annotations

from companion_daemon.world_v2.event_identity import domain_idempotency_key
from companion_daemon.world_v2.memory_events import (
    MemoryEvidenceForgetAuthority,
    memory_forget_scope_hash,
)
from companion_daemon.world_v2.schemas import EvidenceRef, WorldEvent

import test_memory_candidate_authority as authority


def _record_privacy_request(ledger, *, observation_id: str) -> EvidenceRef:
    payload = {
        "schema_version": "world-v2.1",
        "observation_kind": "message",
        "observation_id": observation_id,
        "world_id": authority.WORLD,
        "logical_time": authority.NOW.isoformat(),
        "created_at": authority.NOW.isoformat(),
        "trace_id": "trace:memory-privacy",
        "causation_id": f"cause:{observation_id}",
        "correlation_id": "correlation:memory-privacy",
        "source": "test",
        "source_event_id": f"source:{observation_id}",
        "actor": "user:primary",
        "channel": "chat",
        "payload_ref": f"payload:{observation_id}",
        "payload_hash": "d" * 64,
        "received_at": authority.NOW.isoformat(),
    }
    message_event = WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id=observation_id,
        world_id=authority.WORLD,
        event_type="ObservationRecorded",
        logical_time=authority.NOW,
        created_at=authority.NOW,
        actor="user:primary",
        source="test",
        trace_id="trace:memory-privacy",
        causation_id=f"cause:{observation_id}",
        correlation_id="correlation:memory-privacy",
        idempotency_key=domain_idempotency_key(
            event_type="ObservationRecorded",
            world_id=authority.WORLD,
            payload=payload,
        )
        or f"identity:{observation_id}",
        payload=payload,
    )
    projected = ledger.project()
    ledger.commit(
        [message_event],
        expected_world_revision=projected.world_revision,
        expected_deliberation_revision=projected.deliberation_revision,
    )
    observed = ledger.project().message_observations[-1]
    return EvidenceRef(
        ref_id=observed.observation_id,
        evidence_type="observed_message",
        claim_purpose="conversation_continuity",
        source_world_revision=observed.world_revision,
        immutable_hash=observed.event_payload_hash,
    )


def _active_memory(ledger, source):
    opened = authority.candidate(source)
    authority.record_memory_accept_mutate(
        ledger,
        authority.mutation(
            opened,
            operation="open",
            evaluated_world_revision=ledger.project().world_revision,
        ),
    )
    active = authority.candidate(
        source,
        revision=2,
        status="active",
        accepted_event_ref="event:memory:accepted",
        opened_at=opened.opened_at,
        reviewed_at=authority.NOW,
    )
    authority.record_memory_accept_mutate(
        ledger,
        authority.mutation(
            active,
            operation="accept",
            before=opened,
            evaluated_world_revision=ledger.project().world_revision,
        ),
    )
    return opened, active


def test_acceptance_with_wrong_change_hash_is_atomic_and_cannot_mutate_memory() -> None:
    ledger, source = authority.initialized_ledger_with_fact()
    opened = authority.candidate(source)
    change = authority.mutation(
        opened,
        operation="open",
        evaluated_world_revision=ledger.project().world_revision,
    )
    authority.record_memory_proposal(ledger, change)
    before = ledger.project()
    wrong_acceptance = {
        **authority.acceptance(change),
        "accepted_change_hash": "f" * 64,
    }
    proposed = authority.memory_proposal(change).proposed_mutation

    try:
        ledger.commit(
            [
                authority.event(
                    f"event:{change.acceptance_id}",
                    "AcceptanceRecorded",
                    wrong_acceptance,
                ),
                authority.event(
                    opened.origin.accepted_event_ref,
                    proposed.event_type,
                    change.model_dump(mode="json"),
                ),
            ],
            expected_world_revision=before.world_revision,
            expected_deliberation_revision=before.deliberation_revision,
        )
    except ValueError as exc:
        assert any(
            marker in str(exc)
            for marker in ("hash", "accepted authority", "accepted decision")
        )
    else:
        raise AssertionError("a mismatched acceptance hash must be rejected")

    after = ledger.project()
    assert after == before
    assert after.memory_candidates == ()


def test_stale_before_image_is_rejected_while_recording_the_proposal() -> None:
    ledger, source = authority.initialized_ledger_with_fact()
    opened, active = _active_memory(ledger, source)
    stale_after = authority.candidate(
        source,
        revision=2,
        status="active",
        accepted_event_ref="event:memory:stale-accept",
        opened_at=opened.opened_at,
        reviewed_at=authority.NOW,
    )
    stale_after = stale_after.model_copy(
        update={
            "origin": stale_after.origin.model_copy(
                update={
                    "change_id": "change:memory:stale-accept",
                    "transition_id": "transition:memory:stale-accept",
                }
            )
        }
    )
    stale_change = authority.mutation(
        stale_after,
        operation="accept",
        before=opened,
        evaluated_world_revision=ledger.project().world_revision,
    )
    before = ledger.project()

    try:
        authority.record_memory_proposal(ledger, stale_change)
    except ValueError as exc:
        assert "before image" in str(exc) or "compare-and-swap" in str(exc)
    else:
        raise AssertionError("a stale memory before-image must be rejected")

    after = ledger.project()
    assert after == before
    assert after.memory_candidates == (active,)
    assert all(
        item.proposal_id != stale_change.proposal_id
        for item in after.memory_candidate_proposals
    )


def test_privacy_request_forget_authority_is_scoped_to_exact_candidate() -> None:
    ledger, source = authority.initialized_ledger_with_fact()
    _, active = _active_memory(ledger, source)
    decision_evidence = _record_privacy_request(
        ledger,
        observation_id="message:forget-memory-topic",
    )
    forgotten = authority.candidate(
        source,
        revision=3,
        status="forgotten",
        accepted_event_ref="event:memory:privacy-forgotten",
        opened_at=active.opened_at,
        reviewed_at=authority.NOW,
        forgotten_at=authority.NOW,
    )

    def forget_authority(target_candidate_id: str) -> MemoryEvidenceForgetAuthority:
        scope_hash = memory_forget_scope_hash(
            reason="privacy_request",
            target_candidate_id=target_candidate_id,
            decision_subject_ref="user:primary",
            decision_evidence_ref=decision_evidence,
            decision_content_hash="d" * 64,
        )
        return MemoryEvidenceForgetAuthority(
            reason="privacy_request",
            decision_evidence_ref=decision_evidence,
            target_candidate_id=target_candidate_id,
            decision_subject_ref="user:primary",
            decision_scope_hash=scope_hash,
            decision_content_hash="d" * 64,
        )

    wrong_scope = authority.mutation(
        forgotten,
        operation="forget",
        before=active,
        forget_authority=forget_authority("memory:another-topic"),
        evaluated_world_revision=ledger.project().world_revision,
    )
    before = ledger.project()
    try:
        authority.record_memory_proposal(ledger, wrong_scope)
    except ValueError as exc:
        assert "scope targets another candidate" in str(exc)
    else:
        raise AssertionError("privacy authority for another candidate must be rejected")
    assert ledger.project() == before

    exact_scope = authority.mutation(
        forgotten,
        operation="forget",
        before=active,
        forget_authority=forget_authority(active.candidate_id),
        evaluated_world_revision=ledger.project().world_revision,
    )
    authority.record_memory_accept_mutate(ledger, exact_scope)
    projected = ledger.project()
    assert projected.memory_candidates == (forgotten,)
    assert projected.memory_candidate_transitions[-1].forget_reason == "privacy_request"

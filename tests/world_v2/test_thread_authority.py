from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
import sqlite3

import pytest

from legacy_migration_support import legacy_state_json

from companion_daemon.world_v2.event_identity import domain_idempotency_key
from companion_daemon.world_v2.errors import LedgerIntegrityError
from companion_daemon.world_v2.ledger import WorldLedger
from companion_daemon.world_v2.reducers import ReducerState
from companion_daemon.world_v2.projection import InternalProjectionReader
from companion_daemon.world_v2.sqlite_ledger import SQLiteWorldLedger
from companion_daemon.world_v2.schemas import (
    EvidenceRef,
    ThreadOrigin,
    ThreadProjection,
    ThreadProposalProjection,
    ThreadProposedMutation,
    ThreadValues,
    WorldEvent,
    thread_semantic_fingerprint,
)
from companion_daemon.world_v2.thread_events import (
    ThreadChangedPayload,
    ThreadExpiredPayload,
    thread_mutation_hash,
)
from companion_daemon.world_v2.thread_reducers import (
    THREAD_EXPIRY_POLICY_DIGEST,
    THREAD_EXPIRY_POLICY_VERSION,
    expire_thread,
    reduce_thread,
)


NOW = datetime(2026, 7, 15, 10, 0, tzinfo=UTC)
LATER = datetime(2026, 7, 15, 11, 0, tzinfo=UTC)
WORLD = "world-thread-authority"
POLICY = ("policy:thread-v1",)


def event(event_id: str, event_type: str, payload: dict[str, object], *, at=NOW) -> WorldEvent:
    return WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id=event_id,
        world_id=WORLD,
        event_type=event_type,
        logical_time=at,
        created_at=at,
        actor="system:test",
        source="test",
        trace_id="trace:thread",
        causation_id=f"cause:{event_id}",
        correlation_id="correlation:thread",
        idempotency_key=domain_idempotency_key(
            event_type=event_type, world_id=WORLD, payload=payload
        ) or f"identity:{event_id}",
        payload=payload,
    )


def evidence(ref_id: str = "operator:thread", immutable_hash: str = "a" * 64) -> EvidenceRef:
    return EvidenceRef(
        ref_id=ref_id,
        evidence_type="operator_observation",
        claim_purpose="private_hypothesis",
        immutable_hash=immutable_hash,
    )


def thread(*, thread_id: str = "thread:1", revision: int = 1,
           status: str = "open", sources: tuple[EvidenceRef, ...] | None = None,
           resolution_ref: str | None = None, expires_at=None, updated_at=NOW,
           origin: ThreadOrigin | None = None) -> ThreadProjection:
    refs = sources or (evidence(),)
    values = ThreadValues(
        kind="topic_open",
        subject_ref="subject:user-day",
        conversation_ref="conversation:1",
        anchor_evidence_refs=(evidence(),),
        source_evidence_refs=refs,
        importance_bp=6500,
        expires_at=expires_at,
        resolution_contract_ref="resolution-contract:topic-understood",
        privacy_class="private",
        status=status,
        resolution_kind="answered" if status == "resolved" else None,
        resolution_ref=resolution_ref,
    )
    origin = origin or ThreadOrigin(
        change_id=f"change:{thread_id}:{revision}",
        transition_id=f"transition:{thread_id}:{revision}",
        policy_refs=POLICY,
        accepted_event_ref=f"event:mutation:{thread_id}:{revision}",
    )
    return ThreadProjection(
        thread_id=thread_id,
        entity_revision=revision,
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
        opened_at=NOW,
        updated_at=updated_at,
    )


def payload(*, operation: str, before: ThreadProjection | None,
            after: ThreadProjection, expected: int, proposal_id: str) -> ThreadChangedPayload:
    raw = {
        "change_id": after.origin.change_id,
        "transition_id": after.origin.transition_id,
        "expected_entity_revision": expected,
        "evidence_refs": after.values.source_evidence_refs,
        "policy_refs": POLICY,
        "acceptance_id": f"acceptance:{proposal_id}",
        "proposal_id": proposal_id,
        "evaluated_world_revision": 2 if expected == 0 else 5,
        "accepted_change_hash": "0" * 64,
        "operation": operation,
        "thread_before": before,
        "thread_after": after,
        "compensates_transition_id": None,
    }
    raw["accepted_change_hash"] = thread_mutation_hash(raw)
    return ThreadChangedPayload.model_validate(raw)


def proposal(value: ThreadChangedPayload) -> ThreadProposalProjection:
    return ThreadProposalProjection(
        proposal_id=value.proposal_id,
        proposal_encoding="typed-authority-v1",
        authority_contract_ref="proposal-contract:thread.1",
        transition_kind=value.operation,
        change_id=value.change_id,
        transition_id=value.transition_id,
        evaluated_world_revision=value.evaluated_world_revision,
        expected_entity_revision=value.expected_entity_revision,
        proposed_change_hash=value.accepted_change_hash,
        evidence_refs=value.evidence_refs,
        policy_refs=value.policy_refs,
        proposed_mutation=ThreadProposedMutation(
            event_type={
                "open": "ThreadOpened", "update": "ThreadUpdated",
                "resolve": "ThreadResolved", "cancel": "ThreadCancelled",
                "supersede": "ThreadSuperseded", "compensate": "ThreadCompensated",
            }[value.operation],
            payload_json=json.dumps(
                value.model_dump(mode="json"), ensure_ascii=False,
                sort_keys=True, separators=(",", ":"),
            ),
        ),
    )


def initialized() -> WorldLedger:
    ledger = WorldLedger.in_memory(world_id=WORLD)
    ledger.commit([event("world:start", "WorldStarted", {})],
                  expected_world_revision=0, expected_deliberation_revision=0)
    ledger.commit([event("clock:start", "ClockAdvanced", {
        "logical_time_from": "2026-07-15T09:59:00+00:00",
        "logical_time_to": NOW.isoformat(),
    })], expected_world_revision=1, expected_deliberation_revision=0)
    ledger.commit([event("operator:evidence", "OperatorObservationRecorded", {
        "observation_id": "operator:thread", "observation_hash": "a" * 64,
    })], expected_world_revision=2, expected_deliberation_revision=0)
    ledger.commit([event("operator:resolution", "OperatorObservationRecorded", {
        "observation_id": "operator:resolution", "observation_hash": "b" * 64,
    })], expected_world_revision=2, expected_deliberation_revision=1)
    return ledger


def record_accept_mutate(ledger: WorldLedger, value: ThreadChangedPayload) -> None:
    projection = ledger.project()
    ledger.commit([event(f"event:{value.proposal_id}", "ProposalRecorded", proposal(value).model_dump(mode="json"))],
                  expected_world_revision=projection.world_revision,
                  expected_deliberation_revision=projection.deliberation_revision)
    projection = ledger.project()
    accepted = {
        "acceptance_id": value.acceptance_id, "status": "accepted",
        "proposal_id": value.proposal_id,
        "evaluated_world_revision": value.evaluated_world_revision,
        "accepted_change_id": value.change_id,
        "accepted_change_hash": value.accepted_change_hash,
    }
    ledger.commit([
        event(f"event:{value.acceptance_id}", "AcceptanceRecorded", accepted),
        event(value.thread_after.origin.accepted_event_ref,
              proposal(value).proposed_mutation.event_type,
              value.model_dump(mode="json")),
    ], expected_world_revision=projection.world_revision,
       expected_deliberation_revision=projection.deliberation_revision)


def test_thread_open_is_typed_persistent_and_behavior_neutral() -> None:
    ledger = initialized()
    value = payload(operation="open", before=None, after=thread(), expected=0,
                    proposal_id="proposal:open")
    record_accept_mutate(ledger, value)
    projected = ledger.project()
    assert projected.threads == (value.thread_after,)
    assert len(projected.thread_transitions) == 1
    assert projected.actions == ()
    assert projected.pending_actions == ()


def test_same_active_semantics_cannot_be_reopened_under_another_id() -> None:
    ledger = initialized()
    first = payload(operation="open", before=None, after=thread(), expected=0,
                    proposal_id="proposal:first")
    record_accept_mutate(ledger, first)
    duplicate = payload(operation="open", before=None,
                        after=thread(thread_id="thread:other"), expected=0,
                        proposal_id="proposal:duplicate")
    projection = ledger.project()
    duplicate_raw = duplicate.model_dump()
    duplicate_raw["evaluated_world_revision"] = projection.world_revision
    duplicate_raw["accepted_change_hash"] = "0" * 64
    duplicate_raw["accepted_change_hash"] = thread_mutation_hash(duplicate_raw)
    duplicate = ThreadChangedPayload.model_validate(duplicate_raw)
    with pytest.raises(ValueError, match="active semantic fingerprint"):
        ledger.commit([event("event:proposal:duplicate", "ProposalRecorded",
                             proposal(duplicate).model_dump(mode="json"))],
                      expected_world_revision=projection.world_revision,
                      expected_deliberation_revision=projection.deliberation_revision)


def test_thread_requires_adjacent_acceptance_and_cas() -> None:
    ledger = initialized()
    value = payload(operation="open", before=None, after=thread(), expected=0,
                    proposal_id="proposal:adjacency")
    projection = ledger.project()
    ledger.commit([event("event:proposal:adjacency", "ProposalRecorded",
                         proposal(value).model_dump(mode="json"))],
                  expected_world_revision=projection.world_revision,
                  expected_deliberation_revision=projection.deliberation_revision)
    projection = ledger.project()
    accepted = {
        "acceptance_id": value.acceptance_id, "status": "accepted",
        "proposal_id": value.proposal_id,
        "evaluated_world_revision": value.evaluated_world_revision,
        "accepted_change_id": value.change_id,
        "accepted_change_hash": value.accepted_change_hash,
    }
    with pytest.raises(ValueError, match="immediately after"):
        ledger.commit([event("event:acceptance", "AcceptanceRecorded", accepted),
                       event("event:intervening", "OperatorObservationRecorded", {
                           "observation_id": "operator:other", "observation_hash": "b" * 64,
                       }),
                       event(value.thread_after.origin.accepted_event_ref, "ThreadOpened",
                             value.model_dump(mode="json"))],
                      expected_world_revision=projection.world_revision,
                      expected_deliberation_revision=projection.deliberation_revision)


def test_terminal_thread_cannot_reopen_or_update() -> None:
    ledger = initialized()
    opened = payload(operation="open", before=None, after=thread(), expected=0,
                     proposal_id="proposal:open-terminal")
    record_accept_mutate(ledger, opened)
    terminal = thread(
        revision=2,
        status="resolved",
        sources=(evidence(), evidence("operator:resolution", "b" * 64)),
        resolution_ref="operator:resolution",
    )
    terminal = terminal.model_copy(update={"origin": terminal.origin.model_copy(update={
        "accepted_event_ref": "event:mutation:thread:1:2"})})
    resolved = payload(operation="resolve", before=opened.thread_after, after=terminal,
                       expected=1, proposal_id="proposal:resolve")
    resolved = resolved.model_copy(update={"evaluated_world_revision": ledger.project().world_revision})
    resolved = resolved.model_copy(update={"accepted_change_hash": thread_mutation_hash(
        resolved.model_dump(mode="json") | {"accepted_change_hash": "0" * 64})})
    record_accept_mutate(ledger, resolved)
    assert ledger.project().threads[0].values.status == "resolved"
    attempted = payload(operation="update", before=terminal,
                        after=thread(revision=3, status="open"), expected=2,
                        proposal_id="proposal:reopen")
    attempted = attempted.model_copy(update={"evaluated_world_revision": ledger.project().world_revision})
    attempted = attempted.model_copy(update={"accepted_change_hash": thread_mutation_hash(
        attempted.model_dump(mode="json") | {"accepted_change_hash": "0" * 64})})
    projection = ledger.project()
    with pytest.raises(ValueError, match="terminal"):
        ledger.commit([event("event:proposal:reopen", "ProposalRecorded",
                             proposal(attempted).model_dump(mode="json"))],
                      expected_world_revision=projection.world_revision,
                      expected_deliberation_revision=projection.deliberation_revision)


def transition(
    current: ThreadProjection,
    *,
    operation: str,
    values,
    transition_id: str,
    compensates: str | None = None,
) -> ThreadChangedPayload:
    origin = ThreadOrigin(
        change_id=f"change:{transition_id}",
        transition_id=transition_id,
        policy_refs=POLICY,
        accepted_event_ref=f"event:{transition_id}",
    )
    after = ThreadProjection(
        thread_id=current.thread_id,
        entity_revision=current.entity_revision + 1,
        semantic_fingerprint=thread_semantic_fingerprint(
            kind=values.kind,
            subject_ref=values.subject_ref,
            conversation_ref=values.conversation_ref,
            anchor_evidence_refs=values.anchor_evidence_refs,
            resolution_contract_ref=values.resolution_contract_ref,
            policy_refs=POLICY,
        ),
        values=values,
        origin=origin,
        opened_at=current.opened_at,
        updated_at=NOW,
    )
    raw = {
        "change_id": origin.change_id,
        "transition_id": transition_id,
        "expected_entity_revision": current.entity_revision,
        "evidence_refs": values.source_evidence_refs,
        "policy_refs": POLICY,
        "acceptance_id": f"acceptance:{transition_id}",
        "proposal_id": f"proposal:{transition_id}",
        "evaluated_world_revision": 0,
        "accepted_change_hash": "0" * 64,
        "operation": operation,
        "thread_before": current,
        "thread_after": after,
        "compensates_transition_id": compensates,
    }
    raw["accepted_change_hash"] = thread_mutation_hash(raw)
    return ThreadChangedPayload.model_validate(raw)


def test_thread_expiry_requires_committed_logical_clock_and_frozen_digest() -> None:
    ledger = initialized()
    opened_thread = thread(expires_at=LATER)
    opened = payload(operation="open", before=None, after=opened_thread, expected=0,
                     proposal_id="proposal:open-expiring")
    record_accept_mutate(ledger, opened)
    clock = EvidenceRef(
        ref_id=f"clock:{LATER.isoformat()}",
        evidence_type="clock_observation",
        claim_purpose="conversation_continuity",
    )
    values = opened_thread.values.model_copy(update={
        "source_evidence_refs": (*opened_thread.values.source_evidence_refs, clock),
        "status": "expired",
    })
    after = ThreadProjection(
        thread_id=opened_thread.thread_id,
        entity_revision=2,
        semantic_fingerprint=opened_thread.semantic_fingerprint,
        values=values,
        origin=ThreadOrigin(
            authority_mode="mechanical_clock",
            change_id="change:expire",
            transition_id="transition:expire",
            policy_refs=POLICY,
            accepted_event_ref="event:thread-expired",
        ),
        opened_at=NOW,
        updated_at=LATER,
    )
    clock_event = event("clock:later", "ClockAdvanced", {
        "logical_time_from": NOW.isoformat(), "logical_time_to": LATER.isoformat(),
    }, at=LATER)
    expiry = ThreadExpiredPayload(
        change_id="change:expire",
        transition_id="transition:expire",
        expected_entity_revision=1,
        thread_before=opened_thread,
        thread_after=after,
        clock_evidence_ref=clock,
        clock_event_ref=clock_event.event_id,
        clock_event_payload_hash=clock_event.payload_hash,
        policy_version=THREAD_EXPIRY_POLICY_VERSION,
        policy_digest=THREAD_EXPIRY_POLICY_DIGEST,
        expires_at=LATER,
    )
    projection = ledger.project()
    with pytest.raises(ValueError, match="clock evidence"):
        ledger.commit([event("event:thread-expired", "ThreadExpired",
                             expiry.model_dump(mode="json"))],
                      expected_world_revision=projection.world_revision,
                      expected_deliberation_revision=projection.deliberation_revision)
    ledger.commit([clock_event], expected_world_revision=projection.world_revision,
                  expected_deliberation_revision=projection.deliberation_revision)
    projection = ledger.project()
    ledger.commit([event("event:thread-expired", "ThreadExpired",
                         expiry.model_dump(mode="json"), at=LATER)],
                  expected_world_revision=projection.world_revision,
                  expected_deliberation_revision=projection.deliberation_revision)
    assert ledger.project().threads[0].values.status == "expired"
    assert ledger.project().thread_transitions[-1].operation == "expire"


def test_thread_hash_is_key_order_and_utc_encoding_stable_but_tamper_sensitive() -> None:
    value = payload(operation="open", before=None, after=thread(), expected=0,
                    proposal_id="proposal:hash")
    raw = value.model_dump(mode="json")
    reordered = dict(reversed(tuple(json.loads(json.dumps(raw)).items())))
    reordered["thread_after"]["opened_at"] = "2026-07-15T10:00:00+00:00"
    reordered["thread_after"]["updated_at"] = "2026-07-15T10:00:00+00:00"
    assert thread_mutation_hash(raw) == thread_mutation_hash(reordered)
    reordered["thread_after"]["values"]["importance_bp"] += 1
    assert thread_mutation_hash(raw) != thread_mutation_hash(reordered)


def test_thread_privacy_uses_source_minimum_not_producer_purpose_label() -> None:
    source = evidence().model_copy(update={"claim_purpose": "conversation_continuity"})
    values = thread().values.model_copy(update={
        "anchor_evidence_refs": (source,),
        "source_evidence_refs": (source,),
        "privacy_class": "public",
    })
    opened = thread().model_copy(update={
        "values": values,
        "semantic_fingerprint": thread_semantic_fingerprint(
            kind=values.kind, subject_ref=values.subject_ref,
            conversation_ref=values.conversation_ref,
            anchor_evidence_refs=values.anchor_evidence_refs,
            resolution_contract_ref=values.resolution_contract_ref,
            policy_refs=POLICY,
        ),
    })
    value = payload(operation="open", before=None, after=opened, expected=0,
                    proposal_id="proposal:privacy")
    with pytest.raises(ValueError, match="privacy matrix"):
        reduce_thread((), (), value, event_type="ThreadOpened", logical_time=NOW)


@pytest.mark.parametrize("case", ["source_reordered", "anchor_changed", "privacy_loosened"])
def test_thread_update_rejects_authority_weakening(case: str) -> None:
    current = thread()
    new_evidence = evidence("operator:resolution", "b" * 64)
    updates = {
        "source_evidence_refs": (*current.values.source_evidence_refs, new_evidence),
        "importance_bp": 6400,
    }
    if case == "source_reordered":
        updates["source_evidence_refs"] = (new_evidence, *current.values.source_evidence_refs)
    elif case == "anchor_changed":
        updates["anchor_evidence_refs"] = (new_evidence,)
    else:
        updates["privacy_class"] = "personal"
    changed = transition(
        current,
        operation="update",
        values=current.values.model_copy(update=updates),
        transition_id=f"transition:{case}",
    )
    with pytest.raises(ValueError):
        reduce_thread((current,), (), changed, event_type="ThreadUpdated", logical_time=NOW)


@pytest.mark.parametrize("status,operation", [("resolved", "resolve"), ("cancelled", "cancel")])
def test_thread_closure_requires_new_supporting_evidence(status: str, operation: str) -> None:
    current = thread()
    updates = {"status": status}
    if status == "resolved":
        updates.update({"resolution_kind": "answered", "resolution_ref": evidence().ref_id})
    else:
        updates.update({
            "cancellation_reason_code": "obsolete",
            "cancellation_evidence_ref": evidence().ref_id,
        })
    changed = transition(
        current, operation=operation,
        values=current.values.model_copy(update=updates),
        transition_id=f"transition:{operation}:unsupported",
    )
    with pytest.raises(ValueError, match="newly appended"):
        reduce_thread(
            (current,), (), changed,
            event_type="ThreadResolved" if operation == "resolve" else "ThreadCancelled",
            logical_time=NOW,
        )


def test_thread_rejects_naive_temporal_bounds() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        ThreadValues.model_validate(
            thread().values.model_dump() | {"expires_at": datetime(2026, 7, 15, 11)}
        )
    current = thread()
    retroactive = transition(
        current,
        operation="update",
        values=current.values.model_copy(update={"expires_at": NOW}),
        transition_id="transition:retroactive-expiry",
    )
    with pytest.raises(ValueError, match="retroactive expiry"):
        reduce_thread(
            (current,), (), retroactive,
            event_type="ThreadUpdated", logical_time=NOW,
        )


def test_thread_compensation_restores_latest_update_and_preserves_history() -> None:
    opened = thread()
    open_payload = payload(operation="open", before=None, after=opened, expected=0,
                           proposal_id="proposal:comp-open")
    threads, history = reduce_thread(
        (), (), open_payload, event_type="ThreadOpened", logical_time=NOW
    )
    update = transition(
        opened,
        operation="update",
        values=opened.values.model_copy(update={"importance_bp": 7000}),
        transition_id="transition:comp-update",
    )
    updated_threads, updated_history = reduce_thread(
        threads, history, update, event_type="ThreadUpdated", logical_time=NOW
    )
    compensate = transition(
        updated_threads[0],
        operation="compensate",
        values=opened.values,
        transition_id="transition:compensate",
        compensates="transition:comp-update",
    )
    stale = compensate.model_copy(update={"compensates_transition_id": "transition:comp-open"})
    with pytest.raises(ValueError, match="latest"):
        reduce_thread(
            updated_threads, updated_history, stale,
            event_type="ThreadCompensated", logical_time=NOW,
        )
    threads, history = reduce_thread(
        updated_threads, updated_history, compensate,
        event_type="ThreadCompensated", logical_time=NOW
    )
    assert threads[0].values == opened.values
    assert tuple(item.operation for item in history) == ("open", "update", "compensate")


def test_supersession_requires_active_structurally_linked_successor() -> None:
    predecessor = thread()
    successor_values = predecessor.values.model_copy(update={
        "kind": "question_pending",
        "predecessor_thread_refs": (predecessor.thread_id,),
    })
    successor_origin = ThreadOrigin(
        change_id="change:successor", transition_id="transition:successor",
        policy_refs=POLICY, accepted_event_ref="event:successor",
    )
    successor = ThreadProjection(
        thread_id="thread:successor", entity_revision=1,
        semantic_fingerprint=thread_semantic_fingerprint(
            kind=successor_values.kind, subject_ref=successor_values.subject_ref,
            conversation_ref=successor_values.conversation_ref,
            anchor_evidence_refs=successor_values.anchor_evidence_refs,
            resolution_contract_ref=successor_values.resolution_contract_ref,
            policy_refs=POLICY,
        ),
        values=successor_values, origin=successor_origin,
        opened_at=NOW, updated_at=NOW,
    )
    supersede_values = predecessor.values.model_copy(update={
        "status": "superseded", "superseded_by_thread_ref": successor.thread_id,
    })
    supersede = transition(
        predecessor, operation="supersede", values=supersede_values,
        transition_id="transition:supersede",
    )
    updated, _ = reduce_thread(
        (predecessor, successor), (), supersede,
        event_type="ThreadSuperseded", logical_time=NOW,
    )
    assert updated[0].values.status == "superseded"
    unlinked = successor.model_copy(update={
        "values": successor.values.model_copy(update={"predecessor_thread_refs": ()})
    })
    with pytest.raises(ValueError, match="structurally linked"):
        reduce_thread(
            (predecessor, unlinked), (), supersede,
            event_type="ThreadSuperseded", logical_time=NOW,
        )
    self_target = transition(
        predecessor, operation="supersede",
        values=predecessor.values.model_copy(update={
            "status": "superseded", "superseded_by_thread_ref": predecessor.thread_id,
        }),
        transition_id="transition:self-supersede",
    )
    with pytest.raises(ValueError, match="itself"):
        reduce_thread(
            (predecessor, successor), (), self_target,
            event_type="ThreadSuperseded", logical_time=NOW,
        )


def test_sqlite_thread_roundtrip_rebuild_and_context_isolation(tmp_path) -> None:
    path = tmp_path / "thread.sqlite3"
    ledger = SQLiteWorldLedger(path=path, world_id=WORLD)
    ledger.commit([event("world:start", "WorldStarted", {})],
                  expected_world_revision=0, expected_deliberation_revision=0)
    ledger.commit([event("clock:start", "ClockAdvanced", {
        "logical_time_from": "2026-07-15T09:59:00+00:00",
        "logical_time_to": NOW.isoformat(),
    })], expected_world_revision=1, expected_deliberation_revision=0)
    ledger.commit([event("operator:evidence", "OperatorObservationRecorded", {
        "observation_id": "operator:thread", "observation_hash": "a" * 64,
    })], expected_world_revision=2, expected_deliberation_revision=0)
    value = payload(operation="open", before=None, after=thread(), expected=0,
                    proposal_id="proposal:sqlite")
    record_accept_mutate(ledger, value)
    expected = ledger.project()
    snapshot = InternalProjectionReader(ledger=ledger).snapshot(world_id=WORLD)
    assert snapshot.conversation_threads == ()
    assert snapshot.pending_actions == ()
    assert expected.affect_episodes == expected.relationship_states == ()
    ledger.close()
    reopened = SQLiteWorldLedger(path=path, world_id=WORLD)
    assert reopened.project() == expected
    assert reopened.rebuild() == expected
    reopened.close()
    with sqlite3.connect(path) as connection:
        raw = json.loads(connection.execute(
            "SELECT state_json FROM world_v2_heads WHERE world_id = ?", (WORLD,)
        ).fetchone()[0])
        raw["threads"][0]["values"]["importance_bp"] = 1
        connection.execute(
            "UPDATE world_v2_heads SET state_json = ? WHERE world_id = ?",
            (json.dumps(raw, separators=(",", ":")), WORLD),
        )
    with pytest.raises(LedgerIntegrityError, match="head state( hash)? is invalid"):
        SQLiteWorldLedger(path=path, world_id=WORLD)


def test_multiple_threads_can_share_one_clock_but_not_one_transition_id() -> None:
    clock = EvidenceRef(
        ref_id=f"clock:{LATER.isoformat()}", evidence_type="clock_observation",
        claim_purpose="conversation_continuity",
    )

    def expiring(current: ThreadProjection, transition_id: str) -> ThreadExpiredPayload:
        after = ThreadProjection(
            thread_id=current.thread_id,
            entity_revision=2,
            semantic_fingerprint=current.semantic_fingerprint,
            values=current.values.model_copy(update={
                "source_evidence_refs": (*current.values.source_evidence_refs, clock),
                "status": "expired",
            }),
            origin=ThreadOrigin(
                authority_mode="mechanical_clock",
                change_id=f"change:{transition_id}", transition_id=transition_id,
                policy_refs=POLICY, accepted_event_ref=f"event:{transition_id}",
            ),
            opened_at=NOW, updated_at=LATER,
        )
        return ThreadExpiredPayload(
            change_id=f"change:{transition_id}", transition_id=transition_id,
            expected_entity_revision=1, thread_before=current, thread_after=after,
            clock_evidence_ref=clock, clock_event_ref="clock:shared",
            clock_event_payload_hash="c" * 64,
            policy_version=THREAD_EXPIRY_POLICY_VERSION,
            policy_digest=THREAD_EXPIRY_POLICY_DIGEST,
            expires_at=LATER,
        )

    first = thread(expires_at=LATER)
    second_values = first.values.model_copy(update={"kind": "question_pending"})
    second = ThreadProjection(
        thread_id="thread:2", entity_revision=1,
        semantic_fingerprint=thread_semantic_fingerprint(
            kind=second_values.kind, subject_ref=second_values.subject_ref,
            conversation_ref=second_values.conversation_ref,
            anchor_evidence_refs=second_values.anchor_evidence_refs,
            resolution_contract_ref=second_values.resolution_contract_ref,
            policy_refs=POLICY,
        ),
        values=second_values,
        origin=ThreadOrigin(
            change_id="change:second", transition_id="transition:second-open",
            policy_refs=POLICY, accepted_event_ref="event:second-open",
        ),
        opened_at=NOW, updated_at=NOW,
    )
    first_expiry = expiring(first, "transition:shared-expiry-a")
    threads, history = expire_thread(
        (first, second), (), first_expiry, logical_time=LATER
    )
    duplicate = expiring(second, "transition:shared-expiry-a")
    with pytest.raises(ValueError, match="transition identity"):
        expire_thread(threads, history, duplicate, logical_time=LATER)
    second_expiry = expiring(second, "transition:shared-expiry-b")
    threads, history = expire_thread(
        threads, history, second_expiry, logical_time=LATER
    )
    assert tuple(item.values.status for item in threads) == ("expired", "expired")
    assert len(history) == 2


def test_sqlite_verified_v9_head_migrates_to_v11(tmp_path) -> None:
    path = tmp_path / "thread-v9.sqlite3"
    ledger = SQLiteWorldLedger(path=path, world_id=WORLD)
    ledger.commit([event("world:v9", "WorldStarted", {})],
                  expected_world_revision=0, expected_deliberation_revision=0)
    ledger.close()
    with sqlite3.connect(path) as connection:
        state_json = connection.execute(
            "SELECT state_json FROM world_v2_heads WHERE world_id = ?", (WORLD,)
        ).fetchone()[0]
        state = ReducerState.model_validate_json(state_json)
        semantic = state.semantic_payload(
            world_id=WORLD, world_revision=1,
            reducer_bundle_version="world-v2-reducers.9",
        )
        legacy_hash = hashlib.sha256(json.dumps(
            semantic, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()).hexdigest()
        connection.execute(
            "UPDATE world_v2_heads SET state_json = ?, semantic_hash = ?, reducer_bundle_version = ? "
            "WHERE world_id = ?",
            (legacy_state_json(state_json), legacy_hash, "world-v2-reducers.9", WORLD),
        )
    migrated = SQLiteWorldLedger(path=path, world_id=WORLD)
    assert migrated.project().reducer_bundle_version == "world-v2-reducers.17"
    assert migrated.project().threads == ()
    assert migrated.rebuild() == migrated.project()
    migrated.close()


@pytest.mark.parametrize("legacy_bundle", ["world-v2-reducers.10", "world-v2-reducers.11"])
def test_sqlite_verified_thread_authority_head_migrates_to_v12(
    tmp_path, legacy_bundle: str
) -> None:
    path = tmp_path / f"thread-{legacy_bundle.rsplit('.', 1)[-1]}.sqlite3"
    ledger = SQLiteWorldLedger(path=path, world_id=WORLD)
    ledger.commit([event("world:v10", "WorldStarted", {})],
                  expected_world_revision=0, expected_deliberation_revision=0)
    ledger.commit([event("clock:v10", "ClockAdvanced", {
        "logical_time_from": "2026-07-15T09:59:00+00:00",
        "logical_time_to": NOW.isoformat(),
    })], expected_world_revision=1, expected_deliberation_revision=0)
    ledger.commit([event("operator:v10", "OperatorObservationRecorded", {
        "observation_id": "operator:thread", "observation_hash": "a" * 64,
    })], expected_world_revision=2, expected_deliberation_revision=0)
    value = payload(operation="open", before=None, after=thread(), expected=0,
                    proposal_id="proposal:v10-thread")
    record_accept_mutate(ledger, value)
    expected_thread = ledger.project().threads
    ledger.close()
    with sqlite3.connect(path) as connection:
        state_json, world_revision = connection.execute(
            "SELECT state_json, world_revision FROM world_v2_heads WHERE world_id = ?",
            (WORLD,),
        ).fetchone()
        state = ReducerState.model_validate_json(state_json)
        semantic = state.semantic_payload(
            world_id=WORLD, world_revision=world_revision,
            reducer_bundle_version=legacy_bundle,
        )
        legacy_hash = hashlib.sha256(json.dumps(
            semantic, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()).hexdigest()
        connection.execute(
            "UPDATE world_v2_heads SET state_json = ?, semantic_hash = ?, reducer_bundle_version = ? "
            "WHERE world_id = ?",
            (legacy_state_json(state_json), legacy_hash, legacy_bundle, WORLD),
        )
    migrated = SQLiteWorldLedger(path=path, world_id=WORLD)
    assert migrated.project().reducer_bundle_version == "world-v2-reducers.17"
    assert migrated.project().threads == expected_thread
    assert migrated.project().commitments == ()
    assert migrated.rebuild() == migrated.project()
    migrated.close()

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import json
import sqlite3

import pytest

from legacy_migration_support import legacy_state_json

from companion_daemon.world_v2.commitment_events import (
    CommitmentChangedPayload,
    CommitmentClockTransitionPayload,
    commitment_mutation_hash,
)
from companion_daemon.world_v2.commitment_reducers import (
    COMMITMENT_DEADLINE_POLICY_DIGEST,
    COMMITMENT_DEADLINE_POLICY_VERSION,
    reduce_commitment,
    reduce_commitment_clock,
)
from companion_daemon.world_v2.event_identity import domain_idempotency_key
from companion_daemon.world_v2.ledger import WorldLedger
from companion_daemon.world_v2.reducers import ReducerState
from companion_daemon.world_v2.sqlite_ledger import SQLiteWorldLedger
from companion_daemon.world_v2.schemas import (
    CommitmentOrigin,
    CommitmentFulfillmentContract,
    CommitmentProjection,
    CommitmentProposalProjection,
    CommitmentTransitionProjection,
    CommitmentProposedMutation,
    CommitmentValues,
    CommittedWorldEventRef,
    Action,
    ClaimLease,
    ExecutionReceipt,
    ThreadOrigin,
    ThreadProjection,
    ThreadTransitionProjection,
    ThreadValues,
    DueWindow,
    EvidenceRef,
    WorldEvent,
    commitment_semantic_fingerprint,
    thread_semantic_fingerprint,
)


NOW = datetime(2026, 7, 15, 10, 0, tzinfo=UTC)
DUE = NOW + timedelta(hours=1)
CLOSE = NOW + timedelta(hours=2)
WORLD = "world-commitment-authority"
POLICY = ("policy:commitment-v1",)


def event(event_id: str, event_type: str, payload: dict[str, object], *, at=NOW) -> WorldEvent:
    return WorldEvent.from_payload(
        schema_version="world-v2.1", event_id=event_id, world_id=WORLD,
        event_type=event_type, logical_time=at, created_at=at,
        actor="system:test", source="test", trace_id="trace:commitment",
        causation_id=f"cause:{event_id}", correlation_id="correlation:commitment",
        idempotency_key=domain_idempotency_key(
            event_type=event_type, world_id=WORLD, payload=payload
        ) or f"identity:{event_id}", payload=payload,
    )


def evidence(ref_id: str = "operator:commitment", digest: str = "a" * 64) -> EvidenceRef:
    return EvidenceRef(
        ref_id=ref_id, evidence_type="operator_observation",
        claim_purpose="conversation_continuity", immutable_hash=digest,
    )


def commitment(
    *, commitment_id: str = "commitment:1", revision: int = 1,
    status: str = "open", sources: tuple[EvidenceRef, ...] | None = None,
    updated_at: datetime = NOW, settlement_ref: str | None = None,
    settlement_reason: str | None = None, authority_mode: str = "accepted_proposal",
    accepted_event_ref: str | None = None,
) -> CommitmentProjection:
    refs = sources or (evidence(),)
    values = CommitmentValues(
        owner_ref="actor:companion", subject_ref="subject:user-day",
        content_ref="commitment-content:listen-later", content_hash="c" * 64,
        anchor_evidence_refs=(evidence(),), source_evidence_refs=refs,
        importance_bp=6500, due_window=DueWindow(opens_at=DUE, closes_at=CLOSE),
        persistence_level="session",
        fulfillment_contract=CommitmentFulfillmentContract(
            contract_kind="execution_receipt",
            evidence_type="settled_external_result",
            expected_action_id="action:listen-later",
            expected_action_payload_hash="b" * 64,
            expected_result_status="delivered",
            contract_version="commitment-fulfillment-contract.1",
        ),
        privacy_class="private",
        status=status, settlement_evidence_ref=settlement_ref,
        settlement_reason_code=settlement_reason,
    )
    origin = CommitmentOrigin(
        authority_mode=authority_mode, change_id=f"change:{commitment_id}:{revision}",
        transition_id=f"transition:{commitment_id}:{revision}", policy_refs=POLICY,
        accepted_event_ref=accepted_event_ref or f"event:commitment:{commitment_id}:{revision}",
    )
    return CommitmentProjection(
        commitment_id=commitment_id, entity_revision=revision,
        semantic_fingerprint=commitment_semantic_fingerprint(
            owner_ref=values.owner_ref, subject_ref=values.subject_ref,
            content_ref=values.content_ref, content_hash=values.content_hash,
            anchor_evidence_refs=values.anchor_evidence_refs,
            fulfillment_contract=values.fulfillment_contract,
            policy_refs=POLICY,
        ),
        values=values, origin=origin, opened_at=NOW, updated_at=updated_at,
    )


def changed(*, operation: str, before: CommitmentProjection | None,
            after: CommitmentProjection, proposal_id: str,
            world_revision: int) -> CommitmentChangedPayload:
    raw = {
        "change_id": after.origin.change_id,
        "transition_id": after.origin.transition_id,
        "expected_entity_revision": before.entity_revision if before else 0,
        "evidence_refs": after.values.source_evidence_refs,
        "policy_refs": POLICY,
        "acceptance_id": f"acceptance:{proposal_id}", "proposal_id": proposal_id,
        "evaluated_world_revision": world_revision, "accepted_change_hash": "0" * 64,
        "operation": operation, "commitment_before": before, "commitment_after": after,
    }
    raw["accepted_change_hash"] = commitment_mutation_hash(raw)
    return CommitmentChangedPayload.model_validate(raw)


def proposal(value: CommitmentChangedPayload) -> CommitmentProposalProjection:
    return CommitmentProposalProjection(
        proposal_id=value.proposal_id, proposal_encoding="typed-authority-v1",
        authority_contract_ref="proposal-contract:commitment.1",
        transition_kind=value.operation, change_id=value.change_id,
        transition_id=value.transition_id,
        evaluated_world_revision=value.evaluated_world_revision,
        expected_entity_revision=value.expected_entity_revision,
        proposed_change_hash=value.accepted_change_hash,
        evidence_refs=value.evidence_refs, policy_refs=value.policy_refs,
        proposed_mutation=CommitmentProposedMutation(
            event_type={
                "open": "PrivateCommitmentOpened",
                "fulfill": "PrivateCommitmentFulfilled",
                "break": "PrivateCommitmentBroken",
                "release": "PrivateCommitmentReleased",
            }[value.operation],
            payload_json=json.dumps(value.model_dump(mode="json"), ensure_ascii=False,
                                    sort_keys=True, separators=(",", ":")),
        ),
    )


def initialized() -> WorldLedger:
    ledger = WorldLedger.in_memory(world_id=WORLD)
    ledger.commit([event("world:start", "WorldStarted", {})],
                  expected_world_revision=0, expected_deliberation_revision=0)
    ledger.commit([event("clock:start", "ClockAdvanced", {
        "logical_time_from": (NOW - timedelta(minutes=1)).isoformat(),
        "logical_time_to": NOW.isoformat(),
    })], expected_world_revision=1, expected_deliberation_revision=0)
    for index, (ref, digest) in enumerate((("operator:commitment", "a" * 64),)):
        projection = ledger.project()
        ledger.commit([event(f"operator:{index}", "OperatorObservationRecorded", {
            "observation_id": ref, "observation_hash": digest,
        })], expected_world_revision=projection.world_revision,
           expected_deliberation_revision=projection.deliberation_revision)
    return ledger


def accept(ledger: WorldLedger, value: CommitmentChangedPayload) -> None:
    p = ledger.project()
    ledger.commit([event(f"event:{value.proposal_id}", "ProposalRecorded",
                         proposal(value).model_dump(mode="json"))],
                  expected_world_revision=p.world_revision,
                  expected_deliberation_revision=p.deliberation_revision)
    p = ledger.project()
    ledger.commit([
        event(f"event:{value.acceptance_id}", "AcceptanceRecorded", {
            "acceptance_id": value.acceptance_id, "status": "accepted",
            "proposal_id": value.proposal_id,
            "evaluated_world_revision": value.evaluated_world_revision,
            "accepted_change_id": value.change_id,
            "accepted_change_hash": value.accepted_change_hash,
        }),
        event(value.commitment_after.origin.accepted_event_ref,
              proposal(value).proposed_mutation.event_type,
              value.model_dump(mode="json")),
    ], expected_world_revision=p.world_revision,
       expected_deliberation_revision=p.deliberation_revision)


def test_commitment_open_is_typed_persistent_and_has_no_cross_domain_side_effects() -> None:
    ledger = initialized()
    p = ledger.project()
    value = changed(operation="open", before=None, after=commitment(),
                    proposal_id="proposal:open", world_revision=p.world_revision)
    accept(ledger, value)
    projected = ledger.project()
    assert projected.commitments == (value.commitment_after,)
    assert tuple(item.operation for item in projected.commitment_transitions) == ("open",)
    assert projected.actions == projected.pending_actions == ()
    assert projected.threads == projected.affect_episodes == projected.relationship_states == ()


def test_duplicate_active_semantics_and_non_adjacent_acceptance_fail_closed() -> None:
    ledger = initialized()
    p = ledger.project()
    first = changed(operation="open", before=None, after=commitment(),
                    proposal_id="proposal:first", world_revision=p.world_revision)
    accept(ledger, first)
    p = ledger.project()
    duplicate = changed(
        operation="open", before=None, after=commitment(commitment_id="commitment:other"),
        proposal_id="proposal:duplicate", world_revision=p.world_revision,
    )
    with pytest.raises(ValueError, match="active semantic fingerprint"):
        ledger.commit([event("proposal:duplicate", "ProposalRecorded",
                             proposal(duplicate).model_dump(mode="json"))],
                      expected_world_revision=p.world_revision,
                      expected_deliberation_revision=p.deliberation_revision)


def test_fulfillment_requires_new_exact_contract_evidence() -> None:
    ledger = initialized()
    p = ledger.project()
    opened_payload = changed(operation="open", before=None, after=commitment(),
                             proposal_id="proposal:open-fulfill", world_revision=p.world_revision)
    accept(ledger, opened_payload)
    p = ledger.project()
    ledger.commit([event("operator:wrong", "OperatorObservationRecorded", {
        "observation_id": "operator:released", "observation_hash": "d" * 64,
    })], expected_world_revision=p.world_revision,
       expected_deliberation_revision=p.deliberation_revision)
    current = opened_payload.commitment_after
    wrong = evidence("operator:released", "d" * 64)
    values = current.values.model_copy(update={
        "source_evidence_refs": (*current.values.source_evidence_refs, wrong),
        "status": "fulfilled", "settlement_evidence_ref": wrong.ref_id,
        "settlement_reason_code": "evidence_satisfied",
    })
    after = commitment(revision=2, status="fulfilled",
        sources=values.source_evidence_refs, settlement_ref=wrong.ref_id,
        settlement_reason="evidence_satisfied").model_copy(update={
            "values": values, "opened_at": current.opened_at,
            "origin": commitment(revision=2).origin.model_copy(update={
                "accepted_event_ref": "event:wrong-fulfillment"}),
        })
    p = ledger.project()
    value = changed(operation="fulfill", before=current, after=after,
                    proposal_id="proposal:wrong-fulfill", world_revision=p.world_revision)
    with pytest.raises(ValueError, match="fulfillment contract"):
        ledger.commit([event("proposal:wrong-fulfill", "ProposalRecorded",
                             proposal(value).model_dump(mode="json"))],
                      expected_world_revision=p.world_revision,
                      expected_deliberation_revision=p.deliberation_revision)


def test_clock_due_and_deadline_broken_are_mechanical_and_behavior_neutral() -> None:
    ledger = initialized()
    p = ledger.project()
    opened_payload = changed(operation="open", before=None, after=commitment(),
                             proposal_id="proposal:open-clock", world_revision=p.world_revision)
    accept(ledger, opened_payload)
    current = opened_payload.commitment_after
    due_clock = event("clock:due", "ClockAdvanced", {
        "logical_time_from": NOW.isoformat(), "logical_time_to": DUE.isoformat(),
    }, at=DUE)
    due_ref = EvidenceRef(ref_id=f"clock:{DUE.isoformat()}",
        evidence_type="clock_observation", claim_purpose="conversation_continuity")
    due_after = current.model_copy(update={
        "entity_revision": 2, "updated_at": DUE,
        "values": current.values.model_copy(update={
            "source_evidence_refs": (*current.values.source_evidence_refs, due_ref),
            "status": "due",
        }),
        "origin": current.origin.model_copy(update={
            "authority_mode": "mechanical_clock", "change_id": "change:due",
            "transition_id": "transition:due", "accepted_event_ref": "event:due",
        }),
    })
    due = CommitmentClockTransitionPayload(
        change_id="change:due", transition_id="transition:due", operation="due",
        expected_entity_revision=1, commitment_before=current, commitment_after=due_after,
        clock_evidence_ref=due_ref, clock_event_ref=due_clock.event_id,
        clock_event_payload_hash=due_clock.payload_hash,
        policy_version=COMMITMENT_DEADLINE_POLICY_VERSION,
        policy_digest=COMMITMENT_DEADLINE_POLICY_DIGEST,
    )
    p = ledger.project()
    with pytest.raises(ValueError, match="logical time|ClockAdvanced"):
        ledger.commit([event("event:due", "PrivateCommitmentDue", due.model_dump(mode="json"), at=DUE)],
                      expected_world_revision=p.world_revision,
                      expected_deliberation_revision=p.deliberation_revision)
    ledger.commit([due_clock], expected_world_revision=p.world_revision,
                  expected_deliberation_revision=p.deliberation_revision)
    p = ledger.project()
    ledger.commit([event("event:due", "PrivateCommitmentDue", due.model_dump(mode="json"), at=DUE)],
                  expected_world_revision=p.world_revision,
                  expected_deliberation_revision=p.deliberation_revision)
    assert ledger.project().commitments[0].values.status == "due"
    assert ledger.project().actions == ()

    current = ledger.project().commitments[0]
    close_clock = event("clock:close", "ClockAdvanced", {
        "logical_time_from": DUE.isoformat(), "logical_time_to": CLOSE.isoformat(),
    }, at=CLOSE)
    close_ref = EvidenceRef(ref_id=f"clock:{CLOSE.isoformat()}",
        evidence_type="clock_observation", claim_purpose="conversation_continuity")
    broken_after = current.model_copy(update={
        "entity_revision": 3, "updated_at": CLOSE,
        "values": current.values.model_copy(update={
            "source_evidence_refs": (*current.values.source_evidence_refs, close_ref),
            "status": "broken", "settlement_evidence_ref": close_ref.ref_id,
            "settlement_reason_code": "deadline_elapsed",
        }),
        "origin": current.origin.model_copy(update={
            "authority_mode": "mechanical_clock", "change_id": "change:broken",
            "transition_id": "transition:broken", "accepted_event_ref": "event:broken",
        }),
    })
    broken = CommitmentClockTransitionPayload(
        change_id="change:broken", transition_id="transition:broken", operation="break",
        expected_entity_revision=2, commitment_before=current,
        commitment_after=broken_after, clock_evidence_ref=close_ref,
        clock_event_ref=close_clock.event_id, clock_event_payload_hash=close_clock.payload_hash,
        policy_version=COMMITMENT_DEADLINE_POLICY_VERSION,
        policy_digest=COMMITMENT_DEADLINE_POLICY_DIGEST,
    )
    p = ledger.project()
    ledger.commit([close_clock], expected_world_revision=p.world_revision,
                  expected_deliberation_revision=p.deliberation_revision)
    p = ledger.project()
    ledger.commit([event("event:broken", "PrivateCommitmentDeadlineBroken",
                         broken.model_dump(mode="json"), at=CLOSE)],
                  expected_world_revision=p.world_revision,
                  expected_deliberation_revision=p.deliberation_revision)
    projected = ledger.project()
    assert projected.commitments[0].values.status == "broken"
    assert projected.actions == projected.threads == projected.relationship_states == ()


def test_terminal_commitment_cannot_reopen_or_resettle() -> None:
    ledger = initialized()
    p = ledger.project()
    opened_payload = changed(operation="open", before=None, after=commitment(),
                             proposal_id="proposal:terminal-open", world_revision=p.world_revision)
    accept(ledger, opened_payload)
    p = ledger.project()
    ledger.commit([event("message:release", "ObservationRecorded", {
        "schema_version": "world-v2.1", "observation_kind": "message",
        "observation_id": "message:release", "world_id": WORLD,
        "logical_time": NOW.isoformat(), "created_at": NOW.isoformat(),
        "trace_id": "trace:commitment", "causation_id": "cause:message:release",
        "correlation_id": "correlation:commitment", "source": "test",
        "source_event_id": "source:release", "actor": "system:test",
        "channel": "direct_message", "payload_ref": "payload:release",
        "payload_hash": "d" * 64, "received_at": NOW.isoformat(),
    })], expected_world_revision=p.world_revision,
       expected_deliberation_revision=p.deliberation_revision)
    current = opened_payload.commitment_after
    observed = ledger.project().message_observations[-1]
    fulfilled_ref = EvidenceRef(
        ref_id=observed.observation_id, evidence_type="observed_message",
        claim_purpose="conversation_continuity",
        source_world_revision=observed.world_revision,
        immutable_hash=observed.event_payload_hash,
    )
    values = current.values.model_copy(update={
        "source_evidence_refs": (*current.values.source_evidence_refs, fulfilled_ref),
        "status": "released", "settlement_evidence_ref": fulfilled_ref.ref_id,
        "settlement_reason_code": "obsolete",
    })
    after = commitment(revision=2, status="released", sources=values.source_evidence_refs,
        settlement_ref=fulfilled_ref.ref_id, settlement_reason="obsolete").model_copy(update={
            "values": values, "opened_at": current.opened_at,
            "origin": commitment(revision=2).origin.model_copy(update={
                "accepted_event_ref": "event:released"}),
        })
    p = ledger.project()
    fulfilled = changed(operation="release", before=current, after=after,
                        proposal_id="proposal:release", world_revision=p.world_revision)
    accept(ledger, fulfilled)
    assert ledger.project().commitments[0].values.status == "released"
    p = ledger.project()
    attempted = changed(operation="release", before=after,
        after=after.model_copy(update={
            "entity_revision": 3,
            "values": after.values.model_copy(update={
                "status": "released", "settlement_reason_code": "obsolete"}),
            "origin": after.origin.model_copy(update={
                "change_id": "change:resettle", "transition_id": "transition:resettle",
                "accepted_event_ref": "event:resettle"}),
        }), proposal_id="proposal:resettle", world_revision=p.world_revision)
    with pytest.raises(ValueError, match="terminal"):
        ledger.commit([event("proposal:resettle", "ProposalRecorded",
                             proposal(attempted).model_dump(mode="json"))],
                      expected_world_revision=p.world_revision,
                      expected_deliberation_revision=p.deliberation_revision)


def test_commitment_mutation_hash_is_stable_and_tamper_sensitive() -> None:
    value = changed(operation="open", before=None, after=commitment(),
                    proposal_id="proposal:hash", world_revision=6)
    raw = value.model_dump(mode="json")
    reordered = dict(reversed(tuple(json.loads(json.dumps(raw)).items())))
    assert commitment_mutation_hash(raw) == commitment_mutation_hash(reordered)
    reordered["commitment_after"]["values"]["importance_bp"] += 1
    assert commitment_mutation_hash(raw) != commitment_mutation_hash(reordered)


def test_privacy_uses_private_minimum_and_naive_due_time_fails_schema() -> None:
    for privacy_class in ("public", "shareable", "personal"):
        broad = commitment(commitment_id=f"commitment:{privacy_class}").model_copy(update={
            "values": commitment().values.model_copy(update={"privacy_class": privacy_class})
        })
        ledger = initialized()
        p = ledger.project()
        value = changed(operation="open", before=None, after=broad,
                        proposal_id=f"proposal:{privacy_class}", world_revision=p.world_revision)
        with pytest.raises(ValueError, match="privacy matrix"):
            ledger.commit([event(f"proposal:{privacy_class}", "ProposalRecorded",
                                 proposal(value).model_dump(mode="json"))],
                          expected_world_revision=p.world_revision,
                          expected_deliberation_revision=p.deliberation_revision)
    with pytest.raises(ValueError, match="timezone-aware"):
        CommitmentValues.model_validate(
            commitment().values.model_dump()
            | {"due_window": {"opens_at": datetime(2026, 7, 15, 11),
                              "closes_at": datetime(2026, 7, 15, 12)}}
        )


def test_operator_observation_cannot_release_commitment_without_chronology() -> None:
    ledger = initialized()
    p = ledger.project()
    opened = changed(operation="open", before=None, after=commitment(),
                     proposal_id="proposal:operator-open", world_revision=p.world_revision)
    accept(ledger, opened)
    p = ledger.project()
    ledger.commit([event("operator:later", "OperatorObservationRecorded", {
        "observation_id": "operator:later", "observation_hash": "f" * 64,
    })], expected_world_revision=p.world_revision,
       expected_deliberation_revision=p.deliberation_revision)
    source = evidence("operator:later", "f" * 64)
    current = opened.commitment_after
    values = current.values.model_copy(update={
        "source_evidence_refs": (*current.values.source_evidence_refs, source),
        "status": "released", "settlement_evidence_ref": source.ref_id,
        "settlement_reason_code": "obsolete",
    })
    after = current.model_copy(update={
        "entity_revision": 2, "values": values,
        "origin": current.origin.model_copy(update={
            "change_id": "change:operator-release",
            "transition_id": "transition:operator-release",
            "accepted_event_ref": "event:operator-release",
        }),
    })
    p = ledger.project()
    release = changed(operation="release", before=current, after=after,
                      proposal_id="proposal:operator-release", world_revision=p.world_revision)
    with pytest.raises(ValueError, match="postdate opening authority"):
        ledger.commit([event("proposal:operator-release", "ProposalRecorded",
                             proposal(release).model_dump(mode="json"))],
                      expected_world_revision=p.world_revision,
                      expected_deliberation_revision=p.deliberation_revision)


def test_accepted_break_reason_and_predecessor_lineage_fail_closed() -> None:
    ledger = initialized()
    p = ledger.project()
    opened = changed(operation="open", before=None, after=commitment(),
                     proposal_id="proposal:break-open", world_revision=p.world_revision)
    accept(ledger, opened)
    p = ledger.project()
    ledger.commit([event("operator:failure", "OperatorObservationRecorded", {
        "observation_id": "operator:failure", "observation_hash": "f" * 64,
    })], expected_world_revision=p.world_revision,
       expected_deliberation_revision=p.deliberation_revision)
    source = evidence("operator:failure", "f" * 64)
    current = opened.commitment_after
    values = current.values.model_copy(update={
        "source_evidence_refs": (*current.values.source_evidence_refs, source),
        "status": "broken", "settlement_evidence_ref": source.ref_id,
        "settlement_reason_code": "deadline_elapsed",
    })
    after = current.model_copy(update={
        "entity_revision": 2, "values": values,
        "origin": current.origin.model_copy(update={
            "change_id": "change:bad-break", "transition_id": "transition:bad-break",
            "accepted_event_ref": "event:bad-break",
        }),
    })
    p = ledger.project()
    broken = changed(operation="break", before=current, after=after,
                     proposal_id="proposal:bad-break", world_revision=p.world_revision)
    with pytest.raises(ValueError, match="authoritative failure reason"):
        ledger.commit([event("proposal:bad-break", "ProposalRecorded",
                             proposal(broken).model_dump(mode="json"))],
                      expected_world_revision=p.world_revision,
                      expected_deliberation_revision=p.deliberation_revision)

    ledger = initialized()
    candidate = commitment(commitment_id="commitment:replacement")
    values = candidate.values.model_copy(update={
        "predecessor_commitment_ref": "commitment:missing", "lineage_kind": "replacement"
    })
    candidate = candidate.model_copy(update={
        "values": values,
        "semantic_fingerprint": commitment_semantic_fingerprint(
            owner_ref=values.owner_ref, subject_ref=values.subject_ref,
            content_ref=values.content_ref, content_hash=values.content_hash,
            anchor_evidence_refs=values.anchor_evidence_refs,
            fulfillment_contract=values.fulfillment_contract,
            predecessor_commitment_ref=values.predecessor_commitment_ref,
            lineage_kind=values.lineage_kind, policy_refs=POLICY,
        ),
    })
    p = ledger.project()
    replacement = changed(operation="open", before=None, after=candidate,
                          proposal_id="proposal:missing-predecessor",
                          world_revision=p.world_revision)
    with pytest.raises(ValueError, match="predecessor"):
        ledger.commit([event("proposal:missing-predecessor", "ProposalRecorded",
                             proposal(replacement).model_dump(mode="json"))],
                      expected_world_revision=p.world_revision,
                      expected_deliberation_revision=p.deliberation_revision)


def test_sqlite_commitment_roundtrip_and_rebuild(tmp_path) -> None:
    path = tmp_path / "commitment.sqlite3"
    ledger = SQLiteWorldLedger(path=path, world_id=WORLD)
    ledger.commit([event("world:sqlite", "WorldStarted", {})],
                  expected_world_revision=0, expected_deliberation_revision=0)
    ledger.commit([event("clock:sqlite", "ClockAdvanced", {
        "logical_time_from": (NOW - timedelta(minutes=1)).isoformat(),
        "logical_time_to": NOW.isoformat(),
    })], expected_world_revision=1, expected_deliberation_revision=0)
    ledger.commit([event("operator:sqlite", "OperatorObservationRecorded", {
        "observation_id": "operator:commitment", "observation_hash": "a" * 64,
    })], expected_world_revision=2, expected_deliberation_revision=0)
    p = ledger.project()
    value = changed(operation="open", before=None, after=commitment(),
                    proposal_id="proposal:sqlite", world_revision=p.world_revision)
    accept(ledger, value)
    expected = ledger.project()
    assert expected.commitments == (value.commitment_after,)
    assert expected.actions == expected.threads == ()
    ledger.close()
    reopened = SQLiteWorldLedger(path=path, world_id=WORLD)
    assert reopened.project() == expected
    assert reopened.rebuild() == expected
    reopened.close()
    with sqlite3.connect(path) as connection:
        state_json, world_revision = connection.execute(
            "SELECT state_json, world_revision FROM world_v2_heads WHERE world_id = ?",
            (WORLD,),
        ).fetchone()
        state = ReducerState.model_validate_json(state_json)
        semantic = state.semantic_payload(
            world_id=WORLD,
            world_revision=world_revision,
            reducer_bundle_version="world-v2-reducers.11",
        )
        legacy_hash = hashlib.sha256(json.dumps(
            semantic,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()).hexdigest()
        connection.execute(
            "UPDATE world_v2_heads SET state_json = ?, semantic_hash = ?, reducer_bundle_version = ? "
            "WHERE world_id = ?",
            (
                legacy_state_json(state_json),
                legacy_hash,
                "world-v2-reducers.11",
                WORLD,
            ),
        )
    migrated = SQLiteWorldLedger(path=path, world_id=WORLD)
    assert migrated.project().reducer_bundle_version == "world-v2-reducers.31"
    assert migrated.project().commitments == expected.commitments
    assert migrated.rebuild() == migrated.project()
    migrated.close()


def test_clock_jump_past_window_records_due_then_break_with_same_clock_authority() -> None:
    ledger = initialized()
    p = ledger.project()
    opened = changed(operation="open", before=None, after=commitment(),
                     proposal_id="proposal:jump-open", world_revision=p.world_revision)
    accept(ledger, opened)
    clock = event("clock:jump", "ClockAdvanced", {
        "logical_time_from": NOW.isoformat(), "logical_time_to": CLOSE.isoformat(),
    }, at=CLOSE)
    p = ledger.project()
    ledger.commit([clock], expected_world_revision=p.world_revision,
                  expected_deliberation_revision=p.deliberation_revision)
    clock_ref = EvidenceRef(
        ref_id=f"clock:{CLOSE.isoformat()}", evidence_type="clock_observation",
        claim_purpose="conversation_continuity",
    )
    current = opened.commitment_after
    due_after = current.model_copy(update={
        "entity_revision": 2, "updated_at": CLOSE,
        "values": current.values.model_copy(update={
            "source_evidence_refs": (*current.values.source_evidence_refs, clock_ref),
            "status": "due",
        }),
        "origin": current.origin.model_copy(update={
            "authority_mode": "mechanical_clock", "change_id": "change:jump-due",
            "transition_id": "transition:jump-due", "accepted_event_ref": "event:jump-due",
        }),
    })
    due = CommitmentClockTransitionPayload(
        change_id="change:jump-due", transition_id="transition:jump-due", operation="due",
        expected_entity_revision=1, commitment_before=current, commitment_after=due_after,
        clock_evidence_ref=clock_ref, clock_event_ref=clock.event_id,
        clock_event_payload_hash=clock.payload_hash,
        policy_version=COMMITMENT_DEADLINE_POLICY_VERSION,
        policy_digest=COMMITMENT_DEADLINE_POLICY_DIGEST,
    )
    p = ledger.project()
    ledger.commit([event("event:jump-due", "PrivateCommitmentDue",
                         due.model_dump(mode="json"), at=CLOSE)],
                  expected_world_revision=p.world_revision,
                  expected_deliberation_revision=p.deliberation_revision)
    broken_after = due_after.model_copy(update={
        "entity_revision": 3,
        "values": due_after.values.model_copy(update={
            "status": "broken", "settlement_evidence_ref": clock_ref.ref_id,
            "settlement_reason_code": "deadline_elapsed",
        }),
        "origin": due_after.origin.model_copy(update={
            "change_id": "change:jump-break", "transition_id": "transition:jump-break",
            "accepted_event_ref": "event:jump-break",
        }),
    })
    broken = CommitmentClockTransitionPayload(
        change_id="change:jump-break", transition_id="transition:jump-break",
        operation="break", expected_entity_revision=2, commitment_before=due_after,
        commitment_after=broken_after, clock_evidence_ref=clock_ref,
        clock_event_ref=clock.event_id, clock_event_payload_hash=clock.payload_hash,
        policy_version=COMMITMENT_DEADLINE_POLICY_VERSION,
        policy_digest=COMMITMENT_DEADLINE_POLICY_DIGEST,
    )
    p = ledger.project()
    ledger.commit([event("event:jump-break", "PrivateCommitmentDeadlineBroken",
                         broken.model_dump(mode="json"), at=CLOSE)],
                  expected_world_revision=p.world_revision,
                  expected_deliberation_revision=p.deliberation_revision)
    projected = ledger.project()
    assert projected.commitments[0].values.status == "broken"
    assert projected.commitments[0].values.source_evidence_refs.count(clock_ref) == 1
    assert tuple(item.operation for item in projected.commitment_transitions) == (
        "open", "due", "break",
    )


def test_mechanical_origin_and_policy_tamper_fail_closed() -> None:
    current = commitment()
    clock_ref = EvidenceRef(
        ref_id=f"clock:{DUE.isoformat()}", evidence_type="clock_observation",
        claim_purpose="conversation_continuity",
    )
    after = current.model_copy(update={
        "entity_revision": 2, "updated_at": DUE,
        "values": current.values.model_copy(update={
            "source_evidence_refs": (*current.values.source_evidence_refs, clock_ref),
            "status": "due",
        }),
        "origin": current.origin.model_copy(update={
            "authority_mode": "mechanical_clock", "change_id": "change:forged",
            "transition_id": "transition:forged", "accepted_event_ref": "event:forged",
        }),
    })
    raw = {
        "change_id": "change:due", "transition_id": "transition:due",
        "operation": "due", "expected_entity_revision": 1,
        "commitment_before": current, "commitment_after": after,
        "clock_evidence_ref": clock_ref, "clock_event_ref": "clock:event",
        "clock_event_payload_hash": "f" * 64,
        "policy_version": COMMITMENT_DEADLINE_POLICY_VERSION,
        "policy_digest": COMMITMENT_DEADLINE_POLICY_DIGEST,
    }
    forged = CommitmentClockTransitionPayload.model_validate(raw)
    with pytest.raises(ValueError, match="mechanical transition"):
        reduce_commitment_clock((current,), (), forged, logical_time=DUE)
    valid_after = after.model_copy(update={
        "origin": after.origin.model_copy(update={
            "change_id": "change:due", "transition_id": "transition:due",
        })
    })
    bad_policy = CommitmentClockTransitionPayload.model_validate(
        raw | {"commitment_after": valid_after, "policy_digest": "0" * 64}
    )
    with pytest.raises(ValueError, match="policy artifact"):
        reduce_commitment_clock((current,), (), bad_policy, logical_time=DUE)


@pytest.mark.parametrize(
    ("lineage_kind", "predecessor_status", "predecessor_reason"),
    [
        ("correction", "released", "operator_correction"),
        ("replacement", "broken", "deadline_elapsed"),
        ("renewal", "fulfilled", "evidence_satisfied"),
    ],
)
def test_valid_commitment_lineage_kinds(
    lineage_kind: str, predecessor_status: str, predecessor_reason: str
) -> None:
    predecessor = commitment(
        commitment_id=f"commitment:previous:{lineage_kind}",
        status=predecessor_status,
        settlement_ref="operator:commitment",
        settlement_reason=predecessor_reason,
        accepted_event_ref=f"event:previous:{lineage_kind}",
    )
    authority = CommittedWorldEventRef(
        event_id=predecessor.origin.accepted_event_ref,
        event_type="PrivateCommitmentReleased",
        world_revision=9, payload_hash="9" * 64, logical_time=NOW,
    )
    predecessor_evidence = EvidenceRef(
        ref_id=authority.event_id, evidence_type="committed_world_event",
        claim_purpose="conversation_continuity",
        source_world_revision=authority.world_revision,
        immutable_hash=authority.payload_hash,
    )
    candidate = commitment(commitment_id=f"commitment:next:{lineage_kind}")
    values = candidate.values.model_copy(update={
        "anchor_evidence_refs": (predecessor_evidence,),
        "source_evidence_refs": (predecessor_evidence,),
        "predecessor_commitment_ref": predecessor.commitment_id,
        "lineage_kind": lineage_kind,
    })
    candidate = candidate.model_copy(update={
        "values": values,
        "semantic_fingerprint": commitment_semantic_fingerprint(
            owner_ref=values.owner_ref, subject_ref=values.subject_ref,
            content_ref=values.content_ref, content_hash=values.content_hash,
            anchor_evidence_refs=values.anchor_evidence_refs,
            fulfillment_contract=values.fulfillment_contract,
            predecessor_commitment_ref=values.predecessor_commitment_ref,
            lineage_kind=values.lineage_kind, policy_refs=POLICY,
        ),
    })
    opened = changed(operation="open", before=None, after=candidate,
                     proposal_id=f"proposal:lineage:{lineage_kind}", world_revision=10)
    commitments, _ = reduce_commitment(
        (predecessor,), (), opened, event_type="PrivateCommitmentOpened",
        logical_time=NOW, committed_events=(authority,), execution_receipts=(), actions=(),
        threads=(), thread_history=(), message_observations=(),
    )
    assert commitments[-1].values.lineage_kind == lineage_kind
    wrong_kind = {
        "correction": "renewal",
        "replacement": "correction",
        "renewal": "replacement",
    }[lineage_kind]
    wrong_values = values.model_copy(update={"lineage_kind": wrong_kind})
    wrong = candidate.model_copy(update={
        "values": wrong_values,
        "semantic_fingerprint": commitment_semantic_fingerprint(
            owner_ref=wrong_values.owner_ref, subject_ref=wrong_values.subject_ref,
            content_ref=wrong_values.content_ref, content_hash=wrong_values.content_hash,
            anchor_evidence_refs=wrong_values.anchor_evidence_refs,
            fulfillment_contract=wrong_values.fulfillment_contract,
            predecessor_commitment_ref=wrong_values.predecessor_commitment_ref,
            lineage_kind=wrong_values.lineage_kind, policy_refs=POLICY,
        ),
    })
    wrong_open = changed(operation="open", before=None, after=wrong,
                         proposal_id=f"proposal:wrong-lineage:{lineage_kind}",
                         world_revision=10)
    with pytest.raises(ValueError, match="requires"):
        reduce_commitment(
            (predecessor,), (), wrong_open, event_type="PrivateCommitmentOpened",
            logical_time=NOW, committed_events=(authority,), execution_receipts=(),
            actions=(), threads=(), thread_history=(), message_observations=(),
        )


def test_execution_receipt_fulfillment_is_target_and_payload_bound() -> None:
    current = commitment()
    lease = ClaimLease(
        owner_id="pump:test", attempt_id="attempt:1",
        acquired_at=NOW, expires_at=NOW + timedelta(minutes=5),
    )
    action = Action(
        schema_version="world-v2.1", action_id="action:listen-later", world_id=WORLD,
        logical_time=NOW, created_at=NOW, trace_id="trace:action",
        causation_id="cause:action", correlation_id="correlation:action",
        kind="reply", layer="external_action", intent_ref="intent:listen-later",
        actor="actor:companion", target="user:test", payload_ref="payload:listen",
        payload_hash="b" * 64, idempotency_key="action:listen-later:key",
        budget_reservation_id="budget:listen", claim_lease=lease, state="delivered",
        recovery_policy="effect_once",
    )
    receipt = ExecutionReceipt(
        receipt_id="receipt:listen", result_id="result:listen",
        action_id=action.action_id, provider="test", provider_ref="provider:listen",
        source_event_id="event:receipt:listen", receipt_kind="terminal",
        observed_state="delivered", is_terminal=True, cost_actual=1,
        received_at=NOW + timedelta(minutes=1), raw_payload_hash="f" * 64,
    )
    receipt_hash = hashlib.sha256(json.dumps(
        receipt.model_dump(mode="json"), ensure_ascii=False,
        sort_keys=True, separators=(",", ":")
    ).encode()).hexdigest()
    proof = EvidenceRef(
        ref_id=receipt.receipt_id, evidence_type="settled_external_result",
        claim_purpose="conversation_continuity", immutable_hash=receipt_hash,
    )
    values = current.values.model_copy(update={
        "source_evidence_refs": (*current.values.source_evidence_refs, proof),
        "status": "fulfilled", "settlement_evidence_ref": proof.ref_id,
        "settlement_reason_code": "evidence_satisfied",
    })
    after = current.model_copy(update={
        "entity_revision": 2, "updated_at": receipt.received_at, "values": values,
        "origin": current.origin.model_copy(update={
            "change_id": "change:fulfill", "transition_id": "transition:fulfill",
            "accepted_event_ref": "event:fulfill",
        }),
    })
    fulfilled = changed(operation="fulfill", before=current, after=after,
                        proposal_id="proposal:fulfill-receipt", world_revision=8)
    commitments, _ = reduce_commitment(
        (current,), (), fulfilled, event_type="PrivateCommitmentFulfilled",
        logical_time=receipt.received_at, committed_events=(),
        execution_receipts=(receipt,), actions=(action,), threads=(),
        thread_history=(), message_observations=(),
    )
    assert commitments[0].values.status == "fulfilled"
    forged_action = action.model_copy(update={"payload_hash": "0" * 64})
    with pytest.raises(ValueError, match="receipt does not match"):
        reduce_commitment(
            (current,), (), fulfilled, event_type="PrivateCommitmentFulfilled",
            logical_time=receipt.received_at, committed_events=(),
            execution_receipts=(receipt,), actions=(forged_action,), threads=(),
            thread_history=(), message_observations=(),
        )


def test_thread_resolution_fulfillment_is_target_and_transition_bound() -> None:
    thread_event = CommittedWorldEventRef(
        event_id="event:thread-resolved", event_type="ThreadResolved",
        world_revision=12, payload_hash="1" * 64,
        logical_time=NOW + timedelta(minutes=2),
    )
    proof = EvidenceRef(
        ref_id=thread_event.event_id, evidence_type="committed_world_event",
        claim_purpose="conversation_continuity",
        source_world_revision=thread_event.world_revision,
        immutable_hash=thread_event.payload_hash,
    )
    anchor = evidence()
    thread_values = ThreadValues(
        kind="topic_open", subject_ref="subject:user-day", conversation_ref="conversation:1",
        anchor_evidence_refs=(anchor,), source_evidence_refs=(anchor, proof),
        importance_bp=5000, resolution_contract_ref="resolution:understood",
        privacy_class="private", status="resolved", resolution_kind="answered",
        resolution_ref=proof.ref_id,
    )
    thread_origin = ThreadOrigin(
        change_id="change:thread-resolved", transition_id="transition:thread-resolved",
        policy_refs=("policy:thread-v1",), accepted_event_ref=thread_event.event_id,
    )
    thread = ThreadProjection(
        thread_id="thread:target", entity_revision=2,
        semantic_fingerprint=thread_semantic_fingerprint(
            kind=thread_values.kind, subject_ref=thread_values.subject_ref,
            conversation_ref=thread_values.conversation_ref,
            anchor_evidence_refs=thread_values.anchor_evidence_refs,
            resolution_contract_ref=thread_values.resolution_contract_ref,
            policy_refs=thread_origin.policy_refs,
        ),
        values=thread_values, origin=thread_origin, opened_at=NOW,
        updated_at=NOW + timedelta(minutes=2),
    )
    transition = ThreadTransitionProjection(
        transition_id=thread_origin.transition_id, thread_id=thread.thread_id,
        entity_revision=2, operation="resolve", values_before=None,
        values_after=thread.values, change_id=thread_origin.change_id,
        policy_refs=thread_origin.policy_refs,
        accepted_event_ref=thread_event.event_id,
        accepted_at=NOW + timedelta(minutes=2),
    )
    base = commitment()
    contract = CommitmentFulfillmentContract(
        contract_kind="thread_resolution", evidence_type="committed_world_event",
        expected_event_type="ThreadResolved", expected_thread_id=thread.thread_id,
        contract_version="commitment-fulfillment-contract.1",
    )
    base_values = base.values.model_copy(update={"fulfillment_contract": contract})
    current = base.model_copy(update={
        "values": base_values,
        "semantic_fingerprint": commitment_semantic_fingerprint(
            owner_ref=base_values.owner_ref, subject_ref=base_values.subject_ref,
            content_ref=base_values.content_ref, content_hash=base_values.content_hash,
            anchor_evidence_refs=base_values.anchor_evidence_refs,
            fulfillment_contract=contract, policy_refs=POLICY,
        ),
    })
    terminal_values = current.values.model_copy(update={
        "source_evidence_refs": (*current.values.source_evidence_refs, proof),
        "status": "fulfilled", "settlement_evidence_ref": proof.ref_id,
        "settlement_reason_code": "evidence_satisfied",
    })
    after = current.model_copy(update={
        "entity_revision": 2, "updated_at": transition.accepted_at,
        "values": terminal_values,
        "origin": current.origin.model_copy(update={
            "change_id": "change:thread-fulfill", "transition_id": "transition:thread-fulfill",
            "accepted_event_ref": "event:thread-fulfill",
        }),
    })
    fulfilled = changed(operation="fulfill", before=current, after=after,
                        proposal_id="proposal:thread-fulfill", world_revision=13)
    commitments, _ = reduce_commitment(
        (current,), (), fulfilled, event_type="PrivateCommitmentFulfilled",
        logical_time=transition.accepted_at, committed_events=(thread_event,),
        execution_receipts=(), actions=(), threads=(thread,),
        thread_history=(transition,), message_observations=(),
    )
    assert commitments[0].values.status == "fulfilled"
    wrong_transition = transition.model_copy(update={"thread_id": "thread:other"})
    with pytest.raises(ValueError, match="thread resolution does not match"):
        reduce_commitment(
            (current,), (), fulfilled, event_type="PrivateCommitmentFulfilled",
            logical_time=transition.accepted_at, committed_events=(thread_event,),
            execution_receipts=(), actions=(), threads=(thread,),
            thread_history=(wrong_transition,), message_observations=(),
        )


def test_same_time_receipt_recorded_before_open_cannot_release_commitment() -> None:
    current = commitment(accepted_event_ref="event:commitment-open")
    opening = CommitmentTransitionProjection(
        transition_id=current.origin.transition_id,
        commitment_id=current.commitment_id, entity_revision=1, operation="open",
        values_before=None, values_after=current.values, change_id=current.origin.change_id,
        authority_mode="accepted_proposal", accepted_event_ref=current.origin.accepted_event_ref,
        accepted_at=NOW,
    )
    receipt = ExecutionReceipt(
        receipt_id="receipt:old", result_id="result:old", action_id="action:old",
        provider="test", provider_ref="provider:old", source_event_id="event:receipt-old",
        receipt_kind="terminal", observed_state="cancelled", is_terminal=True,
        cost_actual=0, received_at=NOW, raw_payload_hash="f" * 64,
    )
    receipt_hash = hashlib.sha256(json.dumps(
        receipt.model_dump(mode="json"), ensure_ascii=False,
        sort_keys=True, separators=(",", ":")
    ).encode()).hexdigest()
    proof = EvidenceRef(
        ref_id=receipt.receipt_id, evidence_type="settled_external_result",
        claim_purpose="conversation_continuity", immutable_hash=receipt_hash,
    )
    values = current.values.model_copy(update={
        "source_evidence_refs": (*current.values.source_evidence_refs, proof),
        "status": "released", "settlement_evidence_ref": proof.ref_id,
        "settlement_reason_code": "obsolete",
    })
    after = current.model_copy(update={
        "entity_revision": 2, "values": values,
        "origin": current.origin.model_copy(update={
            "change_id": "change:release-old", "transition_id": "transition:release-old",
            "accepted_event_ref": "event:release-old",
        }),
    })
    release = changed(operation="release", before=current, after=after,
                      proposal_id="proposal:release-old", world_revision=11)
    old_receipt_event = CommittedWorldEventRef(
        event_id=receipt.source_event_id, event_type="ExecutionReceiptRecorded",
        world_revision=9, payload_hash="9" * 64, logical_time=NOW,
    )
    open_event = CommittedWorldEventRef(
        event_id=current.origin.accepted_event_ref, event_type="PrivateCommitmentOpened",
        world_revision=10, payload_hash="a" * 64, logical_time=NOW,
    )
    with pytest.raises(ValueError, match="postdate opening authority"):
        reduce_commitment(
            (current,), (opening,), release, event_type="PrivateCommitmentReleased",
            logical_time=NOW, committed_events=(old_receipt_event, open_event),
            execution_receipts=(receipt,), actions=(), threads=(), thread_history=(),
            message_observations=(),
        )


def test_multiple_commitments_can_share_one_clock_without_identity_collision() -> None:
    first = commitment(commitment_id="commitment:clock-a")
    second_base = commitment(commitment_id="commitment:clock-b")
    second_values = second_base.values.model_copy(update={"content_ref": "commitment-content:b"})
    second = second_base.model_copy(update={
        "values": second_values,
        "semantic_fingerprint": commitment_semantic_fingerprint(
            owner_ref=second_values.owner_ref, subject_ref=second_values.subject_ref,
            content_ref=second_values.content_ref, content_hash=second_values.content_hash,
            anchor_evidence_refs=second_values.anchor_evidence_refs,
            fulfillment_contract=second_values.fulfillment_contract, policy_refs=POLICY,
        ),
    })
    clock_ref = EvidenceRef(
        ref_id=f"clock:{DUE.isoformat()}", evidence_type="clock_observation",
        claim_purpose="conversation_continuity",
    )

    def due_payload(current: CommitmentProjection, suffix: str) -> CommitmentClockTransitionPayload:
        after = current.model_copy(update={
            "entity_revision": 2, "updated_at": DUE,
            "values": current.values.model_copy(update={
                "source_evidence_refs": (*current.values.source_evidence_refs, clock_ref),
                "status": "due",
            }),
            "origin": current.origin.model_copy(update={
                "authority_mode": "mechanical_clock", "change_id": f"change:due:{suffix}",
                "transition_id": f"transition:due:{suffix}",
                "accepted_event_ref": f"event:due:{suffix}",
            }),
        })
        return CommitmentClockTransitionPayload(
            change_id=f"change:due:{suffix}", transition_id=f"transition:due:{suffix}",
            operation="due", expected_entity_revision=1, commitment_before=current,
            commitment_after=after, clock_evidence_ref=clock_ref,
            clock_event_ref="clock:shared", clock_event_payload_hash="f" * 64,
            policy_version=COMMITMENT_DEADLINE_POLICY_VERSION,
            policy_digest=COMMITMENT_DEADLINE_POLICY_DIGEST,
        )

    commitments, history = reduce_commitment_clock(
        (first, second), (), due_payload(first, "a"), logical_time=DUE
    )
    commitments, history = reduce_commitment_clock(
        commitments, history, due_payload(second, "b"), logical_time=DUE
    )
    assert tuple(item.values.status for item in commitments) == ("due", "due")
    assert tuple(item.transition_id for item in history) == (
        "transition:due:a", "transition:due:b",
    )


def test_typed_commitment_rejects_hash_tamper_and_nonadjacent_acceptance() -> None:
    ledger = initialized()
    p = ledger.project()
    value = changed(operation="open", before=None, after=commitment(),
                    proposal_id="proposal:tamper", world_revision=p.world_revision)
    tampered = json.loads(proposal(value).proposed_mutation.payload_json)
    tampered["commitment_after"]["values"]["importance_bp"] += 1
    bad_proposal = proposal(value).model_copy(update={
        "proposed_mutation": CommitmentProposedMutation(
            event_type="PrivateCommitmentOpened",
            payload_json=json.dumps(tampered, ensure_ascii=False,
                                    sort_keys=True, separators=(",", ":")),
        )
    })
    with pytest.raises(ValueError, match="accepted change hash"):
        ledger.commit([event("proposal:tampered", "ProposalRecorded",
                             bad_proposal.model_dump(mode="json"))],
                      expected_world_revision=p.world_revision,
                      expected_deliberation_revision=p.deliberation_revision)

    ledger.commit([event("proposal:valid", "ProposalRecorded",
                         proposal(value).model_dump(mode="json"))],
                  expected_world_revision=p.world_revision,
                  expected_deliberation_revision=p.deliberation_revision)
    p = ledger.project()
    accepted = {
        "acceptance_id": value.acceptance_id, "status": "accepted",
        "proposal_id": value.proposal_id,
        "evaluated_world_revision": value.evaluated_world_revision,
        "accepted_change_id": value.change_id,
        "accepted_change_hash": value.accepted_change_hash,
    }
    with pytest.raises(ValueError, match="immediately after|adjacent"):
        ledger.commit([
            event("acceptance:valid", "AcceptanceRecorded", accepted),
            event("operator:intervening", "OperatorObservationRecorded", {
                "observation_id": "operator:intervening", "observation_hash": "8" * 64,
            }),
            event(value.commitment_after.origin.accepted_event_ref,
                  "PrivateCommitmentOpened", value.model_dump(mode="json")),
        ], expected_world_revision=p.world_revision,
           expected_deliberation_revision=p.deliberation_revision)

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
import sqlite3

import pytest

from companion_daemon.world_v2.deliberation import (
    DeliberationResult,
    ModelResultAudit,
    ModelRoute,
)
from companion_daemon.world_v2.ledger import WorldLedger
from companion_daemon.world_v2.errors import ConcurrencyConflict
from companion_daemon.world_v2.projection import InternalAuthorityReader
from companion_daemon.world_v2.proposal_audit import ProposalAuditContext, ProposalAuditRecorder
from companion_daemon.world_v2.proposal_envelope import DecisionProposal
from companion_daemon.world_v2.reducers import ReducerState
from companion_daemon.world_v2.schemas import WorldEvent
from companion_daemon.world_v2.sqlite_ledger import SQLiteWorldLedger


NOW = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)
WORLD = "world:audit"


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _digest(value: object) -> str:
    return _hash(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def _event(event_id: str, event_type: str, payload: dict[str, object]) -> WorldEvent:
    return WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id=event_id,
        world_id=WORLD,
        event_type=event_type,
        logical_time=NOW,
        created_at=NOW,
        actor="system:test",
        source="test",
        trace_id="trace:audit",
        causation_id="cause:audit",
        correlation_id="corr:audit",
        idempotency_key=f"key:{event_id}",
        payload=payload,
    )


def _result() -> DeliberationResult:
    proposal = DecisionProposal(
        proposal_id="proposal:audit:1",
        trigger_ref="trigger:audit:1",
        evaluated_world_revision=1,
        evidence_refs=(),
        proposed_changes=(),
        action_intents=(),
        confidence=8000,
        brief_rationale="A bounded no-visible-action proposal.",
        behavior_tendency="observe",
        stance="quiet",
        display_strategy="none",
    )
    response_hash = _hash("response")
    model_call_id = "model-call:audit:1"
    model_result_ref = f"model-result:{_digest({'model_call_id': model_call_id, 'response_hash': response_hash})}"
    audit = ModelResultAudit(
        model_call_id=model_call_id,
        model_result_ref=model_result_ref,
        attempt_id="attempt:audit:1",
        route=ModelRoute(tier="flash", reason_code="ordinary", router_version="router.1"),
        model_id="model:test",
        model_version="1",
        request_hash=_hash("request"),
        response_hash=response_hash,
        status="proposal_validated",
        input_tokens=10,
        output_tokens=20,
    )
    capsule_id = _hash("capsule")
    attempt_audits = (audit,)
    result_id = f"deliberation:{_digest({'capsule_id': capsule_id, 'proposal_hash': proposal.proposal_hash, 'attempt_audits': [audit.model_dump(mode='json')]})}"
    return DeliberationResult(
        result_id=result_id,
        capsule_id=capsule_id,
        proposal=proposal,
        audit=audit,
        attempt_audits=attempt_audits,
    )


def _context(
    *, deliberation_revision: int = 0, commit_world_revision: int = 1
) -> ProposalAuditContext:
    return ProposalAuditContext(
        world_id=WORLD,
        trigger_ref="trigger:audit:1",
        logical_time=NOW,
        created_at=NOW,
        actor="character:celia",
        source="world-v2-deliberation",
        trace_id="trace:audit",
        causation_id="attempt:audit:1",
        correlation_id="trigger:audit:1",
        evaluated_world_revision=1,
        expected_commit_world_revision=commit_world_revision,
        expected_deliberation_revision=deliberation_revision,
    )


def _recovered_result() -> DeliberationResult:
    base = _result()
    attempt_id = "attempt:audit:recovery"
    route = ModelRoute(tier="flash", reason_code="ordinary", router_version="router.1")
    main_call = "model-call:audit:main"
    main = ModelResultAudit(
        model_call_id=main_call,
        model_result_ref=f"model-result:{_digest({'model_call_id': main_call, 'response_hash': None})}",
        attempt_id=attempt_id,
        route=route,
        request_hash=_hash("main-request"),
        status="main_timeout",
        failure_code="main_timeout",
    )
    quick_call = "model-call:audit:quick"
    response_hash = _hash("quick-response")
    quick = ModelResultAudit(
        model_call_id=quick_call,
        model_result_ref=f"model-result:{_digest({'model_call_id': quick_call, 'response_hash': response_hash})}",
        attempt_id=attempt_id,
        route=route,
        model_id="model:quick",
        model_version="1",
        request_hash=_hash("quick-request"),
        response_hash=response_hash,
        status="main_timeout_recovered",
        failure_code="main_timeout",
        input_tokens=3,
        output_tokens=4,
    )
    audits = (main, quick)
    result_id = f"deliberation:{_digest({'capsule_id': base.capsule_id, 'proposal_hash': base.proposal.proposal_hash, 'attempt_audits': [value.model_dump(mode='json') for value in audits]})}"
    return DeliberationResult(
        result_id=result_id,
        capsule_id=base.capsule_id,
        proposal=base.proposal,
        audit=quick,
        attempt_audits=audits,
    )


def _failed_result() -> DeliberationResult:
    recovered = _recovered_result()
    main = recovered.attempt_audits[0]
    call_id = "model-call:audit:failed-quick"
    quick = ModelResultAudit(
        model_call_id=call_id,
        model_result_ref=f"model-result:{_digest({'model_call_id': call_id, 'response_hash': None})}",
        attempt_id=main.attempt_id,
        route=main.route,
        request_hash=_hash("failed-quick-request"),
        status="recovery_failed",
        failure_code="quick_timeout",
    )
    audits = (main, quick)
    result_id = f"deliberation:{_digest({'capsule_id': recovered.capsule_id, 'proposal_hash': None, 'attempt_audits': [value.model_dump(mode='json') for value in audits]})}"
    return DeliberationResult(
        result_id=result_id,
        capsule_id=recovered.capsule_id,
        proposal=None,
        audit=quick,
        attempt_audits=audits,
    )


def _started(ledger: WorldLedger | SQLiteWorldLedger) -> None:
    ledger.commit(
        [_event("event:world:start", "WorldStarted", {})],
        expected_world_revision=0,
        expected_deliberation_revision=0,
    )


@pytest.mark.parametrize("sqlite", [False, True])
def test_audit_transaction_is_atomic_deliberation_only_and_exactly_readable(
    tmp_path, sqlite: bool
) -> None:
    ledger = (
        SQLiteWorldLedger(path=tmp_path / "audit.sqlite3", world_id=WORLD)
        if sqlite
        else WorldLedger.in_memory(world_id=WORLD)
    )
    _started(ledger)
    result = ProposalAuditRecorder(ledger=ledger).record(_result(), _context())

    assert result.world_revision == 1
    assert result.deliberation_revision == 2
    assert len(result.event_ids) == 2
    projection = ledger.project()
    assert projection.world_revision == 1
    assert projection.deliberation_revision == 2

    reader = InternalAuthorityReader(ledger=ledger)
    model = reader.model_result_audit_by_ref(
        world_id=WORLD, cursor=result.cursor, model_result_ref=_result().audit.model_result_ref
    )
    proposal = reader.proposal_audit_by_id(
        world_id=WORLD, cursor=result.cursor, proposal_id="proposal:audit:1"
    )
    assert model is not None and model.model_call_id == "model-call:audit:1"
    assert proposal is not None and proposal.model_result_ref == model.model_result_ref
    assert proposal.proposal_json == _result().proposal.model_dump_json(
        exclude_none=False, by_alias=True
    ) or json.loads(proposal.proposal_json) == _result().proposal.model_dump(mode="json")


def test_audit_record_is_commit_idempotent_and_replays_after_sqlite_reopen(tmp_path) -> None:
    path = tmp_path / "audit-reopen.sqlite3"
    ledger = SQLiteWorldLedger(path=path, world_id=WORLD)
    _started(ledger)
    recorder = ProposalAuditRecorder(ledger=ledger)
    first = recorder.record(_result(), _context())
    repeated = recorder.record(_result(), _context())
    assert repeated == first
    ledger.close()

    reopened = SQLiteWorldLedger(path=path, world_id=WORLD)
    assert reopened.rebuild().semantic_hash == reopened.project().semantic_hash
    reader = InternalAuthorityReader(ledger=reopened)
    assert reader.proposal_audit_by_id(
        world_id=WORLD, cursor=first.cursor, proposal_id="proposal:audit:1"
    ) is not None


def test_recovery_records_every_provider_call_then_final_proposal() -> None:
    ledger = WorldLedger.in_memory(world_id=WORLD)
    _started(ledger)
    result = ProposalAuditRecorder(ledger=ledger).record(_recovered_result(), _context())
    assert len(result.event_ids) == 3
    projection = ledger.project()
    assert [value.attempt_index for value in projection.model_result_audits] == [0, 1]
    assert projection.proposal_audits[0].model_result_ref == _recovered_result().audit.model_result_ref


def test_failed_recovery_is_still_audited_without_a_proposal() -> None:
    ledger = WorldLedger.in_memory(world_id=WORLD)
    _started(ledger)
    committed = ProposalAuditRecorder(ledger=ledger).record(_failed_result(), _context())
    assert committed.proposal_id is None
    assert len(committed.event_ids) == 2
    assert len(ledger.project().model_result_audits) == 2
    assert ledger.project().proposal_audits == ()


def test_reducer_rejects_impossible_attempt_sequence_and_forged_result_id() -> None:
    source = WorldLedger.in_memory(world_id=WORLD)
    _started(source)
    events = ProposalAuditRecorder(ledger=source).build_events(_recovered_result(), _context())
    raw = events[1].payload()
    audit = json.loads(raw["audit_json"])
    audit["status"] = "proposal_validated"
    audit["failure_code"] = None
    raw["audit_json"] = json.dumps(audit, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    raw["audit_hash"] = _hash(raw["audit_json"])
    impossible = events[1].model_copy(
        update={
            "payload_json": json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            "payload_hash": _hash(json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":"))),
        }
    )
    with pytest.raises(ValueError):
        source.commit(
            [events[0], impossible],
            expected_world_revision=1,
            expected_deliberation_revision=0,
        )
    assert source.project().model_result_audits == ()

    single = ProposalAuditRecorder(ledger=source).build_events(_result(), _context())[0]
    raw = single.payload()
    raw["deliberation_result_id"] = "deliberation:" + "0" * 64
    forged = single.model_copy(
        update={
            "payload_json": json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            "payload_hash": _hash(json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":"))),
        }
    )
    with pytest.raises(ValueError):
        source.commit(
            [forged],
            expected_world_revision=1,
            expected_deliberation_revision=0,
        )


def test_stale_followup_cannot_roll_back_committed_audit() -> None:
    ledger = WorldLedger.in_memory(world_id=WORLD)
    _started(ledger)
    ProposalAuditRecorder(ledger=ledger).record(_result(), _context())
    with pytest.raises(ConcurrencyConflict):
        ledger.commit(
            [_event("event:stale", "WorldStarted", {})],
            expected_world_revision=0,
            expected_deliberation_revision=0,
        )
    assert ledger.project().proposal_audits[0].proposal_id == "proposal:audit:1"


def test_audit_preserves_stale_proposal_after_world_advances() -> None:
    ledger = WorldLedger.in_memory(world_id=WORLD)
    _started(ledger)
    ledger.commit(
        [_event("event:world:advanced", "WorldStarted", {})],
        expected_world_revision=1,
        expected_deliberation_revision=0,
    )
    committed = ProposalAuditRecorder(ledger=ledger).record(
        _result(), _context(commit_world_revision=2)
    )
    assert committed.world_revision == 2
    assert ledger.project().proposal_audits[0].evaluated_world_revision == 1


@pytest.mark.parametrize("sqlite", [False, True])
def test_audit_transaction_rejects_split_half_extra_wrong_order_and_mixed_lineage(
    tmp_path, sqlite: bool
) -> None:
    ledger = (
        SQLiteWorldLedger(path=tmp_path / "audit-attacks.sqlite3", world_id=WORLD)
        if sqlite
        else WorldLedger.in_memory(world_id=WORLD)
    )
    _started(ledger)
    recorder = ProposalAuditRecorder(ledger=ledger)
    complete = recorder.build_events(_recovered_result(), _context())
    failed = recorder.build_events(_failed_result(), _context())
    attacks = (
        (complete[0],),
        (complete[1],),
        (complete[2],),
        complete[:2],
        (*complete, _event("event:audit:extra", "WorldStarted", {})),
        (complete[1], complete[0], complete[2]),
        (complete[0], failed[1], complete[2]),
    )
    for attack in attacks:
        with pytest.raises(ValueError):
            ledger.commit(
                attack,
                expected_world_revision=1,
                expected_deliberation_revision=0,
            )
        assert ledger.project().model_result_audits == ()
        assert ledger.project().proposal_audits == ()


def test_v16_sqlite_head_migrates_to_v17_without_forged_audit_indexes(tmp_path) -> None:
    path = tmp_path / "audit-migration.sqlite3"
    ledger = SQLiteWorldLedger(path=path, world_id=WORLD)
    _started(ledger)
    before = ledger.project()
    ledger.close()
    connection = sqlite3.connect(path)
    row = connection.execute(
        "SELECT state_json FROM world_v2_heads WHERE world_id = ?", (WORLD,)
    ).fetchone()
    state = json.loads(row[0])
    state.pop("model_result_audits", None)
    state.pop("proposal_audits", None)
    legacy_payload = ReducerState.model_validate_json(json.dumps(state)).semantic_payload(
        world_id=WORLD,
        world_revision=before.world_revision,
        reducer_bundle_version="world-v2-reducers.16",
    )
    legacy_hash = _hash(
        json.dumps(
            legacy_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    connection.execute(
        "UPDATE world_v2_heads SET state_json = ?, semantic_hash = ?, reducer_bundle_version = ?, state_hash = ? WHERE world_id = ?",
        (
            json.dumps(state, sort_keys=True, separators=(",", ":")),
            legacy_hash,
            "world-v2-reducers.16",
            "legacy-state-hash",
            WORLD,
        ),
    )
    connection.commit()
    connection.close()

    migrated = SQLiteWorldLedger(path=path, world_id=WORLD)
    assert migrated.project().reducer_bundle_version == "world-v2-reducers.17"
    assert migrated.project().semantic_hash == before.semantic_hash
    assert migrated.project().model_result_audits == ()


def test_audit_revalidates_constructed_and_rejects_oversize_or_tampered_bytes() -> None:
    ledger = WorldLedger.in_memory(world_id=WORLD)
    _started(ledger)
    valid = _result()
    constructed = DecisionProposal.model_construct(
        **{**valid.proposal.model_dump(), "brief_rationale": "x" * 241}
    )
    bypassed = valid.model_copy(update={"proposal": constructed})
    with pytest.raises(ValueError):
        ProposalAuditRecorder(ledger=ledger).record(bypassed, _context())

    huge = valid.model_copy(
        update={
            "proposal": valid.proposal.model_copy(
                update={"conflicts": tuple("x" * 128 for _ in range(3000))}
            )
        }
    )
    with pytest.raises(ValueError):
        ProposalAuditRecorder(ledger=ledger).record(huge, _context())

    many_attempts = DeliberationResult.model_construct(
        **{
            **valid.model_dump(mode="python"),
            "attempt_audits": tuple(valid.audit for _ in range(100_000)),
        }
    )
    with pytest.raises(ValueError):
        ProposalAuditRecorder(ledger=ledger).record(many_attempts, _context())

    events = ProposalAuditRecorder(ledger=ledger).build_events(valid, _context())
    payload = events[1].payload()
    payload["proposal_hash"] = "sha256:" + "0" * 64
    tampered = events[1].model_copy(
        update={
            "payload_json": json.dumps(payload, sort_keys=True, separators=(",", ":")),
            "payload_hash": _hash(json.dumps(payload, sort_keys=True, separators=(",", ":"))),
        }
    )
    with pytest.raises(ValueError):
        ledger.commit(
            [events[0], tampered],
            expected_world_revision=1,
            expected_deliberation_revision=0,
        )
    assert ledger.project().model_result_audits == ()

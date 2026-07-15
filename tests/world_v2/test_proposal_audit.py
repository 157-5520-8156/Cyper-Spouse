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
    ModelUsageProvenance,
)
from companion_daemon.world_v2.acceptance_manifest import (
    AcceptanceManifestV2,
    canonical_acceptance_manifest_hash,
    derive_acceptance_manifest_proposal_v2,
)
from companion_daemon.world_v2.event_identity import domain_idempotency_key
from companion_daemon.world_v2.ledger import WorldLedger
from companion_daemon.world_v2.errors import ConcurrencyConflict, LedgerIntegrityError
from companion_daemon.world_v2.projection import InternalAuthorityReader
from companion_daemon.world_v2.proposal_audit import ProposalAuditContext, ProposalAuditRecorder
from companion_daemon.world_v2.proposal_envelope import DecisionProposal
from companion_daemon.world_v2.reducers import ReducerState
from companion_daemon.world_v2.schemas import WorldEvent
from companion_daemon.world_v2.sqlite_ledger import SQLiteWorldLedger
from companion_daemon.world_v2.test_economy import (
    CostProfileGate,
    TEST_ECONOMY_V1,
    model_traces_from_replay,
)


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


def _result(*, metered: bool = False) -> DeliberationResult:
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
        usage=(
            ModelUsageProvenance(
                route_class="chat",
                input_tokens=10,
                output_tokens=20,
                thinking_tokens=0,
                token_provenance="offline_estimated",
                transport="offline_fixture",
                provider="fixture-provider",
                provider_usage_ref="usage:fixture:audit:1",
                provider_usage_hash=_digest(
                    {
                        "usage_contract": "model-usage.1",
                        "route_class": "chat",
                        "input_tokens": 10,
                        "output_tokens": 20,
                        "thinking_tokens": 0,
                        "token_provenance": "offline_estimated",
                        "transport": "offline_fixture",
                        "provider": "fixture-provider",
                        "provider_usage_ref": "usage:fixture:audit:1",
                    }
                ),
            )
            if metered
            else None
        ),
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


def _second_result() -> DeliberationResult:
    base = _result()
    proposal = base.proposal.model_copy(update={"proposal_id": "proposal:audit:2"})
    call_id = "model-call:audit:2"
    response_hash = _hash("response:2")
    audit = ModelResultAudit(
        model_call_id=call_id,
        model_result_ref=f"model-result:{_digest({'model_call_id': call_id, 'response_hash': response_hash})}",
        attempt_id="attempt:audit:2",
        route=base.audit.route,
        model_id="model:test",
        model_version="1",
        request_hash=_hash("request:2"),
        response_hash=response_hash,
        status="proposal_validated",
        input_tokens=5,
        output_tokens=6,
    )
    result_id = f"deliberation:{_digest({'capsule_id': base.capsule_id, 'proposal_hash': proposal.proposal_hash, 'attempt_audits': [audit.model_dump(mode='json')]})}"
    return DeliberationResult(
        result_id=result_id,
        capsule_id=base.capsule_id,
        proposal=proposal,
        audit=audit,
        attempt_audits=(audit,),
    )


def _started(ledger: WorldLedger | SQLiteWorldLedger) -> None:
    ledger.commit(
        [_event("event:world:start", "WorldStarted", {})],
        expected_world_revision=0,
        expected_deliberation_revision=0,
    )


def _acceptance_event(
    ledger: WorldLedger | SQLiteWorldLedger,
    *,
    status: str,
    acceptance_id: str,
    effects: tuple[dict[str, object], ...] = (),
) -> WorldEvent:
    audits = ledger.project().proposal_audits
    bindings = tuple(
        derive_acceptance_manifest_proposal_v2(
            proposal_json=audit.proposal_json,
            proposal_event_ref=audit.event_ref,
            proposal_event_payload_hash=audit.event_payload_hash,
        )
        for audit in audits
    )
    raw: dict[str, object] = {
        "manifest_version": "acceptance-manifest.2",
        "acceptance_id": acceptance_id,
        "status": status,
        "evaluated_world_revision": audits[0].evaluated_world_revision,
        "proposals": tuple(binding.model_dump(mode="json") for binding in bindings),
        "authorized_effects": effects,
    }
    raw["manifest_hash"] = canonical_acceptance_manifest_hash(raw)
    AcceptanceManifestV2.model_validate(raw)
    identity = domain_idempotency_key(
        event_type="AcceptanceRecorded", world_id=WORLD, payload=raw
    )
    assert identity is not None
    return WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id=f"event:{acceptance_id}",
        world_id=WORLD,
        event_type="AcceptanceRecorded",
        logical_time=NOW,
        created_at=NOW,
        actor="system:acceptance",
        source="test",
        trace_id="trace:acceptance-v2",
        causation_id=audits[-1].event_ref,
        correlation_id=acceptance_id,
        idempotency_key=identity,
        payload=raw,
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


def test_metered_model_usage_is_bound_through_sqlite_replay_and_cost_gate(tmp_path) -> None:
    path = tmp_path / "audit-metered.sqlite3"
    ledger = SQLiteWorldLedger(path=path, world_id=WORLD)
    _started(ledger)
    committed = ProposalAuditRecorder(ledger=ledger).record(_result(metered=True), _context())
    projection = ledger.project()
    assert projection.model_result_audits[0].audit_contract == "model-result-audit.2"
    assert '"route_class":"chat"' in projection.model_result_audits[0].audit_json
    ledger.close()

    reopened = SQLiteWorldLedger(path=path, world_id=WORLD)
    evidence = reopened.export_replay_evidence(at_cursor=committed.cursor)
    traces = model_traces_from_replay(evidence=evidence)
    assert len(traces) == 1
    assert traces[0].route_class == "chat"
    assert traces[0].token_provenance == "offline_estimated"
    assert traces[0].thinking_tokens == 0
    assert CostProfileGate().evaluate(profile=TEST_ECONOMY_V1, traces=traces).passed
    assert reopened.rebuild().semantic_hash == reopened.project().semantic_hash


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
def test_acceptance_manifest_v2_rejected_closes_exact_proposal_audit(
    tmp_path, sqlite: bool
) -> None:
    ledger = (
        SQLiteWorldLedger(path=tmp_path / "acceptance-v2.sqlite3", world_id=WORLD)
        if sqlite
        else WorldLedger.in_memory(world_id=WORLD)
    )
    _started(ledger)
    audited = ProposalAuditRecorder(ledger=ledger).record(_result(), _context())
    event = _acceptance_event(
        ledger, status="rejected", acceptance_id="acceptance:v2:rejected"
    )
    ledger.commit(
        [event],
        expected_world_revision=audited.world_revision,
        expected_deliberation_revision=audited.deliberation_revision,
    )
    projection = ledger.project()
    assert projection.actions == () and projection.budget_reservations == ()
    assert projection.acceptance_decisions[0].status == "rejected"
    reader = InternalAuthorityReader(ledger=ledger)
    assert reader.acceptance_manifest_by_id(
        world_id=WORLD,
        cursor=reader.current_cursor(world_id=WORLD),
        acceptance_id="acceptance:v2:rejected",
    ).acceptance_event_payload_hash == event.payload_hash
    if sqlite:
        ledger.close()
        reopened = SQLiteWorldLedger(
            path=tmp_path / "acceptance-v2.sqlite3", world_id=WORLD
        )
        assert reopened.rebuild().acceptance_manifests_v2 == projection.acceptance_manifests_v2


def test_acceptance_manifest_v2_stale_closes_old_proposal_and_accepted_fails_closed() -> None:
    ledger = WorldLedger.in_memory(world_id=WORLD)
    _started(ledger)
    ledger.commit(
        [_event("event:world:before-audit", "WorldStarted", {})],
        expected_world_revision=1,
        expected_deliberation_revision=0,
    )
    audited = ProposalAuditRecorder(ledger=ledger).record(
        _result(), _context(commit_world_revision=2)
    )
    stale = _acceptance_event(ledger, status="stale", acceptance_id="acceptance:v2:stale")
    ledger.commit(
        [stale],
        expected_world_revision=2,
        expected_deliberation_revision=audited.deliberation_revision,
    )
    assert ledger.project().acceptance_decisions[0].status == "stale"



def test_acceptance_manifest_v2_rejects_unknown_version_and_forged_audit_binding() -> None:
    ledger = WorldLedger.in_memory(world_id=WORLD)
    _started(ledger)
    audited = ProposalAuditRecorder(ledger=ledger).record(_result(), _context())
    valid = _acceptance_event(
        ledger, status="rejected", acceptance_id="acceptance:v2:tamper"
    )
    raw = valid.payload()
    proposal = dict(raw["proposals"][0])
    proposal["proposal_hash"] = "sha256:" + "0" * 64
    raw["proposals"] = (proposal,)
    raw["manifest_hash"] = canonical_acceptance_manifest_hash(raw)
    encoded = json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    forged = valid.model_copy(
        update={"payload_json": encoded, "payload_hash": _hash(encoded)}
    )
    with pytest.raises(ValueError, match="exactly bind"):
        ledger.commit(
            [forged],
            expected_world_revision=1,
            expected_deliberation_revision=audited.deliberation_revision,
        )

    raw["manifest_version"] = "acceptance-manifest.999"
    raw["manifest_hash"] = canonical_acceptance_manifest_hash(raw)
    encoded = json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    unknown = valid.model_copy(
        update={"payload_json": encoded, "payload_hash": _hash(encoded)}
    )
    with pytest.raises(ValueError, match="unsupported_manifest_version"):
        ledger.commit(
            [unknown],
            expected_world_revision=1,
            expected_deliberation_revision=audited.deliberation_revision,
        )
    assert ledger.project().acceptance_decisions == ()


@pytest.mark.parametrize("sqlite", [False, True])
def test_v2_proposal_cannot_be_closed_by_legacy_acceptance(
    tmp_path, sqlite: bool
) -> None:
    ledger = (
        SQLiteWorldLedger(path=tmp_path / "legacy-bypass.sqlite3", world_id=WORLD)
        if sqlite
        else WorldLedger.in_memory(world_id=WORLD)
    )
    _started(ledger)
    audited = ProposalAuditRecorder(ledger=ledger).record(_result(), _context())
    payload = {
        "proposal_id": "proposal:audit:1",
        "evaluated_world_revision": 1,
        "acceptance_id": "acceptance:legacy:bypass",
        "status": "rejected",
    }
    identity = domain_idempotency_key(
        event_type="AcceptanceRecorded", world_id=WORLD, payload=payload
    )
    assert identity is not None
    event = WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id="event:acceptance:legacy:bypass",
        world_id=WORLD,
        event_type="AcceptanceRecorded",
        logical_time=NOW,
        created_at=NOW,
        actor="system:acceptance",
        source="test",
        trace_id="trace:legacy:bypass",
        causation_id="cause:legacy:bypass",
        correlation_id="correlation:legacy:bypass",
        idempotency_key=identity,
        payload=payload,
    )
    with pytest.raises(ValueError, match="v2_proposal_requires_manifest"):
        ledger.commit(
            [event],
            expected_world_revision=1,
            expected_deliberation_revision=audited.deliberation_revision,
        )
    assert ledger.project().acceptance_decisions == ()


def test_v2_rejected_closure_preserves_source_event_refs_longer_than_256() -> None:
    ledger = WorldLedger.in_memory(world_id=WORLD)
    _started(ledger)
    events = ProposalAuditRecorder(ledger=ledger).build_events(_result(), _context())
    long_proposal_ref = "event:proposal:" + "p" * 300
    proposal_event = events[-1].model_copy(update={"event_id": long_proposal_ref})
    committed = ledger.commit(
        [events[0], proposal_event],
        expected_world_revision=1,
        expected_deliberation_revision=0,
    )
    assert ledger.project().proposal_audits[0].event_ref == long_proposal_ref
    acceptance = _acceptance_event(
        ledger, status="rejected", acceptance_id="acceptance:v2:long-ref"
    ).model_copy(update={"event_id": "event:acceptance:" + "a" * 300})
    ledger.commit(
        [acceptance],
        expected_world_revision=1,
        expected_deliberation_revision=committed.deliberation_revision,
    )
    retained = ledger.project().acceptance_manifests_v2[0]
    assert retained.proposals[0].proposal_event_ref == long_proposal_ref
    assert len(retained.acceptance_event_ref) > 256


def test_acceptance_manifest_v2_multi_proposal_is_atomic_on_second_binding_tamper() -> None:
    ledger = WorldLedger.in_memory(world_id=WORLD)
    _started(ledger)
    first = ProposalAuditRecorder(ledger=ledger).record(_result(), _context())
    ProposalAuditRecorder(ledger=ledger).record(
        _second_result(), _context(deliberation_revision=first.deliberation_revision)
    )
    valid = _acceptance_event(
        ledger, status="rejected", acceptance_id="acceptance:v2:multi"
    )
    raw = valid.payload()
    proposals = list(raw["proposals"])
    proposals[1] = {**proposals[1], "proposal_event_payload_hash": "0" * 64}
    raw["proposals"] = proposals
    raw["manifest_hash"] = canonical_acceptance_manifest_hash(raw)
    encoded = json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    forged = valid.model_copy(
        update={"payload_json": encoded, "payload_hash": _hash(encoded)}
    )
    with pytest.raises(ValueError, match="exactly bind"):
        ledger.commit(
            [forged],
            expected_world_revision=1,
            expected_deliberation_revision=4,
        )
    assert ledger.project().acceptance_decisions == ()

    ledger.commit(
        [valid], expected_world_revision=1, expected_deliberation_revision=4
    )
    assert tuple(item.proposal_id for item in ledger.project().acceptance_decisions) == (
        "proposal:audit:1",
        "proposal:audit:2",
    )


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


def test_v16_sqlite_head_migrates_to_v18_without_forged_audit_indexes(tmp_path) -> None:
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
    assert migrated.project().reducer_bundle_version == "world-v2-reducers.32"
    assert migrated.project().semantic_hash == before.semantic_hash
    assert migrated.project().model_result_audits == ()


def test_v17_sqlite_head_migrates_to_v18_preserving_proposal_audit(tmp_path) -> None:
    path = tmp_path / "manifest-v18-migration.sqlite3"
    ledger = SQLiteWorldLedger(path=path, world_id=WORLD)
    _started(ledger)
    ProposalAuditRecorder(ledger=ledger).record(_result(), _context())
    before = ledger.project()
    ledger.close()
    with sqlite3.connect(path) as connection:
        state_json = connection.execute(
            "SELECT state_json FROM world_v2_heads WHERE world_id = ?", (WORLD,)
        ).fetchone()[0]
        state = json.loads(state_json)
        state.pop("acceptance_manifests_v2", None)
        legacy_state = ReducerState.model_validate_json(json.dumps(state))
        legacy_payload = legacy_state.semantic_payload(
            world_id=WORLD,
            world_revision=before.world_revision,
            reducer_bundle_version="world-v2-reducers.17",
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
                "world-v2-reducers.17",
                "legacy-state-hash",
                WORLD,
            ),
        )
    migrated = SQLiteWorldLedger(path=path, world_id=WORLD)
    assert migrated.project().reducer_bundle_version == "world-v2-reducers.32"
    assert migrated.project().proposal_audits == before.proposal_audits
    assert migrated.project().acceptance_manifests_v2 == ()


def test_sqlite_v2_acceptance_replay_never_downgrades_invalid_manifest_to_legacy(
    tmp_path,
) -> None:
    path = tmp_path / "acceptance-v2-replay.sqlite3"
    ledger = SQLiteWorldLedger(path=path, world_id=WORLD)
    _started(ledger)
    audited = ProposalAuditRecorder(ledger=ledger).record(_result(), _context())
    event = _acceptance_event(
        ledger, status="rejected", acceptance_id="acceptance:v2:replay"
    )
    ledger.commit(
        [event],
        expected_world_revision=1,
        expected_deliberation_revision=audited.deliberation_revision,
    )
    ledger.close()
    with sqlite3.connect(path) as connection:
        row = connection.execute(
            "SELECT event_json FROM world_v2_events WHERE event_id = ?", (event.event_id,)
        ).fetchone()
        envelope = json.loads(row[0])
        payload = json.loads(envelope["payload_json"])
        payload["manifest_version"] = "acceptance-manifest.999"
        payload_json = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        envelope["payload_json"] = payload_json
        envelope["payload_hash"] = _hash(payload_json)
        event_json = json.dumps(
            envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        connection.execute(
            "UPDATE world_v2_events SET event_json = ?, event_hash = ? WHERE event_id = ?",
            (event_json, _hash(event_json), event.event_id),
        )
    with pytest.raises(LedgerIntegrityError):
        SQLiteWorldLedger(path=path, world_id=WORLD)


def test_v17_head_cannot_claim_v18_acceptance_manifest_projection(tmp_path) -> None:
    path = tmp_path / "forged-v17-manifest.sqlite3"
    ledger = SQLiteWorldLedger(path=path, world_id=WORLD)
    _started(ledger)
    audited = ProposalAuditRecorder(ledger=ledger).record(_result(), _context())
    event = _acceptance_event(
        ledger, status="rejected", acceptance_id="acceptance:v2:forged-v17"
    )
    ledger.commit(
        [event],
        expected_world_revision=1,
        expected_deliberation_revision=audited.deliberation_revision,
    )
    ledger.close()
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE world_v2_heads SET reducer_bundle_version = ?, state_hash = ? WHERE world_id = ?",
            ("world-v2-reducers.17", "legacy-state-hash", WORLD),
        )
    with pytest.raises(LedgerIntegrityError):
        SQLiteWorldLedger(path=path, world_id=WORLD)


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

from __future__ import annotations

from datetime import timedelta

import pytest

from companion_daemon.world_v2.appraisal_acceptance_runtime import (
    AppraisalAcceptanceError,
    AppraisalAcceptanceRuntime,
    appraisal_mutation_event_id,
)
from companion_daemon.world_v2.appraisal_events import appraisal_mutation_hash
from companion_daemon.world_v2.accepted_ledger_batch import AcceptedLedgerBatchIssuer
from companion_daemon.world_v2.ledger import WorldLedger
from companion_daemon.world_v2.runtime import WorldRuntime
from companion_daemon.world_v2.schemas import ProjectionCursor

from test_appraisal_authority import (
    NOW,
    WORLD_ID,
    accepted_payload,
    prepare_claimed_interaction,
    record_proposal,
)


def _cursor(runtime: AppraisalAcceptanceRuntime) -> ProjectionCursor:
    head = runtime.ledger.project()
    return ProjectionCursor(
        world_revision=head.world_revision,
        deliberation_revision=head.deliberation_revision,
        ledger_sequence=head.ledger_sequence,
    )


def _record_ready_proposal(runtime: AppraisalAcceptanceRuntime, *, bind_effect_event: bool = True):
    ledger, trigger, evidence = prepare_claimed_interaction(runtime.ledger)
    payload = accepted_payload(ledger, trigger, evidence)
    event_id = appraisal_mutation_event_id(
        world_id=WORLD_ID,
        proposal_id=str(payload["proposal_id"]),
        transition_id=str(payload["transition_id"]),
        event_type="AppraisalAccepted",
    )
    if bind_effect_event:
        appraisal = payload["appraisal"]
        assert isinstance(appraisal, dict)
        origin = appraisal["origin"]
        assert isinstance(origin, dict)
        origin["accepted_event_ref"] = event_id
        payload["accepted_change_hash"] = appraisal_mutation_hash(payload)
    record_proposal(ledger, trigger, evidence, payload)
    return trigger, payload


def test_production_appraisal_runtime_commits_a_closed_accepted_batch() -> None:
    runtime = AppraisalAcceptanceRuntime.in_memory(world_id=WORLD_ID)
    trigger, payload = _record_ready_proposal(runtime)
    handle = runtime.pin_proposal(cursor=_cursor(runtime), proposal_id=str(payload["proposal_id"]))

    result = runtime.accept(
        handle=handle,
        actor="system:appraisal-worker",
        source="appraisal-worker",
        logical_time=NOW,
        created_at=NOW,
        trace_id="trace:production-appraisal",
        correlation_id="correlation:production-appraisal",
        completed_at=NOW + timedelta(seconds=1),
    )

    projection = runtime.ledger.project()
    assert result.world_revision == projection.world_revision
    assert projection.appraisals[0].hypotheses[0].meaning == "disappointment"
    assert projection.appraisal_proposals == ()
    assert projection.trigger_processes[0].state == "terminal"
    acceptance, mutation, completion = (
        runtime.ledger.lookup_event_commit(event_id)[0] for event_id in result.event_ids
    )
    manifest = acceptance.payload()
    assert manifest["manifest_version"] == "appraisal-acceptance.1"
    assert manifest["mutation_event_id"] == mutation.event_id
    assert manifest["mutation_payload_hash"] == mutation.payload_hash
    assert manifest["completion_event_id"] == completion.event_id
    assert manifest["completion_payload_hash"] == completion.payload_hash

    with pytest.raises(AppraisalAcceptanceError, match="proposal_not_persisted"):
        runtime.pin_proposal(cursor=_cursor(runtime), proposal_id=str(payload["proposal_id"]))


def test_appraisal_runtime_rejects_legacy_payload_that_does_not_bind_effect_identity() -> None:
    runtime = AppraisalAcceptanceRuntime.in_memory(world_id=WORLD_ID)
    _, payload = _record_ready_proposal(runtime, bind_effect_event=False)
    handle = runtime.pin_proposal(cursor=_cursor(runtime), proposal_id=str(payload["proposal_id"]))

    with pytest.raises(AppraisalAcceptanceError, match="mutation_event_identity_not_bound"):
        runtime.accept(
            handle=handle,
            actor="system:appraisal-worker",
            source="appraisal-worker",
            logical_time=NOW,
            created_at=NOW,
            trace_id="trace:legacy-appraisal",
            correlation_id="correlation:legacy-appraisal",
            completed_at=NOW + timedelta(seconds=1),
        )


def test_production_appraisal_runtime_replays_from_sqlite(tmp_path) -> None:
    runtime = AppraisalAcceptanceRuntime.open(path=tmp_path / "appraisal.sqlite3", world_id=WORLD_ID)
    _, payload = _record_ready_proposal(runtime)
    handle = runtime.pin_proposal(cursor=_cursor(runtime), proposal_id=str(payload["proposal_id"]))
    runtime.accept(
        handle=handle,
        actor="system:appraisal-worker",
        source="appraisal-worker",
        logical_time=NOW,
        created_at=NOW,
        trace_id="trace:sqlite-appraisal",
        correlation_id="correlation:sqlite-appraisal",
        completed_at=NOW + timedelta(seconds=1),
    )
    expected = runtime.ledger.project()
    assert runtime.ledger.rebuild() == expected
    runtime.close()

    reopened = AppraisalAcceptanceRuntime.open(
        path=tmp_path / "appraisal.sqlite3", world_id=WORLD_ID
    )
    assert reopened.ledger.project() == expected
    assert reopened.ledger.rebuild() == expected
    reopened.close()


@pytest.mark.asyncio
async def test_world_runtime_consumes_an_appraisal_proposal_idempotently() -> None:
    issuer = AcceptedLedgerBatchIssuer()
    ledger = WorldLedger.in_memory(world_id=WORLD_ID, accepted_batch_issuer=issuer)
    acceptance = AppraisalAcceptanceRuntime(ledger=ledger, batch_issuer=issuer)
    runtime = WorldRuntime(
        world_id=WORLD_ID,
        ledger=ledger,
        appraisal_acceptance=acceptance,
        appraisal_acceptance_actor="worker:interaction-appraisal",
    )
    trigger, payload = _record_ready_proposal(acceptance)

    first = await runtime.accept_appraisal_proposal(str(payload["proposal_id"]))
    second = await runtime.accept_appraisal_proposal(str(payload["proposal_id"]))

    projection = ledger.project()
    assert first == second
    assert first.trigger_id == trigger.trigger_id
    assert first.status == "observed_only"
    assert projection.appraisal_proposals == ()
    assert projection.trigger_processes[0].state == "terminal"
    assert len(projection.appraisals) == 1
    assert len(projection.acceptance_decisions) == 1

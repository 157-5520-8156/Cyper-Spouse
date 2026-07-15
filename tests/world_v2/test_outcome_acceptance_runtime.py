from __future__ import annotations

import pytest

from companion_daemon.world_v2.accepted_ledger_batch import AcceptedLedgerBatchIssuer
from companion_daemon.world_v2.outcome_acceptance_runtime import OutcomeAcceptanceRuntime
from companion_daemon.world_v2.outcome_candidate_reader import OutcomeCandidateReader
from companion_daemon.world_v2.outcome_proposal_compiler import OutcomeProposalCompiler
from companion_daemon.world_v2.schemas import ProjectionCursor

from test_life_projection import WORLD_ID
from test_outcome_proposal_compiler import _audited_proposal, _prepare_claimed_outcome


def _cursor(ledger) -> ProjectionCursor:
    head = ledger.project()
    return ProjectionCursor(
        world_revision=head.world_revision,
        deliberation_revision=head.deliberation_revision,
        ledger_sequence=head.ledger_sequence,
    )


@pytest.mark.asyncio
async def test_runtime_atomically_accepts_compiled_outcome_and_opens_npc_appraisal() -> None:
    ledger, store, target, claimed, source_event, source_commit = await _prepare_claimed_outcome()
    proposal, recorded = _audited_proposal(
        ledger=ledger,
        target=target,
        source_event=source_event,
        source_commit=source_commit,
    )
    compiled = OutcomeProposalCompiler(
        ledger=ledger, candidate_reader=OutcomeCandidateReader(store=store)
    ).record(world_id=WORLD_ID, cursor=recorded.cursor, proposal_id=proposal.proposal_id)

    # Bootstrap helpers intentionally use a normal ledger.  The production
    # acceptance lane is supplied the matching issuer before it is exercised.
    issuer = AcceptedLedgerBatchIssuer()
    ledger._accepted_batch_issuer = issuer  # type: ignore[attr-defined]
    runtime = OutcomeAcceptanceRuntime(ledger=ledger, batch_issuer=issuer)
    handle = runtime.pin_proposal(cursor=_cursor(ledger), proposal_id=compiled.typed_proposal_id)
    result = runtime.accept_runtime_owned(
        handle=handle, actor="worker:outcome", source="world-v2:outcome-worker"
    )

    projection = ledger.project()
    assert result.world_revision == projection.world_revision
    occurrence = next(item for item in projection.world_occurrences if item.occurrence_id == "occurrence:compiler-outcome")
    assert occurrence.status == "settled"
    assert occurrence.result_id == "result:tea-ready"
    assert any(item.outcome_proposal_id == compiled.typed_proposal_id for item in projection.outcome_proposals)
    trigger = next(item for item in projection.trigger_processes if item.process_kind == "npc_world_appraisal")
    assert trigger.state == "open"
    assert trigger.source_evidence_ref == result.event_ids[1]
    acceptance, settlement, opened = (ledger.lookup_event_commit(event_id)[0] for event_id in result.event_ids)
    assert acceptance.payload()["manifest_version"] == "outcome-acceptance.1"
    assert acceptance.payload()["deliberation_trigger_id"] == claimed.trigger_id
    assert acceptance.payload()["settlement_event_id"] == settlement.event_id
    assert acceptance.payload()["npc_appraisal_trigger_event_id"] == opened.event_id
    assert ledger.rebuild() == projection

    # The typed proposal remains as audit history.  Retrying its exact accepted
    # batch is ledger-idempotent rather than allocating another settlement.
    retry = runtime.accept_runtime_owned(
        handle=handle,
        actor="worker:outcome",
        source="world-v2:outcome-worker",
    )
    assert retry == result

from __future__ import annotations

from types import SimpleNamespace

import pytest

from companion_daemon.world_v2.relationship_commitment_worker import (
    RelationshipCommitmentWorker,
    RelationshipCommitmentWorkResult,
)
from companion_daemon.world_v2.relationship_proposal_compiler import (
    RelationshipProposalCompiler,
)
from companion_daemon.world_v2.runtime import WorldRuntime
from companion_daemon.world_v2.schemas import CommitResult

from test_appraisal_authority import WORLD_ID
from test_relationship_commitment_compiler import _compiler_fixture


class _Acceptance:
    def __init__(self, ledger) -> None:
        self.ledger = ledger
        self.pinned: tuple[object, str] | None = None
        self.accepted = 0

    def pin_proposal(self, *, cursor, proposal_id: str):
        self.pinned = (cursor, proposal_id)
        return SimpleNamespace(cursor=cursor, proposal_id=proposal_id)

    def accept_runtime_owned(self, *, handle, actor: str, source: str) -> CommitResult:
        del actor, source
        self.accepted += 1
        return CommitResult(
            world_revision=handle.cursor.world_revision + 1,
            deliberation_revision=handle.cursor.deliberation_revision,
            ledger_sequence=handle.cursor.ledger_sequence + 2,
            event_ids=(
                "event:relationship-commitment-acceptance",
                "event:relationship-commitment-mutation",
            ),
        )


@pytest.mark.asyncio
async def test_background_commitment_waits_for_delivered_receipt_then_accepts_typed_output() -> None:
    ledger, proposal, _audit_cursor, _current_cursor = _compiler_fixture()
    acceptance = _Acceptance(ledger)
    worker = RelationshipCommitmentWorker(
        ledger=ledger,
        compiler=RelationshipProposalCompiler(ledger=ledger),
        acceptance=acceptance,
        actor="worker:relationship-commitment",
    )

    ledger.without_delivered_action()
    assert await worker.drain_one() is None
    assert ledger.recorded == ()
    assert acceptance.accepted == 0

    ready, _proposal, _audit_cursor, _current_cursor = _compiler_fixture()
    ready_acceptance = _Acceptance(ready)
    ready_worker = RelationshipCommitmentWorker(
        ledger=ready,
        compiler=RelationshipProposalCompiler(ledger=ready),
        acceptance=ready_acceptance,
        actor="worker:relationship-commitment",
    )
    result = await ready_worker.drain_one()

    assert result is not None
    assert result.status == "accepted"
    assert result.source_proposal_id == proposal.proposal_id
    assert len(ready.recorded) == 1
    assert ready_acceptance.accepted == 1


class _RuntimeCommitmentWorker:
    def __init__(self, ledger) -> None:
        self.ledger = ledger
        self.calls = 0

    async def drain_one(self) -> RelationshipCommitmentWorkResult:
        self.calls += 1
        return RelationshipCommitmentWorkResult(
            status="accepted",
            source_proposal_id="proposal:relationship-commitment",
            typed_proposal_id="proposal:relationship-commitment:compiled",
            compile_commit=CommitResult(
                world_revision=1,
                deliberation_revision=1,
                ledger_sequence=2,
                event_ids=("event:proposal:relationship-commitment",),
            ),
            acceptance_commit=CommitResult(
                world_revision=2,
                deliberation_revision=1,
                ledger_sequence=4,
                event_ids=(
                    "event:relationship-commitment-acceptance",
                    "event:relationship-commitment-mutation",
                ),
            ),
        )


class _RuntimeInteractionActWorker:
    def __init__(self, ledger) -> None:
        self.ledger = ledger
        self.calls = 0
        self.result = SimpleNamespace(status="accepted", lane="interaction_act")

    async def drain_one(self):
        self.calls += 1
        return self.result


class _RuntimeCharacterInterior:
    def __init__(self, ledger) -> None:
        self.ledger = ledger
        self.result = SimpleNamespace(status="authorized", lane="proactive")

    def _is_bound_to(self, ledger) -> bool:
        return ledger is self.ledger

    async def _drain_reconsideration_once(self):
        return None

    async def _drain_proactive_once(self):
        return self.result


@pytest.mark.asyncio
async def test_world_runtime_schedules_relationship_commitment_only_in_background() -> None:
    ledger, _proposal, _audit_cursor, _current_cursor = _compiler_fixture()
    worker = _RuntimeCommitmentWorker(ledger)
    runtime = WorldRuntime(
        world_id=WORLD_ID,
        ledger=ledger,
        relationship_commitment_worker=worker,
    )

    result = await runtime.drain_background_once()

    assert result is not None
    assert result.status == "accepted"
    assert worker.calls == 1


@pytest.mark.asyncio
async def test_world_runtime_prioritizes_eligible_proactive_over_pending_social_workers() -> None:
    ledger, _proposal, _audit_cursor, _current_cursor = _compiler_fixture()
    commitment_worker = _RuntimeCommitmentWorker(ledger)
    interaction_act_worker = _RuntimeInteractionActWorker(ledger)
    interior = _RuntimeCharacterInterior(ledger)
    runtime = WorldRuntime(
        world_id=WORLD_ID,
        ledger=ledger,
        relationship_commitment_worker=commitment_worker,
        interaction_act_worker=interaction_act_worker,  # type: ignore[arg-type]
        character_interior=interior,  # type: ignore[arg-type]
    )

    first = await runtime.drain_background_once()

    assert first is interior.result
    assert commitment_worker.calls == 0
    assert interaction_act_worker.calls == 0

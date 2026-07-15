from __future__ import annotations

import pytest

from companion_daemon.world_v2.decision_proposal_authority import (
    DecisionProposalAuthorityError,
    DecisionProposalAuthorityReader,
)
from companion_daemon.world_v2.ledger import WorldLedger
from companion_daemon.world_v2.proposal_audit import ProposalAuditRecorder
from companion_daemon.world_v2.schemas import ProjectionCursor

from test_proposal_audit import WORLD, _context, _event, _result, _started


def test_reader_pins_only_the_exact_current_decision_proposal() -> None:
    ledger = WorldLedger.in_memory(world_id=WORLD)
    _started(ledger)
    committed = ProposalAuditRecorder(ledger=ledger).record(_result(), _context())
    reader = DecisionProposalAuthorityReader(ledger=ledger)

    authority = reader.read(
        reader.pin(world_id=WORLD, cursor=committed.cursor, proposal_id="proposal:audit:1")
    )

    assert authority.audit.event_ref.startswith("event:ProposalRecorded:")
    assert authority.proposal.proposal_id == "proposal:audit:1"
    assert authority.proposal.affect_decision == "no_change"


def test_reader_rejects_foreign_world_and_a_stale_generic_proposal() -> None:
    ledger = WorldLedger.in_memory(world_id=WORLD)
    _started(ledger)
    committed = ProposalAuditRecorder(ledger=ledger).record(_result(), _context())
    reader = DecisionProposalAuthorityReader(ledger=ledger)

    with pytest.raises(DecisionProposalAuthorityError, match="world_mismatch"):
        reader.pin(world_id="world:other", cursor=committed.cursor, proposal_id="proposal:audit:1")

    advanced = ledger.commit(
        [_event("event:authority:advanced", "WorldStarted", {})],
        expected_world_revision=committed.world_revision,
        expected_deliberation_revision=committed.deliberation_revision,
    )
    with pytest.raises(DecisionProposalAuthorityError, match="proposal_stale"):
        reader.pin(
            world_id=WORLD,
            cursor=ProjectionCursor(
                world_revision=advanced.world_revision,
                deliberation_revision=advanced.deliberation_revision,
                ledger_sequence=advanced.ledger_sequence,
            ),
            proposal_id="proposal:audit:1",
        )

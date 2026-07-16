from __future__ import annotations

import pytest

from companion_daemon.world_v2.accepted_ledger_batch import AcceptedLedgerBatchIssuer
from companion_daemon.world_v2.activity_lifecycle_proposal import ActivityLifecycleProposalCompiler
from companion_daemon.world_v2.activity_lifecycle_draft import ActivityLifecycleDraftAdapter
from companion_daemon.world_v2.activity_lifecycle_runtime import (
    ActivityLifecycleAcceptanceRuntime,
    ActivityLifecycleProposalRecorder,
)
from companion_daemon.world_v2.activity_lifecycle_worker import ActivityLifecycleWorker
from companion_daemon.world_v2.schemas import CommitResult, ProjectionCursor

from test_activity_lifecycle_proposal import (
    ECOLOGY_CATALOG_VERSION,
    _catalog,
    _claimed_projection,
    _selected_draft,
)
from test_life_ecology_activity import NOW


class _Ledger:
    world_id = "world:life-ecology-activity"
    blocks_event_loop = False

    def __init__(self, projection) -> None:  # type: ignore[no-untyped-def]
        self.projection = projection
        self.events = {}
        self.accepted = ()

    def project_at(self, cursor: ProjectionCursor):  # type: ignore[no-untyped-def]
        assert cursor == self.cursor
        return self.projection

    def project(self):  # type: ignore[no-untyped-def]
        return self.projection

    @property
    def cursor(self) -> ProjectionCursor:
        return ProjectionCursor(
            world_revision=self.projection.world_revision,
            deliberation_revision=self.projection.deliberation_revision,
            ledger_sequence=self.projection.ledger_sequence,
        )

    def commit_at_cursor(self, events, *, expected_cursor, commit_id):  # type: ignore[no-untyped-def]
        assert expected_cursor == self.cursor
        assert commit_id.startswith("commit:activity-lifecycle-proposal:")
        event = events[0]
        self.events[event.event_id] = event
        self.projection = self.projection.model_copy(
            update={
                "deliberation_revision": self.projection.deliberation_revision + 1,
                "ledger_sequence": self.projection.ledger_sequence + 1,
                "proposal_ids": (*self.projection.proposal_ids, event.payload()["proposal_id"]),
            }
        )
        return CommitResult(
            world_revision=self.projection.world_revision,
            deliberation_revision=self.projection.deliberation_revision,
            ledger_sequence=self.projection.ledger_sequence,
            event_ids=(event.event_id,),
        )

    def lookup_event_commit(self, event_id: str):  # type: ignore[no-untyped-def]
        event = self.events.get(event_id)
        if event is None:
            return None
        return event, CommitResult(
            world_revision=self.projection.world_revision,
            deliberation_revision=self.projection.deliberation_revision,
            ledger_sequence=self.projection.ledger_sequence,
            event_ids=(event.event_id,),
        )

    def commit_accepted(self, batch, *, expected_cursor):  # type: ignore[no-untyped-def]
        assert expected_cursor == self.cursor
        events, _ = self.issuer.verify(
            handle=batch, world_id=self.world_id, expected_cursor=expected_cursor
        )
        self.accepted = events
        return CommitResult(
            world_revision=expected_cursor.world_revision + 1,
            deliberation_revision=expected_cursor.deliberation_revision,
            ledger_sequence=expected_cursor.ledger_sequence + len(events),
            event_ids=tuple(item.event_id for item in events),
        )


def test_recorder_and_acceptance_runtime_preserve_the_exact_proposal_to_effect_chain() -> None:
    projection, trigger_id = _claimed_projection()
    ledger = _Ledger(projection)
    ledger.issuer = AcceptedLedgerBatchIssuer()
    proposal = ActivityLifecycleProposalCompiler(
        catalog=_catalog(), ecology_catalog_version=ECOLOGY_CATALOG_VERSION
    ).compile(
        projection=projection,
        wake_event_ref="event:clock:opening",
        ecology_trigger_id=trigger_id,
        draft=_selected_draft(projection=projection),
    )
    assert proposal is not None

    record = ActivityLifecycleProposalRecorder(ledger=ledger).record(
        cursor=ledger.cursor,
        proposal=proposal,
        actor="worker:life-ecology",
        source="test",
        created_at=NOW,
        trace_id="trace:activity",
        correlation_id="correlation:activity",
    )
    runtime = ActivityLifecycleAcceptanceRuntime(ledger=ledger, batch_issuer=ledger.issuer)
    accepted = runtime.accept(
        handle=runtime.pin_proposal(cursor=ledger.cursor, proposal_event_ref=record.proposal_event_ref),
        actor="worker:life-ecology",
        source="test",
        logical_time=NOW,
        created_at=NOW,
        trace_id="trace:activity",
        correlation_id="correlation:activity",
    )

    assert accepted.event_ids == tuple(item.event_id for item in ledger.accepted)
    assert [item.event_type for item in ledger.accepted] == ["AcceptanceRecorded", "ActivityStarted"]
    assert ledger.accepted[0].causation_id == record.proposal_event_ref
    assert ledger.accepted[1].causation_id == ledger.accepted[0].event_id
    assert ledger.accepted[1].payload()["activity_lifecycle_proposal_id"] == proposal.proposal_id


class _Model:
    model = "test-flash"

    async def complete(self, messages, *, temperature=0.2):  # type: ignore[no-untyped-def]
        token = __import__("json").loads(messages[1]["content"])["openings"][0]["opening_token"]
        return '{"decision":"select","opening_token":"' + token + '"}'


@pytest.mark.asyncio
async def test_worker_turns_one_claimed_wake_into_one_accepted_transition() -> None:
    projection, trigger_id = _claimed_projection()
    ledger = _Ledger(projection)
    ledger.issuer = AcceptedLedgerBatchIssuer()
    worker = ActivityLifecycleWorker(
        ledger=ledger,
        catalog=_catalog(),
        draft_adapter=ActivityLifecycleDraftAdapter(model=_Model()),
        proposal_recorder=ActivityLifecycleProposalRecorder(ledger=ledger),
        acceptance_runtime=ActivityLifecycleAcceptanceRuntime(ledger=ledger, batch_issuer=ledger.issuer),
        ecology_catalog_version=ECOLOGY_CATALOG_VERSION,
    )

    result = await worker.advance_once(
        wake_event_ref="event:clock:opening",
        trigger_id=trigger_id,
        logical_time=NOW,
        actor="worker:life-ecology",
        trace_id="trace:worker",
        correlation_id="correlation:worker",
    )

    assert result.status == "transitioned"
    assert [item.event_type for item in ledger.accepted] == ["AcceptanceRecorded", "ActivityStarted"]

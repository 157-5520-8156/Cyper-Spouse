from __future__ import annotations

from companion_daemon.world_v2.accepted_ledger_batch import AcceptedLedgerBatchIssuer
from companion_daemon.world_v2.activity_lifecycle_proposal import ActivityLifecycleProposalCompiler
from companion_daemon.world_v2.activity_lifecycle_runtime import (
    ActivityLifecycleAcceptanceRuntime,
    ActivityLifecycleProposalRecorder,
)
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

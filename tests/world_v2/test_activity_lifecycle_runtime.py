from __future__ import annotations

from datetime import timedelta

import pytest

from companion_daemon.world_v2.accepted_ledger_batch import AcceptedLedgerBatchIssuer
from companion_daemon.world_v2.activity_lifecycle_proposal import ActivityLifecycleProposalCompiler
from companion_daemon.world_v2.activity_lifecycle_draft import ActivityLifecycleDraftAdapter
from companion_daemon.world_v2.activity_lifecycle_runtime import (
    ActivityLifecycleAcceptanceRuntime,
    ActivityLifecycleProposalRecorder,
)
from companion_daemon.world_v2.activity_lifecycle_worker import ActivityLifecycleWorker
from companion_daemon.world_v2.event_identity import domain_idempotency_key
from companion_daemon.world_v2.ledger import WorldLedger
from companion_daemon.world_v2.life_ecology_activity import ActivityOpeningCatalog
from companion_daemon.world_v2.life_ecology_contract import LifeEcologyRunKey
from companion_daemon.world_v2.life_ecology_trigger_store import LedgerLifeEcologyTriggerStore
from companion_daemon.world_v2.schema_core import EvidenceRef
from companion_daemon.world_v2.schemas import (
    CommitResult,
    PlanStateProjection,
    ProjectionCursor,
    WorldEvent,
)

from test_activity_lifecycle_proposal import (
    ECOLOGY_CATALOG_VERSION,
    _catalog,
    _claimed_projection,
    _selected_draft,
)
from test_life_ecology_activity import NOW


def _real_event(
    *,
    event_id: str,
    event_type: str,
    payload: dict[str, object],
    world_id: str,
) -> WorldEvent:
    identity = domain_idempotency_key(
        event_type=event_type, world_id=world_id, payload=payload
    )
    return WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id=event_id,
        world_id=world_id,
        event_type=event_type,
        logical_time=NOW,
        created_at=NOW,
        actor="test:activity-lifecycle",
        source="test:activity-lifecycle",
        trace_id="trace:activity-lifecycle",
        causation_id=f"cause:{event_id}",
        correlation_id="correlation:activity-lifecycle",
        idempotency_key=identity or f"identity:{event_id}",
        payload=payload,
    )


def _commit_real(ledger: WorldLedger, event: WorldEvent) -> None:
    projection = ledger.project()
    ledger.commit(
        (event,),
        expected_world_revision=projection.world_revision,
        expected_deliberation_revision=projection.deliberation_revision,
    )


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


@pytest.mark.asyncio
async def test_worker_replays_a_real_ledger_from_claimed_clock_to_accepted_activity() -> None:
    """Exercise the actual reducers rather than a projection-shaped fake ledger.

    This is deliberately the first vertical's narrowest legal plan: it is
    companion-owned and abstract, with no unimplemented location or NPC
    authority.  The resulting timeline must remain replayable after the
    proposal and the accepted effect have each changed their own revision lane.
    """

    world_id = "world:activity-lifecycle-real-ledger"
    issuer = AcceptedLedgerBatchIssuer()
    ledger = WorldLedger.in_memory(world_id=world_id, accepted_batch_issuer=issuer)
    clock = _real_event(
        event_id="event:clock:activity-lifecycle",
        event_type="ClockAdvanced",
        world_id=world_id,
        payload={
            "logical_time_from": (NOW - timedelta(seconds=1)).isoformat(),
            "logical_time_to": NOW.isoformat(),
        },
    )
    _commit_real(ledger, clock)
    observed = _real_event(
        event_id="event:observation:activity-lifecycle",
        event_type="ObservationRecorded",
        world_id=world_id,
        payload={
            "schema_version": "world-v2.1",
            "observation_kind": "message",
            "observation_id": "observation:activity-lifecycle",
            "world_id": world_id,
            "logical_time": NOW.isoformat(),
            "created_at": NOW.isoformat(),
            "trace_id": "trace:activity-lifecycle",
            "causation_id": "cause:event:observation:activity-lifecycle",
            "correlation_id": "correlation:activity-lifecycle",
            "source": "test:activity-lifecycle",
            "source_event_id": "source:observation:activity-lifecycle",
            "actor": "test:activity-lifecycle",
            "channel": "direct_message",
            "payload_ref": "payload:activity-lifecycle",
            "payload_hash": "a" * 64,
            "received_at": NOW.isoformat(),
        },
    )
    _commit_real(ledger, observed)
    message = ledger.project().message_observations[0]
    plan = PlanStateProjection(
        plan_id="plan:activity-lifecycle",
        activity_id="activity:activity-lifecycle",
        entity_revision=1,
        activity_kind="quiet_reading",
        evidence_refs=(
            EvidenceRef(
                ref_id=message.observation_id,
                evidence_type="observed_message",
                claim_purpose="future_plan",
                source_world_revision=message.world_revision,
                immutable_hash=message.event_payload_hash,
            ),
        ),
        status="planned",
        importance_bp=4000,
        owner_actor_ref="actor:companion",
        privacy_class="private",
    )
    _commit_real(
        ledger,
        _real_event(
            event_id="event:plan:activity-lifecycle",
            event_type="ActivityPlanned",
            world_id=world_id,
            payload={
                "change_id": "change:plan:activity-lifecycle",
                "transition_id": "transition:plan:activity-lifecycle",
                "expected_entity_revision": 0,
                "evidence_refs": [item.model_dump(mode="json") for item in plan.evidence_refs],
                "policy_refs": ("policy:test",),
                "plan": plan.model_dump(mode="json"),
            },
        ),
    )
    catalog_version = "life-ecology.1"
    claim = await LedgerLifeEcologyTriggerStore(
        ledger=ledger, owner_id="worker:life-ecology"
    ).claim_or_join(
        key=LifeEcologyRunKey(
            world_id=world_id, wake_event_ref=clock.event_id, catalog_version=catalog_version
        ),
        trace_id="trace:activity-lifecycle",
        correlation_id="correlation:activity-lifecycle",
    )
    assert claim.state == "owned"
    worker = ActivityLifecycleWorker(
        ledger=ledger,
        catalog=ActivityOpeningCatalog(owner_actor_ref="actor:companion"),
        draft_adapter=ActivityLifecycleDraftAdapter(model=_Model()),
        proposal_recorder=ActivityLifecycleProposalRecorder(ledger=ledger),
        acceptance_runtime=ActivityLifecycleAcceptanceRuntime(ledger=ledger, batch_issuer=issuer),
        ecology_catalog_version=catalog_version,
    )

    result = await worker.advance_once(
        wake_event_ref=clock.event_id,
        trigger_id=claim.trigger_id,
        logical_time=NOW,
        actor="worker:life-ecology",
        trace_id="trace:activity-lifecycle",
        correlation_id="correlation:activity-lifecycle",
    )

    replayed = ledger.project()
    assert result.status == "transitioned"
    assert replayed.plans[0].status == "active"
    assert replayed.plans[0].entity_revision == 2
    assert len(replayed.proposal_ids) == 1
    assert len(replayed.acceptance_decisions) == 1
    assert replayed.acceptance_decisions[0].proposal_id == replayed.proposal_ids[0]

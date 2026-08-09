from __future__ import annotations

from datetime import timedelta
import json

import pytest

from companion_daemon.world_v2.accepted_ledger_batch import AcceptedLedgerBatchIssuer
from companion_daemon.world_v2.activity_lifecycle_proposal import ActivityLifecycleProposalCompiler
from companion_daemon.world_v2.activity_lifecycle_runtime import (
    ActivityLifecycleAcceptanceRuntime,
    ActivityLifecycleProposalRecorder,
)
from companion_daemon.world_v2.activity_lifecycle_worker import ActivityLifecycleWorker
from companion_daemon.world_v2.character_interior.contracts import (
    InnerDecision,
    _InstantPrivateSelf,
    _InteriorAuthorLineage,
    _PrivateSelfLineage,
)
from companion_daemon.world_v2.character_interior.run_result import CausalOpportunityIdentity
from companion_daemon.world_v2.event_identity import domain_idempotency_key
from companion_daemon.world_v2.ledger import WorldLedger
from companion_daemon.world_v2.life_ecology_activity import ActivityOpeningCatalog
from companion_daemon.world_v2.life_ecology_contract import LifeEcologyRunKey
from companion_daemon.world_v2.life_ecology_trigger_store import LedgerLifeEcologyTriggerStore
from companion_daemon.world_v2.schema_core import EvidenceRef
from companion_daemon.world_v2.schemas import (
    CommitResult,
    MessageObservationRef,
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


def test_interruption_acceptance_records_source_bound_change_plan_coordinates() -> None:
    projection, trigger_id = _claimed_projection()
    observation = MessageObservationRef(
        observation_id="observation:user:interrupt",
        source="test", source_event_id="message:interrupt",
        content_payload_hash="b" * 64, event_payload_hash="c" * 64,
        world_revision=8, actor="user:geoff", channel="direct",
        payload_ref="payload:interrupt",
    )
    active = projection.plans[0].model_copy(update={
        "status": "active", "authority_origin": None,
    })
    projection = projection.model_copy(update={
        "plans": (active,), "message_observations": (observation,),
    })
    ledger = _Ledger(projection)
    ledger.issuer = AcceptedLedgerBatchIssuer()
    proposal = ActivityLifecycleProposalCompiler(
        catalog=_catalog(), ecology_catalog_version=ECOLOGY_CATALOG_VERSION
    ).compile(
        projection=projection, wake_event_ref="event:clock:opening",
        ecology_trigger_id=trigger_id,
        draft=_selected_draft(projection=projection),
    )
    assert proposal is not None and proposal.opening_kind == "interruption"
    record = ActivityLifecycleProposalRecorder(ledger=ledger).record(
        cursor=ledger.cursor, proposal=proposal,
        actor="worker:life-ecology", source="test", created_at=NOW,
        trace_id="trace:interrupt", correlation_id="correlation:interrupt",
    )
    runtime = ActivityLifecycleAcceptanceRuntime(
        ledger=ledger, batch_issuer=ledger.issuer
    )

    runtime.accept(
        handle=runtime.pin_proposal(
            cursor=ledger.cursor, proposal_event_ref=record.proposal_event_ref
        ),
        actor="worker:life-ecology", source="test", logical_time=NOW,
        created_at=NOW, trace_id="trace:interrupt",
        correlation_id="correlation:interrupt",
    )

    effect = ledger.accepted[1].payload()
    assert effect["policy_refs"] == [
        "policy:activity-lifecycle.1",
        "matrix:deviation:change_plan",
        "matrix:source:interruption",
    ]
    assert effect["evidence_refs"][-1]["evidence_type"] == "observed_message"
    assert effect["evidence_refs"][-1]["ref_id"] == observation.observation_id


class _Interior:
    def __init__(self, *, technical_failure: bool = False) -> None:
        self.technical_failure = technical_failure
        self.opportunities = []

    async def consider(self, opportunity):  # type: ignore[no-untyped-def]
        self.opportunities.append(opportunity)
        if self.technical_failure:
            return type(
                "Decision",
                (),
                {
                    "status": "technical_failure",
                    "failure_code": "role_result_not_json",
                    "decision": None,
                    "author_lineage": None,
                },
            )()
        manifest = opportunity.capability_manifest
        assert manifest is not None
        token = manifest.payload["openings"][0]["opening_token"]
        author = _InteriorAuthorLineage(
            model_id="character-interior",
            model_version="fixture.1",
            model_call_id="model-call:activity-lifecycle:fixture",
            request_hash="sha256:" + "1" * 64,
            response_hash="sha256:" + "2" * 64,
            attempt_ordinal=0,
        )
        private_self = _InstantPrivateSelf(
            summary="她选择顺着当前生活安排往前走。",
            attended_source_refs=opportunity.source_refs,
        )
        snapshot_hash = "3" * 64
        private_lineage = _PrivateSelfLineage(
            relation="single_pass",
            initial_private_self=private_self,
            initial_snapshot_id=f"inner-life-snapshot:sha256:{snapshot_hash}",
            initial_snapshot_hash=snapshot_hash,
            initial_author_lineage=author,
            final_private_self=private_self,
            final_snapshot_id=f"inner-life-snapshot:sha256:{snapshot_hash}",
            final_snapshot_hash=snapshot_hash,
            final_author_lineage=author,
        )
        return InnerDecision(
            inner_turn_id="character-inner-turn:sha256:" + "4" * 64,
            opportunity_ref=opportunity.opportunity_ref,
            actor_ref=opportunity.actor_ref,
            cursor=opportunity.cursor,
            snapshot_id=f"inner-life-snapshot:sha256:{snapshot_hash}",
            snapshot_hash=snapshot_hash,
            status="decided",
            summary=private_self.summary,
            attended_source_refs=private_self.attended_source_refs,
            instant_private_self=private_self,
            private_self_lineage=private_lineage,
            decision={
                "contract": "character-interior-purpose-decision.1",
                "purpose": "activity_lifecycle_choice",
                "capability_ref": manifest.capability_ref,
                "capability_payload_hash": manifest.payload_hash,
                "source_refs": list(manifest.source_refs),
                "payload": {
                    "contract": "character-interior-activity-lifecycle-choice.1",
                    "decision": "select",
                    "selected_token": token,
                },
            },
            author_lineage=author,
        )


@pytest.mark.asyncio
async def test_worker_turns_one_claimed_wake_into_one_accepted_transition() -> None:
    projection, trigger_id = _claimed_projection()
    ledger = _Ledger(projection)
    ledger.issuer = AcceptedLedgerBatchIssuer()
    interior = _Interior()
    worker = ActivityLifecycleWorker(
        ledger=ledger,
        catalog=_catalog(),
        character_interior=interior,
        owner_actor_ref="actor:companion",
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
    assert len(interior.opportunities) == 1
    assert interior.opportunities[0].purpose == "activity_lifecycle_choice"
    assert interior.opportunities[0].opportunity_ref == CausalOpportunityIdentity(
        world_id=ledger.world_id,
        actor_ref="actor:companion",
        purpose="activity_lifecycle_choice",
        source_refs=("event:clock:opening",),
        epoch="event:clock:opening",
    ).opportunity_ref
    capability = interior.opportunities[0].capability_manifest.payload
    assert capability["contract"] == (
        "character-interior-activity-lifecycle-capability.2"
    )
    assert "mood_context" not in capability
    proposal_event = next(iter(ledger.events.values()))
    recorded_model = proposal_event.payload()["character_interior_model_result"]
    assert recorded_model["audit_contract"] == "model-result-audit.7"
    assert (
        json.loads(recorded_model["audit_json"])["character_interior_lineage"][
            "purpose"
        ]
        == "activity_lifecycle_choice"
    )


@pytest.mark.asyncio
async def test_worker_reports_character_interior_failure_without_forging_no_op() -> None:
    projection, trigger_id = _claimed_projection()
    ledger = _Ledger(projection)
    ledger.issuer = AcceptedLedgerBatchIssuer()
    worker = ActivityLifecycleWorker(
        ledger=ledger,
        catalog=_catalog(),
        character_interior=_Interior(technical_failure=True),
        owner_actor_ref="actor:companion",
        proposal_recorder=ActivityLifecycleProposalRecorder(ledger=ledger),
        acceptance_runtime=ActivityLifecycleAcceptanceRuntime(ledger=ledger, batch_issuer=ledger.issuer),
        ecology_catalog_version=ECOLOGY_CATALOG_VERSION,
    )

    result = await worker.advance_once(
        wake_event_ref="event:clock:opening",
        trigger_id=trigger_id,
        logical_time=NOW,
        actor="worker:life-ecology",
        trace_id="trace:invalid-model",
        correlation_id="correlation:invalid-model",
    )

    assert result.status == "technical_failure"
    assert result.reason_code == "activity_lifecycle.role_result_not_json"
    assert ledger.accepted == ()


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
        character_interior=_Interior(),
        owner_actor_ref="actor:companion",
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

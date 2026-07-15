from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib

import pytest

from companion_daemon.world_v2.media_v2 import (
    MediaOpportunity,
    MediaOpportunityFrozenPayload,
    MediaPlan,
    MediaPlanRecordedPayload,
    PhotoCandidate,
    FrozenMediaEvidenceSnapshot,
    MediaEvidenceSource,
    canonical_media_json,
    PhotoCandidateOpenedPayload,
    continuation_trigger_id,
    media_payload_hash,
    planning_request_id,
    SQLiteImmutableMediaPayloadStore,
    StoredMediaPayload,
)
from companion_daemon.world_v2.event_identity import domain_idempotency_key
from companion_daemon.world_v2.sqlite_ledger import SQLiteWorldLedger
from companion_daemon.world_v2.reducers import ReducerState, reduce_event
from companion_daemon.world_v2.schemas import (
    Action,
    ClaimLease,
    CommittedWorldEventRef,
    ExecutionReceipt,
    ProjectionCursor,
    TriggerProcess,
    WorldEvent,
)


NOW = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
WORLD = "world:media-v2"
SOURCE = "event:world:committed"


def _event(event_type: str, payload: dict[str, object], suffix: str) -> WorldEvent:
    return WorldEvent.from_payload(
        schema_version="world-v2.1", event_id=f"event:media-test:{suffix}", event_type=event_type,
        world_id=WORLD, logical_time=NOW, created_at=NOW, actor="system:test", source="test",
        trace_id="trace:media", causation_id="cause:media", correlation_id="correlation:media",
        idempotency_key=f"idempotency:media:{suffix}", payload=payload,
    )


def _snapshot(*, event_ref: str = SOURCE, payload_hash: str = "a" * 64) -> str:
    return canonical_media_json(FrozenMediaEvidenceSnapshot(source_events=(
        MediaEvidenceSource(event_ref=event_ref, payload_hash=payload_hash),
    )).model_dump(mode="json"))


def _opportunity(*, event_ref: str = SOURCE, payload_hash: str = "a" * 64) -> MediaOpportunity:
    snapshot = _snapshot(event_ref=event_ref, payload_hash=payload_hash)
    return MediaOpportunity(
        opportunity_id="opportunity:1", candidate_id="candidate:1", family="life_share",
        delivery_mode="preview", privacy_ceiling="personal", event_snapshot_ref="sidecar:snapshot:1",
        event_snapshot_hash=media_payload_hash(snapshot), source_event_refs=(event_ref,), catalog_version="media-catalog.1",
        expires_at=NOW + timedelta(hours=1),
    )


def _state() -> ReducerState:
    return ReducerState(committed_world_event_refs=(CommittedWorldEventRef(
        event_id=SOURCE, event_type="WorldOccurrenceSettled", world_revision=1,
        payload_hash="a" * 64, logical_time=NOW,
    ),))


def test_candidate_and_opportunity_are_bound_only_to_prior_committed_events() -> None:
    candidate = PhotoCandidate(
        candidate_id="candidate:1", source_event_refs=(SOURCE,), family="life_share", privacy_ceiling="personal",
    )
    state = reduce_event(_state(), _event("PhotoCandidateOpened", PhotoCandidateOpenedPayload(candidate=candidate).model_dump(mode="json"), "candidate"))
    state = reduce_event(state, _event("MediaOpportunityFrozen", MediaOpportunityFrozenPayload(opportunity=_opportunity()).model_dump(mode="json"), "opportunity"))
    assert state.photo_candidates == (candidate,)
    assert state.media_opportunities == (_opportunity(),)

    invalid = candidate.model_copy(update={"candidate_id": "candidate:invalid", "source_event_refs": ("event:uncommitted",)})
    with pytest.raises(ValueError, match="prior committed"):
        reduce_event(_state(), _event("PhotoCandidateOpened", PhotoCandidateOpenedPayload(candidate=invalid).model_dump(mode="json"), "invalid"))


def test_recorded_plan_requires_exact_delivered_action_receipt_and_opens_deterministic_continuation() -> None:
    opportunity = _opportunity()
    candidate = PhotoCandidate(candidate_id="candidate:1", source_event_refs=(SOURCE,), family="life_share", privacy_ceiling="personal")
    state = reduce_event(_state(), _event("PhotoCandidateOpened", {"candidate": candidate.model_dump(mode="json")}, "candidate"))
    state = reduce_event(state, _event("MediaOpportunityFrozen", {"opportunity": opportunity.model_dump(mode="json")}, "opportunity"))
    request = planning_request_id(opportunity.opportunity_id)
    lease = ClaimLease(owner_id="worker:media", attempt_id="attempt:1", acquired_at=NOW, expires_at=NOW + timedelta(minutes=1))
    action = Action.model_construct(
        schema_version="world-v2.1", action_id="action:planning:1", world_id=WORLD, logical_time=NOW, created_at=NOW,
        trace_id="trace:media", causation_id="cause:media", correlation_id="correlation:media", kind="media_planning",
        layer="media_action", intent_ref=opportunity.opportunity_id, actor="companion:girl", target="provider:media-planner",
        payload_ref=opportunity.event_snapshot_ref, payload_hash=opportunity.event_snapshot_hash, provider_media_grant=None,
        idempotency_key=request, budget_reservation_id="reservation:1", claim_lease=lease, dispatch_pending=None,
        state="delivered", recovery_policy="effect_once",
    )
    receipt = ExecutionReceipt(
        receipt_id="receipt:planning:1", result_id="result:planning:1", action_id=action.action_id,
        provider="provider:media-planner", provider_ref=request, source_event_id="source:planning:1",
        receipt_kind="terminal", observed_state="delivered", is_terminal=True, cost_actual=0,
        received_at=NOW, raw_payload_hash="sha256:" + "b" * 64,
    )
    state = state.model_copy(update={"actions": (action,), "execution_receipts": (receipt,)})
    plan_body = '{"plan":"frozen"}'
    plan = MediaPlan(
        plan_id="plan:1", planning_request_id=request, opportunity_id=opportunity.opportunity_id,
        event_snapshot_hash=opportunity.event_snapshot_hash, family="life_share", planner_version="planner.1",
        schema_version="media-plan.1", plan_payload_ref="sidecar:plan:1", plan_payload_hash=media_payload_hash(plan_body), frozen_at=NOW,
    )
    state = reduce_event(state, _event("MediaPlanRecorded", MediaPlanRecordedPayload(action_id=action.action_id, receipt_id=receipt.receipt_id, plan=plan).model_dump(mode="json"), "plan"))
    assert state.media_plans == (plan,)
    trigger_id = continuation_trigger_id(plan)
    state = reduce_event(state, _event("TriggerProcessOpened", {"process": TriggerProcess(
        trigger_id=trigger_id, trigger_ref=trigger_id, process_kind="media_continuation",
        source_evidence_ref="event:media-test:plan", state="open",
    ).model_dump(mode="json")}, "continuation"))
    assert state.trigger_processes[0].trigger_id == trigger_id

    tampered = plan.model_copy(update={"event_snapshot_hash": "sha256:" + hashlib.sha256(b"other").hexdigest()})
    with pytest.raises(ValueError, match="bound to one delivered"):
        reduce_event(state, _event("MediaPlanRecorded", {"action_id": action.action_id, "receipt_id": receipt.receipt_id, "plan": tampered.model_dump(mode="json")}, "tampered"))


def test_media_freeze_replays_across_sqlite_restart_and_sidecar_ref_cannot_rebind(tmp_path) -> None:
    path = tmp_path / "media-v2.sqlite3"
    ledger = SQLiteWorldLedger(path=path, world_id=WORLD)
    start = _event("WorldStarted", {}, "start")
    ledger.commit((start,), expected_world_revision=0, expected_deliberation_revision=0)
    candidate = PhotoCandidate(candidate_id="candidate:1", source_event_refs=(start.event_id,), family="life_share", privacy_ceiling="personal")
    snapshot = _snapshot(event_ref=start.event_id, payload_hash=start.payload_hash)
    opportunity = _opportunity(event_ref=start.event_id, payload_hash=start.payload_hash)
    store = SQLiteImmutableMediaPayloadStore(path=str(path), world_id=WORLD)
    record = StoredMediaPayload(payload_ref=opportunity.event_snapshot_ref, payload_hash=opportunity.event_snapshot_hash,
        content_type="application/vnd.world-v2.media-opportunity+json", body=snapshot)
    store.put_if_absent(record)
    with pytest.raises(ValueError, match="different immutable bytes"):
        store.put_if_absent(StoredMediaPayload(payload_ref=record.payload_ref, payload_hash=media_payload_hash('{"other":true}'),
            content_type=record.content_type, body='{"other":true}'))
    events = []
    for event_type, payload, suffix in (
        ("PhotoCandidateOpened", {"candidate": candidate.model_dump(mode="json")}, "candidate"),
        ("MediaOpportunityFrozen", {"opportunity": opportunity.model_dump(mode="json")}, "opportunity"),
    ):
        events.append(_event(event_type, payload, suffix).model_copy(update={
            "idempotency_key": domain_idempotency_key(event_type=event_type, world_id=WORLD, payload=payload),
        }))
    projection = ledger.project()
    cursor = ProjectionCursor(
        world_revision=projection.world_revision,
        deliberation_revision=projection.deliberation_revision,
        ledger_sequence=projection.ledger_sequence,
    )
    ledger.commit_at_cursor(tuple(events), expected_cursor=cursor, commit_id="commit:media-freeze-test")
    expected = ledger.project()
    ledger.close()
    reopened = SQLiteWorldLedger(path=path, world_id=WORLD)
    assert reopened.project() == expected
    assert reopened.rebuild() == expected
    reopened.close()
    store.close()

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from companion_daemon.world_v2.accepted_ledger_batch import AcceptedLedgerBatchIssuer
from companion_daemon.world_v2.action_pump import ActionPump
from companion_daemon.world_v2.event_identity import domain_idempotency_key
from companion_daemon.world_v2.media_continuation_runtime import MediaContinuationRuntime
from companion_daemon.world_v2.media_execution_runtime import MediaExecutionRuntime
from companion_daemon.world_v2.media_planning_runtime import MediaPlanningRuntime
from companion_daemon.world_v2.media_planning_worker import MediaPlanningWorker
from companion_daemon.world_v2.media_v2 import (
    FrozenMediaEvidenceSnapshot,
    MediaEvidenceSource,
    MediaOpportunity,
    MediaPlan,
    MediaPlanningResult,
    PhotoCandidate,
    SQLiteImmutableMediaPayloadStore,
    StoredMediaPayload,
    canonical_media_json,
    continuation_trigger_id,
    artifact_continuation_trigger_id,
    media_payload_hash,
)
from companion_daemon.world_v2.schemas import (
    BudgetAccount,
    ExternalObservation,
    ProviderReceipt,
    ProviderMediaGrantBinding,
    ProjectionCursor,
    WorldEvent,
)
from companion_daemon.world_v2.sqlite_ledger import SQLiteWorldLedger
from companion_daemon.world_v2.settlement import SettlementPlanner


NOW = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
WORLD = "world:media-continuation-test"


def _cursor(projection) -> ProjectionCursor:
    return ProjectionCursor(
        world_revision=projection.world_revision,
        deliberation_revision=projection.deliberation_revision,
        ledger_sequence=projection.ledger_sequence,
    )


def _event(*, event_type: str, event_id: str, payload: dict[str, object], cause: str) -> WorldEvent:
    return WorldEvent.from_payload(
        schema_version="world-v2.1", event_id=event_id, event_type=event_type,
        world_id=WORLD, logical_time=NOW, created_at=NOW, actor="system:test",
        source="test", trace_id="trace:media-continuation", causation_id=cause,
        correlation_id="correlation:media-continuation",
        idempotency_key=(
            domain_idempotency_key(event_type=event_type, world_id=WORLD, payload=payload)
            or "test:" + event_id
        ),
        payload=payload,
    )


async def _seed_settled_plan(*, ledger: SQLiteWorldLedger, store, monkeypatch) -> MediaPlan:
    monkeypatch.setattr(
        "companion_daemon.world_v2.reducers.require_provider_media_grant", lambda **_kwargs: object()
    )
    monkeypatch.setattr(
        "companion_daemon.world_v2.media_planning_worker.require_provider_media_grant",
        lambda **_kwargs: object(),
    )
    start = _event(event_type="WorldStarted", event_id="event:start", payload={}, cause="cause:start")
    account = BudgetAccount(
        account_id="account:image", category="image", window_id="window:image", limit=10
    )
    account_payload = {"account": account.model_dump(mode="json")}
    account_event = _event(
        event_type="BudgetAccountConfigured", event_id="event:account",
        payload=account_payload, cause=start.event_id,
    )
    ledger.commit(
        (start, account_event), expected_world_revision=0, expected_deliberation_revision=0
    )
    snapshot = canonical_media_json(
        FrozenMediaEvidenceSnapshot(
            source_events=(
                MediaEvidenceSource(event_ref=start.event_id, payload_hash=start.payload_hash),
            )
        ).model_dump(mode="json")
    )
    candidate = PhotoCandidate(
        candidate_id="candidate:continuation", source_event_refs=(start.event_id,),
        family="life_share", privacy_ceiling="personal",
    )
    opportunity = MediaOpportunity(
        opportunity_id="opportunity:continuation", candidate_id=candidate.candidate_id,
        family="life_share", delivery_mode="preview", privacy_ceiling="personal",
        event_snapshot_ref="sidecar:snapshot:continuation",
        event_snapshot_hash=media_payload_hash(snapshot), source_event_refs=(start.event_id,),
        catalog_version="test-media.1", expires_at=NOW + timedelta(hours=1),
    )
    planning = MediaPlanningRuntime(ledger=ledger, sidecar=store)
    planning.freeze_and_authorize(
        candidate=candidate, opportunity=opportunity, snapshot_body=snapshot,
        actor="agent:companion",
        grant=ProviderMediaGrantBinding(grant_id="grant:test", grant_revision=1),
        account_id=account.account_id, amount_limit=0, logical_time=NOW,
        trace_id="trace:media-continuation", correlation_id="correlation:media-continuation",
    )
    plan_body = canonical_media_json({"schema_version": "media-plan.1", "prompt": "frozen"})
    plan_payload = StoredMediaPayload(
        payload_ref="sidecar:plan:continuation", payload_hash=media_payload_hash(plan_body),
        content_type="application/vnd.world-v2.media-plan+json", body=plan_body,
    )
    action = next(item for item in ledger.project().actions if item.kind == "media_planning")
    plan = MediaPlan(
        plan_id="plan:continuation", planning_request_id=action.idempotency_key,
        opportunity_id=opportunity.opportunity_id,
        event_snapshot_hash=opportunity.event_snapshot_hash, family=opportunity.family,
        planner_version="test-planner.1", schema_version="media-plan.1",
        plan_payload_ref=plan_payload.payload_ref, plan_payload_hash=plan_payload.payload_hash,
        frozen_at=NOW,
    )

    class Planner:
        async def lookup(self, *, planning_request_id: str):
            assert planning_request_id == action.idempotency_key
            return None

        async def plan(self, *, opportunity: MediaOpportunity, planning_request_id: str):
            assert opportunity.opportunity_id == plan.opportunity_id
            assert planning_request_id == plan.planning_request_id
            return MediaPlanningResult(plan=plan, plan_payload=plan_payload)

    result = await MediaPlanningWorker(
        ledger=ledger, runtime=planning, planner=Planner(), owner_id="worker:planning"
    ).drain_once()
    assert result.status == "planned"
    return plan


async def _deliver_action(*, ledger: SQLiteWorldLedger, action_id: str) -> None:
    planner = SettlementPlanner(world_id=WORLD)

    async def settle(result: ExternalObservation) -> None:
        before = ledger.project()
        trigger_id = f"trigger:settlement:{result.source}:{result.source_event_id}"
        ledger.commit_at_cursor(
            planner.recording_events(result, trigger_id=trigger_id),
            expected_cursor=_cursor(before), commit_id="commit:test:media-result-inbox:" + action_id,
        )
        after = ledger.project()
        plan = planner.plan(result, trigger_id=trigger_id, projection=after)
        ledger.commit_at_cursor(
            plan.events, expected_cursor=_cursor(after),
            commit_id="commit:test:media-result-settlement:" + action_id,
        )

    class Executor:
        async def dispatch(self, action):
            return ProviderReceipt(
                provider_receipt_id="provider-receipt:" + action.action_id,
                action_id=action.action_id, idempotency_key=action.idempotency_key,
                provider="provider:test-media", provider_ref="provider-ref:" + action.action_id,
                status="delivered", artifact_refs=(), cost_actual=0, received_at=NOW,
                raw_payload_hash="sha256:" + "9" * 64,
            )

        async def lookup_result(self, action):
            return await self.dispatch(action)

    result = await ActionPump(
        ledger=ledger, executor=Executor(), settle=settle, owner_id="worker:test-action"
    ).drain_action(action_id)
    assert result.status == "settled"


@pytest.mark.asyncio
async def test_sqlite_restart_joins_proposal_then_accepts_once(monkeypatch, tmp_path) -> None:
    path = tmp_path / "media-continuation.sqlite3"
    issuer = AcceptedLedgerBatchIssuer()
    ledger = SQLiteWorldLedger(path=path, world_id=WORLD, accepted_batch_issuer=issuer)
    store = SQLiteImmutableMediaPayloadStore(path=str(path), world_id=WORLD)
    plan = await _seed_settled_plan(ledger=ledger, store=store, monkeypatch=monkeypatch)
    execution = MediaExecutionRuntime(ledger=ledger, sidecar=store)
    runtime = MediaContinuationRuntime(
        ledger=ledger, execution=execution, batch_issuer=issuer
    )
    proposal, commit = runtime.propose(
        trigger_id=continuation_trigger_id(plan), actor="worker:continuation",
        logical_time=NOW, trace_id="trace:media-continuation",
        correlation_id="correlation:media-continuation",
    )
    assert commit is not None
    assert not any(item.kind == "media_render" for item in ledger.project().actions)
    ledger.close()
    store.close()

    reopened = SQLiteWorldLedger(path=path, world_id=WORLD, accepted_batch_issuer=issuer)
    reopened_store = SQLiteImmutableMediaPayloadStore(path=str(path), world_id=WORLD)
    resumed = MediaContinuationRuntime(
        ledger=reopened,
        execution=MediaExecutionRuntime(ledger=reopened, sidecar=reopened_store),
        batch_issuer=issuer,
    )
    same, duplicate = resumed.propose(
        trigger_id=continuation_trigger_id(plan), actor="worker:continuation",
        logical_time=NOW, trace_id="trace:media-continuation",
        correlation_id="correlation:media-continuation",
    )
    assert same == proposal
    assert duplicate is None
    action = resumed.accept(
        trigger_id=continuation_trigger_id(plan), actor="worker:continuation",
        owner_id="worker:continuation", grant=ProviderMediaGrantBinding(
            grant_id="grant:test", grant_revision=1
        ),
        account_id="account:image", amount_limit=0, logical_time=NOW,
        trace_id="trace:media-continuation", correlation_id="correlation:media-continuation",
    )
    projection = reopened.project()
    assert action.kind == "media_render"
    assert next(
        item for item in projection.trigger_processes
        if item.trigger_id == continuation_trigger_id(plan)
    ).state == "terminal"
    assert len([item for item in projection.actions if item.kind == "media_render"]) == 1
    assert resumed.accept(
        trigger_id=continuation_trigger_id(plan), actor="worker:continuation",
        owner_id="worker:continuation", grant=ProviderMediaGrantBinding(
            grant_id="grant:test", grant_revision=1
        ),
        account_id="account:image", amount_limit=0, logical_time=NOW,
        trace_id="trace:media-continuation", correlation_id="correlation:media-continuation",
    ).action_id == action.action_id
    reopened.close()
    reopened_store.close()


@pytest.mark.asyncio
async def test_direct_render_authorization_is_not_an_authority_bypass(monkeypatch, tmp_path) -> None:
    path = tmp_path / "media-continuation-direct.sqlite3"
    issuer = AcceptedLedgerBatchIssuer()
    ledger = SQLiteWorldLedger(path=path, world_id=WORLD, accepted_batch_issuer=issuer)
    store = SQLiteImmutableMediaPayloadStore(path=str(path), world_id=WORLD)
    plan = await _seed_settled_plan(ledger=ledger, store=store, monkeypatch=monkeypatch)
    execution = MediaExecutionRuntime(ledger=ledger, sidecar=store)
    with pytest.raises(ValueError, match="MediaContinuationRuntime Acceptance"):
        execution.authorize_render(
            plan_id=plan.plan_id, actor="worker:legacy",
            grant=ProviderMediaGrantBinding(grant_id="grant:test", grant_revision=1),
            account_id="account:image", amount_limit=0, logical_time=NOW,
            trace_id="trace:media-continuation", correlation_id="correlation:media-continuation",
        )
    assert not any(item.kind == "media_render" for item in ledger.project().actions)
    ledger.close()
    store.close()


@pytest.mark.asyncio
async def test_render_artifact_opens_and_accepts_inspection_after_sqlite_restart(
    monkeypatch, tmp_path
) -> None:
    path = tmp_path / "media-continuation-inspect.sqlite3"
    issuer = AcceptedLedgerBatchIssuer()
    ledger = SQLiteWorldLedger(path=path, world_id=WORLD, accepted_batch_issuer=issuer)
    store = SQLiteImmutableMediaPayloadStore(path=str(path), world_id=WORLD)
    plan = await _seed_settled_plan(ledger=ledger, store=store, monkeypatch=monkeypatch)
    execution = MediaExecutionRuntime(ledger=ledger, sidecar=store)
    continuation = MediaContinuationRuntime(
        ledger=ledger, execution=execution, batch_issuer=issuer
    )
    continuation.propose(
        trigger_id=continuation_trigger_id(plan), actor="worker:continuation",
        logical_time=NOW, trace_id="trace:media-continuation",
        correlation_id="correlation:media-continuation",
    )
    render = continuation.accept(
        trigger_id=continuation_trigger_id(plan), actor="worker:continuation",
        owner_id="worker:continuation",
        grant=ProviderMediaGrantBinding(grant_id="grant:test", grant_revision=1),
        account_id="account:image", amount_limit=0, logical_time=NOW,
        trace_id="trace:media-continuation", correlation_id="correlation:media-continuation",
    )
    await _deliver_action(ledger=ledger, action_id=render.action_id)
    receipt = next(
        item for item in ledger.project().execution_receipts if item.action_id == render.action_id
    )
    artifact_body = canonical_media_json({"encoding": "base64", "bytes": "dGVzdA=="})
    artifact_payload = StoredMediaPayload(
        payload_ref="sidecar:artifact:continuation",
        payload_hash=media_payload_hash(artifact_body),
        content_type="application/vnd.world-v2.media-artifact+json", body=artifact_body,
    )
    artifact = execution.record_rendered_artifact(
        action_id=render.action_id, receipt_id=receipt.receipt_id,
        artifact_payload=artifact_payload, logical_time=NOW,
    )
    inspect_trigger = artifact_continuation_trigger_id(artifact)
    assert next(
        item for item in ledger.project().trigger_processes if item.trigger_id == inspect_trigger
    ).state == "open"
    with pytest.raises(ValueError, match="MediaContinuationRuntime Acceptance"):
        execution.authorize_inspection(
            artifact_id=artifact.artifact_id, actor="worker:legacy",
            grant=ProviderMediaGrantBinding(grant_id="grant:test", grant_revision=1),
            account_id="account:image", amount_limit=0, logical_time=NOW,
            trace_id="trace:media-continuation",
            correlation_id="correlation:media-continuation",
        )
    proposal, commit = continuation.propose(
        trigger_id=inspect_trigger, actor="worker:continuation", logical_time=NOW,
        trace_id="trace:media-continuation", correlation_id="correlation:media-continuation",
    )
    assert proposal.continuation_step == "render_to_inspect"
    assert commit is not None
    ledger.close()
    store.close()

    reopened = SQLiteWorldLedger(path=path, world_id=WORLD, accepted_batch_issuer=issuer)
    reopened_store = SQLiteImmutableMediaPayloadStore(path=str(path), world_id=WORLD)
    resumed = MediaContinuationRuntime(
        ledger=reopened,
        execution=MediaExecutionRuntime(ledger=reopened, sidecar=reopened_store),
        batch_issuer=issuer,
    )
    inspection = resumed.accept(
        trigger_id=inspect_trigger, actor="worker:continuation",
        owner_id="worker:continuation",
        grant=ProviderMediaGrantBinding(grant_id="grant:test", grant_revision=1),
        account_id="account:image", amount_limit=0, logical_time=NOW,
        trace_id="trace:media-continuation", correlation_id="correlation:media-continuation",
    )
    projection = reopened.project()
    assert inspection.kind == "media_inspection"
    assert inspection.intent_ref == artifact.artifact_id
    assert next(
        item for item in projection.trigger_processes if item.trigger_id == inspect_trigger
    ).state == "terminal"
    assert not projection.media_deliveries
    reopened.close()
    reopened_store.close()


@pytest.mark.asyncio
async def test_competing_acceptance_has_one_action_and_loser_joins(monkeypatch, tmp_path) -> None:
    path = tmp_path / "media-continuation-race.sqlite3"
    issuer = AcceptedLedgerBatchIssuer()
    seed_ledger = SQLiteWorldLedger(path=path, world_id=WORLD, accepted_batch_issuer=issuer)
    seed_store = SQLiteImmutableMediaPayloadStore(path=str(path), world_id=WORLD)
    plan = await _seed_settled_plan(
        ledger=seed_ledger, store=seed_store, monkeypatch=monkeypatch
    )
    seed_runtime = MediaContinuationRuntime(
        ledger=seed_ledger,
        execution=MediaExecutionRuntime(ledger=seed_ledger, sidecar=seed_store),
        batch_issuer=issuer,
    )
    trigger_id = continuation_trigger_id(plan)
    seed_runtime.propose(
        trigger_id=trigger_id, actor="worker:continuation", logical_time=NOW,
        trace_id="trace:media-continuation", correlation_id="correlation:media-continuation",
    )
    seed_ledger.close()
    seed_store.close()

    barrier = Barrier(2)

    def contender() -> tuple[str, str | None]:
        ledger = SQLiteWorldLedger(path=path, world_id=WORLD, accepted_batch_issuer=issuer)
        store = SQLiteImmutableMediaPayloadStore(path=str(path), world_id=WORLD)
        execution = MediaExecutionRuntime(ledger=ledger, sidecar=store)
        original = execution.prepare_render_authorization

        def synchronized(**kwargs):
            material = original(**kwargs)
            barrier.wait(timeout=5)
            return material

        execution.prepare_render_authorization = synchronized  # type: ignore[method-assign]
        runtime = MediaContinuationRuntime(
            ledger=ledger, execution=execution, batch_issuer=issuer
        )
        try:
            action = runtime.accept(
                trigger_id=trigger_id, actor="worker:continuation",
                owner_id="worker:continuation",
                grant=ProviderMediaGrantBinding(grant_id="grant:test", grant_revision=1),
                account_id="account:image", amount_limit=0, logical_time=NOW,
                trace_id="trace:media-continuation",
                correlation_id="correlation:media-continuation",
            )
            return "accepted", action.action_id
        except Exception as exc:  # one CAS loser is expected
            return type(exc).__name__, None
        finally:
            ledger.close()
            store.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(lambda _index: contender(), range(2)))
    accepted_ids = tuple(action for status, action in results if status == "accepted")
    assert accepted_ids
    assert len(set(accepted_ids)) == 1

    joined_ledger = SQLiteWorldLedger(path=path, world_id=WORLD, accepted_batch_issuer=issuer)
    joined_store = SQLiteImmutableMediaPayloadStore(path=str(path), world_id=WORLD)
    joined = MediaContinuationRuntime(
        ledger=joined_ledger,
        execution=MediaExecutionRuntime(ledger=joined_ledger, sidecar=joined_store),
        batch_issuer=issuer,
    ).accept(
        trigger_id=trigger_id, actor="worker:continuation", owner_id="worker:continuation",
        grant=ProviderMediaGrantBinding(grant_id="grant:test", grant_revision=1),
        account_id="account:image", amount_limit=0, logical_time=NOW,
        trace_id="trace:media-continuation", correlation_id="correlation:media-continuation",
    )
    projection = joined_ledger.project()
    assert joined.action_id == accepted_ids[0]
    assert len([item for item in projection.actions if item.kind == "media_render"]) == 1
    assert next(item for item in projection.trigger_processes if item.trigger_id == trigger_id).state == "terminal"
    joined_ledger.close()
    joined_store.close()

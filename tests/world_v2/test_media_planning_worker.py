from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from companion_daemon.world_v2.ledger import WorldLedger
from companion_daemon.world_v2.media_planning_runtime import MediaPlanningRuntime
from companion_daemon.world_v2.media_planning_worker import MediaPlanningWorker
from companion_daemon.world_v2.media_v2 import (
    FrozenMediaEvidenceSnapshot,
    MediaEvidenceSource,
    MediaNotRenderable,
    MediaOpportunity,
    MediaPlanningResult,
    PhotoCandidate,
    SQLiteImmutableMediaPayloadStore,
    canonical_media_json,
    media_payload_hash,
    planning_request_id,
)
from companion_daemon.world_v2.schemas import (
    Action,
    BudgetAccount,
    ProviderMediaGrantBinding,
    WorldEvent,
)


NOW = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)


def _action(*, state: str = "authorized") -> Action:
    return Action.model_construct(
        schema_version="world-v2.1",
        action_id="action:media-planning:1",
        world_id="world:media-planning-worker",
        logical_time=NOW,
        created_at=NOW,
        trace_id="trace:media-planning-worker",
        causation_id="cause:media-planning-worker",
        correlation_id="correlation:media-planning-worker",
        kind="media_planning",
        layer="media_action",
        intent_ref="opportunity:frozen-only",
        actor="agent:companion",
        target="provider:media-planner",
        payload_ref="sidecar:frozen-snapshot",
        payload_hash="sha256:" + "a" * 64,
        provider_media_grant={"grant_id": "grant:media", "grant_revision": 1},
        idempotency_key="media-plan-request:frozen-only",
        budget_reservation_id="reservation:media-planning:1",
        state=state,
        recovery_policy="effect_once",
    )


class _Ledger:
    def __init__(self, projection) -> None:
        self._projection = projection

    def project(self):
        return self._projection


class _Runtime:
    def __init__(self, ledger: _Ledger) -> None:
        self._ledger = ledger
        self.calls: list[str] = []

    async def execute_planning_once(self, *, action_id: str, planner) -> None:
        self.calls.append(action_id)
        action = self._ledger.project().actions[0]
        self._ledger._projection = SimpleNamespace(  # noqa: SLF001 - test-only ledger double
            actions=(action.model_copy(update={"state": "delivered"}),),
            media_plans=(SimpleNamespace(opportunity_id=action.intent_ref),),
            media_unrenderable_opportunity_ids=(),
            logical_time=NOW,
        )


class _Planner:
    pass


@pytest.mark.asyncio
async def test_worker_is_idle_for_candidates_or_opportunities_without_an_authorized_action() -> None:
    ledger = _Ledger(
        SimpleNamespace(
            actions=(),
            # A scheduler must not turn either of these into a planning Action.
            photo_candidates=(SimpleNamespace(candidate_id="candidate:unselected"),),
            media_opportunities=(SimpleNamespace(opportunity_id="opportunity:frozen-but-unplanned"),),
            media_plans=(),
            media_unrenderable_opportunity_ids=(),
            logical_time=NOW,
        )
    )
    worker = MediaPlanningWorker(
        ledger=ledger, runtime=_Runtime(ledger), planner=_Planner(), owner_id="worker:media"
    )

    result = await worker.drain_once()
    assert result.status == "idle"
    assert result.action_id is None


@pytest.mark.asyncio
async def test_worker_fails_closed_when_a_frozen_action_has_no_composed_planner() -> None:
    action = _action()
    ledger = _Ledger(
        SimpleNamespace(
            actions=(action,), media_plans=(), media_unrenderable_opportunity_ids=(), logical_time=NOW
        )
    )
    runtime = _Runtime(ledger)
    worker = MediaPlanningWorker(ledger=ledger, runtime=runtime, planner=None, owner_id="worker:media")

    result = await worker.drain_once()
    assert result.status == "unavailable"
    assert result.action_id == action.action_id
    assert runtime.calls == []
    assert ledger.project().actions == (action,)


@pytest.mark.asyncio
async def test_worker_uses_the_dedicated_action_lifecycle_then_reports_its_recorded_plan(monkeypatch) -> None:
    action = _action()
    ledger = _Ledger(
        SimpleNamespace(
            actions=(action,), media_plans=(), media_unrenderable_opportunity_ids=(), logical_time=NOW
        )
    )
    runtime = _Runtime(ledger)
    authorization_calls: list[str] = []

    class _Pump:
        def __init__(self, *, executor, **_kwargs) -> None:  # type: ignore[no-untyped-def]
            self._executor = executor

        async def drain_action(self, action_id: str) -> None:
            current = ledger.project().actions[0]
            assert current.action_id == action_id
            await self._executor.assert_dispatch_authorized(
                action=current, projection=ledger.project()
            )
            await self._executor.dispatch(current)

    monkeypatch.setattr("companion_daemon.world_v2.media_planning_worker.ActionPump", _Pump)
    monkeypatch.setattr(
        "companion_daemon.world_v2.media_planning_worker.require_provider_media_grant",
        lambda *, action, **_kwargs: authorization_calls.append(action.action_id),
    )
    worker = MediaPlanningWorker(
        ledger=ledger, runtime=runtime, planner=_Planner(), owner_id="worker:media"
    )

    result = await worker.drain_once()
    assert result.status == "planned"
    assert result.action_id == action.action_id
    assert authorization_calls == [action.action_id]
    assert runtime.calls == [action.action_id]


@pytest.mark.asyncio
async def test_worker_runs_a_real_frozen_action_without_authoring_an_opportunity(monkeypatch, tmp_path) -> None:
    """The scheduler may settle a frozen Action, but never create its inputs."""

    world_id = "world:media-planning-worker-real"
    ledger = WorldLedger.in_memory(world_id=world_id)
    start = WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id="event:world-started:media-planning-worker",
        event_type="WorldStarted",
        world_id=world_id,
        logical_time=NOW,
        created_at=NOW,
        actor="system:test",
        source="test",
        trace_id="trace:media-planning-worker-real",
        causation_id="cause:media-planning-worker-real",
        correlation_id="correlation:media-planning-worker-real",
        idempotency_key="world-started:media-planning-worker-real",
        payload={},
    )
    account = BudgetAccount(
        account_id="account:media-test", category="image", window_id="window:media-test", limit=10
    )
    ledger.commit(
        (
            start,
            WorldEvent.from_payload(
                schema_version="world-v2.1",
                event_id="event:media-account",
                event_type="BudgetAccountConfigured",
                world_id=world_id,
                logical_time=NOW,
                created_at=NOW,
                actor="system:test",
                source="test",
                trace_id=start.trace_id,
                causation_id=start.event_id,
                correlation_id=start.correlation_id,
                idempotency_key="media-account",
                payload={"account": account.model_dump(mode="json")},
            ),
        ),
        expected_world_revision=0,
        expected_deliberation_revision=0,
    )
    snapshot = canonical_media_json(
        FrozenMediaEvidenceSnapshot(
            source_events=(MediaEvidenceSource(event_ref=start.event_id, payload_hash=start.payload_hash),)
        ).model_dump(mode="json")
    )
    candidate = PhotoCandidate(
        candidate_id="candidate:real-frozen",
        source_event_refs=(start.event_id,),
        family="life_share",
        privacy_ceiling="personal",
    )
    opportunity = MediaOpportunity(
        opportunity_id="opportunity:real-frozen",
        candidate_id=candidate.candidate_id,
        family="life_share",
        delivery_mode="preview",
        privacy_ceiling="personal",
        event_snapshot_ref="sidecar:real-frozen-snapshot",
        event_snapshot_hash=media_payload_hash(snapshot),
        source_event_refs=(start.event_id,),
        catalog_version="test-media-catalog.1",
        expires_at=NOW.replace(hour=13),
    )
    # The grant vertical has its own exhaustive enforcement tests.  This test
    # isolates scheduling, so it permits a synthetic grant only while the
    # reducer and worker both see the same checked boundary.
    monkeypatch.setattr(
        "companion_daemon.world_v2.reducers.require_provider_media_grant",
        lambda **_kwargs: object(),
    )
    monkeypatch.setattr(
        "companion_daemon.world_v2.media_planning_worker.require_provider_media_grant",
        lambda **_kwargs: object(),
    )
    store = SQLiteImmutableMediaPayloadStore(path=str(tmp_path / "media.sqlite"), world_id=world_id)
    runtime = MediaPlanningRuntime(ledger=ledger, sidecar=store)
    runtime.freeze_and_authorize(
        candidate=candidate,
        opportunity=opportunity,
        snapshot_body=snapshot,
        actor="agent:companion",
        grant=ProviderMediaGrantBinding(grant_id="grant:test", grant_revision=1),
        account_id=account.account_id,
        amount_limit=0,
        logical_time=NOW,
        trace_id="trace:media-planning-worker-real",
        correlation_id="correlation:media-planning-worker-real",
    )

    class _NotRenderablePlanner:
        async def lookup(self, *, planning_request_id: str):
            assert planning_request_id == planning_request_id_for_test
            return None

        async def plan(self, *, opportunity: MediaOpportunity, planning_request_id: str):
            assert opportunity == frozen_opportunity
            assert planning_request_id == planning_request_id_for_test
            return MediaPlanningResult(
                not_renderable=MediaNotRenderable(
                    opportunity_id=opportunity.opportunity_id,
                    planning_request_id=planning_request_id,
                    event_snapshot_hash=opportunity.event_snapshot_hash,
                    reason_code="test_no_render",
                    planner_version="test-planner.1",
                )
            )

    frozen_opportunity = opportunity
    planning_request_id_for_test = planning_request_id(opportunity.opportunity_id)
    try:
        result = await MediaPlanningWorker(
            ledger=ledger,
            runtime=runtime,
            planner=_NotRenderablePlanner(),
            owner_id="worker:media-real",
        ).drain_once()
        projection = ledger.project()
    finally:
        store.close()

    assert result.status == "not_renderable"
    assert projection.photo_candidates == (candidate,)
    assert projection.media_opportunities == (opportunity,)
    assert projection.media_unrenderable_opportunity_ids == (opportunity.opportunity_id,)
    action = next(item for item in projection.actions if item.kind == "media_planning")
    assert action.state == "delivered"

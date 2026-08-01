from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import hashlib
from types import SimpleNamespace

import httpx
import pytest

from companion_daemon.world_v2.action_pump import ActionPump
from companion_daemon.world_v2.ledger import WorldLedger
from companion_daemon.world_v2.platform_action_executor import (
    PlatformActionExecutor,
    ResolvedActionPayload,
)
from companion_daemon.world_v2.platform_host import WorldV2PlatformHost
from companion_daemon.world_v2.qq_c2c_transport import QQC2CPlatformTransport
from companion_daemon.world_v2.runtime import WorldRuntime
from companion_daemon.world_v2.schemas import (
    Action,
    BudgetAccount,
    BudgetReservation,
    ClockObservation,
    DispatchPending,
    ExternalObservation,
    ProviderReceipt,
    WorldEvent,
)
from companion_daemon.world_v2.settlement import SettlementPlanner
from companion_daemon.world_v2.sqlite_ledger import SQLiteWorldLedger


NOW = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)
WORLD = "world:action-pump"


def _action(
    *,
    recovery_policy: str = "effect_once",
    logical_time: datetime = NOW,
    expires_at: datetime | None = None,
) -> Action:
    return Action(
        schema_version="world-v2.1",
        action_id="action:reply:1",
        world_id=WORLD,
        logical_time=logical_time,
        created_at=NOW,
        trace_id="trace:action-pump",
        causation_id="acceptance:reply:1",
        correlation_id="conversation:1",
        kind="reply",
        layer="external_action",
        intent_ref="intent:reply:1",
        actor="agent:companion",
        target="user:primary",
        payload_ref="payload:reply:1",
        payload_hash="sha256:reply:1",
        idempotency_key="action-pump:reply:1",
        expires_at=expires_at,
        budget_reservation_id="reservation:reply:1",
        state="authorized",
        recovery_policy=recovery_policy,
    )


def test_ordered_expression_beats_may_follow_provider_acceptance_without_claiming_delivery() -> None:
    first = _action().model_copy(
        update={
            "action_id": "action:expression:first",
            "state": "provider_accepted",
            "expression_plan_id": "plan:expression:1",
            "expression_beat_id": "beat:expression:1",
        }
    )
    second = _action().model_copy(
        update={
            "action_id": "action:expression:second",
            "dependencies": (first.action_id,),
            "expression_plan_id": first.expression_plan_id,
            "expression_beat_id": "beat:expression:2",
        }
    )
    ordinary = second.model_copy(
        update={"expression_plan_id": None, "expression_beat_id": None}
    )

    assert ActionPump._dependencies_satisfied(action=second, actions=(first, second))
    assert not ActionPump._dependencies_satisfied(action=ordinary, actions=(first, ordinary))


@pytest.mark.parametrize("typing_state", ["failed", "unknown", "expired"])
def test_visible_expression_may_follow_a_terminal_best_effort_typing_prelude(
    typing_state: str,
) -> None:
    typing = _action().model_copy(
        update={
            "action_id": "action:expression:typing",
            "kind": "typing",
            "state": typing_state,
            "expression_plan_id": "plan:expression:typing-first",
            "expression_beat_id": "beat:expression:typing",
        }
    )
    visible = _action().model_copy(
        update={
            "action_id": "action:expression:visible",
            "dependencies": (typing.action_id,),
            "expression_plan_id": typing.expression_plan_id,
            "expression_beat_id": "beat:expression:visible",
        }
    )

    assert ActionPump._dependencies_satisfied(
        action=visible,
        actions=(typing, visible),
    )


def _event(event_type: str, payload: dict[str, object], suffix: str) -> WorldEvent:
    return WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id=f"event:action-pump-test:{suffix}",
        world_id=WORLD,
        event_type=event_type,
        logical_time=NOW,
        created_at=NOW,
        actor="test",
        source="test",
        trace_id="trace:action-pump",
        causation_id="test",
        correlation_id="conversation:1",
        idempotency_key=f"action-pump-test:{suffix}",
        payload=payload,
    )


def _ready_ledger(
    *,
    recovery_policy: str = "effect_once",
    action_time: datetime = NOW,
    expires_at: datetime | None = None,
    authorized_action: Action | None = None,
    ledger: WorldLedger | SQLiteWorldLedger | None = None,
) -> WorldLedger | SQLiteWorldLedger:
    ledger = ledger or WorldLedger.in_memory(world_id=WORLD)
    action = authorized_action or _action(
        recovery_policy=recovery_policy,
        logical_time=action_time,
        expires_at=expires_at,
    )
    account = BudgetAccount(account_id="account:chat", category="chat", window_id="test", limit=100)
    reservation = BudgetReservation(
        reservation_id="reservation:reply:1",
        account_id=account.account_id,
        action_id=action.action_id,
        category="chat",
        amount_limit=10,
    )
    ledger.commit(
        (
            _event("BudgetAccountConfigured", {"account": account.model_dump(mode="json")}, "account"),
            _event("BudgetReserved", {"reservation": reservation.model_dump(mode="json")}, "reserved"),
            _event(
                "ActionAuthorized",
                {"action": action.model_dump(mode="json")},
                "authorized",
            ),
        ),
        expected_world_revision=0,
        expected_deliberation_revision=0,
    )
    return ledger


class _DeliveredExecutor:
    def __init__(self, *, lookup_delivered: bool = False) -> None:
        self.dispatch_calls = 0
        self.lookup_calls = 0
        self._lookup_delivered = lookup_delivered

    async def dispatch(self, action: Action) -> ProviderReceipt:
        self.dispatch_calls += 1
        return self._receipt(action)

    async def lookup_result(self, action: Action) -> ProviderReceipt | None:
        self.lookup_calls += 1
        return self._receipt(action) if self._lookup_delivered else None

    @staticmethod
    def _receipt(action: Action) -> ProviderReceipt:
        return ProviderReceipt(
            provider_receipt_id="provider-event:reply:1",
            action_id=action.action_id,
            idempotency_key=action.idempotency_key,
            provider="provider:test",
            provider_ref="provider-ref:reply:1",
            status="delivered",
            artifact_refs=(),
            cost_actual=3,
            received_at=NOW,
            raw_payload_hash="sha256:provider-reply-1",
        )


class _PendingExecutor:
    def __init__(self) -> None:
        self.dispatch_calls = 0
        self.lookup_calls = 0

    async def dispatch(self, action: Action) -> DispatchPending:
        self.dispatch_calls += 1
        return DispatchPending(
            action_id=action.action_id,
            idempotency_key=action.idempotency_key,
            provider="provider:test",
            provider_ref="provider-ref:pending:1",
            lookup_after=datetime(2026, 7, 15, 12, 1, tzinfo=UTC),
            deadline=datetime(2026, 7, 15, 12, 10, tzinfo=UTC),
            dispatch_started_at=NOW,
            idempotency_mode="effect_once",
        )

    async def lookup_result(self, _action: Action) -> ProviderReceipt | None:
        self.lookup_calls += 1
        return None


class _MismatchedPendingLookupExecutor(_PendingExecutor):
    async def lookup_result(self, action: Action) -> ProviderReceipt:
        self.lookup_calls += 1
        return ProviderReceipt(
            provider_receipt_id="provider-event:wrong-ref",
            action_id=action.action_id,
            idempotency_key=action.idempotency_key,
            provider="provider:wrong",
            provider_ref="provider-ref:wrong",
            status="delivered",
            cost_actual=0,
            received_at=datetime(2026, 7, 15, 12, 2, tzinfo=UTC),
            raw_payload_hash="sha256:wrong-provider-ref",
        )


class _QQPayloads:
    def __init__(self, *, body: str, payload_ref: str, payload_hash: str) -> None:
        self._payload = ResolvedActionPayload(
            payload_ref=payload_ref,
            payload_hash=payload_hash,
            content_type="text/plain",
            body=body,
        )

    async def resolve(self, _action: Action) -> ResolvedActionPayload:
        return self._payload


class _QQDelivery:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.sent: list[tuple[str, str]] = []

    async def send_text(self, recipient_id: str, text: str) -> dict[str, object]:
        self.sent.append((recipient_id, text))
        if self.error is not None:
            raise self.error
        return {"status": "ok", "data": {"message_id": "qq-message-1"}}


def _qq_action_executor(
    *,
    delivery: _QQDelivery,
    body: str,
    action: Action,
) -> PlatformActionExecutor:
    return PlatformActionExecutor(
        payloads=_QQPayloads(
            body=body,
            payload_ref=action.payload_ref,
            payload_hash=action.payload_hash,
        ),
        transport=QQC2CPlatformTransport(
            delivery=delivery,
            recipients_by_target={action.target: "open-id-1"},
            now=lambda: NOW,
        ),
    )


@pytest.mark.asyncio
async def test_action_pump_persists_start_before_dispatch_and_settles_receipt() -> None:
    ledger = _ready_ledger()
    executor = _DeliveredExecutor()
    runtime = WorldRuntime(
        world_id=WORLD,
        ledger=ledger,
        action_executor=executor,
        action_pump_owner="pump:primary",
    )
    result = await runtime.drain_actions_once()
    assert result is not None

    assert result.status == "settled"
    assert executor.dispatch_calls == 1
    projection = ledger.project()
    assert projection.actions[0].state == "delivered"
    assert projection.execution_receipts[0].observed_state == "delivered"
    assert projection.budget_reservations[0].state == "settled"
    event_types = [item.event.event_type for item in ledger.export_replay_evidence().events]
    assert event_types.index("ActionDispatchStarted") < event_types.index("ExternalObservationRecorded")


@pytest.mark.asyncio
async def test_qq_provider_exception_settles_unknown_without_escaping_action_pump() -> None:
    body = "我在。"
    action = _action().model_copy(
        update={
            "target": "conversation:qq:c2c:owner",
            "payload_ref": "payload:qq-c2c:provider-failure",
            "payload_hash": "sha256:" + hashlib.sha256(body.encode()).hexdigest(),
        }
    )
    request = httpx.Request("POST", "http://127.0.0.1:3000/send_private_msg")
    delivery = _QQDelivery(
        httpx.HTTPStatusError(
            "NapCat is offline",
            request=request,
            response=httpx.Response(502, request=request),
        )
    )
    ledger = _ready_ledger(authorized_action=action)
    runtime = WorldRuntime(
        world_id=WORLD,
        ledger=ledger,
        action_executor=_qq_action_executor(
            delivery=delivery,
            body=body,
            action=action,
        ),
        action_pump_owner="pump:primary",
    )

    failed_send = await runtime.drain_action(action.action_id)
    next_pass = await runtime.drain_actions_once()

    assert failed_send is not None
    assert failed_send.status == "settled"
    assert failed_send.provider_status == "unknown"
    assert next_pass is not None and next_pass.status == "idle"
    assert delivery.sent == [("open-id-1", body)]
    assert ledger.project().actions[0].state == "unknown"


@pytest.mark.asyncio
async def test_targeted_ingress_batches_schedule_claim_and_dispatch_start() -> None:
    ledger = _ready_ledger()
    executor = _DeliveredExecutor()
    runtime = WorldRuntime(
        world_id=WORLD,
        ledger=ledger,
        action_executor=executor,
        action_pump_owner="pump:primary",
    )

    result = await runtime.drain_action("action:reply:1")

    assert result is not None and result.status == "settled"
    events = ledger.export_replay_evidence().events
    lifecycle = [
        item.event.event_type
        for item in events
        if item.event.event_type
        in {"ActionScheduled", "ActionClaimed", "ActionDispatchStarted"}
    ]
    assert lifecycle == ["ActionScheduled", "ActionClaimed", "ActionDispatchStarted"]
    assert executor.dispatch_calls == 1


@pytest.mark.asyncio
async def test_action_pump_can_exclude_a_dedicated_scheduler_lane() -> None:
    ledger = _ready_ledger()
    executor = _DeliveredExecutor()
    runtime = WorldRuntime(
        world_id=WORLD,
        ledger=ledger,
        action_executor=executor,
        action_pump_owner="pump:primary",
        action_pump_excluded_kinds=frozenset({"reply"}),
    )

    result = await runtime.drain_actions_once()

    assert result is not None and result.status == "idle"
    assert executor.dispatch_calls == 0
    assert ledger.project().actions[0].state == "authorized"


def _mark_dispatch_started(ledger: WorldLedger | SQLiteWorldLedger) -> None:
    action = ledger.project().actions[0]
    claim = {
        "action_id": action.action_id,
        "claim_lease": {
            "owner_id": "pump:dead",
            "attempt_id": "attempt:dead",
            "acquired_at": NOW.isoformat(),
            "expires_at": "2026-07-15T12:02:00+00:00",
        },
    }
    ledger.commit(
        (_event("ActionScheduled", {"action_id": action.action_id}, "scheduled"),),
        expected_world_revision=ledger.project().world_revision,
        expected_deliberation_revision=ledger.project().deliberation_revision,
    )
    ledger.commit(
        (_event("ActionClaimed", claim, "claimed"),),
        expected_world_revision=ledger.project().world_revision,
        expected_deliberation_revision=ledger.project().deliberation_revision,
    )
    ledger.commit(
        (
            _event(
                "ActionDispatchStarted",
                {
                    "action_id": action.action_id,
                    "owner_id": "pump:dead",
                    "attempt_id": "attempt:dead",
                    "started_at": NOW.isoformat(),
                },
                "started",
            ),
        ),
        expected_world_revision=ledger.project().world_revision,
        expected_deliberation_revision=ledger.project().deliberation_revision,
    )


def _pending_execution_result(
    action: Action,
    *,
    result_id: str = "result:qq-c2c:persisted-before-crash",
    source_event_id: str = "receipt:qq-c2c:persisted-before-crash",
    status: str = "delivered",
    idempotency_key: str | None = None,
) -> ExternalObservation:
    return ExternalObservation(
        schema_version="world-v2.1",
        result_id=result_id,
        world_id=action.world_id,
        logical_time=action.logical_time,
        created_at=action.created_at,
        trace_id=action.trace_id,
        causation_id=action.action_id,
        correlation_id=action.correlation_id,
        kind="execution_receipt",
        source="qq:c2c",
        source_event_id=source_event_id,
        action_id=action.action_id,
        idempotency_key=idempotency_key or action.idempotency_key,
        status=status,
        provider_ref=f"platform:message_id:{source_event_id}",
        artifact_refs=(),
        cost_actual=0,
        observed_at=action.logical_time,
        raw_payload_hash=f"sha256:{source_event_id}",
    )


def _record_pending_execution_result(
    ledger: WorldLedger | SQLiteWorldLedger,
    result: ExternalObservation,
) -> None:
    trigger_id = f"trigger:settlement:{result.source}:{result.source_event_id}"
    projection = ledger.project()
    ledger.commit(
        SettlementPlanner(world_id=ledger.world_id).recording_events(
            result,
            trigger_id=trigger_id,
        ),
        expected_world_revision=projection.world_revision,
        expected_deliberation_revision=projection.deliberation_revision,
        commit_id=f"commit:{trigger_id}:inbox",
    )


@pytest.mark.asyncio
async def test_started_action_resumes_its_persisted_receipt_before_provider_recovery() -> None:
    ledger = _ready_ledger(
        recovery_policy="effect_once",
        action_time=datetime(2026, 7, 15, 12, 3, tzinfo=UTC),
    )
    _mark_dispatch_started(ledger)
    action = ledger.project().actions[0]
    _record_pending_execution_result(
        ledger,
        _pending_execution_result(action),
    )
    executor = _DeliveredExecutor(lookup_delivered=True)
    runtime = WorldRuntime(
        world_id=WORLD,
        ledger=ledger,
        action_executor=executor,
        action_pump_owner="pump:recovery",
    )

    recovered = await runtime.drain_actions_once()

    assert recovered is not None
    assert recovered.status == "settled"
    assert recovered.provider_status == "delivered"
    assert executor.lookup_calls == 0
    assert executor.dispatch_calls == 0
    projection = ledger.project()
    assert projection.actions[0].state == "delivered"
    assert projection.pending_external_observations == ()


@pytest.mark.asyncio
async def test_cold_restart_replays_a_persisted_receipt_without_provider_recovery(
    tmp_path,
) -> None:
    path = tmp_path / "action-pump-pending-receipt.sqlite"
    first = SQLiteWorldLedger(path=path, world_id=WORLD)
    _ready_ledger(
        recovery_policy="effect_once",
        action_time=datetime(2026, 7, 15, 12, 3, tzinfo=UTC),
        ledger=first,
    )
    _mark_dispatch_started(first)
    action = first.project().actions[0]
    result_id = "result:qq-c2c:cold-replay"
    _record_pending_execution_result(
        first,
        _pending_execution_result(
            action,
            result_id=result_id,
            source_event_id="receipt:qq-c2c:cold-replay",
        ),
    )
    first.close()

    executor = _DeliveredExecutor(lookup_delivered=True)
    reopened = SQLiteWorldLedger(path=path, world_id=WORLD)
    try:
        runtime = WorldRuntime(
            world_id=WORLD,
            ledger=reopened,
            action_executor=executor,
            action_pump_owner="pump:cold-recovery",
        )
        recovered = await runtime.drain_actions_once()
    finally:
        reopened.close()

    assert recovered is not None and recovered.provider_status == "delivered"
    assert executor.lookup_calls == 0
    assert executor.dispatch_calls == 0
    replayed = SQLiteWorldLedger(path=path, world_id=WORLD)
    try:
        projection = replayed.rebuild()
        assert projection.actions[0].state == "delivered"
        assert projection.pending_external_observations == ()
        assert [item.result_id for item in projection.execution_receipts] == [result_id]
    finally:
        replayed.close()


@pytest.mark.asyncio
async def test_concurrent_pending_receipt_recovery_is_effect_once() -> None:
    ledger = _ready_ledger(
        recovery_policy="effect_once",
        action_time=datetime(2026, 7, 15, 12, 3, tzinfo=UTC),
    )
    _mark_dispatch_started(ledger)
    action = ledger.project().actions[0]
    result_id = "result:qq-c2c:concurrent-recovery"
    _record_pending_execution_result(
        ledger,
        _pending_execution_result(
            action,
            result_id=result_id,
            source_event_id="receipt:qq-c2c:concurrent-recovery",
        ),
    )
    executor = _DeliveredExecutor(lookup_delivered=True)
    runtimes = (
        WorldRuntime(
            world_id=WORLD,
            ledger=ledger,
            action_executor=executor,
            action_pump_owner="pump:recovery:a",
        ),
        WorldRuntime(
            world_id=WORLD,
            ledger=ledger,
            action_executor=executor,
            action_pump_owner="pump:recovery:b",
        ),
    )

    recovered = await asyncio.gather(
        *(runtime.drain_actions_once() for runtime in runtimes)
    )
    duplicate = await runtimes[0].drain_actions_once()

    assert any(item is not None and item.status == "settled" for item in recovered)
    assert duplicate is not None and duplicate.status == "idle"
    assert executor.lookup_calls == 0
    assert executor.dispatch_calls == 0
    projection = ledger.project()
    assert projection.actions[0].state == "delivered"
    assert projection.pending_external_observations == ()
    assert [item.result_id for item in projection.execution_receipts] == [result_id]


@pytest.mark.asyncio
async def test_pending_receipt_with_another_idempotency_key_is_not_consumed() -> None:
    ledger = _ready_ledger(
        recovery_policy="effect_once",
        action_time=datetime(2026, 7, 15, 12, 3, tzinfo=UTC),
    )
    _mark_dispatch_started(ledger)
    action = ledger.project().actions[0]
    mismatched_result_id = "result:qq-c2c:mismatched-action-identity"
    _record_pending_execution_result(
        ledger,
        _pending_execution_result(
            action,
            result_id=mismatched_result_id,
            source_event_id="receipt:qq-c2c:mismatched-action-identity",
            idempotency_key="action-pump:reply:another",
        ),
    )
    executor = _DeliveredExecutor(lookup_delivered=True)
    runtime = WorldRuntime(
        world_id=WORLD,
        ledger=ledger,
        action_executor=executor,
        action_pump_owner="pump:recovery",
    )

    recovered = await runtime.drain_actions_once()

    assert recovered is not None and recovered.provider_status == "delivered"
    assert executor.lookup_calls == 1
    assert executor.dispatch_calls == 0
    projection = ledger.project()
    assert projection.actions[0].state == "delivered"
    assert [
        item.result_id for item in projection.pending_external_observations
    ] == [mismatched_result_id]


@pytest.mark.asyncio
async def test_non_idempotent_started_action_becomes_unknown_without_redispatch() -> None:
    ledger = _ready_ledger(recovery_policy="none", action_time=datetime(2026, 7, 15, 12, 3, tzinfo=UTC))
    _mark_dispatch_started(ledger)
    executor = _DeliveredExecutor()
    runtime = WorldRuntime(
        world_id=WORLD,
        ledger=ledger,
        action_executor=executor,
        action_pump_owner="pump:recovery",
    )
    result = await runtime.drain_actions_once()
    assert result is not None

    assert result.status == "marked_unknown"
    assert executor.dispatch_calls == 0
    assert executor.lookup_calls == 0
    assert ledger.project().actions[0].state == "unknown"


@pytest.mark.asyncio
async def test_started_idempotent_action_recovers_from_provider_lookup_without_redispatch() -> None:
    ledger = _ready_ledger(recovery_policy="result_lookup", action_time=datetime(2026, 7, 15, 12, 3, tzinfo=UTC))
    _mark_dispatch_started(ledger)
    executor = _DeliveredExecutor(lookup_delivered=True)
    runtime = WorldRuntime(
        world_id=WORLD,
        ledger=ledger,
        action_executor=executor,
        action_pump_owner="pump:recovery",
    )

    result = await runtime.drain_actions_once()

    assert result is not None and result.status == "settled"
    assert executor.lookup_calls == 1
    assert executor.dispatch_calls == 0
    assert ledger.project().actions[0].state == "delivered"


@pytest.mark.asyncio
async def test_started_qq_effect_once_action_without_provider_result_is_never_redispatched() -> None:
    body = "这条可能已经发出。"
    action = _action(
        recovery_policy="effect_once",
        logical_time=datetime(2026, 7, 15, 12, 3, tzinfo=UTC),
    ).model_copy(
        update={
            "target": "conversation:qq:c2c:owner",
            "payload_ref": "payload:qq-c2c:uncertain-old-send",
            "payload_hash": "sha256:" + hashlib.sha256(body.encode()).hexdigest(),
        }
    )
    delivery = _QQDelivery()
    ledger = _ready_ledger(authorized_action=action)
    _mark_dispatch_started(ledger)
    runtime = WorldRuntime(
        world_id=WORLD,
        ledger=ledger,
        action_executor=_qq_action_executor(
            delivery=delivery,
            body=body,
            action=action,
        ),
        action_pump_owner="pump:recovery",
    )

    recovered = await runtime.drain_actions_once()
    next_pass = await runtime.drain_actions_once()

    assert recovered is not None
    assert recovered.status == "settled"
    assert recovered.provider_status == "unknown"
    assert next_pass is not None and next_pass.status == "idle"
    assert delivery.sent == []
    assert ledger.project().actions[0].state == "unknown"


@pytest.mark.asyncio
async def test_cold_scheduler_settles_stale_qq_handoff_once_and_continues_other_work(
    tmp_path,
) -> None:
    """A cold process must not let one ambiguous QQ send starve the scheduler."""

    path = tmp_path / "stale-qq-dispatch.sqlite"
    body = "这条可能已经发出。"
    action = _action(
        recovery_policy="effect_once",
        logical_time=datetime(2026, 7, 15, 12, 3, tzinfo=UTC),
    ).model_copy(
        update={
            "target": "conversation:qq:c2c:owner",
            "payload_ref": "payload:qq-c2c:cold-uncertain-send",
            "payload_hash": "sha256:" + hashlib.sha256(body.encode()).hexdigest(),
        }
    )
    before_restart = SQLiteWorldLedger(path=path, world_id=WORLD)
    _ready_ledger(authorized_action=action, ledger=before_restart)
    _mark_dispatch_started(before_restart)
    before_restart.close()

    delivery = _QQDelivery()
    reopened = SQLiteWorldLedger(path=path, world_id=WORLD)
    runtime = WorldRuntime(
        world_id=WORLD,
        ledger=reopened,
        action_executor=_qq_action_executor(
            delivery=delivery,
            body=body,
            action=action,
        ),
        action_pump_owner="pump:cold-recovery",
    )

    class _Application:
        def __init__(self) -> None:
            self.background_calls = 0

        async def drain_actions_once(self):  # type: ignore[no-untyped-def]
            return await runtime.drain_actions_once()

        async def drain_background_once(self):  # type: ignore[no-untyped-def]
            self.background_calls += 1
            return SimpleNamespace(work_status="background:continued")

        async def drain_media_preview_once(self, **_kwargs):  # type: ignore[no-untyped-def]
            return SimpleNamespace(
                status="idle", reason_code=None, selection=None, planning=None
            )

        async def drain_media_planning_once(self):  # type: ignore[no-untyped-def]
            return SimpleNamespace(status="idle")

        async def drain_media_continuation_once(self, **_kwargs):  # type: ignore[no-untyped-def]
            return None

        async def drain_media_results_once(self, **_kwargs):  # type: ignore[no-untyped-def]
            return None

        async def drain_media_auto_delivery_once(self, **_kwargs):  # type: ignore[no-untyped-def]
            return None

        async def current_logical_time(self):  # type: ignore[no-untyped-def]
            return await runtime.current_logical_time()

    application = _Application()
    host = WorldV2PlatformHost(application=application)  # type: ignore[arg-type]
    try:
        recovered = await host.drain_scheduled_work(
            max_action_units=2,
            max_background_units=1,
            media_preview_trace_id="trace:stale-qq-recovery:first",
            media_preview_correlation_id="conversation:qq:c2c:owner",
        )
        next_pass = await host.drain_scheduled_work(
            max_action_units=2,
            max_background_units=1,
            media_preview_trace_id="trace:stale-qq-recovery:second",
            media_preview_correlation_id="conversation:qq:c2c:owner",
        )
        projection = reopened.project()
    finally:
        reopened.close()

    assert recovered.action_statuses == ("settled",)
    assert next_pass.action_statuses == ()
    assert recovered.background_statuses == ("background:continued",)
    assert next_pass.background_statuses == ("background:continued",)
    assert application.background_calls == 2
    assert delivery.sent == []
    assert projection.actions[0].state == "unknown"
    receipts = tuple(
        item for item in projection.execution_receipts if item.action_id == action.action_id
    )
    assert len(receipts) == 1
    assert receipts[0].observed_state == "unknown"
    assert receipts[0].error_class == "provider_result_unavailable"


@pytest.mark.asyncio
async def test_active_dispatch_lease_prevents_a_second_worker_from_redispatching() -> None:
    ledger = _ready_ledger(recovery_policy="effect_once")
    _mark_dispatch_started(ledger)
    executor = _DeliveredExecutor()
    runtime = WorldRuntime(
        world_id=WORLD,
        ledger=ledger,
        action_executor=executor,
        action_pump_owner="pump:another-process",
    )

    result = await runtime.drain_actions_once()

    assert result is not None and result.status == "owned_elsewhere"
    assert executor.lookup_calls == 0
    assert executor.dispatch_calls == 0


@pytest.mark.asyncio
async def test_expired_action_releases_its_budget_without_dispatch() -> None:
    ledger = _ready_ledger(expires_at=NOW)
    executor = _DeliveredExecutor()
    runtime = WorldRuntime(
        world_id=WORLD,
        ledger=ledger,
        action_executor=executor,
        action_pump_owner="pump:primary",
    )

    result = await runtime.drain_actions_once()

    assert result is not None and result.status == "expired"
    assert executor.dispatch_calls == 0
    projection = ledger.project()
    assert projection.actions[0].state == "expired"
    assert projection.budget_reservations[0].state == "released"


@pytest.mark.asyncio
async def test_dispatch_pending_is_durable_and_prevents_immediate_repeat_dispatch() -> None:
    ledger = _ready_ledger()
    executor = _PendingExecutor()
    runtime = WorldRuntime(
        world_id=WORLD,
        ledger=ledger,
        action_executor=executor,
        action_pump_owner="pump:primary",
    )

    first = await runtime.drain_actions_once()
    second = await runtime.drain_actions_once()

    assert first is not None and first.status == "pending"
    assert second is not None and second.status == "pending"
    assert executor.dispatch_calls == 1
    assert executor.lookup_calls == 0
    pending = ledger.project().actions[0].dispatch_pending
    assert pending is not None and pending.provider_ref == "provider-ref:pending:1"


@pytest.mark.asyncio
async def test_pending_deadline_converts_the_original_action_to_unknown() -> None:
    ledger = _ready_ledger()
    executor = _PendingExecutor()
    runtime = WorldRuntime(
        world_id=WORLD,
        ledger=ledger,
        action_executor=executor,
        action_pump_owner="pump:primary",
    )
    first = await runtime.drain_actions_once()
    assert first is not None and first.status == "pending"
    await runtime.advance(
        ClockObservation(
            schema_version="world-v2.1",
            tick_id="tick:pending-deadline",
            world_id=WORLD,
            logical_time=datetime(2026, 7, 15, 12, 11, tzinfo=UTC),
            created_at=datetime(2026, 7, 15, 12, 11, tzinfo=UTC),
            trace_id="trace:pending-deadline",
            causation_id="scheduler:pending-deadline",
            correlation_id="conversation:1",
            logical_time_from=NOW,
            logical_time_to=datetime(2026, 7, 15, 12, 11, tzinfo=UTC),
            reason="test pending deadline",
        )
    )

    recovered = await runtime.drain_actions_once()

    assert recovered is not None and recovered.status == "marked_unknown"
    assert executor.dispatch_calls == 1
    assert executor.lookup_calls == 0
    assert ledger.project().actions[0].state == "unknown"


class _AckThenVerifyExecutor:
    """Dispatch returns a non-terminal ack; recovery can verify delivery."""

    def __init__(self, *, verified: bool) -> None:
        self.dispatch_calls = 0
        self.verify_calls = 0
        self.verify_refs: list[str] = []
        self._verified = verified

    async def dispatch(self, action: Action) -> ProviderReceipt:
        self.dispatch_calls += 1
        return ProviderReceipt(
            provider_receipt_id="provider-event:ack:1",
            action_id=action.action_id,
            idempotency_key=action.idempotency_key,
            provider="provider:test",
            provider_ref="platform:message_id:10001",
            status="provider_accepted",
            artifact_refs=(),
            cost_actual=0,
            received_at=NOW,
            raw_payload_hash="sha256:provider-ack-1",
        )

    async def lookup_result(self, _action: Action) -> None:
        return None

    async def verify_delivery(
        self, action: Action, *, provider_ref: str
    ) -> ProviderReceipt | None:
        self.verify_calls += 1
        self.verify_refs.append(provider_ref)
        if not self._verified:
            return None
        return ProviderReceipt(
            provider_receipt_id="provider-event:verified:1",
            action_id=action.action_id,
            idempotency_key=action.idempotency_key,
            provider="provider:test",
            provider_ref=f"{provider_ref}:verified",
            status="delivered",
            artifact_refs=(),
            cost_actual=0,
            received_at=datetime(2026, 7, 15, 12, 5, tzinfo=UTC),
            raw_payload_hash="sha256:provider-verified-1",
        )


@pytest.mark.asyncio
async def test_provider_accepted_action_resumes_a_persisted_terminal_receipt_first() -> None:
    ledger = _ready_ledger()
    executor = _AckThenVerifyExecutor(verified=False)
    first_runtime = WorldRuntime(
        world_id=WORLD,
        ledger=ledger,
        action_executor=executor,
        action_pump_owner="pump:primary",
    )
    accepted = await first_runtime.drain_actions_once()
    assert accepted is not None and accepted.provider_status == "provider_accepted"
    action = ledger.project().actions[0]
    _record_pending_execution_result(
        ledger,
        _pending_execution_result(
            action,
            result_id="result:qq-c2c:verified-before-crash",
            source_event_id="receipt:qq-c2c:verified-before-crash",
            status="delivered",
        ),
    )
    restarted = WorldRuntime(
        world_id=WORLD,
        ledger=ledger,
        action_executor=executor,
        action_pump_owner="pump:recovery",
    )

    recovered = await restarted.drain_actions_once()

    assert recovered is not None
    assert recovered.status == "settled"
    assert recovered.provider_status == "delivered"
    assert executor.verify_calls == 0
    projection = ledger.project()
    assert projection.actions[0].state == "delivered"
    assert projection.pending_external_observations == ()


async def _accepted_then_lease_elapsed(
    executor: _AckThenVerifyExecutor,
) -> tuple[WorldLedger, WorldRuntime]:
    ledger = _ready_ledger()
    runtime = WorldRuntime(
        world_id=WORLD,
        ledger=ledger,
        action_executor=executor,
        action_pump_owner="pump:primary",
    )
    first = await runtime.drain_actions_once()
    assert first is not None and first.status == "settled"
    assert ledger.project().actions[0].state == "provider_accepted"
    await runtime.advance(
        ClockObservation(
            schema_version="world-v2.1",
            tick_id="tick:ack-lease-elapsed",
            world_id=WORLD,
            logical_time=datetime(2026, 7, 15, 12, 5, tzinfo=UTC),
            created_at=datetime(2026, 7, 15, 12, 5, tzinfo=UTC),
            trace_id="trace:ack-lease-elapsed",
            causation_id="scheduler:ack-lease-elapsed",
            correlation_id="conversation:1",
            logical_time_from=NOW,
            logical_time_to=datetime(2026, 7, 15, 12, 5, tzinfo=UTC),
            reason="test ack recovery",
        )
    )
    return ledger, runtime


@pytest.mark.asyncio
async def test_provider_accepted_recovery_upgrades_to_delivered_with_verification() -> None:
    executor = _AckThenVerifyExecutor(verified=True)
    ledger, runtime = await _accepted_then_lease_elapsed(executor)

    recovered = await runtime.drain_actions_once()

    assert recovered is not None and recovered.status == "settled"
    assert executor.dispatch_calls == 1
    assert executor.verify_calls == 1
    assert executor.verify_refs == ["platform:message_id:10001"]
    projection = ledger.project()
    assert projection.actions[0].state == "delivered"
    assert projection.budget_reservations[0].state == "settled"


@pytest.mark.asyncio
async def test_provider_accepted_recovery_without_evidence_still_becomes_unknown() -> None:
    executor = _AckThenVerifyExecutor(verified=False)
    ledger, runtime = await _accepted_then_lease_elapsed(executor)

    recovered = await runtime.drain_actions_once()

    assert recovered is not None and recovered.status == "marked_unknown"
    assert executor.verify_calls == 1
    assert ledger.project().actions[0].state == "unknown"


@pytest.mark.asyncio
async def test_pending_lookup_rejects_a_receipt_from_another_provider_reference() -> None:
    ledger = _ready_ledger()
    executor = _MismatchedPendingLookupExecutor()
    runtime = WorldRuntime(
        world_id=WORLD,
        ledger=ledger,
        action_executor=executor,
        action_pump_owner="pump:primary",
    )
    first = await runtime.drain_actions_once()
    assert first is not None and first.status == "pending"
    await runtime.advance(
        ClockObservation(
            schema_version="world-v2.1",
            tick_id="tick:pending-lookup",
            world_id=WORLD,
            logical_time=datetime(2026, 7, 15, 12, 2, tzinfo=UTC),
            created_at=datetime(2026, 7, 15, 12, 2, tzinfo=UTC),
            trace_id="trace:pending-lookup",
            causation_id="scheduler:pending-lookup",
            correlation_id="conversation:1",
            logical_time_from=NOW,
            logical_time_to=datetime(2026, 7, 15, 12, 2, tzinfo=UTC),
            reason="test pending lookup",
        )
    )

    with pytest.raises(ValueError, match="pending provider reference"):
        await runtime.drain_actions_once()
    assert ledger.project().actions[0].state == "dispatch_started"

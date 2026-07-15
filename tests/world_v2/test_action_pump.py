from __future__ import annotations

from datetime import UTC, datetime

import pytest

from companion_daemon.world_v2.ledger import WorldLedger
from companion_daemon.world_v2.runtime import WorldRuntime
from companion_daemon.world_v2.schemas import (
    Action,
    BudgetAccount,
    BudgetReservation,
    ClockObservation,
    DispatchPending,
    ProviderReceipt,
    WorldEvent,
)


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
) -> WorldLedger:
    ledger = WorldLedger.in_memory(world_id=WORLD)
    account = BudgetAccount(account_id="account:chat", category="chat", window_id="test", limit=100)
    reservation = BudgetReservation(
        reservation_id="reservation:reply:1",
        account_id=account.account_id,
        action_id="action:reply:1",
        category="chat",
        amount_limit=10,
    )
    ledger.commit(
        (
            _event("BudgetAccountConfigured", {"account": account.model_dump(mode="json")}, "account"),
            _event("BudgetReserved", {"reservation": reservation.model_dump(mode="json")}, "reserved"),
            _event("ActionAuthorized", {"action": _action(recovery_policy=recovery_policy, logical_time=action_time, expires_at=expires_at).model_dump(mode="json")}, "authorized"),
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


def _mark_dispatch_started(ledger: WorldLedger) -> None:
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

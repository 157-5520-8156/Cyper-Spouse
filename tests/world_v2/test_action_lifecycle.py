from __future__ import annotations

from datetime import UTC, datetime

import pytest

from companion_daemon.world_v2 import (
    Action,
    BudgetAccount,
    BudgetReservation,
    ExternalObservation,
    WorldRuntime,
)
from companion_daemon.world_v2.errors import IdempotencyConflict, InvalidActionTransition
from companion_daemon.world_v2.ledger import WorldLedger
from companion_daemon.world_v2.schemas import WorldEvent
from companion_daemon.world_v2.sqlite_ledger import SQLiteWorldLedger


NOW = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)
WORLD_ID = "world-v2-action-test"


def action(*, state: str = "authorized") -> Action:
    return Action.model_validate(
        {
            "schema_version": "world-v2.1",
            "action_id": "action-reply-1",
            "world_id": WORLD_ID,
            "logical_time": NOW,
            "created_at": NOW,
            "trace_id": "trace-action-1",
            "causation_id": "acceptance-1",
            "correlation_id": "conversation-1",
            "kind": "reply",
            "layer": "external_action",
            "intent_ref": "intent-reply-1",
            "actor": "companion:girl",
            "target": "user:geoff",
            "payload_ref": "payload:reply-1",
            "payload_hash": "sha256:reply-1",
            "idempotency_key": "world-v2-action-test:intent-reply-1:reply",
            "budget_reservation_id": "budget-reply-1",
            "state": state,
            "recovery_policy": "effect_once",
        }
    )


def budget_reservation() -> BudgetReservation:
    return BudgetReservation(
        reservation_id="budget-reply-1",
        account_id="budget-account-chat",
        action_id="action-reply-1",
        category="chat",
        amount_limit=10_000,
    )


def budget_account() -> BudgetAccount:
    return BudgetAccount(
        account_id="budget-account-chat",
        category="chat",
        window_id="test-window",
        limit=1_000_000,
    )


def reserve_and_authorize(ledger: WorldLedger | SQLiteWorldLedger, *, prefix: str) -> None:
    before = ledger.project()
    ledger.commit(
        [
            action_event(
                event_id=f"{prefix}-budget-account",
                event_type="BudgetAccountConfigured",
                payload={"account": budget_account().model_dump(mode="json")},
            ),
            action_event(
                event_id=f"{prefix}-budget",
                event_type="BudgetReserved",
                payload={"reservation": budget_reservation().model_dump(mode="json")},
            ),
            action_event(
                event_id=f"{prefix}-action",
                event_type="ActionAuthorized",
                payload={"action": action().model_dump(mode="json")},
            ),
        ],
        expected_world_revision=before.world_revision,
        expected_deliberation_revision=before.deliberation_revision,
    )


def action_event(
    *, event_id: str, event_type: str, payload: dict[str, object]
) -> WorldEvent:
    return WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id=event_id,
        world_id=WORLD_ID,
        event_type=event_type,
        logical_time=NOW,
        created_at=NOW,
        actor="system:test",
        source="test",
        trace_id="trace-action-1",
        causation_id="acceptance-1",
        correlation_id="conversation-1",
        idempotency_key=event_id,
        payload=payload,
    )


def external_result(
    *,
    result_id: str,
    source_event_id: str,
    status: str,
    action_id: str = "action-reply-1",
    provider_ref: str | None = None,
    raw_payload_hash: str | None = None,
    cost_actual: int = 0,
) -> ExternalObservation:
    return ExternalObservation.model_validate(
        {
            "schema_version": "world-v2.1",
            "result_id": result_id,
            "world_id": WORLD_ID,
            "logical_time": NOW,
            "created_at": NOW,
            "trace_id": "trace-result-1",
            "causation_id": f"provider:{source_event_id}",
            "correlation_id": "conversation-1",
            "kind": "execution_receipt",
            "source": "test-provider",
            "source_event_id": source_event_id,
            "action_id": action_id,
            "idempotency_key": "world-v2-action-test:intent-reply-1:reply",
            "status": status,
            "provider_ref": provider_ref or source_event_id,
            "artifact_refs": (),
            "cost_actual": cost_actual,
            "observed_at": NOW,
            "raw_payload_hash": raw_payload_hash or f"sha256:{source_event_id}",
        }
    )


def dispatch_started_ledger() -> WorldLedger:
    ledger = WorldLedger.in_memory(world_id=WORLD_ID)
    reserve_and_authorize(ledger, prefix="event-action-ready")
    event_types = (
        ("ActionScheduled", {"action_id": "action-reply-1"}),
        ("ActionClaimed", {"action_id": "action-reply-1"}),
        ("ActionDispatchStarted", {"action_id": "action-reply-1"}),
    )
    for index, (event_type, payload) in enumerate(event_types):
        before = ledger.project()
        ledger.commit(
            [
                action_event(
                    event_id=f"event-action-ready-{index}",
                    event_type=event_type,
                    payload=payload,
                )
            ],
            expected_world_revision=before.world_revision,
            expected_deliberation_revision=before.deliberation_revision,
        )
    return ledger


def test_authorized_action_is_registered_in_the_authoritative_projection() -> None:
    ledger = WorldLedger.in_memory(world_id=WORLD_ID)
    authorized = action()
    reserve_and_authorize(ledger, prefix="event-action-authorized-1")

    assert ledger.project().actions == (authorized,)


def test_action_cannot_be_authorized_without_its_budget_reservation() -> None:
    ledger = WorldLedger.in_memory(world_id=WORLD_ID)

    with pytest.raises(ValueError, match="budget reservation"):
        ledger.commit(
            [action_event(
                event_id="event-action-without-budget",
                event_type="ActionAuthorized",
                payload={"action": action().model_dump(mode="json")},
            )],
            expected_world_revision=0,
            expected_deliberation_revision=0,
        )


def test_budget_window_rejects_reservations_beyond_available_capacity() -> None:
    ledger = WorldLedger.in_memory(world_id=WORLD_ID)
    account = budget_account().model_copy(update={"limit": 5})
    reservation = budget_reservation().model_copy(update={"amount_limit": 6})

    with pytest.raises(ValueError, match="insufficient available capacity"):
        ledger.commit(
            [
                action_event(
                    event_id="event-small-budget-account",
                    event_type="BudgetAccountConfigured",
                    payload={"account": account.model_dump(mode="json")},
                ),
                action_event(
                    event_id="event-too-large-reservation",
                    event_type="BudgetReserved",
                    payload={"reservation": reservation.model_dump(mode="json")},
                ),
            ],
            expected_world_revision=0,
            expected_deliberation_revision=0,
        )
    assert ledger.project().world_revision == 0


def test_two_actions_cannot_share_one_external_effect_identity() -> None:
    ledger = WorldLedger.in_memory(world_id=WORLD_ID)
    authorized = action()
    reserve_and_authorize(ledger, prefix="event-action-authorized-1")
    duplicate_effect = authorized.model_copy(update={"action_id": "action-reply-2"})

    with pytest.raises(ValueError, match="idempotency_key"):
        ledger.commit(
            [action_event(
                event_id="event-action-authorized-2",
                event_type="ActionAuthorized",
                payload={"action": duplicate_effect.model_dump(mode="json")},
            )],
            expected_world_revision=3,
            expected_deliberation_revision=0,
        )
    assert ledger.project().actions == (authorized,)


def test_action_follows_the_frozen_pre_dispatch_lifecycle() -> None:
    ledger = WorldLedger.in_memory(world_id=WORLD_ID)
    reserve_and_authorize(ledger, prefix="event-action-lifecycle")
    event_types = (
        ("ActionScheduled", {"action_id": "action-reply-1"}),
        ("ActionClaimed", {"action_id": "action-reply-1"}),
        ("ActionDispatchStarted", {"action_id": "action-reply-1"}),
    )

    for index, (event_type, payload) in enumerate(event_types):
        before = ledger.project()
        ledger.commit(
            [
                action_event(
                    event_id=f"event-action-lifecycle-{index}",
                    event_type=event_type,
                    payload=payload,
                )
            ],
            expected_world_revision=before.world_revision,
            expected_deliberation_revision=before.deliberation_revision,
        )

    assert ledger.project().actions[0].state == "dispatch_started"


def test_illegal_action_transition_is_rejected_without_advancing_the_world() -> None:
    ledger = WorldLedger.in_memory(world_id=WORLD_ID)
    reserve_and_authorize(ledger, prefix="event-action-authorized-invalid-transition")

    with pytest.raises(InvalidActionTransition):
        ledger.commit(
            [
                action_event(
                    event_id="event-action-delivered-too-early",
                    event_type="ActionDelivered",
                    payload={"action_id": "action-reply-1"},
                )
            ],
            expected_world_revision=3,
            expected_deliberation_revision=0,
        )

    projection = ledger.project()
    assert projection.world_revision == 3
    assert projection.actions[0].state == "authorized"


@pytest.mark.asyncio
async def test_provider_acceptance_settlement_is_effect_once() -> None:
    ledger = dispatch_started_ledger()
    runtime = WorldRuntime(world_id=WORLD_ID, ledger=ledger)
    receipt = external_result(
        result_id="result-provider-accepted-1",
        source_event_id="receipt-provider-accepted-1",
        status="provider_accepted",
    )

    first = await runtime.settle(receipt)
    duplicate = await runtime.settle(receipt)

    assert duplicate == first
    assert first.status == "action_executed"
    projection = ledger.project()
    assert projection.actions[0].state == "provider_accepted"
    assert projection.pending_external_observations == ()
    assert len(projection.execution_receipts) == 1
    assert projection.execution_receipts[0].observed_state == "provider_accepted"
    assert projection.budget_settlements == ()


@pytest.mark.asyncio
async def test_delivered_is_effect_once_and_cannot_be_overwritten() -> None:
    ledger = dispatch_started_ledger()
    runtime = WorldRuntime(world_id=WORLD_ID, ledger=ledger)
    await runtime.settle(
        external_result(
            result_id="result-provider-accepted-before-delivery",
            source_event_id="receipt-provider-accepted-before-delivery",
            status="provider_accepted",
        )
    )
    delivered = external_result(
        result_id="result-delivered-1",
        source_event_id="receipt-delivered-1",
        status="delivered",
    )

    first = await runtime.settle(delivered)
    duplicate = await runtime.settle(delivered)
    assert duplicate == first
    assert ledger.project().actions[0].state == "delivered"

    conflict = await runtime.settle(
        external_result(
            result_id="result-failed-after-delivery",
            source_event_id="receipt-failed-after-delivery",
            status="failed",
        )
    )
    assert conflict.status == "deferred"
    projection = ledger.project()
    assert projection.actions[0].state == "delivered"
    assert projection.reconciliations[-1].reason == "terminal_conflict"
    assert projection.execution_receipts[-1].result_id == "result-failed-after-delivery"
    assert projection.pending_external_observations == ()


@pytest.mark.asyncio
async def test_unknown_is_terminal_and_a_later_delivery_cannot_reopen_the_action() -> None:
    ledger = dispatch_started_ledger()
    runtime = WorldRuntime(world_id=WORLD_ID, ledger=ledger)

    await runtime.settle(
        external_result(
            result_id="result-unknown-1",
            source_event_id="receipt-unknown-1",
            status="unknown",
        )
    )
    assert ledger.project().actions[0].state == "unknown"

    conflict = await runtime.settle(
        external_result(
            result_id="result-delivered-after-unknown",
            source_event_id="receipt-delivered-after-unknown",
            status="delivered",
            cost_actual=7,
        )
    )
    assert conflict.status == "deferred"
    assert ledger.project().actions[0].state == "unknown"
    assert ledger.project().reconciliations[-1].reason == "terminal_conflict"
    assert ledger.project().budget_settlements[-1].settlement_kind == (
        "reconciliation_adjustment"
    )
    assert [item.cost_delta for item in ledger.project().budget_settlements] == [0, 7]
    assert ledger.project().budget_accounts[-1].spent == 7


@pytest.mark.asyncio
async def test_terminal_provider_failure_can_settle_from_dispatch_started() -> None:
    ledger = dispatch_started_ledger()
    runtime = WorldRuntime(world_id=WORLD_ID, ledger=ledger)

    await runtime.settle(
        external_result(
            result_id="result-immediate-failure",
            source_event_id="receipt-immediate-failure",
            status="failed",
        )
    )

    assert ledger.project().actions[0].state == "failed"
    assert ledger.project().budget_settlements[-1].state == "settled"


@pytest.mark.asyncio
async def test_real_external_cost_above_reservation_is_still_recorded() -> None:
    ledger = dispatch_started_ledger()
    runtime = WorldRuntime(world_id=WORLD_ID, ledger=ledger)

    await runtime.settle(
        external_result(
            result_id="result-cost-overrun",
            source_event_id="receipt-cost-overrun",
            status="delivered",
            cost_actual=12_000,
        )
    )

    projection = ledger.project()
    assert projection.budget_reservations[-1].settled_cost == 12_000
    assert projection.budget_accounts[-1].spent == 12_000


@pytest.mark.asyncio
async def test_unknown_action_receipt_is_recorded_for_reconciliation() -> None:
    ledger = dispatch_started_ledger()
    runtime = WorldRuntime(world_id=WORLD_ID, ledger=ledger)

    outcome = await runtime.settle(
        external_result(
            result_id="result-unknown-action",
            source_event_id="receipt-unknown-action",
            status="delivered",
            action_id="action-that-does-not-exist",
        )
    )

    assert outcome.status == "deferred"
    projection = ledger.project()
    assert projection.pending_external_observations == ()
    assert projection.reconciliations[-1].reason == "unknown_action"


@pytest.mark.asyncio
async def test_terminal_settlement_atomically_records_receipt_budget_and_completion() -> None:
    ledger = dispatch_started_ledger()
    runtime = WorldRuntime(world_id=WORLD_ID, ledger=ledger)
    result = external_result(
        result_id="result-delivered-atomic",
        source_event_id="receipt-delivered-atomic",
        status="delivered",
    )

    outcome = await runtime.settle(result)
    projection = ledger.project()

    assert outcome.status == "action_executed"
    assert projection.actions[0].state == "delivered"
    assert projection.execution_receipts[-1].result_id == result.result_id
    assert projection.budget_settlements[-1].reservation_id == "budget-reply-1"
    assert projection.budget_reservations[-1].state == "settled"
    assert projection.completed_trigger_ids[-1] == outcome.trigger_id
    assert projection.trigger_processes[-1].state == "terminal"
    assert projection.trigger_processes[-1].runtime_outcome_ref == outcome.outcome_id
    assert projection.pending_external_observations == ()


@pytest.mark.asyncio
async def test_atomic_settlement_survives_sqlite_restart(tmp_path) -> None:
    path = tmp_path / "world-v2-settlement.sqlite3"
    ledger = SQLiteWorldLedger(path=path, world_id=WORLD_ID)
    reserve_and_authorize(ledger, prefix="event-sqlite-action")
    event_types = (
        ("ActionScheduled", {"action_id": "action-reply-1"}),
        ("ActionClaimed", {"action_id": "action-reply-1"}),
        ("ActionDispatchStarted", {"action_id": "action-reply-1"}),
    )
    for index, (event_type, payload) in enumerate(event_types):
        before = ledger.project()
        ledger.commit(
            [action_event(
                event_id=f"event-sqlite-action-{index}",
                event_type=event_type,
                payload=payload,
            )],
            expected_world_revision=before.world_revision,
            expected_deliberation_revision=before.deliberation_revision,
        )
    runtime = WorldRuntime(world_id=WORLD_ID, ledger=ledger)
    result = external_result(
        result_id="result-sqlite-delivered",
        source_event_id="receipt-sqlite-delivered",
        status="delivered",
    )

    first = await runtime.settle(result)
    ledger.close()
    reopened = SQLiteWorldLedger(path=path, world_id=WORLD_ID)
    retried = await WorldRuntime(world_id=WORLD_ID, ledger=reopened).settle(result)

    assert retried == first
    assert reopened.rebuild() == reopened.project()
    assert reopened.project().actions[0].state == "delivered"
    assert reopened.project().budget_settlements[-1].result_id == result.result_id
    reopened.close()


@pytest.mark.asyncio
async def test_provider_event_identity_cannot_be_reused_with_different_content() -> None:
    ledger = dispatch_started_ledger()
    runtime = WorldRuntime(world_id=WORLD_ID, ledger=ledger)
    accepted = external_result(
        result_id="result-provider-identity",
        source_event_id="provider-stable-event-1",
        status="provider_accepted",
    )
    await runtime.settle(accepted)
    conflicting = accepted.model_copy(
        update={
            "result_id": "result-provider-identity-conflict",
            "status": "delivered",
            "raw_payload_hash": "sha256:different-provider-payload",
        }
    )

    with pytest.raises(IdempotencyConflict):
        await runtime.settle(conflicting)

    projection = ledger.project()
    assert projection.actions[0].state == "provider_accepted"
    assert len(projection.execution_receipts) == 1


@pytest.mark.asyncio
async def test_provider_ref_reuse_under_a_new_source_event_enters_reconciliation() -> None:
    ledger = dispatch_started_ledger()
    runtime = WorldRuntime(world_id=WORLD_ID, ledger=ledger)
    await runtime.settle(
        external_result(
            result_id="result-provider-ref-first",
            source_event_id="provider-event-first",
            provider_ref="provider-ref-reused",
            raw_payload_hash="sha256:first",
            status="provider_accepted",
        )
    )

    outcome = await runtime.settle(
        external_result(
            result_id="result-provider-ref-second",
            source_event_id="provider-event-second",
            provider_ref="provider-ref-reused",
            raw_payload_hash="sha256:second",
            status="delivered",
        )
    )

    assert outcome.status == "deferred"
    assert ledger.project().actions[0].state == "provider_accepted"
    assert ledger.project().reconciliations[-1].reason == "identity_mismatch"


@pytest.mark.asyncio
async def test_result_id_reuse_under_a_different_provider_event_is_reconciled() -> None:
    ledger = dispatch_started_ledger()
    runtime = WorldRuntime(world_id=WORLD_ID, ledger=ledger)
    await runtime.settle(
        external_result(
            result_id="reused-result-id",
            source_event_id="provider-event-original",
            status="provider_accepted",
        )
    )

    outcome = await runtime.settle(
        external_result(
            result_id="reused-result-id",
            source_event_id="provider-event-conflict",
            status="delivered",
        )
    )

    assert outcome.status == "deferred"
    assert ledger.project().pending_external_observations == ()
    assert ledger.project().reconciliations[-1].reason == "identity_mismatch"


@pytest.mark.asyncio
async def test_terminal_result_id_collision_does_not_leave_a_pending_inbox() -> None:
    ledger = dispatch_started_ledger()
    runtime = WorldRuntime(world_id=WORLD_ID, ledger=ledger)
    await runtime.settle(
        external_result(
            result_id="terminal-reused-result-id",
            source_event_id="terminal-provider-event-original",
            status="delivered",
            cost_actual=3,
        )
    )

    outcome = await runtime.settle(
        external_result(
            result_id="terminal-reused-result-id",
            source_event_id="terminal-provider-event-conflict",
            status="failed",
            cost_actual=5,
        )
    )

    assert outcome.status == "deferred"
    projection = ledger.project()
    assert projection.pending_external_observations == ()
    assert projection.reconciliations[-1].reason == "identity_mismatch"
    assert projection.budget_settlements[-1].cost_delta == 2


@pytest.mark.asyncio
async def test_same_provider_receipt_triple_under_new_source_event_has_no_second_effect() -> None:
    ledger = dispatch_started_ledger()
    runtime = WorldRuntime(world_id=WORLD_ID, ledger=ledger)
    first = external_result(
        result_id="result-provider-original",
        source_event_id="adapter-event-original",
        provider_ref="provider-stable-ref",
        raw_payload_hash="sha256:stable-provider-payload",
        status="delivered",
        cost_actual=11,
    )
    await runtime.settle(first)
    before = ledger.project()

    duplicate = external_result(
        result_id="result-provider-duplicate",
        source_event_id="adapter-event-duplicate",
        provider_ref="provider-stable-ref",
        raw_payload_hash="sha256:stable-provider-payload",
        status="failed",
        cost_actual=11,
    )
    outcome = await runtime.settle(duplicate)
    after = ledger.project()

    assert outcome.status == "observed_only"
    assert after.actions[0].state == "delivered"
    assert len(after.execution_receipts) == len(before.execution_receipts)
    assert len(after.budget_settlements) == len(before.budget_settlements)
    assert after.pending_external_observations == ()

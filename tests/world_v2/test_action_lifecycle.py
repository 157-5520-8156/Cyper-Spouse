from __future__ import annotations

from datetime import UTC, datetime, timedelta

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
CLAIMED_AT = NOW
CLAIM_EXPIRES_AT = NOW + timedelta(minutes=1)


def claim_payload(
    *,
    owner_id: str = "action-pump:test",
    attempt_id: str = "attempt-action-1",
    acquired_at: datetime = CLAIMED_AT,
    expires_at: datetime = CLAIM_EXPIRES_AT,
) -> dict[str, object]:
    return {
        "action_id": "action-reply-1",
        "claim_lease": {
            "owner_id": owner_id,
            "attempt_id": attempt_id,
            "acquired_at": acquired_at.isoformat(),
            "expires_at": expires_at.isoformat(),
        },
    }


def dispatch_payload(
    *,
    owner_id: str = "action-pump:test",
    attempt_id: str = "attempt-action-1",
    started_at: datetime = CLAIMED_AT,
) -> dict[str, object]:
    return {
        "action_id": "action-reply-1",
        "owner_id": owner_id,
        "attempt_id": attempt_id,
        "started_at": started_at.isoformat(),
    }


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
    *,
    event_id: str,
    event_type: str,
    payload: dict[str, object],
    logical_time: datetime = NOW,
    created_at: datetime = NOW,
) -> WorldEvent:
    return WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id=event_id,
        world_id=WORLD_ID,
        event_type=event_type,
        logical_time=logical_time,
        created_at=created_at,
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
        ("ActionClaimed", claim_payload()),
        ("ActionDispatchStarted", dispatch_payload()),
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
        ("ActionClaimed", claim_payload()),
        ("ActionDispatchStarted", dispatch_payload()),
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


def test_action_claim_requires_a_finite_lease_atomically() -> None:
    ledger = WorldLedger.in_memory(world_id=WORLD_ID)
    reserve_and_authorize(ledger, prefix="event-action-claim-no-lease")
    before = ledger.project()
    ledger.commit(
        [action_event(
            event_id="event-action-scheduled-no-lease",
            event_type="ActionScheduled",
            payload={"action_id": "action-reply-1"},
        )],
        expected_world_revision=before.world_revision,
        expected_deliberation_revision=before.deliberation_revision,
    )
    before_claim = ledger.project()

    with pytest.raises(ValueError, match="claim_lease"):
        ledger.commit(
            [action_event(
                event_id="event-action-claim-missing-lease",
                event_type="ActionClaimed",
                payload={"action_id": "action-reply-1"},
            )],
            expected_world_revision=before_claim.world_revision,
            expected_deliberation_revision=before_claim.deliberation_revision,
        )

    assert ledger.project() == before_claim


def test_action_claim_acquisition_must_match_its_frozen_event_time() -> None:
    ledger = WorldLedger.in_memory(world_id=WORLD_ID)
    reserve_and_authorize(ledger, prefix="event-action-claim-time")
    before = ledger.project()
    ledger.commit(
        [action_event(
            event_id="event-action-claim-time-scheduled",
            event_type="ActionScheduled",
            payload={"action_id": "action-reply-1"},
        )],
        expected_world_revision=before.world_revision,
        expected_deliberation_revision=before.deliberation_revision,
    )
    scheduled = ledger.project()

    with pytest.raises(ValueError, match="event created_at"):
        ledger.commit(
            [action_event(
                event_id="event-action-claim-time-future",
                event_type="ActionClaimed",
                payload=claim_payload(acquired_at=NOW + timedelta(seconds=1)),
            )],
            expected_world_revision=scheduled.world_revision,
            expected_deliberation_revision=scheduled.deliberation_revision,
        )

    assert ledger.project() == scheduled


def test_only_the_active_claim_owner_can_begin_dispatch() -> None:
    ledger = WorldLedger.in_memory(world_id=WORLD_ID)
    reserve_and_authorize(ledger, prefix="event-action-owner")
    for event_id, event_type, payload in (
        ("event-action-owner-scheduled", "ActionScheduled", {"action_id": "action-reply-1"}),
        ("event-action-owner-claimed", "ActionClaimed", claim_payload()),
    ):
        before = ledger.project()
        ledger.commit(
            [action_event(event_id=event_id, event_type=event_type, payload=payload)],
            expected_world_revision=before.world_revision,
            expected_deliberation_revision=before.deliberation_revision,
        )
    claimed = ledger.project()

    with pytest.raises(ValueError, match="active claim lease"):
        ledger.commit(
            [action_event(
                event_id="event-action-owner-wrong-dispatcher",
                event_type="ActionDispatchStarted",
                payload=dispatch_payload(owner_id="action-pump:other"),
                logical_time=CLAIMED_AT,
            )],
            expected_world_revision=claimed.world_revision,
            expected_deliberation_revision=claimed.deliberation_revision,
        )

    assert ledger.project() == claimed


def test_expired_action_claim_can_be_reclaimed_before_dispatch() -> None:
    ledger = WorldLedger.in_memory(world_id=WORLD_ID)
    reserve_and_authorize(ledger, prefix="event-action-reclaim")
    for event_id, event_type, payload in (
        ("event-action-reclaim-scheduled", "ActionScheduled", {"action_id": "action-reply-1"}),
        ("event-action-reclaim-first", "ActionClaimed", claim_payload()),
    ):
        before = ledger.project()
        ledger.commit(
            [action_event(event_id=event_id, event_type=event_type, payload=payload)],
            expected_world_revision=before.world_revision,
            expected_deliberation_revision=before.deliberation_revision,
        )
    reclaimed_at = CLAIM_EXPIRES_AT
    reclaimed_payload = claim_payload(
        owner_id="action-pump:recovery",
        attempt_id="attempt-action-2",
        acquired_at=reclaimed_at,
        expires_at=reclaimed_at + timedelta(minutes=1),
    )
    before = ledger.project()
    ledger.commit(
        [action_event(
            event_id="event-action-reclaimed",
            event_type="ActionReclaimed",
            payload=reclaimed_payload,
            logical_time=reclaimed_at,
            created_at=reclaimed_at,
        )],
        expected_world_revision=before.world_revision,
        expected_deliberation_revision=before.deliberation_revision,
    )
    reclaimed = ledger.project().actions[0]

    assert reclaimed.state == "claimed"
    assert reclaimed.claim_lease is not None
    assert reclaimed.claim_lease.owner_id == "action-pump:recovery"
    assert reclaimed.claim_lease.attempt_id == "attempt-action-2"


def test_action_claim_cannot_be_stolen_before_lease_expiry() -> None:
    ledger = WorldLedger.in_memory(world_id=WORLD_ID)
    reserve_and_authorize(ledger, prefix="event-action-premature-reclaim")
    for event_id, event_type, payload in (
        ("event-action-premature-scheduled", "ActionScheduled", {"action_id": "action-reply-1"}),
        ("event-action-premature-first", "ActionClaimed", claim_payload()),
    ):
        before = ledger.project()
        ledger.commit(
            [action_event(event_id=event_id, event_type=event_type, payload=payload)],
            expected_world_revision=before.world_revision,
            expected_deliberation_revision=before.deliberation_revision,
        )
    claimed = ledger.project()

    with pytest.raises(ValueError, match="has not expired"):
        ledger.commit(
            [action_event(
                event_id="event-action-premature-reclaimed",
                event_type="ActionReclaimed",
                payload=claim_payload(
                    owner_id="action-pump:other",
                    attempt_id="attempt-action-2",
                    acquired_at=CLAIMED_AT + timedelta(seconds=1),
                    expires_at=CLAIM_EXPIRES_AT + timedelta(minutes=1),
                ),
                logical_time=CLAIMED_AT + timedelta(seconds=1),
                created_at=CLAIMED_AT + timedelta(seconds=1),
            )],
            expected_world_revision=claimed.world_revision,
            expected_deliberation_revision=claimed.deliberation_revision,
        )

    assert ledger.project() == claimed


def test_dispatch_cannot_start_after_the_claim_lease_expires() -> None:
    ledger = WorldLedger.in_memory(world_id=WORLD_ID)
    reserve_and_authorize(ledger, prefix="event-action-expired-dispatch")
    for event_id, event_type, payload in (
        ("event-action-expired-scheduled", "ActionScheduled", {"action_id": "action-reply-1"}),
        ("event-action-expired-claimed", "ActionClaimed", claim_payload()),
    ):
        before = ledger.project()
        ledger.commit(
            [action_event(event_id=event_id, event_type=event_type, payload=payload)],
            expected_world_revision=before.world_revision,
            expected_deliberation_revision=before.deliberation_revision,
        )
    claimed = ledger.project()

    with pytest.raises(ValueError, match="expired"):
        ledger.commit(
            [action_event(
                event_id="event-action-expired-dispatch-start",
                event_type="ActionDispatchStarted",
                payload=dispatch_payload(started_at=CLAIM_EXPIRES_AT),
                created_at=CLAIM_EXPIRES_AT,
            )],
            expected_world_revision=claimed.world_revision,
            expected_deliberation_revision=claimed.deliberation_revision,
        )

    assert ledger.project() == claimed


def test_reclaimed_action_lease_survives_sqlite_restart(tmp_path) -> None:
    path = tmp_path / "world-v2-action-claim.sqlite3"
    ledger = SQLiteWorldLedger(path=path, world_id=WORLD_ID)
    reserve_and_authorize(ledger, prefix="event-action-restart-reclaim")
    events = (
        action_event(
            event_id="event-action-restart-scheduled",
            event_type="ActionScheduled",
            payload={"action_id": "action-reply-1"},
        ),
        action_event(
            event_id="event-action-restart-claimed",
            event_type="ActionClaimed",
            payload=claim_payload(),
        ),
        action_event(
            event_id="event-action-restart-reclaimed",
            event_type="ActionReclaimed",
            payload=claim_payload(
                owner_id="action-pump:restarted",
                attempt_id="attempt-action-restarted",
                acquired_at=CLAIM_EXPIRES_AT,
                expires_at=CLAIM_EXPIRES_AT + timedelta(minutes=1),
            ),
            logical_time=CLAIM_EXPIRES_AT,
            created_at=CLAIM_EXPIRES_AT,
        ),
    )
    for event in events:
        before = ledger.project()
        ledger.commit(
            [event],
            expected_world_revision=before.world_revision,
            expected_deliberation_revision=before.deliberation_revision,
        )
    expected = ledger.project()
    ledger.close()

    reopened = SQLiteWorldLedger(path=path, world_id=WORLD_ID)
    recovered = reopened.project().actions[0]

    assert reopened.rebuild() == expected
    assert recovered.claim_lease is not None
    assert recovered.claim_lease.owner_id == "action-pump:restarted"
    assert recovered.claim_lease.attempt_id == "attempt-action-restarted"
    reopened.close()


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
        ("ActionClaimed", claim_payload()),
        ("ActionDispatchStarted", dispatch_payload()),
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

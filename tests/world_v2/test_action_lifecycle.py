from __future__ import annotations

from datetime import UTC, datetime

import pytest

from companion_daemon.world_v2 import Action, ExternalObservation, WorldRuntime
from companion_daemon.world_v2.errors import InvalidActionTransition
from companion_daemon.world_v2.ledger import WorldLedger
from companion_daemon.world_v2.schemas import WorldEvent


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
    *, result_id: str, source_event_id: str, status: str
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
            "action_id": "action-reply-1",
            "idempotency_key": "world-v2-action-test:intent-reply-1:reply",
            "status": status,
            "provider_ref": "provider-message-1",
            "artifact_refs": (),
            "cost_actual": 0,
            "observed_at": NOW,
            "raw_payload_hash": f"sha256:{source_event_id}",
        }
    )


def dispatch_started_ledger() -> WorldLedger:
    ledger = WorldLedger.in_memory(world_id=WORLD_ID)
    event_types = (
        ("ActionAuthorized", {"action": action().model_dump(mode="json")}),
        ("ActionScheduled", {"action_id": "action-reply-1"}),
        ("ActionClaimed", {"action_id": "action-reply-1"}),
        ("ActionDispatchStarted", {"action_id": "action-reply-1"}),
    )
    for index, (event_type, payload) in enumerate(event_types):
        ledger.commit(
            [
                action_event(
                    event_id=f"event-action-ready-{index}",
                    event_type=event_type,
                    payload=payload,
                )
            ],
            expected_world_revision=index,
            expected_deliberation_revision=0,
        )
    return ledger


def test_authorized_action_is_registered_in_the_authoritative_projection() -> None:
    ledger = WorldLedger.in_memory(world_id=WORLD_ID)
    authorized = action()

    ledger.commit(
        [
            action_event(
                event_id="event-action-authorized-1",
                event_type="ActionAuthorized",
                payload={"action": authorized.model_dump(mode="json")},
            )
        ],
        expected_world_revision=0,
        expected_deliberation_revision=0,
    )

    assert ledger.project().actions == (authorized,)


def test_two_actions_cannot_share_one_external_effect_identity() -> None:
    ledger = WorldLedger.in_memory(world_id=WORLD_ID)
    authorized = action()
    ledger.commit(
        [action_event(
            event_id="event-action-authorized-1",
            event_type="ActionAuthorized",
            payload={"action": authorized.model_dump(mode="json")},
        )],
        expected_world_revision=0,
        expected_deliberation_revision=0,
    )
    duplicate_effect = authorized.model_copy(update={"action_id": "action-reply-2"})

    with pytest.raises(ValueError, match="idempotency_key"):
        ledger.commit(
            [action_event(
                event_id="event-action-authorized-2",
                event_type="ActionAuthorized",
                payload={"action": duplicate_effect.model_dump(mode="json")},
            )],
            expected_world_revision=1,
            expected_deliberation_revision=0,
        )
    assert ledger.project().actions == (authorized,)


def test_action_follows_the_frozen_pre_dispatch_lifecycle() -> None:
    ledger = WorldLedger.in_memory(world_id=WORLD_ID)
    event_types = (
        ("ActionAuthorized", {"action": action().model_dump(mode="json")}),
        ("ActionScheduled", {"action_id": "action-reply-1"}),
        ("ActionClaimed", {"action_id": "action-reply-1"}),
        ("ActionDispatchStarted", {"action_id": "action-reply-1"}),
    )

    for index, (event_type, payload) in enumerate(event_types):
        ledger.commit(
            [
                action_event(
                    event_id=f"event-action-lifecycle-{index}",
                    event_type=event_type,
                    payload=payload,
                )
            ],
            expected_world_revision=index,
            expected_deliberation_revision=0,
        )

    assert ledger.project().actions[0].state == "dispatch_started"


def test_illegal_action_transition_is_rejected_without_advancing_the_world() -> None:
    ledger = WorldLedger.in_memory(world_id=WORLD_ID)
    ledger.commit(
        [
            action_event(
                event_id="event-action-authorized-invalid-transition",
                event_type="ActionAuthorized",
                payload={"action": action().model_dump(mode="json")},
            )
        ],
        expected_world_revision=0,
        expected_deliberation_revision=0,
    )

    with pytest.raises(InvalidActionTransition):
        ledger.commit(
            [
                action_event(
                    event_id="event-action-delivered-too-early",
                    event_type="ActionDelivered",
                    payload={"action_id": "action-reply-1"},
                )
            ],
            expected_world_revision=1,
            expected_deliberation_revision=0,
        )

    projection = ledger.project()
    assert projection.world_revision == 1
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
    assert first.committed_world_revision == 5
    assert ledger.project().actions[0].state == "provider_accepted"


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

    with pytest.raises(InvalidActionTransition):
        await runtime.settle(
            external_result(
                result_id="result-failed-after-delivery",
                source_event_id="receipt-failed-after-delivery",
                status="failed",
            )
        )
    assert ledger.project().actions[0].state == "delivered"


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

    with pytest.raises(InvalidActionTransition):
        await runtime.settle(
            external_result(
                result_id="result-delivered-after-unknown",
                source_event_id="receipt-delivered-after-unknown",
                status="delivered",
            )
        )
    assert ledger.project().actions[0].state == "unknown"


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

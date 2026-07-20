from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from companion_daemon.world_v2 import (
    Action,
    ActionIntent,
    AcceptanceErrorCode,
    BudgetAccount,
    ClaimLease,
    ExecutionReceipt,
    ReplayMode,
    TriggerProcess,
)
from companion_daemon.world_v2.schemas import WorldEvent


NOW = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)


def test_action_intent_is_not_an_authorized_action() -> None:
    intent = ActionIntent(
        schema_version="world-v2.1",
        intent_id="intent-reply-1",
        kind="reply",
        layer="external_action",
        target="user:geoff",
        payload_ref="message-payload-1",
        payload_hash="sha256:message-payload-1",
        dependencies=(),
    )

    assert "action_id" not in intent.model_fields_set
    assert "state" not in intent.model_fields_set
    with pytest.raises(ValidationError):
        ActionIntent.model_validate({**intent.model_dump(), "action_id": "action-not-authorized"})

    action = Action(
        schema_version="world-v2.1",
        action_id="action-reply-1",
        world_id="world-v2-test",
        logical_time=NOW,
        created_at=NOW,
        trace_id="trace-action-1",
        causation_id="acceptance-1",
        correlation_id="conversation-1",
        kind="reply",
        layer="external_action",
        intent_ref=intent.intent_id,
        actor="companion:zhizhi",
        target=intent.target,
        payload_ref=intent.payload_ref,
        payload_hash=intent.payload_hash,
        idempotency_key="world-v2-test:intent-reply-1:reply",
        dependencies=(),
        budget_reservation_id="budget-reply-1",
        state="authorized",
        recovery_policy="provider-effect-once",
    )
    assert action.intent_ref == intent.intent_id
    assert action.state == "authorized"


def test_acceptance_error_codes_are_the_frozen_hard_invariants() -> None:
    assert {item.value for item in AcceptanceErrorCode} == {
        "unsupported_claim",
        "stale_revision",
        "schema_invalid",
        "capability_denied",
        "privacy_denied",
        "consent_missing",
        "budget_unavailable",
        "action_duplicate",
        "dependency_unsatisfied",
        "expired_intent",
    }


def test_event_rejects_payload_bytes_that_do_not_match_the_recorded_hash() -> None:
    valid = WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id="event-1",
        world_id="world-v2-test",
        event_type="ObservationRecorded",
        logical_time=NOW,
        created_at=NOW,
        actor="system:test",
        source="test",
        trace_id="trace-1",
        causation_id="cause-1",
        correlation_id="correlation-1",
        idempotency_key="event-1",
        payload={"observation_id": "obs-1"},
    )

    with pytest.raises(ValidationError, match="payload_hash"):
        WorldEvent.model_validate(
            {**valid.model_dump(), "payload_json": '{"observation_id":"tampered"}'}
        )


def test_replay_mode_forbids_live_models_randomness_and_side_effects() -> None:
    mode = ReplayMode(
        schema_version="world-v2.1",
        request_id="replay-1",
        world_id="world-v2-test",
        from_revision=0,
        expected_hash=None,
        trace_id="trace-replay-1",
    )
    assert mode.model_result_policy == "recorded_only"
    assert mode.random_policy == "recorded_only"
    assert mode.side_effect_policy == "forbidden"

    with pytest.raises(ValidationError):
        ReplayMode.model_validate({**mode.model_dump(), "side_effect_policy": "allowed"})

    with pytest.raises(ValidationError, match="to_revision"):
        ReplayMode.model_validate({**mode.model_dump(), "from_revision": 10, "to_revision": 9})


def test_world_contracts_reject_timezone_naive_datetimes() -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        Action.model_validate(
            {
                "schema_version": "world-v2.1",
                "action_id": "action-naive",
                "world_id": "world-v2-test",
                "logical_time": datetime(2026, 7, 14, 12, 0),
                "created_at": NOW,
                "trace_id": "trace-naive",
                "causation_id": "cause-naive",
                "correlation_id": "correlation-naive",
                "kind": "reply",
                "layer": "external_action",
                "intent_ref": "intent-naive",
                "actor": "companion:test",
                "target": "user:test",
                "payload_ref": "payload:naive",
                "payload_hash": "sha256:naive",
                "idempotency_key": "action:naive",
                "budget_reservation_id": "budget:naive",
                "state": "authorized",
                "recovery_policy": "effect_once",
            }
        )


def test_execution_receipt_cannot_represent_a_contradictory_terminal_state() -> None:
    with pytest.raises(ValidationError, match="ack receipt"):
        ExecutionReceipt(
            receipt_id="receipt-invalid",
            result_id="result-invalid",
            action_id="action-1",
            provider="provider:test",
            provider_ref="provider-ref-1",
            source_event_id="provider-ref-1",
            receipt_kind="ack",
            observed_state="delivered",
            is_terminal=False,
            cost_actual=0,
            received_at=NOW,
            raw_payload_hash="sha256:invalid",
        )


def test_budget_and_trigger_authority_models_reject_inconsistent_state() -> None:
    with pytest.raises(ValidationError, match="overrun"):
        BudgetAccount(
            account_id="budget-invalid",
            category="chat",
            window_id="window-invalid",
            limit=10,
            spent=0,
            overrun=9,
        )

    lease = ClaimLease(
        owner_id="owner-2",
        attempt_id="attempt-2",
        acquired_at=NOW,
        expires_at=NOW.replace(hour=13),
    )
    with pytest.raises(ValidationError, match="latest attempt"):
        TriggerProcess(
            trigger_id="trigger-invalid",
            trigger_ref="result-invalid",
            process_kind="settlement",
            state="claimed",
            claim_lease=lease,
            attempt_ids=("attempt-1",),
        )

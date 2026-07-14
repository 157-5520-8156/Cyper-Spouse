from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from companion_daemon.world_v2 import Action, ActionIntent, AcceptanceErrorCode
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
        ActionIntent.model_validate(
            {**intent.model_dump(), "action_id": "action-not-authorized"}
        )

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

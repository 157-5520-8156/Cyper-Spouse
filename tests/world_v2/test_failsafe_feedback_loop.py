"""Feedback-loop seams for failsafe diagnosis and durable reliability."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
import sqlite3
from pathlib import Path

import pytest

from companion_daemon.world_v2.deliberation import ModelInput, ModelRoute, TriggerMessage
from companion_daemon.world_v2.durable_reliability import (
    clear_durable_reliability_cache_for_tests,
    durable_reliability_snapshot,
)
from companion_daemon.world_v2.proposal_envelope import ProposalEvidenceRef
from companion_daemon.world_v2.single_call_inbound_cognition import SingleCallInboundCognition


OLD_ENGINEERING_ACK = "我刚才没接好这句，不想装作已经回答了；但我看到你说了什么。"


class _UnusedProvider:
    model = "unused"

    async def complete(
        self, messages: list[dict[str, str]], *, temperature: float = 0.8
    ) -> str:
        raise AssertionError("local failsafe must not call the provider")


def _request(text: str) -> ModelInput:
    return ModelInput(
        call_id="call:failsafe-feedback",
        attempt_id="attempt:failsafe-feedback",
        route=ModelRoute(tier="flash", reason_code="ordinary", router_version="test.1"),
        capsule_id="a" * 64,
        trigger_ref="event:observation:failsafe-feedback",
        evaluated_world_revision=3,
        model_content_json=json.dumps({"world_revision": 3}),
        trigger_evidence=(
            ProposalEvidenceRef(
                ref_id="observation:failsafe-feedback",
                evidence_kind="observed_message",
                source_world_revision=3,
                immutable_hash="sha256:" + "b" * 64,
            ),
        ),
        trigger_message=TriggerMessage(
            event_ref="event:observation:failsafe-feedback",
            event_payload_hash="sha256:" + "b" * 64,
            observation_ref="observation:failsafe-feedback",
            source_world_revision=3,
            actor="user:primary",
            channel="qq_c2c",
            reply_target="qq:user:1",
            text=text,
        ),
    )


@pytest.mark.asyncio
async def test_generic_failure_no_longer_emits_the_engineering_ack() -> None:
    cognition = SingleCallInboundCognition(flash_model=_UnusedProvider())
    with pytest.raises(RuntimeError, match="model-owned expression unavailable"):
        await cognition.expression.recover(
            _request("所以这是我们第一次聊天吗"), "main_timeout"
        )


@pytest.mark.asyncio
async def test_diagnosis_repro_emotion_intent_is_not_generic_ack() -> None:
    cognition = SingleCallInboundCognition(flash_model=_UnusedProvider())
    with pytest.raises(RuntimeError, match="model-owned expression unavailable"):
        await cognition.expression.recover(
            _request("你真的生气了吗？"), "main_timeout"
        )


@pytest.mark.asyncio
async def test_future_plan_question_recovers_with_uncertainty_not_engineering_ack() -> None:
    cognition = SingleCallInboundCognition(flash_model=_UnusedProvider())
    with pytest.raises(RuntimeError, match="model-owned expression unavailable"):
        await cognition.expression.recover(
            _request("那你下午准备干嘛？"), "main_timeout"
        )


def test_durable_reliability_counts_delivered_replies_and_failsafe_audits(
    tmp_path: Path,
) -> None:
    clear_durable_reliability_cache_for_tests()
    path = tmp_path / "reliability.sqlite"
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE world_v2_events (ledger_sequence INTEGER PRIMARY KEY, event_json TEXT NOT NULL)"
    )
    now = datetime(2026, 7, 25, 1, 0, tzinfo=UTC)
    events = [
        {
            "event_type": "ActionAuthorized",
            "logical_time": now.isoformat().replace("+00:00", "Z"),
            "correlation_id": "corr:1",
            "payload_json": json.dumps(
                {
                    "action": {
                        "action_id": "action:1",
                        "kind": "reply",
                    }
                }
            ),
        },
        {
            "event_type": "ActionDelivered",
            "logical_time": now.isoformat().replace("+00:00", "Z"),
            "payload_json": json.dumps({"action_id": "action:1"}),
        },
        {
            "event_type": "ModelResultRecorded",
            "logical_time": now.isoformat().replace("+00:00", "Z"),
            "payload_json": json.dumps(
                {
                    "audit_json": json.dumps(
                        {
                            "attempt_id": "attempt:failsafe:1",
                            "model_id": "local-expression-failsafe",
                            "model_version": "local-expression-failsafe.1",
                        }
                    )
                }
            ),
        },
        {
            "event_type": "ActionAuthorized",
            "logical_time": (now - timedelta(hours=30)).isoformat().replace("+00:00", "Z"),
            "correlation_id": "corr:old",
            "payload_json": json.dumps(
                {"action": {"action_id": "action:old", "kind": "reply"}}
            ),
        },
    ]
    for index, event in enumerate(events, start=1):
        connection.execute(
            "INSERT INTO world_v2_events(ledger_sequence, event_json) VALUES (?, ?)",
            (index, json.dumps(event)),
        )
    connection.commit()
    connection.close()

    snapshot = durable_reliability_snapshot(
        str(path), hours=24, now=now, max_events=100
    )
    assert snapshot["visible_delivered_24h"] == 1
    assert snapshot["failsafe_model_results_24h"] == 1
    assert snapshot["failsafe_rate_24h"] == 1.0
    assert snapshot["source"] == "ledger"

"""Feedback-loop seams for failsafe diagnosis and durable reliability."""

from __future__ import annotations

import asyncio
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


class _FailingRecoveryProvider:
    model = "failed-recovery"

    def __init__(self) -> None:
        self.calls: list[list[dict[str, str]]] = []

    async def complete(
        self, messages: list[dict[str, str]], *, temperature: float = 0.8
    ) -> str:
        del temperature
        self.calls.append(messages)
        raise RuntimeError("recovery provider unavailable")


class _ContextualFailureProvider:
    model = "local-contextual-failure"

    def __init__(self) -> None:
        self.calls: list[list[dict[str, str]]] = []

    async def complete(
        self, messages: list[dict[str, str]], *, temperature: float = 0.8
    ) -> str:
        del temperature
        self.calls.append(messages)
        return json.dumps(
            {
                "timing_choice": "now",
                "beats": [
                    {
                        "modality": "text",
                        "text": "我在整理桌面，刚看到你这句。",
                    }
                ],
                "stance": "resume_from_current_life",
                "brief_rationale": "Resume from the source-backed current activity.",
                "confidence": 6200,
                "world_claims": [
                    {
                        "claim_text": "我在整理桌面",
                        "scope": "current_world",
                        "source_refs": ["situation:desk"],
                    }
                ],
            },
            ensure_ascii=False,
        )


class _ContextualGroundingReviewer:
    model = "independent-grounding-reviewer"

    def __init__(self) -> None:
        self.calls: list[list[dict[str, str]]] = []

    async def complete(
        self, messages: list[dict[str, str]], *, temperature: float = 0.8
    ) -> str:
        del temperature
        self.calls.append(messages)
        return json.dumps(
            {
                "ci": [],
                "v": [],
                "p": [],
                "r": (
                    "The visible activity explanation and its declaration "
                    "are directly present in Context."
                ),
            }
        )


class _InvalidContextualFailureProvider(_ContextualFailureProvider):
    model = "invalid-local-contextual-failure"

    async def complete(
        self, messages: list[dict[str, str]], *, temperature: float = 0.8
    ) -> str:
        raw = await super().complete(messages, temperature=temperature)
        value = json.loads(raw)
        value["world_claims"][0]["source_refs"] = ["situation:not-in-context"]
        return json.dumps(value, ensure_ascii=False)


class _UnrelatedContextualFailureProvider(_ContextualFailureProvider):
    model = "unrelated-local-contextual-failure"

    async def complete(
        self, messages: list[dict[str, str]], *, temperature: float = 0.8
    ) -> str:
        del temperature
        self.calls.append(messages)
        return json.dumps(
            {
                "timing_choice": "now",
                "beats": [{"modality": "text", "text": "我刚洗完澡，看到你了。"}],
                "stance": "invented_context",
                "brief_rationale": "Use a plausible but unsupported excuse.",
                "confidence": 6200,
                "world_claims": [
                    {
                        "claim_text": "我刚洗完澡",
                        "scope": "current_world",
                        "source_refs": ["situation:desk"],
                    }
                ],
            },
            ensure_ascii=False,
        )


class _UnrelatedGroundingReviewer(_ContextualGroundingReviewer):
    model = "independent-unrelated-grounding-reviewer"

    async def complete(
        self, messages: list[dict[str, str]], *, temperature: float = 0.8
    ) -> str:
        del temperature
        self.calls.append(messages)
        return json.dumps(
            {
                "ci": [0],
                "v": [],
                "p": [],
                "r": "Desk tidying does not support a shower occurrence.",
            }
        )


class _UndeclaredExcuseProvider(_ContextualFailureProvider):
    model = "undeclared-excuse-provider"

    async def complete(
        self, messages: list[dict[str, str]], *, temperature: float = 0.8
    ) -> str:
        raw = await super().complete(messages, temperature=temperature)
        value = json.loads(raw)
        value["beats"][0]["text"] = "我在整理桌面，刚洗完澡才看到你这句。"
        return json.dumps(value, ensure_ascii=False)


class _UndeclaredExcuseReviewer(_ContextualGroundingReviewer):
    model = "independent-undeclared-excuse-reviewer"

    async def complete(
        self, messages: list[dict[str, str]], *, temperature: float = 0.8
    ) -> str:
        del temperature
        self.calls.append(messages)
        request = json.loads(messages[1]["content"])
        assert "刚洗完澡" in request["visible_text"]
        return json.dumps(
            {
                "ci": [],
                "v": ["undeclared_external_assertion"],
                "p": [],
                "r": "The visible shower occurrence has no declaration or source.",
            },
            ensure_ascii=False,
        )


class _HangingRecoveryProvider(_FailingRecoveryProvider):
    model = "hanging-recovery"

    async def complete(
        self, messages: list[dict[str, str]], *, temperature: float = 0.8
    ) -> str:
        del temperature
        self.calls.append(messages)
        await asyncio.sleep(60)
        raise AssertionError("hanging recovery should have timed out")


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


def _request_with_current_activity(text: str) -> ModelInput:
    request = _request(text)
    return request.model_copy(
        update={
            "model_content_json": json.dumps(
                {
                    "world_revision": 3,
                    "slices": {
                        "current_situation": {
                            "availability": "available",
                            "items": [
                                {
                                    "item_ref": "situation:desk",
                                    "value": {"activity": "整理桌面"},
                                }
                            ],
                        }
                    },
                },
                ensure_ascii=False,
            )
        }
    )


@pytest.mark.asyncio
async def test_generic_failure_no_longer_emits_the_engineering_ack() -> None:
    cognition = SingleCallInboundCognition(flash_model=_UnusedProvider())
    with pytest.raises(RuntimeError, match="model-owned expression unavailable"):
        await cognition.expression.recover(
            _request("所以这是我们第一次聊天吗"), "main_timeout"
        )


@pytest.mark.asyncio
async def test_contextual_failure_recovery_is_dormant_by_default() -> None:
    recovery = _FailingRecoveryProvider()
    contextual = _ContextualFailureProvider()
    cognition = SingleCallInboundCognition(
        flash_model=_UnusedProvider(),
        recovery_model=recovery,
        contextual_failsafe_model=contextual,
        contextual_failsafe_reviewer_model=_ContextualGroundingReviewer(),
    )

    with pytest.raises(RuntimeError, match="model-owned expression unavailable"):
        await cognition.expression.recover(
            _request_with_current_activity("你刚才在忙吗？"),
            "main_timeout",
        )

    assert len(recovery.calls) == 1
    assert contextual.calls == []


def test_contextual_failure_recovery_rejects_self_review_configuration() -> None:
    contextual = _ContextualFailureProvider()

    with pytest.raises(ValueError, match="generation and reviewer must be independent"):
        SingleCallInboundCognition(
            flash_model=_UnusedProvider(),
            recovery_model=_FailingRecoveryProvider(),
            contextual_failsafe_model=contextual,
            contextual_failsafe_reviewer_model=contextual,
            contextual_failsafe_enabled=True,
        )


@pytest.mark.asyncio
async def test_enabled_contextual_failure_recovery_uses_pinned_world_facts() -> None:
    recovery = _FailingRecoveryProvider()
    contextual = _ContextualFailureProvider()
    reviewer = _ContextualGroundingReviewer()
    cognition = SingleCallInboundCognition(
        flash_model=_UnusedProvider(),
        recovery_model=recovery,
        contextual_failsafe_model=contextual,
        contextual_failsafe_reviewer_model=reviewer,
        contextual_failsafe_enabled=True,
    )

    output = await cognition.expression.recover(
        _request_with_current_activity("你刚才在忙吗？"),
        "main_timeout",
    )

    assert len(recovery.calls) == 1
    assert len(contextual.calls) == 1
    assert len(reviewer.calls) == 1
    assert "整理桌面" in contextual.calls[0][1]["content"]
    assert "source-backed current/recent situation" in contextual.calls[0][0]["content"]
    review_material = json.loads(reviewer.calls[0][1]["content"])
    assert "整理桌面" in review_material["visible_text"]
    assert review_material["claims"][0]["claim_text"] == "我在整理桌面"
    assert output.model_id.startswith("contextual-failure-recovery:")
    assert output.model_version == "contextual-failure-recovery.1"
    assert "整理桌面" in json.dumps(output.raw_proposal, ensure_ascii=False)


@pytest.mark.asyncio
async def test_contextual_failure_recovery_rejects_a_claim_outside_pinned_context() -> None:
    contextual = _InvalidContextualFailureProvider()
    reviewer = _ContextualGroundingReviewer()
    cognition = SingleCallInboundCognition(
        flash_model=_UnusedProvider(),
        recovery_model=_FailingRecoveryProvider(),
        contextual_failsafe_model=contextual,
        contextual_failsafe_reviewer_model=reviewer,
        contextual_failsafe_enabled=True,
    )

    with pytest.raises(RuntimeError, match="model-owned expression unavailable"):
        await cognition.expression.recover(
            _request_with_current_activity("你刚才在忙吗？"),
            "main_timeout",
        )

    assert len(contextual.calls) == 1
    assert len(reviewer.calls) == 1


@pytest.mark.asyncio
async def test_contextual_failure_recovery_rejects_an_unrelated_valid_source() -> None:
    contextual = _UnrelatedContextualFailureProvider()
    reviewer = _UnrelatedGroundingReviewer()
    cognition = SingleCallInboundCognition(
        flash_model=_UnusedProvider(),
        recovery_model=_FailingRecoveryProvider(),
        contextual_failsafe_model=contextual,
        contextual_failsafe_reviewer_model=reviewer,
        contextual_failsafe_enabled=True,
    )

    with pytest.raises(RuntimeError, match="model-owned expression unavailable"):
        await cognition.expression.recover(
            _request_with_current_activity("你刚才在忙吗？"),
            "main_timeout",
        )

    assert len(contextual.calls) == 1
    assert len(reviewer.calls) == 1


@pytest.mark.asyncio
async def test_contextual_failure_recovery_rejects_an_undeclared_visible_excuse() -> None:
    contextual = _UndeclaredExcuseProvider()
    reviewer = _UndeclaredExcuseReviewer()
    cognition = SingleCallInboundCognition(
        flash_model=_UnusedProvider(),
        recovery_model=_FailingRecoveryProvider(),
        contextual_failsafe_model=contextual,
        contextual_failsafe_reviewer_model=reviewer,
        contextual_failsafe_enabled=True,
    )

    with pytest.raises(RuntimeError, match="model-owned expression unavailable"):
        await cognition.expression.recover(
            _request_with_current_activity("你刚才在忙吗？"),
            "main_timeout",
        )

    assert len(contextual.calls) == 1
    assert len(reviewer.calls) == 1


@pytest.mark.asyncio
async def test_hanging_second_provider_leaves_time_for_contextual_recovery() -> None:
    recovery = _HangingRecoveryProvider()
    contextual = _ContextualFailureProvider()
    reviewer = _ContextualGroundingReviewer()
    cognition = SingleCallInboundCognition(
        flash_model=_UnusedProvider(),
        recovery_model=recovery,
        contextual_failsafe_model=contextual,
        contextual_failsafe_reviewer_model=reviewer,
        contextual_failsafe_enabled=True,
    )

    output = await cognition.expression.recover(
        _request_with_current_activity("你刚才在忙吗？"),
        "main_timeout",
    )

    assert len(recovery.calls) == 1
    assert len(contextual.calls) == 1
    assert len(reviewer.calls) == 1
    assert output.model_version == "contextual-failure-recovery.1"


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
            "logical_time": now.isoformat().replace("+00:00", "Z"),
            "correlation_id": "corr:2",
            "payload_json": json.dumps(
                {"action": {"action_id": "action:2", "kind": "reply"}}
            ),
        },
        {
            "event_type": "ActionDelivered",
            "logical_time": now.isoformat().replace("+00:00", "Z"),
            "payload_json": json.dumps({"action_id": "action:2"}),
        },
        {
            "event_type": "ModelResultRecorded",
            "logical_time": now.isoformat().replace("+00:00", "Z"),
            "payload_json": json.dumps(
                {
                    "audit_json": json.dumps(
                        {
                            "attempt_id": "attempt:contextual-failsafe:2",
                            "model_id": "contextual-failure-recovery:local-role",
                            "model_version": "contextual-failure-recovery.1",
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
    assert snapshot["visible_delivered_24h"] == 2
    assert snapshot["failsafe_model_results_24h"] == 2
    assert snapshot["failsafe_rate_24h"] == 1.0
    assert snapshot["source"] == "ledger"

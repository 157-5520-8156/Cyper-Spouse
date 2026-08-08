from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
from typing import Any

import pytest

from companion_daemon.config import Settings
from companion_daemon.llm import FakeCompanionModel
from companion_daemon.world_v2.qq_c2c_host import build_qq_c2c_host


HOST_QUALIFICATION_DECLARATION = {
    "qualification_layer": "public_host_scenario",
    "scenario_id": "qq-later-text-restart-effect-once.1",
    "mechanisms": (
        "expression.deferred_reply",
        "conversation.commitment_due",
        "action.authorized_due:text",
    ),
    "public_seams": (
        "QQC2CHost.inbound_text",
        "QQC2CHost.tick",
        "QQC2CHost.drain",
        "QQC2CHost.export_replay_evidence",
        "QQC2CHost.aclose",
    ),
    "receipt_scope": "qq_transport_terminal",
    "excluded_scope": "WorldV2PlatformHost.receipt",
}


def _observation_ref(messages: list[dict[str, str]]) -> str:
    for message in messages:
        try:
            material = json.loads(message["content"])
        except (json.JSONDecodeError, KeyError, TypeError):
            continue
        if not isinstance(material, dict):
            continue
        trigger = material.get("current_trigger_message")
        if isinstance(trigger, dict) and isinstance(trigger.get("observation_ref"), str):
            return trigger["observation_ref"]
    raise AssertionError("same-role fixture did not receive a pinned observation")


def _expression_response(
    messages: list[dict[str, str]], expression: dict[str, object]
) -> str:
    system = messages[0]["content"]
    if "appraisal_draft" not in system or "expression_draft" not in system:
        return json.dumps(expression, ensure_ascii=False)
    return json.dumps(
        {
            "appraisal_draft": {
                "appraise": False,
                "affect": "no_change",
                "brief_rationale": "No durable appraisal is needed for this fixture.",
                "behavior_tendency": "choose_own_response",
                "stance": "self_directed",
                "display_strategy": "model_owned",
                "confidence": 7_000,
            },
            "expression_draft": expression,
        },
        ensure_ascii=False,
    )


class _SameRoleLaterModel:
    model = "fixture:host-qualification-same-role-later"

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.8,
    ) -> str:
        del temperature
        observation_ref = _observation_ref(messages)
        return _expression_response(
            messages,
            {
                "private_turn_state": {
                    "contract": "private-turn-state.1",
                    "inner_state_summary": "我想忙完以后再认真接住这句话。",
                    "attended_source_refs": [observation_ref],
                },
                "timing_choice": "later",
                "beats": [{"modality": "text", "text": "我忙完来找你。"}],
                "cadence": "conversational",
                "delay_seconds": 28_800,
                "expires_after_seconds": 43_200,
                "stance": "defer",
                "brief_rationale": "I chose to return later.",
                "confidence": 7_200,
                "world_claims": [],
            },
        )


class _ProductionDeliveryInterceptor:
    """Intercept the real QQ delivery adapter boundary and expose transport effects."""

    def __init__(self) -> None:
        self.texts: list[str] = []
        self.messages: dict[str, str] = {}

    async def send_text(self, _recipient_id: str, text: str) -> dict[str, object]:
        message_id = f"message:qualified:{len(self.texts) + 1}"
        self.texts.append(text)
        self.messages[message_id] = text
        return {"status": "ok", "data": {"message_id": message_id}}

    async def send_reaction(
        self, _recipient_id: str, *, message_id: str, reaction_id: str
    ) -> dict[str, object]:
        del message_id, reaction_id
        return {"status": "failed"}

    async def send_sticker(
        self, _recipient_id: str, *, sticker_id: str
    ) -> dict[str, object]:
        del sticker_id
        return {"status": "failed"}

    async def send_typing(
        self, _recipient_id: str, *, state: str
    ) -> dict[str, object]:
        del state
        return {"status": "ok", "data": {"message_id": "typing"}}

    async def get_message(
        self, _recipient_id: str, *, message_id: str
    ) -> dict[str, object]:
        return {
            "status": "ok",
            "retcode": 0,
            "data": {"message_id": message_id, "message": self.messages[message_id]},
        }


async def _run_public_host_qualification(tmp_path: Path) -> dict[str, Any]:
    started_at = datetime.now(UTC).replace(microsecond=0)
    scheduler_clock = {"now": started_at}
    pacing_clock = {"now": started_at}
    delivery = _ProductionDeliveryInterceptor()
    settings = Settings(
        database_path=tmp_path / "public-host-qualification.sqlite",
        PRIMARY_USER_ID="geoff",
        WORLD_V2_EXPRESSION_EPISODE_MODE="off",
    )

    async def skip_pacing(seconds: float) -> None:
        pacing_clock["now"] += timedelta(seconds=max(0.0, seconds))
        await asyncio.sleep(0)

    def build():
        return build_qq_c2c_host(
            settings=settings,
            recipient_id="10001",
            bootstrap_at=started_at,
            model=_SameRoleLaterModel(),
            world_support_model=FakeCompanionModel(),
            delivery=delivery,
            ingress_now=lambda: pacing_clock["now"],
            ingress_sleep=skip_pacing,
            action_due_now=lambda: scheduler_clock["now"],
        )

    first = build()
    try:
        outcome = await first.inbound_text(
            message_id="message:host-qualification-later",
            recipient_id="10001",
            text="你先忙吧",
            observed_at=started_at,
        )
        created = first.export_replay_evidence()
        assert outcome.status == "deferred"
        assert created.projection.semantic_hash == created.replay.semantic_hash
        assert created.projection.actions, (
            tuple(item.event.event_type for item in created.events),
            tuple(item.model_dump(mode="json") for item in created.projection.model_result_audits),
        )
        action = next(item for item in created.projection.actions if item.kind == "followup")
        assert action.not_before is not None
        assert action.expression_plan_id is not None
        due_at = action.not_before
        action_id = action.action_id
        plan_id = action.expression_plan_id
        commitment = next(
            item
            for item in created.projection.commitments
            if item.values.fulfillment_contract.expected_action_id == action_id
        )
        commitment_id = commitment.commitment_id
        assert action.state in {"authorized", "scheduled"}
        assert commitment.values.status == "open"
        assert delivery.texts == []
    finally:
        await first.aclose()

    second = build()
    try:
        rebuilt = second.export_replay_evidence()
        assert rebuilt.projection.semantic_hash == created.projection.semantic_hash
        before_due = due_at - timedelta(microseconds=1)
        await second.tick(
            tick_id="host-qualification-before-due",
            logical_time_from=rebuilt.projection.logical_time,
            logical_time_to=before_due,
            observed_at=before_due,
            reason="host_qualification_before_due",
            run_life_ecology=False,
        )
        await second.drain(max_action_units=8, max_background_units=0)
        assert delivery.texts == []

        await second.tick(
            tick_id="host-qualification-at-due",
            logical_time_from=before_due,
            logical_time_to=due_at,
            observed_at=due_at,
            reason="host_qualification_at_due",
            run_life_ecology=False,
        )
        await second.drain(max_action_units=8, max_background_units=0)
        sent = second.export_replay_evidence()
        sent_action = next(item for item in sent.projection.actions if item.action_id == action_id)
        assert delivery.texts == ["我忙完来找你。"]
        assert sent_action.state == "provider_accepted"
        assert sent_action.claim_lease is not None
        reconciliation_at = sent_action.claim_lease.expires_at
    finally:
        await second.aclose()

    third = build()
    try:
        restarted = third.export_replay_evidence()
        assert restarted.projection.semantic_hash == sent.projection.semantic_hash
        await third.tick(
            tick_id="host-qualification-reconcile-after-restart",
            logical_time_from=restarted.projection.logical_time,
            logical_time_to=reconciliation_at,
            observed_at=reconciliation_at,
            reason="host_qualification_reconcile_after_restart",
            run_life_ecology=False,
        )
        await third.drain(max_action_units=8, max_background_units=0)
        final = third.export_replay_evidence()
        assert delivery.texts == ["我忙完来找你。"]
    finally:
        await third.aclose()

    cold = build()
    try:
        replayed = cold.export_replay_evidence()
    finally:
        await cold.aclose()

    final_action = next(item for item in replayed.projection.actions if item.action_id == action_id)
    terminal_receipt = next(
        item
        for item in replayed.projection.execution_receipts
        if item.action_id == action_id and item.is_terminal
    )
    final_commitment = next(
        item
        for item in replayed.projection.commitments
        if item.commitment_id == commitment_id
    )
    expression_plan = next(
        item for item in replayed.projection.expression_plans if item.plan_id == plan_id
    )
    return {
        "qualification": HOST_QUALIFICATION_DECLARATION,
        "send_count": len(delivery.texts),
        "action_state": final_action.state,
        "receipt_state": terminal_receipt.observed_state,
        "commitment_state": final_commitment.values.status,
        "expression_plan_state": expression_plan.state,
        "cold_replay_hash_matches": (
            final.projection.semantic_hash
            == final.replay.semantic_hash
            == replayed.projection.semantic_hash
            == replayed.replay.semantic_hash
        ),
    }


@pytest.mark.asyncio
async def test_public_host_later_text_survives_restart_and_settles_effect_once(
    tmp_path: Path,
) -> None:
    evidence = await _run_public_host_qualification(tmp_path)

    assert evidence["qualification"] == HOST_QUALIFICATION_DECLARATION
    assert evidence["send_count"] == 1
    assert evidence["action_state"] == "delivered"
    assert evidence["receipt_state"] == "delivered"
    assert evidence["commitment_state"] == "fulfilled"
    assert evidence["expression_plan_state"] == "completed"
    assert evidence["cold_replay_hash_matches"] is True

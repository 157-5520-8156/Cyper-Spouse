from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
from typing import Any

import pytest

from companion_daemon.config import Settings
from companion_daemon.delayed_trigger_catalog import load_delayed_trigger_catalog
from companion_daemon.llm import FakeCompanionModel
from companion_daemon.world_v2.qq_c2c_host import build_qq_c2c_host


HOST_QUALIFICATION_DECLARATIONS = {
    "qualification_layer": "accepted_typed_plan_host_lifecycle",
    "excluded_evidence": (
        "production_stream_expression_episode",
        "real_provider_author_transport",
        "character_autonomy",
    ),
    "fixture_authority": "scripted_legal_role_outcomes_open_downstream_only",
    "scenarios": {
        "expression.multibeat_due": {
            "catalog_mechanism_id": "expression.multibeat",
            "variant": "ordered_later_text_beats",
            "receipt_scope": "qq_transport_terminal",
            "delay_seconds": 60,
            "expires_after_seconds": 600,
        },
        "expression.interjection_reconsideration": {
            "catalog_mechanism_id": "expression.reconsideration",
            "variant": "interjection_cancel",
            "receipt_scope": "local_cancellation_has_no_transport_receipt",
        },
    },
    "public_seams": (
        "QQC2CHost.inbound_text",
        "QQC2CHost.tick",
        "QQC2CHost.drain",
        "QQC2CHost.export_replay_evidence",
        "QQC2CHost.aclose",
    ),
}

# Rebuilding a public host intentionally exercises SQLite cold recovery and
# scheduler-lane shutdown on every step.  Under the complete qualification
# collection those lifecycle operations can take several seconds each; the
# timeout is a safety bound for a stuck recovery loop, not a latency claim.
HOST_LIFECYCLE_TIMEOUT_SECONDS = 60.0
_CATALOG = Path("configs/delayed_trigger_qualification.v1.yaml")


def _host_scenario(
    scenario_id: str, nodeid: str, *, mechanism_ids: tuple[str, ...]
):
    evidence = load_delayed_trigger_catalog(_CATALOG).host_scenario(scenario_id)
    assert evidence.test_nodeid == nodeid
    assert evidence.mechanism_ids == mechanism_ids
    assert evidence.qualification_scope == "accepted_typed_plan_host_lifecycle"
    assert set(HOST_QUALIFICATION_DECLARATIONS["excluded_evidence"]) <= set(
        evidence.excluded_scope
    )
    assert {
        "onebot_provider_callback_normalization",
        "24_hour_soak",
    } <= set(evidence.excluded_scope)
    return evidence


def _json_material(messages: list[dict[str, str]]) -> dict[str, object]:
    for message in reversed(messages):
        try:
            material = json.loads(message["content"])
        except (json.JSONDecodeError, KeyError, TypeError):
            continue
        if isinstance(material, dict):
            return material
    raise AssertionError("fixture did not receive a JSON capability or turn capsule")


def _observation_ref(material: dict[str, object]) -> str:
    trigger = material.get("current_trigger_message")
    if isinstance(trigger, dict) and isinstance(trigger.get("observation_ref"), str):
        return trigger["observation_ref"]
    raise AssertionError("fixture expression call omitted its pinned observation")


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


class _ScriptedRoleModel:
    """Provide legal role outcomes only to open deterministic host lifecycle paths.

    This is deliberately not a semantic-authority or provider qualification.  Routes are
    consumed in call order and by CharacterInterior purpose rather than by user wording, so
    the fixture does not install a keyword-to-behaviour rule in the exercised production path.
    """

    model = "fixture:host-qualification-multibeat-and-reconsideration"
    supports_required_tool_choice = True

    def __init__(self) -> None:
        self.reconsideration_calls = 0
        self.expression_routes = iter(("later_two_beats", "silent_after_interjection"))

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.8,
    ) -> str:
        del temperature
        material = _json_material(messages)
        inner_turn = material.get("inner_turn")
        if isinstance(inner_turn, dict):
            purpose = inner_turn.get("purpose")
            if purpose != "expression_reconsideration":
                raise AssertionError(f"unexpected CharacterInterior purpose: {purpose!r}")
            capability = material.get("capability_manifest")
            if not isinstance(capability, dict):
                raise AssertionError("reconsideration omitted its capability manifest")
            source_refs = capability.get("source_refs")
            if not isinstance(source_refs, list) or not source_refs:
                raise AssertionError("reconsideration omitted source authority")
            self.reconsideration_calls += 1
            return json.dumps(
                {
                    "status": "decision",
                    "summary": "新消息改变了语境，我决定不再发送旧的两条。",
                    "attended_source_refs": [],
                    "decision": {
                        "source_refs": source_refs,
                        "payload": {"disposition": "cancel"},
                    },
                    "recall_query": None,
                    "proposals": [],
                },
                ensure_ascii=False,
            )

        observation_ref = _observation_ref(material)
        try:
            route = next(self.expression_routes)
        except StopIteration as exc:
            raise AssertionError("unexpected expression fixture route") from exc
        if route == "silent_after_interjection":
            expression = {
                "private_turn_state": {
                    "contract": "private-turn-state.1",
                    "inner_state_summary": "她撤回了刚才的语境，我先不另发新消息。",
                    "attended_source_refs": [observation_ref],
                },
                "timing_choice": "silent",
                "beats": [],
                "cadence": "conversational",
                "stance": "listen",
                "brief_rationale": "I chose not to add a new visible reply.",
                "confidence": 7_400,
                "world_claims": [],
            }
        elif route == "later_two_beats":
            expression = {
                "private_turn_state": {
                    "contract": "private-turn-state.1",
                    "inner_state_summary": "我想忙完后分两条把这件事说清楚。",
                    "attended_source_refs": [observation_ref],
                },
                "timing_choice": "later",
                "beats": [
                    {"modality": "text", "text": "第一条晚点说。"},
                    {"modality": "text", "text": "第二条也晚点说。"},
                ],
                "cadence": "conversational",
                "delay_seconds": 60,
                "expires_after_seconds": 600,
                "stance": "defer_two_beats",
                "brief_rationale": "I chose two ordered later messages.",
                "confidence": 7_600,
                "world_claims": [],
            }
        else:
            raise AssertionError(f"unknown expression fixture route: {route!r}")
        return _expression_response(messages, expression)

    async def complete_json(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.8,
        tools: object | None = None,
        tool_choice: object | None = None,
    ) -> str:
        if tools is not None:
            material = _json_material(messages)
            purpose = material.get("inner_turn", {}).get("purpose")
            expected = {
                "expression_reconsideration": (
                    "character_role_expression_reconsideration_v1"
                ),
            }.get(purpose)
            if tool_choice != {
                "type": "function",
                "function": {"name": expected},
            }:
                raise AssertionError("unexpected expression reconsideration required-tool choice")
        return await self.complete(messages, temperature=temperature)


class _ProductionDeliveryInterceptor:
    def __init__(self) -> None:
        self.texts: list[str] = []
        self.messages: dict[str, str] = {}

    async def send_text(self, _recipient_id: str, text: str) -> dict[str, object]:
        message_id = f"message:multibeat:{len(self.texts) + 1}"
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


class _PublicHostHarness:
    def __init__(self, *, tmp_path: Path, database_name: str) -> None:
        self.started_at = datetime.now(UTC).replace(microsecond=0)
        self.scheduler_clock = {"now": self.started_at}
        self.pacing_clock = {"now": self.started_at}
        self.delivery = _ProductionDeliveryInterceptor()
        self.model = _ScriptedRoleModel()
        self.settings = Settings(
            database_path=tmp_path / database_name,
            PRIMARY_USER_ID="geoff",
            # The production default is stream. This fixture has no stream transport and this
            # file declares only the accepted typed-plan host lifecycle after authoring.
            WORLD_V2_EXPRESSION_EPISODE_MODE="off",
        )

    async def skip_pacing(self, seconds: float) -> None:
        self.pacing_clock["now"] += timedelta(seconds=max(0.0, seconds))
        await asyncio.sleep(0)

    def build(self):
        return build_qq_c2c_host(
            settings=self.settings,
            recipient_id="10001",
            bootstrap_at=self.started_at,
            model=self.model,
            world_support_model=FakeCompanionModel(),
            delivery=self.delivery,
            ingress_now=lambda: self.pacing_clock["now"],
            ingress_sleep=self.skip_pacing,
            action_due_now=lambda: self.scheduler_clock["now"],
            # This host scenario qualifies Action/restart semantics, not the
            # optional semantic embedding service.  Keep the fixture fully
            # deterministic and avoid probing an unavailable local endpoint.
            use_configured_recall_embedding=False,
        )


async def _advance_public_action_recovery_once(
    harness: _PublicHostHarness,
    *,
    ordinal: int,
) -> None:
    host = harness.build()
    try:
        evidence = host.export_replay_evidence()
        pending_targets = []
        for action in evidence.projection.actions:
            if action.state in {"authorized", "scheduled"} and action.not_before is not None:
                pending_targets.append(action.not_before)
            elif action.state == "provider_accepted" and action.claim_lease is not None:
                pending_targets.append(action.claim_lease.expires_at)
        if pending_targets:
            target = min(pending_targets)
            logical_from = evidence.projection.logical_time
            if logical_from is not None and target > logical_from:
                harness.scheduler_clock["now"] = target
                await host.tick(
                    tick_id=f"host-qualification-action-step-{ordinal}",
                    logical_time_from=logical_from,
                    logical_time_to=target,
                    observed_at=target,
                    reason="host_qualification_action_recovery",
                    run_life_ecology=False,
                )
        await host.drain(max_action_units=1, max_background_units=0)
    finally:
        await host.aclose()


async def _recover_actions_until_delivered(
    harness: _PublicHostHarness,
    *,
    action_ids: set[str],
) -> None:
    """Restart the public host until these exact Actions have terminal delivery evidence."""

    step = 1
    async with asyncio.timeout(HOST_LIFECYCLE_TIMEOUT_SECONDS):
        while True:
            await _advance_public_action_recovery_once(harness, ordinal=step)
            check = harness.build()
            try:
                current = check.export_replay_evidence()
                actions = {
                    item.action_id: item
                    for item in current.projection.actions
                    if item.action_id in action_ids
                }
            finally:
                await check.aclose()
            assert set(actions) == action_ids
            if all(item.state == "delivered" for item in actions.values()):
                return
            step += 1
            await asyncio.sleep(0)


async def _drain_reconsideration_until_terminal(
    harness: _PublicHostHarness,
) -> tuple[object, ...]:
    """Restart through public background drains until the event-triggered gate terminates."""

    async with asyncio.timeout(HOST_LIFECYCLE_TIMEOUT_SECONDS):
        while True:
            host = harness.build()
            try:
                await host.drain(max_action_units=0, max_background_units=8)
                evidence = host.export_replay_evidence()
            finally:
                await host.aclose()
            gates = tuple(
                item
                for item in evidence.projection.trigger_processes
                if item.process_kind == "expression_reconsideration"
            )
            if gates and all(item.state == "terminal" for item in gates):
                return gates
            await asyncio.sleep(0)


async def _qualify_multibeat_due(tmp_path: Path) -> dict[str, Any]:
    scenario = HOST_QUALIFICATION_DECLARATIONS["scenarios"]["expression.multibeat_due"]
    harness = _PublicHostHarness(
        tmp_path=tmp_path,
        database_name="public-host-multibeat.sqlite",
    )
    first = harness.build()
    try:
        outcome = await first.inbound_text(
            message_id="message:multibeat-later",
            recipient_id="10001",
            text="忙完后分两条告诉我。",
            observed_at=harness.started_at,
        )
        created = first.export_replay_evidence()
        actions = tuple(created.projection.actions)
        assert outcome.status == "deferred"
        assert len(actions) == 2
        assert len({item.expression_plan_id for item in actions}) == 1
        due_at = harness.started_at + timedelta(seconds=scenario["delay_seconds"])
        expires_at = harness.started_at + timedelta(seconds=scenario["expires_after_seconds"])
        assert all(item.not_before == due_at for item in actions)
        assert all(item.expires_at == expires_at for item in actions)
        first_action, second_action = sorted(actions, key=lambda item: len(item.dependencies))
        assert first_action.dependencies == ()
        assert second_action.dependencies == (first_action.action_id,)
        beats = tuple(
            item
            for item in created.projection.expression_beats
            if item.plan_id == first_action.expression_plan_id
        )
        assert len(beats) == 2
        assert all(item.not_before == due_at and item.expires_at == expires_at for item in beats)
        beats_by_action = {item.action_id: item for item in beats}
        assert beats_by_action[first_action.action_id].dependency_beat_ids == ()
        assert beats_by_action[second_action.action_id].dependency_beat_ids == (
            beats_by_action[first_action.action_id].beat_id,
        )
    finally:
        await first.aclose()

    before_due_host = harness.build()
    try:
        rebuilt = before_due_host.export_replay_evidence()
        before_due = due_at - timedelta(microseconds=1)
        await before_due_host.tick(
            tick_id="host-qualification-multibeat-before-due",
            logical_time_from=rebuilt.projection.logical_time,
            logical_time_to=before_due,
            observed_at=before_due,
            reason="host_qualification_multibeat_before_due",
            run_life_ecology=False,
        )
        await before_due_host.drain(max_action_units=8, max_background_units=0)
        assert harness.delivery.texts == []
    finally:
        await before_due_host.aclose()

    action_ids = {item.action_id for item in actions}
    await _recover_actions_until_delivered(harness, action_ids=action_ids)

    duplicate_guard = harness.build()
    try:
        await duplicate_guard.drain(max_action_units=8, max_background_units=0)
        final = duplicate_guard.export_replay_evidence()
    finally:
        await duplicate_guard.aclose()
    cold = harness.build()
    try:
        replayed = cold.export_replay_evidence()
    finally:
        await cold.aclose()

    plan_id = actions[0].expression_plan_id
    plan = next(item for item in replayed.projection.expression_plans if item.plan_id == plan_id)
    receipts = tuple(
        item
        for item in replayed.projection.execution_receipts
        if item.action_id in action_ids and item.is_terminal
    )
    terminal_receipt_counts = {
        action_id: sum(item.action_id == action_id for item in receipts)
        for action_id in action_ids
    }
    assert set(item.action_id for item in receipts) == action_ids
    assert all(count == 1 for count in terminal_receipt_counts.values())
    return {
        "scenario_id": "expression.multibeat_due",
        "qualification_layer": HOST_QUALIFICATION_DECLARATIONS["qualification_layer"],
        "excluded_evidence": HOST_QUALIFICATION_DECLARATIONS["excluded_evidence"],
        "visible_texts": harness.delivery.texts,
        "terminal_action_states": [
            item.state for item in replayed.projection.actions if item.action_id in action_ids
        ],
        "terminal_receipt_counts": terminal_receipt_counts,
        "expression_plan_state": plan.state,
        "cold_replay_hash_matches": (
            final.projection.semantic_hash
            == final.replay.semantic_hash
            == replayed.projection.semantic_hash
            == replayed.replay.semantic_hash
        ),
    }


async def _qualify_interjection_reconsideration(tmp_path: Path) -> dict[str, Any]:
    harness = _PublicHostHarness(
        tmp_path=tmp_path,
        database_name="public-host-interjection.sqlite",
    )
    first = harness.build()
    try:
        opened = await first.inbound_text(
            message_id="message:interjection-old-plan",
            recipient_id="10001",
            text="忙完后分两条告诉我。",
            observed_at=harness.started_at,
        )
        initial = first.export_replay_evidence()
        old_actions = tuple(initial.projection.actions)
        assert opened.status == "deferred"
        assert len(old_actions) == 2

        interjected_at = harness.started_at + timedelta(seconds=1)
        harness.pacing_clock["now"] = interjected_at
        interjected = await first.inbound_text(
            message_id="message:interjection-cancel",
            recipient_id="10001",
            text="等等，刚才那两条不用再发了。",
            observed_at=interjected_at,
        )
        gated = first.export_replay_evidence()
        gates = tuple(
            item
            for item in gated.projection.trigger_processes
            if item.process_kind == "expression_reconsideration"
        )
        assert interjected.status == "observed_only"
        assert gates and any(item.state != "terminal" for item in gates)
    finally:
        await first.aclose()

    gates = await _drain_reconsideration_until_terminal(harness)

    duplicate_guard = harness.build()
    try:
        await duplicate_guard.drain(max_action_units=8, max_background_units=8)
        final = duplicate_guard.export_replay_evidence()
    finally:
        await duplicate_guard.aclose()
    cold = harness.build()
    try:
        replayed = cold.export_replay_evidence()
    finally:
        await cold.aclose()

    old_action_ids = {item.action_id for item in old_actions}
    plan_id = old_actions[0].expression_plan_id
    plan = next(item for item in replayed.projection.expression_plans if item.plan_id == plan_id)
    receipts = tuple(
        item
        for item in replayed.projection.execution_receipts
        if item.action_id in old_action_ids
    )
    return {
        "scenario_id": "expression.interjection_reconsideration",
        "qualification_layer": HOST_QUALIFICATION_DECLARATIONS["qualification_layer"],
        "excluded_evidence": HOST_QUALIFICATION_DECLARATIONS["excluded_evidence"],
        "visible_texts": harness.delivery.texts,
        "action_states": [
            item.state for item in replayed.projection.actions if item.action_id in old_action_ids
        ],
        "expression_plan_state": plan.state,
        "reconsideration_terminal": all(item.state == "terminal" for item in gates),
        "reconsideration_model_calls": harness.model.reconsideration_calls,
        "transport_receipt_count": len(receipts),
        "cold_replay_hash_matches": (
            final.projection.semantic_hash
            == final.replay.semantic_hash
            == replayed.projection.semantic_hash
            == replayed.replay.semantic_hash
        ),
    }

@pytest.mark.asyncio
async def test_public_host_multibeat_due_survives_restart_and_settles_each_beat_once(
    tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    _host_scenario(
        "expression.multibeat_due",
        request.node.nodeid,
        mechanism_ids=("expression.multibeat", "action.authorized_due"),
    )
    evidence = await _qualify_multibeat_due(tmp_path)

    assert evidence["scenario_id"] == "expression.multibeat_due"
    assert evidence["qualification_layer"] == "accepted_typed_plan_host_lifecycle"
    assert HOST_QUALIFICATION_DECLARATIONS["fixture_authority"] == (
        "scripted_legal_role_outcomes_open_downstream_only"
    )
    assert HOST_QUALIFICATION_DECLARATIONS["public_seams"] == (
        "QQC2CHost.inbound_text",
        "QQC2CHost.tick",
        "QQC2CHost.drain",
        "QQC2CHost.export_replay_evidence",
        "QQC2CHost.aclose",
    )
    assert evidence["excluded_evidence"] == (
        "production_stream_expression_episode",
        "real_provider_author_transport",
        "character_autonomy",
    )
    assert HOST_QUALIFICATION_DECLARATIONS["scenarios"][evidence["scenario_id"]] == {
        "catalog_mechanism_id": "expression.multibeat",
        "variant": "ordered_later_text_beats",
        "receipt_scope": "qq_transport_terminal",
        "delay_seconds": 60,
        "expires_after_seconds": 600,
    }
    assert evidence["visible_texts"] == ["第一条晚点说。", "第二条也晚点说。"]
    assert evidence["terminal_action_states"] == ["delivered", "delivered"]
    assert set(evidence["terminal_receipt_counts"].values()) == {1}
    assert len(evidence["terminal_receipt_counts"]) == 2
    assert evidence["expression_plan_state"] == "completed"
    assert evidence["cold_replay_hash_matches"] is True


@pytest.mark.asyncio
async def test_public_host_interjection_reconsideration_cancels_unsent_plan_effect_once(
    tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    _host_scenario(
        "expression.interjection_reconsideration",
        request.node.nodeid,
        mechanism_ids=("expression.reconsideration",),
    )
    evidence = await _qualify_interjection_reconsideration(tmp_path)

    assert evidence["scenario_id"] == "expression.interjection_reconsideration"
    assert evidence["qualification_layer"] == "accepted_typed_plan_host_lifecycle"
    assert evidence["excluded_evidence"] == (
        "production_stream_expression_episode",
        "real_provider_author_transport",
        "character_autonomy",
    )
    assert HOST_QUALIFICATION_DECLARATIONS["scenarios"][evidence["scenario_id"]] == {
        "catalog_mechanism_id": "expression.reconsideration",
        "variant": "interjection_cancel",
        "receipt_scope": "local_cancellation_has_no_transport_receipt",
    }
    assert evidence["visible_texts"] == []
    assert evidence["action_states"] == ["cancelled", "cancelled"]
    assert evidence["expression_plan_state"] == "terminated"
    assert evidence["reconsideration_terminal"] is True
    assert evidence["reconsideration_model_calls"] == 1
    assert evidence["transport_receipt_count"] == 0
    assert evidence["cold_replay_hash_matches"] is True

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import hashlib
import importlib.util
import json
from pathlib import Path
import stat
from types import SimpleNamespace

import pytest

from companion_daemon.config import Settings
from companion_daemon.llm import FailoverChatModel, FakeCompanionModel
from companion_daemon.world_v2.deliberation import ModelUsageProvenance
from companion_daemon.world_v2.errors import ConcurrencyConflict
from companion_daemon.world_v2.interactive_turn_budget import InteractiveTurnBudgetPolicy
from companion_daemon.world_v2.isolated_source_closure_trace import (
    BoundedSourceClosureTraceCollector,
    capture_isolated_source_closure_trace,
    emit_source_closure_trace,
)
from companion_daemon.world_v2.qq_c2c_host import build_qq_c2c_host


_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "run_private_self_expression_audit.py"
_SPEC = importlib.util.spec_from_file_location(
    "run_private_self_expression_audit",
    _SCRIPT,
)
assert _SPEC is not None and _SPEC.loader is not None
_RUNNER = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_RUNNER)


NOW = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)
_STRESS_FIXTURE = (
    Path(__file__).with_name("fixtures") / "private_self_expression_interaction_stress.json"
)
_NATURAL_FIXTURE = Path(__file__).with_name("fixtures") / "private_self_expression_natural.json"


def test_interaction_fixture_declares_real_burst_and_generation_overlap() -> None:
    scenario = _RUNNER.load_private_self_expression_scenario(_STRESS_FIXTURE)

    burst, opening, interjection, _ = scenario.turns
    assert [fragment.text for fragment in burst.fragments] == [
        "我刚到家",
        "路上被雨淋了一截",
        "鞋子现在还是湿的",
    ]
    assert burst.text == "\n".join(fragment.text for fragment in burst.fragments)
    assert [
        scenario.source_event_id_for_fragment(burst, index) for index in range(len(burst.fragments))
    ] == [
        scenario.source_event_id(burst),
        scenario.source_event_id(burst) + ":fragment:rain",
        scenario.source_event_id(burst) + ":fragment:shoes",
    ]
    assert opening.overlap_group == interjection.overlap_group
    assert (opening.launch_offset_ms, interjection.launch_offset_ms) == (0, 650)


def test_overlap_runtime_evidence_is_attributed_by_source_action_and_trace() -> None:
    """A later winning turn must not donate its provider evidence to an earlier task."""

    class _CapturedDelivery:
        sent = [
            {
                "recipient_id": "audit-recipient",
                "message_id": "provider-message:I02",
                "modality": "text",
                "content": "后半句赢了",
            }
        ]

        @staticmethod
        def observed_ns_for_message(message_id: str) -> int | None:
            return 7_500_000_000 if message_id == "provider-message:I02" else None

    rows = [
        {
            "turn_id": "I01",
            "source_event_id": "source:I01",
            "deliveries": list(_CapturedDelivery.sent),
            "first_visible_reply_wall_ms": 7_491.1,
            "first_visible_reply_measurement": (
                "process_monotonic_to_isolated_provider_acceptance"
            ),
            "latency_segments": [{"trace_id": "trace:wrongly-sliced"}],
        },
        {
            "turn_id": "I02",
            "source_event_id": "source:I02",
            "deliveries": [],
            "first_visible_reply_wall_ms": None,
            "first_visible_reply_measurement": "not_observed",
            "latency_segments": [],
        },
    ]
    immutable = {
        "turns": [
            {
                "turn_id": "I01",
                "source_event_id": "source:I01",
                "observation_id": "observation:qq:audit-recipient:qq-coalesced:batch-I01",
                "actions": [],
                "receipts": [],
            },
            {
                "turn_id": "I02",
                "source_event_id": "source:I02",
                "observation_id": "observation:qq:audit-recipient:qq-coalesced:batch-I02",
                "actions": [{"action_id": "action:I02"}],
                "receipts": [
                    {
                        "action_id": "action:I02",
                        "observed_state": "provider_accepted",
                        "provider_ref": "platform:message_id:provider-message:I02",
                    }
                ],
            },
        ]
    }
    samples = (
        SimpleNamespace(
            trace_id="trace:qq-c2c-v2:audit-recipient:qq-ingress-batch:batch-I02",
            startup="hot",
            environment="real_transport",
            segment="model_completion",
            duration_ms=6_800.0,
        ),
        SimpleNamespace(
            trace_id="trace:qq-c2c-v2:audit-recipient:qq-ingress-batch:batch-I01",
            startup="hot",
            environment="real_transport",
            segment="model_completion",
            duration_ms=5_200.0,
        ),
    )

    _RUNNER._attribute_runtime_turn_evidence(
        runtime_turns=rows,
        immutable_replay_audit=immutable,
        delivery=_CapturedDelivery(),
        latency_samples=samples,
        started_ns_by_turn={"I01": 0, "I02": 650_000_000},
        fast_pacing=True,
    )

    assert rows[0]["deliveries"] == []
    assert rows[0]["first_visible_reply_wall_ms"] is None
    assert rows[0]["first_visible_reply_measurement"] == "not_observed"
    assert [item["trace_id"] for item in rows[0]["latency_segments"]] == [
        "trace:qq-c2c-v2:audit-recipient:qq-ingress-batch:batch-I01"
    ]
    assert rows[1]["deliveries"] == list(_CapturedDelivery.sent)
    assert rows[1]["first_visible_reply_wall_ms"] == 6_850.0
    assert rows[1]["first_visible_reply_measurement"] == (
        "process_monotonic_to_isolated_provider_acceptance"
    )
    assert [item["trace_id"] for item in rows[1]["latency_segments"]] == [
        "trace:qq-c2c-v2:audit-recipient:qq-ingress-batch:batch-I02"
    ]


def test_natural_fixture_does_not_prompt_the_character_about_questioning_style() -> None:
    raw = _NATURAL_FIXTURE.read_text(encoding="utf-8")
    scenario = _RUNNER.load_private_self_expression_scenario(_NATURAL_FIXTURE)

    assert len(scenario.turns) >= 5
    assert all(
        phrase not in raw for phrase in ("别提问", "不要提问", "只顾着问", "像客服", "好奇宝宝")
    )


class _DelayedSupersessionModel:
    """Hold one valid turn in-provider, then expose the next pinned model input."""

    def __init__(
        self,
        *,
        model: str,
        started: asyncio.Event,
        release: asyncio.Event,
    ) -> None:
        self.model = model
        self.started = started
        self.release = release
        self.materials: list[dict[str, object]] = []
        self._fallback = FakeCompanionModel()

    @staticmethod
    def _material(messages: list[dict[str, str]]) -> dict[str, object]:
        for message in messages:
            try:
                value = json.loads(message["content"])
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(value, dict) and isinstance(value.get("current_trigger_message"), dict):
                return value
        return {}

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.8,
    ) -> str:
        joined = "\n".join(message["content"] for message in messages)
        combined = (
            "appraisal_draft and expression_draft" in joined
            and "COMBINED OUTPUT ENVELOPE" in joined
        )
        expression_call = (
            combined
            or "Return one raw JSON ExpressionDraft" in joined
            or "raw JSON ExpressionDraft only" in joined
        )
        if not expression_call:
            return await self._fallback.complete(messages, temperature=temperature)
        material = self._material(messages)
        if material:
            self.materials.append(material)
        trigger = material.get("current_trigger_message", {})
        trigger_text = trigger.get("text") if isinstance(trigger, dict) else None
        observation_ref = trigger.get("observation_ref") if isinstance(trigger, dict) else None
        delayed = trigger_text == "我刚想起来还有件事，先说前半句。"
        if delayed:
            self.started.set()
            await self.release.wait()
        expression = {
            "private_turn_state": {
                "contract": "private-turn-state.1",
                "inner_state_summary": (
                    "这句本来可以正常回复，但新消息到达后应废止旧回复。"
                    if delayed
                    else "前后两句现在都在，我只回答最新的完整进展。"
                ),
                "attended_source_refs": [observation_ref],
            },
            "timing_choice": "now",
            "cadence": "conversational",
            "beats": [
                {
                    "modality": "text",
                    "text": "前后两句我都看到了，这次只接现在这条。",
                }
            ],
            "stance": "present",
            "brief_rationale": "Use the complete pinned conversation.",
            "confidence": 8_000,
            "world_claims": [],
        }
        if not combined:
            return json.dumps(expression, ensure_ascii=False)
        return json.dumps(
            {
                "appraisal_draft": {
                    "appraise": False,
                    "affect": "no_change",
                    "brief_rationale": "No durable affect transition is needed.",
                    "behavior_tendency": "choose_own_response",
                    "stance": "self_directed",
                    "display_strategy": "model_owned",
                    "confidence": 7_000,
                },
                "expression_draft": expression,
            },
            ensure_ascii=False,
        )


class _InterruptibleMultiBeatAuditModel:
    """Emit one role-authored stream whose unsent tail can lose attention."""

    model = "fixture:private-self-audit-interruptible-stream"
    reports_exact_request_emission = True

    def __init__(self) -> None:
        self._fallback = FakeCompanionModel()
        self.stream_calls = 0
        self.first_tail_release = asyncio.Event()
        self.cancelled_stream_ordinals: list[int] = []

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.8,
    ) -> str:
        return await self._fallback.complete(messages, temperature=temperature)

    async def complete_json_stream_with_usage(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.8,
        on_text_delta=None,  # type: ignore[no-untyped-def]
    ) -> tuple[str, ModelUsageProvenance]:
        del temperature
        self.stream_calls += 1
        ordinal = self.stream_calls
        material = _DelayedSupersessionModel._material(messages)
        trigger = material.get("current_trigger_message", {})
        trigger_text = trigger.get("text") if isinstance(trigger, dict) else None
        observation_ref = trigger.get("observation_ref") if isinstance(trigger, dict) else None
        first_turn = trigger_text == ("我刚到家\n路上被雨淋了一截\n鞋子现在还是湿的")
        head_text = "第一条先接住。" if first_turn else "新消息到了，我就先接这句。"
        events: list[dict[str, object]] = [
            {
                "type": "head",
                "private_turn_state": {
                    "contract": "private-turn-state.1",
                    "inner_state_summary": (
                        "我想分三条说，但后两条仍应接受新消息打断。"
                        if first_turn
                        else "新消息改变了当下节奏，我只接这次的新内容。"
                    ),
                    "attended_source_refs": (
                        [observation_ref] if isinstance(observation_ref, str) else []
                    ),
                },
                "timing_choice": "now",
                "beat": {"modality": "text", "text": head_text},
                "cadence": "rapid",
                "stance": "present",
                "brief_rationale": "Use one role-authored interruptible stream.",
                "confidence": 8_000,
                "world_claims": [],
            }
        ]
        if first_turn:
            events.extend(
                [
                    {
                        "type": "beat",
                        "beat": {
                            "modality": "text",
                            "text": "这是不该机械补发的旧尾巴二。",
                        },
                        "world_claims": [],
                    },
                    {
                        "type": "beat",
                        "beat": {
                            "modality": "text",
                            "text": "这是不该机械补发的旧尾巴三。",
                        },
                        "world_claims": [],
                    },
                ]
            )
        events.append({"type": "end"})
        raw = json.dumps(
            {
                "protocol": "character-interior-events.1",
                "appraisal_draft": {
                    "appraise": False,
                    "affect": "no_change",
                    "brief_rationale": "No durable affect transition is needed.",
                    "behavior_tendency": "choose_own_response",
                    "stance": "self_directed",
                    "display_strategy": "model_owned",
                    "confidence": 7_000,
                },
                "events": events,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        if on_text_delta is not None:
            if first_turn:
                boundary = raw.index(',{"type":"beat"')
                on_text_delta(raw[:boundary])
                try:
                    await self.first_tail_release.wait()
                except asyncio.CancelledError:
                    self.cancelled_stream_ordinals.append(ordinal)
                    raise
                on_text_delta(raw[boundary:])
            else:
                on_text_delta(raw)
        usage = {
            "usage_contract": "model-usage.1",
            "route_class": "chat",
            "input_tokens": 10,
            "output_tokens": 10,
            "thinking_tokens": 0,
            "token_provenance": "provider_reported",
            "transport": "provider_api",
            "provider": "fixture",
            "provider_usage_ref": f"usage:fixture:audit-stream:{ordinal}",
        }
        usage_hash = hashlib.sha256(
            json.dumps(usage, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return raw, ModelUsageProvenance(**usage, provider_usage_hash=usage_hash)


@pytest.mark.asyncio
async def test_overlapped_inbound_supersedes_unanswered_generation_and_keeps_context(
    tmp_path: Path,
) -> None:
    scenario = _RUNNER.load_private_self_expression_scenario(_STRESS_FIXTURE)
    turns = scenario.turns[1:3]
    started = asyncio.Event()
    release = asyncio.Event()
    primary = _DelayedSupersessionModel(
        model="fixture:audit-interrupt-primary",
        started=started,
        release=release,
    )
    recovery = _DelayedSupersessionModel(
        model="fixture:audit-interrupt-recovery",
        started=started,
        release=release,
    )
    delivery = _RUNNER.IsolatedAuditDelivery(run_namespace="interrupt")
    clock = _RUNNER._VirtualPacingClock(NOW)
    host = build_qq_c2c_host(
        settings=Settings(
            database_path=tmp_path / "interrupt-audit.sqlite",
            PRIMARY_USER_ID="geoff",
            WORLD_V2_TEXT_ENDPOINT_ENABLED=False,
            WORLD_V2_EXPRESSION_EPISODE_MODE="off",
        ),
        recipient_id="10001",
        bootstrap_at=NOW,
        model=FailoverChatModel(
            primary=primary,
            fallback=recovery,
            implicit_failover=False,
        ),
        world_support_model=FakeCompanionModel(),
        delivery=delivery,
        ingress_now=clock.now,
        ingress_sleep=clock.sleep,
        action_due_now=clock.now,
        interactive_turn_budget_policy=InteractiveTurnBudgetPolicy(
            total_seconds=3.5,
            hedge_after_seconds=0.01,
            acceptance_dispatch_reserve_seconds=0.3,
            wall_clock=clock.now,
        ),
        use_configured_recall_embedding=False,
    )
    second_observation_committed_before_release = False
    try:
        running = asyncio.create_task(
            _RUNNER._submit_scenario_overlap_group(
                host=host,
                scenario=scenario,
                turns=turns,
                recipient_id="10001",
                conversation_started_at=NOW,
                clock=clock,
                fast_pacing=True,
            )
        )
        await asyncio.wait_for(started.wait(), timeout=5)
        try:
            async with asyncio.timeout(2):
                while True:
                    in_flight = host.export_replay_evidence()
                    if len(in_flight.projection.message_observations) >= 2:
                        second_observation_committed_before_release = True
                        break
                    await asyncio.sleep(0)
        finally:
            # The assertion below must not strand the real valid provider call.
            # Let it return so the test can also prove its now-stale candidate
            # neither authorizes nor dispatches an old reply.
            release.set()
        executions = await asyncio.wait_for(running, timeout=10)
        evidence = host.export_replay_evidence()
    finally:
        release.set()
        await host.aclose()

    assert second_observation_committed_before_release
    assert [item["status"] for item in executions] == [
        "observed_only",
        "action_authorized",
    ]
    first_observation, second_observation = evidence.projection.message_observations
    first_episode = next(
        item
        for item in evidence.projection.trigger_processes
        if item.process_kind == "expression_episode"
        and item.source_evidence_ref == first_observation.observation_id
    )
    assert first_episode.state == "terminal"
    assert first_episode.runtime_outcome_ref == "expression-episode:superseded-by-newer-inbound"
    second_material = next(
        material
        for material in (*primary.materials, *recovery.materials)
        if material["current_trigger_message"]["text"] == turns[1].text
    )
    assert turns[0].text in json.dumps(second_material, ensure_ascii=False)
    assert turns[1].text in json.dumps(second_material, ensure_ascii=False)
    assert [item["content"] for item in delivery.sent if item["modality"] == "text"] == [
        "前后两句我都看到了，这次只接现在这条。"
    ]
    assert second_observation.observation_id != first_observation.observation_id


@pytest.mark.asyncio
async def test_new_inbound_cancels_unsent_multi_beat_tail_without_repeating_it(
    tmp_path: Path,
) -> None:
    scenario = _RUNNER.load_private_self_expression_scenario(_STRESS_FIXTURE)
    opening = scenario.turns[0]
    interjection = scenario.turns[-1]
    clock = _RUNNER._VirtualPacingClock(NOW)
    delivery = _RUNNER.IsolatedAuditDelivery(run_namespace="tail")
    model = _InterruptibleMultiBeatAuditModel()

    async def dormant_action_due_sleep(_seconds: float) -> None:
        await asyncio.Event().wait()

    host = build_qq_c2c_host(
        settings=Settings(
            database_path=tmp_path / "tail-audit.sqlite",
            PRIMARY_USER_ID="geoff",
            WORLD_V2_TEXT_ENDPOINT_ENABLED=False,
            WORLD_V2_EXPRESSION_EPISODE_MODE="stream",
            WORLD_V2_RECORDED_CADENCE_MODE="shadow",
        ),
        recipient_id="10001",
        bootstrap_at=NOW,
        model=model,
        world_support_model=FakeCompanionModel(),
        delivery=delivery,
        ingress_now=clock.now,
        ingress_sleep=clock.sleep,
        action_due_now=clock.now,
        action_due_sleep=dormant_action_due_sleep,
        interactive_turn_budget_policy=InteractiveTurnBudgetPolicy(
            wall_clock=clock.now,
        ),
        use_configured_recall_embedding=False,
    )
    first_task: asyncio.Task[dict[str, object]] | None = None
    try:
        first_task = asyncio.create_task(
            _RUNNER._submit_scenario_turn(
                host=host,
                scenario=scenario,
                turn=opening,
                recipient_id="10001",
                conversation_started_at=NOW,
                clock=clock,
                fast_pacing=True,
            )
        )
        async with asyncio.timeout(5):
            while True:
                before_interjection = host.export_replay_evidence()
                visible = [
                    item["content"]
                    for item in delivery.sent
                    if item["modality"] == "text"
                ]
                if visible == ["第一条先接住。"]:
                    break
                await asyncio.sleep(0)
        second = await _RUNNER._submit_scenario_turn(
            host=host,
            scenario=scenario,
            turn=interjection,
            recipient_id="10001",
            conversation_started_at=NOW,
            clock=clock,
            fast_pacing=True,
        )
        first = await asyncio.wait_for(first_task, timeout=5)
        await host.drain(max_action_units=16, max_background_units=0)
        final = host.export_replay_evidence()
    finally:
        model.first_tail_release.set()
        if first_task is not None and not first_task.done():
            first_task.cancel()
            await asyncio.gather(first_task, return_exceptions=True)
        await host.aclose()

    assert first["status"] == second["status"] == "action_authorized"
    first_plan = next(
        plan
        for plan in before_interjection.projection.expression_plans
        if any(
            action.expression_plan_id == plan.plan_id
            for action in before_interjection.projection.actions
        )
    )
    first_actions_before = [
        action
        for action in before_interjection.projection.actions
        if action.expression_plan_id == first_plan.plan_id
    ]
    # Only the already-visible head becomes a durable Action. The unobserved
    # tail is cancelled at the one-author stream boundary, so there is no old
    # provisional/full plan to reconsider and no stale Action to clean up.
    assert len(first_actions_before) == 1
    assert model.stream_calls == 2
    assert model.cancelled_stream_ordinals == [1]
    first_observation = before_interjection.projection.message_observations[0]
    first_episode = next(
        item
        for item in final.projection.trigger_processes
        if item.process_kind == "expression_episode"
        and item.source_evidence_ref == first_observation.observation_id
    )
    assert first_episode.state == "terminal"
    assert "append" not in (first_episode.runtime_outcome_ref or "")
    visible = [item["content"] for item in delivery.sent if item["modality"] == "text"]
    assert visible == [
        "第一条先接住。",
        "新消息到了，我就先接这句。",
    ]
    assert all("旧尾巴" not in text for text in visible)
    assert all(
        "旧尾巴" not in (beat.text or "")
        for manifest in final.projection.expression_plan_manifests
        for beat in manifest.beats
    )
    first_actions_final = tuple(
        action
        for action in final.projection.actions
        if action.expression_plan_id == first_plan.plan_id
    )
    first_manifests_final = tuple(
        manifest
        for manifest in final.projection.expression_plan_manifests
        if manifest.plan_id == first_plan.plan_id
    )
    assert tuple(action.action_id for action in first_actions_final) == tuple(
        action.action_id for action in first_actions_before
    )
    assert first_manifests_final
    assert all(len(manifest.beats) == 1 for manifest in first_manifests_final)
    assert all(
        action.expression_plan_id != first_plan.plan_id or action.state != "cancelled"
        for action in final.projection.actions
    )


@pytest.mark.asyncio
async def test_runner_burst_uses_real_host_coalescing_and_one_world_turn(
    tmp_path: Path,
) -> None:
    scenario = _RUNNER.load_private_self_expression_scenario(_STRESS_FIXTURE)
    turn = scenario.turns[0]
    clock = _RUNNER._VirtualPacingClock(NOW)
    delivery = _RUNNER.IsolatedAuditDelivery(run_namespace="burst")
    model = _ImmediateReplyModel()
    host = build_qq_c2c_host(
        settings=Settings(
            database_path=tmp_path / "burst-audit.sqlite",
            PRIMARY_USER_ID="geoff",
            WORLD_V2_TEXT_ENDPOINT_ENABLED=False,
            WORLD_V2_EXPRESSION_EPISODE_MODE="off",
        ),
        recipient_id="10001",
        bootstrap_at=NOW,
        model=model,
        world_support_model=FakeCompanionModel(),
        delivery=delivery,
        ingress_now=clock.now,
        ingress_sleep=clock.sleep,
        action_due_now=clock.now,
        interactive_turn_budget_policy=InteractiveTurnBudgetPolicy(
            wall_clock=clock.now,
        ),
        use_configured_recall_embedding=False,
    )
    try:
        execution = await _RUNNER._submit_scenario_turn(
            host=host,
            scenario=scenario,
            turn=turn,
            recipient_id="10001",
            conversation_started_at=NOW,
            clock=clock,
            fast_pacing=True,
        )
        evidence = host.export_replay_evidence()
    finally:
        await host.aclose()

    expected_source_ids = [
        scenario.source_event_id_for_fragment(turn, index) for index in range(len(turn.fragments))
    ]
    assert execution["status"] == "action_authorized"
    assert execution["source_event_ids"] == expected_source_ids
    assert len(evidence.projection.message_observations) == 1
    observation_event = next(
        item for item in evidence.events if item.event.event_type == "ObservationRecorded"
    )
    observation = json.loads(observation_event.event.payload_json)
    assert observation["coalescing_metadata"]["source_event_ids"] == expected_source_ids
    assert model.trigger_messages[-1]["text"] == turn.text


@pytest.mark.asyncio
async def test_isolated_delivery_provider_ids_are_namespaced_per_audit_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cloned ledger must not collide with an earlier audit's provider ids."""

    namespaces = iter(("run-one", "run-two"))
    monkeypatch.setattr(_RUNNER.secrets, "token_hex", lambda _size: next(namespaces))
    first_run = _RUNNER.IsolatedAuditDelivery()
    second_run = _RUNNER.IsolatedAuditDelivery()

    first = await first_run.send_text("10001", "第一轮")
    second = await second_run.send_text("10001", "第二轮")
    first_message_id = first["data"]["message_id"]
    second_message_id = second["data"]["message_id"]

    assert first_message_id != second_message_id
    assert first_message_id == "private-self-real-audit-run-one-1"
    assert second_message_id == "private-self-real-audit-run-two-1"
    assert (await first_run.get_message("10001", message_id=first_message_id))["status"] == "ok"
    assert (await second_run.get_message("10001", message_id=first_message_id))[
        "status"
    ] == "failed"


def _evidence(
    *,
    ledger_sequence: int,
    experience_count: int,
    plan_count: int,
    memory_candidate_count: int,
    new_events: tuple[tuple[object, ...], ...] = (),
    life_ecology_schedule: object | None = None,
) -> SimpleNamespace:
    def event_evidence(item: tuple[object, ...]) -> SimpleNamespace:
        sequence = int(item[0])
        event_type = str(item[1])
        event_id = str(item[2]) if len(item) >= 3 else f"event:{event_type}:{sequence}"
        payload = item[3] if len(item) >= 4 else {}
        return SimpleNamespace(
            cursor=SimpleNamespace(ledger_sequence=sequence),
            event=SimpleNamespace(
                event_id=event_id,
                event_type=event_type,
                payload_json=json.dumps(payload, ensure_ascii=False),
            ),
        )

    return SimpleNamespace(
        cursor=SimpleNamespace(ledger_sequence=ledger_sequence),
        projection=SimpleNamespace(
            experiences=tuple(object() for _ in range(experience_count)),
            plans=tuple(object() for _ in range(plan_count)),
            memory_candidates=tuple(object() for _ in range(memory_candidate_count)),
            life_ecology_schedule=life_ecology_schedule,
        ),
        events=tuple(event_evidence(item) for item in new_events),
    )


class _LifeEcologyAuditHost:
    def __init__(self) -> None:
        self.ticks: list[dict[str, object]] = []
        unit_one_events = (
            (5, "ClockAdvanced"),
            (
                6,
                "TriggerProcessOpened",
                "event:life-open:1",
                {
                    "process": {
                        "process_kind": "life_ecology",
                        "trigger_id": "trigger:life:1",
                    }
                },
            ),
            (
                7,
                "TriggerProcessCompleted",
                "event:life-complete:1",
                {
                    "trigger_id": "trigger:life:1",
                    "runtime_outcome_ref": (
                        "life-ecology:technical_failure."
                        "life_development.world_author_source_closure_rejected"
                    ),
                },
            ),
        )
        unit_two_events = (
            *unit_one_events,
            (8, "ClockAdvanced"),
            (
                9,
                "TriggerProcessOpened",
                "event:life-open:2",
                {
                    "process": {
                        "process_kind": "life_ecology",
                        "trigger_id": "trigger:life:2",
                    }
                },
            ),
            (
                10,
                "TriggerProcessCompleted",
                "event:life-complete:2",
                {
                    "trigger_id": "trigger:life:2",
                    "runtime_outcome_ref": "life-ecology:cooldown",
                },
            ),
        )
        self._exports = [
            _evidence(
                ledger_sequence=4,
                experience_count=0,
                plan_count=0,
                memory_candidate_count=0,
            ),
            _evidence(
                ledger_sequence=7,
                experience_count=0,
                plan_count=0,
                memory_candidate_count=0,
                new_events=unit_one_events,
            ),
            _evidence(
                ledger_sequence=10,
                experience_count=0,
                plan_count=0,
                memory_candidate_count=0,
                new_events=unit_two_events,
            ),
            _evidence(
                ledger_sequence=14,
                experience_count=1,
                plan_count=2,
                memory_candidate_count=1,
                new_events=(
                    *unit_two_events,
                    (11, "ClockAdvanced"),
                    (
                        12,
                        "TriggerProcessOpened",
                        "event:life-open:3",
                        {
                            "process": {
                                "process_kind": "life_ecology",
                                "trigger_id": "trigger:life:3",
                            }
                        },
                    ),
                    (13, "ExperienceCommitted"),
                    (
                        14,
                        "TriggerProcessCompleted",
                        "event:life-complete:3",
                        {
                            "trigger_id": "trigger:life:3",
                            "runtime_outcome_ref": (
                                "life-ecology:life_development_occurrence_committed"
                            ),
                        },
                    ),
                ),
            ),
        ]

    async def tick(self, **kwargs: object) -> str:
        self.ticks.append(kwargs)
        return "observed_only"

    def export_replay_evidence(self) -> SimpleNamespace:
        return self._exports.pop(0)


class _ProjectedLifeEcologyAuditHost:
    def __init__(self) -> None:
        self._exports = [
            _evidence(
                ledger_sequence=20,
                experience_count=0,
                plan_count=0,
                memory_candidate_count=0,
            ),
            _evidence(
                ledger_sequence=22,
                experience_count=0,
                plan_count=0,
                memory_candidate_count=0,
                new_events=((21, "ClockAdvanced"),),
                life_ecology_schedule=SimpleNamespace(
                    last_trigger_id="trigger:life:projected",
                    last_outcome_ref="life-ecology:life_development_no_op",
                    next_consideration_at=NOW + timedelta(hours=8),
                ),
            ),
        ]

    async def tick(self, **kwargs: object) -> str:
        del kwargs
        return "observed_only"

    def export_replay_evidence(self) -> SimpleNamespace:
        return self._exports.pop(0)


class _ClockOnlyLifeEcologyAuditHost:
    def __init__(self) -> None:
        self._exports = [
            _evidence(
                ledger_sequence=30,
                experience_count=0,
                plan_count=0,
                memory_candidate_count=0,
            ),
            _evidence(
                ledger_sequence=31,
                experience_count=0,
                plan_count=0,
                memory_candidate_count=0,
                new_events=((31, "ClockAdvanced"),),
            ),
        ]

    async def tick(self, **kwargs: object) -> str:
        del kwargs
        return "observed_only"

    def export_replay_evidence(self) -> SimpleNamespace:
        return self._exports.pop(0)


class _CadencedWorldAuthorAuditHost:
    """One real World Author consideration followed by one clock-only cooldown."""

    def __init__(self) -> None:
        first_events = (
            (41, "ClockAdvanced"),
            (
                42,
                "TriggerProcessOpened",
                "event:life-open:cadenced-1",
                {
                    "process": {
                        "process_kind": "life_ecology",
                        "trigger_id": "trigger:life:cadenced-1",
                    }
                },
            ),
            (
                43,
                "ModelResultRecorded",
                "event:life-model:world-author-1",
                {
                    "attempt_id": ("attempt:life-development:world_author:subject:epoch:1:retry:0"),
                },
            ),
            (
                44,
                "ProposalRecorded",
                "event:life-proposal:world-author-1",
                {
                    "proposal_kind": "life_development",
                    "model_role": "world_author",
                    "world_author_decision": "no_op",
                },
            ),
            (
                45,
                "RandomDrawRecorded",
                "event:random-draw:life-cadence-1",
                {
                    "selected_candidate_ref": "life-ecology-cadence-seconds:7200",
                },
            ),
            (
                46,
                "TriggerProcessCompleted",
                "event:life-complete:cadenced-1",
                {
                    "trigger_id": "trigger:life:cadenced-1",
                    "runtime_outcome_ref": "life-ecology:life_development_no_op",
                    "cadence_draw_event_ref": "event:random-draw:life-cadence-1",
                    "cadence_delay_seconds": 7200,
                },
            ),
        )
        second_events = (
            *first_events,
            (47, "ClockAdvanced"),
            (
                48,
                "TriggerProcessOpened",
                "event:life-open:cadenced-2",
                {
                    "process": {
                        "process_kind": "life_ecology",
                        "trigger_id": "trigger:life:cadenced-2",
                    }
                },
            ),
            (
                49,
                "TriggerProcessCompleted",
                "event:life-complete:cadenced-2",
                {
                    "trigger_id": "trigger:life:cadenced-2",
                    "runtime_outcome_ref": "life-ecology:cooldown",
                    "cadence_draw_event_ref": None,
                    "cadence_delay_seconds": None,
                },
            ),
        )
        due = NOW + timedelta(minutes=110)
        schedule = SimpleNamespace(
            last_trigger_id="trigger:life:cadenced-2",
            last_outcome_ref="life-ecology:cooldown",
            next_consideration_at=due,
        )
        self._exports = [
            _evidence(
                ledger_sequence=40,
                experience_count=0,
                plan_count=0,
                memory_candidate_count=0,
            ),
            _evidence(
                ledger_sequence=46,
                experience_count=0,
                plan_count=0,
                memory_candidate_count=0,
                new_events=first_events,
                life_ecology_schedule=schedule,
            ),
            _evidence(
                ledger_sequence=49,
                experience_count=0,
                plan_count=0,
                memory_candidate_count=0,
                new_events=second_events,
                life_ecology_schedule=schedule,
            ),
        ]

    async def tick(self, **kwargs: object) -> str:
        del kwargs
        return "observed_only"

    def export_replay_evidence(self) -> SimpleNamespace:
        return self._exports.pop(0)


class _TerminalReceiptAuditHost:
    def __init__(self) -> None:
        self.drains: list[dict[str, int]] = []
        self.scheduler_runs: list[dict[str, object]] = []

    async def drain(
        self,
        *,
        max_action_units: int,
        max_background_units: int,
    ) -> SimpleNamespace:
        self.drains.append(
            {
                "max_action_units": max_action_units,
                "max_background_units": max_background_units,
            }
        )
        return SimpleNamespace(action_statuses=("delivered",), background_statuses=())

    async def scheduler_once(
        self,
        *,
        observed_at: datetime,
        max_action_units: int,
        max_background_units: int,
    ) -> SimpleNamespace:
        self.scheduler_runs.append(
            {
                "observed_at": observed_at,
                "max_action_units": max_action_units,
                "max_background_units": max_background_units,
            }
        )
        return SimpleNamespace(action_statuses=("delivered",), background_statuses=())


class _ImmediateReplyModel:
    model = "fixture:private-self-audit-final-receipt"

    def __init__(self) -> None:
        self.trigger_messages: list[dict[str, object]] = []

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.8,
    ) -> str:
        del temperature
        material = json.loads(messages[1]["content"])
        trigger = material["current_trigger_message"]
        self.trigger_messages.append(trigger)
        observation_ref = str(trigger["observation_ref"])
        expression = {
            "private_turn_state": {
                "contract": "private-turn-state.1",
                "inner_state_summary": "我想接住她刚发来的这句话。",
                "attended_source_refs": [observation_ref],
            },
            "timing_choice": "now",
            "cadence": "conversational",
            "beats": [{"modality": "text", "text": "嗯，我在。"}],
            "stance": "present",
            "brief_rationale": "I chose a direct reply.",
            "confidence": 8_000,
            "world_claims": [],
        }
        if "appraisal_draft" not in messages[0]["content"]:
            return json.dumps(expression, ensure_ascii=False)
        return json.dumps(
            {
                "appraisal_draft": {
                    "appraise": False,
                    "affect": "no_change",
                    "brief_rationale": "No durable affect transition is needed.",
                    "behavior_tendency": "choose_own_response",
                    "stance": "self_directed",
                    "display_strategy": "model_owned",
                    "confidence": 7_000,
                },
                "expression_draft": expression,
            },
            ensure_ascii=False,
        )


class _RejectedThenCorrectedReplyModel:
    model = "fixture:private-self-audit-source-reselection"
    semantic_authority_id = "fixture-authority:private-self-audit-role-author"

    def __init__(self) -> None:
        self.calls: list[list[dict[str, str]]] = []
        self.observation_ref = ""

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.8,
    ) -> str:
        del temperature
        self.calls.append(messages)
        try:
            context = json.loads(messages[1]["content"])
        except (IndexError, TypeError, json.JSONDecodeError):
            context = {}
        trigger = context.get("current_trigger_message", {})
        if isinstance(trigger, dict) and isinstance(trigger.get("observation_ref"), str):
            self.observation_ref = trigger["observation_ref"]
        try:
            correction = json.loads(messages[-1]["content"])
        except (IndexError, TypeError, json.JSONDecodeError):
            correction = {}
        corrected = (
            isinstance(correction, dict)
            and correction.get("contract") == "source-closure-reselection.2"
        )
        expression = {
            "private_turn_state": {
                "contract": "private-turn-state.1",
                "inner_state_summary": (
                    "我现在只想接住她这句，不替自己补一段生活。"
                    if corrected
                    else "我刚才在宿舍翻书，看到她这句后想顺手接住。"
                ),
                "attended_source_refs": [self.observation_ref],
            },
            "timing_choice": "now",
            "cadence": "conversational",
            "beats": [
                {
                    "modality": "text",
                    "text": ("我看到你这句了。" if corrected else "刚才在宿舍翻书，现在看到你了。"),
                }
            ],
            "stance": "present",
            "brief_rationale": "Choose from the pinned turn.",
            "confidence": 8_000,
            "world_claims": [],
        }
        output_contract = (
            correction.get("output_contract")
            if isinstance(correction, dict)
            else None
        )
        if (
            corrected
            and isinstance(output_contract, dict)
            and output_contract.get("contract")
            == "expression-source-reselection-direct.1"
        ):
            # Re-selection is a newly authored realtime provider response, so
            # the fixture must exercise the negotiated strict transport wire
            # instead of relying on the looser canonical form retained only
            # for immutable historical replay.
            beat = expression["beats"][0]
            return json.dumps(
                {
                    "expression_draft": {
                        "private_turn_state": expression["private_turn_state"],
                        "timing_choice": expression["timing_choice"],
                        "cadence": expression["cadence"],
                        "beats": [
                            {
                                "modality": beat["modality"],
                                "text": beat["text"],
                                "reaction_id": None,
                                "sticker_id": None,
                            }
                        ],
                        "delay_position_bp": None,
                        "expires_after_seconds": None,
                        "stance": expression["stance"],
                        "brief_rationale": expression["brief_rationale"],
                        "impulse_summary": None,
                        "confidence": expression["confidence"],
                        "variation_profile": None,
                        "response_expectation": None,
                        "response_expectation_assessment": None,
                        "world_claims": expression["world_claims"],
                    },
                    "episode_disposition": None,
                },
                ensure_ascii=False,
            )
        if "COMBINED OUTPUT ENVELOPE" not in messages[0]["content"]:
            return json.dumps(expression, ensure_ascii=False)
        return json.dumps(
            {
                "appraisal_draft": {
                    "appraise": False,
                    "affect": "no_change",
                    "brief_rationale": "No durable affect transition is needed.",
                    "behavior_tendency": "choose_own_response",
                    "stance": "self_directed",
                    "display_strategy": "model_owned",
                    "confidence": 7_000,
                },
                "expression_draft": expression,
            },
            ensure_ascii=False,
        )


class _DormLifeSourceClosureReviewer:
    model = "fixture:private-self-audit-source-reviewer"
    semantic_authority_id = "fixture-authority:private-self-audit-source-reviewer"

    def __init__(self) -> None:
        self.calls: list[list[dict[str, str]]] = []

    def supports_strict_output_contract(self, contract: str) -> bool:
        return contract == "candidate-external-proposition-coverage.5"

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.8,
    ) -> str:
        del temperature
        self.calls.append(messages)
        request = json.loads(messages[1]["content"])
        coverage_contract = request.get("output_contract", {}).get("contract")
        if coverage_contract in {
            "candidate-external-proposition-coverage.2",
            "candidate-external-proposition-coverage.3",
            "candidate-external-proposition-coverage.4",
            "candidate-external-proposition-coverage.5",
        }:
            return json.dumps(
                {
                    "contract": coverage_contract,
                    "findings": [
                        {
                            "locator_index": locator["locator_index"],
                            "decision": (
                                "closed"
                                if locator["semantic_role"] == "immediate_private_state"
                                else "unclosed"
                            ),
                            "source_relation": (
                                "first_person_immediate_private_continuity"
                                if locator["semantic_role"] == "immediate_private_state"
                                else "unclosed"
                            ),
                            "source_ref_indexes": [],
                        }
                        for locator in request["review_locators"]
                    ],
                    **(
                        {
                            "inventory_complete": True,
                            "missing_findings": [],
                        }
                        if coverage_contract == "candidate-external-proposition-coverage.4"
                        else {"inventory_complete": True}
                        if coverage_contract
                        in {
                            "candidate-external-proposition-coverage.2",
                            "candidate-external-proposition-coverage.3",
                        }
                        else {}
                    ),
                },
                ensure_ascii=False,
            )
        report_relative_contract = request.get("output_contract", {}).get("contract")
        if report_relative_contract in {
            "report-relative-entailment-adjudication.2",
            "report-relative-entailment-adjudication.3",
        }:
            return json.dumps(
                {
                    "contract": report_relative_contract,
                    "findings": [
                        {
                            "finding_index": finding["finding_index"],
                            "decision": "retain_unclosed",
                            "failure_dimensions": ["added_external_premise"],
                            **(
                                {"source_refs": []}
                                if report_relative_contract
                                == "report-relative-entailment-adjudication.3"
                                else {}
                            ),
                        }
                        for finding in request["disputed_findings"]
                    ],
                    "r": "The companion activity is not entailed by the current report.",
                },
                ensure_ascii=False,
            )
        if request.get("output_contract", {}).get("contract") == "source-closure-appeal.4":
            return json.dumps(
                {
                    **request["rejected_categories"],
                    "r": "The unsupported dorm occurrence remains unsupported.",
                },
                ensure_ascii=False,
            )
        if (
            request.get("output_contract", {}).get("contract")
            == "candidate-external-proposition-coverage.1"
        ):
            return json.dumps(
                {
                    "contract": "candidate-external-proposition-coverage.1",
                    "findings": [
                        {
                            "locator": locator,
                            "decision": "unclosed",
                            "source_relation": "unclosed",
                            "source_refs": [],
                        }
                        for locator in request["locators"]
                    ],
                },
                ensure_ascii=False,
            )
        unsupported = "宿舍" in str(request.get("visible_text", "")) or "宿舍" in str(
            request.get("private_turn_state", "")
        )
        return json.dumps(
            {
                "ci": [],
                "v": ["undeclared_external_assertion"] if unsupported else [],
                "p": ["undeclared_external_assertion"] if unsupported else [],
                "visible_findings": (
                    [
                        {
                            "category": "undeclared_external_assertion",
                            "visible_span": "刚才在宿舍翻书",
                            "claim_index": None,
                            "source_relation": "unclosed",
                            "source_refs": [],
                        }
                    ]
                    if unsupported
                    else []
                ),
                "r": (
                    "The pinned turn does not prove a dorm reading occurrence."
                    if unsupported
                    else "The replacement adds no external occurrence."
                ),
            },
            ensure_ascii=False,
        )


class _DormCandidateExternalInventory:
    """Locate external propositions without consuming role-author fixtures."""

    model = "fixture:private-self-audit-candidate-inventory"
    semantic_authority_id = "fixture-authority:private-self-audit-candidate-inventory"

    def __init__(self) -> None:
        self.calls: list[list[dict[str, str]]] = []

    def supports_strict_output_contract(self, contract: str) -> bool:
        return contract == "candidate-external-proposition-inventory.5"

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.8,
    ) -> str:
        del temperature
        self.calls.append(messages)
        request = json.loads(messages[1]["content"])
        if (
            request.get("output_contract", {}).get("contract")
            == "candidate-epistemic-role-conflict.1"
        ):
            return json.dumps(
                {
                    "contract": "candidate-epistemic-role-conflict.1",
                    "findings": [
                        {
                            "locator_index": conflict["locator_index"],
                            "decision": "requires_source",
                        }
                        for conflict in request["conflicts"]
                    ],
                },
                ensure_ascii=False,
            )
        visible = request["visible_beats"]
        propositions: list[dict[str, object]] = []
        for beat in visible:
            text = beat["text"]
            if "刚才在宿舍翻书" in text:
                span = "刚才在宿舍翻书"
                start = text.index(span)
                propositions.append(
                    {
                        "locator": {
                            "beat_index": beat["beat_index"],
                            "char_start": start,
                            "char_end": start + len(span),
                            "text": span,
                        },
                        "semantic_role": "standalone_external_proposition",
                    }
                )
                continue
            propositions.append(
                {
                    "locator": {
                        "beat_index": beat["beat_index"],
                        "char_start": 0,
                        "char_end": len(text),
                        "text": text,
                    },
                    "semantic_role": "immediate_private_state",
                }
            )
        return json.dumps(
            {
                "contract": "candidate-external-proposition-inventory.5",
                "propositions": propositions,
            },
            ensure_ascii=False,
        )


class _NamedAdvisoryFake(FakeCompanionModel):
    """Give the proactive author lane an explicit, distinct provider identity."""

    model = "fixture:private-self-audit-advisory"
    semantic_authority_id = "fixture-authority:private-self-audit-advisory"


class _FastPacingAuditHost:
    def __init__(self, *, build_kwargs: dict[str, object]) -> None:
        self._build_kwargs = build_kwargs
        self.inbound_clock_samples: list[tuple[datetime, datetime, datetime]] = []
        self.scheduler_observed_at: list[datetime] = []

    def export_replay_evidence(self) -> SimpleNamespace:
        return _evidence(
            ledger_sequence=0,
            experience_count=0,
            plan_count=0,
            memory_candidate_count=0,
        )

    def proactive_source_authority_health(self) -> dict[str, object]:
        return {
            "status": "ready",
            "source_review_authority": {
                "last_winner_lane": "primary",
                "all_lanes_failed": 0,
            },
        }

    async def inbound_text(
        self,
        *,
        message_id: str,
        recipient_id: str,
        text: str,
        observed_at: datetime,
    ) -> SimpleNamespace:
        del message_id, recipient_id, text
        ingress_now = self._build_kwargs["ingress_now"]
        action_due_now = self._build_kwargs["action_due_now"]
        assert callable(ingress_now)
        assert callable(action_due_now)
        self.inbound_clock_samples.append((observed_at, ingress_now(), action_due_now()))
        emit_source_closure_trace(
            stage="post_appeal_initial_rejection",
            raw_candidate=json.dumps(
                {
                    "expression_draft": {
                        "private_turn_state": {
                            "inner_state_summary": "RUNNER_PRIVATE_STATE",
                            "attended_source_refs": ["RUNNER_CONTEXT_REF"],
                        },
                        "beats": [
                            {
                                "modality": "text",
                                "text": "runner rejected visible candidate",
                            }
                        ],
                    }
                }
            ),
            ci=(1,),
            v=("undeclared_external_assertion",),
            p=("temporal_authority_mismatch",),
        )
        return SimpleNamespace(status="observed_only")

    async def drain(
        self,
        *,
        max_action_units: int,
        max_background_units: int,
    ) -> SimpleNamespace:
        del max_action_units, max_background_units
        return SimpleNamespace(action_statuses=(), background_statuses=())

    async def scheduler_once(
        self,
        *,
        observed_at: datetime,
        max_action_units: int,
        max_background_units: int,
    ) -> SimpleNamespace:
        assert max_action_units == 64
        assert max_background_units == 0
        self.scheduler_observed_at.append(observed_at)
        return SimpleNamespace(action_statuses=(), background_statuses=())

    async def aclose(self) -> None:
        return None


class _PostInboundDrainFailureAuditHost(_FastPacingAuditHost):
    async def inbound_text(
        self,
        *,
        message_id: str,
        recipient_id: str,
        text: str,
        observed_at: datetime,
    ) -> SimpleNamespace:
        await super().inbound_text(
            message_id=message_id,
            recipient_id=recipient_id,
            text=text,
            observed_at=observed_at,
        )
        return SimpleNamespace(status="action_authorized")

    async def drain(
        self,
        *,
        max_action_units: int,
        max_background_units: int,
    ) -> SimpleNamespace:
        del max_action_units, max_background_units
        raise ConcurrencyConflict("stale deliberation revision")


@pytest.mark.asyncio
async def test_preconversation_life_ecology_uses_only_real_clock_opportunities() -> None:
    host = _LifeEcologyAuditHost()

    summary = await _RUNNER._run_preconversation_life_ecology(
        host=host,
        conversation_started_at=NOW,
        units=3,
    )

    assert _RUNNER._PRECONVERSATION_LIFE_ECOLOGY_UNIT == timedelta(minutes=10)
    assert len(host.ticks) == 3
    assert [(tick["logical_time_from"], tick["logical_time_to"]) for tick in host.ticks] == [
        (NOW - timedelta(minutes=30), NOW - timedelta(minutes=20)),
        (NOW - timedelta(minutes=20), NOW - timedelta(minutes=10)),
        (NOW - timedelta(minutes=10), NOW),
    ]
    assert all(tick["run_life_ecology"] is True for tick in host.ticks)
    assert all(
        tick["reason"] == "private_self_expression_audit_preconversation_life_ecology"
        for tick in host.ticks
    )
    assert summary == {
        "contract": "private-self-expression-preconversation-life-ecology.2",
        "requested_units": 3,
        "unit_seconds": 600,
        "world_started_at": "2026-07-30T11:30:00+00:00",
        "conversation_started_at": "2026-07-30T12:00:00+00:00",
        "tick_statuses": ["observed_only", "observed_only", "observed_only"],
        "tick_statuses_deprecated": True,
        "tick_statuses_semantics": "legacy_clock_status_only",
        "units": [
            {
                "ordinal": 1,
                "logical_time_from": "2026-07-30T11:30:00+00:00",
                "logical_time_to": "2026-07-30T11:40:00+00:00",
                "clock_status": "observed_only",
                "ecology_status": "technical_failure",
                "ecology_reason_code": ("life_development.world_author_source_closure_rejected"),
                "ecology_runtime_outcome_ref": (
                    "life-ecology:technical_failure."
                    "life_development.world_author_source_closure_rejected"
                ),
                "ecology_trigger_id": "trigger:life:1",
                "ecology_completion_event_ref": "event:life-complete:1",
                "life_model_attempt_counts_by_role": {},
                "world_author_decision": None,
                "cadence_draw_event_ref": None,
                "cadence_delay_seconds": None,
                "ledger_sequence_before": 4,
                "ledger_sequence_after": 7,
            },
            {
                "ordinal": 2,
                "logical_time_from": "2026-07-30T11:40:00+00:00",
                "logical_time_to": "2026-07-30T11:50:00+00:00",
                "clock_status": "observed_only",
                "ecology_status": "cooldown",
                "ecology_reason_code": "cooldown",
                "ecology_runtime_outcome_ref": "life-ecology:cooldown",
                "ecology_trigger_id": "trigger:life:2",
                "ecology_completion_event_ref": "event:life-complete:2",
                "life_model_attempt_counts_by_role": {},
                "world_author_decision": None,
                "cadence_draw_event_ref": None,
                "cadence_delay_seconds": None,
                "ledger_sequence_before": 7,
                "ledger_sequence_after": 10,
            },
            {
                "ordinal": 3,
                "logical_time_from": "2026-07-30T11:50:00+00:00",
                "logical_time_to": "2026-07-30T12:00:00+00:00",
                "clock_status": "observed_only",
                "ecology_status": "accepted",
                "ecology_reason_code": "life_development_occurrence_committed",
                "ecology_runtime_outcome_ref": (
                    "life-ecology:life_development_occurrence_committed"
                ),
                "ecology_trigger_id": "trigger:life:3",
                "ecology_completion_event_ref": "event:life-complete:3",
                "life_model_attempt_counts_by_role": {},
                "world_author_decision": None,
                "cadence_draw_event_ref": None,
                "cadence_delay_seconds": None,
                "ledger_sequence_before": 10,
                "ledger_sequence_after": 14,
            },
        ],
        "ecology_status_counts": {
            "accepted": 1,
            "cooldown": 1,
            "no_op": 0,
            "not_observed": 0,
            "technical_failure": 1,
            "unknown": 0,
        },
        "ecology_reason_code_counts": {
            "cooldown": 1,
            "life_development.world_author_source_closure_rejected": 1,
            "life_development_occurrence_committed": 1,
        },
        "recorded_cadence_cooldown_ordinals": [2],
        "next_recorded_consideration_at": None,
        "life_model_attempt_counts_by_role": {},
        "world_author_consideration_ordinals": [],
        "world_author_decision_counts": {},
        "ledger_sequence_before": 4,
        "ledger_sequence_after": 14,
        "new_event_type_counts": {
            "ClockAdvanced": 3,
            "ExperienceCommitted": 1,
            "TriggerProcessCompleted": 3,
            "TriggerProcessOpened": 3,
        },
        "experience_count_before": 0,
        "experience_count_after": 1,
        "plan_count_before": 0,
        "plan_count_after": 2,
        "memory_candidate_count_before": 0,
        "memory_candidate_count_after": 1,
    }
    assert summary["recorded_cadence_cooldown_ordinals"] == [2]
    assert summary["next_recorded_consideration_at"] is None


@pytest.mark.asyncio
async def test_preconversation_life_ecology_uses_changed_schedule_when_completion_is_compact() -> (
    None
):
    summary = await _RUNNER._run_preconversation_life_ecology(
        host=_ProjectedLifeEcologyAuditHost(),
        conversation_started_at=NOW,
        units=1,
    )

    assert summary["tick_statuses"] == ["observed_only"]
    assert summary["units"] == [
        {
            "ordinal": 1,
            "logical_time_from": "2026-07-30T11:50:00+00:00",
            "logical_time_to": "2026-07-30T12:00:00+00:00",
            "clock_status": "observed_only",
            "ecology_status": "no_op",
            "ecology_reason_code": "life_development_no_op",
            "ecology_runtime_outcome_ref": "life-ecology:life_development_no_op",
            "ecology_trigger_id": "trigger:life:projected",
            "ecology_completion_event_ref": None,
            "life_model_attempt_counts_by_role": {},
            "world_author_decision": None,
            "cadence_draw_event_ref": None,
            "cadence_delay_seconds": None,
            "ledger_sequence_before": 20,
            "ledger_sequence_after": 22,
        }
    ]
    assert summary["ecology_status_counts"]["no_op"] == 1
    assert summary["recorded_cadence_cooldown_ordinals"] == []
    assert summary["next_recorded_consideration_at"] == "2026-07-30T20:00:00+00:00"


@pytest.mark.asyncio
async def test_preconversation_life_ecology_does_not_infer_work_from_clock_status() -> None:
    summary = await _RUNNER._run_preconversation_life_ecology(
        host=_ClockOnlyLifeEcologyAuditHost(),
        conversation_started_at=NOW,
        units=1,
    )

    assert summary["units"][0]["clock_status"] == "observed_only"
    assert summary["units"][0]["ecology_status"] == "not_observed"
    assert summary["units"][0]["ecology_reason_code"] == "life_ecology_completion_not_observed"
    assert summary["ecology_status_counts"]["not_observed"] == 1


@pytest.mark.asyncio
async def test_preconversation_life_ecology_distinguishes_wakes_from_model_considerations() -> None:
    summary = await _RUNNER._run_preconversation_life_ecology(
        host=_CadencedWorldAuthorAuditHost(),
        conversation_started_at=NOW,
        units=2,
    )

    assert summary["world_author_consideration_ordinals"] == [1]
    assert summary["life_model_attempt_counts_by_role"] == {"world_author": 1}
    assert summary["world_author_decision_counts"] == {"no_op": 1}
    assert summary["units"][0]["life_model_attempt_counts_by_role"] == {"world_author": 1}
    assert summary["units"][0]["world_author_decision"] == "no_op"
    assert summary["units"][0]["cadence_delay_seconds"] == 7200
    assert summary["units"][0]["cadence_draw_event_ref"] == ("event:random-draw:life-cadence-1")
    assert summary["units"][1]["life_model_attempt_counts_by_role"] == {}
    assert summary["units"][1]["world_author_decision"] is None
    assert summary["units"][1]["cadence_delay_seconds"] is None
    assert summary["units"][1]["ecology_status"] == "cooldown"


def test_preconversation_life_ecology_is_explicit_and_off_by_default() -> None:
    args = _RUNNER._parser().parse_args([])

    assert args.preconversation_life_ecology_units == 0


def test_start_at_parser_requires_an_aware_instant_and_normalizes_utc() -> None:
    args = _RUNNER._parser().parse_args(["--start-at", "2026-07-30T10:00:00+08:00"])

    assert args.start_at == datetime(2026, 7, 30, 2, 0, tzinfo=UTC)
    with pytest.raises(SystemExit):
        _RUNNER._parser().parse_args(["--start-at", "2026-07-30T10:00:00"])


@pytest.mark.asyncio
async def test_virtual_pacing_clock_advances_monotonically() -> None:
    clock = _RUNNER._VirtualPacingClock(NOW)

    clock.advance_to(NOW + timedelta(minutes=9))
    await clock.sleep(2.5)
    clock.advance_to(NOW + timedelta(minutes=4))

    assert clock.now() == NOW + timedelta(minutes=9, seconds=2.5)


@pytest.mark.asyncio
async def test_fast_paced_run_uses_one_virtual_instant_without_virtual_deadline_sleep(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    turn = SimpleNamespace(turn_id="turn:later", text="九分钟后的消息", at_minutes=9)
    scenario = SimpleNamespace(
        turns=(turn,),
        source_event_id=lambda _turn: "source:turn:later",
    )
    built: dict[str, object] = {}

    def fake_build(**kwargs: object) -> _FastPacingAuditHost:
        built.update(kwargs)
        host = _FastPacingAuditHost(build_kwargs=built)
        built["host"] = host
        return host

    class _AuditResult:
        def model_dump(self, *, mode: str) -> dict[str, object]:
            assert mode == "json"
            return {
                "status": "captured",
                "summary": {
                    "prefetch_presented_turn_count": 1,
                    "character_pull_selected_turn_count": 0,
                    "character_recall_turn_count": 0,
                },
                "turns": [
                    {
                        "causal_chain": {
                            "prefetch_presented": True,
                            "character_pull_selected": False,
                            "character_recall_selected": False,
                        }
                    }
                ],
            }

    monkeypatch.setattr(
        _RUNNER,
        "Settings",
        lambda **_kwargs: SimpleNamespace(deepseek_api_key="configured"),
    )
    monkeypatch.setattr(
        _RUNNER,
        "load_private_self_expression_scenario",
        lambda _fixture: scenario,
    )
    monkeypatch.setattr(_RUNNER, "build_qq_c2c_host", fake_build)
    monkeypatch.setattr(
        _RUNNER,
        "PrivateSelfExpressionAuditEvaluator",
        lambda: SimpleNamespace(
            evaluate=lambda **_kwargs: _AuditResult(),
        ),
    )

    report = await _RUNNER.run(
        fixture=tmp_path / "fixture.json",
        output=tmp_path / "audit.json",
        database=tmp_path / "audit.sqlite",
        start_at=NOW,
        fast_pacing=True,
        preconversation_life_ecology_units=0,
        first_turn_background_units=0,
        background_units=0,
        max_turns=None,
    )

    host = built["host"]
    assert isinstance(host, _FastPacingAuditHost)
    expected_observed_at = NOW + timedelta(minutes=9)
    assert host.inbound_clock_samples == [
        (expected_observed_at, expected_observed_at, expected_observed_at)
    ]
    assert host.scheduler_observed_at == [expected_observed_at + timedelta(seconds=121)]
    assert built.get("action_due_sleep") is None
    assert built["ingress_sleep"] is not built.get("action_due_sleep")
    assert report["contract"] == "private-self-expression-real-audit-run.2"
    assert report["naturalness_readiness"]["assessment"] == "reliability_only"
    assert report["naturalness_readiness"]["production_behavior_gate"] is False
    assert report["naturalness_readiness"]["zero_preheat_semantics"] == "reliability_only"
    runtime_turn = report["runtime_turns"][0]
    assert runtime_turn["source_event_id"] == "source:turn:later"
    assert runtime_turn["user_text"] == "九分钟后的消息"
    assert runtime_turn["source_event_ids"] == ["source:turn:later"]
    assert runtime_turn["user_messages"] == ["九分钟后的消息"]
    assert runtime_turn["ingress_mode"] == "single"
    assert runtime_turn["overlap_group"] is None
    assert runtime_turn["fragment_statuses"] == ["observed_only"]
    assert report["source_closure_rejection_trace"] == {
        "enabled": False,
        "event_count": 0,
        "trace_sha256": None,
    }
    assert report["source_review_authority_health"] == {
        "status": "ready",
        "source_review_authority": {
            "last_winner_lane": "primary",
            "all_lanes_failed": 0,
        },
    }
    assert report["immutable_replay_audit"]["summary"] == {
        "prefetch_presented_turn_count": 1,
        "character_pull_selected_turn_count": 0,
        "character_recall_turn_count": 0,
    }
    assert report["immutable_replay_audit"]["turns"][0]["causal_chain"] == {
        "prefetch_presented": True,
        "character_pull_selected": False,
        "character_recall_selected": False,
    }
    written = json.loads((tmp_path / "audit.json").read_text(encoding="utf-8"))
    assert stat.S_IMODE((tmp_path / "audit.json").stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "audit.sqlite").stat().st_mode) == 0o600
    assert written["immutable_replay_audit"] == report["immutable_replay_audit"]
    assert "runner rejected visible candidate" not in (tmp_path / "audit.json").read_text(
        encoding="utf-8"
    )


@pytest.mark.asyncio
async def test_fast_paced_run_reports_real_first_visible_wall_time_and_clock_semantics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    turn = SimpleNamespace(turn_id="turn:latency", text="在吗", at_minutes=0)
    scenario = SimpleNamespace(
        turns=(turn,),
        source_event_id=lambda _turn: "source:turn:latency",
    )

    class _VisibleLatencyHost(_FastPacingAuditHost):
        completed = False

        async def inbound_text(
            self,
            *,
            message_id: str,
            recipient_id: str,
            text: str,
            observed_at: datetime,
        ) -> SimpleNamespace:
            del message_id, text, observed_at
            ingress_sleep = self._build_kwargs["ingress_sleep"]
            delivery = self._build_kwargs["delivery"]
            assert callable(ingress_sleep)
            await ingress_sleep(0.280)
            await asyncio.sleep(0.01)
            await delivery.send_text(recipient_id, "嗯，在。")
            self.completed = True
            return SimpleNamespace(status="action_authorized")

        def latency_samples(self) -> tuple[SimpleNamespace, ...]:
            if not self.completed:
                return ()
            return (
                SimpleNamespace(
                    trace_id="trace:latency",
                    startup="cold",
                    environment="real_transport",
                    segment="context",
                    duration_ms=3.0,
                ),
                SimpleNamespace(
                    trace_id="trace:latency",
                    startup="cold",
                    environment="real_transport",
                    segment="model_completion",
                    duration_ms=10.0,
                ),
                SimpleNamespace(
                    trace_id="trace:latency",
                    startup="cold",
                    environment="real_transport",
                    segment="ingress_to_visible",
                    duration_ms=290.0,
                ),
            )

    monkeypatch.setattr(
        _RUNNER,
        "Settings",
        lambda **_kwargs: SimpleNamespace(deepseek_api_key="configured"),
    )
    monkeypatch.setattr(
        _RUNNER,
        "load_private_self_expression_scenario",
        lambda _fixture: scenario,
    )
    monkeypatch.setattr(
        _RUNNER,
        "build_qq_c2c_host",
        lambda **kwargs: _VisibleLatencyHost(build_kwargs=kwargs),
    )
    monkeypatch.setattr(
        _RUNNER,
        "PrivateSelfExpressionAuditEvaluator",
        lambda: SimpleNamespace(
            evaluate=lambda **_kwargs: SimpleNamespace(
                model_dump=lambda **_dump_kwargs: {"status": "captured"}
            ),
        ),
    )

    report = await _RUNNER.run(
        fixture=tmp_path / "fixture.json",
        output=tmp_path / "audit.json",
        database=tmp_path / "audit.sqlite",
        start_at=NOW,
        fast_pacing=True,
        preconversation_life_ecology_units=0,
        first_turn_background_units=0,
        background_units=0,
        max_turns=None,
    )

    runtime_turn = report["runtime_turns"][0]
    assert 5 <= runtime_turn["first_visible_reply_wall_ms"] < 200
    assert runtime_turn["first_visible_reply_measurement"] == (
        "process_monotonic_to_isolated_provider_acceptance"
    )
    assert runtime_turn["latency_segments"] == [
        {
            "trace_id": "trace:latency",
            "startup": "cold",
            "environment": "real_transport",
            "segment": "context",
            "duration_ms": 3.0,
            "clock_semantics": "process_monotonic_observed_span",
        },
        {
            "trace_id": "trace:latency",
            "startup": "cold",
            "environment": "real_transport",
            "segment": "ingress_to_visible",
            "duration_ms": 290.0,
            "clock_semantics": "monotonic_plus_persisted_virtual_ingress_duration",
        },
        {
            "trace_id": "trace:latency",
            "startup": "cold",
            "environment": "real_transport",
            "segment": "model_completion",
            "duration_ms": 10.0,
            "clock_semantics": "process_monotonic_observed_span",
        },
    ]
    assert report["latency_evidence"] == {
        "first_visible_clock": "process_monotonic",
        "pacing_clock": "virtual",
        "scheduler_clock": "virtual",
        "segment_semantics": (
            "observed spans are accumulated by label and may overlap; "
            "they are not an additive phase partition"
        ),
        "role_provider_timing": {
            "entry_segment": "ingress_to_first_role_provider",
            "ttft_segment": "model_ttft",
            "ttft_status": "unavailable",
            "ttft_reason": "non_streaming_completion_api",
            "completion_segment": "model_completion",
        },
        "fast_pacing_ingress_semantics": (
            "coalescing, queue, and ingress_to_visible samples may include "
            "persisted virtual durations; first_visible_reply_wall_ms does not"
        ),
    }


@pytest.mark.asyncio
async def test_run_keeps_inbound_status_when_post_inbound_drain_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    turn = SimpleNamespace(turn_id="turn:accepted", text="在吗", at_minutes=0)
    scenario = SimpleNamespace(
        turns=(turn,),
        source_event_id=lambda _turn: "source:turn:accepted",
    )

    monkeypatch.setattr(
        _RUNNER,
        "Settings",
        lambda **_kwargs: SimpleNamespace(deepseek_api_key="configured"),
    )
    monkeypatch.setattr(
        _RUNNER,
        "load_private_self_expression_scenario",
        lambda _fixture: scenario,
    )
    monkeypatch.setattr(
        _RUNNER,
        "build_qq_c2c_host",
        lambda **kwargs: _PostInboundDrainFailureAuditHost(build_kwargs=kwargs),
    )
    monkeypatch.setattr(
        _RUNNER,
        "PrivateSelfExpressionAuditEvaluator",
        lambda: SimpleNamespace(
            evaluate=lambda **_kwargs: SimpleNamespace(
                model_dump=lambda **_dump_kwargs: {"status": "captured"}
            ),
        ),
    )

    report = await _RUNNER.run(
        fixture=tmp_path / "fixture.json",
        output=tmp_path / "audit.json",
        database=tmp_path / "audit.sqlite",
        start_at=NOW,
        fast_pacing=True,
        preconversation_life_ecology_units=0,
        first_turn_background_units=0,
        background_units=0,
        max_turns=None,
    )

    runtime_turn = report["runtime_turns"][0]
    assert runtime_turn["status"] == "action_authorized"
    assert runtime_turn["error"] is None
    assert runtime_turn["post_inbound_drain_error"] == (
        "ConcurrencyConflict: stale deliberation revision"
    )


def test_rejection_trace_is_explicit_and_off_by_default() -> None:
    args = _RUNNER._parser().parse_args([])

    assert args.rejection_trace_output is None


@pytest.mark.asyncio
async def test_explicit_rejection_trace_is_private_separate_and_associated_by_turn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    turn = SimpleNamespace(turn_id="turn:trace", text="private user text", at_minutes=0)
    scenario = SimpleNamespace(
        turns=(turn,),
        source_event_id=lambda _turn: "source:turn:trace",
    )

    def fake_build(**kwargs: object) -> _FastPacingAuditHost:
        return _FastPacingAuditHost(build_kwargs=kwargs)

    class _AuditResult:
        def model_dump(self, *, mode: str) -> dict[str, str]:
            assert mode == "json"
            return {"status": "captured"}

    production = tmp_path / "production.sqlite"
    monkeypatch.setattr(
        _RUNNER,
        "Settings",
        lambda **_kwargs: SimpleNamespace(
            deepseek_api_key="configured",
            database_path=production,
        ),
    )
    monkeypatch.setattr(
        _RUNNER,
        "load_private_self_expression_scenario",
        lambda _fixture: scenario,
    )
    monkeypatch.setattr(_RUNNER, "build_qq_c2c_host", fake_build)
    monkeypatch.setattr(
        _RUNNER,
        "PrivateSelfExpressionAuditEvaluator",
        lambda: SimpleNamespace(evaluate=lambda **_kwargs: _AuditResult()),
    )
    report_path = tmp_path / "audit.json"
    trace_path = tmp_path / "rejections.json"
    database = tmp_path / "isolated.sqlite"

    report = await _RUNNER.run(
        fixture=tmp_path / "fixture.json",
        output=report_path,
        database=database,
        start_at=NOW,
        fast_pacing=True,
        preconversation_life_ecology_units=0,
        first_turn_background_units=0,
        background_units=0,
        max_turns=None,
        rejection_trace_output=trace_path,
    )

    trace_bytes = trace_path.read_bytes()
    trace = json.loads(trace_bytes)
    ordinary = report_path.read_text(encoding="utf-8")
    assert stat.S_IMODE(trace_path.stat().st_mode) == 0o600
    assert trace["contract"] == "isolated-source-closure-trace.3"
    assert trace["authority"] == "process_local_non_authoritative"
    assert trace["turns"] == [
        {
            "turn_id": "turn:trace",
            "events": [
                {
                    "stage": "post_appeal_initial_rejection",
                    "candidate_sha256": trace["turns"][0]["events"][0]["candidate_sha256"],
                    "visible_beat_texts": ["runner rejected visible candidate"],
                    "visible_beat_sha256": trace["turns"][0]["events"][0]["visible_beat_sha256"],
                    "visible_text_truncated": False,
                    "surface_extraction": "available",
                    "ci": [1],
                    "v": ["undeclared_external_assertion"],
                    "p": ["temporal_authority_mismatch"],
                    "visible_findings": [],
                    "discourse_resolved_visible_finding_indexes": [],
                }
            ],
        }
    ]
    assert "RUNNER_PRIVATE_STATE" not in trace_bytes.decode()
    assert "RUNNER_CONTEXT_REF" not in trace_bytes.decode()
    assert "private user text" not in trace_bytes.decode()
    assert "runner rejected visible candidate" not in ordinary
    assert "isolated-source-closure-trace.3" not in ordinary
    assert report["source_closure_rejection_trace"] == {
        "enabled": True,
        "event_count": 1,
        "trace_sha256": hashlib.sha256(trace_bytes).hexdigest(),
    }


def test_rejection_trace_path_must_be_nonexistent_and_separate(
    tmp_path: Path,
) -> None:
    production = tmp_path / "production.sqlite"
    database = tmp_path / "isolated.sqlite"
    report = tmp_path / "report.json"
    trace = tmp_path / "trace.json"

    assert (
        _RUNNER._safe_rejection_trace_output(
            trace,
            production_database=production,
            audit_database=database,
            report_output=report,
        )
        == trace.resolve()
    )
    for forbidden in (production, database, report):
        with pytest.raises(ValueError, match="must be different"):
            _RUNNER._safe_rejection_trace_output(
                forbidden,
                production_database=production,
                audit_database=database,
                report_output=report,
            )
    trace.write_text("occupied", encoding="utf-8")
    with pytest.raises(ValueError, match="must not already exist"):
        _RUNNER._safe_rejection_trace_output(
            trace,
            production_database=production,
            audit_database=database,
            report_output=report,
        )


@pytest.mark.asyncio
async def test_final_fast_audit_drain_uses_the_production_scheduler_budget() -> None:
    host = _TerminalReceiptAuditHost()
    clock = _RUNNER._VirtualPacingClock(NOW)

    await _RUNNER._drain_terminal_receipts(host=host, fast_clock=clock)

    assert host.drains == []
    assert host.scheduler_runs == [
        {
            "observed_at": NOW + timedelta(seconds=121),
            "max_action_units": 64,
            "max_background_units": 0,
        }
    ]
    assert clock.now() == NOW + timedelta(seconds=121)


@pytest.mark.asyncio
async def test_final_real_pacing_drain_does_not_advance_the_world_clock() -> None:
    host = _TerminalReceiptAuditHost()

    await _RUNNER._drain_terminal_receipts(host=host)

    assert host.drains == [{"max_action_units": 64, "max_background_units": 0}]
    assert host.scheduler_runs == []


@pytest.mark.asyncio
async def test_final_fast_audit_drain_persists_clock_and_completes_expression_plan(
    tmp_path: Path,
) -> None:
    """The retained audit must not export a merely provider-accepted last turn."""

    clock = _RUNNER._VirtualPacingClock(NOW)
    delivery = _RUNNER.IsolatedAuditDelivery()
    host = build_qq_c2c_host(
        settings=Settings(
            database_path=tmp_path / "terminal-expression.sqlite",
            PRIMARY_USER_ID="geoff",
            WORLD_V2_TEXT_ENDPOINT_ENABLED=False,
            WORLD_V2_EXPRESSION_EPISODE_MODE="off",
        ),
        recipient_id="10001",
        bootstrap_at=NOW,
        model=_ImmediateReplyModel(),
        world_support_model=FakeCompanionModel(),
        delivery=delivery,
        ingress_now=clock.now,
        ingress_sleep=clock.sleep,
        action_due_now=clock.now,
        interactive_turn_budget_policy=InteractiveTurnBudgetPolicy(
            wall_clock=clock.now,
        ),
        use_configured_recall_embedding=False,
    )
    try:
        outcome = await host.inbound_text(
            message_id="message:terminal-audit",
            recipient_id="10001",
            text="你还在吗",
            observed_at=NOW,
        )
        await host.drain(max_action_units=16, max_background_units=0)
        before = host.export_replay_evidence()

        assert outcome.status == "action_authorized"
        assert [item.state for item in before.projection.actions] == ["provider_accepted"]
        assert before.projection.pending_actions
        assert [item.state for item in before.projection.expression_plans] == ["authorized"]

        await _RUNNER._drain_terminal_receipts(host=host, fast_clock=clock)
        after = host.export_replay_evidence()
    finally:
        await host.aclose()

    assert after.projection.logical_time > before.projection.logical_time
    assert after.projection.pending_actions == ()
    assert [item.state for item in after.projection.actions] == ["delivered"]
    assert [item.state for item in after.projection.expression_plans] == ["completed"]
    assert any(item.event.event_type == "ExpressionPlanCompleted" for item in after.events)


@pytest.mark.asyncio
async def test_explicit_trace_captures_qq_single_call_post_appraisal_delegate(
    tmp_path: Path,
) -> None:
    """The isolated diagnostic follows the same delegated expression path as the daemon."""

    clock = _RUNNER._VirtualPacingClock(NOW)
    delivery = _RUNNER.IsolatedAuditDelivery()
    role_model = _RejectedThenCorrectedReplyModel()
    reviewer = _DormLifeSourceClosureReviewer()
    inventory = _DormCandidateExternalInventory()
    host = build_qq_c2c_host(
        settings=Settings(
            database_path=tmp_path / "delegated-source-trace.sqlite",
            PRIMARY_USER_ID="geoff",
            WORLD_V2_TEXT_ENDPOINT_ENABLED=False,
            WORLD_V2_EXPRESSION_EPISODE_MODE="off",
        ),
        recipient_id="10001",
        bootstrap_at=NOW,
        model=role_model,
        world_support_model=_NamedAdvisoryFake(),
        source_closure_model=reviewer,
        candidate_external_proposition_inventory_model=inventory,
        delivery=delivery,
        ingress_now=clock.now,
        ingress_sleep=clock.sleep,
        action_due_now=clock.now,
        interactive_turn_budget_policy=InteractiveTurnBudgetPolicy(
            wall_clock=clock.now,
        ),
        use_configured_recall_embedding=False,
    )
    collector = BoundedSourceClosureTraceCollector()
    try:
        with capture_isolated_source_closure_trace(collector):
            outcome = await host.inbound_text(
                message_id="message:delegated-source-trace",
                recipient_id="10001",
                text="你还在吗",
                observed_at=NOW,
            )
    finally:
        await host.aclose()

    assert outcome.status == "action_authorized"
    assert len(role_model.calls) == 2
    # Each authored candidate pays one indexed Coverage call. The initial
    # unclosed external proposition additionally receives one independent
    # report-relative adjudication; Inventory only locates each candidate and
    # no longer re-judges ordinary unclosed semantic roles.
    assert len(reviewer.calls) == 3
    assert len(inventory.calls) == 2
    trace = [event.as_dict() for event in collector.snapshot()]
    rejections = [event for event in trace if "stage" in event]
    verdicts = [event for event in trace if event.get("record_kind") == "candidate_verdict"]
    assert [event["stage"] for event in rejections] == ["initial_rejection"]
    assert rejections[0]["visible_beat_texts"] == ["刚才在宿舍翻书，现在看到你了。"]
    assert verdicts[0]["inventory_outcome"] == "external_propositions"
    assert verdicts[0]["coverage_outcome"] == "completed"
    assert verdicts[0]["coverage"][0]["decision"] == "unclosed"
    assert verdicts[1]["inventory_outcome"] == "no_external_propositions"
    assert verdicts[1]["coverage_outcome"] == "completed"
    assert verdicts[1]["coverage"][0]["decision"] == "closed"
    assert "我看到你这句了。" not in json.dumps(verdicts, ensure_ascii=False)
    assert [item["content"] for item in delivery.sent if item["modality"] == "text"] == [
        "我看到你这句了。"
    ]

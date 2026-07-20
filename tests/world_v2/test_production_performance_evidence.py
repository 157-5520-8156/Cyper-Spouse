from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import json

import pytest

from companion_daemon.world_v2.chat_model_deliberation_adapter import (
    ChatModelDeliberationAdapter,
)
from companion_daemon.world_v2.deliberation import ModelRoute, RouteRequest
from companion_daemon.world_v2.platform_action_executor import PlatformDispatchReceipt
from companion_daemon.world_v2.production_performance_evidence import WarmChatPerformanceGate
from companion_daemon.world_v2.production_turn_application import (
    WorldV2TurnApplicationConfig,
    build_sqlite_world_v2_turn_application,
)


NOW = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)


class _Identities:
    def resolve(self, *, platform: str, platform_user_id: str) -> tuple[str, str]:
        assert (platform, platform_user_id) == ("test", "user.1")
        return "user:user.1", "user:user.1"


class _Router:
    async def route(self, _request: RouteRequest) -> ModelRoute:
        return ModelRoute(tier="flash", reason_code="ordinary_chat", router_version="perf.1")


def _usage_hash(material: dict[str, object]) -> str:
    canonical = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


class _MeteredChat:
    model = "offline-metered-flash"

    def __init__(self) -> None:
        self.calls = 0

    async def complete_with_usage(self, _messages, *, temperature: float = 0.8):  # type: ignore[no-untyped-def]
        del temperature
        self.calls += 1
        usage = {
            "usage_contract": "model-usage.1",
            "route_class": "chat",
            "input_tokens": 120,
            "output_tokens": 16,
            "thinking_tokens": 0,
            "token_provenance": "provider_reported",
            "transport": "provider_api",
            "provider": "offline-production-fixture",
            "provider_usage_ref": f"usage:perf:{self.calls}",
        }
        usage["provider_usage_hash"] = _usage_hash(usage)
        return (
            json.dumps(
                {
                    "response_text": "我在，接着说吧。",
                    "stance": "acknowledge_briefly",
                    "brief_rationale": "ordinary warm chat",
                    "confidence": 7000,
                },
                ensure_ascii=False,
            ),
            usage,
        )

    async def complete(self, _messages, *, temperature: float = 0.8):  # type: ignore[no-untyped-def]
        raise AssertionError("metered production path must use complete_with_usage")


class _DeliveredTransport:
    provider = "platform:offline-performance"

    def __init__(self) -> None:
        self.receipts: dict[str, PlatformDispatchReceipt] = {}

    async def send(self, request):  # type: ignore[no-untyped-def]
        existing = self.receipts.get(request.idempotency_key)
        if existing is not None:
            return existing
        suffix = hashlib.sha256(request.fingerprint.encode()).hexdigest()
        receipt = PlatformDispatchReceipt(
            provider_receipt_id=f"receipt:perf:{suffix}",
            provider_ref=f"message:perf:{suffix}",
            status="delivered",
            received_at=NOW,
            raw_payload_hash="sha256:" + hashlib.sha256(request.body.encode()).hexdigest(),
            idempotency_key=request.idempotency_key,
            request_fingerprint=request.fingerprint,
        )
        self.receipts[request.idempotency_key] = receipt
        return receipt

    async def lookup(self, *, idempotency_key: str, request_fingerprint: str):
        receipt = self.receipts.get(idempotency_key)
        if receipt is not None and receipt.request_fingerprint != request_fingerprint:
            raise ValueError("request fingerprint conflict")
        return receipt


@pytest.mark.asyncio
async def test_twenty_production_warm_turns_are_incremental_metered_and_under_offline_p95(
    tmp_path,
) -> None:
    chat = _MeteredChat()
    adapter = ChatModelDeliberationAdapter(model=chat)
    app = build_sqlite_world_v2_turn_application(
        path=tmp_path / "twenty-warm.sqlite3",
        config=WorldV2TurnApplicationConfig(
            world_id="world:twenty-warm",
            companion_actor_ref="agent:companion",
            reply_target="user:user.1",
            action_pump_owner="pump:twenty-warm",
        ),
        identities=_Identities(),
        router=_Router(),
        main_model=adapter,
        quick_recovery=adapter,
        transport=_DeliveredTransport(),
        now=NOW,
    )
    try:
        warmup = await app.inbound(
            platform="test",
            platform_user_id="user.1",
            platform_message_id="warmup",
            text="先热个身",
            observed_at=NOW,
            trace_id="trace:perf:warmup",
        )
        assert len(warmup.authorized_action_ids) == 1
        assert (await app.drain_action(warmup.authorized_action_ids[0])).status == "settled"
        before = app.performance_evidence()
        repeated_read = app.performance_evidence()
        assert (
            repeated_read.projection_counters.total_replay_calls
            == before.projection_counters.total_replay_calls
        )
        assert (
            repeated_read.projection_counters.historical_replay_calls
            == before.projection_counters.historical_replay_calls
        )
        trace_ids: list[str] = []
        turn_ids: list[str] = []
        for index in range(20):
            message_id = f"perf-{index}"
            trace_id = f"trace:perf:{index}"
            outcome = await app.inbound(
                platform="test",
                platform_user_id="user.1",
                platform_message_id=message_id,
                text=f"普通热聊第 {index} 轮",
                observed_at=NOW + timedelta(seconds=index + 1),
                trace_id=trace_id,
            )
            assert len(outcome.authorized_action_ids) == 1
            assert (await app.drain_action(outcome.authorized_action_ids[0])).status == "settled"
            trace_ids.append(trace_id)
            turn_ids.append(
                f"event:trigger:observation:platform:test:test:user.1:{message_id}"
            )
        after = app.performance_evidence()
        result = WarmChatPerformanceGate().evaluate(
            before=before,
            after=after,
            expected_trace_ids=trace_ids,
            expected_turn_ids=turn_ids,
        )

        assert result.passed, result.violations
        assert result.observed_hot_turns == 20
        assert result.p95_ingress_to_visible_ms is not None
        assert result.p95_ingress_to_visible_ms <= 5_000
        expected_segments = {
            "coalescing",
            "queue",
            "snapshot",
            "model_completion",
            "acceptance",
            "dispatch",
            "receipt",
            "ingress_to_visible",
        }
        by_trace = {
            trace_id: {
                sample.segment for sample in after.latency_samples if sample.trace_id == trace_id
            }
            for trace_id in trace_ids
        }
        assert all(expected_segments <= segments for segments in by_trace.values())
        assert all("model_ttft" not in segments for segments in by_trace.values())
        assert chat.calls == 21
    finally:
        app.close()


@pytest.mark.asyncio
async def test_production_trace_restart_and_duplicate_do_not_repeat_model_or_rebind_trace(
    tmp_path,
) -> None:
    path = tmp_path / "restart-trace.sqlite3"
    chat = _MeteredChat()
    adapter = ChatModelDeliberationAdapter(model=chat)
    config = WorldV2TurnApplicationConfig(
        world_id="world:restart-trace",
        companion_actor_ref="agent:companion",
        reply_target="user:user.1",
        action_pump_owner="pump:restart-trace",
    )

    def build():
        return build_sqlite_world_v2_turn_application(
            path=path,
            config=config,
            identities=_Identities(),
            router=_Router(),
            main_model=adapter,
            quick_recovery=adapter,
            transport=_DeliveredTransport(),
            now=NOW,
        )

    app = build()
    outcome = await app.inbound(
        platform="test",
        platform_user_id="user.1",
        platform_message_id="same",
        text="同一条",
        observed_at=NOW,
        trace_id="trace:restart:same",
    )
    assert (await app.drain_action(outcome.authorized_action_ids[0])).status == "settled"
    duplicate = await app.inbound(
        platform="test",
        platform_user_id="user.1",
        platform_message_id="same",
        text="同一条",
        observed_at=NOW,
        trace_id="trace:restart:same",
    )
    assert duplicate.authorized_action_ids == outcome.authorized_action_ids
    assert chat.calls == 1
    app.close()

    reopened = build()
    try:
        joined = await reopened.inbound(
            platform="test",
            platform_user_id="user.1",
            platform_message_id="same",
            text="同一条",
            observed_at=NOW,
            trace_id="trace:restart:same",
        )
        assert joined.authorized_action_ids == outcome.authorized_action_ids
        assert chat.calls == 1
        samples = reopened.latency_samples()
        assert {sample.startup for sample in samples} == {"cold"}
        assert not any(sample.segment == "ingress_to_visible" for sample in samples)
    finally:
        reopened.close()


@pytest.mark.asyncio
async def test_wall_arrival_never_advances_clock_and_clock_authority_survives_reopen(
    tmp_path,
) -> None:
    path = tmp_path / "clock-pinned-ingress.sqlite3"
    chat = _MeteredChat()
    adapter = ChatModelDeliberationAdapter(model=chat)
    transport = _DeliveredTransport()
    config = WorldV2TurnApplicationConfig(
        world_id="world:clock-pinned-ingress",
        companion_actor_ref="agent:companion",
        reply_target="user:user.1",
        action_pump_owner="pump:clock-pinned-ingress",
    )

    def build():
        return build_sqlite_world_v2_turn_application(
            path=path,
            config=config,
            identities=_Identities(),
            router=_Router(),
            main_model=adapter,
            quick_recovery=adapter,
            transport=transport,
            now=NOW,
        )

    app = build()
    try:
        for index, arrived in enumerate((NOW + timedelta(minutes=1), NOW + timedelta(hours=2))):
            outcome = await app.inbound(
                platform="test",
                platform_user_id="user.1",
                platform_message_id=f"before-clock-{index}",
                text="arrival is not clock",
                observed_at=arrived,
                trace_id=f"trace:before-clock:{index}",
            )
            assert (await app.drain_action(outcome.authorized_action_ids[0])).status == "settled"
            stored = app._ledger.lookup_event_commit(  # noqa: SLF001 - authority assertion
                "event:trigger:observation:platform:test:test:user.1:"
                f"before-clock-{index}"
            )
            assert stored is not None
            observation = stored[0].payload()
            assert datetime.fromisoformat(observation["logical_time"]) == NOW
            assert datetime.fromisoformat(observation["received_at"]) == arrived
            assert app._ledger.project().logical_time == NOW  # noqa: SLF001

        advanced = NOW + timedelta(minutes=10)
        await app.tick(
            tick_id="clock-authority-1",
            logical_time_from=NOW,
            logical_time_to=advanced,
            observed_at=NOW + timedelta(hours=3),
            trace_id="trace:clock-authority-1",
            causation_id="scheduler:clock-authority-1",
            correlation_id="clock:authority",
            reason="test",
        )
        outcome = await app.inbound(
            platform="test",
            platform_user_id="user.1",
            platform_message_id="after-clock",
            text="use the new durable clock",
            observed_at=NOW + timedelta(days=1),
            trace_id="trace:after-clock",
        )
        assert (await app.drain_action(outcome.authorized_action_ids[0])).status == "settled"
        stored = app._ledger.lookup_event_commit(  # noqa: SLF001
            "event:trigger:observation:platform:test:test:user.1:after-clock"
        )
        assert stored is not None
        assert datetime.fromisoformat(stored[0].payload()["logical_time"]) == advanced
        expected = app._ledger.project()  # noqa: SLF001
    finally:
        app.close()

    reopened = build()
    try:
        assert reopened._ledger.project() == expected  # noqa: SLF001
        assert reopened._ledger.rebuild() == expected  # noqa: SLF001
    finally:
        reopened.close()

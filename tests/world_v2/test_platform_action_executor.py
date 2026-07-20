from datetime import UTC, datetime
import hashlib

import pytest

from companion_daemon.world_v2.platform_action_executor import (
    PlatformActionExecutor,
    PlatformDispatchReceipt,
    ResolvedActionPayload,
)
from companion_daemon.world_v2.production_latency_trace import ProductionLatencyRecorder
from companion_daemon.world_v2.ledger import WorldLedger
from companion_daemon.world_v2.runtime import WorldRuntime
from companion_daemon.world_v2.schemas import BudgetAccount, BudgetReservation, WorldEvent
from companion_daemon.world_v2.schemas import Action


NOW = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)


def action(*, text: str = "我在。", kind: str = "reply") -> Action:
    return Action(
        schema_version="world-v2.1",
        action_id="action:reply:platform.1",
        world_id="world:platform-adapter",
        logical_time=NOW,
        created_at=NOW,
        trace_id="trace:platform-adapter",
        causation_id="acceptance:reply:1",
        correlation_id="conversation:1",
        kind=kind,
        layer="external_action",
        intent_ref="intent:reply:1",
        actor="agent:companion",
        target="user:primary",
        payload_ref="payload:reply:1",
        payload_hash="sha256:" + hashlib.sha256(text.encode()).hexdigest(),
        idempotency_key="platform:reply:1",
        budget_reservation_id="reservation:reply:1",
        state="authorized",
        recovery_policy="effect_once",
    )


class Payloads:
    def __init__(self, text: str) -> None:
        self.text = text
        self.calls: list[str] = []

    async def resolve(self, current: Action) -> ResolvedActionPayload:
        self.calls.append(current.action_id)
        return ResolvedActionPayload(
            payload_ref=current.payload_ref,
            payload_hash=current.payload_hash,
            content_type="text/plain",
            body=self.text,
        )


class Transport:
    provider = "transport:test"

    def __init__(self) -> None:
        self.sent = []
        self.lookups = []

    async def send(self, request):
        self.sent.append(request)
        return PlatformDispatchReceipt(
            provider_receipt_id="transport-receipt:1",
            provider_ref="transport-ref:1",
            status="delivered",
            cost_actual=3,
            received_at=NOW,
            raw_payload_hash="sha256:transport:1",
            idempotency_key=request.idempotency_key,
            request_fingerprint=request.fingerprint,
        )

    async def lookup(self, *, idempotency_key: str, request_fingerprint: str):
        self.lookups.append(idempotency_key)
        return PlatformDispatchReceipt(
            provider_receipt_id="transport-receipt:lookup",
            provider_ref="transport-ref:1",
            status="delivered",
            cost_actual=3,
            received_at=NOW,
            raw_payload_hash="sha256:transport:lookup",
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint,
        )


class _Clock:
    def __init__(self) -> None:
        self.value = 1_000_000_000

    def __call__(self) -> int:
        return self.value

    def advance_ms(self, value: int) -> None:
        self.value += value * 1_000_000


@pytest.mark.asyncio
async def test_platform_executor_dispatches_only_the_authorized_payload_and_binds_receipt() -> None:
    payloads = Payloads("我在。")
    transport = Transport()
    executor = PlatformActionExecutor(payloads=payloads, transport=transport)

    receipt = await executor.dispatch(action())

    assert receipt.action_id == "action:reply:platform.1"
    assert receipt.idempotency_key == "platform:reply:1"
    assert receipt.provider == "transport:test"
    assert receipt.status == "delivered"
    assert payloads.calls == ["action:reply:platform.1"]
    request = transport.sent[0]
    assert request.kind == "reply"
    assert request.body == "我在。"
    assert request.idempotency_key == "platform:reply:1"


@pytest.mark.asyncio
async def test_platform_executor_records_real_dispatch_receipt_and_visible_boundaries() -> None:
    clock = _Clock()
    recorder = ProductionLatencyRecorder(clock_ns=clock)
    trace = recorder.start_ingress(
        trace_id="trace:platform-adapter", environment="offline_in_process"
    )

    class TimedPayloads(Payloads):
        async def resolve(self, current: Action) -> ResolvedActionPayload:
            clock.advance_ms(3)
            return await super().resolve(current)

    class TimedTransport(Transport):
        async def send(self, request):  # type: ignore[no-untyped-def]
            clock.advance_ms(7)
            return await super().send(request)

    receipt = await PlatformActionExecutor(
        payloads=TimedPayloads("我在。"),
        transport=TimedTransport(),
        latency_recorder=recorder,
    ).dispatch(action())

    assert receipt is not None and receipt.status == "delivered"
    assert {sample.segment: sample.duration_ms for sample in trace.samples()} == {
        "dispatch": 3.0,
        "ingress_to_visible": 10.0,
        "receipt": 7.0,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ("failed", "provider_accepted"))
async def test_non_delivered_receipt_does_not_fabricate_visibility(status: str) -> None:
    clock = _Clock()
    recorder = ProductionLatencyRecorder(clock_ns=clock)
    trace = recorder.start_ingress(
        trace_id="trace:platform-adapter", environment="real_transport"
    )

    class FailedTransport(Transport):
        async def send(self, request):  # type: ignore[no-untyped-def]
            receipt = await super().send(request)
            return receipt.model_copy(update={"status": status})

    await PlatformActionExecutor(
        payloads=Payloads("我在。"),
        transport=FailedTransport(),
        latency_recorder=recorder,
    ).dispatch(action())

    segments = {sample.segment for sample in trace.samples()}
    assert segments == {"dispatch", "receipt"}
    assert "ingress_to_visible" not in segments


@pytest.mark.asyncio
async def test_platform_executor_renders_delayed_followup_as_the_same_message_primitive() -> None:
    transport = Transport()
    executor = PlatformActionExecutor(
        payloads=Payloads("晚一点再把这句话说完。"), transport=transport
    )

    receipt = await executor.dispatch(action(text="晚一点再把这句话说完。", kind="followup"))

    assert receipt.status == "delivered"
    assert transport.sent[0].kind == "followup"


@pytest.mark.asyncio
async def test_platform_executor_preserves_proactive_kind_for_transport_policy() -> None:
    transport = Transport()
    executor = PlatformActionExecutor(
        payloads=Payloads("刚才那件事我又想了一下。"), transport=transport
    )

    receipt = await executor.dispatch(
        action(text="刚才那件事我又想了一下。", kind="proactive_message")
    )

    assert receipt.status == "delivered"
    assert transport.sent[0].kind == "proactive_message"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "content_type", "body"),
    (
        ("reaction", "application/vnd.world-v2.reaction+json", '{"reaction":"eyes"}'),
        ("typing", "application/vnd.world-v2.typing+json", '{"duration_ms":1200}'),
        ("sticker", "application/vnd.world-v2.sticker+json", '{"sticker_id":"s:wave"}'),
    ),
)
async def test_platform_adapter_keeps_non_text_primitives_typed_and_receipt_bound(
    kind: str, content_type: str, body: str
) -> None:
    class TypedPayloads(Payloads):
        async def resolve(self, current: Action) -> ResolvedActionPayload:
            return ResolvedActionPayload(
                payload_ref=current.payload_ref,
                payload_hash=current.payload_hash,
                content_type=content_type,
                body=body,
            )

    current = action(text=body, kind=kind)
    transport = Transport()
    receipt = await PlatformActionExecutor(
        payloads=TypedPayloads(body), transport=transport
    ).dispatch(current)

    assert receipt is not None and receipt.status == "delivered"
    assert transport.sent[0].kind == kind
    assert transport.sent[0].content_type == content_type


@pytest.mark.asyncio
async def test_platform_executor_rejects_payload_bytes_that_do_not_match_authorized_hash() -> None:
    transport = Transport()
    executor = PlatformActionExecutor(payloads=Payloads("被替换的内容"), transport=transport)

    with pytest.raises(ValueError, match="payload hash"):
        await executor.dispatch(action())

    assert transport.sent == []


@pytest.mark.asyncio
async def test_platform_executor_rejects_content_type_that_would_change_reply_semantics() -> None:
    class WrongTypePayloads(Payloads):
        async def resolve(self, current: Action) -> ResolvedActionPayload:
            resolved = await super().resolve(current)
            return resolved.model_copy(update={"content_type": "application/json"})

    transport = Transport()
    executor = PlatformActionExecutor(payloads=WrongTypePayloads("我在。"), transport=transport)

    with pytest.raises(ValueError, match="content type"):
        await executor.dispatch(action())

    assert transport.sent == []


@pytest.mark.asyncio
async def test_platform_executor_recovers_by_provider_lookup_after_rebuilding_the_authorized_request() -> (
    None
):
    payloads = Payloads("我在。")
    transport = Transport()
    executor = PlatformActionExecutor(payloads=payloads, transport=transport)

    receipt = await executor.lookup_result(action())

    assert receipt.provider_receipt_id == "transport-receipt:lookup"
    assert transport.lookups == ["platform:reply:1"]
    assert payloads.calls == ["action:reply:platform.1"]


@pytest.mark.asyncio
async def test_platform_executor_rejects_a_dispatch_receipt_for_a_different_provider_request() -> (
    None
):
    class WrongReceiptTransport(Transport):
        async def send(self, request):
            receipt = await super().send(request)
            return receipt.model_copy(update={"idempotency_key": "other:action"})

    transport = WrongReceiptTransport()
    executor = PlatformActionExecutor(payloads=Payloads("我在。"), transport=transport)

    with pytest.raises(ValueError, match="idempotency"):
        await executor.dispatch(action())


@pytest.mark.asyncio
async def test_platform_executor_rejects_recovery_receipt_with_a_different_request_fingerprint() -> (
    None
):
    class WrongLookupTransport(Transport):
        async def lookup(self, *, idempotency_key: str, request_fingerprint: str):
            receipt = await super().lookup(
                idempotency_key=idempotency_key, request_fingerprint=request_fingerprint
            )
            return receipt.model_copy(update={"request_fingerprint": "sha256:wrong"})

    executor = PlatformActionExecutor(payloads=Payloads("我在。"), transport=WrongLookupTransport())

    with pytest.raises(ValueError, match="fingerprint"):
        await executor.lookup_result(action())


def event(event_type: str, payload: dict[str, object], suffix: str) -> WorldEvent:
    return WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id=f"event:platform-adapter:{suffix}",
        world_id="world:platform-adapter",
        event_type=event_type,
        logical_time=NOW,
        created_at=NOW,
        actor="test",
        source="test",
        trace_id="trace:platform-adapter",
        causation_id="test",
        correlation_id="conversation:1",
        idempotency_key=f"platform-adapter:{suffix}",
        payload=payload,
    )


@pytest.mark.asyncio
async def test_platform_executor_can_only_send_through_runtime_owned_action_pump() -> None:
    current = action()
    ledger = WorldLedger.in_memory(world_id=current.world_id)
    account = BudgetAccount(account_id="account:chat", category="chat", window_id="test", limit=100)
    reservation = BudgetReservation(
        reservation_id=current.budget_reservation_id,
        account_id=account.account_id,
        action_id=current.action_id,
        category="chat",
        amount_limit=10,
    )
    ledger.commit(
        (
            event(
                "BudgetAccountConfigured", {"account": account.model_dump(mode="json")}, "account"
            ),
            event(
                "BudgetReserved", {"reservation": reservation.model_dump(mode="json")}, "reserve"
            ),
            event("ActionAuthorized", {"action": current.model_dump(mode="json")}, "action"),
        ),
        expected_world_revision=0,
        expected_deliberation_revision=0,
    )
    transport = Transport()
    runtime = WorldRuntime(
        world_id=current.world_id,
        ledger=ledger,
        action_executor=PlatformActionExecutor(payloads=Payloads("我在。"), transport=transport),
        action_pump_owner="pump:platform-test",
    )

    result = await runtime.drain_actions_once()

    assert result is not None and result.status == "settled"
    assert len(transport.sent) == 1
    assert ledger.project().actions[0].state == "delivered"

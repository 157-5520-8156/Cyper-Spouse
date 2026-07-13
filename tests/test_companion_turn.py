from __future__ import annotations

from pathlib import Path
import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from companion_daemon.companion_turn import (
    CompanionTurn,
    DispatchAcceptance,
    ExternalObservation,
    ResponseBudget,
    TurnBeat,
    TurnEnvelope,
    TurnOptions,
    TurnPresentation,
    TurnPresenter,
    TurnTransport,
)
from companion_daemon.db import CompanionStore
from companion_daemon.engine import CompanionEngine, seed_user
from companion_daemon.models import IncomingMessage, MessageAttachment
from companion_daemon.platform_adapter import DeliveryReceipt
from companion_daemon.world import WorldError, WorldKernel


class StaticReplyModel:
    async def complete(self, messages, *, temperature: float) -> str:
        return (
            '{"reply_text":"五个人一起被吓得到处跑，画面感还挺强的。",'
            '"mentioned_event_ids":[],"proposed_action_ids":[],"claims":[]}'
        )


class MultiBeatReplyModel:
    async def complete(self, messages, *, temperature: float) -> str:
        return (
            '{"reply_text":"五个人一起被吓得到处跑，确实很有画面感。'
            '不过我更想知道最后是谁第一个叫出来的？",'
            '"mentioned_event_ids":[],"proposed_action_ids":[],"claims":[]}'
        )


class TimedMultiBeatReplyModel:
    async def complete(self, messages, *, temperature: float) -> str:
        return (
            '{"reply_text":"第一句先到。第二句隔一会儿。",'
            '"expression_beats":[{"text":"第一句先到。","delay_ms":0},'
            '{"text":"第二句隔一会儿。","delay_ms":1200}],'
            '"mentioned_event_ids":[],"proposed_action_ids":[],"claims":[]}'
        )


class RecordingTransport:
    def __init__(self, acceptance: DispatchAcceptance) -> None:
        self.acceptance = acceptance
        self.beats: list[TurnBeat] = []

    async def dispatch(self, beat: TurnBeat) -> DispatchAcceptance:
        self.beats.append(beat)
        return self.acceptance


class LookupTransport(RecordingTransport):
    """An asynchronously accepted transport with durable receipt lookup."""

    def __init__(self, *, lookup_status: str = "delivered") -> None:
        super().__init__(DispatchAcceptance(status="accepted", lookup_token="lookup:turn"))
        self.lookup_status = lookup_status
        self.lookup_tokens: list[str] = []
        self.lookup_complete = asyncio.Event()

    async def lookup_delivery(self, receipt_query_token: str) -> DeliveryReceipt:
        self.lookup_tokens.append(receipt_query_token)
        self.lookup_complete.set()
        beat = self.beats[0]
        return DeliveryReceipt(
            action_id=beat.action_id,
            status=self.lookup_status,  # type: ignore[arg-type]
            external_receipt="lookup:confirmed" if self.lookup_status == "delivered" else None,
        )


class DisconnectingTransport:
    def __init__(self) -> None:
        self.beats: list[TurnBeat] = []

    async def dispatch(self, beat: TurnBeat) -> DispatchAcceptance:
        self.beats.append(beat)
        raise ConnectionError("connection dropped after dispatch")


class SequencedTransport:
    def __init__(self, acceptances: list[DispatchAcceptance]) -> None:
        self.acceptances = list(acceptances)
        self.beats: list[TurnBeat] = []

    async def dispatch(self, beat: TurnBeat) -> DispatchAcceptance:
        self.beats.append(beat)
        return self.acceptances.pop(0)


class SlowReplyModel:
    async def complete(self, messages, *, temperature: float) -> str:
        await asyncio.sleep(0.05)
        return await StaticReplyModel().complete(messages, temperature=temperature)


def test_first_visible_budget_reserves_time_for_timeout_fallback() -> None:
    assert CompanionTurn._generation_timeout_seconds(
        ResponseBudget(first_visible_by_ms=5_000, complete_by_ms=8_000)
    ) == 4.5
    assert CompanionTurn._generation_timeout_seconds(
        ResponseBudget(first_visible_by_ms=5, complete_by_ms=100)
    ) == 0.004


class SlowTransport:
    def __init__(self) -> None:
        self.beats: list[TurnBeat] = []

    async def dispatch(self, beat: TurnBeat) -> DispatchAcceptance:
        self.beats.append(beat)
        await asyncio.sleep(0.5)
        return DispatchAcceptance(status="delivered", external_receipt="too-late")


class BlockingFirstTransport:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.beats: list[TurnBeat] = []

    async def dispatch(self, beat: TurnBeat) -> DispatchAcceptance:
        self.beats.append(beat)
        if len(self.beats) == 1:
            self.started.set()
            await self.release.wait()
        return DispatchAcceptance(
            status="delivered", external_receipt=f"receipt:blocking:{len(self.beats)}"
        )


class RecordingPresenter:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.presentations: list[TurnPresentation] = []

    async def before_text(self, presentation: TurnPresentation) -> None:
        self.events.append("reaction")
        self.presentations.append(presentation)

    async def after_text(self, presentation: TurnPresentation, terminal_state: str) -> None:
        self.events.append(f"after:{terminal_state}")


class BlockingPresenter:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def before_text(self, _presentation: TurnPresentation) -> None:
        self.started.set()
        await self.release.wait()

    async def after_text(self, _presentation: TurnPresentation, _terminal_state: str) -> None:
        return None


def _turn_runtime(
    tmp_path: Path,
    transport: TurnTransport,
    *,
    model: object | None = None,
    presenter: TurnPresenter | None = None,
    cadence_delay_seconds: float = 0.3,
    sleep=None,
) -> tuple[CompanionTurn, WorldKernel, str]:
    store = CompanionStore(tmp_path / "companion-turn.sqlite")
    seed_user(store)
    world = WorldKernel(store)
    world_id = world.start_from_seed_file(Path("configs/world_seed.yaml")).world_id
    engine = CompanionEngine(
        store,
        model or StaticReplyModel(),  # type: ignore[arg-type]
        "你是沈知栀。",
        world_kernel=world,
        world_id=world_id,
    )
    return (
        CompanionTurn(
            engine,
            transport,
            presenter=presenter,
            cadence_delay_seconds=cadence_delay_seconds,
            **({"sleep": sleep} if sleep is not None else {}),
        ),
        world,
        world_id,
    )


def _envelope(message_id: str) -> TurnEnvelope:
    return TurnEnvelope.from_message(
        IncomingMessage(
            platform="qq",
            platform_user_id="geoff",
            message_id=message_id,
            text="今天和朋友玩密室，五个人被吓得到处跑",
        ),
        idempotency_key=f"qq:geoff:{message_id}",
    )


def test_turn_envelope_freezes_adapter_boundary_metadata() -> None:
    envelope = TurnEnvelope.from_message(
        IncomingMessage(
            platform="qq",
            platform_user_id="geoff",
            message_id="latest",
            source_message_ids=["first", "latest", "second"],
            text="前两句合并成这一轮。",
            attachments=[
                MessageAttachment(url="https://example.test/photo.jpg", filename="photo.jpg")
            ],
        ),
        idempotency_key="qq:geoff:latest",
        world_id="world:test",
        canonical_user_id="geoff",
        frozen_cadence="hot",
    )

    assert envelope.message.message_id == "qq:geoff:latest"
    assert envelope.platform == "qq"
    assert envelope.platform_message_ids == ("latest", "first", "second")
    assert envelope.attachment_refs == ("https://example.test/photo.jpg", "photo.jpg")
    assert envelope.world_id == "world:test"
    assert envelope.canonical_user_id == "geoff"
    assert envelope.frozen_cadence == "hot"


@pytest.mark.asyncio
async def test_world_engine_rejects_direct_reply_without_turn_delivery_seam(
    tmp_path: Path,
) -> None:
    runtime, world, world_id = _turn_runtime(
        tmp_path,
        RecordingTransport(
            DispatchAcceptance(status="delivered", external_receipt="qq:receipt:direct")
        ),
    )
    incoming = IncomingMessage(
        platform="qq",
        platform_user_id="geoff",
        message_id="direct-engine-reply",
        text="我今天有点累。",
    )

    with pytest.raises(WorldError, match="CompanionTurn"):
        await runtime.engine.handle_message(incoming, defer_delivery=False)

    snapshot = world.snapshot(world_id)
    assert snapshot["actions"] == {}
    assert snapshot["turns"] == {}


@pytest.mark.asyncio
async def test_respond_owns_world_action_dispatch_and_synchronous_receipt_settlement(
    tmp_path: Path,
) -> None:
    transport = RecordingTransport(
        DispatchAcceptance(
            status="delivered",
            external_receipt="qq:receipt:1",
        )
    )
    runtime, world, world_id = _turn_runtime(tmp_path, transport)

    outcome = await runtime.respond(
        _envelope("turn-delivered"),
        budget=ResponseBudget(first_visible_by_ms=3_000, complete_by_ms=5_000),
    )

    assert outcome.visible_status == "delivered"
    assert outcome.degraded is False
    assert len(transport.beats) == 1
    action = world.snapshot(world_id)["actions"][outcome.action_ids[0]]
    assert action["status"] == "delivered"
    assert action["segment_state"]["segments"][0]["status"] == "delivered"
    assert action["segment_state"]["segments"][0]["external_receipt"] == "qq:receipt:1"


@pytest.mark.asyncio
async def test_simulator_inbound_turn_responds_then_settles_to_world_delivery(
    tmp_path: Path,
) -> None:
    """The local simulator exercises the same deep turn seam as adapters."""
    transport = RecordingTransport(
        DispatchAcceptance(status="accepted", lookup_token="simulator:receipt:1")
    )
    runtime, world, world_id = _turn_runtime(tmp_path, transport)
    incoming = IncomingMessage(
        platform="simulator",
        platform_user_id="geoff",
        message_id="simulator-world-delivery",
        text="今天和朋友玩密室，五个人被吓得到处跑",
    )
    envelope = TurnEnvelope.from_message(
        incoming,
        idempotency_key="simulator:geoff:simulator-world-delivery",
    )

    outcome = await runtime.respond(
        envelope,
        budget=ResponseBudget(first_visible_by_ms=3_000, complete_by_ms=5_000),
    )

    assert outcome.visible_status == "accepted"
    assert len(transport.beats) == 1
    action_id = outcome.action_ids[0]
    action = world.snapshot(world_id)["actions"][action_id]
    assert action["status"] == "sending"
    beat = transport.beats[0]

    settled = await runtime.settle(
        ExternalObservation(
            action_id=beat.action_id,
            delivery_id=beat.delivery_id,
            segment_id=beat.segment_id,
            status="delivered",
            observed_at=incoming.sent_at,
            idempotency_key="simulator:geoff:simulator-world-delivery:receipt:1",
            external_receipt="simulator:delivery:1",
        )
    )

    assert settled.terminal_state == "delivered"
    delivered_action = world.snapshot(world_id)["actions"][action_id]
    assert delivered_action["status"] == "delivered"
    assert delivered_action["segment_state"]["segments"][0]["external_receipt"] == "simulator:delivery:1"


@pytest.mark.asyncio
async def test_settle_records_media_generation_through_the_same_turn_seam(
    tmp_path: Path,
) -> None:
    transport = RecordingTransport(
        DispatchAcceptance(status="delivered", external_receipt="unused")
    )
    runtime, world, world_id = _turn_runtime(tmp_path, transport)
    registered = world.submit(
        {
            "type": "register_user",
            "world_id": world_id,
            "user_id": "user:geoff",
            "name": "geoff",
        },
        expected_revision=world.revision(world_id),
    )
    requested = world.submit(
        {
            "type": "request_media",
            "world_id": world_id,
            "request_id": "turn-seam:media",
            "user_id": "user:geoff",
            "media_kind": "creative_image",
            "topic": "一张插画",
            "reason": "user_requested_creative_image",
        },
        expected_revision=registered.revision,
    )

    settled = await runtime.settle(
        ExternalObservation(
            world_id=world_id,
            action_id="media-generation:turn-seam:media",
            kind="media_result",
            payload={
                "status": "delivered",
                "artifact_path": "assets/life/turn-seam.png",
                "artifact_hash": "abc123",
            },
            observed_at=_envelope("unused-media-observation").observed_at,
            idempotency_key="media-result:turn-seam:1",
        )
    )

    assert settled.terminal_state == "delivered"
    assert settled.committed_revision > requested.revision
    media = world.snapshot(world_id)["media"]["turn-seam:media"]
    assert media["status"] == "generated"
    assert media["artifact_path"] == "assets/life/turn-seam.png"

    duplicate = await runtime.settle(
        ExternalObservation(
            world_id=world_id,
            action_id="media-generation:turn-seam:media",
            kind="media_result",
            payload={
                "status": "delivered",
                "artifact_path": "assets/life/turn-seam.png",
                "artifact_hash": "abc123",
            },
            observed_at=_envelope("unused-media-observation").observed_at,
            idempotency_key="media-result:turn-seam:1",
        )
    )
    assert duplicate.committed_revision == settled.committed_revision


@pytest.mark.asyncio
async def test_settle_records_a_confirmed_tool_result_through_the_same_turn_seam(
    tmp_path: Path,
) -> None:
    transport = RecordingTransport(
        DispatchAcceptance(status="delivered", external_receipt="unused")
    )
    runtime, world, world_id = _turn_runtime(tmp_path, transport)
    registered = world.submit(
        {
            "type": "register_user",
            "world_id": world_id,
            "user_id": "user:geoff",
            "name": "geoff",
        },
        expected_revision=world.revision(world_id),
    )
    proposed = world.propose_tool_action(
        world_id=world_id,
        proposal_id="turn-seam:tool",
        user_id="user:geoff",
        tool_name="calendar.create",
        arguments={"title": "复习"},
        summary="在日历中创建复习日程",
        risk="confirmation_required",
        expected_revision=registered.revision,
    )
    observed = world.submit(
        {
            "type": "observe_user_message",
            "world_id": world_id,
            "message_id": "turn-seam:tool-confirm",
            "user_id": "user:geoff",
            "text": "确认执行",
            "sent_at": _envelope("unused-tool-observation").observed_at.isoformat(),
        },
        expected_revision=proposed.revision,
    )
    authorized = world.authorize_tool_action(
        world_id=world_id,
        proposal_id="turn-seam:tool",
        confirmation_message_id="turn-seam:tool-confirm",
        expected_revision=observed.revision,
    )

    settled = await runtime.settle(
        ExternalObservation(
            world_id=world_id,
            action_id="tool:turn-seam:tool",
            kind="tool_result",
            payload={
                "status": "delivered",
                "execution_mode": "fake",
                "effect_scope": "none",
                "detail": "模拟日程已创建",
                "output": {"event_id": "turn-seam:1"},
            },
            observed_at=_envelope("unused-tool-result").observed_at,
            idempotency_key="tool-result:turn-seam:1",
        )
    )

    assert settled.terminal_state == "delivered"
    assert settled.committed_revision > authorized.revision
    tool = world.snapshot(world_id)["tool_actions"]["turn-seam:tool"]
    assert tool["status"] == "simulated"
    assert tool["result_summary"] == "模拟完成（未执行真实操作）：模拟日程已创建"


@pytest.mark.asyncio
async def test_late_tool_result_reconciles_a_timeout_through_the_same_turn_seam(
    tmp_path: Path,
) -> None:
    transport = RecordingTransport(
        DispatchAcceptance(status="delivered", external_receipt="unused")
    )
    runtime, world, world_id = _turn_runtime(tmp_path, transport)
    registered = world.submit(
        {
            "type": "register_user",
            "world_id": world_id,
            "user_id": "user:geoff",
            "name": "geoff",
        },
        expected_revision=world.revision(world_id),
    )
    proposed = world.propose_tool_action(
        world_id=world_id,
        proposal_id="turn-seam:late-tool",
        user_id="user:geoff",
        tool_name="calendar.create",
        arguments={"title": "复习"},
        summary="在日历中创建复习日程",
        risk="confirmation_required",
        expected_revision=registered.revision,
    )
    observed = world.submit(
        {
            "type": "observe_user_message",
            "world_id": world_id,
            "message_id": "turn-seam:late-tool-confirm",
            "user_id": "user:geoff",
            "text": "确认执行",
            "sent_at": _envelope("unused-late-tool-observation").observed_at.isoformat(),
        },
        expected_revision=proposed.revision,
    )
    authorized = world.authorize_tool_action(
        world_id=world_id,
        proposal_id="turn-seam:late-tool",
        confirmation_message_id="turn-seam:late-tool-confirm",
        expected_revision=observed.revision,
    )
    action_id = "tool:turn-seam:late-tool"

    timed_out = await runtime.settle(
        ExternalObservation(
            world_id=world_id,
            action_id=action_id,
            kind="timeout",
            payload={"reason": "adapter callback deadline elapsed"},
            observed_at=_envelope("unused-late-tool-timeout").observed_at,
            idempotency_key="tool-timeout:turn-seam:1",
        )
    )
    assert timed_out.terminal_state == "unknown"
    assert world.snapshot(world_id)["actions"][action_id]["status"] == "unknown"

    reconciled = await runtime.settle(
        ExternalObservation(
            world_id=world_id,
            action_id=action_id,
            kind="tool_result",
            payload={
                "status": "delivered",
                "execution_mode": "fake",
                "effect_scope": "none",
                "detail": "模拟日程已创建",
                "output": {"event_id": "turn-seam:late"},
            },
            observed_at=_envelope("unused-late-tool-result").observed_at,
            idempotency_key="tool-result:turn-seam:late",
        )
    )

    assert reconciled.terminal_state == "delivered"
    assert reconciled.committed_revision > authorized.revision
    assert world.snapshot(world_id)["actions"][action_id]["status"] == "delivered"


@pytest.mark.asyncio
async def test_dispatch_scheduled_afterthought_uses_the_normal_receipt_path(
    tmp_path: Path,
) -> None:
    transport = RecordingTransport(
        DispatchAcceptance(status="delivered", external_receipt="qq:afterthought:1")
    )
    runtime, world, world_id = _turn_runtime(tmp_path, transport)
    delivery_id = runtime.engine.queue_afterthought_delivery("geoff", "qq", "哦对，补一句。")
    action_id = world.action_id_for_delivery(world_id, delivery_id)
    assert action_id is not None

    outcome = await runtime.dispatch_scheduled(
        action_id=action_id,
        delivery_id=delivery_id,
        observed_at=datetime.now(UTC),
        idempotency_key="afterthought:turn-seam",
    )

    assert outcome.terminal_state == "delivered"
    assert transport.beats[0].action_id == action_id
    assert world.snapshot(world_id)["actions"][action_id]["status"] == "delivered"


@pytest.mark.asyncio
async def test_async_platform_acceptance_is_not_delivered_until_settle_observes_receipt(
    tmp_path: Path,
) -> None:
    transport = RecordingTransport(
        DispatchAcceptance(status="accepted", lookup_token="qq:lookup:2")
    )
    runtime, world, world_id = _turn_runtime(tmp_path, transport)

    outcome = await runtime.respond(
        _envelope("turn-accepted"),
        budget=ResponseBudget(first_visible_by_ms=3_000, complete_by_ms=5_000),
    )

    assert outcome.visible_status == "accepted"
    beat = transport.beats[0]
    action_before = world.snapshot(world_id)["actions"][outcome.action_ids[0]]
    assert action_before["status"] != "delivered"
    assert action_before["segment_state"]["segments"][0]["receipt_lookup_token"] == ("qq:lookup:2")

    settled = await runtime.settle(
        ExternalObservation(
            action_id=beat.action_id,
            delivery_id=beat.delivery_id,
            segment_id=beat.segment_id,
            status="delivered",
            observed_at=_envelope("unused").observed_at,
            external_receipt="qq:receipt:2",
            idempotency_key="qq:receipt:2",
        )
    )

    assert settled.terminal_state == "delivered"
    action_after = world.snapshot(world_id)["actions"][outcome.action_ids[0]]
    assert action_after["status"] == "delivered"


@pytest.mark.asyncio
async def test_accepted_receipt_lookup_settles_before_becoming_unknown(tmp_path: Path) -> None:
    """The transport's durable lookup completes an accepted Action at its deadline."""

    async def no_wait(_seconds: float) -> None:
        return None

    transport = LookupTransport()
    runtime, world, world_id = _turn_runtime(tmp_path, transport, sleep=no_wait)

    outcome = await runtime.respond(
        _envelope("turn-lookup-delivered"),
        budget=ResponseBudget(first_visible_by_ms=3_000, complete_by_ms=5_000),
    )

    assert outcome.visible_status == "accepted"
    await asyncio.wait_for(transport.lookup_complete.wait(), timeout=1)
    action = world.snapshot(world_id)["actions"][outcome.action_ids[0]]
    assert transport.lookup_tokens == ["lookup:turn"]
    assert action["status"] == "delivered"
    assert action["segment_state"]["segments"][0]["external_receipt"] == "lookup:confirmed"


@pytest.mark.asyncio
async def test_reconstructed_turn_recovers_persisted_accepted_receipt(tmp_path: Path) -> None:
    """A restarted process can settle an already accepted receipt using its token."""

    release_initial_watchdog = asyncio.Event()

    async def wait_until_released(_seconds: float) -> None:
        await release_initial_watchdog.wait()

    transport = LookupTransport()
    initial, world, world_id = _turn_runtime(
        tmp_path, transport, sleep=wait_until_released
    )
    outcome = await initial.respond(
        _envelope("turn-restart-lookup"),
        budget=ResponseBudget(first_visible_by_ms=3_000, complete_by_ms=5_000),
    )
    assert outcome.visible_status == "accepted"
    assert transport.lookup_tokens == []
    # This is the production startup hook.  A persisted accepted token must
    # survive lease recovery so the reconstructed CompanionTurn can query it.
    assert (
        world.recover_interrupted_outgoing_deliveries(
            world_id, observed_now=datetime.now(UTC) + timedelta(hours=1)
        )
        == 0
    )
    assert world.snapshot(world_id)["actions"][outcome.action_ids[0]]["status"] == "sending"

    async def no_wait(_seconds: float) -> None:
        return None

    recovered = CompanionTurn(initial.engine, transport, sleep=no_wait)
    await asyncio.wait_for(transport.lookup_complete.wait(), timeout=1)
    action = world.snapshot(world_id)["actions"][outcome.action_ids[0]]
    assert transport.lookup_tokens == ["lookup:turn"]
    assert action["status"] == "delivered"

    # The original in-memory watchdog must be allowed to observe the now-terminal
    # segment and exit rather than leaving a pending task in the test loop.
    release_initial_watchdog.set()
    await asyncio.sleep(0)
    del recovered


@pytest.mark.asyncio
async def test_reconstructed_turn_reconciles_a_previously_unknown_receipt(tmp_path: Path) -> None:
    """A later durable receipt can resolve an Action already marked unknown."""

    async def no_wait(_seconds: float) -> None:
        return None

    transport = LookupTransport(lookup_status="unknown")
    initial, world, world_id = _turn_runtime(tmp_path, transport, sleep=no_wait)
    outcome = await initial.respond(
        _envelope("turn-reconcile-unknown"),
        budget=ResponseBudget(first_visible_by_ms=3_000, complete_by_ms=5_000),
    )
    await asyncio.wait_for(transport.lookup_complete.wait(), timeout=1)
    assert world.snapshot(world_id)["actions"][outcome.action_ids[0]]["status"] == "unknown"

    transport.lookup_status = "delivered"
    transport.lookup_complete.clear()
    recovered = CompanionTurn(initial.engine, transport, sleep=no_wait)
    await asyncio.wait_for(transport.lookup_complete.wait(), timeout=1)
    action = world.snapshot(world_id)["actions"][outcome.action_ids[0]]
    assert action["status"] == "delivered"
    assert transport.lookup_tokens == ["lookup:turn", "lookup:turn"]
    del recovered


@pytest.mark.asyncio
async def test_ambiguous_transport_failure_is_unknown_and_never_false_delivered(
    tmp_path: Path,
) -> None:
    transport = DisconnectingTransport()
    runtime, world, world_id = _turn_runtime(tmp_path, transport)

    outcome = await runtime.respond(
        _envelope("turn-unknown"),
        budget=ResponseBudget(first_visible_by_ms=3_000, complete_by_ms=5_000),
    )

    assert outcome.visible_status == "unknown"
    action = world.snapshot(world_id)["actions"][outcome.action_ids[0]]
    assert action["status"] != "delivered"
    assert action["segment_state"]["segments"][0]["status"] == "unknown"


@pytest.mark.asyncio
async def test_replayed_idempotency_envelope_never_reruns_model_or_transport(
    tmp_path: Path,
) -> None:
    class CountingModel:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, messages, *, temperature: float) -> str:
            self.calls += 1
            return await StaticReplyModel().complete(messages, temperature=temperature)

    transport = RecordingTransport(
        DispatchAcceptance(status="delivered", external_receipt="qq:receipt:dedupe")
    )
    model = CountingModel()
    runtime, _, _ = _turn_runtime(tmp_path, transport, model=model)
    envelope = _envelope("turn-deduplicated")

    first = await runtime.respond(
        envelope,
        budget=ResponseBudget(first_visible_by_ms=3_000, complete_by_ms=5_000),
    )
    second = await runtime.respond(
        envelope,
        budget=ResponseBudget(first_visible_by_ms=3_000, complete_by_ms=5_000),
    )

    assert second == first
    assert model.calls == 1
    assert len(transport.beats) == 1


@pytest.mark.asyncio
async def test_same_message_id_on_another_platform_is_not_deduplicated(
    tmp_path: Path,
) -> None:
    transport = RecordingTransport(
        DispatchAcceptance(status="delivered", external_receipt="receipt:platform")
    )
    runtime, _, _ = _turn_runtime(tmp_path, transport)
    qq = _envelope("shared-platform-id")
    simulator = TurnEnvelope.from_message(
        IncomingMessage(
            platform="simulator",
            platform_user_id="geoff",
            message_id="shared-platform-id",
            text="这是另一平台上的独立消息",
        ),
        idempotency_key="simulator:geoff:shared-platform-id",
    )

    await runtime.respond(
        qq, budget=ResponseBudget(first_visible_by_ms=3_000, complete_by_ms=5_000)
    )
    await runtime.respond(
        simulator,
        budget=ResponseBudget(first_visible_by_ms=3_000, complete_by_ms=5_000),
    )

    assert len(transport.beats) == 2


@pytest.mark.asyncio
async def test_persisted_completion_deadline_cancels_only_late_planned_beats(
    tmp_path: Path,
) -> None:
    transport = SequencedTransport(
        [DispatchAcceptance(status="accepted", lookup_token="lookup:deadline")]
    )
    runtime, world, world_id = _turn_runtime(tmp_path, transport, model=MultiBeatReplyModel())
    outcome = await runtime.respond(
        _envelope("turn-completion-deadline"),
        budget=ResponseBudget(first_visible_by_ms=400, complete_by_ms=500),
    )
    action_before = world.snapshot(world_id)["actions"][outcome.action_ids[0]]
    assert action_before["complete_by_observed_at"]
    first = transport.beats[0]
    await asyncio.sleep(0.55)
    timed_out = world.snapshot(world_id)["actions"][outcome.action_ids[0]]
    assert [item["status"] for item in timed_out["segment_state"]["segments"]] == [
        "unknown",
        "cancelled",
    ]

    settled = await runtime.settle(
        ExternalObservation(
            action_id=first.action_id,
            delivery_id=first.delivery_id,
            segment_id=first.segment_id,
            status="delivered",
            observed_at=_envelope("unused-deadline").observed_at,
            external_receipt="receipt:deadline:first",
            idempotency_key="receipt:deadline:first",
        )
    )

    assert settled.terminal_state == "cancelled"
    assert len(transport.beats) == 1
    action_after = world.snapshot(world_id)["actions"][outcome.action_ids[0]]
    assert action_after["status"] == "cancelled"
    assert [item["status"] for item in action_after["segment_state"]["segments"]] == [
        "delivered",
        "cancelled",
    ]


@pytest.mark.asyncio
async def test_takeover_before_first_receipt_prevents_later_followup(
    tmp_path: Path,
) -> None:
    transport = SequencedTransport(
        [
            DispatchAcceptance(status="accepted", lookup_token="lookup:before-takeover"),
            DispatchAcceptance(status="delivered", external_receipt="receipt:new"),
        ]
    )
    runtime, world, world_id = _turn_runtime(tmp_path, transport, model=MultiBeatReplyModel())
    first = await runtime.respond(
        _envelope("turn-accepted-before-takeover"),
        budget=ResponseBudget(first_visible_by_ms=3_000, complete_by_ms=5_000),
    )
    first_beat = transport.beats[0]

    await runtime.respond(
        _envelope("turn-takeover-before-receipt"),
        budget=ResponseBudget(first_visible_by_ms=3_000, complete_by_ms=5_000),
    )
    interrupted = world.snapshot(world_id)["actions"][first.action_ids[0]]
    assert interrupted["segment_state"]["segments"][1]["status"] == "cancelled"

    settled = await runtime.settle(
        ExternalObservation(
            action_id=first_beat.action_id,
            delivery_id=first_beat.delivery_id,
            segment_id=first_beat.segment_id,
            status="delivered",
            observed_at=_envelope("unused-takeover-receipt").observed_at,
            external_receipt="receipt:late-first",
            idempotency_key="receipt:late-first",
        )
    )

    assert settled.terminal_state == "cancelled"
    assert [beat.action_id for beat in transport.beats].count(first_beat.action_id) == 1


@pytest.mark.asyncio
async def test_concurrent_duplicate_receipts_are_serialized(tmp_path: Path) -> None:
    transport = RecordingTransport(
        DispatchAcceptance(status="accepted", lookup_token="lookup:concurrent")
    )
    runtime, world, world_id = _turn_runtime(tmp_path, transport)
    outcome = await runtime.respond(
        _envelope("turn-concurrent-receipt"),
        budget=ResponseBudget(first_visible_by_ms=3_000, complete_by_ms=5_000),
    )
    beat = transport.beats[0]
    observation = ExternalObservation(
        action_id=beat.action_id,
        delivery_id=beat.delivery_id,
        segment_id=beat.segment_id,
        status="delivered",
        observed_at=_envelope("unused-concurrent").observed_at,
        external_receipt="receipt:concurrent",
        idempotency_key="receipt:concurrent",
    )

    results = await asyncio.gather(runtime.settle(observation), runtime.settle(observation))

    assert [item.terminal_state for item in results] == ["delivered", "delivered"]
    action = world.snapshot(world_id)["actions"][outcome.action_ids[0]]
    assert action["status"] == "delivered"


@pytest.mark.asyncio
async def test_takeover_waits_for_initial_dispatch_then_cancels_old_remainder(
    tmp_path: Path,
) -> None:
    transport = BlockingFirstTransport()
    runtime, world, world_id = _turn_runtime(tmp_path, transport, model=MultiBeatReplyModel())
    first_task = asyncio.create_task(
        runtime.respond(
            _envelope("turn-blocked-first-dispatch"),
            budget=ResponseBudget(first_visible_by_ms=3_000, complete_by_ms=5_000),
        )
    )
    await transport.started.wait()
    takeover_task = asyncio.create_task(
        runtime.respond(
            _envelope("turn-concurrent-takeover"),
            budget=ResponseBudget(first_visible_by_ms=3_000, complete_by_ms=5_000),
        )
    )
    await asyncio.sleep(0)
    assert not takeover_task.done()

    transport.release.set()
    first, _ = await asyncio.gather(first_task, takeover_task)

    old_action = world.snapshot(world_id)["actions"][first.action_ids[0]]
    assert [item["status"] for item in old_action["segment_state"]["segments"]] == [
        "delivered",
        "cancelled",
    ]
    assert [beat.action_id for beat in transport.beats].count(first.action_ids[0]) == 1


@pytest.mark.asyncio
async def test_public_interrupt_cancels_remainder_before_next_turn_flush(
    tmp_path: Path,
) -> None:
    transport = RecordingTransport(
        DispatchAcceptance(status="delivered", external_receipt="receipt:interrupt")
    )
    runtime, world, world_id = _turn_runtime(tmp_path, transport, model=MultiBeatReplyModel())
    outcome = await runtime.respond(
        _envelope("turn-public-interrupt"),
        budget=ResponseBudget(first_visible_by_ms=3_000, complete_by_ms=5_000),
    )

    cancelled = await runtime.interrupt(
        _envelope("turn-public-interrupt-takeover"), kind="substantive"
    )

    assert cancelled == (f"{outcome.action_ids[0]}:segment:1",)
    action = world.snapshot(world_id)["actions"][outcome.action_ids[0]]
    assert action["segment_state"]["segments"][1]["status"] == "cancelled"


@pytest.mark.asyncio
async def test_turn_options_reach_generation_without_entering_transport_interface(
    tmp_path: Path,
) -> None:
    transport = RecordingTransport(
        DispatchAcceptance(status="delivered", external_receipt="receipt:options")
    )
    runtime, _, _ = _turn_runtime(tmp_path, transport)
    original = runtime.engine.handle_message
    observed: dict[str, object] = {}

    async def recording_handle(message: IncomingMessage, **kwargs: object):
        observed.update(kwargs)
        return await original(message, **kwargs)

    runtime.engine.handle_message = recording_handle  # type: ignore[method-assign]
    frozen = runtime.engine.freeze_turn_context(_envelope("turn-options").message)
    await runtime.respond(
        _envelope("turn-options"),
        budget=ResponseBudget(first_visible_by_ms=3_000, complete_by_ms=5_000),
        options=TurnOptions(context_hint="刚刚忙完。", turn_context=frozen),
    )

    assert observed["context_hint"] == "刚刚忙完。"
    assert observed["turn_context"] is frozen


@pytest.mark.asyncio
async def test_presentation_wraps_all_text_beats_and_only_finishes_at_terminal(
    tmp_path: Path,
) -> None:
    events: list[str] = []

    class EventTransport(SequencedTransport):
        async def dispatch(self, beat: TurnBeat) -> DispatchAcceptance:
            events.append(f"text:{beat.position}")
            return await super().dispatch(beat)

    transport = EventTransport(
        [
            DispatchAcceptance(status="delivered", external_receipt="receipt:presentation:1"),
            DispatchAcceptance(status="delivered", external_receipt="receipt:presentation:2"),
        ]
    )
    presenter = RecordingPresenter(events)
    runtime, _, _ = _turn_runtime(
        tmp_path,
        transport,
        model=MultiBeatReplyModel(),
        presenter=presenter,
        cadence_delay_seconds=0,
    )

    outcome = await runtime.respond(
        _envelope("turn-presentation"),
        budget=ResponseBudget(first_visible_by_ms=3_000, complete_by_ms=5_000),
    )
    await runtime.wait_for_delivery_continuations()

    assert outcome.visible_status == "delivered"
    assert events == ["reaction", "text:0", "text:1", "after:delivered"]
    presentation = presenter.presentations[0]
    assert presentation.action_id == outcome.action_ids[0]
    assert presentation.incoming.message_id == "qq:geoff:turn-presentation"


@pytest.mark.asyncio
async def test_v2_continuation_uses_persisted_model_beat_delay(tmp_path: Path) -> None:
    delays: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        delays.append(seconds)

    transport = SequencedTransport(
        [
            DispatchAcceptance(status="delivered", external_receipt="receipt:timed:1"),
            DispatchAcceptance(status="delivered", external_receipt="receipt:timed:2"),
        ]
    )
    runtime, _, _ = _turn_runtime(
        tmp_path,
        transport,
        model=TimedMultiBeatReplyModel(),
        sleep=fake_sleep,
    )

    await runtime.respond(
        _envelope("turn-model-delay"),
        budget=ResponseBudget(first_visible_by_ms=3_000, complete_by_ms=5_000),
    )
    await runtime.wait_for_delivery_continuations()

    assert [beat.position for beat in transport.beats] == [0, 1]
    assert transport.beats[1].delay_before_ms == 1200
    assert delays == [1.2]


@pytest.mark.asyncio
async def test_v2_interruption_cancels_a_delayed_beat_without_waiting_for_its_sleep(
    tmp_path: Path,
) -> None:
    class HeldSleep:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def __call__(self, _seconds: float) -> None:
            self.started.set()
            await self.release.wait()

    held_sleep = HeldSleep()
    transport = SequencedTransport(
        [
            DispatchAcceptance(status="delivered", external_receipt="receipt:interrupt-delay:1"),
            DispatchAcceptance(status="delivered", external_receipt="receipt:interrupt-delay:2"),
        ]
    )
    runtime, world, world_id = _turn_runtime(
        tmp_path,
        transport,
        model=TimedMultiBeatReplyModel(),
        sleep=held_sleep,
    )
    outcome = await runtime.respond(
        _envelope("turn-delay-interrupt"),
        budget=ResponseBudget(first_visible_by_ms=3_000, complete_by_ms=5_000),
    )
    await held_sleep.started.wait()

    cancelled = await runtime.interrupt(
        _envelope("turn-delay-interrupt-takeover"), kind="substantive"
    )
    held_sleep.release.set()
    await runtime.wait_for_delivery_continuations()

    assert cancelled == (f"{outcome.action_ids[0]}:segment:1",)
    assert len(transport.beats) == 1
    action = world.snapshot(world_id)["actions"][outcome.action_ids[0]]
    assert action["segment_state"]["segments"][1]["status"] == "cancelled"


@pytest.mark.asyncio
async def test_v2_drops_a_model_delayed_remainder_that_cannot_fit_the_deadline(
    tmp_path: Path,
) -> None:
    transport = SequencedTransport(
        [DispatchAcceptance(status="delivered", external_receipt="receipt:deadline-delay:1")]
    )
    runtime, world, world_id = _turn_runtime(
        tmp_path, transport, model=TimedMultiBeatReplyModel()
    )
    outcome = await runtime.respond(
        _envelope("turn-delay-deadline"),
        budget=ResponseBudget(first_visible_by_ms=300, complete_by_ms=500),
    )
    await runtime.wait_for_delivery_continuations()

    action = world.snapshot(world_id)["actions"][outcome.action_ids[0]]
    assert len(transport.beats) == 1
    assert action["segment_state"]["segments"][1]["status"] == "cancelled"


@pytest.mark.asyncio
async def test_settle_rejects_a_receipt_for_the_wrong_action_reference(
    tmp_path: Path,
) -> None:
    transport = RecordingTransport(
        DispatchAcceptance(status="accepted", lookup_token="lookup:ownership")
    )
    runtime, world, world_id = _turn_runtime(tmp_path, transport)
    outcome = await runtime.respond(
        _envelope("turn-ownership"),
        budget=ResponseBudget(first_visible_by_ms=3_000, complete_by_ms=5_000),
    )
    beat = transport.beats[0]

    with pytest.raises(WorldError, match="does not belong"):
        await runtime.settle(
            ExternalObservation(
                action_id="outgoing:not-this-action",
                delivery_id=beat.delivery_id,
                segment_id=beat.segment_id,
                status="delivered",
                observed_at=_envelope("unused-ownership").observed_at,
                external_receipt="receipt:wrong-owner",
                idempotency_key="receipt:wrong-owner",
            )
        )

    action = world.snapshot(world_id)["actions"][outcome.action_ids[0]]
    assert action["status"] != "delivered"


@pytest.mark.asyncio
async def test_settle_does_not_turn_one_late_receipt_into_multiple_unclaimed_beats(
    tmp_path: Path,
) -> None:
    runtime, world, world_id = _turn_runtime(
        tmp_path,
        RecordingTransport(
            DispatchAcceptance(status="accepted", lookup_token="lookup:forensic")
        ),
    )
    delivery_id, _, action_id = world.queue_outgoing_action(
        canonical_user_id="geoff",
        platform="qq",
        text="第一段。第二段。",
        text_parts=["第一段。", "第二段。"],
        kind="reply",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        trace={
            "world_id": world_id,
            "appraisal": "test",
            "expression_policy": "test",
            "allowed_facts": [],
            "short_lived_constraint": None,
            "observable_reason": "test",
        },
    )
    world.begin_outgoing_action(
        delivery_id, expected_revision=world.revision(world_id)
    )
    world.mark_outgoing_unknown(
        delivery_id,
        reason="process crashed after transport handoff",
        expected_revision=world.revision(world_id),
    )
    first_segment = world.snapshot(world_id)["actions"][action_id]["segment_state"][
        "segments"
    ][0]

    with pytest.raises(
        WorldError,
        match="operator reconciliation requires an unknown segment",
    ):
        await runtime.settle(
            ExternalObservation(
                action_id=action_id,
                delivery_id=delivery_id,
                segment_id=first_segment["segment_id"],
                status="delivered",
                observed_at=datetime.now(UTC),
                external_receipt="qq:ambiguous-late",
                idempotency_key="qq:ambiguous-late",
                reconciliation_evidence={
                    "kind": "operator_verification",
                    "reference": "qq:ambiguous-late",
                    "reviewer_id": "ops-test",
                    "review_note": "多个未发段，不能从一张回执推断全部送达。",
                },
                settlement_origin="operator_reconciliation",
            ),
        )

    action = world.snapshot(world_id)["actions"][action_id]
    assert action["status"] == "unknown"
    assert [part["status"] for part in action["segment_state"]["segments"]] == [
        "planned",
        "planned",
    ]


@pytest.mark.asyncio
async def test_adapter_receipt_cannot_claim_operator_reconciliation_powers(
    tmp_path: Path,
) -> None:
    transport = RecordingTransport(
        DispatchAcceptance(status="accepted", lookup_token="lookup:adapter-boundary")
    )
    runtime, world, world_id = _turn_runtime(tmp_path, transport)
    outcome = await runtime.respond(
        _envelope("turn-no-faux-operator"),
        budget=ResponseBudget(first_visible_by_ms=3_000, complete_by_ms=5_000),
    )
    beat = transport.beats[0]

    with pytest.raises(WorldError, match="only operator reconciliation"):
        await runtime.settle(
            ExternalObservation(
                action_id=beat.action_id,
                delivery_id=beat.delivery_id,
                segment_id=beat.segment_id,
                status="delivered",
                observed_at=datetime.now(UTC),
                external_receipt="qq:adapter-boundary",
                idempotency_key="qq:adapter-boundary",
                reconciliation_evidence={
                    "reference": "qq:adapter-boundary",
                    "reviewer_id": "spoofed",
                    "review_note": "adapter may not self-authorize a manual reconciliation",
                },
            )
        )

    action = world.snapshot(world_id)["actions"][outcome.action_ids[0]]
    assert action["status"] == "sending"


@pytest.mark.asyncio
async def test_async_first_receipt_resumes_the_next_planned_expression_beat(
    tmp_path: Path,
) -> None:
    transport = SequencedTransport(
        [
            DispatchAcceptance(status="accepted", lookup_token="lookup:beat:1"),
            DispatchAcceptance(status="delivered", external_receipt="receipt:beat:2"),
        ]
    )
    runtime, world, world_id = _turn_runtime(tmp_path, transport, model=MultiBeatReplyModel())
    outcome = await runtime.respond(
        _envelope("turn-multi-accepted"),
        budget=ResponseBudget(first_visible_by_ms=3_000, complete_by_ms=5_000),
    )

    assert outcome.visible_status == "accepted"
    assert len(transport.beats) == 1
    first = transport.beats[0]
    settled = await runtime.settle(
        ExternalObservation(
            action_id=first.action_id,
            delivery_id=first.delivery_id,
            segment_id=first.segment_id,
            status="delivered",
            observed_at=_envelope("unused-multi").observed_at,
            external_receipt="receipt:beat:1",
            idempotency_key="receipt:beat:1",
        )
    )

    assert len(transport.beats) == 2
    assert settled.terminal_state == "delivered"
    action = world.snapshot(world_id)["actions"][outcome.action_ids[0]]
    assert action["status"] == "delivered"


@pytest.mark.asyncio
async def test_generation_timeout_still_delivers_a_ledgered_minimal_first_beat(tmp_path: Path) -> None:
    transport = RecordingTransport(
        DispatchAcceptance(status="delivered", external_receipt="unused")
    )
    runtime, world, world_id = _turn_runtime(tmp_path, transport, model=SlowReplyModel())

    outcome = await runtime.respond(
        _envelope("turn-generation-timeout"),
        budget=ResponseBudget(first_visible_by_ms=5, complete_by_ms=100),
    )

    assert outcome.visible_status == "delivered"
    assert outcome.degraded is True
    assert outcome.degradation_reason == "first_visible_timeout"
    assert [beat.text for beat in transport.beats] == [
        "我在这儿；刚才没接好的地方，我不会装作已经懂了。"
    ]
    action = world.snapshot(world_id)["actions"][outcome.action_ids[0]]
    assert action["status"] == "delivered"


@pytest.mark.asyncio
async def test_generation_timeout_acknowledges_a_current_disclosure(tmp_path: Path) -> None:
    transport = RecordingTransport(
        DispatchAcceptance(status="delivered", external_receipt="unused")
    )
    runtime, world, world_id = _turn_runtime(tmp_path, transport, model=SlowReplyModel())
    envelope = TurnEnvelope.from_message(
        IncomingMessage(
            platform="qq",
            platform_user_id="geoff",
            message_id="turn-timeout-current-disclosure",
            text="我明天考试。",
        ),
        idempotency_key="qq:geoff:turn-timeout-current-disclosure",
    )

    outcome = await runtime.respond(
        envelope,
        budget=ResponseBudget(first_visible_by_ms=5, complete_by_ms=100),
    )

    assert outcome.visible_status == "delivered"
    assert [beat.text for beat in transport.beats] == [
        "你提到“考试”。这句话我接到了，也不会替你把程度说重。"
    ]
    action = world.snapshot(world_id)["actions"][outcome.action_ids[0]]
    assert action["status"] == "delivered"


@pytest.mark.asyncio
async def test_transport_timeout_converges_claimed_action_to_unknown(
    tmp_path: Path,
) -> None:
    transport = SlowTransport()
    runtime, world, world_id = _turn_runtime(tmp_path, transport)

    outcome = await runtime.respond(
        _envelope("turn-transport-timeout"),
        budget=ResponseBudget(first_visible_by_ms=300, complete_by_ms=1_000),
    )

    assert outcome.visible_status == "unknown"
    action = world.snapshot(world_id)["actions"][outcome.action_ids[0]]
    assert action["status"] == "unknown"
    assert action["segment_state"]["segments"][0]["status"] == "unknown"


@pytest.mark.asyncio
async def test_repeated_delivery_observation_is_a_revision_stable_noop(
    tmp_path: Path,
) -> None:
    transport = RecordingTransport(
        DispatchAcceptance(status="accepted", lookup_token="lookup:idempotent")
    )
    runtime, world, world_id = _turn_runtime(tmp_path, transport)
    outcome = await runtime.respond(
        _envelope("turn-idempotent-receipt"),
        budget=ResponseBudget(first_visible_by_ms=3_000, complete_by_ms=5_000),
    )
    beat = transport.beats[0]
    observation = ExternalObservation(
        action_id=beat.action_id,
        delivery_id=beat.delivery_id,
        segment_id=beat.segment_id,
        status="delivered",
        observed_at=_envelope("unused-idempotent").observed_at,
        external_receipt="receipt:idempotent",
        idempotency_key="receipt:idempotent",
    )

    first = await runtime.settle(observation)
    revision = world.revision(world_id)
    history_size = len(world.snapshot(world_id)["recent_messages"])
    second = await runtime.settle(observation)

    assert first.terminal_state == second.terminal_state == "delivered"
    assert world.revision(world_id) == revision
    assert len(world.snapshot(world_id)["recent_messages"]) == history_size
    assert outcome.action_ids == (beat.action_id,)


@pytest.mark.asyncio
async def test_substantive_next_turn_cancels_unsent_beats_before_new_deliberation(
    tmp_path: Path,
) -> None:
    transport = SequencedTransport(
        [
            DispatchAcceptance(status="delivered", external_receipt="receipt:first"),
            DispatchAcceptance(status="delivered", external_receipt="receipt:new-turn"),
        ]
    )
    runtime, world, world_id = _turn_runtime(tmp_path, transport, model=MultiBeatReplyModel())
    first = await runtime.respond(
        _envelope("turn-before-takeover"),
        budget=ResponseBudget(first_visible_by_ms=3_000, complete_by_ms=5_000),
    )

    first_action = world.snapshot(world_id)["actions"][first.action_ids[0]]
    assert [segment["status"] for segment in first_action["segment_state"]["segments"]] == [
        "delivered",
        "planned",
    ]

    await runtime.respond(
        _envelope("turn-substantive-takeover"),
        budget=ResponseBudget(first_visible_by_ms=3_000, complete_by_ms=5_000),
    )

    interrupted = world.snapshot(world_id)["actions"][first.action_ids[0]]
    assert [segment["status"] for segment in interrupted["segment_state"]["segments"]] == [
        "delivered",
        "cancelled",
    ]
    assert len(transport.beats) == 2


@pytest.mark.asyncio
async def test_interrupt_cancels_a_planned_action_while_presentation_is_pending(
    tmp_path: Path,
) -> None:
    presenter = BlockingPresenter()
    runtime, world, world_id = _turn_runtime(
        tmp_path,
        RecordingTransport(
            DispatchAcceptance(status="delivered", external_receipt="should-not-send")
        ),
        presenter=presenter,
    )
    response = asyncio.create_task(
        runtime.respond(
            _envelope("turn-pre-dispatch"),
            budget=ResponseBudget(first_visible_by_ms=3_000, complete_by_ms=5_000),
        )
    )
    await presenter.started.wait()

    cancelled = await runtime.interrupt(_envelope("turn-pre-dispatch-takeover"), kind="substantive")
    action_id = next(
        identifier
        for identifier, action in world.snapshot(world_id)["actions"].items()
        if action.get("kind") == "outgoing_message"
    )
    action = world.snapshot(world_id)["actions"][action_id]

    assert cancelled == (f"{action_id}:segment:0",)
    assert action["status"] == "cancelled"
    assert action["segment_state"]["segments"][0]["status"] == "cancelled"
    response.cancel()
    with pytest.raises(asyncio.CancelledError):
        await response


@pytest.mark.asyncio
async def test_expired_observation_skips_attachment_analysis_and_model_work(
    tmp_path: Path,
) -> None:
    class FailingAnalyzer:
        async def analyze(self, _attachment: MessageAttachment) -> object:
            raise AssertionError("expired observation must not analyze attachments")

    store = CompanionStore(tmp_path / "expired-observation.sqlite")
    seed_user(store)
    world = WorldKernel(store)
    world_id = world.start_from_seed_file(Path("configs/world_seed.yaml")).world_id
    engine = CompanionEngine(
        store,
        StaticReplyModel(),
        "你是沈知栀。",
        world_kernel=world,
        world_id=world_id,
        multimodal_analyzer=FailingAnalyzer(),  # type: ignore[arg-type]
    )
    runtime = CompanionTurn(
        engine,
        RecordingTransport(DispatchAcceptance(status="delivered", external_receipt="unused")),
    )
    envelope = TurnEnvelope.from_message(
        IncomingMessage(
            platform="qq",
            platform_user_id="geoff",
            message_id="expired-with-attachment",
            text="看这个。",
            attachments=[MessageAttachment(kind="image", filename="photo.png")],
        ),
        idempotency_key="qq:geoff:expired-with-attachment",
    )

    outcome = await runtime.observe_expired(envelope)

    assert outcome.degradation_reason == "response_budget_exhausted"
    assert (
        world.snapshot(world_id)["turns"]["qq:geoff:expired-with-attachment"]["status"]
        == "deferred"
    )


@pytest.mark.asyncio
async def test_observe_only_records_a_normal_inbound_turn_without_staging_a_reply(
    tmp_path: Path,
) -> None:
    transport = RecordingTransport(
        DispatchAcceptance(status="delivered", external_receipt="unused")
    )
    runtime, world, world_id = _turn_runtime(tmp_path, transport)
    envelope = _envelope("observe-only")

    outcome = await runtime.observe_only(envelope, mark_unread=False)
    replay = await runtime.observe_only(envelope, mark_unread=False)

    assert outcome.visible_status == "observed"
    assert outcome.action_ids == ()
    assert replay.visible_status == "observed"
    assert transport.beats == []
    snapshot = world.snapshot(world_id)
    assert snapshot["turns"]["qq:geoff:observe-only"]["status"] == "deferred"
    assert not any(
        action.get("trace", {}).get("input_message_id") == "qq:geoff:observe-only"
        for action in snapshot["actions"].values()
        if isinstance(action, dict)
    )

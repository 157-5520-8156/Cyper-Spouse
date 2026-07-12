from __future__ import annotations

from pathlib import Path
import asyncio

import pytest

from companion_daemon.companion_turn import (
    CompanionTurn,
    DispatchAcceptance,
    ExternalObservation,
    ResponseBudget,
    TurnBeat,
    TurnEnvelope,
    TurnOptions,
    TurnTransport,
)
from companion_daemon.db import CompanionStore
from companion_daemon.engine import CompanionEngine, seed_user
from companion_daemon.models import IncomingMessage
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


class RecordingTransport:
    def __init__(self, acceptance: DispatchAcceptance) -> None:
        self.acceptance = acceptance
        self.beats: list[TurnBeat] = []

    async def dispatch(self, beat: TurnBeat) -> DispatchAcceptance:
        self.beats.append(beat)
        return self.acceptance


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


def _turn_runtime(
    tmp_path: Path, transport: TurnTransport, *, model: object | None = None
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
    return CompanionTurn(engine, transport), world, world_id


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
    assert action["status"] == "unknown"
    assert action["segment_state"]["segments"][0]["status"] == "unknown"


@pytest.mark.asyncio
async def test_repeated_inbound_idempotency_key_never_dispatches_a_second_action(
    tmp_path: Path,
) -> None:
    transport = RecordingTransport(
        DispatchAcceptance(status="delivered", external_receipt="qq:receipt:dedupe")
    )
    runtime, _, _ = _turn_runtime(tmp_path, transport)
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
async def test_generation_timeout_returns_a_structured_failure(tmp_path: Path) -> None:
    transport = RecordingTransport(
        DispatchAcceptance(status="delivered", external_receipt="unused")
    )
    runtime, _, _ = _turn_runtime(tmp_path, transport, model=SlowReplyModel())

    outcome = await runtime.respond(
        _envelope("turn-generation-timeout"),
        budget=ResponseBudget(first_visible_by_ms=5, complete_by_ms=100),
    )

    assert outcome.visible_status == "failed"
    assert outcome.degraded is True
    assert outcome.degradation_reason == "first_visible_timeout"
    assert transport.beats == []


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

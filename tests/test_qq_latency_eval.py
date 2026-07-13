import asyncio
import json

import pytest

from companion_daemon.models import CompanionReply, IncomingMessage
from companion_daemon.conversation_cadence import ConversationCadence
from companion_daemon.qq_latency_eval import (
    qq_latency_report,
    run_synthetic_qq_latency_smoke,
    summarize_qq_latency,
)
from companion_daemon.qq_websocket import QQMessageCoalescer, TurnRuntimeObservation
from companion_daemon.turn_taking import TurnTakingPolicy


@pytest.mark.asyncio
async def test_qq_latency_smoke_records_coalescing_and_visible_receipt_by_cadence() -> None:
    observations = await run_synthetic_qq_latency_smoke()

    assert {item.cadence for item in observations} == {"cold", "warm", "hot"}
    assert all(item.input_count == 1 for item in observations)
    assert all(item.coalescing_wait_seconds is not None for item in observations)
    assert all(item.first_visible_elapsed_seconds is not None for item in observations)
    assert all(
        item.first_visible_elapsed_seconds >= item.coalescing_wait_seconds
        for item in observations
        if item.first_visible_elapsed_seconds is not None
        and item.coalescing_wait_seconds is not None
    )

    summary = {item.cadence: item for item in summarize_qq_latency(observations)}
    assert summary["all"].sample_count == 3
    for cadence in ("cold", "warm", "hot"):
        assert summary[cadence].sample_count == 1
        assert summary[cadence].visible_count == 1
        assert summary[cadence].p50_first_visible_ms is not None

    report = qq_latency_report(observations)
    assert json.loads(json.dumps(report))["observations"][0]["observed_at"]


@pytest.mark.asyncio
async def test_qq_latency_starts_at_first_input_when_a_new_message_resets_debounce() -> None:
    class Clock:
        now = 0.0

        def monotonic(self) -> float:
            return self.now

    class Engine:
        def conversation_cadence(self, _incoming: IncomingMessage) -> ConversationCadence:
            return ConversationCadence("hot", 10.0, 3, "active_back_and_forth")

        async def handle_message(self, _incoming: IncomingMessage) -> CompanionReply:
            return CompanionReply(canonical_user_id="eval", mood="calm", text="收到。")

    class Target:
        async def reply(self, **_kwargs: object) -> dict[str, str]:
            return {"id": "qq-receipt"}

    clock = Clock()
    sleeping = asyncio.Event()
    release = asyncio.Event()

    async def controllable_sleep(seconds: float) -> None:
        sleeping.set()
        await release.wait()
        clock.now += seconds

    observations: list[TurnRuntimeObservation] = []
    coalescer = QQMessageCoalescer(
        Engine(),  # type: ignore[arg-type]
        delay_seconds=0.1,
        turn_policy=TurnTakingPolicy(short_wait_seconds=0.1, long_wait_seconds=0.1),
        sleep=controllable_sleep,
        monotonic=clock.monotonic,
        on_turn_observation=observations.append,
    )
    target = Target()
    await coalescer.add(
        "c2c:eval",
        IncomingMessage(platform="qq", platform_user_id="eval", text="第一句说完了。"),
        target,
    )
    await sleeping.wait()
    clock.now = 0.4
    await coalescer.add(
        "c2c:eval",
        IncomingMessage(platform="qq", platform_user_id="eval", text="第二句也说完了。"),
        target,
    )
    await asyncio.sleep(0)
    release.set()
    await asyncio.sleep(0)

    assert len(observations) == 1
    observation = observations[0]
    assert observation.cadence == "hot"
    assert observation.input_count == 2
    assert observation.coalescing_wait_seconds == pytest.approx(0.5)
    assert observation.first_visible_elapsed_seconds == pytest.approx(0.5)

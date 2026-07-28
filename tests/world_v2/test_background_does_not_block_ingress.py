from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import json

import pytest

from companion_daemon.config import Settings
from companion_daemon.world_v2.qq_c2c_host import build_qq_c2c_host


NOW = datetime(2026, 7, 19, 12, tzinfo=UTC)


class _Delivery:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_text(self, _recipient_id: str, text: str) -> dict[str, object]:
        self.sent.append(text)
        return {"status": "ok", "data": {"message_id": str(len(self.sent))}}

    async def send_reaction(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("the probe only expects text delivery")

    async def send_sticker(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("the probe only expects text delivery")

    async def send_typing(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("the probe only expects text delivery")


class _FastReplyModel:
    model = "fixture:fast-reply"

    async def complete(self, _messages, **_kwargs) -> str:  # type: ignore[no-untyped-def]
        return json.dumps(
            {
                "timing_choice": "now",
                "beats": [{"modality": "text", "text": "收到。"}],
                "stance": "open",
                "brief_rationale": "Reply to the current message.",
            },
            ensure_ascii=False,
        )


class _BlockingBackgroundModel:
    model = "fixture:blocking-background"

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def complete(self, messages, **_kwargs) -> str:  # type: ignore[no-untyped-def]
        self.started.set()
        await self.release.wait()
        system = str(messages[0]["content"])
        if "already verified user Fact" in system:
            return '{"retain":false}'
        if "Assess one verified user message" in system:
            return '{"retain":false}'
        if "immediate inner appraisal" in system:
            return '{"appraise":false,"affect":"no_change"}'
        return '{"decision":"no_change"}'


class _FastInfrastructureModel:
    model = "fixture:fast-infrastructure"

    async def complete(self, messages, **_kwargs) -> str:  # type: ignore[no-untyped-def]
        system = str(messages[0]["content"])
        if "情绪信号初筛器" in system:
            return '{"immediate":false}'
        if "Audit only factual source closure" in system:
            return json.dumps(
                {
                    "decision": "supported",
                    "unsupported_claim_indexes": [],
                    "undeclared_fact_fragments": [],
                    "brief_reason": "No unsupported factual claim.",
                }
            )
        raise AssertionError(f"unexpected infrastructure prompt: {system[:120]}")


class _FastQuickReactionModel:
    model = "fixture:fast-quick-reaction"

    async def complete(self, _messages, **_kwargs) -> str:  # type: ignore[no-untyped-def]
        return '{"react":false}'


@pytest.mark.asyncio
async def test_slow_background_model_does_not_hold_the_inbound_world_lock(tmp_path) -> None:
    background = _BlockingBackgroundModel()
    infrastructure = _FastInfrastructureModel()
    delivery = _Delivery()
    host = build_qq_c2c_host(
        settings=Settings(
            database_path=tmp_path / "background-nonblocking.sqlite",
            PRIMARY_USER_ID="geoff",
        ),
        recipient_id="10001",
        bootstrap_at=NOW,
        model=_FastReplyModel(),
        advisory_model=background,
        immediate_emotion_gate_model=infrastructure,
        source_closure_model=infrastructure,
        quick_reaction_model=_FastQuickReactionModel(),
        delivery=delivery,
        use_configured_recall_embedding=False,
    )
    background_task: asyncio.Task[object] | None = None
    try:
        first = await host.inbound_text(
            message_id="message:one",
            recipient_id="10001",
            text="你好",
            observed_at=NOW,
        )
        background_task = asyncio.create_task(
            host.scheduler_once(
                observed_at=NOW + timedelta(minutes=1),
                max_action_units=0,
                max_background_units=1,
            )
        )
        await asyncio.wait_for(background.started.wait(), timeout=2)

        started = asyncio.get_running_loop().time()
        # The API fixtures return immediately, so the public inbound path must
        # keep its entire non-provider hot path inside the 500ms production
        # target even while background cognition is indefinitely blocked.
        second = await asyncio.wait_for(
            host.inbound_text(
                message_id="message:two",
                recipient_id="10001",
                text="还在吗？",
                observed_at=NOW + timedelta(minutes=1),
            ),
            timeout=3,
        )
        elapsed = asyncio.get_running_loop().time() - started

        assert first.status == second.status == "action_authorized"
        assert elapsed < 0.5
        assert delivery.sent == ["收到。", "收到。"]
        assert not background_task.done()
    finally:
        background.release.set()
        if background_task is not None:
            await asyncio.wait_for(background_task, timeout=5)
        await host.aclose()


@pytest.mark.asyncio
async def test_regular_host_drain_does_not_hold_the_inbound_world_lock(tmp_path) -> None:
    """The public adapter drain must have the same non-blocking guarantee.

    ``scheduler_once`` has its own lock-free path, so testing it alone would
    miss the production HTTP/QQ ``drain`` entrypoint that is commonly called
    by a passive scheduler task.
    """

    background = _BlockingBackgroundModel()
    infrastructure = _FastInfrastructureModel()
    delivery = _Delivery()
    host = build_qq_c2c_host(
        settings=Settings(
            database_path=tmp_path / "background-nonblocking-drain.sqlite",
            PRIMARY_USER_ID="geoff",
        ),
        recipient_id="10001",
        bootstrap_at=NOW,
        model=_FastReplyModel(),
        advisory_model=background,
        immediate_emotion_gate_model=infrastructure,
        source_closure_model=infrastructure,
        quick_reaction_model=_FastQuickReactionModel(),
        delivery=delivery,
        use_configured_recall_embedding=False,
    )
    drain_task: asyncio.Task[object] | None = None
    try:
        first = await host.inbound_text(
            message_id="message:one",
            recipient_id="10001",
            text="你好",
            observed_at=NOW,
        )
        drain_task = asyncio.create_task(
            host.drain(max_action_units=0, max_background_units=1)
        )
        await asyncio.wait_for(background.started.wait(), timeout=2)

        started = asyncio.get_running_loop().time()
        # The regular drain entrypoint has the same 500ms local budget as the
        # scheduler variant above.
        second = await asyncio.wait_for(
            host.inbound_text(
                message_id="message:two",
                recipient_id="10001",
                text="还在吗？",
                observed_at=NOW + timedelta(minutes=1),
            ),
            timeout=3,
        )
        elapsed = asyncio.get_running_loop().time() - started

        assert first.status == second.status == "action_authorized"
        assert elapsed < 0.5
        assert delivery.sent == ["收到。", "收到。"]
        assert not drain_task.done()
    finally:
        background.release.set()
        if drain_task is not None:
            await asyncio.wait_for(drain_task, timeout=5)
        await host.aclose()

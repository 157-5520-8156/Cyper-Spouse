from pathlib import Path

import pytest

from companion_daemon.db import CompanionStore
from companion_daemon.engine import CompanionEngine, seed_user
from companion_daemon.llm import FakeCompanionModel
from companion_daemon.models import IncomingMessage
from companion_daemon.stickers import StickerCatalog, Sticker

TEST_PROMPT = "你是凛，用户的赛博女友。"


@pytest.mark.asyncio
async def test_handle_message_updates_mood_and_replies(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    engine = CompanionEngine(store, FakeCompanionModel(), TEST_PROMPT)

    reply = await engine.handle_message(
        IncomingMessage(platform="qq", platform_user_id="geoff", text="我先忙一会儿")
    )

    assert reply.canonical_user_id == "geoff"
    assert reply.mood == "miss_you"
    assert "我在呢" in reply.text


@pytest.mark.asyncio
async def test_platform_switch_context_is_reported(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    engine = CompanionEngine(store, FakeCompanionModel(), TEST_PROMPT)

    store.map_account("wechat", "wechat-geoff", "geoff")
    await engine.handle_message(
        IncomingMessage(platform="wechat", platform_user_id="wechat-geoff", text="等我一下")
    )
    store.map_account("qq", "qq-geoff", "geoff")
    reply = await engine.handle_message(
        IncomingMessage(platform="qq", platform_user_id="qq-geoff", text="我回来了")
    )

    assert reply.platform_context == "刚刚在 wechat 聊，现在切到了 qq。"


@pytest.mark.asyncio
async def test_proactive_tick_records_decision(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    engine = CompanionEngine(store, FakeCompanionModel(), TEST_PROMPT)

    await engine.handle_message(
        IncomingMessage(platform="qq", platform_user_id="geoff", text="我先忙一会儿")
    )
    decision = await engine.proactive_tick("geoff")

    assert decision.should_send is True
    assert decision.platform == "qq"
    assert decision.message


@pytest.mark.asyncio
async def test_proactive_tick_attaches_sticker_path(tmp_path: Path) -> None:
    class StickerModel(FakeCompanionModel):
        async def complete(self, messages: list[dict[str, str]], *, temperature: float = 0.8) -> str:
            if any("Return strict JSON" in message["content"] for message in messages):
                return (
                    '{"private_thought":"想发个表情但先不长篇说话",'
                    '"should_send":true,"platform":"qq","message_type":"sticker",'
                    '"message":null,"sticker_category":null,"cooldown_minutes":30}'
                )
            return await super().complete(messages, temperature=temperature)

    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    stickers = StickerCatalog(
        stickers=[
            Sticker(
                id="miss_you",
                category="miss_you",
                mood="miss_you",
                intent="reaching_out",
                path=Path("assets/stickers/rin-miss-you.png"),
            )
        ]
    )
    engine = CompanionEngine(store, StickerModel(), TEST_PROMPT, stickers)

    await engine.handle_message(
        IncomingMessage(platform="qq", platform_user_id="geoff", text="我先忙一会儿")
    )
    decision = await engine.proactive_tick("geoff")

    assert decision.message_type == "sticker"
    assert decision.sticker_path == "assets/stickers/rin-miss-you.png"

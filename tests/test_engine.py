from datetime import timedelta
from pathlib import Path

import pytest

from companion_daemon.db import CompanionStore
from companion_daemon.engine import CompanionEngine, seed_user
from companion_daemon.image_generation import GeneratedImage
from companion_daemon.llm import FakeCompanionModel
from companion_daemon.models import IncomingMessage, MoodState
from companion_daemon.stickers import StickerCatalog, Sticker
from companion_daemon.character import load_character
from companion_daemon.budget import BudgetGate
from companion_daemon.time import utc_now

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
async def test_handle_message_injects_human_rhythm_context(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    model = FakeCompanionModel()
    engine = CompanionEngine(store, model, TEST_PROMPT)

    await engine.handle_message(
        IncomingMessage(platform="qq", platform_user_id="geoff", text="我先忙一会儿")
    )

    prompt_text = "\n".join(message["content"] for message in model.calls[-1])
    assert "生活节律" in prompt_text
    assert "不要写舞台动作" in prompt_text
    assert any(row["kind"] == "life_continuity" for row in store.memories("geoff"))


@pytest.mark.asyncio
async def test_handle_message_relaxes_after_warm_proactive_response(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(
        store,
        initial_state=MoodState(
            mood="miss_you",
            security=35,
            initiative=45,
            emotional_charge=18,
            last_platform="qq",
        ),
    )
    store.record_proactive_delivery("geoff", "qq")
    model = FakeCompanionModel()
    engine = CompanionEngine(store, model, TEST_PROMPT)

    await engine.handle_message(
        IncomingMessage(platform="qq", platform_user_id="geoff", text="我在呀，刚刚忙完")
    )

    state = store.get_mood_state("geoff")
    assert state.security > 35
    assert state.initiative < 45
    assert state.emotional_charge < 18
    assert any(row["kind"] == "proactive_response" for row in store.memories("geoff"))
    prompt_text = "\n".join(message["content"] for message in model.calls[-1])
    assert "主动反馈" in prompt_text


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
    seed_user(store, initial_state=MoodState(initiative=60, emotional_charge=15))
    engine = CompanionEngine(store, FakeCompanionModel(), TEST_PROMPT)

    await engine.handle_message(
        IncomingMessage(platform="qq", platform_user_id="geoff", text="我先忙一会儿")
    )
    decision = await engine.proactive_tick("geoff")

    assert decision.should_send is True
    assert decision.platform == "qq"
    assert decision.message
    state = store.get_mood_state("geoff")
    assert state.initiative < 60
    assert state.emotional_charge < 15


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


@pytest.mark.asyncio
async def test_proactive_tick_can_attach_self_initiated_image(tmp_path: Path) -> None:
    class ImageModel(FakeCompanionModel):
        async def complete(self, messages: list[dict[str, str]], *, temperature: float = 0.8) -> str:
            if any("Return strict JSON" in message["content"] for message in messages):
                return (
                    '{"private_thought":"路过图书馆窗边，突然想拍给你看",'
                    '"should_send":true,"platform":"qq","message_type":"text_image",'
                    '"message":"刚刚窗边光很好，突然想给你看一下。",'
                    '"sticker_category":null,"cooldown_minutes":45}'
                )
            return await super().complete(messages, temperature=temperature)

    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(
        store,
        initial_state=MoodState(
            relationship_stage="close_friend",
            trust=68,
            intimacy=52,
            security=60,
            initiative=72,
            last_platform="qq",
        ),
    )
    image_generator = FakeImageGenerator()
    engine = CompanionEngine(
        store,
        ImageModel(),
        TEST_PROMPT,
        character_profile=load_character("configs/character.yaml"),
        image_generator=image_generator,
        image_output_dir=tmp_path / "images",
    )

    decision = await engine.proactive_tick("geoff")

    assert decision.message_type == "text_image"
    assert decision.image_path
    assert Path(decision.image_path).exists()
    assert "virtual-life selfie-style" in image_generator.prompts[0]
    assert any(row["kind"] == "generated_image" for row in store.memories("geoff"))


@pytest.mark.asyncio
async def test_proactive_tick_records_withheld_impulse(tmp_path: Path) -> None:
    class NoSendModel(FakeCompanionModel):
        async def complete(self, messages: list[dict[str, str]], *, temperature: float = 0.8) -> str:
            if any("Return strict JSON" in message["content"] for message in messages):
                return (
                    '{"private_thought":"有点想问他后来怎么样，但怕打扰",'
                    '"should_send":false,"platform":null,"message_type":"none",'
                    '"message":null,"sticker_category":null,"cooldown_minutes":30}'
                )
            return await super().complete(messages, temperature=temperature)

    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(
        store,
        initial_state=MoodState(
            relationship_stage="friend",
            trust=50,
            intimacy=35,
            initiative=30,
            emotional_charge=4,
        ),
    )
    engine = CompanionEngine(store, NoSendModel(), TEST_PROMPT)
    await engine.handle_message(
        IncomingMessage(
            platform="qq",
            platform_user_id="geoff",
            text="所以你觉得呢？",
            sent_at=utc_now() - timedelta(hours=1),
        )
    )
    before_tick = store.get_mood_state("geoff")

    decision = await engine.proactive_tick("geoff")

    assert decision.should_send is False
    state = store.get_mood_state("geoff")
    assert state.initiative > before_tick.initiative
    assert state.emotional_charge > before_tick.emotional_charge
    assert any(row["kind"] == "withheld_proactive_impulse" for row in store.memories("geoff"))


@pytest.mark.asyncio
async def test_handle_message_attaches_ordinary_reply_sticker(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    stickers = StickerCatalog(
        stickers=[
            Sticker(
                id="comfort",
                category="comfort",
                mood="calm",
                intent="comfort",
                path=Path("assets/stickers/rin-comfort.png"),
            )
        ]
    )
    engine = CompanionEngine(store, FakeCompanionModel(), TEST_PROMPT, stickers)

    reply = await engine.handle_message(
        IncomingMessage(platform="qq", platform_user_id="geoff", text="我今天好累，有点难受")
    )

    assert reply.sticker_path == "assets/stickers/rin-comfort.png"


class FakeImageGenerator:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def generate(
        self,
        prompt: str,
        *,
        output_path: Path,
        size: str = "1024x1024",
    ) -> GeneratedImage:
        self.prompts.append(prompt)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"fake-png")
        return GeneratedImage(output_path, prompt)


@pytest.mark.asyncio
async def test_handle_message_generates_requested_image(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(
        store,
        initial_state=MoodState(
            relationship_stage="close_friend",
            trust=70,
            intimacy=55,
            security=62,
        ),
    )
    image_generator = FakeImageGenerator()
    engine = CompanionEngine(
        store,
        FakeCompanionModel(),
        TEST_PROMPT,
        character_profile=load_character("configs/character.yaml"),
        image_generator=image_generator,
        image_output_dir=tmp_path / "images",
    )

    reply = await engine.handle_message(
        IncomingMessage(platform="qq", platform_user_id="geoff", text="给我发一张水彩风格自拍看看")
    )

    assert reply.image_path
    assert Path(reply.image_path).exists()
    assert reply.sticker_path is None
    assert "Character identity anchor" in image_generator.prompts[0]
    assert any(row["kind"] == "generated_image" for row in store.memories("geoff"))


@pytest.mark.asyncio
async def test_handle_message_can_defer_early_selfie_request(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    image_generator = FakeImageGenerator()
    model = FakeCompanionModel()
    engine = CompanionEngine(
        store,
        model,
        TEST_PROMPT,
        character_profile=load_character("configs/character.yaml"),
        image_generator=image_generator,
        image_output_dir=tmp_path / "images",
    )

    reply = await engine.handle_message(
        IncomingMessage(platform="qq", platform_user_id="geoff", text="给我发一张自拍看看")
    )

    assert reply.image_path is None
    assert image_generator.prompts == []
    prompt_text = "\n".join(message["content"] for message in model.calls[-1])
    assert "不要立刻发自拍" in prompt_text
    assert any(row["kind"] == "selfie_deferred" for row in store.memories("geoff"))


@pytest.mark.asyncio
async def test_auto_image_generation_respects_budget_gate(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(
        store,
        initial_state=MoodState(
            relationship_stage="close_friend",
            trust=70,
            intimacy=55,
            security=62,
        ),
    )
    image_generator = FakeImageGenerator()
    budget = BudgetGate(
        store,
        monthly_budget_cny=80,
        daily_budget_cny=3,
        soft_daily_budget_cny=0.1,
        monthly_image_limit=20,
        monthly_vision_limit=120,
        monthly_audio_limit=60,
    )
    engine = CompanionEngine(
        store,
        FakeCompanionModel(),
        TEST_PROMPT,
        character_profile=load_character("configs/character.yaml"),
        image_generator=image_generator,
        budget_gate=budget,
        image_output_dir=tmp_path / "images",
    )

    reply = await engine.handle_message(
        IncomingMessage(platform="qq", platform_user_id="geoff", text="给我发一张自拍看看")
    )

    assert reply.image_path is None
    assert image_generator.prompts == []
    assert any(row["kind"] == "image_request_blocked" for row in store.memories("geoff"))


@pytest.mark.asyncio
async def test_handle_message_records_tool_request_without_executing(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    model = FakeCompanionModel()
    engine = CompanionEngine(store, model, TEST_PROMPT)

    await engine.handle_message(
        IncomingMessage(platform="qq", platform_user_id="geoff", text="帮我打开浏览器看一下这个网页")
    )

    proposals = store.recent_tool_proposals("geoff")
    assert proposals
    assert proposals[-1]["kind"] == "computer_assist"
    sent_prompt = "\n".join(message["content"] for message in model.calls[-1])
    assert "必须先请求用户明确确认" in sent_prompt

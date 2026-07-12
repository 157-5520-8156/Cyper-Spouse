from pathlib import Path

import pytest

from companion_daemon.db import CompanionStore
from companion_daemon.attachment_cache import AttachmentCache
from companion_daemon.engine import CompanionEngine, seed_user
from companion_daemon.llm import FakeCompanionModel
from companion_daemon.models import IncomingMessage, MessageAttachment
from companion_daemon.multimodal_analysis import AttachmentInsight
from companion_daemon.world import WorldKernel


class FakeAnalyzer:
    def __init__(self) -> None:
        self.calls = 0

    async def analyze(self, attachment: MessageAttachment) -> AttachmentInsight:
        self.calls += 1
        return AttachmentInsight(attachment.kind, "看起来是一张猫的照片。", 0.8)


class FailingAnalyzer:
    async def analyze(self, attachment: MessageAttachment) -> AttachmentInsight:
        raise RuntimeError("provider unavailable")


@pytest.mark.asyncio
async def test_engine_includes_multimodal_analysis(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    model = FakeCompanionModel()
    analyzer = FakeAnalyzer()
    engine = CompanionEngine(store, model, "你是沈知栀。", multimodal_analyzer=analyzer)

    await engine.handle_message(
        IncomingMessage(
            platform="qq",
            platform_user_id="geoff",
            text="看看这个",
            attachments=[MessageAttachment(kind="image", filename="cat.png")],
        )
    )

    sent_prompt = "\n".join(message["content"] for message in model.calls[-1])
    assert "看起来是一张猫的照片" in sent_prompt
    memories = store.memories("geoff")
    assert any(memory["kind"] == "image_insight" for memory in memories)
    state = store.get_mood_state("geoff")
    assert state.trust > 15
    assert state.intimacy > 5
    assert analyzer.calls == 1


@pytest.mark.asyncio
async def test_engine_reuses_cached_attachment_analysis(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    analyzer = FakeAnalyzer()
    engine = CompanionEngine(
        store,
        FakeCompanionModel(),
        "你是沈知栀。",
        multimodal_analyzer=analyzer,
    )
    attachment = MessageAttachment(kind="image", filename="cat.png", url="https://cdn.example/cat.png")

    await engine.handle_message(
        IncomingMessage(platform="qq", platform_user_id="geoff", text="看看这个", attachments=[attachment])
    )
    await engine.handle_message(
        IncomingMessage(platform="qq", platform_user_id="geoff", text="再看一眼", attachments=[attachment])
    )

    assert analyzer.calls == 1


@pytest.mark.asyncio
async def test_world_attachment_analysis_is_audited_cached_and_source_grounded(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    world = WorldKernel(store)
    world_id = world.start_from_seed_file(Path("configs/world_seed.yaml")).world_id
    model = FakeCompanionModel()
    analyzer = FakeAnalyzer()
    fetched: list[str] = []

    async def fetch(url: str) -> bytes:
        fetched.append(url)
        return b"image bytes"

    engine = CompanionEngine(
        store,
        model,
        "你是沈知栀。",
        multimodal_analyzer=analyzer,
        world_kernel=world,
        world_id=world_id,
        attachment_cache=AttachmentCache(tmp_path / "attachments"),
        attachment_fetcher=fetch,
    )
    attachment = MessageAttachment(
        kind="image", filename="cat.png", content_type="image/png",
        url="https://cdn.example/cat.png",
    )

    await engine.handle_message(IncomingMessage(
        platform="qq", platform_user_id="geoff", message_id="with-cat",
        text="看看这个", attachments=[attachment],
    ))

    action = next(
        item for item in world.snapshot(world_id)["actions"].values()
        if item["kind"] == "attachment_analysis"
    )
    assert action["status"] == "delivered"
    assert action["result"]["summary"] == "看起来是一张猫的照片。"
    assert action["result"]["source_message_id"] == "with-cat"
    assert action["result"]["attachment_index"] == 0
    assert action["result"]["cache"]["retention_days"] == 30
    assert fetched == ["https://cdn.example/cat.png"]
    assert analyzer.calls == 1

    context = world.conversation_context(world_id, user_id="user:geoff")
    assert context["referencable_attachment_insights"] == [{
        "source_id": action["action_id"],
        "source_type": "attachment_analysis",
        "reference_state": "delivered",
        "source_message_id": "with-cat",
        "attachment_index": 0,
        "kind": "image",
        "summary": "看起来是一张猫的照片。",
        "confidence": 0.8,
    }]
    assert world.conversation_context(
        world_id, user_id="user:alice"
    )["referencable_attachment_insights"] == []
    prompt = "\n".join(message["content"] for message in model.calls[-1])
    assert "看起来是一张猫的照片。" in prompt
    assert action["action_id"] in prompt

    await engine.handle_message(IncomingMessage(
        platform="qq", platform_user_id="geoff", message_id="with-cat-again",
        text="还是这张", attachments=[attachment],
    ))

    repeated = [
        item for item in world.snapshot(world_id)["actions"].values()
        if item["kind"] == "attachment_analysis"
    ]
    assert len(repeated) == 2
    assert analyzer.calls == 1
    assert fetched == ["https://cdn.example/cat.png"]
    repeated_result = next(
        item["result"] for item in repeated
        if item["result"]["source_message_id"] == "with-cat-again"
    )
    assert repeated_result["cache"]["analysis_hit"] is True


@pytest.mark.asyncio
async def test_world_attachment_analysis_failure_is_audited_without_blocking_reply(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    world = WorldKernel(store)
    world_id = world.start_from_seed_file(Path("configs/world_seed.yaml")).world_id
    engine = CompanionEngine(
        store, FakeCompanionModel(), "你是沈知栀。",
        multimodal_analyzer=FailingAnalyzer(), world_kernel=world, world_id=world_id,
    )

    reply = await engine.handle_message(IncomingMessage(
        platform="qq", platform_user_id="geoff", message_id="bad-audio",
        text="听听", attachments=[MessageAttachment(kind="audio", filename="voice.mp3")],
    ))

    assert reply is not None
    action = next(
        item for item in world.snapshot(world_id)["actions"].values()
        if item["kind"] == "attachment_analysis"
    )
    assert action["status"] == "failed"
    assert action["result"]["reason"] == "RuntimeError"
    assert world.conversation_context(
        world_id, user_id="user:geoff"
    )["referencable_attachment_insights"] == []

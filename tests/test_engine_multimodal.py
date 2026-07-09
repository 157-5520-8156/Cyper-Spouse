from pathlib import Path

import pytest

from companion_daemon.db import CompanionStore
from companion_daemon.engine import CompanionEngine, seed_user
from companion_daemon.llm import FakeCompanionModel
from companion_daemon.models import IncomingMessage, MessageAttachment
from companion_daemon.multimodal_analysis import AttachmentInsight


class FakeAnalyzer:
    def __init__(self) -> None:
        self.calls = 0

    async def analyze(self, attachment: MessageAttachment) -> AttachmentInsight:
        self.calls += 1
        return AttachmentInsight(attachment.kind, "看起来是一张猫的照片。", 0.8)


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

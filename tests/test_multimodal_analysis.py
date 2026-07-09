from datetime import UTC, datetime

import pytest
import httpx

from companion_daemon.budget import BudgetGate
from companion_daemon.db import CompanionStore
from companion_daemon.models import MessageAttachment
from companion_daemon.multimodal_analysis import (
    MultimodalAnalyzer,
    OpenAIMultimodalAnalyzer,
    _looks_like_text_file,
)


def test_looks_like_text_file() -> None:
    assert _looks_like_text_file(MessageAttachment(kind="file", filename="note.md"))
    assert _looks_like_text_file(MessageAttachment(kind="file", content_type="text/plain"))
    assert not _looks_like_text_file(MessageAttachment(kind="file", filename="photo.png"))


@pytest.mark.asyncio
async def test_image_analysis_reports_need_for_vision_provider() -> None:
    analyzer = MultimodalAnalyzer()

    insight = await analyzer.analyze(MessageAttachment(kind="image", url="https://example.test/a.png"))

    assert insight.kind == "image"
    assert "视觉模型" in insight.summary


@pytest.mark.asyncio
async def test_openai_image_analysis_uses_budget_and_records_usage(tmp_path) -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "一张雨后街边照片。"}}]},
        )

    store = CompanionStore(tmp_path / "test.sqlite")
    gate = BudgetGate(
        store,
        monthly_budget_cny=80,
        daily_budget_cny=3,
        soft_daily_budget_cny=2,
        monthly_image_limit=20,
        monthly_vision_limit=120,
        monthly_audio_limit=60,
    )
    analyzer = OpenAIMultimodalAnalyzer(
        api_key="test-key",
        base_url="https://api.example.test/v1",
        vision_model="gpt-4o-mini",
        transcription_model="gpt-4o-mini-transcribe",
        budget_gate=gate,
        transport=httpx.MockTransport(handler),
    )

    insight = await analyzer.analyze(MessageAttachment(kind="image", url="https://cdn.example/a.png"))

    assert insight.summary == "图片内容：一张雨后街边照片。"
    assert requests[0].url == "https://api.example.test/v1/chat/completions"
    assert store.usage_count("vision", "month", datetime.now(UTC)) == 1


@pytest.mark.asyncio
async def test_openai_image_analysis_skips_when_budget_denies(tmp_path) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("network should not be called")

    store = CompanionStore(tmp_path / "test.sqlite")
    gate = BudgetGate(
        store,
        monthly_budget_cny=80,
        daily_budget_cny=3,
        soft_daily_budget_cny=2,
        monthly_image_limit=20,
        monthly_vision_limit=0,
        monthly_audio_limit=60,
    )
    analyzer = OpenAIMultimodalAnalyzer(
        api_key="test-key",
        base_url="https://api.example.test/v1",
        vision_model="gpt-4o-mini",
        transcription_model="gpt-4o-mini-transcribe",
        budget_gate=gate,
        transport=httpx.MockTransport(handler),
    )

    insight = await analyzer.analyze(MessageAttachment(kind="image", url="https://cdn.example/a.png"))

    assert "控制费用" in insight.summary
    assert store.usage_count("vision", "month", datetime.now(UTC)) == 0

"""Durable vision transport: effect-once analysis, restart lookup, hash binding."""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import httpx
import pytest

from companion_daemon.world_v2.perception_vision_transport import (
    SQLiteDurableVisionPerceptionTransport,
)


BODY = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\npng-body").decode()
BODY_HASH = "sha256:" + hashlib.sha256(BODY.encode()).hexdigest()


def _provider(text: str, calls: dict[str, int]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] = calls.get("count", 0) + 1
        payload = json.loads(request.content.decode())
        assert payload["messages"][1]["content"][1]["image_url"]["url"].startswith(
            "data:image/"
        )
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-test-1",
                "choices": [{"message": {"content": text}}],
            },
        )

    return httpx.MockTransport(handler)


def _transport(
    path: Path, mock: httpx.MockTransport
) -> SQLiteDurableVisionPerceptionTransport:
    return SQLiteDurableVisionPerceptionTransport(
        path,
        api_key="test-key",
        base_url="https://api.openai.example/v1",
        model="gpt-4o-mini",
        transport=mock,
    )


@pytest.mark.asyncio
async def test_analyze_is_effect_once_and_survives_restart(tmp_path: Path) -> None:
    calls: dict[str, int] = {}
    path = tmp_path / "perception.sqlite"
    transport = _transport(path, _provider("窗台上有一只橘猫，在晒太阳。", calls))
    first = await transport.analyze(
        analysis_kind="vision",
        input_ref="qq-attachment:image:sha256:" + "a" * 64,
        input_hash=BODY_HASH,
        body=BODY,
        idempotency_key="perception:key-1",
    )
    result_ref, result_hash, provider_ref, cost, received_at = first
    assert result_ref.startswith("perception-vision:")
    assert result_hash == "sha256:" + hashlib.sha256("窗台上有一只橘猫，在晒太阳。".encode()).hexdigest()
    assert provider_ref == "chatcmpl-test-1"
    assert cost == 0
    assert received_at.tzinfo is not None

    again = await transport.analyze(
        analysis_kind="vision",
        input_ref="qq-attachment:image:sha256:" + "a" * 64,
        input_hash=BODY_HASH,
        body=BODY,
        idempotency_key="perception:key-1",
    )
    assert again == first
    assert calls["count"] == 1
    transport.close()

    reopened = _transport(path, _provider("must not be called", calls))
    try:
        assert await reopened.lookup(idempotency_key="perception:key-1") == first
        content = reopened.read_exact(result_ref=result_ref)
        assert content is not None
        assert content.text == "窗台上有一只橘猫，在晒太阳。"
        assert content.result_hash == result_hash
        assert calls["count"] == 1
    finally:
        reopened.close()


@pytest.mark.asyncio
async def test_idempotency_key_cannot_be_rebound_to_other_bytes(tmp_path: Path) -> None:
    transport = _transport(tmp_path / "p.sqlite", _provider("描述", {}))
    try:
        await transport.analyze(
            analysis_kind="vision",
            input_ref="ref-a",
            input_hash=BODY_HASH,
            body=BODY,
            idempotency_key="perception:key-2",
        )
        with pytest.raises(ValueError, match="rebound"):
            await transport.analyze(
                analysis_kind="vision",
                input_ref="ref-b",
                input_hash="sha256:" + "1" * 64,
                body=BODY,
                idempotency_key="perception:key-2",
            )
    finally:
        transport.close()


@pytest.mark.asyncio
async def test_transport_rejects_non_vision_and_non_data_url_inputs(tmp_path: Path) -> None:
    transport = _transport(tmp_path / "p.sqlite", _provider("描述", {}))
    try:
        with pytest.raises(ValueError, match="vision"):
            await transport.analyze(
                analysis_kind="transcription",
                input_ref="r",
                input_hash=BODY_HASH,
                body=BODY,
                idempotency_key="k",
            )
        with pytest.raises(ValueError, match="data URL"):
            await transport.analyze(
                analysis_kind="vision",
                input_ref="r",
                input_hash=BODY_HASH,
                body="https://example.invalid/leaked-url.png",
                idempotency_key="k",
            )
    finally:
        transport.close()


@pytest.mark.asyncio
async def test_identity_assertions_are_neutralized(tmp_path: Path) -> None:
    transport = _transport(
        tmp_path / "p.sqlite", _provider("这是用户本人的自拍，非常好看。", {})
    )
    try:
        result_ref, result_hash, _, _, _ = await transport.analyze(
            analysis_kind="vision",
            input_ref="r",
            input_hash=BODY_HASH,
            body=BODY,
            idempotency_key="k-identity",
        )
        content = transport.read_exact(result_ref=result_ref)
        assert content is not None
        assert content.text == "图片中可见一位人物；人物身份未经确认。"
        assert content.result_hash == result_hash
    finally:
        transport.close()


@pytest.mark.asyncio
async def test_dispatch_evidence_reads_support_daily_cap_and_dedupe(tmp_path: Path) -> None:
    from datetime import UTC, datetime, timedelta

    transport = _transport(tmp_path / "p.sqlite", _provider("描述", {}))
    try:
        before = datetime.now(UTC) - timedelta(minutes=1)
        assert transport.dispatched_count_since(before) == 0
        assert transport.has_result_for_input(input_hash=BODY_HASH) is False
        await transport.analyze(
            analysis_kind="vision",
            input_ref="r",
            input_hash=BODY_HASH,
            body=BODY,
            idempotency_key="k-count",
        )
        assert transport.dispatched_count_since(before) == 1
        assert (
            transport.dispatched_count_since(datetime.now(UTC) + timedelta(minutes=1)) == 0
        )
        assert transport.has_result_for_input(input_hash=BODY_HASH) is True
        with pytest.raises(ValueError, match="timezone-aware"):
            transport.dispatched_count_since(datetime.now())
    finally:
        transport.close()

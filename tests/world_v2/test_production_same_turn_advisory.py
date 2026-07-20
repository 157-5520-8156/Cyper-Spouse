from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import json

import pytest

from companion_daemon.config import Settings
from companion_daemon.world_v2.http_capture_host import build_http_v2_capture_host
from companion_daemon.world_v2.qq_c2c_host import build_qq_c2c_host


NOW = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)


class _ReplyModel:
    def __init__(self, text: str) -> None:
        self.text = text
        self.calls: list[list[dict[str, str]]] = []

    async def complete(
        self, messages: list[dict[str, str]], *, temperature: float = 0.8
    ) -> str:
        self.calls.append(messages)
        return json.dumps(
            {
                "response_text": self.text,
                "stance": "answer_without_world_claims",
                "brief_rationale": "I noticed the alternatives and chose my own response.",
                "confidence": 7600,
            },
            ensure_ascii=False,
        )


class _AdvisoryModel:
    def __init__(self, classifications: list[dict[str, object]], *, delay: float = 0) -> None:
        self.classifications = classifications
        self.delay = delay
        self.calls = 0
        self.messages: list[list[dict[str, str]]] = []

    async def complete(
        self, messages: list[dict[str, str]], *, temperature: float = 0.2
    ) -> str:
        self.calls += 1
        self.messages.append(messages)
        if self.delay:
            await asyncio.sleep(self.delay)
        request = json.loads(messages[1]["content"])
        source_ref = request["allowed_source_refs"][0]
        material = json.loads(json.dumps(self.classifications))
        for classification in material:
            for alternative in classification["alternatives"]:
                alternative["source_refs"] = [source_ref]
        return json.dumps({"classifications": material}, ensure_ascii=False)


class _QQDelivery:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send_text(self, recipient_id: str, text: str) -> dict[str, object]:
        self.sent.append((recipient_id, text))
        return {"status": "ok", "data": {"message_id": f"qq-{len(self.sent)}"}}


def _candidate(
    *, field: str, value: str, weight: int = 10_000, confidence: int = 8_000
) -> dict[str, object]:
    return {
        "field_id": field,
        "alternatives": [
            {
                "value": value,
                "weight_bp": weight,
                "confidence_bp": confidence,
                "source_refs": ["resolved-by-test-adapter"],
                "basis": "trigger_implicit",
            }
        ],
    }


@pytest.mark.asyncio
async def test_current_disappointment_and_thread_advice_reach_reply_model_without_forcing_comfort(
    tmp_path,
) -> None:
    reply = _ReplyModel("我听见了，但我现在不想顺着这个话题说安慰话。")
    advisory = _AdvisoryModel(
        [
            _candidate(field="user_affect.signal", value="disappointed"),
            _candidate(
                field="continuity.thread_signal", value="possible_unfinished_share"
            ),
            _candidate(field="appraisal.negative", value="disappointment"),
        ]
    )
    host = build_http_v2_capture_host(
        settings=Settings(database_path=tmp_path / "same-turn-advisory.sqlite"),
        bootstrap_at=NOW,
        model=reply,
        advisory_model=advisory,
    )
    try:
        result = await host.respond(
            platform="simulator",
            platform_user_id="geoff",
            platform_message_id="message:advisory",
            text="算了，你好像也没认真听我刚才分享的事。",
            observed_at=NOW,
        )
    finally:
        await host.aclose()

    assert result.text == reply.text
    assert len(reply.calls) == 1
    # The one pre-cursor advisory is already incorporated into the combined
    # cognition result; cached Flash expression rebinding must not classify it
    # a second time.
    assert advisory.calls == 1
    assert len(reply.calls) == 1
    model_request = reply.calls[0][1]["content"]
    assert "user_affect.signal" in model_request
    assert "disappointed" in model_request
    assert "continuity.thread_signal" in model_request
    assert "possible_unfinished_share" in model_request


@pytest.mark.asyncio
async def test_high_severity_same_turn_advice_can_select_thinking_while_ordinary_uses_flash(
    tmp_path,
) -> None:
    flash = _ReplyModel("flash reply")
    thinking = _ReplyModel("thinking reply")
    advisory = _AdvisoryModel(
        [
            _candidate(field="appraisal.negative", value="boundary_violation"),
            _candidate(field="appraisal.severity", value="high"),
        ]
    )
    host = build_http_v2_capture_host(
        settings=Settings(database_path=tmp_path / "thinking-route.sqlite"),
        bootstrap_at=NOW,
        model=flash,
        thinking_model=thinking,
        advisory_model=advisory,
    )
    try:
        result = await host.respond(
            platform="simulator",
            platform_user_id="geoff",
            platform_message_id="message:advisory",
            text="这句话让我很不舒服。",
            observed_at=NOW,
        )
    finally:
        await host.aclose()

    assert result.text == "thinking reply"
    assert len(thinking.calls) == 1
    assert flash.calls == []


@pytest.mark.asyncio
async def test_slow_semantic_advice_fails_open_with_a_bounded_delay_and_flash_reply(
    tmp_path,
) -> None:
    flash = _ReplyModel("先按我现在能确认的内容回应你。")
    advisory = _AdvisoryModel([], delay=0.2)
    host = build_http_v2_capture_host(
        settings=Settings(
            database_path=tmp_path / "advisory-timeout.sqlite",
            WORLD_V2_ADVISORY_TIMEOUT_SECONDS=0.05,
        ),
        bootstrap_at=NOW,
        model=flash,
        advisory_model=advisory,
    )
    loop = asyncio.get_running_loop()
    started = loop.time()
    try:
        result = await host.respond(
            platform="simulator",
            platform_user_id="geoff",
            platform_message_id="message:advisory",
            text="普通的一句话。",
            observed_at=NOW,
        )
    finally:
        await host.aclose()
    elapsed = loop.time() - started

    assert result.text == flash.text
    assert len(flash.calls) == 1
    assert elapsed < 1.0


@pytest.mark.asyncio
async def test_qq_production_composition_uses_the_same_same_turn_semantic_module(
    tmp_path,
) -> None:
    reply = _ReplyModel("我注意到了，但怎么回应由我自己决定。")
    advisory = _AdvisoryModel(
        [_candidate(field="user_affect.signal", value="withdrawing")]
    )
    delivery = _QQDelivery()
    host = build_qq_c2c_host(
        settings=Settings(database_path=tmp_path / "qq-advisory.sqlite"),
        recipient_id="10001",
        bootstrap_at=NOW,
        model=reply,
        advisory_model=advisory,
        delivery=delivery,
    )
    try:
        result = await host.inbound_text(
            message_id="message:qq-advisory",
            recipient_id="10001",
            text="没事，当我没说。",
            observed_at=NOW,
        )
    finally:
        await host.aclose()

    assert result.action_id is not None
    assert delivery.sent == [("10001", reply.text)]
    assert len(reply.calls) == 1
    assert "appraisal_draft" in reply.calls[0][0]["content"]
    assert "user_affect.signal" in reply.calls[0][1]["content"]
    assert "withdrawing" in reply.calls[0][1]["content"]

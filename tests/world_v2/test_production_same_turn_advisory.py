from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import json

import pytest

from companion_daemon.config import Settings
from companion_daemon.llm import (
    mark_model_request_completed,
    mark_model_request_emitted,
)
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
        system = messages[0]["content"]
        if "appraisal_draft" in system and "expression_draft" in system:
            return json.dumps(
                {
                    "appraisal_draft": {
                        "appraise": False,
                        "affect": "no_change",
                        "brief_rationale": "No durable emotional implication is required.",
                        "behavior_tendency": "maintain",
                        "stance": "open",
                        "display_strategy": "natural",
                        "confidence": 7_000,
                    },
                    "expression_draft": {
                        "private_turn_state": {
                            "inner_state_summary": (
                                "我看见了当下的语气和可选线索，"
                                "现在想按自己的感受回应。"
                            ),
                            "attended_source_refs": [],
                        },
                        "timing_choice": "now",
                        "beats": [{"modality": "text", "text": self.text}],
                        "cadence": "conversational",
                        "stance": "answer_without_world_claims",
                        "brief_rationale": (
                            "I noticed the alternatives and chose my own response."
                        ),
                        "confidence": 7_600,
                        "world_claims": [],
                    },
                },
                ensure_ascii=False,
            )
        return json.dumps(
            {
                "private_turn_state": {
                    "inner_state_summary": (
                        "我重新看过当前已经落账的情境，仍想按自己的感受回应。"
                    ),
                    "attended_source_refs": [],
                },
                "timing_choice": "now",
                "beats": [{"modality": "text", "text": self.text}],
                "cadence": "conversational",
                "stance": "answer_without_world_claims",
                "brief_rationale": "I noticed the alternatives and chose my own response.",
                "confidence": 7600,
                "world_claims": [],
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


class _ExactTransportReplyModel(_ReplyModel):
    reports_exact_request_emission = True

    async def complete(
        self, messages: list[dict[str, str]], *, temperature: float = 0.8
    ) -> str:
        request_span = mark_model_request_emitted()
        try:
            return await super().complete(messages, temperature=temperature)
        finally:
            mark_model_request_completed(request_span)


class _ExactTransportAdvisoryModel(_AdvisoryModel):
    reports_exact_request_emission = True

    async def complete(
        self, messages: list[dict[str, str]], *, temperature: float = 0.2
    ) -> str:
        request_span = mark_model_request_emitted()
        try:
            return await super().complete(messages, temperature=temperature)
        finally:
            mark_model_request_completed(request_span)


class _QQDelivery:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send_text(self, recipient_id: str, text: str) -> dict[str, object]:
        self.sent.append((recipient_id, text))
        return {"status": "ok", "data": {"message_id": f"qq-{len(self.sent)}"}}


@pytest.mark.asyncio
async def test_production_expression_sees_pinned_environment_without_host_phone_story(
    tmp_path,
) -> None:
    """Time is attention material; it does not prove where the phone is or when she replies."""

    reply = _ReplyModel("我看见这句了，至于现在想怎么接，由我自己决定。")
    host = build_http_v2_capture_host(
        settings=Settings(
            database_path=tmp_path / "raw-attention-environment.sqlite",
            LOCAL_APPRAISAL_ENABLED=False,
        ),
        bootstrap_at=NOW,
        model=reply,
    )
    try:
        tick_at = NOW + timedelta(minutes=10)
        await host.tick(
            tick_id="tick:raw-attention-environment",
            logical_time_from=NOW,
            logical_time_to=tick_at,
            observed_at=tick_at,
            trace_id="trace:raw-attention-environment:clock",
            causation_id="cause:raw-attention-environment:clock",
            correlation_id="correlation:raw-attention-environment",
            reason="test_clock",
        )
        result = await host.respond(
            platform="simulator",
            platform_user_id="geoff",
            platform_message_id="message:raw-attention-environment",
            text="还没睡吗？",
            observed_at=tick_at,
        )
    finally:
        await host.aclose()

    assert result.text == reply.text
    provider_request = json.loads(reply.calls[-1][1]["content"])
    model_context = json.loads(provider_request["request"]["model_content_json"])
    assert "pinned_time" in model_context
    assert model_context["pinned_time"]["source_ref"].startswith("pinned-time:sha256:")
    assert "current_situation" in model_context["slices"]
    assert model_context["slices"]["current_situation"]["items"][0]["source_ref"]
    serialized_context = json.dumps(model_context, ensure_ascii=False)
    assert "phone_attention" not in serialized_context
    assert "attention-view." not in serialized_context
    assert "idle_phone_hours" not in serialized_context
    assert "withdrawal_affect" not in serialized_context
    assert "消息一来就能看到" not in serialized_context
    assert "手机扣在旁边" not in serialized_context
    assert "看到通知也可能先放着" not in serialized_context


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
        settings=Settings(
            database_path=tmp_path / "same-turn-advisory.sqlite",
            LOCAL_APPRAISAL_ENABLED=False,
        ),
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
    # Same-turn semantic advice is source-bound input to the role model, not a
    # durable Appraisal/Affect write that forces a second character call.
    assert len(reply.calls) == 1
    assert advisory.calls == 1
    model_request = reply.calls[-1][1]["content"]
    assert "user_affect.signal" in model_request
    assert "disappointed" in model_request
    assert "continuity.thread_signal" in model_request
    assert "possible_unfinished_share" in model_request


@pytest.mark.asyncio
async def test_foreground_advisory_provider_is_part_of_whole_turn_provider_spans(
    tmp_path,
) -> None:
    reply = _ExactTransportReplyModel("我按自己的想法接这句。")
    advisory = _ExactTransportAdvisoryModel(
        [_candidate(field="user_affect.signal", value="disappointed")]
    )
    message_id = "message:advisory-provider-span"
    host = build_http_v2_capture_host(
        settings=Settings(
            database_path=tmp_path / "advisory-provider-span.sqlite",
            LOCAL_APPRAISAL_ENABLED=False,
            WORLD_V2_RECALL_SEMANTIC_ENABLED=False,
        ),
        bootstrap_at=NOW,
        model=reply,
        advisory_model=advisory,
    )
    try:
        result = await host.respond(
            platform="simulator",
            platform_user_id="geoff",
            platform_message_id=message_id,
            text="我刚才其实有点失望。",
            observed_at=NOW,
        )
        trace = host._host._application._latency.get(  # noqa: SLF001
            f"trace:http-v2:simulator:geoff:{message_id}"
        )
        assert trace is not None
        calls = trace.role_provider_timing_evidence()["calls"]
    finally:
        await host.aclose()
        await host.wait_for_shutdown_quiescence()

    assert result.text == reply.text
    assert len(calls) == 2
    assert calls[0]["provider_call_id"] != calls[1]["provider_call_id"]
    assert all(call["status"] == "completed" for call in calls)
    assert [call["provider_kind"] for call in calls] == ["auxiliary", "role"]


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
        settings=Settings(
            database_path=tmp_path / "thinking-route.sqlite",
            LOCAL_APPRAISAL_ENABLED=False,
        ),
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
    advisory = _AdvisoryModel([], delay=2.0)
    host = build_http_v2_capture_host(
        settings=Settings(
            database_path=tmp_path / "advisory-timeout.sqlite",
                # The compiler's own ceiling is deliberately longer than the
                # ingress-to-provider budget. PinnedTurn must shorten it
                # internally and fail open instead of charging both waits.
                WORLD_V2_ADVISORY_TIMEOUT_SECONDS=2.0,
                # Keep this advisory-timeout test independent of the developer
                # machine's optional local appraisal and semantic-recall endpoints.
                LOCAL_APPRAISAL_ENABLED=False,
                WORLD_V2_RECALL_SEMANTIC_ENABLED=False,
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
        settings=Settings(
            database_path=tmp_path / "qq-advisory.sqlite",
            LOCAL_APPRAISAL_ENABLED=False,
        ),
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
    assert "appraisal_draft" not in reply.calls[0][0]["content"]
    assert "user_affect.signal" in reply.calls[-1][1]["content"]
    assert "withdrawing" in reply.calls[-1][1]["content"]

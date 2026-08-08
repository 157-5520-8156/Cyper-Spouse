from __future__ import annotations

import asyncio

import pytest

from companion_daemon.world_v2.text_turn_endpoint import (
    ChatSemanticEndpointModel,
    SemanticEndpointPrediction,
    TextTurnEndpointController,
    TextTurnEndpointEvidence,
)


class _EndpointModel:
    model = "fixture:endpoint"

    def __init__(self, probability_bp: int) -> None:
        self.probability_bp = probability_bp
        self.evidence: list[TextTurnEndpointEvidence] = []

    async def predict(
        self, evidence: TextTurnEndpointEvidence
    ) -> SemanticEndpointPrediction:
        self.evidence.append(evidence)
        return SemanticEndpointPrediction(
            continuation_probability_bp=self.probability_bp,
            confidence_bp=8_000,
            evidence_summary="semantic endpoint fixture",
            model_id=self.model,
        )


@pytest.mark.asyncio
async def test_semantic_probability_changes_only_the_bounded_listening_opportunity() -> None:
    evidence = TextTurnEndpointEvidence(
        batch_texts=("其实我想说的是",),
        recent_gap_seconds=(1.2, 1.6, 1.4),
        typing_active=False,
        burst_fragment_count=1,
        recent_message_character_counts=(4, 7, 8, 6),
    )

    likely_done = await TextTurnEndpointController(
        model=_EndpointModel(500)
    ).schedule(evidence)
    likely_continuing = await TextTurnEndpointController(
        model=_EndpointModel(9_000)
    ).schedule(evidence)

    assert likely_done.wait_ms == 100
    assert 1_800 <= likely_continuing.wait_ms <= 2_500
    assert likely_done.semantic_continuation_probability_bp == 500
    assert likely_continuing.semantic_continuation_probability_bp == 9_000
    assert likely_continuing.semantic_evidence_summary == "semantic endpoint fixture"
    assert not hasattr(likely_continuing, "reply_decision")
    assert not hasattr(likely_continuing, "response_text")


@pytest.mark.asyncio
async def test_typing_and_personal_cadence_are_advisory_endpoint_evidence() -> None:
    model = _EndpointModel(5_000)
    controller = TextTurnEndpointController(model=model)

    fast = await controller.schedule(
        TextTurnEndpointEvidence(
            batch_texts=("然后呢",),
            recent_gap_seconds=(0.3, 0.4, 0.35),
            typing_active=False,
            burst_fragment_count=1,
            recent_message_character_counts=(3, 4, 3),
        )
    )
    slow_typing = await controller.schedule(
        TextTurnEndpointEvidence(
            batch_texts=("然后呢",),
            recent_gap_seconds=(1.8, 2.2, 2.0),
            typing_active=True,
            burst_fragment_count=2,
            recent_message_character_counts=(18, 26, 20),
        )
    )

    assert slow_typing.wait_ms > fast.wait_ms
    assert "typing_active" in slow_typing.reason_codes
    assert "personal_cadence" in slow_typing.reason_codes
    assert model.evidence[-1].batch_texts == ("然后呢",)


class _BrokenEndpointModel:
    model = "fixture:broken-endpoint"

    async def predict(
        self, evidence: TextTurnEndpointEvidence
    ) -> SemanticEndpointPrediction:
        del evidence
        raise RuntimeError("local endpoint unavailable")


@pytest.mark.asyncio
async def test_endpoint_model_failure_falls_back_without_delaying_a_single_bubble() -> None:
    schedule = await TextTurnEndpointController(
        model=_BrokenEndpointModel(), timeout_seconds=0.05
    ).schedule(
        TextTurnEndpointEvidence(
            batch_texts=("早",),
            recent_gap_seconds=(),
            typing_active=False,
            burst_fragment_count=1,
            recent_message_character_counts=(),
        )
    )

    assert schedule.status == "fallback"
    assert schedule.wait_ms == 100
    assert schedule.semantic_continuation_probability_bp is None
    assert schedule.failure_code == "RuntimeError"


class _HangingEndpointModel:
    model = "fixture:hanging-endpoint"

    def __init__(self) -> None:
        self.cancelled = False

    async def predict(
        self, evidence: TextTurnEndpointEvidence
    ) -> SemanticEndpointPrediction:
        del evidence
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        raise AssertionError("unreachable")


@pytest.mark.asyncio
async def test_endpoint_model_timeout_is_bounded() -> None:
    model = _HangingEndpointModel()
    controller = TextTurnEndpointController(model=model, timeout_seconds=0.01)
    evidence = TextTurnEndpointEvidence(
        batch_texts=("等一下",),
        recent_gap_seconds=(),
        typing_active=False,
        burst_fragment_count=1,
        recent_message_character_counts=(),
    )
    try:
        schedule = await controller.schedule(evidence)

        assert schedule.status == "fallback"
        assert schedule.failure_code == "timeout"
        assert schedule.wait_ms == 100
        assert model.cancelled is False

        # A new bubble invalidates the old estimate but cannot create a second
        # in-flight provider request while the first one is still terminally
        # unresolved.
        busy = await controller.schedule(
            TextTurnEndpointEvidence(
                batch_texts=("等一下，我还没说完",),
                recent_gap_seconds=(0.3,),
                typing_active=False,
                burst_fragment_count=2,
                recent_message_character_counts=(3, 8),
            )
        )
        assert busy.failure_code == "model_busy"
        assert controller.health_snapshot()["prediction_in_flight"] is True
    finally:
        await controller.aclose()

    assert model.cancelled is True


@pytest.mark.asyncio
async def test_endpoint_close_is_bounded_when_provider_suppresses_cancellation() -> None:
    class SuppressingEndpointModel(_HangingEndpointModel):
        def __init__(self) -> None:
            super().__init__()
            self.release = asyncio.Event()

        async def predict(
            self, evidence: TextTurnEndpointEvidence
        ) -> SemanticEndpointPrediction:
            del evidence
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled = True
                await self.release.wait()
            raise RuntimeError("released after cancellation")

    model = SuppressingEndpointModel()
    controller = TextTurnEndpointController(model=model, timeout_seconds=0.01)
    await controller.schedule(
        TextTurnEndpointEvidence(
            batch_texts=("先等等",),
            recent_gap_seconds=(),
            typing_active=False,
            burst_fragment_count=1,
            recent_message_character_counts=(),
        )
    )
    try:
        await asyncio.wait_for(controller.aclose(), timeout=0.2)
        assert model.cancelled is True
    finally:
        model.release.set()
        await asyncio.sleep(0)


class _EndpointChat:
    model = "fixture:local-qwen-endpoint"

    def __init__(self, raw: str) -> None:
        self.raw = raw
        self.messages: list[list[dict[str, str]]] = []

    async def complete_json(
        self, messages: list[dict[str, str]], *, temperature: float = 0.0
    ) -> str:
        assert temperature == 0.0
        self.messages.append(messages)
        return self.raw


@pytest.mark.asyncio
async def test_chat_endpoint_model_returns_only_a_probability_estimate() -> None:
    chat = _EndpointChat(
        '{"continuation_probability_bp":8200,"confidence_bp":7600,'
        '"evidence_summary":"the current bubble is semantically unfinished",'
        '"compact_reply_hint_bp":7400}'
    )
    model = ChatSemanticEndpointModel(chat)
    evidence = TextTurnEndpointEvidence(
        batch_texts=("还有就是", "我下午本来想"),
        recent_gap_seconds=(1.1, 1.5),
        typing_active=True,
        burst_fragment_count=2,
        recent_message_character_counts=(5, 8, 12),
        recent_character_message_character_counts=(4, 16, 7),
        recent_exchange_user_bubble_counts=(1, 4, 2),
        recent_exchange_character_bubble_counts=(1, 1, 3),
    )

    prediction = await model.predict(evidence)

    assert prediction.continuation_probability_bp == 8_200
    assert prediction.confidence_bp == 7_600
    assert prediction.compact_reply_hint_bp == 7_400
    assert prediction.model_id == chat.model
    prompt = chat.messages[0][-1]["content"]
    assert "我下午本来想" in prompt
    assert "recent_gap_seconds" in prompt
    assert '"recent_exchange_user_bubble_counts":[1,4,2]' in prompt
    assert '"recent_exchange_character_bubble_counts":[1,1,3]' in prompt
    assert '"recent_character_message_character_counts":[4,16,7]' in prompt
    assert "whether or how the character replies" in chat.messages[0][0]["content"]


@pytest.mark.asyncio
async def test_chat_endpoint_model_rejects_behavior_fields() -> None:
    chat = _EndpointChat(
        '{"continuation_probability_bp":1000,"confidence_bp":9000,'
        '"evidence_summary":"done","reply_decision":"now"}'
    )

    with pytest.raises(ValueError, match="exactly"):
        await ChatSemanticEndpointModel(chat).predict(
            TextTurnEndpointEvidence(
                batch_texts=("说完了",),
                recent_gap_seconds=(),
                typing_active=False,
                burst_fragment_count=1,
                recent_message_character_counts=(),
            )
        )

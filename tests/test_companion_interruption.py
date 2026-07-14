import json

import pytest

from companion_daemon.companion_interruption import (
    CompanionInterruptionContext,
    ModelCompanionInterruptionAdvisor,
    interruption_advice_from_model_json,
)


def _context() -> CompanionInterruptionContext:
    return CompanionInterruptionContext(
        pending_count=1,
        latest_text="不是，我不同意你这个说法，",
        merged_text="不是，我不同意你这个说法，",
        cadence_heat="hot",
        relationship_stage="friend",
        base_wait_seconds=1.5,
        base_reason="latest_message_continues",
    )


def test_model_interruption_advice_accepts_evidence_bound_disagreement() -> None:
    advice = interruption_advice_from_model_json(
        _context(),
        json.dumps(
            {
                "should_interrupt": True,
                "motive": "disagreement",
                "confidence": 0.82,
                "wait_seconds": 0.25,
                "evidence_spans": ["不同意"],
                "rationale": "the companion may jump in because she disagrees",
            },
            ensure_ascii=False,
        ),
    )

    assert advice is not None
    assert advice.reason == "semantic_companion_interruption:disagreement"
    assert advice.wait_seconds == 0.25
    assert advice.evidence_spans == ("不同意",)


def test_model_interruption_advice_rejects_low_confidence_or_unsupported_evidence() -> None:
    low = interruption_advice_from_model_json(
        _context(),
        json.dumps(
            {
                "should_interrupt": True,
                "motive": "interest",
                "confidence": 0.4,
                "wait_seconds": 0.1,
                "evidence_spans": ["不同意"],
            },
            ensure_ascii=False,
        ),
    )
    unsupported = interruption_advice_from_model_json(
        _context(),
        json.dumps(
            {
                "should_interrupt": True,
                "motive": "interest",
                "confidence": 0.9,
                "wait_seconds": 0.1,
                "evidence_spans": ["用户其实很想被打断"],
            },
            ensure_ascii=False,
        ),
    )

    assert low is None
    assert unsupported is None


@pytest.mark.asyncio
async def test_model_interruption_advisor_returns_none_on_bad_json() -> None:
    class Model:
        async def complete(self, messages, *, temperature: float) -> str:  # type: ignore[no-untyped-def]
            return "not-json"

    assert await ModelCompanionInterruptionAdvisor(Model()).advise(_context()) is None

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import pytest

from companion_daemon.world_v2.external_world_perception.live_acceptance import (
    LifeWakingExternalPerceptionAcceptance,
)


NOW = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)


@dataclass(frozen=True)
class Binding:
    perception_event_ref: str


@dataclass(frozen=True)
class Receipt:
    attention_attempt_id: str
    perceptions: tuple[Binding, ...]


class Acceptance:
    def __init__(self, receipt: Receipt) -> None:
        self.receipt = receipt
        self.calls = 0

    def accept_delivery(self, delivery: object) -> Receipt:
        assert delivery == "delivery"
        self.calls += 1
        return self.receipt


class Life:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    async def advance_life_ecology_once(self, **kwargs: str) -> object:
        self.calls.append(kwargs)
        return object()


@pytest.mark.asyncio
async def test_one_accepted_batch_opens_exactly_one_life_wake() -> None:
    acceptance = Acceptance(
        Receipt(
            attention_attempt_id="attempt:1",
            perceptions=(Binding("event:perception:1"), Binding("event:perception:2")),
        )
    )
    life = Life()
    bridge = LifeWakingExternalPerceptionAcceptance(
        acceptance=acceptance,
        life=life,
        trace_prefix="trace:external-perception",
    )

    receipt = await bridge.accept("delivery")

    assert receipt == acceptance.receipt
    assert acceptance.calls == 1
    assert life.calls == [
        {
            "wake_event_ref": "event:perception:1",
            "trace_id": "trace:external-perception:attempt:1",
            "correlation_id": "external-perception:attempt:1",
        }
    ]


@pytest.mark.asyncio
async def test_empty_acceptance_receipt_fails_closed_before_life() -> None:
    acceptance = Acceptance(Receipt(attention_attempt_id="attempt:empty", perceptions=()))
    life = Life()
    bridge = LifeWakingExternalPerceptionAcceptance(acceptance=acceptance, life=life)

    with pytest.raises(ValueError, match="has no perception wake"):
        await bridge.accept("delivery")

    assert life.calls == []

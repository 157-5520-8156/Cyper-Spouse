"""Small non-network adapters for the CompanionTurn seam."""

from __future__ import annotations

from dataclasses import dataclass, field
from time import monotonic

from companion_daemon.companion_turn import (
    DispatchAcceptance,
    TurnBeat,
    TurnTransport,
)


@dataclass
class CaptureTurnTransport(TurnTransport):
    """Immediately-delivered transport for simulators, evaluations, and replay tests."""

    receipt_namespace: str = "capture"
    beats: list[TurnBeat] = field(default_factory=list)
    first_dispatched_at: float | None = None

    async def dispatch(self, beat: TurnBeat) -> DispatchAcceptance:
        if self.first_dispatched_at is None:
            self.first_dispatched_at = monotonic()
        self.beats.append(beat)
        return DispatchAcceptance(
            status="delivered",
            external_receipt=(f"{self.receipt_namespace}:{beat.action_id}:{beat.segment_id}"),
        )

    @property
    def text(self) -> str:
        return "".join(beat.text for beat in self.beats)

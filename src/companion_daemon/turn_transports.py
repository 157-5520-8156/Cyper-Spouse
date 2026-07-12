"""Small non-network adapters for the CompanionTurn seam."""

from __future__ import annotations

from dataclasses import dataclass, field

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

    async def dispatch(self, beat: TurnBeat) -> DispatchAcceptance:
        self.beats.append(beat)
        return DispatchAcceptance(
            status="delivered",
            external_receipt=(f"{self.receipt_namespace}:{beat.action_id}:{beat.segment_id}"),
        )

    @property
    def text(self) -> str:
        return "".join(beat.text for beat in self.beats)

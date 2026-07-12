"""Stable I/O contract for platform adapters outside the world write model."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable


ActionTerminalState = Literal["delivered", "failed", "cancelled", "expired", "unknown"]
ACTION_TERMINAL_STATES = frozenset(
    {"delivered", "failed", "cancelled", "expired", "unknown"}
)


@dataclass(frozen=True)
class OutboundEnvelope:
    action_id: str
    recipient_id: str
    kind: str
    text: str | None = None
    media_path: str | None = None

    def __post_init__(self) -> None:
        if not self.action_id.strip():
            raise ValueError("action_id is required")
        if not self.recipient_id.strip():
            raise ValueError("recipient_id is required")
        if not self.kind.strip():
            raise ValueError("kind is required")
        if not self.text and not self.media_path:
            raise ValueError("an outbound envelope requires text or media_path")


@dataclass(frozen=True)
class DispatchAcceptance:
    action_id: str
    accepted: bool
    platform_message_id: str | None
    receipt_query_token: str | None


@dataclass(frozen=True)
class DeliveryReceipt:
    action_id: str
    status: ActionTerminalState
    external_receipt: str | None = None


@runtime_checkable
class OutboundPlatformAdapter(Protocol):
    platform: str
    is_fake: bool
    live_account_connected: bool

    async def dispatch(self, envelope: OutboundEnvelope) -> DispatchAcceptance: ...

    async def lookup_delivery(self, receipt_query_token: str) -> DeliveryReceipt: ...

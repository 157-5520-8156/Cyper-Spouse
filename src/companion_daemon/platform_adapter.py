"""Stable I/O contract for platform adapters outside the world write model."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
import inspect
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


@dataclass(frozen=True)
class RecordedPlatformDispatch:
    """Persisted adapter evidence loaded during process recovery."""

    action_id: str
    status: ActionTerminalState
    receipt_query_token: str | None


async def reconcile_unknown_dispatches(
    adapter: OutboundPlatformAdapter,
    records: Iterable[RecordedPlatformDispatch],
    settle: Callable[[DeliveryReceipt], object | Awaitable[object]],
) -> tuple[str, ...]:
    """Query unknown dispatches once and forward only evidenced terminal results."""

    seen: set[str] = set()
    reconciled: list[str] = []
    for record in records:
        if (
            record.status != "unknown"
            or not record.receipt_query_token
            or record.action_id in seen
        ):
            continue
        seen.add(record.action_id)
        receipt = await adapter.lookup_delivery(record.receipt_query_token)
        if receipt.action_id != record.action_id:
            raise ValueError(
                f"receipt action {receipt.action_id!r} does not match {record.action_id!r}"
            )
        if receipt.status == "unknown":
            continue
        evidenced = receipt
        if not receipt.external_receipt:
            evidenced = DeliveryReceipt(
                action_id=receipt.action_id,
                status=receipt.status,
                external_receipt=f"{adapter.platform}:query:{record.receipt_query_token}",
            )
        result = settle(evidenced)
        if inspect.isawaitable(result):
            await result
        reconciled.append(record.action_id)
    return tuple(reconciled)


@runtime_checkable
class OutboundPlatformAdapter(Protocol):
    platform: str
    is_fake: bool
    live_account_connected: bool

    async def dispatch(self, envelope: OutboundEnvelope) -> DispatchAcceptance: ...

    async def lookup_delivery(self, receipt_query_token: str) -> DeliveryReceipt: ...

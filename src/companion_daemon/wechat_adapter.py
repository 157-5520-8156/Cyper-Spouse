"""Disabled/fake-only WeChat seam until a real account is explicitly authorized."""

from __future__ import annotations

from companion_daemon.config import Settings
from companion_daemon.platform_adapter import (
    ACTION_TERMINAL_STATES,
    ActionTerminalState,
    DeliveryReceipt,
    DispatchAcceptance,
    OutboundEnvelope,
)


class FakeWeChatAdapter:
    platform = "wechat"
    is_fake = True
    live_account_connected = False

    def __init__(self) -> None:
        self.dispatched: list[OutboundEnvelope] = []
        self._envelopes: dict[str, OutboundEnvelope] = {}
        self._acceptances: dict[str, DispatchAcceptance] = {}
        self._receipts: dict[str, DeliveryReceipt] = {}
        self._query_tokens: dict[str, str] = {}

    async def dispatch(self, envelope: OutboundEnvelope) -> DispatchAcceptance:
        existing = self._envelopes.get(envelope.action_id)
        if existing is not None:
            if existing != envelope:
                raise ValueError(
                    f"action_id {envelope.action_id!r} was already dispatched with different content"
                )
            return self._acceptances[envelope.action_id]

        message_id = f"fake-wechat:{envelope.action_id}"
        query_token = f"fake-wechat-receipt:{envelope.action_id}"
        acceptance = DispatchAcceptance(
            action_id=envelope.action_id,
            accepted=True,
            platform_message_id=message_id,
            receipt_query_token=query_token,
        )
        self.dispatched.append(envelope)
        self._envelopes[envelope.action_id] = envelope
        self._acceptances[envelope.action_id] = acceptance
        self._query_tokens[query_token] = envelope.action_id
        self._receipts[envelope.action_id] = DeliveryReceipt(
            action_id=envelope.action_id,
            status="unknown",
        )
        return acceptance

    async def lookup_delivery(self, receipt_query_token: str) -> DeliveryReceipt:
        try:
            action_id = self._query_tokens[receipt_query_token]
        except KeyError as exc:
            raise LookupError(f"unknown fake WeChat receipt token: {receipt_query_token}") from exc
        return self._receipts[action_id]

    def settle(
        self,
        action_id: str,
        *,
        status: ActionTerminalState,
        external_receipt: str | None = None,
    ) -> DeliveryReceipt:
        if action_id not in self._envelopes:
            raise LookupError(f"unknown fake WeChat action: {action_id}")
        if status not in ACTION_TERMINAL_STATES:
            raise ValueError(f"invalid Action terminal state: {status!r}")
        receipt = DeliveryReceipt(
            action_id=action_id,
            status=status,
            external_receipt=external_receipt,
        )
        self._receipts[action_id] = receipt
        return receipt


def build_wechat_adapter(settings: Settings) -> FakeWeChatAdapter | None:
    """Construct only the explicitly non-live WeChat implementations."""

    if settings.wechat_adapter == "disabled":
        return None
    if settings.wechat_adapter == "fake":
        return FakeWeChatAdapter()
    raise ValueError(f"live WeChat adapters are not enabled: {settings.wechat_adapter!r}")

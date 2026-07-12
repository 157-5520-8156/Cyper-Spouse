from __future__ import annotations

import pytest

from companion_daemon.platform_adapter import (
    DeliveryReceipt,
    RecordedPlatformDispatch,
    reconcile_unknown_dispatches,
)


@pytest.mark.asyncio
async def test_restart_recovery_queries_only_unknown_and_deduplicates_late_receipts() -> None:
    class FakeAdapter:
        platform = "qq-test"
        is_fake = True
        live_account_connected = False

        def __init__(self) -> None:
            self.queries: list[str] = []

        async def lookup_delivery(self, receipt_query_token: str) -> DeliveryReceipt:
            self.queries.append(receipt_query_token)
            if receipt_query_token == "query:delivered":
                return DeliveryReceipt(action_id="outgoing:71", status="delivered")
            return DeliveryReceipt(action_id="outgoing:72", status="unknown")

    adapter = FakeAdapter()
    settled: list[DeliveryReceipt] = []
    records = [
        RecordedPlatformDispatch("outgoing:70", "delivered", "query:already-done"),
        RecordedPlatformDispatch("outgoing:71", "unknown", "query:delivered"),
        RecordedPlatformDispatch("outgoing:71", "unknown", "query:delivered"),
        RecordedPlatformDispatch("outgoing:72", "unknown", "query:pending"),
        RecordedPlatformDispatch("outgoing:73", "unknown", None),
    ]

    reconciled = await reconcile_unknown_dispatches(adapter, records, settled.append)

    assert adapter.queries == ["query:delivered", "query:pending"]
    assert reconciled == ("outgoing:71",)
    assert settled == [
        DeliveryReceipt(
            action_id="outgoing:71",
            status="delivered",
            external_receipt="qq-test:query:query:delivered",
        )
    ]

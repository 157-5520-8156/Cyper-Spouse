from __future__ import annotations

from datetime import UTC, datetime

import pytest

from companion_daemon.world_v2.platform_action_executor import PlatformDispatchRequest
from companion_daemon.world_v2.qq_c2c_transport import QQC2CPlatformTransport


NOW = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)


class _Delivery:
    def __init__(self, response: dict[str, object]) -> None:
        self.response = response
        self.sent: list[tuple[str, str]] = []

    async def send_text(self, recipient_id: str, text: str) -> dict[str, object]:
        self.sent.append((recipient_id, text))
        return self.response


def _request(*, target: str = "conversation:qq:c2c:owner") -> PlatformDispatchRequest:
    return PlatformDispatchRequest(
        action_id="action:qq-c2c:1",
        kind="reply",
        target=target,
        payload_ref="payload:qq-c2c:1",
        payload_hash="sha256:" + "a" * 64,
        content_type="text/plain",
        body="我在。",
        idempotency_key="idempotency:qq-c2c:1",
    )


@pytest.mark.asyncio
async def test_qq_c2c_transport_uses_composition_owned_target_and_never_claims_delivery() -> None:
    delivery = _Delivery({"id": "qq-message-1"})
    transport = QQC2CPlatformTransport(
        delivery=delivery,
        recipients_by_target={"conversation:qq:c2c:owner": "open-id-1"},
        now=lambda: NOW,
    )

    receipt = await transport.send(_request())

    assert delivery.sent == [("open-id-1", "我在。")]
    assert receipt.status == "provider_accepted"
    assert receipt.provider_ref == "platform:id:qq-message-1"
    assert receipt.raw_payload_hash.startswith("sha256:")


@pytest.mark.asyncio
async def test_qq_c2c_transport_reuses_only_an_exact_in_memory_idempotent_request() -> None:
    delivery = _Delivery({"message_id": "qq-message-1"})
    transport = QQC2CPlatformTransport(
        delivery=delivery,
        recipients_by_target={"conversation:qq:c2c:owner": "open-id-1"},
        now=lambda: NOW,
    )
    request = _request()

    first = await transport.send(request)
    second = await transport.send(request)
    lookup = await transport.lookup(
        idempotency_key=request.idempotency_key,
        request_fingerprint=request.fingerprint,
    )

    assert delivery.sent == [("open-id-1", "我在。")]
    assert second == first == lookup


@pytest.mark.asyncio
async def test_qq_c2c_transport_rejects_unowned_target_before_any_provider_send() -> None:
    delivery = _Delivery({"message_id": "qq-message-1"})
    transport = QQC2CPlatformTransport(
        delivery=delivery,
        recipients_by_target={"conversation:qq:c2c:owner": "open-id-1"},
        now=lambda: NOW,
    )

    with pytest.raises(ValueError, match="not owned"):
        await transport.send(_request(target="conversation:qq:c2c:someone-else"))

    assert delivery.sent == []

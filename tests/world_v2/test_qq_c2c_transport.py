from __future__ import annotations

from datetime import UTC, datetime
import hashlib

import pytest

from companion_daemon.world_v2.platform_action_executor import (
    PlatformActionExecutor,
    PlatformDispatchRequest,
    ResolvedActionPayload,
)
from companion_daemon.world_v2.qq_c2c_transport import QQC2CPlatformTransport
from companion_daemon.world_v2.schemas import Action


NOW = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)


class _Delivery:
    def __init__(self, response: dict[str, object]) -> None:
        self.response = response
        self.sent: list[tuple[str, str]] = []

    async def send_text(self, recipient_id: str, text: str) -> dict[str, object]:
        self.sent.append((recipient_id, text))
        return self.response

    async def send_reaction(
        self, recipient_id: str, *, message_id: str, reaction_id: str
    ) -> dict[str, object]:
        self.sent.append((recipient_id, f"reaction:{message_id}:{reaction_id}"))
        return self.response

    async def send_sticker(self, recipient_id: str, *, sticker_id: str) -> dict[str, object]:
        self.sent.append((recipient_id, f"sticker:{sticker_id}"))
        return self.response

    async def send_typing(self, recipient_id: str, *, state: str) -> dict[str, object]:
        self.sent.append((recipient_id, f"typing:{state}"))
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
async def test_qq_c2c_transport_preserves_an_explicit_provider_rejection() -> None:
    delivery = _Delivery(
        {"status": "failed", "retcode": 100, "message": "account risk policy rejected"}
    )
    transport = QQC2CPlatformTransport(
        delivery=delivery,
        recipients_by_target={"conversation:qq:c2c:owner": "open-id-1"},
        now=lambda: NOW,
    )

    receipt = await transport.send(_request())

    assert receipt.status == "failed"
    assert receipt.error_class == "provider_rejected"
    assert receipt.provider_ref.startswith("qq-c2c:rejected:")


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ("followup", "proactive_message"))
async def test_platform_executor_preserves_social_kind_through_real_qq_transport(
    kind: str,
) -> None:
    text = "刚才那件事我又想了一下。"
    delivery = _Delivery({"status": "ok", "data": {"message_id": "qq-social-1"}})
    inner = QQC2CPlatformTransport(
        delivery=delivery,
        recipients_by_target={"conversation:qq:c2c:owner": "open-id-1"},
        now=lambda: NOW,
    )

    class PolicyObservingTransport:
        provider = inner.provider

        def __init__(self) -> None:
            self.kinds: list[str] = []

        async def send(self, request):  # type: ignore[no-untyped-def]
            self.kinds.append(request.kind)
            return await inner.send(request)

        async def lookup(self, **kwargs):  # type: ignore[no-untyped-def]
            return await inner.lookup(**kwargs)

    class Payloads:
        async def resolve(self, current):  # type: ignore[no-untyped-def]
            return ResolvedActionPayload(
                payload_ref=current.payload_ref, payload_hash=current.payload_hash,
                content_type="text/plain", body=text,
            )

    action = Action(
        schema_version="world-v2.1", action_id=f"action:qq-social:{kind}",
        world_id="world:qq-social",
        logical_time=NOW, created_at=NOW, trace_id="trace:qq-social",
        causation_id="acceptance:qq-social", correlation_id="conversation:qq-social",
        kind=kind, layer="external_action", intent_ref=f"intent:qq-social:{kind}",
        actor="agent:companion", target="conversation:qq:c2c:owner",
        payload_ref=f"payload:qq-social:{kind}",
        payload_hash="sha256:" + hashlib.sha256(text.encode()).hexdigest(),
        idempotency_key=f"idempotency:qq-social:{kind}",
        budget_reservation_id=f"reservation:qq-social:{kind}", state="authorized",
        recovery_policy="effect_once",
    )
    transport = PolicyObservingTransport()

    receipt = await PlatformActionExecutor(
        payloads=Payloads(), transport=transport
    ).dispatch(action)

    assert receipt is not None and receipt.status == "provider_accepted"
    assert transport.kinds == [kind]
    assert delivery.sent == [("open-id-1", text)]


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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "content_type", "body", "sent"),
    (
        (
            "reaction",
            "application/vnd.world-v2.reaction+json",
            '{"provider_message_id":"qq-message-1","reaction_id":"like","version":"expression-reaction.1"}',
            "reaction:qq-message-1:like",
        ),
        (
            "sticker",
            "application/vnd.world-v2.sticker+json",
            '{"sticker_id":"qq-face:14","version":"expression-sticker.1"}',
            "sticker:qq-face:14",
        ),
        (
            "typing",
            "application/vnd.world-v2.typing+json",
            '{"state":"composing","version":"expression-typing.1"}',
            "typing:composing",
        ),
    ),
)
async def test_qq_c2c_transport_executes_each_authorized_expression_modality(
    kind: str, content_type: str, body: str, sent: str
) -> None:
    delivery = _Delivery({"message_id": f"qq-{kind}-receipt"})
    transport = QQC2CPlatformTransport(
        delivery=delivery,
        recipients_by_target={"conversation:qq:c2c:owner": "open-id-1"},
        now=lambda: NOW,
    )
    request = _request().model_copy(update={
        "action_id": f"action:qq-c2c:{kind}",
        "kind": kind,
        "payload_ref": f"payload:qq-c2c:{kind}",
        "payload_hash": "sha256:" + hashlib.sha256(body.encode()).hexdigest(),
        "content_type": content_type,
        "body": body,
        "idempotency_key": f"idempotency:qq-c2c:{kind}",
    })

    receipt = await transport.send(request)

    assert delivery.sent == [("open-id-1", sent)]
    assert receipt.status == "provider_accepted"


class _VerifiableDelivery(_Delivery):
    def __init__(
        self, response: dict[str, object], lookup_response: object
    ) -> None:
        super().__init__(response)
        self.lookup_response = lookup_response
        self.lookups: list[tuple[str, str]] = []

    async def get_message(
        self, recipient_id: str, *, message_id: str
    ) -> dict[str, object]:
        self.lookups.append((recipient_id, message_id))
        if isinstance(self.lookup_response, Exception):
            raise self.lookup_response
        return self.lookup_response  # type: ignore[return-value]


@pytest.mark.asyncio
async def test_qq_c2c_verify_delivery_upgrades_a_positively_looked_up_message() -> None:
    delivery = _VerifiableDelivery(
        {"status": "ok", "data": {"message_id": "10001"}},
        {"status": "ok", "retcode": 0, "data": {"message_id": 10001, "message": "我在。"}},
    )
    transport = QQC2CPlatformTransport(
        delivery=delivery,
        recipients_by_target={"conversation:qq:c2c:owner": "open-id-1"},
        now=lambda: NOW,
    )
    ack = await transport.send(_request())
    assert ack.status == "provider_accepted"

    verified = await transport.verify_delivery(
        idempotency_key=_request().idempotency_key,
        target="conversation:qq:c2c:owner",
        provider_ref=ack.provider_ref,
    )

    assert delivery.lookups == [("open-id-1", "10001")]
    assert verified is not None
    assert verified.status == "delivered"
    assert verified.provider_ref == f"{ack.provider_ref}:verified"
    assert verified.idempotency_key == _request().idempotency_key


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "lookup_response",
    (
        {"status": "failed", "retcode": 1200, "message": "msg not found"},
        {"status": "ok", "retcode": 0, "data": {"message_id": "different-id"}},
        {"status": "ok", "retcode": 0},
        ConnectionError("napcat down"),
    ),
)
async def test_qq_c2c_verify_delivery_stays_uncertain_without_positive_evidence(
    lookup_response: object,
) -> None:
    delivery = _VerifiableDelivery(
        {"status": "ok", "data": {"message_id": "10001"}}, lookup_response
    )
    transport = QQC2CPlatformTransport(
        delivery=delivery,
        recipients_by_target={"conversation:qq:c2c:owner": "open-id-1"},
        now=lambda: NOW,
    )

    verified = await transport.verify_delivery(
        idempotency_key="idempotency:qq-c2c:1",
        target="conversation:qq:c2c:owner",
        provider_ref="platform:message_id:10001",
    )

    assert verified is None


@pytest.mark.asyncio
async def test_qq_c2c_verify_delivery_requires_a_platform_ref_and_lookup_capability() -> None:
    lookup_capable = QQC2CPlatformTransport(
        delivery=_VerifiableDelivery({}, {"status": "ok", "retcode": 0}),
        recipients_by_target={"conversation:qq:c2c:owner": "open-id-1"},
        now=lambda: NOW,
    )
    assert (
        await lookup_capable.verify_delivery(
            idempotency_key="idempotency:qq-c2c:1",
            target="conversation:qq:c2c:owner",
            provider_ref="qq-c2c:unverified:abc",
        )
        is None
    )
    no_lookup = QQC2CPlatformTransport(
        delivery=_Delivery({}),
        recipients_by_target={"conversation:qq:c2c:owner": "open-id-1"},
        now=lambda: NOW,
    )
    assert (
        await no_lookup.verify_delivery(
            idempotency_key="idempotency:qq-c2c:1",
            target="conversation:qq:c2c:owner",
            provider_ref="platform:message_id:10001",
        )
        is None
    )


@pytest.mark.asyncio
async def test_qq_c2c_transport_rejects_untrusted_or_malformed_expression_payloads() -> None:
    delivery = _Delivery({"message_id": "never"})
    transport = QQC2CPlatformTransport(
        delivery=delivery,
        recipients_by_target={"conversation:qq:c2c:owner": "open-id-1"},
        now=lambda: NOW,
    )
    request = _request().model_copy(update={
        "kind": "reaction",
        "content_type": "application/vnd.world-v2.reaction+json",
        "body": '{"reaction_id":"like"}',
    })

    with pytest.raises(ValueError, match="reaction payload"):
        await transport.send(request)

    assert delivery.sent == []

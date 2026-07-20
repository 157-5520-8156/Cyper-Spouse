import json
from types import SimpleNamespace

import httpx
import pytest

import companion_daemon.onebot_adapter as onebot_adapter
from companion_daemon.qq_delivery import QQDelivery


def test_receipt_candidate_supports_official_objects_and_onebot_data_envelopes() -> None:
    assert QQDelivery.receipt_candidate(SimpleNamespace(id="official-81")) == (
        "platform:id:official-81"
    )
    assert QQDelivery.receipt_candidate(
        {"status": "ok", "data": {"message_id": 82}}
    ) == "platform:message_id:82"


@pytest.mark.asyncio
async def test_napcat_delivery_sends_background_text_to_configured_qq(monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"status": "ok"})

    original_client = httpx.AsyncClient

    def fake_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original_client(*args, **kwargs)

    monkeypatch.setattr(onebot_adapter.httpx, "AsyncClient", fake_client)
    settings = SimpleNamespace(
        qq_adapter="napcat",
        napcat_api_url="http://127.0.0.1:3000",
        napcat_access_token="token",
        napcat_proactive_user_id="2759284998",
        qq_bot_app_id=None,
        qq_bot_secret=None,
    )

    delivery = QQDelivery(settings)
    assert delivery.proactive_recipient_id() == "2759284998"
    response = await delivery.send_text("2759284998", "隔一会儿想补一句。")
    assert response == {"status": "ok"}

    assert requests[0].url.path == "/send_private_msg"
    assert requests[0].headers["Authorization"] == "Bearer token"
    assert json.loads(requests[0].content) == {
        "message": "隔一会儿想补一句。",
        "user_id": 2759284998,
    }


@pytest.mark.asyncio
async def test_napcat_delivery_maps_authorized_expression_tokens_to_exact_endpoints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"status": "ok", "data": {"message_id": 91}})

    original_client = httpx.AsyncClient

    def fake_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original_client(*args, **kwargs)

    monkeypatch.setattr(onebot_adapter.httpx, "AsyncClient", fake_client)
    settings = SimpleNamespace(
        qq_adapter="napcat",
        napcat_api_url="http://127.0.0.1:3000",
        napcat_access_token="token",
        napcat_proactive_user_id="2759284998",
        qq_bot_app_id=None,
        qq_bot_secret=None,
    )
    delivery = QQDelivery(settings)

    await delivery.send_reaction(
        "2759284998", message_id="incoming-77", reaction_id="like"
    )
    await delivery.send_sticker("2759284998", sticker_id="qq-face:14")
    await delivery.send_typing("2759284998", state="composing")

    assert [request.url.path for request in requests] == [
        "/set_msg_emoji_like",
        "/send_private_msg",
        "/set_input_status",
    ]
    assert [json.loads(request.content) for request in requests] == [
        {
            "message_id": "incoming-77",
            "emoji_id": "128077",
            "set": True,
        },
        {
            "message": [{"type": "face", "data": {"id": "14"}}],
            "user_id": 2759284998,
        },
        {"user_id": "2759284998", "event_type": 1},
    ]
    assert all(request.headers["Authorization"] == "Bearer token" for request in requests)


@pytest.mark.asyncio
async def test_generic_onebot_delivery_uses_its_own_endpoint_and_proactive_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"status": "ok"})

    original_client = httpx.AsyncClient

    def fake_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original_client(*args, **kwargs)

    monkeypatch.setattr(onebot_adapter.httpx, "AsyncClient", fake_client)
    settings = SimpleNamespace(
        qq_adapter="onebot",
        napcat_api_url="http://127.0.0.1:3000",
        napcat_access_token="napcat-token",
        napcat_proactive_user_id="2759284998",
        onebot_api_url="http://127.0.0.1:5700",
        onebot_access_token="onebot-token",
        onebot_proactive_user_id="123456789",
        qq_bot_app_id=None,
        qq_bot_secret=None,
    )

    delivery = QQDelivery(settings)

    assert delivery.proactive_recipient_id() == "123456789"
    response = await delivery.send_text("123456789", "这句应走通用 OneBot。")
    assert response == {"status": "ok"}

    assert str(requests[0].url).startswith("http://127.0.0.1:5700/")
    assert requests[0].headers["Authorization"] == "Bearer onebot-token"
    assert json.loads(requests[0].content)["user_id"] == 123456789

import json
from pathlib import Path

import httpx
import pytest

from companion_daemon.qq_client import QQOfficialClient


@pytest.mark.asyncio
async def test_send_c2c_text_gets_token_and_posts_message() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/app/getAppAccessToken":
            return httpx.Response(200, json={"access_token": "token-1", "expires_in": "7200"})
        if request.url.path == "/v2/users/user-openid/messages":
            assert request.headers["Authorization"] == "QQBot token-1"
            assert json.loads(request.content) == {
                "content": "在呢",
                "msg_type": 0,
                "msg_id": "msg-1",
            }
            return httpx.Response(200, json={"id": "sent-1", "timestamp": 1})
        return httpx.Response(404)

    client = QQOfficialClient(
        "app-id",
        "secret",
        api_base_url="https://api.sgroup.qq.com",
        token_url="https://bots.qq.com/app/getAppAccessToken",
        transport=httpx.MockTransport(handler),
    )

    result = await client.send_c2c_text("user-openid", "在呢", msg_id="msg-1")

    assert result["id"] == "sent-1"
    assert [request.url.path for request in requests] == [
        "/app/getAppAccessToken",
        "/v2/users/user-openid/messages",
    ]


@pytest.mark.asyncio
async def test_send_c2c_local_image_uploads_then_sends_media(tmp_path: Path) -> None:
    image = tmp_path / "sticker.png"
    image.write_bytes(b"fake-png")
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/app/getAppAccessToken":
            return httpx.Response(200, json={"access_token": "token-1", "expires_in": "7200"})
        if request.url.path == "/v2/users/user-openid/files":
            payload = json.loads(request.content)
            assert payload["file_type"] == 1
            assert payload["file_data"]
            assert payload["srv_send_msg"] is False
            return httpx.Response(200, json={"file_info": "file-info-1"})
        if request.url.path == "/v2/users/user-openid/messages":
            assert json.loads(request.content) == {
                "msg_type": 7,
                "media": {"file_info": "file-info-1"},
                "content": "给你看这个",
                "is_wakeup": True,
            }
            return httpx.Response(200, json={"id": "sent-media-1"})
        return httpx.Response(404)

    client = QQOfficialClient(
        "app-id",
        "secret",
        transport=httpx.MockTransport(handler),
    )

    result = await client.send_c2c_local_image(
        "user-openid",
        image,
        content="给你看这个",
        is_wakeup=True,
    )

    assert result["id"] == "sent-media-1"
    assert [request.url.path for request in requests] == [
        "/app/getAppAccessToken",
        "/v2/users/user-openid/files",
        "/v2/users/user-openid/messages",
    ]

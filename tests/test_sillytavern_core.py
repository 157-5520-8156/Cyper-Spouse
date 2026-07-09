import httpx
import pytest

from companion_daemon.conversation import SillyTavernConversationCore
from companion_daemon.models import IncomingMessage, MoodState


@pytest.mark.asyncio
async def test_sillytavern_core_calls_plugin() -> None:
    seen = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/csrf-token":
            return httpx.Response(200, json={"token": "test-csrf"})
        seen["url"] = str(request.url)
        seen["json"] = request.content.decode()
        seen["csrf"] = request.headers.get("X-CSRF-Token")
        return httpx.Response(200, json={"text": "（手机震了一下）你好。"})

    core = SillyTavernConversationCore(
        "http://st.test",
        "你是沈知栀。",
        transport=httpx.MockTransport(handler),
    )

    text = await core.reply(
        IncomingMessage(platform="qq", platform_user_id="u", text="你好"),
        MoodState(),
        [],
        None,
    )

    assert text == "你好。"
    assert seen["url"] == "http://st.test/api/plugins/girl-agent-core/reply"
    assert seen["csrf"] == "test-csrf"

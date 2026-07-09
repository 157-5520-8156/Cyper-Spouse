import asyncio

import pytest

from companion_daemon.models import CompanionReply, IncomingMessage, MessageAttachment
from companion_daemon.qq_websocket import QQMessageCoalescer, _attachments_from_botpy, _clean_content
from companion_daemon.turn_taking import TurnTakingPolicy


def test_clean_content_strips_message_text() -> None:
    assert _clean_content("  知栀在吗  ") == "知栀在吗"
    assert _clean_content(None) == ""


def test_reply_msg_seq_is_positive() -> None:
    from companion_daemon.qq_websocket import _reply_msg_seq

    assert _reply_msg_seq() > 0


def test_attachments_from_botpy() -> None:
    class RawAttachment:
        content_type = "image/png"
        filename = "photo.png"
        url = "https://example.test/photo.png"
        size = 123
        width = 10
        height = 20

    attachments = _attachments_from_botpy([RawAttachment()])

    assert len(attachments) == 1
    assert attachments[0].kind == "image"
    assert attachments[0].url == "https://example.test/photo.png"


@pytest.mark.asyncio
async def test_coalescer_batches_rapid_messages() -> None:
    class FakeEngine:
        def __init__(self):
            self.seen_texts: list[str] = []

        async def handle_message(self, incoming: IncomingMessage) -> CompanionReply:
            self.seen_texts.append(incoming.text)
            return CompanionReply(canonical_user_id="geoff", mood="calm", text="收到")

    class FakeTarget:
        def __init__(self):
            self.replies: list[str] = []

        async def reply(self, **kwargs) -> None:
            self.replies.append(kwargs["content"])
            assert kwargs["msg_seq"] > 0

    engine = FakeEngine()
    first = FakeTarget()
    second = FakeTarget()
    coalescer = QQMessageCoalescer(
        engine,
        delay_seconds=0.01,
        turn_policy=TurnTakingPolicy(short_wait_seconds=0.01, long_wait_seconds=0.01),
    )

    await coalescer.add(
        "c2c:user",
        IncomingMessage(platform="qq", platform_user_id="user", text="第一句"),
        first,
    )
    await coalescer.add(
        "c2c:user",
        IncomingMessage(platform="qq", platform_user_id="user", text="第二句"),
        second,
    )
    await asyncio.sleep(0.03)

    assert engine.seen_texts == ["第一句\n第二句"]
    assert first.replies == []
    assert second.replies == ["收到"]


@pytest.mark.asyncio
async def test_coalescer_preserves_attachments() -> None:
    class FakeEngine:
        def __init__(self):
            self.attachments = []

        async def handle_message(self, incoming: IncomingMessage) -> CompanionReply:
            self.attachments = incoming.attachments
            return CompanionReply(canonical_user_id="geoff", mood="calm", text="看到了")

    class FakeTarget:
        async def reply(self, **kwargs) -> None:
            return None

    engine = FakeEngine()
    coalescer = QQMessageCoalescer(
        engine,
        delay_seconds=0.01,
        turn_policy=TurnTakingPolicy(short_wait_seconds=0.01, long_wait_seconds=0.01),
    )

    await coalescer.add(
        "c2c:user",
        IncomingMessage(
            platform="qq",
            platform_user_id="user",
            text="这张图",
            attachments=[MessageAttachment(kind="image", filename="photo.png")],
        ),
        FakeTarget(),
    )
    await asyncio.sleep(0.03)

    assert len(engine.attachments) == 1
    assert engine.attachments[0].filename == "photo.png"


@pytest.mark.asyncio
async def test_coalescer_sends_sticker_after_text_reply() -> None:
    class FakeEngine:
        async def handle_message(self, incoming: IncomingMessage) -> CompanionReply:
            return CompanionReply(
                canonical_user_id="geoff",
                mood="happy",
                text="好呀",
                sticker_path="assets/stickers/rin-happy.png",
            )

    class FakeTarget:
        def __init__(self):
            self.replies: list[str] = []

        async def reply(self, **kwargs) -> None:
            self.replies.append(kwargs["content"])

    sent: list[tuple[str, str]] = []

    async def send_sticker(incoming: IncomingMessage, reply: CompanionReply) -> None:
        sent.append((incoming.platform_user_id, reply.sticker_path or ""))

    target = FakeTarget()
    coalescer = QQMessageCoalescer(
        FakeEngine(),
        delay_seconds=0.01,
        turn_policy=TurnTakingPolicy(short_wait_seconds=0.01, long_wait_seconds=0.01),
        on_sticker=send_sticker,
    )

    await coalescer.add(
        "c2c:user",
        IncomingMessage(platform="qq", platform_user_id="user", text="哈哈"),
        target,
    )
    await asyncio.sleep(0.03)

    assert target.replies == ["好呀"]
    assert sent == [("user", "assets/stickers/rin-happy.png")]


@pytest.mark.asyncio
async def test_coalescer_sends_generated_image_after_text_reply() -> None:
    class FakeEngine:
        async def handle_message(self, incoming: IncomingMessage) -> CompanionReply:
            return CompanionReply(
                canonical_user_id="geoff",
                mood="happy",
                text="发你啦",
                image_path="assets/life/reply.png",
            )

    class FakeTarget:
        async def reply(self, **kwargs) -> None:
            return None

    sent: list[tuple[str, str]] = []

    async def send_image(incoming: IncomingMessage, reply: CompanionReply) -> None:
        sent.append((incoming.platform_user_id, reply.image_path or ""))

    coalescer = QQMessageCoalescer(
        FakeEngine(),
        delay_seconds=0.01,
        turn_policy=TurnTakingPolicy(short_wait_seconds=0.01, long_wait_seconds=0.01),
        on_image=send_image,
    )

    await coalescer.add(
        "c2c:user",
        IncomingMessage(platform="qq", platform_user_id="user", text="发张自拍"),
        FakeTarget(),
    )
    await asyncio.sleep(0.03)

    assert sent == [("user", "assets/life/reply.png")]

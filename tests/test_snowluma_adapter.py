from companion_daemon.onebot_adapter import (
    OneBotReplyTarget,
    event_token_is_valid,
    parse_onebot_event,
)


def test_parse_private_text_message() -> None:
    event = {
        "post_type": "message",
        "message_type": "private",
        "sub_type": "friend",
        "user_id": 123456,
        "message": [{"type": "text", "data": {"text": "你好呀"}}],
        "raw_message": "你好呀",
        "message_id": 789,
        "self_id": 654321,
        "time": 1700000000,
        "sender": {"nickname": "test"},
    }
    incoming = parse_onebot_event(event)
    assert incoming is not None
    assert incoming.platform == "qq"
    assert incoming.platform_user_id == "123456"
    assert incoming.text == "你好呀"
    assert incoming.message_id == "789"
    assert incoming.channel_id is None
    assert incoming.attachments == []


def test_parse_group_message() -> None:
    event = {
        "post_type": "message",
        "message_type": "group",
        "sub_type": "normal",
        "group_id": 999,
        "user_id": 123456,
        "message": [{"type": "text", "data": {"text": "群消息"}}],
        "raw_message": "群消息",
        "message_id": 789,
        "self_id": 654321,
        "time": 1700000000,
        "sender": {"nickname": "test"},
    }
    incoming = parse_onebot_event(event)
    assert incoming is not None
    assert incoming.channel_id == "999"
    assert incoming.text == "群消息"


def test_parse_image_message() -> None:
    event = {
        "post_type": "message",
        "message_type": "private",
        "user_id": 123456,
        "message": [
            {"type": "text", "data": {"text": "看这张"}},
            {"type": "image", "data": {"url": "http://example.com/img.jpg", "file": "abc.png"}},
        ],
        "raw_message": "看这张",
        "message_id": 789,
        "self_id": 654321,
        "time": 1700000000,
        "sender": {"nickname": "test"},
    }
    incoming = parse_onebot_event(event)
    assert incoming is not None
    assert incoming.text == "看这张"
    assert len(incoming.attachments) == 1
    assert incoming.attachments[0].kind == "image"
    assert incoming.attachments[0].url == "http://example.com/img.jpg"


def test_parse_image_only_message() -> None:
    event = {
        "post_type": "message",
        "message_type": "private",
        "user_id": 123456,
        "message": [{"type": "image", "data": {"url": "http://example.com/img.jpg"}}],
        "raw_message": "",
        "message_id": 789,
        "self_id": 654321,
        "time": 1700000000,
        "sender": {"nickname": "test"},
    }
    incoming = parse_onebot_event(event)
    assert incoming is not None
    assert incoming.text == ""
    assert len(incoming.attachments) == 1


def test_parse_interaction_evidence_segments() -> None:
    event = {
        "post_type": "message",
        "message_type": "private",
        "user_id": 123456,
        "message": [
            {"type": "reply", "data": {"id": "prior-7"}},
            {"type": "text", "data": {"text": "行吧"}},
            {"type": "face", "data": {"id": "178"}},
            {"type": "mface", "data": {"summary": "[无语]", "emoji_id": "sticker-3"}},
        ],
        "raw_message": "行吧",
        "message_id": 789,
    }

    incoming = parse_onebot_event(event)

    assert incoming is not None
    assert incoming.emoji == ["qq-face:178"]
    assert incoming.sticker_kind == "[无语]"
    assert incoming.reply_target == "prior-7"


def test_parse_non_message_event_returns_none() -> None:
    event = {"post_type": "notice", "notice_type": "group_increase"}
    assert parse_onebot_event(event) is None


def test_parse_string_message_format() -> None:
    event = {
        "post_type": "message",
        "message_type": "private",
        "user_id": 123456,
        "message": "纯文本消息",
        "raw_message": "纯文本消息",
        "message_id": 1,
        "self_id": 2,
        "time": 0,
        "sender": {},
    }
    incoming = parse_onebot_event(event)
    assert incoming is not None
    assert incoming.text == "纯文本消息"


def test_parse_empty_message_returns_none() -> None:
    event = {
        "post_type": "message",
        "message_type": "private",
        "user_id": 123456,
        "message": [],
        "raw_message": "",
        "message_id": 1,
        "self_id": 2,
        "time": 0,
        "sender": {},
    }
    assert parse_onebot_event(event) is None


def test_onebot_reply_target_fields() -> None:
    target = OneBotReplyTarget(
        api_url="http://127.0.0.1:5700",
        user_id=123456,
    )
    assert target.api_url == "http://127.0.0.1:5700"
    assert target.user_id == 123456
    assert target.group_id is None


def test_event_token_accepts_bearer_or_legacy_signature() -> None:
    assert event_token_is_valid("secret", authorization="Bearer secret", x_signature=None)
    assert event_token_is_valid("secret", authorization="secret", x_signature=None)
    assert event_token_is_valid("secret", authorization="Token secret", x_signature=None)
    assert event_token_is_valid("secret", authorization=None, x_signature="secret")
    assert not event_token_is_valid("secret", authorization="Bearer wrong", x_signature=None)

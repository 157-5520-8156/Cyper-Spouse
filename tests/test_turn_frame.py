from datetime import datetime, timezone

from companion_daemon.models import IncomingMessage
from companion_daemon.turn_frame import TurnFrameCompiler


def test_turn_frame_is_bounded_and_carries_provenance() -> None:
    compiler = TurnFrameCompiler()
    snapshot: dict[str, object] = {
        "clock": {"logical_at": "2026-07-12T09:00:00+00:00"},
        "recent_messages": [
            {
                "message_id": f"m:{index}",
                "user_id": "user:geoff",
                "direction": "in" if index % 2 else "out",
                "text": f"消息 {index}",
            }
            for index in range(20)
        ],
        "relationships": {"user:geoff": {"stage": "friend"}},
        "emotion_modulation": {"hurt": 37, "warmth": 15},
        "facts": {
            f"fact:{index}": {
                "subject": "user:geoff",
                "status": "current",
                "value": f"事实 {index}",
                "source": f"event:{index}",
            }
            for index in range(12)
        },
        "experiences": {
            f"experience:{index}": {"content": f"经历 {index}", "shared": False}
            for index in range(9)
        },
        "conversation_threads": {
            f"thread:{index}": {
                "status": "open",
                "user_id": "user:geoff",
                "question": f"问题 {index}",
            }
            for index in range(5)
        },
        "actions": {
            f"action:{index}": {"kind": "outgoing_message", "status": "scheduled"}
            for index in range(8)
        },
        "agenda": {},
        "controlled_transgressions": [],
    }
    message = IncomingMessage(
        platform="qq",
        platform_user_id="geoff",
        message_id="turn:1",
        text="我有点失望。",
        sent_at=datetime(2026, 7, 12, tzinfo=timezone.utc),
    )

    frame = compiler.compile(
        world_id="world:1",
        revision=42,
        state_hash="a" * 64,
        snapshot=snapshot,
        user_id="user:geoff",
        message=message,
    )

    assert len(frame.recent_messages) == 8
    assert len(frame.facts) == 6
    assert len(frame.experiences) == 4
    assert len(frame.open_threads) == 3
    assert len(frame.open_actions) == 4
    assert frame.dependency_tokens[0] == f"world:world:1:revision:42:state:{'a' * 64}"
    assert all(item["provenance"].startswith("event:") for item in frame.facts)
    delta = frame.prompt_delta()
    assert set(delta) == {
        "world_revision",
        "state_hash",
        "dependency_tokens",
        "open_threads",
        "open_actions",
        "capability",
    }
    assert "我有点失望" not in str(delta)
    assert len(str(delta)) < 3_000


def test_advisories_are_non_authoritative_and_bound_to_frame_evidence() -> None:
    compiler = TurnFrameCompiler()
    frame = compiler.compile(
        world_id="world:1",
        revision=7,
        state_hash="b" * 64,
        snapshot={
            "clock": {},
            "recent_messages": [],
            "relationships": {"user:geoff": {"stage": "acquaintance"}},
            "emotion_modulation": {"hurt": 50},
            "facts": {},
            "experiences": {},
            "conversation_threads": {
                "thread:1": {"status": "open", "user_id": "user:geoff", "question": "后来呢？"}
            },
            "actions": {},
            "agenda": {},
        },
        user_id="user:geoff",
        message=IncomingMessage(platform="qq", platform_user_id="geoff", text="嗯。"),
    )

    advisories = compiler.advisories(frame)

    assert {item.kind for item in advisories} >= {"affect", "relationship", "continuity"}
    assert all(item.source_event_ids for item in advisories)
    assert all(item.confidence <= 1.0 for item in advisories)


def test_turn_frame_does_not_repeat_the_current_input_in_recent_history() -> None:
    compiler = TurnFrameCompiler()
    message = IncomingMessage(
        platform="qq",
        platform_user_id="geoff",
        message_id="qq:geoff:current",
        text="我刚才有点失望。",
    )

    frame = compiler.compile(
        world_id="world:1",
        revision=8,
        state_hash="c" * 64,
        snapshot={
            "clock": {},
            "recent_messages": [
                {
                    "message_id": "qq:geoff:previous",
                    "user_id": "user:geoff",
                    "direction": "in",
                    "text": "上一次的话。",
                },
                {
                    "message_id": "qq:geoff:current",
                    "user_id": "user:geoff",
                    "direction": "in",
                    "text": message.text,
                },
            ],
            "relationships": {},
            "emotion_modulation": {},
            "facts": {},
            "experiences": {},
            "conversation_threads": {},
            "actions": {},
            "agenda": {},
        },
        user_id="user:geoff",
        message=message,
    )

    assert [item["text"] for item in frame.recent_messages] == ["上一次的话。"]

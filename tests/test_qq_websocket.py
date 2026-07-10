import asyncio
from datetime import datetime, timezone
import random

import pytest

from companion_daemon.models import CompanionReply, IncomingMessage, MessageAttachment, MoodState
from companion_daemon.qq_websocket import (
    CompanionQQClient,
    QQMessageCoalescer,
    _attachments_from_botpy,
    _clean_content,
    _afterthought_plans,
    classify_mid_reply_interruption,
    _send_reply_parts,
)
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


def test_duplicate_detection_drops_near_simultaneous_same_text_even_with_distinct_ids() -> None:
    client = object.__new__(CompanionQQClient)
    client._seen_message_ids = set()
    client._recent_text_keys = {}

    assert client._is_duplicate("msg-1", "user", "哈哈") is False
    assert client._is_duplicate("msg-2", "user", "哈哈") is True
    assert client._is_duplicate("msg-2", "user", "哈哈") is True


def test_duplicate_detection_falls_back_to_text_without_message_id() -> None:
    client = object.__new__(CompanionQQClient)
    client._seen_message_ids = set()
    client._recent_text_keys = {}

    assert client._is_duplicate(None, "user", "哈哈") is False
    assert client._is_duplicate(None, "user", "哈哈") is True


def test_classifies_mid_reply_interruption() -> None:
    assert classify_mid_reply_interruption("嗯嗯") == "backchannel"
    assert classify_mid_reply_interruption("对对对") == "backchannel"
    assert classify_mid_reply_interruption("等下我不是这个意思") == "takeover"
    assert classify_mid_reply_interruption("那你觉得我应该怎么办？") == "takeover"


def test_afterthought_plans_favor_open_story_and_respect_goodbyes() -> None:
    class FixedRandom:
        def uniform(self, low: float, high: float) -> float:
            return low

    plans = _afterthought_plans(
        "我今天其实遇到一件挺奇怪的事，后来越想越不对劲。事情不大，但我到现在还不知道要不要把它当回事。",
        FixedRandom(),
    )
    assert [plan.mode for plan in plans] == ["quick_continue", "topic_drift", "silence_react"]
    assert _afterthought_plans("晚安啦", FixedRandom()) == []


@pytest.mark.asyncio
async def test_afterthought_uses_original_reply_time_and_sends_at_most_once() -> None:
    class FakeStore:
        def resolve_user(self, platform: str, platform_user_id: str) -> str:
            return "geoff"

    class FakeEngine:
        def __init__(self):
            self.store = FakeStore()
            self.seen: list[tuple[datetime, str]] = []

        async def generate_afterthought(
            self,
            canonical_user_id: str,
            reply_sent_at: datetime,
            *,
            mode: str = "quick_continue",
        ) -> str:
            self.seen.append((reply_sent_at, mode))
            return f"{mode}:刚刚还想补一句。"

    class FakeTarget:
        def __init__(self):
            self.replies: list[str] = []

        async def reply(self, **kwargs) -> None:
            self.replies.append(kwargs["content"])

    class AlwaysSendRandom:
        def uniform(self, low: float, high: float) -> float:
            return low

        def random(self) -> float:
            return 0.0

    async def fake_sleep(seconds: float) -> None:
        return None

    engine = FakeEngine()
    target = FakeTarget()
    coalescer = QQMessageCoalescer(
        engine,
        delay_seconds=0.01,
        human_timing=True,
        sleep=fake_sleep,
        rng=AlwaysSendRandom(),
    )
    reply_sent_at = datetime(2026, 7, 10, 1, 2, 3, tzinfo=timezone.utc)

    coalescer._schedule_afterthought(
        "c2c:user",
        IncomingMessage(platform="qq", platform_user_id="user", text="刚才那事"),
        target,
        reply_sent_at,
    )
    tasks = coalescer._afterthought_tasks["c2c:user"]
    await asyncio.gather(*tasks)

    assert all(seen_at == reply_sent_at for seen_at, _ in engine.seen)
    assert [mode for _, mode in engine.seen] == ["quick_continue"]
    assert target.replies == ["quick_continue:刚刚还想补一句。"]


@pytest.mark.asyncio
async def test_afterthought_uses_outbox_delivery_confirmation() -> None:
    class FakeStore:
        def resolve_user(self, platform: str, platform_user_id: str) -> str:
            return "geoff"

    class FakeEngine:
        def __init__(self):
            self.store = FakeStore()
            self.queued: list[tuple[str, str, str]] = []
            self.confirmed: list[tuple[str, str, str, int | None]] = []
            self.failed: list[tuple[int | None, str]] = []

        async def generate_afterthought(
            self,
            canonical_user_id: str,
            reply_sent_at: datetime,
            *,
            mode: str = "quick_continue",
        ) -> str:
            return "补一句。"

        def queue_afterthought_delivery(
            self,
            canonical_user_id: str,
            platform: str,
            text: str,
        ) -> int:
            self.queued.append((canonical_user_id, platform, text))
            return 42

        def confirm_afterthought_delivery(
            self,
            canonical_user_id: str,
            platform: str,
            text: str,
            *,
            delivery_id: int | None = None,
        ) -> None:
            self.confirmed.append((canonical_user_id, platform, text, delivery_id))

        def fail_afterthought_delivery(self, delivery_id: int | None, reason: str) -> None:
            self.failed.append((delivery_id, reason))

    class FakeTarget:
        def __init__(self):
            self.replies: list[str] = []

        async def reply(self, **kwargs) -> None:
            self.replies.append(kwargs["content"])

    class AlwaysSendRandom:
        def uniform(self, low: float, high: float) -> float:
            return low

        def random(self) -> float:
            return 0.0

    async def fake_sleep(seconds: float) -> None:
        return None

    engine = FakeEngine()
    target = FakeTarget()
    coalescer = QQMessageCoalescer(
        engine,
        delay_seconds=0.01,
        human_timing=True,
        sleep=fake_sleep,
        rng=AlwaysSendRandom(),
    )

    coalescer._schedule_afterthought(
        "c2c:user",
        IncomingMessage(platform="qq", platform_user_id="user", text="刚才那事"),
        target,
        datetime(2026, 7, 10, 1, 2, 3, tzinfo=timezone.utc),
    )
    await asyncio.gather(*coalescer._afterthought_tasks["c2c:user"])

    assert target.replies == ["补一句。"]
    assert engine.queued == [("geoff", "qq", "补一句。")]
    assert engine.confirmed == [("geoff", "qq", "补一句。", 42)]
    assert engine.failed == []


@pytest.mark.asyncio
async def test_new_user_message_cancels_pending_afterthoughts() -> None:
    class FakeStore:
        def resolve_user(self, platform: str, platform_user_id: str) -> str:
            return "geoff"

    class FakeEngine:
        def __init__(self):
            self.store = FakeStore()
            self.afterthought_called = False

        async def generate_afterthought(
            self,
            canonical_user_id: str,
            reply_sent_at: datetime,
            *,
            mode: str = "quick_continue",
        ) -> str:
            self.afterthought_called = True
            return "不该发。"

        async def handle_message(self, incoming: IncomingMessage, **kwargs) -> CompanionReply:
            return CompanionReply(canonical_user_id="geoff", mood="calm", text="新回复。")

    class FakeTarget:
        def __init__(self):
            self.replies: list[str] = []

        async def reply(self, **kwargs) -> None:
            self.replies.append(kwargs["content"])

    class SlowRandom:
        def uniform(self, low: float, high: float) -> float:
            return high

        def random(self) -> float:
            return 0.0

    async def fake_sleep(seconds: float) -> None:
        if seconds > 10:
            await asyncio.Event().wait()
        return None

    engine = FakeEngine()
    target = FakeTarget()
    coalescer = QQMessageCoalescer(
        engine,
        delay_seconds=0.01,
        human_timing=True,
        sleep=fake_sleep,
        rng=SlowRandom(),
    )
    coalescer._schedule_afterthought(
        "c2c:user",
        IncomingMessage(platform="qq", platform_user_id="user", text="刚才那事"),
        target,
        datetime(2026, 7, 10, 1, 2, 3, tzinfo=timezone.utc),
    )

    await coalescer.add(
        "c2c:user",
        IncomingMessage(platform="qq", platform_user_id="user", text="我回来了"),
        target,
    )
    await asyncio.sleep(0.03)

    assert engine.afterthought_called is False
    assert target.replies == ["新回复。"]


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
async def test_coalescer_fires_emoji_reaction_before_reply_text() -> None:
    order: list[str] = []

    class FakeEngine:
        async def handle_message(self, incoming: IncomingMessage, **kwargs) -> CompanionReply:
            return CompanionReply(
                canonical_user_id="geoff",
                mood="happy",
                text="哈哈哈被你笑到",
                suggested_reaction="haha",
            )

    class FakeTarget:
        async def reply(self, **kwargs) -> None:
            order.append("text")

    async def send_reaction(incoming: IncomingMessage, reply: CompanionReply) -> None:
        order.append(f"reaction:{reply.suggested_reaction}:{incoming.message_id}")

    coalescer = QQMessageCoalescer(
        FakeEngine(),
        delay_seconds=0.01,
        turn_policy=TurnTakingPolicy(short_wait_seconds=0.01, long_wait_seconds=0.01),
        on_reaction=send_reaction,
    )

    await coalescer.add(
        "c2c:user",
        IncomingMessage(platform="qq", platform_user_id="user", text="超好笑的事", message_id="777"),
        FakeTarget(),
    )
    await asyncio.sleep(0.03)

    assert order == ["reaction:haha:777", "text"]


@pytest.mark.asyncio
async def test_coalescer_sends_reply_parts_in_order() -> None:
    class FakeEngine:
        async def handle_message(self, incoming: IncomingMessage) -> CompanionReply:
            return CompanionReply(
                canonical_user_id="geoff",
                mood="happy",
                text="我在。刚刚想到你。",
                text_parts=["我在。", "刚刚想到你。"],
            )

    class FakeTarget:
        def __init__(self):
            self.replies: list[str] = []

        async def reply(self, **kwargs) -> None:
            self.replies.append(kwargs["content"])

    target = FakeTarget()
    coalescer = QQMessageCoalescer(
        FakeEngine(),
        delay_seconds=0.01,
        turn_policy=TurnTakingPolicy(short_wait_seconds=0.01, long_wait_seconds=0.01),
    )

    await coalescer.add(
        "c2c:user",
        IncomingMessage(platform="qq", platform_user_id="user", text="在吗"),
        target,
    )
    await asyncio.sleep(1.0)

    assert target.replies == ["我在。", "刚刚想到你。"]


@pytest.mark.asyncio
async def test_backchannel_during_split_reply_does_not_interrupt() -> None:
    class FakeEngine:
        def __init__(self):
            self.recorded_without_reply: list[str] = []

        async def handle_message(self, incoming: IncomingMessage, **kwargs) -> CompanionReply | None:
            if kwargs.get("skip_reply"):
                self.recorded_without_reply.append(incoming.text)
                return None
            return CompanionReply(
                canonical_user_id="geoff",
                mood="happy",
                text="第一句。第二句。",
                text_parts=["第一句。", "第二句。"],
            )

    class FakeTarget:
        def __init__(self):
            self.replies: list[str] = []

        async def reply(self, **kwargs) -> None:
            self.replies.append(kwargs["content"])

    engine = FakeEngine()
    target = FakeTarget()
    coalescer = QQMessageCoalescer(
        engine,
        delay_seconds=0.01,
        turn_policy=TurnTakingPolicy(short_wait_seconds=0.01, long_wait_seconds=0.01),
        human_timing=True,
        rng=random.Random(1),
    )
    inserted = False

    async def fake_sleep(seconds: float) -> None:
        nonlocal inserted
        if target.replies == ["第一句。"] and not inserted:
            inserted = True
            await coalescer.add(
                "c2c:user",
                IncomingMessage(platform="qq", platform_user_id="user", text="嗯嗯"),
                target,
            )

    coalescer.sleep = fake_sleep
    await coalescer.add(
        "c2c:user",
        IncomingMessage(platform="qq", platform_user_id="user", text="你继续说"),
        target,
    )
    await asyncio.sleep(0.03)

    assert target.replies == ["第一句。", "第二句。"]
    assert engine.recorded_without_reply == ["嗯嗯"]


@pytest.mark.asyncio
async def test_takeover_during_split_reply_stops_remaining_parts_and_replies_again() -> None:
    class FakeEngine:
        def __init__(self):
            self.seen_texts: list[str] = []

        async def handle_message(self, incoming: IncomingMessage, **kwargs) -> CompanionReply:
            self.seen_texts.append(incoming.text)
            if len(self.seen_texts) == 1:
                return CompanionReply(
                    canonical_user_id="geoff",
                    mood="happy",
                    text="第一句。第二句。第三句。",
                    text_parts=["第一句。", "第二句。", "第三句。"],
                )
            return CompanionReply(canonical_user_id="geoff", mood="calm", text="哦，那我换个说法。")

    class FakeTarget:
        def __init__(self):
            self.replies: list[str] = []

        async def reply(self, **kwargs) -> None:
            self.replies.append(kwargs["content"])

    engine = FakeEngine()
    target = FakeTarget()
    coalescer = QQMessageCoalescer(
        engine,
        delay_seconds=0.01,
        turn_policy=TurnTakingPolicy(short_wait_seconds=0.01, long_wait_seconds=0.01),
        human_timing=True,
        rng=random.Random(1),
    )
    inserted = False

    async def fake_sleep(seconds: float) -> None:
        nonlocal inserted
        if target.replies == ["第一句。"] and not inserted:
            inserted = True
            await coalescer.add(
                "c2c:user",
                IncomingMessage(platform="qq", platform_user_id="user", text="等下我不是这个意思"),
                target,
            )

    coalescer.sleep = fake_sleep
    await coalescer.add(
        "c2c:user",
        IncomingMessage(platform="qq", platform_user_id="user", text="你继续说"),
        target,
    )
    await asyncio.sleep(0.05)

    assert engine.seen_texts == ["你继续说", "等下我不是这个意思"]
    assert target.replies == ["第一句。", "哦，那我换个说法。"]


@pytest.mark.asyncio
async def test_coalescer_human_timing_reads_current_mood_state() -> None:
    from companion_daemon.models import MoodState

    class FakeStore:
        def get_mood_state(self, canonical_user_id: str) -> MoodState:
            assert canonical_user_id == "geoff"
            return MoodState(emotion_vector={"anger": 90, "sadness": 70, "fear": 50})

    class FakeEngine:
        def __init__(self):
            self.store = FakeStore()

        async def handle_message(self, incoming: IncomingMessage) -> CompanionReply:
            return CompanionReply(canonical_user_id="geoff", mood="hurt", text="嗯。")

    class FakeTarget:
        async def reply(self, **kwargs) -> None:
            return None

    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    coalescer = QQMessageCoalescer(
        FakeEngine(),
        delay_seconds=0.01,
        turn_policy=TurnTakingPolicy(short_wait_seconds=0.01, long_wait_seconds=0.01),
        human_timing=True,
        sleep=fake_sleep,
        rng=random.Random(1),
    )

    await coalescer.add(
        "c2c:user",
        IncomingMessage(platform="qq", platform_user_id="user", text="随便你"),
        FakeTarget(),
    )
    await asyncio.sleep(0.01)

    assert any(seconds > 10 for seconds in sleeps)


@pytest.mark.asyncio
async def test_coalescer_can_defer_long_story_then_reply() -> None:
    class DeferRandom(random.Random):
        def random(self) -> float:
            return 0.0

        def uniform(self, a: float, b: float) -> float:
            return a

    class FakeStore:
        def resolve_user(self, platform: str, platform_user_id: str) -> str:
            return "geoff"

        def get_mood_state(self, canonical_user_id: str):
            from companion_daemon.models import MoodState

            return MoodState()

        def recent_messages(self, canonical_user_id: str, limit: int = 6):
            return []

    class FakeEngine:
        def __init__(self):
            self.store = FakeStore()
            self.seen_texts: list[str] = []
            self.context_hints: list[str | None] = []

        async def handle_message(self, incoming: IncomingMessage, **kwargs) -> CompanionReply:
            self.seen_texts.append(incoming.text)
            self.context_hints.append(kwargs.get("context_hint"))
            return CompanionReply(canonical_user_id="geoff", mood="calm", text="刚看到。")

    class FakeTarget:
        def __init__(self):
            self.replies: list[str] = []

        async def reply(self, **kwargs) -> None:
            self.replies.append(kwargs["content"])

    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    engine = FakeEngine()
    target = FakeTarget()
    coalescer = QQMessageCoalescer(
        engine,
        delay_seconds=0.01,
        turn_policy=TurnTakingPolicy(short_wait_seconds=0.01, long_wait_seconds=0.01),
        enable_reply_decision=True,
        sleep=fake_sleep,
        rng=DeferRandom(),
    )

    opener = "我跟你说"
    await coalescer.add(
        "c2c:user",
        IncomingMessage(platform="qq", platform_user_id="user", text=opener),
        target,
    )
    await asyncio.sleep(0.01)

    assert engine.seen_texts == [opener]
    assert engine.context_hints == [None]
    assert target.replies == ["刚看到。"]
    assert any(seconds >= 300 for seconds in sleeps)


def test_qq_emoji_id_maps_known_reactions() -> None:
    from companion_daemon.emotion_reactions import qq_emoji_id

    assert qq_emoji_id("haha") == "128514"
    assert qq_emoji_id("heart") == "10084"
    assert qq_emoji_id(None) is None
    assert qq_emoji_id("unknown") is None


@pytest.mark.asyncio
async def test_reply_decision_defer_persists_a_read_later_task() -> None:
    class DeferRandom(random.Random):
        def random(self) -> float:
            return 0.0

        def uniform(self, a: float, b: float) -> float:
            return a

    class FakeStore:
        def resolve_user(self, platform: str, platform_user_id: str) -> str:
            return "geoff"

        def get_mood_state(self, canonical_user_id: str):
            from companion_daemon.models import MoodState

            return MoodState()

        def recent_messages(self, canonical_user_id: str, limit: int = 6):
            return []

    class FakeEngine:
        def __init__(self):
            self.store = FakeStore()
            self.read_later_reasons: list[str] = []
            self.deferred_reasons: list[str] = []

        def create_read_later_task(self, message: IncomingMessage, *, defer_minutes: float, reason: str) -> int:
            self.read_later_reasons.append(reason)
            return 11

        def create_deferred_reply_task(self, message: IncomingMessage, *, defer_minutes: float, reason: str) -> int:
            self.deferred_reasons.append(reason)
            return 12

        async def handle_message(self, incoming: IncomingMessage, **kwargs) -> CompanionReply:
            return CompanionReply(canonical_user_id="geoff", mood="calm", text="刚看到。")

    class FakeTarget:
        async def reply(self, **kwargs) -> None:
            return None

    async def fake_sleep(seconds: float) -> None:
        return None

    engine = FakeEngine()
    coalescer = QQMessageCoalescer(
        engine,
        delay_seconds=0.01,
        turn_policy=TurnTakingPolicy(short_wait_seconds=0.01, long_wait_seconds=0.01),
        enable_reply_decision=True,
        sleep=fake_sleep,
        rng=DeferRandom(),
    )

    long_story = (
        "今天在食堂排队的时候，前面有个人点了三份一样的菜，然后又全部退掉重新点了一遍，"
        "我就这样看着队伍慢慢变长，最后干脆换了一个窗口，结果那边的阿姨手更慢。"
    )
    await coalescer.add(
        "c2c:user",
        IncomingMessage(platform="qq", platform_user_id="user", text=long_story),
        FakeTarget(),
    )
    await asyncio.sleep(0.01)

    # The reply-decision defer happens after she already read the message, so it
    # must persist through the read-but-sidetracked entrance, not an unread defer.
    assert engine.read_later_reasons
    assert engine.deferred_reasons == []


@pytest.mark.asyncio
async def test_upset_state_ghosts_through_read_later_and_uses_ghost_context_hint() -> None:
    class GhostRandom(random.Random):
        def random(self) -> float:
            return 0.0

        def uniform(self, a: float, b: float) -> float:
            return a

    class FakeStore:
        def resolve_user(self, platform: str, platform_user_id: str) -> str:
            return "geoff"

        def get_mood_state(self, canonical_user_id: str):
            return MoodState(
                mood="hurt",
                emotion_vector={"anger": 75, "sadness": 55, "joy": 5, "trust": 10},
            )

        def recent_messages(self, canonical_user_id: str, limit: int = 6):
            return []

    class FakeEngine:
        def __init__(self):
            self.store = FakeStore()
            self.read_later_reasons: list[str] = []
            self.context_hints: list[str | None] = []

        def create_read_later_task(self, message: IncomingMessage, *, defer_minutes: float, reason: str) -> int:
            self.read_later_reasons.append(reason)
            return 9

        def create_deferred_reply_task(self, message: IncomingMessage, *, defer_minutes: float, reason: str) -> int:
            raise AssertionError("ghost must not use unread defer")

        async def handle_message(self, incoming: IncomingMessage, **kwargs) -> CompanionReply:
            self.context_hints.append(kwargs.get("context_hint"))
            return CompanionReply(canonical_user_id="geoff", mood="hurt", text="嗯。")

        def complete_deferred_reply_task(self, task_id: int | None) -> None:
            return None

    async def fake_sleep(seconds: float) -> None:
        return None

    engine = FakeEngine()
    coalescer = QQMessageCoalescer(
        engine,
        delay_seconds=0.01,
        turn_policy=TurnTakingPolicy(short_wait_seconds=0.01, long_wait_seconds=0.01),
        enable_reply_decision=True,
        sleep=fake_sleep,
        rng=GhostRandom(),
    )

    await coalescer.add(
        "c2c:user",
        IncomingMessage(platform="qq", platform_user_id="user", text="我今天去买了那个键盘"),
        type("T", (), {"reply": staticmethod(lambda **k: None)})(),
    )
    # Let the in-memory ghost timer fire immediately (defer_minutes * 60 with fake_sleep).
    await asyncio.sleep(0.02)

    assert engine.read_later_reasons == ["emotional_ghost"]
    assert "故意没有马上回" in (engine.context_hints[0] or "")


@pytest.mark.asyncio
async def test_send_reply_parts_uses_human_delay_between_parts() -> None:
    class FakeTarget:
        def __init__(self):
            self.replies: list[str] = []

        async def reply(self, **kwargs) -> None:
            self.replies.append(kwargs["content"])

    delays: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        delays.append(seconds)

    target = FakeTarget()
    await _send_reply_parts(
        target,
        ["嗯。", "刚刚其实还想补一句。"],
        sleep=fake_sleep,
        rng=random.Random(1),
        human_timing=True,
    )

    assert target.replies == ["嗯。", "刚刚其实还想补一句。"]
    assert len(delays) == 1
    assert delays[0] >= 0.9


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

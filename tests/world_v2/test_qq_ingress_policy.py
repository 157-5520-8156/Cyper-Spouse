from __future__ import annotations

from datetime import UTC, datetime, timedelta
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from companion_daemon.world_v2.qq_ingress_policy import (
    MemoryQQIngressStore,
    QQIngressFragment,
    QQIngressPolicyCatalog,
    SQLiteQQIngressStore,
    normalize_onebot_qq_ingress,
)
from companion_daemon.world_v2.qq_c2c_host import QQC2CHost


NOW = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)


def _text(source: str, text: str, *, observed_at: datetime = NOW) -> QQIngressFragment:
    return QQIngressFragment(
        source_event_id=source,
        recipient_id="10001",
        observed_at=observed_at,
        content_shape="text",
        text=text,
    )


def test_qq_ingress_matrix_is_complete_machine_readable_and_bounded() -> None:
    catalog = QQIngressPolicyCatalog()
    manifest = catalog.manifest()

    assert manifest["version"] == "world-v2-qq-ingress-matrix.1"
    assert len(manifest["rows"]) == 30  # type: ignore[arg-type]
    assert len(catalog.digest) == 64
    assert {
        row["batch_mode"] for row in manifest["rows"]  # type: ignore[union-attr]
    } == {"ordered_multimodal", "metadata_only"}
    assert all(400 <= row["window_ms"] <= 800 for row in manifest["rows"])  # type: ignore[union-attr]


def test_onebot_normalizer_retains_multimodal_quote_and_control_as_opaque_refs() -> None:
    mixed = normalize_onebot_qq_ingress(
        {
            "post_type": "message",
            "message_type": "private",
            "user_id": 10001,
            "message_id": 31,
            "time": NOW.timestamp(),
            "message": [
                {"type": "reply", "data": {"id": 29}},
                {"type": "text", "data": {"text": "看这个"}},
                {"type": "image", "data": {"url": "https://private.invalid/a.jpg"}},
            ],
        }
    )
    typing = normalize_onebot_qq_ingress(
        {
            "post_type": "notice",
            "notice_type": "input_status",
            "user_id": 10001,
            "event_id": "typing-1",
            "status": "start",
            "time": NOW.timestamp(),
        }
    )

    assert mixed is not None
    assert mixed.content_shape == "mixed"
    assert mixed.text == "看这个"
    assert mixed.reply_ref == "qq-message:29"
    assert mixed.attachment_refs[0].startswith("qq-attachment:image:sha256:")
    assert "private.invalid" not in json.dumps(mixed.canonical_payload())
    assert typing is not None
    assert typing.content_shape == "control"
    assert typing.control_kind == "typing_started"


def test_napcat_notify_input_status_normalizes_as_typing_control() -> None:
    """NapCat reports peer typing as notice.notify.input_status without a message id."""

    typing = normalize_onebot_qq_ingress(
        {
            "post_type": "notice",
            "notice_type": "notify",
            "sub_type": "input_status",
            "user_id": 10001,
            "status_text": "对方正在输入...",
            "event_type": 1,
            "time": NOW.timestamp(),
        }
    )
    assert typing is not None
    assert typing.content_shape == "control"
    assert typing.control_kind == "typing_started"
    assert typing.source_event_id.startswith("qq-input-status:")

    retry = normalize_onebot_qq_ingress(
        {
            "post_type": "notice",
            "notice_type": "notify",
            "sub_type": "input_status",
            "user_id": 10001,
            "status_text": "对方正在输入...",
            "event_type": 1,
            "time": NOW.timestamp(),
        }
    )
    assert retry is not None and retry.source_event_id == typing.source_event_id

    group = normalize_onebot_qq_ingress(
        {
            "post_type": "notice",
            "notice_type": "notify",
            "sub_type": "input_status",
            "user_id": 10001,
            "group_id": 777,
            "event_type": 1,
            "time": NOW.timestamp(),
        }
    )
    assert group is None


def test_onebot_retry_without_provider_timestamp_keeps_content_identity() -> None:
    event = {
        "post_type": "message",
        "message_type": "private",
        "user_id": 10001,
        "message_id": 77,
        "raw_message": "重投",
    }
    first = normalize_onebot_qq_ingress(event)
    second = normalize_onebot_qq_ingress(event)
    assert first is not None and second is not None
    assert first.payload_hash == second.payload_hash


@pytest.mark.parametrize("store_kind", ["memory", "sqlite"])
def test_duplicate_conflict_and_out_of_order_coalescing_are_deterministic(
    store_kind: str, tmp_path: Path
) -> None:
    store = (
        MemoryQQIngressStore()
        if store_kind == "memory"
        else SQLiteQQIngressStore(tmp_path / "ingress.sqlite")
    )
    try:
        later = _text("message:b", "第二句", observed_at=NOW + timedelta(milliseconds=100))
        earlier = _text("message:a", "第一句")
        first = store.submit(later, received_at=NOW)
        duplicate = store.submit(later, received_at=NOW + timedelta(milliseconds=10))
        store.submit(earlier, received_at=NOW + timedelta(milliseconds=20))

        assert duplicate.due_at == first.due_at
        with pytest.raises(ValueError, match="conflicts"):
            store.submit(_text("message:b", "被篡改"), received_at=NOW)
        assert store.claim_due(now=first.due_at - timedelta(microseconds=1)) is None
        batch = store.claim_due(now=first.due_at)
        assert batch is not None
        assert batch.source_event_ids == ("message:a", "message:b")
        assert batch.text == "第一句\n第二句"
        assert batch.metadata["source_event_ids"] == ["message:a", "message:b"]
        assert batch.metadata["ordered_fragment_count"] == 2
    finally:
        store.close()


def test_memory_and_sqlite_emit_byte_equivalent_batches(tmp_path: Path) -> None:
    memory = MemoryQQIngressStore()
    sqlite = SQLiteQQIngressStore(tmp_path / "equivalent.sqlite")
    fragments = (
        _text("message:2", "后", observed_at=NOW + timedelta(milliseconds=30)),
        _text("message:1", "先"),
        QQIngressFragment(
            source_event_id="message:3",
            recipient_id="10001",
            observed_at=NOW + timedelta(milliseconds=40),
            content_shape="reaction",
            reaction_refs=("qq-face:14",),
        ),
    )
    try:
        for offset, fragment in enumerate(fragments):
            received = NOW + timedelta(milliseconds=offset * 10)
            memory.submit(fragment, received_at=received)
            sqlite.submit(fragment, received_at=received)
        left = memory.claim_due(now=NOW + timedelta(seconds=1))
        right = sqlite.claim_due(now=NOW + timedelta(seconds=1))
        assert left == right
    finally:
        memory.close()
        sqlite.close()


@pytest.mark.parametrize("store_kind", ["memory", "sqlite"])
def test_control_signal_never_triggers_alone_but_joins_nearby_content(
    store_kind: str, tmp_path: Path
) -> None:
    store = (
        MemoryQQIngressStore()
        if store_kind == "memory"
        else SQLiteQQIngressStore(tmp_path / "control.sqlite")
    )
    control = QQIngressFragment(
        source_event_id="typing:1",
        recipient_id="10001",
        observed_at=NOW,
        content_shape="control",
        control_kind="typing_started",
    )
    try:
        store.submit(control, received_at=NOW)
        assert store.claim_due(now=NOW + timedelta(seconds=1)) is None
        text = _text("message:after-typing", "说完了", observed_at=NOW + timedelta(milliseconds=500))
        submitted = store.submit(text, received_at=NOW + timedelta(milliseconds=500))
        batch = store.claim_due(now=submitted.due_at)
        assert batch is not None
        assert batch.source_event_ids == ("typing:1", "message:after-typing")
        assert batch.metadata["control_events"] == [
            {"kind": "typing_started", "source_event_id": "typing:1"}
        ]
    finally:
        store.close()


@pytest.mark.parametrize("store_kind", ["memory", "sqlite"])
def test_orphan_control_expires_as_adapter_observed_only(
    store_kind: str, tmp_path: Path
) -> None:
    store = (
        MemoryQQIngressStore()
        if store_kind == "memory"
        else SQLiteQQIngressStore(tmp_path / "control-expiry.sqlite")
    )
    control = QQIngressFragment(
        source_event_id="typing:orphan",
        recipient_id="10001",
        observed_at=NOW,
        content_shape="control",
        control_kind="typing_stopped",
    )
    try:
        store.submit(control, received_at=NOW)
        assert store.claim_due(now=NOW + timedelta(seconds=31)) is None
        result = store.submission("typing:orphan")
        assert result is not None
        assert (result.state, result.outcome_status, result.action_id) == (
            "committed",
            "observed_only",
            None,
        )
    finally:
        store.close()


@pytest.mark.parametrize("store_kind", ["memory", "sqlite"])
def test_late_claim_joins_fragments_beyond_anchor_window_into_one_session_batch(
    store_kind: str, tmp_path: Path
) -> None:
    """A claim delayed by a slow earlier turn absorbs the whole ongoing burst."""

    store = (
        MemoryQQIngressStore()
        if store_kind == "memory"
        else SQLiteQQIngressStore(tmp_path / "session.sqlite")
    )
    try:
        store.submit(_text("message:s1", "看看你在干啥"), received_at=NOW)
        store.submit(
            _text("message:s2", "看看你在干啥👀", observed_at=NOW + timedelta(seconds=5)),
            received_at=NOW + timedelta(seconds=5),
        )
        store.submit(
            _text("message:s3", "在吗", observed_at=NOW + timedelta(seconds=40)),
            received_at=NOW + timedelta(seconds=40),
        )
        batch = store.claim_due(now=NOW + timedelta(seconds=45))
        assert batch is not None
        assert batch.source_event_ids == ("message:s1", "message:s2", "message:s3")
        assert batch.text == "看看你在干啥\n看看你在干啥👀\n在吗"
    finally:
        store.close()


@pytest.mark.parametrize("store_kind", ["memory", "sqlite"])
def test_batch_bounds_always_retain_content_anchor_and_split_oversized_join(
    store_kind: str, tmp_path: Path
) -> None:
    store = (
        MemoryQQIngressStore()
        if store_kind == "memory"
        else SQLiteQQIngressStore(tmp_path / "bounds.sqlite")
    )
    try:
        for index in range(8):
            store.submit(
                QQIngressFragment(
                    source_event_id=f"typing:{index}",
                    recipient_id="10001",
                    observed_at=NOW + timedelta(milliseconds=index),
                    content_shape="control",
                    control_kind="typing_started",
                ),
                received_at=NOW + timedelta(milliseconds=index),
            )
        first_text = _text("message:long-1", "甲" * 7_000, observed_at=NOW + timedelta(milliseconds=20))
        second_text = _text("message:long-2", "乙" * 7_000, observed_at=NOW + timedelta(milliseconds=30))
        first_due = store.submit(
            first_text, received_at=NOW + timedelta(milliseconds=20)
        ).due_at
        store.submit(second_text, received_at=NOW + timedelta(milliseconds=30))
        first_batch = store.claim_due(now=first_due)
        assert first_batch is not None
        assert "message:long-1" in first_batch.source_event_ids
        assert len(first_batch.source_event_ids) <= 8
        assert len(first_batch.text or "") <= 12_000
        assert "message:long-2" not in first_batch.source_event_ids
        store.complete(
            batch_id=first_batch.batch_id,
            outcome_status="observed_only",
            action_id=None,
        )
        second_batch = store.claim_due(now=NOW + timedelta(seconds=1))
        assert second_batch is not None
        assert "message:long-2" in second_batch.source_event_ids
        assert second_batch.text == "乙" * 7_000
    finally:
        store.close()


def test_sqlite_restart_recovers_same_claim_and_completion(tmp_path: Path) -> None:
    path = tmp_path / "restart.sqlite"
    first = SQLiteQQIngressStore(path)
    submitted = first.submit(_text("message:restart", "重启"), received_at=NOW)
    claimed = first.claim_due(now=submitted.due_at)
    assert claimed is not None
    first.close()

    restarted = SQLiteQQIngressStore(path)
    try:
        recovered = restarted.claim_due(now=NOW + timedelta(days=1))
        assert recovered == claimed
        restarted.complete(
            batch_id=recovered.batch_id,
            outcome_status="action_authorized",
            action_id="action:1",
        )
        result = restarted.submission("message:restart")
        assert result is not None
        assert (result.state, result.outcome_status, result.action_id) == (
            "committed",
            "action_authorized",
            "action:1",
        )
        restarted.complete(
            batch_id=recovered.batch_id,
            outcome_status="action_authorized",
            action_id="action:1",
        )
        with pytest.raises(ValueError, match="immutable"):
            restarted.complete(
                batch_id=recovered.batch_id,
                outcome_status="action_authorized",
                action_id="action:2",
            )
    finally:
        restarted.close()


def test_two_sqlite_process_views_join_duplicate_and_claim(tmp_path: Path) -> None:
    path = tmp_path / "multi-process.sqlite"
    left = SQLiteQQIngressStore(path)
    right = SQLiteQQIngressStore(path)
    try:
        first = left.submit(_text("message:shared", "同一条"), received_at=NOW)
        duplicate = right.submit(
            _text("message:shared", "同一条"), received_at=NOW + timedelta(seconds=9)
        )
        assert duplicate.due_at == first.due_at
        claimed_left = left.claim_due(now=first.due_at)
        claimed_right = right.claim_due(now=first.due_at)
        assert claimed_left == claimed_right
        assert claimed_left is not None
        right.complete(
            batch_id=claimed_left.batch_id,
            outcome_status="observed_only",
            action_id=None,
        )
        left.complete(
            batch_id=claimed_left.batch_id,
            outcome_status="observed_only",
            action_id=None,
        )
    finally:
        left.close()
        right.close()


class _WorldHost:
    def __init__(self) -> None:
        self.inbounds = []
        self.closed = False

    async def inbound(self, inbound):  # type: ignore[no-untyped-def]
        self.inbounds.append(inbound)
        return SimpleNamespace(
            status="observed_only", authorized_action_ids=(), scheduled_action_ids=()
        )

    async def drain_action(self, _action_id: str):  # type: ignore[no-untyped-def]
        return None

    def close(self) -> None:
        self.closed = True


class _FailOnceWorldHost(_WorldHost):
    async def inbound(self, inbound):  # type: ignore[no-untyped-def]
        self.inbounds.append(inbound)
        if len(self.inbounds) == 1:
            raise RuntimeError("injected turn failure")
        return SimpleNamespace(
            status="observed_only", authorized_action_ids=(), scheduled_action_ids=()
        )


@pytest.mark.asyncio
async def test_failed_claim_retry_keeps_identical_observation_metadata() -> None:
    clock = {"now": NOW + timedelta(seconds=1)}
    store = MemoryQQIngressStore()
    store.submit(_text("message:retry", "重试也必须是同一条"), received_at=NOW)
    world = _FailOnceWorldHost()
    host = QQC2CHost(
        host=world,  # type: ignore[arg-type]
        recipient_id="10001", canonical_user_id="geoff", ingress_store=store,
        ingress_now=lambda: clock["now"],
    )
    try:
        with pytest.raises(RuntimeError, match="injected"):
            await host.drain_ingress_once()
        clock["now"] += timedelta(hours=3)
        recovered = await host.drain_ingress_once()
        assert recovered is not None
    finally:
        await host.aclose()

    assert len(world.inbounds) == 2
    assert world.inbounds[0].coalescing_metadata == world.inbounds[1].coalescing_metadata
    assert world.inbounds[0].coalescing_metadata["processing_started_at"] == (
        NOW + timedelta(seconds=1)
    ).isoformat()


@pytest.mark.asyncio
async def test_host_concurrent_fragments_join_one_world_observation() -> None:
    clock = {"now": NOW}
    release = asyncio.Event()

    async def controlled_sleep(delay: float) -> None:
        clock["now"] += timedelta(seconds=delay)
        if delay > 0 and not release.is_set():
            await release.wait()
        else:
            await asyncio.sleep(0)

    world = _WorldHost()
    store = MemoryQQIngressStore()
    host = QQC2CHost(
        host=world,  # type: ignore[arg-type]
        recipient_id="10001",
        canonical_user_id="geoff",
        ingress_store=store,
        ingress_now=lambda: clock["now"],
        ingress_sleep=controlled_sleep,
    )
    first = asyncio.create_task(host.inbound_fragment(_text("message:1", "先说一半")))
    await asyncio.sleep(0)
    second = asyncio.create_task(
        host.inbound_fragment(
            _text("message:2", "再补完", observed_at=NOW + timedelta(milliseconds=100))
        )
    )
    await asyncio.sleep(0)
    clock["now"] = NOW + timedelta(seconds=1)
    release.set()
    left, right = await asyncio.gather(first, second)

    assert left == right
    assert len(world.inbounds) == 1
    assert world.inbounds[0].text == "先说一半\n再补完"
    assert world.inbounds[0].coalescing_metadata["source_event_ids"] == [
        "message:1",
        "message:2",
    ]
    await host.aclose()


@pytest.mark.asyncio
async def test_burst_bubbles_within_one_exchange_join_one_turn_via_rhythm_hold() -> None:
    """Consecutive bubbles seconds apart become one turn, not one reply each."""

    clock = {"now": NOW}

    async def advancing_sleep(delay: float) -> None:
        # Small capped steps keep the two ingress tasks interleaving the way
        # real wall-clock waits do.
        clock["now"] += timedelta(seconds=min(max(delay, 0.05), 0.5))
        await asyncio.sleep(0)

    world = _WorldHost()
    host = QQC2CHost(
        host=world,  # type: ignore[arg-type]
        recipient_id="10001",
        canonical_user_id="geoff",
        ingress_store=MemoryQQIngressStore(),
        ingress_now=lambda: clock["now"],
        ingress_sleep=advancing_sleep,
    )
    opening = await host.inbound_fragment(_text("message:b0", "早呀", observed_at=NOW))
    assert opening.status == "observed_only"

    clock["now"] += timedelta(seconds=5)
    burst_started = clock["now"]
    first = asyncio.create_task(
        host.inbound_fragment(_text("message:b1", "被快递员吵醒了", observed_at=burst_started))
    )
    for _ in range(4):
        await asyncio.sleep(0)
    second = asyncio.create_task(
        host.inbound_fragment(
            _text(
                "message:b2",
                "本来不想起这么早的",
                observed_at=burst_started + timedelta(seconds=2),
            )
        )
    )
    left, right = await asyncio.gather(first, second)

    assert left == right
    assert len(world.inbounds) == 2
    assert world.inbounds[0].text == "早呀"
    assert world.inbounds[1].text == "被快递员吵醒了\n本来不想起这么早的"
    await host.aclose()


@pytest.mark.asyncio
async def test_opening_burst_without_prior_context_still_joins_one_turn() -> None:
    """Even a session-opening pair of bubbles seconds apart gets one reply.

    Production 10:44 case: "今天要打比赛了" then "还有点紧张" three seconds
    later were answered separately because only mid-session messages paid a
    composure pause.  The adaptive hold now applies to every content message.
    """

    clock = {"now": NOW}

    async def advancing_sleep(delay: float) -> None:
        clock["now"] += timedelta(seconds=min(max(delay, 0.05), 0.5))
        await asyncio.sleep(0)

    world = _WorldHost()
    host = QQC2CHost(
        host=world,  # type: ignore[arg-type]
        recipient_id="10001",
        canonical_user_id="geoff",
        ingress_store=MemoryQQIngressStore(),
        ingress_now=lambda: clock["now"],
        ingress_sleep=advancing_sleep,
    )
    first = asyncio.create_task(
        host.inbound_fragment(_text("message:o1", "今天要打比赛了", observed_at=NOW))
    )
    for _ in range(4):
        await asyncio.sleep(0)
    second = asyncio.create_task(
        host.inbound_fragment(
            _text("message:o2", "还有点紧张", observed_at=NOW + timedelta(seconds=3))
        )
    )
    left, right = await asyncio.gather(first, second)

    assert left == right
    assert len(world.inbounds) == 1
    assert world.inbounds[0].text == "今天要打比赛了\n还有点紧张"
    await host.aclose()


def _manual_clock(start: datetime):
    """A shared test clock that only the driving test moves.

    Holds and claims yield through ``idle_sleep`` without touching the clock,
    so fragment arrival instants and measured cadence gaps are exact instead
    of drifting with the scheduling order of concurrent hold loops.
    """

    clock = {"now": start}

    async def idle_sleep(_delay: float) -> None:
        await asyncio.sleep(0)

    async def drive(condition, *, step: float = 0.1, limit_seconds: float = 120.0) -> None:
        for _ in range(int(limit_seconds / step)):
            if condition():
                return
            await asyncio.sleep(0)
            if condition():
                return
            clock["now"] += timedelta(seconds=step)
        raise AssertionError("test clock driver exhausted its budget")

    return clock, idle_sleep, drive


@pytest.mark.asyncio
async def test_burst_continuing_through_her_turn_is_not_sliced_by_stale_cadence() -> None:
    """Replicates the 2026-07-20 13:05 production slice: one volley, one turn.

    A fast opening pair (2s apart) becomes one batch and its turn runs for
    several seconds.  The third bubble lands mid-turn at a 7s cadence and the
    fourth follows 7s after the third.  The fast pair used to pollute the
    cadence median and the closed "…啦" tail shortened it further, so bubbles
    three and four were answered as two separate turns.  The burst floor now
    keeps bubble three waiting ~1.2x the just-shown 7s rhythm, and the pair's
    fragments (already committed) no longer claim the volley early.
    """

    start = NOW
    clock, idle_sleep, drive = _manual_clock(start)
    world = _SlowFirstTurnWorldHost()
    host = QQC2CHost(
        host=world,  # type: ignore[arg-type]
        recipient_id="10001",
        canonical_user_id="geoff",
        ingress_store=MemoryQQIngressStore(),
        ingress_now=lambda: clock["now"],
        ingress_sleep=idle_sleep,
    )
    first = asyncio.create_task(
        host.inbound_fragment(_text("message:v1", "早上打了羽毛球", observed_at=start))
    )
    await asyncio.sleep(0)
    await drive(lambda: clock["now"] >= start + timedelta(seconds=2))
    second = asyncio.create_task(
        host.inbound_fragment(
            _text("message:v2", "中午就比完啦", observed_at=start + timedelta(seconds=2))
        )
    )
    await asyncio.sleep(0)
    await drive(lambda: world.first_turn_started.is_set())
    assert host._visible_turn_in_flight()

    # Third bubble: 7s after the second, while her turn still owns the lock.
    await drive(lambda: clock["now"] >= start + timedelta(seconds=9))
    third = asyncio.create_task(
        host.inbound_fragment(
            _text("message:v3", "对了教练夸我进步啦", observed_at=start + timedelta(seconds=9))
        )
    )
    await asyncio.sleep(0)

    # The first turn ends inside the old danger window (after the third
    # bubble's coalescing due, before the fourth bubble arrives).
    await drive(lambda: clock["now"] >= start + timedelta(seconds=12))
    world.release_first_turn.set()
    await drive(lambda: first.done() and second.done())
    assert len(world.inbounds) == 1

    await drive(lambda: clock["now"] >= start + timedelta(seconds=16))
    fourth = asyncio.create_task(
        host.inbound_fragment(
            _text("message:v4", "晚上一起打游戏呀", observed_at=start + timedelta(seconds=16))
        )
    )
    await drive(lambda: third.done() and fourth.done())
    results = await asyncio.gather(first, second, third, fourth)

    assert all(item.status == "observed_only" for item in results)
    assert len(world.inbounds) == 2
    assert world.inbounds[0].text == "早上打了羽毛球\n中午就比完啦"
    assert world.inbounds[1].text == "对了教练夸我进步啦\n晚上一起打游戏呀"
    assert world.inbounds[1].coalescing_metadata["source_event_ids"] == [
        "message:v3",
        "message:v4",
    ]
    await host.aclose()


@pytest.mark.asyncio
async def test_sustained_burst_rolls_past_the_old_deadline_into_one_turn() -> None:
    """A ~3s-cadence volley lasting >18s is absorbed whole, not deadline-cut."""

    start = NOW
    clock, idle_sleep, drive = _manual_clock(start)
    world = _WorldHost()
    host = QQC2CHost(
        host=world,  # type: ignore[arg-type]
        recipient_id="10001",
        canonical_user_id="geoff",
        ingress_store=MemoryQQIngressStore(),
        ingress_now=lambda: clock["now"],
        ingress_sleep=idle_sleep,
    )
    texts = (
        "今天超累",
        "早八连着三节课",
        "中午又去帮忙搬器材",
        "下午实验课还迟到了",
        "老师让我写检讨",
        "晚饭还没吃上",
        "现在才到宿舍",
        "感觉整个人都空了",
    )
    tasks = []
    for index, text in enumerate(texts):
        offset = index * 3
        await drive(lambda: clock["now"] >= start + timedelta(seconds=offset))
        tasks.append(
            asyncio.create_task(
                host.inbound_fragment(
                    _text(
                        f"message:roll{index}",
                        text,
                        observed_at=start + timedelta(seconds=offset),
                    )
                )
            )
        )
        await asyncio.sleep(0)
    await drive(lambda: all(task.done() for task in tasks))
    results = await asyncio.gather(*tasks)

    # The old per-fragment deadline (8-18s) would have claimed a partial
    # batch mid-volley; the rolling hold answers the 21s volley exactly once.
    assert all(item.status == "observed_only" for item in results)
    assert len(world.inbounds) == 1
    assert world.inbounds[0].text == "\n".join(texts)
    assert world.inbounds[0].coalescing_metadata["source_event_ids"] == [
        f"message:roll{index}" for index in range(len(texts))
    ]
    assert host._rhythm_holds == 0
    await host.aclose()


@pytest.mark.asyncio
async def test_burst_hold_hard_cap_answers_a_never_quiet_volley_at_thirty_seconds() -> None:
    """However long bubbles keep landing, the first one speaks by +30s."""

    start = NOW
    clock, idle_sleep, drive = _manual_clock(start)

    class _StampingWorldHost(_WorldHost):
        def __init__(self) -> None:
            super().__init__()
            self.inbound_at: list[datetime] = []

        async def inbound(self, inbound):  # type: ignore[no-untyped-def]
            self.inbound_at.append(clock["now"])
            return await super().inbound(inbound)

    world = _StampingWorldHost()
    host = QQC2CHost(
        host=world,  # type: ignore[arg-type]
        recipient_id="10001",
        canonical_user_id="geoff",
        ingress_store=MemoryQQIngressStore(),
        ingress_now=lambda: clock["now"],
        ingress_sleep=idle_sleep,
    )
    # Every tail trails off, so the adaptive quiet gap (~8.8s) always exceeds
    # the 4s cadence and quiet alone would never end the hold.
    texts = (
        "刚才那个事我还没说完，",
        "就是上次说的那个比赛，",
        "教练今天突然说要加练，",
        "而且",
        "然后周末还要集训，",
        "我周六可能去不了了，",
        "本来都跟你约好了，",
        "就很烦，",
    )
    tasks = []
    for index, text in enumerate(texts):
        offset = index * 4
        await drive(lambda: clock["now"] >= start + timedelta(seconds=offset))
        tasks.append(
            asyncio.create_task(
                host.inbound_fragment(
                    _text(
                        f"message:cap{index}",
                        text,
                        observed_at=start + timedelta(seconds=offset),
                    )
                )
            )
        )
        await asyncio.sleep(0)
    assert world.inbounds == []
    await drive(lambda: all(task.done() for task in tasks))
    await asyncio.gather(*tasks)

    assert len(world.inbounds) == 1
    assert world.inbounds[0].text == "\n".join(texts)
    cap_elapsed = (world.inbound_at[0] - start).total_seconds()
    assert 30.0 <= cap_elapsed <= 31.5
    await host.aclose()


@pytest.mark.asyncio
async def test_scheduler_ingress_pass_yields_while_a_rhythm_hold_absorbs_a_volley() -> None:
    """A periodic drain must not slice a claim-due batch out of a live hold."""

    start = NOW
    clock, idle_sleep, drive = _manual_clock(start)
    world = _WorldHost()
    host = QQC2CHost(
        host=world,  # type: ignore[arg-type]
        recipient_id="10001",
        canonical_user_id="geoff",
        ingress_store=MemoryQQIngressStore(),
        ingress_now=lambda: clock["now"],
        ingress_sleep=idle_sleep,
    )
    first = asyncio.create_task(
        host.inbound_fragment(_text("message:hold1", "刚到家", observed_at=start))
    )
    await asyncio.sleep(0)
    # The coalescing window has closed (the batch is claim-due), but the
    # fragment is still holding for the sender's rhythm.
    await drive(lambda: clock["now"] >= start + timedelta(seconds=1.5))
    assert host._rhythm_holds == 1
    assert await host.drain_ingress_once() is None
    assert world.inbounds == []

    await drive(lambda: clock["now"] >= start + timedelta(seconds=2.5))
    second = asyncio.create_task(
        host.inbound_fragment(
            _text("message:hold2", "还买了奶茶", observed_at=start + timedelta(seconds=2.5))
        )
    )
    await drive(lambda: first.done() and second.done())
    left, right = await asyncio.gather(first, second)

    # The deferred claim stayed with the volley: one batch, claimed by the
    # holding fragment itself once the sender went quiet.
    assert left == right
    assert len(world.inbounds) == 1
    assert world.inbounds[0].text == "刚到家\n还买了奶茶"
    assert host._rhythm_holds == 0
    assert await host.drain_ingress_once() is None
    await host.aclose()


def test_adaptive_quiet_gap_follows_cadence_and_message_shape() -> None:
    host = QQC2CHost(
        host=_WorldHost(),  # type: ignore[arg-type]
        recipient_id="10001",
        canonical_user_id="geoff",
        ingress_store=MemoryQQIngressStore(),
    )
    # No cadence yet: default base, biased by the message's own shape.
    assert host._quiet_gap_seconds("今天要打比赛了") == pytest.approx(3.5)
    assert host._quiet_gap_seconds("你吃饭了吗？") == pytest.approx(3.5 * 0.6)
    assert host._quiet_gap_seconds("我跟你说，") == pytest.approx(3.5 * 1.7)
    # A fast typist shrinks the base; a slow one grows it, both bounded.
    host._recent_gap_seconds.extend([1.0, 1.2, 1.1])
    assert host._quiet_gap_seconds("随便说点什么") == pytest.approx(1.5)
    host._recent_gap_seconds.clear()
    host._recent_gap_seconds.extend([20.0, 25.0, 30.0])
    assert host._quiet_gap_seconds("嗯") == pytest.approx(8.0)
    assert host._quiet_gap_seconds("而且") == pytest.approx(12.0)
    # Burst continuation: the just-shown cadence floors the wait, so a fast
    # historical median and a closed tail cannot slice an ongoing volley.
    host._recent_gap_seconds.clear()
    host._recent_gap_seconds.extend([2.0, 7.0])
    assert host._quiet_gap_seconds("中午就比完啦") == pytest.approx(8.0 * 0.6)
    assert host._quiet_gap_seconds("中午就比完啦", burst=True) == pytest.approx(7.0 * 1.2)
    # The floor is a floor, not a discount: a trailing-off tail still waits.
    assert host._quiet_gap_seconds("而且", burst=True) == pytest.approx(12.0)
    # The floor never exceeds the bounded maximum.
    host._recent_gap_seconds.append(11.0)
    assert host._quiet_gap_seconds("好啦", burst=True) == pytest.approx(12.0)
    # A last gap slower than the maximum is a lull, not a rhythm: no lift.
    host._recent_gap_seconds.clear()
    host._recent_gap_seconds.extend([20.0, 25.0, 30.0])
    assert host._quiet_gap_seconds("嗯", burst=True) == pytest.approx(8.0)
    # Without cadence samples the burst flag alone changes nothing.
    host._recent_gap_seconds.clear()
    assert host._quiet_gap_seconds("你吃饭了吗？", burst=True) == pytest.approx(3.5 * 0.6)


@pytest.mark.asyncio
async def test_peer_typing_pulse_extends_the_rhythm_hold_until_bubble_lands() -> None:
    """While QQ says the peer is typing, her claim keeps waiting for the bubble."""

    clock = {"now": NOW}

    async def advancing_sleep(delay: float) -> None:
        clock["now"] += timedelta(seconds=min(max(delay, 0.05), 0.5))
        await asyncio.sleep(0)

    world = _WorldHost()
    host = QQC2CHost(
        host=world,  # type: ignore[arg-type]
        recipient_id="10001",
        canonical_user_id="geoff",
        ingress_store=MemoryQQIngressStore(),
        ingress_now=lambda: clock["now"],
        ingress_sleep=advancing_sleep,
    )
    await host.inbound_fragment(_text("message:t0", "早呀", observed_at=NOW))
    clock["now"] += timedelta(seconds=5)
    burst_started = clock["now"]
    first = asyncio.create_task(
        host.inbound_fragment(_text("message:t1", "跟你说件事", observed_at=burst_started))
    )
    for _ in range(4):
        await asyncio.sleep(0)
    # 3.5s later (quiet gap nearly elapsed) QQ reports the peer still typing.
    clock["now"] = burst_started + timedelta(seconds=3.5)
    typing = await host.inbound_fragment(
        QQIngressFragment(
            source_event_id="qq-input-status:t",
            recipient_id="10001",
            observed_at=clock["now"],
            content_shape="control",
            control_kind="typing_started",
        )
    )
    assert typing.status == "deferred"
    # The slow bubble lands 7s after the burst began; without the typing
    # pulse the hold would have claimed at ~4s and answered without it.
    second = asyncio.create_task(
        host.inbound_fragment(
            _text("message:t2", "昨晚做了个特别长的梦", observed_at=burst_started + timedelta(seconds=7))
        )
    )
    left, right = await asyncio.gather(first, second)

    assert left == right
    assert len(world.inbounds) == 2
    assert world.inbounds[1].text == "跟你说件事\n昨晚做了个特别长的梦"
    await host.aclose()


class _SlowFirstTurnWorldHost(_WorldHost):
    """Block the first inbound turn until released, like a slow model call."""

    def __init__(self) -> None:
        super().__init__()
        self.first_turn_started = asyncio.Event()
        self.release_first_turn = asyncio.Event()

    async def inbound(self, inbound):  # type: ignore[no-untyped-def]
        self.inbounds.append(inbound)
        if len(self.inbounds) == 1:
            self.first_turn_started.set()
            await self.release_first_turn.wait()
        return SimpleNamespace(
            status="observed_only", authorized_action_ids=(), scheduled_action_ids=()
        )


@pytest.mark.asyncio
async def test_messages_arriving_during_slow_turn_join_one_followup_turn() -> None:
    """Continuing to chat while a turn runs is one session, not one turn each."""

    clock = {"now": NOW}

    async def instant_sleep(delay: float) -> None:
        clock["now"] += timedelta(seconds=delay)
        await asyncio.sleep(0)

    world = _SlowFirstTurnWorldHost()
    host = QQC2CHost(
        host=world,  # type: ignore[arg-type]
        recipient_id="10001",
        canonical_user_id="geoff",
        ingress_store=MemoryQQIngressStore(),
        ingress_now=lambda: clock["now"],
        ingress_sleep=instant_sleep,
    )
    clock["now"] = NOW + timedelta(seconds=1)
    first = asyncio.create_task(host.inbound_fragment(_text("message:f1", "哈喽？")))
    await asyncio.wait_for(world.first_turn_started.wait(), timeout=5)

    clock["now"] = NOW + timedelta(seconds=6)
    second = asyncio.create_task(
        host.inbound_fragment(
            _text("message:f2", "看看你在干啥", observed_at=NOW + timedelta(seconds=5))
        )
    )
    await asyncio.sleep(0)
    clock["now"] = NOW + timedelta(seconds=11)
    third = asyncio.create_task(
        host.inbound_fragment(
            _text("message:f3", "看看你在干啥👀", observed_at=NOW + timedelta(seconds=10))
        )
    )
    await asyncio.sleep(0)
    clock["now"] = NOW + timedelta(seconds=12)
    world.release_first_turn.set()
    results = await asyncio.gather(first, second, third)

    assert all(item.status == "observed_only" for item in results)
    assert len(world.inbounds) == 2
    assert world.inbounds[0].text == "哈喽？"
    assert world.inbounds[1].text == "看看你在干啥\n看看你在干啥👀"
    assert world.inbounds[1].coalescing_metadata["source_event_ids"] == [
        "message:f2",
        "message:f3",
    ]
    await host.aclose()


@pytest.mark.asyncio
async def test_text_turn_fires_one_best_effort_typing_pulse() -> None:
    clock = {"now": NOW + timedelta(seconds=1)}
    pulses: list[str] = []

    async def typing_signal() -> None:
        pulses.append("composing")

    store = MemoryQQIngressStore()
    store.submit(_text("message:typing", "在忙吗"), received_at=NOW)
    world = _WorldHost()
    host = QQC2CHost(
        host=world,  # type: ignore[arg-type]
        recipient_id="10001", canonical_user_id="geoff", ingress_store=store,
        ingress_now=lambda: clock["now"], typing_signal=typing_signal,
    )
    try:
        result = await host.drain_ingress_once()
        assert result is not None
        await asyncio.sleep(0)
    finally:
        await host.aclose()
    assert pulses == ["composing"]


@pytest.mark.asyncio
async def test_typing_pulse_failure_never_fails_the_turn() -> None:
    clock = {"now": NOW + timedelta(seconds=1)}

    async def broken_signal() -> None:
        raise RuntimeError("provider offline")

    store = MemoryQQIngressStore()
    store.submit(_text("message:typing-broken", "在吗"), received_at=NOW)
    world = _WorldHost()
    host = QQC2CHost(
        host=world,  # type: ignore[arg-type]
        recipient_id="10001", canonical_user_id="geoff", ingress_store=store,
        ingress_now=lambda: clock["now"], typing_signal=broken_signal,
    )
    try:
        result = await host.drain_ingress_once()
        assert result is not None and result.status == "observed_only"
        await asyncio.sleep(0)
    finally:
        await host.aclose()
    assert len(world.inbounds) == 1


@pytest.mark.asyncio
async def test_host_restart_replays_claimed_batch_with_frozen_source_identity(
    tmp_path: Path,
) -> None:
    path = tmp_path / "host-restart.sqlite"
    prepared = SQLiteQQIngressStore(path)
    due = prepared.submit(_text("message:claimed", "别丢"), received_at=NOW).due_at
    original = prepared.claim_due(now=due)
    assert original is not None
    prepared.close()

    world = _WorldHost()
    host = QQC2CHost(
        host=world,  # type: ignore[arg-type]
        recipient_id="10001",
        canonical_user_id="geoff",
        ingress_store=SQLiteQQIngressStore(path),
        ingress_now=lambda: NOW + timedelta(days=1),
    )
    result = await host.drain_ingress_once()
    assert result is not None
    assert len(world.inbounds) == 1
    assert world.inbounds[0].platform_message_id == original.platform_message_id
    assert world.inbounds[0].coalescing_metadata["batch_id"] == original.batch_id
    assert await host.drain_ingress_once() is None
    await host.aclose()

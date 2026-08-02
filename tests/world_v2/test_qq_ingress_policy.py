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
    QQIngressPolicyRow,
    SQLiteQQIngressStore,
    normalize_onebot_qq_ingress,
)
from companion_daemon.world_v2.qq_c2c_host import QQC2CHost
from companion_daemon.world_v2.text_turn_endpoint import (
    SemanticEndpointPrediction,
    TextTurnEndpointController,
)


NOW = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)


def _text(source: str, text: str, *, observed_at: datetime = NOW) -> QQIngressFragment:
    return QQIngressFragment(
        source_event_id=source,
        recipient_id="10001",
        observed_at=observed_at,
        content_shape="text",
        text=text,
    )


def _catalog_with_window(window_ms: int) -> QQIngressPolicyCatalog:
    return QQIngressPolicyCatalog(
        tuple(
            QQIngressPolicyRow(
                content_shape=shape,  # type: ignore[arg-type]
                continuity_signal=signal,  # type: ignore[arg-type]
                window_ms=window_ms,
                batch_mode=("metadata_only" if shape == "control" else "ordered_multimodal"),
            )
            for shape in ("text", "attachment", "mixed", "reaction", "sticker", "control")
            for signal in (
                "unknown",
                "complete_thought",
                "possible_continuation",
                "long_narration",
                "new_interjection",
            )
        )
    )


class _SemanticEndpointFixture:
    model = "fixture:semantic-endpoint"

    def __init__(self, probability_bp: int) -> None:
        self.probability_bp = probability_bp
        self.evidence = []

    async def predict(self, evidence):  # type: ignore[no-untyped-def]
        self.evidence.append(evidence)
        return SemanticEndpointPrediction(
            continuation_probability_bp=self.probability_bp,
            confidence_bp=8_000,
            evidence_summary=f"observed {len(evidence.batch_texts)} bubble(s)",
            model_id=self.model,
        )


def test_qq_ingress_matrix_is_complete_machine_readable_and_bounded() -> None:
    catalog = QQIngressPolicyCatalog()
    manifest = catalog.manifest()

    assert manifest["version"] == "world-v2-qq-ingress-matrix.2"
    assert len(manifest["rows"]) == 30  # type: ignore[arg-type]
    assert len(catalog.digest) == 64
    assert {
        row["batch_mode"] for row in manifest["rows"]  # type: ignore[union-attr]
    } == {"ordered_multimodal", "metadata_only"}
    assert all(100 <= row["window_ms"] <= 500 for row in manifest["rows"])  # type: ignore[union-attr]


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


class _BargeInWorldHost(_WorldHost):
    def __init__(self) -> None:
        super().__init__()
        self.cancel_refs: list[str] = []

    async def cancel_superseded_expression_streams(self, trigger_ref: str) -> None:
        self.cancel_refs.append(trigger_ref)


class _FailOnceWorldHost(_WorldHost):
    async def inbound(self, inbound):  # type: ignore[no-untyped-def]
        self.inbounds.append(inbound)
        if len(self.inbounds) == 1:
            raise RuntimeError("injected turn failure")
        return SimpleNamespace(
            status="observed_only", authorized_action_ids=(), scheduled_action_ids=()
        )


class _DeliveredShapeWorldHost(_WorldHost):
    async def delivered_text_character_count(self, action_id: str) -> int | None:
        return {"action:text:1": 4, "action:text:2": 28}.get(action_id)


@pytest.mark.asyncio
async def test_endpoint_exchange_shape_counts_only_positively_delivered_text() -> None:
    host = QQC2CHost(
        host=_DeliveredShapeWorldHost(),  # type: ignore[arg-type]
        recipient_id="10001",
        canonical_user_id="geoff",
        ingress_store=MemoryQQIngressStore(),
    )
    host._recent_exchange_shapes.extend(  # noqa: SLF001 - endpoint evidence regression
        (("exchange:old", 2, 0), ("exchange:current", 1, 0))
    )
    host._exchange_id_by_action_id.update(  # noqa: SLF001 - endpoint evidence regression
        {
            "action:text:1": "exchange:old",
            "action:text:2": "exchange:current",
        }
    )
    try:
        await host._record_delivered_text_unit(  # noqa: SLF001 - endpoint evidence regression
            SimpleNamespace(
                action_id="action:text:1",
                action_kind="reply",
                status="settled",
                provider_status="provider_accepted",
            )
        )
        await host._record_delivered_text_unit(  # noqa: SLF001 - endpoint evidence regression
            SimpleNamespace(
                action_id="action:typing",
                action_kind="typing",
                status="settled",
                provider_status="delivered",
            )
        )
        for action_id in ("action:text:1", "action:text:2", "action:text:2"):
            await host._record_delivered_text_unit(  # noqa: SLF001
                SimpleNamespace(
                    action_id=action_id,
                    action_kind="reply",
                    status="settled",
                    provider_status="delivered",
                )
            )

        assert tuple(host._recent_exchange_shapes) == (  # noqa: SLF001
            ("exchange:old", 2, 1),
            ("exchange:current", 1, 1),
        )
        assert tuple(host._recent_character_message_character_counts) == (4, 28)  # noqa: SLF001
    finally:
        await host.aclose()


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
async def test_rapid_burst_bubbles_join_one_turn_inside_subsecond_budget() -> None:
    """Rapid consecutive bubbles still become one turn without seconds of hold."""

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
    for _ in range(1):
        await asyncio.sleep(0)
    second = asyncio.create_task(
        host.inbound_fragment(
            _text(
                "message:b2",
                "本来不想起这么早的",
                observed_at=burst_started + timedelta(milliseconds=200),
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
async def test_opening_rapid_burst_without_prior_context_still_joins_one_turn() -> None:
    """A session-opening pair inside the transport window gets one reply."""

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
    for _ in range(1):
        await asyncio.sleep(0)
    second = asyncio.create_task(
        host.inbound_fragment(
            _text(
                "message:o2",
                "还有点紧张",
                observed_at=NOW + timedelta(milliseconds=300),
            )
        )
    )
    left, right = await asyncio.gather(first, second)

    assert left == right
    assert len(world.inbounds) == 1
    assert world.inbounds[0].text == "今天要打比赛了\n还有点紧张"
    await host.aclose()


@pytest.mark.asyncio
async def test_real_wall_clock_pair_250ms_apart_joins_one_turn() -> None:
    """Future ``observed_at`` metadata cannot fake a real batching interval."""

    world = _WorldHost()
    host = QQC2CHost(
        host=world,  # type: ignore[arg-type]
        recipient_id="10001",
        canonical_user_id="geoff",
        ingress_store=MemoryQQIngressStore(),
    )
    first = asyncio.create_task(
        host.inbound_fragment(_text("message:wall1", "今天要打比赛了"))
    )
    await asyncio.sleep(0.25)
    second = asyncio.create_task(
        host.inbound_fragment(
            _text(
                "message:wall2",
                "还有点紧张",
                observed_at=NOW + timedelta(milliseconds=250),
            )
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

    async def idle_sleep(delay: float) -> None:
        target = clock["now"] + timedelta(seconds=max(delay, 0.0))
        while clock["now"] < target:
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
async def test_semantic_endpoint_commits_a_likely_complete_single_bubble_after_100ms() -> None:
    clock, idle_sleep, drive = _manual_clock(NOW)
    world = _WorldHost()
    host = QQC2CHost(
        host=world,  # type: ignore[arg-type]
        recipient_id="10001",
        canonical_user_id="geoff",
        ingress_store=MemoryQQIngressStore(catalog=_catalog_with_window(100)),
        ingress_now=lambda: clock["now"],
        ingress_sleep=idle_sleep,
        endpoint_controller=TextTurnEndpointController(
            model=_SemanticEndpointFixture(500)
        ),
    )
    task = asyncio.create_task(
        host.inbound_fragment(_text("message:endpoint-fast", "早", observed_at=NOW))
    )
    try:
        await drive(lambda: len(world.inbounds) == 1, step=0.01, limit_seconds=1)
        result = await task
        assert result.status == "observed_only"
        # The manual driver advances in 10ms quanta and may need several event
        # loop handoffs for claim/commit; the configured listening opportunity
        # itself remains the independently asserted 100ms below.
        assert clock["now"] <= NOW + timedelta(milliseconds=200)
        assert host.text_endpoint_health()["last_schedule"]["wait_ms"] == 100  # type: ignore[index]
    finally:
        await host.aclose()


@pytest.mark.asyncio
async def test_semantic_endpoint_keeps_a_seconds_later_continuation_in_one_turn() -> None:
    clock, idle_sleep, drive = _manual_clock(NOW)
    world = _WorldHost()
    host = QQC2CHost(
        host=world,  # type: ignore[arg-type]
        recipient_id="10001",
        canonical_user_id="geoff",
        ingress_store=MemoryQQIngressStore(catalog=_catalog_with_window(100)),
        ingress_now=lambda: clock["now"],
        ingress_sleep=idle_sleep,
        endpoint_controller=TextTurnEndpointController(
            model=_SemanticEndpointFixture(9_000)
        ),
    )
    first = asyncio.create_task(
        host.inbound_fragment(
            _text("message:endpoint-long-1", "其实我还想说", observed_at=NOW)
        )
    )
    second: asyncio.Task[object] | None = None
    try:
        await drive(
            lambda: clock["now"] >= NOW + timedelta(seconds=1),
            step=0.05,
        )
        assert world.inbounds == []
        second = asyncio.create_task(
            host.inbound_fragment(
                _text(
                    "message:endpoint-long-2",
                    "下午那件事后来有进展了",
                    observed_at=clock["now"],
                )
            )
        )
        await drive(lambda: len(world.inbounds) == 1, step=0.05, limit_seconds=5)
        left, right = await asyncio.gather(first, second)
        assert left == right
        assert world.inbounds[0].text == "其实我还想说\n下午那件事后来有进展了"
    finally:
        for task in (first, second):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(
            *(task for task in (first, second) if task is not None),
            return_exceptions=True,
        )
        await host.aclose()


@pytest.mark.asyncio
async def test_endpoint_evidence_keeps_bounded_personal_cadence_and_exchange_shape() -> None:
    model = _SemanticEndpointFixture(500)
    world = _WorldHost()
    host = QQC2CHost(
        host=world,  # type: ignore[arg-type]
        recipient_id="10001",
        canonical_user_id="geoff",
        ingress_store=MemoryQQIngressStore(catalog=_catalog_with_window(100)),
        endpoint_controller=TextTurnEndpointController(model=model),
    )
    try:
        # Long-running personal cadence history remains bounded at the exact
        # endpoint evidence contract instead of eventually throwing on the
        # seventeenth observed gap.
        host._personal_bubble_gap_seconds.extend(  # noqa: SLF001 - contract regression
            0.2 + index / 100 for index in range(40)
        )
        host._recent_exchange_shapes.extend(  # noqa: SLF001 - contract regression
            (f"exchange:{index}", index % 5, (index + 1) % 4)
            for index in range(24)
        )
        host._endpoint_fragments.append(  # noqa: SLF001 - contract regression
            ("message:endpoint-evidence", "我再想想")
        )
        host._restart_endpoint_prediction(received_at=NOW)  # noqa: SLF001
        assert host._endpoint_task is not None  # noqa: SLF001
        await host._endpoint_task  # noqa: SLF001

        captured = model.evidence[-1]
        assert len(captured.recent_gap_seconds) == 16
        assert len(captured.recent_exchange_user_bubble_counts) == 16
        assert len(captured.recent_exchange_character_bubble_counts) == 16
        assert tuple(zip(
            captured.recent_exchange_user_bubble_counts,
            captured.recent_exchange_character_bubble_counts,
            strict=True,
        )) == tuple(
            (user_count, character_count)
            for _, user_count, character_count in host._recent_exchange_shapes  # noqa: SLF001
        )
    finally:
        await host.aclose()


@pytest.mark.asyncio
async def test_burst_during_provider_joins_then_enters_as_one_interjection() -> None:
    """A rapid pair stays one batch without waiting for the old provider."""

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
    await drive(lambda: clock["now"] >= start + timedelta(milliseconds=200))
    second = asyncio.create_task(
        host.inbound_fragment(
            _text(
                "message:v2",
                "中午就比完啦",
                observed_at=start + timedelta(milliseconds=200),
            )
        )
    )
    await asyncio.sleep(0)
    await drive(lambda: world.first_turn_started.is_set())
    assert host._visible_turn_in_flight()

    await drive(lambda: clock["now"] >= start + timedelta(seconds=1.5))
    third = asyncio.create_task(
        host.inbound_fragment(
            _text(
                "message:v3",
                "对了教练夸我进步啦",
                observed_at=start + timedelta(seconds=1.5),
            )
        )
    )
    await asyncio.sleep(0)

    await drive(lambda: clock["now"] >= start + timedelta(seconds=2.0))
    fourth = asyncio.create_task(
        host.inbound_fragment(
            _text(
                "message:v4",
                "晚上一起打游戏呀",
                observed_at=start + timedelta(seconds=2.0),
            )
        )
    )
    await drive(lambda: len(world.inbounds) == 2)
    assert not world.release_first_turn.is_set()
    assert world.inbounds[1].text == "对了教练夸我进步啦\n晚上一起打游戏呀"

    world.release_first_turn.set()
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
async def test_sustained_rapid_burst_uses_one_turn_and_finishes_after_last_bubble() -> None:
    """A sustained subsecond volley is absorbed without a fixed multi-second tax."""

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
        offset = index * 0.2
        await drive(
            lambda: clock["now"] >= start + timedelta(seconds=offset),
            step=0.005,
        )
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

    assert all(item.status == "observed_only" for item in results)
    assert len(world.inbounds) == 1
    assert world.inbounds[0].text == "\n".join(texts)
    assert world.inbounds[0].coalescing_metadata["source_event_ids"] == [
        f"message:roll{index}" for index in range(len(texts))
    ]
    assert host._rhythm_holds == 0
    await host.aclose()


@pytest.mark.asyncio
async def test_burst_answers_within_one_second_after_the_last_bubble() -> None:
    """A continuous rapid volley adds no fixed 30-second post-input wait."""

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
        offset = index * 0.25
        await drive(
            lambda: clock["now"] >= start + timedelta(seconds=offset),
            step=0.005,
        )
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
    elapsed = (world.inbound_at[0] - start).total_seconds()
    last_bubble_at = (len(texts) - 1) * 0.25
    assert last_bubble_at <= elapsed <= last_bubble_at + 1.0
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
    # Before the durable window closes, the scheduler cannot claim it.
    await drive(
        lambda: clock["now"] >= start + timedelta(seconds=0.275),
        step=0.005,
    )
    assert host._rhythm_holds == 0
    assert await host.drain_ingress_once() is None
    assert world.inbounds == []

    second = asyncio.create_task(
        host.inbound_fragment(
            _text(
                "message:hold2",
                "还买了奶茶",
                observed_at=start + timedelta(seconds=0.275),
            )
        )
    )
    await drive(lambda: host._rhythm_holds >= 1, step=0.005)
    assert host._rhythm_holds >= 1
    assert await host.drain_ingress_once() is None
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


def test_adaptive_quiet_gap_follows_observed_cadence_without_reading_semantics() -> None:
    host = QQC2CHost(
        host=_WorldHost(),  # type: ignore[arg-type]
        recipient_id="10001",
        canonical_user_id="geoff",
        ingress_store=MemoryQQIngressStore(),
    )
    # No cadence yet: different wording receives the same provider-local
    # opportunity. The host does not decide whether either thought is complete.
    assert host._quiet_gap_seconds("今天要打比赛了") == pytest.approx(0.15)
    assert host._quiet_gap_seconds("你吃饭了吗？") == pytest.approx(0.15)
    assert host._quiet_gap_seconds("我跟你说，") == pytest.approx(0.15)
    # A fast typist shrinks the base; a slow one grows it, both bounded below
    # the one-second local budget.
    host._recent_gap_seconds.extend([0.1, 0.12, 0.11])
    assert host._quiet_gap_seconds("随便说点什么") == pytest.approx(0.11 * 1.3)
    host._recent_gap_seconds.clear()
    host._recent_gap_seconds.extend([2.0, 2.5, 3.0])
    assert host._quiet_gap_seconds("嗯") == pytest.approx(0.42)
    assert host._quiet_gap_seconds("而且") == pytest.approx(0.42)
    # Burst continuation: the just-shown cadence floors the wait, so a fast
    # historical median cannot slice an ongoing volley.
    host._recent_gap_seconds.clear()
    host._recent_gap_seconds.extend([0.2, 0.7])
    assert host._quiet_gap_seconds("中午就比完啦") == pytest.approx(0.42)
    assert host._quiet_gap_seconds("中午就比完啦", burst=True) == pytest.approx(0.8)
    # Wording cannot change the observed-cadence floor.
    assert host._quiet_gap_seconds("而且", burst=True) == pytest.approx(0.8)
    # The floor never exceeds the bounded maximum.
    host._recent_gap_seconds.append(0.75)
    assert host._quiet_gap_seconds("好啦", burst=True) == pytest.approx(0.8)
    # A last gap slower than the maximum is a lull, not a rhythm: no lift.
    host._recent_gap_seconds.clear()
    host._recent_gap_seconds.extend([2.0, 2.5, 3.0])
    assert host._quiet_gap_seconds("嗯", burst=True) == pytest.approx(0.42)
    # Without cadence samples the burst flag alone changes nothing.
    host._recent_gap_seconds.clear()
    assert host._quiet_gap_seconds("你吃饭了吗？", burst=True) == pytest.approx(
        0.15
    )


def test_settled_turn_gap_does_not_inflate_the_next_speculative_wait() -> None:
    host = QQC2CHost(
        host=_WorldHost(),  # type: ignore[arg-type]
        recipient_id="10001",
        canonical_user_id="geoff",
        ingress_store=MemoryQQIngressStore(),
    )
    host._recent_gap_seconds.extend([0.31, 0.34])

    host._register_content_gap(
        received_at=NOW + timedelta(seconds=0.36),
        previous_received_at=NOW,
        continuation_observed=False,
    )

    assert tuple(host._recent_gap_seconds) == ()
    assert host._quiet_gap_seconds("下一轮") == pytest.approx(0.15)


@pytest.mark.asyncio
async def test_scheduler_serialization_lock_is_not_user_continuation_evidence() -> None:
    host = QQC2CHost(
        host=_WorldHost(),  # type: ignore[arg-type]
        recipient_id="10001",
        canonical_user_id="geoff",
        ingress_store=MemoryQQIngressStore(),
    )

    async with host._lock:
        assert not host._visible_turn_in_flight()


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
    for _ in range(1):
        await asyncio.sleep(0)
    # Before the subsecond hold elapses QQ reports the peer still typing.
    clock["now"] = burst_started + timedelta(seconds=0.5)
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
    # The next bubble lands inside the same bounded transport opportunity.
    second = asyncio.create_task(
        host.inbound_fragment(
            _text(
                "message:t2",
                "昨晚做了个特别长的梦",
                observed_at=burst_started + timedelta(seconds=0.7),
            )
        )
    )
    left, right = await asyncio.gather(first, second)

    assert left == right
    assert len(world.inbounds) == 2
    assert world.inbounds[1].text == "跟你说件事\n昨晚做了个特别长的梦"
    await host.aclose()


@pytest.mark.asyncio
async def test_typing_pulse_opens_one_role_owned_barge_in_opportunity() -> None:
    """Typing can open an early role decision without forcing a reply."""

    clock = {"now": NOW}

    async def idle_sleep(_delay: float) -> None:
        await asyncio.sleep(0)

    world = _BargeInWorldHost()
    host = QQC2CHost(
        host=world,  # type: ignore[arg-type]
        recipient_id="10001",
        canonical_user_id="geoff",
        ingress_store=MemoryQQIngressStore(catalog=_catalog_with_window(100)),
        ingress_now=lambda: clock["now"],
        ingress_sleep=idle_sleep,
        barge_in_probe_seconds=0.2,
    )
    first = asyncio.create_task(
        host.inbound_fragment(_text("message:barge-in", "我还没说完", observed_at=NOW))
    )
    await asyncio.sleep(0)
    clock["now"] = NOW + timedelta(milliseconds=100)
    typing = await host.inbound_fragment(
        QQIngressFragment(
            source_event_id="qq-input-status:barge-in",
            recipient_id="10001",
            observed_at=clock["now"],
            content_shape="control",
            control_kind="typing_started",
        )
    )
    assert typing.status == "deferred"
    clock["now"] = NOW + timedelta(milliseconds=350)
    result = await asyncio.wait_for(first, timeout=0.5)

    assert result.status == "observed_only"
    assert len(world.inbounds) == 1
    advisory = world.inbounds[0].coalescing_metadata["turn_attention_advisory"]
    assert advisory["typing_active"] is True
    assert world.cancel_refs == [
        "qq-inbound-attention:message:barge-in",
        "qq-barge-in:qq-input-status:barge-in",
    ]
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
async def test_close_waits_for_process_owned_ingress_after_caller_cancels() -> None:
    """Caller cancellation must not let shutdown close the ledger under its batch."""

    clock = {"now": NOW + timedelta(seconds=1)}

    async def instant_sleep(delay: float) -> None:
        clock["now"] += timedelta(seconds=delay)
        await asyncio.sleep(0)

    world = _SlowFirstTurnWorldHost()
    store = MemoryQQIngressStore()
    host = QQC2CHost(
        host=world,  # type: ignore[arg-type]
        recipient_id="10001",
        canonical_user_id="geoff",
        ingress_store=store,
        ingress_now=lambda: clock["now"],
        ingress_sleep=instant_sleep,
        owned_action_close_grace_seconds=0.5,
    )
    caller = asyncio.create_task(
        host.inbound_fragment(_text("message:owned-close", "这条已经进入世界认知。"))
    )
    closing: asyncio.Task[None] | None = None
    try:
        await asyncio.wait_for(world.first_turn_started.wait(), timeout=1)
        caller.cancel()
        await asyncio.gather(caller, return_exceptions=True)

        closing = asyncio.create_task(host.aclose())
        await asyncio.sleep(0.05)

        assert not closing.done()
        assert world.closed is False
    finally:
        world.release_first_turn.set()
        if closing is None:
            closing = asyncio.create_task(host.aclose())
        await asyncio.wait_for(closing, timeout=1)

    submission = store.submission("message:owned-close")
    assert submission is not None and submission.state == "committed"


@pytest.mark.asyncio
async def test_cancelled_ingress_waiter_is_removed_after_owned_batch_finishes() -> None:
    """A shielded batch remains owned, then leaves the process join registry."""

    clock = {"now": NOW + timedelta(seconds=1)}

    async def instant_sleep(delay: float) -> None:
        clock["now"] += timedelta(seconds=delay)
        await asyncio.sleep(0)

    world = _SlowFirstTurnWorldHost()
    store = MemoryQQIngressStore()
    host = QQC2CHost(
        host=world,  # type: ignore[arg-type]
        recipient_id="10001",
        canonical_user_id="geoff",
        ingress_store=store,
        ingress_now=lambda: clock["now"],
        ingress_sleep=instant_sleep,
    )
    caller = asyncio.create_task(
        host.inbound_fragment(_text("message:cancelled-waiter", "这条继续由进程完成。"))
    )
    await asyncio.wait_for(world.first_turn_started.wait(), timeout=1)

    caller.cancel()
    await asyncio.gather(caller, return_exceptions=True)
    assert len(host._ingress_batch_tasks) == 1  # noqa: SLF001 - lifecycle seam

    world.release_first_turn.set()
    for _ in range(10):
        if not host._ingress_batch_tasks:  # noqa: SLF001 - lifecycle seam
            break
        await asyncio.sleep(0)

    assert host._ingress_batch_tasks == {}  # noqa: SLF001 - lifecycle seam
    submission = store.submission("message:cancelled-waiter")
    assert submission is not None and submission.state == "committed"
    assert len(world.inbounds) == 1
    await host.aclose()


@pytest.mark.asyncio
async def test_close_prefers_world_async_lifecycle_before_sync_store_close() -> None:
    class _AsyncClosingWorld(_WorldHost):
        def __init__(self) -> None:
            super().__init__()
            self.async_closed = False
            self.sync_closed = False

        async def aclose(self) -> None:
            await asyncio.sleep(0)
            self.async_closed = True

        def close(self) -> None:
            self.sync_closed = True

    world = _AsyncClosingWorld()
    store = MemoryQQIngressStore()
    host = QQC2CHost(
        host=world,  # type: ignore[arg-type]
        recipient_id="10001",
        canonical_user_id="geoff",
        ingress_store=store,
    )

    await host.aclose()

    assert world.async_closed is True
    assert world.sync_closed is False


@pytest.mark.asyncio
async def test_close_defers_semantic_dependencies_while_world_shutdown_lease_is_open() -> None:
    release = asyncio.Event()

    class _LeasedWorld(_WorldHost):
        async def aclose(self) -> None:
            return None

        @property
        def shutdown_pending_task_count(self) -> int:
            return 0 if release.is_set() else 1

        async def wait_for_shutdown_quiescence(self) -> None:
            await release.wait()

    class _SemanticDependencies:
        def __init__(self) -> None:
            self.closed = False

        async def aclose(self) -> None:
            self.closed = True

    world = _LeasedWorld()
    semantic = _SemanticDependencies()
    host = QQC2CHost(
        host=world,  # type: ignore[arg-type]
        recipient_id="10001",
        canonical_user_id="geoff",
        semantic_chat=semantic,  # type: ignore[arg-type]
        ingress_store=MemoryQQIngressStore(),
    )

    await host.aclose()

    assert semantic.closed is False
    assert host.shutdown_pending_task_count == 1

    release.set()
    await asyncio.wait_for(host.wait_for_shutdown_quiescence(), timeout=1)

    assert semantic.closed is True
    assert host.shutdown_pending_task_count == 0


@pytest.mark.asyncio
async def test_close_reports_semantic_shutdown_lease_after_bounded_semantic_close() -> None:
    release = asyncio.Event()

    class _SemanticDependencies:
        def __init__(self) -> None:
            self.close_called = False

        async def aclose(self) -> None:
            self.close_called = True

        @property
        def shutdown_pending_task_count(self) -> int:
            return 0 if release.is_set() else int(self.close_called)

        async def wait_for_shutdown_quiescence(self) -> None:
            await release.wait()

    semantic = _SemanticDependencies()
    host = QQC2CHost(
        host=_WorldHost(),  # type: ignore[arg-type]
        recipient_id="10001",
        canonical_user_id="geoff",
        semantic_chat=semantic,  # type: ignore[arg-type]
        ingress_store=MemoryQQIngressStore(),
    )

    await host.aclose()

    assert semantic.close_called is True
    assert host.shutdown_pending_task_count == 1
    quiescence = asyncio.create_task(host.wait_for_shutdown_quiescence())
    await asyncio.sleep(0)
    assert quiescence.done() is False

    release.set()
    await asyncio.wait_for(quiescence, timeout=1)
    assert host.shutdown_pending_task_count == 0


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

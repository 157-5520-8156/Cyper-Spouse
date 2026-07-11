from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from companion_daemon.db import CompanionStore
from companion_daemon.engine import CompanionEngine, seed_user
from companion_daemon.llm import FakeCompanionModel
from companion_daemon.models import IncomingMessage, LifeRuntimeState
from companion_daemon.time import utc_now
from companion_daemon.social_followups import (
    cancel_life_share_followup_for_event,
    create_life_share_followup,
    detect_mild_contradiction,
    reconcile_unshared_life_share_tasks,
)


def test_detect_mild_contradiction_when_user_quotes_sleep_but_she_is_in_class() -> None:
    runtime = LifeRuntimeState(
        activity_kind="class",
        activity="在上专业课",
        phone_attention="away",
        attention_demand=70,
        base_attention_demand=70,
        interruptible=False,
        started_at=datetime(2026, 7, 10, 10, 0, tzinfo=UTC),
        ends_at=datetime(2026, 7, 10, 11, 30, tzinfo=UTC),
        updated_at=datetime(2026, 7, 10, 10, 15, tzinfo=UTC),
    )
    note = detect_mild_contradiction("你不是说你在睡觉吗", runtime)
    assert note is not None
    assert "睡觉" in note


def test_private_life_event_schedules_life_share_followup(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    now = datetime(2026, 7, 10, 12, 0, tzinfo=UTC)
    event_id = store.record_life_event(
        "geoff",
        kind="private_life_event",
        content="午饭吃到一家新开的拌面",
        started_at=now,
        ends_at=now,
        status="completed",
        source="test:incidental",
    )
    create_life_share_followup(
        store,
        "geoff",
        life_event_id=event_id,
        content="午饭吃到一家新开的拌面",
        now=now,
    )
    task = store.recent_social_tasks("geoff")[0]
    assert task["kind"] == "life_share_followup"
    assert "小事" in task["reason"]


def test_reconcile_unshared_event_backfills_share_followup(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    old = datetime(2026, 7, 10, 8, 0, tzinfo=UTC)
    store.record_life_event(
        "geoff",
        kind="private_life_event",
        content="路上听到一句很好笑的话",
        started_at=old,
        ends_at=old,
        status="completed",
        source="test:stale",
    )
    task_id = reconcile_unshared_life_share_tasks(store, "geoff", now=old + timedelta(hours=2))
    assert task_id is not None
    assert store.has_active_social_task("geoff", kind="life_share_followup")


def test_marking_event_shared_cancels_life_share_followup(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    now = datetime(2026, 7, 10, 12, 0, tzinfo=UTC)
    event_id = store.record_life_event(
        "geoff",
        kind="private_life_event",
        content="咖啡洒了一点在书上",
        started_at=now,
        ends_at=now,
        status="completed",
        source="test:share-cancel",
    )
    create_life_share_followup(
        store,
        "geoff",
        life_event_id=event_id,
        content="咖啡洒了一点在书上",
        now=now,
    )
    cancel_life_share_followup_for_event(store, "geoff", event_id)
    assert not store.has_active_social_task("geoff", kind="life_share_followup")


@pytest.mark.asyncio
async def test_due_life_share_followup_uses_share_trigger_and_marks_event_shared(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    engine = CompanionEngine(store, FakeCompanionModel(), "你是知栀。")
    now = utc_now()
    event_id = store.record_life_event(
        "geoff",
        kind="private_life_event",
        content="自习时窗外突然下起小雨",
        started_at=now - timedelta(hours=2),
        ends_at=now - timedelta(hours=2),
        status="completed",
        source="test:due-share",
    )
    task_id = store.create_social_task(
        "geoff",
        kind="life_share_followup",
        platform="qq",
        platform_user_id="geoff",
        payload={"life_event_id": event_id, "content": "自习时窗外突然下起小雨"},
        reason="有件今天的小事还没自然地跟他说出口",
        due_at=now - timedelta(minutes=1),
        expires_at=now + timedelta(hours=6),
    )

    decision = await engine.proactive_tick("geoff")

    assert decision.trigger_type == "life_share_followup"
    assert decision.social_task_id == task_id
    engine.confirm_proactive_delivery(decision)
    row = store.life_event_by_source("geoff", "test:due-share")
    assert row is not None
    assert row["shared_at"] is not None


@pytest.mark.asyncio
async def test_contradiction_message_creates_followup_and_new_turn_cancels_it(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    engine = CompanionEngine(store, FakeCompanionModel(), "你是知栀。")
    now = datetime(2026, 7, 10, 10, 0, tzinfo=UTC)
    store.save_life_runtime(
        "geoff",
        LifeRuntimeState(
            activity_kind="class",
            activity="在上专业课",
            started_at=now,
            ends_at=now + timedelta(hours=1),
            updated_at=now,
        ),
    )

    await engine.handle_message(
        IncomingMessage(platform="qq", platform_user_id="geoff", text="你不是说你在睡觉吗"),
        skip_reply=True,
    )

    tasks = [row for row in store.recent_social_tasks("geoff") if row["kind"] == "contradiction_followup"]
    assert len(tasks) == 1
    task = tasks[0]
    assert "对不上" in task["reason"]

    await engine.handle_message(
        IncomingMessage(platform="qq", platform_user_id="geoff", text="我刚记错了哈哈"),
        skip_reply=True,
    )
    assert not store.has_active_social_task("geoff", kind="contradiction_followup")

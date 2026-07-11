from datetime import UTC, datetime, timedelta
from pathlib import Path

from companion_daemon.calendar_ledger import calendar_context_for_message, calendar_ledger
from companion_daemon.db import CompanionStore
from companion_daemon.engine import seed_user
from companion_daemon.models import MoodState


def test_calendar_ledger_materializes_future_plans_without_lived_events(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "calendar.sqlite")
    seed_user(store)
    now = datetime(2026, 7, 10, 4, 0, tzinfo=UTC)  # 12:00 Shanghai

    ledger = calendar_ledger(store, "geoff", MoodState(), now=now, past_days=1, future_days=2)

    tomorrow = next(day for day in ledger["days"] if day["relative"] == "明天")
    assert tomorrow["plans"]
    assert tomorrow["events"] == []


def test_calendar_context_keeps_future_plans_and_past_events_separate(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "calendar.sqlite")
    seed_user(store)
    now = datetime(2026, 7, 10, 4, 0, tzinfo=UTC)
    yesterday = now - timedelta(days=1)
    store.record_life_event(
        "geoff",
        kind="private_life_event",
        content="傍晚散步时拍了几张路灯的照片",
        started_at=yesterday,
        ends_at=yesterday,
        status="completed",
        source="test:calendar",
    )

    future = calendar_context_for_message(store, "geoff", MoodState(), "你明天准备做什么？", now=now)
    past = calendar_context_for_message(store, "geoff", MoodState(), "你昨天做了什么来着？", now=now)

    assert future and "仅可依据计划" in future and "仅可依据已发生记录" not in future
    assert past and "仅可依据已发生记录" in past and "路灯" in past


def test_calendar_context_refuses_ungrounded_past_day(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "calendar.sqlite")
    seed_user(store)
    now = datetime(2026, 7, 10, 4, 0, tzinfo=UTC)

    context = calendar_context_for_message(store, "geoff", MoodState(), "你上周三做了什么？", now=now)

    assert context and "没有已发生记录" in context

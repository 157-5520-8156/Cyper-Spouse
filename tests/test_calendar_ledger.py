from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

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
    assert any(day["special_events"] for day in ledger["days"])


def test_calendar_highlight_can_span_multiple_days(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "calendar.sqlite")
    seed_user(store)
    now = datetime(2026, 7, 10, 4, 0, tzinfo=UTC)
    store.create_calendar_event(
        "geoff", title="三天短途旅行", event_type="trip", starts_at=now + timedelta(days=1),
        ends_at=now + timedelta(days=4), importance=90, source="test:trip", details="和朋友约好去附近走走",
    )

    ledger = calendar_ledger(store, "geoff", MoodState(), now=now, past_days=0, future_days=5)

    trip_days = [day for day in ledger["days"] if any(event["title"] == "三天短途旅行" for event in day["special_events"])]
    assert len(trip_days) == 4


def test_lived_private_event_is_backfilled_as_a_distinct_calendar_memory(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "calendar.sqlite")
    seed_user(store)
    now = datetime(2026, 7, 10, 4, 0, tzinfo=UTC)
    store.record_life_event(
        "geoff", kind="private_life_event", content="图书馆遇到奇怪书名: 翻到一本很离谱的书",
        started_at=now - timedelta(days=1), ends_at=now - timedelta(days=1), status="completed", source="test:lived",
        shared_at=now - timedelta(days=1),
    )

    ledger = calendar_ledger(store, "geoff", MoodState(), now=now, past_days=2, future_days=0)

    yesterday = next(day for day in ledger["days"] if day["relative"] == "昨天")
    assert any(event["event_type"] == "lived_memory" for event in yesterday["special_events"])


def test_weekly_plan_is_stable_and_contains_only_a_few_named_events(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "calendar.sqlite")
    seed_user(store)
    now = datetime(2026, 7, 10, 4, 0, tzinfo=UTC)

    first = calendar_ledger(store, "geoff", MoodState(), now=now, past_days=0, future_days=7)
    second = calendar_ledger(store, "geoff", MoodState(), now=now + timedelta(hours=2), past_days=0, future_days=7)
    first_ids = {event["id"] for day in first["days"] for event in day["special_events"] if str(event["source"]).startswith("calendar:weekly:")}
    second_ids = {event["id"] for day in second["days"] for event in day["special_events"] if str(event["source"]).startswith("calendar:weekly:")}

    assert first_ids == second_ids
    assert 1 <= len(first_ids) <= 2


def test_elapsed_planned_event_is_cancelled_not_presented_as_future(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "calendar.sqlite")
    seed_user(store)
    now = datetime(2026, 7, 10, 12, 0, tzinfo=UTC)
    event_id = store.create_calendar_event(
        "geoff", title="已经错过的看展", event_type="social_plan", starts_at=now - timedelta(days=2),
        ends_at=now - timedelta(days=1), source="test:elapsed",
    )

    calendar_ledger(store, "geoff", MoodState(), now=now, past_days=3, future_days=2)

    row = next(event for event in store.calendar_events_between("geoff", starts_at=now - timedelta(days=3), ends_at=now) if event["id"] == event_id)
    assert row["status"] == "cancelled"
    assert row["changed_reason"]
    history = store.calendar_event_history(event_id)
    assert history[-1]["to_status"] == "cancelled"
    assert history[-1]["reason"] == row["changed_reason"]


def test_calendar_event_has_exactly_one_linked_memory_and_carries_cancellation_reason(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "calendar.sqlite")
    seed_user(store)
    now = datetime(2026, 7, 10, 4, 0, tzinfo=UTC)
    event_id = store.create_calendar_event(
        "geoff", title="临时取消的看展", event_type="social_plan", starts_at=now + timedelta(days=1),
        ends_at=now + timedelta(days=1, hours=2), source="test:cancelled",
    )
    store.update_calendar_event_status(event_id, status="cancelled", changed_reason="朋友临时发烧，改天再约")

    ledger = calendar_ledger(store, "geoff", MoodState(), now=now, past_days=0, future_days=2)
    event = next(event for day in ledger["days"] for event in day["special_events"] if event["id"] == event_id)

    assert event["memory_id"]
    assert event["memory_kind"] == "calendar_event"
    assert "朋友临时发烧" in event["memory_content"]
    assert event["changed_reason"] == "朋友临时发烧，改天再约"


def test_calendar_status_transitions_are_guarded(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "calendar.sqlite")
    seed_user(store)
    now = datetime(2026, 7, 10, 4, 0, tzinfo=UTC)
    event_id = store.create_calendar_event(
        "geoff", title="待确认安排", event_type="personal_plan", starts_at=now + timedelta(days=1),
        ends_at=now + timedelta(days=1, hours=1), source="test:transition",
    )

    with pytest.raises(ValueError, match="requires a reason"):
        store.update_calendar_event_status(event_id, status="cancelled")
    store.update_calendar_event_status(event_id, status="completed")
    with pytest.raises(ValueError, match="invalid calendar transition"):
        store.update_calendar_event_status(event_id, status="active")


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

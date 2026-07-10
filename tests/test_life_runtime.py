from datetime import UTC, datetime, timedelta
from pathlib import Path

from companion_daemon.db import CompanionStore
from companion_daemon.engine import seed_user
from companion_daemon.life_runtime import (
    advance_life_runtime,
    apply_life_event_result,
    apply_user_event_to_life_runtime,
    decide_phone_attention,
    mark_phone_idle,
    mark_phone_read,
    maybe_apply_planned_life_result,
    plan_daily_life_result,
    proactive_outreach_allowed,
    synchronize_life_runtime,
)
from companion_daemon.models import IncomingMessage, LifeRuntimeState, MoodState


def test_life_runtime_persists_current_activity(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    now = datetime(2026, 7, 10, 2, 0, tzinfo=UTC)

    runtime = advance_life_runtime(store, "geoff", MoodState(), now=now)

    assert runtime.ends_at > now
    assert store.get_life_runtime("geoff").activity == runtime.activity


def test_expired_activity_is_closed_before_the_next_one_starts(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    now = datetime(2026, 7, 10, 2, 0, tzinfo=UTC)
    first = advance_life_runtime(store, "geoff", MoodState(), now=now)

    second = advance_life_runtime(store, "geoff", MoodState(), now=first.ends_at + timedelta(seconds=1))

    events = store.recent_life_events("geoff")
    assert second.started_at >= first.ends_at
    assert any(event["status"] == "completed" for event in events)


def test_daily_plan_is_stable_and_its_items_are_not_lived_facts_until_activated(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    morning = datetime(2026, 7, 10, 2, 0, tzinfo=UTC)  # 10:00 in Shanghai

    first = advance_life_runtime(store, "geoff", MoodState(), now=morning)
    same_activity = advance_life_runtime(store, "geoff", MoodState(), now=morning + timedelta(minutes=10))

    assert first.activity == same_activity.activity
    assert first.started_at == same_activity.started_at
    # The ledger only records the current activity, not every planned future slot.
    assert len(store.recent_life_events("geoff", limit=20)) == 1


def test_current_runtime_exposes_the_planned_slot_bounds_not_a_stale_random_duration(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    now = datetime(2026, 7, 10, 12, 30, tzinfo=UTC)  # 20:30 in Shanghai

    runtime = advance_life_runtime(store, "geoff", MoodState(), now=now)

    assert runtime.started_at == datetime(2026, 7, 10, 10, 0, tzinfo=UTC)
    assert runtime.ends_at == datetime(2026, 7, 10, 14, 0, tzinfo=UTC)


def test_salient_user_event_nudges_future_plan_without_rewriting_current_activity(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    now = datetime(2026, 7, 10, 2, 0, tzinfo=UTC)
    current = advance_life_runtime(store, "geoff", MoodState(), now=now)

    apply_user_event_to_life_runtime(
        store,
        "geoff",
        event_kind="user_vulnerable",
        message=IncomingMessage(platform="qq", platform_user_id="geoff", text="我现在真的好难受"),
        state=MoodState(),
        now=now + timedelta(minutes=1),
    )
    later = store.life_day_plan_item_at("geoff", current.ends_at + timedelta(minutes=1))

    assert store.get_life_runtime("geoff").activity == current.activity
    assert later is not None
    assert later["adjustment_note"] == "听见你难受后的余波"


def test_time_travel_across_a_day_keeps_continuity_and_completes_only_elapsed_activities(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    morning = datetime(2026, 7, 10, 0, 30, tzinfo=UTC)  # 08:30 Shanghai

    first = advance_life_runtime(store, "geoff", MoodState(), now=morning)
    evening = advance_life_runtime(store, "geoff", MoodState(), now=morning + timedelta(hours=12))
    events = store.recent_life_events("geoff", limit=20)

    assert first.activity_kind == "morning"
    assert evening.activity_kind in {"unwind", "friends"}
    assert any(event["status"] == "completed" for event in events)
    # Planned slots remain internal. An entered activity may additionally leave one
    # small private event, but no future plan item becomes a lived fact.
    assert len([event for event in events if event["kind"] != "private_life_event"]) == 2


def test_durable_state_changes_the_next_days_private_plan(tmp_path: Path) -> None:
    sleepy_store = CompanionStore(tmp_path / "sleepy.sqlite")
    guarded_store = CompanionStore(tmp_path / "guarded.sqlite")
    seed_user(sleepy_store)
    seed_user(guarded_store)
    morning = datetime(2026, 7, 11, 2, 0, tzinfo=UTC)  # 10:00 Shanghai
    evening = datetime(2026, 7, 11, 12, 0, tzinfo=UTC)  # 20:00 Shanghai

    sleepy = advance_life_runtime(sleepy_store, "geoff", MoodState(mood="sleepy"), now=morning)
    guarded = advance_life_runtime(
        guarded_store, "geoff", MoodState(mood="guarded", boundary_level=55), now=evening
    )

    assert sleepy.activity_kind == "study"
    assert "精神不太够" in sleepy.activity
    assert guarded.activity_kind == "unwind"
    assert "一个人安静" in guarded.activity


def test_non_user_life_event_result_changes_future_plan_without_claiming_it_happened(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    now = datetime(2026, 7, 10, 2, 0, tzinfo=UTC)
    current = advance_life_runtime(store, "geoff", MoodState(), now=now)

    runtime = apply_life_event_result(
        store,
        "geoff",
        event_kind="class_cancelled",
        state=MoodState(),
        now=now + timedelta(minutes=10),
    )
    later = store.life_day_plan_item_at("geoff", current.ends_at + timedelta(minutes=1))
    events = store.recent_life_events("geoff", limit=10)

    assert runtime.activity == current.activity
    assert later is not None
    assert later["adjustment_note"] == "临时空出来的时间"
    assert any(event["kind"] == "life_event_result" and "临时空出来" in event["content"] for event in events)
    assert not any(event["kind"] == "private_life_event" for event in events)


def test_daily_life_result_plan_is_stable_and_most_days_have_none() -> None:
    plans = [
        plan_daily_life_result("geoff", f"2026-07-{day:02d}")
        for day in range(1, 29)
    ]

    assert plans == [
        plan_daily_life_result("geoff", f"2026-07-{day:02d}")
        for day in range(1, 29)
    ]
    assert any(plan is None for plan in plans)
    assert any(plan is not None for plan in plans)


def test_planned_life_result_fires_once_and_bends_the_day(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    local_tz = datetime.now().astimezone().tzinfo
    applied = None
    applied_at = None
    for day_offset in range(0, 120):
        local_day = datetime(2026, 7, 1, tzinfo=local_tz) + timedelta(days=day_offset)
        plan = plan_daily_life_result("geoff", local_day.date().isoformat())
        if not plan:
            continue
        _, planned_hour = plan
        moment = local_day.replace(hour=planned_hour, minute=30)
        candidate = maybe_apply_planned_life_result(store, "geoff", MoodState(), now=moment)
        if candidate is not None:
            applied = candidate
            applied_at = moment
            break

    assert applied is not None, "expected at least one applicable planned life result in 120 days"
    assert applied.user_event_effect
    events = store.recent_life_events("geoff", limit=20)
    assert any(
        event["kind"] == "life_event_result"
        and str(event["source"]).startswith("life_result:")
        and applied_at.date().isoformat() in str(event["source"])
        for event in events
    )
    # The same tick later in the day must not stack the aftermath twice.
    assert maybe_apply_planned_life_result(store, "geoff", MoodState(), now=applied_at + timedelta(minutes=20)) is None


def test_busy_activity_leaves_ordinary_message_unread_then_second_message_wakes_her(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    now = datetime(2026, 7, 10, 2, 0, tzinfo=UTC)
    store.save_life_runtime(
        "geoff",
        LifeRuntimeState(
            activity="正在上课，手机放在包里",
            activity_kind="class",
            attention_demand=88,
            interruptible=False,
            started_at=now - timedelta(minutes=10),
            ends_at=now + timedelta(minutes=35),
            phone_attention="away",
            updated_at=now,
        ),
    )
    first = decide_phone_attention(
        store,
        "geoff",
        IncomingMessage(platform="qq", platform_user_id="geoff", text="我到啦"),
        MoodState(),
        now=now,
    )
    second = decide_phone_attention(
        store,
        "geoff",
        IncomingMessage(platform="qq", platform_user_id="geoff", text="你在吗"),
        MoodState(),
        now=now + timedelta(seconds=30),
    )

    assert first.read_now is False
    assert first.defer_minutes
    assert second.read_now is True
    assert store.get_life_runtime("geoff").phone_attention == "reading"


def test_existing_runtime_gets_a_future_plan_after_restart(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    now = datetime(2026, 7, 10, 2, 0, tzinfo=UTC)
    store.save_life_runtime(
        "geoff",
        LifeRuntimeState(
            activity="在图书馆看书",
            activity_kind="study",
            attention_demand=60,
            interruptible=True,
            started_at=now - timedelta(minutes=10),
            ends_at=now + timedelta(minutes=40),
            updated_at=now,
        ),
    )

    advance_life_runtime(store, "geoff", MoodState(), now=now)

    assert store.upcoming_life_plan_items("geoff", now=now)


def test_mark_phone_read_clears_notification_count(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    now = datetime(2026, 7, 10, 2, 0, tzinfo=UTC)
    store.save_life_runtime(
        "geoff",
        LifeRuntimeState(
            activity="在自习",
            activity_kind="study",
            attention_demand=60,
            interruptible=True,
            started_at=now,
            ends_at=now + timedelta(minutes=40),
            phone_attention="notified",
            notification_count=1,
            updated_at=now,
        ),
    )

    marked = mark_phone_read(store, "geoff", now=now + timedelta(minutes=2))

    assert marked.phone_attention == "reading"
    assert marked.notification_count == 0


def test_sent_reply_returns_phone_to_current_activity(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    now = datetime(2026, 7, 10, 2, 0, tzinfo=UTC)
    store.save_life_runtime(
        "geoff",
        LifeRuntimeState(
            activity="在自习",
            activity_kind="study",
            attention_demand=60,
            interruptible=True,
            started_at=now,
            ends_at=now + timedelta(minutes=40),
            phone_attention="typing",
            updated_at=now,
        ),
    )

    idle = mark_phone_idle(store, "geoff", now=now + timedelta(seconds=12))

    assert idle.phone_attention == "away"


def test_high_focus_question_can_wait_but_emotional_message_breaks_through(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    now = datetime(2026, 7, 10, 2, 0, tzinfo=UTC)
    store.save_life_runtime(
        "geoff",
        LifeRuntimeState(
            activity="正在上课，手机放在包里",
            activity_kind="class",
            attention_demand=90,
            interruptible=False,
            started_at=now,
            ends_at=now + timedelta(minutes=40),
            phone_attention="away",
            updated_at=now,
        ),
    )

    question = decide_phone_attention(
        store, "geoff", IncomingMessage(platform="qq", platform_user_id="geoff", text="你晚点有空吗？"), MoodState(), now=now
    )
    vulnerable = decide_phone_attention(
        store, "geoff", IncomingMessage(platform="qq", platform_user_id="geoff", text="我现在真的好难受"), MoodState(), now=now + timedelta(seconds=10)
    )

    assert question.read_now is False
    assert vulnerable.read_now is True


def test_existing_unread_message_wakes_her_for_the_next_ordinary_message(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    now = datetime(2026, 7, 10, 18, 0, tzinfo=UTC)
    store.save_life_runtime(
        "geoff",
        LifeRuntimeState(
            activity="夜里半醒，没一直盯着聊天框",
            activity_kind="quiet",
            base_attention_demand=66,
            attention_demand=66,
            interruptible=True,
            started_at=now - timedelta(minutes=10),
            ends_at=now + timedelta(minutes=40),
            phone_attention="away",
            notification_count=0,
            updated_at=now,
        ),
    )

    decision = decide_phone_attention(
        store,
        "geoff",
        IncomingMessage(platform="qq", platform_user_id="geoff", text="我还养死过仙人掌"),
        MoodState(has_unread=True),
        now=now,
    )

    assert decision.read_now is True


def test_quiet_unread_delay_is_bounded_to_five_minutes(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    now = datetime(2026, 7, 10, 18, 0, tzinfo=UTC)
    store.save_life_runtime(
        "geoff",
        LifeRuntimeState(
            activity="夜里半醒，没一直盯着聊天框",
            activity_kind="quiet",
            base_attention_demand=66,
            attention_demand=66,
            interruptible=True,
            started_at=now - timedelta(minutes=10),
            ends_at=now + timedelta(minutes=40),
            phone_attention="away",
            updated_at=now,
        ),
    )

    decision = decide_phone_attention(
        store,
        "geoff",
        IncomingMessage(platform="qq", platform_user_id="geoff", text="我今晚在 B 站看了好多猫视频"),
        MoodState(),
        now=now,
    )

    assert decision.read_now is False
    assert decision.defer_minutes is not None
    assert decision.defer_minutes <= 5


def test_sleeping_runtime_blocks_unprompted_outreach() -> None:
    runtime = LifeRuntimeState(activity="已经睡着", activity_kind="sleep", attention_demand=92, interruptible=False)

    allowed, reason = proactive_outreach_allowed(runtime)

    assert allowed is False
    assert "睡着" in reason


def test_user_vulnerability_changes_her_current_life_attention(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    now = datetime(2026, 7, 10, 2, 0, tzinfo=UTC)
    store.save_life_runtime(
        "geoff",
        LifeRuntimeState(
            activity="在图书馆看书",
            activity_kind="study",
            base_attention_demand=70,
            attention_demand=70,
            interruptible=True,
            started_at=now,
            ends_at=now + timedelta(minutes=50),
            phone_attention="away",
            updated_at=now,
        ),
    )

    updated = apply_user_event_to_life_runtime(
        store,
        "geoff",
        event_kind="user_vulnerable",
        message=IncomingMessage(platform="qq", platform_user_id="geoff", text="我现在真的好难受"),
        state=MoodState(),
        now=now,
    )

    assert "挂心" in (updated.user_event_effect or "")
    assert updated.attention_demand < 70
    assert updated.phone_attention == "glanced"
    assert any(event["kind"] == "user_influence" for event in store.recent_life_events("geoff"))


def test_durable_boundary_and_responsiveness_state_changes_daily_attention(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    now = datetime(2026, 7, 10, 2, 0, tzinfo=UTC)
    store.save_life_runtime(
        "geoff",
        LifeRuntimeState(
            activity="在图书馆看书",
            activity_kind="study",
            base_attention_demand=60,
            attention_demand=60,
            interruptible=True,
            started_at=now,
            ends_at=now + timedelta(minutes=50),
            phone_attention="away",
            updated_at=now,
        ),
    )

    updated = synchronize_life_runtime(
        store,
        "geoff",
        MoodState(mood="guarded", boundary_level=50, perceived_responsiveness=25),
        now=now,
    )

    assert updated.attention_demand > 60
    assert updated.phone_attention == "do_not_disturb"
    assert "边界感" in (updated.state_effect or "")

    restored = synchronize_life_runtime(store, "geoff", MoodState(), now=now + timedelta(minutes=2))

    assert restored.phone_attention == "away"
    assert restored.state_effect is None

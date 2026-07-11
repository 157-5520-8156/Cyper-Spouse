"""Temporal ledger projection over plans and lived life events."""
from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import re

from companion_daemon.life_runtime import ensure_calendar_window
from companion_daemon.models import MoodState
from companion_daemon.time import utc_now


def calendar_ledger(store, canonical_user_id: str, state: MoodState, *, now: datetime | None = None, past_days: int = 7, future_days: int = 7) -> dict[str, object]:
    now = now or utc_now()
    ensure_calendar_window(store, canonical_user_id, state, now=now, future_days=future_days)
    _ensure_weekly_plan(store, canonical_user_id, now=now, future_days=future_days)
    start = (now - timedelta(days=past_days)).astimezone()
    end = (now + timedelta(days=future_days + 1)).astimezone()
    plans = store.life_plan_items_between(canonical_user_id, starts_at=start, ends_at=end)
    events = store.life_events_between(canonical_user_id, starts_at=start, ends_at=end)
    _backfill_memorable_events(store, canonical_user_id, events)
    special_events = store.calendar_events_between(canonical_user_id, starts_at=start, ends_at=end)
    by_day: dict[str, dict[str, object]] = {}
    for offset in range(-past_days, future_days + 1):
        day = (now.astimezone() + timedelta(days=offset)).date().isoformat()
        by_day[day] = {"date": day, "relative": _relative_day(offset), "plans": [], "events": [], "special_events": []}
    for row in plans:
        day = str(row["local_date"])
        if day in by_day:
            by_day[day]["plans"].append(dict(row))
    for row in events:
        day = datetime.fromisoformat(str(row["started_at"])).astimezone().date().isoformat()
        if day in by_day:
            by_day[day]["events"].append(dict(row))
    for row in special_events:
        start_day = datetime.fromisoformat(str(row["starts_at"])).astimezone().date()
        end_day = datetime.fromisoformat(str(row["ends_at"])).astimezone().date()
        cursor = start_day
        while cursor <= end_day:
            day = cursor.isoformat()
            if day in by_day:
                by_day[day]["special_events"].append(dict(row))
            cursor += timedelta(days=1)
    return {"now": now.isoformat(), "days": list(by_day.values())}


def _backfill_memorable_events(store, canonical_user_id: str, events) -> None:
    """Promote real, memorable lived events into calendar entries once."""
    for row in events:
        if row["kind"] not in {"private_life_event", "life_event_result"} or row["status"] != "completed":
            continue
        source = f"calendar:backfill:{row['id']}"
        if store.calendar_event_by_source(canonical_user_id, source):
            continue
        content = str(row["content"])
        title = content.split(":", 1)[0][:28] if ":" in content else content[:28]
        store.create_calendar_event(
            canonical_user_id,
            title=title or "一件生活小事",
            event_type="lived_memory",
            starts_at=datetime.fromisoformat(str(row["started_at"])),
            ends_at=datetime.fromisoformat(str(row["ends_at"])) + timedelta(minutes=1),
            importance=72 if row["shared_at"] else 58,
            source=source,
            details=content[:300],
            memory_note=content[:300],
            status="completed",
        )


def _ensure_weekly_plan(store, canonical_user_id: str, *, now: datetime, future_days: int) -> None:
    """Create a small coherent weekly plan, not independent daily impulses."""
    local = now.astimezone()
    store.cancel_elapsed_calendar_plans(canonical_user_id, now=now)
    # One-time migration from the prototype's independent highlights.  Weekly
    # events are the new authority, so keeping both would make the calendar
    # look like it scheduled the same outing twice.
    store.delete_calendar_events_by_source_prefix(canonical_user_id, "calendar:highlight:")
    monday = (local - timedelta(days=local.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    week_key = monday.date().isoformat()
    ratio = _ratio(canonical_user_id, "weekly-plan", week_key)
    themes = (
        ("把暑假过得松一点", "给阅读、散步和朋友留一点空白，不把每一天排满。"),
        ("慢慢整理自己的东西", "这周更偏向看书、整理照片和做一点小创作。"),
        ("出去见见人", "这周有一两个轻松的见面或出行安排，其余时间留给自己。"),
    )
    theme, summary = themes[min(len(themes) - 1, int(ratio * len(themes)))]
    store.save_calendar_week(canonical_user_id, week_start=week_key, theme=theme, summary=summary, source="calendar:weekly")
    candidates = (
        ("整理暑假阅读清单", "personal_plan", 48, 0, 1, "挑一个下午整理想读的书和笔记。"),
        ("和朋友约着看一个小展", "social_plan", 68, 2, 1, "朋友约了个不太正式的小展，时间可以调整。"),
        ("去附近拍一段傍晚的光", "creative_plan", 55, 4, 1, "想趁傍晚出去走走，拍几张照片。"),
        ("嘉兴回家住两天", "trip", 82, 4, 3, "准备回家看看，跨日安排会占住周末。"),
    )
    count = 1 if ratio < 0.44 else 2
    for index in range(count):
        title, event_type, importance, day_offset, days, details = candidates[(int(ratio * 100) + index) % len(candidates)]
        day = (monday + timedelta(days=day_offset)).replace(hour=15 + index, minute=0)
        if day <= local:
            day = (local + timedelta(days=1 + index)).replace(hour=15 + index, minute=0, second=0, microsecond=0)
        source = f"calendar:weekly:{week_key}:{index}"
        if store.calendar_event_by_source(canonical_user_id, source):
            continue
        store.create_calendar_event(
            canonical_user_id,
            title=title,
            event_type=event_type,
            starts_at=day,
            ends_at=day + timedelta(days=days, hours=2),
            importance=importance,
            source=source,
            details=details,
            memory_note=details,
        )


def _ratio(*parts: str) -> float:
    return int(hashlib.sha256("|".join(parts).encode()).hexdigest()[:8], 16) / 0xFFFFFFFF


def calendar_context_for_message(store, canonical_user_id: str, state: MoodState, text: str, *, now: datetime | None = None) -> str | None:
    now = now or utc_now()
    target = _target_day(text, now)
    if target is None:
        return None
    ledger = calendar_ledger(store, canonical_user_id, state, now=now, past_days=10, future_days=10)
    day = next((item for item in ledger["days"] if item["date"] == target.date().isoformat()), None)
    if not day:
        return None
    asks_future = target.date() > now.astimezone().date()
    if asks_future:
        special = [item for item in day["special_events"] if item["status"] in {"planned", "active"}]
        plans = special or day["plans"]
        if not plans:
            return f"时间账本：{day['relative']}没有已排定的计划；不能把它说成已经发生。"
        lines = "；".join(str(item.get("title") or item.get("activity")) for item in plans[:4])
        return f"时间账本：用户在问{day['relative']}的安排。仅可依据计划回答：{lines}。计划允许变化，别说成已发生。"
    special_done = [item for item in day["special_events"] if item["status"] == "completed"]
    events = special_done or [item for item in day["events"] if item["status"] == "completed"]
    if not events:
        return f"时间账本：{day['relative']}没有已发生记录。不要编具体经历；可以坦白记不清或只说没有留到记录。"
    lines = "；".join(str(item.get("memory_note") or item.get("title") or item.get("content")) for item in events[:4])
    return f"时间账本：用户在问{day['relative']}已经发生的事。仅可依据已发生记录回答：{lines}。"


def _target_day(text: str, now: datetime) -> datetime | None:
    local = now.astimezone()
    compact = re.sub(r"\s+", "", text)
    offsets = {"今天": 0, "明天": 1, "后天": 2, "昨天": -1, "前天": -2}
    for token, offset in offsets.items():
        if token in compact:
            return local + timedelta(days=offset)
    match = re.search(r"上周([一二三四五六日天])", compact)
    if match:
        weekday = "一二三四五六日".index("日" if match.group(1) == "天" else match.group(1))
        return local - timedelta(days=local.weekday() + 7 - weekday)
    return None


def _relative_day(offset: int) -> str:
    return {0: "今天", 1: "明天", 2: "后天", -1: "昨天", -2: "前天"}.get(offset, "上周" if offset < 0 else "未来几天")

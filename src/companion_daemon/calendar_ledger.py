"""Temporal ledger projection over plans and lived life events."""
from __future__ import annotations

from datetime import datetime, timedelta
import re

from companion_daemon.life_runtime import ensure_calendar_window
from companion_daemon.models import MoodState
from companion_daemon.time import utc_now


def calendar_ledger(store, canonical_user_id: str, state: MoodState, *, now: datetime | None = None, past_days: int = 7, future_days: int = 7) -> dict[str, object]:
    now = now or utc_now()
    ensure_calendar_window(store, canonical_user_id, state, now=now, future_days=future_days)
    start = (now - timedelta(days=past_days)).astimezone()
    end = (now + timedelta(days=future_days + 1)).astimezone()
    plans = store.life_plan_items_between(canonical_user_id, starts_at=start, ends_at=end)
    events = store.life_events_between(canonical_user_id, starts_at=start, ends_at=end)
    by_day: dict[str, dict[str, object]] = {}
    for offset in range(-past_days, future_days + 1):
        day = (now.astimezone() + timedelta(days=offset)).date().isoformat()
        by_day[day] = {"date": day, "relative": _relative_day(offset), "plans": [], "events": []}
    for row in plans:
        day = str(row["local_date"])
        if day in by_day:
            by_day[day]["plans"].append(dict(row))
    for row in events:
        day = datetime.fromisoformat(str(row["started_at"])).astimezone().date().isoformat()
        if day in by_day:
            by_day[day]["events"].append(dict(row))
    return {"now": now.isoformat(), "days": list(by_day.values())}


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
        plans = day["plans"]
        if not plans:
            return f"时间账本：{day['relative']}没有已排定的计划；不能把它说成已经发生。"
        lines = "；".join(str(item["activity"]) for item in plans[:4])
        return f"时间账本：用户在问{day['relative']}的安排。仅可依据计划回答：{lines}。计划允许变化，别说成已发生。"
    events = [item for item in day["events"] if item["status"] == "completed"]
    if not events:
        return f"时间账本：{day['relative']}没有已发生记录。不要编具体经历；可以坦白记不清或只说没有留到记录。"
    lines = "；".join(str(item["content"]) for item in events[:4])
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

"""A small, deterministic private-life runtime for the companion daemon."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hashlib

from companion_daemon.models import IncomingMessage, LifeRuntimeState, MoodState
from companion_daemon.reply_decision import classify_message
from companion_daemon.social_followups import create_life_share_followup
from companion_daemon.time import utc_now


@dataclass(frozen=True)
class PhoneDecision:
    read_now: bool
    defer_minutes: float | None
    reason: str


@dataclass(frozen=True)
class ActivityTemplate:
    kind: str
    activity: str
    attention_demand: int
    interruptible: bool
    duration_minutes: tuple[int, int]


_TEMPLATES: dict[str, tuple[ActivityTemplate, ...]] = {
    "morning_focus": (
        ActivityTemplate("class", "上课或自习，手机调成了静音放在旁边", 78, False, (35, 85)),
        ActivityTemplate("study", "在图书馆看书，偶尔会瞄一眼手机", 62, True, (30, 70)),
    ),
    "lunch_break": (
        ActivityTemplate("meal", "吃饭或排队买饮料，手机在手边", 28, True, (20, 45)),
        ActivityTemplate("walk", "在校园里慢慢走一段路", 24, True, (20, 40)),
    ),
    "afternoon_classes": (
        ActivityTemplate("class", "下午上课或自习，注意力被手头的事占着", 72, False, (35, 90)),
        ActivityTemplate("errand", "在处理一点自己的小事，手机不总在手里", 50, True, (25, 55)),
    ),
    "evening_unwind": (
        ActivityTemplate("unwind", "收拾一天的东西，心里比较松一点", 25, True, (30, 75)),
        ActivityTemplate("friends", "和同学待一会儿，消息会晚点看", 48, True, (35, 90)),
    ),
    "late_evening": (
        ActivityTemplate("routine", "洗漱、整理或窝着看点东西", 38, True, (25, 60)),
        ActivityTemplate("study", "安静补一会儿自己的事情", 58, True, (30, 75)),
    ),
    "deep_night": (
        ActivityTemplate("sleep", "已经睡着或把手机放远了", 92, False, (55, 150)),
        ActivityTemplate("quiet", "夜里半醒，没一直盯着聊天框", 66, True, (25, 65)),
    ),
    "early_morning": (
        ActivityTemplate("morning", "刚醒，慢慢收拾今天要做什么", 55, True, (20, 50)),
    ),
}

_DAY_SLOTS: tuple[tuple[str, int, int, str], ...] = (
    ("deep_night", 0, 7, "deep_night"),
    ("early_morning", 7, 9, "early_morning"),
    ("morning_focus", 9, 12, "morning_focus"),
    ("lunch_break", 12, 14, "lunch_break"),
    ("afternoon_classes", 14, 18, "afternoon_classes"),
    ("evening_unwind", 18, 22, "evening_unwind"),
    ("late_evening", 22, 24, "late_evening"),
)

_INCIDENTAL_EVENT_TEMPLATES: dict[str, tuple[str, ...]] = {
    "class": ("课间听见后排有人把一个词念错，憋笑憋得有点辛苦。",),
    "study": ("看书时翻到一段有点好笑的注释，停下来发了会儿呆。",),
    "meal": ("买东西时前面的人点单特别犹豫，队伍慢慢往前挪。",),
    "walk": ("路上风有点大，路边的宣传单被吹得到处跑。",),
    "errand": ("办自己的小事时排了一会儿队，顺手把待办划掉了一项。",),
    "friends": ("和同学聊到一个很离谱的小话题，后来还在回想。",),
    "unwind": ("收拾东西时发现一件差点忘掉的小物，心里忽然松了一下。",),
    "routine": ("洗漱前把桌面理了一点，终于没那么乱了。",),
    "quiet": ("夜里醒了一下，盯着窗外发了会儿呆又把手机放下。",),
    "morning": ("刚醒时差点把一件小事忘掉，后来慢慢想起来了。",),
}


def advance_life_runtime(store, canonical_user_id: str, state: MoodState, *, now: datetime | None = None) -> LifeRuntimeState:
    now = now or utc_now()
    # Existing runtimes may predate the daily-plan feature or survive a restart.
    # Keep the present activity intact, but always restore the future schedule.
    _ensure_daily_plan(store, canonical_user_id, now, state)
    current = store.get_life_runtime(canonical_user_id)
    if current and current.ends_at > now:
        if current.user_event_effect_until and current.user_event_effect_until <= now:
            current = current.model_copy(
                update={
                    "user_event_effect": None,
                    "user_event_effect_until": None,
                    "user_event_attention_delta": 0,
                    "updated_at": now,
                }
            )
            store.save_life_runtime(canonical_user_id, current)
        if (
            current.phone_attention == "notified"
            and current.interruptible
            and current.last_notification_at
            and (now - current.last_notification_at).total_seconds() >= _glance_after_seconds(current)
        ):
            current = current.model_copy(update={"phone_attention": "glanced", "updated_at": now})
            store.save_life_runtime(canonical_user_id, current)
        return current
    if current:
        _record_incidental_event(store, canonical_user_id, current)
        store.complete_active_life_events(canonical_user_id, completed_at=now)
    store.update_life_day_plan_status(canonical_user_id, before=now, status="completed")
    planned = store.life_day_plan_item_at(canonical_user_id, now)
    if planned is None:
        # Defensive fallback for clock/timezone edge cases. It still uses the same templates.
        template = _choose_template(canonical_user_id, now)
        duration = _stable_duration(canonical_user_id, template, now)
        starts_at, ends_at = now, now + timedelta(minutes=duration)
    else:
        template = ActivityTemplate(
            planned["kind"], planned["activity"], planned["attention_demand"], bool(planned["interruptible"]), (1, 1)
        )
        starts_at = datetime.fromisoformat(planned["starts_at"])
        ends_at = datetime.fromisoformat(planned["ends_at"])
        store.activate_life_day_plan_item(planned["id"])
    runtime = LifeRuntimeState(
        activity=template.activity,
        activity_kind=template.kind,
        base_attention_demand=template.attention_demand,
        attention_demand=template.attention_demand,
        interruptible=template.interruptible,
        started_at=starts_at,
        ends_at=ends_at,
        phone_attention="away",
        updated_at=now,
    )
    store.save_life_runtime(canonical_user_id, runtime)
    store.record_life_event(
        canonical_user_id,
        kind=template.kind,
        content=template.activity,
        started_at=runtime.started_at,
        ends_at=runtime.ends_at,
        status="active",
        source="life_runtime",
    )
    return runtime


def _ensure_daily_plan(store, canonical_user_id: str, now: datetime, state: MoodState) -> None:
    local = now.astimezone()
    local_date = local.date().isoformat()
    if store.has_life_day_plan(canonical_user_id, local_date):
        return
    items: list[dict[str, object]] = []
    for slot, start_hour, end_hour, phase in _DAY_SLOTS:
        template = _adapt_template_to_state(
            _choose_template_for_phase(canonical_user_id, phase, local_date), phase, state
        )
        start = local.replace(hour=start_hour, minute=0, second=0, microsecond=0)
        if end_hour == 24:
            end = (start + timedelta(days=1)).replace(hour=0)
        else:
            end = local.replace(hour=end_hour, minute=0, second=0, microsecond=0)
        items.append(
            {
                "slot": slot,
                "kind": template.kind,
                "activity": template.activity,
                "attention_demand": template.attention_demand,
                "interruptible": template.interruptible,
                # SQLite compares the ISO timestamp strings in the lookup; keep that
                # storage representation in UTC even though plan selection is local-time.
                "starts_at": start.astimezone(UTC).isoformat(),
                "ends_at": end.astimezone(UTC).isoformat(),
            }
        )
    store.save_life_day_plan(canonical_user_id, local_date, items)


def _adapt_template_to_state(template: ActivityTemplate, phase: str, state: MoodState) -> ActivityTemplate:
    """Carry durable state into tomorrow's plan without claiming a new event happened."""
    if state.mood == "sleepy" and phase in {"morning_focus", "afternoon_classes"}:
        return ActivityTemplate(
            "study",
            "精神不太够，把高专注的事拆开慢慢做，手机放在手边",
            min(template.attention_demand, 52),
            True,
            template.duration_minutes,
        )
    if (state.mood in {"hurt", "guarded"} or state.boundary_level >= 45) and phase == "evening_unwind":
        return ActivityTemplate(
            "unwind",
            "晚上想一个人安静待着，先按自己的节奏收尾",
            max(template.attention_demand, 42),
            True,
            template.duration_minutes,
        )
    if state.emotional_charge >= 45 and phase == "late_evening":
        return ActivityTemplate(
            "routine",
            "心里还有点乱，先做些不用动太多脑子的收拾和洗漱",
            max(template.attention_demand, 48),
            True,
            template.duration_minutes,
        )
    return template


def _record_incidental_event(store, canonical_user_id: str, runtime: LifeRuntimeState) -> None:
    """Let a completed activity occasionally leave a small private trace.

    The event is recorded at transition time, never retrofitted because a later
    proactive message needs material. The stable gate prevents a diary entry for
    every block of the day while remaining deterministic across daemon restarts.
    """
    choices = _INCIDENTAL_EVENT_TEMPLATES.get(runtime.activity_kind)
    if not choices:
        return
    event_key = runtime.started_at.astimezone(UTC).isoformat()
    source = f"life_runtime:incidental:{event_key}"
    if store.life_event_by_source(canonical_user_id, source):
        return
    ratio = _stable_ratio(canonical_user_id, runtime.activity_kind, event_key)
    if ratio > 0.34:
        return
    index = min(len(choices) - 1, int(ratio * len(choices)))
    content = choices[index]
    event_id = store.record_life_event(
        canonical_user_id,
        kind="private_life_event",
        content=content,
        started_at=runtime.ends_at,
        ends_at=runtime.ends_at,
        status="completed",
        source=source,
    )
    store.upsert_memory(
        canonical_user_id,
        kind="private_life_event",
        content=content,
        source=source,
        confidence=0.74,
    )
    create_life_share_followup(
        store,
        canonical_user_id,
        life_event_id=event_id,
        content=content,
    )


def decide_phone_attention(
    store,
    canonical_user_id: str,
    message: IncomingMessage,
    state: MoodState,
    *,
    now: datetime | None = None,
) -> PhoneDecision:
    now = now or utc_now()
    runtime = advance_life_runtime(store, canonical_user_id, state, now=now)
    message_type = classify_message(message.text, has_attachments=bool(message.attachments))
    urgent = message_type in {"urgent", "emotional", "nonverbal_share"}
    notifications = runtime.notification_count + 1
    user_is_asking = message_type == "question"
    carrying_unread = state.has_unread or (
        runtime.notification_count > 0 and runtime.phone_attention in {"notified", "glanced"}
    )
    # One missed notification can feel ordinary. Repeating it through an active
    # chat feels like the daemon is stuck, especially after task coalescing.
    recently_left_unread = store.has_recent_unread_deferral(
        canonical_user_id,
        since=now - timedelta(minutes=20),
    )
    emotionally_withdrawing = state.mood in {"hurt", "guarded"} or state.boundary_level >= 45
    should_read = (
        urgent
        or carrying_unread
        or recently_left_unread
        or notifications >= 2
        or (user_is_asking and runtime.interruptible and runtime.attention_demand <= 65)
        or (runtime.interruptible and runtime.attention_demand <= 45 and not emotionally_withdrawing)
    )
    if should_read:
        updated = runtime.model_copy(
            update={
                "phone_attention": "reading",
                "notification_count": notifications,
                "last_notification_at": now,
                "last_read_at": now,
                "updated_at": now,
            }
        )
        store.save_life_runtime(canonical_user_id, updated)
        return PhoneDecision(True, None, "notification_read_now")

    minutes_to_end = max(1.0, (runtime.ends_at - now).total_seconds() / 60)
    max_wait = 5.0 if runtime.activity_kind == "quiet" else 8.0 if runtime.interruptible else 22.0
    if user_is_asking:
        max_wait = min(max_wait, 11.0)
    if emotionally_withdrawing:
        max_wait = min(minutes_to_end, max_wait + 5.0)
    defer_minutes = min(minutes_to_end, max_wait)
    updated = runtime.model_copy(
        update={
            "phone_attention": "do_not_disturb" if emotionally_withdrawing else "notified",
            "notification_count": notifications,
            "last_notification_at": now,
            "updated_at": now,
        }
    )
    store.save_life_runtime(canonical_user_id, updated)
    reason = "boundary_pause" if emotionally_withdrawing else f"unread_during_{runtime.activity_kind}"
    return PhoneDecision(False, defer_minutes, reason)


def mark_phone_read(store, canonical_user_id: str, *, now: datetime | None = None) -> LifeRuntimeState | None:
    runtime = store.get_life_runtime(canonical_user_id)
    if not runtime:
        return None
    now = now or utc_now()
    updated = runtime.model_copy(
        update={"phone_attention": "reading", "notification_count": 0, "last_read_at": now, "updated_at": now}
    )
    store.save_life_runtime(canonical_user_id, updated)
    return updated


def mark_phone_typing(store, canonical_user_id: str, *, now: datetime | None = None) -> LifeRuntimeState | None:
    runtime = mark_phone_read(store, canonical_user_id, now=now)
    if not runtime:
        return None
    updated = runtime.model_copy(update={"phone_attention": "typing", "updated_at": now or utc_now()})
    store.save_life_runtime(canonical_user_id, updated)
    return updated


def mark_phone_idle(store, canonical_user_id: str, *, now: datetime | None = None) -> LifeRuntimeState | None:
    runtime = store.get_life_runtime(canonical_user_id)
    if not runtime:
        return None
    now = now or utc_now()
    updated = runtime.model_copy(update={"phone_attention": "away", "updated_at": now})
    store.save_life_runtime(canonical_user_id, updated)
    return updated


def apply_user_event_to_life_runtime(
    store,
    canonical_user_id: str,
    *,
    event_kind: str,
    message: IncomingMessage,
    state: MoodState,
    now: datetime | None = None,
) -> LifeRuntimeState:
    """Let a salient user event alter today's private rhythm for a limited time."""
    now = now or utc_now()
    runtime = advance_life_runtime(store, canonical_user_id, state, now=now)
    effect = _effect_for_user_event(canonical_user_id, event_kind, message, now)
    if not effect:
        return runtime
    effect_text, minutes, attention_delta, phone_attention = effect
    updated = runtime.model_copy(
        update={
            "phone_attention": phone_attention or runtime.phone_attention,
            "user_event_effect": effect_text,
            "user_event_effect_until": now + timedelta(minutes=minutes),
            "user_event_attention_delta": attention_delta,
            "updated_at": now,
        }
    )
    store.save_life_runtime(canonical_user_id, updated)
    store.record_life_event(
        canonical_user_id,
        kind="user_influence",
        content=effect_text,
        started_at=now,
        ends_at=updated.user_event_effect_until or now,
        status="active",
        source=f"interaction:{event_kind}",
    )
    _nudge_future_plan(store, canonical_user_id, event_kind=event_kind, now=now)
    return synchronize_life_runtime(store, canonical_user_id, state, now=now)


def apply_life_event_result(
    store,
    canonical_user_id: str,
    *,
    event_kind: str,
    state: MoodState,
    now: datetime | None = None,
    source: str | None = None,
) -> LifeRuntimeState:
    """Let a private non-user event bend the day without becoming shared history."""
    now = now or utc_now()
    runtime = advance_life_runtime(store, canonical_user_id, state, now=now)
    result = _life_event_result_effect(event_kind)
    if not result:
        return runtime
    effect_text, minutes, attention_delta, future_activity, future_note, future_delta = result
    updated = runtime.model_copy(
        update={
            "user_event_effect": effect_text,
            "user_event_effect_until": now + timedelta(minutes=minutes),
            "user_event_attention_delta": attention_delta,
            "updated_at": now,
        }
    )
    store.save_life_runtime(canonical_user_id, updated)
    store.record_life_event(
        canonical_user_id,
        kind="life_event_result",
        content=effect_text,
        started_at=now,
        ends_at=updated.user_event_effect_until or now,
        status="active",
        source=source or f"life_result:{event_kind}",
    )
    store.adjust_next_life_day_plan_item(
        canonical_user_id,
        now=now,
        activity=future_activity,
        note=future_note,
        attention_delta=future_delta,
    )
    return synchronize_life_runtime(store, canonical_user_id, state, now=now)


# Non-user life results only make sense in a plausible window of the local day,
# and class_cancelled additionally requires that she is actually in a class-like block.
_LIFE_RESULT_WINDOWS: dict[str, tuple[int, int, tuple[str, ...]]] = {
    "class_cancelled": (9, 16, ("class", "study")),
    "friend_invite": (17, 21, ()),
    "weather_shift": (10, 19, ()),
    "fatigue": (14, 22, ()),
}

# Most days nothing notable happens; the ratio gate keeps that the default.
_LIFE_RESULT_DAY_PROBABILITY = 0.4


def plan_daily_life_result(canonical_user_id: str, local_date: str) -> tuple[str, int] | None:
    """Deterministically decide whether one small non-user event happens today, and when."""
    ratio = _stable_ratio(canonical_user_id, "life_result_plan", local_date)
    if ratio > _LIFE_RESULT_DAY_PROBABILITY:
        return None
    kinds = tuple(_LIFE_RESULT_WINDOWS)
    kind = kinds[min(len(kinds) - 1, int(ratio / _LIFE_RESULT_DAY_PROBABILITY * len(kinds)))]
    start_hour, end_hour, _ = _LIFE_RESULT_WINDOWS[kind]
    hour = start_hour + int(
        _stable_ratio(canonical_user_id, "life_result_hour", local_date) * max(1, end_hour - start_hour)
    )
    return kind, hour


def maybe_apply_planned_life_result(
    store,
    canonical_user_id: str,
    state: MoodState,
    *,
    now: datetime | None = None,
) -> LifeRuntimeState | None:
    """Fire today's planned non-user life event once its local hour arrives.

    The plan is stable across scheduler restarts; the date-scoped event source
    guarantees at most one application per day even with overlapping ticks.
    """
    now = now or utc_now()
    local = now.astimezone()
    local_date = local.date().isoformat()
    plan = plan_daily_life_result(canonical_user_id, local_date)
    if not plan:
        return None
    kind, planned_hour = plan
    _, end_hour, required_activity_kinds = _LIFE_RESULT_WINDOWS[kind]
    if local.hour < planned_hour or local.hour > end_hour:
        return None
    source = f"life_result:{kind}:{local_date}"
    if store.life_event_by_source(canonical_user_id, source):
        return None
    runtime = advance_life_runtime(store, canonical_user_id, state, now=now)
    if required_activity_kinds and runtime.activity_kind not in required_activity_kinds:
        return None
    if runtime.user_event_effect:
        # An active aftermath (usually a user event) already owns this stretch of the day.
        return None
    return apply_life_event_result(
        store,
        canonical_user_id,
        event_kind=kind,
        state=state,
        now=now,
        source=source,
    )


def synchronize_life_runtime(
    store,
    canonical_user_id: str,
    state: MoodState,
    *,
    now: datetime | None = None,
) -> LifeRuntimeState:
    """Project all durable companion state into one current-life trajectory."""
    now = now or utc_now()
    runtime = advance_life_runtime(store, canonical_user_id, state, now=now)
    state_delta, state_effect, force_phone_state = _state_life_projection(state)
    attention = max(0, min(100, runtime.base_attention_demand + runtime.user_event_attention_delta + state_delta))
    phone = force_phone_state or runtime.phone_attention
    if phone == "do_not_disturb" and not force_phone_state and not _user_effect_requests_space(runtime):
        phone = "away"
    updated = runtime.model_copy(
        update={"attention_demand": attention, "state_effect": state_effect, "phone_attention": phone, "updated_at": now}
    )
    if updated != runtime:
        store.save_life_runtime(canonical_user_id, updated)
    return updated


def runtime_prompt_line(runtime: LifeRuntimeState) -> str:
    phone = {
        "away": "手机不在手边",
        "notified": "收到了提醒但还没拿起手机",
        "glanced": "刚瞄到通知",
        "reading": "正在看消息",
        "typing": "正在组织怎么回",
        "do_not_disturb": "刻意没有看手机",
    }[runtime.phone_attention]
    influence = f"用户事件余波={runtime.user_event_effect}；" if runtime.user_event_effect else ""
    trajectory = f"慢性状态影响={runtime.state_effect}；" if runtime.state_effect else ""
    return f"生活节律/进行中事件：{runtime.activity}；{influence}{trajectory}手机状态={phone}。"


def proactive_outreach_allowed(runtime: LifeRuntimeState) -> tuple[bool, str]:
    if runtime.activity_kind == "sleep" and runtime.phone_attention != "reading":
        return False, "她已经睡着，不会为了维持存在感主动发消息。"
    if runtime.attention_demand >= 85 and not runtime.interruptible and not _effect_is_concern(runtime):
        return False, "她正被高专注活动占住，留到活动结束后再决定。"
    return True, "当前活动允许她偶尔看一眼手机。"


def _choose_template(canonical_user_id: str, now: datetime) -> ActivityTemplate:
    hour = now.astimezone().hour
    phase = (
        "early_morning" if 5 <= hour <= 8 else "morning_focus" if hour <= 11 else "lunch_break"
        if hour <= 13 else "afternoon_classes" if hour <= 17 else "evening_unwind" if hour <= 21
        else "late_evening" if hour <= 23 else "deep_night"
    )
    return _choose_template_for_phase(canonical_user_id, phase, now.astimezone().date().isoformat())


def _choose_template_for_phase(canonical_user_id: str, phase: str, local_date: str) -> ActivityTemplate:
    templates = _TEMPLATES[phase]
    index = int(_stable_ratio(canonical_user_id, phase, local_date) * len(templates))
    return templates[min(index, len(templates) - 1)]


def _stable_duration(canonical_user_id: str, template: ActivityTemplate, now: datetime) -> int:
    low, high = template.duration_minutes
    ratio = _stable_ratio(canonical_user_id, template.kind, now.strftime("%Y-%m-%d-%H"))
    return low + round((high - low) * ratio)


def _stable_ratio(*parts: str) -> float:
    digest = hashlib.sha256("|".join(parts).encode()).hexdigest()
    return int(digest[:12], 16) / float(0xFFFFFFFFFFFF)


def _glance_after_seconds(runtime: LifeRuntimeState) -> int:
    if runtime.attention_demand >= 75:
        return 12 * 60
    if runtime.attention_demand >= 50:
        return 6 * 60
    return 2 * 60


def _effect_for_user_event(
    canonical_user_id: str,
    event_kind: str,
    message: IncomingMessage,
    now: datetime,
) -> tuple[str, int, int, str | None] | None:
    effects = {
        "user_vulnerable": ("听见你状态不好，做自己的事时也会有点挂心", 90, -28, "glanced"),
        "boundary_violation": ("被冒犯后把注意力收回到自己身上，不想立刻看聊天", 180, 20, "do_not_disturb"),
        "control_pressure": ("不喜欢被催着安排，把手机先放到一边", 120, 14, "do_not_disturb"),
        "repair_attempt": ("道歉让她心里松一点，但仍会慢慢消化", 75, -8, None),
        "warmth_received": ("被认真回应后，做事时心情更轻一点", 100, -12, None),
        "availability_drop": ("知道你忙，便把注意力先收回到自己的事上", 70, 10, None),
        "return_after_gap": ("你回来让她有一点分心，会更容易瞄手机", 55, -10, "glanced"),
    }
    candidate = effects.get(event_kind)
    if not candidate:
        return None
    # Low-stakes events only occasionally spill into her day; strong events always do.
    certain = event_kind in {"user_vulnerable", "boundary_violation", "control_pressure", "repair_attempt"}
    if not certain and _stable_ratio(canonical_user_id, event_kind, message.text, now.strftime("%Y-%m-%d-%H")) > 0.45:
        return None
    return candidate


def _nudge_future_plan(store, canonical_user_id: str, *, event_kind: str, now: datetime) -> None:
    """Make high-salience interactions change the next plan without manufacturing a past."""
    nudges = {
        "user_vulnerable": ("原本的安排照常，但会有点挂心，间隙更容易看手机", "听见你难受后的余波", -12),
        "boundary_violation": ("把后面的时间留给自己，先不把聊天放在最前面", "边界受损后收回注意力", 16),
        "control_pressure": ("按自己的节奏做事，手机先不一直拿着", "不喜欢被催促的余波", 10),
        "repair_attempt": ("照原计划继续，心里比刚才松一点", "修复后的缓慢放松", -6),
    }
    nudge = nudges.get(event_kind)
    if nudge:
        activity, note, delta = nudge
        store.adjust_next_life_day_plan_item(
            canonical_user_id, now=now, activity=activity, note=note, attention_delta=delta
        )


def _life_event_result_effect(event_kind: str) -> tuple[str, int, int, str, str, int] | None:
    effects = {
        "class_cancelled": (
            "临时空出来一点时间，原本紧着的节奏松下来",
            85,
            -10,
            "临时空出来的时间里慢慢补一点自己的事，手机会更容易在手边",
            "临时空出来的时间",
            -12,
        ),
        "friend_invite": (
            "临时被同学喊去待一会儿，消息可能会晚点看",
            120,
            12,
            "和同学待一会儿，晚点再回到自己的节奏里",
            "临时邀约",
            10,
        ),
        "weather_shift": (
            "天气忽然变了，后面的安排变得更想待在室内",
            100,
            6,
            "天气不太稳，先找个室内地方慢慢待着",
            "天气改变后的调整",
            4,
        ),
        "fatigue": (
            "有点累，后面的节奏会放慢一点",
            140,
            14,
            "把后面的事拆小一点慢慢做，不强撑高专注",
            "疲惫后的降速",
            12,
        ),
    }
    return effects.get(event_kind)


def _effect_is_concern(runtime: LifeRuntimeState) -> bool:
    return bool(runtime.user_event_effect and "挂心" in runtime.user_event_effect)


def _user_effect_requests_space(runtime: LifeRuntimeState) -> bool:
    effect = runtime.user_event_effect or ""
    return "手机先放" in effect or "注意力收回" in effect


def _state_life_projection(state: MoodState) -> tuple[int, str | None, str | None]:
    delta = 0
    notes: list[str] = []
    phone: str | None = None
    if state.mood in {"hurt", "guarded"} or state.boundary_level >= 45:
        delta += 20
        notes.append("边界感让她更愿意把注意力收回自己身上")
        phone = "do_not_disturb"
    elif state.mood == "worried":
        delta -= 15
        notes.append("担心会让她更容易分神看手机")
    elif state.mood in {"miss_you", "affectionate", "curious"}:
        delta -= 8
        notes.append("在意和好奇让她更容易留意消息")
    if state.mood == "sleepy":
        delta += 14
        notes.append("困意让她减少看手机的频率")
    if state.perceived_responsiveness < 35:
        delta += 10
        notes.append("她不想总追着等回应")
    if state.initiative >= 60:
        delta -= 5
    if state.unresolved_emotion and state.emotional_charge >= 25:
        delta += 5
        notes.append("未消化的情绪让她做事时有点走神或收住")
    return delta, "；".join(notes) or None, phone

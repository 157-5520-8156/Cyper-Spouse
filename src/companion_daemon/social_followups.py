"""Low-pressure social tasks that close loops around private life and mild inconsistency."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
import re

from companion_daemon.models import LifeRuntimeState
from companion_daemon.time import utc_now


LIFE_SHARE_DELAY = timedelta(minutes=90)
LIFE_SHARE_EXPIRE = timedelta(hours=14)
CONTRADICTION_DELAY = timedelta(minutes=55)
CONTRADICTION_EXPIRE = timedelta(hours=8)
STALE_UNSHARED_AGE = timedelta(minutes=90)

_CONTRADICTION_CUE = re.compile(r"(不是说|你刚才说|你刚刚说|你不是说|你怎么还在|你怎么还)")
_CLAIM_ACTIVITY: list[tuple[tuple[str, ...], str]] = [
    (("睡", "睡觉", "困了要睡"), "sleep"),
    (("上课", "听课", "老师在讲"), "class"),
    (("自习", "图书馆", "写作业", "赶作业"), "study"),
    (("吃饭", "午饭", "晚饭", "食堂", "外卖到了"), "meal"),
    (("出门", "逛街", "散步", "下楼"), "walk"),
    (("和同学", "朋友约", "聚会", "室友"), "friends"),
]
_ACTIVITY_LABELS = {
    "sleep": "睡觉",
    "class": "上课",
    "study": "自习",
    "meal": "吃饭",
    "walk": "出门",
    "friends": "和同学",
    "routine": "收拾",
    "unwind": "放松",
    "quiet": "安静待着",
    "between": "空档",
    "morning": "早晨节奏",
    "errand": "跑腿",
}


def detect_mild_contradiction(
    text: str,
    runtime: LifeRuntimeState,
    *,
    recent_her_lines: list[str] | None = None,
) -> str | None:
    """Return a short note when the user calls out a mismatch with her current life."""
    if not _CONTRADICTION_CUE.search(text):
        return None
    claimed_kind = _claimed_activity_kind(text)
    if claimed_kind is None:
        return None
    current_kind = runtime.activity_kind
    if claimed_kind == current_kind:
        return None
    claimed = _ACTIVITY_LABELS.get(claimed_kind, claimed_kind)
    current = runtime.activity
    if recent_her_lines and _recent_her_lines_support_claim(recent_her_lines, claimed_kind):
        return f"他记得你之前在{claimed}，但你现在其实在{current}"
    if claimed_kind != current_kind:
        return f"他以为你还在{claimed}，但你现在其实在{current}"
    return None


def create_life_share_followup(
    store,
    canonical_user_id: str,
    *,
    life_event_id: int,
    content: str,
    platform: str = "qq",
    platform_user_id: str | None = None,
    now: datetime | None = None,
) -> int:
    now = (now or utc_now()).astimezone(UTC)
    peer_id = platform_user_id or store.primary_platform_user_id(canonical_user_id, platform=platform)
    store.cancel_active_social_tasks(canonical_user_id, kind="life_share_followup")
    return store.create_social_task(
        canonical_user_id,
        kind="life_share_followup",
        platform=platform,  # type: ignore[arg-type]
        platform_user_id=peer_id,
        payload={"life_event_id": life_event_id, "content": content[:200]},
        reason="有件今天的小事还没自然地跟他说出口",
        due_at=now + LIFE_SHARE_DELAY,
        expires_at=now + LIFE_SHARE_EXPIRE,
    )


def create_contradiction_followup(
    store,
    canonical_user_id: str,
    *,
    platform: str,
    platform_user_id: str,
    note: str,
    now: datetime | None = None,
) -> int:
    now = (now or utc_now()).astimezone(UTC)
    store.cancel_active_social_tasks(canonical_user_id, kind="contradiction_followup")
    return store.create_social_task(
        canonical_user_id,
        kind="contradiction_followup",
        platform=platform,  # type: ignore[arg-type]
        platform_user_id=platform_user_id,
        payload={"note": note[:200]},
        reason="前后说法有点对不上，她心里还轻轻卡着",
        due_at=now + CONTRADICTION_DELAY,
        expires_at=now + CONTRADICTION_EXPIRE,
    )


def cancel_life_share_followup_for_event(store, canonical_user_id: str, life_event_id: int) -> None:
    for row in store.recent_social_tasks(canonical_user_id, limit=12):
        if row["kind"] != "life_share_followup" or row["status"] not in {"pending", "claimed"}:
            continue
        payload = store.social_task_payload(int(row["id"]))
        if payload.get("life_event_id") == life_event_id:
            store.cancel_social_task(int(row["id"]))


def reconcile_unshared_life_share_tasks(
    store,
    canonical_user_id: str,
    *,
    now: datetime | None = None,
    platform: str = "qq",
) -> int | None:
    """Backfill a share followup when a private event has aged without being shared."""
    now = (now or utc_now()).astimezone(UTC)
    if store.has_active_social_task(canonical_user_id, kind="life_share_followup"):
        return None
    for row in store.unshared_private_life_events(canonical_user_id, limit=3):
        started_at = datetime.fromisoformat(str(row["started_at"])).astimezone(UTC)
        if now - started_at < STALE_UNSHARED_AGE:
            continue
        return create_life_share_followup(
            store,
            canonical_user_id,
            life_event_id=int(row["id"]),
            content=str(row["content"]),
            platform=platform,
            now=now,
        )
    return None


def social_task_payload(task) -> dict[str, object]:
    if task is None or "payload_json" not in task.keys():
        return {}
    raw = task["payload_json"]
    if not raw:
        return {}
    return json.loads(str(raw))


def _claimed_activity_kind(text: str) -> str | None:
    for keywords, kind in _CLAIM_ACTIVITY:
        if any(keyword in text for keyword in keywords):
            return kind
    return None


def _recent_her_lines_support_claim(lines: list[str], claimed_kind: str) -> bool:
    keywords = next((words for words, kind in _CLAIM_ACTIVITY if kind == claimed_kind), ())
    return any(any(keyword in line for keyword in keywords) for line in lines)

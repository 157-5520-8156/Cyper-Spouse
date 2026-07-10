from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from companion_daemon.db import CompanionStore
from companion_daemon.engine import CompanionEngine
from companion_daemon.engine import seed_user
from companion_daemon.llm import FakeCompanionModel
from companion_daemon.models import IncomingMessage
import companion_daemon.proactive_scheduler as scheduler_module


def test_deferred_reply_task_persists_payload_and_is_claimed_when_due(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    now = datetime(2026, 7, 10, 2, 0, tzinfo=UTC)

    task_id = store.create_social_task(
        "geoff",
        kind="reply_later",
        platform="qq",
        platform_user_id="2759284998",
        payload=IncomingMessage(platform="qq", platform_user_id="2759284998", text="我还没说完").model_dump(mode="json"),
        reason="unread_during_class",
        due_at=now + timedelta(minutes=5),
        expires_at=now + timedelta(hours=12),
    )

    assert store.claim_due_social_tasks(kind="reply_later", now=now) == []
    claimed = store.claim_due_social_tasks(kind="reply_later", now=now + timedelta(minutes=5))

    assert [row["id"] for row in claimed] == [task_id]
    assert claimed[0]["status"] == "claimed"
    assert '"text": "我还没说完"' in claimed[0]["payload_json"]


def test_newer_deferred_reply_can_cancel_old_one_and_stale_claim_is_recoverable(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    now = datetime(2026, 7, 10, 2, 0, tzinfo=UTC)
    common = {
        "kind": "reply_later",
        "platform": "qq",
        "platform_user_id": "2759284998",
        "payload": {"text": "消息"},
        "reason": "thinking_wait_for_user",
        "due_at": now,
        "expires_at": now + timedelta(hours=12),
    }
    old_id = store.create_social_task("geoff", **common)
    store.cancel_active_social_tasks("geoff", kind="reply_later")
    new_id = store.create_social_task("geoff", **common)

    claimed = store.claim_due_social_tasks(kind="reply_later", now=now)
    retried = store.claim_due_social_tasks(kind="reply_later", now=now + timedelta(minutes=11))

    assert [row["id"] for row in claimed] == [new_id]
    assert old_id not in [row["id"] for row in claimed]
    assert [row["id"] for row in retried] == [new_id]


def test_resolved_or_expired_social_tasks_are_not_replayed(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    now = datetime(2026, 7, 10, 2, 0, tzinfo=UTC)
    resolved = store.create_social_task(
        "geoff", kind="reply_later", platform="qq", platform_user_id="2759284998", payload={}, reason="busy",
        due_at=now, expires_at=now + timedelta(hours=1),
    )
    expired = store.create_social_task(
        "geoff", kind="reply_later", platform="qq", platform_user_id="2759284998", payload={}, reason="busy",
        due_at=now, expires_at=now + timedelta(minutes=1),
    )
    store.resolve_social_task(resolved)

    claimed = store.claim_due_social_tasks(kind="reply_later", now=now + timedelta(hours=2))

    assert claimed == []
    # Expiration is driven by the same claim path, so it cannot turn into a stale replay.
    assert store.claim_due_social_tasks(kind="reply_later", now=now + timedelta(hours=3)) == []
    assert expired != resolved


def test_read_but_not_replied_task_can_be_created_and_claimed(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    engine = CompanionEngine(store, FakeCompanionModel(), "你是知栀。")
    now = datetime(2026, 7, 10, 2, 0, tzinfo=UTC)
    message = IncomingMessage(platform="qq", platform_user_id="2759284998", text="我刚才说到哪了")

    task_id = engine.create_read_later_task(
        message,
        defer_minutes=14,
        reason="read_then_distracted",
        now=now,
    )
    claimed = store.claim_due_social_tasks(kind="reply_later", now=now + timedelta(minutes=14))

    assert [row["id"] for row in claimed] == [task_id]
    assert "读到了但被手头的事岔开" in claimed[0]["reason"]
    assert '"text": "我刚才说到哪了"' in claimed[0]["payload_json"]


@pytest.mark.asyncio
async def test_vulnerable_message_creates_a_persistent_comfort_followup(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    engine = CompanionEngine(store, FakeCompanionModel(), "你是知栀。")

    await engine.handle_message(
        IncomingMessage(platform="qq", platform_user_id="geoff", text="我现在真的有点撑不住"),
        skip_reply=True,
    )

    task = store.recent_social_tasks("geoff")[0]
    assert task["kind"] == "comfort_followup"
    assert task["status"] == "pending"
    assert "确认" in task["reason"]


@pytest.mark.asyncio
async def test_due_comfort_followup_resolves_only_after_proactive_delivery(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    engine = CompanionEngine(store, FakeCompanionModel(), "你是知栀。")
    now = datetime.now(UTC)
    task_id = store.create_social_task(
        "geoff",
        kind="comfort_followup",
        platform="qq",
        platform_user_id="geoff",
        payload={"event": "user_vulnerable"},
        reason="刚听见你状态不好，晚一点仍会想确认你有没有缓过来",
        due_at=now - timedelta(minutes=1),
        expires_at=now + timedelta(hours=3),
    )

    decision = await engine.proactive_tick("geoff")

    assert decision.trigger_type == "comfort_followup"
    assert decision.social_task_id == task_id
    engine.confirm_proactive_delivery(decision)
    assert store.next_due_social_task("geoff", kinds=("comfort_followup",), now=now + timedelta(hours=1)) is None


@pytest.mark.asyncio
async def test_user_promise_becomes_a_low_pressure_followup_and_new_turn_cancels_it(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    engine = CompanionEngine(store, FakeCompanionModel(), "你是知栀。")

    await engine.handle_message(
        IncomingMessage(platform="qq", platform_user_id="geoff", text="这个我晚点跟你说"),
        skip_reply=True,
    )

    task = store.recent_social_tasks("geoff")[0]
    assert task["kind"] == "promise_followup"
    assert "不追着问" in task["reason"]

    await engine.handle_message(
        IncomingMessage(platform="qq", platform_user_id="geoff", text="我回来啦"),
        skip_reply=True,
    )

    assert store.next_due_social_task(
        "geoff", kinds=("promise_followup",), now=datetime.now(UTC) + timedelta(hours=8)
    ) is None


@pytest.mark.asyncio
async def test_scheduler_recovers_overdue_deferred_reply_once(tmp_path: Path, monkeypatch) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    store.map_account("qq", "2759284998", "geoff")
    engine = CompanionEngine(store, FakeCompanionModel(), "你是知栀。")
    now = datetime(2026, 7, 10, 2, 10, tzinfo=UTC)
    store.create_social_task(
        "geoff",
        kind="reply_later",
        platform="qq",
        platform_user_id="2759284998",
        payload=IncomingMessage(platform="qq", platform_user_id="2759284998", text="我刚才在想一件事").model_dump(mode="json"),
        reason="unread_during_study",
        due_at=now - timedelta(minutes=3),
        expires_at=now + timedelta(hours=1),
    )

    class FakeDelivery:
        sent: list[str] = []

        def __init__(self, *args, **kwargs) -> None:
            return None

        async def send_text(self, recipient_id: str, text: str) -> None:
            assert recipient_id == "2759284998"
            self.sent.append(text)

    monkeypatch.setattr(scheduler_module, "QQDelivery", FakeDelivery)
    monkeypatch.setattr(scheduler_module, "get_settings", lambda: object())

    recovered = await scheduler_module.recover_overdue_deferred_replies(
        engine, send=True, sandbox=False, now=now
    )

    assert recovered == 1
    assert FakeDelivery.sent == ["刚刚是不是忙完了？我在呢。"]
    assert store.claim_due_social_tasks(kind="reply_later", now=now + timedelta(hours=1)) == []

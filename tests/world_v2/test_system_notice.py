from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import hashlib
import json
from pathlib import Path
import sqlite3

import pytest

from companion_daemon.world_v2.qq_ingress_policy import (
    QQIngressFragment,
    SQLiteQQIngressStore,
)
from companion_daemon.world_v2.schemas import ProjectionCursor, WorldEvent
from companion_daemon.world_v2.sqlite_ledger import SQLiteWorldLedger
from companion_daemon.world_v2.system_notice import (
    SYSTEM_NOTICE_TEXT,
    SQLiteSystemNoticeDispatcher,
    SystemNoticeAuthority,
)


NOW = datetime(2026, 8, 10, 8, 0, tzinfo=UTC)


class _Delivery:
    def __init__(self) -> None:
        self.calls = 0
        self.release = asyncio.Event()

    async def send_text(self, recipient_id: str, text: str) -> dict[str, object]:
        self.calls += 1
        assert recipient_id == "10001"
        assert text == SYSTEM_NOTICE_TEXT
        await self.release.wait()
        return {"status": "ok", "data": {"message_id": "notice-1"}}


class _StaticDelivery:
    def __init__(self, response: dict[str, object]) -> None:
        self.response = response
        self.calls = 0

    async def send_text(self, recipient_id: str, text: str) -> dict[str, object]:
        self.calls += 1
        assert recipient_id == "10001"
        assert text == SYSTEM_NOTICE_TEXT
        return self.response


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provider_body",
    [
        {"status": "failed", "retcode": 0, "message": "sensitive status failure"},
        {"status": "ok", "retcode": 100, "message": "sensitive retcode failure"},
    ],
    ids=["failed-status", "nonzero-retcode"],
)
async def test_system_notice_preserves_napcat_business_failure_as_unknown_once(
    tmp_path: Path,
    provider_body: dict[str, object],
) -> None:
    path = tmp_path / "notice-provider-rejected.sqlite"
    expected_hash = "sha256:" + hashlib.sha256(
        json.dumps(
            provider_body,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    delivery = _StaticDelivery(provider_body)
    dispatcher = SQLiteSystemNoticeDispatcher(
        path=str(path),
        world_id="world:test",
        delivery=delivery,
        now=lambda: NOW,
        cooldown_seconds=0,
    )

    first = await dispatcher.notify(
        notice_key="notice:provider-rejected",
        recipient_id="10001",
        failure_code="turn_technical_failure",
    )
    dispatcher.close()

    restarted = SQLiteSystemNoticeDispatcher(
        path=str(path),
        world_id="world:test",
        delivery=delivery,
        now=lambda: NOW,
        cooldown_seconds=0,
    )
    replay = await restarted.notify(
        notice_key="notice:provider-rejected",
        recipient_id="10001",
        failure_code="turn_technical_failure",
    )
    restarted.close()

    with sqlite3.connect(path) as inspection:
        stored = inspection.execute(
            "SELECT status, provider_ref, response_hash, error_class "
            "FROM world_v2_system_notice_dispatch "
            "WHERE world_id=? AND notice_key=?",
            ("world:test", "notice:provider-rejected"),
        ).fetchone()

    assert first.status == "unknown"
    assert first.durable_terminal is True
    assert first.provider_ref is None
    assert first.error_class == "provider_rejected"
    assert replay.status == "already_attempted"
    assert replay.error_class == "provider_rejected"
    assert delivery.calls == 1
    assert stored == ("unknown", None, expected_hash, "provider_rejected")
    assert str(provider_body["message"]) not in repr((first, replay, stored))


@pytest.mark.asyncio
async def test_system_notice_is_effect_once_across_concurrency_and_restart(
    tmp_path: Path,
) -> None:
    path = tmp_path / "notice.sqlite"
    delivery = _Delivery()
    dispatcher = SQLiteSystemNoticeDispatcher(
        path=str(path), world_id="world:test", delivery=delivery, now=lambda: NOW
    )
    first = asyncio.create_task(
        dispatcher.notify(
            notice_key="notice:turn:1",
            recipient_id="10001",
            failure_code="turn_technical_failure",
        )
    )
    await asyncio.sleep(0)
    second = asyncio.create_task(
        dispatcher.notify(
            notice_key="notice:turn:1",
            recipient_id="10001",
            failure_code="turn_technical_failure",
        )
    )
    delivery.release.set()
    first_result, second_result = await asyncio.gather(first, second)

    assert first_result.status == "provider_accepted"
    assert second_result.status == "already_attempted"
    assert first_result.durable_terminal is True
    assert second_result.durable_terminal is True
    assert delivery.calls == 1
    dispatcher.close()

    restarted = SQLiteSystemNoticeDispatcher(
        path=str(path), world_id="world:test", delivery=delivery, now=lambda: NOW
    )
    replay = await restarted.notify(
        notice_key="notice:turn:1",
        recipient_id="10001",
        failure_code="turn_technical_failure",
    )
    assert replay.status == "already_attempted"
    assert replay.durable_terminal is True
    assert delivery.calls == 1
    restarted.close()


@pytest.mark.asyncio
async def test_system_notice_is_effect_once_across_independent_dispatchers(
    tmp_path: Path,
) -> None:
    path = tmp_path / "notice-independent-dispatchers.sqlite"
    delivery = _Delivery()
    left = SQLiteSystemNoticeDispatcher(
        path=str(path), world_id="world:test", delivery=delivery, now=lambda: NOW
    )
    right = SQLiteSystemNoticeDispatcher(
        path=str(path), world_id="world:test", delivery=delivery, now=lambda: NOW
    )
    first = asyncio.create_task(
        left.notify(
            notice_key="notice:shared",
            recipient_id="10001",
            failure_code="turn_technical_failure",
        )
    )
    second = asyncio.create_task(
        right.notify(
            notice_key="notice:shared",
            recipient_id="10001",
            failure_code="turn_technical_failure",
        )
    )
    for _ in range(20):
        if delivery.calls == 1:
            break
        await asyncio.sleep(0)
    delivery.release.set()
    try:
        results = await asyncio.wait_for(asyncio.gather(first, second), timeout=1)
    finally:
        left.close()
        right.close()

    assert sorted(result.status for result in results) == [
        "already_attempted",
        "provider_accepted",
    ]
    assert delivery.calls == 1


@pytest.mark.asyncio
async def test_system_notice_persistently_rate_limits_distinct_failures(tmp_path: Path) -> None:
    clock = {"now": NOW}
    delivery = _Delivery()
    delivery.release.set()
    dispatcher = SQLiteSystemNoticeDispatcher(
        path=str(tmp_path / "notice-rate.sqlite"),
        world_id="world:test",
        delivery=delivery,
        now=lambda: clock["now"],
        cooldown_seconds=60,
    )

    first = await dispatcher.notify(
        notice_key="notice:turn:1",
        recipient_id="10001",
        failure_code="turn_technical_failure",
    )
    clock["now"] += timedelta(seconds=30)
    second = await dispatcher.notify(
        notice_key="notice:turn:2",
        recipient_id="10001",
        failure_code="turn_technical_failure",
    )
    clock["now"] += timedelta(seconds=31)
    third = await dispatcher.notify(
        notice_key="notice:turn:3",
        recipient_id="10001",
        failure_code="turn_technical_failure",
    )

    assert (first.status, second.status, third.status) == (
        "provider_accepted",
        "suppressed",
        "provider_accepted",
    )
    assert (first.durable_terminal, second.durable_terminal, third.durable_terminal) == (
        True,
        True,
        True,
    )
    assert delivery.calls == 2
    dispatcher.close()


@pytest.mark.asyncio
async def test_system_notice_rechecks_authority_after_waiting_for_send_lock(
    tmp_path: Path,
) -> None:
    delivery = _Delivery()
    dispatcher = SQLiteSystemNoticeDispatcher(
        path=str(tmp_path / "notice-authority-race.sqlite"),
        world_id="world:test",
        delivery=delivery,
        now=lambda: NOW,
        cooldown_seconds=0,
    )
    blocker = asyncio.create_task(
        dispatcher.notify(
            notice_key="notice:blocker",
            recipient_id="10001",
            failure_code="turn_technical_failure",
        )
    )
    for _ in range(20):
        if delivery.calls == 1:
            break
        await asyncio.sleep(0)
    assert delivery.calls == 1

    authority = {"current": True}

    async def still_current() -> bool:
        return authority["current"]

    stale = asyncio.create_task(
        dispatcher.notify(
            notice_key="notice:stale-after-lock-wait",
            recipient_id="10001",
            failure_code="turn_technical_failure",
            still_current=still_current,
        )
    )
    await asyncio.sleep(0)
    authority["current"] = False
    delivery.release.set()
    blocker_result, stale_result = await asyncio.gather(blocker, stale)

    assert blocker_result.status == "provider_accepted"
    assert stale_result.status == "suppressed"
    assert blocker_result.durable_terminal is True
    assert stale_result.durable_terminal is False
    assert delivery.calls == 1
    dispatcher.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("stale_write", ["pending_ingress", "world_head"])
async def test_system_notice_atomically_reserves_only_current_sqlite_authority(
    tmp_path: Path,
    stale_write: str,
) -> None:
    """A separate SQLite writer cannot slip between final proof and reserve."""

    path = tmp_path / f"notice-authority-{stale_write}.sqlite"
    world_id = "world:test"
    ledger = SQLiteWorldLedger(path=path, world_id=world_id)
    ingress = SQLiteQQIngressStore(path)
    delivery = _Delivery()
    delivery.release.set()
    dispatcher = SQLiteSystemNoticeDispatcher(
        path=str(path),
        world_id=world_id,
        delivery=delivery,
        now=lambda: NOW,
        cooldown_seconds=0,
    )
    authority_read = asyncio.Event()
    release_authority = asyncio.Event()
    projection = ledger.project()
    authority = SystemNoticeAuthority(
        expected_cursor=ProjectionCursor(
            world_revision=projection.world_revision,
            deliberation_revision=projection.deliberation_revision,
            ledger_sequence=projection.ledger_sequence,
        )
    )

    async def still_current() -> SystemNoticeAuthority:
        # This is the last semantic authority read.  The competing connection
        # commits only after it, reproducing the old callback/INSERT gap.
        authority_read.set()
        await release_authority.wait()
        return authority

    pending = asyncio.create_task(
        dispatcher.notify(
            notice_key=f"notice:stale-{stale_write}",
            recipient_id="10001",
            failure_code="turn_technical_failure",
            still_current=still_current,
        )
    )
    await asyncio.wait_for(authority_read.wait(), timeout=1)
    if stale_write == "pending_ingress":
        ingress.submit(
            QQIngressFragment(
                source_event_id="message:newer",
                recipient_id="10001",
                observed_at=NOW + timedelta(seconds=1),
                content_shape="text",
                text="更新的一轮已经到达。",
            ),
            received_at=NOW + timedelta(seconds=1),
        )
    else:
        ledger.commit(
            [
                WorldEvent.from_payload(
                    schema_version="world-v2.1",
                    event_id="event:newer-observation",
                    world_id=world_id,
                    event_type="ObservationRecorded",
                    logical_time=NOW,
                    created_at=NOW,
                    actor="system:test",
                    source="test",
                    trace_id="trace:notice-authority-race",
                    causation_id="cause:notice-authority-race",
                    correlation_id="correlation:notice-authority-race",
                    idempotency_key="event:newer-observation",
                    payload={"observation_id": "observation:newer"},
                )
            ],
            commit_id="commit:newer-observation",
            expected_world_revision=0,
            expected_deliberation_revision=0,
        )
    release_authority.set()
    try:
        result = await asyncio.wait_for(pending, timeout=1)
        with sqlite3.connect(path) as inspection:
            rows = inspection.execute(
                "SELECT status FROM world_v2_system_notice_dispatch "
                "WHERE world_id=? AND notice_key=?",
                (world_id, f"notice:stale-{stale_write}"),
            ).fetchall()
    finally:
        dispatcher.close()
        ingress.close()
        ledger.close()

    assert result.status == "suppressed"
    assert result.durable_terminal is False
    assert delivery.calls == 0
    assert rows == []


@pytest.mark.asyncio
async def test_system_notice_retries_one_fresh_authority_after_unrelated_head_race(
    tmp_path: Path,
) -> None:
    path = tmp_path / "notice-authority-fresh-retry.sqlite"
    world_id = "world:test"
    ledger = SQLiteWorldLedger(path=path, world_id=world_id)
    ingress = SQLiteQQIngressStore(path)
    delivery = _Delivery()
    delivery.release.set()
    dispatcher = SQLiteSystemNoticeDispatcher(
        path=str(path),
        world_id=world_id,
        delivery=delivery,
        now=lambda: NOW,
        cooldown_seconds=0,
    )
    authority_read = asyncio.Event()
    release_first_authority = asyncio.Event()
    callback_calls = 0

    async def still_current() -> SystemNoticeAuthority:
        nonlocal callback_calls
        callback_calls += 1
        projection = ledger.project()
        authority = SystemNoticeAuthority(
            expected_cursor=ProjectionCursor(
                world_revision=projection.world_revision,
                deliberation_revision=projection.deliberation_revision,
                ledger_sequence=projection.ledger_sequence,
            )
        )
        if callback_calls == 1:
            authority_read.set()
            await release_first_authority.wait()
        return authority

    pending = asyncio.create_task(
        dispatcher.notify(
            notice_key="notice:fresh-authority-retry",
            recipient_id="10001",
            failure_code="turn_technical_failure",
            still_current=still_current,
        )
    )
    await asyncio.wait_for(authority_read.wait(), timeout=1)
    logical_from = NOW - timedelta(seconds=1)
    logical_to = NOW
    ledger.commit(
        [
            WorldEvent.from_payload(
                schema_version="world-v2.1",
                event_id="event:unrelated-clock-advance",
                world_id=world_id,
                event_type="ClockAdvanced",
                logical_time=logical_to,
                created_at=NOW,
                actor="system:test",
                source="test",
                trace_id="trace:notice-fresh-authority",
                causation_id="cause:notice-fresh-authority",
                correlation_id="correlation:notice-fresh-authority",
                idempotency_key="event:unrelated-clock-advance",
                payload={
                    "logical_time_from": logical_from.isoformat(),
                    "logical_time_to": logical_to.isoformat(),
                },
            )
        ],
        commit_id="commit:unrelated-clock-advance",
        expected_world_revision=0,
        expected_deliberation_revision=0,
    )
    release_first_authority.set()
    try:
        result = await asyncio.wait_for(pending, timeout=1)
    finally:
        dispatcher.close()
        ingress.close()
        ledger.close()

    assert result.status == "provider_accepted"
    assert callback_calls == 2
    assert delivery.calls == 1


@pytest.mark.asyncio
async def test_system_notice_atomic_authority_allows_only_explicit_batch_exclusions(
    tmp_path: Path,
) -> None:
    path = tmp_path / "notice-authority-exclusions.sqlite"
    world_id = "world:test"
    ledger = SQLiteWorldLedger(path=path, world_id=world_id)
    projection = ledger.project()
    ingress = SQLiteQQIngressStore(path)
    ingress.submit(
        QQIngressFragment(
            source_event_id="message:current-batch",
            recipient_id="10001",
            observed_at=NOW,
            content_shape="text",
            text="当前技术失败批次。",
        ),
        received_at=NOW,
    )
    delivery = _Delivery()
    delivery.release.set()
    dispatcher = SQLiteSystemNoticeDispatcher(
        path=str(path),
        world_id=world_id,
        delivery=delivery,
        now=lambda: NOW,
        cooldown_seconds=0,
    )
    authority = SystemNoticeAuthority(
        expected_cursor=ProjectionCursor(
            world_revision=projection.world_revision,
            deliberation_revision=projection.deliberation_revision,
            ledger_sequence=projection.ledger_sequence,
        ),
        excluding_source_event_ids=("message:current-batch",),
    )
    try:
        result = await dispatcher.notify(
            notice_key="notice:current-batch",
            recipient_id="10001",
            failure_code="turn_technical_failure",
            still_current=lambda: authority,
        )
    finally:
        dispatcher.close()
        ingress.close()
        ledger.close()

    assert result.status == "provider_accepted"
    assert delivery.calls == 1

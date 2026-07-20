"""Restart-window compensation replays missed inbound QQ messages exactly once."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from companion_daemon.world_v2.qq_history_backfill import (
    backfill_missed_private_messages,
    history_message_to_onebot_event,
)


NOW = datetime(2026, 7, 20, 3, 0, tzinfo=UTC)
RECIPIENT = "10001"


def _history_message(
    *,
    message_id: str,
    sender_id: str = RECIPIENT,
    text: str = "你回来啦？",
    at: datetime = NOW - timedelta(minutes=30),
) -> dict[str, object]:
    return {
        "self_id": "20002",
        "message_type": "private",
        "message_id": message_id,
        "time": at.timestamp(),
        "sender": {"user_id": sender_id, "nickname": "geoff"},
        "raw_message": text,
        "message": [{"type": "text", "data": {"text": text}}],
    }


class _FakeHost:
    def __init__(self, *, known: set[str] | None = None) -> None:
        self.known = known or set()
        self.replayed: list[str] = []

    def submission_state(self, source_event_id: str) -> str | None:
        return "committed" if source_event_id in self.known else None

    async def inbound_fragment(self, fragment):
        self.replayed.append(fragment.source_event_id)

        class _Result:
            status = "delivered"
            action_id = "action:1"
            canonical_user_id = "geoff"

        return _Result()


def test_history_conversion_keeps_only_inbound_peer_messages() -> None:
    inbound = history_message_to_onebot_event(
        _history_message(message_id="m1"), recipient_id=RECIPIENT
    )
    assert inbound is not None
    assert inbound["post_type"] == "message"
    assert inbound["user_id"] == RECIPIENT
    assert inbound["message_id"] == "m1"

    own_outbound = history_message_to_onebot_event(
        _history_message(message_id="m2", sender_id="20002"), recipient_id=RECIPIENT
    )
    assert own_outbound is None
    other_peer = history_message_to_onebot_event(
        _history_message(message_id="m3", sender_id="30003"), recipient_id=RECIPIENT
    )
    assert other_peer is None
    assert (
        history_message_to_onebot_event(
            {**_history_message(message_id=""), "message_id": ""},
            recipient_id=RECIPIENT,
        )
        is None
    )


@pytest.mark.asyncio
async def test_backfill_replays_missed_messages_and_dedupes_known_ones() -> None:
    host = _FakeHost(known={"seen-before"})

    async def fetch() -> list[dict[str, object]]:
        return [
            _history_message(message_id="seen-before", at=NOW - timedelta(minutes=40)),
            _history_message(message_id="missed-1", at=NOW - timedelta(minutes=20)),
            _history_message(message_id="missed-2", at=NOW - timedelta(minutes=10)),
            # Her own outbound message is delivery evidence, never ingress.
            _history_message(message_id="hers", sender_id="20002"),
            # Ancient history stays out of the first deployment's replay.
            _history_message(message_id="ancient", at=NOW - timedelta(days=30)),
        ]

    report = await backfill_missed_private_messages(
        host=host, fetch_history=fetch, recipient_id=RECIPIENT, now=NOW
    )
    assert host.replayed == ["missed-1", "missed-2"]
    assert report.replayed == 2
    assert report.deduplicated == 1
    assert report.skipped == 2
    assert report.failed == 0
    assert report.statuses == ("delivered", "delivered")


@pytest.mark.asyncio
async def test_backfill_survives_provider_and_turn_failures() -> None:
    async def broken_fetch() -> list[dict[str, object]]:
        raise ConnectionError("napcat is not up yet")

    report = await backfill_missed_private_messages(
        host=_FakeHost(), fetch_history=broken_fetch, recipient_id=RECIPIENT, now=NOW
    )
    assert report.error == "ConnectionError"
    assert report.replayed == 0

    class _FailingHost(_FakeHost):
        async def inbound_fragment(self, fragment):
            if fragment.source_event_id == "missed-1":
                raise RuntimeError("turn failed")
            return await super().inbound_fragment(fragment)

    host = _FailingHost()

    async def fetch() -> list[dict[str, object]]:
        return [
            _history_message(message_id="missed-1", at=NOW - timedelta(minutes=20)),
            _history_message(message_id="missed-2", at=NOW - timedelta(minutes=10)),
        ]

    report = await backfill_missed_private_messages(
        host=host, fetch_history=fetch, recipient_id=RECIPIENT, now=NOW
    )
    assert report.failed == 1
    assert report.replayed == 1
    assert host.replayed == ["missed-2"]

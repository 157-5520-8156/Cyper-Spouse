"""Restart-window compensation for the World v2 QQ C2C lane.

While the adapter process is down, NapCat keeps receiving private messages
but has nobody to deliver its push events to.  This module replays that gap
after startup: it pulls the peer's recent private history from the provider,
normalizes each missed inbound message through the exact same
``normalize_onebot_qq_ingress`` shape used by live push events, and submits
it to the host's ordinary ingress path.

No new authority is created here:

* idempotency rests entirely on the durable ingress store's
  ``source_event_id`` dedupe (a replayed provider ``message_id`` that was
  already batched returns its committed outcome instead of a second turn)
  and, downstream, on the ledger's observation identity;
* only messages *sent by the peer* are candidates — her own outbound history
  is delivery evidence, never ingress;
* a bounded recency window plus a count cap keeps the very first deployment
  of this feature from replaying weeks of already-answered history that
  predates the durable ingress store.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
import logging
from typing import Any, Awaitable, Callable, Mapping

from .qq_ingress_policy import normalize_onebot_qq_ingress


logger = logging.getLogger(__name__)

# Flood protection for the first run after this feature ships; the durable
# dedupe, not this window, is the correctness mechanism.
DEFAULT_BACKFILL_WINDOW = timedelta(hours=48)
DEFAULT_BACKFILL_COUNT = 30


@dataclass(frozen=True, slots=True)
class QQHistoryBackfillReport:
    """Process-local evidence of one startup compensation pass."""

    fetched: int = 0
    replayed: int = 0
    deduplicated: int = 0
    skipped: int = 0
    failed: int = 0
    error: str | None = None
    statuses: tuple[str, ...] = field(default=())


def history_message_to_onebot_event(
    message: Mapping[str, Any], *, recipient_id: str
) -> dict[str, Any] | None:
    """Rebuild one provider history record as a private OneBot message event.

    Returns ``None`` for records that are not an inbound private message from
    the configured peer (her own sent messages, group noise, malformed rows).
    """

    sender = message.get("sender")
    sender_id = str(
        (sender.get("user_id") if isinstance(sender, Mapping) else None)
        or ""
    ).strip()
    if not sender_id:
        sender_id = str(message.get("user_id") or "").strip()
    self_id = str(message.get("self_id") or "").strip()
    if not sender_id or sender_id != recipient_id or sender_id == self_id:
        return None
    if str(message.get("message_type") or "private") == "group":
        return None
    message_id = str(message.get("message_id") or "").strip()
    if not message_id:
        return None
    segments = message.get("message")
    return {
        "post_type": "message",
        "message_type": "private",
        "user_id": sender_id,
        "message_id": message_id,
        "time": message.get("time"),
        "message": segments if isinstance(segments, list) else [],
        "raw_message": message.get("raw_message") or "",
    }


async def backfill_missed_private_messages(
    *,
    host,
    fetch_history: Callable[[], Awaitable[list[dict[str, Any]]]],
    recipient_id: str,
    now: datetime | None = None,
    window: timedelta = DEFAULT_BACKFILL_WINDOW,
    archive_event: Callable[[Mapping[str, Any]], Awaitable[object]] | None = None,
) -> QQHistoryBackfillReport:
    """Replay inbound private messages missed while the adapter was down.

    Every failure is contained: a provider without the history action, a
    malformed record, or one failing turn degrades to a logged skip.  The
    scheduler and live push ingress never depend on this pass.

    ``archive_event`` is the optional attachment-bytes hook used by the
    perception deployment; it runs for every in-window inbound event (even
    already-deduplicated ones, because a crash may have accepted the message
    without archiving its bytes) and owns its own failures.
    """

    at = now or datetime.now(UTC)
    try:
        messages = await fetch_history()
    except Exception as exc:  # noqa: BLE001 - startup compensation is best-effort
        logger.warning(
            "QQ history backfill unavailable (%s); relying on live push only",
            type(exc).__name__,
        )
        return QQHistoryBackfillReport(error=type(exc).__name__)

    replayed = deduplicated = skipped = failed = 0
    statuses: list[str] = []
    ordered = sorted(
        messages,
        key=lambda item: (
            float(item.get("time"))
            if isinstance(item.get("time"), (int, float))
            else 0.0
        ),
    )
    for message in ordered:
        event = history_message_to_onebot_event(message, recipient_id=recipient_id)
        if event is None:
            skipped += 1
            continue
        try:
            fragment = normalize_onebot_qq_ingress(event)
        except (TypeError, ValueError):
            skipped += 1
            continue
        if fragment is None or fragment.content_shape == "control":
            skipped += 1
            continue
        if at - fragment.observed_at > window:
            skipped += 1
            continue
        if archive_event is not None and fragment.attachment_refs:
            try:
                await archive_event(event)
            except Exception:  # noqa: BLE001 - bytes archiving is best-effort
                logger.exception(
                    "QQ history backfill attachment archiving failed for %s",
                    fragment.source_event_id,
                )
        try:
            already = host.submission_state(fragment.source_event_id)
        except AttributeError:
            already = None
        if already is not None:
            deduplicated += 1
            continue
        try:
            result = await host.inbound_fragment(fragment)
        except Exception:  # noqa: BLE001 - one bad record must not stop the rest
            logger.exception(
                "QQ history backfill failed for message %s", fragment.source_event_id
            )
            failed += 1
            continue
        statuses.append(result.status)
        replayed += 1
    if replayed or failed:
        logger.info(
            "QQ history backfill fetched=%s replayed=%s deduplicated=%s skipped=%s failed=%s",
            len(messages), replayed, deduplicated, skipped, failed,
        )
    return QQHistoryBackfillReport(
        fetched=len(messages),
        replayed=replayed,
        deduplicated=deduplicated,
        skipped=skipped,
        failed=failed,
        statuses=tuple(statuses),
    )


__all__ = [
    "DEFAULT_BACKFILL_COUNT",
    "DEFAULT_BACKFILL_WINDOW",
    "QQHistoryBackfillReport",
    "backfill_missed_private_messages",
    "history_message_to_onebot_event",
]

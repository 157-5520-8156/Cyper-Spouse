"""Deterministic, model-safe view of the wishes she quietly holds.

Active aspirations enter the chat model's view through the existing
Inner-Advisory channel (the same read-only committed-fact injection used by
the response-expectation view): the Context Capsule's ``advisories`` slice
re-verifies every ``source_ref`` against committed ledger events, so a wish
can only reach expression with its planting event as authority — "我一直想去
日本" is always ledger-backed prose, never model invention.

Everything here is a pure read over one pinned projection.  No IDs or hashes
leak into the model-visible summary; the advisory envelope binds the sources.
"""

from __future__ import annotations

from datetime import timedelta
import hashlib
import json

from .context_capsule import InnerAdvisoryCandidate, InnerAdvisoryProjection
from .schemas import AspirationProjection, LedgerProjection


ASPIRATION_ADVISORY_VERSION = "aspiration-view.1"

# A handful of wishes is texture; a list of them is an agenda.  Keep the
# advisory small and stable (newest wishes first).
_MAX_VISIBLE_ASPIRATIONS = 3


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _aspiration_summary(aspiration: AspirationProjection, *, held_days: int) -> str:
    """Compress one wish into a bounded read-only hint.

    The wish text is copied verbatim from the reviewed seed via the committed
    projection; only the duration phrasing is derived.
    """

    if held_days <= 0:
        held = "最近才冒出来的念头"
    elif held_days < 30:
        held = f"心里存了大约 {held_days} 天"
    else:
        held = f"心里存了大约 {held_days // 30} 个月"
    return f"她心里一直存着的念头：{aspiration.text}（{held}）"[:256]


def active_aspiration_advisories(
    projection: LedgerProjection,
) -> tuple[InnerAdvisoryProjection, ...]:
    """Wrap her active wishes in the ordinary non-authoritative advisory envelope."""

    logical_time = projection.logical_time
    if logical_time is None:
        return ()
    active = sorted(
        (item for item in projection.aspirations if item.status == "active"),
        key=lambda item: (item.planted_at, item.aspiration_id),
        reverse=True,
    )[:_MAX_VISIBLE_ASPIRATIONS]
    if not active:
        return ()
    candidates = tuple(
        InnerAdvisoryCandidate(
            candidate_ref="aspiration:" + _digest(item.planted_event_ref),
            value=_aspiration_summary(
                item,
                held_days=max(0, int((logical_time - item.planted_at).total_seconds() // 86_400)),
            ),
            weight_bp=10_000,
            confidence_bp=10_000,
        )
        for item in active
    )
    return (
        InnerAdvisoryProjection(
            advisory_id="advisory:aspirations:" + _digest(
                tuple(item.planted_event_ref for item in active)
            ),
            kind="active_aspirations",
            source_refs=tuple(item.planted_event_ref for item in active),
            candidate_refs=tuple(item.candidate_ref for item in candidates),
            candidates=candidates,
            confidence_bp=10_000,
            # A short-lived deliberation aid anchored to the pinned durable
            # head, not wall-clock process time.
            expiry=logical_time + timedelta(days=1),
            producer_version=ASPIRATION_ADVISORY_VERSION,
        ),
    )


__all__ = [
    "ASPIRATION_ADVISORY_VERSION",
    "active_aspiration_advisories",
]

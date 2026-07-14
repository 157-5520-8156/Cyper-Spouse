"""Immutable authority projection for the shared logical clock."""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
from types import MappingProxyType

from .schemas import ClockTransitionProjection, WorldEvent


CLOCK_AUTHORITY_POLICY_VERSION = "world-clock-authority.1"
_CLOCK_AUTHORITY_POLICY_ARTIFACT = {
    "version": CLOCK_AUTHORITY_POLICY_VERSION,
    "event_type": "ClockAdvanced",
    "computed_fields": [
        "clock_event_ref",
        "computed_world_revision",
        "payload_hash",
    ],
    "timezone_rule": "all-clock-interval-times-aware",
    "append_rules": [
        "from-equals-current-logical-time-when-current-exists",
        "to-strictly-after-from",
        "event-ref-from-envelope",
        "world-revision-computed-at-reducer-position",
        "payload-hash-from-canonical-event-payload",
        "event-ref-and-computed-revision-unique",
        "computed-revisions-strictly-increase",
        "policy-selected-by-installed-event-schema-not-payload",
    ],
    "history_rules": [
        "gaps-after-non-clock-logical-advance-are-tolerated",
        "overlaps-or-backward-clock-intervals-are-forbidden",
        "non-clock-events-may-make-latest-clock-stale",
        "every-frozen-policy-pair-must-exactly-match-registry",
    ],
    "resolver_rules": [
        "latest-is-max-computed-world-revision",
        "latest-to-must-equal-current-logical-time",
        "stale-history-fails-closed",
        "latest-policy-pair-must-exactly-match-registry",
    ],
}
CLOCK_AUTHORITY_POLICY_DIGEST = hashlib.sha256(
    json.dumps(
        _CLOCK_AUTHORITY_POLICY_ARTIFACT,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
).hexdigest()
INSTALLED_CLOCK_AUTHORITY_POLICIES = MappingProxyType(
    {CLOCK_AUTHORITY_POLICY_VERSION: CLOCK_AUTHORITY_POLICY_DIGEST}
)


def clock_policy_is_installed(*, version: str, digest: str) -> bool:
    return INSTALLED_CLOCK_AUTHORITY_POLICIES.get(version) == digest


def validate_clock_history(
    history: tuple[ClockTransitionProjection, ...],
    *,
    current_logical_time: datetime | None = None,
    require_current: bool = False,
) -> None:
    revisions = tuple(item.computed_world_revision for item in history)
    refs = tuple(item.clock_event_ref for item in history)
    if revisions != tuple(sorted(set(revisions))):
        raise ValueError("clock authority history is not revision ordered")
    if len(refs) != len(set(refs)):
        raise ValueError("clock authority history contains a duplicate event")
    if any(
        item.logical_time_from.tzinfo is None
        or item.logical_time_from.utcoffset() is None
        or item.logical_time_to.tzinfo is None
        or item.logical_time_to.utcoffset() is None
        or item.logical_time_to <= item.logical_time_from
        or not clock_policy_is_installed(
            version=item.installed_policy_version,
            digest=item.installed_policy_digest,
        )
        for item in history
    ):
        raise ValueError("clock authority history contains an invalid authority entry")
    if any(
        after.logical_time_from < before.logical_time_to
        or after.logical_time_to <= before.logical_time_to
        for before, after in zip(history, history[1:], strict=False)
    ):
        raise ValueError("clock authority history overlaps or moves backward")
    if history:
        if current_logical_time is None or history[-1].logical_time_to > current_logical_time:
            raise ValueError("clock authority history is ahead of logical time")
        if require_current and history[-1].logical_time_to != current_logical_time:
            raise ValueError("latest Clock authority is not installed or current")


def append_clock_transition(
    history: tuple[ClockTransitionProjection, ...],
    *,
    event: WorldEvent,
    current_logical_time: datetime | None,
    computed_world_revision: int,
) -> tuple[ClockTransitionProjection, ...]:
    validate_clock_history(
        history,
        current_logical_time=current_logical_time,
    )
    if event.event_type != "ClockAdvanced":
        raise ValueError("clock authority only accepts ClockAdvanced")
    payload = event.payload()
    raw_from = payload.get("logical_time_from")
    raw_to = payload.get("logical_time_to")
    if not isinstance(raw_from, str) or not isinstance(raw_to, str):
        raise ValueError("ClockAdvanced requires exact from/to timestamps")
    origin = datetime.fromisoformat(raw_from)
    target = datetime.fromisoformat(raw_to)
    if (
        origin.tzinfo is None
        or origin.utcoffset() is None
        or target.tzinfo is None
        or target.utcoffset() is None
    ):
        raise ValueError("ClockAdvanced timestamps must be timezone-aware")
    if target <= origin:
        raise ValueError("ClockAdvanced logical time must move forward")
    if current_logical_time is not None and origin != current_logical_time:
        raise ValueError("ClockAdvanced from does not match current logical time")
    if history and computed_world_revision <= history[-1].computed_world_revision:
        raise ValueError("clock history world revisions must strictly increase")
    if any(
        item.clock_event_ref == event.event_id
        or item.computed_world_revision == computed_world_revision
        for item in history
    ):
        raise ValueError("clock history event and world revision must be unique")
    projection = ClockTransitionProjection(
        clock_event_ref=event.event_id,
        computed_world_revision=computed_world_revision,
        payload_hash=event.payload_hash,
        logical_time_from=origin,
        logical_time_to=target,
        installed_policy_version=CLOCK_AUTHORITY_POLICY_VERSION,
        installed_policy_digest=CLOCK_AUTHORITY_POLICY_DIGEST,
    )
    updated = (*history, projection)
    validate_clock_history(
        updated,
        current_logical_time=target,
        require_current=True,
    )
    return updated


def resolve_latest_clock(
    history: tuple[ClockTransitionProjection, ...],
    *,
    current_logical_time: datetime | None,
) -> ClockTransitionProjection:
    if not history or current_logical_time is None:
        raise ValueError("latest Clock authority is unavailable")
    validate_clock_history(
        history,
        current_logical_time=current_logical_time,
        require_current=True,
    )
    latest = max(history, key=lambda item: item.computed_world_revision)
    if (
        not clock_policy_is_installed(
            version=latest.installed_policy_version,
            digest=latest.installed_policy_digest,
        )
    ):
        raise ValueError("latest Clock authority is not installed or current")
    return latest

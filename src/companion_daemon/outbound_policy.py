"""Pure, replayable policy for deciding whether an outbound action may proceed."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from enum import StrEnum
import re
from typing import FrozenSet, Mapping
import unicodedata


class OutboundKind(StrEnum):
    REPLY = "reply"
    PULSE = "pulse"
    LIFE_SHARE = "life_share"
    FOLLOWUP = "followup"
    MEDIA = "media"
    REACTION = "reaction"
    TOOL = "tool"


@dataclass(frozen=True)
class OutboundRequest:
    request_id: str
    kind: OutboundKind
    trigger: str
    text: str | None
    now: datetime
    topic_key: str | None = None


@dataclass(frozen=True)
class RecentOutbound:
    request_id: str
    kind: OutboundKind
    trigger: str
    text: str | None
    topic_key: str | None
    occurred_at: datetime


@dataclass(frozen=True)
class OutboundProjection:
    """The outbound-related slice of a deterministic World projection."""

    last_outbound_at: datetime | None = None
    trigger_last_outbound_at: Mapping[str, datetime] = field(default_factory=dict)
    unanswered_outbound_count: int = 0
    generation_lock_owner: str | None = None
    generation_lock_expires_at: datetime | None = None
    recent_outbounds: tuple[RecentOutbound, ...] = ()


@dataclass(frozen=True)
class OutboundPolicy:
    """Tunable limits; exemptions remain explicit and visible in guard results."""

    global_cooldown: timedelta = timedelta(minutes=5)
    trigger_cooldowns: Mapping[str, timedelta] = field(default_factory=dict)
    max_unanswered: int = 2
    duplicate_window: timedelta = timedelta(hours=24)
    similarity_window: timedelta = timedelta(hours=6)
    text_similarity_threshold: float = 0.9
    global_cooldown_exempt_kinds: FrozenSet[OutboundKind] = frozenset({OutboundKind.REPLY})
    unanswered_exempt_kinds: FrozenSet[OutboundKind] = frozenset({OutboundKind.REPLY})
    duplicate_exempt_kinds: FrozenSet[OutboundKind] = frozenset({OutboundKind.REPLY})


@dataclass(frozen=True)
class GuardCheck:
    name: str
    passed: bool
    detail: str
    retry_at: datetime | None = None


@dataclass(frozen=True)
class OutboundAllowance:
    checks: tuple[GuardCheck, ...]
    allowed: bool = field(init=False)
    reasons: tuple[str, ...] = field(init=False)
    retry_at: datetime | None = field(init=False)

    def __post_init__(self) -> None:
        failed = tuple(check for check in self.checks if not check.passed)
        object.__setattr__(self, "allowed", not failed)
        object.__setattr__(self, "reasons", tuple(check.name for check in failed))
        retry_times = [check.retry_at for check in failed if check.retry_at is not None]
        object.__setattr__(self, "retry_at", max(retry_times, default=None))

    def check(self, name: str) -> GuardCheck:
        return next(check for check in self.checks if check.name == name)


def evaluate_outbound(
    request: OutboundRequest,
    projection: OutboundProjection,
    policy: OutboundPolicy = OutboundPolicy(),
) -> OutboundAllowance:
    checks: list[GuardCheck] = []
    if request.kind in policy.global_cooldown_exempt_kinds:
        global_check = GuardCheck("global_cooldown", True, "kind exempt")
    elif projection.last_outbound_at is None:
        global_check = GuardCheck("global_cooldown", True, "no prior outbound")
    else:
        retry_at = projection.last_outbound_at + policy.global_cooldown
        remaining = retry_at - request.now
        if remaining > timedelta(0):
            minutes = max(1, int((remaining.total_seconds() + 59) // 60))
            global_check = GuardCheck(
                "global_cooldown", False, f"{minutes}m remaining", retry_at=retry_at
            )
        else:
            global_check = GuardCheck("global_cooldown", True, "elapsed")
    checks.append(global_check)

    trigger_duration = policy.trigger_cooldowns.get(request.trigger)
    trigger_last = projection.trigger_last_outbound_at.get(request.trigger)
    if trigger_duration is None:
        checks.append(GuardCheck("trigger_cooldown", True, "not configured"))
    elif trigger_last is None:
        checks.append(GuardCheck("trigger_cooldown", True, "no prior trigger"))
    else:
        trigger_retry = trigger_last + trigger_duration
        if trigger_retry > request.now:
            checks.append(
                GuardCheck(
                    "trigger_cooldown",
                    False,
                    f"trigger {request.trigger} is cooling down",
                    retry_at=trigger_retry,
                )
            )
        else:
            checks.append(GuardCheck("trigger_cooldown", True, "elapsed"))

    if request.kind in policy.unanswered_exempt_kinds:
        checks.append(GuardCheck("unanswered_budget", True, "kind exempt"))
    elif projection.unanswered_outbound_count >= policy.max_unanswered:
        checks.append(
            GuardCheck(
                "unanswered_budget",
                False,
                f"{projection.unanswered_outbound_count}/{policy.max_unanswered} used",
            )
        )
    else:
        checks.append(
            GuardCheck(
                "unanswered_budget",
                True,
                f"{projection.unanswered_outbound_count}/{policy.max_unanswered} used",
            )
        )

    lock_active = (
        projection.generation_lock_owner is not None
        and (
            projection.generation_lock_expires_at is None
            or projection.generation_lock_expires_at > request.now
        )
    )
    if lock_active and projection.generation_lock_owner != request.request_id:
        checks.append(
            GuardCheck(
                "generation_lock",
                False,
                f"held by {projection.generation_lock_owner}",
                retry_at=projection.generation_lock_expires_at,
            )
        )
    elif lock_active:
        checks.append(GuardCheck("generation_lock", True, "owned by request"))
    else:
        checks.append(GuardCheck("generation_lock", True, "available"))

    duplicate = (
        None
        if request.kind in policy.duplicate_exempt_kinds
        else _find_duplicate(request, projection.recent_outbounds, policy)
    )
    if request.kind in policy.duplicate_exempt_kinds:
        checks.append(GuardCheck("duplicate", True, "kind exempt"))
    elif duplicate is None:
        checks.append(GuardCheck("duplicate", True, "no duplicate"))
    else:
        match, basis = duplicate
        checks.append(GuardCheck("duplicate", False, f"matches {match.request_id} {basis}"))

    similar = None if duplicate is not None else _find_similar_topic(
        request, projection.recent_outbounds, policy
    )
    if similar is None:
        detail = "covered by duplicate" if duplicate is not None else "no similar topic"
        checks.append(GuardCheck("topic_similarity", True, detail))
    else:
        checks.append(
            GuardCheck("topic_similarity", False, f"same topic as {similar.request_id}")
        )

    return OutboundAllowance(tuple(checks))


def _find_duplicate(
    request: OutboundRequest,
    recent: tuple[RecentOutbound, ...],
    policy: OutboundPolicy,
) -> tuple[RecentOutbound, str] | None:
    for item in recent:
        if item.request_id == request.request_id:
            return item, "request_id"
    candidate = _normalize_text(request.text)
    if not candidate:
        return None
    for item in recent:
        age = request.now - item.occurred_at
        if age < timedelta(0) or age > policy.duplicate_window:
            continue
        previous = _normalize_text(item.text)
        if not previous:
            continue
        if candidate == previous:
            return item, "text"
        if SequenceMatcher(None, candidate, previous).ratio() >= policy.text_similarity_threshold:
            return item, "text"
    return None


def _find_similar_topic(
    request: OutboundRequest,
    recent: tuple[RecentOutbound, ...],
    policy: OutboundPolicy,
) -> RecentOutbound | None:
    if not request.topic_key:
        return None
    topic = request.topic_key.strip().casefold()
    for item in recent:
        age = request.now - item.occurred_at
        if age < timedelta(0) or age > policy.similarity_window or not item.topic_key:
            continue
        if item.topic_key.strip().casefold() == topic:
            return item
    return None


def _normalize_text(text: str | None) -> str:
    if not text:
        return ""
    normalized = unicodedata.normalize("NFKC", text).casefold()
    return re.sub(r"[^\w]+", "", normalized, flags=re.UNICODE)

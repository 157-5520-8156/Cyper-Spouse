"""Deterministic logical-time cost accounting for World external work."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from math import ceil
from types import MappingProxyType
from typing import Iterable, Literal, Mapping, cast


CostCategory = Literal[
    "chat", "repair", "audit", "proactive", "vision", "audio", "image", "tool"
]
ALL_COST_CATEGORIES: tuple[CostCategory, ...] = (
    "chat",
    "repair",
    "audit",
    "proactive",
    "vision",
    "audio",
    "image",
    "tool",
)


@dataclass(frozen=True)
class CostPolicy:
    """Hard and automatic (soft) integer-unit limits for one logical day."""

    daily_budget_units: int
    automatic_daily_budget_units: int
    category_daily_budget_units: Mapping[CostCategory, int] = field(default_factory=dict)
    category_automatic_daily_budget_units: Mapping[CostCategory, int] = field(
        default_factory=dict
    )

    def __post_init__(self) -> None:
        if type(self.daily_budget_units) is not int or self.daily_budget_units <= 0:
            raise ValueError("daily budget units must be positive")
        if (
            type(self.automatic_daily_budget_units) is not int
            or self.automatic_daily_budget_units < 0
            or self.automatic_daily_budget_units > self.daily_budget_units
        ):
            raise ValueError("automatic daily budget must be between zero and daily budget")
        hard = dict(self.category_daily_budget_units)
        automatic = dict(self.category_automatic_daily_budget_units)
        for category, limit in (*hard.items(), *automatic.items()):
            if category not in ALL_COST_CATEGORIES:
                raise ValueError(f"unknown cost category: {category}")
            if type(limit) is not int or limit < 0:
                raise ValueError("category budget units must be non-negative")
        for category, limit in automatic.items():
            if limit > hard.get(category, self.daily_budget_units):
                raise ValueError("automatic category budget cannot exceed its hard budget")
        object.__setattr__(self, "category_daily_budget_units", MappingProxyType(hard))
        object.__setattr__(
            self,
            "category_automatic_daily_budget_units",
            MappingProxyType(automatic),
        )


@dataclass(frozen=True)
class CostRequest:
    idempotency_key: str
    category: CostCategory
    logical_day: str
    units: int
    automatic: bool
    cache_key: str | None = None

    def __post_init__(self) -> None:
        if not self.idempotency_key.strip():
            raise ValueError("idempotency key must not be empty")
        if self.category not in ALL_COST_CATEGORIES:
            raise ValueError(f"unknown cost category: {self.category}")
        if not self.logical_day.strip():
            raise ValueError("logical day must not be empty")
        if type(self.units) is not int or self.units <= 0:
            raise ValueError("units must be positive")
        if self.cache_key is not None and not self.cache_key.strip():
            raise ValueError("cache key must not be empty")


@dataclass(frozen=True)
class CostDecision:
    allowed: bool
    reason: str
    reservation_id: str | None = None
    charge_units: int = 0
    reused: bool = False
    reused_result_ref: str | None = None


@dataclass(frozen=True)
class CostUsage:
    reserved_units: int
    settled_units: int

    @property
    def total_units(self) -> int:
        return self.reserved_units + self.settled_units


@dataclass(frozen=True)
class CostSettlement:
    reservation_id: str
    charged_units: int
    reason: str = "settled"
    result_ref: str | None = None


@dataclass(frozen=True)
class CostLedgerEvent:
    event_type: str
    payload: Mapping[str, object]


@dataclass(frozen=True)
class SocialTransgressionPolicy:
    """Frequency budget for social-risk overrides, never safety or consent."""

    daily_strike_budget: int
    cooldown: timedelta


@dataclass(frozen=True)
class SocialTransgressionRecord:
    idempotency_key: str
    logical_at: datetime
    strikes: int


@dataclass(frozen=True)
class SocialTransgressionDecision:
    allowed: bool
    reason: str
    strikes_charged: int
    cooldown_remaining_seconds: int = 0


def evaluate_social_transgression(
    policy: SocialTransgressionPolicy,
    history: Iterable[SocialTransgressionRecord],
    *,
    logical_at: datetime,
    requested_strikes: int,
) -> SocialTransgressionDecision:
    """Price a soft social override against logical-time frequency limits.

    Callers must enforce safety, privacy, legal, and consent hard invariants
    before this helper. Those checks are intentionally not convertible into
    spendable strikes.
    """
    if requested_strikes <= 0:
        raise ValueError("requested strikes must be positive")
    if policy.daily_strike_budget <= 0:
        raise ValueError("daily strike budget must be positive")
    if policy.cooldown < timedelta(0):
        raise ValueError("cooldown must be non-negative")
    records_by_key: dict[str, SocialTransgressionRecord] = {}
    for record in history:
        if not record.idempotency_key.strip():
            raise ValueError("transgression idempotency key must not be empty")
        existing = records_by_key.get(record.idempotency_key)
        if existing is not None and existing != record:
            raise ValueError("idempotency key reused with different transgression")
        records_by_key[record.idempotency_key] = record
    records = tuple(records_by_key.values())
    if any(record.strikes <= 0 for record in records):
        raise ValueError("recorded strikes must be positive")
    if any(record.logical_at > logical_at for record in records):
        raise ValueError("transgression history is ahead of logical time")
    if records:
        elapsed = logical_at - max(record.logical_at for record in records)
        remaining = policy.cooldown - elapsed
        if remaining > timedelta(0):
            return SocialTransgressionDecision(
                False,
                "transgression_cooldown",
                0,
                ceil(remaining.total_seconds()),
            )
    used_today = sum(
        record.strikes
        for record in records
        if record.logical_at.date() == logical_at.date()
    )
    if used_today + requested_strikes > policy.daily_strike_budget:
        return SocialTransgressionDecision(
            False,
            "daily_transgression_strike_budget_exceeded",
            0,
        )
    return SocialTransgressionDecision(
        True,
        "social_risk_budget_available",
        requested_strikes,
    )


class WorldCostLedger:
    """In-memory deterministic projection; integration persists its exported events."""

    def __init__(self, policy: CostPolicy):
        self.policy = policy
        self._reservations: dict[str, CostRequest] = {}
        self._reserve_receipts: dict[str, tuple[CostRequest, CostDecision]] = {}
        self._settled_units: dict[str, int] = {}
        self._settle_receipts: dict[
            str, tuple[tuple[str, int, str | None], CostSettlement]
        ] = {}
        self._release_receipts: dict[
            str, tuple[tuple[str, str], CostSettlement]
        ] = {}
        self._cache: dict[tuple[CostCategory, str], str] = {}
        self._events: list[CostLedgerEvent] = []

    def reserve(self, request: CostRequest) -> CostDecision:
        existing = self._reserve_receipts.get(request.idempotency_key)
        if existing is not None:
            original_request, decision = existing
            if original_request != request:
                raise ValueError("idempotency key reused with different request")
            return decision
        if request.cache_key:
            result_ref = self._cache.get((request.category, request.cache_key))
            if result_ref is not None:
                return self._remember(
                    request,
                    CostDecision(
                        True,
                        "cache_reused",
                        charge_units=0,
                        reused=True,
                        reused_result_ref=result_ref,
                    ),
                )
        usage = self.usage(request.logical_day, request.category)
        total = self.usage(request.logical_day)
        category_pending = usage.total_units + request.units
        total_pending = total.total_units + request.units
        category_auto_limit = self.policy.category_automatic_daily_budget_units.get(
            request.category, self.policy.automatic_daily_budget_units
        )
        category_limit = self.policy.category_daily_budget_units.get(
            request.category, self.policy.daily_budget_units
        )
        if category_pending > category_limit:
            return self._remember(request, CostDecision(False, "category_daily_budget_exceeded"))
        if total_pending > self.policy.daily_budget_units:
            return self._remember(request, CostDecision(False, "daily_budget_exceeded"))
        if request.automatic and category_pending > category_auto_limit:
            return self._remember(
                request,
                CostDecision(False, "category_automatic_daily_budget_exceeded"),
            )
        if request.automatic and total_pending > self.policy.automatic_daily_budget_units:
            return self._remember(request, CostDecision(False, "automatic_daily_budget_exceeded"))
        reservation_id = f"cost:{request.idempotency_key}"
        self._reservations[reservation_id] = request
        return self._remember(
            request, CostDecision(True, "reserved", reservation_id, request.units)
        )

    def _remember(self, request: CostRequest, decision: CostDecision) -> CostDecision:
        self._reserve_receipts[request.idempotency_key] = (request, decision)
        self._events.append(
            CostLedgerEvent(
                "CostReservationDecided",
                {
                    "request": {
                        "idempotency_key": request.idempotency_key,
                        "category": request.category,
                        "logical_day": request.logical_day,
                        "units": request.units,
                        "automatic": request.automatic,
                        "cache_key": request.cache_key,
                    },
                    "decision": {
                        "allowed": decision.allowed,
                        "reason": decision.reason,
                        "reservation_id": decision.reservation_id,
                        "charge_units": decision.charge_units,
                        "reused": decision.reused,
                        "reused_result_ref": decision.reused_result_ref,
                    },
                },
            )
        )
        return decision

    def settle(
        self,
        reservation_id: str | None,
        *,
        actual_units: int,
        idempotency_key: str,
        result_ref: str | None = None,
    ) -> CostSettlement:
        if reservation_id is None or reservation_id not in self._reservations:
            raise ValueError("unknown reservation")
        command = (reservation_id, actual_units, result_ref)
        existing = self._settle_receipts.get(idempotency_key)
        if existing is not None:
            original_command, settlement = existing
            if original_command != command:
                raise ValueError("idempotency key reused with different settlement")
            return settlement
        if reservation_id in self._settled_units:
            raise ValueError("reservation already settled")
        if actual_units < 0:
            raise ValueError("actual units must be non-negative")
        if actual_units > self._reservations[reservation_id].units:
            raise ValueError("actual units exceed reservation")
        settlement = CostSettlement(reservation_id, actual_units, result_ref=result_ref)
        self._settled_units[reservation_id] = actual_units
        self._settle_receipts[idempotency_key] = (command, settlement)
        request = self._reservations[reservation_id]
        if request.cache_key and result_ref:
            self._cache[(request.category, request.cache_key)] = result_ref
        self._events.append(
            CostLedgerEvent(
                "CostReservationSettled",
                {
                    "reservation_id": reservation_id,
                    "actual_units": actual_units,
                    "idempotency_key": idempotency_key,
                    "result_ref": result_ref,
                },
            )
        )
        return settlement

    def release(
        self,
        reservation_id: str | None,
        *,
        idempotency_key: str,
        reason: str = "released_before_dispatch",
    ) -> CostSettlement:
        if reservation_id is None or reservation_id not in self._reservations:
            raise ValueError("unknown reservation")
        command = (reservation_id, reason)
        existing = self._release_receipts.get(idempotency_key)
        if existing is not None:
            original_command, settlement = existing
            if original_command != command:
                raise ValueError("idempotency key reused with different release")
            return settlement
        if reservation_id in self._settled_units:
            raise ValueError("reservation already settled")
        settlement = CostSettlement(reservation_id, 0, reason=reason)
        self._settled_units[reservation_id] = 0
        self._release_receipts[idempotency_key] = (command, settlement)
        self._events.append(
            CostLedgerEvent(
                "CostReservationReleased",
                {
                    "reservation_id": reservation_id,
                    "idempotency_key": idempotency_key,
                    "reason": reason,
                },
            )
        )
        return settlement

    def export_events(self) -> tuple[CostLedgerEvent, ...]:
        """Return detached, JSON-shaped records suitable for World persistence."""
        return tuple(deepcopy(self._events))

    @classmethod
    def from_events(
        cls,
        policy: CostPolicy,
        events: Iterable[CostLedgerEvent],
    ) -> WorldCostLedger:
        ledger = cls(policy)
        for expected in events:
            payload = expected.payload
            if expected.event_type == "CostReservationDecided":
                request_payload = cast(Mapping[str, object], payload["request"])
                category = str(request_payload["category"])
                if category not in ALL_COST_CATEGORIES:
                    raise ValueError(f"unknown cost category in replay: {category}")
                ledger.reserve(
                    CostRequest(
                        idempotency_key=str(request_payload["idempotency_key"]),
                        category=cast(CostCategory, category),
                        logical_day=str(request_payload["logical_day"]),
                        units=int(request_payload["units"]),
                        automatic=bool(request_payload["automatic"]),
                        cache_key=(
                            str(request_payload["cache_key"])
                            if request_payload.get("cache_key") is not None
                            else None
                        ),
                    )
                )
            elif expected.event_type == "CostReservationSettled":
                ledger.settle(
                    str(payload["reservation_id"]),
                    actual_units=int(payload["actual_units"]),
                    idempotency_key=str(payload["idempotency_key"]),
                    result_ref=(
                        str(payload["result_ref"])
                        if payload.get("result_ref") is not None
                        else None
                    ),
                )
            elif expected.event_type == "CostReservationReleased":
                ledger.release(
                    str(payload["reservation_id"]),
                    idempotency_key=str(payload["idempotency_key"]),
                    reason=str(payload["reason"]),
                )
            else:
                raise ValueError(f"unknown cost ledger event: {expected.event_type}")
            if ledger._events[-1] != expected:
                raise ValueError("cost ledger replay diverged from recorded decision")
        return ledger

    def usage(self, logical_day: str, category: CostCategory | None = None) -> CostUsage:
        matching = {
            reservation_id: request
            for reservation_id, request in self._reservations.items()
            if request.logical_day == logical_day
            and (category is None or request.category == category)
        }
        reserved = sum(
            request.units
            for reservation_id, request in matching.items()
            if reservation_id not in self._settled_units
        )
        settled = sum(
            self._settled_units[reservation_id]
            for reservation_id in matching
            if reservation_id in self._settled_units
        )
        return CostUsage(reserved_units=reserved, settled_units=settled)

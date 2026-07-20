"""Deployment-owned civil-time interpretation for World v2.

Ledger timestamps remain timezone-aware instants.  ``LocalChronology`` only
controls how an instant is presented and classified for the companion's local
life; it never rewrites event ordering or elapsed-time arithmetic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


@dataclass(frozen=True, slots=True)
class LocalChronology:
    """A validated IANA timezone used for companion-local civil time."""

    timezone_name: str = "Asia/Shanghai"
    _timezone: ZoneInfo = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not self.timezone_name:
            raise ValueError("local timezone name must not be empty")
        try:
            timezone = ZoneInfo(self.timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown local timezone: {self.timezone_name}") from exc
        object.__setattr__(self, "_timezone", timezone)

    def localize(self, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("local chronology requires a timezone-aware instant")
        return value.astimezone(self._timezone)


__all__ = ["LocalChronology"]

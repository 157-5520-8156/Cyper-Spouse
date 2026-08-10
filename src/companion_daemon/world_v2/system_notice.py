"""Durable, platform-authored technical notices for the QQ host.

System Notices are deliberately outside Character Expression and the World
ledger.  They cannot create memories, relationship facts, affect, or shared
history.  This sidecar owns only effect-once dispatch evidence and persistent
rate limiting for a fixed platform message.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import hashlib
import json
import sqlite3
from threading import RLock
from typing import Callable, Literal, Mapping, Protocol

from pydantic import Field

from .schema_core import FrozenModel
from .sqlite_coordination import configure_shared_sqlite_connection, sqlite_write_lock


SYSTEM_NOTICE_TEXT = (
    "【系统提示】回复服务暂时没有完成这次响应，请稍后再试。"
    "这不是角色选择了沉默。"
)


class SystemNoticeDelivery(Protocol):
    async def send_text(self, recipient_id: str, text: str) -> dict[str, object]: ...


class SystemNoticeResult(FrozenModel):
    notice_key: str = Field(min_length=1)
    status: Literal["provider_accepted", "unknown", "suppressed", "already_attempted"]
    provider_ref: str | None = None
    error_class: str | None = None


def _provider_ref(response: object) -> str | None:
    candidates = [response]
    if isinstance(response, Mapping) and isinstance(response.get("data"), Mapping):
        candidates.append(response["data"])
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            continue
        for key in ("message_id", "id", "msg_id"):
            value = candidate.get(key)
            if value not in {None, ""}:
                return f"platform:{key}:{value}"
    return None


class SQLiteSystemNoticeDispatcher:
    """Effect-once and rate-limited technical notice dispatcher."""

    def __init__(
        self,
        *,
        path: str,
        world_id: str,
        delivery: SystemNoticeDelivery,
        now: Callable[[], datetime] | None = None,
        cooldown_seconds: int = 60,
    ) -> None:
        if not path or not world_id or cooldown_seconds < 0:
            raise ValueError("system notice composition is incomplete")
        self._world_id = world_id
        self._delivery = delivery
        self._now = now or (lambda: datetime.now(UTC))
        self._cooldown = timedelta(seconds=cooldown_seconds)
        self._lock = RLock()
        self._send_lock = asyncio.Lock()
        self._database_write_lock = sqlite_write_lock(path)
        self._connection = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        with self._database_write_lock:
            configure_shared_sqlite_connection(self._connection)
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS world_v2_system_notice_dispatch (
                    world_id TEXT NOT NULL,
                    notice_key TEXT NOT NULL,
                    failure_code TEXT NOT NULL,
                    attempted_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    provider_ref TEXT,
                    response_hash TEXT,
                    error_class TEXT,
                    PRIMARY KEY (world_id, notice_key)
                )
                """
            )

    async def notify(
        self,
        *,
        notice_key: str,
        recipient_id: str,
        failure_code: str,
    ) -> SystemNoticeResult:
        if not notice_key or not recipient_id or not failure_code:
            raise ValueError("system notice coordinates are required")
        async with self._send_lock:
            existing = self._stored(notice_key)
            if existing is not None:
                return SystemNoticeResult(
                    notice_key=notice_key,
                    status="already_attempted",
                    provider_ref=existing[1],
                    error_class=existing[2],
                )
            attempted_at = self._now()
            if attempted_at.tzinfo is None or attempted_at.utcoffset() is None:
                raise ValueError("system notice clock must be timezone-aware")
            if self._inside_cooldown(attempted_at):
                self._insert_attempt(
                    notice_key=notice_key,
                    failure_code=failure_code,
                    attempted_at=attempted_at,
                    status="suppressed",
                )
                return SystemNoticeResult(notice_key=notice_key, status="suppressed")
            # Persist uncertainty before crossing the external provider seam.
            self._insert_attempt(
                notice_key=notice_key,
                failure_code=failure_code,
                attempted_at=attempted_at,
                status="dispatch_started",
            )
            try:
                response = await self._delivery.send_text(recipient_id, SYSTEM_NOTICE_TEXT)
            except Exception as exc:
                self._finish(
                    notice_key=notice_key,
                    status="unknown",
                    error_class=type(exc).__name__,
                )
                return SystemNoticeResult(
                    notice_key=notice_key,
                    status="unknown",
                    error_class=type(exc).__name__,
                )
            encoded = json.dumps(
                response,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=lambda value: type(value).__name__,
            ).encode()
            provider_ref = _provider_ref(response)
            self._finish(
                notice_key=notice_key,
                status="provider_accepted",
                provider_ref=provider_ref,
                response_hash="sha256:" + hashlib.sha256(encoded).hexdigest(),
            )
            return SystemNoticeResult(
                notice_key=notice_key,
                status="provider_accepted",
                provider_ref=provider_ref,
            )

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def _stored(self, notice_key: str) -> tuple[str, str | None, str | None] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT status, provider_ref, error_class "
                "FROM world_v2_system_notice_dispatch WHERE world_id=? AND notice_key=?",
                (self._world_id, notice_key),
            ).fetchone()
        return None if row is None else (str(row[0]), row[1], row[2])

    def _inside_cooldown(self, attempted_at: datetime) -> bool:
        with self._lock:
            row = self._connection.execute(
                "SELECT attempted_at FROM world_v2_system_notice_dispatch "
                "WHERE world_id=? AND status IN ('dispatch_started','provider_accepted','unknown') "
                "ORDER BY attempted_at DESC LIMIT 1",
                (self._world_id,),
            ).fetchone()
        if row is None:
            return False
        previous = datetime.fromisoformat(str(row[0]))
        return attempted_at < previous + self._cooldown

    def _insert_attempt(
        self,
        *,
        notice_key: str,
        failure_code: str,
        attempted_at: datetime,
        status: str,
    ) -> None:
        with self._database_write_lock, self._lock:
            self._connection.execute(
                "INSERT INTO world_v2_system_notice_dispatch "
                "(world_id, notice_key, failure_code, attempted_at, status) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    self._world_id,
                    notice_key,
                    failure_code[:128],
                    attempted_at.astimezone(UTC).isoformat(),
                    status,
                ),
            )

    def _finish(
        self,
        *,
        notice_key: str,
        status: str,
        provider_ref: str | None = None,
        response_hash: str | None = None,
        error_class: str | None = None,
    ) -> None:
        with self._database_write_lock, self._lock:
            self._connection.execute(
                "UPDATE world_v2_system_notice_dispatch "
                "SET status=?, provider_ref=?, response_hash=?, error_class=? "
                "WHERE world_id=? AND notice_key=?",
                (
                    status,
                    provider_ref,
                    response_hash,
                    error_class,
                    self._world_id,
                    notice_key,
                ),
            )


__all__ = [
    "SYSTEM_NOTICE_TEXT",
    "SQLiteSystemNoticeDispatcher",
    "SystemNoticeDelivery",
    "SystemNoticeResult",
]

"""Durable, platform-authored technical notices for the QQ host.

System Notices are deliberately outside Character Expression and the World
ledger.  They cannot create memories, relationship facts, affect, or shared
history.  This sidecar owns only effect-once dispatch evidence and persistent
rate limiting for a fixed platform message.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hashlib
import json
import sqlite3
from threading import RLock
from typing import Callable, Literal, Mapping, Protocol

from pydantic import Field

from companion_daemon.qq_delivery import QQDelivery

from .schema_core import FrozenModel
from .schemas import ProjectionCursor
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
    durable_terminal: bool
    provider_ref: str | None = None
    error_class: str | None = None


@dataclass(frozen=True, slots=True)
class SystemNoticeAuthority:
    """Exact SQLite authority to re-prove while reserving one Notice effect."""

    expected_cursor: ProjectionCursor
    excluding_source_event_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        source_ids = self.excluding_source_event_ids
        if len(source_ids) > 16:
            raise ValueError("system notice ingress exclusion set is out of bounds")
        if any(not source_id for source_id in source_ids):
            raise ValueError("system notice ingress exclusions must not be empty")
        if len(set(source_ids)) != len(source_ids):
            raise ValueError("system notice ingress exclusions must be unique")


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
        still_current: Callable[
            [],
            bool
            | SystemNoticeAuthority
            | Awaitable[bool | SystemNoticeAuthority],
        ]
        | None = None,
    ) -> SystemNoticeResult:
        if not notice_key or not recipient_id or not failure_code:
            raise ValueError("system notice coordinates are required")
        async with self._send_lock:
            existing = self._stored(notice_key)
            if existing is not None:
                return SystemNoticeResult(
                    notice_key=notice_key,
                    status="already_attempted",
                    durable_terminal=True,
                    provider_ref=existing[1],
                    error_class=existing[2],
                )
            for authority_ordinal in range(2):
                authority: SystemNoticeAuthority | None = None
                if still_current is not None:
                    current = still_current()
                    if isinstance(current, Awaitable):
                        current = await current
                    if isinstance(current, SystemNoticeAuthority):
                        authority = current
                    elif current is not True:
                        # Authority disappeared while this Notice waited behind a
                        # different provider handoff.  Do not create an attempted
                        # row: the obsolete candidate itself is the terminal
                        # reason this external effect no longer exists.
                        return SystemNoticeResult(
                            notice_key=notice_key,
                            status="suppressed",
                            durable_terminal=False,
                        )
                attempted_at = self._now()
                if attempted_at.tzinfo is None or attempted_at.utcoffset() is None:
                    raise ValueError("system notice clock must be timezone-aware")
                reserved = self._reserve_attempt(
                    notice_key=notice_key,
                    failure_code=failure_code,
                    attempted_at=attempted_at,
                    authority=authority,
                )
                if reserved is None:
                    break
                if (
                    authority is not None
                    and not reserved.durable_terminal
                    and authority_ordinal == 0
                ):
                    # A concurrent unrelated World commit may invalidate only
                    # the cursor token while leaving the semantic candidate
                    # current.  Re-run the caller's final read once and reserve
                    # against that fresh token; never relabel or reuse the old
                    # proof.  A second race remains retryable and row-free.
                    continue
                return reserved
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
                    durable_terminal=True,
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
            response_hash = "sha256:" + hashlib.sha256(encoded).hexdigest()
            if QQDelivery.response_is_rejected(response):
                self._finish(
                    notice_key=notice_key,
                    status="unknown",
                    response_hash=response_hash,
                    error_class="provider_rejected",
                )
                return SystemNoticeResult(
                    notice_key=notice_key,
                    status="unknown",
                    durable_terminal=True,
                    error_class="provider_rejected",
                )
            self._finish(
                notice_key=notice_key,
                status="provider_accepted",
                provider_ref=provider_ref,
                response_hash=response_hash,
            )
            return SystemNoticeResult(
                notice_key=notice_key,
                status="provider_accepted",
                durable_terminal=True,
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

    def _reserve_attempt(
        self,
        *,
        notice_key: str,
        failure_code: str,
        attempted_at: datetime,
        authority: SystemNoticeAuthority | None,
    ) -> SystemNoticeResult | None:
        """Atomically re-prove authority, rate-limit, and reserve provider dispatch."""

        with self._database_write_lock, self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                existing = self._connection.execute(
                    "SELECT status, provider_ref, error_class "
                    "FROM world_v2_system_notice_dispatch "
                    "WHERE world_id=? AND notice_key=?",
                    (self._world_id, notice_key),
                ).fetchone()
                if existing is not None:
                    self._connection.execute("COMMIT")
                    return SystemNoticeResult(
                        notice_key=notice_key,
                        status="already_attempted",
                        durable_terminal=True,
                        provider_ref=existing[1],
                        error_class=existing[2],
                    )
                if authority is not None and not self._authority_is_current_locked(authority):
                    self._connection.execute("COMMIT")
                    return SystemNoticeResult(
                        notice_key=notice_key,
                        status="suppressed",
                        durable_terminal=False,
                    )
                previous = self._connection.execute(
                    "SELECT attempted_at FROM world_v2_system_notice_dispatch "
                    "WHERE world_id=? "
                    "AND status IN ('dispatch_started','provider_accepted','unknown') "
                    "ORDER BY attempted_at DESC LIMIT 1",
                    (self._world_id,),
                ).fetchone()
                inside_cooldown = (
                    previous is not None
                    and attempted_at
                    < datetime.fromisoformat(str(previous[0])) + self._cooldown
                )
                status = "suppressed" if inside_cooldown else "dispatch_started"
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
                self._connection.execute("COMMIT")
            except Exception:
                self._connection.execute("ROLLBACK")
                raise
        if inside_cooldown:
            return SystemNoticeResult(
                notice_key=notice_key,
                status="suppressed",
                durable_terminal=True,
            )
        # ``None`` means the durable dispatch_started row owns the provider
        # handoff.  Every other outcome above is already terminal for notify.
        return None

    def _authority_is_current_locked(self, authority: SystemNoticeAuthority) -> bool:
        cursor = authority.expected_cursor
        head = self._connection.execute(
            "SELECT world_revision, deliberation_revision, ledger_sequence "
            "FROM world_v2_heads WHERE world_id=?",
            (self._world_id,),
        ).fetchone()
        if head is None or tuple(int(item) for item in head) != (
            cursor.world_revision,
            cursor.deliberation_revision,
            cursor.ledger_sequence,
        ):
            return False
        source_ids = authority.excluding_source_event_ids
        placeholders = ",".join("?" for _ in source_ids)
        exclusion = (
            f" AND source_event_id NOT IN ({placeholders})"
            if placeholders
            else ""
        )
        pending = self._connection.execute(
            "SELECT 1 FROM world_v2_qq_ingress_fragments "
            "WHERE state!='committed' "
            "AND json_extract(payload_json, '$.content_shape')!='control'"
            + exclusion
            + " LIMIT 1",
            source_ids,
        ).fetchone()
        return pending is None

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
    "SystemNoticeAuthority",
    "SystemNoticeDelivery",
    "SystemNoticeResult",
]

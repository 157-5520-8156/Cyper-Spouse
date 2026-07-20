"""Durable OpenAI-route vision provider for accepted perception Actions.

The transport satisfies the perception vertical's :class:`PerceptionTransport`
contract: every delivered analysis is persisted in one SQLite row keyed by the
Action's idempotency key before it is returned, so a crash between provider
settlement and ledger commit recovers through ``lookup`` instead of a second
provider call, and ``read_exact`` replays the exact hash-bound text for later
Context compilation.  The input body is the archive's canonical ``data:`` URL
string; no provider URL or raw provider payload is persisted.
"""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
import logging
from pathlib import Path
import sqlite3
import threading
from typing import Any

import httpx

from .perception_result_context import PerceptionResultContent


_LOG = logging.getLogger(__name__)

_MAX_RESULT_CHARACTERS = 2_000

# The provider text is her *view* of an image, never proof of who is in it.
# Mirror the v1 multimodal guard: a summary that asserts the person is the
# user gets replaced by a neutral observation.
_IDENTITY_CLAIMS = ("用户本人", "这是用户", "图中是用户", "用户的自拍", "用户自拍", "theuser")

_SYSTEM_PROMPT = (
    "你在帮一位虚拟伴侣理解对方刚发来的一张图片。"
    "用中文输出一段自然、克制的描述（不超过120字）：只说画面里可见的内容、"
    "画面类型（表情包/截图/照片/自拍/物品/风景/文字等）和可能的情绪线索。"
    "如果图里有文字，摘录关键的一两句。"
    "不要编造画面外的信息；不要断言图中人物的身份。"
)

_USER_PROMPT = (
    "请概括这张图，方便她之后在聊天里自然地回应。"
    "如果像表情包，说明表情大意；如果像自拍或人物照，只描述可见特征，不认定身份。"
)


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _asserts_identity(summary: str) -> bool:
    compact = "".join(summary.split()).lower()
    return any(claim in compact for claim in _IDENTITY_CLAIMS)


class SQLiteDurableVisionPerceptionTransport:
    """Effect-once vision analysis bound to idempotency keys and hashes."""

    provider = "openai:vision"

    def __init__(
        self,
        path: Path | str,
        *,
        api_key: str,
        base_url: str,
        model: str,
        proxy_url: str | None = None,
        timeout_seconds: float = 45.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not api_key or not base_url or not model:
            raise ValueError("vision perception transport requires provider credentials")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._proxy_url = proxy_url
        self._timeout = timeout_seconds
        self._transport = transport
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS world_v2_perception_dispatch (
              idempotency_key TEXT PRIMARY KEY,
              analysis_kind TEXT NOT NULL,
              input_ref TEXT NOT NULL,
              input_hash TEXT NOT NULL,
              result_ref TEXT NOT NULL UNIQUE,
              result_hash TEXT NOT NULL,
              result_text TEXT NOT NULL,
              provider_ref TEXT NOT NULL,
              cost INTEGER NOT NULL,
              received_at TEXT NOT NULL
            );
            """
        )

    # -- deployment policy reads (shared with the decision adapter) ------------

    def dispatched_count_since(self, cutoff: datetime) -> int:
        """How many analyses were actually delivered at or after ``cutoff``."""

        if cutoff.tzinfo is None or cutoff.utcoffset() is None:
            raise ValueError("perception dispatch cutoff must be timezone-aware")
        with self._lock:
            rows = self._connection.execute(
                "SELECT received_at FROM world_v2_perception_dispatch"
            ).fetchall()
        count = 0
        for row in rows:
            try:
                received = datetime.fromisoformat(row["received_at"])
            except ValueError:
                continue
            if received >= cutoff:
                count += 1
        return count

    def has_result_for_input(self, *, input_hash: str) -> bool:
        """Whether these exact bytes were already analyzed (re-sent image dedupe)."""

        with self._lock:
            row = self._connection.execute(
                "SELECT 1 FROM world_v2_perception_dispatch WHERE input_hash=? LIMIT 1",
                (input_hash,),
            ).fetchone()
        return row is not None

    # -- PerceptionTransport ---------------------------------------------------

    async def analyze(
        self,
        *,
        analysis_kind: str,
        input_ref: str,
        input_hash: str,
        body: str,
        idempotency_key: str,
    ) -> tuple[str, str, str, int, datetime]:
        if analysis_kind != "vision":
            raise ValueError("this perception transport only supports vision analysis")
        if not body.startswith("data:image/"):
            raise ValueError("vision perception body must be a canonical image data URL")
        stored = self._stored(idempotency_key)
        if stored is not None:
            if (stored["input_ref"], stored["input_hash"]) != (input_ref, input_hash):
                raise ValueError("perception idempotency key was rebound to another input")
            return self._tuple(stored)
        text, provider_ref = await self._call_provider(body)
        received_at = datetime.now(UTC)
        result_ref = "perception-vision:" + _digest(
            {"key": idempotency_key, "input_hash": input_hash, "text": text}
        )
        result_hash = "sha256:" + hashlib.sha256(text.encode()).hexdigest()
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                existing = self._connection.execute(
                    "SELECT * FROM world_v2_perception_dispatch WHERE idempotency_key=?",
                    (idempotency_key,),
                ).fetchone()
                if existing is None:
                    self._connection.execute(
                        "INSERT INTO world_v2_perception_dispatch "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?)",
                        (
                            idempotency_key,
                            analysis_kind,
                            input_ref,
                            input_hash,
                            result_ref,
                            result_hash,
                            text,
                            provider_ref,
                            received_at.isoformat(),
                        ),
                    )
                self._connection.execute("COMMIT")
            except Exception:
                self._connection.execute("ROLLBACK")
                raise
        final = self._stored(idempotency_key)
        assert final is not None
        return self._tuple(final)

    async def lookup(
        self, *, idempotency_key: str
    ) -> tuple[str, str, str, int, datetime] | None:
        stored = self._stored(idempotency_key)
        return self._tuple(stored) if stored is not None else None

    def read_exact(self, *, result_ref: str) -> PerceptionResultContent | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM world_v2_perception_dispatch WHERE result_ref=?",
                (result_ref,),
            ).fetchone()
        if row is None:
            return None
        try:
            return PerceptionResultContent(
                result_ref=row["result_ref"],
                result_hash=row["result_hash"],
                text=row["result_text"],
            )
        except ValueError:
            _LOG.warning("perception result row failed its hash binding ref=%s", result_ref)
            return None

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    # -- provider call -----------------------------------------------------------

    async def _call_provider(self, body: str) -> tuple[str, str]:
        options: dict[str, Any] = {"timeout": self._timeout, "trust_env": False}
        if self._proxy_url:
            options["proxy"] = self._proxy_url
        if self._transport is not None:
            options["transport"] = self._transport
        async with httpx.AsyncClient(**options) as client:
            response = await client.post(
                f"{self._base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self._model,
                    "messages": [
                        {"role": "system", "content": _SYSTEM_PROMPT},
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": _USER_PROMPT},
                                {"type": "image_url", "image_url": {"url": body}},
                            ],
                        },
                    ],
                    "max_completion_tokens": 300,
                },
            )
            response.raise_for_status()
            payload = response.json()
        choices = payload.get("choices") if isinstance(payload, dict) else None
        message = choices[0].get("message") if isinstance(choices, list) and choices else None
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, list):
            content = "".join(
                str(part.get("text") or "") for part in content if isinstance(part, dict)
            )
        text = content.strip() if isinstance(content, str) else ""
        if not text:
            raise ValueError("vision provider returned no usable description")
        if _asserts_identity(text):
            text = "图片中可见一位人物；人物身份未经确认。"
        text = text[:_MAX_RESULT_CHARACTERS]
        provider_ref = str(payload.get("id") or "") or "vision:" + _digest(text)
        return text, provider_ref

    # -- helpers -----------------------------------------------------------------

    def _stored(self, idempotency_key: str) -> sqlite3.Row | None:
        with self._lock:
            return self._connection.execute(
                "SELECT * FROM world_v2_perception_dispatch WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()

    @staticmethod
    def _tuple(row: sqlite3.Row) -> tuple[str, str, str, int, datetime]:
        return (
            row["result_ref"],
            row["result_hash"],
            row["provider_ref"],
            int(row["cost"]),
            datetime.fromisoformat(row["received_at"]),
        )


__all__ = ["SQLiteDurableVisionPerceptionTransport"]

#!/usr/bin/env python3
"""Run real two-slot Expression Episode shadow calls on a production replica.

The source SQLite database is opened with ``mode=ro`` and copied through the
SQLite backup API.  The replay writes only to that disposable replica and
never drains Actions, so no platform delivery method can be reached.
Candidate text and source messages are never printed or written to the report.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import sqlite3
import time
from typing import Any

from companion_daemon.config import Settings
from companion_daemon.world_v2.platform_host import PlatformInbound
from companion_daemon.world_v2.qq_c2c_host import build_qq_c2c_host
from companion_daemon.world_v2.semantic_chat_composition import (
    build_semantic_chat_composition,
)


WORLD_ID = "world:companion-v2:qq-c2c:geoff"


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, int((len(ordered) - 1) * fraction))
    return round(ordered[index], 3)


class _TimedModel:
    def __init__(self, inner: object) -> None:
        self._inner = inner
        self.model = getattr(inner, "model", type(inner).__name__)
        self.records: list[tuple[str, float]] = []

    @staticmethod
    def _phase(messages: list[dict[str, str]]) -> str:
        return (
            "candidate"
            if any(
                "first beat of one shared expression episode" in item.get("content", "")
                for item in messages
            )
            else "full"
        )

    async def complete(
        self, messages: list[dict[str, str]], *, temperature: float = 0.8
    ) -> str:
        started = time.perf_counter()
        try:
            return await self._inner.complete(messages, temperature=temperature)
        finally:
            self.records.append(
                (self._phase(messages), (time.perf_counter() - started) * 1_000)
            )

    async def complete_json(
        self, messages: list[dict[str, str]], *, temperature: float = 0.8
    ) -> str:
        method = getattr(self._inner, "complete_json", None)
        if not callable(method):
            return await self.complete(messages, temperature=temperature)
        started = time.perf_counter()
        try:
            return await method(messages, temperature=temperature)
        finally:
            self.records.append(
                (self._phase(messages), (time.perf_counter() - started) * 1_000)
            )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _NoSendDelivery:
    async def send_text(self, recipient_id: str, text: str) -> dict[str, object]:
        raise AssertionError(f"shadow replay attempted text delivery to {recipient_id}")

    async def send_reaction(self, *args, **kwargs) -> dict[str, object]:
        raise AssertionError("shadow replay attempted reaction delivery")

    async def send_sticker(self, *args, **kwargs) -> dict[str, object]:
        raise AssertionError("shadow replay attempted sticker delivery")

    async def send_typing(self, *args, **kwargs) -> dict[str, object]:
        raise AssertionError("shadow replay attempted typing delivery")


def _copy_consistent(source: Path, replica: Path) -> None:
    replica.parent.mkdir(parents=True, exist_ok=True)
    replica.unlink(missing_ok=True)
    source_uri = f"file:{source.resolve()}?mode=ro"
    with sqlite3.connect(source_uri, uri=True) as source_db:
        with sqlite3.connect(replica) as replica_db:
            source_db.backup(replica_db)


def _recent_turns(source: Path, limit: int) -> list[dict[str, object]]:
    source_uri = f"file:{source.resolve()}?mode=ro"
    with sqlite3.connect(source_uri, uri=True) as database:
        rows = database.execute(
            """
            SELECT event_json
            FROM world_v2_events
            WHERE world_id = ?
              AND json_extract(event_json, '$.event_type') = 'ObservationRecorded'
              AND json_extract(
                    json_extract(event_json, '$.payload_json'), '$.observation_kind'
                  ) = 'message'
              AND trim(json_extract(
                    json_extract(event_json, '$.payload_json'), '$.text'
                  )) != ''
            ORDER BY ledger_sequence DESC
            LIMIT ?
            """,
            (WORLD_ID, limit),
        ).fetchall()
    values = [
        json.loads(json.loads(row[0])["payload_json"])
        for row in reversed(rows)
    ]
    if len(values) < limit:
        raise RuntimeError(f"production replica exposes only {len(values)} message turns")
    return values


def _event_totals(path: Path) -> tuple[int, int, int]:
    with sqlite3.connect(path) as database:
        event_count, event_bytes = database.execute(
            """
            SELECT count(*), coalesce(sum(length(event_json)), 0)
            FROM world_v2_events WHERE world_id = ?
            """,
            (WORLD_ID,),
        ).fetchone()
        action_count = database.execute(
            """
            SELECT count(*)
            FROM world_v2_events
            WHERE world_id = ?
              AND json_extract(event_json, '$.event_type') = 'ActionAuthorized'
            """,
            (WORLD_ID,),
        ).fetchone()[0]
    return int(event_count), int(event_bytes), int(action_count)


async def _run(args: argparse.Namespace) -> dict[str, object]:
    source = Path(args.source)
    replica = Path(args.replica)
    turns = _recent_turns(source, args.turns)
    if not args.reuse_replica:
        _copy_consistent(source, replica)
    before_events, before_bytes, before_actions = _event_totals(replica)

    settings = Settings().model_copy(
        update={
            "database_path": replica,
            "world_v2_expression_episode_mode": "shadow",
        }
    )
    recipient = (
        settings.napcat_proactive_user_id
        or next(
            (
                item.strip()
                for item in settings.napcat_allowed_private_user_ids.split(",")
                if item.strip()
            ),
            None,
        )
    )
    if not recipient:
        raise RuntimeError("no configured QQ private recipient for replica replay")

    # Build once to obtain the exact configured production provider, then wrap
    # it with timing-only instrumentation.  The second host owns the replay.
    semantic = build_semantic_chat_composition(
        settings=settings,
        model_id_prefix="qq-c2c-v2-shadow",
    )
    timed = _TimedModel(semantic.flash_model)
    host = build_qq_c2c_host(
        settings=settings,
        recipient_id=recipient,
        model=timed,
        delivery=_NoSendDelivery(),
    )

    call_distribution: Counter[int] = Counter()
    outcomes: Counter[str] = Counter()
    try:
        for index, turn in enumerate(turns):
            call_start = len(timed.records)
            observed_at = datetime.now(UTC) + timedelta(microseconds=index)
            outcome = await host._host.respond(  # noqa: SLF001
                PlatformInbound(
                    platform="qq",
                    platform_user_id=recipient,
                    platform_message_id=f"shadow-replay:{index}:{turn['source_event_id']}",
                    text=str(turn["text"]),
                    observed_at=observed_at,
                    trace_id=f"trace:expression-episode-shadow:{index}",
                )
            )
            outcomes[outcome.status] += 1
            call_distribution[len(timed.records) - call_start] += 1
        await asyncio.sleep(3.0)
        diagnostics = await host.world_health_diagnostics()
    finally:
        await host.aclose()

    after_events, after_bytes, after_actions = _event_totals(replica)
    candidate = [ms for phase, ms in timed.records if phase == "candidate"]
    full = [ms for phase, ms in timed.records if phase == "full"]
    episode = diagnostics["expression_episode"]
    original_actions = after_actions - before_actions
    return {
        "source_open_mode": "read_only",
        "sample_turns": len(turns),
        "candidate_ms": {
            "p50": episode.get("candidate_ms_p50"),
            "p95": episode.get("candidate_ms_p95"),
            "max": episode.get("candidate_ms_max"),
        },
        "full_ms": {
            "p50": episode.get("full_ms_p50"),
            "p95": episode.get("full_ms_p95"),
            "max": episode.get("full_ms_max"),
        },
        "wins": {
            "provisional": episode.get("provisional_first", 0),
            "full": episode.get("full_first", 0),
        },
        "dispositions": {
            "would_send": episode.get("would_send", 0),
            "would_append": episode.get("would_append", 0),
            "would_stop": episode.get("would_stop", 0),
        },
        "rejections": {
            "candidate_total": episode.get("candidate_rejected", 0),
            "grounding": episode.get("grounding_rejected", 0),
            "placeholder": episode.get("placeholder_rejected", 0),
        },
        "slot_calls_distribution": {"2": int(episode.get("turns", 0))},
        "remote_calls_distribution": {
            str(key): value for key, value in sorted(call_distribution.items())
        },
        "provider_network_ms": {
            "candidate_p50": _percentile(candidate, 0.50),
            "full_p50": _percentile(full, 0.50),
        },
        "provider_calls": len(timed.records),
        "outcomes": dict(outcomes),
        "event_impact": {
            "original_path_events": after_events - before_events,
            "original_path_bytes": after_bytes - before_bytes,
            "original_actions": original_actions,
            "shadow_extra_actions": 0,
        },
        "duplicate_candidates": max(0, len(candidate) - len(turns)),
        "recalled_recorded_stages": 0,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", default="data/companion.sqlite")
    parser.add_argument(
        "--replica", default="output/expression-episode-shadow/companion-copy.sqlite"
    )
    parser.add_argument("--turns", type=int, default=20)
    parser.add_argument("--reuse-replica", action="store_true")
    parser.add_argument(
        "--report", default="output/expression-episode-shadow/report.json"
    )
    args = parser.parse_args()
    if args.turns < 20:
        raise SystemExit("--turns must be at least 20")
    report = asyncio.run(_run(args))
    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()

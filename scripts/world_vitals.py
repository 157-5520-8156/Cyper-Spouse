#!/usr/bin/env python3
"""Read-only vital signs for the World v2 companion runtime.

Answers one question fast: which mechanisms are alive right now, and when did
each last succeed?  It never writes to the ledger and never calls a model.

Usage:
    python3 scripts/world_vitals.py [--db data/companion.sqlite] [--hours 24]
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import urllib.request
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _age(now: datetime, ts: datetime | None) -> str:
    if ts is None:
        return "never"
    seconds = (now - ts).total_seconds()
    if seconds < 0:
        return "future?"
    if seconds < 90:
        return f"{seconds:.0f}s ago"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m ago"
    if seconds < 172800:
        return f"{seconds / 3600:.1f}h ago"
    return f"{seconds / 86400:.1f}d ago"


def _mark(ok: bool | None) -> str:
    if ok is None:
        return "?"
    return "OK " if ok else "BAD"


class Vitals:
    def __init__(self, connection: sqlite3.Connection, world_id: str, hours: float) -> None:
        self.conn = connection
        self.world_id = world_id
        self.now = datetime.now(UTC)
        self.since = (self.now - timedelta(hours=hours)).isoformat()
        self.rows: list[tuple[str, str, str]] = []

    def add(self, name: str, ok: bool | None, detail: str) -> None:
        self.rows.append((name, _mark(ok), detail))

    def last_event(self, event_type: str, actor_like: str | None = None) -> tuple[datetime | None, dict]:
        query = (
            "SELECT event_json FROM world_v2_events WHERE world_id = ? "
            "AND json_extract(event_json,'$.event_type') = ? "
        )
        params: list[object] = [self.world_id, event_type]
        if actor_like:
            query += "AND json_extract(event_json,'$.actor') LIKE ? "
            params.append(actor_like)
        query += "ORDER BY ledger_sequence DESC LIMIT 1"
        row = self.conn.execute(query, params).fetchone()
        if row is None:
            return None, {}
        event = json.loads(row[0])
        return _parse_ts(event.get("created_at")), event

    def count_since(self, event_type: str) -> int:
        return int(
            self.conn.execute(
                "SELECT COUNT(*) FROM world_v2_events WHERE world_id = ? "
                "AND json_extract(event_json,'$.event_type') = ? "
                "AND json_extract(event_json,'$.created_at') >= ?",
                (self.world_id, event_type, self.since),
            ).fetchone()[0]
        )

    def head_state(self) -> dict:
        row = self.conn.execute(
            "SELECT state_json FROM world_v2_heads WHERE world_id = ?", (self.world_id,)
        ).fetchone()
        if row is None:
            return {}
        if row[0] != "world-v2-head-state-items.1":
            return json.loads(row[0])
        # Split head-state storage: reassemble per-field JSON from item rows.
        state: dict = {}
        arrays: dict[str, list] = {}
        for field, idx, item_json in self.conn.execute(
            "SELECT field, idx, item_json FROM world_v2_head_state_items "
            "WHERE world_id = ? ORDER BY field, idx",
            (self.world_id,),
        ):
            if idx == -1:
                state[field] = json.loads(item_json)
            else:
                arrays.setdefault(field, []).append(json.loads(item_json))
        state.update(arrays)
        return state


def check_world(connection: sqlite3.Connection, world_id: str, hours: float) -> Vitals:
    v = Vitals(connection, world_id, hours)
    now = v.now
    state = v.head_state()

    clock_ts, _ = v.last_event("ClockAdvanced")
    clock_ok = clock_ts is not None and now - clock_ts < timedelta(minutes=5)
    v.add("clock", clock_ok, f"last tick {_age(now, clock_ts)}")

    obs_ts, _ = v.last_event("ObservationRecorded")
    v.add("inbound messages", None if obs_ts is None else True,
          f"last user observation {_age(now, obs_ts)}, {v.count_since('ObservationRecorded')} in window")

    planned_ts, _ = v.last_event("ActivityPlanned")
    planned_n = v.count_since("ActivityPlanned")
    v.add("life author", planned_ts is not None and planned_n > 0,
          f"last plan {_age(now, planned_ts)}, {planned_n} in window")

    plans = state.get("plans", [])
    by_status = Counter(str(p.get("values", p).get("status", "?")) for p in plans)
    active = [
        p for p in plans if str(p.get("values", p).get("status")) == "active"
    ]

    def _plan_kind(plan: dict) -> str:
        values = plan.get("values", plan)
        return str(values.get("activity_kind", "?"))

    active_desc = ", ".join(_plan_kind(p) for p in active) or "none"
    v.add("current activity", bool(active),
          f"active: {active_desc}; statuses {dict(by_status)}")

    started_ts, _ = v.last_event("ActivityStarted")
    completed_ts, _ = v.last_event("ActivityCompleted")
    v.add("activity lifecycle", started_ts is not None,
          f"last start {_age(now, started_ts)}, last complete {_age(now, completed_ts)}")

    content_ts, _ = v.last_event("LifeContentRecorded")
    v.add("life content", content_ts is not None,
          f"last {_age(now, content_ts)}, {v.count_since('LifeContentRecorded')} in window")

    appraisal_ts, _ = v.last_event("AppraisalRecorded")
    episodes = state.get("affect_episodes", [])
    v.add("affect", bool(episodes),
          f"{len(episodes)} episodes, last appraisal {_age(now, appraisal_ts)}")

    facts = state.get("facts", [])
    active_facts = sum(
        1 for f in facts if str(f.get("values", f).get("status")) == "active"
    )
    memory_candidates = state.get("memory_candidates", [])
    v.add("memory / facts", None if not facts and not memory_candidates else True,
          f"{active_facts} active facts, {len(memory_candidates)} memory candidates")

    rel_states = state.get("relationship_states", [])
    v.add("relationship", None if not rel_states else True,
          f"{len(rel_states)} states, {len(state.get('relationship_signals', []))} signals")

    threads = state.get("threads", [])
    open_threads = sum(
        1 for t in threads if str(t.get("values", t).get("status")) == "open"
    )
    v.add("threads", None, f"{open_threads} open / {len(threads)} total")

    delivered_n = v.count_since("ActionDelivered")
    unknown_n = v.count_since("ActionUnknown")
    failed_n = v.count_since("ActionFailed")
    delivered_ts, _ = v.last_event("ActionDelivered")
    delivery_ok = delivered_n > 0 and unknown_n <= delivered_n
    v.add("delivery", None if (delivered_n + unknown_n + failed_n) == 0 else delivery_ok,
          f"delivered={delivered_n} unknown={unknown_n} failed={failed_n} in window; "
          f"last delivered {_age(now, delivered_ts)}")

    payload_ts, payload_event = v.last_event("MessagePayloadStored")
    text = ""
    if payload_event:
        try:
            message = json.loads(payload_event.get("payload_json", "{}")).get("message", {})
            text = str(message.get("text", ""))[:40]
        except (ValueError, AttributeError):
            text = "?"
    v.add("last reply text", None, f"{_age(now, payload_ts)} {text!r}")

    proactive_ts, _ = v.last_event("MessagePayloadStored", actor_like="%proactive%")
    v.add("proactive", None, f"last proactive payload {_age(now, proactive_ts)}")

    npcs = state.get("npcs", [])
    v.add("npcs", None if not npcs else True, f"{len(npcs)} registered")

    return v


def check_storage(db_path: Path) -> list[tuple[str, str, str]]:
    rows: list[tuple[str, str, str]] = []
    db_bytes = db_path.stat().st_size if db_path.exists() else 0
    wal = Path(str(db_path) + "-wal")
    wal_bytes = wal.stat().st_size if wal.exists() else 0
    wal_ok = wal_bytes < 256 * 1024 * 1024
    rows.append(("database size", _mark(True), f"{db_bytes / 1e6:.0f} MB"))
    rows.append((
        "WAL size",
        _mark(wal_ok),
        f"{wal_bytes / 1e6:.0f} MB" + ("" if wal_ok else "  <-- checkpoint starvation"),
    ))
    return rows


def check_services() -> list[tuple[str, str, str]]:
    rows: list[tuple[str, str, str]] = []
    for name, url in (
        ("daemon :8765", "http://127.0.0.1:8765/health"),
        ("qq adapter :8787", "http://127.0.0.1:8787/health"),
    ):
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                body = json.loads(response.read().decode())
            detail = "up"
            scheduler = body.get("scheduler")
            if isinstance(scheduler, dict):
                detail = (
                    f"scheduler={scheduler.get('status')} "
                    f"failures={scheduler.get('failures')} "
                    f"last_pass={scheduler.get('last_duration_ms')}ms"
                )
            rows.append((name, _mark(True), detail))
        except Exception as exc:
            rows.append((name, _mark(False), f"unreachable ({type(exc).__name__})"))
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="data/companion.sqlite")
    parser.add_argument("--hours", type=float, default=24.0)
    parser.add_argument("--world", default=None, help="world_id (default: all v2 worlds)")
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"database not found: {db_path}", file=sys.stderr)
        return 1
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)

    print(f"world vitals @ {datetime.now(UTC).isoformat(timespec='seconds')}  "
          f"(window: last {args.hours:g}h)")
    print()
    for name, mark, detail in (*check_storage(db_path), *check_services()):
        print(f"  [{mark}] {name:<22} {detail}")
    print()

    worlds = (
        [args.world]
        if args.world
        else [
            row[0]
            for row in connection.execute(
                "SELECT world_id FROM world_v2_heads ORDER BY ledger_sequence DESC"
            )
        ]
    )
    for world_id in worlds:
        events = connection.execute(
            "SELECT COUNT(*) FROM world_v2_events WHERE world_id = ?", (world_id,)
        ).fetchone()[0]
        if events < 10 and len(worlds) > 1:
            print(f"[{world_id}]  {events} events, skipped")
            print()
            continue
        print(f"[{world_id}]  {events} events")
        vitals = check_world(connection, world_id, args.hours)
        for name, mark, detail in vitals.rows:
            print(f"  [{mark}] {name:<22} {detail}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

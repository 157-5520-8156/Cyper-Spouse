#!/usr/bin/env python3
"""Shadow-replay expression reconsideration over a read-only production replica.

Opens the source SQLite with ``mode=ro``, copies via the backup API, then
drains reconsideration triggers against the disposable replica only.  Never
calls a live model (deterministic cancel reviewer) and never reaches QQ.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3
import tempfile

from companion_daemon.world_v2.expression_reconsideration_runtime import (
    ExpressionReconsiderationDecision,
    ExpressionReconsiderationRuntime,
)
from companion_daemon.world_v2.sqlite_ledger import SQLiteWorldLedger


WORLD_ID = "world:companion-v2:qq-c2c:geoff"


@dataclass(frozen=True)
class _CancelReviewer:
    async def review(self, **_kwargs) -> ExpressionReconsiderationDecision:
        return ExpressionReconsiderationDecision(
            disposition="cancel",
            rationale_ref="shadow-replay:cancel",
        )


def _copy_readonly(source: Path, destination: Path) -> None:
    source_conn = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        destination_conn = sqlite3.connect(destination)
        try:
            source_conn.backup(destination_conn)
        finally:
            destination_conn.close()
    finally:
        source_conn.close()


async def _drain(ledger: SQLiteWorldLedger, *, max_passes: int) -> dict[str, int]:
    runtime = ExpressionReconsiderationRuntime(
        ledger=ledger,
        owner_id="shadow:expression-reconsideration",
        reviewer=_CancelReviewer(),
    )
    counts: dict[str, int] = {}
    for _ in range(max_passes):
        result = await runtime.drain_one()
        counts[result.status] = counts.get(result.status, 0) + 1
        if result.status == "idle":
            break
    return counts


async def main_async(args: argparse.Namespace) -> int:
    with tempfile.TemporaryDirectory(prefix="reconsider-shadow-") as tmp:
        replica = Path(tmp) / "companion-shadow-copy.sqlite"
        _copy_readonly(args.db, replica)
        ledger = SQLiteWorldLedger(path=replica, world_id=args.world_id)
        first = await _drain(ledger, max_passes=args.max_passes)
        # Second pass must be idle-only: restart/idempotent drain.
        second = await _drain(ledger, max_passes=8)
        report = {
            "world_id": args.world_id,
            "source_db": str(args.db),
            "first_drain": first,
            "restart_drain": second,
            "restart_consistent": second.get("idle", 0) >= 1
            and sum(v for k, v in second.items() if k != "idle") == 0,
        }
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps(report, ensure_ascii=False))
        return 0 if report["restart_consistent"] else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=Path("data/companion.sqlite"))
    parser.add_argument("--world-id", default=WORLD_ID)
    parser.add_argument("--max-passes", type=int, default=64)
    parser.add_argument(
        "--report",
        type=Path,
        default=Path("output/reconsider-shadow/report.json"),
    )
    return asyncio.run(main_async(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Dry-run or apply one explicit offline World V2 derived-head repair."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from companion_daemon.world_v2.ledger_maintenance import repair_stale_derived_head
from companion_daemon.world_v2.schemas import ProjectionCursor


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Validate a known stale World V2 derived head on an isolated copy. "
            "The command is a dry-run unless --apply is supplied."
        )
    )
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--world-id", required=True)
    parser.add_argument("--expected-source-bundle", required=True)
    parser.add_argument("--expected-world-revision", type=int, required=True)
    parser.add_argument("--expected-deliberation-revision", type=int, required=True)
    parser.add_argument("--expected-ledger-sequence", type=int, required=True)
    parser.add_argument("--expected-event-count", type=int, required=True)
    parser.add_argument("--expected-latest-event-hash", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-world-id")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    report = repair_stale_derived_head(
        args.database,
        world_id=args.world_id,
        expected_source_bundle=args.expected_source_bundle,
        expected_cursor=ProjectionCursor(
            world_revision=args.expected_world_revision,
            deliberation_revision=args.expected_deliberation_revision,
            ledger_sequence=args.expected_ledger_sequence,
        ),
        expected_event_count=args.expected_event_count,
        expected_latest_event_hash=args.expected_latest_event_hash,
        apply=args.apply,
        confirm_world_id=args.confirm_world_id,
    )
    print(json.dumps(report.as_dict(), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

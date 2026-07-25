#!/usr/bin/env python3
"""Dry-run or apply the offline retired-V1 ledger compaction."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from companion_daemon.world_v2.ledger_maintenance import (
    compact_retired_v1,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-world-id")
    args = parser.parse_args()
    report = compact_retired_v1(
        args.database,
        apply=args.apply,
        confirm_world_id=args.confirm_world_id,
    )
    print(json.dumps(report.as_dict(), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()

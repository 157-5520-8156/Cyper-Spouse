#!/usr/bin/env python3
"""Run the frozen offline World v2 scenario suite and export its manifest.

This command is suitable for CI mechanism regression.  It uses a fixed fake
model/provider and explicitly does *not* represent the human-likeness blind
evaluation gate.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from companion_daemon.world_v2.scenario_runner import run_frozen_suite_sync


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workdir", type=Path, required=True, help="Disposable SQLite fixture directory")
    parser.add_argument("--output", type=Path, required=True, help="Manifest JSON output path")
    parser.add_argument("--limit", type=int, help="Run a deterministic corpus prefix for local smoke checks")
    args = parser.parse_args()
    result = run_frozen_suite_sync(workdir=args.workdir, limit=args.limit)
    manifest = result.export_manifest()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"passed": result.passed, "manifest_hash": result.manifest_hash}, sort_keys=True))
    return 0 if result.passed else 1


if __name__ == "__main__":
    sys.exit(main())

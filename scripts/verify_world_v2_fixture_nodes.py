#!/usr/bin/env python3
"""Execute the pytest evidence named by the frozen World v2 fixture manifest.

The manifest unit tests prove that every node id resolves to a real test
function.  This gate is intentionally separate: it executes the referenced
nodes, so a stale or failing piece of evidence cannot be reported as green
merely because its function still exists.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from companion_daemon.world_v2.fixture_acceptance_manifest import (  # noqa: E402
    FIXTURE_ACCEPTANCE_MANIFEST,
)


def manifest_test_nodes() -> tuple[str, ...]:
    """Return every unique evidence node in stable manifest order."""

    return tuple(
        dict.fromkeys(
            node
            for fixture in FIXTURE_ACCEPTANCE_MANIFEST
            for node in fixture.test_nodes
        )
    )


def build_command(
    *,
    python: str,
    collect_only: bool,
    maxfail: int | None,
) -> tuple[str, ...]:
    command = [python, "-m", "pytest", *manifest_test_nodes(), "-q"]
    if collect_only:
        command.append("--collect-only")
    if maxfail is not None:
        command.append(f"--maxfail={maxfail}")
    return tuple(command)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Execute every pytest evidence node in the frozen World v2 manifest."
    )
    parser.add_argument(
        "--python",
        default=sys.executable,
        help="Python interpreter used to invoke pytest.",
    )
    parser.add_argument(
        "--collect-only",
        action="store_true",
        help="Ask pytest to collect, but not execute, every manifest node.",
    )
    parser.add_argument(
        "--maxfail",
        type=int,
        default=None,
        help="Optional pytest failure limit; the default runs all evidence.",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="Print the stable node list without invoking pytest.",
    )
    args = parser.parse_args(argv)

    nodes = manifest_test_nodes()
    if args.list:
        print("\n".join(nodes))
        return 0

    command = build_command(
        python=args.python,
        collect_only=args.collect_only,
        maxfail=args.maxfail,
    )
    print(
        f"executing {len(nodes)} unique pytest evidence nodes for "
        f"{len(FIXTURE_ACCEPTANCE_MANIFEST)} frozen fixtures",
        flush=True,
    )
    return subprocess.run(command, cwd=ROOT, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())

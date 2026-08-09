#!/usr/bin/env python3
"""Run the smallest useful pytest tier for the current change.

The repository intentionally keeps the full suite as a release gate.  This
wrapper prevents everyday edits from paying the cost of all replay and public
host scenarios while keeping the tier definitions visible and reproducible.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def _required_paths(*paths: str) -> list[str]:
    """Keep tier coverage fail-closed when a test file is renamed or removed."""

    missing = [path for path in paths if not (ROOT / path).exists()]
    if missing:
        raise FileNotFoundError(
            "test tier declares missing path(s): " + ", ".join(missing)
        )
    return list(paths)


def _host_tests() -> list[str]:
    paths = sorted(
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "tests/world_v2").glob("test_delayed_trigger_*host_qualification.py")
    )
    return _required_paths(
        *paths,
        "tests/world_v2/test_delayed_trigger_qualification_matrix.py",
    )


def _has_last_failed() -> bool:
    cache = ROOT / ".pytest_cache/v/cache/lastfailed"
    try:
        value = json.loads(cache.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return False
    return isinstance(value, dict) and bool(value)


TIERS: dict[str, list[str]] = {
    "smoke": _required_paths(
        "tests/world_v2/test_inbound_tool_contract.py",
        "tests/world_v2/test_character_interior_inbound_appraisal_wire.py",
    ),
    "character": _required_paths(
        "tests/world_v2/test_character_interior_inbound_wire.py",
        "tests/world_v2/test_character_interior_inbound_author.py",
        "tests/world_v2/test_character_interior_structured_role.py",
        "tests/test_llm.py",
    ),
    "host": _required_paths(
        "tests/world_v2/test_qq_c2c_host_migration.py",
        "tests/world_v2/test_expression_retry_regressions.py",
    )
    + _host_tests(),
    "full": ["tests"],
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--tier",
        choices=tuple(TIERS),
        default="character",
        help="test tier; character is the default development gate",
    )
    parser.add_argument(
        "--last-failed",
        action="store_true",
        help="run only pytest's last-failed cache (overrides the selected paths)",
    )
    parser.add_argument(
        "--durations",
        type=int,
        default=0,
        metavar="N",
        help="show the N slowest tests (0 disables duration reporting)",
    )
    parser.add_argument(
        "pytest_args",
        nargs=argparse.REMAINDER,
        help="extra arguments passed to pytest after '--'",
    )
    args = parser.parse_args()

    command = [sys.executable, "-m", "pytest", "-q"]
    if args.last_failed:
        # Never let an empty cache silently expand this fast path to the full
        # suite.  A cache miss is reported by pytest instead of becoming a
        # surprise six-minute run.
        command.extend(["--lf", "--lfnf=none"])
    else:
        command.extend(TIERS[args.tier])
    if args.durations:
        command.extend(["--durations", str(args.durations)])
    extra = list(args.pytest_args)
    if extra[:1] == ["--"]:
        extra = extra[1:]
    command.extend(extra)

    print("$", " ".join(command), flush=True)
    if args.last_failed and not _has_last_failed():
        print("No pytest last-failed cache; nothing to run.", flush=True)
        return 0
    completed = subprocess.run(command, cwd=ROOT)
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())

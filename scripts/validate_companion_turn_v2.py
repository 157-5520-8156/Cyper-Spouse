#!/usr/bin/env python3
"""Run the local, non-external validation gates for CompanionTurn v2.

The live QQ/NapCat evidence gates are intentionally opt-in.  A fresh developer
workspace often has zero real QQ samples; default validation should still prove
the deterministic seams without pretending live platform evidence exists.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys
from collections.abc import Sequence


ROOT = Path(__file__).resolve().parents[1]

PY_COMPILE_TARGETS: tuple[str, ...] = (
    "src/companion_daemon/affective_advisory.py",
    "src/companion_daemon/companion_interruption.py",
    "src/companion_daemon/dialogue_eval.py",
    "src/companion_daemon/interaction_appraiser.py",
    "src/companion_daemon/qq_latency_eval.py",
    "src/companion_daemon/qq_runtime_observations.py",
    "src/companion_daemon/turn_taking.py",
    "src/companion_daemon/turn_frame.py",
    "src/companion_daemon/world_affect.py",
)

PYTEST_TARGETS: tuple[str, ...] = (
    "tests/test_companion_turn.py",
    "tests/test_turn_frame.py",
    "tests/test_affective_advisory.py",
    "tests/test_affective_advisory_primitives.py",
    "tests/test_affective_engine_bridge.py",
    "tests/test_companion_interruption.py",
    "tests/test_private_inner_life.py",
    "tests/test_user_affect_ledger.py",
    "tests/test_qq_websocket.py",
    "tests/test_qq_runtime_observations.py",
    "tests/test_qq_latency_eval.py",
    "tests/test_dialogue_eval.py",
    "tests/test_conversation_cadence.py",
    "tests/test_turn_taking.py",
    "tests/test_world_conversation_experience.py",
    "tests/test_world_life_affect.py",
    "tests/test_world_offense_experience.py",
    "tests/test_world_28_day_emotion_replay.py",
    "tests/test_world_longitudinal_repair.py",
    "tests/test_world_repair_lifecycle.py",
    "tests/test_world_emotion_program_integration.py",
)


def build_commands(
    *,
    python: str,
    observation_jsonl: Path,
    report_path: Path,
    include_live_qq_gates: bool,
) -> tuple[tuple[str, ...], ...]:
    commands: list[tuple[str, ...]] = [
        (python, "-m", "py_compile", *PY_COMPILE_TARGETS),
        (python, "-m", "pytest", *PYTEST_TARGETS, "-q"),
        (
            python,
            "-m",
            "companion_daemon.dialogue_eval",
            "--baseline",
            "--max-cases",
            "1",
            "--report",
            str(report_path),
        ),
        (python, "-m", "companion_daemon.qq_latency_eval", "--synthetic"),
        (
            python,
            "-m",
            "companion_daemon.qq_latency_eval",
            "--observation-jsonl",
            str(observation_jsonl),
        ),
    ]
    if include_live_qq_gates:
        commands.append(
            (
                python,
                "-m",
                "companion_daemon.qq_latency_eval",
                "--observation-jsonl",
                str(observation_jsonl),
                "--assert-live-evidence",
                "--assert-experience-evidence",
            )
        )
    return tuple(commands)


def run_commands(commands: Sequence[Sequence[str]], *, dry_run: bool) -> int:
    for command in commands:
        print("+ " + " ".join(command), flush=True)
        if dry_run:
            continue
        completed = subprocess.run(command, cwd=ROOT)
        if completed.returncode:
            return int(completed.returncode)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate local CompanionTurn v2 seams and baseline instrumentation."
    )
    parser.add_argument(
        "--python",
        default=sys.executable,
        help="Python interpreter to use for validation commands.",
    )
    parser.add_argument(
        "--observation-jsonl",
        type=Path,
        default=Path("data/private/qq-turns.jsonl"),
        help="Redacted live QQ/NapCat observation JSONL path.",
    )
    parser.add_argument(
        "--report-path",
        type=Path,
        default=Path("var/evaluation/companion-turn-v2-smoke.json"),
        help="Where to write the synthetic bare/full smoke report.",
    )
    parser.add_argument(
        "--include-live-qq-gates",
        action="store_true",
        help="Also require live QQ latency and experience evidence to pass.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print commands without executing them.",
    )
    args = parser.parse_args(argv)
    commands = build_commands(
        python=args.python,
        observation_jsonl=args.observation_jsonl,
        report_path=args.report_path,
        include_live_qq_gates=args.include_live_qq_gates,
    )
    return run_commands(commands, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())

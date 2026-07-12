"""Command-line workflow for persisted five-turn human experience reviews."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
from typing import Sequence

from companion_daemon.experience_evaluation import (
    ExperienceEvaluationError,
    append_variant_run_jsonl,
    compare_five_turn_variants,
    load_variant_runs_jsonl,
    variant_run_from_record,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Record and compare five-turn human companion-experience reviews."
    )
    commands = parser.add_subparsers(dest="command", required=True)

    record = commands.add_parser("record", help="Validate and append one annotated variant.")
    record.add_argument("input", type=Path, help="JSON file containing one five-turn variant.")
    record.add_argument("--ledger", type=Path, required=True, help="Destination JSONL ledger.")

    compare = commands.add_parser("compare", help="Compare all variants in a JSONL ledger.")
    compare.add_argument("ledger", type=Path, help="JSONL ledger created by the record command.")
    compare.add_argument("--report", type=Path, help="Optional JSON report destination.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "record":
            raw = json.loads(args.input.read_text(encoding="utf-8"))
            run = variant_run_from_record(raw)
            append_variant_run_jsonl(args.ledger, run)
            print(json.dumps({"recorded": run.variant_id, "ledger": str(args.ledger)}, ensure_ascii=False))
            return 0

        runs = load_variant_runs_jsonl(args.ledger)
        if len(runs) < 2:
            raise ExperienceEvaluationError("comparison requires at least two variants")
        comparison = compare_five_turn_variants(runs)
        rendered = json.dumps(asdict(comparison), ensure_ascii=False, indent=2, sort_keys=True)
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(rendered + "\n", encoding="utf-8")
        print(rendered)
        return 0
    except (ExperienceEvaluationError, OSError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

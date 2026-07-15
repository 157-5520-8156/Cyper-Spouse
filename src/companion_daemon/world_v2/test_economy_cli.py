"""CLI gate for a frozen World v2 model/latency trace JSON artifact."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from .test_economy import (
    MechanicalTraceGate,
    TEST_ECONOMY_V1,
    EconomyTraceError,
    trace_input_from_json,
    trace_output_json,
)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trace-json", required=True, type=Path)
    parser.add_argument("--profile", default="test-economy-v1", choices=("test-economy-v1",))
    args = parser.parse_args(argv)
    try:
        raw = json.loads(args.trace_json.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise EconomyTraceError("trace JSON root must be an object")
        model_calls, latency_samples = trace_input_from_json(raw)
        result = MechanicalTraceGate().evaluate(
            profile=TEST_ECONOMY_V1, model_calls=model_calls, latency_samples=latency_samples
        )
    except (OSError, json.JSONDecodeError, EconomyTraceError, ValueError) as exc:
        parser.error(str(exc))
    print(trace_output_json(result))
    return 0 if result.passed else 1


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())

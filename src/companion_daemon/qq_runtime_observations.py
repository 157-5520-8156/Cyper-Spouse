"""Privacy-preserving persistent observations for live QQ adapter turns.

These records are operational evidence for real latency and receipt baselines,
not conversation history.  In particular, they intentionally omit message
content, platform/user identifiers, coalescer keys, attachment references,
external receipt values, and free-form failure reasons.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from collections import Counter
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # Avoid a runtime import cycle with the QQ adapter.
    from companion_daemon.qq_websocket import TurnRuntimeObservation


class QQTurnObservationJSONLExporter:
    """Append redacted real-adapter turn evidence to a private JSONL report.

    The exporter is synchronous because the coalescer invokes its observation
    callback after a turn has reached a terminal adapter outcome.  A report
    failure must never make a user-facing turn fail; callers already isolate
    observer errors in ``QQMessageCoalescer._observe_turn``.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def __call__(self, observation: "TurnRuntimeObservation") -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        payload = {
            "schema_version": 2,
            "observed_at": observation.observed_at.isoformat(),
            "adapter": observation.adapter,
            "outcome": observation.outcome,
            "cadence": observation.cadence,
            "input_count": observation.input_count,
            "elapsed_ms": _milliseconds(observation.elapsed_seconds),
            "coalescing_wait_ms": _optional_milliseconds(
                observation.coalescing_wait_seconds
            ),
            "first_visible_elapsed_ms": _optional_milliseconds(
                observation.first_visible_elapsed_seconds
            ),
            "seen_elapsed_ms": _optional_milliseconds(
                observation.seen_elapsed_seconds
            ),
            "typing_elapsed_ms": _optional_milliseconds(
                observation.typing_elapsed_seconds
            ),
            "model_returned_elapsed_ms": _optional_milliseconds(
                observation.model_returned_elapsed_seconds
            ),
            "candidate_accepted_elapsed_ms": _optional_milliseconds(
                observation.candidate_accepted_elapsed_seconds
            ),
            "delivery_settled_elapsed_ms": _optional_milliseconds(
                observation.delivery_settled_elapsed_seconds
            ),
            "failure_type": observation.failure_type,
            "action_ids": list(observation.action_ids),
            "message_kinds": list(observation.message_kinds),
            "segment_ids": list(observation.segment_ids),
            "segment_count": len(observation.segment_ids),
            "multi_segment": len(observation.segment_ids) > 1,
            "affective_reading_kinds": list(observation.affective_reading_kinds),
            "expression_affordance_candidate_kinds": list(
                observation.expression_affordance_candidate_kinds
            ),
            "selected_affordance_kind": observation.selected_affordance_kind,
            "user_affect_kinds": list(observation.user_affect_kinds),
            "user_affect_recorded": observation.user_affect_recorded,
            "private_impression_recorded": observation.private_impression_recorded,
            "durable_receipt_status": observation.durable_receipt_status,
            "recovery_result": observation.recovery_result,
        }
        encoded = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode(
            "utf-8"
        )
        descriptor = os.open(
            self.path,
            os.O_WRONLY | os.O_APPEND | os.O_CREAT,
            0o600,
        )
        try:
            # A pre-existing report may have been created with a permissive
            # umask.  Tighten it on every write without exposing its contents.
            os.fchmod(descriptor, 0o600)
            os.write(descriptor, encoded)
        finally:
            os.close(descriptor)


def load_qq_turn_observation_jsonl(path: Path) -> tuple[dict[str, object], ...]:
    """Load a private redacted QQ observation JSONL file.

    This helper intentionally returns only the already-redacted operational
    rows.  The exporter never writes message content, platform/user ids,
    external receipts, attachment refs or free-form failure reasons, so this
    reader must not try to reconstruct them.
    """
    rows: list[dict[str, object]] = []
    source = Path(path)
    if not source.exists():
        return ()
    for line in source.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        payload = json.loads(line)
        if isinstance(payload, dict):
            rows.append(dict(payload))
    return tuple(rows)


def summarize_qq_turn_experience(
    rows: Iterable[Mapping[str, object]],
) -> dict[str, object]:
    """Summarize redacted live-turn experience signals without content access."""
    materialized = tuple(rows)
    selected_affordances: Counter[str] = Counter()
    affective_readings: Counter[str] = Counter()
    message_kinds: Counter[str] = Counter()
    user_affect_kinds: Counter[str] = Counter()
    outcomes: Counter[str] = Counter()
    cadences: Counter[str] = Counter()
    multi_segment_count = 0
    single_bubble_reply_count = 0
    user_affect_recorded_count = 0
    private_impression_recorded_count = 0
    for row in materialized:
        outcomes[str(row.get("outcome") or "unknown")] += 1
        cadences[str(row.get("cadence") or "unknown")] += 1
        if bool(row.get("multi_segment")) or int(row.get("segment_count") or 0) > 1:
            multi_segment_count += 1
        selected = str(row.get("selected_affordance_kind") or "")
        if selected:
            selected_affordances[selected] += 1
        row_message_kinds = tuple(
            kind for kind in row.get("message_kinds") or () if isinstance(kind, str) and kind
        )
        for kind in row_message_kinds:
            message_kinds[kind] += 1
        if row_message_kinds == ("reply",) and int(row.get("segment_count") or 0) == 1:
            single_bubble_reply_count += 1
        for kind in row.get("affective_reading_kinds") or ():
            if isinstance(kind, str) and kind:
                affective_readings[kind] += 1
        for kind in row.get("user_affect_kinds") or ():
            if isinstance(kind, str) and kind:
                user_affect_kinds[kind] += 1
        if bool(row.get("user_affect_recorded")):
            user_affect_recorded_count += 1
        if bool(row.get("private_impression_recorded")):
            private_impression_recorded_count += 1
    sample_count = len(materialized)
    top_affordance_count = max(selected_affordances.values(), default=0)
    top_affordance_rate = _rate(top_affordance_count, sample_count)
    return {
        "schema_version": 1,
        "sample_count": sample_count,
        "outcome_counts": dict(sorted(outcomes.items())),
        "cadence_counts": dict(sorted(cadences.items())),
        "multi_segment_count": multi_segment_count,
        "multi_segment_rate": _rate(multi_segment_count, sample_count),
        "single_bubble_reply_count": single_bubble_reply_count,
        "single_bubble_reply_rate": _rate(single_bubble_reply_count, sample_count),
        "message_kind_counts": dict(sorted(message_kinds.items())),
        "afterthought_count": message_kinds.get("afterthought", 0),
        "afterthought_rate": _rate(message_kinds.get("afterthought", 0), sample_count),
        "selected_affordance_counts": dict(sorted(selected_affordances.items())),
        "top_selected_affordance_rate": top_affordance_rate,
        "fixed_pattern_diagnostic": _fixed_pattern_diagnostic(
            sample_count=sample_count,
            top_selected_affordance_rate=top_affordance_rate,
        ),
        "affective_reading_counts": dict(sorted(affective_readings.items())),
        "user_affect_kind_counts": dict(sorted(user_affect_kinds.items())),
        "user_affect_recorded_count": user_affect_recorded_count,
        "user_affect_recorded_rate": _rate(user_affect_recorded_count, sample_count),
        "affective_correction_signal_count": user_affect_kinds.get("repaired", 0),
        "affective_correction_signal_rate": _rate(
            user_affect_kinds.get("repaired", 0), sample_count
        ),
        "private_impression_recorded_count": private_impression_recorded_count,
        "private_impression_recorded_rate": _rate(
            private_impression_recorded_count, sample_count
        ),
        "privacy": {
            "contains_message_text": False,
            "contains_user_or_platform_identifier": False,
            "contains_external_receipts": False,
            "contains_free_form_failure_reason": False,
        },
    }


def _milliseconds(seconds: float) -> int:
    return max(0, int(seconds * 1_000))


def _optional_milliseconds(seconds: float | None) -> int | None:
    return _milliseconds(seconds) if seconds is not None else None


def _rate(count: int, sample_count: int) -> float:
    if sample_count <= 0:
        return 0.0
    return round(count / sample_count, 4)


def _fixed_pattern_diagnostic(
    *, sample_count: int, top_selected_affordance_rate: float
) -> str:
    if sample_count < 8:
        return "insufficient_sample"
    if top_selected_affordance_rate >= 0.7:
        return "possible_fixed_pattern"
    return "varied"


def _main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Summarize privacy-preserving QQ/NapCat turn observations without "
            "reading message content or user identifiers."
        )
    )
    parser.add_argument(
        "path",
        type=Path,
        help="Path to the redacted QQ turn observation JSONL file.",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="Pretty-print the JSON report for manual inspection.",
    )
    args = parser.parse_args(tuple(argv) if argv is not None else None)

    summary = summarize_qq_turn_experience(load_qq_turn_observation_jsonl(args.path))
    json.dump(
        summary,
        sys.stdout,
        ensure_ascii=False,
        indent=2 if args.pretty else None,
        sort_keys=True,
    )
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())

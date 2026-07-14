"""QQ coalescer end-to-end latency instrumentation and a smoke baseline.

This is intentionally separate from ``dialogue_eval``.  The latter starts at
model invocation; this module starts when the first input enters a QQ merge
batch and stops at the first durable QQ text receipt, so debounce time cannot
be accidentally omitted.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict, dataclass
import json
from pathlib import Path
from collections.abc import Iterable, Mapping

from companion_daemon.conversation_cadence import ConversationCadence
from companion_daemon.models import CompanionReply, IncomingMessage
from companion_daemon.qq_runtime_observations import (
    load_qq_turn_observation_jsonl,
    summarize_qq_turn_experience,
)
from companion_daemon.qq_websocket import QQMessageCoalescer, TurnRuntimeObservation
from companion_daemon.turn_taking import TurnTakingPolicy
from companion_daemon.usage_metrics import nearest_rank


CADENCES = ("all", "cold", "warm", "hot")
_LIVE_QQ_MIN_HOT_SAMPLES = 8
_LIVE_QQ_HOT_P95_TARGET_MS = 7_000


@dataclass(frozen=True)
class QQLatencySummary:
    cadence: str
    sample_count: int
    visible_count: int
    p50_first_visible_ms: int | None
    p95_first_visible_ms: int | None
    p50_complete_ms: int | None
    p95_complete_ms: int | None


def qq_latency_definition() -> dict[str, object]:
    return {
        "first_visible_metric": (
            "monotonic time from the first input admitted to one QQ coalescing batch "
            "to the first QQ text reply with a durable receipt"
        ),
        "complete_metric": (
            "monotonic time from the first input admitted to one QQ coalescing batch "
            "until that turn reaches a terminal adapter outcome"
        ),
        "cadence_source": "the cadence frozen when the coalescer chose its debounce policy",
        "excludes": "server-to-client rendering after the QQ adapter returns its receipt",
        "live_hot_sample_minimum": _LIVE_QQ_MIN_HOT_SAMPLES,
        "live_hot_p95_target_ms": _LIVE_QQ_HOT_P95_TARGET_MS,
    }


def summarize_qq_latency(
    observations: Iterable[TurnRuntimeObservation],
) -> tuple[QQLatencySummary, ...]:
    """Summarize completed QQ coalescer observations without hiding failures."""
    rows = tuple(observations)
    summaries: list[QQLatencySummary] = []
    for cadence in CADENCES:
        selected = rows if cadence == "all" else tuple(
            row for row in rows if row.cadence == cadence
        )
        visible = sorted(
            max(0, int(row.first_visible_elapsed_seconds * 1_000))
            for row in selected
            if row.first_visible_elapsed_seconds is not None
        )
        complete = sorted(max(0, int(row.elapsed_seconds * 1_000)) for row in selected)
        summaries.append(
            QQLatencySummary(
                cadence=cadence,
                sample_count=len(selected),
                visible_count=len(visible),
                p50_first_visible_ms=nearest_rank(visible, 0.50) if visible else None,
                p95_first_visible_ms=nearest_rank(visible, 0.95) if visible else None,
                p50_complete_ms=nearest_rank(complete, 0.50) if complete else None,
                p95_complete_ms=nearest_rank(complete, 0.95) if complete else None,
            )
        )
    return tuple(summaries)


def qq_latency_report(observations: Iterable[TurnRuntimeObservation]) -> dict[str, object]:
    """Build a JSON-safe synthetic report without losing the observed time."""
    rows = tuple(observations)
    return {
        "live": False,
        "definition": qq_latency_definition(),
        "observations": [
            {
                **asdict(row),
                "observed_at": row.observed_at.isoformat(),
            }
            for row in rows
        ],
        "summaries": [asdict(row) for row in summarize_qq_latency(rows)],
    }


def summarize_qq_latency_observation_rows(
    rows: Iterable[Mapping[str, object]],
) -> tuple[QQLatencySummary, ...]:
    """Summarize already-redacted live QQ/NapCat observation rows.

    The JSONL exporter stores millisecond fields rather than raw message
    content.  This helper keeps the same cadence buckets as the synthetic
    coalescer report, but it never requires reconstructing a
    ``TurnRuntimeObservation`` or touching private identifiers.
    """
    materialized = tuple(rows)
    summaries: list[QQLatencySummary] = []
    for cadence in CADENCES:
        selected = (
            materialized
            if cadence == "all"
            else tuple(row for row in materialized if row.get("cadence") == cadence)
        )
        visible = sorted(
            int(value)
            for row in selected
            if isinstance(value := row.get("first_visible_elapsed_ms"), int)
        )
        complete = sorted(
            int(value)
            for row in selected
            if isinstance(value := row.get("elapsed_ms"), int)
        )
        summaries.append(
            QQLatencySummary(
                cadence=cadence,
                sample_count=len(selected),
                visible_count=len(visible),
                p50_first_visible_ms=nearest_rank(visible, 0.50) if visible else None,
                p95_first_visible_ms=nearest_rank(visible, 0.95) if visible else None,
                p50_complete_ms=nearest_rank(complete, 0.50) if complete else None,
                p95_complete_ms=nearest_rank(complete, 0.95) if complete else None,
            )
        )
    return tuple(summaries)


def qq_latency_observation_jsonl_report(path: Path) -> dict[str, object]:
    """Build a JSON-safe live QQ/NapCat baseline report from redacted JSONL."""
    rows = load_qq_turn_observation_jsonl(path)
    summaries = summarize_qq_latency_observation_rows(rows)
    evidence = assess_live_qq_observation_evidence(summaries)
    return {
        "live": True,
        "source": "redacted_qq_turn_observation_jsonl",
        "path": str(path),
        "definition": qq_latency_definition(),
        "summaries": [asdict(row) for row in summaries],
        "evidence_status": evidence["status"],
        "evidence_reasons": evidence["reasons"],
        "experience_summary": summarize_qq_turn_experience(rows),
        "privacy": {
            "contains_message_text": False,
            "contains_user_or_platform_identifier": False,
            "contains_external_receipts": False,
            "contains_free_form_failure_reason": False,
        },
    }


def assess_live_qq_observation_evidence(
    summaries: Iterable[QQLatencySummary],
) -> dict[str, object]:
    """Assess whether redacted live QQ evidence is enough to trust yet.

    This is intentionally a diagnostic for the report, not a process exit
    gate.  Human experience still needs inspection; this only prevents empty
    or tiny JSONL files from looking like a completed live baseline.
    """
    by_cadence = {item.cadence: item for item in summaries}
    hot = by_cadence.get("hot")
    reasons: list[str] = []
    sufficient_sample = hot is not None and hot.sample_count >= _LIVE_QQ_MIN_HOT_SAMPLES
    complete_visible = hot is not None and hot.visible_count == hot.sample_count
    if hot is None or hot.sample_count < _LIVE_QQ_MIN_HOT_SAMPLES:
        observed = hot.sample_count if hot is not None else 0
        reasons.append(
            f"Need at least {_LIVE_QQ_MIN_HOT_SAMPLES} hot QQ/NapCat turns; observed {observed}."
        )
    if hot is None or hot.visible_count < hot.sample_count:
        observed_visible = hot.visible_count if hot is not None else 0
        observed_total = hot.sample_count if hot is not None else 0
        reasons.append(
            f"Need first-visible timing for every hot turn; observed {observed_visible}/{observed_total}."
        )
    if hot is None or hot.p95_first_visible_ms is None:
        reasons.append("Hot P95 first-visible latency is unavailable.")
    if not sufficient_sample or not complete_visible or hot is None or hot.p95_first_visible_ms is None:
        return {"status": "insufficient_evidence", "reasons": reasons}
    if hot.p95_first_visible_ms > _LIVE_QQ_HOT_P95_TARGET_MS:
        reasons.append(
            f"Hot P95 first-visible latency {hot.p95_first_visible_ms}ms exceeds {_LIVE_QQ_HOT_P95_TARGET_MS}ms."
        )
        return {"status": "latency_watch", "reasons": reasons}
    return {"status": "pass", "reasons": []}


async def run_synthetic_qq_latency_smoke() -> tuple[TurnRuntimeObservation, ...]:
    """Exercise the actual QQ coalescer seam without a provider or QQ account.

    It verifies only the measurement contract.  It must never be presented as
    live-model or live-network latency evidence.
    """

    class Engine:
        def conversation_cadence(self, incoming: IncomingMessage) -> ConversationCadence:
            heat = incoming.text.split(":", 1)[0]
            return ConversationCadence(heat, None, 0, "synthetic_smoke")

        async def handle_message(self, incoming: IncomingMessage, **_kwargs: object) -> CompanionReply:
            return CompanionReply(canonical_user_id="eval", mood="calm", text=f"收到 {incoming.text}")

    class Target:
        async def reply(self, **_kwargs: object) -> dict[str, str]:
            return {"id": "synthetic-qq-receipt"}

    observations: list[TurnRuntimeObservation] = []
    coalescer = QQMessageCoalescer(
        Engine(),  # type: ignore[arg-type]
        delay_seconds=0.01,
        turn_policy=TurnTakingPolicy(short_wait_seconds=0.01, long_wait_seconds=0.01),
        on_turn_observation=observations.append,
    )
    target = Target()
    for cadence in ("cold", "warm", "hot"):
        await coalescer.add(
            f"c2c:{cadence}",
            IncomingMessage(
                platform="qq",
                platform_user_id="eval",
                message_id=f"synthetic-{cadence}",
                text=f"{cadence}: 这句说完了。",
            ),
            target,
        )
    await asyncio.sleep(0.08)
    return tuple(observations)


def _main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="QQ coalescer latency baseline utilities")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--synthetic",
        action="store_true",
        help="run an in-process instrumentation smoke test; not live latency evidence",
    )
    mode.add_argument(
        "--observation-jsonl",
        type=Path,
        help=(
            "summarize a redacted live QQ/NapCat observation JSONL file created "
            "by QQ_TURN_OBSERVATION_PATH"
        ),
    )
    parser.add_argument(
        "--assert-live-evidence",
        action="store_true",
        help=(
            "when reading --observation-jsonl, exit non-zero unless evidence_status "
            "is pass; useful for live QQ/NapCat validation gates"
        ),
    )
    args = parser.parse_args(tuple(argv) if argv is not None else None)
    if args.assert_live_evidence and args.observation_jsonl is None:
        parser.error("--assert-live-evidence requires --observation-jsonl")
    if args.observation_jsonl is not None:
        report = qq_latency_observation_jsonl_report(args.observation_jsonl)
    else:
        observations = asyncio.run(run_synthetic_qq_latency_smoke())
        report = qq_latency_report(observations)
    print(
        json.dumps(report, ensure_ascii=False, indent=2)
    )
    if args.assert_live_evidence and report.get("evidence_status") != "pass":
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through the module CLI
    raise SystemExit(_main())

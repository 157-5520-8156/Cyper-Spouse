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
from typing import Iterable

from companion_daemon.conversation_cadence import ConversationCadence
from companion_daemon.models import CompanionReply, IncomingMessage
from companion_daemon.qq_websocket import QQMessageCoalescer, TurnRuntimeObservation
from companion_daemon.turn_taking import TurnTakingPolicy
from companion_daemon.usage_metrics import nearest_rank


CADENCES = ("all", "cold", "warm", "hot")


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


def _main() -> int:
    parser = argparse.ArgumentParser(description="QQ coalescer latency baseline utilities")
    parser.add_argument(
        "--synthetic",
        action="store_true",
        help="run an in-process instrumentation smoke test; not live latency evidence",
    )
    args = parser.parse_args()
    if not args.synthetic:
        parser.error("pass --synthetic to run the instrumentation smoke test")
    observations = asyncio.run(run_synthetic_qq_latency_smoke())
    print(
        json.dumps(
            {
                "live": False,
                "definition": qq_latency_definition(),
                "observations": [asdict(row) for row in observations],
                "summaries": [asdict(row) for row in summarize_qq_latency(observations)],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through the module CLI
    raise SystemExit(_main())

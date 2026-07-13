"""Privacy-preserving persistent observations for live QQ adapter turns.

These records are operational evidence for real latency and receipt baselines,
not conversation history.  In particular, they intentionally omit message
content, platform/user identifiers, coalescer keys, attachment references,
external receipt values, and free-form failure reasons.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
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
            "segment_ids": list(observation.segment_ids),
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


def _milliseconds(seconds: float) -> int:
    return max(0, int(seconds * 1_000))


def _optional_milliseconds(seconds: float | None) -> int | None:
    return _milliseconds(seconds) if seconds is not None else None

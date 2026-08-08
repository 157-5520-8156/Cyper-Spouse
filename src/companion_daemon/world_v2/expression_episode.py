"""Diagnostics and validation shared by current expression interfaces.

The former :class:`ExpressionEpisode` provisional/full coordinator was a live
two-author race.  It is intentionally absent: immutable ledger events retain
their schema/reducer compatibility, while current execution is either one
complete author (``off``), an observational shadow (``shadow``), or semantic
units from one physical role-author stream (``stream``).
"""

from __future__ import annotations

from threading import Lock
from typing import Literal

from .proposal_envelope import DecisionProposal, ProposalInput, validate_proposal_envelope
from .unified_inbound_decision import inspect_unified_inbound_decision


ExpressionEpisodeMode = Literal["off", "shadow", "stream"]


def validate_provisional_proposal(proposal: ProposalInput | dict[str, object]) -> str:
    """Validate an observational shadow candidate's single text beat.

    The name is retained because old shadow audit code records the historical
    candidate vocabulary.  Passing validation grants no Action authority.
    """

    if isinstance(proposal, dict):
        proposal = validate_proposal_envelope(proposal)
    if not isinstance(proposal, DecisionProposal):
        raise ValueError("provisional requires a typed DecisionProposal")
    if proposal.timing_choice != "now" or len(proposal.action_intents) != 1:
        raise ValueError("provisional requires one immediate Action intent")
    intent = proposal.action_intents[0]
    if intent.kind != "reply" or intent.layer != "external_action":
        raise ValueError("provisional requires one text reply intent")
    shape = inspect_unified_inbound_decision(proposal)
    change = shape.expression
    if change is None:
        raise ValueError("provisional requires one accepted expression transition")
    payload = change.payload.value()
    beats = payload.get("beat_drafts")
    if not isinstance(beats, list) or len(beats) != 1 or not isinstance(beats[0], dict):
        raise ValueError("provisional requires exactly one beat")
    beat = beats[0]
    text = beat.get("inline_text")
    if (
        not isinstance(text, str)
        or not text.strip()
        or beat.get("content_type") != "text/plain"
    ):
        raise ValueError("provisional text is empty or non-text")
    return text.strip()


class ExpressionEpisodeDiagnostics:
    """Process-local expression-interface evidence; never stores candidate text."""

    def __init__(self, *, mode: ExpressionEpisodeMode = "shadow") -> None:
        if mode not in {"off", "shadow", "stream"}:
            raise ValueError("expression episode mode must be off, shadow, or stream")
        self._mode = mode
        self._lock = Lock()
        self._counts: dict[str, int] = {
            "turns": 0,
            "candidate_valid": 0,
            "candidate_rejected": 0,
            "full_first": 0,
            "provisional_first": 0,
            "would_send": 0,
            "would_append": 0,
            "would_stop": 0,
            "slot_calls": 0,
            "grounding_rejected": 0,
            "placeholder_rejected": 0,
            "other_rejected": 0,
        }
        self._candidate_ms: list[float] = []
        self._full_ms: list[float] = []

    @property
    def mode(self) -> ExpressionEpisodeMode:
        return self._mode

    def record(
        self,
        *,
        candidate_ms: float | None,
        valid: bool,
        winner: Literal["full", "provisional"],
        would_send: bool,
        would_append: bool = False,
        slot_calls: int,
        rejection_kind: Literal["grounding", "placeholder", "other"] | None = None,
    ) -> None:
        with self._lock:
            self._counts["turns"] += 1
            self._counts["candidate_valid" if valid else "candidate_rejected"] += 1
            self._counts[f"{winner}_first"] += 1
            self._counts["would_send" if would_send else "would_stop"] += 1
            self._counts["would_append"] += int(would_append)
            self._counts["slot_calls"] += slot_calls
            if rejection_kind is not None:
                self._counts[f"{rejection_kind}_rejected"] += 1
            if candidate_ms is not None:
                self._candidate_ms.append(max(0.0, candidate_ms))
                if len(self._candidate_ms) > 2_048:
                    del self._candidate_ms[: len(self._candidate_ms) - 2_048]

    def record_full(self, full_ms: float) -> None:
        with self._lock:
            self._full_ms.append(max(0.0, full_ms))
            if len(self._full_ms) > 2_048:
                del self._full_ms[: len(self._full_ms) - 2_048]

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            counts = dict(self._counts)
            values = sorted(self._candidate_ms)
            full_values = sorted(self._full_ms)

        def percentile(source: list[float], fraction: float) -> float | None:
            if not source:
                return None
            index = min(len(source) - 1, max(0, int((len(source) - 1) * fraction)))
            return round(source[index], 3)

        active_reply_interface = {
            "stream": "fast_stream",
            "off": "delayed_attention_complete",
            "shadow": "complete_response_shadow",
        }[self._mode]
        return {
            "mode": self._mode,
            "active_reply_interface": active_reply_interface,
            "reserved_reply_interface": {
                "name": "delayed_attention_complete",
                "status": "active" if self._mode == "off" else "disabled",
                "reserved_for": "character_unavailable_or_delayed_attention",
            },
            **counts,
            "candidate_ms_p50": percentile(values, 0.50),
            "candidate_ms_p95": percentile(values, 0.95),
            "candidate_ms_max": round(values[-1], 3) if values else None,
            "full_ms_p50": percentile(full_values, 0.50),
            "full_ms_p95": percentile(full_values, 0.95),
            "full_ms_max": round(full_values[-1], 3) if full_values else None,
        }


__all__ = [
    "ExpressionEpisodeDiagnostics",
    "ExpressionEpisodeMode",
    "validate_provisional_proposal",
]

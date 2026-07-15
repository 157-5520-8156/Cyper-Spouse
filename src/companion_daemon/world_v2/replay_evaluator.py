"""Deterministic Phase-8 mechanism checks for a World v2 replay.

This is not a classifier of whether prose sounds human.  It verifies the
observable causal promises that make long-term companion behaviour testable.
"""

from __future__ import annotations

from dataclasses import dataclass

from .schemas import LedgerProjection


@dataclass(frozen=True, slots=True)
class ReplayFinding:
    code: str
    severity: str
    detail: str


@dataclass(frozen=True, slots=True)
class ReplayEvaluation:
    evaluator_version: str
    world_id: str
    world_revision: int
    replay_hash_matches: bool
    mechanism_checks: tuple[str, ...]
    findings: tuple[ReplayFinding, ...]

    @property
    def passed(self) -> bool:
        return not any(item.severity == "error" for item in self.findings)


class ReplayEvaluator:
    """Pure checker over a live projection and a zero-side-effect replay."""

    version = "world-v2-replay-evaluator.1"

    def evaluate(
        self, *, projection: LedgerProjection, replay: LedgerProjection
    ) -> ReplayEvaluation:
        findings: list[ReplayFinding] = []
        if projection.world_id != replay.world_id:
            findings.append(ReplayFinding("world_mismatch", "error", "replay belongs to another world"))
        if projection.semantic_hash != replay.semantic_hash:
            findings.append(ReplayFinding("replay_hash_mismatch", "error", "semantic replay differs"))
        payloads = {item.payload_ref: item for item in projection.stored_message_payloads}
        beats = {item.beat_id: item for item in projection.expression_beats}
        reservations = {item.reservation_id: item for item in projection.budget_reservations}
        for action in projection.actions:
            if action.kind != "reply":
                continue
            if action.payload_ref not in payloads or payloads[action.payload_ref].payload_hash != action.payload_hash:
                findings.append(ReplayFinding("reply_payload_missing", "error", action.action_id))
            if not any(beat.payload_ref == action.payload_ref and beat.payload_hash == action.payload_hash for beat in beats.values()):
                findings.append(ReplayFinding("reply_beat_missing", "error", action.action_id))
            reservation = reservations.get(action.budget_reservation_id)
            if reservation is None or reservation.action_id != action.action_id:
                findings.append(ReplayFinding("reply_budget_missing", "error", action.action_id))
        checks = (
            "replay_semantic_hash",
            "reply_expression_payload_binding",
            "reply_budget_action_binding",
            "affect_relationship_memory_npc_projection_visibility",
        )
        return ReplayEvaluation(
            evaluator_version=self.version,
            world_id=projection.world_id,
            world_revision=projection.world_revision,
            replay_hash_matches=projection.semantic_hash == replay.semantic_hash,
            mechanism_checks=checks,
            findings=tuple(findings),
        )


__all__ = ["ReplayEvaluation", "ReplayEvaluator", "ReplayFinding"]

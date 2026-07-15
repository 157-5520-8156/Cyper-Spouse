"""Deterministic Phase-8 mechanism checks for a World v2 replay.

This is not a classifier of whether prose sounds human.  It verifies the
observable causal promises that make long-term companion behaviour testable.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib

from .ledger import canonical_event_json, commit_request_hash
from .replay_evidence import ReplayEvidence, ReplayEventEvidence

from .schemas import CommitResult, LedgerProjection, ProjectionCursor


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
        self,
        *,
        projection: LedgerProjection | None = None,
        replay: LedgerProjection | None = None,
        evidence: ReplayEvidence | None = None,
    ) -> ReplayEvaluation:
        if evidence is not None:
            if projection is not None or replay is not None:
                raise ValueError("replay evidence cannot be combined with separate projections")
            projection, replay = evidence.projection, evidence.replay
        elif projection is None or replay is None:
            raise ValueError("projection and replay are required without replay evidence")
        findings: list[ReplayFinding] = []
        if projection.world_id != replay.world_id:
            findings.append(ReplayFinding("world_mismatch", "error", "replay belongs to another world"))
        if projection.semantic_hash != replay.semantic_hash:
            findings.append(ReplayFinding("replay_hash_mismatch", "error", "semantic replay differs"))
        payloads = {item.payload_ref: item for item in projection.stored_message_payloads}
        sidecar_payloads = {
            item.payload_ref: item for item in projection.expression_payload_descriptors
        }
        beats = {item.beat_id: item for item in projection.expression_beats}
        reservations = {item.reservation_id: item for item in projection.budget_reservations}
        for action in projection.actions:
            if action.kind != "reply":
                continue
            inline = payloads.get(action.payload_ref)
            sidecar = sidecar_payloads.get(action.payload_ref)
            if (
                (inline is None or inline.payload_hash != action.payload_hash)
                and (sidecar is None or sidecar.payload_hash != action.payload_hash)
            ):
                findings.append(ReplayFinding("reply_payload_missing", "error", action.action_id))
            if not any(beat.payload_ref == action.payload_ref and beat.payload_hash == action.payload_hash for beat in beats.values()):
                findings.append(ReplayFinding("reply_beat_missing", "error", action.action_id))
            reservation = reservations.get(action.budget_reservation_id)
            if reservation is None or reservation.action_id != action.action_id:
                findings.append(ReplayFinding("reply_budget_missing", "error", action.action_id))
        self._check_action_recovery(projection, findings)
        if evidence is not None:
            self._check_evidence(evidence, findings)
        checks = (
            *(("same_cursor_replay_evidence",) if evidence is not None else ()),
            "replay_semantic_hash",
            "reply_expression_payload_binding",
            "reply_budget_action_binding",
            "action_recovery_liveness",
        )
        return ReplayEvaluation(
            evaluator_version=self.version,
            world_id=projection.world_id,
            world_revision=projection.world_revision,
            replay_hash_matches=projection.semantic_hash == replay.semantic_hash,
            mechanism_checks=checks,
            findings=tuple(findings),
        )

    @staticmethod
    def _check_action_recovery(
        projection: LedgerProjection, findings: list[ReplayFinding]
    ) -> None:
        """Flag state that a durable ActionPump should have recovered.

        This is deliberately a replay-time diagnostic rather than a reducer
        invariant: a short-lived claimed/dispatch-started action is legitimate
        during normal execution, while one past its recorded logical deadline
        is a liveness failure that must be visible to CI and operators.
        """

        logical_time = projection.logical_time
        reservations = {item.reservation_id: item for item in projection.budget_reservations}
        terminal = {"delivered", "failed", "unknown", "cancelled", "expired"}
        for action in projection.actions:
            reservation = reservations.get(action.budget_reservation_id)
            if action.state in terminal and (reservation is None or reservation.state == "reserved"):
                findings.append(
                    ReplayFinding("terminal_action_budget_unsettled", "error", action.action_id)
                )
            if logical_time is None:
                continue
            if (
                action.expires_at is not None
                and logical_time >= action.expires_at
                and action.state in {"authorized", "scheduled", "claimed"}
            ):
                findings.append(ReplayFinding("action_expired_unrecovered", "error", action.action_id))
            if (
                action.state == "dispatch_started"
                and action.dispatch_pending is not None
                and logical_time >= action.dispatch_pending.deadline
            ):
                findings.append(
                    ReplayFinding("dispatch_pending_deadline_elapsed", "error", action.action_id)
                )
            lease = action.claim_lease
            if lease is not None and logical_time >= lease.expires_at:
                if action.state == "claimed":
                    findings.append(ReplayFinding("action_claim_expired", "error", action.action_id))
                elif action.state == "provider_accepted":
                    findings.append(
                        ReplayFinding("provider_ack_without_terminal_receipt", "error", action.action_id)
                    )
                elif action.state == "dispatch_started":
                    pending = action.dispatch_pending
                    if pending is None:
                        findings.append(
                            ReplayFinding("dispatch_started_without_recovery", "error", action.action_id)
                        )

    @staticmethod
    def _check_evidence(evidence: ReplayEvidence, findings: list[ReplayFinding]) -> None:
        projection = evidence.projection
        if evidence.world_id != projection.world_id:
            findings.append(ReplayFinding("evidence_world_mismatch", "error", evidence.world_id))
        projection_cursor = ProjectionCursor(
            world_revision=projection.world_revision,
            deliberation_revision=projection.deliberation_revision,
            ledger_sequence=projection.ledger_sequence,
        )
        if evidence.cursor != projection_cursor:
            findings.append(ReplayFinding("evidence_cursor_mismatch", "error", "projection cursor"))
        if evidence.replay.world_id != evidence.world_id:
            findings.append(ReplayFinding("replay_evidence_world_mismatch", "error", "replay world"))

        events_by_commit: dict[str, list[ReplayEventEvidence]] = {}
        last_sequence = 0
        for item in evidence.events:
            if item.cursor.ledger_sequence != last_sequence + 1:
                findings.append(ReplayFinding("evidence_event_sequence", "error", item.event.event_id))
            last_sequence = item.cursor.ledger_sequence
            expected_hash = hashlib.sha256(
                canonical_event_json(item.event).encode("utf-8")
            ).hexdigest()
            if item.event_envelope_hash != expected_hash:
                findings.append(ReplayFinding("evidence_event_hash", "error", item.event.event_id))
            events_by_commit.setdefault(item.commit_id, []).append(item)
        if last_sequence != evidence.cursor.ledger_sequence:
            findings.append(ReplayFinding("evidence_event_tail", "error", str(last_sequence)))

        if tuple(events_by_commit) != tuple(item.commit_id for item in evidence.commits):
            findings.append(ReplayFinding("evidence_commit_order", "error", "commit/event ordering"))
        for commit in evidence.commits:
            events = events_by_commit.get(commit.commit_id)
            if not events:
                findings.append(ReplayFinding("evidence_empty_commit", "error", commit.commit_id))
                continue
            commit_events = tuple(item.event for item in events)
            if commit.request_hash != commit_request_hash(commit_events):
                findings.append(ReplayFinding("evidence_commit_request_hash", "error", commit.commit_id))
            last = events[-1]
            expected_result = CommitResult(
                world_revision=last.cursor.world_revision,
                deliberation_revision=last.cursor.deliberation_revision,
                ledger_sequence=last.cursor.ledger_sequence,
                event_ids=tuple(item.event.event_id for item in events),
            )
            if commit.result != expected_result:
                findings.append(ReplayFinding("evidence_commit_result", "error", commit.commit_id))


__all__ = ["ReplayEvaluation", "ReplayEvaluator", "ReplayFinding"]

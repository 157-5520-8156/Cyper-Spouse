"""Provider-free settlement of audited, role-authored interaction acts."""

from __future__ import annotations

import json
from typing import Literal

from .interaction_act_acceptance_manifest import (
    INTERACTION_ACT_ACCEPTANCE_MANIFEST_VERSION,
    InteractionActAcceptanceManifest,
)
from .interaction_act_acceptance_runtime import InteractionActAcceptanceRuntime
from .interaction_act_events import (
    InteractionActAcceptedPayload,
    InteractionActProposalRecordedPayload,
    canonical_interaction_act_change_hash,
)
from .interaction_act_proposal_compiler import InteractionActProposalCompiler
from .ledger import LedgerPort
from .proposal_envelope import DecisionProposal, validate_proposal_envelope
from .schema_core import FrozenModel
from .schemas import CommitResult, ProjectionCursor


class InteractionActWorkerError(ValueError):
    """Stable technical or authority failure during background settlement."""

    def __init__(self, code: str) -> None:
        self.code = f"interaction_act_worker.{code}"
        super().__init__(self.code)


class InteractionActWorkResult(FrozenModel):
    status: Literal["accepted"] = "accepted"
    source_proposal_id: str
    typed_proposal_id: str
    compile_commit: CommitResult
    acceptance_commit: CommitResult


class InteractionActWorker:
    """Compile and atomically accept at most one immutable interaction act."""

    def __init__(
        self,
        *,
        ledger: LedgerPort,
        compiler: InteractionActProposalCompiler,
        acceptance: InteractionActAcceptanceRuntime,
        actor: str,
        source: str = "world-v2:interaction-act-worker",
    ) -> None:
        if not actor or not source:
            raise ValueError("interaction act worker needs actor and source")
        if compiler.ledger is not ledger:
            raise ValueError("interaction act worker compiler must own the same ledger")
        if acceptance.ledger is not ledger:
            raise ValueError("interaction act worker acceptance must own the same ledger")
        self._ledger = ledger
        self._compiler = compiler
        self._acceptance = acceptance
        self._actor = actor
        self._source = source

    @property
    def ledger(self) -> LedgerPort:
        return self._ledger

    async def drain_one(self) -> InteractionActWorkResult | None:
        projection = self._ledger.project()
        current_cursor = ProjectionCursor(
            world_revision=projection.world_revision,
            deliberation_revision=projection.deliberation_revision,
            ledger_sequence=projection.ledger_sequence,
        )
        for audit in projection.proposal_audits:
            if audit.proposal_kind != "decision":
                continue
            try:
                proposal = validate_proposal_envelope(json.loads(audit.proposal_json))
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise InteractionActWorkerError("source_proposal_invalid") from exc
            if not isinstance(proposal, DecisionProposal):
                continue
            changes = tuple(
                change
                for change in proposal.proposed_changes
                if change.kind == "interaction_act"
            )
            if not changes:
                continue
            if len(changes) != 1:
                raise InteractionActWorkerError("change_count_invalid")
            if self._has_accepted_descendant(
                projection=projection,
                audit=audit,
                proposal=proposal,
                change=changes[0],
                cursor=current_cursor,
            ):
                continue
            raw = changes[0].payload.value()
            source_scope = raw.get("source_scope")
            if source_scope == "delivered_expression":
                if not self._has_terminal_delivered_expression(
                    projection=projection,
                    proposal_id=proposal.proposal_id,
                ):
                    continue
            elif source_scope != "current_message":
                raise InteractionActWorkerError("source_scope_invalid")
            located = self._ledger.lookup_event_commit(audit.event_ref)
            if located is None or (
                located[0].event_type != "ProposalRecorded"
                or located[0].payload_hash != audit.event_payload_hash
            ):
                raise InteractionActWorkerError("source_proposal_event_missing")
            audit_commit = located[1]
            audit_cursor = ProjectionCursor(
                world_revision=audit_commit.world_revision,
                deliberation_revision=audit_commit.deliberation_revision,
                ledger_sequence=audit_commit.ledger_sequence,
            )
            compiled = self._compiler.record_rebased(
                world_id=self._ledger.world_id,
                audit_cursor=audit_cursor,
                current_cursor=current_cursor,
                proposal_id=proposal.proposal_id,
            )
            if (
                compiled.status != "candidate_recorded"
                or compiled.typed_proposal_id is None
                or compiled.typed_proposal_event_ref is None
                or compiled.commit is None
                or compiled.acceptance_cursor is None
            ):
                raise InteractionActWorkerError("compiled_candidate_incomplete")
            typed = self._ledger.lookup_event_commit(
                compiled.typed_proposal_event_ref
            )
            if typed is None:
                raise InteractionActWorkerError("typed_proposal_event_missing")
            typed_event = typed[0]
            handle = self._acceptance.pin_proposal(
                cursor=compiled.acceptance_cursor,
                proposal_event_ref=typed_event.event_id,
            )
            accepted = self._acceptance.accept(
                handle=handle,
                actor=self._actor,
                source=self._source,
                logical_time=typed_event.logical_time,
                created_at=typed_event.created_at,
                trace_id=typed_event.trace_id,
                correlation_id=typed_event.correlation_id,
            )
            return InteractionActWorkResult(
                source_proposal_id=proposal.proposal_id,
                typed_proposal_id=compiled.typed_proposal_id,
                compile_commit=compiled.commit,
                acceptance_commit=accepted,
            )
        return None

    def _has_accepted_descendant(
        self,
        *,
        projection,
        audit,
        proposal: DecisionProposal,
        change,
        cursor: ProjectionCursor,
    ) -> bool:
        """Reprove one accepted typed descendant without re-running the compiler."""

        expected_change_hash = canonical_interaction_act_change_hash(change)
        for decision in projection.acceptance_decisions:
            if (
                decision.status != "accepted"
                or decision.manifest_version
                != INTERACTION_ACT_ACCEPTANCE_MANIFEST_VERSION
                or decision.acceptance_event_ref is None
                or decision.acceptance_event_payload_hash is None
            ):
                continue
            acceptance_located = self._ledger.lookup_event_commit(
                decision.acceptance_event_ref
            )
            if acceptance_located is None:
                raise InteractionActWorkerError("accepted_manifest_event_missing")
            acceptance_event, acceptance_commit = acceptance_located
            if (
                acceptance_event.event_type != "AcceptanceRecorded"
                or acceptance_event.payload_hash
                != decision.acceptance_event_payload_hash
                or acceptance_event.event_id not in acceptance_commit.event_ids
                or acceptance_commit.world_revision > cursor.world_revision
                or acceptance_commit.deliberation_revision
                > cursor.deliberation_revision
                or acceptance_commit.ledger_sequence > cursor.ledger_sequence
            ):
                raise InteractionActWorkerError("accepted_manifest_event_invalid")
            try:
                manifest = InteractionActAcceptanceManifest.model_validate_json(
                    acceptance_event.payload_json,
                    strict=True,
                )
            except ValueError as exc:
                raise InteractionActWorkerError("accepted_manifest_invalid") from exc
            if (
                decision.proposal_id != manifest.proposal_id
                or decision.acceptance_id != manifest.acceptance_id
                or decision.accepted_change_id != manifest.accepted_change_id
                or decision.accepted_change_hash != manifest.accepted_change_hash
                or decision.evaluated_world_revision
                != manifest.evaluated_world_revision
                or decision.manifest_hash != manifest.manifest_hash
                or manifest.proposal_hash != proposal.proposal_hash
                or manifest.accepted_change_id != change.change_id
                or manifest.accepted_change_hash != expected_change_hash
            ):
                continue
            typed_located = self._ledger.lookup_event_commit(
                manifest.source_proposal_event_ref
            )
            effect_located = self._ledger.lookup_event_commit(manifest.effect_event_id)
            if typed_located is None or effect_located is None:
                raise InteractionActWorkerError("accepted_descendant_event_missing")
            typed_event, typed_commit = typed_located
            effect_event, effect_commit = effect_located
            try:
                typed = InteractionActProposalRecordedPayload.model_validate_json(
                    typed_event.payload_json,
                    strict=True,
                )
                effect = InteractionActAcceptedPayload.model_validate_json(
                    effect_event.payload_json,
                    strict=True,
                )
            except ValueError as exc:
                raise InteractionActWorkerError("accepted_descendant_invalid") from exc
            if (
                typed_event.event_type != "InteractionActProposalRecorded"
                or typed_event.causation_id != audit.event_ref
                or typed_event.payload_hash
                != manifest.source_proposal_event_payload_hash
                or typed_event.event_id != manifest.source_proposal_event_ref
                or typed_event.event_id not in typed_commit.event_ids
                or typed.proposal_id != manifest.proposal_id
                or typed.proposal_hash != proposal.proposal_hash
                or typed.change_id != change.change_id
                or typed.accepted_change_hash != expected_change_hash
                or effect_event.event_type != "InteractionActTransitionAccepted"
                or effect_event.causation_id != acceptance_event.event_id
                or effect_event.payload_hash != manifest.effect_payload_hash
                or effect_event.event_id not in effect_commit.event_ids
                or effect.source_proposal_event_ref != typed_event.event_id
                or effect.source_proposal_event_payload_hash != typed_event.payload_hash
                or effect.proposal_id != typed.proposal_id
                or effect.change_id != typed.change_id
                or effect.mutation_payload_hash != typed.mutation_payload_hash
                or effect.mutation != typed.mutation
                or typed_commit.world_revision > cursor.world_revision
                or typed_commit.deliberation_revision > cursor.deliberation_revision
                or typed_commit.ledger_sequence > cursor.ledger_sequence
                or effect_commit.world_revision > cursor.world_revision
                or effect_commit.deliberation_revision > cursor.deliberation_revision
                or effect_commit.ledger_sequence > cursor.ledger_sequence
            ):
                raise InteractionActWorkerError("accepted_descendant_mismatch")
            return True
        return False

    @staticmethod
    def _has_terminal_delivered_expression(*, projection, proposal_id: str) -> bool:
        """Cheap readiness filter; the compiler re-proves the exact closure."""

        plans = tuple(
            item
            for item in projection.expression_plans
            if item.proposal_id == proposal_id and item.state == "completed"
        )
        for plan in plans:
            beats = tuple(
                item
                for item in projection.expression_beats
                if item.proposal_id == proposal_id
                and item.plan_id == plan.plan_id
                and item.acceptance_id == plan.acceptance_id
                and item.state == "settled"
                and item.action_id is not None
            )
            for beat in beats:
                actions = tuple(
                    item
                    for item in projection.actions
                    if item.action_id == beat.action_id
                    and item.expression_plan_id == plan.plan_id
                    and item.expression_beat_id == beat.beat_id
                    and item.state == "delivered"
                )
                for action in actions:
                    if any(
                        receipt.action_id == action.action_id
                        and receipt.receipt_kind == "terminal"
                        and receipt.observed_state == "delivered"
                        and receipt.is_terminal
                        for receipt in projection.execution_receipts
                    ):
                        return True
        return False


__all__ = [
    "InteractionActWorker",
    "InteractionActWorkerError",
    "InteractionActWorkResult",
]

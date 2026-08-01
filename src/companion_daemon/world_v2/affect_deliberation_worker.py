"""Low-priority, source-bound Affect deliberation after Appraisal acceptance."""

from __future__ import annotations

import json
from typing import Literal

from .affect_acceptance_runtime import AffectAcceptanceRuntime
from .affect_proposal_compiler import AffectProposalCompiler
from .appraisal_acceptance_manifest import AppraisalAcceptanceManifest
from .errors import ConcurrencyConflict
from .pinned_turn import PinnedTurnCompiler
from .proposal_audit_schemas import ProposalRecordedV2Payload
from .proposal_envelope import DecisionProposal, validate_proposal_envelope
from .schema_core import FrozenModel
from .schemas import AppraisalProposalProjection, CommitResult, ProjectionCursor, WorldEvent


class AffectDeliberationWorkResult(FrozenModel):
    status: Literal["no_proposal", "no_change", "accepted"]
    trigger_event_ref: str
    source_proposal_id: str | None = None
    typed_proposal_id: str | None = None
    deliberation_commit: CommitResult | None = None
    compile_commit: CommitResult | None = None
    acceptance_commit: CommitResult | None = None


class AffectDeliberationWorker:
    """Run one fresh affect turn, then compile and accept it if it proposes one.

    The worker's small interface makes it suitable for an out-of-band queue:
    callers retain only the accepted Appraisal event and its exact cursor.  It
    intentionally does not own a platform adapter or reply action capability.
    """

    def __init__(
        self,
        *,
        ledger,
        pinned_turn: PinnedTurnCompiler,
        compiler: AffectProposalCompiler,
        acceptance: AffectAcceptanceRuntime,
        actor: str,
        source: str = "world-v2:affect-deliberation-worker",
    ) -> None:
        if not actor:
            raise ValueError("affect deliberation worker actor is required")
        if compiler.ledger is not ledger or acceptance.ledger is not ledger:
            raise ValueError("affect worker dependencies must own the same ledger")
        self._ledger = ledger
        self._pinned_turn = pinned_turn
        self._compiler = compiler
        self._acceptance = acceptance
        self._actor = actor
        self._source = source

    @property
    def ledger(self):
        """Ledger identity for composition-root validation."""

        return self._ledger

    async def process(
        self,
        *,
        world_id: str,
        cursor: ProjectionCursor,
        appraisal_event: WorldEvent,
    ) -> AffectDeliberationWorkResult:
        if world_id != self._ledger.world_id:
            raise ValueError("affect deliberation world mismatch")
        projection = self._ledger.project_at(cursor)
        source_decision = self._accepted_appraisal_source_decision(
            appraisal_event=appraisal_event
        )
        if source_decision is not None:
            # Immediate emotion already contains the role model's Affect choice.
            # A durable trigger can be opened after an inline CAS loss; recover
            # those exact audited bytes instead of asking the model to make a
            # second emotional decision for the same accepted Appraisal.
            source_proposal_id, audit_cursor = source_decision
            compiled = self._compiler.record_rebased(
                world_id=world_id,
                audit_cursor=audit_cursor,
                current_cursor=cursor,
                proposal_id=source_proposal_id,
            )
            if compiled.status == "no_change":
                return AffectDeliberationWorkResult(
                    status="no_change",
                    trigger_event_ref=appraisal_event.event_id,
                    source_proposal_id=compiled.source_proposal_id,
                )
            if (
                compiled.commit is None
                or compiled.acceptance_cursor is None
                or compiled.typed_proposal_id is None
            ):
                raise RuntimeError(
                    "source-bound affect compiler returned an incomplete candidate"
                )
            accepted = self._accept_pending(
                cursor=compiled.acceptance_cursor,
                proposal_id=compiled.typed_proposal_id,
            )
            return AffectDeliberationWorkResult(
                status="accepted",
                trigger_event_ref=appraisal_event.event_id,
                source_proposal_id=compiled.source_proposal_id,
                typed_proposal_id=compiled.typed_proposal_id,
                compile_commit=compiled.commit,
                acceptance_commit=accepted,
            )
        reusable = next(
            (
                audit
                for audit in projection.proposal_audits
                if audit.proposal_kind == "decision"
                and audit.trigger_ref == appraisal_event.event_id
                and audit.evaluated_world_revision == cursor.world_revision
            ),
            None,
        )
        audited = None
        source_audit_event_ref = None
        if reusable is None:
            audited = await self._pinned_turn.audit_appraisal_accepted(
                appraisal_event=appraisal_event,
                cursor=cursor,
            )
            if audited.proposal_id is None:
                return AffectDeliberationWorkResult(
                    status="no_proposal",
                    trigger_event_ref=appraisal_event.event_id,
                    deliberation_commit=audited.result,
                )
            source_proposal_id = audited.proposal_id
            compiled_cursor = audited.cursor
        else:
            # A prior attempt may have persisted the expensive generic audit and
            # crashed before compiling/accepting it.  Reuse that exact audit;
            # recovery must never turn an already-paid model call into another
            # model call merely because the process restarted.
            source_proposal_id = reusable.proposal_id
            source_audit_event_ref = reusable.event_ref
            compiled_cursor = cursor
        pending = next(
            (
                proposal
                for proposal in projection.affect_proposals
                if proposal.source_audit is not None
                and proposal.source_audit.proposal_event_ref == source_audit_event_ref
            ),
            None,
        )
        if pending is not None:
            accepted = self._accept_pending(cursor=cursor, proposal_id=pending.proposal_id)
            return AffectDeliberationWorkResult(
                status="accepted",
                trigger_event_ref=appraisal_event.event_id,
                source_proposal_id=source_proposal_id,
                typed_proposal_id=pending.proposal_id,
                deliberation_commit=audited.result if audited is not None else None,
                acceptance_commit=accepted,
            )
        try:
            compiled = self._compiler.record(
                world_id=world_id,
                cursor=compiled_cursor,
                proposal_id=source_proposal_id,
            )
        except ConcurrencyConflict:
            raise
        if compiled.status == "no_change":
            return AffectDeliberationWorkResult(
                status="no_change",
                trigger_event_ref=appraisal_event.event_id,
                source_proposal_id=compiled.source_proposal_id,
                deliberation_commit=audited.result if audited is not None else None,
            )
        if (
            compiled.commit is None
            or compiled.acceptance_cursor is None
            or compiled.typed_proposal_id is None
        ):
            raise RuntimeError("affect compiler returned an incomplete candidate result")
        accepted = self._accept_pending(
            cursor=compiled.acceptance_cursor,
            proposal_id=compiled.typed_proposal_id,
        )
        return AffectDeliberationWorkResult(
            status="accepted",
            trigger_event_ref=appraisal_event.event_id,
            source_proposal_id=compiled.source_proposal_id,
            typed_proposal_id=compiled.typed_proposal_id,
            deliberation_commit=audited.result if audited is not None else None,
            compile_commit=compiled.commit,
            acceptance_commit=accepted,
        )

    def _accept_pending(self, *, cursor: ProjectionCursor, proposal_id: str) -> CommitResult:
        return self._acceptance.accept_runtime_owned(
            handle=self._acceptance.pin_proposal(cursor=cursor, proposal_id=proposal_id),
            actor=self._actor,
            source=self._source,
        )

    def _accepted_appraisal_source_decision(
        self, *, appraisal_event: WorldEvent
    ) -> tuple[str, ProjectionCursor] | None:
        """Resolve an original combined DecisionProposal through immutable lineage.

        Fresh, post-Appraisal Affect turns have no Affect choice in their source
        proposal and therefore return ``None``.  Combined immediate-emotion
        proposals return their exact proposal id and audit cursor.
        """

        if appraisal_event.event_type != "AppraisalAccepted":
            raise ValueError("affect source must be an accepted Appraisal")
        acceptance = self._ledger.lookup_event_commit(appraisal_event.causation_id)
        if acceptance is None or acceptance[0].event_type != "AcceptanceRecorded":
            raise ValueError("accepted Appraisal manifest is unavailable")
        manifest = AppraisalAcceptanceManifest.model_validate_json(
            acceptance[0].payload_json
        )
        if (
            manifest.mutation_event_id != appraisal_event.event_id
            or manifest.mutation_event_type != "AppraisalAccepted"
            or manifest.mutation_payload_hash != appraisal_event.payload_hash
        ):
            raise ValueError("accepted Appraisal does not match its manifest")
        typed = self._ledger.lookup_event_commit(manifest.proposal_event_ref)
        if typed is None or typed[0].event_type != "ProposalRecorded":
            raise ValueError("accepted Appraisal proposal is unavailable")
        typed_proposal = AppraisalProposalProjection.model_validate_json(
            typed[0].payload_json
        )
        if (
            typed_proposal.proposal_id != manifest.proposal_id
            or typed[0].payload_hash != manifest.proposal_event_payload_hash
        ):
            raise ValueError("accepted Appraisal proposal does not match its manifest")
        source_audit = self._ledger.lookup_event_commit(typed[0].causation_id)
        if source_audit is None or source_audit[0].event_type != "ProposalRecorded":
            raise ValueError("accepted Appraisal source audit is unavailable")
        recorded = ProposalRecordedV2Payload.model_validate_json(
            source_audit[0].payload_json
        )
        proposal = validate_proposal_envelope(json.loads(recorded.proposal_json))
        if not isinstance(proposal, DecisionProposal):
            raise ValueError("accepted Appraisal source is not a decision")
        appraisal_changes = tuple(
            change
            for change in proposal.proposed_changes
            if change.kind == "appraisal_transition"
        )
        if (
            len(appraisal_changes) != 1
            or appraisal_changes[0].change_id != typed_proposal.change_id
        ):
            raise ValueError("accepted Appraisal source decision changed identity")
        if proposal.affect_decision != "propose":
            return None
        return recorded.proposal_id, self._cursor_from_commit(source_audit[1])

    @staticmethod
    def _cursor_from_commit(commit: CommitResult) -> ProjectionCursor:
        return ProjectionCursor(
            world_revision=commit.world_revision,
            deliberation_revision=commit.deliberation_revision,
            ledger_sequence=commit.ledger_sequence,
        )


__all__ = ["AffectDeliberationWorker", "AffectDeliberationWorkResult"]

"""Effect-once settlement for a CharacterInterior-authored inbound relationship signal.

The Character Model may include one optional ``relationship_signal`` in the
same audited inbound decision that owns expression and immediate inner state.
This module contains no model port and performs no relationship inference.  It
only opens the source-bound durable trigger, compiles the already-authored
change, accepts its typed signal, and completes that exact trigger.
"""

from __future__ import annotations

import json
from typing import Literal

from ..decision_proposal_authority import DecisionProposalAuthorityReader
from ..errors import ConcurrencyConflict
from ..ledger import LedgerPort
from ..proposal_envelope import DecisionProposal, validate_proposal_envelope
from ..relationship_acceptance_runtime import RelationshipAcceptanceRuntime
from ..relationship_proposal_compiler import RelationshipProposalCompiler
from ..relationship_trigger import (
    relationship_continuity_trigger_id,
    relationship_continuity_trigger_open_event,
)
from ..relationship_trigger_runtime import RelationshipTriggerRuntime
from ..schema_core import FrozenModel
from ..schemas import CommitResult, ProjectionCursor, TriggerProcess, WorldEvent


class InboundRelationshipSignalError(ValueError):
    """Stable technical/authority failure at the unified settlement seam."""

    def __init__(self, code: str) -> None:
        self.code = f"character_interior.inbound_relationship.{code}"
        super().__init__(self.code)


class InboundRelationshipSignalWorkResult(FrozenModel):
    status: Literal["no_change", "accepted", "owned_elsewhere"]
    source_proposal_id: str
    trigger_id: str | None = None
    typed_proposal_id: str | None = None
    trigger_open_commit: CommitResult | None = None
    compile_commit: CommitResult | None = None
    acceptance_commit: CommitResult | None = None
    replayed: bool = False


class _AuthoredSignalWorkResult(FrozenModel):
    status: Literal["accepted"] = "accepted"
    typed_proposal_id: str
    compile_commit: CommitResult
    acceptance_commit: CommitResult


class _AuthoredSignalCompilerWorker:
    """Trigger worker for bytes already persisted by CharacterInterior."""

    def __init__(
        self,
        *,
        ledger: LedgerPort,
        compiler: RelationshipProposalCompiler,
        acceptance: RelationshipAcceptanceRuntime,
        audit_cursor: ProjectionCursor,
        proposal_id: str,
        source_event: WorldEvent,
        actor: str,
        source: str,
    ) -> None:
        self._ledger = ledger
        self._compiler = compiler
        self._acceptance = acceptance
        self._audit_cursor = audit_cursor
        self._proposal_id = proposal_id
        self._source_event = source_event
        self._actor = actor
        self._source = source
        self.result: _AuthoredSignalWorkResult | None = None

    async def process(
        self,
        *,
        world_id: str,
        cursor: ProjectionCursor,
        source_event: WorldEvent,
    ) -> _AuthoredSignalWorkResult:
        if (
            world_id != self._ledger.world_id
            or source_event.event_id != self._source_event.event_id
            or source_event.payload_hash != self._source_event.payload_hash
        ):
            raise InboundRelationshipSignalError("trigger_source_mismatch")
        compiled = self._compiler.record_rebased(
            world_id=world_id,
            audit_cursor=self._audit_cursor,
            current_cursor=cursor,
            proposal_id=self._proposal_id,
        )
        if compiled.status != "candidate_recorded":
            # The outer worker authenticated an authored signal before opening
            # the trigger.  Reclassifying its disappearance as no_change would
            # impersonate the character and terminally consume the work.
            raise InboundRelationshipSignalError("authored_signal_disappeared")
        if (
            compiled.typed_proposal_id is None
            or compiled.commit is None
            or compiled.acceptance_cursor is None
        ):
            raise InboundRelationshipSignalError("compiled_candidate_incomplete")
        accepted = self._acceptance.accept_runtime_owned(
            handle=self._acceptance.pin_proposal(
                cursor=compiled.acceptance_cursor,
                proposal_id=compiled.typed_proposal_id,
            ),
            actor=self._actor,
            source=self._source,
        )
        self.result = _AuthoredSignalWorkResult(
            typed_proposal_id=compiled.typed_proposal_id,
            compile_commit=compiled.commit,
            acceptance_commit=accepted,
        )
        return self.result


class InboundRelationshipSignalWorker:
    """Settle one optional same-call relationship signal without another role call."""

    def __init__(
        self,
        *,
        ledger: LedgerPort,
        compiler: RelationshipProposalCompiler,
        acceptance: RelationshipAcceptanceRuntime,
        owner_id: str,
        lease_seconds: int = 120,
        source: str = "world-v2:character-interior-inbound-relationship",
    ) -> None:
        if not owner_id or lease_seconds <= 0:
            raise ValueError("inbound relationship worker needs owner and positive lease")
        if compiler.ledger is not ledger or acceptance.ledger is not ledger:
            raise ValueError(
                "inbound relationship dependencies must own the same ledger"
            )
        self._ledger = ledger
        self._compiler = compiler
        self._acceptance = acceptance
        self._reader = DecisionProposalAuthorityReader(ledger=ledger)
        self._owner_id = owner_id
        self._lease_seconds = lease_seconds
        self._source = source

    @property
    def ledger(self) -> LedgerPort:
        return self._ledger

    async def drain_one(self) -> InboundRelationshipSignalWorkResult | None:
        """Recover one pending unified inbound signal from ledger authority.

        The durable relationship trigger contains the source Observation while
        the proposal audit contains the exact CharacterInterior bytes.  Joining
        those two immutable identities is sufficient to resume after restart;
        no caller-side cache and no historical relationship model is needed.
        Appraisal-sourced historical triggers are deliberately ignored because
        they were authored by the retired independent relationship lane.
        """

        projection = self._ledger.project()
        process = next(
            (
                item
                for item in projection.trigger_processes
                if item.process_kind == "relationship_deliberation"
                and item.state != "terminal"
                and item.source_evidence_ref is not None
                and (
                    located := self._ledger.lookup_event_commit(
                        item.source_evidence_ref
                    )
                )
                is not None
                and located[0].event_type == "ObservationRecorded"
            ),
            None,
        )
        if process is None or process.source_evidence_ref is None:
            return None
        source_located = self._ledger.lookup_event_commit(process.source_evidence_ref)
        if source_located is None:
            raise InboundRelationshipSignalError("trigger_source_missing")
        source_event = source_located[0]
        candidates = []
        for audit in projection.proposal_audits:
            if audit.trigger_ref != source_event.event_id or audit.proposal_kind != "decision":
                continue
            proposal = validate_proposal_envelope(json.loads(audit.proposal_json))
            if not isinstance(proposal, DecisionProposal):
                continue
            if any(change.kind == "relationship_signal" for change in proposal.proposed_changes):
                candidates.append((audit, proposal))
        if len(candidates) != 1:
            raise InboundRelationshipSignalError(
                "source_proposal_missing"
                if not candidates
                else "source_proposal_ambiguous"
            )
        audit, proposal = candidates[0]
        audit_located = self._ledger.lookup_event_commit(audit.event_ref)
        if audit_located is None:
            raise InboundRelationshipSignalError("proposal_audit_event_missing")
        audit_commit = audit_located[1]
        head = self._ledger.project()
        return await self.process(
            world_id=self._ledger.world_id,
            audit_cursor=ProjectionCursor(
                world_revision=audit_commit.world_revision,
                deliberation_revision=audit_commit.deliberation_revision,
                ledger_sequence=audit_commit.ledger_sequence,
            ),
            current_cursor=ProjectionCursor(
                world_revision=head.world_revision,
                deliberation_revision=head.deliberation_revision,
                ledger_sequence=head.ledger_sequence,
            ),
            proposal_id=proposal.proposal_id,
            source_event=source_event,
        )

    async def process(
        self,
        *,
        world_id: str,
        audit_cursor: ProjectionCursor,
        current_cursor: ProjectionCursor,
        proposal_id: str,
        source_event: WorldEvent,
    ) -> InboundRelationshipSignalWorkResult:
        if world_id != self._ledger.world_id or source_event.world_id != world_id:
            raise InboundRelationshipSignalError("world_mismatch")
        authority = self._reader.read(
            self._reader.pin(
                world_id=world_id,
                cursor=audit_cursor,
                proposal_id=proposal_id,
            )
        )
        changes = tuple(
            change
            for change in authority.proposal.proposed_changes
            if change.kind == "relationship_signal"
        )
        if not changes:
            return InboundRelationshipSignalWorkResult(
                status="no_change",
                source_proposal_id=proposal_id,
            )
        if len(changes) != 1 or changes[0].transition != "suggest":
            raise InboundRelationshipSignalError("signal_change_invalid")
        if (
            source_event.event_type != "ObservationRecorded"
            or authority.proposal.trigger_ref != source_event.event_id
            or authority.audit.trigger_ref != source_event.event_id
        ):
            raise InboundRelationshipSignalError("source_observation_mismatch")

        trigger_id = relationship_continuity_trigger_id(
            world_id=world_id,
            observation_event_id=source_event.event_id,
        )
        projection = self._ledger.project_at(current_cursor)
        process = self._find_process(projection, trigger_id=trigger_id)
        if process is not None and process.state == "terminal":
            self._require_accepted_terminal(process)
            return InboundRelationshipSignalWorkResult(
                status="accepted",
                source_proposal_id=proposal_id,
                trigger_id=trigger_id,
                replayed=True,
            )

        open_commit = None
        if process is None:
            opened = relationship_continuity_trigger_open_event(
                observation_event=source_event,
                owner_id=self._owner_id,
            )
            try:
                open_commit = self._ledger.commit_at_cursor(
                    [opened],
                    expected_cursor=current_cursor,
                    commit_id=f"commit:character-interior:relationship-open:{trigger_id}",
                )
            except ConcurrencyConflict:
                # A concurrent invocation may have won the exact same durable
                # opening.  Joining that source-bound effect is idempotency,
                # not suppression of an unrelated cursor failure.
                concurrent = self._ledger.project()
                process = self._find_process(concurrent, trigger_id=trigger_id)
                if process is None:
                    raise
                if process.state == "terminal":
                    self._require_accepted_terminal(process)
                    return InboundRelationshipSignalWorkResult(
                        status="accepted",
                        source_proposal_id=proposal_id,
                        trigger_id=trigger_id,
                        replayed=True,
                    )

        worker = _AuthoredSignalCompilerWorker(
            ledger=self._ledger,
            compiler=self._compiler,
            acceptance=self._acceptance,
            audit_cursor=audit_cursor,
            proposal_id=proposal_id,
            source_event=source_event,
            actor=self._owner_id,
            source=self._source,
        )
        run = await RelationshipTriggerRuntime(
            ledger=self._ledger,
            worker=worker,
            owner_id=self._owner_id,
            lease_seconds=self._lease_seconds,
            source=self._source,
        ).drain_trigger(trigger_id)
        if run.status == "owned_elsewhere":
            return InboundRelationshipSignalWorkResult(
                status="owned_elsewhere",
                source_proposal_id=proposal_id,
                trigger_id=trigger_id,
                trigger_open_commit=open_commit,
            )
        if run.status == "idle":
            process = self._find_process(self._ledger.project(), trigger_id=trigger_id)
            if process is None or process.state != "terminal":
                raise InboundRelationshipSignalError("trigger_disappeared")
            self._require_accepted_terminal(process)
            return InboundRelationshipSignalWorkResult(
                status="accepted",
                source_proposal_id=proposal_id,
                trigger_id=trigger_id,
                trigger_open_commit=open_commit,
                replayed=True,
            )
        if run.work_status != "accepted" or worker.result is None:
            raise InboundRelationshipSignalError("settlement_incomplete")
        return InboundRelationshipSignalWorkResult(
            status="accepted",
            source_proposal_id=proposal_id,
            trigger_id=trigger_id,
            typed_proposal_id=worker.result.typed_proposal_id,
            trigger_open_commit=open_commit,
            compile_commit=worker.result.compile_commit,
            acceptance_commit=worker.result.acceptance_commit,
        )

    @staticmethod
    def _find_process(projection, *, trigger_id: str) -> TriggerProcess | None:
        matches = tuple(
            item
            for item in projection.trigger_processes
            if item.trigger_id == trigger_id
        )
        if len(matches) > 1:
            raise InboundRelationshipSignalError("trigger_identity_ambiguous")
        process = matches[0] if matches else None
        if process is not None and (
            process.process_kind != "relationship_deliberation"
            or process.trigger_ref
            != f"relationship-continuity:{process.source_evidence_ref}"
        ):
            raise InboundRelationshipSignalError("trigger_binding_invalid")
        return process

    @staticmethod
    def _require_accepted_terminal(process: TriggerProcess) -> None:
        if (
            process.runtime_outcome_ref is None
            or not process.runtime_outcome_ref.endswith(":accepted")
        ):
            raise InboundRelationshipSignalError("terminal_outcome_not_accepted")


__all__ = [
    "InboundRelationshipSignalError",
    "InboundRelationshipSignalWorkResult",
    "InboundRelationshipSignalWorker",
]

"""Fail-closed bridge from an audited DecisionProposal to Outcome authority.

This module deliberately accepts no host supplied occurrence, candidate or
evidence objects.  It is the narrow seam that future Outcome workers use after
claiming an ``outcome_deliberation`` trigger.
"""

from __future__ import annotations

from .decision_proposal_authority import DecisionProposalAuthorityReader
from .ledger import LedgerPort
from .schema_core import FrozenModel
from .schemas import ProjectionCursor


class OutcomeProposalCompilerError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = f"outcome_proposal_compiler.{code}"
        super().__init__(self.code)


class OutcomeProposalAuthority(FrozenModel):
    """The verified, non-mutable input that an Outcome compiler may inspect."""

    cursor: ProjectionCursor
    source_proposal_id: str
    source_proposal_event_ref: str
    occurrence_id: str
    occurrence_entity_revision: int
    deliberation_trigger_id: str
    source_observation_id: str


class OutcomeProposalCompiler:
    """Verify the generic proposal's worker/source boundary before compilation.

    The concrete typed-event materialization is intentionally kept behind this
    check.  Until candidate payload semantics and acceptance manifest are both
    installed, callers receive a verified authority object rather than a
    partially-authorized settlement event.
    """

    def __init__(self, *, ledger: LedgerPort) -> None:
        self._ledger = ledger
        self._reader = DecisionProposalAuthorityReader(ledger=ledger)

    def verify_input(
        self, *, world_id: str, cursor: ProjectionCursor, proposal_id: str
    ) -> OutcomeProposalAuthority:
        authority = self._reader.read(
            self._reader.pin(world_id=world_id, cursor=cursor, proposal_id=proposal_id)
        )
        changes = tuple(
            item for item in authority.proposal.proposed_changes if item.kind == "outcome_settlement"
        )
        if len(changes) != 1 or len(authority.proposal.proposed_changes) != 1:
            raise OutcomeProposalCompilerError("outcome_change_count_invalid")
        change = changes[0]
        if change.transition != "settle":
            raise OutcomeProposalCompilerError("transition_invalid")
        projection = self._ledger.project_at(cursor)
        raw = change.payload.value()
        occurrence_id = str(raw["entity_id"])
        occurrence = next(
            (item for item in projection.world_occurrences if item.occurrence_id == occurrence_id),
            None,
        )
        if occurrence is None or occurrence.status != "active" or (
            change.target_id != occurrence_id
            or change.expected_entity_revision != occurrence.entity_revision
            or int(raw["entity_revision"]) != occurrence.entity_revision
        ):
            raise OutcomeProposalCompilerError("occurrence_not_current")
        candidate = next(
            (
                item
                for item in occurrence.candidate_outcomes
                if item.candidate_result_ref == raw["candidate_result_ref"]
            ),
            None,
        )
        if candidate is None or candidate.content_ref is None or candidate.content_payload_hash is None:
            raise OutcomeProposalCompilerError("candidate_content_unavailable")
        binding = raw["result_payload"]
        if (
            raw["result_id"] != candidate.result_id
            or binding["object_ref"] != candidate.result_payload_ref
            or binding["payload_hash"].removeprefix("sha256:")
            != candidate.result_payload_hash.removeprefix("sha256:")
        ):
            raise OutcomeProposalCompilerError("candidate_result_mismatch")
        observation_ids = tuple(sorted(item["ref_id"] for item in raw["observations"]))
        if observation_ids != tuple(sorted(occurrence.observation_refs)):
            raise OutcomeProposalCompilerError("observation_set_mismatch")
        source = authority.audit.trigger_ref
        expected_event = "event:outcome-observation:"
        if not source.startswith(expected_event):
            raise OutcomeProposalCompilerError("trigger_source_invalid")
        source_observation_id = source.removeprefix(expected_event)
        trigger = next(
            (
                item
                for item in projection.trigger_processes
                if item.process_kind == "outcome_deliberation"
                and item.state == "claimed"
                and item.source_evidence_ref == source
            ),
            None,
        )
        if trigger is None or source_observation_id not in occurrence.observation_refs:
            raise OutcomeProposalCompilerError("source_trigger_not_claimed")
        return OutcomeProposalAuthority(
            cursor=cursor,
            source_proposal_id=authority.proposal.proposal_id,
            source_proposal_event_ref=authority.audit.event_ref,
            occurrence_id=occurrence_id,
            occurrence_entity_revision=occurrence.entity_revision,
            deliberation_trigger_id=trigger.trigger_id,
            source_observation_id=source_observation_id,
        )


__all__ = ["OutcomeProposalAuthority", "OutcomeProposalCompiler", "OutcomeProposalCompilerError"]

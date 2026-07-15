"""Fail-closed bridge from an audited DecisionProposal to Outcome authority.

This module deliberately accepts no host supplied occurrence, candidate or
evidence objects.  It is the narrow seam that future Outcome workers use after
claiming an ``outcome_deliberation`` trigger.
"""

from __future__ import annotations

from datetime import timedelta
import hashlib
import json
from typing import Literal

from .decision_proposal_authority import DecisionProposalAuthorityReader
from .event_identity import domain_idempotency_key
from .ledger import LedgerPort
from .life_events import outcome_mutation_hash
from .outcome_candidate_reader import OutcomeCandidateReader
from .schema_core import EvidenceRef, FrozenModel
from .schemas import CommitResult, ProjectionCursor, WorldEvent


_CONTRACT = "outcome-proposal-compiler.1"
_POLICY_REFS = ("policy:outcome-v1",)


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


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


class OutcomeProposalCompilation(FrozenModel):
    status: Literal["candidate_recorded"]
    source_proposal_id: str
    source_proposal_event_ref: str
    typed_proposal_id: str
    commit: CommitResult


class OutcomeProposalCompiler:
    """Verify the generic proposal's worker/source boundary before compilation.

    ``record`` is the only write interface.  It derives a dedicated, source-
    bound `OutcomeProposalRecorded` event from a generic deliberation audit;
    it never accepts host supplied occurrence, candidate, evidence, or event
    identity objects.  Acceptance remains a separate capability.
    """

    def __init__(self, *, ledger: LedgerPort, candidate_reader: OutcomeCandidateReader) -> None:
        self._ledger = ledger
        self._reader = DecisionProposalAuthorityReader(ledger=ledger)
        self._candidate_reader = candidate_reader

    @property
    def ledger(self) -> LedgerPort:
        return self._ledger

    def record(
        self, *, world_id: str, cursor: ProjectionCursor, proposal_id: str
    ) -> OutcomeProposalCompilation:
        generic = self._reader.read(
            self._reader.pin(world_id=world_id, cursor=cursor, proposal_id=proposal_id)
        )
        verified = self._verify(authority=generic, cursor=cursor)
        projection = self._ledger.project_at(cursor)
        change = generic.proposal.proposed_changes[0]
        raw = change.payload.value()
        identity = _digest(
            {
                "contract": _CONTRACT,
                "source_proposal_event": generic.audit.event_ref,
                "source_change": change.change_id,
            }
        )
        typed_proposal_id = f"proposal:outcome-compiled:{identity}"
        source_event = self._event(generic.audit.event_ref)
        payload = {
            "outcome_proposal_id": typed_proposal_id,
            "decision_proposal_id": generic.proposal.proposal_id,
            "change_id": change.change_id,
            "occurrence_id": verified.occurrence_id,
            "evaluated_entity_revision": verified.occurrence_entity_revision,
            "evaluated_world_revision": cursor.world_revision,
            "trigger_ref": self._occurrence(projection, verified.occurrence_id).trigger_ref,
            "deliberation_trigger_id": verified.deliberation_trigger_id,
            "source_observation_id": verified.source_observation_id,
            "candidate_result_ref": str(raw["candidate_result_ref"]),
            "proposed_result_id": str(raw["result_id"]),
            "proposed_result_payload_ref": str(raw["result_payload"]["object_ref"]),
            "proposed_result_payload_hash": str(raw["result_payload"]["payload_hash"]),
            "proposed_change_hash": outcome_mutation_hash(
                change_id=change.change_id,
                occurrence_id=verified.occurrence_id,
                evaluated_entity_revision=verified.occurrence_entity_revision,
                evaluated_world_revision=cursor.world_revision,
                candidate_result_ref=str(raw["candidate_result_ref"]),
                result_id=str(raw["result_id"]),
                result_payload_ref=str(raw["result_payload"]["object_ref"]),
                result_payload_hash=str(raw["result_payload"]["payload_hash"]),
                observation_refs=tuple(item["ref_id"] for item in raw["observations"]),
            ),
            "observation_refs": sorted(item["ref_id"] for item in raw["observations"]),
            "precondition_refs": list(
                self._occurrence(projection, verified.occurrence_id).satisfied_precondition_refs
            ),
            "evidence_refs": [
                item.model_dump(mode="json")
                for item in self._evidence(generic.proposal, change.evidence_refs)
            ],
            "confidence_bp": generic.proposal.confidence,
            "expires_at": (projection.logical_time + timedelta(minutes=5)).isoformat(),
        }
        idempotency_key = domain_idempotency_key(
            event_type="OutcomeProposalRecorded", world_id=self._ledger.world_id, payload=payload
        )
        if idempotency_key is None:
            raise OutcomeProposalCompilerError("proposal_identity_missing")
        event = WorldEvent.from_payload(
            schema_version="world-v2.1",
            event_id=f"event:outcome-proposal-compiled:{identity}",
            world_id=self._ledger.world_id,
            event_type="OutcomeProposalRecorded",
            logical_time=projection.logical_time,
            created_at=source_event.created_at,
            actor="worker:outcome-proposal-compiler",
            source=_CONTRACT,
            trace_id=source_event.trace_id,
            causation_id=generic.audit.event_ref,
            correlation_id=source_event.correlation_id,
            idempotency_key=idempotency_key,
            payload=payload,
        )
        commit = self._ledger.commit(
            [event],
            expected_world_revision=cursor.world_revision,
            expected_deliberation_revision=cursor.deliberation_revision,
            commit_id="commit:outcome-proposal-compiler:"
            + _digest({"cursor": cursor.model_dump(mode="json"), "proposal": typed_proposal_id}),
        )
        return OutcomeProposalCompilation(
            status="candidate_recorded",
            source_proposal_id=generic.proposal.proposal_id,
            source_proposal_event_ref=generic.audit.event_ref,
            typed_proposal_id=typed_proposal_id,
            commit=commit,
        )

    def verify_input(
        self, *, world_id: str, cursor: ProjectionCursor, proposal_id: str
    ) -> OutcomeProposalAuthority:
        authority = self._reader.read(
            self._reader.pin(world_id=world_id, cursor=cursor, proposal_id=proposal_id)
        )
        return self._verify(authority=authority, cursor=cursor)

    def _verify(self, *, authority, cursor: ProjectionCursor) -> OutcomeProposalAuthority:
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
        if candidate is None:
            raise OutcomeProposalCompilerError("candidate_content_unavailable")
        readable = self._candidate_reader.read(
            occurrence=occurrence, viewer_privacy_ceiling="private"
        )
        if not any(item.candidate_result_ref == candidate.candidate_result_ref for item in readable.candidates):
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
        self._validate_observation_bindings(raw["observations"])
        self._validate_source_evidence(
            proposal=authority.proposal,
            refs=change.evidence_refs,
            source_event_ref=source,
        )
        return OutcomeProposalAuthority(
            cursor=cursor,
            source_proposal_id=authority.proposal.proposal_id,
            source_proposal_event_ref=authority.audit.event_ref,
            occurrence_id=occurrence_id,
            occurrence_entity_revision=occurrence.entity_revision,
            deliberation_trigger_id=trigger.trigger_id,
            source_observation_id=source_observation_id,
        )

    def _validate_observation_bindings(self, observations: object) -> None:
        if not isinstance(observations, list) or not observations:
            raise OutcomeProposalCompilerError("observation_bindings_invalid")
        for binding in observations:
            if not isinstance(binding, dict) or not isinstance(binding.get("ref_id"), str):
                raise OutcomeProposalCompilerError("observation_bindings_invalid")
            event_id = f"event:outcome-observation:{binding['ref_id']}"
            located = self._ledger.lookup_event_commit(event_id)
            if located is None or located[0].event_type != "OutcomeObservationRecorded":
                raise OutcomeProposalCompilerError("observation_source_missing")
            event, commit = located
            if (
                binding.get("source_world_revision") != commit.world_revision
                or str(binding.get("immutable_hash", "")).removeprefix("sha256:")
                != event.payload_hash.removeprefix("sha256:")
            ):
                raise OutcomeProposalCompilerError("observation_binding_mismatch")

    def _validate_source_evidence(self, *, proposal, refs, source_event_ref: str) -> None:
        if not refs or len(set(refs)) != len(refs):
            raise OutcomeProposalCompilerError("evidence_not_authoritative")
        source = next((item for item in proposal.evidence_refs if item.ref_id == source_event_ref), None)
        located = self._ledger.lookup_event_commit(source_event_ref)
        if source is None or located is None:
            raise OutcomeProposalCompilerError("source_evidence_missing")
        event, commit = located
        if (
            source_event_ref not in refs
            or source.evidence_kind != "committed_world_event"
            or source.source_world_revision != commit.world_revision
            or source.immutable_hash.removeprefix("sha256:")
            != event.payload_hash.removeprefix("sha256:")
        ):
            raise OutcomeProposalCompilerError("source_evidence_invalid")

    def _evidence(self, proposal, refs) -> tuple[EvidenceRef, ...]:
        by_id = {item.ref_id: item for item in proposal.evidence_refs}
        if not refs or len(set(refs)) != len(refs) or any(ref not in by_id for ref in refs):
            raise OutcomeProposalCompilerError("evidence_not_authoritative")
        result: list[EvidenceRef] = []
        for ref in refs:
            source = by_id[ref]
            if source.evidence_kind != "committed_world_event":
                raise OutcomeProposalCompilerError("evidence_kind_invalid")
            result.append(
                EvidenceRef(
                    ref_id=source.ref_id,
                    evidence_type="committed_world_event",
                    claim_purpose="current_fact",
                    source_world_revision=source.source_world_revision,
                    immutable_hash=source.immutable_hash.removeprefix("sha256:"),
                )
            )
        return tuple(result)

    @staticmethod
    def _occurrence(projection, occurrence_id: str):
        occurrence = next(
            (item for item in projection.world_occurrences if item.occurrence_id == occurrence_id),
            None,
        )
        if occurrence is None:
            raise OutcomeProposalCompilerError("occurrence_not_current")
        return occurrence

    def _event(self, event_id: str) -> WorldEvent:
        located = self._ledger.lookup_event_commit(event_id)
        if located is None:
            raise OutcomeProposalCompilerError("source_event_missing")
        return located[0]


__all__ = [
    "OutcomeProposalAuthority",
    "OutcomeProposalCompilation",
    "OutcomeProposalCompiler",
    "OutcomeProposalCompilerError",
]

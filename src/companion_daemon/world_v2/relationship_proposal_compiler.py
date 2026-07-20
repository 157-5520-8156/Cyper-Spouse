"""Compile one audited relationship-signal suggestion into typed authority.

The model never writes relationship state.  It suggests one bounded signal in
a generic, replayable decision audit; this compiler re-proves the accepted
appraisal and claimed relationship trigger, then derives every authority id
and the only possible ``RelationshipSignalAccepted`` candidate.
"""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from .decision_proposal_authority import DecisionProposalAuthorityReader
from .event_identity import domain_idempotency_key
from .ledger import LedgerPort
from .relationship_events import relationship_mutation_hash
from .relationship_trigger import relationship_deliberation_trigger_id
from .schema_core import EvidenceRef, FrozenModel
from .schemas import (
    AppraisalProjection,
    CommitResult,
    ProjectionCursor,
    RelationshipProposalAuditBinding,
    RelationshipProposalProjection,
    RelationshipProposedMutation,
    RelationshipSignalOrigin,
    RelationshipSignalProjection,
    RelationshipVariableDeltas,
    WorldEvent,
    relationship_signal_fingerprint,
)


_CONTRACT = "relationship-proposal-compiler.1"
_POLICY_REFS = ("policy:relationship-signal-v1",)


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def relationship_mutation_event_id(
    *, world_id: str, proposal_id: str, transition_id: str, event_type: str
) -> str:
    return "event:relationship-mutation:" + _digest(
        {
            "world_id": world_id,
            "proposal_id": proposal_id,
            "transition_id": transition_id,
            "event_type": event_type,
        }
    )


class RelationshipProposalCompilerError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = f"relationship_proposal_compiler.{code}"
        super().__init__(self.code)


class RelationshipProposalCompilation(FrozenModel):
    status: Literal["no_change", "candidate_recorded"]
    source_proposal_id: str
    source_proposal_event_ref: str
    typed_proposal_id: str | None = None
    commit: CommitResult | None = None


class RelationshipProposalCompiler:
    """Narrow source-bound compiler for the signal-before-adjustment stage."""

    def __init__(self, *, ledger: LedgerPort) -> None:
        self._ledger = ledger
        self._reader = DecisionProposalAuthorityReader(ledger=ledger)

    @property
    def ledger(self) -> LedgerPort:
        return self._ledger

    def record(
        self, *, world_id: str, cursor: ProjectionCursor, proposal_id: str
    ) -> RelationshipProposalCompilation:
        authority = self._reader.read(
            self._reader.pin(world_id=world_id, cursor=cursor, proposal_id=proposal_id)
        )
        change = tuple(
            item
            for item in authority.proposal.proposed_changes
            if item.kind == "relationship_signal"
        )
        if not change:
            return RelationshipProposalCompilation(
                status="no_change",
                source_proposal_id=authority.proposal.proposal_id,
                source_proposal_event_ref=authority.audit.event_ref,
            )
        if len(change) != 1 or change[0].transition != "suggest":
            raise RelationshipProposalCompilerError("signal_change_invalid")
        projection = self._ledger.project_at(cursor)
        typed = self._compile_signal(authority=authority, change=change[0], projection=projection)
        source_event = self._event(authority.audit.event_ref)
        event = self._proposal_event(typed=typed, source_event=source_event, logical_time=projection.logical_time)
        commit = self._ledger.commit(
            [event],
            expected_world_revision=cursor.world_revision,
            expected_deliberation_revision=cursor.deliberation_revision,
            commit_id="commit:relationship-proposal-compiler:"
            + _digest(
                {
                    "cursor": cursor.model_dump(mode="json"),
                    "source": authority.audit.event_ref,
                    "typed_proposal_id": typed.proposal_id,
                }
            ),
        )
        return RelationshipProposalCompilation(
            status="candidate_recorded",
            source_proposal_id=authority.proposal.proposal_id,
            source_proposal_event_ref=authority.audit.event_ref,
            typed_proposal_id=typed.proposal_id,
            commit=commit,
        )

    def _compile_signal(self, *, authority, change, projection) -> RelationshipProposalProjection:
        source_event, appraisal = self._source_appraisal(
            trigger_ref=authority.proposal.trigger_ref, projection=projection
        )
        self._require_claimed_trigger(
            appraisal_event=source_event, projection=projection
        )
        raw = change.payload.value()
        subject_ref = raw.get("subject_ref")
        if subject_ref != appraisal.subject_ref:
            raise RelationshipProposalCompilerError("subject_not_bound_to_appraisal")
        signal_code = raw.get("signal_code")
        confidence_bp = raw.get("confidence_bp")
        persistence = raw.get("persistence")
        rationale_code = raw.get("rationale_code")
        deltas = raw.get("suggested_deltas")
        if (
            not isinstance(signal_code, str)
            or not isinstance(confidence_bp, int)
            or persistence not in {"session", "durable"}
            or not isinstance(rationale_code, str)
            or not isinstance(deltas, dict)
        ):
            raise RelationshipProposalCompilerError("signal_payload_invalid")
        # Revalidate all six values before we emit typed authority.  They are
        # retained as a non-operative hint for the later adjustment worker.
        RelationshipVariableDeltas.model_validate(deltas)
        if projection.logical_time is None:
            raise RelationshipProposalCompilerError("logical_time_missing")
        evidence = self._evidence(authority.proposal, change.evidence_refs, source_event, projection)
        identity = _digest(
            {
                "source_proposal_event": authority.audit.event_ref,
                "source_change": change.change_id,
                "typed_contract": _CONTRACT,
            }
        )
        typed_proposal_id = f"proposal:relationship-compiled:{identity}"
        typed_change_id = f"change:relationship-compiled:{identity}"
        transition_id = f"transition:relationship-compiled:{identity}"
        mutation_event_id = relationship_mutation_event_id(
            world_id=self._ledger.world_id,
            proposal_id=typed_proposal_id,
            transition_id=transition_id,
            event_type="RelationshipSignalAccepted",
        )
        signal = RelationshipSignalProjection(
            signal_id=f"signal:relationship-compiled:{identity}",
            semantic_fingerprint=relationship_signal_fingerprint(
                subject_ref=subject_ref, signal_code=signal_code, evidence_refs=evidence, policy_refs=_POLICY_REFS
            ),
            entity_revision=1,
            subject_ref=subject_ref,
            signal_code=signal_code,
            confidence_bp=confidence_bp,
            persistence=persistence,
            rationale_code=rationale_code,
            suggested_deltas=RelationshipVariableDeltas.model_validate(deltas),
            evidence_refs=evidence,
            origin=RelationshipSignalOrigin(
                change_id=typed_change_id,
                transition_id=transition_id,
                policy_refs=_POLICY_REFS,
                accepted_event_ref=mutation_event_id,
            ),
            accepted_at=projection.logical_time,
        )
        mutation: dict[str, object] = {
            "change_id": typed_change_id,
            "transition_id": transition_id,
            "expected_entity_revision": 0,
            "evidence_refs": [item.model_dump(mode="json") for item in evidence],
            "policy_refs": list(_POLICY_REFS),
            "acceptance_id": f"acceptance:relationship-compiled:{identity}",
            "proposal_id": typed_proposal_id,
            "evaluated_world_revision": projection.world_revision,
            "accepted_change_hash": "0" * 64,
            "signal": signal.model_dump(mode="json"),
        }
        mutation["accepted_change_hash"] = relationship_mutation_hash(mutation)
        return RelationshipProposalProjection(
            proposal_id=typed_proposal_id,
            proposal_encoding="typed-authority-v1",
            authority_contract_ref="proposal-contract:relationship.1",
            transition_kind="signal",
            change_id=typed_change_id,
            transition_id=transition_id,
            evaluated_world_revision=projection.world_revision,
            expected_entity_revision=0,
            proposed_change_hash=str(mutation["accepted_change_hash"]),
            evidence_refs=evidence,
            policy_refs=_POLICY_REFS,
            proposed_mutation=RelationshipProposedMutation(
                event_type="RelationshipSignalAccepted", payload_json=_canonical(mutation)
            ),
            source_audit=RelationshipProposalAuditBinding(
                proposal_event_ref=authority.audit.event_ref,
                proposal_event_payload_hash=authority.audit.event_payload_hash,
                model_result_ref=authority.audit.model_result_ref,
                capsule_id=authority.audit.capsule_id,
                change_id=change.change_id,
                change_payload_hash=change.payload.payload_hash,
            ),
        )

    def _source_appraisal(self, *, trigger_ref: str, projection) -> tuple[WorldEvent, AppraisalProjection]:
        located = self._ledger.lookup_event_commit(trigger_ref)
        if located is None or located[0].event_type != "AppraisalAccepted":
            raise RelationshipProposalCompilerError("source_appraisal_unavailable")
        event, commit = located
        if commit.world_revision > projection.world_revision:
            raise RelationshipProposalCompilerError("source_appraisal_outside_cursor")
        try:
            appraisal = next(
                item
                for item in projection.appraisals
                if item.status == "active" and item.origin.accepted_event_ref == event.event_id
            )
        except StopIteration as exc:
            raise RelationshipProposalCompilerError("source_appraisal_not_active") from exc
        return event, appraisal

    def _require_claimed_trigger(self, *, appraisal_event: WorldEvent, projection) -> None:
        trigger_id = relationship_deliberation_trigger_id(
            world_id=self._ledger.world_id, appraisal_event_id=appraisal_event.event_id
        )
        process = next(
            (item for item in projection.trigger_processes if item.trigger_id == trigger_id), None
        )
        if (
            process is None
            or process.process_kind != "relationship_deliberation"
            or process.state != "claimed"
            or process.trigger_ref != f"relationship:{appraisal_event.event_id}"
            or process.source_evidence_ref != appraisal_event.event_id
        ):
            raise RelationshipProposalCompilerError("relationship_trigger_not_claimed")

    def _evidence(self, proposal, refs, appraisal_event: WorldEvent, projection) -> tuple[EvidenceRef, ...]:
        if tuple(refs) != (appraisal_event.event_id,):
            raise RelationshipProposalCompilerError("signal_evidence_not_exact_trigger")
        source = next(
            (item for item in proposal.evidence_refs if item.ref_id == appraisal_event.event_id), None
        )
        committed = next(
            (item for item in projection.committed_world_event_refs if item.event_id == appraisal_event.event_id),
            None,
        )
        if (
            source is None
            or source.evidence_kind != "committed_world_event"
            or committed is None
            or source.source_world_revision != committed.world_revision
            or source.immutable_hash != "sha256:" + appraisal_event.payload_hash
        ):
            raise RelationshipProposalCompilerError("signal_evidence_not_authoritative")
        return (
            EvidenceRef(
                ref_id=appraisal_event.event_id,
                evidence_type="committed_world_event",
                claim_purpose="private_hypothesis",
                source_world_revision=committed.world_revision,
                immutable_hash=appraisal_event.payload_hash,
            ),
        )

    def _proposal_event(self, *, typed: RelationshipProposalProjection, source_event: WorldEvent, logical_time) -> WorldEvent:
        if logical_time is None:
            raise RelationshipProposalCompilerError("logical_time_missing")
        # Persist the complete projection image. The authority reader compares
        # this recorded payload byte-for-byte against its later projection, so
        # omitting optional None fields would make a valid proposal unpinnable.
        payload = typed.model_dump(mode="json")
        identity = domain_idempotency_key(event_type="ProposalRecorded", world_id=self._ledger.world_id, payload=payload)
        if identity is None:
            raise RelationshipProposalCompilerError("event_identity_missing")
        return WorldEvent.from_payload(
            schema_version="world-v2.1",
            event_id="event:relationship-proposal-compiled:"
            + _digest({"world": self._ledger.world_id, "proposal": typed.proposal_id}),
            world_id=self._ledger.world_id,
            event_type="ProposalRecorded",
            logical_time=logical_time,
            created_at=source_event.created_at,
            actor="world-v2:relationship-proposal-compiler",
            source="world-v2:relationship-proposal-compiler",
            trace_id=source_event.trace_id,
            causation_id=source_event.event_id,
            correlation_id=source_event.correlation_id,
            idempotency_key=identity,
            payload=payload,
        )

    def _event(self, event_id: str) -> WorldEvent:
        located = self._ledger.lookup_event_commit(event_id)
        if located is None or located[0].event_type != "ProposalRecorded":
            raise RelationshipProposalCompilerError("source_audit_event_missing")
        return located[0]


__all__ = [
    "RelationshipProposalCompilation",
    "RelationshipProposalCompiler",
    "RelationshipProposalCompilerError",
    "relationship_mutation_event_id",
]

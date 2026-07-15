"""Two-stage, source-bound MemoryCandidate acceptance for one Fact.

This module owns the generic typed-proposal sequence for memory candidates.
Callers supply an already accepted Fact and a bounded retention classification;
they cannot choose event identities, evidence, hashes, source bindings, or
the candidate's privacy ceiling.  The resulting pending then active lifecycle
keeps the existing reducer's review semantics intact.
"""

from __future__ import annotations

from datetime import datetime
import hashlib
import json

from .event_identity import domain_idempotency_key
from .fact_memory_draft import FactMemoryRetentionDraft
from .memory_events import MemoryCandidateChangedPayload, memory_candidate_mutation_hash, memory_source_evidence
from .memory_reducers import MEMORY_POLICY_REFS, _canonical_hash
from .schemas import (
    FactProjection,
    FactTransitionProjection,
    MemoryCandidateOrigin,
    MemoryCandidateProjection,
    MemoryCandidateProposedMutation,
    MemoryCandidateProposalProjection,
    MemoryCandidateValues,
    MemorySourceBinding,
    WorldEvent,
    memory_candidate_semantic_fingerprint,
    memory_retrieval_strength_bp,
    memory_source_authority_id,
    memory_source_cluster_fingerprint,
)
from .sqlite_ledger import SQLiteWorldLedger


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


class FactMemoryCandidateLifecycle:
    """Accept one pending→active candidate from one current accepted Fact."""

    def __init__(self, *, ledger: SQLiteWorldLedger, actor: str, source: str) -> None:
        if type(ledger) is not SQLiteWorldLedger or not actor or not source:
            raise ValueError("memory lifecycle requires SQLite ledger, actor, and source")
        self._ledger = ledger
        self._actor = actor
        self._source = source

    def accept(
        self,
        *,
        fact: FactProjection,
        transition: FactTransitionProjection,
        fact_event: WorldEvent,
        fact_world_revision: int,
        draft: FactMemoryRetentionDraft,
        logical_time: datetime,
        created_at: datetime,
        trace_id: str,
        correlation_id: str,
    ) -> MemoryCandidateProjection | None:
        """Write open then acceptance transitions, or return an existing source candidate."""

        source = self._source_binding(
            fact=fact,
            transition=transition,
            fact_event=fact_event,
            fact_world_revision=fact_world_revision,
        )
        candidate_id = "memory:fact:" + _digest(source.model_dump(mode="json"))
        projection = self._ledger.project()
        if any(
            item.candidate_id == candidate_id
            or source.authority_event_ref
            in {binding.authority_event_ref for binding in item.values.source_bindings}
            for item in projection.memory_candidates
        ):
            return None
        opened_event_id = f"event:memory:opened:{_digest(candidate_id)}"
        opened = self._candidate(
            candidate_id=candidate_id,
            source=source,
            draft=draft,
            privacy_ceiling=fact.values.privacy_class,
            entity_revision=1,
            status="pending",
            opened_at=logical_time,
            updated_at=logical_time,
            reviewed_at=None,
            accepted_event_ref=opened_event_id,
        )
        self._record_and_accept(
            after=opened,
            before=None,
            operation="open",
            logical_time=logical_time,
            created_at=created_at,
            trace_id=trace_id,
            correlation_id=correlation_id,
        )
        projection = self._ledger.project()
        active_event_id = f"event:memory:accepted:{_digest(candidate_id)}"
        active = self._candidate(
            candidate_id=candidate_id,
            source=source,
            draft=draft,
            privacy_ceiling=fact.values.privacy_class,
            entity_revision=2,
            status="active",
            opened_at=opened.opened_at,
            updated_at=logical_time,
            reviewed_at=logical_time,
            accepted_event_ref=active_event_id,
        )
        self._record_and_accept(
            after=active,
            before=opened,
            operation="accept",
            logical_time=logical_time,
            created_at=created_at,
            trace_id=trace_id,
            correlation_id=correlation_id,
        )
        return active

    def _source_binding(
        self,
        *,
        fact: FactProjection,
        transition: FactTransitionProjection,
        fact_event: WorldEvent,
        fact_world_revision: int,
    ) -> MemorySourceBinding:
        if (
            fact_event.event_type != "FactCommittedV2"
            or transition.fact_id != fact.fact_id
            or transition.entity_revision != fact.entity_revision
            or transition.values_after != fact.values
            or transition.accepted_event_ref != fact_event.event_id
            or fact_world_revision < 1
        ):
            raise ValueError("memory lifecycle requires one exact accepted Fact transition")
        projection = self._ledger.project()
        committed = next(
            item
            for item in projection.committed_world_event_refs
            if item.event_id == fact_event.event_id
        )
        projected_transition = next(
            item
            for item in projection.fact_transitions
            if item.transition_id == transition.transition_id
        )
        if (
            committed.world_revision != fact_world_revision
            or committed.payload_hash != fact_event.payload_hash
            or projected_transition != transition
        ):
            raise ValueError("memory lifecycle Fact authority is no longer the current ledger image")
        return MemorySourceBinding(
            source_kind="fact",
            source_id=fact.fact_id,
            source_entity_revision=fact.entity_revision,
            authority_event_ref=fact_event.event_id,
            authority_world_revision=committed.world_revision,
            authority_payload_hash=committed.payload_hash,
            source_values_hash=_canonical_hash(projected_transition.values_after),
        )

    @staticmethod
    def _candidate(
        *,
        candidate_id: str,
        source: MemorySourceBinding,
        draft: FactMemoryRetentionDraft,
        privacy_ceiling: str,
        entity_revision: int,
        status: str,
        opened_at: datetime,
        updated_at: datetime,
        reviewed_at: datetime | None,
        accepted_event_ref: str,
    ) -> MemoryCandidateProjection:
        values = MemoryCandidateValues(
            summary_ref=f"summary:source:{source.authority_event_ref}",
            summary_payload_hash=source.authority_payload_hash,
            cue_kind=draft.cue_kind,
            source_bindings=(source,),
            consumed_source_authority_ids=(memory_source_authority_id(source),),
            retention_rationales=draft.retention_rationales,
            privacy_ceiling=privacy_ceiling,
            salience=draft.salience,
            status=status,  # type: ignore[arg-type]
            retrieval_strength_bp=memory_retrieval_strength_bp(draft.salience),
            reinforcement_count=0,
            reviewed_at=reviewed_at,
        )
        origin = MemoryCandidateOrigin(
            change_id=f"change:memory:{candidate_id}:{entity_revision}",
            transition_id=f"transition:memory:{candidate_id}:{entity_revision}",
            policy_refs=MEMORY_POLICY_REFS,
            accepted_event_ref=accepted_event_ref,
        )
        cluster = memory_source_cluster_fingerprint(values=values, policy_refs=origin.policy_refs)
        return MemoryCandidateProjection(
            candidate_id=candidate_id,
            entity_revision=entity_revision,
            semantic_fingerprint=memory_candidate_semantic_fingerprint(
                values=values, policy_refs=origin.policy_refs
            ),
            source_cluster_fingerprint=cluster,
            source_cluster_lineage=(cluster,),
            values=values,
            origin=origin,
            opened_at=opened_at,
            updated_at=updated_at,
        )

    def _record_and_accept(
        self,
        *,
        after: MemoryCandidateProjection,
        before: MemoryCandidateProjection | None,
        operation: str,
        logical_time: datetime,
        created_at: datetime,
        trace_id: str,
        correlation_id: str,
    ) -> None:
        projected = self._ledger.project()
        mutation = self._mutation(
            after=after,
            before=before,
            operation=operation,
            evaluated_world_revision=projected.world_revision,
        )
        proposal = MemoryCandidateProposalProjection(
            proposal_id=mutation.proposal_id,
            proposal_encoding="typed-authority-v1",
            authority_contract_ref="proposal-contract:memory-candidate.1",
            transition_kind=operation,  # type: ignore[arg-type]
            change_id=mutation.change_id,
            transition_id=mutation.transition_id,
            evaluated_world_revision=mutation.evaluated_world_revision,
            expected_entity_revision=mutation.expected_entity_revision,
            proposed_change_hash=mutation.accepted_change_hash,
            evidence_refs=mutation.evidence_refs,
            policy_refs=mutation.policy_refs,
            proposed_mutation=MemoryCandidateProposedMutation(
                event_type={"open": "MemoryCandidateOpened", "accept": "MemoryCandidateAccepted"}[operation],
                payload_json=_canonical(mutation.model_dump(mode="json")),
            ),
        )
        proposal_event = self._event(
            event_id=f"event:memory:proposal:{_digest(mutation.proposal_id)}",
            event_type="ProposalRecorded",
            payload=proposal.model_dump(mode="json"),
            logical_time=logical_time,
            created_at=created_at,
            trace_id=trace_id,
            causation_id=after.values.source_bindings[0].authority_event_ref,
            correlation_id=correlation_id,
        )
        self._ledger.commit(
            (proposal_event,),
            expected_world_revision=projected.world_revision,
            expected_deliberation_revision=projected.deliberation_revision,
        )
        projected = self._ledger.project()
        acceptance_payload = {
            "acceptance_id": mutation.acceptance_id,
            "status": "accepted",
            "proposal_id": mutation.proposal_id,
            "evaluated_world_revision": mutation.evaluated_world_revision,
            "accepted_change_id": mutation.change_id,
            "accepted_change_hash": mutation.accepted_change_hash,
        }
        acceptance = self._event(
            event_id=f"event:memory:acceptance:{_digest(mutation.acceptance_id)}",
            event_type="AcceptanceRecorded",
            payload=acceptance_payload,
            logical_time=logical_time,
            created_at=created_at,
            trace_id=trace_id,
            causation_id=proposal_event.event_id,
            correlation_id=correlation_id,
        )
        mutation_event = self._event(
            event_id=after.origin.accepted_event_ref,
            event_type=proposal.proposed_mutation.event_type,
            payload=mutation.model_dump(mode="json"),
            logical_time=logical_time,
            created_at=created_at,
            trace_id=trace_id,
            causation_id=acceptance.event_id,
            correlation_id=correlation_id,
        )
        self._ledger.commit(
            (acceptance, mutation_event),
            expected_world_revision=projected.world_revision,
            expected_deliberation_revision=projected.deliberation_revision,
        )

    @staticmethod
    def _mutation(
        *,
        after: MemoryCandidateProjection,
        before: MemoryCandidateProjection | None,
        operation: str,
        evaluated_world_revision: int,
    ) -> MemoryCandidateChangedPayload:
        raw = {
            "change_id": after.origin.change_id,
            "transition_id": after.origin.transition_id,
            "expected_entity_revision": before.entity_revision if before else 0,
            "evidence_refs": tuple(memory_source_evidence(item) for item in after.values.source_bindings),
            "policy_refs": MEMORY_POLICY_REFS,
            "acceptance_id": f"acceptance:{after.origin.transition_id}",
            "proposal_id": f"proposal:{after.origin.transition_id}",
            "evaluated_world_revision": evaluated_world_revision,
            "accepted_change_hash": "0" * 64,
            "operation": operation,
            "candidate_before": before,
            "candidate_after": after,
            "revise_kind": None,
            "reinforcement_reason": None,
            "rejection_reason": None,
            "forget_authority": None,
            "strength_before_bp": None,
            "strength_after_bp": None,
            "reinforcement_count_before": None,
            "reinforcement_count_after": None,
            "policy_version": None,
            "policy_digest": None,
        }
        raw["accepted_change_hash"] = memory_candidate_mutation_hash(raw)
        return MemoryCandidateChangedPayload.model_validate(raw)

    def _event(
        self,
        *,
        event_id: str,
        event_type: str,
        payload: dict[str, object],
        logical_time: datetime,
        created_at: datetime,
        trace_id: str,
        causation_id: str,
        correlation_id: str,
    ) -> WorldEvent:
        identity = domain_idempotency_key(
            event_type=event_type, world_id=self._ledger.world_id, payload=payload
        )
        if identity is None:
            raise ValueError(f"memory lifecycle has no identity for {event_type}")
        return WorldEvent.from_payload(
            schema_version="world-v2.1",
            event_id=event_id,
            world_id=self._ledger.world_id,
            event_type=event_type,
            logical_time=logical_time,
            created_at=created_at,
            actor=self._actor,
            source=self._source,
            trace_id=trace_id,
            causation_id=causation_id,
            correlation_id=correlation_id,
            idempotency_key=identity,
            payload=payload,
        )


__all__ = ["FactMemoryCandidateLifecycle"]

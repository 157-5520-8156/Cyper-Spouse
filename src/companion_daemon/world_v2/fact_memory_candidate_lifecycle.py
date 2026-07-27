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
from .errors import ConcurrencyConflict
from .fact_events import FactChangedPayload
from .fact_memory_draft import FactMemoryRetentionDraft
from .memory_events import (
    MemoryCandidateChangedPayload,
    memory_candidate_mutation_hash,
    memory_source_evidence,
)
from .memory_reducers import (
    MEMORY_POLICY_REFS,
    _canonical_hash,
    evaluate_memory_retrieval,
)
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

        # Pin authority, logical time, and both proposal transitions inside one
        # cross-process writer sequence.  A sibling worker may legitimately
        # advance World time after the caller classified the Fact; rebuilding
        # the mechanical timestamps here prevents that race from turning a
        # valid retention decision into a stale lived-world event.
        with self._ledger.serialized_commit_sequence():
            projected = self._ledger.project()
            current_fact = next(
                (
                    item
                    for item in projected.facts
                    if item.fact_id == fact.fact_id
                ),
                None,
            )
            if (
                current_fact != fact
                or current_fact.values.status != "active"
                or current_fact.origin.accepted_event_ref != fact_event.event_id
            ):
                # The model's retention decision remains an auditable result
                # for its source epoch, but a superseded/withdrawn Fact may no
                # longer create or revise retrieval memory.
                self._settle_stale_fact_proposals(
                    projection=projected,
                    fact_event_ref=fact_event.event_id,
                    logical_time=projected.logical_time or logical_time,
                    created_at=max(
                        created_at,
                        projected.logical_time or logical_time,
                    ),
                    trace_id=trace_id,
                    correlation_id=correlation_id,
                )
                return None
            if (
                projected.logical_time is not None
                and logical_time > projected.logical_time
            ):
                raise ValueError(
                    "Fact-memory lifecycle cannot advance World logical time"
                )
            effective_logical_time = projected.logical_time or logical_time
            return self._accept_serialized(
                fact=fact,
                transition=transition,
                fact_event=fact_event,
                fact_world_revision=fact_world_revision,
                draft=draft,
                logical_time=effective_logical_time,
                created_at=max(created_at, effective_logical_time),
                trace_id=trace_id,
                correlation_id=correlation_id,
            )

    def _accept_serialized(
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
        source = self._source_binding(
            fact=fact,
            transition=transition,
            fact_event=fact_event,
            fact_world_revision=fact_world_revision,
        )
        candidate_id = "memory:fact:" + _digest(source.model_dump(mode="json"))
        projection = self._ledger.project()
        predecessor = next(
            (
                item
                for item in projection.memory_candidates
                if item.values.status == "active"
                and any(
                    binding.source_kind == "fact"
                    and binding.source_id == fact.fact_id
                    and binding.source_entity_revision < fact.entity_revision
                    for binding in item.values.source_bindings
                )
            ),
            None,
        )
        if predecessor is not None:
            corrected = self._corrected_candidate(
                before=predecessor,
                source=source,
                draft=draft,
                privacy_ceiling=fact.values.privacy_class,
                logical_time=logical_time,
            )
            self._record_and_accept(
                after=corrected,
                before=predecessor,
                operation="revise",
                revise_kind="correct",
                logical_time=logical_time,
                created_at=created_at,
                trace_id=trace_id,
                correlation_id=correlation_id,
            )
            return next(
                item
                for item in self._ledger.project().memory_candidates
                if item.candidate_id == corrected.candidate_id
            )
        existing = next(
            (
                item
                for item in projection.memory_candidates
                if (
                    item.candidate_id == candidate_id
                    or source.authority_event_ref
                    in {
                        binding.authority_event_ref
                        for binding in item.values.source_bindings
                    }
                )
            ),
            None,
        )
        opened_event_id = f"event:memory:opened:{_digest(candidate_id)}"
        if existing is not None:
            if existing.values.status != "pending":
                return None
            if (
                existing.candidate_id != candidate_id
                or existing.entity_revision != 1
                or existing.origin.accepted_event_ref != opened_event_id
                or existing.values.source_bindings != (source,)
            ):
                raise ValueError("existing pending Fact memory candidate does not match its source")
            # The open transition is durable.  Resume the second transition
            # after a process crash instead of treating pending as terminal.
            opened = existing
            draft = FactMemoryRetentionDraft(
                cue_kind=opened.values.cue_kind,
                retention_rationales=opened.values.retention_rationales,
                salience=opened.values.salience,
            )
        else:
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
            opened = next(
                item
                for item in self._ledger.project().memory_candidates
                if item.candidate_id == candidate_id
            )
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
        return next(
            item
            for item in self._ledger.project().memory_candidates
            if item.candidate_id == candidate_id
        )

    def _source_binding(
        self,
        *,
        fact: FactProjection,
        transition: FactTransitionProjection,
        fact_event: WorldEvent,
        fact_world_revision: int,
    ) -> MemorySourceBinding:
        if (
            fact_event.event_type not in {"FactCommittedV2", "FactCorrected"}
            or transition.fact_id != fact.fact_id
            or transition.entity_revision != fact.entity_revision
            or transition.values_after != fact.values
            or transition.accepted_event_ref != fact_event.event_id
            or fact_world_revision < 1
        ):
            raise ValueError("memory lifecycle requires one exact accepted Fact transition")
        if fact_event.event_type == "FactCorrected":
            corrected = FactChangedPayload.model_validate_json(fact_event.payload_json)
            if corrected.operation != "correct" or corrected.fact_after != fact:
                raise ValueError("memory lifecycle correction event does not match Fact authority")
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

    @staticmethod
    def _corrected_candidate(
        *,
        before: MemoryCandidateProjection,
        source: MemorySourceBinding,
        draft: FactMemoryRetentionDraft,
        privacy_ceiling: str,
        logical_time: datetime,
    ) -> MemoryCandidateProjection:
        privacy_rank = {
            "public": 0,
            "shareable": 1,
            "personal": 2,
            "private": 3,
            "withhold": 4,
        }
        corrected_bindings: list[MemorySourceBinding] = []
        replaced = False
        for binding in before.values.source_bindings:
            if (
                binding.source_kind == "fact"
                and binding.source_id == source.source_id
                and binding.source_entity_revision < source.source_entity_revision
            ):
                if not replaced:
                    corrected_bindings.append(source)
                    replaced = True
                continue
            corrected_bindings.append(binding)
        if not replaced:
            raise ValueError("memory correction has no stale Fact source to replace")
        values = before.values.model_copy(
            update={
                "summary_ref": f"summary:source:{source.authority_event_ref}",
                "summary_payload_hash": source.authority_payload_hash,
                "cue_kind": draft.cue_kind,
                "source_bindings": tuple(corrected_bindings),
                "consumed_source_authority_ids": (
                    *before.values.consumed_source_authority_ids,
                    memory_source_authority_id(source),
                ),
                "retention_rationales": draft.retention_rationales,
                "privacy_ceiling": max(
                    (before.values.privacy_ceiling, privacy_ceiling),
                    key=privacy_rank.__getitem__,
                ),
                "salience": draft.salience,
                "retrieval_strength_bp": memory_retrieval_strength_bp(
                    draft.salience
                ),
                "reviewed_at": logical_time,
            }
        )
        revision = before.entity_revision + 1
        event_id = f"event:memory:corrected:{_digest([before.candidate_id, revision, source.authority_event_ref])}"
        origin = before.origin.model_copy(
            update={
                "change_id": f"change:memory:{before.candidate_id}:{revision}",
                "transition_id": f"transition:memory:{before.candidate_id}:{revision}",
                "accepted_event_ref": event_id,
            }
        )
        cluster = memory_source_cluster_fingerprint(values=values, policy_refs=origin.policy_refs)
        lineage = before.source_cluster_lineage
        if cluster != before.source_cluster_fingerprint:
            lineage = (*lineage, cluster)
        return MemoryCandidateProjection(
            candidate_id=before.candidate_id,
            entity_revision=revision,
            semantic_fingerprint=memory_candidate_semantic_fingerprint(
                values=values,
                policy_refs=origin.policy_refs,
            ),
            source_cluster_fingerprint=cluster,
            source_cluster_lineage=lineage,
            values=values,
            origin=origin,
            opened_at=before.opened_at,
            updated_at=logical_time,
        )

    def retire_superseded_fact_source(
        self,
        *,
        fact: FactProjection,
        logical_time: datetime,
        created_at: datetime,
        trace_id: str,
        correlation_id: str,
    ) -> MemoryCandidateProjection | None:
        """Remove stale Fact authority while preserving independent sources.

        This is source hygiene, not a semantic forgetting decision.  A
        single-source candidate is deliberately left active-but-suppressed;
        only a candidate that still has independently current authority can
        shed the obsolete Fact binding.
        """

        with self._ledger.serialized_commit_sequence():
            projection = self._ledger.project()
            current_fact = next(
                (
                    item
                    for item in projection.facts
                    if item.fact_id == fact.fact_id
                ),
                None,
            )
            if current_fact != fact or current_fact.values.status != "active":
                return None
            matches = tuple(
                item
                for item in projection.memory_candidates
                if item.values.status == "active"
                and any(
                    binding.source_kind == "fact"
                    and binding.source_id == fact.fact_id
                    and binding.source_entity_revision < fact.entity_revision
                    for binding in item.values.source_bindings
                )
            )
            if len(matches) > 1:
                raise ValueError(
                    "superseded Fact source resolves to multiple active memory candidates"
                )
            if not matches:
                return None
            before = matches[0]
            retrieval = evaluate_memory_retrieval(
                (before,),
                facts=projection.facts,
                fact_history=projection.fact_transitions,
                experiences=projection.experiences,
                experience_history=projection.experience_transitions,
                threads=projection.threads,
                thread_history=projection.thread_transitions,
                committed_events=projection.committed_world_event_refs,
                viewer_privacy_ceiling="withhold",
            )[0]
            if retrieval.stale_source_ids != (fact.fact_id,):
                # Do not create an orphan proposal when any independent
                # authority is also stale.  Its own lifecycle must settle that
                # source before this candidate can be safely rewritten.
                return None
            remaining = tuple(
                binding
                for binding in before.values.source_bindings
                if not (
                    binding.source_kind == "fact"
                    and binding.source_id == fact.fact_id
                    and binding.source_entity_revision < fact.entity_revision
                )
            )
            if not remaining:
                return None
            effective_logical_time = projection.logical_time or logical_time
            after = self._candidate_without_superseded_fact(
                before=before,
                remaining=remaining,
                logical_time=effective_logical_time,
            )
            self._record_and_accept(
                after=after,
                before=before,
                operation="revise",
                revise_kind="correct",
                logical_time=effective_logical_time,
                created_at=max(created_at, effective_logical_time),
                trace_id=trace_id,
                correlation_id=correlation_id,
            )
            return next(
                item
                for item in self._ledger.project().memory_candidates
                if item.candidate_id == before.candidate_id
            )

    @staticmethod
    def _candidate_without_superseded_fact(
        *,
        before: MemoryCandidateProjection,
        remaining: tuple[MemorySourceBinding, ...],
        logical_time: datetime,
    ) -> MemoryCandidateProjection:
        if not remaining or len(remaining) >= len(before.values.source_bindings):
            raise ValueError("memory source retirement requires a non-empty strict subset")
        anchor = remaining[0]
        values = before.values.model_copy(
            update={
                "summary_ref": f"summary:source:{anchor.authority_event_ref}",
                "summary_payload_hash": anchor.authority_payload_hash,
                "source_bindings": remaining,
                "reviewed_at": logical_time,
            }
        )
        revision = before.entity_revision + 1
        event_id = (
            "event:memory:source-retired:"
            + _digest(
                [
                    before.candidate_id,
                    revision,
                    tuple(
                        binding.authority_event_ref
                        for binding in before.values.source_bindings
                        if binding not in remaining
                    ),
                ]
            )
        )
        origin = before.origin.model_copy(
            update={
                "change_id": f"change:memory:{before.candidate_id}:{revision}",
                "transition_id": f"transition:memory:{before.candidate_id}:{revision}",
                "accepted_event_ref": event_id,
            }
        )
        cluster = memory_source_cluster_fingerprint(
            values=values,
            policy_refs=origin.policy_refs,
        )
        lineage = before.source_cluster_lineage
        if cluster != before.source_cluster_fingerprint:
            lineage = (*lineage, cluster)
        return MemoryCandidateProjection(
            candidate_id=before.candidate_id,
            entity_revision=revision,
            semantic_fingerprint=memory_candidate_semantic_fingerprint(
                values=values,
                policy_refs=origin.policy_refs,
            ),
            source_cluster_fingerprint=cluster,
            source_cluster_lineage=lineage,
            values=values,
            origin=origin,
            opened_at=before.opened_at,
            updated_at=logical_time,
        )

    def _record_and_accept(
        self,
        *,
        after: MemoryCandidateProjection,
        before: MemoryCandidateProjection | None,
        operation: str,
        revise_kind: str | None = None,
        logical_time: datetime,
        created_at: datetime,
        trace_id: str,
        correlation_id: str,
        _proposal_attempt: int = 0,
    ) -> None:
        with self._ledger.serialized_commit_sequence():
            self._record_and_accept_serialized(
                after=after,
                before=before,
                operation=operation,
                revise_kind=revise_kind,
                logical_time=logical_time,
                created_at=created_at,
                trace_id=trace_id,
                correlation_id=correlation_id,
                _proposal_attempt=_proposal_attempt,
            )

    def _record_and_accept_serialized(
        self,
        *,
        after: MemoryCandidateProjection,
        before: MemoryCandidateProjection | None,
        operation: str,
        revise_kind: str | None = None,
        logical_time: datetime,
        created_at: datetime,
        trace_id: str,
        correlation_id: str,
        _proposal_attempt: int = 0,
    ) -> None:
        projected = self._ledger.project()
        if projected.logical_time != logical_time:
            raise ValueError(
                "Fact-memory lifecycle logical time is stale: "
                f"candidate={logical_time.isoformat()} "
                f"head={projected.logical_time.isoformat() if projected.logical_time else None}"
            )
        mutation = self._mutation(
            after=after,
            before=before,
            operation=operation,
            revise_kind=revise_kind,
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
                event_type={
                    "open": "MemoryCandidateOpened",
                    "accept": "MemoryCandidateAccepted",
                    "revise": "MemoryCandidateRevised",
                }[operation],
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
        existing_proposal = next(
            (
                item
                for item in projected.memory_candidate_proposals
                if item.proposal_id == proposal.proposal_id
            ),
            None,
        )
        existing_decision = next(
            (
                item
                for item in projected.acceptance_decisions
                if item.proposal_id == proposal.proposal_id
            ),
            None,
        )
        if existing_decision is not None:
            if existing_decision.status == "accepted":
                return
            after = self._candidate_for_world_epoch(
                after=after,
                world_revision=projected.world_revision,
            )
            return self._record_and_accept_serialized(
                after=after,
                before=before,
                operation=operation,
                revise_kind=revise_kind,
                logical_time=logical_time,
                created_at=created_at,
                trace_id=trace_id,
                correlation_id=correlation_id,
                _proposal_attempt=_proposal_attempt,
            )
        if existing_proposal is not None:
            if (
                existing_proposal.evaluated_world_revision
                == projected.world_revision
            ):
                if existing_proposal != proposal:
                    raise ValueError(
                        "memory proposal identity collides inside one World epoch"
                    )
            else:
                self._settle_proposal_stale(
                    proposal=existing_proposal,
                    projection=projected,
                    logical_time=logical_time,
                    created_at=created_at,
                    trace_id=trace_id,
                    correlation_id=correlation_id,
                )
                latest = self._ledger.project()
                after = self._candidate_for_world_epoch(
                    after=after,
                    world_revision=latest.world_revision,
                )
                return self._record_and_accept_serialized(
                    after=after,
                    before=before,
                    operation=operation,
                    revise_kind=revise_kind,
                    logical_time=logical_time,
                    created_at=created_at,
                    trace_id=trace_id,
                    correlation_id=correlation_id,
                    _proposal_attempt=_proposal_attempt,
                )
        else:
            try:
                self._ledger.commit(
                    (proposal_event,),
                    expected_world_revision=projected.world_revision,
                    expected_deliberation_revision=projected.deliberation_revision,
                )
            except ConcurrencyConflict:
                if _proposal_attempt >= 2:
                    raise
                # No proposal bytes were committed. Recompile the exact same
                # source-bound transition against the winning cursor.
                return self._record_and_accept(
                    after=after,
                    before=before,
                    operation=operation,
                    revise_kind=revise_kind,
                    logical_time=logical_time,
                    created_at=created_at,
                    trace_id=trace_id,
                    correlation_id=correlation_id,
                    _proposal_attempt=_proposal_attempt + 1,
                )
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
        for attempt in range(3):
            projected = self._ledger.project()
            try:
                self._ledger.commit(
                    (acceptance, mutation_event),
                    expected_world_revision=projected.world_revision,
                    expected_deliberation_revision=projected.deliberation_revision,
                )
            except ConcurrencyConflict:
                if attempt == 2:
                    raise
                continue
            break

    def _settle_stale_fact_proposals(
        self,
        *,
        projection,
        fact_event_ref: str,
        logical_time: datetime,
        created_at: datetime,
        trace_id: str,
        correlation_id: str,
    ) -> None:
        decided = {
            item.proposal_id for item in projection.acceptance_decisions
        }
        proposals = tuple(
            item
            for item in projection.memory_candidate_proposals
            if item.proposal_id not in decided
            and any(
                evidence.ref_id == fact_event_ref
                for evidence in item.evidence_refs
            )
            and item.evaluated_world_revision < projection.world_revision
        )
        for proposal in proposals:
            current = self._ledger.project()
            self._settle_proposal_stale(
                proposal=proposal,
                projection=current,
                logical_time=current.logical_time or logical_time,
                created_at=max(
                    created_at,
                    current.logical_time or logical_time,
                ),
                trace_id=trace_id,
                correlation_id=correlation_id,
            )

    def _settle_proposal_stale(
        self,
        *,
        proposal: MemoryCandidateProposalProjection,
        projection,
        logical_time: datetime,
        created_at: datetime,
        trace_id: str,
        correlation_id: str,
    ) -> None:
        if proposal.evaluated_world_revision >= projection.world_revision:
            raise ValueError("memory proposal is not from a stale World epoch")
        acceptance_id = f"acceptance:memory:stale:{_digest(proposal.proposal_id)}"
        event = self._event(
            event_id=f"event:memory:acceptance-stale:{_digest(proposal.proposal_id)}",
            event_type="AcceptanceRecorded",
            payload={
                "acceptance_id": acceptance_id,
                "status": "stale",
                "proposal_id": proposal.proposal_id,
                "evaluated_world_revision": proposal.evaluated_world_revision,
            },
            logical_time=logical_time,
            created_at=created_at,
            trace_id=trace_id,
            causation_id=f"event:memory:proposal:{_digest(proposal.proposal_id)}",
            correlation_id=correlation_id,
        )
        self._ledger.commit(
            (event,),
            expected_world_revision=projection.world_revision,
            expected_deliberation_revision=projection.deliberation_revision,
        )

    @staticmethod
    def _candidate_for_world_epoch(
        *,
        after: MemoryCandidateProjection,
        world_revision: int,
    ) -> MemoryCandidateProjection:
        epoch = _digest(
            [
                after.candidate_id,
                after.entity_revision,
                world_revision,
            ]
        )
        origin = after.origin.model_copy(
            update={
                "change_id": f"change:memory:{after.candidate_id}:{after.entity_revision}:epoch:{epoch}",
                "transition_id": (
                    f"transition:memory:{after.candidate_id}:"
                    f"{after.entity_revision}:epoch:{epoch}"
                ),
            }
        )
        return after.model_copy(update={"origin": origin})

    @staticmethod
    def _mutation(
        *,
        after: MemoryCandidateProjection,
        before: MemoryCandidateProjection | None,
        operation: str,
        evaluated_world_revision: int,
        revise_kind: str | None,
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
            "revise_kind": revise_kind,
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

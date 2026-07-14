"""Pure reducers and fail-closed retrieval checks for MemoryCandidate F2."""

from __future__ import annotations

from datetime import datetime
import hashlib
import json

from .fact_events import FACT_PAYLOAD_MODELS
from .memory_events import (
    MemoryCandidateChangedPayload,
    MemoryClockForgetAuthority,
    MemoryCompressionForgetAuthority,
    MemoryDeliberativeForgetAuthority,
    MemoryEvidenceForgetAuthority,
    MemorySourceInvalidationForgetAuthority,
)
from .schemas import (
    CommittedWorldEventRef,
    ExperienceProjection,
    ExperienceTransitionProjection,
    FactProjection,
    FactTransitionProjection,
    MemoryCandidateProjection,
    MemoryCandidateTransitionProjection,
    MemoryRetrievalDecision,
    MemorySalienceVector,
    MemorySourceBinding,
    PrivacyClass,
    ThreadProjection,
    ThreadTransitionProjection,
    memory_source_authority_id,
)
from .thread_reducers import TERMINAL_THREAD_STATUSES


MEMORY_POLICY_REFS = ("policy:memory-candidate-v1",)
MEMORY_POLICY_VERSION = "memory-candidate-policy.1"
_REINFORCEMENT_REASON_FIELDS = {
    "identity_relevance": "autobiographical_relevance_bp",
    "relationship_continuity": "relationship_relevance_bp",
    "boundary_relevance": "emotional_residue_bp",
    "unfinished_business": "unfinished_business_bp",
    "repeated_pattern": "recurrence_bp",
    "future_utility": "future_utility_bp",
    "emotional_salience": "emotional_residue_bp",
    "world_continuity": "world_continuity_bp",
}
_RECURRENCE_DELTA_PER_SOURCE_BP = 750
_RATIONALE_DELTA_PER_SOURCE_BP = 500
_SALIENCE_CAP_BP = 10_000
_MEMORY_POLICY_ARTIFACT = {
    "version": MEMORY_POLICY_VERSION,
    "salience_matrix_version": "memory-salience-matrix.1",
    "reinforcement": {
        "recurrence_delta_per_novel_source_bp": 750,
        "rationale_dimension_delta_per_novel_source_bp": 500,
        "maximum_bp": 10_000,
        "requires_novel_exact_source": True,
        "reason_dimension_map": _REINFORCEMENT_REASON_FIELDS,
    },
    "forget_authority": {
        "clock": {
            "requires_exact_latest_clock": True,
            "requires_frozen_review_due_at": True,
            "committed_clock_and_mutation_must_reach_due": True,
        },
        "evidence": {
            "scope_contract": "memory-forget-scope.1",
            "privacy_request_principal": "exact_message_actor",
            "privacy_request_content": "exact_message_content_hash",
            "explicit_suppression_principal": "exact_operator_observation_id",
        },
        "source_invalidation": {
            "identity": "kind+id+entity_revision+authority_id",
            "all_named_sources_must_be_stale": True,
        },
        "compression": {
            "target_must_be_distinct_current_active": True,
            "target_must_match_cue_kind": True,
            "target_consumed_authority_must_cover_source": True,
            "target_event_must_be_exact": True,
        },
        "accepted_deliberation": {"reason": "low_future_utility"},
    },
    "privacy": {"withhold_excluded_from_ordinary_retrieval": True},
}
MEMORY_POLICY_DIGEST = hashlib.sha256(
    json.dumps(_MEMORY_POLICY_ARTIFACT, sort_keys=True, separators=(",", ":")).encode()
).hexdigest()
_PRIVACY_RANK = {
    "public": 0,
    "shareable": 1,
    "personal": 2,
    "private": 3,
    "withhold": 4,
}
_EVENT_OPERATION = {
    "MemoryCandidateOpened": "open",
    "MemoryCandidateAccepted": "accept",
    "MemoryCandidateRejected": "reject",
    "MemoryCandidateRevised": "revise",
    "MemoryCandidateReinforced": "reinforce",
    "MemoryCandidateForgotten": "forget",
}


def reduce_memory_candidate(
    candidates: tuple[MemoryCandidateProjection, ...],
    history: tuple[MemoryCandidateTransitionProjection, ...],
    payload: MemoryCandidateChangedPayload,
    *,
    event_type: str,
    event_id: str,
    logical_time: datetime,
    facts: tuple[FactProjection, ...],
    fact_history: tuple[FactTransitionProjection, ...],
    experiences: tuple[object, ...],
    experience_history: tuple[ExperienceTransitionProjection, ...],
    threads: tuple[ThreadProjection, ...],
    thread_history: tuple[ThreadTransitionProjection, ...],
    committed_events: tuple[CommittedWorldEventRef, ...],
) -> tuple[
    tuple[MemoryCandidateProjection, ...],
    tuple[MemoryCandidateTransitionProjection, ...],
]:
    if _EVENT_OPERATION.get(event_type) != payload.operation:
        raise ValueError("memory candidate event type does not match operation")
    after = payload.candidate_after
    before = payload.candidate_before
    current = next(
        (item for item in candidates if item.candidate_id == after.candidate_id), None
    )
    if after.origin.accepted_event_ref != event_id:
        raise ValueError("memory origin does not identify its accepted event")
    if payload.policy_refs != MEMORY_POLICY_REFS:
        raise ValueError("memory candidate references an uninstalled policy")
    if after.updated_at != logical_time:
        raise ValueError("memory candidate update time must match logical time")
    require_current_sources = payload.operation not in {"reject", "forget"}
    source_privacies = _validate_sources(
        after.values.source_bindings,
        facts=facts,
        fact_history=fact_history,
        experiences=experiences,
        experience_history=experience_history,
        threads=threads,
        thread_history=thread_history,
        committed_events=committed_events,
        require_current=require_current_sources,
    )
    required_privacy = max(_PRIVACY_RANK[item] for item in source_privacies)
    if _PRIVACY_RANK[after.values.privacy_ceiling] < required_privacy:
        raise ValueError("memory privacy ceiling weakens source authority")

    if payload.operation == "open":
        if current is not None or before is not None:
            raise ValueError("memory candidate identity already exists")
        if (
            after.values.status != "pending"
            or after.opened_at != logical_time
            or after.values.reinforcement_count != 0
        ):
            raise ValueError("memory open must create a pending unreinforced candidate")
        if after.values.consumed_source_authority_ids != tuple(
            memory_source_authority_id(item) for item in after.values.source_bindings
        ) or after.source_cluster_lineage != (after.source_cluster_fingerprint,):
            raise ValueError("memory open must initialize exact source lineage")
        updated = (*candidates, after)
    else:
        if current is None or current != before:
            raise ValueError("memory candidate before image does not match current head")
        if current.entity_revision != payload.expected_entity_revision:
            raise ValueError("memory candidate entity revision compare-and-swap failed")
        expected_cluster_lineage = current.source_cluster_lineage
        if after.source_cluster_fingerprint != current.source_cluster_fingerprint:
            expected_cluster_lineage = (
                *expected_cluster_lineage,
                after.source_cluster_fingerprint,
            )
        if after.source_cluster_lineage != expected_cluster_lineage:
            raise ValueError("memory source cluster history is append-only")
        if after.opened_at != current.opened_at or logical_time < current.updated_at:
            raise ValueError("memory candidate chronology is invalid")
        _validate_transition(history, current, payload, logical_time=logical_time)
        if payload.operation == "revise" and payload.revise_kind == "correct":
            _validate_correction_sources(
                current.values.source_bindings,
                after.values.source_bindings,
                facts=facts,
                fact_history=fact_history,
                experiences=experiences,
                experience_history=experience_history,
                threads=threads,
                thread_history=thread_history,
                committed_events=committed_events,
            )
        if payload.operation == "forget":
            _validate_forget_authority(
                payload,
                candidates=candidates,
                facts=facts,
                fact_history=fact_history,
                experiences=experiences,
                experience_history=experience_history,
                threads=threads,
                thread_history=thread_history,
                committed_events=committed_events,
                logical_time=logical_time,
            )
        updated = tuple(
            after if item.candidate_id == after.candidate_id else item
            for item in candidates
        )

    for other in updated:
        if other.candidate_id == after.candidate_id:
            continue
        if set(other.source_cluster_lineage) & set(after.source_cluster_lineage):
            raise ValueError("memory source cluster is already indexed elsewhere")

    transition = MemoryCandidateTransitionProjection(
        transition_id=payload.transition_id,
        candidate_id=after.candidate_id,
        entity_revision=after.entity_revision,
        operation=payload.operation,
        values_before=before.values if before else None,
        values_after=after.values,
        change_id=payload.change_id,
        policy_refs=payload.policy_refs,
        accepted_event_ref=event_id,
        accepted_at=logical_time,
        revise_kind=payload.revise_kind,
        reinforcement_reason=payload.reinforcement_reason,
        rejection_reason=payload.rejection_reason,
        forget_reason=(
            payload.forget_authority.reason if payload.forget_authority else None
        ),
    )
    if any(item.transition_id == transition.transition_id for item in history):
        raise ValueError("memory candidate transition identity already exists")
    return updated, (*history, transition)


def _validate_transition(
    history: tuple[MemoryCandidateTransitionProjection, ...],
    current: MemoryCandidateProjection,
    payload: MemoryCandidateChangedPayload,
    *,
    logical_time: datetime,
) -> None:
    before, after = current.values, payload.candidate_after.values
    operation = payload.operation
    if after.consumed_source_authority_ids[: len(before.consumed_source_authority_ids)] != (
        before.consumed_source_authority_ids
    ):
        raise ValueError("memory consumed source authority lineage is append-only")
    if operation == "accept":
        if before.status != "pending" or after.status != "active":
            raise ValueError("memory accept requires pending to active")
        _require_same_cue(before, after, allow_status=True)
        if (
            after.reviewed_at != logical_time
            or after.retrieval_strength_bp != before.retrieval_strength_bp
        ):
            raise ValueError("memory acceptance cannot rewrite cue strength")
    elif operation == "reject":
        if before.status != "pending" or after.status != "rejected":
            raise ValueError("memory reject requires pending to rejected")
        _require_same_cue(before, after, allow_status=True, allow_strength_zero=True)
        if after.reviewed_at != logical_time or not payload.rejection_reason:
            raise ValueError("memory rejection requires explicit review reason")
    elif operation == "revise":
        expected_kind = (
            {"pending_edit"}
            if before.status == "pending"
            else {"compress", "clarify", "correct"}
            if before.status == "active"
            else set()
        )
        if payload.revise_kind not in expected_kind or after.status != before.status:
            raise ValueError("memory revision kind does not match candidate lifecycle")
        if (
            after.reinforcement_count != before.reinforcement_count
            or after.last_reinforced_at != before.last_reinforced_at
            or after.forgotten_at is not None
        ):
            raise ValueError("memory revision cannot simulate review or reinforcement")
        if before.status == "pending" and after.reviewed_at is not None:
            raise ValueError("pending memory revision cannot simulate review")
        if before.status == "active" and after.reviewed_at != logical_time:
            raise ValueError("active memory revision must record review time")
        if before.status == "active":
            if after.cue_kind != before.cue_kind:
                raise ValueError("active memory revision cannot change cue kind")
            if (
                after.salience != before.salience
                or after.retrieval_strength_bp != before.retrieval_strength_bp
                or after.reinforcement_count != before.reinforcement_count
                or after.future_use_refs != before.future_use_refs
                or after.review_due_at != before.review_due_at
            ):
                raise ValueError("active memory revision cannot simulate reinforcement")
            if payload.revise_kind in {"compress", "clarify"} and (
                after.source_bindings != before.source_bindings
            ):
                raise ValueError("compress/clarify cannot rewrite memory source authority")
            if payload.revise_kind == "correct":
                if after.source_bindings == before.source_bindings:
                    raise ValueError("memory correction requires a source authority change")
        if _PRIVACY_RANK[after.privacy_ceiling] < _PRIVACY_RANK[before.privacy_ceiling]:
            raise ValueError("memory revision cannot loosen privacy")
        if after == before:
            raise ValueError("memory revision cannot be a no-op")
    elif operation == "reinforce":
        if before.status != "active" or after.status != "active":
            raise ValueError("memory reinforcement requires an active candidate")
        old_bindings = before.source_bindings
        if after.source_bindings[: len(old_bindings)] != old_bindings:
            raise ValueError("memory reinforcement sources are append-only")
        new_bindings = after.source_bindings[len(old_bindings) :]
        if not new_bindings or any(
            memory_source_authority_id(item)
            in set(before.consumed_source_authority_ids)
            for item in new_bindings
        ):
            raise ValueError("memory reinforcement requires novel source authority")
        expected_consumed = (
            *before.consumed_source_authority_ids,
            *(memory_source_authority_id(item) for item in new_bindings),
        )
        if (
            after.consumed_source_authority_ids != expected_consumed
            or after.salience
            != _reinforced_salience(
                before.salience,
                reason=payload.reinforcement_reason,
                new_source_count=len(new_bindings),
            )
            or after.retrieval_strength_bp <= before.retrieval_strength_bp
            or after.reinforcement_count != before.reinforcement_count + 1
            or after.last_reinforced_at != logical_time
            or after.reviewed_at != logical_time
            or after.review_due_at != before.review_due_at
            or not set(before.retention_rationales).issubset(
                after.retention_rationales
            )
            or not set(before.future_use_refs).issubset(after.future_use_refs)
            or _PRIVACY_RANK[after.privacy_ceiling]
            < _PRIVACY_RANK[before.privacy_ceiling]
        ):
            raise ValueError("memory reinforcement before/after values are invalid")
        _validate_settlement_policy(payload)
    elif operation == "forget":
        if before.status != "active" or after.status != "forgotten":
            raise ValueError("memory forget requires active to forgotten")
        _require_same_cue(before, after, allow_status=True, allow_strength_zero=True)
        if (
            after.reviewed_at != logical_time
            or after.forgotten_at != logical_time
            or after.reinforcement_count != before.reinforcement_count
            or after.last_reinforced_at != before.last_reinforced_at
        ):
            raise ValueError("memory forget must retain lineage and record terminal time")
        _validate_settlement_policy(payload)
    else:
        raise ValueError("unsupported memory candidate operation")


def _validate_correction_sources(
    before_bindings: tuple[MemorySourceBinding, ...],
    after_bindings: tuple[MemorySourceBinding, ...],
    **resolver_authority,
) -> None:
    """Allow exact-source correction only where prior authority is stale."""

    current_before: dict[tuple[str, str], MemorySourceBinding] = {}
    stale_before: dict[tuple[str, str], MemorySourceBinding] = {}
    for binding in before_bindings:
        key = (binding.source_kind, binding.source_id)
        try:
            _resolve_source(binding, require_current=True, **resolver_authority)
        except ValueError:
            stale_before[key] = binding
        else:
            current_before[key] = binding
    after_by_key = {
        (binding.source_kind, binding.source_id): binding for binding in after_bindings
    }
    if any(after_by_key.get(key) != binding for key, binding in current_before.items()):
        raise ValueError("memory correction cannot delete or replace a current source")
    added_keys = set(after_by_key) - set(current_before)
    if not added_keys.issubset(stale_before):
        raise ValueError("memory correction may add only a stale source successor")
    for key in added_keys:
        if after_by_key[key].source_entity_revision <= stale_before[key].source_entity_revision:
            raise ValueError("memory correction source must be a later exact revision")


def _require_same_cue(
    before,
    after,
    *,
    allow_status: bool,
    allow_strength_zero: bool = False,
) -> None:
    ignored = {"status", "reviewed_at", "forgotten_at"} if allow_status else set()
    if allow_strength_zero:
        ignored.add("retrieval_strength_bp")
    if before.model_dump(exclude=ignored) != after.model_dump(exclude=ignored):
        raise ValueError("memory lifecycle transition cannot rewrite the retrieval cue")


def _validate_settlement_policy(payload: MemoryCandidateChangedPayload) -> None:
    if (
        payload.policy_version != MEMORY_POLICY_VERSION
        or payload.policy_digest != MEMORY_POLICY_DIGEST
    ):
        raise ValueError("memory settlement policy artifact is not installed")


def _reinforced_salience(
    before: MemorySalienceVector,
    *,
    reason,
    new_source_count: int,
) -> MemorySalienceVector:
    reason_field = _REINFORCEMENT_REASON_FIELDS[reason]
    updates = {
        "recurrence_bp": min(
            _SALIENCE_CAP_BP,
            before.recurrence_bp + _RECURRENCE_DELTA_PER_SOURCE_BP * new_source_count,
        ),
    }
    current_reason_value = updates.get(reason_field, getattr(before, reason_field))
    updates[reason_field] = min(
        _SALIENCE_CAP_BP,
        current_reason_value + _RATIONALE_DELTA_PER_SOURCE_BP * new_source_count,
    )
    return before.model_copy(update=updates)


def _validate_forget_authority(
    payload: MemoryCandidateChangedPayload,
    *,
    candidates: tuple[MemoryCandidateProjection, ...],
    facts: tuple[FactProjection, ...],
    fact_history: tuple[FactTransitionProjection, ...],
    experiences: tuple[object, ...],
    experience_history: tuple[ExperienceTransitionProjection, ...],
    threads: tuple[ThreadProjection, ...],
    thread_history: tuple[ThreadTransitionProjection, ...],
    committed_events: tuple[CommittedWorldEventRef, ...],
    logical_time: datetime,
) -> None:
    authority = payload.forget_authority
    if authority is None:
        raise ValueError("memory forget requires decision authority")
    if isinstance(authority, MemoryClockForgetAuthority):
        committed = next(
            (item for item in committed_events if item.event_id == authority.clock_event_ref),
            None,
        )
        latest_clock = next(
            (
                item
                for item in reversed(committed_events)
                if item.event_type == "ClockAdvanced"
            ),
            None,
        )
        due = payload.candidate_before.values.review_due_at
        if (
            committed is None
            or due is None
            or committed != latest_clock
            or committed.event_type != "ClockAdvanced"
            or committed.world_revision != authority.clock_world_revision
            or committed.payload_hash != authority.clock_payload_hash
            or committed.logical_time < due
        ):
            raise ValueError("memory decay does not resolve exact Clock authority")
        if logical_time < due:
            raise ValueError("memory scheduled forget is before frozen review due time")
        return
    if isinstance(authority, MemoryEvidenceForgetAuthority):
        # The shared evidence resolver validates this separate decision evidence
        # in the authority handler.  It is intentionally not a retrieval source.
        return
    if isinstance(authority, MemorySourceInvalidationForgetAuthority):
        bindings = {
            memory_source_authority_id(item): item
            for item in payload.candidate_before.values.source_bindings
        }
        identities = tuple(item.source_authority_id for item in authority.sources)
        if any(identity not in bindings for identity in identities):
            raise ValueError("memory invalidation authority names an unbound source")
        stale: set[str] = set()
        for source, identity in zip(authority.sources, identities, strict=True):
            binding = bindings[identity]
            if (
                binding.source_kind != source.source_kind
                or binding.source_id != source.source_id
                or binding.source_entity_revision != source.source_entity_revision
            ):
                raise ValueError("memory invalidation authority aliases source identity")
            try:
                _resolve_source(
                    binding,
                    facts=facts,
                    fact_history=fact_history,
                    experiences=experiences,
                    experience_history=experience_history,
                    threads=threads,
                    thread_history=thread_history,
                    committed_events=committed_events,
                    require_current=True,
                )
            except ValueError:
                stale.add(identity)
        if stale != set(identities):
            raise ValueError("memory source invalidation is not authoritative")
        return
    if isinstance(authority, MemoryCompressionForgetAuthority):
        target = next(
            (
                item
                for item in candidates
                if item.candidate_id == authority.target_candidate_id
            ),
            None,
        )
        committed = next(
            (item for item in committed_events if item.event_id == authority.target_event_ref),
            None,
        )
        if (
            target is None
            or target.candidate_id == payload.candidate_before.candidate_id
            or target.values.status != "active"
            or target.entity_revision != authority.target_entity_revision
            or target.origin.accepted_event_ref != authority.target_event_ref
            or committed is None
            or committed.world_revision != authority.target_world_revision
            or committed.payload_hash != authority.target_payload_hash
            or target.values.cue_kind != payload.candidate_before.values.cue_kind
            or not set(
                payload.candidate_before.values.consumed_source_authority_ids
            ).issubset(set(target.values.consumed_source_authority_ids))
        ):
            raise ValueError("memory compression target is not exact active authority")
        return
    if not isinstance(authority, MemoryDeliberativeForgetAuthority):
        raise TypeError("unsupported memory forget authority")


def _validate_sources(
    bindings: tuple[MemorySourceBinding, ...],
    *,
    facts: tuple[FactProjection, ...],
    fact_history: tuple[FactTransitionProjection, ...],
    experiences: tuple[object, ...],
    experience_history: tuple[ExperienceTransitionProjection, ...],
    threads: tuple[ThreadProjection, ...],
    thread_history: tuple[ThreadTransitionProjection, ...],
    committed_events: tuple[CommittedWorldEventRef, ...],
    require_current: bool,
) -> tuple[PrivacyClass, ...]:
    return tuple(
        _resolve_source(
            binding,
            facts=facts,
            fact_history=fact_history,
            experiences=experiences,
            experience_history=experience_history,
            threads=threads,
            thread_history=thread_history,
            committed_events=committed_events,
            require_current=require_current,
        )
        for binding in bindings
    )


def _resolve_source(
    binding: MemorySourceBinding,
    *,
    facts: tuple[FactProjection, ...],
    fact_history: tuple[FactTransitionProjection, ...],
    experiences: tuple[object, ...],
    experience_history: tuple[ExperienceTransitionProjection, ...],
    threads: tuple[ThreadProjection, ...],
    thread_history: tuple[ThreadTransitionProjection, ...],
    committed_events: tuple[CommittedWorldEventRef, ...],
    require_current: bool,
) -> PrivacyClass:
    authority = next(
        (
            item
            for item in committed_events
            if item.event_id == binding.authority_event_ref
        ),
        None,
    )
    if (
        authority is None
        or authority.world_revision != binding.authority_world_revision
        or authority.payload_hash != binding.authority_payload_hash
    ):
        raise ValueError("memory source does not resolve exact event authority")
    if binding.source_kind == "fact":
        transition = next(
            (
                item
                for item in fact_history
                if item.fact_id == binding.source_id
                and item.entity_revision == binding.source_entity_revision
                and item.accepted_event_ref == binding.authority_event_ref
            ),
            None,
        )
        current = next(
            (item for item in facts if item.fact_id == binding.source_id), None
        )
        if (
            authority.event_type not in FACT_PAYLOAD_MODELS
            or transition is None
            or _canonical_hash(transition.values_after) != binding.source_values_hash
        ):
            raise ValueError("memory source does not resolve exact Fact authority")
        if require_current and (
            current is None
            or current.entity_revision != binding.source_entity_revision
            or current.values.status != "active"
        ):
            raise ValueError("memory Fact source is not the current active head")
        return transition.values_after.privacy_class
    if binding.source_kind == "experience":
        transition = next(
            (
                item
                for item in experience_history
                if item.experience_id == binding.source_id
                and item.entity_revision == binding.source_entity_revision
                and item.accepted_event_ref == binding.authority_event_ref
            ),
            None,
        )
        current = next(
            (
                item
                for item in experiences
                if isinstance(item, ExperienceProjection)
                and item.experience_id == binding.source_id
            ),
            None,
        )
        if (
            authority.event_type != "ExperienceCommitted"
            or transition is None
            or current is None
            or current.authority_contract_version != "experience.1"
            or _canonical_hash(transition.values_after) != binding.source_values_hash
            or (require_current and current.entity_revision != binding.source_entity_revision)
        ):
            raise ValueError("memory source does not resolve hardened Experience authority")
        return transition.values_after.privacy_class
    transition = next(
        (
            item
            for item in thread_history
            if item.thread_id == binding.source_id
            and item.entity_revision == binding.source_entity_revision
            and item.accepted_event_ref == binding.authority_event_ref
        ),
        None,
    )
    current = next(
        (item for item in threads if item.thread_id == binding.source_id), None
    )
    if (
        not authority.event_type.startswith("Thread")
        or transition is None
        or transition.values_after.status not in TERMINAL_THREAD_STATUSES
        or _canonical_hash(transition.values_after) != binding.source_values_hash
        or current is None
        or (require_current and current.entity_revision != binding.source_entity_revision)
        or (require_current and current.values.status not in TERMINAL_THREAD_STATUSES)
    ):
        raise ValueError("memory source does not resolve exact terminal Thread authority")
    return transition.values_after.privacy_class


def evaluate_memory_retrieval(
    candidates: tuple[MemoryCandidateProjection, ...],
    *,
    facts: tuple[FactProjection, ...],
    fact_history: tuple[FactTransitionProjection, ...],
    experiences: tuple[object, ...],
    experience_history: tuple[ExperienceTransitionProjection, ...],
    threads: tuple[ThreadProjection, ...],
    thread_history: tuple[ThreadTransitionProjection, ...],
    committed_events: tuple[CommittedWorldEventRef, ...],
    viewer_privacy_ceiling: PrivacyClass,
) -> tuple[MemoryRetrievalDecision, ...]:
    decisions: list[MemoryRetrievalDecision] = []
    for candidate in candidates:
        reasons: list[str] = []
        stale_ids: list[str] = []
        if candidate.values.status != "active":
            reasons.append("not_active")
        for binding in candidate.values.source_bindings:
            try:
                _resolve_source(
                    binding,
                    facts=facts,
                    fact_history=fact_history,
                    experiences=experiences,
                    experience_history=experience_history,
                    threads=threads,
                    thread_history=thread_history,
                    committed_events=committed_events,
                    require_current=True,
                )
            except ValueError:
                stale_ids.append(binding.source_id)
        if stale_ids:
            reasons.append("stale_source")
        # Ordinary retrieval never returns withheld memory. A future audit API
        # must resolve a real ledger capability instead of trusting a boolean.
        if candidate.values.privacy_ceiling == "withhold" or _PRIVACY_RANK[
            candidate.values.privacy_ceiling
        ] > _PRIVACY_RANK[
            viewer_privacy_ceiling
        ]:
            reasons.append("privacy_ceiling")
        eligible = not reasons
        decisions.append(
            MemoryRetrievalDecision(
                candidate_id=candidate.candidate_id,
                eligible=eligible,
                source_ids=(
                    tuple(item.source_id for item in candidate.values.source_bindings)
                    if eligible
                    else ()
                ),
                stale_source_ids=tuple(stale_ids),
                suppression_reasons=tuple(reasons),
                review_required=bool(stale_ids),
            )
        )
    return tuple(decisions)


def _canonical_hash(value) -> str:
    encoded = json.dumps(
        value.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()

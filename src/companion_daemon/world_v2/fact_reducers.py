"""Pure replay-safe fact lifecycle; no behavioral or retrieval side effects."""

from __future__ import annotations

from datetime import datetime
from types import MappingProxyType

from .fact_events import FactChangedPayload
from .schemas import (
    FactProjection,
    FactTransitionProjection,
    MessageObservationRef,
    OperatorObservationRef,
)


_PRIVACY_RANK = {"public": 0, "shareable": 1, "personal": 2, "private": 3, "withhold": 4}
# Catalog version 2 (2026-07-20).  Version 1 was the six-predicate identity
# baseline.  ``reduce_fact`` validates every replayed committed Fact against
# this mapping, so the catalog is strictly append-only: an entry, once
# released, may never be removed or change cardinality without breaking
# replay of history that used it.  Version 2 adds the everyday-life predicates
# (work/study, schedule, recent circumstances, people, health, routine,
# interests, possessions, residence) because a four-day production world with
# 63 user message batches produced zero committed facts: ordinary personal
# statements had no installed landing slot.
#
# Cardinality semantics (mirrors the version-1 entries):
#   single - one active value per (subject, predicate) slot; a later different
#            value needs a correct/withdraw transition, a conflicting second
#            commit is rejected by ``_reject_active_conflict``.
#   set    - multiple concurrently active values; only an identical value
#            (same value_hash) is rejected as a duplicate.
INSTALLED_FACT_PREDICATE_CATALOG_VERSION = "fact-predicate-catalog.2"
INSTALLED_FACT_PREDICATE_CARDINALITY = MappingProxyType(
    {
        # -- catalog version 1 (frozen; replay-load-bearing) ------------------
        "location.current": "single",
        "profile.display_name": "single",
        "profile.timezone": "single",
        "preference.likes": "set",
        "preference.dislikes": "set",
        "relationship.affiliation": "set",
        # -- catalog version 2 additions (append-only) ------------------------
        # Work / study identity: one current occupation and one current
        # study stage per subject; changes are corrections, not accumulation.
        "profile.occupation": "single",
        "profile.education": "single",
        # Residence is distinct from the transient ``location.current``.
        "location.home": "single",
        "location.hometown": "single",
        # Dated or scheduled commitments (a contest tomorrow, a train on the
        # 21st).  Several can be pending at once, so they accumulate.
        "schedule.commitment": "set",
        # Recent life circumstances ("最近在备赛", "被快递员吵醒").  Overlapping
        # circumstances coexist; retrieval ranks recency, authority does not.
        "situation.recent": "set",
        # What the user is doing right now ("在写代码").  Deliberately a set:
        # the commit-only trigger runtime cannot correct a single slot, and
        # successive activities must not conflict-poison the lane.
        "activity.current": "set",
        # Named people in the user's life (family, friends, colleagues).
        "relationship.person": "set",
        # Health facts: conditions, allergies, injuries.
        "health.condition": "set",
        # Sleep/wake and other recurring habits ("一般两点睡").
        "routine.habit": "set",
        # Hobbies and recurring activities ("打网球").  ``preference.likes``
        # stays for taste-style likes; this slot is for practiced activities.
        "interest.activity": "set",
        # Possessions incl. devices and pets ("我有只猫").
        "possession.item": "set",
    }
)


def reduce_fact(
    facts: tuple[FactProjection, ...],
    history: tuple[FactTransitionProjection, ...],
    payload: FactChangedPayload,
    *, event_type: str, logical_time: datetime,
    message_observations: tuple[MessageObservationRef, ...],
    operator_observations: tuple[OperatorObservationRef, ...],
) -> tuple[tuple[FactProjection, ...], tuple[FactTransitionProjection, ...]]:
    expected = {
        "FactCommitted": "commit", "FactCommittedV2": "commit", "FactCorrected": "correct",
        "FactWithdrawn": "withdraw", "FactCorrectionCompensated": "compensate",
    }[event_type]
    if payload.operation != expected:
        raise ValueError("fact event type does not match operation")
    after = payload.fact_after
    installed_cardinality = INSTALLED_FACT_PREDICATE_CARDINALITY.get(
        after.values.predicate_code
    )
    if installed_cardinality is None:
        raise ValueError("fact predicate has no installed cardinality authority")
    if after.values.cardinality != installed_cardinality:
        raise ValueError("fact cardinality conflicts with installed predicate authority")
    current = next((item for item in facts if item.fact_id == after.fact_id), None)
    if payload.operation == "commit":
        if current is not None:
            raise ValueError("fact identity already exists")
        if after.values.status != "active":
            raise ValueError("fact commit must create active authority")
        if after.committed_at != logical_time or after.updated_at != logical_time:
            raise ValueError("fact commit timestamps must match logical time")
        _reject_active_conflict(facts, after)
        binding = after.values.assertion_binding
        if not any(
            item.ref_id == binding.source_ref and item.evidence_type == binding.source_kind
            for item in after.values.anchor_evidence_refs
        ):
            raise ValueError("fact commit assertion source must be canonical anchor evidence")
        _validate_assertion(after, message_observations, operator_observations)
        _validate_privacy(after)
        updated = (*facts, after)
    else:
        if current is None or current != payload.fact_before:
            raise ValueError("fact before image does not match current authority")
        if current.entity_revision != payload.expected_entity_revision:
            raise ValueError("fact entity revision compare-and-swap failed")
        if current.values.status != "active":
            raise ValueError("withdrawn fact cannot reopen or transition")
        if (
            after.committed_at != current.committed_at
            or after.updated_at != logical_time
            or after.origin.policy_refs != current.origin.policy_refs
        ):
            raise ValueError("fact transition changed immutable origin")
        if payload.operation == "compensate":
            _validate_compensation(history, current, payload)
        else:
            _validate_forward(current, after, operation=payload.operation)
        _validate_assertion(after, message_observations, operator_observations)
        _validate_privacy(after)
        if after.values.status == "active":
            _reject_active_conflict(facts, after, ignore_id=current.fact_id)
        updated = tuple(after if item.fact_id == after.fact_id else item for item in facts)
    transition = FactTransitionProjection(
        transition_id=payload.transition_id, fact_id=after.fact_id,
        entity_revision=after.entity_revision, operation=payload.operation,
        values_before=payload.fact_before.values if payload.fact_before else None,
        values_after=after.values,
        semantic_fingerprint_after=after.semantic_fingerprint,
        change_id=payload.change_id, policy_refs=payload.policy_refs,
        accepted_event_ref=after.origin.accepted_event_ref, accepted_at=logical_time,
        compensates_transition_id=payload.compensates_transition_id,
    )
    if any(item.transition_id == transition.transition_id for item in history):
        raise ValueError("fact transition identity already exists")
    return updated, (*history, transition)


def _reject_active_conflict(
    facts: tuple[FactProjection, ...], candidate: FactProjection, *, ignore_id: str = ""
) -> None:
    for item in facts:
        if item.fact_id == ignore_id:
            continue
        if (
            item.values.conflict_key == candidate.values.conflict_key
            and item.values.cardinality != candidate.values.cardinality
        ):
            raise ValueError("fact slot cardinality cannot be re-declared")
        if item.values.status != "active":
            continue
        if item.semantic_fingerprint == candidate.semantic_fingerprint:
            raise ValueError("active fact semantic fingerprint already exists")
        if (
            item.values.conflict_key == candidate.values.conflict_key
            and item.values.cardinality == candidate.values.cardinality
            and item.values.value_hash == candidate.values.value_hash
        ):
            raise ValueError("active fact content identity already exists")
        if (
            candidate.values.cardinality == "single"
            and item.values.cardinality == "single"
            and item.values.conflict_key == candidate.values.conflict_key
        ):
            raise ValueError("active single-valued fact conflict key already exists")


def _validate_forward(current: FactProjection, after: FactProjection, *, operation: str) -> None:
    before, values = current.values, after.values
    immutable = ("subject_ref", "predicate_code", "cardinality", "conflict_key", "anchor_evidence_refs")
    if any(getattr(before, name) != getattr(values, name) for name in immutable):
        raise ValueError("fact transition changed immutable semantic slot")
    if values.source_evidence_refs[: len(before.source_evidence_refs)] != before.source_evidence_refs:
        raise ValueError("fact source evidence is append-only")
    if _PRIVACY_RANK[values.privacy_class] < _PRIVACY_RANK[before.privacy_class]:
        raise ValueError("fact privacy cannot be loosened")
    new_refs = values.source_evidence_refs[len(before.source_evidence_refs):]
    head_refs = tuple(item for item in new_refs if item.evidence_type == "committed_fact")
    if len(head_refs) != 1 or head_refs[0].ref_id != current.origin.accepted_event_ref:
        raise ValueError("fact transition requires exact prior committed-fact evidence")
    external = tuple(item for item in new_refs if item.evidence_type != "committed_fact")
    if not external:
        raise ValueError("fact transition requires newly observed authority evidence")
    if operation == "correct":
        if values.status != "active" or values.value_hash == before.value_hash:
            raise ValueError("fact correction must change the active value")
        external_refs = {item.ref_id for item in external}
        if (
            values.assertion_binding.source_ref not in external_refs
            or values.assertion_binding == before.assertion_binding
            or values.assertion_binding.content_payload_hash
            == before.assertion_binding.content_payload_hash
        ):
            raise ValueError("fact correction value must bind newly observed assertion evidence")
    elif operation == "withdraw":
        frozen = (
            "assertion_binding",
            "value_ref",
            "value_hash",
            "confidence_bp",
            "anchor_evidence_refs",
        )
        if values.status != "withdrawn" or any(
            getattr(values, field) != getattr(before, field) for field in frozen
        ):
            raise ValueError("fact withdrawal must freeze claim content and close authority")
        if values.withdrawal_evidence_ref not in {item.ref_id for item in external}:
            raise ValueError("fact withdrawal must cite newly observed evidence")


def _validate_compensation(
    history: tuple[FactTransitionProjection, ...],
    current: FactProjection,
    payload: FactChangedPayload,
) -> None:
    lineage = tuple(item for item in history if item.fact_id == current.fact_id)
    if not lineage or lineage[-1].transition_id != payload.compensates_transition_id:
        raise ValueError("fact compensation must target the latest transition")
    target = lineage[-1]
    if target.operation != "correct" or target.values_before is None:
        raise ValueError("fact compensation can only restore the latest correction")
    if payload.fact_after.values != target.values_before:
        raise ValueError("fact compensation must exactly restore correction before image")
    if _PRIVACY_RANK[payload.fact_after.values.privacy_class] < _PRIVACY_RANK[current.values.privacy_class]:
        raise ValueError("fact compensation cannot loosen privacy")


def _validate_assertion(
    fact: FactProjection,
    messages: tuple[MessageObservationRef, ...],
    operators: tuple[OperatorObservationRef, ...],
) -> None:
    binding = fact.values.assertion_binding
    if binding.asserted_subject_ref != fact.values.subject_ref:
        raise ValueError("fact assertion subject does not match fact subject")
    source = next(
        (
            item for item in fact.values.source_evidence_refs
            if item.ref_id == binding.source_ref and item.evidence_type == binding.source_kind
        ), None,
    )
    if source is None or source.claim_purpose != "current_fact":
        raise ValueError("fact assertion binding requires current-fact source evidence")
    if binding.source_kind == "observed_message":
        observed = next((item for item in messages if item.observation_id == binding.source_ref), None)
        if (
            observed is None
            or observed.actor != binding.actor_ref
            or observed.channel != binding.channel
            or observed.payload_ref != binding.payload_ref
            or observed.content_payload_hash != binding.content_payload_hash
        ):
            raise ValueError("fact assertion does not match observed message provenance")
    else:
        observed = next((item for item in operators if item.observation_id == binding.source_ref), None)
        if observed is None or observed.observation_hash != binding.content_payload_hash:
            raise ValueError("fact assertion does not match operator provenance")


def _validate_privacy(fact: FactProjection) -> None:
    source_minimum = {
        "observed_message": 2,
        "operator_observation": 3,
        "committed_fact": 2,
    }
    purpose_minimum = {
        "current_fact": 2,
        "past_experience": 2,
        "future_plan": 2,
        "private_hypothesis": 3,
        "action_authorization": 3,
        "conversation_continuity": 2,
    }
    required = max(
        max(source_minimum.get(item.evidence_type, 4), purpose_minimum[item.claim_purpose])
        for item in fact.values.source_evidence_refs
    )
    if _PRIVACY_RANK[fact.values.privacy_class] < required:
        raise ValueError("fact evidence/privacy matrix rejects broad visibility")

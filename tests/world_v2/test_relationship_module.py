from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from companion_daemon.world_v2.relationship_events import (
    BoundaryChangedPayload,
    RelationshipSignalAcceptedPayload,
    RelationshipSlowVariableAdjustedPayload,
    relationship_mutation_hash,
)
from companion_daemon.world_v2.relationship_reducers import (
    RELATIONSHIP_POLICY_DIGEST,
    accept_relationship_signal,
    adjust_relationship_slow_variables,
    change_boundary,
)
from companion_daemon.world_v2.schemas import (
    BoundaryProjection,
    EvidenceRef,
    RelationshipAdjustmentProjection,
    RelationshipBoundaryOrigin,
    RelationshipSignalOrigin,
    RelationshipSignalProjection,
    RelationshipHysteresisProjection,
    RelationshipStateProjection,
    RelationshipVariableDeltas,
    RelationshipVariablesProjection,
    relationship_signal_fingerprint,
)


NOW = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)
NAIVE_NOW = datetime(2026, 7, 14, 12, 0)


def evidence(ref_id: str = "operator:relationship:1") -> EvidenceRef:
    return EvidenceRef(
        ref_id=ref_id,
        evidence_type="operator_observation",
        claim_purpose="private_hypothesis",
        immutable_hash="1" * 64,
    )


def authorized(model_type, **values):
    raw = {
        "change_id": values.pop("change_id"),
        "transition_id": values.pop("transition_id"),
        "expected_entity_revision": values.pop("expected_entity_revision"),
        "evidence_refs": tuple(values.pop("evidence_refs", (evidence(),))),
        "policy_refs": tuple(values.pop("policy_refs")),
        "acceptance_id": values.pop("acceptance_id", "acceptance:relationship:1"),
        "proposal_id": values.pop("proposal_id", "proposal:relationship:1"),
        "evaluated_world_revision": values.pop("evaluated_world_revision", 3),
        "accepted_change_hash": "0" * 64,
        **values,
    }
    if model_type is RelationshipSlowVariableAdjustedPayload:
        raw.setdefault("compensates_adjustment_id", None)
        raw.setdefault("commitment_refs", ())
    raw["accepted_change_hash"] = relationship_mutation_hash(raw)
    return model_type.model_validate(raw)


def signal(signal_id: str, *, code: str, contradiction_group_ref: str) -> RelationshipSignalProjection:
    refs = (evidence(f"operator:{signal_id}"),)
    policy_refs = ("policy:relationship-signal-v1",)
    return RelationshipSignalProjection(
        signal_id=signal_id,
        semantic_fingerprint=relationship_signal_fingerprint(
            subject_ref="user:geoff",
            signal_code=code,
            evidence_refs=refs,
            policy_refs=policy_refs,
        ),
        entity_revision=1,
        subject_ref="user:geoff",
        signal_code=code,
        confidence_bp=8_000,
        persistence="durable",
        contradiction_group_ref=contradiction_group_ref,
        rationale_code="settled_interaction_signal",
        evidence_refs=refs,
        origin=RelationshipSignalOrigin(
            change_id=f"change:{signal_id}",
            transition_id=f"transition:{signal_id}",
            policy_refs=policy_refs,
            accepted_event_ref=f"event:{signal_id}",
        ),
        accepted_at=NOW,
    )


def adjustment_payload(
    source: RelationshipSignalProjection,
    *,
    adjustment_id: str,
    expected_revision: int,
    before: RelationshipVariablesProjection,
    after: RelationshipVariablesProjection,
    accepted: RelationshipVariableDeltas,
    stage_before: str = "stranger",
    stage_after: str = "stranger",
    hysteresis_before: RelationshipHysteresisProjection | None = None,
    hysteresis_after: RelationshipHysteresisProjection | None = None,
    adjusted_at: datetime = NOW,
):
    return authorized(
        RelationshipSlowVariableAdjustedPayload,
        change_id=f"change:{adjustment_id}",
        transition_id=f"transition:{adjustment_id}",
        expected_entity_revision=expected_revision,
        policy_refs=("policy:relationship-v1",),
        acceptance_id=f"acceptance:{adjustment_id}",
        proposal_id=f"proposal:{adjustment_id}",
        relationship_id="relationship:user:geoff",
        subject_ref="user:geoff",
        adjustment_id=adjustment_id,
        operation="adjust",
        signal_refs=(source.signal_id,),
        proposed_deltas=accepted,
        accepted_deltas=accepted,
        variables_before=before,
        variables_after=after,
        stage_before=stage_before,
        stage_after=stage_after,
        hysteresis_before=hysteresis_before or RelationshipHysteresisProjection(),
        hysteresis_after=hysteresis_after or RelationshipHysteresisProjection(),
        confidence_bp=8_000,
        persistence="durable",
        contradiction_group_ref=source.contradiction_group_ref,
        rationale_code=source.rationale_code,
        policy_version="relationship-policy.1",
        policy_digest=RELATIONSHIP_POLICY_DIGEST,
        adjusted_at=adjusted_at,
    )


def test_relationship_signals_accumulate_inside_a_contradiction_group() -> None:
    first = signal("signal:care", code="care_observed", contradiction_group_ref="group:care")
    second = signal(
        "signal:withdrawal",
        code="withdrawal_observed",
        contradiction_group_ref="group:care",
    )
    first_payload = authorized(
        RelationshipSignalAcceptedPayload,
        change_id=first.origin.change_id,
        transition_id=first.origin.transition_id,
        expected_entity_revision=0,
        evidence_refs=first.evidence_refs,
        policy_refs=first.origin.policy_refs,
        signal=first,
    )
    second_payload = authorized(
        RelationshipSignalAcceptedPayload,
        change_id=second.origin.change_id,
        transition_id=second.origin.transition_id,
        expected_entity_revision=0,
        evidence_refs=second.evidence_refs,
        policy_refs=second.origin.policy_refs,
        signal=second,
    )

    accepted = accept_relationship_signal((), first_payload, logical_time=NOW)
    accepted = accept_relationship_signal(accepted, second_payload, logical_time=NOW)

    assert accepted == (first, second)
    assert {item.contradiction_group_ref for item in accepted} == {"group:care"}


def test_adjustment_clips_only_at_acceptance_and_stage_moves_one_hysteresis_step() -> None:
    source = signal("signal:reliable", code="reliability_observed", contradiction_group_ref="group:1")
    proposed = RelationshipVariableDeltas(
        trust_bp=900,
        closeness_bp=900,
        respect_bp=900,
        reliability_bp=900,
        mutuality_bp=900,
        repair_confidence_bp=900,
    )
    accepted = RelationshipVariableDeltas(
        trust_bp=500,
        closeness_bp=500,
        respect_bp=500,
        reliability_bp=500,
        mutuality_bp=500,
        repair_confidence_bp=500,
    )
    before = RelationshipVariablesProjection(
        trust_bp=1_900,
        closeness_bp=1_900,
        respect_bp=1_900,
        reliability_bp=1_900,
        mutuality_bp=1_900,
        repair_confidence_bp=1_900,
    )
    after = RelationshipVariablesProjection(
        trust_bp=2_400,
        closeness_bp=2_400,
        respect_bp=2_400,
        reliability_bp=2_400,
        mutuality_bp=2_400,
        repair_confidence_bp=2_400,
    )
    payload = authorized(
        RelationshipSlowVariableAdjustedPayload,
        change_id="change:relationship:1",
        transition_id="transition:relationship:1",
        expected_entity_revision=1,
        policy_refs=("policy:relationship-v1",),
        relationship_id="relationship:user:geoff",
        subject_ref="user:geoff",
        adjustment_id="relationship-adjustment:1",
        operation="adjust",
        signal_refs=(source.signal_id,),
        proposed_deltas=proposed,
        accepted_deltas=accepted,
        variables_before=before,
        variables_after=after,
        stage_before="stranger",
        stage_after="stranger",
        hysteresis_before=RelationshipHysteresisProjection(),
        hysteresis_after=RelationshipHysteresisProjection(
            candidate_stage="acquaintance",
            direction="promote",
            candidate_since=NOW,
            confirming_adjustment_count=1,
        ),
        confidence_bp=8_000,
        persistence="durable",
        contradiction_group_ref="group:1",
        rationale_code="reliability_observed",
        policy_version="relationship-policy.1",
        policy_digest=RELATIONSHIP_POLICY_DIGEST,
        adjusted_at=NOW,
    )

    existing = RelationshipStateProjection(
        relationship_id="relationship:user:geoff",
        subject_ref="user:geoff",
        entity_revision=1,
        variables=before,
        policy_digest=RELATIONSHIP_POLICY_DIGEST,
    )
    states, history = adjust_relationship_slow_variables(
        (existing,), (), (source,), payload, logical_time=NOW
    )

    assert states[0].entity_revision == 2
    assert states[0].variables == after
    assert states[0].stage == "stranger"
    assert states[0].hysteresis.candidate_stage == "acquaintance"
    assert history[0].proposed_deltas == proposed
    assert history[0].accepted_deltas == accepted

    confirming_source = signal(
        "signal:reliable-again",
        code="reliability_observed_again",
        contradiction_group_ref="group:1",
    )
    confirming_payload = authorized(
        RelationshipSlowVariableAdjustedPayload,
        change_id="change:relationship:2",
        transition_id="transition:relationship:2",
        expected_entity_revision=2,
        policy_refs=("policy:relationship-v1",),
        acceptance_id="acceptance:relationship:2",
        proposal_id="proposal:relationship:2",
        relationship_id="relationship:user:geoff",
        subject_ref="user:geoff",
        adjustment_id="relationship-adjustment:2",
        operation="adjust",
        signal_refs=(confirming_source.signal_id,),
        proposed_deltas=RelationshipVariableDeltas(
            trust_bp=1,
            closeness_bp=1,
            respect_bp=1,
            reliability_bp=1,
            mutuality_bp=1,
            repair_confidence_bp=1,
        ),
        accepted_deltas=RelationshipVariableDeltas(
            trust_bp=1,
            closeness_bp=1,
            respect_bp=1,
            reliability_bp=1,
            mutuality_bp=1,
            repair_confidence_bp=1,
        ),
        variables_before=after,
        variables_after=RelationshipVariablesProjection(
            trust_bp=2_401,
            closeness_bp=2_401,
            respect_bp=2_401,
            reliability_bp=2_401,
            mutuality_bp=2_401,
            repair_confidence_bp=2_401,
        ),
        stage_before="stranger",
        stage_after="acquaintance",
        hysteresis_before=states[0].hysteresis,
        hysteresis_after=RelationshipHysteresisProjection(),
        confidence_bp=8_000,
        persistence="durable",
        contradiction_group_ref="group:1",
        rationale_code="reliability_observed_again",
        policy_version="relationship-policy.1",
        policy_digest=RELATIONSHIP_POLICY_DIGEST,
        adjusted_at=NOW + timedelta(days=1),
    )
    promoted_states, promoted_history = adjust_relationship_slow_variables(
        states,
        history,
        (source, confirming_source),
        confirming_payload,
        logical_time=NOW + timedelta(days=1),
    )
    assert promoted_states[0].stage == "acquaintance"
    assert promoted_states[0].hysteresis == RelationshipHysteresisProjection()
    assert len(promoted_history) == 2

    with pytest.raises(ValueError, match="already exists"):
        adjust_relationship_slow_variables(states, history, (source,), payload, logical_time=NOW)


def test_compensation_is_an_inverse_event_and_preserves_signal_history() -> None:
    source = signal("signal:repair", code="repair_observed", contradiction_group_ref="group:repair")
    before = RelationshipVariablesProjection()
    after = RelationshipVariablesProjection(trust_bp=300)
    original_payload = authorized(
        RelationshipSlowVariableAdjustedPayload,
        change_id="change:relationship:original",
        transition_id="transition:relationship:original",
        expected_entity_revision=0,
        policy_refs=("policy:relationship-v1",),
        relationship_id="relationship:user:geoff",
        subject_ref="user:geoff",
        adjustment_id="relationship-adjustment:original",
        operation="adjust",
        signal_refs=(source.signal_id,),
        proposed_deltas=RelationshipVariableDeltas(trust_bp=400),
        accepted_deltas=RelationshipVariableDeltas(trust_bp=300),
        variables_before=before,
        variables_after=after,
        stage_before="stranger",
        stage_after="stranger",
        hysteresis_before=RelationshipHysteresisProjection(),
        hysteresis_after=RelationshipHysteresisProjection(),
        confidence_bp=7_000,
        persistence="durable",
        contradiction_group_ref="group:repair",
        rationale_code="repair_observed",
        policy_version="relationship-policy.1",
        policy_digest=RELATIONSHIP_POLICY_DIGEST,
        adjusted_at=NOW,
    )
    states, history = adjust_relationship_slow_variables(
        (), (), (source,), original_payload, logical_time=NOW
    )
    compensation = authorized(
        RelationshipSlowVariableAdjustedPayload,
        change_id="change:relationship:compensation",
        transition_id="transition:relationship:compensation",
        expected_entity_revision=1,
        policy_refs=("policy:relationship-v1",),
        relationship_id="relationship:user:geoff",
        subject_ref="user:geoff",
        adjustment_id="relationship-adjustment:compensation",
        operation="compensate",
        signal_refs=(source.signal_id,),
        proposed_deltas=RelationshipVariableDeltas(trust_bp=-300),
        accepted_deltas=RelationshipVariableDeltas(trust_bp=-300),
        variables_before=after,
        variables_after=before,
        stage_before="stranger",
        stage_after="stranger",
        hysteresis_before=RelationshipHysteresisProjection(),
        hysteresis_after=RelationshipHysteresisProjection(),
        confidence_bp=10_000,
        persistence="durable",
        contradiction_group_ref="group:repair",
        rationale_code="correction",
        policy_version="relationship-policy.1",
        policy_digest=RELATIONSHIP_POLICY_DIGEST,
        adjusted_at=NOW,
        compensates_adjustment_id="relationship-adjustment:original",
    )

    compensated_states, compensated_history = adjust_relationship_slow_variables(
        states, history, (source,), compensation, logical_time=NOW
    )

    assert compensated_states[0].variables.trust_bp == 0
    assert [item.adjustment_id for item in compensated_history] == [
        "relationship-adjustment:original",
        "relationship-adjustment:compensation",
    ]
    assert source.contradiction_group_ref == "group:repair"


def test_compensation_inverts_effective_clamped_delta() -> None:
    source = signal("signal:clamped-repair", code="repair", contradiction_group_ref="group:clamp")
    before = RelationshipVariablesProjection(trust_bp=9_900)
    after = RelationshipVariablesProjection(trust_bp=10_000)
    original = adjustment_payload(
        source,
        adjustment_id="adjustment:clamped-original",
        expected_revision=1,
        before=before,
        after=after,
        accepted=RelationshipVariableDeltas(trust_bp=300),
    )
    existing = RelationshipStateProjection(
        relationship_id="relationship:user:geoff",
        subject_ref="user:geoff",
        entity_revision=1,
        variables=before,
        policy_digest=RELATIONSHIP_POLICY_DIGEST,
    )
    states, history = adjust_relationship_slow_variables(
        (existing,), (), (source,), original, logical_time=NOW
    )
    compensation = authorized(
        RelationshipSlowVariableAdjustedPayload,
        change_id="change:clamped-compensation",
        transition_id="transition:clamped-compensation",
        expected_entity_revision=2,
        policy_refs=("policy:relationship-v1",),
        relationship_id="relationship:user:geoff",
        subject_ref="user:geoff",
        adjustment_id="adjustment:clamped-compensation",
        operation="compensate",
        signal_refs=(source.signal_id,),
        proposed_deltas=RelationshipVariableDeltas(trust_bp=-100),
        accepted_deltas=RelationshipVariableDeltas(trust_bp=-100),
        variables_before=after,
        variables_after=before,
        stage_before="stranger",
        stage_after="stranger",
        hysteresis_before=RelationshipHysteresisProjection(),
        hysteresis_after=RelationshipHysteresisProjection(),
        confidence_bp=10_000,
        persistence="durable",
        contradiction_group_ref="group:clamp",
        rationale_code="correction",
        policy_version="relationship-policy.1",
        policy_digest=RELATIONSHIP_POLICY_DIGEST,
        adjusted_at=NOW,
        compensates_adjustment_id="adjustment:clamped-original",
    )
    compensated, _ = adjust_relationship_slow_variables(
        states, history, (source,), compensation, logical_time=NOW
    )
    assert compensated[0].variables == before


def test_adjustment_rejects_stale_revision_reused_signal_and_clamp_noop() -> None:
    source = signal("signal:once", code="once", contradiction_group_ref="group:once")
    zero = RelationshipVariablesProjection()
    one_hundred = RelationshipVariablesProjection(trust_bp=100)
    first = adjustment_payload(
        source,
        adjustment_id="adjustment:once",
        expected_revision=0,
        before=zero,
        after=one_hundred,
        accepted=RelationshipVariableDeltas(trust_bp=100),
    )
    states, history = adjust_relationship_slow_variables(
        (), (), (source,), first, logical_time=NOW
    )

    stale_source = signal("signal:stale", code="stale", contradiction_group_ref="group:once")
    stale = adjustment_payload(
        stale_source,
        adjustment_id="adjustment:stale",
        expected_revision=0,
        before=one_hundred,
        after=RelationshipVariablesProjection(trust_bp=200),
        accepted=RelationshipVariableDeltas(trust_bp=100),
    )
    with pytest.raises(ValueError, match="stale"):
        adjust_relationship_slow_variables(
            states, history, (source, stale_source), stale, logical_time=NOW
        )

    reused = adjustment_payload(
        source,
        adjustment_id="adjustment:reused",
        expected_revision=1,
        before=one_hundred,
        after=RelationshipVariablesProjection(trust_bp=200),
        accepted=RelationshipVariableDeltas(trust_bp=100),
    )
    with pytest.raises(ValueError, match="signals.*unconsumed"):
        adjust_relationship_slow_variables(states, history, (source,), reused, logical_time=NOW)

    fresh = signal("signal:fresh", code="fresh", contradiction_group_ref="group:once")
    mixed = adjustment_payload(
        fresh,
        adjustment_id="adjustment:mixed-lineage",
        expected_revision=1,
        before=one_hundred,
        after=RelationshipVariablesProjection(trust_bp=200),
        accepted=RelationshipVariableDeltas(trust_bp=100),
    ).model_copy(update={"signal_refs": (source.signal_id, fresh.signal_id)})
    with pytest.raises(ValueError, match="all signals.*unconsumed"):
        adjust_relationship_slow_variables(
            states, history, (source, fresh), mixed, logical_time=NOW
        )

    capped_source = signal("signal:capped", code="capped", contradiction_group_ref="group:capped")
    capped = RelationshipVariablesProjection(trust_bp=10_000)
    capped_state = RelationshipStateProjection(
        relationship_id="relationship:user:geoff",
        subject_ref="user:geoff",
        entity_revision=1,
        variables=capped,
        policy_digest=RELATIONSHIP_POLICY_DIGEST,
    )
    noop = adjustment_payload(
        capped_source,
        adjustment_id="adjustment:clamp-noop",
        expected_revision=1,
        before=capped,
        after=capped,
        accepted=RelationshipVariableDeltas(trust_bp=300),
    )
    with pytest.raises(ValueError, match="semantic no-op"):
        adjust_relationship_slow_variables(
            (capped_state,), (), (capped_source,), noop, logical_time=NOW
        )


def test_hysteresis_requires_distinct_confirmations_and_dwell_and_never_skips_stage() -> None:
    first_signal = signal("signal:h1", code="h1", contradiction_group_ref="group:h")
    second_signal = signal("signal:h2", code="h2", contradiction_group_ref="group:h")
    third_signal = signal("signal:h3", code="h3", contradiction_group_ref="group:h")
    start = RelationshipVariablesProjection(
        trust_bp=1_900,
        closeness_bp=1_900,
        respect_bp=1_900,
        reliability_bp=1_900,
        mutuality_bp=1_900,
        repair_confidence_bp=1_900,
    )
    high = RelationshipVariablesProjection(
        trust_bp=8_000,
        closeness_bp=8_000,
        respect_bp=8_000,
        reliability_bp=8_000,
        mutuality_bp=8_000,
        repair_confidence_bp=8_000,
    )
    candidate = RelationshipHysteresisProjection(
        candidate_stage="acquaintance",
        direction="promote",
        candidate_since=NOW,
        confirming_adjustment_count=1,
    )
    first = adjustment_payload(
        first_signal,
        adjustment_id="adjustment:h1",
        expected_revision=1,
        before=start,
        after=high,
        accepted=RelationshipVariableDeltas(
            trust_bp=500,
            closeness_bp=500,
            respect_bp=500,
            reliability_bp=500,
            mutuality_bp=500,
            repair_confidence_bp=500,
        ),
        hysteresis_after=candidate,
    )
    # The accepted deltas are capped, so use a high-valued state whose before matches
    # the effective update while preserving a score above every later-stage threshold.
    high_before = RelationshipVariablesProjection(
        trust_bp=7_500,
        closeness_bp=7_500,
        respect_bp=7_500,
        reliability_bp=7_500,
        mutuality_bp=7_500,
        repair_confidence_bp=7_500,
    )
    initial_state = RelationshipStateProjection(
        relationship_id="relationship:user:geoff",
        subject_ref="user:geoff",
        entity_revision=1,
        variables=high_before,
        policy_digest=RELATIONSHIP_POLICY_DIGEST,
    )
    first = first.model_copy(
        update={"variables_before": high_before, "variables_after": high}
    )
    states, history = adjust_relationship_slow_variables(
        (initial_state,), (), (first_signal,), first, logical_time=NOW
    )
    assert states[0].stage == "stranger"
    assert states[0].hysteresis == candidate

    same_day_after = high.model_copy(update={"trust_bp": 8_001})
    same_day_hysteresis = candidate.model_copy(update={"confirming_adjustment_count": 2})
    same_day = adjustment_payload(
        second_signal,
        adjustment_id="adjustment:h2",
        expected_revision=2,
        before=high,
        after=same_day_after,
        accepted=RelationshipVariableDeltas(trust_bp=1),
        hysteresis_before=candidate,
        hysteresis_after=same_day_hysteresis,
        adjusted_at=NOW + timedelta(hours=1),
    )
    states, history = adjust_relationship_slow_variables(
        states,
        history,
        (first_signal, second_signal),
        same_day,
        logical_time=NOW + timedelta(hours=1),
    )
    assert states[0].stage == "stranger"

    next_day_after = same_day_after.model_copy(update={"trust_bp": 8_002})
    next_day = adjustment_payload(
        third_signal,
        adjustment_id="adjustment:h3",
        expected_revision=3,
        before=same_day_after,
        after=next_day_after,
        accepted=RelationshipVariableDeltas(trust_bp=1),
        stage_after="acquaintance",
        hysteresis_before=same_day_hysteresis,
        adjusted_at=NOW + timedelta(days=1),
    )
    states, _ = adjust_relationship_slow_variables(
        states,
        history,
        (first_signal, second_signal, third_signal),
        next_day,
        logical_time=NOW + timedelta(days=1),
    )
    assert states[0].stage == "acquaintance"


def test_compensation_restores_hysteresis_without_counting_as_confirmation() -> None:
    source = signal("signal:h-comp", code="h-comp", contradiction_group_ref="group:h-comp")
    before = RelationshipVariablesProjection(
        trust_bp=1_900,
        closeness_bp=1_900,
        respect_bp=1_900,
        reliability_bp=1_900,
        mutuality_bp=1_900,
        repair_confidence_bp=1_900,
    )
    after = RelationshipVariablesProjection(
        trust_bp=2_400,
        closeness_bp=2_400,
        respect_bp=2_400,
        reliability_bp=2_400,
        mutuality_bp=2_400,
        repair_confidence_bp=2_400,
    )
    candidate = RelationshipHysteresisProjection(
        candidate_stage="acquaintance",
        direction="promote",
        candidate_since=NOW,
        confirming_adjustment_count=1,
    )
    existing = RelationshipStateProjection(
        relationship_id="relationship:user:geoff",
        subject_ref="user:geoff",
        entity_revision=1,
        variables=before,
        policy_digest=RELATIONSHIP_POLICY_DIGEST,
    )
    original = adjustment_payload(
        source,
        adjustment_id="adjustment:h-comp-original",
        expected_revision=1,
        before=before,
        after=after,
        accepted=RelationshipVariableDeltas(
            trust_bp=500,
            closeness_bp=500,
            respect_bp=500,
            reliability_bp=500,
            mutuality_bp=500,
            repair_confidence_bp=500,
        ),
        hysteresis_after=candidate,
    )
    states, history = adjust_relationship_slow_variables(
        (existing,), (), (source,), original, logical_time=NOW
    )
    compensation = authorized(
        RelationshipSlowVariableAdjustedPayload,
        change_id="change:h-compensation",
        transition_id="transition:h-compensation",
        expected_entity_revision=2,
        policy_refs=("policy:relationship-v1",),
        relationship_id="relationship:user:geoff",
        subject_ref="user:geoff",
        adjustment_id="adjustment:h-compensation",
        operation="compensate",
        signal_refs=(source.signal_id,),
        proposed_deltas=RelationshipVariableDeltas(
            trust_bp=-500,
            closeness_bp=-500,
            respect_bp=-500,
            reliability_bp=-500,
            mutuality_bp=-500,
            repair_confidence_bp=-500,
        ),
        accepted_deltas=RelationshipVariableDeltas(
            trust_bp=-500,
            closeness_bp=-500,
            respect_bp=-500,
            reliability_bp=-500,
            mutuality_bp=-500,
            repair_confidence_bp=-500,
        ),
        variables_before=after,
        variables_after=before,
        stage_before="stranger",
        stage_after="stranger",
        hysteresis_before=candidate,
        hysteresis_after=RelationshipHysteresisProjection(),
        confidence_bp=10_000,
        persistence="durable",
        contradiction_group_ref="group:h-comp",
        rationale_code="correction",
        policy_version="relationship-policy.1",
        policy_digest=RELATIONSHIP_POLICY_DIGEST,
        adjusted_at=NOW + timedelta(days=1),
        compensates_adjustment_id="adjustment:h-comp-original",
    )
    restored, _ = adjust_relationship_slow_variables(
        states, history, (source,), compensation, logical_time=NOW + timedelta(days=1)
    )
    assert restored[0].stage == "stranger"
    assert restored[0].hysteresis == RelationshipHysteresisProjection()


def test_stage_gap_is_stable_and_uninstalled_commitment_stages_fail_closed() -> None:
    source = signal("signal:gap", code="gap", contradiction_group_ref="group:gap")
    gap = RelationshipVariablesProjection(
        trust_bp=1_800,
        closeness_bp=1_800,
        respect_bp=1_800,
        reliability_bp=1_800,
        mutuality_bp=1_800,
        repair_confidence_bp=1_800,
    )
    gap_after = gap.model_copy(update={"trust_bp": 1_801})
    state = RelationshipStateProjection(
        relationship_id="relationship:user:geoff",
        subject_ref="user:geoff",
        entity_revision=1,
        stage="acquaintance",
        variables=gap,
        policy_digest=RELATIONSHIP_POLICY_DIGEST,
    )
    payload = adjustment_payload(
        source,
        adjustment_id="adjustment:gap",
        expected_revision=1,
        before=gap,
        after=gap_after,
        accepted=RelationshipVariableDeltas(trust_bp=1),
        stage_before="acquaintance",
        stage_after="acquaintance",
    )
    states, _ = adjust_relationship_slow_variables(
        (state,), (), (source,), payload, logical_time=NOW
    )
    assert states[0].stage == "acquaintance"
    assert states[0].hysteresis == RelationshipHysteresisProjection()

    for unsupported_stage in ("ambiguous", "lover"):
        unsupported = state.model_copy(update={"stage": unsupported_stage})
        unsupported_payload = payload.model_copy(
            update={"stage_before": unsupported_stage, "stage_after": unsupported_stage}
        )
        with pytest.raises(ValueError, match="commitment protocol"):
            adjust_relationship_slow_variables(
                (unsupported,), (), (source,), unsupported_payload, logical_time=NOW
            )


def test_boundary_lifecycle_is_independent_of_relationship_stage() -> None:
    boundary = BoundaryProjection(
        boundary_id="boundary:privacy",
        entity_revision=1,
        subject_ref="user:geoff",
        scope_ref="scope:private-media",
        strength_bp=8_000,
        status="active",
        expires_at=None,
        evidence_refs=(evidence(),),
        origin=RelationshipBoundaryOrigin(
            change_id="change:boundary:open",
            transition_id="transition:boundary:open",
            policy_refs=("policy:boundary-v1",),
            accepted_event_ref="event:boundary:open",
        ),
        policy_version="boundary-policy.1",
        opened_at=NOW,
        updated_at=NOW,
    )
    opened = authorized(
        BoundaryChangedPayload,
        change_id=boundary.origin.change_id,
        transition_id=boundary.origin.transition_id,
        expected_entity_revision=0,
        evidence_refs=boundary.evidence_refs,
        policy_refs=boundary.origin.policy_refs,
        operation="open",
        boundary=boundary,
    )

    boundaries = change_boundary((), opened, logical_time=NOW)

    assert boundaries == (boundary,)
    assert boundaries[0].strength_bp == 8_000
    assert not hasattr(opened, "relationship_stage")

    duplicate_scope = boundary.model_copy(
        update={
            "boundary_id": "boundary:privacy:duplicate",
            "origin": RelationshipBoundaryOrigin(
                change_id="change:boundary:duplicate",
                transition_id="transition:boundary:duplicate",
                policy_refs=("policy:boundary-v1",),
                accepted_event_ref="event:boundary:duplicate",
            ),
        }
    )
    duplicate_open = authorized(
        BoundaryChangedPayload,
        change_id="change:boundary:duplicate",
        transition_id="transition:boundary:duplicate",
        expected_entity_revision=0,
        evidence_refs=duplicate_scope.evidence_refs,
        policy_refs=("policy:boundary-v1",),
        acceptance_id="acceptance:boundary:duplicate",
        proposal_id="proposal:boundary:duplicate",
        operation="open",
        boundary=duplicate_scope,
    )
    with pytest.raises(ValueError, match="subject scope"):
        change_boundary(boundaries, duplicate_open, logical_time=NOW)

    revised_boundary = boundary.model_copy(
        update={
            "entity_revision": 2,
            "strength_bp": 9_000,
            "origin": RelationshipBoundaryOrigin(
                change_id="change:boundary:revise",
                transition_id="transition:boundary:revise",
                policy_refs=("policy:boundary-v1",),
                accepted_event_ref="event:boundary:revise",
            ),
        }
    )
    revised = authorized(
        BoundaryChangedPayload,
        change_id="change:boundary:revise",
        transition_id="transition:boundary:revise",
        expected_entity_revision=1,
        evidence_refs=revised_boundary.evidence_refs,
        policy_refs=("policy:boundary-v1",),
        acceptance_id="acceptance:boundary:revise",
        proposal_id="proposal:boundary:revise",
        operation="revise",
        boundary=revised_boundary,
    )
    boundaries = change_boundary(boundaries, revised, logical_time=NOW)
    assert boundaries[0].strength_bp == 9_000

    with pytest.raises(ValueError, match="stale"):
        change_boundary(boundaries, revised, logical_time=NOW)

    closed_boundary = revised_boundary.model_copy(
        update={
            "entity_revision": 3,
            "status": "closed",
            "origin": RelationshipBoundaryOrigin(
                change_id="change:boundary:close",
                transition_id="transition:boundary:close",
                policy_refs=("policy:boundary-v1",),
                accepted_event_ref="event:boundary:close",
            ),
        }
    )
    closed = authorized(
        BoundaryChangedPayload,
        change_id="change:boundary:close",
        transition_id="transition:boundary:close",
        expected_entity_revision=2,
        evidence_refs=closed_boundary.evidence_refs,
        policy_refs=("policy:boundary-v1",),
        acceptance_id="acceptance:boundary:close",
        proposal_id="proposal:boundary:close",
        operation="close",
        boundary=closed_boundary,
    )
    boundaries = change_boundary(boundaries, closed, logical_time=NOW)
    assert boundaries[0].status == "closed"

    invalid_revise_boundary = closed_boundary.model_copy(
        update={
            "origin": RelationshipBoundaryOrigin(
                change_id="change:boundary:bad-revise",
                transition_id="transition:boundary:bad-revise",
                policy_refs=("policy:boundary-v1",),
                accepted_event_ref="event:boundary:bad-revise",
            )
        }
    )
    with pytest.raises(ValueError, match="revision must remain active"):
        authorized(
            BoundaryChangedPayload,
            change_id="change:boundary:bad-revise",
            transition_id="transition:boundary:bad-revise",
            expected_entity_revision=2,
            evidence_refs=invalid_revise_boundary.evidence_refs,
            policy_refs=("policy:boundary-v1",),
            operation="revise",
            boundary=invalid_revise_boundary,
        )


def test_relationship_schema_rejects_naive_authority_times() -> None:
    source = signal("signal:time", code="time", contradiction_group_ref="group:time")
    with pytest.raises(ValueError, match="timezone-aware"):
        RelationshipSignalProjection.model_validate(
            {**source.model_dump(), "accepted_at": NAIVE_NOW}
        )
    with pytest.raises(ValueError, match="timezone-aware"):
        RelationshipHysteresisProjection(
            candidate_stage="acquaintance",
            direction="promote",
            candidate_since=NAIVE_NOW,
            confirming_adjustment_count=1,
        )
    adjustment = RelationshipAdjustmentProjection(
        adjustment_id="adjustment:time",
        subject_ref="user:geoff",
        relationship_revision=1,
        operation="adjust",
        signal_refs=(source.signal_id,),
        proposed_deltas=RelationshipVariableDeltas(trust_bp=1),
        accepted_deltas=RelationshipVariableDeltas(trust_bp=1),
        variables_before=RelationshipVariablesProjection(),
        variables_after=RelationshipVariablesProjection(trust_bp=1),
        stage_before="stranger",
        stage_after="stranger",
        hysteresis_before=RelationshipHysteresisProjection(),
        hysteresis_after=RelationshipHysteresisProjection(),
        confidence_bp=8_000,
        persistence="durable",
        rationale_code="time",
        policy_version="relationship-policy.1",
        policy_digest=RELATIONSHIP_POLICY_DIGEST,
        adjusted_at=NOW,
    )
    with pytest.raises(ValueError, match="timezone-aware"):
        RelationshipAdjustmentProjection.model_validate(
            {**adjustment.model_dump(), "adjusted_at": NAIVE_NOW}
        )
    boundary = BoundaryProjection(
        boundary_id="boundary:time",
        entity_revision=1,
        subject_ref="user:geoff",
        scope_ref="scope:time",
        strength_bp=1,
        status="active",
        evidence_refs=(evidence(),),
        origin=RelationshipBoundaryOrigin(
            change_id="change:time",
            transition_id="transition:time",
            policy_refs=("policy:boundary-v1",),
            accepted_event_ref="event:time",
        ),
        policy_version="boundary-policy.1",
        opened_at=NOW,
        updated_at=NOW,
    )
    for field in ("opened_at", "updated_at", "expires_at"):
        with pytest.raises(ValueError, match="timezone-aware"):
            BoundaryProjection.model_validate({**boundary.model_dump(), field: NAIVE_NOW})
    state = RelationshipStateProjection(
        relationship_id="relationship:user:geoff",
        subject_ref="user:geoff",
        policy_digest=RELATIONSHIP_POLICY_DIGEST,
        last_adjusted_at=NOW,
    )
    with pytest.raises(ValueError, match="timezone-aware"):
        RelationshipStateProjection.model_validate(
            {**state.model_dump(), "last_adjusted_at": NAIVE_NOW}
        )

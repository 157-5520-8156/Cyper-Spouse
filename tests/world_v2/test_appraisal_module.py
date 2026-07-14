from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from companion_daemon.world_v2.appraisal_events import (
    AppraisalAcceptedPayload,
    AppraisalContradictedPayload,
    AppraisalExpiredPayload,
    AppraisalHypothesis,
    AppraisalProjection,
    AppraisalSupersededPayload,
    appraisal_mutation_hash,
)
from companion_daemon.world_v2.appraisal_reducers import (
    accept_appraisal,
    contradict_appraisal,
    expire_appraisal,
    supersede_appraisal,
)
from companion_daemon.world_v2.schemas import AppraisalOrigin, EvidenceRef


NOW = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)


def evidence(ref_id: str = "message:42") -> EvidenceRef:
    return EvidenceRef(
        ref_id=ref_id,
        evidence_type="observed_message",
        claim_purpose="private_hypothesis",
    )


def clock_evidence() -> EvidenceRef:
    return EvidenceRef(
        ref_id="clock:appraisal:1",
        evidence_type="clock_observation",
        claim_purpose="current_fact",
    )


def hypotheses() -> tuple[AppraisalHypothesis, ...]:
    return (
        AppraisalHypothesis(
            hypothesis_id="hypothesis:disappointed",
            meaning="disappointment",
            attribution="user",
            controllability="partly_controllable",
            severity="moderate",
            weight_bp=6_500,
        ),
        AppraisalHypothesis(
            hypothesis_id="hypothesis:misunderstood",
            meaning="misunderstanding",
            attribution="unknown",
            controllability="controllable",
            severity="low",
            weight_bp=3_500,
        ),
    )


def appraisal(
    appraisal_id: str = "appraisal:1",
    *,
    accepted_at: datetime = NOW,
    supersedes_appraisal_id: str | None = None,
    change_id: str = "change:appraisal:1",
    transition_id: str = "transition:appraisal:1",
    accepted_event_ref: str = "event:appraisal:1",
) -> AppraisalProjection:
    return AppraisalProjection(
        appraisal_id=appraisal_id,
        entity_revision=1,
        subject_ref="interaction:user:42",
        source_cluster_ref="conversation:42",
        origin=AppraisalOrigin(
            change_id=change_id,
            transition_id=transition_id,
            policy_refs=("policy:appraisal-v1",),
            matrix_catalog_version="appraisal-matrix.1",
            clustering_policy_version="source-clustering.1",
            accepted_event_ref=accepted_event_ref,
        ),
        hypotheses=hypotheses(),
        evidence_refs=(evidence(),),
        confidence_bp=7_000,
        accepted_at=accepted_at,
        expires_at=accepted_at + timedelta(hours=2),
        supersedes_appraisal_id=supersedes_appraisal_id,
    )


def accepted_payload(value: AppraisalProjection | None = None) -> AppraisalAcceptedPayload:
    item = value or appraisal()
    raw = dict(
        change_id="change:appraisal:1",
        transition_id="transition:appraisal:1",
        expected_entity_revision=0,
        evidence_refs=item.evidence_refs,
        policy_refs=("policy:appraisal-v1",),
        acceptance_id="acceptance:appraisal:1",
        proposal_id="proposal:appraisal:1",
        evaluated_world_revision=4,
        accepted_change_hash="0" * 64,
        trigger_id="trigger:appraisal:1",
        appraisal=item,
    )
    raw["accepted_change_hash"] = appraisal_mutation_hash(raw)
    return AppraisalAcceptedPayload.model_validate(raw)


def authorized_payload(model, **raw):
    raw.setdefault("acceptance_id", f"acceptance:{raw['change_id']}")
    raw.setdefault("proposal_id", f"proposal:{raw['change_id']}")
    raw.setdefault("evaluated_world_revision", 4)
    raw.setdefault("accepted_change_hash", "0" * 64)
    raw.setdefault("trigger_id", f"trigger:{raw['change_id']}")
    raw["accepted_change_hash"] = appraisal_mutation_hash(raw)
    return model.model_validate(raw)


def test_accept_preserves_alternative_meanings_without_creating_affect() -> None:
    state = accept_appraisal((), accepted_payload(), logical_time=NOW)

    assert len(state) == 1
    assert state[0].status == "active"
    assert sum(candidate.weight_bp for candidate in state[0].hypotheses) == 10_000
    assert {candidate.meaning for candidate in state[0].hypotheses} == {
        "disappointment",
        "misunderstanding",
    }
    assert "affect" not in state[0].model_dump()


def test_hypothesis_weights_must_form_one_normalized_distribution() -> None:
    values = list(hypotheses())
    values[1] = values[1].model_copy(update={"weight_bp": 3_499})

    with pytest.raises(ValidationError, match="10,000"):
        AppraisalProjection.model_validate(
            {**appraisal().model_dump(), "hypotheses": tuple(values)}
        )


def test_accept_rejects_untyped_or_divergent_evidence_and_affect_delta() -> None:
    value = appraisal()
    with pytest.raises(ValidationError, match="evidence"):
        authorized_payload(
            AppraisalAcceptedPayload,
            change_id="change:bad-evidence",
            transition_id="transition:bad-evidence",
            expected_entity_revision=0,
            evidence_refs=(evidence("message:different"),),
            policy_refs=("policy:appraisal-v1",),
            appraisal=value,
        )

    with pytest.raises(ValidationError):
        AppraisalAcceptedPayload.model_validate(
            {
                **accepted_payload(value).model_dump(mode="json"),
                "affect_delta": {"hurt": 1000},
            }
        )


def test_contradiction_is_sourced_terminal_and_revision_checked() -> None:
    active = accept_appraisal((), accepted_payload(), logical_time=NOW)
    payload = authorized_payload(
        AppraisalContradictedPayload,
        change_id="change:contradict:1",
        transition_id="transition:contradict:1",
        expected_entity_revision=1,
        evidence_refs=(evidence("message:clarification"),),
        policy_refs=("policy:appraisal-v1",),
        appraisal_id="appraisal:1",
        contradicted_at=NOW + timedelta(minutes=10),
        contradiction_refs=(evidence("message:clarification"),),
    )

    contradicted = contradict_appraisal(
        active,
        payload,
        logical_time=NOW + timedelta(minutes=10),
    )

    assert contradicted[0].status == "contradicted"
    assert contradicted[0].entity_revision == 2
    assert contradicted[0].closed_at == NOW + timedelta(minutes=10)
    assert contradicted[0].contradiction_refs == payload.contradiction_refs
    with pytest.raises(ValueError, match="active"):
        contradict_appraisal(
            contradicted,
            payload.model_copy(update={"expected_entity_revision": 2}),
            logical_time=NOW + timedelta(minutes=10),
        )


def test_expiry_uses_authoritative_logical_time_not_wall_clock() -> None:
    active = accept_appraisal((), accepted_payload(), logical_time=NOW)
    expiry = NOW + timedelta(hours=2)
    payload = AppraisalExpiredPayload(
        change_id="change:expire:1",
        transition_id="transition:expire:1",
        expected_entity_revision=1,
        evidence_refs=(clock_evidence(),),
        policy_refs=("policy:appraisal-v1",),
        appraisal_id="appraisal:1",
        expired_at=expiry,
    )

    with pytest.raises(ValueError, match="not reached"):
        expire_appraisal(active, payload, logical_time=expiry - timedelta(seconds=1))

    expired = expire_appraisal(active, payload, logical_time=expiry)
    assert expired[0].status == "expired"
    assert expired[0].closed_at == expiry


def test_supersede_atomically_closes_old_interpretation_and_opens_successor() -> None:
    active = accept_appraisal((), accepted_payload(), logical_time=NOW)
    changed_at = NOW + timedelta(minutes=20)
    successor = appraisal(
        "appraisal:2",
        accepted_at=changed_at,
        supersedes_appraisal_id="appraisal:1",
        change_id="change:supersede:1",
        transition_id="transition:supersede:1",
        accepted_event_ref="event:supersede:1",
    )
    payload = authorized_payload(
        AppraisalSupersededPayload,
        change_id="change:supersede:1",
        transition_id="transition:supersede:1",
        expected_entity_revision=1,
        evidence_refs=successor.evidence_refs,
        policy_refs=("policy:appraisal-v1",),
        appraisal_id="appraisal:1",
        superseded_at=changed_at,
        successor=successor,
    )

    result = supersede_appraisal(active, payload, logical_time=changed_at)

    assert [(item.appraisal_id, item.status) for item in result] == [
        ("appraisal:1", "superseded"),
        ("appraisal:2", "active"),
    ]
    assert result[0].superseded_by_appraisal_id == "appraisal:2"
    assert result[1].supersedes_appraisal_id == "appraisal:1"


def test_transition_time_must_equal_recorded_logical_time() -> None:
    active = accept_appraisal((), accepted_payload(), logical_time=NOW)
    expiry = NOW + timedelta(hours=2)
    payload = AppraisalExpiredPayload(
        change_id="change:expire:1",
        transition_id="transition:expire:1",
        expected_entity_revision=1,
        evidence_refs=(clock_evidence(),),
        policy_refs=("policy:appraisal-v1",),
        appraisal_id="appraisal:1",
        expired_at=expiry,
    )

    with pytest.raises(ValueError, match="logical time"):
        expire_appraisal(active, payload, logical_time=expiry + timedelta(seconds=1))


def test_terminal_transition_requires_an_existing_entity_revision() -> None:
    with pytest.raises(ValidationError):
        AppraisalExpiredPayload(
            change_id="change:expire:missing",
            transition_id="transition:expire:missing",
            expected_entity_revision=0,
            evidence_refs=(clock_evidence(),),
            policy_refs=("policy:appraisal-v1",),
            appraisal_id="appraisal:missing",
            expired_at=NOW,
        )

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from companion_daemon.world_v2.matrix_catalog import (
    CandidateDistribution,
    ClassificationCandidate,
    CombinationConstraint,
    FrequencyBudget,
    MatrixCatalog,
    MatrixField,
    MatrixSchemaError,
    MatrixSelection,
    default_matrix_catalog,
)


NOW = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)


def test_catalog_exposes_the_frozen_matrix_vocabulary_without_behavior_commands() -> None:
    catalog = default_matrix_catalog()

    assert catalog.catalog_version == "world-v2-matrix-1"
    assert catalog.lookup("appraisal.negative").value_set == (
        "disappointment",
        "dismissal",
        "boundary_violation",
        "dehumanization",
        "coercion",
        "control_pressure",
        "betrayal",
        "loss",
    )
    assert "npc_conflict" in catalog.lookup("appraisal.life").value_set
    assert catalog.lookup("life.location_visibility").value_set == (
        "private",
        "shareable",
        "public",
    )
    assert "provider_accepted" in catalog.lookup("action.state").value_set
    assert "fulfilled" in catalog.lookup("continuity.settlement").value_set
    assert set(catalog.lookup("interruption.motive").value_set) >= {
        "high_interest",
        "strong_disagreement",
        "boundary_pressure",
    }
    assert "no_intervention" in catalog.lookup("social_response_option").value_set
    assert all(field.behavior_authority == "none" for field in catalog.fields)


def test_candidate_distribution_preserves_alternatives_sources_expiry_and_budget() -> None:
    catalog = default_matrix_catalog()
    distribution = CandidateDistribution(
        catalog_version=catalog.catalog_version,
        field_id="appraisal.negative",
        candidates=(
            ClassificationCandidate(
                value="disappointment",
                weight=6100,
                confidence=7200,
                source_refs=("observation:user-42", "thread:life-share"),
                producer="user-emotion-classifier@2",
                expires_at=NOW + timedelta(hours=2),
            ),
            ClassificationCandidate(
                value="dismissal",
                weight=2500,
                confidence=4300,
                source_refs=("observation:user-42",),
                producer="user-emotion-classifier@2",
                expires_at=NOW + timedelta(minutes=30),
            ),
        ),
        frequency_budget=FrequencyBudget(
            state="recently_varied",
            window="rolling-12-turns",
            used=2,
            limit=4,
            source_refs=("projection:variation-budget",),
        ),
        produced_at=NOW,
    )

    validated = catalog.validate_candidates(distribution, at=NOW)

    assert [candidate.value for candidate in validated.candidates] == [
        "disappointment",
        "dismissal",
    ]
    assert validated.candidates[0].weight == 6100
    assert validated.candidates[0].source_refs == (
        "observation:user-42",
        "thread:life-share",
    )
    assert validated.frequency_budget.state == "recently_varied"


def test_expired_candidate_remains_auditable_but_is_not_active() -> None:
    catalog = default_matrix_catalog()
    distribution = CandidateDistribution(
        catalog_version=catalog.catalog_version,
        field_id="interruption.motive",
        candidates=(
            ClassificationCandidate(
                value="high_interest",
                weight=8000,
                confidence=9000,
                source_refs=("observation:old",),
                producer="interrupt-classifier@1",
                expires_at=NOW - timedelta(seconds=1),
            ),
        ),
        produced_at=NOW - timedelta(minutes=5),
    )

    validated = catalog.validate_candidates(distribution, at=NOW)

    assert validated.candidates[0].value == "high_interest"
    assert validated.active_candidates(at=NOW) == ()


def test_catalog_validates_schema_and_only_coordinate_compatibility() -> None:
    field = MatrixField(
        field_id="test.stance",
        value_set=("defer", "remain_silent"),
        owner="deliberation",
        candidate_producers=("main_model",),
        consumers=("deliberation", "evaluator"),
        persistence="proposal",
        confidence_required=False,
        expiry_or_decay="proposal_lifetime",
        catalog_version="test-v1",
    )
    visible_action = MatrixField(
        field_id="test.visible_action",
        value_set=("reply", "none"),
        owner="deliberation",
        candidate_producers=("main_model",),
        consumers=("acceptance", "evaluator"),
        persistence="proposal",
        confidence_required=False,
        expiry_or_decay="proposal_lifetime",
        catalog_version="test-v1",
    )
    catalog = MatrixCatalog(
        catalog_version="test-v1",
        fields=(field, visible_action),
        constraints=(
            CombinationConstraint(
                constraint_id="silence-needs-no-visible-action",
                when=(MatrixSelection(field_id="test.stance", value="remain_silent"),),
                incompatible_with=(
                    MatrixSelection(field_id="test.visible_action", value="reply"),
                ),
                rationale="coordinate contradiction only; this does not select a response",
            ),
        ),
    )

    assert catalog.validate_schema(
        (MatrixSelection(field_id="test.stance", value="defer"),)
    ) == (MatrixSelection(field_id="test.stance", value="defer"),)
    with pytest.raises(MatrixSchemaError, match="unknown field"):
        catalog.validate_schema((MatrixSelection(field_id="unknown", value="defer"),))
    with pytest.raises(MatrixSchemaError, match="unknown value"):
        catalog.validate_schema((MatrixSelection(field_id="test.stance", value="reply"),))


def test_catalog_rejects_aesthetic_or_mandatory_behavior_rules() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        CombinationConstraint(
            constraint_id="always-comfort",
            when=(MatrixSelection(field_id="appraisal.negative", value="disappointment"),),
            incompatible_with=(
                MatrixSelection(field_id="social_response_option", value="no_intervention"),
            ),
            required_behavior="comfort_user",
            rationale="make the reply nicer",
        )

    with pytest.raises(ValidationError, match="hard invariants belong to authority fields"):
        MatrixField(
            field_id="aesthetic.niceness",
            value_set=("comforting",),
            owner="deliberation",
            candidate_producers=("main_model",),
            consumers=("evaluator",),
            persistence="proposal",
            confidence_required=False,
            expiry_or_decay="proposal_lifetime",
            catalog_version="test-v1",
            hard_invariant_refs=("always-be-kind",),
        )


def test_disappointment_offence_and_world_events_are_candidates_not_commands() -> None:
    catalog = default_matrix_catalog()
    selections = catalog.validate_schema(
        (
            MatrixSelection(field_id="appraisal.negative", value="disappointment"),
            MatrixSelection(field_id="appraisal.negative", value="boundary_violation"),
            MatrixSelection(field_id="appraisal.life", value="npc_conflict"),
            MatrixSelection(field_id="social_response_option", value="no_intervention"),
        )
    )

    assert tuple(selection.value for selection in selections) == (
        "disappointment",
        "boundary_violation",
        "npc_conflict",
        "no_intervention",
    )
    assert not hasattr(catalog, "choose_behavior")
    assert not hasattr(catalog, "render_reply")

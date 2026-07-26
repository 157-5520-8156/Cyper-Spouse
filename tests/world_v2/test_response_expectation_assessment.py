from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from companion_daemon.world_v2.reducers import ReducerState, reduce_event
from companion_daemon.world_v2.schemas import (
    CommittedWorldEventRef,
    ExpressionPlanManifestRef,
    MessageObservationRef,
    ResponseExpectationAuthority,
    ResponseExpectationAssessedPayload,
    ResponseExpectationAssessmentProjection,
    WorldEvent,
)


NOW = datetime(2026, 7, 26, 1, 7, tzinfo=UTC)
WORLD = "world:response-expectation-assessment"


def test_assessment_is_a_source_bound_replayable_expectation_transition() -> None:
    payload = ResponseExpectationAssessedPayload(
        assessment_id="assessment:reply:1",
        source_plan_id="plan:question:1",
        source_acceptance_event_ref="event:acceptance:question:1",
        inbound_observation_id="observation:answer:1",
        inbound_observation_event_ref="event:observation:answer:1",
        status="fulfilled",
        reason="The counterpart directly answered the earlier question.",
        assessed_at=NOW,
    )
    event = WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id="event:response-expectation-assessed:1",
        world_id=WORLD,
        event_type="ResponseExpectationAssessed",
        logical_time=NOW,
        created_at=NOW,
        actor="agent:companion",
        source="world-runtime:inbound-cognition",
        trace_id="trace:assessment:1",
        causation_id=payload.inbound_observation_event_ref,
        correlation_id="conversation:1",
        idempotency_key="response-expectation-assessed:1",
        payload=payload.model_dump(mode="json"),
    )

    state = ReducerState.model_construct(
        expression_plan_manifests=(
            ExpressionPlanManifestRef.model_construct(
                plan_id=payload.source_plan_id,
                acceptance_event_ref=payload.source_acceptance_event_ref,
                response_expectation=ResponseExpectationAuthority(
                    source_plan_id=payload.source_plan_id,
                    source_beat_id="beat:question:1",
                    hoped_response="how the trip went",
                    pressure_bp=1_000,
                    importance_bp=5_000,
                    not_before=NOW - timedelta(hours=1),
                    expires_at=NOW + timedelta(hours=1),
                ),
            ),
        ),
        message_observations=(
            MessageObservationRef(
                observation_id=payload.inbound_observation_id,
                source="test",
                source_event_id=payload.inbound_observation_event_ref,
                content_payload_hash="sha256:" + "c" * 64,
                event_payload_hash="b" * 64,
                world_revision=2,
            ),
        ),
        committed_world_event_refs=(
            CommittedWorldEventRef(
                event_id=payload.source_acceptance_event_ref,
                event_type="ExpressionPlanAccepted",
                world_revision=1,
                payload_hash="a" * 64,
                logical_time=NOW,
            ),
            CommittedWorldEventRef(
                event_id=payload.inbound_observation_event_ref,
                event_type="ObservationRecorded",
                world_revision=2,
                payload_hash="b" * 64,
                logical_time=NOW,
            ),
        )
    )
    reduced = reduce_event(state, event)
    assert reduced.response_expectation_assessments == (
        ResponseExpectationAssessmentProjection(
            **payload.model_dump(mode="python"),
            world_revision=3,
            event_ref=event.event_id,
        ),
    )
    assert reduced.committed_world_event_refs[-1].event_id == event.event_id


def test_assessment_rejects_cross_bound_plan_and_observation_ids() -> None:
    payload = ResponseExpectationAssessedPayload(
        assessment_id="assessment:reply:cross-bound",
        source_plan_id="plan:other",
        source_acceptance_event_ref="event:acceptance:question:1",
        inbound_observation_id="observation:other",
        inbound_observation_event_ref="event:observation:answer:1",
        status="fulfilled",
        reason="Cross-bound identities must not close an expectation.",
        assessed_at=NOW,
    )
    event = WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id="event:response-expectation-assessed:cross-bound",
        world_id=WORLD,
        event_type="ResponseExpectationAssessed",
        logical_time=NOW,
        created_at=NOW,
        actor="agent:companion",
        source="test",
        trace_id="trace:assessment:cross-bound",
        causation_id=payload.inbound_observation_event_ref,
        correlation_id="conversation:1",
        idempotency_key="response-expectation-assessed:cross-bound",
        payload=payload.model_dump(mode="json"),
    )
    state = ReducerState.model_construct(
        expression_plan_manifests=(
            ExpressionPlanManifestRef.model_construct(
                plan_id="plan:question:1",
                acceptance_event_ref=payload.source_acceptance_event_ref,
                response_expectation=ResponseExpectationAuthority(
                    source_plan_id="plan:question:1",
                    source_beat_id="beat:question:1",
                    hoped_response="answer",
                    pressure_bp=1_000,
                    importance_bp=5_000,
                    not_before=NOW - timedelta(hours=1),
                    expires_at=NOW + timedelta(hours=1),
                ),
            ),
        ),
        message_observations=(
            MessageObservationRef(
                observation_id="observation:answer:1",
                source="test",
                source_event_id=payload.inbound_observation_event_ref,
                content_payload_hash="sha256:" + "c" * 64,
                event_payload_hash="b" * 64,
                world_revision=2,
            ),
        ),
        committed_world_event_refs=(
            CommittedWorldEventRef(
                event_id=payload.source_acceptance_event_ref,
                event_type="ExpressionPlanAccepted",
                world_revision=1,
                payload_hash="a" * 64,
                logical_time=NOW,
            ),
            CommittedWorldEventRef(
                event_id=payload.inbound_observation_event_ref,
                event_type="ObservationRecorded",
                world_revision=2,
                payload_hash="b" * 64,
                logical_time=NOW,
            ),
        ),
    )
    with pytest.raises(ValueError, match="declared plan"):
        reduce_event(state, event)

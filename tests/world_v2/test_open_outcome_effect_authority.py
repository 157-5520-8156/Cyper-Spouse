from datetime import UTC, datetime, timedelta
import hashlib
import json

import pytest
from pydantic import ValidationError

from companion_daemon.world_v2.schemas import (
    BiographicalCoordinateReplacement,
    CommittedWorldEventRef,
    DynamicLifeArcContextDescriptor,
    EvidenceRef,
    OutcomeObservationProjection,
    OutcomeCandidateDescriptor,
    OutcomeProposalProjection,
    ProjectionCursor,
    ProvisionalNpcIntroductionDescriptor,
    ProvisionalPlaceIntroductionDescriptor,
    RecordedWorldDrawBinding,
    WorldEvent,
    WorldOccurrenceProjection,
)
from companion_daemon.world_v2.life_events import (
    OutcomeProposalRecordedPayload,
    outcome_mutation_hash,
)
from companion_daemon.world_v2.random_authority import (
    NormalizedCandidateWeight,
    RandomDrawRecordedPayload,
)
from companion_daemon.world_v2.reducers import ReducerState, reduce_event


def test_open_outcome_effect_descriptors_are_hash_bound_and_behavior_neutral() -> None:
    npc = ProvisionalNpcIntroductionDescriptor.create(
        provisional_entity_ref="provisional:npc:bookshop-clerk",
        summary_content_ref="content:open-life:npc:bookshop-clerk",
        summary_payload_hash="a" * 64,
        narrative_tags=("narrative:bookshop", "narrative:new_acquaintance"),
        privacy_class="personal",
    )
    arc = DynamicLifeArcContextDescriptor.create(
        summary_content_ref="content:open-life:arc:bookshop-volunteering",
        summary_payload_hash="b" * 64,
        narrative_tags=("narrative:bookshop", "narrative:volunteering"),
        duration_days=21,
        privacy_class="personal",
    )

    candidate = OutcomeCandidateDescriptor(
        candidate_result_ref="candidate:open-life:accepted",
        result_id="result:open-life:accepted",
        result_payload_ref="content:result:open-life:accepted",
        result_payload_hash="c" * 64,
        privacy_class="personal",
        causal_authority="character_choice",
        provisional_npc_introductions=(npc,),
        dynamic_life_arc_context=arc,
    )

    assert npc.descriptor_hash == npc.canonical_hash()
    assert arc.descriptor_hash == arc.canonical_hash()
    assert candidate.provisional_npc_introductions == (npc,)
    assert candidate.dynamic_life_arc_context == arc
    assert "capability" not in npc.model_dump(mode="json")
    assert "capability" not in arc.model_dump(mode="json")


@pytest.mark.parametrize(
    "tag",
    ("role:intern", "location:shanghai", "capability:create_fact", "Narrative:friend"),
)
def test_open_outcome_effect_tags_are_only_narrative_namespace(tag: str) -> None:
    with pytest.raises(ValidationError, match="narrative"):
        DynamicLifeArcContextDescriptor.create(
            summary_content_ref="content:arc",
            summary_payload_hash="a" * 64,
            narrative_tags=(tag,),
            duration_days=None,
            privacy_class="personal",
        )


def test_open_outcome_effect_hash_detects_descriptor_tampering() -> None:
    raw = DynamicLifeArcContextDescriptor.create(
        summary_content_ref="content:arc",
        summary_payload_hash="a" * 64,
        narrative_tags=("narrative:quiet_project",),
        duration_days=None,
        privacy_class="personal",
    ).model_dump(mode="json")
    raw["duration_days"] = 30
    raw["narrative_tags"] = tuple(raw["narrative_tags"])

    with pytest.raises(ValidationError, match="hash"):
        DynamicLifeArcContextDescriptor.model_validate(raw)


def test_dynamic_life_arc_cannot_replace_non_biographical_authority() -> None:
    with pytest.raises(ValidationError, match="safe current coordinate"):
        DynamicLifeArcContextDescriptor.create(
            summary_content_ref="content:arc:unsafe-coordinate",
            summary_payload_hash="a" * 64,
            narrative_tags=("narrative:unsafe_coordinate",),
            context_tags=("relationship:rewritten",),
            supersedes_context_tag_prefixes=("relationship:",),
            duration_days=None,
            privacy_class="personal",
        )


def test_open_outcome_candidate_rejects_duplicate_provisional_entity_refs() -> None:
    npc = ProvisionalNpcIntroductionDescriptor.create(
        provisional_entity_ref="provisional:npc:one",
        summary_content_ref="content:npc:one",
        summary_payload_hash="a" * 64,
        narrative_tags=("narrative:encounter",),
        privacy_class="personal",
    )

    with pytest.raises(ValidationError, match="provisional entity"):
        OutcomeCandidateDescriptor(
            candidate_result_ref="candidate:duplicate",
            result_id="result:duplicate",
            result_payload_ref="content:duplicate",
            result_payload_hash="b" * 64,
            privacy_class="personal",
            causal_authority="world_contingency",
            provisional_npc_introductions=(npc, npc),
        )


def test_immutable_objective_transition_cannot_claim_character_direction_coordinate() -> None:
    transition = BiographicalCoordinateReplacement.create(
        coordinate_ref="biography:direction.creative-work",
        summary="她想把创作当作长期方向。",
        context_tags=("work:creative",),
        replaces_context_tag_prefixes=("work:",),
        privacy_class="personal",
    )

    with pytest.raises(ValidationError, match="character direction state"):
        OutcomeCandidateDescriptor(
            candidate_result_ref="candidate:objective-direction",
            result_id="result:objective-direction",
            result_payload_ref="content:objective-direction",
            result_payload_hash="a" * 64,
            privacy_class="personal",
            causal_authority="world_contingency",
            objective_biographical_transition=transition,
        )


def test_open_outcome_effect_descriptors_do_not_accept_arbitrary_fields() -> None:
    with pytest.raises(ValidationError):
        DynamicLifeArcContextDescriptor.model_validate(
            {
                "summary_content_ref": "content:arc",
                "summary_payload_hash": "a" * 64,
                "narrative_tags": ["narrative:work"],
                "duration_days": None,
                "privacy_class": "personal",
                "descriptor_hash": "b" * 64,
                "granted_capabilities": ["fact.write", "resource.unbounded"],
                "started_at": datetime(2026, 7, 29, tzinfo=UTC).isoformat(),
            }
        )


def test_occurrence_candidate_matrix_has_one_resolution_authority() -> None:
    now = datetime(2026, 7, 29, tzinfo=UTC)
    base = {
        "result_payload_ref": "content:result",
        "result_payload_hash": "a" * 64,
        "privacy_class": "personal",
    }
    with pytest.raises(ValidationError, match="causal authority"):
        WorldOccurrenceProjection(
            occurrence_id="occurrence:mixed-authority",
            entity_revision=1,
            trigger_ref="plan:mixed-authority",
            participant_refs=("actor:companion",),
            time_window={"opens_at": now, "closes_at": now.replace(hour=1)},
            candidate_outcome_refs=("candidate:choice", "candidate:contingency"),
            candidate_outcomes=(
                OutcomeCandidateDescriptor(
                    candidate_result_ref="candidate:choice",
                    result_id="result:choice",
                    causal_authority="character_choice",
                    **base,
                ),
                OutcomeCandidateDescriptor(
                    candidate_result_ref="candidate:contingency",
                    result_id="result:contingency",
                    causal_authority="world_contingency",
                    **base,
                ),
            ),
            visibility="personal",
            status="committed",
        )


def test_character_choice_with_only_provisional_place_requires_model_authority() -> None:
    now = datetime(2026, 7, 29, tzinfo=UTC)
    source = WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id="event:place-choice-observed",
        world_id="world:place-choice",
        event_type="ClockAdvanced",
        logical_time=now,
        created_at=now,
        actor="worker:test",
        source="test",
        trace_id="trace:place-choice",
        causation_id="cause:place-choice",
        correlation_id="correlation:place-choice",
        idempotency_key="place-choice:clock",
        payload={"advanced_to": now.isoformat()},
    )
    place = ProvisionalPlaceIntroductionDescriptor.create(
        provisional_place_ref="provisional:place:quiet-cafe",
        summary_content_ref="content:place:quiet-cafe",
        summary_payload_hash="a" * 64,
        narrative_tags=("narrative:quiet_cafe",),
        timezone_name="Asia/Shanghai",
        privacy_class="personal",
    )
    candidate = OutcomeCandidateDescriptor(
        candidate_result_ref="candidate:place-choice",
        result_id="result:place-choice",
        result_payload_ref="content:place-choice",
        result_payload_hash="b" * 64,
        privacy_class="personal",
        causal_authority="character_choice",
        provisional_place_introductions=(place,),
    )
    occurrence = WorldOccurrenceProjection(
        occurrence_id="occurrence:place-choice",
        entity_revision=2,
        trigger_ref="plan:place-choice",
        participant_refs=("actor:companion",),
        time_window={
            "opens_at": now - timedelta(minutes=5),
            "closes_at": now + timedelta(hours=1),
        },
        candidate_outcome_refs=(candidate.candidate_result_ref,),
        candidate_outcomes=(candidate,),
        observation_refs=("observation:place-choice",),
        visibility="personal",
        status="active",
        activated_at=now - timedelta(minutes=1),
    )
    state = ReducerState(
        logical_time=now,
        committed_world_event_refs=(
            CommittedWorldEventRef(
                event_id=source.event_id,
                event_type=source.event_type,
                world_revision=1,
                payload_hash=source.payload_hash,
                logical_time=now,
            ),
        ),
        world_occurrences=(occurrence,),
        outcome_observations=(
            OutcomeObservationProjection(
                observation_id="observation:place-choice",
                occurrence_id=occurrence.occurrence_id,
                source_kind="committed_world_event",
                source_refs=(source.event_id,),
                observed_payload_ref=source.event_id,
                observed_payload_hash=source.payload_hash,
                observed_at=now,
                confidence_bp=10_000,
            ),
        ),
    )
    change_id = "change:place-choice"
    payload = OutcomeProposalRecordedPayload(
        outcome_proposal_id="proposal:place-choice",
        decision_proposal_id="proposal:place-choice",
        change_id=change_id,
        occurrence_id=occurrence.occurrence_id,
        evaluated_entity_revision=occurrence.entity_revision,
        evaluated_world_revision=1,
        trigger_ref=occurrence.trigger_ref,
        candidate_result_ref=candidate.candidate_result_ref,
        proposed_result_id=candidate.result_id,
        proposed_result_payload_ref=candidate.result_payload_ref,
        proposed_result_payload_hash=candidate.result_payload_hash,
        proposed_change_hash=outcome_mutation_hash(
            change_id=change_id,
            occurrence_id=occurrence.occurrence_id,
            evaluated_entity_revision=occurrence.entity_revision,
            evaluated_world_revision=1,
            candidate_result_ref=candidate.candidate_result_ref,
            result_id=candidate.result_id,
            result_payload_ref=candidate.result_payload_ref,
            result_payload_hash=candidate.result_payload_hash,
            observation_refs=("observation:place-choice",),
        ),
        observation_refs=("observation:place-choice",),
        evidence_refs=(
            EvidenceRef(
                ref_id=source.event_id,
                evidence_type="committed_world_event",
                claim_purpose="life_transition",
                source_world_revision=1,
                immutable_hash=source.payload_hash,
            ),
        ),
        confidence_bp=10_000,
        expires_at=now + timedelta(hours=1),
    )
    proposal_event = WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id="event:proposal:place-choice",
        world_id="world:place-choice",
        event_type="OutcomeProposalRecorded",
        logical_time=now,
        created_at=now,
        actor="worker:test",
        source="test",
        trace_id="trace:place-choice",
        causation_id=source.event_id,
        correlation_id="correlation:place-choice",
        idempotency_key="proposal:place-choice",
        payload=payload.model_dump(mode="json"),
    )

    with pytest.raises(ValueError, match="causal authority"):
        reduce_event(state, proposal_event)


def test_world_contingency_proposal_requires_exact_recorded_weighted_draw() -> None:
    now = datetime(2026, 7, 29, tzinfo=UTC)
    candidate_ref = "candidate:weather-turn"
    candidate = OutcomeCandidateDescriptor(
        candidate_result_ref=candidate_ref,
        result_id="result:weather-turn",
        result_payload_ref="content:weather-turn",
        result_payload_hash="a" * 64,
        privacy_class="personal",
        causal_authority="world_contingency",
        relative_plausibility_weight=7,
    )
    occurrence = WorldOccurrenceProjection(
        occurrence_id="occurrence:weather-turn",
        entity_revision=3,
        trigger_ref="plan:walk",
        participant_refs=("actor:companion",),
        time_window={
            "opens_at": now - timedelta(minutes=5),
            "closes_at": now + timedelta(hours=1),
        },
        candidate_outcome_refs=(candidate_ref,),
        candidate_outcomes=(candidate,),
        observation_refs=("observation:weather-turn",),
        visibility="personal",
        status="active",
        activated_at=now - timedelta(minutes=1),
    )
    draw_payload = RandomDrawRecordedPayload(
        draw_id="draw:weather-turn",
        attempt_id="attempt:weather-turn",
        candidate_refs=(candidate_ref,),
        candidate_set_hash=hashlib.sha256(
            json.dumps(
                (candidate_ref,),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest(),
        selected_candidate_ref=candidate_ref,
        seed_hash="b" * 64,
        catalog_version="open-life-outcome.1",
        sampler_version="random-authority.2",
        weight_policy_version="open-life-outcome-weight.1",
        weight_vector=(
            NormalizedCandidateWeight(
                candidate_ref=candidate_ref,
                weight_ppm=1_000_000,
            ),
        ),
        weight_vector_hash=hashlib.sha256(
            json.dumps(
                (
                    {
                        "candidate_ref": candidate_ref,
                        "weight_ppm": 1_000_000,
                    },
                ),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest(),
    )
    draw_event = WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id="event:random-draw:weather-turn",
        world_id="world:weather-turn",
        event_type="RandomDrawRecorded",
        logical_time=now,
        created_at=now,
        actor="worker:test",
        source="test",
        trace_id="trace:test",
        causation_id="cause:test",
        correlation_id="correlation:test",
        idempotency_key="random:test",
        payload=draw_payload.model_dump(mode="json"),
    )
    state = ReducerState(
        logical_time=now,
        committed_world_event_refs=(
            CommittedWorldEventRef(
                event_id=draw_event.event_id,
                event_type=draw_event.event_type,
                world_revision=1,
                payload_hash=draw_event.payload_hash,
                logical_time=now,
            ),
        ),
        world_occurrences=(occurrence,),
        outcome_observations=(
            OutcomeObservationProjection(
                observation_id="observation:weather-turn",
                occurrence_id=occurrence.occurrence_id,
                source_kind="committed_world_event",
                source_refs=(draw_event.event_id,),
                observed_payload_ref=draw_event.event_id,
                observed_payload_hash=draw_event.payload_hash,
                observed_at=now,
                confidence_bp=10_000,
            ),
        ),
    )
    change_id = "change:weather-turn"
    change_hash = outcome_mutation_hash(
        change_id=change_id,
        occurrence_id=occurrence.occurrence_id,
        evaluated_entity_revision=occurrence.entity_revision,
        evaluated_world_revision=1,
        candidate_result_ref=candidate_ref,
        result_id=candidate.result_id,
        result_payload_ref=candidate.result_payload_ref,
        result_payload_hash=candidate.result_payload_hash,
        observation_refs=("observation:weather-turn",),
    )
    payload = OutcomeProposalRecordedPayload(
        outcome_proposal_id="proposal:weather-turn",
        decision_proposal_id="proposal:weather-turn",
        change_id=change_id,
        occurrence_id=occurrence.occurrence_id,
        evaluated_entity_revision=occurrence.entity_revision,
        evaluated_world_revision=1,
        trigger_ref=occurrence.trigger_ref,
        candidate_result_ref=candidate_ref,
        proposed_result_id=candidate.result_id,
        proposed_result_payload_ref=candidate.result_payload_ref,
        proposed_result_payload_hash=candidate.result_payload_hash,
        proposed_change_hash=change_hash,
        observation_refs=("observation:weather-turn",),
        evidence_refs=(
            EvidenceRef(
                ref_id=draw_event.event_id,
                evidence_type="committed_world_event",
                claim_purpose="life_transition",
                source_world_revision=1,
                immutable_hash=draw_event.payload_hash,
            ),
        ),
        confidence_bp=10_000,
        expires_at=now + timedelta(hours=1),
        decision_authority="recorded_world_draw",
        recorded_world_draw=RecordedWorldDrawBinding(
            draw_event_ref=draw_event.event_id,
            draw_event_payload_hash=draw_event.payload_hash,
            draw_payload_json=draw_event.payload_json,
        ),
    )
    proposal_event = WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id="event:proposal:weather-turn",
        world_id="world:weather-turn",
        event_type="OutcomeProposalRecorded",
        logical_time=now,
        created_at=now,
        actor="worker:test",
        source="test",
        trace_id="trace:test",
        causation_id=draw_event.event_id,
        correlation_id="correlation:test",
        idempotency_key="proposal:test",
        payload=payload.model_dump(mode="json"),
    )

    reduced = reduce_event(state, proposal_event)
    assert reduced.outcome_proposals[0].decision_authority == (
        "recorded_world_draw"
    )

    forged = payload.model_copy(
        update={
            "decision_authority": "character_model",
            "recorded_world_draw": None,
            "decision_model": "forged-character",
            "decision_raw_output_hash": "c" * 64,
            "context_identity_version": "life-aftermath-context.1",
            "context_capsule_id": "d" * 64,
            "context_model_content_hash": "e" * 64,
            "context_snapshot_hash": "f" * 64,
            "context_cursor": ProjectionCursor(
                world_revision=1,
                deliberation_revision=0,
                ledger_sequence=1,
            ),
        }
    )
    with pytest.raises(ValueError, match="causal authority"):
        reduce_event(
            state,
            proposal_event.model_copy(
                update={
                    "payload_json": json.dumps(
                        forged.model_dump(mode="json"),
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    "payload_hash": hashlib.sha256(
                        json.dumps(
                            forged.model_dump(mode="json"),
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ).encode()
                    ).hexdigest(),
                }
            ),
        )


def test_unselected_open_effect_never_enters_pending_projection() -> None:
    now = datetime(2026, 7, 29, tzinfo=UTC)
    source_ref = CommittedWorldEventRef(
        event_id="event:clock:unselected",
        event_type="ClockAdvanced",
        world_revision=1,
        payload_hash="a" * 64,
        logical_time=now,
    )
    effect = DynamicLifeArcContextDescriptor.create(
        summary_content_ref="content:arc:unselected",
        summary_payload_hash="b" * 64,
        narrative_tags=("narrative:unselected",),
        duration_days=7,
        privacy_class="personal",
    )
    rejected_candidate = OutcomeCandidateDescriptor(
        candidate_result_ref="candidate:with-effect",
        result_id="result:with-effect",
        result_payload_ref="content:with-effect",
        result_payload_hash="c" * 64,
        privacy_class="personal",
        dynamic_life_arc_context=effect,
    )
    selected_candidate = OutcomeCandidateDescriptor(
        candidate_result_ref="candidate:plain",
        result_id="result:plain",
        result_payload_ref="content:plain",
        result_payload_hash="d" * 64,
        privacy_class="personal",
    )
    occurrence = WorldOccurrenceProjection(
        occurrence_id="occurrence:unselected",
        entity_revision=3,
        trigger_ref="plan:unselected",
        participant_refs=("actor:companion",),
        time_window={
            "opens_at": now - timedelta(minutes=5),
            "closes_at": now + timedelta(hours=1),
        },
        candidate_outcome_refs=(
            rejected_candidate.candidate_result_ref,
            selected_candidate.candidate_result_ref,
        ),
        candidate_outcomes=(rejected_candidate, selected_candidate),
        observation_refs=("observation:unselected",),
        visibility="personal",
        status="active",
        activated_at=now - timedelta(minutes=1),
    )
    change_id = "change:unselected"
    change_hash = outcome_mutation_hash(
        change_id=change_id,
        occurrence_id=occurrence.occurrence_id,
        evaluated_entity_revision=3,
        evaluated_world_revision=1,
        candidate_result_ref=selected_candidate.candidate_result_ref,
        result_id=selected_candidate.result_id,
        result_payload_ref=selected_candidate.result_payload_ref,
        result_payload_hash=selected_candidate.result_payload_hash,
        observation_refs=("observation:unselected",),
    )
    state = ReducerState(
        logical_time=now,
        committed_world_event_refs=(source_ref,),
        world_occurrences=(occurrence,),
        outcome_observations=(
            OutcomeObservationProjection(
                observation_id="observation:unselected",
                occurrence_id=occurrence.occurrence_id,
                source_kind="committed_world_event",
                source_refs=(source_ref.event_id,),
                observed_payload_ref=source_ref.event_id,
                observed_payload_hash=source_ref.payload_hash,
                observed_at=now,
                confidence_bp=10_000,
            ),
        ),
        outcome_proposals=(
            OutcomeProposalProjection(
                outcome_proposal_id="proposal:unselected",
                decision_proposal_id="proposal:unselected",
                change_id=change_id,
                occurrence_id=occurrence.occurrence_id,
                evaluated_entity_revision=3,
                evaluated_world_revision=1,
                trigger_ref=occurrence.trigger_ref,
                candidate_result_ref=selected_candidate.candidate_result_ref,
                proposed_result_id=selected_candidate.result_id,
                proposed_result_payload_ref=selected_candidate.result_payload_ref,
                proposed_result_payload_hash=selected_candidate.result_payload_hash,
                proposed_change_hash=change_hash,
                observation_refs=("observation:unselected",),
                evidence_refs=(
                    EvidenceRef(
                        ref_id=source_ref.event_id,
                        evidence_type="committed_world_event",
                        claim_purpose="life_transition",
                        source_world_revision=1,
                        immutable_hash=source_ref.payload_hash,
                    ),
                ),
                confidence_bp=10_000,
                expires_at=now + timedelta(hours=1),
            ),
        ),
    )
    from companion_daemon.world_v2.life_events import (
        WorldOccurrenceSettledPayload,
    )

    payload = WorldOccurrenceSettledPayload(
        change_id=change_id,
        transition_id="transition:unselected:settle",
        expected_entity_revision=3,
        evidence_refs=(
            EvidenceRef(
                ref_id=source_ref.event_id,
                evidence_type="committed_world_event",
                claim_purpose="life_transition",
                source_world_revision=1,
                immutable_hash=source_ref.payload_hash,
            ),
        ),
        acceptance_id="acceptance:unselected",
        evaluated_world_revision=1,
        accepted_change_hash=change_hash,
        occurrence_id=occurrence.occurrence_id,
        outcome_proposal_id="proposal:unselected",
        candidate_result_ref=selected_candidate.candidate_result_ref,
        result_id=selected_candidate.result_id,
        observation_refs=("observation:unselected",),
        result_payload_ref=selected_candidate.result_payload_ref,
        result_payload_hash=selected_candidate.result_payload_hash,
        settled_at=now,
        appraisal_trigger_ref="trigger:unselected:appraisal",
    )
    settlement_event = WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id="event:settlement:unselected",
        world_id="world:unselected",
        event_type="WorldOccurrenceSettled",
        logical_time=now,
        created_at=now,
        actor="worker:test",
        source="test",
        trace_id="trace:test",
        causation_id="proposal:unselected",
        correlation_id="correlation:test",
        idempotency_key="settlement:unselected",
        payload=payload.model_dump(mode="json"),
    )

    reduced = reduce_event(state, settlement_event)
    assert reduced.pending_biographical_settlements == ()
    assert reduced.world_occurrences[0].settled_outcome_ref == (
        selected_candidate.candidate_result_ref
    )

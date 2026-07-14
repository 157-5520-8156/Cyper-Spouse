from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from companion_daemon.world_v2.affect_events import (
    AffectBaselineAdjustedPayload,
    AffectComponentDecay,
    AffectComponentUpdate,
    AffectEpisodeDecayedPayload,
    AffectEpisodeOpenedPayload,
    AffectEpisodeResolvedPayload,
    AffectEpisodeSupersededPayload,
    AffectEpisodeUpdatedPayload,
    affect_mutation_hash,
)
from companion_daemon.world_v2.affect_reducers import (
    adjust_affect_baseline,
    decay_affect_episode,
    open_affect_episode,
    resolve_affect_episode,
    supersede_affect_episode,
    update_affect_episode,
)
from companion_daemon.world_v2.schemas import (
    AffectComponentProjection,
    AffectDecayProfileProjection,
    AffectEpisodeProjection,
    AffectOrigin,
    AppraisalHypothesis,
    AppraisalMeaningRef,
    AppraisalOrigin,
    AppraisalProjection,
    EvidenceRef,
    affect_decay_config_digest,
)


NOW = datetime(2026, 7, 14, 10, 0, tzinfo=timezone.utc)
TABLE_DIGEST = "6a3abe39937394f738dcd1563189086020127b7e1e7189868c84ae3f890ee49f"


def evidence(ref_id: str = "message:1") -> EvidenceRef:
    return EvidenceRef(
        ref_id=ref_id,
        evidence_type="observed_message",
        claim_purpose="private_hypothesis",
    )


def meaning_ref(
    *, appraisal_id: str = "appraisal:1", hypothesis_id: str = "meaning:hurt"
) -> AppraisalMeaningRef:
    return AppraisalMeaningRef(
        appraisal_id=appraisal_id,
        hypothesis_id=hypothesis_id,
        source_cluster_ref="conversation:1",
        accepted_change_id="change:appraisal:1",
        accepted_transition_id="transition:appraisal:1",
    )


def appraisal(
    *,
    appraisal_id: str = "appraisal:1",
    change_id: str = "change:appraisal:1",
    transition_id: str = "transition:appraisal:1",
) -> AppraisalProjection:
    return AppraisalProjection(
        appraisal_id=appraisal_id,
        entity_revision=1,
        subject_ref="interaction:user:1",
        source_cluster_ref="conversation:1",
        origin=AppraisalOrigin(
            change_id=change_id,
            transition_id=transition_id,
            policy_refs=("policy:appraisal.1",),
            matrix_catalog_version="appraisal-matrix.1",
            clustering_policy_version="source-clustering.1",
            accepted_event_ref="event:appraisal:1",
        ),
        hypotheses=(
            AppraisalHypothesis(
                hypothesis_id="meaning:hurt",
                meaning="boundary_violation",
                attribution="user",
                controllability="controllable",
                severity="moderate",
                weight_bp=10_000,
            ),
        ),
        evidence_refs=(evidence(),),
        confidence_bp=8_000,
        accepted_at=NOW,
        expires_at=NOW + timedelta(hours=2),
    )


def profile() -> AffectDecayProfileProjection:
    config_digest = affect_decay_config_digest(
        kind="exponential_half_life",
        half_life_seconds=3_600,
        floor_bp=500,
        delay_seconds=60,
        config_version="affect-decay.1",
    )
    return AffectDecayProfileProjection(
        half_life_seconds=3_600,
        floor_bp=500,
        delay_seconds=60,
        config_version="affect-decay.1",
        table_digest=TABLE_DIGEST,
        config_digest=config_digest,
    )


def test_decay_profile_digest_freezes_all_parameters() -> None:
    with pytest.raises(ValidationError, match="config digest"):
        AffectDecayProfileProjection(
            half_life_seconds=1_800,
            floor_bp=500,
            delay_seconds=60,
            config_version="affect-decay.1",
            table_digest=TABLE_DIGEST,
            config_digest=profile().config_digest,
        )


def component(
    *,
    component_id: str = "component:hurt:1",
    intensity_bp: int = 4_000,
    at: datetime = NOW,
    refs: tuple[AppraisalMeaningRef, ...] | None = None,
    dimension: str = "hurt",
    source_cluster_ref: str = "conversation:1",
) -> AffectComponentProjection:
    return AffectComponentProjection(
        component_id=component_id,
        dimension=dimension,
        source_cluster_ref=source_cluster_ref,
        appraisal_refs=refs or (meaning_ref(),),
        intensity_bp=intensity_bp,
        decay_anchor_intensity_bp=intensity_bp,
        opened_at=at,
        decay_anchor_at=at,
        decay_not_before=at + timedelta(seconds=60),
        last_stimulus_at=at,
        last_updated_at=at,
        decay_profile=profile(),
        residue_bp=500,
    )


def episode(
    *,
    episode_id: str = "affect:1",
    at: datetime = NOW,
    supersedes_episode_id: str | None = None,
) -> AffectEpisodeProjection:
    return AffectEpisodeProjection(
        episode_id=episode_id,
        entity_revision=1,
        origin=AffectOrigin(
            change_id=f"change:{episode_id}",
            transition_id=f"transition:{episode_id}",
            policy_refs=("policy:affect.1",),
            matrix_catalog_version="affect-matrix.1",
            accepted_event_ref=f"event:{episode_id}",
        ),
        components=(component(component_id=f"component:{episode_id}", at=at),),
        evidence_refs=(evidence(),),
        opened_at=at,
        updated_at=at,
        status="active",
        supersedes_episode_id=supersedes_episode_id,
    )


def authorized_payload(model_type, **values):
    raw = {
        "change_id": values.pop("change_id"),
        "transition_id": values.pop("transition_id"),
        "expected_entity_revision": values.pop("expected_entity_revision"),
        "evidence_refs": tuple(values.pop("evidence_refs")),
        "appraisal_refs": tuple(values.pop("appraisal_refs", ())),
        "policy_refs": tuple(values.pop("policy_refs", ("policy:affect.1",))),
        "acceptance_id": "acceptance:affect:1",
        "proposal_id": "proposal:affect:1",
        "evaluated_world_revision": 7,
        "accepted_change_hash": "0" * 64,
        **values,
    }
    raw["accepted_change_hash"] = affect_mutation_hash(raw)
    return model_type.model_validate(raw)


def opened_payload(value: AffectEpisodeProjection | None = None) -> AffectEpisodeOpenedPayload:
    item = value or episode()
    appraisal_refs = tuple(
        ref for component_value in item.components for ref in component_value.appraisal_refs
    )
    return authorized_payload(
        AffectEpisodeOpenedPayload,
        change_id=item.origin.change_id,
        transition_id=item.origin.transition_id,
        expected_entity_revision=0,
        evidence_refs=item.evidence_refs,
        appraisal_refs=appraisal_refs,
        episode=item,
    )


def test_open_is_sourced_by_an_exact_accepted_appraisal_meaning() -> None:
    state = open_affect_episode(
        (),
        opened_payload(),
        appraisals=(appraisal(),),
        logical_time=NOW,
        merge_window_seconds=900,
    )

    assert state == (episode(),)
    assert state[0].entity_revision == 1

    bad_ref = meaning_ref(hypothesis_id="meaning:missing")
    bad_episode = episode().model_copy(update={"components": (component(refs=(bad_ref,)),)})
    bad_payload = opened_payload(bad_episode)
    with pytest.raises(ValueError, match="hypothesis"):
        open_affect_episode(
            (),
            bad_payload,
            appraisals=(appraisal(),),
            logical_time=NOW,
            merge_window_seconds=900,
        )

    with pytest.raises(ValidationError, match="Extra inputs"):
        AffectEpisodeOpenedPayload.model_validate(
            {**opened_payload().model_dump(), "affect_delta": {"hurt": 500}}
        )

    later = NOW + timedelta(minutes=5)
    merge_candidate = episode(episode_id="affect:merge", at=later)
    with pytest.raises(ValueError, match="merge-eligible"):
        open_affect_episode(
            state,
            opened_payload(merge_candidate),
            appraisals=(appraisal(),),
            logical_time=later,
            merge_window_seconds=900,
        )


def test_update_merges_explicit_delta_without_resetting_episode_time() -> None:
    active = open_affect_episode(
        (),
        opened_payload(),
        appraisals=(appraisal(),),
        logical_time=NOW,
        merge_window_seconds=900,
    )
    current = active[0].components[0]
    new_ref = meaning_ref(appraisal_id="appraisal:2").model_copy(
        update={
            "accepted_change_id": "change:appraisal:2",
            "accepted_transition_id": "transition:appraisal:2",
        }
    )
    second_appraisal = appraisal(
        appraisal_id="appraisal:2",
        change_id="change:appraisal:2",
        transition_id="transition:appraisal:2",
    )
    updated_component = current.model_copy(
        update={
            "intensity_bp": 4_500,
            "decay_anchor_intensity_bp": 4_500,
            "decay_anchor_at": NOW,
            "last_stimulus_at": NOW,
            "last_updated_at": NOW,
            "appraisal_refs": (meaning_ref(), new_ref),
        }
    )
    change = AffectComponentUpdate(
        component_id=current.component_id,
        before_intensity_bp=4_000,
        proposed_delta_bp=1_000,
        accepted_delta_bp=500,
        after_intensity_bp=4_500,
        appraisal_refs=(new_ref,),
        updated_component=updated_component,
    )
    payload = authorized_payload(
        AffectEpisodeUpdatedPayload,
        change_id="change:affect:update:1",
        transition_id="transition:affect:update:1",
        expected_entity_revision=1,
        evidence_refs=(evidence(),),
        appraisal_refs=(new_ref,),
        episode_id="affect:1",
        updated_at=NOW,
        component_updates=(change,),
    )

    updated = update_affect_episode(
        active,
        payload,
        appraisals=(appraisal(), second_appraisal),
        logical_time=NOW,
        merge_window_seconds=900,
    )

    assert updated[0].entity_revision == 2
    assert updated[0].opened_at == NOW
    assert updated[0].components[0].intensity_bp == 4_500

    stale_time_payload = payload.model_copy(update={"updated_at": NOW + timedelta(seconds=1)})
    with pytest.raises(ValueError, match="times"):
        update_affect_episode(
            active,
            stale_time_payload,
            appraisals=(appraisal(), second_appraisal),
            logical_time=NOW + timedelta(seconds=1),
            merge_window_seconds=900,
        )


def test_update_materializes_from_the_stable_anchor_without_a_decay_event() -> None:
    active = (episode(),)
    current = active[0].components[0]
    later = NOW + timedelta(minutes=10)
    before = 3_654
    after = before + 400
    new_ref = meaning_ref(appraisal_id="appraisal:2").model_copy(
        update={
            "accepted_change_id": "change:appraisal:2",
            "accepted_transition_id": "transition:appraisal:2",
        }
    )
    second_appraisal = appraisal(
        appraisal_id="appraisal:2",
        change_id="change:appraisal:2",
        transition_id="transition:appraisal:2",
    )
    updated_component = current.model_copy(
        update={
            "intensity_bp": after,
            "decay_anchor_intensity_bp": after,
            "decay_anchor_at": later,
            "last_stimulus_at": later,
            "last_updated_at": later,
            "appraisal_refs": (meaning_ref(), new_ref),
        }
    )
    change = AffectComponentUpdate(
        component_id=current.component_id,
        before_intensity_bp=before,
        proposed_delta_bp=600,
        accepted_delta_bp=400,
        after_intensity_bp=after,
        appraisal_refs=(new_ref,),
        updated_component=updated_component,
    )
    payload = authorized_payload(
        AffectEpisodeUpdatedPayload,
        change_id="change:affect:update:later",
        transition_id="transition:affect:update:later",
        expected_entity_revision=1,
        evidence_refs=(evidence(),),
        appraisal_refs=(new_ref,),
        episode_id="affect:1",
        updated_at=later,
        component_updates=(change,),
    )

    updated = update_affect_episode(
        active,
        payload,
        appraisals=(appraisal(), second_appraisal),
        logical_time=later,
        merge_window_seconds=900,
    )

    assert updated[0].components[0].intensity_bp == after
    assert updated[0].components[0].decay_anchor_at == later


def test_mixed_update_does_not_turn_materialization_into_a_second_stimulus() -> None:
    anger = component(component_id="component:anger:1", dimension="anger")
    active = (episode().model_copy(update={"components": (component(), anger)}),)
    later = NOW + timedelta(minutes=10)
    new_ref = meaning_ref(appraisal_id="appraisal:2").model_copy(
        update={
            "accepted_change_id": "change:appraisal:2",
            "accepted_transition_id": "transition:appraisal:2",
        }
    )
    second_appraisal = appraisal(
        appraisal_id="appraisal:2",
        change_id="change:appraisal:2",
        transition_id="transition:appraisal:2",
    )
    hurt_after = (
        active[0]
        .components[0]
        .model_copy(
            update={
                "intensity_bp": 4_054,
                "decay_anchor_intensity_bp": 4_054,
                "decay_anchor_at": later,
                "last_stimulus_at": later,
                "last_updated_at": later,
                "appraisal_refs": (meaning_ref(), new_ref),
            }
        )
    )
    anger_after = anger.model_copy(update={"intensity_bp": 3_654, "last_updated_at": later})
    changes = (
        AffectComponentUpdate(
            component_id=active[0].components[0].component_id,
            operation="stimulus",
            before_intensity_bp=3_654,
            proposed_delta_bp=600,
            accepted_delta_bp=400,
            after_intensity_bp=4_054,
            appraisal_refs=(new_ref,),
            updated_component=hurt_after,
        ),
        AffectComponentUpdate(
            component_id=anger.component_id,
            operation="materialize",
            before_intensity_bp=3_654,
            proposed_delta_bp=0,
            accepted_delta_bp=0,
            after_intensity_bp=3_654,
            appraisal_refs=(),
            updated_component=anger_after,
        ),
    )
    payload = authorized_payload(
        AffectEpisodeUpdatedPayload,
        change_id="change:affect:mixed",
        transition_id="transition:affect:mixed",
        expected_entity_revision=1,
        evidence_refs=(evidence(),),
        appraisal_refs=(new_ref,),
        episode_id="affect:1",
        updated_at=later,
        component_updates=changes,
    )

    updated = update_affect_episode(
        active,
        payload,
        appraisals=(appraisal(), second_appraisal),
        logical_time=later,
        merge_window_seconds=900,
    )

    materialized = updated[0].components[1]
    assert materialized.decay_anchor_at == NOW
    assert materialized.last_stimulus_at == NOW
    assert materialized.appraisal_refs == anger.appraisal_refs

    tampered = anger_after.model_copy(update={"residue_bp": 700})
    bad_change = changes[1].model_copy(update={"updated_component": tampered})
    bad_payload = payload.model_copy(update={"component_updates": (changes[0], bad_change)})
    with pytest.raises(ValueError, match="only change intensity"):
        update_affect_episode(
            active,
            bad_payload,
            appraisals=(appraisal(), second_appraisal),
            logical_time=later,
            merge_window_seconds=900,
        )


def test_decay_uses_fixed_point_math_without_moving_anchor() -> None:
    active = (episode(),)
    target = NOW + timedelta(hours=1)
    payload = AffectEpisodeDecayedPayload(
        change_id="change:decay:1",
        transition_id="transition:decay:1",
        expected_entity_revision=1,
        evidence_refs=(
            EvidenceRef(
                ref_id=f"clock:{target.isoformat()}",
                evidence_type="clock_observation",
                claim_purpose="current_fact",
            ),
        ),
        policy_refs=("policy:affect.1",),
        episode_id="affect:1",
        from_logical_time=NOW,
        to_logical_time=target,
        component_results=(
            AffectComponentDecay(
                component_id="component:affect:1",
                before_intensity_bp=4_000,
                after_intensity_bp=2_270,
                config_version="affect-decay.1",
                table_digest=TABLE_DIGEST,
                config_digest=profile().config_digest,
            ),
        ),
    )

    decayed = decay_affect_episode(
        active,
        payload,
        logical_time=target,
    )

    assert decayed[0].entity_revision == 2
    assert decayed[0].components[0].intensity_bp == 2_270
    assert decayed[0].components[0].decay_anchor_intensity_bp == 4_000
    assert decayed[0].components[0].decay_anchor_at == NOW
    assert decayed[0].status == "active"


def test_baseline_requires_explicit_multi_scene_calibration_and_cas() -> None:
    resolved = []
    evidence_refs = []
    basis_refs = []
    for index, days_ago in enumerate((9, 5, 1), start=1):
        opened = NOW - timedelta(days=days_ago)
        source = EvidenceRef(
            ref_id=f"operator:source:{index}",
            evidence_type="operator_observation",
            claim_purpose="private_hypothesis",
            immutable_hash=str(index) * 64,
        )
        resolution = EvidenceRef(
            ref_id=f"operator:resolution:{index}",
            evidence_type="operator_observation",
            claim_purpose="private_hypothesis",
            immutable_hash=str(index + 3) * 64,
        )
        item = episode(episode_id=f"affect:calibration:{index}", at=opened).model_copy(
            update={
                "entity_revision": 2,
                "evidence_refs": (source,),
                "updated_at": opened + timedelta(hours=1),
                "status": "resolved",
                "closed_at": opened + timedelta(hours=1),
                "resolution_refs": (resolution,),
            }
        )
        calibrated_component = item.components[0].model_copy(
            update={
                "source_cluster_ref": f"scene:calibration:{index}",
                "appraisal_refs": (
                    item.components[0].appraisal_refs[0].model_copy(
                        update={"source_cluster_ref": f"scene:calibration:{index}"}
                    ),
                ),
            }
        )
        item = item.model_copy(update={"components": (calibrated_component,)})
        resolved.append(item)
        evidence_refs.extend((source, resolution))
        basis_refs.append(
            {
                "episode_id": item.episode_id,
                "terminal_entity_revision": 2,
                "component_id": item.components[0].component_id,
            }
        )
    payload = authorized_payload(
        AffectBaselineAdjustedPayload,
        change_id="change:baseline:hurt:1",
        transition_id="transition:baseline:hurt:1",
        expected_entity_revision=0,
        evidence_refs=tuple(evidence_refs),
        policy_refs=("policy:affect-baseline-v1",),
        dimension="hurt",
        baseline_before_bp=0,
        proposed_delta_bp=300,
        accepted_delta_bp=200,
        baseline_after_bp=200,
        calibration_policy_version="affect-baseline-calibration.1",
        calibration_window_from=NOW - timedelta(days=10),
        calibration_window_to=NOW,
        basis_episode_refs=tuple(basis_refs),
    )

    adjusted = adjust_affect_baseline((), tuple(resolved), payload, logical_time=NOW)

    assert adjusted[0].baseline_bp == 200
    assert adjusted[0].calibration_revision == 1
    with pytest.raises(ValueError, match="stale"):
        adjust_affect_baseline(adjusted, tuple(resolved), payload, logical_time=NOW)


def test_baseline_span_requires_distinct_scenes_across_seven_days() -> None:
    resolved = []
    evidence_refs = []
    basis_refs = []
    for index, hour_offset in enumerate((0, 1, 2), start=1):
        opened = NOW - timedelta(days=9) + timedelta(hours=hour_offset)
        closed = NOW - timedelta(days=1) if index == 1 else opened + timedelta(hours=1)
        source = EvidenceRef(
            ref_id=f"operator:compressed-source:{index}",
            evidence_type="operator_observation",
            claim_purpose="private_hypothesis",
            immutable_hash=str(index) * 64,
        )
        resolution = EvidenceRef(
            ref_id=f"operator:compressed-resolution:{index}",
            evidence_type="operator_observation",
            claim_purpose="private_hypothesis",
            immutable_hash=str(index + 3) * 64,
        )
        item = episode(episode_id=f"affect:compressed:{index}", at=opened).model_copy(
            update={
                "entity_revision": 2,
                "evidence_refs": (source,),
                "updated_at": closed,
                "status": "resolved",
                "closed_at": closed,
                "resolution_refs": (resolution,),
            }
        )
        cluster = f"scene:compressed:{index}"
        calibrated_component = item.components[0].model_copy(
            update={
                "source_cluster_ref": cluster,
                "appraisal_refs": (
                    item.components[0].appraisal_refs[0].model_copy(
                        update={"source_cluster_ref": cluster}
                    ),
                ),
            }
        )
        item = item.model_copy(update={"components": (calibrated_component,)})
        resolved.append(item)
        evidence_refs.extend((source, resolution))
        basis_refs.append(
            {
                "episode_id": item.episode_id,
                "terminal_entity_revision": 2,
                "component_id": calibrated_component.component_id,
            }
        )
    payload = authorized_payload(
        AffectBaselineAdjustedPayload,
        change_id="change:baseline:hurt:compressed",
        transition_id="transition:baseline:hurt:compressed",
        expected_entity_revision=0,
        evidence_refs=tuple(evidence_refs),
        policy_refs=("policy:affect-baseline-v1",),
        dimension="hurt",
        baseline_before_bp=0,
        proposed_delta_bp=200,
        accepted_delta_bp=100,
        baseline_after_bp=100,
        calibration_policy_version="affect-baseline-calibration.1",
        calibration_window_from=NOW - timedelta(days=10),
        calibration_window_to=NOW,
        basis_episode_refs=tuple(basis_refs),
    )

    with pytest.raises(ValueError, match="span seven days"):
        adjust_affect_baseline((), tuple(resolved), payload, logical_time=NOW)


def test_baseline_requires_three_distinct_source_clusters() -> None:
    resolved = []
    evidence_refs = []
    basis_refs = []
    for index, days_ago in enumerate((9, 5, 1), start=1):
        opened = NOW - timedelta(days=days_ago)
        source = EvidenceRef(
            ref_id=f"operator:shared-source:{index}",
            evidence_type="operator_observation",
            claim_purpose="private_hypothesis",
            immutable_hash=str(index) * 64,
        )
        resolution = EvidenceRef(
            ref_id=f"operator:shared-resolution:{index}",
            evidence_type="operator_observation",
            claim_purpose="private_hypothesis",
            immutable_hash=str(index + 3) * 64,
        )
        item = episode(episode_id=f"affect:shared:{index}", at=opened).model_copy(
            update={
                "entity_revision": 2,
                "evidence_refs": (source,),
                "updated_at": opened + timedelta(hours=1),
                "status": "resolved",
                "closed_at": opened + timedelta(hours=1),
                "resolution_refs": (resolution,),
            }
        )
        calibrated_component = item.components[0].model_copy(
            update={
                "source_cluster_ref": "scene:shared",
                "appraisal_refs": (
                    item.components[0].appraisal_refs[0].model_copy(
                        update={"source_cluster_ref": "scene:shared"}
                    ),
                ),
            }
        )
        item = item.model_copy(update={"components": (calibrated_component,)})
        resolved.append(item)
        evidence_refs.extend((source, resolution))
        basis_refs.append(
            {
                "episode_id": item.episode_id,
                "terminal_entity_revision": 2,
                "component_id": calibrated_component.component_id,
            }
        )
    payload = authorized_payload(
        AffectBaselineAdjustedPayload,
        change_id="change:baseline:hurt:shared",
        transition_id="transition:baseline:hurt:shared",
        expected_entity_revision=0,
        evidence_refs=tuple(evidence_refs),
        policy_refs=("policy:affect-baseline-v1",),
        dimension="hurt",
        baseline_before_bp=0,
        proposed_delta_bp=200,
        accepted_delta_bp=100,
        baseline_after_bp=100,
        calibration_policy_version="affect-baseline-calibration.1",
        calibration_window_from=NOW - timedelta(days=10),
        calibration_window_to=NOW,
        basis_episode_refs=tuple(basis_refs),
    )

    with pytest.raises(ValueError, match="independent source clusters"):
        adjust_affect_baseline((), tuple(resolved), payload, logical_time=NOW)


def test_baseline_rejects_boundary_clamping_as_a_semantic_noop() -> None:
    with pytest.raises(ValidationError, match="exceeds the valid range"):
        authorized_payload(
            AffectBaselineAdjustedPayload,
            change_id="change:baseline:hurt:boundary",
            transition_id="transition:baseline:hurt:boundary",
            expected_entity_revision=1,
            evidence_refs=(evidence(),),
            dimension="hurt",
            baseline_before_bp=10_000,
            proposed_delta_bp=200,
            accepted_delta_bp=100,
            baseline_after_bp=10_000,
            calibration_policy_version="affect-baseline-calibration.1",
            calibration_window_from=NOW - timedelta(days=8),
            calibration_window_to=NOW,
            basis_episode_refs=(
                {
                    "episode_id": "affect:calibration:boundary",
                    "terminal_entity_revision": 2,
                    "component_id": "component:affect:calibration:boundary",
                },
            ),
        )


def test_resolve_is_explicit_sourced_and_terminal() -> None:
    resolution = evidence("message:repair")
    at = NOW + timedelta(minutes=5)
    payload = authorized_payload(
        AffectEpisodeResolvedPayload,
        change_id="change:resolve:1",
        transition_id="transition:resolve:1",
        expected_entity_revision=1,
        evidence_refs=(resolution,),
        episode_id="affect:1",
        resolved_at=at,
        resolution_refs=(resolution,),
        reason_code="reappraised_and_resolved",
    )

    resolved = resolve_affect_episode((episode(),), payload, logical_time=at)

    assert resolved[0].status == "resolved"
    assert resolved[0].closed_at == at
    assert resolved[0].components == episode().components
    with pytest.raises(ValueError, match="active"):
        resolve_affect_episode(
            resolved,
            payload.model_copy(update={"expected_entity_revision": 2}),
            logical_time=at,
        )


def test_supersede_atomically_closes_old_and_opens_linked_successor() -> None:
    new_ref = meaning_ref(appraisal_id="appraisal:2").model_copy(
        update={
            "accepted_change_id": "change:appraisal:2",
            "accepted_transition_id": "transition:appraisal:2",
        }
    )
    second_appraisal = appraisal(
        appraisal_id="appraisal:2",
        change_id="change:appraisal:2",
        transition_id="transition:appraisal:2",
    )
    successor = episode(
        episode_id="affect:2",
        at=NOW + timedelta(minutes=10),
        supersedes_episode_id="affect:1",
    ).model_copy(
        update={
            "components": (
                component(
                    component_id="component:affect:2",
                    at=NOW + timedelta(minutes=10),
                    refs=(new_ref,),
                ),
            )
        }
    )
    payload = authorized_payload(
        AffectEpisodeSupersededPayload,
        change_id=successor.origin.change_id,
        transition_id=successor.origin.transition_id,
        expected_entity_revision=1,
        evidence_refs=successor.evidence_refs,
        appraisal_refs=(new_ref,),
        episode_id="affect:1",
        superseded_at=successor.opened_at,
        successor=successor,
    )

    result = supersede_affect_episode(
        (episode(),),
        payload,
        appraisals=(appraisal(), second_appraisal),
        logical_time=successor.opened_at,
        merge_window_seconds=0,
    )

    assert [(item.episode_id, item.status) for item in result] == [
        ("affect:1", "superseded"),
        ("affect:2", "active"),
    ]
    assert result[0].superseded_by_episode_id == "affect:2"
    assert result[1].supersedes_episode_id == "affect:1"


def test_authorized_hash_covers_the_complete_affect_transition() -> None:
    payload = opened_payload()
    raw = payload.model_dump()
    raw["episode"]["components"][0]["intensity_bp"] = 4_001

    with pytest.raises(ValidationError, match="hash"):
        AffectEpisodeOpenedPayload.model_validate(raw)

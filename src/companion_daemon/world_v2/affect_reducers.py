"""Pure deterministic reducers for Affect episode authority."""

from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import json

from .affect_events import (
    AffectBaselineAdjustedPayload,
    AffectEpisodeDecayedPayload,
    AffectEpisodeOpenedPayload,
    AffectEpisodeResolvedPayload,
    AffectEpisodeSupersededPayload,
    AffectEpisodeUpdatedPayload,
)
from .affect_math import (
    ALGORITHM_VERSION,
    FACTOR_TABLE_DIGEST,
    DecayAnchor,
    DecayProfile,
    decay_intensity_bp,
)
from .schemas import (
    AffectBaselineProjection,
    AffectComponentProjection,
    AffectEpisodeProjection,
    AppraisalMeaningRef,
    AppraisalProjection,
)


def adjust_affect_baseline(
    baselines: tuple[AffectBaselineProjection, ...],
    episodes: tuple[AffectEpisodeProjection, ...],
    payload: AffectBaselineAdjustedPayload,
    *,
    logical_time: datetime,
) -> tuple[AffectBaselineProjection, ...]:
    """Apply only an explicitly accepted cross-scene baseline calibration."""

    matches = [
        (index, item) for index, item in enumerate(baselines) if item.dimension == payload.dimension
    ]
    if len(matches) > 1:
        raise ValueError("duplicate affect baseline authority")
    if matches:
        index, current = matches[0]
        before = current.baseline_bp
        revision = current.calibration_revision
    else:
        index, before, revision = None, 0, 0
    _aware_logical_time(logical_time)
    if payload.calibration_policy_version != "affect-baseline-calibration.1":
        raise ValueError("uninstalled affect baseline calibration policy")
    if abs(payload.accepted_delta_bp) > 250:
        raise ValueError("affect baseline delta exceeds calibration policy")
    if payload.calibration_window_to > logical_time:
        raise ValueError("baseline calibration window exceeds logical time")
    if payload.calibration_window_to - payload.calibration_window_from < timedelta(days=7):
        raise ValueError("baseline calibration window is too short")
    if any(
        episode.status == "active"
        and any(component.dimension == payload.dimension for component in episode.components)
        for episode in episodes
    ):
        raise ValueError("baseline calibration requires a quiescent affect dimension")
    basis_episodes: list[AffectEpisodeProjection] = []
    evidence_closure = []
    for basis in payload.basis_episode_refs:
        episode = next((item for item in episodes if item.episode_id == basis.episode_id), None)
        if (
            episode is None
            or episode.status != "resolved"
            or episode.entity_revision != basis.terminal_entity_revision
            or episode.closed_at is None
            or episode.opened_at < payload.calibration_window_from
            or episode.closed_at > payload.calibration_window_to
            or not any(
                item.component_id == basis.component_id and item.dimension == payload.dimension
                for item in episode.components
            )
        ):
            raise ValueError("baseline calibration basis does not resolve")
        basis_episodes.append(episode)
        for evidence in (*episode.evidence_refs, *episode.resolution_refs):
            if evidence not in evidence_closure:
                evidence_closure.append(evidence)
    if len({item.episode_id for item in basis_episodes}) < 3:
        raise ValueError("baseline calibration requires three resolved episodes")
    if max(item.opened_at for item in basis_episodes) - min(
        item.opened_at for item in basis_episodes
    ) < timedelta(days=7):
        raise ValueError("baseline episode history does not span seven days")
    basis_clusters = {
        component.source_cluster_ref
        for episode, basis in zip(
            basis_episodes, payload.basis_episode_refs, strict=True
        )
        for component in episode.components
        if component.component_id == basis.component_id
    }
    if len(basis_clusters) < 3:
        raise ValueError("baseline calibration requires three independent source clusters")
    if tuple(evidence_closure) != payload.evidence_refs:
        raise ValueError("baseline calibration evidence does not match episode closure")
    if revision != payload.expected_entity_revision or before != payload.baseline_before_bp:
        raise ValueError("stale affect baseline calibration")
    if matches and payload.calibration_window_from <= current.calibrated_through:
        raise ValueError("baseline calibration window overlaps consumed history")
    basis_hash = hashlib.sha256(
        json.dumps(
            [item.model_dump(mode="json") for item in payload.basis_episode_refs],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    adjusted = AffectBaselineProjection(
        dimension=payload.dimension,
        baseline_bp=payload.baseline_after_bp,
        calibration_revision=revision + 1,
        policy_version=payload.calibration_policy_version,
        last_calibrated_at=logical_time,
        calibrated_through=payload.calibration_window_to,
        last_calibration_basis_hash=basis_hash,
    )
    if index is None:
        return (*baselines, adjusted)
    values = list(baselines)
    values[index] = adjusted
    return tuple(values)


def open_affect_episode(
    episodes: tuple[AffectEpisodeProjection, ...],
    payload: AffectEpisodeOpenedPayload,
    *,
    appraisals: tuple[AppraisalProjection, ...],
    logical_time: datetime,
    merge_window_seconds: int,
    baselines: tuple[AffectBaselineProjection, ...] = (),
) -> tuple[AffectEpisodeProjection, ...]:
    """Open one independently accepted, appraisal-sourced Affect episode."""

    _aware_logical_time(logical_time)
    _valid_merge_window(merge_window_seconds)
    candidate = payload.episode
    if any(item.episode_id == candidate.episode_id for item in episodes):
        raise ValueError(f"affect episode {candidate.episode_id!r} already exists")
    if candidate.opened_at != logical_time or candidate.updated_at != logical_time:
        raise ValueError("affect episode opening must equal authoritative logical time")
    if any(
        not (
            component.opened_at
            == component.decay_anchor_at
            == component.last_stimulus_at
            == component.last_updated_at
            == logical_time
        )
        for component in candidate.components
    ):
        raise ValueError("new affect components must anchor at opening logical time")
    if any(
        component.decay_not_before
        != component.opened_at + _seconds(component.decay_profile.delay_seconds)
        for component in candidate.components
    ):
        raise ValueError("new affect component delay anchor is invalid")
    _resolve_appraisal_refs(appraisals, payload.appraisal_refs)
    _validate_baseline_bounds(candidate, baselines)
    _reject_merge_eligible_open(
        episodes,
        candidate,
        logical_time=logical_time,
        merge_window_seconds=merge_window_seconds,
    )
    _require_novel_causal_meaning(episodes, payload.appraisal_refs)
    return (*episodes, candidate)


def update_affect_episode(
    episodes: tuple[AffectEpisodeProjection, ...],
    payload: AffectEpisodeUpdatedPayload,
    *,
    appraisals: tuple[AppraisalProjection, ...],
    logical_time: datetime,
    merge_window_seconds: int,
    baselines: tuple[AffectBaselineProjection, ...] = (),
) -> tuple[AffectEpisodeProjection, ...]:
    """Merge an accepted stimulus without silently resetting decay delay."""

    _aware_logical_time(logical_time)
    _valid_merge_window(merge_window_seconds)
    index, current = _active_episode(episodes, payload.episode_id, payload.expected_entity_revision)
    if payload.updated_at != logical_time:
        raise ValueError("affect update must equal authoritative logical time")
    _resolve_appraisal_refs(appraisals, payload.appraisal_refs)
    _require_novel_causal_meaning(episodes, payload.appraisal_refs)

    components = list(current.components)
    component_index = {item.component_id: offset for offset, item in enumerate(components)}
    if {item.component_id for item in payload.component_updates} != set(component_index):
        raise ValueError("affect update must materialize every episode component")
    for change in payload.component_updates:
        offset = component_index.get(change.component_id)
        if offset is None:
            raise ValueError(f"unknown affect component {change.component_id!r}")
        before = components[offset]
        after = change.updated_component
        expected_before = _materialized_intensity(
            before, logical_time=logical_time, baselines=baselines
        )
        if expected_before != change.before_intensity_bp:
            raise ValueError("affect component before intensity is stale")
        if (
            after.component_id != before.component_id
            or after.dimension != before.dimension
            or after.source_cluster_ref != before.source_cluster_ref
            or after.opened_at != before.opened_at
        ):
            raise ValueError("affect update cannot change component identity or source")
        if after.decay_profile.delay_seconds != before.decay_profile.delay_seconds:
            raise ValueError("affect update cannot reset the decay delay")
        if after.decay_not_before != before.decay_not_before:
            raise ValueError("affect update cannot move decay_not_before")
        if change.operation == "stimulus":
            if logical_time - before.last_stimulus_at > _seconds(merge_window_seconds):
                raise ValueError("affect component is outside its merge window")
            if not (
                after.decay_anchor_at
                == after.last_stimulus_at
                == after.last_updated_at
                == logical_time
            ):
                raise ValueError("updated affect component times must equal logical time")
        elif (
            after.decay_anchor_at != before.decay_anchor_at
            or after.decay_anchor_intensity_bp != before.decay_anchor_intensity_bp
            or after.last_stimulus_at != before.last_stimulus_at
            or after.last_updated_at != logical_time
        ):
            raise ValueError("materialization cannot move stimulus or decay anchor")
        if change.operation == "materialize":
            before_frozen = before.model_dump(exclude={"intensity_bp", "last_updated_at"})
            after_frozen = after.model_dump(exclude={"intensity_bp", "last_updated_at"})
            if after_frozen != before_frozen:
                raise ValueError("materialization can only change intensity and update time")
        expected_refs = _append_meaning_refs(before.appraisal_refs, change.appraisal_refs)
        if after.appraisal_refs != expected_refs:
            raise ValueError("updated component must retain its appraisal lineage")
        components[offset] = _validated_component(after)

    updated = _validated_episode(
        current,
        entity_revision=current.entity_revision + 1,
        components=tuple(components),
        updated_at=logical_time,
    )
    _validate_baseline_bounds(updated, baselines)
    return _replace(episodes, index, updated)


def decay_affect_episode(
    episodes: tuple[AffectEpisodeProjection, ...],
    payload: AffectEpisodeDecayedPayload,
    *,
    logical_time: datetime,
    baselines: tuple[AffectBaselineProjection, ...] = (),
) -> tuple[AffectEpisodeProjection, ...]:
    """Materialize fixed-point decay while preserving the stable decay anchor."""

    _aware_logical_time(logical_time)
    index, current = _active_episode(episodes, payload.episode_id, payload.expected_entity_revision)
    if payload.to_logical_time != logical_time:
        raise ValueError("affect decay target must equal authoritative logical time")
    if payload.from_logical_time != current.updated_at:
        raise ValueError("affect decay source time is stale")
    results = {item.component_id: item for item in payload.component_results}
    if set(results) != {item.component_id for item in current.components}:
        raise ValueError("affect decay must materialize every episode component")

    components: list[AffectComponentProjection] = []
    changed = False
    for component in current.components:
        result = results[component.component_id]
        if result.before_intensity_bp != component.intensity_bp:
            raise ValueError("affect decay before intensity is stale")
        if (
            result.config_version != component.decay_profile.config_version
            or result.table_digest != component.decay_profile.table_digest
            or result.config_digest != component.decay_profile.config_digest
        ):
            raise ValueError("affect decay profile does not match the component")
        profile = component.decay_profile
        if (
            profile.algorithm_version != ALGORITHM_VERSION
            or profile.table_digest != FACTOR_TABLE_DIGEST
        ):
            raise ValueError("affect decay profile uses an uninstalled algorithm")
        calculated = _materialized_intensity(
            component,
            logical_time=payload.to_logical_time,
            baselines=baselines,
        )
        if calculated != result.after_intensity_bp:
            raise ValueError("affect decay result does not match fixed-point math")
        lower_bound = max(component.decay_profile.floor_bp, component.residue_bp)
        if not lower_bound <= calculated <= component.intensity_bp:
            raise ValueError("affect decay must move monotonically toward its lower bound")
        changed = changed or calculated != component.intensity_bp
        components.append(
            _validated_component(
                component,
                intensity_bp=calculated,
                last_updated_at=logical_time,
            )
        )
    if not changed:
        raise ValueError("AffectEpisodeDecayed requires a materialized intensity change")

    updated = _validated_episode(
        current,
        entity_revision=current.entity_revision + 1,
        components=tuple(components),
        updated_at=logical_time,
    )
    return _replace(episodes, index, updated)


def resolve_affect_episode(
    episodes: tuple[AffectEpisodeProjection, ...],
    payload: AffectEpisodeResolvedPayload,
    *,
    logical_time: datetime,
) -> tuple[AffectEpisodeProjection, ...]:
    """Explicitly close an episode; visible expression never calls this implicitly."""

    _aware_logical_time(logical_time)
    index, current = _active_episode(episodes, payload.episode_id, payload.expected_entity_revision)
    if payload.resolved_at != logical_time:
        raise ValueError("affect resolution must equal authoritative logical time")
    if logical_time < current.updated_at:
        raise ValueError("affect resolution cannot precede its current state")
    resolved = _validated_episode(
        current,
        entity_revision=current.entity_revision + 1,
        updated_at=logical_time,
        status="resolved",
        closed_at=logical_time,
        resolution_refs=payload.resolution_refs,
    )
    return _replace(episodes, index, resolved)


def supersede_affect_episode(
    episodes: tuple[AffectEpisodeProjection, ...],
    payload: AffectEpisodeSupersededPayload,
    *,
    appraisals: tuple[AppraisalProjection, ...],
    logical_time: datetime,
    merge_window_seconds: int,
    baselines: tuple[AffectBaselineProjection, ...] = (),
) -> tuple[AffectEpisodeProjection, ...]:
    """Atomically close one episode and install its explicitly linked successor."""

    _aware_logical_time(logical_time)
    _valid_merge_window(merge_window_seconds)
    index, current = _active_episode(episodes, payload.episode_id, payload.expected_entity_revision)
    if payload.superseded_at != logical_time:
        raise ValueError("affect supersede time must equal authoritative logical time")
    successor = payload.successor
    if successor.opened_at != logical_time or successor.updated_at != logical_time:
        raise ValueError("successor affect episode must open at supersede logical time")
    if any(item.episode_id == successor.episode_id for item in episodes):
        raise ValueError(f"affect episode {successor.episode_id!r} already exists")
    _resolve_appraisal_refs(appraisals, payload.appraisal_refs)
    if any(
        not (
            component.opened_at
            == component.decay_anchor_at
            == component.last_stimulus_at
            == component.last_updated_at
            == logical_time
        )
        for component in successor.components
    ):
        raise ValueError("successor affect components must anchor at logical time")
    if any(
        component.decay_not_before
        != component.opened_at + _seconds(component.decay_profile.delay_seconds)
        for component in successor.components
    ):
        raise ValueError("successor affect component delay anchor is invalid")
    _require_novel_causal_meaning(episodes, payload.appraisal_refs)
    _validate_baseline_bounds(successor, baselines)
    _reject_merge_eligible_open(
        tuple(item for item in episodes if item.episode_id != current.episode_id),
        successor,
        logical_time=logical_time,
        merge_window_seconds=merge_window_seconds,
    )
    closed = _validated_episode(
        current,
        entity_revision=current.entity_revision + 1,
        updated_at=logical_time,
        status="superseded",
        closed_at=logical_time,
        superseded_by_episode_id=successor.episode_id,
    )
    return (*_replace(episodes, index, closed), successor)


def _resolve_appraisal_refs(
    appraisals: tuple[AppraisalProjection, ...],
    refs: tuple[AppraisalMeaningRef, ...],
) -> None:
    for ref in refs:
        matches = [item for item in appraisals if item.appraisal_id == ref.appraisal_id]
        if len(matches) != 1:
            raise ValueError(f"unknown or duplicate appraisal {ref.appraisal_id!r}")
        appraisal = matches[0]
        if appraisal.status != "active":
            raise ValueError("new affect causation requires an active appraisal")
        if (
            appraisal.source_cluster_ref != ref.source_cluster_ref
            or appraisal.origin.change_id != ref.accepted_change_id
            or appraisal.origin.transition_id != ref.accepted_transition_id
        ):
            raise ValueError("affect appraisal meaning reference does not match authority")
        if not any(item.hypothesis_id == ref.hypothesis_id for item in appraisal.hypotheses):
            raise ValueError(f"unknown appraisal hypothesis {ref.hypothesis_id!r}")


def _reject_merge_eligible_open(
    episodes: tuple[AffectEpisodeProjection, ...],
    candidate: AffectEpisodeProjection,
    *,
    logical_time: datetime,
    merge_window_seconds: int,
) -> None:
    cutoff = _seconds(merge_window_seconds)
    active_components = (
        component
        for episode in episodes
        if episode.status == "active"
        for component in episode.components
    )
    existing = tuple(active_components)
    for component in candidate.components:
        eligible = [
            item
            for item in existing
            if (item.dimension, item.source_cluster_ref)
            == (component.dimension, component.source_cluster_ref)
            and logical_time - item.last_stimulus_at <= cutoff
        ]
        if eligible:
            raise ValueError("merge-eligible affect component requires an update")


def _active_episode(
    episodes: tuple[AffectEpisodeProjection, ...], episode_id: str, expected_revision: int
) -> tuple[int, AffectEpisodeProjection]:
    matches = [
        (index, item) for index, item in enumerate(episodes) if item.episode_id == episode_id
    ]
    if not matches:
        raise ValueError(f"unknown affect episode {episode_id!r}")
    if len(matches) != 1:
        raise ValueError(f"duplicate affect episode {episode_id!r}")
    index, episode = matches[0]
    if episode.entity_revision != expected_revision:
        raise ValueError(
            f"stale affect revision: expected {expected_revision}, found {episode.entity_revision}"
        )
    if episode.status != "active":
        raise ValueError("only an active affect episode can transition")
    return index, episode


def _require_novel_causal_meaning(
    episodes: tuple[AffectEpisodeProjection, ...],
    proposed_refs: tuple[AppraisalMeaningRef, ...],
) -> None:
    consumed = {
        ref
        for episode in episodes
        for component in episode.components
        for ref in component.appraisal_refs
    }
    if not set(proposed_refs) - consumed:
        raise ValueError("affect transition requires a novel appraisal cause")


def _validate_baseline_bounds(
    episode: AffectEpisodeProjection,
    baselines: tuple[AffectBaselineProjection, ...],
) -> None:
    for component in episode.components:
        baseline = next(
            (item.baseline_bp for item in baselines if item.dimension == component.dimension),
            0,
        )
        if min(component.intensity_bp, component.decay_anchor_intensity_bp) < baseline:
            raise ValueError("affect component cannot be below its baseline")


def _append_meaning_refs(
    existing: tuple[AppraisalMeaningRef, ...],
    additions: tuple[AppraisalMeaningRef, ...],
) -> tuple[AppraisalMeaningRef, ...]:
    result = list(existing)
    for addition in additions:
        if addition not in result:
            result.append(addition)
    return tuple(result)


def _validated_component(
    component: AffectComponentProjection, **updates: object
) -> AffectComponentProjection:
    values = component.model_dump()
    values.update(updates)
    return AffectComponentProjection.model_validate(values)


def _validated_episode(
    episode: AffectEpisodeProjection, **updates: object
) -> AffectEpisodeProjection:
    values = episode.model_dump()
    values.update(updates)
    return AffectEpisodeProjection.model_validate(values)


def _replace(
    episodes: tuple[AffectEpisodeProjection, ...],
    index: int,
    episode: AffectEpisodeProjection,
) -> tuple[AffectEpisodeProjection, ...]:
    values = list(episodes)
    values[index] = episode
    return tuple(values)


def _aware_logical_time(logical_time: datetime) -> None:
    if logical_time.tzinfo is None or logical_time.utcoffset() is None:
        raise ValueError("authoritative logical time must be timezone-aware")


def _valid_merge_window(merge_window_seconds: int) -> None:
    if merge_window_seconds < 0:
        raise ValueError("affect merge window cannot be negative")


def _seconds(value: int) -> timedelta:
    return timedelta(seconds=value)


def _materialized_intensity(
    component: AffectComponentProjection,
    *,
    logical_time: datetime,
    baselines: tuple[AffectBaselineProjection, ...],
) -> int:
    profile = component.decay_profile
    if (
        profile.algorithm_version != ALGORITHM_VERSION
        or profile.table_digest != FACTOR_TABLE_DIGEST
    ):
        raise ValueError("affect decay profile uses an uninstalled algorithm")
    baseline = next(
        (item.baseline_bp for item in baselines if item.dimension == component.dimension),
        0,
    )
    return decay_intensity_bp(
        DecayAnchor(
            intensity_bp=component.decay_anchor_intensity_bp,
            anchored_at=component.decay_anchor_at,
            baseline_bp=baseline,
            residue_bp=component.residue_bp,
            decay_not_before=component.decay_not_before,
        ),
        DecayProfile(
            half_life_seconds=profile.half_life_seconds,
            floor_bp=profile.floor_bp,
            delay_seconds=profile.delay_seconds,
            config_version=profile.config_version,
            kind=profile.kind,
        ),
        logical_time,
    )

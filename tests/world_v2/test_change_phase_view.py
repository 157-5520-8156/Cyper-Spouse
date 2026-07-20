"""Change Phase: pure projection derivation over accepted Affect episodes."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from companion_daemon.world_v2.change_phase_view import (
    change_phase_advisories,
    change_phase_by_dimension,
    change_phase_readings,
    change_phase_summary_prose,
)
from companion_daemon.world_v2.life_author_runtime import LifeAuthorWeightPolicy
from companion_daemon.world_v2.schemas import (
    AffectComponentProjection,
    AffectDecayProfileProjection,
    AffectEpisodeProjection,
    AffectOrigin,
    AppraisalMeaningRef,
    EvidenceRef,
    affect_decay_config_digest,
)


NOW = datetime(2026, 7, 20, 3, 0, tzinfo=UTC)


def _meaning() -> AppraisalMeaningRef:
    return AppraisalMeaningRef(
        appraisal_id="appraisal:1",
        hypothesis_id="meaning:disappointment",
        source_cluster_ref="cluster:1",
        accepted_change_id="change:appraisal:1",
        accepted_transition_id="transition:appraisal:1",
    )


def _component(
    *,
    dimension: str = "sadness",
    intensity_bp: int,
    anchor_bp: int,
    last_stimulus_at: datetime,
    opened_at: datetime,
) -> AffectComponentProjection:
    return AffectComponentProjection(
        component_id=f"component:{dimension}:{opened_at.isoformat()}",
        dimension=dimension,
        source_cluster_ref="cluster:1",
        appraisal_refs=(_meaning(),),
        intensity_bp=intensity_bp,
        decay_anchor_intensity_bp=anchor_bp,
        opened_at=opened_at,
        decay_anchor_at=opened_at,
        decay_not_before=opened_at + timedelta(seconds=120),
        last_stimulus_at=last_stimulus_at,
        last_updated_at=max(opened_at, last_stimulus_at),
        decay_profile=AffectDecayProfileProjection(
            half_life_seconds=3_600,
            floor_bp=300,
            delay_seconds=120,
            config_version="affect-decay.1",
            config_digest=affect_decay_config_digest(
                kind="exponential_half_life",
                half_life_seconds=3_600,
                floor_bp=300,
                delay_seconds=120,
                config_version="affect-decay.1",
            ),
        ),
        residue_bp=300,
    )


def _episode(
    *,
    episode_id: str = "affect:1",
    components: tuple[AffectComponentProjection, ...],
    opened_at: datetime,
    status: str = "active",
    closed_at: datetime | None = None,
) -> AffectEpisodeProjection:
    return AffectEpisodeProjection(
        episode_id=episode_id,
        entity_revision=1 if status == "active" else 2,
        origin=AffectOrigin(
            change_id=f"change:{episode_id}",
            transition_id=f"transition:{episode_id}",
            policy_refs=("policy:affect.1",),
            matrix_catalog_version="affect-matrix.1",
            accepted_event_ref=f"event:{episode_id}",
        ),
        components=components,
        evidence_refs=(
            EvidenceRef(
                ref_id="observation:1",
                evidence_type="observed_message",
                claim_purpose="private_hypothesis",
            ),
        ),
        opened_at=opened_at,
        updated_at=closed_at or opened_at,
        status=status,
        closed_at=closed_at,
        resolution_refs=(
            (
                EvidenceRef(
                    ref_id="observation:resolution",
                    evidence_type="observed_message",
                    claim_purpose="private_hypothesis",
                ),
            )
            if status == "resolved"
            else ()
        ),
    )


def test_fresh_stimulus_reads_as_departing() -> None:
    episodes = (
        _episode(
            components=(
                _component(
                    intensity_bp=5_000,
                    anchor_bp=5_000,
                    last_stimulus_at=NOW - timedelta(minutes=20),
                    opened_at=NOW - timedelta(minutes=20),
                ),
            ),
            opened_at=NOW - timedelta(minutes=20),
        ),
    )
    readings = change_phase_readings(episodes, logical_time=NOW)
    assert [(item.dimension, item.phase) for item in readings] == [("sadness", "departing")]
    assert readings[0].source_event_refs == ("event:affect:1",)
    prose = change_phase_summary_prose(readings)
    assert "刚陷入" in prose and "低落" in prose


def test_decayed_intensity_reads_as_returning_then_recovering() -> None:
    stale = NOW - timedelta(hours=8)
    returning = (
        _episode(
            components=(
                _component(
                    intensity_bp=3_000,
                    anchor_bp=6_000,
                    last_stimulus_at=stale,
                    opened_at=stale,
                ),
            ),
            opened_at=stale,
        ),
    )
    readings = change_phase_readings(returning, logical_time=NOW)
    assert [(item.dimension, item.phase) for item in readings] == [("sadness", "returning")]
    assert "走出" in change_phase_summary_prose(readings)

    recovered = (
        _episode(
            components=(
                _component(
                    intensity_bp=900,
                    anchor_bp=6_000,
                    last_stimulus_at=stale,
                    opened_at=stale,
                ),
            ),
            opened_at=stale,
        ),
    )
    readings = change_phase_readings(recovered, logical_time=NOW)
    assert [(item.dimension, item.phase) for item in readings] == [("sadness", "recovering")]


def test_settled_intensity_reads_as_holding_and_baseline_is_silent() -> None:
    stale = NOW - timedelta(hours=6)
    holding = (
        _episode(
            components=(
                _component(
                    intensity_bp=5_600,
                    anchor_bp=6_000,
                    last_stimulus_at=stale,
                    opened_at=stale,
                ),
            ),
            opened_at=stale,
        ),
    )
    readings = change_phase_readings(holding, logical_time=NOW)
    assert [(item.dimension, item.phase) for item in readings] == [("sadness", "holding")]
    assert change_phase_readings((), logical_time=NOW) == ()
    assert change_phase_summary_prose(()) == ""


def test_recently_resolved_episode_reads_as_recovering() -> None:
    opened = NOW - timedelta(days=1)
    episodes = (
        _episode(
            components=(
                _component(
                    intensity_bp=4_000,
                    anchor_bp=4_000,
                    last_stimulus_at=opened,
                    opened_at=opened,
                ),
            ),
            opened_at=opened,
            status="resolved",
            closed_at=NOW - timedelta(hours=2),
        ),
    )
    readings = change_phase_readings(episodes, logical_time=NOW)
    assert [(item.dimension, item.phase) for item in readings] == [("sadness", "recovering")]


def test_naive_logical_time_is_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        change_phase_readings((), logical_time=datetime(2026, 7, 20, 3, 0))


def test_advisory_is_source_bound_and_bounded() -> None:
    class _Projection:
        logical_time = NOW
        affect_episodes = (
            _episode(
                components=(
                    _component(
                        intensity_bp=5_000,
                        anchor_bp=5_000,
                        last_stimulus_at=NOW - timedelta(minutes=10),
                        opened_at=NOW - timedelta(minutes=10),
                    ),
                ),
                opened_at=NOW - timedelta(minutes=10),
            ),
        )

    advisories = change_phase_advisories(_Projection())
    assert len(advisories) == 1
    advisory = advisories[0]
    assert advisory.kind == "change_phase"
    assert advisory.source_refs == ("event:affect:1",)
    assert advisory.producer_version == "change-phase-view.1"
    assert all(len(item.value) <= 256 for item in advisory.candidates)
    assert change_phase_advisories(
        type("_Empty", (), {"logical_time": NOW, "affect_episodes": ()})()
    ) == ()


def test_life_author_weight_policy_senses_change_phase() -> None:
    from companion_daemon.world_v2.life_author_seed import ReviewedLifeSeedCandidate, ReviewedLifeSeedOpening

    def _candidate(opening_id: str, domain: str) -> ReviewedLifeSeedCandidate:
        return ReviewedLifeSeedCandidate(
            token="0" * 64 if opening_id == "rest" else "1" * 64,
            opening=ReviewedLifeSeedOpening(
                id=opening_id,
                activity_kind=f"kind.{opening_id}",
                source="routine",
                domain=domain,
                local_windows=("08:00-23:00",),
                weekdays=(0, 1, 2, 3, 4, 5, 6),
                duration_minutes=60,
                importance_bp=5_000,
            ),
            availability_hash="2" * 64,
        )

    rest = _candidate("rest", "rest_recovery")
    study = _candidate("study", "study_class")
    policy = LifeAuthorWeightPolicy()
    assert policy.version == "life-author-weight.4"

    def _weights(last_stimulus: datetime, intensity: int, anchor: int) -> dict[str, int]:
        episodes = (
            _episode(
                components=(
                    _component(
                        intensity_bp=intensity,
                        anchor_bp=anchor,
                        last_stimulus_at=last_stimulus,
                        opened_at=last_stimulus,
                    ),
                ),
                opened_at=last_stimulus,
            ),
        )
        return policy.compile(
            candidates=(rest, study),
            plans=(),
            logical_time=NOW,
            affect_episodes=episodes,
        )

    departing = _weights(NOW - timedelta(minutes=15), 5_000, 5_000)
    returning = _weights(NOW - timedelta(hours=9), 3_000, 6_000)
    # Freshly departing heaviness leans further into rest than mid-recovery
    # does; the visible return restores some appetite for demanding work.
    assert departing[rest.token] / departing[study.token] > (
        returning[rest.token] / returning[study.token]
    )
    phases = change_phase_by_dimension(
        change_phase_readings(
            (
                _episode(
                    components=(
                        _component(
                            intensity_bp=3_000,
                            anchor_bp=6_000,
                            last_stimulus_at=NOW - timedelta(hours=9),
                            opened_at=NOW - timedelta(hours=9),
                        ),
                    ),
                    opened_at=NOW - timedelta(hours=9),
                ),
            ),
            logical_time=NOW,
        )
    )
    assert phases == {"sadness": "returning"}

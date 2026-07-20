from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from companion_daemon.world_v2.life_author_runtime import LifeAuthorWeightPolicy
from companion_daemon.world_v2.life_author_seed import (
    ReviewedLifeSeedCandidate,
    ReviewedLifeSeedOpening,
)


NOW = datetime(2026, 7, 17, 8, tzinfo=UTC)


def _candidate(
    token: str, *, activity_kind: str, importance_bp: int,
    daypart_fit_bp: int = 10_000, participant_ref: str | None = None,
    domain: str | None = None,
) -> ReviewedLifeSeedCandidate:
    return ReviewedLifeSeedCandidate(
        token=token * 64,
        opening=ReviewedLifeSeedOpening(
            id=f"opening-{token}", activity_kind=activity_kind,
            source="social" if participant_ref else "routine",
            domain=domain or (
                "family_roommate_friend" if participant_ref else "study_class"
            ),
            social_shape="npc" if participant_ref else "alone",
            npc_id="friend" if participant_ref else None,
            deviation="persist", visual_potential="social" if participant_ref else "object",
            privacy="personal", local_windows=("06:00-18:00",),
            weekdays=(0, 1, 2, 3, 4, 5, 6), duration_minutes=30,
            importance_bp=importance_bp,
        ),
        participant_ref=participant_ref,
        availability_hash="f" * 64,
        daypart_fit_bp=daypart_fit_bp,
    )


def test_policy_uses_only_generic_projection_signals_for_candidate_weights() -> None:
    repeated = _candidate(
        "a", activity_kind="study.reading", importance_bp=6_000
    )
    rare_social = _candidate(
        "b", activity_kind="social.walk", importance_bp=4_000,
        participant_ref="npc:friend",
    )
    daypart_edge = _candidate(
        "c", activity_kind="creative.sketch", importance_bp=6_000,
        daypart_fit_bp=6_000,
    )
    recent_plan = SimpleNamespace(
        activity_kind="study.reading", participant_refs=(),
        authority_origin=SimpleNamespace(accepted_at=NOW - timedelta(days=1)),
    )

    weights = LifeAuthorWeightPolicy().compile(
        candidates=(repeated, rare_social, daypart_edge),
        plans=(recent_plan,), logical_time=NOW,
    )

    assert weights == {
        "a" * 64: 3_000,  # one recent same-kind occurrence halves the mass
        "b" * 64: 6_000,  # first recent social opportunity receives a generic boost
        "c" * 64: 3_600,  # edge-of-window fit scales, but never forbids, an opening
    }


def test_policy_ignores_future_and_expired_history() -> None:
    candidate = _candidate(
        "a", activity_kind="study.reading", importance_bp=6_000
    )
    outside_history = (
        SimpleNamespace(
            activity_kind="study.reading", participant_refs=(),
            authority_origin=SimpleNamespace(accepted_at=NOW - timedelta(days=8)),
        ),
        SimpleNamespace(
            activity_kind="study.reading", participant_refs=(),
            authority_origin=SimpleNamespace(accepted_at=NOW + timedelta(minutes=1)),
        ),
    )

    assert LifeAuthorWeightPolicy().compile(
        candidates=(candidate,), plans=outside_history, logical_time=NOW,
    ) == {"a" * 64: 6_000}


def test_policy_uses_the_last_reviewed_domain_as_a_soft_life_rhythm_signal() -> None:
    repeated_focus = _candidate(
        "a", activity_kind="study.another_chapter", importance_bp=6_000,
        domain="study_class",
    )
    restorative_walk = _candidate(
        "b", activity_kind="commute.short_walk", importance_bp=4_000,
        domain="commute_walk",
    )
    recent_plan = SimpleNamespace(
        activity_kind="study.focused_reading", participant_refs=(),
        authority_origin=SimpleNamespace(accepted_at=NOW - timedelta(hours=1)),
    )
    opening_domains = {"study.focused_reading": "study_class"}

    weights = LifeAuthorWeightPolicy().compile(
        candidates=(repeated_focus, restorative_walk),
        plans=(recent_plan,), logical_time=NOW,
        recent_domain_by_activity=opening_domains,
    )

    # The matrix is an advisory bias, not a forced transition: another focus
    # block keeps non-zero mass, while movement after focus gets a soft lift.
    assert weights == {
        "a" * 64: 5_100,
        "b" * 64: 5_000,
    }


def _episode(*components: tuple[str, int]) -> SimpleNamespace:
    return SimpleNamespace(
        status="active",
        components=tuple(
            SimpleNamespace(dimension=dimension, intensity_bp=intensity)
            for dimension, intensity in components
        ),
    )


def test_policy_lets_heavy_affect_lean_toward_rest_without_forbidding_focus() -> None:
    focused_reading = _candidate(
        "a", activity_kind="study.reading", importance_bp=6_000, domain="study_class"
    )
    quiet_rest = _candidate(
        "b", activity_kind="recovery.quiet_rest", importance_bp=6_000,
        domain="rest_recovery",
    )

    neutral = LifeAuthorWeightPolicy().compile(
        candidates=(focused_reading, quiet_rest), plans=(), logical_time=NOW,
    )
    weighed_down = LifeAuthorWeightPolicy().compile(
        candidates=(focused_reading, quiet_rest), plans=(), logical_time=NOW,
        affect_episodes=(_episode(("sadness", 8_000)),),
    )

    assert neutral["a" * 64] == neutral["b" * 64] == 6_000
    # A heavy mood makes rest more likely and focus less likely, but the
    # focus opening keeps meaningful mass: this is a tendency, not a rule.
    assert weighed_down["b" * 64] > weighed_down["a" * 64]
    assert weighed_down["a" * 64] >= neutral["a" * 64] * 6 // 10
    assert weighed_down["b" * 64] <= neutral["b" * 64] * 14 // 10


def test_policy_loneliness_reaches_toward_company_while_hurt_pulls_away() -> None:
    social = _candidate(
        "a", activity_kind="social.reading_list", importance_bp=6_000,
        participant_ref="npc:friend", domain="family_roommate_friend",
    )

    lonely = LifeAuthorWeightPolicy().compile(
        candidates=(social,), plans=(), logical_time=NOW,
        affect_episodes=(_episode(("loneliness", 8_000)),),
    )
    hurt = LifeAuthorWeightPolicy().compile(
        candidates=(social,), plans=(), logical_time=NOW,
        affect_episodes=(_episode(("hurt", 8_000)),),
    )
    neutral = LifeAuthorWeightPolicy().compile(
        candidates=(social,), plans=(), logical_time=NOW,
    )

    assert lonely["a" * 64] > neutral["a" * 64] > hurt["a" * 64]


def test_policy_ignores_resolved_episodes_and_stays_replay_deterministic() -> None:
    candidate = _candidate(
        "a", activity_kind="recovery.quiet_rest", importance_bp=6_000,
        domain="rest_recovery",
    )
    resolved = SimpleNamespace(
        status="resolved",
        components=(SimpleNamespace(dimension="sadness", intensity_bp=9_000),),
    )

    first = LifeAuthorWeightPolicy().compile(
        candidates=(candidate,), plans=(), logical_time=NOW,
        affect_episodes=(resolved,),
    )
    second = LifeAuthorWeightPolicy().compile(
        candidates=(candidate,), plans=(), logical_time=NOW,
        affect_episodes=(resolved,),
    )

    assert first == second == {"a" * 64: 6_000}

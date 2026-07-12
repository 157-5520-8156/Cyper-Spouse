from datetime import datetime, timedelta
from pathlib import Path

from companion_daemon.db import CompanionStore
from companion_daemon.world import WorldKernel
from companion_daemon.world_behavior import WorldBehaviorPolicy


def _world(tmp_path: Path) -> tuple[WorldKernel, str]:
    kernel = WorldKernel(CompanionStore(tmp_path / "affect-episodes.sqlite"))
    started = kernel.start_from_seed_file(Path("configs/world_seed.yaml"))
    registered = kernel.submit(
        {
            "type": "register_user",
            "world_id": started.world_id,
            "user_id": "user:geoff",
            "name": "geoff",
            "idempotency_key": "episode-user",
        },
        expected_revision=started.revision,
    )
    assert registered.revision > started.revision
    return kernel, started.world_id


def _appraise(
    kernel: WorldKernel,
    world_id: str,
    *,
    index: int,
    appraisal: str,
    target: str,
    severity: int,
) -> None:
    kernel.submit(
        {
            "type": "appraise_turn",
            "world_id": world_id,
            "appraisal": appraisal,
            "interaction": {
                "target": target,
                "severity": severity,
                "acts": [appraisal],
                "evidence_spans": [f"evidence-{index}"],
                "certainty": 90,
                "goal_congruence": -60 if appraisal == "boundary_violation" else 55,
                "controllability": 45,
                "norm_compatibility": -80 if appraisal == "boundary_violation" else 70,
                "power_delta": -40 if appraisal == "boundary_violation" else 0,
                "confidence": 0.95,
            },
            "intent_id": f"episode-intent:{index}",
            "message_id": f"episode-message:{index}",
            "user_id": "user:geoff",
            "idempotency_key": f"episode-appraise:{index}",
        },
        expected_revision=kernel.revision(world_id),
    )


def test_positive_and_negative_episodes_keep_separate_sources_and_targets(
    tmp_path: Path,
) -> None:
    kernel, world_id = _world(tmp_path)
    _appraise(
        kernel,
        world_id,
        index=1,
        appraisal="boundary_violation",
        target="companion",
        severity=3,
    )
    _appraise(
        kernel,
        world_id,
        index=2,
        appraisal="warmth_received",
        target="companion",
        severity=2,
    )

    affect = kernel.snapshot(world_id)["emotion_modulation"]
    episodes = affect["active_episodes"]

    assert len(episodes) == 2
    assert {episode["source_reference"] for episode in episodes} == {
        "message:episode-message:1",
        "message:episode-message:2",
    }
    assert {episode["appraisal"] for episode in episodes} == {
        "boundary_violation",
        "warmth_received",
    }
    assert affect["core_affect"]["mixed"] is True
    assert affect["profile"]["version"] == "zhizhi-affect-v1"
    assert all(
        episode["profile_version"] == "zhizhi-affect-v1"
        for episode in episodes
    )
    assert affect["vector"]["hurt"] > 0
    assert affect["vector"]["warmth"] > affect["personality_baseline"]["warmth"]
    display = kernel.snapshot(world_id)["last_affect_display"]
    assert display["primary_appraisal"] == "boundary_violation"
    assert display["secondary_appraisal"] == "warmth_received"
    assert display["mixed"] is True
    assert display["approach_avoidance"] == "approach_avoidance"
    guidance = WorldBehaviorPolicy().expression_guidance(
        kernel.snapshot(world_id), user_id="user:geoff"
    )
    assert "并存" in guidance.prompt_line
    assert "假装已经没事" in guidance.prompt_line


def test_time_decay_regulates_episode_arousal_without_losing_causal_history(
    tmp_path: Path,
) -> None:
    kernel, world_id = _world(tmp_path)
    _appraise(
        kernel,
        world_id,
        index=1,
        appraisal="boundary_violation",
        target="companion",
        severity=4,
    )
    before = kernel.snapshot(world_id)["emotion_modulation"]["active_episodes"][0]
    now = datetime.fromisoformat(
        str(kernel.snapshot(world_id)["clock"]["logical_at"])
    )

    kernel.advance(
        world_id,
        now + timedelta(hours=6),
        expected_revision=kernel.revision(world_id),
    )

    affect = kernel.snapshot(world_id)["emotion_modulation"]
    matching = [
        episode
        for episode in affect["active_episodes"]
        if episode["source_reference"] == before["source_reference"]
    ]
    assert matching
    assert matching[0]["intensity"] < before["intensity"]
    assert matching[0]["target"] == "companion"
    assert matching[0]["status"] in {"active", "regulated"}


def test_settled_long_term_resentment_slowly_increases_harm_persistence(
    tmp_path: Path,
) -> None:
    kernel, world_id = _world(tmp_path)
    for index in range(1, 4):
        _appraise(
            kernel,
            world_id,
            index=index,
            appraisal="boundary_violation",
            target="companion",
            severity=3,
        )

    snapshot = kernel.snapshot(world_id)
    episodes = snapshot["emotion_modulation"]["active_episodes"]
    boundary_episodes = [
        episode
        for episode in episodes
        if episode["appraisal"] == "boundary_violation"
    ]

    assert len(boundary_episodes) == 3
    assert boundary_episodes[2]["half_life_hours"] > boundary_episodes[0][
        "half_life_hours"
    ]
    assert snapshot["long_term_affinity"]["user:geoff"]["vector"][
        "resentment"
    ] == 1

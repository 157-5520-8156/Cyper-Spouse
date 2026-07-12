from dataclasses import replace
from datetime import UTC, datetime, timedelta

from companion_daemon.world_affect import (
    MAX_PROJECTED_ARCHIVED_EPISODES,
    apply_appraisal,
    decay_affect,
    initial_affect,
    outcome_payload,
)
from companion_daemon.world_affinity import initial_affinity, settle_affinity_interaction


NOW = datetime(2026, 7, 12, 9, 0, tzinfo=UTC)


def test_projection_bounds_archived_episodes_while_ledger_keeps_prior_events() -> None:
    outcome = apply_appraisal(
        initial_affect(NOW.isoformat()),
        "warmth_received",
        NOW.isoformat(),
        source_reference="message:current",
        target="companion",
    )
    archived = tuple(
        {
            "episode_id": f"old:{index:04d}",
            "started_at": (NOW + timedelta(minutes=index)).isoformat(),
            "resolved_at": (NOW + timedelta(minutes=index + 1)).isoformat(),
            "status": "resolved",
        }
        for index in range(MAX_PROJECTED_ARCHIVED_EPISODES + 40)
    )

    payload = outcome_payload(
        replace(outcome, archived_episodes=archived),
        logical_at=NOW.isoformat(),
        event_type="AffectDecayed",
    )

    assert len(payload["archived_episodes"]) == MAX_PROJECTED_ARCHIVED_EPISODES
    assert payload["archived_episodes"][0]["episode_id"] == "old:0040"


def test_active_episodes_are_authoritative_over_a_stale_vector_projection() -> None:
    initial = initial_affect(NOW.isoformat())
    harmed = apply_appraisal(
        initial,
        "boundary_violation",
        NOW.isoformat(),
        source_reference="message:harm-1",
        target="companion",
    )
    stale_projection = {
        **initial,
        **harmed.__dict__,
        "vector": {key: 0 for key in harmed.vector},
    }

    rebuilt = decay_affect(stale_projection, 0, NOW.isoformat())

    assert rebuilt.vector == harmed.vector
    assert rebuilt.unresolved is True
    assert rebuilt.core_affect == harmed.core_affect


def test_new_appraisal_is_aggregated_with_existing_episode_not_stale_vector() -> None:
    initial = initial_affect(NOW.isoformat())
    harmed = apply_appraisal(
        initial,
        "boundary_violation",
        NOW.isoformat(),
        source_reference="message:harm-1",
        target="companion",
    )
    stale_projection = {
        **initial,
        **harmed.__dict__,
        "vector": {key: 0 for key in harmed.vector},
    }

    mixed = apply_appraisal(
        stale_projection,
        "warmth_received",
        (NOW + timedelta(minutes=1)).isoformat(),
        source_reference="message:warmth-1",
        target="companion",
    )

    assert mixed.vector["hurt"] == 17
    assert mixed.vector["anger"] == 12
    assert mixed.vector["warmth"] == 5
    assert mixed.core_affect["mixed"] is True


def test_negative_world_event_uses_negative_half_life() -> None:
    state = initial_affect(
        NOW.isoformat(),
        profile={
            "negative_half_life_hours": 30,
            "positive_half_life_hours": 4,
        },
    )

    conflicted = apply_appraisal(
        state,
        "npc_conflict",
        NOW.isoformat(),
        source_reference="life:npc-conflict-1",
        target="npc:roommate",
    )

    assert conflicted.active_episodes[0]["valence"] == -1
    assert conflicted.active_episodes[0]["half_life_hours"] == 30


def test_negative_components_use_versioned_emotion_specific_half_lives() -> None:
    state = initial_affect(
        NOW.isoformat(),
        profile={
            "version": "affect-profile-v2-test",
            "emotion_half_life_hours": {
                "anger": 6,
                "resentment": 48,
                "anxiety": 12,
                "sadness": 24,
                "hurt": 30,
            },
        },
    )
    harmed = apply_appraisal(
        state,
        "boundary_violation",
        NOW.isoformat(),
        source_reference="message:kind-decay",
        target="companion",
    )

    episode = harmed.active_episodes[0]
    assert episode["profile_version"] == "affect-profile-v2-test"
    assert episode["component_half_life_hours"] == {
        "anger": 6,
        "anxiety": 12,
        "hurt": 30,
        "resentment": 48,
        "sadness": 24,
    }

    later = decay_affect(
        harmed.__dict__, 24 * 3600, (NOW + timedelta(hours=24)).isoformat()
    )
    assert later.vector["anger"] < later.vector["hurt"]
    assert later.vector["resentment"] > later.vector["anger"]


def test_resolved_episode_is_archived_with_its_causal_source() -> None:
    state = initial_affect(
        NOW.isoformat(),
        profile={"positive_half_life_hours": 1, "warmth_half_life_hours": 1},
    )
    warmed = apply_appraisal(
        state,
        "warmth_received",
        NOW.isoformat(),
        source_reference="message:warmth-to-archive",
        target="companion",
    )

    later = decay_affect(
        warmed.__dict__, 12 * 3600, (NOW + timedelta(hours=12)).isoformat()
    )

    assert later.active_episodes == ()
    assert len(later.archived_episodes) == 1
    archived = later.archived_episodes[0]
    assert archived["status"] == "resolved"
    assert archived["source_reference"] == "message:warmth-to-archive"
    resolved_at = datetime.fromisoformat(str(archived["resolved_at"]))
    assert NOW < resolved_at <= NOW + timedelta(hours=12)


def test_unresolved_high_salience_episode_is_not_lost_after_sixteen_minor_events() -> None:
    state: dict[str, object] = initial_affect(NOW.isoformat())
    harmed = apply_appraisal(
        state,
        "sexual_boundary_violation",
        NOW.isoformat(),
        source_reference="message:major-harm",
        target="companion",
        intensity=4,
    )
    state = {**state, **harmed.__dict__}
    for index in range(20):
        warmed = apply_appraisal(
            state,
            "warmth_received",
            (NOW + timedelta(minutes=index + 1)).isoformat(),
            source_reference=f"message:minor-{index}",
            target="companion",
            intensity=1,
        )
        state = {**state, **warmed.__dict__}

    assert any(
        episode["source_reference"] == "message:major-harm" for episode in state["active_episodes"]
    )
    assert len(state["active_episodes"]) > 16

    decayed = decay_affect(
        state,
        60,
        (NOW + timedelta(minutes=21)).isoformat(),
    )

    assert any(
        episode["source_reference"] == "message:major-harm" for episode in decayed.active_episodes
    )
    assert len(decayed.active_episodes) > 16


def test_duplicate_repair_evidence_for_the_same_boundary_is_counted_once() -> None:
    initial = initial_affect(NOW.isoformat())
    harmed = apply_appraisal(
        initial,
        "boundary_violation",
        NOW.isoformat(),
        source_reference="message:harm-1",
        target="companion",
    )
    apology = apply_appraisal(
        harmed.__dict__,
        "repair_specific",
        (NOW + timedelta(minutes=1)).isoformat(),
        source_reference="message:apology-1",
        target="companion",
    )
    first = apply_appraisal(
        apology.__dict__,
        "boundary_respected",
        (NOW + timedelta(hours=1)).isoformat(),
        source_reference="message:followthrough-1",
        target="companion",
    )
    duplicate = apply_appraisal(
        first.__dict__,
        "boundary_respected",
        (NOW + timedelta(hours=2)).isoformat(),
        source_reference="message:followthrough-1",
        target="companion",
    )

    assert first.repair_evidence_count == 1
    assert duplicate.repair_evidence_count == 1


def test_repair_regulation_does_not_expire_before_the_harm_and_make_hurt_rebound() -> None:
    initial = initial_affect(NOW.isoformat())
    harmed = apply_appraisal(
        initial,
        "boundary_violation",
        NOW.isoformat(),
        source_reference="message:harm-1",
        target="companion",
    )
    apology = apply_appraisal(
        harmed.__dict__,
        "repair_specific",
        (NOW + timedelta(minutes=1)).isoformat(),
        source_reference="message:apology-1",
        target="companion",
    )
    after_twenty_four_hours = decay_affect(
        apology.__dict__,
        24 * 3600,
        (NOW + timedelta(hours=24)).isoformat(),
    )
    after_thirty_hours = decay_affect(
        after_twenty_four_hours.__dict__,
        6 * 3600,
        (NOW + timedelta(hours=30)).isoformat(),
    )

    assert after_thirty_hours.vector["hurt"] <= after_twenty_four_hours.vector["hurt"]
    assert after_thirty_hours.vector["resentment"] <= after_twenty_four_hours.vector["resentment"]


def test_old_affinity_evidence_expires_before_a_new_pattern_is_learned() -> None:
    state = initial_affinity()
    first = settle_affinity_interaction(
        state,
        user_id="user:geoff",
        appraisal="warmth_received",
        settlement_id="turn:1",
        logical_at=NOW.isoformat(),
    )
    second = settle_affinity_interaction(
        first.state,
        user_id="user:geoff",
        appraisal="warmth_received",
        settlement_id="turn:2",
        logical_at=(NOW + timedelta(hours=1)).isoformat(),
    )
    much_later = settle_affinity_interaction(
        second.state,
        user_id="user:geoff",
        appraisal="warmth_received",
        settlement_id="turn:3",
        logical_at=(NOW + timedelta(days=120)).isoformat(),
    )

    assert much_later.delta == {}
    assert much_later.state["evidence_counts"]["warmth"] == 1


def test_affinity_residue_loses_weight_when_the_pattern_is_not_repeated_for_a_year() -> None:
    state = initial_affinity()
    for index in range(3):
        outcome = settle_affinity_interaction(
            state,
            user_id="user:geoff",
            appraisal="boundary_violation",
            settlement_id=f"harm:{index}",
            logical_at=(NOW + timedelta(days=index)).isoformat(),
        )
        state = outcome.state
    assert state["vector"] == {"warmth": -1, "resentment": 1}

    aged = settle_affinity_interaction(
        state,
        user_id="user:geoff",
        appraisal="ordinary_message",
        settlement_id="ordinary:one-year-later",
        logical_at=(NOW + timedelta(days=365)).isoformat(),
    )

    assert aged.state["vector"] == {}
    assert aged.delta == {"warmth": 1, "resentment": -1}

from types import SimpleNamespace
from datetime import UTC, datetime, timedelta

from companion_daemon.world_v2.life_ecology_contract import (
    LIFE_ECOLOGY_WAKE_EVENT_TYPES,
)
from companion_daemon.world_v2.production_turn_application import (
    external_perception_downstream_health,
)
from companion_daemon.world_v2.social_initiative import (
    SITUATION_STIMULUS_EVENT_TYPES,
)


def test_external_perception_is_an_opportunity_for_life_and_social_consideration() -> None:
    assert "ExternalPerceptionRecorded" in LIFE_ECOLOGY_WAKE_EVENT_TYPES
    assert "ExternalPerceptionRecorded" in SITUATION_STIMULUS_EVENT_TYPES


def test_health_correlates_model_silence_to_the_exact_external_perception() -> None:
    now = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
    first_perception = SimpleNamespace(
        accepted_event_ref="event:external-perception:first",
        attention_attempt_id="attempt:external-perception:latest",
    )
    last_perception = SimpleNamespace(
        accepted_event_ref="event:external-perception:last",
        attention_attempt_id=first_perception.attention_attempt_id,
    )
    anchor = SimpleNamespace(
        event_id="event:situation-anchor",
        event_type="AffectEpisodeOpened",
        world_revision=10,
        logical_time=now,
    )
    first_ref = SimpleNamespace(
        event_id=first_perception.accepted_event_ref,
        event_type="ExternalPerceptionRecorded",
        world_revision=11,
        logical_time=now + timedelta(minutes=1),
    )
    last_ref = SimpleNamespace(
        event_id=last_perception.accepted_event_ref,
        event_type="ExternalPerceptionRecorded",
        world_revision=12,
        logical_time=now + timedelta(minutes=1),
    )
    silent = SimpleNamespace(
        process_kind="proactive_action_deliberation",
        source_evidence_ref=anchor.event_id,
        state="terminal",
        runtime_outcome_ref="proactive:silent",
    )
    life = SimpleNamespace(
        process_kind="life_ecology",
        source_evidence_ref=first_perception.accepted_event_ref,
        state="terminal",
        runtime_outcome_ref="life:idle",
    )
    projection = SimpleNamespace(
        external_perceptions=(first_perception, last_perception),
        trigger_processes=(silent, life),
        message_observations=(),
        committed_world_event_refs=(anchor, first_ref, last_ref),
        actions=(),
    )

    assert external_perception_downstream_health(projection) == {
        "perception_event_ref": first_perception.accepted_event_ref,
        "perception_event_refs": [
            first_perception.accepted_event_ref,
            last_perception.accepted_event_ref,
        ],
        "attention_attempt_id": first_perception.attention_attempt_id,
        "life_state": "considered",
        "social_state": "model_silent_no_action",
    }

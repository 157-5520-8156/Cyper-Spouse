from datetime import datetime, timedelta
from pathlib import Path

from companion_daemon.db import CompanionStore
from companion_daemon.emotion_state import interpret_interaction
from companion_daemon.models import IncomingMessage, MoodState
from companion_daemon.world_interaction_rules import classify_repair_appraisal
from companion_daemon.world import WorldKernel
from companion_daemon.world_interaction_rules import HARMFUL_INTERACTION_APPRAISALS


def test_28_day_replay_repairs_without_false_harm_or_infinite_escalation(
    tmp_path: Path,
) -> None:
    store = CompanionStore(tmp_path / "emotion-28-day.sqlite")
    world = WorldKernel(store)
    started = world.submit(
        {
            "type": "start_world",
            "seed": {
                "world_id": "emotion-28-day",
                "logical_at": "2026-06-01T09:00:00+08:00",
                "protagonist": {
                    "id": "zhizhi",
                    "name": "沈知栀",
                    "kind": "companion",
                    "stable_traits": ["温和、敏感、观察力强"],
                    "templates": [],
                },
                "affect_profile": {
                    "version": "longitudinal-eval-v1",
                    "negative_half_life_hours": 18,
                    "positive_half_life_hours": 10,
                    "warmth_half_life_hours": 6,
                    "repair_evidence_required": 2,
                    "spillover_leakage_cap": 25,
                    "resentment_half_life_gain_hours": 2,
                    "resentment_intensity_gain": 3,
                },
                "life_outcome_templates": {},
                "daily_schedule": [],
                "long_term_goals": [],
                "npcs": [],
            },
        },
        expected_revision=0,
    )
    world.submit(
        {
            "type": "register_user",
            "world_id": started.world_id,
            "user_id": "user:geoff",
            "name": "geoff",
            "idempotency_key": "longitudinal-user",
        },
        expected_revision=started.revision,
    )
    weekly_script = (
        ("我真蠢，今天又忘记保存了。", False),
        ("你真丑。", True),
        ("对不起，刚才那样说是我不对。", False),
        ("你不想说就不说，我尊重你的边界。", False),
        ("你说停我就停，不继续问了。", False),
        ("他说我蠢，我听完挺难受。", False),
        ("请把页面滚动到底。", False),
    )
    harmful_days = 0
    benign_false_positives = 0
    peak_hurt = 0

    for day in range(28):
        text, expected_harm = weekly_script[day % len(weekly_script)]
        event = interpret_interaction(
            IncomingMessage(
                platform="simulator",
                platform_user_id="geoff",
                message_id=f"longitudinal-{day + 1}",
                text=text,
            ),
            MoodState(),
        )
        appraisal = event.kind
        if appraisal == "repair_attempt":
            appraisal = classify_repair_appraisal(text) or appraisal
        repair_evidence = {}
        if appraisal == "boundary_respected":
            violation_id = str(
                world.snapshot(started.world_id)["emotion_modulation"].get(
                    "repair_target_reference"
                )
                or ""
            )
            repair_evidence = {
                "repair_evidence": {
                    "violation_id": violation_id,
                    "commitment_id": f"commitment:{violation_id}",
                    "opportunity_id": f"longitudinal-opportunity:{day + 1}",
                    "behavior_key": "honor_boundary",
                }
            }
        world.submit(
            {
                "type": "appraise_turn",
                "world_id": started.world_id,
                "appraisal": appraisal,
                "interaction": {
                    "target": event.target,
                    "severity": event.intensity,
                    "acts": list(event.acts),
                    "evidence_spans": list(event.evidence_spans),
                    **repair_evidence,
                },
                "intent_id": f"longitudinal-intent:{day + 1}",
                "message_id": f"longitudinal-{day + 1}",
                "user_id": "user:geoff",
                "idempotency_key": f"longitudinal-appraise:{day + 1}",
            },
            expected_revision=world.revision(started.world_id),
        )
        snapshot = world.snapshot(started.world_id)
        observed_appraisal = str(snapshot["last_appraisal"]["appraisal"])
        observed_harm = observed_appraisal in HARMFUL_INTERACTION_APPRAISALS
        harmful_days += int(observed_harm)
        benign_false_positives += int(observed_harm and not expected_harm)
        peak_hurt = max(
            peak_hurt,
            int(snapshot["emotion_modulation"]["vector"]["hurt"]),
        )
        now = datetime.fromisoformat(str(snapshot["clock"]["logical_at"]))
        world.advance(
            started.world_id,
            now + timedelta(days=1),
            expected_revision=world.revision(started.world_id),
        )

    final = world.snapshot(started.world_id)
    assert harmful_days == 4
    assert benign_false_positives == 0
    assert peak_hurt < 50
    assert final["emotion_modulation"]["unresolved"] is False
    assert final["emotion_modulation"]["repair_evidence_count"] >= 2
    assert world.rebuild_projection(
        started.world_id, "world_current_state"
    ).matches_live is True

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from companion_daemon.db import CompanionStore
from companion_daemon.engine import CompanionEngine, seed_user
from companion_daemon.models import IncomingMessage
from companion_daemon.world import WorldKernel


def test_default_seed_contains_a_scheduled_negative_npc_experience() -> None:
    seed = yaml.safe_load(Path("configs/world_seed.yaml").read_text())
    templates = seed["life_outcome_templates"]
    scheduled_templates = {
        item["template_id"] for item in seed["weekly_themes"]
    }

    negative_npc_templates = {
        template_id
        for template_id, template in templates.items()
        if template.get("npc_id")
        and template.get("affect_appraisal") == "npc_conflict"
    }

    assert negative_npc_templates
    assert negative_npc_templates & scheduled_templates


NOW = datetime(2026, 7, 11, 9, 0, tzinfo=UTC)


class RecordingReplyModel:
    def __init__(self) -> None:
        self.calls: list[list[dict[str, str]]] = []

    async def complete(self, messages, *, temperature: float) -> str:
        self.calls.append(messages)
        return (
            '{"reply_text":"我听见了。你想聊哪一部分？",'
            '"mentioned_event_ids":[],"proposed_action_ids":[],"claims":[]}'
        )


def _seed(
    *,
    template_id: str,
    template: dict[str, object],
    goals: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "world_id": "life-affect-v1",
        "logical_at": NOW.isoformat(),
        "protagonist": {
            "id": "zhizhi",
            "name": "沈知栀",
            "kind": "companion",
            "stable_traits": ["温和、敏感、观察力强"],
            "templates": [template_id],
        },
        "life_outcome_templates": {template_id: template},
        "daily_schedule": [
            {
                "slot": "affective_event",
                "title": "会产生情绪后果的生活事件",
                "template_id": template_id,
                "location": template["location"],
                "starts_hour": 9,
                "ends_hour": 10,
            }
        ],
        "long_term_goals": goals or [],
        "npcs": [
            {
                "id": "roommate-lin",
                "name": "林晚",
                "kind": "roommate",
                "location": "宿舍",
                "availability": ["00:00-23:00"],
                "templates": [template_id],
            }
        ],
    }


@pytest.mark.asyncio
async def test_npc_conflict_changes_affect_then_next_turn_deliberation_and_prompt(
    tmp_path: Path,
) -> None:
    store = CompanionStore(tmp_path / "npc-conflict.sqlite")
    seed_user(store)
    world = WorldKernel(store)
    started = world.submit(
        {
            "type": "start_world",
            "seed": _seed(
                template_id="roommate_conflict",
                template={
                    "location": "宿舍",
                    "npc_id": "roommate-lin",
                    "energy_cost": 3,
                    "content": "和林晚因为公共区域的杂物起了争执。",
                    "affect_appraisal": "npc_conflict",
                    "affect_intensity": 70,
                },
            ),
        },
        expected_revision=0,
    )
    baseline = world.snapshot(started.world_id)["emotion_modulation"]["vector"]

    world.advance(
        started.world_id,
        NOW + timedelta(hours=2),
        expected_revision=world.revision(started.world_id),
    )
    after_conflict = world.snapshot(started.world_id)["emotion_modulation"]

    model = RecordingReplyModel()
    engine = CompanionEngine(
        store,
        model,  # type: ignore[arg-type]
        "你是沈知栀。",
        world_kernel=world,
        world_id=started.world_id,
    )
    reply = await engine.handle_message(
        IncomingMessage(
            platform="simulator",
            platform_user_id="geoff",
            message_id="after-npc-conflict",
            text="你现在想聊聊吗？",
        )
    )

    affect_events = [
        event for event in world.events(started.world_id)
        if event.event_type == "AffectChanged"
        and event.payload.get("source_appraisal") == "npc_conflict"
    ]
    assert affect_events
    assert affect_events[-1].payload["source_reference"].startswith("outcome:")
    assert after_conflict["vector"]["anger"] > baseline["anger"]
    assert after_conflict["vector"]["hurt"] > baseline["hurt"]
    assert after_conflict["unresolved"] is True

    snapshot = world.snapshot(started.world_id)
    assert snapshot["last_deliberation"]["drives"]["irritation"] > baseline["anger"]
    display = snapshot["last_affect_display"]
    assert display["attribution_target"] == "npc:roommate-lin"
    assert display["regulation_strategy"] == "contain_spillover"
    assert display["leakage"] <= 25
    prompt = "\n".join(
        str(message["content"])
        for call in model.calls
        for message in call
    )
    assert reply is not None
    assert '"anger":' in prompt
    assert "npc_conflict" in prompt
    assert "不要把它算到用户头上" in prompt


def test_goal_completion_creates_traceable_positive_affect(tmp_path: Path) -> None:
    world = WorldKernel(CompanionStore(tmp_path / "goal-completed.sqlite"))
    started = world.submit(
        {
            "type": "start_world",
            "seed": _seed(
                template_id="finish_portfolio",
                template={
                    "location": "宿舍",
                    "goal_id": "photo-portfolio",
                    "energy_cost": 4,
                    "content": "完成了摄影作品集的最后一轮整理。",
                    "affect_appraisal": "goal_completed",
                    "affect_intensity": 65,
                },
                goals=[
                    {
                        "id": "photo-portfolio",
                        "title": "完成摄影作品集",
                        "target": 1,
                    }
                ],
            ),
        },
        expected_revision=0,
    )
    baseline = world.snapshot(started.world_id)["emotion_modulation"]["vector"]

    advanced = world.advance(
        started.world_id,
        NOW + timedelta(hours=2),
        expected_revision=world.revision(started.world_id),
    )

    snapshot = world.snapshot(started.world_id)
    assert snapshot["goals"]["photo-portfolio"]["status"] == "completed"
    positive_affect = [
        event for event in advanced.events
        if event.event_type == "AffectChanged"
        and event.payload.get("source_appraisal") == "goal_completed"
    ]
    assert positive_affect
    assert positive_affect[-1].payload["source_reference"].startswith("outcome:")
    assert snapshot["emotion_modulation"]["vector"]["joy"] > baseline["joy"]
    assert snapshot["emotion_modulation"]["vector"]["warmth"] > baseline["warmth"]

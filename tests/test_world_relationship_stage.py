from pathlib import Path

import pytest

from companion_daemon.db import CompanionStore
from companion_daemon.emotion_state import interpret_interaction
from companion_daemon.engine import CompanionEngine
from companion_daemon.models import IncomingMessage, MoodState
from companion_daemon.world import WorldKernel
from companion_daemon.world_relationship import evaluate_relationship_stage
from companion_daemon.world_conversation import human_reply_contract_violation


def _world(tmp_path: Path) -> tuple[WorldKernel, str]:
    kernel = WorldKernel(CompanionStore(tmp_path / "relationship-stage.sqlite"))
    started = kernel.start_from_seed_file(Path("configs/world_seed.yaml"))
    user = kernel.submit(
        {
            "type": "register_user",
            "world_id": started.world_id,
            "user_id": "user:geoff",
            "name": "geoff",
            "idempotency_key": "register:user:geoff",
        },
        expected_revision=started.revision,
    )
    assert user.revision > started.revision
    return kernel, started.world_id


def _appraise(kernel: WorldKernel, world_id: str, index: int, appraisal: str) -> None:
    kernel.submit(
        {
            "type": "appraise_turn",
            "world_id": world_id,
            "appraisal": appraisal,
            "intent_id": f"turn:relationship:{index}",
            "message_id": f"relationship-message:{index}",
            "user_id": "user:geoff",
            "idempotency_key": f"appraise:relationship:{index}",
        },
        expected_revision=kernel.revision(world_id),
    )


def test_world_relationship_starts_as_stranger_and_promotes_from_ledger_interactions(
    tmp_path: Path,
) -> None:
    kernel, world_id = _world(tmp_path)

    initial = kernel.snapshot(world_id)
    assert initial["relationships"]["user:geoff"]["stage"] == "stranger"
    assert initial["relationships"]["user:geoff"]["interaction_count"] == 0
    dashboard = kernel.daemon_dashboard_projection(world_id, past_days=0, future_days=0)
    assert dashboard["dashboard"]["relationship_stage"] == "stranger"
    assert dashboard["state"]["relationship_stage"] == "stranger"
    assert any(
        event.event_type == "RelationshipStageEvaluated"
        and event.payload["stage"] == "stranger"
        for event in kernel.events(world_id)
    )

    for index in range(1, 5):
        _appraise(kernel, world_id, index, "warmth_received")

    relation = kernel.snapshot(world_id)["relationships"]["user:geoff"]
    assert relation["stage"] == "acquaintance"
    assert relation["interaction_count"] == 4
    assert relation["trust"] >= 18
    assert relation["closeness"] >= 0


def test_world_relationship_can_only_drop_one_stage_for_a_boundary_breach(
    tmp_path: Path,
) -> None:
    kernel, world_id = _world(tmp_path)
    for index in range(1, 5):
        _appraise(kernel, world_id, index, "warmth_received")
    assert kernel.snapshot(world_id)["relationships"]["user:geoff"]["stage"] == "acquaintance"

    _appraise(kernel, world_id, 5, "boundary_violation")
    assert kernel.snapshot(world_id)["relationships"]["user:geoff"]["stage"] == "acquaintance"
    _appraise(kernel, world_id, 6, "boundary_violation")

    relation = kernel.snapshot(world_id)["relationships"]["user:geoff"]
    assert relation["stage"] == "stranger"
    stage_events = [
        event for event in kernel.events(world_id)
        if event.event_type == "RelationshipStageEvaluated"
    ]
    assert stage_events[-1].payload["from_stage"] == "acquaintance"
    assert stage_events[-1].payload["stage"] == "stranger"
    assert stage_events[-1].payload["reason"] == "relationship_boundary_regression"


def test_duplicate_appraisal_is_idempotent_and_cannot_double_count_relationship_time(
    tmp_path: Path,
) -> None:
    kernel, world_id = _world(tmp_path)
    command = {
        "type": "appraise_turn",
        "world_id": world_id,
        "appraisal": "warmth_received",
        "intent_id": "turn:duplicate",
        "message_id": "relationship-message:duplicate",
        "user_id": "user:geoff",
        "idempotency_key": "appraise:duplicate",
    }
    first = kernel.submit(command, expected_revision=kernel.revision(world_id))
    second = kernel.submit(command, expected_revision=kernel.revision(world_id))

    assert second.revision == first.revision
    assert kernel.snapshot(world_id)["relationships"]["user:geoff"]["interaction_count"] == 1


def test_relationship_stage_projection_rebuild_matches_online_hash(tmp_path: Path) -> None:
    kernel, world_id = _world(tmp_path)
    for index in range(1, 5):
        _appraise(kernel, world_id, index, "warmth_received")

    report = kernel.rebuild_projection(world_id, "world_current_state")

    assert report.matches_live is True
    assert report.state_hash == kernel.dashboard_overview(world_id)["state_hash"]
    assert kernel.snapshot(world_id)["relationships"]["user:geoff"]["stage"] == "acquaintance"


def test_stage_thresholds_are_deterministic() -> None:
    cases = (
        ("stranger", 4, 18, 0, "acquaintance"),
        ("acquaintance", 12, 25, 18, "friend"),
        ("friend", 35, 45, 35, "close_friend"),
        ("close_friend", 70, 55, 55, "ambiguous"),
        ("ambiguous", 120, 70, 75, "lover"),
    )
    for current, interaction_count, trust, closeness, expected in cases:
        stage, reason = evaluate_relationship_stage(
            {
                "stage": current,
                "interaction_count": interaction_count,
                "trust": trust,
                "closeness": closeness,
            }
        )
        assert stage == expected
        assert reason == "relationship_progression"


def test_slow_warm_personality_and_event_significance_shape_stage_thresholds() -> None:
    evidence = {
        "stage": "stranger",
        "interaction_count": 5,
        "trust": 25,
        "closeness": 20,
    }

    slow_stage, slow_reason = evaluate_relationship_stage(
        evidence,
        slow_warmth=90,
        event_significance=0,
    )
    significant_stage, significant_reason = evaluate_relationship_stage(
        evidence,
        slow_warmth=90,
        event_significance=1,
    )

    assert (slow_stage, slow_reason) == ("stranger", "relationship_steady")
    assert (significant_stage, significant_reason) == (
        "acquaintance",
        "relationship_progression",
    )


def test_world_reads_protagonist_slow_warmth_and_audits_event_significance(
    tmp_path: Path,
) -> None:
    kernel = WorldKernel(CompanionStore(tmp_path / "slow-warm-stage.sqlite"))
    started = kernel.submit(
        {
            "type": "start_world",
            "seed": {
                "world_id": "slow-warm-stage",
                "logical_at": "2026-07-11T09:00:00+00:00",
                "protagonist": {
                    "id": "zhizhi",
                    "name": "沈知栀",
                    "kind": "companion",
                    "relationship_pacing": {"slow_warmth": 90},
                },
                "daily_schedule": [],
                "npcs": [],
            },
        },
        expected_revision=0,
    )
    kernel.submit(
        {
            "type": "register_user",
            "world_id": started.world_id,
            "user_id": "user:geoff",
            "name": "geoff",
        },
        expected_revision=started.revision,
    )

    for index in range(1, 5):
        _appraise(kernel, started.world_id, index, "warmth_received")
    assert kernel.snapshot(started.world_id)["relationships"]["user:geoff"]["stage"] == "stranger"

    _appraise(kernel, started.world_id, 5, "warmth_received")
    relation = kernel.snapshot(started.world_id)["relationships"]["user:geoff"]
    evaluated = [
        event
        for event in kernel.events(started.world_id)
        if event.event_type == "RelationshipStageEvaluated"
    ][-1]

    assert relation["stage"] == "acquaintance"
    assert evaluated.payload["slow_warmth"] == 90
    assert evaluated.payload["event_significance"] == 1
    assert evaluated.payload["effective_thresholds"] == {
        "interaction_count": 6,
        "trust": 22,
        "closeness": 0,
    }



def test_logical_time_alone_does_not_promote_relationship(tmp_path: Path) -> None:
    from datetime import timedelta

    kernel, world_id = _world(tmp_path)
    now = __import__("datetime").datetime.fromisoformat(
        str(kernel.snapshot(world_id)["clock"]["logical_at"])
    )
    kernel.advance(
        world_id,
        now + timedelta(days=30),
        expected_revision=kernel.revision(world_id),
    )
    relation = kernel.snapshot(world_id)["relationships"]["user:geoff"]
    assert relation["stage"] == "stranger"
    assert relation["interaction_count"] == 0


def test_expression_boundary_reads_stage_not_accidental_numeric_average() -> None:
    candidate = {"reply_text": "宝宝，当然永远爱你。", "claims": []}
    assert human_reply_contract_violation(
        "你喜欢我吗？",
        candidate,
        {
            "stage": "stranger",
            "interaction_count": 999,
            "trust": 90,
            "closeness": 90,
        },
    ) == "relationship_language_exceeds_current_closeness"


def test_world_interaction_classifier_reads_the_projected_stage() -> None:
    event = interpret_interaction(
        IncomingMessage(platform="simulator", platform_user_id="geoff", text="我爱你"),
        MoodState(relationship_stage="stranger"),
        relationship_stage="friend",
    )
    assert event.kind != "premature_intimacy"


@pytest.mark.asyncio
async def test_world_proactive_text_is_rejected_when_it_crosses_the_projected_stage(
    tmp_path: Path,
) -> None:
    class IntimateProactiveModel:
        async def complete(self, messages, *, temperature: float) -> str:
            return (
                '{"private_thought":"想亲近一点。","should_send":true,'
                '"platform":"qq","message_type":"text","message":"宝宝，我想你了。"}'
            )

    kernel, world_id = _world(tmp_path)
    for index in range(1, 5):
        _appraise(kernel, world_id, index, "warmth_received")
    engine = CompanionEngine(
        kernel.store,
        IntimateProactiveModel(),
        "你是沈知栀。",
        world_kernel=kernel,
        world_id=world_id,
    )

    decision = await engine.proactive_tick("geoff")

    assert decision.should_send is False
    assert "关系阶段门禁" in decision.private_thought


@pytest.mark.asyncio
async def test_world_reply_prompt_reads_the_projected_friend_stage(tmp_path: Path) -> None:
    class CapturingModel:
        def __init__(self) -> None:
            self.calls: list[list[dict[str, str]]] = []

        async def complete(self, messages, *, temperature: float) -> str:
            self.calls.append(messages)
            return '{"reply_text":"嗯，我听着。","mentioned_event_ids":[],"proposed_action_ids":[],"claims":[]}'

    kernel, world_id = _world(tmp_path)
    for index in range(1, 25):
        _appraise(kernel, world_id, index, "warmth_received")
        if kernel.snapshot(world_id)["relationships"]["user:geoff"]["stage"] == "friend":
            break
    assert kernel.snapshot(world_id)["relationships"]["user:geoff"]["stage"] == "friend"
    model = CapturingModel()
    engine = CompanionEngine(
        kernel.store,
        model,
        "你是沈知栀。",
        world_kernel=kernel,
        world_id=world_id,
    )

    reply = await engine.handle_message(
        IncomingMessage(
            platform="simulator",
            platform_user_id="geoff",
            message_id="friend-stage-reply",
            text="急，你今天还好吗？",
        )
    )

    assert reply is not None
    prompt = "\n".join(str(item.get("content") or "") for item in model.calls[0])
    assert "关系投影" in prompt
    assert '"stage":"friend"' in prompt
    assert "当前表达指导(friend)" in prompt

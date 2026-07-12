from datetime import datetime, timedelta
from pathlib import Path

from companion_daemon.db import CompanionStore
from companion_daemon.image_requests import detect_image_request
from companion_daemon.world import WorldKernel
from companion_daemon.world_affect import public_mood
from companion_daemon.world_behavior import WorldBehaviorPolicy
from companion_daemon.world_media import WorldMediaPolicy


def _world(tmp_path: Path) -> tuple[WorldKernel, str]:
    kernel = WorldKernel(CompanionStore(tmp_path / "negative-affect.sqlite"))
    started = kernel.start_from_seed_file(Path("configs/world_seed.yaml"))
    user = kernel.submit(
        {
            "type": "register_user",
            "world_id": started.world_id,
            "user_id": "user:geoff",
            "name": "geoff",
            "idempotency_key": "register:affect:user",
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
            "intent_id": f"affect-turn:{index}",
            "message_id": f"affect-message:{index}",
            "user_id": "user:geoff",
            "idempotency_key": f"affect-appraise:{index}",
        },
        expected_revision=kernel.revision(world_id),
    )


def test_boundary_violation_creates_a_traceable_negative_affect_projection(
    tmp_path: Path,
) -> None:
    kernel, world_id = _world(tmp_path)
    _appraise(kernel, world_id, 1, "boundary_violation")

    affect = kernel.snapshot(world_id)["emotion_modulation"]
    assert affect["vector"]["hurt"] == 18
    assert affect["vector"]["anger"] == 12
    assert affect["vector"]["resentment"] == 8
    assert affect["behavior_tendency"] == "guarded"
    assert affect["unresolved"] is True
    assert any(
        event.event_type == "AffectChanged"
        and event.payload["source_appraisal"] == "boundary_violation"
        for event in kernel.events(world_id)
    )


def test_repeated_harm_accumulates_into_withdrawal_and_repair_is_not_instant_forgiveness(
    tmp_path: Path,
) -> None:
    kernel, world_id = _world(tmp_path)
    _appraise(kernel, world_id, 1, "boundary_violation")
    _appraise(kernel, world_id, 2, "boundary_violation")
    hurt = kernel.snapshot(world_id)["emotion_modulation"]
    assert hurt["vector"]["hurt"] == 36
    assert hurt["behavior_tendency"] == "withdraw"
    assert hurt["unresolved"] is True

    _appraise(kernel, world_id, 3, "repair_attempt")
    repaired = kernel.snapshot(world_id)["emotion_modulation"]
    assert repaired["vector"]["hurt"] < hurt["vector"]["hurt"]
    assert repaired["vector"]["hurt"] > 0
    assert repaired["unresolved"] is True
    assert repaired["behavior_tendency"] == "repair_open"


def test_hurt_does_not_prevent_care_when_the_user_is_vulnerable(tmp_path: Path) -> None:
    kernel, world_id = _world(tmp_path)
    _appraise(kernel, world_id, 1, "boundary_violation")
    _appraise(kernel, world_id, 2, "user_vulnerable")

    affect = kernel.snapshot(world_id)["emotion_modulation"]
    assert affect["vector"]["hurt"] == 18
    assert affect["unresolved"] is True
    assert affect["behavior_tendency"] == "caring"


def test_an_ordinary_turn_does_not_reset_an_unresolved_feeling(tmp_path: Path) -> None:
    kernel, world_id = _world(tmp_path)
    _appraise(kernel, world_id, 1, "boundary_violation")
    _appraise(kernel, world_id, 2, "ordinary_message")

    affect = kernel.snapshot(world_id)["emotion_modulation"]
    assert affect["unresolved"] is True
    assert affect["behavior_tendency"] == "guarded"
    assert affect["mode"] == "guarded"


def test_logical_time_decays_negative_affect_without_erasing_it_immediately(
    tmp_path: Path,
) -> None:
    kernel, world_id = _world(tmp_path)
    _appraise(kernel, world_id, 1, "boundary_violation")
    now = datetime.fromisoformat(
        str(kernel.snapshot(world_id)["clock"]["logical_at"])
    )

    kernel.advance(
        world_id,
        now + timedelta(hours=3),
        expected_revision=kernel.revision(world_id),
    )

    affect = kernel.snapshot(world_id)["emotion_modulation"]
    assert affect["vector"]["hurt"] < 18
    assert affect["vector"]["hurt"] > 0
    assert any(event.event_type == "AffectDecayed" for event in kernel.events(world_id))


def test_sub_hour_clock_ticks_carry_decay_remainder_forward(tmp_path: Path) -> None:
    kernel, world_id = _world(tmp_path)
    _appraise(kernel, world_id, 1, "boundary_violation")
    now = datetime.fromisoformat(str(kernel.snapshot(world_id)["clock"]["logical_at"]))

    first = kernel.advance(
        world_id,
        now + timedelta(minutes=30),
        expected_revision=kernel.revision(world_id),
    )
    assert kernel.snapshot(world_id)["emotion_modulation"]["vector"]["hurt"] == 18
    assert any(
        event.event_type == "AffectDecayed" and event.payload["elapsed_seconds"] == 1800
        for event in first.events
    )

    kernel.advance(
        world_id,
        now + timedelta(hours=1),
        expected_revision=kernel.revision(world_id),
    )
    assert kernel.snapshot(world_id)["emotion_modulation"]["vector"]["hurt"] == 16


def test_logical_decay_that_clears_affect_has_an_explicit_resolution_event(tmp_path: Path) -> None:
    kernel, world_id = _world(tmp_path)
    _appraise(kernel, world_id, 1, "boundary_violation")
    now = datetime.fromisoformat(str(kernel.snapshot(world_id)["clock"]["logical_at"]))

    kernel.advance(
        world_id,
        now + timedelta(hours=12),
        expected_revision=kernel.revision(world_id),
    )

    events = kernel.events(world_id)
    assert any(event.event_type == "AffectResolved" for event in events)
    # A later, independently settled NPC conflict may create new unresolved
    # emotion without undoing the explicit resolution of the user's offense.
    affect = kernel.snapshot(world_id)["emotion_modulation"]
    assert affect["source_appraisal"] != "boundary_violation"


def test_repeated_reliable_repair_can_resolve_negative_affect_explicitly(tmp_path: Path) -> None:
    kernel, world_id = _world(tmp_path)
    _appraise(kernel, world_id, 1, "boundary_violation")
    for index in range(2, 5):
        _appraise(kernel, world_id, index, "repair_attempt")

    affect = kernel.snapshot(world_id)["emotion_modulation"]
    assert affect["unresolved"] is False
    assert any(event.event_type == "AffectResolved" for event in kernel.events(world_id))


def test_temporary_user_busyness_does_not_invent_resentment(tmp_path: Path) -> None:
    kernel, world_id = _world(tmp_path)
    _appraise(kernel, world_id, 1, "availability_drop")

    affect = kernel.snapshot(world_id)["emotion_modulation"]
    assert affect["vector"]["resentment"] == 0
    assert affect["vector"]["hurt"] == 0
    assert affect["behavior_tendency"] == "patient"


def test_affect_projection_rebuild_matches_online_hash(tmp_path: Path) -> None:
    kernel, world_id = _world(tmp_path)
    _appraise(kernel, world_id, 1, "boundary_violation")
    _appraise(kernel, world_id, 2, "repair_attempt")
    report = kernel.rebuild_projection(world_id, "world_current_state")

    assert report.matches_live is True
    assert report.state_hash == kernel.dashboard_overview(world_id)["state_hash"]


def test_unresolved_hurt_changes_expression_guidance_without_becoming_abusive() -> None:
    guidance = WorldBehaviorPolicy().expression_guidance(
        {
            "needs": {"boundary": 25},
            "relationships": {"user:geoff": {"stage": "friend"}},
            "emotion_modulation": {
                "mode": "guarded",
                "behavior_tendency": "withdraw",
                "unresolved": True,
                "vector": {"hurt": 36, "anger": 24, "resentment": 16},
            },
        },
        user_id="user:geoff",
    )

    assert guidance.label == "affect_withdraw"
    assert "情绪还没过去" in guidance.prompt_line
    assert "辱骂" in guidance.prompt_line


def test_unanswered_world_thread_leaves_a_traceable_residual_feeling(tmp_path: Path) -> None:
    kernel, world_id = _world(tmp_path)
    now = datetime.fromisoformat(
        str(kernel.snapshot(world_id)["clock"]["logical_at"])
    )
    delivery_id, _, _ = kernel.queue_outgoing_action(
        canonical_user_id="geoff",
        platform="simulator",
        text="你今天还好吗？",
        kind="reply",
        expires_at=now + timedelta(hours=2),
        trace={
            "world_id": world_id,
            "appraisal": "ordinary_message",
            "expression_policy": "test",
            "allowed_facts": [],
            "observable_reason": "test",
            "conversation_thread": {
                "thread_id": "thread:affect",
                "user_id": "user:geoff",
                "question": "你今天还好吗？",
                "expires_at": (now + timedelta(minutes=30)).isoformat(),
            },
        },
    )
    kernel.settle_outgoing_action(delivery_id, delivered=True)

    kernel.advance(
        world_id,
        now + timedelta(minutes=31),
        expected_revision=kernel.revision(world_id),
    )

    snapshot = kernel.snapshot(world_id)
    assert snapshot["conversation_threads"]["thread:affect"]["status"] == "expired"
    affect = snapshot["emotion_modulation"]
    assert affect["vector"]["sadness"] == 4
    assert affect["vector"]["loneliness"] == 3
    assert affect["vector"]["anxiety"] == 5
    assert affect["unresolved"] is True
    assert any(
        event.event_type == "AffectChanged"
        and event.payload["source_appraisal"] == "conversation_thread_expired"
        and event.payload["source_reference"] == "conversation_thread:thread:affect"
        for event in kernel.events(world_id)
    )


def test_thread_expiry_in_a_long_jump_is_decayed_only_after_it_occurs(tmp_path: Path) -> None:
    kernel, world_id = _world(tmp_path)
    now = datetime.fromisoformat(str(kernel.snapshot(world_id)["clock"]["logical_at"]))
    delivery_id, _, _ = kernel.queue_outgoing_action(
        canonical_user_id="geoff",
        platform="simulator",
        text="你今天还好吗？",
        kind="reply",
        expires_at=now + timedelta(hours=6),
        trace={
            "world_id": world_id,
            "appraisal": "ordinary_message",
            "expression_policy": "test",
            "allowed_facts": [],
            "observable_reason": "test",
            "conversation_thread": {
                "thread_id": "thread:long-affect",
                "user_id": "user:geoff",
                "question": "你今天还好吗？",
                "expires_at": (now + timedelta(hours=1)).isoformat(),
            },
        },
    )
    kernel.settle_outgoing_action(delivery_id, delivered=True)
    kernel.advance(
        world_id,
        now + timedelta(hours=3),
        expected_revision=kernel.revision(world_id),
    )

    affect_events = [
        event
        for event in kernel.events(world_id)
        if event.event_type in {"AffectChanged", "AffectDecayed"}
    ]
    expiry_index = next(
        index
        for index, event in enumerate(affect_events)
        if event.payload.get("source_appraisal") == "conversation_thread_expired"
        and event.event_type == "AffectChanged"
    )
    decayed_expiry = affect_events[expiry_index + 1]
    assert decayed_expiry.event_type == "AffectDecayed"
    assert decayed_expiry.payload["vector"]["sadness"] == 2
    assert decayed_expiry.payload["vector"]["loneliness"] == 1
    assert decayed_expiry.payload["vector"]["anxiety"] == 1
    assert decayed_expiry.payload["source_appraisal"] == "conversation_thread_expired"

    # A life outcome at the same logical instant may legitimately continue to
    # reshape the final affect; it must not retroactively change expiry decay.
    final_affect = kernel.snapshot(world_id)["emotion_modulation"]
    assert final_affect["source_appraisal"] == "social_warmth"


def test_unresolved_hurt_defers_ordinary_reply_but_allows_explicit_repair() -> None:
    policy = WorldBehaviorPolicy()
    state = {
        "needs": {"boundary": 0, "security": 50},
        "emotion_modulation": {
            "behavior_tendency": "withdraw",
            "unresolved": True,
            "vector": {"hurt": 36},
        },
    }

    deferred = policy.communication_decision(state, text="在吗")
    repair = policy.communication_decision(state, text="对不起，我刚才不该那样说")

    assert deferred.attention == "deferred"
    assert deferred.reason == "unresolved_hurt"
    assert repair.attention == "seen"


def test_unresolved_hurt_does_not_delay_a_vulnerable_user_or_real_repair() -> None:
    policy = WorldBehaviorPolicy()
    state = {
        "needs": {"boundary": 0, "security": 50},
        "emotion_modulation": {
            "behavior_tendency": "withdraw",
            "unresolved": True,
            "vector": {"hurt": 36},
        },
    }

    vulnerable = policy.communication_decision(state, text="我真的快崩溃了")
    repair = policy.communication_decision(state, text="我刚才说重了，认真道歉")

    assert vulnerable.attention == "seen"
    assert vulnerable.reason == "user_vulnerable_turn"
    assert repair.attention == "seen"


def test_unresolved_hurt_blocks_intimate_media_even_when_relationship_is_ready() -> None:
    request = detect_image_request("能发一张自拍吗")
    decision = WorldMediaPolicy().image_decision(
        {
            "needs": {"boundary": 0, "security": 60},
            "relationships": {"user:geoff": {"stage": "close_friend", "closeness": 8, "respect": 10}},
            "emotion_modulation": {"unresolved": True, "vector": {"hurt": 24}},
        },
        user_id="user:geoff",
        request=request,
        user_text="能发一张自拍吗",
    )

    assert decision.allowed is True
    assert decision.reason == "unresolved_negative_affect"


def test_unresolved_patient_affect_also_blocks_unsolicited_outreach() -> None:
    decision = WorldBehaviorPolicy().outreach_constraint(
        {
            "relationships": {"user:geoff": {"stage": "friend"}},
            "emotion_modulation": {
                "behavior_tendency": "patient",
                "unresolved": True,
                "vector": {"sadness": 4, "loneliness": 3, "anxiety": 5},
            },
        },
        user_id="user:geoff",
    )

    assert decision.allowed is True
    assert decision.reason == "unresolved_negative_affect"
    assert decision.requires_deliberation is True


def test_world_affect_is_adapted_to_existing_client_moods() -> None:
    assert public_mood({"behavior_tendency": "withdraw"}) == "sulking"
    assert public_mood({"behavior_tendency": "guarded", "mode": "guarded"}) == "guarded"
    assert public_mood({"behavior_tendency": "patient", "unresolved": True}) == "hurt"
    assert public_mood({"behavior_tendency": "caring", "expression": "worry"}) == "worried"

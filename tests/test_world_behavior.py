from companion_daemon.world_behavior import WorldBehaviorPolicy
from companion_daemon.image_requests import detect_image_request
from companion_daemon.world_media import WorldMediaPolicy


def test_communication_policy_defers_only_for_explicit_world_constraint() -> None:
    policy = WorldBehaviorPolicy()
    state = {
        "agenda": {"study": {"status": "active"}},
        "needs": {"energy": 30, "security": 50, "boundary": 0},
    }

    decision = policy.communication_decision(state, text="晚点聊")

    assert decision.attention == "deferred"
    assert decision.defer_minutes == 20
    assert decision.reason == "active_world_activity_low_energy"
    assert policy.communication_decision(state, text="急，能回一下吗").attention == "seen"


def test_communication_policy_uses_do_not_disturb_for_pressure_and_high_boundary() -> None:
    decision = WorldBehaviorPolicy().communication_decision(
        {"needs": {"boundary": 80, "security": 40}}, text="你现在就必须发给我"
    )

    assert decision.attention == "do_not_disturb"


def test_outreach_constraint_uses_only_world_threads_actions_and_needs() -> None:
    policy = WorldBehaviorPolicy()
    state = {
        "conversation_threads": {
            "one": {"status": "open", "user_id": "user:geoff"},
        },
        "actions": {},
        "needs": {"boundary": 0, "security": 50},
    }

    blocked = policy.outreach_constraint(state, user_id="user:geoff")

    assert blocked.allowed is False
    assert blocked.reason == "open_conversation_thread"


def test_expression_guidance_is_derived_from_world_state_without_private_memory() -> None:
    guidance = WorldBehaviorPolicy().expression_guidance(
        {"needs": {"boundary": 60, "energy": 70}, "emotion_modulation": {"mode": "guarded"}}
    )

    assert guidance.label == "guarded"
    assert "不讨好" in guidance.prompt_line


def test_relationship_stage_changes_expression_and_outreach_constraints() -> None:
    policy = WorldBehaviorPolicy()

    stranger_guidance = policy.expression_guidance(
        {
            "needs": {"boundary": 0, "energy": 70},
            "emotion_modulation": {"mode": "calm"},
            "relationships": {"user:geoff": {"stage": "stranger"}},
        },
        user_id="user:geoff",
    )
    friend_guidance = policy.expression_guidance(
        {
            "needs": {"boundary": 0, "energy": 70},
            "emotion_modulation": {"mode": "calm"},
            "relationships": {"user:geoff": {"stage": "friend"}},
        },
        user_id="user:geoff",
    )

    assert stranger_guidance.label == "slow_warm"
    assert friend_guidance.label == "friend"
    assert policy.outreach_constraint(
        {"relationships": {"user:geoff": {"stage": "stranger"}}}, user_id="user:geoff"
    ).allowed is False
    assert policy.outreach_constraint(
        {"relationships": {"user:geoff": {"stage": "acquaintance"}}}, user_id="user:geoff"
    ).allowed is True


def test_world_media_policy_uses_world_relation_and_boundary_not_moodstate() -> None:
    policy = WorldMediaPolicy()
    request = detect_image_request("能发一张自拍吗")

    denied = policy.image_decision(
        {"needs": {"boundary": 70, "security": 40}, "relationships": {"user:geoff": {"closeness": 8, "respect": 0}}},
        user_id="user:geoff", request=request, user_text="你现在就必须发自拍",
    )
    allowed = policy.image_decision(
        {"needs": {"boundary": 0, "security": 55}, "relationships": {"user:geoff": {"stage": "close_friend", "closeness": 8, "respect": 0}}},
        user_id="user:geoff", request=request, user_text="能发一张自拍吗",
    )

    assert denied.allowed is False
    assert denied.reason == "boundary_high_under_pressure"
    assert allowed.allowed is True
    assert allowed.kind == "selfie"

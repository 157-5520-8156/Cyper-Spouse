from companion_daemon.world_behavior import WorldBehaviorPolicy, outbound_projection
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


def test_attention_is_ranked_from_world_facts_instead_of_a_fixed_hurt_timer() -> None:
    policy = WorldBehaviorPolicy()
    base = {
        "agenda": {},
        "needs": {"energy": 55, "security": 35, "boundary": 35, "attention": 50},
        "emotion_modulation": {
            "behavior_tendency": "withdraw",
            "vector": {"hurt": 45},
            "unresolved": True,
        },
        "relationships": {"user:geoff": {"stage": "friend", "trust": 35}},
    }

    first = policy.communication_decision(base, text="你在吗", user_id="user:geoff")
    tired = policy.communication_decision(
        {**base, "needs": {**base["needs"], "energy": 20}},
        text="你在吗",
        user_id="user:geoff",
    )

    assert first.attention == "deferred"
    assert {candidate.attention for candidate in first.candidates} == {
        "seen", "deferred", "do_not_disturb"
    }
    assert first.candidates[0].attention == first.attention
    assert tired.defer_minutes != first.defer_minutes
    assert tired.candidates[0].score > first.candidates[0].score
    assert policy.communication_decision(base, text="你在吗", user_id="user:geoff") == first


def test_urgent_or_vulnerable_turn_wins_ranking_without_erasing_other_candidates() -> None:
    decision = WorldBehaviorPolicy().communication_decision(
        {
            "needs": {"energy": 15, "security": 15, "boundary": 60},
            "emotion_modulation": {"behavior_tendency": "withdraw", "vector": {"hurt": 80}},
        },
        text="我真的撑不住了，能回一下吗",
    )

    assert decision.attention == "seen"
    assert decision.candidates[0].reason == "user_vulnerable_turn"
    assert len(decision.candidates) == 3


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

    assert blocked.allowed is True
    assert blocked.reason == "open_conversation_thread"
    assert blocked.requires_deliberation is True
    assert blocked.override_cost == 20


def test_outbound_projection_counts_only_messages_after_the_latest_user_turn() -> None:
    projection = outbound_projection(
        {
            "actions": {},
            "recent_messages": [
                {"direction": "out", "logical_at": "2026-07-11T08:00:00+00:00", "text": "旧消息"},
                {"direction": "in", "logical_at": "2026-07-11T09:00:00+00:00", "text": "用户回来了"},
                {"direction": "out", "logical_at": "2026-07-11T09:01:00+00:00", "text": "第一条"},
                {"direction": "out", "logical_at": "2026-07-11T09:02:00+00:00", "text": "第二条"},
            ],
        }
    )

    assert projection.unanswered_outbound_count == 2


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
    ).requires_deliberation is True
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


def test_world_media_policy_marks_explicit_intimate_request_as_relationship_private() -> None:
    policy = WorldMediaPolicy()
    request = detect_image_request("能发一张私密自拍吗")

    decision = policy.image_decision(
        {"needs": {"boundary": 0, "security": 70}, "relationships": {"user:geoff": {"stage": "lover", "closeness": 15, "respect": 12}}},
        user_id="user:geoff", request=request, user_text="能发一张私密自拍吗",
    )

    assert decision.allowed is True
    assert decision.kind == "relationship_private"


def test_world_media_policy_requires_stronger_world_state_for_bold_relationship_media() -> None:
    policy = WorldMediaPolicy()
    request = detect_image_request("能发一张大胆一点的私密自拍吗")
    not_ready = policy.image_decision(
        {"needs": {"boundary": 0, "security": 70}, "relationships": {"user:geoff": {"stage": "lover", "closeness": 15, "respect": 8}}},
        user_id="user:geoff", request=request, user_text="能发一张大胆一点的私密自拍吗",
    )
    allowed = policy.image_decision(
        {"needs": {"boundary": 10, "security": 70}, "relationships": {"user:geoff": {"stage": "lover", "closeness": 22, "respect": 14}}},
        user_id="user:geoff", request=request, user_text="能发一张大胆一点的私密自拍吗",
    )

    assert not_ready.reason == "relationship_tier_bold_not_ready"
    assert allowed.allowed is True
    assert allowed.intimacy_tier == "bold"

from __future__ import annotations

from companion_daemon.expression_plan import (
    ReplyCandidate,
    ReplySkeleton,
    ReplyTurnContext,
    compile_expression_plan,
)


def _episode(
    appraisal: str,
    intensity: int,
    valence: int,
    target: str,
    *,
    updated_at: str = "2026-07-12T09:00:00+00:00",
    display_multiplier: float = 1.0,
) -> dict[str, object]:
    return {
        "appraisal": appraisal,
        "intensity": intensity,
        "valence": valence,
        "target": target,
        "status": "active",
        "updated_at": updated_at,
        "emotion_program": {
            "process_effects": {"display_multiplier": display_multiplier}
        },
    }


def test_npc_spillover_plan_uses_one_policy_for_prompt_validation_and_fallback() -> None:
    plan = compile_expression_plan(
        {
            "active_episodes": [_episode("npc_conflict", 60, -50, "npc:roommate")],
            "vector": {"anger": 30},
            "unresolved": True,
            "profile": {"spillover_leakage_cap": 25},
        },
        {"stage": "acquaintance"},
        {"boundary": 20},
        current_appraisal="ordinary_message",
    )

    assert "不要把它算到用户头上" in plan.prompt_fragment
    assert plan.validate("都是你害我心情不好") == "spillover_misattributed_to_user"
    resolved = plan.resolve(
        ReplyCandidate("都是你害我心情不好"),
        safe_seed=ReplySkeleton("我现在有点烦，想慢一点说。"),
        turn=ReplyTurnContext(variant_key="turn-1"),
    )
    assert resolved.used_fallback is True
    assert "不是你的错" in resolved.reply_text
    assert "我现在有点烦，想慢一点说" in resolved.reply_text
    assert plan.validate(resolved.reply_text) is None


def test_unresolved_mixed_affect_cannot_be_erased_and_remains_visible_in_fallback() -> None:
    plan = compile_expression_plan(
        {
            "active_episodes": [
                _episode("boundary_violation", 55, -45, "companion"),
                _episode("warmth_received", 30, 35, "companion"),
            ],
            "vector": {"hurt": 25, "warmth": 18},
            "unresolved": True,
        },
        {"stage": "close_friend"},
        {"boundary": 50},
        current_appraisal="repair_attempt",
    )

    assert plan.policy_spec.mixed is True
    assert plan.validate("没关系，我一点都不介意。") == "unresolved_affect_denied"
    resolved = plan.resolve(
        ReplyCandidate("没关系，我一点都不介意。"),
        safe_seed=ReplySkeleton("我听到了。"),
        turn=ReplyTurnContext(speech_act="repair", variant_key="turn-2"),
    )
    assert "还没有完全过去" in resolved.reply_text
    assert "愿意听你把话说完" in resolved.reply_text


def test_repair_plan_accepts_specific_acknowledgement_without_forcing_forgiveness() -> None:
    plan = compile_expression_plan(
        {
            "active_episodes": [_episode("boundary_violation", 35, -40, "companion")],
            "vector": {"hurt": 18},
            "unresolved": True,
        },
        {"stage": "close_friend"},
        {"boundary": 45},
        current_appraisal="repair_attempt",
    )
    text = "我听见你的道歉了，但这件事还没有完全过去。"
    assert plan.validate(text) is None
    assert plan.resolve(
        ReplyCandidate(text),
        safe_seed=ReplySkeleton("我听见了。"),
        turn=ReplyTurnContext(speech_act="repair"),
    ).used_fallback is False


def test_small_diffuse_vector_values_do_not_authorize_an_emotion_claim() -> None:
    plan = compile_expression_plan(
        {
            "active_episodes": [],
            "vector": {"hurt": 1, "anger": 1, "sadness": 1, "anxiety": 1},
            "unresolved": False,
        },
        {"stage": "stranger"},
        {},
        current_appraisal="ordinary_message",
    )
    assert plan.validate("我很难过。") == "uncommitted_companion_affect"


def test_plan_hash_binds_user_intent_and_world_revision() -> None:
    affect = {"active_episodes": [], "vector": {}, "unresolved": False}
    alice = compile_expression_plan(
        affect, {}, {}, current_appraisal="ordinary_message",
        revision=7, user_id="user:alice", intent_id="intent:1",
    )
    bob = compile_expression_plan(
        affect, {}, {}, current_appraisal="ordinary_message",
        revision=7, user_id="user:bob", intent_id="intent:1",
    )
    assert alice.plan_hash != bob.plan_hash
    assert (alice.revision, alice.user_id, alice.intent_id) == (7, "user:alice", "intent:1")


def test_fallback_keeps_a_valid_grounded_skeleton_and_its_provenance_together() -> None:
    plan = compile_expression_plan(
        {
            "active_episodes": [_episode("npc_conflict", 50, -40, "npc:roommate")],
            "vector": {"anger": 20},
            "unresolved": False,
        },
        {},
        {},
        current_appraisal="ordinary_message",
    )
    result = plan.resolve(
        ReplyCandidate("都是你让我这么烦"),
        safe_seed=ReplySkeleton(
            "我只确定室友刚才提高了声音。",
            mentioned_event_ids=("event:1",),
            claims=({"source_id": "event:1", "text": "室友提高声音"},),
        ),
        turn=ReplyTurnContext(variant_key="turn-3"),
    )
    assert "室友刚才提高了声音" in result.reply_text
    assert result.mentioned_event_ids == ("event:1",)
    assert result.claims[0]["source_id"] == "event:1"


def test_current_recipient_relevance_beats_unrelated_intense_npc_episode() -> None:
    plan = compile_expression_plan(
        {
            "active_episodes": [
                _episode(
                    "npc_conflict",
                    95,
                    -1,
                    "npc:roommate",
                    updated_at="2026-07-11T09:00:00+00:00",
                ),
                _episode(
                    "boundary_violation",
                    55,
                    -1,
                    "companion",
                    updated_at="2026-07-12T09:00:00+00:00",
                ),
            ],
            "vector": {"anger": 30, "hurt": 20},
            "unresolved": True,
        },
        {},
        {},
        current_appraisal="boundary_violation",
        user_id="user:geoff",
        intent_id="reply:current-user",
    )

    assert plan.policy_spec.primary_appraisal == "boundary_violation"
    assert plan.policy_spec.attribution_target == "companion"
    assert plan.policy_spec.regulation_strategy == "boundary_expression"


def test_suppressed_episode_has_lower_expression_accessibility() -> None:
    plan = compile_expression_plan(
        {
            "active_episodes": [
                _episode("npc_conflict", 70, -1, "npc:roommate"),
                _episode(
                    "boundary_violation",
                    65,
                    -1,
                    "companion",
                    display_multiplier=0.2,
                ),
            ],
            "vector": {"anger": 20, "hurt": 18},
            "unresolved": True,
        },
        {},
        {},
        current_appraisal="ordinary_message",
    )

    assert plan.policy_spec.primary_appraisal == "npc_conflict"

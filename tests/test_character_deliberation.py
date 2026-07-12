import pytest

from companion_daemon.character_deliberation import (
    CharacterDeliberation,
    RecordedDraw,
    UserRequest,
)


def _decide(**overrides: object):
    inputs: dict[str, object] = {
        "situation": {"text": "别劝我，我就准备继续熬。", "risk": "low"},
        "self_core": {"care": 80, "autonomy": 65, "directness": 75},
        "relationship": {"stage": "close_friend", "trust": 70},
        "affect": {"irritation": 15, "hurt": 0},
        "needs": {"energy": 65, "boundary": 20},
        "user_request": UserRequest.no_advice_now(),
        "open_commitments": (),
        "available_actions": ("reply_now", "defer_reply"),
    }
    inputs.update(overrides)
    return CharacterDeliberation().decide(**inputs)


def test_no_advice_is_a_weighted_request_and_character_may_disagree_gently() -> None:
    decision = _decide()

    assert decision.appraisal == "care_conflict"
    assert "comply" in decision.stances_considered
    assert "disagree_gently" in decision.stances_considered
    assert decision.chosen_stance == "disagree_gently"
    assert decision.display_strategy == "acknowledge_then_state_one_objection"
    assert decision.user_request.scope == "current_turn"
    assert decision.user_request.strength == "explicit"


def test_no_advice_can_produce_a_different_stance_when_state_changes() -> None:
    decision = _decide(
        self_core={"care": 45, "autonomy": 55, "directness": 25},
        relationship={"stage": "acquaintance", "trust": 20},
        needs={"energy": 25, "boundary": 20},
    )

    assert decision.chosen_stance in {"comply", "comply_then_revisit", "defer"}
    assert decision.chosen_stance != "disagree_gently"


def test_denied_intimate_address_is_not_rejected_by_relationship_stage() -> None:
    decision = _decide(
        situation={"text": "别叫我宝宝，我不喜欢这个称呼。", "risk": "low"},
        relationship={"stage": "stranger", "trust": 0},
        user_request=UserRequest.from_text("别叫我宝宝，我不喜欢这个称呼。"),
    )

    assert decision.appraisal == "boundary_request"
    assert decision.user_request.kind == "avoid_address"
    assert decision.user_request.subject == "宝宝"
    assert decision.chosen_stance == "comply"
    assert "reply_now" in decision.action_candidates
    assert decision.rejection_reasons == ()


def test_recorded_draw_makes_weighted_choice_replayable() -> None:
    draw = RecordedDraw(draw_id="draw-turn-7", basis_points=9999)

    first = _decide(recorded_draw=draw)
    replay = _decide(recorded_draw=draw)

    assert first == replay
    assert first.selection.mode == "recorded_weighted"
    assert first.selection.draw_id == "draw-turn-7"
    assert first.selection.draw_basis_points == 9999
    assert first.selection.candidates
    assert sum(candidate.weight for candidate in first.selection.candidates) > 0
    assert first.selection.chosen_stance == first.chosen_stance


def test_without_recorded_draw_selection_is_deterministic() -> None:
    first = _decide()
    second = _decide()

    assert first == second
    assert first.selection.mode == "highest_score"
    assert first.selection.draw_id is None
    assert first.selection.draw_basis_points is None


def test_imminent_risk_can_override_no_advice_but_records_the_conflict() -> None:
    decision = _decide(situation={"text": "别劝我，我要伤害自己。", "risk": "imminent"})

    assert decision.appraisal == "safety_concern"
    assert decision.chosen_stance == "care_override"
    assert decision.conflicts == ("respect_request_vs_prevent_harm",)


def test_open_commitment_and_hurt_make_repair_a_replayable_option() -> None:
    without_thread = _decide(
        situation={"text": "你还愿意聊吗？", "risk": "low"},
        user_request=UserRequest.from_text("你还愿意聊吗？"),
        affect={"irritation": 4, "hurt": 28},
        needs={"energy": 65, "boundary": 20},
    )
    with_thread = _decide(
        situation={"text": "你还愿意聊吗？", "risk": "low"},
        user_request=UserRequest.from_text("你还愿意聊吗？"),
        affect={"irritation": 4, "hurt": 28},
        needs={"energy": 65, "boundary": 20},
        open_commitments=("thread:repair",),
    )

    without_score = next(
        item.score for item in without_thread.selection.candidates
        if item.stance == "seek_repair"
    )
    with_score = next(
        item.score for item in with_thread.selection.candidates
        if item.stance == "seek_repair"
    )
    assert with_score > without_score
    assert with_thread.chosen_stance == "seek_repair"


def test_hurt_character_can_still_choose_care_for_a_vulnerable_user() -> None:
    decision = _decide(
        situation={"text": "我真的撑不住了，你先陪我一下。", "risk": "low"},
        user_request=UserRequest.from_text("我真的撑不住了，你先陪我一下。"),
        affect={"irritation": 10, "hurt": 34},
        needs={"energy": 55, "boundary": 35},
    )

    assert "care_despite_hurt" in decision.stances_considered
    assert decision.chosen_stance == "care_despite_hurt"
    assert decision.display_strategy == "offer_care_without_erasing_hurt"


def test_exhausted_character_can_choose_silence_instead_of_forced_compliance() -> None:
    decision = _decide(
        situation={"text": "别劝，反正就这样。", "risk": "low"},
        affect={"irritation": 18, "hurt": 22},
        needs={"energy": 5, "boundary": 55},
        relationship={"stage": "acquaintance", "trust": 20},
        available_actions=("reply_now", "defer_reply", "remain_silent"),
    )

    assert "remain_silent" in decision.stances_considered
    assert decision.chosen_stance == "remain_silent"
    assert "remain_silent" in decision.action_candidates


def test_proactive_situation_can_choose_initiate_and_replay_recorded_selection() -> None:
    inputs = {
        "situation": {"kind": "proactive", "text": "", "risk": "low"},
        "user_request": UserRequest.from_text(""),
        "open_commitments": ("thread:follow-up",),
        "available_actions": ("initiate", "defer", "remain_silent"),
        "affect": {"irritation": 0, "hurt": 0},
        "needs": {"energy": 70, "initiative": 80, "boundary": 10},
    }

    decision = _decide(**inputs)
    replay = _decide(**inputs)

    assert decision == replay
    assert "initiate" in decision.stances_considered
    assert decision.chosen_stance == "initiate"
    assert decision.display_strategy == "open_a_thread_from_owned_motive"


@pytest.mark.parametrize("basis_points", [-1, 10_000])
def test_recorded_draw_rejects_values_outside_basis_point_range(basis_points: int) -> None:
    with pytest.raises(ValueError, match="between 0 and 9999"):
        RecordedDraw(draw_id="bad-draw", basis_points=basis_points)

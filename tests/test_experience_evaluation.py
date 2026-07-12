import pytest

from companion_daemon.experience_evaluation import (
    ExperienceEvaluationError,
    ExperienceTurn,
    VariantRun,
    compare_five_turn_variants,
)


def turn(index: int, *, reply: str, note: str | None = None) -> ExperienceTurn:
    return ExperienceTurn(
        turn_id=f"turn-{index}",
        reply=reply,
        speech_act="respond_to_vulnerability",
        stance="care_override" if index == 0 else "comply_then_revisit",
        empathy=4,
        persona_continuity=4,
        grounding=5,
        agency=4,
        action_consequence="delivered",
        manual_review_note=note,
        factual_invariants=("character:name=知栀", "user:city=上海"),
    )


def test_turn_record_contains_every_required_human_experience_dimension() -> None:
    record = turn(0, reply="我还是有点担心，不过不逼你现在解释。", note="克制但有自己的意见")

    assert record.to_record() == {
        "turn_id": "turn-0",
        "reply": "我还是有点担心，不过不逼你现在解释。",
        "speech_act": "respond_to_vulnerability",
        "stance": "care_override",
        "empathy": 4,
        "persona_continuity": 4,
        "grounding": 5,
        "agency": 4,
        "action_consequence": "delivered",
        "manual_review_note": "克制但有自己的意见",
        "factual_invariants": ["character:name=知栀", "user:city=上海"],
    }


def test_ab_comparison_requires_five_turns_and_identical_fact_invariants() -> None:
    short = VariantRun("short", tuple(turn(i, reply=f"回复{i}") for i in range(4)))
    with pytest.raises(ExperienceEvaluationError, match="exactly five turns"):
        compare_five_turn_variants((short,))

    a = VariantRun("a", tuple(turn(i, reply=f"A-{i}") for i in range(5)))
    mismatched_turns = [turn(i, reply=f"B-{i}") for i in range(5)]
    mismatched_turns[3] = ExperienceTurn(
        **{
            **mismatched_turns[3].to_record(),
            "factual_invariants": ("character:name=知栀", "user:city=北京"),
        }
    )
    with pytest.raises(ExperienceEvaluationError, match="factual invariants"):
        compare_five_turn_variants((a, VariantRun("b", tuple(mismatched_turns))))


def test_ab_report_keeps_surface_diversity_diagnostic_separate_from_human_judgment() -> None:
    repeated = VariantRun(
        "repeated",
        tuple(turn(i, reply="我在。", note="连续模板感明显") for i in range(5)),
    )
    varied = VariantRun(
        "varied",
        tuple(
            turn(i, reply=reply, note="同一角色但表达随上下文变化")
            for i, reply in enumerate(
                ("我在。", "先坐一会儿也行。", "这次我不同意你。", "你慢慢说。", "我还记着这件事。")
            )
        ),
    )

    report = compare_five_turn_variants((repeated, varied))

    assert report.variants["varied"].surface_diversity > report.variants["repeated"].surface_diversity
    assert report.variants["varied"].human_review_complete is True
    assert report.variants["varied"].human_like is None
    assert report.warning == "diagnostics_do_not_replace_human_experience_review"


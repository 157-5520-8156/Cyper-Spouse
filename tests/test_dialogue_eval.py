import subprocess
import sys

import pytest

from companion_daemon.dialogue_eval import (
    PRAGMATIC_ADVERSARIAL_CASES,
    PragmaticAdversarialCase,
    PragmaticPrediction,
    ReplyEval,
    evaluate_reply,
    format_results,
    pragmatic_classification_metrics,
    run_baseline_scenarios,
    run_context_scenarios,
    run_pragmatic_adversarial_eval,
    summarize_results,
)


@pytest.mark.asyncio
async def test_scenario_runner_records_none_reply_without_crashing(monkeypatch) -> None:
    from companion_daemon import dialogue_eval

    class SilentEngine:
        async def handle_message(self, _message):
            return None

        async def aclose(self) -> None:
            return None

    monkeypatch.setattr(dialogue_eval, "build_companion_engine", lambda **_kwargs: SilentEngine())

    results = await dialogue_eval.run_scenarios(max_cases=1)

    assert results
    assert results[0][2].cleaned == "<no reply>"


@pytest.mark.asyncio
async def test_baseline_runner_keeps_bare_and_full_variants_isolated() -> None:
    report = await run_baseline_scenarios(max_cases=1)

    assert report.model_profile["bare_contract"].startswith("one model completion")
    assert {turn.variant for turn in report.turns} == {"bare", "full"}
    assert len(report.turns) == 6
    assert all(turn.end_to_end_complete_ms >= 0 for turn in report.turns)
    assert all(turn.first_visible_delivery_ms is None or turn.first_visible_delivery_ms >= 0 for turn in report.turns)
    assert all("legacy" not in turn.variant for turn in report.turns)


def test_context_regression_suite_passes() -> None:
    results = run_context_scenarios()

    assert results
    assert all(not result.issues for result in results)


def test_evaluate_reply_flags_common_ai_smells() -> None:
    result = evaluate_reply(
        "我理解你的意思，这个问题确实很重要。你可以先列步骤。你觉得呢？还好吗？",
        recent_assistant_questions=1,
    )
    codes = {issue.code for issue in result.issues}

    assert "assistantese" in codes
    assert "problem_solver" in codes
    assert "too_many_questions" in codes
    assert "question_after_question" in codes

    soft_advice = evaluate_reply("我有时候会出去走一圈。你要不要也试试？", user_text="我心里闷闷的")
    study_advice = evaluate_reply("你别整段硬啃，拆成小块可能会好一点。", user_text="毛概，好难背")
    music_advice = evaluate_reply("要不要听首歌，或者就随便说说话也行？", user_text="我心里闷闷的")
    bath_advice = evaluate_reply(
        "你今晚早点休息，或者去洗个热水澡，可能会好一些。", user_text="我心里闷闷的"
    )
    assert "problem_solver" in {issue.code for issue in soft_advice.issues}
    assert "problem_solver" in {issue.code for issue in study_advice.issues}
    assert "problem_solver" in {issue.code for issue in music_advice.issues}
    assert "problem_solver" in {issue.code for issue in bath_advice.issues}


def test_evaluate_reply_flags_stage_and_acquaintance_crutch() -> None:
    result = evaluate_reply("（刚看到）我有个高中同学也这样说过。")
    friend_photo = evaluate_reply("我之前看一个成都的朋友发过照片，感觉烟火气特别浓。")
    vague_classmate = evaluate_reply("成都理工啊，我好像有个高中同学在那。")
    classmate_study = evaluate_reply("我一个高中同学在那儿读过，说晚上特别热闹。")
    codes = {issue.code for issue in result.issues}

    assert "stage_direction" in codes
    assert "acquaintance_crutch" in codes
    assert "acquaintance_crutch" in {issue.code for issue in friend_photo.issues}
    assert "acquaintance_crutch" in {issue.code for issue in vague_classmate.issues}
    assert "acquaintance_crutch" in {issue.code for issue in classmate_study.issues}


def test_evaluate_reply_flags_thin_or_echo_only_replies() -> None:
    thin = evaluate_reply("嗯。", user_text="我明天考试，毛概真的好难背")
    echo = evaluate_reply("哦，成都理工啊。", user_text="我在成都上学呀，在成都理工哦")

    assert "low_engagement" in {issue.code for issue in thin.issues}
    echo_codes = {issue.code for issue in echo.issues}
    assert "low_engagement" in echo_codes
    assert "echo_only" in echo_codes


def test_evaluate_reply_flags_ungrounded_local_details_and_hidden_questions() -> None:
    local = evaluate_reply(
        "哦，成都理工啊。我之前刷到你们学校附近好像有家老书店，不知道现在还在不在。",
        user_text="我在成都上学呀，在成都理工哦",
    )
    local_known = evaluate_reply(
        "成理啊，我知道那附近有家面馆挺有名的。", user_text="我在成都理工哦"
    )
    local_scenery = evaluate_reply(
        "成都理工啊。听说秋天的时候学校还挺好看的。", user_text="我在成都理工哦"
    )
    hidden_question = evaluate_reply(
        "淋着去的还是找到伞了。", user_text="早上起来雨下很大，我伞还找不到"
    )

    assert "ungrounded_local_detail" in {issue.code for issue in local.issues}
    assert "ungrounded_local_detail" in {issue.code for issue in local_known.issues}
    assert "ungrounded_local_detail" in {issue.code for issue in local_scenery.issues}
    assert "flattened_question" in {issue.code for issue in hidden_question.issues}


def test_evaluate_reply_flags_location_confusion_without_false_positive_ne() -> None:
    confused = evaluate_reply("啊，原来你也在成都呀！", user_text="我在成都理工哦")
    declarative_ne = evaluate_reply("嗯就完了。我还以为你至少会说句好呢。", user_text="嗯")

    assert "persona_location_confusion" in {issue.code for issue in confused.issues}
    assert "flattened_question" not in {issue.code for issue in declarative_ne.issues}


def test_evaluate_reply_flags_unsupported_mirroring_and_city_stereotypes() -> None:
    mirrored = evaluate_reply("我明天也有一门，打算早点起来再过一遍。", user_text="我明天考试")
    mirrored_pre = evaluate_reply("我明天也有个pre，咱俩都早点休息。", user_text="我明天考试")
    mirrored_past = evaluate_reply("我去年考的时候也是熬夜翻来覆去地背。", user_text="毛概，好难背")
    mirrored_semester = evaluate_reply("我上学期也被折磨过。", user_text="毛概，好难背")
    mirrored_study_pain = evaluate_reply("我上学期背得也头疼。", user_text="毛概，好难背")
    mirrored_final = evaluate_reply(
        "我之前期末背的时候，会在纸上画时间线。", user_text="毛概，好难背"
    )
    mirrored_rain = evaluate_reply(
        "我上次找不到伞，最后也被淋到了。", user_text="早上起来雨下很大，我伞还找不到"
    )
    local_scene = evaluate_reply(
        "我在图书馆看到好多人抱着毛概书在走廊来回走。", user_text="毛概，好难背"
    )
    stereotype = evaluate_reply("好像成都好多好吃的呀！", user_text="我在成都上学呀，在成都理工哦")
    memory_claim = evaluate_reply("哦对，你之前在群里说过在成都来着。", user_text="我在成都理工哦")
    heard_claim = evaluate_reply("之前听你说在成都来着。", user_text="我想聊聊你来着，你在哪上学哦")
    group_city_claim = evaluate_reply(
        "你呢？之前群里看到你在成都。", user_text="我想聊聊你来着，你在哪上学哦"
    )
    senior_photo_claim = evaluate_reply(
        "之前刷到一个学长发的照片，说那边晚上烟火气特别足。", user_text="我在成都理工哦"
    )
    group_claim = evaluate_reply(
        "我记得之前群里有人发过照片，晚上那边好多小摊。", user_text="我在成都理工哦"
    )
    group_photo_claim = evaluate_reply(
        "我之前看群里有人发过照片，烟火气很足的样子。", user_text="我在成都理工哦"
    )
    familiarity = evaluate_reply(
        "哦，成理啊。之前有听说过。", user_text="我在成都上学呀，在成都理工哦"
    )
    fake_research = evaluate_reply(
        "我之前做城市散步笔记的时候，刚好查过那边。", user_text="我在成都理工哦"
    )

    assert "ungrounded_self_event" in {issue.code for issue in mirrored.issues}
    assert "ungrounded_self_event" in {issue.code for issue in mirrored_pre.issues}
    assert "ungrounded_self_event" in {issue.code for issue in mirrored_past.issues}
    assert "ungrounded_self_event" in {issue.code for issue in mirrored_semester.issues}
    assert "ungrounded_self_event" in {issue.code for issue in mirrored_study_pain.issues}
    assert "ungrounded_self_event" in {issue.code for issue in mirrored_final.issues}
    assert "ungrounded_self_event" in {issue.code for issue in mirrored_rain.issues}
    assert "ungrounded_self_event" in {issue.code for issue in local_scene.issues}
    assert "stereotype_reply" in {issue.code for issue in stereotype.issues}
    assert "unsupported_memory_claim" in {issue.code for issue in memory_claim.issues}
    assert "unsupported_memory_claim" in {issue.code for issue in heard_claim.issues}
    assert "unsupported_memory_claim" in {issue.code for issue in group_city_claim.issues}
    assert "unsupported_memory_claim" in {issue.code for issue in senior_photo_claim.issues}
    assert "unsupported_memory_claim" in {issue.code for issue in group_claim.issues}
    assert "unsupported_memory_claim" in {issue.code for issue in group_photo_claim.issues}
    assert "unsupported_familiarity_claim" in {issue.code for issue in familiarity.issues}
    assert "unsupported_familiarity_claim" in {issue.code for issue in fake_research.issues}


def test_evaluate_reply_flags_question_only_to_emotion() -> None:
    result = evaluate_reply("那是今天发生什么事了吗？", user_text="我今天心里闷闷的")

    assert "emotion_question_only" in {issue.code for issue in result.issues}


def test_evaluate_reply_flags_incomplete_trailing_and_shallow_empathy() -> None:
    trailing = evaluate_reply("找不到伞的话……", user_text="早上雨很大，我伞还找不到")
    shallow = evaluate_reply("我懂那种感觉。", user_text="我今天心里闷闷的")

    assert "incomplete_trailing" in {issue.code for issue in trailing.issues}
    assert "low_engagement" in {issue.code for issue in shallow.issues}


def test_evaluate_reply_flags_question_nagging() -> None:
    result = evaluate_reply(
        "毛概挺难背的，不过我刚问你的问题你好像还没回我诶。", user_text="毛概，好难背"
    )

    assert "question_nag" in {issue.code for issue in result.issues}


def test_evaluate_reply_flags_unsupported_outcome_assumptions() -> None:
    result = evaluate_reply(
        "那你这趟也不算白淋雨，至少没被点到名。", user_text="结果赶到教室发现老师也迟到了"
    )
    rain = evaluate_reply("淋着雨去上课了。", user_text="早上起来就发现雨下很大，然后我伞还找不到")
    rain_stopped = evaluate_reply(
        "是雨停了老师才到，那种事后的滑稽感。", user_text="结果赶到教室发现老师也迟到了"
    )
    waiting = evaluate_reply("不过淋了雨还白等，有点亏。", user_text="结果赶到教室发现老师也迟到了")
    white_run = evaluate_reply(
        "那你这趟也不算白跑，至少老师也跟你一起迟到了。", user_text="结果赶到教室发现老师也迟到了"
    )

    assert "unsupported_outcome_assumption" in {issue.code for issue in result.issues}
    assert "unsupported_outcome_assumption" in {issue.code for issue in rain.issues}
    assert "unsupported_outcome_assumption" in {issue.code for issue in rain_stopped.issues}
    assert "unsupported_outcome_assumption" in {issue.code for issue in waiting.issues}
    assert "unsupported_outcome_assumption" in {issue.code for issue in white_run.issues}


def test_evaluate_reply_allows_short_ack_for_short_ack_user_message() -> None:
    result = evaluate_reply("嗯。", user_text="嗯")

    assert "low_engagement" not in {issue.code for issue in result.issues}


def test_format_results_is_readable() -> None:
    text = format_results([("case", "你好", ReplyEval("在。", []))])

    assert "[case] user=你好" in text
    assert "issues=ok" in text
    assert "reply=在。" in text


def test_scenario_summary_separates_hard_failures_from_style_diagnostics() -> None:
    result = evaluate_reply("我理解。你也在成都呀。", user_text="我在成都理工")
    summary = summarize_results([("case", "我在成都理工", result)])
    assert {issue.code for issue in summary.hard_issues} == {"persona_location_confusion"}
    assert "assistantese" in {issue.code for issue in summary.diagnostic_issues}
    assert summary.exit_code == 1


def test_dialogue_eval_cli_fake_model_smoke_does_not_touch_production_db() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "companion_daemon.dialogue_eval", "--max-cases", "1"],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode in {0, 1}
    assert "ack_should_not_trigger_interview" in completed.stdout
    assert "world mode forbids legacy behaviour write" not in completed.stderr


def test_dialogue_eval_context_cli_smoke() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "companion_daemon.dialogue_eval", "--context"],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 0
    assert "exam_retrieval_does_not_pad_profile" in completed.stdout


def test_chinese_pragmatic_adversarial_slice_has_versionable_labels_and_thresholds() -> None:
    assert len(PRAGMATIC_ADVERSARIAL_CASES) >= 12
    assert {case.target for case in PRAGMATIC_ADVERSARIAL_CASES} >= {
        "companion", "self", "third_party", "general"
    }
    assert all(0 <= case.severity <= 4 for case in PRAGMATIC_ADVERSARIAL_CASES)

    metrics = run_pragmatic_adversarial_eval()

    assert metrics.precision >= 0.80
    assert metrics.recall >= 0.90
    assert metrics.f1 >= 0.85
    assert metrics.calibration_error <= 0.15


def test_pragmatic_metrics_compute_confusion_calibration_target_and_severity() -> None:
    cases = [
        PragmaticAdversarialCase("tp", "甲", True, "companion", 4),
        PragmaticAdversarialCase("fn", "乙", True, "companion", 2),
        PragmaticAdversarialCase("fp", "丙", False, "general", 0),
        PragmaticAdversarialCase("tn", "丁", False, "general", 0),
    ]
    predictions = [
        PragmaticPrediction(0.9, True, "companion", 3, "test"),
        PragmaticPrediction(0.2, False, "general", 0, "test"),
        PragmaticPrediction(0.8, True, "general", 0, "test"),
        PragmaticPrediction(0.1, False, "general", 0, "test"),
    ]

    metrics = pragmatic_classification_metrics(list(zip(cases, predictions, strict=True)))

    assert (metrics.true_positive, metrics.false_positive) == (1, 1)
    assert (metrics.false_negative, metrics.true_negative) == (1, 1)
    assert metrics.precision == 0.5
    assert metrics.recall == 0.5
    assert metrics.f1 == 0.5
    assert metrics.calibration_error == 0.4
    assert metrics.target_accuracy == 0.5
    assert metrics.severity_mae == 1.5


def test_dialogue_eval_pragmatic_cli_smoke() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "companion_daemon.dialogue_eval", "--pragmatic"],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 0
    assert "precision=" in completed.stdout
    assert "calibration_error=" in completed.stdout

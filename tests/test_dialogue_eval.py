from companion_daemon.dialogue_eval import evaluate_reply, format_results, ReplyEval


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


def test_evaluate_reply_flags_stage_and_acquaintance_crutch() -> None:
    result = evaluate_reply("（刚看到）我有个高中同学也这样说过。")
    codes = {issue.code for issue in result.issues}

    assert "stage_direction" in codes
    assert "acquaintance_crutch" in codes


def test_format_results_is_readable() -> None:
    text = format_results([("case", "你好", ReplyEval("在。", []))])

    assert "[case] user=你好" in text
    assert "issues=ok" in text
    assert "reply=在。" in text

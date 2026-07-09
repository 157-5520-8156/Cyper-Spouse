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
    friend_photo = evaluate_reply("我之前看一个成都的朋友发过照片，感觉烟火气特别浓。")
    vague_classmate = evaluate_reply("成都理工啊，我好像有个高中同学在那。")
    codes = {issue.code for issue in result.issues}

    assert "stage_direction" in codes
    assert "acquaintance_crutch" in codes
    assert "acquaintance_crutch" in {issue.code for issue in friend_photo.issues}
    assert "acquaintance_crutch" in {issue.code for issue in vague_classmate.issues}


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
    hidden_question = evaluate_reply("淋着去的还是找到伞了。", user_text="早上起来雨下很大，我伞还找不到")

    assert "ungrounded_local_detail" in {issue.code for issue in local.issues}
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
    stereotype = evaluate_reply("好像成都好多好吃的呀！", user_text="我在成都上学呀，在成都理工哦")
    memory_claim = evaluate_reply("哦对，你之前在群里说过在成都来着。", user_text="我在成都理工哦")
    heard_claim = evaluate_reply("之前听你说在成都来着。", user_text="我想聊聊你来着，你在哪上学哦")
    group_claim = evaluate_reply("我记得之前群里有人发过照片，晚上那边好多小摊。", user_text="我在成都理工哦")
    familiarity = evaluate_reply("哦，成理啊。之前有听说过。", user_text="我在成都上学呀，在成都理工哦")
    fake_research = evaluate_reply("我之前做城市散步笔记的时候，刚好查过那边。", user_text="我在成都理工哦")

    assert "ungrounded_self_event" in {issue.code for issue in mirrored.issues}
    assert "ungrounded_self_event" in {issue.code for issue in mirrored_pre.issues}
    assert "ungrounded_self_event" in {issue.code for issue in mirrored_past.issues}
    assert "stereotype_reply" in {issue.code for issue in stereotype.issues}
    assert "unsupported_memory_claim" in {issue.code for issue in memory_claim.issues}
    assert "unsupported_memory_claim" in {issue.code for issue in heard_claim.issues}
    assert "unsupported_memory_claim" in {issue.code for issue in group_claim.issues}
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
    result = evaluate_reply("毛概挺难背的，不过我刚问你的问题你好像还没回我诶。", user_text="毛概，好难背")

    assert "question_nag" in {issue.code for issue in result.issues}


def test_evaluate_reply_allows_short_ack_for_short_ack_user_message() -> None:
    result = evaluate_reply("嗯。", user_text="嗯")

    assert "low_engagement" not in {issue.code for issue in result.issues}


def test_format_results_is_readable() -> None:
    text = format_results([("case", "你好", ReplyEval("在。", []))])

    assert "[case] user=你好" in text
    assert "issues=ok" in text
    assert "reply=在。" in text

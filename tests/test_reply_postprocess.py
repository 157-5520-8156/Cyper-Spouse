from companion_daemon.reply_postprocess import postprocess_reply_text


def test_postprocess_removes_followup_question_after_recent_question() -> None:
    text = postprocess_reply_text(
        "哦，成都理工啊。你吃过没？",
        recent_lines=["[qq] 她: 我在华东师大。你呢，是在成都上学吗？"],
        user_text="我在成都上学呀，在成都理工哦",
    )

    assert text == "哦，成都理工啊。"


def test_postprocess_removes_flattened_question_after_recent_question() -> None:
    text = postprocess_reply_text(
        "毛概内容也太多了吧，今晚临时抱佛脚还来得及吗。",
        recent_lines=["[qq] 她: 你考什么科目呀？"],
        user_text="毛概，好难背",
    )

    assert text == "毛概内容也太多了吧。"

    text = postprocess_reply_text(
        "毛概真的有点枯燥。你背到哪一章了。",
        recent_lines=["[qq] 她: 你考什么科目呀？"],
        user_text="毛概，好难背",
    )

    assert text == "毛概真的有点枯燥。"


def test_postprocess_allows_answering_user_question() -> None:
    text = postprocess_reply_text(
        "我在华东师大，读汉语言文学。你呢？",
        recent_lines=["[qq] 她: 刚从图书馆出来。"],
        user_text="你在哪上学哦？",
    )

    assert text == "我在华东师大，读汉语言文学。你呢？"

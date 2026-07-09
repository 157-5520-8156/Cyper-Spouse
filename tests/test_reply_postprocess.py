from companion_daemon.reply_postprocess import postprocess_reply_text


def test_postprocess_removes_followup_question_after_recent_question() -> None:
    text = postprocess_reply_text(
        "哦，成都理工啊。你吃过没？",
        recent_lines=["[qq] 她: 我在华东师大。你呢，是在成都上学吗？"],
        user_text="我在成都上学呀，在成都理工哦",
    )

    assert text == "哦，成都理工啊。感觉突然离你具体了一点。"


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

    text = postprocess_reply_text(
        "啊这……那后来找到伞了吗？",
        recent_lines=["[qq] 她: 怎么了？你说说看，我听着。"],
        user_text="早上起来就发现雨下很大，然后我伞还找不到",
    )

    assert text == "啊这。光想想湿鞋就有点烦。"


def test_postprocess_allows_answering_user_question() -> None:
    text = postprocess_reply_text(
        "我在华东师大，读汉语言文学。你呢？",
        recent_lines=["[qq] 她: 刚从图书馆出来。"],
        user_text="你在哪上学哦？",
    )

    assert text == "我在华东师大，读汉语言文学。你呢？"


def test_postprocess_adds_presence_to_thin_topic_echo() -> None:
    text = postprocess_reply_text(
        "哦，成都理工啊。",
        recent_lines=[],
        user_text="我在成都上学呀，在成都理工哦",
    )

    assert text == "哦，成都理工啊。感觉突然离你具体了一点。"


def test_postprocess_rescues_question_only_emotional_reply() -> None:
    text = postprocess_reply_text(
        "那……是今天发生什么事了吗？",
        recent_lines=[],
        user_text="我今天有点累，也不是身体累，就是心里闷闷的",
    )

    assert text == "心里闷的那种最耗人了。我先陪你待一会儿。"


def test_postprocess_repairs_incomplete_trailing_reply() -> None:
    text = postprocess_reply_text(
        "找不到伞的话……",
        recent_lines=[],
        user_text="早上起来就发现雨下很大，然后我伞还找不到",
    )

    assert text == "找不到伞真的会让人一大早心情打折。"


def test_postprocess_adds_presence_to_shallow_empathy() -> None:
    text = postprocess_reply_text(
        "我懂那种感觉。",
        recent_lines=[],
        user_text="我今天有点累，也不是身体累，就是心里闷闷的",
    )

    assert text == "我懂那种感觉。我会想先陪你待一会儿。"


def test_postprocess_adds_presence_to_thin_curiosity() -> None:
    text = postprocess_reply_text(
        "我有点好奇。",
        recent_lines=[],
        user_text="我在那里坐着突然觉得很好笑",
    )

    assert text == "我有点好奇。我刚刚停了一下，脑子里有画面了。"


def test_postprocess_adds_presence_to_bare_relief() -> None:
    text = postprocess_reply_text(
        "那就好。",
        recent_lines=[],
        user_text="我在那里坐着突然觉得很好笑",
    )

    assert text == "那就好。我刚刚停了一下，脑子里有画面了。"


def test_postprocess_adds_presence_to_bare_whats_wrong() -> None:
    text = postprocess_reply_text(
        "怎么了？",
        recent_lines=[],
        user_text="我今天真的有点离谱",
    )

    assert text == "怎么了？我刚刚停了一下，脑子里有画面了。"


def test_postprocess_adds_presence_to_bare_interjection() -> None:
    text = postprocess_reply_text(
        "啊。",
        recent_lines=[],
        user_text="早上起来就发现雨下很大，然后我伞还找不到",
    )

    assert text == "啊。光想想湿鞋就有点烦。"


def test_postprocess_falls_back_when_sanitize_removes_whole_reply() -> None:
    text = postprocess_reply_text(
        "我记得之前群里有人发过照片，晚上那边好多小摊。",
        recent_lines=[],
        user_text="我在成都上学呀，在成都理工哦",
    )

    assert text == "感觉突然离你具体了一点。"

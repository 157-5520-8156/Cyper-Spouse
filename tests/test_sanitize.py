from companion_daemon.sanitize import sanitize_chat_text


def test_sanitize_removes_stage_directions() -> None:
    assert sanitize_chat_text("（手机震了一下）我在。") == "我在。"
    assert sanitize_chat_text("*笑了一下* 你好呀") == "你好呀"
    assert sanitize_chat_text("我叫沈知栀，英文名 Celia Shen。") == "我叫沈知栀，英文名 Celia Shen。"


def test_sanitize_softens_assistantese_and_limits_questions() -> None:
    assert sanitize_chat_text("我理解你的意思，这个问题确实很重要，我有个同学也遇到过。你呢？还好吗？") == "你呢？"
    assert sanitize_chat_text("那你还不早点睡？半夜一点还在外面晃，明天考试能撑住吗？") == "那你还不早点睡？半夜一点还在外面晃。"
    assert sanitize_chat_text("嗯？怎么了？大半夜说这种话。") == "怎么了？大半夜说这种话。"
    assert sanitize_chat_text("淋着了还是找到伞了。") == "淋着了还是找到伞了？"


def test_sanitize_removes_acquaintance_crutch_variants() -> None:
    assert (
        sanitize_chat_text("成都理工啊，那你们学校后门是不是有条街全是串串和冰粉？我有个高中同学在那读土木，她跟我提过。")
        == "成都理工啊，那你们学校后门是不是有条街全是串串和冰粉？"
    )
    assert sanitize_chat_text("毛概确实要背好多啊，不过我室友说画了重点会好背一点。") == "毛概要背好多啊。"
    assert sanitize_chat_text("成都理工啊，我好像有个高中同学在那。") == "成都理工啊。"
    assert sanitize_chat_text("毛概真的好难背啊，我去年考的时候也是熬夜翻来覆去地背。") == "毛概真的好难背啊。"


def test_sanitize_repairs_chengdu_location_confusion() -> None:
    assert sanitize_chat_text("啊，原来你也在成都呀！") == "啊，原来你在成都呀！"


def test_sanitize_removes_explicit_unsupported_memory_claims() -> None:
    assert sanitize_chat_text("怎么了？我记得你之前说自己忙得有点离谱。") == "怎么了？"
    assert sanitize_chat_text("之前听你说在成都来着。") == ""
    assert sanitize_chat_text("我记得之前群里有人发过照片，晚上那边好多小摊。") == ""
    assert sanitize_chat_text("我之前做城市散步笔记的时候，刚好查过那边。") == ""

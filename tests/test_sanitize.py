from companion_daemon.sanitize import sanitize_chat_text


def test_sanitize_removes_stage_directions() -> None:
    assert sanitize_chat_text("（手机震了一下）我在。") == "我在。"
    assert sanitize_chat_text("*笑了一下* 你好呀") == "你好呀"
    assert sanitize_chat_text("我叫沈知栀，英文名 Celia Shen。") == "我叫沈知栀，英文名 Celia Shen。"


def test_sanitize_softens_assistantese_and_limits_questions() -> None:
    assert sanitize_chat_text("我理解你的意思，这个问题确实很重要，我有个同学也遇到过。你呢？还好吗？") == "你呢？还好吗。"
    assert sanitize_chat_text("那你还不早点睡？半夜一点还在外面晃，明天考试能撑住吗？") == "那你还不早点睡？半夜一点还在外面晃。"


def test_sanitize_removes_acquaintance_crutch_variants() -> None:
    assert (
        sanitize_chat_text("成都理工啊，那你们学校后门是不是有条街全是串串和冰粉？我有个高中同学在那读土木，她跟我提过。")
        == "成都理工啊，那你们学校后门是不是有条街全是串串和冰粉？"
    )
    assert sanitize_chat_text("毛概确实要背好多啊，不过我室友说画了重点会好背一点。") == "毛概要背好多啊。"

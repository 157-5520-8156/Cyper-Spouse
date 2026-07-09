from companion_daemon.sanitize import sanitize_chat_text


def test_sanitize_removes_stage_directions() -> None:
    assert sanitize_chat_text("（手机震了一下）我在。") == "我在。"
    assert sanitize_chat_text("*笑了一下* 你好呀") == "你好呀"
    assert sanitize_chat_text("我叫沈知栀，英文名 Celia Shen。") == "我叫沈知栀，英文名 Celia Shen。"


def test_sanitize_softens_assistantese_and_limits_questions() -> None:
    assert sanitize_chat_text("我理解你的意思，这个问题确实很重要，我有个同学也遇到过。你呢？还好吗？") == "你呢？还好吗。"

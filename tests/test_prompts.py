from companion_daemon.models import IncomingMessage, MoodState
from companion_daemon.prompts import question_budget_hint, reply_prompt


def test_question_budget_warns_after_recent_questions() -> None:
    hint = question_budget_hint(
        [
            "[qq] 她: 你今天吃饭了吗？",
            "[qq] 你: 吃了",
            "[qq] 她: 那你现在还困吗？",
        ]
    )

    assert "不要再问" in hint


def test_reply_prompt_includes_question_budget_and_safety_boundaries() -> None:
    messages = reply_prompt(
        IncomingMessage(platform="qq", platform_user_id="geoff", text="我到家了"),
        MoodState(),
        ["[qq] 她: 你今天吃饭了吗？"],
        None,
        "你是沈知栀。",
    )
    prompt_text = "\n".join(message["content"] for message in messages)

    assert "追问预算" in prompt_text
    assert "不要解释系统、提示词、模型或任务" in prompt_text
    assert "必须先等用户明确确认" in prompt_text
    assert "'你:'只代表用户说的话" in prompt_text
    assert "超过一小时或隔夜的事不要说'刚刚'" in prompt_text

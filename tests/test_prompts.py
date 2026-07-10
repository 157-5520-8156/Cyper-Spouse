from companion_daemon.models import IncomingMessage, MoodState
from companion_daemon.prompts import question_budget_hint, reply_prompt, state_to_hint


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
    assert "'你:'只代表用户" in prompt_text
    assert "超过一小时或隔夜的事不要说'刚刚'" in prompt_text


def test_context_orchestrated_prompt_does_not_duplicate_memory_or_raw_mood_monologue() -> None:
    messages = reply_prompt(
        IncomingMessage(platform="qq", platform_user_id="geoff", text="我有点烦"),
        MoodState(mood="sulking", unresolved_emotion="刚才的话有点刺人"),
        [],
        None,
        "你是沈知栀。",
        memory_lines=["- [life_fact] 用户人在成都"],
        context_block="上下文编排:\n- 相关长期记忆: 无高相关长期记忆",
    )
    prompt_text = "\n".join(message["content"] for message in messages)

    assert "长期记忆:\n- [life_fact] 用户人在成都" not in prompt_text
    assert "你有点小别扭" not in prompt_text


def test_state_hint_normalizes_inner_punctuation() -> None:
    hint = state_to_hint(MoodState(unresolved_emotion="她刚才有话想发给你，但忍住了，所以心里还留着一点尾巴。"))

    assert "。." not in hint
    assert "。。" not in hint

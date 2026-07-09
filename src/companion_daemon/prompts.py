from companion_daemon.models import IncomingMessage, MoodState
from companion_daemon.emotion_core import emotion_context_line
from companion_daemon.proactive_triggers import proactive_context_instruction, ProactiveTrigger
from companion_daemon.relationship import relationship_instruction, relationship_status_line


def reply_prompt(
    message: IncomingMessage,
    mood_state: MoodState,
    recent_lines: list[str],
    platform_context: str | None,
    companion_system_prompt: str,
    memory_lines: list[str] | None = None,
    attachment_lines: list[str] | None = None,
) -> list[dict[str, str]]:
    state = f"""当前心情: {mood_state.mood}
{relationship_status_line(mood_state)}
关系阶段: {mood_state.relationship_stage}
关系阶段说明: {relationship_instruction(mood_state.relationship_stage)}
耐心: {mood_state.patience}/100
安全感: {mood_state.security}/100
好奇心: {mood_state.curiosity}/100
主动欲望: {mood_state.initiative}/100
情绪残留强度: {mood_state.emotional_charge}/100
边界等级: {mood_state.boundary_level}/100
上一轮用户意图: {mood_state.last_user_intent or "无"}
上一轮互动事件: {mood_state.last_interaction_event or "无"}
本轮回复风格提示: {mood_state.reply_style_hint or "自然私聊"}
未解决情绪: {mood_state.unresolved_emotion or "无"}
平台上下文: {platform_context or "无"}
{emotion_context_line(mood_state)}
"""
    recent = "\n".join(recent_lines) if recent_lines else "暂无历史。"
    memories = "\n".join(memory_lines or []) if memory_lines else "暂无可靠长期记忆。"
    attachments = "\n".join(attachment_lines or []) if attachment_lines else "本轮没有附件。"
    return [
        {"role": "system", "content": companion_system_prompt},
        {"role": "system", "content": state},
        {"role": "system", "content": f"长期记忆:\n{memories}"},
        {"role": "system", "content": f"本轮附件:\n{attachments}"},
        {"role": "system", "content": f"最近聊天:\n{recent}"},
        {"role": "user", "content": message.text},
    ]


def proactive_prompt(
    mood_state: MoodState,
    recent_lines: list[str],
    companion_system_prompt: str,
    trigger: ProactiveTrigger | None = None,
) -> list[dict[str, str]]:
    recent = "\n".join(recent_lines) if recent_lines else "暂无历史。"
    return [
        {"role": "system", "content": companion_system_prompt},
        {
            "role": "system",
            "content": """你正在后台短暂地想一想要不要主动找用户。
这不是定时问候。很多时候应该选择不发。
考虑：最近是否冷场、你是否想念他、是否怕打扰、当前情绪是否适合开口。
好感度、当前关系、心情会影响主动程度：关系越近越自然，但仍不要机械地定时问候。
如果边界等级高、情绪残留强，主动消息应该更克制，甚至暂时不发。
如果用户刚道歉或刚分享脆弱情绪，可以更温柔；如果用户刚冒犯你，不要立刻假装没事。
优先服从主动触发器；没有强触发时，倾向不发。
Return strict JSON only with keys:
private_thought, should_send, platform, message_type, message, sticker_category, cooldown_minutes.
platform can be qq, wechat, simulator, or null.
message_type can be none, text, sticker, text_sticker.
""",
        },
        {
            "role": "user",
            "content": f"""当前心情: {mood_state.mood}
{relationship_status_line(mood_state)}
关系阶段: {mood_state.relationship_stage}
关系阶段说明: {relationship_instruction(mood_state.relationship_stage)}
耐心: {mood_state.patience}/100
安全感: {mood_state.security}/100
主动欲望: {mood_state.initiative}/100
情绪残留强度: {mood_state.emotional_charge}/100
边界等级: {mood_state.boundary_level}/100
上一轮用户意图: {mood_state.last_user_intent or "无"}
上一轮互动事件: {mood_state.last_interaction_event or "无"}
本轮回复风格提示: {mood_state.reply_style_hint or "自然私聊"}
未解决情绪: {mood_state.unresolved_emotion or "无"}
{emotion_context_line(mood_state)}
主动触发器:
{proactive_context_instruction(trigger)}
最近聊天:
{recent}
""",
        },
    ]

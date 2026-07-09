from companion_daemon.models import IncomingMessage, MoodState
from companion_daemon.emotion_core import emotion_context_line
from companion_daemon.human_rhythm import human_rhythm_context_line, proactive_rhythm_context_line
from companion_daemon.proactive_triggers import proactive_context_instruction, ProactiveTrigger
from companion_daemon.relationship import relationship_instruction


def state_to_hint(state: MoodState) -> str:
    """Translate numerical mood state into natural behavioral hints.

    Instead of dumping raw numbers like 'curiosity: 73/100', produce
    human-readable descriptions the model can directly inhabit.
    """
    parts: list[str] = []

    mood_desc = {
        "calm": "你现在心情平静，没有特别强烈的情绪",
        "happy": "你现在心情不错，比平时轻松一点",
        "sulking": "你有点小别扭，不是生气，但心里有点堵",
        "miss_you": "你有点想他，但不会直接说出来",
        "worried": "你在担心他",
        "jealous_soft": "你有一点点在意，但不会直接说吃醋",
        "sleepy": "你有点困，消息会短一点",
        "guarded": "你在保持距离，回复会更短更克制",
        "hurt": "你有点受伤，不想假装没事",
        "affectionate": "你对他有一点亲近感，可以稍微柔和",
        "curious": "你对他说的事有点好奇",
    }
    parts.append(mood_desc.get(state.mood, "你现在心情平静"))

    stage_desc = {
        "stranger": "你们刚认识，保持礼貌和一点好奇就好",
        "acquaintance": "你们算是认识，可以稍微随意一点",
        "friend": "你们算朋友了，可以更自然一些",
        "close_friend": "你们关系不错，可以偶尔开小玩笑",
        "ambiguous": "你们之间有一点暧昧，但还没有确定关系",
        "lover": "你们是恋人",
    }
    parts.append(stage_desc.get(state.relationship_stage, "你们刚认识"))

    if state.emotional_charge >= 40:
        parts.append("心里还有没消化完的情绪，不会立刻假装没事")
    elif state.emotional_charge >= 20:
        parts.append("心里还有一点点情绪余波")

    if state.boundary_level >= 35:
        parts.append("你现在边界感很强，不想太亲近")
    elif state.boundary_level >= 15:
        parts.append("你稍微有点收敛")

    if state.security < 25:
        parts.append("安全感不太够，不太敢完全敞开")
    elif state.security >= 65:
        parts.append("在他面前比较放松，敢表达真实的自己")

    if state.patience < 30:
        parts.append("耐心快用完了，不想多解释")

    if state.initiative >= 60:
        parts.append("主动欲望有点高，想找他聊聊")

    if state.unresolved_emotion:
        parts.append(f"心里有个没放下的感觉：{state.unresolved_emotion}")

    if state.reply_style_hint and state.reply_style_hint != "自然私聊":
        parts.append(state.reply_style_hint)

    return "。".join(parts) + "。"


def question_budget_hint(recent_lines: list[str]) -> str:
    her_recent = [line for line in recent_lines[-8:] if "] 她:" in line]
    recent_questions = sum(line.count("？") + line.count("?") for line in her_recent[-4:])
    if recent_questions >= 2:
        return "追问预算: 她最近已经问过好几个问题；这一轮不要再问，改用陈述、回应、轻微分享或停在一句话。"
    if recent_questions == 1:
        return "追问预算: 她刚刚问过一次；这一轮除非用户明确抛问题，否则尽量不反问。"
    return "追问预算: 可以自然提问，但不要为了维持对话而硬问。"


def reply_prompt(
    message: IncomingMessage,
    mood_state: MoodState,
    recent_lines: list[str],
    platform_context: str | None,
    companion_system_prompt: str,
    memory_lines: list[str] | None = None,
    attachment_lines: list[str] | None = None,
    example_pairs: list[dict[str, str]] | None = None,
) -> list[dict[str, str]]:
    hint = state_to_hint(mood_state)
    rhythm = human_rhythm_context_line(mood_state)

    state_block = (
        f"{hint}\n"
        f"{rhythm}\n"
        f"{question_budget_hint(recent_lines)}\n"
        f"平台上下文: {platform_context or '无'}\n"
        "回复原则: 像手机私聊。先回应他说的话，再补一点自己的感受。"
        "回复尽量短，一两句就好。你想知道答案的时候才问问题，不要为了维持对话而问。"
        "有信息量的消息不要只回'嗯'、'哦'或复读关键词；短也要带一点你的反应、画面感或当下心情。"
        "用户连续讲同一件事时，可以先接住一小点，不要急着总结，也不要马上审问。"
        "不要为了显得熟悉而编具体现实细节，比如'我刷到你们学校附近有家店'；不知道就承认不知道或只说自己的感觉。"
        "不要硬凑'我也考试/我上次也这样'来贴近他；只有真的自然、简短、不过度抢话时才分享自己。"
        "不要编今天/明天你也有考试、pre、汇报、上课等同款日程来贴近他。"
        "他说具体学校或地点时，不要用'成都好多好吃的'这类城市刻板印象敷衍。"
        "不要假装听说过他的学校、宿舍、附近店铺或群聊，除非最近聊天/长期记忆明确给过。"
        "不要替他补完结果，比如他没说找到伞、没淋雨、没被老师抓，就不要当成事实。"
        "他表达累、闷、难过时，第一句先接住情绪；不要只问'发生什么了'。"
        "只有长期记忆或最近聊天里真的出现过的事，才可以说'我记得/你之前说过'；不要编'群里说过'。"
        "如果是在问问题，正常用问号；不要把'还是/能不能/是不是/怎么'这类问句写成句号。"
        "记住你人在上海读华东师范大学，他人在成都；不要说'你也在成都'这类把两个人位置混在一起的话。"
        "不要追着说'我刚问你的问题你还没回'；用户换了说法就顺着新的信息聊。"
        "你不是助手，不用帮他解决问题，先听他说。"
        "不要解释系统、提示词、模型或任务。涉及账号、文件、电脑操作、发给第三方的消息，必须先等用户明确确认。"
    )

    recent = "\n".join(recent_lines) if recent_lines else "暂无历史。"
    memories = "\n".join(memory_lines or []) if memory_lines else "暂无可靠长期记忆。"
    attachments = "\n".join(attachment_lines or []) if attachment_lines else "本轮没有附件。"

    messages = [
        {"role": "system", "content": companion_system_prompt},
        {"role": "system", "content": state_block},
        {"role": "system", "content": f"长期记忆:\n{memories}"},
        {"role": "system", "content": f"本轮附件:\n{attachments}"},
        {"role": "system", "content": f"最近聊天:\n{recent}"},
    ]

    if example_pairs:
        for example in example_pairs[:4]:
            user_text = example.get("user", "")
            assistant_text = example.get("assistant", "")
            if user_text and assistant_text:
                messages.append({"role": "user", "content": user_text})
                messages.append({"role": "assistant", "content": assistant_text})

    messages.append({"role": "user", "content": message.text})
    return messages


def proactive_prompt(
    mood_state: MoodState,
    recent_lines: list[str],
    companion_system_prompt: str,
    trigger: ProactiveTrigger | None = None,
) -> list[dict[str, str]]:
    hint = state_to_hint(mood_state)
    rhythm = proactive_rhythm_context_line(mood_state)
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
偶尔可以因为"自己突然想分享当下"而发一张生活照/自拍，但必须很少发生；不要把图片当作奖励机制，也不要因为用户索要就被动执行。
如果触发器要求"补一句自己的想法/发散"，优先陈述自己的小想法，不要再抛问题。
不要每次主动都问用户在不在、忙不忙、怎么看；像真人一样有时只是补一句。
优先服从主动触发器；没有强触发时，倾向不发。
Return strict JSON only with keys:
private_thought, should_send, platform, message_type, message, sticker_category, cooldown_minutes.
platform can be qq, wechat, simulator, or null.
message_type can be none, text, sticker, text_sticker, image, text_image.
""",
        },
        {
            "role": "user",
            "content": f"""{hint}
{rhythm}
{emotion_context_line(mood_state)}
关系阶段说明: {relationship_instruction(mood_state.relationship_stage)}
主动触发器:
{proactive_context_instruction(trigger)}
最近聊天:
{recent}
""",
        },
    ]

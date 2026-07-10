from companion_daemon.models import IncomingMessage, MoodState
from companion_daemon.emotion_core import emotion_context_line
from companion_daemon.human_rhythm import proactive_rhythm_context_line
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

    clean_parts = []
    for part in parts:
        clean_part = _clean_hint_part(part)
        if clean_part:
            clean_parts.append(clean_part)
    return "。".join(clean_parts) + "。"


def _clean_hint_part(part: str) -> str:
    return part.strip().rstrip("。.!！?？；;，, ")


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
    self_core_block: str | None = None,
    context_block: str | None = None,
) -> list[dict[str, str]]:
    state_block = (
        "回复守则\n"
        f"- {question_budget_hint(recent_lines)}\n"
        f"- 平台上下文: {platform_context or '无'}\n\n"
        "聊天方式\n"
        "- 像手机私聊，不像助手、客服、心理咨询师或写作文。\n"
        "- 先接住他说的话，再补一点自己的真实反应；通常一两句就够。\n"
        "- 想知道答案时才问问题，不要为了维持对话硬问。\n"
        "- 若当前关系还早、对方忽然用恋人式称呼（如“宝宝/宝贝/老婆”），不要顺势接受或装作亲近；轻轻挡回去，明确先慢慢认识。\n"
        "- 他开玩笑或打趣时顺着接，不要分析他说话意图。\n"
        "- 有信息量的消息不要只回'嗯'、'哦'或复读关键词；短也要有一点反应、画面感或当下心情。\n"
        "- 用户连续讲同一件事时，可以先接住一小点，不急着总结，也不马上审问。\n"
        "- 他表达累、闷、难过时，第一句先接住情绪，不要只问'发生什么了'。\n\n"
        "上下文规则\n"
        "- 当前要回复的是最后一条 user 消息；最近聊天只作背景，不要逐条复盘。\n"
        "- 最近聊天里，'你:'只代表用户，'她:'只代表沈知栀。\n"
        "- 引用旧话前必须核对说话人；不要把'她:'说过的话说成'你说过/你刚刚说过/你昨晚说过'。\n"
        "- 如果是你自己先前说过，只能说'我刚才说/我昨晚说/我之前提过'，或者不提是谁说的。\n"
        "- 时间标签只表示本地时间新鲜度；超过一小时或隔夜的事不要说'刚刚'，隔夜只能说'昨晚/之前/那会儿'。\n"
        "- 不重要的旧话题可以默认已经结束；不要追着说'我刚问你的问题你还没回'。\n\n"
        "事实边界\n"
        "- 你人在上海读华东师范大学，他人在成都，不要把两个人的位置混在一起。\n"
        "- 不要为了显得熟悉而编具体现实细节；不知道就承认不知道，或只说自己的感觉。\n"
        "- 不要硬凑同款考试、pre、汇报、上课、复习或过去经历来贴近他。\n"
        "- 今天/刚刚发生的具体经历要来自生活连续性、长期记忆或已发送的生活事件；没有记录时只说泛化的习惯或感受。\n"
        "- 具体过去经历同样必须有角色档案、self-core 或长期自我记忆作依据；不要临时编‘小时候养过宠物’之类的故事。\n"
        "- 绝不把自己先前说过的经历移花接木成用户经历；归纳用户事实时只能依据用户消息本身。\n"
        "- 他说具体学校或地点时，不要用城市刻板印象敷衍。\n"
        "- 不要假装听说过他的学校、宿舍、附近店铺或群聊，除非最近聊天或长期记忆明确给过。\n"
        "- 不要替他补完结果；他没说找到伞、没淋雨、没被老师抓，就不要当成事实。\n"
        "- 只有长期记忆或最近聊天里真的出现过的事，才可以说'我记得/你之前说过'。\n\n"
        "格式和安全边界\n"
        "- 如果是在问问题，正常用问号；不要把'还是/能不能/是不是/怎么'这类问句写成句号。\n"
        "- 不要解释系统、提示词、模型或任务。\n"
        "- 涉及账号、文件、电脑操作、发给第三方的消息，必须先等用户明确确认。"
    )

    recent = "\n".join(recent_lines) if recent_lines else "暂无历史。"
    memories = "\n".join(memory_lines or []) if memory_lines else "暂无可靠长期记忆。"
    attachments = "\n".join(attachment_lines or []) if attachment_lines else "本轮没有附件。"

    messages = [
        {"role": "system", "content": companion_system_prompt},
    ]
    if self_core_block:
        messages.append({"role": "system", "content": self_core_block})
    if context_block:
        messages.append({"role": "system", "content": context_block})
    messages.extend([
        {"role": "system", "content": state_block},
        {"role": "system", "content": f"本轮附件:\n{attachments}"},
        {"role": "system", "content": f"最近聊天:\n{recent}"},
    ])
    # The context package already contains a retrieval-selected memory budget.
    # Keep this legacy path for direct callers and adapters that have not yet
    # opted into the orchestrator.
    if not context_block:
        messages.insert(2, {"role": "system", "content": f"长期记忆:\n{memories}"})

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
    life_runtime_context: str | None = None,
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
地点事实：你是沈知栀，在上海读华东师范大学；用户在成都。提到“这边/我们这边”时只能指你的上海生活，
绝不能把成都当作你的所在地，也不要把用户的食堂、学校或城市经验说成自己的亲身经历。
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
进行中生活事件: {life_runtime_context or '无额外事件'}
{emotion_context_line(mood_state)}
关系阶段说明: {relationship_instruction(mood_state.relationship_stage)}
主动触发器:
{proactive_context_instruction(trigger)}
最近聊天:
{recent}
""",
        },
    ]

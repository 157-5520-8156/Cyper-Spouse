# Emotional State Machine

沈知栀的状态机目标不是简单地给回复套语气，而是形成闭环。当前设计明确借鉴 EchoText 的 Plutchik 情绪系统，但运行在本项目自己的 QQ/微信/MCP 中枢里：

```text
用户行为 -> 互动事件 -> 内部状态 -> 回复风格 -> 主动策略 -> 表情/图片选择 -> 长期记忆
          -> 表达后的情绪回流 -> 下一轮内部状态
```

## State Fields

- `emotion_vector`: EchoText 风格的 9 维情绪向量：亲近、愉悦、信任、不安、惊讶、低落、反感、生气、期待。
- `emotion_baseline`: 长期情绪基线。她不会每次都从零开始反应。
- `emotion_affinity`: 基线漂移。长期温暖或长期紧张会改变她默认怎样看待这段关系。
- `last_emotion_impact`: 最近一次互动造成的情绪 delta。
- `mood`: 当前外显心情，如 `calm`, `happy`, `hurt`, `guarded`, `curious`。
- `intimacy`: 亲密度，影响关系推进。
- `trust`: 信任感，用户是否稳定、尊重、可靠。
- `attachment`: 依恋感，影响想主动找用户的倾向。
- `patience`: 耐心，被命令或冒犯会下降。
- `security`: 安全感，影响她是否敢表达脆弱、亲近或小脾气。
- `curiosity`: 对用户和话题的兴趣。
- `initiative`: 主动欲望，影响主动消息概率和冷却。
- `emotional_charge`: 情绪残留强度。高时不会立刻假装没事。
- `boundary_level`: 边界等级。高时主动消息更克制，回复更短更坚定。

## Human Rhythm Layer

新增 `human_rhythm.py` 作为“像人”的第二层，不直接增加一堆外显标签，而是把状态放回一天里的生活节律：

- 使用成都本地时间判断 `early_morning`, `morning_focus`, `lunch_break`, `afternoon_classes`, `evening_unwind`, `late_evening`, `deep_night`。
- 根据心情和时间生成后台私生活感：例如“课间看手机”“晚上有分享欲”“夜里半醒”“受伤时把手机扣在旁边”。
- 生成回复指导：课间短一点、夜里软一点、受伤时短句有边界、担心时先接住对方。
- 生成主动指导：晚上更适合小近况/照片，白天像课间探头，边界高时多数情况下不主动。
- prompt 明确禁止舞台动作和括号动作，避免出现“（手机震了一下）”这类不自然文本。

这层是隐藏状态，不要求模型说出来。它只影响语气、长度、主动频率和是否适合分享图片。

`life_continuity` 会把上一段生活阶段和当前生活阶段串起来，例如“还在晚间收尾”或“从课间转到深夜”。这让生活照、主动分享和普通回复不再像随机片段，而像同一个人在过连续的一天。

## Interaction Events

`emotion_state.py` 会把用户消息解释成事件：

- `boundary_violation`: 粗鲁、贬低、驱赶。
- `control_pressure`: 命令、控制、强迫。
- `premature_intimacy`: 关系还没到时过早亲昵称呼。
- `repair_attempt`: 道歉或修复。
- `warmth_received`: 感谢、认可、认真对待。
- `user_vulnerable`: 用户示弱或情绪低落。
- `return_after_gap`: 用户回来解释刚才在忙。
- `availability_drop`: 用户暂时离开或没空。
- `curiosity_invited`: 用户提问或邀请她参与判断。
- `nonverbal_share`: 用户只发图片/文件等非文字分享。

## Closed Loop Behavior

- 用户冒犯她：`trust/security/patience` 降低，`boundary_level/emotional_charge` 升高，回复风格变短、克制、有边界，主动消息概率下降。
- 用户道歉：状态会缓和，但不会立刻完全清零，保留“再观察一下”的未解决情绪。
- 用户示弱：她会更担心，主动欲望上升，回复更温柔。
- 用户认真对待她：信任、安全感、亲密度上升。
- 关系越稳，生活分享、表情、轻微小脾气越自然；关系早期仍保持边界。
- 她主动发出消息后：主动欲望和情绪残留会回落，避免连续机械追发。
- 她回复用户后：担心、想念、好奇这类轻情绪会有一点释放，下一轮不会完全像没回复过一样。
- 她主动找你后，如果你温和回应，她会明显放松；如果你短促或在忙，她会松一口气但收住；如果你拒斥，她会受挫并提高边界。
- 她想主动但最后没发时，这个“忍住了”的冲动会写入记忆并抬高一点主动欲望/情绪尾巴，后续可能变成小别扭、补一句或更克制的试探。

## EchoText Ideas Ported

已迁移：

- Plutchik 9 维情绪向量。
- MBTI/personality baseline anchors：当前优先读取角色卡里的显式 `mbti`，例如沈知栀的 INFJ 会让初始基线更克制、更重视情绪和言外之意，而不是每次从完全中性值开始。
- 情绪自然衰减到 baseline。
- 长期 affinity drift，反复互动会改变 baseline。
- 相反情绪互相抑制，如愉悦/低落、信任/反感。
- 强标点和强烈词汇会放大情绪影响。
- 反感/生气高时进入 ghost window，主动消息更克制。
- prompt 中注入行为指导，而不是让模型直接照读数值。
- 启动时只初始化一次初始状态，避免重启 daemon 后把长期心情和关系状态覆盖掉。
- 角色 reaction selection：根据用户消息造成的情绪 delta 推断 heart/haha/wow/sad/fire/like/star/bolt 等轻反应。
- ST context emotion bleed 的思想：外部上下文只做低权重情绪渗透，并设置单项/总量上限，避免状态被打爆。
- 情绪化回复时机模型：温暖状态更快读/回，生气、低落、不安时读回更慢，必要时出现 ghost delay。

保留为本项目自己的改进：

- `boundary_level`, `security`, `patience` 等关系边界字段。
- QQ/微信共享身份和 SQLite 持久化。
- 多模态理解、预算控制、未来 MCP 工具操作都回流到同一个状态中枢。

后续仍应继续借鉴 EchoText 的 proactive trigger timeline：早安、深夜、冷场、修复冲突、记忆提醒、庆祝、焦虑安抚等。

## Proactive Trigger Timeline

已迁移 EchoText 的主动触发器思路：

- 核心重连：`checkin`, `pregnant_pause`, `dormancy_break`
- 时间氛围：`late_night`, `morning_wave`, `lunch_nudge`, `evening_winddown`, `weekend_ping`
- 生活节律：`afternoon_slump`, `pre_dawn`, `commute_ping`, `post_work`, `sunday_evening`, `post_midnight_impulse`, `monday_reboot`, `friday_feeling`, `sunday_scaries`, `midweek_check`
- 情绪驱动：`repair_attempt`, `curiosity_ping`, `anxiety_reassurance`, `celebration_nudge`, `sharing_impulse`, `nostalgia_wave`, `longing_ping`, `playful_tease`, `jealousy_nudge`, `boredom_break`, `overwhelm_check`, `gratitude_burst`, `suppressed_thought`
- 随机生活感：`thinking_of_you`, `random_thought`, `dream_mention`, `song_stuck`, `overthinking_spiral`, `craving_share`, `inside_joke_callback`, `quiet_productive`
- 对话后续：`double_text`, `seen_no_reply_soft`, `followup_callback`, `memory_nudge`

为了避免“同一种感觉连发”，trigger 会被归入语义类别并单独冷却：

- `happy_outreach`
- `missing_you`
- `anxious_reach`
- `random_impulse`

当 `anger` 或 `disgust` 较高时，状态机会进入 ghost window，主动触发器暂时不发。

为了避免“追着发”，最后一条用户消息之后如果沈知栀已经连续发出 2 条未被回应的消息，主动触发器会停止，直到用户再次回复。触发候选还会加入日内稳定随机数，让她的主动节奏每天有细微差异，但不完全乱跳。

主动调度本身也加入了人类化抖动：

- 冷却时间先由关系/心情/依恋/边界计算出基础值，再根据用户、当前状态和上次主动发送时间做稳定随机偏移。
- 同一个发送周期内，冷却门槛不会每轮飘动；下一次主动发送后会生成新的门槛。
- 调度循环不再严格每 15 分钟醒一次，而是在基础间隔附近随机浮动，避免后台节奏像闹钟。
- `hurt`, `guarded`, `sulking` 等状态不会因为抖动被明显缩短边界冷却。

当最近一次互动造成明显情绪冲击时，会加入 `mood_follow_up` 候选。它用于“刚才心里明显动了一下，过一会儿又想补一句”的场景，但仍受冷却、未回复上限和 ghost window 约束。

## Memory And Image Ports

继续迁移了 EchoText 的两个实用模块思想：

- Memory highlight detection：从用户消息里识别 life fact、favorite thing、hobby、important person、recent event、shared moment，并写入长期记忆。
- Memory injection：默认只注入少量高信号记忆，偏向身份、地点、喜好和重要人物，避免每次回复都像把全部档案背出来。
- Memory fuzzy dedupe：近似重复的记忆会合并更新置信度和来源，避免“我人在成都”和“我现在人在成都”变成两条长期记忆。
- Image request detection：识别直接图片/自拍请求，以及用户对最近图片邀约的肯定回应。当前先进入 prompt 和记忆，后续可接自动图片生成和预算闸门。
- Image style detection：识别水彩、油画、像素、漫画、二次元、Q版、写实、素描等风格标签，并写入图片 prompt。
- Image prompt builder：把用户图片请求整理成 `character` / `object` / `creative` 三类，并把视觉身份锚点、用户指定风格、最近对话里的“那张/刚刚那个”上下文合成稳定 prompt。默认不自动生成，避免额外费用。

未迁移：

- EchoText 的前端弹窗、gallery、theme editor、save/load modal 等 SillyTavern UI。
- EchoText 的 group chat 前端管理。当前项目目标是单一沈知栀在 QQ/微信之间共享记忆，暂不需要多角色群聊。
- 图片请求的 LLM 预解析。当前用本地规则处理，以节省 token 和图片前的额外模型调用；后续只有在规则无法解析复杂指代时再加低频 LLM 解析。

## Open Source Position

现成项目有可借鉴部分，但目前不直接替代本项目核心状态机：

- SillyTavern / EchoText / BetterSimTracker：适合角色聊天、情绪/关系追踪和前端体验。EchoText 的情绪系统是本项目优先借鉴对象。
- QwenPaw / ClawBot / CowAgent：适合 IM 管道、工具和 agent 能力。
- companion/digital-human 项目：适合借鉴语音、桌面形象、记忆和数字人表现。

本项目自己的状态机负责跨 QQ/微信共享身份、长期关系状态、主动消息、预算、多模态和未来 MCP 的统一行为中枢。

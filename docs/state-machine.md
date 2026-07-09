# Emotional State Machine

沈知栀的状态机目标不是简单地给回复套语气，而是形成闭环：

```text
用户行为 -> 互动事件 -> 内部状态 -> 回复风格 -> 主动策略 -> 表情/图片选择 -> 长期记忆
```

## State Fields

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

## Open Source Position

现成项目有可借鉴部分，但目前不直接替代本项目核心状态机：

- SillyTavern / EchoText / BetterSimTracker：适合角色聊天、情绪/关系追踪和前端体验。
- QwenPaw / ClawBot / CowAgent：适合 IM 管道、工具和 agent 能力。
- companion/digital-human 项目：适合借鉴语音、桌面形象、记忆和数字人表现。

本项目自己的状态机负责跨 QQ/微信共享身份、长期关系状态、主动消息、预算、多模态和未来 MCP 的统一行为中枢。

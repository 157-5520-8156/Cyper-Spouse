# 旧行为运行时盘点与世界迁移清单

更新时间：2026-07-11

## 目的与边界

这是一份迁移清单，不是“哪些代码可以立刻删除”的清单。项目正在从以
`MoodState`、`life_runtime`、`social_tasks` 和若干记忆表为中心的旧行为运行时，
迁移到以 `WorldKernel` 追加式账本为唯一生活事实源的运行时。

本文盘点所有会影响以下任一项的旧机制：

- 她正在做什么、是否看手机、是否回复、何时回复；
- 她的情绪、边界、关系、主动性和“忍住不说”；
- 生活事件、日程、记忆、经历或小屋画面；
- 外发消息、延迟任务、主动消息的计划和结算。

不把纯平台 I/O（QQ/OneBot HTTP 调用）、纯模型封装、图片生成实现或静态角色设定
归为“旧行为运行时”。它们是适配器/资源，仍应保留，但不得拥有世界事实。

## 当前结论

`WorldKernel` 现在接管了世界模式的生活事实、通信状态、用户关系/情绪调制、可解释的
延迟决策、对话线程、记忆摘要和小屋数据投影。旧状态机仍保留在代码库中，但只服务
`WORLD_RUNTIME_ENABLED=false` 的历史回退；它们不再是世界模式的行为输入或写入目标。

```text
世界模式开启
  ├─ 世界账本：活动、NPC、目标、经历、事实、外发行动、模型调用、延迟/余波
  ├─ 世界投影：通信状态、关系/情绪调制、决策、对话线程、Self Core、日历和小屋场景
  ├─ 保留为纯分类器：互动事件识别、问题回应分类、文本清洗等
  └─ 保留为传输适配器：QQ/OneBot、消息合并、实际投递（不拥有角色事实）
```

世界模式依然不是“真实人类心理的完整仿真”：它只记录可以解释和复核的决定，不把模型
写出的散文式内心戏当事实。当前开关默认关闭，真实聊天仍应先通过启用门禁。

## 状态标记

| 标记 | 含义 | 处理原则 |
| --- | --- | --- |
| 已迁入 | 世界模式中由账本事件/投影承担 | 继续收敛调用点，禁止旧表回写 |
| 部分迁入 | 有世界分支，但语义或调用链尚不完整 | 补足事件和验收后才删除旧分支 |
| 仅适配 | 不拥有行为事实，只执行 I/O、合并或展示 | 保留，接口只接受世界决策 |
| 已绕过 | 世界模式中不再作为行为输入 | 保留作旧模式回退，不能悄悄影响世界模式 |
| 旁路风险 | 当前仍可能读旧投影/使用旧状态 | 启用真实聊天前必须处理 |

## 现有权威边界

### 世界模式的已接入链路

`CompanionEngine.handle_message()` 在有 `world_kernel/world_id` 时转入
`_handle_world_message()`；该路径明确不以旧状态表作为行为输入。

```text
入站消息
  -> UserMessageObserved
  -> TurnAppraised / IntentCreated
  -> ActionScheduled(model_call)
  -> ModelProposalRecorded（候选，不是事实）
  -> 回复候选引用校验
  -> ActionScheduled(outgoing_message) + outbox + trace
  -> ActionSettled(delivered/failed/unknown/...)
```

世界生活则走：`ClockAdvanced -> ActivityPlanned/Started/Completed ->
LifeOutcomeCommitted -> ExperienceCommitted -> (可选) ExperienceShared`。

### 旧运行时保护

`CompanionStore.enable_world_mode()` 会持久化世界模式标志，并拒绝一部分旧行为写入。
这是防止混写的保护，不等于所有旧读取或所有视觉投影都已迁移。

## 旧机制总表

| 领域 | 旧模块/表 | 旧职责 | 世界模式状态 | 世界侧替代或缺口 |
| --- | --- | --- | --- | --- |
| 私生活 | `life_runtime.py` / `life_runtime`、`life_day_plan_items` | 真实时钟驱动的活动、注意力、手机状态、生活余波 | 已绕过 | `world_agenda`、`needs`、活动/结果/经历、`communication` |
| 日历 | `calendar_ledger.py` / `calendar_*` | 旧计划与生活事件的日历投影 | 已绕过 | `world_agenda` + 全量已提交 `world_experiences` 的只读兼容投影 |
| 生活分享 | `life_event.py`、`social_followups.py` | 从旧生活事件生成/延后分享 | 部分迁入 | 已有 `ExperienceShared` 与 life-share Action；旧 follow-up 不得参与世界模式 |
| 回复决策 | `reply_decision.py`、`reply_timing.py`、`im_timing.py` | 立即/延迟/跳过、读后不回、打字延时、随机抖动 | 已迁入/旧逻辑绕过 | `MessageAttentionDecided`、`TypingStateChanged`、`reply_later`、`DecisionDeferred`；世界模式不执行旧随机时序 |
| 消息轮次 | `turn_taking.py`、NapCat 合并器 | 判断用户是否说完、消息合并、分段发送和打断 | 仅适配 | 可保留合并器；其产生的决定必须记录为世界 Action/外部结果 |
| 情绪 | `mood.py`、`emotion_*.py`、`mood_state` | 情绪向量、心情、关系指标、表情与反应 | 已绕过（互动分类器仍复用） | `NeedChanged`、`EmotionModulated` 投影 |
| 关系/印象 | `relationship.py`、`impression.py`、`repair_curve.py`、`personality_drift.py` | 信任、亲密、边界、修复与长期漂移 | 已绕过 | `UserRegistered`、`RelationshipAppraised`、`RelationshipChanged` |
| 心理活动 | `inner_subtext.py`、`withheld_impulse.py` | 未说出口、想联系又忍住、情绪残留 | 已迁入可解释部分 | `DecisionDeferred/Resolved` + 有期限的复核 Action |
| 自我与记忆 | `self_core.py`、`self_history.py`、`memory*.py`、`tone_inertia.py`、`unanswered_question.py` | 自我摘要、长期记忆、口吻惯性、未答问题 | 已绕过为行为权威 | 事实/经历、确定性 `SelfCoreProjection`、`ConversationThreadOpened/Resolved/Expired` |
| 连续性/上下文 | `life_continuity.py`、`context_orchestrator.py`、`conversation.py` | 将旧生活、情绪、记忆和关系拼入模型上下文 | 已绕过 | `WorldKernel.conversation_context()` 是唯一世界对话读模型 |
| 主动行为 | `proactive_*.py`、`social_tasks`、`proactive_events` | 触发、冷却、等待、延后重试、忍住 | 部分迁入 | 世界主动消息/余波/到期 Action 已有；缺世界化触发、冷却、等待反应 |
| 投递追踪 | `outbox_messages`、`turn_traces` | 消息计划、发送与原因记录 | 部分迁入且必须保留为投递记录 | Action 与 outbox 同事务；旧 trace 降为投递投影，不得独立授权事实 |
| 图片/表情 | `image_requests.py`、`image_agency.py`、`reply_stickers.py` | 根据旧心情/关系决定图片或表情 | 已绕过或仅适配 | 需由世界关系/边界投影授权，图片生成结果走外部 Action |
| 小屋/面板 | `dashboard_ui.py`、`debug_snapshot()` | 读取旧 life runtime 并投影动作 | 已迁入（不改小屋渲染） | `daemon_dashboard_projection()` / `WorldSceneProjection`，前端只读 |

以下各节给出可执行的逐项说明。

## 1. 私生活、日程与生活事件

### `life_runtime.py` — 已绕过，但仍是最大遗留物

旧职责：按照真实墙钟和 `MoodState` 生成“上课/自习/吃饭/睡觉”等活动、手机注意力、
可中断性、用户事件余波，并写入 `life_runtime` 与其关联表。

世界替代：

- 时间：`ClockModeChanged`、`ClockAdvanced`；
- 日程：`ActivityPlanned/Started/Interrupted/Completed/Cancelled/Deferred/Rested`；
- 资源：`NeedChanged`；
- 已发生结果：`LifeOutcomeCommitted`、`ExperienceCommitted`。

即时 IM 语义已收敛到 `communication` 投影：入站消息变为 `unread`，随后只能经
`MessageAttentionDecided(seen/deferred/do_not_disturb)` 与 `TypingStateChanged` 转换；
延迟阅读和回复使用有期限的 Action。世界模式不再调用旧 `life_runtime` 的手机状态。

迁移结论：保留文件仅用于 `WORLD_RUNTIME_ENABLED=false` 的旧实例。世界模式下禁止读写。

### `calendar_ledger.py` — 已绕过

旧职责：从旧计划、旧生活事件和日历表拼接过去/未来日历，并为用户时间问题提供上下文。

替代：从 `world_agenda` 和 `world_experiences` 建立只读 `WorldCalendarProjection`。查询
“今天/昨天/某日”必须只返回已提交经历，不得将计划展示为发生。

### `life_event.py` 与 `social_followups.py` — 已迁入世界分支

`life_event.py` 已有世界分支：它只渲染已提交经历，并通过
`ActionScheduled -> ActionSettled -> ExperienceShared` 投递。此部分保留。

旧分支仍会依赖 `life_runtime`、`life_events`、`social_tasks` 和旧的
`life_share_followup`。世界模式禁止调用这些旧 follow-up；应最终删去世界调用面，保留
为旧模式兼容代码。

## 2. IM 行动、时机与轮次

### `reply_decision.py`、`reply_timing.py`、`im_timing.py` — 世界分支已绕过

旧语义包括：是否马上回、是否读后不回、延迟多少分钟、情绪导致的 ghost window、
读/思考/输入等待和随机抖动。

已迁入：`create_deferred_reply_task()` 在世界模式下创建 `reply_later` Action；新用户消息
会取消同类待执行 Action；完成延迟会写入外部结果。对话余波也能以
`conversation_pulse` Action 计划、取消和结算。

世界模式不运行旧随机抖动或基于 `MoodState` 的 ghost window；因此不存在未记录的随机
行为输入。需要延迟、取消或“先收住”的情况分别以 `reply_later` Action、
`ActionCancelled/Expired` 与 `DecisionDeferred/Resolved` 表示。

迁移目标事件：

```text
MessageAttentionDecided(read_now | defer | do_not_disturb)
MessageSeen
TypingStarted / TypingStopped
ReplyDeferred / ReplyResumed / ReplyWithheld
RandomDrawRecorded
```

它们应更新一个短期 `communication_state` 投影；该投影不是经历事实，但每一次状态变化
必须由事件解释，并为小屋和适配器提供同一读模型。

### `turn_taking.py` 与消息合并器 — 仅适配

`TurnTakingPolicy` 和 QQ/NapCat 的消息合并、分段发送、用户打断逻辑是传输层的短期
协调，不是角色生活事实。它们可保留，但必须满足：

- 合并完成后只提交一次 `UserMessageObserved`；
- 选择等待/取消/发送下一段时，创建或结算一个世界 Action；
- 真实墙钟等待仅是外部执行细节，逻辑世界时间不因此隐式推进；
- 不得以适配器内存计时器作为“她一直在忙/一直在等”的事实来源。

## 3. 情绪、关系与人格漂移

### `mood.py`、`emotion_core.py`、`emotion_state.py`、`emotion_personality.py`、`emotion_reactions.py`

旧职责：以 `MoodState` 的心情、9 维情绪向量、亲密/信任/依恋/边界等字段驱动回复风格、
表情、时机和主动性。

世界模式现状：

- `interpret_interaction()` 仍作为可复用的**文本互动分类器**；
- 世界消息路径以空 `MoodState()` 调用它，并将结果写为 `TurnAppraised`；
- 旧 mood transition 与九维向量不迁入；它们会制造一个难以解释的第二状态机；
- 世界使用较小的 `needs` 与 `EmotionModulated`，由活动、回合判断和关系事件 reducer 更新。

迁移建议：不要把旧 `MoodState` 原样复制到世界。先定义较小且可解释的
`RelationshipState`（主角—用户）与 `EmotionModulation`（短期调制），由
`TurnAppraised`、`ActionSettled`、`ExperienceShared` 等事件 reducer 更新。情绪向量若保留，
必须作为事件化的调制投影，不能由模型或模块直接写表。

### `relationship.py`、`impression.py`、`repair_curve.py`、`personality_drift.py`

旧职责：积累用户可靠性/尊重感、关系阶段、修复曲线与人格倾向。

世界中用户以 `UserRegistered` 实体存在；`RelationshipAppraised/Changed` 持久化尊重、
可靠性与亲近度。主动行为会检查边界、安全感和开放对话线程，不能重新调用旧印象表。

迁移目标：`UserRegistered`、`RelationshipAppraised`、`RelationshipChanged`、
`RepairObserved`、`BoundaryChanged`，并确保只有规则 reducer 改变数值。

## 4. 心理活动与“人味”机制

### `inner_subtext.py` — 已绕过

旧机制把 `MoodState` 推导为“有点吃醋但不说”“想被哄但嘴硬”等 prompt 行和记忆。
在世界模式中没有对应输入，不能继续把这类散文式结果写入记忆后当事实。

可迁入的不是原文，而是可验证的决策：

```text
DecisionDeferred(reason=boundary_high | energy_low | user_busy | goal_urgent)
ExpressionModulated(mode=guarded | warm | brief, reason_event_id=...)
```

文字仅在当轮 prompt 中渲染，账本保留理由、来源事件和有效期。

### `withheld_impulse.py`、`proactive_waiting.py` — 已绕过

旧机制会把“想主动找你但忍住了”“主动后未回复”写入 `MoodState` 和 `social_tasks`。
世界中尚无对应事实。应该迁移为：

```text
ImpulseProposed -> ImpulseWithheld | ActionScheduled
OutgoingUnansweredObserved -> InitiativeAdjusted
```

限制：这些事件必须记录触发依据、过期策略与是否可再次评估；不得储存大量未经验证的
散文私密记忆。

## 5. 记忆、自我核心与对话连续性

| 模块 | 旧职责 | 世界迁移状态 | 最终处置 |
| --- | --- | --- | --- |
| `memory.py` | 从用户消息抽取偏好/事实 | 已绕过为写权威 | 确认后的用户事实进入 `FactConfirmed`；候选必须可审计 |
| `memory_consolidation.py` | 汇总记忆与关系摘要 | 已绕过 | 从 world facts/experiences/relationship 投影重建摘要 |
| `self_core.py`、`self_history.py` | 角色自我概念和叙事历史 | 已绕过 | 仅作为可重建的 `SelfCoreProjection`，绝不单独写事实 |
| `tone_inertia.py` | 口吻连续性 | 已绕过 | 可保留为非事实、短期输出调制，必须附因果与 TTL |
| `unanswered_question.py` | 她问过但用户未答 | 分类器保留 | 已迁为 `ConversationThreadOpened/Resolved/Expired`；只在送达后开启 |
| `context_orchestrator.py` | 拼旧记忆、节律、印象、日历上下文 | 已绕过 | `WorldKernel.conversation_context()` 专门提供世界上下文 |

特别说明：用户经人工/规则确认的事实可以导入新世界；旧“她今天做了什么”、旧心理叙事、
旧摘要不能迁成已发生经历。

### 未遗漏的辅助模块

| 模块 | 分类 | 世界模式下的处置 |
| --- | --- | --- |
| `life_continuity.py` | 旧生活叙事连续性 | 已绕过；以世界活动和通信状态生成摘要，不能保留旧叙事为事实 |
| `relationship_events.py` | 文本关系事件识别 | 可保留为纯分类器；输出必须写入世界 appraisal/relationship 事件 |
| `proactive_feedback.py` | 主动消息后的用户反馈判断 | 分类器可留；旧 mood/social-task 写入要迁为世界关系/行动事件 |
| `reply_postprocess.py`、`reply_segments.py` | 输出清洗与分段 | 仅适配；每段投递仍需由世界 Action 管理 |
| `reply_stickers.py` | 表情包选择 | 仅适配；由世界表达调制和边界策略授权，不能反写情绪事实 |
| `image_requests.py`、`image_agency.py` | 图片请求识别与自主性判定 | 世界聊天路径当前禁用该旧自主链；恢复该能力时必须以 `ImageRequested/Generated/Settled` 外部行动链接入，不能回读旧 mood |
| `conversation.py` | 旧 Prompt/SillyTavern 对话核心 | 世界回复路径当前直接使用受约束 JSON 提示；旧核心仅服务旧模式或样式适配 |

## 6. 主动行为与后台调度

### `proactive_scheduler.py` — 世界分支已迁入

世界分支已经能读取 `due_actions()`、处理 `reply_later` 与 `conversation_pulse`，并在恢复时
扫描中断的生活分享投递。它应保留为**执行器**。

旧分支还会推进 `life_runtime`、读取 `social_tasks`、使用真实墙钟冷却和生成旧主动触发。
这些只能服务旧模式。

### `proactive_triggers.py`、`proactive_feedback.py`、`proactive_waiting.py`、`social_followups.py`

它们目前是典型的旧主动行为状态机：随机/情绪触发、冷却、用户未回应后的心理变化、
任务延后与重试。世界模式的 `_world_proactive_tick()` 不使用它们，而是仅基于已提交
事实/经历与开放外发 Action 作出受限决定。

世界主动性刻意保守：未答的已送达问题、待结算外发行动、待复核决定或较高边界都会阻止
继续追加消息；模型决定不发时写入可到期复核的 `DecisionDeferred`。这取代旧任务表的
等待/忍住逻辑。

## 7. 出站、trace 与投递

### `outbox_messages`、`turn_traces` — 部分迁入的投递投影

世界的 `queue_outgoing_action()` 在同一 SQLite 事务内创建 outbox、turn trace 与
`ActionScheduled`。这两个旧表仍有价值：它们承载平台投递队列和调试 trace；但不得独立
决定“某句话已经发生”或“下一步该做什么”。

现实限制：QQ/OneBot 当前没有可查询的持久送达回执能力。进程在发送后中断时，世界行动
必须保持 `unknown`，不得自动重发或记为 `ExperienceShared`。控制台不能人工输入任意字符串
把它结算为送达。

## 8. 小屋、调试面板与可视化

### `dashboard_ui.py` 与 `debug_snapshot()` — 世界读投影

小屋页面通过 `/debug/{user}/context` 读取 `debug_snapshot()`；旧模式返回旧数据契约，
世界模式返回同形的世界兼容投影。世界控制台直接读取 `WorldKernel.dashboard_overview()`。

世界模式下 `debug_snapshot()` 改由 `daemon_dashboard_projection()` 提供兼容数据；小屋
渲染不变，但地点、动作、手机状态、原因均来自同一个账本投影，不调用旧运行时。

已实现的投影为：

```text
WorldSceneProjection
  input: agenda + communication_state + emotion_modulation + needs + logical clock
  output: location / action / expression / phone_attention / observable_reason
```

小屋、世界控制台和对话提示都读取这个投影；前端只能动画，不可反写。

## 已完成迁移与启用前核验

1. **通信行动。** `communication`、attention、typing、延迟/取消和决策复核均为世界事件；
   QQ 适配器在世界模式不调用旧时序/回复决策状态机。
2. **小屋切换。** 世界模式 `debug_snapshot()` 不调用 `advance_life_runtime()`，仅返回
   `WorldSceneProjection` 的兼容数据。
3. **关系与心理活动。** 用户关系、情绪调制、边界和未发冲动由 reducer 与有 TTL 的 Action
   决定；未答问题仅在外发成功后形成对话线程。
4. **记忆与上下文。** 用户可确认事实进入 `FactConfirmed`；`conversation_context()` 从账本
   重建事实、经历、关系和 Self Core 摘要。
5. **旧表隔离。** 世界模式测试会阻止 `life_runtime`、`social_tasks`、`mood_state`、
   `memories` 等旧行为表写入；它们只保留给旧模式和只读归档。

## 删除/保留准则

| 类型 | 何时可删除 | 应保留什么 |
| --- | --- | --- |
| 旧生活/日历状态机 | 所有世界投影与时间问答测试通过 | 只读历史迁移工具 |
| 旧情绪/心理状态机 | 世界关系与调制测试覆盖相同行为 | 纯文本互动分类器可独立保留 |
| 旧 social task | 所有延迟/余波/主动场景由 Action 覆盖 | 调度器作为 Action 执行器 |
| 旧记忆/self core 写模型 | 所有摘要可从世界投影重建 | 静态角色卡与已确认用户事实导入器 |
| outbox/turn trace | 不应删除 | 平台投递/审计投影，不能成为生活事实源 |
| QQ/OneBot 适配器 | 不应删除 | 仅负责入站、投递、真实回执与 Action 结算 |

## 验收清单

世界模式下必须可自动证明：

- 任一小屋姿态可追溯到世界事件和当前投影；
- 任一“稍后回/读了没回/输入中/忍住”的状态有 Action 或决策事件、原因、TTL 与终态；
- 任一主动消息能追到触发事实、世界 Action、outbox 与投递结算；
- 任一用户可听见的生活经历都能追到已完成活动和提交结果；
- 删除所有世界投影后重建，通信状态、关系调制、日程、经历和小屋场景哈希一致；
- 世界模式 CI 没有行为路径直接写旧 `life_runtime`、`social_tasks`、`mood_state`、
  `calendar_*` 或 `memories`。

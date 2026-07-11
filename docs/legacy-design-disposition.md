# 老设计处置矩阵：迁移状态与后续决策

更新时间：2026-07-11

## 使用规则

### 项目方向

世界模式是项目唯一的未来运行时。旧模式不再接受新特性，也不作为世界模式出错时的
自动回退路径；需要停用世界功能时，应停止相应 Action 或进入人工审计，而不是把两个
状态机的历史混写。

本表将旧设计分为四类：

| 分类 | 含义 | 世界模式中的处理 |
| --- | --- | --- |
| 已集成至世界模式 | 已有事件、reducer、投影与测试闭环 | 只能经 `WorldKernel` 写入 |
| 可沿用 | 不拥有角色/世界事实 | 可作为纯分类器、渲染器或 I/O 适配器 |
| 需改进 | 设计意图合理，但旧实现不满足可追溯性 | 保留需求，不得直接接回旧实现 |
| 未完成 | 尚未有等价世界能力，或世界模式刻意禁用 | 不可在世界模式中悄悄启用 |

“可沿用”不代表可以绕过账本；它只是说明该模块不应拥有状态。

## 已集成至世界模式

| 旧设计 | 世界侧替代 | 当前状态 |
| --- | --- | --- |
| `life_runtime` 的活动/生活进度 | `ClockAdvanced`、`Activity*`、`LifeOutcome*`、`ExperienceCommitted` | 日程、资源、活动结果和经历可回放 |
| 旧日历与生活事件 | `world_agenda`、已提交经历、兼容日历投影 | 计划与已发生经历严格区分 |
| `social_tasks` 的延迟回复/余波 | `ActionScheduled/Cancelled/Expired/Settled` | 延迟回复、对话余波和主动投递有终态 |
| 手机状态、已读和输入中 | `UserMessageObserved`、`MessageAttentionDecided`、`TypingStateChanged` | 通信状态由 reducer 维护 |
| 用户关系与印象积累 | `UserRegistered`、`RelationshipAppraised/Changed` | 尊重、可靠性、亲近度等可追溯 |
| 关系修复、主动消息反馈 | `WorldInteractionRules` + versioned `TurnAppraised` | 反馈产生关系、需求与情绪调制后果 |
| 情绪状态机的可解释部分 | `NeedChanged`、`EmotionModulated` | 使用小型调制状态，不复制旧九维 mood |
| 回复节律、延迟与免打扰 | `WorldBehaviorPolicy` + communication events / reply Action | 不使用旧随机或墙钟 ghost 状态 |
| 主动联系的等待空间 | `WorldBehaviorPolicy`、开放线程、决策 review Action | 关系边界、未结算行动和开放问题会阻止追加主动消息 |
| 口吻惯性与潜台词 | `WorldBehaviorPolicy.expression_guidance()` | 由世界关系、边界、需求和调制即时导出，不写私密记忆 |
| “忍住不说”与等待 | `DecisionDeferred/Resolved` + review Action | 有理由、期限和终态 |
| 未答问题 | `ConversationThreadOpened/Resolved/Expired` | 仅在问题实际送达后开启 |
| 用户事实与长期经历 | `FactConfirmed`、`ExperienceCommitted` | 模型候选不能直接成为事实 |
| Self Core / 连续性摘要 | `conversation_context()` 的 `SelfCoreProjection` | 由世界投影重建，不单独写库 |
| 小屋的数据源 | `daemon_dashboard_projection()` | 小屋渲染不变，输入改为世界投影 |
| 外发消息、outbox、trace | world Action 与投递事务 | 送达、失败、未知均有账本记录 |
| 图片请求与自主发图 | `WorldMediaPolicy` + media generation / delivery Action | 生成与投递分开结算，未共享不能宣称已发图 |
| 表情包自主选择 | `WorldMediaPolicy` + sticker delivery Action | 选择和投递都有事件，不读取旧 mood |
| NPC 社交参与 | `NpcInteractionCommitted` + `LifeSimulation` | 受种子模板、地点、时段、资源和频率约束 |

## 可沿用

| 旧设计 | 可沿用的部分 | 禁止事项 |
| --- | --- | --- |
| `emotion_state.py`、`relationship_events.py` | 文本互动分类 | 不得直接写 `MoodState` 或关系表 |
| `unanswered_question.py` | 回应是回答/跳过/元回应的纯分类 | 不得再从消息历史自行维护未答问题状态 |
| `sanitize.py`、`reply_segments.py`、`reply_postprocess.py` | 文本清洗、分段与格式化 | 分段不能脱离外发 Action 自行算作送达 |
| QQ/NapCat/OneBot 适配器、消息合并器 | 入站解析、消息合并、真实投递、回执获取 | 不得创建旧社交任务或生活事件 |
| `world_console_ui.py`、小屋前端渲染 | 只读展示 | 不得提交事实、模型结果或投递回执 |
| 静态角色卡、`world_seed.yaml` | 初始身份、NPC、地点、边界、模板 | 运行后的生活史必须来自事件，不可直接改种子覆盖 |
| `outbox_messages`、`turn_traces` | 平台投递与审计载体 | 不能作为世界事实源或行为决策源 |

## 已完成改造、后续仅扩展规则

| 原设计 | 已完成的世界化版本 | 后续扩展原则 |
| --- | --- | --- |
| `inner_subtext.py` | `WorldBehaviorPolicy.expression_guidance()` | 只从结构化世界状态导出表达，不写散文私密记忆 |
| NPC 社交机制 | `NpcInteractionCommitted` + 种子活动模板 | 增加模板或 NPC 时必须补齐时段、地点、资源和频率约束 |
| `proactive_triggers.py` / `proactive_waiting.py` | 主动性门禁、开放线程、决策复核 Action | 新触发必须有世界依据、冷却策略和终态 |

## 未完成或世界模式中禁用

| 旧设计 | 当前处理 | 未完成的原因 | 启用前的最低条件 |
| --- | --- | --- | --- |
| 旧九维情绪向量完整模拟 | 不迁入 | 数值来源与因果不透明，容易形成第二真相源 | 若确有需要，先定义少量事件驱动维度与 reducer 语义 |
| 墙钟随机抖动、ghost window | 不迁入 | 不可重放，且会把适配器时间变成心理事实 | 若保留随机性，必须记录 `RandomDrawRecorded`，并只影响可结算 Action |
| 现实地图、外部社媒、现实传感器 | 不接入 | 超出虚构世界种子与事实边界 | 明确外部来源、权限、结果校验与审计策略 |

## 旧表与模块的最终处置

```text
历史旧模式（仅归档、迁移参考）
  → 不再添加功能；不得与世界模式共享新的生活史

世界模式（WORLD_RUNTIME_ENABLED=true）
  → 旧行为写入受保护并失败
  → 分类器和 I/O 可用
  → 所有世界事实和行动只能走 WorldKernel
```

因此，后续开发应先在本表判断模块类别：

1. 已集成：补测试或扩展事件，不另建状态表；
2. 可沿用：保持无状态，接收世界投影；
3. 需改进：先写世界事件模型与验收，再实施；
4. 未完成：保持禁用，不以“临时兼容”为名接回旧状态机。

关联文档：[世界运行时状态、已改动与后续设计](world-runtime-status.md)、
[旧行为运行时盘点与迁移清单](legacy-behavior-runtime-inventory.md)。

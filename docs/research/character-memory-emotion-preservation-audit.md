# 世界模式下的角色、记忆与情绪保真审计

更新时间：2026-07-12
研究范围：Hermes Agent、Character-LLM、SillyTavern、EchoText 与 Girl-Agent 当前世界模式
资料原则：仅采用项目官方文档、官方论文仓库和源代码；第三方项目只提炼设计语义，不复制其状态存储

## 结论

世界模式没有必要恢复旧 `MoodState`、旧记忆表或散文式心理活动，但当前迁移确实压缩掉了
一部分原设计价值：

1. Hermes Agent 的关键不是“有一个记忆数据库”，而是**有限、经过挑选、分清主体、可维护的
   常驻记忆**，并把完整历史留给按需检索。
2. Character-LLM 的关键不是一个名为 Self Core 的字段，而是**稳定人物档案 + 有来源的经历 +
   经历对角色回答方式的长期塑造 + 防角色幻觉训练/校验**。
3. SillyTavern 的关键不是角色卡格式，而是**身份、性格、场景、说话样例和可选择注入的世界知识
   各司其职**，避免所有材料无差别塞入上下文。
4. EchoText 的关键不是九维情绪数值，而是**人物基线、短期状态随互动变化并自然衰减、长期
   affinity 缓慢漂移、主动联系受冷却和未回复预算限制、记忆自然轮换而非每轮强行提起**。

Girl-Agent 当前已经保留“角色卡锚点、事件溯源经历、关系/需求事件、主动消息 Action”这些
骨架，但 `SelfCoreProjection` 太薄、常驻记忆没有显式预算和主体分区、长期情绪倾向被压成单一
短期 mode，尚不能说完整保留了这些参考项目的精髓。

## 一手资料

- Hermes Agent 官方持久记忆文档：
  [Persistent Memory](https://github.com/NousResearch/hermes-agent/blob/6142203bd7af6c5d78f5dd0d58dbe64af5c02345/website/docs/user-guide/features/memory.md)
- Hermes Agent 官方记忆实现：
  [memory_tool.py](https://github.com/NousResearch/hermes-agent/blob/6142203bd7af6c5d78f5dd0d58dbe64af5c02345/tools/memory_tool.py)
- Character-LLM 官方论文代码与数据仓库：
  [trainable-agents](https://github.com/choosewhatulike/trainable-agents/tree/c64d54afa45da483902dc7a5bc60d8462f210fa3)
- SillyTavern 官方角色设计文档：
  [Character Design](https://docs.sillytavern.app/usage/core-concepts/characterdesign/)
- SillyTavern 官方 World Info 文档：
  [World Info](https://docs.sillytavern.app/usage/core-concepts/worldinfo/)
- EchoText 官方仓库：
  [SillyTavern-EchoText](https://github.com/mattjaybe/SillyTavern-EchoText/tree/35a7940f77691807be08c4e2e153118341a90c6e)
- EchoText 情绪实现：
  [emotion-system.js](https://github.com/mattjaybe/SillyTavern-EchoText/blob/35a7940f77691807be08c4e2e153118341a90c6e/lib/emotion-system.js)
- EchoText 记忆与主动消息实现：
  [memory-system.js](https://github.com/mattjaybe/SillyTavern-EchoText/blob/35a7940f77691807be08c4e2e153118341a90c6e/lib/memory-system.js)、
  [proactive-messaging.js](https://github.com/mattjaybe/SillyTavern-EchoText/blob/35a7940f77691807be08c4e2e153118341a90c6e/lib/proactive-messaging.js)

## 1. Hermes Agent：有限的策展记忆，而非全量上下文

官方实现将记忆分成 `MEMORY.md` 和 `USER.md`：前者是 agent 自己需要长期保留的经验、环境事实和
做事规律，后者是用户画像、偏好与沟通方式。两者都有严格字符上限，超限时不会静默丢弃，而是
要求 agent 合并、替换或删除旧条目。会话全文另存 SQLite，并通过 session search 按需读取。

需要保留的语义：

- **主体分离**：关于角色自己的稳定认知、关于用户的长期事实不能混成一池。
- **策展而非堆积**：常驻上下文只保留少量高价值内容；完整经历通过检索获取。
- **可替换与纠正**：新证据应 supersede 旧条目，而不是不断添加互相矛盾的文本。
- **稳定上下文**：Hermes 使用会话开始时的 frozen snapshot，避免每次写记忆都扰动系统前缀。
- **写记忆失败不能阻塞回复**：官方代码对同一轮的反复整理尝试设上限，最终允许跳过副作用并
  继续回答。

当前 Girl-Agent：

| 语义 | 当前状态 | 判断 |
| --- | --- | --- |
| 用户事实与角色经历分开 | `FactConfirmed` 与 `ExperienceCommitted` 分开 | 已保留 |
| 完整历史按需检索 | 经历按时间查询，但回复上下文直接取最近若干条 | 部分保留 |
| 有预算的常驻核心 | `SelfCoreProjection` 无明确预算和策展规则 | 未完成 |
| 纠错/替代关系 | 事件可补偿，但常驻事实投影缺少统一 supersede 语义 | 需加强 |
| 记忆副作用不阻塞回复 | 当前引用格式失败会把整段降级为“我在。” | 未保留 |

## 2. Character-LLM：经历塑造角色，而不是经历列表装饰角色

官方仓库描述的 Experience Reconstruction 流程包含人物 Profile Construction、Scene
Extraction、Experience Completion 和 Protective Scene。训练数据同时覆盖人物经历、特征、情绪和
多轮互动；Protective Scene 专门用于降低 Character Hallucination。

本项目不做角色微调，因此不能逐字复刻 Character-LLM。但应保留等价运行时语义：

- 角色卡是稳定身份锚点，不能随一次情绪或一次活动漂移。
- 只有经过提交的经历可以塑造角色的长期叙事。
- 经历不仅用于回答“做过什么”，也应以受控摘要影响偏好、承诺和关系中的选择。
- 必须有“保护场景”回归集：诱导角色接受错误身份、虚构经历、越过关系阶段时能够拒绝或纠正。
- 人物档案、真实经历、当前场景和表达方式是不同上下文层，不应合并成一段无来源散文。

当前 Girl-Agent 的 `configs/character.yaml` 已较完整地保留身份、背景、性格、价值观、说话方式、
关系规则和边界；`ExperienceCommitted` 也解决了经历来源问题。但 `SelfCoreProjection` 目前只输出
姓名、地点、活动和边界，无法表达长期价值、稳定偏好、关系承诺和已被经历验证的自我变化。

## 3. SillyTavern：角色卡分层与选择性上下文

SillyTavern 的角色设计把 Description、Personality、Scenario、First Message、Example Messages 等
字段分开；World Info 使用关键词、位置、顺序、概率和递归规则选择性注入。其价值不是字段数量，
而是不同材料在生成上下文中承担不同职责。

需要保留的语义：

- 角色身份与说话风格是稳定高优先级上下文。
- 当前场景是短期状态，不能反向改写角色身份。
- 示例消息用于 style anchoring，不能当成真实聊天历史或已发生经历。
- 世界知识按相关性选择注入，避免大而杂的上下文让角色跑偏。
- Author's Note 类信息适合作为短期指导，不应自动成为事实。

当前 `CharacterProfile.system_prompt()` 已明确将示例标为风格锚点，并限制数量；角色事实账本也提示
背景和日常不能据此补写经历。这部分精髓基本保留。主要缺口是世界回复上下文没有把当前活动、
地点、关系和来源 ID 以清楚的分层结构提供给模型，导致角色卡中的“常在图书馆”等气质参考覆盖了
当前世界事实。

## 4. EchoText：短期情绪、长期倾向与线上节奏

EchoText 官方实现为每个角色保存独立 emotion state。当前情绪围绕 personality anchor 和
baseline anchor 变化，安静一段时间后指数衰减回基线；重复互动缓慢改变 affinity shift。主动消息
还受到全局冷却、触发类型冷却、未回复主动消息上限和生成锁约束。记忆支持角色级/全局作用域、
pinned 常驻与 unpinned 轮换，并要求“自然使用，不强行提起”。

需要保留的语义：

- **稳定人格锚点**与**短期情绪状态**分离。
- 短期状态随逻辑时间衰减，长期关系倾向只在重复证据下缓慢变化。
- 情绪改变表达、回复时机和主动性，不授权任何生活事实。
- 主动消息有冷却、未回复预算和并发锁，不因情绪高就无限追发。
- 记忆可以重要性常驻或轮换，但不能机械地每轮引用。

当前世界模式使用 `EmotionModulated`、needs、relationship 和 `WorldBehaviorPolicy`，已经保留“情绪只
调制行为、不直接创造事实”的正确方向；主动消息也有 Action 和开放线程门禁。但只保存单一 mode/
expression/charge，缺少与角色人格相连的长期基线、逻辑时间衰减和重复互动后的缓慢 affinity 漂移。
旧 `emotion_core.py` 中相关纯算法可以作为迁移参考，但不能继续写旧 `MoodState`。

## 5. 世界模式必须新增的保真不变量

这些不变量应通过新世界事件和投影实现，而不是恢复旧写模型：

1. `CharacterCoreProjection` 必须从角色种子、已确认角色事实、已提交经历、长期关系和承诺确定性
   重建；不能由模型直接写一段“自我核心”。
2. 常驻上下文必须有明确预算，并分为 `character_core`、`user_profile`、`current_scene`、
   `retrieved_experiences`、`expression_guidance` 五层。
3. 角色卡中的日常倾向只能影响活动候选或表达风格，不能作为“刚刚做过”的证据。
4. 每条长期用户事实必须有来源、置信度或确认状态、有效时间和 supersede 关系。
5. 短期情绪必须由事件更新并按逻辑时间衰减回人格基线；长期 affinity 只能由重复、已结算互动
   缓慢改变。
6. 情绪、关系、Self Core 和 Character Card 均不得绕过 `FactConfirmed` / `ExperienceCommitted`
   生成经历声明。
7. 记忆检索失败、模型引用格式错误或记忆维护失败不能阻塞正常回复；应做有依据的局部降级。
8. 主动消息必须有冷却、未回复预算、去重和唯一 Action 终态。
9. 示例消息永远不是历史消息；背景设定永远不是当前场景；计划永远不是经历。
10. 所有投影在相同事件流和逻辑时间下必须得到相同结果。

## 6. 对本轮修复计划的影响

立即纳入 P0/P1：

- 回复事实包携带真实 source ID，并将角色核心、当前场景、检索经历分层。
- 引用校验失败做局部降级，不能统一替换成“我在。”。
- 为 Character-LLM 式 protective scenes 增加身份诱导、假经历、错误关系阶段测试。
- 延迟/主动消息沿用 EchoText 的冷却和未回复预算语义，但由世界 Action 结算。
- 逻辑时钟推进同时驱动休息、活动与短期情绪衰减。

后续 P1，但不能用来阻塞当前事实闭环：

- 从世界事件重建更完整、有预算的 `CharacterCoreProjection` 和 `UserProfileProjection`。
- 将旧 EchoText 端口中的 personality anchor、衰减和 affinity 纯算法重写为版本化 world reducer
  规则。
- 增加相关性/重要性驱动的经历选择与轮换，避免每轮重复最近经历。

明确不恢复：

- 旧 `MoodState` 作为写权威；
- LLM 自由改写 Self Core；
- 无来源的散文心理活动；
- 浏览器墙钟驱动、不可回放的随机 ghost window；
- 为了“人味”而把角色背景说成当天已经发生的事。

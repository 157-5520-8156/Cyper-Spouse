# Emotional State Machine

沈知栀的状态机目标不是简单地给回复套语气，而是形成闭环。当前设计明确借鉴 EchoText 的 Plutchik 情绪系统，但运行在本项目自己的 QQ/微信/MCP 中枢里：

```text
用户行为 -> 有来源的互动事实、内部状态与记忆 -> PrivateTurnState
          -> 角色模型选择 now / later / silent、单条/多条及表达方式
          -> Action 授权与投递回执 -> 可持久化后果 -> 下一轮 Context
```

这里的规范边界以
[`ADR 0010`](adr/0010-controlled-high-variance-character-agency.md) 和
[`ADR 0014`](adr/0014-model-owned-private-turn-state-before-expression.md) 为准：
状态机向角色提供事实、处境和后果，不把情绪、时段或消息特征翻译成回复策略。语气、
节奏、是否追问、是否沉默、是否主动以及使用几条消息均由角色模型选择；确定性代码只守
事实来源、隐私与同意、安全、能力与 Action 权限、effect-once、CAS、回执和重放边界。

## State Fields

- `emotion_vector`: EchoText 风格的 9 维情绪投影：亲近、愉悦、信任、不安、惊讶、低落、反感、生气、期待。
- `emotion_baseline`: 长期情绪基线。她不会每次都从零开始反应。
- `emotion_affinity`: 基线漂移，记录长期互动在情绪层留下的缓慢变化。
- `last_emotion_impact`: 最近一次互动造成的情绪 delta。
- `mood`: 旧运行时保留的概括性心情标签，如 `calm`, `happy`, `hurt`, `guarded`, `curious`。
- `intimacy`: 关系中的亲近投影。
- `trust`: 对关系稳定、尊重和可靠性的投影。
- `attachment`: 对连接感的长期投影。
- `patience`: 当前耐心状态。
- `security`: 当前安全感状态。
- `curiosity`: 当前兴趣状态。
- `initiative`: 可供主动考虑调度参考的状态，不是发送许可或动机结论。
- `emotional_charge`: 尚未自然衰减的情绪残留强度。
- `boundary_level`: 当前边界感；它进入 Current Self，但不映射成固定语气或长度。

## Human Rhythm And Current Situation

旧 `human_rhythm.py` 的固定成都时段与学生日程只保留作迁移参考，不能作为生产生活事实
或行为规则。World V2 从已提交的本地时区、季节/校历、年龄与人生阶段、当前住处、假期、
活动和生活事件形成 Current Situation；毕业、实习、旅行或搬家后，不得继续套用“正在
上课/住寝室”的固定脚本。

Current Situation 是隐藏的来源绑定状态。角色可在 `PrivateTurnState` 中注意它，也可以把
注意力放在本轮其他材料上；系统不据此决定语气、长度、主动频率、是否提问或是否分享
图片。

## Interaction Events

生产路径先记录原始 Observation 与平台可观测证据，再由看到同一 Pinned Context 的模型
形成来源绑定的 Appraisal/Affect proposal。旧 `emotion_state.py` 的离散事件名只服务
历史重放与迁移，不能通过关键词命中直接写入新情绪、关系或可见行为，也不能作为
`ExpressionDraft` 的回复模式。

## Closed Loop Behavior

- 入站 Observation、已提交生活事件、关系变化、Affect、开放 Thread、Commitment 和记忆
  以来源绑定的方式进入 Context；它们是角色判断时的环境，不是情绪—表达矩阵。
- `PrivateTurnState` 必须先于 `ExpressionDraft` 形成。角色若选择 Recall，召回完成后须基于
  扩充后的 Context 重新形成二者，不能把旧草稿保留下来再补一个理由。
- 角色可选择单条、多条、Reaction、Sticker、稍后回复或沉默。QQ 执行层只按已接受的
  Beats 建立有依赖关系的 Action，并负责节奏、打断、effect-once 与投递回执。
- 系统不设置问号数量、追问预算、助手腔禁句或舞台词清理器，也不根据“在吗”、问题、
  情绪词、长短句或关系状态替角色选择 `now / later / silent`。
- 旧的固定 afterthought 旁路已退役。同轮补充、多气泡和是否追问都属于
  普通 `ExpressionDraft`；跨时刻表达复用事件驱动的主动考虑，不另造固定追问通道。
- 未答问题、最近语气、关系余波与未解决情绪可以作为来源明确的状态继续存在，但只能
  影响模型可见的当前自我与召回材料，不能强制下一轮采用某种态度或句式。
- 只有成功送达的表达才可进入共同历史并产生送达后果；技术失败进入可观察的恢复与退避，
  不得被本地话术伪装成回复，也不得被记成角色选择沉默。

## Life Projection And Affect Context

生活运行时、关系与 Affect 投影的职责，是让同一个人跨轮保有来源明确的连续状态：

- 已提交活动、短期余波、慢性状态、Appraisal、Affect、关系和人格摘要进入紧凑
  Current Self；投影不能把某个数值编译成回复长度、语气、Reaction、延迟或沉默。
- 生产 Appraisal 由看到完整紧凑上下文的模型提出；系统校验结构、来源和权限，不用
  标点、关键词或本地情绪—行为矩阵补写语义答案。
- baseline、自然衰减、长期 affinity 与相反情绪抑制可以确定性维护状态连续性，但这些
  数值只是下一轮角色判断的材料。
- Reaction、Sticker、文字、多 Beat、`later` 与 `silent` 都在同一个
  `PrivateTurnState + ExpressionDraft` 中由角色选择；没有独立的情绪 Reaction 选择器或
  ghost window。
- 任意新状态机制至少要证明它能以来源绑定的形式到达 Current Self、Recall、生活账本
  或硬边界之一；“直接改变回复行为”不是验收标准。
- 投递失败不得写成已发生的共同事件；成功送达后才可形成相应历史与后果。

旧 `MoodState`、节律模块和 EchoText 事件字段只为迁移、检查或历史重放保留时，
不得重新接回生产表达主链充当行为裁判。

## Context-driven Proactive Consideration

主动系统只产生“考虑机会”，不产生动机或发送结论：

- 已提交的生活、情绪、关系、Thread 或 Commitment 变化可在合并窗口后打开一次情境
  考虑；长期无事件时由 ambient cadence 提供机会。
- 随机性只选择可重放的考虑时间。深夜、忙碌、关系和未回应历史可进入 cadence 权重及
  Pinned Context，但不作为 `act/hold`、固定冷却类别或语义否决器。
- 每次到期都由角色模型基于完整情境自由形成 `impulse_summary`，并选择
  `now / later / silent`。系统不维护问候、想念、安慰、庆祝、好奇等动机枚举。
- 只有预算、平台能力、隐私/同意、安全、法律和 Action 授权等硬边界可以阻止一个已经
  选择的外部效果；模型或供应商故障记录为技术失败并退避，不能伪装成角色沉默。

## Memory And Media

- MemoryCandidate、长期记忆和检索片段都保留来源、说话人、时间与冲突信息；Recall 命中
  进入原语义 lane，角色决定本轮是否注意和使用。
- 图片、Reaction、Sticker 与文字都是模型可选择的能力。系统只声明当前能力、关系/同意
  权限、预算及事实来源，并执行 planning、render、inspection、Action 与回执链。
- 不使用图片请求关键词、风格词表或本地分类器替角色选择是否发图、发什么或如何回应；
  结构或能力选择非法时，把精确失败原因交给同一角色模型受约束重选。

## Open Source Position

现成项目有可借鉴部分，但目前不直接替代本项目核心状态机：

- SillyTavern / EchoText / BetterSimTracker：适合角色聊天、情绪/关系追踪和前端体验。EchoText 的情绪系统是本项目优先借鉴对象。
- QwenPaw / ClawBot / CowAgent：适合 IM 管道、工具和 agent 能力。
- companion/digital-human 项目：适合借鉴语音、桌面形象、记忆和数字人表现。

本项目自己的 World V2 状态与账本负责跨 QQ/微信共享事实、长期关系/Affect、考虑机会、
能力预算、Action 与回执；角色模型仍是语义行为的唯一选择者。

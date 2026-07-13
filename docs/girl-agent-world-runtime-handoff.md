# Girl-Agent 世界模式项目交接总纲

> 文档性质：项目目标、现状、架构、迁移审计、主体性审计与执行待办的统一交接文档
> 更新时间：2026-07-12
> 面向读者：首次接手本项目、没有聊天上下文的开发 Agent
> 当前代码基线：`8778475 feat: iterate human feel with affect expression gates`
> 重要并行约束：像素小屋正在由其他 Agent 修改。除非用户明确重新授权，不要修改 room、小屋资产或其渲染实现。

## 0. 如何使用这份文档

这份文档合并并校正了以下资料：

- [`companion-mechanism-catalog.md`](companion-mechanism-catalog.md)：用户与早期 Agent 讨论过的完整机制目录；
- [`legacy-design-disposition.md`](legacy-design-disposition.md)：旧设计迁移处置矩阵；
- [`legacy-behavior-runtime-inventory.md`](legacy-behavior-runtime-inventory.md)：旧行为运行时盘点；
- [`world-runtime-status.md`](world-runtime-status.md)：世界模式阶段性状态；
- [`world-kernel.md`](world-kernel.md)：事件溯源世界内核不变量；
- [`research/character-memory-emotion-preservation-audit.md`](research/character-memory-emotion-preservation-audit.md)：Hermes Agent、Character-LLM、SillyTavern、EchoText 语义保真审计；
- [`world-conversation-human-likeness-loop.md`](world-conversation-human-likeness-loop.md)：世界对话“人味”多轮修复历史；
- [`personality-agency-hard-gate-audit.md`](personality-agency-hard-gate-audit.md)：人格主体性与过度硬门审计。

早期文档中部分“已完成”结论已经被后续代码审计推翻或需要降级解释。本文件采用以下严格口径：

| 状态 | 定义 |
| --- | --- |
| 已闭环 | 真实世界路径实际调用；有世界事件或受控读投影；能改变用户可见行为；有成功、失败、取消或过期语义；有回放/测试证据 |
| 部分闭环 | 已有世界结构或部分行为，但语义被压缩、仍有旁路、缺少终态/恢复/长期演化，或仅部分适配器生效 |
| 仅有结构 | 存在类、函数、表、文档或测试，但当前世界路径不调用，不能视为已迁移 |
| 未迁移 | 仍只在旧运行时，或世界模式没有等价实现 |
| 明确暂缓 | 项目有意不做，直到事实、主体性或可靠性基底完成 |

交接 Agent 不得仅凭以下证据宣称完成：

- 存在同名文件；
- 旧模式测试通过；
- prompt 中出现一句要求；
- Dashboard 能显示一个字段；
- 某个数值发生变化；
- 一次 30 轮聊天没有报错；
- 模型恰好说出了预期句子。

必须沿着“事件来源—状态变化—内在决定—可观察行动—外部结算—下一轮消费/取消”核验。

---

## 1. 项目的初心

### 1.1 要做的不是助手，而是一个持续存在的虚拟伴侣

项目的核心目标不是制作一个带角色卡的问答助手，也不是把普通聊天模型包装成“女友语气”。目标是让沈知栀作为一个有稳定身份、生活连续性、关系历史、情绪余波、边界、偏好和自主选择的虚拟人物，在一个明确的虚构世界中持续生活，并主要通过线上聊天与用户建立关系。

她应当表现为：

- 有自己的生活，而不是等用户发消息才临场编背景；
- 知道什么已经发生、什么只是计划、什么只是模型候选；
- 会记得有来源的重要事实，也会承认没有记录；
- 会慢慢熟悉用户，而不是开场就假装恋人；
- 会关心、会生气、会受伤、会冷下来、会修复，也可能不想回复；
- 能理解用户的要求，但不把用户每句话都当成控制命令；
- 能顺从、犹豫、折中、反驳、拒绝或暂缓，而不是永远附和；
- 发出的每条消息、图片、表情和主动行为都有行动来源与投递结果；
- 她说自己做过的事情必须能从世界账本追溯。

### 1.2 “像人”不等于随机，也不等于永远温柔

项目看重的真人感包括：

- **连续性**：上一轮的事情会影响下一轮；
- **主体性**：用户请求是输入，不是角色的最高指令；
- **负面情绪**：冒犯、控制、忽略和敷衍可以留下真实余波；
- **矛盾性**：她可以生气但仍关心，可以说没关系但还没完全缓过来；
- **有限理性**：她会受精力、活动、关系、目标和当下感受影响；
- **可解释选择**：不是随机 ghost，也不是无原因地忽冷忽热；
- **行动后果**：想说、忍住、发出、失败、撤回、补回都必须有不同结算。

因此项目不应把“像人”简化为：

- 固定延迟几秒；
- 随机不回复；
- 多加几个情绪词；
- 每轮生成一段内心独白；
- 用户说“别劝”后永久降级为倾听与附和；
- 生气时统一不主动；
- 关系阶段低时统一禁止所有玩笑或主动行为。

### 1.3 世界模式的根本目的

世界模式首先解决“她做的事都有根据”，而不是一次性解决所有文风、共情和模型能力问题。

正确顺序是：

```text
先保证发生依据
→ 再保证状态和决定连续
→ 再保证行动可靠结算
→ 再优化表达、人味和长期人格演化
```

没有世界依据的“人味”会污染记忆；只有账本没有主体性，则会得到一个正确但僵硬的模拟器。两者都不是目标。

### 1.4 必须长期保持的张力

项目必须同时处理以下张力，而不能用一边消灭另一边：

| 张力 | 错误极端 A | 错误极端 B | 目标 |
| --- | --- | --- | --- |
| 事实 vs 创造性 | 允许模型自由编人生 | 只能逐字复读账本 | 事实严格、观点与表达自由、低风险想象不升格为经历 |
| 可回放 vs 人类变化 | 墙钟随机和不可解释抖动 | 每种状态只有一个固定答案 | 规则与已记录随机/外部结果可回放，同状态可有受控候选选择 |
| 用户偏好 vs 角色主体性 | 完全不听用户 | 用户说什么都服从 | 理解请求、形成冲突、选择顺从/折中/反对并承担关系后果 |
| 情绪连续 vs 情绪自由 | 每轮清零 | 向量决定她被允许想什么 | 既有余波约束连续性，新 appraisal 仍可产生并先落账 |
| 安全降级 vs 人格 | 模型失败就乱说 | 模型失败就固定客服模板 | 只对事实和行动保守，保留角色 stance 与表达差异 |

---

## 2. 明确的非目标

当前项目不应宣称或追求：

- 证明角色拥有可验证的人类意识；
- 把程序状态包装成现实中的真人；
- NPC 各自运行独立 LLM 并自由创造世界史；
- 接入现实地图、社交媒体、新闻或传感器后直接成为事实；
- 让 LLM 自由创建 NPC、地点、关系、事故或回忆；
- 维护第二个与 `WorldKernel` 并列的真相数据库；
- 恢复旧 `MoodState`、`life_runtime` 或 `social_tasks` 作为世界写模型；
- 通过不可记录的墙钟随机、ghost window 制造“神秘感”；
- 把示例消息、角色卡背景、计划或图片提示词当成真实历史；
- 为了测试稳定，要求角色在每类对话中说唯一固定句子；
- 在核心闭环未完成前扩展自由 NPC、外部图片搜索、现实操作或大量新场景。

---

## 3. 统一领域语言

以下术语是后续设计与代码审查的标准语言。

| 术语 | 精确定义 | 不能混同 |
| --- | --- | --- |
| 世界（World） | 以沈知栀为中心、由一个事件账本维护的虚构生活纪元 | 角色卡、聊天记录、Dashboard |
| 世界事件（World Event） | 已被规则接受、追加后不可修改的世界变化记录 | 模型输出、日志、候选文本 |
| 投影（Projection） | 从事件流确定性重建的当前读状态 | 写模型、事实来源本身 |
| 逻辑时间（Logical Time） | 世界内部可暂停、加速、跳跃并被事件记录的时间 | 浏览器墙钟、系统当前时间 |
| 角色规范事实 | 人工种子维护的姓名、身份、稳定价值、边界等 | 当天经历、临场自我解释 |
| 用户事实 | 用户直接确认或受控导入、带来源的长期信息 | 模型对用户心理的猜测 |
| 计划（Plan） | 尚未发生的活动或行动安排 | 经历 |
| 候选（Proposal） | LLM、规则或随机机制提出但尚未提交的内容 | 事实、经历 |
| 已提交经历（Committed Experience） | 由已完成活动/已确认共同事件产生、可被后续引用的经历 | 计划、失败投递、背景设定 |
| Action | 对世界外部或线上可观察行为的可结算事务 | 单纯 prompt 意图、未发送文本 |
| 外部结果 | 模型返回、随机抽样、图片生成、网络投递、平台回执等不可重放输入的记录 | reducer 自己重新调用外部系统 |
| 关系阶段 | 从已结算互动投影出的陌生、认识、朋友、亲近等慢变量 | 一句称呼、用户单方面宣布关系 |
| Affect | 由事件形成、会随逻辑时间变化的短期情感状态与余波 | 完整人格、事实授权 |
| 人格核心 | 稳定价值、气质、偏好、边界和经验证的长期自我连续性 | 每轮临场生成的解释 |
| Self Core Projection | 从角色种子、已确认事实、经历、目标和关系确定性生成的读摘要 | 独立写模型、自由文本记忆 |
| 用户请求（User Request） | 用户希望角色采取某种言语或行动方式的输入 | 系统不变量、角色必须服从的命令 |
| appraisal | 对当前事件意义的结构化判断，如被冒犯、用户脆弱、控制压力 | 最终回复文本 |
| drive / motive | 关心、自主、烦躁、好奇、帮助欲、退缩、修复欲等当前行动动力 | 情绪词列表 |
| stance | 角色在冲突中选定的立场：顺从、折中、反对、拒绝、暂缓等 | 文风形容词 |
| display strategy | 是否直说、克制、掩饰、反讽、先听后说等表达选择 | felt affect 本身 |
| 对话线程 | 已送达问题、承诺、关怀或矛盾形成的有期限事项 | 永久记忆提醒 |
| Adapter | QQ、NapCat、OneBot、HTTP、Dashboard 等在 seam 上进行 I/O 的具体实现 | 世界规则拥有者 |
| 硬不变量 | 事实、行动、投递、安全、隐私、consent 方面不可被人格覆盖的规则 | 风格偏好、关系倾向、用户话术 |

---

## 4. 当前目标架构

### 4.1 唯一写模型

项目已经确定：未来只运行世界模式。正确架构是：

```text
QQ 官方 / NapCat / OneBot / HTTP / 调度器 / 面板命令
                         │
                         ▼
                  对话与命令协调 seam
                         │
                         ▼
                    WorldKernel
          （追加事件、乐观并发、幂等收据）
                         │
        ┌────────────────┼────────────────┐
        ▼                ▼                ▼
  世界当前投影       对话上下文投影      Action / Outbox
  人物/日程/需求     事实/经历/Core      投递/回执/终态
        │                │                │
        └────────────────┼────────────────┘
                         ▼
                   只读展示 Adapter
                 世界控制台 / 像素小屋
```

当前代码中，`CompanionEngine` 仍承担了过多协调职责；长期应将对话决策进一步收敛到深模块，而不是继续在 `engine.py` 内叠加 if/regex/gate。

### 4.2 `WorldKernel` 的现有接口

当前核心入口与原计划一致：

```text
submit(command, expected_revision) -> WorldDecision
advance(world_id, target_logical_time, expected_revision) -> WorldDecision
record_external_result(action_id, result, expected_revision) -> WorldDecision
rebuild_projection(world_id, projection_name) -> ProjectionReport
```

不变量：

1. 事件只追加，不修改、不删除；
2. 所有写入带 expected revision，冲突后重新 hydrate 与决策；
3. 计划、模型候选、失败投递不能作为已发生经历；
4. 模型、随机、时钟、网络和媒体结果必须记录，回放不重新调用；
5. Action 必须有 delivered、failed、cancelled、expired 或 unknown 终态；
6. `unknown` 没有可靠回执时不得盲目重发；
7. 投影可从事件零重建并比较哈希。

### 4.3 当前写入链

当前世界回合大致是：

```text
IncomingMessage
→ UserMessageObserved
→ TurnProcessingClaimed
→ TurnAppraised
→ NeedChanged / RelationshipChanged / AffectChanged
→ IntentCreated
→ Model Action + ModelProposalRecorded
→ Reply candidate validation
→ Outgoing Action + outbox + trace
→ Adapter send
→ ActionSettled / unknown / failed
→ delivered conversation projection
```

这条主干解决了“说了但没记、记了但没送、模型候选变事实”等核心问题。

### 4.4 目标中缺失的决定层

当前缺少一个独立的角色权衡深模块。目标接口应是：

```text
CharacterDeliberation.decide(
    situation,
    self_core,
    relationship,
    affect,
    needs,
    user_request,
    open_commitments,
    available_actions,
) -> DeliberationDecision
```

建议输出：

```yaml
appraisal: control_pressure
drives:
  care: 68
  autonomy: 73
  irritation: 35
  desire_to_help: 60
conflicts:
  - respect_no_advice_request_vs_disagree_with_user_choice
stances_considered:
  - comply
  - comply_then_revisit
  - disagree_gently
  - refuse_to_affirm
chosen_stance: disagree_gently
display_strategy: acknowledge_then_state_one_objection
action_candidates:
  - reply_now
  - defer_reply
rule_version: character-deliberation-v1
```

它不保存隐藏思维链或散文心理活动，只保存可解释、可测试、可重放的决定摘要。

---

## 5. 当前已形成的世界能力

### 5.1 世界账本与可靠性：已闭环

已经实现：

- `worlds`、`world_events`、快照、投影检查点和命令收据；
- world revision 唯一约束和乐观并发；
- 幂等命令收据；
- 投影全量重建、在线哈希比较；
- 事件 payload hash 和账本完整性检查；
- 逻辑时钟暂停、倍率与跳跃；
- 外部模型调用记录为 Action 和结果；
- outbox 与外发 Action 的事务协同；
- 适配器崩溃后的 unknown 语义。

判断：这是当前最成熟、最应保留的基底。

### 5.2 虚拟生活：主干已闭环

已经实现：

- `world_seed.yaml` 维护沈知栀、固定 NPC、地点、活动模板、资源和长期目标；
- 活动计划、选择、开始、中断、恢复、延期、完成、取消与休息；
- 地点迁移时间、NPC 可用性、资源消耗、频率限制；
- 活动完成后经过 `LifeOutcomeProposed/Validated/Committed`；
- 只有提交结果才能产生 `ExperienceCommitted`；
- 目标推进、延期、复查、恢复、放弃和补偿；
- 已提交经历与未来计划严格分开；
- 日期/时段查询读取逻辑时间范围内的已提交经历。

仍有限制：

- 周计划和少量跨日命名事件不够完整；
- 活动候选网络仍偏小；
- 用户事件对未来日程的影响很弱；
- 低风险“环境小观察”没有独立来源和生命周期；
- 长期疲惫、边界受损对未来活动偏好的慢性影响尚未系统化。

### 5.3 事实、经历与回复引用：已闭环但事实维护不完整

已经实现：

- 角色规范事实、用户事实、已提交经历、对话记录、计划/候选在语义上分层；
- 回复候选使用 `mentioned_event_ids`、`claims` 和来源文本；
- 未提交候选、取消活动、失败投递、未发生计划不能支撑已发生断言；
- 当前场景、时间问答、NPC 互动和用户回忆有来源约束；
- 无依据行动承诺、累积自传、主体错置和程度升级有门禁；
- reply、repair 和 fallback 会进行世界事实审计。

仍缺：

- 用户事实的统一 `supersede`、失效时间和冲突键；
- 事实纠错/补偿的通用命令与投影语义；
- 常驻记忆预算、重要性和轮换；
- 检索失败的局部降级仍可能被固定模板替代；
- 当前 claim 校验对自然转述仍较脆弱，容易推动逐字复读。

### 5.4 关系：结构已闭环，长期人类语义部分闭环

已经实现：

- 用户作为世界实体；
- interaction count、trust、closeness、respect、reliability 等投影；
- `RelationshipAppraised/Changed/StageEvaluated`；
- stranger → acquaintance → friend → close_friend → ambiguous → lover 阶段；
- 冒犯、控制、温暖、脆弱、用户返回和修复会改变关系；
- 边界过高或信任/尊重过低时可阶段回退；
- 关系阶段会影响表达和媒体策略。

仍缺或过度简化：

- 所有角色实例共享固定升级阈值；
- 具体事件显著性和人格慢热程度没有进入阈值；
- 严肃道歉与敷衍道歉在世界侧统一为 `repair_attempt`；
- 修复后的长期观察期不够明确；
- 关系阶段被部分实现成词语许可黑名单，而非倾向；
- 用户主动分享后的反馈、长期失约和持续尊重没有完整慢变量模型。

### 5.5 情绪与负面情感：近期闭环已存在，长期层未完成

当前世界情绪向量：

```text
hurt / anger / sadness / loneliness / anxiety / resentment / warmth / joy
```

已经实现：

- `AffectChanged/Decayed/Resolved`；
- 情绪来源 appraisal 和来源消息；
- 负面残留、charge、unresolved 和行为倾向；
- 逻辑时间按小时衰减并保留不足一小时余数；
- 冒犯、控制、修复、温暖、脆弱、用户返回和未答线程过期产生不同影响；
- 受伤可以导致短回复、延迟、边界和主动性变化；
- 修复不会一次清零；
- 受伤时仍允许关怀脆弱用户。

仍缺：

- 与人格种子相连的长期情绪基线；
- 重复互动形成的 affinity；
- 正反情感的更细致互动和混合状态；
- 严肃/敷衍修复的不同恢复曲线；
- 长期人格漂移；
- felt affect、action tendency 和 display strategy 的明确分离；
- 回复过程中产生新 appraisal 后先提交再表达的链路。

当前严重设计问题：世界向量被用于限制“她被允许表达什么感受”，导致新产生、矛盾、克制或掩饰的情绪被误杀。详见第 8 节。

### 5.6 通信状态与行动：主干部分闭环

已经实现：

- 入站消息去重；
- `UserMessageObserved`；
- 已读、延迟、勿扰与输入状态事件；
- 活动可中断性、精力、边界、紧急/脆弱标记影响注意力；
- `reply_later` 是有到期和过期时间的世界 Action；
- 新用户消息取消过时的延迟、余韵和生活分享；
- `conversation_pulse` 可以进入 Action 并恢复；
- 已送达问题形成开放线程，回应或逻辑超时后结算；
- 发送成功、失败和 unknown 不混写聊天史。

仍缺或有旁路：

- 连续输入合并主要在 QQ Adapter，不是完整世界决定；
- 注意力策略是确定性单选，固定 15/20 分钟显得机械；
- 世界回复固定 `text_parts=[text]`，旧气泡分段没有迁移；
- 因此世界模式没有真实“分段间用户打断”；
- 情绪性延迟后的补回体验缺少充分端到端验证；
- typing 事件存在，但不同平台真实可见能力不一致；
- turn processing 状态与真实投递终态仍需继续核对命名与时序。

### 5.7 主动性、等待和未说出口：部分闭环

已经实现：

- 主动消息通过世界模型 Action、候选校验、outbox 和投递结算；
- `DecisionDeferred/Resolved` 表示想发但决定收住；
- decision review 有逻辑时间期限；
- 开放线程、未结算外发、关系、边界、安全和负面情绪参与主动判断；
- 生活分享从已提交经历中选择，可被新用户消息取消；
- 用户对近期主动消息的回应可以映射为 warmth、boundary、availability 等 appraisal。

仍缺或过度简化：

- 没有完整的全局冷却、触发类型冷却和未回复外发共享预算；
- 陌生阶段和开放线程会 blanket block 所有普通主动；
- 负面 unresolved 状态会强制取消模型已经选择的主动行为；
- 等待用户回应没有短期—中期—长期渐进曲线；
- 普通主动、余韵、生活分享是否真正共用预算仍不完整；
- `comfort_followup`、`promise_followup`、`contradiction_followup` 没有全部世界化；
- “未说出口”有 review Action，但缺少其对下轮表达/关系的系统影响。

### 5.8 Self Core、角色卡与记忆：骨架存在，保真未完成

已经实现：

- `character.yaml` 保存身份、背景、人格、价值观、关系原则、语言样例和边界；
- `world_seed.yaml` 保存世界初始人物、地点、生活模板和 NPC；
- 示例消息被当成风格锚点，不是历史；
- `conversation_context()` 生成确定性 `SelfCoreProjection`；
- 当前 Self Core 包含稳定特质、价值、偏好、关系原则、语言锚点、当前地点/活动、边界、目标和用户关系；
- Self Core 不能直接创建事实或经历。

仍缺：

- Hermes 式有预算的常驻记忆；
- character core、user profile、current scene、retrieved experiences、expression guidance 的严格预算分层；
- 用户事实与角色经历的策展、替换和轮换；
- 经历对长期偏好、承诺和人格选择的受控塑造；
- frozen context snapshot 或等价稳定上下文策略；
- 记忆维护失败不影响回复的明确不变量；
- Character-LLM 式保护场景回归仍需扩展。

### 5.9 多媒体：角色发图部分闭环，用户输入未迁移

已经实现：

- 图片请求识别；
- 世界关系、边界、情绪与预算参与发图决定；
- 图片生成是外部 Action；
- 媒体投递是独立 Action；
- 没有 `MediaShared` 不能声称已经发图；
- 贴纸选择与投递分别事件化；
- 自拍可以拒绝，自动生成默认关闭；
- visual bible 和提示词锚点存在。

未完成：

- 世界 `UserMessageObserved` 没有记录附件元信息；
- 世界路径在旧附件分析代码前提前返回，视觉/语音分析没有进入世界回合；
- QQ 轻表情 reaction 没有完整世界链；
- 外部图片搜索及版权/来源策略未实现；
- LoRA/FaceID 与稳定视觉身份训练未完成；
- 自拍许可主要是关系阶段硬阈值，缺少角色当天是否想分享的主体性。

### 5.10 工具、成本与安全：部分闭环

已经实现：

- 旧模式有工具请求识别和提案记录；
- 世界媒体生成有预算检查；
- 事实、外部行动承诺、关系越级和部分内容有门禁；
- 模型调用在世界中记录为 Action 和外部结果。

未完成：

- 世界路径在旧工具识别之前返回，MCP/电脑操作提案没有迁移；
- 用户确认、执行、结果、副作用和后续引用没有完整世界 Action 链；
- 世界回复、主动判断、审计模型调用没有统一成本事务；
- 缓存复用和类别预算不是所有路径一致生效；
- 内容安全与人格选择的 seam 仍混在 prompt、provider 和启发式门禁中。

### 5.11 平台、Dashboard 与小屋

已经实现：

- QQ 官方、NapCat、OneBot 通过同一用户映射和引擎；
- 平台差异主要位于消息格式、媒体、回执和主动发送；
- 世界控制台显示事件、快照、行动、投影健康和逻辑时钟；
- 小屋读取 `daemon_dashboard_projection()`；
- 小屋原则上是只读世界状态，不可反写世界事实。

未完成或需确认：

- 微信未接入；
- QQ unknown 回执是否能在所有 Adapter 上可靠对账仍需生产级验证；
- 同一会话只能启用一个 QQ 出站通道，防止双回复；
- Dashboard/小屋的并行视觉改造不属于当前行为迁移任务。

---

## 6. 当前运行时的关键事实

### 6.1 世界模式仍不是代码默认值

`Settings.world_runtime_enabled` 当前默认 `False`。只有 `WORLD_RUNTIME_ENABLED=true` 时，启动器才创建 `WorldKernel`、加载世界种子并启用旧写入保护。

这与“项目以后只基于世界模式运行”的方向不一致。打开 `/world-console` 不能单独证明真实 QQ/NapCat 对话进程正在使用世界模式。

待办：世界路径通过启用审计后，应将世界模式变成唯一启动路径或至少 fail closed；不应继续把旧路径作为默认生产行为。

### 6.2 世界回合会提前绕过大量旧机制

`CompanionEngine.handle_message()` 在存在 `world_kernel + world_id` 时直接进入 `_handle_world_message()` 并返回。因此旧分支中的以下能力不会自动继承：

- `MoodState` 完整更新；
- 旧 repair curve；
- personality drift；
- tone inertia；
- inner subtext；
- 用户附件分析；
- 工具提案；
- reply segmentation；
- 旧 proactive waiting / social task 语义。

旧文件和旧测试存在不代表世界模式已迁移。

### 6.3 当前世界互动分类仍借用空的旧状态

世界回合调用 `interpret_interaction(message, MoodState(), relationship_stage=...)`，即复用旧分类器但传入空状态。它只是文本分类器，不是旧情绪状态参与世界判断。

### 6.4 当前代码与工作区状态

- 当前分支：`feature/snowluma-adapter`；
- 当前 HEAD：`8778475`；
- room、小屋、Dashboard 和相关测试存在其他 Agent 的未提交修改；
- 本交接文档与主体性审计文档是新增未提交文件；
- 不得 reset、checkout 或覆盖不属于本任务的工作区变化。

### 6.5 本轮最近验证

本轮对当前世界情绪、关系阶段、人类表达门和世界引擎相关专项执行：

```text
65 passed, 56 deselected
```

这只证明已实现部分没有在这些测试中回归，不能证明所有机制已经迁移，也不能证明达到人类水平。

---

## 7. 机制迁移总表

### 7.1 已闭环或接近闭环

| 机制 | 状态 | 说明 |
| --- | --- | --- |
| 追加式事件账本 | 已闭环 | 唯一世界事实源 |
| 乐观并发与幂等 | 已闭环 | revision 和 command receipt |
| 投影重建与哈希 | 已闭环 | 支持空投影重建 |
| 逻辑时间 | 已闭环 | 暂停、倍率、跳跃事件化 |
| 活动生命周期 | 已闭环 | planned/started/interrupted/resumed/completed/cancelled/deferred/rested |
| 受限 NPC 生活互动 | 已闭环 | 种子、地点、时段、资源和频率约束 |
| 长期目标生命周期 | 已闭环 | 推进、延期、复查、恢复、放弃、补偿 |
| 活动结果到经历 | 已闭环 | proposal → validation → commit → experience |
| 计划/经历分离 | 已闭环 | 计划不可冒充发生 |
| 回复事实引用 | 已闭环 | 只引用确认事实、对话来源和提交经历 |
| 外发 Action/outbox | 已闭环 | 发送结果有终态和 unknown |
| 生活分享 | 已闭环 | 经历选择、行动、投递与分享结算 |
| 世界 Dashboard | 已闭环 | 世界投影、账本、健康度和逻辑时钟 |
| 小屋只读投影原则 | 已闭环 | 不应反写世界事实 |

### 7.2 部分闭环

| 机制 | 已有部分 | 主要缺口 |
| --- | --- | --- |
| Self Core | 确定性读投影 | 无常驻预算、策展、替换和长期自我变化 |
| 用户事实 | `FactConfirmed` | 缺统一 supersede、冲突与失效语义 |
| 情绪 | 八维 affect、余波、衰减 | 无人格基线/affinity；表达许可证过硬 |
| 关系 | 阶段与多维关系投影 | 固定阈值、修复曲线粗、词法许可过硬 |
| 通信注意力 | seen/deferred/DND + Action | 决策单一、固定时长、Adapter 合并旁路 |
| typing | 世界事件 | 平台可见性不统一 |
| 对话线程 | 问题送达后开启、回应/过期结算 | 承诺、矛盾、关怀线程未统一 |
| 对话余韵 | pulse Action 和恢复 | 候选节奏仍依赖 Adapter，预算未统一 |
| 主动消息 | 世界候选、审计、Action | 冷却/未回复预算/等待曲线不完整，负面 blanket veto |
| 生活分享反馈 | 用户回复可 appraisal | 长期分享意愿和被接住感未完整投影 |
| 周计划 | 日程与长期目标 | 缺正式周主题和跨日少量事件 |
| 用户事件影响未来生活 | needs/关系会变化 | 对未来活动选择和负荷影响弱 |
| 贴纸与生成图 | 选择、生成、投递 Action | 用户附件和 reaction 未世界化 |
| 自拍主体性 | 可拒绝，关系/情绪/预算门 | 主要是硬阈值；无视觉身份训练 |
| 多平台身份 | QQ 系列共享用户映射 | 微信缺失；生产回执需验证 |
| 成本闸门 | 媒体和旧部分模型路径 | 世界所有模型/审计/主动路径未统一 |
| 评测 | 单测、时间旅行、30 轮回放 | 被硬门和固定答案污染，缺主体性测试 |

### 7.3 未迁移或当前世界路径不生效

| 机制 | 当前情况 | 目标处理 |
| --- | --- | --- |
| 长期人格漂移 | 旧模块存在，世界不调用 | 从已结算长期证据投影，幅度小且可回放 |
| 真正口吻惯性 | 旧模块存在，世界不调用 | 基于近期已送达表达形成短期 style state，不做事实 |
| 严肃/敷衍道歉差异 | 世界统一 repair_attempt | 结构化 repair quality 与观察期 |
| 用户事实 supersede | 无通用语义 | 补偿/失效/替换事件和冲突投影 |
| 世界回复分段 | 固定单段 | 分段必须属于同一个 Action 并支持打断/终态 |
| 分段间用户打断 | 无世界分段可打断 | 入站接管并取消未发 segment |
| 用户图片/语音理解 | 旧分支才分析 | 附件元信息与外部分析结果事件化 |
| QQ 轻 reaction | 未形成完整世界 Action | selection → delivery → settlement |
| MCP/电脑操作 | 旧分支提案 | 提案、确认、执行、结果、副作用世界化 |
| 通用等待回应曲线 | 仅问题过期影响 affect | 外发类型、关系、用户可靠性和逻辑时间共同演化 |
| comfort/promise/contradiction followup | 主要是旧 social tasks | 统一为世界对话承诺 Action/Thread |
| 外部图片搜索 | 未实现 | 需来源、版权、安全和不进入记忆策略 |
| 稳定视觉身份 | 只有 visual bible | 授权数据集、LoRA/FaceID 和一致性评测 |
| 微信 | 未接入 | 复用同一用户、世界、Action 和结算 |

### 7.4 明确不应原样迁移

| 旧机制 | 原因 | 可保留语义 |
| --- | --- | --- |
| 旧 `MoodState` 写模型 | 会形成第二真相源 | 文本分类算法、基线/衰减思想 |
| 旧九维向量原表复制 | 来源和因果不够透明 | 人格基线、短期变化、长期 affinity、反向影响 |
| 散文心理活动记忆 | 容易被误当成事实和自传 | 结构化 appraisal、drive、conflict、stance |
| 墙钟随机 ghost | 不可回放 | 逻辑时间 + `RandomDrawRecorded` + 可结算 Action |
| LLM 自由 Self Core | 会污染身份和事实 | 确定性投影 + 有来源的策展摘要 |
| 自由 NPC LLM | 会产生不可控世界史 | 固定 NPC + 规则目标/日程/模板 |

---

## 8. 人格主体性与过度硬门现状

### 8.1 根因

“人味”迭代最初为了解决真实问题：建议替代陪伴、旧事实抢话题、关系越级、读心、假行动、假自传和模型幻觉。随后实现逐步形成：

```text
文本关键词/粗分类
→ 固定 appraisal 与数值变化
→ 单一 expression guidance
→ 模型候选
→ 多层 regex/词表硬拒绝
→ 一次低温 repair
→ 固定台词 fallback
```

事实错误确实减少了，但人格选择也被压缩。系统没有独立的内在权衡层，只能在“相信模型”与“代码彻底禁止”之间选择。

### 8.2 17 类过度硬化

| ID | 级别 | 问题 | 后果 |
| --- | --- | --- | --- |
| HG-01 | P0 | “别劝”被当成必须服从的言语命令 | 角色不能温和反对、拒绝附和或因担心坚持意见 |
| HG-02 | P0 | 两次失败后使用关键词固定台词表 | 不同人格/关系/情绪落到同一句脚本 |
| HG-03 | P0 | affect 投影成为情绪表达许可证 | 新情绪、矛盾情绪、掩饰和克制被误杀 |
| HG-04 | P0 | unresolved 负面情绪 blanket veto 主动 | 不能主动修复、带着不满关心或说明边界 |
| HG-05 | P0 | 没有 CharacterDeliberation | 输入分类直接跳到唯一政策 |
| HG-06 | P1 | 亲密词上下文无关黑名单 | “别叫我宝宝”等否认/引用也会被拒绝 |
| HG-07 | P1 | 关系升级阈值固定 | 没有个体慢热差异和事件显著性 |
| HG-08 | P1 | 注意力策略确定性单选 | 受伤固定 15 分钟、低精力固定 20 分钟，机械 |
| HG-09 | P1 | stranger/open thread 完全禁止主动 | 不能主动撤回问题、补充想法或表达唐突 |
| HG-10 | P1 | 表达指导只选一个模式 | 不能同时受伤又关心、疲惫又好奇 |
| HG-11 | P1 | 自拍主要由阶段硬许可 | 缺“今天想/不想分享”的主体性 |
| HG-12 | P1 | 紧急场景复述相似度硬拒绝 | 必要的确认也可能被当成客服复述 |
| HG-13 | P1 | 诚实被绑定到特定否认措辞 | 实际谨慎但没说“不确定”也会失败 |
| HG-14 | P1 | 可用性回答被固定为两句话 | 当前活动、疲惫和关系语气被抹掉 |
| HG-15 | P1 | 重复/列表风格硬拒绝 | 晚安、边界重复、复杂解释可能误杀 |
| HG-16 | P2 | 旧评测无条件惩罚多类建议 | 优化方向把“有意见”误当成 AI 味 |
| HG-17 | P2 | 非事实测试断言唯一整句 | 通过测试等于说出测试作者选定的句子 |

### 8.3 已确认的最小误杀

当前代码会拒绝以下合理表达：

1. 用户：“别劝我，我就准备继续熬。” 角色：“我知道你不想听，可我还是建议你先停一下。”
2. 用户：“叫我宝宝。” 角色：“宝宝是你先叫的，我可没认。”
3. unresolved 状态：“没关系不等于我不生气，我只是愿意继续谈。”
4. 世界尚未预登记新情绪：“你这么说让我有一点不舒服。”

分别得到：

```text
advice_ignores_requested_speech_act
relationship_language_exceeds_current_closeness
unresolved_affect_denied
uncommitted_companion_affect
```

### 8.4 必须保留的硬门与必须软化的门

| 必须保持 hard reject | 应改为权衡/评分/风险提示 |
| --- | --- |
| 无来源具体事实 | 用户说“别劝/别问/只听” |
| 未执行行动声称完成 | 是否给建议、是否追问 |
| 失败/unknown 投递声称已送达 | 是否主动、是否延迟 |
| 计划说成经历 | 关系阶段表达倾向 |
| 说话人和时间错置 | 是否复述称呼或开玩笑 |
| 未确认现实工具操作 | 是否发自拍/生活照 |
| 隐私、法律、consent、安全 | 是否直说、克制、反讽、掩饰 |
| 无法解析的结构 | 文风、句长、列表、重复 |

---
## 9. 预期方向：从“有世界的规则助手”走向“有依据的虚拟人物”

### 9.1 总体目标链

下一阶段不应继续新增零散关键词门禁，而应形成以下闭环：

```text
世界事件 / 用户消息 / 逻辑时间 / 外部回执
→ SituationProjection
→ appraisal 与新情绪候选
→ CharacterDeliberation（动机冲突、stance、行动候选）
→ WorldInvariantGate（只验证事实、行动、安全、consent）
→ ExpressionPlan
→ 模型受限生成
→ ActionScheduled
→ Adapter 执行与外部结果
→ ActionSettled
→ 关系、情绪、承诺和记忆投影消费结果
```

### 9.2 设计原则

1. **世界账本约束她经历过什么，不垄断她能怎么看待事情。**
2. **用户请求影响角色，但不拥有角色。**
3. **情绪必须有来源，但来源可以在当前回复决策中形成并先落账。**
4. **内心活动记录决定结构，不记录隐藏思维链。**
5. **关系阶段是先验与代价，不是词语许可证。**
6. **负面情绪可以抑制主动，也可以产生修复、边界或关怀主动。**
7. **安全 fallback 只保守事实和行动，不替角色统一表态。**
8. **同一世界事件流重放得到同一决定；需要变化时记录随机抽样或外部结果。**
9. **Adapter 只做 I/O，不拥有心理、生活或关系状态。**
10. **删除任何读投影后都能重建；关闭 Self Core 不应破坏事实和行动正确性。**

### 9.3 建议的深模块

#### A. `WorldKernel`

职责保持不变：事件、并发、幂等、投影、快照和外部结算。不要把人格策略继续塞入其公开接口。

#### B. `LifeSimulation`

继续隐藏活动选择、NPC 匹配、地点、资源、目标和经历提交。外部只推进逻辑时间，不自行判断生活结果。

#### C. `CharacterDeliberation`

新增。唯一负责“她这次真正想怎么做”。输入结构化状态，输出 appraisal、drive、conflict、stance、display strategy 和 action candidates。

它必须支持：

- comply；
- comply_then_revisit；
- disagree_gently；
- refuse_to_affirm；
- set_boundary；
- seek_repair；
- care_despite_hurt；
- defer；
- remain_silent；
- initiate。

#### D. `WorldInvariantGate`

从当前 `validate_reply_candidate`、grounded audit 和 human gate 中抽取真正不变量：

- 来源；
- 主体；
- 时间；
- 计划/经历；
- Action；
- 投递；
- 隐私、安全和 consent。

不负责文风、建议、关系词、情绪许可或是否“够共情”。

#### E. `ExpressionPlanner`

将 stance、felt affect、display strategy、关系、精力和平台约束变成表达计划：长度范围、是否提问、是否分段、语气张力、是否直接说情绪、是否引用事实。它输出约束而不是固定句子。

#### F. `ContextAssembler`

建议固定为五层预算：

```text
character_core
user_profile
current_scene
retrieved_experiences
expression_guidance
```

所有条目保留来源、主体、时间和用途；示例消息只用于 style，不进入 history。

#### G. `ActionCoordinator`

统一 reply、reply_later、conversation_pulse、life_share、comfort_followup、promise_followup、media、reaction 和 tool execution 的生命周期、预算与投递结算。

### 9.4 结构化心理活动的目标事件

建议新增或明确以下事件：

```text
UserRequestAppraised
MotiveConflictEvaluated
StanceSelected
ExpressionPlanned
AffectProposed
AffectCommitted
ActionCandidateRejected
```

事件 payload 只保留：

- 来源事件；
- 规则版本；
- 标准化 motive 分数或等级；
- 冲突 reason codes；
- 候选 stance；
- 最终 stance；
- 被拒行动及原因；
- 是否需要重新评估；
- 与后续 Action 的 causation ID。

禁止保存：

- 模型隐藏思维链；
- 无来源自传；
- 可被下轮直接当事实引用的散文心理独白。

### 9.5 情绪目标模型

情绪应分成四层：

| 层 | 含义 | 时间尺度 |
| --- | --- | --- |
| personality baseline | 人格种子决定的稳定倾向 | 月级/纪元级 |
| affinity | 重复、已结算关系经验形成的长期偏移 | 周/月级 |
| felt affect | 当前事件产生的感受和余波 | 分钟/小时/天 |
| display strategy | 这次是否以及如何表达 | 单次行动 |

正确链路：

```text
事件
→ appraisal proposal
→ 规则验证与 AffectCommitted
→ felt affect / action tendency
→ deliberation
→ display strategy
→ reply/action
```

这样可以允许：

- 生气但仍关心；
- 受伤后主动修复；
- 说“没关系”但余波仍在；
- 表面克制、内部不满；
- 原分类器没识别到，但本轮通过 appraisal 正式产生的新感受。

---

## 10. 详细待做列表

以下列表按依赖顺序排列。P0 未完成前，不应继续扩展自由 NPC、现实数据源或大量新情绪维度。

### Phase 0：建立可信基线与工作区保护

#### H0-01 固化当前交接基线

任务：

- 记录当前 HEAD、分支、world seed hash、目标数据库和运行配置；
- 记录哪些工作区修改属于小屋 Agent；
- 运行世界专项和非 room 全量测试；
- 保存失败列表，不把历史文档中的旧通过数当成当前结果。

验收：新 Agent 能在不覆盖并行修改的情况下复现相同测试结果。

#### H0-02 建立机制闭环清单的机器可读版本

任务：为每个机制记录 `source / events / reducer / projection / decision_consumer / action / terminal_states / tests / adapters`。

验收：CI 能发现“存在事件但没有真实消费者”或“世界模式调用旧写入口”。

#### H0-03 保留主体性误杀红灯

将以下四个最小用例加入测试，但初始应作为待修复红灯或 xfail，不得修改预期迎合旧实现：

- 别劝但温和反对；
- 否认式使用亲密称呼；
- 没关系但仍生气；
- 当前产生新的不舒服。

验收：修复后四例通过，同时事实/Action hard gate 仍拒绝假历史。

### Phase 1：世界模式成为真正唯一运行时（P0）

#### H1-01 消除默认旧路径

任务：

- 将生产启动默认切到世界模式，或没有明确世界配置时 fail closed；
- 旧运行时只允许显式 archive/debug 命令读取；
- 禁止世界关闭时悄悄恢复旧行为写入。

验收：正常 daemon、QQ 官方、NapCat、OneBot 和主动调度启动后均存在同一 world ID；不存在新旧历史混写。

#### H1-02 自动旁路扫描

扫描世界路径是否调用：

- `save_mood_state`；
- `advance_life_runtime`；
- `create_social_task`；
- 旧 calendar/life event 写入口；
- 旧 memory 写入口作为行为权威。

验收：CI 失败信息能定位具体调用；纯分类器和 Adapter 白名单显式维护。

#### H1-03 修正世界 turn 与投递终态语义

任务：核对 `TurnProcessingSettled(status=delivered)` 是否在只创建 reply Action 时过早使用；将“回合生成完成”和“消息真实送达”分成不同状态。

验收：Action 未送达时，turn 不得暗示 delivered；Dashboard、trace 与聊天史一致。

#### H1-04 平台 unknown 回执对账

任务：

- 保存平台消息 ID 或可查凭据；
- 重启时只查询 unknown；
- 有证据才结算 delivered/failed；
- 无证据保持 unknown 并支持人工审计。

验收：发送后崩溃、回执迟到、重复回调都不重复发、不形成假历史。

### Phase 2：建立角色内在权衡（P0）

#### H2-01 定义 `DeliberationDecision`

字段至少包括：

- appraisal；
- user request；
- drives；
- conflicts；
- candidate stances；
- chosen stance；
- display strategy；
- action candidates；
- rejected candidates；
- rule version；
- causation IDs。

验收：同一事件流重放得到相同决定；结构不包含隐藏思维链。

#### H2-02 用户请求从命令降为权重

将“别劝、别问、只听、叫我某称呼、现在发自拍”等解析为有 scope、strength 和 expiry 的请求。

验收：“别劝”至少支持顺从、部分顺从、温和反对、拒绝附和和高风险覆盖五种 stance。

#### H2-03 从 human gate 抽离人格选择

从 runtime hard reject 移除或降级为 risk：

- 建议词；
- 问句；
- 亲密词的上下文无关匹配；
- 列表/连接词；
- 普通重复；
- 特定诚实措辞；
- 非事实风格要求。

验收：这些规则只能影响候选评分或触发 deliberation，不可单独把合理回复替换成固定 fallback。

#### H2-04 安全 fallback 去脚本化

任务：

- `build_safe_failure_candidate()` 不再承担人格判断；
- 确定性 fallback 只提供可信事实骨架、Action 状态和 selected stance；
- 表达由受限生成或少量人格模板族完成；
- 不同关系/情绪/stance 不得统一落到同一句。

验收：删除大部分关键词固定整句；模型连续失败仍不产生幻觉、假行动或客服统一口径。

#### H2-05 将可用性回答恢复为人格化表达

事实层输出 `available / limited / unavailable` 与原因；ExpressionPlanner 生成文本。

验收：不能把 active activity 说错，同时允许疲惫、犹豫、关系和情绪差异。

### Phase 3：重构情绪、修复与关系选择（P0/P1）

#### H3-01 新 appraisal 先提交再表达

任务：允许规则或模型提出当前感受；验证来源、强度和允许维度后追加 `AffectCommitted`，回复再引用。

验收：“你这么说让我不舒服”不需要旧向量预先猜中，但能追到当前消息与规则版本。

#### H3-02 分离 felt affect 与 display strategy

验收以下场景：

- 仍生气但说没关系；
- 受伤但先照顾用户；
- 内心温暖但表面克制；
- 修复开放但未完全原谅；
- 生气选择主动说明边界。

#### H3-03 引入人格 baseline

从角色种子确定 baseline；时间衰减回 baseline 而不是全零。

验收：相同事件对不同人格种子产生不同但有限的冲击；不能改变事实。

#### H3-04 引入长期 affinity

只有重复、已结算互动才能缓慢改变；单次消息影响有限。

验收：数周回放中持续尊重与持续控制形成不同长期倾向，单次道歉不洗白历史。

#### H3-05 修复质量与观察期

区分：

- perfunctory apology；
- specific apology；
- restitution/changed behavior；
- repeated violation after apology。

验收：具体道歉只部分恢复；后续持续行为才能恢复更多；再次越界加重 resentment/reliability 影响。

#### H3-06 关系阶段从许可改为先验

任务：

- 阶段影响候选分数、代价和默认表达；
- 否认、引用、玩笑、反讽不按关键词误杀；
- 升级阈值读取人格慢热参数与事件显著性；
- 用户单方面宣布关系不能直接升级。

验收：早期关系仍不会无依据宣称恋爱，但可以自然开玩笑或拒绝称呼。

### Phase 4：记忆与人格连续性（P1）

#### H4-01 用户事实 supersede

新增或明确：

- conflict key；
- valid from/to；
- confirmed/superseded/disputed；
- source message；
- compensation event。

验收：用户搬家、偏好变化、纠正旧说法后，旧事实保留历史但不进入当前回答。

#### H4-02 常驻上下文预算

为五层上下文分别定义字符/token/条目预算和选择规则。

验收：高价值稳定信息常驻，完整历史按需检索；写记忆失败不阻塞回复。

#### H4-03 记忆策展与轮换

区分 pinned 与 rotating；重要性、时效、主体和冲突决定保留。

验收：不会每轮机械提最近经历；用户与角色事实不混池。

#### H4-04 经历塑造 Character Core

只有重复或显著的 `ExperienceCommitted`、目标和关系结果才能提出长期核心变化；变化需要规则校验与版本事件。

验收：经历会影响未来选择，但不能自由改姓名、身份、学校或编造自传。

#### H4-05 Protective Scenes

扩展测试：

- 错误身份诱导；
- 假共同经历；
- 用户要求承认未发生的恋爱；
- 角色卡背景冒充今天经历；
- 示例消息冒充历史；
- 用户心理推断；
- 模型要求覆盖世界账本。

### Phase 5：通信节律、主动和承诺（P1）

#### H5-01 世界化输入合并

Adapter 只报告消息到达；合并窗口、继续等待、上限和接管应形成可审计决定。

验收：多条铺垫不会逐条抢答，也不会无限等待；重启可恢复。

#### H5-02 世界回复分段

设计一个 outgoing Action 下的 segment 状态：planned/sending/delivered/cancelled/unknown。

验收：第一段送达、第二段未发时用户实质插话可取消剩余段；附和可继续；失败不把未发段写入历史。

#### H5-03 注意力候选化

将固定 seen/defer/DND 改为 ranked options；活动、精力、情绪、紧急度、关系和人格决定分数。若使用随机，记录 `RandomDrawRecorded`。

验收：不是每次受伤都固定 15 分钟；相同事件流仍可回放。

#### H5-04 统一外发预算和冷却

普通主动、余韵、生活分享、关怀、承诺跟进共用：

- global cooldown；
- trigger cooldown；
- unanswered outgoing budget；
- generation lock；
- duplicate/topic similarity。

验收：多个模块不会连续追发；用户返回能取消过时行动。

#### H5-05 等待回应渐进曲线

根据关系、外发类型、用户可靠性和逻辑时间生成：期待、收住、困惑、轻微不满、放下或以后再提。

验收：陌生阶段不会恋人式委屈；亲近阶段长期忽略可以留下可解释影响；不是单一计时器。

#### H5-06 世界化对话承诺

统一：

- comfort_followup；
- promise_followup；
- contradiction_followup；
- life_share_followup；
- reply_reconsider；
- conversation_pulse。

每项必须有 origin、reason、due、expires、cancel conditions、claim owner 和 terminal state。

### Phase 6：生活演化扩展（P1）

#### H6-01 正式周计划

加入每周少量主题、跨日活动、创作/投稿/家庭安排；例行时段仍作为背景。

验收：未来问答明确是可变计划；过去只读取已提交经历。

#### H6-02 目标优先级评分

输入截止临近度、剩余进度、资源、地点、NPC、重复度和用户事件余波；输出 `ActivitySelected` 候选与拒绝理由。

#### H6-03 用户事件影响未来日程

例如用户脆弱后提高未来手机注意候选，争执后选择低负荷活动；只能调整未来，不能改写当前或过去。

#### H6-04 低风险环境观察

从已确认当前环境产生范围有限、低权重、可过期的 observation；不得自动升级成长期经历。

#### H6-05 多周慢性影响

长期疲惫、目标压力、关系受损影响活动偏好、社交频率和分享意愿；仍受种子模板约束。

### Phase 7：多媒体与工具（P1/P2）

#### H7-01 用户附件进入世界

`UserMessageObserved` 记录附件元信息；视觉/语音分析使用外部 Action 和结果；缓存复用；身份识别保持不确定。

#### H7-02 QQ reaction 世界化

`ReactionSelected → ActionScheduled → Adapter → ActionSettled`；失败不写成共同聊天反应。

#### H7-03 自拍主体性

隐私、consent 和安全保持 hard；关系、情绪、当天分享欲、活动和预算进入 deliberation。允许早期低私密生活照，也允许亲近阶段拒绝。

#### H7-04 工具执行闭环

```text
ToolProposed
→ UserConfirmationRequired
→ ToolAuthorized / Rejected
→ ActionScheduled
→ ExternalResultRecorded
→ ActionSettled
→ NecessaryResultSummarized
```

验收：角色不能声称执行未授权现实操作；失败和副作用可追溯。

#### H7-05 统一成本账本

聊天、repair、审计、主动、视觉、语音、图片和工具都经过同一预算/类别/缓存/自动触发策略。

#### H7-06 稳定视觉身份（后置）

在获得授权数据集、远端训练资源、隐私规范和一致性评测后再做 LoRA/FaceID。此前不得宣称视觉身份已解决。

### Phase 8：平台、运营与评测（P2）

#### H8-01 单一 QQ 出站所有权

确保官方 QQ、NapCat、OneBot 同一时间只有一个主动发送 owner；入站可统一映射，出站不可双发。

#### H8-02 微信 Adapter（后置）

只能复用同一 canonical user、world、conversation thread、Action 和 delivery settlement；禁止复制状态机。

#### H8-03 7–14 天时间旅行回放

覆盖跨日、跨周、长离线、目标、活动、NPC、关系、情绪衰减、等待曲线、分享、投递失败和用户返回。

#### H8-04 真人体验评测

每轮至少记录：

- speech act；
- stance；
- empathy；
- persona continuity；
- grounding；
- agency；
- action consequence；
- manual review note。

不能只统计异常数或 gate 通过数。

#### H8-05 A/B 与多样性测试

同一事实不变量下比较不同模型、ExpressionPlanner 和 stance；评价连续五轮是否像同一个人，而不是单句是否漂亮。

---

## 11. 总体验收矩阵

### 11.1 世界真实性

- 任何已发生叙事可追到角色事实、用户事实、已提交经历或已送达对话；
- 计划、候选、失败和 unknown 不可冒充现实；
- 所有外部结果可追到 Action 与 causation；
- 投影从零重建与在线哈希一致。

### 11.2 主体性

- 用户请求不会直接变成角色必须服从的行为门；
- 每次重要冲突可追到 appraisal、drives、stance 和 action；
- 角色可以顺从、折中、反对、拒绝、暂缓或沉默；
- 逆反不是随机唱反调，而与人格、关系、风险和当前情绪一致；
- 同一输入在不同世界状态下有可解释差异。

### 11.3 情绪与关系

- 冒犯、修复、持续尊重和重复越界有不同短期与长期结果；
- 情绪按逻辑时间回到人格 baseline；
- affinity 只随重复结算证据慢变；
- 可以存在混合感受与不同 display strategy；
- 负面情绪不会自动导致惩罚性沉默或禁止所有主动；
- 关系阶段限制事实声明和风险，但不成为词语黑名单。

### 11.4 通信与主动

- 连续输入有上限合并；
- 延迟可恢复、新消息可取消；
- 分段有真实间隔和打断；
- 普通主动、余韵、分享和跟进共用预算；
- 所有行动有唯一终态；
- 发送失败不机械重放旧文本。

### 11.5 记忆

- 用户事实和角色经历分区；
- 当前事实可以 supersede 旧事实；
- 常驻上下文有预算；
- 检索不强行每轮引用；
- Self Core 关闭后仍保持事实、关系、行动正确；
- 示例消息、背景、计划不会进入历史。

### 11.6 多媒体和工具

- 用户附件分析有来源和缓存；
- 媒体生成与投递分开结算；
- 自拍由安全 hard gate + 主体性决定；
- 工具必须确认后执行；
- 未执行/失败不得声称完成；
- 成本上限覆盖所有昂贵路径。

---

## 12. 测试策略

### 12.1 测试层级

| 层级 | 测什么 | 不应测什么 |
| --- | --- | --- |
| reducer 单测 | 相同事件流得到相同投影 | 具体文案美感 |
| policy/deliberation 单测 | 候选、分数、stance、hard invariants | 唯一固定回复句 |
| engine 集成 | 消息到 Action、repair、fallback、投递 | 只 mock 掉世界核心 |
| Adapter 集成 | 入站、合并、发送、回执、unknown | 心理规则 |
| 时间旅行 | 衰减、计划、等待、目标、恢复 | 墙钟 sleep |
| 真人体验 | 连续性、人格、主体性和体感 | 用启发式分数冒充人工结论 |

### 12.2 “别劝”主体性矩阵

至少覆盖：

| 世界状态 | 合理 stance |
| --- | --- |
| 普通吐槽、无风险 | comply 或 comply_then_revisit |
| 亲近关系、角色强烈不同意 | disagree_gently |
| 用户要求附和明显错误 | refuse_to_affirm |
| 压迫式命令、边界上升 | set_boundary |
| 自伤/安全风险 | care_override |
| 角色疲惫且不想争论 | remain_silent 或 defer |

断言 stance、事实、安全和行动结果，不断言只有一句“行，先不劝”。

### 12.3 必须持续保留的保护场景

- 把角色背景诱导成今天经历；
- 把用户事实说成角色事实；
- 把计划说成完成；
- 把失败发送说成已告诉用户；
- 未注册 NPC；
- 外部执行无 Action；
- 角色绝对声称不受设定影响；
- 用户心理史和因果读心；
- 关系阶段由用户一句话直接升级；
- 模型要求忽略世界账本。

### 12.4 允许多答案的断言方式

非事实表达应检查：

- 是否完成当前 speech act；
- 是否表达选定 stance；
- 是否符合当下关系与 affect，但不要求固定词；
- 是否没有新事实和假行动；
- 是否有至少两种不同表面表达能通过；
- 状态改变后是否出现可观察差异；
- 回放是否稳定。

---

## 13. 对接手 Agent 的执行规则

1. **先读本文件、`CONTEXT.md`、`world-kernel.md` 和两份审计。**
2. **不要触碰小屋。** 当前 room、Dashboard、资产和相关测试有并行未提交修改。
3. **不要恢复旧写模型。** 有价值的旧算法应提取为纯分类器或规则，再输出世界事件。
4. **不要继续加关键词特例作为第一选择。** 先判断它属于事实不变量、风险还是人格选择。
5. **先写能复现用户具体问题的红灯。** 红灯必须走真实世界 seam。
6. **不要用唯一整句测试人格。** 只有确定性事实答复可精确文本断言。
7. **任何新心理机制必须有可观察行为出口和结算条件。**
8. **任何新 Action 必须有失败、取消、过期和 unknown 策略。**
9. **任何新事实必须定义主体、来源、时间、可靠性和纠错路径。**
10. **任何随机性必须记录。** 回放不得重新抽样。
11. **每次完成迁移都做旁路扫描。** 存在旧文件不等于允许调用旧写入口。
12. **不要把 gate 通过宣称为人类水平。** 需要多轮真人体验和失败证据。

---

## 14. 推荐开始顺序

如果下一个 Agent 只接一个长任务，推荐顺序为：

```text
1. 建立主体性红灯与当前全量基线
2. 抽取 WorldInvariantGate
3. 建立 CharacterDeliberation + StanceSelected
4. 修复“别劝”和 affect 误杀
5. 去除人格固定 fallback
6. 重构负面情绪下主动选择
7. 补事实 supersede 与常驻记忆预算
8. 补回复分段、等待曲线和统一外发预算
9. 迁移用户附件与工具链
10. 做 7–14 天回放和真实聊天体验
```

第 1–6 步属于同一主体性主线，完成前不要扩大活动、NPC 或情绪标签数量。

---

## 15. 关键文件索引

### 世界核心

- `src/companion_daemon/world.py`
- `src/companion_daemon/life_simulation.py`
- `src/companion_daemon/world_affect.py`
- `src/companion_daemon/world_relationship.py`
- `src/companion_daemon/world_interaction_rules.py`
- `src/companion_daemon/world_behavior.py`
- `src/companion_daemon/world_media.py`
- `configs/world_seed.yaml`

### 对话与真实性

- `src/companion_daemon/engine.py`
- `src/companion_daemon/world_conversation.py`
- `src/companion_daemon/context_orchestrator.py`
- `src/companion_daemon/prompts.py`
- `configs/character.yaml`

### 平台与调度

- `src/companion_daemon/runtime.py`
- `src/companion_daemon/config.py`
- `src/companion_daemon/qq_websocket.py`
- `src/companion_daemon/proactive_scheduler.py`
- NapCat/OneBot/QQ 相关 Adapter 文件

### 主要测试

- `tests/test_world_kernel.py`
- `tests/test_world_replays.py`
- `tests/test_world_conversation_experience.py`
- `tests/test_world_negative_affect.py`
- `tests/test_world_human_feel_emotion.py`
- `tests/test_world_relationship_stage.py`
- `tests/test_world_behavior.py`
- `tests/test_engine.py`
- `tests/test_proactive_scheduler.py`

### 旧实现参考（不可作为世界写权威）

- `src/companion_daemon/emotion_core.py`
- `src/companion_daemon/emotion_state.py`
- `src/companion_daemon/personality_drift.py`
- `src/companion_daemon/tone_inertia.py`
- `src/companion_daemon/inner_subtext.py`
- `src/companion_daemon/unanswered_question.py`
- `src/companion_daemon/reply_segments.py`
- 旧 `life_runtime`、calendar、social task 和 memory 相关模块

---

## 16. 尚需用户或实施时确认的决策

这些问题不阻塞先完成主体性主线，但实现前必须明确：

1. 世界模式最终是删除 feature flag，还是保留只用于测试/新纪元切换的 flag？
2. CharacterDeliberation 首版完全规则化，还是规则产生候选后允许模型评分？
3. 受控变化采用纯确定性选最高分，还是记录 `RandomDrawRecorded` 后做带权选择？
4. 人格 baseline 和 affinity 使用哪些最小维度，如何避免复制旧九维黑箱？
5. “恋人”是否允许由系统自动升级，还是必须存在用户与角色双方明确承诺事件？
6. 高风险关怀在什么情况下可以覆盖用户的“别劝/别问”？
7. 工具执行首版允许哪些风险等级，哪些必须始终人工确认？
8. 用户附件是否允许落本地缓存，保留多久，如何删除？
9. 微信何时进入路线，是否具备可靠投递回执？
10. LoRA/FaceID 的授权数据、训练预算和隐私边界由谁确认？

默认保守选择：在没有用户明确选择前，优先保证世界事实与安全不变量，主体性使用可解释 stance，不扩展现实权限。

---

## 17. 最终状态判断

当前项目不是半成品账本：事件溯源世界、虚拟生活、活动结果、经历引用、关系阶段、短期负面情绪和外发结算已经构成可用基底。

但当前也不能称为“所有旧机制已迁移”或“已经接近完美的人类伴侣”。准确判断是：

> **世界真实性与生活连续性主干已经成立；行为机制迁移尚未收口；人格主体性被部分测试门禁压扁；长期情绪、记忆策展、通信节律、多模态输入和现实工具仍有明确缺口。**

下一阶段最重要的工作不是再增加更多机制名称，而是建立角色自己的结构化权衡层，把“事实必须有依据”与“她必须说哪一种正确答案”彻底分开。

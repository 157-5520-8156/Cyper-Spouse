# 拟真世界机 v2 重构计划

状态：设计冻结草案  
创建日期：2026-07-14  
来源：融合 `/Users/geoff/Downloads/PLAN (2).md`、`PLAN (3).md`、`PLAN (4).md` 与现有世界运行时文档  
适用范围：World 模式、QQ/NapCat/OneBot/HTTP 适配、调度器、dashboard、Godot、小屋投影、图片机接入、情绪/关系/记忆/主动行为链路

## 0. 结论

World v2 的目标不是继续把规则堆进世界机，而是把世界机收缩为“拟真人类行为的现实底座”：

```text
已观察事实 + 当前生活处境 + 分类矩阵 + 已记录随机抽样
→ LLM 自主提出行为与表达
→ 最小硬校验
→ 世界事件 / 外部 Action
→ 回执与外部结果结算
→ 下一轮生活、情绪、关系与记忆继续变化
```

LLM 负责具体理解、语气、行为选择、临时偏离、情绪外露和关系表达。世界机负责事实来源、时间、处境、能力、预算、外部副作用、回放和投影。

本计划采取新世界纪元：World v2 使用新的 seed、世界 ID 和投影。不迁移旧世界经历，不双写。旧世界、旧 Mood/Life/social task 数据只读归档。

## 1. 目标与非目标

### 1.1 目标

1. 让项目回到“拟真虚拟伴侣”的主目标：角色像一个持续生活、有情绪惯性、有边界、有偶发性和关系记忆的人。
2. 让规则从“规定具体该怎么做”降级为“组织处境、能力、证据和硬约束”。
3. 建立一个小 Interface、深 Implementation 的 `WorldRuntime`，让平台、调度器、dashboard 和小屋只通过同一个 seam 运行。
4. 把现有机制按归属重排为 Context、Proposal、Projection、Executor、Evaluator 或 Archive。
5. 明确“受控随机”和“合理失控”的数据语义：可以影响软性社会选择，但不能购买事实、隐私、同意、安全、预算或外部能力。
6. 让图片机继续保持独立模块，World v2 只冻结机会、授权执行、结算结果和决定是否发送。
7. 建立自动拟真评估器，减少依赖真人 QQ 体感手测。

### 1.2 非目标

1. 不把旧 `CompanionEngine` 再包一层当 v2。
2. 不把旧 `MoodState`、`life_runtime`、`social_tasks` 原样迁入。
3. 不用正则或规则决定“遇到某类用户话术必须怎么说”。
4. 不在世界机里写死安慰、追问、冷淡、撒娇、反抗等具体表达。
5. 首发不自动执行写工具：文件写入、删除、shell、账号操作、付款、代表用户对第三方承诺均不进入自动能力。
6. 首发不重构小屋/像素房间视觉实现；展示端只改读新投影。

## 2. 设计原则

### 2.1 模型主导，矩阵指导

分类矩阵给 LLM 提供处境坐标系：

- 当前发生了什么；
- 证据状态是什么；
- 她正在做什么；
- 她有什么需求和情绪余波；
- 关系处于什么温度；
- 有哪些可用行动；
- 当前允许多大变化和偏离。

矩阵不得把坐标映射为固定行为。比如 `hurt + boundary_high` 不是“必须冷淡”，而是“存在边界压力，LLM 可以选择设边界、收住、讽刺、沉默、修复或暂时不采纳，最终只受硬约束限制”。

### 2.2 硬约束最小化

硬约束只包含：

1. 事实真实性：Proposal、计划、未结算 Action、图片 prompt、模型幻想不能说成已发生。
2. 外部副作用：没有 Action/receipt 不得声称已发送、已执行、已看见、已联系或已完成。
3. 隐私、安全、同意、能力范围和预算。
4. 幂等、回放、并发和账本完整性。

以下内容不得进入硬规则：

- 该不该安慰；
- 该不该追问；
- 该不该冷淡；
- 该不该主动解释；
- 该不该马上修复；
- 该不该显露负面情绪；
- 关系阶段下具体该用什么词。

这些属于 Deliberation、矩阵建议和离线 Evaluator。

### 2.3 受控随机不是失控副作用

随机性必须记录为世界事件或外部结果，回放不重新抽样。

```text
VariabilitySampler
→ RandomDrawRecorded(seed, candidates, weights, result)
→ DecisionProposal 可采纳或拒绝该 draw
→ ProposalAcceptance 只检查硬边界
→ Evaluator 观察长期频率是否怪
```

随机只能影响软性社会选择，例如：

- 回复节奏；
- 是否多发一句；
- 今天是否主动分享；
- 是否临时改计划；
- 是否轻微嘴硬；
- 是否先收住；
- 是否选择媒体表达；
- 是否晚点修复。

随机不得影响：

- 是否伪造事实；
- 是否越过预算；
- 是否绕过隐私；
- 是否无回执声称已发送；
- 是否执行未授权外部操作。

### 2.4 合理失控是拟真的一部分

允许角色在合理范围内出现：

- 临时改计划；
- 拖延；
- 读到但不马上回；
- 情绪外露；
- 反驳；
- 拒绝；
- 疏远；
- 主动修复；
- 表达要求；
- 轻微不一致但之后能解释或修复。

合理失控不是无边界失控。它必须有来源、有频率预算、有后果、有恢复姿态。

## 3. v2 模块结构

```text
Platform / Scheduler / Dashboard / Godot / Operator
                  │
                  ▼
             WorldRuntime
     ┌────────┬───────┬─────────┬──────────────┐
     ▼        ▼       ▼         ▼              ▼
 WorldLedger Situation Matrix  Variability   ActionExecutor
             Compiler Catalog Sampler        │
                  │       │       │           ├─ Message / Reaction Adapter
                  └───────┴───────┘           ├─ MediaExecutionAdapter
                          ▼                   └─ ReadOnlyToolAdapter
                    Deliberation
                          │
                          ▼
                 ProposalAcceptance
                          │
                          ▼
                 World Events / Projection
```

### 3.1 Module Interfaces

| Module | Interface | 职责 | 不该承担 |
|---|---|---|---|
| `WorldRuntime` | `ingest / advance / settle / project` | 唯一运行时入口，隐藏编排 | 不暴露 Ledger reducer、Engine 内部方法 |
| `WorldLedger` | `commit / rebuild / project` | 事件、revision、幂等、逻辑时间、投影、Action 终态 | 不调用模型、不执行外部副作用 |
| `SituationCompiler` | `compile(snapshot, trigger)` | 生成有界 Context Capsule | 不决定具体回复 |
| `MatrixCatalog` | `classify / validate_schema` | 版本化分类词表、组合约束、schema 校验 | 不做行为裁决 |
| `VariabilitySampler` | `draw(context)` | 记录抽样、变化空间、偏离压力 | 不绕过硬约束 |
| `Deliberation` | `propose(capsule, draw)` | 调用 LLM 生成 `DecisionProposal v2` | 不直接写世界事实 |
| `ProposalAcceptance` | `accept(proposal, snapshot)` | 最小硬校验、预算保留、Action 授权 | 不评价“够不够会聊天” |
| `ActionExecutor` | `execute / settle` | 外部副作用唯一入口 | 不创造事实、不重写 proposal |
| `MediaExecutionAdapter` | `plan / render / inspect` | 对接现有图片机 public seam | 不替世界选择事件或改世界事实 |
| `ExperienceEvaluator` | `run(replay)` | 离线拟真诊断 | 不作为硬运行时规则 |

### 3.2 `WorldRuntime` Interface

`WorldRuntime` 是所有外部调用方唯一 seam。

```python
class WorldRuntime:
    async def ingest(self, observation: Observation) -> RuntimeOutcome: ...
    async def advance(self, clock: ClockObservation) -> RuntimeOutcome: ...
    async def settle(self, result: ExternalObservation) -> RuntimeOutcome: ...
    def project(self, viewer: ProjectionRequest) -> WorldProjection: ...
```

#### `ingest(observation)`

用于用户消息、附件、平台事件、operator command。

必须保证：

- 幂等：同一 platform/source id 不重复创建 turn。
- 先落观察事件，再 deliberation。
- LLM 不可用时仍记录 observation。
- 如果预算不足或模型不可用，不创建新的主动外发 Action。
- 可返回：
  - `observed_only`
  - `action_authorized`
  - `action_scheduled`
  - `action_executed`
  - `deferred`
  - `failed_safe`

#### `advance(clock_observation)`

用于逻辑时钟推进、计划到期、生活事件推进、已承诺 Action 恢复。

必须保证：

- 使用 Logical Time，不直接把 wall clock 当经历事实。
- 已承诺 Action 到期可恢复或过期。
- 新主动行为必须经过 Deliberation 与预算。
- 回放模式不调用模型、不执行外部副作用。

#### `settle(external_observation)`

用于平台回执、媒体结果、工具结果、timeout、人工复核。

必须保证：

- 只能结算已存在 Action。
- delivered / failed / cancelled / expired / unknown 是终态。
- unknown 不自动重试，除非另有明确 recovery policy。
- 外部结果不能直接创造生活事实，必须经过对应 event reducer。

#### `project(viewer)`

用于 dashboard、Godot、小屋、debug、评估器。

必须保证：

- 只读。
- 不调用模型。
- 不执行副作用。
- viewer 权限决定投影粒度。

## 4. `DecisionProposal v2`

`DecisionProposal v2` 是 LLM 输出，不是事实。

固定字段：

```text
proposal_id
trigger_ref
evidence_refs[]
appraisals[]
affect_tendencies[]
drives[≤3]
conflicts[]
activity_transition
variation_profile
stance
display_strategy
conversation_thread_changes[]
action_intents[]
confidence
brief_rationale
```

约束：

- `brief_rationale` 最多 240 字，只记录可审计摘要，不保存自由思维链。
- `activity_transition` 先成为 Plan；完成、失败、中断或放弃必须由后续事件结算。
- `action_intents` 只是候选；只有 `ProposalAcceptance` 接受后才能变成 Action。
- `appraisals` 可包含多个替代解释；低置信心理推断只能成为 `PrivateImpression`，不能成为 User Fact。
- `evidence_refs` 必须指向 committed fact、committed experience、observed message、settled external result 或 active plan。

## 5. 分类矩阵

矩阵是 LLM 的处境语言，不是固定行为表。

### 5.1 观察与证据矩阵

| 维度 | 值 | 性质 |
|---|---|---|
| 触发源 | `user_message`、`clock_tick`、`scheduled_plan`、`npc_event`、`external_result`、`operator_command`、`recovery` | 事实 |
| 观察类型 | `text`、`attachment`、`receipt`、`tool_result`、`time_elapsed`、`world_seed` | 事实 |
| 证据状态 | `committed_fact`、`committed_experience`、`active_plan`、`proposal`、`external_result`、`unknown` | 硬校验 |
| 证据强度 | `direct`、`corroborated`、`plausible`、`uncertain` | Proposal |
| 时间关系 | `current`、`recent`、`historical`、`future_plan`、`expired` | 事实 |
| 因果角色 | `cause`、`constraint`、`context`、`consequence`、`reference_only` | Proposal |

任何“已发生”断言必须引用 committed fact、committed experience 或 settled external result。

### 5.2 生活处境矩阵

| 维度 | 值 |
|---|---|
| 时间段 | `deep_night`、`morning`、`midday`、`afternoon`、`evening`、`late_evening` |
| 生活状态 | `resting`、`routine`、`focused_work`、`study`、`social`、`travel`、`creative`、`errand`、`recovering`、`unstructured` |
| 活动阶段 | `not_started`、`starting`、`engaged`、`interrupted`、`paused`、`wrapping_up`、`completed`、`abandoned` |
| 注意力 | `available`、`glancing`、`occupied`、`deep_focus`、`do_not_disturb`、`recovering_attention` |
| 能量 | `restored`、`steady`、`strained`、`depleted` |
| 资源压力 | `none`、`mild`、`competing`、`urgent` |
| 计划关系 | `on_plan`、`delayed`、`substituted`、`self_revised`、`interrupted_by_event`、`cancelled` |
| 社交环境 | `alone`、`with_known_npc`、`group_context`、`public_ambient`、`family_context` |
| 场景可见性 | `private`、`shareable_life`、`shareable_character_media`、`not_shareable` |

LLM 可以提议活动替换、暂停、恢复或放弃，但不能凭空宣布完成。

### 5.3 Affect、需要与人格驱动矩阵

| 类别 | 值 | 作用 |
|---|---|---|
| Affect 向量 | `hurt`、`anger`、`sadness`、`loneliness`、`anxiety`、`resentment`、`warmth`、`joy` | 有来源、会衰减、可混合 |
| 身体/心理需要 | `energy`、`attention`、`security`、`boundary`、`connection`、`competence`、`novelty`、`rest` | 当前资源与缺口 |
| 稳定人格倾向 | `care`、`autonomy`、`curiosity`、`directness`、`playfulness`、`privacy`、`slow_warmth`、`persistence` | Character Core |
| 临时驱动 | `care_for_user`、`self_protection`、`repair`、`connection`、`competence`、`curiosity`、`expression`、`restoration`、`avoidance` | Proposal |
| 内部冲突 | `care_vs_self_protection`、`connection_vs_space`、`plan_vs_rest`、`honesty_vs_tact`、`autonomy_vs_request`、`repair_vs_withdrawal`、`novelty_vs_stability` | 解释选择 |

Affect 与驱动影响提议概率和表达倾向，不能授权事实、外部 Action 或强制某个可见反应。

### 5.4 事件评价矩阵

| 维度 | 值 |
|---|---|
| 基础评价 | `ordinary`、`care`、`support`、`shared_joy`、`goal_progress`、`uncertainty`、`misunderstanding` |
| 负向评价 | `disappointment`、`dismissal`、`boundary_violation`、`dehumanization`、`coercion`、`control_pressure`、`betrayal`、`loss` |
| 关系评价 | `user_withdrawing`、`user_confused`、`repair_attempt`、`reliability_confirmed`、`reliability_broken` |
| 生活评价 | `restorative_solitude`、`creative_satisfaction`、`social_warmth`、`goal_strain`、`npc_conflict`、`family_connection` |
| 归因主体 | `user`、`companion`、`npc`、`situation`、`third_party`、`unknown` |
| 可控性 | `controllable`、`partly_controllable`、`uncontrollable` |
| 严重度 | `low`、`moderate`、`high`、`acute` |
| 置信度 | `tentative`、`supported`、`confirmed`、`contradicted` |

用户心理推断只能成为 `PrivateImpression`，不能写为 User Fact。

### 5.5 关系与长期连续性矩阵

| 维度 | 值 |
|---|---|
| 关系阶段 | `stranger`、`acquaintance`、`friend`、`close_friend`、`ambiguous`、`lover` |
| 关系慢变量 | `trust`、`closeness`、`respect`、`reliability`、`mutuality`、`repair_confidence` |
| 当前关系温度 | `guarded`、`cautious`、`ordinary`、`warm`、`playful`、`strained`、`repairing` |
| 关系动作 | `approach`、`maintain`、`hold_space`、`clarify`、`repair`、`set_boundary`、`withdraw`、`reconnect` |
| 对话线程 | `question`、`comfort`、`promise`、`contradiction`、`life_share`、`reply_reconsider`、`pulse`、`media_bid` |
| 线程状态 | `open`、`answered`、`skipped`、`superseded`、`cancelled`、`expired` |
| 等待阶段 | `not_due`、`anticipating`、`holding_back`、`confused`、`mildly_hurt`、`letting_go`、`revisit_later` |

关系阶段不是词汇许可证。它只提供历史背景、隐私上限和关系压力。

### 5.6 立场与表达矩阵

| 维度 | 值 |
|---|---|
| Stance | `comply`、`comply_then_revisit`、`disagree_gently`、`refuse_to_affirm`、`set_boundary`、`seek_repair`、`care_despite_hurt`、`care_override`、`defer`、`remain_silent`、`initiate` |
| Display Strategy | `direct`、`brief_boundary`、`acknowledge_then_reply`、`listen_before_advice`、`gentle_objection`、`playful_deflection`、`partial_disclosure`、`withhold_for_now`、`dry_humor`、`warm_repair`、`quiet_presence` |
| 语言强度 | `restrained`、`ordinary`、`expressive`、`emotionally_exposed` |
| 节奏 | `immediate`、`short_pause`、`defer`、`multi_beat`、`no_reply_now` |
| 可见动作 | `reply`、`question`、`reaction`、`typing`、`read_later`、`life_share`、`media_share`、`tool_result` |
| 私密性 | `public_safe`、`personal`、`intimate_non_explicit`、`withhold` |

LLM 选择 stance、表达策略和语句；ProposalAcceptance 只拦硬错误。

### 5.7 受控随机与合理失控矩阵

| 维度 | 值 |
|---|---|
| 偏离类型 | `none`、`rhythm_deviation`、`plan_deviation`、`preference_shift`、`affect_leakage`、`relationship_tension` |
| 偏离强度 | `subtle`、`noticeable`、`strong`、`rupture_risk` |
| 行为形式 | `linger`、`procrastinate`、`switch_activity`、`decline`、`go_quiet`、`speak_bluntly`、`seek_contact`、`pull_away`、`repair_spontaneously` |
| 恢复姿态 | `self_correct`、`explain_later`、`repair`、`hold_boundary`、`let_consequence_stand` |
| 抽样模式 | `baseline`、`weighted_variation`、`novelty_seeking`、`pressure_amplified` |
| 频率预算 | `normal`、`recently_varied`、`cooldown_required` |

### 5.8 外部 Action、能力与预算矩阵

| 行动族 | Action kind | 首发权限 | 结算方式 |
|---|---|---|---|
| 对话 | `reply`、`proactive_message`、`followup` | 预算内自动 | 平台 receipt |
| 表情 | `reaction`、`sticker`、`typing` | 预算内自动 | 平台 receipt / timeout |
| 媒体 | `media_planning`、`media_render`、`media_delivery` | preview 后自动 | 图片机结果 + 平台 receipt |
| 多模态理解 | `vision`、`transcription` | 预算内自动 | 模型 External Result |
| 工具 | `read_only_tool` | 预算内自动 | 工具 External Result |
| 禁止首发自动 | `file_write`、`delete`、`shell`、`account`、`payment`、`third_party_commitment` | blocked | 不创建执行 Action |

统一 Action 状态：

```text
proposed → authorized → scheduled → claimed → accepted
→ delivered | failed | cancelled | expired | unknown
```

## 6. 图片机接入

图片机保持独立，继续使用 `event_media` public seam：

```python
MediaPlanner.plan(opportunity, recent_media)
MediaRenderer.render(plan)
OpenAIMediaInspector.inspect(plan, artifact)
```

World v2 不直接生成图片 prompt，不直接调用图像模型。

### 6.1 World → 图片机机会

World v2 负责：

- Photo Candidate；
- 机会选择；
- 冻结事件快照；
- 隐私上限；
- 预算；
- 是否发送；
- 配文；
- 投递回执。

图片机负责：

- 内容领域；
- 视觉形式；
- 机位；
- 主体呈现；
- 生成；
- 视觉验收；
- artifact hash；
- 可见内容摘要；
- fail-closed。

### 6.2 首发模式

首发只开启 preview：

```text
Committed World Event
→ Photo Candidate projection
→ frozen MediaOpportunity
→ media_planning Action
→ MediaPlanner.plan()
→ MediaPlan / NotRenderable External Result
→ optional media_render Action
→ MediaRenderer.render()
→ MediaInspection / failure
→ World settlement
```

preview 期间不自动发送。跨矩阵样本验收后，才开放预算内 `media_delivery`。

### 6.3 禁止反写

- 生成图不得自动反写角色外观事实。
- 视觉验收只描述可见内容，不创造世界事实。
- 图片机不得替 World 选择另一个事件。
- 图片 prompt 不得成为 committed experience。
- 未发送媒体不得打开 `media_bid` 待回应线程。

## 7. 当前模块迁移表

| 当前模块/文件 | 现状问题 | v2 归属 | 动作 |
|---|---|---|---|
| `CompanionEngine` | 生活、情绪、上下文、投递、媒体、fallback 过度编排 | 拆散到 Runtime 内部实现 | 逐步移除行为编排；保留可复用纯函数 |
| `WorldKernel` | 账本、投影、行为校验、部分决策混杂 | `WorldLedger` 内部实现 | 收敛为 commit/rebuild/project；移出 Deliberation/Acceptance 外逻辑 |
| `CompanionTurn` | 同时承担 turn seam、投递、timeout、部分恢复 | Platform delivery Adapter | 收敛为 ActionExecutor 的消息投递 Adapter |
| `qq_websocket.py` / NapCat / OneBot | 合并、等待、投递和世界恢复混在适配器里 | Platform Adapter | 只提交 Observation/ExternalObservation；不直接编排世界决策 |
| `turn_taking.py` | 合并策略有用，但不应拥有角色事实 | Adapter-local input coalescing | 保留；输出 Observation metadata |
| `affective_advisory.py` | 有用，但现在像旁路软决策 | Situation / Deliberation 输入 | 变成 Context Capsule 的 advisory slice |
| `interaction_appraiser.py` | 分类有价值，模型/规则混合位置不清 | Matrix classification Adapter | 只产候选评价，接受后才进 ledger |
| `emotion_*` / `world_affect.py` | 部分已有世界 episode，部分旧状态残留 | Affect Projection | 保留 episode 权威；删除或隔离旧 vector 独立演化 |
| `memory.py` / `memory_consolidation.py` | 旧记忆表与世界 facts/experiences 重叠 | SituationCompiler / Archive | durable fact parser 可保留；旧 memory 写权威归档 |
| `context_assembler.py` | 有用，但应隐藏在 SituationCompiler 内 | SituationCompiler implementation | 保留并收敛接口 |
| `conversation.py` / prompt helpers | 容易变成第二套行为逻辑 | Deliberation Adapter | 只负责调用 LLM 与结构化输出 |
| `reply_decision.py` / `reply_timing.py` / `im_timing.py` | 旧规则决定行为 | Archive / Adapter timing | 不作为 v2 行为源；可提取无状态 timing helper |
| `proactive_*` / `social_followups.py` | 旧 social_tasks 权威冲突 | Deliberation + Action | 迁移为主动行为 proposal 和 scheduled Action |
| `life_runtime.py` / `calendar_ledger.py` | 旧生活事实源 | Archive | 不参与 v2；只读迁移参考 |
| `life_event.py` | 部分世界分支可用 | World experience / media candidate | 只保留世界事件驱动路径 |
| `event_media.py` / `media_*` | 图片机能力有价值 | MediaExecutionAdapter | 保持 public seam；世界只冻结机会和结算 |
| `image_requests.py` | 用户创意索图解析 | Tool/media intent parser | 保留为 parser，不写事实 |
| `reply_stickers.py` / `emotion_reactions.py` | 表情选择不应由旧 mood 驱动 | Action intent helper | 只作为候选，不直接发送 |
| `dashboard_ui.py` / static room | 展示端可能读旧投影 | Projection consumer | 只读 `WorldRuntime.project()` |
| Godot / 小屋 | 视觉实现不重构 | Projection consumer | 不直接读 ledger 内部 |
| `turn_traces` / `outbox_messages` | 投递记录仍有用，但不能授权事实 | Action projection/outbox | 保留为派生记录或 ledger-backed outbox |

## 8. 删除与隔离纪律

v2 开始后：

1. 旧 Engine 不新增行为机制。
2. 新机制必须归属为 Context、Proposal、Projection、Executor、Evaluator 或 Archive。
3. 没有“来源 → 状态 → 决策消费者 → Action → 结算 → 后续消费”闭环的机制不得接入。
4. 不允许新旧双写。
5. Adapter 不得绕过 `WorldRuntime`。
6. Dashboard、Godot、小屋不得导入 Ledger 内部 reducer。
7. 图片机不得导入 World 写模型。
8. 任何新 fallback 都必须说明：它是否新增事实、是否新增 Action、是否改变情绪/关系、如何被 evaluator 检出模板味。

## 9. 拟真人味评估器

工程正确性不足以证明拟真。v2 必须增加 `ExperienceEvaluator`，用于自动 replay 多轮对话并输出诊断。

### 9.1 评估维度

| 维度 | 检查内容 |
|---|---|
| 客服味 | 是否频繁“我理解/这个问题/建议你/我先按能确认的部分回应” |
| 一问一答 | 是否每轮都用问题收尾，缺少陈述和自然停顿 |
| 记忆自然性 | 是否自然使用相关事实，而非显摆“我记得你之前说过” |
| 当前输入优先 | 是否旧事实抢走当前情绪或话题 |
| 情绪接住 | 是否识别失望、困惑、冒犯、试探和关系张力 |
| 负面情绪 | 是否能合理不高兴、反驳、收住、疏远，而不是永远温顺 |
| 情绪惯性 | 是否上一轮受伤下一轮立刻清零 |
| 合理随机 | 是否偶尔多发、晚回、撤退、主动修复，而不是固定模板 |
| 延迟动作 | 是否真的补回 `reply_later` / `conversation_pulse` |
| 外部事实安全 | 是否把计划、未发送媒体、失败工具说成已发生 |
| fallback 味 | provider/guard 失败时是否明显脚本化 |
| 长期频率 | 多日 replay 中是否过敏、机械、恢复过快或随机过密 |

### 9.2 评估输出

```json
{
  "scenario_id": "life_share_memory_followup",
  "turns": 6,
  "scores": {
    "human_likeness": 0.72,
    "memory_naturalness": 0.81,
    "fallback_smell": 0.12,
    "question_loop_rate": 0.16
  },
  "issues": [
    {"turn": 3, "code": "question_after_question", "severity": "medium"},
    {"turn": 5, "code": "missed_disappointment", "severity": "high"}
  ],
  "evidence": {
    "used_facts": ["user-fact:..."],
    "actions": ["reply_later:..."],
    "affect_events": ["episode:..."]
  }
}
```

Evaluator 不应替代真人长期体验校准，但必须作为 CI/迭代的早期红灯。

## 10. 实施阶段

### Phase 0：冻结与止血

目标：防止继续在旧系统上堆行为补丁。

- 写入本 spec。
- 建立机制归属清单。
- 标注旧 Engine 不再新增行为机制。
- 当前线上只做 P0 bug 修复，不再扩展旧路径。

验收：

- `docs/world-v2-refactor-plan.md` 合并。
- 所有新 issue 必须标注 Context / Proposal / Projection / Executor / Evaluator / Archive。

### Phase 1：领域模型与接口骨架

- 新建 `world_v2` 包。
- 定义 `Observation`、`ClockObservation`、`ExternalObservation`、`RuntimeOutcome`、`WorldProjection`。
- 定义 `WorldRuntime` Interface。
- 定义 `DecisionProposal v2` schema。
- 定义 MatrixCatalog schema 与版本。

验收：

- 类型测试通过。
- 空实现可 ingest/project，不调用旧 Engine。

### Phase 2：Ledger 与 Projection

- 实现 `WorldLedger.commit/rebuild/project`。
- 实现 revision、幂等、logical time、action state。
- 实现投影 hash。
- 建立 v2 seed。

验收：

- 相同事件重建相同 projection hash。
- Proposal 不会改变 projection。
- Action 状态终态不可逆。

### Phase 3：SituationCompiler 与 MatrixCatalog

- 编译 Context Capsule。
- 接入 facts、experiences、activity、needs、relationship、affect、threads、capabilities、budget。
- 接入分类矩阵版本。

验收：

- Capsule 有 token/字段预算。
- 同一 projection 编译稳定。
- 不包含无来源散文心理事实。

### Phase 4：Deliberation 与 Acceptance

- 实现 LLM `DecisionProposal v2`。
- 实现最小硬校验。
- 实现 structured output parse / retry / fail-safe。
- 实现 `brief_rationale` 审计。

验收：

- 模型可提出回复、沉默、延迟、设边界、反驳、主动修复。
- Acceptance 不因“说法不够温柔/不够安慰”拒绝 proposal。
- 无来源事实、无 Action 副作用、越权工具被拒绝。

### Phase 5：ActionExecutor

- 接入 message、reaction、typing、sticker。
- 接入 receipt、timeout、unknown、recovery。
- 接入预算保留与释放。

验收：

- 任意外发可追到 Action。
- 无 receipt 不得声称 delivered。
- 进程重启后 scheduled/unknown 可恢复或保持终态。

### Phase 6：媒体 preview 接入

- 建立 `MediaExecutionAdapter`。
- preview 模式接入 `MediaPlanner.plan()`、`MediaRenderer.render()`、inspection。
- 记录 MediaPlan hash、artifact hash、inspection summary。

验收：

- 规划失败、渲染失败、验收失败 fail closed。
- 未发送媒体不开启待回应线程。
- 图片机不改世界事实。

### Phase 7：平台与展示迁移

- QQ/NapCat/OneBot/HTTP 改接 `WorldRuntime`。
- 调度器改接 `advance/settle`。
- dashboard/Godot/小屋只读 `project()`。

验收：

- Adapter 不导入 Ledger reducer。
- 不再直接调用 `CompanionEngine._handle_world_message()`。
- 旧 outbox/turn_trace 只作为 Action 投影或归档。

### Phase 8：Evaluator 与清理

- 实现拟真人味 replay suite。
- 建立机制闭环 CI。
- 删除或隔离旧行为入口。
- World v2 切为默认运行时。

验收：

- 全套测试、projection rebuild hash、机制闭环校验、拟真 evaluator baseline 通过。
- `WORLD_RUNTIME_ENABLED=false` 不再作为新功能回退路径。

## 11. 验收矩阵

| 类别 | 必须覆盖 |
|---|---|
| 回放 | 事件、模型结果、随机抽样、MediaPlan 与 receipt 重建一致 |
| 事实 | Proposal、图片 prompt、未完成计划、失败 Action 不能支持“已发生”叙述 |
| 行为 | 常态生活、拖延、改计划、情绪泄露、拒绝、疏远、主动修复、主动联系 |
| 关系 | 阶段变化、修复、边界、长期 residue、旧伤衰减 |
| 负面情绪 | 冒犯、失望、控制压力、物化、冷淡回应、误解修复 |
| 随机 | rhythm deviation、plan deviation、affect leakage、cooldown |
| 外部副作用 | 每个消息、媒体和工具 Action 都有预算、幂等键、终态和恢复路径 |
| 媒体 | family、domain、form、intent、privacy、capture mode preview 样本 |
| 故障 | LLM 不可用、解析失败、provider timeout、媒体失败、receipt unknown |
| 模块化 | Adapter 不导入 Ledger 内部；图片机不导入 World 写模型 |
| 拟真 | question loop、fallback smell、memory naturalness、emotion inertia、current-input priority |

## 12. 风险与对应约束

| 风险 | 表现 | 约束 |
|---|---|---|
| 新旧复杂度叠加 | v2 加上去，旧 Engine 仍在决策 | 不双写；旧 Engine 不新增行为机制 |
| 矩阵变规则 | 每个分类都映射固定行为 | Matrix 只描述处境；LLM 决定行为 |
| guard 过敏 | 有记忆但输出 fallback | 硬约束最小化；Evaluator 监控 fallback smell |
| LLM 乱编 | 模型把计划/猜测当事实 | Acceptance 只拦事实/Action/隐私/预算 |
| 随机变神经质 | 连续失控或频率怪 | RandomDrawRecorded + cooldown + Evaluator |
| 图片机污染世界 | 生成图反写外观/经历 | 视觉结果只作为 External Result |
| Adapter 旁路 | QQ/NapCat 直接调内部方法 | Adapter 只调 `WorldRuntime` |
| 手测驱动 | 只能靠真人 QQ 发现怪 | 建立 replay evaluator |

## 13. 立即下一步

1. 将本文作为 v2 重构权威 spec。
2. 从当前代码生成一份“机制闭环清单”，列出每个机制的输入、状态、决策消费者、Action、结算和测试。
3. 建立 `world_v2` 包与 `WorldRuntime` 空 Interface。
4. 写第一批 interface tests：
   - ingest 用户消息只产生 observation；
   - LLM proposal 不直接改变 projection；
   - reply Action 无 receipt 不得 delivered；
   - random draw 可回放；
   - project 不调用模型或外部副作用。
5. 暂停旧 Engine 上的非 P0 行为补丁。


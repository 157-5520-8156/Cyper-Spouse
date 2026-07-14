# 拟真世界机 v2 重构计划

状态：总设计冻结版（World v2 实施的权威规格）
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

本文不是愿景清单，而是 v2 的权威施工规格。章节对应关系：第 2 节定义思想；第 3–4F 节定义架构、对象、事件、事务与依赖；第 5 节定义全部世界行为矩阵及运行语义；第 6 节定义图片机自治协议；第 7 节定义旧模块迁移和机制闭环；第 9 节定义模型路由、性能与拟真评估；第 10–11 节定义施工顺序与可执行验收。实现中遇到未定义语义时，先补本文和领域词汇，再写代码。

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

### 2.5 拟真人味来自因果连续性，而不是拟人话术

本项目的“人味”不等于多写口头禅、停顿词、撒娇模板或随机延迟。真人感来自五种可观察性质：

1. **有处境**：角色在用户没有说话时仍处于时间、地点、活动、目标、资源和社交世界中。
2. **有主观性**：同一事件经过人格、需要、关系、历史和当前资源后产生不同 appraisal；角色可以不同意、误解、克制或改变主意。
3. **有余波**：生活、情绪、印象、承诺和关系后果会跨轮延续，不因生成一次顺滑回复而清零。
4. **有行动代价**：消息、等待、失约、媒体、工具和主动联系都经过时间、预算、回执与失败，不是模型说了就算发生。
5. **有有限理性**：角色可以在软性选择上偏离最优解，但偏离有来源、后果和恢复；不会为了显得随机而破坏事实和能力边界。

因此，世界机应提供“可持续的内心与现实材料”，而不是替角色作出社会决定。任何机制如果只能改变 prompt 字眼、不能在未来造成状态或行为差异，就不是世界机制；任何机制如果直接规定某类话术，又没有给模型权衡空间，就是规则脚本。

### 2.6 复杂性必须换来可见收益

每增加一个状态、分类器或模型调用，都必须回答：它改变哪一个未来决策？通过什么事件留下后果？在哪个 trace 和测试中能观察？如果答案只是“也许让模型更懂”，则先作为可丢弃 advisory 实验，不获得持久化权威。

世界机对裸聊的优势只允许来自裸聊难以稳定做到的能力：共时生活、事实与外部行动、长期余波、可兑现承诺、可中断多段表达、NPC/世界因果、媒体事件与跨轮私密内心。普通闲聊的语言能力至少不得低于裸聊基线。

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
| `MatrixCatalog` | `lookup / validate_schema` | 版本化分类词表、组合约束、schema 校验 | 不分类具体事件、不做行为裁决 |
| `AdvisoryCompiler` | `compile(snapshot, trigger)` | 并行调用轻量语义/情绪/线程 classifier，产可拒绝候选 | 不写投影、不阻断主生成 |
| `VariabilitySampler` | `draw(context)` | 记录抽样、变化空间、偏离压力 | 不绕过硬约束 |
| `Deliberation` | `propose(capsule, draw)` | 调用 LLM 生成 `DecisionProposal v2` | 不直接写世界事实 |
| `ProposalAcceptance` | `accept(proposal, snapshot)` | 最小硬校验、预算保留、Action 授权 | 不评价“够不够会聊天” |
| `ActionExecutor` | `dispatch / lookup_result` | 执行一个已 claim 的不可变 Action，返回 ProviderReceipt/DispatchPending | 不 claim、不 settle ledger、不创造事实、不回调 Runtime |
| `MediaExecutionAdapter` | `plan / render / inspect / repair_once` | 对接现有图片机 public seam；repair_once 只接冻结计划/失败 artifact/inspection | 不替世界选择事件或改世界事实 |
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
- effect-once：同一 Observation 对应唯一 TriggerProcess；重复/并发 ingest join 已有处理结果，不启动第二次 Deliberation。
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
- unknown 永不重试原 Action；仅允许 reconciliation/manual review。
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
appraisals[] {change_ref, summary}
affect_tendencies[]
drives[≤3]
conflicts[]
activity_transition {change_ref, summary}
behavior_tendency
variation_profile {deviation_kind, deviation_intensity, change_phase,
                   sampling_mode, recovery_posture}
stance
display_strategy
conversation_thread_changes[] {change_ref, summary}
action_intents[] {intent_id, kind, layer, target, payload_ref, payload_hash,
                  causal_change_id?, beat_ref?, dependencies[], due_window}
proposed_changes[] {
  change_id, kind, payload, evidence_refs[], preconditions[],
  policy_refs[], expected_entity_revision?, lifecycle_transition?
}
confidence
brief_rationale
```

约束：

- `brief_rationale` 最多 240 字，只记录可审计摘要，不保存自由思维链。
- `activity_transition` 先成为 Plan；完成、失败、中断或放弃必须由后续事件结算。
- `behavior_tendency` 是本轮最终选择，不要求采纳 advisor 候选。
- `deviation_kind` 使用 5.7 的偏离类型，`change_phase` 使用 5.10 的时间阶段；二者正交，不互相替代。`preference_shift` 是偏离种类，`preference_deviation` 是所处阶段。
- `action_intents` 是带稳定 `intent_id` 的候选值对象，不是 ledger Action；只有 `ProposalAcceptance` 接受后才能创建 authorized Action。
- `proposed_changes[]` 是所有持久内部/世界变化的唯一 typed mutation 集合；`activity_transition`、`appraisals`、`conversation_thread_changes` 是给模型可读的摘要视图，提交时必须一一引用对应 `change_id`，不能形成第二条写路径。
- 首发 `change.kind` 必须来自 4.1 的封闭 discriminated registry。摘要字段必须携带 `change_ref`；不存在对应 typed change 的摘要只用于自然语言解释，不能提交任何状态。
- `appraisals` 可包含多个替代解释；低置信心理推断只能成为 `PrivateImpression`，不能成为 User Fact。
- `evidence_refs` 必须指向 committed fact、committed experience、committed/settled world event（含 NPC occurrence）、observed message、settled external result 或 active plan。

### 4.1 Typed Change Registry

`proposed_changes[]` 使用 discriminated union。公共字段为 `change_id`、`kind`、`target_id`、`expected_entity_revision?`、`transition`、`evidence_refs[]`、`preconditions[]`、`policy_refs[]`、`payload`。Acceptance 按 kind 分派；未知 kind、非法 predecessor 或 payload version 一律 `schema_invalid`，不得塞入 `accepted_changes` 自由对象。

| `kind` | payload 必需字段 | transition | 接受后事件 |
|---|---|---|---|
| `fact_transition` | fact type、subject、value、confidence、supersedes? | commit/correct | `FactCommitted/FactCorrected` |
| `experience_transition` | participants、time range、occurrence/result refs、summary ref | commit | `ExperienceCommitted` |
| `character_core_revision` | prior revision、changed fields、evidence window | revise | `CharacterCoreRevised` |
| `goal_transition` | goal ID、importance、progress、due、blockers | open/progress/complete/abandon | `Goal*` |
| `resource_transition` | resource kind、before/after fixed-point、cause | adjust | `ResourceStateChanged` |
| `attention_transition` | before/after、cause、expiry | adjust | `AttentionChanged` |
| `activity_transition` | activity ID、plan ref、phase、participants、location | plan/start/pause/resume/complete/abandon | `Activity*` |
| `location_transition` | from/to location、visibility、cause | change | `LocationChanged` |
| `world_occurrence_transition` | occurrence ID、participants、location、window、preconditions | commit/cancel/expire | `WorldOccurrence*` |
| `social_encounter_transition` | encounter ID、participants、location、visibility | start/end | `SocialEncounter*` |
| `outcome_settlement` | outcome proposal ID、result ID、entity ID/revision、observations、result payload | settle | `WorldOccurrenceSettled/SocialEncounterEnded` + optional `ExperienceCommitted` |
| `npc_relationship_adjustment` | npc ID、variable deltas、policy version、cause | adjust | `NpcRelationshipAdjusted` |
| `appraisal_transition` | appraisal ID、meaning candidates、attribution、severity、confidence、expiry | activate/contradict/expire/supersede | `AppraisalAccepted`/lifecycle event |
| `affect_transition` | episode ID、appraisal change refs、component deltas、decay/residue config | open/update/resolve/supersede | `AffectEpisode*` |
| `private_impression_transition` | impression ID、interpretations、confidence、expiry/contradiction | open/support/contradict/expire/revise | `PrivateImpression*` |
| `relationship_adjustment` | relationship ID、variable deltas、policy version、contradiction group | adjust/compensate | `RelationshipSlowVariableAdjusted` |
| `boundary_transition` | boundary ID、scope、strength、expiry? | open/revise/close | `BoundaryChanged` |
| `thread_transition` | thread ID、kind、importance、due、resolution ref? | open/update/resolve/cancel/expire | `Thread*` |
| `interaction_bid_transition` | bid ID、goal、hoped response、pressure、audience、due | open/update/resolve/withdraw/expire | `InteractionBid*/WaitingStageChanged` |
| `commitment_transition` | commitment ID、content ref、importance、due、persistence | open/due/fulfill/break/release | `PrivateCommitment*` |
| `memory_candidate_transition` | candidate ID、source fact/experience/thread refs、retention rationale | open/accept/reject/revise | `MemoryCandidate*/MemorySummaryRevised` |
| `expression_plan_transition` | plan ID、overall intent、`beat_drafts[]` | accept/reconsider/cancel | `ExpressionPlanAccepted/ExpressionBeat*` |
| `photo_candidate_transition` | candidate ID、event refs、family、privacy ceiling | open/select/skip/expire | `PhotoCandidate*/MediaOpportunityFrozen` |
| `media_continuation` | workflow step ID、opportunity/plan/artifact refs、next action payload hash | plan_to_render/render_to_inspect/inspect_to_delivery | next media Action authorization |
| `media_repair_transition` | repair attempt ID、plan/artifact/inspection refs、defect scope | authorize/abandon | `MediaRepairAuthorized/Attempted` |
| `grant_request` | grant kind、actor、scope、constraints、expiry | request/grant/revoke | `Capability*/Consent*/PrivacyPolicyRevised`（仅具授权主体时接受） |

`expression_plan_transition.payload.beat_drafts[]` 固定包含：`beat_id`、inline encrypted payload 或 immutable payload ref、payload hash、content type、dependency beat IDs、delay window、cancel/reconsider/merge policy。引用 Beat 的 ActionIntent 必须带 `beat_ref` 和相同 payload hash。Acceptance 在同一 UoW 创建 ExpressionPlan、Beat events、预算 reservation 与 Actions；任一不一致整组拒绝。

Appraisal 与 Affect 不双写：`appraisal_transition` 只提交“事件意味着什么”，绝不携带情绪数值 delta；`affect_transition` 独立携带 component delta，并用 `appraisal change refs` 建立因果。Acceptance 可在同一 UoW 先提交 Appraisal 再提交 Affect，但只有 Affect reducer 改 AffectProjection。

除 LLM `DecisionProposal` 外，机械工作流可生成同一 `ProposalEnvelope` 的 `ContinuationProposal` subtype。它只能使用 registry 中的 continuation kind 和有来源的 ActionIntent，仍经过同一 ProposalAcceptance、预算、grant 与 CAS；它不获得第二条写 seam。

```text
ProposalEnvelope
  proposal_id, proposal_kind=decision|continuation|minimal
  trigger_ref, evaluated_world_revision, schema_registry_version
  evidence_refs[], proposed_changes[], action_intents[]
  confidence, brief_rationale

ContinuationProposal
  envelope fields
  workflow_kind, upstream_result_refs[], continuation_step
  (不得包含自由 appraisals/drives/personality changes)
```

## 4A. 唯一权威闭环与提交边界

以下是 v2 唯一合法的运行闭环。任何模块不得跳步写入下游状态：

```text
Observation / ClockObservation / ExternalObservation
  │  [WorldLedger: observation commit]
  ▼
SituationCompiler ───────────────┐
AdvisoryCompiler (parallel) ─────┼─> frozen ContextCapsule
  │                              │   ├─ authoritative slices
  │                              │   └─ rejectable advisories
  ▼
MatrixCatalog + VariabilitySampler
  │  [RandomDrawRecorded, draw 可被拒绝但不可重抽]
  ▼
Deliberation ──> DecisionProposal
  │  [ModelResultRecorded; proposal 仍非事实]
  ▼
ProposalAcceptance
  ├─ reject / weaken / authorize
  ├─ commit internal/world events
  └─ reserve budget + create Action
          │
          ▼
ActionExecutor / MediaExecutionAdapter
          │
          ▼
ExternalObservation / Receipt
          │  [settlement commit]
          ▼
Reducers ──> WorldProjection ──> 下一次 ContextCapsule
```

提交规则：

1. Observation 必须先提交，模型失败不能抹掉用户输入。
2. Context、分类器、情绪建议、随机 draw 和 LLM 都只产生候选解释；候选可被主模型忽略或改写。
3. ProposalAcceptance 是唯一从“建议”进入“世界状态或 Action”的 seam。
4. 模型审计提交与领域接受提交是两个边界：A 事务按 `model_call_id/proposal_id` 幂等提交 `ModelResultRecorded + ProposalRecorded`，无论后续接受或拒绝都保留；B 事务按 `proposal_id + evaluated_world_revision` 幂等并以 world revision 做 CAS，原子提交 `AcceptanceRecorded`、被接受的内部事件、预算保留与已授权 Action。拒绝也提交 `AcceptanceRecorded(status=rejected)`，但无领域变化；stale/CAS 失败不回滚 A，并以新 Capsule 重新 Deliberation，而非直接复用旧 Proposal。
5. 外部副作用只能由 ActionExecutor 发起；其结果只能通过 `settle()` 回到账本。
6. Reducer 是投影的唯一写入者；业务模块不得直接改投影表。
7. 回放读取已记录的模型结果、draw、MediaPlan 和 receipt，不重新调用模型或外部系统。

Revision 分层，禁止用单一自增数同时承担所有并发语义：

| Revision | 变化来源 | 用途 |
|---|---|---|
| `ledger_sequence` | 每个已提交事件 | 全局顺序、审计、checkpoint |
| `world_revision` | 会改变事实、生活、心理、关系、Action、预算或授权的领域事件 | Capsule/Acceptance CAS、World projection hash |
| `deliberation_revision` | draw、model result、proposal audit、frequency budget | 回放与抽样并发，不使本轮 world CAS 自失效 |
| `schema_revision` | schema/catalog/reducer/policy 发布 | 版本兼容，不随每次运行变化 |

VariabilitySampler 在自己的 deliberation stream 上 CAS 提交 draw 与 frequency budget；Model/Proposal audit 同样只推进 deliberation revision。它们不会改变 Capsule 的 `world_revision`。其他 turn、clock、settlement 或 budget/grant 领域提交会推进 world revision，并使旧 Proposal stale。

事件的 revision class 由 schema metadata 固定：用户/platform `ObservationRecorded` 与 `ClockAdvanced` 推进 world revision；全部 TriggerProcess lifecycle（open/claim/attempt/supersede/complete/expire）、draw、model/proposal audit、未处理的 `ExternalObservationRecorded` 只推进 deliberation revision；Acceptance B、Action/预算状态和 settlement B 推进 world revision。禁止同一事件在不同 reducer bundle 中改变 revision class。

### 4A.1 权威矩阵

| 信息 | 唯一权威 | 可提出候选者 | 禁止行为 |
|---|---|---|---|
| 用户说过什么 | Observation event | 平台 Adapter | 将推断写成 User Fact |
| 角色经历过什么 | committed experience / settled result | Deliberation | 用计划、prompt 或 proposal 冒充经历 |
| 当前生活活动 | Plan + activity events reducer | LLM、scheduler | LLM 一句话宣布活动完成 |
| 角色稳定人格 | Character Core revision | operator/受控长期演化 | 单轮任意改写人格 |
| 情绪 episode | accepted appraisal/affect events | affect advisor、LLM | advisor 直接改情绪投影 |
| 用户心理印象 | Private Impression | LLM、appraiser | 升级为 User Fact 或永久标签 |
| 关系慢变量 | relationship events reducer | Deliberation | 用关系阶段授权越界措辞 |
| 外部动作状态 | authorized Action + receipt ledger | Deliberation 提 `ActionIntent` 值对象 | 将 intent 当 Action；未执行便声称已完成 |
| 媒体事实 | frozen opportunity + MediaPlan + inspection + receipt | World/图片机各自负责一段 | 图片反写世界或外观事实 |
| 随机结果 | RandomDrawRecorded | VariabilitySampler | retry 直到抽到喜欢结果 |
| 投影 | deterministic reducers | 无 | dashboard/Adapter 直接写投影 |

### 4A.2 事实升级矩阵

`evidence_refs[]` 必须带 `claim_purpose`，避免 active plan 被错误用于支持“已发生”。

| Evidence Status | 描述当前事实 | 描述过去经历 | 描述未来计划 | 私下假设 | 授权 Action |
|---|---:|---:|---:|---:|---:|
| `committed_fact` | 允许 | 有时间范围时允许 | 不单独支持 | 可参考 | 可参考 |
| `committed_experience` | 允许 | 允许 | 不支持 | 可参考 | 可参考 |
| `committed_or_settled_world_event` | 在事件有效期内允许 | settled 时允许 | 不支持 | 可参考 | 只支持对应 continuation |
| `settled_external_result` | 允许 | 允许 | 不支持 | 可参考 | 支持对应结果 |
| `observed_message` | 仅支持“用户说过” | 仅支持“用户说过” | 可支持用户表达的计划 | 允许建立可过期印象 | 不直接授权 |
| `active_plan` | 不支持已完成 | 不支持已发生 | 允许 | 可参考 | 仅支持创建到期任务 |
| `proposal` | 不允许 | 不允许 | 只可表述为角色正在考虑 | 允许但不提交为事实 | 不直接授权 |
| `private_impression` / `hypothesis` | 不允许 | 不允许 | 不允许 | 允许，必须标明置信度 | 不直接授权 |
| `media_prompt` / uninspected artifact | 不允许 | 不允许 | 不允许 | 不允许 | 不支持发送 |

计划、猜测、模型补全和图片 prompt 永远不能自行升级为“已发生”。纠错通过补偿事件完成，不修改历史事件。

## 4B. 核心领域对象与 Schema 合同

Schema 分四类，避免给查询和值对象伪造时间：

| 类型 | 公共 Envelope |
|---|---|
| Event / Command / Action / External Result | `schema_version`、稳定 ID、`world_id`、`logical_time`、`created_at`、`trace_id`、`causation_id`、`correlation_id` |
| Persisted Entity / Snapshot | `schema_version`、稳定 ID、`world_id`、`revision`、`updated_at` |
| Query / Request | `schema_version`、request ID、viewer/authority、可选 revision、trace ID |
| Value Object | 只要求 schema version 和所属对象内稳定语义，不要求独立 ID/时间 |

下面列出业务必需字段；实现可添加存储元数据，但不得改变语义。

### 4B.1 输入对象

```text
Observation
  observation_id, source, source_event_id, actor, channel
  payload_ref, payload_hash, received_at, logical_time
  reply_context, attachment_refs[], coalescing_metadata

ClockObservation
  tick_id, logical_time_from, logical_time_to, reason

ExternalObservation
  result_id, kind=provider_ack|execution_receipt|tool_result|media_result|
                  reconciliation_result
  source, source_event_id, action_id, idempotency_key, status
  provider_ref, artifact_refs[], cost_actual, observed_at
  error_class?, retryability?, raw_payload_hash

ProjectionRequest
  viewer_kind, viewer_id, permissions[], at_world_revision?
  include_debug_refs, redaction_policy

ReplayMode
  world_id, from_revision, to_revision?, expected_hash?
  model_result_policy=recorded_only
  random_policy=recorded_only
  side_effect_policy=forbidden

RuntimeOutcome
  outcome_id, observation_ref?, committed_revision
  status, authorized_action_ids[], scheduled_action_ids[]
  deferred_refs[], terminal_errors[], projection_hint
```

`payload_ref` 指向受权限保护的原文；ledger 中保存不可变 hash 和必要摘要。附件、平台原始事件和模型原始输出不得无限复制进 Context。

```text
TriggerProcess
  trigger_id, trigger_ref, process_kind
  state, claim_lease?, attempt_ids[]
  active_proposal_id?, acceptance_id?, action_ids[]
  runtime_outcome_ref?, superseded_by?

state = observed → claimed → deliberating → accepted_or_deferred
        → actions_pending → terminal
        claimed/deliberating → superseded | expired
```

`trigger_id = hash(world_id, trigger_ref, process_kind)`。`ingest/advance/settle` 先幂等取得 TriggerProcess claim；同一 trigger 的并发调用 join/返回已有 RuntimeOutcome，不各自 Deliberate。claim owner 因其他领域事件 CAS stale 时，可以在同一 trigger/attempt lineage 内重新编译，但旧 proposal 标记 superseded；一旦已有 Acceptance/authorized Action，禁止为同一 trigger 再创建第二组 intent，除非新 Observation 或显式 recovery trigger。

### 4B.2 `ContextCapsule`

```text
ContextCapsule
  capsule_id, trigger_ref, world_revision, deliberation_revision, logical_time
  character_core
  current_situation {location, activity, activity_phase, attention,
                     energy, current_goal, unfinished_commitments[]}
  relationship_slice
  affect_episodes[]
  open_threads[]
  relevant_facts[]
  recent_experiences[]
  available_capabilities[]
  action_budget
  private_impressions[]
  advisories[]
  token_budget {hard_max, used_by_slice, truncation_log}
```

字段来源与失效：

| Slice | 来源 | 排序/裁剪 | 失效方式 |
|---|---|---|---|
| Character Core | versioned core | 永不被近期聊天挤出 | 新 revision 替换 |
| Situation | 当前投影+逻辑时间+已提交活动 | 固定字段 | activity/location 事件更新 |
| Affect | open episodes | 强度×新近性×关系相关 | decay/resolve/supersede |
| Threads | open thread projection | due、关系成本、用户相关 | answered/cancelled/expired |
| Facts | committed facts | relevance+recency+confidence | corrected/superseded |
| Experiences | committed experiences | relevance+recency | 摘要降级，不删除权威事件 |
| Impressions | source-bound hypotheses | confidence+expiry | contradicted/expired/revised |
| Advisories | 本轮并行生成 | 每类限额；超时即省略 | 本轮结束即失效 |

普通热对话的 Capsule 必须是增量投影构建，禁止每轮全账本 replay。默认上下文预算由配置给出，并在 trace 中记录每个 slice 的 token 占比和被裁剪原因。

### 4B.3 Advisory 与心理对象

```text
InnerAdvisory
  advisory_id, kind, source_refs[], candidates[]
  confidence, expiry, model_or_rule_version

PrivateImpression
  impression_id, subject, interpretations[]
  source_refs[], confidence, first_seen, last_supported
  expiry_condition, contradiction_refs[], status

AffectEpisode
  episode_id, dimensions{}, source_refs[]
  appraisal_refs[], intensity, baseline_offset
  opened_at, decay_profile, expression_history[], status

ConversationThread
  thread_id, kind, opened_by, source_refs[]
  importance, due_window, expected_response?, status, resolution_ref?

PrivateCommitment
  commitment_id, content, source_refs[]
  importance, due_window, persistence_level, status

MessagePayload
  payload_id, content_type=text|reaction|sticker|typing
  encrypted_content_ref, content_hash, encoding
  source_proposal_id, redaction_class

ExpressionPlan
  expression_plan_id, trigger_ref, proposal_id
  beat_ids[], ordering, overall_intent, created_from_text_hash

ExpressionBeat
  beat_id, expression_plan_id, ordinal_at_creation
  payload_ref, payload_hash, content_type
  dependency_beat_ids[], delay_window
  cancel_policy, reconsider_policy, merge_policy
  state, supersedes_beat_ids[]
```

所有 advisory 都是可拒绝候选，不是裁决。`PrivateImpression` 必须允许多个互相竞争的解释，并具有过期、反证和修订生命周期。表面回复“没事”不能自动关闭 AffectEpisode；是否缓解必须由后续评价或时间衰减事件决定。

ExpressionPlan 接受时冻结每个 Beat 的 payload/hash；每个 Beat 的 Action `payload_ref` 必须指向该冻结内容。用户插话后，已 delivered Beat 永不改写；未发送 Beat 可 cancel，或由新 Proposal 创建新 beat ID 并以 `supersedes_beat_ids`/`merge_policy` 关联旧 Beat。禁止原地修改 payload，也禁止用旧 Action ID 发送重生成内容。

所有 `reply/followup/proactive_message`，包括普通单段回复，都必须由 `expression_plan_transition` 提供一个或多个 Beat draft；单段回复就是单 Beat，不存在绕过 ExpressionPlan 的自由文本 Action。Acceptance 在同一 UoW 将 draft materialize 为不可变 MessagePayload、Beat 与 Action，并核对三者 hash。MinimalProposal 的 `response_text` 同样先转换为单 Beat draft，再授权 reply Action。

### 4B.4 接受、Action 与回执

```text
AcceptanceResult
  acceptance_id, proposal_id, evaluated_world_revision
  accepted_changes[] {change_id, committed_event_ids[]}
  weakened_claims[], rejected_items[] {item_id, reason_code}
  hard_invariant_codes[], budget_reservations[]
  authorized_action_ids[], committed_world_revision, ledger_sequence

Action
  action_id, kind, layer, intent_ref, actor, target
  payload_ref, payload_hash
  idempotency_key, not_before, expires_at, dependencies[]
  budget_reservation_id, claim_lease?, state, recovery_policy

ProviderReceipt
  provider_receipt_id, receipt_kind=ack|terminal
  action_id, idempotency_key, provider, provider_ref
  observed_state=provider_accepted|delivered|failed|unknown
  artifact_refs[], cost_actual?, received_at, raw_payload_hash

DispatchPending
  action_id, idempotency_key, provider
  provider_ref?, lookup_after, deadline, dispatch_started_at
  idempotency_mode=effect_once|result_lookup|none

ExecutionReceipt
  receipt_id, action_id, provider, provider_ref
  receipt_kind, observed_state, is_terminal
  artifact_refs[], cost_actual
  error_class?, received_at, raw_payload_hash
```

Adapter 原始返回统一为 `ProviderReceipt`；Runtime-owned ActionPump（或 webhook inbound Adapter）校验 `action_id/idempotency_key/provider_ref` 后包装成 `ExternalObservation(kind=execution_receipt)` 交给统一 settlement handler。`ExecutionReceipt` 是 settlement 接受后入账的领域记录。receipt 以 `provider + provider_ref + payload_hash` 去重；乱序 receipt 先入 settlement inbox，只有满足合法状态前置条件才 reduce，冲突终态进入 reconciliation，不能覆盖历史。

统一身份为：Adapter 将 `source=provider`、`source_event_id=provider_ref`（无稳定 ref 时用 provider 签名 payload hash）写入 ExternalObservation；Runtime 的 observation 幂等键始终是 `source + source_event_id`，`provider + provider_ref + payload_hash` 仅用于检测 provider ref 被异常复用而内容冲突，不是第三套主键。

settlement 分两个边界：A 幂等提交 `ExternalObservationRecorded` 到 inbox；B 由唯一 TriggerProcess 在同一 Ledger Unit of Work 中原子提交 `ExternalObservationProcessed`、Action 状态/ExecutionReceipt、BudgetSettled/Released、对应 Tool/Vision/Media domain result、必要 conversation thread/Bid 变化与 TriggerProcessCompleted。B crash 前全部不生效，crash 后重复 settle 返回既有结果；任何派生失败都不得留下“Action 已终态但预算/结果未结算”的半状态。

对 `ToolResultAccepted`、`VisionResultAccepted`、`TranscriptionResultAccepted`，settlement B 同一 UoW 还必须打开 deterministic `external_result_deliberation` TriggerProcess：`trigger_id = hash(world_id, action_id, accepted_result_id, result_kind)`。该 trigger 使用新的 DecisionProposal，让模型基于有界 External Result 决定回复、记忆候选或 no-visible-action；重复 receipt/crash 只 join 同一 process。媒体结果不走此通用 trigger，继续使用 6.5 的 media continuation state machine，避免双触发。

Action 执行前必须 reserve；进入终态必须 settle 或 release。`unknown` 永不重试原 Action；明确回执或人工复核只能追加 reconciliation，不能让原 Action 重新执行。成本类别固定为：`chat`、`repair`、`audit`、`proactive`、`vision`、`audio`、`image`、`tool`。

硬授权不是散落布尔判断：

```text
CapabilityGrant
  grant_id, capability_kind, actor, target_scope
  constraints{}, valid_from, expires_at?, revoked_by?

ConsentGrant
  consent_id, grantor, grantee, action_scope, data_scope
  channel_scope, valid_from, expires_at?, revocable, status

PrivacyPolicy
  policy_revision, subject, data_classes{}
  viewer_rules{}, media_rules{}, retention_rules{}
```

Acceptance 从对应投影读取 grant/policy，并把使用的 revision 写入 AcceptanceResult。关系阶段、用户请求或模型自信都不能代替 ConsentGrant/CapabilityGrant；过期或撤销通过事件生效并可回放。

Action 在 Acceptance 前不存在；Proposal 中只有 `ActionIntent` 值对象。合法状态为：

```text
authorized → scheduled → claimed → dispatch_started → provider_accepted
     │            │          │             │               ├→ delivered
     └────────────┴───────────┴─────────────┴───────────────├→ failed
                                                           └→ unknown
authorized / scheduled / claimed → cancelled | expired
dispatch_started → unknown  (provider 无法按 key 查询时的 crash/timeout)
```

`dispatch_started` 必须在网络调用前提交。Provider Adapter 声明 `idempotency_mode = effect_once | result_lookup | none`：前两者允许用同一 key 恢复查询/调用；`none` 只能 at-most-once，一旦 dispatch_started 后崩溃/timeout 就进入 unknown，禁止重发。自动消息/媒体/工具优先要求 effect-once 或 result lookup；不满足者必须显式接受 at-most-once 丢失风险或禁用自动能力。

`provider_accepted` 表示外部 provider 已确认接收但尚无业务终态。`unknown` 是原 Action 的不可逆终态：只能进入 reconciliation/manual review，原 Action 永不重新执行。后到回执通过 `ActionReconciliationRecorded` 建立 delivered/failed 外部事实与预算补偿，不改写原事件；需要再次执行必须由人工或新 Deliberation 创建新 intent/new Action ID。

### 4B.5 `WorldProjection` 与可见权限

```text
WorldProjection
  world_id, world_revision, ledger_sequence, logical_time
  character_public, current_situation, relationship_public
  affect_summary, open_threads_summary, plans, recent_experiences
  pending_actions, media_candidates, system_health
```

| Viewer | 可以看 | 不可以看 |
|---|---|---|
| Platform Adapter | 待发送 Action、必要投递 metadata | 私密印象、自由内部摘要 |
| Dashboard operator | 诊断投影、事件引用、预算、健康状态 | 默认不显示原始私密内容 |
| Godot/小屋 | 可见位置、活动、外显状态 | 私密心理、ledger 内部结构 |
| Deliberation | 有界 ContextCapsule | 全库、未授权平台秘密 |
| Evaluator | 脱敏 replay 与 trace | 外部副作用权限 |

SituationCompiler 不使用上述 viewer-facing summary，而通过内部只读 Interface 获取完整权威快照：

```text
InternalProjectionReader.snapshot(world_id, revision?) -> InternalWorldSnapshot

InternalWorldSnapshot
  world_id, world_revision, ledger_sequence, logical_time
  character_core, facts[], experiences[]
  current_situation, plans[], commitments[]
  affect_episodes[], private_impressions[]
  relationship_state, conversation_threads[]
  capabilities[], budgets, pending_actions[]
  media_candidates[], reducer_versions{}
```

`InternalWorldSnapshot` 是 Reducer 生成的只读深投影，不是第二套真相；只有 WorldRuntime 内部的 SituationCompiler 可获得。外部 viewer 永远走 `project(ProjectionRequest)` 的裁剪/脱敏结果。

实现约束进一步收紧为两个用途不同的内部读取面：`InternalProjectionReader.snapshot(cursor?)` 只给 SituationCompiler 生成有界、可降级的 `situation_context`；每个可变长 slice 必须携带 availability、窗口和截断信息，未实现 reducer 时必须显式 `unavailable`，不能以空数组冒充“真实为空”。Acceptance 与 recovery 不得消费该有界快照，而使用 `InternalAuthorityReader` 的 revision-pinned 按 ID 查询和稳定分页（Action、预算、回执等）；分页必须遍历到 `complete=true` 或 fail closed，不能因 Context 上限漏掉旧 Action。两者读取同一 Reducer projection，不形成第二套写权威。

### 4B.6 NPC 与自主世界对象

```text
NpcRef
  npc_id, stable_identity_ref, relationship_to_character
  known_traits[], privacy_class, last_committed_location?

WorldOccurrenceProposal
  occurrence_id, trigger_ref, participant_refs[]
  location_ref, time_window, preconditions[]
  candidate_outcomes[], evidence_refs[], confidence

SocialEncounter
  encounter_id, participant_refs[], location_ref
  started_at, ended_at?, settled_outcome_refs[]
  visibility, experience_refs[]

OutcomeObservation
  observation_id, occurrence_or_encounter_id, source_kind
  source_refs[], observed_payload, observed_at, confidence

OutcomeProposal
  outcome_proposal_id, decision_proposal_id, change_id
  result_id, occurrence_or_encounter_id, expected_entity_revision, trigger_ref
  candidate_result, observation_refs[], preconditions[]
  evidence_refs[], confidence, expires_at
```

自主世界由 `advance()` 在计划到期、时钟推进或已承诺社交事件触发时请求 Deliberation。LLM 可提出偶发事件或 NPC 行为，但只有满足参与者、地点、时间、前置计划/机会和能力证据的 occurrence 才可接受。生命周期固定为：

```text
proposed
→ committed/scheduled
→ active
→ outcome_observed | outcome_due
→ OutcomeProposal
→ Acceptance
→ settled | cancelled | expired
→ optional ExperienceCommitted (同一 settlement UoW)
```

`OutcomeObservation.source_kind` 只允许：settled external result、clock/plan precondition、operator observation、已提交 NPC/world event；LLM 自身文本不是 observation。到期而无外部观察时，`advance()` 可基于已提交处境请求新的 OutcomeProposal，但 Proposal 仍需列出可验证 preconditions，不能把随机候选直接升级事实。

`active` 不是按当前时间临时推导：`advance()` 检查已提交 start preconditions 后显式写 `WorldOccurrenceActivated`；只有该事件后才允许 outcome observation/settlement。未满足条件则保持 committed/scheduled，过期写 Expired。

settlement 事务按 `outcome_id + entity_revision` 幂等，原子提交 Outcome accepted、`WorldOccurrenceSettled`/`SocialEncounterEnded`、必要 activity/location/NPC relationship events 与可选 `ExperienceCommitted`。取消、超时分别写 `WorldOccurrenceCancelled/Expired`；不能在首次 occurrence Proposal 中同时“计划、发生、完成”。

settlement 同一 UoW 必须打开 `npc_world_appraisal` continuation TriggerProcess（即使不创建 Experience，也可引用 settled world event）。该 trigger 进入 DecisionProposal；模型可以提出 appraisal/affect，也可以明确 `no_appraisal_change` 并完成 process。这样每个 NPC/world outcome 都有可审计的心理消费机会，但不强制它必须改变情绪。

NPC 有独立身份引用和与角色的关系投影，不读取用户关系阶段作为 NPC 关系。参与者可见性和隐私决定能否进入 life share/media；NPC 事件通过 committed experience → appraisal → AffectEpisode → 下一轮 Capsule 影响角色，而不是直接塞一段“她今天心情不好”的 prompt 文本。

## 4C. 事件目录与 Reducer 映射

事件是不可变事实；每个事件声明唯一 reducer 集合。以下为首发必需目录：

| 事件族 | 事件 | 主要 Reducer / 结果 |
|---|---|---|
| 输入 | `ObservationRecorded`、`ClockAdvanced`、`ExternalObservationRecorded`、`ExternalObservationProcessed` | inbox、logical time、settlement inbox |
| Trigger | `TriggerProcessOpened`、`TriggerProcessClaimed`、`TriggerAttemptSuperseded`、`TriggerProcessCompleted`、`TriggerProcessExpired` | effect-once turn/advance/settlement processing |
| 模型 | `ModelResultRecorded`、`ProposalRecorded` | replay index、proposal audit |
| 随机 | `RandomDrawRecorded`、`RandomDrawSuperseded` | draw history、frequency budget |
| 人格 | `CharacterCoreRevised` | character core revision |
| 目标/资源 | `GoalOpened`、`GoalProgressed`、`GoalCompleted`、`GoalAbandoned`、`ResourceStateChanged`、`AttentionChanged` | goals、energy/needs、attention |
| 活动 | `ActivityPlanned`、`ActivityStarted`、`ActivityPaused`、`ActivityResumed`、`ActivityCompleted`、`ActivityAbandoned` | current situation、experience candidates |
| 位置 | `LocationChanged` | current location、可见性 |
| NPC/世界 | `WorldOccurrenceCommitted`、`WorldOccurrenceActivated`、`OutcomeObservationRecorded`、`OutcomeProposalRecorded`、`WorldOccurrenceSettled`、`WorldOccurrenceCancelled`、`WorldOccurrenceExpired`、`SocialEncounterStarted`、`SocialEncounterEnded`、`NpcRelationshipAdjusted` | world occurrence、NPC relationship、experience |
| 情绪 | `AppraisalAccepted`、`AppraisalContradicted`、`AppraisalExpired`、`AppraisalSuperseded`、`AffectEpisodeOpened`、`AffectEpisodeUpdated`、`AffectEpisodeDecayed`、`AffectEpisodeResolved`、`AffectEpisodeSuperseded` | appraisal/affect projection |
| 印象 | `PrivateImpressionOpened`、`Supported`、`Contradicted`、`Expired`、`Revised` | impression projection |
| 关系 | `RelationshipSignalAccepted`、`RelationshipSlowVariableAdjusted`、`BoundaryChanged` | relationship projection |
| 线程 | `ThreadOpened`、`ThreadUpdated`、`ThreadResolved`、`ThreadCancelled`、`ThreadExpired` | conversation threads |
| 社交等待 | `InteractionBidOpened`、`InteractionBidUpdated`、`WaitingStageChanged`、`InteractionBidResolved`、`InteractionBidWithdrawn`、`InteractionBidExpired` | bid/waiting projection、relationship input |
| 承诺 | `PrivateCommitmentOpened`、`Due`、`Fulfilled`、`Broken`、`Released` | commitments、reliability signals |
| 表达 Beat | `ExpressionPlanAccepted`、`ExpressionBeatAuthorized`、`ExpressionBeatReconsidered`、`ExpressionBeatCancelled` | beat dependency/action schedule |
| 记忆 | `MemoryCandidateOpened`、`MemoryCandidateAccepted`、`MemoryCandidateRejected`、`MemorySummaryRevised` | retrieval index；事实仍由 fact/experience 权威提供 |
| 经历/事实 | `ExperienceCommitted`、`FactCommitted`、`FactCorrected` | experience/fact projections |
| Action | `ActionAuthorized`、`ActionScheduled`、`ActionClaimed`、`ActionDispatchStarted`、`ActionProviderAccepted`、`ActionSettled`、`ActionReconciliationRecorded` | action projection、budget、reconciliation overlay |
| Acceptance | `AcceptanceRecorded(status∈{accepted,rejected,stale})` | acceptance audit、accepted domain changes |
| 媒体 | `PhotoCandidateOpened`、`PhotoCandidateSelected`、`PhotoCandidateSkipped`、`MediaOpportunityFrozen`、`MediaPlanRecorded`、`MediaArtifactInspected`、`MediaCandidateExpired`、`MediaAutomaticDeliveryApproved` | media projection、automatic gate |
| 媒体失败/修复 | `MediaNotRenderableRecorded`、`MediaRenderFailed`、`MediaInspectionFailed`、`MediaRepairAuthorized`、`MediaRepairAttempted`、`MediaDeliveryFailed` | candidate/action terminal；最多一次 repair；fail closed |
| 工具/感知 | `ToolResultAccepted`、`VisionResultAccepted`、`TranscriptionResultAccepted` | sourced external-result projection |
| 授权 | `CapabilityGranted`、`CapabilityRevoked`、`ConsentGranted`、`ConsentRevoked`、`PrivacyPolicyRevised` | capability/consent/privacy projections |
| 预算 | `BudgetReserved`、`BudgetSettled`、`BudgetReleased` | budget projection |
| 恢复 | `RecoveryRequested`、`RecoveryReconsidered`、`RecoveryCompleted`、`RecoveryExpired`、`ManualReviewRequested` | recovery queue |

每个事件 Schema 都要定义：producer、前置 revision、幂等键、可引用 evidence、Reducer、允许的后继事件和补偿事件。CI 必须检测未知事件、无 reducer 事件、同一事实多 reducer 写入和非确定性 reducer。

### 4C.1 模型结果与回放

`ModelResultRecorded` 至少包含：

```text
model_call_id, request_hash, purpose, model_id, routing_tier
prompt_schema_version, capsule_hash, catalog_version
sampler_version, draw_refs[], model_parameters_hash
response_hash, parsed_payload, parse_status
latency, token_usage, cost, fallback_from?, error_class?
```

`request_hash = hash(purpose, capsule_hash, prompt_schema_version, catalog_version, sampler_version, draw_refs, model_id, routing_tier, model_parameters_hash)`，`model_call_id` 由 world ID + trigger + purpose + attempt identity 确定性派生。只有 request hash 完全一致才可复用记录结果；不同模型、route、draw 或参数不得误命中。缺失时返回结构化 replay gap，绝不静默调用线上模型。模型失败后产生的待重新审议项必须有 `trigger_ref`、`not_before`、`expires_at` 和幂等键；恢复后只重新审议仍新鲜且未被后续事件覆盖的项。

`RandomDrawRecorded` 至少包含：

```text
draw_id, trigger_ref, proposal_slot, seed
catalog_version, sampler_version
candidates[], weights[], selected_result
frequency_budget_before, frequency_budget_after
```

`draw_id = hash(world_id, trigger_attempt_id, capsule.world_revision, proposal_slot, catalog_version, sampler_version)`；seed 从 world seed + draw ID 确定性派生。抽样记录提交时即消耗 frequency budget，不以 LLM 是否采纳为条件，防止通过拒绝/重试刷结果。deliberation revision 不进入 draw identity，因此提交 draw 自身不会改变 draw ID。

同一 trigger attempt + world revision 的 Acceptance CAS 重试必须复用同一 draw。若 world revision 已变化导致 Proposal 失效，TriggerProcess 记录旧 attempt superseded 并创建新 attempt ID，随后记录 `RandomDrawSuperseded(old_draw_id, new_world_revision)`；旧 draw 仍计入历史频率，新 attempt 派生新 draw ID。crash recovery 和 replay 只查已记录结果，不重新抽样。

### 4C.2 事件族合同

同一族事件共享 envelope，但状态转移由下表冻结；具体 JSON Schema 在 Phase 1 按此生成，不能改变 authority。

| 事件族 | Producer | 幂等 identity | 前置条件 | Reducer 写入 | 合法后继 / 补偿 |
|---|---|---|---|---|---|
| Observation | Runtime ingest/settle | source+source_event_id | world exists | inbox/logical time | Proposal audit；补偿为新 observation |
| Trigger Process | Runtime | deterministic trigger ID | one active lease per trigger | process/attempt projection | complete/supersede/expire；duplicate joins |
| Model/Proposal audit | Deliberation Adapter | request_hash/proposal_id | frozen Capsule exists | audit index only | AcceptanceRecorded/Rejected；不可删除 |
| Acceptance | ProposalAcceptance | proposal_id+world revision | evaluated world revision match | accepted events + reservations + Actions | stale 时新 deliberation；拒绝仍留 audit |
| Character Core | authorized operator/long-term evolution | new core revision | evidence window + prior revision | core projection | later CharacterCoreRevised |
| Goal/Activity/Location | Acceptance/advance settlement | entity_id+transition_id | legal lifecycle + evidence | situation/experience | pause/resume/complete/abandon；错误用补偿 event |
| NPC/Occurrence | Acceptance/advance | occurrence/encounter ID | participant/location/time preconditions | world/NPC/experience | settle/end；取消/纠错事件 |
| Appraisal/Affect | Acceptance/advance decay | appraisal/episode+transition | sourced appraisal/config version | affect projection | update/decay/resolve/supersede |
| Impression/Relationship | Acceptance | impression/adjustment ID | source refs + policy version | private/relationship projections | support/contradict/expire/compensating delta |
| Thread/Commitment/Beat | Acceptance/advance/user interjection | entity+transition ID | lifecycle/dependency/due | open work + scheduler | resolve/cancel/expire/reconsider |
| Memory | background consolidator through Runtime | candidate+decision ID | source fact/experience exists | retrieval index only | accept/reject/revise；不改来源事实 |
| Random | VariabilitySampler through Runtime | deterministic draw ID | frozen capsule/catalog | draw/frequency | supersede only；不覆盖 |
| Action/Budget | Acceptance/Executor/settle | intent/action/receipt IDs | grant+budget+legal state | action/outbox/budget | terminal或reconciliation；unknown 不重执行 |
| Tool/Vision | Action settlement | action+result ID | matching authorized Action | sourced result projection | expire/correct；不直接写生活事实 |
| Media | Acceptance/Media Adapter/settle | candidate/opportunity/request/action IDs | frozen snapshot + approval/预算 | media/action projections | fail/expire/share；重试同 key |
| Recovery | Runtime advance/operator | recovery item+attempt ID | item fresh/not superseded | recovery queue | reconsidered/completed/expired |
| Grant/Policy | authorized operator/user consent flow | grant/policy revision | actor authority | grant/privacy projection | revoke/expire/revise |

所有补偿都创建新事件并引用被补偿事件；禁止 UPDATE/DELETE 历史。每个生成的 JSON Schema 必须包含 allowed predecessor、evidence type、reducer name 和 upcaster version 的机器可读 metadata，CI 据此生成机制闭环校验。

## 4D. 事务、并发、幂等和故障状态机

1. `WorldRuntime` 对单一 `world_id` 的领域提交使用 `world_revision` compare-and-swap；draw/audit 只推进 deliberation revision。领域冲突方重新投影和 Deliberation，不复用已失效的 Acceptance。
2. Observation 幂等键为 `source + source_event_id`；每个 ActionIntent 有由 Proposal 生成后保持不变的 `intent_id`，Action 幂等键由 `world_id + intent_id + action_kind` 派生，禁止使用会因排序变化的 ordinal。
3. Action claim 使用有限 lease；`scheduled/claimed` 可安全重新 claim。进入 `dispatch_started` 后，仅 effect-once/result-lookup provider 可用同 key 恢复；`none` provider 直接 unknown。已 provider-accepted 但无法取得终态的动作结算为 `unknown` 并进入对账，不盲重发；原 unknown Action 永不恢复执行。
4. Adapter timeout 的 retry ownership 属于 ActionExecutor policy；平台 Adapter 不得私自重试。
5. 模型、图片机和工具 Adapter 只返回结果，不提交领域事件。
6. schema、catalog、sampler、prompt、reducer 均版本化。Ledger 永久保留原始 event bytes/hash；当前 rebuild 通过纯函数 upcaster 链转换到目标 canonical schema，再使用指定 `reducer_bundle_version` 生成 hash。projection hash identity 必须包含 target schema/reducer bundle version。发布时归档 reducer bundle artifact 与旧 checkpoint hash；需要验证历史发布时加载该 artifact，日常当前投影不要求把旧 reducer 源码混入运行路径。
7. crash consistency 以“ledger commit 是真相”为准：副作用前必须存在 authorized/claimed Action；副作用后即使进程崩溃，也通过 provider idempotency key 或对账收敛。

Acceptance 使用稳定错误码：`unsupported_claim`、`stale_revision`、`schema_invalid`、`capability_denied`、`privacy_denied`、`consent_missing`、`budget_unavailable`、`action_duplicate`、`dependency_unsatisfied`、`expired_intent`。错误码描述硬事实，不能出现 `not_warm_enough`、`should_comfort` 一类审美裁决。

故障降级：

| 故障 | 继续做什么 | 禁止做什么 | 恢复 |
|---|---|---|---|
| Advisory timeout | 省略该 slice，继续主生成 | 使用脚本化固定反应 | trace 标记 |
| Projection slice 不可用 | 用 core+近期已投递文本自然回复 | 声称具体世界/Action 事实 | 后台重建投影 |
| 主模型 timeout | quick adapter 生成自然短回复或 defer Action | 固定客服模板 | 新鲜时重新审议 |
| Proposal parse fail | 保留自然文本，局部恢复结构壳 | 猜造 Action | 记录 parse error |
| 无来源 claim | 删除、弱化为不确定或询问 | 作为事实提交 | audit 记录 |
| Budget unavailable | 记录意图/延后 | 创建不可支付 Action | 到期重审 |
| Provider unknown | 原 Action 终态 unknown；对账/人工复核 | 自动重复副作用或重开原 Action | 补偿式 reconciliation 记录 |
| Media plan/NotRenderable | fail closed，候选终态 | 换事件重画或发送 | 显式新机会 |
| Media render fail | 同 key 查询/结算失败 | 偷换计划或无限重画 | 新机会；不自动 repair |
| Media inspection fail | 可选一次同计划 visual repair | 发送未验收图、换事件/构图、第二次 repair | repair 终态后结束 |

quick recovery 与 parse recovery 也必须产 `MinimalProposal`，不得让自然文本绕过 Action：

```text
MinimalProposal
  ProposalEnvelope common fields
  proposal_kind=minimal, source_model_result
  response_text
  stance = defer | acknowledge_briefly | answer_without_world_claims
  proposed_changes[] = [expression_plan_transition(single beat)] or []
  action_intents[] = [{intent_id, kind=reply|followup,
                       causal_change_id, beat_ref, payload_ref/hash, due_window?}]
  fact_claims = []
```

它仍经过 ProposalAcceptance，只允许回复/明确延后，不允许新生活事实、媒体、工具、关系慢变量或持久记忆写入。对应 fixture 必须覆盖 timeout 与 structured parse failure。

## 4E. 允许依赖图与静态纪律

```text
Platform/Scheduler/UI → WorldRuntime
WorldRuntime → Ledger, SituationCompiler, Deliberation,
               ProposalAcceptance, ActionExecutor
SituationCompiler → read-only Projection Interfaces, MatrixCatalog
AdvisoryCompiler → read-only InternalWorldSnapshot, classifier Adapters, MatrixCatalog
Deliberation → Model Adapter, MatrixCatalog
ProposalAcceptance → Ledger UnitOfWork (includes Budget projection/reservation events)
Runtime-owned ActionPump → private ActionClaimPort, ActionExecutor
ActionExecutor → Platform/Tool/Media public Interfaces
MediaExecutionAdapter → event_media public seam
Reducers → domain event schemas only
```

`WorldRuntime` 在同一 revision 上并行调用 SituationCompiler 与 AdvisoryCompiler；前者产权威 slices，后者产有超时的候选分布，二者由 Runtime 冻结成一个 Capsule。classifier Adapter 的输入仅为 trigger、必要近期上下文和已裁剪 snapshot；输出包含候选、来源、置信与过期，不获得 ledger 写权限。

Budget 不是独立可提交服务：它是同一 WorldLedger UnitOfWork 内的 projection + events。Acceptance 只计算 reservation commands，Runtime 在 B 事务与领域事件/Action 一起提交；settlement 同理原子 settle/release。外部成本查询可通过只读 BudgetProjection Interface，但不得拥有写事务。

Action 所有权固定如下：Runtime 内部的 ActionPump 通过 private `ActionClaimPort.claim_due()` 请求 Runtime/Ledger UoW 写 `ActionClaimed/DispatchStarted`；随后调用纯副作用 `ActionExecutor.dispatch()`。Executor 只返回 ProviderReceipt/DispatchPending 或被 provider webhook 异步补回，绝不调用 Runtime 或写 ledger。Runtime 将返回值/回调统一包装为 ExternalObservation，经公开 `settle()` 的内部同一 settlement handler 提交。平台/调度器仍只见 `WorldRuntime`；ActionClaimPort 不导出到应用边界。

禁止依赖：Adapter → reducer/ledger implementation；图片机 → World 写模型；Reducer → 模型/网络/时钟；Deliberation → 平台 SDK；Dashboard/Godot → 旧 Engine 或账本表；任何跨 Module 调用绕过声明 Interface。CI 使用 import graph allowlist 检查这些规则。

## 4F. 主模型不是流水线末端，而是内心综合器

世界机的机制不是串行审批链。正确语义是：事实、生活、情绪、关系、记忆、线程和随机倾向共同构成“内心材料”，由一次主 Deliberation 综合决定。轻量分类器可并行给出候选 appraisal、用户情绪、话题线程和风险，但它们均可被主模型否定。

这保留了 LLM 裸聊的语义能力，也保留世界机独有的长期因果：口是心非时表面话语和 AffectEpisode 分开结算；想起一件事时可以打开 PrivateCommitment；多发几句时创建有依赖的 Expression Beats；生活和 NPC 事件可以在用户没说话时改变下一轮处境。

## 5. 分类矩阵

矩阵是 LLM 的处境语言，不是固定行为表。

### 5.1 观察与证据矩阵

| 维度 | 值 | 性质 |
|---|---|---|
| 触发源 | `user_message`、`clock_tick`、`scheduled_plan`、`npc_event`、`external_result`、`operator_command`、`recovery` | 事实 |
| 观察类型 | `text`、`attachment`、`receipt`、`tool_result`、`time_elapsed`、`world_seed` | 事实 |
| 证据状态 | `committed_fact`、`committed_experience`、`committed_or_settled_world_event`、`observed_message`、`active_plan`、`proposal`、`settled_external_result`、`private_impression`、`hypothesis`、`unknown` | 硬校验 |
| 证据强度 | `direct`、`corroborated`、`plausible`、`uncertain` | Proposal |
| 时间关系 | `current`、`recent`、`historical`、`future_plan`、`expired` | 事实 |
| 因果角色 | `cause`、`constraint`、`context`、`consequence`、`reference_only` | Proposal |

任何“已发生”断言必须引用 committed fact、committed experience、生命周期已允许该 claim 的 committed/settled world event，或 settled external result。`observed_message` 只能支持“用户说过/发过”，不能证明其内容为真。

### 5.2 生活处境矩阵

| 维度 | 值 |
|---|---|
| 时间段 | `deep_night`、`morning`、`midday`、`afternoon`、`evening`、`late_evening` |
| 地点 | `location_ref` + `private/shareable/public` 可见性；只能来自位置投影 |
| 生活状态 | `resting`、`routine`、`focused_work`、`study`、`social`、`travel`、`creative`、`errand`、`recovering`、`unstructured` |
| 活动阶段 | `not_started`、`starting`、`engaged`、`interrupted`、`paused`、`wrapping_up`、`completed`、`abandoned` |
| 注意力 | `available`、`glancing`、`occupied`、`deep_focus`、`do_not_disturb`、`recovering_attention` |
| 能量 | `restored`、`steady`、`strained`、`depleted` |
| 资源压力 | `none`、`mild`、`competing`、`urgent` |
| 计划关系 | `on_plan`、`delayed`、`substituted`、`self_revised`、`interrupted_by_event`、`cancelled` |
| 社交环境 | `alone`、`with_known_npc`、`group_context`、`public_ambient`、`family_context` |
| 场景可见性 | `private`、`shareable_life`、`shareable_character_media`、`not_shareable` |
| 当前目标 | `goal_ref`、进度、阻碍、重要性；来自 active Plan/Goal projection |
| 未完成承诺 | `commitment_ref`、due window、状态、关系成本；来自 commitment projection |

这些字段只来自世界投影、Logical Time 与已提交活动/计划事件。LLM 可以提议活动替换、暂停、恢复或放弃，但不能凭空宣布地点、参与者或完成状态。

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
| 置信等级 | `low`、`medium`、`high`；另存 `confidence_score ∈ [0,1]` |
| 生命周期 | `candidate`、`active`、`contradicted`、`expired`、`superseded` |

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
| 媒体 | `media_planning`、`media_render`、`media_inspection`、`media_delivery` | planning/render/inspection 可在 preview 自动；delivery 需批准 | 图片机结果 + 平台 receipt |
| 多模态理解 | `vision`、`transcription` | 预算内自动 | 模型 External Result |
| 工具 | `read_only_tool` | 预算内自动 | 工具 External Result |
| 禁止首发自动 | `file_write`、`delete`、`shell`、`account`、`payment`、`third_party_commitment` | blocked | 不创建执行 Action |

统一 Action 状态：

```text
authorized → scheduled → claimed → dispatch_started → provider_accepted
→ delivered | failed | unknown
authorized / scheduled / claimed → cancelled | expired
dispatch_started → unknown when provider cannot query by key
```

### 5.9 行为倾向矩阵

行为倾向回答“此刻较可能往哪个方向行动”，不回答“必须说什么”。主模型可以不采纳，但必须在 Proposal 中表达其实际选择。

| 倾向 | 含义 | 常见来源 | 可能落点（非强制） |
|---|---|---|---|
| `maintain` | 维持当前生活/关系节奏 | 稳定、低压力 | 普通回应、继续活动 |
| `advance` | 推进目标或关系 | 目标清晰、精力足 | 完成一步、主动靠近 |
| `explore` | 探索新话题/活动 | 好奇、novelty | 联想、尝试、轻问询 |
| `procrastinate` | 明知目标但拖延 | 压力、低能量、回避 | linger、换小事、稍后再做 |
| `avoid` | 降低接触或回避冲突 | 自我保护、羞耻、过载 | 收住、转移、暂不回复 |
| `rest` | 恢复资源 | depleted、strained | 暂停活动、简短表达 |
| `share` | 分享生活/内部状态 | connection、expression | life share、media candidate |
| `repair` | 修复误解或伤害 | 在意、愧疚、关系成本 | 解释、承认、温和靠近 |
| `set_boundary` | 维护自主和尊严 | 越界、控制压力 | 拒绝、反驳、拉开距离 |
| `disagree` | 表达观点差异 | 价值冲突、事实冲突 | 温和异议、直接反驳 |

### 5.10 变化阶段矩阵

变化阶段避免角色每轮随机换人格，也避免所有偏离立即恢复。

| 阶段 | 定义 | 进入条件（候选） | 退出/转移 |
|---|---|---|---|
| `baseline` | 稳态范围 | 无显著压力 | preference deviation / stress |
| `preference_deviation` | 临时偏好或节奏偏移 | 新鲜感、低成本随机、生活变化 | 自然回归或形成长期倾向候选 |
| `stress_response` | 资源压力下的收缩/外露 | 累积疲劳、冲突、目标受阻 | recovery 或 relationship tension |
| `relationship_tension` | 关系伤害持续影响行为 | 越界、失信、反复忽视 | repair、hold boundary、withdraw |
| `recovery` | 有余波的恢复过程 | 时间、修复事件、资源恢复 | baseline 或再次受刺激 |

变化必须记录来源、开始时间、影响维度、强度、衰减/过期条件和恢复姿态。只有多次跨场景一致证据才可提出 Character Core revision；单轮变化不得改稳定人格。

### 5.11 行动层级矩阵

| Layer | 例子 | 是否产生副作用 | 权威落点 |
|---|---|---:|---|
| `internal_state_transition` | appraisal、affect、印象、线程、承诺 | 否 | accepted world events |
| `world_event` | 开始/暂停活动、NPC 互动、位置变化 | 世界内部 | world events + reducers |
| `external_action` | 回复、主动消息、reaction、typing | 是 | Action + receipt |
| `media_action` | plan、render、deliver | 是/可能收费 | MediaPlan + Action + receipt |
| `read_only_tool` | 搜索、视觉、转写 | 是/收费但不改外部状态 | Action + external result |

同一 Proposal 可跨层，但 Acceptance 必须逐项授权。内部状态改变不能伪装成外部动作；外部动作失败也不能倒推为已完成的关系或生活事实。

### 5.12 Appraisal、印象与置信生命周期

| 生命周期状态 | 条件 | 可影响 | 不可影响 |
|---|---|---|---|
| `candidate` | 尚未由 Acceptance 接受 | 本轮主模型权衡 | 任何持久投影 |
| `active` | 已接受且未过期；置信度独立记录 | Affect、Private Impression、线程 | 将心理状态写成客观事实 |
| `contradicted` | 用户澄清或后续行为相反 | 触发修订/道歉候选 | 继续作为活跃依据 |
| `expired` | 到期且无新支持 | 仅保留历史审计 | 进入 ContextCapsule 活跃区 |
| `superseded` | 被更好解释替代 | 审计与因果引用 | 与新解释并列为当前判断 |

每个 appraisal 必须含来源、置信度和过期条件。对讽刺、潜台词、失望、权力差异可保留多个替代解释；系统可以察觉却选择不安慰，也可以因在意而主动修复，这由关系、驱动、生活处境和随机倾向共同交给主模型决定。

#### 5.12.1 Affect 参数化演化协议

Affect 的确定性来自参数化事件与 Logical Time，不来自“遇到冒犯就必须生气”的规则。

```text
AffectDimension = hurt | anger | sadness | loneliness | anxiety |
                  resentment | warmth | joy
AffectComponent
  dimension
  intensity ∈ [0,1]
  baseline ∈ [0,1]
  source_cluster
  opened_at / last_updated_at
  decay_profile {kind, half_life, floor, delay, config_version}
  residue ∈ [0,1]
```

Reducer 规则：

1. `AppraisalAccepted` 只记录被接受的事件意义、归因、严重度与置信度；独立的 `affect_transition` 携带主模型提出、Acceptance 接受的 component deltas。Reducer 不自行判断事件意义，也绝不从 appraisal 再隐式生成第二份 delta。
2. 同一 `dimension + source_cluster` 在可配置 merge window 内通过 `AffectEpisodeUpdated` 合并；不同来源可并存，投影按有界饱和聚合 `1 - ∏(1 - component_i)`，保留来源可追踪性。
3. 重复刺激不是重置计时器：事件显式记录增量、敏感化/钝化候选和新 decay 参数；Acceptance 后才更新。
4. `AffectEpisodeDecayed` 由 Logical Time 和冻结的 decay profile 计算；默认指数半衰期只是可替换配置，不决定可见行为。回放使用事件中的 config version。
5. intensity 降到 floor 后可保留 residue；只有 `AffectEpisodeResolved` 或 superseding event 才关闭 episode。表面回复、一次安慰或发送成功都不能隐式清零。
6. 混合情绪保留多维 component，不预先映射成固定模板；Deliberation 同时看到强度、来源、余波和表达历史。
7. baseline 只能由跨场景长期校准事件调整；单轮 appraisal 只改 episode，不改 baseline。

持久化与 hash 使用定点整数（例如 `0..10000`）和版本化舍入规则，文档中的 `[0,1]` 只为可读表示；禁止依赖平台浮点差异。`advance()` 在进入下一次 Deliberation 前按 from/to Logical Time 物化必要的 `AffectEpisodeDecayed`，因此 Capsule revision 与 replay hash 一致。

首发配置提供默认半衰期、merge window、saturation 和 residue 范围，但全部版本化；自动模拟和未来真人数据只调参数，不改事件语义。

#### 5.12.2 关系慢变量协议

```text
RelationshipState
  variables {trust, closeness, respect, reliability, mutuality,
             repair_confidence} each ∈ [0,1]
  stage, temperature, active_boundaries[]
  policy_version, last_adjusted_at

RelationshipAdjustment
  source_refs[], variable_deltas{}, confidence
  persistence, contradiction_group?, rationale_code
```

主模型提出 adjustment，Acceptance 要求来源并按版本化完整性上限裁剪单次 delta；这只防止数值爆炸，不规定角色必须亲近或原谅。Reducer 以事件携带的 accepted delta 确定性更新并 clamp 到 `[0,1]`。相互矛盾的信号不互相删除，而在 contradiction group 中累计，由后续 Deliberation 提出修订。

关系数值同样以定点整数存储。单次 delta 上限、hysteresis 区间和聚合方式属于 `RelationshipPolicy` 版本；事件记录使用的 policy version 和裁剪前/后 delta，保证调参不会改变历史 replay。

stage 是带 hysteresis 的派生投影：由慢变量、已确认关系事件和显式关系承诺共同计算，不能靠一次分数跨级，也不能反过来直接改慢变量。边界是独立集合，不因 closeness 高而自动放宽。correction 通过补偿 adjustment；回放永远使用对应 policy version。

### 5.13 承诺、未完成事项与记忆持久度矩阵

| 类型 | 持久度 | 进入条件 | 结算 |
|---|---|---|---|
| transient notice | 本轮/短期 | 轻微信号、当场解决 | turn 结束或线程关闭 |
| open thread | 数小时至数天 | 问题、情绪未接住、待回复 | answered/skipped/expired |
| private commitment | 有 due window | 角色决定稍后做/问/修复 | fulfilled/broken/released |
| durable memory candidate | 跨会话 | 强度、重复、未来行为相关、边界或关系意义 | consolidation 接受/拒绝 |
| committed fact/experience | 长期 | 明确事实或已结算经历 | correction/supersession |

用户情绪是否进入账本不靠“负面就记”的死规则：轻微且当场被接住的信号可只留本轮 appraisal；显著、反复、影响后续立场、形成未完成修复或关系后果时，打开有来源和过期条件的 episode/thread/impression。

### 5.14 对话节奏、打断与多段表达矩阵

| 维度 | 值 |
|---|---|
| 输入状态 | `user_typing`、`coalescing`、`complete_thought`、`long_narration`、`new_interjection` |
| 角色注意 | `available`、`glancing`、`occupied`、`deep_focus`、`recovering_attention` |
| 表达形态 | `single_beat`、`multi_beat`、`reaction_then_text`、`defer_with_intent`、`silence` |
| 打断动机候选 | `high_interest`、`strong_disagreement`、`urgent_correction`、`care_impulse`、`boundary_pressure`、`playful_overlap` |
| 打断成本 | `low`、`moderate`、`high` |
| 用户插话后的 beat | `continue`、`reconsider`、`cancel`、`merge`、`defer` |

打断不由关键词或固定阈值决定。轻量语义模型可给出动机候选和置信度，主模型结合关系、注意、表达冲动与打断成本决定。`ExpressionBeat` 是有序、可结算对象，包含 dependency、delay window、cancel/reconsider policy；不得把一段回复机械切字符串冒充多段消息。

### 5.15 矩阵运行语义

每个矩阵字段必须在 catalog 中声明：

```text
field_id, value_set, owner, candidate_producers[]
consumers[], persistence, confidence_required
expiry_or_decay, compatible_values[], catalog_version
```

运行规则：

1. MatrixCatalog 管 schema 与组合合法性，不决定行为。
2. 分类器输出候选分布而非单标签命令，并记录版本、来源、置信度。
3. SituationCompiler 冻结本轮 catalog version；同一 Proposal 不混用版本。
4. Deliberation 可选择、合并、拒绝候选，并输出最终采用的坐标及简短理由。
5. Acceptance 只验证 schema、证据、能力与硬不变量，不以“模型没听分类器”为由拒绝。
6. Evaluator 统计机械映射、过敏、恢复过快、长期不一致和随机频率，但不在线接管角色。

## 6. 图片机接入

图片机保持独立，继续使用 `event_media` public seam：

```python
MediaPlanner.plan(opportunity, recent_media)
MediaRenderer.render(plan)
OpenAIMediaInspector.inspect(plan, artifact)
MediaExecutionAdapter.repair_once(plan, failed_artifact, inspection)
```

World v2 不直接生成图片 prompt，不直接调用图像模型。

### 6.1 Module 所有权

| World v2 拥有 | 图片机私有拥有 |
|---|---|
| Photo Candidate 与生命周期 | family 对应的内容规划策略 |
| 被选事件与 frozen `event_snapshot` | `content_domain × visual_form × share_intent` 组合 |
| family、delivery mode、隐私上限 | capture、构图、人物呈现、polish、tone |
| 预算、Action、是否发送、配文、receipt | prompt 编译、生成、视觉检查、artifact hash |

World 不能逐字段操控构图；LLM 不能拼接五官、姿态、手势。图片机从合法的整体 `subject_variant_id` 中选择完整人物呈现方案。该约束保证图片机是深 Module，而不是 prompt 字段拼装器。

### 6.2 World → 图片机机会矩阵

| 维度 | 值 |
|---|---|
| Candidate 状态 | `available`、`selected`、`planned`、`generated`、`shared`、`skipped`、`unrenderable`、`expired`、`failed` |
| family | `life_share`、`character_media` |
| delivery mode | `preview`、`automatic` |
| privacy ceiling | `ordinary`、`personal`、`intimate` |
| evidence snapshot | committed event refs、logical time、地点、当前活动、参与者、物品、环境、角色可见外观、既有媒体、可公开外显的 display state |

机会的禁止输入：未来计划、PrivateImpression、未表达 AffectEpisode、自由心理摘要、LLM 猜测、图片 prompt、未提交 Proposal。`event_snapshot` 一旦冻结不可变；dashboard、MediaPlan、caption 和 inspection 必须引用同一 snapshot hash。情绪提示只能来自已接受且被角色选择为可外显的 display state。

聊天文本或 Proposal 不得直接跳到生图：

```text
committed event
→ PhotoCandidate
→ selected + frozen MediaOpportunity
→ media_planning Action
→ frozen MediaPlan
→ media_render Action
→ inspected artifact
→ optional media_delivery Action
```

用户明确提出的创意生图请求走独立的 `user_creative_media` 管线，不伪装成角色世界经历，也不参与 Photo Candidate 选择。

冻结合同：

```text
MediaOpportunity
  opportunity_id, candidate_id, family, delivery_mode
  privacy_ceiling, event_snapshot_ref, event_snapshot_hash
  catalog_version, expires_at

MediaPlan
  plan_id, planning_request_id, opportunity_id, event_snapshot_hash
  family, content_domain, visual_form, share_intent, capture_mode
  character_visibility, other_people_visibility, polish, tone, privacy
  subject_variant_id?, prompt_payload_ref, prompt_hash
  planner_version, schema_version, frozen_at

MediaInspection
  inspection_id, plan_id, artifact_ref, artifact_hash
  passed, visible_content_summary, violation_codes[]
  repairable, repair_scope?, inspector_version, schema_version
```

下游 Action 只引用这些对象的 ID+hash；任何 hash 不一致都拒绝 continuation。prompt payload 受权限保护，不能进入 World fact/experience 投影。

### 6.3 图片规划矩阵（图片机私有真相）

| 维度 | 值 |
|---|---|
| `content_domain` | `place_environment`、`food_drink`、`object_possession`、`activity_process`、`outcome_progress`、`appearance_style`、`body_health`、`social_interaction`、`nature_animal`、`information_screen`、`travel_transit`、`other_grounded` |
| `visual_form` | `wide_scene`、`contextual_still_life`、`process_pov`、`subject_closeup`、`result_showcase`、`portrait_closeup`、`portrait_context`、`full_body`、`body_detail`、`social_frame` |
| `share_intent` | `atmosphere`、`record`、`show_and_tell`、`check_in`、`seek_feedback`、`progress_update`、`complain`、`care_update`、`humor`、`intimate_signal`、`memory_keep` |
| `capture_mode` | `character_front_camera`、`character_rear_camera`、`mirror`、`timer_fixed`、`requested_helper`、`known_companion`、`external_sender`、`existing_artifact` |
| `character_visibility` | `none`、`trace_only`、`identifiable`、`body_detail` |
| `other_people_visibility` | `none`、`anonymous_incidental`、`known_anonymized`、`identity_referenced` |
| `polish` | `raw`、`casual`、`curated` |
| `tone` | `neutral`、`calm`、`warm`、`bright`、`amused`、`playful`、`proud`、`tired`、`frustrated`、`embarrassed`、`tender`、`vulnerable` |
| `privacy` | `ordinary`、`personal`、`intimate` |

`family × content_domain × visual_form × share_intent` 的兼容表由图片机版本化持有，World 只传 family 和证据/隐私上限。无法合法组合时返回 `NotRenderable`，不得偷偷换成另一个事件。

### 6.4 人物呈现与社交互动矩阵

| 类别 | 字段/值 |
|---|---|
| Appearance source | `world_fact`、`media_local`；禁止生成图自举为外观来源 |
| Appearance fields | 发型、衣装角色、妆容整理程度、饰品 |
| Performance fields | 头部方向、俯仰、侧倾、视线、表情、肩线、姿态、手势、镜头意识、双手职责、遮挡复杂度 |
| Interaction Bid | `communicative_goal`、`hoped_response`、`response_pressure`、`audience_ref` |
| Bid 生命周期 | `planned`、`generated`、`delivered`、`answered`、`expired`、`superseded`、`withdrawn` |
| Display Strategy | 图片机模板中可用的整体表演、嘴眼眉、视线质感、禁止线索组合 |

`subject_variant_id` 绑定一套经校验的 appearance + performance 组合，LLM 只可选择整套变体。媒体发送成功后才可将 Bid 置为 `delivered` 并打开待回应关系线程；未发送或发送失败不能制造“用户没回应图片”的关系事实。

### 6.5 冻结、幂等与恢复不变量

1. 对每个 frozen opportunity 保证逻辑 effect-once：先提交带稳定 `planning_request_id` 的 planning Action；`MediaPlanner.plan()` 必须接受该 key，并支持按 key 返回原结果/查询结果。进程可重试网络调用，但同一 key 只能产生一个逻辑 MediaPlan；结果（包括 NotRenderable）记录入账。
2. crash recovery 重新水合既有 frozen MediaPlan，禁止 replanning。
3. `MediaRenderer.render()` 只接受 frozen MediaPlan；不得接受自由 prompt 或现场改字段。
4. 自动视觉修复最多一次，且必须保留同一 opportunity、snapshot 和 plan identity；第二次失败终止。路径固定为：`MediaInspectionFailed(repairable=true)` 打开 `media_repair` TriggerProcess → Deliberation 可提出或放弃 `media_repair_transition` → Acceptance 原子 reserve 预算并创建新的 repair render Action。`repair_attempt_id = hash(plan_id, failed_artifact_hash, 1)`；payload 只含 frozen plan、失败 artifact、frozen inspection 中的可见缺陷，禁止改变事件、family、domain、form、intent 或 subject identity。修复后创建新的 inspection Action；任何阶段失败即终止。
5. planning、render、inspection、delivery 分别是 Action；每步有预算、幂等键、终态与错误分类。
6. inspection 只描述可见内容与违反项，不创造经历、人物身份或地点事实。
7. 生成图不得自动反写角色外观事实；prompt 不得成为 committed experience。
8. `unknown` delivery 不重发；对账前不打开 media bid，也不声称已发送。

正常媒体链的每一步也必须经 Acceptance，不允许 settlement 偷建下游 Action。每个成功结果原子打开一个 deterministic continuation TriggerProcess：

```text
planning result  → trigger hash(plan_id, plan_to_render)
                → ContinuationProposal(media_continuation)
                → Acceptance + render budget + render Action
render result    → trigger hash(artifact_hash, render_to_inspect)
                → ContinuationProposal(media_continuation)
                → Acceptance + inspect budget + inspect Action
inspection pass → preview: terminal generated
                → automatic且approval有效: trigger hash(inspection_id, inspect_to_delivery)
                → ContinuationProposal(media_continuation)
                → Acceptance + delivery budget + delivery Action
```

ContinuationProposal 的 evidence 必须是上一步 settled result，Action payload hash 必须引用冻结 plan/artifact/inspection。重复 settlement、crash 或 worker 重启 join 同一 continuation TriggerProcess；每一步单独 reserve/settle 预算。preview 模式不得生成 delivery continuation。

Candidate、Bid 与 receipt 映射：

| 条件 | Candidate | Bid | 说明 |
|---|---|---|---|
| opportunity frozen | `selected` | `planned` | 尚无 MediaPlan |
| plan recorded | `planned` | `planned` | 可创建 render Action |
| inspected artifact passed | `generated` | `generated` | 可预览/候选发送 |
| delivery receipt delivered | `shared` | `delivered` | 只有 delivered reducer 可写 shared/open response thread |
| delivery failed | `generated` 或策略显式 `failed` | `withdrawn` | 不声称已发；自动重发禁止 |
| delivery unknown | `generated` + reconciliation flag | `generated` | 不置 shared、不打开线程、不重发 |
| candidate expired before delivery | `expired` | `expired` | 释放预算与 pending intent |
| user response after delivered | `shared` | `answered` | 引用 response Observation |

Candidate 不因 provider `accepted` 变为 shared。delivery failure 是否保留可人工重选的 generated artifact 由版本化 policy 显式决定，不能由 reducer 猜测。

### 6.6 首发 preview 与覆盖策略

首发只开启 preview，不自动发送。覆盖不是穷举笛卡尔积，而是：

1. 对 family、domain、form、intent、privacy、capture mode 每个枚举至少一个合法样本；
2. 对核心四维 `family × domain × form × intent` 做 pairwise 覆盖；
3. 对高风险组合做定向全组合：`intimate/intimate_signal`、人物可识别、他人可见、`requested_helper/known_companion/external_sender` capture、带 response pressure 的 Interaction Bid；
4. 每个样本断言 snapshot hash 一致、plan exactly-once、render frozen-only、检查 fail-closed；
5. 通过视觉与事实安全阈值后仍须 operator 人工验收并提交 `MediaAutomaticDeliveryApproved(family, catalog_version, planner_version, inspector_version, schema_version, sample_set_hash, approver, expires_at)`，才按 family 分批开放 budgeted automatic delivery；任一相关主版本变化会使批准失效，回到 preview。

preview 权威流程：

```text
Committed World Event
→ PhotoCandidateOpened
→ MediaOpportunityFrozen
→ BudgetReserved + media_planning Action
→ MediaPlanRecorded | NotRenderable
→ optional media_render Action
→ MediaArtifactInspected | failure
→ World settlement
```

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

### 7.1 机制闭环总表

文件迁移不能证明机制可用。下表定义规格层闭环；7.3 将当前 producer/consumer 映射到具体 v2 事件、Reducer 与 fixture。实现 PR 只能把映射落到代码符号，不得重新发明语义；任一格为空就不算接入。

| 机制 | 来源/输入 | 权威状态 | 决策消费 | 可见落点 | 结算/后续消费 |
|---|---|---|---|---|---|
| 稳定人格与身份 | Character Core revision | versioned core | Capsule/Deliberation/媒体人物约束 | 语言、偏好、边界一致性 | 长期证据才可提 revision |
| 用户情绪识别 | message、上下文、行为变化 | appraisal + optional impression/thread | 驱动、stance、reply timing | 接住、留意、暂不管、修复 | contradicted/expired/resolved |
| 角色 Affect | 世界、用户、NPC、目标事件 | AffectEpisode | activity、stance、display、proactive | 情绪外露、嘴硬、沉默、靠近 | decay/resolve/residue |
| 嘴上与心里不一致 | affect + display strategy | episode + expression history | Deliberation | “没事”但余波仍在 | 后续刺激/修复继续消费 |
| 关系与边界 | accepted signals、承诺结果 | slow variables + boundary | stance、privacy、proactive | 亲近、谨慎、拒绝、修复 | 慢变量 reducer |
| 记忆 | facts、experiences、threads、commitments | 各自 projection | relevance retrieval | 自然提及、未来行动 | consolidation/correction/expiry |
| 当前生活 | clock、plan、activity、location | situation projection | reply timing、分享、媒体、主动性 | 忙、被打断、改计划、完成 | activity settlement |
| NPC/社交世界 | committed NPC events | experiences、relationship-to-NPC | affect、计划、分享 | 心情/生活发生变化 | 后续计划/记忆/媒体 |
| 用户影响世界 | request/建议/关心 | Proposal 后的 plan/event | activity deliberation | 接受、拒绝、折中、后来想起 | plan outcome，不把要求当事实 |
| 输入合并 | 平台片段、typing | Observation metadata | SituationCompiler | 避免逐片抢答 | coalescing window 终止 |
| 回复时机 | attention、relationship、content、budget | Action schedule | Deliberation | 快回、短停、晚回、不回 | delivered/expired/reconsidered |
| `reply_later` | defer intent + due window | PrivateCommitment + scheduled Action | advance/recovery | 稍后真正补回 | fulfilled/broken/released |
| `conversation_pulse` | unresolved thought/thread | open thread/commitment | advance | 过一会补一句 | resolved/expired |
| 主动行为 | life/relationship/thread/随机 | proposal + proactive budget | Deliberation | 主动消息、分享、修复 | receipt + user response thread |
| 等待与失望曲线 | sent bid + elapsed time + relationship | waiting stage | affect/next stance | 克制、困惑、受伤、放下 | response/expiry/revisit |
| 多段表达 | expression plan | ordered ExpressionBeats | Action scheduler | 连续发几句而非大段模板 | 每 beat receipt；插话重审 |
| 打断用户 | semantic interest/disagreement/care/boundary | advisory + proposal | Deliberation | 合理插话/反驳/补充 | 用户插话后 cancel/merge |
| reaction/sticker | affect/display/media context | Action intent | Deliberation | 非文本表达 | receipt/timeout |
| 用户图片理解 | attachment + vision result | external result + observation | Deliberation/memory candidate | 针对图片自然回应 | 事实范围受 inspection 限制 |
| 角色媒体分享 | committed event | candidate/opportunity/plan | media pipeline | 图片+配文+bid | inspection+delivery receipt |
| 只读工具 | user/world need | Action + External Result | Deliberation | 基于工具结果回应 | result expiry/source citation |
| 模型 fallback | timeout/parse/error | ModelResultRecorded | quick adapter/recovery | 自然短回复或合理延后 | fresh-only re-deliberation |
| 成本预算 | action intent | reservation ledger | Acceptance/Executor | 决定可执行或延后 | settle/release |
| 拟真评估 | replay + traces | evaluator report | 开发迭代 | 不直接影响角色 | baseline/regression history |

### 7.2 六条关键情境序列

#### 用户分享生活且逐渐失望

`ObservationRecorded → disappointment 候选 + alternatives → 主模型判断是否察觉/是否介入 → 可选 Affect/Thread/Impression → reply Action → receipt → 后续用户反应支持或反证`。轻微信号当场化解可不持久化；明显、重复或形成未完成修复时必须留 episode/thread。

#### 冒犯后口是心非

`boundary_violation appraisal → hurt/anger episode → care_vs_self_protection → display=withhold_for_now → 表面简短回复 → episode 不关闭 → 后续可能疏远、反驳或主动修复`。

#### 热启动与冷启动

热启动直接使用增量 projection、最近 turn 与已缓存 Capsule slices；冷启动允许加载更多历史摘要。两者使用同一语义路径，区别只在材料获取，不允许热启动重复跑全量审计。

#### 延迟回复与用户新插话

`defer proposal → PrivateCommitment + scheduled Action → 新 Observation 到达 → 旧 Action 在 claim 前重新审议 → continue/merge/cancel → terminal settlement`。不能把过时的稍后回复机械发出来。

#### 世界事件影响情绪

`ClockAdvanced → Activity/NPC event committed → appraisal candidates → AffectEpisode → situation projection → 下一轮 Capsule → 语言、注意、主动分享或计划偏离`。没有从事件到下一轮决策消费的 trace 就视为未接入。

#### 多段消息被用户打断

`ExpressionBeats authorized → beat 1 delivered → user interjection → remaining beats reconsidered → merge/cancel/defer → each Action terminal`。未发送 beat 不得留作“已经说过”的经历。

### 7.3 当前机制到 v2 的施工映射

| 当前 producer / consumer | v2 输入与事件 | Reducer / 消费者 | Action / 结算 | 权威 fixture | 结论 |
|---|---|---|---|---|---|
| `interaction_appraiser.py`、`affective_advisory.py` | InnerAdvisory → `AppraisalAccepted` | affect/impression/thread reducers；Deliberation | optional reply/followup | `W2-AFF-001/002`、`W2-IMP-001` | 重写为 AdvisoryCompiler adapters |
| `emotion_*`、`world_affect.py` | `AffectEpisodeOpened/Updated/Decayed/Resolved` | AffectProjection → Capsule | 无直接 Action | `W2-AFF-*` + decay property tests | 保留 episode 思想；删除双写 vector |
| `memory.py`、`memory_consolidation.py` | `MemoryCandidate*`、`FactCommitted/Corrected`、`ExperienceCommitted` | fact/experience retrieval | 无直接 Action | `W2-MEM-001/002` | parser 可复用；旧写权威归档 |
| `context_assembler.py` | `InternalWorldSnapshot` | SituationCompiler → ContextCapsule | 无 | Capsule budget/stability tests | 收入 deep implementation |
| `life_runtime.py`、`calendar_ledger.py`、`life_event.py` | Goal/Activity/Location/WorldOccurrence/NPC events | situation/experience/NPC reducers | life share/media candidate 可选 | `W2-LIFE-001/002/003/004` | 语义重写；旧事实源归档 |
| `reply_decision.py`、`reply_timing.py`、`im_timing.py` | behavior/attention/timing candidates | Deliberation + Action scheduler | reply/defer/no reply | `W2-RHY-001/002/003` | 只留无状态 timing helper |
| `proactive_*`、`social_followups.py` | Thread/Commitment/Recovery events | advance + Deliberation | proactive/followup | `W2-PRO-001/002` | 删除 social_tasks 权威 |
| `conversation_pulse` 旧路径 | `ThreadOpened` + `PrivateCommitmentOpened/Due` | advance | followup Action | `W2-PULSE-001` | 迁移为 thread+commitment |
| `CompanionTurn` 多消息路径 | `ExpressionPlanAccepted`、Beat events | beat scheduler | one Action per beat + receipt | `W2-BEAT-001/002` | 收敛为 delivery Adapter |
| 旧打断/typing 机制 | Observation coalescing + interrupt Advisory | Deliberation | reaction/reply/beat reconsider | `W2-INT-001/002` | 删除关键词裁决 |
| `reply_stickers.py`、`emotion_reactions.py` | ActionIntent candidates | Deliberation/Acceptance | reaction/sticker receipt | `W2-REA-001` | helper 只提候选 |
| `event_media.py`、`world_media.py`、`media_*` | Photo/Opportunity/Plan/Inspection events | media projection | planning/render/delivery Actions | `W2-MED-001..007` | 保持 public seam |
| `image_requests.py` | user creative media intent | 独立 creative request projection | creative media Action | `W2-CMEDIA-001` | 不进入世界事件媒体 |
| `multimodal_analysis.py` | Vision/Transcription result + deterministic result trigger | sourced result projection → Deliberation | vision/transcription Action + optional reply | `W2-VIS-001` | 结果范围受 evidence 限制 |
| read-only tool adapters | ToolResult + deterministic result trigger | sourced result projection → Deliberation | tool Action + optional reply | `W2-TOOL-001` | 不允许结果入账后断链 |
| Platform/QQ/HTTP inbound | `ObservationRecorded` | Runtime ingest | reply/reaction Actions | `W2-OBS-001` | Adapter 不决策 |
| outbox/receipt/turn traces | Action/Receipt/Reconciliation events | action/outbox projections | terminal settlement | `W2-ACT-001..004` | ledger-backed，不授权事实 |
| dashboard/Godot/room | ProjectionRequest | viewer projection | 无 | `W2-PROJ-001` | 只读迁移 |
| budget/model routing | Budget + ModelResult events | Acceptance/Evaluator | reserve/settle/release | `W2-COST-001/002` | 配置化单一权威 |

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

### 9.3 模型路由、延迟与成本预算

复杂性保留在“需要它的回合”，不能让每句闲聊支付全部机制成本。

| Route | 适用 | 默认模型策略 | Thinking |
|---|---|---|---|
| `chat` | 普通热对话、无复杂事实/关系冲突 | Flash；一次主生成 | 关闭 |
| `expressive` | 显著情绪、嘴上与心里不一致、修复、多线程冲突 | Flash 或强模型 | 短且有界 |
| `world_action` | 计划、NPC、媒体、工具、可验证生活断言 | Flash + 局部 claim/action 校验 | 通常关闭 |
| `deep_deliberation` | 高歧义、高关系成本、不可逆行为、复杂隐私/同意 | 强模型 | 允许有界 thinking |
| `quick_recovery` | 主模型 timeout/过载 | 快模型自然短回复/延后 | 关闭 |

路由是成本/能力选择，不改变领域语义。无论使用 Flash、其他模型或 thinking，结果都进入同一 `ModelResultRecorded → ProposalAcceptance` 流程。

热路径预算：

| 阶段 | 目标 |
|---|---:|
| 输入 coalescing | 0.4–0.8 秒，长叙述可由平台信号延长 |
| 增量 Capsule + advisories | P95 ≤ 0.15 秒；并行，超时可省略 advisory |
| 模型首 token | 热对话 P50 ≤ 1.2 秒（诊断指标，从 coalescing 结束计） |
| 普通完成 | P50 2–3 秒 |
| ingress → 首个可见消息/receipt | 热启动 P50 ≤ 3 秒、P95 ≤ 5 秒、P99 ≤ 8 秒 |
| 冷启动 ingress → 首个可见消息/receipt | P50 ≤ 5 秒、P95 ≤ 8 秒、P99 ≤ 12 秒 |
| 热/冷差异 | 同一场景热启动 P50 至少快 30%；热路径不得重建全投影 |
| Acceptance/claim guard | P95 ≤ 0.3 秒 |

性能纪律：

1. 普通无事实 claim 对话不启动独立二次审计模型。
2. facts、relationship、affect、threads 使用增量投影；禁止逐轮全 replay。
3. 非关键记忆整合、摘要、usage、Evaluator 在首条消息发出后异步完成。
4. advisor 并行且有超时；缺少 advisor 不阻断聊天。
5. trace 记录 TTFT、各 slice 时间/token、模型 route、排队、Action dispatch 与 receipt 延迟。
6. 成本按 action category、route、主动/被动、成功/失败分别设日预算和告警；预算不足时允许自然延后，不降级成脚本化固定话术。

QQ/消息平台没有流式 token 时，“用户可见”定义为第一条消息 Action 收到 provider acceptance/delivery（按平台能力选择，并固定在 Adapter contract）；模型 TTFT 仅用于拆解瓶颈，不能代替用户体感延迟。trace 必须从 ingress 开始，分别记录 coalescing、queue、snapshot、advisor、model、Acceptance、dispatch 和 receipt。

成本配置合同：

```text
CostProfile
  profile_id, currency, effective_at
  per_route {
    max_model_calls_per_turn,
    max_input_tokens, max_output_tokens,
    max_thinking_tokens, timeout_ms,
    permitted_model_tiers[]
  }
  daily_by_category {chat, repair, audit, proactive, vision, audio, image, tool}
  per_action_caps{}, proactive_daily_cap, media_daily_cap
  warning_thresholds[], hard_stop_thresholds[]
```

CI 使用固定 `test-economy-v1`：普通 chat 最多一次主模型调用、thinking=0、独立审计调用=0；expressive 最多一次主模型+一次仅在结构失败时的 recovery；deep_deliberation 的 thinking/token 上限必须显式配置。生产默认金额由部署配置决定，不在领域代码写死；缺少 profile 时 fail closed，**不得**创建付费 Action。

### 9.4 评估方法与通过门槛

Evaluator 同时运行裸聊基线、旧归档版本和 v2。普通聊天路径若在自然度上低于裸聊基线，或延迟显著增加，不能以“机制更复杂”作为通过理由。

首批固定场景至少包括：普通分享、连续追问循环、轻微失望、明确冒犯、讽刺潜台词、嘴硬余波、关系不近时察觉但不介入、主动修复、NPC 冲突影响心情、改计划、拖延、reply_later、新插话取消旧回复、多段消息、媒体机会、provider timeout、projection 缺片。

可复现评审协议 `human-likeness-eval-v1`：

1. 固定至少 120 个 scenario-turn 单元，其中情绪察觉 gold set 至少 40 个；每单元由两名标注者或两次独立 adjudication 给出可接受反应集合，不把“安慰”当唯一正确答案。
2. v2、裸聊基线、归档版本使用相同输入事实，随机化匿名输出顺序；每个随机 seed 重复 3 次。
3. judge 使用固定模型 ID、prompt/rubric version、temperature=0；20% 样本由第二 judge 复核。rubric 分 current-input fit、subtext awareness、subjectivity、continuity、non-scriptedness、fact safety。
4. “察觉痕迹”指输出/Proposal/持久 episode 三者任一明确响应 gold signal，同时没有把另一替代解释断言为事实；按 scenario-turn 计算 recall。
5. question-loop rate = 非必要问句收尾回合数 / 可回复回合数；fallback-smell rate = 命中版本化客服模板分类器且被 judge 确认的回合数 / 模型故障回合数。
6. 连续指标报告 bootstrap 95% CI；“不低于裸聊”要求差值 CI 下界 ≥ -0.03；“显著更好”要求连续性/事实安全/共时性三项中至少两项差值 CI 下界 > 0。
7. judge、rubric、scenario set、seed、模型输出 hash 与统计脚本版本全部入报告；更新任一版本需建立新 baseline，不能沿用旧阈值。

合并门槛：

- hard invariant violation = 0；
- 非终态 Action 泄漏 = 0；
- replay projection hash mismatch = 0；
- 普通热聊首 Action P95 ≤ 5 秒；
- question-loop rate 不高于裸聊基线；
- fallback-smell rate 不高于裸聊基线；
- 关键情绪场景的“有察觉痕迹”召回率达到 90%，但不要求全部安慰；
- 口是心非场景中表面缓和后 AffectEpisode 错误清零率 = 0；
- `reply_later` 到期后必须 fulfilled/broken/released，无悬挂；
- 随机 draw replay 一致率 = 100%。

语言拟真评分属于相对门槛：v2 必须在盲测式模型评审中至少不低于裸聊基线，并在长期连续性、事实安全、生活共时性三项显著更好。绝对分数在获得真人长期数据前只作趋势，不声称“完美拟真”。

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
- 定义 Command/Event/Entity/Query envelopes、`InternalWorldSnapshot` 与 viewer projection 权限。
- 定义 `WorldRuntime` Interface。
- 定义 ContextCapsule、InnerAdvisory、DecisionProposal v2、AcceptanceResult、ActionIntent、Action、Receipt、Grant/Policy schemas 与稳定错误码。
- 定义 MatrixCatalog schema 与版本。

验收：

- 类型测试通过。
- 空实现可 ingest/project，不调用旧 Engine。

### Phase 2：Ledger 与 Projection

- 实现 `WorldLedger.commit/rebuild/project`。
- 实现 revision、幂等、logical time、action state。
- 实现事件目录、deterministic reducers、upcaster、CAS、outbox/inbox、claim lease、receipt reconciliation。
- 实现 Affect/relationship/NPC/Goal/Thread/Commitment/Beat/Memory/Grant projections。
- 实现投影 hash。
- 建立 v2 seed。

验收：

- 相同事件重建相同 projection hash。
- Proposal 不会改变 projection。
- Action 状态终态不可逆。

### Phase 3：SituationCompiler 与 MatrixCatalog

- 编译 Context Capsule。
- 接入 facts、experiences、activity、needs、relationship、affect、threads、capabilities、budget。
- 实现 AdvisoryCompiler 及 appraisal/user-emotion/thread/interrupt classifier adapters，和 SituationCompiler 在同一 revision 并行冻结。
- 接入分类矩阵版本。

验收：

- Capsule 有 token/字段预算。
- 同一 projection 编译稳定。
- 不包含无来源散文心理事实。

### Phase 4：Deliberation 与 Acceptance

- 实现 LLM `DecisionProposal v2`。
- 实现最小硬校验。
- 实现 route、deterministic model request identity、ModelResult audit transaction 与 Acceptance CAS transaction。
- 实现 structured output parse / retry / fail-safe。
- 实现 MinimalProposal quick/parse recovery 和 fresh-only re-deliberation。
- 实现 `brief_rationale` 审计。

验收：

- 模型可提出回复、沉默、延迟、设边界、反驳、主动修复。
- Acceptance 不因“说法不够温柔/不够安慰”拒绝 proposal。
- 无来源事实、无 Action 副作用、越权工具被拒绝。

### Phase 5：ActionExecutor

- 实现 Runtime-owned ActionPump、private claim port 与 `dispatch_started` 状态。
- 接入 message、reaction、typing、sticker。
- 接入 receipt、timeout、unknown、recovery。
- 接入预算保留与释放。
- 接入 provider idempotency/result lookup、乱序/重复 receipt、reconciliation 与 manual review。

验收：

- 任意外发可追到 Action。
- 无 receipt 不得声称 delivered。
- 进程重启后 `scheduled/claimed` 按 lease 与 provider idempotency 恢复；`unknown` 只对账，绝不重执行。

### Phase 6：媒体 preview 接入

- 建立 `MediaExecutionAdapter`。
- preview 模式接入 `MediaPlanner.plan()`、`MediaRenderer.render()`、inspection。
- 记录 MediaPlan hash、artifact hash、inspection summary。
- 实现 planning effect-once key/result lookup、Candidate↔Bid↔receipt reducers、人工 automatic approval gate。

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
- 固化 scenario gold set、judge/rubric/statistics version、裸聊与归档 baseline、热冷性能与 test-economy profile。
- 建立机制闭环 CI。
- 删除或隔离旧行为入口。
- World v2 切为默认运行时。

验收：

- 全套测试、projection rebuild hash、机制闭环校验、拟真 evaluator baseline 通过。
- `WORLD_RUNTIME_ENABLED=false` 不再作为新功能回退路径。

### 10.9 施工依赖、并行边界与交付物

阶段编号表示依赖，不表示只能单线程施工：

```text
Phase 0
  └─ Phase 1 schemas/interfaces
       ├─ Phase 2 ledger/reducers ─┬─ Phase 3 capsule/catalog ─ Phase 4 deliberation
       │                           └─ Phase 5 action executor
       └─ media contracts/tests (可并行，不执行真实副作用)
Phase 2 + Phase 4 + Phase 5 + media contracts ─ Phase 6 media preview
Phase 4 + 5 ─ Phase 7 platform migration
All phases ─ Phase 8 evaluator/cleanup
```

可以并行：Reducer fixtures、MatrixCatalog、import graph CI、Evaluator scenario corpus、图片机 contract tests。必须串行：Schema 冻结后再写持久化；Action 状态机通过后再接真实平台；frozen MediaPlan 协议通过后再渲染；新 Runtime 完整通过 shadow replay 后再切默认。

每个阶段必须交付：

- 版本化 Schema 与错误码；
- 对应 Interface 实现；
- 事件/Reducer 清单；
- unit/integration/replay/fault tests；
- 一组可读 trace fixtures；
- 性能和 token/cost 报告；
- 旧入口删除或隔离证明；
- 阶段 exit report，列出未满足项，禁止用 TODO 假装通过。

切换策略：v2 使用独立 world ID、seed、ledger 和 projection。迁移期只允许“同一 Observation 在 shadow 环境做无副作用 replay 对比”，不允许新旧运行时双写同一世界。平台切换按内部 harness → HTTP → 非 QQ 测试 Adapter → QQ 最后接入；本轮重构测试不以 QQ 可用为前置条件。

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

### 11.1 测试分层

| 层 | 输入 | 断言 | 是否允许模型/网络 |
|---|---|---|---|
| Schema/contract | 单对象、错误枚举、版本 | parse、兼容、拒绝码 | 否 |
| Reducer unit | 初始投影 + 单事件 | 精确 projection diff | 否 |
| Event sequence | fixture 事件序列 | 事件顺序、终态、hash | 否 |
| Interface integration | fake model/provider | RuntimeOutcome、事务、幂等 | fake only |
| Replay | ledger fixture | 零外呼、相同 hash | 否 |
| Scenario | scripted user/world/NPC | 机制闭环、自然度指标 | 可用固定模型结果 |
| Model eval | 多模型真实调用 | 相对裸聊/归档基线 | 是，隔离运行 |
| Fault injection | timeout/crash/duplicate/unknown | 无重复副作用、可恢复 | fake provider |
| Performance | 热/冷 Capsule、并发 turn | P50/P95、token、成本 | 可分层 |
| Static architecture | import graph/schema ownership | 禁止依赖为零 | 否 |

### 11.2 首批权威 Fixtures

| ID | 场景 | 预期关键事件/投影 | 失败条件 |
|---|---|---|---|
| `W2-OBS-001` | 同一 QQ/HTTP event 重投 | 仅一个 ObservationRecorded | 创建两轮或两 Action |
| `W2-FACT-001` | Proposal 声称计划已完成 | claim 被弱化/拒绝 | ExperienceCommitted |
| `W2-AFF-001` | 用户分享后两次表现失望 | appraisal + 可选 episode/thread；下一轮被消费 | 完全无 trace 或被固定安慰 |
| `W2-AFF-002` | 冒犯后角色说“没事” | hurt/anger episode 仍 open/decaying | 回复发送即 resolved |
| `W2-IMP-001` | 讽刺含义不确定 | ≥2 interpretations + expiry | 写入 User Fact |
| `W2-LIFE-001` | NPC 冲突 outcome settled 后用户来信 | 唯一 npc appraisal continuation 完成；settled event/experience 进入 Capsule；可产生 affect 或显式 no-change | 世界事件无心理消费者或无合法 evidence |
| `W2-LIFE-002` | 角色临时改计划 | Plan substituted，未产生 completed experience | 直接写完成事实 |
| `W2-LIFE-003` | depleted + occupied 时选择沉默/延后 | behavior tendency + commitment/scheduled Action 或明确 no-reply | 能量标签固定映射话术；无结算悬挂 |
| `W2-LIFE-004` | paused plan 在条件恢复后 resumed | ActivityPaused → Recovery/Clock → ActivityResumed | 重建新活动或直接写 completed |
| `W2-MEM-001` | 小事当场解决 | 不创建 durable memory | 长期账本污染 |
| `W2-MEM-002` | 反复边界问题 | episode/impression/thread 有来源并持久 | 下轮完全遗忘 |
| `W2-RHY-001` | defer 后到期 | commitment + Action → terminal | 悬挂或无 Action 直接补话 |
| `W2-RHY-002` | defer 后用户插话 | 旧 Action merge/cancel/reconsider | 过时回复机械发出 |
| `W2-RHY-003` | 平台非流式热聊/冷启动 | ingress→首条 visible receipt 分段计时 | 只报告模型 TTFT 或冷启动全 replay |
| `W2-BEAT-001` | 三 beat 中用户插话 | 剩余 beat 重新审议 | 全部预排不可取消 |
| `W2-BEAT-002` | 普通单段回复 | one-beat ExpressionPlan/MessagePayload/Action 同 UoW 且 hash 一致 | 自由文本直接进 Executor |
| `W2-INT-001` | 高兴趣语义打断 | advisory 可采纳或拒绝 | 关键词直接强制打断 |
| `W2-INT-002` | 有兴趣但关系疏/打断成本高 | 模型可不打断并保留 thread | classifier 强制 Action |
| `W2-PRO-001` | 世界事件触发主动分享 | proposal + proactive budget + terminal Action | social_tasks 直接发送 |
| `W2-PRO-002` | 主动预算耗尽 | intent 延后/放弃且有终态 | 绕过预算或固定模板 |
| `W2-PULSE-001` | 未完想法稍后补一句 | thread+commitment→followup receipt | 定时器直接发无因果文本 |
| `W2-REA-001` | 情绪适合 reaction 但主模型拒绝 | 无 Action | helper 直接发送 |
| `W2-ACT-001` | provider accepted 后进程崩溃 | 对账而非重复发送 | duplicate delivery |
| `W2-ACT-002` | receipt unknown | Action terminal unknown | 自动重试 |
| `W2-ACT-003` | 乱序/重复 receipt | 去重、合法状态 reduce、冲突进对账 | 终态覆盖或重复预算结算 |
| `W2-ACT-004` | quick/parse fallback | MinimalProposal→Acceptance→reply/defer Action | 自然文本旁路 Action |
| `W2-MED-001` | media planning crash recovery | 同一 frozen plan；plan() 一次 | replanning 或 snapshot 改变 |
| `W2-MED-002` | inspection fail 两次 | 最多一次 repair 后 failed | 无限重画或发送失败图 |
| `W2-MED-003` | delivery 明确失败 | 不 shared/不开 Bid；预算结算 | 声称已发送 |
| `W2-MED-004` | delivery unknown/receipt lost | generated+reconcile；不重发 | duplicate delivery |
| `W2-MED-005` | provider accepted 后崩溃 | stable key 查询原结果 | 新 key 重新投递 |
| `W2-MED-006` | planner/inspector 主版本升级 | automatic approval 失效并回 preview | 沿用旧人工批准自动发送 |
| `W2-MED-007` | plan→render→inspect→delivery continuation | 每步唯一 TriggerProcess/Acceptance/预算/Action；crash 后 join | settlement 直接建 Action、重复 continuation、预览误发送 |
| `W2-CMEDIA-001` | 用户要求创意图 | creative pipeline；无 World experience | 伪装成角色拍摄经历 |
| `W2-VIS-001` | 图片理解含不确定对象 | result accepted 后唯一 external-result trigger；visible evidence 有界 | 猜测写 fact 或结果入账后无人消费 |
| `W2-TOOL-001` | 只读工具返回后 receipt 重投/崩溃 | 唯一 result trigger，DecisionProposal 回复或 no-action，预算终态一次 | 双回复、无回复链路、重复结算 |
| `W2-PROJ-001` | 多 viewer 投影 | 权限裁剪、project 零副作用 | 私密印象泄露/读内部表 |
| `W2-COST-001` | chat route token/调用数 | 符合测试 profile | 普通回合触发深思/多审计 |
| `W2-COST-002` | budget reserve 后执行失败 | settle/release 精确一次 | 预算泄漏/重复扣费 |
| `W2-REP-001` | 全 ledger replay | projection hash、draw、model result 一致 | 任一外呼或 hash mismatch |
| `W2-PERF-001` | 20 轮普通热聊 | 增量 Capsule、P95 ≤ 5s | 全量 replay/独立审计拖慢 |
| `W2-ARCH-001` | import graph | 只存在 4E 允许边 | Adapter 导入 reducer/旧 Engine |

### 11.3 每个机制 PR 的 Definition of Done

1. 在 7.1 对应行填入实际 Schema、事件、Reducer、消费者和 Action。
2. 提供正常、拒绝、timeout、duplicate、crash/replay 至少五类测试（纯内部机制可不含 provider timeout，但要有冲突和 replay）。
3. 给出一条 trace，能从 source evidence 追到最终 terminal state 和下一轮消费。
4. 提供 projection diff 与 rebuild hash。
5. 报告热路径延迟/token/cost 差异；超过预算必须说明并拆出后台工作。
6. 运行静态依赖检查，禁止为了赶进度旁路 `WorldRuntime`。
7. 删除或隔离旧入口；不接受“新路径可用但旧路径仍随机生效”。
8. 更新 Context/ADR/本规格中实际发生变化的领域语义。

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

1. 审阅并冻结本文的领域词汇、事件名、矩阵枚举与硬不变量；冻结前不继续扩展实现。
2. 把 7.1 的机制闭环总表映射到当前代码的真实 producer/consumer，并把缺口拆入 Phase 1–8 工作包。
3. 恢复现有 World v2 goal，从 Schema/contract tests 开始建立 `world_v2` 包与 `WorldRuntime` Interface，不搬旧 Engine 编排。
4. 第一批 interface tests：
   - ingest 用户消息只产生 observation；
   - LLM proposal 不直接改变 projection；
   - reply Action 无 receipt 不得 delivered；
   - random draw 可回放；
   - project 不调用模型或外部副作用。
5. 完成 event/reducer 与 Action 状态机后再接 Deliberation；完成 shadow replay 和性能门槛后再迁平台。
6. 继续暂停旧 Engine 上的非 P0 行为补丁，避免重构期间再次形成双权威。

## 14. 三份原计划覆盖索引

| 原计划内容 | 本文权威位置 |
|---|---|
| PLAN (2)：模型主导、WorldRuntime、行为倾向/变化幅度/行动层级、合理失控 | 2、3、4F、5.9–5.15 |
| PLAN (2)：Proposal/Projection/Executor、新纪元、机制闭环、故障恢复 | 4A–4D、7.1、10 |
| PLAN (3)：深 Module 边界、图片机 public seam、冻结机会与计划复用 | 3、4E、6.1–6.6 |
| PLAN (3)：plan exactly-once、恢复不重规划、最多一次修复、共享 snapshot | 6.5、11.2 `W2-MED-*` |
| PLAN (3)：preview 后开放投递、创意索图与事件媒体分离 | 6.2、6.6 |
| PLAN (4)：八组世界分类矩阵与完整枚举 | 5.1–5.8 |
| PLAN (4)：图片机会、规划、人物呈现三张矩阵与完整枚举 | 6.2–6.4 |
| PLAN (4)：Action 成本、预算 reserve/settle、unknown 不重试 | 4B.4、4D、5.8、9.3 |
| PLAN (4)：事实来源、appraisal 生命周期、随机 draw 记录、回放 | 4A.2、4B.3、4C.1、5.12 |
| PLAN (4)：迁移、删除、模块化 CI 与验收 | 7、8、10、11 |
| 后续讨论：情绪矩阵、用户失望、负面情绪、口是心非 | 4B.3、5.3–5.6、5.12–5.13、7.2、11.2 |
| 后续讨论：热/冷启动、延迟回复、主动脉冲、打断、多段消息 | 5.14、7.1–7.2、9.3、11.2 |
| 后续讨论：世界/NPC 影响情绪、记忆进入未来行为 | 7.1–7.2、11.2 `W2-LIFE-*` / `W2-MEM-*` |

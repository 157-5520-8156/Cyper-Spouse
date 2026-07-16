# World v2 `activity_lifecycle` 首条生产纵切：实现映射

日期：2026-07-16

状态：设计审计；尚未安装生产 lane
依据：[Life Ecology 合同](../design/world-v2-life-ecology.md)、[事件来源审计](world-v2-event-ecology-source-audit-2026-07-16.md)

## 结论

现有 Activity reducer 已能可靠执行 `planned → active → paused → active`
及终态转换，但它**不是** Life Ecology 的生产纵切：平台目前可直接调用
`WorldV2TurnApplication.transition_activity()`，而它要求一个用户
`observed_message`；`tick()` 不创建 activity trigger，也没有模型受限选择、
proposal audit、acceptance handle、重试 join 或 media follow-up。故不能把现状
称为“世界会自行生活”。

首条 lane 应只推进**已被接受的 plan**，不创建 plan、不结算 outcome、不创建
Experience。其唯一模型可选行为为从冻结的 opening 中选择一个合法 operation，或
no-op；所有 plan id、revision、事件 id、evidence、actor、时间、policy 和媒体
参数由 authority compiler 派生。

```text
ClockAdvanced (已提交)
  -> LifeEcologyRuntime.advance_once(wake_event_ref)
  -> activity_lifecycle TriggerProcess claim
  -> pinned ActivityLifecycleCapsule + admissible openings
  -> model: {opening_token | no_op}                    # 不含领域身份
  -> ActivityLifecycleProposalRecorded                 # 审计，不是 life fact
  -> ActivityLifecycleAcceptanceRuntime
  -> AcceptanceRecorded + 一个 Activity{Started|...}   # 唯一 life mutation
  -> 新 cursor 的 Capsule
  -> EventEcologyMediaCandidateRuntime.drain_once(accepted_event_ref)
```

`TriggerProcess`、审计和 completion 是 worker 的恢复元数据；唯一改变 plan
状态的写入必须是 acceptance batch 中的 Activity 事件。平台、图片机和模型均不
得到 ledger write capability。

## 已有可复用 authority

| 需要 | 现有实现 | 可复用结论 |
| --- | --- | --- |
| plan 状态及 CAS | `schemas.PlanStateProjection`、`life_reducers.transition_activity()` | 可复用；reducer 已检查 entity revision、合法前态、逻辑时钟、owner authority。 |
| Activity 事件 payload | `life_events.ActivityTransitionPayload` | 可复用；接受层应构造而非让模型提供。 |
| event contract | `event_catalog.py` 的 `ActivityStarted/Paused/Resumed/Completed/Abandoned` | 可复用；前序状态和 evidence type 已固定。 |
| durable claim/restart | `TriggerProcess`、`OutcomeTriggerRuntime` / `InteractionFactTriggerRuntime` | 可复用模式；新 kind 需显式加入 schema 和 reducer trigger validation。 |
| 模型 failure/no-op | `InteractionFactTriggerRuntime` 的 claim → bounded disposition → completion | 可复用恢复形状，但不可复用 Fact 的 message-only source resolver。 |
| accepted batch | `AcceptedLedgerBatchIssuer`、专门 acceptance runtime（如 `OutcomeAcceptanceRuntime`） | 可复用机制；Activity 不能使用 generic `DomainCompilerRegistry`，后者生产注册显式 fail-closed。 |
| visible Context | `SituationCompiler._activity_slices()` 和 Context capsule | plan 已可见；生态仍需专用、带 opening hash 的 capsule。 |
| preview 候选 | `EventEcologyMediaCandidateRuntime.drain_once()` | 可复用；只可在 acceptance 成功后，以 accepted Activity event 为 wake 调用。 |

## 不能直接复用的路径与具体原因

1. `ActivityPlanRuntime.transition()`（`activity_plan_runtime.py`）以
   `source_observation_id` 为必填入参，并总是生成 `observed_message/future_plan`
   evidence。这能服务“用户消息触发的手工 plan”，却不能证明 scheduler 的
   `ClockAdvanced`、地点或资源前置；把 tick 冒充用户 observation 会破坏来源语义。
2. `production_proposal_grammar.py` 明确将 ActivityPlan 排除在
   `DecisionProposal` 的 production grammar 之外。虽然 `proposal_envelope.py`
   语法上存在 `activity_transition`，其 `ActivityPayload` 仍要求模型写
   `activity_id`、`plan_ref`、phase；这违反 Life Ecology 合同“模型不返回身份、
   revision 和证据”的不变量。
3. `DomainCompilerRegistry` 的生产构造拒绝全部 registration；不能为图省事把
   activity 添加进该 generic registry。应像 Fact/Outcome 一样建立一个闭合、
   专用 audit/compiler/acceptance chain。
4. `PlanStateProjection.location_ref` 只是 ref，且没有资源/地点可用性 binding；
   Activity reducer 也只验证 active plan/NPC privacy。现有数据不足以证明“地点、
   资源、NPC 可用”。首发不可声称这些前置已经被验证。
5. `WorldV2TurnApplication.tick()` 只提交时钟；`
   drain_media_ecology_once()` 仍是手动 scheduler seam。故当前生产组合不会从 tick
   自动到达 Activity 或 preview。

## 最小深 Module 及接口

新增一个深 Module `LifeEcologyRuntime`，只给 application 一个入口：

```python
async def advance_life_ecology_once(
    self, *, wake_event_ref: str, trace_id: str, correlation_id: str,
) -> LifeEcologyRunResult: ...
```

外部调用者不得传 plan、operation、地点、资源、模型 seed 或 candidate。内部可由
下列私有 adapter 组成：

| 私有 adapter | 输入 → 输出 | 责任 |
| --- | --- | --- |
| `ActivityOpeningCatalog` | pinned projection + ClockAdvanced → openings | 从已接受、同 actor、非终态 plan 计算**合法**后继；冻结 catalog version/hash。 |
| `ActivityLifecycleDeliberator` | capsule + opaque openings → `selection | no_op` | 仅解析受限 schema；不得产生领域 ref。 |
| `ActivityLifecycleProposalAuthority` | selection + pinned opening → durable audit handle | 校验 token、wake、cursor、opening hash、模型审计和有效期。 |
| `ActivityLifecycleAcceptanceRuntime` | opaque handle → accepted batch | 从当前 plan/head 派生 evidence、CAS、event identity 和 policy refs；重验 opening。 |
| `ActivityLifecycleTriggerRuntime` | one process → one completed disposition | claim/reclaim、审计复用、模型失败 `deferred`、exactly-once completion。 |
| `MediaEcologyFollowup` | accepted Activity event → current media seam | best effort；媒体 unavailable/failure 绝不回滚 life mutation。 |

`LifeEcologyRunResult` 采用合同中的 `advanced | idle | joined_existing | deferred |
unavailable | rejected | failed_safe`。其中 `unavailable` 必须带 lane availability
reason；不可与没有 opening 的 `idle` 混同。

## 首发 opening 规范

首发只装 `activity_lifecycle`，且只对已有 accepted plan 产生 opening：

| 当前状态 | 可出现 operation | 至少要冻结的 authority | 首发禁止 |
| --- | --- | --- | --- |
| `planned` | `start`、`abandon` | plan head、ClockAdvanced、计划时间窗关系 | 创建新 plan、同轮 complete、虚构地点/资源到位 |
| `active` | `pause`、`complete`、`abandon` | plan head、ClockAdvanced | outcome、Experience、NPC 到场叙述 |
| `paused` | `resume`、`abandon` | plan head、ClockAdvanced | 无前态 start、连续两次 transition |
| `completed`/`abandoned` | 无 | — | 任何 reopening |

`complete` 仅代表 plan lifecycle 结束，**不**代表发生了可叙述经历或 outcome；后者
仍必须经 occurrence observation/settlement 纵切。一次 run 至多接受一个 operation。

opening token 应是对 `{world_id, wake_event_ref, plan_id, plan_revision, operation,
catalog_version, catalog_hash}` 的不透明 hash。模型只收到短 token、活动的安全摘要和
`no_op`；接受层将 token 反解/比对到内存或审计中的冻结 opening，不能相信模型提供的
任何 id。

### 前置能力的诚实降级

为了符合冻结合同，catalog 对缺少下列 authority 的情况必须返回
`blocked_by_missing_capability` 或不开放相应 opening，不能以普通 ref 猜测：

- 地点：需使 plan location 成为可验证 location authority binding，或首发仅允许
  `location_ref is None` 的 abstract activity（并在机制目录标为受限）。
- 资源：需有 plan-level resource precondition/ref 与已安装的 resource resolver。
- NPC：参与者必须已登记且 active；现有 reducer 已能检查登记/隐私，但未检查
  availability，因此有 NPC 的 social opening 在可用性前不得开放。

`clock_observation` evidence 当前可由 reducer 的 authoritative logical clock 校验；
接受 payload 还应包含 `active_plan` evidence（hash 为 canonical current plan），并将
`claim_purpose` 扩展为 `activity_lifecycle`，而非复用语义错误的 `future_plan`。

## 逐文件实现图

以下是建议的最小新文件；避免向 `ActivityPlanRuntime` 堆叠 ecology 分支。

| 文件/位置 | 变更 |
| --- | --- |
| `world_v2/life_ecology_activity.py` | `ActivityOpening`、catalog、capsule、availability、canonical hash；纯读/纯计算，单元测试主表面。 |
| `world_v2/activity_lifecycle_draft.py` | 严格模型输出 adapter：`no_op` 或 `opening_token`；审计 raw/normalized bytes，不允许领域 ids。 |
| `world_v2/activity_lifecycle_trigger.py` | 从 exact `ClockAdvanced` 建 deterministic `TriggerProcessOpened`；identity 绑定 `world_id + wake_event_ref + catalog_version`。 |
| `world_v2/activity_lifecycle_trigger_runtime.py` | claim/reclaim → audit → dedicated worker → completion；以 `OutcomeTriggerRuntime` 的 restart/CAS 形状为范本。 |
| `world_v2/activity_lifecycle_acceptance.py` | opaque pin、重新读取 plan、构造 `AcceptanceRecorded + Activity*` accepted batch；独占 life mutation writer。 |
| `world_v2/activity_lifecycle_events.py` | 专用 audit/typed proposal payload；不要污染 generic `DecisionProposal`。 |
| `schemas.py`, `event_catalog.py`, `reducers.py` | 加入 `activity_lifecycle` trigger kind、audit projection/index、event contract predecessor/evidence 更新；只增加闭合 family。 |
| `schema_core.py` | 增加 `EvidenceRef.claim_purpose == "activity_lifecycle"`；相应 validator/fixture 更新。 |
| `production_turn_application.py` | config 追加显式 `life_ecology_policy`、`activity_lifecycle_model`、worker owner；构造 `LifeEcologyRuntime`，暴露 `advance_life_ecology_once`；默认未注入返回 `unavailable`。 |
| `runtime.py` | 不把 scheduler tick 偷塞进 inbound；仅在显式配置时容纳 activity trigger worker，或保持 application 专用 worker，二选一后固定。 |
| `mechanism_closure.yaml` 和 runtime status | 只有 production trace/验证后把 lane 标为 limited-production；资源/地点/NPC 未装必须保留 limitation。 |

推荐将 worker 放在 application 的 scheduler-only seam，而非 `WorldRuntime.drain_background_once()`：
后者被 chat background 轮询复用，会让 scheduler life 与用户消息的优先级、频率和
延迟耦合。`tick` 成功后由同一 durable scheduler 显式调用 `advance_life_ecology_once`。

## 顺序与验收证据

1. **先做纯 catalog/availability。** 输入固定 projection/wake，验证状态矩阵、
   token hash、owner/privacy、time-window、无 opening、未安装 capability；无 ledger
   写入。
2. **加 trigger 与无模型 no-op。** 证明重复 scheduler/restart/concurrency 只出现一个
   process，`unavailable` 不伪装 idle，claim lease 过期可 reclaim。
3. **加 draft audit 和 acceptance。** fake model 只输出 token；验证 token 替换、
   stale plan revision、错误 wake、非法 state、模型 malformed/timeout 都零 Activity 写入。
4. **生产 trace。** `tick → advance_life_ecology_once → start accepted → 新 capsule
   显示 active → drain_media_ecology_once(accepted_event)`；同时断言只有一个
   `ActivityStarted`、一个 preview candidate/opportunity，零 media planning/render/send。
5. **恢复与隔离。** audit 后重启、acceptance 前 crash、acceptance 后 completion 前 crash、
   media sidecar failure、media unavailable，分别验证 exactly-once 或保留可恢复状态；
   不允许旧 `ActivityPlanRuntime.transition()` 成为 production scheduler bypass。
6. **再扩展前置能力。** 先完成 location/resource bindings，再启用有地点/资源的
   openings；NPC/social、occurrence observation、VisualFact 另列纵切，不能用本 lane
   的 `complete` 伪造。

必跑范围：新增 unit/authority/restart/production fixture，`pytest tests/world_v2 -q`，
`ruff check src/companion_daemon/world_v2 tests/world_v2`，冻结 scenario replay/hash，
以及静态断言 application scheduler 不从 inbound 调用 life ecology。

## 实施前必须作出的一个组合决策

当前文档的“地点/资源前置”比现存 plan authority 更强。首发必须二选一并在 catalog
availability 体现：

1. 先实现 plan 的 location/resource verified bindings，再开放任何 start/resume；或
2. 仅安装不要求这些前置的 abstract、无 NPC、无 location/resource plan，并明确标为
   limited-production。

选择 2 可以较快验证完整的 proposal→acceptance→preview 闭环，但不能声称已经满足
完整生活生态；选择 1 才能兑现合同的完整前置语义，代价是多一个 authority 纵切。

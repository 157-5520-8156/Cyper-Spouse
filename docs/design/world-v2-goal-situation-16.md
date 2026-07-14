# World v2 `.16`：Goal / Location / Resource / Attention authority 与 Situation 确定性编译

> 状态：只读威胁建模与最小可执行设计；不是实现完成声明。
> 目标 bundle：`world-v2-reducers.16`。
> 前置条件：CharacterCore `.15` 已完成、验收并提交后才能合并 `.16`。
> 上位规格：[world-v2-refactor-plan.md](../world-v2-refactor-plan.md) 4B.10、10.9、11.2、13。若本文与上位规格冲突，以上位规格为准。

## 1. 结论

`.16` 不是一个新的“大世界状态机”，而是五个有明确 seam 的 Module：

1. `GoalAuthority`：角色想推进什么结果；
2. `LocationAuthority`：角色当前在哪里及场景可见性；
3. `ResourceAuthority`：角色当前有限资源；
4. `AttentionAuthority`：角色当前注意状态；
5. `SituationCompiler`：把以上 authority 与既有 Clock、Plan/Activity、NPC/Occurrence、Commitment 编译成只读处境。

前四个是独立写 authority，各有自己的 before/after、CAS、history 和补偿；其中人为/模型选择的 mutation 走 typed proposal family，Clock mechanical mutation 明确不属于 typed family。第五个没有写事件、没有 reducer、没有独立 reducer bundle，只暴露一个纯函数式 Interface：

```text
SituationCompiler.compile(request: SituationCompileRequest) -> SituationProjection
```

这个 Interface 隐藏 source selection、缺失处理、分类 catalog、隐私 meet、排序、截断、semantic hash 和缓存校验。调用方不能逐字段拼 Situation，也不能把 LLM 输出塞回编译结果。

## 2. 非目标与硬边界

`.16` 不做以下事情：

- 不把 Goal、Activity 和 Occurrence 合并；
- 不让低资源自动产生 sadness，不让 blocked goal 自动产生 anger；
- 不让 `do_not_disturb` 自动变成“不回复”；
- 不让 location scene visibility 自动变成对用户的披露许可；
- 不从用户陈述、图片背景、NPC 猜测或旧 prompt 文本直接改 Location；
- 不把进度达到 `10000` 隐式改成 completed；
- 不读取 wall clock，不调用模型、随机、网络或外部工具；
- 不引入 `SituationChanged`、`SituationAccepted` 或 Situation history；
- 不把编译缓存写成 authority；
- 不在任一 reducer 中级联修改 Affect、Relationship、Memory、CharacterCore、Action 或其他三个 `.16` projection；
- 不在 `.16` 接 QQ；平台 Adapter 最后迁移。

受控随机仍属于 Deliberation：如果角色在多个目标或活动间随机选择，必须先有 `RandomDrawRecorded`，proposal 绑定 draw、候选集 hash、catalog/sampler version。四个 reducer 和 SituationCompiler 都不抽样。

## 3. Module、Interface 与 seam

| Module | 外部 Interface | 隐藏的 implementation | 依赖类别 | 主要测试 seam |
|---|---|---|---|---|
| `GoalAuthority` | typed proposal codec + `reduce_goal(...)` | 状态机、completion/Clock authority、CAS、补偿、lineage | in-process | proposal → acceptance → event → head/history |
| `LocationAuthority` | typed proposal codec + `reduce_location(...)` | single-head、cause binding、scene classification、privacy floor、CAS、补偿 | in-process | exact before/after 与 source authority |
| `ResourceAuthority` | typed proposal codec + `reduce_resource(...)` | 定点运算、band policy、Clock interval、CAS、补偿 | in-process | delta conservation 与 deterministic band |
| `AttentionAuthority` | typed proposal codec + `reduce_attention(...)` | mode/focus 约束、expiry、Clock authority、CAS、补偿 | in-process | lifecycle、time 与 exact latest |
| `SituationCompiler` | `compile(request)` | source matrix、分类、隐私、排序、截断、hash、cache validation | in-process | 相同 pinned input 必须 byte-equivalent |
| `SQLiteWorldLedger` | 现有 open/commit/project/rebuild | `.15→.16` verified migration | local-substitutable | 非空 SQLite reopen/rebuild/tamper |

不为 reducer 再包一层 pass-through “manager”。proposal registry、batch invariants 和 Ledger 是既有 seam；新 Module 直接注册到这些 seam。

## 4. 共享 authority envelope

四个 deliberative/operator family 复用当前 typed-authority 模式，但不能共用含糊的 generic payload。

```text
AuthorizedMutationEnvelope
  change_id
  transition_id
  expected_entity_revision
  evidence_refs[]
  policy_refs[]
  acceptance_id
  proposal_id
  evaluated_world_revision
  accepted_change_hash
```

共同不变量：

- `ProposalRecorded` 必须单独 commit，并 pin 当前 `world_revision`；
- proposal 内保存 canonical typed mutation JSON，不保存任意 dict；
- `AcceptanceRecorded` 与唯一 domain mutation 必须相邻、同一 commit；
- acceptance 的 proposal/change/hash/revision 必须逐项等于 mutation；
- accepted mutation 必须紧随最新 acceptance；
- `candidate_before == current head`，而非只比较 revision；
- `expected_entity_revision == before.entity_revision`；首次写入必须为 `0→1`；
- after revision 必须连续加一；
- event ID 必须等于 after origin 的 `accepted_event_ref`；
- transition ID、change ID、proposal ID、acceptance ID 全局不可重用；
- semantic fingerprint、source/cause digest 和 change hash 均用 canonical JSON、稳定排序与 UTC 时间；
- reducer 只改变自己的 head/history，并消费自己的 pending proposal；
- failed commit 对 head、history、proposal、acceptance、committed refs 全部原子回滚。

Clock mechanical lane 不制造 `ProposalRecorded/AcceptanceRecorded`，其事件也不得注册进 typed proposal family 的 `mutation_event_types`。它使用 domain-specific typed authority，仍必须绑定 exact `ClockAdvanced` event ID、world revision、payload hash、from/to logical time、policy version/digest 和目标 before image。其 idempotency key 固定为：

```text
sha256(world_id + event_type + operation + target_identity + before_revision + clock_event_ref + policy_digest)
```

同一 Clock authority 对同一目标只能结算一次。recovery 可重放同一确定性事件，不能生成新随机 ID 或改写结果。Clock 只有两种合法输出：打开 deterministic due trigger，或提交 payload 中已经冻结、且 reducer 能按 pinned policy 复算的 exact after image；它不能临时决定角色接下来想做什么。

### 4.1 冻结的 proposal routing

四个显式 proposal family 使用以下唯一 selector 与合同；机械事件不在表内：

| Domain | `proposal_kind` | `authority_contract_ref` | typed mutation events |
|---|---|---|---|
| Goal | `v2_goal_transition` | `proposal-contract:v2-goal.1` | `V2GoalOpened`、`V2GoalRevised`、`V2GoalProgressed`、`V2GoalPaused`、`V2GoalResumed`、`V2GoalBlocked`、`V2GoalUnblocked`、`V2GoalCompleted`、`V2GoalAbandoned`、`V2GoalTransitionCompensated` |
| Location | `v2_location_transition` | `proposal-contract:v2-location.1` | `V2LocationChanged`、`V2LocationChangeCompensated` |
| Resource | `v2_resource_transition` | `proposal-contract:v2-resource.1` | `V2ResourceStateInitialized`、`V2ResourceStateAdjusted`、`V2ResourceTransitionCompensated` |
| Attention | `v2_attention_transition` | `proposal-contract:v2-attention.1` | `V2AttentionChanged`、`V2AttentionTransitionCompensated` |

四个 family 均以 `ProposalRecorded` 为 record event，并分别拥有 concrete `*ProposalProjection`、canonical `*ProposedMutation`、codec 与 proposal store。selector `(ProposalRecorded, proposal_kind)`、contract ref、mutation event owner 必须全局唯一。`V2GoalExpired`、`V2ResourceClockAdjusted` 和 `TriggerProcessOpened(process_kind=v2_attention_expiry_due)` 由 mechanical payload map 与普通 event catalog 注册，不经过 proposal registry。

### 4.2 可选受控随机 binding

任何由受控随机选出的 proposal 都在 domain mutation 中携带以下可选字段，字段进入 canonical mutation hash：

```text
RandomDrawBinding
  draw_event_ref
  draw_world_revision
  draw_payload_hash
  attempt_id
  candidate_set_hash
  selected_candidate_ref
  catalog_version
  sampler_version
  supersedes_draw_ref?
```

Reducer 必须 exact resolve 已提交 `RandomDrawRecorded`，验证候选集、选中项、attempt、catalog/sampler 与 supersede lineage。没有发生随机选择时该字段必须为空；存在随机选择却缺 binding 时 proposal 拒绝。CAS retry 复用同一 draw，world revision 改变导致候选集变化时必须显式 supersede，不能通过拒绝 proposal 刷结果。

### 4.3 统一时间 pin

所有 after image 的 `updated_at`、transition 的 `accepted_at` 必须等于 domain event 的 Logical Time。首次建立的 `opened_at/since` 必须等于建立事件时间；terminal `closed_at` 必须等于 terminal event 时间。普通 transition 不得回写 `opened_at/since`，不得让任何生命周期时间晚于 `updated_at`。Clock mechanical after image 使用所绑定 Clock 的 `logical_time_to`，且 domain event Logical Time 必须与它相等。

## 5. 投影合同

### 5.1 Goal

```text
GoalProjection
  goal_id
  actor_ref
  entity_revision
  semantic_fingerprint
  values {
    outcome_ref
    importance_bp              # 0..10000
    progress_bp                # 0..10000
    due_window? {starts_at, ends_at}
    blocker_refs[]             # canonical unique sorted refs
    privacy_class
    completion_contract_ref?
    completion_contract_digest?
    status                     # active|paused|blocked|completed|abandoned|expired
    terminal_reason_ref?
    supersedes_goal_id?
  }
  origin {change_id, transition_id, policy_refs, accepted_event_ref}
  opened_at
  updated_at
  closed_at?

GoalTransitionProjection
  transition_id, goal_id, entity_revision, operation
  values_before?, values_after
  semantic_fingerprint_after
  change_id, policy_refs, accepted_event_ref, accepted_at
  cause_authority?
  revise_kind?
  random_draw_binding?
  compensates_transition_id?
```

`outcome_ref` 是想实现的结果引用，不是已发生 Fact。它与 actor/goal identity 在 Open 后不可变；目标含义变化时创建新 Goal 并用 `supersedes_goal_id` 串联。`importance_bp`、`due_window`、completion contract 可通过显式 `revise_kind=reprioritize|reschedule|recontract` 修改并保留 before/after；没有 completion contract 的 Goal 不可 Complete，只能 Abandon/Expire。terminal Goal 永不 reopen。

状态机：

| 当前 | 操作 | 下一状态 | 额外条件 |
|---|---|---|---|
| 无 | open | active | rev `0→1`；初始 progress 可为 0..10000；记录初始 contract/due |
| active/paused/blocked | revise | 原状态 | 只改 revise kind 允许的字段；outcome/identity/progress 不变 |
| active/paused/blocked | progress | 原状态 | 只接受 settled external/domain progress；非负 delta；before + delta = after；after ≤ 10000 |
| active | pause | paused | reason ref；不改 progress |
| paused | resume | active | 明确 resume evidence |
| active | block | blocked | 新 blocker 非空；blocker set 有实际变化 |
| blocked | unblock | blocked/active | exact non-empty removal diff；仍有 blocker 则保持 blocked，为空才 active |
| blocked | block | blocked | 只允许增加/替换有 authority 的 blocker refs |
| active/paused/blocked | complete | completed | exact completion contract + settled evidence；closed_at=logical time |
| active/paused/blocked | abandon | abandoned | deliberative/operator reason；closed_at=logical time |
| active/paused/blocked | expire | expired | exact Clock authority 到达 due end；closed_at=clock logical time |
| latest non-open transition | compensate | 显式 restored head | exact latest transition；生成新 revision，不删除历史 |

达到 `progress_bp=10000` 仍保持原状态，直到独立 `V2GoalCompleted`。`recontract` 不可用来把尚未满足的 completion 事后改写为已经满足：新 contract 的 evidence cutoff 必须晚于 revision 前的最新 completion evidence，或由 deployment ActorAuthority 明确纠错；否则创建 superseding Goal。

### 5.2 Location

```text
LocationProjection
  actor_ref                    # actor 单 current head
  entity_revision
  semantic_fingerprint
  values {
    location_ref
    zone_ref?
    scene_visibility           # private|shareable|public；物理场景分类
    privacy_class              # 精确地点信息的 disclosure ceiling
    since
  }
  origin
  updated_at

LocationTransitionProjection
  transition_id, actor_ref, entity_revision
  operation=establish|change|compensate
  values_before?, values_after
  cause_authority
  accepted_event_ref, accepted_at, compensates_transition_id?
```

`scene_visibility` 是场景事实，不是披露授权；从 private 场所移动到 public 场所可以由合法 settled movement authority 自然改变它，不需要“放宽隐私”的授权。`privacy_class` 单独约束是否向某 viewer 暴露精确 `location_ref/zone_ref`，不得弱于来源 privacy。`since` 表示进入该 location 的 logical time，`updated_at` 表示 authority transition 时间。首次 `V2LocationChanged(operation=establish)` 只允许持有 active deployment ActorAuthority 的 operator initialization，before 为 `None`；不设隐式 seed lane。后续必须携带 exact from/to；同 location 只改 scene visibility、privacy 或 zone 仍算 change，不可静默覆盖。

### 5.3 Resource

```text
ResourceProjection
  actor_ref
  resource_kind                # physical_energy|cognitive_capacity|social_capacity
  entity_revision
  semantic_fingerprint
  values {
    value_bp                   # 0..10000
    derived_band               # policy-derived
    band_policy_version
    band_policy_digest
  }
  origin
  updated_at

ResourceTransitionProjection
  transition_id, actor_ref, resource_kind, entity_revision
  operation=initialize|adjust|clock_adjust|compensate
  adjust_kind=state_change|reclassify?
  value_before?, delta_bp?, value_after
  band_before?, band_after
  cause_authority
  policy_version, policy_digest
  accepted_event_ref, accepted_at, compensates_transition_id?
```

首发 resource kind 是闭集。财务 Budget、关系资源、need、Affect 不得塞入此 projection。普通调整必须满足 `before + delta == after` 且结果在范围内；越界拒绝，禁止 clamp。`derived_band` 只能由事件中 pinned 的 `resource-band-policy.1` 对 value 确定性计算，policy version/digest 是 committed values 的一部分。SituationCompiler 消费 committed band 并验证其 pinned policy；它不得用“当前 policy”静默重算。band policy 变化需要显式 `V2ResourceStateAdjusted` reclassification（delta=0 但 policy/band 有真实变化）或 bundle migration。

`V2ResourceClockAdjusted` 携带 exact before/delta/after、Clock interval、recovery input refs/digest 与 frozen recovery policy。Runtime 用该 immutable policy 计算，reducer 用相同纯函数复算；不支持的 policy fail closed。恢复输入只能是 Clock 与在 Clock 开始 revision 已提交的 activity/rest authority，不能在 reducer 内推测“角色应该休息好了”。

### 5.4 Attention

```text
AttentionProjection
  actor_ref                    # actor 单 current head
  entity_revision
  semantic_fingerprint
  values {
    mode                       # available|glancing|occupied|deep_focus|do_not_disturb|recovering_attention
    focus_ref?
    allocation_bp              # 0..10000
    interruptibility_bp        # 0..10000
    since
    expires_at?
  }
  origin
  updated_at

AttentionTransitionProjection
  transition_id, actor_ref, entity_revision
  operation=establish|change|compensate
  values_before?, values_after
  cause_authority
  accepted_event_ref, accepted_at, compensates_transition_id?
```

`expires_at` 只使重新审议变为 due，不在查询时临时改 head，也不机械重置为 `available`。Clock 到期时只确定性打开唯一 `TriggerProcessOpened(process_kind=v2_attention_expiry_due)`，绑定 exact Attention before revision、Clock event/ref/hash 和 expiry policy digest；Attention head 保持不变并在 Situation 中显示 `transition_due=true`。主 Deliberation 随后可以提 `V2AttentionChanged`（继续、放松、转为 recovering、清除 focus 等）或明确 no-change/完成 trigger。这样 Clock 只授权“该重审了”，不替角色选择后续注意状态。

## 6. 事件与 authority lane 矩阵

### 6.1 Lane 定义

| Lane | 谁可产生候选 | 是否 Proposal/Acceptance | 可引用 authority | 禁止事项 |
|---|---|---|---|---|
| `deliberative` | 主 Deliberation | 是 | committed Fact/Experience/Plan/Occurrence/Action receipt、active Goal 等 | 用户一句话直接成世界状态；自证 completion |
| `operator` | 明确 operator command Adapter | 是 | active deployment `ActorAuthorityProjection` + domain required operation；OperatorObservation 仅审计 | 借 operator observation 伪造授权；借 operator lane 接普通用户消息 |
| `settlement` | Runtime 对已提交 settled domain/external result 的确定性 adapter | 是；settlement 先 commit，下一 world revision 才记录 proposal | exact committed settled event/receipt | prospective/future evidence；provider accepted 冒充 settled；从 pending Action 改状态 |
| `clock_runtime` | `advance()` / recovery | 否；domain-specific mechanical contract | exact latest applicable `ClockAdvanced` | wall clock、LLM、随机、查询时隐式变化 |
| `compensation` | operator 或 domain correction Deliberation | 是 | exact latest transition + correction evidence | 回滚历史、补偿非 latest、跨 identity 补偿 |

### 6.2 Event catalog

| Domain | Event | Operation | Lane | 必需 authority | Reducer 只写 |
|---|---|---|---|---|---|
| Goal | `V2GoalOpened` | open | deliberative/operator | outcome source、policy、可选 due/contract | goal head/history |
| Goal | `V2GoalRevised` | revise | deliberative/operator | revise kind + exact before/after | goal head/history |
| Goal | `V2GoalProgressed` | progress | settlement | prior-revision exact settled cause + before/delta/after | goal head/history |
| Goal | `V2GoalPaused` | pause | deliberative/operator | reason evidence | goal head/history |
| Goal | `V2GoalResumed` | resume | deliberative/operator | resume evidence | goal head/history |
| Goal | `V2GoalBlocked` | block | deliberative/settlement | blocker additions/replacements 均可解析 | goal head/history |
| Goal | `V2GoalUnblocked` | unblock | deliberative/settlement | exact non-empty resolved blocker diff | goal head/history |
| Goal | `V2GoalCompleted` | complete | settlement/operator | frozen completion contract + prior-revision settled evidence | goal head/history |
| Goal | `V2GoalAbandoned` | abandon | deliberative/operator | reason evidence | goal head/history |
| Goal | `V2GoalExpired` | expire | clock_runtime | exact Clock + frozen due end + policy digest + exact expired after | goal head/history |
| Goal | `V2GoalTransitionCompensated` | compensate | compensation | exact latest non-open transition | goal head/history |
| Location | `V2LocationChanged` | establish/change | operator/deliberative/settlement | exact cause union + from/to；establish 仅 operator | location head/history |
| Location | `V2LocationChangeCompensated` | compensate | compensation | exact latest transition | location head/history |
| Resource | `V2ResourceStateInitialized` | initialize | operator | active ActorAuthority + band policy | resource head/history |
| Resource | `V2ResourceStateAdjusted` | adjust/reclassify | deliberative/operator/settlement | exact cause + before/delta/after + pinned band policy | resource head/history |
| Resource | `V2ResourceClockAdjusted` | clock_adjust | clock_runtime | exact Clock interval + frozen recovery inputs/policy + exact after | resource head/history |
| Resource | `V2ResourceTransitionCompensated` | compensate | compensation | exact latest transition | resource head/history |
| Attention | `V2AttentionChanged` | establish/change | operator/deliberative/settlement | exact cause + before/after；establish 仅 operator | attention head/history |
| Attention | `TriggerProcessOpened(v2_attention_expiry_due)` | due trigger | clock_runtime | exact Clock + attention revision/expires_at + policy | trigger process only；Attention 不变 |
| Attention | `V2AttentionTransitionCompensated` | compensate | compensation | exact latest transition | attention head/history |

Situation 没有任何 catalog event。

### 6.3 Cause authority union

共享字符串 `cause_ref` 不足以授权变化。每个 payload 使用 discriminator union：

```text
DeliberativeCauseAuthority
  kind=accepted_deliberation
  evidence_refs[]

DomainOperatorAuthorityBinding
  kind=deployment_actor_authority
  authority_id, authority_revision, principal_ref
  authority_event_ref, authority_world_revision, authority_payload_hash
  authority_values_hash, authority_policy_digest
  authorization_contract
  required_operation
  audit_observation_ref?          # 仅审计，不参与授权

SettledEventCauseAuthority
  kind=settled_event
  event_ref, event_type, world_revision, payload_hash

ClockCauseAuthority
  kind=clock
  clock_event_ref, clock_world_revision, clock_payload_hash
  logical_time_from, logical_time_to
  policy_version, policy_digest

CompensationCauseAuthority
  kind=compensation
  target_transition_id, target_entity_revision
  correction_evidence_refs[]
```

每个 operator mutation 都必须解析当前 active `ActorAuthorityProjection`：principal kind 为 deployment operator、required operation 存在、authority 未过期、values/policy digest 和 committed event 完全匹配。四域 required operation 分别冻结为 `v2_goal_governance|v2_location_governance|v2_resource_governance|v2_attention_governance`。`OperatorObservationRecorded` 只能作为 `audit_observation_ref`，单独存在永远不能授权 mutation。补偿若撤销 operator-lane transition，也必须携带当前有效的同域 ActorAuthority；过期/撤销的旧授权不能借 compensation 复活。

domain reducer 还要限制允许的 kind 和 event type。例如图片 inspection result 不能成为 Location change；`ExecutionReceiptRecorded` 只有 terminal settled receipt 才能成为 Goal completion 或 Resource adjustment evidence。Deliberative evidence 可以支持角色“选择改变目标/注意”，但不能把用户或模型的一句话升级成 Location、settled progress 或 completion 事实。

## 7. Settlement 两阶段与跨 projection 原子批次

Settlement authority 固定采用两阶段，不引入 prospective binding：

```text
world revision N:
  ActivityCompleted / WorldOccurrenceSettled / terminal receipt committed

deliberation revision(s), evaluated_world_revision=N:
  ProposalRecorded(V2ResourceStateAdjusted, cause = exact committed settlement)
  ProposalRecorded(V2GoalProgressed, cause = exact committed settlement)
  ProposalRecorded(V2AttentionChanged, cause = exact committed settlement)

world revision N+1 atomic commit:
  AcceptanceRecorded(resource) → V2ResourceStateAdjusted
  AcceptanceRecorded(goal)     → V2GoalProgressed
  AcceptanceRecorded(attention)→ V2AttentionChanged
```

规则：

1. Settlement 必须先成为 `CommittedWorldEventRef`；proposal 不得引用未来 event ID，也不得因“计划同批提交”跳过 source resolver；
2. 每个 domain change 有独立 mutation hash、proposal、acceptance 和 CAS；
3. 需要共同生效的 downstream changes 放进一个普通 Ledger commit，按现有 batch invariant 排成相邻 acceptance/mutation pairs；不引入新的 UoW manifest schema；
4. 任一 before image、source authority、Acceptance 或 hash 失败则 downstream commit 整体失败；已经在 revision N 提交的 settlement 不回滚；
5. 未列出的 projection 保持 byte-equivalent；
6. Settlement 已发生但某些 downstream proposal 未被接受是合法状态：相应 projection 保持不变，不能伪称已调整；
7. retry 复用 proposal、draw（若有）和 event identity；world revision 已变化则旧 proposal stale，重新 deliberation；
8. 如果三个 downstream changes 不要求共同生效，也可分别 commit，但 trace 必须明确哪些已经结算，禁止 reducer 自动补齐。

## 8. SituationCompiler 的确定性边界

### 8.1 唯一 Interface

```text
SituationCompileRequest
  world_id
  actor_ref
  pinned_world_revision
  logical_time
  authority_snapshot
  policy {
    situation_policy_version
    time_segment_catalog_digest
    resource_pressure_policy_digest
    privacy_policy_digest
    ordering_policy_digest
    budget_policy_digest
  }
  viewer_scope                   # internal 或 exact viewer/privacy grant

SituationCompiler.compile(request) -> SituationCompileResult
```

`authority_snapshot` 是同一 `pinned_world_revision` 的 immutable input，只包含编译需要的 heads/refs。Compiler 不持有 Ledger，不自行读取“最新状态”。这样测试和调用方跨同一个 seam，缓存命中与否不改变结果。Resource band 来自 head 自带的 pinned band policy；request policy 只负责 Situation 聚合，不能重分类 domain authority。

### 8.2 输出

```text
SituationProjection
  compiled_at_world_revision
  actor_ref
  logical_time
  time_segment
  location_slice
  activity_slices[]
  goal_slices[]
  resource_slices[]
  resource_pressure
  attention_slice {committed_head, transition_due, due_trigger_ref?}
  social_environment
  plan_relation
  open_commitment_refs[]
  scene_visibility
  source_revisions{}
  policy_versions{}
  internal_semantic_hash

ViewerSituationProjection
  source_internal_semantic_hash
  viewer_scope_digest
  redacted_slices
  truncation_reasons[]
  capsule_budget_policy_digest
  viewer_projection_hash

SituationCompileResult
  internal?                     # 只向 internal viewer 返回
  viewer_projection
```

每个可缺失 slice 使用显式 availability：

```text
availability = available | unavailable | redacted
reason = no_authority | not_applicable | privacy_ceiling | budget_truncated
```

不得用空字符串、默认地点、默认 Goal 或 LLM 补全替代 unavailable。

### 8.3 字段唯一来源矩阵

| 输出字段 | 唯一 authority/source | 确定性变换 | 缺失行为 | 隐私 |
|---|---|---|---|---|
| logical time | ReducerState Logical Clock | 原值 | world 未启动则 unavailable | internal |
| time segment | logical time + versioned catalog | catalog lookup | catalog 缺失为 hard error | 可披露分类，不披露原始私密 schedule |
| location ref/zone | actor Location head | 原值 | unavailable | 按 location privacy + viewer policy redacted |
| location scene visibility | Location head | 原值；不是披露许可 | unavailable | 精确 location disclosure 另按 privacy meet |
| activities | active lifecycle-valid Plan/Activity heads | stable sort `(importance desc, id asc)` | 空列表 | 每项 privacy filter |
| participants | Plan/Occurrence/NPC exact refs | 去重稳定排序 | 不从对话猜测 | participant 最严格 privacy |
| goals | non-terminal Goal heads | stable sort `(importance desc, due relation, id)` | 空列表 | 每 Goal privacy filter |
| goal due relation | Goal frozen due window + logical time | `none|future|open|overdue` | due 缺失为 none | 继承 Goal privacy |
| resources | actor Resource heads | 消费 committed band；验证 head pinned policy 可识别，绝不按当前 policy 重算 | 每个缺 kind显式 unavailable；未知 source policy fail closed | 默认 internal；viewer 需明确 policy |
| resource pressure | available resource values + policy | catalog 聚合，不触发行为 | 无资源为 unavailable | 继承最严格 resource visibility |
| attention | actor Attention head + active exact `v2_attention_expiry_due` TriggerProcess | head 原值；只有 trigger 精确绑定 current head 时 `transition_due=true` | head 缺失为 unavailable；stale trigger 忽略并保留审计 | viewer 默认只给粗粒度 mode 或 redacted |
| social environment | active participants + location class | catalog 分类 | unavailable/alone 仅在证据充分时 | participant/privacy meet |
| plan relation | Plan schedule/status + Clock + latest explicit transition | catalog 分类 | unavailable | 继承 Plan privacy |
| commitments | open Commitment heads | stable sort `(due, id)` | 空列表 | 每项 privacy filter |
| scene visibility | location、participants、privacy policy | 最严格 meet | unavailable | 不可弱化 |
| source revisions | 所有被消费 head/event | canonical map | 不允许遗漏已输出字段来源 | internal audit；viewer 可省略敏感 ID |

### 8.4 纯度、hash 与缓存

Compiler 必须满足：

- 禁止 `datetime.now()`、`random`、模型、网络、文件系统和环境变量；
- 所有集合排序规则写入 policy version；
- 定点整数，不使用平台相关浮点；
- `internal_semantic_hash` 排除 cache timestamp、性能计数、viewer、Capsule budget、redaction 与 truncation；其 material 包含 world/actor/revision、logical time、完整 internal 语义字段、source revisions 和 Situation policy digests；
- `viewer_projection_hash` 绑定 `internal_semantic_hash`、viewer scope digest、redacted output、budget policy 和 truncation reasons；不同 viewer/budget 可以有不同 projection hash，但不能改变 internal identity；
- 相同 request 必须产生 canonical JSON byte-equivalent 输出；
- internal cache key 等于 authority snapshot + Situation policy input hash；viewer cache key 等于 internal hash + viewer/budget input hash；cache value 必须重新校验对应 hash；
- cache miss 只影响延迟；cache hit 不得跳过 privacy projection；
- Internal 与 viewer projection 使用不同 viewer scope digest，不能共用 cache entry；
- Compiler 不写 Ledger。Snapshot 可携带编译结果，但 rebuild 时必须可从 pinned heads 重算。

### 8.5 与行为/情绪的 seam

Situation 是 evidence-bearing context，不是行为规则：

```text
SituationProjection
  ├─> Appraisal/Inner Advisory：可解释为压力、阻碍、公共场景等候选
  └─> 主 Deliberation：与 CharacterCore、Affect、Relationship、输入共同选择
```

以下映射一律禁止进入 Compiler 或 reducer：

- `low energy -> sadness`；
- `blocked goal -> anger`；
- `deep focus -> no reply`；
- `public location -> refuse intimacy`；
- `available -> immediate reply`。

模型可以结合这些处境形成不同选择，包括察觉但不安慰、在意所以修复、因目标冲突而延后、因关系不近而略过。选择本身仍受 Action/Expression authority 约束。

## 9. 威胁模型与攻击测试

### 9.1 Proposal、Acceptance、CAS

| ID | 攻击 | 预期拒绝/结果 |
|---|---|---|
| GS16-AUTH-001 | proposal evaluated revision 不是当前 revision | ProposalRecorded 拒绝；零残留 |
| GS16-AUTH-002 | acceptance hash 与 mutation hash 不同 | 批次拒绝；proposal 保留，head 不变 |
| GS16-AUTH-003 | acceptance 与 mutation 不相邻 | 批次拒绝 |
| GS16-AUTH-004 | mutation 指向另一个 proposal/change ID | 拒绝 |
| GS16-AUTH-005 | stale before image 但 revision 相同 | exact before-image 拒绝 |
| GS16-AUTH-006 | expected revision 正确但 current semantic head 不同 | CAS 拒绝 |
| GS16-AUTH-007 | event ID 不等于 accepted_event_ref | 拒绝 |
| GS16-AUTH-008 | duplicate proposal/transition/change/event ID | 拒绝或严格幂等；不得二次变化 |
| GS16-AUTH-009 | failed multi-domain UoW 只落一半 | 整批回滚 |
| GS16-AUTH-010 | 只有 OperatorObservation、无 active domain ActorAuthority | operator mutation 拒绝 |
| GS16-AUTH-011 | ActorAuthority operation/expiry/principal/values digest 不匹配 | 拒绝 |
| GS16-AUTH-012 | 随机选择缺 draw binding、候选集被篡改或 retry 重抽 | 拒绝；CAS retry 复用 exact draw |
| GS16-AUTH-013 | after.updated_at/opened_at/since/closed_at 不等 event/Clock time | 拒绝 |
| GS16-AUTH-014 | mechanical event 被注册为 typed mutation 或携带 Acceptance | registry/contract test 失败 |
| GS16-AUTH-015 | legacy `GoalProgressed/Resumed/Abandoned/Compensated` payload 进入 v2 | unknown event/contract 拒绝；只接受冻结的 `V2*` 名称 |

### 9.2 Goal

| ID | 攻击 | 预期 |
|---|---|---|
| GS16-GOAL-001 | progress delta 为负、溢出或 before+delta≠after | 拒绝；禁止 clamp |
| GS16-GOAL-002 | progress 到 10000 自动 completed | head 仍非 terminal |
| GS16-GOAL-003 | completion contract 在 Complete 时被替换 | 拒绝 |
| GS16-GOAL-004 | pending Action/provider accepted 当 completion evidence | 拒绝 |
| GS16-GOAL-005 | settled evidence hash/revision/event type 不匹配 | 拒绝 |
| GS16-GOAL-006 | terminal goal pause/resume/progress/reopen | 拒绝 |
| GS16-GOAL-007 | Expired 使用 wall clock、非 latest applicable Clock 或未到 due | 拒绝 |
| GS16-GOAL-008 | compensation 指向非 latest、其他 goal 或 open transition | 拒绝 |
| GS16-GOAL-009 | blocker ref 不存在、跨 world 或 aliases 另一 authority | 拒绝 |
| GS16-GOAL-010 | Goal event 改 Affect/Memory/Attention | zero-cascade diff 失败 |
| GS16-GOAL-011 | 多 blocker 只解除一个却被强制 active | 保持 blocked；exact diff 保留其余 blocker |
| GS16-GOAL-012 | paused/blocked goal 收到 prior-revision settled progress | 可 progress 且状态不隐式变化 |
| GS16-GOAL-013 | revise 偷改 outcome/identity/progress 或事后伪造 completion contract | 拒绝 |

### 9.3 Location

| ID | 攻击 | 预期 |
|---|---|---|
| GS16-LOC-001 | 普通用户说“你在学校”直接 V2LocationChanged | evidence authority 拒绝 |
| GS16-LOC-002 | 图片背景/未 settled vision result 改 Location | 拒绝 |
| GS16-LOC-003 | from image 与 current head 不等 | 拒绝 |
| GS16-LOC-004 | 同 actor 建立第二个 rev1 head | 拒绝 |
| GS16-LOC-005 | 合法移动改变 scene_visibility，但尝试同时弱化 location privacy_class | scene 可变；privacy 弱化拒绝 |
| GS16-LOC-006 | since 晚于 updated_at 或倒退 | 拒绝 |
| GS16-LOC-007 | location scene visibility 被 viewer 当披露 grant | viewer projection 必须仍按 privacy_class redacted |

### 9.4 Resource

| ID | 攻击 | 预期 |
|---|---|---|
| GS16-RES-001 | 未知 resource kind 或财务 Budget 混入 | schema 拒绝 |
| GS16-RES-002 | delta 守恒错误、范围越界、静默 clamp | 拒绝 |
| GS16-RES-003 | LLM 自报 derived band | reducer 按 policy 重算，不同则拒绝 |
| GS16-RES-004 | Clock recovery interval 与 event payload 不同 | 拒绝 |
| GS16-RES-005 | 同一 Clock 对同一 resource 重复恢复 | identity/idempotency 拒绝 |
| GS16-RES-006 | recovery policy digest 被换、跨 bundle 重解释 | 拒绝/replay hash 不同并报错 |
| GS16-RES-007 | Resource adjustment 自动写 Goal/Affect | zero-cascade diff 失败 |
| GS16-RES-008 | Compiler 用当前 band policy 重算旧 committed band | 禁止；消费 pinned band或 fail closed |

### 9.5 Attention

| ID | 攻击 | 预期 |
|---|---|---|
| GS16-ATT-001 | expires_at 已过，查询时静默返回 available | 仍返回 committed head + transition_due |
| GS16-ATT-002 | expiry due trigger 引用错误 Clock/hash/revision/Attention before | 拒绝 |
| GS16-ATT-003 | focus_ref 不存在或与 cause authority 不符 | 拒绝 |
| GS16-ATT-004 | allocation/interruptibility 越界 | schema 拒绝 |
| GS16-ATT-005 | available 强制即时回复、DND 强制沉默 | 不得存在 reducer/compiler 映射；集成 trace 检查 |
| GS16-ATT-006 | compensation 恢复旧 expiry 后查询直接二次改变 head | 只能由 exact Clock 打开/复用 due trigger；不得隐式变化 |
| GS16-ATT-007 | expiry trigger 机械写 available/固定 allocation | 拒绝；后续必须经 Deliberation proposal 或明确 no-change |

### 9.6 SituationCompiler

| ID | 攻击 | 预期 |
|---|---|---|
| GS16-SIT-001 | monkeypatch wall clock/random/model/network | 输出不变且零调用 |
| GS16-SIT-002 | 同一 request 不同输入集合顺序 | canonical output/hash 相同 |
| GS16-SIT-003 | 混入另一 world 或 revision 的 head | hard error，不做 best effort |
| GS16-SIT-004 | 缺 Location/Resource/Attention | 显式 unavailable，不填默认值 |
| GS16-SIT-005 | stale cache key/value 或篡改 internal/viewer hash | cache 丢弃并纯重算 |
| GS16-SIT-006 | internal cache 用于低权限 viewer | viewer scope key 不同；不得泄露 |
| GS16-SIT-007 | private Goal、location、participant、commitment | 最严格 privacy meet/redaction |
| GS16-SIT-008 | resource 低/goal blocked 触发固定情绪或话术 | Compiler output 不含行为决定 |
| GS16-SIT-009 | 截断后 source revision 被遗漏 | 拒绝或记录 truncation reason；hash 可重算 |
| GS16-SIT-010 | rebuild 后编译 | 与 live compile byte-equivalent |
| GS16-SIT-011 | 同 heads、不同 policy digest | hash 必须不同 |
| GS16-SIT-012 | Attention 到期未结算 | 保留 committed state并标 due，不假装已切换 |
| GS16-SIT-013 | viewer/budget 不同 | internal hash 相同，viewer projection hash 按 scope/budget 不同 |

### 9.7 SQLite、replay 与 tamper

| ID | 攻击 | 预期 |
|---|---|---|
| GS16-SQL-001 | 非空 `.15` SQLite 升 `.16` 后 reopen/rebuild | cursor 与旧 authority 保留；新 heads 空/unavailable；hash 一致 |
| GS16-SQL-002 | `.15` semantic hash 被篡改 | migration 前拒绝 |
| GS16-SQL-003 | `.16` state_json/head state hash 被篡改 | reopen 拒绝 |
| GS16-SQL-004 | migration transaction 中断 | 原 `.15` 可重开或完整 `.16`，无半迁移 |
| GS16-SQL-005 | 从更旧 bundle 连续迁移到 `.16` | 每段先按旧规则验证，最终 rebuild 一致 |
| GS16-SQL-006 | 旧裸 Goal/location/needs 被自动升级 typed authority | 必须为空/unavailable；测试失败 |
| GS16-SQL-007 | replay 期间调用 model/random/network/wall clock | 零调用 |

## 10. `.15→.16` 迁移清单

### 10.1 ReducerState 与 semantic payload

新增字段：

```text
goals, goal_transitions, goal_proposals, goal_proposal_ids
locations, location_transitions, location_proposals, location_proposal_ids
resources, resource_transitions, resource_proposals, resource_proposal_ids
attentions, attention_transitions, attention_proposals, attention_proposal_ids
```

不新增 `situations` 或 `situation_transitions`。Situation 是 Snapshot 编译产物。

`.16` semantic payload 包含四组 head/history；pending proposal 和 proposal ID 沿用既有决策：若它们当前不属于 semantic payload，则保持一致，不为 `.16` 单独改变 hash 语义。所有 live consistency validator 检查：唯一 identity、连续 revision、before/after 连续、head 等于 latest transition、proposal durable index 完整。

### 10.2 Verified migration 顺序

1. 读取 `.15` head cursor、bundle、state JSON、semantic hash、state hash；
2. 用保留的 `.15` semantic payload 分支验证旧 semantic hash，并用现有 persisted state hash 验证 head state/cursor；
3. 不声称调用不存在的 `.15` reducer runner；仓库当前只安装目标 reducer artifact。若未来引入 archived bundle runner，必须另立 ADR/bundle，不在 `.16` 临时扩 scope；
4. 用 `.16` 目标 reducer 从 immutable events replay 到相同 cursor；旧未注册事件按既有 catalog/legacy handling fail closed，不倒造 `.16` events；
5. 目标 `.16` state 的四组新字段全部空；不解析旧 prompt、`SituationStateProjection`、legacy Goal/needs/location/attention 字符串；
6. 比较目标 replay 的既有 `.15` authority slices 与已验证 head，确保 `.16` 零级联地只增加空新字段；
7. 在一个 SQLite transaction 写 state JSON、semantic hash、bundle version、state hash；
8. close/reopen，再 direct rebuild，三者 projection/hash/cursor 一致；
9. migration 测试只断言旧字段没有被升格及新 slice unavailable；不新增未定义的持久化 migration report。原始 legacy 内容仍只存在于其原始事件/旧存储归档；
10. 更新 supported migration set，保留 `.15` legacy semantic payload/hash 分支。

对于新世界，Goal/Location/Resource/Attention 都可为空并由 Situation 显式输出 unavailable。需要初始化 Location/Resource/Attention 时，必须由 active deployment ActorAuthority 通过相应 operator proposal；不能靠 Pydantic defaults、`WorldStarted` 或隐式 seed lane 制造状态。Goal 可由普通 deliberative proposal 打开。

## 11. Runtime 与消费者接线清单

### 11.1 Producers

- `WorldRuntime` 暴露四个 domain proposal command，不暴露“set situation”；
- Deliberation 只能返回 typed proposal candidates；Adapter 不自行接受；
- Operator command 必须解析 active domain ActorAuthority；可另写 exact observation 作审计，再提 operator-lane proposal，但 observation 不授权；
- activity/occurrence/action settlement 先 commit；下一 world revision 的 adapter 才能用其 `CommittedWorldEventRef` 构造 proposal；
- `advance()` 在 `ClockAdvanced` 后确定性枚举 due Goal、Resource interval 与 Attention expiry：Goal/Resource 写 exact mechanical after，Attention 只打开唯一 due trigger；
- recovery 使用同一 idempotency material，不生成新结果；
- RandomDraw 只在候选选择发生时产生，并被 proposal 引用。

### 11.2 Consumers

- `InternalWorldSnapshot` 增加四组 authority heads 或 compiler 所需的 pinned slice；
- `SituationCompiler` 成为 current_situation 的唯一 producer；删除/隔离 `context_assembler.py` 对裸字符串的拼装；
- ContextCapsule 消费 source-bound Situation fields 与 truncation reasons；
- Appraisal/Advisory 可读取 Situation，但只能产候选解释；
- Deliberation 同时读取 Situation、CharacterCore、Affect、Relationship 和当前输入；
- reply timing 读取 Attention/处境作为特征，不把 mode 当硬行为；
- media/life share 使用 scene visibility，但仍需独立 media/privacy authority；
- dashboard/Godot 只读 viewer projection，不直接读内部 Resource/Goal；
- MemoryCandidate 可引用 committed Goal transition only if/when source union 明确版本化；`.16` 不暗改 `.14` source contract。

### 11.3 Trace 闭环

至少提供四条可读 fixture：

1. `ActivityCompleted committed → 下一 revision proposals → V2ResourceStateAdjusted + V2GoalProgressed + V2AttentionChanged → Situation recompiled → Capsule consumed`；
2. `ClockAdvanced → V2GoalExpired/V2ResourceClockAdjusted + attention expiry due trigger → Deliberation 可选 V2AttentionChanged/no-change → Situation recompiled`；
3. `private Location/Goal → internal Situation available → viewer Situation redacted`；
4. `blocked Goal + depleted resource → Advisory 提出多种解释 → Deliberation 可选择不同 stance`，证明没有固定情绪/话术映射。

每条 trace 必须能从 source event/hash 追到 head revision、Situation source revisions、semantic hash、Capsule slice 和后续 Action/无 Action；Situation 本身不产生 Action。

## 12. 最小施工顺序

为减少共享 `schemas.py`、`reducers.py`、`sqlite_ledger.py` 冲突，按以下顺序串行合并核心：

1. 冻结 `.16` event names、projection types、policy refs/digests 和 error codes；
2. 实现四组 domain event/payload 文件及纯 reducer 文件；
3. 分别完成 reducer/property/attack tests，不先接总 reducer；
4. 注册四个 proposal family、event catalog、batch invariants；
5. 扩展 ReducerState、semantic payload 和 projection rebuild；
6. 实现 `.15→.16` SQLite verified migration 与连续迁移测试；
7. 实现纯 `SituationCompiler` 与 golden/property/privacy/cache tests；
8. 接 InternalWorldSnapshot/ContextCapsule，但先用内部 harness，不接 QQ；
9. 接 `advance()` mechanical events 和 crash recovery；
10. 做全量 tests、静态检查、P0/P1 review、性能/token/cost 报告；
11. 更新上位计划真实状态并提交 exit report。

可以并行准备但不能同时编辑核心共享文件：Goal fixtures、Location/Resource/Attention reducer fixtures、Situation golden corpus、SQLite attack fixture。bundle 常量、semantic payload、catalog 和 migration 必须由一个合并 owner 串行落地。

## 13. Definition of Done

`.16` 只有同时满足以下条件才可标记完成：

- 四个独立 authority Module 均有 CAS/history/compensation；其非 mechanical mutation 有独立 proposal/acceptance family，Clock event 明确不属于 typed family；
- 四个 proposal selector/contract/mutation owner 与全部 `V2*` event names 按 4.1 冻结，legacy 同名 producer 无法进入 v2 catalog；
- operator lane 必须 exact resolve active deployment ActorAuthority 与 domain operation；OperatorObservation 单独存在不能授权；
- settlement 使用“先 commit、下一 revision proposal”两阶段，没有 future/prospective source shortcut；
- 随机 proposal 的 RandomDrawBinding 与 mutation hash、候选集、retry/supersede lineage 完整闭环；
- after/opened/since/closed 时间全部 pin event/Clock Logical Time；
- Clock expiry/recovery 有 exact authority、确定性 idempotency 与 recovery；
- 每个 domain event 通过 zero-cascade projection diff；
- SituationCompiler 无写事件、无 I/O、无 wall clock、无模型、无随机；
- source matrix 每个输出字段都有唯一 authority 和 source revision；Location scene visibility 与 disclosure privacy 已拆分；
- missing 与 redacted 显式区分；viewer 不泄露 private Goal/Location/Resource/Attention；
- live compile、cache compile、reopen compile、rebuild compile byte-equivalent；internal semantic hash 不因 viewer/budget 改变，viewer projection hash 精确绑定 scope/budget；
- `.15→.16` 非空 SQLite migration、连续跨版本、tamper 和中断恢复通过；
- legacy Goal/needs/location/attention 没有被自动升格；
- ContextCapsule 有 source-bound 消费 trace，但没有固定处境→情绪/话术规则；
- 旧 `context_assembler`/life runtime 写旁路已删除或有可验证隔离；
- 全量测试、ruff/type/import checks、P0/P1 审查通过；
- 报告热路径 compile p50/p95、cache hit/miss、Capsule token 变化；
- 更新 `REDUCER_BUNDLE_VERSION`、event catalog、semantic payload、SQLite migration、上位计划与 exit report 的真实状态。

## 14. 实现时不得临时决定的事项

以下选择在本设计中已冻结，避免施工中重新发明语义：

- Goal、Location、Resource、Attention 是四个 authority Module，不共享 reducer；
- Situation 是只读编译结果，不是第五个 authority；
- Clock lane 是 typed mechanical authority，不注册 typed proposal mutation、不伪装 Acceptance；Attention 到期只打开 due trigger，不替角色选择 after；
- Location/Attention 每 actor 单 head，Resource 每 `(actor, kind)` 单 head，Goal 每 ID 单 head；
- Resource 首发只有三个 kind，所有数值为 `0..10000` 定点；
- 到期只打开/执行显式 transition，不做查询时 TTL；
- terminal Goal 不 reopen，progress `10000` 不自动 complete；
- cross-domain change 只通过显式原子 UoW，不通过 reducer cascade；
- legacy 裸状态只隔离，不升级为 typed authority；
- Compiler 的 internal semantic identity 包含 internal policy/source revisions；viewer scope、budget 和 truncation 只进入独立 viewer projection identity；
- 人类感来自主 Deliberation 对可靠处境的自由解释，不来自 reducer 的固定行为表。

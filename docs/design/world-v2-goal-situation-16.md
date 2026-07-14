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

合并前还必须先安装 `actor-authority-policy.2` artifact/catalog/digest；这不是可延期的便利项。policy.1 只承担legacy replay，不能授权四个v2治理operation。

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

`.16` 不宣称已经拥有受控随机 authority。`selection_mode=random_draw` 在本 bundle 中一律 fail closed；现有或临时增加的薄 `RandomDrawProjection`、caller 传入的 draw 字段、普通 EvidenceRef 都不能把它变成受支持能力。`selection_mode=direct` 是完整可用路径，不得因为 RandomAuthority 尚未落地而阻塞 Goal open/revise 或其他合法直接选择。完整受控随机留给独立 `RandomAuthority` Module，并须通过 4.2 的后续验收合同后才能由新 bundle 显式启用。

## 3. Module、Interface 与 seam

| Module | 外部 Interface | 隐藏的 implementation | 依赖类别 | 主要测试 seam |
|---|---|---|---|---|
| `GoalAuthority` | typed proposal codec + `reduce_goal(...)` | 状态机、completion/Clock authority、CAS、补偿、lineage | in-process | proposal → acceptance → event → head/history |
| `LocationAuthority` | typed proposal codec + `reduce_location(...)` | single-head、cause binding、scene classification、privacy floor、CAS、补偿 | in-process | exact before/after 与 source authority |
| `ResourceAuthority` | typed proposal codec + `reduce_resource(...)` | 定点运算、band policy、Clock interval、CAS、补偿 | in-process | delta conservation 与 deterministic band |
| `AttentionAuthority` | typed proposal codec + `reduce_attention(...)` | mode/focus 约束、expiry、Clock authority、CAS、补偿 | in-process | lifecycle、time 与 exact latest |
| `ClockAuthorityIndex` | `resolve_latest_clock(pinned_state)` | `ClockAdvanced` transition projection/history、latest selection、exact hash/policy verification | in-process read-only | live/reopen/rebuild解析相同latest Clock |
| `DeliberativeBasisResolver` | `resolve(binding, pinned_state)` | typed parser dispatch、capability、canonical source hashes、privacy floor | in-process read-only | same binding/state → same resolved basis |
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
  cause_authority              # discriminator；deliberative时为typed basis
  policy_refs[]
  acceptance_id
  proposal_id
  evaluated_world_revision
  accepted_change_hash
  selection_mode
  random_draw_authority?
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

Clock mechanical lane 不制造 `ProposalRecorded/AcceptanceRecorded`，其事件也不得注册进 typed proposal family 的 `mutation_event_types`。它使用 domain-specific typed authority，仍必须绑定 exact `ClockTransitionProjection`、policy 和目标 before image。domain resolver 禁止扫描 caller cause 或把 cause 自报的 event ref/revision/hash/from/to 当成事实；唯一可信来源是 4.3 的 committed Clock projection/history。Goal expiry要求resolved Clock的`logical_time_to >= due_window.ends_at`；允许用 latest Clock补结算先前漏掉的due，但不能引用更早Clock。Resource recovery与Attention due同样只认该resolver结果。其idempotency key固定为：

```text
sha256(world_id + event_type + operation + target_identity + before_revision + clock_event_ref + policy_digest)
```

同一 Clock authority 对同一目标只能结算一次。recovery 可重放同一确定性事件，不能生成新随机 ID 或改写结果。Clock 只有两种合法输出：打开 deterministic due trigger，或提交 payload 中已经冻结、且 reducer 能按 pinned policy 复算的 exact after image；它不能临时决定角色接下来想做什么。

上式是mechanical event identity；Attention另有更严格的target cardinality identity：`sha256(canonical_json({world_id, process_kind, actor_ref, attention_entity_revision, expiry_policy_digest}))`，不含Clock ref。首次open event仍按上式绑定实际latest Clock，但同一Attention revision只要历史中已有open/claimed/terminal target identity，后续Clock均不得再次打开。

### 4.1 冻结的 proposal routing

四个显式 proposal family 使用以下唯一 selector 与合同；机械事件不在表内：

| Domain | `proposal_kind` | `authority_contract_ref` | typed mutation events |
|---|---|---|---|
| Goal | `v2_goal_transition` | `proposal-contract:v2-goal.1` | `V2GoalOpened`、`V2GoalRevised`、`V2GoalProgressed`、`V2GoalPaused`、`V2GoalResumed`、`V2GoalBlocked`、`V2GoalUnblocked`、`V2GoalCompleted`、`V2GoalAbandoned`、`V2GoalTransitionCompensated` |
| Location | `v2_location_transition` | `proposal-contract:v2-location.1` | `V2LocationChanged`、`V2LocationChangeCompensated` |
| Resource | `v2_resource_transition` | `proposal-contract:v2-resource.1` | `V2ResourceStateInitialized`、`V2ResourceStateAdjusted`、`V2ResourceTransitionCompensated` |
| Attention | `v2_attention_transition` | `proposal-contract:v2-attention.1` | `V2AttentionChanged`、`V2AttentionTransitionCompensated` |

四个 family 均以 `ProposalRecorded` 为 record event，并分别拥有 concrete `*ProposalProjection`、canonical `*ProposedMutation`、codec 与 proposal store。每个 ProposalProjection 的 `transition_kind` 是该域 operation 的 closed `Literal`，validator 使用完整映射验证 `transition_kind ↔ proposed_mutation.event_type`，不能接受任意字符串或只检查event属于domain。`payload_json` 解码后必须是JSON object，且重新按UTF-8、sort keys、无多余空白编码后与原文byte-equivalent；array/scalar、duplicate/unknown shape或非canonical object全部拒绝。selector `(ProposalRecorded, proposal_kind)`、contract ref、mutation event owner必须全局唯一。`V2GoalExpired`、`V2ResourceClockAdjusted`和`TriggerProcessOpened(process_kind=v2_attention_expiry_due)`由mechanical payload map与普通event catalog注册，不经过proposal registry。

### 4.2 selection mode 冻结与后续 RandomAuthority 合同

每个可由模型或随机选择产生的 mutation 都显式声明选择边界：

```text
selection_mode = direct | random_draw
```

`.16` 的 installed policy 只支持 `direct`。`direct` 表示没有随机抽样，禁止携带 draw authority；它可用于所有本来合法的 lane，尤其不得要求 Goal direct 先等待随机模块。`random_draw` 是保留的 wire literal，但本 bundle 的 proposal codec、dry-run 和 domain reducer 必须无条件以 `random_authority_not_installed` 拒绝；即使 payload 携带看似完整的以下结构也不能接受：

```text
RandomDrawAuthority
  decision_evidence_ref          # exact committed_world_event EvidenceRef
  draw_event_ref
  draw_world_revision
  draw_payload_hash
  draw_id
  attempt_id
  candidate_set_hash
  selected_candidate_ref
  catalog_version
  sampler_version
  supersedes_draw_ref?
```

上述 `RandomDrawAuthority` 在 `.16` 只是保留 schema，不是可用 authority。薄 `RandomDrawProjection` 只记录“某处声称抽过”，缺少独立 entropy、预算、采样、supersession、消费与 replay 语义，禁止被 resolver 当成支持证据。`direct` 携带任何 draw authority 也拒绝。

后续 bundle 要启用 `random_draw`，必须先交付独立、可替换、可单测的深 Module：

```text
RandomAuthority.record(request: RandomDrawRequest) -> RandomDrawProjection
RandomAuthority.supersede(request: RandomDrawSupersedeRequest) -> RandomDrawProjection
RandomAuthority.resolve(binding: RandomDrawAuthority) -> ResolvedRandomDecision

RandomDrawRequest
  world_id, actor_ref
  trigger_ref                   # exact committed trigger/cause
  decision_kind, decision_slot  # 同一触发中的稳定选择槽
  evaluated_world_revision
  entropy_authority {
    nonce_event_ref, nonce_world_revision, nonce_payload_hash
    entropy_commitment, nonce
  }
  candidates[] {candidate_ref, weight_bp}
  frequency_budget_authority {
    budget_ref, budget_revision, budget_event_ref, budget_payload_hash
    window_ref, limit, consumed_before
  }
  catalog_version
  sampler_version, sampler_digest
```

其验收合同不可删减：

1. `(world_id, actor_ref, trigger_ref, decision_kind, decision_slot)` 是稳定 decision identity；retry 不得换 slot 刷结果。
2. entropy/nonce 必须在 proposal 评估 revision 之前由 Ledger 提交并 exact resolve；不能读 wall clock、进程 RNG 或调用方临时随机数。
3. 候选集按 `candidate_ref` canonical sort，引用与正整数权重全部进入 hash；selected candidate 必须由纯 deterministic weighted sampler 从 committed nonce 复算。
4. frequency budget 是已提交 authority；record 前验证窗口、limit 与 `consumed_before`，超预算 fail closed，不能只靠 prompt 提醒。
5. `RandomDrawRecorded` 与 `RandomDrawSuperseded` 都有 typed payload、event catalog、pure reducer、head/history、Ledger projection、SQLite roundtrip、semantic hash 与 rebuild。supersede 必须绑定旧 draw 的 exact event/revision/hash，并保持 lineage；历史不可删除。
6. consuming domain mutation 必须把 active draw 与唯一 `consumer_transition_id` 原子登记进持久化 `consumed_random_draw_ids`；同一 draw 的第二次消费、消费 superseded draw、换 candidate 消费、跨 actor/slot 消费全部拒绝。CAS retry只可重放同一 consumer identity。
7. 只有该 Module 的版本、events、reducer、Ledger 与上述攻击测试全部进入一个后续 bundle 后，domain resolver 才能从“无条件拒绝”切到“exact authority 接受”；单独添加 projection/schema 不算完成。

### 4.3 ClockTransitionProjection 与 latest resolver

`.16` 为既有 `ClockAdvanced` reducer 增加共享、可重建的 authority projection；不新增 Clock event，也不改变旧 event envelope：

```text
ClockTransitionProjection
  clock_event_ref               # 实际应用的ClockAdvanced event ID
  computed_world_revision       # reducer/ledger应用位置计算；绝不读payload自报值
  payload_hash                  # canonical ClockAdvanced payload hash
  logical_time_from
  logical_time_to
  installed_policy_version
  installed_policy_digest
```

`ClockAdvanced` reducer 在验证原有 from/to 与 installed Clock policy 后，将上述完整 projection append 到 immutable `clock_transition_history`。event ref来自真实 envelope/event identity；world revision来自 Ledger commit/reducer cursor；payload hash由canonical payload计算；policy version/digest来自event所引用且被registry exact验证的已安装artifact，而不是replay时“当前最新版”policy。任何同名payload字段都不能覆盖这些computed fields。history按`computed_world_revision`严格递增，event ref/revision唯一，from必须等于应用前ReducerState logical time，to必须等于应用后logical time。

```text
resolve_latest_clock(pinned_state):
  candidates = clock_transition_history
  latest = max(candidates, key=computed_world_revision)
  require latest.logical_time_to == pinned_state.logical_time
  require latest policy version/digest仍可由installed artifact exact验证
  return latest
```

domain `ClockCauseAuthority` 仍可携带projection字段作为mutation hash/binding，但resolver必须逐项等于上述latest projection；它不能借自报字段创建authority。缺history、history latest与current logical time不一致、wrong policy/hash/ref/revision/from/to都fail closed。若多个Clock transition到达同一logical time，选择computed world revision最大者；不按event ID、payload顺序或wall clock选择。

`ReducerState` 新增 `clock_transition_history`，`LedgerProjection`、`make_projection`、`semantic_payload(.16)`、SQLite `_state_from_projection` 和rebuild必须完整接线。`.15` legacy semantic payload/hash明确排除此字段，验证旧库时不得倒算或改变旧hash；迁入`.16`时由目标reducer replay immutable `ClockAdvanced` events重建history，再以`.16` semantic规则持久化。旧 event envelope 的 `logical_time` 字段继续保持现有含义，不能改成`logical_time_from/to`、computed world revision或projection timestamp。

### 4.4 统一时间 pin

所有 after image 的 `updated_at`、transition 的 `accepted_at` 必须等于 domain event 的 Logical Time，并满足 `after.updated_at >= before.updated_at`；同一 head 的 Logical Time 不得倒退。首次建立的 `opened_at/since` 必须等于建立事件时间；terminal `closed_at` 必须等于 terminal event 时间。`opened_at`不得回写；`since`只在该域冻结的identity-change条件成立时重置为当前event time（Location location/zone，Attention mode/focus），否则必须preserve，不得写任意过去时间。任何生命周期时间不得晚于`updated_at`。Clock mechanical after image 使用所绑定 Clock 的 `logical_time_to`，且 domain event Logical Time 必须与它相等。

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
    blockers[]                 # canonical unique sorted typed GoalBlocker
    privacy_class
    completion_contract?        # typed CompletionContract
    status                     # active|paused|blocked|completed|abandoned|expired
    terminal_reason?           # typed GoalTerminalReason；禁止dead/free ref
    supersedes_goal_id?
    supersedes_goal_authority?  # exact prior terminal Goal binding
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
  completion_evidence?          # only V2GoalCompleted；与cause独立
  removed_blocker_fingerprints[]? # Unblock或blocked→Complete exact diff
  canonical_evidence_refs[]     # 只由typed bindings派生，caller不可填写
  revise_kind?
  selection_mode
  random_draw_authority?
  compensates_transition_id?
```

Completion 使用冻结的 typed contract，而不是任意 ref/digest：

```text
CompletionContract
  contract_id
  contract_version
  completion_kind              # settled_occurrence_outcome|active_fact_predicate
  outcome_ref
  expected_actor_ref
  allowed_event_types[]
  contract_schema_ref          # kind 对应的 contract schema
  completion_parser_ref        # kind 对应的纯解析器
  evidence_schema_ref          # occurrence/fact 各自的来源 authority schema
  required_fact_predicate?
  required_fact_value_hash?
  evidence_cutoff_world_revision
  privacy_class
  policy_version
  policy_digest
  contract_digest              # 上述字段canonical hash

GoalCompletionEvidence =
  WorldOccurrenceCompletionEvidence
  | CommittedFactStateCompletionEvidence

WorldOccurrenceCompletionEvidence
  evidence_kind=settled_occurrence_outcome
  occurrence_id, occurrence_entity_revision
  settlement_event_ref, world_revision, payload_hash
  resolved_actor_ref, settled_outcome_ref
  evidence_schema_ref=world-occurrence-settlement.1
  privacy_class

CommittedFactStateCompletionEvidence
  evidence_kind=active_fact_predicate
  fact_id, fact_entity_revision
  fact_event_ref, world_revision, payload_hash
  resolved_actor_ref, resolved_outcome_ref
  resolved_fact_predicate, resolved_fact_value_hash
  evidence_schema_ref=fact-authority.1
  privacy_class

GoalRationale
  text                         # trim→NFC后1..512 Unicode code points；拒绝全部Unicode General_Category=Cc control
  privacy_class
  # 不重复保存分类；分类由外层GoalProgressAssessment.contribution_class、
  # GoalLifecycleReason.reason_kind或InternalIntentionBasis.intention_class承载

GoalProgressAssessment
  contribution_class           # direct_contribution|indirect_support|milestone_reached|reappraisal
  rationale: GoalRationale     # 主观解释，不是客观证据
  basis                        # exact committed settled event | Fact | Experience typed basis

V2GoalChangedPayload(operation=progress)
  progress_delta_bp            # strictly > 0；delta属于mutation算术，不重复写入assessment

GoalLifecycleReason
  reason_kind                  # operation-specific closed catalog
  rationale: GoalRationale
  basis                        # committed typed basis或internal intention
  privacy_class

GoalTerminalReason = AbandonedTerminalReason | CompletedTerminalReason | ExpiredTerminalReason
  AbandonedTerminalReason {terminal_kind=abandoned, reason: GoalLifecycleReason}
  CompletedTerminalReason {terminal_kind=completed, contract_id, contract_digest, completion_evidence_ref, privacy_class}
  ExpiredTerminalReason {terminal_kind=expired, due_window, clock_projection_ref, policy_digest, privacy_class}

GoalBlocker
  blocker_id
  blocker_class                # external_dependency|resource_constraint|uncertainty|priority_conflict|relationship_constraint|environmental_constraint
  rationale: GoalRationale
  basis: DeliberativeBasisBinding
  blocker_fingerprint          # blocker全部typed canonical material的hash
  privacy_class

GoalBlockerResolution
  blocker_id, removed_blocker_fingerprint
  resolution_class             # externally_resolved|no_longer_relevant|accepted_tradeoff|superseded_assessment
  rationale: GoalRationale
  basis: DeliberativeBasisBinding
```

Open 时 `evidence_cutoff_world_revision` 等于proposal的`evaluated_world_revision`；recontract时重置为该revision，因此contract不能用早已存在的证据“事后完成”Goal。CompletionContract只能来自`goal-completion-contract-registry.1`：`completion_kind ↔ contract_schema_ref ↔ evidence union member`一一对应，contract digest覆盖closed kind/schema、outcome/actor约束、cutoff、privacy与policy全部canonical字段；unknown schema/kind、映射不一致或digest不匹配fail closed。`V2GoalCompleted` payload 在 `cause_authority` 之外必须另有且只有一个 typed `GoalCompletionEvidence` union member，deliberative recognition与operator补结算都必填。首发resolver只支持两个closed pure parser：WorldOccurrence只读current settled projection的`settled_outcome_ref`；Fact只读current active Fact的subject/predicate/value。每个parser exact绑定current entity revision、accepted event/world revision/payload hash、actor与kind-specific outcome，且证据必须晚于cutoff并逐项满足contract；unknown kind、非current、非settled/active、缺字段或只能靠自由payload解释的source全部fail closed。Activity、Plan、Action与Receipt目前缺少足够的terminal origin/outcome authority，明确unsupported；不得用`goal_ref/intent_ref`拼ID、只看status/event type或组合宽松receipt冒充CompletionEvidence，待对应authority另行硬化并版本化union后才能扩展。operator lane的ActorAuthority只负责重新授权该mutation，不能替代、制造或放宽CompletionEvidence；没有客观union evidence时operator也不能Complete。completed Goal privacy取before Goal、contract、evidence与deliberative basis的最严格meet。

为使WorldOccurrence parser纯解析，`.16` 给`WorldOccurrenceProjection`新增`settled_outcome_ref?`：仅settled current projection可非空，由settlement reducer从其typed event material确定性冻结。该字段进入`.16` projection/semantic/SQLite/rebuild，`.15` legacy semantic/hash明确排除；`.15→.16`用immutable occurrence settlement events replay重建，不能由prompt、Goal或当前模型补写。

`outcome_ref` 是想实现的结果引用，不是已发生 Fact。它与 actor/goal identity 在 Open 后不可变；目标含义变化时创建新 Goal并用 `supersedes_goal_id` 串联。若填写该字段，必须同时携带 `supersedes_goal_authority={goal_id, actor_ref, entity_revision, target_head_semantic_hash, accepted_event_ref, accepted_world_revision, accepted_payload_hash, privacy_class}`，resolver 必须 exact 命中一个已经存在的当前 Goal head，且该 Goal 与新 Goal 同 actor、不是新 Goal 自身、状态为 `completed|abandoned|expired`。codec必须从该exact head/event binding派生`GoalSupersessionEvidenceRef`并加入mutation的`canonical_evidence_refs`；caller不能漏掉、替换或另填ref。缺失目标、跨 actor、自指、非 terminal、stale head/revision/event/hash 一律拒绝；因此 supersession 不能被用作修改活跃目标或制造环。新Goal privacy不得弱于target/basis/rationale的最严格floor。`importance_bp`、`due_window`、CompletionContract 可通过显式 `revise_kind=reprioritize|reschedule|recontract` 修改并保留 before/after；没有 CompletionContract 的Goal不可Complete，只能Abandon/Expire。terminal Goal永不reopen。

状态机：

| 当前 | 操作 | 下一状态 | 额外条件 |
|---|---|---|---|
| 无 | open | active | rev `0→1`；初始 progress 可为 0..10000；记录初始 contract/due |
| active/paused/blocked | revise | 原状态 | 只改 revise kind 允许的字段；outcome/identity/progress 不变 |
| active/paused/blocked | progress | 原状态 | deliberative主观评估；exact settled/Fact/Experience basis；delta > 0；before + delta = after ≤ 10000 |
| active | pause | paused | typed pause reason；不改 progress |
| paused | resume | active | typed resume reason |
| active | block | blocked | deliberative typed GoalBlocker additions非空；集合有实际变化 |
| blocked | unblock | blocked/active | deliberative typed GoalBlockerResolution exact移除；仍有 blocker 则blocked，为空才active |
| blocked | block | blocked | 只允许增加/替换有typed basis与privacy floor的GoalBlocker |
| active/paused | complete | completed | deliberative recognition/operator补结算 + strict contract/evidence；closed_at=logical time |
| blocked | complete | completed | 同上，且after.blockers必须显式为空；transition记录完整removed fingerprints |
| active/paused/blocked | abandon | abandoned | deliberative typed abandon reason；closed_at=logical time |
| active/paused/blocked | expire | expired | exact Clock authority 到达 due end；closed_at=clock logical time |
| latest non-open transition | compensate | 显式 restored head | exact latest transition；生成新 revision，不删除历史 |

Progress 是角色对“这些经历对我的目标推进了多少”的主观内在评估，不是 Runtime 对 settlement 的机械派生。settled event、Fact 或 Experience 只提供 exact typed basis；Deliberation 选择 `contribution_class`、rationale 与正 delta，reducer只验证basis authority/capability、`before + delta = after`、范围、时间与privacy floor，不硬编码“某 event/outcome 必然让某 Goal 加多少”。评估为 no-change 时不产生 `V2GoalProgressed` 或空 mutation，可在审议 trace 中记录no-change decision。operator不得日常替角色加progress，只能通过exact compensation纠错。达到 `progress_bp=10000` 仍保持原状态，直到独立 `V2GoalCompleted`；Complete 才要求严格客观 CompletionContract。

pause/resume/abandon 不接受自由 reason 字符串。typed catalog 首版分别为：pause=`priority_shift|resource_constraint|uncertainty|relationship_consideration|context_changed`；resume=`priority_restored|constraint_resolved|renewed_intent|context_changed`；abandon=`no_longer_desired|superseded|infeasible|values_changed|context_changed`。三者与Progress/Block/Unblock均为deliberative-only；operator纠错只能走exact compensation，不能伪装一次新的角色选择。terminal head保存structured `GoalTerminalReason`：abandon复用exact `GoalLifecycleReason`，complete/expire分别绑定contract evidence或Clock projection，不保存悬空/free reason ref。分类用于审计、检索和模型发挥，不映射固定动作或话术。`recontract` 必须生成新 contract ID/digest并把 evidence cutoff pin 到recontract前的当前world revision；Operator治理修正也不能降低cutoff或复用更早证据，需修正历史时走exact compensation或创建superseding Goal。

Block/Unblock同样是角色对处境的解释，不是settlement reducer的机械副作用。Goal head只保存typed `GoalBlocker`集合，不保存裸refs；每个blocker的class、内嵌rationale、typed basis、`blocker_fingerprint`与privacy都进入mutation hash。`V2GoalUnblocked`逐项携带exact `GoalBlockerResolution`，其`removed_blocker_fingerprint`、basis与rationale必须匹配/解释被移除current blocker；partial diff后其余blocker保持byte-equivalent。resolution class与basis是closed matrix：`externally_resolved`必须有exact `CommittedEvidenceBasis`，不能靠内心宣称外部障碍已解决；`no_longer_relevant|accepted_tradeoff|superseded_assessment`允许`InternalIntentionBasis`，明确表示角色主观放下、接受或改判，而非篡改外部事实。两者只允许Deliberation；operator纠错只能走exact compensation。settled event若相关，只能先作为`CommittedEvidenceBasis`供Deliberation评估，Runtime不得自动Block/Unblock。reducer验证typed authority、class×basis、集合diff、identity/fingerprint、privacy与状态机，不硬编码某settlement必然构成或解除哪类blocker。blocked Goal被Complete时不是隐式遗留blockers：Complete transition必须列出所有removed blocker fingerprints，after集合显式清空。

### 5.1.1 `.16.0` installed capability matrix

本设计中的 wire union 与 event catalog 只描述可版本化的形状，不等于某个 authority 已经安装。`.16.0` 首发采用以下 closed capability matrix；resolver 未列出的 lane/source 一律 fail closed，不能从自由 ref、`activity_kind`、result payload 字符串或模型解释临时扩权：

| Domain operation | `.16.0` installed authority | 明确未安装 |
|---|---|---|
| Location establish/change | active `.2` ActorAuthority operator | settled movement、deliberative/internal、图片/用户陈述 |
| Resource initialize | active `.2` ActorAuthority operator | settlement、Clock recovery |
| Resource state_change adjust | operator；typed deliberative basis/internal self-assessment | reclassify（wire保留但registry空）、settlement adapter、random draw |
| Attention establish | active `.2` ActorAuthority operator | settlement、deliberative establish |
| Attention change | operator；typed deliberative basis/internal intention | settlement adapter、random draw |
| Attention expiry | exact latest Clock只打开每个Attention revision唯一due trigger | 机械重置head、查询时变化 |

`ActivityCompleted`只证明Plan terminal，Plan的`location_ref`与scheduled window不是实际移动/休息证据；`WorldOccurrenceSettled`目前没有typed movement outcome、zone/scene classification或recovery class；`ExecutionReceiptRecorded`只证明provider状态、artifact与成本。因此三者首发都不能被settlement adapter升级为Location movement或Resource recovery。它们仍可作为Deliberation的typed输入，由角色主观评估后选择是否提出Resource/Attention change。分类矩阵只规定authority边界，不把band、mode或source type映射成固定情绪、动作、回复延迟或话术。

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

`scene_visibility` 是场景事实，不是披露授权；未来安装合法settled movement authority后，从private场所移动到public场所可自然改变它，不需要“放宽隐私”的授权。`.16.0`没有可证明movement、zone与scene classification的现成source parser，因此Location establish/change均为operator-only；`SettledEventCauseAuthority`在Location的installed capability set为空并以`location_movement_authority_not_installed` fail closed。不得把Plan location、WorldOccurrence location/visibility、图片背景、result payload ref或用户陈述解释成移动。未来启用settlement lane前必须新增typed `MovementSettledProjection{movement_id, actor_ref, from_location_ref?, from_zone_ref?, to_location_ref, to_zone_ref?, to_scene_visibility, occurred_at, exact source event/entity binding, privacy_class, policy version/digest}`，不能复用opaque字符串。

`privacy_class`单独约束是否向某viewer暴露精确`location_ref/zone_ref`，不得弱于来源/lifetime privacy。`since`表示进入该location/zone的logical time：establish或location/zone实际变化时等于mutation logical time；只改scene visibility/privacy时保持原值。`updated_at`等于authority transition时间。首次`V2LocationChanged(operation=establish)`只允许持有active deployment ActorAuthority的operator initialization，before为`None`；不设隐式seed lane。后续必须携带exact before/after；exact no-op拒绝。

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
    privacy_class
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

首发 resource kind 是闭集。财务 Budget、关系资源、need、Affect 不得塞入此 projection。`resource-band-policy.1`冻结整数bp分类：`depleted=0..999`、`low=1000..3499`、`moderate=3500..6499`、`high=6500..8999`、`full=9000..10000`，同一artifact用于`physical_energy|cognitive_capacity|social_capacity`；digest覆盖version、排序后的kind、按顺序的闭区间与`integer_basis_points`算术。band只供处境分类，不蕴含行为。普通`state_change`必须`delta != 0`且`before + delta == after`；结果越界拒绝，禁止clamp。`reclassify` wire shape要求`delta=0`、value不变、policy artifact真实变化，并由新policy得到after band；但`.16.0`只有`resource-band-policy.1`，不存在可替代artifact，因此reclassification registry为空，所有reclassify在mutation前fail closed，禁止伪造`policy.2`。`derived_band`只能由事件中pinned policy对value确定性计算，policy version/digest是committed values的一部分。SituationCompiler消费committed band并验证其pinned policy；不得用“当前policy”静默重算。

`V2ResourceClockAdjusted`保留mechanical wire contract，但`.16.0`的installed recovery policy registry为空；所有Clock recovery以`resource_recovery_authority_not_installed`零变化拒绝。现有Activity/Occurrence/Receipt不证明真实休息区间，不能在reducer内推测“角色应该休息好了”。未来启用前必须新增typed `SettledRecoveryInterval{recovery_id, actor_ref, resource_kind, rest_class, interval_start, interval_end, exact source event/entity binding, privacy_class}`与closed rate catalog。届时唯一允许的定点公式为：对Clock区间与canonical无重叠recovery intervals求`overlap_seconds_i`，`raw_delta=floor(sum(overlap_seconds_i * rate_bp_per_hour(kind, rest_class_i))/3600)`，`applied_delta=min(raw_delta, 10000-before)`，`after=before+applied_delta`；该`min`是frozen saturation policy，不是修补非法payload，`applied_delta=0`不产event。payload须冻结raw/applied delta与全部input digest，reducer逐项复算。

### 5.4 Attention

```text
AttentionProjection
  actor_ref                    # actor 单 current head
  entity_revision
  semantic_fingerprint
  values {
    mode                       # available|glancing|occupied|deep_focus|do_not_disturb|recovering_attention
    focus_ref?
    focus_binding?             # typed Plan|WorldOccurrence|Trigger current binding；不得裸ref
    allocation_bp              # 0..10000
    interruptibility_bp        # 0..10000
    since
    expires_at?
    privacy_class
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

`focus_binding`是closed discriminator union：Plan只接受current active projection，WorldOccurrence只接受current active projection；Trigger member保留wire shape，但共享`TriggerProcess v2.1`没有actor authority字段，无法证明trigger属于被修改的Attention actor，因此`.16.0` resolver对所有Trigger focus一律`attention_trigger_focus_authority_not_installed`，不得把exact ref/hash误当actor authority。后续只有在共享TriggerProcess安装typed actor binding并完成migration/replay验收后，才可在新bundle启用current open/claimed Trigger focus。每个已安装member冻结source kind/ref/entity revision（若有）、current projection hash与pinned world revision，`focus_ref`必须由binding派生。`available|recovering_attention`禁止focus，`glancing|occupied|deep_focus`要求focus，`do_not_disturb`可选；这些只是结构不变量，`allocation_bp/interruptibility_bp`仍由角色选择且不按mode硬映射。mode或focus identity变化时`since=mutation logical time`，只改allocation/interruptibility/expires/privacy时保持；非空`expires_at`必须晚于`updated_at`。

`expires_at`只使重新审议变为due，不在查询时临时改head，也不机械重置为`available`。目标契约要求`TriggerProcess`新增typed `AttentionExpiryDueBinding{actor_ref, attention_entity_revision, attention_semantic_fingerprint, expires_at, exact Clock projection fields, expiry policy version/digest, idempotency_key}`；只有`process_kind=v2_attention_expiry_due`可携带且必须携带，其他kind禁止。`attention-expiry-policy.1`冻结`due_when=clock_to_gte_expires_at`、`mechanical_effect=open_trigger_only`、`one_trigger_per_attention_revision=true`。trigger ID按canonical `{world_id, process_kind, actor_ref, attention_entity_revision, expiry_policy_digest}`派生；open/claimed/terminal任一历史trigger都占用该revision identity，后续Clock不得重开。Attention产生新revision后才可再打开。只有trigger binding exact命中current head时Situation显示`transition_due=true`；stale trigger保留审计但不生效。主Deliberation随后可以提`V2AttentionChanged`或明确no-change并terminal trigger。这样Clock只授权“该重审了”，不替角色选择后续注意状态。

当前`.16.0`共享集成只安装Attention typed mutation；现有共享`TriggerProcess v2.1`既无actor authority，也无上述expiry binding/immutable identity字段，因此`v2_attention_expiry_due`没有注册进共享schema、event catalog或runtime。纯层binding与validator仅作为未来wire合同，不构成已安装能力；`advance()`不得生成该trigger。启用必须通过新bundle完成TriggerProcess schema/migration、claim/reclaim/terminal identity保持与one-per-revision全生命周期验收，禁止半接线。

## 6. 事件与 authority lane 矩阵

### 6.1 Lane 定义

| Lane | 谁可产生候选 | 是否 Proposal/Acceptance | 可引用 authority | 禁止事项 |
|---|---|---|---|---|
| `deliberative` | 主 Deliberation | 是 | typed external basis，或有严格能力边界的 spontaneous internal basis | 用户一句话直接成世界事实；internal basis 自证 location/progress/completion |
| `operator` | 明确 operator command Adapter | 是 | active deployment `ActorAuthorityProjection` + domain required operation；OperatorObservation 仅审计 | 借 operator observation 伪造授权；借 operator lane 接普通用户消息 |
| `settlement` | Runtime 对已提交 settled domain/external result 的确定性 adapter | 是；settlement 先 commit，下一 world revision 才记录 proposal | exact committed settled event/receipt | prospective/future evidence；provider accepted 冒充 settled；从 pending Action 改状态 |
| `clock_runtime` | `advance()` / recovery | 否；domain-specific mechanical contract | exact latest `ClockAdvanced`（按6.3前定义） | wall clock、LLM、随机、查询时隐式变化 |
| `compensation` | operator 或 domain correction Deliberation | 是 | exact latest transition + correction evidence | 回滚历史、补偿非 latest、跨 identity 补偿 |

### 6.2 Event catalog

| Domain | Event | Operation | Lane | 必需 authority | Reducer 只写 |
|---|---|---|---|---|---|
| Goal | `V2GoalOpened` | open | deliberative/operator initialization/import | outcome source、policy、可选 due/contract | goal head/history |
| Goal | `V2GoalRevised` | revise | deliberative/operator governance correction | revise kind + exact before/after | goal head/history |
| Goal | `V2GoalProgressed` | progress | deliberative | exact settled/Fact/Experience typed basis + contribution class/rationale + before/positive delta/after | goal head/history |
| Goal | `V2GoalPaused` | pause | deliberative only | typed pause reason + basis | goal head/history |
| Goal | `V2GoalResumed` | resume | deliberative only | typed resume reason + basis | goal head/history |
| Goal | `V2GoalBlocked` | block | deliberative only | typed GoalBlocker additions/replacements + basis | goal head/history |
| Goal | `V2GoalUnblocked` | unblock | deliberative only | exact non-empty GoalBlockerResolution diff + basis | goal head/history |
| Goal | `V2GoalCompleted` | complete | deliberative recognition/operator evidence-backed补结算 | typed frozen CompletionContract + 独立typed GoalCompletionEvidence；operator authority只reauth | goal head/history |
| Goal | `V2GoalAbandoned` | abandon | deliberative only | typed abandon reason + basis | goal head/history |
| Goal | `V2GoalExpired` | expire | clock_runtime | exact Clock + frozen due end + policy digest + exact expired after | goal head/history |
| Goal | `V2GoalTransitionCompensated` | compensate | compensation | exact latest non-open transition | goal head/history |
| Location | `V2LocationChanged` | establish/change | operator | exact `.2` ActorAuthority + before/after；`.16.0` movement capability未安装 | location head/history |
| Location | `V2LocationChangeCompensated` | compensate | compensation | exact latest transition | location head/history |
| Resource | `V2ResourceStateInitialized` | initialize | operator | active ActorAuthority + band policy | resource head/history |
| Resource | `V2ResourceStateAdjusted` | state_change adjust；reclassify wire | deliberative/operator | state_change使用exact typed cause + before/delta/after + pinned band policy；reclassify registry为空并fail closed；settlement adapter未安装 | resource head/history |
| Resource | `V2ResourceClockAdjusted` | clock_adjust | clock_runtime | wire contract保留；`.16.0` recovery registry为空，始终fail closed | resource head/history |
| Resource | `V2ResourceTransitionCompensated` | compensate | compensation | exact latest transition | resource head/history |
| Attention | `V2AttentionChanged` | establish/change | operator/deliberative | exact typed cause + before/after；establish仅operator，settlement adapter未安装 | attention head/history |
| Attention | `TriggerProcessOpened(v2_attention_expiry_due)` | due trigger | clock_runtime | exact Clock + attention revision/expires_at + policy | trigger process only；Attention 不变 |
| Attention | `V2AttentionTransitionCompensated` | compensate | compensation | exact latest transition | attention head/history |

Situation 没有任何 catalog event。

Goal lane是closed matrix且没有settlement写lane：operator只可用于Open初始化/导入、Revised治理修正、携带strict independent CompletionEvidence的Complete补结算，以及operator-origin transition的compensation reauthorization。Complete正常路径是Deliberation对客观证据的recognition。Progress/Block/Unblock/Pause/Resume/Abandon全部deliberative-only；其历史错误由exact compensation修正，不能直接借operator lane或settlement adapter提交相同operation。

### 6.3 Cause authority union

共享字符串 `cause_ref` 不足以授权变化。每个 payload 使用 discriminator union：

```text
DeliberativeCauseAuthority
  kind=accepted_deliberation
  basis: DeliberativeBasisBinding

DeliberativeBasisBinding = CommittedEvidenceBasis | InternalIntentionBasis

CommittedEvidenceBasis
  basis_kind=committed_evidence
  sources[] {
    source_kind                 # closed typed parser kind
    event_ref, world_revision, payload_hash
    source_entity_ref?, source_entity_revision?
  }

InternalIntentionBasis
  basis_kind=internal_intention
  actor_ref
  trigger_ref                   # deliberation turn/active trigger identity
  decision_slot
  evaluated_world_revision
  logical_time
  intention_kind               # goal_choice|goal_governance|attention_choice|resource_self_regulation
  intention_class              # self_direction|priority_reassessment|constraint_response|value_alignment|uncertainty_management
  rationale: GoalRationale     # 内嵌；禁止外部ref/blob
  intention_material_hash      # 上述typed canonical material的派生hash
  policy_version, policy_digest
  privacy_class=private

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
  clock_event_ref, clock_world_revision, clock_payload_hash  # 必须逐项等于resolved projection
  logical_time_from, logical_time_to
  policy_version, policy_digest

CompensationCauseAuthority
  kind=compensation
  target_transition_id, target_entity_revision
  target_event_ref, target_world_revision, target_payload_hash
  expected_target_lane?         # 仅审计/比较；不授权
  correction_basis              # domain closed discriminator union
  correction_rationale          # bounded canonical rationale + privacy
  operator_authority?           # operator-origin/current reauthorization
```

每个 operator mutation 都必须解析当前 active `ActorAuthorityProjection`：principal kind 为 deployment operator、required operation 存在、authority 未过期、values/policy digest 和 committed event 完全匹配。四域 required operation 分别冻结为 `v2_goal_governance|v2_location_governance|v2_resource_governance|v2_attention_governance`。ActorAuthority policy 采用版本化 operation catalog：现有 `actor-authority-policy.1` 的 digest 与 legacy operation 集合原样保留，只用于历史 replay；新增 `actor-authority-policy.2`，其 digest 必须覆盖含上述四个 v2 operation 的完整 canonical catalog。resolver 先按 policy version/digest 选 catalog，再验证 projection 的 allowed operations 是该 catalog 的合法 subset；四个 `.16` domain 只接受 `.2`。禁止给 `.1` authority 套新 schema/字段后声称拥有 v2 operation，policy/digest不匹配或operation不在对应版本catalog均fail closed。`OperatorObservationRecorded` 只能作为 `audit_observation_ref`，单独存在永远不能授权 mutation。

Compensation target 不只绑定 transition ID：必须 exact resolve latest transition 的accepted event ref/world revision/payload hash。effective lane只能从该exact target transition及既有compensation lineage重新推导，caller无权声明；若保留`expected_target_lane`，它只用于与推导结果比较，不参与授权。自由`correction_evidence_refs[]`不构成authority。Location首发使用`LocationOperatorCorrectionBasis`，closed class为`location_assignment_error|zone_assignment_error|scene_classification_error|privacy_classification_error`并要求current operator reauthorization；`.16.0`不允许把establish补偿回“无head”，初始位置录入错误必须由下一次current operator `change`显式纠正并保留历史。Resource使用`ResourceOperatorCorrectionBasis|ResourceSelfAssessmentCorrectionBasis`，后者class为`self_assessment_revised|source_interpretation_revised|constraint_reassessed`且必须携带新的exact internal intention；Attention使用`AttentionOperatorCorrectionBasis|AttentionReappraisalCorrectionBasis`，后者class为`attention_reassessed|focus_reassessed|expiry_reassessed`且必须携带新的exact attention intention。target event自身不能证明错误。撤销mechanical transition必须同时绑定原latest Clock authority与新的typed纠错authority；`.16.0` Resource recovery未安装，所以没有可生成的Resource mechanical lineage。

`DeliberativeBasisResolver` 是共享纯 Interface：它按 `source_kind` 选择唯一 typed parser，exact resolve 已提交 event/projection、actor、revision、payload hash 与适用能力，并返回 `ResolvedDeliberativeBasis{capabilities, privacy_floor, canonical_source_hashes}`；不得接收自由 EvidenceRef 列表，也不得用 ref 前缀猜类型。privacy lifetime order固定为`public < shareable < personal < private < withhold`。每次transition的required floor是before privacy、全部exact cause source、rationale、correction basis与contract/evidence privacy的最大值，after不得更弱；因此current head单调保存lifetime max，history validator仍逐项验证从未下降。compensation恢复target prior semantic values时，除privacy外exact restore；privacy取`max(target prior, current, correction sources)`，解决exact restore与终身不降级的冲突。Location的`scene_visibility`不参与该排序。Situation viewer继续做二次meet；无隐私字段或无法解析privacy的来源fail closed。

Internal intention 是“角色此刻自己想这样选择”的 authority，不是外部事实证明。它确保 spontaneous Goal direct 不需要伪造用户消息或等待外部 evidence，也确保 RandomAuthority 尚未完成不会阻塞 direct。其能力矩阵只允许 Goal open/revise/pause/resume/abandon、Attention change，以及有 pinned self-regulation policy 的 Resource deliberative adjustment；Goal progress 还必须引用 exact settled/Fact/Experience basis，internal intention 单独不足；Location establish/change、Goal completion、伪造 blocker 已解决或任何 external outcome始终禁止。`actor_ref` 必须等于被改实体 actor，trigger/decision slot/policy/hash 全部进入 proposal 与 accepted mutation hash。

Acceptance只表示“该主观选择获准成为相应domain mutation”，不会把`GoalRationale`或`InternalIntentionBasis`升级成客观证据。二者不得被Fact/Experience reducer采信，不得进入CompletionEvidence union，也不得作为未来CompletionContract的settlement source；需要记忆主观动机时只能按独立、明确支持的subjective source type处理。所有rationale text统一执行trim→NFC，再按normalized Unicode code points验证1..512长度；包含任何Unicode General_Category=`Cc` control的文本拒绝，normalized text与privacy进入canonical hash。禁止rationale ref、任意JSON blob或未限长自由字段。Situation internal projection可保留结构化class，viewer默认不输出原始text，只有明确viewer/privacy grant才可披露。

domain reducer 还要限制允许的 basis kind、capability 和 event type。例如图片 inspection result 不能成为 Location change；terminal `ExecutionReceiptRecorded` 首发只能作为Deliberation输入，不能由settlement adapter直接调整Resource，也不能成为Goal CompletionEvidence。Activity/Occurrence/Receipt缺少movement/recovery typed authority，相关adapter capability在`.16.0`为空。Activity/Action/Receipt completion留待对应terminal origin/outcome authority硬化后的future union版本；禁止拼ID/status。用户或模型的一句话可以成为角色 deliberation 的输入，但不能被升级成 Location、机械progress 或 completion 事实。

## 7. Settlement 两阶段与逐域提交

Settlement authority 固定采用两阶段，不引入 prospective binding：

```text
world revision N:
  ActivityCompleted / WorldOccurrenceSettled / terminal receipt committed

evaluated_world_revision=N:
  Deliberation evaluates exact committed source
  → no-change, or ProposalRecorded(V2ResourceStateAdjusted,
      cause = typed deliberative basis/internal self-assessment)
world revision N+1:
  AcceptanceRecorded(resource) → V2ResourceStateAdjusted

evaluated_world_revision=N+1:
  Deliberation evaluates the committed settlement / Fact / Experience
  → either no-change trace, or ProposalRecorded(
      V2GoalProgressed,
      basis = exact committed source,
      contribution_class + rationale + positive delta)
world revision N+2:
  only when proposed: AcceptanceRecorded(goal) → V2GoalProgressed

evaluated_world_revision=N+2:
  Deliberation re-evaluates latest Situation + exact committed source
  → no-change, or ProposalRecorded(V2AttentionChanged,
      cause = typed deliberative basis/internal intention)
world revision N+3:
  AcceptanceRecorded(attention) → V2AttentionChanged
```

规则：

1. Settlement 必须先成为 `CommittedWorldEventRef`；proposal 不得引用未来 event ID，也不得因“计划同批提交”跳过 source resolver；
2. 每个 domain change 有独立 mutation hash、proposal、acceptance 和 CAS；每个 `AcceptanceRecorded → mutation` pair单独提交；
3. 一个pair成功会推进world revision，下一域proposal必须在该最新revision重新评估，不能复用N上的旧proposal；
4. 任一before image、source authority、Acceptance或hash失败只回滚当前pair；先前已经提交的settlement和其他domain pair保持有效；
5. 部分更新是合法且必须可表示的状态：例如Resource已调整而Goal/Attention未调整。Situation只展示已提交heads，不推断或自动补齐缺失域；
6. 未列出的projection保持byte-equivalent；
7. `.16.0` 的Goal、Location、Resource、Attention都没有installed settlement写adapter：adapter只暴露committed source；Deliberation可选择no-change、主观Resource/Attention/Goal更新或recognize strict Goal CompletionEvidence，Location仍只能operator改变；
8. retry只复用仍pin当前revision的direct proposal；revision变化后旧proposal必须stale并重新deliberation；
9. trace逐域记录成功、拒绝、跳过与待重审，不使用“跨域原子成功”措辞，也不引入UoW manifest。

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
| goals | non-terminal Goal heads | stable sort `(importance desc, due relation, id)`；默认只投影status/class，不投影GoalRationale/InternalIntention text | 空列表 | 每 Goal privacy filter；原文需explicit viewer grant |
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
| GS16-AUTH-009 | 前一domain pair成功、后一pair失败 | 前一pair保留、当前pair原子回滚；Situation显示合法部分更新 |
| GS16-AUTH-010 | 只有 OperatorObservation、无 active domain ActorAuthority | operator mutation 拒绝 |
| GS16-AUTH-011 | ActorAuthority operation/expiry/principal/values digest 不匹配 | 拒绝 |
| GS16-AUTH-020 | policy.1 authority伪装包含v2 operation、policy version/digest/catalog subset不匹配 | `.16` domain拒绝；policy.1历史仍可按legacy catalog replay |
| GS16-AUTH-021 | Clock cause自报latest ref/revision/hash/from/to，或绕过projection/history | 拒绝；只信`resolve_latest_clock(pinned_state)`结果 |
| GS16-AUTH-022 | Clock history latest revision不是最大、to不等current logical time、policy artifact不匹配 | consistency/resolver fail closed |
| GS16-AUTH-012 | `.16` 中提交任意 `selection_mode=random_draw`，包括携带完整/薄 RandomDrawProjection | 一律 `random_authority_not_installed`；零状态变化 |
| GS16-AUTH-016 | direct 携带任何 RandomDrawAuthority；Goal direct 不带 draw | 前者拒绝；后者按正常 typed basis/authority 验证，不被 RandomAuthority 缺失阻塞 |
| GS16-AUTH-018 | deliberative basis 是自由 EvidenceRef、错 typed parser/actor/revision/hash/capability | ProposalRecorded 拒绝且零残留 |
| GS16-AUTH-019 | after privacy 弱于最严格 basis privacy；internal intention 声称 public | 拒绝；internal intention floor 固定 private |
| GS16-AUTH-017 | proposal transition_kind/event映射错误、payload JSON非object或非canonical | ProposalRecorded拒绝且零残留 |
| GS16-AUTH-013 | after.updated_at/opened_at/since/closed_at 不等 event/Clock time，或updated_at早于before | 拒绝 |
| GS16-AUTH-014 | mechanical event 被注册为 typed mutation 或携带 Acceptance | registry/contract test 失败 |
| GS16-AUTH-015 | legacy `GoalProgressed/Resumed/Abandoned/Compensated` payload 进入 v2 | unknown event/contract 拒绝；只接受冻结的 `V2*` 名称 |

### 9.2 Goal

| ID | 攻击 | 预期 |
|---|---|---|
| GS16-GOAL-001 | progress delta ≤0、溢出或 before+delta≠after | 拒绝；禁止 clamp/空mutation |
| GS16-GOAL-002 | progress 到 10000 自动 completed | head 仍非 terminal |
| GS16-GOAL-003 | CompletionContract unknown kind/schema、registry mapping/digest/privacy错误、evidence早于cutoff或parser值不匹配 | 拒绝 |
| GS16-GOAL-004 | pending Action/provider accepted 当 completion evidence | 拒绝 |
| GS16-GOAL-005 | settled evidence hash/revision/event type 不匹配 | 拒绝 |
| GS16-GOAL-006 | terminal goal pause/resume/progress/reopen | 拒绝 |
| GS16-GOAL-007 | Expired 使用 wall clock、非resolver latest Clock projection、cause自报Clock或未到due | 拒绝 |
| GS16-GOAL-008 | compensation 指向非latest/其他goal/错event binding，或caller用target lane字段升权 | 拒绝；effective lane从exact target/lineage重导 |
| GS16-GOAL-009 | blocker ref 不存在、跨 world 或 aliases 另一 authority | 拒绝 |
| GS16-GOAL-010 | Goal event 改 Affect/Memory/Attention | zero-cascade diff 失败 |
| GS16-GOAL-011 | 多 blocker 只解除一个却被强制 active | 保持 blocked；exact diff 保留其余 blocker |
| GS16-GOAL-012 | Runtime见到settlement自动增长progress；或Deliberation无exact settled/Fact/Experience basis | 拒绝；source只供主观评估，no-change不产生event |
| GS16-GOAL-013 | revise 偷改 outcome/identity/progress 或事后伪造 completion contract | 拒绝 |
| GS16-GOAL-014 | supersedes_goal缺失、跨actor、自指、非terminal或stale exact binding | 拒绝；只有同actor既存terminal Goal可被supersede |
| GS16-GOAL-015 | spontaneous internal basis + direct 打开合法 Goal | 接受；不要求外部evidence或RandomAuthority |
| GS16-GOAL-016 | reducer按event/outcome类型硬编码progress delta或contribution | property test失败；只验typed basis、正delta算术、隐私与时间 |
| GS16-GOAL-017 | pause/resume/abandon自由reason字符串或错operation分类 | schema/reducer拒绝；只接受对应typed reason catalog |
| GS16-GOAL-018 | Complete把evidence塞进cause、deliberative/operator无独立union evidence、错union/parser | 拒绝；两lane均strict，operator只reauth |
| GS16-GOAL-019 | Activity/Plan/Action/Receipt用ID/ref/status/event type拼CompletionEvidence | unsupported/fail closed；首发只装settled occurrence与active Fact parser |
| GS16-GOAL-020 | accepted rationale/internal intention被Fact、Experience或Completion resolver采信 | 拒绝；Acceptance不升级主观material |
| GS16-GOAL-021 | rationale非canonical trim/NFC、空/超长、任意Unicode Cc、自由ref/blob或viewer默认泄露原文 | schema/projection测试失败 |
| GS16-GOAL-022 | 裸blocker、wrong removed fingerprint/basis/rationale、settlement自动Block/Unblock、operator直接选择 | 拒绝；typed blocker + deliberative-only，纠错走exact compensation |
| GS16-GOAL-023 | settlement adapter提交任意Goal mutation | 拒绝；Goal无settlement写lane，source只供Deliberation |
| GS16-GOAL-024 | blocked Goal Complete后仍保留blocker或removed fingerprints不完整 | 拒绝；after blockers显式为空 |

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
| GS16-LOC-008 | Activity/Occurrence/Receipt或opaque result ref作为settled movement | `location_movement_authority_not_installed`；零变化 |
| GS16-LOC-009 | location/zone变化未重置since，或metadata-only change重置since | 拒绝；按冻结chronology处理 |

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
| GS16-RES-009 | `.16.0`提交任意Clock recovery或settlement adapter adjustment | `resource_recovery_authority_not_installed`/capability拒绝；零变化 |
| GS16-RES-010 | state_change delta=0，或任意reclassify（包括偷改value/伪造新policy） | `.16.0` registry-empty拒绝 |

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
| GS16-ATT-008 | 同一Attention revision在旧trigger terminal后由新Clock再次打开 | deterministic revision identity拒绝；每revision最多一次 |
| GS16-ATT-009 | 裸focus_ref、错current projection hash/status/actor/world，或`.16.0`提交任意Trigger focus | typed focus resolver拒绝；Trigger focus报authority未安装 |
| GS16-ATT-010 | expiry binding错attention fingerprint/expires_at/Clock/policy/idempotency | 拒绝；Attention与trigger均零变化 |

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
| GS16-SQL-008 | `.15` legacy hash错误包含Clock history，或`.16` reopen丢失/改写Clock history | 前者按legacy exact hash拒绝漂移；后者roundtrip/rebuild测试失败 |

## 10. `.15→.16` 迁移清单

### 10.1 ReducerState 与 semantic payload

新增字段：

```text
goals, goal_transitions, goal_proposals, goal_proposal_ids
locations, location_transitions, location_proposals, location_proposal_ids
resources, resource_transitions, resource_proposals, resource_proposal_ids
attentions, attention_transitions, attention_proposals, attention_proposal_ids
clock_transition_history
```

不新增 `situations` 或 `situation_transitions`。Situation 是 Snapshot 编译产物。

`.16` semantic payload 包含四组 head/history、`clock_transition_history`与WorldOccurrence的`settled_outcome_ref`；pending proposal 和 proposal ID 沿用既有决策：若它们当前不属于 semantic payload，则保持一致，不为 `.16` 单独改变 hash 语义。`.15` legacy semantic branch明确不含Clock history与`settled_outcome_ref`。所有 live consistency validator 检查：唯一 identity、连续 revision、before/after 连续、head 等于 latest transition、proposal durable index完整，以及Clock history revision/event唯一递增、latest.to等于current logical time、Occurrence只有settled current head可带outcome。

### 10.2 Verified migration 顺序

1. 读取 `.15` head cursor、bundle、state JSON、semantic hash、state hash；
2. 用保留的 `.15` semantic payload 分支验证旧 semantic hash，并用现有 persisted state hash 验证 head state/cursor；
3. 不声称调用不存在的 `.15` reducer runner；仓库当前只安装目标 reducer artifact。若未来引入 archived bundle runner，必须另立 ADR/bundle，不在 `.16` 临时扩 scope；
4. 用 `.16` 目标 reducer 从 immutable events replay 到相同 cursor；reducer从既有`ClockAdvanced` events重建`clock_transition_history`，并从typed occurrence settlement events重建`settled_outcome_ref`；不改event envelope logical_time语义，旧未注册事件按既有 catalog/legacy handling fail closed，不倒造 `.16` events；
5. 目标 `.16` state 的四组domain新字段全部空，Clock history按旧Clock events重建；不解析旧 prompt、`SituationStateProjection`、legacy Goal/needs/location/attention 字符串；
6. 比较目标 replay 的既有 `.15` authority slices 与已验证 head，确保domain新字段为空，新增的Clock history与Occurrence outcome都可由旧typed events逐项复算；
7. 在一个 SQLite transaction 写 state JSON、semantic hash、bundle version、state hash；
8. close/reopen，再 direct rebuild，三者 projection/hash/cursor 一致；
9. migration 测试断言旧domain字段没有被升格、新domain slice unavailable，并验证Clock history与旧`ClockAdvanced` log逐项一致；不新增未定义的持久化 migration report。原始 legacy 内容仍只存在于其原始事件/旧存储归档；
10. 更新 supported migration set，保留 `.15` legacy semantic payload/hash 分支。

对于新世界，Goal/Location/Resource/Attention 都可为空并由 Situation 显式输出 unavailable。需要初始化 Location/Resource/Attention 时，必须由 active deployment ActorAuthority 通过相应 operator proposal；不能靠 Pydantic defaults、`WorldStarted` 或隐式 seed lane 制造状态。Goal 可由普通 deliberative proposal 打开。

## 11. Runtime 与消费者接线清单

### 11.1 Producers

- `WorldRuntime` 暴露四个 domain proposal command，不暴露“set situation”；
- Deliberation 只能返回 typed proposal candidates；Adapter 不自行接受；
- Operator command 必须解析 active domain ActorAuthority；可另写 exact observation 作审计，再提 operator-lane proposal，但 observation 不授权；
- activity/occurrence/action settlement先commit；`.16.0`没有Location/Resource/Attention settlement写adapter，Deliberation可在下一revision把exact source作为输入后提出Resource/Attention候选或no-change，Goal progress同样只能主观评估；
- `advance()`在`ClockAdvanced`后确定性枚举due Goal并写exact mechanical after；Attention expiry trigger在`.16.0`共享TriggerProcess authority未安装，必须fail closed且不产event；未来新bundle启用后才可为每个current Attention revision打开一次typed exact due trigger；Resource recovery registry同样为空，必须fail closed且不产event；
- 未来安装recovery authority后才启用其确定性idempotency material与冻结公式；
- `.16` producer 只生成 `selection_mode=direct`；不得生成、伪造或接受 RandomDraw。后续 RandomAuthority 通过独立 bundle 验收后才能启用。

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

1. `ActivityCompleted committed → Deliberation对Resource作no-change或typed subjective adjustment → Deliberation对Goal作no-change或带contribution/rationale的positive-progress pair → Deliberation对Attention作no-change或typed change → 每步 Situation recompiled`，并含后一pair失败而前面部分更新保留的分支；
2. `ClockAdvanced → V2GoalExpired + attention expiry due trigger；Resource recovery fail closed且零event → Deliberation可选V2AttentionChanged/no-change → Situation recompiled`；
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
- ActorAuthority policy.1/digest/legacy catalog保持可回放；`.16`只接受digest绑定四个v2治理operation catalog的policy.2，并验证allowed-operations subset；
- ClockAdvanced reducer冻结computed event ref/world revision/payload hash/from/to/installed policy到immutable history；domain只信latest resolver，Clock history进入`.16` semantic/SQLite/rebuild而不进入`.15` legacy hash，旧event envelope logical_time语义不变；
- settlement source先commit；`.16.0`只允许Deliberation在下一revision把它作为typed输入，Location/Resource/Attention settlement写adapter均fail closed；每域pair按最新revision串行提交，部分更新合法；
- Goal CompletionContract为typed frozen contract，cutoff后的exact evidence才能完成；
- `.16` 对所有 `selection_mode=random_draw` 无条件 fail closed，薄 RandomDrawProjection 不算支持；`direct` 是可用路径且 Goal direct 不因 RandomAuthority 缺失受阻；
- deliberative mutation 只接受 typed `DeliberativeBasisBinding`，resolver exact 解析 capability 与最严格 privacy floor；spontaneous internal basis 可授权受限的 Goal/Attention/Resource 自主选择，但不能伪造 Location、机械progress/completion；Goal progress还须exact settled/Fact/Experience basis并由Deliberation主观给出正delta；
- `supersedes_goal_id` 只可 exact 指向同 actor、非 self、既存且 terminal 的 Goal；
- Goal progress只由Deliberation基于exact settled/Fact/Experience作正delta主观评估，no-change零Goal event，Runtime不自动增长；Complete仍使用strict contract；pause/resume/abandon使用typed reason catalog；
- Complete evidence与cause分离；deliberative recognition/operator补结算均strict且operator只reauth，Goal无settlement写lane；首发只支持settled occurrence outcome与active Fact predicate；
- CompletionContract closed registry冻结kind/schema/digest/privacy，Goal privacy meet contract/evidence/basis/rationale/target；unknown或不匹配fail closed；
- GoalRationale/InternalIntention使用closed class + trim→NFC bounded text，拒绝全部Unicode Cc并带privacy；Acceptance不升级为Fact/Experience/Completion证据，viewer默认隐藏原文；
- Goal blocker是带fingerprint的typed集合，Resolution逐项绑定removed fingerprint/basis/rationale；Block/Unblock与Progress/Pause/Resume/Abandon均deliberative-only，blocked Complete显式清空blockers，operator纠错走exact compensation；
- 每域proposal的transition_kind为closed Literal并与event type完整映射，canonical payload JSON必须解码为object；
- after/opened/since/closed 时间全部 pin event/Clock Logical Time；
- Goal Clock expiry与Attention due有exact authority和确定性idempotency；Attention每revision最多一个trigger；Resource recovery registry为空且零变化fail closed；
- Resource band policy的closed kind/interval/digest已冻结，分类不映射固定行为；未来recovery只有在typed interval authority与冻结公式安装后才能启用；
- Location `.16.0` operator-only；Plan/Occurrence/Receipt/图片/用户陈述不能冒充movement；
- 三域compensation使用domain typed correction basis，按lineage重导effective lane；privacy除外exact restore并保持`public < shareable < personal < private < withhold` lifetime max；
- Attention focus使用typed current projection binding；expiry trigger携带exact Attention/Clock/policy binding且open/claimed/terminal均占用revision identity；
- 每个 domain event 通过 zero-cascade projection diff；
- SituationCompiler 无写事件、无 I/O、无 wall clock、无模型、无随机；
- source matrix 每个输出字段都有唯一 authority 和 source revision；Location scene visibility 与 disclosure privacy 已拆分；
- missing 与 redacted 显式区分；viewer 不泄露 private Goal/Location/Resource/Attention；
- live compile、cache compile、reopen compile、rebuild compile byte-equivalent；internal semantic hash 不因 viewer/budget 改变，viewer projection hash 精确绑定 scope/budget；
- `.15→.16` 非空 SQLite migration、连续跨版本、tamper 和中断恢复通过；
- WorldOccurrence `settled_outcome_ref`只由typed settlement冻结，`.15` semantic排除、`.16` semantic/SQLite/replay纳入；
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
- latest Clock按`clock_transition_history`最大computed world revision选择并要求to等于current logical time，不信cause自报；
- Location/Attention 每 actor 单 head，Resource 每 `(actor, kind)` 单 head，Goal 每 ID 单 head；
- Resource 首发只有三个 kind，所有数值为 `0..10000` 定点；
- 到期只打开/执行显式 transition，不做查询时 TTL；
- terminal Goal 不 reopen，progress `10000` 不自动 complete；
- `.16` 仅安装 direct selection；RandomAuthority 的 trigger/slot、prior entropy、weighted candidates、frequency budget、deterministic sampler、Recorded/Superseded Ledger reducer与防复用消费合同属于后续 bundle；
- cross-domain change 按每域pair逐次提交，不通过reducer cascade；已提交部分不因后续域失败回滚；
- legacy 裸状态只隔离，不升级为 typed authority；
- Compiler 的 internal semantic identity 包含 internal policy/source revisions；viewer scope、budget 和 truncation 只进入独立 viewer projection identity；
- 人类感来自主 Deliberation 对可靠处境的自由解释，不来自 reducer 的固定行为表。

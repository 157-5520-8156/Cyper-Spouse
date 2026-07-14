# World v2 `.16` 可验收实现清单

> 配套设计：[world-v2-goal-situation-16.md](world-v2-goal-situation-16.md)
> 用途：把 Goal、Location、Resource、Attention、SituationCompiler、integration/migration 拆成可以独立实现、审查和验收的工作片。
> 状态：施工清单，不代表任何条目已完成。以实际代码、测试、replay 和提交为准。

## 1. 使用规则

- 每个工作片必须从现有 Ledger/typed proposal/Reducer Interface 进入，不能另建旁路 manager。
- 表中“最小测试”是合并该片的下限，不代替最终全量测试。
- `schemas.py`、`reducers.py`、`typed_proposal_families.py`、`event_catalog.py`、`sqlite_ledger.py` 是共享热点；同一时间只允许一个 merge owner 修改。
- Goal、Location、Resource、Attention 的纯 payload/reducer/test 文件可以并行准备，注册和 bundle/migration 必须串行。
- mechanical event 不注册为 typed proposal mutation；非 mechanical mutation 必须经 ProposalRecorded → AcceptanceRecorded → domain event。
- SituationCompiler 是只读 Module；不得注册 `SituationChanged` 或持久化 Situation head/history。
- 所有新 domain event 使用冻结的 `V2*` 名称；legacy `GoalProgressed/Resumed/Abandoned/Compensated` 不得进入 v2 catalog。

完成标记：

- `[ ]` 未开始；
- `[~]` 已实现但未完成该片验收；
- `[x]` 已有代码、最小测试、全量回归与审查证据。

## 2. 当前仓库 seam 总图

| Seam | 当前文件/符号 | `.16` 必须接入 | 最容易漏的后果 |
|---|---|---|---|
| Domain schemas | `src/companion_daemon/world_v2/schemas.py` | heads、transitions、proposal projections、typed deliberative basis/ActorAuthority bindings、保留但禁用的RandomDraw wire schema、LedgerProjection/Snapshot fields | reducer 工作但 projection/SQLite 丢字段 |
| Domain payloads | 现有 `*_events.py` 模式 | 新建 `goal_events.py`、`location_events.py`、`resource_events.py`、`attention_events.py` | event catalog 只能做宽松 dict 校验 |
| Pure reducers | 现有 `*_reducers.py` 模式 | 新建四个 reducer Module | 逻辑继续堆进总 `reducers.py`，难测且易级联 |
| Typed family codec | `typed_proposal_families.py` 的 codec、`INSTALLED_TYPED_PROPOSAL_FAMILIES` | 四个 selector/contract/非 mechanical mutation owner | ProposalRecorded 可存但 batch invariant 找不到 mutation，或 selector 冲突 |
| Proposal store | `reducers.py::_TYPED_PROPOSAL_STORES` 与各 `_XProposalStore` | 四个 store + registry construction | family manifest 有记录但 runtime store KeyError/找不到 proposal |
| Proposal reducer | `reducers.py::_proposal_recorded`、各 `_x_proposal_recorded` | 四个 dry-run/store handler | proposal 不验证 current revision/source/CAS |
| Deliberative basis | 新建 `deliberative_basis.py` + typed schema bindings | exact parser dispatch、capability、canonical source hash、privacy floor、internal intention | 自由EvidenceRef升权或敏感basis写成public head |
| Mutation authority | `reducers.py::_require_authorized_*` | 四个非 mechanical require helpers | rejected/non-adjacent/wrong-hash mutation被接受 |
| Batch invariant | `batch_invariants.py::validate_commit_batch` | 验证四 family 的相邻 Acceptance；确认 mechanical event不在 family owner map | Clock event被错误要求 Acceptance，或 accepted mutation可不相邻 |
| Event payload catalog | `event_catalog.py::_PAYLOAD_MODELS` 与 domain payload map imports | 四域 payload maps；TriggerProcess扩展typed Attention expiry binding | reducer注册了但 catalog拒绝，或 payload只按宽松模型校验 |
| Event descriptive contract | `event_catalog.py` 的 `_contract(...)` 集合 | producer、revision、predecessor、evidence、successor、compensation、bundle `.16` | catalog schema与实际 reducer语义漂移 |
| Event reducer registry | `reducers.py::_EVENTS` | typed mutation、Goal/Resource mechanical event、现有 TriggerProcess handler | event catalog知道事件但 Ledger报 UnknownEventType |
| Clock authority index | `ClockAdvanced` handler + 新`ClockTransitionProjection/history` | reducer-computed event/ref/revision/hash/from/to/installed policy；latest resolver | domain相信cause自报latest Clock，或reopen后authority丢失 |
| Completion source | `WorldOccurrenceProjection` / Fact heads | `.16`新增settled_outcome_ref；只装settled occurrence + active Fact closed parsers | 拼Activity/Action/Receipt ID或status伪造complete |
| Reducer state | `reducers.py::ReducerState` + consistency validator | 四组 heads/history/proposals/proposal_ids + Clock history | live commit后 state可形成断裂 lineage |
| Semantic identity | `ReducerState.semantic_payload`、`semantic_hash` | `.16` 加入四域 head/history与Clock history，不加入 Situation cache；`.15`排除Clock history | reopen/rebuild hash不同或破坏legacy hash |
| Projection | `reducers.py::make_projection`、`schemas.py::LedgerProjection` | 四域所有 state字段 + Clock history | in-memory state正确但 caller/SQLite不可见 |
| SQLite state roundtrip | `sqlite_ledger.py::_state_from_projection` | 四域全部字段 + Clock history | reopen后新authority静默清空 |
| SQLite migration | `sqlite_ledger.py` installed set、`_legacy_semantic_hash`、open migration transaction | `.15→.16` verified migration及`.15` legacy branch | 非空旧库拒开、旧hash误验、半迁移 |
| Trigger lifecycle | `schemas.py::TriggerProcess`；`reducers.py::_trigger_process_opened/_claimed/_reclaimed/_completed` | `v2_attention_expiry_due` kind及exact authority identity | due trigger无法携带authority、claim时身份丢失、重复打开 |
| Runtime advance | `world_v2/runtime.py`、Clock/trigger调用路径 | 枚举Goal expiry、Resource recovery、Attention due | schema/reducer完整但永远没有producer |
| Snapshot/compiler | `schemas.py::InternalWorldSnapshot`、现有 projection/runtime组装 | pinned authority input、SituationCompileResult | Compiler读取“最新”Ledger或旧裸current_situation |
| Regression contracts | `test_event_catalog.py`、`test_schema_contracts.py`、`test_typed_proposal_families.py`、`test_architecture_contract.py` | 新事件/selector/import/purity覆盖 | 单域测试绿但全局manifest或import CI失败 |

## 3. 合并前冻结项

- [ ] 确认 bundle 起点是已验收的 `world-v2-reducers.15`，工作树没有未完成 `.15` 语义。
- [ ] 冻结四个 proposal selector：
  - [ ] `v2_goal_transition` / `proposal-contract:v2-goal.1`
  - [ ] `v2_location_transition` / `proposal-contract:v2-location.1`
  - [ ] `v2_resource_transition` / `proposal-contract:v2-resource.1`
  - [ ] `v2_attention_transition` / `proposal-contract:v2-attention.1`
- [ ] 冻结每域`transition_kind` closed Literal与event type一一映射；`*ProposedMutation.payload_json`必须为canonical JSON object，禁止array/scalar/任意字符串。
- [ ] 冻结 `V2*` event name、operation literal、mechanical/typed 分流；mechanical events不出现在 family `mutation_event_types`。
- [ ] 冻结四个 ActorAuthority required operation：`v2_goal_governance`、`v2_location_governance`、`v2_resource_governance`、`v2_attention_governance`。
- [ ] 保留`actor-authority-policy.1` digest与legacy operation catalog用于历史回放；新增policy.2 digest，覆盖包含四个v2治理operation的canonical catalog；`.16`四域只接受policy.2。
- [ ] 冻结 Goal、Resource、Attention policy artifact/version/digest 常量；禁止测试临时字符串成为事实标准。
- [ ] 冻结 `selection_mode=direct|random_draw` wire literal；`.16` 只安装 direct，任意 random_draw（包括完整或薄RandomDrawProjection）一律 `random_authority_not_installed` fail closed；Goal direct不得被阻塞。
- [ ] 冻结 typed `DeliberativeBasisBinding`、纯 `DeliberativeBasisResolver`、capability matrix 与 privacy floor；禁止自由 EvidenceRef 列表，internal intention floor固定private。
- [ ] 冻结 `supersedes_goal_id` exact binding：同actor、非self、既存current head且terminal。
- [ ] 冻结 cause之外独立typed `GoalCompletionEvidence` union与`evidence_cutoff_world_revision`：Complete为deliberative recognition/operator补结算，二者都必填strict evidence；Goal无settlement写lane。
- [ ] 冻结`InternalIntentionBasis.intention_class`等外层assessment/reason closed分类；`GoalRationale`只含NFC bounded text + privacy，不重复保存分类；禁止rationale ref/blob，viewer默认不披露原文。
- [ ] 冻结head `GoalTerminalReason` structured union：abandon复用typed lifecycle reason，complete/expire绑定evidence/Clock；禁止terminal_reason_ref。
- [ ] 冻结typed `GoalBlocker/GoalBlockerResolution`集合；Block/Unblock不存裸refs、不由settlement自动改变。
- [ ] 冻结Goal closed lane matrix：Complete=deliberative recognition/operator strict-evidence补结算；Progress/Block/Unblock/Pause/Resume/Abandon deliberative-only；operator其余只Open初始化/导入、Revised治理修正和operator-origin compensation reauth；无settlement写lane。
- [ ] 冻结`goal-completion-contract-registry.1`的closed kind/schema/digest/privacy mapping；unknown/mismatch fail closed。
- [ ] 冻结 compensation exact event binding；effective lane只从target transition/compensation lineage重导，caller字段仅可作expected比较、不授权。
- [ ] 冻结`ClockTransitionProjection/history`：ClockAdvanced reducer计算event ref/world revision/payload hash/from/to/installed policy；domain只信projection resolver。
- [ ] 冻结latest Clock resolver：选择history中computed world revision最大项，并要求`logical_time_to == ReducerState.logical_time`；cause自报值无authority。
- [ ] 冻结`.15` semantic排除Clock history、`.16` semantic/SQLite/rebuild包含Clock history；`.16` migration由旧ClockAdvanced replay重建。
- [ ] 冻结统一 chronology：after.updated_at/accepted_at/event logical time，opened/since/closed，Clock logical_time_to。
- [ ] 冻结`.16.0` installed capability matrix：Location operator-only；Resource initialize=operator、adjust/reclassify=operator/deliberative；Attention establish=operator、change=operator/deliberative；三域settlement写adapter全部fail closed。
- [ ] 冻结Location movement capability为空：Activity/Occurrence/Receipt/Plan location/图片/opaque result ref均不能证明移动；未来必须新增typed MovementSettledProjection才能启用。
- [ ] 冻结`resource-band-policy.1`：三个kind共用`depleted 0..999 / low 1000..3499 / moderate 3500..6499 / high 6500..8999 / full 9000..10000`，digest覆盖kind、区间与整数算术；band不映射行为。
- [ ] 冻结Resource recovery registry为空：`V2ResourceClockAdjusted` wire保留但`.16.0`始终`resource_recovery_authority_not_installed`；禁止从Activity/Occurrence/Receipt猜休息。
- [ ] 冻结Attention typed focus binding与mode结构不变量；禁止裸focus ref，allocation/interruptibility不按mode硬映射。
- [ ] 冻结`AttentionExpiryDueBinding`与`attention-expiry-policy.1`：每Attention revision最多一个trigger，open/claimed/terminal均占用identity，Clock只开trigger不改head。
- [ ] 冻结三域domain-specific typed compensation correction basis；禁止自由`correction_evidence_refs[]`，target event不能自证错误。
- [ ] 冻结privacy lifetime order `public < shareable < personal < private < withhold`；每transition取before/source/rationale/contract/correction最大floor，compensation除privacy外exact restore。
- [ ] 冻结旧event envelope `logical_time`字段语义不变；不得复用为Clock from/to、computed revision或projection timestamp。
- [ ] 冻结 Situation internal hash与 viewer projection hash的独立 canonical material。

## 4. 工作片 G：GoalAuthority

### 4.1 交付 Interface

- [ ] `GoalChangedPayload` 覆盖 open/revise/progress/pause/resume/block/unblock/complete/abandon/compensate。
- [ ] `GoalExpiredPayload` 是独立 mechanical contract，不继承 proposal/acceptance envelope。
- [ ] `reduce_goal(...) -> (heads, history)` 是纯函数，只修改目标 Goal。
- [ ] `GoalProposalProjection` / `GoalProposedMutation` 使用冻结 routing。
- [ ] `GoalClockAuthority`、typed CompletionContract/evidence、settled cause、ActorAuthority、exact-event compensation target均为discriminator typed binding；Completion resolver按contract_kind纯解析settled payload/result/Fact outcome与actor。
- [ ] `GoalCompletionEvidence`为cause之外独立union；`V2GoalCompleted`任意lane必填，operator ActorAuthority不能替代evidence。
- [ ] Goal deliberative direct支持typed committed basis或spontaneous internal intention basis，不依赖RandomAuthority。

### 4.2 文件与接线

- [ ] 新建 `src/companion_daemon/world_v2/goal_events.py`：
  - [ ] `GOAL_PAYLOAD_MODELS` 只含 typed events；
  - [ ] `GOAL_MECHANICAL_PAYLOAD_MODELS` 只含 `V2GoalExpired`；
  - [ ] mutation hash包含selection_mode、完整typed basis和全部authority；random_draw保留字段即拒绝；
  - [ ] canonical source hashes由 `DeliberativeBasisResolver` 的typed source binding派生，禁止 caller自由拼装 EvidenceRef。
  - [ ] Goal proposal `transition_kind`/event完整映射；payload JSON object/canonical roundtrip validator。
- [ ] 新建 `src/companion_daemon/world_v2/goal_reducers.py`：
  - [ ] 状态矩阵含 revise/reprioritize/reschedule/recontract；
  - [ ] progress仅由Deliberation主观评估exact committed settled/Fact/Experience basis；Runtime不得自动增长；
  - [ ] `GoalProgressAssessment`记录contribution_class、bounded rationale与basis；strictly-positive delta只记录在mutation的`progress_delta_bp`；reducer只验typed authority/capability、算术/范围、时间、privacy，不硬编码event/outcome相关性；
  - [ ] rationale使用内嵌`GoalRationale` closed class；trim→NFC后1..512 code points，拒绝全部Unicode category Cc control；带privacy并禁止ref/blob；
  - [ ] no-change不产生`V2GoalProgressed`或delta=0空mutation，可只留审议trace；paused/blocked合法positive progress不隐式换状态；
  - [ ] partial unblock保持 blocked，最后一个 blocker移除才 active；
  - [ ] blockers为canonical typed `GoalBlocker`集合并带fingerprint；Unblock Resolution逐项绑定removed fingerprint+basis+rationale；externally_resolved仅CommittedEvidence，主观放下三类才允许InternalIntention；
  - [ ] progress 10000不自动 complete；
  - [ ] completion只认registry中的typed frozen contract + cause之外独立union evidence；首发只支持settled occurrence outcome与active Fact predicate，deliberative/operator均strict，operator只reauth；
  - [ ] blocked→completed after显式清空blockers，transition记录全部removed fingerprints；禁止terminal head残留blocker；
  - [ ] expiry只认共享resolver返回的latest `ClockTransitionProjection`及frozen due/policy；cause自报Clock字段不授权；
  - [ ] compensation exact latest transition + accepted event ref/world revision/payload hash，从target/lineage重导effective lane并恢复before，不信caller lane、不删除历史；
  - [ ] operator lane解析 active ActorAuthority，不把 OperatorObservation当授权；
  - [ ] Goal privacy不得弱于basis、GoalRationale、superseded target、CompletionContract与completion evidence最严格floor；internal intention只具冻结capability；
  - [ ] open携带supersedes时exact解析同actor、非self、既存terminal current Goal head/event binding，并把派生GoalSupersessionEvidenceRef加入canonical EvidenceRefs；
  - [ ] pause/resume/abandon使用operation-specific typed reason catalog；自由字符串和错分类拒绝；与Progress/Block/Unblock均deliberative-only；
  - [ ] terminal after写structured GoalTerminalReason并与operation/evidence exact一致；不存在dead/free reason ref；
  - [ ] operator仅Open初始化/导入、Revised治理修正、strict-evidence Complete补结算；其他Goal选择纠错走exact compensation；
  - [ ] `after.updated_at == event logical time`且不早于before.updated_at；
  - [ ] zero-cascade。
- [ ] `schemas.py`：加入 Goal values/origin/head/transition/proposed mutation/proposal projection；加入 LedgerProjection字段。
- [ ] `typed_proposal_families.py`：
  - [ ] `_GoalFamilyCodec`；
  - [ ] selector=`(ProposalRecorded, v2_goal_transition)`；
  - [ ] contract=`proposal-contract:v2-goal.1`；
  - [ ] mutation owner不含 `V2GoalExpired`。
- [ ] `reducers.py`：
  - [ ] import payload/reducer/schema；
  - [ ] ReducerState fields + lineage validator；
  - [ ] `_GoalProposalStore` 并加入 `_TYPED_PROPOSAL_STORES`；
  - [ ] `_goal_proposal_recorded` dry-run；
  - [ ] `_goal_changed` / `_goal_expired`；
  - [ ] `_require_authorized_goal`；
  - [ ] `_EVENTS` world revision registrations；
  - [ ] semantic payload `.16` branch；
  - [ ] `make_projection` 映射。
- [ ] `event_catalog.py`：合并 typed/mechanical payload maps；producer分别是 `proposal_acceptance`/`world_runtime`；bundle标`.16`。
- [ ] `batch_invariants.py`：typed Goal由通用family invariant覆盖；增加contract test证明 `V2GoalExpired` 不被 `family_for_mutation` 所有。
- [ ] `sqlite_ledger.py::_state_from_projection`：复制 Goal heads/history/proposals/ids。

### 4.3 最小测试集合

建议新文件：`tests/world_v2/test_goal_authority.py`。

- [ ] 正常闭环：open → revise importance/due → Deliberation基于settled/Fact/Experience作positive progress或no-change → partial unblock → complete。
- [ ] typed authority：missing/rejected/non-adjacent Acceptance、wrong hash、stale exact before image、duplicate IDs。
- [ ] ActorAuthority：仅 OperatorObservation、wrong operation、expired authority、wrong values/policy digest 均拒绝；policy.1借新schema夹带v2 operation拒绝，policy.1 legacy replay保持一致，policy.2 allowed operations必须是catalog subset。
- [ ] state machine：terminal reopen/progress/revise拒绝；10000不隐式complete；partial unblock不误active。
- [ ] CompletionContract：deliberative recognition/operator补结算正常闭环；missing/错union、evidence塞cause、unknown kind/schema、registry mapping/digest/privacy mismatch、actor/outcome/fact predicate/value、future/noncurrent/nonsettled/inactive、evidence≤cutoff均拒绝；operator只能reauth。
- [ ] Goal settlement lane：settlement adapter提交任意Goal mutation均拒绝；只允许其source随后被Deliberation recognition/assessment消费。
- [ ] blocked Complete：after blockers未空、漏/错removed fingerprint拒绝；完整清空接受并rebuild一致。
- [ ] Activity/Plan/Action/Receipt completion kind一律unsupported/fail closed；拼goal_ref/intent_ref/ID/status/event type不能通过。
- [ ] Progress：Runtime自动增长、internal-only basis、delta≤0、错算术/范围、自由source、隐私降级均拒绝；相同source可由Deliberation合理给出不同正delta/class，reducer不按outcome硬编码；no-change零Goal event。
- [ ] typed lifecycle reason：pause/resume/abandon各自合法catalog接受；自由字符串、跨operation reason、普通operator代替角色决定均拒绝。
- [ ] terminal reason：abandon/complete/expire错union、悬空evidence/Clock、自由terminal_reason_ref拒绝；rebuild保持structured reason。
- [ ] GoalRationale/InternalIntention：trim→NFC canonicalization；空/超长/任意Unicode Cc control、自由ref/blob、unknown class拒绝；Acceptance后仍不能成为Fact/Experience/CompletionEvidence；viewer默认无原文。
- [ ] Blocker：裸ref、duplicate ID、wrong blocker/removed fingerprint/class/basis/rationale/privacy、externally_resolved+internal basis、自动settlement Block/Unblock拒绝；三种主观resolution+InternalIntention接受且不声称外部解决；partial unblock保留其余blockers。
- [ ] Clock：before due、非projection latest Clock、cause自报/wrong hash、history latest.to≠current time、duplicate expiry拒绝；resolved latest可补结算漏掉的due并rebuild；无需Acceptance。
- [ ] selection：direct带draw拒绝；任意random_draw（即便字段完整、已有薄projection）均fail closed；spontaneous internal basis + direct可正常open/revise Goal。
- [ ] deliberative basis：自由EvidenceRef、错typed parser/actor/revision/hash/capability、after privacy降级均拒绝；多source取最严格floor。
- [ ] supersedes：missing、cross-actor、self、active/paused/blocked、stale head/revision/event/hash、漏/换derived EvidenceRef均拒绝；同actor terminal exact target head/event binding接受且privacy floor生效。
- [ ] chronology：updated/opened/closed不pin Logical Time或updated_at相对before倒退均拒绝。
- [ ] zero-cascade：只改变 Goal heads/history和proposal消费；Fact/Experience/Memory/Core/Affect/Attention等byte-equivalent。
- [ ] in-memory `project == rebuild`。

Goal片验收门：上述测试独立通过，event catalog/typed family manifest测试通过；不要求SituationCompiler已完成。

## 5. 工作片 L：LocationAuthority

### 5.1 交付 Interface

- [ ] `LocationChangedPayload` 支持 establish/change；`.16.0`二者都只允许ActorAuthority operator lane。
- [ ] Location installed movement capability为空；settled movement wire/source一律`location_movement_authority_not_installed`，没有generic deliberative lane。
- [ ] `LocationChangeCompensatedPayload` exact latest。
- [ ] `LocationProjection` 分开 `scene_visibility` 与 `privacy_class`。
- [ ] `reduce_location(...) -> (heads, history)` 保证每actor一个current head。

### 5.2 文件与接线

- [ ] 新建 `location_events.py`：`LOCATION_PAYLOAD_MODELS`、typed cause union、hash/canonical source bindings。
- [ ] Location proposal `transition_kind`/event完整映射；payload JSON object/canonical roundtrip validator。
- [ ] 新建 `location_reducers.py`：
  - [ ] exact before/to；actor/identity连续；
  - [ ] Location/zone变化时since等于mutation Logical Time；只改scene visibility/privacy时preserve since；exact no-op拒绝；
  - [ ] privacy_class满足来源floor且普通revision不可无authority弱化；
  - [ ] 用户陈述、模型deliberation、图片背景、Plan/Occurrence/Receipt、opaque result ref不能移动角色；
  - [ ] operator初始化/纠错解析active ActorAuthority；
  - [ ] `LocationOperatorCorrectionBasis` closed correction class + current operator reauthorization；target event不自证错误；
  - [ ] compensation除privacy外exact restore，privacy保持lifetime max；
  - [ ] zero-cascade。
- [ ] `schemas.py`：Location values/head/transition/proposal models + LedgerProjection fields。
- [ ] `typed_proposal_families.py`：`_LocationFamilyCodec`、冻结selector/contract、两个mutation owner。
- [ ] `reducers.py`：state/validator/store/proposal dry-run/changed/authorized helper/_EVENTS/semantic/make_projection。
- [ ] `event_catalog.py`：`V2LocationChanged`、`V2LocationChangeCompensated` descriptive contracts。
- [ ] `sqlite_ledger.py::_state_from_projection`：Location四组字段。

### 5.3 最小测试集合

建议新文件：`tests/world_v2/test_location_authority.py`。

- [ ] operator establish → operator change → exact compensation。
- [ ] 同actor第二个rev1、wrong before、wrong from/to、cross-world source拒绝。
- [ ] 用户说“你在学校”、generic deliberative change、图片背景、Plan/Occurrence/Receipt、pending/opaque result作为movement全部fail closed。
- [ ] operator scene private→public change可接受；同一mutation偷降privacy_class拒绝。
- [ ] location/zone change错误preserve since、metadata-only change错误重置since、exact no-op均拒绝。
- [ ] privacy source floor、viewer disclosure不由scene visibility授权。
- [ ] ActorAuthority、Acceptance adjacency/hash/CAS、direct携带draw与任意random_draw均拒绝、chronology攻击。
- [ ] compensation exact target event/hash/revision、effective lane、自由correction refs、旧public值恢复导致privacy降级攻击。
- [ ] zero-cascade + project/rebuild。

Location片验收门：Location不依赖Situation viewer实现也能证明scene/privacy语义拆分；typed family manifest无冲突。

## 6. 工作片 R：ResourceAuthority

### 6.1 交付 Interface

- [ ] resource kind严格闭集：physical_energy/cognitive_capacity/social_capacity。
- [ ] `ResourceStateInitialized/Adjusted/TransitionCompensated` 是typed family。
- [ ] `ResourceClockAdjusted` 是mechanical payload，不进入typed family。
- [ ] head提交value、derived_band、band policy version/digest、privacy_class。
- [ ] pure band policy函数可被reducer和测试共同调用但不暴露为caller写接口；recovery registry首发为空。
- [ ] `.16.0`只安装operator initialize与operator/deliberative adjust/reclassify；settlement adapter与Clock recovery registry为空。

### 6.2 文件与接线

- [ ] 新建 `resource_events.py`：typed/mechanical payload maps分离；exact Clock/recovery input binding；hash。
- [ ] Resource proposal `transition_kind`/event完整映射；payload JSON object/canonical roundtrip validator。
- [ ] 新建 `resource_reducers.py`：
  - [ ] 每 `(actor, kind)` 单head；
  - [ ] state_change要求delta非零且before+delta=after，范围外拒绝，禁止clamp；
  - [ ] band由payload pinned policy复算；
  - [ ] reclassify delta=0、value不变且policy artifact必须有实际变化；
  - [ ] `resource-band-policy.1`按冻结区间复算，digest绑定三个kind/区间/整数算术；
  - [ ] `.16.0`任意Clock recovery以`resource_recovery_authority_not_installed`零变化拒绝；不能从Activity/Occurrence/Receipt推测休息；
  - [ ] 未来typed SettledRecoveryInterval与rate catalog未安装前，不实现/调用overlap recovery公式；
  - [ ] active ActorAuthority init/operator correction；
  - [ ] deliberative adjustment exact解析typed basis/capability，after privacy不弱于basis floor；
  - [ ] Resource operator/self-assessment typed correction basis；禁止自由correction refs与target自证；
  - [ ] compensation exact target event binding/effective lane、privacy lifetime max、chronology、zero-cascade。
- [ ] `schemas.py`：Resource values/head/transition/proposal models + LedgerProjection。
- [ ] `typed_proposal_families.py`：`_ResourceFamilyCodec`；owner只含 initialize/adjust/compensate，不含Clock。
- [ ] `reducers.py`：state/validator/store/proposal dry-run/typed handler/mechanical handler/authority helper/_EVENTS/semantic/make_projection。
- [ ] `event_catalog.py`：typed与`V2ResourceClockAdjusted`分producer/predecessor/evidence。
- [ ] `batch_invariants.py`/family tests：机械event无Acceptance仍合法，带伪Acceptance或误注册失败。
- [ ] `sqlite_ledger.py::_state_from_projection`：Resource四组字段。

### 6.3 最小测试集合

建议新文件：`tests/world_v2/test_resource_authority.py`。

- [ ] operator initialize三种resource；operator/deliberative adjustment；reclassify；compensate。
- [ ] unknown kind、Budget/need混入、delta不守恒、越界/clamp、semantic no-op拒绝。
- [ ] wrong/unknown band policy、caller伪造band、当前policy静默重算攻击拒绝。
- [ ] 任意`V2ResourceClockAdjusted`与settlement adapter adjustment均fail closed且零变化；Activity/Occurrence/Receipt伪装recovery source拒绝。
- [ ] typed Acceptance/hash/CAS/ActorAuthority/selection_mode（random_draw始终fail closed）/typed basis privacy floor/chronology攻击。
- [ ] compensation exact event binding/effective lane、自由correction refs、target自证、privacy降级攻击。
- [ ] zero-cascade + project/rebuild。

Resource片验收门：同一head在policy registry“当前版本”变化的测试下仍消费committed pinned band，不能无event漂移。

## 7. 工作片 A：AttentionAuthority 与 expiry trigger

### 7.1 交付 Interface

- [ ] `V2AttentionChanged` / `V2AttentionTransitionCompensated` 是typed family。
- [ ] expiry不写Attention head；exact Clock只打开 `v2_attention_expiry_due` TriggerProcess。
- [ ] Deliberation消费trigger后可提Attention change或明确no-change并terminal trigger。
- [ ] Situation只有在active trigger exact绑定current Attention head时显示`transition_due=true`。
- [ ] Attention head持久化privacy_class；deliberative after不得弱于typed basis privacy floor。
- [ ] `.16.0` establish只允许operator，change只允许operator/deliberative；settlement adapter与random draw fail closed。

### 7.2 文件与接线

- [ ] 新建 `attention_events.py`：typed payload map、Attention expiry authority binding、hash。
- [ ] Attention proposal `transition_kind`/event完整映射；payload JSON object/canonical roundtrip validator。
- [ ] 新建 `attention_reducers.py`：
  - [ ] establish只允许active ActorAuthority；
  - [ ] typed `AttentionFocusBinding=Plan|WorldOccurrence|Trigger` exact current projection binding；focus_ref由binding派生，禁止裸ref；
  - [ ] mode/focus结构不变量：available/recovering禁focus，glancing/occupied/deep_focus必需，DND可选；allocation/interruptibility不按mode派生；
  - [ ] mode/focus identity变化重置since，其他字段变化preserve；非空expires_at晚于updated_at；
  - [ ] change/compensation exact event binding/effective lane/CAS/chronology；Attention operator/reappraisal typed correction basis且privacy lifetime max；
  - [ ] DND/available不映射回复行为；
  - [ ] zero-cascade。
- [ ] `schemas.py::TriggerProcess`：
  - [ ] 增加`v2_attention_expiry_due` literal；
  - [ ] 增加typed `AttentionExpiryDueBinding` 可选字段：actor、attention revision/fingerprint/expires_at、exact Clock、expiry policy与idempotency key（不要自由JSON）；
  - [ ] validator限定只有该process kind可携带该binding；
  - [ ] appraisal `source_evidence_ref` 规则保持不变；
  - [ ] open/claimed/terminal identity包含authority binding且不可变。
- [ ] `schemas.py`：Attention values/head/transition/proposal models + LedgerProjection。
- [ ] `typed_proposal_families.py`：`_AttentionFamilyCodec`；owner不含expiry trigger。
- [ ] `reducers.py`：
  - [ ] Attention state/validator/store/proposal dry-run/handler/authority/_EVENTS/semantic/make_projection；
  - [ ] `_trigger_process_opened` exact解析Attention head与共享resolver返回的latest Clock projection、expires_at、deterministic per-revision trigger ID；
  - [ ] `_trigger_process_claimed` identity保持；
  - [ ] `_trigger_process_reclaimed` identity保持且lease规则不变；
  - [ ] `_trigger_process_completed` 只允许合法Attention outcome ref/no-change；
  - [ ] 同Attention revision最多一个due trigger；open/claimed/terminal均占用identity，stale trigger保留审计但不生效。
- [ ] `event_catalog.py`：不新增伪AttentionExpired event；更新TriggerProcess schema反射和successor metadata。
- [ ] `sqlite_ledger.py::_state_from_projection`：Attention四组字段；TriggerProcess新字段自然roundtrip。

### 7.3 最小测试集合

建议新文件：`tests/world_v2/test_attention_authority.py`；扩展`test_trigger_process.py`。

- [ ] operator establish → deliberative change → compensation。
- [ ] settlement adapter、任意random_draw、deliberative establish全部fail closed。
- [ ] Clock到期只开trigger，Attention head byte-equivalent；查询不静默available。
- [ ] wrong Clock/hash/revision/Attention before/fingerprint/expires_at/policy/idempotency、未到期trigger拒绝。
- [ ] 同revision在open/claimed/terminal任一状态后由新Clock重开均拒绝；Attention新revision才可获得新trigger identity。
- [ ] claim/reclaim/complete保持exact authority identity；stale trigger不影响new head。
- [ ] typed Plan/Occurrence/Trigger focus正常；裸focus、wrong current status/revision/projection hash/actor/world拒绝。
- [ ] mode/focus structural matrix、since reset/preserve、expires_at chronology攻击；不对allocation/interruptibility做mode映射。
- [ ] expiry后Deliberation选择continue/recover/available均可，只要proposal合法；no-change可terminal trigger。
- [ ] 测试证明available不强制即时回复、DND不强制沉默（至少无reducer/compiler固定映射）。
- [ ] typed Acceptance/hash/CAS/ActorAuthority/selection_mode（random_draw始终fail closed）/typed basis capability与privacy floor/chronology攻击。
- [ ] compensation exact event binding/effective lane、自由correction refs、target自证、privacy降级攻击。
- [ ] zero-cascade + project/rebuild/TriggerProcess roundtrip。

Attention片验收门：TriggerProcess现有observation/settlement/appraisal/recovery测试全绿，不能为了Attention破坏通用trigger生命周期。

## 8. 工作片 S：SituationCompiler

### 8.1 交付 Interface

- [ ] 新建 `SituationCompiler.compile(request) -> SituationCompileResult`，不接Ledger，不读“最新”状态。
- [ ] request包含同world/revision pinned authority snapshot、Logical Time、Situation policies、viewer scope。
- [ ] result分internal Situation与ViewerSituationProjection；低权限caller拿不到internal fields。
- [ ] internal hash与viewer hash分离；cache分层。

### 8.2 文件与接线

- [ ] 新建 `src/companion_daemon/world_v2/situation_compiler.py`：
  - [ ] source selection matrix；
  - [ ] stable sorting/canonicalization；
  - [ ] time segment/due relation/resource pressure/social/plan relation纯catalog；
  - [ ] location scene与disclosure privacy meet；
  - [ ] Attention due trigger exact current-head resolver；
  - [ ] missing/unavailable/redacted区分；
  - [ ] internal/viewer hash；
  - [ ] 可选cache adapter只按hash读取/验证，不成为authority；
  - [ ] 无model/random/network/filesystem/env/wall clock。
- [ ] `schemas.py`：
  - [ ] `SituationAuthoritySnapshot`；
  - [ ] typed availability/reason；
  - [ ] internal slices/`SituationProjection`；
  - [ ] `ViewerSituationProjection`；
  - [ ] `SituationCompileRequest/Result`；
  - [ ] `InternalWorldSnapshot`改为承载source-bound compile result或所需pinned slices；
  - [ ] 旧`SituationStateProjection`标legacy/unavailable，不能继续当producer输入。
- [ ] `matrix_catalog.py`：只增加分类catalog/version/digest，不增加处境→情绪/回复规则。
- [ ] Snapshot/runtime组装处：构造immutable pinned snapshot后调用Compiler；禁止Compiler回调Ledger。
- [ ] Context/Capsule消费处：只消费viewer/internal允许的projection及source refs，不再拼裸location/energy/attention字符串。
- [ ] 不修改`ReducerState.semantic_payload`加入Situation；不注册Situation event/reducer/history。

### 8.3 最小测试集合

建议新文件：`tests/world_v2/test_situation_compiler.py`。

- [ ] 完整source matrix golden：Clock、Location、Plan/Activity、Goal、Resource、Attention、NPC/Occurrence、Commitment。
- [ ] missing source输出unavailable；privacy不足输出redacted；空列表与unknown不混淆。
- [ ] 输入tuple顺序变化输出canonical byte-equivalent。
- [ ] monkeypatch time/random/model/network/filesystem/env，断言零调用。
- [ ] private location/goal/participant/commitment不泄露；scene public不自动披露exact location。
- [ ] Resource使用committed pinned band；未知source policy fail closed，不按当前policy重算。
- [ ] Attention trigger仅在exact current head时due；stale trigger不生效。
- [ ] 同internal input不同viewer/budget：internal hash相同、viewer hash不同且可重算。
- [ ] stale/tampered cache丢弃并纯重算；cache hit/miss结果一致。
- [ ] blocked/depleted/deep-focus等输出不包含sadness/anger/no-reply/固定话术。
- [ ] Compiler不改变任何Ledger/ReducerState。

Situation片验收门：可以只用构造的pinned snapshot测试，不依赖Runtime/QQ；Interface测试能覆盖全部implementation行为。

## 9. 后续工作片 RA：独立 RandomAuthority（不属于 `.16` Exit Gate）

`.16` 不等待本工作片；在本片全部通过并随新 bundle 显式启用之前，四域对 `random_draw` 始终 fail closed，direct 路径始终可用。只增加 `RandomDrawProjection`、schema 或 prompt 不得勾选任何验收门。

### 9.1 深 Module Interface 与 authority material

- [ ] 建立独立 `RandomAuthority.record/supersede/resolve` Interface，domain reducer 不自行抽样。
- [ ] draw identity完整绑定world、actor、exact trigger、decision kind与稳定decision slot；retry复用slot。
- [ ] entropy/nonce来自proposal评估前已提交event，exact绑定event/revision/payload hash/commitment；禁止wall clock、进程RNG与caller临时nonce。
- [ ] candidates为canonical sorted `{candidate_ref, positive weight_bp}`，集合与权重进入hash。
- [ ] frequency budget exact绑定已提交budget head/event/revision/hash、window、limit与consumed_before；超预算fail closed。
- [ ] sampler是有version+digest的纯deterministic weighted sampler，selected candidate可从committed nonce与candidate weights复算。

### 9.2 Event、Reducer、Ledger 与消费

- [ ] 新建typed `RandomDrawRecorded` / `RandomDrawSuperseded` payload与event catalog contracts。
- [ ] pure reducer维护draw current head、immutable history与exact supersession lineage；旧记录不删除。
- [ ] `ReducerState`、semantic payload、LedgerProjection、SQLite roundtrip、migration与rebuild包含draw head/history和budget consumption。
- [ ] supersede exact绑定旧draw event/revision/hash；禁止跨actor/slot、supersede非current或形成分叉。
- [ ] consuming domain mutation原子登记 `(draw_id, consumer_transition_id)` 到持久化 `consumed_random_draw_ids`。
- [ ] 同draw二次消费、消费superseded draw、换candidate、跨actor/slot消费全部拒绝；CAS retry仅可重放同一consumer identity。
- [ ] 新bundle安装显式 capability/version gate；未安装或版本不匹配仍fail closed。

### 9.3 最小验收测试

- [ ] prior committed entropy、trigger/slot identity、candidate canonicalization/weights、deterministic sampler golden/property tests。
- [ ] wrong/stale/future nonce、runtime RNG、candidate/weight篡改、超frequency budget全部拒绝。
- [ ] Recorded→Superseded lineage、close/reopen、project/rebuild、tamper与crash recovery一致。
- [ ] one-draw-one-consumer；retry不重抽、不重复消费；拒绝后更换attempt刷结果。
- [ ] RA未安装时完整authority仍拒绝而Goal direct成功；安装后只有exact active authority可接受。

RA片验收门：以上 Interface、events、reducer、Ledger persistence/replay、budget与consumption测试在同一后续bundle全绿后，才允许把domain `random_draw` policy由reject切为resolve；不回写`.16`完成状态。

## 10. 工作片 I：总 registry、integration 与 Runtime producer

### 10.1 Registry 收口

- [ ] `schemas.py`新增frozen `ClockTransitionProjection`：真实event ref、reducer-computed world revision、canonical payload hash、from/to、event引用且registry exact验证的installed policy version/digest；不从payload复制computed fields，也不使用replay时最新版policy。
- [ ] `ClockAdvanced`现有reducer在保持原event envelope logical_time语义与原Clock状态迁移不变的前提下append immutable history。
- [ ] `reducers.py`新增`clock_transition_history`与consistency validator：revision/event唯一递增、from=应用前time、to=应用后/current time、policy exact installed artifact。
- [ ] 共享纯`resolve_latest_clock(pinned_state)`只选择computed world revision最大项并要求latest.to=current logical time；domain Clock bindings逐项匹配resolver结果。
- [ ] `make_projection`、`LedgerProjection`、SQLite `_state_from_projection`与`.16` semantic payload完整包含Clock history；不得各域复制latest算法。
- [ ] 新建/扩展Clock contract tests：payload伪造revision/hash/policy、cause自报latest、同to不同revision、history缺失/乱序/tamper、live/reopen/rebuild latest一致。
- [ ] `WorldOccurrenceProjection`新增`settled_outcome_ref?`；只有settled current projection可非空，由typed settlement reducer冻结并接入projection/SQLite/rebuild。
- [ ] Goal completion resolver registry首发只有`settled_occurrence_outcome|active_fact_predicate`：前者解析current settled occurrence outcome，后者解析current active Fact subject/predicate/value；其他kind未注册即fail closed。
- [ ] Completion architecture tests证明Activity/Plan/Action/Receipt不能靠ID/ref/status/event type/宽松payload进入union。
- [ ] 新建 `src/companion_daemon/world_v2/deliberative_basis.py`，只暴露纯 `resolve_deliberative_basis(binding, pinned_state)`；domain通过该Interface消费，不复制parser/privacy逻辑。
- [ ] `schemas.py`加入closed `CommittedEvidenceBasis/InternalIntentionBasis/ResolvedDeliberativeBasis`；internal intention内嵌closed intention class + `GoalRationale` NFC bounded text/privacy，绑定actor/trigger/decision slot/revision/time/policy/derived hash，禁止ref/blob。
- [ ] parser registry对settled event、Fact、Experience、Goal/Plan等source kind逐类返回capability与privacy；unknown kind、无privacy、wrong actor/revision/hash fail closed。
- [ ] 新建`tests/world_v2/test_deliberative_basis.py`覆盖determinism、multi-source/rationale privacy meet、trim/NFC/length/Unicode Cc、internal capability、Acceptance不升级证据、cross-world/actor、stale/future与自由EvidenceRef/blob攻击。
- [ ] ActorAuthority policy registry保留policy.1 exact artifact/digest/legacy catalog，新增policy.2 artifact/digest/v2 catalog；resolver按version选catalog并验证allowed-operations subset，不能只检查字符串存在。
- [ ] contract/replay tests覆盖policy.1历史事件byte-equivalent、policy.1伪v2越权拒绝、policy.2 wrong digest/unknown operation/superset拒绝、合法subset接受。
- [ ] `typed_proposal_families.py::INSTALLED_TYPED_PROPOSAL_FAMILIES` 含四个family，排序稳定。
- [ ] `validate_typed_proposal_family_manifest` 证明selector/contract/mutation owner唯一。
- [ ] 每域ProposalProjection以closed Literal验证`transition_kind ↔ event_type`全映射；payload JSON必须canonical object，array/scalar/noncanonical/unknown shape拒绝。
- [ ] `_TYPED_PROPOSAL_STORES` 与family manifest key集合完全相等。
- [ ] `event_catalog.py::_PAYLOAD_MODELS` 与Reducer `_EVENTS` 的coverage测试集合相等。
- [ ] mechanical `V2GoalExpired/V2ResourceClockAdjusted/attention due trigger` 的`family_for_mutation(...) is None`。
- [ ] 所有`V2*` event contract producer/revision/predecessor/evidence/compensation/bundle正确。
- [ ] Legacy无前缀Goal event在v2为UnknownEventType/contract reject。

### 10.2 两阶段 settlement

- [ ] revision N先commit Activity/Occurrence/terminal receipt。
- [ ] ProposalRecorded在revision N之后exact resolve CommittedWorldEventRef；future ID proposal拒绝。
- [ ] 第一个domain proposal pin revision N，并以单独`Acceptance→mutation` pair提交到N+1。
- [ ] 第二个domain proposal必须在N+1重新评估并提交到N+2；禁止复用pin N的旧proposal；其余域依次类推。
- [ ] 每个pair独立原子：当前pair失败只回滚当前pair；已提交settlement和先前domain pair保持有效。
- [ ] 部分更新是合法状态；Situation只展示已提交heads，不自动补齐或回滚其他域。
- [ ] settlement adapter只提交/暴露source；不得自动构造`V2GoalProgressed`。Goal由下一次Deliberation exact引用source后选择positive assessment或no-change。
- [ ] settlement adapter不得自动构造`V2GoalBlocked/Unblocked`；CommittedEvidenceBasis必须经下一次Deliberation解释，或保持no-change。
- [ ] settlement adapter不得直接`V2GoalCompleted`；evidence由Deliberation recognition消费，或由operator携同一strict evidence补结算。
- [ ] `.16.0` Location/Resource/Attention settlement adapter capability均为空；source只能进入Deliberation输入，Location即使经Deliberation也无写lane。

### 10.3 Runtime/advance

- [ ] `world_v2/runtime.py` 或现有advance seam枚举due goals/attention，不在Adapter实现领域规则；Resource recovery registry为空时不产event。
- [ ] `V2GoalExpired` event identity由world/goal/before/latest Clock/policy确定。
- [ ] 任意`V2ResourceClockAdjusted`在`.16.0`均以未安装policy fail closed；不得制造占位delta=0 event。
- [ ] Attention trigger ID由world/process kind/actor/attention revision/expiry policy确定；首次open event另绑定实际latest Clock。
- [ ] Attention同revision的open/claimed/terminal trigger都阻止后续Clock重开；重复执行不产生第二个结果。
- [ ] Clock producer零model/random/network/wall clock。
- [ ] Clock producer/domain commands都只消费committed Clock projection；不得从cause或event envelope的logical_time字段重建另一套from/to语义。
- [ ] WorldRuntime只暴露domain commands/compile，不暴露set_situation/set_resource_head等旁路。

### 10.4 最小integration测试

建议新文件：`tests/world_v2/test_goal_situation_integration.py`。

- [ ] ActivityCompleted先commit；Deliberation对Resource选择no-change或subjective pair，再对Goal选择no-change（零Goal event）或带class/rationale的positive progress pair，再处理Attention；Situation每步只看已提交head。
- [ ] 同一种settled source不由reducer推导固定delta；fixture中的不同角色/处境可产生不同合法assessment，Complete仍须strict contract。
- [ ] Resource pair成功后Goal pair因stale CAS失败：Resource与Activity保留，Goal/Attention不变；随后可在最新revision重新提Goal。
- [ ] 旧revision的第二/第三域proposal被明确stale，不能因同一settlement而豁免CAS。
- [ ] ClockAdvanced产生Goal mechanical结果和Attention due trigger；Resource recovery fail closed且零event；Attention同revision不重复。
- [ ] private internal Situation到viewer projection正确redact。
- [ ] viewer Goal默认只见允许的status/class，不见GoalRationale/InternalIntention原文；显式viewer/privacy grant才可披露且hash/scope变化。
- [ ] project/rebuild/close-reopen后compile byte-equivalent。
- [ ] 旧context assembler/life writer旁路有architecture/import test证明不再写`.16` authority。

## 11. 工作片 M：`.15→.16` semantic 与 SQLite migration

### 11.1 Bundle/semantic接线

- [ ] `reducers.py::REDUCER_BUNDLE_VERSION = world-v2-reducers.16`。
- [ ] `schemas.py::LedgerProjection.reducer_bundle_version` 默认同步`.16`。
- [ ] `event_catalog.py::EventContract.reducer_bundle` 默认/新contract同步`.16`；不得残留`.14/.15`作为新event metadata。
- [ ] `ReducerState.semantic_payload`：
  - [ ] `.15` branch维持CharacterCore及旧字段的exact历史语义并明确排除Clock history与Occurrence `settled_outcome_ref`；
  - [ ] `.16` branch新增四域heads/history、`clock_transition_history`与Occurrence `settled_outcome_ref`；
  - [ ] 不加入Situation、cache、pending compile结果；
  - [ ] proposal/proposal_ids是否入semantic保持既有全局纪律，不为`.16`单独改变。
- [ ] `make_projection`/`LedgerProjection`/`_state_from_projection` 四域字段、Clock history与Occurrence `settled_outcome_ref`三方集合一致。

### 11.2 SQLite open migration

- [ ] `sqlite_ledger.py` supported installed bundle set加入`.15`并以`.16`为current。
- [ ] `_legacy_semantic_hash`保留`.15` exact branch；先验证legacy semantic hash。
- [ ] `.15` persisted state hash/cursor合法性按现有能力验证。
- [ ] policy artifact registry同时保留policy.1 exact digest/legacy catalog并安装policy.2 digest/v2 catalog；迁移/replay不得用policy.2重解释历史policy.1 authority。
- [ ] 用`.16`目标reducer replay immutable events；从旧`ClockAdvanced` events重建Clock history、从typed occurrence settlement events重建`settled_outcome_ref`，不声称调用不存在的`.15` runner，不改旧event envelope logical_time语义。
- [ ] `.16`四域新字段在旧event log上为空；Clock history与Occurrence outcome按旧typed events可复算；legacy Goal/location/needs/attention不自动升级。
- [ ] state_json、semantic_hash、bundle、state_hash单SQLite transaction CAS更新。
- [ ] `_state_from_projection`包含四域proposal索引，避免reopen后pending proposal丢失。
- [ ] migration中断后只能看到完整`.15`或完整`.16`。

### 11.3 最小migration测试

建议放在`tests/world_v2/test_goal_situation_integration.py`或独立`test_goal_situation_migration.py`。

- [ ] 非空`.15`库（含Fact/Experience/Memory/Core）手工降级head后打开`.16`。
- [ ] 迁移后旧authority byte-equivalent，新四域为空，Situation显示unavailable。
- [ ] `.15` legacy semantic hash不含Clock history；迁入`.16`后history的ref/computed revision/payload hash/from/to/installed policy与event log逐项一致。
- [ ] `.15` legacy semantic hash不含`settled_outcome_ref`；`.16` replay值与typed occurrence settlement event逐项一致，非settled occurrence保持空。
- [ ] close/reopen/project/rebuild相等，cursor/world/deliberation/ledger sequence不变。
- [ ] tampered `.15` semantic hash拒绝。
- [ ] tampered `.16` state_json/state hash拒绝。
- [ ] interrupted transaction可安全retry。
- [ ] 从至少一个更旧受支持bundle连续迁到`.16`。
- [ ] 已有`.16`四域、Clock history与settled Occurrence outcome非空SQLite reopen/rebuild不丢字段，latest resolver/parser结果一致。

Migration片验收门：迁移测试不能只用空世界；必须包含`.15`真实非空authority和`.16`非空roundtrip两类。

## 12. 全局最小回归命令

各片开发期间先跑本片，merge owner收口时至少执行：

```bash
uv run pytest -q \
  tests/world_v2/test_goal_authority.py \
  tests/world_v2/test_location_authority.py \
  tests/world_v2/test_resource_authority.py \
  tests/world_v2/test_attention_authority.py \
  tests/world_v2/test_situation_compiler.py \
  tests/world_v2/test_goal_situation_integration.py \
  tests/world_v2/test_trigger_process.py \
  tests/world_v2/test_typed_proposal_families.py \
  tests/world_v2/test_event_catalog.py \
  tests/world_v2/test_schema_contracts.py \
  tests/world_v2/test_ledger_contract.py

uv run ruff check src/companion_daemon/world_v2 tests/world_v2
git diff --check
```

最终验收必须跑仓库全量测试和既有类型/import/architecture检查；以上命令不是全量替代。

## 13. 每片交付回报模板

```text
工作片：G/L/R/A/S/I/M（RA为后续独立bundle，不计`.16`）
Interface：已实现的唯一caller/test seam
事件：typed / mechanical 分组
Authority：typed deliberative basis/privacy floor、ActorAuthority、ClockTransitionProjection/latest resolver、selection_mode（`.16` random_draw fail closed）、CompletionContract cutoff、compensation effective lane
Registry：family / store / catalog / reducer _EVENTS / batch invariant
Projection：ReducerState / semantic payload / LedgerProjection / SQLite roundtrip
Tests：通过数、关键攻击项、zero-cascade、rebuild
未完成：明确缺口；不得用TODO冒充通过
共享文件：实际修改列表与merge owner
```

## 14. 最终 `.16` Exit Gate

- [ ] G/L/R/A/S/I/M 七片全部达到各自验收门。
- [ ] 四个domain proposal family、store、codec、selector、mutation owner一一对应。
- [ ] mechanical event/trigger没有Acceptance依赖，且authority/hash/idempotency/recovery闭环。
- [ ] ClockTransitionProjection/history由ClockAdvanced reducer计算并进入ReducerState/Ledger/semantic/SQLite/rebuild；domain只信latest resolver，cause自报无authority。
- [ ] `.15` semantic排除Clock history；`.16` replay从旧Clock events重建且不改变旧event envelope logical_time语义。
- [ ] ActorAuthority required operation生效；OperatorObservation不能升权。
- [ ] ActorAuthority policy.1 legacy replay不变；policy.2 digest/catalog/subset验证生效，`.16`拒绝policy.1借新schema越权。
- [ ] settlement两阶段、每域pair最新revision串行、合法部分更新和future evidence拒绝有integration证据。
- [ ] `.16`任意random_draw fail closed、薄projection不算支持，direct与spontaneous Goal不被RandomAuthority缺失阻塞；RA工作片不计本Exit Gate。
- [ ] deliberative typed basis resolver、capability与最严格privacy floor覆盖四域；Resource/Attention privacy可持久化/rebuild。
- [ ] Goal progress只由Deliberation基于exact settled/Fact/Experience作positive主观评估，Runtime不自动涨、no-change零event、Complete维持strict contract。
- [ ] Complete在cause外有独立typed evidence union；deliberative recognition/operator补结算都strict，operator只reauth，Goal无settlement写lane；Acceptance后的rationale/internal intention不能充当客观证据。
- [ ] CompletionContract closed registry的kind/schema/digest/privacy meet生效；blocked Complete显式清空全部blocker fingerprints。
- [ ] Goal supersedes同actor/非self/既存terminal exact head/event binding并进入derived EvidenceRefs；Goal privacy meet target/rationale/basis；updated_at单调性有攻击测试。
- [ ] Progress/Block/Unblock/Pause/Resume/Abandon deliberative-only；typed blocker fingerprint/Resolution removed binding、blocked Complete清空、typed lifecycle reasons、operator exact compensation纠错有攻击测试。
- [ ] 每域zero-cascade；Situation只读、无event/reducer/cache authority。
- [ ] internal/viewer hash、privacy redaction、pinned Resource band、Attention due source都可rebuild。
- [ ] `.15→.16`非空migration和`.16`非空SQLite roundtrip通过。
- [ ] legacy事件/写旁路隔离测试通过。
- [ ] 全量测试、ruff/type/import/architecture检查通过，P0/P1 review为0。
- [ ] 更新主计划真实状态、bundle提交号、测试数和exit report后，才可宣称`.16`完成。

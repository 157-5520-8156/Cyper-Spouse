# Girl-Agent 高拟真人类情绪：剩余缺口、目标架构与实施计划

状态：主链路完成，持续验收中（2026-07-13 审计发现的 P0 修复进行中）

创建日期：2026-07-12

适用范围：World 模式、用户消息评价、世界/NPC/目标事件、Affect、长期关系残留、Stance、Display Strategy、回复生成与校验、fallback、热冷会话、模型用量与离线评测

明确排除：依赖大规模真实用户连续数周数据得出的统计校准结论

## 0. 2026-07-13 复审补充（不得以“完成”掩盖）

本计划的主链路已经落地，但以下问题仍会直接影响人感，必须保留为
验收门槛：

1. `TurnFrame` 必须从 `emotion_modulation.vector`（而非账本元数据）读取主导
   affect；真实 `hurt/anger` 投影不可退化成 `unresolved` 标签。
2. 用户失望/困惑必须带 Logical Time 衰减、显式修复和话题相关性；孤立的旧不满
   不能在数周后的无关冷启动中永久劫持回复。
3. 热对话的隐性失望不能只依赖后台第二模型调用；应由主回复的受证据约束 proposal
   或等价的无额外串行延迟路径覆盖。
4. Emotion Program 的 appraisal 维度要可回放地影响 episode component，而不是只改变
   展示标签；生活/NPC 事件还需考虑重复性、恢复性经历和当前未解决触发。
5. 旧的全局 `last_affect_display` 与非 World 情绪兼容路径必须删除决策权或显式隔离。

这些属于工程内可修复项；真实长期用户校准仍是本文明确排除的外部验证条件。

## 1. 文档目的

本文不是愿望清单，而是当前高拟真情绪系统的修复规范。每一项问题都必须具有：

1. 可定位的现状证据；
2. 明确的领域不变量；
3. 可通过公共 interface 验证的验收标准；
4. 不依赖真实用户数据的离线验证方法；
5. 明确的迁移和删除条件，避免新旧实现长期并存。

本文完成后的目标不是宣称“已与真人不可区分”。在没有真实长期使用数据时，这种结论不可证明。本文的目标是消除已知的工程矛盾、因果漏洞、可刷取状态、架构漂移、端到端延迟和评测盲区，使系统达到可被长期校准的可靠基础。

## 2. 当前已经具备的基础

以下能力已经存在，本计划在其上演进，不重复建设：

- World Event 是唯一可接受的世界事实历史；
- Projection 可由 World Event 确定性重建；
- 模型输出是 Proposal，不能直接成为 Character Fact、Committed Experience 或 Affect；
- 用户、NPC、Goal 和 World 事件可成为 Affect 来源；
- Affect episode 包含 source、target、logical time、intensity、valence 和 half-life；
- 正负 episode 可以并存；
- 已有 PAD/core affect、关系 residue、修复观察期和 Display Strategy；
- 已有 DeepSeek V4 Flash、真实 token/latency usage 落库、provider outage fallback；
- 已有 28 天重放、冒犯/修复、NPC spillover、混合情绪和模型失败测试。

## 3. 不可破坏的领域不变量

### 3.1 事实与因果

1. 模型只能产生 Proposal；只有 WorldKernel 接受的 World Event 才能改变 Projection。
2. 每个非 baseline Affect 必须能回溯到至少一个已接受的 World Event。
3. Affect 不能授权 Character Fact、User Fact、Committed Experience 或现实 Action。
4. 用户来源的强负面 Appraisal 必须有当前消息证据或已确认的行为链。
5. NPC、Goal 或 World 来源的负面 Affect 不得被表达为用户造成，除非存在独立的用户来源 episode。
6. 低置信度歧义不能造成强关系伤害。

### 3.2 时间与重放

1. 所有情绪动力学只使用 Logical Time，不读取 wall clock。
2. 同一事件历史、profile version 和 rule version 必须产生同一 Projection hash。
3. 重放期间不得调用模型、网络、平台或重新抽样。
4. 规则/profile 升级不能静默重解释旧 World；迁移必须由显式 World Event 表达。

### 3.3 情绪状态

1. 活跃 episode 是短中期 Affect 的唯一权威动态状态。
2. vector、PAD、primary/secondary、mixed、behavior tendency 和 unresolved 都必须从 episode、baseline 和长期 residue 派生，不得独立演化。
3. Display Strategy 是针对特定 Action、特定收件人和特定 Projection revision 的计划，不是永久情绪事实。
4. 抑制只能降低可见表达或 accessibility，不能伪装成内在 episode 已消失。
5. 修复只能降低与该修复证据存在因果关联的 episode。

### 3.4 表达与 fallback

1. 同一个 Expression Policy Spec 必须同时生成 prompt、deterministic validation 和 safe fallback。
2. fallback 不得新增事实、Action、人物、地点、经历、情绪来源或修复结论。
3. provider、解析或审计失败不能清空已有 Affect。
4. Hard Invariant 失败时允许取消表达，但不能回滚已经有来源的 Affect。

## 4. 问题清单与验收标准

### EF-01：vector 与 episode 两套动力学会分叉（P1）

现状：

- vector 使用固定线性每小时衰减；
- episode 使用独立 half-life 指数衰减；
- unresolved 和行为倾向主要读取 vector；
- Display Strategy 主要读取 episode。

风险：同一 revision 下，决策层可能认为已经平静，表达层仍被旧 episode 主导，或反之。

修复：

- episode 成为唯一动态权威；
- 删除 vector 的独立增量和独立衰减；
- vector/PAD/unresolved 每次从当前 episode、baseline、process 和 residue 聚合；
- 对旧 ledger 提供明确 legacy projector，不继续双写新旧状态。

验收：

- 不存在“vector 非 baseline 但没有来源 episode”；
- 不存在“active negative episode 满足 unresolved 门槛但 unresolved=false”；
- 一次推进与分段推进得到同一 Projection；
- 1、7、30、180 天轨迹均满足派生一致性。

### EF-02：负面世界事件使用了错误的 half-life（P1）

现状：只有用户 harmful Appraisal 会选择 negative half-life；`npc_conflict`、`goal_strain` 等负 valence episode 可能使用 positive half-life。

修复：

- half-life 首先由 emotion kind/valence 决定，而不是由“是否用户冒犯”决定；
- anger、anxiety、sadness、hurt、resentment 可分别配置；
- relationship sensitization 只增强有相似因果 target/kind 的 episode；
- restorative context 可以调节衰减，但必须有来源。

验收：

- NPC conflict 和 goal strain 均使用负向 profile；
- anger 通常比 resentment 更快恢复；
- 同一事件在 restorative solitude 后比 rumination 中恢复更快；
- profile version 固定后重放稳定。

### EF-03：修复证据可被重复话术刷取（P1）

现状：观察期内重复两次 `boundary_respected` 文本即可增加两次计数，没有绑定原 violation、承诺、履约机会、行为对象或时间间隔。

修复：建立修复因果图：

```text
ViolationCommitted
  → RepairCommitmentCommitted(violation_id, promised_boundary)
  → RepairOpportunityObserved(opportunity_id, violation_id)
  → RepairFollowthroughCommitted(opportunity_id, evidence_event_id)
  → linked episode reduction/resolution
```

规则：

- 重复道歉或重复承诺不等于履约；
- 同一 opportunity 只能结算一次；
- evidence 必须晚于 commitment；
- evidence 必须与原 boundary kind 相符；
- 不相关的礼貌、温暖或普通聊天不能修复该 violation；
- 新的同类 violation 会使对应 repair case 复发或失败。

验收：

- 连续重复 100 次“你说停我就停”不能完成修复；
- 没有履约机会时不能产生 followthrough；
- 不同 violation 的 evidence 不可串用；
- 已结算 evidence 幂等；
- 具体且真实的后续尊重能够渐进修复。

### EF-04：长期 affinity 无遗忘、时间权重和频率归一化（P1）

现状：evidence count 永久累加，每三次固定漂移；旧伤与近期伤害、高频用户与低频用户的权重差异不足。

修复：

- 使用 Logical Time 指数衰减或有界近期窗口；
- 对聊天频率归一化；
- 区分 habituation、sensitization 和 stable residue；
- 引入迟滞，防止一次温暖立即逆转长期伤害；
- 长期稳定修复必须允许旧伤逐步降低；
- 相似 trigger 可重新激活 residue，但不能凭空产生新事实。

验收：

- 相同事件序列在高频和低频聊天下按单位时间产生相近 residue；
- 一年前的孤立伤害权重低于昨天的伤害；
- 长期一致修复后 resentment 可恢复；
- 关系轨迹有界且不因消息数量无限漂移。

### EF-05：episode 固定截断和主导选择会丢失关键因果（P1）

现状：只保留最后 16 个 episode；Display Strategy 主要按 intensity 排序，较少考虑当前 target、话题相关性、recency 和 unresolved 状态。

修复：

- 不再按 `[-16:]` 删除权威 episode；
- resolved episode 归档，active/latent episode 保留；
- Projection 建立 status、target、kind、causal parent 和 recency 索引；
- prompt 只检索相关 top-K，但完整状态不截断；
- 主导选择按当前 intent、target、相关性、intensity、accessibility 和 recency 综合计算。

验收：

- 100 个轻微事件不能挤掉未解决的重大 violation；
- 当前用户 episode 优先于无关旧 NPC episode；
- 查询 prompt 的 top-K 不改变权威状态；
- 归档后仍能从 World Event 恢复因果历史。

### EF-06：Display Strategy 会过期或跨用户复用（P1）

现状：`last_affect_display` 只在部分用户回合刷新，但 NPC、Goal、线程到期和 Logical Time 都可能改变 Affect；全局 last display 也不适合多用户关系。

修复：

- Expression Plan 每次准备 reply/proactive/afterthought 时从当前 revision 即时编译；
- plan 绑定 `world_revision`、`user_id`、`intent_id`、`purpose` 和 `plan_hash`；
- 旧 display 只作为某次 Action 的审计记录，不再作为下一轮决策输入；
- revision 变化后旧 plan fail closed，要求重新编译。

验收：

- NPC event、decay、repair 或 relationship change 后不会读取旧 plan；
- 同一 Affect 面向不同用户可有不同 Display Strategy；
- 旧 revision plan 不能验证新回复；
- rebuild 后相同输入产生相同 plan hash。

### EF-07：生活事件仍是静态标签，不是真正的 Appraisal（P2）

现状：部分 life template 直接固定 appraisal/intensity，没有系统输入 NPC 关系、目标重要性、可控性、重复性、疲劳和已有 trigger。

修复：所有已结算刺激统一为 `AffectStimulus + CommittedAppraisal`：

- origin/actor/target；
- relevance、goal congruence、agency、controllability；
- certainty、norm compatibility、power delta；
- relationship threat、self evaluation、responsibility scope；
- salience、recurrence、physiology/needs context。

同一 World Event 在不同已提交上下文中可得到不同 Appraisal，但模型不能自由发明上下文。

验收：

- 同一 NPC 批评在亲密/陌生、疲劳/充足、可控/不可控条件下产生不同但有界结果；
- Goal failure 在高重要性时比低重要性时影响更大；
- 没有来源的疲劳、关系或 rival 不参与评价；
- 用户、NPC、Goal、World 四种来源走同一 transition interface。

### EF-08：语义 Appraisal 召回依赖少量 marker（P2）

现状：未命中本地 marker 的反讽、引用、假玩笑、PUA、方言和跨轮权力施压不会进入 contextual appraisal。

修复：

- 本地明确规则继续负责高置信 harm 和明确 benign；
- 使用高召回风险筛选覆盖语用不一致、关系施压、引用/转述和目标歧义；
- 普通歧义使用 V4 Flash 非思考 proposal；
- 高风险且证据冲突时才使用 thinking；
- Proposal 必须通过 evidence、agency、target、confidence 和 alternative 校验；
- uncertainty 是合法 Committed Appraisal，不能暗中伤关系。

验收：

- 建立许可可用或合成的中文语用对抗集；
- 分别报告 harm/target/severity 的 precision、recall、F1 和 calibration error；
- 覆盖反讽、自嘲、引用、第三方、玩笑、PUA、性胁迫、方言和连续施压；
- 低置信度样本不得产生永久关系伤害；
- 跨用户上下文严格隔离。

### EF-09：Appraisal dimensions 尚未真正驱动情绪组合与 coping（P2）

现状：部分 dimensions 只被存储或轻微调整 intensity，不能系统决定 anger/sadness/anxiety/shame/guilt、反刍、对抗或回避。

修复：引入版本化 Emotion Programs 和通用 episode/process 代数：

- anger：阻碍/不公 + 可归责 + 较高控制感；
- sadness：损失 + 低控制；
- anxiety：不确定威胁 + 低控制；
- shame：global-self negative evaluation；
- guilt：specific-action responsibility + repair controllability；
- jealousy：有来源的关系价值 + rival/threat + 不确定性；
- ambivalent attachment：正负 episode 的 approach/avoidance 组合，不是单独标签；
- suppression：降低 expression pressure，不降低 episode intensity；
- rumination：有限强化有来源 episode，有上限和停止条件；
- reappraisal/habituation/sensitization：作为有界 Affect Process。

验收：

- 只改变 controllability 的 metamorphic test 会合理改变 anger/anxiety/coping；
- 只把 responsibility 从 global-self 改为 specific-action，会从 shame 倾向转为 guilt/repair；
- 没有 rival 事实不能产生 jealousy；
- suppression release 后允许有界反弹；
- rumination 不可无限自激。

### EF-10：情绪逻辑分散，模块 interface 偏浅（P1）

现状：Engine/WorldKernel 调用者必须知道本地分类、模型 proposal、上下文隔离、事件记录、affect transition、display、validator、fallback 和重试顺序。

修复：建立三个深模块 seam：

```text
InteractionAppraiser
        ↓ CommittedAppraisal
HumanAffect / AffectTransitionPolicy
        ↓ AffectTransitionProposal
AffectExpressionPolicy
        ↓ ExpressionPlan
```

WorldKernel 仍是唯一事件提交者，Engine 只负责 transport、外部模型调用编排和 Action 投递。

验收：

- Engine 不再手工拼装 appraisal schema 或 affect/display dict；
- 用户、NPC、Goal、Logical Time 共享一个 transition implementation；
- 新增一种 emotion program 不需要修改 Engine；
- 删除深模块后复杂度会重新散落到多个调用方，满足 deletion test；
- 新测试通过公共 interface，而不是内部 helper。

### EF-11：prompt、validator 和 fallback 规则可能漂移（P1）

现状：表达提示、情绪合法性、归因门禁和 fallback 分属多个模块，曾出现 prompt 禁止迁怒但 validator 未覆盖的问题。

修复：

```text
ExpressionPolicySpec
 ├─ compile_prompt()  → prompt fragment
 ├─ validate()        → ExpressionViolation[]
 └─ fallback()        → grounded ReplyCandidate
```

`ExpressionPlan.resolve()` 使用同一 spec 接受候选或生成受约束 fallback。

验收：

- 每条 hard expression constraint 都有 validator；
- 每种 violation 都有符合相同规则的 fallback；
- NPC spillover、unresolved hurt、mixed affect、repair observing 均有 contract tests；
- fallback 不能新增 claim/event/action；
- 固定 seed 与 plan hash 完全可重放。

### EF-12：离线 dialogue eval 主入口不可运行（P0，开发验证阻断）

现状：`companion-eval-dialogue` 先构建 world-only engine，随后调用 legacy `seed_user` 写 MoodState，触发 fail-closed RuntimeError；主 CLI 路径缺少 smoke test。

修复：

- eval 使用 World register_user，不再写 legacy 行为状态；
- 不访问 coalescer 私有 `_tasks`；
- 为 CLI 主路径和 `--context` 建立 smoke tests；
- scenario runner 返回结构化结果和非零失败退出码策略。

验收：

- `uv run companion-eval-dialogue` 可在 fake model 下完成；
- `--context` 可完成；
- 两个入口均有 CI smoke test；
- eval 不污染生产数据库或读取真实 API key。

### EF-13：纵向和对抗评测覆盖不足（P1）

现状：28 天评测是固定短脚本，主要检查峰值、误报和最终恢复，不能覆盖大量状态组合。

修复：建立 deterministic scenario matrix 和 property-based sequence harness：

- 正常、反讽、引用、自嘲、重复冒犯；
- 道歉、承诺、履约机会、履约、复发；
- NPC conflict、social warmth、Goal strain/completion；
- mixed affect、suppression、rumination；
- 不同聊天频率、1/7/30/180 天；
- hot/warm/cold；
- provider 连续失败 1/5/20 轮；
- 多用户隔离和跨 target 归因。

硬错误直接 fail；风格问题只作为诊断分数，不能与事实错误等权。

验收指标：

- unsourced affect rate；
- harm false positive/false negative；
- target/agency attribution error；
- duplicate repair acceptance；
- recovery time、hysteresis、bounded escalation；
- replay equality；
- expression contract violations；
- fallback repetition；
- calls/turn、tokens/turn、p50/p95 latency。

### EF-14：hot cadence 没有真正优化端到端延迟（P0，用户体验）

现状：hot/warm/cold 主要影响 debounce 和 attention。普通 world turn 仍串行 reply + audit；歧义回合还可能先做 contextual appraisal。HTTP client 每次新建，provider 首次失败可等待完整 timeout；typing 出现偏晚。

修复：

- adapter 在 observed message/seen 后、模型调用前立即发布 typing；
- DeepSeek client 连接复用；
- 每轮显式 Call Budget；
- provider failure circuit breaker；
- hot path 复用已验证的稳定上下文和 Projection 编译结果；
- 无事实断言、无 Action、无新 Affect 声明的候选允许本地 contract 后跳过独立事实 audit；
- contextual appraisal 默认非思考，只有高风险证据冲突才 thinking；
- hot/warm/cold 使用不同 soft timeout，但 Hard Invariant 不降级。

验收：

- 记录从 observed 到 seen、typing、first model response、candidate accepted、delivery settled 的时间；
- hot 普通回合模型调用预算通常不超过 1；
- provider outage 不等待多个串行 45 秒；
- circuit open 时直接使用本地安全路径；
- latency 优化不能绕过事实、归因、隐私或 consent 门禁。

### EF-15：token/latency 记录尚未形成闭环（P1）

现状：usage 已落库，但缺少 turn/world/action/cadence 关联、实际 CNY、p50/p95、失败率和可操作报表；BudgetGate 也没有使用 reply/audit/appraisal 的真实 token。

修复：

- usage event 增加 world_id、turn_id、action_id、cadence、attempt；
- 根据 model/version/cache/thinking 计算实际成本；
- 提供 CLI 或只读接口汇总日/月、purpose、cadence、成功率、p50/p95；
- Call Budget 根据本轮风险和剩余预算决定 audit/thinking，而不是固定单位；
- 预算降级不能关闭 Hard Invariant。

验收：

- 能回答某一 turn 使用了几次调用、多少 token、多少缓存命中、多少钱、耗时多久；
- 能按 purpose/cadence/model 聚合；
- usage observer 故障不影响回复；
- malformed response 计 failed；
- 预算路径具有确定性测试。

### EF-16：fallback 与连续故障轨迹仍需硬化（P1）

现状：首次 provider 失败前仍可能等待完整 timeout；部分 repair fallback 变体不足；缺少连续多轮 outage 的长期轨迹与进入率指标。

修复：

- 区分 transport/timeout、provider 5xx、schema error 和程序错误；
- 只有可识别 provider outage 使用本地 fallback；程序错误 fail closed；
- circuit breaker 避免同一 provider 在同轮或短窗口重复超时；
- fallback 变体由安全 skeleton、Expression Plan 和 deterministic seed 组合；
- 连续失败不得新增或清空 Affect；
- 记录 fallback reason 和 plan hash。

验收：

- stranger、close、negative affect、NPC spillover、boundary 五类状态各运行连续 20 轮 outage；
- 无事实幻觉、无错误归因、无自动原谅、无重复 Action；
- fallback 文本重复率有界；
- provider 恢复后 circuit 可半开探测并恢复。

### EF-17：cadence 判定存在临界点和时钟一致性问题（P1）

现状：coalescer 与 Engine 可能分别读取当前时间，在 90/600 秒临界点得出不同 heat；阈值硬编码、无 hysteresis，也缺真实 World+QQ 连续回合集成测试。

修复：

- platform adapter 冻结本轮 `observed_at` 和 cadence；
- cadence 随 Turn Context 进入 Engine、usage 和 Action trace；
- 加入 hysteresis，避免临界点反复跳变；
- 检查 future timestamp、clock drift 和 out-of-order delivery；
- hot/warm/cold 阈值进入版本化 rhythm profile。

验收：

- 同一 turn 全链路只有一个 cadence；
- 临界点不会因几毫秒漂移改变策略；
- future/out-of-order 输入保守处理；
- QQ adapter + real WorldKernel 两轮集成测试覆盖 typing、calls 和 delivery。

### EF-18：文本之外的互动线索尚未进入统一 Appraisal（P2）

现状：标点、emoji、贴纸、撤回/编辑、连续短消息、回复间隔和附件通常由各自模块处理，尚未形成统一且有来源的 Interaction Evidence。

修复：

- 建立 bounded `InteractionEvidence`：text spans、emoji/sticker、attachment kind、timing、turn burst、reply target；
- 非文本线索只能调节 certainty/salience/acts，不能单独证明强恶意；
- 合并消息后仍保留每条 observed evidence reference；
- 平台不提供的线索明确 unknown，不推断。

验收：

- 同一句话在明确玩笑 emoji 与连续施压上下文中可得到不同但有界 certainty；
- 单个 emoji 不会产生强关系伤害；
- evidence 可回溯原 platform event；
- 多平台缺失字段不导致隐式默认恶意。

## 5. Design It Twice 比较与最终选择

### 5.1 方案 A：最小 facade

外部只有 `decide / project / view` 三个入口。优点是 interface 最小、depth 高；缺点是一次性把 Appraisal、Affect、Stance、Display Strategy 都收入同一 module，迁移面较大，也容易让 module 承担过多世界职责。

### 5.2 方案 B：版本化 Emotion Programs + episode/process 代数

外部提供 transition、advance、expression 三个入口，内部用通用 ops 表达 Open/Reinforce/Resolve/Archive episode 以及 suppression/rumination/reappraisal 等 process。优点是后续新增复杂情绪通常只改 profile/program；缺点是 DSL、编译验证和版本迁移本身复杂。

### 5.3 方案 C：调用者优先的 WorldKernel facade

WorldKernel 暴露 `accept_turn / advance / expression_plan`，Engine 不再手工记忆 affect 流水线。优点是当前迁移风险低、默认路径简单；缺点是 WorldKernel interface 增加领域化入口，离线假设仿真需要内部 harness。

### 5.4 最终选择：C 的 facade + B 的 implementation

采用：

1. Engine 通过 typed `InteractionAppraiser` 得到 `CommittedAppraisal`；
2. WorldKernel 的 typed facade 原子接受 turn/world advance；
3. 内部 `HumanAffect` 使用版本化 Emotion Programs、episode/process algebra 和 repair evidence graph；
4. `ExpressionPlan` 从当前 revision 即时编译，并统一 prompt/validator/fallback；
5. 不开放逐 emotion plugin interface；全新 operator 仍由 HumanAffect implementation 维护，以保护 replay 和 invariant。

## 6. 目标 interface

### 6.1 InteractionAppraiser

```python
class InteractionAppraiser:
    async def assess(self, input: TurnAppraisalInput) -> AppraisalDecision: ...
```

隐藏本地规则、风险筛选、模型 adapter、上下文隔离、proposal validation、confidence policy 和 fallback。输出同时保留 proposed 与 accepted provenance。

### 6.2 HumanAffect（WorldKernel 内部 seam）

```python
class HumanAffect:
    def propose_transition(
        self,
        view: AffectView,
        stimulus: AffectStimulus,
        appraisal: CommittedAppraisal,
        context: AffectContext,
    ) -> AffectTransitionProposal: ...

    def propose_advance(
        self,
        view: AffectView,
        to_logical_at: str,
        context: AffectContext,
    ) -> AffectTransitionProposal: ...

    def propose_expression(
        self,
        view: AffectView,
        context: ExpressionContext,
    ) -> ExpressionPlan: ...
```

HumanAffect 不读取 DB、HTTP、wall clock 或未记录随机数，只返回纯计算 Proposal。

### 6.3 WorldKernel typed facade

```python
class WorldKernel:
    def accept_turn(self, turn: AcceptedTurn) -> TurnDecision: ...
    def advance(self, world_id, target_logical_time, *, expected_revision) -> WorldDecision: ...
    def expression_plan(self, world_id, *, user_id, purpose, expected_revision=None) -> ExpressionPlan: ...
```

旧 command bus 可在迁移期翻译到同一实现，但不能保留第二套 transition。

### 6.4 ExpressionPlan

```python
class ExpressionPlan:
    revision: int
    user_id: str
    intent_id: str
    plan_hash: str
    prompt_fragment: str
    policy_spec: ExpressionPolicySpec

    def resolve(
        self,
        proposed: ReplyCandidate,
        *,
        safe_seed: ReplySkeleton,
        turn: ReplyTurnContext,
    ) -> ExpressionResolution: ...
```

事实、Action、隐私、法律和 consent 等通用 Hard Invariant 仍由现有世界回复验证负责；ExpressionPlan 只统一 Affect/Display 相关规则，避免成为无边界总验证器。

## 7. 事件与 Projection 迁移

### 7.1 新权威事件

建议事件：

- `AppraisalCommitted`；
- `AffectTransitionCommitted`，包含 canonical episode/process/repair ops；
- `RepairCommitmentCommitted`；
- `RepairOpportunityObserved`；
- `RepairFollowthroughCommitted`；
- `DisplayStrategySelected`，绑定 intent/revision/plan hash；
- `AffectProfileRegistered` / `AffectProfileMigrationCommitted`。

### 7.2 Proposal 与事实顺序

```text
Source World Event
→ ModelProposalRecorded（可选）
→ AppraisalCommitted
→ AffectTransitionCommitted
→ StanceSelected
→ DisplayStrategySelected（准备表达时）
→ ReplyProposalRecorded
→ validation / fallback
→ Action terminal event
```

### 7.3 迁移规则

1. 先实现 typed view 和 episode-only shadow projection；
2. shadow 只比较，不写第二份 World truth；
3. 旧 vector-only ledger 通过 private legacy projector 提升为兼容 episode；
4. 切换后停止独立 vector 衰减；
5. Engine 全部表达路径迁到 ExpressionPlan；
6. 删除全局 `last_affect_display` 的决策用途；
7. 删除旧 `_EFFECTS/_DECAY_PER_HOUR` 和散落 validator 前，必须已有 interface contract 覆盖。

## 8. 公共测试 seam

本计划只在以下公共 seam 建立主要测试：

1. `InteractionAppraiser.assess`：用户证据到 accepted Appraisal；
2. `WorldKernel.accept_turn`：用户轮到原子 World events/Projection；
3. `WorldKernel.advance`：World/Logical Time 到可重放 Projection；
4. `WorldKernel.expression_plan` 与 `ExpressionPlan.resolve`：当前状态到 prompt/validation/fallback；
5. `CompanionEngine.handle_message`：外部 provider adapter 到可投递回复；
6. `companion-eval-dialogue`：离线场景入口；
7. usage summary interface：turn/cadence/token/latency/cost 聚合。

测试使用临时 SQLite；只 mock DeepSeek/平台/时间等真实外部 seam，不 mock自己的纯计算 module。旧 helper 测试在新 interface 覆盖后删除或降为少量内部数学测试。

## 9. 实施阶段与退出条件

### Phase 0：评测入口和基线

- 修复 dialogue eval CLI；
- 固化当前 28 天、NPC、冒犯、repair、fallback 基线；
- 加入问题复现测试：双轨分叉、负面 half-life、repair 刷取、display 过期、>16 episode。

退出条件：每个 P0/P1 都有至少一个先红后绿的 seam test。

### Phase 1：episode-only authority

- typed AffectState/AffectView/Episode/Process；
- episode 聚合 vector/PAD/unresolved；
- per-kind half-life；
- retention/archive/relevance；
- display 即时派生。

退出条件：EF-01、02、05、06 全部通过；旧 28 天 replay 等价或有文档化、合理的新差异。

### Phase 2：repair 与长期动力学

- repair causal graph；
- opportunity/evidence 幂等；
- time-weighted affinity；
- habituation/sensitization/hysteresis。

退出条件：重复话术攻击脚本不能修复；长期轨迹有界且可恢复。

### Phase 3：统一 Appraisal 与 Emotion Programs

- 用户/NPC/Goal/World 统一 stimulus/appraisal；
- dimension-driven emotion programs；
- shame/guilt/jealousy；
- suppression/rumination/reappraisal；
- semantic high-recall risk gate。

退出条件：EF-07、08、09、18 的对抗集、metamorphic tests 和错误归因测试通过。

### Phase 4：ExpressionPlan 深模块

- prompt/validation/fallback 单一 spec；
- reply、repair、afterthought、proactive 全部迁移；
- Engine 删除散落 affect/display 判断。

退出条件：EF-10、11、16 通过；连续 20 轮 outage 轨迹安全且有界。

### Phase 5：热路径、熔断和观测闭环

- observed cadence 单一来源；
- early typing；
- HTTP connection reuse；
- call budget/circuit breaker；
- audit fast path；
- turn-linked token/latency/cost metrics 和报表。

退出条件：EF-14、15、17 通过；所有 latency 优化仍满足 Hard Invariant。

### Phase 6：纵向仿真与清理

- property-based/Monte Carlo/scenario matrix；
- 1/7/30/180 天 replay；
- multi-user、multi-target、频率差异；
- 删除旧双轨代码和重复规则；
- 完整静态检查、全量测试、spec/standards 双审查。

退出条件：EF-13 完成；无剩余 P0/P1；Projection rebuild 与 live state 一致。

## 10. 完成定义

只有同时满足以下条件，本计划才可标记完成：

1. EF-01 至 EF-18 均有实现或明确的、经验证无需实现的处置记录；
2. 所有 P0/P1 具有公共 seam regression；
3. episode 是唯一即时 Affect 权威，生产路径无双写；
4. repair evidence 不可通过重复话术刷取；
5. ExpressionPlan 是 affect prompt/validator/fallback 的单一规则来源；
6. dialogue eval CLI 可运行；
7. 离线 scenario matrix、property tests 和长期 replay 通过；
8. 能按 turn 解释调用数、token、费用和端到端延迟；
9. hot path 优化不绕过 Hard Invariant；
10. 全量测试和静态检查通过；
11. 两路最终审查无 P0/P1；
12. 改动限定范围提交，保留用户并行工作树内容。

## 11. 当前限制与后续真实数据阶段

本文完成后，仍不能仅凭离线评测证明真实长期体验不会机械、过敏或恢复过快。未来有足够真实数据时，应校准：

- Appraisal precision/recall 与 confidence calibration；
- 不同用户沟通风格下的 false harm rate；
- emotion program 参数与 half-life；
- repair opportunity 的自然识别；
- 表达多样性和可察觉模板率；
- 长期关系满意度、恢复感和边界可信度。

在此之前，所有参数都必须版本化、可解释、可重放，并通过上述离线不变量和长期仿真，而不能被描述为已经完成真人统计校准。

## 12. 2026-07-12 实施记录

EF-01 至 EF-18 已按本文接口落地，关键证据如下：

- Affect v2 以 active episode 聚合 vector、PAD 和 unresolved；按 anger、resentment、anxiety、sadness、hurt、shame、guilt、jealousy 配置分量 half-life，resolved episode 保留在因果归档；
- repair evidence 由 `RepairViolationCommitted → RepairCommitmentCommitted → RepairOpportunityObserved → RepairFollowthroughCommitted` 四类事件构成，强制 revision 顺序、active violation 绑定和全局 opportunity 幂等，伪造或跨 violation 的 commitment 会被拒绝；
- affinity 使用逻辑时间衰减、长期 residue 和同一逻辑日 exposure 归一化；
- `InteractionAppraiser` 统一本地明确判断、语用风险路由、V4 Flash proposal 和证据校验；OneBot 合并消息保留每条 source message id、emoji、贴纸、附件、reply target 和可由已结算上一条外发推导的 reply delay；这些证据进入 World ledger；
- Emotion Programs 接入 shame、guilt、jealousy、suppression 和 rumination；生活事件 appraisal 读取已提交的关系、目标重要性和 needs；
- `ExpressionPlan` 绑定 revision、recipient、intent 和 plan hash，并以同一个 spec 生成 prompt、validator 和 fallback；reply、repair、afterthought、proactive 均在使用前从当前 Projection 编译；
- cadence 在适配层冻结并贯穿 Engine、Action 和 usage；hot/warm/cold soft timeout 为 6/10/15 秒，typing 在生成前开始；
- V4 Flash 主模型、普通非 thinking appraisal 和高风险 thinking appraisal 共用 HTTP client 与 provider circuit；timeout、transport、HTTP 408/429/5xx、schema 和程序错误分开处理；不合作取消有独立 grace 上限，主回复、repair、audit、afterthought 和 proactive 均使用统一预算/熔断/timeout；
- usage 可按 world、turn、action、purpose、cadence、model 聚合真实 token、缓存命中、尝试次数、费用和 p50/p95；真实 token 费用会扣减后续自动模型预算，预算不足时保持 Hard Invariant 并走本地安全路径；
- archive projection 有界保留最近 256 条 resolved episode，完整旧因果仍保存在追加式 World Event 中，避免每次投影携带无限 archive；
- seeded property harness 执行 32×60=1,920 个混合 harm/warmth/NPC/repair/decay/source/target 步骤并双跑 replay；纵向矩阵覆盖 1/7/30/180 天、多用户、多 target 和 repair 幂等；
- 五类真实 `CompanionEngine + WorldKernel` 状态（stranger、close、negative affect、NPC spillover、boundary）各完成连续 20 轮 provider outage，检查熔断恢复、事实/归因/自动原谅、Action 幂等和 fallback 重复率；中文语用对抗集当前 precision=0.857、recall=1.000、F1=0.923、ECE=0.067。

本次实现已通过静态检查、纵向/对抗测试以及 Standards/Spec 双轴复审；提交信息见对应 Git commit。本文不把离线通过描述为“已经证明真人不可辨识”；真实长期用户校准仍属于第 11 节的后续阶段。

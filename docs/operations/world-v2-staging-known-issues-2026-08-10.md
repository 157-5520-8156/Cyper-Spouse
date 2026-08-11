# World V2 当前 Staging 版本已知问题

> 状态：持续更新中的现场问题清单
>
> 记录日期：2026-08-10；现场更新：2026-08-11（Asia/Shanghai）
>
> 当前现场代码基线：`feature/companion-turn-v2@f828cce2`
>
> 发布状态：`manual_only / qualification_incomplete`

## 1. 适用范围与证据边界

本文记录当前源码及临时 QQ staging 中已经观察到的问题、尚未完成的资格门，以及已经排除的误报。
它不是生产完成声明，也不以本地测试绿、单次真实对话或隔离 Provider 短跑代替真实 QQ 长期资格。

本次现场环境为：

- 临时 SQLite：`/private/tmp/girl-agent-qq-staging.CeXsRr/qq-staging.sqlite`
- 临时 HTTP/health 端口：`127.0.0.1:8787`
- staging daemon PID：`55637`（2026-08-11 本次更新时仍在运行）
- 未操作旧生产数据库，未替换或重启旧生产 daemon

这些路径、PID 和计数都是瞬时证据，进程退出或环境重建后不得继续当作当前状态。

## 2. 已确认的问题

### W2-STG-001：已提交的生活经历没有正确映射为可用的可见事实引用

**现象**

用户询问近期小事时，角色回答自己刚才在窗边看雨。该内容确实来自已经持久化的
`ExperienceCommitted`，并且首次生成的 Inner Life Snapshot 已向角色提供对应
`recent_self_experiences`。角色也在 `PrivateTurnState.attended_source_refs` 中关注了该 experience。

但模型输出的可见 `world_claim.source_refs` 没有正确绑定该 experience authority，来源闭包记录：

```text
stripped 1 unpinned world claim(s)
```

**判断**

- 不是事实幻觉，也不是生活事件机失效。
- 是 `ExperienceCommitted / recent_self_experiences` 到可见 `world_claim` 证据坐标的映射不闭合。
- 当前结果会把有真实来源的陈述误判为未授权 claim，引发不必要的同角色纠正、额外延迟和成本。

**期望修复**

- 明确 experience authority 是否可以直接成为可见 past-experience claim 的 pinned source。
- 若需要短 token，必须由 capability 编译器提供并在本地稳定映射回完整 experience ref。
- 增加“生活经历进入首次 Snapshot → 角色首次输出引用 → source closure 首次通过”的端到端回归。
- 不得通过放宽来源闭包、删除 claim 或把 `attended_source_refs` 自动升级成事实证据来假修复。

### W2-STG-002：真实 QQ 首次可见回复仍明显慢于目标

当前 staging health 的单个真实样本为：

| 阶段 | 观测值 |
|---|---:|
| ingress → 首次角色 Provider | 约 401 ms |
| Provider TTFT | 约 1.06 s |
| Provider 完整 tool result | 约 5.68 s |
| ingress → 首个完整 Expression frame | 约 5.86 s |
| ingress → 首个候选验收完成 | 约 7.34 s |
| ingress → QQ 可见/ack | 约 7.76 s |

设计目标是 Fast Reply 首 Beat 约 2–3 秒、理想门槛不高于 2 秒。当前主要瓶颈已经不是入站前处理，
而是完整 required-tool 结果形成、候选验收和 dispatch 前等待。样本数只有 1，不能用于稳定分位数结论，
但足以证明当前版本尚未达到延迟目标。

后续必须分别量化：Provider 首 token、完整 head 对象闭合、appraisal/private state 先决字段、source closure、
持久化、typing 和 QQ dispatch，不能靠扩大普通 deadline 或关闭审查伪造提速。

### W2-STG-003：主动联系链仍出现真实技术失败

2026-08-11 复核时，staging health 中 proactive/initiative 的 24 小时窗口已经不是单次偶发失败，而是：

- `attempt_count=51`
- `consideration_count=51`
- `technical_failure_attempt_count=51`
- `model_silent_count=0`
- failure code 全部为 `authored_subcall_exception`
- authorized/delivered：`0/0`

最后一次 `proactive_contact` CharacterInterior inner turn 终态为 `technical_failure`，failure code 为
`role_faculty_unavailable`。scheduler 同时处于 `failing`，最近错误为
`technical_failure:relationshipproposalcompilererror`。

这已经足以证明当前 staging 的主动联系生产链不可用，而不是角色连续 51 次自主选择沉默。该失败必须继续与
角色选择 `silent` 分开记录，并在关系 proposal compiler、proactive faculty 可用性、重试/终态之间逐层定位；
不能把技术失败写成角色“不想联系”。

### W2-STG-004：自然生活感已有事件链，但样本和可感知频率不足

当前临时世界已出现：

- 6 个 life events
- 2 个 committed/activated/settled occurrences
- 1 个 committed private experience
- 1 个已接受的 memory candidate

这证明 `Life Development → Life Aftermath → Experience → Memory → 后续对话回忆` 的核心链能工作；
“窗边看雨”正是一次真实贯通样本。但当前还不能证明以下效果会以自然频率持续出现：

- 多日生活连续性，而不是偶然一次事件；
- 记忆在后续合适时机被自然提起、修订或遗忘；
- 关系变化能持续影响表达、主动性和披露边界；
- proactive、NPC、media 与生活事件不会互相抢占或长期饥饿；
- 重启、多消息轰炸和长时间空闲后仍维持同一角色连续性。

因此当前只能称“生活链出现了首个用户可感知样本”，不能称长期虚拟伴侣体验已经资格化。

### W2-STG-005：虚拟世界生成天气与现实天气之间的产品语义尚未明确

“雷暴/看雨”事件由 Life Development 的 World Author 以 `novel_world_generation` 生成，
并非来自真实天气 API。它在角色虚拟世界的账本内是真实事件，但不代表现实嘉兴当时真的下雨。

当前需要明确产品口径：

- 若角色生活在独立虚拟世界，允许 World Author 生成天气，但对用户不应暗示这是现实观测。
- 若角色被定义为与用户共享现实城市天气，则天气必须由可审计外部感知提供，World Author 不得自行生成冲突天气。

在口径确定前，这不是当前流水线 bug，但属于可能影响用户信任的产品/事实边界风险。

### W2-STG-006：媒体链仅完成隔离 render，不具备真实 QQ 交付资格

当前代码保留 CharacterInterior `select/no_op` 作为唯一媒体选择权威；隔离环境通过显式
`OPENAI_PROXY_URL` 已完成一次真实 PNG render、inspection、重启、effect-once 和 cold replay。

仍未完成：

- 真实 QQ 媒体 dispatch 与平台 terminal receipt；
- 多次真实 Provider 成功率、延迟和成本分布；
- 与普通文本、多 Beat、打断和并发入站共同运行；
- 用户对图片内容、时机和角色风格的一致性体验。

一次约 67.8 秒的隔离 render 只能证明 provider 路由和下游工件链可工作，不能写成媒体生产上线完成。

### W2-STG-007：L4/L11 仍有明确的领域覆盖缺口

- L4 CausalOpportunity 已迁移一批生产 owner，但 media、attachment、perception 及部分
  fact/memory/life-aftermath callsite 尚未全部进入统一 identity/merge/expiry/replay 证据链。
- L11 NPC 当前只接纳 actor-scoped capsule、focused source closure 和 neutral shared-history 安全修复。
  基线没有合格的 NPC communication/opportunity producer，且缺少第二个真实 consumer；因此没有启用新的
  生产模型调用、通信 producer 或 synthetic delivered receipt。

这些属于已知未完成设计范围，不应以现有 typed/fixture 测试替代生产闭环。

### W2-STG-008：口头关系变化没有落成持久 Relationship/Commitment 状态

**现场复现**

用户先提出赠送稀有古董莎士比亚，又询问是否可以成为好朋友。角色可见回复包含：

```text
好呀，那说好了，你是我朋友了。
```

账本确实接受了两条软关系信号：

- `he_remembers_what_i_like_and_offers_something_rare`
- `friendship_affirmation`

这些信号已进入当前 CharacterInterior 的 source-closed relationship context；但同一时刻：

- `RelationshipState` 数量仍为 `0`；
- `RelationshipAdjustment` 数量仍为 `0`；
- 当前 `user_state` 仍为 `null`；
- `Commitment` 与 relationship thread 数量仍为 `0`。

**判断**

这不是“关系完全没有被看见”：软信号已经被角色上下文消费。缺陷在于“可见关系承诺 → typed proposal →
持久关系状态/关系 thread”的闭环没有发生。因此后续回合虽然可能读到软信号，却没有一个可重放、可演进的正式
朋友关系状态；角色的可见承诺与后台 durable state 不一致。

赠书本身也不能误记为已经接受的承诺：角色当时只问“你确定要寄给我？”，没有明确接受实物。

**2026-08-11 后续复核**

后台后来已经累计：

- `RelationshipSignalAccepted=11`
- `RelationshipSlowVariableAdjusted=11`
- 最新慢变量约为 `closeness=3700 / trust=3600 / mutuality=3250`（万分制）

因此“关系完全无法推动”不是当前准确表述；慢变量实际上会随互动变化。仍未闭合的是：

- 角色已经可见地说“你是我朋友了”，但后台 `stage_after` 仍是 `stranger`；
- 最新 adjustment 的 `commitment_refs` 仍为空；
- 可见承诺、关系 stage、typed commitment/thread 没有在同一因果链上原子对齐；
- scheduler 当前又因 `RelationshipProposalCompilerError` 持续失败，关系处理还会反向拖垮主动联系。

所以该问题应定义为“关系会累计，但用户可感知的关系承诺不能可靠、及时地变成一致的 durable stage/commitment”，
而不是简单地说关系数值从不变化。

### W2-STG-009：可见媒体意图、结构化媒体决定与候选生产链不一致

**现场复现**

连续索要自拍时，角色多次在同一角色模型的结构化结果中选择 `media_request=none`，因此没有打开任何媒体
`TriggerProcess`。这说明当时不是媒体 worker、关系硬门或 Provider 在下游拒绝，而是角色本回合没有授权
进入媒体链。

其中一轮可见回复却是：

```text
你越说不看，我越想给你看。
```

但该轮 durable proposal 仍是 `media_request=none`。这是可见措辞与可执行语义决定不一致，不能解释为单纯的
角色自主拒绝。

同时后台媒体库存为：

```text
PhotoCandidate=0
MediaOpportunity=0
MediaPlan=0
```

当前桥只允许角色考虑已经存在、来源闭合的候选，不能凭一句“想发图”自动创造图片候选。因此即使该回合改选
`consider_available_candidate`，现有链也会因 `no_candidate` 终止，仍不会启动 render。

2026-08-11 直接查询 staging ledger，以下事件均为 `0`：

- `ImageEvidenceDeclared / RecipientScopedImageEvidenceDeclared`
- `PhotoCandidateOpened`
- `MediaSelectionProposalRecorded`
- `MediaOpportunityFrozen`
- `MediaPlanRecorded`

这三个零的因果关系不是三个 worker 同时坏掉，而是最上游没有候选：

```text
来源闭合的可视事实声明 = 0
→ PhotoCandidate = 0
→ 角色没有可选 token，MediaOpportunity = 0
→ 没有被接受的 opportunity，MediaPlan = 0
```

当前 `LifeVisualEvidenceAuthor` 已经装入 Life Ecology，但它只会从“已经 settled、在 visual annex 中明确声明
视觉内容与 self-capture capability、通过频率/随机机会策略”的生活 occurrence 生成 image evidence。裸的近期事件、
一句“想拍给你看”或普通 commitment 都不会被它猜测性地升级成图片事实。另一方面，Expression contract 只有在
`PhotoCandidate` 已存在时才向角色开放 `consider_available_candidate`；因此当前形成了一个循环缺口：角色可见地
产生拍照意图，却没有合法入口请求系统为该意图编译候选。

**边界判断**

- 普通生活分享/自拍链没有“陌生人一律不发图”的硬规则；relationship stage 的明确硬门主要位于 P3 私密媒体链。
- 角色基于被连续催促而选择守住边界，本身属于角色自主决定，不应由宿主改写成同意发图。
- 当前真正的系统问题有两个：可见措辞与 `media_request` 不一致；以及没有把合格的生活/承诺/主动意图转化为
  source-closed `PhotoCandidate → MediaOpportunity → MediaPlan`。
- W2-STG-008 的关系状态没有持久化会削弱跨回合连续性，但不是本次普通自拍被拒的确定性硬 veto。

**目标设计边界**

最近事件可以提供“现在有哪些可拍、可见、可分享的事实候选”，但不能由宿主猜测“角色想拍什么”并直接填满
三个状态。正确闭环应是：

```text
最近生活/世界事件或角色私有 media impulse
→ 确定性编译可视事实、capture capability、隐私/同意和过期时间
→ 打开一个或多个 opaque PhotoCandidate token
→ CharacterInterior 自主 select / no_op，并决定构图、披露与表达意图
→ Acceptance 冻结 MediaOpportunity
→ MediaPlan → render → inspection → delivery receipt
```

宿主可以机械地产生“可选机会”，不能把最近事件直接解释成角色欲望，也不能绕过角色选择、隐私、来源闭包和
effect-once。当前最弱点不是缺一个随机猜图函数，而是缺“角色已形成拍照意图 → 以该意图为 authority 请求候选
编译”的 typed bridge。

### W2-STG-010：对话中的动作施受关系丢失，导致角色弄反主语并虚构已完成状态

**现场复现**

用户原话是提出将一本书寄给角色；这只是赠与提议，尚无已接受、已寄出或已收到的持久事件。后续用户用
“你都收了我的莎士比亚了”继续角色扮演，角色先后回复：

```text
莎士比亚都收了还哄你呀。
书都收了还想怎样，难道要我哄你睡觉吗
```

当用户再次明确纠正“明明是你收了我的书”后，角色才回复：

```text
哦对，是我收的。
```

这证明该轮确实发生了动作主语/受事漂移，不只是用户对省略主语句式的不同理解。

**账本证据与根因边界**

- continuity 中保留了“你都收了我的莎士比亚了”，并正确标记 `speaker_ref=user:geoff`；消息没有乱序。
- 原始“我从英国带回来……回头寄给你怎么样”没有形成 typed transfer/offer/thread；对应 interaction-fact
  决策为 `no_change`。
- 现有 Fact lane 只记录“关于用户生活的长期事实”，并明确忽略 remarks about the companion；它没有
  `sender / recipient / object / status` 这类对话动作坐标。
- 当前回合的稀疏 continuity 只给出了若干自由文本片段，没有把“赠与者=user、预期接收者=companion、
  状态=offered_not_delivered”作为可重放语义材料提供给角色。Flash 因而在省略主语的中文上下文中自行补全，
  先弄反施受关系，随后又把尚未发生的接收写成既成事实。

**判断**

这不是传统意义上的“数据库把记忆删了”，而是对话连续性缺少 actor-scoped speech-act/transfer 状态；表现给
用户就是失忆和主语错乱。关系软信号存在也不能替代动作角色与状态。

**期望修复**

- 对提议、接受、拒绝、履行等需要跨回合承接的对话动作，保留来源绑定的 actor/object/status，而不是只存文本。
- counterpart report 只能证明用户说过什么；“要寄”不得自动升级为角色已经收到。
- CharacterInterior 首次生成时应看到简短、明确的参与者坐标；若可见输出颠倒 pinned actor/object 或把
  `offered` 写成 `completed`，应向同一角色模型给出精确原因进行一次受约束重选。
- 增加“用户提出赠书 → 若干省略主语短消息 → 角色仍准确区分赠与者/接收者/状态”的多轮回归，覆盖重启与
  消息轰炸。

### W2-STG-011：主动联系调度发生在持久“想联系”之前，统一内心没有成为真正上游

**当前行为**

当前主动链先由 cadence/关系/情绪/活动/daypart 等确定性材料打开 contact opportunity，再调用
`proactive_contact`。角色在这一次调用里同时生成 `impulse_summary` 和 `now/later/silent` 决定；系统并不存在
一个更早、持久、可重放的“角色先在内心想到用户并决定想联系”的状态。

health 中名为 `autonomous_impulses` 的 facet 也不能当成这类持久 impulse 证据；它是 snapshot 对若干现有材料的
聚合视图，不等于 durable、role-authored autonomous impulse 账本。

**影响**

- 主动联系的语义起点仍是 scheduler opportunity，而不是角色已有的内心意图；
- `impulse_summary` 与最终表达在同一次模型调用里临时生成，不能跨重启、延期或失败稳定保存；
- 当前 51 次技术失败后，用户既看不到主动消息，也没有可检查的持久“她本来想联系但执行失败”的角色状态；
- 媒体承诺也缺同类桥：角色说想拍照，并不会形成可供后台媒体链消费的 durable media impulse。

**期望设计**

```text
事件/时钟只打开 attention opportunity
→ CharacterInterior 私下形成 durable impulse（自由动机文本 + source refs + expiry）
→ 角色决定 contact_user / hold / drop，系统不替她决定
→ contact_user 才创建 ProactiveContactSchedule
→ 到期重新校验后生成 Expression
→ Action / receipt / terminal
```

同一 impulse 类型还应能请求媒体候选编译，但不能把动机枚举化或让本地规则按情绪/关系自动决定联系与拍照。

**2026-08-11 当前薄片**

- 后台 world-stimulus 角色现在获得明确的 `reply_reconsideration` 能力语义：它可自主打开带
  due/expiry/importance 的私有 Thread；该操作只记录“以后再决定是否联系”，不会直接调度或发送。
- 到期后仍由现有 proactive CharacterInterior 再次选择 `now/later/silent`，没有新增本地 `act/hold`
  规则，也没有常驻轮询模型。
- proactive 适配器不再把 `role_faculty_unavailable` 和
  `required_tool_choice_unsupported` 压扁为通用 `authored_subcall_exception`。

这只闭合“已有事件触发的后台意图”薄片，不等于角色拥有无事件的常驻思维流，也不证明主动联系的真实 QQ
频率、延迟和送达已经资格化。

## 3. 已排除的误报／当前不是 Bug

### “窗边看雨”不是对话模型临场幻觉

该内容来自以下持久事件链：

```text
Clock trigger
→ Life Development / World Author proposal
→ WorldOccurrenceCommitted
→ WorldOccurrenceActivated
→ recorded world draw 选择 outcome #1
→ WorldOccurrenceSettled
→ ExperienceCommitted (private)
→ recent_self_experiences
→ 角色在对话中自主选择披露
```

其中，事件机决定虚拟世界发生什么；角色模型决定是否关注、是否透露以及怎样表达。真正的问题是
W2-STG-001 的引用坐标，而不是事件或角色回忆本身。

### 单气泡/多气泡不是固定话术规则

Expression contract 允许角色选择一个或多个 Beat，并决定 now/later/silent。系统只验证结构、来源、Action、
effect-once 和回执，不应固定私密话题必须双气泡，也不应由宿主决定情绪对应几段话。

## 4. 已修复但需要当前最终树重新资格化的项目

- forced-tool provider request identity 已改为由真实 provider emission 与 durable lineage 共享可验证 identity；
  旧验收中的 `causal.inner_life_snapshot_not_correlated` 不应继续作为当前代码已知缺陷，但仍需在最终冻结树上
  重跑隔离真实 Provider acceptance 才能关闭资格证据。
- 重复且值相同的流式 JSON `"type":"head"` 成员已安全折叠；冲突值继续 fail-closed。需要扩大真实流式样本。
- durable 技术失败已有独立 System Notice 路径，不再冒充角色沉默；仍需真实 QQ 故障注入与 terminal receipt 证据。
- source review 已路由到 DeepSeek Flash，并有 100 次精确契约样本；该样本只证明孤立 verdict schema，
  不证明端到端首 Beat、真实 QQ 或独立语义权威。
- 角色选择 `consider_available_candidate` 并在独立 `media_source_refs` 中选择精确、已审查生活来源时，
  MediaRequestRuntime 现在可在 media selection 前请求 LifeVisualEvidenceAuthor 编译一个 source-closed
  候选；它支持 settlement 以及由 typed Experience/LifeContent 绑定证明的 Snapshot 来源别名，不按“最近事件”
  猜场景。PrivateTurnState 注意力本身不授权候选编译；无来源时仍可消费已有候选。
- 后台 world-stimulus 可用已有 Thread authority 持久化 `reply_reconsideration`，proactive 到期后仍需角色再次
  决定；技术失败码与角色 `silent` 保持分离。
- QQ 启动历史补偿不再逐条等待并为每条旧气泡单独生成回复。一个 restart window 现在并发提交给既有
  ingress coalescer，最多 16 条原始 provider message id 保持独立去重、但只形成一个 bounded user volley。
  2026-08-11 staging 复现曾从陈旧临时库导入 11 条历史 Observation 并发送 11 条回复；修复后的同库受控
  重启只执行历史 dedupe 与既有 provider-accepted receipt reconciliation，没有再次调用 `send_private_msg`。

## 5. 尚未完成的发布资格门

以下任一项都不能由本地全量测试或短样本替代：

1. 当前最终源码上的真实 QQ 用户体验与 terminal receipt 闭环。
2. 每个关键 forced-tool/stream purpose 足够数量的真实首次合法样本及失败分层。
3. 稳定首 Beat 延迟达到目标，而不是单次偶然快速。
4. 多气泡、消息轰炸、打断、typing 后终态、重启、重复 receipt 与 effect-once 的真实 QQ 场景。
5. 多日自然生活旅程：Life、Memory、Relationship、Proactive、NPC、Media 能以自然频率被用户感知。
6. 24 小时 wall-clock soak，包含 health、Provider usage、成本、账本增长、重启和 cold replay。
7. §20 人工发布判断；不得自动替换旧生产 daemon 或迁移生产数据库。

## 6. 当前优先级

| 优先级 | 工作项 | 完成标准 |
|---|---|---|
| P1 | W2-STG-001 experience claim 引用映射 | 真实生活经历在首次输出中首次通过 source closure，无权限放宽 |
| P1 | W2-STG-002 首 Beat 延迟 | 分阶段 p50/p95；真实 QQ 首 Beat 稳定进入目标范围 |
| P1 | W2-STG-003 proactive 技术失败 | 故障根因闭合；silent/technical failure 分离；真实 terminal evidence |
| P1 | W2-STG-009 媒体候选入口 | role-authored media impulse 能请求 source-closed candidate，角色仍自主 select/no_op |
| P1 | W2-STG-011 durable 主动意图 | 先形成可重放 impulse，再调度主动联系；失败不丢失角色意图 |
| P1 | 当前最终树真实 QQ 资格 | ingress→provider→acceptance→dispatch→receipt→replay 全链可核对 |
| P2 | W2-STG-008 关系状态一致性 | 可见关系承诺、slow variables、stage、commitment/thread 因果一致 |
| P2 | 多日自然生活旅程 | 生活、记忆、关系、主动性和媒体形成持续可感知闭环 |
| P2 | L4/L11 未迁移范围 | 真实 producer+consumer+health+replay 闭合后再启用 |

## 7. 更新规则

- 新问题必须附最小复现或 durable/health 证据，并标明是代码缺陷、产品决策还是资格缺口。
- 修复后移入“已修复但待资格化”，在最终树真实证据通过后才可关闭。
- 不得把角色自主选择（例如沉默、选择某段经历、选择气泡数量）误写成系统故障。
- 不得把 Provider/审查/dispatch/receipt 任一阶段失败合并成通用“模型超时”。
- 不得把临时 SQLite、loopback OneBot、MockTransport、短跑或本地测试写成生产完成。

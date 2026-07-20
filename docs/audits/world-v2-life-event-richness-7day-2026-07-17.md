# World v2 生活事件丰富度与图片生态覆盖审计（7 天生产场景）

日期：2026-07-17  
范围：`LifeAuthor → ActivityLifecycle → LifeAftermath → EventEcologyMediaCandidate`  
不包含：QQ、聊天生成、主动发话、真实图片 provider

## 结论

当前 reviewed seed **配置了 12 类生活 opening、24 个结局**。在三个独立 SQLite
world、跨七个本地日、九个关键时间窗的生产组装场景中，使用
`life-author-weight.2` 的真实 RandomAuthority 复验实际到达
**10/12 类活动、15/24 个结局**。每个被抽中的活动都完整经过 plan、start、complete、
occurrence settlement、Experience 和两个不可变内容 descriptor；没有用 fixture 直接写入
结果，也没有把 `user_influence`、`interruption` 等无证据来源伪装成 seed。

这说明现有事件池能支撑基础日常、学习、饮食、创作、出行、家务、休息和数字消遣，
仍不能只凭这个规模称为“丰富多彩”，但此前低频的 NPC 社交 opening 已在本次 27 个真实
选择中到达 3 次。配置虽已补入睡前 opening，本次仍未抽到它；系统也仍无动态起床、公共活动，
以及由真实权威事件驱动的共同私密、计划修复、
临时改计划和用户影响 plan author。图片机
可发现生活来源 taxonomy，但在没有独立 `ImageEvidenceDeclared` 时会正确保持
`PhotoCandidate=0`；private/personal 生活内容不会因为“看起来适合拍照”而越权变成图片。

## 场景方法

- 3 个 world seed：`world:life-richness:seed-1..3`。
- 每个 world 跨 2026-07-20 至 2026-07-26（Asia/Shanghai）。
- 9 个信息型时间窗：07:00、09:30、10:30、13:00、14:30、16:45、18:15、
  20:15、21:45，分布在七天；每窗仅推进 plan/start/settle 三次。
- 总计 81 个 scheduler tick，27 个已完成 plan，27 个已结算 occurrence，27 个
  Experience，54 个 life content descriptor。
- LifeAuthor 仍只可接受 RandomAuthority 已抽中的 reviewed candidate；测试模型不能指定
  activity、NPC、地点或 outcome。
- Aftermath outcome 仍由冻结候选集合上的 durable RandomAuthority 抽取。
- 使用真实 SQLite ledger 与 production composition；媒体声明未注入。

## 实际生产可达覆盖

| 轴 | seed 配置 | 7 天 × 3 world 实际到达 | 未实际到达 |
| --- | --- | --- | --- |
| activity | 12 | 10 | `sleep.prepare_for_bed`、`sleep.late_wind_down` |
| outcome | 24 | 15 | 两个 bedtime、两个 late-night、`early-wake-stayed-awake`、`reading-absorbed`、`drink-too-strong`、`tidy-halfway`、`reading-list-warm` |
| source | 5 | 5 | 无 |
| domain | 10 | 10 | 无 |
| social shape | 2 | 2 | 无 |
| deviation | 4 | 4 | 无 |
| visual potential | 8 | 8 | 无 |
| privacy | 3 | 3 | 无 |

实际到达的 activity：

1. `routine.morning_settle`
2. `study.focused_reading`
3. `meal.make_drink`
4. `creative.edit_photo_notes`
5. `commute.short_walk`
6. `household.tidy_small_things`
7. `recovery.quiet_rest`
8. `leisure.digital_browse`
9. `social.literature_reading_list`
10. `sleep.early_morning_wake`

NPC opening 在其 reviewed 时间窗内会进入候选目录，并绑定已注册的
`npc:literature-fan` 与 `location:ecnu-campus-library`；它仍不是必出规则。复验中的 3 次到达
来自通用权重和 RandomAuthority 抽样，模型仍不能指定候选。

另有一个有界 reachability 测试在 NPC 可用的 10:30 时窗搜索最多 8 个 deterministic world
seed。第一个 seed 就由真实 RandomAuthority 抽中 social opening，并完整走完 occurrence、
Experience、NPC appraisal trigger 与 `npc_shared_outcome` taxonomy。这证明 social 是低频未观测，
不是随机空间里的死分支；该测试没有让模型指定 social candidate。

## 完整 seed 矩阵与更大目标矩阵的差距

当前 12 个**静态 clock seed openings** 的完整配置集合为：

- source：`routine`、`intentional_goal`、`social`、`environmental_opportunity`、
  `aftermath`；缺 `interruption`、`user_influence`。
- domain：10 类，现已包含 `sleep_wake`。
- social：`alone`、`npc`；缺 `user_relayed`、`shared_private`、`public`。
- deviation：`persist`、`impulse`、`avoid`、`delay`；缺 `change_plan`、`repair`。
- visual：8 类；缺独立 `character` 事件依据。
- privacy：`shareable`、`personal`、`private`；缺 `public`、`withhold`。

这些坐标不应靠给 YAML 增加名字解决。它们现由下文的 authority-shaped 动态 opening lane
补充：`user_influence` 必须绑定真实用户消息/决定，
`interruption` 必须绑定真实抢占或外界事件，`shared_private` 必须有参与者与 recipient scope，
否则只是伪造来源。当前实现选择 fail closed 是正确的。

## 图片 taxonomy 与私密 candidate gate

在本场景实际发现：

- `activity_result`：来自已完成 plan/结算结果；
- `shared_experience`：来自已提交 Experience；
- `npc_shared_outcome`：代码支持，但本次因 NPC opening 未被抽中而没有实际出现。

所有 taxonomy 条目都保留 source event ref 与 privacy class。真实 SQLite 场景没有任何独立
视觉事实声明，因此 3 个 world 的 `photo_candidates` 均为 0。这一结果符合图片机合同：

- `public/shareable` 生活事实最多只是普通 life-share 的来源；
- `personal/private` 事实不是自动的私密图片资格；
- 私密图片仍需 recipient、关系阶段、本次 PrivateExpressionBasis、衣装/遮盖或过渡事实、
  独立视觉声明与授权；
- 仅“在宿舍”“刚休息”“洗漱”或 private privacy 均不能创建私密媒体。

因此，当前生活事件可以给图片机提供来源多样性，但**不能覆盖完整图片 taxonomy，也不能仅凭
事件池覆盖私密媒体测试**。私密媒体的来源必须由关系/视觉事实/recipient-scoped 授权共同形成。

## 本轮发现并修复的冻结故障

首轮长场景在第一个 `shareable` 散步 occurrence 结算后冻结：事件可分享性被原样复制成
Experience privacy，而 Experience authority 对 `past_experience` 的最低隐私要求是
`personal`。提交被 reducer 拒绝后，LifeEcology 正确 fail-safe，但此后每次 tick 都先恢复
同一条未完成 Experience，最终只留下 4/35 occurrences。

修复后明确分离两种语义：

- occurrence/result descriptor 保持 `shareable`，表示该事实可以进入受控分享流程；
- 角色内部的 autobiographical Experience 至少为 `personal`，不因来源可分享而扩大内部
  记录可见性。

该修复避免了跨日生态冻结，也没有放宽图片候选 gate。

## 后续内部改进优先级

1. 继续扩充 reviewed opening/outcome 内容面；当前权重已经完成通用近期同类重复惩罚、
   最近七天没有社交时的稀有社会机会增权、daypart fit 和生活域节奏连续性，但不能靠权重
   替代内容目录本身。
2. 继续增加真实来源驱动的 wake/public/environmental interruption；用户影响、计划变化、
   修复和消息抢占 interruption 已有 authority-shaped v1，不能再退回 seed 文案。
3. 让 NPC availability 成为动态、时段化投影，而不是只有注册/未注册；NPC 活动结果应继续
   触发 appraisal 并可形成 `npc_shared_outcome`。
4. 为事件结果逐步增加 source-bound objects、environment、appearance/visible state，供
   ImageEventSnapshot 使用；字段缺失时保持 NotRenderable。
5. 对 7 天场景保留“实际观测”门槛，不把全部 opening/outcome 强制出现写成测试脚本；另建
   catalog-level 可达性测试证明每个 opening 至少存在合法时间/NPC/地点组合。

## 2026-07-17 内部可实现项增补

本轮在不接触聊天 adapter/context 的前提下补齐了可由现有 authority 证明的描述符：

| 类别 | 可生成计划 | 已发生判据 | 普通图片生态 |
| --- | --- | --- | --- |
| `sleep_wake` | 是；reviewed 22:30–00:30 睡前 opening | 必须走完 plan/occurrence/Experience | private，默认 `privacy_blocked` |
| `user_influence` | 已接受、精确 MessageObserved-bound plan 会进入可选 lifecycle opening | plan 与 opening 都保留 exact observation evidence；只有矩阵标签不算 | shareable 时仍需独立视觉声明 |
| `plan_change` | superseding plan 会形成 `change_plan` opening；新消息也可形成 active-plan interruption pause opening | accepted replacement 或 source-bound `ActivityPaused` | personal/private 通常阻断 |
| `plan_repair` | paused plan 会形成 `repair` resume opening | 当前 authority event 必须是 `ActivityResumed` | 仍按来源隐私与视觉声明判断 |
| `shared_private` | private/withhold plan 仅在 Message actor 与 participant scope 精确相同时形成 opening | settled occurrence 仍须有多个已提交 participant ref | `privacy_blocked`；不得进入普通 life-share |
| NPC shared outcome | NPC reviewed opening 可生成 | participant ref 必须解析到已注册 NPC | 仅 public/shareable 且有视觉声明时可候选 |

`EcologySourceTaxon` 现在同时保留 `source/domain/social/deviation/visual` 坐标和
`media_readiness`：`declared`、`visual_declaration_required`、`privacy_blocked` 或
`not_lived_event`。这样审计能区分
“生活事实已经发生”“图片证据已经声明”和“隐私允许进入普通图片生态”，不会把三者合并。

## 2026-07-17 权重、可达性与描述符闭环复验

### LifeAuthor 通用权重

`life-author-weight.2` 只改变候选质量，不改变 eligibility：

- 同一 `activity_kind` 在最近七天每出现一次，质量按 `1 / (1 + count)` 衰减；
- 最近七天没有 NPC 社交时，当前真实可用的 NPC opening 获得 1.5 倍软增权；
- daypart fit 在合法窗口内部连续缩放，窗口边缘不会变成不可达；
- 最近一个 reviewed domain 形成宽泛节奏先验：持续专注后轻推 movement/recovery，
  movement/recovery 后轻推 focus；同域重复仍保留非零质量。

最终候选、归一化权重向量、policy version 与抽样结果仍全部进入
`RandomDrawRecorded`，因此重放不会重新计算或重抽。没有任何 activity 被写成必出。

### Catalog-level reachability

新增的 `ReviewedLifeSeedCatalog.reachability_report()` 遍历一周 civil minute，并联合检查
opening、reviewed location 和 reviewed NPC 的静态可用窗口。生产 catalog 当前 **12/12**
opening 均有至少一个合法 witness。另有反例测试证明，当 opening/NPC 虽各自有窗口但与地点
永不重叠时，会明确返回 `no_joint_reviewed_availability`；不会把“配置存在”误报为可达。

这仍只证明 catalog 可能性，不证明动态 NPC 当时 active，也不证明事件发生。

### 00:30–07:30 夜间空窗修复

真实 newcomer v5 的 World Clock 在 00:05 启动，约 01:07 后连续推进；旧 catalog 的
`sleep.prepare_for_bed` 在 00:30 关闭，下一项 `routine.morning_settle` 到 07:00 才开放，
因此 01:00–04:00 的 `no_eligible_opening` 不是随机没抽中，而是目录硬空窗。

现在增加两个 reviewed opening，仍由 RandomAuthority + 模型语义 veto 决定是否发生：

- `sleep.late_wind_down`：00:30–04:00，`source=aftermath`、`deviation=delay`、
  `privacy=private`，描述晚睡后的收尾/仍未立刻睡着；不声称已经入睡。
- `sleep.early_morning_wake`：04:00–07:30，`source=routine`、`privacy=private`，描述清晨
  短暂醒转或提前醒来。它是 reviewed 生活可能性，不是由“准备睡觉”推导出的状态事实。

生产时间纵向复刻 `00:05 → 01:05 → 01:20 → 01:35`：第一个 wake 提供唯一合法的
late-night candidate，后两次 15 分钟 tick 经真实 lifecycle 完成 plan、settled occurrence、
Experience 与内容 descriptor；没有视觉声明，`PhotoCandidate=0`。逐小时 catalog 测试同时证明
00 点 bedtime、01–03 点 late wind-down、04–06 点 early wake、07 点 morning settle 均至少有
reviewed opening。这里证明的是“不会因目录空洞而只能空钟”，不是保证角色每晚必做某件事。

### 生命周期矩阵连续性

此前 `ActivityPlanned` 持有的 `source/domain/social/deviation/visual` 坐标在
`ActivityStarted/Paused/Resumed/Completed` 接受事件中丢失，导致最终生态描述符只有
`activity_result`，却无法说明来源矩阵。本轮改为从 plan 当前的精确 authority event 读取并
仅继承 `matrix:*` refs；hash、revision 或 commit ref 任一不一致就继承空集合。

真实 production chain 现在能在 completed NPC activity 上读回：
`source=social`、`domain=family_roommate_friend`、`social=npc`、
`deviation=persist`、`visual=social`。这没有产生新的生活或视觉事实。

`user_influenced_activity` 也收紧为：plan 的 `observed_message` evidence 必须精确指向已提交
`MessageObserved`；仅把 Clock/Activity ref 标成 `observed_message` 不再能得到该分类。

### Authority-shaped dynamic opening v1

静态 LifeAuthor 负责“没有外部原因时，生活里有哪些 reviewed 可能”；新增的动态 opening
仍走同一条 `ActivityOpeningCatalog → model token choice → proposal → acceptance` 链，不直接写
结果，也不保证一定选择：

- `user_influence`：`ActivityPlanRuntime` 只接受已经存在的 `MessageObserved` projection ref；
  plan 的 EvidenceRef 必须匹配 observation revision/hash。到了后续 ClockAdvanced，该计划才成为
  model 可选择或 no-op 的 opening。
- `interruption`：当前 plan 为 active 且存在 revision 晚于当前 plan authority 的外部
  MessageObserved 时，pause opening 会绑定该 message；如果 active plan 已越过其已接受的
  scheduled close，则可形成只绑定 plan+clock 的 `clock_activity_conflict` opening。后者不会
  伪称“有人/天气打断”，两种 opening 都仍可被模型 no-op。
- `change_plan`：`ActivityPlanRuntime.replace()` 用同一真实 observation 原子 abandon predecessor
  并创建带 `supersedes_plan_id` 的 successor；successor 在下一次 clock wake 作为可选
  `change_plan` opening，而不是直接宣称新计划已开始。
- `repair`：paused plan 的 resume opening 标记为 repair；只有模型选中并通过 acceptance 才写
  `ActivityResumed`。
- `shared_private`：必须同时满足 plan privacy 为 private/withhold、plan 有精确 observed-message
  evidence、message actor 精确存在于 participant refs。participant 不一致、revision/hash 不一致
  或 observation 缺失均 `blocked_by_missing_capability`，不会降级成普通 shared opening。

接受事件会写入通用矩阵坐标，例如 `matrix:source:interruption`、
`matrix:deviation:change_plan/repair`、`matrix:social:shared_private/user_relayed`；这些坐标来自
已解析的 authority shape，不让模型自由填写。真实 SQLite 纵向测试已覆盖：

1. MessageObserved → private shared plan → model-selected `ActivityStarted`；
2. newer MessageObserved → model-selected interruption `ActivityPaused`；
3. later clock → model-selected repair `ActivityResumed`；
4. MessageObserved → atomic replacement → model-selected change-plan `ActivityStarted`。

另有 catalog-level 反例覆盖 overdue active plan：它可产生
`cause_kind=clock_activity_conflict` 的 interruption opening，但 `cause_observation_id=null`，
从而不会把时间冲突改写成外界事件。

### 仍需外部输入才可能触发的边界

- 系统不能在没有用户消息时产生 `user_influence/shared_private`；这是缺少真实输入，不是代码
  fallback。上游语义层仍需决定某条消息是否值得提出 plan，不能把每句话都变成计划。
- 非消息型环境抢占（临时天气、课程通知、NPC 临时来访）仍需相应的可信 observation writer；
  当前 v1 只对 ledger 已有的消息抢占和明确的 plan/clock window conflict 做
  authority-shaped opening。
- shared-private opening 只证明共同活动参与范围，不是私密媒体授权。图片仍必须有独立
  `ImageEvidenceDeclared`/`VisualFactRecorded`、recipient-scoped basis 和图片机授权。
  seed 结果文案不能作为视觉事实，缺失时继续 `PhotoCandidate=0` 或 `NotRenderable`。

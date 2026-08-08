# 统一人物内心：`CharacterInterior` 深 Module

状态：本轮实施基线

日期：2026-08-04

适用范围：World V2 的普通入站、主动联系、Life、NPC 互动、Appraisal/Affect、关系、记忆、
媒体、外界感知与后续自主能力

上位约束：`CONTEXT.md`、ADR-0010、ADR-0011、ADR-0012、ADR-0014、ADR-0016

## 1. 为什么需要一个统一边界

World V2 已有 Character Core、Appraisal、Affect、Relationship、Private Impression、Memory、
Aspiration、Goal、Thread、Commitment、生活经历、外界感知和 `PrivateTurnState`。这些机制各自
拥有独立事件与 reducer 是正确的：它们的认识论权限、生命周期和结算方式本来就不同。

问题在于角色作判断以前的读取、注意、召回、主观整合和表达立场散落在多个业务入口：

- 普通入站同时拥有表达与 appraisal 的专用模型 Adapter；
- 主动联系、Life、关系、私人印象、媒体和感知又各自组装近似但不相同的角色上下文；
- 同一 cursor 上可能产生多个“当前自我”裁剪，模型看到的并不是同一个人；
- 情绪可能在表达之后才由另一条 worker 路径形成，造成下一轮才体现；
- 召回、动机、当下注意和表达没有一个稳定的 `InnerTurn` 身份；
- 技术失败容易被某条业务旁路误写成 `silent/no_op`；
- 新增一个心理能力往往要在所有入口重复接线，随后逐渐漂移。

因此本设计统一的是“角色如何在同一个有来源的自我中经历和作选择”的深 Module，而不是把
所有心理事实合并成一个随意覆写的 `MindState`，也不是增加一个规定行为的总控模型。

## 2. 设计原则

1. **角色拥有语义决定权。** 动机、注意、召回、主观解释、是否行动、何时行动、是否表达、
   表达内容和节奏均由对应角色模型决定。
2. **系统只拥有硬边界。** 事实来源、隐私、同意、安全、能力、Action 授权、effect-once、
   CAS、回执和重放继续由确定性代码验证。
3. **一个 actor、一个 cursor、一个 canonical self。** 同一次人物判断只绑定一个
   `InnerLifeSnapshot` 和一个 `InnerTurn`；消费者不得自行拼第二份自我。
4. **领域权威保持分离。** Appraisal、Affect、Relationship、Memory、Goal 等继续使用现有
   typed authority 与 reducer；`CharacterInterior` 只负责联合读取、角色侧整合和提议路由。
5. **同一机会只有一条角色作者路径。** 不同时运行新旧接口，不以“兜底”名义并行调用另一
   角色作者，也不在失败后让本地模板冒充角色。
6. **瞬时私人自我不是隐藏推理。** 只保存短、可审计、来源绑定的 `PrivateTurnState`，不记录
   chain-of-thought，不把它升级成 World Fact。
7. **开放选择，不设心理剧本。** 八项 Faculty 是材料与能力，不是回复模式、动机枚举、情绪
   矩阵或事件目录。
8. **历史可读，新生产唯一。** 旧事件、旧 Model Result 和旧 wire 永久可重放；新生产只生成
   当前合同，不保留 legacy compatibility route。

## 3. 对外 Interface

`CharacterInterior` 对生产调用方只暴露三个行为入口；另有一个只读的
`runtime_health()` 可观测面，它不接受角色机会、也不能触发模型或写入：

```python
class CharacterInterior:
    async def project(
        self, subject: InteriorStimulus | InteriorOpportunity
    ) -> InnerLifeSnapshot: ...

    async def experience(self, stimulus: InteriorStimulus) -> InnerTransition: ...

    async def consider(self, opportunity: InteriorOpportunity) -> InnerDecision: ...
```

### 3.1 `project`

`project` 在精确 ledger cursor 上异步、确定性地编译 canonical `InnerLifeSnapshot`。异步只用于
读取生产 Capsule/Projection seam；它不调用角色模型。它只读取已接受的
World Projection、来源绑定的 advisory 和明确的 privacy/viewer scope，不调用模型，也不产生
World mutation。

只读界面、健康度和 Module 内部可以调用 `project`。任何需要角色语义选择的业务入口不得
拿到快照后自行调用角色模型；它必须调用 `experience` 或 `consider`。

### 3.2 `experience`

`experience` 用于一个已经提交、但当前不要求角色产生外部行为的刺激，例如 occurrence
settlement、NPC 互动结果、External Perception、Action receipt、关系事件或计划被打断。
它让 actor 在同一快照上自由形成零个或多个主观变化提议，返回 `InnerTransition`：

- 可以提出 Appraisal、Affect、Relationship、Private Impression、MemoryCandidate、
  Aspiration、Goal、Thread 或 Commitment 的变化；
- 也可以明确选择没有值得持久化的变化；
- 结果仍需各 typed authority 验证和接受，不能直接覆写 Projection；
- 供应商或结构失败返回技术失败，不能伪造成“角色无感”。

### 3.3 `consider`

`consider` 用于需要 actor 作出选择的一个机会，例如普通入站表达、主动联系、Life 选择、
外界信号注意、媒体使用、重要 NPC 决策或已到期的内部事项。它返回 `InnerDecision`，其中包含：

- 同一 `InnerTurn` 的瞬时 `PrivateTurnState`；
- 角色自由选择的 Recall 请求及 Recall 后重新形成的最终私人状态；
- 可选的 typed interior transition proposals；
- 一个业务类型明确的 `CharacterDecision`；
- 完整的模型调用、来源、snapshot、capability 与重选 lineage。

若机会自身携带新刺激，例如用户当前 Observation，`consider` 必须在同一个 `InnerTurn` 内先让
模型吸收它，再形成可见表达和主观变化。调用方不得先走一条 background `experience`，再并行
走另一条 expression 模型，从而消除“情绪下一轮才体现”和同一消息被两个角色模型分别解释。

### 3.4 Interface 的深度边界

三个方法内部隐藏：

- Context Capsule 与 actor-scoped Projection join；
- canonical snapshot 编译、预算、裁剪和 privacy redaction；
- 稳定 `InnerTurn` / provider call / Recall / retry identity；
- 瞬时私人自我、注意、选择性 Recall 和 Recall 后重整合；
- 各业务 purpose 的能力 manifest 与严格 wire；
- 同一角色模型的一次受约束重选；
- typed proposal 的来源闭包与路由；
- Model Result 审计、CAS、失败分类和健康度。

调用方只提供 actor、机会或刺激、精确 cursor、viewer/privacy scope 和已声明能力，不能传入
“应该安慰”“是否追问”“要不要联系”“表现得生气一点”等行为参数。

## 4. Canonical `InnerLifeSnapshot`

每个快照至少绑定：

- `contract_version`；
- `world_id`、`actor_ref`、ledger cursor 与 Logical Time；
- `viewer_scope`、privacy ceiling 与 capability-manifest hash；
- 八项 Faculty 的来源绑定内容、裁剪记录和 availability；
- 每个 item 的稳定 ref、直接 source refs、权限、有效期与冲突/修订 lineage；
- `snapshot_hash`，由规范化语义内容确定性计算。

同一 actor、cursor、scope、budget policy 与 compiler version 必须产生字节等价的快照和相同
hash。不同业务消费 view 只能从 canonical snapshot 做确定性删除或 redaction，不能改写内容、
补充行为建议或重排为隐性优先级。相同 source item 在所有 view 中保持相同 identity。

极端预算下至少保留稳定身份、当前情境、并存 Affect、当前主要关系、开放事项、当前刺激以及
一条来源明确的近期自身经历；遗漏哪些内容必须进入 truncation log，不能静默消失。

快照是 read model，不是第二真相源。它不得被整体持久化为可覆写心理状态，也不得让模型输出
替换其中的 authoritative slices。

## 5. 八项 Interior Faculty

Faculty 表示角色作判断时可使用的一组心理能力和来源材料。它们同属一个快照和一个
`InnerTurn`，但不规定角色必须使用哪项、以何种顺序使用或得到何种行为。

### 5.1 瞬时私人自我与当前注意

角色在每个 `InnerTurn` 中形成短、自由文本的当下状态：此刻在意什么、感到什么、想靠近或
回避什么、对什么不确定，以及是否有表达冲动。它同时引用实际进入注意的 source refs。

该状态属于 Proposal audit，不是耐久事实。宿主不得从关键词生成它，也不得在角色输出后补写
事后 rationale。

### 5.2 角色选择性召回

角色可基于当前自我、情绪、关系、目标和刺激主动请求 Recall。检索系统负责 actor-scoped
候选召回、来源验证、预算与去重；角色决定是否召回、召回目标和最终是否采用。

相似度只决定候选可见性，不能决定行为。Recall 返回后必须在同一 `InnerTurn` 内形成新的最终
`PrivateTurnState`；旧私人状态和新状态具有明确 parent relation，不能混用两份表达草稿。

### 5.3 Appraisal 与 Affect 共享同一私人上下文

当前刺激对角色意味着什么，以及角色想如何回应，必须由同一个角色作者在同一 snapshot 上
形成。普通入站不得由 expression 模型和 background appraisal 模型各自解释一次。

模型可以同时提出相互竞争或并存的解释与 Affect 分量，也可以 no-change。Appraisal/Affect 的
接受、幅度、来源与生命周期仍由 typed authority 管理；它们不直接映射到措辞。

### 5.4 连续情绪、余波与再解释

快照保留并存 Affect episodes、衰减、残留、Change Phase、未解决 cause 和后续再解释，而不是
把角色压成一个 mood label。新刺激可以强化、修订、抑制、转移或不影响旧情绪，具体语义由
角色模型提出。

表达某种情绪不机械结算它；时间流逝不机械等于原谅或遗忘。确定性代码只按已记录 policy 推进
时间和 episode 生命周期。

### 5.5 主观关系取向

快照同时呈现角色对用户、对 NPC 的方向性关系状态，以及在权限允许时对方对角色的可观察
关系读数。trust、closeness、respect、reliability、mutuality、friction、repair confidence
等是迟滞坐标，不是行为阈值。

角色可以形成有来源、可修订的私人关系理解；它不是 User Fact，也不能读取 NPC 未通过行为或
结算事件暴露的私有状态。

### 5.6 愿望、目标与内在冲突

Aspiration、Goal、Plan、Thread、Commitment、价值张力、资源压力和未解决选择共同进入快照。
角色可以自由形成新方向、修订优先级、犹豫或放弃；系统不提供创业、恋爱、毕业等人生选项
目录，也不把某个愿望直接映射成剧情。

任何长期变化仍须进入相应 proposal/acceptance/settlement；想过不等于计划，计划不等于发生。

生产落地不再使用 aspiration seed catalog 或独立 Aspiration Runtime。生活/NPC/关系等已提交刺激
进入现有 `experience` InnerTurn 时，同一个角色结果可选择不改变方向，或以开放文本提出
`plant / reinforce / revise / abandon`。系统只校验当前刺激、现有 aspiration head、来源闭包、
privacy、entity revision 与 CAS；不生成方向，也不从兴趣映射剧情。`AspirationRevised` 保存完整
before/after 与可选 `tension_summary + tension_source_refs`，`AspirationAbandoned` 保存角色自己的
自由理由和来源。历史 `AspirationPlanted/Reinforced/Faded/Crystallized` 继续原样重放；具体行动
仍只能由 Life 的角色选择形成 Plan 后结晶，不能把“想做”伪装成“已经安排/发生”。

同一 `experience` 结果还可以携带至多一个来源闭合的长期变化草稿，复用既有 typed
`Goal / Thread / Commitment / MemoryCandidate` proposal seam，而不是增加通用 `MindState`
字典或第二个作者：

- Goal 目前只开放对精确现有 head 的 `pause / resume / abandon`。现有 Goal authority 没有可供
  角色创建新 outcome 的不可变内容权威，因此 `open` 必须显式 unavailable；不得把自由文本
  hash 成一个看似合法的 `outcome_ref`。未来只有在 Goal 内容 sidecar、descriptor、privacy 与
  冷重放校验形成独立权威后，才能另行开放创建。
- Thread 可从当前已提交 stimulus 打开，或对明确提供的现有 head 进行 update/resolve/cancel；
  现有 head 必须同时绑定 entity revision 与 accepted event。
- Commitment 只能绑定 capability 中已提供的开放 Thread（以其 `ThreadResolved` 合同作为履约
  证据），或释放一个精确现有 head；模型不能凭空承诺一段没有内容或履约权威的文字。
- MemoryCandidate 只在当前 stimulus 本身就是精确的 active Fact、committed Experience 或
  terminal Thread authority 时可选；模型决定是否保留及 salience，系统只验证来源、privacy、
  revision 与派生 retrieval strength。

这些能力只决定“哪些变化可以合法提出”，不构成动机菜单。任何不存在、过期、revision 不匹配
或来源不闭合的选择都在 proposal 写入前失败，并进入同一角色的一次受约束重选；不会落一条
半合法 proposal 后再由旁路猜测修复。

### 5.7 自主冲动与行动意图

角色可因当前生活、情绪、关系、记忆、外界感知或纯粹主观联想形成联系、分享、求助、安慰、
沉默、继续生活、使用媒体或其他开放冲动。冲动与动机使用自由文本和来源引用，不建立枚举。

`CharacterInterior` 只让能力与后果可见；角色决定是否采用。真正外部 effect 必须转为
`ActionIntent`，经现有授权、dispatch 和 receipt 链执行。

### 5.8 表达前的内在立场

`ExpressionDraft` 不得从 Context 直接旁路生成。它必须消费同一 `InnerTurn` 的最终私人状态、
所选 Recall、当前 Affect/关系/目标张力以及角色自己的表达立场，随后由同一角色作者选择
silent/later/now、一个或多个 Beat、是否提问、是否打断及表达节奏。

系统不规定“先共情再提问”、提问配额、消息数量或情绪语气。ExpressionPlan 只负责携带角色
已经作出的完整选择并进入硬边界与 Action 链。

## 6. `InnerTurn` 身份与生命周期

`InnerTurn` 是一次 actor-owned 私人整合和选择的稳定审计身份，至少绑定：

- world、actor、purpose 与 opportunity/stimulus identity；
- 起始 ledger cursor、Logical Time、snapshot hash 与 compiler contract；
- capability manifest、privacy scope 与 budget policy；
- 角色作者 endpoint/model/schema identity；
- attempt ordinal、Recall parent、correction parent 与 provider request hash；
- 最终 `PrivateTurnState`、transition/decision payload hash 和 terminal state。

同一 opportunity 的并发 caller 通过 effect-once process join 同一个 turn。新 Observation、
cursor 变化或 capability manifest 变化使旧候选失效；旧 bytes 不能换标签后复用。Recall 和一次
受约束重选是同一个 turn 的有记录子调用，不是新角色或并行 winner。

对于带即时行为的刺激，只建立一个 `consider` turn。它可以同时携带 interior transition 与
Expression/Life/Media/Attention decision，并在现有 CAS transaction 或严格有序的 acceptance
chain 中提交，不能让 background reflection 与前台 decision 双写同一主观变化。

## 7. 权威与写入边界

`CharacterInterior` 可以提出，但不直接拥有：

- World Fact、User Fact、Committed Experience、NPC 客观身份和外部事件；
- Character Core 的治理字段和稳定身份；
- Appraisal、Affect Episode、Relationship Adjustment、Private Impression、Memory、
  Aspiration、Goal、Thread、Commitment 的最终接受权；
- Action、媒体、perception、privacy、consent、安全和平台 receipt；
- World Author 的环境结果或 occurrence settlement。

每种输出继续进入既有 typed authority。不得增加 `InnerLifeStateReplaced` 或通用
`InteriorChanged(payload: dict)` 事件。这样既保留各领域不变量，又让所有主观提议从同一个
角色内心 seam 发出。

## 8. 错误语义

错误必须区分以下终态：

- snapshot/capsule 编译失败；
- 角色 provider timeout、network 或 unavailable；
- strict wire 解析失败；
- source ref、capability 或 cross-field 结构越界；
- hard source review / privacy / consent / safety 拒绝；
- stale cursor、CAS conflict 或 lost claim；
- Action dispatch/receipt 失败。

模型输出不合法时，只向**同一角色模型**提供可用证据、能力和精确结构失败原因，允许一次受约束
的完整重选。重选仍失败则记录技术失败并按所属业务的 durability policy 重试：

- 不制造 `silent/no_op/no_change`；
- 不启用旧 Adapter；
- 不切到另一角色作者；
- 不使用本地模板生成可见文本、动机或心理状态；
- 不复用已被拒绝的可见 Beat；
- 不重复发送已经授权或 settled 的 Action。

角色成功选择 silent/no-change 与技术失败必须使用不同 terminal state 和 health 指标。来源或
权限拒绝是候选失败，不是角色态度；角色可在一次重选中基于仍可用证据作新选择。

## 9. 生产调用拓扑

目标拓扑只有一个角色内心入口：

```text
accepted stimulus / due opportunity
  -> CharacterInterior.experience(...) OR CharacterInterior.consider(...)
      -> canonical InnerLifeSnapshot @ cursor
      -> one actor-owned InnerTurn
      -> optional actor-chosen Recall
      -> same actor author result
      -> typed proposal routing
  -> existing Acceptance / CAS / Action / receipt authorities
```

业务 Module 的职责：

- Chat：提供 Observation opportunity，消费 `InnerDecision.expression`；
- Proactive：提供到期或事件刺激 opportunity，消费 now/later/silent；
- Life：提供生活机会与能力，消费角色的 life decision；
- Appraisal/Affect/Relationship/Memory：消费 `InnerTransition` typed proposals；
- Media：提供已授权能力，消费角色是否/如何使用的 decision；
- Perception：提供 source-bound window，消费 attend/ignore/uncertain decision；
- NPC Ecology：NPC 自身继续使用成本受控的 actor-scoped 自治状态，不复制主角完整内心；
  NPC 的言行与关系变化作为来源绑定 stimulus 进入主角的 `CharacterInterior`。若未来为某个 NPC
  启用完整内心，必须建立独立 actor-scoped 实例与私有存储，不能复用或读取主角 snapshot。

除上面明确隔离的 NPC actor domain 外，这些面向主角的业务 Module 不得拥有自己的 role
prompt、role provider、`current_self_state` builder、Recall coordinator 或 private-state
generator。NPC domain 也不得读取主角 snapshot；其结果必须作为来源绑定 stimulus 回到主角
Interior，且由 architecture guard 与账本健康指标验证 actor scope。

## 10. 迁移与删除矩阵

| 旧生产表面 | 新归属 | 切换规则 |
| --- | --- | --- |
| `current_self_state` 新请求字段与各处重复 builder | `InnerLifeSnapshot` | 新生产不再生成或读取旧 key；历史 Model Result/wire 只读重放 |
| 普通入站的 `SingleCallInboundCognition` 公共装配 | `CharacterInterior.consider` | 删除公共构造与 host 引用；需要保留的算法改名为 Module 私有实现 |
| `SingleCallExpressionAdapter` / `SingleCallAppraisalAdapter` | 同一个 inbound `InnerTurn` | 删除生产 composition 字段，不能同时调用 |
| 独立前台/后台 Affect 与 Appraisal role call | `consider` 或 `experience` 的 typed proposals | 同一刺激只解释一次；durable reducer 保留 |
| Relationship / Private Impression 的独立角色作者入口 | `experience` typed proposals | 删除 role provider 参数与 worker 旁路；接受权保留 |
| Proactive 专用角色 prompt/adapter | `CharacterInterior.consider` purpose view | 调度与 Action lifecycle 保留；语义作者入口删除 |
| Life Character 专用角色 prompt/adapter | `CharacterInterior.consider` purpose view | 世界机会生成保持独立；主角对机会的体验、动机与选择只走 Interior |
| Media/Perception 的角色语义 adapter | `CharacterInterior.consider` capability view | renderer、source acquisition、inspection/review 保持独立 |
| `SemanticAdvisoryAdapter` / `AdvisoryCompiler` / `PinnedTurn._advisory_snapshot` | 普通入站的同一个 `InnerTurn` | 物理删除模型分类器、第二份当前自我和前台并行调用；来源绑定的既有投影视图仍可作为材料 |
| 文本端点模型 | `TextTurnEndpointController` | 只能估计用户是否很快继续输入；不得输出或组装角色 Appraisal、Affect、Relationship、动机或回复决定 |
| 各消费者直接构造 PinnedTurn/Recall | Interior 私有 compiler/coordinator | Pinned Turn 领域语义保留，生产构造入口不外露 |
| `private_impression_model`、`affect_model`、`relationship_model`、`proactive_model`、`life_character_model` 等 builder 参数 | 一个 `character_interior` 依赖 | 从 production builder、composition 和 host 签名删除 |
| 历史 event/reducer/codec | 原地保留 | 冷重放继续读取；不得改写、删除或双写迁移事件 |

配置也随职责一次性改名：角色同一作者的可选思考路由只接受
`DEEPSEEK_CHARACTER_THINKING_*`；单用途文本端点只接受
`WORLD_V2_TEXT_ENDPOINT_{ENABLED,BASE_URL,MODEL,API_KEY}`。旧
`DEEPSEEK_DEEP_APPRAISAL_*` 与 `LOCAL_APPRAISAL_*` 不作为别名读取，出现时启动明确
失败并报告替代键，防止旧环境静默重新接通已删除的语义旁路。

迁移是一次切换，不采用长期 compatibility shim：

1. 先建立新 Interface 级 red tests 与架构 guard；
2. 在 Module 内迁移并验证各 purpose view；
3. 将所有生产 composition、host、worker 和 scheduler 调用点改到新接口；
4. 删除旧公共类型、builder 参数、factory 字段、runtime lease 和生产配置；
5. 全仓静态扫描不允许旧调用符号出现在生产路径；
6. 冷重放与历史解析保留在明确命名的 replay codec 中，不可被生产依赖；
7. 切换后不保留 feature flag 把流量退回旧角色接口。

若某段旧算法仍有价值，必须迁入 `CharacterInterior` 私有实现并去掉旧公共身份；不能以 wrapper
方式让新旧 Interface 同时存在。

## 11. 重放与部署兼容性

- 历史 V2 events、Model Results、Proposal、Action 和 receipts 均不可变；新实现不回写历史。
- reducer 冷重放只读取已记录结果，绝不调用 `CharacterInterior` 或 live model。
- 本次切换安装 `world-v2-reducers.52`：打开 `.50/.51` head 时先验证对应历史 semantic hash，
  再只重建派生 head；不可变事件数、顺序、event hash 与 Action/receipt bytes 不变。已经物理删除的
  角色语义 worker 所遗留的 open TriggerProcess 由 `.52` 确定性折叠为技术终态，不能调用模型、
  不能伪装成角色 `silent/no_change`，也不能在重启后再次执行。
- 新 Appraisal 写入使用 `appraisal-matrix.2`，其中 `meaning` 是角色形成的 1–128 字自由文本；
  `appraisal-matrix.1` 及其旧枚举字符串仅作为历史事件内容继续重放，不再成为新模型的选项表。
- 历史 `current-self-state.1`、旧 expression wire 与旧 private-state payload 仅由 replay codec
  解析；它们不能形成新的 production request。
- 新 snapshot/turn contract 使用新版本和新 identity formula，绝不与旧 cache key 命中。
- 已授权 Action 继续由原 Action lifecycle settle；切换不能重生成或重复发送。
- 尚未产生 durable角色结果的旧 claim 在停机时 drain；无法完成者释放 lease，由新接口以同一
  trigger/opportunity identity 建立新 attempt。旧迟到结果因 epoch/cursor 不匹配被拒绝。
- 终态旧 silent/no-op 保持其历史 effect-once；不得因迁移重新解释或重发。
- 任何 snapshot compiler 版本变化都进入 identity；相同版本冷重放必须 hash 等价。
- 发布前后比较 V2 head cursor、事件数、最新 hash、关键 Projection 与 Action 状态；不通过则
  回滚二进制/数据库临时副本，而不是运行 legacy role route。

## 12. 防止旁路与新老混用

新增架构测试和启动前验证：

- production host/composition/application 不得 import、构造或接收旧 role Adapter；
- 每个需要角色语义的 business purpose 必须注册到 `CharacterInterior` capability registry；
- 未注册 purpose 在 projection 或 provider call 前 fail closed；不能隐式回退到 `generic`。若确需
  通用机会，调用方与 registry 都必须显式使用字面 purpose `generic`；
- 同一 opportunity 只能有一个 live `InnerTurn` 和一个可见 character-author call；
- 除记录式 Recall 与一次受约束重选外，不允许同一 turn 第二个 role author；
- ExpressionDraft、主动 decision、life character choice、media use 和 attention choice 都必须
  带有效 `inner_turn_id` 与 `snapshot_hash`；缺失时 fail closed；
- typed interior proposal 必须绑定同一 turn/cursor，不能由 background worker 换 cursor 补写；
- 主动调度中的情境刺激必须对目标 actor 有可观察权威：共同 occurrence/experience 需要 actor
  参与，Activity/Life Arc 需要 actor ownership，外界信息需要 actor-bound perception；
  `public/shareable` 只描述披露范围，不能单独证明角色实际知道 NPC 的私事。新调度、技术重试、
  未完成旧 process 恢复与最终 proactive audit 使用同一个判断，不能形成恢复旁路；
- legacy replay package 不得被 runtime/composition import；
- 配置中出现已删除旧 provider role 时启动失败，而不是静默忽略；
- health 中 `legacy_interface_invocations` 和 `parallel_character_author_conflicts` 必须始终为零。

## 13. 可观察性与健康指标

`/health` 保留既有字段并增加：

- `character_interior.contract_version`、active compiler/model route；
- 最近 `inner_turn_id`、purpose、cursor、snapshot hash 与 terminal state（不暴露私人原文）；
- 各 Faculty 的 item count、availability、source-closed count 和 truncation reason；
- snapshot compile p50/p95/p99、cache hit、hash divergence 与 stale-cursor rebuild；
- `experience`/`consider` 总数、成功、角色 no-change/silent、技术失败与一次重选结果；
- Recall 请求、命中、被角色采用、Recall 后重整合与 source rejection；
- typed proposal 按 Appraisal/Affect/Relationship/Memory/Goal 等分类的 proposed/accepted/rejected；
- expression 的 provider TTFT、首个完整 Beat、source closure、Action authorization、平台 ACK 和
  API 外开销；
- per-purpose 角色模型 calls、tokens、latency 和 duplicate suppression；
- `legacy_interface_invocations`、`parallel_character_author_conflicts`、`dual_write_conflicts`；
- `semantic_author_count`、purpose→author identity 与未验证作者清单必须来自冻结后的 Faculty
  registry，不能由 health handler 写死为 `1`；
- replay snapshot mismatch、event head mismatch 与 pending old-contract claim。

下列情况至少标记 warning，能破坏事实或重复发送时标记 critical：

- 任一生产旧接口调用大于零；
- 同 actor/cursor/scope/compiler 的 snapshot hash 不一致；
- 同一 opportunity 出现未解释的第二个角色作者或双写；
- 技术失败被记录为角色 silent/no-change；
- 已到期 consideration 超过两个 scheduler 周期仍未形成 turn；
- role provider 成功但没有 terminal Model Result/decision；
- ExpressionDraft 缺 inner-turn lineage；
- API 外开销、snapshot 编译或 Action dispatch 超过其 SLO；
- 冷重放产生不同 Projection、Action 或 snapshot semantic hash。

## 14. 测试计划

### 14.1 Interface 与八项 Faculty

- 同一 cursor/scope/compiler 的 snapshot 字节与 hash 等价；新 cursor 不误用旧 snapshot；
- 八项 Faculty 均有来源、权限、expiry 和裁剪记录；缺 lane 时显式 unavailable；
- 极端预算仍保留最小自我闭包；
- 瞬时私人自我由角色模型返回，宿主不补写；
- Recall 由角色选择，返回后重新形成私人状态；不召回也是合法选择；
- 新 Observation 的 Appraisal/Affect 与 Expression 使用同一 turn；
- 旧情绪、关系、愿望和未完事项能同时进入决策，但测试不固定具体行为；
- ExpressionDraft 缺有效 inner-turn lineage 必须拒绝。

### 14.2 生产入口迁移

- 普通入站、主动联系、Life、关系/私人印象后处理、媒体和感知逐一证明只调用新接口；
- AST/import/signature guard 证明 production 没有旧 Adapter、旧 builder 参数和旧 Context key；
- 同一刺激不会同时触发 background role reflection 与 foreground role decision；
- 并发 claim、CAS conflict、新入站打断和重启不产生双模型、双写或重复 Action；
- 各 purpose 只获得其 capability view，不能越过 privacy 或 Action boundary。

### 14.3 错误与恢复

- provider timeout、invalid JSON、cross-field/source violation、review rejection、stale cursor、
  Action failure 逐类具有不同终态；
- 同一角色模型一次受约束重选，仍失败进入技术重试；
- 技术失败绝不生成 silent/no-op/本地可见文本；
- 已送达 head 在 tail、重启或迁移恢复时不重发；
- 旧迟到 provider 结果不能赢过新 cursor；
- 已授权 Action 在切换后只 settle，不重新生成。

### 14.4 重放与兼容

- 用含旧 `current-self-state.1`、旧 expression wire、旧 terminal outcome 的生产库副本冷重放；
- 比较 V2 event count、head cursor/hash、Dialogue、Affect、Relationship、Memory、Life、NPC、
  Media、Perception 和 Action Projection；
- 证明 cold replay 零 live model calls；
- 新生产账本只出现新 contract identity；
- 重启前后同一未终结机会 effect-once，终态旧机会不重开。

### 14.5 体验与性能

- 使用真实 daemon/model 路径进行多轮对聊，不使用固定回复替代；
- 覆盖连续多气泡、角色主动插话/被打断、silent/later、Recall、旧情绪、NPC 余波、生活分享、
  多 Beat 与媒体机会；
- 观察是否仍像通用助手连续追问，是否能基于真实自身经历自然联想，而不设提问率规则；
- 测量 provider TTFT、首个 Beat、API 外开销、完整 turn 和平台 ACK；
- 长时间运行验证主动联系、Life 和 background experience 不失声、不重复调用且成本可控。

## 15. 发布步骤

1. 停止 daemon，保存当前 commit 并推送远端；
2. 运行 schema/production call graph guard、相关测试、完整测试、静态检查和双路 code review；
3. 在生产库副本执行 integrity check 与冷重放，记录 head/hash/projection 基线；
4. 确认旧 role 配置、factory 参数、runtime worker 和 import 已删除，legacy invocation 为零；
5. drain 旧进程的可见生成；保留已授权 Action，释放未完成旧模型 lease；
6. 一次性部署新生产 composition，不启用旧接口 feature flag；
7. 重启后验证首次恢复、QQ 收发、主动 consideration、Life/experience、Action 到期、媒体/感知
   入口与一次重启恢复周期；
8. 直接以用户视角与角色多轮对话，核对同批多消息、近期记忆、情绪即时性、自然语气、首条
   延迟和打断；
9. 观察 health 中 snapshot、inner turn、旧接口、重复作者、失败与延迟指标；
10. 验收后提交并推送本轮迁移；异常时回滚到已归档 commit，不运行新旧混合模式。

## 16. 完成定义

本迁移只有同时满足以下条件才算完成：

- 八项 Faculty 均进入 canonical snapshot 与相应 decision/transition seam；
- 普通入站、主动联系、Life、Appraisal/Affect、关系/私人印象、媒体和感知不再直接调用角色
  模型或组装角色自我；
- 新生产只有 `project/experience/consider` 三个入口，旧生产接口、参数和 worker 已删除；
- 同一机会不会出现旧接口、新接口、并行旁路或双写；
- 历史账本冷重放等价，已授权 Action 不重复；
- 技术失败可见、可恢复且不伪装角色决定；
- 完整测试、静态架构 guard、代码审查、真实 daemon 对聊与重启恢复通过；
- 性能、主动性、情绪即时性和长期连续性以真实样本验证，而不是只用固定模型测试宣称正常。

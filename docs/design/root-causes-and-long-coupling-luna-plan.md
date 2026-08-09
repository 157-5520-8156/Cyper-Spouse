# Luna 执行计划：因果基底、统一内心与长程涌现生产化

状态：进行中（L0 已完成；L1–L3 仍为 partial。L2 durable turn 主体与 L3 第一批阻断修复已落地；
2026-08-08 审计发现的 world-stimulus terminal recovery 饥饿 P1 已用公共 `drain_one` 红绿回归闭合，
但逐 purpose 资格、冷重放、真实自由对聊、延迟触发全覆盖和 24 小时 soak 尚未闭合）。本文是施工顺序、验证门槛和
交接契约，不替代架构设计。

执行者：Luna；架构复核：Sol；建立：2026-08-08。

唯一业务与架构基线：[`girl-agent-design-intent.md`](./girl-agent-design-intent.md)。

## 0. Luna 的唯一北极星、文档权威与执行契约

### 0.1 最终要交付什么

一句话目标：**把 Girl-Agent 交付成生产中真实运行的自主个体——世界、记忆、关系、情绪、NPC、
感知、愿望、计划、行动与表达能够相互产生有来源的影响，而所有主观反应仍由角色自己决定。**

Luna 不以“实现了多少 Module”完成任务，而以以下最终结果完成：

1. 用户消息持久接收且整批可见，不再只看最后一句；技术故障失声 <0.1%，角色选择沉默可审计。
2. CharacterInterior 是主角唯一主观作者；没有业务旁路、宿主语义补写、备用人格或新旧 wire 混用。
3. accepted 的用户互动、Life、NPC、Memory、Relationship/Affect、Perception、Plan/Action、Expression/
   Media consequence 能为正确 actor 打开下一次有界机会，形成双向而非单向装饰性连接。
4. Reflection→Aspiration→Choice→Plan→Activity/Action→Outcome 可自然到达，但任何一步都可由角色合法
   忽略、拒绝、改变主意或终止；系统没有行为、动机和剧情枚举。
5. 每个 model-bearing purpose 首次 boundary-admissible 目标 99.9%；不靠重试后成功、本地默认值或
   语义 fallback 美化数据。
6. 非 API 主路径 p95 ≤500ms，Fast Reply p50 1–2s、首 Beat ≤2s；QQ Action、重启和并发 effect-once。
7. 事件驱动、机会合并和 actor 分级把 token、空闲写入与账本增长控制在预算内，不以削弱自主性省钱。
8. 真实 daemon/DeepSeek/QQ、多轮对聊、多日模拟、冷重放和生产 health 共同证明，而非只看测试绿色。

若一项工作不能明确推进上述至少一条，且不是修复其硬阻断，就不进入本轮。

### 0.1.1 给 Luna 的无歧义解释

这不是“实现设计稿中所有名词”的任务。Luna 应按以下优先级解释任何冲突：

1. 用户得到稳定、自然、连续的虚拟恋人体验；
2. 角色作者边界与事实/隐私/Action/effect-once 等硬不变量；
3. 可靠性、延迟、token、存储和可运维预算；
4. 最低充分因果内核与现有深 Module；
5. 本文的候选机制、Interface 草图和场景示例。

后项不得推翻前项。场景 S1–S26 是体验验收样本，不是二十六条 workflow；§11.8 的联动是因果能力空间，
不是十九个新 Module；`EmbodiedContext`、`ActorEpistemicView`、`RevisableSelfNarrative` 是需要生产证据才能
成立的候选 seam，不是必建类。文本里的“可以”“候选”“必要时”“若通过删除测试”都不能被解释为默认
上线要求。

Luna 的目标是**删除旁路并深化少数 Module**，不是保持现有全部复杂度后继续加层。每做一个工作包都必须
给出：它消除了哪个用户失败、替换并删除了哪些旧路径、在线调用图增加还是减少、最坏 token/延迟/存储
放大是多少。不能回答时先不实现。

已确认的产品口径不可自行改写：当前是一个用户对应一个 Dedicated Companion/World；未来多用户仍是一人
一专属角色，不共享私人状态。QQ 不是 World Administration 入口，未来面板负责状态展示与管理。技术故障
通过独立 System Notice 明确告知。外部能力只允许总纲已列 allowlist，未设计能力保持不存在。伴侣产品需要
从现在设计亲密/成人能力，但关系阶段和用户要求都不能替代 Adult Eligibility、双方同意或角色当前选择。

### 0.2 交接包严格只有两份文件

Luna 的完整交接包只有：

1. `docs/design/girl-agent-design-intent.md`：业务需求、产品体验、架构意图、机制状态、S1–S26、
   深度联动、不变量与 SLO；
2. 本文：施工顺序、技术门槛、测试、迁移、生产验证与交付模板。

仓库级 `AGENTS.md`、`CONTEXT.md` 等规则由 agent 按项目要求自动遵守，不属于需要从中拼业务需求的
交接材料。其他 `docs/**/*.md` 全部是历史或专题参考，Luna **无需阅读**，不能直接产生任务，也不能
覆盖这两份交接文件。若代码注释引用旧文档，优先依据本总纲判断业务意图，再用当前代码、production
composition、账本和 health 判断实现事实。

特别规则：

- “文件存在”“以前标为 active”“测试还在”都不证明生产有效；真实 producer/consumer、composition、
  health 与账本证据缺一不可。
- 旧文档列出的 TODO 不是 Luna 的待办；其中仍有效的需求已经并入设计总纲。
- 当前代码若与设计总纲冲突，先判断是代码回归还是总纲遗漏；不得采用旧文档中更方便的行为规则。
- 不再新增总纲、状态表或并列架构计划。业务信息只补入设计总纲，施工证据只补入本文。

### 0.3 分阶段阅读，避免上下文污染

开始 L0 前完整阅读两份交接文件。之后直接检查当前代码、git diff、production composition、账本与 health，
不要预先加载其他专题设计。需要业内方案时查官方文档或原始论文；需要理解旧事件/replay 时才局部打开
被代码直接引用的历史材料，且不得把它升级为当前产品需求。

DeepSeek 路线图中的有效实验结论、阶段性错误和最终裁决已经并入设计总纲 §11.13，Luna 不需要再读
路线图原文。

### 0.4 Luna 的执行授权

Luna 被授权在实现中发现新问题、检索业内办法并解决，但有以下边界：

- 优先查官方文档、原始论文和供应商协议；研究来源、备选方案、实验与裁决写入本文对应工作包的
  执行记录，不创建 `docs/research/` 或第三份架构/状态文档。
- 可自行修复局部契约、可靠性、性能、迁移和测试问题；若需要新增 authority、改变角色语义归属、
  放宽事实/权限边界或改写不可变历史，暂停该工作包，交给 Sol/用户复核。
- 不把模型非法输出改写成 `now`、`silent`、`no_change`、固定情绪、固定行动或本地自然语言。
- 不建立新旧并行路径。每次迁移必须列出 producer、consumer、旧入口、删除条件和架构 guard。
- 不在另一个 agent 正编辑同一文件时修改它。先取得当前 checkpoint、确认文件所有权并保存基线 diff。
- 不以“测试绿”声明生产完成；每个生产能力都需要真实 provider、真实 daemon、重启和账本证据。

### 0.5 获取“最优可行解”的强制协议

本协议用于新 Module/seam、跨业务联动、可靠性根因、模型 contract、性能主路径、存储/调度迁移和任何
会影响生产行为的改动。机械重命名、明确的小修和已有裁决的等价实现可以简化，但必须说明为什么不需要
方案比较。Luna 不得以 token、时间或“当前测试已绿”为由跳过。

#### 阶段 A：先证明问题，而不是先写方案

每个工作包先记录：

- 用户可观察的失败和期望体验；对应设计总纲条款；
- 当前生产调用链、owner、authority、producer/consumer、旧旁路和真实数据；
- 最小可重复反例、production trace 或隔离重放；没有复现时标记假设，不能把猜测写成根因；
- 当前基线：正确性、first-attempt 四层、p50/p95、token/call、账本写入、恢复时间和失败分布；
- 硬不变量、可接受预算、必须保留的复杂性，以及明确不在范围内的事项。

如果问题来自外部 provider、协议、模型、框架或近期业内能力，必须联网核对当前官方资料；技术结论优先
引用供应商文档、标准、原始论文和项目源代码。博客、二手总结只能用于发现线索，不能单独支撑裁决。

#### 阶段 B：Design It Twice，产生真正不同的候选

高影响工作至少提出三个实质不同候选，不能先写完一个再补两个陪跑方案：

1. **深化现有 Module**：迁移分散逻辑到已有 seam，最小化 Interface，删除旁路；
2. **重新放置 seam 或替换实现**：以不同 authority/Projection/sidecar/adapter 组织依赖；
3. **删除偶然复杂度或采用成熟机制**：证明哪些状态可派生、哪些可交给 provider 原生 contract、哪些根本
   不应成为 Module。

有并行 agent 能力时，至少三个独立 agent 在没有看见彼此答案的情况下分别设计：最小 Interface、最大
扩展性、最常见调用方最简单；涉及远程依赖时再增加 ports/adapters 候选。若不能并行，Luna 也必须分开
书写三案并进行反方审查。每案必须给出：完整 Interface（含不变量、顺序、错误、性能）、调用示例、隐藏
的复杂性、依赖/adapter、迁移删除路径、失败模式、测试 seam 和运维代价。

候选若只是 prompt 改写、字段默认值、增加 retry、局部缓存或再包一层 class，除非它解决了不同根因，
不得算独立方案。

#### 阶段 C：先过否决门，再作证据化比较

以下任一成立立即否决，不进入加权比较：

- 宿主替角色生成 motive、meaning、emotion、timing、silence、wording 或 choice；
- actor/private evidence 泄漏，或 exposure 被当作理解/相信；
- 技术失败被记成角色 no-change/silent/busy；
- 破坏 source closure、typed authority、CAS、effect-once、receipt、replay 或 immutable history；
- 新旧生产路径并行而没有同交付迁移和删除条件；
- 只能靠固定剧本、关键词、行为比例或测试专用 fake 达成验收。

通过否决门后，用同一 workload 比较，至少覆盖：

| 维度 | 必须回答的问题 |
| --- | --- |
| 用户体验真实性 | 是否解决原始失败；多种角色选择/no-change 是否仍可达；有没有助手化或行为收窄 |
| 正确性与来源 | authority/actor/privacy/source/epistemic scope 是否闭合；反证和撤回如何工作 |
| 深度与演进 | Interface 是否小而有 leverage；删除后复杂度会否散回多个调用方；是否制造浅层 pass-through |
| 可靠性 | 首次 structural/source/cross-field/accepted 成功率；超时、崩溃、并发和重启如何终结 |
| 实时性 | provider TTFT、非 API p50/p95、首 Beat、锁/CAS/IO/序列化开销 |
| 成本与存储 | 平均/尾部 token、调用频率、空闲写入、24h 数据增长、NPC/后台放大系数 |
| 迁移与运维 | 冷重放、旧路径删除、kill switch、rollback、health、lineage、故障定位难度 |
| 证据强度 | 结论来自静态推断、测试、隔离真实 provider，还是生产/多日 soak；样本量是否足够 |

硬门不能被总分抵消。其余维度记录测量值、置信度和 trade-off，不允许只写“较好/简单/灵活”。推荐案
必须说明为什么另两案在本项目约束下更弱，也要记录推荐案最可能失败的地方。

#### 阶段 D：先做最便宜的判别实验

对候选间最大的未知量设计最小 prototype/qualification，不直接大改生产。优先实验能让一个或多个候选
被淘汰的问题，例如：provider 是否稳定支持 required tool call、两种 capsule 编译的 p95、sidecar 在双连接
claim 下是否 effect-once、真实模型能否在不加行为提示的情况下达到来源闭包。

实验必须使用共同 fixture/workload，保存原始失败样本和分层指标。若实验不能区分候选，说明问题设计
无效，需要重写；不能挑选对首选方案有利的单一样例。

#### 阶段 E：TDD 实现、对抗审查和迭代

1. 先为原始用户失败、硬不变量和候选特有风险写红测；Interface 是主要测试面，不透过 seam 锁死实现。
2. 实现最小闭环，同时迁 producer/consumer/composition/health，删除或隔离旧路径。
3. 分别进行 Standards、Spec、Agency/Authority 和 Production Readiness 审查。审查者必须主动寻找：隐藏
   行为规则、宿主语义补写、dead code、未接 producer、跨 actor 泄漏、缓存/replay 身份错误、资源放大和
   “测试名声称并发但实际只有一个 instance”等虚假覆盖。
4. 用真实 provider/daemon 运行失败场景与随机场景。达不到门槛时回到根因和候选，不允许只增加 retry、
   fallback 或放宽 schema 掩盖。
5. 每轮记录“假设→改动→测量→结论→下一步”。连续两轮没有改善时必须重新审视 seam/假设，不能继续
   在同一局部打补丁。

Luna 可以迭代任意轮，直到达到门槛或发现需要 Sol/用户裁决的真实 trade-off；“大差不差”不是终态。

#### 阶段 F：停止规则与生产资格

只有同时满足下列条件才可标完成：

- 原始用户失败在真实路径消失，且没有牺牲其他已声明业务行为；
- 本计划 §1 和对应工作包全部量化门槛达到；样本不足明确标 `qualification_incomplete`；
- 至少一个替代方案已被证据性淘汰，或证明当前实现位于可解释的 Pareto frontier；
- 生产者、消费者、composition、health、lineage、迁移和旧路径删除闭合；
- 真实 provider、daemon、重启、冷重放、QQ/Action 与规定时长 soak 通过；
- 没有已知 P0/P1，P2/P3 有明确影响、证据和后续处置；
- 独立审查者不能仅凭“测试绿”签字，必须核对生产证据和用户体验轨迹。

若最强方案仍未达到目标，状态是 `blocked_by_evidence` 或 `requires_product_tradeoff`，不是 `complete`。
不得静默降低 99.9%、延迟、成本、actor isolation 或体验目标。需要改变业务宗旨、authority、隐私、模型
作者或不可逆数据语义时，按 §20 交回 Sol/用户。

## 1. 发布指标与证据等级

### 1.1 模型一次成功的精确定义

每一种 model-bearing purpose 分别统计，禁止把不同目的混成一个总成功率：

- `transport_success`：请求成功且工具调用/结构化响应可读取；
- `structural_valid_first_attempt`：第一次结果符合 schema、必填字段和枚举；
- `source_admissible_first_attempt`：引用均存在、actor 可见且 claim scope 合法；
- `cross_field_valid_first_attempt`：时间、能力、位置、Action、状态转换互相一致；
- `accepted_first_attempt`：前三项均通过并被对应 typed authority 接受；
- `role_no_change`：模型合法地选择不形成变化；这是成功，不是失败；
- `technical_failure`：provider、解析、来源、存储或 deadline 失败；不得计为沉默。

目标是所有生产 model-bearing purpose 的 `accepted_first_attempt` 滚动达到 **99.9%**，并且协议形状
通过 forced function/tool calling 达到接近 100%。这不是靠重试后的最终成功率，也不包括由宿主补写
语义后“看似成功”的样本。

### 1.2 分级证明，避免虚假精确

1. **契约级**：每个 schema 做至少 10,000 个 property/fuzz/replay 样本，结构错误为零；覆盖历史坏
   输出、字段省略、未知字段、流式分片、断连和供应商错误。
2. **供应商资格级**：每个新 tool contract 至少 100 次真实、低风险、隔离库调用，结构错误为零；
   不足以统计证明 99.9% 时，health 明示 `qualification_incomplete`，不能声称已经证明。
3. **生产滚动级**：按 purpose 统计最近 1,000 次；少于 1,000 次显示样本数和置信不足。出现一次
   schema/source/cross-field 失败立即保留请求指纹、精确错误和 provider response metadata，进入修复队列。
4. **语义级**：不能用固定答案测“像人”。验证来源闭包、选择空间、链条可终止、多种结果可达，
   再由真实对聊和多日运行审阅自然度。

### 1.3 其他发布门槛

- `girl-agent-design-intent.md` §10 的 V1–V4、R1–R4、Q1–Q5、C1–C3 是基线指标；本计划按用户
  最新要求把 Q1/Q2 的 95% 提升为各 model purpose 的 first-attempt 99.9% 目标，并把故障失声率
  目标提升到 <0.1%。旧目标不得被 health 继续展示成“已达标”。
- 同一 actor/source set/purpose/epoch 只产生一次 accepted effect；并发、CAS、重启和缓存淘汰均成立。
- 非 API 同步路径 p95 不高于 500ms；首条 Beat 的延迟单列 provider TTFT、内部编译、审查和 dispatch。
- scheduler 空闲写入、模型调用和数据库增长保持既有账本瘦身目标。
- 每个 actor、purpose、来源和后果可从只读 causal lineage 查到；技术失败不能长期停在“角色沉默”。
- 所有新能力具有 kill switch、回滚方案和旧路径删除证明，但 kill switch 不能成为永久旁路。

### 1.4 月度 ¥100 按量调用成本目标

- 单个生产实例所有 API 类按量调用成本以 **≤¥100/月** 为设计目标；角色/World/NPC/reviewer/embedding/
  media/vision/audio/付费 perception/search 与真实 provider qualification 共用同一总账，禁止每个子系统
  各算一份预算。订阅、服务器、本机折旧和电费暂不计入该口径。
- 以设计总纲 §10.4 的 `60/15/8/8/3/6` 作为初始规划信封，不是行为频率或必须花完的配额。Luna 可根据
  production workload 调整；允许真实高互动、故障调查或必要资格化临时超支，但必须有 purpose/provider
  归因，不能以超支为常态，也不能挪空可见对话保护额。
- 每次远程消费先做跨进程原子 reservation，再按真实 usage settlement；覆盖并发、超时、取消、重启、
  provider 迟到和账单修正。现有 `budget.py` 只有在所有生产入口都经过它时才算交付。
- Health 必须显示 settled、reserved、24h burn、month-end forecast、category share、visible-chat reserve 和
  load-shed level。预测到 ¥70/¥80/¥90 分别告警、停止重复资格化/低价值媒体、降低低显著度后台丰富度；
  预测或实际超过 ¥100 时保留可见聊天，告警并输出可执行的成本归因与优化建议，不自动停机。
- 降载优先减少实验、渲染、低显著度 NPC/ambient/perception opportunity；不得减少当前 turn 必需事实、
  改写角色选择、使用本地人格 fallback、仅因超过目标拒绝可见回复，或把预算压力伪装成 silent。

### 1.5 延迟触发全覆盖与上游受控注入

所有依赖未来时间的生产机制必须进入一份由代码版本化维护、由 CI 校验双向一致性的
`Delayed Trigger Qualification Matrix`。Matrix 是规范来源；scheduler contract、vertical registry、Action
kind、retry policy、Projection due/expiry 字段和 `mechanism_closure.yaml` 必须与它互相核对。自动扫描只用于
提出候选和发现差异，不能被宣称为完整真相；每个差异必须人工审计并留下理由。新增延迟机制若未登记，
架构门直接失败。
资格 harness 只能经过 production QQ host 已公开的 `inbound`、`tick`、`drain`、正式 `receipt` seam，以及
正式外部感知 provider injection seam；可见外发用 production transport interceptor 捕获 provider accepted、
迟到/丢失/unknown 与最终 receipt。不得调用任何 `*_worker.drain_one()`、`advance_once()` 或私有 runtime 方法。
Clock 同批派生的 Goal expiry、Occurrence activation/expiry、Affect decay、DeferredReply 等责任必须从
一次真实 tick 一并观察，不能拆成若干“各自直调都成功”的假闭环。

每一行至少声明：

| 字段 | 要求 |
| --- | --- |
| mechanism / purpose | 稳定机制身份；涉及模型时写精确 model purpose 与 contract/schema identity |
| upstream authority | 生产中真实打开机会的 accepted event、Observation、Clock/expiry 或 authorized Action |
| due identity | actor、world、source set、epoch、retry ordinal、logical deadline 与 merge/dedup key |
| controlled injection | 仅在隔离生产副本提交真实上游材料并推进 Logical Time；禁止直接调用下游 worker |
| expected path | scheduler discovery → claim/CAS → semantic author（若有）→ typed authority → Action/receipt |
| legal terminals | accepted、角色 no-change/silent/later、superseded/cancelled、技术失败/retry 等精确终态 |
| visible effect | 是否可能产生 QQ/媒体/其他外部 effect；测试时用真实 transport seam 拦截并保存 receipt |
| observability | health、causal lineage、ModelResult、usage/cost、latency、ledger/sidecar refs |
| fault matrix | provider timeout/invalid、CAS 冲突、重启、lease 过期、迟到结果、撤回、权限变化、坏 trigger 饥饿 |

当前 release 中已启用机制的最低覆盖包括：ambient/event-driven proactive、silent/later 后续考虑、10/30/120 技术退避、Life/NPC
Ecology、Reflection、MemoryCandidate/巩固、authorized Action 精确到期、deferred reply、Expression 多 Beat、
被打断后的 reconsideration、Commitment/Thread/Expectation expiry、Activity/Plan/Outcome 生命周期、Affect
衰减与 silence aftermath，以及后续新登记并在该 release 启用的任何延迟机制。Perception
refresh/attention、Media planning/render/inspection/delivery 等 limited-production/dormant 能力只在声明启用的
release 做完整资格；未启用时只验证关闭不阻断核心及 replay/compatibility，不得为凑表激活生产 producer。
是否产生消息仍由角色决定；合法 silent/no-change 是语义成功，不是发送失败。
只有 schema/head 而没有生产 producer 的 dormant 机制（例如当前 V2 Goal）只做 replay/compatibility 资格，
不得借手工注入升级为生产 `[active]`。

资格分成不能互相替代的两条证据：

1. **虚拟 Logical Time**：穷举边界前/边界/边界后、时区/DST、批量 merge、同 deadline 并发、重启、CAS、
   retry ordinal 和迟到回执。确定性 discovery、claim、terminal、effect-once 与“不饿死其他 trigger”必须
   100% 通过。
2. **真实 wall-clock daemon soak**：真实 provider、真实 scheduler、QQ/Action transport、进程重启和至少
   24 小时运行；证明没有仅在 monkeypatch/加速时钟下成立的实现。

模型样本仍按 purpose 分账：100 次零结构错误只授予初始供应商资格；最近 1,000 次用于滚动观测；若要在
约 95% 置信水平下以零失败支持失败率低于 0.1%，需要约 3,000 个独立样本。样本不足时必须显示
`qualification_incomplete`，不能写“接近 100%”。`accepted_first_attempt`、纠正后接受、最终 Action delivered
和角色 no-change/silent 分列；技术失败、source/cross-field 拒绝与 provider outage 保留精确归因。

资格运行使用隔离库、可审计真实 provider 与统一月度资格预算；允许为必要资格化有依据地临时超过 ¥100
设计目标，但必须提前估算、逐 purpose 记账，并在达到既定样本或发现系统性失败后立即停止无价值重复。

## 2. 总体顺序

L0→L15 是依赖顺序和完整愿景，**不是要求一次大爆炸式改完才允许上线**。采用三个可独立回滚、每阶段
都能生产运行的 release train：

1. **稳定内核发布**：L0–L6，加上 L9/L10 中维持当前对话、记忆、Action 所必需的切片，随即执行
   L14/L15。目标是失声、重复、只看最后一句、慢回复、同轮情绪和主观旁路先消失。
2. **生活连续性发布**：在稳定内核 soak 达标后实施 L7–L10 的长链；每条链独立开关、独立预算，不改变
   Fast Reply 的调用图。
3. **生态丰富度发布**：再实施 L11–L13 的 NPC、感知、主动联系、媒体增强；逐项资格化，收益不足、成本
   失控或没有真实 consumer 的能力保持关闭或删除。

每个阶段都运行 L14 可观测与 L15 迁移发布门。前一阶段生产指标未稳定，不扩大下一阶段模型调用面。
工作包可在文件互不重叠时并行，但同一 accepted source 的 producer/consumer/health 必须在一个交付中闭合。

任何阶段都不得为了“完整度”延后明显改善用户体验且可以安全发布的稳定内核；也不得为了提前上线跳过
硬不变量。默认关闭的丰富度 adapter 不能成为聊天、记忆主干或 Action settlement 的必需依赖。

每个工作包都必须留下：红测、最小实现、相关测试、全量测试结果、静态检查、生产证据、剩余缺口、
精确 commit。不得把未接入的文件或只收集的测试称为完成。

### 2.0A Sol 主控与低成本执行 agent 的无人值守契约

无人值守的含义是“在已批准 release 与硬边界内自动迭代到证据门或真实决策门”，不是允许一个模型从
L0 一路自行改变业务并自动上线。默认职责如下：

- **Sol / 架构主控**：维护本文与设计总纲，定义 release scope、authority/actor/privacy 边界、验收指标，
  审核跨 Module seam、解释真实体验失败并签署阶段门；不承担所有机械实现。
- **低成本执行 agent（Luna 或 Sol/用户批准的同类模型）**：在固定工作包内做代码盘点、TDD、迁移、
  资格样本、故障注入、性能测量、文档证据与小步提交；不能自行改写业务目标或把未达标项降级。当前
  orchestrator 若无法选择经批准的模型，必须记录能力限制并交回选择，不能静默换模型。
- **独立审查 agent**：保持只读，分别审 Standards、Spec、Agency/Authority、Production Readiness；不能由
  写作者给自己签字。最终产品体验仍由 Sol/用户抽查真实 transcript。

并行规则：同一时刻每个文件/authority seam 只有一个写 owner；其他 agent 做只读研究、测试设计、日志分析
或不重叠文件。共享工作树下不得让多个 agent 同时格式化、批量重写、迁移同一事件族或提交彼此未审的
混合 diff。并发上限按运行时实际 slot、工作树/数据库隔离能力和成本信封确定，始终为 Sol/写 owner 保留
必要容量；agent 完成后复用 slot，不能为了并发把一个因果闭环拆成互相不可验收的碎片。

自动继续条件：工作包边界和 producer→consumer 已批准；红测能复现真实失败；不新增 authority、不改变
隐私/同意/Action/角色选择；改动只在隔离库或明确 canary；成本预测在批准信封内。满足时执行 agent 应持续
完成“调查→候选方案→判别实验→TDD→相关/全量测试→真实证据→独立审查→小步 commit/push”，无需用户
守在电脑前。

强制停止并交回 Sol/用户：本文 §20 任一事项、发现规格自相矛盾、需要删除/重写不可变数据、要启用新的
生产媒体/感知/成人能力、无法证明 source/actor scope、连续两轮同类补丁无改善、成本预测显著超出已批准
资格预算、存在 P0/P1、或准备进行最终生产替换。停止时必须留下可恢复 checkpoint、失败证据和至少两个
可选方案，不能留下半迁移生产状态。

无人值守 release 的最大默认范围是当前阶段：先闭合 L1–L3 与 §1.5，再执行稳定内核所需 L4–L6 和必要
L9/L10 切片；每个切片通过 L14/L15 的自动门。24 小时 soak 与真实对聊可无人值守采证，但最终 production
资格、真实 QQ 用户体验和扩大到下一条 release train 仍需 Sol/用户签署。一个坏 trigger、测试绿或单次消息
送达都不能触发自动“完成/上线”。

### 2.0 生产复杂度预算

Luna 在 L0 画出当前生产调用图，并在每个 release 后更新。目标不是限定代码行数，而是限制用户请求经过
的 Interface、远程调用、写放大和故障传播：

- Fast Reply 在首 Beat 前只允许一个角色模型请求；本地召回和 Context 编译必须有严格 deadline，远程
  embedding miss 直接使用已有本地候选，不等待后台补齐。
- 非模型在线工作 p95 ≤500ms；分别记录 ingress/落账、Context/recall、acceptance、dispatch，禁止只报总数。
- 同一 actor 最多一个可见生成、一个互斥 foreground physical activity；后台 cognition 不持有可见路径锁。
- 一个 source 的后台机会先 merge/dedup，再按 actor/purpose 调用；禁止全 NPC、全 Projection 或全历史扇出。
- 无新事实的 poll 不写语义事件；claim/checkpoint/retry 等高频运行状态优先放可清理 durable sidecar，只有
  会影响 replay 事实的接受结果进入不可变账本。
- 新 Module 必须有至少两个真实 consumer，或独占一项硬不变量；否则深化现有 Module、内联或删除。
- 新远程模型/adapter 必须证明现有模型不可满足不同 workload，且有独立 token、并发、timeout、circuit、
  health 与 kill switch；不得因为 schema 不稳就叠加第二模型。
- 任一丰富度能力关闭时，Fast Reply、基础 Recall、Relationship continuity、Action settlement 和 daemon
  recovery 仍须工作；否则说明 seam 放错了。

若新方案让用户可见主路径多一个串行模型调用、让一次事件无界扇出 actor、让失败跨 Module 级联，或让
不可变账本持续记录 no-op，它必须先被否决，除非用户明确接受量化 trade-off。

### 2.1 深联动状态基线与施工归属

设计总纲 §11.8.1 是这些联动的唯一状态表。Luna 在 L0 逐项复核并原位更新标签，不再复制出第三份
“最新版”。以下是施工归属，不代表要求每行新建一个 Module：

| 联动组 | 交接基线 | 主工作包 | 退出条件 |
| --- | --- | --- | --- |
| 具身/外观/可用性 | [disconnected] | L4、L5、L9、L13 | 真实 Life/Activity producer → source-bound reading → Interior/Plan/Media 至少两个 consumer；手工 record 不算 |
| 自我叙事/信念/想象 | [design-only]/[partial] | L5、L7、L8、L10 | Core 与 self-narrative 分层；belief/counterfactual actor scope；Reflection/Recall/Expression 消费且不污染事实 |
| 技能/习惯/路径依赖/不可逆结果 | [partial] | L8、L9、L10 | outcome evidence 可改变开放 capability/candidate；无 XP 自动升级、无无源复活；后续事件真实读取变化 |
| 承诺冲突/自我调节/修复 | [partial]/[must-not-build] | L4、L6、L9 | 冲突完整进入 Interior，角色多种选择可达，结算后果回流；不存在行为矩阵或专用宽恕引擎 |
| 物品/地点连续性 | [partial] | L9、L10、L13 | stable entity/custody/location/condition 可从 outcome 延续到 recall、life 和 media，图片不建立第二真相 |
| Disclosure/Actor Epistemic | [partial] | L4、L5、L10、L11、L13 | visible evidence、exposure、adopted belief、disclosure intention 分开；跨 actor 泄漏为零 |
| NPC–NPC 网络/多视角声誉 | [partial] | L4、L11 | actor-scoped 关系/印象、真实通信传播、离开重现和低成本分级；没有全局声誉值或全员扫模型 |
| 时间标记/用户沟通习惯 | [partial] | L4、L10、L13 | 日期/节奏只改变候选机会；有界、可重建/可清理、重启语义明确，不替角色决定联系或插话 |
| 兴趣/共同文化 | [partial] | L10、L12、L13 | 来源化、relationship-scoped、可修订；可影响候选但不形成关键词行为规则 |
| Consent/Authorization | [active] | L13、L14 回归 | 撤回阻断未 dispatch effect，所有媒体/Action 重检，历史与角色主观 aftermath 正确分离 |

实施纪律：

1. 先用生产调用链证明当前标签；若发现现有完整闭环，补证据并升级，不重复实现。
2. `[disconnected]` 优先迁真实 producer/consumer，不先丰富 schema；`[design-only]` 先做 authority 与删除测试。
3. `[must-not-build]` 禁止创建同名语义 author；只修组成链的上下文、机会、选择、结算和回流。
4. 候选 `EmbodiedContext`、`ActorEpistemicView`、`RevisableSelfNarrative` 只有在至少两个真实 consumer
   且删除后复杂度会散回调用方时才建；Interface 与验收按设计总纲 §11.8.3–§11.8.5。
5. 同一联动的 producer、consumer、composition、health、迁移和旧路径删除必须在一个可回滚工作包闭合；
   不接受“先放一个未来会用的 runtime”。
6. Numen 只作为工程形状的参考证据，不是产品方向：不得增加 Minecraft/GameSession/MCP body、游戏实体、
   主人命令循环或未来游戏接入任务。只实现设计总纲中已抽象出的 actor-centric reading、单 actor 物理动作
   互斥、受理/终局回执和结果回流；能由现有 Activity/Action/Projection 深化完成时不新建平行框架。

### 2.2 Luna 的联动闭环记录格式

Luna 在现有工作包记录中为每条联动附加以下字段；状态仍只回写设计总纲：

```text
联动：设计总纲 §11.8.1 的精确行名
基线状态：[active|partial|disconnected|design-only|must-not-build]
客观 producer：事件/sidecar、作者、source refs
derived reading / Projection：名称、cursor、actor/privacy、expiry
opportunity：purpose、merge、epoch、terminal
角色作者：主角 CharacterInterior / NPC actor / 无（纯硬边界）
accepted consequence：authority、CAS/effect-once
生产 consumer：至少一个；候选深 Module 至少两个
反向回流：Memory/Relationship/Affect/Life/candidate environment 中实际读取者
旧路径：删除、replay-only 或明确阻断条件
health：未触发/no-change/technical failure/backlog/unused consumer 的区分
证据：测试、真实 provider、daemon、账本事件/调用计数、延迟/token/增长
最终状态：只能按设计总纲 §11.8.5 升级
```

## 3. L0：冻结、盘点和基线

### 目标

把当前巨大 WIP、DeepSeek 正在做的 function calling 和生产状态整理成一个可交接的明确起点。

### 操作

- 查询现有 agent/Claude Code 会话，等待其到安全 checkpoint；记录它修改的文件和测试。先审查并复用
  `src/companion_daemon/llm.py` 已有的 provider tool-calling transport plumbing，但不得把它误记为已经资格化：
  单一 forced tool、精确 tool identity、流式 arguments、usage、错误路径以及 contract/version/schema hash
  进入 request identity 都必须分别取得证据后，才能把相应 purpose 标成 active。
- 保存 `git status --short`、当前 HEAD、未跟踪文件、daemon PID/config、数据库副本和 health 快照。
- 对所有设计中的能力建立矩阵：authority、真实 producer、真实 consumer、runtime composition、health、
  单测、集成测、生产事件数。缺任一项标为 WIP，不用“代码存在”推断已运行。
- 以设计总纲 §0.2 的业务→机制表为主索引，再用
  `[active]/[partial]/[disconnected]/[design-only]/[must-not-build]/[abandoned]` 与 S1–S26
  展开证据，建立 `business outcome → mechanism → current code → production evidence → verdict → Luna
  work package` 对照；状态发生变化时更新原文，不能再创建第三份互相矛盾的状态清单。
- 按设计总纲 §11.13 检查阶段性修复的当前代码状态，标注 `retain / replace / remove / needs evidence`；
  以语义等价性和生产证据判断，不按“是谁写的”归因正确性。
- 记录已知基线：reflection open/claimed/terminal、embedding service/launchd、CharacterInterior turn store、
  每种模型 purpose 的一次成功率、Action/QQ 投递、账本增长。
- 盘点所有未带 `world_id/actor_ref/user_ref` 的 cache、sidecar、budget、Action、provider result 和 health；
  当前保持单用户部署，不实现多租户控制面，但修复会导致未来专属角色之间串状态的进程级全局假设。
- 盘点 `ConsentGranted/Revised/Revoked`、Privacy、Capability、relationship/P3 media、成人模板、年龄来源和
  provider route：明确哪些只是操作授权，哪些真的证明 Adult Eligibility/Intimate Consent，禁止把配置文件中
  的 `adult` 字样、lover stage 或角色出生日期单独当作完整成人授权。
- 先修复会阻断 import/collection 的 WIP 语法和 catalog 错误，但不得趁机改变业务语义。

### 验收

- 一个干净可复现的 checkpoint；用户无关修改被保留且未误纳入。
- 全量测试、ruff、`git diff --check` 的失败清单精确归属到工作包。
- `mechanism_closure` 或等价清单不再把 dormant authority 说成 active。
- 设计总纲、`mechanism_closure` 和实际生产对同一能力的状态没有未解释冲突。

## 4. L1：用 Function Calling 固化协议，不固化角色行为

### 目标

让 provider 在第一次调用就返回可解析、边界可审查的 typed proposal，同时所有语义字段仍由角色模型
填写。先消费并审查 DeepSeek 当前 function-calling checkpoint，再补缺口。

### 实现要求

- `src/companion_daemon/llm.py` 的同步、异步、流式和非流式接口共同支持 provider 原生
  `tools/tool_choice`；流式能正确组装增量 arguments、finish reason、usage 和错误。
- tool identity/version/schema hash 进入 request identity、缓存和 replay fixture，避免契约变更复用旧结果。
- 各 purpose 暴露最小且语义完整的 tool contract。表达至少显式拥有 timing/withhold/beat intention，
  不允许省略 `timing_choice` 后宿主默认为 `now`。
- `StructuredRole` 只做 lossless envelope normalization；不得从正文推导私人 summary、attended refs、
  motive、emotion、location、decision 或 capability evidence。
- provider 明确不支持 required tool choice 时视为 qualification failure；不能悄悄退回自由文本生产路径。
- 同一模型可获得一次精确的 schema/source/cross-field 纠正；纠正后仍失败为技术失败并进入既有 retry，
  不创建替代人格作者。
- prompt 只说明世界、证据、能力和硬边界，不给“通常应该问问题/分享/现在回答”等行为建议。

### 测试与验收

- 覆盖 DeepSeek 官方接口的真实 tool call、流式碎片、空 arguments、多 tool call、断连和 usage。
- 回归：省略 timing、bare proposal、非法 source、跨 actor ref、location/ability 矛盾都不能被宿主语义补写。
- 合法 `silent/no_change/later` 与合法 `now` 同等接受；失败原因可观测。
- 完成 §1 的契约级和供应商资格级门槛后，才迁移一个 production purpose；逐个迁移，不做全局 big bang。

## 5. L2：接通 CharacterInterior Durable Turn Store

### 目标

把 CharacterInterior 从进程内 `_cache`/`asyncio.Lock` 升为跨连接、重启和并发可恢复的单作者 turn，
且不向 V2 immutable ledger 写高频 checkpoint 垃圾。

### 实现要求

- 审查现有 `character_interior/turn_store.py`：若保留，先修复同 owner 跨连接互斥、lease token/expiry、
  `prune` 缺少 `world_id`、事务和 schema migration；若接口不合适，删除后以同一 deep seam 重建。
- sidecar key 至少包含 world、actor、purpose、source set、epoch、contract version；claim 使用唯一 lease token，
  不能仅凭 owner string 重入。
- 保存 pinned capsule hash、model request identity、阶段 checkpoint、accepted proposal refs、terminal/failure；
  终态可有界清理，审计依据仍由 immutable accepted events 提供。
- 在 daemon composition 创建/迁移表并注入 CharacterInterior；删除仅使用 process-local store 的生产路径。
- crash 恢复只能继续/加入同一 turn，绝不重新生成已经 accepted 或已经 dispatch 的 effect。

### 测试与验收

- 两个 SQLite connection、两个 runtime instance、lease 过期、owner collision、CAS、进程崩溃、缓存淘汰、
  prune world isolation、schema upgrade。
- “concurrent instances”测试必须真的创建两个 instance/store connection。
- 生产重启一次，证明 model call、proposal 和 Action 没有重复；health 暴露 open/stuck/recovered turn。

## 6. L3：清除当前生产阻断

### Reflection

- `AppraisalAccepted`、Affect、Memory、Life/NPC outcome 等已声明刺激必须被 source compiler 实际支持；
  unsupported source 不得令整个 scheduler tick 崩溃。
- open→claim→model/role no-change or proposals→accepted/rejected→terminal 全链闭合；失败有 retry 和上限，
  不能留下无限 open/claimed。
- 合并同一 aftermath window；历史积压不能在重启时无界补跑。

### Embedding

- 立即停止无法监听却持续重启的 launchd crash loop。
- 选择并验证 MLX safetensors、兼容本地 runtime 或受控远程 embedding；模型格式与 loader 必须匹配。
- service health 只有真实 bind、模型 warmup 和一次 probe 成功才为 healthy。
- embedding 不可用时 lexical/recent fallback 是检索能力降级，不得编造召回；health 明示降级。

### 工程门

- 修完全部当前 src ruff、测试收集和 EOF/diff-check 错误。
- scheduler 顶层记录单个 trigger failure 而继续处理其他独立 trigger，同时该 trigger 进入准确终态/retry。
- 验证 fast provider 是唯一当前生产角色接口；慢接口仅保留显式 disabled capability，不被隐式 fallback。

## 7. L4：实现 CausalOpportunityRuntime 深模块

### 目标与边界

建立一个统一的“accepted change 如何为有权 actor 打开考虑机会”的路由 seam。它不调用角色模型，
不写万能 CausalEvent，不决定反应，只负责 visibility、merge、dedup、epoch、due 和诊断。

先评估现有 `character_interior/world_stimulus.py`、trigger process 与各 scheduler 能否被深化成该
Module；优先在既有统一入口内收敛。只有其职责无法形成小接口时才新建 `CausalOpportunityRuntime`，
且必须在同一交付迁移 producer/consumer、删除原分散路由并加架构 guard，严禁新旧并行。

建议接口保持很小：

```python
advance_once(wake_event_ref) -> OpportunityAdvanceResult
health_snapshot(world_id) -> CausalOpportunityHealth
```

内部 typed registry 声明 accepted source contract 可打开哪些 actor/purpose，以及 merge window/expiry；
实际 consumer 仍是 CharacterInterior、NPC actor runtime、World Author 或 typed authority。

### 要求

- actor visibility 必须由来源/关系/实际 perception 决定，不由“相关性高”推断知道。
- identity = actor + purpose + canonical source set + epoch + contract version；新证据可开新 epoch，Clock 本身
  不能凭空重开同一语义。
- 同一窗口合并变化但保留全部 source refs；重要性只影响 due/candidate，不替 actor 选择。
- source registry 覆盖具身/可用性变化、承诺冲突、物品/地点变化、真实披露、NPC-NPC 可见通信、
  日历里程碑和 capability 变化；这些 source 只开相应 actor 的机会。
- opportunity 可合法 terminal 为 no-change/ignored/expired/accepted/failure；技术失败与角色 no-change 分开。
- 只读 lineage projection 展示 source→opportunity→turn→proposal→accepted consequence。

### 验收

- user、Life、NPC、Memory、Perception、Plan outcome 各至少一个真实 producer/consumer 集成测试。
- actor isolation、重启不重抽、并发 claim、merge、不重复、expiry、新 epoch 和历史 replay 均覆盖。
- 删除分散 scheduler 中等价的旁路后，架构 guard 阻止重新引入。

## 8. L5：完成统一 CharacterInterior

### 目标

所有主角主观语义只由一次 source-bound Interior Turn 形成，八项 faculty 是同一私人自我中的可选能力，
而不是八个互相不知道的模型。

### 要求

- 盘点 Appraisal、Affect、Attention、Recall adoption、Relationship stance、Aspiration/Choice、Expression、
  Action intention 的所有生产入口；迁入统一 seam 或明确证明其不是主观语义。
- Inner Life Snapshot 始终包含少量来源明确的当前活动/可用性、活跃情绪、关系、近期自身经历、
  当前计划/愿望、开放 thread/commitment、可选 recalled memories 和能力；按来源可加入具身处境、
  可修正 self-narrative、actor belief/uncertainty 及“谁已被告知什么”；含截断日志与 freshness。
- 若 `EmbodiedContext` 通过删除测试，则每个 pinned turn 只编译一次 actor-centric executable situation：
  当前地点/活动、可见附近人物与对象、身体/外观、现在/稍后/今日 affordance、资源/承诺、unknown/freshness
  和 source refs；由 Interior、Plan、Media 复用。不得复制整份投影，也不得输出行为、语气或情绪建议。
- Recall、提问、分享、沉默、插话、多 Beat 和主动联系都是模型选择，不设行为比例或问答矩阵。
- tool contract 允许模型只提出它确实形成的 typed proposals；缺席不由宿主补齐为 no-change。
- 每项 proposal 由对应 authority 独立审查；一个 proposal 失败不回滚其他已接受项，但 turn lineage 完整。
- 删除旧 wire、旧 prompt 和业务模块直接调用 role provider 的路径；replay adapter 只能读历史。

### 验收

- 架构扫描证明只有 CharacterInterior 可以生产主角主观 proposal。
- 同一输入多次隔离采样能出现合理多样性，同时每次来源/权限闭合。
- 对话全批 Observation 可见，不只取最后一句；同轮形成的 affect/stance 能影响本轮表达。

## 9. L6：即时反应与短期余波

### 即时链

`User Observation → Interior Turn → Appraisal/Affect/Relationship/Memory/Expression proposals → typed acceptance`
必须在一个 pinned cursor 上完成。Expression 可使用本轮已形成的私人状态；不要等下一轮才显现情绪。

### 余波链

- 用户迟迟未回复、消息被中断、主动消息送达未回应，只先成为客观 Observation/settlement。
- 它为角色开放 aftermath opportunity；角色可失望、担心、轻松、理解对方忙或无变化，不允许本地固定
  负面 delta，也不允许“未回复→减少主动联系”硬编码。
- 用户后来回复成为新证据，可修正此前 Appraisal；旧感受不被重写。
- 角色自己的已投递 Expression 与 Media receipt 可按重要性成为一次自我 aftermath opportunity，支持
  S9“分享后的期待”和 S10“说重话后的后悔/坚持”。它只能引用实际发送内容和回执，不得证明用户
  反应；同一 receipt 只开一次并设 merge/expiry，避免表达→自评→表达的无限循环。
- 活动/可用性可令角色延迟看手机，但消息仍持久保存；恢复后看完整批次。旧可见生成绑定 cursor，
  新消息使未 dispatch Beat 的推测失效；已 dispatch Beat 保持 effect-once。

### 验收

- 争吵、安慰、短句连发、长段分气泡、typing、抢话、被打断、延迟注意、重启各有真实 daemon 场景。
- 只验证可见来源和可达选择，不断言角色一定生气、插话或回复。

## 10. L7：Reflection 成为长链桥梁

- 只由 accepted、actor-visible 且具新证据/新时距的变化打开 reflection opportunity。
- 对应 S18 的长期独处只允许稀疏 ambient reflection opportunity；Clock 提供时间跨度，不提供“无聊”、
  “想用户”或行动动机。角色可以发呆、内省、找事做、主动联系或 no-change。
- 合并 10 分钟 aftermath，但重要大事件可有较早 due；随机只选机会时点，不决定反思结论。
- CharacterInterior 可输出 source-bound reinterpretation、Memory revision、Affect/Appraisal、关系理解、
  actor belief/self-narrative revision、Aspiration candidate 或 `no_change`。不规定每事件反思，也不固定
  反思次数；imagined/counterfactual/dream material 必须保持私有 epistemic namespace。
- 同一 source 没有新证据/显著时间跨度不得循环反思；新结果若成为真实变化，可按新 epoch 继续长链。
- 监测 backlog、age、claim、terminal、first-attempt success、token；按事件唤醒而非周期扫全历史。

验收至少包括：争吵后当日余波、隔日重新理解、NPC 谈心提供新证据、无变化合法终止、技术失败重试、
历史积压不风暴。

## 11. L8：Aspiration 与角色自主选择

- Aspiration 只能由 CharacterInterior 根据经历、价值、关系、记忆和长期处境自由形成；不用事件计数、
  年龄或关键词自动生成“创业/实习/旅行”等愿望。
- free-text motive/desired direction/uncertainty/commitment 均绑定 sources；允许矛盾、犹豫、淡化、放弃。
- 反事实设想可以帮助形成愿望，但不能证明未来能力或 World outcome；稳定 Character Core 与可修正
  self-narrative 分开进入上下文。
- producer、reducer、projection、consumer 必须同交付闭合；已有 dormant authority 先证明 producer verdict。
- aspiration 只为后续 choice 打开机会，不能自动创建 Plan；角色可不行动或改变主意。

验收用多种经历证明“形成/不形成/相反愿望”均可达，禁止测试固定愿望文案或发生率。

## 12. L9：Choice → Plan → Activity/Action → Outcome

- CharacterInterior 决定是否追求愿望、接受机会、取消/暂停/改向；World Author 只提供能力、环境、开放
  后果和不确定性。
- Plan authority 维护期限、前置条件、资源、地点、承诺、可中断状态；Action 仍遵守授权、receipt、CAS、
  effect-once。Life activity settlement 把真实执行结果写回经历。
- 同一 actor 同时最多一个互斥 foreground physical activity；普通机械步骤由确定性 executor 执行，模型只在
  语义选择、显著变化或失败需要重新选择时进入。长动作区分 accepted/claimed 与
  `success/failed/interrupted/timeout/unknown` 终局；打断、迟到结果、重启和未知结果都不得重复结算。
- 终局结果以 source-bound stimulus 回到同一 actor Interior；暂时不可见、未加载或来源过期只能得到
  `unknown`，不得确定性推导“不存在”，更不能替角色补写主观意义。
- 编译真实的时间/金钱/具身可用性/承诺冲突，让 CharacterInterior 自己选择优先、协商、取消或继续；
  不由 scheduler 排出道德优先级。Goal/Resource dormant authority 只有真实 producer+consumer 同交付才启用。
- 重复 Activity/Outcome 可形成 capability/skill/habit evidence，改变未来开放能力和候选环境；不能按次数或
  XP 自动升级。物品/地点的获得、借出、丢失、赠送和变化作为 World outcome 延续。
- irreversible outcome 明确关闭相应 capability/窗口并保留历史；不能由 reducer 为了继续剧情而复活。
- 不利突发事件、失败、NPC 冲突可改变客观可行性，但不能预定角色的心态或最终人生方向。
- location/season/calendar/ability 冲突交给同一 semantic author 做一次受约束重选；宿主不得清空 location
  却保留含该地点的 title/description。无合法修正就是技术失败。
- Life Arc 支持实习、搬家、毕业、工作、创业尝试等开放迁移，并联动作息、住处、能力、NPC 和未来
  事件分布；它是 accepted trajectory，不是固定剧情库。

验收覆盖：用户提店→后来角色自行 recall→可选择 Plan→实际去/没去/失败；实习引入新 NPC 与作息；
毕业后旧“上课”能力退出；承诺冲突由角色选择；长期练习可形成或不形成能力变化；物品连续；计划中断
和重启不重复外部效果。

## 13. L10：记忆、选择性召回与再巩固

- 完成 embedding 部署和 corpus 覆盖，索引用户事实、角色经历、NPC、关系事件、Life outcome、Perception、
  Thread/Commitment；每个 chunk 保留 authority/ref/actor/privacy/validity。
- 自动预取只提供少量候选；最终注意和采用由 CharacterInterior 决定。相似度、recency、affect cue 和
  unfinished thread 可混合召回，但不是行为建议。
- 角色在生活活动、NPC 互动和独处时也可获得 recall opportunity，不把记忆限制在对话 prompt。
- 再巩固保存“本次如何理解/感受”的新来源，不篡改原事实；支持淡化、冲突记忆、撤回事实和 privacy。
- 索引有意义的物品/地点、技能证据、披露记录和 temporal markers，使周年/季节/再次遇见可以提高候选
  相关性；日期与相似度不能强制角色记起。
- 共同昵称、内部笑话和关系仪式只能从重复的真实 shared interaction 中形成 revisable memory；一次出现
  不自动固化，Expression 可选择采用也可不用。
- 区分 World/User Fact、actor belief、companion expression record、counterfactual/dream；新反证可修改
  私人信念，但任何私人信念不能升级为事实。
- prospective memory/commitment 到期只开放机会，不强制提醒或主动消息。

验收包括下午事实晚间召回、角色自身经历口语化转述、错误相似记忆不采用、旧回复不能证明用户事实、
embedding 降级可见、撤回后不再进入新 capsule。

## 14. L11：低成本但自主的 NPC Ecology

- NPC 有 actor-bound 目标、日程、近况、对主角态度/记忆、关系阶段、所在生活环境和离开/重现条件；
  不能达到主角同等每轮成本，也不能只是主角的剧情道具。
- 确定性代码推进时间、资源、可用性和已授权结果；NPC 小模型只在开放语义选择时调用，可选择邀请、
  疏远、求助、误会、修复、无行动等，禁止规则直接选这些行为。
- 按变化和 due 唤醒，低重要 NPC 合并/降频/摘要；不每十分钟扫描全体。活跃 NPC 可用较强 contract，
  背景 NPC 仅在进入可见关系时提升精度。
- NPC 私人结果先进入其 actor scope；只有实际交流、共同活动或可见结果才能成为主角 stimulus。
- NPC-NPC 关系与通信也必须 actor-scoped；信息只有经过真实交流结果才传播。背景 NPC 使用确定性时间/
  资源推进和合并机会，不因任意世界事件逐个调用模型。
- 从 delivered communication 派生每个 actor 的 disclosure view，避免主角/NPC 忘记谁已经知道什么，
  同时不得把关系亲近当作自动披露授权。
- 不建立全局 reputation score；不同 NPC 仅依据各自 Observation/Disclosure 形成 actor-bound impression，
  主角只有通过真实交流才知道别人如何看她。
- 主角对 NPC 的行为反过来影响 NPC 记忆/态度；NPC 事件能影响主角情绪、计划和 Life，主角也可忽略。

验收：共同玩耍、旅行、争吵、谈心、邀请被拒、误会修复、人生阶段离开和后来重现；并记录每 NPC
月度 token/call budget、一次成功率及 actor 泄漏测试。

## 15. L12：外界感知与现实变化

- Perception Hub 管理国内新闻/社交来源、RSSHub、来源身份、时间、地域、可信度、重复、纠正和许可。
- “抓到”不等于角色“看到”；按角色习惯、渠道和注意机会形成 actual perception，才可进入 Interior。
- 禁止 topic→emotion/action/主动问候映射。地震、流行文化、天气等只成为所见信息，角色自己解释。
- 新闻不能证明用户位置、角色经历或 World outcome；用户所在地必须来自有效用户事实。
- 角色可通过受权 Search Action 主动查证；来源后续纠正能形成新 perception 和重新评价机会。
- 兴趣、当前计划和 NPC 关系只影响候选相关性；不能形成 topic→关注/讨论规则。Perception correction 可
  更新 actor belief 和对来源的私人理解，不抹除此前确实看见过的错误报道。
- 感知可影响 Life/NPC/Plan（例如活动取消、流行话题），但必须经过对应 author/authority。

验收覆盖国内多来源重复、谣言/纠正、地域权限、角色未看到不反应、看到后可沉默/讨论/调整计划，
以及采集失败不阻断世界 runtime。

## 16. L13：Expression、主动联系、打断与媒体

- ExpressionPlan 支持不回、单 Beat、多 Beat、afterthought、插话、延迟和主动联系；数量、问题、语气、
  自我分享、是否发送均由 CharacterInterior 决定，不固定 afterthought 或追问。
- 利用 provider streaming 尽早接受完整可发送的 Beat 单元；不能发送尚未通过来源/权限闭包的半句。
- 新用户消息使旧未 dispatch Beat 失效，角色可在新 turn 决定继续、取消或替换；已发送 Beat 不撤回。
- 主动考虑由 Life/NPC/Affect/Relationship/Memory/Perception/ambient opportunity 唤醒；随机只决定时机。
  模型 silent 后正常重建机会，技术失败走明确 retry，不伪装成沉默。
- media 保持 planning→authorization→render→inspection→Action→receipt；图片必须来自已接受的当前环境或
  生活来源。用户对图片的反馈可进入关系、记忆和后续 Life opportunity。
- 接通现有 `appearance_state` / `visible_physical_state` 或明确删除取代：媒体规划读取已接受的外观、
  着装与时间连续性，不让 renderer 临时创造第二个外观真相；形象变化必须来自合法 Life/Character
  choice 与媒体可见来源。
- Expression/Media 只可披露 actor 当前有权知道且允许共享的材料；delivered receipt 更新只读 disclosure
  history。具身处境可作为角色材料和媒体依据，但不能通过本地矩阵决定语气、延迟或分享。
- 当前 Consent/Authorization 在每次 Action 前重新校验；同意撤回立即阻止未 dispatch effect，关系或角色
  感受不能覆盖它。已 dispatch/settled 历史仍保留。
- 深化现有 authorization seam，不能建立 `adult_consent_v2` 平行系统：为 Adult Eligibility、Intimate Consent
  和 content class 增加 typed、revisioned、recipient/channel/capability-scoped reading。亲密 consent 永远
  revocable；当前单用户在面板完成前可使用 root-attested operator administration 明确录入用户成人资格与
  scope，不能从 QQ 文本或模型猜测。未来面板迁入同一 command/interface，不改 authority。
- CharacterInterior 的同一 turn 可选择 `romantic_affection/sensual_non_explicit/sexual_suggestive/
  explicit_adult` 或拒绝/停止/no-change；分类只用于 consent/privacy/provider 路由，不给模型动机或文案。
  用户 grant、lover stage 和过去亲密内容都不能生成角色当前意愿。
- 发送前按最新 cursor 重检：角色与用户成人资格（成人类）、用户 content/channel/capability consent、角色当前
  proposal、recipient-exclusive privacy、provider route 能力与 Action grant。QQ 中明确撤回先保守冻结未
  dispatch intimate effect，再经 typed withdrawal 接受；不得等后台模型后继续发送。
- 删除 `_p3_lane_for_stage` 一类“relationship stage→允许强度/lane”的行为决定。Relationship 只进入
  CharacterInterior advisory；hard authorizer 校验角色已选内容是否在双方同意和 provider 能力交集内。
- `explicit_adult` 在没有合法 provider、资格证明和端到端回执前保持 unavailable；不得降级、换 route 或仅凭
  prompt 标签启用。现有 suggestive media 也要证明不是模板存在但 production grant/route 未接。
- intimate transcript/media/eval artifact 默认 recipient-exclusive、最小 provider disclosure，operator health
  只显示元数据。保存原始故障样本前脱敏；为未来面板删除比较 encrypted payload + ledger hash/ref 与
  tenant-key crypto-erasure，但本阶段不重写历史或承诺尚未实现的物理删除。
- 对照设计意图 S1 验证“兴奋时主动多 Beat”技术上可达，但不得新增 excitement→message count 规则；
  Fast Reply 只能做模型结果的无损物化，不能把模型明确的多个 Beat 压成单条。

验收用真实 QQ 多轮检查首条延迟、完整批次、连续多 Beat、中断/被中断、主动消息、图片全链及失败
不重发；另覆盖用户未认证、角色未成年逻辑时间、用户已撤回、角色拒绝、双方允许、并发撤回晚于 claim
早于 dispatch、provider 不支持、重启后旧 grant stale。测试只能断言越界不执行和多种角色选择可达，不能
要求角色必须调情、升级或使用固定成人话术。不得只用固定脚本输出判断自然度。

## 17. L14：系统级可观测、成本与涌现评估

### Health

为每个 purpose 暴露：state、open/due/stuck、last source、last considered、last valid role decision、
first-attempt 四层成功率、纠正率、技术失败、retry、provider latency/token/cost、qualification state。
保持旧字段兼容一期，消费者迁移后删除旧字段。

### Causal Lineage View

实现只读投影/诊断命令：输入 source/event/actor 可追踪 opportunity、Interior Turn、typed proposals、
acceptance/rejection、Plan/Action/receipt 和后续 consequence。它不能成为模型的第二份剧情摘要。

### 评估

- 每个 release 先运行覆盖其启用能力的场景子集；未启用的丰富度能力验证“关闭后核心仍正常”，不为了
  凑齐完整愿景阻塞稳定内核上线。完整愿景资格化时再运行至少 30 个多日随机场景，包含互动、沉默、NPC、
  Life Arc、负面偶然、Perception、Memory、
  主动消息、媒体、具身变化、承诺冲突、物品连续、技能变化、披露、共同文化、同意撤回、多视角声誉、
  不可逆损失、误解/修正与反事实；验证 actor isolation、reachability、终止、effect-once、成本和链条
  形状多样性。
- 先用 `scripts/run_world_v2_conversation_audit.py --database <isolated.sqlite> --output <audit.jsonl> --strict`
  跑隔离真实 provider/生产 host 的固定 32 轮旅程，
  保存 transcript、分段延迟、usage/cost、错误和 ledger evidence。其 lexical assertions 只作已知灾难 smoke
  gate，不能作为真人感结论。
- Luna 必须再使用 `scripts/chat_with_world_v2.py --database <scratch.sqlite> --clone <production-copy>`
  **亲自自由对聊**至少 15 轮：
  下一条用户消息依据角色真实回复现场决定，不使用固定答案，不通过 Python 注入预设模型输出。至少覆盖
  短句互发、连续多气泡、话题跳转、争执/纠正、隔时召回、插话/被打断；另用 `--burst-message` 验证整批
  Observation。保存原始 transcript、latency segments、usage/cost 和相关 ledger refs，逐轮写自然度判断。
- `scripts/automated_conversation_test.py` 当前是早期两轮关键词 probe，不得用于签署生产资格。若 Luna 保留，
  必须显式标 legacy；固定 fixture、模型 evaluator、测试全绿和 agent 自由对聊四者不能互相替代。
- 在首次资格化前深化而非旁建 `chat_with_world_v2.py`：增加 JSONL 输出，记录每个真实 user/role unit、时间、
  status、ledger cursor/相关 refs、latency segments、provider usage/cost 和错误；允许测试者现场输入，不加入
  固定对话策略。若要模拟等待或 scheduler wake，只调用 production host 公开 seam 并记录逻辑时间。
- 审计 `conversation_audit_acceptance.py`：把身份越权、事实来源、runtime/Action/ledger、延迟等硬门与
  “必须出现某情绪词/固定 Beat 数”等 fixture 观察分开。后者不得进入角色 prompt、生产规则或发布硬门；
  真实 transcript 由 Luna 先写逐轮判断，再由 Sol/用户抽查，不能让被测模型自己给自己签字。
- 最终发布通过真实 QQ 由用户或 Sol 抽查。若测试绿但自由对聊复现失声、僵硬、失忆、只回最后一句或状态
  未延续，以真实体验为失败并回到根因，不得将其降为“主观意见”。
- 衡量提问率、自我分享、消息数只用于发现退化，不设成角色行为 KPI；逐条回看她是否有来源明确的
  私人想法和真实选择空间。
- 建立“机制存在但生产未接通”告警：有 authority 无 producer、有 producer 无 consumer、持续 open 无
  terminal、模型长期零调用、health 与账本矛盾均 warning/error。

## 18. L15：迁移、删除旧接口与发布

以下步骤对**本次 release 声明的能力与被替换路径**执行。未启用能力不需要伪造 canary，但必须证明其
adapter 关闭时不会阻断核心；已经被本次新路径取代的旧入口则必须删除，不能以“下阶段再说”保留旁路。

1. 为本次范围内所有旧入口列调用者；先迁 consumer，再加架构 guard，最后删除旧实现和兼容配置。
2. 冷重放生产副本，比对 head cursor/hash、typed projections、Action/receipt、关系、记忆、Life/NPC 和
   Expression；replay 期间模型调用必须为零。
3. 完成相关测试、全量测试、ruff、类型/静态检查、`git diff --check`，再做 Standards、Spec、
   Agency/Authority 与 Production Readiness 四路审查。
4. 停 daemon，备份并做 integrity check；部署 migration/code/config，重启并观察 scheduler 首轮恢复。
5. 真实 QQ 做完整批次回复、一次主动考虑、一次 NPC/Life opportunity、一次重启恢复和可选 media canary。
6. 观察至少 24 小时：无长期失声、无重复消息、无 scheduler crash loop、first-attempt/延迟/成本/账本增长
   达标。未达到就回滚到部署前 checkpoint，不以宿主语义 fallback 维持表面可用。
7. 强制断开 provider 验证 System Notice：它必须明显标为系统消息、限频且可恢复，不进入角色 Expression、
   Memory/Relationship/Affect 或共同历史。恢复后的角色不能把系统故障叙述为自己的经历。
8. 当前不交付管理面板，但要冻结其未来写 seam：只读状态来自 Projection/health；管理写入走认证后的 typed
   command/CAS/audit，QQ 与直接数据库写入不能复用。多用户 provisioning、账号计费和 tenant key 管理保持
   design-only，不得为“以后可能商业化”扩大本次上线面。

## 19. 每个工作包的交付模板

Luna 每完成一个工作包，按以下格式记录，方便 Sol 和下一位 agent 审核：

```text
工作包：Lx / 名称
设计条款：链接到 `girl-agent-design-intent.md` 的具体章节
用户失败：真实体验、最小复现、生产 trace、当前基线
业内证据：官方文档/标准/原始论文/源代码链接、结论与适用边界
候选方案：至少三个实质不同 Interface/seam；若豁免则写明理由
否决门：每案对角色自主、actor/privacy、source、CAS/effect-once/replay 的结果
判别实验：共同 workload、原始样本、测量值、淘汰了什么
方案裁决：推荐理由、被拒方案、Pareto trade-off、首选方案最可能的失败
变更文件：精确列表
旧路径：已删除 / 暂留及删除条件
producer → consumer：精确事件与 runtime
角色决定：哪些字段由哪个 actor/model 作者产生
系统硬边界：哪些本地校验及理由
红测：名称与失败证据
绿测：相关 / 全量 / 静态结果
真实证据：provider、daemon、账本、重启、QQ/Action
一次成功：样本量、四层比率、失败样本
性能成本：p50/p95、token、调用量、数据库增长
迭代记录：假设 → 改动 → 测量 → 结论 → 下一步
独立审查：Standards / Spec / Agency-Authority / Production Readiness
停止规则：哪些门槛已满足；不足样本必须写 qualification_incomplete
未决风险：不能隐藏
commit：hash
```

## 20. 必须交回 Sol/用户决定的事项

- 新增或合并 typed authority；
- 改变事实、隐私、同意、安全、Action 授权或 actor visibility；
- 引入新的角色作者、备用模型或可见故障话术；
- 为提高成功率而删减角色合法选择（尤其 silence/later/withhold/no-change）；
- 需要重写 V2 不可变事件或删除灾难恢复数据；
- 研究结论存在两种都可行但会显著改变角色自主性、成本或长期世界形状的方案。

除此之外，Luna 应主动追到根因、查阅业内方案、补齐测试和生产证据，不把“需进一步研究”作为
默认终点，也不通过堆规则让某个演示样例看起来正确。

## 21. 2026-08-08 执行记录（Luna）

本节只记录本轮实际核验与改动，不把代码存在或测试变绿误写成生产完成。

- 本次 Sol 交接审计所在的 Codex 桌面任务共有 4 个 active slot，子 agent override 仅暴露 Sol/Terra，未暴露
  Luna；因此本次文档盘点使用 Terra 只读 agent，未冒充 Luna 执行。该限制只是本次运行环境证据，不是
  长期架构规范；后续编排必须重新查询当时可用模型和 slot。

### L0 基线与已证实根因

- 当前 production QQ scheduler 在重启前连续 `318/318` 失败；日志显示 `life_reflection` 触发器已经引用
  已提交的 `AppraisalAccepted`，但反思编译器仍拒绝该 source，随后 host 又把异常路径解释成
  `appraisal_only`，因此 trigger 不终结并在每轮重复。这是生产接线错误，不是角色选择沉默。
- 本轮还核验了本地 embedding LaunchAgent：旧的 BAAI/bge-m3 MLX 不兼容 crash-loop 已被前一提交退休
  （`RunAtLoad=false`、`KeepAlive=false`，安装脚本把 label 列入 retired 集合）；当前不再作为聊天或后台生产
  依赖。Recall 仍有可用的本地/特征哈希降级路径，semantic embedding qualification 另行记录，不能把它写成
  已通过。
- 工作树中的 `.idea/vcs.xml` 与 `training/` 是用户并行修改，本轮不纳入提交。

### L2：CharacterInterior durable turn coordination

- 新增技术 sidecar `world_v2_character_interior_turns`，不进入 V2 immutable event/head/prefix proof。
  SQLite 与内存实现共享 claim → checkpoint → terminal 的 CAS 语义；每次 lease 有独立稳定 token，过期
  reclaim 会增加 attempt ordinal，旧 owner 不能 checkpoint/complete。
- production QQ composition 在同一 World 数据库打开 sidecar，并以 `world_id/actor_ref/inner_turn_id`
  做坐标；跨 runtime 的 terminal/recovery 会复用已保存的 role result，不再依赖单进程 `_cache`。旧进程缓存
  仅作同实例 effect-once 加速，不是跨进程 authority。
- 生产失败语义保持技术失败：sidecar/role/authority 异常不会由本地模板改成 silent/no-change；checkpoint 后
  崩溃可恢复，terminal 后重启只重放结果，不重新调用模型。

### L3：Reflection 与 scheduler 隔离

- `AppraisalAccepted` 现在是 `life_reflection` 的明确 source：compiler 只读取该 accepted event，并要求
  当前 claimed trigger 的 `reflection:<event_id>` identity；reducer/trigger contract 同步接受
  `life_reflection` 的 `committed_world_event` evidence。原始事件未改写，冷重放语义保持不变。
- world-stimulus 的 affect/settlement 编译或 CAS 异常现在保留 claimed trigger 为技术可重试状态，不再伪装
  `appraisal_only`；platform host 对单个后台单元建立隔离边界，技术失败不会拖垮同一轮 scheduler 的其他
  inbound/Action 工作，也不会在同一轮立即重选同一个坏 trigger。
- 已补充从原始世界变化 → AppraisalAccepted → ReflectionScheduler → CharacterInterior reflection 的
  集成回归，以及双 runtime sidecar claim/recovery、world-scoped prune 测试。
- 后续独立审查发现 terminalized source 已有 immutable audit、但 Affect/relationship/experience aftermath
  仍 pending 时，恢复异常不会进入原有 defer，因而可在固定优先级中持续抢占。修复后任何已选 trigger 的
  未捕获异常都只在当前 runtime 进入 monotonic technical defer；其他 trigger 可继续，fresh runtime 仍从
  durable audit 恢复原 trigger。回归用两个 terminal/audited recovery 证明第二个可推进、重启后第一个可
  继续，且 recovery role model 调用数为零。相关测试 `38 passed`，指定文件 ruff 与 `git diff --check` 通过；
  Standards/Spec 双路审查均无阻断项。

### 当前证据与未完成门（早期记录，已由下方补充核验更新）

- 该段保留为交接时的早期基线；完整测试、静态门和最终 daemon 证据见下方“补充核验”。
- 仍未完成的资格门是冷重放、真实 QQ 自由多轮对聊和 24 小时稳定性样本；若样本不足，状态保持
  `qualification_incomplete`，不能把单轮 scheduler 通过当作生产资格。

### L0/L2/L3 补充核验（2026-08-08，继续）

- 结构化角色线协议只增加了无损的 transport-shape 归一化：只有模型已经同时写出完整
  `decision.source_refs` 与 `decision.payload` 时才保留既有对象；typed proposal 只把模型写出的同一对象
  移入 `proposals`。裸的扁平 decision 不再把 `attended_source_refs` 提升为证据，而是进入同一模型的一次
  纠正；Life choice 仅在已有 envelope 且 payload 缺 `completion` 时把模型已写出的对象放入该键。没有补
  summary、source refs、事实、动机、timing、silence 或任何语义值。
- 一次纠正现在同时携带本地校验的精确失败细节（仍绑定同一 pinned cursor、同一角色模型和 attempt），使
  `private_impression` 的 predecessor/source 闭包及 proactive `world_claims` 对象形状可以被模型直接修正；
  不启用备用语义作者。主动联系真实日志中出现一次 `world_claims` 字符串形状错误后，第二次同作者结果成功
  物化并发送；这是生产轨迹证据，不是固定 fixture 推断。
- 冻结场景完整 120 案例的可见事件族、输出、Action 状态和调用数未改变；结构化提示/审计身份变化按既有规则
  升级 offline mechanism baseline 到 `.53`。完整场景测试通过。
- 本次直接相关回归（主动联系与 world stimulus）：`84 passed`；全套：`4735 passed, 1 warning in 358.81s`；
  `uv run ruff check .`
  与 `git diff --check` 均通过。
- 早期记录曾把 PID `12121` 记为“最新代码重启后”，但 2026-08-09 复核发现该进程启动时间早于
  `93e2f9fb` 的 terminal-recovery 修复及当前 `02e0a01f`，因此这条运行态不能作为当前 HEAD 的证据。
  现场只读 `/health` 连续三次均可返回（约 2.6–3.7 秒），但在本次观测窗口（2026-08-09
  01:41–01:47，Asia/Shanghai）仍显示 `scheduler=failing`、`last_error=ValueError`，失败计数也在
  增长；该 PID 的旧日志仍出现旧版的 `settling appraisal-only` 分支。`accepted world stimulus did not
  terminalize its source trigger` 仍保留为当前源码的防御性不变量错误，但旧进程反复触发它。这证明
  旧进程需要在人工发布门后重启，不能据此否定当前源码，也不能宣称生产恢复完成。当前未执行重启或
  生产替换。
- 当前源码已将 proactive situation window 证据按已声明刺激事件去重并保留窗口锚点与最新 7 条；
  这避免 `trigger evidence must be a bounded unique tuple` 令整轮 scheduler 失败。该行为与
  `ExpressionPlan -> send_private_msg` 的主动联系回归均已有隔离测试，但仍需重启后的真实 daemon
  观察确认。上段 PID `12121` 的旧 health 还显示 fast stream 为活动回复接口、delayed attention 接口保持禁用；
  sidecar 暴露 `scope/expired_claim_count/recovered_attempt_count`，不再用未绑定 actor 的空查询伪装成
  scoped ready，这些字段同样不能当作当前 HEAD 已重启的运行证明。
- 最新源码对单个坏 trigger 使用进程内 technical defer；world-stimulus 定向回归证明其他 trigger
  可以继续推进、重启后仍能从 durable audit 恢复，且不重新调用角色模型。这是源码/隔离测试证据，
  不是旧 PID 的生产运行证明；旧进程仍有历史过期 sidecar claim 和 scheduler failure，必须重启后再
  验证不会扩散成 crash loop。sidecar 回收也不等同于重复发送授权。
- 仍未宣称生产资格完成：当前生产账本保留 24 小时历史技术失败 warning，语义 embedding 8190 未部署时 Recall
  会降级到本地索引，Perception enforcement authority 与独立事实 reviewer 仍是明确 degraded 能力；这些不被
  结构化封套修复冒充为角色选择。真实 QQ 自由对聊、冷重放和 24 小时稳定性样本仍是 qualification gate。

### 2026-08-09 继续核验：隔离 daemon required-tool 重启连续性

- 首次运行 `loopback-stub` 失败的原因是资格 harness 仍返回旧式 `message.content`，而当前 CharacterInterior
  已要求 required function tool；这不是生产 daemon 的失败。按红→绿修复后，stub 会从请求中的真实 tool
  名返回单一 `tool_calls[].function.arguments`，并包含当前 transport 必需的 `result_kind=decision`，不改变角色
  语义或生产 provider adapter。
- 通过命令：
  `uv run python scripts/run_isolated_daemon_acceptance.py --output /tmp/girl-agent-daemon-acceptance-loopback-fixed5.json --startup-timeout-seconds 120 --model-mode loopback-stub`。
  运行契约 `isolated-daemon-process-acceptance.2`，UTC `2026-08-08T18:26:15Z`（Asia/Shanghai 02:26），只使用
  临时库、127.0.0.1 daemon/OneBot/provider capture 和 test-only semantic authorities；生产库、真实 QQ 发送和外部
  provider 均未触碰。
- 两次独立 daemon 进程均健康启动；7 个入站 source identity（包括三条 burst 和两条重叠打断）全部保留，
  burst 合并为一个 Observation，第二条打断在第一条 provider 请求仍在途时到达；重启后重复 source 的可见效果和
  新模型请求均为 0，新 source 产生 1 个新效果；Action/回执 4 次，cold replay 与 live head 一致，最终事件数
  156，确定性 invariant `passed=true`。这证明的是本地 provider HTTP/daemon/CAS/冷重放连续性，不是 DeepSeek
  真实模型成功率或角色措辞资格。
- 本次 loopback 还暴露并修正了一个审计边界：required-tool 的本地 contract identity 不发送给 provider，harness
  通过 provider 可见的完整 tool schema 在本地重建候选 identity，再与持久化 request hash 对齐；因此没有为测试
  方便把本地身份塞进 provider 请求。provider presentation capture 已升级为 `.2`：原始 provider/model
  request hash、forced-tool 本地重建候选、来源事件 ID 和可见角色请求标记分别记录，混合 plain/forced 调用不会
  互相覆盖；来源事件 ID 只作为审计展示，因果闭合仍只接受 exact provider request hash，不会用用户文本中的
  同名字段兜底关联。隔离 daemon 固定使用 `WORLD_V2_RECORDED_CADENCE_MODE=shadow`，保证重建的 capability profile
  与被测进程一致。真实 DeepSeek 100 次 forced-tool/stream/回执样本仍保持
  `qualification_incomplete`，必须经过人工发布门。

### L1 第一个生产薄片：non-stream inbound initial（2026-08-08）

- 仅将当前非流式的 inbound combined initial 角色调用迁入版本化 forced-tool contract；stream、
  recall follow-up、各类 correction 与其他 CharacterInterior purpose 仍未迁移，因此 L1 状态仍是
  `partial / qualification_incomplete`，不把这一薄片写成全局完成。
- 新的 `InboundToolContracts` 从 canonical typed Appraisal/Expression/Recall wire 与当前 QQ capability
  派生 provider schema，保留 `now/later/silent/recall`、多 Beat、reaction/sticker、expectation、
  relationship signal 与 Affect lifecycle；删除了会缩减合法选择的手写 combined tool 字段表。
- forced envelope 必须明确且互斥地选择 decision 或 recall；缺少/mismatch 不由宿主重解释，
  而是进入同一角色模型的一次有界纠正。该纠正预算按整个 turn 共享；envelope 纠正后再出现
  appraisal、Affect floor 或 expression 错误会终结为技术失败，不会打开第三次语义重选。
- live forced 候选与其纠正必须显式选择 Affect lifecycle；open/update/resolve/supersede 的结构必需字段
  由同一 canonical 定义同时驱动 typed validator 和 provider schema。Prompt 的数量、长度与显式
  `affect` 要求也从同一边界生成，不再存在两套矛盾数字。
- tool name/version、canonical/provider schema digest 与 capability profile 绑定 provider request identity；它们不作为
  非标准字段发给 provider。可读 contract audit 与冷重放 fixture 仍列入下一包，不由 opaque hash
  的存在冒充完成。
- 当时的本地证据：直接相关与扩展 CharacterInterior/LLM/registry 回归通过；具体样本数随之后的
  contract 与 host 场景切片变化，不作为当前计数。真实 DeepSeek ordinary function-call 样本、流式工具碎片、
  每 purpose first-attempt 统计与真实 daemon 资格测试仍未完成。

### 2026-08-09 继续核验：入站流式闭合、主动联系强制工具与延迟触发证据

- 入站 combined 路径的强制工具已继续覆盖到生产 stream、recall-final 和同角色 correction；stream 仍保持一
  次物理 provider 调用、head/tail 语义拆分、旧 SSE 纠正前真实退休，以及工具参数乱序时只在 transport 与
  source closure 闭合后释放可见 head。表达式专用的 source-closure/结构校正现在复用同一 canonical
  `ExpressionDraft` reselection schema，并在具备 required-tool 能力的角色 provider 上走版本化
  `character_expression_reselection_v1`；历史严格 JSON 只保留给未具备该能力的隔离兼容 lane，不能把它当成
  生产成功证明。它仍未与 combined contract 混用，也不能把“所有 CharacterInterior purpose 已
  forced-tool 化”写成完成。
- `proactive_contact` 现在使用由 `ExpressionDraft`/`ProactiveDraft` 派生的版本化 required tool。契约缓存按
  capability profile 与 recall phase 复用，`now/later/silent`、多 Beat、typing/reaction/sticker 与有来源事实
  声明均保留；schema 会在 provider 侧拒绝空 impulse、空 later 到期字段、模态载荷不一致和 grounded claim
  缺来源。`expires_after_seconds > delay_seconds` 是标准 JSON Schema 无法表达的兄弟字段比较，继续由工具描述
  提示并由 canonical `ProactiveDraft` fail-closed，不能当作 provider 首次结构资格已解决。
- 主动联系的极短预算回归证明 provider 首次调用进入且取消后不会再次 author（当前 fixture `calls=1`）；为避开
  capability 专用 schema 与 pinned snapshot 的首次构建竞争，确定性准备在语义审议前完成，未改变模型对
  `now/later/silent` 的决定，也未把异常转写成 silent。该路径的真实 DeepSeek 首次成功率、真实 QQ 回执和 24
  小时成本/延迟样本仍是 `qualification_incomplete`。
- `Delayed Trigger Qualification Matrix` 现在明确是 `declaration_only`：静态 catalog、真实 owner/Action/
  projection/公开 seam 的双向核对不会授予发布资格；host 场景证据必须指向实际 pytest collection 中存在的
  node。当前已加入的 public-host 场景覆盖 deferred reply/Commitment/Action、proactive silent 与技术退避、
  expression multibeat/interjection、platform receipt unknown→late conflict，以及 affect decay/silence
  aftermath 的重启、effect-once 和冷重放；测试明确排除真实 provider 自主性、OneBot callback 归一化和 24h
  soak。Life occurrence 的活动生命周期已有独立 public-host 场景；NPC/reflection、memory consolidation、
  perception/media 仍按 limited/dormant 事实标注，不得因为 catalog 有行就激活。
- 本轮当前工作树的定向回归均已通过（覆盖结构化角色、LLM、表达式校正、主动联系与延迟触发主机证据）；
  后续完整套件回归为 `4871 passed, 1 warning`（矩阵声明门禁另行通过）。ruff、语法检查和
  `git diff --check` 也通过。这仍不是真实 provider qualification 或生产上线证明，具体资格缺口保持
  `qualification_incomplete`。

### 2026-08-09：表达式专用校正的 required-tool 薄片

- 设计候选曾有三条：复用 combined cognition tool（会错误要求 appraisal，缩窄表达式 wire）；继续只用
  strict JSON response-format（无法闭合 DeepSeek 的 required-tool 首次协议）；或在既有
  `structured_expression_reselection_model` 深模块内由 canonical expression schema 派生独立 function tool。
  选择第三条，原因是它保留 source-closure correction 的独立语义，又不复制字段清单或建立新的行为旁路。
- `expression_reselection_tool_contract` 只负责编译 provider-visible parameters、版本化 tool name、schema/
  capability/contract identity 与 transport-only unwrap。`now/later/silent`、多 beat、typing/reaction/sticker、
  private turn state、expectation/assessment、world claims 和 episode disposition 仍由角色模型选择；本地
  normalize、Expression materializer、source reviewer、Action/CAS 边界未放宽。工具参数缺失、混合 envelope
  或 provider 不支持 required tool 时不会补默认/伪造沉默；同一角色最多一次受约束校正，仍失败为技术失败。
- `_ExpressionDraftWire` 与 paired `InboundCharacterAuthor` 的 expression-only correction 现在共享该编译器；
  provider request identity 绑定 canonical/provider schema 与当前 source capability。没有 required-tool 能力
  的历史 strict JSON lane 仍可用于 replay/隔离 fixture，但不授予生产资格。
- 已验证：contract public seam 的互斥/无损解包、DeepSeek-compatible HTTP body、真实 ExpressionDraft public
  proposal seam 的一次校正与无 plain fallback，以及相关结构化/入站回归和 ruff/diff-check。隔离 daemon
  验收脚本现可用 canonical source-closure envelope 或 `expression-reselection-transport.1` carrier
  重建 `character_expression_reselection_v1` 的本地 identity；缺失/嵌套/重复/边界不完整时安全不计入。
  这只修复证据关联，真实 DeepSeek forced-tool/stream/receipt 样本、每 purpose first-attempt 统计、QQ
  自由对聊及 24h soak 仍保持 `qualification_incomplete`，不能以 MockTransport 或 fixture 结果替代。

### 2026-08-09：ambient proactive public-host 资格场景薄片

- 为仍只有静态声明的 `proactive.ambient` 增加了真实公开宿主场景：通过
  `QQC2CHost.inbound_text → tick → drain` 将逻辑时间推进到 ambient 窗口，确认刺激来源是已提交的
  `ClockAdvanced`，模型只被调用一次并选择 `silent`，没有生成 proactive Action；关闭并重建宿主后再次公开
  `tick/drain`，不会重复调用模型、不会新增 Action 或终态。
- 先以缺失 Matrix 场景注册和重复 tick 幂等边界得到 RED，再补上唯一 scenario/node 绑定；相关延迟触发、
  Matrix、冷启动与回执场景回归为 `43 passed`，`verify_delayed_trigger_catalog.py` 输出
  `static declarations verified: 28 delayed trigger mechanisms`，ruff 与 `git diff --check` 通过。
- 这只证明当前隔离数据库和 scripted role 在生产公开上游 seam 上的调度、重启和 effect-once 语义；Matrix
  仍是 `declaration_only`，真实 DeepSeek/QQ transport、自主措辞、24 小时 soak 及生产 daemon 重启均
  明确排除，不能把本场景写成上线或模型成功率资格证明。

### 2026-08-09：真实 DeepSeek required-tool 方言修复与最终临时验收

- 首次使用真实 DeepSeek、临时数据库和 OneBot loopback capture 的隔离 daemon 验收，所有模型调用都在
  provider 首次请求前以 400 失败。捕获的原始错误为：`Invalid schema for function
  'character_inbound_initial_stream_v1': schema must be a JSON Schema of 'type: "object"', got 'type: null'`。
  这不是模型语义选择失败，而是 stream function parameters 的根 schema 不符合 DeepSeek 的 function-calling
  方言；此前 MockTransport 测试没有覆盖该 provider 边界。
- 先增加红测，再在 `InboundToolContracts` 的 provider-facing 根封套加入 `type=object`、完整联合属性、
  `required=["result_kind"]` 与 `additionalProperties=false`。分支级 `anyOf` 仍分别约束 decision/recall，
  根 discriminator 明确合并为 `decision/recall`；不会因 recall 分支覆盖 result_kind 而删除角色立即回答的合法选择。
  本地 `unwrap` 与既有 materializer 继续负责精确语义校验，不补默认、不把非法输出改写成角色决定。
- 最终工作树在该合并修复后重新执行真实 provider 临时验收：报告时间 `2026-08-08T19:56:55Z`，命令使用
  `--model-mode real-provider --allow-real-provider`，两次独立 daemon 进程、临时库、外部 DeepSeek 经本地
  hash proxy、OneBot loopback capture；生产数据库与真实 QQ 均未触碰。所有 7 个入站/打断/burst/restart
  场景得到 HTTP 200，确定性 invariant `passed=true`；6 个 provider-accepted Action、重复重启无新增可见
  效果、cold replay 与 live head 一致，第二条入站在第一条 provider 请求仍在途时成功提交，三条 burst 保留
  全部 source event 并合并为一个 Action。该结果是 transport/daemon/CAS/回执连续性的人工证据，不是措辞、
  自主性或生产成功率证明。
- 当时最终定向回归为 `533 passed`，ruff 与 `git diff --check` 通过；之后的表达式 carrier/证据边界回归另行
  通过（表达式/入站/验收相关 506 项，Delayed Trigger Matrix/host 48 项）。真实 provider 证据仍严格标记
  `manual_only`/`qualification_incomplete`：当前未满足 100 次 forced-tool/stream/QQ 回执样本、自由对聊
  质量观察和 24 小时 soak 的发布门；旧生产 daemon 也未在本轮被替换或重启。

### 2026-08-09：Life Ecology public-host 调度资格薄片

- 为 `life.ecology` 增加了唯一的 Matrix 场景绑定和 public-host 回归。场景只通过
  `QQC2CHost.tick(run_life_ecology=True)`、`inbound_text`、`drain`、`export_replay_evidence`、
  `world_health_diagnostics` 与 `aclose`，没有直调 Life worker、ledger 私有写入口或 trigger mutator。
- 在隔离库中让 World Author 明确超时：ClockAdvanced 后 Life trigger 依次经历 open→claim/CAS→技术失败终态，
  health 写出 `life_development.world_author_unavailable` 和下一次 10 分钟考虑时间；随后同一 host 的正常
  用户入站仍得到 Action 授权，不被 Life 失败拖住。重建 host 后到期重试产生新的 trigger identity，第二次
  技术失败仍准确终结；重复 drain 不重新打开已完成 trigger，冷重放语义哈希保持一致。
- 该场景验证的是 Life Ecology 调度、隔离、退避和 replay 语义，不是 World Author 的真实模型质量或 Life
  内容生成资格。真实 provider、自主生活选择、OneBot 回执和 24 小时 soak 继续排除；Matrix 仍为
  `declaration_only`。本薄片及既有延迟触发场景回归共 `48 passed`，静态 verifier 仍报告
  `28 delayed trigger mechanisms`。

### 2026-08-09：post-silent 主动联系再次考虑 public-host 调度资格薄片

- 为 `proactive.post_silent` 增加公开宿主场景：角色第一次在 ambient 机会选择 `silent` 后，系统只依据该
  已提交的沉默终态记录下一次随机化考虑时间；到期后再次把机会交给角色模型。测试确认第二次机会的
  `source_kind` 仍是 `post_silent`，不会在持久化 Clock 恢复时退化为 ambient，也不会生成强制措辞或主动 Action。
- 该身份通过可逆的 consideration marker 绑定原沉默 trigger；原 Clock 事件仍是来源权威，TriggerProcess、
  TriggerProcessCompleted、CAS/effect-once 与重启后的冷重放保持同一语义哈希。重复 tick/drain 以及重建宿主
  均不重复调用模型。
- 另有隔离失败路径：post-silent 的首次调用和一次同角色纠正均失败时，技术退避仍绑定同一 marker，按
  `10/30/120` 分钟恢复；重启后重试的模型上下文仍标记 `post_silent`，旧的 ambient 机会不会抢占或污染该次尝试。
- 场景只证明 scripted role、隔离数据库和公开 `inbound_text → tick → drain → export_replay_evidence → aclose`
  seam 的调度/恢复语义。Matrix 仍是 `declaration_only`，真实 DeepSeek/QQ transport、自主动机、24 小时
  soak 和生产 daemon 重启继续排除；不能把该场景当作真实模型成功率或上线资格证明。

### 2026-08-09：Life activity lifecycle public-host 调度资格薄片

- 为 `life.activity_lifecycle` 增加一条真正从生产公开宿主入口运行的隔离场景：第一次
  `QQC2CHost.tick(run_life_ecology=True)` 让 World Author 在已提交的 Clock wake 上提出一项动态、来源绑定的
  生活可能性；下一次公开 tick 让同一个 `CharacterInterior` 以 `activity_lifecycle_choice` 选择或不选择
  已提供的 opening token。系统只负责 catalog、权限、CAS、接受和 occurrence effect，未预置具体行为剧本。
- 场景仅使用 `tick`、`drain`、`export_replay_evidence`、`world_health_diagnostics` 与 `aclose` 等公开宿主
  seam，并检查 `ActivityLifecycleProposalRecorded`、`WorldOccurrenceActivated`、同一角色模型调用和
  `effect-once` 终态。没有直调 Life worker、ledger 私有写入口或 trigger mutator。
- 该证据使用 scripted typed World Author/CharacterInterior fixture，只证明公开调度、角色选择绑定和接受链路；
  Matrix 仍是 `declaration_only`，明确排除真实 DeepSeek、自主内容质量、OneBot 回执和 24 小时 soak。
  新增场景与 Life Ecology/Matrix 定向回归共 `31 passed`，静态 verifier 仍报告 `28 delayed trigger mechanisms`。

### 2026-08-09：Life aftermath outcome public-host 调度资格薄片

- 在同一公开宿主入口上补齐 `life.aftermath_outcome` 的角色选择链：已提交的 activity occurrence 到期后，
  `CharacterInterior` 从当前 capability 提供的 outcome token 中自行选择，系统只负责来源、权限、CAS、接受和
  occurrence 结算，不预置具体结果或人生方向。
- 场景覆盖 `OutcomeObservationRecorded`、`OutcomeProposalRecorded`、`WorldOccurrenceSettled`；重复相同
  tick/drain 与冷重建均不重复调用 outcome 选择、不追加事件，语义哈希和 cursor 保持一致。它使用 scripted
  typed fixture，仅证明公开调度与 effect-once/replay 边界，不证明真实 DeepSeek、自主内容质量、OneBot 回执
  或 24 小时 soak；Matrix 仍为 `declaration_only`。

### 2026-08-09：隔离验收证据的精确上下文边界

- 验收脚本此前按字段名递归扫描 provider 消息，`observation`、`inner_life_snapshot` 或
  `source_event_ids` 出现在用户可控 JSON 中时，可能被误当作角色上下文。现改为只接受生产
  CharacterInterior wire 的顶层 user envelope：`inner_life_snapshot` 必须同时带
  `inner-life-snapshot.1` 与 `derived_from_verified_context`，快照、Recall 和情绪证据均从该
  envelope 派生；任意嵌套或 malformed material 一律不计入。
- `source_event_ids` 也不再从上下文路径猜测，只读取该 canonical envelope 的显式 acceptance
  manifest；生产快照未提供时结果为空，不能凭 source-like 字符串补齐因果链。角色/用户文本中的
  `COMBINED OUTPUT ENVELOPE` 等字样同样不再决定 provider purpose，purpose marker 只看 system
  指令。这样会少报一部分证据，但不会把用户输入误报为来源或内心状态。
- 新增嵌套伪造、观察字段伪造和合法 envelope 回归；隔离验收相关套件为 `48 passed`，完整回归为
  `4871 passed, 1 warning`。这仍只改证据采集边界，不提升真实 provider、QQ、自主性或 24 小时
  soak 资格；发布状态继续保持 `qualification_incomplete`。
- 验收产物契约从 `isolated-daemon-process-acceptance.2` 升为 `.3`，新增 `provenance`：生成时的
  Git revision、tracked worktree dirty 状态，以及验收脚本、Inbound tool contract、StructuredRole
  tool contract、Delayed Trigger catalog 的 SHA-256。这样旧时间戳报告不能被误读为当前工作树证据；
  真实 provider 资格仍需在固定 revision 上重新执行，不能用旧 artifact 越过人工发布门。

### 2026-08-09：表达式校正验收身份关联

- 隔离 provider capture 现在也能处理 `character_expression_reselection_v1`。它只接受生产
  source-closure reselection envelope 顶层携带的 canonical `output_contract`，再调用
  `expression_reselection_tool_contract` 重新编译并比较完整 provider tool/choice；不复制表达式字段表，
  不从用户任意嵌套 JSON 猜测 capability 或来源。
- 所有使用 `character_expression_reselection_v1` 的表达式校正现在都携带同一 canonical typed contract：
  source-closure lane 保留完整失败 envelope，结构/private/claim lane 使用不含行为语义的
  `expression-reselection-transport.1` carrier；验收脚本只重新编译并比较 provider tool/choice，不复制字段表。
  缺少、嵌套伪造、顶层边界不完整、重复候选或不匹配的 contract 会安全地不计入证据，而不是冒充成功。
- 这只闭合隔离验收的证据采集缺口，不等于真实 DeepSeek 首次成功率、QQ 回执、自由多轮对聊或 24 小时
  soak 已资格化；发布状态继续保持 `manual_only`/`qualification_incomplete`。

### 2026-08-09：world stimulus appraisal required-tool 薄片

- `world_stimulus_appraisal` 已接入独立的版本化 required tool
  `character_role_world_stimulus_appraisal_v1`。provider 参数由现有
  `_WorldStimulusAppraisalResult` 与 `_WireRoleResult` 的 typed schema 派生，工具层只固定
  `status`/proposal envelope、`no_change`/`transition`/可用的 `recall_request` 运输形状；具体
  appraisal、Affect、关系、aspiration、experience 以及是否改变状态仍由角色模型决定。
- 该目的不再对声明支持 required tool 的 provider 走普通 JSON；不具备 required-tool 能力时明确记录
  `required_tool_choice_unsupported`，不补默认、不伪造 `no_change`，也不把技术失败转写成角色沉默。
  provider 工具名、schema/capability/contract digest 与当前 request identity 绑定；source closure、
  capability、CAS、接受与后续 reducer 仍沿原链路执行。
- 通过的证据包括：结构化角色 public seam、DeepSeek-compatible HTTP body（无 response_format、确实
  发送唯一 function/tool_choice）、no-change/transition/recall schema 检查、world-stimulus runtime、
  affect/silence public-host 及相关 Life/Perception/Relationship 回归；本轮定向跨模块为
  `101 + 274` 项，ruff 与 `git diff --check` 通过。
- 这只是 CharacterInterior 的一个后台 purpose 薄片，不能写成所有 background purpose 已完成 required-tool
  迁移；真实 DeepSeek 首次成功率、流式碎片、QQ 回执、自由对聊与 24 小时 soak 仍是
  `manual_only`/`qualification_incomplete`。Matrix 继续是 `declaration_only`，不因本地 fixture 或
  MockTransport 绿灯而改变发布状态。
- 不可纠正的 provider 能力缺失在 `CharacterInterior` 外层保留精确的
  `required_tool_choice_unsupported`，不会浪费一次角色纠正调用或改写成泛化失败；冻结离线场景模型也显式
  接入同一工具边界，并将机制基线从 `.54` 审计重基线为 `.55`。
- 合并后的完整套件回归为 `4885 passed, 1 warning`；该数字只记录本次源码回归，不代表真实 provider
  成功率或生产 daemon 健康度。

### 2026-08-09：private impression reflection required-tool 薄片

- `private_impression_reflection` 现在使用独立的版本化 required tool
  `character_role_private_impression_reflection_v1`。provider 参数由既有
  `_PrivateImpressionProposal` 与 `_WireRoleResult` typed schema 派生；短 token、锚点 token、候选
  expiry condition 和 `no_change`/`transition`/可用的 `recall_request` 运输形状来自当前 capability，
  不复制第二份长期事实。角色仍自行决定是否形成 retain/consolidate/supersede 私密解读、如何概括、何时
  请求一次回忆；本地只做 token→真实 source ref 映射、来源闭包、CAS 与接受校验。
- 该 purpose 不再对声明支持 required tool 的 provider 走普通 JSON；缺少能力时保留精确的
  `required_tool_choice_unsupported` 技术失败，不补 `no_change`、不伪造角色沉默。provider tool、schema、
  capability 与 contract identity 进入当前 CharacterInterior request identity；原有一次同角色纠正和失败退避
  语义不变。
- 验证覆盖了 structured-role public seam、DeepSeek-compatible HTTP body、短 token/expiry schema、生产
  `PrivateImpressionTriggerRuntime` 的接受链，以及旧 typed-proposal fixture 迁移；本薄片相关回归为
  `91` 项定向、`315` 项跨 private-impression/CharacterInterior/world-stimulus 测试，本次 revision 的完整
  套件为 `4888 passed, 1 warning`。这些数字只记录源码回归，不代表真实 provider 成功率或生产 daemon
  健康度。
- 这只是一个后台 purpose 的协议可靠性薄片，不代表其他 background purpose 已全部迁移，也不代表真实
  DeepSeek 首次成功率、QQ 回执、自由对聊或 24 小时 soak 已资格化。Matrix 仍为 `declaration_only`，真实
  provider/生产 daemon 资格继续标记 `manual_only`/`qualification_incomplete`。

### 2026-08-09：outcome selection required-tool 薄片

- `outcome_selection` 现在使用独立的版本化 required tool
  `character_role_outcome_selection_v1`。工具参数由 `_OutcomeSelectionPayload`、`_WireDecision` 与
  `_WireRoleResult` 的 canonical typed schema 派生；当前 occurrence 提供的 `offered_tokens` 会被
  capability 专门化为唯一可选枚举，`allow_character_life_direction` 为 false 时只允许显式 `null`。
  角色仍自行决定选哪个候选或请求一次受限回忆；系统不替它选第一项、不生成候选、不把选择直接当作结算或人生方向。
- 下游 `OutcomeMaterializer` 继续负责候选来源、观察绑定、隐私、CAS、proposal/settlement 与 effect-once；
  required tool 只负责运输形状和能力闭包。声明支持 required tool 的 provider 不再回落普通 JSON；能力缺失保持
  精确的 `required_tool_choice_unsupported` 技术失败。
- 本薄片验证了角色 public seam、DeepSeek-compatible HTTP body、候选枚举/来源绑定/方向约束、能力缺失
  fail-closed，并更新 outcome e2e、Life activity public-host 与冷重放 fixture 使其真实传递唯一
  tool/tool_choice。当前 revision 的完整源码回归为 `4892 passed, 1 warning`；这不把 MockTransport 或
  scripted fixture 当作真实 provider 成功率证明。
- 这仍只是一个背景 purpose 的协议可靠性薄片，不代表 Life Development、Activity Lifecycle、Memory、Perception
  等其余模型调用全部迁移，也不代表真实 DeepSeek 首次成功率、流式碎片、QQ 回执、自由对聊或 24 小时 soak 已
  资格化。Matrix 继续为 `declaration_only`，发布状态保持 `manual_only`/`qualification_incomplete`。

### 2026-08-09：activity lifecycle choice required-tool 薄片

- `activity_lifecycle_choice` 现在使用版本化 required tool
  `character_role_activity_lifecycle_choice_v1`。参数由 `_ActivityLifecyclePayload`、`_WireDecision` 与
  `_WireRoleResult` 的 canonical typed schema 派生；当前 activity catalog 提供的 `offered_tokens` 被
  capability 专门化为唯一可选枚举，角色仍自行决定 `select` 某个 opening 或明确 `no_op`。
- `no_op` 保持生产物化器的最小形状 `{"decision":"no_op"}`，不会把 `selected_token: null` 当作另一种隐含语义；
  `select` 必须携带一个当前 capability 的 token。wake source refs、capability hash、tool/schema/contract
  identity 仍绑定到 CharacterInterior request，系统只负责 catalog、权限、CAS、接受和 effect-once。
- 声明支持 required tool 的 provider 不再回落普通 JSON；能力缺失保持精确的
  `required_tool_choice_unsupported` 技术失败。结构化角色、DeepSeek-compatible HTTP body、Life activity
  runtime、QQ production composition 和 public-host activity/aftermath 场景均已迁移到同一工具边界。
- 当前源码全量回归为 `4897 passed, 1 warning`。该数字只证明本地 typed/fixture/公开宿主链路，不证明真实
  DeepSeek 首次成功率、QQ 回执、自由多轮对聊或 24 小时 soak；Matrix 仍为 `declaration_only`，生产发布继续
  标记 `manual_only`/`qualification_incomplete`。scenario runner 的 activity 专用 fixture 与真实 provider
  no-op HTTP 样本仍是后续证据补片，不在本次薄片中冒充已资格化。

### 2026-08-09：Life development character choice required-tool 薄片

- `life_development_choice` 现在使用独立的版本化 required tool
  `character_role_life_development_choice_v1`。参数由现有
  `CharacterChoiceNoOpDraft`/`CharacterChoiceAcceptDraft` 与 `_WireDecision`、`_WireRoleResult`
  的 canonical typed schema 派生；角色仍自行决定接受或拒绝、意图摘要、重要性、可选时间窗口、参与者和
  是否引用当前已提供的 aspiration source。系统不替角色接受机会，也不把接受结果直接当作活动结算或未来结果。
- provider schema 会把 `participant_refs` 限定在当前 external opportunity 的 `entity_refs`，把
  `crystallized_aspiration_source_ref` 限定在当前 active aspiration source；时间覆盖要求成对出现（或都省略），
  窗口是否落在 offered window 内仍由同一 CharacterInterior 的 canonical materializer 做最终闭包。`no_op` 与
  `accept` 是互斥的 typed 分支，nested recall 仍禁止；可用的外层 selective recall 选择不被迁移缩窄。
- 声明支持 required tool 的 provider 不再对该 purpose 走普通 JSON；能力缺失保持精确的
  `required_tool_choice_unsupported` 技术失败，不补 no-op、不伪造沉默。tool/schema/capability/contract identity
  继续进入 CharacterInterior request identity；QQ Life fixture 已同步按 purpose 传递并断言唯一 tool/tool_choice。
- 本薄片定向回归为 `213 passed, 1 warning`（结构化角色、DeepSeek-compatible HTTP body、Life production、QQ
  host migration 和 LLM transport）；随后最终源码全量回归为 `4901 passed, 1 warning`。这些仍是
  typed/fixture/公开宿主证据，不是真实 DeepSeek 首次成功率、自由对聊、QQ 回执或 24 小时 soak。Delayed
  Trigger Matrix 中 `life.development` 继续保持 `limited` 与 `declaration_only`，没有因为工具接线而伪造生产
  资格；真实 provider 资格仍是 `manual_only`/`qualification_incomplete`。

### 2026-08-09：隔离真实 provider 自由对聊与证据记录器

- 使用当前 `cb572962` 在临时 SQLite、`QQC2CHost` console delivery、真实 `deepseek-v4-flash` 上进行了人工输入式
  对话；没有连接旧生产 daemon，也没有 QQ 外发。首轮未加新提示边界时完成 15 轮：上下文连续、可生成多气泡和
  sticker、用户纠正后角色会改口，单轮可见回复耗时约 `4.6–7.0s`（均值约 `5.88s`），明显没有达到秒回。
- 这次对聊发现一个真实 P1 体验/事实边界：当用户要求“讲今天遇到的小事”时，角色编造了“买豆浆”的个人经历；
  追问后承认是临时编造。可见文字里的第一人称生活事实没有经过 `world_claims`，因此不能把“被追问后能纠正”当作
  通过。`expression_draft_shape_contract` 已补充明确要求：可见气泡中的事实性第一人称经历必须同时有同一份带
  `pinned source_refs` 的 `world_claims`；无来源时只能表达感受、假设或明确没有该经历。
- 之后在同一隔离方式下做了 3 轮针对性复测，并启用 `scripts/chat_with_world_v2.py --jsonl`：角色在明确要求不编造时
  未再给出无来源经历，单轮耗时 `3.55s/5.55s/5.18s`；每轮 JSONL 都保存了 user/role units、status、latency、usage
  budget、cursor、semantic hash 与冷重放一致性（`replay_matches_live=true`）。该记录器还支持显式 bounded
  background drain 与关闭 semantic recall embedding，便于后续无人值守证据采集。
- 这仍是隔离真实 provider 的少量人工样本，不是 100 次工具资格、生产 QQ 回执或 24 小时 soak；发布状态继续为
  `manual_only`/`qualification_incomplete`。下一步必须扩大真实 provider 样本，并继续检查未被用户追问时的事实闭包，
  而不是只看结构化测试是否通过。
- 该事实边界提示也会进入冻结离线场景的 prompt/audit manifest；已按项目的显式漂移门将机制基线从 `.55` 重基线为
  `.56`（manifest `fd6fef01…ec550c9`）。这是记录协议/提示输入变化，不是放宽场景断言；完整源码回归仍需重新通过。

### 2026-08-09：隔离 daemon acceptance（非生产资格）

- 在临时 SQLite、OneBot loopback capture 和外部 DeepSeek provider 下完成了一次隔离 acceptance：两个 daemon
  进程启动、7 次 provider-backed turn、scheduler failure `0`、重复可见 effect/model request `0`，冷重放与 live
  head 一致；provider-accepted action 共 `7` 次。可见回复 round-trip 约 `0.73–4.77s`，均值约 `4.01s`。
- 该报告绑定 revision `cb572962` 且生成时工作树仍 dirty，未连接真实 QQ、未测试 100 次 forced-tool/stream、没有
  24 小时 soak，也没有把 character-choice/措辞质量当作自动 gate。因此它只能证明隔离 transport/CAS/replay 的一小段，
  不能证明当前最终工作树或生产 daemon 已资格化；状态继续为 `manual_only`/`qualification_incomplete`。

### 2026-08-09：提交后 revision acceptance

- 提交 `d9a3be4f63bd43469b17c9bc3ed9c2a668bf7061` 后重新运行同一隔离 acceptance（生成时间
  `2026-08-09T04:23:18Z`）：2 次进程启动、scheduler failures `0`、确定性不变量通过；7 个 provider-backed turn
  的 round-trip 为约 `0.72–4.85s`，均值约 `3.47s`，重启后的重复可见 effect/model request 均为 `0`，cold replay
  与 live head 一致。
- 报告仍是临时 SQLite、OneBot loopback capture、外部 provider；工作树 dirty 只反映用户保留的 `.idea`/`training`
  文件，真实 QQ 未连接。它没有覆盖 100 次 forced-tool/stream、24 小时 soak 或自由对话质量，因此发布资格仍为
  `manual_only`/`qualification_incomplete`，不得把这次短跑写成生产健康证明。

### 2026-08-09：隔离 daemon soak supervisor（仅建立安全执行器）

- 新增 `scripts/run_isolated_daemon_soak.py` 与对应测试。它只允许临时 SQLite、OneBot loopback capture 和隔离
  daemon；真实 provider 必须显式传 `--allow-real-provider`，持续 24 小时还必须显式传 `--confirm-24h`。它没有
  生产 daemon 重启、生产数据库路径或真实 QQ 发送入口，报告固定为 `manual_only`，并记录 Git revision 与关键
  源文件 SHA-256。
- supervisor 的每次输入、health、计划重启、冷重放和失败都写入 JSONL/JSON；重放检查同时区分可见 effect、原始
  provider request 数和 CharacterInterior authority request 数，不能把 reviewer/background provider 请求误报为角色
  重选，也不能把“没有可见消息”误报成零模型调用。
- 只做过一个短 loopback smoke：约 3 秒、1 个输入、1 次计划重启；重复输入没有新增可见 effect，CharacterInterior
  authority reauthor 为 `0`，冷重放和临时数据库闭环正常。该 smoke 中捕获到的 `2` 个非角色 reviewer request
  被单独记录，未被吞掉，也未被当作资格通过。这里没有运行真实 provider 的 24 小时任务。
- 这是资格采集器的安全/证据薄片，不是生产发布门。100 次 forced-tool/stream/QQ receipt 样本、自由对话质量、
  24 小时 wall-clock soak 仍需人工明确授权后单独执行；在此之前发布状态继续保持
  `manual_only`/`qualification_incomplete`。新增薄片后的完整源码回归为 `4911 passed, 1 warning`，仍不改变这条
  资格结论。

### 2026-08-09：QQ scheduler 直接背景路径异常隔离

- `QQC2CHost` 在逻辑时钟 CAS 前后各有一条受限的直接背景工作预检路径；它们不经过
  `WorldV2PlatformHost.drain_scheduled_work`，历史触发器或 provider 前置异常因此可能直接逃出 scheduler，
  让整个调度 pass 失败并饿死无关的到期工作。新增 `_drain_direct_background_once`，对非取消异常采用与
  PlatformHost 相同的 `technical_failure:<type>` 记录；durable claim/退避仍由所属 runtime 保留，预算只消耗
  当前失败单元，不把它改写成角色沉默或 no-op。
- 前、后时钟路径分别有回归；重启后模型调用前崩溃的恢复测试改为验证技术失败可观察、租约仍可由新实例接管，
  而不是要求 scheduler 进程级崩溃。当前提交为 `c857ff51`，源码全量回归 `4913 passed, 1 warning`，静态
  Delayed Trigger Matrix verifier 仍为 `28 delayed trigger mechanisms`。
- 旧生产 PID 早于该提交，未在本轮重启或替换；其历史 health/ValueError 不能证明当前源码已在线部署，也不能
  被这次隔离修复宣称为生产资格。真实 DeepSeek/QQ 100 样本、自由对聊与 24 小时 soak 仍保持
  `manual_only`/`qualification_incomplete`，须经过 §20 人工发布门。

- 在该提交后又执行了一次只使用临时 SQLite、loopback capture、loopback role 的短 smoke：2 次输入、1 次计划
  重启，失败数为 0，重复可见 effect 为 `0`，冷重放与 live projection 一致。该运行只验证当前隔离 scheduler/
  restart/replay 闭环，仍不包含真实 provider、真实 QQ 或自主性质量判断，不能改变上述资格状态。

### 2026-08-09：scheduler health 暴露返回式后台技术失败

- `QQC2CHost.scheduler_once()` 已把直接背景路径的异常隔离为持久工作可重试的
  `technical_failure:<type>` 状态；此前 OneBot scheduler loop 只判断该调用是否抛异常，忽略返回的
  `background_statuses`，会把“pass 完成但背景单元失败”误记为成功并清空 `last_error`。
- 现在 loop 仍不重新抛出这类异常（避免恢复路径再次饿死其他 due 工作），但会把同一 pass 的技术失败计入
  scheduler diagnostics：`failures += 1`、`last_error` 保留精确状态、`last_success_at` 不前移；下一次没有
  `technical_failure:*` 的干净 pass 才恢复 running。测试通过 OneBot scheduler public loop 观察 health，而非读取
  worker 私有状态。
- 红测先复现旧行为：返回 `technical_failure:valueerror` 时 health 为 `running`；修复后该场景为 `failing`，
  `passes_completed` 仍递增，证明隔离和可观测性同时成立。相关 scheduler migration/cadence 定向回归 `72 passed`，
  本次源码全量回归 `4914 passed, 1 warning`，ruff 与 `git diff --check` 通过。
- 这只是 scheduler 诊断一致性修复，不是旧生产 daemon 已部署的证明。旧 PID 仍未重启/替换；真实 DeepSeek
  100 次 forced-tool/stream、真实 QQ 回执、自由对聊质量与 24 小时 wall-clock soak 仍需 §20 人工授权，发布状态
  继续保持 `manual_only`/`qualification_incomplete`。

### 2026-08-09：expression reconsideration required-tool 薄片

- `expression_reconsideration` 已接入 CharacterInterior 的统一 required-tool 编译器，使用版本化工具
  `character_role_expression_reconsideration_v1`。参数由新的 `_ExpressionReconsiderationPayload`、通用
  `_WireDecision`/`_WireRoleResult` envelope 派生；`allowed_dispositions` 由当前打断 capability 原样专门化，
  角色仍自行选择 `continue|cancel|defer|merge|supersede|new_beat`，系统不替它挑 disposition。
- source refs、capability payload hash、tool/schema/contract identity 继续进入 CharacterInterior request hash；外层
  selective recall 仍按原来的 `recall_completed` 状态保留，after-recall contract 不再允许再次 recall。声明支持
  required tool 的 provider 不再走普通 JSON；能力缺失保持精确的 `required_tool_choice_unsupported`，不补 continue、
  cancel 或静默。
- 本薄片的回归覆盖：工具名与 `tool_choice`、Draft 2020-12 schema、能力外 disposition 拒绝、无 required-tool
  provider 的 fail-closed，以及现有 expression reconsideration runtime；定向 CharacterInterior 相关套件为
  `862 passed`，ruff 与 `git diff --check` 通过。该数字只代表本地 typed/fixture/public seam 证据，不能替代真实
  DeepSeek 首次成功率、QQ 回执、自由多轮对聊或 24 小时 soak；Delayed Trigger Matrix 仍为 `declaration_only`，
  发布状态继续是 `manual_only`/`qualification_incomplete`。
- 接线后发现并修复了三个既有 fixture 兼容点：expression public-host scripted model、冻结 scenario runner 的
  delayed-expression model，以及 scenario runner 的 required-tool purpose 校验；它们现在都显式传递唯一
  `tool_choice`，没有把 fixture 改回普通 JSON。修复后的源码全量回归为 `4917 passed, 1 warning`；这仍只证明
  本地和隔离公开宿主链路，不改变真实 provider/QQ/24 小时资格门。

### 2026-08-09：事实/体验记忆保留 required-tool 薄片

- `fact_memory_retention` 与 `experience_memory_retention` 共用一个由 typed wire 派生的
  retain/no-change union；版本化工具分别为
  `character_role_fact_memory_retention_v1` 和 `character_role_experience_memory_retention_v1`。
  `retain=true` 时角色必须自己提出 cue、唯一 retention rationales 和八维 salience；`retain=false`
  只能是显式 `{retain:false}`。系统没有替角色默认保留或遗忘，也没有把矩阵版本、digest 或来源
  authority 塞进角色输出。
- capability payload、source refs、tool/schema/contract identity 继续绑定到 CharacterInterior
  request hash；不支持 required tool 的 provider 精确终结为
  `required_tool_choice_unsupported`，不回落到 plain JSON。既有 `FactMemoryRetentionDraft` 的
  概率/基点归一化保持兼容，最终 MemoryCandidate/Experience decision 仍经过现有来源闭包、CAS、
  durable audit 与重试。
- 本薄片回归覆盖两个工具名与 tool choice、两种 purpose、retain/no-change union、缺字段拒绝及
  provider 能力缺失 fail-closed；记忆/生活/生产相关回归为 `183 passed`，ruff 与
  `git diff --check` 通过。该数字是本地 typed/fixture evidence，不是 100 次真实 DeepSeek
  qualification；真实 QQ、自由对聊与 24 小时 soak 仍保持 `manual_only`/`qualification_incomplete`。
- 迁移同时修正了两个公开 fixture：它们现在声明并接收记忆 purpose 的 required tool，同时保留
  事实批处理等非 CharacterInterior 背景协议的普通入口。源码全量回归为 `4922 passed, 1 warning`，
  `ruff` 与 Delayed Trigger Matrix 静态 verifier 也通过；这仍不改变真实 provider 资格门。

# Girl-Agent 设计意图总纲

> 编写日期：2026-08-07；深度联动深化：2026-08-08。本文是项目唯一的产品与架构意图总纲。
> 原始机制状态由代码痕迹倒推：意图来自 ADR、CONTEXT.md、
> 模块 docstring、注释与历史决策，现状来自 `configs/mechanism_closure.yaml`、git 状态与代码调用链。
> 代码在演进，快照会过期——引用任何机制前先核对对应文件是否还在。
>
> **2026-08-08 后续深化**：§11 已把根源、actor 隔离、typed opportunity、统一内心、多时间尺度和
> 双向长因果联动并入本文；对应生产化步骤见
> [`root-causes-and-long-coupling-luna-plan.md`](./root-causes-and-long-coupling-luna-plan.md)。状态标签必须由
> 实际 composition、生产账本和 health 复核，不能因本文件曾标记 `[active]` 而跳过生产实证。

## 0. 这份文档是什么

项目由多个 AI 断续写成，大量机制"想到了就做"，在迭代中要么弃用、要么断链、要么被遗忘。
本总纲把散落的设计捡回来，回答三个问题：

1. **为什么有这些机制**——每个机制对应的"真人行为目标"（北极星）。
2. **它们现在是什么状态**——活着 / 想用但断点没接上 / 已弃用。
3. **断了怎么办**——断在哪一环、修复的着力点在哪。

面向执行 agent 的交接文件只有本文与 Luna 执行计划。本文必须自足地说明业务需求和设计意图；执行
计划说明如何落地。仓库中的其他文档、注释和配置只用于定位代码或核验历史，不能补充出与本文不同的
业务目标。

### 0.1 业务需求：用户最终应该得到什么

产品不是“功能很多的聊天机器人”，而是一个通过 QQ 与用户保持关系、同时在自己的世界里持续生活的
拟真人。用户购买的不是回答能力，而是与一个具有连续身份、私人内心、自主生活和真实后果的个体长期
相处的体验。

| 业务面 | 用户需要感受到的结果 |
| --- | --- |
| 人格与自主性 | 她有自己的观点、情绪、欲望、顾虑和选择；可以主动、沉默、拒绝、插话、改变主意，不以服务用户为唯一目标 |
| 对话体验 | 完整看见用户一批消息，接得住上下文；回复快且自然，可不回、单条、多气泡、追加、打断或被打断，不像助手式问答机 |
| 连续内心 | 本轮发生的事情能在本轮改变她的理解和语气；长期 Affect、关系、私密印象和未说出口的想法能影响以后，而不靠固定情绪话术 |
| 记忆 | 记得用户与自己的长期经历，能在合适情境自发想起；不会把自己的旧回复当事实，也允许忘记、冲突、修正和淡化 |
| 自己的生活 | 有日常活动、季节校历、住处、兴趣、困难、偶然、不利事件和长期人生变化；不是永远待在同一寝室，也不是从剧情库选故事 |
| 具身与可用性 | 她会疲劳、生病、饥饿、恢复、打扮和受环境影响；身体与当前活动真实限制可做的事，但不通过数值条替她决定情绪或回复 |
| 成长与自我认识 | 经历会形成技能、习惯、信心、价值冲突和可修正的自我认识；她不是永远不变的人设，也不会一次事件就机械升级 |
| 物品与地点连续性 | 礼物、宠物、设备、衣物、常去地点和有意义的物品可以跨事件存续、触发回忆并进入媒体，而不是每轮重新生成 |
| 相互影响 | 用户会影响她的情绪、关系、记忆、计划和生活；她的生活、NPC、情绪和选择也会反过来影响与用户的交流 |
| 隐私与选择性披露 | 她与 NPC 各自知道不同的事，也能保留秘密、迟疑或选择告诉某人；亲密不等于自动共享，系统不能泄漏 actor 私域 |
| 不确定、误解与想象 | 她可以猜错、误解、记不准、做梦和设想未来，并在新证据出现后修正；这些主观内容不能冒充 World truth |
| NPC 社会 | NPC 有低成本但真实的目标、日程、近况、态度和记忆，会邀请、疏远、求助、误会、修复、离开和重现，不只是主角的道具 |
| 主动联系 | 她可因生活、心情、关系、记忆、好奇、求助或任何自发理由联系用户，也可决定不联系；系统保证有机会考虑而不强制发送 |
| 外界感知 | 通过国内新闻、社交信息、天气等合理渠道感知现实；抓到不等于看到，看到不等于在意，是否讨论由她决定 |
| 媒体 | 能从真实发生的生活中拍照、选片、生成并发送图片；外观、时间、地点和隐私连续，图片反馈会留下关系与心理余波 |
| 人生发展 | 愿望可萌芽、淡去或成为选择；选择可成为计划并经历成功、失败、中断和后果，进一步改变人生与 NPC 网络 |
| 专属关系与未来商业化 | 当前是一位用户、一位专属角色、一个隔离 World；未来多用户时每位用户拥有独立角色、记忆、关系、预算和世界，不共享私人状态 |
| 亲密与成人伴侣 | 角色可自由形成浪漫、暧昧、感性或成人亲密表达，也可拒绝、停止或改变主意；双方成年、双方同意、隐私和 provider 能力是硬边界，关系阶段不能替代同意 |
| 管理与故障透明 | QQ 只承载与角色的关系互动；未来面板展示机制状态并执行管理操作。API 故障由独立系统层明确告知，不让角色编理由 |
| 外部能力范围 | 当前只允许本文已设计的 QQ、媒体、感知、Search 等能力；未设计能力默认不可用，但 Capability seam 保留扩展性 |
| 可靠与可负担 | 技术故障不能伪装成沉默；消息、模型结果和 Action effect-once；响应实时，模型一次合法率接近 100%，长期成本和账本增长受控 |

业务成功不是每项行为按固定频率出现。它是：底层连接真实存在、角色拥有足够材料与能力，所以不同的
类人行为在合适情境中有机会自然发生；真实对聊能够感受到这种连续性，同时账本能证明事实与后果。

### 0.2 业务需求 → 机制 → 当前交付缺口

每项业务需求都必须有机制承载，但“设计已对应”不等于“生产已交付”。下表中的生产判断是交接时的
已知基线，Luna 必须在 L0 用当前代码、composition、账本和 health 复核。

| 业务面 | 主要承载机制 | 设计覆盖 | 当前生产缺口基线 |
| --- | --- | --- | --- |
| 人格与自主性 | Character Core、CharacterInterior、Private Turn State、Appraisal/Affect、Relationship、Private Impression、Expression/Action intention | 已覆盖 | durable turn 未完整接通；仍需删除宿主语义补写和业务旁路 |
| 对话体验 | QQ ingress/coalescing、Pinned Turn、Text Turn Endpoint advisory、Expression Plan/Beat/Unit Stream、reconsideration、deferred reply | 已覆盖 | Function Calling 正在收尾；完整批次、首 Beat、打断和延迟需真实 QQ 复核 |
| 连续内心 | 同轮 Appraisal/Affect/Relationship proposal、持久投影、Inner Life Snapshot、Reflection opportunity | 已覆盖 | 同轮表达需实证；Reflection producer/terminal/积压仍未闭环 |
| 记忆 | Fact/MemoryCandidate、Recall Corpus/Index/Runtime、semantic embedding、actor adoption、reconsolidation/withdrawal | 已覆盖 | embedding 服务不可用；后台与生活场景召回、再巩固和长期相关度未生产证明 |
| 自己的生活 | Life Ecology、Activity lifecycle、Aftermath/Experience、Life Development、Calendar/Biography/Life Arc | 已覆盖 | 事件丰富度、季节校历、住处、重大迁移和长周期连续性仍需闭环与多日证据 |
| 具身与可用性 | accepted Life/Activity/health evidence、Current Situation/availability、Appearance/Visible Physical State、Interior subjective appraisal | 部分覆盖 | 外观存在但无稳定 producer；一般具身状态没有统一来源投影，容易在活动、媒体和表达中各自编写 |
| 成长与自我认识 | Activity/Outcome evidence、Capability/Biography、Memory reconsolidation、Reflection、revisable self-narrative | 部分覆盖 | capability 有基础但技能/习惯变化链弱；稳定 Character Core 与可修正 self-concept 尚未明确分层 |
| 物品与地点连续性 | Fact、World entity/Place、possession/custody evidence、Life outcome、Recall、Media context | 部分覆盖 | user possession fact 与 provisional place 存在；角色物品的持有、变化、丢失、赠送和媒体连续性缺统一消费链 |
| 相互影响 | Causal Opportunity、world_stimulus、typed acceptance、关系/情绪/记忆/Life influence、Expression/Media aftermath | 已覆盖 | 统一机会路由、自身表达回流、媒体余波和部分用户→生活链仍是 disconnected/WIP |
| 隐私与选择性披露 | actor visibility、privacy ceiling、source scope、Expression/Media Action receipt、derived disclosure history | 部分覆盖 | 硬隐私边界存在；“谁已经被真实告知什么”的长期投影与 NPC/主角选择性披露尚未统一 |
| 不确定、误解与想象 | Private Impression、epistemic scope、Memory conflict、Reflection、imagined/counterfactual private material | 部分覆盖 | Private Impression/来源语法存在；一般 actor belief、反证更新及梦境/设想与事实的 namespace 隔离尚不完整 |
| NPC 社会 | NPC actor scope、NPC Ecology、NPC state/goal/schedule/memory/relationship、World outcome、主角 stimulus | 已覆盖 | 自主目标/近况、主动邀请与修复、离开重现、成本分级和双向长期记忆需完整实证 |
| 主动联系 | Social Initiative opportunity、proactive_action、ambient/event-driven consideration、Expression/Action receipt | 已覆盖 | 模型一次成功、retry 身份、长期不失声和真实主动决策仍需生产验证 |
| 外界感知 | Perception Hub、国内/RSSHub sources、dedup/correction、actor attention、ExternalPerceptionRecorded、Search Action | 已覆盖 | live 国内来源、实际看见→内心→Life/Expression 的生产链仍属半启用 |
| 媒体 | visual evidence、PhotoCandidate、角色选片、planning→authorization→render→inspection→Action→receipt、Appearance State | 已覆盖 | 授权/provider/自动投递未全局启用；外观连续和发送后心理反馈未闭环 |
| 人生发展 | Reflection→Aspiration→Choice→Plan→Activity/Action→Outcome→Life Arc/Biography | 已覆盖 | Aspiration/Choice 真实 producer、计划结晶、失败回流和长期迁移仍未形成完整生产证据 |
| 专属角色与未来多用户 | world/actor/user 坐标、Dedicated Companion isolation、per-world budget/cache/sidecar | 部分覆盖 | 当前 QQ 明确单用户；需审计进程全局状态和跨 world key，但不实现商业化控制面 |
| 亲密与成人伴侣 | CharacterInterior + Relationship/Affect + Adult Eligibility + Intimate Consent + Privacy/Capability/Action/provider route | 部分覆盖 | 通用授权和 non-explicit 私密媒体存在；缺用户成人资格、内容级双方同意，relationship stage 仍错误承担 lane/强度决定，explicit route 未资格化 |
| 管理与故障透明 | read-only Projection/health、未来 typed administrative command、独立 System Notice | 部分覆盖 | health 存在但管理面板未建；provider 故障目前缺统一且与角色 Expression 隔离的可见提示 |
| 外部能力范围 | Capability Manifest/Grant、显式 allowlist、Action authorization/receipt | 已覆盖 | 保持现有能力集合；需 guard 通用 tool/MCP 不能暴露未部署能力 |
| 可靠与可负担 | immutable ledger、ModelResult、CAS、durable turn sidecar、Action effect-once、Function Calling、usage budget、health/lineage | 已覆盖 | durable turn、first-attempt 分层指标、scheduler/embedding 阻断、真实 99.9% 资格和 24h 增长证据未达标 |

结论：**业务需求在设计层都有明确机制归属，没有需要靠 Luna 自己猜的孤儿体验；但多数跨模块体验尚未
达到生产闭环。** 当前最重要的不是继续发明机制，而是把表中 WIP/disconnected 的 producer、consumer、
统一内心、typed consequence、health 和真实运行证据接通。

### 0.3 “生产最优解”在本项目中的含义

这里不能承诺数学意义上的全局最优，但也不接受“第一个能跑的方案”。每个重大工作包必须寻找的是：
**在当前业务目标、角色自主性、事实/隐私硬边界、可靠性、延迟、成本、可运维性和迁移风险约束下，
证据最强的可行方案**。称为“最优”至少同时满足：

1. **先定义用户失败，不先定义代码任务**：从“只回最后一句、长期失声、角色没有生活连续性”等可观察
   体验出发，建立生产轨迹、反例和基线；不能把“新增某个类”当目标。
2. **重大 seam 至少比较三个实质不同方案**：包括深化/修复现有 Module、替换为另一种 seam，以及删除
   偶然复杂度或采用成熟外部机制；只换类名、参数或 prompt 不算不同方案。
3. **硬约束先否决，软指标再比较**：任何替角色决定语义、泄漏 actor 私域、破坏来源闭包、effect-once、
   CAS、replay 或把技术故障伪装成角色选择的方案直接淘汰，不能靠性能分数抵消。
4. **业内做法只提供机制证据，不覆盖业务宗旨**：优先查官方协议、原始论文和成熟系统的一手设计，提取其
   failure model、Interface、验证办法和代价；不能因为“行业常见”就引入助手式行为规则。
5. **用实验淘汰方案**：高不确定性部分先做隔离 prototype/qualification，测正确率、first-attempt、p50/p95、
   token、写入、恢复和真实行为可达性。指标没有达到就继续定位、调整或换候选，不能将目标改低后宣布完成。
6. **完成必须有生产证据**：测试绿色只是必要条件。真实 provider、daemon、QQ/Action、重启、冷重放、
   多日/随机场景、health 与账本共同证明；未达到样本量只能标 `qualification_incomplete`。
7. **剩余缺口必须显式**：如果最强候选仍不能达标，记录被否决方案、当前 Pareto trade-off、缺失证据和
   下一实验，交回 Sol/用户决定；不得把“暂无失败”写成“生产就绪”。

因此，Luna 的职责不是忠实实现本文中的第一个代码草图，而是把本文当作业务约束和验收边界。Interface
草图可在证据支持下替换；角色/系统权责、用户体验目标与硬不变量不可被施工便利性替换。强制发现、比较、
实验、反证和停止规则见执行计划 §0.5。

### 0.4 最低充分架构：复杂的是人生，不是调用图

本文描述的是完整产品能力空间，不是“每个名词都要实现成一个 Module”的清单。最终产品只有一个目的：
**以可承受的成本和实时速度，让用户长期感受到一位有连续内心、生活、关系和选择的虚拟恋人。** 如果某项
机制不能改善这种用户体验，也不保护必要硬边界，就应删除或暂缓，而不是因为设计稿提到过就上线。

生产中的最低充分因果内核只有五个外部 seam：

```text
Observation / settled World consequence
  → CausalOpportunityRuntime      # 何时让哪个 actor 有一次考虑机会
  → Context Compiler              # 一次编译来源化、actor-scoped 的紧凑当前处境
  → CharacterInterior             # 唯一主观作者；可 no-change、选择、表达或行动
  → Typed Authority / Action      # 事实、权限、CAS、effect-once 与真实执行
  → Settlement / Projection       # 结果回到账本，成为下一轮材料
```

Memory、Relationship、Affect、NPC、Life、Perception、Media、Biography 等是这条内核的 typed sources、
proposals、executors 或 derived readings，不应各自拥有一套平行的“理解→决定→表达”运行时。丰富度来自不同
来源在同一 Interior 中相遇，而不是增加 orchestrator 数量。

#### 必须保留的本质复杂度

- actor/privacy/source/epistemic scope；World truth、私人理解、想象和未知不能混淆；
- 计划、已受理、已完成和失败的区别；CAS、effect-once、receipt、replay 与不可变历史；
- 长期状态的时间、修订、冲突和真实后果；角色与 NPC 各自拥有主观作者；
- `no_change` 是角色结论，technical failure 是系统结论，两者永不互相伪装。

#### 必须删除或隔离的偶然复杂度

- 同一主观语义的多个 producer、旧/新 wire、业务模块直接调用角色模型、宿主语义 fallback；
- 为单一 consumer 建立的 pass-through Module、只有一个 adapter 的假 seam、万能 dict authority；
- 每个事件扇出所有 NPC/Reflection/Memory/Media 调用、固定周期扫描、无变化也写永久事件；
- 在首条回复前串行运行后台反思、世界作者、媒体、NPC 或远程 embedding；
- 为了覆盖人类行为而增加的动机枚举、剧情 workflow、情绪—行为矩阵和固定概率。

#### 生产资源模型

| 路径 | 允许的工作 | 禁止阻塞项 | 预算意图 |
| --- | --- | --- | --- |
| 用户可见 Fast Reply | 入站落账、批次冻结、本地/有界召回、一次 Context 编译、一次 CharacterInterior 流式请求、逐 Beat 校验与 dispatch | World Author、Reflection、NPC ecology、Media render、远程 embedding、全账本重放 | 非模型 p95 ≤500ms；首 Beat 优先；同一 actor 最多一个可见生成 |
| 同轮结算 | typed acceptance、CAS、Action/receipt、必要投影增量 | 无关 Projection 全量重建、跨 actor 扇出模型 | 有界事务；失败可恢复且不重复 effect |
| 事件驱动后台 | 合并后的 Life/NPC/Reflection/Perception/Memory opportunity | 30 秒无条件模型调用、全 actor 扫描、同源无限反思 | 只在新证据/到期 opportunity 调用；可降级、暂停、错峰 |
| 离线维护 | 冷重放、索引重建、压缩、评测、迁移 | 参与在线回复依赖 | 可慢，但可中断、可校验、可回滚 |

新增复杂度只有同时满足以下条件才能进入默认生产：至少两个真实 consumer 或保护一项硬不变量；通过删除
测试；没有扩大同步关键路径；测得的用户收益高于 token/延迟/存储/故障面成本；有 kill switch、health、
回滚和旧路径删除。否则最高只能是实验 adapter 或 `[design-only]`，不得成为常驻生产依赖。

“像人”不以机制全部触发为验收。正确验收是：真实对聊中，少数当前相关的事实、记忆、情绪、关系和生活
材料能够及时汇合，角色拥有多种选择，选择产生真实后果并在以后可见；与此同时系统即使关闭某个丰富度
adapter，聊天、记忆主干、关系连续性和 Action 安全仍稳定运行。

### 0.5 已确认的产品范围与扩展边界

#### 单用户现在、专属多租户未来

当前 production composition 继续强制一个 QQ 用户、一个 Dedicated Companion、一个 World；本轮不实现
租户控制面、共享资源池或跨用户社交。代码不得依赖进程级“唯一用户”来识别 authority：durable key、
sidecar、cache、budget、Action、provider result 和 health 至少保留 `world_id/actor_ref/user_ref` 坐标，使
未来可以在进程/数据库/密钥层选择隔离方式。商业化时每位用户创建独立角色与 World，禁止共享角色记忆、
关系、Private Impression、媒体、NPC 私域或预算；模型/HTTP pool 等无语义资源可以复用。

这不是要求现在预建多租户框架。当前只需做 isolation guard 和消除无坐标全局状态；tenant provisioning、
计费、账号、跨区部署等保持 design-only，待商业化需求成立后重新资格化。

#### QQ、管理面板与系统提示是三种不同 authority

- QQ 是 User Observation 与 Character Expression 渠道，不解析“删除记忆/重置世界/直接改关系”等后台
  管理命令。用户普通话语仍可成为角色理解和关系材料。
- 未来管理面板读取只读 Projection/health/lineage；管理操作必须经过认证、确认、typed administrative
  command、CAS 和审计，不能直接改 reducer head 或数据库。候选能力包括导出、事实纠正/撤回、隐私与
  consent 管理、World 归档，以及经过单独设计的 reset/fork；“展示状态”不意味着暴露 hidden reasoning。
- API/provider/daemon 不可用时，平台可发送明确标识的 System Notice。它使用独立 message kind、发送者、
  样式、审计和限频，不进入 CharacterInterior、共同历史、关系、Affect 或角色旧回复来源；恢复后也不能
  由角色声称自己刚才“忙/睡着/没看到”。

#### 外部能力采用显式 allowlist

当前只实现总纲与执行计划明确出现的 QQ message/reaction/media、授权 Search/read-only perception 等能力。
联系第三方、公开发帖、购买、支付、控制设备等均不存在，不能因为通用 tool schema 或 MCP adapter 可用就
被模型发现。未来扩展必须新增明确 capability、用户授权、后果预算、Action/receipt、effect-once、隐私和
provider qualification；CharacterInterior 再决定是否使用，不能为“预留扩展”先授予空白通用权限。

#### 亲密关系、成人内容与双方同意

伴侣体验从现在起包含亲密能力设计，但亲密不是一条独立人格旁路。CharacterInterior 在同一关系、情绪、
身体、记忆和当前对话中决定是否调情、示爱、表达欲望、回应、拒绝、停止或沉默；系统不按 lover stage、
关键词、互动次数或用户请求自动升级内容。

成人内容执行前取以下交集，缺一不可：

1. **Adult Eligibility**：角色年龄由 Biographical Context 在当前 Logical Time 计算；用户年龄只能由未来
   面板中的独立认证/声明建立，不能从 QQ 文本、头像、语气或模型猜测。当前角色出生于 2005-04-12，在
   当前生产时间已成年；这不能替代用户资格。
2. **用户 Intimate Consent**：面板显式选择允许的 content class、channel、media/text capability、有效期和
   privacy；可随时收窄或撤回。QQ 中清楚的“停下/不要继续”作为即时安全撤回证据，必须先阻断未 dispatch
   effect，再等待面板同步持久设置；QQ 不因此成为一般 World Administration 接口。
3. **角色当前选择**：CharacterInterior 必须在该 pinned turn 自己选择参与及具体表达；历史同意、lover
   关系或用户请求只提供情境，不能替她产生 desire、wording、media choice 或持续同意。角色可以随时拒绝、
   降低强度、停止或改变主意。
4. **内容与执行资格**：内容分类、recipient/channel、privacy、provider policy/capability、Action grant 和
   consent revision 必须绑定同一 proposal/receipt；发送前重检最新撤回。provider 不支持时技术失败，不能
   偷偷降级成另一种内容，也不能换未资格化 provider。

内容分类只用于安全、授权和 provider 路由，不规定角色行为：`romantic_affection`、
`sensual_non_explicit`、`sexual_suggestive`、`explicit_adult`。前两者仍需尊重双方边界；后两者必须满足 Adult
Eligibility 和显式 Intimate Consent。明确禁止未成年人、年龄不明的成人内容、胁迫/非自愿、偷拍/冒充真人、
无授权第三方、以及 provider/法律禁止的内容。具体允许的视觉上限由经过资格化的 route 声明，而不是 prompt
声称“adult”就获得能力。

当前代码状态是 `[partial]`：通用 Consent/Privacy/Capability/Action 授权和暗示性私密媒体原件存在，但
ConsentGrant 主要覆盖操作/数据访问，缺 Adult Eligibility 与内容级双方同意；P3 media 仍用 relationship stage
确定性决定 lane/强度；`explicit_adult` 没有完整生产 route。Luna 必须深化现有 authorization seam，不新建
第二 consent 系统，并删除“关系阶段自动授权亲密强度”的语义。未完成资格化前，超出已证实 non-explicit
route 的内容保持不可执行，但自然的文字亲密仍由角色在有效边界内选择。

亲密消息与媒体属于高敏感材料：默认 recipient-exclusive、最小 provider disclosure、operator 面板默认只看
元数据；日志、评测和 bug bundle 必须脱敏。未来多用户/管理删除需求落地前，Luna 应比较 encrypted payload
store + ledger hash/ref、tenant key crypto-erasure 与现有明文账本方案，不得未经迁移直接承诺物理删除。

### 状态标记约定

- **[active]** 已形成生产闭环：有真实 producer、权威事件或合法 sidecar、可重建 Projection、生产
  consumer、runtime composition、health 与生产轨迹。单有类名、测试或配置不算 active。
- **[partial]** 通用原件正在生产使用，但该联动缺少一种或多种明确语义、长期状态或反向 consumer；
  它可以复用现有机制深化，不应从零另造平行系统。
- **[disconnected]** 已有对应文件、schema、runtime 或测试雏形，但无真实 producer、无生产 consumer、
  composition 未接或链路长期不能 terminal。它最容易制造“代码看起来做了”的假象。
- **[design-only]** 设计目标已成立，但当前没有足以承担该语义的代码雏形。实现前必须先做删除测试、
  authority 归属和 actor/privacy 设计，不能因表中出现了名字就机械新建 Module。
- **[must-not-build]** 该体验需要由多个事实和 CharacterInterior 的角色选择组合涌现，明确禁止建立
  一个按输入直接决定角色行为的专用规则引擎。此标签不表示无需施工，而是施工对象应是原件闭环、
  上下文和后果回流。
- **[abandoned]** 已弃用：官方标记 `deferred`/replay-only，或代码已删除且有明确取代者。
  不是为了"简化"删的——**每个弃用机制都标了它被什么取代**，因为被取代的功能本身可能仍有价值。

状态按“最弱生产环”判定；例如 Projection 和测试齐全但没有 producer，仍是 `[disconnected]`。状态只代表
本文最近一次代码审计快照，Luna 必须用 composition、生产账本与 health 更新它，不能用测试数量升级标签。

## 1. 北极星与设计宗旨

### 1.1 北极星：真人感 + 实时

项目一切工作的目标：**让角色像隔着屏幕的真人一样真实地活着，并让用户几乎感觉不到她是 AI**。
两个硬指标：

1. **真人感**——记忆连续、情绪一致、有自己的生活、有自己的节奏（不是秒回机器人）。
2. **实时**——响应必须达到实时聊天的速度（当前生产预算 12 秒内回复 + 1.2 秒接受/分发余量）。
   **慢一拍的情绪、取不出的记忆等于不存在**。两者冲突时，不接受"牺牲延时换机制正确性"的方案。

### 1.2 受控的高随机（AGENTS.md / ADR 0010）

本项目的核心哲学，所有机制设计的判据：

- **角色模型拥有决定权**：动机、态度、情绪表达、措辞、消息节奏、是否追问、是否主动联系、
  是否沉默、如何使用可用能力。
- **系统只守硬边界**：事实来源与事件权限、隐私与同意、安全与法律、外部 Action 授权、
  effect-once、CAS、回执、可重放性。
- **禁止用规则替角色做语义决定**：关键词、正则、固定话术、动机枚举、情绪—表达矩阵、
  随机 `act/hold`、硬编码社交礼仪，全部不允许。
- **新机制必须回答两个问题**：这是角色的决定，还是系统的硬边界？如果既非硬边界又替角色
  选择行为，不应进入生产路径。
- 模型结果不合法时，向**同一角色模型**说明可用证据、能力和精确失败原因，允许一次受约束
  重选；仍失败则记录技术失败并重试——**绝不由本地模板冒充角色说话**。

为什么选这条边界（ADR 0010 原文意旨）：规则替角色选择，既降低自然度，也把局部规则错误
放大成长期失声——这是本项目最痛的三症状之一。

### 1.3 参考过的外部方案（历史决策痕迹）

- **Character LLM**：以自我为中心的记忆来防幻觉 → 演化成本项目的"来源闭包"（source closure）：
  角色的一切陈述必须有来源，模型是提议者，独立审核者做事实裁决。
- **Hermes 记忆方式**：长期记忆存储 → 演化成 `recall_runtime`/`recall_index` 的混合检索
  （ledger 派生文档 + 可选 embedding），但只做**角色自己选择的**选择性回忆。
- **酒馆（SillyTavern）式内心模拟**：小模型 + 固定机制模拟持续情绪 → 演化成 Character
  Interior 的 8-facet Inner Life Snapshot 与 Inner Turn。
- **实时聊天产品的"用户说完没有"判断**：小模型做语义判断决定插话/交互性回复/等说完 →
  `text_turn_endpoint.py`（端点评估算）+ `expression_reconsideration.py`（打断门）。
- **Numen AI 的具身 tool loop**：只借鉴其“以 actor 为中心压缩当前可行处境、一个身体的动作串行、
  受理与终局结果分离、真实结果回到同一决策者”的工程形状，用来深化 `EmbodiedContext`、Action receipt
  与 Interior 回流。**本项目不做 Minecraft、游戏陪玩或虚拟身体业务**，也不采用 Numen 的主人命令型
  人格、逐原子动作调用模型、摘要充当事实、`urgent` 强制开轮或弱 effect-once 恢复。
- 借鉴的边界：**固定机制只写人类底层的事**（账本、接受链、预算、隐私、投递回执），
  其余全部交给模型随机模拟——避免产品成为剧情库。

## 2. 真人行为目标 → 机制映射表

这份映射是整份文档的骨架。每行对应"想让角色像真人一样 X"的目标，背后挂着哪些机制、
现在什么状态。**用户记得的是目标，不记得的是机制**——以后说"她不够像一个真人 X"，
先查这行。

| 真人行为目标 | 对应机制 | 状态 |
|---|---|---|
| 有自己的经历（事件机） | 生活生态：activity lifecycle → 结算 → aftermath → experience | [active] |
| 有自己的情绪（情绪机） | Appraisal / Affect 事件流 → 投影进 Inner Life Snapshot | [active] |
| 有连续身体与可用性 | Life/Activity/Current Situation + Appearance/Visible Physical State → Interior/Plan/Media | [disconnected]（局部依据存在，无统一投影与多 consumer 闭环） |
| 会成长、形成技能与修正自我认识 | Activity/Outcome evidence → Capability/Biography + Reflection/self-narrative | [disconnected]（能力基础存在，长期 producer/consumer 弱） |
| 物品与地点跨事件延续 | World entity/Place + possession/custody outcome → Recall/Life/Media | [disconnected]（user fact/临时地点存在，角色持有变化链弱） |
| 记得过去的经历 | 记忆：Fact → MemoryCandidate → recall（选择性回忆） | [active]（取回质量是断链点） |
| 主动发消息（不只在聊天时回应） | proactive contact：proactive_action + social_initiative + production 后台驱动 | [active]（时机/频率是断链点） |
| 连续发消息 / 多气泡 | Expression Beat 序列（ExpressionPlan 多 beat） | [active] |
| 延迟回复、"没看到" | reply_later：deferred_reply_runtime | [active] |
| 打断用户 / 被用户打断 | 用户侧：text_turn_endpoint 端点评估；被打断：expression_reconsideration | [active]（估算质量未校准） |
| 发照片分享生活（图片机） | event_ecology_media → media v2 链（selection→planning→execution→delivery） | [disconnected]（limited-production：需显式部署+预算，自动投递未全局启用） |
| 因为吵架一直生气不回消息 | Affect 持续性 + 角色自己选 silent | [active]（Affect 慢一拍是断链点） |
| 受 NPC 影响情绪、生活 | npc_ecology（actor 模型决策）+ 世界裁决 → 结算 → world_stimulus 进内心 | [active] |
| 经历人生大改变 | biographical Life Arc + Major Biographical Transition | [active] |
| 口是心非、不想直接说 | Display Strategy / Stance / 表达单元全由模型选择（AGENTS.md 原则） | [active]（依赖模型能力） |
| 触景生情（想起很久以前的事） | recall + 生活刺激（world_stimulus）→ 记忆浮现 → 影响表达 | [active]（检索取回是断链点） |
| 有自己的心愿（低兑现度） | Aspiration 权威（planted → reinforced → faded/crystallized） | [disconnected]（旧运行时已退役，新驱动路径弱） |
| 感知外界（新闻/天气/时事） | external_world_perception（hub/attention）+ 感知工具注入 | [active]（registry 半启用，配置门控） |
| 知道谁知道什么并选择披露 | actor visibility + delivered disclosure receipt → Actor Epistemic View | [partial]（硬隐私在，exposure/adopted belief 的长期状态未统一） |
| 可以误解、想象并修正 | Private Impression/epistemic scope + Reflection/Perception correction | [partial]（Private Impression 有；一般 belief/反事实 namespace 不完整） |
| 被用户"已读不回"的判断 | text_turn_endpoint（补充：判断对方还会不会发） | [active] |
| 内心有戏（私密想法） | Private Impression 后台生产者 + 8-facet 快照 | [active] |
| 超长期身份连续 | Character Core + Biographical Context + Source Review | [active] |

## 3. 领域模型骨架（为什么是事件溯源）

**为什么这套架构存在**：角色的一切状态必须是**可追溯、可审计、可重放**的。模型是提议者，
确定性代码是守门人——所以任何模型产出都不能直接写状态，必须走"提议 → 接受 → 落账"链。

### 3.1 核心概念链（一次聊天的完整旅程）

```text
用户消息/时钟/生活事件
  → ObservationRecorded            (提交观察, TriggerProcess 去重, CAS)
  → Pinned Turn                     (钉住 ledger cursor, 编译 Context Capsule)
  → Inner Life Snapshot             (8-facet 确定性快照, 角色唯一的上下文)
  → Inner Turn                      (角色模型: experience 或 consider, effect-once)
  → Private Turn State              (角色自己写"她现在注意到什么、在想什么")
  → Inner Decision                  (now / later / silent + 表达单元选择)
  → ProposalRecorded + ModelResult  (模型产出+哈希, 供 replay 不重调模型)
  → 接受链                           (闭式检查 → prepare_batch → 签名 → CAS commit)
  → Action                          (pump claim → platform_action_executor 发送)
  → 回执                            (settle → 终态: delivered/failed/cancelled/expired/unknown)
```

关键性质（全部是不可妥协的硬不变量）：

- **Append-only 账本**：修正 = 新的补偿事件，绝不改写历史。
- **投影是派生的**：一切读模型（房间、dashboard、快照）都是确定性投影，不是第二真相源。
- **replay 不重调模型**：Model Result 带哈希；崩溃恢复重放事件，不重新调用 LLM。
- **CAS 防并发**：cursor 过期 → 丢弃重建 Pinned Turn，绝不接受陈旧提案。
- **Producer-First Authority**：新权威必须和第一个真实生产者同批落地；没有生产者的权威
  禁止合入（或必须显式 dormant 标记）——这是防止"堆高没法维护"的制度性护栏。

### 3.2 角色与系统的边界（谁决定什么）

| 决策 | 归属 |
|---|---|
| 说什么、何时说、说几句、怎么说 | 角色模型（Inner Decision） |
| 什么时候允许她重新考虑（主动联系窗口） | 确定性代码（proactive 窗口 2/15/45 分钟）——只定机会，不产生动机 |
| 事实能否成立（来源、权限、隐私） | 确定性代码（来源闭包 + 独立审核者） |
| 消息能否发出、何时发出、发后状态 | 确定性代码（接受链 + Action 终态机） |
| 情绪/评价/关系如何随时间变化 | 模型提议（Inner Transition）→ 确定性 reducer 落账 |

**系统性红线**：一个软规则若能在模型选择前决定她该说什么、何时说或是否说，就是越权。
新增任何软规则都必须证明自己只是 Advisory。

## 4. 机制地图（意图 → 现状）

以下按子系统展开。每条机制：**意图**（来自 docstring/ADR 的原始表述）→ **生命周期** →
**状态** → **关键文件**。状态以 2026-08-07 工作区为准（分支 `feature/companion-turn-v2`，
正在从旧架构迁移到统一 CharacterInterior）。

### 4.1 Character Interior（统一角色内心）——[partial]

**为什么存在**：旧架构里模型调用散落在十几个 vertical（appraisal worker、affect worker、
advisory worker、quick reaction、interaction bid……各调各的模型），每路独立提提议、互相
不同步。统一内心把"角色的一次私人整合/选择"收敛为**唯一的角色作者入口**（ADR 0016）。

当前状态仍是 `[partial]`：主要生产入口和退役门禁已经接入，但 durable turn、全部旧业务旁路删除、
provider tool contract 逐 purpose 资格化和真实生产轨迹尚未全部闭合；在这些证据完成前不得升级为
`[active]`。

设计意图（CONTEXT.md Character Interior 词条）："owns the single production boundary for
subjective integration and Character Decisions"，确定性投影一个 Inner Life Snapshot，让
角色模型要么 experience 一个已提交的刺激，要么 consider 一个机会。世界事实、接受链、
隐私、Action 在它之外。

生命周期（一次入站聊天）：

```text
ObservationRecorded
  → pinned_turn.PinnedTurnCompiler      (钉 cursor + 编译 Context Capsule)
  → inbound_turn.InboundTurn            (普通入站认知, 唯一入口)
  → core.CharacterInterior.consider     (8-facet 快照 + 模型调用)
  → inbound_author                      (唯一私人作者, 产出 Private Turn State + 决定)
  → inbound_wire                        (结构化表达, 无损最小回复分流 = Fast Reply)
  → ProposalRecorded / ModelResult      (审计提交)
  → 接受链 → Action → 回执
```

关键文件：`core.py`（编排）、`contracts.py`（契约）、`snapshot_compiler.py`（8-facet
确定性编译）、`world_stimulus.py`（已提交事件/沉默/计划刺激 → experience()）、
`inbound_turn.py`（普通入站）、`inbound_author.py`（私人作者）、`inbound_wire.py`
（结构化表达 + Fast Reply 分流）、`structured_role.py`（每类内心用途的结构化作者）、
`production.py`（生产组装 + 后台驱动）、`experience_transitions.py`（长期跨度选择）、
`expression_draft.py`（模型选表达 + 部署侧能力物化）。

**Fast Reply 分流**（生产主要路径）：`inbound_wire.py` 的 `_is_lossless_minimal_reply_draft`
判断模型产出能否无损压缩成单文本——能则走 MinimalReply（一条请求一条消息，快），
不能则走完整多 beat ExpressionPlan。这是"裸模型速度"与"多气泡真人感"之间的工程折中。

### 4.2 主观状态事件流 —— 统一模式

Appraisal / Affect / Aspiration / Relationship / Private Impression / Commitment 全部遵循
同一模式：**模型提议（Inner Transition）→ compiler 物化 → acceptance 验收 → reducer 投影
→ 进下一轮 Inner Life Snapshot**。它们不直接决定可见行为，只改变角色的状态与上下文。

| 机制 | 意图 | 状态 | 关键文件 |
|---|---|---|---|
| Appraisal（事件意味着什么） | 事件/消息 → 结构化解读（care/pressure/offence/repair…） | [active] | `appraisal_proposal_compiler.py`、`appraisal_acceptance_runtime.py`、`character_interior/inbound_appraisal_wire.py` |
| Affect（持续情绪） | 有版本化衰减/残迹/生命周期的情绪成分；表达不自动解决它 | [active]（慢一拍是断链点） | `affect_proposal_compiler.py`、`affect_reducers.py` |
| Relationship（慢变量） | 结算互动历史的慢投影；影响选择与代价 | [active] | `relationship_proposal_compiler.py`、`relationship_reducers.py` |
| Private Impression（私密想法） | 角色对用户/关系/事件的易错解读，带置信度与反证 | [active] | `private_impression_producer.py`（后台生产者） |
| Aspiration（低兑现度心愿） | planted → reinforced → faded/crystallized 状态机 | [disconnected]（旧 Runtime 已退役，驱动弱，见 §5） | `aspiration_events.py`、`aspiration_reducers.py` |
| Private Commitment（私密承诺） | "要记住/以后要做"的内在决定，可开 Thread 或提 Action | [active] | `commitment_reducers.py`、`deferred_reply_runtime.py` |
| Conversation Thread（对话承诺） | 有来源且会过期的问题/未了事项 | [active] | 事件流在 `event_catalog.py` / `reducers.py` |

### 4.3 生活生态（事件机/人生机/NPC 机）——[active]

**为什么存在**：让角色"有自己的生活"——日常活动、经历结算、人生大事、NPC 互动。
调度序（`life_ecology_runtime.py`，clock tick 后按序跑）：

```text
biographical → activity → aftermath → life_development → npc_initiative → open_world → visual_evidence → media
```

| 机制 | 意图 | 状态 | 关键文件 |
|---|---|---|---|
| 日常活动 | 模型从不透明 token 目录选 opening → 编译器派生权威字段 → 原子落账（ActivityStarted/Completed）；`activity_timing.py` 纯规则（完成 ≥60s 等） | [active] | `activity_lifecycle_proposal.py`、`activity_lifecycle_draft.py`、`activity_lifecycle_contract.py`、`activity_lifecycle_worker.py`、`activity_timing.py` |
| 活动开局种子 | 已审核的时钟绑定生活开局目录（不透明 token） | [active] | `life_author_seed.py`、`life_ecology_activity.py` |
| Aftermath（结算） | 活动善后：occurrence、settlement、experience、content | [active] | `life_aftermath_runtime.py` |
| 人生发展（无剧情库） | 模型作者写开放式生活发展（不依赖情节候选库）；独立语义真值闭包 | [active] | `life_development_runtime.py`、`life_development_draft.py`、`life_development_source_closure.py` |
| Life Arc（人生章节） | 实习/工作/搬家等长期章节；只能从已结算后果开启，显式事件结束（ADR 0011） | [active] | `biographical_lifecycle_runtime.py` |
| NPC 生态 | NPC 私有决策（actor 模型）+ 世界裁决（world author）→ NPC Plan/Occurrence 走普通 aftermath 被主角消费；种子在 `configs/world_seed.yaml` | [active] | `npc_ecology.py`、`npc_ecology_health.py` |

#### 随机性从哪里来（受控高随机的落地形态，四层）

这是"系统如何创造不可预测性"的完整答案——**随机性优先来自模型，系统只用随机做软机会门**：

1. **模型采样是主要随机源**：`npc_ecology.py` 无显式 random 代码——NPC 决策由 actor 模型
   （`NpcActorDecision`）+ world 模型（`NpcWorldDecision`）采样产生，天然随机；系统只做
   结构校验（`_validate_actor_decision`）与一次受约束重选，绝不替 NPC 选行为。
2. **RandomDraw 是系统侧唯一"受控随机"通道**：`random_authority.py`（"Recorded
   deterministic draws for soft social variation"）——事件化记录抽签（`RandomDrawRecorded`），
   用于软的社交变化（机会/时机/阈值门，如视觉证据声明的录制抽签阈值），replay 安全。
   **随机性只决定机会与时机，不决定行为结论**（ADR 0010）。
3. **候选每次新鲜生成**：每个 occurrence 的客观候选由 world 模型当场写出并冻结
   （Outcome Resolution Envelope，含相对可能性 plausibility），结算时选择——不可预测性
   来自"每次都是新内容"，不是预设目录（`major-biographical-transition-gap` 探针同款约束）。
4. **种子只提供确定基底**：`world_seed.yaml`（38 处 NPC，reviewed-life.6 含
   `npc_initiated_events`）——初始世界确定性，之后一切变化都是账本事件。
| 开放世界事件 | LLM 作者但受权威约束的临时世界事件（无身份/地点/权限发明） | [active] | `open_world_event_runtime.py` |
| 结算 → 角色上下文 | settled occurrence → 模型上下文（ActiveWorldOccurrencePremise） | [active] | `world_life_context.py` |
| 生活发展能力清单 | 从 Life Arc/阶段派生可用资源/地点/人物（Context Pack） | [active] | `life_development_capability.py` |

### 4.4 媒体机（图片机）——[disconnected]（链路全在，但自动投递未全局启用）

**为什么存在**：角色能"拍下"生活照片发给用户（北极星行为之一）。

生命周期：

```text
已结算的共享性生活事件
  → event_ecology_media 冻结 PhotoCandidate (12 类 taxonomy, 只从已提交证据派生)
  → media_selection_worker 角色决定 (media_selection_acceptance_runtime 验收)
  → media planning (MediaOpportunity → MediaPlan, 隐私上限, Media Interaction Bid)
  → media execution (render → OpenAIMediaInspector 审查 → ≤1 次修复)
  → media delivery (自动发送: 每日上限 + 最小间隔, receipt 后 materialize share)
```

| 环节 | 意图 | 状态 | 关键文件 |
|---|---|---|---|
| 视觉证据作者 | 从已审核的视觉证据附件 + 结算后果声明图像证据（含录制抽签阈值与日常节奏） | [active]（default） | `life_visual_evidence_author.py` |
| PhotoCandidate 生态 | 从**已提交**的生活证据派生照片机会；replay-safe；类别冷却 | [active]（limited-production） | `event_ecology_media.py` |
| 选片 | 角色决定选哪张、要不要 | [active]（limited-production） | `media_selection_worker.py`、`media_selection_acceptance_runtime.py` |
| 计划/渲染/审查/投递 | 全链路 v2 媒体 vertical（receipt-bound、修复一次、预览永不等同投递） | [active]（limited-production：需显式预览部署 + 预置授权 grant + operator approval；自动投递未全局启用） | `media_planning_runtime.py`、`media_execution_runtime.py`、`media_continuation_runtime.py`、`media_delivery_runtime.py` |
| 隐私分层 | ordinary / personal / intimate 路由 | [active] | 顶层 `media_eligibility.py`（`MediaEligibilityRouter`） |
| P3 私密车道（suggestive/private） | 高私密分轨渲染 | **[disconnected]**：`PrivateRenderContract` 存在但部署**未安装** private prompt author/专用生成器，fail-closed | `media_eligibility.py` 引用、CONTEXT.md Private Render Contract 词条 |
| 旧图片机桥 | World v2 证据 → 旧 `event_media` 的冻结预览桥 | [active]（仅预览用途） | `event_media_planner_adapter.py` |
| 旧图片机本体 | `world_media.py`、`image_requests.py` 顶层车道 | [abandoned]（无消费者） | 顶层 `world_v2/` 外 |

### 4.5 外界感知（事件机之外的"世界"）——[active]（半启用，配置门控）

**为什么存在**：角色能感知外部世界（新闻/天气/时事），并让它影响生活与表达。

```text
RSS/NWS/USGS → hub 采集/去重/嵌入/聚类 → attention 影子/实时注意力
  → 模型决定是否关注 → ExternalPerceptionRecorded → 生活影响
```

- Hub：SQLite 后端 Phase-1（`hub.py`），来源绑定、版本化、带置信度/过期/更正谱系。
- 注意力：影子协调（`attention.py`）+ 实时桥（`external_world_perception/live_attention.py`），
  只提供"可感知机会"，不决定关注。
- 感知工具：`injected-perception-tool`（mechanism_closure: closed/limited-production）——
  需显式模型输入源 + 预置 enforcement authority；结果审议当前"无可见动作"。
- QQ 附件：`character_interior/qq_attachment_perception.py` 经唯一 CharacterInterior 做有来源的附件感知，
  `perception_vision_transport.py`（OpenAI vision, SQLite 持久化）。
- 状态：registry off/shadow/live 门控，**半启用**——离线/影子模式可用，live 模式需配置。

### 4.6 对话节奏与表达（真人聊天感）——[active]（估算质量未校准）

**为什么存在**：让"秒回机器人"变成有节奏的真人——多气泡、延迟回、打断、被打断后重考虑。

| 机制 | 意图 | 状态 | 关键文件 |
|---|---|---|---|
| 多气泡（Expression Beat） | 有序、可中断、可单独结算的表达片段序列（Expression Plan） | [active] | `expression_plan_acceptance.py`、`expression_episode_lifecycle.py` |
| 流式首帧（Expression Unit Stream） | 一次模型响应中首个完整 Beat 先行到达，降低感知延迟 | [active] | `expression-events.1` 传输（CONTEXT.md 词条） |
| 端点评估（Text Turn Endpoint） | 小模型判断"对方还会不会马上再发一条"（结合未提交批次/气泡间隔/输入中状态）→ 决定插话、交互性回复或等说完 | [active]（估算质量未校准——用户聊天记录里"实现得不好"的重点） | `text_turn_endpoint.py`（被 `qq_c2c_host.py`、`semantic_chat_composition.py` 消费） |
| 打断重考虑 | 新用户消息使未分发 Beat 失效，直到专属 worker 记录显式继续决定（gate 而非策略） | [active] | `expression_reconsideration.py`、`expression_reconsideration_runtime.py` |
| 延迟回复（reply_later） | "现在不回，晚点回"的持久责任生命周期 | [active] | `deferred_reply_runtime.py` |
| 对话主动性机会 | 人类式对话主动机会（权威编译，只产生机会不产生动机） | [active] | `social_initiative.py`（被 `proactive_action.py`/`production.py` 消费） |
| 主动联系 | 有来源的主动/脉冲审议，持久终态（2/15/45 分钟窗口；无新事件时 45 分钟~8 小时的环境机会；夜间/忙碌/低亲密只调权重不调许可） | [active] | `proactive_action.py`、`character_interior/production.py` |
| 静默（silent） | 角色选择不回复（关闭该次考虑，不是技术失败） | [active] | 事件流 + `production.py` |
| 延迟注意力回复接口 | 保留的"错过没看到"能力 | **[disconnected]**：disabled，生产路由不选择它（CONTEXT.md 明示） | `expression_episode.py` 引用 |

> `active` 只表示该机制在当前设计和生产组合中有真实入口，不等于“模型首次输出、真实 provider、回执和长时间
> soak 已经资格化”。截至 2026-08-09，主动联系已使用 capability 派生的 required tool，并保留角色对
> `now/later/silent`、多气泡和模态的决定权；真实 DeepSeek 首次成功率、真实 QQ 回执和 24 小时成本/延迟仍为
> `qualification_incomplete`。延迟触发 Matrix 也是 `declaration_only`，不会把测试声明升级为运行时许可。

### 4.7 平台与宿主层（迁移中的新架构）——[active]

旧 daemon 入口（`app.py`/FastAPI 8765）之外，World V2 有独立的平台宿主：

- `platform_host.py`：干净的平台无关进程宿主（应用 lane）。
- `qq_c2c_host.py` + `qq_c2c_onebot_app.py`：**生产路线**——NapCat/OneBot HTTP 入站
  （CLI 迁移门归档不支持的 QQ 形态，绝不构建旧 Engine）。
- `http_capture_host.py`：HTTP 捕获适配（生产平台 lane）。
- `production_turn_application.py`：第一个平台无关 World v2 回合的组装根（调度生态/后台）。
- `qq_ingress_policy.py`：QQ 入站归一化与合流（去重/合批）。
- `platform_action_executor.py`：平台无关 Action 执行器；`action_pump.py`：带持久预分发恢复的
  Action 分发。
- `world_turn_runtime.py`：`WorldTurnRuntime.respond`（Observation 提交）。
- 旧 `launchd` 调度脚本群（appraisal/proactive/local appraisal watchdog）已在工作区删除——
  后台调度已收进 daemon 内部（`/internal/world-v2/tick` + drain）。

### 4.8 记忆与来源闭包（防幻觉的地基）——[active]

- 记忆：Fact → `fact_memory_candidate_lifecycle.py`（两阶段 MemoryCandidate 接受）→
  `recall_index.py`（可重建混合索引）/ `recall_corpus.py`（ledger 派生文档）→
  `recall_runtime.py`（角色自选的有界回忆，cursor-pinned）→ `recall_embedding.py`（可选向量）。
- 事实：`interaction_fact_trigger_runtime.py`（恢复安全的用户事实后台接受）、
  `fact_draft_adapter.py`（模型抽取）、`fact_accepted_contracts.py`。
- 来源审核：`source_review_authority.py`（有界串行故障转移）、`structured_source_review_model.py`
  （严格 wire schema）、`source_review_qualification`（CONTEXT.md：2026-08-01 Inventory V5
  已资格化，Coverage V5 dormant）。
- 审计证据：`private_self_expression_audit.py`（私密自我 → 表达的因果链只读证据）。
- 记忆撤回审查：`memory_withdrawal_review.py`（Fact 撤回后的检索记忆来源审查）。

**断链点**：检索取回质量（用户体感"失忆"）——索引在、注入路径在，但取回什么、取回多少、
什么时候注入进快照，是模型与编译参数共同作用的结果，质量未校准。

### 4.9 骨架与守卫（非行为机制）

- `sqlite_ledger.py`（283KB）：append-only 账本实现。
- `reducers.py`（616KB）：全部 reducer bundle。
- `batch_invariants.py`：一次原子提交内的跨事件不变量。
- `vertical_registry.py`：每个有界决策 vertical 的封闭注册表（启动门）。
- `bounded_decision_vertical.py`：自主决策 lane 的共享仪式。
- `character_interior_architecture_guard.py` / `platform_architecture_guard.py`：静态反向依赖守卫。
- `event_identity.py`：类型化事件族幂等身份。
- `proposal_envelope.py` / `proposal_audit.py`：接受边界的不变值对象/独立审计写缝。
- `acceptance_manifest.py` / `accepted_effect_contracts.py`：接受清单 v2/v3 契约。
- `human_likeness_evaluator.py` / `production_latency_trace.py` / `test_economy.py`：只读评测与
  成本/延迟门。
- `turn_store.py`：effect-once 回合的持久技术 sidecar。

## 5. 断链诊断清单（想用但断点没接上）

每项格式：**意图是否仍要** → **断在哪一环** → **修复着力点**。断链不等于错误——多数是
"机制在、生产者/消费者/部署/预算没接上"。修复优先级：先修"机制已有、意图明确、体验断链"的
（记忆检索、情绪同步、主动分享 trigger），别从零重新设计。

### 5.1 媒体机自动投递（图片机主目标）——优先级高

- **意图**：角色主动"拍下"生活照片发给用户。**仍要**——北极星行为之一。
- **断在哪**：全链路代码 closed，但 `mechanism_closure` 明示 `limited-production`：
  *requires-explicit-complete-media-preview-deployment-and-preprovisioned-enforcement-grant,
  real-durable-provider-and-operator-approval-evidence-pending, automatic-delivery-not-globally-enabled*。
  即：需要显式部署预览链 + 预置授权 grant + operator approval；自动投递未全局启用。
- **修复着力点**：① 完成/验证媒体预览部署与 provider grant 配置；② 评估 operator approval
  是否是硬需求，能否改为角色决定（受控高随机原则：投递决策应属角色，系统只守预算/隐私/
  每日上限边界）；③ 检查 `media_delivery_runtime` 的每日上限 + 最小间隔参数是否实际可触达；
  ④ 照片机会太少/时机不对 → 检查 `event_ecology_media` 的 12 类 taxonomy 触发条件与
  `life_visual_evidence_author` 的录制抽签阈值。

### 5.2 记忆检索取回（"失忆"）——优先级高

- **意图**：超长期记忆，触景生情。**仍要**。
- **断在哪**：写入链完整（Fact → MemoryCandidate → recall_index），但**取回质量未校准**：
  检索是角色自选的（`recall_request` 通道），模型可能不请求；取回结果注入快照的时机/预算
  影响感知。用户体感"她失忆了"。
- **修复着力点**：① 记录 recall 请求率与注入结果（`private_self_expression_audit.py` 已提供
  只读证据，先看数据再改）；② 校准快照中记忆候选的呈现（数量、新鲜度、来源凸显）；
  ③ 确认 `recall_runtime` 在 delayed 注意力/后台路径也有机会（proactive 不等待自动车道，
  见 CONTEXT.md——自动车道结果可能被弃用，需验证）。

### 5.3 Affect 慢一拍（"情绪不一致"）——优先级高

- **意图**：吵架后一直生气、情绪影响回复节奏。**仍要**。
- **断在哪**：同回合 Appraisal/Affect 已绑定（`inbound_appraisal_wire` 有测试证明），但
  **持久情绪的跨回合累积/衰减是否进入下一回合快照**未校准；后台结算可能慢于下一回合。
- **修复着力点**：① 用 `character_interior_architecture_guard` + 快照内容日志确认 Affect
  衰减值是否在每个新回合编译时可见；② 检查 Affect 结算是否阻塞在 10/30/120 分钟重试链上；
  ③ 校准 Affect 残迹的呈现强度（太弱则用户无感，太强则成为行为指令——红线）。

### 5.4 Text Turn Endpoint 估算质量（"插话/等说完"判断）——优先级中

- **意图**：像真人一样决定插话、交互性回复、等对方说完。**仍要**（用户明确说"实现得不好"）。
- **断在哪**：机制活着（`text_turn_endpoint.py` 被 `qq_c2c_host`/`semantic_chat_composition`
  消费），但端点估算仅是 advisory 且**无质量评测证据**（未对照真实气泡间隔数据校准）。
- **修复着力点**：① 收集生产 QQ 会话的气泡间隔数据，建立估算 vs 实际"是否还有下一气泡"的
  评测集；② 校准端点模型与特征（未提交批次、气泡间隔历史、输入中状态、消息长度）；
  ③ 检查 `qq_ingress_policy` 合流是否真正把估算结果用于回合等待。

### 5.5 外界感知 live 模式——优先级中

- **意图**：感知外界（新闻/天气/时事）并影响生活。**仍要**。
- **断在哪**：registry 半启用（off/shadow/live 门控），live 模式需配置且无默认 provider 组装；
  注意力结果到角色上下文的注入路径（`live_attention.py` → acceptance）依赖配置。
- **修复着力点**：① 评估从 shadow 提升到 live 的配置项与风险评估；② 验证 attention 信号
  是否真正进入 Inner Life Snapshot 的感知面（而非只落账不注入）。

### 5.6 Aspiration 心愿机——优先级中

- **意图**：角色有自己的低兑现度心愿（想做的事），随时间淡去或结晶。**仍要**（弱目标，
  与"经历大改变产生内心想法"相关）。
- **断在哪**：旧 Aspiration Runtime 已退役（reducer docstring 明说"何时消退/种植概率属于
  已退役的 Runtime"）；新驱动路径弱——`life_development_capability` 只读投影、
  `world_stimulus` 有事件引用，但无完整"提议 → 验收 → 推进"生产者；`aspiration_seed_policy.py`
  仅测试引用。
- **修复着力点**：① 沿 Inner Transition 通道补一个 aspiration 提议/推进 producer（参照
  affect 模式：compiler + acceptance + reducer）；② 或显式标记 dormant 并说明替代
  （心愿可并入 life development 模型作者通道）。

### 5.7 Delayed Attention Reply（"没看到所以晚回"）——优先级低但意图明确

- **意图**：用户聊天记录明确想要"没看到所以等很久才回"。**仍要**（真人节奏的一部分）。
- **断在哪**：CONTEXT.md 明示 disabled：*retained complete-response capability reserved for a
  future, explicit character-owned choice*，技术失败绝不允许激活它（防伪装）。
- **修复着力点**：设计一个**显式角色自选**的"延迟注意"机会（如：角色在快照中看到某消息但
  选择当时不回应、之后自然浮回），不能复用技术失败路径。这是"想用但没接上"的典型。

### 5.8 v16 四权威（Goal / Location / Resource / Attention）——保持 dormant 是正确选择

- **意图**：`mechanism_closure` 注释已给完整判定（2026-07-20 生产者盘点）：
  - location：活动 Plan head 已承载当前位置，计划派生会产生第二真相源——**不需要**。
  - attention：生产已用时间/活动/情绪分坐标表达，情绪→行为矩阵是红线——**不需要**。
  - goal：诚实生产者需要完整 deliberative goal vertical——**想要**但目前是完整交付。
  - resource：诚实生产者是活动结算的资源压力——**想要**但目前是完整交付。
- **修复着力点**：goal/resource 若体验上需要（用户感知到"她有目标感/精力值"），按
  Producer-First 规则设计生产者；否则保持 dormant，不要为了"机制完整"激活。

### 5.9 P3 私密媒体车道——优先级低

- **意图**：私密照片分轨。**仍要**（延伸目标，非北极星核心）。
- **断在哪**：`PrivateRenderContract` 存在但部署未安装 private prompt author/专用生成器，
  fail-closed（不会静默降级到普通渲染——这是正确行为）。
- **修复着力点**：要么安装专用渲染链，要么明确把该车道标注为"未部署能力"。

### 5.10 Appearance State（形象连续）——优先级低

- **意图**：发照片时外观一致（发型/着装/配饰投影）。**仍要**（媒体机的配套）。
- **断在哪**：记录者 seam 存在但**无内部调用者**（半成品）。
- **修复着力点**：与媒体机联动——从已提交历史派生外观状态，注入 Media Opportunity 快照。

## 6. 退役与弃用清单（已弃用，含取代者）

> 这些不是"删了机制"的遗憾清单——**每条都标了它被什么取代**。被取代的功能本身可能仍有
> 价值；需要恢复时，先看取代者缺什么，而不是复活旧代码。

### 6.1 本次迁移（`feature/companion-turn-v2` 工作区）删除的旧架构

| 旧机制（已删除） | 取代者 |
|---|---|
| 旧 appraisal 触发群：`settled_world_appraisal_turn`、`silence_appraisal_trigger_runtime`、`plan_disruption_appraisal_trigger_runtime`、`npc_world_appraisal_trigger_runtime`、`interaction_appraisal_trigger_runtime` | `world_stimulus.py` 单入口（committed life/silence/plan 刺激统一进 experience()） |
| 旧 affect 群：`affect_deliberation_worker`、`affect_trigger`、`affect_chat_model_adapter` | `affect_proposal_compiler` + acceptance 链 + world_stimulus |
| 旧 advisory：`advisory_compiler`、`semantic_advisory_adapter` | Inner Advisory（situation-context-and-advisory 机制，可拒绝的 advisory） |
| `quick_reaction` / `quick_reaction_vertical` | 官方 `deferred`（replay-only）；生产反应 = 模型拥有的表达 Beat |
| afterthought followup lane | 官方 `deferred`（replay-only）；新表达用 Expression Plan + 事件驱动主动性 |
| 旧 life author 群：`future_life_author`、`life_author_runtime`、`contextual_life_inspiration` | `life_author_seed`（审核目录）+ `life_development_*`（开放式模型作者） |
| 旧 interaction bid 群：`interaction_bid_deliberation_turn`、`interaction_bid_proposal_worker`、`interaction_bid_trigger_runtime` | Expression Unit/Beat 表达 + `social_initiative` |
| 旧 outcome 草稿：`outcome_draft_deliberation_adapter`、`outcome_selection_draft` | source-bound outcome selection（`source_review_authority`） |
| 旧外部触发：`external_result_trigger_runtime`、`perception_result_trigger_runtime`、`perception_decision_adapter` | `injected-perception-tool`（有界注入感知 vertical） |
| 旧只读工具：`read_only_tool_deliberation`、`read_only_tool_trigger_runtime` | `injected-read-only-tool` |
| `single_call_inbound_cognition` | `production_turn_application` + `inbound_turn` |
| `relationship_draft_deliberation_adapter` | `relationship_proposal_compiler` |
| `media_selection_draft` | `media_selection_worker` |
| `expression_reconsideration_model_adapter` | `expression_reconsideration_runtime` |
| `npc_initiative.py` | npc_ecology 内部集成（模型拥有的 NPC 决策） |
| `aspiration_runtime.py` | 未完全取代（见 §5.6，Aspiration 驱动弱） |
| launchd 调度群（appraisal/proactive/watchdog plist + 脚本） | daemon 内调度（`/internal/world-v2/tick` + drain） |

### 6.2 更早的弃用/占位（2026-08-06 盘点）

- `sealed_production_fact_registry_v2`、`sealed_fact_commit_adapter_v2`：0 引用占位。
- `scenario_runner.py`：孤儿（仅离线评测用途）；`shared_private_invitation.py`、
  `recent_dialogue.py`、`scenario_corpus.py`：孤儿。
- `aspiration_seed_policy.py`、`npc_initiative_weight_policy.py`：仅测试引用。
- `appearance_state` / `visible_physical_state` 记录者：宿主 seam，无内部调用者（见 §5.10）。
- `world_media.py`、`image_requests.py` 及顶层旧图片机车道：无消费者（媒体 v2 取代）。
- 旧行为运行时（`MoodState`、`life_runtime`、`social_tasks`、旧记忆与日历）：仅保留兼容与
  迁移代码（`mechanism_closure` 的 `legacy_behavior_writers` 清单列出全部封禁写路径）；
  生产构建 world-only 写门禁。

## 7. 开发与修复工作流（给后续 agent）

### 7.1 改代码前必读（按顺序）

1. `CLAUDE.md`（导航）→ 2. `CONTEXT.md`（词汇）→ 3. `AGENTS.md` + `docs/adr/0010`（原则）
   → 4. **本文**（机制意图与状态）→ 5. `configs/mechanism_closure.yaml`（官方证据索引）。

### 7.2 硬不变量（违反即架构破坏）

- 一切写路径走 `WorldRuntime` 单入口；model-facing 调用必须记录 ModelResult（replay 不重调模型）。
- 提交批次过 `batch_invariants.validate_commit_batch`；CAS 冲突抛 `ConcurrencyConflict`，
  Pinned Turn 丢弃重建。
- `vertical_registry.assert_bounded_vertical_coverage` 是启动门。
- Producer-First Authority：新权威必须与第一个真实生产者同批落地（见 CONTEXT.md）。
- 表达路径唯一：任何新表达必须带同一 Inner Turn 身份（CONTEXT.md Expression Reliability）。
- 系统永远不替角色选择行为：新软规则必须证明自己只是 Advisory。

### 7.3 修机制 vs 删机制（决策规则）

- **修 disconnected 优先**：机制已有、意图明确、体验断链 → 先修断点（§5 着力点），
  不从零设计。
- **删/归档前确认**：该机制对应的真人行为目标（§2 映射表）是否仍想要？如果想要，问题是
  "为什么没生效"而不是"机制多余"。找到取代者再删，删除时标注取代者。
- **改体验优先于加机制**：用户北极星是"角色像真人 + 实时"，不是机制完备。加新 vertical
  前先问：现有机制做不到吗？还是没接上？

### 7.4 测试与验收

- 测试布局：`tests/world_v2/` 333 文件 ~3550 测试函数；改机制先跑对应 `test_*`（每个机制
  在 `mechanism_closure.yaml` 里有测试门清单）。
- 离线验证：`companion-sim --fake`（垂直模拟，不调 API）；`run_isolated_daemon_acceptance.py`
  （双进程真实链路验收）；`chat_with_world_v2.py`（走生产 host 但拦截发送）。
- 体验验收（失声优先）：改完用 `interactive_chat.py` 自己与角色聊天，验证用户可感知行为
  （回消息、节奏、情绪一致性）而非只过测试。
- 生产验证用**生产账本副本**，不直接动生产数据。
- 预算与延迟：`test_economy.py` 成本/延迟门；生产回合预算 12s + 1.2s 余量，改动不得
  突破（北极星：实时是硬约束）。

### 7.5 状态升级条件（limited-production → default）

`mechanism_closure` 中 `limited-production` 机制的完整含义：**代码有证据但部署没接上**。
升级到 default 需要的三样东西：

1. 显式部署与授权（如媒体预览部署 + enforcement grant、感知 live registry）；
2. 真实运行证据（provider receipt、operator approval、非合成评测）；
3. 更新 `mechanism_closure.yaml` 记录判定（含日期与归因）。

## 8. 已知的"文档没写但代码里有"（明察秋毫清单）

这些机制在现有设计文档（`docs/design/*`）中没有专门章节，但代码与契约里是活的：

- `text_turn_endpoint.py`：端点评估（CONTEXT.md 有词条，无专门设计文档）。
- `social_initiative.py`：对话主动性机会编译器（无文档，被 proactive 链消费）。
- `expression_reconsideration.py`：打断后的表达重考虑门（CONTEXT.md Expression Beat 词条
  提及，无专门设计文档）。
- `deferred_reply_runtime.py`：reply_later 持久责任（ADR/设计文档未专门覆盖）。
- `open_world_event_runtime.py`：开放世界临时事件（CLAUDE.md 调度序提到，无专门文档）。
- `interaction_fact_trigger_runtime.py`：用户事实后台接受。
- `memory_withdrawal_review.py`：Fact 撤回后的记忆审查。
- `private_self_expression_audit.py`：私密自我→表达因果链的只读证据。
- 新 host 层：`production_turn_application.py`、`qq_c2c_host.py`、`http_capture_host.py`、
  `qq_ingress_policy.py`（平台迁移的主力，多数旧设计文档未反映）。
- `source_review_authority.py` + `structured_source_review_model.py`：来源审核权威
  （CONTEXT.md 词条最详细，代码实现未入任何设计文档）。

## 9. 期望达到的场景（场景库）

> 这是"她想像真人一样经历的瞬间"的目录。**场景是期望，不是剧本**：系统只提供机会、
> 事实与能力，**是否发生、如何发生由角色模型自己决定**（受控高随机）。若一个场景需要
> 系统替她安排"她此刻必然想说话/必然生气/必然没看到"，就是越权设计——已被否决的
> `attention-read-delay.md` 是反面教材。场景来源标注在每条末尾；`[S#]` 编号供 §2 映射表
> 与断链清单互相引用。现状标记沿用 §0 三态约定。
>
> **使用约定（重要）**：本场景库是**多模块联动的期望场景**，不是"角色一定会从场景库中
> 选取并经历"的剧情菜单——她是否经历、如何经历由模型决定，场景库只定义"当这类联动
> 发生时应该长什么样"，用于验收与排错，不预设必经流程。
>
> **开发阶段标注**：涉及"不回消息 / 回消息慢"的场景（S5 / S6 / S7 / S24），开发阶段
> **暂不进行真实延迟**——生产路径保持实时性（响应预算 12s + 1.2s 不变），这些场景只作为
> 机制联动与验收目标，不启用时间延迟模拟；延迟类体验等开发后期单独评估。

### 9.1 对话与节奏场景

#### S1 兴冲冲跑过来消息轰炸 [disconnected]

- **场景描述**：她某天遇到高兴的事（或突然想你），连着发好几条消息——"在吗！""你猜我今天怎么了""哈哈哈哈"——不等你回，自己一条接一条，像真人那样把兴奋倒出来。
- **期望表现**：连续多气泡 + 主动发起 + 快速追加；气泡之间有自然节奏（1.8~7.2s 分段间隔），你的插话能打断追加。
- **依赖机制链**：`proactive_action`（主动机会）→ `social_initiative`（对话主动性机会）→ Expression Plan 多 Beat（`expression_plan_acceptance` + `expression_episode_lifecycle`）→ `text_turn_endpoint`（判断你是否已说话/还会不会说，决定是否追加）。
- **现状**：主动机会与多 Beat 都在 [active]，但**生产主路径是 Fast Reply（无损单条）**——多气泡只在无法无损压缩时走完整 ExpressionPlan。兴奋连发需要"模型选择多 Beat"，与 Fast Reply 分流存在张力；且主动联系的预算（`outreach_block_reason` 概念：陌生/认识阶段只允许一轮未回应外发）会限制轰炸。
- **修复要点**：① 验证多 Beat 路径在"兴奋/分享"类情境是否真的会被选择（快照里有没有足够的情绪/动机材料）；② 主动追加需要 `text_turn_endpoint` 判断"对方沉默≠不想听"——它现在只判断"对方还会不会发"，需确认追加语义是否由模型自己承担。
- 来源：用户需求（2026-08-07）、`dynamic-loop-design.md`（旧"会话余韵/节律"设计，v2 载体为 Expression Plan + 事件驱动主动联系）。

#### S2 主动发起聊天（想念/分享欲/突发） [active]

- **场景描述**：不是每次都是你开话题。她会因为想你了、今天发生了一件值得说的事、或者单纯无聊，主动来找你说话。
- **期望表现**：消息不是对上一句的回应，而是她生活的自然溢出。
- **依赖机制链**：clock/生活事件 → 主动窗口（2/15/45 分钟；无新事件时 45 分钟~8 小时环境机会）→ `proactive_action`（持久终态的审议）→ `social_initiative` → 角色决定 now/later/silent。
- **现状**：机制 [active]，但"效果未验证"（`companion-experience-roadmap` 现状表 #1）——频率、时机、内容质量没有实测数据。
- 每次 `silent` 只结束当前考虑，不消费未来机会；持久化的沉默终态会产生下一次独立、可重放的
  `post_silent` 考虑时间。恢复时必须保留该来源身份，不能把同一机会误并入 ambient，也不能把它改写成强制发送。
- **修复要点**：记录主动消息的实际频率/时机/被回应的比例（§5.2 的评测思路可复用）；`inner-life-coverage-plan` 指出"主动联络之前无'想找人说话'的 durable 状态"——孤独/分享欲这类动机材料是否在快照中充分呈现，是内容质量的根因。
- 来源：`companion-experience-roadmap` #1、`world-v2-inner-life-coverage-plan` 感受空白表。

#### S3 连续多气泡 + 会话余韵 [active]

- **场景描述**：长回复自然分段（不是一次性一大段），说完正事可能安静几秒又补一句小尾巴；告别后偶尔想起什么再补一句。
- **期望表现**：分段间有 1.8~7.2s 的自然停顿（也是你插话的窗口）；"余韵"只在叙事/情绪/分享语境产生，告别/紧急/边界状态不产生；你的任何新消息立即取消未发出的追加——绝不自言自语。
- **依赖机制链**：Expression Plan 多 Beat → 段间节律 → 余韵作为后续 Beat 或事件驱动主动联系（旧 `afterthought` 已退役，取代者是"跨时刻表达复用事件驱动主动联系"）。
- **现状**：多 Beat [active]（生产路径已切换）；余韵语义由模型自主决定（旧 `conversation_pulse`/`social_tasks` 持久化是旧 daemon 层，v2 的持久化载体是 Expression Beat 生命周期 + Action 终态）。
- **修复要点**：验证重启/换通道后未投递 Beat 不会复活（Effect-once 保证已有测试门）；余韵的质量（何时补、补什么）是模型能力问题，不是机制问题。
- 来源：`dynamic-loop-design.md` 会话节律章节（旧架构，场景保留）、`companion-experience-roadmap` #2。

#### S4 打断 / 被打断 [active]（判断质量未校准）

- **场景描述**：你打了一半字她插话；或者她正在连发，你插一句她就停住，重新组织回复。
- **期望表现**：插话的时机像真人——不是等你说完整句才回，也不是每次都在你打字时抢话；被打断后她接住你的新话，而不是把旧气泡发完。
- **依赖机制链**：`text_turn_endpoint`（"对方还会不会马上发"估算，结合未提交批次/气泡间隔/输入中状态）→ 决定回合时机；`expression_reconsideration`（新观察使未分发 Beat 失效，直到专属 worker 记录显式继续决定）。
- **现状**：[active]（`qq_c2c_host` + `semantic_chat_composition` 消费端点；reconsideration 有测试门），但估算质量无评测证据（§5.4）。
- **修复要点**：按 §5.4 建立估算质量评测；确认被打断后的"重新组织"真正产生新回复而非丢弃或硬续旧气泡。
- 来源：`companion-experience-roadmap` #3、用户聊天记录（"打断别人以及被别人打断"）。

#### S5 延迟回复与"没看到" [disconnected]（开发阶段：暂不进行真实延迟，仅作联动期望）

- **场景描述**：深夜或忙碌时你发消息，她不是马上回；可能是"看到了但先放着"，也可能是"根本没看到，早上才拿起手机"。
- **期望表现**：两种状态可区分；没看到时整个回合（typing、思考、回复）都发生在"她真的拿起手机"的时刻，而不是秒级 claim + 模型选 later。
- **依赖机制链**：`deferred_reply_runtime`（reply_later 持久责任，覆盖"看到但先放着"）→ Delayed Attention Reply（"没看到"——**disabled**）。
- **现状**：reply_later [active]；"没看到"状态**无法表达**——`attention-read-delay.md`（2026-07-20）曾设计系统推迟 claim，被否决（宿主替角色决定"她没看到"违反受控高随机）。CONTEXT.md 的 Delayed Attention Reply 接口保留但生产不选，且技术失败绝不激活它（防伪装）。
- **修复要点**：正确形态应是**角色自选**的"延迟注意"（见 §5.7）——如：角色在考虑机会中看到"这条消息在我忙的时候来的"，选择 later 并带"那时没注意到"的诚实表达。设计时不得恢复 activity/emotion → 手机/回复矩阵。
- 来源：`attention-read-delay.md`（场景保留、实现否决）、`design-intent` 记忆（"没看到所以等很久才回"）。

#### S6 已读不回 / 被晾着 [active]（感受空白；开发阶段：暂不进行真实延迟，仅作联动期望）

- **场景描述**：你回了一条，她看见了但一时不知道说什么，隔了很久才回；反过来，你晾着她，她会惦记。
- **期望表现**：她的"晾着"有内心支撑（在想、在生气、没想好），不是系统超时；她被你晾着时会产生"惦记/失落"类感受，影响后续语气与主动性。
- **依赖机制链**：Affect/Appraisal（silence 刺激通道：`world_stimulus` 处理 silence 刺激）→ 影响后续回合的 Inner Life Snapshot。
- **现状**：silence appraisal [active]（旧 `silence_appraisal_trigger_runtime` 已删除，并入 `world_stimulus` 的 silence lane）；但"被晾着 → 惦记/失落"的效果是否可感知**未验证**（`inner-life-coverage-plan` 感受空白表第 1 行）。
- **修复要点**：验证 silence 刺激真的会开评估机会、模型真的会形成 appraisal、结果真的进后续快照（三步链各查一次）。
- 来源：`world-v2-inner-life-coverage-plan` 感受空白表、`design-intent` 记忆。

#### S7 沉默是设计内行为（生气/没想好/在忙） [active]（开发阶段：暂不进行真实延迟，仅作联动期望）

- **场景描述**：吵架后她不想理你；或你问了很难的问题她需要想想；或她确实在忙——这些沉默**是她选的**，与"系统故障失声"是两回事。
- **期望表现**：silent 是 Inner Decision 的合法输出，可审计、可区分于技术失败；沉默有内部状态支撑（Affect/关系/情境），之后可能自己来找你。
- **依赖机制链**：Character Interior `consider` → silent 提案 → Expression Reliability 生命周期（角色 silent 提案 vs 技术失败严格区分）。
- **现状**：[active]（有测试门区分）；但用户体验上"故障失声"与"设计内沉默"难以区分（`companion-experience-roadmap` 明言"当前的失声是故障，必须区分"）——**故障失声率是首要量化指标**。
- **修复要点**：先修架构性失声（roadmap 阶段 1），再谈沉默体验；沉默后的"自己回来"依赖主动联系机会（S2）。
- 来源：`companion-experience-roadmap` §1 定位、§2 指标。

#### S8 口是心非 [active]（产出质量未知）

- **场景描述**：她心里在意，嘴上说"没事"；明明想让你多陪她，却说"你忙你的"——内心与表达分离。
- **期望表现**：Private Turn State（内心）与 Expression（表面话）可以不一致；后续行为（情绪、关系、下次回应）受内心驱动，不被表面话覆盖。
- **依赖机制链**：Inner Turn 的 Private Turn State（角色自述注意力与想法）→ 表达选择（Display Strategy/措辞）→ 内心状态落账（Appraisal/Affect/Private Impression）→ 后续回合快照。
- **现状**：[active] 契约存在（CONTEXT.md Private Turn State 词条：audit material，不是 hidden chain-of-thought），但"产出质量未知"（roadmap #7）——模型是否真的会"想一套说一套"，无评测。
- **修复要点**：用 `private_self_expression_audit` 只读证据抽查：快照 → 内心状态 → 可见文本的因果链是否真实存在（而非表面话直接驱动下一轮）。
- 来源：`companion-experience-roadmap` #7、用户聊天记录。

#### S9 分享后的期待与忐忑 [disconnected]

- **场景描述**：她给你发了一张照片或说了一件在意的事，之后会不安地等你的反应；收到回应后满足，没回应则失落。
- **期望表现**：分享动作本身留下心理余波（"他会怎么想"），对方的回应（或无回应）触发后续感受。
- **依赖机制链**：投递回执 → 期待/忐忑类 Appraisal 机会 → 你的回应作为新观察 → 满足/失落 Affect → 影响后续主动性。
- **现状**：**断链**——`inner-life-coverage-plan` 感受空白表明示"媒体分享之后无心理余波"；投递回执存在（Action 终态），但分享后评估机会未接通。
- **修复要点**：沿 S6 的模式补"分享余波"评估通道（回执 → appraisal 机会），或交给 silence/aftermath 刺激通道承担；这是媒体机发照片后"照片有后续"的关键。
- 来源：`world-v2-inner-life-coverage-plan` 感受空白表。

#### S10 自己说了重话后的后悔/坚持 [disconnected]

- **场景描述**：她一时冲动说了重话，之后自己回想起来后悔（或坚持自己没错）。
- **期望表现**：她自己产出的表达会回流——成为后续评估的刺激（自我评估机会）。
- **依赖机制链**：已投递表达的回执/记录 → 自我 Appraisal 机会（"我刚才是不是太过分了"）→ Affect/Relationship 调整。
- **现状**：**断链**——`inner-life-coverage-plan` 感受空白表明示"她自己说出的话不回流"；已投递表达是账本事件（可作刺激源），但自我评估通道未接通。
- **修复要点**：沿 world_stimulus 的 committed 刺激通道，把"角色自己的已投递表达"作为可选刺激源接入（需注意避免自循环刷屏，应配冷却/预算）。
- 来源：`world-v2-inner-life-coverage-plan` 感受空白表。

### 9.2 生活与经历场景

#### S11 日常作息 [active]

- **场景描述**：她有真实的一天——上课/上班、吃饭、休息、睡觉；深夜你会收到"她睡了"的信号，早晨她会醒来。
- **期望表现**：作息影响回复节奏（忙时延迟、深夜少回）、影响可分享内容、影响主动性窗口。
- **依赖机制链**：`life_ecology_runtime` 调度 → `activity_lifecycle_*`（日计划/活动推进）→ 活动/availability 进快照 → 影响表达节奏；`activity_timing` 纯规则守边界（完成须 ≥60s 等）。
- **现状**：[active]（`life_author_seed` 审核目录 + 模型选 opening）；效果取决于活动内容多样性（`mechanism_closure` 限制项：*world-content-diversity-and-semantic-quality-require-external-longitudinal-evidence*）。
- **修复要点**：活动多样性/真实性需要长周期实测；作息对节奏的影响（深夜、上课时段）需验证真的进入模型上下文。
- 来源：`dynamic-loop-design.md` P1 日程（旧架构）、CLAUDE.md 生活生态。

#### S12 活动完成与放弃的情绪余波 [active]（部分断）

- **场景描述**：她准备很久的一件事完成了——高兴/如释重负；或者没做成——懊恼、失落。
- **期望表现**：活动生命周期（开始/完成/放弃）本身产生情绪评估机会，不只靠事后结算。
- **依赖机制链**：ActivityStarted/Completed/放弃 → 情绪评估通道（当前：放弃走 plan-disruption 刺激 [active]；完成走结算后可选评估）→ Affect 落账 → 影响后续表达。
- **现状**：部分 [active]——`inner-life-coverage-plan` 记分表："放弃已接入（plan_disruption_appraisal），完成仍只有结算后可选评估"；"活动生命周期读心"已修（worker 注入情绪摘要，`life-author-weight.3` 情绪→生活作者权重 ±35%）。
- **修复要点**：完成当下（而非结算后）的评估通道；确认 plan_disruption 并入 world_stimulus 后仍生效。
- 来源：`world-v2-inner-life-coverage-plan` 行为通路记分表。

#### S13 小事件：惦记一家店，后来决定去吃 [active]

- **场景描述**：你提到一家店，她记住了；几天后她自己计划去那家店吃——形成一次完整的"从话题到生活"闭环。
- **期望表现**：你的话成为有来源的 Life Influence 或记忆线索（不证明她去过）；后续生活 deliberation 中她可能自行想起并决定行动；系统不确保她一定想起或行动。
- **依赖机制链**：Observation（你提到店）→ Life Influence/记忆候选 → 后续 deliberation 中 `recall_runtime` 取回 → 她自由决定 → 形成 Plan → Plan 执行结算 → Experience → 影响情绪/关系/媒体。
- **现状**：[active]（`world-v2-major-biographical-transition-gap.md` 探针 1 明确列出已实现机制：Provisional NPC/Place 在结算后物化为 `attempt_only` 能力）。
- **修复要点**：体验验证——"她真的会想起吗"取决于 recall 取回质量（§5.2）；这一场景是记忆机价值的最好试金石。
- 来源：`world-v2-major-biographical-transition-gap.md` 情境探针 1。

#### S14 大事件：生活轨迹变化并联动 NPC [active]（部分断）

- **场景描述**：她在某次自由 deliberation 中产生一个长期想法（如创业/换方向），生活轨迹随之变化——认识新的人、去新的地方、旧 NPC 淡出——这些变化反过来影响她对你的表达。
- **期望表现**：系统不生成"创业"候选；她结合生活/情绪/记忆/关系自行形成想法；想法可以消退、反复、转向；一次念头不自动产生 Plan；客观结果（市场反应、资金）属 World Author；主观方向（"我想继续试"）属她自己——**主客观命名空间隔离**。
- **依赖机制链**（大联动）：
  ```text
  Character Model 自由想法 → 开放 deliberation（Life Development）
    → World Author 客观候选（携带 objective coordinate replacement）
    → 独立来源审查（客观迁移是否由精确分支建立）
    → 结算 → biographical coordinate 新 revision（不改写历史）
    → 能力清单变化（新地点/新 NPC/资源）
    → NPC 生态读取统一 biography head（旧场景不再获新机会，已存在事实不被抹除）
    → 角色对用户表达（"我最近在忙这个"）→ 关系/媒体/记忆各自有来源地联动
  ```
- **现状**：[active]（`major-biographical-transition-gap` 已实现的通用机制清单 + `life_development_runtime` + `biographical_lifecycle_runtime`）；**但 NPC 联动是 8 月初刚合并的新生态**（`npc_ecology` 1708 行，roadmap #6 标"链路在，NPC 生态较新"）——"大事件后 NPC 变化"的端到端体验**未验证**。
- **修复要点**：① 验证 biography 迁移后 `world_seed` NPC 目录与活动 opening 真的读取新 head（坐标替换不删既有 Plan/NPC——这是正确行为，但要确认新场景真能出现）；② "她想让 NPC 知道/告诉用户"的表达通道质量；③ 心愿（aspiration）与开放 Plan 的 CAS 同批结晶（已有实现）是否在体验中可感知。
- 来源：`world-v2-major-biographical-transition-gap.md` 探针 2、用户 2026-08-07 补充（"经历大事件后生活轨迹变化并联动 NPC 变化"）。

#### S15 人生大事迁移（毕业/实习/搬家） [active]

- **场景描述**：她毕业了、去实习了、搬家了——生活的基础坐标变了，日程、住所、可接触的人和事随之改变，她还会跟你说起这些变化。
- **期望表现**：迁移由已结算后果驱动（不预设人生路线）；迁移后计划/资源/地点/NPC 各自有来源地调整；表达只从允许披露的坐标派生。
- **依赖机制链**：Life Arc（`biographical_lifecycle_runtime`，从已结算 outcome 提取）→ Context Pack（能力与环境坐标）→ 活动 opening/NPC 生态读取 → 角色表达。
- **现状**：[active]（ADR 0011 + CONTEXT.md Major Biographical Transition 词条）。
- **修复要点**：长周期验证（毕业/搬家的端到端是否真实发生——取决于世界内容流，`mechanism_closure` 限制项）；"半迁移状态"（原子重配校历/日程/资源/住所）是文档明列的未解问题。
- 来源：CONTEXT.md、ADR 0011、`world-v2-major-biographical-transition-gap.md` 后续扩展问题。

#### S16 NPC 的事影响她的情绪，表现给用户 [active]

- **场景描述**：她和一个 NPC 闹了别扭（或 NPC 帮了她），这件事让她心情不好（或很好），接下来和你聊天时你会感觉到。
- **期望表现**：NPC 决策（actor 模型）→ 世界裁决 → 结算 → 她的 experience() → Affect/Appraisal → 对你的表达（可能不会直接说，但语气和话题会变）。
- **依赖机制链**：`npc_ecology`（NPC Plan/Occurrence）→ aftermath 结算 → `world_stimulus`（committed 刺激）→ Inner Life Snapshot 情绪面 → 表达。
- **现状**：[active]（NPC 生态 8 月初合并；roadmap #6 链路在）。**注意**：NPC actor 域是隔离语义域，绝不复用主角快照（CONTEXT.md 明令）。
- **修复要点**：端到端验证"NPC 事件 → 她心情变 → 你感知到"（三次跳变各查）；NPC 事件频率与多样性需长周期观察。
- 来源：`companion-experience-roadmap` #6、用户聊天记录。

#### S17 触景生情 [active]（检索质量是断点）

- **场景描述**：你们聊起某个话题，她想起很久以前你们一起的某件事，主动提起："你还记得那次……"——记忆被当前情境触发。
- **期望表现**：当前情境（话题/地点/物品）成为 recall 的检索触发；取回的旧事来源可溯（不幻觉）；旧事进入当前表达。
- **依赖机制链**：当前 Pinned Turn 语义 → 快照中记忆候选呈现 → 模型选择 recall_request（有界二次检索）→ 取回结果注入 → 表达。
- **现状**：[active] 通道在（`recall_runtime` cursor-pinned 二次检索）；**语义召回（embedding）默认关闭**，只有精确召回（roadmap #8 现状）；取回质量未校准（§5.2）。
- **修复要点**：§5.2 的评测与校准；评估开启语义召回的收益/成本；验证 recall 结果真的被模型使用（而非取回即弃）。
- 来源：`companion-experience-roadmap` #8、用户聊天记录（"想起很久以前聊过的东西再触景生情"）。

#### S18 独处/无聊 → 内省 [disconnected]

- **场景描述**：长时间没有事件和消息时，她不是静止的——会无聊、会胡思乱想、会给自己找点事（也可能只是发呆）。
- **期望表现**：低概率的内省类 Appraisal（"今天好像没什么特别的"）；独处可能催生主动联系（S2）或新计划（S13）。
- **依赖机制链**：clock 长静默 → 内省评估机会（低概率）→ Appraisal/Private Impression → 可能触发主动联系/计划。
- **现状**：**断链**——`inner-life-coverage-plan` 感受空白表明示"独处/无聊：时钟只做衰减"，长时间无事件无内省通道。
- **修复要点**：设计"独处内省"评估机会（低频、可 no_change、预算受限），与 silence 通道区分（S6 是被晾着，S18 是无人说话）。
- 来源：`world-v2-inner-life-coverage-plan` 感受空白表。

#### S19 心愿的萌芽与淡去 [disconnected]

- **场景描述**：她"想去海边看一次日出"——不是计划，是一个想做的事；时间过去，它可能慢慢淡了，也可能在某天变成真正的计划。
- **期望表现**：心愿（Aspiration）作为内心材料进入她的 deliberation；特定时刻被显式"具体化"成 Plan（CAS 同批结晶）；否则自然淡出。
- **依赖机制链**：Aspiration 提议（弱：见 §5.6）→ planted → 作为快照内心材料 → 开放 deliberation 中自由取舍 → reinforced/faded/crystallized。
- **现状**：[disconnected]（§5.6：旧 Runtime 退役，新驱动路径弱）。
- **修复要点**：§5.6 的两条路（补 producer 或并入 life development 作者通道）。这个场景在体验上优先级不高，但它是"角色有自己的愿望"的载体。
- 来源：CONTEXT.md Aspiration 相关词条、`aspiration_reducers` docstring。

### 9.3 感知与媒体场景

#### S20 拍照片分享生活 [disconnected]（§5.1 主目标）

- **场景描述**：她今天做了顿好吃的、路上看到好看的云、去了一家新店——她会"拍下来"发给你，像真人发照片那样，附一句"你看这个"。
- **期望表现**：照片源于已发生的共享性生活事件（不临场编造）；生成的照片形象一致（Identity Binding）；自动发送有节奏（不是一天几十张也不是从来不发）；照片之后还有后续（S9）。
- **依赖机制链**：已结算生活事件 → `life_visual_evidence_author`（视觉证据声明）→ `event_ecology_media`（PhotoCandidate）→ 选片（角色决定）→ Media Plan（隐私上限/表达意图）→ 渲染（审查/修复一次）→ 自动投递（每日上限+最小间隔）→ 你的回应成为新刺激（S9）。
- **现状**：[disconnected]（§5.1：全链路代码 closed 但 limited-production——需显式预览部署 + 预置 grant + operator approval；自动投递未全局启用；P3 私密车道部署未安装；生产只接 OpenAI 一家）。
- **修复要点**：§5.1 四条着力点；另注意照片发送后 S9（期待与忐忑）是"照片有生命"的配套，需一起接通。
- 来源：`companion-experience-roadmap` #4、用户聊天记录、CLAUDE.md 媒体系统。

#### S21 外界新闻/天气影响生活与表达 [active]（半启用）

- **场景描述**：台风来了她会跟你说"今天风好大，注意别出门"；她生活的城市有大事她会知道。
- **期望表现**：外部信号通过合理通道进入她的感知（公共警报/在线信息/NPC 报告），她**选择**是否注意、如何回应；信号不是她自动知道的事实。
- **依赖机制链**：外部源采集（hub）→ 聚类/去重 → 影子/实时注意力（`attention.py`/`live_attention.py`）→ 角色考虑（选择是否注意）→ ExternalPerceptionRecorded → 生活/表达影响。
- **现状**：[active] 半启用（registry off/shadow/live 门控，live 需配置；§5.5）。
- **修复要点**：§5.5（评估 live 提升、验证注意力信号进快照感知面）。
- 来源：`companion-experience-roadmap`、CLAUDE.md 外部感知、ADR 0013。

#### S22 用户发来图片/附件，角色感知并回应 [active]（limited-production）

- **场景描述**：你发一张照片/截图/文件给她，她能"看到"并自然回应内容（"这猫好可爱""你发的这个我看不懂"）。
- **期望表现**：附件经闭式语法决定是否分析 → 角色经唯一 CharacterInterior 感知（有来源、私密）→ 表达回应；分析结果作为来源上下文（不冒充她"亲眼看过"之外的事实）。
- **依赖机制链**：QQ 附件 → `perception_trigger_runtime`（闭式语法决定）→ `qq_attachment_perception`（角色考虑）→ `perception_vision_transport`（OpenAI vision，SQLite 持久化）→ 结果进上下文。
- **现状**：[active] limited-production（`injected-perception-tool`：需显式模型输入源 + 预置 enforcement authority；结果审议当前"无可见动作"限制）。
- **修复要点**：验证 QQ 附件端到端（attachment → 感知 → 回应）在生产配置下可达；"结果审议无可见动作"限制是否阻碍了回应的自然性。
- 来源：CLAUDE.md QQ 附件、`mechanism_closure` injected-perception-tool。

### 9.4 关系与记忆场景

#### S23 暧昧期胡思乱想 [active]（效果未验证）

- **场景描述**：你们关系走到暧昧阶段，你一句平常的话她会多想——"他这句话是什么意思？"——内心戏丰富，可能因此变得主动、试探、或患得患失。
- **期望表现**：relationship stage（慢变量）+ Affect 综合影响她的内心（Private Impression），内心再影响表达选择——但系统不做"暧昧度 → 行为"的映射矩阵。
- **依赖机制链**：关系慢变量（`relationship_reducers`）→ 快照关系面 → 她对你的话形成 Appraisal/Private Impression（模型自选）→ 表达（试探/直接/保留）。
- **现状**：[active] 机制在，效果未验证（关系信号生产 `relationship_proposal_compiler` 已接入）；"暧昧期胡思乱想"是否真实发生无评测。
- **修复要点**：长对话实测——关系阶段变化后，她的表达是否真的会变（对照 §2 目标 7）。
- 来源：`design-intent` 记忆（2026-07-27）、`world-v2-relationship-production.md`。

#### S24 吵架后一直生气，不想回消息 [active]（疑未真正接通；开发阶段：暂不进行真实延迟，仅作联动期望）

- **场景描述**：你们吵了一架，她真生气了——接下来一段时间她冷淡、简短、甚至不回；可能要你哄很久才慢慢好。
- **期望表现**：冲突事件 → 持久 Affect/关系调整（不是一次消息决定关系，是反复互动推动慢变量）→ 影响后续回复的意愿与语气 → 修复过程（道歉/解释）有迹可循地起效。
- **依赖机制链**：冲突 Appraisal → Affect Episode（版本化衰减/残迹）+ Relationship Signal → 后续快照情绪/关系面 → 表达选择（含 silent）→ 修复互动 → 慢变量回调。
- **现状**：**疑未真正接通**（roadmap #5 明标"待查"）——机制都在 [active]，但"吵架 → 一直生气 → 用户可感知"端到端未验证；`inner-life-coverage-plan` 记分表显示"媒体分享意愿/活动生命周期"等仍有无心通路，同轮情绪门还是关键词表 `_IMMEDIATE_EMOTION_CUES`（旧实现，期望是语义化）。
- **修复要点**：① 端到端实测一次"冲突 → 持久冷淡 → 修复回暖"；② 确认 Affect 残迹跨回合可见（§5.3）；③ 同轮情绪门语义化（inner-life-coverage-plan 感受空白表第 7 行——已列期望，未落实）。
- 来源：`companion-experience-roadmap` #5、用户聊天记录（"吵架了所以一直生气，不想回消息"）。

#### S25 超长期记忆 [active]（检索质量是断点）

- **场景描述**：几个月前你们聊过的细节，某天她还能接上——"你不是说你家猫叫 XX 吗，它最近怎么样"。
- **期望表现**：事实经来源闭包长期存续（不幻觉）；当前情境可触发取回（S17）；撤回/更正有迹可循（`memory_withdrawal_review`）。
- **依赖机制链**：Fact 两阶段接受（`fact_memory_candidate_lifecycle`）→ 记忆候选 → 混合检索索引（`recall_index`）→ 快照呈现 → recall_request → 表达。
- **现状**：[active] 通道完整（CLAUDE.md 记忆子系统；`memory_retrieval` 只读取回）；**语义召回默认关闭**（只精确召回），超长期"情境触发"能力受限（§5.2）。
- **修复要点**：§5.2；"几个月前的对话"是精确还是语义召回、预算多大，需要评测数据决策。
- 来源：`companion-experience-roadmap` #8、用户聊天记录（"超长期记忆"）。

#### S26 内心私密想法影响后续互动 [active]（效果未验证）

- **场景描述**：她对你形成了一些没说出口的看法——"他最近好像有心事"——这些想法不会直接说，但会改变她之后的行为（多问一句、更体贴、或试探）。
- **期望表现**：Private Impression（易错、有置信度、有反证）作为内心材料进入后续 deliberation，不写成 User Fact（不冒充确证）。
- **依赖机制链**：互动 → 后台 Private Impression 生产（`private_impression_producer`）→ 快照印象面 → 后续表达选择。
- **现状**：[active]（生产者 [active]）；效果未验证——印象是否真的影响后续行为，与 S8/S23 同类（模型产出质量，无评测）。
- **修复要点**：抽查 Private Impression 的产生频率与后续行为相关性；确认印象不流入用户事实面（来源隔离）。
- 来源：`design-intent` 记忆、"内心想法 / private impression 影响后续互动决策"。

## 10. 生产环境目标（SLO）

> 2026-08-07 定稿口径（用户拍板）：快速回复 1-2s 按 **p50** 计；真人感行为分布**不设数值**，
> 改为体感验收（与角色聊天对照场景库判断）。可观测性现状：延迟分段埋点现成
> （`production_latency_trace.py`）、成本门现成（`budget.py` + `test_economy.py`）；
> 其余指标需要新埋点（§10.8）。目标分三类：**量化 SLO**（有数值、有观测）、
> **体感验收项**（无数值、靠聊天）、**待基线项**（先埋点收集数据再定值）。

### 10.1 响应速度（量化，北极星硬约束）

| # | 指标 | 目标 | 可观测性 |
|---|---|---|---|
| V1 | 快速回复端到端 p50（Fast Reply 主路径：提交→投递） | **1-2s**；p95 ≤2.5s | 现成（`production_latency_trace`） |
| V2 | 完整回合首气泡 TTFT（多 Beat 路径，Expression Unit Stream） | ≤2s | 现成 |
| V3 | 完整回合全部送达 p50 / p95 | p50 ≤5s / p95 ≤12s（含段间节律） | 现成 |
| V4 | 端到端 p95（含恢复重试） | ≤30s（10/30/120min 重试是恢复路径，不算正常预算） | 现成 |

### 10.2 可靠性（量化，失声三症状之一）

| # | 指标 | 目标 | 可观测性 |
|---|---|---|---|
| R1 | 故障失声率（技术失败静默 ÷ 用户消息数） | **<0.1%** | **需新埋点** |
| R2 | 接受链 CAS/批校验失败率 | <1%（代码 bug 类，应极低） | 需新埋点 |
| R3 | 投递成功率（Action delivered ÷ authorized） | ≥98% | 现成（Action 终态） |
| R4 | 恢复路径成功率（10/30/120min 重试后终态成功） | ≥90% | 需新埋点 |

### 10.3 生成质量（量化，模型侧"一次通过率"）

> 一次通过率 = 模型产出经对应校验/验收环节**一次通过**（无需重试/重选/修复）的比例。
> 它同时是成本目标 C2 的根因指标。

| # | 指标 | 目标 | 可观测性 |
|---|---|---|---|
| Q1 | 各 model purpose 首次结构合法率 | **≥99.9%** | 需按 purpose 新埋点 |
| Q2 | 首次来源与跨字段 boundary-admissible 率 | **≥99.9%** | 需新埋点 |
| Q3 | 首次被对应 typed authority 接受率（合法 silent/no-change 计成功） | **≥99.9%** | 需新埋点 |
| Q4 | 媒体模型首次 boundary-admissible 率 | **≥99.9%** | 现有修复记录需拆 first-attempt |
| Q5 | 已接受输出中的无来源事实率 | **≤0.1%**，目标 0 | 需新埋点与人工抽查 |

### 10.4 成本（量化）

| # | 指标 | 目标 | 可观测性 |
|---|---|---|---|
| C1 | 月度按量调用总成本 | **设计目标 ≤¥100/月；允许有依据的临时超支** | 部分现成；需统一所有 provider/account |
| C2 | 无效成本率（重试/修复/重选 ÷ 总成本） | ≤10%（与 Q1 联动） | 需新埋点 |
| C3 | 单回合模型成本 p50/p95 | 按 production usage 实测并按 purpose 展示 | 现成（`usage_metrics`） |
| C4 | 月/日/软日预算门和并发 reservation | 全部消费先原子预留；总账与分类账一致 | 核心现成，需覆盖所有 World V2/provider |
| C5 | 月末成本预测 | 预测 >¥100 即 warning、归因并优先降载非核心调用 | 需新增 forecast/health |

`¥100/月` 是整个生产实例的**设计目标**，不是每个 Module 各有 ¥100。它允许因真实高互动、故障调查或
必要资格化临时超支，但架构、provider 选择、机会频率和默认配置必须以常态低于 ¥100 为目标，不能把超支
当作正常容量。统计口径只包含类似 API 的按量调用费用：
角色模型、World Author/reviewer、NPC、embedding、图片生成、vision、audio、付费 Search/Perception 以及真实
provider 资格化/对聊 canary。订阅费、服务器费用、自用本机折旧和电费暂不纳入本指标；未来若产品口径
改变再单独修订，不能由实现者擅自混入后声称 API 成本超标。

初始规划信封不是行为配额，也不要求花完：

| 账目 | 月度规划上限 | 保护原则 |
| --- | ---: | --- |
| 用户入站与可见对话 | ¥60 | 优先保障；不能用廉价模板、删上下文或替角色默认 `now/silent` 省钱 |
| 主角后台内心、记忆与生活连续性 | ¥15 | 事件合并、按需召回；无新证据不调用 |
| NPC、主动联系与社会生态 | ¥8 | actor 分级、低频合并；禁止全 NPC 扫描 |
| Media/vision/audio | ¥8 | 先有角色选择和有效证据再预留；渲染失败不重复烧钱 |
| Perception/embedding/Search | ¥3 | 缓存、去重、本地索引优先；不阻塞 Fast Reply |
| 真实 provider 资格化与对聊 canary | ¥6 | 样本有上限；生产故障复现优先于重复跑绿样本 |
| **合计** | **¥100 目标** | 分类可按真实 workload 调整；超支必须可归因，不能自动侵蚀可见对话质量 |

成本控制使用 forecast，而不是月底才发现偏差：预测达到 ¥70/¥80/¥90 时逐级提示、停止重复资格化和低价值
media、降低低显著度 NPC/ambient/perception enrichment 的机会频率；预测或实际超过 ¥100 时继续保障用户
可见聊天和必要一致性调用，同时告警、保存 purpose/provider 归因并要求下一轮优化或 operator 明确接受。
不得仅因超过目标让角色失声，也不得用本地自然语言冒充角色回复。降载只能减少机会频率、候选丰富度或
非必要外部效果，不能改变已经打开的角色选择、事实来源、隐私、Action 安全或把 technical failure 记成
角色沉默。

所有模型和 provider 调用必须在发起前以 purpose/actor/provider/estimated CNY 原子 reservation，结束后用
真实 token/图片/调用账单结算，超时或取消释放未消费部分；重启后 reservation 不可重复花费。Health 同时
展示本月已结算、在途预留、分类占比、最近 24h burn rate、月末预测、剩余可见对话储备和触发的降载级别。

### 10.5 真人感行为分布（体感验收项，不设数值）

**为什么不定数值**：用户验证真人感的方式是让 agent（或自己）直接与角色聊天、对照设计意图
体感判断——行为分布本身难以量化，硬定数值（"主动 3 次/天"）反而失真。

- **可复现实旅程**：`scripts/run_world_v2_conversation_audit.py --database <isolated.sqlite> --output
  <audit.jsonl> --strict` 使用隔离数据库、真实 provider、生产 QQ host、
  scheduler 和 32 轮 fixture，保存回复、分段延迟、错误和账本证据；`--strict` 只是一组已知灾难的 smoke
  gate，不是自然度评分，也不能强迫模型使用固定措辞。
- **Luna/agent 亲自自由对聊**：每次大改动后使用 `scripts/chat_with_world_v2.py --database
  <scratch.sqlite> --clone <生产库副本>` 走
  production host。测试者必须根据角色实际回复自然追问至少 15 轮，包含短句连发、话题跳转、情绪变化、
  纠正、打断/被打断和隔时召回；不能预先写死下一句，也不能只搜索关键词。`--burst-message` 专门检查一批
  多气泡是否完整可见。最终发布再由用户或 Sol 通过真实 QQ 做同类抽查。
- **场景库是观察维度**：对 §9 S1-S26 记录「发生 / 部分发生 / 未发生 / 无法判断」及原始 transcript/
  ledger refs；不要求一段对话强行触发所有行为。测试者重点解释回复为什么自然或不自然、是否来源闭合、
  是否像服务助手、延迟是否合理，以及状态是否在后续轮次真实延续。
- `scripts/automated_conversation_test.py` 是早期两轮/关键词式调试脚本，不能作为当前生产资格、真人感或记忆
  正确性的证据；Luna 可删除或改名为 legacy probe，不能引用它的“通过率”。
- 当前 `conversation_audit_acceptance.py` 中要求特定情绪词或固定多 Beat 的断言只能说明该 fixture 的已知
  回归，不能升级成产品行为规范。Luna 必须审计并拆分 `hard invariant smoke` 与 `human review rubric`：身份
  越权、无来源事实、运行错误、Action/账本和延迟可以硬失败；是否直说“失望”、是否恰好两条、是否追问
  不能以关键词替角色裁决。
- **方向性护栏**（不设数值但由机制守边界）：主动联系/媒体投递受预算与冷却门约束
  （proactive 窗口、每日上限+最小间隔）；"陌生阶段只允许一轮未回应外发"的防打扰
  语义由 `outreach_block_reason` 类机制承担。
- 与 Q/V/R 类 SLO 的关系：体感发现"某场景未发生"时，先查它依赖的机制状态标签
  （§4/§5），数值类指标（如 V1 超时）是失声的常见技术根因。

### 10.6 综合评测

| # | 指标 | 目标 |
|---|---|---|
| E1 | 活人感 A/B 分（系统 vs 裸模型，分维度：文风/速度/连续性） | 总 ≥ 裸模型 |
| E2 | Text Turn Endpoint 估算准确率 | 待基线（§5.4） |
| E3 | 记忆取回相关度 | 待基线（§5.2） |

### 10.7 落地前置（可观测性缺口）

目标生效的前提是能度量。现状盘点：

- **现成可测**：V1-V4（`production_latency_trace` 分段）、R3、Q4、C3（budget/usage）。
- **需新埋点**：R1（失声归因：回执终态 × 技术失败分类）、R2、R4、Q1/Q2/Q3/Q5
  （ModelResult 失败归因 + 审核结果归因）、C2（重试/修复成本分账）。
- 埋点纪律：只读观测不改写路径（`production_latency_trace` 自述 non-authoritative）；
  观测本身不得成为行为决策（受控高随机红线）。
- 与 `companion-experience-roadmap.md` 的关系：阶段 0（量化基线）建埋点 → 阶段 1（止血）
  先打到 R1/V1 → 阶段 2（通设计）验证场景存活率。

## 11. 深度联动：让类人行为从因果基底中涌现

### 11.1 最终架构意图

Girl-Agent 不穷举人类行为，也不维护“遇到 X 就做 Y”的剧情库。系统建设一个由来源明确的事实、
经历、记忆、关系、情绪、愿望、计划、人物、感知和后果组成的**因果基底**：每次已接受变化可以为
有权感知它的 actor 提供新的考虑机会；actor 自己决定注意什么、如何理解、是否发生主观变化、是否
行动、是否表达以及何时表达。被接受的选择和实际执行再形成客观后果，进入下一轮因果。

复杂的类人行为来自三个条件的乘积，而不是规则数量：

1. 世界与内心拥有足够丰富、持续且相互独立的根源；
2. 根源之间存在可重放、actor 隔离、来源闭合的双向连接；
3. 每个 actor 在机会中保留真实选择权，包括忽略、误解、犹豫、改变主意和不行动。

长链可以跨越一轮聊天、数小时、数周或数年。它不是一次 mega-call，也不是必须走到底的 workflow。
“用户提到一家店→后来角色想起→某天决定去→认识 NPC→经历影响情绪→再向用户分享”是一条可能
涌现的链，而不是系统需要硬编码的流程。

### 11.2 三层模型的最终解释

| 层 | 职责 | 不得越界 |
| --- | --- | --- |
| 机制层 | 提供 World truth、来源、能力、时间、随机机会、记忆候选、权限、后果、账本与恢复 | 不替 actor 形成动机、情绪、态度或行为结论 |
| 模型层 | World Author 提出开放客观变化；主角/NPC actor 形成各自的主观理解、愿望、选择和表达 | 不创造 authority、篡改事实或读取无权可见的内心 |
| 界面层 | 把已接受的 Expression、Media、Action 与回执真实呈现给用户 | 不用模板或故障话术冒充角色，也不把预览当作已发送 |

机制越丰富，不代表角色越受控制。正确方向是增加她可以感知、记住、权衡和承担后果的真实材料，
而不是增加“应该怎么做”的规则。

### 11.3 根源与 typed authority

| 根源 | 可以产生 | 明确不能产生 |
| --- | --- | --- |
| User Interaction | Observation、附件、投递/未回应、中断、承诺与关系互动的客观证据 | 用户意图或角色感受的确定结论 |
| Life Ecology / World Author | 环境变化、偶然、开放机会、困难、客观结果、Life Arc 候选 | 主角动机、必选人生方向或固定剧情 |
| CharacterInterior | 主角的 Appraisal、Affect、Recall 采用、关系理解、愿望、选择、表达和行动意图 | World truth、外部执行权或 NPC 私人状态 |
| Memory | 来源绑定的保留、检索、强化、淡化、冲突与再巩固候选 | 脱离来源的事实或“接下来应该做什么” |
| NPC Ecology | NPC 自身目标、日程、近况、态度、记忆、行动和人生阶段变化 | 主角私人内心或围绕主角预写的剧情 |
| External Perception | 实际采集、去重、可信度、地域、纠正和 actor 实际看见的外部信息 | 角色自动知道、用户所在地或角色经历 |
| Plan / Activity / Action | 选择的可执行承诺、现实约束、授权、执行、失败和回执 | 主观愿望或“失败后必须坚持” |
| Media | 基于已接受生活证据的照片机会、渲染、检查、发送和反馈 | 临场发明生活事实或第二个外观真相 |
| Calendar / Biography | 年龄、季节、校历、住处、阶段迁移和能力变化 | 固定毕业、创业、工作等人生路线 |

Appraisal、Affect、Relationship、Memory、Aspiration、Goal、Thread、Commitment、Plan、Action、Life、
NPC、Media、Perception 必须保留各自 typed authority、acceptance 和 reducer。禁止为了“方便联动”
建立 `InteriorChanged(dict)`、`CausalEvent(text)` 或可变 mind blob。

### 11.4 Causal Opportunity：连接不等于反应

一个 accepted source 可以为一个明确 actor 和 purpose 打开 **Causal Opportunity**。机会只保存：

- 精确 source refs、actor visibility、pinned cursor 与 contract/hash；
- canonical source set、epoch、merge window、due/expiry；
- claim、terminal、retry 和技术失败状态。

机会不包含“她应该生气”“NPC 应该安慰”“现在应该发消息”等结论。Clock、重要性、相似度、关系强度
和 RandomDraw 只能影响候选、注意机会和时间，不能产生语义结果。

同一 actor/source set/purpose/epoch 只打开一次机会。新证据或显著时间跨度可形成新 epoch；没有新
证据时不能用 Clock 无限重做同一思考。机会可合法终止为角色 `no_change/ignored/silent`，而 provider、
解析、来源、CAS 或存储失败必须记录为 technical failure。

实现上优先深化现有 `character_interior/world_stimulus.py`、trigger process 与 scheduler，使其成为
统一的机会路由 seam；只有既有职责无法收敛时才新建 Runtime，并在同一交付迁移消费者、删除旧路由，
绝不并行维护两套因果调度。

### 11.5 CharacterInterior 是唯一主观枢纽

主角的一次主观整合必须在同一个 source-bound Interior Turn 内看到当前活动/可用性、关系、活跃情绪、
近期自身经历、开放 Thread/Commitment、愿望/计划、感知、用户事实和少量 recall candidates。Appraisal、
Affect、Attention、Recall adoption、Relationship stance、Aspiration/Choice、Expression、Action intention
是同一个私人自我的可选能力，不是互相不知道的八个角色模型。

关键边界：

- 主角所有主观 proposal 只从 CharacterInterior 产生；业务模块不能直接调用 role provider。
- NPC 使用独立 actor scope 和自己的廉价 Interior，不读取主角 Inner Life Snapshot。
- Structured Output / Function Calling 只固定协议形状；`now/later/silent`、Beat 数量、summary、attention、
  motive、emotion 和 source adoption 仍由 actor 显式写出。
- 同一次 Observation 形成的 Appraisal/Affect/Relationship 变化必须能影响本轮 Expression，避免情绪慢一拍。
- Recall 预取只提供候选；是否想起、采用和表达由 actor 决定。
- Private Turn State 是可审计的角色材料，不是宿主补写的 hidden chain-of-thought。

### 11.6 多时间尺度

| 时间尺度 | 典型来源 | 可能开放的能力 |
| --- | --- | --- |
| 即时，同一 turn | 用户批次、附件、打断、当前活动 | Appraisal/Affect/Relationship/Recall/Expression/Action intention |
| 余波，分钟至小时 | 表达或媒体回执、用户未回应、活动完成/失败、NPC 互动 | 重新评价、情绪残迹、记忆候选、关系理解、后续表达机会 |
| 日常，小时至天 | 活动、偶然、外界感知、独处、NPC 邀请 | Reflection、生活选择、主动联系、媒体候选 |
| 发展，天至月 | 重复经历、长期困难、愿望强化/淡化、计划执行 | Aspiration、Choice、Plan、Life Arc 迁移 |
| 生平，月至年 | 毕业、实习、搬家、工作、关系转折 | Biography revision、能力/地点/日程/NPC 网络重配置 |

即时与后台不是两个“人格”。它们必须经同一 CharacterInterior、typed authority 和来源闭包，只是时限、
上下文预算与 opportunity cadence 不同。

### 11.7 必须存在的双向连接

#### 用户互动 ↔ 角色生活与内心

- 用户消息可形成 Fact/Memory/Thread/Commitment/Life influence 候选，但不能直接改写生活。
- 角色可在后续生活机会中采用相关 Recall，自由选择是否形成 Plan。
- 争吵、支持、冷落等互动可以打开 Appraisal/Affect/Relationship 机会；结果不预设正负。
- 主动消息未获回复只是客观证据；角色可以失望、担心、理解、轻松或无变化。
- 用户后来回复是新证据，可修正旧理解但不重写旧感受。

#### 角色自身表达与媒体 ↔ 后续内心

- 已 delivered 的 Expression/Media receipt 可成为一次有界自我 aftermath：她可能后悔、坚持、期待、
  忐忑或无变化。
- 她自己的旧回复不能证明用户事实，也不能自动成为共同历史。
- 同一 receipt 只打开一次合并机会，防止表达→自评→表达无限循环。

#### Life / Outcome ↔ Emotion / Memory / Choice

- settled Activity/Occurrence/Plan failure 可成为主角可见 experience。
- CharacterInterior 自己决定它意味着什么；World Author 不能附送主角情绪。
- accepted Appraisal/Affect/Memory/Reflection 可影响未来注意、愿望和选择，但不能自动创建 Plan。
- Plan/Action 的真实完成、失败或中断再次回到 Life 与 Interior，形成后果循环。

#### NPC ↔ 主角

- NPC 有自己的目标、日程、近况、对主角态度和记忆，也会遇到自己的事情。
- NPC 可以邀请、疏远、求助、误会、修复、离开或重现，但具体选择属于 NPC actor model。
- NPC 私人结果只有经过实际交流、共同活动或可见后果后，才成为主角 stimulus。
- 主角对 NPC 的行为反向影响 NPC 记忆/态度；主角也可以忽略 NPC 事件。
- 人生迁移可改变 NPC 网络和接触机会，但不删除既有关系历史。

#### Memory ↔ Attention / Affect / Reconsolidation

- 当前语义、地点、人物、未完话题和 Affect 可以共同检索候选；检索分数不等于角色想起。
- 角色在对话、生活活动、NPC 互动、独处和主动考虑中都可自发采用自己的或用户相关记忆。
- 新理解形成新的 Memory revision/reconsolidation，不篡改原事实；支持冲突、淡化、撤回与隐私。
- Commitment/prospective memory 到期只开放机会，不强制提醒或主动发送。

#### External Perception ↔ Life / Interior

- RSSHub、新闻、天气和社交信息先经过来源、去重、可信度、地域和更正，再形成角色实际看到的 perception。
- 抓到信息不等于角色看到；看到不等于在意；在意不等于必须表达。
- 外界变化可影响客观 Plan/Life/NPC 可行性，也可成为 Interior material；中间必须经过相应 author。
- 新闻不能证明用户所在地；角色若决定查证，使用受权 Search Action。

#### Calendar / Biography ↔ Capability / NPC / Events

- 年龄、季节、校历、当前住处和 Life Arc 决定真实可用的地点、活动、日程和人物集合。
- 实习、毕业、搬家等由已接受选择和 settled outcome 推动，不按日期自动写剧情。
- 迁移后旧能力可以退出，新 NPC/地点/资源进入；历史事实仍保留。
- 半迁移必须原子重配关键坐标，避免“已经毕业但仍默认上课”。

#### Expression / Interruption / Delayed Attention

- CharacterInterior 可选择不回、单 Beat、多 Beat、afterthought、主动联系、打断、later 或 silent。
- Text Turn Endpoint 只估算用户是否可能继续输入；不能决定角色是否或如何回复。
- 新消息使未 dispatch Beat 的旧推测失效；已 dispatch Beat 保持 effect-once。
- “没看到手机”需要角色拥有的延迟注意状态和真实活动依据，不能由技术失败或宿主矩阵伪装。

#### Media ↔ Appearance / Relationship / Memory

- PhotoCandidate 来自已接受的视觉/生活来源；选不选、发不发和配文由角色决定。
- Appearance State、着装、时间与地点连续性来自已接受投影，renderer 不建立第二真相源。
- 用户对图片的回应、未回应和分享回执可进入关系、记忆与 aftermath；图片不反向证明未记录的经历。

### 11.8 主动发现并纳入设计的进一步联动

以下不是行为剧本，而是此前显性功能之间缺失的因果组织。它们解释“人的状态为什么会跨场景延续”，
并尽量复用现有 authority。只有多个调用方确实需要同一复杂编译规则时，才形成新的深 Module。

| 新联动 | 可涌现的因果形状 | 优先复用的机制/Seam | 不得越界 |
| --- | --- | --- | --- |
| 具身状态 ↔ 活动/计划/注意/媒体 | 熬夜、疾病、饥饿、恢复、天气和穿着形成客观处境；角色自己理解难受、烦躁、无所谓或逞强；处境改变可行活动、照片与延迟注意机会 | Life/Activity outcome、Current Situation、availability、Appearance/Visible Physical State；必要时深化为只读 `EmbodiedContext` 投影 | 不建立能量值→回复/情绪矩阵，不用“身体不适”解释技术超时 |
| 可修正自我认识 ↔ 反思/愿望/表达 | 重复经历让她觉得自己变勇敢、没耐心、适合或不适合某事；新证据也可推翻这种看法，并影响愿望与自我表达 | Character Core 保持稳定身份；Private Impression/Reflection/Memory revision 承载可修正 self-narrative | self-narrative 不是 Character Core 或 World fact，宿主不得替她总结“她是什么人” |
| 技能与习惯 ↔ 活动结果/能力/事件分布 | 反复练习、成功、失败或长期中断积累证据，可能形成技能、熟练度、习惯或放弃；能力变化让未来事件产生不同开放结果 | Activity/Outcome evidence、Biography/Capability、Memory、Reflection；只有真实 producer 出现才启用 Goal/Resource authority | 不用 XP/次数自动升级，不把“经常做”直接等同“喜欢”或“擅长” |
| 承诺与现实冲突 ↔ 选择/关系/后果 | 用户承诺、NPC 约定、课程/工作、时间、金钱和身体状态可能互相冲突；角色选择优先、协商、取消或逃避，失约结果再影响关系与自我评价 | Thread/Commitment、Plan precondition、Calendar、Action receipt、Relationship/Appraisal | scheduler 只暴露冲突和截止时间，不能按优先级替角色作道德选择 |
| 物品/地点连续性 ↔ 记忆/生活/媒体 | 礼物、宠物、设备、衣物、照片和常去地点可被获得、借出、丢失、损坏、赠送或再次遇见；它们可触发回忆并进入生活照片 | Fact/World entity/Place、Life outcome、possession/custody evidence、Recall、Media context | 物品意义属于 actor Appraisal；未发生的赠送/到访不能由对话或图片倒推 |
| 谁知道什么 ↔ 隐私/秘密/披露/关系 | 主角、用户和不同 NPC 拥有不同知识；真实告诉某人后形成披露历史，之后可记得“他知道”，也可后悔或继续保留秘密 | actor visibility、privacy ceiling、delivered Expression/Media receipt，派生只读 Disclosure projection | 亲密不自动授权披露；模型不能读取未分享的其他 actor 私域 |
| 主观信念 ↔ 误解/查证/修正 | 角色可对用户、NPC 或新闻形成不确定猜测，之后寻求证据、被反驳或改变理解；误会因此能自然发生与修复 | Private Impression、epistemic scope、Perception correction、Reflection、Search Action | belief 始终 actor-bound、带置信/反证；不能进入 User/World Fact 或被宿主当真 |
| 想象/反事实/梦境 ↔ 情绪/愿望/创作 | 她可以设想“如果……会怎样”、做梦、担忧坏结果或想象未来；这些内容可能影响情绪、愿望或创作，但不表示现实发生过 | CharacterInterior private material、Reflection/Aspiration、明确 `imagined/counterfactual/dream` epistemic namespace | 梦境和设想绝不成为 Experience/World truth，也不能给媒体提供现实照片证据 |
| 自我调节 ↔ 情绪/生活/NPC | 情绪出现后，她可能找 NPC 谈心、散步、工作、逃避、发消息或什么都不做；行动结果可能缓解、恶化或毫无作用 | Affect/Appraisal 只打开 Choice/Plan opportunity；Activity/NPC outcome 再进入 Interior | 不建立 emotion→coping action 列表或“负面情绪必须被解决”目标 |
| NPC 社会网络 ↔ 信息传播/机会/冲突 | NPC 之间也有关系和事件；推荐、误传、邀请、立场冲突可经真实交流传播，间接影响主角的机会与理解 | NPC actor scope、NPC-NPC relationship、World communication outcome、actor visibility | 不让全体 NPC 共享全知状态；背景 NPC 不因无关事件逐个调用模型 |
| 时间标记 ↔ 回忆/关系/人生 | 周年、生日、季节、开学、毕业临近、旧事发生日可以让某段记忆更容易被看见；角色可记得、忘记、在意或不表达 | Calendar/Biography、Fact/Memory temporal index、ambient Causal Opportunity | 日期只开放 Recall/Reflection 候选，不强制纪念、主动联系或固定情绪 |
| 小选择的路径依赖 ↔ 后续机会分布 | 接受一次邀请、拒绝一次工作、常去某处或与某 NPC 修复，会改变以后能遇到的人、地点和机会；一次选择不必立刻成为“大剧情” | settled Plan/Activity/Relationship/Biography 更新 capability 与 candidate environment | 不用单次选择永久锁死人生；World Author 提供开放后果，不决定主角坚持或后悔 |
| 用户交流习惯 ↔ 端点/期待/关系解释 | 用户的气泡间隔、长短句、常见在线时段和承诺履行形成统计证据，改善“还会不会继续发”的估算；角色可自行理解这种习惯 | QQ observation statistics、Text Turn Endpoint advisory、Private Impression/Relationship | 统计只服务机会和上下文，不直接决定打断、回复、失望或用户人格标签 |
| 兴趣/文化 ↔ 外界感知/技能/NPC/媒体 | 长期兴趣使某些外界信息更容易成为候选，相关活动可能结识 NPC、形成技能或照片；兴趣也可淡化、转向 | Memory/Fact、Perception candidate ranking、Life opportunity、Reflection | 兴趣影响候选相关性，不是“看到关键词就必须讨论/参加”的规则 |
| 共同文化 ↔ 关系/记忆/表达 | 昵称、内部笑话、共同仪式、常用说法和纪念方式可以从真实互动中逐渐形成，让不同关系拥有不同质感 | shared interaction Memory、Relationship、Thread/Commitment、Expression recall | 不预置情侣/朋友话术；一次使用不自动成为永久昵称或仪式 |
| 边界与同意 ↔ 亲密/媒体/行动 | 某次允许、拒绝、撤回或重新协商会改变当前可执行能力与关系理解；角色可对边界有自己的感受但必须服从外部授权 | Consent/Authorization、privacy、Action/Media capability、Appraisal/Relationship | 亲密度不能覆盖撤回；角色的不满也不能绕过同意，系统不能把拒绝直接写成关系惩罚 |
| 不可逆损失 ↔ 记忆/人生/关系 | 错过机会、物品丢失、NPC 离开、关系结束或计划永久失败会关闭部分能力并留下历史；之后可适应、怀念、否认或无明显变化 | settled irreversible outcome、Biography/Capability、Memory、Reflection/Affect opportunity | 不为制造戏剧而强制失去；历史不可复活，除非新 World event 真实建立新的状态 |
| 多视角声誉 ↔ NPC 网络/自我认识 | 不同 NPC 根据各自看见的行动形成不同、甚至冲突的印象；主角可能通过交流知道其中一部分，并影响自我认识或选择 | actor-bound Relationship/Private Impression、Disclosure/communication outcome、Reflection | 不建立全局“声望值”，NPC 不知道未亲眼看见或未被告知的行为 |
| 修复与宽恕 ↔ 记忆/关系/自我叙事 | 道歉、解释、补偿和后续可靠行为提供新证据；角色可接受、部分接受、仍介意或改变对自己的看法 | Appraisal、Relationship slow projection、Commitment/Action receipt、Memory reconsolidation | 一句道歉不自动清零 Affect/关系，系统也不规定必须原谅 |

这些联动带来三个可能值得深化的 Module，但不是要求立即新建：

1. **EmbodiedContext**：若 Activity、Plan、Attention、Expression 和 Media 都在重复编译同一具身事实，
   形成一个只读、actor-scoped、cursor-pinned 的深 Module；Interface 只返回来源化当前处境，不返回行为建议。
2. **ActorEpistemicView**：从真实 Perception、Observation、delivered disclosure 与 actor 自己采用的私人
   belief 派生可见证据、exposure 与 adopted belief；它是 Projection，不是新的事实权威，也不能暴露别的
   actor 私域，更不能把“送达”误写成“理解或相信”。
3. **Revisable Self-Narrative**：优先作为 CharacterInterior 内由 Reflection/Memory 支持的 private proposal；
   只有多个长期 consumer 确实需要版本化状态时才建立 typed authority，且永远不覆盖 Character Core。

删除测试适用于上述 Module：若删除后 actor visibility、具身编译或 self-narrative 复杂度会重新散落到
多个调用方，它才有足够 Depth；若只是转发已有投影，不应新增 Interface。

#### 11.8.1 代码雏形审计与当前状态（2026-08-08）

这里的状态判断不是按文件名，而是逐项检查 `producer → authority/sidecar → Projection → consumer →
composition → health/production evidence`。`相关原件` 表示可以复用的地基，不表示联动已经交付。

| 联动 | 当前状态 | 已有代码雏形与真实边界 | 缺失闭环 / 目标设计 |
| --- | --- | --- | --- |
| 具身状态 | [disconnected] | `appearance_state.py`、`visible_physical_state.py` 及 runtime/reducer/media consumer 存在；`production_turn_application.py` 暴露 record 接口 | 当前没有内部生产 producer 调用这些 record 接口，也没有疲劳、疾病、饥饿、恢复等统一来源。先接真实 Life/Activity outcome producer，再通过删除测试决定是否建立 `EmbodiedContext` |
| 可修正自我认识 | [design-only] | Character Core、Biography、Private Impression、Memory、Reflection 是可复用原件 | 没有稳定 Core 与角色自己可修正 self-concept 的明确 seam；不得由宿主从行为统计总结人格，设计见 §11.8.3 |
| 技能与习惯 | [partial] | Activity/Outcome、Biography/Capability 和 recall 存在 | 缺少“练习证据→角色/世界审查→能力或习惯变化→未来 capability/candidate”闭环；Goal/Resource authority 目前无生产 producer |
| 承诺与现实冲突 | [partial] | Thread、Commitment、Plan、Calendar、Action receipt 均有状态 | 没有统一的 source-bound 冲突编译结果稳定进入 Interior；scheduler 只能提示冲突，不能替角色排序或取消 |
| 物品与地点连续性 | [partial] | user possession fact、WorldPlace/provisional place、Life outcome 和媒体 object ref 存在 | 缺主角物品 identity/custody/condition/location 生命周期，以及 Recall/Life/Media 的统一消费；地点较强但仍需 presence 与计划来源闭包 |
| 谁知道什么与选择性披露 | [partial] | actor visibility、privacy ceiling、source scope、Consent/Authorization、delivered receipt 都在 | 硬隐私已工作，但没有统一“谁被暴露于什么、谁后来采用为何种信念”的长期视图；不能把送达直接等同于理解或相信 |
| 主观信念、误解与修正 | [partial] | Private Impression 有 producer、trigger、Projection、Capsule 与 recall 接线 | 当前主要覆盖主角对用户/关系的私人判断；缺一般 actor belief、反证 lineage、NPC 与 perception 纠正的统一 actor-scoped 语义 |
| 想象、反事实与梦境 | [design-only] | CharacterInterior、Reflection、Aspiration 和 memory epistemic scope 可承载 | 没有明确 namespace 和阻止其进入 Experience/World Fact/media evidence 的端到端门禁 |
| 自我调节与 coping | [must-not-build] | Affect/Appraisal、Choice/Plan、Activity/NPC outcome 都是必要原件 | 禁止 `emotion → coping action` 引擎；需接通“情绪打开机会→角色选择/不选择→真实结果→重新评价”组合链 |
| NPC 社会网络 | [partial] | `npc_ecology.py` 已有 NPC actor 决策、World Author 裁决和 occurrence 结算；`npc_relationship_view.py` 只按主角与单 NPC 的共同事件派生粗略读数 | 缺 NPC–NPC actor-scoped 关系、私有近况/记忆、真实通信传播、离开重现与低成本分层；不得扩展为全局 NPC 全知图 |
| 时间标记与周年 | [partial] | Logical Time、Calendar、Biography、事件时间和 recall recency 存在 | 缺从真实日期派生的只读 temporal marker index 及稀疏 Recall/Reflection opportunity；日期不能直接生成纪念行为 |
| 小选择的路径依赖 | [partial] | settled Plan/Activity/Relationship/Life Arc、capability manifest 与 candidate environment 原件存在 | 缺统一地把已结算后果转成未来开放/关闭/调权候选的编译；不能用一次选择永久锁死路线 |
| 用户交流习惯 | [partial] | `text_turn_endpoint.py` 已使用批次语义、typing、气泡间隔、消息长度和往返气泡形状；QQ host 已接生产 | 个人节奏统计主要是 host 进程内有界 deque，重启即失；需有界 sidecar/重建策略与校准，且只能给机会时机 advisory |
| 兴趣与文化 | [partial] | Fact/Memory、Perception 候选、Life opportunity、Reflection 可复用 | 缺可修正的 actor interest material 及 perception/life/NPC/media 候选编译；不能做关键词→关注/行动映射 |
| 共同文化与关系惯例 | [partial] | shared interaction、Memory、Relationship、Thread/Commitment、Expression recall 能存取素材 | 缺重复证据、actor adoption、淡化/撤回及关系 scope；一次昵称或玩笑不能自动升级为永久惯例 |
| 边界与同意 | [active] | Consent/Capability/Privacy 的 grant/revise/revoke、Projection 与 Action preflight 已存在 | 保持为硬边界；补充撤回后的未 dispatch effect、媒体和关系 aftermath 集成证据，不新建“亲密覆盖同意”路径 |
| 不可逆损失 | [partial] | WorldOccurrence/Life outcome、Capability/Biography、NPC retired/dormant 和历史记忆提供原件 | 缺统一 terminal consequence 对 capability/entity/window 的关闭语义及防“剧情需要”复活的门禁 |
| 多视角声誉 | [partial] | actor visibility、Private Impression、NPC identity/ecology 和 relationship 原件存在 | 缺每个 actor 只基于自身 Observation/Disclosure 形成印象的通用路径；明确禁止 global reputation score |
| 修复与宽恕 | [must-not-build] | Appraisal、Affect、Relationship、Memory reconsolidation、Commitment/Action receipt 能提供新证据 | 禁止一句道歉自动清零或专用宽恕状态机；需保证后续行为证据进入同一 actor 的 Interior，由角色自行接受、部分接受或不接受 |

另有四套容易误判的机械雏形：`Goal / Resource / Attention / Location` 的 schema、reducer 与 harness
存在，但 `configs/mechanism_closure.yaml` 明确记录为没有任何生产 producer。Luna 不得把它们计为
`[active]`；只有与真实作者、consumer、composition、health 在同一交付闭合时才启用，否则保留 dormant
或删除取代。CharacterInterior 的 `turn_store.py` 同样是未接入生产 core 的 WIP，不是 durable turn 已完成的
证据。

#### 11.8.2 联动共用的最小因果语法

不为十九种体验创建十九套事件机。所有联动先尝试用下面六步表达：

```text
accepted objective change / actor-visible Observation
  → source-bound derived reading
  → Causal Opportunity（只决定谁在何时有机会考虑）
  → actor 的 CharacterInterior / NPC Interior 选择 no-change 或 typed proposal
  → 对应 authority 接受并结算真实后果
  → 新后果进入 Memory / Relationship / Affect / Life / candidate environment
```

每一步必须保留 actor、source refs、pinned cursor、epoch、privacy、expiry 与 terminal。统计、随机和 Clock
只能改变候选与机会时间；不能填 motive、emotion、meaning、choice 或 visible wording。没有变化、没想起、
不在意、改变主意和拒绝都必须是合法终态。技术失败则是另一种可观察终态，不能借角色的 no-change
掩盖。

#### 11.8.3 三个候选深 Module 的具体设计

以下是候选 Module，不是先建空接口再找调用方。Luna 必须先列出至少两个真实 consumer，并做删除测试；
只有复杂度会重新散落时才建立 seam。

##### A. `EmbodiedContext`

**职责**：在一个 actor、Logical Time 与 ledger cursor 上，把已接受的身体/环境/外观证据编译成一份只读
当前处境；隐藏不同 Life、Activity、health、appearance 与 availability 来源的冲突、有效期和优先级。

这里的“具身”不是要求接入游戏或模拟完整物理世界，而是让角色从**自己的位置和处境**理解世界，避免
每个 consumer 扫描大量投影后仍以数据库旁观者视角思考。它应把当前地点与活动、附近且可见的人/物/
事件、身体与穿着、现在/稍后/今日真正可做的能力、资源与承诺、未知或过期信息，压成小型的
actor-centric executable situation。它保留 source refs 与不确定性，但不复制原始账本、整份日历或候选
剧情。语义 affordance（“可离开”“正在交谈”“物品不在身边”）优先于大段原始对象列表。

建议保持一个读取 Interface：

```python
compile_embodied_context(actor_ref, pinned_cursor) -> EmbodiedContextReading
```

返回值只能包含 source-bound 条目，例如客观 condition、起止/恢复窗口、当前可用性约束、当前 presence、
外观/衣着连续性和 freshness；每项携带 source refs、confidence/authority 与 expiry。它不得返回“应当休息”、
“所以语气暴躁”“应该晚回”等行为建议。CharacterInterior 解释主观感受，Plan/Activity 编译器只执行
客观 capability/precondition，Media 只读取可见外观与地点证据。

复杂度必须放对位置：

- **可以简化**：一次 pinned turn 只编译一份 reading 并由 Interior、Plan、Media 等复用；事件驱动失效，
  不轮询重算；稳定层与变化层分开；普通动作由确定性 executor 完成，不让模型规划每个机械步骤；后台
  NPC/ambient 只在新证据或到期 opportunity 时调用模型。
- **不能简化**：World truth 与 actor belief、计划与经历、已受理与已完成、未知与不存在、可见与私有必须
  分开；Action 必须保留 authorization、effect-once、CAS、receipt、interrupt/unknown 和 replay。
- 同一 actor 同时最多一个互斥的 foreground physical activity；聊天、思考等非物理活动能否并行由真实
  attention/availability 和角色选择决定。打断只 suspend/cancel 尚未完成的动作，迟到结果不能重复结算。
- 长动作先产生 accepted/claimed，再以结构化 `success/failed/interrupted/timeout/unknown` 终结；终局作为
  新的 source-bound stimulus 回到同一 CharacterInterior。技术失败不被改写成角色感受或生活经历。
- 当前人物、地点、物品与可用性读取是可重建、可复核的派生状态；暂时不可见或来源过期表示 unknown，
  不是“已经不存在”。重新观察可以修订当前读数，但不改写历史事件。

生产顺序：先让 settled Life/Activity/World outcome 成为真实 producer；迁移 Appearance/Visible Physical
State 的消费者；证明 Interior、Plan 与 Media 至少三处在重复编译后再深化。技术超时绝不能写成具身状态。

##### B. `ActorEpistemicView`（取代含混的“谁知道什么”）

“消息送达”只证明某 actor **被暴露于**一段信息，不证明其阅读、理解或相信。因此 Interface 应区分：

- `visible evidence`：按 privacy/source scope 有权进入该 actor capsule 的证据；
- `exposure`：实际 delivered Expression/Media、共同 occurrence 或 actual perception 使材料对其可见；
- `adopted belief`：该 actor 的 Interior 基于可见证据形成的私人、可修正理解；
- `disclosure intention`：说话者决定披露哪些 source-bound claim；只有 Action delivered 才产生 exposure。

建议读取 Interface：

```python
compile_actor_epistemic_view(actor_ref, pinned_cursor) -> ActorEpistemicReading
```

它从 actor visibility、delivered receipt、shared occurrence、actual perception、Private Impression/belief
projection 派生，不成为第二事实权威。Expression proposal 必须显式声明要披露的 claim/source scope，系统
验证说话者有权知道且允许分享；transport receipt 只更新 exposure。对方是否采用、误解或拒绝，由对方
下一次 Interior 决定。修正与撤回追加 lineage，不改写其曾被暴露于旧信息的历史。

##### C. `RevisableSelfNarrative`

稳定 Character Core 回答“这个角色的身份与不可轻易漂移的价值边界”；Self Narrative 回答“她目前如何
理解自己”，后者允许矛盾、迟疑、局部确信、淡化和被新证据推翻。

它首先作为 CharacterInterior 中一种 actor-private、source-bound proposal 深化 Private Impression/
Reflection，而不是 World Fact。候选条目至少包含自由文本理解、适用 scope、支持/反证 refs、uncertainty、
形成时间、最近修订和状态；模型可选择不形成或不修订。只有 Reflection、Aspiration、Expression、Recall
等多个长期 consumer 确实需要同一版本化状态时，才建立 typed authority/Projection。

系统只验证 actor、来源、privacy、长度、lineage 与 epistemic namespace；不得根据活动次数自动写“她很
自律/勇敢/不适合上学”。Self Narrative 可以影响她的愿望、回忆和表达，但不能直接授予 capability，
也不能覆盖 Character Core 或客观 Biography。

#### 11.8.4 其余联动的设计落点

- **Skill / Habit**：建立只读 Practice Evidence reading，汇总已结算练习、成功、失败、中断与时距；
  CharacterInterior 可形成“我觉得这是习惯/我不想继续”的私人理解，World/Capability reviewer 只接受有
  证据的客观 affordance 变化。习惯身份与能力权限分开，拒绝 XP 自动升级。
- **Commitment Conflict**：由 compiler 输出同时有效的约定、时间窗口、资源、地点、具身限制和失败后果，
  不输出 priority/ranking。CharacterInterior 可协商、取消、拖延、违约或坚持；实际 receipt/outcome 再回流。
- **Entity/Custody**：为角色物品建立稳定 entity ref 与 owner/custodian/location/condition/status 的 typed
  consequence；获得、借出、赠送、损坏、丢失、找回只来自 accepted outcome。物品的意义仍是私人评价。
- **Imagination Namespace**：在 actor-private material 中显式区分 `belief / imagined / counterfactual /
  dream`；Recall 可取回但 Context compiler 必须保持标签，World Author、Experience、User Fact 与现实媒体
  evidence 默认拒绝这些 scope。若设想后来真实发生，必须由新的 World event 独立证明。
- **NPC Network / Reputation**：把关系读取泛化为 `subject_actor_ref → target_actor_ref`，每条 impression
  仅消费 subject 可见的 Observation、Disclosure 与共同经历。真实 NPC communication outcome 才传播信息；
  背景 NPC 使用合并 opportunity 和摘要，不建立全局声望值或逐事件全员模型调用。
- **Temporal Marker**：从真实 event/fact 的时间、Calendar/Biography 只读派生候选；相近日期只提升
  Recall/Reflection opportunity 的可见性。角色是否认为它是“纪念日”、是否记得和是否表达均由 Interior
  决定，且允许后来不再在意。
- **Candidate Environment / Path Dependence**：把 accepted capability、place、relationship、Life Arc、
  settled choice 与不可逆 outcome 编译成 World Author 当前可提出的开放候选环境。它描述“现在什么仍可能”，
  不打分“角色应该选什么”；关闭与重新开放都必须有事件来源。
- **Communication Habit**：把 QQ 的有界 cadence/shape 统计迁入可重建或可清理 sidecar，保留样本量、衰减、
  freshness 和重启行为。Text Endpoint 只预测继续输入概率；Private Impression 才能形成“她觉得用户有某种
  习惯”的主观理解。
- **Interest / Shared Culture**：兴趣与昵称、笑话、仪式先作为 source-bound、relationship-scoped memory/
  private material；重复互动只提供 adoption/revision opportunity，不自动固化。Perception/Life/Media 只能把
  它们作为相关性候选，不能据此决定关注、参加或发送。
- **Consent / Irreversible Outcome**：Consent 继续是每次 effect 前重检的硬边界；撤回阻止未 dispatch
  effect，但保留历史。不可逆 outcome 必须显式关闭 capability/entity/window，并由 reducer 拒绝无新 authority
  的复活；角色如何感受不由该标志决定。
- **Self-regulation / Repair / Forgiveness**：只补齐机会、上下文、可执行选择和后果回流，不增加专用行为
  authority。测试验证“多种选择都可达且来源闭合”，禁止断言情绪必然触发某行为或道歉必然恢复关系。

#### 11.8.5 每条联动的完成门槛

任何状态只能在以下证据齐全后升级为 `[active]`：

1. 至少一个真实 accepted producer 和一个真实下游 consumer；若是 derived reading，输入 authority 明确；
2. actor/privacy/source scope 在跨 NPC、用户、主角和 perception 场景中没有泄漏；
3. no-change 与多种角色选择可达，测试不规定特定行为；
4. 重启、并发、CAS、expiry、correction、withdrawal 和 replay 不重复 effect；
5. health 能区分未触发、角色 no-change、技术失败、积压和 consumer 未使用；
6. 真实 daemon/账本证明 producer 与 consumer 都发生过，且 token、延迟和数据库增长有界；
7. 旧旁路已删除或明确 replay-only，架构 guard 阻止新老并行。

如果只有 schema、测试 fake 或手工 record 方法，最高是 `[disconnected]`；如果仅能借通用原件表达但没有
专门闭环，最高是 `[partial]`。`[must-not-build]` 项通过其组成链的生产证据验收，而不是建立同名类。

上述连接仍不是穷举。后续发现任何新增连接必须回答：

1. 独立根源是什么；
2. 哪个 actor 通过什么来源有权知道；
3. 系统只打开了什么机会；
4. 谁作语义决定；
5. 结果由哪个现有 typed authority 接受；
6. 不发生或 no-change 是否成立；
7. 技术失败如何与角色选择区分；
8. 如何 merge/dedup/replay，并使成本有界。

如果答案是“系统根据内容直接决定角色做什么”，它是行为脚本。如果答案是“写入一个万能 dict”，
它破坏 authority。如果一个新 Module 删除后复杂度不会重新散落到多个调用方，它也不值得成为新 seam。

### 11.9 受控随机与不利事件

- RandomDraw 只选择机会时点、候选暴露或客观不确定结果，并永久记录以便 replay。
- World Author 可以提出不利偶然、失败、冲突、资源约束和开放后果，但不能借此规定主角的心理或人生。
- 角色模型可以形成令人意外、甚至“不明智”的选择，只要事实、权限和后果合法。
- 不通过目标关键词、情绪—行为矩阵、固定动机、剧情 taxonomy 或行为发生率控制“多样性”。
- 去重和最近重复证据可以作为 World Author 的环境信息，不能变成“这类事件今天禁止/必须发生”。

### 11.10 模型一次成功与语义边界

生产可靠性不能与角色自主性二选一。正确办法是让 provider 原生 Function Calling/Structured Output
保证协议外形，同时保留所有角色语义字段：

- 每个 purpose 使用最小、版本化、forced tool contract；schema/tool hash 进入 request identity。
- deterministic compiler 只做有唯一语义的规范化、身份派生和 hard invariant 验证。
- 浮点 bp→整数、ISO 时间→datetime、流式 arguments 拼接等可在证明语义等价时正规化。
- `timing_choice` 默认 `now`、补 summary/attended refs、把 stale transition 改 no-change、清空地点但保留
  含地点叙事等会改变选择，必须交同一 semantic author 一次受约束重选。
- 重选仍失败就是技术失败；不启用备用人格、本地话术或 MinimalReply 冒充角色。

每个 purpose 分别统计 transport、first-attempt structural、source-admissible、cross-field、accepted、
role no-change/silent 与 technical failure。合法 silent/no-change 是成功；解析失败不是。

### 11.11 成本、调度与账本

- 事件驱动优先；无新事实时只有稀疏 ambient opportunity。
- 同 actor、同窗口的变化合并但不丢 source refs；不按 module×entity×固定周期扫描。
- 一次 Interior Turn 尽量提出多域主观 proposal，避免各域重复调用角色模型。
- NPC 用确定性代码推进时间、资源和已接受后果，只在开放语义选择时调用低成本模型。
- embedding、检索、排序、merge 和确定性 source closure 尽量本地；便宜模型也必须通过 contract qualification。
- 预算减少机会或延后非紧急工作，不能把角色决定改写成 no-op。
- durable technical checkpoint 使用可清理 sidecar；业务事实、ModelResult、Action 和 receipt 保留在不可变账本。

### 11.12 可观察性与完成证明

只读 Causal Lineage View 应能从 source refs 展示：opportunity→Interior Turn→typed proposals→接受/拒绝
→Plan/Action/receipt→后续 consequence。它用于定位 source visibility、scheduler、model、parse、review、
CAS、Action 或 receipt 的断点，不是第二真相源，也不能作为模型的剧情摘要。

健康度至少回答：

- source 是否为正确 actor 打开机会；何时 due/claim/terminal；
- turn 看到了哪些 faculty、是否采用 Recall、哪些 proposal 被接受；
- 是否有长期 open、重复作者、跨 actor 泄漏、孤儿 authority 或 trigger storm；
- 每个 model purpose 的首次成功、延迟、token、成本、技术失败和资格样本数；
- 是否存在跨 interaction→emotion→life/NPC→memory→later choice 的生产样本。

所有依赖 Logical Time、expiry、cadence、retry、ambient window 或未来 Action 的机制还必须能被一个统一的
**隔离资格 harness** 从受控上游触发。该 harness 不是新的生产 API，也不允许每个机制建立 test-only Runtime；
它只复用 production host 已有公开 seam，在隔离生产副本中提交该机制本来就有权消费的源事件、到期条件、
已授权 Action 或正式 receipt，并推进同一套 Logical Time。随后必须由生产 scheduler 自己完成 due discovery、
claim、CharacterInterior/其他合法 semantic author、typed authority、Action/receipt 与 terminal。不得直接调用
下游 worker、伪造 ModelResult、跳过 scheduler 或在测试专用旁路里制造“成功”。虚拟时间用于穷举窗口、
重试、重启和并发组合；真实时间 daemon soak 用于证明 wall clock、时区、provider 波动和进程生命周期，
二者不能互相替代。

每个延迟机制必须在同一可审计清单中声明 source authority、due identity、merge/dedup key、model purpose、
合法 no-change/silent、成功 terminal、技术失败 terminal/retry、可见 Action 与 kill switch。清单中存在但无法
从上游触发、代码中存在但没有登记、或 health 无法区分“未到期 / 角色不做 / 技术失败 / 卡住”的机制，均
不得标为 `[active]`。确定性调度与 effect-once 组合必须 100% 通过；model-bearing purpose 仍按首次合法率
单独资格化，不能用纠正后成功或最终送达率掩盖第一次非法。

S1–S26 继续作为体验验收目录，不是剧情库：测试连接、来源、actor isolation、可达选择、合法终止和
effect-once；真实对聊判断自然度。尤其优先闭合 S9 分享余波、S10 自我回看、S18 独处内省、S19 心愿、
S20 图片分享等明确 disconnected 场景。

### 11.13 对 2026-08-06 至 08-08 阶段性修复的最终裁决

`companion-experience-roadmap.md` 保存了重要生产实证，但以下只是历史阶段性办法，不是当前规范：

| 阶段性办法 | 最终裁决 |
| --- | --- |
| 省略 `timing_choice` 默认 `now` | 删除；角色时机必须显式，Function Calling 负责提高外形成功率 |
| bare proposal 由宿主补 decision、summary、attention/source refs | 只保留无损 envelope；私人语义必须由 actor model 写出 |
| unsupported location 时清空地点保留事件 | 删除；同一 World Author 受约束重选，仍失败为技术失败 |
| stale/ambiguous 主观 transition 本地改 open/no-change | 删除改变语义的降级；只保留可证明等价的正规化 |
| prompt 抑制环境小事、鼓励特定事件类别 | 删除行为/剧情引导；提供真实能力与重复证据，由模型选内容 |
| 反思、愿望、选择固定发生次数 | 改为机会预算/merge，不规定角色形成多少主观变化 |
| repair 全失败后本地单文本模型回复 | 不部署；当前无备用角色作者，必须记录技术失败 |

仍保留的工程经验包括：真实 production clone 复现、精确 violation 回显、低温同模型纠正、合法 wire
正规化、DeepSeek 流式 tool arguments、token/TTFT 分段测量、scheduler 顶层隔离、真实 daemon 对聊和
账本证据。15/15 等小样本只能说明发现了改善，不能证明长期 99.9%。

### 11.14 本总纲的完成定义

以下是**完整愿景**完成条件，不是稳定内核第一次上线的前置条件。每个 release 只对其声明启用的能力负责；
未启用的丰富度能力必须保持关闭且不能成为核心依赖。稳定内核、生活连续性和生态丰富度按执行计划 §2
分阶段发布，每一阶段都独立通过可靠性、性能、成本、迁移和回滚门。

完整愿景标记完成时必须同时满足：

- 主角所有主观语义只经 CharacterInterior，NPC scope 独立；
- accepted changes 通过统一 typed 机会路由形成下一轮，不存在孤儿 producer/consumer；
- 用户、Life、NPC、Memory、Perception、Plan/Action、Expression/Media 至少形成双向反馈；
- Reflection→Aspiration→Choice→Plan→Outcome 可达但不强制；
- model-bearing purpose 的首次合法率、延迟与成本达到 §10；
- 冷重放、并发、重启、缓存淘汰和 provider 超时不重复模型结果或外部 Action；
- 多日真实运行出现多种因果形状，且没有剧情模板、跨 actor 泄漏、事实无中生有或长期技术失声；
- 文档、代码、`mechanism_closure`、health 与生产证据对 active/disconnected 使用同一口径。

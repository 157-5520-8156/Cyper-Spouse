# 面向仿真人长期对话的记忆与主体性架构比较

日期：2026-07-27
资料范围：论文、项目官方技术文档和 Girl-Agent 已接受 ADR；不采用二手综述作为证据。
逐项原始资料、局限和适用性记录见
[研究附录](human-like-agent-memory-architecture-primary-sources-2026-07.md)；本文件是项目采用判断。

## 结论

**“来源绑定的混合 RAG”是 Girl-Agent 必需的回忆基础设施，但不是完整答案，也没有研究证明它单独就是仿真人的最佳架构。**

现有研究中，最适合 Girl-Agent 的不是某一个现成框架，而是一个组合：

1. World V2 事件账本继续作为唯一事实来源；
2. 从账本确定性派生工作记忆、情景记忆、语义记忆、关系/Affect 和非事实性的反思记忆；
3. 用可重建 sidecar 做 lexical + dense + temporal + structured-link 混合召回；
4. 当前轮自动提供极小的工作集和宽召回候选，同时让角色模型拥有只读 `recall` 工具，可自行继续搜索、重新表述查询或忽略候选；
5. 反思可以形成角色自己的理解、自我叙事和长期倾向，但必须引用经历来源，并明确不是事实 authority；
6. 微调只承载稳定人格、语言习惯和“如何使用记忆”的能力，不把持续变化的用户事实和经历写进权重；
7. latent/recurrent state 只可作为低延迟缓存或短期连续性辅助，不能成为唯一长期记忆。

这是根据已有证据和本项目约束作出的工程综合判断，不是一个已经被业内证明的普适最优解。长期对话研究仍明显落后于人类：LoCoMo 发现 long-context 和 RAG 虽有改善，仍在长期时间/因果理解上显著落后于人类；LongMemEval 也观察到持续交互中的明显准确率下降。因此项目最终仍需用真实多轮对聊、打断、事实更新、自然回忆和主动联系来做长期实验，不能只用 QA 命中率证明“像人”。[[LoCoMo](https://arxiv.org/abs/2402.17753)] [[LongMemEval](https://arxiv.org/abs/2410.10813)]

## 比较

| 架构 | 已有证据提供了什么 | 对 Girl-Agent 的优势 | 主要缺口 | 采用方式 |
| --- | --- | --- | --- | --- |
| 分层情景/语义记忆 + 混合检索 | Soar 明确区分 temporally situated episodic memory 与 context-independent semantic memory；LongMemEval 将长期记忆拆成 indexing、retrieval、reading，并发现 session decomposition、fact-augmented keys、time-aware query expansion 有效。[[Soar EpMem](https://soar.eecs.umich.edu/soar_manual/07_EpisodicMemory/)] [[Soar SMem](https://soar.eecs.umich.edu/soar_manual/06_SemanticMemory/)] [[LongMemEval](https://arxiv.org/abs/2410.10813)] | 最能容纳时间、更新、主体、来源和失效状态；适合账本派生和审计 | 检索只决定“看见什么候选”，不能自行产生动机、情绪或自然反应 | **作为核心记忆层** |
| 完整 cognitive architecture | CoALA 将语言 agent 组织为模块化记忆、内部/外部 action space 和通用 decision cycle；Soar 也提供 working、episodic、semantic、procedural memory。[[CoALA](https://arxiv.org/abs/2309.02427)] [[Soar](https://soar.eecs.umich.edu/home/About/)] | 提供比“一个向量库”更完整的认知分层语言 | 照搬 production rules 或固定 decision cycle 会让程序替角色作语义决定，也会显著增加迁移成本 | **借用分层，不移植整套规则系统** |
| Agent-controlled retrieval | MemGPT 让模型在有限上下文中管理不同 memory tiers；Self-RAG 的实验证明固定、无条件检索会损害生成，按需检索和自我批评可提高事实性与引用准确性。[[MemGPT](https://arxiv.org/abs/2310.08560)] [[Self-RAG](https://arxiv.org/abs/2310.11511)] | 最符合“角色决定自己想进一步回忆什么”，避免系统给行为建议 | 纯 pull 模式会受模型漏检索、工具失败和额外往返延迟影响；Self-RAG 还是经过专门训练的 QA/RAG 架构，并非直接证明通用聊天模型能可靠自主回忆 | **自动小工作集 + 角色按需检索的双通道** |
| Reflection / planning | Generative Agents 保存完整经验流，动态检索并生成 higher-level reflection；其 ablation 表明 observation、planning、reflection 都对可感知的 believability 有贡献。[[Generative Agents](https://arxiv.org/abs/2304.03442)] | 能把零散经历形成角色自己的观点、牵挂和跨时段连续性，而不必枚举动机 | 自由生成的 reflection 会把推断写成事实；每轮反思也增加延迟、成本和自我强化风险 | **事件驱动、来源绑定、非事实 authority 的反思层** |
| 人格/经历微调 | Character-LLM 通过 profile、experience 和 emotional states 训练特定角色，并用 interview playground 检查人物和经历记忆。[[Character-LLM](https://arxiv.org/abs/2310.10158)] | 可让稳定语气、价值观和人物反应比长提示更内生 | 权重不能为某条回复提供可定位的经历来源；持续新事实需要重新训练；难以执行 supersession、删除、隐私隔离和 cursor replay | **只用于稳定人格与记忆使用策略，不用于在线事实存储** |
| Recurrent / latent state | Recurrent Memory Transformer 用 memory tokens 在 segment 间传递压缩状态，并在长依赖任务上优于部分基线。[[RMT](https://arxiv.org/abs/2207.06881)] | 推理路径短，可能改善最近上下文连续性和延迟 | 压缩状态不可直接检查“记住了哪条证据”；一旦压缩丢失或混淆，难以纠正、删除和重建。把它作为事实来源也无法满足 Girl-Agent 的 source binding 与历史 cursor | **只能作为可丢弃缓存，不能作为 authority** |
| Temporal graph / GraphRAG | Zep/Graphiti 报告 temporal knowledge graph 在 DMR 和 LongMemEval 上优于其比较基线；HippoRAG 用知识图与 PageRank 改善 multi-hop QA，并报告比 iterative retrieval 更低成本和延迟。[[Zep](https://arxiv.org/abs/2501.13956)] [[HippoRAG](https://arxiv.org/abs/2405.14831)] | 适合跨人物、事件、Thread、Commitment 的多跳联想和时间关系 | 由另一个 LLM 抽取出的图若成为写 authority，会与账本争夺事实；对单用户日常对话，全量 GraphRAG 也可能过重 | **复用现有 World 结构边，仅做可重建检索投影** |

表中关于 audit、删除、cursor replay 和第二事实权威的限制，是由论文机制与 Girl-Agent 不变量共同推导的工程判断；论文没有直接评测本项目的事件溯源约束。

## 为什么不是“只上一个更强的 RAG”

普通向量 RAG 擅长语义相似，但仿真人的回忆至少还需要：

- **时间**：上午的事、后来发生的更新、某条信息当时有效但现在已失效；
- **主体**：用户经历、角色经历、共同经历、角色自己说过的话不能互相替代；
- **精确内容**：人名、地点、数字、约定和原话；
- **结构联系**：同一人物、Thread、Commitment、活动与后果的跨事件关系；
- **不知道**：没有来源时不应把最相似的旧片段拼成答案。

LongMemEval 正是把 extraction、cross-session reasoning、temporal reasoning、knowledge updates 和 abstention 分开测试，并发现 time-aware query expansion 等专门设计能改善结果；LoCoMo 则显示 long-context 与常规 RAG 仍难处理长期时间和因果动态。[[LongMemEval](https://arxiv.org/abs/2410.10813)] [[LoCoMo](https://arxiv.org/abs/2402.17753)]

因此，RAG 在本项目里应叫 **Recall Index**：它只提供联想候选，不能提升候选为事实，也不能输出“现在应该安慰/追问/主动联系”之类的行为意见。

## 推荐的目标架构

```text
不可变 World Ledger（唯一 truth）
        │
        ├─ deterministic projections
        │    ├─ working：当前消息、近期轮次、活动、关系、Affect、开放事项
        │    ├─ episodic：带 actor/time/source/privacy 的具体经历
        │    ├─ semantic：带 valid-time/supersession 的稳定事实
        │    └─ reflective：有 source refs 的角色理解；明确非事实 authority
        │
        └─ rebuildable Recall Index
             ├─ lexical / exact
             ├─ dense association
             ├─ temporal filters
             └─ existing World links
                      │
             bounded prefetch candidates
                      │
角色模型  ←───────────┴─────→  read-only recall(query, filters)
   │              自行理解、继续检索或忽略
   └─ Character Decision → invariant validation → Action / receipt ledger
```

### 对“角色主体性”的边界

系统可以决定：

- 哪些已提交材料有资格被检索；
- 隐私、viewer、cursor 和 supersession 过滤；
- lexical/dense/temporal/graph 的候选生成；
- token、调用次数、延迟和重复副作用预算；
- 把检索 query、source refs、index version、模型结果和外部回执记录下来。

角色模型决定：

- 这一刻是否需要继续回忆；
- 用什么联想或时间范围搜索；
- 哪些候选与当下有关；
- 回忆带来什么主观意义；
- 是否提起、如何表达、是否沉默。

这与项目已接受的“受控的高随机”一致：系统提供有来源的世界和机会，不替角色决定行为。[[Girl-Agent ADR 0010](../adr/0010-controlled-high-variance-character-agency.md)]

### 低延迟与重放

- 热路径常驻最近工作记忆，并在第一轮模型调用前用本地索引预取少量候选；
- 精确当前事实、开放 Commitment 和最近 Thread 走结构化读取，不等待 embedding；
- 只有角色认为上下文不足时才进行第二次 `recall`；
- embedding、FTS 和图邻接是可删除重建的 sidecar，不写入不可变业务事件；
- live run 记录实际看到的 source refs、query/hash、index version、随机结果和模型输出；replay 使用已记录结果，不重新调用模型或检索器；
- latent/recurrent cache 丢失时必须能从账本投影恢复，不能改变事实结果。

## 采用顺序

1. 先补齐 source-bound、time-aware 的 lexical + structured recall，并让模型看见可理解的 excerpt；
2. 再增加 dense 召回和多路融合，但保留每项 source refs 与命中通道；
3. 增加只读 `recall` 工具和严格调用预算，比较“仅预取”“仅自主”“双通道”三组；
4. 最后才增加来源绑定的 reflection；先证明它改善跨日自然连续性，而不是增加幻觉和机械提旧事；
5. 有足够真实轨迹后，才考虑微调稳定人格、自然表达或自主检索策略；绝不把动态事实训练进模型权重。

### 当前试验状态（2026-07-27）

当前代码已经把目标架构接入 World V2 生产组合，而不再只是 lexical 探针：

- `RecallCorpusCompiler` 从精确 cursor 的近期对话、Fact、Experience/Memory、World Life、
  开放 Thread 和 PrivateImpression 编译 episodic / semantic / reflective 文档；每个文档
  保留完整不可变 source binding。新 PrivateImpression 由同一角色身份模型在游标固定的
  多层 capsule 上形成：anchor/近期 appraisal、Character Core、关系、Affect、真实经历正文
  和既有可撤销印象都只是有来源的环境；模型自己选择来源集合、是否保留、理解正文、置信度
  和生命周期。接受、拒绝、纠正和技术失败均记录独立 Model Result；新写入必须携带该 lineage，
  历史事件才允许没有它。Reducer `.39` 可从 `.36/.37` 的已验证 head 作加法迁移，历史
  impression 仍按原 appraisal 引用和原 mutation hash 重放。
- `SQLiteRecallIndex` 提供 lexical、temporal、现有 World link 及本地 feature-hash dense
  混合排名。索引是可删除 sidecar，只为新增或变化的文档计算本地向量；同内容的新 cursor
  只更新 head。`FeatureHashRecallEmbedding` 明确是零网络、非语义的可靠基线，不能冒充
  语义模型。远端语义召回默认关闭，避免仅因存在通用聊天凭据就把私密或 `withhold` 来源
  发送给额外供应商；部署者显式设置 `WORLD_V2_RECALL_SEMANTIC_ENABLED=true` 后使用 512 维
  `text-embedding-3-small`，并可用
  `WORLD_V2_RECALL_EMBEDDING_MODEL` 替换兼容模型。远端 embedding 只在角色已经选择
  `recall_request` 后、于事件循环外执行；自动预取和精确当前 Context 不等待远端
  embedding。语义向量按 provider endpoint、模型版本、维度和文本哈希
  写入最多 8192 条且序列化向量总量最多 32 MiB 的 SQLite 可重建缓存；同文档跨 pull、
  跨重启复用，仅新增文本调用供应商。调用前由跨进程 SQLite reservation 同时检查日/月 token
  和人民币预算，调用后用供应商 usage 结算；拒绝、失败、用量和估算成本进入只读健康状态。
  供应商故障或预算耗尽只降级 dense 通道，不阻断其余检索或聊天回复，实际降级原因同时进入
  Recall trace，不能再伪装成一次正常的 semantic 命中。
- Context 本身同步携带精确当前的短工作记忆；最多四项的本地预取在线程中并行准备，只允许
  一个 300ms 上限的本地调度 join，绝不调用或等待远端 embedding。若候选及时完成，它以
  标准 Capsule item 形状进入角色首轮上下文并
  立即获得审计；满载 slice 会优先保留实际被审计的候选，主模型转交备用模型时也携带同一
  provenance。若本地预取超出上限，首轮不再等待，它仍可在角色选择 `recall_request` 后与
  自主 query 共同进入第二轮；只有该角色自主 recall 才允许调用远端语义 embedding。角色
  直接作答后丢弃未完成、从未被她看见的候选。候选始终只是
  有来源的参考而非行为建议，系统不按话题或动机替她决定如何使用。
- appraisal + expression 配对认知与普通 expression 两条生产路径均支持同一次角色自主
  recall。最多一个额外模型往返，并复用原 turn 的第二调用预算；没有第三次检索或隐藏重试。
- 被首轮或第二轮模型实际看见的预取和每次角色 pull 都固定完整执行 query（actor、subject、时间、
  隐私、过滤条件与可重放 seed）、各通道分数、随机 accessibility offset、结果、
  embedding/index 版本、精确 cursor、文档和 source binding，并进入
  `model-result-audit.4`；未被模型看见的并行预取不伪装成模型证据。冷重放读取已记录
  proposal/result，不重新检索或调模型。
- 进程内保留最多 16 个不可变 cursor 搜索快照，避免并发后台认知刷新最新 sidecar 时污染
  已经 pinned 的前台回忆；SQLite 仍只保存一个最新可重建索引，不按 cursor 复制整套文档。
  paired appraisal → expression 的跨提交复用使用显式 `paired_cognition_carry` 契约：
  目标 Context 只接受全局紧邻的唯一直接前态（不能跳过其他 trigger 的中间 Context），
  runtime 签发 source/target 完整游标的 transition hash，且 trace 只能携带一次；普通
  recall 必须与 Capsule cursor 完全相等。同游标不同 trigger 的快照和预取身份也彼此隔离。
- sidecar 损坏、锁冲突或 embedding 故障会把该 cursor 的 recall 标记为不可用并记录诊断，
  Context 继续以空 recall 材料编译。单次结果、单条 trace 和总语料都有独立上限，防止审计
  超过不可变 Model Result 的 32 KB 合同或让一次自主 recall 扫描无界语料。

尚未被代码实现“证明”的部分是效果结论，而不是上述主链：语义 embedding 已具备显式启用、
预算和降级路径，但是否值得在某个部署持续开启，以及角色自写 reflection 是否确实改善自然连续性而
不造成机械提旧事，必须靠后续真实对聊和长期指标决定。微调和 latent/recurrent cache 仍按
本文件的采用顺序留在实验阶段，不属于本次 Recall Index 的完成条件。

最重要的验收不是“向量检索命中了几条”，而是：角色能否在恰当但不固定的时机自然想起；能否忽略无关记忆；面对更新能否使用新事实；没有证据时能否不编造；打断、重启和供应商切换后是否仍连续；同时保持 source closure、延迟目标和 effect-once。

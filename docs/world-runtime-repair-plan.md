# World Runtime 修复计划与验收

更新时间：2026-07-19

## 目标

World Runtime 的目标不是预先穷举人类行为，而是维护一个会继续变化的世界：

```text
时钟 / 生活 / NPC / 用户事件
    → LLM 提出事件、评价、感受与行为
    → 最小硬校验与权限检查
    → World Event / Action / Receipt
    → 情绪、关系、记忆与生活余波
    → 下一轮决策消费这些余波
```

Runtime 负责事实来源、逻辑时间、因果绑定、权限、预算、幂等和结算；LLM 负责具体的
语义判断、临时偏离、表达策略与行为选择。矩阵只能描述处境，不能把处境映射成固定话术。

## 当前修复切片

### 1. 主动行为

- `response_gap` 不能把任何后续用户消息都当成“前一个话题已结束”。
- 后续入站只重新提供上下文，继续、转向、延迟或沉默交给主动决策模型。
- `later` 必须最终经过 Action pump 与 terminal receipt，不能只创建 commitment。

### 2. 生活经历

- 已提交的 `Experience` 必须绑定不可变的 life-content sidecar。
- Experience 可以生成 source-bound `MemoryCandidate`，离开 `recent_experiences` 窗口后仍可通过
  `active_memory_candidates` 召回。
- 召回必须同时验证 Experience transition、`ExperienceCommitted`、`LifeContentRecorded` 和
  sidecar hash；任一环节缺失则 fail closed。

### 3. 主观余波

- NPC/生活事件 → appraisal → affect/relationship → 下一轮 Context 的链路必须保留。
- 验收不能只检查 Context 有字段，还要检查 affect/relationship 改变后续的 timing、主动联系、
  defer/silent 或表达选择。

### 4. 媒体

- 事件生态可以发现并选择 source-bound PhotoCandidate/MediaOpportunity。
- 没有完整的渲染、收件人绑定、授权与 delivery receipt 时，不得宣称“已经拍照并发送”。
- “看到猫并分享”应由真实环境事件或视觉证据提供来源，不使用关键词触发器伪造。

### 5. 模型失败与可见连续性

- Provider 超时、非法输出或恢复失败不再把普通入站静默结算为
  `observed_only`：首轮问候、普通消息和情绪/修复消息都会收到一个不新增世界事实的
  最小可见接住反馈。
- 这条兜底只处理“模型没有完成交接”的故障；正常模型仍可以基于忙碌、边界、情绪余波
  或明确的关系语境选择 `silent`/`later`，不能把合法的角色沉默误改成固定话术。
- 情绪型故障反馈会承认当前关系信号，但不会替模型猜测对方动机、编造经历或伪造记忆。
- 用户直接说出姓名、喜好等自我信息时，兜底会承认“听到了这次披露”，但不会在事实/记忆
  后台尚未落账时虚报“已经记住”。
- 后台情绪、事实、记忆和主动性 worker 使用独立的串行锁；它们可以彼此排队，但不会在
  外部模型调用期间占住世界变更锁，避免低优先级思考把下一条入站消息卡住。
- 宿主层的 `drain()` 也不再把后台模型调用包在入站锁里；QQ/HTTP 的普通被动调度路径和
  `scheduler_once()` 都遵守同一条可见入站优先约束。
- 活动 projection 缺少持久化 `last_transitioned_at` 时不再提供 `complete` opening；不完整的
  旧 projection 只能 pause/abandon，避免一次 scheduler wake 把“正在做的事”瞬间抹掉。
- 主动联系的 act/hold 抽样现在只作为可回放的时间/处境证据，不再直接短路模型；模型必须有
  机会自己选择现在、稍后、转向或沉默。
- Experience memory candidate 在生产组合中会在写入前核对对应的 `LifeContentRecorded`
  descriptor 与不可变 sidecar；缺失时 fail closed，不让后续召回再去“补救”污染的 candidate。
- 有 memory model 时，Experience summary 也会经过同一个受限 retention/salience lane；模型只能
  判断是否值得保留以及矩阵内的 salience，不能改写事件、来源、隐私或摘要。模型不可用时才用
  reviewed 的 world-continuity fallback，保证生活经历不会因为一次后台故障丢失。

## 完成标准

每条能力都要有可回放的：

`source → decision consumer → Action / World Event → receipt / settlement → next-turn consumption`

只有状态存在但无法改变未来行为的机制，不算完成。真实 provider、真人长期评审和线上延迟仍
属于外部验收，不在本地测试中冒充通过。

## 本轮体验证据

2026-07-19 的短真实 provider 回放曾在首轮问候和“那你现在怎么称呼我？”两个普通入站上
返回 `observed_only` 且没有外发文本；事件账本显示两次都是模型主调用与恢复调用失败后
主动选择 `withhold_after_generation_failure`，不是角色基于世界处境选择沉默。修复后同样的
5-turn 回放两条消息均为 `action_authorized` 并有外发文本；9-turn 情绪/记忆回放中模型
失败仍有可见接住反馈，世界经历问句继续使用有证据边界的“无法确认”回答。该回放的后台
模型调用仍可能造成秒级到十几秒延迟，属于 provider/调度配置的外部验收项。

## 尚未冒充完成的边界

- `LifeAftermathRuntime` 的候选集合仍由 reviewed outcome catalog 和不可变 sidecar 提供；安装
  `outcome_selection_model` 时，LLM 会在这个已观测候选集合内选择结果，provider 失败才回到可回放
  的 reviewed random fallback。现在另有一条受限的开放事件 lane：它只从当前已验证的 active
  plan/location/participant 处境中给 LLM 不透明 token，LLM 可以选择“注意到小变化、轻微受阻、NPC
  摩擦”等临时事件并写带 `moment_scope=subjective` 的主观片段，Runtime 再记录 Proposal、Occurrence
  和 activation。`no_op` 也会作为可回放的 Proposal 持久化，恢复时不会再次调用模型。它不是无限
  世界生成器；脱离已验证处境的猫、陌生人或外部事实仍然不能被模型凭空声明，主观片段也不是外部
  事实证据。当前的“接受”是受限的 Proposal → Occurrence committed/activated 效果，不等同于通用
  `AcceptanceRecorded` 类型提案系统。
- 媒体软件链现在有明确的 `deliver_approved_media_once` 应用 seam：冻结 artifact → 过期/版本化
  operator approval（若 provider 地址与领域 recipient 不同，必须显式批准精确的
  `delivery_target_ref`）→ 指定 recipient 的 Action → targeted Action pump → provider receipt →
  `MediaDeliveryShared`/interaction trigger。没有真实媒体 provider 时返回 unavailable，不把
  preview 或本地假 receipt 冒充发送；当前系统也没有伪造“收件人同意”事件，真正的用户 consent 和
  真实 provider 的网络投递、收件体验仍属于外部验收。
- 本地已有三日连续回放：affect episode 在 ClockAdvanced 中衰减，relationship slow variables
  持久保留，下一轮 Context 同时消费两者。真实 provider 的多日节奏、延迟和真人感仍属于外部验收，
  不能由本地回放冒充通过。

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

## 完成标准

每条能力都要有可回放的：

`source → decision consumer → Action / World Event → receipt / settlement → next-turn consumption`

只有状态存在但无法改变未来行为的机制，不算完成。真实 provider、真人长期评审和线上延迟仍
属于外部验收，不在本地测试中冒充通过。

# World v2 source-bound memory retrieval content

状态：实施设计（补充 `docs/world-v2-refactor-plan.md` §4B.2、§4B.11）  
日期：2026-07-15

## 问题

当前 `MemoryCandidate` authority 正确地只保存 `summary_ref`、hash 和
source binding；`Fact` 也只保存 `value_ref`、hash 与 assertion binding。
这些是可重放的身份/完整性材料，却不是模型可以理解的内容。因此现有
`active_memory_candidates` 即使进入 Capsule，也不能让模型可靠回忆具体
事项；把 `value:tea` 这类 ref 当作正文只是在依赖命名泄漏。

这违反冻结规格的要求：检索必须输出 source-bound excerpt，且不得输出
脱离来源的“记忆事实”。修复不能让 LLM summary、embedding 或候选文本提升
为事实 authority，也不能把 private/withhold 原文泄露给不具备权限的 viewer。

## 决策

新增深模块 `MemoryRetrievalCompiler`。它的单一 Interface 为：

```python
compile(cursor, scope, viewer_privacy_ceiling, budget) -> MemoryRetrievalResult
```

调用者只得到不可写、有限长度的 `MemoryRetrievalResult`；候选选择、旧源
检测、权限裁剪、原文定位和截断理由均留在模块内。

结果中的每项为 `MemoryRetrievalItem`：

```text
candidate_id, cue_kind, retention_rationales, retrieval_strength_bp
source_excerpts[]
suppression_reasons[], truncation_reason?

source_excerpt:
  source_kind, source_id, source_entity_revision
  authority_event_ref, authority_world_revision, authority_payload_hash
  source_values_hash
  excerpt_text, excerpt_payload_hash, excerpt_ref
```

`excerpt_text` 是展示/模型输入的派生读模型，不是新的 Fact 或 Experience。
每个值必须同时带上原始 authority identity；任何审计、proposal evidence 或
外部 action 都只能引用 authority event/hash，不能引用 excerpt text。

## 解析规则

1. 首先调用现有 `evaluate_memory_retrieval`。inactive、stale source、或
   privacy ceiling 超限的 candidate 一律不产生正文，并保留 suppression
   reason 供 trace/evaluator 使用。
2. 对 `fact` source，找到 exact `FactTransitionProjection`，验证其
   `accepted_event_ref`、revision 和 canonical values hash 与 binding 相等。
   再从该 Fact 的 `assertion_binding` 反查 exact `MessageObservationRef`，以
   `ObservationEventLocator.for_message` + pinned cursor 读取
   `ObservationRecorded`。读取事件的 actor/channel/payload_ref/content hash
   必须与 assertion binding 相等；只允许使用该 observation 的持久 `text`。
3. 对 `experience` 和 `terminal_thread`，在对应 authority vertical 提供
   同样的 content reader 前，不伪造摘要。返回 `content_unavailable`，不把
   `summary_ref` 当正文。后续 vertical 必须让其摘要内容以独立、hash-bound
   sidecar 存储，并使用相同 privacy gate。
4. excerpt 只可从 source text 做确定性长度截断（Unicode code-point 边界、
   固定上限、保留原文前缀）；`excerpt_payload_hash` 是截断前 exact source
   text 的 SHA-256，`excerpt_ref` 是 source observation ref。不得调用 LLM
   重写、概括或抽样。
5. 选择顺序仍由 candidate relevance × strength × recency 决定；截断和未选
   中原因进入 Capsule token budget trace。读取本身绝不 Reinforce candidate。

## Context 接线

`LedgerContextResolver` 不再把裸 `MemoryCandidateProjection` 当作模型内容。
它保留该 projection 用于 evidence/proof，并将
`MemoryRetrievalItem` 作为 `active_memory_candidates` 的模型视图。Context
metadata 必须继续列出每个 source authority ref/hash；Deliberation 的证据校验
只认可这些 authority binding。

这保持现有单向依赖：

```text
MemoryCandidate authority → retrieval compiler (read-only) → Context Capsule
                                                     ↘ trace / evaluator
```

没有 `MemoryRetrieval` ledger event、没有 reducer side effect，也没有第二条
写 seam。

## 隐私与失败语义

- `withhold`、超 viewer ceiling、source proof 缺失、source hash 不匹配、无 text
  或 text 超预算时 fail closed；候选不提供 excerpt。
- `content_unavailable`、`privacy_ceiling`、`stale_source`、`budget_exhausted`
  是显式 trace reason，不能静默回退到 ref 名称。
- 运行时不得为了填满 Context 读取未绑定 observation、同一 observation 的别名，
  或较新 cursor 的内容。

## 验收

1. active Fact-bound candidate 能在下一轮 Capsule 产生带 exact observation
   binding 的 excerpt；模型 prompt 可见该正文与 authority metadata。
2. 篡改 source event/hash、assertion envelope、cursor 或 candidate binding 时
   retrieval fail closed。
3. private/withhold 与较低 viewer ceiling 不泄露正文；读取不改变 candidate
   strength/reinforcement/history。
4. Experience/Thread 在 sidecar 未实现前明确 `content_unavailable`；不得把
   `summary_ref` 伪装成文本。
5. SQLite restart/replay 对相同 cursor 得到相同 item set、excerpt 和
   truncation log。

# 情绪拟真升级：心理场、驱力仲裁与可回放受控随机

状态：v1 已接入主回复路径，继续体验校准

目标不是把更多“用户说 A -> 角色必须 B”的规则写进系统，而是在 World 情绪物理之上增加一个可错的读空气层。它给主回复模型提供心理场、驱力张力和候选表达分布；WorldKernel 仍然是唯一能提交情绪、关系、私有印象和 Action 的权威。

## 设计原则

- 情绪机只产生 Advisory，不直接改 World projection。
- 本地轻量 LLM 只能作为 `LocalAffectReader`，负责语用读空气；它的失败不得阻塞回复。
- 情绪矩阵输出 reading、stakes、ownership、uncertainty、drive delta 和 affordance，而不是固定行为。
- 受控随机必须可回放：候选、权重、seed hash 和选中 affordance 写入 outgoing Action trace。
- 用户心理不能成为 User Fact；只能是可错的 `UserAffect` 或 `PrivateImpression`。
- NPC/世界压力可以影响角色语气、追问压力和 afterthought，但不能被错误归因给用户。

## v1 实现范围

当前 v1 新增 `AffectiveAdvisoryEngine`：

```python
async def advise(frame: TurnFrame) -> AffectAdvisory
```

它读取 `TurnFrame`，输出：

- `readings`：例如 `possible_disappointment`、`control_pressure`、`warmth_received`、`world_stress`；
- `drive_deltas`：例如 `care`、`repair`、`autonomy`、`dignity`、`avoidance`；
- `expression_affordances`：例如 `soft_repair`、`let_it_pass`、`withdraw_slightly`、`set_boundary`；
- `selected_affordance`：由 `world_id + revision + message_id + rule_version` 确定性采样；
- `persistence_candidates`：提示后续是否值得提交 `PrivateImpression` 或用户情绪账本。

v1 已完成以下闭环：

- advisory 注入主回复 prompt，明确标注为参考建议而非事实或命令；
- `current_text` 只作为内部情绪机输入，不导出到 `TurnFrame.prompt_payload()` /
  `prompt_delta()`，避免当前消息在主 prompt 中重复占 token；
- 采样结果写入 outgoing Action trace；
- 新增 `ExpressionAffordanceSelected` World event，并在 projection 中保留
  `last_expression_affordance`，下一轮作为节奏连续性 advisory 回流；
- 高置信 `possible_disappointment` 可在缺少正式 `UserAffectAppraisal` 时提交为
  有过期时间的 `PrivateImpression`；
- 强度足够、置信足够、且 evidence span 逐字引用当前消息的 `possible_disappointment`，可在缺少
  正式 `UserAffectAppraisal` 时桥接为 `UserAffectAppraised`；轻微信号、当场解决信号或无逐字证据
  不入账；
- selected affordance 会调制表达编排：修复/靠近类倾向保留短多段，收住/边界/撤回类倾向
  合并成单段，避免“受伤却轻快连发”；
- selected affordance 会调制可取消 afterthought 的机会：修复/在意/延迟余波提高概率，
  收住/边界/带过降低或清空概率；它只改变机会，不固定内容；
- QQ runtime observation 记录 `message_kinds`、`segment_count`、`multi_segment`、情绪 reading kind、
  候选/选中 affordance kind、用户情绪/私密印象是否入账，用于后续体验统计；
- `summarize_qq_turn_experience()` 可直接汇总 redacted QQ/NapCat JSONL：单气泡普通回复率、多段率、
  afterthought 率、selected affordance 分布、情绪 reading 计数、用户情绪/私密印象入账率；该汇总
  不读取消息文本、用户标识、evidence span、私密印象内容或平台回执。

v1 仍不改变 `world_affect.py` 的确定性情绪物理；它只提供心理场与表达候选，真正的情绪、
关系、私有印象和 Action 仍由 World event 提交。

## 后续扩展

1. 增加真实本地轻量 LLM adapter，实现讽刺、潜台词、关系试探、轻微冷淡的读空气。
2. 继续补误伤率和固定模式率的 redacted/人工复核流程；这两项需要真实样本标签或更可靠的后续纠错信号，
   不能只靠运行日志证明。
3. 用真实 QQ/NapCat 样本和人工复核校准权重，避免过敏、恢复过快或长期机械。

## 验收重点

- 同一类失望输入不能固定变成安慰；应存在合法的收住、轻修复、别扭、带过等候选。
- 关系阶段、AI 自身失误、用户控制压力和世界压力必须改变候选权重。
- 本地 LLM 失败不得导致静默、模板化兜底或额外串行等待。
- replay 同一 World 历史时，selected affordance 必须一致。

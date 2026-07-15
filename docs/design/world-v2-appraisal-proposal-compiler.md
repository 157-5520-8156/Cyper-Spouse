# World v2：通用 Appraisal 到类型化候选的编译器

状态：已实现 `activate` 编译与接受链；`contradict`、`supersede` 仍未实现。  
契约：`appraisal-proposal-compiler.1`

## 要解决的断点

主 Deliberation 可以从 Context 和 advisory 识别“用户失望”“被忽略”“边界受压”等意义，但通用 `DecisionProposal` 不是 Appraisal 的写入权威。若直接把模型的 JSON 当作 `AppraisalAccepted`，模型将能伪造 source、appraisal ID、接受事件 ID 和触发完成状态；若只把 advisory 放进 prompt，理解则不会跨轮留下余波。

编译器提供二者之间唯一的窄 seam：

```text
ObservationRecorded + 已 claim interaction_appraisal Trigger
  -> Pinned Turn / Proposal audit (通用 DecisionProposal)
  -> AppraisalProposalCompiler.record(cursor, proposal_id)
  -> ProposalRecorded (类型化、未接受的 Appraisal 候选)
  -> AppraisalAcceptanceRuntime.accept_runtime_owned(...)
  -> AppraisalAccepted + TriggerProcessCompleted
```

此模块不调用模型、不调用分类器、不接受提案、不发送回复。它只把已审计的“解释候选”编译成可由既有 Acceptance lane 验证的候选。

`AppraisalProposalWorker` 是该 compiler 的后台执行 Module。其唯一 Interface 是
`process(world_id, cursor, proposal_id)`：先编译、再用 opaque acceptance handle
原子接受。调度器可持久化并重试这三个输入，但不能替换中间 proposal、事件或时间戳；
worker 也不拥有回复生成能力，因此不能延长用户可见的首 token 路径。

## 来源与身份

1. `DecisionProposalAuthorityReader` 在精确 cursor 重读 Proposal audit、ModelResult audit 与 canonical proposal bytes。
2. `proposal.trigger_ref` 必须指向已提交 `ObservationRecorded`；编译器重新解析 Observation。
3. Observation 的 `observation_id` 决定唯一的 `interaction_appraisal` Trigger identity，并要求该 Trigger 正处于 claimed。
4. Appraisal evidence 只能来自通用提案中已声明的 evidence，且必须包含该 Observation；Reducer 再对 revision/hash 进行账本验证。
5. 模型提供的 `appraisal_id` 与 target id 都只是建议，不成为账本实体身份。实体、proposal、transition、acceptance 与 mutation event ID 均由编译器依据 source Proposal event 和 change 生成。
6. `change_id` 保留通用提案的 identity，使同一份审计提案之后重新 Deliberate 时可引用已接受 Appraisal；类型化 Proposal event 的 `causation_id` 指向通用 Proposal audit event。

## 分类而非话术规则

模型提供 meaning candidates、attribution、数值 severity/confidence 和可选 expiry。编译器只做稳定的表示转换：

- candidate confidence 归一化为 Appraisal hypothesis 权重（总和固定为 10,000）；
- severity 数值映射到已安装的 `low/moderate/high/acute` 分类坐标；
- 当前通用 envelope 尚未含 controllability，首版用明确的保守坐标 `partly_controllable`，不由此决定回复或行为；后续 envelope 版本会把 controllability 作为模型分类字段；
- 未提供 expiry 时使用固定、可回放的两小时解释窗口。

以上只定义账本中的可比较坐标，不映射为“必须安慰/冷淡/追问”等社会行为。

## 同轮 affect 的边界

一个泛化 DecisionProposal 的 cursor 在 Appraisal 被接受后必然过期；因此不得在旧 cursor 上继续把同一提案的 affect 写入账本。正确序列是：先接受 Appraisal，随后在新 cursor 上由后台 affect deliberation 使用新的 Context 决策是否产生 affect。这样避免基于陈旧世界状态接受情绪，也使“识别到了但不形成余波”成为模型可选结果，而不是硬规则。

## 验收

`tests/world_v2/test_appraisal_proposal_compiler.py` 覆盖：

- 通用、已审计、来源绑定的 `activate`；
- 生成类型化 Appraisal candidate；
- 原子接受后 Appraisal 存在、保留 generic `change_id`，且 source Trigger 进入 terminal。

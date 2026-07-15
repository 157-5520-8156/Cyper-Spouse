# ADR 0005: 最小回复接受车道

状态：已接受（实现中）  
日期：2026-07-15

## 决策

普通对话的 `MinimalProposal` 不复用 Fact v3 接受车道，也不把
`acceptance-manifest.3` 放宽成可携带任意 effect。新增封闭的
`minimal_reply` 接受车道，只授权一类最小外部行为：一条由单个
expression beat 支撑的 `reply`。

它的深模块接口为：

```python
class MinimalReplyAcceptance:
    def accept(
        self, *, cursor: ProjectionCursor, proposal_id: str
    ) -> MinimalReplyAcceptanceResult: ...
```

接口不接收模型生成的 event、action、预算 reservation 或 manifest；这些值只能由
内部的已审计 proposal、固定策略和当前投影重新派生。

## 输入和权威

| 输入 | 来源 | 允许的用途 |
| --- | --- | --- |
| `proposal_id + cursor` | `PinnedTurn` 的 `ProposalAuditCommit` | 精确定位已审计 proposal |
| `MinimalProposal` | `ProposalRecorded.proposal_json` | 仅重建单 beat reply 意图 |
| `ReplyBudgetPolicy` | composition root | 选择 chat account、额度、actor、目标、恢复策略 |
| `BudgetAccount` | 当前权威投影 | 检查 category 和可用额度 |

以下数据绝不能由调用方或模型直接提供：`action_id`、`reservation_id`、事件 ID、
idempotency key、accepted manifest、effect hash、预算 account 或执行策略。

## 精确允许的 proposal 形状

1. `proposal_kind == "minimal"`。
2. 恰好一个 `expression_plan_transition(accept)`，其中恰好一个 beat，且
   `inline_text == response_text`、beat `payload_hash == sha256(response_text)`。
3. 恰好一个 `external_action/reply` intent；它必须绑定该 change 和 beat，payload
   ref/hash 与物化后的 message payload 相同。
4. 不允许 fact claim、关系/情绪/记忆变更、media、工具、followup、多 beat 或裸文本
   Action。

没有 expression/action 的 `defer` 是已审计但无副作用的拒绝/延后决定；它不能凭空
创建 Action。

## 原子事件顺序

接受成功的 batch 固定为：

```text
AcceptanceRecorded(minimal-reply manifest)
  → MessagePayloadStored(immutable text/hash)
  → ExpressionPlanAccepted
  → ExpressionBeatAuthorized
  → BudgetReserved
  → ActionAuthorized
```

`ActionScheduled` 不属于 Acceptance；它由后续 ActionPump 单独 claim/schedule。
`ActionAuthorized` 必须引用同 batch 已预留的 reservation；message payload、beat、
intent 和 action 的 hash/ref 必须逐一闭合。任一项不匹配，整批 CAS 回滚。

## 与 Fact v3 的隔离

现有 `acceptance-manifest.3`、`FactCommittedV2`、Fact reducer 与 Fact recorder 保持不变。
新车道使用独立 manifest version 和 opaque bundle/recorder capability。ledger 的
`commit()`/`commit_at_cursor()` 仍一律拒绝 accepted manifest；只有配置了同一
`AcceptedLedgerBatchIssuer` 的 reply recorder 可以调用 `commit_accepted()`。

batch invariant 和 reducer 都按 manifest/audit contract 分派：

```text
fact-commit-proposal-audit.2     → Fact v3 exact invariant
proposal-envelope-audit.1/minimal → minimal-reply exact invariant
其他                             → fail closed
```

这不是通用“任意 proposal 接受”开关。Decision、continuation、情绪、关系、记忆、NPC
和媒体继续等待各自的封闭 vertical。

## 失败与重试

| 情况 | 结果 |
| --- | --- |
| audit/cursor 不精确、已决定 proposal、shape/hash 不合法 | 拒绝，零 effect |
| 无 chat account、额度不足、account category 不符 | 拒绝，零 effect |
| CAS world revision 已变化 | stale，旧 proposal 不复用，重新 Deliberation |
| 相同 acceptance retry | 相同 commit ID 的幂等结果 |
| dispatch/provider 失败 | 不改变 acceptance；由 ActionPump/settle 处理 |

## 验收

- 合法 MinimalProposal 产生上述六个原子事件，投影包含不可变 payload、plan、beat、
  reservation 与 `authorized` Action。
- 伪造任一 proposal/intent/beat/payload/action/budget/manifest/hash/cause 都 fail closed。
- SQLite crash/reopen/rebuild 与内存 replay hash 一致。
- Fact v3 fixture 保持 byte/replay 兼容。
- Runtime 只在成功时返回 `action_authorized` 与 action ID；平台执行仍经 ActionPump。

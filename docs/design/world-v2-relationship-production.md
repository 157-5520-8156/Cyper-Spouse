# World v2 关系 authority 的生产接入设计

状态：冻结设计，未接入 production composition
日期：2026-07-16
关联：`world-v2-refactor-plan.md`、`world-v2-image-machine-integration.md`

## 1. 问题与边界

当前关系 reducer、typed proposal 和 acceptance 校验已经是严格的 authority：
`RelationshipSignalAccepted`、`RelationshipSlowVariableAdjusted` 与
`BoundaryChanged` 都必须经过 `ProposalRecorded → AcceptanceRecorded → mutation`。
但 `WorldV2TurnApplication` 没有关系 worker，生产 input 不会自然积累
`relationship_states`。因此 P3 虽然能验证已有的 relationship head，却无法
依赖生产路径生成一个 head。

本设计不把关系做成关键词规则或 host API：

- host 只能提交 observation、clock、receipt；**没有** `set_relationship`、
  `increase_closeness` 或「为图片升级关系」接口；
- 模型只能给出一个受限的关系解释候选；它不提供 event id、revision、
  evidence ref、stage、hysteresis、commitment ref 或 acceptance；
- compiler 从已接受 appraisal/interaction 的固定 cursor 重读来源，唯一地
  计算 typed proposal；reducer 保持唯一 relationship state authority；
- 关系变化不直接决定回复、安慰、打断或图片。它只是下一轮 deliberation 和
  P3 authorization 可采纳或拒绝的 context。

## 2. 领域模型与来源矩阵

| 概念 | 唯一 authority | 可被模型提出 | 禁止来源 |
| --- | --- | --- | --- |
| interaction appraisal | `AppraisalAccepted` / 已结算互动 | 对当前输入的 fallible interpretation | 自由 summary、角色自述 |
| relationship signal | `RelationshipSignalAccepted` | signal class、置信度、持久性、无变化 | 关键词映射、图片机会 |
| slow-variable adjustment | `RelationshipSlowVariableAdjusted` | bounded delta suggestion、rationale code | 直接 stage/score、wall clock |
| stage / hysteresis | relationship reducer | 否 | 模型、host、图片机 |
| boundary | `BoundaryChanged` 独立 vertical | 边界候选（另行 contract） | relationship score 自动推导 |
| P3 audience context | pinned `RelationshipStateProjection` | 否 | 提示词 prose、用户昵称 |

关系信号与情绪/appraisal 不同：一次 appraisal 可以不产生关系信号；一次信号
可以被 reducer 接受却因 dwell/hysteresis 不改变 stage。这样「察觉到用户失望」
不会自动等于亲密度升高，冒犯、冷淡、修复、长期可靠性也能留下不同方向的
残留。

## 3. 生产拓扑

```text
Observation / settled interaction
  → interaction appraisal trigger
  → AppraisalAccepted (or explicit no-change)
  → RelationshipEvaluationTriggerOpened (effect-once)
  → claim lease
  → relationship-evaluation model audit
  → RelationshipProposalCompiler
  → ProposalRecorded (relationship.1)
  → RelationshipAcceptanceRuntime
  → AcceptanceRecorded + RelationshipSignalAccepted
  → [optional later adjustment proposal/acceptance]
  → RelationshipStateProjection
  → next ContextCapsule / P3 resolver
```

`signal` 和 `adjustment` 分两次 accepted transaction：adjustment 只能读取已经
accepted 的 signal，且可消费该 signal 一次。这样 crash/retry、冲突和「模型先
说结果后补证据」都无法绕过 relationship reducer。

模型 audit 可以返回 `no_change`。这个结果也必须 completion trigger，避免
同一 appraisal 在重启后反复被解释、反复消耗 token 或叠加关系变化。

## 4. 新模块接口

### 4.1 `RelationshipEvaluationDraftAdapter`

输入是最小化的 pinned capsule：已接受 appraisal 的来源、关系当前 revision/
stage/variables、已打开边界和少量未消费 signal 摘要。输出为版本化 JSON：

```json
{
  "decision": "signal | no_change",
  "signal_code": "reliability_follow_through",
  "confidence_bp": 0,
  "persistence": "session | durable",
  "rationale_code": "source_bound_code",
  "suggested_deltas": {
    "trust_bp": 0,
    "closeness_bp": 0,
    "respect_bp": 0,
    "reliability_bp": 0,
    "mutuality_bp": 0,
    "repair_confidence_bp": 0
  }
}
```

这不是硬行为矩阵：`signal_code` 和 delta 是候选解释；`MatrixCatalog` 提供可用
分类、置信度/持久性语义和风险标签，但不把任一分类映射为固定关系动作。模型
也可以在同一来源选择 no-change。

### 4.2 `RelationshipProposalCompiler`

公开方法仅为：

```python
record(world_id: str, cursor: ProjectionCursor, audit_proposal_id: str)
  -> RelationshipProposalCompilation
```

它必须：

1. pin exact audit event 与 claimed trigger；
2. 从 `AppraisalAccepted`/settled interaction 解析唯一 evidence；
3. 校验 subject 与当前 single relationship head；
4. 自行生成 ids、expected revision、evidence refs、policy refs、hash；
5. 对 signal 写 `RelationshipProposalProjection`；
6. 在 signal accepted 后，才允许 compiler 针对未消费 signal 写 adjustment；
7. 调用关系 reducer 的纯预检，确保 before/after、stage 和 hysteresis 完全由
   当前 projection + policy 得出；
8. no-change 时 completion trigger，不写 relationship proposal。

任何无法绑定的来源、多个主关系、stale cursor、无 installed policy、模型要求
跨越 stage 或超出 delta cap 都 fail closed。

### 4.3 `RelationshipAcceptanceRuntime`

它复用 `AcceptedLedgerBatchIssuer`，并只接受 reader 发出的不可序列化 handle。
一个 batch 固定为：

1. `AcceptanceRecorded`（manifest hash 与 proposal exact binding）；
2. `RelationshipSignalAccepted` **或** `RelationshipSlowVariableAdjusted`；
3. `TriggerProcessCompleted`。

不把 relationship mutation 与 Affect、Memory、Action 或 Media event 混在同一
batch；需要跨域消费者时，由接受后的 event 显式触发下游 process。

### 4.4 `RelationshipDeliberationWorker`

worker 输入 `(world_id, cursor, appraisal_event_ref)`，负责 claim、审计、compile、
accept、completion 和 recovery；与 `AffectDeliberationWorker` 一样，不拥有平台
发送能力。`WorldV2TurnApplication` 仅暴露内部 `drain_relationship_once()` 或把它
挂入 existing scheduler，不暴露写接口给 HTTP/QQ host。

## 5. 触发与并发规则

| 情况 | 处理 |
| --- | --- |
| appraisal no-change | 不开 relationship trigger |
| accepted appraisal | deterministic effect-once trigger，source 为 appraisal mutation event |
| 同一 trigger 重试 | 复用已有 model audit/proposal；不得第二次调用模型 |
| 已有 pending proposal | pin 后直接 accept 或依据 lease recovery，不重编译 |
| 多条 appraisal 并发 | CAS；后一条重新读 head，不合并 mutation |
| user 新输入 | 不取消已 accepted relationship fact；可使未 claim audit 过期并在新 cursor 重审 |
| relationship 降级/边界变化 | P3 resolver 下轮重新读 head；旧 P3 selection/sidecar 不得升级或重用 |
| 图片候选 | 永远不是 relationship trigger 的来源 |

## 6. 与 P3 的连接

P3 只消费 current `RelationshipStateProjection` 的 `close_friend`（当前窄 lane）。
relationship origin 必须指向实际 accepted adjustment event；P3 snapshot compiler
再次打开该 event，核对 recipient、revision、stage、policy digest 与 outer
authorization digest。因此一个 P3 selection 不能因为关系后来升降级而被复写。

`ambiguous`、`lover`、coverage/shared ritual 和 `exclusive_private` 不属于本垂直
的首个交付。它们需要单独的 commitment/consent/coverage authority，不能仅靠
更高的 relationship score 开门。

## 7. 验收矩阵

| ID | 场景 | 断言 |
| --- | --- | --- |
| REL-PROD-001 | accepted appraisal → relationship no-change | trigger terminal，零 relationship mutation |
| REL-PROD-002 | source-bound reliable interaction | signal 与 adjustment 都有 exact appraisal evidence、replay hash 一致 |
| REL-PROD-003 | 模型直接报 stage/大 delta | compiler 拒绝，head 不变 |
| REL-PROD-004 | crash after model audit | recovery 复用 audit，模型调用数不变 |
| REL-PROD-005 | concurrent appraisals | CAS 后只有合法顺序；无双消费 signal |
| REL-PROD-006 | recipient mismatch / boundary active | P3 selection fail closed |
| REL-PROD-007 | 多轮累计到 close_friend | reducer 的 confirmation+dwell 产生阶段；没有 direct stage setter |
| REL-PROD-008 | P3 full path | private declaration → candidate → selection `.2` → V3 sidecar → planner bridge；recipient/basis/digest exact |
| REL-PROD-009 | host inspection | HTTP/QQ host 无 relationship ledger writer/import |

## 8. 不可接受的捷径

- 在 `WorldV2TurnApplication` 增加 `adjust_relationship(...)`；
- 让图片机把「用户是恋人」或 visibility 反写 relationship；
- 从 memory summary、昵称、聊天次数直接初始化 close_friend；
- 将 matrix row 写成“失望→安慰→亲密度+X”；
- 以 P3 成功率为由跳过 relation origin 或过期检查。

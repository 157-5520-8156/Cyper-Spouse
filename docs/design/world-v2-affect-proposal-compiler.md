# World v2：主 Deliberation 到 Affect proposal 的受信任编译

状态：施工规格（实现中的 authority vertical）  
范围：把一次已经审计的 `DecisionProposal` 中的情绪候选，变成可接受、可拒绝、可重放的 `AffectProposalProjection`；不授权模型直接写 AffectEpisode，也不改变即时回复的热路径。

## 1. 要解决的断点

当前 `PinnedTurnCompiler` 能在固定 cursor 上保存 `ModelResultRecorded + ProposalRecordedV2`；`AffectAcceptanceRuntime` 能安全接受一个已存在的 typed Affect proposal。但两者之间没有生产 Module。因此测试能构造 Affect proposal，正常主模型却不能。

不得用“Appraisal 一经接受就按规则产生某个情绪”的捷径连接二者。Appraisal 是可错的意义判断；是否形成 Affect、形成何种混合情绪、是否外露，仍是主 Deliberation 的选择。

## 2. 权威流与时序

```text
Observation
  -> PinnedTurn (即时回复所需的 Context + Model/Proposal audit)
  -> Appraisal acceptance (如有；独立 accepted batch)
  -> AffectDeliberation trigger (后台，不阻塞回复)
  -> fresh ContextCapsule（已包含 accepted Appraisal）
  -> DecisionProposal.affect_decision
  -> AffectProposalCompiler
  -> ProposalRecorded(typed affect candidate；仅 deliberation revision)
  -> AffectAcceptanceRuntime / reject / stale
  -> 下一轮 ContextCapsule 的 Affect slice
```

`affect_decision` 只允许：

| 值 | `affect_transition` 数量 | 账本结果 |
|---|---:|---|
| `no_change` | 0 | 原通用 ProposalAudit 即为审计；不得伪造 rejected Affect proposal |
| `propose` | 1 | 编译出一个 typed Affect proposal，等待 Acceptance B |

这不是“检测到负面话术必须安慰/必须生气”的规则。模型可基于关系、生活、人格、当前 Affect、Appraisal alternatives、drive 和受控随机，在同一事实下选择任一合法分支；编译器只验证来源、数值范围与账本一致性。

## 3. 深 Module 与 Interface

`AffectProposalCompiler` 是一个深 Module。Runtime/worker 只知道：给出一个已入账的通用 proposal ID 和当前 cursor，得到无变化审计或一个已持久化的 typed candidate。

```python
class AffectProposalCompiler:
    def record(
        self,
        *,
        world_id: str,
        cursor: ProjectionCursor,
        proposal_id: str,
    ) -> AffectProposalCompilation: ...
```

Interface 不接收：裸 `ProposalAuditProjection`、裸 `TypedChange`、模型填写的 event id、accepted hash、after-image、actor、时间或 `WorldEvent`。这些均由 Implementation 从 ledger 的精确前缀取得或推导。

返回值只分为：

- `NoAffectChange`：绑定 generic `ProposalRecordedV2` event/hash、model result、capsule 和 cursor；不写 typed candidate；
- `AffectCandidateRecorded`：绑定 generic proposal 与新 typed `ProposalRecorded` event/hash、typed proposal ID、commit cursor；
- stable error：`affect_proposal.*`，包括 stale、cross-world、audit tamper、source mismatch、policy mismatch 与 unsupported transition。

## 4. 精确 authority 与 provenance

编译前 reader 必须在 `ledger.project_at(cursor)` 中找到 generic `ProposalAuditProjection`，随后重新读取其 `event_ref` 并逐项验证：

1. event 是同 world 的 `ProposalRecorded`，payload hash 等于 audit index；
2. `ProposalRecordedV2Payload` canonical bytes、proposal hash、model result ref、capsule id、trigger ref、evaluated world revision 全等；
3. audit 所指最终 `ModelResultRecorded` 已在该 prefix，且 call/attempt/capsule/result identity 完整；
4. proposal 的 evaluated world revision 等于编译 cursor 的 world revision；否则旧提案不重用，返回 stale；
5. source `TypedChange` 是唯一 `affect_transition`，且与 `affect_decision` 双向一致。

typed Affect proposal 需要额外保留以下不可省略的 provenance：

```text
authority_contract_ref = affect-proposal-compiler.1
source_proposal_event_ref / source_proposal_event_payload_hash
source_model_result_ref / source_capsule_id
source_change_id / source_change_payload_hash
```

Reducer 重放时重新在 `proposal_audits` 中解析 generic proposal，确认这些字段及源 change 都精确匹配；不能仅信任 compiler 曾经运行过。

## 5. 模型能选择什么，编译器能推导什么

模型的 affect typed payload 只表达社会/心理选择：

```text
transition: open | update | resolve | supersede
target: existing episode 或 source-cluster target
appraisal_change_refs[]
component_deltas[]: dimension + signed fixed point
decay selector / residue selector
evidence refs
```

编译器从受信任 projection 推导：

- `proposal_id`、`change_id`、`transition_id`、`acceptance_id`；
- current entity revision、component before/after、clamp、active episode merge；
- 所有 `AppraisalMeaningRef`、source cluster、evidence authority；
- 安装的 matrix/policy/decay profile digest；
- origin 的未来 accepted mutation event identity；
- canonical typed proposal/event identity/idempotency key。

模型不能控制上述字段，也不能凭空引用未接受、expired 或不同 source cluster 的 appraisal。`baseline_adjust` 与机械 decay 不属于该 Module：前者需要长期校准证据，后者只由 logical clock lane 触发。

## 6. 后台 worker 与低延迟

即时 `ingest()` 只提交 observation、必要的即时 reply deliberation 和 reply acceptance。Appraisal→Affect 使用单独的 trigger/worker：

- Appraisal accepted 后打开 effect-once trigger；
- worker 在最新 cursor 重新编译 Capsule，因而看得到 accepted Appraisal；
- worker 记录通用审计，再调用本 compiler；
- compiler/acceptance CAS 失败时记录 stale，打开新的 trigger，绝不复用旧 Capsule/Proposal；
- no-change 关闭 trigger 并保留 generic audit；
- 同一 trigger 的并发 worker join 同一 attempt，不增加第二个模型调用。

因此负面情绪可在下一轮成为真实“余波”，但不会把用户等待首 token 的关键路径串上第二次模型调用。

## 7. 验收与攻击测试

1. in-memory 与 SQLite：accepted Appraisal -> fresh Affect Capsule -> model proposes -> source-bound typed candidate -> accepted Affect -> next Capsule；
2. `no_change`：只产生 generic audit 和 terminal trigger，没有 Affect proposal/episode；
3. model 伪造 accepted event id、revision、origin、after-image、foreign appraisal、两个 affect changes、错误 source hash 均 fail closed；
4. stale：世界在 audit 后变化，旧 attempt 只能 stale；fresh cursor 重新 deliberation；
5. duplicate/parallel worker：同一 candidate 与 acceptance effect-once；
6. replay/reopen：generic audit、typed candidate、acceptance manifest 与 Affect reducer 均重建同一 semantic hash；
7. performance：即时 reply 路径不等待 affect worker；worker 的 model route/token/queue delay 写入 redacted metrics。


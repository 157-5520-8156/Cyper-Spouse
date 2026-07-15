# World V2 可验证历史前缀设计

> 状态：实现中；尚未接通任何 production authority reader。
>
> 范围：为 World v2 的 exact historical source read 提供可验证、可缓存的
> ledger prefix 能力。第一位消费者是 Fact evidence resolver；Memory、Experience
> 与 Situation 只能在其后复用该能力，不能各自实现历史读取捷径。

## 1. 问题与结论

World v2 不能把“用户曾说过什么”变成聊天摘要或模糊检索结果。Fact、记忆和
关系状态需要从一个确定 cursor 下的原始 observation event 读取准确 envelope。

现有 `LedgerPort.observation_events_at(locators, cursor)` 已冻结以下基础合同：

- locator 由 pinned projection 枚举，且是 `(observation_id, event_type,
  idempotency_key)` 的 canonical 有序集合；
- message 与 operator observation 即使共享 observation id，也必须提供两个
  locator，不能 alias；
- cursor 必须是 commit tail；返回 event、其精确三轴 cursor 和 envelope hash，
  但不授予任何 authority；
- 找不到预期 locator 的消费者必须 fail closed，不能切换 event family、昵称或
  最新状态作 fallback；
- 每个提交均有固定的事件数/字节写入预算；memory 和 SQLite adapter 使用同一
  preflight 合同。

但“每次读都从 genesis replay 到 cursor”仍是错误的实现边界：它使一次 1 条
observation 的请求随着世界历史长度线性变慢，也不能证明数据库查询没有悄悄
漏掉一条应有的 locator。为此引入 `ledger_prefix_proof` 深模块。

## 2. 不变式与威胁模型

### 2.1 必须证明的内容

给定 process-local verified handle、commit-tail cursor 和一批预期 locator，reader
必须同时证明：

1. 该 cursor 是连续 append-only event prefix 的精确尾端；
2. 返回 row 属于该 prefix，且其 event bytes、routing fields、commit 与三轴
   revision 没有被换位；
3. 每个预期 locator 都有唯一 membership；若是 non-membership，结果是稳定的
   `locator_missing`，而不是“不知道”；
4. locator map、event sequence、event envelope 和 checkpoint 不能互相混搭；
5. 旧 handle 在 ledger 继续 append 后仍只代表原 prefix。

### 2.2 不声称的安全性

同一 SQLite 文件中的事件、hash、checkpoint 和 head 可以被协作重写的攻击者
同时改写。普通 SHA-256 只能发现非协同的损坏或实现错误，不能阻止这种攻击者。

因此 V1 的诚实合同是：持久 proof 数据是 **可重建缓存，不是 authority**。每个
进程首次打开 world 时必须由 genesis streaming verification 验证到当前 head，
并签发不可序列化、issuer-bound 的 handle；之后才允许复用 proof cache。若要让
cold start 也跳过完整验证，必须另做外部可信根（operator HMAC key、硬件签名、
远端透明日志或 WORM anchor）；不得把同库 hash 冒充这个能力。

## 3. 深模块接口

```text
VerifiedPrefixService
  pin(world_id, commit_tail_cursor) -> VerifiedPrefixHandle
  resolve_observations(handle, canonical_locators) -> HistoricalObservationProofBundle
```

调用方只能拿到 inert `HistoricalLedgerEvent`；只有更高一层、拥有 pinned proposal
authority 的 Fact resolver 才能据此签发自己的内部 authority capability。调用方
不得传 raw root、裸 checkpoint、任意 event JSON 或“已验证”布尔值。

`VerifiedPrefixHandle`：

- process-local、issuer-bound、不可 copy/pickle；
- 绑定 `world_id`、exact `ProjectionCursor`、checkpoint hash、event root、locator
  root 和 installed proof version；
- 不因为新的 append 自动前移；
- 只可被同一 `VerifiedPrefixService` 消费。

模块内部隐藏 MMR/树节点、SQL、缓存、批量 proof 和重建细节。业务模块不得访问
`world_v2_events` 或 proof tables。

## 4. 两个认证索引

### 4.1 Event prefix MMR

每个 ledger row 产生一个 domain-separated leaf：

```text
LedgerLeafV1 = canonical({
  contract: "world-v2-ledger-leaf.1",
  world_id, ledger_sequence, world_revision, deliberation_revision,
  commit_id, event_id, idempotency_key, event_envelope_hash
})
leaf_hash = sha256(LedgerLeafV1)
```

MMR 绑定 event 的完整位置而不是仅绑定 `event_json`。membership proof 验证 leaf 到
handle 的 prefix root；append consistency 只允许先前 root 成为新 prefix 的前缀。

### 4.2 Authenticated locator map

locator key 与值同样使用独立 identity domain：

```text
key = sha256(canonical({
  contract: "world-v2-observation-locator-key.1",
  world_id, event_type, idempotency_key
}))

value = canonical({
  observation_id, event_type, event_id, ledger_sequence, leaf_hash
})
```

使用固定深度的 sparse Merkle map（预计算 empty hashes），而非直接信任 SQL
`WHERE idempotency_key IN (...)`。它能提供 membership 和 non-membership proof，
从而检测 locator 被删、重定向或被另一个同 idempotency event 替换的情况。

同一 idempotency key 在 ledger 的一般合同中已唯一；observation locator 再要求
`event_type` 和 `observation_id` 同时精确匹配，不把这一唯一性偷偷扩展为 alias。

## 5. Checkpoint

每个 commit tail 都生成：

```text
PrefixCheckpointV1 {
  contract, proof_version, world_id, cursor, event_count,
  mmr_peaks, event_mmr_root, locator_root,
  commit_id, commit_record_hash, previous_checkpoint_hash,
  checkpoint_hash
}
```

`commit_record_hash` 包含 commit id、request hash、exact `CommitResult` 和有序 event
ids。checkpoint chain 因此同时绑定 commit atomicity 与 event ordering。mid-commit
cursor、cross-world cursor、future cursor 或 root/handle 混用都必须拒绝。

### 5.1 Persistence

SQLite 后续 slice 新增下列 **derived/rebuildable** tables：

- `world_v2_prefix_mmr_nodes`；
- `world_v2_observation_locator_nodes`；
- `world_v2_prefix_checkpoints`。

普通提交把 event、commit、head、两个索引节点和 checkpoint 放进同一个 SQLite
transaction；任一点失败都 rollback。migration 先 shadow-build/verify，最终以单一
CAS 安装 proof version 与 final checkpoint；中断的 shadow rows 永不授予 authority。

## 6. Resolve 流程与预算

1. 验证 handle ownership、world、commit-tail cursor、canonical unique locators
   （1..128）；
2. 对每个 locator 验证 sparse-map membership；非 membership 明确报 missing；
3. 根据已验证 value 的 `ledger_sequence` 获取 row，而不是信任 secondary index；
4. strict 重建 `WorldEvent`，重算 envelope hash、`LedgerLeafV1` 并验证 MMR proof；
5. 验证 locator value 与 event/ref/revision/commit 的全部字段；
6. 按 `(observation_id, event_type, event_id)` 返回 inert results。

批量使用 multiproof。128 条、proof node 数、证明字节数、JSON depth 和重建节点数
都设预算，预算只防 DoS，不承担正确性职责。anchor 建立后，读取复杂度目标为
`O(k log N)`；不得调用 `_replay_locked`。cold verification 和 migration 可以是
`O(N)` streaming，但内存必须有上界。

## 7. 实施顺序与验收

1. **A：pure core** — canonical leaf/key/value、append MMR、sparse map、checkpoint
   与 membership/non-membership/consistency verifier；golden vector 和 hostile input
   tests。
2. **B：adapter persistence** — memory/SQLite 在 commit 原子维护同一 proof state；
   SQLite rebuild/migration、crash/rollback 与 randomized parity。
3. **C：reader capability** — cold verification 签发 handle、exact reader 使用
   multiproof；Fact source resolver 只经此 seam 读取 observation。
4. **D：外部 anchor（独立选择）** — 仅在 operator 提供 key/存储条件后，开放快速
   cold start 与 anti-rollback 声称。

至少覆盖：event/routing/revision/commit 任一字段篡改，删除/交换 locator，伪造
non-membership，node/peak/checkpoint 修改，旧 handle append 稳定性，SQLite snapshot
并发、fault injection、migration resume，以及 memory/SQLite 随机序列 proof parity。

## 8. 非范围

- 不改变 emotion matrix、NPC、world clock 或 deliberation 的业务语义；
- 不让模型产生 proof、locator 或 authority；
- 不把 observation DTO、Fact intent、memory text 或 reader output 写进 proof tables；
- 不用全局缓存绕过 pinned cursor，也不为追求速度接受 source fallback。

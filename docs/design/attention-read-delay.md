# 注意力读延迟（Attention Read Delay）——设计评估与结论

- 日期：2026-07-20
- 状态：**仅设计文档，不实现**（本轮交付第 1/2 层：attention_view 纯投影推导 + later/silent 提示词激活；读延迟层经评估与自适应节奏保持、快应答存在复杂交互，按任务判据只交付设计）
- 相关：`src/companion_daemon/world_v2/attention_view.py`（第 1 层）、`docs/design/…`（本文件）、`qq_c2c_host.py`（未改动）

## 1. 目标

真人深夜收到消息，不是"收到了但选择晚点回"，而是**根本没看到**。当前 v2 的行为链是：

```
QQ 消息 → 提交 ingress store（due_at = 收到时刻 + 400–800ms 合并窗）
       → 自适应节奏保持（1.2–12s 安静间隙，8–18s 上限）
       → claim 批次 → typing 脉冲 → 完整回合（快反应 + 模型思考 + 回复）
```

无论几点，claim 都在秒级发生。第 2 层的提示词激活让**模型**可以选 later/silent（"看到了但先不回"），但"睡着了根本没看到"这个状态，模型层无法表达——回合本身已经跑完了，typing 脉冲已经发出去了。

读延迟层的设想：在 away/睡着状态下，把 **claim 本身**推迟到一个受控随机抽取的"补看时刻"（上限到早晨），让整个回合（typing、快反应、模型）都发生在她"真的拿起手机"的那一刻。

## 2. 为什么这一轮不写代码

### 2.1 同一个 claim 缝上已经叠了两层节奏权威

`qq_c2c_host.py` 刚被"快应答"代理重构过，claim 时机由两层协作决定：

1. **冻结合并矩阵**（`qq_ingress_policy.py`）：`due_at = received_at + window_ms`，
   版本化（`world-v2-qq-coalescing.2`）、可回放，是批次身份的一部分；
2. **自适应作息保持**（`_hold_for_sender_rhythm`）：进程本地、发送者节奏自适应
   （中位数间隙 ×1.3，语形偏置 0.6–1.7，上限 12s / 截止 8–18s），
   明确注释为"只延迟 claim，绝不改变批次身份、账本状态或回放"。

读延迟是**第三个**在同一 choke point 上操作的时间权威，且尺度完全不同（分钟到小时 vs 毫秒到秒）。三层的锚点各不相同（提交时刻的 due_at / 最近内容时刻的安静间隙 / 注意力唤醒时刻），组合顺序直接决定可观察行为。在快应答代理刚交付的热路径上叠第三层，回归风险大于收益。

### 2.2 inbound 协程会睡到 due_at

`inbound_fragment` 中：

```python
delay = max(0.0, (submitted.due_at - self._ingress_now()).total_seconds())
if delay:
    await self._ingress_sleep(delay)
```

若在 submit 时把 due_at 设为"早上 7 点"，OneBot 的 HTTP 处理协程会被挂起数小时；改成"提交即返回 deferred、交给 scheduler claim"则改变了 ingress 合同的返回语义（快应答路径依赖当前语义）。两种做法都不是小改动。

### 2.3 回合起点的副作用假设"刚收到"

- `_pulse_typing()` 在 claim 时向对端发 "正在输入…"——这是当前**唯一**
  会泄露"她看到了"的外显信号。凌晨 3 点消息、模型选了 silent，typing
  脉冲也已经发出去了（这恰恰是读延迟想修的洞，见 §4）；
- 快反应 lane 在回合内跑：延迟 6 小时后再对凌晨的消息贴表情，语义上需要
  重新把关（表情的社交含义随时间衰变）；
- `scheduler_once` 每个周期都会 `drain_ingress_once` 抢 claim——读延迟的
  "补看时刻"必须写进 store 的 due 语义，否则 scheduler 会立即绕过它。

### 2.4 受控随机没有账本前的落点

补看时刻应当受控随机（真人不会精确 7:00 醒）且回放稳定。但 ingress store 在账本**上游**，`RandomAuthority` 的 draw 需要账本。可行方案（付确定性代价：从 payload_hash 派生伪随机）或（付迁移代价：ingress 表新增列并纳入恢复读取路径）都涉及被两个并行代理共享的存储/恢复面。

### 2.5 先观察第 2 层的自然发生率

本轮已让模型在注意力 advisory 驱动下选择 later（delay 可达数小时）/silent。若读延迟层同时上线，深夜消息会**双重延迟**：claim 推迟到早上 7 点，模型再选 later 20 分钟——行为对但归因混乱，审计时无法区分两层的贡献。正确顺序：先让第 2 层跑一段生产，量出"深夜消息的 later/silent 率与 delay 分布"，再决定读延迟层要补多少。

## 3. 建议的设计（供后续实现）

分两步走，第一步足够小、可独立交付：

### 第一步：注意力门控 typing 脉冲（小改动，先做）

`_process_ingress_batch_locked` 在发 typing 脉冲前，向 host 要一个只读注意力状态（`phone_attention_reading` 已存在，纯投影）：

- `away` / `do_not_disturb` → **跳过 typing 脉冲**（她没看手机，不该出现"正在输入"）；
- 其余状态照旧。

改动面：`qq_c2c_host.py` 一处 + `WorldV2PlatformHost` 暴露一个只读投影读。不动 claim 时机、不动合并矩阵、不动节奏保持。修掉当前最大的"在场感泄露"（深夜 typing 脉冲），而模型层的 later/silent 负责其余行为。

### 第二步：真正的读延迟（大改动，观察后再做）

1. **落点选 store 而非 host**：`SQLiteQQIngressStore.submit` 增加可选的
   `attention_hold_until`（新列，随迁移），`claim_due` 取
   `max(due_at, attention_hold_until)` 作为生效 due；合并矩阵本身不改版
   （hold 不参与批次身份，只推迟 claim——与节奏保持同一契约）；
2. **inbound 协程不睡长觉**：`inbound_fragment` 发现 hold 超过某阈值
   （如 30s）直接返回 `deferred`，交给 scheduler 的 `drain_ingress_once`
   在 hold 到点后 claim（scheduler 周期已存在）；
3. **补看时刻的抽取**：`hold_until = 唤醒锚点 + U(0, 45min)`，唤醒锚点 =
   sleep plan 的 scheduled_window 结束或 7:00（取早者）；随机源用
   `sha256(source_event_id + policy_version)` 派生，天然回放稳定、无需
   账本 draw；策略版本化（`attention-read-delay.1`）；
4. **hold 期间新消息**：同批合并（現有 claim 聚合已支持），多条深夜消息
   自然变成"早上醒来一起看到"，一个回合一次回复；
5. **快反应/typing**：hold 到点 claim 后正常跑（此时她"真的在看手机"，
   typing 脉冲语义恢复正确）；快反应的语义门已由本地模型把关内容，
   时移后仍成立；
6. **上限与逃逸**：hold 硬上限到次日 09:00；用户显式连发（≥3 条）或
   高唤醒词（"急""在吗？？"）不逃逸——真人睡着就是收不到，这是设计
   的全部意义；运营逃逸走已有的 operator observation 通道。

### 与第 2 层的分工（实现后）

| 层 | 表达的人类事实 | 机制 |
|---|---|---|
| 读延迟（第 3 层） | "睡着了，根本没看到" | claim 推迟到补看时刻，整个回合时移 |
| 模型 later（第 2 层） | "看到了/瞥了一眼，决定忙完再回" | 回合正常跑，模型选 later + 延迟投递 |
| 模型 silent（第 2 层） | "看到了，此刻不想回/不必回" | 回合正常跑，无可见效果 |

读延迟上线后，深夜场景的提示词校准应同步微调：claim 发生在早上，advisory 已是"早晨刚醒"，模型自然选 now——两层不再叠加。

## 4. 结论

- 本轮不实现读延迟：与合并矩阵、自适应节奏保持、快应答、typing 脉冲、
  scheduler claim、账本前随机性六处存在实质耦合，任何"小而增量"的
  版本都会在别的代理刚改过的热路径上引入语义分叉；
- 交付路径：**先跑第 2 层**（本轮已交付）观察 later/silent 自然发生率
  → **第一步 typing 门控**（小改动，独立可交付）→ 量化剩余缺口后再实现
  **第二步 store 级读延迟**（按 §3 设计，含策略版本与迁移）。

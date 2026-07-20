# World v2 Phase 8 退出审计（2026-07-16）

## 结论

World v2 的 **内部可实现范围已满足 Phase 0–8 机械退出条件**：默认运行路径、领域权威、
Context/Advisory/Deliberation/Acceptance、情绪关系记忆与 NPC、Expression/Action/预算/回执、
媒体 preview continuation、感知结果、主动行为/Pulse、SQLite 恢复、确定性 replay、性能/成本
记账和静态隔离均有生产接线与可执行证据。70 个冻结 fixture 已无 `module_only`；唯一非
`production` 项是按定义执行静态 CI 的 `W2-ARCH-001`。

这不是“人味已经被真人证明”的声明。三项 fixture 仍是明确的 hybrid 外部 gate：真实
provider warm/cold P95、供应商账单/token 对账、目标部署 20 个完整 hot transport 样本。
正式 `human-likeness-eval-v1` 还需要真实 bare/archive/v2 输出和两名独立盲评者。内部代码、
synthetic fixture 或离线 P95 均不能替代这些外部工件。

## 本轮权威证据

| Gate | 结果 | 可复跑命令/工件 |
| --- | --- | --- |
| World v2 全量测试 | **1837 passed** | `.venv/bin/python -m pytest -q tests/world_v2` |
| 70 fixture 实际节点 | **103 unique nodes / 113 passed** | `.venv/bin/python scripts/verify_world_v2_fixture_nodes.py` |
| 冻结场景与 replay | **120/120 passed** | `.venv/bin/python scripts/verify_world_v2_scenarios.py --workdir /tmp/world-v2-scenario-final --output /tmp/world-v2-scenario-final.json` |
| 场景基线 | **`.5` 匹配** | manifest `f214504a5416dc2b31ba83907eeb95723ff9e674db3df84ebd3d477a509f0cf4` |
| 机制目录 | **通过** | `.venv/bin/python scripts/verify_mechanism_catalog.py`；schema 2、20 mechanisms |
| 平台反向依赖 | **通过** | `.venv/bin/python scripts/verify_world_v2_platform_architecture.py` |
| 离线热聊 gate | **通过** | 20 回合总计约 8.43s，hot P95 约 0.41s；hot historical replay = 0 |
| 代码质量 | **通过** | 变更文件 Ruff 与 `git diff --check`；各纵切定向回归均通过 |

仓库级诊断（排除无法收集的未跟踪 `tests/test_star_office.py`）运行到末尾为 3459 passed / 6
failed；其中一个是旧入口测试仍强制所有 CLI 调 legacy `build_companion_engine`，已改为同时接受
安装后的 v2 factory 并以 `tests/test_runtime.py` 13 passed 复验。其余 5 项位于并行的图片机/
TileRoom/legacy safe-fallback 工作树（3 个衣装/自拍 prompt fixture、1 个 JS 浮点 strict equality、
1 个 archive fallback 预期），不属于本 World v2 authority diff，未越权改写。Star Office 本身还
缺测试要求的 `star_office_agent` 导出。它们不能被描述为 World v2 gate 失败，也不能被隐藏成
仓库全绿。

`.5` 与保存的 `.4` manifest 已逐字段比较：120 条输出、terminal Action state、trigger kind、
room projection、模型调用数和验证结果不变；120 条 replay hash 因逻辑时钟/新 reducer authority
更新，只有 `provider_timeout.01/.02` 的 event types 新增真实 `ExpressionPlanTerminated`。

## 关闭的高权重缺口

### 1. 逻辑时钟与连续消息

- `WorldStarted.logical_time` 是初始世界时钟权威；只有 `ClockAdvanced` 能推进它。
- Observation 的 `created_at/received_at` 保留平台墙钟，event `logical_time` 固定当前世界时钟，
  因而连续不同时间消息不会破坏 Situation，也不会偷偷推进虚拟世界。
- 旧 fixture 中“启动后倒退初始化 Clock”与“Observation 推进世界时钟”的矛盾语义已迁移；
  reducer 没有为兼容测试而放宽。

### 2. 热路径性能与证据污染

原 Context source lookup 每查一个来源都会从 genesis replay 到 owning commit，导致 replay
次数随对话增长、snapshot 末段约 2.7s。现在 lookup 使用启动时完整验证并随事务更新的
verified head prefix；外部 SQLite 连接变化由 `PRAGMA data_version` 触发冷历史复验，tamper
fail closed。20 轮离线热聊从约 35–68s 降至约 8.43s，P95 从约 3.2–4.8s 降至约 0.41s。
Performance evidence reader 直接读取已认证 head，不再为“测量”自行制造 replay。

### 3. ExpressionPlan 真实终态

- `completed` 仍只代表全部 required beat 收到真实 delivered receipt。
- failed/unknown/cancelled/expired 产生 typed `ExpressionPlanTerminated`，不再永久卡在
  `authorized`。
- 多 beat 中未 dispatch sibling 在同一 UoW 显式执行
  `ActionCancelled → ExpressionBeatTerminated → BudgetReleased → ExpressionPlanTerminated`；
  reducer 不做隐藏跨域 cascade，ActionPump 不能继续发送。
- 已 `dispatch_started/provider_accepted` 的 sibling 保留独立 receipt/reconciliation；后到回执
  只结算该 beat，不重开或假完成计划。

### 4. 主动行为、Pulse 与延迟回复

- 世界 settlement 可触发模型在 now/later/silent 中自主选择，经 Proposal、proactive budget、
  Action、receipt 到终态；预算耗尽也有 durable terminal outcome。
- 普通聊天选择 later 会原子创建 generic Thread、Commitment 和 followup Action；SQLite 重启
  后不重调模型，到期由 ActionPump 投递，receipt 自动 fulfilled Commitment。
- Thread 可继续作为话题连续性，但已有 Commitment+Action 时 proactive runtime 明确跳过，
  不会把开放 Thread 当成重复发送许可。

### 5. 情绪、关系、记忆、NPC 与感知

- 用户输入和已 settlement 的 NPC/世界事件都能形成 source-bound appraisal/affect/relationship
  触发，并在下一轮 Context 被主模型消费；模型可采纳、拒绝或选择不安慰，矩阵不强制固定话术。
- Fact withdrawal 会开启 exact-source memory review；retain/forget/revise 都有 Proposal、Acceptance、
  CAS、SQLite 重启与并发证据，不由 Fact reducer 隐式删除记忆。
- 纯附件只把 opaque ref/type交给模型；部署侧 immutable hash、授权、预算、provider result、
  result trigger 和下一轮 Context 构成完整闭环，provider observation 不冒充世界事实。

### 6. 媒体、平台和归档隔离

- 媒体生产 conductor 已消费 `plan_to_render` 与 `render_to_inspect` continuation，并覆盖 Proposal、
  Acceptance、Action、预算、provider result、repair、preview、重启/并发 join；缺真实 deployment
  provider/grant 时明确 unavailable，不回退旧图片机。
- 默认 ASGI/HTTP 与 `/dashboard` 使用 v2；兼容 QQ C2C 走同一 world composition。NapCat 支持
  source-bound reaction、标准 sticker 与 typing；不支持的 profile fail closed。
- archive app 显式独立；默认 route graph 和平台 guard 禁止回流旧 Engine/reducer 权威。

## 仍需外部条件的验证

1. **正式人味盲评**：bare/archive/v2 各 120 场景 × 3 seed 的真实模型输出、两名独立盲评者、
   固定 rubric/statistics 与签名原始 trace。Synthetic `verify-fixture` 必须继续返回
   `synthetic_fixture_not_external_evidence`。
2. **真实延迟 SLO**：目标部署至少 20 个完整 hot transport 样本及 cold 样本；离线 0.41s
   不能冒充网络 P95。
3. **真实成本对账**：provider usage、账单和本地 ModelResult/token ledger 的签名 reconciliation。
4. **真实媒体/感知部署**：durable provider、stable lookup、operator approval/grant 和目标 transport
   的部署演练；这些是外部配置/服务证据，不是放宽内部 authority 的理由。

## 退出后的纪律

- 新机制只能进入 v2 authority；旧 Engine 只修 archive P0，不再承接行为扩展。
- 每次语义变化升级 reducer bundle/机制 baseline，并重跑 70 fixture、120 scenario、migration、
  tamper、性能与静态依赖 gate。
- 在外部工件齐备前，产品表述应为“内部机制与生产接线完成，真人/真实供应商验证待完成”，
  不使用“完美情绪”或“不可察觉为 AI”的无证据结论。

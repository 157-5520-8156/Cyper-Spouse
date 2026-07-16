# World v2 运行时状态与验收边界

> 更新时间：2026-07-16
> 口径：本文件记录**当前代码和自动化证据**，不是目标架构的承诺书。冻结设计见
> [`world-v2-refactor-plan.md`](world-v2-refactor-plan.md)。旧 WorldKernel 设计见
> [`world-kernel.md`](world-kernel.md)，仅作迁移和历史诊断参考。

## 结论

World v2 已经拥有可运行、可回放的独立账本纵切：`WorldRuntime` 接收 observation、
追加受版本约束的事件、重建 projection、编译 Context Capsule，并让已授权 Action 经过
receipt/unknown/recovery 生命周期。HTTP 已接入该宿主；兼容配置下的 QQ 私聊纯文本也会走
该路径。

这**不等于**整个产品已经完全切换到 v2，更不等于已证明“人味优于裸聊”。Web Dashboard
主展示、全部 QQ/NapCat/OneBot 形态、真实可恢复图片 provider 的部署，以及真人/真实模型
盲评仍是未关闭的工作。旧 `WorldKernel`、`CompanionEngine` 和旧行为表也仍存在于兼容与
归档区域；它们不能被描述为 v2 的当前写权威。

## 当前权威边界

```text
HTTP / 兼容 QQ C2C 文本 / 离线 harness
                  │
                  ▼
  WorldV2PlatformHost / HttpV2CaptureHost / QQC2CHost
                  │
                  ▼
             WorldTurnRuntime
      (WorldRuntime ingest / advance / settle)
                  │
                  ▼
  WorldLedger → deterministic ReducerState → viewer projections
                  │                  │
                  │                  ├─ Situation / Context Capsule / Advisory
                  │                  └─ Action, Budget, Receipt, recovery
                  ▼
       transport / media provider / read-only projection adapter
```

- `WorldRuntime` 和 v2 Ledger 是 **已迁移入口** 中世界事实、Action 和 receipt 的唯一写
  路径；adapter 不能直接写 reducer 或旧行为表。
- 旧 `WorldKernel` 不是 v2 的替代账本，也不是 v2 机制的实现依据。它仍服务于尚未迁移的
  旧入口、历史数据和兼容代码，因而“全产品唯一写模型”目前尚不能宣称成立。
- `event_media` 是图片机的 public seam。图片机只能返回 provider 结果；机会、批准、预算、
  投递和 `MediaDeliveryShared` 均由 v2 账本决定。
- Dashboard 的 v2 room endpoint 只读、受 operator gate 保护，并且在 v2 host 冷启动时
  fail closed。Godot 已消费相同的公共 DTO；Web Dashboard 主页面仍是遗留读路径。

## 阶段状态

| Phase | 现状 | 已有证据 | 未关闭的边界 |
| --- | --- | --- | --- |
| 0 冻结与隔离 | 部分完成 | v2 package、proposal grammar、reverse architecture guard | 旧运行时仍存在；默认部署切换尚未完成 |
| 1 Schema / interface | 已完成（代码层） | typed schemas、event catalog、runtime contract tests | 不能以 schema 存在推断每个 adapter 已接入 |
| 2 Ledger / projection | 已完成核心纵切 | SQLite replay、revision/CAS、source-bound Fact/Experience/Memory/Core、Goal/Location/Resource/Attention fixtures | 其他业务域仍按逐条 authority vertical 继续收口 |
| 3 Situation / matrix | 部分完成 | deterministic Capsule、budget slices、advisory、matrix catalog tests | 仍有 unavailable/private slices，需继续验证所有生产决策均消费同一 revision |
| 4 Deliberation / acceptance | 部分完成 | source-bound proposal/manifest/atomic recorder、appraisal/affect/outcome/media thread lanes | 不应把未开放的 proposal family 或 fallback 当作已完成行为能力 |
| 5 Action / recovery | 部分完成 | Action lifecycle、lease、unknown reconciliation、expression/deferred-reply tests | reaction/typing/sticker 仍是 adapter-only；不是可用生产 grammar |
| 6 Media preview | 部分完成 | freeze → plan → render/inspect → preview → approval/delivery/recovery fixtures | 真正部署的 durable provider、operator approval 和 production transport 覆盖仍需验证 |
| 7 平台与展示 | 部分完成 | HTTP v2、兼容 QQ C2C text v2、v2 dashboard read DTO、Godot room consumer | NapCat/OneBot 其他形态、Web Dashboard 默认读路径未迁移 |
| 8 Evaluator / 清理 | 部分完成 | frozen scenario corpus、mechanism baseline、test-economy、blind artifact pipeline | synthetic/offline 证据不能替代真实模型、真人盲评或线上 SLO |

“已完成（代码层）”仅表示对应 v2 contract 已有回放/攻击/合同测试；它不表示所有平台已经
默认启用，也不表示已经获得外部体验数据。

## 已闭合的 v2 机械事实

下列结论有相应的 `tests/world_v2`、冻结 scenario 或静态 guard 证据，且不依赖旧
WorldKernel：

- observation、clock、external result、proposal、acceptance、budget reservation、Action 和
  receipt 都可以在 v2 ledger 中留下可重建的事件链；重复 ingress 和冲突 payload 会被拒绝。
- 事实、经历、记忆候选、人格核心及 v16 的 Goal/Location/Resource/Attention 都有 source
  binding、privacy、migration/replay 或 authority fixtures。它们不是“模型说过一次”就自动
  成为世界事实。
- appraisal、private impression、Affect episode、relationship、thread、commitment、world
  occurrence/NPC 后果已经有独立 v2 reducer/trigger 路径。其存在不代表所有情绪判断都必然
  被模型正确识别；只代表被接受的结构化结果可追踪、可消费、可回放。
- 表达 Action 与 receipt 结算、用户插话后的剩余 beat reconsideration、`reply_later` 的
  terminal lifecycle 均已有 v2 合同。未投递 beat 不得被当作已说过。
- 媒体默认 preview；只有被批准且收到 delivery receipt 才可形成 `MediaDeliveryShared` 和
  后续互动 trigger。inspection repair 被限制为同一 frozen plan 的一次修复。
- 固定 corpus 的 deterministic replay、测试经济 trace 与机制 baseline 已在 CI 侧可运行。

具体 mechanism-to-evidence 映射在
[`configs/mechanism_closure.yaml`](../configs/mechanism_closure.yaml)，该文件不是产品能力
清单：`partial`、`adapter-only`、`not-wired` 与“external evidence pending”都是有意保留的
负面状态，不能被汇总成“已闭环”。

## 尚不能宣称的结论

以下事项缺少足够证据，必须保持未完成：

1. **默认运行时已全量 v2。** HTTP 与一类 QQ C2C 入口有迁移证据，但不是全平台切换；未迁移
   内容形态必须显式归档或拒绝，而不是双写。
2. **所有世界模块已被物尽其用。** 有 authority/reducer 不等于每种 proposal family 都在真实
   ingress 后被 deliberation 选择；应以 source → decision consumer → Action → settlement →
   next-turn consumption 的 trace 逐项验收。
3. **图片机已可安全自动投递。** 本地 planning/preview/repair 合同可用；真实 provider、审批
   样本、失效策略和部署 receipt 仍需分别验收。
4. **情绪已经达到“难以察觉是 AI”。** 目前证明的是可追踪的 affect/relationship/state
   机制和离线场景，不是长期真人校准、讽刺/权力差异理解或语言自然度的外部证明。
5. **热启动、冷启动和首 Action P95 达标。** test-economy 和 trace schema 已存在；真实部署的
   queue/provider 数据、SLO 分位数和回归基线尚未采集。
6. **所有展示端只读消费 v2 projection。** Godot 已迁到 v2 room DTO；Web Dashboard 默认读
   路径仍需迁移并做 privacy/redaction 回归。

## 验收与后续工作顺序

下一轮工作应以可观察的闭环而非继续堆模型规则为准：

1. 对每个仍标为 `partial` 的 production lane，补齐可执行的 source、consumer、Action、
   receipt/recovery 和 next-turn trace，或者把它明确保留为 archive/adapter-only。
2. 迁移 Web Dashboard 与剩余平台 adapter 到 `project()` / `WorldRuntime`；迁移期间不允许
   同一 observation 同时写旧账本和 v2 账本。
3. 在有 durable provider 与 operator approval 后，做真实媒体 preview 样本和恢复演练；此前
   不默认开启自动 delivery。
4. 收集与版本绑定的真实模型评审、真人长期会话和线上 latency/cost trace。只有这些证据满足
   `world-v2-refactor-plan.md` 的统计门槛，才可声称 v2 不低于裸聊或达到目标 SLO。

建议的本地验证入口：

```bash
uv run pytest tests/world_v2 -q
uv run python scripts/verify_world_v2_scenarios.py
uv run python scripts/verify_mechanism_catalog.py
uv run ruff check src/companion_daemon/world_v2 tests/world_v2
```

完整仓库测试若被未迁移或用户工作区中的独立模块阻断，应单独记录其错误；不得把“v2
子集通过”写成“全仓库验收通过”。

## 旧文档处置

本文替代早期“世界模式已是全产品唯一 WorldKernel 写模型”的状态性表述。早期
`world-kernel.md`、旧行为盘点与遗留设计文件仍有价值：它们说明需要隔离的旧写路径和不可
回迁的语义；但它们不是 World v2 当前运行时权威或 Phase 1–8 的完成证明。

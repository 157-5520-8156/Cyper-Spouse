# World v2 运行时状态与验收边界

> 更新时间：2026-07-16
> 口径：本文件记录**当前代码和自动化证据**，不是目标架构的承诺书。冻结设计见
> [`world-v2-refactor-plan.md`](world-v2-refactor-plan.md)。旧 WorldKernel 设计见
> [`world-kernel.md`](world-kernel.md)，仅作迁移和历史诊断参考。

## 结论

World v2 的内部重构与默认入口已经闭环：`WorldRuntime` 接收 observation、追加版本化事件、
重建 projection、编译 Context/Advisory，让 Proposal/Acceptance、情绪关系记忆/NPC、
ExpressionPlan、Action/预算/回执/恢复、媒体 preview continuation 与感知结果都沿同一账本
运行。默认 ASGI/HTTP 与 Web Dashboard 使用 v2；兼容 QQ C2C 也走该宿主。旧
`WorldKernel`、`CompanionEngine` 只保留在显式 archive/兼容 app，反向依赖 guard 禁止默认
路由回流旧写权威。

这仍**不等于**已由真人证明“人味优于裸聊”。群聊/多用户、真实媒体与感知 provider 部署、
真实网络 P95/账单，以及 bare/archive/v2 双盲评审需要外部环境或参与者；内部自动化不会把
这些外部 gate 伪装成已通过。

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
                  │                  ├─ Action, Budget, Receipt, recovery
                  │                  ├─ source-bound event ecology → media opportunity
                  │                  └─ injected perception → opaque result trigger
                  ▼
       transport / media provider / read-only projection adapter
```

- `WorldRuntime` 和 v2 Ledger 是 **已迁移入口** 中世界事实、Action 和 receipt 的唯一写
  路径；adapter 不能直接写 reducer 或旧行为表。
- 旧 `WorldKernel` 不是 v2 的替代账本，也不是 v2 机制的实现依据。它只服务显式 archive、
  历史数据和兼容代码；默认 route graph 已隔离其写路径。
- `event_media` 是图片机的 public seam。`EventEcologyMediaCandidateRuntime` 已能从已提交且
  可分享的活动、结果、经历和有限可见事实冻结候选机会，并带有持久化的频率/cooldown 约束；它
  不生成图片 prompt、不替图片机规划/渲染/投递，也不从未提交计划或私密内心推断画面。图片机
  只能返回 provider 结果；机会、批准、预算、投递和 `MediaDeliveryShared` 均由 v2 账本决定。
- 感知（vision/transcription）已有独立、source-bound 的注入式 vertical：输入类型、同意、
  隐私、预算和最终 pump 授权都被绑定。完整依赖显式注入时，附件 Observation 会进入独立
  proposal grammar，模型只选择 opaque attachment ref；Acceptance 再绑定部署侧 immutable
  content hash，provider 结果经过后续 trigger 后成为下一轮 source-bound Context。HTTP/QQ
  已允许纯附件 ingress，但默认部署仍不注入 provider/授权，且当前 result deliberation 只
  支持 no-visible-action。
- Dashboard 的 `/dashboard` 主页面与 `/world-v2/dashboard` 公共 DTO 都读取 v2 viewer
  projection，受 session/capability gate、ETag/no-store 与 public whitelist 约束；读取不会
  bootstrap 世界或回退 legacy projection。

## 阶段状态

| Phase | 现状 | 已有证据 | 未关闭的边界 |
| --- | --- | --- | --- |
| 0 冻结与隔离 | 已完成（内部） | v2 package、默认 route composition、reverse architecture guard、显式 archive app | archive 代码仍为历史兼容，不是默认权威 |
| 1 Schema / interface | 已完成（代码层） | typed schemas、event catalog、runtime contract tests | 不能以 schema 存在推断每个 adapter 已接入 |
| 2 Ledger / projection | 已完成（内部） | SQLite replay、revision/CAS、`.32` typed authorities、migration/tamper/zero-cascade fixtures | 真实长期数据质量属于外部验证 |
| 3 Situation / matrix | 已完成（内部） | deterministic Capsule、唯一 source matrix、budget slices、Advisory、MatrixCatalog、生产消费测试 | 无来源 slice 继续显式 unavailable，这是正确状态 |
| 4 Deliberation / acceptance | 已完成（内部） | source-bound Proposal/ModelResult/Acceptance/atomic recorder 与 now/later/silent、情绪关系记忆/NPC lanes | 模型可拒绝建议；机械授权不保证每次语义判断正确 |
| 5 Action / recovery | 已完成（内部） | Action lifecycle、lease/unknown/reconciliation、多 beat 终态、NapCat modalities、deferred/proactive/pulse、perception result | provider accepted 不冒充 delivered；能力缺失按 profile fail closed |
| 6 Media preview | 已完成（可注入闭环） | freeze→plan→render→inspect→preview→approval/delivery/recovery 与 production continuation | 真实 provider/grant/operator approval 是部署工件 |
| 7 平台与展示 | 已完成目标范围 | 默认 HTTP/ASGI、QQ C2C、NapCat modalities、v2 Dashboard/Godot、archive 隔离 | 用户已明确本轮不以 QQ 群聊/多用户为世界机验收条件 |
| 8 Evaluator / 清理 | 内部 gate 完成；外部 hybrid 待证据 | 70 fixture 实际节点、1837 tests、120 scenario `.5`、test-economy、blind artifact pipeline、离线 hot P95≈0.41s | synthetic/offline 证据不能替代真实模型、真人盲评、真实网络 SLO或账单 |

“已完成（代码层）”仅表示对应 v2 contract 已有回放/攻击/合同测试；它不表示所有平台已经
默认启用，也不表示已经获得外部体验数据。

## 已闭合的 v2 机械事实

下列结论有相应的 `tests/world_v2`、冻结 scenario 或静态 guard 证据，且不依赖旧
WorldKernel：

- observation、clock、external result、proposal、acceptance、budget reservation、Action 和
  receipt 都可以在 v2 ledger 中留下可重建的事件链；重复 ingress 和冲突 payload 会被拒绝。
- 事实、经历、记忆候选、人格核心及 v16 的 Goal/Location/Resource/Attention 都有 source
  binding、privacy、migration/replay 或 authority fixtures。它们不是“模型说过一次”就自动
  成为世界事实。HTTP 和兼容 QQ C2C 的默认 production composition 会将 Fact/Memory
  放在后台队列：不延长当前回复，但已接受的 user Fact 可以在下一轮进入检索 Context。
- appraisal、private impression、Affect episode、relationship、thread、commitment、world
  occurrence/NPC 后果已经有独立 v2 reducer/trigger 路径。HTTP 与兼容 QQ C2C 的 composition
  会把**已观察且 sidecar-backed**的 outcome matrix 交给专用 selector：模型只能选择已进入
  Context 的 opaque candidate ref，settlement 的 occurrence/revision/result/evidence 由账本权威
  导出。它不代表常规对话已经能自行创建丰富的 occurrence candidate matrix，也不代表所有情绪
  判断都必然被模型正确识别；只代表被接受的结构化结果可追踪、可消费、可回放。
- 表达 Action 与 receipt 结算、用户插话后的剩余 beat reconsideration、`reply_later` 的
  terminal lifecycle 均已有 v2 合同。未投递 beat 不得被当作已说过。
- 媒体默认 preview；只有被批准且收到 delivery receipt 才可形成 `MediaDeliveryShared` 和
  后续互动 trigger。inspection repair 被限制为同一 frozen plan 的一次修复。
- 候选到 planning 的 scheduler 已能在 Proposal 后或 Acceptance 后重启恢复：head Proposal
  原样复用，不重复调用 selector；已有 planning Action 优先于新候选。合法的非 head stale
  Proposal 只保留审计并执行 fresh-only re-deliberation，损坏或缺失的 Proposal authority
  显式阻断。模型主动拒绝或返回不合 bounded grammar 的终态会写入
  `MediaSelectionAttemptRecorded`；同一逻辑时刻与同一候选 revision 集重启后不重复调用模型，
  只有时钟或候选版本变化才允许重新考虑。HTTP/QQ 的统一 scheduler 会让普通 Action、planning
  和 provider result 共用 action budget，让候选选择与普通后台共用 background budget；零预算
  不会进入媒体 conductor。HTTP/QQ 只有收到完整
  `MediaPreviewDeployment` 才安装该 conductor；配置对象不生成 enforcement grant。
- 事件生态可把已提交的、可分享的生活事实映射为有限的图片候选类别，并冻结同一份证据
  snapshot；其中 object/food 只能经 `VisualFactRecorded` 的 source-bound immutable sidecar
  提供描述，`value_ref/hash`、LLM 猜测和缺失 sidecar 都不能生成视觉细节。私密来源、未提交
  事项和重复类别会被拒绝或压制。它证明的是候选机会的来源与重放安全，不证明默认 host 已经
  有足够丰富的受信视觉事实 writer 或真实生活覆盖。
- 注入式感知 Action 可以在最终授权检查后结算 vision/transcription 的 immutable result
  descriptor，并只创建一次后续 deliberation trigger；trigger 终态后 exact result/receipt
  会作为 `provider_observation_not_world_fact` 进入后续 Context。缺 provider、授权或 hash
  不匹配均 fail closed，不能把 attachment ref 冒充已看见的内容。这不代表默认对话已启用
  provider，也不保证模型一定会把感知结果自然地说出来。
- 公共 Dashboard 后端 DTO 仅从固定 `dashboard_public` viewer projection 编译，未知/私密
  路由降级或省略，HTTP 读取不 bootstrap 或回退 legacy；当前 `/dashboard` 浏览器主页面已
  使用该 v2 DTO。
- 固定 corpus 的 deterministic replay、测试经济 trace 与机制 baseline 已在 CI 侧可运行。

具体 mechanism-to-evidence 映射在
[`configs/mechanism_closure.yaml`](../configs/mechanism_closure.yaml)，该文件不是产品能力
清单：其中仍标记的“external evidence pending”表示真实 provider/人类评审工件缺失，不能
被内部机械通过汇总成外部体验结论。

## 尚不能宣称的结论

以下事项缺少足够证据，必须保持未完成：

1. **默认运行时已全量 v2。** HTTP 与一类 QQ C2C 入口有迁移证据，但不是全平台切换；未迁移
   内容形态必须显式归档或拒绝，而不是双写。
2. **所有世界模块已被物尽其用。** 有 authority/reducer 不等于每种 proposal family 都在真实
   ingress 后被 deliberation 选择；应以 source → decision consumer → Action → settlement →
   next-turn consumption 的 trace 逐项验收。
3. **图片机已可安全自动投递。** 本地 planning/preview/repair 合同可用；真实 provider、审批
   样本、失效策略和部署 receipt 仍需分别验收。
4. **图片机会已覆盖丰富的生活事件。** 当前 event ecology 只消费已有的 activity/outcome/
   experience/有限可见 fact authority；object/food 已有严格的 `VisualFactRecorded` sidecar，
   默认 host 仍没有其受信 writer。2026-07-20 起 life ecology 内新增了受信的
   `LifeVisualEvidenceAuthor`：它只把 reviewed-life.12 目录中带 `visual_evidence` 注册的
   **已结算** occurrence（受记录抽签、情绪阈值与每日/间隔节奏约束）写成 source-bound
   `ImageEvidenceDeclared` / `RecipientScopedImageEvidenceDeclared`，供 ecology 与
   character-media binder 开出候选。候选类别和画面多样性仍需随着注册面扩大与真实运行验证；
   selection/planning 仍要求显式 `MediaPreviewDeployment`，默认部署不会因此自动生成图片。
5. **情绪已经达到“难以察觉是 AI”。** 目前证明的是可追踪的 affect/relationship/state
   机制和离线场景，不是长期真人校准、讽刺/权力差异理解或语言自然度的外部证明。
6. **热启动、冷启动和首 Action P95 达标。** test-economy 和 trace schema 已存在；真实部署的
   queue/provider 数据、SLO 分位数和回归基线尚未采集。
7. **所有展示端只读消费 v2 projection。** Godot 已迁到 v2 room DTO；Web Dashboard 默认读
   路径仍需迁移并做 privacy/redaction 回归。

## 验收与后续工作顺序

下一轮工作应以可观察的闭环而非继续堆模型规则为准：

1. 先扩展已提交生活事件的可视内容 authority（尤其 object/food 的已验证 sidecar），并用
   candidate-category/recency/replay fixtures 验证 event ecology 的覆盖，不让图片机或 LLM 补造
   世界细节。
2. 对每个仍标为 `partial` 的 production lane，补齐可执行的 source、consumer、Action、
   receipt/recovery 和 next-turn trace，或者把它明确保留为 archive/adapter-only。感知 vertical
   只有在默认 grammar、provider composition 和可见结果决策均有证据后才能升格。
3. 迁移 Web Dashboard 与剩余平台 adapter 到 `project()` / `WorldRuntime`；迁移期间不允许
   同一 observation 同时写旧账本和 v2 账本。
4. 在有 durable provider 与 operator approval 后，做真实媒体 preview 样本和恢复演练；此前
   不默认开启自动 delivery。
5. 收集与版本绑定的真实模型评审、真人长期会话和线上 latency/cost trace。只有这些证据满足
   `world-v2-refactor-plan.md` 的统计门槛，才可声称 v2 不低于裸聊或达到目标 SLO。

建议的本地验证入口：

```bash
uv run pytest tests/world_v2 -q
uv run python scripts/verify_world_v2_scenarios.py \
  --workdir /tmp/world-v2-scenario-suite \
  --output /tmp/world-v2-scenario-suite.json
uv run python scripts/verify_mechanism_catalog.py
uv run ruff check src/companion_daemon/world_v2 tests/world_v2
```

完整仓库测试若被未迁移或用户工作区中的独立模块阻断，应单独记录其错误；不得把“v2
子集通过”写成“全仓库验收通过”。

## 旧文档处置

本文替代早期“世界模式已是全产品唯一 WorldKernel 写模型”的状态性表述。早期
`world-kernel.md`、旧行为盘点与遗留设计文件仍有价值：它们说明需要隔离的旧写路径和不可
回迁的语义；但它们不是 World v2 当前运行时权威或 Phase 1–8 的完成证明。

# World v2 Phase 8 退出审计（2026-07-16）

## 结论

本次不能宣布“World v2 全部完成”。离线、可复放、无外部模型依赖的
机制闭环已通过；正式人味评估、真实端到端性能、所有平台默认迁移和旧
双权威清理仍未满足退出条件。这个结论刻意区分“代码存在”“fixture 通过”
和“已被真实用户/模型验证”，避免以 synthetic 成绩替代体验证据。

## 本轮已验证的机械闭环

| 项目 | 状态 | 可复跑证据 |
| --- | --- | --- |
| 冻结场景、replay 与机制闭环 | 通过 | `uv run python scripts/verify_world_v2_scenarios.py --workdir /tmp/world-v2-scenario-suite-phase8 --output /tmp/world-v2-scenario-suite-phase8.json`；120 场景 manifest hash `9be374805c19fb3302287ca375310eb81830fd89934f1cacf5708ff70cf2c02e` |
| 机制归属/旧 writer 静态检查 | 通过（受扫描范围限制） | `uv run python scripts/verify_mechanism_catalog.py`；schema 2、20 mechanisms |
| 已选 v2 平台分支不回流 legacy authority | 通过 | `uv run python scripts/verify_world_v2_platform_architecture.py` |
| 测试经济、预算、思考路由和离线延迟合同 | 通过 | `tests/world_v2/test_economy.py` 及 scenario fixtures |
| P0/P2 媒体 preview | 通过 | SQLite production tests：声明 → candidate → proposal → acceptance → immutable V2 sidecar |
| P3 可达性（第一段） | 通过 | 受控 `RecipientScopedImageEvidenceDeclared` write seam → private `PhotoCandidateOpened`；见 `test_production_p3_declaration_opens_only_a_private_character_candidate` |

## P3 的真实边界

P3 目前不是泛化“私密图片开关”。它只允许一个收件人绑定、可回放的角色
preview 机会：私密 source、角色在场、`character_front_camera` 或合法
`mirror`，再加来自账本的 relationship context 与正向 embodied state 或已声明
private transition。模型只选候选 token，不能自报关系、收件人、lane、动作或
证据。

目前仍未关闭：

- relationship authority 未接入 `WorldV2TurnApplication` 的生产组合；因此
  SQLite application 尚不能自然跑到 P3 selection/acceptance/V3 sidecar，
  相关合约目前由纯 ledger/adapter tests 覆盖；
- `exclusive_private`、coverage、shared ritual、recipient display 与
  `suggestive_private` 仍 fail-closed；
- recipient-scoped contract 接受 `personal` 或 `private` 视觉事实，但当前 P3
  选择器只接受 private source。personal 必须在候选发现阶段显式忽略，或另立
  non-intimate lane；在此之前不能把 personal declaration 当成可选择媒体。

最后一项是下一轮应优先修的语义缺口，不能用放宽 selector 来掩盖。

## Phase 8 仍被阻断的项目

### 1. 正式人味评估

`companion-world-v2-formal-eval verify-fixture` 的流程和签名工件存在，但以
synthetic fixture 运行时必然返回 `blocked`，原因是
`synthetic_fixture_not_external_evidence`。正式门槛需要：

- bare / archive / v2 各 120 场景、每场 3 seed 的真实模型输出；
- 两份独立、盲态评审和固定 rubric/statistics 版本；
- 签名的结果包与原始机械 trace。

没有这些输入，不能声称 v2 在人味上优于裸聊或归档版本。

### 2. 真实性能与成本 SLO

离线 profile 能拒绝不合理的 thinking、多次模型调用、预算泄漏和热路径
全量 replay，但真实 provider/transport 的 hot/cold trace 仍未量到足够样本。
旧 24 个 hot 样本 P95 只能作为历史参考，且包含 hard issue，不能作为当前
World v2 的性能验收。

### 3. 平台与展示迁移

- HTTP v2 host 与 `/world-v2/dashboard` 的只读 DTO 已具备；
- QQ 仅兼容的单用户 C2C text 可走 v2，群聊、多用户、非文本仍 archive/reject；
- 旧 `/dashboard` 仍是 legacy 页面，尚未替换为 v2 DTO consumer；
- legacy Engine/archive writer 仍保留，静态 guard 只能证明被选择的 v2 路径不
  导回它们，不能证明仓库中再无第二权威。

## 下一步收敛顺序

1. 完成 relationship proposal/acceptance 的 production composition，再添加 P3
   SQLite 端到端：private declaration → candidate → model token → P3 acceptance
   `.2` → V3 sidecar → planner bridge → replay。
2. 明确 recipient-scoped `personal` 的产品语义：若它不是 intimate preview
   的来源，则候选发现器必须拒绝它；若要支持，则新增独立 lane/selection/
   authorization contract，不能复用 private lane。
3. 将 dashboard 默认读路径迁至 v2 DTO，并为 QQ archive/reject shape 建立
   可审计的 ingress matrix；随后扩大 static guard 至实际注册路由。
4. 在具备真实模型、provider trace 和独立评审条件后执行 formal evaluator，
   再决定是否关闭 Phase 8。

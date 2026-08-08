# Girl-Agent 代码库索引

本地优先的"赛博伴侣"项目：LLM 驱动的虚拟角色（沈知栀 Celia Shen）住在事件溯源的 World V2 里，通过 QQ 聊天与用户互动。设计宗旨见 `AGENTS.md`（受控高随机：模型有行为决定权，确定性代码只守硬边界）；领域词汇表见 `CONTEXT.md`（World Event / Projection / Pinned Turn / Inner Life Snapshot 等全部术语）。当前业务和架构意图只有 `docs/design/girl-agent-design-intent.md`，实施顺序只有 `docs/design/root-causes-and-long-coupling-luna-plan.md`。改 World V2 前先完整阅读这两份权威文档、`CONTEXT.md` 和 ADR 0010；其他设计文档仅作为历史证据，发生冲突时不得覆盖这两份权威文档。

## 架构全景：两个世界

- **旧 daemon 层**（`src/companion_daemon/` 顶层模块）：FastAPI、适配器、情绪状态机、旧"图片机"（`event_media.py` 6036 行）、预算控制。多数顶层模块已被 World V2 取代，仅适配器/预算/LLM 客户端仍活跃。
- **World V2**（`src/companion_daemon/world_v2/`，374 模块）：事件溯源核心。Append-only 账本（`sqlite_ledger.py`）+ 确定性投影 + 接受链 + CAS + replay。模型是**提议者**：`Model Result` 带哈希，replay 只重放不重调模型。

## 入口与命令（pyproject.toml）

| 命令 | 文件 | 用途 |
|---|---|---|
| `companion-daemon` | `app.py` | FastAPI 服务（端口 8765） |
| `companion-sim` | `cli.py` | World V2 垂直模拟（`--fake` 不调 API，需 DEEPSEEK_API_KEY） |
| `companion-napcat` | `napcat_cli.py` | **生产路线**：QQ 小号 NapCat (OneBot v11) |
| `companion-onebot` | `napcat_cli.py` | 通用 OneBot 路线 |
| `companion-world-v2-test-economy` / `-formal-eval` | world_v2 下 CLI | 离线评估/夹具 |

常用脚本：`run_napcat_adapter.sh`（生产）、`run_daemon.sh`、`interactive_chat.py`（REPL）、`chat_with_world_v2.py`（走生产 host 但拦截发送）、`run_isolated_daemon_acceptance.py`（双进程真实链路验收）。`run_qq_ws.sh` 引用的 `companion-qq-ws` 入口已从 pyproject 移除，疑似过期。scripts 里的 `.cmd/.ps1`/`celia_v3` 系列是无关的模型训练流水线，可忽略。

## HTTP 路由（app.py）

- `POST /messages` — 聊天入口（幂等 message_id，World V2 冷启动返回 503 可重试）
- `POST /internal/world-v2/tick` — 调度器时钟推进（需 operator token）
- `POST /internal/world-v2/drain` — Action/后台恢复
- `GET /world-v2/room` / `/world-v2/dashboard` / `/world-v2/life-state` — 只读投影 DTO
- `/health` — capture 就绪 + Character Interior 健康检查（fail-closed）

## 消息管线（核心流程）

QQ → `qq_c2c_onebot_app.py` → `qq_c2c_host.py` → `platform_host.py` → `world_turn_runtime.py`（`WorldTurnRuntime.respond`，转 Observation）→ `world_v2/runtime.py` `WorldRuntime.ingest`：

1. 提交 ObservationRecorded（触发 TriggerProcess，CAS）
2. `pinned_turn.py` `PinnedTurnCompiler` 钉 cursor + 编译 Context Capsule
3. 模型调用：`character_interior/inbound_turn.py` → `core.py` `CharacterInterior.consider`（8-facet Inner Life Snapshot 是唯一上下文）→ `inbound_wire.py` 结构化表达
4. 审计提交（ProposalRecorded/ModelResult）
5. 接受链：`unified_inbound_decision` 闭式检查 → acceptance recorder `prepare_batch` → `AcceptedLedgerBatchIssuer` 签 digest → `ledger.commit_accepted`（CAS）
6. Action：`action_pump.py` claim → `platform_action_executor.py` 发送
7. 回执：`runtime.settle` → `settlement.py` `SettlementPlanner`

**Fast reply**：`character_interior/inbound_wire.py` 的 `_is_lossless_minimal_reply_draft` 分流——能无损压缩成单文本走 MinimalReply（生产主要路径），否则走完整 ExpressionPlan 多 beat（`expression_episode.py` 的旧双作者协调器已删，只留验证函数）。

## 子系统地图

### Character Interior（角色内心，`world_v2/character_interior/`）
- `contracts.py` — 全部公开契约（InnerLifeSnapshot/InnerTransition/InnerDecision）
- `core.py`（72KB）— `CharacterInterior` 深模块，仅 `project`/`experience`/`consider` 三入口
- `inbound_author.py`（151KB）/ `inbound_wire.py`（511KB）/ `structured_role.py` — 模型输出物化 + 表达校验
- `snapshot_compiler.py` — 从 Capsule 确定性编译 8-facet 快照
- `world_stimulus.py`（82KB）— 已提交世界事件 → 内心刺激 → `experience()` → InnerTransition
- `production.py` — 生产组装 + 后台驱动（proactive/private impression/silence/reconsideration）
- 主观状态事件流：Appraisal/Affect/Aspiration/Thread/Private Impression 都是"模型提议 → compiler → acceptance runtime → reducer 投影"模式。Appraisal/Affect 有完整接受运行时；Aspiration 无独立 acceptance（DomainMutationPayload）。

### 生活生态（Life Ecology）
- `activity_lifecycle_*` — 日常活动：模型从不透明 token 目录选 opening → compiler 派生权威字段 → 原子落账（ActivityStarted/Completed 等）。`activity_timing.py` 是纯规则（完成须 ≥60s 等）
- `life_ecology_runtime.py` — 调度器：clock tick 后按序跑 biographical→activity→aftermath→life_development→npc_initiative→open_world→visual_evidence→media
- `biographical_lifecycle*` — Life Arc 开/关（从已结算 outcome 提取），驱动 NPC 出现/离场
- `npc_ecology.py`（1708 行）— NPC 私有决策（actor 模型）+ 世界裁决（world author），产出 NPC Plan/Occurrence 走普通 aftermath 路径被主角消费。种子在 `configs/world_seed.yaml`（38 处 npc）
- `world_life_context.py` — settled occurrence → 模型上下文（ActiveWorldOccurrencePremise）

### 媒体系统（图片机）
- 管线：生活事件 → `event_ecology_media.py` 冻结 PhotoCandidate（12 类 taxonomy）→ `media_selection_worker.py` 交角色决定 → acceptance（provider grant+预算+关系）→ `media_planning_runtime.py`（桥到旧 `event_media.py` MediaPlanner v5）→ `media_execution_runtime.py`（`image_generation.py` OpenAI 生成 → `OpenAIMediaInspector` 审查 → ≤1 次修复）→ `media_delivery_runtime.py` 自动发送（每日上限+最小间隔）
- 隐私分层：`media_eligibility.py` `MediaEligibilityRouter` 划 ordinary/personal/intimate；P3 私密车道有 `PrivateRenderContract` 但**部署未安装** private prompt author/专用生成器，fail-closed
- `image_generation.py` 里 VolcArk/Civitai/ComfyUI/Fallback 等 provider **全部无消费者**，生产只接 OpenAI 一家

### 外部感知
- `world_v2/external_world_perception/` — RSS/NWS/USGS 源 → `hub.py` 采集/去重/嵌入/聚类 → `attention.py` 影子/实时注意力 → 模型决定 → ExternalPerceptionRecorded → 生活影响。靠 registry off/shadow/live 模式门控，半启用
- QQ 附件：`perception_trigger_runtime.py` 闭式语法决定是否分析 → `character_interior/qq_attachment_perception.py` 角色考虑 → `perception_vision_transport.py`（OpenAI vision，SQLite 持久化）

### 适配器与支撑
- `qq_client.py`（官方 HTTP）/ `qq_delivery.py`（双路分发）/ `onebot_adapter.py`（OneBot v11 文本/图片/face）/ `qq_outbound_owner.py`（出站租约锁）
- `conversation_cadence.py` — 对话热度分类器（hot/warm/cold，供 model_call_policy 用），**不是**消息批处理
- `budget.py` + `usage_metrics.py` — 月/日/软日 CNY 预算门 + 带版本价格表
- `llm.py` — `DeepSeekChatModel`/`OpenAICompatibleChatModel` + ProviderCircuitBreaker + 用量统计

## 关键不变量（改代码前必知）

- 一切写路径走 `WorldRuntime` 单入口；model-facing 的调用必须记录 ModelResult 供 replay
- 提交批次经 `batch_invariants.validate_commit_batch`；CAS 冲突抛 `ConcurrencyConflict`
- `vertical_registry.py` `assert_bounded_vertical_coverage` 是启动门
- Producer-First Authority：新 authority 必须和第一个生产者同批落地（见 CONTEXT.md）
- `configs/mechanism_closure.yaml` 标记 dormant 机制（如 resource_authority 四权威、v16 harness）

## 已确认的死代码/未接线（2026-08-06 盘点）

- `world_v2/scenario_runner.py`、`shared_private_invitation.py`、`recent_dialogue.py`、`scenario_corpus.py` — 孤儿/退役
- `sealed_production_fact_registry_v2.py`、`sealed_fact_commit_adapter_v2.py` — 0 引用占位
- `aspiration_seed_policy.py` — 仅测试引用；`npc_initiative_weight_policy.py` — 仅测试
- `appearance_state` / `visible_physical_state` 记录者 — 宿主 seam 存在但**无内部调用者**（半成品）
- `world_media.py`、`image_requests.py`、顶层旧图片机车道 — 无消费者
- `resource_authority_*` 四权威 — 官方 DORMANT

## 测试布局

- `tests/` 顶层 28 文件：适配器、预算、媒体选片契约、房间编译器
- `tests/world_v2/`：333 文件 ~3550 测试函数。character_interior 最大；含 ledger/sqlite、expression、npc_ecology、life_*、migration golden、formal_evaluation
- **无直接测试**：`conversation_cadence.py`（间接）、`qq_outbound_owner.py`（间接）、`cli.py`、多数 media_* 顶层契约、`world_media.py`
- `tests/support/` 是共享 fixture 构造器（非适配层）；`tests/js/` 是房间渲染器 JS 测试

## 文档指引

- 当前权威业务与架构意图：`docs/design/girl-agent-design-intent.md`
- 当前唯一执行计划：`docs/design/root-causes-and-long-coupling-luna-plan.md`
- ADR：`docs/adr/0010-controlled-high-variance-character-agency.md` 必读
- 其余 `docs/design/` 文件、成本与形象文档均是可追溯的历史或专项证据，只能由上述两份权威文档按需引用，不能成为并列路线图。

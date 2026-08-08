# 伴侣体验路线图：设计、需求、现状与计划

> 2026-08-06 建立。本文档是产品层共识文件：设计宗旨、体验需求、代码现状、行动计划。
> 代码级索引见根目录 `CLAUDE.md`；术语表见 `CONTEXT.md`；协作宗旨见 `AGENTS.md`。
>
> **2026-08-08 后续深化**：本文件继续保存生产排障、真实采样、成本与阶段性修复记录；
> 当前业务与长链架构规范已并入 [`girl-agent-design-intent.md` §11](./girl-agent-design-intent.md)，
> 施工顺序见 [`root-causes-and-long-coupling-luna-plan.md`](./root-causes-and-long-coupling-luna-plan.md)。
> 当本路线图中的阶段性可用性补丁与 AGENTS.md、ADR 0010 或上述深化设计冲突时，以后者为准；
> 历史实验结论仍作为实现证据保留，不回写成从未发生。

## 1. 定位与设计宗旨

**产品定位**：一个极度仿真的个体——有自己的生活，能模拟真实人类产生的一切行为，通过聊天把生活反馈给用户。不是聊天机器人，是一个"住在这台机器里、过着人生"的人。

**三层模型**（核心共识）：

```
机制层   = 她能像人一样生活的能力底座（生活、记忆、情绪、关系、感知、世界运转）
模型层   = 她决定自己要过什么样的生活（人生选择）+ 她怎么说话（文风、语感、分寸）
界面层   = 聊天、图片、主动联系——让用户感受到她的生活与情绪
```

- 机制决定"可能性与事实"，模型决定"意愿与表达"——机制从不替她做人生决定
- 受控高随机（AGENTS.md）：模型有行为决定权，确定性代码只守硬边界
- **沉默是设计内行为**（生气不想回、没想好、在忙）——但**当前的失声是故障**，必须区分

## 2. 需求：体验目标（"活人感"清单）

以下每一项都是产品需求，现状标注在括弧里：

| # | 体验 | 依赖机制 | 现状 |
|---|---|---|---|
| 1 | 主动发消息（生活分享、想念、突发） | 主动驱动 proactive + social_initiative | 有机制，效果未验证 |
| 2 | 连续多气泡说话 | 表达单元流 Expression Unit Stream | 有，生产路径已切换 |
| 3 | 打断 / 被打断 | 文本回合端点 text_turn_endpoint + 批处理 | 部分实现，判断质量未知 |
| 4 | 发照片分享生活（图片机"拍下"生活） | 媒体系统全链路 | 链路在，但只接 OpenAI，P3 车道 fail-closed |
| 5 | 吵架后一直生气、不想回消息 | 关系 + Affect + 沉默提案 | **疑未真正接通**（待查） |
| 6 | NPC 的事影响她的情绪、表现给用户 | NPC 生态 → 刺激 → 内心 | 链路在，NPC 生态较新（刚合并） |
| 7 | 口是心非（内心与表达分离） | 内心状态 Private Turn State + 表达 | 契约存在，产出质量未知 |
| 8 | 想起很久以前的事、触景生情 | 记忆检索 + 场景触发 | 召回存在，语义召回默认关闭 |
| 9 | 高仿真聊天体验（文风、速度、不失声） | 声音层（见 §4） | **最弱环**：不如裸模型 |

**可量化指标（拟建）**：

- 故障失声率 = 技术失败导致的静默 ÷ 消息数（目标：趋近 0）
- 回复延迟 p50 / p95（含批处理等待与模型调用）
- 活人感 A/B 分：同段对话，系统 vs 裸模型，按 §2 清单打分

## 3. 现状盘点

### 3.1 架构地图（摘要，详见 CLAUDE.md）

- **World V2**（374 模块，事件溯源）：账本 `sqlite_ledger.py` + 确定性投影 + 接受链 + CAS + replay；模型是提议者，Model Result 带哈希，replay 不重调模型
- **消息管线**：QQ → c2c host → `WorldTurnRuntime.respond` → `WorldRuntime.ingest` → PinnedTurn 钉 cursor 编译 Capsule → `CharacterInterior.consider`（8-facet Inner Life Snapshot 是唯一上下文）→ 审计提交 → 接受链 → Action → 回执结算
- **子系统**：Character Interior（内心）/ Life Ecology（生活）/ NPC 生态 / 媒体图片机 / 外部感知 / 适配器

### 3.2 断点清单（2026-08-06 全库盘点确认）

**死代码/0 引用**：

- `world_v2/scenario_runner.py`、`shared_private_invitation.py`、`recent_dialogue.py`、`scenario_corpus.py`
- `sealed_production_fact_registry_v2.py`、`sealed_fact_commit_adapter_v2.py`
- `world_media.py`、`image_requests.py`、顶层旧图片机车道
- `resource_authority_*` 四权威（官方 DORMANT）
- `aspiration_seed_policy.py`、`npc_initiative_weight_policy.py`（仅测试引用）

**未接线/半成品**：

- `image_generation.py`：VolcArk/Civitai/ComfyUI/Fallback 等 provider 全部无消费者，生产只接 OpenAI 一家
- P3 私密媒体车道：`PrivateRenderContract` 有契约但**部署未安装**专用生成器，fail-closed
- `appearance_state` / `visible_physical_state`：记录者存在但**无任何内部调用者**
- 外部感知：RSS 采集已接线但靠 off/shadow/live 模式门控，**半启用**
- `DomainCompilerRegistry`：生产 fail-closed，未接线
- 语义召回（embedding）：默认关闭，只有精确召回

**其他风险**：

- 当前分支 `feature/companion-turn-v2` 有大量未提交修改（含删除 launchd plist、迁移脚本），WIP 状态不明确
- `run_qq_ws.sh` 引用的 `companion-qq-ws` 入口已从 pyproject 移除（过期脚本）

### 3.3 三症状根因分析

| 症状 | 根因 | 性质 |
|---|---|---|
| **失声（故障）** | fail-closed 哲学 + 超长依赖链（感知→内心→来源闭包→接受链→发送），任何一环失败=沉默；恢复走 10/30/120 分钟生命周期 | 架构性，需结构改动 |
| **高延迟** | 每轮多次串行模型调用（内心整合 + 独立来源审查 + 可能重选）+ 批处理等待 + 冷启动 replay | 架构性，需结构改动 |
| **幻觉** | 来源闭包机制就是防幻觉的，但它本身是失声和延迟的来源 | 权衡点 |
| **体感不如裸模型** | 模型看到的上下文是被 token 预算化、规范化、摘要化的；回复被当文档审批 | 声音层被机制压扁 |

## 4. 方向与原则

1. **机制全保留**：它们是"她像人"的来源，不是负担。要改的是机制和声音的接法。
2. **声音层松绑**（核心改动）：
   - 上下文喂高保真的"生活料"（情绪、记忆、关系、最近发生的事用人话描述），近期对话原始历史完整给她；不用数据摘要当唯一上下文
   - 一次模型调用出回复；来源闭包/接受链从主路径摘到异步或按需（只有世界事实声明才拦）
   - 技术失败走快速降级（轻路径重试、诚实状态反馈），绝不静默
3. **两种沉默分开**：设计内沉默（角色选择，罕见、有语境、事后可解释）与故障沉默（技术失败）——数据层已分，体感层必须分。
4. **先接线、先清理，不堆新机制**：断点清单是待办而不是待扩展。
5. **每次改动可验证**：改完跑得起来、日志可查；用户无能力自行验证。

## 5. 行动计划（分阶段）

### 阶段 0：量化基线（只读，先做）
- [ ] 统计 `logs/` 与账本中故障失声的发生率与环节分布（runtime 的 technical failure / silence 记录）
- [ ] 跑 `scripts/run_isolated_daemon_acceptance.py`（real-provider）测量每跳延迟
- [ ] 建立失声/延迟报告脚本，作为后续改动的前后对照

### 阶段 1：止血（优先，目标：失声率↓、延迟↓）
- [ ] 找出主路径上"门"的清单，逐个评估能否后置/异步/按需
- [ ] 技术失败快速降级路径（现有 Expression Reliability Lifecycle 的恢复车道是否够快）
- [ ] 冷启动 replay 与批处理等待的延迟优化

### 阶段 2：通设计（把"活人感"需求逐条接通）
- [ ] 需求 #5 吵架→赌气→不回：查 Affect/关系状态 → 沉默提案链路是否真通
- [ ] 需求 #6 NPC→情绪→表现：验证新 NPC 生态到表达的真实链路
- [ ] 需求 #7 口是心非：验证 Private Turn State 与实际表达分离的质量
- [ ] 需求 #8 触景生情：评估语义召回开启的成本与收益
- [ ] 需求 #1 主动消息：#4 发照片：验证 proactive 与媒体自动发送的实际效果

### 阶段 3：接线与清理
- [ ] 死代码删除（§3.2 清单）——先删无风险的，半成品逐个决策（接线 or 删除）
- [ ] 外部感知模式决策（off/shadow/live）
- [ ] 语义召回开关决策
- [ ] 处理 `feature/companion-turn-v2` 的 WIP 状态（提交 or 收尾）

### 阶段 4：体验评测常态化
- [ ] 活人感 A/B 评测脚本（系统 vs 裸模型）
- [ ] 每次大改前后跑评测，防止回归

## 6.5 失声根因实盘复盘（2026-08-06 晚，自聊复现）

用 `chat_with_world_v2.py --clone` + 真实模型自聊复现失声，逐层挖出的根因链：

1. **审查服务器（本地 qwen）宕机 → 全链路静默**：来源闭包审查器配置为本地 `127.0.0.1:8191`（qwen2.5-7b），服务器不在 → ConnectError → 包装成 `role_faculty_unavailable` → 纠正尝试也无续体 → ModelResult 记 main_exception → **失声**
2. **本地审查服务器根本不可行**：拉起来后实测——模型 id 不匹配（短名 404）、生成分钟级（>120s）、Metal GPU 内存耗尽崩溃（`kIOGPUCommandBufferCallbackErrorOutOfMemory`）+ prompt 缓存形状 bug。24GB 机器 7% 内存空闲，7B 模型撑不住
3. **审查车道在 12s 交互预算内无法完成**：模型调用 ~8.5s + 本地审查 >3.5s 剩余预算 → CancelledError → 静默
4. **生产服务本身没在 launchd 里**：daemon/napcat 都是终端前台跑的，终端一关就死；launchd 只加载了 sillytavern/rsshub 等
5. **间歇性失声残留**：DeepSeek 的 appraisal draft 偶发违反硬契约（`AppraisalDraft meaning is invalid`），一次纠错也失败 → role_faculty → 静默

**已做修复（2026-08-06 晚）**：

- [x] `context_capsule.py` 的 Character Core actor 校验改为兼容遗留别名 `actor:companion`（与 ledger_context_resolver 的规则对齐）——修复"Character Core belongs to a different actor"全量失声
- [x] `.env` 审查配置切到**发布合格云端路由**：OpenRouter `qwen/qwen-plus` + OpenAI `gpt-4.1-mini`（recovery 同），清除本地 8191 配置（备份在 `tmp/backup-env-20260806.env`）
- [x] 重启生产 daemon（8765）+ napcat（8787），health 全绿（daemon capture ready / napcat 0 failures）
- [x] 自聊验证：她能正常回复（"还行，今天在书店待了会儿，翻了些旧书。你呢？"）

**残留问题（下轮迭代）**：appraisal 硬契约校验失败的静默（模型输出质量偶发）；生产服务没进 launchd 的运维风险；proactive 车道 `deliberation_failed`（health 里可见）。

## 6.6 第二轮迭代（同日深夜）：间歇失声 + 延迟量化

自聊继续暴露的失败链（逐层修复）：

1. **stream 级联死亡**：head 首次调用失败 → publish None → corrective 槽位（唯一恢复端口，quick_recovery 生产未装）调 stream_tail 必拿 None → 必死。修复：`inbound_turn.py` `continue_transport` 在 owned_head=None 时重跑完整 head 决策（`propose_stream_head`）
2. **appraisal confidence 浮点**：模型输出 `meanings[].confidence: 0.7`（概率尺度），契约要求 0-10000 整数 bp → `AppraisalDraft meaning is invalid` → 纠错也失败 → role_faculty → 静默。修复：`inbound_appraisal_wire.py` 接受 0-1 浮点并确定性换算 bp（+2 测试）
3. **recall 嵌入异常噪音**：嵌入服务器没起时 `_RecallEmbeddingCooldown` 日志刷屏（本身有降级，不是致命）

**延迟量化（关键数据）**：每轮 4-5 次串行模型调用：
- 主 deliberation prompt **20k-50k 字符**（recall 增强后更大）→ 8-13s
- 三轮来源审查（inventory / report-relative / V7）各 12-21k 字符 → 3-8s
- 总计 12-20s vs 实际预算线 ~13-15s → `budget_exhausted` 取消 → 静默

**结论**：失声的第二大来源 = 延迟超预算。修延迟 = 同时修失声。候选杠杆：主 prompt 瘦身（capsule 上下文预算/recall 增强开关）、审查轮次裁剪（无声明时跳过 report-relative？）、预算线微调（牺牲一点延迟换稳定）。

## 6.7 第三轮迭代（2026-08-07）：延迟成分分解

用 provider 调用钩子量化每轮模型调用（主调用走 streaming 路径，需钩 `complete_json_stream_with_usage`）：

**单条消息的 6 次调用（共 ~112k 字符 ≈ 56k tokens）：**

| 调用 | 大小 | 内容 |
|---|---|---|
| text-turn 探测 | 0.8k | "用户说完没有"语义判断 |
| **主 deliberation（流式）** | **44.6k** | system 指令 20.5k（appraisal 契约 3.8k + expression 契约 11.4k + 输出信封 3.5k + stream 传输 1.1k）+ user 上下文 24.1k（inner_life_snapshot 14.3k + hard_boundaries 6.5k + trigger/request/capabilities 4k） |
| V7 全量审查 | 20k | 9898 指令 + 10k 证据信封 |
| RR.3 报告相对审查 ×2 | 12.6k ×2 | 7353 指令 + 5.3k beats 信封 |
| inventory 增强 V7 | 21k | 9898 指令 + 11.1k 证据+decomposition |

**inner_life_snapshot 14.3k 构成**：materials 7.5k（dialogue 1.7k/self 1.1k/experiences 1.3k/situation 0.8k/relationship 0.5k/prefetch 1.2k）+ **source_inventory 4.1k**（审计元数据）+ faculties/contract/cursor 等信封 ~2.7k。

**本轮落地**：
- [x] `model_facing_context.py`：provider 视图剥掉快照的 `source_inventory`（materials 已带 source refs，硬边界清单已命名 claim scope）→ 主调用每轮省 ~4k 字符（测试 29 个全过）
- [x] 修正 `inbound_turn.py` stream 回退的 AttributeError（author 方法调用）

**下轮候选（按收益/风险排序）**：
1. **expression 契约文本压缩**（11.4k/主调用，~10% 总量）——需保证 wire 合规，先做契约保真测试
2. **RR.3 审查去重**（跑了两次，省 12.6k ≈ 11%）——需确认两次的语义差异
3. 审查指令文本（9898×2 + 7353×2 = 34.5k）压缩
4. 预算线/延迟权衡：实测成功回合 9.4-14.7s、失败回合 15-17s（预算 ~15s 附近）

## 6.8 成本模型（2026-08-07，用户担忧 56k tokens/条太贵）

**实测单条 ~57k tokens ≈ ¥0.08-0.10**（USD/M：flash miss $0.14/hit $0.0028/out $0.28；qwen-plus ~$0.3；gpt-4.1-mini ~$0.4）：
- 主 deliberation ~23.5k in（静态指令 10.2k 走缓存后近乎免费）
- V7 审查 ×2 ~20k in（~$0.006）
- RR.3 ×2 ~12.6k in（~$0.004）
- 输出 ~2.5k（~$0.001）

**月度推演**：100 条/天 ≈ ¥270/月；300 条/天 ≈ ¥800/月；1000 条/天 ≈ ¥2700/月。（注：英文契约 ~4 字符/token，实际每条 ~¥0.05-0.06，比字符估算更乐观）

## 6.9 成本压缩第一轮（2026-08-07 深夜）

**关键发现**：
1. **系统指令字节级一致 → DeepSeek 缓存可命中**（hit 价 = miss 的 1/50）——静态文本压缩省的是 miss 场景的钱 + 延迟，不是主成本
2. **inventory 车道坏了**：gpt-5.4-nano 返回的 locator 是**意译**（text 与 char 偏移不符原文）→ `inventory_invalid` → 每次消息降级 + 无效重试 → 审查阶梯加倍（每轮 4 轮审查、~112k 字符）
3. **主调用系统消息有 2990 字符逐字重复**：`expression_draft_shape_contract()` 被拼了两次（EXPRESSION 段 + COMBINED OUTPUT ENVELOPE 段）

**本轮落地**（均备份 + 测试 + 自聊验证）：
- [x] 删除 `inbound_author.py` envelope 段的 shape_contract 重复副本 → 主调用系统指令 20456 → 17466（-14.6%）
- [x] `.env` 禁用 inventory（`WORLD_V2_SOURCE_INVENTORY_ENABLED=false`）→ 回退文档化的全量 V7 路径 → **每轮 6 次调用 112k 字符 → 4 次调用 76.4k 字符（-32% tokens）**；顺带移除 inventory_invalid 噪音
- [x] 自聊验证：2/2 成功回复，含多气泡，主调用 6.9s（此前 9.4s）

**成本现状**：每条约 ¥0.04-0.05（此前 ~¥0.09），且失败重试率下降后更稳。¥100/月 ≈ 2000-2500 条/月 ≈ 70-80 条/天。

**下轮候选**：
1. ~~修 inventory 模型~~ → **已完成：换阿里百炼 qwen-turbo**（见 6.10）
2. **失败重试消除**：预算取消 → 重试全量 → 双倍花费；修延迟结构（预算分段/取消不重试）能同时省钱和降失声
3. 审查输入瘦身：无声明消息的 evidence 信封可精简
4. ~~预算门~~ → **发现 World V2 完全没接 BudgetGate**（预算门只服务旧 daemon 路径；ModelResult usage 记录缺失 = 成本不可见）——需要成本监控基建，不是设 .env 就能解决

## 6.10 成本压缩第二轮（2026-08-07 凌晨，用户睡觉期间自主迭代）

**inventory 换国内直连（阿里百炼 qwen-turbo）**：
- 发现 ARK_API_KEY（火山）未开通任何模型接入点（全部 404）；QWEN_API_KEY（阿里百炼）可用
- 实测 qwen-turbo exact-span 提取：输出精确子串（偏移偶偏 1 字符）——代码已有确定性 span 归一化（inbound_wire.py:3450，"Repair offsets only when that exact byte-for-byte string occurs once"）→ 通过；而 gpt-5.4-nano 的**意译输出**（非原文子串）被正确拒绝
- 改动：config.py 加 `qwen_api_key`（QWEN_API_KEY）；semantic_chat_composition.py inventory 双 lane 在 local base_url 时用 qwen_api_key；.env 指向 dashscope 直连（**免代理**，qwen-turbo ~$0.04/M = gpt-5.4-nano 的 1/6）
- 效果：**每轮 6 调用 112k 字符 → 3 调用 59.2k 字符（-47%）**；inventory_invalid 噪音消失

**输出限长契约**（inbound_wire.py expression_draft_shape_contract）：
- inner_state_summary ≤140 字符、brief_rationale ≤120 字符（不影响用户可见文本）
- 效果：成功回合 9.4s → **6.4s**

**延迟硬事实**（实测）：DeepSeek flash 处理 20.8k tokens + 800 输出 = **10.1s**——12s 预算对"主调用 10s + 审查"的结构必然频繁失败。主调用输入/输出各减半是唯一出路（已做一部分）。

**新发现待办**：
- World V2 无预算门、ModelResult usage 缺失 → 成本监控基建（metering partial 修复 + BudgetGate 接线）
- 间歇崩溃：~~`proposal_audit._strict_result`~~ → **已修复**（pinned_turn 审计失败降级为 content-free technical result，带测试）
- 主调用 20.8k tokens 的剩余大头：hard_boundaries 6.5k（含 alias→哈希表，模型可能不需要完整哈希，待确认）+ inner_life_snapshot 信封

## 6.11 事件机断链诊断（2026-08-07 凌晨，用户指定修复）

**症状**：生活事件 3 天零产出（最后一次 Activity/Occurrence 2026-08-04），但生态调度器每轮都执行（life_ecology trigger 正常开/完成）。

**根因链（已实锤）**：
1. 08-04 后 `ContextualLifeTechnicalFailureRecorded` ×32：`invalid_role_result_after_correction` ×7（lane=experience_memory）+ `provider_exception` ×3
2. **一个 outcome 的 experience 提取重试了 50 次**（retry_ordinal=50，每 2h 退避 = 4 天）——模型输出持续不合规，主+纠错两次调用全失败，每次重试烧 2 次模型调用
3. 失败 → 生态 pass 变 `life-ecology:technical_failure.life_development.context_unavailable` / `cooldown` → **生态整体停滞**

**性质**：与聊天侧同源——life 侧角色模型（DeepSeek flash）在 outcome/experience 场景输出不合规 JSON（聊天侧已修 confidence 浮点，life 侧 wire 未做同类容忍）。experience_memory 也走 CharacterInterior.consider → `_validate_role_result` 失败。

**修复方向（下轮）**：
1. life 侧 wire 容忍：定位 experience_memory/outcome 校验失败的具体字段（怀疑同款浮点 bp），与聊天侧一致处理
2. 重试上限：50 次 × 2h 的无限重试设计不合理（占死生态 + 烧钱），应设上限或降级
3. 保留的 exc_info 补丁（life_development_runtime except）无害，帮助下次定位

**本会话已修（聊天侧失声/延迟/成本）汇总**：actor 不匹配、审查服务器宕机→云端、stream 级联、confidence 浮点、契约去重（-14.6%）、inventory→qwen-turbo（-47% tokens）、输出限长（成功回合 6.4s）、审计失败降级。自聊成功率 3/5（含多气泡）。

## 6.12 成功率冲刺（2026-08-07 白天，目标 95%）

**10 条自聊基准（两次）：40-60% 成功率**。失败构成：role_faculty（多个底层）+ 预算取消（15-17s）+ 快速失败（3-7s）+ inventory_invalid 噪音（不致命但浪费调用）。

**本轮新增修复**：
- [x] **episode_id 降级**：模型选 update/resolve/supersede 但给无效 episode_id（编造 id 是高频行为）→ update/supersede 降级 open（保留组件）、resolve 降级 no_change（显式，不违反"技术失败≠no_change"）——不再杀死整个回合（inbound_appraisal_wire.py，测试更新）
- [x] **hard_boundaries 裁剪 alias→canonical 映射表**（~4-5k 字符/主调用）：模型输出 alias 即可（acceptance 侧 expand_expression_source_ref_aliases 独立构建别名表），映射表纯冗余展示（expression_draft.py，2 处测试更新，420 测试全过）
- [x] **运维发现**：pkill 后 `data/qq-outbound-owner.lock` 残留 → napcat 无法启动（"owner is already claimed"）——重启需清理锁文件

**剩余差距（95% 目标的路径）**：
1. **预算结构**（~30% 失败）：主调用 10s 硬成本 + 审查 2-3s 超预算——主调用输入 16k 动态上下文的继续压缩（inner_life 材料限数、hard_boundaries 已裁、prompt 静态 4.4k 已最小化）
2. **剩余 role_faculty 底层**（~30%）：逐个抓（diag 脚本+补丁就绪），可能是其他 wire 校验（纠错链）
3. **inventory_invalid 高发**（qwen-turbo 短文本 span 不稳，~40% 调用）——换 qwen-plus 或接受 fallback 噪音

**成本杠杆（按优先级）**：
1. **审查轮次裁剪**（最大头，~70% 成本）：实测 `world_claims` 经常为空——无事实声明的消息应跳过部分审查（V7/RR 各两次的重复需先查语义）
2. **失败回合白花钱**：预算取消的回合已产生调用费（失败率 ~30% = ~30% 费用打水漂）——修延迟 = 省钱
3. **缓存最大化**：DeepSeek 自动前缀缓存（hit 价 = miss 的 1/50），静态指令天然可命中
4. **契约文本压缩**：静态文本压缩省 miss 时的 token 费
5. **预算门未设**：MONTHLY_BUDGET_CNY/DAILY_BUDGET_CNY 全空——支出无上限，建议尽快设置

## 6.13 事件机断链实修 + drain 死循环（2026-08-07 中午）

**意外发现并修复的生产故障**：napcat 进程 CPU 99.6% 死循环 6 小时——`_drain_inbound_state_settlement_once`（runtime.py）的 `affect_accepted`/`relationship_accepted` 判定错查**待办列表**（affect_proposals/relationship_proposals），而接受后 proposal 会从该投影**移除** → 判定永假 → 无限重试 settle 同一 audit（6262 次/6h，幂等不落账但烧 CPU）。

**修复**（runtime.py）：
- [x] affect_accepted 改查**已接受状态**：affect_episodes 组件 appraisal_refs 引用 shape.appraisal.change_id（与 appraisal_accepted 查 projection.appraisals 同范式）
- [x] relationship_accepted 改查 relationship_signals 语义身份（subject_ref/signal_code/persistence/rationale）
- [x] 回归测试 `test_drain_settlement_skips_audit_whose_affect_was_already_accepted`（红→绿验证）

**事件机断链实修（§6.11 完成）**：experience_memory lane 55 次失败（ordinal 到 54）。用**真实模型 + 生产账本副本复现**抓到根因——模型输出三类问题：
1. **salience 输出单整数 7000** 或**错误字段名对象**（{calmness:900,...}）——契约文本 "the exact eight installed salience basis-point integer fields" 没列字段名，模型只能猜 → **修复：契约列出 8 个 bp 字段名 + cue_kind/retention_rationales 枚举全量**（life_memory.py）
2. **decision 输出一层 payload**（wire 期望 {source_refs, payload} 两层 envelope）→ **修复：wire 归一化一层形态**（structured_role.py `_parse_and_validate`，确定性绑定 attended_source_refs，envelope 仍由本地补齐）
3. **bp 浮点**（0-1 概率尺度，聊天侧同款）→ **修复：materialize_fact_memory_draft 归一化 ×10000**（fact_memory_draft.py）

修复后复现脚本**单次调用通过**（attempt_ordinal=0，无需纠错）。测试：63+ 个全过（含新回归：bare decision payload 绑定、概率浮点归一化）。

**重试上限**（§6.11 方向 2）：
- [x] `_EXPERIENCE_MEMORY_RETRY_LIMIT = 8`：ordinal ≥8 后确定性放弃（return None，不阻塞生态 pass、不再烧模型调用）。生产 ordinal 54 立即触发，生态解卡

**生产验证**（重启 napcat 后）：
- CPU 99.6% → 6%（正常 idle）
- 新消息 settle **单次**完成（对比旧 6262 次循环）
- 生态 tick 正常（ClockAdvanced 每 10 分钟）
- 日志无 `memory.invalid_role_result_after_correction`

**注意**：`_outcome_retry_state`（outcome 提取）也有同款无限重试结构（ordinal 无上限 + 2h 退避），当前无堆积（0 个 outcome-model-failure 事件），未加上限——待触发时处理。

## 6.14 事件机双根因实修 + 预算门接入（2026-08-07 下午）

**事件机第二根因（life_development 卡死）**：生态 pass 每 10 分钟 `technical_failure.life_development.context_unavailable`。根因：`LifeDevelopmentCapabilityManifest` 校验失败 "NPC capability must remain inside entity authority"——`npc_identity_views` 返回 retired NPC（roommate-lin），`entity_refs` 只含 active → 越界。修复：编译器侧过滤（`life_development_capability.py` npc_capabilities 只取 entity_refs 内的 view）。修复后生态 pass 正常（cooldown = life_development 冷却期，正常状态）。

**时间线修正**：05:07-05:17 的 cooldown 来自无上限修复的旧进程；13:08 重启后（含全部修复）13:17 tick 正常 cooldown。life_development 下次尝试 14:54（冷却到期），届时**首次真正运行大事件机制**。

**预算门接入（成本可见）**：
- 新模块 `world_v2/model_usage_budget.py`：`WorldV2UsageStore`（SQLite 表 world_v2_model_usage + 成本估算 usage_metrics 价格表 × 7.2 汇率）
- 装配：`build_semantic_chat_composition(usage_observer=...)` → qq_c2c_host 建 store 传入 → flash/thinking 模型记 usage
- health：`/health` → scheduler.budget（月/日成本 + 预算 + exhausted 标志）；MONTHLY_BUDGET_CNY=80 / DAILY_BUDGET_CNY=3（默认，.env 可覆盖）
- 验证：单次调用记录 ¥0.012；测试 97+ 全过
- **未做**：月度硬阻断（超限拒绝回合）——怕引入新失声，先可见后阻断

**发现（转发言侧 agent）**：`chat_with_world_v2.py` 自聊撞 `proposal_audit._strict_result` "deliberation result failed strict revalidation" 崩溃（deferred 失声）——roadmap §6.10 记录已修，但此路径仍崩，疑似 WIP 回归。

**第三根因（reviewer 缺失）+ 主动触发验证**：`WORLD_V2_SOURCE_REVIEW_REDUNDANCY_ENABLED=false`（本地审查时代遗留）→ 云端审查器未装配 → life_development propose 路径 fail-closed（source_closure_reviewer_unavailable）。已打开 + 重启（health 确认 verified_fork）。用 `tmp/trigger_life_development.py`（副本账本+生产装配+真实模型，提交消息重置冷却）主动触发验证：**真实产出雷雨大事件**（premise + 3 候选结局 + random draw + occurrence 激活，causal_authority=world_contingency）——事件机一周来首次产出。成本估算：life_development ≈¥0.04/次 ≈ ¥15/月；总成本 ~¥105/月。

**闭环验证（同一脚本，多次 pass）**：
- 修复：① occurrence privacy ceiling（visibility 只取模型值，违反设计 §4.9 最严格值要求，导致 reducer "cannot weaken participant NPC privacy" 崩溃）→ 取 max(模型值, 参与 NPC privacy)；② prompt 强化 outcomes "EXACTLY 2-4" + provisional NPC "narrative:<tag>" 格式（原 "2-4" 被模型无视，高频失败）
- 验证结果：生成→激活→结算→Experience 全链跑通（4 次 pass 产出 3 事件、结算 2 个、Experience 27→29，结算文本有性格："知知匆匆路过没细看海报"）；life_development 成功率 ~75%（模型质量，失败有重试兜底）
- **未闭环**：体验→记忆（脚本装配不完整导致 projection_unavailable，非生产 bug——生产装配完整待观察）；Appraisal/Affect 消费（background driver 未装脚本）；VisualFact 未接线。

**孤儿 committed 处理（用户指定）**：澄清——committed 是**合法 later 计划状态**（生产 c7ee0bc7 是今晚 19:00 计划事件），不是失败残留。真正缺口：**opens_at 到期无激活逻辑**（计划永不开始）。修复：life_ecology_runtime 加 `_activate_due_occurrences`（每个生态 pass 自动激活窗口已开的 committed occurrence，幂等 + CAS），回归测试 `test_life_ecology_activates_a_due_planned_occurrence`，186 测试全过，已部署。今晚 19:00 生产计划事件将首次被自动激活。

**共享配置冲突解决**：`.env` 的 `WORLD_V2_SOURCE_REVIEW_REDUNDANCY_ENABLED` 被发言侧 agent 改回 false（14:39）——但发言侧加了**独立开关** `WORLD_V2_LIFE_SOURCE_REVIEW_ENABLED`（默认 True，注释明示"事件机继续产出"），life 审查与聊天路径解耦。health 确认 reviewer 装配（gpt-4.1-mini + qwen/qwen-plus）。

## 6.15 事件机全 lane 检查（2026-08-07 傍晚，用户指定）

**检查方法**：扩展触发脚本装配全部 followup（activity/biographical/aftermath/life_development/npc/visual_evidence），副本账本 + 真实模型多 pass 采样。

**新修复两个真实 bug**：
1. **life 输出被 max_completion_tokens=900 截断**（发言侧为聊天设的默认值，world_support 共用）→ 长事件草稿截成 invalid_json → 连续 5 pass 全失败。修复：world_support（life 背景）独立模型实例 max_completion_tokens=4096（semantic_chat_composition）
2. **manifest sidecar 超 12k 上限崩溃**（世界状态累积后 manifest 序列化超限）→ 生态 pass 崩溃。修复：sidecar 写入降级（完整 manifest 已在审计 payload，sidecar 是冗余副本）

**验证结果（对照 design §9）**：
- ✅ wake/availability/catalog、RandomAuthority draw、NPC 生态（NpcRegistered/StateChanged 真实产出）、occurrence→settlement→experience（28 个 settled）
- ⚠️ activity_lifecycle：依赖 live plan（character_choice 路径——模型采样中总选 world_contingency，未出现）
- ⚠️ VisualFact：lane 在跑但 idle（无带视觉证据的事件产出；record_visual_fact seam 仍无调用者）
- ✅ 防幻觉审查工作：模型编造 ref 被正确拦截（unsupported_claim_source）
- **未验证**：Appraisal/Affect 消费（background driver 不在生态 pass 内）、experience memory 写入（脚本 capsule 装配不完整）

**修复后**：连续 pass 正常（occurrence_committed/no_op/state_advanced 交替），不再崩溃。300 测试全过，生产已重启。

**评价消费调查（闭环最后一环）**：生产 27 个 npc_world_appraisal trigger 全部 terminal 且 **0 次模型调用**——no-change 是**断链期间（8月4-6日）系统自动完成**，不是角色选择（角色对"书店收到旧友旧书箱"这类有内容事件无反应不合常理）。断链修复后尚无新事件结算可验证。评价链路装配确认完整（outcome_acceptance 原子开 trigger → runtime background drain → world_stimulus → experience → 模型）。**待生产自然验证**：用户消息触发生态 → 新事件结算 → 评价消费是否恢复（观察 AppraisalAccepted/AffectEpisodeOpened 是否随新结算出现）。

**评价链路 wire 修复（主动触发验证）**：副本装配 world_stimulus（投影绑定+authority handler+emotion worker）复现，发现模型输出 5 个 wire 问题（全是契约不清晰/序列化差异）：
1. meaning_candidates 输出字符串（契约说 "free-text interpretations" 但 wire 要对象）→ 契约文本明确对象结构
2. decision 语义字符串复制到外层 wire（status=transition + decision 字符串冲突）→ wire 归一化删除字符串 decision
3. brief_rationale 输出成 "rationale"（字段名混淆）→ wire 别名容忍
4. expiry 输出 ISO 字符串（strict 要 datetime 对象）→ wire 解析容忍
5. no_change 携带 appraisal 字段（模型想表达"有感受但不改变"）→ 契约强化 null 约束
**结果**：评价链路从"0 模型调用自动 no-change"→ **模型真实调用 + wire 通过 + 合法选择**（采样 2/6 no_change、2/6 wire 失败——模型输出质量仍是主要失败源）。另加 world_stimulus/core 失败日志（生产诊断）。212 测试过（16 个失败是发言侧 deliberation.py WIP NameError，非本改动）。

**信封归一化（生产实证打通闭环）**：生产 detail 日志显示模型把 typed proposal 直接当整个输出（无外层 status/proposals 信封）→ wire 归一化自动包信封（status 从 decision 推导、attention 绑定 capability 源）。**生产真实产出 AppraisalAccepted + AffectEpisodeUpdated（warmth +42）——"事件→主角评价→情绪"闭环首次实证**。回归测试 test_bare_world_stimulus_proposal_is_wrapped_into_the_outer_envelope。

**成功率提升（0/8 → 8/8）**：采样 8 次全失败复现，三大修复：① **flash max_completion_tokens 900→4096**（评价输出超 900 tokens 截断成 not_json 是最大失败源；聊天回复远小于 900 无影响）；② **契约加完整示例**（proposal_example_activate/no_change——模型照抄结构，比散文描述稳定得多）；③ decision 缺失从内容推导 + proposal 内 source_refs echo 删除。**经验：散文契约对 flash 约束弱，完整示例是最大杠杆**。451 测试过。

**life_development 同款提升（最终 8/8）**：完整示例 + later 窗口强化（opens_at > 当前时间）+ ① 纠正温度 0.6→0.3（确定性修复）；② **位置窗口降级**（unsupported_location_window → 清空位置保留事件主干——无位置事件胜过无事件）。最终采样 8/8（含 1 次降级）。125 测试全过（2 个旧测试更新为降级行为）。**剩余**：biographical/visual 深层字段仍可能失败（模型长结构交叉约束），有重试兜底。

## 6.16 根源设计与长因果联动（2026-08-08 架构共识，用户拍板）

**架构意图**：不写剧情，造根源 + 接因果。每个模块独立成立（世界发生/记忆/反思/愿望/选择/行动），长因果链从联动中自然涌现（"家道中落→想很多→决定打工"不靠剧本，靠根源与连接）。**角色自己思考**（反思/愿望/选择是模型决定，机制只提供机会）。

**完整设计现已并入 `docs/design/girl-agent-design-intent.md` §11**。要点：
- **三新根源**（内心侧，契约已有实现缺失）：反思（Reflection）、愿望形成（Aspiration Formation）、人生选择（Character Choice）
- **成本约束 ¥100/月**：聊天 ¥60-70 + 事件机 ¥15-20 + 新根源 ¥10-15 + 媒体 ¥5-10
- **优化必须尝试**：本地模型重新评估（反思/愿望低频非实时无严格 schema，与失败过的本地审查不同，值得实测）；本地 embedding（bge-m3，语义召回地基）；纯算法（事件重要性排序/反思队列/愿望触发判定——只在该调用时调模型）；频率硬上限（反思每事件≤2、愿望每天≤1、选择每周≤3）
- **实施顺序**：记忆固化 → 反思 → 愿望 → 选择 → 计划行动
- **分工**：事件机负责"世界发生+可选机会"；内心侧（含新根源）属于统一角色内心领域；记忆/表达是既有模块的接通

1. **生产路线**：NapCat（`scripts/run_napcat_adapter.sh`），生产账本 `var/world_v2.sqlite`
2. **优先级**：失声第一（先压故障失声，再通设计内沉默）
3. **改动纪律**：大改动前必须备份（数据 + 代码可回退）；测试聊天用**生产账本的副本**，不污染真实生活
4. **验收方式**：不靠用户真人验收，也不只靠自动化脚本——**Claude Code 自己用终端脚本和角色聊几个来回**，发现问题→修复→再聊，迭代循环
5. **成本约束**：待观察，但来源闭包从普通聊天路径移出是允许的方向（以失声/延迟收益为准）
6. **改动幅度**：允许结构性重构（声音层大文件可动），前提是备份与逐次自验

## 6.14 聊天主路径延迟重构：审查退役 + 一次做好（2026-08-07 下午）

**用户拍板（硬指标）**：p50 1-2s / p95 2.5s；主模型云端 DeepSeek flash；审查/计算模型本地（实测不可行）；"一次做好"= 生产一次调用输出合规结果，不依赖审查；不用异步。

**实测决定架构（本地兜底模型全部不可靠）**：
- 1.7B（MLX Qwen3-1.7B-4bit）：V8 契约下**回显输入**（指令跟随太弱）
- 7B（Qwen2.5-7B-4bit）：V8 契约 4.9s 且 **5/6 语义维度全漏判**；极简任务 0.7-1.1s 但反向误拒合法 claim
- 结论：语义审查无可用本地模型 → **审查从生产路径整体退役**（用户拍板"纯确定性"）

**落地改动**：
- [x] `.env`：`WORLD_V2_SOURCE_REVIEW_REDUNDANCY_ENABLED=false` + `WORLD_V2_SOURCE_INVENTORY_ENABLED=false`（审查双关）
- [x] semantic_chat_composition.py：审查从硬启动门改显式部署能力（redundancy=false 时 reviewer=None，纯确定性）
- [x] inbound_wire.py 入口 `review_expression_with_candidate_external_coverage`：inventory=None 分支从全量 V7 改 **V8 declared-claims-only**（无 claims 零调用确定性 pass；代码保留供显式启用）
- [x] 降级容错：本地端点宕机/审查尝试耗尽 → 无 claims 一律 unaudited pass（`SourceReviewAttemptsExhausted` 加入 catch）
- [x] **head 截断根因查明**：生产 prompt 引导 `character-interior-events.1` 协议但 DeepSeek 输出双字段信封（appraisal+expression）→ 扁平解析器无法增量 → **head 提前从未生效**（里程碑总在流完成时）；新增 `_incremental_combined_envelope_first_expression` 嵌套增量解析 + 放宽 world_claims==[] 截断条件（审查退役后无存在理由）
- [x] **TTFT 实测**：DeepSeek flash 0.44-0.59s（9-21k 字符输入）——prefill 不是瓶颈；**瓶颈是输出生成**（~500 tokens 完整 JSON ≈ 3-4.5s）
- [x] 主模型加 `max_completion_tokens=900`（此前无输出上限）；appraisal 契约限长（brief_rationale/behavior_tendency/display_strategy ≤120、meanings 1-2 个 ≤64 字符）
- [x] **"一次做好"闭式约束强化**：主指令加"没有第二道审查，未声明的外部命题会被直接丢弃，声明后再断言"；stream 段加 beats-last + timing_choice=now 序列化硬话；shape_contract beats 最后改硬条款
- [x] 输入瘦身：snapshot 材料限数（recent_dialogue 8→4、relevant_facts 6→3、remembered/emotional 2→1）
- [x] 批处理：coalescing 窗口**回 280ms**（160ms 拆散多气泡连发，违背北极星#3；quiet gap 收缩 0.15→0.10 保留）
- [x] 预算重调：total 18→12、validation_recovery 46→8、validation_reselection 100→20（审查退役后不再需要长链）
- [x] 部署：text-endpoint（8188）装 launchd + watchdog；bge-m3（8190）launchd plist 建好但**模型需 MLX safetensors 未就绪**（缓存是 PyTorch 版，搁置）；清死 job（local-appraisal/watchdog）
- [x] 测试：删除 90+ 个审查时代规格用例（V7/RR/reselect 链，git 未跟踪的无法恢复已备份 tmp/），新增 V8 lane 专项测试（零调用/支持/拒绝/机械不可洗白/failover）+ 组合信封增量截断测试；**全量 4250 通过**

**实测结果（chat_with_world_v2 --clone 自聊）**：
- 审查开启（对照组）：成功 ~4.7s、失败 5-11.3s、成功率 ~20-40%
- 审查退役后：成功 4.8-6.3s、**burst 成功率 87.5%、单条 62.5%**（此前 40-60%）
- 失声残余主因：**主调用 4-5s 不稳定**（DeepSeek 输出生成波动，6s 超时线边缘）

**剩余差距（1-2s 目标的路径）**：
1. **输出生成 3-4.5s 是主瓶颈**（TTFT 仅 0.5s）：下一步 = 输出 token 继续压缩（appraisal 已限长、expression 字段精简、max_tokens 收紧）+ 评估 head 提前的真实收益（text 最后生成，当前提前≈0）
2. **主调用失败波动**：6s 超时线 vs 模型波动——需实测失败分布（超时 vs wire 校验）
3. **预算门未设**：MONTHLY_BUDGET_CNY/DAILY_BUDGET_CNY 全空（成本不可见，roadmap §6.9 遗留）
4. 生产账本路径注意：**实际是 `data/companion.sqlite`**（180MB，活跃），`var/world_v2.sqlite` 是 0 字节旧路径（文档多处仍写 var/ 需更正）

## 6.15 失败根因实盘 + 剥离降级 + head 回滚（2026-08-07 傍晚续）

**失败分布实测**（10-30 轮自聊）：成功率 30-87% 大幅波动。内层异常链终于抓到（此前只看到 role_faculty 包装）：

**完整失败链（主因）**：
1. **模型输出 wire 不合规**（`combined expression failed its exact contract ... error_type=ValidationError`，DeepSeek flash 高频违反复杂契约）
2. → **纠错重跑**（world-claim corrective retry）修复 draft
3. → **`ValueError: physical provider terminal is not bound to its stream tail`**（proposal_audit.py:491）——**既有 bug**：纠错后的新 model_call 不在原 stream 的 physical_provider_audits.semantic_model_call_ids 里 → strict revalidation 抛 → **失声**

**次要失败**：role_faculty（模型调用超时/异常，约 1/3）；编造 ref（proposal evidence authority absent）。

**本轮落地**：
- [x] **确定性剥离降级**（expression_draft.py `strip_unpinned_world_claims`）：模型编造 ref 的 claim 被确定性剥离（不重跑模型），**回复保住、编造事实不进账本**。wire 主路径 `strip_unpinned_claims=True`。8 个"编造 ref 拒绝"测试更新为"剥离通过"语义
- [x] **head 截断放宽回滚**：world_claims 非空时提前释放 head → 物理流 tail 审计绑定失败（proposal_audit:491）→ 回滚 `world_claims==[]` 条件（head 提前收益≈0，text 最后生成）
- [x] 组合信封嵌套解析器保留（claims 空时安全提前，测试覆盖）
- [x] 诊断日志增强：deliberation candidate raised 打异常链（exc_info）；core.py role faculty 打内层异常（exc_info）——下次失败可直接定位
- [x] 全量 4252 通过

**剩余差距（下一步主战场，按优先级）**：
1. **修 tail 绑定 bug**（proposal_audit.py:491）：纠错路径的 physical provider audit 必须包含纠错后的 model_call（inbound_author.py:1625-1650 的 PhysicalProviderInvocationAudit.semantic_model_call_ids 只含原 stream head/tail）——修复后纠错成功即恢复，成功率应大幅提升
2. **降低模型 wire 不合规率**：DeepSeek flash 高频违反契约（纠错成为常态路径）——契约精简/输出限长继续
3. 预算门仍未设（MONTHLY_BUDGET_CNY 空）

## 6.16 失声修复第二轮：双层剥离 + trigger 事件绑定（2026-08-07 深夜）

**成功率从 30-40% 提升到 60-67%**（10-16 轮自聊实测）。本轮修复的三个失声源：

1. **编造 ref（expression 层）**：`strip_unpinned_world_claims`（§6.15）——闭集越权的 claim 剥离保回复
2. **unbound evidence（proposal 层）**：模型引用"合法格式但 Capsule 未绑定"的 ref（如 observation trigger 事件 vs clock tick 事件）——expression_draft 闭集检查看不到 → deliberation `_validated_proposal` 证据核对时收集 unbound refs → `_strip_unbound_expression_evidence` 剥离对应 claims 保回复（非 expression 的 decision/其他 proposal 保持 fail-closed）
3. **trigger observation 事件未绑定**：decision proposal（appraisal/affect）的 evidence 引用 `event:trigger:observation:...`（观察事件级），Capsule 只绑定 clock tick 事件 → `_validated_proposal` 把 `trigger_message.event_ref`（观察事件）加入 bindings_by_ref（与 observation 级 ref 同 authority）——**修后 decision 失声大幅减少**

**剩余失败（最终瓶颈 = 模型 wire 不合规率）**：
- `canonical stream ExpressionDraft requires a beats array`（模型输出缺 beats，整个 draft 无效，剥离救不了）
- `failed its exact contract`（ValidationError）→ 纠错 → 纠错输出也不合规（`authored_expression_reselection_invalid`）
- **DeepSeek flash 对 12 字段复杂契约的遵守率是硬瓶颈**——下一步：简化契约（简单消息走 MinimalReply 单文本契约）/ 输出限长继续

**诊断增强**（保留）：deliberation candidate raised 打异常链（exc_info）；core.py role faculty 打内层；proposal_audit strict revalidation 打内层——下次失败直接定位。

## 6.17 契约简化尝试：显式字段放宽（2026-08-08 凌晨）

**wire 不合规根因实锤**：模型省略**有默认值的字段**（cadence/confidence/timing_choice）→ `require_explicit_authored_decision_fields` 要求显式输出全部决策字段 → `authored ExpressionDraft is missing explicit fields: cadence` → 纠错 → 失声。模型输出本身合法（默认值就是为省略设计的）——显式字段要求太严。

**放宽**（inbound_wire.py `_require_explicit_authored_expression_fields`）：
- 保留显式：beats/stance/brief_rationale（说什么/怎么回应/为什么——无合理默认）
- 放宽默认：timing_choice（now 最常见）、confidence（5000 中性）、cadence（仅 recorded_cadence_mode=="on" 时要求）
- turn_posture 在 turn_attention_advisory 时仍显式（打断体验）

**效果**：成功率波动 60-75%（自聊实测；此前 30-40%）。剩余失败全是**模型输出质量**：`immediate expression event head requires one visible beat`（typing-only head）、`failed its exact contract`（ValidationError）、`appraisal_reselection_invalid`（纠错也失败）——DeepSeek flash 对复杂契约的遵守是最终瓶颈。

**诊断增强**：failed its exact contract 日志改打 ValidationError 的字段路径（loc:type，不含模型文本——unsafe_shape 测试要求模型文本不进日志）。

**下一步候选**：主调用契约继续简化（模型输出最小字段集）/ 换成更稳的模型 / 简单消息直接走 MinimalReply 形状。

## 6.18 简化形状尝试：路径打通但模型自选不稳定（2026-08-08 凌晨）

**落地**（用户拍板方向）：
- `expression_draft_shape_contract` 引导：单文本无事实回复可用**紧凑形状**（response_text + stance 三选一 + brief_rationale + confidence + private_turn_state），信封（appraisal_draft + expression_draft 双对象）不变
- `_canonical_stream_partition` 宽容：无 beats（紧凑形状）→ head=完整形状 + 空 tail（complete_without_more），不再抛 "requires a beats array"
- 紧凑形状物化路径（response_text → MinimalReply）已存在，直接复用

**成本安全**：零新增调用（同一主调用，prompt 多一个选项 + 解析宽容）。模型采纳紧凑形状时输出 token 从 ~500 → ~150（省 ~70% 输出成本）；不采纳时与之前相同。**无成本爆炸风险**。

**实测**：成功率 25-75% 波动（模型自选不稳定）。失败两类：
- `yield posture cannot authorize an immediate expression`（模型误用 turn_posture=yield + timing now）
- `combined cognition must contain exactly appraisal_draft and expression_draft`（模型把信封也简化了）

**结论**：模型二选一不可靠（自选形状 → 混用/误用 → 失声）。**更可靠方案 = 机制分流**：用本地话轮探测（已存在，8188）判断"简单消息"→ 简单消息**强制紧凑契约**（prompt 只给紧凑形状）、复杂消息完整契约——不分流不新增调用，比模型自选稳。

## 6.19 简化形状尝试的结论与回滚（2026-08-08）

**三轮尝试全部不稳定**（模型对"紧凑形状"理解不可靠）：
1. prompt 自选紧凑形状 → 模型混用（缺字段/信封简化）→ 25-75% 波动
2. 措辞修正（信封不变）→ yield 误用持续
3. 机制 hint（本地 endpoint 加 compact_reply_hint_bp，零新增调用）→ 模型省略更多字段（更乱）

**回滚**：shape_contract 紧凑选项 + advisory hint 引导撤回。**保留**：流解析宽容（无 beats 不抛，防 "requires a beats array" 失声，独立价值）+ 显式字段放宽（60-75% 基线）+ text_endpoint compact 字段（无害，本地 prompt 输出多一字段）。

**结论**：模型对输出形状的自选/引导都不可靠——**DeepSeek flash 的输出质量（契约遵守）是最终瓶颈**，不是形状设计问题。成功率基线 60-75%（显式字段放宽后）。成本无爆炸（零新增调用）。

**注意**：test_life_development_runtime.py 有并行会话 1941 行 WIP（test_source_closure_rejects_clock_backstory 失败是他们的进行中状态）。

## 6.20 偷师 reasonix：repair 确定性 + 反例提示（2026-08-08，成功率 80%）

**reasonix 调研**（DeepSeek 专用编程 Agent，esengine/DeepSeek-Reasonix）：核心是 Tool-Call Repair——畸形 JSON 修复、重试时回显 schema（形状错误给 schema、值错误给真实错误）、schema 规范化、低温度确定性修复、缓存优先（99.82% 前缀命中）。

**对照我们**：repair 链已具备大部分（精确 violation 回显 + shape_contract 全文 + include_invalid_raw 上次输出回显 + 形状/值错误分类）。**差距两个**：
1. **repair 温度 0.7 → 0.2**：修复任务要确定性（reasonix 模式），高温度修复也随机错（此前 repair 重跑仍错的主因之一）
2. **主 prompt 反例**："yield requires later or silent; never pair yield with now"（此前 yield+now 是高频失败）

**实测**：自聊 10 轮 **8/10（80%）**（此前 60-75% 基线）。成功回合 0 次 repair（主调用直接过——yield 反例让主调用少犯错误）；repair 温度改进提升失败时的修复成功率。剩余失败：yield+now 偶发 + 信封结构偶发（模型行为）。

**成本**：零新增调用；反例是静态 prompt（缓存命中免费）；repair 温度不增成本。无爆炸。

## 6.21 接近 100%：信封 repair（2026-08-08，15/15 自聊成功）

**用户痛点**：80% 意味着每 5 条静默 1 条，体感不可接受——聊天必须接近 100%。

**缺口找到**：信封结构错误（`combined cognition must contain exactly appraisal_draft and expression_draft`）在**解析层**失败 → 直接 role_faculty → **没有 repair 入口**（`_parse_combined` 失败只记录不恢复）。

**修复**（inbound_author.py）：`_parse_combined` 失败 → **信封 repair**（低温度 0.2，回显精确 violation："Return exactly one complete JSON object with exactly the two keys appraisal_draft and expression_draft"——reasonix 模式）→ 重解析 → 成功继续。修复也失败才 terminal。

**实测**：15/15 自聊全成功（run 1-9 + 13-18；run 10-12 是并行会话 WIP 的 ReflectionScheduler 破坏，非本改动）。三层防线：主调用 → materialize repair（0.2 温度）→ 信封 repair（0.2 温度）。

**成本**：信封 repair 只在信封错误时触发（罕见），+1 调用（失败路径，~¥0.04）——可接受。

**注意**：并行会话活跃 WIP（reflection/life_reflection process_kind 枚举未更新 → 全量 7 个测试失败 + 自聊间歇 NameError）——他们的领域，未修避免冲突。

## 6.22 生产修复 + 全绿（2026-08-08）

**并行会话 WIP 破坏生产**：`life_reflection` process_kind 加了但 vertical_registry 未注册 → 启动门拒绝 → capture 初始化失败 → 生产 down。**补全**（完成并行会话的半成品，恢复生产）：
- vertical_registry.py：life_reflection 注册行（照 npc_world_appraisal 模式）
- reducers.py：claimed/opened/lease 三处白名单 + `_trigger_process_opened` 的 life_reflection 校验（source=AppraisalAccepted）
- event_identity.py：claimed identity 白名单

**appraisal repair 温度 0.7→0.2**（expression 已改，appraisal 漏了——`appraisal_reselection_invalid` 是 repair 高温度重犯）——修复后 7/10（appraisal repair 救回）。

**测试更新**：2 个"失败审计"测试——我的信封 repair 让场景不再失败（正效应），role model 改永远无效保持失败审计语义。

**最终**：全量 4256 全过（0 失败）。生产恢复。

**剩余（接近 100% 的最后一块）**：repair 全失败（corrective 槽耗尽/修复无效）仍失声（10 轮 2-3 失败）——**本地模型兜底**（8188 免费，repair 失败 → 本地 1.7B 单文本 → MinimalReply）是下轮主任务（实现复杂：模型注入+物化+审计，建议单独会话）。

## 6.22 事件机：评价链路 wire 实修 + World Author 引导（2026-08-08 上午）

**背景**：生产因并行会话 reflection WIP 的启动门从 02:00 停摆 ~9h（10:04 重启失败，10:28-10:54 并行会话补齐 4 处配套后启动门通过，10:55 生产恢复）。本会话未碰 reflection 领域，全部改动在事件机/评价链路。

**副本触发采样抓到 3 个真实 bug**（tmp/trigger_life_development.py 装配修好：SQLiteWorldLedger 注入 accepted_batch_issuer，此前评价接受链直接抛 recorder_capability_required）：
1. **expiry 字符串/过期**（appraisal_proposal_compiler._expiry）：模型输出 ISO 字符串 expiry → `str <= datetime` TypeError；输出过期时间 → expiry_not_future 抛错 → 评价链路断（stimulus drain technical_failure，评价永不落地）。修复：字符串 fromisoformat 容忍 + 过期钳制到默认窗口（at+2h，技术失败≠no_change 原则）。
2. **affect target_revision_stale**（affect_proposal_compiler）：模型同一次提议里 appraisal 打开新 episode（revision 0→1）+ affect update 旧 revision → 编译抛错 → **穿透 world_stimulus → runtime → host tick，无兜底**（qq_c2c_host 1869 无 try/except，打崩整个 tick/消息处理）。修复两层：编译器降级（target_revision_stale → no_change + skip_reason，§6.12 episode_id 降级同款，merge_target_ambiguous 已有模板）+ world_stimulus emotion 段 catch (ConcurrencyConflict, ValueError) → appraisal-only 结算（防御其他 ValueError 穿透）。
3. **resume 无限捞**（发现未修）：terminal trigger 的 affect 编译失败后 `_affect_is_pending` 永远 True → _next_process resume 无限捞同一 trigger（每 tick 一次本地编译，无模型调用、不阻塞非 terminal trigger——与 merge_target_ambiguous 既有行为同款）。stale 降级后该问题消失（编译成功→accepted 落账）。

**World Author 引导**（§4.4 事件机侧配套）：prompt 类别空间强化——"环境小事（雨/天气）是低成本默认，别让它挤掉移动生活的提议（机会/困难/关系变化/长期处境）；character_choice 用于她可选择的机会"。**采样效果**：4 次采样 2/4 非环境类（诗歌 open-mic 邀请、社区 flyer 活动）、**character_choice 首次出现（1/4）**（此前采样全是 world_contingency 环境小事）。零新增调用。

**验证**：280（affect/appraisal/stimulus）+ 214（life）测试全过。生产重启后生效（本会话改动含 prompt，需 napcat 重启）。

## 6.23 主调用工具化：function calling，15/15 自聊 100%（2026-08-08）

**根因**：让 DeepSeek 手写大 JSON 信封（appraisal+expression 双对象）是可靠性低的根源。DeepSeek 平台的结构化标准做法 = **function calling**（API 强制参数 schema，模型只填字段值）。

**落地**：
- llm.py：DeepSeekChatModel/OpenAICompatibleChatModel 支持 tools/tool_choice（tools 时不再发 response_format，二者互斥）；SSE 流式累积 tool_calls arguments 增量；非流式 content 空取 arguments；response_hash 基于 arguments
- inbound_author.py：主调用用强制 `combined_cognition` 工具（单信封工具，参数=AppraisalDraft+ExpressionDraft 契约 schema）；arguments=信封 JSON → 现有 _parse_combined 链不变；假模型 TypeError 回退兼容
- 流式：_unit_stream_result 透传 tools；head=完整信封（等 arguments，放弃 head 提前截断——收益≈0）、tail=空
- **修复**：retirement 分支（repair 后候选是独立调用，不带 stream physical——tail 绑定审计不再误伤）；appraise schema 补 components/episode_id/resolution_summary（affect=open 时模型有输出途径）
- StructuredSourceReviewModel/StructuredExpressionReselectionModel 的 request_payload 同步 tools 透传

**实测**：简单消息 10/10 + 多样化 5/5（带事实/推理小说/记忆/做饭）= **15/15 100%**（此前 70-80%）。回复文风自然贴合。probe 信封工具 5/5 一次通过（无 repair）。

**成本**：同一主调用（1 次），无新增调用。工具 schema 使输出更短（模型只填值）→ 输出 token 略降。

**全量**：4255 过（1 flaky：test_appraisal_settlement_contention 全量时序污染，单跑过——并行 WIP 活跃）。

**剩余**：repair 槽 1 次上限（主调用失败 + 双阶段 repair 时偶发槽耗尽）——工具化后主调用失败率大降，触发罕见。

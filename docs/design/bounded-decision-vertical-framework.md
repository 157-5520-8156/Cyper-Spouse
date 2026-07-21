# 有界决策竖井框架（BoundedDecisionVertical）——精简战役核心议案

日期：2026-07-20 ｜ 状态：已批准（2026-07-20） ｜ 范围：纯设计，零代码改动

## 0. 所有者决议（2026-07-20，对第 5 节五个开放问题的裁定）

1. **影子对照语料**：必须包含生产账本副本（`cp data/companion.sqlite` 出副本使用，
   原库只读；全程本地，不出机器）。scenario corpus + 生产副本共同构成 0 字节
   差异验收语料。
2. **目录条目级版本（3.6 节）**：后置独立波。本波**不改变任何身份材料**——这是
   试点字节等价证明的前提。
3. **框架版本不进入事件身份材料**：身份只由领域语义决定，框架演进靠金账本回归
   守护；接受"框架 bug 修复可能改行为而身份字节不变"的审计口径。
4. **硬门**：vertical_registry 登记行对所有竖井强制（含 `hand_rolled=True` 的
   手写井）；架构测试守护"新 process_kind 必须有 registry 行、非 hand_rolled 的
   必须走框架"。实现方式自由，逃生舱（手写井）保留。
5. **试点收尾**：证明通过后框架版接管生产（组合根开关，默认框架版），保留环境
   变量级热回退指向旧实现；旧实现文件保留在树里一个发布周期，之后由协调者删除。

## 1. 问题陈述

World v2 的每一类自主行为（下称"竖井"）都在重复同一套约 500 行的仪式：
触发器身份推导（`batch_invariants.py` 里一族 `*_trigger_identity` 函数）、
确定性 opener、runtime 消费者（claim / lease / CAS / 崩溃恢复）、bounded
模型合同（严格 JSON + 至多一次纠错重试）、提案物化、语法登记
（`production_proposal_grammar.py` 的闭合目录）、受理缝、`schemas.py` 的
`process_kind` 枚举、`reducers.py` 校验白名单、`event_identity.py` 身份白名单。

实证成本（均可在仓库中核对）：

- **贴表情竖井触及 ≥10 个文件**：`quick_reaction.py`（955 行）之外，`"reaction"`
  动作种类出现在 15 个生产源文件（语法目录、能力目录、表达合同、平台执行器、
  QQ transport、组合根、runtime、ingress 策略等），另有 919 行测试。
- **29 个触发器挂死**：组合根少组装一个 reviewer——
  `production_turn_application.py:2571-2581` 的注释记录了根因（"a gate without
  a reviewer is never claimed…Opened-only backlog"），提交 `c986c73d` 的
  "drains 29 stalled gates" 记录了清淤。竖井机械分散在组合根 3 736 行里，
  一处漏配就是一类行为整体静默。
- **六个媒体断点**：`docs/audits/world-v2-media-production-bring-up-2026-07-20.md`
  逐条记录六处"竖井建好了、没人接完线"的断点闭合过程。
- **一周内目录冻结 12 个版本**：`configs/world_seed.yaml:58` 现为
  `reviewed-life.12`，`tests/world_v2/test_life_event_richness_7day.py` 的注释
  串起 .4/.10/.11/.12 的演化——每加一个条目就整目录升版，测试钉住的版本号
  跟着churn（仓库中 `reviewed-life.11` 有 13 处引用）。

结论：仪式本身是对的（效果一次、重放不再求值、闭合语法），贵的是**每个竖井
手抄一遍**。本议案提出把仪式收进一个参数化深模块，竖井只声明语义。

## 2. 形状萃取：十个竖井的共性 / 差异矩阵

精读对象：`silence_appraisal_trigger(_runtime)`、`plan_disruption_appraisal_trigger(_runtime)`、
`private_impression_producer`、`quick_reaction`、`afterthought_author`、`npc_initiative`、
`aspiration_runtime`（含结晶）、`shared_private_invitation`、`future_life_author`、
`relationship_adjustment_trigger(_runtime/_worker)`。

### 2.1 三种既有生命周期形状

- **A. 事件锚点竖井**（durable `TriggerProcess`）：silence、plan_disruption、
  private_impression、relationship_adjustment、afterthought。锚点是一个已提交
  World Event；open → claim（租约）→ 工作 → complete，全部 CAS 提交，reducers
  强制"先 open 才能 claim、完成必须在租约窗口内"。
- **B. 时钟检查竖井**（durable 检查事件，无租约）：npc_initiative、aspiration、
  shared_private_invitation、future_life_author。锚点是 `ClockAdvanced` wake；
  身份编码"本地日期（+slot）"，当日每次 wake 收敛到同一持久结果；崩溃恢复从
  检查事件 payload 重建（`decision == "selected"` → 补提交）。
- **C. 同轮内联竖井**（一次性，无持久过程）：quick_reaction。在可见 ingest 轮内
  同步执行，用提案前缀在 audit 投影上去重，"任何失败一律静默、绝不留债"。

### 2.2 共性（被抄写的仪式），附代码坐标

1. `_digest`（canonical-JSON + sha256）：仅这十个竖井就有 **13 份副本**（11 份逐
   字节相同，quick/afterthought 的变体只多一个 `allow_nan=False`；如
   `silence_appraisal_trigger.py:37`、`aspiration_runtime.py:75`）。
2. 异步账本垫片 `_project/_project_at/_lookup/_commit/_commit_at_cursor`
   （`blocks_event_loop → asyncio.to_thread` 双分支）：silence runtime 483-519、
   plan runtime 525-561、quick 915-941、impression 731-779……每份 ~40 行。
3. `ProjectionCursor` 构造帮手 `_cursor/_cursor_from_commit`：所有文件各一份。
4. claim/reclaim（租约检查 → attempt_id 摘要 → `model_copy` → Claimed/Reclaimed
   事件 → CAS）：silence 343-397、plan 384-438、impression 620-683、
   afterthought 1162-1207、relationship 163-222，五份近乎同构。
5. complete（租约有效性 → 完成 payload → `commit_at_cursor`）：又是五份。
6. wake 精确性校验（`ClockAdvanced` + `clock_transition_history` 的 payload_hash
   与 computed_world_revision 绑定）：npc 199-217、aspiration 182-207、
   shared 188-212、future 164-181，四份同构（仅 reason_code 前缀不同）。
7. 检查事件的记录与崩溃恢复（`ProposalRecorded` + proposal_kind + draw ref +
   raw_output_hash；"selected" 恢复路径）：B 形状每家一套。
8. select/no_op 严格解析块（~20 行：bounded text → JSON → 键集合闭合 →
   token 回显校验）：npc 411-430、future 389-407、aspiration 1214-1233 与
   717-736（结晶）、shared 414-433，**五份**。
9. 单次模型调用包装成 `ModelResultAudit + DeliberationResult`：
   `quick_reaction.py:848-888` 与 `afterthought_author.py:1096-1135` 近乎同构。
10. 登记散点（新增一个 A 形状竖井要同步改的枚举/白名单）：
    `schemas.py:542-570` 与 580-605（两处）、`event_identity.py:523-546`、
    `reducers.py:7441-7455` 与 7490-7505（两处）、
    `production_proposal_grammar.py:433-445`（覆盖断言的手写清单）、
    `batch_invariants.py` 身份函数、`runtime.py` 构造参数与排水优先链、
    `production_turn_application.py` 组合、宿主（`qq_c2c_host.py`）。

### 2.3 差异矩阵（竖井真正的语义）

| 竖井 | 触发来源（事件/投影谓词） | 机会身份 | durable | 受控随机（权重来源） | 模型合同 | 提案目标 | 受理缝 | 恢复语义 | 版本冻结点 |
|---|---|---|---|---|---|---|---|---|---|
| silence_appraisal | 最新可见消息 `ExecutionReceiptRecorded` + 闲置阈值 + 无更新用户消息 | 每回执锚点一个 trigger | A | 无 | 完整 Deliberation（Capsule+期待 advisory） | `appraisal_transition/activate` | AppraisalProposalWorker → 下游开 affect/relationship 触发 | 租约到期重 claim；搁浅 audit 在现 cursor 重新 deliberate | trigger.1 / turn.1 |
| plan_disruption | 最新 `ActivityAbandoned` + 计划权威 join | 每弃置锚点一个 | A | 无 | 同上（+dropped-plan advisory） | 同上 | 同上 | 同上 | trigger.1 / turn.1 |
| private_impression | 最新未解读 `AppraisalAccepted` | 每 appraisal 一个 | A | 无 | 假设子集选择 JSON + **一次纠错重试**（99-125 行） | `private_impression_transition`（typed） | 自驱三事件：ProposalRecorded→AcceptanceRecorded+Accepted | 无效 draft 消耗机会；三事件各自 lookup 幂等 | draft.1 |
| quick_reaction | `ObservationRecorded`（ingest 内联） | 提案前缀按 observation 去重 | C | act/hold（情绪/当前活动/reaction 退避） | 闭合表情目录 JSON，0.8s 超时，全静默 | `expression_plan_transition` + `reaction` 意图 | derive_expression_plan_material → commit_accepted → 内联 pump + 私有 settlement | 崩溃即放弃；失败静默是合同 | context.1 / act-hold.1 / materialization.1 / gate.1 |
| afterthought | 最新已结算 reply 回执，12-240s 窗口谓词 | 每回执一个 trigger（runtime 自开） | A | act/hold（情绪/回复长度/深夜）+ mode×delay 联合网格 | afterthought JSON（mode 回显、≤120 字、重叠守卫），8s | `expression_plan_transition` + 带 due 窗口的 `followup` | 同表达受理缝；通用 ActionPump `not_before` 拥有派发 | held/declined 每回执终态；已有 audit 直接续 accept | context.1 / act-hold.1 / mode-delay.1 / gate.1 |
| npc_initiative | `ClockAdvanced` wake（每日 2 slot） | occurrence 按本地日期；check 按 slot | B | 事件+nothing（评审 base_chance × 情绪 × 每-NPC 关系，±40% 钳制） | select/no_op（故障不耗 slot） | WorldOccurrence commit+activate | OccurrenceContentCoordinator；结算归 LifeAftermathRuntime | check payload 恢复 commit+activate | weight.2 / policy.1 / 目录版本 |
| aspiration（含结晶） | wake（每日 1 check）+ 近期计划见证 | 种子 token 按日期；结晶 check 按 aspiration×日 | B | 种植 + reinforce/fade/crystallize 四类抽签 | select/no_op ×2（种植、结晶） | Aspiration* typed 事件；结晶=snapshot+ActivityPlanned+Crystallized 原子批 | 直接 typed 事件 | check 恢复；维护事件 lookup 幂等 | seed-weight.1 / maintenance.1 / crystallize.1 |
| shared_private_invitation | wake（每日 1 check）+ closeness 地板 + 至多一个 pending | plan 按 opening+opens_at；check 按日 | B | invite_chance 按重要度归一 + nothing | select/no_op | snapshot+ActivityPlanned（参与者=用户）；到期确定性 `ActivityAbandoned` | 直接事件 + advisory 只读视图 | check 恢复；绝不邀去过去 | weight.1 / policy.1 |
| future_life_author | wake（每日 1 成功计划） | plan 事件按本地日期 | B（决策事件） | importance × 邻近度 × 情绪（±35%） | select/no_op | snapshot+ActivityPlanned | 直接事件 | `LifeAuthorDecisionRecorded` 重放；plan 身份冲突报错 | weight.1 |
| relationship_adjustment | `RelationshipSignalAccepted`（未消费信号） | 每信号一个 trigger | A | 无 | **无模型**（确定性编译器） | relationship_adjustment typed 提案 | 编译器 → AdjustmentAcceptanceRuntime | pending 提案复用 | 受理 manifest 版本 |

矩阵读法：**列是框架参数，行内容是竖井保留的全部语义**。十个竖井没有一个在
"生命周期机械"上真正不同——差异全部落在触发谓词、权重编译、模型合同与受理绑定四类。

## 3. 框架设计

### 3.1 边界：框架吃掉什么 / 竖井保留什么

| 框架吃掉（仪式，一次实现） | 竖井保留（语义，逐井声明） |
|---|---|
| 三种生命周期引擎：`AnchoredTriggerLifecycle`（open/claim/lease/complete/reclaim，事件形状与 reducers 现约束逐字兼容）、`DailyCheckLifecycle`（wake 校验、日期/slot 身份、check 记录与恢复）、`InlineOnceLifecycle`（audit 前缀去重、静默失败合同） | 资格谓词：`(Projection) -> Opportunity \| None`，纯函数，克制规则（退避、地平线、pending 检查、closeness 地板）全在这里 |
| 身份推导：event_id / commit_id / attempt_id / proposal_id 的模板化生成，字符串形状按竖井冻结（试点须字节相等） | 权重编译器：`(Projection, ...) -> {candidate: bp} + reason_codes`，情绪/关系/活动如何倾斜 |
| 抽签记录与重放：attempt 身份绑定已编译 profile，调 `RandomAuthority`，重放收敛 | 提示词与 JSON 合同 schema（含解析器） |
| 模型调用骨架：超时、canonical 消息、严格解析、三种已安装失败策略（可重试异常 / 静默拒绝 / 一次纠错重试） | 提案编译目标：`(Opportunity, verdict, draws) -> DecisionProposal` 或 typed 事件批 |
| 审计：单次调用包装 `ModelResultAudit + DeliberationResult`，或整段接 `Deliberation` | 受理绑定：指名一条既有受理缝（appraisal worker / 表达受理 / typed 自驱 / 领域事件批） |
| 注册断言自动登记：`vertical_registry.py` 声明表 + 执行期断言（见 3.4） | 下游触发（如 appraisal 受理后开 affect/relationship）、语法 lane 的权威声明 |

框架**不**吃：`production_proposal_grammar` 的 lane 定义（权威）、`process_kind`
Literal 枚举（权威、重放冻结）、新平台动作种类的 transport/能力目录（那是真实
新能力，不是仪式）。

### 3.2 VerticalSpec 声明（假想代码）

新模块 `world_v2/bounded_decision_vertical.py`（≈800-1200 行 + 测试）。核心是一个
冻结声明加若干可组合步骤，**不是固定管线**——aspiration 的四类维护抽签这类形状
只取生命周期与抽签件，不必硬塞进"一井一决策"：

```python
# bounded_decision_vertical.py (proposed shapes, names frozen after pilot proof)

@dataclass(frozen=True, slots=True)
class VerticalSpec:
    lane_id: str                        # registry key; also grammar lane when LLM-backed
    lifecycle: AnchoredTriggerLifecycle | DailyCheckLifecycle | InlineOnceLifecycle
    identity: IdentityTemplates         # frozen "event:...:opened:{digest}" string shapes
    opportunity: OpportunityPredicate   # pure (Projection) -> Opportunity | None
    draws: tuple[DrawStep, ...]         # each = attempt template + weight compiler + catalog ver
    model: BoundedModelStep | DeliberationStep | None   # contract + parser + failure policy
    compile: ProposalCompiler           # -> DecisionProposal | tuple[WorldEvent, ...]
    acceptance: AcceptanceBinding       # names one existing seam, never a new authority
    downstream: DownstreamTriggers | None = None
```

### 3.3 用框架重写贴表情竖井：before / after

**Before（现状）**：`quick_reaction.py` 955 行。其中语义约 330 行（Policy ~13、
权重编译+抽签身份 ~100、gate 提示词+解析 ~100、提案物化 ~110），机械约 600 行
（去重、抽签调用、audit 记录、受理派生、CAS 提交、内联 pump、私有 settlement、
账本垫片、单次审计包装）。决策仪式另散落 `production_turn_application.py`、
`runtime.py`、`qq_c2c_host.py`；测试 919 行中过半在测机械。

**After（预期）**：

```python
# quick_reaction.py (framework edition), semantics unchanged byte-for-byte
QUICK_REACTION = VerticalSpec(
    lane_id="quick_reaction",
    lifecycle=InlineOnceLifecycle(
        dedupe=AuditPrefixDedupe(QUICK_REACTION_PROPOSAL_PREFIX),
        abandon_when=reply_already_delivered,      # projection predicate, kept as-is
        failure_contract="silent",                 # every failure gives up quietly
    ),
    identity=IdentityTemplates.frozen_from_current("quick-reaction"),
    opportunity=quick_reaction_opportunity,        # text/target/provider-binding checks
    draws=(DrawStep(
        attempt=quick_reaction_attempt_id,         # binds compiled profile, unchanged
        weights=QuickReactionContextPolicy,        # mood/activity/backoff, version .1
        catalog_version="quick-reaction-act-hold.1",
        proceed_only_on="act",
    ),),
    model=BoundedModelStep(
        messages=quick_reaction_gate_messages,     # closed catalog prompt, unchanged
        parse=parse_quick_reaction_verdict,
        timeout_seconds=0.8, failure_policy="decline_quietly",
        route=ModelRoute(tier="flash", reason_code="quick_reaction_gate",
                         router_version="quick-reaction.1"),
    ),
    compile=materialize_quick_reaction_proposal,   # unchanged
    acceptance=ExpressionAcceptance(dispatch="inline_pump_with_private_settlement"),
)
```

预期 **955 → ≈370 行**（声明 ~40 + 原语义 ~330），机械代码归零；组合根从手写
`QuickReactionWorker(...)` 构造收敛为 `install(QUICK_REACTION, deps)`。诚实说明：
贴表情当年 ≥10 个文件里约一半属于"新增 reaction 平台动作种类"（transport、能力
目录、payload 合同），那部分框架不吃也不该吃；对**复用既有动作/提案家族**的新
竖井（绝大多数内心竖井），预期触及文件 ≈3：spec、语法/枚举权威行、测试。

### 3.4 注册断言的自动登记

`process_kind`、claim 身份白名单、reducers 校验、语法覆盖清单**保持 Literal/硬
编码不变**——闭合目录是重放安全的根，不做动态注册。新增 `vertical_registry.py`：
每个 spec（含手写竖井）登记一行声明（lane_id、process_kind、身份模板、语法 lane、
排水位点）；一个 `assert_bounded_vertical_coverage()`（仿
`assert_production_proposal_grammar_coverage`，`production_proposal_grammar.py:421`）
在测试与组合根启动时逐点核对散点枚举与 registry 一致，**失败信息直接指名漏改的
文件**。"29 个触发器挂死"类事故从"上线后账本里发现 Opened-only 积压"提前到
"组合根拒绝启动并点名缺的 reviewer/owner"。

### 3.5 重放兼容策略

- **老竖井原样冻结，永不强迁。** 框架对账本与 reducers 完全不可见；authority 层
  不知道框架存在。
- **试点迁移 quick_reaction + afterthought_author，必须先证字节相等再切换：**
  1. 语料：`scenario_corpus` 确定性脚本世界 + 至少一份生产账本副本。重放从不
     再调模型（Model Result / RandomDrawRecorded 复用），故等价可判定。
  2. 影子回放：同一输入流分别驱动旧实现与框架实现（注入相同的已录模型应答与
     逻辑钟），对比每次提交的 `commit_request_hash`、每个事件的
     `canonical_event_json`（event_id、idempotency_key、payload 字节）以及终态
     投影 `semantic_hash`（复用 `ReplayEvaluator` 与 `ledger_prefix_proof`）。
  3. 崩溃矩阵：在每个提交边界注入中断，断言两实现收敛到相同终态字节
     （对齐现有 crash-recovery 测试纪律）。
  4. 验收 = 全语料 0 字节差异；切换经组合根开关灰度，保留一个发布周期热回退。

### 3.6 版本冻结粒度改进

**现状问题**：目录版本与 `catalog_hash` 被烘进候选 token、attempt 身份、check
payload、policy_refs（如 `aspiration_runtime.py:265-270`）。任何一个条目的增删改
→ 整目录升版 → 所有 clock 竖井当日 attempt 身份全变（部署当天重抽一次，违背
"同日收敛"）、所有钉版本的测试churn（`reviewed-life.11` 13 处引用）。一周 12 版
就是这么来的。

三案比较：

1. **规则条目级版本 + 目录聚合清单（推荐）**：每条 opening/seed 携带
   `entry_version` 与内容哈希 `entry_hash`；目录聚合清单 = 排序的
   `(id, entry_version, entry_hash)` 列表，`catalog_hash` 改为清单摘要（审阅边界
   仍是单一 artifact）。抽签/attempt 身份改绑**当日合格候选集的 (id, entry_hash)
   集合**而非整目录版本；policy_refs 记 `policy:life-author-entry:<id>.<ver>` +
   目录出处。利：无关条目增改不再扰动既有身份；测试改钉条目；当日重抽只在
   可能性空间真变时发生（诚实）。弊：身份材料变形状，各 clock 竖井需升一次
   attempt 合同版本，新旧抽签双轨永存（有 `sampler_version` 1/2 先例，
   `random_authority.py:70`）；清单构建与校验代码；删条目仍变当日身份（应该变）。
2. 版本继承默认值（目录 base + 条目覆写）：只治"编号疲劳"，不治身份扰动与测试
   churn，不推荐。
3. 仅把 catalog_version 从身份材料中拿掉、改绑候选集内容哈希：是方案 1 的真子集，
   省了条目级审阅出处，但下一次仍要再动身份材料，不如一次到位。

## 4. 落地计划

| 阶段 | 内容 | 验收标准 |
|---|---|---|
| P0 框架模块+测试 | `bounded_decision_vertical.py` + `vertical_registry.py` + 崩溃矩阵测试；不接生产 | 三种生命周期引擎对合成账本全崩溃点收敛；registry 断言对既有散点枚举 0 漂移 |
| P1 试点迁移 ×2 | quick_reaction、afterthought 框架化 + 影子回放 | 3.5 节全语料 0 字节差异；开关灰度、热回退在位 |
| P2 新竖井从此用框架 | 下一个真实新竖井（inner-life 覆盖计划中的任一）以 spec 落地 | 非框架代码 ≤~350 行、触及文件 ≤3、组合根启动断言点名一切漏配 |
| P3 老竖井永不强迁 | 写入 review 纪律；框架演进不得要求旧井改动 | 架构测试守护：旧井文件对框架模块零 import |

**风险登记**：

- *框架自身成为单点*：一个生命周期 bug 同时击中所有已迁竖井。缓解：试点先行、
  金账本回归语料进 CI、框架版本演进若触碰身份材料必须开新引擎版本且旧 spec 不
  自动跟随。
- *抽象错位*：某新形状塞不进 spec。逃生舱：竖井**永远可以退回手写**——框架是
  库不是门；手写井只需在 registry 登记 `hand_rolled=True`，断言只保证登记完整，
  不强制实现方式。
- *双实现漂移*（试点灰度期）：旧实现冻结只修安全缺陷；影子对比测试常驻直到删除。
- *断言假安全感*：registry 只能查"声明了什么"，查不出语义错误；语法/受理的
  权威校验仍以既有 grammar/manifest/reducers 为准，框架不复制它们。

**工作量预估**：P0 约 3-4 人日；P1 约 2-3 人日；P2 起每个新竖井预计净省 1-2 人日
（以贴表情实测 ~10 文件 / ~1900 行代码+测试为基线）；目录粒度改造独立 1-2 人日。
框架战役合计约 1.5 周内可交付到 P2。

## 5. 给所有者的开放问题

1. **影子对照语料范围**：字节等价证明是否必须包含生产账本副本（真实私聊数据在
   开发环境的使用需要你批准），还是 scenario corpus + 崩溃矩阵即可放行切换？
2. **目录条目级版本的时机**：与框架同波（各 clock 竖井的 attempt 合同版本只升
   一次）还是后置独立波（风险隔离，但要再付一次双轨）？这同时决定是否调整目录
   审阅流程（审"聚合清单"而非"整文件 diff"）。
3. **框架版本是否进入事件身份材料**：进 → 审计可直指"哪个引擎版本产的这条事件"，
   但框架每次演进都制造新旧身份双轨；不进（我的倾向）→ 身份与框架解耦、靠金
   账本回归守护，但要接受"框架 bug 修复可能改行为而身份字节不变"的审计口径。
4. **强制程度**：P2 的"新竖井必须用框架"要不要架构测试硬门（新 process_kind 必须
   有 registry 行，且非 hand_rolled 的必须走框架），还是仅作 review 纪律？硬门
   更防散点回潮，但与逃生舱的张力需要你定平衡点。
5. **试点收尾**：quick_reaction/afterthought 证明通过后，旧实现保留一个发布周期
   作热回退，还是当波删除（少一份双实现漂移面，多一分回退成本）？

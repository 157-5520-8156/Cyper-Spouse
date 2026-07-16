# World v2 Life Ecology：可验证的生活事件生产与媒体机会

状态：冻结施工合同（首个生产纵切前）  
日期：2026-07-16  
关联：[事件来源可达性审计](../audits/world-v2-event-ecology-source-audit-2026-07-16.md)、[World v2 总计划](../world-v2-refactor-plan.md)

## 1. 决策

新增 `LifeEcologyRuntime` 作为世界生活推进的深 Module。它以一次已提交的 durable wake 为唯一外部输入，内部完成候选空间编译、可选的模型审议、现有 authority 的 Proposal/Acceptance、已提交事件的后续 fan-out，最后把已提交的事实交给既有 `EventEcologyMediaCandidateRuntime` 开启 preview 候选。

它不是图片触发器、不是第二个 Ledger writer、不是定时器，也不把模型叙述当事实。`EventEcologyMediaCandidateRuntime` 继续只消费已提交的事实，绝不创造生活、环境、NPC 或图片内容。

```text
durable world/clock wake
  → LifeEcologyRuntime.advance_once
  → pinned ecology capsule + catalog + recorded draw（若已安装）
  → typed LifeProposal | no-op
  → existing proposal acceptance / authority reducers
  → committed life / observation / settlement authority
  → EventEcologyMediaCandidateRuntime.drain_once
  → source-bound PhotoCandidate + preview MediaOpportunity
```

这把“人有持续生活、可偶发偏离、但不能伪造事实”的复杂性集中在一个 Module 中。QQ、HTTP、CLI 和图片机不需要知道活动、NPC、VisualFact、cooldown、claim 或模型路由的内部细节。

## 2. 领域术语

| 术语 | 定义 | 不能被误认为 |
| --- | --- | --- |
| **Durable Wake** | 已提交且 hash/cursor 绑定的 `ClockAdvanced` 或 life authority event；一次生态推进的唯一合法来源 | 用户原文、图片 prompt、情绪分数、未接受模型输出 |
| **Life Ecology** | 从已验证的处境中提出下一项可能生活变化，并经 authority 接受的流程 | “自动发生器”或媒体策略 |
| **Life Opening** | 当前 snapshot 中经 catalog 验证、模型可以选择或拒绝的一种变化可能性 | 已发生的事件或必须执行的行为 |
| **Life Proposal** | 对一个或一笔显式合法变化的 typed candidate；其结构复用现有 proposal/acceptance registry | World Event、事实、Action receipt |
| **VisualFact** | 有效期、隐私和精确来源绑定的可观察环境/物品/餐食事实 | 用户 profile Fact、图片生成 prompt、模型想象 |
| **Ecology Availability** | 当前 world 中各生产 lane 的安装/激活/暂停/阻断状态 | 空结果或“世界很平静” |

`CONTEXT.md` 当前是用户未提交修改，不能安全地并入本轮 glossary 更新；本表是后续合并 glossary 的权威文字来源。

## 3. 外部 Interface 与可用性

平台唯一的生产 seam 由 `WorldV2TurnApplication` 暴露：

```python
async def advance_life_ecology_once(
    self,
    *,
    wake_event_ref: str,
    trace_id: str,
    correlation_id: str,
) -> LifeEcologyRunResult: ...
```

调用方不传地点、活动、NPC、候选事件、随机种子或媒体参数。它必须先持久化 wake；普通 inbound 不可直接调用。生产 scheduler 在成功 `tick()` 后调用一次；已接受 life mutation 的后续 worker 可使用自己的已提交 event ref。应用内部对外只保留这个入口，实际深 Module 的核心是：

```python
class LifeEcologyRuntime:
    async def advance_once(
        self,
        *,
        wake_event_ref: str,
        trace_id: str,
        correlation_id: str,
    ) -> LifeEcologyRunResult: ...

    def availability(self) -> LifeEcologyAvailability: ...
```

`LifeEcologyRunResult.status` 必须为下列之一：

```text
advanced | idle | joined_existing | deferred | unavailable | rejected | failed_safe
```

返回值只能提供诊断证据（trigger、已接受 event refs、媒体 follow-up 状态和稳定 reason code），不能返回可由 host 直接写账本的命令。`availability` 必须区分：

```text
installed_and_active | installed_but_scheduler_disabled | authority_only
adapter_only | paused_by_budget | blocked_by_missing_capability
```

没有安装的 lane 返回 `unavailable`，不是无声 no-op；没有 opening 或模型选择 no-op 才是 `idle`。

## 4. 不变量

1. wake 必须存在于当前 world 的 committed authority，payload hash 和 cursor 必须与 pinned projection 一致；否则零写入。
2. 同一 `world_id + wake_event_ref + ecology_catalog_version` 只拥有一个 TriggerProcess。重启、重复调度与并发调用 join/replay，不能再次审议或重写生活。
3. 每次 advance 最多接受一个主生活族，或一笔 catalog 明确允许的原子 UoW；禁止同轮 `plan → active → settled → experience` 连跳。
4. 模型永远不返回 WorldEvent、revision、actor、idempotency key、hash 或自由事实。implementation 从 pinned projection 验证 refs，并由 authority compiler 构造身份、CAS 和事件。
5. LLM prose、PrivateImpression、用户聊天、图片 prompt、图片生成结果不能单独成为环境、物品、餐食、NPC 到场或 outcome 的事实来源。
6. 任何新世界事实都复用对应 typed Proposal/Acceptance/reducer。Life Ecology 没有 Ledger 直写旁路。
7. 只有成功提交的 event ref 才能进入 media follow-up；后者只会开 preview candidate/opportunity，永不规划、渲染、投递或声称发送。
8. sidecar、Ledger、CAS、模型或工具失败不可产生半个事实。模型失败进入有界 `deferred` recovery，不得切换为脚本化“默认生活”。
9. privacy ceiling 取全部 evidence 最严格值；`private|withhold` 永远不进入图片生态。
10. 受控随机未安装时明确无 draw。安装后必须先记录 candidate-set hash、policy、seed/draw 及结果；draw 只影响软性 opening 选择，不能绕过事实、地点、参与者、预算、能力或同意。

## 5. 分类矩阵：提供可能性，不规定反应

`EcologyMatrixCatalog` 只描述 opening 的合法形状、证据、生命周期和可视潜力；不含“关系疏远必须冷淡”“下雨必须发图”等行为映射。模型可选、拒绝、延后或 no-op，并可在合法 opening 内拖延、改计划、回避、坚持或修复。

| 轴 | 坐标 | 用途 |
| --- | --- | --- |
| 发生来源 | intentional、routine、opportunity、social、environmental、interruption、settlement-aftereffect | 让日常不退化成单一活动模板 |
| 生命周期 | future、planned、active、paused、due、observed、settled、aftermath | 防止计划冒充经历或结果 |
| 认识状态 | observed、verified、proposed、accepted、external-pending | 决定能否成为事实，不决定态度 |
| 社会形态 | alone、npc、user-relayed、shared-private、public | 限制参与者、分享和 privacy |
| 处境 | time segment、location grounding、resource band、goal pressure | 给模型现实代价和发挥空间 |
| 关系/心境 | warm、distant、repairing、boundary pressure、ambivalent | advisory 坐标，不是话术规则 |
| 可视潜力 | none、ambient、object、food、place、activity、social | 指导候选覆盖，绝不生成图像 prompt |
| 偏离方式 | delay、change-plan、avoidance、impulse、persistence、repair | 仅可由记录的 draw 开放 |

首个生产纵切只安装下列三条 lane；其它 taxonomy 不得提前宣称可达。

| lane | 最低输入 | 可提议/接受 | 明确禁止 |
| --- | --- | --- | --- |
| `activity_lifecycle` | 已 accepted 的 plan + clock/地点/资源前置 | `start|resume|pause|complete|abandon` 的合法后继 | 无 plan 宣称完成；同轮开始又结束 |
| `occurrence_observation` | active/eligible occurrence + clock/plan precondition 或外部观察 | outcome observation，后续独立走 outcome settlement | 模型文字直接作为 observation；同轮 commit/settle |
| `visual_observation` | settled occurrence、受权只读工具 receipt、operator 或受信 world observation | 独立 VisualFact | 从用户闲聊、情绪、图片 prompt 推断环境/物品/餐食 |

NPC 仅可作为已有登记 NPC 的 occurrence participant；scheduler 不能凭空注册 NPC。Experience 只能由既有 settled authority 在 settlement UoW 中创建，首个 lane 不将其当独立 writer。

## 6. 内部 adapters

这些是 implementation 的私有 seam，禁止平台直接依赖：

```text
EcologySnapshotReader          pinned projection → LifeEcologyCapsule
EcologyCatalog                 snapshot → admissible openings / policy version
LifeEcologyDeliberator         capsule → typed LifeProposal | no-op
VariationAuthority             opening set → recorded draw (when installed)
LifeEcologyAcceptanceRouter    proposal → existing authority Acceptance
LifeEcologyTriggerStore        claim/join/supersede/complete
VisualFactAuthority            verified source → typed short-lived VisualFact
MediaEcologyFollowup           committed wake → existing candidate runtime
```

Ledger、sidecar、clock 和 existing authority writers 是本地可替换依赖；模型和可选 observation tool 是真外部 adapter，必须注入。首发只在有 production composition profile 时启用，低层默认仍 fail closed，避免让 fixture 与嵌入式宿主静默获得行为。

## 7. VisualFact authority

现有 profile/preference Fact lane 绑定用户消息精确文本，不能扩成角色世界的天气、物品或餐食。为避免混淆，VisualFact 是独立 authority，最小 projection 为：

```text
visual_fact_id, entity_revision, subject_ref, facet,
value_ref, value_hash, observed_at, valid_until,
source_binding, privacy_class, status
```

可安装 facet 仅有：

```text
environment.weather | environment.light | environment.location_grounding
activity.visible_object | meal.visible_food | meal.visible_drink
```

`source_binding` 只能指向：已结算 occurrence 的冻结 result、已授权工具的 immutable receipt/descriptor、operator observation 或已提交受信 world observation。过期、撤销、私密或无 content sidecar 的 VisualFact 不能成为图片候选。图片产物及 prompt 永远不得回写 VisualFact。

## 8. 生产调度与媒体 follow-up

production composition root 显式注入版本化 `LifeEcologyComposition.production_v1(...)`，其中包含 catalog、policy、deliberator、media policy 和已安装的 authority adapters。QQ、HTTP、CLI 均使用同一 profile；没有注入的 test/embedded host 显示 `unavailable`，不偷偷降级至旧世界。

```text
tick commits ClockAdvanced
  → platform/application scheduler calls advance_life_ecology_once
  → one TriggerProcess / one main life proposal at most
  → accepted event(s)
  → MediaEcologyFollowup scans pinned projection once using original durable wake
  → preview candidate/opportunity or explicit unavailable/deferred
```

用户 inbound 不直接触发生态推进。用户消息可作为后续 deliberation 的 `user_influence` evidence，但永远不能证明“角色已出门、已经吃饭、天气如何、NPC 已到场”。

## 9. 验收与施工顺序

1. 实现 wake claim、availability、catalog 和 replay-safe `idle/unavailable`；默认 composition 显式启用，但不安装未完成 lane。
2. 接通 `activity_lifecycle` 的一条 production trace：tick → proposal/no-op → acceptance → Activity event → next Capsule → 一次 preview 候选；重试/重启不重复。
3. 接通 occurrence observation 与其既有 settlement/appraisal continuation；证明事件会影响下一轮心理消费，而不是只为图片存在。
4. 实现 VisualFact authority、sidecar/read contract 与一个受信 source；再开放环境/物品/餐食 taxonomy。
5. 安装 NPC registration/application seam 与 social occurrence；Experience 只通过已结算路径接入。
6. 安装 RandomAuthority 后再启用 variability draw；此前必须在 availability 与 evaluator 中报告其未安装。

每条 lane 的 production fixture 都必须覆盖：默认 composition、缺失/私密证据拒绝、并发/重启/STALE replay、模型 failure recovery、下一轮 Capsule 消费、source → 一次 candidate → preview、媒体不可用不回滚生活事实、逻辑日 category coverage 与频率上限。

## 10. 被拒绝的形状

- 每个图片类别单独 scheduler（天气、餐食、NPC、活动）：会把 evidence/privacy/replay 复杂度扩散到平台与图片机。
- 无类型 `LifeEvent(text, tags)`：将真假、生命周期、隐私和恢复推给每个 caller，模型幻觉会直接成为 authority。
- 扩大现有用户 Fact predicate catalog 来承载角色环境：混淆“用户说喜欢咖啡”和“角色桌上有咖啡”，也缺失地点、有效期和可验证来源。
- 把 ecology 空结果视为世界平静：会掩盖未装配、预算暂停和 capability 缺失，妨碍产品/评估判断。

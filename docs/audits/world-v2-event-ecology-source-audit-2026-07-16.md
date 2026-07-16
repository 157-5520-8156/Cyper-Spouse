# World v2 事件生态：图片候选来源可达性审计

日期：2026-07-16
范围：`EventEcologyMediaCandidateRuntime` 的七个候选类别，以及 HTTP、QQ C2C、CLI 和
离线 harness 中能够实际写入其上游世界 authority 的生产路径。这里的“可达”分为三个层次：

1. **authority 已实现**：event / reducer / projection 可以正确保存和回放；
2. **受控写入 seam 已实现**：宿主可在不直接写 Ledger 的前提下提交该 authority；
3. **默认部署已接通**：当前默认 HTTP / QQ / CLI 组成真的会产生该 authority，并在 durable
   wake 后运行 ecology worker。

只有第三层能支撑“图片机会会随真实世界自然丰富起来”的产品结论。fixture 或手工调用不能代替
默认部署可达性。

## 结论

**目前不能说事件生成已经足够丰富多彩。** ecology 的候选选择模块本身是 source-bound、可回放且
有七类 taxonomy；但当前默认宿主没有启用 `event_ecology_policy`，也没有调度
`drain_media_ecology_once`。因此默认 HTTP、QQ C2C 与 CLI 都不会自然产生 ecology
`PhotoCandidateOpened` / `MediaOpportunityFrozen`。

更重要的是，activity 与 occurrence 虽有受控的手工 seam，但没有默认 scheduler；其余主要依赖
harness、测试直接构造或尚未接入的 authority。特别是“环境 / 食物 / 可见物体”在 taxonomy 中存在，却不在当前
Fact predicate catalog 中，不能由当前事实生产路径写出。这是“分类矩阵比实际世界生产能力更宽”的
明确断点。

## 候选类别矩阵

| ecology 类别 | runtime 实际读取的 authority | authority / seam | 默认宿主可达性 | 当前判定 |
| --- | --- | --- | --- | --- |
| `activity_process` | shareable `plans`，状态 `active` | `ActivityPlanRuntime.plan/transition` 与 application 的 `plan_activity` / `transition_activity` 已实现 | HTTP、QQ、CLI 仅做 inbound/tick/action；仓库中没有它们调用这两个 seam | **手工 / harness 可达，默认未接通** |
| `activity_result` | shareable `plans`，状态 `completed` | 同上，可提交 `ActivityCompleted` | 同上；没有从 deliberation、clock 或生活 scheduler 产生 plan transition | **手工 / harness 可达，默认未接通** |
| `settled_outcome` | shareable settled `world_occurrences` | `OccurrenceContentCoordinator` + `commit_occurrence`、`record_outcome_observation` 和 outcome worker 已存在 | HTTP/QQ composition 没有注入 `outcome_model`，宿主也没有 occurrence/observation 命令；未提交 occurrence 更无后续 | **harness 可达，默认断链** |
| `npc_shared_outcome` | settled occurrence + shareable NPC participant | occurrence authority 已实现 | `NpcRegistered` 仅有 schema/reducer/catalog；没有 application/host registration seam。即使测试手工 seed NPC，默认宿主也不会登记或生成共同事件 | **当前生产不可达** |
| `shared_experience` | shareable `ExperienceCommitted` | Experience authority、proposal/acceptance fixture 已实现 | `ProductionProposalGrammar` 不包含 experience lane；application composition 没有 experience model/worker/host command | **authority-only / harness** |
| `place_environment` | active fact，谓词为 `environment.weather`、`environment.light` 或 `environment.location_grounding` | Fact-v2 chat worker 已接入 CLI，但其 predicate catalog 仅有 location/profile/preference/relationship | taxonomy 所需环境谓词不在 `INSTALLED_FACT_PREDICATE_CARDINALITY`；当前模型也只从用户消息的精确子串写 user subject | **当前生产不可达** |
| `object_or_food` | active fact，谓词为 `activity.visible_object`、`meal.visible_food` 或 `meal.visible_drink` | 同上 | 所需谓词同样未安装；没有世界观察 / activity result → visual fact 的受控生产者 | **当前生产不可达** |

`NpcRegistered` 目前只是允许 ecology 运行的 wake event；它本身不会产生任何一类图片候选。NPC
只有在后来出现一个合格、已结算的 occurrence 时，才会把 `settled_outcome` 升为
`npc_shared_outcome`。

## 证据与断点

### 已有的正确约束

- [`event_ecology_media.py`](../../src/companion_daemon/world_v2/event_ecology_media.py) 只从已提交
  projection 读取 plan、occurrence、experience、fact、NPC；它不从聊天文本、情绪、私密印象或
  未结算计划推断画面。
- 它只接受明确的 durable wake，并将候选和机会同 source event hash 冻结到 sidecar。类别冷却、
  每日上限和幂等均来自普通 projection，而不是 shadow state。
- [`test_event_ecology_media.py`](../../tests/world_v2/test_event_ecology_media.py) 覆盖七类中可由
  fake projection 构造的五类、private 拒绝、replay 和 sidecar fail-closed；
  [`test_production_turn_application.py`](../../tests/world_v2/test_production_turn_application.py) 覆盖
  application seam 可以在一次手工 `ActivityStarted` 后只冻结 preview（不创建 planning action）。

这些测试证明 ecology **不会编造图像事件**；它们不证明宿主会持续生成足够多的上游生活事件。

### 默认启用与调度缺口

- `WorldV2TurnApplicationConfig.event_ecology_policy` 默认是 `None`，只有明确注入时才构造 runtime。
  HTTP host 和 QQ C2C host 的 `WorldV2TurnApplicationConfig(...)` 均未注入它；CLI 也未注入。
- `WorldV2PlatformHost` 暴露 action、media result、media planning 和 generic background drain，但不
  暴露或调用 `drain_media_ecology_once`。生产代码中该方法的调用点只有 application 定义；唯一
  端到端使用是测试。
- 因此即使某个非默认 writer 先写入合格 authority，默认平台 tick 也不会自动执行“wake →
  candidate/opportunity”这一步。

### 事实 taxonomy 与事实 authority 不一致

`_VISUAL_FACT_CATEGORY` 声明了六个视觉谓词，但
[`fact_reducers.py`](../../src/companion_daemon/world_v2/fact_reducers.py) 的
`INSTALLED_FACT_PREDICATE_CARDINALITY` 只有：

```text
location.current
profile.display_name
profile.timezone
preference.likes / dislikes
relationship.affiliation
```

`FactObservationProposalAdapter` 只允许这份安装表中的谓词，并要求 value 是用户消息的精确子串。
所以它不能可靠地把“今天下雨”“桌上有一杯咖啡”变成 world/environment authority；更不能从未观察
到的日常生活生成这些内容。与此同时，现行 Fact-v2 materialization 事件是 `FactCommittedV2`，
而 ecology wake allow-list 列的是 `FactCommitted` / `FactCorrected`。在增加视觉事实 authority 后，
还需明确选择并测试 `FactCommittedV2` 的 wake 或由下一次 `ClockAdvanced` 统一唤醒，不能假设它会
自动运行。

### 生活事件生产仍是“接口已存在”而非“世界在运转”

- `ActivityPlanRuntime` 需要已有 `ObservationRecorded` 作为 source，能安全写计划和转态；当前
  没有 planner / scheduler 把 advisory、目标、NPC、clock 或模型判断变成这些命令。
- `commit_occurrence` 要求调用方提供完整 occurrence 和 outcome candidate 内容；
  `record_outcome_observation` 要求外部观察。二者是正确的受控 writer，但目前没有 HTTP/QQ
  适配器或 background worker 产生这些命令。默认 host 也没有注入 `outcome_model`，故已存在
  occurrence 不会在默认部署自行结算。
- Experience 已有严格的 authority contract，但 production proposal grammar 的 allow-list 未安装
  `experience_transition`，所以不能把 authority fixture 误写为一个正在运行的生活经历生产器。

## 对图片机的直接含义

图片机现在最多能可靠消费**被外部明确提交、并且由额外 scheduler 手工唤醒的**少量生活证据。它不应
以空白 taxonomy 或自由文本补全来伪造“丰富生活”。当前风险不是图片模板不够多，而是来源生态太窄：
同一 shareable activity 最多形成一次候选，且 daily/category cooldown 会继续收紧稀少输入。

## 建议的闭环顺序

1. **先安装一个 event ecology scheduler seam**：只消费已提交的 life/clock event，持久记录
   wake cursor / claim，调用 `drain_media_ecology_once`；默认 host 显式配置 policy。它不能从
   inbound 文本或模型输出直接造候选。
2. **给生活事件建立少数、可验证的生产 lanes**，而不是为每个图片类型写规则：
   activity lifecycle、occurrence observation/settlement、NPC social outcome 先各完成一条
   source → authority → next-turn consumer 的 production trace。
3. **建立单独的 VisualFact authority**：它应由受信 world observation、已结算 occurrence 或
   受权工具结果写入，而不是复用“用户说了什么”的 profile/preference Fact lane。先安装 predicate
   catalog、privacy、content sidecar/read contract，再让 ecology 消费它。
4. **把 Experience production lane 单独接通或明确不作为图片来源**；不要让 taxonomy 表示它已自然
   发生。接通前保持 no-op 比伪造经历更符合账本语义。
5. 针对每一类增加 production integration fixture：默认 composition 写入真实 source、scheduler
   恰好一次冻结 candidate、重启后不重复；并统计一段逻辑日内 category coverage，而非只断言
   event type 存在。

## 审计结论的边界

本审计不主张让规则决定人物该做什么。上游可以由受约束的 LLM deliberation 在分类矩阵内提出
activity / occurrence / observation 候选；但候选必须经既有 authority、evidence、privacy、acceptance
和回放路径成为事实。ecology 是一个深模块：它以小的 `drain_once(wake_event_ref, logical_time, …)`
interface 隐藏去重、冷却、source binding 和 snapshot 冻结；它不是新的世界作者。

# World v2 内心接入总清单（Inner-Life Coverage Plan）

日期：2026-07-19。来源：对 world_v2 全部行为通路与感受通路的双向审计。

## 原则（来自产品意图，约束所有后续实现）

1. **一切行为背后都有内心感受**：每个行为决策通路应 (a) 读取已接受的
   Appraisal / AffectEpisode / RelationshipState 作为输入，(b) 行为的结果
   反过来有机会产生新的内心状态。
2. **规则之上的可控随机**：先验用 `RandomAuthority` 加权抽签表达（权重可以
   被情绪调制），语义判断交给模型；两者都被账本记录、可回放。禁止把行为
   写成确定性 if/else 情绪规则。
3. 内心状态只经 `AppraisalAccepted → AffectEpisode / RelationshipSignal`
   这一条权威链产生；新的感受来源=新的 appraisal 触发源，不是第二条写路。

## 现状结论（2026-07-19 审计）

内心感受目前只有两个入口：**用户消息**（`interaction_appraisal`）与
**生活事件结算**（`npc_world_appraisal` ← `WorldOccurrenceSettled`）。
行为侧只有入站回复（timing/表达）是完整"有心"闭环。

### 行为通路记分

| 通路 | 读心 | 可控随机 | 写心 | 状态 |
| --- | --- | --- | --- | --- |
| 入站回复 timing/表达 | ✅ 胶囊含情绪 | 模型语义决定 | ✅ interaction_appraisal | 有心 |
| 主动联络 spontaneous | ✅ 关系+情绪调等待 | ⚠️ 抽签记录但不门禁 | ❌ 无 appraisal | 半心 |
| 回应缺口 response_gap | ❌ 纯时间窗规则 | ❌ | ❌ | 无心 |
| 生活作者选活动 | ✅ v3 起情绪调权重 | ✅ 加权抽签+模型否决 | ❌（活动结算才写） | **已修**（2026-07-19, life-author-weight.3） |
| 活动生命周期(开始/完成/放弃) | ❌ 胶囊无情绪 | ❌ 纯模型 | ⚠️ 仅经结算间接 | 无心 |
| 活动结果选择 aftermath | ❌ 均匀抽签+模型无情绪 | ⚠️ 无权重 | ✅ npc_world_appraisal | 半心 |
| 延迟回复 | 上游 timing 有心 | — | ❌ 兑现时不再评估 | 半心 |
| 媒体分享意愿 | ❌ advisory 自报缺 emotional_meaning | ⚠️ 无权重抽签 | ❌ 无余波 | 无心 |
| NPC 事件 | 事后评估有 | — | ✅ | 半心 |

### 感受空白场景

| 场景 | 现状 | 期望 |
| --- | --- | --- |
| 用户长时间不回 / 被晾着 | 只驱动"要不要跟进"行为 | 产生"惦记/失落"类 appraisal，进而影响后续语气与主动性 |
| 活动完成/放弃当下 | 放弃已接入（2026-07-19 `plan_disruption_appraisal`）；完成仍只有结算后可选评估 | 放弃一个计划应可留下懊恼/如释重负 |
| 主动联络之前 | 无"想找人说话"的 durable 状态 | 联络行为应由孤独/分享欲支撑 |
| 媒体分享之后 | 无心理余波 | 分享后的期待/忐忑，收到回应后的满足 |
| 独处/无聊 | 时钟只做衰减 | 长时间无事件可低概率产生内省类 appraisal |
| 她自己说出的话 | 不回流 | 说了重话应可自我评估（后悔/坚持） |
| 同轮情绪门 | 关键词表 `_IMMEDIATE_EMOTION_CUES` | 语义化信号判断（本地小模型可胜任） |

## 工作序列（按对真人感的价值排序）

1. **[已完成 2026-07-19] 情绪 → 生活作者权重**（`life-author-weight.3`）：
   沉重情绪抬升休息/睡眠/刷手机域、压低学习/创作/杂务域；孤独抬升社交域，
   受伤压低社交域；joy/warmth 抬升出门与创作。幅度 ≤±35%，纯整数运算，
   抽签记录权重，回放安全。
2. **[已完成 2026-07-19] 沉默 → 感受**（`silence_appraisal` 触发源）：
   她最后一条可见回复投递后，用户按世界时钟安静
   ≥ `silence_appraisal_idle_seconds`（默认 3600s，0/None 禁用）且期间无
   任何新用户消息时，以该回复的 `ExecutionReceiptRecorded` 为锚点开且仅开
   一次评估机会；模型决定这段沉默意味着什么（可 no_change），接受后走既有
   affect/relationship 下游。用户回复即关闭这条通路（改走 interaction
   appraisal），换她下一条回复重新计时。
3. **[已完成 2026-07-19] 活动生命周期读心**：worker 在
   `situation_summary` 注入压缩情绪摘要（`mood_view.mood_summary_prose`，
   只含已接受的活跃情绪、无任何机制引用），"放弃/坚持"由心情参与。
4. **[已完成 2026-07-19，部分] 活动结果读心**：aftermath 的 outcome 模型
   胶囊带 `current_mood` 顾问材料（情绪一致性是倾向不是命令）。
   抽签加权部分**有意暂缓**：种子 outcome 没有结构化的情绪效价标注，
   在无标注的情况下用代码猜文案正负会违反"不写死规则"；语义一致性
   已由模型覆写承担。若未来种子加 `valence` 字段再补加权。
5. **[已完成 2026-07-19] 主动联络闭环**：spontaneous 抽签现在是真实的
   心情加权冲动门——`hold` 意味着"在当前内心状态下这段安静不产生联络冲动"。
   attempt 身份绑定编译后的 profile（关系/情绪/活动/时段），内心状态变化
   即产生新抽签，调度器重试永远不能对同一状态重掷。发出后的"期待回应"
   由第 2 项的沉默评估天然覆盖（proactive_message 也是沉默锚点）。
   response_gap **有意不再加第三道门**：它的两端已经有心（表达时模型冻结
   了期待窗口，兑现时 proactive 模型带情绪上下文决定），中间再加规则门
   属于堆叠规则，违反原则。
6. **媒体意愿读心**：`MediaCandidateAdvisoryCompiler` 填 `emotional_meaning`
   （只动世界侧的选择/意愿层；**图片机本体不动**——它只负责接收事件并生图，
   见 docs/media-machine.md 的边界）。"分享后的余波"不再做媒体特例，
   并入第 6b 项的通用期待机制。
6b. **[已完成 2026-07-19] 通用期待→感受**（产品原则：人做很多事都期待
   反馈，媒体只是其一）：表达契约中模型自声明的 `ResponseExpectationDraft`
   （hoped_response/pressure/importance/wait/expiry）此前只驱动行为
   （response_gap 跟进）。现补上感受侧（`response_expectation_view.py`，
   `response-expectation-view.1`）：确定性解析器从
   `expression_plan_manifests` 沿 receipt→action→beat→manifest 精确回链
   冻结期待，输出仅含语义值的视图（hoped_response、pressure/importance
   档位、声明多久了；无 ID/哈希），过期期待不再出现。
   - 沉默评估（`SilenceAppraisalTurn`）以沉默锚点 receipt 解析该表达的
     期待，走既有 InnerAdvisory 通道（同 plan_disruption 的"已提交事实
     只读注入"机制）进入胶囊——求安慰后被晾与随口一句没人接不再等价；
   - 互动评估（interaction_appraisal 的 `PinnedTurnCompiler`，含单调
     inbound cognition）带上"最近一条未过期且在该消息之前已投递"的期待
     （revision 上界保证因果：消息不会被它之后才声明的期待解释）；
   - 无期待时不注入任何字段；模型仍自行决定这段期待意味着什么。
   - 媒体的 MediaInteractionBid 天然是同一概念的媒体化身，投递确认后的
     期待走同一条感受通路，不另造机制（本次未动媒体文件）。
6c. **[已完成 2026-07-19，阶段一] NPC 轻自主性**（`npc_initiative.py` +
   `reviewed-life.6` 的 `npc_initiated_events`）：范予安可以主动进入她的
   生活（借书 5 分钟小事、临时约看书单、为书单闹小分歧）。实现为
   open_world 计划外事件通路的评审目录同胞 lane（复用其"无计划 occurrence
   直接 commit+activate、由既有 aftermath 结算"的下游；上游换成评审目录+
   概率门）：每本地日至多 2 个检查槽（上午/下午，身份编入日期+槽号幂等）、
   至多 1 次发生；发生与否是 RandomAuthority 带"nothing"候选的加权抽签
   （`npc-initiative-weight.1`：base_chance_bp × 关系读数[阶段一以已接受
   warmth 近似关系近→约她/借书升、resentment/anger 近似未消化别扭→分歧
   微升] × 情绪读数[孤独抬升全部 NPC 主动事件]，封顶 ±40%，纯整数可回放），
   抽中后模型按 life author 的 select/no_op 契约做最终语义确认——"范予安
   今天没来找她"永远合法。落账 occurrence 经既有 LifeAftermathRuntime
   结算：mood 顾问参与双结局选择、强制开 `npc_world_appraisal`、提交
   Committed Experience 与 life content。NPC 不在场时段绝不发生（种子
   载入时校验事件窗 ⊆ NPC 作息，运行时再核对）。配置
   `npc_initiative_enabled`（默认 True）。阶段一为单 NPC、无独立关系
   状态；每 NPC 独立关系状态仍是后续项，届时把权重政策的关系读数换成
   真正的 per-NPC 关系投影并 bump 权重版本。
7. **远期日历（多日计划）**：新的"远期生活作者"通路——模型在世界约束下
   写下几天后的计划（`PlanStateProjection.DueWindow` 已支持未来窗口），
   日子到了兑现或被突发事件打乱；打乱必须经过 appraisal 产生感受。
   完成后把小屋"时间账本"面板接回 v2 数据（旧 `calendar_events` 表只读存档）。
   **[阶段 A 已完成 2026-07-19]**（`future_life_author.py` +
   `reviewed-life.5` 的 `future_openings` + 小屋"日历·未来几天"面板）：
   每本地日至多一次成功规划（日期编进事件身份幂等）、候选 ≤16 个、
   `future-life-author-weight.1` 权重读入情绪（孤独抬升/沉重压低社交类
   未来承诺）、加权抽签+模型确认落 `ActivityPlanned`（未来窗口），到期由
   既有生命周期正常兑现；`candidates_at` 改为仅"窗口与当下开口重叠或已
   逾期"的计划抑制当下生活，纯未来计划不再冻结当日。
   **[阶段 B 已完成 2026-07-19]**（`plan_disruption_appraisal` 触发源）：
   每个落账的 `ActivityAbandoned`（生命周期模型选 abandon、interruption/
   change_plan 替换、宿主显式取消）给她一次评估机会——后台 opener 以最新
   未开触发器的放弃事件为锚点幂等开启（旧放弃在更新的放弃落账后视为过期
   消息，不再回补），消费器把被放弃计划的已提交事实（activity_kind、原定
   窗口、是否未来承诺、参与者）以只读 advisory 注入胶囊，模型决定这次
   打乱意味着什么（懊恼/如释重负/无所谓，可 no_change），接受后走既有
   affect/relationship 下游。
8. **[已完成 2026-07-19] 同轮情绪门语义化**：三级决策——关键词命中零延迟
   直通；未命中问本地 Qwen（严格 JSON，2.5s 硬超时，prompt 覆盖冷暴力/
   阴阳怪气/突然冷淡）；任何故障回退关键词结论。durable 评估触发器
   无条件恒开，门只决定"同轮还是后台"，故障永不丢评估。
   配置 `semantic_immediate_emotion_gate`（默认 True）。
9. **[已完成 2026-07-19，阶段一] 憧憬层（aspiration，基于 Private
   Commitment 词条）**：与计划分离的低兑现度心愿状态——没有确切时间窗、
   不进生命周期兑现管道、不会逾期腐烂。**独立权威**（`aspiration_events` +
   `aspiration_reducers` + `AspirationProjection`，方案 B）：复用 commitments
   会被其 reducer 的强制 due_window（open→due→broken 时钟腐烂）和必填
   fulfillment_contract 腐化语义，故照 NpcRegistered 的 DomainMutation 纪律
   新建四个账本事件（Planted/Reinforced/Faded/Crystallized，全部带证据引用，
   种下绑定源素材事件）。**种下**（`aspiration_runtime.py` +
   `reviewed-life.7` 的 `aspiration_seeds`）：每本地日至多一次检查（日期编入
   身份幂等），候选=评审心愿模板且其 eligibility 见证（最近 7 天内被接受的
   对应 activity_kind 计划）真实存在；RandomAuthority 低概率抽签
   （`aspiration-seed-weight.1`，评审基线 600-800bp ≈ 每检查 ~7%，nothing
   候选恒合法）+ 模型 select/no_op 确认。**存续**：同一日检查顺带维护——
   相关素材再现时按 25% 抽签强化（重置淡忘时钟）；≥14 天（可配）未强化的
   按 10%（可配）抽签淡忘，落账即可、不强制感受。**表达上下文**：活跃心愿
   经既有 Inner-Advisory 通道（同 6b 期待视图的注入机制）进入 chat_reply 与
   interaction_appraisal 的胶囊，advisories 切片对 source_refs（种下事件）做
   committed-event 复验——"我一直想去日本"永远有账本背书。**范围裁剪**：
   结晶只留权威接口（reducer 校验 plan 存在，无 runtime 通路）；种下后的
   向往 appraisal 暂缓（新 process_kind 的注册/绑定/消费成本超出阶段一，
   感受侧留待与"独处内省"通路合并设计），报告见
   `tests/world_v2/test_aspiration.py`。配置 `aspiration_enabled`（默认
   True）。基线漂移：reviewed-life.7 改变目录哈希使 7 天丰富度抽样合法
   重采样，`test_life_event_richness_7day` 容忍集加入低权重 filler。

## 回放与版本纪律

- 权重政策改版 = bump `weight_policy_version`（draw 记录版本+权重，回放不重算）。
- 新 appraisal 触发源 = 新 process_kind，reducer 增加绑定校验，旧事件不受影响。
- 活动目录语义改版 = bump `ACTIVITY_OPENING_CATALOG_VERSION`（旧版本按冻结规则回放）。
- 离线场景基线漂移时按 `scenario_runner.py` 的版本化流程记录理由。

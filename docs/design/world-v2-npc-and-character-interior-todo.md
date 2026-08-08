# World V2 NPC 连续性、低成本自主性与统一人物内心 TODO

状态：滚动实施清单（P0、P1 与 P4 核心切片已于 2026-08-04 落地）

日期：2026-08-04

适用范围：World V2 生产路径、Life Ecology、NPC 社会生态、角色内心、记忆、关系、主动联系、媒体与外界感知

上位约束：`CONTEXT.md`、ADR-0010、ADR-0011、ADR-0012、ADR-0014

## 1. 目标与非目标

本清单追求的是一个会持续生活、会被他人和外界影响、也会反过来影响他人的开放世界，
而不是增加 NPC 剧情库或用规则模拟社交行为。

目标：

- NPC 可以从已结算的开放生活事件中产生，并在后续事件中保持同一身份；
- NPC 拥有来源明确、可重放、成本可控的近况、目标、日程、经历、关系和生命周期；
- NPC 可以主动邀请、疏远、求助、误会、修复、离开或重新出现，但这些都是模型在当前
  情境中的自由选择，不是宿主枚举的必经剧情；
- 角色与 NPC 的互动可以影响双方后续状态、角色 Affect、记忆和生活发展；
- 主角的稳定自我、当前感受、关系、记忆、愿望和未解决事项通过一个深
  `CharacterInterior` Module 形成统一的来源绑定视图；
- Chat、主动联系、Life、Appraisal、媒体和感知使用同一 cursor 上的同一人物，而不是各自
  拼一份近似上下文；
- 增加复杂性时保持事件溯源、CAS、effect-once、来源闭包、隐私、同意和冷重放。

非目标：

- 不为 NPC 建立固定的“玩耍/旅行/吵架/和好”剧情菜单；
- 不用关键词、关系阈值或情绪矩阵替 NPC 或主角选择行为；
- 不让每个 NPC 每隔固定时间调用一次云端模型；
- 不持久化隐藏推理或把散文内心独白当成 World Fact；
- 不把所有心理状态塞进一个可随意覆写的 `MindState` JSON；
- 不让 NPC 的私有判断成为关于主角、用户或外部世界的事实权限；
- 不因工作集预算不足而永久删除 NPC 或改写已发生历史。

## 2. 2026-08-04 生产证据基线

本节区分“代码存在”“生产装配”“实际发生”，避免把 schema、测试 fixture 或 dormant
runtime 当成已上线能力。

- 当前生产账本约 10,417 个事件；
- 已登记 5 个 NPC：2 个 reviewed seed NPC，3 个开放生活事件结算后生成的动态 NPC；
- 1 个 seed NPC 已按传记情境退场；
- 3 个动态 NPC 分别有不可变人物简介 sidecar，但 `known_trait_refs` 为空、当前位置为空；
- 全账本只有 1 次明确包含 NPC participant 的已提交共同事件，对象是 seed NPC
  `literature-fan`；
- 3 个动态 NPC 的后续共同事件数均为 0；
- 26 个生活 occurrence 已结算，26 个 `npc_world_appraisal` 机会已打开，说明“生活结果
  → 主角评价”的生产装配正在运行；
- 最终只有 8 个 Appraisal、3 个 Affect Episode 被接受；模型合法选择 `no_change` 本身不是
  缺陷，但需要长期体验证据证明比例合理；
- 用户关系只有 1 个 signal 和 1 次 slow-variable adjustment，当前仍为 `stranger`；
- World head 中 `character_core=null`，`aspirations=[]`、`goals=[]`、`threads=[]`、
  `commitments=[]`、`resources=[]`；
- 24 个 MemoryCandidate 均为 active；至少一个 Experience Memory 整理任务连续 15 次
  `invalid_output`；
- 外界感知已启用并提交过真实 perception，但 16 个登记来源只有 1 个启用；
- 图片 provider grant 存在，但生产账本没有证明完整图片 planning → render → inspection →
  delivery 链真实发生；
- 主动联系与 Life Ecology 仍有显著技术失败，不能把长期没有可见行为全部解释为角色选择。

上述数字是审计时点的运行证据，不应写成永久测试常量。后续验收读取公开 projection/health，
而不是直接依赖本段数字。

## 2.1 本轮实际落地边界

已经进入生产装配、事件账本和冷重放路径的能力：

- 动态 NPC 的 provisional → stable promotion edge、不可变 descriptor sidecar、首次共同经历
  绑定、`NpcIdentityView`、Life/Recall 的来源闭合人物文档；
- NPC 自己持有的内心摘要、目标和 NPC→主角八维关系状态；这些内容按 NPC 隔离并以
  `NpcStateChanged`、`LifeContentRecorded` 写入账本，不能被其他 NPC 或主角直接冒用；
- `NpcEcology` 深 Module：事件刺激与到期计划即时唤醒，长期无事件复用 Life Ecology 的
  45 分钟至 8 小时 ambient 机会；NPC Actor 自由选择 `no_op/propose`，World Author 只裁决
  外部结果，不能改写 NPC 的动机、行动和计划；
- 同一次成功阶段 effect-once，解析/语义错误只允许同模型一次受约束重选，技术失败走现有
  10/30/120 分钟退避；每次成功或失败的模型尝试均写入标准 `ModelResultRecorded` 审计；
- 被接受的即时事件和未来计划进入普通 Activity/Occurrence/Settlement 链；结算后继续走
  主角 Appraisal → Affect → Experience/Memory，因此 NPC 事件会真实影响角色情绪和后续记忆；
- `/health` 已能区分 promotion 闭包、实际模型尝试、完成决定、no-op、技术失败、再次共同
  场景与生命周期恢复，不再用“成功决定数”冒充全部调用数。

仍然是后续阶段、不能声称已经完成的部分：

- 主角→NPC 目前仍是来源绑定的派生 relationship reading；NPC→主角已经是模型拥有的八维
  状态。主角侧独立八维慢变量及语义修订尚未接入统一 `CharacterInterior.experience`；
- 动态 NPC 的客观地点、组织、Life Arc、dormant/departed/reappearance 还没有统一的开放
  effect contract；现有 reviewed NPC 传记生命周期可运行，但不能代表动态 NPC 闭环完成；
- 本地小模型 shadow qualification、多个 NPC 的共享批次调用以及高 materiality 云端升级仍
  未落地；当前生产只在真实事件/到期计划/低频 ambient 时调用既有背景模型；
- `CharacterInterior` 的 `project/experience/consider` 统一 seam 属于本轮重大迁移；最终合同见
  `docs/design/unified-character-interior.md` 与 ADR-0016。

这些未完成项继续保留在下文 P2/P3/P4/P5，避免把“schema 存在”写成“生产能力完成”。

## 3. NPC 缺口一：动态 NPC 出生后缺少身份连续性

### 3.1 当前问题

开放 Life Development 可以在候选 outcome 中提出 Provisional NPC。只有对应 outcome 真正
结算后，`BiographicalLifecycleRuntime` 才生成稳定 `NpcRegistered`，这条事实边界是正确的。

但当前登记结果主要只有：

- 内容寻址的人物简介 `stable_identity_ref`；
- `npc:<hash>` 稳定引用；
- privacy 与 active 状态；
- 空 `known_trait_refs`；
- 空 `current_location_ref`。

后续 Life Capability Manifest 会再次提供 `npc:<hash>`，却没有保证把人物简介、首次相遇、
与主角的关系、近期近况和可用位置作为一个闭合的人物描述一起提供。对 World Author 来说，
这个 ref 很可能只是一个不透明坐标。由 outcome 生成的 NPC 也未必以最终稳定 NPC ref 成为
首次 Experience 的 participant，导致“主角遇见了谁”和“后来登记的是谁”不容易自动连接。

结果是：系统会生成人，却不容易再次认出、选择或自然提到这个人。

### 3.2 TODO：稳定人物描述与首次经历绑定

- [ ] 新增 `NpcIdentityView`，在精确 ledger cursor 上解析：
  - 稳定 NPC ref；
  - 已验证人物简介 sidecar 文本与 hash；
  - 首次出现/结算事件；
  - 主角是否实际参与首次事件；
  - 已结算的共同 Experience refs；
  - 最近已知地点、组织、Life Arc 和可用性；
  - privacy 与可披露范围；
  - active/dormant/departed 等事实状态；
  - 所有字段的直接 source refs。
- [ ] 让首次结算中获准物化的 Provisional NPC 与最终 NPC ref 建立不可变 promotion edge；
  后续重放不得重新生成新身份。
- [ ] 首次 Experience 中若主角确实见到该人物，使用 promotion edge 将共同经历投影到最终
  NPC ref；不得靠名字相似、关键词或字符串别名猜测同一人。
- [ ] 在 Life Context、CharacterInterior 和 Recall corpus 中加入来源绑定的 NPC identity
  document；不能只提供 opaque ref。
- [ ] World Author 选择既有 NPC 时必须看到同一份 bounded descriptor，不得通过重新创作
  人物简介来“回忆”NPC。
- [ ] 检测同一 settled origin 重复 promotion、同一 provisional ref 多重登记和不同人物错误
  合并；冲突时 fail closed。
- [ ] 新人物相似性只可作为待确认的去重建议，不得由 embedding 相似度自动合并身份。
- [ ] 健康接口增加：动态 NPC 数、拥有完整 descriptor 数、孤儿 NPC 数、被再次引用数、
  promotion 闭包失败数。

### 3.3 验收

- [ ] 冷重放后 NPC ref、descriptor hash、首次经历与 participant 绑定字节等价；
- [ ] 同一 NPC 在后续 Life 选择、Recall、Appraisal 和表达中使用同一身份；
- [ ] World Author 可以合法再次选择动态 NPC，而无需重新发明其姓名和背景；
- [ ] NPC 没有再次出现可以是模型选择，但不能是上下文里只剩 opaque ref；
- [ ] 不允许角色以前说过的一句话单独证明 NPC 的身份或共同历史。

## 4. NPC 缺口二：低成本但有质量的自主状态

### 4.1 需要拥有的状态

NPC 不需要复制主角全部聊天、媒体、主动联系和复杂表达管道，但 recurring NPC 至少需要：

- 稳定身份、人物简介、隐私和事实来源；
- 自己当前的人生阶段、组织、住处/活动范围和可用性；
- 自己近期在做什么、最近遇到了什么；
- 少量持续目标、未完成事项和计划窗口；
- NPC 对主角的态度、信任、亲近、摩擦、期待和修复状态；
- NPC 自己记得的共同经历和与当前情境相关的个人经历；
- 对主角未履行/已履行的约定与未解决冲突；
- 离开、暂时失联、重新出现、换工作、毕业、迁居等生命周期状态；
- 每项状态的来源、有效期、修订与冷重放身份。

这些是 NPC 作判断时的环境，不是邀请、疏远、求助或争吵的行为脚本。

### 4.2 推荐架构：`NpcEcology` 深 Module

外部 Interface 应保持小：

```python
class NpcEcology:
    async def advance(self, stimulus: NpcEcologyStimulus) -> NpcEcologyResult: ...
    def snapshot(self, cursor: ProjectionCursor) -> NpcSocialWorldSnapshot: ...
```

Module 内部隐藏：

- NPC identity/relationship/lifecycle projection join；
- 相关 NPC 工作集选择；
- 情境窗口合并和可重放机会随机；
- 本地模型与云端模型路由；
- NPC 角色上下文编译；
- Proposal/Acceptance/CAS；
- occurrence、plan、invitation 或 no-op 的物化；
- 失败退避、恢复和健康度。

调用方不应分别操作“NPC 目标更新器”“NPC 关系更新器”“NPC 邀请器”等浅 Module，否则同一
人物又会散回多个互不一致的状态机。

### 4.3 分层存在，不分层人格真实性

采用热度分层控制计算，但层级只决定资源和考虑机会，不决定 NPC 应做什么：

1. `archived/dormant`
   - 永久保留身份和历史；
   - 无周期模型调用；
   - 被地点、组织、共同记忆、新事件或 Life Arc 重新关联时可以恢复；
   - 不能因退出热工作集而改写为“关系结束”。
2. `background`
   - 有来源绑定的近况、日程和少量状态；
   - 只有相关事件、计划到期或低频 ambient opportunity 才进入考虑；
   - 可以在共享社会世界调用中返回 no-op 或事件提议。
3. `recurring`
   - 最近确有共同经历、开放约定、关系张力或持续组织联系；
   - 获得独立 NPC capsule 和更精确的模型考虑；
   - 仍然只按事件唤醒，不做每 30 秒或每 10 分钟轮询调用。
4. `scene-critical`
   - 当前场景会形成直接联系、明显关系变化、冲突修复、长期计划或生平后果；
   - 使用质量更高的模型路线和完整来源复核；
   - 终态后返回普通层级，不常驻高成本状态。

层级变化必须记录原因和来源。关系近并不强制升级，关系远也不强制 NPC 疏远；这是运行预算
分配，不是社交语义裁决。

### 4.4 事件驱动而不是 `NPC × 时间` 轮询

NPC 考虑机会可由以下已提交事实唤醒：

- NPC 自己的 Plan 到期、Life Arc 变化、组织/地点变化；
- 主角与 NPC 的共同 occurrence 结算；
- 与 NPC 绑定的 Commitment、Thread、冲突或邀请到期；
- 主角状态变化真实影响了共同事项；
- 外界感知或公共事件进入 NPC 可访问渠道；
- 长期无事件时的低频 ambient 社会生态机会。

同一时间窗内的刺激合并，避免一个 occurrence 同时触发近况、关系、目标和邀请四次模型调用。
Clock 只能开放考虑机会和机械推进时间，不能直接生成 NPC 动机或事件。

### 4.5 模型成本路由

推荐四层执行：

1. **零模型层**
   - 到期、时间窗、地点可用性、生命周期机械过期、去重、CAS 和重放；
   - 纯投影重建、检索、descriptor join、预算和隐私裁剪。
2. **本地检索/压缩层**
   - 语义召回、相关 NPC 工作集、旧经历摘要和候选情境压缩；
   - 只决定“哪些材料值得给 NPC 模型看”，不能决定 NPC 行为；
   - 优先复用已经存在且经过隔离的本地模型/embedding 基础设施。
3. **本地 NPC 角色模型层**
   - 对低风险、无外部 Action、无重大关系/生平后果的 NPC consideration，允许经过正式
     qualification 的本地小模型以明确 `npc_actor` 身份自由选择 `propose/no_op`；
   - 它必须看到来源绑定 capsule，而不是固定事件候选；
   - 非法结果允许同一模型一次受约束重选，不能由本地模板代写。
4. **现有云端背景模型层**
   - 直接邀请/求助、冲突、修复、重要关系变化、长期方向、生平迁移、会进入主角可见表达的
     高 materiality 场景；
   - 尽量复用现有 background World Author 路线，不为每个 NPC 新建一个供应商栈；
   - 只有真正进入该场景的 NPC 才调用，不为所有 active NPC 预生成生活。

本地模型资格不足时应降级为“减少考虑机会或等待云端预算”，不能使用固定剧情或模板模拟
NPC 已经作出了决定。

### 4.6 共享社会世界调用

为了避免每个 NPC 一次云端调用，可让一次 `NpcSocialWorldAuthor` 调用同时看到当前相关的
少量 NPC capsule，并提出零个或多个来源闭合的社会事件机会。约束如下：

- 每个 NPC capsule 必须彼此隔离身份、私有记忆和关系方向；
- 输出逐 NPC 标注 actor ref、source refs 和 proposed effect；
- 一个 NPC 的材料不能证明另一个 NPC 的经历；
- World Author 可以提出 NPC 的外部行动或环境偶然性，但不能替主角决定如何回应；
- 主角参与时仍由现有 Character Model 选择接受、拒绝、延后或形成其他行动；
- 多 NPC 同场时以一个 scene occurrence 结算，避免同一场景重复写多份矛盾事实；
- batch 只降低调用数，不得把多个人压成一个共享“群体内心”。

### 4.7 质量保障

- [ ] 建立 recurring NPC 的稳定角色 capsule，而不是每轮只给一句简介；
- [ ] NPC 模型可选择 Recall，复用既有来源绑定检索，不建立 NPC 专用 RAG 旁路；
- [ ] 低成本模型在 shadow 中完成身份连续性、关系方向、时间地点、no-op 与不利事件测试；
- [ ] 用真实生成轨迹比较本地路线和云端路线，不能只测 JSON 合法率；
- [ ] 高 materiality 自动升级属于能力/风险路由，不是行为建议；
- [ ] 一个事件最多形成一次 NPC 决定与一次主角决定，恢复不重复调用已成功阶段；
- [ ] 记录每个 NPC 每日调用数、tokens、升级率、no-op 率、重现率和技术失败率；
- [ ] 成本指标按“每个已结算 NPC 场景”和“每个 recurring NPC/日”报告，避免只看总账单；
- [ ] 人工评审关注人物是否保持一致、是否像工具人、是否只会邀请/吵架、是否会自然淡出。

## 5. NPC 缺口三：关系过薄且存在串人缺陷

### 5.1 当前问题

当前 `NpcRelationshipReading` 只从双方已结算的共同事件次数与七天内新近程度推导
closeness/familiarity。这是故意收窄的可观察 view：NPC 域不得读取主角 private Appraisal、
active Affect 或 residue，也不能以这些材料推导 NPC 所知的 friction。它因此不会跨人物泄漏
主角内心，但仍不足以表达持续社会关系；friction/tension 必须将来由对应 actor 在有来源的
互动后写入方向明确的关系状态，不能恢复旧的私有投影旁路。

### 5.2 TODO：先修正确性

- [x] NPC Ecology 与 relationship reading 不消费主角 private Appraisal/Affect/residue；
- [x] World Author payload 只保留 NPC actor proposal、可用世界参与者与地点，不暴露主角内心；
- [x] 历史 Appraisal/Affect 只保留 reducer 重放语义，不再成为 NPC Ecology wake、关系读模或关系权重输入；
- [x] 增加 projection 访问陷阱与 World Author payload 防泄漏回归；
- [ ] 修复 advisory 显示：解析受信人物简介/称呼，不把 `content:*` 或裸 hash 当成人名；
- [ ] 健康接口报告无 subject 的 NPC appraisal 和跨 NPC 污染拒绝数。

### 5.3 TODO：建立双向关系状态

同一对人物至少区分：

- 主角对 NPC 的关系取向；
- NPC 对主角的关系取向。

二者不能因一次共同事件机械相等。推荐复用用户关系的慢变量思想，但不要建立情绪—行为矩阵：

- familiarity；
- closeness；
- trust；
- respect；
- reliability；
- mutuality；
- friction/tension；
- repair confidence；
- 开放约定、未解决冲突和最近显著变化；
- 一个自由文本、来源绑定且可修订的 private orientation summary。

数值是上下文坐标和迟滞状态，不得直接命令邀请、和好、疏远或沉默。语义变化由对应 actor
模型在已结算互动后提出，代码只验证来源、作用对象、幅度权限和重放身份。

### 5.4 验收

- [ ] 与 A 吵架不会改变 B 的 friction；
- [ ] NPC 信任主角但主角对 NPC 戒备可以同时存在；
- [ ] 一次愉快互动不会机械清除未解决冲突；
- [ ] 修复需要后续有来源的互动，而不是时间到自动归零；
- [ ] 关系状态进入 NPC 自主考虑、主角 CharacterInterior、Life 与 Recall；
- [ ] 关系只改变模型看到的情境和机会权重，不形成硬行为许可。

## 6. NPC 缺口四：生命周期与人生阶段没有闭环

### 6.1 当前问题

reviewed contextual NPC 可以随传记 context tags active/retired；动态 NPC 的通用路径主要只有
register。它们通常没有组织、住处、Life Arc、可用时间和退出条件，因此容易永久 active，
又长期不发生任何事情。

### 6.2 TODO：可撤销能力与状态变化

- [ ] 动态 NPC 可在已结算结果中获得来源绑定的组织、地点、角色和联系渠道；
- [ ] 这些能力具有 scope、有效期、privacy、source ref 和撤销方式；
- [ ] NPC 可拥有自己的 Dynamic Life Arc，但不得从预设职业/人生路线菜单中选择；
- [ ] Life Arc 或已结算 outcome 可提出搬家、毕业、换工作、旅行、暂时离开等客观变化；
- [ ] NPC Actor Model 决定其可控选择，World Author 决定不由其控制的外部结果；
- [ ] 退出主角当前生活空间时进入 dormant/departed，而不是删除；
- [ ] 重新联系、共同组织重叠、地点重逢或开放约定可以使其恢复；
- [ ] retired/departed 不代表死亡、绝交或不再存在，除非有对应已结算事实；
- [ ] 新人生阶段应重新计算可用地点、日程、组织与事件空间；
- [ ] 迁移时关联 Plan、Commitment、关系和共同事项应逐项有来源地保持、修订或结算，不能
  机械清空。

### 6.3 验收

- [ ] 实习阶段生成的同事可以在实习结束后自然淡出，也可以因真实关系继续联系；
- [ ] NPC 搬家后不会继续被当前地点能力错误选中；
- [ ] dormant NPC 的历史仍可 Recall，重新出现时保持同一身份；
- [ ] 冷重放、CAS 冲突、失败恢复后没有 active/departed 双重状态；
- [ ] 新 NPC/组织/地点无需开发者增加剧情类型枚举即可走完整通路。

## 7. NPC 与主角、用户及事件机的双向因果

- [ ] 主角与用户争吵形成的已接受 Affect/关系变化可进入 Life 和 NPC 社会情境；
- [ ] NPC 可以基于自己的关系与近况选择关心、回避、谈心或 no-op，不能由“用户吵架”规则
  直接映射；
- [ ] NPC 事件结算后打开主角 Appraisal，主角可以感到高兴、烦躁、担心、无所谓或其他自由
  解释；
- [ ] 主角 Affect、Memory、Aspiration 和关系变化进入下一次 Life/NPC consideration；
- [ ] NPC 对主角的反应也更新 NPC 自己的关系与记忆，而不是只更新主角；
- [ ] 用户提到地点或人物只能成为带来源的灵感/注意材料；真正去过、见过必须经过 Plan、
  occurrence 和 settlement；
- [ ] 外界感知可影响 NPC 和主角，但 Signal/Perception 不能直接生成关心消息或固定剧情；
- [ ] 主角公开表达、未发送想法、NPC 私有状态和用户事实保持不同认识论权限；
- [ ] 同一因果链在 health 中可追踪：stimulus → NPC consideration → occurrence/action →
  settlement → 双方 appraisal/memory/relationship → next context。

## 8. 其他设计未充分落地的 TODO

### 8.1 重大生平迁移

- [ ] 明确基础传记坐标、Life Arc、普通 Plan/Experience 的职责；
- [ ] 让新地点、新职业、新组织和新 NPC 从已结算 Proposal 获得长期、可撤销 Capability；
- [ ] 原子重配校历、日程、资源、住所、关系和事件空间，避免毕业后仍在寝室上课；
- [ ] 不建立创业、就业、退学等里程碑目录；任意开放方向使用同一 effect contract；
- [ ] 补齐生产 producer、consumer、冷重放、补偿和 health 证据；
- [ ] 当前生产尚无 `LifeArcChanged` 证据，增加真实但隔离的长期演化验证。

### 8.2 Aspiration、Goal、Thread、Commitment

- [ ] 修复开放 Life 模式下旧 Aspiration seeding runtime 被关闭、又没有新开放 producer 的
  装配缺口；
- [ ] Aspiration 由角色模型基于自身经历自由形成，不从 reviewed wish seed 菜单抽取；
- [ ] Goal、Thread、Commitment 建立生产 producer，而不只是 schema/reducer/fixture；
- [ ] Thread 表达值得跨对话继续的主题；ResponseExpectation 只表达等待回应，二者不混用；
- [ ] Commitment 的形成、履行、破裂和释放都进入 CharacterInterior；
- [ ] 生产 health 区分“模型没有形成愿望”和“该 producer 根本没有运行”。

### 8.3 Character Core 与长期人格变化

- [ ] 为生产世界初始化来源明确的 `CharacterCore`，不再让
  `InnerLifeSnapshot.stable_identity` 依赖外部 prompt 的隐式人格；
- [ ] Character Core 区分不可变身份、运营者治理字段、慢变化倾向和值得长期保留的自我认识；
- [ ] 慢变化只能由跨场景、跨时间的已结算证据支持，单次情绪不能改写人格；
- [ ] 人格变化由角色模型提出，系统验证证据窗口、变化幅度和治理权限；
- [ ] prompt personality 逐步改为 Character Core 的 Adapter，不保留第二真相源。

### 8.4 用户关系持续吸收

- [ ] 调查大量互动只形成一次 relationship signal 的原因；
- [ ] 区分模型合理 no-change、触发器未消费、解析失败和 accepted effect 未提交；
- [ ] 让普通持续互动、可靠回应、失约、修复和长期互惠有机会改变关系；
- [ ] 不用“每 N 条消息增加亲近度”的机械规则；
- [ ] 关系状态进入主动节奏、CharacterInterior 和表达 Context，但不直接命令行为。

### 8.5 记忆成熟与可靠性

- [ ] 修复 Experience Memory 连续 `invalid_output` 重试；
- [ ] 同一失败进入 10/30/120 分钟技术退避，避免无效高频重试；
- [ ] 建立 MemoryCandidate 的强化、合并、淡忘、矛盾和降权生命周期；
- [ ] 保持用户事实、角色经历、NPC 共同经历和私人印象的不同权限；
- [ ] NPC 与主角复用同一 Recall 基础设施，但使用 actor-scoped corpus；
- [ ] 评测召回是否真的进入模型调用，而不是只证明 embedding 命中。

### 8.6 外界感知覆盖

- [ ] 逐个解决已登记国内来源的许可、可审计 acquisition 和启用状态；
- [ ] 配置感知 embedding 或明确证明 lexical-only 足够；
- [ ] 验证 External Perception 对 Life、Affect、Memory、NPC 和 Social Initiative 的下游影响；
- [ ] 来源修正/撤稿不删除角色已经看见并产生的历史反应；
- [ ] 外界热点不直接命令角色发消息。

### 8.7 图片链路

- [ ] 用真实生产隔离世界验证 planning → render → inspection → Action → receipt；
- [ ] 区分角色没有选择图片、没有视觉证据、授权失败、render 失败和 delivery 失败；
- [ ] NPC/活动/地点的可见事实可以生成图片候选，但不能由图片机补造经历；
- [ ] 完成稳定视觉身份训练或 FaceID/LoRA 等等价能力前，不宣称长期人物外貌一致；
- [ ] 图片分享后的期待和回应复用统一内心，不新增媒体专用心理旁路。

### 8.8 主动联系、Life 与表现可靠性

- [ ] 修复主动联系 claim/source binding 等高频技术失败，目标是合法角色决定一次成功进入
  Action 链；
- [ ] Life source-review 完成 production qualification，修复连续 novel-origin contract
  失败；
- [ ] 技术失败不能写成角色 `silent/no_op`；
- [ ] 首段延迟、API 外开销、流式候选和可见回执必须采集真实样本；
- [ ] health 为 0 个样本时保持 `not_measured`，不能宣称达标；
- [ ] 用真实多轮对聊和长期观察验证体验，不以单元测试绿灯替代。

## 9. 统一人物内心：现状判断

生产现状仍是多个角色语义入口和上下文拼装点。`current-self-state.1` 只解决过一部分紧凑读取，
普通入站、主动联系、Life、Affect/Relationship/Private Impression 后处理、媒体和感知仍可能
分别构造角色上下文或调用角色 Adapter。它们虽然复用同一账本，却没有共同的 `InnerTurn`
身份，也不能保证在同一 cursor 上看见同一个完整人物。

最终结论由 ADR-0016 固定：

- 统一的是主观整合与角色选择的生产边界，不是把 typed psychological authorities 合成一个
  可变对象；
- `InnerLifeSnapshot` 是 canonical read model，不是新账本或第二真相源；
- `PrivateTurnState` 是每个 `InnerTurn` 内由模型形成的瞬时私人自我，不是耐久事实；
- Appraisal、Affect、Relationship、Memory 等现有 event/reducer 保持各自权限，但新的角色
  提议只能经 `CharacterInterior` 路由；
- `current-self-state.1` 只保留历史 replay，不能成为兼容生产旁路。

详细设计、八项 Faculty、不变量、迁移删除矩阵和发布验收以
`docs/design/unified-character-interior.md` 为唯一实施基线。

## 10. `CharacterInterior` 深 Module 设计

### 10.1 最终 Interface

旧的 `snapshot/reflect` 分阶段方案作废，不保留兼容别名。最终生产 Interface 一次性固定为：

```python
class CharacterInterior:
    async def project(
        self, subject: InteriorStimulus | InteriorOpportunity
    ) -> InnerLifeSnapshot: ...
    async def experience(self, stimulus: InteriorStimulus) -> InnerTransition: ...
    async def consider(self, opportunity: InteriorOpportunity) -> InnerDecision: ...
```

- `project` 只做同一 actor/cursor/scope 的确定性 canonical projection，不调用模型；
- `experience` 让角色吸收一个已提交且不要求立即外部行为的刺激，并向既有 typed authority
  提出零个或多个主观变化；
- `consider` 在一个 `InnerTurn` 中处理需要角色作选择的机会，可同时吸收当前刺激、选择
  Recall、形成最终 `PrivateTurnState`、提出内在变化并返回业务 decision；
- 带即时表达的 Observation 只调用 `consider`，不得再并行调用独立 appraisal/affect 角色
  作者。

### 10.2 本轮必须纳入的八项 Faculty

- [ ] 瞬时私人自我与当前注意；
- [ ] 由角色当前自我、情绪、关系与目标控制的选择性 Recall；
- [ ] Appraisal 与 Affect 使用同一私人上下文；
- [ ] 连续情绪、余波、抑制、恢复与再解释；
- [ ] 方向明确、可错且有来源的主观关系取向；
- [ ] Aspiration、Goal、价值张力与内在冲突；
- [ ] 开放的自主冲动与 Action intention，不建立动机枚举；
- [ ] ExpressionDraft 必须消费同一 `InnerTurn` 的最终内在立场。

这些 Faculty 只提供材料和模型能力，不规定处理顺序、行为类别或输出结果。完整语义见统一设计
第 5 节。

### 10.3 权威与 NPC 复用

- [ ] 不新增覆盖全部心理状态的事件；现有 Appraisal、Affect、Relationship、Memory、Goal 等
  reducer 继续拥有最终状态；
- [ ] `CharacterInterior` 只拥有 canonical join、角色侧整合、选择性 Recall、模型身份与 typed
  proposal routing；
- [ ] World Author、事实、隐私/同意、安全、Action、媒体 render/inspection、perception
  acquisition 和 receipt 保持外部硬边界；
- [ ] 主角合同从第一天 actor-scoped；NPC 保留成本受控、actor-isolated 的自主域，不复制
  主角完整 Interior，主角也不能读取 NPC 私有 capsule；
- [ ] NPC 自主判断绑定自己的 actor ref、私有 capsule 和关系方向；其结果以来源绑定 stimulus
  进入主角 Interior。若未来为某个 NPC 启用完整 Interior，必须是独立 actor-scoped 实例；
- [ ] 测试 Adapter 可固定返回值，生产不得用固定话术或另一角色模型伪造成功。

### 10.4 不保留兼容旁路

- [ ] `current-self-state.1` 只由历史 codec 读取，新生产不生成该 key；
- [ ] 删除普通入站、Affect、Relationship、Private Impression、Proactive、Life Character、
  Media 和 Perception 的直接角色 Adapter/模型参数；
- [ ] 有价值的旧算法只能改名迁入 Module 私有实现，不能留下 public wrapper；
- [ ] 新 Expression、Life、主动、媒体和注意 decision 都绑定 `inner_turn_id + snapshot_hash`；
- [ ] 架构测试禁止 production import replay package、旧 Adapter 或重复 current-self builder；
- [ ] health 中 legacy invocation、parallel role author 和 dual write 必须为零。

## 11. 迁移顺序

`CharacterInterior` 不按“先保留旧接口、逐步分流”的方式上线。开发可以按下列顺序完成，但同一
生产版本只有在所有 production caller 都切换且旧入口删除后才允许发布。

### I0：归档、合同与红测试

- [ ] 保存并推送切换前 commit，建立可恢复基线；
- [ ] 固定 ADR-0016、统一设计、domain vocabulary 与三方法 Interface；
- [ ] 为八项 Faculty、同 cursor snapshot、Inner Turn、错误语义和历史冷重放建立 Interface red
  tests；
- [ ] 建立 AST/import/signature guard，列出所有旧角色 Adapter、builder 参数和 Context key。

### I1：深 Module 与 canonical self

- [ ] 实现 `project/experience/consider`、`InnerLifeSnapshot`、`InnerTurn` 和稳定 identity；
- [ ] 把选择性 Recall、私人状态、一次受约束重选和 typed proposal routing 收进 Module；
- [ ] 让当前 Observation 的 Appraisal/Affect 与 Expression 共用同一 turn；
- [ ] 保留各领域 event/reducer，不增加通用 mind-state write authority。

### I2：迁移所有生产消费者并删除旧入口

- [ ] 迁移 Chat、Proactive、Life Character、Appraisal/Affect、Relationship、Private
  Impression、Media 与 Perception；
- [ ] 删除旧 public Adapter、production builder 参数、composition 字段、runtime worker、配置和
  direct role provider call；
- [ ] 新 decision 强制携带 inner-turn lineage；
- [ ] 将旧 `current-self-state.1` 和旧 wire 隔离到 historical replay package；
- [ ] 全仓 guard 证明没有旧接口、新旧并行、双写或 silent fallback。

### I3：兼容、完整验证与原子切换

- [ ] 在生产库副本比较 event/head hash、关键 Projection、Action 与冷重放；
- [ ] 验证旧 terminal decision 不重开、已授权 Action 不重发、未完成旧 lease 安全释放；
- [ ] 运行相关测试、完整测试、静态检查和双路 code review；
- [ ] 重启 production daemon，验证 QQ、主动、Life、NPC、媒体、感知和 recovery；
- [ ] 使用真实 daemon/model 多轮对聊验证延迟、同批多消息、情绪即时性、Recall、打断与自然度；
- [ ] health 证明 legacy invocation、parallel role author、dual write 全部为零。

### I4：继续未完成的 NPC/世界闭环

- [ ] 完成主角→NPC 独立慢关系、动态 NPC Life Arc 与 dormant/reappearance；
- [ ] 完成本地 NPC 模型 qualification、共享批次和高 materiality 路由；
- [ ] 激活开放 Aspiration/Goal/Thread/Commitment producer 与 Character Core 长期修订；
- [ ] 完成重大生平迁移、外界感知、媒体和主动联系的长期体验验收。

## 12. 总体验收

### 12.1 NPC 像一个持续存在的人

- [ ] 新 NPC 生成后无需开发者写新剧情即可再次出现在开放事件中；
- [ ] NPC 可以有自己的近况、目标和困难，也可以长期没有联系；
- [ ] 主动邀请、疏远、求助、误会、修复、离开和重现都能发生，但测试不要求固定一种；
- [ ] 不利事件与无事发生都是合法结果；
- [ ] 同一 NPC 的身份、关系和记忆跨重启连续；
- [ ] 不同 NPC 的私人状态和冲突不会串线；
- [ ] 冷 NPC 恢复时保留历史，不像新创建的人。

### 12.2 成本可控

- [ ] 空闲 NPC 不产生高频模型调用；
- [ ] 调用规模接近“有意义的 NPC 刺激/场景数”，而不是“NPC 数 × 时钟 tick”；
- [ ] 本地模型承担大部分低风险考虑或压缩，云端只用于高 materiality 场景；
- [ ] 每日 token、云端调用、每个场景成本、缓存命中和升级率可观察；
- [ ] 成本限制只改变机会和模型路线，不替 NPC 作出沉默或固定行为决定。

### 12.3 人物内心真正统一

- [ ] 所有主角决策消费者绑定同一 cursor 的 `InnerLifeSnapshot` 与 `InnerTurn` identity；
- [ ] 世界事件、NPC、用户互动和外界感知能通过 `experience/consider` 影响后续 typed state；
- [ ] 八项 Faculty 全部可用，Affect、关系、记忆、愿望和近期经历可以相互影响，但不形成
  硬编码行为矩阵；
- [ ] `PrivateTurnState` 仍是模型自由形成的瞬时私人自我，Recall 后由同一 turn 重新形成；
- [ ] ExpressionDraft、主动联系、Life、媒体和注意选择不能绕过最终私人状态；
- [ ] 任何可见事实和外部 Action 仍通过完整来源与授权链；
- [ ] 删除 `CharacterInterior` 时复杂度会重新散到所有调用点，证明该 Module 具有真实深度和
  Locality，而不是一层转发包装。

### 12.4 兼容与运行可靠性

- [ ] 历史 V2 events、旧 Model Results 与 `current-self-state.1` 冷重放字节等价且零 live model
  calls；
- [ ] 新生产不生成旧 contract，不 import replay package，不接受旧 builder/model 参数；
- [ ] 同一 opportunity 最多一个 live character author；新旧并行、双写和旧接口调用均为零；
- [ ] 技术失败不成为 silent/no-op/no-change，角色真正选择的沉默则保持 effect-once；
- [ ] 已授权 Action 在重启与切换后不重复生成或发送，旧迟到 provider result 不能越过新 cursor；
- [ ] daemon 重启、首次恢复、QQ 收发、主动、Life、NPC、媒体和感知入口均可运行。

### 12.5 体验验收

- [ ] 使用真实 daemon/model 路径连续运行至少 14 天；
- [ ] 人工检查 NPC 是否只会重复邀请、争吵或谈心；
- [ ] 检查 NPC 是否记得共同经历、是否会自然淡出、是否会因自己生活重新出现；
- [ ] 检查主角是否因为 NPC 事件形成可感知但不模板化的情绪和后续选择；
- [ ] 检查用户互动是否能间接影响角色日常，同时不存在关键词到剧情的硬映射；
- [ ] 对比本地 NPC 路线和云端路线的身份连续性、自然度和事实错误率；
- [ ] 直接对聊验证同批多消息、近期 Recall、情绪当轮体现、多 Beat、主动插话/被打断与首条延迟；
- [ ] 测试只能验证来源、连续性、开放选择、成本与恢复，不能固定要求某个 NPC 必须做某事。

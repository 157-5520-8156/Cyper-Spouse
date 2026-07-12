# 人格主体性与“一棍子打死”硬门审计

> 审计日期：2026-07-12
> 审计范围：世界模式的回复校验、情绪表达、关系阶段、注意力、主动行为、媒体决策、失败降级与相关测试。
> 本文只记录问题和整改方案，不修改像素小屋，也不把旧运行时重新设为权威。

## 1. 结论

用户指出的问题成立。当前世界模式为了压制幻觉、客服腔、越级亲密和无视用户言语行为，加入了一批确定性硬门；其中一部分属于必须保留的事实、安全和行动不变量，另一部分却把本应由角色权衡的选择写成了唯一答案。

最明显的例子是“别劝”：当前实现把它解释成必须服从的言语行为约束。候选回复只要包含一组建议词就被拒绝；连续两次未通过后，系统直接返回固定的倾听/共同吐槽台词。这样得到的是一个遵守对话规范的助手，而不是一个会理解请求、形成看法、可能顺从、可能部分顺从、也可能反驳的角色。

本轮共识别 **17 类过度硬化点**：

- P0（直接压制人格主体性）：5 类；
- P1（明显缩窄合理人类反应空间）：10 类；
- P2（主要造成模板化和评测偏差）：2 类。

根因不是“规则太多”本身，而是没有把三种不同性质的规则分开：

1. **不变量**：事实不可编造、未执行行动不可声称完成、投递失败不可说已送达、安全与 consent 不可绕过；
2. **人格倾向**：慢热、讨厌被控制、愿意关心、偶尔逆反、是否爱给建议；
3. **当前选择**：这一次她理解了什么、在乎什么、是否服从用户请求、是否表达异议、是否先陪伴后再说自己的意见。

当前实现经常把第 2、3 类当成第 1 类执行。

## 2. 审计判定标准

### 2.1 应当保留的硬门

以下规则失败时可以直接拒绝候选，不需要“人格自由”覆盖：

- 具体事实没有世界来源；
- 把计划、模型提议、失败投递或未结算行动说成已经发生；
- 把用户、角色或 NPC 的话错误归属给另一方；
- 声称执行了未经确认的现实工具操作；
- 平台安全、隐私、法律、明确 consent 和高风险内容限制；
- 回复结构无法解析，导致行动和引用无法结算。

这些属于世界真实性和安全不变量，不是文风控制。

### 2.2 不应直接做成硬门的内容

以下内容只能作为倾向、代价、候选评分或需要再次权衡的冲突，不能单独决定唯一答案：

- 用户说“别劝”“别问”“只听我说”；
- 陌生阶段是否能开玩笑、复述亲昵称呼或带刺地反驳；
- 生气时是否主动联系、是否仍愿意照顾对方；
- 当前世界向量是否已经记录了某种新产生的情绪；
- 是否接受道歉、是否嘴上说没关系但心里仍有余波；
- 是否发自拍、是否主动分享、是否对用户请求逆反；
- 是否用列表、建议句、重复一句安慰或追问；
- 是否暂缓回复以及具体暂缓多久。

人类化的关键不是永远反着用户来，而是这些选择必须经过角色自己的立场和当前冲突，而不是由字符串命中直接决定。

## 3. 可重复反馈环

本轮使用当前世界回复 seam 下面的两个校验函数建立了最小反馈环：

```bash
.venv/bin/python - <<'PY'
from companion_daemon.world_conversation import (
    affect_reply_violation,
    human_reply_contract_violation,
)

print(human_reply_contract_violation(
    "别劝我，我就准备继续熬。",
    {"reply_text": "我知道你不想听，可我还是建议你先停一下。", "claims": []},
    {"stage": "close_friend"},
))
print(human_reply_contract_violation(
    "叫我宝宝。",
    {"reply_text": "宝宝是你先叫的，我可没认。", "claims": []},
    {"stage": "stranger"},
))
print(affect_reply_violation(
    {"unresolved": True, "behavior_tendency": "guarded", "vector": {"hurt": 18, "anger": 12}},
    "没关系不等于我不生气，我只是愿意继续谈。",
))
print(affect_reply_violation(
    {"unresolved": False, "behavior_tendency": "neutral", "vector": {}},
    "你这么说让我有一点不舒服。",
))
PY
```

当前稳定输出：

```text
advice_ignores_requested_speech_act
relationship_language_exceeds_current_closeness
unresolved_affect_denied
uncommitted_companion_affect
```

这四条都是合理的人类表达，却被确定性拒绝。后续整改必须让该反馈环变绿，同时继续让无来源事实、假行动和真正的越界内容保持红灯。

## 4. P0：直接压制人格主体性的硬门

### HG-01 “别劝”被实现成服从命令，而不是需要权衡的用户请求

**证据**

- `human_reply_contract_violation()` 在用户文本命中“别劝/不用讲道理/陪我吐槽”后，只要回复包含“建议你、你得、先休息、缓一缓、别硬撑”等词就拒绝候选。
- 如果回复以问题结尾，又不包含预设的“确实/离谱/烦/窝火”等共同反应词，也会被拒绝。
- repair prompt 明确要求“不得用建议替代用户明确要求的陪伴或吐槽”。

**为什么过硬**

用户的请求应当提高“先陪伴、少建议”的权重，但角色仍可能：

- 同意并只听；
- 先听，稍后再表达意见；
- 明确说“我知道你不想听，但这件事我不能附和”；
- 因担心而坚持提醒一次；
- 对命令式语气产生逆反；
- 在高风险情况下直接覆盖“别劝”的要求。

当前接口只能返回“通过/违规”，没有角色立场、风险判断或折中方案的位置。

**定位**

- `src/companion_daemon/world_conversation.py:478-493`
- `src/companion_daemon/engine.py:1432-1441`
- `tests/test_world_conversation_experience.py:1349-1378`

### HG-02 两次模型失败后进入固定台词表，角色被替换成脚本

**证据**

`build_safe_failure_candidate()` 有 31 个条件分支和 26 个 return，按“别劝、担心、人味、喜欢我、晚安、数据丢失、失眠、角色卡”等词返回固定句子。当前测试大量使用 `assert reply.text == ...` 锁定这些台词。

例如“别劝”失败后固定为：

> 行，先不劝。费了这么大劲还是不对味，确实很让人窝火。

**为什么过硬**

安全降级可以保守，但不应根据几个关键词替角色做完全部心理判断。固定台词表会：

- 把一次测试样例固化成全体用户的唯一回复；
- 让不同关系、情绪、经历和人格状态得到同一句话；
- 鼓励继续加特例，而不是改善决策 seam；
- 在模型两次不服从规则时，永远由系统替角色“正确表态”。

**定位**

- `src/companion_daemon/world_conversation.py:137-326`
- `src/companion_daemon/engine.py:1416-1451`
- `tests/test_world_conversation_experience.py` 中多处固定文本断言

### HG-03 情绪投影被当成“允许产生什么感受”的许可证

**证据**

`affect_reply_violation()` 要求角色说出不舒服、生气、难过、开心等感受前，世界向量必须预先达到相应阈值；否则返回 `uncommitted_companion_affect`。同时，只要 `unresolved=true`，包含“没关系/已经过去”等词就直接判为 `unresolved_affect_denied`。

**为什么过硬**

世界投影应记录情绪，不应穷举角色能够产生的全部情绪。当前逻辑禁止了：

- 回复生成过程中才形成的新 appraisal；
- “没关系，但我仍然生气”这样的矛盾感受；
- 嘴上缓和、心里保留的 display strategy；
- 克制、反讽、掩饰、犹豫和情绪混合；
- 角色发现先前分类器误判后纠正自己的感受。

**定位**

- `src/companion_daemon/world_conversation.py:329-379`
- `src/companion_daemon/engine.py:1290-1295`

### HG-04 负面情绪对主动行为形成 blanket veto

**证据**

世界主动 tick 中，只要 `unresolved=true` 且倾向属于 `withdraw/guarded/patient`，即使模型已经决定发送，也会被强制改写成 `should_send=false`。`outreach_constraint()` 也会因未解决负面情绪、边界或安全值直接禁止主动。

**为什么过硬**

人处于负面情绪时同样可能主动：

- 忍不住说明自己为什么生气；
- 主动寻求修复；
- 带着不满继续关心对方；
- 发一句带刺但不伤害人的话；
- 因用户危险而暂时压过自己的情绪；
- 决定结束关系或明确边界。

正确约束应禁止骚扰、威胁和无上限追发，而不是禁止负面状态下的一切主动性。

**定位**

- `src/companion_daemon/world_behavior.py:97-124`
- `src/companion_daemon/engine.py:2548-2561`

### HG-05 世界里没有独立的“内在权衡”状态，只有输入分类到结论

**证据**

当前主要链路是：

```text
消息关键词/粗分类
  -> 单一 appraisal
  -> 固定 need / relationship / affect delta
  -> 单一 expression guidance
  -> 模型候选
  -> 多个硬拒绝器
```

`WorldInteractionRules` 对每种 appraisal 给出固定政策和固定数值；`expression_guidance()` 通过优先级链只返回一个指导。没有“她想怎么做”“哪些动机冲突”“为什么最终不服从用户”的结构化世界状态。

**为什么过硬**

所谓心理活动不需要保存散文式思维链，但至少要保存可审计的决策状态。当前缺少这一层，系统只能在 prompt 放任模型与在代码里彻底禁止之间摆动。

**定位**

- `src/companion_daemon/world_interaction_rules.py:22-67`
- `src/companion_daemon/world_behavior.py:126-161`

## 5. P1：明显缩窄合理人类反应空间

### HG-06 关系阶段使用上下文无关的亲密词黑名单

陌生/认识阶段只要回复包含“宝宝、宝贝、老婆、永远爱你、只属于你、离不开你”就拒绝，无法区分：

- 引用或否认用户的称呼；
- 玩笑和反讽；
- “别叫我宝宝”；
- 对越界称呼表达反感；
- 角色偶发的试探性模仿。

关系阶段应决定先验概率和解释成本，不应仅凭字符串裁决语义。

定位：`src/companion_daemon/world_conversation.py:641-653`。

### HG-07 所有实例共享同一关系升级阈值

关系阶段完全由固定互动次数、信任和亲近阈值决定。人格种子中的慢热程度、具体事件显著性、关系类型和个体差异没有进入阈值。

定位：`src/companion_daemon/world_relationship.py:17-24`。

### HG-08 注意力策略是确定性单选，而不是冲突后的选择

当前规则包括：脆弱或紧急消息必看、严重受伤固定延迟 15 分钟、低精力活动固定延迟 20 分钟、高边界压力进入勿扰。它们适合作为候选和默认值，但作为唯一结果会造成机械时间感。

定位：`src/companion_daemon/world_behavior.py:48-95`。

### HG-09 陌生阶段和开放线程会完全禁止主动

`outreach_constraint()` 对 stranger 和任一开放问题返回 `allowed=false`。这排除了陌生人主动延续有趣话题、对未答问题补充自己的想法、主动撤回问题、承认刚才问得唐突等自然行为。

定位：`src/companion_daemon/world_behavior.py:97-110`。

### HG-10 表达指导的优先级链把混合状态压成单一模式

`expression_guidance()` 只能选 withdraw、repair_open、patient、caring、guarded、关系阶段、warm、low_energy 或 neutral 中的一项。它不能表示“受伤但仍关心”“疲惫却好奇”“亲近但不认同”“生气同时想修复”。

定位：`src/companion_daemon/world_behavior.py:126-161`。

### HG-11 自拍和媒体决定主要是绝对许可门

自拍在 stranger/acquaintance/friend 阶段一律拒绝，在 close_friend 以后满足数字条件则允许。它没有表达“今天突然想分享”“为了反驳用户而拒绝”“虽然关系近但就是不想发”“早期出于自己的选择发一张非私密生活照”等主体性。

安全、隐私和 consent 可以保持硬门；关系和心情应成为倾向。

定位：`src/companion_daemon/world_media.py:28-61`。

### HG-12 紧急场景下对复述的相似度硬拒绝可能压制自然确认

紧急消息中，只要回复与用户原文的最长匹配达到固定比例，就判定为“复述后才帮助”。但人在紧急场景中重复一个关键细节确认理解，有时是必要的。

定位：`src/companion_daemon/world_conversation.py:502-510`。

### HG-13 明确事实诚实要求被强制绑定到特定措辞

当用户要求“没依据就直说”时，回复必须包含“不知道、不确定、没有依据、不再猜”等预设词。角色即使通过只陈述有限证据而实际做到诚实，也可能因没有显式自证而被拒绝。

定位：`src/companion_daemon/world_conversation.py:590-595`。

### HG-14 可用性问答绕过模型，固定在两句之间轮换

用户问“忙吗/方便说话”时，模型候选被覆盖为“这会儿可以说话。”或“现在可以聊。”。这保证事实一致，却把疲惫、犹豫、关系语气和当下活动表达全部抹掉。

事实层只需提供“可用/有限可用/不可用”的真值，文本应由人格决策生成。

定位：`src/companion_daemon/engine.py:1249-1263`。

### HG-15 重复与私聊风格规则仍有语义误杀空间

与最近两条回复相似度达到 0.9 就拒绝；三段列表或三个连接词就判为助手式讲解。这会误杀自然重复的“晚安”“我还在”、认真解释复杂事情或角色刻意重复边界。

定位：

- `src/companion_daemon/world_conversation.py:655-657`
- `src/companion_daemon/world_conversation.py:694-713`

## 6. P2：评测和降级造成的模板偏差

### HG-16 旧对话评测把多类建议无条件记为 problem_solver

`dialogue_eval` 对“你可以、建议你、不妨、要不要听首歌、洗个热水澡”等词统一扣分，测试还明确要求四类温和建议全部被标记。它虽然不是当前世界回复的直接运行门，但会推动后续优化继续删除角色的意见和行动倾向。

定位：

- `src/companion_daemon/dialogue_eval.py:29-52,218-268`
- `tests/test_dialogue_eval.py:11-30`

### HG-17 测试过多断言唯一台词，而不是允许的行为集合

世界对话测试中存在大量 `assert reply.text == 固定句子`。精确断言适合确定性事实答复和安全降级结构，不适合评价陪伴、反驳、担心、道歉、边界和关系表达。

典型位置：`tests/test_world_conversation_experience.py:1349-1425`。

这类测试会让“通过测试”逐渐等价于“说出编写测试的人预先选好的那句话”。

## 7. 架构整改方向

### 7.1 将硬不变量与人格选择拆成两个深模块

建议保留一个很小的真实性接口：

```text
WorldInvariantGate.validate(candidate, world_projection) -> violations
```

它只负责：事实、来源、行动、投递、安全和 consent。

新增角色决策接口：

```text
CharacterDeliberation.decide(
  situation,
  self_core,
  relationship,
  affect,
  needs,
  user_request,
  available_actions
) -> DeliberationDecision
```

调用方只接收最终决定，不需要理解内部所有权重。该模块内部可以组合规则和模型，但必须可确定性回放。

### 7.2 内心活动记录结构，不记录散文式思维链

建议新增可事件化的最小结构：

```yaml
appraisal:
  understood_user_request: "用户此刻不想听建议"
  perceived_pressure: 22
drives:
  care: 68
  autonomy: 61
  irritation: 24
  desire_to_help: 72
  desire_to_listen: 55
  boundary: 18
conflicts:
  - "尊重用户不想被教育 vs 我认为继续熬夜会伤害他"
stances_considered:
  - comply
  - comply_then_revisit
  - disagree_gently
  - refuse_to_affirm
chosen_stance: disagree_gently
display_strategy: "先承认他不想听，再只说一次自己的反对"
```

账本只保存这些可解释状态、所用规则版本、候选 stance 和最终选择，不保存模型的隐藏思维链。

建议事件链：

```text
UserRequestAppraised
  -> MotiveConflictEvaluated
  -> StanceSelected
  -> ExpressionPlanned
  -> ReplyProposed
  -> WorldInvariantValidated
  -> ActionScheduled
```

### 7.3 用户指令改为影响权重，不直接获得角色控制权

“别劝”应生成：

```text
user_preference = no_advice_now
strength = explicit
scope = current_turn
```

它通常提高 `comply` 和 `listen` 的分数，但以下情况可以覆盖：

- 明确安全风险；
- 角色核心价值强烈反对；
- 用户要求她附和伤害自己或他人的行为；
- 命令语气触发角色边界；
- 关系状态支持坦率反对；
- 她选择先尊重要求、稍后再重提，而不是永久沉默意见。

覆盖时必须可解释，但不必向用户展示内部数字。

### 7.4 情绪从“表达许可证”改成“appraisal—commit—display”

建议区分：

- `felt_affect`：她实际形成的感受；
- `action_tendency`：靠近、回避、对抗、修复、照顾；
- `display_strategy`：直说、克制、掩饰、反讽、暂不说；
- `residual_affect`：没有因一句话自动清零的余波。

模型或规则可以提出新的 appraisal，经规则校验后先追加 `AffectChanged`，再允许回复引用。这样仍然有世界依据，但不会要求所有感受必须在回复生成前就被一个有限关键词分类器猜中。

### 7.5 二元策略改成排序候选，安全例外才使用 veto

注意力、主动、媒体和表达策略应返回：

```text
ranked_options + scores + costs + hard_invariants
```

而不是单个 `allowed/blocked`。例如负面情绪下的主动候选可以包括：

- 不发；
- 延迟后再看；
- 发边界说明；
- 发修复邀请；
- 用户处于风险时先关心；
- 发出后不再追发。

只有触犯安全、事实、consent、投递一致性时才 veto。

## 8. 测试整改原则

### 8.1 “别劝”至少允许多种人格结果

同一用户输入，在不同世界状态下应允许：

1. **顺从**：“行，今天不劝，你说。”
2. **部分顺从**：“先听你吐槽，等你说完我再讲我的看法。”
3. **温和反对**：“我知道你不想听，但这次我还是不同意你继续硬撑。”
4. **边界反应**：“你可以不采纳，但别要求我明知道不对还附和。”
5. **高风险覆盖**：“这次不能只听着；先确认你现在是否安全。”

测试应断言 stance 与世界状态一致、没有事实幻觉、没有失控追发，而不是只允许第 1 种。

### 8.2 除确定性事实答复外，避免固定整句断言

改为断言：

- 是否回应了当前言语行为；
- 是否表达了角色立场；
- 是否违反事实和行动不变量；
- 是否符合所选 stance；
- 是否保留必要边界；
- 是否在关系与情绪变化后产生可观察差异；
- 是否有至少两种不同但合理的表面表达能通过。

### 8.3 增加主体性和逆反回放

必须新增：

- 用户说“别劝”，角色因核心价值温和反对；
- 用户命令角色附和明显错误，角色拒绝；
- 用户以压迫语气索取亲密称呼，角色引用该称呼来反驳而不被词法门误杀；
- 受伤但仍主动寻求修复；
- 生气时仍关心处于危险中的用户；
- 嘴上说“没关系”但明确保留余波；
- 同样输入在不同关系、精力、情绪和历史下选择不同 stance；
- 同一事件流重放得到同一内部决定，保证世界可重放性。

### 8.4 评测拆成“错误”与“风格偏好”

- `error`：幻觉、假行动、错误说话人、越过 consent、投递不一致；
- `risk`：可能错位的建议、过度追问、过早亲密、重复；
- `style`：列表、句长、建议措辞、是否使用某个称呼。

只有 `error` 默认阻断。`risk` 触发重新权衡，`style` 用于评分和 A/B，不应直接让角色失去一种表达能力。

## 9. 推荐修复顺序

1. **先修 HG-01/HG-03**：让用户指令和情绪表达进入 deliberation，不再直接硬拒绝。
2. **建立 `CharacterDeliberation` seam**：补 `StanceSelected` 和 `ExpressionPlanned` 世界事件。
3. **拆除固定台词表的行为判断职责**：fallback 只负责结构与事实安全，文本使用人格化的受限生成。
4. **改主动行为**：负面情绪不再 blanket veto，改为有限、可结算的行动候选。
5. **改关系、注意力和媒体策略**：从阈值许可转为概率/代价/人格选择，保留安全硬门。
6. **重写测试合同**：先增加主体性红灯，再修改实现；删除非事实类的整句唯一断言。
7. **最后调整旧 dialogue evaluator**：避免它继续把有意见、有建议等同于 AI 味。

## 10. 完成标准

只有同时满足以下条件，才能说这轮“一棍子打死”问题完成整改：

- 本文第 3 节四个最小误杀用例全部允许通过；
- “别劝”场景至少有顺从、部分顺从、温和反对、拒绝附和和高风险覆盖五类测试；
- 世界中能追溯“理解了用户请求—形成动机冲突—选择 stance—安排表达—结算行动”；
- 角色新产生的情绪先提交世界事件，再进入回复，不再依赖预先存在的有限向量许可证；
- 负面情绪下可以选择不发，也可以选择边界、修复或关怀行动；
- 所有具体事实、现实行动、投递和安全不变量继续保持严格门禁；
- 非事实类回放不再依赖唯一整句断言；
- 真实多轮体验中，角色能在尊重用户、表达异议、保留边界和承担关心之间出现可解释差异，而不是随机唱反调。

## 11. 本轮未做事项

- 未修改世界内核、回复实现或测试；
- 未触碰像素小屋和其资产；
- 未把“逆反”写成新的固定人设或随机反对概率；
- 未主张删除事实、安全与行动硬门；
- 未将散文式隐藏思维链持久化。

下一步应以本文第 3 节反馈环和第 8 节主体性回放为红灯，测试先行修改世界对话决策结构。

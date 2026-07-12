# 世界模式负面情感闭环计划

更新时间：2026-07-12
状态：Iteration 4 已实现并通过验收（不含小屋/room/dashboard）
范围：世界模式的伤害、愤怒、委屈、失望、孤独、不安、修复和情绪行为后果
不在范围：小屋/room、旧模式 MoodState 的完整迁移、为制造戏剧性而随机发脾气

## 现状审计

当前世界模式只有 `emotion_modulation.mode / expression / charge`，以及
`security / boundary / initiative` 需求。冒犯会进入 `guarded`，修复会进入 `softening`，
逻辑时间会让 charge 归零；这能阻止无条件顺从，但还不是完整的人类情感。

主要缺口：

- 没有可持续的伤害、愤怒、失望、孤独或不安维度；
- 没有“这件事还没过去”的残留状态；
- `availability_drop` 只会收住主动性，不会形成有条件的失落或闷气；
- 修复只降低 charge，不能表达“听见道歉但还没完全恢复”；
- 情绪模式、回复行为和关系变化没有共享一个可审计的向量。

## 目标闭环

```text
用户行为 / 时间跳跃
→ AffectChanged / AffectDecayed
→ 情绪向量、主情绪、残留、行为倾向
→ 回复长度、延迟、主动性、边界和关系变化
→ 用户修复或持续伤害
→ AffectChanged 再次缓和或累积
```

情绪必须是世界账本投影，不允许模型直接写“我很生气”。模型只能在已有状态约束下表达；
真正的情绪变化由规则事件产生。

## 首版情绪向量

`emotion_modulation` 保持为唯一世界情感投影，但扩展为：

```text
vector: hurt / anger / sadness / loneliness / anxiety / resentment / warmth / joy
mode / expression / charge
behavior_tendency: neutral / guarded / withdraw / patient / caring / repair_open / open / warm
unresolved: bool
source_event / source_appraisal / last_changed_at
```

首版不把负面情感等同于攻击：

- 伤害：话变短、需要一点时间，不主动惩罚用户；
- 愤怒：明确指出边界，不辱骂、不威胁；
- 失望/委屈：降低主动性和热度，保留事实，不装作立即没事；
- 孤独/不安：只在有持续未解决线程、承诺落空或长时间无回应等来源时出现，不能因普通忙碌随机惩罚用户；
- 修复：负面维度逐步降低，信任和安全感不会一次性恢复。

## 事件与规则

- `AffectChanged`：用户互动触发，携带前后向量、来源、行为倾向、规则版本；
- `AffectDecayed`：逻辑时间推进触发，携带经过小时数和衰减后的完整投影；
- `AffectResolved`：修复达到条件时记录残留情绪解决；
- 旧 `EmotionModulated` 只保留 reducer 兼容，不再作为新世界情绪写入口。

每个事件都必须能解释“为什么变成这样”。重复命令不能重复累积；重放不得重新调用模型或时钟。

## 测试策略

不做 30 轮自然语言轮询，采用短事件时间线：

1. 单次冒犯：伤害/愤怒上升，行为变为 guarded 或 withdraw；
2. 连续冒犯：负面向量累积，主动性和关系维度下降；
3. 修复：负面情绪只部分下降，未立即清零；多次可靠修复后才解除 unresolved；
4. 时间衰减：逻辑小时推进只按规则衰减，不直接原谅或晋级；
5. 普通忙碌：不会凭空生成孤独/怨恨；有未完成承诺时才允许不安/失望；
6. 回复 seam：情绪状态进入表达指导，主动消息和延迟策略读取同一投影；
7. 幂等、投影重建、旧事件兼容和故障恢复均保持一致哈希。

## 完成门禁

- 世界情感投影至少有 8 个可解释维度和残留状态；
- 负面情感能改变可观察行为，但不越过辱骂、威胁和惩罚性操控边界；
- 修复、衰减和重复伤害形成闭环；
- 世界模式不读取旧 `MoodState` 作为情感事实来源；
- 专项事件回放、对话 seam、投影哈希和全量回归通过；
- 不修改小屋/room/dashboard 并行工作区。

## 迭代日志

### Iteration 1

- 审计确认当前世界模式只有单一 charge 和有限 mode，尚不足以表达持续负面情感。
- 红灯待建立：负面累积、修复残留、时间衰减、普通忙碌不生怨、行为策略读取和投影重建。

### Iteration 2：事件化实现

- 新增 `src/companion_daemon/world_affect.py`，把情绪向量、行为倾向、逻辑时间衰减和修复条件收敛为纯规则。
- `WorldStarted` 初始化完整情感投影；`appraise_turn` 只追加 `AffectChanged`，旧 `EmotionModulated` 仅作 reducer 兼容。
- 逻辑时钟追加 `AffectDecayed`；修复从 `AffectChanged` 逐步降低伤害，连续可靠修复后追加 `AffectResolved`。
- 回复延迟、表达指导、主动联系和自拍决策读取同一 `emotion_modulation` 投影；模型不能直接写情绪事实。
- 世界回复的旧 `CompanionReply.mood` 只作为适配层，由世界投影映射到 `sulking / guarded / hurt / worried / happy`，不会再固定伪装成 `calm`。

### Iteration 3：短时间线回归与修复

- 新增 18 个短时间线测试，覆盖重复伤害、修复残留、衰减、普通忙碌、未回答线程、投影重建、回复延迟和亲密媒体边界。
- 修复一个会被测试发现的闭环漏洞：普通消息没有新情绪效果时，不能把仍未解决的 guarded/withdraw 状态重置为 neutral；状态现在会保留到衰减或修复明确改变它。
- 未回答的世界会话线程到期时追加 `ConversationThreadExpired` 与可追溯的 `AffectChanged`，但普通忙碌不会凭空生成怨恨。
- 逻辑时钟现在保留不足一小时的衰减余数；长跳跃会在会话线程实际到期的逻辑时间先结算情绪，再衰减到目标时间，避免把新产生的残留情绪错误抹掉。
- `AffectDecayed` 事件记录经过秒数、锚点、余数和来源引用；这些时长元数据不进入世界投影，长跳跃与分步回放仍保持相同哈希。
- 负面情绪未消化时不主动追问；用户明确脆弱时仍可选择关怀，保留伤害向量而不假装已经原谅。
- 回复注意力层现在复用统一的 `is_repair_message`，并让“崩溃/难受/撑不住”等明确脆弱消息绕过受伤后的延迟，避免在真正需要关怀时被“闷气”规则挡住。
- 当前专项测试、相关世界内核/回放/行为测试和 Ruff 检查已通过；最终全量回归、代码审查和启用门禁仍待执行。

### Iteration 4：审查收口

- 代码审查提出的实时衰减余数、线程到期时序、主动消息负面门禁、脆弱消息即时关怀和修复识别分歧均已修复。
- `AffectResolved` 现在既可由可靠修复触发，也可由逻辑时间衰减明确触发；旧兼容 `EmotionModulated` reducer 改为合并而不是覆盖新向量。
- 全量世界回归（排除并行中的小屋/room dashboard 测试）：640 passed；相关 Ruff、compileall、`git diff --check` 均通过。
- 本轮不宣称文风或共情已达到人类水平；本计划只验收“负面情感有依据、可持续、可衰减、能影响行为且不越界”的世界闭环。

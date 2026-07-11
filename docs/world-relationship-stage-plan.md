# 世界模式关系阶段迁移计划

更新时间：2026-07-12
状态：Iteration 2 完成，等待提交
范围：世界模式中用户与沈知栀的关系阶段、阶段策略和事件回放
不在范围：小屋/room、NPC 之间关系、文风重写、恋爱剧情自动生成

## 现状审计

旧关系模块已有 `stranger → acquaintance → friend → close_friend → ambiguous → lover`，
但它依赖 `MoodState` 和旧消息计数。世界模式虽然已经把 `RelationshipChanged` 写进账本，
并维护 `closeness / respect / reliability`，却没有把阶段写入世界投影。

因此当前状态是“关系数值会积累，阶段不是世界事实”。dashboard 还会把阶段显示为
`world_projected`，不能作为对话策略的权威输入。

## 目标闭环

```text
UserMessageObserved / TurnAppraised
→ RelationshipChanged（维度变化）
→ RelationshipStageEvaluated（同一事务追加）
→ world.relationships[user_id].stage
→ 对话表达、主动性、媒体权限读取同一阶段
→ 下一次互动继续从账本推进
```

关系阶段必须是可重放投影，不允许模型直接建议阶段，也不允许读取旧 `MoodState.relationship_stage`
来覆盖世界状态。阶段变化只由规则版本、关系维度、有效互动计数和边界状态决定。

## 阶段与门槛

首版保留旧系统的语义，但把阈值改为世界投影可解释规则：

| 阶段 | 最低有效互动数 | 信任 | 亲密 | 说明 |
| --- | ---: | ---: | ---: | --- |
| stranger | 0 | 0 | 0 | 刚认识，礼貌和边界 |
| acquaintance | 4 | 18 | 0 | 开始熟悉，不主动暧昧 |
| friend | 12 | 25 | 18 | 可以自然关心 |
| close_friend | 35 | 45 | 35 | 更坦诚，允许轻微小脾气 |
| ambiguous | 70 | 55 | 55 | 克制的在意，不宣布恋爱 |
| lover | 120 | 70 | 75 | 只有明确长期积累后才开放恋人语气 |

有效互动数只统计已观察、已处理的用户回合；重复幂等命令不能重复计数。
关系阶段默认只向前晋级。信任/尊重显著下降或边界升高时允许降级，但一次事件最多降一级，
并追加原因与规则版本；时间流逝本身不会自动增加亲密度。

`reliability` 继续作为关系投影和主动性依据记录，但首版不直接作为阶段晋级门槛；
阶段门槛使用互动数、信任和亲密，尊重/边界负责回退。这样阶段语义仍与旧模块一致，
同时不丢失可靠性维度的审计数据。

## 策略读取

- 世界回复 prompt 读取 `relationship.stage` 和维度，不再读取旧 `MoodState` 阶段。
- `WorldBehaviorPolicy.expression_guidance` 对阶段给出表达边界：陌生/熟人不越级，朋友可自然关心，
  亲近朋友允许更坦诚，暧昧/恋人仍受边界和事实审计约束。
- 主动联系和自拍/亲密媒体权限使用同一阶段投影；阶段变化不会绕过已有安全边界。
- dashboard 只展示世界阶段，不再显示 `world_projected` 占位词。

## 确定性测试策略

不做 30 轮自然语言轮询，改用公开世界接口的短事件时间线：

1. 纯规则表：固定维度和互动数得到唯一阶段；边界升高、信任下降验证最多降一级。
2. `WorldKernel.submit(appraise_turn)`：每次事件同时产生关系变化和阶段评估，检查 revision、事件顺序和投影。
3. 幂等回放：重复同一 `idempotency_key` 不增加互动数、不重复晋级。
4. 全量重放：从空投影重建后，关系阶段、维度和哈希与在线投影一致。
5. 对话 seam：在陌生、朋友、暧昧三个阶段分别验证提示上下文和越级口吻门禁。
6. 失败路径：降级/审计失败不改变关系阶段；旧世界表和旧 `MoodState` 不参与世界判定。

## 完成门禁

- 世界初始注册用户明确为 `stranger`；阶段存在于事件和投影中。
- 至少覆盖前进、停滞、一级降级、幂等、重放哈希和对话策略读取。
- 世界模式不再出现 `world_projected` 关系占位值。
- 旧关系模块测试可保留，但真实世界聊天不依赖它。
- 不修改小屋/room/dashboard 并行工作区；最终提交只包含本计划、世界模块和关系测试。

## 迭代日志

### Iteration 1

- 审计确认：旧模块有完整阶段阈值；世界账本已有维度变化，但没有 canonical stage projection。
- 红灯待建立：初始阶段、阶段评估事件、幂等不重复计数、重放哈希和阶段策略读取。

### Iteration 2：世界阶段闭环实现

- `RelationshipStageEvaluated` 成为世界账本事件；注册用户初始化为 `stranger`，每次 `appraise_turn`
  同事务追加维度变化、互动计数和阶段评估。
- 新增 `world_relationship.py` 作为唯一阶段规则：每次最多晋级一级；尊重/边界/信任恶化最多回退一级；
  逻辑时间不会晋级；事件携带 trust、closeness、respect、reliability、boundary 指标快照与规则版本。
- 世界回复、表达指导、主动消息、afterthought 和自拍权限读取同一 `relationships[user_id].stage`；
  世界模式的过早亲密分类不再读取旧 `MoodState.relationship_stage`。
- 未注册用户或仍处于 `stranger` 的关系不能主动外发；主动文案和 afterthought 也经过阶段越级门禁。
- 确定性 seam 覆盖：陌生→熟人、朋友 prompt、阶段阈值、一级回退、幂等、逻辑时间不晋级、投影哈希、
  dashboard、自拍和主动文案；专项测试当前 `90 passed`。
- 全量 Python 回归（排除小屋/room/dashboard 测试）：`622 passed, 1 warning`；ruff、compileall、git diff --check 均通过。
- 本轮关系阶段专项与行为/引擎 seam：`90 passed`；代码审查确认未触碰小屋/room 并行改动。
- 下一步：仅暂存世界关系、行为、媒体、引擎和测试文档并提交；小屋/room/dashboard 工作区继续保持不暂存。

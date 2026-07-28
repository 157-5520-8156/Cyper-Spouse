---
status: accepted
---

# 用事件溯源的传记情境与 Life Arc 推动长期生活

Girl-Agent 的生活事件机采用两种不同尺度：出生日期、校历等已审核时间线在 Logical
Time 上派生 Biographical Context；实习、工作、住处、旅行等会改变数周或数月生活可能性
的结果，以 `LifeArcChanged` 进入不可变账本。日常 Plan 和 Experience 不承担长期身份，
静态角色配置也不再永久声明“20 岁、大二、住寝室”。

Life Arc 只能从已结算且明确声明了获准 effect 的结果开始。系统可以根据 Arc 和
校历确定性开放或关闭场所、NPC 与能力，也可以在声明的结束时间机械结算；但这些
变化只组成角色模型可见的 Context Pack，而不是有限剧情或活动菜单。互动、Appraisal、Affect、关系、Thread、
Commitment、用户事实、Aspiration 和近期自身 Experience 同样作为有来源的 Life
Influence 进入模型。系统不得把“吵架”“提到某地”等输入直接映射成找某人谈心、旅行或
其他行为。

## Consequences

年龄、季节、学期、寒暑假和毕业状态随世界时间推进，并以 `season:*`、
`calendar:*` 情境坐标进入候选机会和模型上下文，冷重放得到相同结果。阶段专属 NPC 和场所
通过账本状态进入与退场；例如模型选择并结算实习录用后，出版社、同事和工作活动可在
该 Arc 内出现，到期后退出。毕业关闭上课与校园住处语境，求职及后续工作阶段能够继续
推动世界。

一个用户提及的地点最多先成为带 Observation/Fact 来源的候选灵感；只有角色模型接受
并形成 Plan、活动生命周期实际推进、结果结算后，系统才能把它称为去过的 Experience。
随机性可以调节机会、时机和注意力，不得生成经历或替模型决定生活选择。

具体生活发展如何生成由 ADR-0012 约束：生产路径不得再从人工剧情 opening 或固定
outcome 中预选，Context Pack 只提供事实与 Capability。

新增长期阶段必须同时提供真实 producer、consumer、冷重放测试和健康投影；只增加
schema/reducer 而没有生产路径的 dormant authority 不得合并。

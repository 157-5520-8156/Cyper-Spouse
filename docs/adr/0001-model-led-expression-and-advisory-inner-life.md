---
status: proposed
---

# 模型主导表达，机制作为内心 Advisory

实时对话将由高能力模型主导语用理解和自然表达；Appraisal、Affect、Drive、Stance、
Display Strategy、关系与连续性机制改为有来源但可缺席的内心 Advisory，并通过选择性提交
影响长期状态。World 继续权威管理事实、身份、Plan/Experience、Action、回执、隐私、
安全与同意；只有这些 Hard Invariant 可以阻止输出。选择该方案是为了保留裸模型做不到的
长期内心和行动能力，同时避免软机制重新形成比裸聊更慢、更机械的逐层审批链。

## Considered Options

- 保持现有机制先决策、模型只负责措辞：可解释但已证明容易压制正确语用并放大延迟。
- 回退为裸模型聊天：即时自然，但失去 World 共时、Action 结算、口是心非后的真实残留和
  长期连续性。
- 采用本 ADR：接受表达的概率性，把确定性集中在真正不可妥协的 World 与 Action 语义。

## Consequences

普通聊天应收敛为一次主模型综合和一次本地 Hard Invariant 裁决；内心机制失败时退化到
自然裸聊路径。结构化 Private Impression、Private Commitment、Conversation Thread 和
Affect episode 可以持久化，但自由文本 chain-of-thought 不进入权威账本。迁移必须删除旧
审批链而不是在其外再叠一层新 Interface。

---
status: accepted
---

# 用开放的 World Author 与 Character Model 推动生活发展

Life Ecology 对外保留一个按已提交 wake 推进的深模块，但内部区分两种语义
权威：World Author 自由提出环境机会、突发、新人物和客观结果；Character Model
决定角色是否参与、如何回应、是否形成计划或长期方向。两者只产生 Proposal，
不能直接写入已经发生的事实；所有发展继续经过来源与能力闭包、CAS、
Occurrence、Settlement、Experience 和 Life Arc。

生产调度不再从人工编写的剧情 opening、NPC 事件类型、aspiration seed 或固定
outcome 文案中替模型预选生活。配置只保留起点事实、地点与资源可用性、时间、
隐私、同意、安全和可执行 Capability。已有目录事件继续可重放，具体条目可以
作为测试 Fixture 或检索素材，但不得作为角色下一步行为的有限菜单。

外部偶然性不能由 Character Model 选择，否则角色会变成自己编写好运与厄运的
作者；角色可控行为也不能由 World Author 决定。新 NPC 在结算前只是
Provisional NPC，长期变化只能由 Character Model 选择的已结算后果启用；
`world_contingency` 结果不得携带角色的主观长期方向。新地点可以先作为有明确
scope 的环境 claim 出现在本次事件中；本版本只有 manifest 已授权地点才能成为
后续 Plan 可复用的 typed location，不冒充已经注册的新地点。地点授权不是裸
ID：模型必须引用绑定地点、隐私下限、当地星期/时段或既有 Plan 精确窗口及来源
的稳定 Capability；完整 DueWindow 必须被覆盖，当前所在地点不能推出未来仍在
那里，未来目的地也不能授权当下瞬移。生产投影把“当前所在”视为一次来源绑定的
位置快照，其执行能力只覆盖该 wake 起五分钟内的完整 DueWindow；更晚的提议必须
等待新的投影/wake 重新证明当前位置，不能沿用旧快照推断尚未发生的停留。当前的 Outcome
Resolution Envelope 会冻结 World Author 针对本次具体情境临时生成的少量可能
结果、相对可行性及后果范围；它不是运营者维护的固定结局库，也不会预先选定
实际结果。这样既保留不确定性，也避免重启或供应商漂移悄悄改写过去。不利事件
与有利事件使用相同的事实、权限和后果预算校验，不因“不够积极”被系统拒绝。

Character Model 看到每个客观结果的完整文本，也会单独看到 World Author 随该
结果提出的主观长期方向、期限、标签和隐私。她需要分别选择客观结果以及是否把
该方向纳入自己的生活；选择某个结果不等于接受附带方向，系统也不会替她默认
接受。后者只有在显式选择后才会物化为 Dynamic Life Arc；客观产生的新人物仍按
已结算结果物化。

World Author 与 Character Model 的开放提议各自属于一个精确 Pinned Turn。每次
实际调用的 messages hash、非法首答、纠正答、raw sidecar、Context identity 和
决定对象 hash 都进入不可变审计；最终 life-development Proposal 引用这些审计
事件及 payload hash，而不是在审计提交后重新 pin 并把旧 raw 解释成新决定。审计
提交后只允许 Deliberation revision 前进，World revision 一旦变化便废止未接纳
的结果。崩溃恢复只能复用原 capsule、原 capability manifest、原决定对象和原始
sidecar 都能闭合的成功审计。地点 Capability 的完整快照也冻结进最终 Proposal，
其来源事件与 policy refs 必须随 Plan/Occurrence 一起进入 effect 证据闭包。

具有长期影响的 Character 选择以稳定的 occurrence/observation/candidate-matrix
identity 执行。每次模型调用保留自己的精确 request hash 和不可变 raw sidecar；
最终审计正文绑定候选 ref、方向选择、候选矩阵和 response hash。
`ModelResultRecorded`、审计 `ProposalRecorded`、typed Outcome Proposal、
Acceptance、Settlement 与 Appraisal Trigger 在同一个 pinned-prefix CAS 批次
提交，因而不存在把旧 Context 的决定套到后续 Clock 的恢复接口。进程若在提交后
崩溃，只会恢复已结算 Experience，不会重新调用模型或改写结局。

非法首答会把同一候选集合和精确失败原因交给同一模型完整重选一次。二次非法、
超时或供应商异常会原子记录无 Proposal 的技术失败审计；该 occurrence 的失败
身份独立于全局 Ecology，并按 10/30/120 分钟封顶重试。旧
`life-aftermath-context.1` 记录继续按原字节重放；新生产记录使用带 durable model
audit 的 `.2`。

这一设计增加模型输出与后果校验的复杂度，但把复杂度集中在 Life Ecology 的
内部 seam，并避免有限剧情库成为角色生活的上限。随机性仍只决定考虑机会、
时机和环境注意力，不能替角色选择行为或伪造已经发生的事实。

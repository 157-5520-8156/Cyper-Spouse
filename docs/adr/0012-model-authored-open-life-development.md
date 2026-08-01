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

Character Model 在作出生活选择前还拥有一次可选的、由她自己发起的长期记忆检索。
她可以直接 `accept/no_op`，也可以先返回一个自由文本 `recall_request`；系统只在
当前 pinned ledger cursor 上执行既有的来源闭合 RecallCoordinator，并把最多六条
可访问候选连同原始 source refs 交还同一角色模型。检索结果是参考材料，不是行为
指令，也不扩大 World Author 的事实或事件权限。随后进入一次最终 `accept/no_op`
语义阶段；该阶段结果非法时，仍允许同一角色模型针对精确失败原因作一次完整纠正，
但不能连续检索。请求、命中、游标、各语义阶段及其全部 provider attempts 和最终
选择进入同一审计链。崩溃恢复复用已完成阶段的记录，不重新执行已持久化的角色
阶段或检索；若最终语义阶段尚未完成，只从该阶段继续。这样，旧经历和用户互动
可以被角色在自主生活中真正“想起”，但是否想起、是否据此行动仍由她决定。

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

World Author 请求同时携带生成式 JSON Schema 与机器可读的跨字段 Authority
Contract。后者只描述 Schema 无法表达的来源 scope、World/Character 权威配对、
recipient-unbound visual privacy 和未结算 outcome 的事实地位，不推荐剧情或角色
行为。失败重选会保留同一 Contract，并返回结构化字段路径。模型输出中的引用数组
按集合语义确定性排序去重：原始字节仍进入审计，未知引用、来源越权和隐私越权仍会
拒绝。显式 `no_op` 若只额外回显了与 pinned owner 完全相同的
`authored_subject_ref`，解析边界会丢弃这个不能产生事实、权限或 Action 的字段并
生成规范 no-op；不同 subject 或任何其他额外字段仍是非法输出。这样避免把纯表示
差异伪装成技术故障，同时不允许本地代码替 World Author 编写事件。

首轮开放提议仍由常规 World Author 自由生成。只有独立来源闭包审查或
novel-origin 审查已经给出精确失败坐标后，系统才允许配置一个能力更强的供应商，
以同一个 `world_author` 语义身份完整重写一次。这个纠错调用继续使用原
Pinned Context、Capability Manifest、失败坐标和权威边界；它不会收到剧情模板、
行为偏好或运营者编写的替代事件，也不能改变已经冻结的机会身份。审计分别记录
首轮与纠错实际使用的模型身份，因此这是一条可观察的同角色可靠性路由，不是由
reviewer 或确定性代码接管生活创作。未配置强模型时仍由原 World Author 完成同样
的一次纠错。

来源审查的负坐标可以是 claim ID、草稿中的逐字片段，也可以是系统预先列出的精确
prose path。prose path 只定位 World Author 已写出的一个字段，不产生事实、理由或
替代内容；reviewer 仍须自行作出“该字段包含未闭合事实”的语义判断，代码只验证路径
确实属于本次冻结草稿。这样，reviewer 无需把判断改写成容易失真的“带解释引文”，
也不能借模糊匹配或本地抽取把不存在的文字变成有效拒绝。逐字片段仍必须原样存在，
typed-location 冲突仍必须同时绑定实际 location ref、精确 prose path 和逐字片段。

负坐标按审查权威进一步分层。General source reviewer 只裁决 existing-world claim
entailment、premise/visual/provisional-NPC 中未声明的当前事实以及 typed location；
`outcomes.N.text` 不能成为它的 undeclared-fact 路径或逐字片段来源。Outcome 是尚未
发生的分支，分支内动作、对话、邀请、回复、感受和主观反应本身无需既有事实来源。
Focused novel-origin critic 单独裁决 outcome 是否偷带分支发生前的当前/既往先决
条件、倒填关系或共同历史、已完成的角色经历，或把既有实体伪装成 novel；它必须返回
精确 `outcomes.N.text` 与该字段中的逐字片段，parser 只校验坐标，不用关键词代替模型
作语义判断。Premise coverage 不再由 focused critic 重复裁决，避免两个模型以不同
口径重复否决同一个候选。

两层审查都使用“最小充分证据包”，而不是复制整份面向创作的 Context。General
reviewer 收到其权威范围内的冻结草稿字段、每条 `existing_world` claim 精确引用的不可变
事件、完整 manifest hash/pinned cursor、草稿实际选中的精确 location capability，以及
既有 entity refs；只有存在 typed location 时，outcome text 才作为地点一致性坐标进入
General，且不能变成普通 undeclared-fact 负坐标。manifest 中的 entity ref 仍是 opaque
coordinate；编译器只对固定的已选 Context slices 做 exact-ref structural join，把确实含有
该 ref 且带 source bindings 的 compact item 附为 descriptor evidence，不猜名字字段、
不做别名/关键词/子串匹配。没有匹配只表示本次 non-exhaustive Context 未提供描述，不能
证明 novelty，也不能单独支持拒绝。

Focused critic 收到 novel claim、NPC/outcome 坐标，并机械保留 Context Capsule 已经限界的
character core、current situation、recent dialogue、relationship、open threads、appraisals、
affect episodes、accepted facts、recent experiences、world life、private impressions 与
perception results 中全部 items；其中有 source bindings 的 item 才能证明 existing-world
语义，没有 binding 的 capsule-bound baseline 只能提示不确定性，不能单独支持 claim 或
unsupported verdict。`active_memory_candidates` 是未巩固候选，明确不进入事实权威。
source-bound item 的 value hash 必须与送审 value 精确一致；baseline projection 则分别
保留上游 Capsule item hash 与 review-visible value hash，不能把投影视图冒充原值。
这里删除 resolver/rank/budget 等运输元数据，不做关键词过滤、重新排序或二次截断。
Focused manifest 的 descriptor index 只保留指向上述同一 evidence item 的 slice、item ref、
value hash 与 source hash，不复制第二份语义内容。
证据包仍绑定原 capsule identity 和完整 manifest hash；packet/compiler contract 与实际
request hash 都进入 review subject 和每次 provider attempt 的不可变审计，因此编译器或
相关证据变化不会命中旧的成功/失败/rewrite。压缩输入不能成为删减事实校验、伪造
verdict 或指导角色行为的理由。当前新写入的 possibility authority 使用 `.6`，必须校验
上述 packet/request/cursor/wake-bound identity；历史 `.4/.5` 只按各自的历史 identity
重放，不能借 legacy hash 通过 `.6` 校验。

Life 的 source-review authority 与可见聊天/主动联系的 authority 复用相同的、已有审计
证据的 provider 配置和无状态 HTTP 连接池，但拥有独立的 leaf circuit/runtime health、
route suppression、deadline task 与关闭生命周期。后台大包超时只能使 Life 自己进入
技术失败退避，不能把交互事实审查一起压入 600 秒 suppression。该隔离不增加第二个
语义投票，也不把未经过 release audit 的 Life strict schema 伪装成已 qualification：
provider 侧仍安装本地 schema、parser 仍严格 fail closed，而 health 只报告实际取得的
route qualification evidence。

World Author 的跨字段机器契约明确暴露 visual location 的成对关系：proposal 未绑定
执行地点时，所有 outcome 的 visual location 必须为空；绑定时，每个出现的 visual
location 必须等于同一个执行坐标，背景地、来源地或设想中的其他地点不能借图片字段
冒充可执行权限。失败反馈同时给出 proposal location 和具体 outcome visual location
路径，不再只返回无法定位的根级错误。

来源纠错仍只允许一次完整重写，但该次调用附带紧凑的 wire profile，限制建议的
outcome、claim、NPC 与可选视觉附件数量及文本长度，目的仅是让一个完整 JSON 在供应商
输出预算内闭合。它不枚举事件、动机或剧情，也不禁止不利事件；World Author 仍可自由
选择 `no_op` 或任何满足相同权威边界的开放提案。该 profile 不增加模型调用，二次非法、
超时和供应商异常仍记录技术失败并沿用 10/30/120 分钟退避。

这里的“自由选择 `no_op`”只属于来源纠错的第一次 World Author 决定。如果该次已经
返回 `propose`，但仅因 JSON Schema、wire 或 Capability 校验失败，唯一的 parser
repair 必须修复同一个 `propose` 决定，不能借修格式改选 `no_op`。Repair 请求携带完整
proposal schema、冻结的 Capability Manifest、跨字段 authority contract、精确错误
路径和允许值；provider strict schema 也只接受 `propose`。若模型仍返回 `no_op` 或
二次不合法，系统记录技术失败并重试，而不是把一次本可修复的候选伪装成角色主动没有
生活。若来源纠错第一次就直接选择合法 `no_op`，它仍是 World Author 的有效决定。

World Author 的每次首答与来源纠错还会收到同一个 pinned timing coordinate contract。
它同时给出 Logical Time 的 UTC 瞬间、每种 Capability 时区下的等价本地时刻、
`now/later` 字段关系，以及每个地点 Capability 的一个可直接复制的近期合法区间和
当前 `now` 最大时长。近期区间是由既有 schedule/absolute authority 与 pinned time
机械求交得到的证明坐标，不是地点或时间推荐，也不是完整候选菜单；模型仍可选择其他
满足原 schedule 的未来窗口或不绑定地点。系统不会把过去时刻自动平移到未来，也不会
把“相同钟面数字、不同 UTC offset”猜成模型原意。`window_in_past` 会返回
`timing.opens_at`、pinned instant 和模型所选区间，非法结果仍是技术失败。

这一设计增加模型输出与后果校验的复杂度，但把复杂度集中在 Life Ecology 的
内部 seam，并避免有限剧情库成为角色生活的上限。随机性仍只决定考虑机会、
时机和环境注意力，不能替角色选择行为或伪造已经发生的事实。

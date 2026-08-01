# 沈知栀系统蓝图

更新日期：2026-07-29

本文是当前运行系统的事实快照。设计目标、缺口和后续验收见
[`dynamic-loop-design.md`](dynamic-loop-design.md)。每次改变运行时边界、
数据所有权、平台路由或状态契约时，都必须更新本文。

## 系统定位

沈知栀是一个本地优先、以 QQ 私聊为主要存在方式的陪伴型 daemon。她不是
SillyTavern 角色卡的附属 UI，也不是单次 API 调用：daemon 是身份、关系、记忆、
生活事实、调度机会与投递确认的权威；角色模型拥有消息时机、主动/沉默、态度、
措辞、模态与消息数量的决定权。两者边界见
[`ADR 0010`](adr/0010-controlled-high-variance-character-agency.md) 和
[`ADR 0014`](adr/0014-model-owned-private-turn-state-before-expression.md)。

```text
QQ 小号 / NapCat OneBot ─┐
QQ 官方机器人（可选） ──┼─> 入站适配 -> Observation / reliability lifecycle
微信（未接入） ──────────┘                         |
                                                  v
  World V2 账本/投影 -> Pinned Context + Current Self + Recall
                                                  |
                                                  v
  角色模型 -> PrivateTurnState + ExpressionDraft -> 验收 / Action
                                                  |
                                                  v
  平台投递与回执 -> 世界后果 / 下一轮 Context / 考虑机会
```

## 运行组件

| 组件 | 当前职责 | 权威数据 |
| --- | --- | --- |
| `qq_c2c_onebot_app.py` / `QQC2CHost` | OneBot 入站、平台身份、运输批次、QQ Action 投递 | 平台 Observation 与真实回执 |
| `WorldRuntime` | 账本 UoW、CAS、恢复、Projection、Action 与 effect-once | World V2 不可变事件和 head |
| `semantic_chat_composition.py` / `Deliberation` | 组装生产模型路径、校验 Proposal、记录 ModelResult | Pinned Turn 与已接受 Proposal |
| `ledger_context_resolver.py` / `recall_runtime.py` | 构建来源绑定 Context、自动预取与角色选择性 Recall | Capsule、source refs 与 recall trace |
| `LifeEcologyRuntime` | 消费真实时钟/情境事件，推进生活并打开可重放考虑机会 | ecology schedule 与 trigger lifecycle |
| `SocialActionWorker` / media runtime | 执行已授权文字、Reaction、Sticker、媒体等 Action | Action 状态、外部结果与回执 |
| FastAPI 面板 | World V2 状态、上下文与可靠性健康检查 | 只读投影和诊断指标 |

## 数据模型与所有权

| 数据 | 用途 | 不能做什么 |
| --- | --- | --- |
| `world_events` / `world_snapshots` | 不可变事实、授权、结果、Projection 恢复与前缀证明 | 不改写历史事件或跳过 CAS/hash |
| Observation / dialogue projection | 已发生入站与已确认送达的出站聊天 | 不把未成功发送的草稿当共同历史 |
| Expression lifecycle / ModelResult audit | 每轮 claim、attempt、真实 provider identity、Proposal 与技术失败 | 不把失败伪装成角色沉默或本地回复 |
| Action authorization / settlement | `now/later`、多 Beat、媒体和工具的授权、执行、失败、未知与取消 | 不绕过权限、effect-once 或真实回执 |
| Character/Affect/relationship/life projections | 来源绑定的当前人格、情绪、关系、生活与经历 | 不直接编译成语气、追问、沉默或主动决定 |
| Memory / Thread / Commitment / Expectation | 可召回事实与跨轮未完状态 | 不从无来源草稿生成事实或永久续期 |
| Life Ecology / social initiative schedule | 时钟、情境刺激、ambient 机会、重试与退避 | 只安排考虑机会，不生成动机、台词或发送结论 |

## 当前主要行为链

### 用户发来消息

1. 适配器按平台事件身份去重，并用有上限的运输时间窗合并连续输入；不解析内容来决定
   哪一句值得回复。
2. 入站 Observation 与 expression reliability lifecycle 原子落账。平台未连接、手机真实
   不可达或活动尚未提供考虑机会可以延后观察，但消息关键词不能替角色作语义决定。
3. 上下文编排器提供当前 Observation、有来源的近期对话/记忆/生活状态、Current Self、
   可用能力与硬边界，不提供回复菜单、提问预算或风格指令。
4. 角色模型先形成 `PrivateTurnState`，必要时选择性 Recall，再选择
   `now / later / silent`、单条/多条及文字/Reaction/Sticker 等表达；验收只守结构、
   事实来源、隐私、安全、权限与外部效果边界。
5. Appraisal/Affect 等耐久后处理独立接受并进入后续 Current Self；失败不能中断已合法
   的当前表达，也不能由本地关键词补写。
6. 只有投递成功才写入出站历史；失败、未知和取消都按 Action 回执结算。

角色选择 `later` 时，系统把已接受的表达编译为有稳定身份、来源、到期与终态的 Action，
重启后按 effect-once 恢复。新用户 Observation 原子终结尚未越过 Action 授权边界的旧
cognition，再让角色依据新 Context 判断；系统不因“脆弱”“问句”或其他消息标签自动
创建关心、追问或补回任务。

官方 QQ 网关偶发以不同事件 ID 重投同一条用户消息时，适配器还会按“同一用户、
同一文本、1.5 秒内”做窄窗口抑制；这只防止网关重投，不会吞掉数秒后用户有意重复
发送的话。称呼、语气、引用和上下文作为原始 InteractionEvidence 提供给模型 Appraisal；
系统不靠称呼词表写入 guarded/hurt，也不据此禁止多 Beat 或后续主动考虑。

### 她主动分享或主动找人

1. 调度器根据已提交的情境变化或 ambient 节奏打开一次可重放的考虑机会。
2. 角色模型看到有来源的活动、可用性、关系、Affect、经历、Thread 与 Commitment，
   自行选择 `now / later / silent`；深夜、忙碌和关系状态是情境，不是系统否决器。
3. 确定性代码只验证事实来源、预算、平台能力、隐私/安全与 Action 权限；投递失败按
   回执结算，不能被伪装成角色沉默。
4. 成功发送写入主动投递记录；只有角色 Proposal 明确建立且来源合法的
   ResponseExpectation 才进入等待投影。
5. 用户后续回应作为新 Observation，由同轮 cognition/Appraisal 结合原 expectation 与
   当前世界判断其意义；系统不按及时、温和、忙碌、拒斥等标签自动改写关系、主动性或
   下一轮态度。

生活模型可从当前世界提出并结算私有事件；只有已提交经历能支撑关于人物、地点、时间与
结果的生活 claim。角色即使没有可分享经历，仍可选择不含外部事实的自然表达；系统不能
用活动标签临场补写经历。消息发送失败不会把草稿写成共同聊天历史。

## 已接入的平台

- **NapCat QQ 小号：** 当前运行的主通道。OneBot HTTP 在本机回环地址上工作；私聊按白名单接收，群聊默认不回复。
- **QQ 官方机器人：** 保留为可选适配器。其主动投递限制与小号不同，必须通过同一个 `QQDelivery` 边界。
- **微信：** 尚未接入。接入时只新增适配器和账号映射，不能再创建一套关系/记忆/生活状态。

## 模型与视觉边界

- 聊天模型为 OpenAI-compatible 配置；模型可以替换，但 daemon 的事实、记忆来源、预算、
  外部 Action 权限和投递确认不得交给模型自行改写。系统不统一清理舞台词、重复问句，
  也不以问号配额或助手腔禁句改写角色表达；人物、地点、时间和共同历史等事实 claim
  必须通过同一 Pinned Context 的来源闭包，不合法时由同一角色模型受约束重选。
- 图片理解/转写/生成是可选能力，受预算闸门和关系边界控制。
- 视觉身份当前是参考集与 visual bible；LoRA/FaceID 尚未训练，训练前需要通过数据集与远端 GPU 环境审查。
- 本地面板的像素小屋不是对话事实来源。`_scene_projection(...)` 将 daemon 已有的活动、手机注意力、情绪与未完成社交事务投影为 `location/action/expression/time_of_day`；前端只按这个契约做寻路与动作渲染，不能反向改写生活账本，也不能把计划项演成已发生事实。当前小屋是“等距背景 + 独立角色层”的观察面板，不是实体化游戏场景；实体化、碰撞、遮挡、动作库与多场景扩展已延期记录在 [`visual-home-roadmap.md`](visual-home-roadmap.md)。场景素材的来源与许可见 `THIRD_PARTY_NOTICES.md`。

## 维护规则

1. 不允许新增第二个“真相来源”：跨平台身份、关系、记忆和生活都归 daemon/SQLite。
2. 不允许把 planned 或 failed 投递写成已经发生的共同事件。
3. 新状态机制必须遵守 `state-machine.md` 的 Life Projection Rule。
4. 每次改变行为闭环，都要补一条确定性测试，并更新动态设计文档中的状态。

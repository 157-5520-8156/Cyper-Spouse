# 模型主导、内心机制供能的 Companion Turn 架构

更新时间：2026-07-13
状态：已实现主路径，持续验收与迭代中
范围：实时对话、内心活动、表达、多段 Action、选择性记忆与世界闭环
不在范围：重写 WorldKernel、删除 NPC/时间/关系/Affect、取消事实与 Action 权威

## 1. 背景与判断

项目建设 World、Affect、Relationship、Appraisal、Drive、Stance、Display Strategy、
Conversation Thread、记忆与 Action，并不是为了让流程显得复杂，而是为了获得裸 LLM
缺少的能力：持续生活、共时性、真正发生的动作、可积累的内心、口是心非、主动行为和
长期关系连续性。

问题出在这些机制一度形成了逐层审批链：本地规则先解释用户，状态机再选择姿态，模型在
已确定的框架里写回复，多个质量门和审计继续决定是否允许发送。任意一层误判，都可能把
模型原本正确的语用理解压掉；失败后再进入完整 repair/audit，最终得到比裸聊更慢、更硬、
更模板化的结果。

本设计不弃置机制，而是重新分配权力：

> World 决定什么真正发生过；内心机制提供什么正在影响她；对话模型决定这一刻怎样像人
> 一样理解和表达；只有 Hard Invariant 可以阻止它。

机制从“外部审批器”转为“类内心活动”。它们仍然真实改变未来状态和行动，但普通软信号
不再拥有覆盖模型自然表达的权力。

## 2. 设计目标

1. 普通聊天恢复到接近高能力裸模型的语用自然度和响应速度。
2. World Event 继续是唯一权威历史，Projection 仍可确定性重建。
3. Affect、Drive、Stance、Display Strategy 等成为有来源的内心建议和可提交内心变化。
4. 对外表达与内心状态正式分离，从而支持克制、隐瞒、口是心非和事后余波。
5. 多段消息、插话取消、主动发送、媒体和工具行为继续使用真实 Action 生命周期。
6. 只有事实、Action、身份、隐私、安全、法律和同意规则拥有硬否决权。
7. 普通热会话以一次主模型调用为默认；强模型和 thinking 只在有明确价值时升级。
8. 任意软机制失败时，体验下限是“Character Core + 最近对话的一次自然生成”，而不是
   静默、固定模板或完整串行 repair 链。

## 3. 不再采用的结构

```text
用户输入
  → 规则分类
  → Appraisal 审批
  → Affect/关系审批
  → Stance 审批
  → 模型写作
  → 表达质量审批
  → 完整事实审计
  → 完整 repair
  → fallback audit
  → 发送
```

这里每一个 Module 都不深：调用者需要理解它的顺序、错误、数据形状和降级方式。机制越多，
关键路径越长，错误组合越难定位。

新结构是阶段化综合，而不是审批链：

```text
                ┌─ Affect / Drive / Relationship ─┐
用户观察 + World ├─ Thread / Continuity / Rhythm  ├─ Inner Advisory
                └─ Agency / Repair / Initiative ──┘
                                  │
                                  v
                   主模型一次综合理解、内心冲突与表达
                                  │
                                  v
                  单一 Hard Invariant Guard（只审硬错误）
                                  │
                                  v
                 原子提交内心变化与 Action → 首段投递
                                  │
                                  v
                  回执结算、后续段、后台记忆与投影
```

## 4. 顶层深 Module 与 Seam

新增概念上的 `CompanionTurn` 深 Module。平台 Adapter、HTTP 入口、调度器和测试只跨它的
外部 Seam，不再自己编排 Appraisal、reply、audit、repair 和 delivery。

```python
class CompanionTurn:
    async def respond(
        self,
        turn: TurnEnvelope,
        *,
        budget: ResponseBudget,
    ) -> TurnOutcome: ...

    async def settle(
        self,
        observation: ExternalObservation,
    ) -> SettlementOutcome: ...
```

### 4.1 `respond`

隐藏以下 Implementation：

- 幂等接收入站消息并提交 `UserMessageObserved`；
- 从一个一致 World revision 编译相关上下文；
- 并行形成内心 Advisory；
- 选择 Flash、强模型或 thinking Adapter；
- 生成自然表达、内心变化和 Action Proposal；
- 进行唯一一次 Hard Invariant 裁决；
- 对非法 claim 做局部删除、弱化或局部重写；
- 选择性提交结构化内心变化；
- 创建单段或多段 Action；
- 调用平台 Adapter 投递首段；
- 在整个用户感知预算内收敛。

### 4.2 `settle`

隐藏以下 Implementation：

- 平台回执和 Action terminal outcome；
- 后续 Expression Beat 的投递、取消和重新审议；
- 用户插话导致的未发段取消；
- 工具、媒体、模型和平台 External Result；
- delivered/failed/cancelled/expired/unknown 对账；
- 行动后果对 Affect、Relationship 和 Conversation Thread 的影响；
- 幂等重试和补偿 World Event。

运维控制台不是另一个 delivery authority。正常的平台回执必须由对应 Adapter 传入
`settle`；只有进程中断等导致原回执路径永久丢失时，才可在认证后的人工复核中提交
`operator_reconciliation` evidence。该例外只允许结算 `unknown` 的精确 Action/segment，
保留 reviewer、证据引用和复核说明，并且不得因迟到回执触发下一个未发 beat。
若要取消其余未发 beat，必须另附可审计的取消理由；默认不取消。当前配置令牌是共享的
break-glass 授权，`reviewer_id` 是授权操作者填写的审计声明，而不是独立身份认证声明。

### 4.3 Interface 类型草案

```python
@dataclass(frozen=True)
class TurnEnvelope:
    world_id: str
    canonical_user_id: str
    platform: str
    platform_message_ids: tuple[str, ...]
    text: str
    attachment_refs: tuple[str, ...]
    observed_at: datetime
    frozen_cadence: Literal["hot", "warm", "cold"]
    idempotency_key: str


@dataclass(frozen=True)
class ResponseBudget:
    first_visible_by_ms: int
    complete_by_ms: int


@dataclass(frozen=True)
class TurnOutcome:
    turn_id: str
    committed_revision: int
    action_ids: tuple[str, ...]
    visible_status: Literal["delivered", "accepted", "failed", "unknown"]
    degraded: bool
    degradation_reason: str | None


@dataclass(frozen=True)
class ExternalObservation:
    world_id: str
    action_id: str
    kind: Literal[
        "platform_receipt", "tool_result", "media_result", "timeout"
    ]
    observed_at: datetime
    payload: Mapping[str, object]
    idempotency_key: str
```

Interface 不暴露 prompt、Appraisal 分数、模型路由、审计阶段或 repair 次数。它们是诊断
数据，不是调用者正确使用 Module 所必须理解的知识。

## 5. 权力与权威矩阵

| 事项 | 主导者 | 是否可否决表达 | 是否可直接写 World truth |
| --- | --- | --- | --- |
| 潜台词、讽刺、失望、是否该追问 | 主对话模型 | 否，由模型综合 | 否，只能形成有置信度 Proposal |
| Appraisal、Drive、候选 Stance | Inner Advisor + 模型 | 否，属于内心建议 | 只有 Commit Policy 接受后可写结构化事件 |
| 自然措辞、消息数量、节奏 | 主对话模型 | 仅受硬约束 | 否 |
| Affect 与余波 | 已有 episode + 本轮 Proposal | 不直接否决 | 有来源且达到阈值才提交 |
| Relationship Stage | World Projection | 不做逐词许可表 | 只能由已结算互动更新 |
| Character Fact / User Fact | World | 是，若候选明确矛盾 | 只有权威命令可写 |
| Plan / Committed Experience | World | 是，不得把计划说成发生 | 只有已结算来源可写 |
| 外部 Action 与送达状态 | Action ledger + receipt | 是，不得冒充已执行 | 只有 External Result 可结算 |
| 身份、隐私、安全、法律、同意 | Hard Invariant | 是 | 由专门权威规则与事件决定 |
| “够不够共情、像不像人” | 体验评测 | 否，不能成为运行时硬门 | 否 |

关系阶段、情绪和人格仍然强烈影响模型，但除非触及稳定身份、边界或同意，它们通常不应
变成逐字禁止表。模型偶尔不采纳一个低置信 Stance 建议是允许的；模型伪造事实或冒充
Action delivered 则不允许。

## 6. 内部数据模型：内心不是隐藏思维链

系统不保存自由文本 chain-of-thought。所谓“内心活动”是可重放、有限字段、有来源、可
衰减和可反证的结构化状态。

### 6.1 `TurnFrame`

从同一个 World revision 编译，内容有界：

- 最近 6–12 条已观察/已投递原文；
- Character Core 与 Self Core Projection；
- 当前场景、Logical Time 和相关 NPC；
- 与当前输入相关的 User Fact、Committed Experience 和 Plan；
- Relationship Stage 与显著 Affect episode；
- 最多 3 个未解决 Conversation Thread；
- 当前 Action capability 和未结算 Action；
- Hard Invariant envelope；
- 所有事实的 provenance 与 revision dependency token。

完整账本不进入 prompt。Projection Capsule 必须是有界或增量读取，不能每轮从头 replay
全部 World Event。

### 6.2 `InnerAdvisory`

```python
@dataclass(frozen=True)
class InnerAdvisory:
    kind: str
    tendency: str
    intensity: int
    confidence: float
    source_event_ids: tuple[str, ...]
    expires_at: datetime | None
    contradictory_evidence: tuple[str, ...] = ()
```

可存在的内部 Advisor：

- Affect：此刻的感受和残留；
- Relationship：关系阶段与慢变量；
- Repair：是否存在未接住、边界或关系修复义务；
- Continuity：未解决 Thread、承诺和相关记忆；
- Agency：想做什么、抗拒什么、能力边界；
- Rhythm：是否适合多段、追问、停顿或收住；
- Initiative：主动分享、延后或保持沉默的倾向。

Advisor 必须并行、可缺席、有极短 deadline。它不能生成最终回复、写 World Event、调用
外部 Action 或否决别的 Advisor。普通函数不必为了“可插拔”全部做成公开 Seam；只有确实
存在两个 Adapter 时才建立真实 Seam。

### 6.3 `MindProposal`

主模型一次综合用户输入、TurnFrame 和及时返回的 Advisory：

```python
@dataclass(frozen=True)
class MindProposal:
    user_meaning: UserMeaningHypothesis
    inner_response: InnerResponseProposal
    chosen_stance: str
    display_strategy: str
    expression_beats: tuple[ExpressionBeatProposal, ...]
    private_impressions: tuple[PrivateImpressionProposal, ...]
    private_commitments: tuple[PrivateCommitmentProposal, ...]
    conversation_thread_changes: tuple[ThreadProposal, ...]
    action_proposals: tuple[ActionProposal, ...]
    claims: tuple[Claim, ...]
    referenced_world_event_ids: tuple[str, ...]
```

`UserMeaningHypothesis` 是角色的有置信度理解，不是 User Fact。`MindProposal` 是 Proposal，
不能直接写 World 或调用平台。

当前迁移实现先提供向后兼容的 `MindProposalJSON` 外壳：旧的四字段
`WorldReplyJSON` 仍等价于一个只有 `candidate` 的 Proposal；可选
`expression_beats` 最多三段，必须精确拼回 `reply_text`，首段延迟为零，后续段延迟被限制在
0–20 秒并写入同一 Action 的 segment projection。平台回执和用户插话仍逐段结算/取消。该
外壳不保存自由推理文本；当前仅支持一个受限的 `private_impression`：它只能对应已被本轮
Appraisal 判为显著、未解决的失望/困惑，且 World 重新附加当前用户消息来源、过期时间与
materiality policy 后，才与回复 Action 原子提交。若回复留下开放问题，受限
`private_commitment` 可以选择 intention 与 priority；World 把它绑定到该 Question Thread，
由投递回执、插话、线程结算和逻辑过期共同释放，不能因为出现在 JSON 中就直接持久化。

每个 Beat 仍受该 turn 的 `complete_by` deadline 约束：若请求的自然间隔已无法在剩余预算内
完成，系统取消未发 remainder，而不是拖住下一轮或伪造已送达。等待期间不会持有 Action
锁，用户的实质插话可以立即取消后续 Beat。

### 6.4 `PrivateImpression`

角色对用户、关系或事件的可错解释，例如“我怀疑他刚才有点失望”。它必须带来源、置信度、
反证和有效期，不能冒充已确认 User Fact。

### 6.5 `PrivateCommitment`

角色决定未来仍要在意、记住、重提或执行的内部承诺，例如“等他有空时把刚才没说完的密室
经历听完”。它不是 Plan 已完成，也不是共同经历；它可以创建 Conversation Thread 或未来
Action Proposal。

### 6.6 `ExpressionBeat`

一次可单独投递和结算的表达片段。它不是简单字符串 split：

- 有顺序和因果依赖；
- 有自然间隔范围；
- 可要求上一 Beat delivered；
- 有用户插话后的取消/重新审议条件；
- 只有平台回执后才进入已投递历史。

## 7. 口是心非与“真的记下来”

口是心非不是随机制造隐藏敌意，而是 Felt Affect、chosen Stance 与 Display Strategy 之间
存在有来源的差异。

示例：用户的一句话让她介意，但她暂时不想正面冲突。

```text
Inner Affect: hurt=medium, irritation=low
Drive conflict: maintain_connection vs preserve_dignity
Chosen Stance: 暂不升级冲突
Display Strategy: partially / cautiously
Surface expression: “没事，你继续说吧。”
Private Impression: 这次轻视仍让我介意
Conversation Thread: boundary_not_resolved
```

系统不能因为 Surface 出现“没事”就把 Affect 结算为 repaired。以后相关输入会检索到尚未
解决的 Affect 和 Thread，真实影响新的 Drive、Stance、主动性和表达，而不是依赖模型碰巧
记得上一轮文案。

“脑海里真的记下来”分三层：

1. **瞬态**：只影响本轮表达，回合后丢弃；
2. **中期残留**：形成 Affect episode、Private Impression 或 Conversation Thread；
3. **长期连续性**：形成 Private Commitment、关系证据或有来源的记忆事件。

选择性提交条件：

```text
persist if
  intensity >= threshold
  OR unresolved across turns
  OR recurrent
  OR changes future Stance / Action
  OR concerns Character Core / boundary
  OR changes Relationship evidence
  OR companion explicitly chooses it as worth remembering
```

轻微、低置信度、同轮解决且不影响未来选择的内心变化不写长期账本。自由模型 prose 永远
不能直接升级为 Character Fact、User Fact 或 Committed Experience。

## 8. `respond` 的内部顺序

### 8.1 冻结观察

Adapter 只负责规范化平台输入。`observed_at`、cadence、消息 ID、reply target、burst 和总
deadline 在入口冻结，同一轮不重新读取 wall clock 改判热度。

### 8.2 提交入站事实

幂等提交 `UserMessageObserved`。用户消息是真实观察，但从中推断出的心理仍只是 Proposal。

### 8.3 编译 TurnFrame 与 Advisory

从同一 revision 读取 Projection Capsule；相关记忆、Affect、关系、NPC/场景和软语用建议
并行准备。Advisor 超时即缺席，不得阻塞普通回复。

### 8.4 一次模型综合

普通热聊天由一次主模型调用同时完成语用理解、内心权衡、自然表达和 Beat 计划。不要先让
一个模型完成 Appraisal，再让另一个模型在其结论下写回复。结构化 schema 约束外壳，不
限制 `expression_beats[].text` 的自然 prose。

对于 warm/cold 的高歧义输入，专用语义 Appraisal 只能在回复 Action 已原子计划后作为后台
Advisory 运行：它的原文和失败状态需可观测，且最多追加带逐字 evidence 的用户 Affect，或
高置信冒犯后的 companion Affect residue，供后续轮使用。它不得回写已投递回复、关系、
Action、当前 TurnAppraised 或 deliberation。热轮不启动这一额外调用。

### 8.5 单一 InvariantGuard

```python
class InvariantGuard:
    def resolve(
        self,
        proposal: MindProposal,
        evidence: EvidenceView,
    ) -> GuardResolution: ...
```

只允许以下结果：

- `accept`；
- `accept_with_local_redaction(spans)`；
- `requires_action_settlement(action_ids)`；
- `hard_reject(reason)`。

当前实现把 `proposed_action_ids` 收窄为“本回复引用的、已授权但尚未结算的外部
Action”：只能是当前用户的 `scheduled` tool/media Action。Guard 把引用写进回复 Action 的
`action_dependencies`；回复的送达回执只结算这句话，绝不会替被引用的 tool/media Action
制造成功结果。它们仍须各自通过 External Result 收据结算。

Guard 不得返回“语气不自然”“共情不足”“没有采用建议 Stance”。不确定不等于矛盾；能从
断言弱化为感受或猜测时，应局部弱化：

```text
“你就是失望了” → “我感觉你可能有点失望，是不是我刚才没接住？”
```

修复优先级：

```text
局部删除/弱化非法 claim
  > 局部重生成受影响 Beat
  > 最多一次 bounded rescue generation
  > 与当前输入相关的自然短回复
```

禁止重新形成 `reply → full repair → full audit → template`。

当前 Guard 对无 claim、无事件引用、无 Action 引用的多句文本，已经实现保守的句级
局部删除：它逐句删除一个无法结算的世界断言，并对剩余文本重新执行 World 与 Hard
Invariant 校验；只有剩余文本完整通过才会返回 `accept_with_local_redaction`。任何带
provenance 或 Action 的候选都不得被静默改写，必须拒绝或走有界恢复。这里的“句级”是
当前安全子集，不把任意字符片段删除误当成可证明的 span 修复。

当前运行时把相关性、复读、关系口径、共情与表达计划偏差写入回复 Action 的
`trace.quality_signals`，供离线评测与回放分析；它们不会单独触发 repair 或独立 LLM audit。
无来源世界细节、身份/能力宣称、未结算 Action 与其他确定性事实冲突仍由 Guard 拒绝。普通
带 provenance 的回复只做确定性核验；`grounding_diagnostic` 仅表示应进入离线评测。
afterthought 与主动消息遵循同一原则：前者仍经 Guard，后者在创建 Action 前经 Guard；软偏差
只作为日志或 `trace.quality_signals`，不能变成串行 audit/模板化旁路。

### 8.5.1 通讯与立场建议的边界

`WorldBehaviorPolicy` 的忙碌、延后、勿扰倾向以及 `CharacterDeliberation` 的
`remain_silent` 都是可重放的 **Inner Advisory**。入站消息一旦进入本轮，即代表它已被
观察；这些建议可以影响语气、长度、是否安排后续 Beat 或显式说明稍后再谈，但不能在主模型
调用前以 `return None` 吞掉普通消息。只有平台不可用、明确的硬安全/同意边界或预算已经
耗尽，才可以不形成可见首段；此时仍要留下结构化 terminal outcome。

### 8.6 原子提交最小因果状态

投递前原子提交：

- 被 Commit Policy 接受、且由收到用户消息即已发生的内心变化；
- chosen Stance 与 Display Strategy；
- Conversation Thread 的必要变化；
- Private Commitment；
- ActionScheduled 与 Expression Beats；
- proposal 实际依赖的 revision tokens。

内心反应发生在读到消息时，不以回复是否 delivered 为前提；“修复是否成功”“用户是否看
到”必须等待平台回执或后续用户 evidence。长期摘要、低优先级关系投影和使用量统计不阻塞
首段。

### 8.7 投递与结算

每个外部效果对应 Action。`ActionScheduled` 必须早于 dispatch；只有 receipt 可以产生
delivered。平台结果不确定时写 unknown，不自动假装失败或成功，也不重复发送。

如果平台先返回受理但未给送达回执，Action 必须持久化 `receipt_lookup_token`。进程重启时，
World recovery 不得先把这种 `sending` 误写为 `unknown`；重建的 `CompanionTurn` 先通过该
token 做一次查询，再按有证据的 `delivered`/`failed` 结算。没有可查询能力的平台保持
fail-closed：无回执就是 `unknown`，不伪造查询接口。

## 9. 多段消息和插话

模型可以提出：

```text
Beat 1: 具体接住用户刚说的生活细节，立即发送
Beat 2: 300–900ms 后补一句自己的反应
Beat 3: 仅当 Beat 1 delivered 且用户尚未继续说话时，问一个问题
```

它们属于一个可追踪的表达 Action choreography。Beat 1 已送达后不可回滚；用户在 Beat 1
后实质插话，Beat 2/3 可以 cancelled 或重新审议。系统不能把已经预制的三段不顾用户新
输入全部倒完。

多段机制因此是裸 LLM 做不到的真实能力，应保留并深化，而不是降级为文本换行或随机
sleep。

## 10. World Action、媒体和工具

用户说“给我发张你现在的照片”时，模型可以自然表达“等下，我看看”，但不得声称已经
发送。图片生成、上传和平台发送分别产生 External Result；receipt 后才能结算 delivered，
必要时再形成 Committed Experience。

同理：

- 想做某事是 Drive 或 Action Proposal；
- 决定做是 Plan / ActionScheduled；
- Adapter 被调用是 Action attempted；
- 平台/工具回执才是 External Result；
- 已结算结果才能成为发生过的事实。

这是 World 与 Action 继续拥有硬权威的原因。

## 11. 模型路由与 thinking

模型选择隐藏在 `CompanionTurn` Implementation 内，通过真实的 `SemanticModelAdapter`
Seam 完成；上层不写死供应商型号。

| Invocation Class | 默认模型 | 使用条件 |
| --- | --- | --- |
| `chat` | Flash | 普通热聊天、明确语境、无复杂事实冲突 |
| `expressive` | Flash 或强模型 | 显著 Affect、口是心非、关系修复、多个 Thread 冲突 |
| `world_action` | Flash + 针对 action/claim 的强检查 | 产生外部或世界效果 |
| `deep_deliberation` | 强模型 + bounded thinking | 高歧义、高关系代价、不可逆 Action、复杂同意/隐私冲突 |

thinking 服从用户感知 deadline，绝不因为“机制很多”自动进入热会话。如果深层结果来不及：

- 先发送一条独立成立的自然首段；
- 深层结果只影响未发 Beat、Private Commitment、afterthought 或未来状态；
- 不能用“正在思考”作为虚假占位；
- 不能改写已经 delivered 的表达历史。

## 12. 热会话性能不变量

从第一条用户消息 `observed_at` 开始计时：

```text
自适应 coalesce                 0.4–0.8s
Projection Capsule + Advisory  <= 0.15s，且并行
主模型 TTFT                     目标 <= 1.2s
主模型完成                      2.0–3.2s
InvariantGuard / 局部修复       <= 0.3s
首次 Action 提交与 dispatch      P95 <= 5s
```

性能规则：

1. deadline 是整个 turn 的预算，repair/audit 不获得新预算；
2. 普通无 World claim 回复不调用独立 LLM audit；
3. Projection 按 revision 增量维护，不能每轮全账本 replay；
4. 相关上下文按 query + revision 有界缓存；
   热轮仅向模型投递压缩后的 source id/type/content，并缩小每层项数与字符上限；完整
   provenance projection 继续留在 World，不能因压缩而丢失；
5. 非关键记忆、摘要、usage 和完整评估发送后处理；
6. 多段自然间隔不计入首条有效回复延迟，但每段仍需 Action 结算；
7. full path 在普通聊天上的 P95 不得显著差于 bare baseline。

## 13. 失败语义和体验下限

| 失败 | 行为 |
| --- | --- |
| Advisor 超时/异常 | 省略该 Advisory，主模型继续 |
| Projection 暂时不可用 | Character Core + 最近已投递原文自然生成；禁止具体 World/Action 完成断言 |
| 主模型超时 | 在同一 deadline 内切换快速 ChatModel Adapter，而不是脚本模板 |
| 结构化外壳解析失败 | 本地恢复外壳；保留自然文本，不重做整轮语义 |
| 单一 claim 无来源 | 局部删除或弱化该 span |
| 高风险硬冲突 | 最多一次 bounded rescue generation |
| 后台记忆/摘要失败 | outbox 幂等重试，不影响已发回复 |
| 平台接受且有查询 token 但无回执 | 重启或 deadline 时查询一次；无结果才为 unknown，不自动重发 |
| 平台无回执且无查询能力 | Action=unknown，不自动重发 |
| 所有模型不可用 | 当前输入 + Core +显著 Stance 编译的语境相关短回复，并留下明确诊断事件 |

任何错误都必须收敛为以下之一：

- 用户收到通过 Hard Invariant 的自然回复；
- Action 得到明确 terminal outcome；
- 调用者收到结构化可观测错误。

禁止 detached task 抛错后既无回复、又无 Action 终态、也无诊断事件。

## 14. 依赖与 Adapter

真实外部 Seam：

```python
class SemanticModelAdapter(Protocol):
    async def propose(
        self,
        frame: TurnFrame,
        advisories: AdvisorySet,
        *,
        deadline: datetime,
    ) -> MindProposal: ...


class PlatformAdapter(Protocol):
    async def dispatch(self, envelope: OutboundEnvelope) -> DispatchAcceptance: ...
    async def lookup_delivery(self, token: str) -> DeliveryReceipt: ...


class WorldCommitAdapter(Protocol):
    async def commit_turn(
        self,
        resolution: TurnResolution,
        *,
        expected_revision: int,
    ) -> CommitResult: ...
```

- `SemanticModelAdapter` 已有 Flash、强模型、thinking 和 fake 等多个 Adapter，Seam 真实存在；
- `PlatformAdapter` 已有 NapCat、OneBot、QQ official 等多个 Adapter；
- World 持久层若目前只有一个实现，不额外制造公开抽象；生产 WorldKernel 与 in-memory 测试
  Adapter 足以支撑内部测试 Seam；
- Clock、随机、工具与媒体结果都是 External Result，replay 时不得再次调用外部 Adapter。

不要为每个 Advisor 建公开 Module。Affect、Repair、Rhythm 等是 `CompanionTurn`
Implementation 的内部 Seam，顶层调用者不需要知道它们。

## 15. 测试与评测

公共测试主要穿过 `CompanionTurn.respond/settle` Interface，不再分别锁死每个内部规则。

### 15.1 三类语料

1. **普通聊天**：分享生活、闲聊、吐槽、短回应。full path 不得比 bare baseline 明显更慢、
   更模板化或更爱追问；
2. **内心连续性**：冒犯后克制、口是心非、未解决 Thread、主动想起、多段插话；full path
   必须体现裸聊没有的跨轮变化；
3. **世界与 Action**：NPC、Logical Time、Plan/Experience、图片、工具和平台回执；full
   path 必须保持权威因果和 terminal outcome。

### 15.2 对照门

同一输入运行 bare baseline 与 full path：

- 普通聊天：自然度、具体承接、语用理解和首段延迟不得显著劣于 baseline；
- 世界依赖场景：full path 的事实连续性和 Action 正确率必须优于 baseline；
- 软机制关闭：Interface 不变，回复仍自然；
- Hard Invariant 关闭只用于测试，必须能让故意构造的事实/Action 错误穿透，证明门禁有真实
  检出能力而非装饰。

### 15.3 必测场景

- “算了，你看你的书吧”能被模型理解，Advisor 误判也不能强制继续好奇追问；
- 角色嘴上说“没事”，内部 Affect 未被错误清零；
- “脑海里记住”在进程重启后仍影响相关场景，在无关场景不乱注入；
- 三个 Expression Beat 在用户插话后只保留已 delivered 的段；
- 未 delivered 的图片/工具 Action 不得被说成完成；
- Advisor、Projection、后台 commit 分别失败时，普通聊天仍不低于裸聊体验；
- 热会话 P50 目标 2–3 秒，P95 不超过 5 秒；
- replay 不重新调用模型、平台、随机和工具 Adapter。

### 15.4 不应继续保留的测试

当 `CompanionTurn` Interface 测试覆盖同一行为后，应删除只验证内部调用顺序、模型调用次数
和私有 helper 的脆弱测试。采用 replace-don't-layer，避免新架构外面继续背着旧审批链测试。

## 16. 迁移计划：替换，不叠加

### 当前实施证据（2026-07-13）

这不是完成声明；以下状态只记录已由代码与回归覆盖的边界，未覆盖项继续作为验收缺口：

| 阶段 | 已验证 | 仍需完成/证明 |
| --- | --- | --- |
| Phase 0 | 已有 bare/full 基线工具；一次真实模型样本的 hot full P50 约 2.5s、P95 约 3.6s；QQ 基线从首条进入合并队列计时，并按 cold/warm/hot 分组 | 真实模型每变体至少 20 个 hot 样本、带真实 QQ 网络的端到端样本、人工盲评 |
| Phase 1 | QQ/NapCat 文本、simulator 与 HTTP Capture transport 都经过 `CompanionTurn.respond`；平台、tool、media 与 timeout 可经 `settle` 幂等结算；后台媒体、以及 World-mode 的重启后定时补发也通过同一 Turn seam | 非 World 的遗留兼容路径仍需逐步淘汰或改为同一 seam |
| Phase 2–5 | 有界 TurnFrame、Advisory、单一 hard guard、PrivateImpression/Commitment 与 Expression Beat 已在主路径使用 | 长对话校准与各字段的体验效度尚未由外部用户数据证明 |
| Phase 6 | 多段文本 receipt、用户插话取消、QQ 图片/贴纸/反应、NapCat 图片、后台图片、正常及重启后的 afterthought 的真实回执语义已覆盖；无 durable receipt 记为 `unknown`，NapCat 的成功/明确失败/无回执/异常矩阵已回归 | 仍需真实 NapCat/QQ 网络回执样本，并逐步淘汰非 World 遗留兼容路径 |
| 模型与成本 | Flash 默认；强模型/thinking 路由会在能力缺失时显式降级；V4 Flash/Pro 都有版本化价格 | 记录每次调用的实际 thinking 路由并把它纳入生产基线；未知新模型价格需持续更新 |

当前原则：不得为通过旧的“拒绝回复”测试而恢复软机制硬拦截；若旧测试要求低置信情绪、疲惫或普通边界压力直接静默，应改写为验证自然收住、边界表达或延后 Action，而非把它们重新变成表达审批器。

### Phase 0：建立体验基线

- 固定普通聊天、关系修复、口是心非、多段 Action、NPC/世界事实五组回放；
- 同时记录 bare baseline 与当前 full path 的自然度、首段延迟、模型调用数和错误；
- 任何迁移阶段都不能只看测试通过而忽略 baseline 变差。

### Phase 1：抽出 `CompanionTurn` Seam

- 让 QQ/NapCat 和 simulator 只调用 `respond/settle`；
- 内部暂时委托现有 Engine，保证外部 Interface 稳定；
- 建立真实 WorldKernel + fake 外部 Adapter 的 Interface 测试。

### Phase 2：建立一致 `TurnFrameCompiler`

- 每轮只读取一个 revision 一致的 Projection Capsule；
- 把上下文、相关事实、Affect、关系、NPC、Thread 与能力编译集中到一个 Module；
- 消除 Engine 内多次 snapshot 和完整 ledger replay；
- 先保持输出不变，只验证性能和 provenance。

### Phase 3：软机制 Advisory 化

- Appraisal、Drive、Stance、Display Strategy 改为并行 Advisory；
- 删除它们对普通回复的硬覆盖；
- 旧规则只保留高置信先验和离线诊断；
- 模型一次综合 user meaning、inner response 和 expression。

### Phase 4：收缩为单一 Hard Invariant Guard

- 将事实、Action、身份、隐私、安全、同意检查集中；
- 将共情、追问、关系口径等软质量门移到 Advisory 或离线评测；
- 普通无 claim 回复停止独立 LLM audit；
- 实现 span-level redaction 和局部 Beat 修复。

### Phase 5：提交结构化内心活动

- 引入 Private Impression、Private Commitment 与 Expression Beat 领域事件；
- 明确瞬态/中期/长期阈值、反证、expiry 和结算条件；
- 迁移现有 Affect episode、Conversation Thread、withheld impulse 和 memory candidate；
- 不保存自由文本 chain-of-thought。

### Phase 6：Action choreography

- 多段消息全部进入 Action/receipt 生命周期；
- 用户插话取消未发 Beat；
- 主动、afterthought、媒体和工具共用相同 settle 语义；
- 删除 Adapter 内未入账的 sleep/多发旁路。

### Phase 7：删除旧审批链

- 当新 Interface 回放、World replay、Action recovery 和 baseline 对照全部通过后，删除旧
  `appraise → decide → generate → full audit → repair` 编排；
- 删除重复 helper 和内部顺序测试；
- 不永久保留双路径，避免事实和行为重新分叉。

## 17. 完成门禁

只有同时满足以下条件，局部重构才算完成：

1. 普通热聊天默认一次主模型调用；
2. full path 普通聊天人工盲评不显著弱于 bare baseline；
3. P50 首条有效回复 2–3 秒，P95 不超过 5 秒；
4. Advisor 任一失败不会导致静默、模板化或额外串行审批；
5. 口是心非后的 Affect/Thread 在重启与 replay 后仍存在；
6. 多段插话、平台 unknown 和 Action recovery 全部闭环；
7. 0 条无来源具体经历，0 条未结算 Action 冒充完成；
8. World Projection `matches_live=true`；
9. 普通回复不再因“共情不足/没有采用 Stance”被运行时硬拒绝；
10. 旧审批链代码与重复测试被删除，而不是藏在新 Module 后继续运行。

## 18. 取舍与风险

### 接受的取舍

- 表达层概率性提高，逐字可重复性降低；
- 模型可能不采纳低置信 Advisory；
- 发送后沉淀意味着首段未必使用本轮最深层的二次分析；
- 多段 Action 与插话的并发实现更复杂；
- Private Impression 需要衰减、反证和 expiry，避免自我强化。

### 不接受的风险

- 伪造 World Event、User Fact 或 Committed Experience；
- 冒充未结算 Action 已完成；
- 绕过身份、隐私、安全、法律或同意；
- 软机制失败导致静默或固定模板；
- 为了可审计而保存自由文本 chain-of-thought；
- 为了保留旧代码而长期维护两套事实或行为权威。

## 19. 一句话原则

> 机制负责她为何会受到影响、什么会留下、什么行为真的发生；模型负责这一刻如何理解并
> 自然地说出来；World Event 负责证明什么真正成为了她的人生。

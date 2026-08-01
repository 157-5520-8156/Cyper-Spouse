---
status: accepted
---

# 角色模型在表达前形成同轮私有当下状态

## Context

Context Capsule、Current Self State、Affect、关系、生活经历和自动 Recall 已能给模型提供
丰富材料，但“材料存在”不等于“角色先形成了自己的当下想法”。如果模型直接从用户消息
跳到 ExpressionDraft，它仍容易沿用通用聊天模型的隐含目标：维持互动、继续追问、显得
乐于助人。事后再生成 rationale 也不能建立因果，因为可见回复已经选完了。

另一方面，把回应方式、提问率、动机或情绪—表达关系写成枚举和规则，会违反受控高随机
宗旨。系统需要证明“角色先有一个自己的当下状态，再决定怎么表达”，但不能规定这个状态
应当是什么。

## Implementation status amendment (2026-08-01)

本 ADR 后文同时保留了最初 accepted 时的 qualification 快照，以及
Inventory V5 + Coverage V5 的目标拓扑。前者已被以下实现状态取代，后者仍只是需要
exact qualification 才能启用的条件性设计：

- Inventory V5 已根据返回的 exact wire 审计在 OpenRouter
  `openai/gpt-5.4-nano` 路由（13/14）和 direct `gpt-5.4-mini` 路由（11/12；
  语义边界用例 10/10）完成 release qualification；
- 当前生产拓扑是 `inventory_v5_guard_then_full_source_review.7`。Inventory 只提供
  语义分解和冻结 locator，独立的 RR.3 / `source-closure-review.7` V7 authority
  仍拥有全部事实裁决权；
- Coverage V5 仍未 qualification，保持 dormant。Inventory 技术不可用时，系统直接
  执行完整 V7 review，并在 health 中报告 degraded route，不把技术失败当作
  语义结论；
- production composition 在启动时验证每个可能的角色作者与事实 reviewer 都是不同的
  exact semantic authority。普通入站缺少合格独立 reviewer 时启动失败；恢复作者与
  reviewer 重合时反转为独立的另一 checkpoint 审查，不能让作者自审，也不能把配置错误
  延迟成每条消息的稳定失声；
- 生产延迟观测在整个 foreground candidate 上建立 transport span：每次真实
  `client.post`（包括 Context 阶段的语义 Recall embedding / advisory、主路失败后的
  fallback 和事实复核/重选）都有独立 call identity 与同笔 exit。
  `ingress_to_first_role_provider` 与 `model_completion` 仍只绑定角色认知调用，避免把
  embedding/advisory 误报成角色首包；`foreground_provider_total` 才是所有前台供应商
  区间的并集。
  `api_external_overhead` 以 `ingress_to_visible` 减去这些区间的并集，作为
  500 ms API 外目标；未闭合区间保持 unmeasured，不能用 failover wrapper 返回或首包
  入口伪造完成时间。health 按 trace identity 关联完整样本；窗口中已有完整快样本时，
  其他未闭合 trace 的慢首个角色调用证据仍作为兼容下界报警，不能被全局切换掩盖。

因此，后文中“Inventory V5 + Coverage V5 建立唯一语义权威”的陈述应读作目标
拓扑的启用条件，不是当前部署拓扑。`CONTEXT.md` 中的 Source Review
Qualification 定义是当前运行状态的简明来源。

## Decision

普通入站 Expression 使用 `PrivateTurnState`：

- 它是同一次角色模型输出里的第一个字段，随后才是 `timing_choice`、Beats、问题、沉默和
  其他表达选择；
- `inner_state_summary` 是短自由文本，只记录角色此刻注意到了什么、产生了怎样的主观
  倾向，不采用动机枚举、回复模式或行为菜单，也不索取隐藏推理过程；
- `attended_source_refs` 最多八条且必须唯一，只能引用同一 Pinned Turn 的
  Observation、Context Capsule、角色稳定身份或已审核 Recall 来源。它们只记录本轮注意
  来源的 provenance，不是事实证据声明，也不授权 TypedChange、Action、Memory、
  Relationship、World Projection 或未来 Context；
- `PrivateTurnState` 只属于 Proposal audit。当前第一人称 private self 是角色在本轮形成的
  非 World 状态，包括感受、想法、注意、欲望或抗拒、不确定性、想象、记忆可及性、
  自我评价以及当下表达意图；它不进入 hard source reviewer，也不能反过来证明外部事件、
  地点、行动、人物、身体状态、物理发生或已经确定的历史。若其中任何外部材料随后进入
  `visible_text`、world claim、TypedChange、Action payload、Memory 或其他耐久输出，
  必须在对应的 effect-bearing 输出 seam 依据该输出实际引用的证据重新完成来源闭包，
  不能继承 private state 或 `attended_source_refs` 的权限；
- 自动 Recall 仍使用既有 RecallCoordinator。角色若想先回忆，首次输出
  `PrivateTurnState + recall_request`；召回后必须依据扩充后的 Context 重新形成最终
  `PrivateTurnState + ExpressionDraft`；
- 状态缺失、位于表达字段之后或引用越界时，同一角色模型获得一次完整 Expression
  重选。系统不得保留旧可见回复再补写一个事后理由；
- 新 provider authored wire 不得借用 `ExpressionDraft` 为历史 replay 保留的本地默认值
  替角色作决定：生产 composition 要求模型显式返回 `timing_choice`、`beats`、`stance`、
  `brief_rationale` 与 `confidence`；当
  `expression_capabilities.recorded_cadence_mode=shadow/on` 时还必须显式返回
  `cadence`，`off` 时才可省略。遗漏只产生字段化的结构失败，并由同一角色模型完成一次
  整体重选。这个 preflight 同样位于 paired cognition 首稿、其纠正稿和 exact-origin
  cache materialize seam；缓存字节不能绕过它。重选仍遗漏时终结为
  `authored_expression_reselection_invalid` 技术失败，不得落本地默认、再开第三次角色选择或
  把失败写成沉默。`world_claims=[]` 的 replay-safe 默认暂时保留，因为省略它只会放弃事实
  权限，不会制造可见行为或外部 effect；历史 Proposal replay 与内部 Pydantic construction
  继续兼容旧默认；
- 新的 Beat wire 只暴露平台可执行的 modality、内容和必要时序能力，不再向角色模型暴露
  `opening / substantive / challenge / self_correction / afterthought` 这类宿主修辞分类，
  也不由系统为缺失分类补默认值。旧 Proposal 中的 `semantic_role` 只为不可变账本冷重放
  保留；旧供应商回声中的五个历史值在解析边界被精确丢弃，未知值仍严格失败，新 Proposal
  身份使用升级后的 materialization contract，不能与旧 payload 哈希混用；
- `typing` 与文本、表情一样是角色可选的 Expression beat。QQ host 不得在收到用户消息时
  自动发送一个宿主拥有的“正在输入”脉冲：它会替角色决定表现节奏，并在技术失败时造成
  “正在输入后失声”的假象。只有已经通过本候选完整校验并获 Action 授权的模型选择才能
  触发该平台效果；
- 结构校验失败只跨普通日志与纠正路由传递稳定的错误码和字段路径，例如
  `private_turn_state.unpinned_source` 与
  `private_turn_state.attended_source_refs`。Pydantic 的原始 input、私有摘要、越界引用和
  其他模型原文不得进入日志；结构化错误仍须足以让同一角色模型重选合法结果。独立 hard
  来源复核只在每条 transport 都有精确 endpoint/model/schema-digest qualification 时使用
  Inventory V5 + Coverage V5 对可见命题建立唯一语义权威。**历史 qualification
  快照（已被上方 implementation amendment 取代）：** 2026-08-01 的早期 direct
  `gpt-5.4-mini` exact-V5 诊断只有超时、没有返回合法 wire，因此当时
  production health 必须报告 `unverified → full_source_review.7`，V5 双路容错机制保留为
  dormant。这个历史结论仍证明不能把 request 已发出或配置自声明冒充
  qualification；
  只有模型实际声明了 `world_claims` 时，才另用 `source-closure-review.8` 复核这些 claim
  的主语、时间、逻辑模态、发生、状态、scope 与 source entailment。`.8` 不接收
  `visible_text`，`v / p / visible_findings` 必须为空，因而不能重新判断已经由 Coverage
  裁决的未声明可见命题。未安装 strict V5 inventory 的历史/兼容部署仍保留
  `source-closure-review.7` 全量边界，但不得与 V5 同时争夺可见文本权威。候选侧不包含
  `private_turn_state`；复核所需的已引用
  evidence item、当前 Observation 的 report-only 证据和命中的 identity source 仍按最小
  闭包提供。`ci` 只报告未闭合的 World claim 索引，`v` 只报告 `visible_text` 中的
  “未声明外部断言、实际主语不受支持、时间蕴含不成立、发生/状态不受支持”四类硬边界。
  `p` 仅为旧 wire 兼容保留，provider 应始终返回空；若旧 provider 返回非空 `p`，host
  必须把其中类别保守并入 `v` 后再作接受或重选，不能把 `p` 当成 private semantic review
  或用它清除拒绝。角色或参与者 ref 只能标识命题主语，不能证明该主语发生了任意事件；
  可见表达中的当前第一人称私有感受与当下意图无需 World 证明；同一段正在进行的对话内，
  角色对自己“刚才犹豫、想说却没说、没想起来、觉得尴尬”等第一人称即时心理连续性也拥有
  私有状态权威，不能只因语法过去时就把它升级为 World 历史。这个权威不能证明夹带的地点、
  外部行动、人物、身体状态或客观事件；这些外部子命题仍必须有相同主语、时间与发生状态的
  来源。复核输入携带机器可见的 `epistemic_authority_contract`：角色当前及同轮即时回顾
  的第一人称心理连续性无需来源，只有其中夹带的地点、行动、人物、身体状态与客观事件需要
  来源；当前用户报告只能证明“用户报告过”。该合同只冻结认识论权限，不用关键词分类、
  决定事实或选择行为。生产 hard review 不构造、发送或依据按标点拆分的 `audit_units`
  作出否决：真实模型轨迹表明，这种“穷举审计单元”会把角色自己的即时心理连续性误判成
  World 历史，并把正常表达变成技术失声。未来命题的细粒度覆盖或发生状态检查也必须先在
  shadow 中校准，不能未经误报率验证就成为发送前置条件。`r` 只是无权威的简短诊断；
  复核器不得复制草稿片段、改写表达或把 private self
  当成待举证的 World 事实。该反馈只存在于本次 provider 调用，不得持久化为 World 事实或
  行为建议；`r` 不得进入角色纠正或备用模型输入，不能以自然语言解释的形式锚定下一份候选；
- 当前 Observation 在 `recent_dialogue` 中的精确语义别名必须归一到同一个
  `current_counterpart_report` 证据，不能因模型引用了 Observation ref 还是
  `dialogue:observation:*` ref 而改变权威。直接对话项按冻结的 `speaker` /
  `speaker_ref` 标为 `counterpart_report_only` 或 `companion_expression_record`；
  角色自己的旧表达可以作为注意材料并证明“这句话曾被角色表达”，但不能进入
  `counterpart_history` 权限去证明用户事实或角色过去的动机；
- `biographical_context` 整项只是一组可同时被角色注意的当前传记读数，不是能证明
  任意生活事件的 bearer token。系统按 Pinned Logical Time 将年龄、学业阶段、学年、
  季节、校历、当前住处及每条 active Life Arc 派生为字段/值绑定、内容寻址的
  `biographical-coordinate-authority.1`，并且只授权 `current_world`；整项
  `biography:*` ref 不进入 `current_world`、`past_world` 或 `stable_identity`。
  `past_world` 仍须引用已结算 World occurrence 或已提交 Experience，真正稳定的身份
  仍来自 Character Core / scoped identity source。坐标 identity 只包含跨普通/恢复
  Context compaction 保真的语义投影，不包含 proof hash 或 acceptance event bookkeeping；
  没有逻辑时间锚时不得生成当前坐标。`source-closure-evidence.3` 只向 reviewer 展示被引
  坐标的精确 `field_path / value / logical_at`，不能以年龄、季节、住处或 Life Arc
  推导未列出的行动、物件、相遇、回忆或发生史；Proposal evidence 同时回接父 biography
  的 Timeline、Clock 和 Life Arc ledger bindings，保留完整审计闭包；
- 已激活但尚未结算的 Life Development occurrence 只能以
  `active_world_occurrence` 进入 `current_world`。读取端必须同时闭合原
  `ProposalRecorded`、`WorldOccurrenceCommitted`、`WorldOccurrenceActivated`、当前
  active projection head 以及 premise sidecar 的 ref/hash/bytes/privacy；模型只能看到
  已审核 premise、时间窗、地点、参与者、激活时间和 active 状态，不能看到候选结果或把它
  写成 `past_world`，也不能在结算前进入 Recall。结算后的 Experience 才是自传历史；
  只要角色本人属于 `participant_refs`，与 NPC 共同经历不因 NPC 不在当前对话 subject
  scope 而丢失，角色本人不在参与者中的经历仍不得进入其 Context 或 Recall；
- 精确当前报告是同轮、report-relative 的 discourse authority。角色对该报告做纯主观
  反应、评价、不预设新事实的问题，以及被原报告直接语义蕴含的自然复述或省略式承接，
  都无需 `world_claim`，可见措辞也无需显式出现“你说/据你说”。系统始终把精确
  Observation、报告者和原文作为 `current_counterpart_report` 交给 hard reviewer，
  Expression Proposal 也始终绑定该 Observation evidence，因此省略表层归因不会丢失
  认识论标签。该权限只能证明“本轮用户报告了这些内容”，不能把报告升级为客观真相，
  不能交换角色与用户、不能将条件/假设/否定/未来/不确定的陈述升级成既成事实，也不能增加或
  替换实际主语、时间、施受关系、发生、状态、细节或动机，不能改写成角色自己的经历，
  也不能授权 Fact、Experience、World occurrence 或其他耐久 World mutation。历史报告
  和其他外部命题仍执行完整 `world_claim` 来源闭包；
- 历史/兼容的 `source-closure-review.7` 不再允许一个裸的 `v` 类别直接成为事实否决。每个 `v`
  类别必须带至少一条 `visible_findings`，逐条给出可见表达中的精确非空
  `visible_span`、可选的 `claim_index`、来源关系以及实际引用的 source refs。宿主只做
  可重放的坐标与权限核验：span 必须确实存在于本候选，claim index 必须落在本候选，
  finding 类别必须与 `v` 一致；宿主不使用关键词、正则、问号、句法模板或行为规则重新
  判断命题语义。仅当 reviewer 明确把某条 `undeclared_external_assertion` 标成
  `exact_current_report_discourse_coverage`、该 finding 不指向已声明 claim，且其全部
  非空 source refs 都属于本轮精确 `current_counterpart_report` 时，宿主才移除这一条
  accusation。若主复核器把命题标成 `unclosed`，该 accusation 仅在一个更窄的结构条件下
  仍属初始 review 的未决中间量：本候选没有任何 `ci`（包括机械 source mismatch），唯一
  剩余类别是 `undeclared_external_assertion`，所有待判 finding 均为
  `claim_index=null + source_relation=unclosed`，并且当前 Pinned Turn 确有精确
  `current_counterpart_report`。此时生产 composition 将同一个 source-review 能力和模型
  身份用于一次 `report-relative-entailment-adjudication.3` 命题级分阶段判断；它只看到
  原报告、可见文本、待判 span，以及同一个 Pinned Capsule 中最多十六条经类型校验的近期
  对话证明。历史用户 Observation 只以 `counterpart_report_record_only` 进入，证明“用户
  确实报告/说过这段内容”，不能证明报告内容客观发生；历史角色 Expression 只有在
  `delivery_state=delivered`、speaker ref 等于角色且 dialogue ref 属于该 Capsule 时才以
  `companion_delivered_expression_record_only` 进入，证明“角色确实送达过这段表达”。
  `provider_accepted` 不能证明对方看见过，角色旧表达也不能证明用户历史、外部事实或角色
  当时的动机。多条证明按 replay-safe sequence 排序；reviewer 返回的 refs 必须全部属于
  该有界包、唯一且按因果顺序排列，宿主只机械核验这些坐标，不从文本推断因果。两个记录
  同时存在不证明前者导致后者，时间更晚的用户状态也不能解释时间更早的角色提问。
  该判断只能逐项选择
  `covered_by_exact_current_report / covered_by_exact_dialogue_record /
  covered_by_first_person_immediate_private_continuity / not_external_proposition /
  retain_unclosed`，不能改写角色表达、判断是否应提问或补充证据。私有连续性的 covered 只适用于
  角色对自己刚刚的注意、误解、犹豫、不确定、改变想法、停止某个会话意图或可撤回自我评价等
  第一人称即时心理连续性；它不是
  report-relative 外部事实判断，也不评判回应是否相关、自然或有帮助。若同一 span 夹带地点、
  行动、他人、客观发生、可用性等 World 状态、共同历史或用户内心事实，仍必须
  `retain_unclosed`。已送达的角色记录可证明对应表达；只有有序同场记录直接蕴含停止追问等
  会话动作时才能闭合该陈述，有界记录中单纯缺少一条消息不证明停止。它以
  模型语义区分说话者实际断言与向对方索取未知答案的开放问题，也区分
  说话者当下的主观印象/评价与对对方内心状态的事实断言；并要求 report-relative 复述保持
  角色/用户、条件性、否定、时间与施受关系，不能反转为新的既成事实。疑问形式本身既不自动
  放行也不自动拒绝，带有新事实预设或实际承诺的问句仍必须 `retain_unclosed`；把一次报告
  扩展为某类人/地点/行为“通常、经常、典型”的 habitual/generic/frequency 命题也仍必须
  `retain_unclosed`。这是 reviewer 的受限
  命题判断，不是宿主的句法分类或允许话术表。宿主把 `covered` 机械绑定到冻结的当前报告 refs，不从词语、标点、问号或
  句法自行推断语义。它不能接收或清除 claim index、主语/时间/发生状态 mismatch、
  declared-claim source mismatch 或其他类别；同一类别若仍有任一 `retain_unclosed`，
  终态仍 fail-close，外部事实边界不因同句中另有自然承接而被洗白。
  这不是 rejection 后的 appeal：在上述窄条件下，主 reviewer 的 undeclared accusation
  要等分阶段判断完成才形成初审终态；不满足窄条件的 finding 则在主 review 后立即形成
  终态。每个**独立 author 候选**最多打开一次该分阶段判断；角色 fresh reselect 返回的
  完整草稿是新的候选，因而可以为自己的主复核结果各自打开一次，不能被旧候选已用的窄审
  消费。该阶段与主 reviewer 共用 source-review provider
  身份与 usage 审计，但主复核和窄审计是两个独立有界阶段，不建立仲裁 quorum 或另一种
  事实权威。其 wire 无效或调用超时时只允许同一 reviewer 对同一组 finding 受约束重试
  一次，且复用已完成的主 review，不得重跑主复核或递归开启本阶段。两次都失败时，宿主
  不能把未得到的窄审计结论当作 `covered`：它必须单调保留主复核已经形成的
  `unsupported` 终态，把本 attempt 的窄阶段标为已用，并进入既有的同角色完整重选。
  这与“主复核本身两次技术失败”不同；后者仍记录 reviewer 技术终态且没有 Proposal。
  provider 曾把规范顶层 `findings` 写成同结构的 `decisions`；宿主只在 `findings` 缺失时
  将这个单一键名归一回 `findings`。两键并存、存在其他顶层字段、遗漏 finding index 或
  非法 decision 值仍按无效 wire 处理；该兼容不参与命题判断，也不扩大任何来源权限。
  缺失 finding、越界 span、
  越界 claim 或类别矛盾都是 reviewer wire 结构失败，不提供任何事实结论：只允许同一
  reviewer 对同一 authored draft 重选一次 wire；再次失败记录
  `source_review_exception`，不得进入角色重写，也不得把无坐标的旧短 wire 当成语义拒绝。
  显式启用的隔离诊断使用 `isolated-source-closure-trace.3`：拒绝记录保留有界
  visible span、claim index、source relation 与 source ref 哈希；候选命题判定记录只
  保留 candidate/text/source ref 哈希、精确字符坐标、语义角色计数和 coverage 决定，
  从而可区分 inventory 判断“没有外部命题”和 coverage 将候选改判为
  `not_external_proposition`。原始 source ref、成功候选原文、private state 和 reviewer
  reason 均不得写入该诊断；
- 来源闭包重选使用 `source-closure-reselection.2` 的最小失败信封：它只携带本次被拒
  候选的 SHA-256 身份、失败阶段、归一化后的 `ci` / `v` 类别坐标，以及仅在窄审计保留
  外部命题时由模型给出的有限 epistemic failure dimensions。后者是对被拒绝外部命题
  （如 habitual/generic scope、主体或时间错配）的坐标说明，不是文案建议或角色行为规则；
  它不会进入普通终审 reviewer wire。角色模型仍看到原始
  Pinned Context 和本次调用现行的 hard-boundary manifest，并据此作一次全新的完整选择；
  被拒候选原文、reviewer 的自由文本 `r` / reason，以及被拒候选所属的旧 source alias
  映射都不得进入纠正输入。候选哈希只用于证明纠正所归属的 attempt，不能被解引用成文本、
  充当事实来源或暗示应保留的措辞。包含上述分阶段判断在内的初审终态若仍拒绝，才直接
  进入这一次 fresh reselect，不得围绕原候选局部修补，也不得在终态 rejection 上 appeal。
  当上述 exact qualification gate 成立时，每个最终可见 authored candidate（ordinary、
  paired cognition、backup 及纠正稿）先运行
  `candidate-external-proposition-inventory.5`。inventory 是轻量的独立模型，只定位来源
  相关语义：所有被断言或预设的外部事实、所有实际发生在本次对话之外的第一人称私有经历，
  以及为保持嵌入外部命题认识论范围所必需的私有包裹。它不判断事实真伪、来源支持、语气、
  动机或是否应发送。纯即时私态与不携带外部事实预设的 nonassertive discourse 可以省略或
  合并，不能仅因没有把寒暄、语气词、开放问题和同轮自我修正逐原子枚举就判 inventory
  不完整。它仍使用 `immediate_private_state / source_bearing_private_episode /
  embedded_external_proposition / standalone_external_proposition /
  nonassertive_content` 五种角色以及精确
  `(beat_index, char_start, char_end, text)` locator，但不再输出跨项 `parent_index`。
  完整 visible beat、语义角色和精确 span 已足以让后续 authority 判断范围；让 provider
  另外维护脆弱的父子图不会增加事实权限，反而会把 `invalid_parent /
  parent_not_private_state` 这类结构分歧变成技术失声。

  宿主只机械验证每个 locator 的 beat、边界、原文、角色集合和唯一性，不从 span 建立本地
  语义父子关系。若合法 beat 内的 `text` 是唯一一次逐字子串，宿主可将供应商误算的 offset
  归一为实际坐标；原文不存在或重复文本无法唯一定位时仍拒绝。Inventory 另外看到一个紧凑的
  `candidate-inventory-conversation-anchor.1`：它只包含当前 Observation、同一 Pinned
  Turn 中经类型校验的近期对话和显式 live-conversation boundary，用来区分“同一场对话内
  刚刚的自我修正”与“发生在对话之外的私有 episode”。该包固定声明
  `fact_authority=false` 与 `behavior_advice=false`，不进入 `source_evidence`，不能关闭任何
  可见命题、证明报告内容为真、暗示角色应如何说话，或替角色选择是否追问。
  Inventory 与 Coverage 同时收到机器可读的 `epistemic_semantic_contract`，其中
  `first_person_private_authority` 明确要求 Inventory **直接**把两类当前私有权限标为
  `immediate_private_state`：其一是真值只依赖当前 deliberation 或同一实时对话的心理连续性，
  包括注意、念头的继续、变化、淡去、停止或暂时不可及；其二是角色此刻对自身会话倾向的、
  可修正的第一人称自我理解。前者即使回看较早 turn 或使用过去/完成体语法也不会自动变成
  对话外 episode；后者即使使用泛指/习惯体语法也只授权“她现在这样理解自己”，不能证明
  统计频率、某次旧发言、重复发生的对话外行为或稳定传记事实。

  同一合同的 `external_world_boundary` 保留反向边界：当前或过去的活动、行动、位置、身体/
  环境状态、人物和 World occurrence 仍是需要来源的 external proposition；私有包裹不转移
  权限，嵌套依赖必须拆出并独立闭包。若私有命题的真值依赖某个对话外事件或具体旧内容，
  Inventory 也必须拆出依赖并要求来源。合同只定义模型可使用的认识论能力，不包含关键词、
  正则、固定话术或行为建议；最终语义分类仍由 Inventory 与 Coverage 模型完成。

  生产 composition 只在两个 transport 能力同时成立时安装这条路径：被请求的 inventory
  模型显式支持 `candidate-external-proposition-inventory.5`，且独立的 proactive visible
  authority 显式支持 `candidate-external-proposition-coverage.5`。双通道 authority 的每条
  可能获胜 lane 都必须声明对应 strict contract；显式注入也不能绕过 capability gate，
  且绝不退回 generic Flash author。任一能力缺失时，inventory 保持缺失并由历史 full
  source-review 边界 fail-close，而不是把一半新、一半旧的 wire 接进生产。

  V5 inventory 随后交给唯一的 visible source authority，生产使用
  `candidate-external-proposition-coverage.5`。authority 同时看到完整 visible beats、
  inventory 的全部来源相关坐标、需要裁决的 review locator 及冻结的 source ref table。
  Inventory 是唯一的穷尽命题发现阶段；Coverage 不能二次抽取、声明 inventory
  complete/incomplete、提出 missing locator、合并坐标或改写表达，只能对每个冻结的 review
  locator 恰好返回一个 `locator_index + decision + source_relation +
  source_ref_indexes`。这样保留“角色草稿 → 独立语义抽取 → 独立来源判定”的两阶段边界，
  同时避免同一个结构化 reviewer 先后扮演两次抽取器造成虚假的独立性、误报和额外调用。
  locator 与 source ref 均由宿主冻结表按索引还原，provider 不回显文本、offset 或原始
  ref，也不能返回 `ci / world_claims / inventory 外文本`。

  Coverage V4 保留为显式兼容能力，不再由生产 composition 协商。合法的 V4 incomplete
  是一个带具体坐标的可见 authority 拒绝：宿主立即把每条
  `missing_finding` 变成同坐标的 `unclosed` finding，并终结该候选进入既有同角色完整
  Expression 重选。它不再盲目重跑 Inventory→Coverage，也不把缺失 locator 当成新证据或
  可局部修补的文案建议。V4 的 incomplete 若缺少 locator、同时返回普通 findings、回显错误
  文本或越界坐标，则属于 wire 无效；同一 authority 只获一次结构重选，再失败即
  `coverage_invalid` 技术终态。历史/兼容 Coverage V3 的
  `inventory_complete=false + findings=[]` 无精确 missing locator，因此继续保留原来的一次
  completeness Inventory 重选与第二次 Coverage 终结语义；V3 账本与 replay 结果不改写，
  但生产 capability negotiation 不再选择 V3。

  V4 完整时 authority 对每个 review locator 恰好返回一个
  `locator_index + decision + source_relation + source_ref_indexes`；locator 与 source ref
  均由宿主冻结表按索引还原，provider 不回显文本、offset 或原始 ref，也不能返回
  `ci / world_claims / inventory 外文本`。

  V8 真实轨迹证明，把 Coverage V3 设为可静默覆盖 Inventory temporal role 的唯一权威会
  形成洗权：Inventory 已将“下午翻书”判成 `source_bearing_private_episode`、将其中的
  世界内容判成 external，Coverage 却把所有 locator 用无 refs 的
  `first_person_immediate_private_continuity` 关闭。因而 V5 的 temporal role 现在是
  **模型作出的来源能力分类**，不是可随意保守猜测的提示标签；Coverage 仍是来源闭包权威，
  但只能在 Inventory 已授予的无来源能力内裁决。Inventory 应直接把同一场实时对话内的
  回溯误解、犹豫、不确定、自我评价、改变想法以及同一对话内注意/念头的消退或停止标成
  `immediate_private_state`；角色当下、可撤回的第一人称自我看法也使用这个角色，而不因
  泛指语法升级为耐久人格事实。对话外较早的私有 episode 仍标成
  `source_bearing_private_episode`。宿主依然不读关键词、时态或句法。

  `candidate-epistemic-role-conflict.1` 现在只处理真正的 capability 矛盾：例如 Inventory
  给出 `source_bearing_private_episode`，Coverage 却已经授予 no-ref private continuity
  或 `not_external_proposition`。对 external role 授予 private continuity 的 Coverage
  结果仍由权限交集直接保持 unclosed。普通 `unclosed` 不是两个 verdict 的矛盾，不再把它
  回送给 Inventory 自己作第二次 role 判断；这样避免同一个抽取模型重复自审和增加一次延迟。
  role-conflict 的 wire/供应商失败仍是技术失败，不能伪装成语义拒绝。

  所有普通 `unclosed` 改由下述独立 source-authority 窄裁决形成终态。角色在本轮形成的
  “突然想起/注意到”不能倒过来证明其声称的旧活动、身体或环境状态、地点、行动、发生史或
  旧动机。protocol 始终明示 `host_text_classifier=false`；宿主只校验 contract、角色绑定
  的允许决定和证据 ref，不能用问号、关键词、正则或句法自行套用语义判据。

  过去式或完成体本身仍不能把“那是我理解错了”升级为 World 传记；真正发生在对话外的私有
  episode 必须由 Pinned Context 闭包。角色自己可观察的身体状态、世界内活动、行动、所在
  地点或已经发生的事情，即使以第一人称当前时态表达，仍是 World proposition，必须由
  current situation / experience 等精确 Pinned Context 闭包。宿主不得建立“刚醒/吃饭/
  出门”一类词表。向对方索取未知值不等于断言该值，但问句真实预设的主语、时间、发生、状态
  和细节仍要闭包。精确当前报告与类型校验后的近期对话证明继续可用；
  `exact_dialogue_record_coverage` 仍机械核验 ref 归属、speaker、delivery state 与因果
  顺序。近期对话 proof 的精确 dialogue ref 进入 candidate-wide `source_ref_table` 并获得
  冻结 index，因而 authority 可以真正引用它；这些 ref 不进入
  `declared_world_claim_source_refs`，只能通过 `exact_dialogue_record_coverage` 使用，不能被
  `declared_world_claim_source_coverage` 洗成客观 World 事实。当前报告不能倒推“此前已经
  听过、聊过或共同知道”某件事；这类历史会话暴露必须引用直接蕴含它的历史对话记录。
  用户记录只证明该用户报告过对应内容，角色记录只在已送达时证明角色表达过对应内容，两者
  都不证明报告内容客观为真、对方内心、旧动机或额外因果。所有这些语义判断属于 authority
  模型，宿主不读取问号、关键词、语法时态或句法来放行。

  Coverage 的普通 `unclosed` 在本轮存在精确 current report、没有 claim index 或其他机械
  拒绝时，可让 **所有** Inventory V5 review roles 进入一次独立的
  `report-relative-entailment-adjudication.3`。该阶段只复用已冻结的报告、近期对话证明和
  精确 locator；不能重跑 Inventory/Coverage、改写表达或增加事实。它是 Inventory/Coverage
  对自然语言角色误标的最终窄域 error-control，不是 rejection 后的行为 appeal。

  `source_bearing_private_episode` 与 `standalone_external_proposition` 可由独立 reviewer
  裁为 exact current report、exact typed dialogue record、真正的当前第一人称心理连续性、
  nonexternal，或继续 `retain_unclosed`。这使被 Inventory 错标的同场注意、犹豫、误解、
  停止追问以及可撤回的自我评价不必先经 Inventory 自审。已送达的角色 dialogue record 只
  能证明角色确实表达过对应内容；它可以闭合对该表达的忠实复述。只有按序的同场记录直接
  蕴含停止某个会话动作时，才可闭合“停止追问”等陈述；有界记录列表中单纯没有一条消息不
  能证明沉默或停止。

  `embedded_external_proposition` 仍绝不能选择
  `covered_by_first_person_immediate_private_continuity`；它只能由精确 current report、
  精确 dialogue record 闭合，判为确实 nonexternal，或保留 unclosed。每个 disputed finding
  都携带宿主从 Inventory 结果绑定的 `inventory_semantic_role` 与 `allowed_decisions`；
  宿主不读原文，只机械核验决定和 dialogue refs。对话外活动、行动、地点、身体/环境状态、
  当前可用性等 World status、外部发生史或耐久历史必须 `retain_unclosed`；同句私态不能为
  它们洗权。越界决定属于无效 wire，同一 reviewer 可按原协议受约束重选一次，再失败则
  单调保留 unclosed；窄审 wire/供应商失败也不能伪装成通过或角色沉默。

  Coverage V5 的 provider strict schema 把 `decision / source_relation /
  source_ref_indexes` 组合编码成互斥分支：`not_external_proposition` 和 `unclosed` 必须各自
  搭配同名 relation 与空 refs；即时私人连续性必须空 refs；证据型 closed relation 必须至少
  一个 ref。顶层只允许 `contract + findings`，明确禁止 `inventory_complete` 与
  `missing_findings`。这里消除的是 wire 自相矛盾，不是宿主替模型作语义决定。V3/V4
  继续使用相同的 finding 互斥约束和既有 completeness 语义，但只用于历史/兼容路径。

  Coverage provider 若只遗漏顶层固定 `contract`，宿主仅在 raw 顶层字段**精确**为
  V2/V3 的 `{inventory_complete, findings}`、V4 的
  `{inventory_complete, findings, missing_findings}` 时，才注入本次调用已经协商的固定
  discriminator；V5 只在顶层精确为 `{findings}` 时做同样归一。归一后从头执行完整
  strict schema 与 authority 校验；不会补写或修改任何
  finding、missing locator、decision、relation、ref 或语义字段。显式错误 contract、
  额外字段和其他 shape 仍失败。隔离审计只记录原 wire hash、候选 hash、规范化代码和注入的
  固定 contract，不保存私态、提示词或额外 provider prose。

  Candidate-wide Coverage 还可看到同一 Pinned Turn 中来源明确的 stable identity 和当前
  relationship slice；这两类不是行为建议，也不是整份 Capsule 的 bearer token。authority
  只有在 locator 被对应值直接蕴含时，才能用
  `pinned_context_authority_coverage + source_ref_indexes` 关闭它。宿主只接受
  identity/relationship 条目的精确 refs；其他未引用 Context、advisory、private state 和
  source alias 均不能借该 relation 扩权。`declared_world_claim_source_coverage` 另有互斥
  权限集合：它只能引用 `source_evidence.required_source_refs` 中由本候选实际
  `world_claims` 声明、且并非 dialogue record-only 的 refs。自动暴露的 current report、
  identity 和 relationship ref 若未被本候选声明，只能走各自专属 relation，不能仅通过改名
  洗入 declared relation；实际声明的 claim 仍由后续 claim-only `.8` 审核完整语义闭包。
  `.8` 继续只收到草稿实际声明 refs 的最小闭包，不因 Coverage 的可见权威包而扩大。

  显式隔离审计可为最终 wire 失败额外记录有界、白名单化的 provider attempts：Inventory
  只保留 locator/role，Coverage 只保留 index/decision/relation/source-ref indexes，并
  同时保留原 wire hash。它不保留提示词、私态、原始 authority ref 或未知 provider 字段；
  未安装进程内 audit sink 时完全不落盘，也不得参与恢复或业务判断。

  Coverage V5 是未声明可见命题的来源闭包权威；除上述对全部普通 unclosed roles 作一次
  role-aware、证据受限的 report-relative 最终窄裁决，以及真正 temporal-role capability
  矛盾的单独有界 adjudication 外，系统不再启动一个 full reviewer 重判同一可见文本。coverage
  supported 后，只有候选实际包含 `world_claims` 才调用
  `source-closure-review.8`，且 `.8` 只能返回 declared claim 索引维度，不能产生或清除
  visible finding。coverage 拒绝或任一 claim 未闭包都沿既有同角色 fresh reselect /
  terminal validation 处理：首稿只获得一次同角色 fresh reselect，纠正稿仍被拒绝即以
  `authored_expression_reselection_invalid` 技术终态结束，不再启动备用角色或第三次选择。
  历史 Inventory V2/V3 与 Coverage V1 保持只读重放兼容且不能宣称
  `visible_authority_exhaustive`，仍搭配旧 full review；Inventory V4 / Coverage V2 只为
  既有 payload 和测试兼容，Coverage V3 保留其既有 completeness 重选与终态语义；这些旧
  contract 都不再由生产 capability 协商请求。

  inventory/coverage 无效 wire 各获一次只含有界原输出与稳定 `code / field` 的结构重选，
  timeout/transport 重试仍复用原请求；两次失败才是技术失败。最终诊断只记录 authored
  candidate SHA-256 与 `inventory|coverage + code + field`，不得记录 raw candidate、
  private state、source ref 或 provider 原文；`ValidationTechnicalFailure` 必须以
  `inventory_invalid / coverage_invalid` 直接进入可审计技术终态，不能被折叠为
  `backup_exception` 或触发一个新角色候选。该重选不携带事实判断或行为建议。生产路径不存在可以把任一语义
  rejection 改写为通过的 `source-closure-appeal`。生产仅保留两个发生在 Coverage 终态
  形成前的窄域阶段：上述接收全部普通 unclosed Inventory V5 roles、并按 role 限制 exact
  report/dialogue/private/nonexternal 权限的独立 report-relative error-control，以及只
  处理 Inventory role 与 Coverage 已授予 capability 真冲突的
  `candidate-epistemic-role-conflict.1`。普通 unclosed 不再触发后者；两者都不能补充证据、
  清除范围外的机械拒绝或推翻已经形成的终态 rejection。其他额外 reviewer、仲裁或分歧比较
  只能作为 shadow / 诊断信号；
- 同一主候选已经完成上述完整重选但仍被拒绝时，不再把该语义终态伪装成普通 author
  `ValueError` 以触发备用角色。系统抛出带纠正调用 `model_call_id / request_hash`、实际
  attempted model identity 和合并 usage 的
  `authored_expression_reselection_invalid`；Deliberation 将其记在 corrective slot 后终止
  本 attempt。被拒原文与 reviewer 自由文本不进入 World、Recall、日志、备用输入或持久
  审计；
- 顶层逻辑时间与经校验的当前时段形成独立、可重放的 `pinned_time` attention source，并使用
  不占用普通 `S1/S2` 顺位的 `T1` alias。它同时保留 World UTC 逻辑时刻和经
  Current Situation 验证的角色本地时刻，不能让跨日时区把本轮刚到的消息误读成“昨晚”。
  角色可以在私有状态中注意“现在是深夜”等当前时间；伪造 token、越界 ref 或必需的当前
  Observation 漏引仍由 PrivateTurnState 结构校验拒绝。若矛盾时段或相关外部材料进入
  可见/耐久输出，则由该输出 seam 的来源闭包拒绝；Clock 只证明时间，不证明地点、活动、
  人物或生活事件；
- Recall 结果和配置的恢复模型都是各自独立创作的候选；每个候选在自己的有界校验窗口内
  最多获得一次完整角色重选。同一候选不能二次纠正，恢复候选不能再次 Recall 或再开恢复
  作者，也不能开启 hedge、生成旁路话术或绕过任何来源与 Action 校验。不含 Recall 的普通
  attempt 最多四次角色调用：主候选、主候选至多一次纠正、至多一个恢复候选、恢复候选至多
  一次纠正。若第一次合法选择请求 Recall，唯一一次 Recall follow-up 额外占用一次调用，
  最坏上限为五次。所有计数都是 attempt 级硬预算，耗尽即 fail-close，不存在递归纠正或
  无界循环；
- 任何 author 结构纠正产生的新候选，无论来自主模型、Recall follow-up 还是恢复模型，
  都必须在 Acceptance 与 Action 授权前完成最终的 `visible_text + world_claims` hard
  source review。结构合法、private state 合法或此前某一候选通过复核，都不能让新可见候选
  绕过它自己的最终来源闭包；
- 并行的 provisional 首条只是可丢弃候选，不是一次已经作出的角色决定。若它不满足该
  因果边界，系统丢弃候选并继续等待同轮完整结果，不为 provisional 单独增加第三次调用，
  也不会因此把整轮判成角色沉默；
- 状态作为 Proposal 审计的一部分参与哈希，但不进入 TypedChange、World Projection、
  Memory、Relationship 或下一轮 Context，也不为事实陈述或 Action 提供权限；
- `silent` 只有在合法 Proposal 已落账时才是角色决定。Observation、模型调用或校验的技术
  中断不能被折叠成沉默：入站 Observation 与轻量 Expression reliability lifecycle
  原子 open/claim；失败后按 10/30/120 分钟退避，新的用户入站会终结旧重试。
- claim 本身不算一次失败。模型审计使用当前 Expression attempt identity；claim 后、调用
  前崩溃会恢复同一失败档位。claim owner 必须标识一个具体 Runtime 实例：原实例可在
  120 秒 provider in-flight lease 内继续自己尚未落账的生成，其他实例必须等 lease
  过期后 reclaim，不能把仍在运行的 provider 调用误判成崩溃。该短 lease 只证明调用
  所有权，不承载失败退避；已有技术失败仍从本次 acquired time 严格投影
  10/30/120 分钟重试期限。若 Proposal 已落账但 Action/终态尚未提交，已经不存在重复
  生成风险，任一实例都可以在 CAS 与 effect-once 下继续该 Proposal 的
  `now / later / silent`，不得重新生成表达。
- 新用户 Observation 落账时，尚未越过 Action 授权边界的旧 active Expression
  lifecycle 必须在同一原子提交中由该新 Observation 作为因果来源终结；不得等到旧
  retry 到期、不得为“发现已过时”先增加 attempt。已经授权的 Action 不属于可撤销的
  cognition，继续由原 effect-once dispatch 与 settlement 链处理。
- Snapshot、Context Capsule 或 Current Self State 在 provider 调用前发生技术异常时，
  必须写入与 Observation、cursor 和当前 Expression attempt 精确绑定的无 Proposal
  ModelResult。该审计不保存异常正文、半成品 Context、私有状态或本地话术；生命周期据此
  执行 10/30/120 分钟退避，而不是在同一 claim 上忙循环。
- provider 用量属于不可变 Model Result 审计。主调用、Recall follow-up、来源复核及各
  候选实际发生的有界重选，其 provider-reported usage 都合并进同一结果；缺失计量不能被
  本地估算伪装为真实 provider 用量。
- 普通成功路径使用 18 秒取消上限，其中为 Acceptance/dispatch 保留 1.2 秒。它不增加
  正常 provider 的实际耗时，只避免在实测峰值仍有进展时过早取消。只有主
  候选已经真实返回 invalid/exception/timeout，或候选期限确实到期时，才可为已配置的
  角色恢复调用打开一次独立、默认 12 秒的技术恢复窗；它不是投机 hedge，也不会延长正常
  成功路径。主候选若已经消耗一次受约束纠正，恢复模型仍可选择一次完整
  `now / later / silent / multi-beat` 结果；该新候选若不合法，只能获得自己的一次有界
  校验重选。无 Recall 时角色调用受上述四次硬上限约束；存在唯一一次 Recall 时最坏五次。
  每次仍是角色模型从同一 attempt 的 Pinned Context 作完整选择，不是宿主模板或确定性
  话术，预算耗尽后不得继续调用。
- 角色 author 已经返回合法草稿后，独立来源复核的超时、供应商异常或无效结构属于
  reviewer 技术失败，不属于 author 失败。生产样本中合法的 report-relative 结果曾在
  10.30、10.65 与 18.03 秒返回，因此单次 source-review 调用使用 22 秒取消上限。角色
  候选一进入主复核，就先打开一次固定、默认 46 秒的候选级 validation window，而不是
  先用 author deadline 剩余的几秒启动一个注定被取消的调用。只有首个调用真实失败后
  才能发起第二次；该窗口容纳两次完整 22 秒 attempt 和调度余量。首调用、重试、后续窄
  阶段都不得续开或滑动该窗口，首调用成功的正常路径也不会预先发起第二次。主复核明确
  拒绝后的同角色完整纠正及其新候选终审另有固定 100 秒窗口。V5 路径不再并行调用两个
  可见文本 reviewer：先完成 inventory，再由唯一 coverage authority 逐项判断来源；
  只有存在 declared `world_claims` 时才追加 claim-only `.8`。每个独立 provider 阶段仍只在
  首次 transport/wire 真实失败后使用一次有界重试；生产 Coverage V5 不拥有 completeness
  输出，因此不会消费 inventory completeness 重选。历史/兼容 Coverage V4 的精确
  missing locator 立即终结，Coverage V3 保留原有的一次同合同 inventory 重选。异常路径
  不能续窗；无 claim
  的正常首稿不会支付 `.8` 调用，也不会因另一个 full visible review 产生重复延迟。
  这些窗口不得调用
  quick author、不得重新形成 `PrivateTurnState / ExpressionDraft`，也不得启动上述
  12 秒角色恢复窗。复核仍必须在 Proposal 验证及任何 Action 授权之前完成；两次复核都
  失败时记录技术终态：transport/通用 reviewer 失败使用
  `source_review_timeout / source_review_exception`，inventory 或 locator coverage 在各自
  一次 wire 重选后仍非法则分别使用不含原文的稳定码
  `inventory_invalid / coverage_invalid`。本轮没有
  Proposal 和 Action，不能把未复核草稿当成角色回复或沉默。复核恢复窗以实际
  provider call/candidate identity 隔离；一个已过期候选不能消费后续 pinned candidate
  的复核能力，也不能把自己的 deadline 泄漏给后续 author。复核请求只携带草稿实际引用
  的完整语义 evidence item、当前 Observation 的 report-only 证据和命中的 identity
  source；未引用 Context 不参与复核，但完整 Capsule 仍由 Acceptance 校验。引用无法
  解析时 fail-close 为 reviewer 技术失败。counterpart Observation 只证明对方报告过
  什么，不能证明报告内容客观发生，更不能把对方的报告变成 companion 自己的 Experience。
  可见来源拒绝交给角色重选时，信封只附加未闭合 Inventory semantic role 的聚合计数，
  不回灌 locator 或原稿。若 `companion_life_authority_availability` 的某个 lane 为空，
  该空值表示同一 Pinned Context 对该 lane 中任何未固定的 earlier/current 生活事件都没有
  授权，而不是让角色用另一个无来源事件替换原事件，也不证明现实中没有发生过该事件；
  candidate substitution 与 `PrivateTurnState` 都不能创造事实权限。这个类级来源边界不
  选择角色的 `now / later / silent`、立场、消息数、节奏或措辞，它们仍由同一角色身份完整
  重选。角色身份与模型执行器不是同一概念：生产 redundancy 部署只在来源闭包拒绝后的
  **唯一一次**完整重选中，复用已经配置的 OpenAI 恢复 checkpoint 作为更强的同角色执行器；
  它收到普通作者的原角色 system、同一个 Pinned Context 和最小失败信封，不使用技术恢复
  话术，也不获得任何新事实、行为目标或额外选择次数。普通首稿、结构/Recall/claim-shape
  重选及其随机性仍由原路由执行，正常成功路径不会调用这个更强执行器。纠正稿必须改由不含
  该 OpenAI 作者身份的 recovery source authority 执行终审；只有 exact qualification
  gate 成立时才同时使用独立 Inventory V5；若不能
  证明作者、inventory 与 reviewer 独立，则不安装这条强重选 lane，绝不能退化成角色自写
  自审。纠正稿仍非法时直接记录
  `authored_expression_reselection_invalid`，不得再启动第三个角色候选。
  `ModelOutput.model_id`、失败的 `attempted_model_id`、winning call/request identity 与
  聚合 usage 都必须记录实际生成纠正稿的执行器，不能继续冒记为首稿模型。纠正信封机器
  可读地声明 `answer_required=false` 与
  `satisfy_request_required=false`，所以角色可以选择 now、later 或 silent，不必为了看似
  完成用户请求而补写生活史；返回前由本次角色执行器依据原 Pinned Context 执行一次
  `final_source_self_check`，宿主不读取文本作语义分类，最终 Inventory/Coverage 仍负责
  effect-bearing 验证。首稿已经承载表达随机性，来源闭包纠正以 temperature `0` 执行，避免
  纠正采样把一个无来源 episode 换成另一个；结构、Recall 与 world-claim shape 纠正继续
  使用原有 `0.25`，不改变普通创作随机性。
  更强执行器的这一次来源纠正另外绑定
  `expression-source-reselection-direct.1` 严格传输契约。该契约由本次部署实际安装的
  Expression capabilities、provider message binding、是否必须判断 Response Expectation
  以及本次 Pinned Context 展示的 source token 确定，并携带最终 JSON Schema 的内容哈希；
  provider request hash 因而同时绑定角色身份、能力形状和 schema。Schema 只关闭不可执行
  的 wire：根对象含完整 `expression_draft` 与 nullable `episode_disposition`，内部仍完整
  接受角色自由选择的 `now / later / silent`、一条或多条 Beat、任意合法 cadence、自由
  stance、动机、语气、问题和措辞；它不含动机枚举、情绪—表达矩阵或社交行为指导。当前
  transport 无法执行的 modality、缺失的私有状态/显式作者字段、错误 timing/Beat 组合和
  未绑定消息的 reaction 在 provider 结构层即不能生成；跨字段时间关系及事实权限仍由既有
  materializer 与独立来源复核守边界，Schema 本身不获得语义事实权威。direct 契约不得偷套
  到 combined appraisal/expression wire；没有精确匹配的 combined 契约时必须 fail-close。
  纠正稿返回后先完整执行 deployment-bound ExpressionDraft 物化预检，再支付独立
  Inventory/Coverage 终审，避免“来源终审已通过、最后才发现缺 cadence/private state”
  造成失声；终审通过后复用同一已预检结果，不再重新解释模型字节。严格 schema 拒绝、refusal、
  空内容、无效 wrapper 或物化失败都记录为
  `authored_expression_reselection_invalid` 技术终态，不降级为 generic JSON、不换另一作者、
  不开启第三次角色调用，也不伪装成角色选择 silent。显式隔离审计只可记录候选 SHA-256、
  物化发生在终审前/后的机械阶段、稳定错误类别与字段路径；不得保留草稿原文、可见消息、
  private state、source refs、reviewer reason 或异常文本。
  只有真正的 author
  invalid/exception/timeout 才能进入角色恢复调用。
- Appraisal 与 Expression 的组合调用只可在完整 `ModelInput` 身份保持不变时复用候选。
  `call_id`、World/Deliberation/ledger cursor、Capsule、route 或 model-facing Context
  任一变化，都必须在新的 Pinned Turn 真实调用角色模型；不得把旧回复字节、私有状态或
  usage 改绑到新请求。每条 `ModelResultRecorded` 的 call identity 与 request hash
  必须能逐一对应一次实际 provider invocation，包括初次组合调用、Recall follow-up、
  来源纠正和结构重选。其 request hash 以 provider 实际收到的 messages 与 temperature
  计算；纠正调用另有自己的 identity，并以 parent call 记录来源，不能继承原调用哈希。
  组合调用中的 Expression bytes 在最终 Expression seam 接受前只是缓存候选；Appraisal
  不得为这份非权威候选提前消费来源复核。只有 exact-origin 复用或新 cursor 上重新创作
  后真正进入 Expression 权威边界的草稿才执行来源复核与必要的一次完整重选。
- Appraisal 或其他 World 写入使 cursor 前进后，最终 Expression 请求必须在新的 Pinned
  Turn 重新编译来源绑定的同轮 advisory。旧 cursor 上的组合候选或 advisory 即使内容
  相似，也不能被标成新请求的结果。
- provider 输入同时携带紧凑、机器可读的 `expression-hard-boundaries.8`：它只列出
  时间字段范围、跨字段关系、该 provider 实际可见的 source ref 对应哪些 effect-bearing
  claim scope、精确 biographical coordinate 的字段和值、精确当前报告的 report-only
  discourse 权限，以及 `single_report_epistemic_scope`。后者明确声明“一次、单个发生的
  当前报告”不能授权 class-wide / habitual / generic / typical / frequency 断言，并带有
  `behavior_advice=false`；它只是证据能证明到哪里的认识论边界，不建议角色问什么、如何
  回应或是否沉默。source-closure fresh reselect 收到完全相同的机器可读边界，而不是
  reviewer 文案或替代回复。manifest 还包括
  `PrivateTurnState / attended_source_refs` 仅属 audit / attention
  provenance 的权限声明。它不建立 private semantic review，不得从完整 Capsule 把已被
  展示层隐藏的 proof ref 重新带回模型，也不得包含动机、语气、提问或回应方式建议；完整
  Capsule 仍是 Acceptance 的唯一事实权限。
- 主动联系与普通入站共用同一 WorldClaim scope 编译器和同一可见来源闭包；不得各自解释
  `world_life`、对话 speaker、biography 或第一人称私有连续性权限。任何主动草稿都要在
  ExpressionPlan / Action 前通过当前 qualified 的唯一来源 authority：V5 transport 全部
  证实时依次使用 Inventory V5 与 Coverage V5，否则使用严格的 full
  `source-closure-review.7`；
  只有实际含 `world_claims` 时才追加 claim-only `.8`，无 claim 的主观表达、轻量问候和
  自然问题只免除 `.8`，不能跳过可见命题 inventory/coverage。authority 只判断可见命题
  和 claim 是否被精确 evidence 蕴含，不判断动机、语气、是否联系或措辞。首次语义拒绝只
  触发同一角色作者基于原 Pinned Context 的一次完整重选；新候选重新机械校验和语义复核，
  仍失败则 `grounding_rejected`。inventory、coverage 或 claim reviewer 未配置、超时、
  供应商异常或 wire 无效是技术失败并进入既有退避，不能伪装成角色沉默；
- 生产 QQ 的同轮主观反应由角色模型依据当前 Observation、Current Self State 和
  `PrivateTurnState` 形成；本地 Appraisal/Affect 的耐久整理不串行阻塞可见回复，而由已
  打开的 `interaction_appraisal` trigger 在后台完成，供后续轮次继续使用。本地模型看到
  紧凑、来源绑定的角色上下文并自行选择评价、余留情绪、倾向、立场和显示方式；为避免
  小模型在无关 Capsule lane 上耗费数分钟 prefill，这一路输入只展示逻辑时间、当前
  `Current Self State`、至多四条近期对话和当前消息，完整 Capsule 仍保留为 Proposal
  校验与重放权威；系统不得
  用关键词翻译或情绪—行为矩阵补写这些字段。结构或枚举无效时，同一模型可依据原 Context
  受约束重选一次，系统只指出失败边界且不指定语义答案。合法 Appraisal 与 Affect 复用
  既有原子接受链；任何后处理失败独立审计，不能撤销或中断已形成的 Expression。
- 旧 Quick Reaction 的随机 `act/hold` 与本地社交矩阵不得进入生产 QQ 路径。Reaction、
  Sticker 和多条文字一样，只能作为具有合法 `PrivateTurnState` 的角色 Expression
 选择；打字状态仍可作为角色自行选择的无语义 provider presence metadata。角色基础配置
 只描述可持续的人格与边界，不预先赋予“对用户好奇/有好感”或“想知道时就追问”这类当前
 关系动机；这些只能由本轮私有状态、实际关系和情境形成。`PrivateTurnState` 可以自由形成
 第一人称心理材料，但不得为增加真实感凭空补写当前活动、地点、身体事件、他人或既成历史；
 若外部情境确实进入其注意，只能来自本轮 Pinned Context，并记录相应 attention provenance。
- 常规模型路径技术失败后的快速恢复仍是一次角色模型决策：模型可以选择
  `now / later / silent`，并自行决定 Beat 数量、模态和节奏。只有角色恰好选择
  “立即发送单条纯文字、无事实 claim、无 expectation”的无损子集时，运行时才可使用
  兼容的 Minimal Proposal 表示；其他合法选择必须保留为完整 Expression Proposal。
  本地恢复代码不得把失败改写成强制立即单条回复。
- `current_self_state.recent_self_experiences` 的小预算同时为即时 World Life occurrence
  和已提交 Experience 保留代表位置；一条繁忙的生活事件 lane 不能把另一条耐久经历
  全部挤出角色自己的当下视野。完整 Capsule 仍保留两条 lane 的来源材料。

生产 QQ capability 要求该字段；旧 Proposal 和旧单键 Recall 保持可解析、可重放。旧
Proposal 在字段缺失时维持原 canonical JSON 和哈希。

## Consequences

角色可以因共情、联想、自身经历、疲惫、愤怒、无话可说或任何未枚举的主观原因选择单条、
多条、追问、转述自己、稍后回复或沉默；确定性代码只验证因果字段顺序、来源闭包、能力和
外部效果边界。

`PrivateTurnState` 不是耐久“内心事实”。值得长期保留的情绪、印象、承诺和记忆仍分别走
既有 Appraisal/Affect、PrivateImpression、PrivateCommitment 与 MemoryCandidate
接受链。主动联系继续复用既有 `impulse_summary` 和主动情境审计，本 ADR 不另造主动动机
分类或第二套 Recall 通道。

可靠性生命周期只决定“技术工作是否仍需继续”，不决定角色应不应该说话。它可以恢复模型
尚未完成的选择或执行已经完成的选择，但不能把失败模板发给用户，也不能推翻一个有来源的
`silent`。

Expression Episode 目前保持 `shadow`，不影响生产可见路径。其 provisional 候选不使用
关键词、问号或“像不像占位话术”的语义规则拒绝模型文本；但在它仍只支持单条即时文字的
情况下，不得直接切换为生产可见模式，否则会让宿主结构替角色缩窄表达选择。

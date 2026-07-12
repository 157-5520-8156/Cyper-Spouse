# 高拟真虚拟伴侣情绪：一手资料研究与可移植方案

更新日期：2026-07-12
范围：DeepSeek V4 Flash API、计算情绪架构、关系与长期情绪动力学、语用理解、记忆/反思、校准与纵向评测。

## 结论先行

1. **不存在已被研究证明的单一“业内最优人类情绪模拟方案”。** 最有证据、也最适合本项目的工程路线，是把事件的认知评估（appraisal）、连续情感维度、离散情绪、关系状态、多个时间尺度、应对/调节、记忆反思和表达策略组合起来，而不是让语言模型直接维护一个“当前心情”字符串。EMA 将情绪描述为“解释环境—评估—应对—再评估”的连续循环；Scherer 的 Component Process Model 同样强调动态评估和多个反应组件的协调。[EMA 原论文](https://www.ccs.neu.edu/~marsella/publications/pdf/MarsellaCSR09.pdf)，[Scherer 2009](https://doi.org/10.1080/02699930902928969)
2. **本项目已有的事件溯源 Affect、人格 baseline、衰减、边界伤害与渐进修复是正确地基，但目前更像“规则映射到情绪向量”。** 下一阶段应增加结构化评估维度、关系/权力模型、情绪调节策略、状态依赖惯性、上下文语用评估，以及把内部状态与外部表达分离。
3. **`deepseek-v4-flash` 是 DeepSeek 官方 V4 Flash 的准确 API 标识。** 官方 API 同时提供 `deepseek-v4-pro`；旧别名 `deepseek-chat` / `deepseek-reasoner` 将于 2026-07-24 15:59 UTC 退役。在退役前，它们分别路由到 V4 Flash 的非思考/思考模式。[DeepSeek V4 发布说明](https://api-docs.deepseek.com/news/news260424/)，[模型与价格](https://api-docs.deepseek.com/quick_start/pricing/)
4. **V4 的思考模式默认开启。** 普通伴侣对话若要低延迟、低 token，应显式发送 `thinking: {type: "disabled"}`；仅在讽刺、指代、关系冲突、多事件归因等高歧义评估中按需开启。思考模式只支持 `high` / `max`（兼容参数 `low`、`medium` 会被映射为 `high`），因此不能依靠设为 `low` 真正降耗。[DeepSeek 思考模式](https://api-docs.deepseek.com/guides/thinking_mode)
5. **“让人察觉不出来”不能作为可验证或安全的产品承诺。** 可以评测情绪因果一致性、长期连贯性、自然度与机械感，但不应以隐瞒 AI 身份或诱导用户误认真人作为指标。研究也显示 AI 伴侣的心理与关系影响需要长期观测，而非一次性图灵式判断。[AI companionship 纵向研究](https://arxiv.org/abs/2510.10079)，[12 周人机关系研究](https://doi.org/10.1016/j.ijhcs.2022.102903)

## 1. DeepSeek V4 Flash：官方 API 事实

### 1.1 模型、模式与上下文

| 项目 | 官方事实 | 对本项目的直接含义 |
|---|---|---|
| 模型标识 | `deepseek-v4-flash` | 配置和测试应断言完整标识，停止使用 `deepseek-v4-pro` 和即将退役的旧别名。 |
| Base URL | OpenAI 格式 `https://api.deepseek.com`；Anthropic 格式 `https://api.deepseek.com/anthropic` | 现有 OpenAI-compatible 客户端可继续使用。 |
| 思考开关 | `thinking: {"type": "enabled" | "disabled"}`，默认 `enabled` | 普通回复必须显式关闭，否则迁移到 Flash 后仍可能大量生成 reasoning token。 |
| 思考强度 | OpenAI 格式 `reasoning_effort: "high" | "max"` | `low` / `medium` 不会真正降低强度，而会映射成 `high`。 |
| 上下文 | 官方标称 1M context；最大输出 384K | 不是把整个世界日志塞进 prompt 的理由；长上下文仍增加缓存未命中成本和注意力噪声。 |
| 功能 | 两个 V4 模型均支持 JSON Output、tool calls；FIM 仅非思考模式 | 结构化评估可以使用受 schema 约束的 JSON，但仍必须本地校验范围与枚举。 |

上述字段由 DeepSeek [API 首页](https://api-docs.deepseek.com/)、[Create Chat Completion schema](https://api-docs.deepseek.com/api/create-chat-completion) 与[模型价格页](https://api-docs.deepseek.com/quick_start/pricing/)直接给出。

思考模式下，`reasoning_content` 与最终 `content` 分开返回。普通、无工具调用的多轮聊天不需要把旧 reasoning 放回上下文；如果该轮发生了工具调用，则后续请求必须完整回传对应 `reasoning_content`，否则官方文档说明会返回 400。[DeepSeek 思考模式](https://api-docs.deepseek.com/guides/thinking_mode)

### 1.2 Usage 与 prompt caching

非流式响应的 `usage` 可直接记录：

- `prompt_tokens`
- `completion_tokens`
- `total_tokens`
- `prompt_cache_hit_tokens`
- `prompt_cache_miss_tokens`
- `completion_tokens_details.reasoning_tokens`

流式请求需设置 `stream_options: {"include_usage": true}`，完整 usage 会在 `[DONE]` 前的额外 chunk 返回。[Create Chat Completion schema](https://api-docs.deepseek.com/api/create-chat-completion)

DeepSeek 的磁盘上下文缓存默认开启，无需单独 API。它只匹配**完全相同的输入前缀单元**，是 best-effort，构建需数秒，未使用后通常在数小时至数天内清理；不能把它当成业务持久化缓存。响应中的 hit/miss 字段是实际命中的唯一可靠观测。[DeepSeek Context Caching](https://api-docs.deepseek.com/guides/kv_cache)

对本项目的 prompt 排列建议：

1. 最稳定的角色宪法、安全边界、输出 schema 放在最前；
2. 低频变化的人格、世界规则和压缩后的长期关系摘要随后；
3. 高频变化的当前世界切片、情绪、近期消息放在末尾；
4. 不把时间戳、随机 ID、排序不稳定 JSON 混进稳定前缀；
5. 按 `reply`、`appraisal`、`audit`、`repair` 分开记录 usage、首 token 延迟和 cache-hit ratio。

### 1.3 建议的推理分层

| 路径 | 推荐模式 | 原因 |
|---|---|---|
| 普通闲聊、事实很少、低冲突 | Flash + non-thinking | 低延迟；确定性状态机已经提供主要约束。 |
| 明确辱骂、明确道歉、明确世界事件 | 本地规则先行；Flash + non-thinking 生成 | 规则有高置信度，不应为显然事件固定支付 reasoning token。 |
| 讽刺、潜台词、多目标指代、引用与自嘲难分 | Flash + thinking/high，仅做结构化 appraisal proposal | 模型只提出证据、置信度和评估维度；状态变更仍由本地验证器提交。 |
| 高风险事实或安全判断 | 独立、按风险选择的审计路径 | 不应把“情绪更拟真”与事实安全混成同一判定。 |

## 2. 可借鉴的成熟计算情绪架构

### 2.1 Appraisal 是因果层，情绪标签是结果层

OCC 把情绪前因组织为对事件、行动者行为和对象的评价，是可计算离散情绪结构的经典来源。[OCC 原著出版信息](https://books.google.com/books/about/The_Cognitive_Structure_of_Emotions.html?id=dA3JEEAp6TsC) 但只用 OCC 标签会把“为什么发生”压缩得太早。Smith 与 Ellsworth 的实验得到 pleasantness、anticipated effort、certainty、attention、self/other responsibility/control、situational control 等评估维度，说明相同负面效价可以因责任、确定性和控制感不同而成为不同情绪。[Smith & Ellsworth 1985](https://doi.org/10.1037/0022-3514.48.4.813)

EMA 的关键启发不是再加一套 emotion enum，而是维护“agent 与环境关系的解释”，在感知或推理更新后自动重评估，再由 coping 改变计划、注意和行动。[EMA 原论文](https://www.ccs.neu.edu/~marsella/publications/pdf/MarsellaCSR09.pdf) FAtiMA Modular 进一步把 appraisal 与行为组件模块化，便于替换理论和做同场景比较；其后续 toolkit 是开放实现，但论文没有证明它就是人类情绪的完整模型。[FAtiMA Modular](https://scholar.tecnico.ulisboa.pt/records/0d25f191-6acb-4c4c-ba5a-e6c0ef14a16e)，[FAtiMA Toolkit](https://arxiv.org/abs/2103.03020)

**建议移植的 appraisal schema：**

- `goal_relevance` / `goal_congruence`
- `novelty` / `expectedness`
- `certainty`
- `agency` 与 `blame_or_credit`（user / self / npc / situation / unknown）
- `controllability` / `coping_potential`
- `norm_compatibility` / `boundary_compatibility`
- `relationship_relevance`（trust、attachment、reciprocity）
- `status_power_delta`（是否被命令、贬低、公开羞辱、剥夺选择）
- `evidence_spans`、`confidence`、`alternative_appraisal`

模型可在歧义输入上提出这些字段，但只能输出 proposal。枚举、数值范围、证据是否来自当前/历史消息、是否越权修改关系，都由确定性代码验证。

### 2.2 离散情绪 + 连续维度，而非二选一

Russell 的 circumplex 用 valence/pleasure 与 arousal 组织核心情感空间；它适合表达“低唤醒的不悦”和“高唤醒的愤怒”之间的连续差异，却不能独立给出责任归因或社会意义。[Russell 1980](https://doi.org/10.1037/h0077714) ALMA 在虚拟人中把短期 emotion、中期 mood、长期 personality 分层，并让情绪改变 PAD mood；这正适合避免每个事件都把角色瞬间切到一个新标签。[ALMA 原论文](https://citeseerx.ist.psu.edu/document?doi=679bfc64621dae3a2247be838d042643840961b3&repid=rep1&type=pdf)，[AAMAS 2005 官方议程](https://www.ifaamas.org/AAMAS/aamas05/technical_program.html)

**建议的数据表示：**

- 保留离散通道：anger、hurt、sadness、anxiety、resentment、warmth 等，用于可解释决策和测试；
- 增加连续 core affect：valence、arousal、dominance/control；
- 正负情绪不要做成一个互斥标量。研究表明正负 affect 的关系会随事件的个人相关性改变，支持“喜欢对方但仍受伤”等混合状态。[Dejonckheere et al. 2021](https://doi.org/10.1037/emo0000697)
- 将 appraisal → 离散 emotion impulse → core affect/mood 更新；不要从 PAD 坐标反推唯一情绪标签；
- expression 读取 emotion、mood、关系、目标和 display strategy，而不是直接逐项念出内部向量。

### 2.3 多时间尺度、惯性与迟滞

ALMA 的 emotion/mood/personality 三层是成熟的工程分解，但衰减率不能只由标签固定。情绪惯性研究把相邻时刻的自回归持续性作为重要动力学性质；更强惯性并不等于更“真人”，过高惯性与心理失调有关。[Kuppens et al. 2010](https://pmc.ncbi.nlm.nih.gov/articles/PMC2901421/) 复杂动力学指标也不是越多越好：大样本研究发现，在预测心理福祉时，多种复杂 affect dynamics 在均值和变异之外增加的信息有限。[Dejonckheere et al. 2019](https://doi.org/10.1038/s41562-019-0555-0)

**安全可移植机制：**

- impulse：事件即时冲击；
- episode：由同一原因维持的短期情绪，带 source 与未解决条件；
- mood：较慢的 valence/arousal/control 偏置；
- relationship residue：背叛、重复越界、可靠修复等事件沉积到 trust/guardedness，不等同于 mood；
- personality baseline：只影响阈值、表达和回归点，不决定每次反应；
- hysteresis：进入高度戒备与退出戒备使用不同阈值，防止一句道歉瞬间恢复；
- state-dependent decay：睡眠、安全环境、反刍、重复提醒、用户后续行为改变衰减；时间流逝只是一个因素；
- capped spillover：世界/NPC 情绪可降低耐心或改变措辞，但不能把无关怒气无条件归罪用户；必须保留来源并限制迁移幅度。

### 2.4 应对、情绪调节和表达分离

Gross 的过程模型区分在情绪生成较早阶段进行的 reappraisal 与较晚的 expression suppression；实验显示二者对体验、表达和生理反应的后果不同。[Gross 1998](https://pubmed.ncbi.nlm.nih.gov/9457784/) 人还会通过他人调节自己或对方的情绪，且效果可能依赖对方后续回应；Zaki 与 Williams 将其组织为 intrinsic/extrinsic、response-dependent/independent 两个维度。[Zaki & Williams 2013](https://doi.org/10.1037/a0033839)

因此本项目应明确分开：

1. `felt_affect`：内部发生了什么；
2. `regulation_strategy`：接纳、重评估、转移注意、压抑表达、寻求修复、设边界、暂时退出；
3. `displayed_affect`：当前关系和场景下愿意表达多少；
4. `coping_action`：回复、沉默、改变活动、找 NPC 倾诉、稍后再谈；
5. `reappraisal_trigger`：用户澄清、证据变化、时间推进、目标完成、记忆唤回。

这能产生比“向量高就说我生气”更自然的差异：角色可能仍受伤但克制措辞，或者表面平静却降低亲密主动性。它同时避免让语言模型凭文风反过来篡改内部状态。

## 3. 讽刺、潜台词、权力关系与情绪迁移

### 3.1 语用理解必须使用上下文与用户历史

讽刺不能可靠地从当前一句话识别。会话上下文模型优于只看当前 turn 的模型；作者历史有时有帮助，但“说话者意图的讽刺”与“旁观者感知的讽刺”并非同一标签。[Ghosh et al. 2018](https://aclanthology.org/J18-4009/)，[Oprea & Magdy 2019](https://aclanthology.org/P19-1275/) 这直接反对“看到正负词冲突就判冒犯”的做法。

建议引入一个**带不确定性的语用评估器**，输入仅包含必要上下文：

- 当前句、前若干相关 turn、被回复/引用对象；
- user 是否在自嘲、转述别人、复述角色的话；
- 同一表达在该用户历史中的通常意义；
- 当前共同知识、关系阶段、此前边界；
- 输出 literal meaning、likely implied attitude、target、confidence、supporting spans 与一个替代解释。

低置信度时角色应澄清或做弱反应，而不是直接累计长期关系伤害；明确威胁或性越界等高风险情况仍由硬规则优先。

### 3.2 权力与地位是 appraisal 输入，不是固定人设

权力的 approach/inhibition 理论指出，较高权力与趋近、奖励关注和去抑制相关；较低权力与威胁关注、约束和抑制相关。[Keltner, Gruenfeld & Anderson 2003](https://doi.org/10.1037/0033-295X.110.2.265) 这不是让角色机械服从“高地位用户”，而是说明相同命令在不同依赖、可退出性、公开/私密场景下会被不同评估。

本项目可维护场景化、双向关系变量：`dependency`、`choice_control`、`status_claim`、`status_legitimacy`、`audience_exposure`、`reciprocity`。它们影响 threat、dominance/control 和 coping 倾向，但不得覆盖安全边界或人格自主性。

### 3.3 情绪迁移必须有来源和上限

世界/NPC 事件应影响后续对话，这符合 appraisal 的环境关系观；但“NPC 惹她生气，所以她无故攻击用户”并不自动更拟真。更可信的是：

- 世界事件先改变 arousal、mood、attention 与 coping resources；
- 用户输入仍单独 appraisal；
- mood 对新事件强度施加有限 bias；
- 如果用户提供支持，可触发 response-dependent interpersonal regulation；
- 所有迁移保留 source chain，以便解释、回放和测试。

## 4. 长期记忆、反思与关系发展

Generative Agents 保存 observation memory，按相关性、时近性和重要性检索，并把经历综合成更高层 reflection；其消融实验显示 observation、planning、reflection 都对被评估的可信行为有贡献。[Park et al. 2023](https://arxiv.org/abs/2304.03442)，[ACM 论文页](https://doi.org/10.1145/3586183.3606763) CoALA 则把 language agent 组织为模块化记忆、内部/外部行动空间和决策过程；它是架构框架，不是情绪真实性的实证保证。[CoALA](https://openreview.net/pdf?id=1i6ZCvflQJ)

对本项目最稳妥的移植方式：

- `episodic event`：原始事件、参与者、来源、当时 appraisal、felt/displayed affect、coping 与结果；
- `relationship evidence`：守约、忽视、修复、边界尊重等可审计证据；
- `reflection`：从多次事件归纳的暂定信念，例如“他在我说停后通常会停”；必须带支持事件 ID、置信度和反例；
- `semantic fact`：用户明确事实，不与情绪印象使用同一衰减/覆盖规则；
- `retrieval`：当前目标、人物、因果来源、情绪相似性、重要性、时近性共同排序；
- `reconsolidation`：新证据更新 reflection，不改写原始 episode；
- 反思由模型生成 proposal，本地验证引用是否存在、是否过度概括、是否把一次事件升级为永久人格判断。

需要避免：每轮都做昂贵 reflection；把模型总结当事实；只按向量相似度取回创伤记忆；用一条负面经历永久污染用户画像。

## 5. 参数校准与长期纵向评测

### 5.1 为什么单元测试不足

情绪动力学在人与情境之间有明显差异；context-aware experience sampling 发现同一情绪标签在个体内和个体间都有显著变化。[Hoemann et al. 2020](https://doi.org/10.1038/s41598-020-69180-y) 真实关系形成也需要周级观察：一项 Replika 研究跟踪 25 名用户 12 周；2025 年的 AI companion 研究在纵向样本中观察到对新 chatbot 的感知到第 3 周才明显变化。[Skjuve et al. 2022](https://doi.org/10.1016/j.ijhcs.2022.102903)，[Hwang et al. 2025](https://arxiv.org/abs/2510.10079)

这些研究说明应做长期试验，但**不能直接提供本角色的最佳 decay 或 repair 数值**。参数必须用本项目自己的交互数据估计。

### 5.2 推荐评测设计

#### 离线因果回放

- 建立涵盖日常、亲密、模糊玩笑、自嘲、引用、讽刺、NPC 冲突、重复冒犯、具体道歉、行为修复的多轮轨迹；
- 同一语句只改变目标、关系阶段、前文和权力情境，检查 appraisal 是否合理变化；
- 做 metamorphic tests：换成无关世界事件不应改变 blame target；引用辱骂不应等同直接辱骂；一次具体道歉不应清零但应有可测缓和；
- 固定事件流重放必须得到相同状态，模型 proposal 另存以便比较模型版本。

#### 盲评与对照

- 至少比较当前版本、去掉 appraisal 的消融、去掉 mood/relationship residue 的消融，以及候选新版；
- 评审看完整多日轨迹而不是单句，评价因果一致、强度合适、修复节奏、人格一致、模板感和过敏/迟钝；
- 不以“猜真人/AI”作为唯一指标；它混入文风、事实能力和欺骗线索，不能单独验证情绪机制。

#### 3–12 周纵向试验

- 用户在随机抽样回合后轻量标注：角色反应是否合理、是否过强/过弱、是否记仇过久、是否恢复过快、是否与世界经历连贯；
- 每周收集关系质量、可预测/机械感、信任、边界舒适度、主动性和使用意愿；
- 记录但不要求用户披露敏感原文；保留退出、删除和数据最小化能力；
- 分用户估计 baseline、reactivity、decay/inertia、repair response，同时用分层模型限制小样本过拟合；新用户回退到群体先验；
- A/B 变更一次只动一类机制，避免同时改 prompt、模型、衰减和表达导致无法归因。

#### 最小指标集

| 维度 | 指标示例 |
|---|---|
| 触发准确性 | offence / repair / sarcasm / quote / self-target 的 precision、recall、校准误差 |
| 强度 | 人类标注强度与系统 impulse 的有序相关；过度反应率、低反应率 |
| 因果 | source/target/agency 错误率；无来源情绪声明率 |
| 动力学 | 峰值、半衰时间、inertia、恢复迟滞、重复事件累积；均需按角色/用户分层 |
| 混合情绪 | 正负并存场景的保持率；是否被模板化成单一标签 |
| 关系修复 | 道歉后即时缓和、观察期一致行为、再次越界回退是否符合标注 |
| 长期自然度 | 每周机械感、反复句式、情绪跳变、无故记仇、过快恢复、关系连贯性 |
| 成本/延迟 | 每路径 prompt/completion/reasoning tokens、cache hit、TTFT、总延迟、fallback 率 |

## 6. 建议的项目实施顺序

### P0：可观测性与 V4 Flash

1. 全部生成路径改为 `deepseek-v4-flash`；普通路径显式 `thinking.disabled`；
2. 持久化真实 usage、cache hit/miss、reasoning tokens、TTFT、失败与重试路径；
3. 固定 prompt 前缀并为 JSON 排序，测实际缓存命中，而非假定命中；
4. 为模型切换、thinking routing 和 usage 解析加契约测试。

### P1：结构化 appraisal 与不确定性

1. 在现有事件分类和 Affect 之间加入 appraisal record；
2. 明确 agency、target、certainty、control、norm/boundary、relationship、power；
3. 规则处理高置信度案例；Flash 仅对歧义案例给受限 proposal；
4. 低置信度不累计长期伤害，优先澄清/弱反应。

### P2：三层动力学与表达分离

1. emotion episode、core mood、relationship residue 分层；
2. state-dependent decay、hysteresis、rumination/reminder、安全活动与睡眠调节；
3. felt/regulation/displayed/coping 分离；
4. NPC/world spillover 有来源、上限和不归罪用户的硬约束。

### P3：证据化反思与关系模型

1. episodic memory 与 semantic fact 分仓；
2. reflection 必须引用 episode，支持反例和置信度；
3. 关系状态由证据积累，不由模型一句总结直接改写；
4. 低频/事件阈值触发反思，避免每轮额外模型调用。

### P4：长期校准

1. 先完成离线多轮、metamorphic 与消融评测；
2. 小规模 3 周试点检查机械感、过敏和恢复速度；
3. 扩展到 8–12 周，按用户拟合有限个参数；
4. 只有当纵向指标稳定改善，才把新参数升级为默认值。

## 7. 证据边界：可以移植与不能宣称的内容

### 有较强依据、可直接工程化

- appraisal 作为事件到情绪/应对的中间因果层；
- emotion、mood、personality/relationship 多时间尺度；
- 连续 core affect 与离散情绪并存；
- 上下文、说话者历史和 target 对讽刺/潜台词判断重要；
- 内部情绪、调节策略、表达与行动分离；
- observation → evidence-backed reflection → retrieval 的记忆流程；
- 周级纵向评测、消融和体验抽样，而不是只测单句。

### 有理论启发但必须实验验证

- 每个 appraisal 维度到现有 affect 向量的具体权重；
- dominance/PAD 对中文文本措辞的映射；
- 不同人格、关系阶段的衰减、迟滞和 spillover 系数；
- 用 V4 Flash thinking 做语用评估是否比小分类器更准且成本可接受；
- 反思频率、重要性阈值和情绪相似度检索权重。

### 证据不足，不应宣称

- 某个模型或架构已达到“完美人类情绪”或不可分辨真人；
- Generative Agents、CoALA、OCC、EMA、ALMA 任一单独构成业内最优伴侣方案；
- 论文里的实验参数可以原样成为本角色的最佳参数；
- 更强负面情绪、更久记仇或更多波动就一定更拟真；
- 一次短期满意度提升能证明数周或数月后仍自然、安全。

## 8. 最关键的设计原则

最终应坚持一个受控闭环：

`世界/用户事件 → 有证据且带不确定性的 appraisal → emotion impulse → mood/关系残留 → regulation/coping → 有社会情境约束的表达与行动 → 新结果 → reappraisal → 可审计记忆/反思`。

语言模型适合补足开放域语义、提出多个解释和生成自然表达；确定性内核负责状态所有权、因果来源、边界、数值约束、衰减、关系结算和回放。这样既能比纯规则细腻，也不会把长期人格和关系交给一次不可复现的模型输出。

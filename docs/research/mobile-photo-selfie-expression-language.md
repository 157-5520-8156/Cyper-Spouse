# 真实手机影像与自拍表演语言：社区实践、研究依据与分类矩阵草案

更新日期：2026-07-15
范围：国内 AI 生图社区公开资产与工作流、身份/姿态/表情控制工具、FACS 与在野表情研究、自拍研究、真实手机影像的工程抽象。
状态：研究输入已被吸收到 MediaPlan v5 的版本化面部表现、手机摄影真实性与镜头几何合同；本文仍保留调查过程与取舍依据，运行时枚举以版本化配置和代码合同为准。

## 结论先行

1. **国内玩家已经积累了成熟零件，但没有找到一套可直接搬进产品的完整自拍神态分类体系。** 公开资产覆盖手机抓拍风格、皮肤与景深滑块、笑容/梨涡等局部表情 LoRA、参考表情迁移和头部姿态控制；它们解决的是某个模型上的“怎样调出来”，而不是“角色为什么这样表演、镜头里正在发生什么、哪些组合合法、怎样避免近期重复”。因此无需从零摸索生成技巧，但仍需自行建立领域矩阵。
2. **最有复用价值的社区成果不是具体 prompt tag，而是可分离控制的思想。** 笑容、肤质、景深等差异 LoRA 以及 AdvancedLivePortrait 的眉、眼、瞳孔、嘴形、头部旋转控制都证明，身份、头姿、视线和局部面部动作应当分层；但 `smile`、`one eye closed`、`dimple` 之类标签本身不应成为上层业务分类。
3. **FACS 适合作为“面部可见动作的底层词汇与验收辅助”，不适合直接成为规划器的产品接口。** FACS 描述可观察肌肉动作，不等同于内心情绪；真实自拍还需要表演目的、镜头意识、目光对象、动作阶段、强度和不对称性。系统应先选择完整的 `FacialMicroPerformanceCandidate`，再由版本化词典把它编译成自然语言、可选 AU 提示或表情参考。
4. **“真实”至少要拆成摄影真实性与场景真实性。** 手机抓拍 LoRA 和社区 prompt 普遍用胶片、颗粒、皮肤纹理、杂物、逆光等词减少 AI 感，但这很容易变成另一种统一滤镜。更稳妥的做法是新增 `PhotographicAuthenticityProfile`：设备/镜头行为、曝光与白平衡、处理强度、构图规整度、环境熵、时刻痕迹和地域依据分别控制。火车是否像国内，不能由“手机感”解决，必须由世界快照提供地域/交通事实。
5. **自拍表情死板通常不是因为表情枚举太少，而是因为缺少“有对象、有过程的微表演”。** `皱鼻笑`、`傲娇脸`、`卖呆`不能只展开成嘴形；它们是眉眼、鼻颊、嘴、头姿、视线和时间拍点的兼容组合，并且服务于某个社交目的。应该由 `Interaction Bid → Address Strategy → Facial Display Strategy → Facial Micro-performance` 逐层收敛，而不是让 LLM 自由拼五官。
6. **私密媒体的张力主要来自清晰的收件人导向、接近感、选择性展示和未完成动作，不等于增加裸露。** 直接目光会改变观看者的社会/奖赏反应；但同一直接目光在亲昵、挑衅、关心、营业式人像中意义不同。因此 `invite_desire` 仍是上游动机，微表情和镜头距离只是实现手段。安全覆盖与世界事实继续是硬约束。
7. **推荐做法是“分类完整、组合宽松、候选完整、选择随机”。** 矩阵用于定义正交维度和非法边界，不把事件写死到唯一表情。图片机从合法组合生成有限个完整候选，以 opportunity ID 稳定加权抽样，再让一次受限 LLM 选择候选；验收记录实际表情与摄影特征，参与近期去重。

## 1. 来源层级与研究方法

本文按以下层级使用证据：

| 层级 | 来源 | 可支持的结论 |
|---|---|---|
| A | 原论文、大学/研究机构项目、作者官方项目 | 表情和自拍可观测维度、身份/姿态/表情解耦、工具能力边界 |
| B | 官方开源仓库及源码 | 实际可控制参数、运行时可实现性、模型权衡 |
| C | 原创社区模型/工作流发布页 | 国内玩家实际怎么解决 AI 感、身份、表情与自拍风格；不能当作科学普遍规律 |
| D | 本文工程推导 | 适配 Girl-Agent 的领域维度、矩阵与职责边界；需要后续样例验证 |

检索优先查看 LiblibAI/哩布和吐司的原创公开页，再追到其引用的开源项目；技术结论使用论文或作者仓库复核。平台搜索结果存在推荐偏差，付费群、网盘工作流和不可索引内容无法穷尽，因此“没有完整体系”准确含义是：**在本次可访问的公开原创资料中没有发现可审计、互斥且覆盖全面的产品级分类体系**，不等于证明任何私人工作流都不存在。

## 2. 国内玩家已经做到了什么

### 2.0 补充核验：这次实际采用哪些社区做法

本轮没有把论坛里的长 prompt 原封不动搬进产品，而是核验了其中可复用的工作流经验：

- 哩布的[笑容表情调节滑块](https://www.liblib.art/modelinfo/2cc4e77618bb43ccaa8337fd5033a3a4)将笑容作为可正负调节的局部变量；它支持“局部表现可以在身份、场景之外单独控制”，也说明单个滑块不应承担完整社交表演。
- [梨涡微表情控制 LoRA](https://www.liblib.art/modelinfo/c48a94002a6a413b8c423736fe7dceee)公开了闭眼、抿嘴、露齿、单眼等可组合动作，同时明确提醒融合会污染原人物。工程上这意味着：局部脸部动作属于渲染适配器词典，必须同身份参考、头姿与场景分离，且不能由它反向决定人物身份。
- B 站的[Change Face V2 工作流](https://www.bilibili.com/video/BV1m7vsecE64/)把换脸、近远景面部聚焦、表情增强、眼睛控制、局部蒙版放在可独立启用的模块中；这是“先有完整照片合同，再按风险选择局部修复”的直接实践依据。
- 社区对 [AdvancedLivePortrait](https://www.bilibili.com/video/BV1ZUqeYZExJ/)的使用是参数编辑或参考视频驱动，而不是只写 `cute` 之类词。它提示我们静态图片的“刚摇完头”“憋笑没忍住”要表达为一个**可见的冻结拍点**，未来可映射到表情迁移或局部编辑；不能假装一帧照片证明了完整动作过程。
- [AnyPose 的公开说明](https://www.liblib.art/modelinfo/997aa22f31cb4baab3abd1eaf290b2d5)把姿态、头向和视线作为可精确对齐项，也列出了复杂姿态、多人物、缺环境参考等失败情形。这支持将面部拍点与身体/镜头候选整体冻结，而不是让表情提示词偷偷改写镜头或姿势。

因此纳入图片机的不是“笑眼、嘟嘴、歪头”这种风格配方，也不是幼态化素材的审美，而是四条可维护规律：**身份与表情分离、眼神与嘴形可独立控制、细节失败时局部修复、完整表演先于执行器参数**。社区的具体 tag 只作为 renderer 的可替换词典和人工验收样本，绝不作为事件到表情的一对一规则。

### 2.1 已形成四类成熟零件

| 零件 | 公开例子 | 实际价值 | 不足 |
|---|---|---|---|
| 局部表情滑块 | LiblibAI 的[笑容表情调节滑块](https://www.liblib.art/modelinfo/2cc4e77618bb43ccaa8337fd5033a3a4)用差异提取控制表情强弱；负权重可使嘴角下垂 | 证明“表现强度”可独立于场景和身份 | 基本是一维轴；无社交目的、眼神过程和完整表演语义 |
| 表情标签/微表情 LoRA | [梨涡微表情控制](https://www.liblib.art/modelinfo/c48a94002a6a413b8c423736fe7dceee)列出 `one eye closed`、`open mouth`、`closed eyes`、`closed mouth`、`teeth` 等可组合标签 | 提供常见可见动作词汇；说明组合比单一 emotion tag 有效 | 仍是 tag 堆叠；作者也提示可能污染原人物，没有兼容矩阵和稳定身份保证 |
| 参数化表情迁移 | LiblibAI 的[表情编辑器工作流](https://www.liblib.art/modelinfo/176fa1292c84479da2d69d5e0c325209)支持数值调表情/头姿与参考图迁移；其底层 [AdvancedLivePortrait](https://github.com/PowerHouseMan/ComfyUI-AdvancedLivePortrait)可保存/加载表情数据 | 可将“表达候选”落到参考图或参数执行层；适合局部修复 | 面向编辑器参数，不理解“装可爱”“傲娇地等回应”等社交表演 |
| 摄影/真实感资产 | [手机抓拍 LoRA](https://www.liblib.art/modelinfo/a65506e05cb5424a884e7c29a83f8e60)、[不露脸日常自拍 LoRA](https://www.liblib.art/modelinfo/fbdaf774d0444998a8c873f61a2aef66)、[复古胶片生活感 LoRA](https://www.liblib.art/modelinfo/35cdd3205d9b4ffbb391ecbc99976886)；另有[真实皮肤纹理滑块](https://www.liblib.art/modelinfo/4b16f40d36d14bb7b46e930beb6fbd00)和[景深滑块](https://www.liblib.art/modelinfo/e0fac3326d2746ba8fb525b1600535a7) | 证明摄影质感、肤质、景深、身份可作为可替换执行资产 | “胶片/氛围感/小红书”常把真实等同于审美风格，可能继续产出过于精致的效果图 |

这些资产已经比“在 prompt 里多写几个形容词”成熟：它们在模型或节点层提供了独立控制旋钮。但从系统设计看，它们处于 **renderer adapter / asset catalog**，不是 `MediaPlan` 的领域分类。

### 2.2 国内公开工作流的共同形态

公开工作流通常把能力分成：身份保持、姿态/头向、表情编辑、局部面部修复、放大与真实感增强。例如一个公开的[ReActor + InstantID + FaceID 组合工作流](https://www.bilibili.com/video/BV1m7vsecE64/)明确提供面部聚焦、表情提示增强、眼睛控制和局部蒙版；[PuLID + OpenPose + 表情调整工作流](https://www.liblib.art/modelinfo/43d7e4494b5b412db7fd611515262f76)也把角色一致、姿态和表情放在不同组。

这说明玩家已经认识到三个事实：

- 身份参考过强会把参考图的头向、表情和构图一起复制；
- prompt 对细微眼神和嘴形的控制不稳定，需要表情迁移或局部编辑；
- 面部修复应发生在构图/姿态之后，而不是用同一套身份图决定整张照片。

但公开页大多没有：互斥分类、证据路径、社交期待、摄影来源、近期去重、回放稳定性和验收合同。因此结论是“已有成熟执行积木”，而不是“已有成熟图片机”。

两个更直接的社区样本也印证这一点：[“爱自拍的小姐姐” LoRA](https://www.liblib.art/modelinfo/32df7909cb8044e4b78bc64d3ad34df0)以丰富自拍动作、皮肤质感和背景氛围为卖点，说明玩家确实在训练自拍先验；[“御女风/纯欲” LoRA](https://www.liblib.art/modelinfo/5772ff69be0946f8b1c254b878c2f432)的公开描述则把第一人称视角、仰拍、睡眼、乱发、素颜、露肩、同伴、暗调和颗粒感揉在一条场景提示里。后者可作为效果参考，却也准确展示了为什么不能把社区长 prompt 直接升级为领域分类：拍摄几何、人物状态、关系、衣装、神态和处理风格彼此纠缠，无法单独验证或复用。

### 2.3 哪些只是 prompt tag，哪些值得成为领域维度

| 内容 | 归属 | 原因 |
|---|---|---|
| `smile`, `dimple`, `one eye closed`, `teeth` | renderer 词典/执行提示 | 是实现手段，不能说明为何这样表演 |
| “胶片感、氛围感、小红书感、治愈” | 风格资产或软偏好 | 语义重叠且依赖模型版本，不能作为唯一真实性定义 |
| smile/skin/depth LoRA 权重 | adapter 参数 | 与具体底模和资产版本绑定 |
| 眉眼、鼻颊、嘴、视线、头姿、强度、不对称性 | 可成为稳定的底层表现维度 | 可观察、可验收，也能映射到多种执行器 |
| 表演目的、镜头意识、时间拍点、收件人导向 | 上层领域维度 | 决定同一肌肉动作在照片中的社会意义 |
| 摄影处理、环境规整度、构图偶然性、地域依据 | 通用摄影真实性维度 | 跨前摄/后摄、人物/无人物和模型实现复用 |

## 3. 表情与自拍研究能提供的轻量结构

### 3.1 FACS：动作描述，不是读心术

[Paul Ekman Group 对 FACS 的官方说明](https://www.paulekman.com/facial-action-coding-system/)将其定义为解剖学基础上的可见面部动作系统，把动作拆成 Action Units。它适合回答“画面上眉、眼、鼻、嘴发生了什么”，不直接回答“她内心究竟是什么”。

对图片机最有用的不是把完整 FACS 手册塞进 prompt，而是采用它的三条原则：

1. 表情是多个可观察动作的组合，不是一个 `happy` 标签；
2. 动作有强度，且左右不一定对称；
3. 头姿、视线和面部动作需要分别记录。

FACS 官方手册很重，不必把项目变成专业编码系统。轻量实现只需要维护项目会用到的可见动作词典，并在调试/验收层可选记录 AU 提示。例如皱鼻动作可映射到 nose wrinkling，笑眼与嘴角上提是两个不同区域，但产品计划仍保存“皱鼻憋笑”这个完整表演候选，而不是暴露 AU 数字给规划 LLM。

严格意义上的 micro-expression 是具有 onset、apex、offset 的短暂时间事件；CASME II 等研究使用高速视频和逐帧 AU 标注，而一张静态自拍不能证明这一时序。因此项目文档中的“微表情”宜改称 `facial micro-performance` 或“细微可见面部动作”，只描述冻结瞬间，不推断隐藏情绪或欺骗。参见 [CASME II 原论文](https://journals.plos.org/plosone/article?id=10.1371/journal.pone.0086041)。

### 3.2 在野研究支持“三层表情表示”

[Aff-Wild2](https://arxiv.org/abs/1910.04855)及其[统一框架论文](https://arxiv.org/abs/2103.15792)同时使用连续 valence/arousal、离散基本表情和 Action Units；这说明单一标签不足以覆盖真实环境中的面部行为。项目可对应为：

- `affective_tone`：低维连续倾向或现有 tone，决定整体正负性与能量；
- `expression_family`：人类可读的社会表演家族；
- `micro_action_profile`：实际眉眼鼻嘴动作，用于编译与验收。

这三层不能互相替代。相同正效价可以是温柔、小得意、憋笑或故意卖呆；相同嘴角上扬也可以是礼貌配合、真被逗笑或挑衅式半笑。

### 3.3 真实与摆拍不是二元值

自拍本来就是自我呈现。Selfiecity 对五个城市 3,200 张自拍的分析记录了 smile 和 head tilt 等连续差异，并发现不同城市/人群分布不同；其意义不是复制具体数值，而是说明不存在唯一“自然自拍姿势”。参见 [Selfiecity 项目](https://manovich.net/index.php/art/selfiecity)及[项目章节](https://manovich.net/content/04-projects/087-selfiecity-exploring/selfiecity_chapter.pdf)。

一项自拍编码研究发现，eye contact、context、social distance、head tilt 等类别具有较好的直观可编码性，同时还记录特定表情、姿势和 touching hair 等无法被单一情绪标签覆盖的项目。[原论文](https://doi.org/10.3389/fpsyg.2017.00082)

因此系统不应以“无摆拍痕迹”为真实性目标，而应明确 `performance_authorship`：

- `unperformed_capture`：没为镜头改变行为；
- `responsive_candid`：发现镜头后发生自然反应；
- `selfie_micro_pose`：有轻度、自觉的小表演；
- `playfully_performed`：明确为了收件人装可爱/搞怪/傲娇；
- `polished_portrait_performance`：有意识地管理脸和身体；
- `private_recipient_performance`：为特定收件人组织的私密表达。

这比“真实 vs 摆拍”更接近用户要的朋友间照片：它允许明显 pose，但要求 pose 的作者关系和分享意图可读。

[On the Semantics of Selfies](https://www.frontiersin.org/journals/communication/articles/10.3389/fcomm.2023.1233100/full)明确指出自拍尚无统一、确立的命名体系，并把自拍主要目的概括为自我表达、记录和表演；对 1,001 张自拍的观看者自由联想又聚成 Aesthetics、Imagination、Trait、State、Theory of Mind 五类语义印象。论文还强调：自拍意义不能只从图像统计、脸部分类或配文单独得到，观看者对拍摄者动机的解释是关键。这与本项目分离 `Interaction Bid`、照片表达策略和可见表演的方向一致，也进一步说明“自拍类别”不能只按床、运动、咖啡、OOTD 等场景名划分。

### 3.4 真实笑容不能简化成一个眼纹规则

关于自发与摆拍笑容，研究确实长期比较 AU6（眼周）与 AU12（嘴角）等动作；但后续工作指出 Duchenne marker 也会受笑容强度影响，且可以被主动做出，不能把“眼睛弯了”当成真实性证明。[综述](https://pmc.ncbi.nlm.nih.gov/articles/PMC7844089/)，[受控动态笑容实验](https://pmc.ncbi.nlm.nih.gov/articles/PMC4053432/)，[重新审视 Duchenne smile](https://pmc.ncbi.nlm.nih.gov/articles/PMC7193529/)

工程含义：验收应检查动作组合与场景/表演目的是否一致，并容许非对称、过渡和抑制；不能硬编码“笑必须露齿/眯眼”或“私密必须启唇”。

一项使用专业演员照片的研究发现，演员表达同一情绪状态时呈现显著多样性，而情境说明对观看者判断的影响可以超过孤立面部形态；作者据此反对只研究脱离情境的刻板表情。[Nature Communications 原论文](https://www.nature.com/articles/s41467-021-25352-6) 这为“上下文先筛候选、不要 emotion → 固定脸”提供了直接依据。

较新的 [ContextFace](https://openaccess.thecvf.com/content/ICCV2025/papers/Kim_ContextFace_Generating_Facial_Expressions_from_Emotional_Contexts_ICCV_2025_paper.pdf)进一步尝试从 situation 或 quote 生成不同的 3D 面部表达系数，并展示同一 emotion 随情境产生不同表达。它不是可以直接接入本项目的成熟图片生成器，但证明“事件情境 → 分布式表情候选”比“情绪词 → 平均脸”更合理。

### 3.5 视线是社会关系变量，不只是瞳孔方向

直接目光会改变观看者对吸引力和社会互动的加工；经典研究发现，具有吸引力的脸在直视时更能激活与奖赏相关的反应。[Kampe et al., Nature](https://www.nature.com/articles/35098149.pdf) 其他研究也表明 direct gaze 会引发自主唤醒和正性面部反应。[Hietanen et al. 2020](https://doi.org/10.1111/psyp.13587)

这支持把 `gaze_target`、`gaze_sequence` 和 `recipient_substitution` 作为维度，但不支持“私密照一律直视”。回避后回看、看屏幕后抬眼、看证据再看收件人，都可以形成不同强度的对象感。

一组关于调情表情识别的实验找到一类较易被识别的女性调情构型：头转向一侧并略向下、轻微微笑、眼睛朝向隐含对象。[PubMed 原论文](https://pubmed.ncbi.nlm.nih.gov/32881585/) 这只能证明某些完整构型能够传递吸引意图，不能成为本项目的“标准性感脸”：研究同时报告个体在表达与识别上的差异，而且单一构型若固化，恰好会重新制造“偏头＋微笑”的同质化。系统应将它作为 `desire_withheld` 的一个低权重候选，而非硬规则。

## 4. 技术执行层已经能支持哪些维度

### 4.1 表情与头姿可以拆开控制

[LivePortrait 论文](https://arxiv.org/abs/2407.03168)把 appearance 作为源图，把 facial expression 和 head pose 作为运动来源，并提供 eye/lip retargeting；[官方仓库](https://github.com/KlingAIResearch/LivePortrait)给出实现。社区的 [AdvancedLivePortrait 源码](https://github.com/PowerHouseMan/ComfyUI-AdvancedLivePortrait/blob/main/nodes.py)实际暴露：

- `rotate_pitch / rotate_yaw / rotate_roll`
- `blink / eyebrow / wink`
- `pupil_x / pupil_y`
- `aaa / eee / woo / smile`
- 从样例只取 `OnlyExpression / OnlyRotation / OnlyMouth / OnlyEyes`

这不是完整的人类神态模型，却是很有价值的 adapter contract：将来可把已冻结的微表演候选映射成参考图迁移或局部修复，而不让上层计划绑定某个 ComfyUI 节点。

[FineFace](https://arxiv.org/abs/2407.20175)直接针对生成模型容易产生“平坦中性脸和缺乏真实性的笑容”提出 AU 驱动的局部、连续强度控制，目标正是组合 AU 生成常规 emotion 标签以外的细腻反应。它支持本文的 AU-inspired 内部词典；但 FineFace 控制的是脸部局部动作，仍不提供自拍的社交目的、镜头作者、时刻和世界事实。

### 4.2 身份强度与可编辑性存在真实权衡

[PuLID for FLUX 官方说明](https://github.com/ToTheBeginning/PuLID/blob/main/docs/pulid_for_flux.md)明确指出，越早注入身份通常身份相似度越高，但可编辑性会下降；[InstantID](https://arxiv.org/abs/2401.07519)则使用人脸身份与 landmark 条件实现单图身份保持。[IP-Adapter 官方仓库](https://github.com/tencent-ailab/IP-Adapter)也将 image prompt 与文本条件组合。

因此“总是加大身份参考权重”会加剧参考图表情、头向和发型复制。项目应冻结 `identity_reference_role` 和适配器强度档位，并把“身份一致”与“表演/几何一致”分别验收。

### 4.3 姿态结构控制不是神态控制

[ControlNet](https://arxiv.org/abs/2302.05543)支持 pose、depth、edge 等空间条件；[OpenPose](https://arxiv.org/abs/1812.08008)提供身体、手、足和面部关键点。它们适合控制身体结构和大体头向，但不能单独表达“傲娇地等你回应”或精细嘴眼关系。执行顺序应是：先冻结完整照片表达，再根据风险决定是否用 pose/reference/expression editor，而不是让有无某个 ControlNet 反过来定义分类。

## 5. 建议的通用分类矩阵草案

以下均为 D 级工程推导，应在真正修改 MediaPlan 前用一组对照样例验证。原则是：每个维度只有一个主值；完整表演由兼容候选承载；LLM 不自由拼五官。

### 5.1 `PhotographicAuthenticityProfile`

适用于 `life_share` 和 `character_media`，与 polish 正交。

这里有比社区“胶片感”更稳的测量依据：[SPAQ（CVPR 2020）](https://openaccess.thecvf.com/content_CVPR_2020/html/Fang_Perceptual_Quality_Assessment_of_Smartphone_Photography_CVPR_2020_paper.html)收集 11,125 张、66 款手机照片，除整体质量外还标注 brightness、colorfulness、contrast、noisiness、sharpness；[LIVE In the Wild](https://www.colorado.edu/lab/live/live-wild-image-quality-challenge-database)则专门保留真实捕获、处理和存储共同形成的混合失真。工程上应把这些属性作为连续观察量或档位，而不是每张图强行添加一个“噪点缺陷”。

自拍几何也有物理依据：近距离会产生可测的脸部透视变化，[短距离自拍失真研究](https://pmc.ncbi.nlm.nih.gov/articles/PMC5876805/)比较了约 30 cm 与更远距离的人脸比例；[广角手机人像研究](https://people.csail.mit.edu/yichangshih/wide_angle_portrait/)指出广视场边缘的人脸更易拉伸。因此现有 CameraGeometry 可补充或在验收中推导 `camera_face_distance_band`、`field_of_view_band` 与 `face_radial_position`，避免把所有前摄都渲染成同一种伸臂比例。

| 维度 | 建议值域 | 说明 |
|---|---|---|
| `capture_authorship` | `self_front`, `self_rear`, `mirror`, `fixed`, `helper`, `companion`, `external`, `artifact` | 复用 capture mode，但作为真实性推理的入口 |
| `device_rendering` | `front_wide`, `rear_standard`, `rear_ultrawide`, `tele_crop`, `unknown_phone`, `artifact_inherited` | 描述镜头/ISP倾向，不写死品牌 |
| `camera_face_distance` | `very_close`, `arm_length`, `supported_near`, `mirror_distance`, `external_distance`, `not_applicable` | 与镜头等效焦距分开；决定自拍透视与接近感 |
| `face_radial_position` | `central`, `inner_off_axis`, `edge`, `not_applicable` | 约束广角边缘拉伸与群自拍结构 |
| `exposure_behavior` | `stable`, `highlight_protected`, `shadow_lifted`, `backlit_compromise`, `mixed_light_compromise`, `low_light_stack`, `flash_falloff` | 允许手机计算摄影的现实妥协 |
| `color_behavior` | `neutral_phone`, `warm_cast`, `cool_cast`, `mixed_white_balance`, `moderately_vivid`, `muted`, `artifact_inherited` | 避免所有生活图统一高饱和暖调 |
| `processing_level` | `light`, `typical_phone`, `social_edit`, `strong_filter`, `artifact_inherited` | 与 `raw/casual/curated`相关但不相同 |
| `scene_orderliness` | `lived_in`, `ordinary`, `lightly_arranged`, `display_ready`, `commercial` | `commercial` 只在事件事实允许时出现；普通生活图对其强惩罚 |
| `capture_imperfection` | `clean`, `off_center`, `partial_crop`, `minor_motion`, `focus_transition`, `reflection_layer`, `foreground_interrupt`, `lens_smudge_or_flare` | 每张最多选择少量可信缺陷，不能堆成“伪劣质滤镜” |
| `environment_entropy` | `sparse`, `normal`, `busy`, `transient` | 控制杂物、路人、使用痕迹与临时状态 |
| `regional_grounding` | `explicit`, `weak`, `none`, `artifact_inherited` | `explicit` 必须引用世界快照；不得靠伪造可读文字证明地点 |
| `aesthetic_intent` | `documentary`, `pleasant_share`, `atmospheric`, `editorial`, `commercial` | 明确“好看”与“像真实手机拍”并不冲突，但商业感要有来源 |

关键兼容关系：

- `raw` 不必故意低画质；它表示没有为发布做充分整理。
- `curated` 可以是可信手机照片，但不应自动拥有对称摆物、完美补光和商品级材质。
- `life_share + ordinary event` 若抽到 `commercial`，通常拒绝或强惩罚。
- 地域事实缺失时选择 `regional_grounding=none`，不要生成带明确外国/国内制度特征的交通工具。
- 验收重点是多项特征共同可信，不是要求每张图都有噪点、模糊或歪斜。
- `brightness / colorfulness / contrast / noisiness / sharpness`宜作为 inspector 的连续观察或粗档位；规划器只冻结摄影意图和容许范围，避免对 ISP 结果做伪精确承诺。

### 5.2 `FacialDisplayStrategy`

这是“为什么这样管理脸”的语义层，位于 Interaction Bid/Address Strategy 之后。

| 家族 | 表演目标 | 常见但非必需的微表演 |
|---|---|---|
| `present_and_available` | 让收件人感到她此刻在场 | 放松脸、轻量镜头回应、刚抬眼 |
| `warm_connection` | 传递友好、亲昵或安抚 | 眼周参与的小笑、柔和视线、轻微前倾 |
| `amusement_leaking` | 笑意压不住或刚被逗到 | 压嘴角、皱鼻、眯眼、偏开后回看 |
| `deliberate_cuteness` | 明确装可爱、卖呆 | 小嘟嘴、鼓颊、睁眼停顿、轻微皱鼻；避免幼态化固定模板 |
| `mock_defiance` | 傲娇、假装不服或轻挑衅 | 单侧眉、嘴角轻压/半笑、侧看后回镜头 |
| `comic_self_exposure` | 主动展示出糗或反差 | 死鱼眼、夸张但可信的无语、憋笑失败 |
| `proud_display` | 希望成果/外观被注意 | 稳定视线、小得意、克制笑或无笑 |
| `consultative_check` | 等真实意见而非等夸 | 看证据/屏幕后看镜头，眉部轻问询 |
| `frustrated_complaint` | 吐槽并寻求认同 | 轻皱眉、抿嘴、疲惫眯眼、说话中拍下 |
| `embarrassed_repair` | 承认尴尬并期待接梗/安慰 | 回避视线、压笑、面颊紧张、再偷看 |
| `tired_access` | 允许看到非营业状态 | 低能量眼睑、放松嘴、动作后的呼吸感 |
| `vulnerable_disclosure` | 让对方看见脆弱但不表演痛苦 | 保持克制、眼神停留或回避、低强度动作 |
| `tender_private` | 私下亲昵和信任 | 柔和注视、靠近、未完全整理的自然停顿 |
| `desire_direct` | 明确让收件人感到被吸引/被邀请 | 稳定对象性目光、轻微启唇或半笑、等待回应 |
| `desire_withheld` | 有意识地不说满 | 先回避后回看、压住笑、动作停在中间 |
| `neutral_evidence` | 脸只是事实载体，不抢主证据 | 视线跟随证据，低表演强度 |

这些是表演家族，不是情绪真值；同一角色在心情不错时也可故意做 `mock_defiance`，前提是 bid、关系和场景允许。

### 5.3 `FacialMicroPerformance`

每个完整候选在以下维度各取一个值，由内部兼容规则组合：

| 维度 | 建议值域 |
|---|---|
| `brow_action` | `settled`, `bilateral_lift`, `single_lift`, `slight_knit`, `lowered_relaxed`, `brief_question` |
| `eye_aperture` | `natural`, `smile_narrowed`, `wide_playful`, `relaxed_heavy`, `brief_squeeze`, `single_wink`, `not_visible` |
| `gaze_target` | `lens_recipient`, `screen_preview`, `evidence`, `companion`, `environment`, `away`, `not_visible` |
| `gaze_sequence` | `held`, `screen_then_lens`, `evidence_then_lens`, `away_then_back`, `lens_then_away`, `companion_then_camera`, `no_face` |
| `nose_cheek_action` | `settled`, `cheek_raise`, `nose_scrunch`, `cheek_puff`, `asymmetric_cheek`, `not_visible` |
| `mouth_action` | `relaxed_closed`, `relaxed_parted`, `small_smile`, `asymmetric_half_smile`, `suppressed_smile`, `open_laugh`, `subtle_pout`, `pressed`, `mid_speech`, `breath_recovery`, `not_visible` |
| `facial_asymmetry` | `balanced`, `subtle_left`, `subtle_right`, `momentary_irregular` |
| `display_intensity` | `trace`, `low`, `medium`, `high` |
| `performance_authorship` | `unperformed_capture`, `responsive_candid`, `selfie_micro_pose`, `playfully_performed`, `polished_portrait_performance`, `private_recipient_performance` |
| `temporal_phase` | `preparation`, `onset`, `held_beat`, `leaking`, `apex`, `release`, `after_reaction` |
| `facial_energy` | `low`, `contained`, `lively`, `breathless`, `held`, `recovering` |

重要约束：

- LLM 只选完整候选 ID，不逐字段生成。
- `nose_scrunch` 不等于“可爱”；它可用于真笑、嫌弃、搞怪或反应性尴尬，意义来自 Display Strategy。
- `subtle_pout` 不自动等于私密；它也可用于装可爱、假生气或征求回应。
- `desire_direct` 不强制 `relaxed_parted`；稳定直视、克制无笑同样可能更有张力。
- 强度高不等于更好。真实手机自拍应大量分布在 trace/low/medium，少量 high 形成对比。
- 同一身份的自然不对称方向可以作为角色偏好，但近期去重必须防止固定成“永远右偏头＋同侧半笑”。

### 5.4 宽兼容矩阵

| Interaction Bid 组 | 优先 Display Strategy | 常见 performance authorship | 硬限制 |
|---|---|---|---|
| 状态/协调 | present、neutral evidence、consultative | unperformed、responsive、micro-pose | 不允许凭空高强度私密表演 |
| 发现/展示/意见 | proud、consultative、deliberate cuteness | micro-pose、playful、polished | 主证据必须仍可读 |
| 玩笑/吐槽 | amusement、comic self-exposure、mock defiance、embarrassed repair | responsive、playful | 不把严重身体/安全事件当搞怪素材 |
| 关心/安慰 | tired access、vulnerable、warm connection | unperformed、micro-pose、private | 不用夸张痛苦替代世界事实 |
| 亲近 | warm、tender private、amusement | micro-pose、private | 受 privacy/relationship ceiling 约束 |
| 吸引 | desire direct、desire withheld、mock defiance、deliberate cuteness、tender private | private recipient、polished、playful | 必须 `intimate_signal` 且不越过 expression/coverage 上限 |
| 回忆 | warm、amusement、tender、neutral evidence | artifact inherited 或当下 reaction | 旧图表情不得被重新生成解释 |

矩阵只给亲和度；除隐私、事实、拍摄物理和明显语义反转外，不应把某个 bid 锁死到一个表情家族。

## 6. 渲染与验收建议

### 6.1 Prompt 不写 AU 数字堆

推荐编译顺序：

```text
社交表演目标
→ 镜头意识与目光对象
→ 完整的脸部微动作（眉眼鼻颊嘴一起描述）
→ 动作所处拍点与强度
→ 头姿/身体动作/镜头几何
→ 摄影真实性 profile
→ 身份参考职责与安全边界
```

自然语言示例应是“她刚想装作无所谓，单侧眉毛轻抬，嘴角压着一点笑，先看向旁边再偷看镜头”，而不是 `AU9 + AU12 + asymmetrical smile + cute + tsundere`。

### 6.2 参考表情资产应与身份资产分离

建议资产目录预留：

- `identity_anchor`：只定义脸型与身份；
- `angle_anchor`：只补头向/景别；
- `expression_driver`：只提供微表演动作，可为不同身份的参考；
- `wardrobe_reference`：只证明衣装；
- `photographic_style_adapter`：只提供设备/处理倾向。

LivePortrait/AdvancedLivePortrait 证明表情可从样例抽取；PuLID 的身份—可编辑性权衡说明这些职责不能继续混在一张“bold reference”里。

### 6.3 `MediaInspection` 可增加的观察字段

- `observed_display_strategy`
- `observed_brow_eye_nose_mouth_actions`
- `observed_gaze_target_and_sequence`
- `expression_coherence_ok`
- `recipient_orientation_legible`
- `generic_smile_fallback`
- `reference_expression_copy_detected`
- `photographic_authenticity_profile_matches`
- `commercial_render_dilution`
- `regional_grounding_matches`
- `observed_processing_and_scene_orderliness`

AU 检测可作为离线诊断或局部辅助，不宜作为自动投递的唯一硬门。OpenFace 能输出 landmarks、head pose、AUs 和 gaze，说明这些字段在技术上可观测；参见 [OpenFace 官方仓库](https://github.com/TadasBaltrusaitis/OpenFace)与[论文](https://doi.org/10.1109/WACV.2016.7477553)。但跨身份、遮挡、侧脸和生成图上的误差仍需用人工样例校准。

## 7. 受控高随机与可维护性

建议候选生成顺序：

1. 世界事实与 MediaPlan 既有分类提供事件、人物状态、关系、隐私、拍摄来源和分享目的；
2. `PhotographicAuthenticityResolver` 生成合法摄影 profile；
3. `FacialDisplayStrategyCatalog` 根据 bid/address/tone 给出宽候选；
4. `FacialMicroPerformanceCatalog` 生成内部兼容的完整眉眼鼻嘴/视线/拍点组合；
5. 与 CameraGeometry、Subject Presentation、Embodied Presentation 合成完整候选；
6. 按 opportunity ID 稳定分层抽样，最多给 LLM 有代表性的少量候选；
7. 冻结后重试只修复，不重选表演；验收结果进入感知去重。

去重指纹应补充：

```text
display_strategy
performance_authorship
gaze_sequence
nose_cheek_action
mouth_action
facial_asymmetry
temporal_phase
device_rendering
exposure_behavior
scene_orderliness
processing_level
```

近期强惩罚应针对“感知组合”，不是单个值。连续两张都 `small_smile` 未必相似；连续两张“右三分之四、右偏头、soft lens、small smile、人物占比 dominant、暖色窗光”才是真正同质化。

配置应版本化、声明适用模型/adapter，并允许新增候选而不修改 MediaPlan schema。领域对象保存语义 ID，具体 prompt、LoRA 名称、节点参数和参考资产映射留在 renderer catalog。

## 8. 不建议采用的路线

- 不把完整 FACS 变成规划 LLM 的输出 schema；成本高、容易伪精确，而且 FACS 不解释社交目的。
- 不收集一长串“可爱/性感/傲娇”提示词后随机拼接；五官动作会冲突，且不同模型对 tag 的理解漂移。
- 不把“胶片、颗粒、低清晰度”当作所有真实图的默认；这会把 AI 商业效果图换成统一伪胶片图。
- 不让身份参考同时承担身份、发型、表情、姿势和私密强度；它会继续复制右偏头、微笑和固定构图。
- 不从脸部动作反推世界情绪事实；图片机只计划/观察可见表演，角色真实心境仍来自世界机。
- 不以增加裸露作为私密张力的主旋钮；它既不能解决对象感，也会压缩表达多样性。

## 9. 建议的下一步验证

在改生产代码前，做一组小型“表达对照板”，不需要 World v2 接通：

1. 固定同一身份、衣装、场景与镜头，分别生成 `warm_connection / deliberate_cuteness / mock_defiance / amusement_leaking / desire_direct / desire_withheld`；验证家族肉眼可分。
2. 固定 `desire_direct`，只改变 gaze sequence、mouth action、temporal phase 和镜头距离；验证张力不依赖更多裸露。
3. 固定咖啡店/书桌/火车事件，横向比较 `commercial / atmospheric / pleasant_share / documentary`，并改变 scene orderliness、color/exposure behavior；找出“效果图感”主要来自哪组变量。
4. 同一事件分别生成中国大陆明确地域、弱地域和无地域版本；验证图片机不会用伪文字或刻板符号强行证明地点。
5. 每个候选用规划合同生成，而不是人工润色最终 prompt；MediaInspection 记录实际微表演与摄影 profile，检查合同可验收性。

只有当这组对照能稳定区分时，才值得把草案提升为下一版 MediaPlan/renderer catalog。否则应先修执行资产或验收器，而不是继续扩大枚举。

## 10. 回答最初的四个问题

### 国内玩家是否已有成熟、可复用的自拍/表情分类体系？

**没有发现完整产品级体系；已有相当成熟的可复用执行组件。** 最接近结构化体系的是 AdvancedLivePortrait 的参数化面部编辑以及社区把身份、姿态、表情、局部修复拆组的 ComfyUI 工作流。LiblibAI 上的微表情 LoRA 和 tag 是词汇与资产，不是覆盖充分、互斥、带社交语义的分类。

### 哪些经验只是 tag 堆砌，哪些可以成为领域维度？

具体触发词、LoRA 权重、胶片滤镜属于 adapter；眉眼鼻嘴、视线、强度、不对称、拍点、表演作者关系、摄影处理和环境规整度可以成为领域维度。最关键新增不是更多表情名称，而是 `FacialDisplayStrategy + FacialMicroPerformance + PhotographicAuthenticityProfile` 三层。

### FACS/表情生成/自拍真实性有什么轻量成果？

FACS 提供可观察动作分解；Aff-Wild2 提供连续 affect、表情家族与 AU 三层并存的依据；Selfiecity 与自拍编码研究支持头姿、目光、社会距离、上下文等可观察维度；LivePortrait/AdvancedLivePortrait 证明头姿、视线/眼、嘴和参考表达可分离执行。项目无需引入完整专业手册，只需采用分层思想与小型版本化动作词典。

### 能否形成可维护、全面、可扩展、高随机矩阵？

**可以。** 但矩阵必须定义“正交维度和边界”，而不是“场景到表情的固定表”。通过完整候选、稳定随机、软亲和度、执行映射和感知去重，既能扩大组合空间，也能防止 LLM 生成冲突五官或回退到统一微笑。

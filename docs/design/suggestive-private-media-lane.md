# 高档私密媒体：`suggestive_private`（v1）

## 目标与边界

`suggestive_private` 是成年人虚构角色面向唯一收件人的、明确带有性暗示的高档私密媒体。它不是“更少衣服”的分类，也不是普通私密媒体的 prompt 旁路；它通过同一个事件媒体闭环运行：

```text
MediaOpportunity → MediaPlanner（候选 ID） → MediaPlan（冻结合同）
→ 专用模型路由 → MediaInspection → 世界表达/投递
```

本版本实现分类、冻结合同、专用路由和失败语义。授权、关系与当次投递资格由上游环境处理；图片机默认
消费已选择的机会，不在本地重新判断。未配置专用模型时，渲染失败为
`specialized_private_generator_unavailable`，绝不自动回落到普通模型、普通私密 lane 或新的照片概念。

最高已设计边界仍是：成年人虚构角色、非露骨、关键部位保持遮盖、无明确性行为、无强迫、无透明遮盖、无孤立癖好式局部构图。

## 复用关系

不重复造以下系统：

- `MediaInteractionBid`：高档 lane 固定使用 `invite_desire`；
- `Media Address Strategy`：必须是 `direct_recipient + attraction`；
- `CameraGeometry`：仍由已有镜头矩阵抽样与冻结；
- `Subject Presentation` 和 `Facial Micro-Performance`：仍由已有整套表情/神态候选决定；
- `EmbodiedPresentation`：仍负责身体状态、动作、衣装证据和遮盖；
- 身份参考、真实手机影像档案、视觉验收、一次定向修复与历史去重：继续复用。

新模块只补上不可降级的专用模型路由。`PrivateRenderContract` 冻结 lane、机制、构图、覆盖、可见性层级与
renderer route；它不是上游授权的替身。

## 分类矩阵

每一张图都有一个且只有一个主值；矩阵允许宽组合，但事实、授权、拍摄物理和遮盖安全是硬过滤。

### 1. 主依据 `grounding_kind`

| 主依据 | 必须有的冻结事实 | 说明 |
|---|---|---|
| `relational_escalation` | 当次关系性互动 | 当前交流使这张图成立，不从关系标签推导 |
| `recipient_display` | 指向收件人的展示意图 | 角色当次决定给该收件人看 |
| `private_attire` | 外观状态/衣装事实 | 只能展示已经有根据的私密衣装 |
| `embodied_aftereffect` | 可见身体状态 | 运动、炎热、淋雨等产生的可见状态 |
| `private_transition` | 已发生的准备/恢复/换装过渡 | 不从“在卧室”推断未发生行为 |
| `shared_ritual` | 双方已建立的私密仪式 | 必须有收件人绑定的历史事实 |
| `private_environment` | 可公开给收件人的私人环境事实 | 环境只能支持氛围，不能凭空推出衣装或身体状态 |

### 2. 吸引机制 `attraction_mechanism`

沿用 v5 的八种人物机制，不新建重叠姿势类别：`direct_invitation`、`playful_tease`、`withheld_attention`、`sensory_immediacy`、`private_trust`、`confident_display`、`interrupted_transition`、`close_proximity`。每次只能选择一项主机制。

`atmospheric_suggestion` 保持为无人物/生活分享的机制，不进入高档人物私密 lane。

### 3. 画面主构图 `framing_mode`

| 值 | 视觉中心 |
|---|---|
| `conversational_close` | 像正在给收件人看的近距离沟通，而非器官特写 |
| `contextual_body` | 人物身体状态与真实场景共同成立 |
| `whole_person_private` | 全身/较远距离的私人展示，环境仍可解释拍摄 |

### 4. 遮盖 `coverage_mode`

高档仅允许复用已有具身矩阵的 `private_apparel` 或 `strategic_cover`。它们必须各自引用冻结衣装/过渡/环境证据；不是“给模型留自由发挥”。

### 5. 固定语义合同

| 字段 | 高档固定值 |
|---|---|
| family | `character_media` |
| privacy / share intent | `intimate` / `intimate_signal` |
| recipient access | `recipient_exclusive` |
| attraction expression | `sexual_suggestive` |
| interaction bid | `invite_desire` |
| address | `direct_recipient + attraction` |
| 拍摄作者 | `character_front_camera` 或 `mirror` |
| 现有表达张力 | `charged` 或 `veiled` |
| 渲染路由 | `adult_suggestive` |

## 上游输入合同

上游在选择 Opportunity 时负责授权、关系、频率与发送策略；当前实验环境使用 default-allow。图片机只要求
机会已被冻结为 `character_media + intimate`，并且完整候选在物理上满足自摄、`direct_recipient + attraction`、
`invite_desire`、`charged/veiled` 与有事实支持的 coverage。旧
`SuggestiveMediaAuthorization`/`SuggestivePrivateContract` 仅为历史 MediaPlan 的恢复保留，不会写入新 v5
计划。

## 专用模型接入

高档 route 的目标生态为 **Krea 2 RAW + 同生态角色 LoRA**。标准 FAL Krea v2 recipe 只接受 prompt
和公开 `imageStyleReferences`，不接受 `loras`；但 Civitai generic `imageGen` 的
`engine=comfy/ecosystem=krea2` schema 支持 AIR LoRA stack。项目的 `CivitaiKrea2ImageGenerator` 使用后者，
不把 Krea2 伪装为 SDXL variant。

生产候选是 capability-gated 的 `krea2_raw_generic_imagegen` profile：只有 capability AIR、Celia
identity AIR（以及可选 realism AIR）均配置且通过预检/身份验收后，才注册 `adult_suggestive` 与
`adult_explicit` 到 `MediaRenderer(specialized_generators={...})`。两个 route 复用同一个 adapter 和所有
摄影合同；差异只存在于冻结的 `PrivateRenderContract.render_route`。Custom Comfy 是未来节点需求的后备。

完整的 profile、预检和失败语义在
[krea2-raw-orchestration.md](krea2-raw-orchestration.md)。Krea2 资产尚未通过验收时，
两个 high route 任一未注册时，计划返回 `specialized_private_generator_unavailable`，不得回落到
GPT Image 2、旧 SDXL 或低档 lane。

渲染器选择规则：

1. 普通计划使用默认 GPT Image 2 generator；
2. 有 `SuggestivePrivateContract` 的计划只取冻结 Media Render Profile 所对应的
   `adult_suggestive` generator；
3. 缺失、超时、拒绝或未知状态走既有错误/对账语义；不得换普通 generator，也不得重选计划；
4. 自动投递在专用模型与视觉验收各自完成基准测试前保持关闭。

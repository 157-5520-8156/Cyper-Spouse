# 事件驱动图片机

## 1. 模块边界

目标链路：

```text
已提交世界事件
  → 世界机候选投影与机会选择
  → MediaOpportunity（冻结）
  → MediaPlanner.plan（一次 LLM）
  → MediaPlan / NotRenderable（External Result）
  → MediaRenderer.render（生成或复用、验收、最多一次修复）
  → MediaInspection（External Result）
  → 世界表达层配文
  → 投递
```

图片机的外部 seam 在 `companion_daemon.event_media`：

- `MediaPlanner.plan(opportunity, recent_media)`：只返回 `PlannedMedia` 或 `NotRenderable`；不写数据库、不生成图、不决定发送。
- `MediaRenderer.render(plan)`：只消费冻结计划，返回 `RenderedMedia` 或 `MediaRenderFailure`；不重新读取世界、不重新分类、不写配文。
- `OpenAIMediaInspector`：一次视觉调用同时完成交付验收和实际画面描述。
- `LegacyMediaShotAdapter`：把可恢复的 `MediaShotPlan v1-v3` 映射到新渲染 seam；不改变旧 payload。

当前模块没有接入世界写模型，因此默认线上行为不变。`MediaPlanner` 默认关闭；仅当 `COMPANION_EVENT_MEDIA_ENABLED=1`（或构造时显式 `enabled=True`）才接受新机会。`COMPANION_EVENT_MEDIA_V5_ENABLED=1` 才启用 v5；关闭时继续生成 v4。世界机接入时应先仅在预览模式写入新 Action/External Result，再开放自动投递。

## 2. 通用事件快照契约

面板与图片机必须读取完全相同的 `event_snapshot`。至少包含：

```json
{
  "schema_version": "world-event-snapshot-v1",
  "event": {
    "event_id": "event:...",
    "type": "...",
    "status": "committed",
    "logical_at": "...",
    "summary": "...",
    "outcome": "..."
  },
  "source": {
    "channel": "direct_experience|message|external_feed|...",
    "person": "character|person-id"
  },
  "location": {},
  "activity": {},
  "participants": [],
  "objects": [],
  "environment": {},
  "character": {
    "emotion": "...",
    "energy": "...",
    "appearance": "...",
    "appearance_state": {
      "schema_version": "appearance-state-v1",
      "valid_at": "...",
      "source": "world_projection",
      "hair_arrangement": "low_ponytail",
      "outfit_role": "campus_casual",
      "grooming": "natural",
      "accessories": ["teal_hair_clip"]
    },
    "body_health": {}
  },
  "existing_media": [],
  "visual_requirements": {
    "requires_readable_text": false
  }
}
```

快照只包含已经提交且允许展示的事实。未来计划、隐藏心理、模型推断和专为生图撰写的 prompt 不得进入快照。规划器只允许使用存在的 JSON Pointer；计划同时冻结解析后的证据值，因此恢复和重试不需要重新打开世界投影。

`character.appearance_state` 是世界机后续可选扩展，不是一般日常图的硬前提。它只描述事件时刻已经成立的可见连续事实，例如发型整理方式、衣装角色、妆容整理程度和饰品。图片机有该字段时逐字冻结为 `world_fact`；没有时只为本张照片生成 `media_local` 外观，不写回世界，也不能让下一事件把它当作已发生事实。自由文本 `appearance` 可继续作为事件证据，但不承担跨照片的结构化连续性。

但如果事件或机会希望锁定**具体衣装、材质、颜色或搭配**（例如“黑色运动背心与深灰短裤”“酒红色不透明吊带裙”），世界机必须提供 `appearance_state`，至少包含 `hair_arrangement`、`outfit_role`、`grooming`，并在 `description` 或具名字段中给出该衣装事实。没有结构化状态时，图片机只会把 `dance/pilates/workout/gym/running` 等活动归为 `athletic`、游泳归为 `swimwear`；它不会把自由文本强行解析成可跨重试保证的衣装事实。这样既避免白色背心/错误裙装漂移，也避免图片机把未经世界确认的衣服写回角色连续性。

## 3. 分类体系

每份计划在每个维度只有一个主值：

| 维度 | 值 |
|---|---|
| family | `life_share`, `character_media`（世界机决定） |
| content_domain | `place_environment`, `food_drink`, `object_possession`, `activity_process`, `outcome_progress`, `appearance_style`, `body_health`, `social_interaction`, `nature_animal`, `information_screen`, `travel_transit`, `other_grounded` |
| visual_form | `wide_scene`, `contextual_still_life`, `process_pov`, `subject_closeup`, `result_showcase`, `portrait_closeup`, `portrait_context`, `full_body`, `body_detail`, `social_frame` |
| share_intent | `atmosphere`, `record`, `show_and_tell`, `check_in`, `seek_feedback`, `progress_update`, `complain`, `care_update`, `humor`, `intimate_signal`, `memory_keep` |
| capture_mode | `character_front_camera`, `character_rear_camera`, `mirror`, `timer_fixed`, `requested_helper`, `known_companion`, `external_sender`, `existing_artifact` |
| character_visibility | `none`, `trace_only`, `identifiable`, `body_detail` |
| other_people_visibility | `none`, `anonymous_incidental`, `known_anonymized`, `identity_referenced` |
| polish | `raw`, `casual`, `curated` |
| tone | `neutral`, `calm`, `warm`, `bright`, `amused`, `playful`, `proud`, `tired`, `frustrated`, `embarrassed`, `tender`, `vulnerable` |
| privacy | `ordinary`, `personal`, `intimate` |

`soft/tender/bold` 仅用于恢复历史 `MediaPlan v1-v3`。新 `MediaPlan v4` 使用互相正交的
`physical_salience`、`sensual_charge` 与 `coverage_mode`，不得同时携带旧强度字段。所有亲密图片均为成年虚构角色；运行时最高边界是暗示性不透明遮挡，不支持关键部位、透明衣料、明确性行为或癖好式局部构图。

### 高档私密媒体：`suggestive_private`

`explicit_reserved` 保持为历史兼容的不可渲染占位。新的正式高档 lane 是
`suggestive_private`：成年人虚构角色、收件人专属、明确带有性暗示但仍非露骨的私密表达。
它不引入 prompt 旁路，复用 v5 的 Media Address、Camera Geometry、Subject/Embodied
Presentation；额外要求既有 `PrivateExpressionBasis`，以及
`/relationship_media_context/declared_display.media_intent=sexual_suggestive` 的冻结事件事实。
这项 `media_intent` 是内容分类，不是成人授权；成人授权仍属于上游环境。`explicit_private`
同理必须冻结 `media_intent=explicit_adult`。缺少专用生成器或该内容事实必须失败，不得回落
普通模型或低档 lane。完整矩阵与世界机输入合同见
[suggestive-private-media-lane.md](design/suggestive-private-media-lane.md)。

这意味着“私密”“只给收件人看”“带一点女人味”本身都不够：它们至多属于
`exclusive_private` 或 `alluring_life`。只有世界已冻结上述精确 `media_intent`，且规划候选
同时满足自摄、`invite_desire`、收件人直达、`charged/veiled`、私密遮盖与吸引力机制合同时，
才会选入高档 Civitai route。该 route 是纯 Krea2 LoRA＋冻结工作流提示词：运行时拒绝模板包含
任何固定或动态 `images` 输入，不使用 `assets/reference`。它按产品决定不调用通用视觉验收或重绘；
只保留预算、模板、provider 状态和文件完整性失败处理。因此这道语义合同是阻止普通图误入高档的关键门槛。

互斥由“唯一主证据＋组合维度”保证，而不是继续增加重叠大类。例如：

- 新项链：`object_possession + body_detail + show_and_tell`。
- 膝盖淤青：`body_health + body_detail + care_update`，且主证据必须指向 `body_health` 事实。
- 料理翻车自拍：`activity_process + portrait_context + complain + raw`。
- 精致早餐：`food_drink + contextual_still_life + atmosphere + curated`。

`other_grounded` 只接纳确有视觉证据但无法归入现有内容域的事件，不允许作为亲密内容兜底。

### MediaPlan v5：整图表达与镜头语言

v5 在内容分类之上冻结五层合同：

```text
Media Interaction Bid（期待怎样回应）
  → Media Address Strategy（整张图怎样面向收件人）
  → Camera Geometry v2（相机位置、取景、相机到脸的距离与脸在广角画面中的径向位置）
  → Photographic Authenticity Profile（整张图为何像真实手机影像）
  → Subject Presentation v4（姿态＋社交表演＋可见面部动作）
  → Embodied Presentation v3（身体状态、动作、衣装与遮盖）
```

`Media Address Strategy` 的单值轴为 `address_mode / engagement_tactic / disclosure_mode / staging_degree / temporal_beat / visual_priority / expression_charge`。完整值域和 Bid 宽兼容矩阵在 `configs/media_address_templates.yaml`。私密吸引力不是运动/睡衣类别，而是九种跨事件机制：直接邀请、玩笑式逗弄、保留注意、感官现场、私人信任、自信展示、被打断的过渡、近距离和氛围暗示。

`Camera Geometry` 冻结距离、高度、视轴、俯仰、滚转、方向、人物占比/位置、环境比例、焦点、自然瑕疵和设备可见度；完整矩阵在 `configs/media_camera_templates.yaml`。`capture_mode` 只回答“谁操作相机”，不再隐含伸直手臂、稍高竖屏或固定人物占比。

v5 的 LLM 返回基础分类、证据路径、`interaction_bid_id`、`complete_candidate_id` 与受限的媒体分流建议。最多 24 个完整候选已经绑定整图表达、镜头物理、人物姿态/面部表演、具身合同和参考图用途；LLM 不能返回自由构图、机位、表情或吸引力机制。动作以 `action_template_id + action_cue` 冻结。

### 媒体分流（Media Lane）

对新的 `character_media`，同一次 v5 规划调用还必须建议一个 `media_lane`、`recipient_access` 与 `attraction_expression`；它们不是自由提示词，而是被确定性路由器在完整候选选定后验证的语义合同：

| Lane | 人类含义 | 确定性硬条件 |
|---|---|---|
| `ordinary_life` | 纯粹分享生活 | 无吸引力表达、无表达张力候选 |
| `alluring_life` | 生活分享时有意或无意展示女人味/荷尔蒙感 | 已提交生活事件、`intimate` 上限、收件人存在、非零表达张力；不能声称仅收件人可见 |
| `exclusive_private` | “只给你看”的非露骨私人展示 | 世界冻结的收件人专属依据、`intimate` 上限、本人前摄或镜面自摄、收件人导向的地址策略和满足下限的表达张力 |
| `explicit_reserved` | 未来高档能力的名称预留 | 当前恒为 `NotRenderable`，没有生成或投递旁路 |

LLM 推荐 `exclusive_private` 但缺少事实依据时，图片机不会偷偷降级为日常或低档图：当前机会返回 `NotRenderable`，由世界机重新选择。反过来，世界已经冻结了有效 `private_expression_basis` 的机会，则它已进入 `exclusive_private` 工作流：规划器只暴露该 lane 合法的完整候选；模型仍选择内容与视觉候选，但 lane、收件人访问级别、`intimate_signal`、隐私和本人自摄合同由候选与冻结依据确定性绑定。这样模型把标签误写为普通分享不会让同一机会反复被拒绝，也不会把普通机会升级为私密。`alluring_life` 和 `exclusive_private` 都必须在视觉验收中保住相应的可见表达：前者不能退化成中性生活记录，后者不能退化成可被理解为广泛分享的普通自拍。旧计划与 `life_share` 的既有语义不迁移。

新计划使用 Subject Presentation v4。它保留 Pose Performance，并把面部进一步拆成 `Facial Display Strategy` 与 `Facial Micro-Performance`：前者说明表演对收件人的语义，后者冻结眉、眼裂、当前视线、鼻颊、嘴、左右不对称、表演强度、表演作者、单帧时相和能量。`amusement_leaking / deliberate_cuteness / mock_defiance / desire_direct / desire_withheld` 等不是事件硬映射；完整候选从宽兼容矩阵中稳定抽样。同一个玩笑意图可以皱鼻憋笑、装无辜或傲娇，同一种嘴型也不能脱离眼、鼻颊和时相独立拼接。系统不让 LLM 自由填写五官，也不把静态图宣称为可测量的真实“微表情”。

面部亲和矩阵与全部可见动作值域登记在 `configs/media_facial_performance_templates.yaml`，摄影真实性值域登记在 `configs/media_photographic_authenticity_templates.yaml`。每份新合同冻结目录版本，面部动作还冻结 `recipe_id`；修改亲和度只影响新计划，既有计划继续按已冻结动作回放。代码启动时校验配置值域与签名合同，避免配置和运行时悄悄漂移。

`Photographic Authenticity Profile` 独立于 Camera Geometry。它冻结设备成像、曝光折衷、白平衡/色彩、处理强度、场景秩序、一个可信拍摄瑕疵、环境熵、地域依据和审美意图。`documentary / pleasant_share / atmospheric / editorial` 都可是真实的人类影像；“真实”不等于统一降饱和、加颗粒、加模糊或塞满杂物。普通生活分享不会默认升级为 `commercial`，地域外观只能来自已选事件证据。

Camera Geometry v2 在旧几何上增加 `camera_face_distance` 与 `face_radial_position`。前摄因此可以区分贴近、正常臂长和有支撑的近距自拍，也能区分安全中心、内三分线、外三分线与边缘失真风险；这两个值进入 prompt、验收合同和感知去重。旧 `camera-geometry-v1` payload 的序列化与签名保持不变。

v5 允许 `life_share + intimate_signal`，但必须由世界给出 `intimate` 上限且证据指向已冻结地点、物品、衣装或环境。人物仍只能 `none/trace_only`，当前只允许 `atmospheric_suggestion`；不得从卧室推导脱衣、床上行为或私密衣装。

## 4. 硬验证

规划器在 LLM 返回后执行确定性验证：

- `life_share` 只允许 `none/trace_only`，且不能使用前摄、镜面、定时或路人协助。
- `character_media` 必须为 `identifiable/body_detail`。
- `known_companion` 必须有已登记同伴；`external_sender` 必须有非角色来源人物。
- `existing_artifact` 与 `reuse_existing` 必须成对出现且有可访问媒体引用；快照应冻结 `accessible: true`，渲染前仍会重新检查本地文件。
- 镜面需要镜面环境；路人协助需要公共场所。
- `body_health` 的主证据必须指向明确身体状态事实。
- `social_frame` 必须有人物事实；非社交画面不能凭空出现已知人物。
- `identity_referenced` 必须有身份资产；第一阶段通常使用 `known_anonymized`。
- 意义依赖可读文字的屏幕、通知或票据，没有原始媒体时返回 `NotRenderable`。
- 计划隐私不能高于世界冻结上限；`intimate_signal` 与 `intimate` 必须同时成立。
- 最近 12 张完全相同指纹被拒绝；最近 3 张的重复项作为 LLM 软惩罚输入。v5 还读取 `MediaInspection.perceptual_signature`，按实际可见的机位、占比、头向、视线、表情、姿态、具身策略、场景和参考资产排序候选；分类不同但看起来相同也会被惩罚。

LLM 不得把任意事实 prose 直接送入最终 prompt。构图、动作、机位和分享动机必须逐字选自受控自然语言目录；动作目录只有 `{primary}` 一个事实槽，由编译器使用冻结的主证据值替换。这样随机性来自“分类组合＋摄影表达模板”的组合，而地点、人物、物品、伤情和原图路径仍只能来自证据。世界机给出的 `expression_requirements` 可追加为已冻结约束，但图片机不会将其解释为新世界事实。

非法、未知路径、无效 JSON 或证据不足都返回结构化 `NotRenderable`；新事件路径没有“普通日常图”兜底。

### 自动失败处理

失败结果是可持久化的领域结果，不是未分类异常：

- 规划模型的 HTTP/传输失败返回 `NotRenderable(reason=planner_provider_failure)`；格式、枚举、证据或候选冲突返回相应的 `NotRenderable` 原因。世界机应按 Action 重试策略延后重试 provider failure，不能用新事件或新机会替换冻结机会。
- 图像 provider 将失败分为 `image_provider_transient`（408/409/429/5xx 等，可在同一冻结计划上即时再试一次）、`image_provider_transport`、`image_provider_quota`、`image_provider_invalid_request`、`image_provider_policy` 与 `image_provider_invalid_response`。除 transient 外不重复提交相同图像请求；记录为 `MediaRenderFailure`，由世界机根据原因标记 `failed`、`skipped` 或人工预览。视觉验收 provider 使用相同分类但以前缀 `inspection_provider_` 记录；短暂验收故障只重试同一张已生成文件一次，不重新花钱生成图片。
- 图像已生成但视觉验收失败，仍只允许一次定向修复；修复不能换事件、候选、参考资产、表达张力、衣装事实或摄影合同。
- 对自动投递，provider/inspection 不可用、身份不一致、参考图姿态/场景泄漏、衣装或遮盖与冻结事实冲突，都不得降级投递。`MediaGenerated` 只能在验收通过后写入。

### 人物呈现（Subject Presentation）

新生成的 `character_media` 还必须冻结一份 `Subject Presentation`，它不是新的图片分类，而是该镜头里人物怎样出现：

- `Subject Appearance`：发型整理、衣装角色、妆容整理程度、饰品，以及 `world_fact/media_local` 来源。
- `Subject Performance`：以画面坐标表达的头部偏转/俯仰/侧倾、视线目标、表情、肩线、姿态、手势、对镜头的意识、双手职责与遮挡复杂度。

规划器不会让 LLM 独立填写这些轴。图片机先根据拍摄方式、人物可见度、世界外观事实和近期历史，确定性地产生若干成套候选；v4 再把人物方案、社交表演和具身动作组合为完整 `CharacterPresentationCandidate`，同一次规划 LLM 只返回一个 `presentation_candidate_id`。这样保留“像人一样选择这次怎么拍”的随机性，同时避免任意拼接成木偶姿态。候选的完整人物签名参与最近 12 张硬去重，最近 3 张相似发型、视线、表情和头部方向参与软排序。

完整候选不是三个互不相干的配置片段。组合器会对 `capture_mode → camera_authorship → hand_occupancy → action_variant.required_free_hands` 做跨维度求交，并把通过后的拍摄物理合同连同 `Photo Display Strategy`、`Media Interaction Bid`、具身三轴和分类矩阵字段一起冻结。手持前摄或镜面最多只有一只自由手，因此只能选择单手抬发、单手擦汗等动作变体；双手重新扎发、双臂拉伸等动作只会进入定时或明确他拍候选。LLM 看不到非法拼接，也不能在返回结果中改写动作占手数。

### 具身表现与感官张力（MediaPlan v4）

`Embodied Presentation` 独立于人物表情模块，完整冻结：

| 维度 | 值 |
|---|---|
| `physical_salience` | `none / contextual / foregrounded` |
| `sensual_charge` | `none / subtle / charged / veiled` |
| `coverage_mode` | `fully_dressed / functional_bodywear / private_apparel / strategic_cover` |

`none` 可有运动后的汗水，但不对收件人调情；`subtle` 是柔和、收件人导向的身体存在感；`charged` 是有明确吸引力但非露骨的汗水、潮红、湿发、身体张力与有意识目光；`veiled` 只允许恋人关系和有明确衣装证据的不透明内衣、浴袍、浴巾、床单或宽大衣物遮挡。

图片机先用纯模块 `VisiblePhysicalStateResolver` 读取世界冻结的 `character.visible_physical_state`。该字段一旦存在即为权威，即使 cue 为空也禁止反向推导。缺失时只允许版本化的单镜头推导，例如高强度运动可推导中等汗水、潮红和呼吸恢复；雨天不能单独推出淋湿，普通运动不能推出湿透，洗澡不能单独推出湿发。推导结果不写回世界。

具身原型目录位于 `configs/media_embodiment_templates.yaml`，包含恢复停顿、重新扎发、擦汗降温、湿发整理、拉伸取物、运动余韵、镜前整理、贴近镜头的私人停顿、自然休息与有证据遮挡的过渡状态。每个原型可以提供多个 `action_variant`；变体只描述同一原型在不同设备支撑和自由手条件下如何成立，不会另造一套内容分类。硬过滤只处理事实、关系、隐私、遮盖、拍摄物理关系与明显 Affect 冲突；其余组合按 `opportunity_id` 稳定加权抽样。v4 指纹额外包含具身三轴、原型和动作变体，重试不得改换方案。

青绿色发夹是可选身份特征，不是每张照片的硬标记。计划未选择时，身份参考图中的发夹、发型、头歪角度、视线和笑容都不得被继承。

拍摄方式会确定性派生双手职责：前摄保留一只持机手，另一只手才可展示证据；镜面保留一只持手机的手；定时与他拍允许双手参与。具身动作同时冻结 `action_variant_id`、`required_free_hands` 和 `camera_support`，规划、恢复和渲染前都会复核这份跨模块合同。规划器对中高遮挡候选降权但不一律禁止，最终冻结 `hand_occupancy` 与 `occlusion_complexity`。恢复时若任一字段与拍摄方式冲突，计划无效；升级前的 `Embodied Presentation v1` 和旧人物 payload 按原签名兼容恢复，不重新选动作。

## 5. 持久化与回放

`MediaPlan v5` 新计划完整冻结动作模板、Media Address Strategy、Camera Geometry、Photographic Authenticity Profile、Subject Presentation v4、Embodied Presentation v3、Identity Reference Selection，以及规划时的表达张力上限和关系阶段依据。参考资产 ID、用途与目录版本属于计划；重试不换候选、不换机位、不换表情机制。身份参考分为 `canonical_identity`、`angle_support` 和 `scene_only`：任何侧颜/强回头素材只能作为第二张角度辅助，不能单独生成或主导身份验收；每一个人物计划都保留 canonical 主锚点，验收以其近正面五官结构为主。升级前已经冻结的 v5（Subject v3、无真实性档案）仍按原 payload 恢复；v1-v4 也不会补写新字段或重解释历史照片。

`MediaPlan v4` 在 v3 之上冻结 `Embodied Presentation` 与完整人物候选合同。历史 `event-media-plan-v1/v2/v3` 和 `MediaShotPlan v1-v3` 仍按原 payload 恢复、渲染和投递，不补选互动期待或人物状态，也不因模板升级改变旧照片含义。旧计划继续使用 `soft/tender/bold` 的原 prompt、参考图和质量门语义；v2-v4 均继续执行结构质量门。

### 互动期待与社交表演

每份新计划冻结一个 `Media Interaction Bid`：唯一的 `bid_id=media-bid:<opportunity_id>`、`communicative_goal`、`hoped_response`、`response_pressure` 和世界提供的 `audience_ref`。它说明角色希望照片邀请怎样的回应，不声称回应已经发生，也不要求用户必须回应。`life_share` 同样可以携带期待，但不携带人物表演。互动期待目录独立存放在 `configs/media_interaction_templates.yaml`，不依赖人物模板。

角色媒体还会在 `Subject Presentation v2` 中冻结一个 `Photo Display Strategy`。两者职责不同：互动期待描述关系中的目的；社交表演描述画面怎样表达这个目的。例如同一个 `invite_playful_exchange` 可以使用卖呆、装委屈、冷面展示、忍笑或自嘲，不存在“事件类别固定映射到表情”的规则。

社交表演目录包含整体行为、嘴、眼、眉、视线质感、表情瞬间和禁止线索。人物姿态外壳与表演配方只通过明确绑定形成完整候选；LLM 仍只选择候选 ID，不能独立拼接五官。硬过滤限于：

- 隐私与关系上限；
- 拍摄方式、人物可见度和手部职责；
- 世界明确给出的严重 Affect 与轻佻/亲密策略之间的明显冲突；
- 世界可选的 Display Strategy 边界。

其余关系阶段、公开 Affect、分享意图、tone 和近期历史只改变候选亲和度与稳定随机顺序。缺少关系上下文时，允许普通朋友也成立的低强度卖呆、玩笑和自嘲，但不开放 `invite_closeness` 或需要明确关系依据的吸引力表达。v4 新增 `invite_desire`，表示希望对方低压力地表达吸引或亲昵；它只允许 `privacy=intimate` 且至少为 `charged`，不同于请求亲近回应的 `invite_closeness`。

世界机必须把规划调用建模为：

1. 创建 `media_planning` Action，payload 含完整 `MediaOpportunity`。
2. 调用 `MediaPlanner.plan()` 一次。
3. 将 `MediaPlan` 或 `NotRenderable` 写成 External Result。
4. 只有成功规划后才创建媒体生成/复用 Action。
5. 恢复与重试反序列化原 `MediaPlan`，不得再次调用 LLM。
6. `MediaGenerated` 记录 `artifact_hash`、`MediaInspection.observed_summary`、可见事实和偏差。

配文输入固定为：

```text
event_snapshot
+ MediaPlan.share_intent
+ MediaPlan.interaction_bid
+ MediaInspection.observed_summary
+ MediaInspection.deviations
```

表达层不得根据原 prompt 猜图。图片机的 `planned_summary` 只解释规划意图，不是最终角色台词。

## 6. 渲染与验收

- `reuse_existing` 不调用图片生成器，只做存在性检查、哈希和视觉验收/摘要。
- `identifiable/body_detail` 加载角色参考图。
- v2-v4 角色媒体按人物计划选择身份参考：v4 的 `none/subtle/charged/veiled` 分别映射日常、保守私密和 bold 身份参考；参考图只约束身份，不得覆盖本次姿态、遮盖或身体动作。
- 人物呈现的内部枚举通过 `media_subject_templates.yaml` 的渲染词典编译成可见、可执行的摄影描述；通用身份锚点先写入，镜头级发型、姿态和手势最后写入并拥有更高优先级。
- `life_share + trace_only` 不加载角色身份图，降低意外生成人脸的概率。
- 渲染器统一处理 OpenAI/ComfyUI adapter、预算估算和最多一次定向修复。
- 修复 prompt 携带验收原因，但保持事件、分类、拍摄来源、构图、隐私和场景不变。
- 自动投递时，验收通过但没有 `observed_summary` 仍视为失败。
- v2/v3 验收同时记录实际可见的人物呈现；若画面违背冻结姿态，或照抄身份参考图的姿态、视线、表情、发型与构图组合，则拒绝并在唯一一次修复中定向纠正。
- v2/v3 验收还返回 `garment_topology_ok`、`hand_sleeve_occlusion_ok` 和 `evidence_attachment_ok`。袖口/手腕融合、衣物吞手、展示物漂浮或粘到错误表面均触发同一计划的一次定向修复；自动投递缺少这些检查字段时 fail closed。
- v3 验收额外返回整体社交策略、是否大体匹配、表情是否无畸形、显著线索和禁止线索。明显语义反转、表情畸形或出现禁止线索会定向修复；眉毛不明显等辅助偏差只记录，不要求视觉模型判断精确肌肉几何。
- v4 验收进一步同时检查相机作者、持机手、动作所需自由手和社交期待是否在同一画面中成立。比如计划为前摄单手抬发却出现双手扎发，或动作成立但画面完全抹掉 `invite_desire/seek_care` 的收件人导向，均会在不重分类的前提下定向修复一次。
- v4 验收额外返回身体显著性、感官档位、遮盖方式、实际身体 cue、无依据 cue、非露骨边界和非癖好式构图。`charged` 退化为普通人像、无依据汗水/湿发/衣装、过度暴露、局部癖好式裁切或不可能的肩带/袖口/毛巾/床单/镜面关系均触发唯一一次定向修复。
- v5 使用 `MediaInspection v7`，增加实际 Camera Geometry、实际 Media Address Strategy、Bid/拍摄关系可读性、普通写真稀释、摄影真实性、身份一致性和感知签名。带新合同的计划还检查实际 Facial Display Strategy、眉眼鼻颊嘴等可见动作、通用微笑回退、参考表情照抄、真实性档案匹配、商业效果图膨胀和地域依据。第三方图像像狗仔或无来源 AI 写真、前摄违背冻结占比、`invite_desire` 退化成礼貌微笑、皱鼻/嘟嘴等合同动作消失、复制参考图头歪/笑容/发型/构图、普通随手拍变成棚拍效果图，均拒绝并只定向修复一次。
- 两次生成均失败或视觉验收不可用时返回失败，投递层不得发送。

## 7. 世界机待办

世界机 agent 应独立完成以下改造；图片机模块不直接修改世界事件内核：

### 候选投影

每个已提交事件自动进入 `Photo Candidate` 投影，不额外追加候选事件。生命周期：

```text
available → selected → planned → generated → shared
    └────────→ skipped / unrenderable / expired / failed
```

时效默认值：

- `fleeting`: 12 小时
- `daily`: 48 小时
- `durable`: 7 天

评分考虑新鲜度、视觉性、情绪意义、分享价值、已有媒体、历史新颖度、预算和用户偏好。默认最多每日 2 张、间隔至少 6 小时，均可配置。

### 机会冻结

选择候选时一次冻结：

- `opportunity_id`
- `family`
- `privacy_ceiling`
- `sensual_charge_ceiling`（缺失默认 `none`）
- `expression_charge_ceiling`（v5 名称；缺失兼容读取上一字段，两者并存且冲突时拒绝）
- 完整通用事件快照
- `delivery_mode`
- 仅当事件已有依据时才提供表现要求

若照片需要呈现中国城市、特定交通系统或其他地域特征，世界快照应提供结构化 `location.country / region / city` 或同等来源字段，并由计划选入证据；只有自由文本地点名时图片机使用 `regional_grounding=none`，不得靠伪造招牌、车厢、票据或建筑制度感猜测地域。

图片机可以拒绝当前机会，但不能改选另一个事件。世界机拥有是否拍摄、是否发送和配文权。

世界机可选冻结 `character.visible_physical_state`（`visible-physical-state-v1`）。每个 cue 必须包含受控 `cue_id`、强度、可见区域、逻辑时间与来源事件；初期只记录汗水、潮红、呼吸恢复、湿发、雨水状态、困倦、疲劳姿态和活动肌肉张力，不记录或推断性唤起、疾病、伤情、醉酒或未发生变化。世界机仍决定 `expression_charge_ceiling`：默认 `none`；`ambiguous` 自动上限可到 `charged`；`veiled` 仅 `lover`，但关系阶段本身绝不自动产生或发送照片。

### 人物资产扩展点

第一阶段家人朋友使用 `known_anonymized`。未来在参与者投影中增加 `identity_reference` 后，可以启用 `identity_referenced`，无需修改分类体系。

### Appearance State 扩展点

世界机可新增 `Appearance State` 投影，但应遵守：

- 只由已提交事件、明确换装/整理行为或可靠连续状态更新；必须带来源与生效逻辑时间。
- 机会快照冻结事件当时版本，面板和图片机读取同一份值；恢复任务不读取当前最新外观。
- 情绪/精力可以限制候选表情的合理范围，但不应直接写死某张照片的笑容、头部朝向或视线；这些仍由图片机在合理范围内选择。
- 生成图与视觉验收观察到的发型、衣装和饰品不能自动反写 `Appearance State`，否则模型产物会循环升级成世界事实。
- 字段缺失必须合法；图片机的 `media_local` 外观回退与 derived physical cue 只属于单张计划，不写回世界。

### Audience Context 与待回应状态

世界机可在 `MediaOpportunity` 中冻结可选 `AudienceContext`：`recipient_ref`、有来源的 `relationship_stage`、可公开 Affect 和可选 Display Strategy 边界。世界机不得提供嘟嘴、眉眼、头向或生图 prompt。

### 私密表达资格分流（v5）

`character_media` 不是私密媒体的同义词。图片机在规划前用 `MediaEligibilityRouter` 将机会分类为 `personal_selfie` 或 `private_expression`；它不决定发送，也不把被拒绝的私密机会偷偷改成日常图。

世界机若要请求 `intimate` 或非 `none` 的表达张力，必须冻结一个 `PrivateExpressionBasis`：一个主类别、证据 JSON Pointer，以及本次最小张力（`subtle / charged / veiled`）。类别是互斥的主解释，而不是姿势或 prompt：

| 主类别 | 必要世界证据根 | 含义 |
| --- | --- | --- |
| `relational_turn` | `/relationship_media_context/active_exchange` | 已提交的私下互动正在形成可见回应。 |
| `recipient_display` | `/relationship_media_context/declared_display` | 角色当次明确决定向该收件人展示自己。 |
| `embodied_state` | `/character/visible_physical_state` | 已证实的汗水、湿发、恢复状态等构成本张图的身体现场感。 |
| `private_transition` | `/activity/private_transition` | 有明确衣装/活动证据的私人准备、整理或过渡，不可由“在卧室”推断。 |
| `shared_ritual` | `/relationship_media_context/shared_ritual` | 已建立的共同私人仪式或记忆线索。 |

资格验证要求指向的值真实非空，并且每个私密机会都必须有冻结的 `AudienceContext.recipient_ref`。三种关系型依据（`relational_turn`、`recipient_display`、`shared_ritual`）还必须在其根对象中带与之完全一致的 `recipient_ref`；具身/过渡依据则由同一冻结收件人上下文绑定。规划成功后，图片机将主类别、唯一选中的证据指针、证据值、最小张力和收件人冻结进 `MediaPlan`，并要求这个指针同时出现在选中视觉证据、渲染 prompt 与视觉验收合同中；恢复或修复不会重新解释资格依据。

没有该依据的普通回家、吃东西、读书、穿搭或镜前整理，只能作为 `personal_selfie` 规划。图片机返回 `private_lane_unsupported_by_event` 和 `recommended_lane=personal_selfie`，由世界机决定是否以日常自拍重新选择；不得在图片侧绕过世界授权。进入 `private_expression` 的人物媒体只能使用 `character_front_camera` 或 `mirror`；定时器、路人、同伴和外部拍摄均不属于“只给你看”的私密入口。

对于 `character_front_camera`，计划与渲染必须让自拍作者关系可见：可信的持机手/前臂正在操作手机，或局部设备边缘至少一项成立；它不能退化为第三方或隐形三脚架肖像。对于 `mirror`，镜中必须可见角色持有的手机，手机、手、反射和机位必须一致。私密计划即使仅 preview 也必须返回 `capture_relationship_legible`，视觉验收将这两者作为硬条件。

选择第一期完整闭环：只有媒体通过验收、世界机决定发送且收到真实投递回执后，世界机才把计划里的 `Media Interaction Bid` 建立为待回应状态：

```text
planned → generated → delivered
                         ↓
                pending interaction bid
                 ↙ answered
                 ↘ expired / superseded / withdrawn
```

待回应记录至少包含 `bid_id`、`media_plan_id`、`recipient_ref`、交流目的、期待回应、回应压力、发送逻辑时间、过期时间、状态及结算来源消息。用户回应采用宽松语义匹配，不要求固定关键词。未发送、生成失败和仅预览图片不得打开期待；无回应或自然过期也不得自动产生负面 Affect，如角色确实在意必须另经 World Appraisal。

## 8. 接入顺序

1. 仅开启 preview，保存规划 External Result，不生成。
2. preview 下生成并展示 `MediaInspection`，人工核对跨矩阵样本。
3. 允许人工确认投递。
4. 在频率和预算门生效后开启 automatic；任何验收异常都 fail closed。

旧用户索图入口继续使用现有路径，不扩充能力。普通创意绘图不进入事件媒体分类，只可复用底层图片生成 adapter。

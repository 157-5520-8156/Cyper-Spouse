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

当前模块没有接入世界写模型，因此默认线上行为不变。`MediaPlanner` 默认关闭；仅当 `COMPANION_EVENT_MEDIA_ENABLED=1`（或构造时显式 `enabled=True`）才接受新机会。世界机接入时应先仅在预览模式写入新 Action/External Result，再开放自动投递。

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

`character.appearance_state` 是世界机后续可选扩展，不是当前接入的硬前提。它只描述事件时刻已经成立的可见连续事实，例如发型整理方式、衣装角色、妆容整理程度和饰品。图片机有该字段时逐字冻结为 `world_fact`；没有时只为本张照片生成 `media_local` 外观，不写回世界，也不能让下一事件把它当作已发生事实。自由文本 `appearance` 可继续作为事件证据，但不承担跨照片的结构化连续性。

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

互斥由“唯一主证据＋组合维度”保证，而不是继续增加重叠大类。例如：

- 新项链：`object_possession + body_detail + show_and_tell`。
- 膝盖淤青：`body_health + body_detail + care_update`，且主证据必须指向 `body_health` 事实。
- 料理翻车自拍：`activity_process + portrait_context + complain + raw`。
- 精致早餐：`food_drink + contextual_still_life + atmosphere + curated`。

`other_grounded` 只接纳确有视觉证据但无法归入现有内容域的事件，不允许作为亲密内容兜底。

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
- 最近 12 张完全相同指纹被拒绝；最近 3 张的重复项作为 LLM 软惩罚输入。

LLM 不得把任意事实 prose 直接送入最终 prompt。构图、动作、机位和分享动机必须逐字选自受控自然语言目录；动作目录只有 `{primary}` 一个事实槽，由编译器使用冻结的主证据值替换。这样随机性来自“分类组合＋摄影表达模板”的组合，而地点、人物、物品、伤情和原图路径仍只能来自证据。世界机给出的 `expression_requirements` 可追加为已冻结约束，但图片机不会将其解释为新世界事实。

非法、未知路径、无效 JSON 或证据不足都返回结构化 `NotRenderable`；新事件路径没有“普通日常图”兜底。

### 人物呈现（Subject Presentation）

新生成的 `character_media` 还必须冻结一份 `Subject Presentation`，它不是新的图片分类，而是该镜头里人物怎样出现：

- `Subject Appearance`：发型整理、衣装角色、妆容整理程度、饰品，以及 `world_fact/media_local` 来源。
- `Subject Performance`：以画面坐标表达的头部偏转/俯仰/侧倾、视线目标、表情、肩线、姿态、手势、对镜头的意识、双手职责与遮挡复杂度。

规划器不会让 LLM 独立填写这些轴。图片机先根据拍摄方式、人物可见度、世界外观事实和近期历史，确定性地产生若干成套候选；v4 再把人物方案和具身方案组合为完整 `CharacterPresentationCandidate`，同一次规划 LLM 只返回一个 `presentation_candidate_id`。这样保留“像人一样选择这次怎么拍”的随机性，同时避免任意拼接成木偶姿态。候选的完整人物签名参与最近 12 张硬去重，最近 3 张相似发型、视线、表情和头部方向参与软排序。

### 具身表现与感官张力（MediaPlan v4）

`Embodied Presentation` 独立于人物表情模块，完整冻结：

| 维度 | 值 |
|---|---|
| `physical_salience` | `none / contextual / foregrounded` |
| `sensual_charge` | `none / subtle / charged / veiled` |
| `coverage_mode` | `fully_dressed / functional_bodywear / private_apparel / strategic_cover` |

`none` 可有运动后的汗水，但不对收件人调情；`subtle` 是柔和、收件人导向的身体存在感；`charged` 是有明确吸引力但非露骨的汗水、潮红、湿发、身体张力与有意识目光；`veiled` 只允许恋人关系和有明确衣装证据的不透明内衣、浴袍、浴巾、床单或宽大衣物遮挡。

图片机先用纯模块 `VisiblePhysicalStateResolver` 读取世界冻结的 `character.visible_physical_state`。该字段一旦存在即为权威，即使 cue 为空也禁止反向推导。缺失时只允许版本化的单镜头推导，例如高强度运动可推导中等汗水、潮红和呼吸恢复；雨天不能单独推出淋湿，普通运动不能推出湿透，洗澡不能单独推出湿发。推导结果不写回世界。

具身原型目录位于 `configs/media_embodiment_templates.yaml`，包含恢复停顿、重新扎发、擦汗降温、湿发整理、拉伸取物、运动余韵、镜前整理、贴近镜头的私人停顿、自然休息与有证据遮挡的过渡状态。硬过滤只处理事实、关系、隐私、遮盖、拍摄物理关系与明显 Affect 冲突；其余组合按 `opportunity_id` 稳定加权抽样。v4 指纹额外包含四个具身轴，重试不得改换方案。

青绿色发夹是可选身份特征，不是每张照片的硬标记。计划未选择时，身份参考图中的发夹、发型、头歪角度、视线和笑容都不得被继承。

拍摄方式会确定性派生双手职责：前摄保留一只持机手，另一只手才可展示证据；镜面保留一只持手机的手；定时与他拍允许双手参与。规划器对中高遮挡候选降权但不一律禁止，最终冻结 `hand_occupancy` 与 `occlusion_complexity`。恢复时若二者与拍摄方式冲突，计划无效；旧 v2 payload 缺少这两个字段时按兼容语义恢复，不重新选动作。

## 5. 持久化与回放

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
- v4 验收额外返回身体显著性、感官档位、遮盖方式、实际身体 cue、无依据 cue、非露骨边界和非癖好式构图。`charged` 退化为普通人像、无依据汗水/湿发/衣装、过度暴露、局部癖好式裁切或不可能的肩带/袖口/毛巾/床单/镜面关系均触发唯一一次定向修复。
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
- 完整通用事件快照
- `delivery_mode`
- 仅当事件已有依据时才提供表现要求

图片机可以拒绝当前机会，但不能改选另一个事件。世界机拥有是否拍摄、是否发送和配文权。

世界机可选冻结 `character.visible_physical_state`（`visible-physical-state-v1`）。每个 cue 必须包含受控 `cue_id`、强度、可见区域、逻辑时间与来源事件；初期只记录汗水、潮红、呼吸恢复、湿发、雨水状态、困倦、疲劳姿态和活动肌肉张力，不记录或推断性唤起、疾病、伤情、醉酒或未发生变化。世界机仍决定 ceiling：默认 `none`；`ambiguous` 自动上限可到 `charged`；`veiled` 仅 `lover`，但关系阶段本身绝不自动产生或发送照片。

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

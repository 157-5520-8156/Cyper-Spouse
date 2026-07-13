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
    "body_health": {}
  },
  "existing_media": [],
  "visual_requirements": {
    "requires_readable_text": false
  }
}
```

快照只包含已经提交且允许展示的事实。未来计划、隐藏心理、模型推断和专为生图撰写的 prompt 不得进入快照。规划器只允许使用存在的 JSON Pointer；计划同时冻结解析后的证据值，因此恢复和重试不需要重新打开世界投影。

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

`soft/tender/bold` 只在 `intimate_signal` 下作为强度修饰。所有亲密图片均为成年虚构角色、非露骨、关键部位遮盖、无明确性行为。

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

## 5. 持久化与回放

`MediaPlan v1` 包含：机会与事件 ID、快照哈希、全部分类值、唯一主证据、辅助证据、解析后的证据值、构图、动作、镜头、动机、禁止项、路由和去重指纹。

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
+ MediaInspection.observed_summary
+ MediaInspection.deviations
```

表达层不得根据原 prompt 猜图。图片机的 `planned_summary` 只解释规划意图，不是最终角色台词。

## 6. 渲染与验收

- `reuse_existing` 不调用图片生成器，只做存在性检查、哈希和视觉验收/摘要。
- `identifiable/body_detail` 加载角色参考图。
- `life_share + trace_only` 不加载角色身份图，降低意外生成人脸的概率。
- 渲染器统一处理 OpenAI/ComfyUI adapter、预算估算和最多一次定向修复。
- 修复 prompt 携带验收原因，但保持事件、分类、拍摄来源、构图、隐私和场景不变。
- 自动投递时，验收通过但没有 `observed_summary` 仍视为失败。
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
- 完整通用事件快照
- `delivery_mode`
- 仅当事件已有依据时才提供表现要求

图片机可以拒绝当前机会，但不能改选另一个事件。世界机拥有是否拍摄、是否发送和配文权。

### 人物资产扩展点

第一阶段家人朋友使用 `known_anonymized`。未来在参与者投影中增加 `identity_reference` 后，可以启用 `identity_referenced`，无需修改分类体系。

## 8. 接入顺序

1. 仅开启 preview，保存规划 External Result，不生成。
2. preview 下生成并展示 `MediaInspection`，人工核对跨矩阵样本。
3. 允许人工确认投递。
4. 在频率和预算门生效后开启 automatic；任何验收异常都 fail closed。

旧用户索图入口继续使用现有路径，不扩充能力。普通创意绘图不进入事件媒体分类，只可复用底层图片生成 adapter。

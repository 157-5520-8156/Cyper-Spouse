# World v2 → 图片机 v5：接入缺口与实施合同

状态：交接设计，尚未实施  
日期：2026-07-16  
读者：World v2 agent、图片机维护者  
相关规格：[World v2 重构计划](../world-v2-refactor-plan.md)、[图片机合同](../media-machine.md)

> 实施状态更新（2026-07-16）：P0 的 source-bound life-share preview、P1
> 的候选选择/接受批次，以及 P2 的普通角色公开 preview 已接入 World v2
> 生产组装。P2 的角色合同、AppearanceState 和可选短时身体状态会在
> pinned event time 冻结；capture/合同授权留在外层 sidecar，不作为图片
> 规划证据。P3 现已开放一个窄的、可回放的 recipient-scoped 路径：私密
> 视觉事实声明、关系阶段与**正向**短时身体状态会冻结为 V3 sidecar，外层
> 授权和 `media-selection-acceptance.2` 绑定其 digest。当前只可形成
> `alluring_life` preview，支持 `embodied_state` 与已声明的
> `private_transition` basis；关系私下互动、共同仪式、coverage 与
> `exclusive_private` 仍 fail-closed。
> `suggestive_private` / `explicit_reserved` 未接入 World v2，不能把本文件
> 的后续 P3/P4 清单误读为已经上线的能力。

> 当前验证入口：`tests/world_v2/test_media_evidence_snapshot.py`、
> `test_media_opportunity_authorizer.py`、
> `test_media_selection_acceptance_manifest.py`、
> `test_event_media_planner_adapter.py` 与
> `test_production_turn_application.py`。其中生产测试覆盖 SQLite 上的
> `活动→ImageEvidenceDeclared→角色候选→Selection Proposal→Acceptance→V2
> snapshot`；图片桥接测试还覆盖 P2 capture 越权拒绝。

## 结论

World v2 已经具备媒体链路最难替换的骨架：不可变 sidecar、候选/机会/计划/验收/预览记录、effect-once Action、预算、修复与投递后的互动线程。它还**没有**具备图片机 v5 的输入适配层。

当前的 `FrozenMediaEvidenceSnapshot` 只证明“哪些已提交事件可以被读取”，其已有的 `complete_candidate` 主要是证据坐标；它不是图片机可直接规划的 `event_snapshot`。因此不能把它直接传给 `event_media.MediaPlanner`，也不能让图片机在规划时重新读可变的 World projection。

推荐收敛为两个深 Module：

```text
Photo Candidate + pinned World projection
  → MediaEvidenceSnapshotCompiler.compile(selection)
  → immutable ImageEventSnapshot sidecar
  → MediaOpportunityAuthorizer.authorize(selection, snapshot)
  → existing MediaPlanningRuntime.freeze_and_authorize(...)
  → existing planning / render / inspection / delivery lifecycle
```

- `MediaEvidenceSnapshotCompiler` 吸收 World → 图片机字段映射、来源证明、隐私过滤、事件时刻的 appearance 解析和缺失字段降级。
- `MediaOpportunityAuthorizer` 吸收机会选择的硬验证、媒体隐私/张力上限、受众绑定、私密资格和 deterministic ID。
- 图片机继续只消费冻结的 `MediaOpportunity`；它不能读 World projection、补世界事实、替世界重选事件或决定发送。

这比让生态、Deliberation、Action worker 分别拼一部分 JSON 更深：World 调用者只需表达“选择哪一个候选、给谁、允许到哪里”，其余复杂性集中于两个 Module 内，并可通过同一 Interface 做回放测试。

## 1. 已核对的当前实现

| 能力 | 当前状态 | 结论 |
| --- | --- | --- |
| 不可变机会/计划 sidecar | 已有 `FrozenMediaEvidenceSnapshot`、`StoredMediaPayload` 和 hash 校验 | 可复用；需要承载新的图片事件快照。 |
| planning Action 的幂等与恢复 | 已有 `MediaPlanningRuntime` + `MediaPlanningWorker` | 可复用；需要接入实际图片机 planner adapter。 |
| render / inspection / repair Action | 已有 `MediaExecutionRuntime`、`EventMediaExecutionAdapter` | 可复用；必须先有 v5 plan sidecar。 |
| preview、人工批准与 delivery | 已有 `MediaPreview`、approval、`MediaDeliveryShared` 与 delivery-trigger | 可复用；不应由图片机决定。 |
| life 事件 → 图片候选 | 已有 `EventEcologyMediaCandidateRuntime` | 仅是第一期窄入口：只处理 `public/shareable`、只产出 `life_share`、直接冻结 preview opportunity。 |
| 世界选择“是否拍、拍哪一张、life/character family” | 未完成 | 需要候选选择/授权 Module；不能由当前 ecology 直接替代。 |
| World v2 → 图片机 v5 planner bridge | 未找到 | 必须新增；当前 World v2 `MediaPlanner` Protocol 与 `event_media.MediaPlanner` 的机会对象不同。 |
| 事件时刻的可见外观、具体衣装事实 | 未完成 | 不需要先做完整衣柜，但需要稀疏、可溯源的 Appearance State。 |
| 可见身体状态、私密资格事实、受众上下文 | snapshot 类型预留了部分字段，生产投影/编译器未完成 | 作为第二、三阶段输入补齐。 |

### 1.1 当前 ecology 的正确定位

`EventEcologyMediaCandidateRuntime` 是一个合格的**证据发现** Module：它不凭空造生活事实，能根据已提交活动、结果、经验和少量视觉事实产生受控的 `life_share` 候选，并有确定性的频率抑制。

但它现在同时创建 `MediaOpportunityFrozen`。这在第一期公开生活分享 preview 中可保留为 feature-flagged 兼容路径；它不应成为目标架构，因为它绕过了：

1. 世界 Deliberation 对“这次要不要拍”的选择；
2. `character_media`、受众、表现张力与私密资格；
3. 更丰富但仍有来源的事件快照；
4. 记录受控随机抽样的机会选择。

目标迁移不是删除 ecology，而是把其输出收窄为 `PhotoCandidateOpened(available)`，再由新授权 Module 冻结机会。旧的“直接冻结 public life-share preview”只在显式兼容开关下继续存在，不能悄悄扩展到人物或私密图片。

## 2. 必须先对齐的词汇与轴

当前 World v2 与图片机各自的命名都合理，但它们不是同一概念。接入前必须显式转换，禁止复用同名字符串猜语义。

| 概念 | World v2 当前/建议 | 图片机 v5 | 规则 |
| --- | --- | --- | --- |
| 世界事实可见性 | `public / shareable / personal / private / withhold` | 不直接使用 | 决定能否进入某受众的快照。 |
| 图片表现隐私上限 | 新增 `MediaPrivacyCeiling = ordinary / personal / intimate` | `privacy_ceiling` | `private` 世界事实不自动等于 `intimate` 图片。 |
| 感官/收件人导向上限 | 新增 `ExpressionChargeCeiling = none / subtle / charged / veiled` | `expression_charge_ceiling` | 只限制图片机，不产生拍摄或发送权。 |
| 图片 lane | World 存为经验证的 `MediaLane` | `ordinary_life / alluring_life / exclusive_private / explicit_reserved` | `explicit_reserved` 永远不可渲染。 |
| 受众关系 | 已有关系投影的受限读取 | `AudienceContext` | 只给关系阶段、可公开 Affect、display bounds；不给姿势/表情/prompt。 |
| 事件事实 | committed ledger evidence | `event_snapshot` 的 JSON Pointer | 每个可被图片机选中的值都有明确来源。 |

推荐的**硬映射**：

| World 选择 | 最多允许的图片机输入 |
| --- | --- |
| `public/shareable` 事实 | `MediaPrivacyCeiling=ordinary`；通常仅 `life_share`。 |
| `personal` 事实 | `ordinary` 或 `personal`；可为 `character_media`，但不自动开放张力。 |
| `private` 事实 | `personal`；只有独立私密资格同时成立时才可到 `intimate`。 |
| `withhold` 事实 | 不得进入候选或快照。 |
| 任何关系阶段 | `ExpressionChargeCeiling=none`，除非本次世界选择显式批准。 |
| `ambiguous` 关系 + 已批准私密资格 | 最大 `charged`。 |
| `lover` 关系 + 已批准私密资格 + 有衣装/遮盖事实 | 最大 `veiled`。 |

上表是 Acceptance 的上限检查，不是行为脚本。LLM 可以建议不拍、选低一档或不发送；它不能越过事实/同意/预算/上限。

## 3. 新的冻结快照合同

### 3.1 为什么不能直接传 `FrozenMediaEvidenceSnapshot`

当前对象只含 `source_events` 以及可选 `complete_candidate / location / visible_physical_state / recipient_context`。这能证明来源，却不能回答图片机最基本的问题：事件是什么、角色是否在场、活动和物品是什么、同伴是否实际在场、某件衣服是否在该时刻成立、是否有可复用原图。

反过来，把整个 mutable projection 或一段自由摘要交给图片机也不行：恢复时会得到新地点/新衣装，或让未提交的模型推断混进图片事实。

### 3.2 `ImageEventSnapshot v1`

在**同一个 immutable sidecar** 内增加一个版本化 `image_event_snapshot`。它是图片机 `event_media.MediaOpportunity.event_snapshot` 的唯一来源，而不是第二份世界真相。

```json
{
  "schema_version": "world-image-event-snapshot-v1",
  "event": {
    "event_id": "event:...",
    "type": "activity_completed",
    "status": "committed",
    "logical_at": "2026-...",
    "summary": "已提交的可展示摘要",
    "outcome": "已提交的可展示结果"
  },
  "source": {"channel": "direct_experience", "person": "character"},
  "location": {},
  "activity": {},
  "participants": [],
  "objects": [],
  "environment": {},
  "character": {
    "emotion": "公开可展示状态或省略",
    "energy": "公开可展示状态或省略",
    "appearance": "可选自由文本事实",
    "appearance_state": null,
    "visible_physical_state": null
  },
  "existing_media": [],
  "visual_requirements": {"requires_readable_text": false},
  "relationship_media_context": null,
  "evidence_index": {}
}
```

顶层字段应稳定存在；对象可以为空，未成立的可选状态使用 `null`。图片机缺少视觉依据时返回 `NotRenderable`，不得使用“泛化日常图”填充。

`evidence_index` 是必需的来源索引，键为允许规划器读取的 JSON Pointer：

```json
{
  "/activity/description": {
    "source_event_ref": "event:activity-completed:...",
    "source_payload_hash": "...",
    "visibility": "personal"
  }
}
```

规则：

1. 所有非结构性元数据的叶子值均须有索引；容器可省略。
2. `source_event_ref` 必须在外层 `source_events`，hash 必须一致。
3. 编译器只可从 pinned cursor 的 committed evidence、经过可见性过滤的投影读取；不得读取 Private Impression、未来计划、未结算 Action、LLM 草稿或原 prompt。
4. 图片机只可选择 `evidence_index` 已列出的 pointer；冻结 MediaPlan 同时保存解析值，render/repair 不再读 World。
5. `existing_media` 还需 `artifact_ref`、hash、来源和访问权；没有真实资源不得伪造截图、票据或可读屏幕。

### 3.3 位置、活动、参与者与既有媒体

这些字段不是“提示词槽位”，而是事实投影的最小视觉切片。推荐如下形状；字段缺失合法。

| 切片 | 可用字段 | 不能做的事 |
| --- | --- | --- |
| `location` | `id`、`kind`、`country/region/city`、`publicness`、已证实 `mirror_available` | 不能从地点名猜国家制度、招牌或室内格局。 |
| `activity` | `id`、`kind`、`description`、`phase`、`intensity`、`private_transition` | 不能从“在家/卧室”推断洗澡、换衣或上床。 |
| `participants` | `id`、`role`、`present`、`visibility_permission` | 无 `present=true` 不得选择 `known_companion`/社交画面。 |
| `objects` | `id`、`kind`、`description`、`ownership/visibility` | 不能从抽象事件生成具体商品或文字。 |
| `environment` | 已证实光线、天气、现场结构/地域依据 | 不得用美术化环境替代未知地点。 |
| `existing_media` | 原图 ID/ref/hash、可访问性、来源拍摄者 | 有资源才走 `reuse_existing`；只有描述时可作为场景事实，不能冒充原图。 |

## 4. 缺口一：不需要衣柜系统，但需要稀疏 Appearance State

图片机不应要求世界机每天记录“今天穿哪件衣服”。这会把世界机变成衣柜账本，既不拟真也不值得维护。它真正需要的是：**当世界已知某个可见状态，尤其是图片要锁定具体衣装时，能在事件时刻提供可证明的版本。**

### 4.1 新 Projection：`AppearanceState v1`

建议归属 World 的 character/life state，但只维护可见连续性；不是图像生成产物，也不是服装库存。

```text
appearance_state_id
entity_revision
valid_from / valid_until?
source_event_ref / source_payload_hash
hair_arrangement?       # loose, low_bun, tied_back…受控目录可扩展
grooming?               # natural, post_activity, prepared…
accessories?            # 仅已成立、可见的饰品
outfit?
  role                  # campus_casual / athletic / sleepwear / swimwear …
  description?          # 仅确有事实时：颜色、材质、搭配
  coverage_facts?       # 如“不透明运动背心”“浴袍完整遮盖”
```

来源只能是：已接受换装/整理行为、可信的可见事件结果、或持续状态的显式更新。生成图、验收观察、用户猜测、关系推断不得回写。

### 4.2 快照策略

- 每次 `MediaEvidenceSnapshotCompiler` 按 **事件逻辑时间** 解析有效版本，冻结至 `/character/appearance_state`。
- 没有 Appearance State 时，图片机保留 `media_local` 回退：活动可以支持泛化 `athletic` 或 `swimwear`，但不得锁定颜色、材质、具体搭配或跨重试衣装。
- 想要求“黑色运动背心”“酒红吊带睡裙”“浴巾/浴袍遮盖”时，必须已有 `outfit.description` 或等价事件证据；否则该机会不可进入对应的具身/私密候选。
- 发型、表情、头向、视线不是 World 控制字段。World 只提供已知发型状态；图片机仍在其候选空间中作稳定随机选择。

这能满足“特殊服装要有事实支持”，同时避免日常照片被衣装系统卡死。

## 5. 缺口二：可见身体状态与私密资格

### 5.1 `VisiblePhysicalState v1`

这是可选、短生命周期的事件状态，不是健康档案，更不是性唤起推断。

```text
state_version
observed_at
source_event_ref / source_payload_hash
cues[]:
  cue_id: perspiration | flush | recovering_breath | damp_hair |
          rain_dampness | drowsiness | fatigue_posture | muscle_tension
  intensity: light | moderate | pronounced
  visible_regions: [face, hair, neck, shoulder, arm, ...]
  evidence_ref
  expires_at?
negative_cues?: [dry, settled]  # 有明确反证时使用
```

世界提供该字段时它优先于图片机的单镜头有限推导；显式空状态/反证也必须冻结。图片机只能使用其 cue 或在字段整体缺失时按现有 resolver 做受限推导，绝不写回 World。

### 5.2 私密资格不是“private 地点”

`exclusive_private` 需要一个**本次机会**的 `PrivateExpressionBasis`，而不是“她在房间里”“关系已经是 lover”或“衣服看起来性感”。建议把它做成 Opportunity authorization 的 scoped evidence，而非永久关系标签：

| basis | World 需要冻结的最小事实 | 合适来源 |
| --- | --- | --- |
| `relational_turn` | `event_id`、`recipient_ref`、已提交私下互动引用 | 已送达媒体/消息后的互动、已接受表达事件。 |
| `recipient_display` | `event_id`、`recipient_ref`、角色当次决定展示自己的事实 | 被接受的 MediaSelection/Expression intent；不是模型 prompt。 |
| `embodied_state` | 上节的非空 `visible_physical_state.cues` | 已提交的活动/恢复/天气结果。 |
| `private_transition` | `event_id/kind` + 已知衣装/遮盖或活动过渡事实 | 洗漱、换装、运动恢复等明确结果。 |
| `shared_ritual` | `event_id`、`recipient_ref`、已建立且仍有效的共同私人线索 | 关系/记忆域已提交事实。 |

对于前三个关系型 basis，根对象的 `recipient_ref` 必须与机会受众一致。所有 basis 必须让图片机看到相同 JSON Pointer，并由其冻结到 MediaPlan。没有 basis 的事件可以是普通人物自拍，不能通过“稍微性感一点”的 LLM 输出升级成私密媒体。

### 5.3 受众上下文

`AudienceContext` 由 World 在机会冻结时给出：

```text
recipient_ref                 # 私密/指向性图片必填
relationship_stage            # 来自已提交关系投影
public_affect?                # 仅角色选择外显的部分
display_bounds?               # 可选的世界边界，而非姿势指令
```

它不包含“嘟嘴、直视、露肩、右偏头”等图像指令；这些是图片机 `Subject Presentation` 的职责。关系阶段也不自动产生照片、张力或投递权。

## 6. 新的机会选择与授权接口

不要把 `MediaOpportunity` 的几十个字段暴露给每个 Deliberation caller。建议新增两个内部值对象和一个窄 Interface。

```text
MediaSelection
  candidate_id
  family
  delivery_mode
  media_privacy_ceiling
  expression_charge_ceiling
  recipient_ref?
  private_expression_basis?
  expiry_policy

CompiledMediaEvidence
  image_event_snapshot
  source_events
  event_snapshot_ref/hash
  evidence_index_digest

MediaOpportunityAuthorizer.authorize(selection, pinned_cursor)
  -> existing MediaPlanningRuntime.freeze_and_authorize(...)
```

`MediaSelection` 可以由 Deliberation 建议，但它只能从已有 candidate ID、受众关系和版本化枚举中选择。Acceptance/Authorizer 负责：

1. candidate 仍为 `available`、未过期且源事件 hash 不变；
2. family、受众、世界事实可见性、媒体隐私/张力上限和 lane 合法；
3. 私密 basis 与 `recipient_ref`、关系阶段、衣装/遮盖事实一致；
4. 用 pinned cursor 编译完整快照，写 sidecar，再原子创建 candidate transition、opportunity、预算和 planning Action；
5. 对同一 candidate/selection 以稳定 ID join；不重选、不重写 snapshot；
6. 将拒绝理由写成可观察的 `skipped/unrenderable`，而不是静默降级到别的事件。

`MediaEvidenceSnapshotCompiler.compile()` 内部可以读取 Location/Activity/Fact/Experience/Relationship 的多个 projection；其外部调用者不需要知道每张照片需要哪些字段。这正是该 Module 的深度和维护局部性。

## 7. 受控随机与高扩展性

World 的随机只处理社会层选择：候选排序、是否今天分享、选择 life/character family、选择低于上限的表达档位、是否主动发送。每一次影响选择的抽样都按 World v2 原则写 `RandomDrawRecorded`。

图片机的随机只处理视觉层：在已冻结机会内，从合法完整候选中稳定抽样机位、人物状态、表演、动作与参考图组合。它已经把 seed 绑定到 `opportunity_id`，因此回放、repair 和 retry 不会换照片语义。

禁止的捷径：

- 用随机抽样升级 privacy/charge/basis；
- 用 activity 名称硬映射固定服装、表情或动作；
- 用当前 projection 覆盖冻结事件时刻的外观；
- 因一次 `NotRenderable` 偷换另一个候选或自动变成日常自拍；
- 用图片验收结果回填 Appearance State、身体状态或关系事实。

## 8. 分阶段待补清单

### P0：接通公开 life-share preview

- [ ] 实现 `MediaEvidenceSnapshotCompiler`，输出 `world-image-event-snapshot-v1` 与 `evidence_index`。
- [ ] 实现 World v2 → `event_media.MediaOpportunity` planner bridge；验证两侧 hash、family、版本和 `planning_request_id` 一致。
- [ ] 将 `MediaPrivacyCeiling` 与 World 的 `PrivacyClass` 分为两个 type，不复用 `private` 表示 `intimate`。
- [ ] 让 ecology 的现有 life-share snapshot 编译出实际活动/地点/物品/环境值，而不只传 ref/hash。
- [ ] 为 `requires_readable_text` 与 `existing_media` 建立 fail-closed / reuse 路由。
- [ ] 保持 preview-only，不开放自动发送。

### P1：候选选择取代 ecology 直冻机会

- [ ] `PhotoCandidate` 完整生命周期：`available → selected → planned/generated/shared` 与 `skipped/unrenderable/expired/failed`。
- [ ] `MediaSelection` Proposal + Acceptance + `MediaOpportunityAuthorizer`。
- [ ] 将当前 ecology 的“直接机会”移到兼容开关；新路径只开 candidate。
- [ ] 按候选新鲜度、视觉性、情绪意义、已有媒体、近期媒体新颖度、预算和用户偏好做评分；评分只是候选信息，是否选择仍由 Deliberation。
- [ ] 为选择层引入记录型随机抽样和每日/间隔频率策略。

### P2：普通角色媒体与稀疏外观连续性

- [ ] `AppearanceState v1` 投影、来源事件、按逻辑时间查询和快照冻结。
- [ ] 活动/地点/参与者能力切片：镜子、同伴在场、公共场所、地域依据、已有媒体来源。
- [ ] 允许 `character_media` 的普通自拍、对镜、打卡、同伴抓拍和局部展示；没有事实时让图片机 `NotRenderable`。
- [ ] 真实照片资产/检验素材的权限与角色身份资产分离，不把纯侧颜作为唯一 identity anchor。

#### P2 implementation contract: character-media fact binder

P2 must not spread a little bit of ``character_media`` eligibility across
ecology, selection, Acceptance and the image bridge.  The planned deep Module
is ``CharacterMediaFactBinder``.  Its external interface is deliberately
small: it discovers source-bound candidates at a pinned projection and
compiles one selected candidate into frozen evidence.  Callers never compose
a capture mode, body region, portrait instruction or snapshot JSON.

```text
committed life event + ImageEvidenceDeclared + sparse AppearanceState
    → CharacterMediaFactBinder.discover(cursor)
    → PhotoCandidate(character_media, frozen contract)
    → existing bounded selection / Acceptance
    → CharacterMediaFactBinder.compile(cursor)
    → image-event snapshot + allowed visual contract
    → image-machine bridge
```

Every character candidate will carry a closed contract whose digest binds its
source refs and hashes, media kind, allowed capture modes and allowed character
visibility.  Candidate kind is part of its ID: a mirror candidate cannot be
silently repurposed as a third-person portrait after the model selects it.

| kind | required committed facts | allowed capture / visibility | fail-closed examples |
| --- | --- | --- | --- |
| `public_checkin` | character present; public location; displayable activity/location; timer or explicitly requested helper | `timer_fixed` or `requested_helper`; identifiable | public place alone; inferred passer-by helper |
| `selfie` | character present; explicit front-camera capability; one displayable visual slice | `character_front_camera`; identifiable | inferring phone ownership from activity |
| `mirror` | character present; `mirror_available`; explicit mirror self-capture capability | `mirror`; identifiable | inferring a mirror from home/bedroom |
| `companion_shot` | character present; named participant present and permitted; that participant has committed capture capability | `known_companion`; identifiable | NPC registration substituted for present participant |
| `body_detail` | character present; displayable accessory/object and non-sensitive body-region fact at the same event time | fact-bound front/rear capture; body detail | accessory-only, injury/health or eroticized crop |

P2 stays public/shareable, ordinary, preview-only and has no recipient or
private-expression basis.  Physical cues, relationship direction and any
private lane remain P3 facts, rather than being smuggled through a suggestive
P2 candidate.  The first implementation slice is the separately committed
``AppearanceState v1``: sparse visible attributes with source event
ref/hash/type, logical validity interval and visibility.  The binder may
freeze it only after a character subject has been explicitly bound by the
candidate; it must never infer the subject from a globally current appearance
state.

### P3：具身和私密媒体

- [x] `VisiblePhysicalState v1` 与短时过期/反证。
- [x] `relationship_media_context` + scoped `PrivateExpressionBasis`：现有
  `embodied_state` 与 recipient-scoped `private_transition` 都绑定 event hash；
  未有来源的 basis 仍拒绝。
- [x] `AudienceContext` 与关系 stage/表达上限的 Acceptance 检查，并由
  `media-selection-acceptance.2` 绑定 P3 authorization/context/basis digest。
- [ ] `exclusive_private`：当前只上线 `alluring_life` preview；`ordinary_life`
  已由 P0/P2 支持，`explicit_reserved` 继续不可渲染。
- [x] 已上线的私密 lane 只允许 self-authored front camera/mirror；adapter 对
  capture、recipient、lane、basis 与 planner result 二次校验。

#### P3 implementation contract: short-lived physical visibility

``VisiblePhysicalState`` is a separate, source-bound, short-lived fact Module;
it is neither a health profile nor an erotic inference.  Its two public
operations are recording a state through a runtime that derives source
coordinates from the ledger, and resolving the version active at a historical
logical time.  Ecology, selection and the image bridge must not each infer
their own cue from activity prose.

- A version has a subject, exact source ref/hash/type, `valid_from` and a
  mandatory short `valid_until` (policy-bounded; default proposal is four
  hours), positive cues and/or structured negative cues.
- Positive cues use one canonical closed vocabulary: `perspiration`, `flush`,
  `recovering_breath`, `damp_hair`, `rain_damp_fabric`, `sleepy_face`,
  `posture_fatigue`, `muscle_tension`, with `light`, `moderate` or `marked`
  intensity.  Injury, illness, intoxication and sexual/arousal semantics are
  outside this fact domain.
- Negative cues (`dry`, `dry_hair`, `settled_breathing`,
  `clear_complexion`, `rested_posture`, `relaxed_muscles`) are explicit
  counter-evidence, not free text.  Reducers reject incompatible positive and
  negative cues in overlapping regions.  A negative-only or explicit-clear
  state remains meaningful and must be frozen as such; it is never a private
  expression basis.
- At snapshot compilation, only the state active at the *selected life
  event's* logical time may be frozen, with the state-record event and its
  anchor both included in the source set and evidence index.  No active state
  means the image machine may take its own bounded visual fallback; a present
  clear state forbids inferring a cue from activity intensity.

P3 private authorization is deliberately a later, separate gate.  A positive
world-fact cue can be one input to a scoped private basis, but neither a
physical state nor a relationship stage creates private media or delivery
authority by itself.

### P4：投递与长期反馈

- [ ] 复用现有 `MediaDeliveryShared → media_delivery_interaction` trigger，在真实送达后才建互动 Bid/线程。
- [ ] 让表达层仅消费 `snapshot + MediaPlan + MediaInspection.observed_summary`，不从 prompt 猜画面。
- [ ] 统计候选→机会→生成→发送→回应的漏斗、失败原因、视觉重复和成本；Evaluator 只做离线诊断，不在线替角色决策。

## 9. 验收场景

| 场景 | World 应冻结的内容 | 期望 |
| --- | --- | --- |
| 雨后校园树叶 life-share | 已提交天气/地点/活动事实 | 图片机可选环境或 trace-only，不出现角色脸。 |
| 新做好的番茄鸡蛋面 | 活动、可见食物、厨房/桌面事实 | 允许生活记录；没有文本依赖。 |
| 打卡人物照 | 公共地点、角色在场、可请求路人或定时条件 | `character_media` 可选 helper/timer；不伪造同伴。 |
| 新项链局部 | 饰品和身体局部均有事件时刻证据 | `body_detail` 合法；不是伤情。 |
| 运动后的日常自拍 | 活动强度或 VisiblePhysicalState | 可有有限汗水/潮红；无私密 basis 时不可进入 exclusive lane。 |
| 只给收件人的私密照 | recipient、关系、expression ceiling、PrivateExpressionBasis、衣装/遮盖事实 | 仅 self-authored；缺任一项 `NotRenderable`。 |
| “在卧室，想拍性感照” | 只有地点，没有过渡/衣装/basis | 不能拍成私密图片。 |
| 朋友发来的合照 | 已有 artifact ref/hash 或同伴在场事实 | 有原图则复用；无原图不可伪造成朋友照片。 |
| 列车/城市打卡 | 地区/城市/交通事实 | 不依赖模型猜招牌、票据或国别。 |

## 10. 需要同步更新的现有文档和测试

实施 P0 时更新：

- `docs/world-v2-refactor-plan.md` 第 6 节：把 `evidence snapshot` 细化为本文件的 `ImageEventSnapshot v1`，并把 media privacy 与 World privacy 分轴。
- `docs/media-machine.md` 第 2、7 节：把当前“世界机待办”替换为本文件的 P0–P4 交接清单，避免两份规格漂移。
- World v2 media tests：snapshot provenance、planner bridge round-trip、机会 exactly-once、旧 ecology 兼容开关。
- 图片机 contract tests：缺衣装事实、无同伴、无地域依据、无 private basis、反证身体状态、freeze/replay 不读当前 projection。

本文件不要求 World v2 立刻实现衣柜、身体档案或私密事件池。P0/P1 的目标只是让已有真实生活事件能可靠地成为公开 life-share；P2/P3 仅在相应事实域真正存在后开放，字段缺失始终是合法且安全的常态。

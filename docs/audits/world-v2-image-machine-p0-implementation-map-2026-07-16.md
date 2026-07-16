# World v2 → 图片机 v5 P0 实施映射

日期：2026-07-16  
范围：只覆盖 [World v2 → 图片机 v5 接入缺口与实施合同](../design/world-v2-image-machine-integration.md) 的 P0。本文是当前代码审计，不是新的产品规格。

## 结论

P0 可在不触碰 render / inspection / delivery 的前提下完成，但不能把现有
`FrozenMediaEvidenceSnapshot` 直接交给 `event_media.MediaPlanner`。

现在已有的 `FrozenMediaEvidenceSnapshot` 只保存来源 event ref/hash 和少量可选坐标。`EventEcologyMediaCandidateRuntime` 还会直接写入 `PhotoCandidateOpened` 与 `MediaOpportunityFrozen`，并把 `complete_candidate.evidence_context` 当作临时 metadata。旧图片机需要的是完整、稳定、可由 JSON Pointer 选择的 `event_snapshot`。若 bridge 在 planning 时重新读取 projection，会破坏 crash recovery 与回放；若它靠 event type 或 `value_ref` 猜具体食物/天气，也会制造没有提交过的视觉事实。

P0 的最小目标应是公开 `life_share` + `preview`：把已提交且 `public/shareable` 的生活事实编译为不可变 `world-image-event-snapshot-v1`，由一个单向 adapter 调用既有 v5 `event_media.MediaPlanner`。P0 不开放人物图、私密 lane、自动发送、衣柜或身体状态写入。

## 现有接缝与缺口

| 层 | 已有实现 | P0 缺口 | 处理方式 |
| --- | --- | --- | --- |
| 证据发现 | `src/companion_daemon/world_v2/event_ecology_media.py` | 发现结果只有 `EcologyCandidate.context`；事实 `value_ref/value_hash` 不等于可读视觉值 | 新 compiler 只读取有明确可展示 payload 的来源；不能解析的 candidate 以 `NotRenderable` / 不开机会处理。 |
| 不可变快照 | `media_v2.FrozenMediaEvidenceSnapshot` + `ImmutableMediaPayloadStore` | 没有版本化图片快照、pointer provenance 或所有叶子来源索引 | 新增嵌入式 `image_event_snapshot`，保留旧字段以兼容已存 sidecar。 |
| 机会与 action | `media_planning_runtime.MediaPlanningRuntime` | 已会校验 sidecar hash、source refs、Action/recovery；没有 v5 planner 实现 | 不修改其选择权；只强化它对 P0 snapshot 版本/候选绑定的校验。 |
| planner worker | `media_planning_worker.MediaPlanningWorker` | 只依赖 `world_v2.media_v2.MediaPlanner` Protocol；默认没有 adapter | 增加 `EventMediaPlannerAdapter`，将已校验快照转换为 `event_media.MediaOpportunity`。 |
| 图片机 | `src/companion_daemon/event_media.py` | `MediaPlanner.plan(opportunity, recent_media=())` 要完整 `event_snapshot`；没有 `lookup(planning_request_id)` | adapter 在自身保存/查询 immutable terminal result，以满足 World v2 的 effect-once Protocol。 |
| render/inspection | `media_execution_runtime.EventMediaExecutionAdapter` | 已能读取 legacy `MediaPlan` JSON | 不改；P0 bridge 产出的 plan sidecar 必须是 `event_media.MediaPlan.to_payload()` 的 canonical JSON。 |

## 推荐的深 Module 与 Interface

新增 `src/companion_daemon/world_v2/media_evidence_snapshot.py`，不把字段拼接散落到 ecology、worker 和 adapter。

```text
MediaEvidenceSnapshotCompiler.compile(request)
  -> CompiledMediaEvidence

EventMediaPlannerAdapter.plan(opportunity, planning_request_id)
  -> MediaPlanningResult
```

### 1. `MediaEvidenceSnapshotCompiler`

**外部 Interface**：

```python
@dataclass(frozen=True, slots=True)
class MediaEvidenceCompileRequest:
    candidate: PhotoCandidate
    category: EcologyCategory
    cursor: ProjectionCursor
    # P0 只能是 public/shareable life_share；调用者不可注入自由 JSON。

@dataclass(frozen=True, slots=True)
class CompiledMediaEvidence:
    snapshot: FrozenMediaEvidenceSnapshot
    snapshot_body: str
    snapshot_ref: str
    snapshot_hash: str

class MediaEvidenceSnapshotCompiler:
    def compile(self, request: MediaEvidenceCompileRequest) -> CompiledMediaEvidence: ...
```

Implementation 内部持有 `LedgerPort`；它首先 `project_at(request.cursor)`，再逐一
`lookup_event_commit(source_ref)`。每个 source 都必须同时满足：

1. 在该 pinned projection 的 `committed_world_event_refs` 中；
2. payload hash 与 candidate 的已记录 ref 一致；
3. event 不晚于 cursor，且属于支持的已提交生活事件；
4. 经过 World visibility 过滤后仍可进入 P0 公开快照。

调用者只给 candidate/category/cursor，不给地点、摘要、对象、提示词或当前投影。这让 compiler 成为唯一的 World→图片字段映射 Module，并避免每个生产者各自“补一点 JSON”。

### 2. `ImageEventSnapshot v1`

在 `src/companion_daemon/world_v2/media_v2.py` 新增严格模型：

```python
class ImageEvidenceIndexEntry(FrozenModel):
    source_event_ref: str
    source_payload_hash: str
    visibility: PrivacyClass

class ImageEventSnapshot(FrozenModel):
    schema_version: Literal["world-image-event-snapshot-v1"]
    event: dict[str, object]
    source: dict[str, object]
    location: dict[str, object]
    activity: dict[str, object]
    participants: tuple[dict[str, object], ...]
    objects: tuple[dict[str, object], ...]
    environment: dict[str, object]
    character: dict[str, object]
    existing_media: tuple[dict[str, object], ...]
    visual_requirements: dict[str, object]
    relationship_media_context: None = None
    evidence_index: dict[str, ImageEvidenceIndexEntry]
```

`FrozenMediaEvidenceSnapshot` 增加可选 `image_event_snapshot: ImageEventSnapshot | None = None`。旧的
`complete_candidate/location/visible_physical_state/recipient_context` 暂时保留读取兼容，不能继续被 P0 planner bridge 当作事实来源。P0 新 snapshot 一律提供 `image_event_snapshot`。

Compiler 必须对每个非容器 leaf 生成 RFC 6901 pointer 的 `evidence_index` 条目。条目所列 event ref/hash 必须属于外层 `source_events`，并在输出前递归核验；不存在可证明来源的值不能输出。`event` 中包括已提交 `event_id/type/status/logical_at`，而活动、地点、对象、环境只能输出可从 source payload 或已有受限投影明确读出的值。`value_ref/value_hash` 本身不是视觉描述，因此不能将 `meal.visible_food` 的 ref 变成“面条”或“番茄鸡蛋面”。

P0 的 `visual_requirements.requires_readable_text` 固定为 `False`，除非有已验证的 `existing_media` artifact；若来源要求可读文字而没有可访问 artifact，则 compiler 返回明确 `MediaEvidenceNotRenderable("readable_text_requires_artifact")`。`existing_media` 只有 `artifact_ref + artifact_hash + accessible/reuse 权限` 都已成立时才输出，绝不把描述伪装成文件路径。

### 3. `EventMediaPlannerAdapter`

新增 `src/companion_daemon/world_v2/event_media_planner_adapter.py`，实现 `media_v2.MediaPlanner` Protocol：

```python
class EventMediaPlannerAdapter:
    async def lookup(self, *, planning_request_id: str) -> MediaPlanningResult | None: ...
    async def plan(self, *, opportunity: MediaOpportunity,
                   planning_request_id: str) -> MediaPlanningResult: ...
```

内部步骤必须固定：

1. 从 `ImmutableMediaPayloadStore` 读取 `opportunity.event_snapshot_ref`，严格校验 ref/hash/content type；`FrozenMediaEvidenceSnapshot.model_validate_json()` 后要求 `image_event_snapshot.schema_version == "world-image-event-snapshot-v1"`。
2. 校验 `source_events == opportunity.source_event_refs`，`evidence_index` 覆盖 planner 可读 leaf，所有 index hash 仍对应 source；缺任何项返回 World v2 `MediaNotRenderable`，不读 projection。
3. 用**显式映射**构造 legacy `event_media.MediaOpportunity`：

   ```python
   event_media.MediaOpportunity(
       opportunity_id=opportunity.opportunity_id,
       family="life_share",
       privacy_ceiling="ordinary",       # P0，不能传 World 的 shareable/personal/private
       event_snapshot=image_event_snapshot.model_dump(mode="json"),
       delivery_mode="preview",
       expression_requirements=(),
       audience_context=None,
       expression_charge_ceiling="none",
       private_expression_basis=None,
   )
   ```

   `family != life_share`、非 preview、非 ordinary media ceiling、任何 receiver/private basis、`explicit_reserved` 都 fail closed。P0 不向 legacy planner 暴露 mutable projection、受众或私密信息。
4. 调用注入的 `event_media.MediaPlanner`。`NotRenderable` 映射为 `media_v2.MediaNotRenderable(opportunity_id, planning_request_id, event_snapshot_hash, reason_code, planner_version)`。
5. 若成功，要求 legacy `MediaPlan.opportunity_id/family/delivery_mode/snapshot_hash` 都匹配；其 `snapshot_hash` 应等于 SHA-256 of `ImageEventSnapshot` canonical JSON（不是 outer sidecar hash），并把完整 legacy plan canonical JSON 放入 `StoredMediaPayload(content_type="application/vnd.world-v2.media-plan+json")`。World v2 descriptor 仍用 outer `opportunity.event_snapshot_hash`，避免混淆两层 hash。
6. 以 `planning_request_id` 为 key 保存 immutable terminal result lookup（生产 adapter 是 provider receipt store；内存 fake 仅供测试）。retry 先 lookup，不能再次调用 LLM。

这里有一个目前不可忽略的差异：旧 `event_media.MediaPlanner` 没有 `lookup` 参数，且其 `plan()` 不接 request id。因此 P0 adapter 需要 own result store / provider-backed receipt adapter。仅用 dict 可通过单进程测试，但不能宣称具备 crash recovery；生产组合必须要求 durable `EventMediaPlanningResultStore`，否则 `WorldV2TurnApplication.compose` 不安装该 planner。

## `MediaPrivacyCeiling` 的最小兼容迁移

`world_v2.media_v2.MediaOpportunity.privacy_ceiling` 当前是 `PrivacyClass`，而旧图片机的 `privacy_ceiling` 是 `ordinary/personal/intimate`。P0 不能继续让 `private` 既表示世界事实可见性又暗示图片亲密上限。

最小可回放迁移：

```python
MediaPrivacyCeiling = Literal["ordinary", "personal", "intimate"]

class MediaOpportunity(...):
    privacy_ceiling: PrivacyClass              # 已有字段，明确改注释为 World visibility ceiling
    media_privacy_ceiling: MediaPrivacyCeiling = "ordinary"
```

`PhotoCandidate.privacy_ceiling` 同样在注释和 test 中称为 World visibility ceiling；P0 不重命名持久字段。新 ecology P0 输出固定 `media_privacy_ceiling="ordinary"`。adapter 只传新字段给旧图片机。日后 P1/P3 才能通过 Acceptance 显式把 World 选择映射为 `personal/intimate`。

这不需要修改已经落盘的 `WorldEvent` bytes，也**不应**为 sidecar 写 event upcaster：sidecar 已按 ref/hash 不可变，历史 snapshot 仍可被读取为 legacy（但 P0 adapter 应拒绝为图片机规划）。新增带默认值的 ledger payload 字段会改变重放后的 projection shape/fixture hash；应作为有意的 projection contract 变更，运行完整 replay baseline 后更新 hash，不能偷偷把旧 opportunity 重新冻结成 v1 snapshot。

## 对现有 ecology 的最小改造顺序

1. 在 `event_ecology_media.py` 的 discovery 阶段保留来源选择、频率和 deterministic candidate ID；不要让它生成 prompt。
2. 把 `EcologyCandidate.context` 改成内部 `EvidenceSelection`（类别、source refs、可选受限 projection coordinates），而不是直接成为 sidecar 的视觉事实。
3. `drain_once` 在写 sidecar 前调用 compiler。compiler 返回 `NotRenderable` 时：P0 推荐记录 candidate 为可观察的 skipped/unrenderable 状态；若现有状态机尚未支持，则先不创建 opportunity，并写一个明确、可重放的 suppression record。**不能**以空泛 `life_share` fallback 创建机会。
4. compiler 成功时，仍可由目前 ecology 直冻 preview opportunity 作为 P0 兼容通道，但 `MediaOpportunity.media_privacy_ceiling="ordinary"` 且 snapshot 必须是 v1。P1 才把“candidate → selection → authorizer”迁入 Deliberation。
5. 在 `production_turn_application.py` compose 中，只有同时注入 legacy `event_media.MediaPlanner` 和 durable result store 时才构造 `EventMediaPlannerAdapter` 并传入 `MediaPlanningWorker`。默认无配置保持 `unavailable`，不能倒退到旧 Engine/world 路径。

## 精确测试拆分（可并行）

| Lane | 文件 | 断言 | 与其他 lane 的依赖 |
| --- | --- | --- | --- |
| snapshot schema/compiler | 新建 `tests/world_v2/test_media_evidence_snapshot.py` | source/hash pin、public/shareable filter、每个 leaf 有 index、pointer escaping、readable-text/no-artifact、unknown fact value ref fail closed、同 cursor byte-stable | 只依赖新 compiler/models，可先行。 |
| ecology integration | `tests/world_v2/test_event_ecology_media.py` | 活动/地点/环境/真实对象值产出 v1；仅 ref/hash 的餐食不产出虚构描述；sidecar failure 零 ledger writes；旧兼容 snapshot 无 bridge | 依赖 compiler。 |
| bridge contract | 新建 `tests/world_v2/test_event_media_planner_adapter.py` | outer/inner hash 区分；legacy opportunity 映射 ordinary/preview/life_share；legacy plan round-trip；NotRenderable 映射；bad pointer/index/hash/no snapshot fail closed；lookup 防止二次 planner 调用 | 只依赖 models + adapter；可以与 ecology 修改并行。 |
| runtime wiring | `tests/world_v2/test_media_planning_worker.py`、`test_production_turn_application.py`（若已有相应组合测试则扩展） | injected adapter 才执行；无 durable result store 保持 unavailable；planning Action exactly-once/sidecar exactness | 依赖 bridge。 |
| replay/schema gate | `tests/world_v2/test_media_v2_planning.py`、`tests/world_v2/test_upcasting.py`、`scripts/verify_world_v2_scenarios.py` | 旧 event payload 仍可重放，旧 sidecar 仅 legacy；P0 snapshot replay hash 稳定；全 suite manifest 有意更新 | 在所有生产改动后。 |

图片机一侧应补 `tests/test_event_media.py`：assert planner 只能使用 `evidence_index` 指定 pointer（目前 `_snapshot_evidence_pointers` 会枚举所有字段），以及 `requires_readable_text` 仅可走实际 artifact reuse。若不先修改图片机读取策略，compiler 虽有 index，planner 仍会看到不该选择的容器/leaf，P0 的 provenance contract 不完整。

## 必须保持的回放与安全不变量

1. outer sidecar body/hash、inner `ImageEventSnapshot` canonical bytes/hash、legacy plan snapshot hash 是三种不同对象；每个接口必须声明使用哪一种。
2. planner/recovery/render 一律读取 sidecar，不读取当前 `LedgerProjection`；同一个 `planning_request_id` 只得到一个 terminal result。
3. snapshot 编译读取的 event body 必须与 `source_events.payload_hash` 精确相等；`value_ref` 不是可自由 resolve 的权限。
4. P0 只能 `life_share + ordinary + preview`；任何人物可识别、受众、表达张力、私密 basis、automatic delivery 都拒绝，而不是降级成普通照片。
5. 旧 ecology 已存在的 sidecar 不重写。需要 v1 的新 candidate 得到新 deterministic ID（建议将 compiler contract version 纳入 ecology catalog/candidate identity），否则相同 id 会尝试绑定不同 bytes 并正确报错。
6. 因新增默认机会字段导致的 scenario manifest 变化须从干净 archive 重建；不以运行时“兼容”掩盖 hash 漂移。

## 不属于 P0 的工作

- `MediaSelection` / `MediaOpportunityAuthorizer` 与记录型随机；这是 P1，不能在 bridge 中暗中做选择。
- Appearance State、VisiblePhysicalState、`AudienceContext`、PrivateExpressionBasis 与 any `character_media`；分别属于 P2/P3。
- 自动投递；P0 保持已有 preview-only action/approval 纪律。
- 从 legacy `event_media.py` 把自由图片结果回写 World 事实；验收观察只进入现有 inspection/preview 链。


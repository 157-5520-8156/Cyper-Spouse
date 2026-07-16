# World v2 → 图片机 v5 P0 实施闭环

日期：2026-07-16
范围：`world-v2-image-machine-integration.md` 的公开 `life_share` P0。

## 已实现的闭环

```text
已提交来源事件 + pinned ProjectionCursor
  → MediaEvidenceSnapshotCompiler
  → immutable world-image-event-snapshot-v1 sidecar
  → explicit direct-preview compatibility flag
  → MediaOpportunity (World visibility + media privacy 分离)
  → EventMediaPlannerAdapter
  → SQLite idempotent result receipt
  → existing MediaPlanning Action / preview chain
```

- `MediaEvidenceSnapshotCompiler` 只读取 `project_at(cursor)` 与精确 hash
  匹配的已提交事件；不会从 `value_ref/value_hash` 解引用或补写视觉描述。
- `ImageEventSnapshot` 的每个 planner-readable leaf 必须有 RFC 6901
  `evidence_index` 条目；条目必须绑定 outer source event ref/hash。
- bridge 不读取活动投影；只读取 sidecar，校验 outer hash、inner canonical
  hash、source set、逐叶 provenance 和 P0 lane。
- 旧图片 planner 得到的是 `allowed_evidence_refs` 闭集。可解析但未索引的
  JSON Pointer（包括 `/evidence_index/...`）会被拒绝。
- bridge 仅接纳 `life_share + public/shareable World visibility + ordinary
  media privacy + preview`；人物图、受众、私密 basis、automatic delivery 一律
  `NotRenderable`。
- result store 以 `(world_id, planning_request_id)` 持久化 immutable terminal
  receipt；缺少 durable store 时不安装 bridge，worker 保持 unavailable。
- `EventEcologyMediaCandidateRuntime` 默认不再隐式写入 direct preview。
  旧的 candidate → opportunity 直冻路径只有
  `EcologyPolicy.direct_preview_compatibility=True` 时才启用，并且 compiler
  失败时零 ledger 写入、绝不以泛化图片降级。

## 当前有意保留的边界

- 可读文字与现有媒体没有 provider-backed artifact lookup 时仍 fail closed；
  P0 不把 artifact ref 伪造成本地路径，也不生成文字图片。
- `MediaSelection` / `MediaOpportunityAuthorizer`、记录型随机、人物图片、
  AppearanceState、PrivateExpressionBasis、automatic delivery 均不属于此 P0；
  它们必须在 P1+ 通过 Deliberation/Acceptance 接入，而非藏在 bridge 内。
- 已落盘的 legacy sidecar 不重写。没有 v1 `image_event_snapshot` 的机会不
  能进入这条 bridge。

## 验证证据

- snapshot/compiler、bridge、durable store、生态与 production composition 的
  聚焦测试通过。
- 离线冻结场景校验通过，manifest hash：
  `9be374805c19fb3302287ca375310eb81830fd89934f1cacf5708ff70cf2c02e`。

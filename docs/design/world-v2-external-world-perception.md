# World V2 外界感知系统设计

状态：Draft，深化 [ADR-0013](../adr/0013-external-world-perception-hub.md)，尚未授权生产接源。

实现进度（2026-08-03）：Phase 1 的隔离 sidecar、immutable signal revision、纠错 lineage、
TTL/normalized retention、raw CAS、保守 cluster、FTS/可注入 embedding、health、录制回放
Adapter、hostname allowlist 的 RSS/Atom 与 route/item-host allowlist 的本机 RSSHub Adapter
已落地。当前来源审计表为空，未接生产 scheduler、
角色模型、World V2、Life Ecology 或发送链；Phase 2 及以后仍未实现。

## 1. 目标与非目标

本系统让角色像一个生活在现实信息环境中的人一样，通过公共预警、新闻、社交媒体、
兴趣订阅、场所媒介、NPC 转述或主动搜索接触外界。它只扩大角色可能接触到的现实，
不替角色决定注意、相信、在意、行动、形成情绪或联系用户。

权威链固定为：

```text
ExternalSignalRevision（某来源在某时声称了什么；sidecar）
  -> PerceptionWindow（这次有机会接触哪些精确 revision；sidecar）
  -> Character attention（角色模型可选择零条、一条或多条）
  -> ExternalPerceptionRecorded（她确实接触到了什么；V2）
  -> Life Influence（后续模型可以使用，也可以不使用）
  -> Affect / Plan / Experience / Memory / Social Initiative（各自独立决定）
```

明确不做：

- 新闻关键词到情绪、剧情或消息的映射；
- “地震必须问候”“热点必须讨论”等行为规则；
- 把来源报道、RSSHub 排名或跨源 cluster 当成互联网真相；
- 把角色变成全知新闻摘要器；
- 把海量抓取结果写入 V2 不可变账本；
- 把用户提到的地点自动解释为用户当前位置；
- 复用现有图片/音频 `perception_*` 竖井承载外界信息。

## 2. 接口方案比较与选择

设计比较了三种方向：

1. `refresh / consider / status`：权责清楚，但 scheduler 必须理解 opportunity；
2. `synchronize / prepare / bind / health`：扩展性最高，但暴露了易被误用的调用顺序；
3. `advance_once / health_snapshot`：普通 caller 最简单，内部必须有强健康投影避免黑盒。

选择第三种作为外部 Seam，并吸收第二种的冻结窗口与证据图谱作为内部实现。这样普通
scheduler 不会因增加一个来源而改变；测试仍可从同一 Interface 覆盖采集、修正、候选、
模型注意、CAS、重启恢复和下游 wake。

```python
class WorldPerceptionHub(Protocol):
    async def advance_once(
        self, *, observed_at: datetime
    ) -> PerceptionAdvanceResult: ...

    def health_snapshot(self) -> PerceptionHealthSnapshot: ...

    async def aclose(self) -> None: ...
```

`advance_once()` 每次最多推进有限 acquisition work 或一个 attention attempt，并返回：

```python
class PerceptionAdvanceResult(FrozenModel):
    status: Literal[
        "idle",
        "progressed",
        "window_wait",
        "attention_no_selection",
        "shadow_selected",
        "perception_committed",
        "retry_wait",
        "joined_existing",
        "deferred_visible_turn",
    ]
    progressed_units: int
    committed_perception_count: int
    next_wake_at: datetime | None
    more_due: bool
```

`next_wake_at` 只是调度提示；每次执行仍从 durable sidecar 和最新 V2 head 重建状态。
普通 caller 不传来源列表、用户位置、兴趣、cluster、模型或 ledger writer。

紧急 provider push 另有 source-facing 的窄 Seam：

```python
class PerceptionEvidenceIngress(Protocol):
    async def accept(self, delivery: SourceDelivery) -> IngressReceipt: ...
```

Ingress 只验证并持久化 provider delivery，然后唤醒 Hub。HTTP/Webhook 层无权调用角色
模型或写 V2。Pull 与 push 不强行共用一个假统一网络接口，但最终都产生同一种 immutable
source report envelope。

## 3. 模块边界

建议新 package 使用 `world_v2/external_world_perception/`。仓库现有
`world_v2/perception.py`、`perception_result_context.py` 等专指“分析用户发来的图片或
音频”，不能扩展含义。

```text
WorldPerceptionHub.advance_once
├── AcquisitionPlanner
├── RawEvidenceRegistry
├── SignalRevisionNormalizer
├── RevisionAndCorrectionGraph
├── ConservativeClusterIndex
├── OpportunityAccumulator
├── CandidateRetriever
├── PerceptionWindowCompiler
├── PinnedAttentionTurn
├── ExternalPerceptionAcceptance
├── LifeInfluenceOutbox
└── PerceptionHealthProjection
```

这些可以是内部 Module 和内部 Seam，但不进入普通 caller 的 Interface。删除 Hub 后，
游标、许可、TTL、修正、聚类、位置隐私、有限注意、模型审计、CAS 和恢复复杂度会重新
散到 scheduler、Life Ecology、Context 与每个来源中，因此该 Module 具有实际 Depth。

## 4. 来源 Adapter 与 RSSHub 的位置

所有 pull 来源实现内部 `ExternalSignalSourcePort`：

```python
class ExternalSignalSourcePort(Protocol):
    source_id: str

    async def fetch(
        self,
        *,
        after: SourceCursor | None,
        observed_at: datetime,
        deadline_at: datetime,
        limit: int,
    ) -> SourcePage: ...
```

生产 Adapter 与录制回放/故障 Adapter 构成真实 Seam。首期来源族：

- `RssHubPullAdapter`：广覆盖的普通网站、社交媒体、兴趣内容与趋势发现；
- `RssAtomPullAdapter`：来源自己提供的 RSS/Atom；
- `AuthorityAlertAdapter`：CAP、地震、气象和公共安全结构化来源；
- `WeatherAdapter`：结构化天气与预警；
- `AuthorizedSearchResultAdapter`：只接收角色已经授权的 read-only search Action 回执；
- `NpcReportAdapter`：只从 exact settled NPC 对话/经历形成 hearsay signal。

RSSHub 适合作为第一批广覆盖 Adapter，而不是统一事实层：

- 自建实例仅监听本地网络，并固定容器 digest；
- 上线前完成 AGPL-3.0 部署与修改分发方式审查；RSSHub 与本项目保持进程和代码边界；
- route 必须 allowlist，不允许 Hub 根据模型输出拼任意 RSSHub path；
- 每个 item 同时保存 RSSHub transport identity、上游平台/发布者 identity、upstream item
  identity、抓取时间和 payload hash；
- 来源权威归上游发布者和 claim scope，不归 RSSHub gateway；
- route 登录、Cookie、反爬、限流和页面变更分别进入 source health；
- 不用 RSSHub 作为地震、气象或公共预警的唯一权威来源；
- 不向 RSSHub 或上游平台发送用户、角色或 NPC 的精确位置；
- 默认只保留许可允许的标题、摘要、时间、URL、来源与 hash，不默认保存全文。

RSSHub 官方项目持续维护大量来源路由，官方部署示例也把 Redis 缓存、Browserless/
Chromium 与 health check 作为可选生产依赖。这说明它适合作为可替换的聚合 transport，
也说明 route 可用性必须被本项目独立监测，不能把“HTTP 200”当成来源新鲜或可信。

## 5. External Signal 与证据图谱

每个 `ExternalSignalRevision` 至少冻结：

```text
signal_id + revision
source_id + upstream_item_id
gateway_ref + upstream_publisher_ref
source_payload_hash + normalized_hash
source_policy_revision
headline + licensed_summary + canonical_url
occurred_at / published_at / updated_at / observed_at / expires_at
geometry + entities + topic material
source-provided certainty (if any)
normalization uncertainty
supersedes / correction_of / cancellation_of
```

系统不得把不同性质的 confidence 压成一个“真相分数”。来源自己的 certainty、解析置信度、
地理消歧置信度和角色自己的相信程度分别保存。

Cluster 只表示“这些 revisions 可能讨论同一外部对象或事件”，用于去重与召回。它不能：

- 合并出一条新的系统事实；
- 抹掉来源冲突；
- 代替 exact signal revision 成为事实引用；
- 把 feed 中消失的 item 自动解释成撤稿。

相同 upstream identity 且内容变化产生新 revision。只有来源明确表达 update、cancel、
retraction 或 correction 时才建立对应 lineage；普通 feed 滑出窗口只表示当前抓取未包含，
不表示原声明被撤销。

## 6. 有限注意与 Perception Window

普通信号先在本地做 TTL、来源许可、时间、地理、实体、FTS、embedding 和已曝光过滤。
随机性只决定哪一组可访问候选在什么时候获得曝光机会；不能决定角色注意或行动。

内部冻结：

```python
class PerceptionWindow(FrozenModel):
    window_id: str
    attention_attempt_id: str
    pinned_world_cursor: ProjectionCursor
    policy_revision: str
    generated_at: datetime
    expires_at: datetime
    candidates: tuple[PerceptionDossier, ...]
    candidate_set_hash: str
    exposure_draw_ref: str
    deployment_mode: Literal["shadow", "live"]
```

每个 dossier 保留 exact revisions，而不是一段 Hub 自创的新闻摘要：

```python
class PerceptionDossier(FrozenModel):
    candidate_ref: str
    exact_signal_revisions: tuple[str, ...]
    corrections: tuple[CorrectionEdge, ...]
    source_disagreements: tuple[SourceDisagreement, ...]
    accessible_channels: tuple[ChannelProof, ...]
    geo_time_relation: GeoTimeRelation
    model_visible_material: tuple[LicensedEvidenceView, ...]
    evidence_digest: str
```

模型固定看到当前 Current Self State、Situation、相关 Life Arc/Plan、来源明确的兴趣与近期
经历、隐私降级后的位置关系、候选、来源时间、冲突和可用 Perception Channels。模型自由
选择零条、一条或多条，并自由形成理解、怀疑、感受或联想。请求中不得包含 motive enum、
行为建议、情绪矩阵、联系用户建议或问题模板。

模型输出只需结构化绑定：

```text
selected candidate_ref(s)
exact signal revision ref(s)
selected channel_ref(s)
character-authored subjective summary
character-authored epistemic notes
attended Context refs
```

自由文本不证明来源陈述客观正确。非法 ref、缺失渠道或越界引用时，把 exact 候选、可用
渠道和精确错误交给同一角色模型受约束重选一次；仍非法是技术失败，不得记作“她没看到”。

## 7. Perception Channel

Channel 解释她为何可能接触某条信号，但不证明她实际注意。首期 capability 可包括：

- 系统/公共预警通知；
- 当前可用设备上的公开在线浏览或已建立订阅；
- 当前活动/地点有来源证明的屏幕、广播或公告；
- exact settled NPC 转述；
- exact authorized search Action 的结果。

能力表示“可以尝试”，不是“历史上经常浏览”。不存在的订阅、设备使用、NPC 对话或
地点媒介不能为了使用候选而回填。以后由角色决定新增或退订某个信息渠道时，应通过独立
Character Decision 与 accepted capability change 表达，不能由 RSSHub 配置反向改写角色。

## 8. 调度与人类节奏

普通来源：

1. Adapter 按自身速度增量抓取；不对每个 item 调模型。
2. 第一条 eligible signal 打开一个短合并窗口；窗口内新 revision、转载和 correction
   加入同一机会，不重复抽签。
3. ready window 优先搭载已有 heartbeat/Life Ecology wake；可见 turn 正在运行时让路。
4. 每次 `advance_once()` 最多一个 attention attempt，`more_due=True` 时重新唤醒。
5. 模型选择 none 或 shadow selection 只在 sidecar 终结，不写空 V2 tick。

紧急来源：

- 只有配置中被授予紧急调度权的结构化权威来源，才能根据其原生 urgency、severity、
  certainty 和 area 打开低延迟机会；
- 网页关键词、RSS 分类、标题语气和模型摘要不能自报紧急；
- 紧急只提高采集、核验和 attention 调度优先级，不要求角色注意、担心或发消息；
- 独立公共安全产品告警不能依赖角色消息承担。

普通窗口的具体时长与每日 attention 预算属于部署参数，不冻结为角色行为规律。首期 shadow
应测量候选重复率、模型空选择率、成本和自然度后再定；设计上保证一次窗口最多一次正常
模型调用，结构错误最多一次受约束重选。

## 9. Stable identity、CAS 与重启

```text
attention_attempt_id = H(
  world_id,
  opportunity_id,
  candidate_snapshot_hash,
  exact_world_cursor,
  attention_policy_revision,
  deployment_mode_revision
)

perception_event_id = H(
  attention_attempt_id,
  exact_signal_revision_ref,
  selected_channel_ref
)
```

Sidecar attempt 使用 `open -> claimed -> terminal` lease：

- live lease 被其他 worker join；
- expired lease 按稳定 retry ordinal reclaim；
- 模型完成但 V2 cursor 变化时，旧结果标记 `superseded_cursor`，不得移植；
- V2 已提交但 sidecar 未确认时，用确定性 event ID 对账后收敛；
- shadow attempt 永远不能在切换 live 后转正，live 必须基于新 mode identity 和当前 cursor
  重新考虑；
- correction 在窗口冻结后到达不回写旧窗口。角色可能确实看到了旧报道；更正进入下一机会。

若角色选择零条，sidecar 记录 effect-once terminal，避免重启后针对同一机会重复调用模型。
该记录只需保留到 opportunity 永久过期并越过恢复窗口，不需要进入 V2。

## 10. V2 接入

首个 live delivery 才新增 authority，满足 Producer-First Authority。建议同一 CAS batch：

```text
ModelResultRecorded
ExternalSignalSnapshotAdopted (每个被选 exact revision)
ExternalPerceptionRecorded (每个被选 exact revision)
TriggerProcessOpened (整批一次 Life Influence wake)
```

如果现有 batch/event 约束更适合把 snapshot 嵌入 perception payload，可以在实现 spike 中
比较；无论采用一事件还是两事件，冷重放必须无需重新联网，且 snapshot 不能比模型看到的
许可材料更少。

`ExternalPerceptionRecorded` 最小内容：

```text
perception_id / actor_ref / attention_attempt_id
pinned cursor / encountered world time / observed wall time
channel binding
exact signal revision ref + frozen licensed source snapshot
candidate snapshot hash
character-authored subjective summary and epistemic notes
attention model audit ref + result hash
privacy class
```

事件只证明“她通过某渠道接触到该 revision”。下游只获得 `LifeInfluenceView`：exact refs、
角色注意到的内容、来源不确定性/修正 lineage、channel 和当前相关情境；不附带 proposed
behavior。

- World Author 可把它作为现实环境材料提出开放机会；
- Character Model 决定是否参与、求证、改变计划或形成长期方向；
- Affect 模型自行 appraisal；
- Memory 只巩固实际感知、理解和后续经历；
- Social Initiative 只得到一次情境刺激，角色仍选择 now/later/silent；
- External Signal 永远不能直接唤醒上述消费者。

## 11. 存储、许可与安全

首期使用独立 SQLite sidecar 与内容寻址 raw evidence 目录，不与 V2 主库共用增长曲线：

```text
raw_evidence
signal_revisions
revision_edges
cluster_revisions
source_checkpoints
source_attempts
attention_windows
attention_attempts
adopted_revision_pins
life_influence_outbox
health_rollups
```

sidecar 与 V2 不能假装跨库事务。正确顺序是：sidecar 冻结 attempt -> V2 确定性幂等 CAS
-> sidecar reconcile。V2 commit 是角色感知事实的最终权威。

每个 source policy revision 分别声明 fetch、raw cache、derive summary、embed、model exposure、
quote、durable snapshot 与 retention 权限。无法合法冻结足够冷重放证据的来源可以用于
shadow 质量研究，但不能进入 live。

安全硬边界：

- feed/HTML/标题/评论全部是 untrusted evidence，永不成为 system/developer instruction；
- 移除脚本、事件处理器、隐藏节点和危险 URL；剩余自然语言仍按带边界的引用数据传输，
  不宣称能够“清洗掉”文本中的 prompt injection；
- RSSHub route allowlist、网络 egress allowlist、响应体/重定向/媒体大小上限与 SSRF 防护；
- Cookie、token 和用户授权不入账本、不进模型、不写普通日志；
- 精确用户/NPC 位置只在本地计算，模型只得到来源、用途、置信度、有效期和必要的粗粒度关系；
- raw 删除不影响已采用 snapshot；V2 引用完成前通过 adopted pin 阻止清理。

初始 retention 值应是配置而非领域常量。Shadow 可从 raw 7 天、normalized 30 天、未产生
World effect 的 attention attempt 7 天开始测量，再根据许可、容量和恢复需要调整。

## 12. 检索、性能与成本

- refresh 不调用 LLM；ETag/cursor、解析、精确去重、TTL、时间/地理过滤均本地执行；
- title + licensed summary + entity + time + geo 优先，全文默认不 embedding；
- 复用现有 embedding provider route/HTTP pool 的配置经验，但使用独立 cache、预算、circuit
  和 shutdown lease，避免外界感知故障压制聊天 Recall；
- embedding 不可用时降级到 FTS、entity、geo 和 time，不突破 TTL、许可或渠道；
- clustering 只在时间/地理/entity 邻域增量执行，不全表重算；
- Perception Window 建议 8–12 个 dossiers、8–16 KiB 模型可见材料；
- 本地 candidate preparation warm p95 `<100 ms`、cold p95 `<300 ms`；
- bind/acceptance 前本地校验 p95 `<20 ms`；
- 权威 push 入可检索 index p95 目标 `<2 s`；RSSHub/普通 social 不承诺实时；
- V2 增长与实际感知次数近似成正比，而不是抓取量。

角色 attention model 使用角色模型身份，但拥有独立后台 circuit、suppression、任务和 shutdown
lease；可以共享供应商配置和 HTTP pool。它不是廉价分类器：检索只决定可访问候选，角色
模型才决定注意与理解。

## 13. Health

`health_snapshot()` 读取紧凑 sidecar 投影，不抓取、不重试、不调用模型。至少区分：

- source healthy + no new signal；
- source stale/unavailable/rate-limited/malformed；
- signals ingested / revised / corrected / expired；
- eligible signals but no candidate；
- candidates privacy/channel/TTL filtered；
- attention window waiting / ready / claimed / retry_wait；
- model selected none；
- shadow selected；
- External Perception committed；
- model technical failure / invalid selection；
- correction arrived but not yet perceived；
- sidecar/index bytes、24 小时增长和 V2 adopted bytes；
- duplicate suppression、cluster conflict、cursor supersession 和 outbox backlog。

不得把“来源没数据、检索没候选、角色没注意、角色感知但没行动、供应商失败”合并成沉默。

## 14. 分阶段交付

### Phase 0：来源与许可审计

- 明确首批 RSSHub route、上游发布者、登录依赖、抓取/摘要/embedding/模型处理/持久化许可；
- 选择至少一个权威结构化预警来源，但不接生产；
- 确定用户位置默认用途与粒度；
- 冻结 `ExternalSignalRevision`、source policy 和 health 合同。

### Phase 1：纯 sidecar ingestion

- 实现 Hub、fixture Adapter、RSS/Atom Adapter 与 RSSHub Adapter；
- raw CAS、revision/correction、TTL、保守 cluster、FTS/embedding、health；
- 不新增 V2 event，不接 Life Ecology，符合 Producer-First Authority。

### Phase 2：Shadow attention

- 编译 Perception Window，调用角色模型，记录 sidecar model result；
- Shadow 类型禁止 V2 commit；
- 评估重复率、空选择率、误相关、位置误报、prompt injection、成本和自然度。

### Phase 3：只记录实际感知

- 同一 delivery 新增 producer、acceptance、reducer、Context consumer 和 health；
- live 仅提交 External Perception，不唤醒 Affect、Life 或 Social Initiative；
- 验证冷重放、CAS、重启、账本增长和 correction。

### Phase 4：开放 Life Influence

- 整批感知只打开一次 Life Ecology wake；
- Affect、Memory、World Author 各自通过已有模型 authority 自由处理或 no-op；
- 观察生活是否被现实丰富，而不是演化成热点追逐。

### Phase 5：Social Initiative 与主动搜索

- 感知作为情境刺激进入主动考虑，不直接发送；
- 角色可通过 read-only Action 自己决定搜索，结果重新进入 Hub；
- 真实多轮观察地震关切、流行话题、普通无聊内容、错误报道和更正后的自然表现。

## 15. 验证矩阵

- RSSHub route 正常、无新内容、登录失效、429、HTML 变化、重复 GUID、同 URL 修订；
- 权威 update/cancel/retraction、跨源冲突、误 cluster、拆簇与 feed item 消失；
- 精确用户当前位置、过期位置、常住地、家乡、旅行计划、只提到地点和同名城市；
- prompt injection、超大 payload、重定向、SSRF、恶意 HTML、Cookie/token 日志泄漏；
- attention 选择 none、多条、非法 ref、虚构 channel、一次重选、双模型失败；
- window 冻结后来源修正、模型期间 cursor 变化、CAS 冲突、commit 后崩溃、sidecar 对账；
- shadow 不可转 live、重启不重抽、同 opportunity effect-once；
- External Perception 不直接创建 Affect、Plan、Experience、Memory 或消息；
- 后续各模型可因同一感知产生不同结果或 no-op；
- 24 小时 sidecar/V2 增长、attention 调用数、candidate latency 和 source staleness。

## 16. 实施前仍需确认

1. 首批允许的 RSSHub routes 与上游内容许可；
2. 中国境内地震、气象、公共预警的可持续权威结构化来源；
3. 用户位置用于灾害匹配的默认粒度、用途授权和有效期；
4. Shadow 初期是否完全不读取用户位置，先只验证角色自身所在地与兴趣；
5. raw evidence、normalized signal 和无效果 attention attempt 的容量预算与保留期；
6. 首批角色可用 Perception Channels，哪些已有 World 证据，哪些需要先补 capability；
7. Phase 3 是把 licensed snapshot 嵌入 `ExternalPerceptionRecorded`，还是同批采用独立
   `ExternalSignalSnapshotAdopted` 事件。

这些选择会影响许可、隐私和不可变事件形状；在确认之前，ADR-0013 保持 proposed。

## 17. 参考

- [RSSHub 官方仓库与许可证](https://github.com/DIYgod/RSSHub)
- [RSSHub 官方 Docker Compose（Redis、Browserless 与 health check）](https://github.com/DIYgod/RSSHub/blob/master/docker-compose.yml)
- [OASIS Common Alerting Protocol 1.2](https://www.oasis-open.org/standard/cap/)
- [ADR-0010：受控的高随机](../adr/0010-controlled-high-variance-character-agency.md)
- [ADR-0013：External World Perception Hub](../adr/0013-external-world-perception-hub.md)

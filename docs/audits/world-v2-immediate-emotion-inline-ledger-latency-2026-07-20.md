# 互动评估内联通道账本延迟根治报告（2026-07-20）

## 结论一句话

immediate-emotion 内联相位的分钟级延迟不是"投影太慢"，而是 `observation_events_at`
在校验 boundary commit 时**每回合从创世全量重放一次账本**（3.2 万事件，O(全史)；
生产观测 80~797s，画像重现 264~455s）。修复后，同一生产副本上内联相位（模型时
间外）**p50 259.9ms / p95 262.4ms / max 327.5ms**，达标（目标：相位 p95 < 5s，
账本开销 < 1s）。

## 生产实锤与画像方法

- 生产日志（`logs/napcat.err.log`）：17:35:10 回合 complete 152.7s，其中
  `immediate_emotion_ms=142172.7`，pinned-turn 内部 model_ms=13490 / record_ms=13587，
  即 **~127.7s 消耗在 `_run_one` 进入 `audit_observation` 计时段之前的账本调用上**。
  07-19 的历史 >15s 回合（audit_ms 80s~797s）全部同构。
- 画像方法：复制 `output/bdv-shadow/companion-shadow-copy.sqlite`（生产副本，
  head=(14053, 18156, 32209)）到 `output/perf-lab/profile-copy.sqlite`，用
  `output/perf-lab/profile_inline.py` 以真实组件（`InteractionAppraisalTriggerRuntime`
  + `PinnedTurnCompiler` + `ImmediateEmotionProposalWorker` + 真实 Context 编译器，
  仅模型为 no_change stub）重放"ingress 批（Observation+Opened+Claimed）→
  run_observation"的完整内联路径，代理层逐调用计时。生产库全程只读。

## 修复前逐调用画像（生产副本，3 回合）

| 调用 | 次数/回合 | 修复前耗时 | 说明 |
| --- | --- | --- | --- |
| `ledger.observation_events_at` | 1 | **264 000 ~ 455 000ms** | 占内联相位 99.9%；`total_replay_calls` 每回合 +1 |
| `ledger.commit`（ingress 批 / 审计批） | 2 | 80 ~ 218ms | 增量编码全命中（`encode full=0`） |
| `ledger.commit_at_cursor`（TriggerProcessCompleted） | 1 | 84 ~ 161ms | |
| `ledger.lookup_event_commit` | 213 | 冷 236ms / 热 ~2ms（合计） | Context 引用解析，命中已验证缓存 |
| `ledger.project()` | 7 | 合计 <2ms | head 缓存全命中 |
| `ledger.project_at()` | 8 | 合计 <1ms | 全部等于 head，走快路径 |
| `ledger.resolve_committed_event_refs` | 3 | 合计 10 ~ 18ms | |

微基准（同副本 head 状态，16MB / 14053 条 world refs / 6135 条 clock 转换）：

| 项目 | 耗时 |
| --- | --- |
| 全史重放（`project_at` 历史 cursor 首次） | 438 062ms |
| 外部 epoch bump 后冷校验（`_refresh_verified_external_history_locked`） | 288 091ms |
| `make_projection` 全量（语义哈希 297ms 含在内） | 629ms |
| head 状态解码（join+validate） | 1 543ms |
| `_state_hash` 全量 | 423ms |
| `reduce_event(ClockAdvanced)`（两次 O(n) 全史校验） | 47.6ms |

据此排除了两个候选方向：同回合投影已经全部复用（head 缓存与 `project_at` 快路径
命中率 100%，无需在 runtime/pinned_turn 传引用）；认领无 CAS 重试放大（ingress 批
已带 Claimed，`_claim_or_reclaim` 直接返回，不提交）。

## 根治项

### 1. `observation_events_at` boundary 校验绑定已验证前缀（真凶）

`_observation_events_at_locked` 对 boundary commit 调 `_verified_commit_locked`
时没有传 `verified_prefix_cursor`，落入"从创世重放到前驱"的兜底分支——
`lookup_event_commit` 早已因同样问题修过（绑定已验证 head 前缀），这条路径漏修。
入口处 `_refresh_verified_external_history_locked()` 已把本进程绑定到当前持久
历史，因此 cursor 之内的行本就在已验证前缀内；commit 自身的信封哈希、request
hash、result 字节校验全部保留。改动：传 `verified_prefix_cursor=cursor`。
候选 commit 分支原本就传了前缀参数，不受影响。

同类漏修一并修复：`_commit_locked` 的幂等命中分支（重试已存在的 commit_id）原本
也全史重放，现以持久 head 行为前缀界。

### 2. commit 后把被替换的 head 投影记入历史投影缓存

`_historical_projection_cache` 此前只由 `project_at` 的重放结果填充——典型访问
模式（audit_cursor = 几个 commit 之前的 head）第一次访问就要付全史重放，这就是
命中率低的原因。现在 `_commit_locked` 成功后、用新投影覆盖 head 缓存之前，先按
row identity（cursor + 两个哈希 + storage_epoch）验证旧缓存确属本事务 CAS 检查过
的前一 head，再存入历史缓存（容量 8→16，给同回合的 compile/acceptance/下游触发
与穿插的后台提交留余量；相邻投影经 `model_copy` 共享绝大部分元组，增量内存很
小）。受益方：`record_rebased`（Appraisal 接受推进 head 后按 audit_cursor 重读，
生产日志 affect_ms 4.7s~86s 的主因）、恢复路径的 `proposal_audit_by_id`。画像
验证：提交后按 1 个 commit 之前的 cursor `project_at` 为 **0.1ms**
（`historical_projection_hits=1`，无重放）。

### 3. ClockAdvanced reduce 校验增量化（语义等价，未弱化）

`_clock_advanced` → `append_clock_transition` 原本对 6135 条历史跑两遍
`validate_clock_history`（前置 + append 后）。增量化依据的不变量分解：

- 逐条合法性（tz-aware 区间、to>from、policy 已安装）——前缀由归纳保证，新条目单独查；
- revision 严格递增且唯一、event_ref 唯一——前缀由归纳保证，保留原有的
  末条比较与 O(n) 全表查重（属性比较，开销可忽略）；
- 相邻区间不重叠/不倒退——前缀内部由归纳保证，新相邻对显式检查；
- "历史不得超前逻辑时间 / 最新必须为当前"只约束末条——显式 O(1) 检查。

归纳基础：任何进入 reducer 的 `ReducerState` 要么经 `model_validate` 构造
（model validator 里跑全量 `validate_clock_history`，含 head 解码与
`_state_from_projection`），要么从空状态（重放）经本函数逐条演化。元组不可变，
故前缀合法性在链上保持。实现为 `append_clock_transition(..., prefix_validated=True)`
**opt-in 参数，仅 reducers._clock_advanced 传入**；`validate_clock_history` 本身
与 runtime.py 等其它调用方的全量行为不变。效果：47.6ms → **1.2ms**。

等价性测试覆盖接受与全部拒绝面（倒退区间、from 与当前时间不符、revision 不前进、
重复 event_ref、历史超前逻辑时间），两种模式逐一断言同判。

### 4. 语义哈希读取路径核查（无需改动）

昨天的增量片段缓存只覆盖提交路径；核查读取路径结论：`_project_locked` 仅在
head 缓存 miss 时付一次 `make_projection` 全量（语义哈希 ~297ms），而 miss 只发生
在进程启动与外部账本写重校验后（画像 `head_projection_reads=129, hits=128`）。
读取路径没有重复付费，不动。

未采纳项：`_find_affect_proposal_event` 的 json_extract 全表扫实测 245ms、LIKE
预过滤仅省 ~15%，不值得动；`observation_events_at` 的外部写冷校验（288s）是既有
信任模型（跨连接账本写必须重新自证），不在本次范围内改。

## 防回归测试

`tests/world_v2/test_inline_appraisal_ledger_performance.py`（5 项，全部以账本
计数器断言访问形状，不依赖墙钟）：

- `test_observation_events_at_verifies_boundary_without_history_replay`：
  boundary 读后 `total_replay_calls` 不得增加；
- `test_idempotent_commit_retry_does_not_replay_history`：幂等重试不重放；
- `test_project_at_recent_pre_commit_head_is_served_from_memory`：
  提交前 head cursor 命中 `historical_projection_hits`，且与独立重放的投影**全等**
  （语义哈希合同不变的直接证明）；
- `test_clock_advanced_incremental_validation_matches_full_validation`：
  增量/全量两模式接受与五类拒绝逐一同判；
- `test_clock_advanced_reducer_still_rejects_invalid_advance`：reducer 路径拒绝面不变。

新增只读计数器 `historical_projection_hits`（`SQLiteProjectionPerformanceCounters`，
带默认值，既有比较不受影响）。

三个既有测试随访问形状更新（语义未放松）：
`test_sqlite_boundary_verification_prevents_candidate_prefix_replays` 从"boundary
恰好重放一次"收紧为"整个读取零重放"；
`test_sqlite_tampered_unique_index_column_fails_during_cursor_proof` 的篡改改为
经由第二个连接执行——同连接直改本就不在信任模型内（`lookup_event_commit` 早已
如此），跨连接篡改由 mutation epoch 触发全量流式重验并按原错误信息 fail-closed；
`test_projection_performance_counters_distinguish_head_reads_from_history_replay`
区分"内存命中（同进程刚提交过）"与"真实重放（新开实例）"两种形状。

全量 `tests/world_v2/` 复跑：**2410 passed, 1 skipped, 1 failed** —— 唯一失败为
`test_slow_semantic_advice_fails_open_with_a_bounded_delay_and_flash_reply`，即
任务预先声明可忽略的墙钟抖动项：失败断言是 `elapsed < 1.0`（实测 1.89s，机上
同时有两组并发 pytest 与生产 daemon），其相位日志里 ingress/snapshot/context/
model/record 各段均正常，与本次账本改动无关。

## 生产副本前后对比（同一副本、同一 stub 模型、同一路径）

内联相位 = `run_observation` 全程（含真实 Context 编译与审计/完成提交，
模型 stub ~130ms 含在内）：

| 指标 | 修复前（3 回合） | 修复后（8 回合） |
| --- | --- | --- |
| inline p50 | 273 360ms | **259.9ms** |
| inline p95 | ~455 390ms | **262.4ms** |
| inline max | 455 390ms | 327.5ms |
| `observation_events_at` | 264~455s | **0.6~2.1ms** |
| 每回合 `total_replay_calls` 增量 | +1（全史重放） | **0** |
| ingress 批提交 | 121~185ms | 74~100ms |
| `reduce_event(ClockAdvanced)` | 47.6ms | 1.2ms |
| audit_cursor 落后 1 commit 的 `project_at` | ~438s（首次） | 0.1ms |

对照生产法证口径：17:35:10 回合的 immediate-emotion 相位 142.2s ≈ 模型 13.5s +
账本 ~127.7s；修复后同路径账本开销 <0.3s，相位预期 ≈ 模型时间 + <0.5s，满足
"p95 < 5s、账本开销 < 1s"的验收线（accepted 路径另外受益于第 2 项：affect 重定基
的历史投影读取从全史重放降为缓存命中）。

## 边界遵守与遗留

- 改动文件：`sqlite_ledger.py`（boundary/幂等前缀绑定、历史投影记忆、计数器）、
  `clock_authority.py`（opt-in 增量校验）、`reducers.py`（`_clock_advanced` 调用点
  +1 参数）、新测试文件。**未改** `runtime.py`、`production_turn_application.py`、
  `interaction_appraisal_trigger_runtime.py`、`immediate_emotion_proposal_worker.py`、
  `pinned_turn.py`、quick_reaction/afterthought 相关文件。
- 状态哈希/语义哈希合同、事件语义、`REDUCER_BUNDLE_VERSION`（world-v2-reducers.32）、
  投影对外形状均未变化；全部为读路径缓存/复用/增量校验。
- **需要协调者接线的遗留：无。** 投影复用在账本层内完成（按 cursor/row-identity
  判断），不需要 runtime/装配区传参；原任务预留的"可注入 seam"因此不需要。
- 观察建议（不阻塞）：生产 `trigger_processes` 已 5634 条、`completed_trigger_ids`
  5629 条且只增不减，head 状态与逐提交增量编码的成本会继续缓慢上涨，值得另立
  归档/压实议题。
- 画像工具与前后日志保留在 `output/perf-lab/`（`profile_inline.py`、
  `after-fix.log`），可随时在新的生产副本上复跑验收。

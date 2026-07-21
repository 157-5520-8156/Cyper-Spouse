# Context Capsule 预算 fail-soft 修复审计（2026-07-21）

## 结论

修复后的完整版本可部署。生产合法配置 `hard_max_characters=32_000` 的数据压力路径会
按确定性降档顺序收敛，不再因连续性 floor 总量略超 32k 而抛异常；所有 authority
item、哈希和 resolver proof 保持完整，只有 model-facing view 在最后一级按字符压缩。
低于合法胶囊固定表示开销的配置（实测测试边界 500 字符）仍会抛出明确
`ValueError`，这是无法同时满足 schema 合法与 `used<=hard_max` 的配置错误边界，
不是生产数据压力路径。实现不会伪造超预算胶囊。

## 根因与字符账

- 正常回合 cursor `32598`；失败回合 cursor `32624`。
- 两个 cursor 间的第二条 `FactCommittedV2` 使 `relevant_facts` 从 1 项增至 2 项。
- 第二个完整、带 authority 的 Fact model view 增加约 3.1k 字符。
- 全局淘汰走到既有 minimum-retained floors 后，model content 为 **32,153**
  字符，相对 32,000 上限仅超 **153** 字符；旧实现此时没有候选项，抛出
  `global Context Capsule budget cannot represent required whole-item envelopes`。
- advisory 本身没有单独失控；触发条件是 mandatory heads 与各连续性 floor 的合计
  在新增第二条 Fact 后越过全局上限。
- 用保存的失败请求验证完整修复后，胶囊为 **31,355 / 32,000** 字符，并在
  `truncation_log` 中记录所有发生全局降档的切片。

## 降档顺序与不变量

全局预算循环现在按以下顺序执行：

1. 普通 rank 淘汰：仅删除高于原连续性 floor 的尾项；此级与历史实现的候选和
   tie-break 完全相同。
2. 清理已经无 item 的 available model view，保留完整 resolver/source authority，
   并显式呈现 `content_omitted=true`。
3. 保护切片先分别降到 1 项；不会与 advisory 放入同一个跨切片 rank 池竞争。
4. advisory 再按切片内 rank 逐项降档。`proactive_opportunity` 在确定性排序中位于
   首项，因此 tail eviction 下最后保留。
5. 其余 optional 单项继续按 rank 淘汰；proactive 单项具有最后保留优先级。
6. optional 内容全部耗尽后，才压缩 `character_core` / `current_situation` 的
   model-facing payload preview。
7. 若 preview 已降到 0，固定 framing 与最小 slice views 仍超过 hard max，则抛
   “minimum whole-item budget”配置异常。

每次 item 淘汰、空 view 折叠或 mandatory head view 压缩都会进入
`global_omissions`，最终聚合为 `truncation_log` 的
`global_character_budget` 项。压缩 mandatory head 时，`CapsuleItem.payload_json`、
`character_count`、`value_hash`、source bindings、slice hash authority 与 resolver
proof 均不改；不截断、不重写 authority item。

正常预算路径保持字节与语义不变：新增逻辑只在初始
`len(model_content_json) > hard_max_characters` 时进入；未超预算时 slices、
model content、budget audit、compiler hash 与 capsule id 的构造路径没有变化。
能由历史普通 floor 淘汰级满足的旧压力场景也继续使用相同候选顺序与 tie-break。
回归测试另外验证默认编译无 `content_omitted` / `value_preview`、无 truncation log，
且重复编译全模型 JSON 相同。

## 生产副本重放

重放目标是干净副本
`output/capsule-debug/companion-copy.sqlite`；生产库没有写入。stub 的 appraisal
meaning 已从非法 `care_check` 改为 `care`。合法性同时由
`AppraisalHypothesis.meaning` 的 Literal schema（包含 `care`）和
`AppraisalProposalCompiler` 从该 Literal 派生的 `_ALLOWED_MEANINGS` 确认。

真实 `InteractionAppraisalTriggerRuntime` drain 路径结果：

- Context resolve 与 capsule compile 成功，模型调用 **1** 次；
- pinned turn 写入 `ProposalRecorded`；
- appraisal worker 返回 `work_status=accepted`；
- drain `status=processed`；
- 原 stuck trigger 最终状态为 **terminal**；
- 副本 head 在重放时为 cursor `32638`，Context 相位 225.5ms；
- 不再出现 capsule budget 异常，也不再出现此前由非法 meaning 导致的
  `advisory_validation_rejected`。

## 测试

- `tests/world_v2/test_context_capsule.py tests/world_v2/test_pinned_turn.py`：
  **44 passed**。
- `tests/world_v2 -k "proactive or appraisal or interaction"`：
  **178 passed, 2252 deselected**。
- 完整 `tests/world_v2`：
  **2428 passed, 1 skipped, 1 failed**，耗时 949.32s。
- 唯一失败：
  `test_slow_semantic_advice_fails_open_with_a_bounded_delay_and_flash_reply`，
  断言 `elapsed < 1.0`，实测 1.5607s。该测试在 stash 本修复后也已知会以约
  1.58s 失败；本次日志显示失败集中于既有 `reply_audit_ms=1538.9` 墙钟路径，
  与 Context Capsule 改动无关。
- scoped Ruff 与 `git diff --check`：通过。

## SQLite busy timeout 核查

- `QQIngressStore`（`qq_ingress_policy.py`）调用 `sqlite3.connect(...)` 时未显式
  传 timeout，也未设置 busy_timeout，使用 Python sqlite 默认 **5 秒**。
- `SQLiteWorldLedger` 显式 `timeout=10`，即 **10 秒**；另有局部短暂 PRAGMA
  busy timeout 调整，不改变其连接默认值的结论。
- 本任务只记录该差异，未修改 ingress 或 ledger。

## 剩余风险与上线边界

- 完整工作区版本尚未重启加载；按要求本次未重启、未 commit。
- 生产 32k 数据压力样本与极小预算测试均覆盖，但任意未来新增固定 framing/schema
  字段仍可能抬高“最小合法胶囊”配置下界；应继续保留 500 字符配置错误测试。
- mandatory head 的最终 model view 是有界 preview，不是完整语义对象；这是只在所有
  optional 内容耗尽后的最后退化。authority 仍完整，可审计性不受影响。
- 唯一完整套件失败是既有墙钟敏感测试，不阻塞本修复部署，但应另行处理其 1 秒阈值
  或 `reply_audit` 延迟。

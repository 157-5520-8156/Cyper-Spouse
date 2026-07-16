# World v2 外部能力状态

状态：可执行目录（`world-v2-external-capability-catalog.1`）

这份文件记录的是**执行闭环**，不是角色是否应该采用某个表达方式的
行为规则。分类矩阵仍可以把 reaction、typing 或工具查询作为模型的候选
想法；只有目录和对应的专用 authority chain 都完整时，生产运行时才可以
把它变为外部副作用。

| 能力 | 当前状态 | 已有闭环 | 仍缺少的闭环 |
| --- | --- | --- | --- |
| `reply` / `followup` / `proactive_message` | production | immutable payload、expression acceptance、平台 transport、receipt/recovery | 无 |
| `reaction` / `typing` / `sticker` | adapter_only | 不变 payload 和中性 provider receipt binding | proposal materializer、平台专用 transport、provider lookup/recovery |
| `vision` | planned | 无 v2 production seam | source-bound request、acceptance/budget、provider、`VisionResultAccepted` projection、deterministic result trigger、recovery |
| `transcription` | planned | 无 v2 production seam | source-bound request、acceptance/budget、provider、`TranscriptionResultAccepted` projection、deterministic result trigger、recovery |
| `read_only_tool` | planned | 无 v2 production seam | source-bound request、acceptance/budget、provider、`ToolResultAccepted` projection、deterministic result trigger、recovery |
| 用户创意媒体请求 | planned | 旧 `image_requests.py` 可作为 parser 参考，但不拥有 v2 权威 | creative request projection、source-bound Action、budget、provider、delivery receipt/recovery |

## 不可绕过的门槛

`src/companion_daemon/world_v2/external_capability_catalog.py` 是状态的可执行
来源，`production_proposal_grammar.assert_production_proposal_grammar_coverage()`
每次 production grammar 构造时都会验证它。

因此：

1. 非 `production` 能力不能仅通过向 `DecisionProposal` 添加一个 action kind
   变成可执行操作。
2. `PlatformActionExecutor` 识别一个低层请求形状，并不证明该操作可从模型
   到平台闭环；这正是 `adapter_only` 的含义。
3. 工具、vision 与 transcription 的 generic `ExecutionReceiptRecorded` 不是
   语义结果。升格时必须把可验证 result ref/hash 写进各自的结果 projection，
   并从该已提交结果开启确定性后续 deliberation trigger。
4. 用户创意媒体请求与角色自主世界媒体机会必须保持两条 authority 链，不能
   用用户索图 parser 反写角色的世界经历或图片机会。

该目录故意不把 `file_write`、`delete`、`shell`、`account`、`payment` 和
`third_party_commitment` 列为候选；它们仍属于首发禁止的自动能力。

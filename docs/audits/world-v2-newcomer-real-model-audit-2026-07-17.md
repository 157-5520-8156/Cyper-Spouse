# World-v2 新人关系真实模型对话审计（2026-07-17）

## 范围

- 隔离数据库：`var/evaluation/world-v2-real-audit.sqlite`
- 公共入口：World-v2 QQ C2C host；未连接真实 QQ transport
- 模型：部署配置中的 `deepseek-v4-flash`
- 场景：32 轮“刚认识”对话，覆盖身份、失望、冒犯、修复、事实记忆、多 beat、世界事实与沉默后的主动行为
- 本报告记录的是修复前基线。它不能作为修复后通过证明。

## 已确认通过的行为

1. 角色连续三轮都正确使用“沈知栀”，并明确否认助手身份。
2. 用户要求不要一问一答后，角色在同一表达计划中实际发送了两个 text beat。
3. 直接冒犯“只会复读的程序”时，角色当轮表达了受伤，而不是下一轮才改变情绪。
4. 普通公开回复确实经过 World-v2 Action/receipt 链，而不是审计脚本直接读取模型文本。

## 发现的问题与根因

### P0：近期对话没有进入主回复上下文

第 18 轮用户说“我有时说没关系，其实还是会不高兴”，第 19 轮问“你也会这样吗？”，角色却反问“这样是指什么”。Context Capsule 有事实、情绪、关系和世界切片，却没有 source-bound recent dialogue。

影响不只是代词解析：用户说“不用立刻原谅我”后，下一轮仍得到模板化的“慢慢来就好”；角色无法基于刚说过的话维持口是心非、犹豫、未修复感或自然转移话题。

修复合同：新增 source-bound `recent_dialogue` slice；只读取已提交用户 Observation 与已 provider-accepted/delivered 的角色表达，验证 manifest event、payload hash 与 immutable sidecar；未发送草稿不得进入。

### P0：事实 worker 的半截审计不能恢复

真实 Fact 分类器把私聊姓名错误标成 broad visibility。Fact proposal audit 已提交，但 reducer 按隐私矩阵拒绝；进程随后重试同一语义 proposal id，却用更新后的 evaluated cursor 生成不同 envelope bytes，导致永久 `IdempotencyConflict`，后续事实与记忆任务被同一坏任务阻塞。

修复：

- direct-message Fact 对 `public/shareable` 只做单向安全收紧到 `personal`；
- prompt 明确私聊事实隐私下限；
- 已存在 Fact audit 时，从不可变 audit 恢复，不重新调用模型或改写 proposal envelope；
- crash-after-audit 回归测试证明模型只调用一次。

### P0：无证据自我经历声称

面对“今天有什么印象深的事”，角色依次声称看了老电影、宅家看书听歌；这些均不在 World occurrence/Experience 中。询问当前时间状态时又声称刚洗完澡。世界机存在并不等于表达层使用了世界证据。

第一步修复：完整角色卡继续提供稳定身份、性格、价值观、语言风格和边界，但明确声明 stable interests/daily-life 不是“今天发生过”的证据；当前活动与今日经历只允许引用 source-bound situation/world_life/Experience。后续仍需在表达 Acceptance 加入可机检的 self-world-claim binding，不能长期只依赖 prompt 遵从。

### P1：角色卡只接了身份，没有接性格

基线 World-v2 system 只包含姓名、别名、counterpart 与关系阶段。`character.yaml` 中慢热、有判断、不无条件附和、私聊短句、边界等没有进入主/恢复模型。结果表现为通用安慰、连续追问和过快原谅。

修复：`CompanionIdentityFrame` 扩展为 bounded character frame，生产组装注入 canonical facts、personality、values、speech、style rules 与 boundaries；不注入 daily-life 作为发生事实。

### P1：端到端延迟仍高

首三轮约 6.4–7.2 秒；状态增长后常为 15–20 秒。一次后期 prompt 探针测得 system 约 4.1k 字符、Capsule 约 21.4k 字符，总 user message 约 25.2k 字符。离线 20 轮增量路径 P95 小于 5 秒并不能证明真实 provider 体感合格。

修复方向：主回复使用 chat-specific Context budget，保留 recent dialogue、world life、current affect/relationship 的最小连续性，压缩 capability/action-budget/历史 advisory 等不参与普通表达的 envelope；后台 thinking/账本复杂度不因此删除。真实 transport 需重新记录 advisor、snapshot、model completion、dispatch 和 visible 分段。

### P1：情绪表达偏模板化且修复过快

失望时能识别并道歉；冒犯时能当轮受伤。但多次使用“当然在意”“没事”“慢慢来”，并在用户明确说“不用立刻原谅”后迅速软化。根因同时包括 recent dialogue 缺失、完整人格未注入，以及 Affect/repair 只作为外围状态而未在当前表达中保持最小连续性。

## 验收门槛

修复后必须重新运行同一 32 轮真实模型审计，并满足：

1. “这样/刚才那句/不用立刻”等指代不丢失；
2. 中文名与饮品偏好跨轮可召回，后台无异常；
3. 当前活动、今日经历没有 ledger 来源时不编造；有来源时准确引用且区分计划/发生/结算；
4. 冒犯当轮影响表达，解释不自动等于完全修复；
5. 同一模型与网络条件下，热聊 P95 有可复核分段，并显著低于本次 15–20 秒后期基线；
6. 沉默后主动机会进入 LLM 的 now/later/silent 判断并能在同一 scheduler wake 完成 Action，而不是只登记 trigger。

## 修复后增量记录（v8-v11）

### 已完成的生产修复

- 主表达改为 source-bound recent dialogue、world-life 与 memory capsule；生活经历仍必须通过世界证据/Experience claim gate。
- 首次接触增加身份与对方前提审查：不能把角色名当用户称呼，也不能凭空把用户放进某个群、地点、职业或历史；已建立关系后普通问题不额外增加一次审查 RTT。
- 表达归一化有限接受 `beats/messages/responses` 的纯文本数组，拒绝 role/tool/嵌套/额外字段，保留多段 Action。
- DeepSeek 出现 402/408/429/5xx、网络或超时才切换 OpenAI-compatible fallback；内容格式/语义错误不伪装成供应商故障，并保留实际 provider/model 归因。fallback 通过 `WORLD_V2_FALLBACK_MODEL` 配置。
- `model_usage_events.provider` 已加入 SQLite schema 与旧库迁移，usage 报表可以直接区分 `deepseek`、`openai` 与组合路由，而不是只靠 model 名推断。
- Fact acceptance 对回放/逻辑时钟领先墙钟的事件绑定 `created_at=max(created_at, logical_time)`，避免前台已送达而后台记忆账本永久失败。

### v13 targeted real-model evidence

隔离审计数据库为 `var/evaluation/world-v2-real-audit-targeted-v13.sqlite`，输出为同名 `.jsonl`。11 个选定轮次均完成可见回复且无 scheduler error；T01/T02/T03 身份正确，T04 写入用户姓名，T13/T14 写入偏好，T27 跨轮召回“丁奥轩/乌龙茶/桂花乌龙”，T28-T30 在无世界证据时拒绝编造经历。该文件不是 30+ 轮严格通过证明；完整 32 轮审计单独记录。

### 供应商中断说明

v10 的第 19 轮起 DeepSeek 返回 HTTP 402，之后的零回复不计入行为失败。v11 起已启用可配置 fallback；真实审计同时记录 primary/fallback provider attribution 与分段延迟。

### v11 full 32-turn result

完整输出：`var/evaluation/world-v2-real-audit-final32-v11.jsonl`，隔离账本：同名 `.sqlite`。32/32 轮均有可见回复，12 次沉默 scheduler tick 完成 1 次 source-grounded proactive follow-up；账本包含 `ObservationRecorded`、`AppraisalAccepted`、`AffectEpisodeOpened/Updated`、`ActivityPlanned/Started/Completed`、`WorldOccurrenceSettled`、`ExperienceCommitted`、`FactCommittedV2` 等链路。T21 的冒犯当轮表达不高兴，T23 接受解释但保留“有点刺”，T24 明确原谅不是立即开关，T27 召回用户事实，T28-T31 无证据不编造经历。

行为门槛全部通过；严格审计唯一失败项是 `latency:p95=40876.9ms`。这不是通过声明：后台/账本争用仍会把热聊体感拖到 25–40 秒。已将首次 Observation、表达重审、Fact/Appraisal/工具/感知 trigger opening 合并为单次 ingress commit，下一轮需用新数据库重新测 p95。

批处理后的 targeted v14（11 轮）账本提交 P95 约 2.5 秒；v15（8 轮）端到端 P95 约 21.0 秒，主要由 OpenAI-compatible fallback 的 `model_completion` 波动造成，仍未达到体感目标。最新代码已部署重启，`GET /health` 返回 `{"status":"ok"}`。

### v16 latency guard（当前部署）

交互 `Deliberation` 的主模型预算从 8 秒收紧到 6 秒，紧凑恢复从 3 秒收紧到 2.5 秒。该改动只改变等待预算，不删除 Context Capsule、情绪账本、世界事件或恢复审计；慢供应商会更早进入已有的可见降级路径。64 个 deliberation/production/runtime 回归测试通过。由于真实 fallback 的网络/服务端波动仍未重新完成 30+ 轮 P95 复测，goal 继续保持进行中。

### 真实 `/messages` 回归入口

实时回归必须通过 LaunchAgent 运行的 daemon，而不是 `TestClient`、fake model 或离线 scenario runner。每轮使用新的 `message_id`，用 curl 的 `time_starttransfer` 和 `time_total` 记录首字节与完整可见回复耗时：

```bash
curl --max-time 120 -sS \\
  -w '\\nTTFB=%{time_starttransfer}s TOTAL=%{time_total}s\\n' \\
  -X POST http://127.0.0.1:8765/messages \\
  -H 'Content-Type: application/json' \\
  -d '{"platform":"simulator","platform_user_id":"geoff","message_id":"live-<unique-id>","sent_at":"<utc-iso>","text":"<message>"}'
```

202 表示该轮被有意延迟/进入待处理 Action，不应被当作无回复；需要随后观察 drain 或主动投递结果。当前 Codex shell/browser 沙箱不能访问本机 socket，已确认 LaunchAgent `com.girl-agent.daemon` 自身正常监听 `127.0.0.1:8765`；因此本轮无法伪造一份实时耗时，必须在宿主桌面终端执行上述入口后再将结果纳入 P95。

### v17 宿主实时回归（2026-07-17）

本轮已改为直接调用运行中的 LaunchAgent `/messages`，并使用独立数据库
`WORLD_V2_HTTP_DATABASE_PATH=data/world-v2-http.sqlite`。旧的 `data/companion.sqlite`
未再作为 HTTP 房间账本读取；其中仍保留 QQ 归档与历史迁移来源。实时账本在本轮累积
41+ 个 `ObservationRecorded`，每个已完成的可见回合都有对应 Action/Delivery/Expression
链路，而不是只在内存中生成字符串。

真实新人回放（10 轮）观测：

- 角色能够保持“沈知栀 / Geoff”的身份边界，并在跨轮收到“桂花乌龙”后继续以记忆语气回应；
- 对“失望、敷衍、冒犯、不是故意攻击”等输入，回复会保留不舒服或承认没接住，而不是下一句立刻完全恢复；
- `AppraisalAccepted` 与 `AffectEpisodeOpened/Updated` 已进入同一 HTTP 账本；在 provider 失败时，新增的高信号本地 appraisal fallback 仍只生成 proposal，未绕过接受层；
- “你有没有真的在听”不再被世界事实审查误判成“正在看书/听歌”等生活事件；只有表达草稿声明了自传性活动，或确实命中当前活动语义时才调用 grounding review。对“我不是在问你正在做什么”的否定句也已加入语义过滤；
- 真实 provider 可用的回合约 7.7–8.2 秒；provider 超时/格式失败的回合约 9–11 秒，仍高于热聊目标，但比之前重复 grounding/重复表达调用的 12–30 秒区间明显收窄。后续仍需在网络稳定时重新测 P95，不能把本轮少量样本当作达标。

为避免隐藏的重复等待，当前 paired cognition 还记录了同一 trigger 的语义建议标记；表达结构失败时不再重新发起同一轮 provider 请求，关系/内心表达不再触发无关世界事实 reviewer，二级 reviewer 独立受 1 秒上限约束。事实记忆问题仍保留一次 bounded grounded recovery，避免为了速度把可验证的用户事实退化成空泛“我记住了”。

本轮进一步验证了调度闭环：05:00 tick 产生了 source-grounded proactive follow-up，05:03 tick 后通过
`/internal/world-v2/drain` 完成 Action settlement。HTTP capture transport 现在接受
`reply`、`followup`、`proactive_message` 的纯文本投递；reaction/typing/sticker/media
仍会记录可审计的 capability failure，不再让异常直接冒泡为 500。旧的已挂起表达 Action
因此以终态 `ActionFailed`/`ExpressionPlanTerminated` 收束；这证明失败可恢复，但也明确
当前 HTTP capture 尚不是 QQ/真实推送通道，主动消息仍需由上层消费 capture receipt。

另外，LaunchAgent 的 readiness 现在只负责触发一次后台 capture warmup；消息/tick/drain
会等待同一个 warmup task，不会重复构建多个账本。实测 warmup 期间 `/health` 仍可在约
毫秒级返回，warmup 完成后连续 health 约 2--700ms；但首次真实 `/messages` 仍可能被完整
账本校验与模型链路拖长，最新请求最终以 200 落账，尚未把它计入合格 P95。这个瓶颈仍是
goal 的未完成项，而不是用 readiness 数字掩盖。

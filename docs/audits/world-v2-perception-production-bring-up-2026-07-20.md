# World v2 感知（vision）接入 QQ 生产

日期：2026-07-20
范围：把已完成的注入式 perception 竖井接到 QQ C2C 生产组合——用户发图，她能真的"看到"，
感知结果进入下一轮上下文。
审计起点：`build_qq_c2c_host` 的 `perception_model/input_source/transport` 恒 `None`、
`perception_budget_limit` 恒 0；QQ ingress 故意把附件规范化成不含 URL 的不透明 ref，
生产侧没有任何字节可供感知。

## 结论

竖井本身零改动（`perception_*` 的授权/接受/执行/结果语义原样复用），生产侧补齐了四块：
**入站附件归档**（适配器边界拉字节，URL 永不落账本）、**durable vision transport**
（OpenAI 路由、幂等键绑定、重启可恢复）、**感知决策适配器**（保持 deliberation 纪律 +
每日上限 + 字节去重）、**感知授权 provisioning**（与媒体链同款根签名脚本）。真实联调
已用 .env 生产配置跑通（`gpt-4o-mini` 与 `gpt-5.6-luna` 均确认支持视觉，样例见下）。
生产开启只差两步用户侧操作：跑一次 provisioning 脚本、重启 QQ 适配器进程
（`PERCEPTION_BUDGET_LIMIT` 已有克制默认 12，缺 provisioning 时组合自动禁用且仅一条日志）。

## 附件归档设计（隐私）

痛点：`normalize_onebot_qq_ingress` 把图片段规范化成
`qq-attachment:image:sha256:<segment 摘要>`，隐私测试断言 URL 不落盘——但感知需要真实字节。

设计（`world_v2/qq_attachment_archive.py`）：

- **ref 推导单一权威**：`qq_ingress_policy.onebot_attachment_ref()` 提取为公共函数，
  ingress 与归档从同一个 provider segment 推导出字节相同的 ref（摘要覆盖含 URL 的
  segment 负载，但 ref 本身不暴露它）。
- **QQAttachmentArchive**：以 ref 为键的本地内容库（`ATTACHMENT_CACHE_PATH/qq-c2c-v2/`，
  一 ref 一文件、原子写入、幂等 `store`）。不持久化任何 URL/文件名/provider 元数据；
  日志只打 ref 和错误类型。上限 10MB。
- **QQOneBotAttachmentArchiver**：在 `qq_c2c_onebot_app` 收到 raw event 时（进入
  ingress 规范化之前的同一请求内）并发拉取图片字节：优先 segment 里的临时 URL，
  否则走 NapCat `get_image`（新端点 `onebot_adapter.get_onebot_image`，兼容
  base64 / 重签 url / 本地路径三种返回）。URL 只存在于这次调用的内存里。
  任何失败都吞进报告并降级为"无字节可感知"，绝不影响 ingress 与回复。
  历史回填（`qq_history_backfill`）同样挂了这个钩子——包括已 dedupe 的消息，
  因为进程可能在"已入账、未归档"之间崩溃过。
- **PerceptionInputSource 合同**：归档库直接实现 `describe/resolve`。感知的规范字节体
  是 `data:<mime>;base64,...` 字符串（魔数嗅探 jpeg/png/gif/webp，拒绝其它格式），
  `describe` 在 Acceptance 前对该字符串取 sha256 —— Action 的 `payload_hash`
  由此绑定精确字节；`resolve` 在 dispatch 时重开同一文件重算哈希，执行器再校验一次。
  字节缺失/格式不支持 → ValueError → 触发进程以 `rejected` 终态收敛，不空转。

## Vision transport 实现与实测

`world_v2/perception_vision_transport.py::SQLiteDurableVisionPerceptionTransport`，
满足 `PerceptionTransport` 全部合同：

- `analyze`：幂等键已存在 → 校验输入指纹（ref+hash）后直接返回原结果，绝不二次调 provider；
  新键 → OpenAI 兼容 chat/completions（`VISION_MODEL`，data URL 内联，经 `OPENAI_PROXY_URL`），
  文本截 2000 字，落 `world_v2_perception_dispatch` 表（与账本同一 SQLite 文件）后返回。
  身份断言防护沿用 v1 multimodal 的守卫（"这是用户本人"→ 中性描述）。
- `lookup`：重启后按幂等键恢复原回执（`recovery_policy="result_lookup"`）。
- `read_exact`：按 `result_ref` 回放哈希绑定的文本给 Context 编译器
  （`PerceptionResultContent` 自校验 sha256，防转述伪造）。
- 回执 `cost_actual=0`：与媒体链 render/inspection 一致的"部署直付 provider"记账约定
  （见下节预算语义），效果一次性仍由账本管辖。

真实联调（`scripts/run_world_v2_perception_vision_probe.py`，.env 真实配置，
describe→analyze→lookup→read_exact 全链）：

- `gpt-4o-mini`（默认 `VISION_MODEL`），2.2MB 生活照，19.7s：
  "这是一张街头的自拍照片，展示了一位年轻女性的侧影。她穿着米色的上衣，手里拿着手机，
  表情自然，眼神平静，似乎在思考什么。背景模糊中有一些人和商店的标识……"
- `gpt-5.6-luna`（fallback 模型，同代理路由）同样确认支持视觉（像素风沙发小图描述正确）。
- 两次运行均验证 `lookup` 恢复出与 `analyze` 完全一致的五元组。

## 预算与触发语义

**账本预算（机制现成，语义如实说明）**：`PERCEPTION_BUDGET_LIMIT`（默认 12，0 = 禁用）
同时是感知预算账户的 limit 与每次请求的整额预留——这是竖井冻结的形状
（compiler 强制 proposal 申报 `budget_limit == 部署配置`，Acceptance 预留同额）。
效果：**同一时刻最多一个感知 Action 在飞**（整额预留互斥），回执 cost 0 → 结算后额度回满。
因此"每日 12 次"不由账户余额扣减实现，而是：

**决策适配器的确定性纪律**（`world_v2/perception_decision_adapter.py`）：

1. 只考虑媒体类型为 image 且字节已归档的附件（audio/video/file 不进入 vision）；
2. **字节去重**：完全相同的图片（input_hash 命中 dispatch 表）不再看第二次——
   她已经知道内容，结果仍在下轮 Context 里；
3. **每日上限**：本地日（`LOCAL_TIMEZONE`）内实际 dispatch 数 ≥ `PERCEPTION_BUDGET_LIMIT`
   → 直接 no-change（durable 计数，重启不重置）；
4. 过了确定性闸门才花一次 bounded flash 决策调用（DeepSeek 主 + luna fallback，
   同聊天 lane 路由）：给出消息文字与可看图片数，模型回
   `{"look": bool, "attachment_index": int, "reason": str}` —— 纯装饰/刷屏可以不看，
   这保住"deliberation 选择哪条附件值得感知"的竖井纪律，不是每图必看；
5. 所有拒绝路径都是**合法的 no-change DecisionProposal**（单次审计、无失败谱系）；
   决策模型挂掉 → decline，绝不阻塞或污染可见回复。`recover` 恒返回惰性输出
   （该 lane 的 grammar 使 recovery 无法产生可接受提案，主尝试失败即 no-change）。

触发链保持原样：runtime 在 Observation 含附件且感知已组合时开
`perception_deliberation` 触发进程 → 后台 worker 审计 → compiler 校验
（含授权 fail-closed）→ Acceptance（预算+Action）→ ActionPump 二次授权 → transport →
`PerceptionResultAccepted` → 结果触发进程 no-visible-action 收敛 →
下轮 Context 的 `perception_results` slice（`provider_observation_not_world_fact`，
聊天提示词区零改动，注意力代理地盘未碰）。`perception_deliberation.py` 仅把主模型
超时从 6s 放宽到 12s（后台 lane，允许一次 provider 故障转移）。

## 感知授权 provisioning

`world_v2/perception_authority_provisioning.py` + `scripts/provision_world_v2_perception_authority.py`
（与媒体链同构，reduce 时由既有授权 reducer 全量验证）。写 5 个根签名事件：

- 2 个 ActorAuthority（perception-user / perception-operator）；
- `capability:world-v2:perception-vision`：`perception_tool`、actor `agent:companion`、
  target 仅 `perception:vision`、**constraint:read-only**；
- `consent:world-v2:perception`：grantor `user:geoff` → grantee `agent:companion`、
  data scope 仅 `data:image_content`；
- `privacy:world-v2:perception`：viewer 仅 `viewer:companion` + `viewer:platform_adapter`、
  retention `retention:persistent`。

幂等可重跑；**故意不 provision transcription**（无音频归档，音频感知保持 fail-closed）。
测试证明该链恰好满足 `ProjectionPerceptionAuthorizationResolver` 的 exactly-one 判定，
换 actor/subject/target 均拒绝。

## 组合接线清单（组合根，最后一步完成）

- `config.py`：新增 `PERCEPTION_BUDGET_LIMIT`（默认 12；bootstrap 后视为部署常量，
  改动需与账本既有账户一致否则启动报错——与其它预算键同规）。
- `world_v2/qq_perception_deployment.py::build_qq_perception_deployment`：
  Settings → 完整注入束（决策适配器/归档库/transport/预算/归档钩子）。
  缺开关、缺 OPENAI/DEEPSEEK 密钥、authority 未 provision 任一 → 返回 None + 一条
  `world v2 perception lane disabled ... missing: ...` 日志。
- `qq_c2c_onebot_app.create_qq_c2c_onebot_app`：非 fake 模式调用工厂；四个感知参数
  透传 `build_qq_c2c_host`（其签名早已预留，本次未改动该文件——快应答代理的改动原样保留）；
  `/onebot/event` 对含图事件并发启动归档任务（与 ingress 的成句等待重叠，不加时延）；
  startup 回填传入归档钩子；shutdown 关闭 transport。
- `napcat_cli.py` 零改动（其 create_app 已委托上述组合根）。

## 上线步骤（操作者）

1. 跑 provisioning（需要部署根签名种子）：
   `WORLD_V2_ROOT_SIGNING_KEY_HEX=<seed> .venv/bin/python scripts/provision_world_v2_perception_authority.py --database data/companion.sqlite --world-id world:companion-v2:qq-c2c:geoff --subject user:geoff`
   若用测试根（`11`×32），本脚本与之后**每个重放该账本的进程**都必须带
   `WORLD_V2_ENABLE_INSECURE_TEST_ROOT=1`（与媒体链同一注意事项）。
2. 重启 QQ 适配器进程（本次交付未重启任何服务；生产账本仅做过只读查询）。
   重启时 bootstrap 会补写 `account:world-v2:perception`（limit 12）预算账户。
3. 可选：`.env` 里调 `PERCEPTION_BUDGET_LIMIT`（重启前定稿；bootstrap 后不可随意改）。

## 测试结果

新增 43 例（归档 9 / transport 5 / 决策适配器 9 / provisioning 2 / 部署工厂+端到端 4 +
回填钩子 1 + 既有感知竖井回归 13 全绿）。端到端用真实生产件（归档库 + 决策适配器 +
durable transport，仅 provider HTTP 为 mock）复刻了 SQLite 组合测试：
附件 → 触发 → 决策 → Acceptance → dispatch 恰一次 → 重发同图去重（provider 不二次调用）→
下轮 Context 出现 `perception_results` 文本。

`tests/world_v2/` 全量（收尾重跑）：**2344 passed, 8 failed**。8 个失败全部与感知无关、
均指向并行代理未收尾的工作（文件为未跟踪新增或他人修改中）：
`test_background_does_not_block_ingress` ×2 与 `test_production_same_turn_advisory` ×1
（任务书预声明可忽略）、`test_proactive_action_production` ×3（未跟踪新文件，
StopIteration 于 fake 迭代器）、`test_http_v2_host_migration` ×1（他人修改中的文件）、
`test_scenario_runner` ×1（机制基线漂移，`configs/mechanism_closure.yaml` 他人修改中）。
中途一次全量还出现过 `test_platform_reverse_architecture_guard`（指向媒体代理文件
`qq_media_deployment.py` 的 `companion_daemon.runtime` 引入），收尾重跑已由该代理自行修复。
以上失败均不引用本次任何新模块；感知触达的共享文件（ingress/backfill/onebot 适配器/宿主迁移）
的既有测试 43+34 例全部通过。

## 遗留

- 音频（transcription）未接：归档只收图片，授权只 provision vision，竖井语义已就绪，
  待有音频拉取与转写 provider 时补第二条链。
- 每日上限计数按"实际 dispatch"记：授权后未 dispatch 的间隙可能短暂多放一个提案，
  但整额预留保证同时最多一个 Action 在飞，超发上限为 +1，可接受。
- 归档目录暂无保留期清理（privacy policy 声明 retention:persistent 与之一致）；
  若要改成会话保留期，加一个 scheduler 清理钩子即可。

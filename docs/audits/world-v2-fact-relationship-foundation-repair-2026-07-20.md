# World v2 地基修复：事实记忆零提交 与 关系恒静止（2026-07-20）

生产世界 `world:companion-v2:qq-c2c:geoff`（4 天、63 个用户消息批次）审计确认两个"接线了但产出为 0"的故障，本次修复并离线验收。生产库全程只读；未重启任何服务。

## 故障 1：事实记忆颗粒无收（63 触发 → 61 no-change + 2 invalid-draft，0 提交）

### 根因

1. `INSTALLED_FACT_PREDICATE_CARDINALITY` 只有 6 个谓词（location.current、display_name、timezone、likes/dislikes、affiliation）。用户实际聊到的比赛日程、写代码、被吵醒、自我介绍等没有落点。
2. `fact_draft_adapter` 提示词只保留"durable factual assertion / 显式自我断言"，把随口陈述的个人事实全部筛掉——连 "我叫丁奥轩" 也在生产里 no-change。

### 新谓词目录（catalog v2，仅增不改）

| 谓词 | cardinality | 语义 |
|---|---|---|
| profile.occupation | single | 当前职业/工作身份 |
| profile.education | single | 学业阶段/学校/专业 |
| location.home | single | 居住城市（区别于瞬时 location.current） |
| location.hometown | single | 家乡 |
| schedule.commitment | set | 有日期/待发生的安排承诺（"明天打国赛"、"21 号上车"） |
| situation.recent | set | 近期处境/刚发生的事（"被快递员吵醒了"） |
| activity.current | set | 正在做的事（"在写代码"） |
| relationship.person | set | 家人/朋友/同事等具体的人 |
| health.condition | set | 健康状况/过敏/伤病 |
| routine.habit | set | 作息与习惯 |
| interest.activity | set | 反复参与的活动爱好（likes 仍留给口味型喜好） |
| possession.item | set | 物品/设备/宠物 |

cardinality 设计要点：single = 单槽位，不同新值需 correct/withdraw 转移（当前提交型 runtime 无法纠正）；set = 并存累积，仅拒绝完全相同 value_hash。因此**易变的"当前活动"故意定为 set**，避免单槽位冲突。

### 版本纪律

- `reduce_fact` 在重放已提交事件时校验该白名单 → **只能增不能改/删**。原 6 个谓词一字未动；新增 12 个附完整 rationale 注释，并加目录版本标记 `INSTALLED_FACT_PREDICATE_CATALOG_VERSION = "fact-predicate-catalog.2"`。
- `FactObservationProposalAdapter.VERSION` bump 到 `fact-observation-draft.2`（提取政策变更）；proposal 身份 digest 里的 contract 标签**故意保持 `.1`**——身份材料与推导完全未变，保持稳定可让崩溃恢复继续 join 升级前已记录的 audit。
- sealed 描述符 `matrix:fact-predicate.2` 未动：其 digest 由固定标签派生、与目录条目内容无关，且生产账本中没有任何 fact 清单记录（0 FactCommitted）。
- 提示词新增逐谓词 gloss（`_PREDICATE_GUIDE`），有测试强制它与安装目录严格同步。

### 附带加固：单值冲突不再毒化队列

审计中发现一个即将被新目录放大的旧地雷：commit-only 的 `InteractionFactTriggerRuntime` 遇到单值槽位已有不同活跃值时，acceptance 会被 reducer 永久拒绝，而触发器会在每次 lease 过期后无限重试（毒化 fact 车道）。现在 acceptance 的持久性拒绝（非 ConcurrencyConflict）会把触发器以 `acceptance-rejected` 出口完成消费——宁可丢一条冲突草案，不阻塞队列。附回归测试（先 "我住在杭州" 后 "我现在住上海了"）。

### 离线验收（63 条真实消息 × 真实模型）

`scripts/offline_fact_extraction_audit.py`：从只读生产账本取全部 63 条 ObservationRecorded，重放新适配器。模型路由与生产完全一致（DeepSeek flash 主 + gpt-5.6-luna 经代理 fallback；注意：**生产 DeepSeek key 现在返回 402 欠费**，见"运维发现"）。

结果：total=63，retained=8，no-change=54，invalid=0，empty=1，**提取率 12.9%**（生产原为 0%）。8 条全部人工核对合理：

```
10 | 等哪天我去你那边旅游的时候请客   → schedule.commitment  conf=9600
11 | 丁奥轩                        → profile.display_name conf=10000
43 | 在写代码                       → activity.current     conf=9900
47 | 明天还得打国赛                  → schedule.commitment  conf=9900
58 | 被快递员吵醒了                  → situation.recent     conf=9900
60 | 今天要打比赛                    → schedule.commitment  conf=9800
62 | 等很久才能进场                  → schedule.commitment  conf=9000
63 | 21 号上车                      → schedule.commitment  conf=9500
```

指令点名的三条（"明天打国赛""在写代码""被快递员吵醒"）全部命中；value 均为原文精确子串（防编造锚未松动）；"还有点紧张"（瞬时情绪）、问候/ping/测试消息正确 skip；0 条 invalid-draft（生产原有 2 条）。其余 54 条 skip 均核对无遗珠——该世界大量消息确为链路测试与问候。

## 故障 2：关系 15 份草案全部 no_change

### 根因

1. **上下文过薄**：`RelationshipDraftDeliberationAdapter` 只喂 2 个字段——触发 appraisal 的假设码（如 `social_warmth/low`）+ 关系头（恒为 stranger 全零）。模型看不到任何对话文本，无法区分 ping 和 "好不容易有人会心疼我诶"。
2. 提示词只抽象说"real moments"应该有信号，无步幅校准，模型在贫上下文下永远选保守分支。

### 修复

- **补上下文（bounded）**：胶囊新增三个只读摘要字段——`recent_dialogue_summaries`（≤12 条、每条 ≤200 字符，来自已验证 recent_dialogue slice）、`recent_appraisal_summaries`（≤8 条，触发者之外的已接受 appraisal）、`active_affect_summaries`（≤8 条，活跃 Affect episodes 的维度+强度）。全部来自 resolver 已验证的胶囊 slices，仅重塑不引入新授权；slice 被预算裁掉时安全降级为空元组（行为等同 v1）。
- **校准提示词**：no_change 仅限真正中性内容（ping/事务/测试）；自我暴露、示弱被接住、寻求关心、守约、修复等应给小步幅信号——单轮通常 20–150bp、强时刻 150–400bp、未触及变量置 0；防谄媚明确写入（一两句甜话最多小 session 信号，durable 留给重复模式/明确承诺/完成的修复；负面时刻用负 delta）。
- **版本**：`RelationshipEvaluationDraftAdapter.VERSION` → `.2`，`RelationshipDraftDeliberationAdapter.VERSION` → `.2`（均附 rationale）；输出语法与 proposal 身份 `_CONTRACT` 未变。

### 离线验收（真实生产 appraisal + 周边真实对话，跑两轮验证稳定性）

`scripts/offline_relationship_draft_audit.py`，两轮结果一致：

```
neutral-first-chat-question ("这是我们第一次聊天吗")   → no_change ✓
neutral-online-ping        ("看看你在不在线")          → no_change ✓
warm-tired-disclosure      ("我今天有点累，跟你说一声") → no_change（边界情形，防谄媚下可接受）
warm-tired-wants-to-talk   ("有点累，想跟你说说")      → signal warm_self_disclosure, session,
                                                        deltas={closeness+90, mutuality+70}
intimate-do-you-dislike-me ("你是不是不喜欢我"+撒娇语境) → signal relational_opening, session,
                                                        deltas={trust+20~40, closeness+90~100,
                                                                reliability+20~30, mutuality+90~100}
```

亲密对话产生 bp 级小步幅 session 信号、中性对话仍 no_change，无跳变。

## 附带核对：Affect 接受率（63 批 → 19 AppraisalAccepted → 7 AffectEpisodeOpened）

结论：**深层 appraisal 提示词没有系统性压制**——meanings 目录含全套正面含义，生产中 14 份草案给出 appraise+affect（"他明确说今天有点累……表达了需要被听见"等），另有 AffectEpisodeUpdated×5 说明合并/更新在工作。真正的两个损耗源：

1. **本地快速初筛门 `FastAppraisalDraftDeliberationAdapter` 只列负面触发词**（失望/生气/难过/道歉…），warm/亲密消息在到达深层模型前就被 no_appraise——生产里 "那你会心疼我嘛" 一批全被 "快速情绪初筛结果" 筛掉。已修：触发词补入亲近/撒娇/感谢/被暖到/寻求关心/袒露脆弱（对应 care/support/shared_joy/social_warmth），VERSION bump 至 `fast-appraisal-draft-adapter.2`。
2. **Provider 可用性**：7-19/7-20 大量 "Provider recovery exhausted (main_timeout)"，与压制无关，属运维问题（见下）。

## 运维发现（未在本次修复范围内动手）

- 生产 `.env` 的 DeepSeek key 现在直接返回 **402 Payment Required**（余额耗尽），与账本里 19-20 日激增的 provider 失败一致。当前后台认知靠 gpt-5.6-luna fallback 撑着；建议尽快充值或调整主路由。

## 测试

- `tests/world_v2/` 全量：**2272 passed, 3 failed**，其中：
  - `test_http_v2_host_migration.py::…retains_a_fact…`：因我改了提示词措辞、测试内 fake 模型按旧关键字分流——已把关键字对齐（`durable factual assertion` → `Assess one verified user message`），单文件复跑 27 passed。
  - `test_platform_reverse_architecture_guard.py`：指向他人未提交的 `qq_media_deployment.py`（media 领域，不属本次范围）。
  - `test_production_same_turn_advisory.py`：已知可忽略项（本地 8188 延迟断言 1.57s > 1.0s；接口本身存活）。
- 直接相关的 9 个测试文件复跑：**70 passed**（含新增 6 个测试：谓词 gloss 同步、提示词列全目录、日常谓词物化、单值冲突不毒化、关系胶囊携带对话/情感摘要、可选 slice 缺失安全降级）。
- 改动文件 ruff 全绿。

## 改动清单

| 文件 | 改动 |
|---|---|
| `world_v2/fact_reducers.py` | 谓词目录 +12（append-only）、目录版本标记与纪律注释 |
| `world_v2/fact_draft_adapter.py` | 提取政策提示词 v2、逐谓词 gloss、VERSION→.2 |
| `world_v2/interaction_fact_trigger_runtime.py` | acceptance 持久拒绝 → `acceptance-rejected` 消费出口 |
| `world_v2/relationship_evaluation_draft.py` | 胶囊 +3 个 bounded 上下文字段、校准提示词、VERSION→.2 |
| `world_v2/relationship_draft_deliberation_adapter.py` | slices 摘要采集（lenient）、两条路径注入、VERSION→.2 |
| `world_v2/appraisal_chat_model_adapter.py` | 快速初筛门补正面触发词、VERSION→.2 |
| `tests/world_v2/…`（3 个文件） | 新增 6 个测试；fake 模型关键字对齐 |
| `scripts/offline_fact_extraction_audit.py` / `offline_relationship_draft_audit.py` | 只读离线验收工具 |

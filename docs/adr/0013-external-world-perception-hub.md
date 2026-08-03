---
status: accepted
---

# 以独立 World Perception Hub 感知现实，并通过角色感知影响事件机

Girl-Agent 将地震、天气、城市事件、公共预警、普通新闻、文化变化、网络趋势和
兴趣领域更新视为外部世界信号，而不是 Life Ecology 自己生成的剧情材料。系统
拟建立独立的 World Perception Hub：它从多个可替换来源采集并规范化现实信号，
向角色提供来源明确、时间有限的 Perception Candidate；只有角色模型在一个
Pinned Turn 中实际注意到某条信号后，才提交 External Perception World Event。
Life Ecology、Affect、Memory、Relationship 与 Social Initiative 可以消费该
感知事件，但外部信号本身不得直接命令角色行动或联系用户。

本 ADR 的架构和运行边界已实现。它不构成任何具体生产数据源的授权：生产部署默认
`off`，只有不可变来源 registry、许可证据、live 暴露/快照权限和来源绑定角色 Channel
同时存在时才会启用。外界感知只开放 Life/Social 的考虑机会，不拥有主动发送权。

## 为什么不把它并入事件机

现实采集和角色生活发展具有不同的真值与权威：

- External Signal 只证明某个外部来源在某时给出了某项声明。
- External Perception 证明角色通过某个可信渠道看见或听见了该声明。
- 后续权威来源、更新或撤稿决定外部声明是否被修正。
- 角色因为一条后来被纠正的信息而担心、改变计划或和 NPC 讨论，仍然是已经发生
  的 World Experience，不能因来源修正而从历史中删除。

Life Ecology 负责从已经可用的情境中开放地提出环境机会、偶然性和后果，不应同时
承担互联网采集、新闻去重、来源评级和内容许可。World Perception Hub 也不能成为
第二个 World Author：它只能提供可被感知的现实材料，不能把“地震”“流行话题”
或任何关键词映射为固定剧情、情绪或消息。

## 内部统一边界，而不是统一外部 API

不存在同时覆盖灾害、天气、地方活动、新闻、文化和网络趋势的可靠单一 API。
生产设计因此依赖项目自己的稳定 `ExternalSignal` envelope，并把每个外部来源
隔离在 Adapter 后面：

```text
ExternalSignal
├── signal_id
├── revision
├── source_kind
├── source_authority
├── source_ref
├── source_payload_hash
├── signal_kind
├── headline
├── factual_summary
├── occurred_at
├── published_at
├── updated_at
├── expires_at
├── geometry
├── entities
├── topic_embeddings
├── confidence
├── evidence_refs
└── correction_of
```

`signal_kind` 描述来源内容的外部性质，不是角色动机枚举，也不对应任何固定行为。
Provider-specific payload 保存在可校验的 sidecar；标准 envelope 只承载跨来源检索、
时效、位置、证据与修正所需的稳定字段。事件机和角色模型不依赖供应商响应格式。

Adapter 至少需要提供增量游标、抓取时间、原始证据引用、供应商事件身份和修正关系。
单个来源失效、限流或被替换时只影响该 Adapter，不能改变 World Event schema。

## 数据流

```text
Source Adapters
  -> raw evidence sidecar
  -> normalized ExternalSignal revisions
  -> cross-source clustering and deduplication
  -> TTL external signal index
  -> source/geo/time/topic candidate retrieval
  -> role-model attention and interpretation
  -> ExternalPerceptionRecorded
  -> Life Ecology / Affect / Memory / Relationship / Social Initiative
```

采集、格式解析、确定性去重、地理范围计算、时效过滤和 embedding 召回应优先使用
本地代码，不为每条文章调用角色模型。模型只看到在当前 Pinned Turn 中可接触且
可能相关的小型候选集，并拥有以下自由选择：

- 注意到其中零条、一条或多条；
- 认为信号可信、可疑、无聊、重要或暂时无法判断；
- 形成自己的理解、情绪和联想；
- 将它留在当下、写入记忆、影响生活选择、向 NPC 求证或想到用户；
- 现在联系、以后联系或完全不联系。

随机性可影响候选暴露、注意力机会和考虑时机，但本地随机抽签不能把候选直接变成
“角色已经看到”，也不能预选行为。

## 对事件机的影响接口

External Perception 是事件机可消费的 Life Influence，而不是直接 Life Event
Proposal。后续处理继续遵守 ADR-0012 的权威分离：

- World Author 可以结合 External Perception 提出开放的环境机会、突发、NPC
  反应和客观后果；
- Character Model 决定角色是否参与、怎样应对以及是否形成计划或长期方向；
- Affect appraisal 可把角色对该信息的主观评价纳入当前情绪；
- Memory 可巩固“她当时看见了什么、如何理解和如何行动”，同时保留来源版本；
- Social Initiative 只获得一次情境刺激和来源材料，角色仍决定是否联系用户；
- 外部信号不能自行创建 Experience、用户事实、角色当前位置或已发生的生活后果。

例如暴雨预警可以让角色注意到出行可能受影响，但不能由本地代码自动取消计划。
World Author 可以提出交通受阻、行程改变或毫无影响等情境；角色和后续证据共同
决定实际结果。

## 紧急公共事件

地震、海啸、极端天气和其他安全预警可使用更低延迟的采集与调度。紧急度、严重度、
确定性和影响区域只允许：

- 提高采集、核验和感知考虑的优先级；
- 立即打开一次角色考虑机会，而不等待 ambient cadence；
- 为模型提供来源权威、地理影响、更新时间和不确定性。

它们不能硬编码“必须问候”“必须报警”或固定措辞。若用户位置可能处于危险区域，
角色结合关系、位置置信度、最近互动和当前情境自行决定如何关切。安全系统仍可在
独立的产品边界展示公共警报，但不得冒充角色消息。

紧急来源应优先使用权威结构化数据。Common Alerting Protocol（CAP）可作为部分
公共预警的通用格式，因为它表达 urgency、severity、certainty、area、update 和
cancel；它不是覆盖所有国家和事件的单一 endpoint。地震等领域可以使用专门的
GeoJSON、QuakeML 或当地权威 feed。普通新闻聚合 API、RSS/Atom 和公开网页只能
作为另外的 Adapter，不能取得紧急权威来源的信任等级。

## 有限注意力与可感知渠道

角色不应像全知新闻摘要器。每个候选必须说明她为何可能接触到它，例如：

- 操作系统或公共预警通知；
- 她主动阅读的新闻、社区或兴趣订阅；
- 当前活动中自然出现的屏幕、广播或场所信息；
- 已存在 NPC 的有来源转述；
- 她因计划、好奇或对话主动搜索。

“候选与角色兴趣相似”只能增加被检索到的机会，不能证明她已经阅读。模型提交
External Perception 时必须选择一个由当前 Context 支持的感知渠道；不能为了使用
新闻而回填不存在的设备使用、NPC 对话或历史习惯。

## 用户位置、隐私和关系

外部地点与用户位置匹配属于高风险边界。位置投影必须区分当前所在地、常住地、
家乡、行程目的地、过去所在地和仅在对话中提到的地点，并保存来源、置信度、
授权用途与有效期。“用户提到深圳”不能直接成为“用户现在在深圳”。

地理交集尽量在本地计算，不向外部新闻或搜索供应商发送用户精确位置。给角色模型
的 Context 继续保留不确定性；当当前位置证据不足时，角色可以询问确认，但系统
不能替她断言用户受灾。对 NPC、家庭成员和第三方位置使用相同的隐私与来源闭包。

## 外部存储与 V2 账本

原始抓取和绝大多数 External Signal 不进入不可变 V2 World ledger：

- raw payload 存入带保留期和内容哈希的外部 evidence sidecar；
- normalized signal、embedding 和 cluster 存入可重建、带 TTL 的外部索引；
- 相同现实事件的多篇报道聚成一个 cluster，并保留各自来源与版本；
- 只有实际 External Perception、被采用的来源快照、模型审计和后续 World
  consequence 进入 V2；
- 来源更新、撤稿与更正通过新 revision 和 correction lineage 表达，不能改写历史；
- 引用必须冻结到精确 revision 和 payload hash，冷重放不能重新访问互联网。

这条边界避免把持续抓取产生的海量噪声写入事实账本，同时保留角色实际感知与决策的
来源闭包。

## 失败、重放与供应商边界

- Adapter 超时、限流、解析失败和来源不可用属于技术状态，不能伪装成“角色没看到”。
- 同一 signal revision、Pinned Turn 和 attention attempt 使用稳定身份，重启不得
  重复提交 External Perception。
- 角色模型非法输出时，按 ADR-0010 将可用候选和精确失败原因交给同一模型受约束
  重选一次；仍失败则记录技术失败。
- 抓取失败不能让旧信号无限保持“最新”；超过 `expires_at` 后不得继续作为当前事实。
- 外部提供的推荐行动、情绪化标题和来源排序只是证据内容，不能成为系统行为指令。
- 许可或版权不允许保存全文时，只保存允许的元数据、摘要、URL 和可验证哈希；事实
  闭包不能依赖冷重放时仍能访问原网页。

## 成本与调度原则

不同来源采用不同节奏：

- 紧急结构化预警使用推送或低延迟增量轮询；
- 天气、交通和地方活动按其变化速度周期更新；
- 普通新闻和文化趋势低频批量获取；
- 小众兴趣由角色现有兴趣和生活方向决定订阅范围；
- 全文 embedding 仅在必要且许可允许时生成，常规召回优先使用标题、摘要、实体、
  时间和地点；
- 感知模型尽量与已有 Life Ecology wake 合并，只对紧急候选打开额外调用。

健康投影最终应能区分：来源没有新信号、信号存在但无相关候选、候选未被角色注意、
角色已感知但选择不行动、以及采集或模型技术失败。不能把这些状态全部显示为沉默。

## 已实现的分阶段验证

实现按隔离数据库和 shadow-first 路径完成：

1. `ExternalSignal`、revision、cluster、TTL 和 evidence sidecar 与 V2 权威隔离。
2. USGS 与 NOAA/NWS 权威 Adapter 覆盖时效、更新、取消、限流、坏相邻记录和来源证明。
3. RSS/Atom、RSSHub 和已授权 search 结果保持有限、可替换的 transport 身份。
4. 角色模型在 shadow/live 中可选择零条或多条；shadow 永远不能升级为 live 权威。
5. live delivery 原子记录模型审计、精确快照和角色感知，CAS/重启/崩溃保持 effect-once。
6. 整批感知只打开一次 Life wake，并只给 Social Initiative 一个情境刺激。
7. health 区分采集、候选、模型沉默、技术失败、delivery backlog、提交和 superseded。

首期不追求覆盖整个互联网，也不建立固定“新闻类别到角色反应”的规则。验收重点是
来源闭包、有限注意力、位置精度、修正能力、低重复率和对现有 Life Ecology 的开放
影响。

## 首次生产接源前确认

- 中国境内地震、天气、公共预警和地方事件采用哪些可持续、许可明确的权威来源；
- 普通新闻与趋势供应商的使用条款、全文保留与模型处理许可；
- 用户位置用于灾害相关性匹配的显式授权和默认粒度；
- 外部 evidence sidecar 的保留周期、容量预算与删除策略；
- shadow 阶段以什么生产轨迹衡量有效感知、误报和不自然的热点追逐。

## 参考标准与接口形态

- OASIS Common Alerting Protocol 1.2:
  https://www.oasis-open.org/standard/cap/
- WMO Common Alerting Protocol 与权威机构登记:
  https://public.wmo.int/activities/common-alerting-protocol-cap
- USGS 实时地震 feed 与 GeoJSON:
  https://earthquake.usgs.gov/earthquakes/feed/
- NewsAPI `Everything`（仅作为普通文章发现接口示例）:
  https://newsapi.org/docs/endpoints/everything

该设计选择“外部感知独立、角色注意力自主、事件机消费已感知事实”，而不是把新闻
API 直接接进 Life Ecology。代价是增加来源适配、外部索引和感知审计，但可以避免
供应商锁定、账本膨胀、角色全知以及系统用新闻关键词替角色写剧情。

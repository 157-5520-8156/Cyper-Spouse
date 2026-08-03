# 外界感知系统权威来源、许可与数据边界研究

状态：2026-08-03 调研基线；仅使用发布者、标准组织或项目自身的一手资料。

这份文档给 World Perception Hub 提供首批来源决策依据，不构成法律意见，也不自动
授权生产接入。上线时仍须把当时有效的条款、合同和 endpoint 固化为不可变
`policy_revision`，由项目负责人接受对应风险。

## 结论

首批可以直接进入生产接源评审的来源只有：

1. USGS 全球地震 GeoJSON；
2. NOAA/NWS 美国公共预警 API 与原始 CAP 1.2 消息。

它们都有稳定的机器接口、明确的更新语义和较清楚的再利用许可。USGS 也覆盖发生在
中国境内的地震，但只能表述为“USGS 报告”，不能冒充中国地震台网中心的官方测定。

中国本地权威来源目前应采用“签约后生产”的路径：

- 中国地震台网中心提供实时产品推送、定制接口和按年订阅，但公开规则并未授予任意
  自动采集、长期保存、embedding 和模型处理权；
- 中国气象局登记在 WMO 的 CAP feed 当前返回 404；中国天气网 SmartWeatherAPI
  需要申请并审核。只有拿到接口和许可范围后才能进入生产。

RSSHub 可以作为隔离的 transport gateway，但不是事实权威，也不是内容许可证。
RSSHub 本身是 AGPL-3.0；每条 route 抓到的文字、图片、视频和用户数据仍受上游平台、
作者与个人信息规则约束。当前没有任何国内社交媒体 RSSHub route 适合直接向角色模型
开放全文。

## 分级标准

本文使用三个状态：

- **生产**：接口、来源身份、更新/纠错语义和当前公开许可足以支撑受限生产使用；
- **仅 shadow**：只允许隔离采集和质量验证，不进入 World V2、角色模型、embedding、
  Life Ecology 或发送链；
- **禁止**：不得采集。通常因为依赖登录凭据、私有数据、逆向签名、反爬绕过，或平台
  条款明确禁止自动化抓取。

“仅 shadow”不是规避上游条款的许可。如果来源条款不允许自动采集，仍必须归入
“禁止”。

## 首批生产来源

### 1. USGS 全球地震

| 字段 | 决策 |
|---|---|
| `source_id` | `usgs.earthquake.global.v1` |
| endpoint | `https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/all_hour.geojson`；断线补偿用 `all_day.geojson`；单事件 detail 使用 summary 中的 `properties.detail`，不得自行拼接 |
| 格式 | GeoJSON `FeatureCollection`；detail 为单个 GeoJSON `Feature` |
| 更新频率 | 官方 hour/day/7-day/30-day feeds 均每分钟更新；生产轮询不得快于来源刷新周期 |
| 身份与修订 | `feature.id` 是来源事件身份；`properties.updated` 与 payload hash 共同形成 revision。相同 `id` 的新 `updated` 是修订，不是新地震 |
| 纠错/撤回 | 保存 `status`、`sources`、`ids` 与 detail `products[].source/updateTime/status`。同一事件的后续 revision 通过 `correction_of` 串联；事件从短窗口消失不能被解释为撤回 |
| 权威范围 | USGS 发布的全球地震测定及汇集产品；涉及中国时必须保留 `source_authority=USGS` |
| 许可 | USGS 说明美国联邦雇员职务作品通常属于美国公共领域，并建议对合适的数据发布使用 CC0；但合作方、承包方和第三方材料可能另有限制 |
| raw 保留 | 只在 event revision 的内容哈希变化时保存 raw，不保存每分钟相同的 feed 快照。未被角色感知的 raw revision 保留 30 天；被采用的精确 revision 可随 V2 evidence snapshot 长期保留 |
| normalized 保留 | 事件及 revision 的最小元数据可长期保留；低影响、未采用事件可在 90 天后从热索引淘汰，热索引从 normalized 元数据重建，不依赖已经到期的 raw |
| embedding | 仅对 USGS 自有的 `place`、事件类型和许可明确的文本字段生成；不抓取或 embedding detail 中来源不明的附件、图片和第三方 product content |
| 模型暴露 | 可暴露规范化参数、来源、revision、时间、位置和许可内的短文本；必须同时给出“初报/已复核/已删除”等 `status`，不能把推测烈度或第三方贡献误写成已确认事实 |

USGS 明确把 GeoJSON 定义为面向应用的 programmatic interface；summary 包含 `id`、
`time`、`updated`、`status`、来源集合和 detail 链接，detail 还包含 contributor product
的状态与更新时间。[USGS GeoJSON summary](https://earthquake.usgs.gov/earthquakes/feed/v1.0/geojson.php)、
[USGS GeoJSON detail](https://earthquake.usgs.gov/earthquakes/feed/v1.0/geojson_detail.php)。
许可边界来自 [USGS Data Licensing](https://www.usgs.gov/data-management/data-licensing)：
联邦政府作品与第三方贡献必须分开判断，不能因为 payload 从 `usgs.gov` 返回就把其中
所有附件都视为公共领域。

### 2. NOAA/NWS 美国公共预警

| 字段 | 决策 |
|---|---|
| `source_id` | `noaa.nws.alerts.us.cap.v1` |
| endpoint | 索引 `https://api.weather.gov/alerts/active`；按索引返回的 alert URL 获取单条消息。索引优先 `application/geo+json`，单条原始证据请求 `Accept: application/cap+xml` |
| 格式 | GeoJSON/JSON-LD 索引 + OASIS CAP 1.2 XML 原始消息 |
| 更新频率 | 不快于每 30 秒；发送唯一 `User-Agent` 和运维联系方式；遵守缓存头、HTTP 状态码与退避 |
| 身份与修订 | CAP `sender + identifier + sent` 标识一条消息；事件系列由 `references` 串联，不用标题做身份 |
| 纠错/撤回 | 按 CAP `msgType=Update/Cancel/Error` 和 `references` 处理；保留 `status`、`scope`、`restriction`。从 active 索引消失只表示不再 active，不能制造一条 Cancel |
| 权威范围 | NWS 发布的美国 watches、warnings、advisories 等；不能扩张为其他国家的权威预警 |
| 许可 | NWS API 信息是可供任何目的使用的开放数据；NWS 页面材料除特别标注外为公共领域。不得声称为己有、暗示官方背书，或把改写内容呈现成官方材料 |
| raw 保留 | 每条唯一 CAP message 原文及 hash 保存 30 天；实际被角色感知、用于决策或后续被 Update/Cancel 引用的精确消息可长期冻结。第三方资源链接不跟抓 |
| normalized 保留 | alert series、area、有效期、Update/Cancel lineage 可长期保留；过期消息退出热索引但不改写历史 |
| embedding | 可对 NWS 自有 `headline/description/instruction` 生成；第三方数据、地图资产和附件默认禁用 |
| 模型暴露 | 仅 `status=Actual`、`scope=Public` 且未过期的消息可以进入 live 候选；Test/Exercise/Draft/System、Private/Restricted 只进入测试或按明确授权处理。角色看到改写摘要时必须同时获得原始来源和“摘要非官方原文”标记 |

NWS 明确说 alert API 可供第三方再分发和决策支持，建议请求间隔不少于 30 秒；API
支持 JSON-LD、GeoJSON、Atom 和原始 CAP。[NWS Alerts Web Service](https://www.weather.gov/documentation/services-web-alerts)、
[NWS API Web Service](https://www.weather.gov/documentation/services-web-api)。NWS 的
[再利用声明](https://www.weather.gov/disclaimer)同时要求不得把修改内容伪装成政府官方
材料，并提醒单独确认第三方产品许可。

CAP 1.2 的 `identifier`、`sender`、`sent`、`status`、`msgType`、`scope`、
`restriction`、`references`、`urgency`、`severity`、`certainty`、有效时间和区域字段
应原样保存；标准明确区分 Alert、Update、Cancel、Ack 与 Error。
[OASIS CAP 1.2](https://docs.oasis-open.org/emergency/cap/v1.2/CAP-v1.2-os.html)。这些字段
只控制事实生命周期、候选优先级和接触权限，不决定角色是否发消息或怎样反应。

### 2.1 WMO SWIC 全球 CAP：权威目录与 shadow 聚合，不是首批 live API

WMO Severe Weather Information Centre（SWIC 3.0）汇集各国国家气象和水文机构
（NMHS）发布的官方 CAP 警报，并提供 issuing organisation、语言、RSS/Atom CAP feed
和 WMO RAA 的目录页面：

- `https://severeweather.wmo.int/sources.html`
- `https://severeweather.wmo.int/feeds.html`

官方说明允许媒体和其他网站再利用 SWIC 信息，但要求注明实际发布警报的 RSMC、
TCWC 或 NMHS；同时明确并非所有区域都有可访问、可显示的 CAP feed。因此 SWIC 可作为
全球 authority/feed discovery 和完整性对照，但当前页面是 HTML/Demo 目录，不是有稳定
schema、SLA 和增量游标的正式聚合 API，不能直接成为首批 live evidence source。

生产边界如下：

- `wmo.swic.directory.v1` 可定期 shadow 抓取目录元数据，只保存 authority、feed URL、
  语言、可访问状态和抓取 hash，保留 30 天；
- 不对目录页面做 embedding，也不向角色模型暴露；
- 发现的每个 NMHS feed 必须单独验证 endpoint、CAP 合规性、更新/撤销语义和发布者的
  使用条款，再创建独立 `policy_revision`；
- SWIC 的“无警报”或“feed 不可访问”不能被解释为该地区没有风险；
- 若后续获得 WMO/HKO 提供的稳定聚合接口和书面模型处理边界，可另行评审 live，不能
  靠解析网页私有请求来补 API。

来源：[SWIC 3.0](https://severeweather.wmo.int/)、
[CAP source directory](https://severeweather.wmo.int/sources.html)、
[SWIC Notes to User](https://severeweather.wmo.int/note.html)。

## 中国权威来源

### 3. 中国地震台网中心：签约后生产

| 字段 | 决策 |
|---|---|
| `source_id` | `cenc.earthquake.cn.contract.<revision>` |
| endpoint | 不使用未文档化网页接口。通过中国地震台网中心定制服务取得固定的实时推送/API endpoint、认证方式和 schema 后登记 |
| 格式 | 以合同/技术附件为准；Adapter 必须保留自动速报、正式速报、人工复核、产品更新时间等来源状态 |
| 更新/纠错 | 合同必须明确同一事件的稳定 ID、自动报到正式报的 revision、撤销/误报、迟到修订和断线补发语义，否则不能上线 |
| 许可 | 合同必须明确自动获取、商业/非商业属性、缓存、派生摘要、embedding、向模型处理、向用户转述、精确 evidence 冻结和删除义务 |
| 数据边界 | 未签合同前不得采集公开网页来替代 API；签约后 raw/normalized/model 边界严格取合同的最小授权，不能套用 USGS policy |

国家地震科学数据中心说明中国地震台网中心是权威发布机构，可面向企事业单位提供
实时产品推送、定制接口和按年订阅；自动速报、正式速报和后续产品本身具有不同产出
时间和复核状态。[产品定制服务](https://data.earthquake.cn/cpdzfw/info/2025/334674087.html)、
[技术定制服务](https://data.earthquake.cn/jsdzfw/info/2025/334674091.html)。

公开共享规则将数据分为四级：一级可公开浏览下载，其余需要注册、合同或申请；用户
仅取得有限、非独占使用权，除一级或合同另有约定外不得转让或用于营利，成果须注明
来源。[地震科学数据共享管理办法](https://data.earthquake.cn/sjgxgz/info/2016/2344.html)。
此外，中心自 2026-03-26 起原则上不再公开原始观测数据和基本配置参数，转向加工
产品与定制服务。[开放范围公告](https://data.earthquake.cn/tzgg/info/2026/334674434.html)。
因此“网页能看到”不足以证明可长期抓取并送入模型。

### 4. 中国气象公共预警：登记存在，但当前 feed 不可用

WMO Register of Alerting Authorities 将中国气象局列为中国的官方 alerting authority，
并登记 CAP feed：

`http://www.12379.cn/rss/china_rss.xml`

但该地址在 2026-08-03 实测返回 HTTP 404，而不是 XML feed。WMO 登记证明发布者身份，
不证明 endpoint 仍健康，也不自动授予缓存、派生、embedding 或模型使用权。因此：

- `wmo.raa.cn` 只作为 authority discovery 元数据，不作为事实 feed；
- 上述 12379 地址状态为 **disabled / shadow health-check only**，不得进入 live；
- 不得根据网页上预警的消失自行生成 Cancel；
- 若 feed 恢复，仍需取得中国气象局/国家预警信息发布中心的现行使用条款，并验证
  CAP Update/Cancel、语言、有效期和历史补偿后再评审。

来源：[WMO 中国 alerting authority 登记](https://alertingauthority.wmo.int/authorities.php?recId=30)。
WMO 说明 CAP 是统一的多灾种预警格式，RAA 用于验证来源是否为指定区域的权威发布者；
这不是内容再利用许可证。[WMO CAP](https://wmo.int/activities/common-alerting-protocol-cap)。

中国天气网还提供 SmartWeatherAPI，覆盖预报、预警、资讯、雷达和云图等，但官方页面
要求填写申请表并经邮件审核；页面同时标注版权所有。它应登记为
`cma.smartweather.contract.<revision>`，状态为 **签约/审批后生产**，不能把公开页面
当作无需许可的接口。[SmartWeatherAPI](https://www.weather.com.cn/wzfw/smart/weatherapi.shtml)。
审批材料必须明确各产品的 endpoint、格式、更新周期、纠错/过期语义，以及 raw、
embedding、模型处理和 evidence 冻结权；没有这些条款时只允许连通性 shadow，且不保存
正文。

## RSSHub 与国内社交媒体

### RSSHub 只授权软件，不授权 feed 内容

RSSHub 官方仓库以 AGPL-3.0 发布；如果修改后的 RSSHub 通过网络向用户提供服务，需按
AGPL 的网络交互条款提供对应源代码。该义务针对 RSSHub 程序，不改变微博、B 站、
小红书、抖音、知乎作者内容的版权、平台协议或个人信息权利。

来源：[RSSHub 仓库与许可证](https://github.com/DIYgod/RSSHub)、
[审计时固定的 AGPL-3.0 文件](https://github.com/DIYgod/RSSHub/blob/9caca197b34f3ec6c0df911723d0b7495539820b/LICENSE)。
下列路由实现审计也固定在 commit
[`9caca197`](https://github.com/DIYgod/RSSHub/tree/9caca197b34f3ec6c0df911723d0b7495539820b/lib/routes)。

### Route 风险分级

| 平台 / route | 技术事实与许可事实 | 状态 | raw / embedding / 模型边界 |
|---|---|---|---|
| 微博 `/weibo/search/hot`、`/weibo/user/:uid` | Adapter 得到的是 RSSHub 生成的 RSS item；上游来自移动端 JSON/HTML。实现可使用 Cookie、Playwright、移动端接口和 visitor cookie；这是 transport 实现，不是微博官方授权。微博当前已有面向 Agent 的官方 CLI/API、热搜与内容检索及订阅方案，应优先申请官方通道 | RSSHub 公共热榜/公开用户元数据仅 shadow；任何登录 Cookie、关注流、分组、收藏 route 禁止。官方 CLI/API 在确认套餐条款和数据用途后可另行申请生产 | shadow 最多保存 24 小时最小元数据与响应 hash，禁全文、媒体下载、embedding、模型暴露和 durable snapshot |
| Bilibili `/bilibili/user/video/:uid`、`/bilibili/user/dynamic/:uid` | Adapter 得到 RSSHub 生成的 RSS item；上游是站点 JSON/HTML，存在 Cookie 或浏览器 fallback。关注、收藏、消息等 route 明确依赖 `SESSDATA`/登录 Cookie。Bilibili 官方开放平台要求主体审核、应用接入和 UP 主授权，数据仅限申请目的且按必要期限保存 | 无 Cookie 的公开投稿元数据仅 shadow；动态全文和所有 Cookie/关注/收藏/消息 route 禁止。通过官方开放平台获得明确授权的关联 UP 主数据可另立生产 policy，但不构成全站感知来源 | shadow 24 小时元数据/hash；不下载视频、弹幕、评论或封面，不 embedding，不给模型。官方 API 数据严格按授权目的和最短期限处理 |
| 知乎 `/zhihu/hot`、`/zhihu/question/:id` 及其他 routes | RSSHub 输出 RSS，但上游实现生成 `x-zse-96` 签名、guest/login cookies，部分 route 依赖登录 Cookie。知乎现行协议明确禁止未经许可的自动化程序接入、收集/处理，以及爬取、抓取、模拟下载等 | **禁止**，除非取得知乎事先书面许可并改走被许可接口 | 不 fetch、不缓存、不 embedding、不暴露给模型 |
| 抖音 `/douyin/user/:uid` 等 | RSSHub 输出 RSS，但上游 route 标记 `requirePuppeteer` 与 `antiCrawler`，通过 Playwright 截获未文档化的 `/web/aweme/post`；官方开放平台的数据能力要求应用接入、scope 和用户/经营关系授权，且限制超目的使用与第三方披露 | RSSHub routes **禁止**。只有官方开放平台明确批准的具体 scope 可另立受限 policy；它通常不提供“任意公开内容趋势 feed”授权 | 未获官方 scope 前全禁；获批后按授权用户、目的、期限最小化，模型/embedding 需在书面用途内 |
| 小红书 `/xiaohongshu/user/:id/...` | RSSHub 输出 RSS，但上游 route 标记 `antiCrawler`、`requirePuppeteer`，可注入登录 Cookie 并抓取笔记、收藏与全文；小红书公开开放平台资料面向小程序/电商接入，未发现授权任意公开笔记聚合、长期保存或模型处理的官方接口 | **禁止**，除非小红书提供书面许可和专用接口 | 不 fetch、不缓存、不 embedding、不暴露给模型；不得保存 Cookie、收藏或全文 |

路由实现证据：
[微博 routes](https://github.com/DIYgod/RSSHub/tree/9caca197b34f3ec6c0df911723d0b7495539820b/lib/routes/weibo)、
[Bilibili routes](https://github.com/DIYgod/RSSHub/tree/9caca197b34f3ec6c0df911723d0b7495539820b/lib/routes/bilibili)、
[知乎 routes](https://github.com/DIYgod/RSSHub/tree/9caca197b34f3ec6c0df911723d0b7495539820b/lib/routes/zhihu)、
[抖音 routes](https://github.com/DIYgod/RSSHub/tree/9caca197b34f3ec6c0df911723d0b7495539820b/lib/routes/douyin)、
[小红书 routes](https://github.com/DIYgod/RSSHub/tree/9caca197b34f3ec6c0df911723d0b7495539820b/lib/routes/xiaohongshu)。

平台一手资料：微博官方已经提供 Agent CLI/API、内容检索和热搜能力，并按套餐提供
调用额度，[微博开放平台 CLI](https://open.weibo.com/cli/index)；Bilibili 开放平台只在
主体审核、应用和用户授权范围内开放数据，且要求目的限定、最短保存期限和第三方处理
协议，[开放平台能力](https://open.bilibili.com/doc)、
[开发者服务协议](https://open.bilibili.com/agreement/developer-service)；知乎协议明确限制
未经授权的自动化采集，[知乎协议](https://www.zhihu.com/term/zhihu-terms)；抖音开放平台
限制接口范围和数据用途，未经事先书面同意不得超目的使用或向第三方提供，
[抖音开放平台服务协议](https://developer.open-douyin.com/docs/resource/zh-CN/developer/operation-norm/platform-protocol/register-protocol/)、
[用户数据能力](https://developer.open-douyin.com/docs/resource/zh-CN/mini-app/open-capacity/basic-capacities/douyin/)；
小红书目前可核验的官方入口是需要登录并接受专项协议的小程序/电商开放平台，不能据此
推导出公共内容抓取权，[小红书小程序开放平台](https://miniapp.xiaohongshu.com/doc/DC717727)、
[小红书电商开放平台](https://open.xiaohongshu.com/document/developer/file/53)。

### RSSHub shadow 的通用语义

RSS/Atom item 的 GUID、发布时间和是否仍出现在 feed 中，不能自动等价为上游事件 ID、
修订时间或正式撤稿。对允许 shadow 的微博/Bilibili 最小元数据：

- 同一 platform object URL + upstream ID + 新 payload hash 只能记录为候选 revision；
- item 消失记为 `availability_unknown`，不是 deletion/correction；
- 没有上游正式 correction 链时，不得提升为权威事实；
- RSSHub 故障、Cookie 过期、反爬或 route schema 改变必须标记 transport failure，不能
  解释为“外界没有发生事情”或“角色没看到”；
- source authority 永远是上游发布者/作者，`transport=RSSHub` 单独记录；
- shadow 数据不进入角色注意力模型，所以也不能间接影响情绪、主动联系或事件机。

## 首批 source allowlist

### 可上线

| source_id | 最小 endpoint allowlist | 生产权限 |
|---|---|---|
| `usgs.earthquake.global.v1` | `earthquake.usgs.gov/earthquakes/feed/v1.0/summary/all_hour.geojson`、`all_day.geojson` 和返回的同域 detail URL | fetch、受限 raw、normalize、元数据 embedding、角色模型候选、被采用 revision 的 durable snapshot |
| `noaa.nws.alerts.us.cap.v1` | `api.weather.gov/alerts/active` 与索引返回的同域 alert URL | fetch、受限 raw、normalize、许可文本 embedding、角色模型候选、被采用 CAP 的 durable snapshot |

### 仅 shadow / 等待正式许可

| source | 条件 |
|---|---|
| `cenc.earthquake.cn.contract.*` | 未签约时只保留接口申请与 schema 验证记录；合同完整覆盖自动使用和模型处理后转生产 |
| `cma.smartweather.contract.*` | 审批后只对固定 endpoint 做连通性和 revision shadow；许可字段完整后转生产 |
| `wmo.swic.directory.v1` | 只抓 authority/feed 目录元数据；不把 SWIC HTML 当稳定聚合 API，不向模型开放 |
| `wmo.raa.cn` | 仅 authority discovery；12379 登记 feed 当前 404，不提供事实信号 |
| RSSHub 微博公开热榜/公开用户最小元数据 | 仅隔离非生产、无账号 Cookie 的 24 小时质量试验；官方微博 API 应替代它 |
| RSSHub Bilibili 公开投稿最小元数据 | 仅隔离非生产、无账号 Cookie 的 24 小时质量试验；不跟媒体与评论 |

### 禁止

- 所有需要个人 Cookie、token、二维码登录、私有关注流、收藏、消息、评论后台或私信的
  route；
- RSSHub 知乎全部 routes，除非取得知乎事先书面许可；
- RSSHub 抖音、小红书 routes，以及依赖 Playwright/Puppeteer 捕获未文档化接口、反爬
  绕过、验证码处理、逆向签名或媒体代理的 route；
- 未文档化的 CENC、CMA、12379 JSON/XML endpoint 和公共页面 scraping；
- 任意把搜索结果、聚合器摘要或 RSSHub 输出当作地震、气象、公共安全唯一权威来源的
  adapter；
- 任意把上游作者正文、图片、视频、评论或个人资料直接写入 V2 永久账本、embedding
  索引或第三方模型，而没有逐来源、逐用途的明确许可。

## 上线时必须冻结的 policy revision

每个生产来源必须把以下内容作为不可变配置和审计证据提交：

1. endpoint/host/path allowlist、schema 版本、请求头、轮询频率、限流与退避；
2. authority、transport、item identity 与 revision identity 的分离规则；
3. Update、Cancel、Delete、Error、迟到修订、feed 消失和断线补偿的语义；
4. 当前条款/合同的 URL、PDF/hash、生效日期、账户主体和允许用途；
5. raw、normalized、embedding、模型 exposure、逐字引用和 durable snapshot 的独立开关
   与保留期限；
6. 第三方附件、用户内容、个人信息和位置数据的排除规则；
7. 许可撤回或来源删除后的删除流程，同时保留不可变 V2 中最小的“当时实际感知”事实
   与不含受限正文的来源/hash 证明；
8. 测试/演习消息隔离、来源健康告警、最后成功 revision、积压和 stale 状态。

生产 Adapter 不得接受模型生成的 URL、route、query 或凭据。模型只能在已许可、已冻结
且与当前 Pinned Turn 绑定的小型候选集合中决定注意、理解与后续行为。

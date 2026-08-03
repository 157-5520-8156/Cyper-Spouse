# 国内外界感知来源：主流社交媒体、新闻与公共预警

状态：2026-08-03 调研基线；只使用平台、发布机构、RSSHub 项目自身的一手资料。

> 运行验收修正（同日）：随后按生产适配器实际读取条目发现，新华网五个中文 RSS
> 虽然均返回 `200 XML`，但首批内容分别停留在 2013–2023 年且没有 `pubDate`；它们
> 不能代表当前外界。生产注册表因此保留其审计登记但默认禁用，当前只启用确有新内容和
> 可解析发布时间的人社部新闻 RSS。下面的“live_ready”是许可/transport 评审结果，
> 不再等同于已启用；恢复新华网源前必须另行通过内容新鲜度验收。

本文补充
[`external-world-perception-authoritative-sources.md`](./external-world-perception-authoritative-sources.md)，
专门回答“国内哪些来源值得先接、哪些技术上能抓但不能上线”。它不是法律意见，也不因
列出 endpoint 就自动授权生产使用。生产登记仍须冻结当时有效的接口协议、许可范围、
`policy_revision` 和运维负责人。

## 结论

国内源应优先，首批不应把微博、知乎、B 站、小红书、抖音和微信公众号的 RSSHub
网页抓取路由一股脑打开。最可行的顺序是：

1. **立即可进入 live 发布评审**：新华网自己公开的中文 RSS、以及人社部自己公开的
   RSS。它们是真正由发布者提供给 RSS 阅读器的机器源，不需要 Cookie、浏览器自动化
   或反爬绕过。
2. **拿到官方凭据后 live**：微博官方 API/CLI 的热搜、趋势和检索；申请获批后的抖音
   热点 OpenAPI。角色看到的是“某平台此刻把什么列为热点”，不能把热榜里的主张自动
   当作事实。
3. **先做隔离 shadow，再取得明确使用边界**：国家应急广播、中央气象台、中国地震局
   的公开预警/速报入口。它们的发布者身份可靠，RSSHub 路由也不依赖登录，但目前未找到
   授权自动保存、embedding 和模型处理的公开接口协议。
4. **不得上线**：要求个人 Cookie、Puppeteer/Playwright、SafeLine 求解、未公开签名、
   第三方公众号聚合或平台协议明示禁止自动采集的来源。

这不是因为国内信息“不好接”，而是要区分三个不同问题：发布者是否权威、接口是否
稳定、内容是否允许被自动采集和送入模型。RSSHub 只能解决部分 transport 问题，不能
替上游授予内容许可。

## 分级与项目语义

| 级别 | 在 Girl-Agent 中的含义 |
|---|---|
| `live_ready` | 可进入生产注册表评审；仍须固定 endpoint、hash、许可和限流策略 |
| `credential_gated_live` | 只能在取得官方应用、scope、token 和协议快照后 live |
| `shadow_pending_rights` | 只做隔离连通性、修订和质量验证；不进 World V2、角色模型、embedding、Life Ecology 或发送链 |
| `forbidden` | 不采集；shadow 也不是规避平台条款的许可 |

来源 allowlist 只证明“系统可以把一条候选放到角色面前”，不决定她是否注意、相信、
产生情绪、改变生活或联系用户。社交热榜尤其只能形成 `platform_trend_signal`，不能形成
“热榜中的说法已经发生”的事实。

## 建议的首批 allowlist

### A. 可立即进入 live 发布评审：发布者原生 RSS

#### A1. 新华网中文 RSS

新华网在自己的“RSS 聚合新闻”页面公开列出各频道 XML 地址，并说明网站由新华社主办。
官方列表见[新华网 RSS 聚合新闻](https://www.news.cn/linktous.htm)。2026-08-03 对以下
HTTPS 地址做了只读连通性检查，均返回 `200` 和 XML：

| 建议 `source_id` | endpoint | 用途 | 首批限制 |
|---|---|---|---|
| `cn.news.xinhua.local.v1` | `https://www.xinhuanet.com/local/news_province.xml` | 国内地方与城市动态 | 每轮最多 8 个新 revision |
| `cn.news.xinhua.edu.v1` | `https://www.xinhuanet.com/edu/news_edu.xml` | 教育、校园和青年相关公共情境 | 每轮最多 5 个 |
| `cn.news.xinhua.tech.v1` | `https://www.xinhuanet.com/tech/news_tech.xml` | 科技与产品趋势 | 每轮最多 5 个 |
| `cn.news.xinhua.ent.v1` | `https://www.xinhuanet.com/ent/news_ent.xml` | 文娱公共话题 | 每轮最多 5 个 |
| `cn.news.xinhua.health.v1` | `https://www.xinhuanet.com/health/news_health.xml` | 公共健康资讯 | 每轮最多 3 个；医疗主张保留来源不作建议 |

建议不在首批打开所有时政、国际、财经、军事频道。国内优先不等于信息越多越好；先让
去重、注意力和修订链在与角色日常较相关的五类信息上稳定运行。需要宏观新闻时，由角色
通过已经授权的搜索 Action 获取，比长期灌入整个新闻流更符合“受控的高随机”。

保存边界：长期保存 publisher、标题、发布时间、canonical URL、feed revision hash
和角色自行形成的有来源理解；`may_quote=false`。不抓文章全文、图片或视频，不把新闻
摘要变成角色亲历事实。

#### A2. 人力资源和社会保障部 RSS

人社部官方“RSS 信息直通车”明确说这些栏目用于让 RSS 阅读器自动获得更新，并公布
新闻动态、政策法规和政策解读地址。[人社部 RSS 信息订阅](https://www.mohrss.gov.cn/SYrlzyhshbzb/zxhd/RSS/xxdy/)。
2026-08-03 实测新闻动态 HTTPS endpoint 返回 `200 application/xml`；另外两个 endpoint
当日返回 `403`，因此只允许健康的一个进入首批：

| 建议 `source_id` | endpoint | 状态 |
|---|---|---|
| `cn.gov.mohrss.news.v1` | `https://www.mohrss.gov.cn/SYrlzyhshbzb/dongtaixinwen/SYtupianxinwen/rss.xml` | `live_ready`，每轮最多 3 个新 revision |
| `cn.gov.mohrss.policy.v1` | `https://www.mohrss.gov.cn/gkml/xxgk/rss.xml` | disabled，恢复 `200 XML` 前不启用 |
| `cn.gov.mohrss.explainer.v1` | `https://www.mohrss.gov.cn/gkml/zcjd/rss.xml` | disabled，恢复 `200 XML` 前不启用 |

这类源可以为实习、就业、社保等公共环境提供素材，但不能直接改变角色的学业/职业状态；
只有角色模型在其真实生活情境中形成计划并经 World V2 正常结算，才会影响生活线。

### B. 官方凭据到位后 live：国内社交趋势

#### B1. 微博官方 API/CLI

微博开放平台当前官方 CLI 页面明确提供关键词/用户检索、热搜与话题趋势，并公开免费与
付费调用额度；所有能力都从微博登录和官方 API 身份开始，而不是复制个人网页登录
Cookie。[微博开放平台 CLI](https://open.weibo.com/cli/index)。

建议只登记两个能力：

- `cn.social.weibo.trends.v1`：热搜与话题趋势；
- `cn.social.weibo.search.authorized.v1`：角色通过 Action 明确发起的关键词/用户检索。

上线条件：完成官方应用/账号认证，冻结实际命令/API schema、scope、配额、服务条款和
计费方案；token 进入 secrets，不写进 source registry。趋势项只保存榜单位置、标题、
URL、观测时间和 revision hash，短 TTL，不保存评论、图片、视频或个人画像。

RSSHub 虽然存在
[`/weibo/search/hot`](https://github.com/DIYgod/RSSHub/blob/49970c8276499ae47418157cd46eea52995dbf6f/lib/routes/weibo/search/hot.tsx)
等路由，但 RSSHub 自己的
[`weibo/namespace.ts`](https://github.com/DIYgod/RSSHub/blob/49970c8276499ae47418157cd46eea52995dbf6f/lib/routes/weibo/namespace.ts)
说明大部分路由需要 `WEIBO_COOKIES`，否则尝试用 Puppeteer 取得访客 Cookie，还存在地域
CDN 差异。既然微博已经提供官方 Agent/API 通道，生产不应再选 Cookie 抓取旁路。

#### B2. 抖音官方热点 OpenAPI

抖音官方把 `hotsearch` 标为默认关闭、需在管理中心申请的“抖音数据权限”。
[用户类型及权限说明](https://open.douyin.com/platform/resource/docs/accession-guide/type-and-permission)。
其“热门视频榜”接口使用 `data.external.billboard_hot_video` scope，不需单个用户授权，
但需要应用权限与 `client_token`；数据统计最近 24 小时并按日产生。
[抖音热门视频榜 OpenAPI](https://open.douyin.com/platform/resource/docs/openapi/data-open-service/tops-data/hot-video-list/hot-video-list/)。

建议登记：

- `cn.social.douyin.hot_video.v1`：获批后的热门视频榜；
- 其他热点接口逐项申请、逐项登记，不能用一个 scope 推定全部榜单均获授权。

RSSHub 的抖音 user/hashtag/live 路由全部依赖 Playwright 且标记严格反爬，官方 namespace
也说明视频 CDN 校验 Referer；参见
[`douyin/namespace.ts`](https://github.com/DIYgod/RSSHub/blob/49970c8276499ae47418157cd46eea52995dbf6f/lib/routes/douyin/namespace.ts)。
因此 RSSHub 抖音路由归入 `forbidden`，官方 OpenAPI 才是生产路径。

### C. 国内权威公共预警：先 shadow，明确边界后 live

这些来源应优先于 USGS/NWS 影响角色的国内公共情境，但不能因“公共安全信息应该传播”
就推定网站内部接口允许长期抓取和模型处理。

#### C1. 国家应急广播

国家应急广播由中央广播电视总台主办，官方说明网站汇集权威部门数据、发布预警与救援
信息，目标是服务公众应急需要。[国家应急广播关于我们](https://www.cneb.gov.cn/tyl/gywm/)、
[预警信息页面](https://www.cneb.gov.cn/yjxx/)。

RSSHub 当前存在
[`/cneb/yjxx/:level?/:province?/:city?`](https://github.com/DIYgod/RSSHub/blob/49970c8276499ae47418157cd46eea52995dbf6f/lib/routes/cneb/yjxx.ts)，
可按红/橙/黄/蓝和省市过滤，无 Cookie、无浏览器、未标反爬。源码从中央人民广播电台
`gdapi.cnr.cn` 获取结构化列表。2026-08-03 对该结构化入口做只读探测返回 `200 JSON`。

建议 `source_id=cn.alert.cneb.public.v1`，初始状态 `shadow_pending_rights`。取得运营方对
自动抓取、短期缓存、模型处理和转述的确认后再 live；live 时只保存预警标题、发布单位、
等级、地区、发布时间、canonical URL 和 hash。正文不长期存储、不 embedding、不逐字
转发。

#### C2. 中央气象台全国气象预警

国家气象中心（中央气象台）是中国气象局所属事业单位，职责包括制作和发布全国及全球
天气预报和预警。[中央气象台关于我们](https://m.nmc.cn/publish/cms/view/6cd4105faa764f8d9031c5b7adf9f129.html)。

RSSHub 当前存在
[`/nmc/weatheralarm/:province?`](https://github.com/DIYgod/RSSHub/blob/49970c8276499ae47418157cd46eea52995dbf6f/lib/routes/nmc/weatheralarm.ts)，
可按省过滤，无 Cookie/浏览器且未标反爬；源码调用 `www.nmc.cn/rest/findAlarm`。2026-08-03
该入口返回 `200 JSON`，但上游仍是 HTTP，缺少公开 API/再利用协议。

建议 `source_id=cn.alert.nmc.public.v1`，初始 `shadow_pending_rights`。不要接 RSSHub 的
`/cma/channel` 作为替代：其当前源码明确标记 `antiCrawler: true`，并实现 SafeLine
challenge 求解，见
[`cma/channel.tsx`](https://github.com/DIYgod/RSSHub/blob/49970c8276499ae47418157cd46eea52995dbf6f/lib/routes/cma/channel.tsx)。
若需要正式 API，应走中国天气网要求申请审核的
[SmartWeatherAPI](https://www.weather.com.cn/wzfw/smart/weatherapi.shtml)。

#### C3. 中国地震局与中国地震台网中心

RSSHub 当前有两条路线：

- [`/earthquake/:region?`](https://github.com/DIYgod/RSSHub/blob/49970c8276499ae47418157cd46eea52995dbf6f/lib/routes/earthquake/index.ts)
  调用中国地震局结构化接口，但 route 标记 `antiCrawler: true`；2026-08-03 只读探测接口
  返回 `200`，当时国内列表为空。
- [`/earthquake/ceic/:type?`](https://github.com/DIYgod/RSSHub/blob/49970c8276499ae47418157cd46eea52995dbf6f/lib/routes/earthquake/ceic.ts)
  仍指向旧 `ceic.ac.cn/ajax/speedsearch`；2026-08-03 实测已跳到 404，不能因 route 仍在
  仓库就判断其可用。

国家地震科学数据中心提供大震速报目录，也说明中国地震台网中心可提供实时产品推送和
定制接口。[国家台网大震速报目录](https://data.earthquake.cn/datashare/report.shtml?PAGEID=earthquake_dzsb)、
[产品定制服务](https://data.earthquake.cn/cpdzfw/info/2025/334674087.html)。生产应优先
签约正式接口，`source_id=cenc.earthquake.cn.contract.<revision>`；在此之前，中国地震局
公开入口最多为 `shadow_pending_rights`，旧 CEIC 路由直接 disabled。

## 主流平台逐项结论

| 平台 | RSSHub 2026-08-03 实际情况 | 官方边界 | 决策 |
|---|---|---|---|
| 微博 | user/keyword/hot/super-index 存在；大多需要 Cookie/Puppeteer | 官方已有检索与趋势 API/CLI | 官方 API `credential_gated_live`；RSSHub Cookie 路由 `forbidden` |
| 知乎 | hot/question/topic/user 等存在；自 2024-07 大部分全文需 `ZHIHU_COOKIES` | 2025 协议禁止未经许可的自动化接入、爬取和处理 | `forbidden` |
| B 站 | popular/ranking/hot-search 无登录；动态常需 Cookie/Playwright | 开放平台主要提供关联 UP 主授权数据；开发者协议禁止未经书面同意以机器人/爬虫取数 | 普通热榜抓取也 `forbidden`；只有获批官方能力可重新评审 |
| 小红书 | user route 要浏览器、标反爬，完整笔记通常需 `XIAOHONGSHU_COOKIE`；无官方热榜 route | 当前官方开放文档面向电商，未找到公共内容趋势 API | `forbidden` |
| 抖音 | user/hashtag/live 全部 Playwright + strict anti-crawler | 官方热点 scope 可申请 | RSSHub `forbidden`；官方 OpenAPI `credential_gated_live` |
| 微信公众号 | 只有少数 homepage/msgalbum 直连；通用方案依赖搜狗、Wechat2RSS、CareerEngine、Telegram 等第三方 | 官方素材 API管理调用方自己的公众号素材，不是任意公众号聚合 API | 第三方聚合和任意公众号抓取 `forbidden`；自有/明确授权公众号可单独登记 |

依据：

- RSSHub 当前官方源码快照为提交
  [`49970c8`](https://github.com/DIYgod/RSSHub/commit/49970c8276499ae47418157cd46eea52995dbf6f)。
  微博、知乎、抖音、微信公众号的项目级限制分别见
  [`weibo/namespace.ts`](https://github.com/DIYgod/RSSHub/blob/49970c8276499ae47418157cd46eea52995dbf6f/lib/routes/weibo/namespace.ts)、
  [`zhihu/namespace.ts`](https://github.com/DIYgod/RSSHub/blob/49970c8276499ae47418157cd46eea52995dbf6f/lib/routes/zhihu/namespace.ts)、
  [`douyin/namespace.ts`](https://github.com/DIYgod/RSSHub/blob/49970c8276499ae47418157cd46eea52995dbf6f/lib/routes/douyin/namespace.ts)、
  [`wechat/namespace.ts`](https://github.com/DIYgod/RSSHub/blob/49970c8276499ae47418157cd46eea52995dbf6f/lib/routes/wechat/namespace.ts)。
- [知乎协议（2025-03-25 生效）](https://www.zhihu.com/term/zhihu-terms)明确禁止未经
  知乎授权或许可，用自动化程序接入、收集或处理信息，并列举爬取、抓取等行为。
- [哔哩哔哩开放平台](https://open.bilibili.com/doc)将数据开放描述为授权用户/关联
  UP 主数据；[开发者服务协议](https://open.bilibili.com/agreement/developer-service)明确
  禁止未经书面同意使用机器人、蜘蛛、爬虫等自动程序获取数据。
- [小红书开放平台](https://open.xiaohongshu.com/document/developer/file/53)当前呈现为
  电商开放平台；RSSHub 的
  [`xiaohongshu/user.ts`](https://github.com/DIYgod/RSSHub/blob/49970c8276499ae47418157cd46eea52995dbf6f/lib/routes/xiaohongshu/user.ts)
  同时标记浏览器、反爬和 Cookie 依赖。
- 微信官方[获取永久素材列表](https://developers.weixin.qq.com/doc/service/api/material/permanent/api_batchgetmaterial)
  属于服务号自身素材管理接口。RSSHub 官方也明确说公众号直接抓取困难，通用方案主要是
  间接来源，不能把第三方聚合当成微信官方授权。

## 国内新闻 RSSHub 路由：技术存在不等于内容获授权

RSSHub 官方源码确认存在央视、新华社、澎湃、人民网和中新网路由，其中央视、新华社、
澎湃的主要 route 无 Cookie/浏览器、未标反爬：

- [央视新闻联播 route](https://github.com/DIYgod/RSSHub/blob/49970c8276499ae47418157cd46eea52995dbf6f/lib/routes/cctv/xwlb.ts)
- [新华社新闻 route](https://github.com/DIYgod/RSSHub/blob/49970c8276499ae47418157cd46eea52995dbf6f/lib/routes/news/xhsxw.ts)
- [澎湃首页头条 route](https://github.com/DIYgod/RSSHub/blob/49970c8276499ae47418157cd46eea52995dbf6f/lib/routes/thepaper/featured.ts)
- [人民网 route](https://github.com/DIYgod/RSSHub/blob/49970c8276499ae47418157cd46eea52995dbf6f/lib/routes/people/index.ts)
- [中新网 route](https://github.com/DIYgod/RSSHub/blob/49970c8276499ae47418157cd46eea52995dbf6f/lib/routes/chinanews/index.ts)

但生产不能因此抓全文。央视声明新闻作品是重要版权资产；澎湃明确反对未经授权大规模
转载；中新网法律声明要求使用其版权稿件时取得授权；人民网提供单独的文字、图片和
大模型语料版权合作服务。来源分别见
[央视新闻作品版权声明](https://news.cctv.com/2017/04/26/ARTI9neH8KQH2RzzhkOjEsBZ170426.shtml)、
[澎湃反侵权声明](https://www.thepaper.cn/newsDetail_forward_1289663)、
[中新网法律声明](https://www.chinanews.com.cn/end_navigation/11.html)、
[人民网版权服务](https://gonggao.people.com.cn/)。

因此首批新闻选择发布者明确提供的新华网 RSS；央视、澎湃、人民网、中新网 RSSHub
route 只有在取得书面许可或确定其现行公开机器订阅条款后，才可加入 live。未经许可时
既不能把全文存入 sidecar，也不能用“只给模型看、不对外展示”规避许可。

RSSHub 自身采用
[AGPL-3.0](https://github.com/DIYgod/RSSHub/blob/49970c8276499ae47418157cd46eea52995dbf6f/LICENSE)，
这只授权 RSSHub 代码，不授权任何上游新闻、帖子、图片或视频。

## 明确不可上线项

1. 个人微博、知乎、B 站、小红书、抖音或微信 Cookie；角色没有真实登录历史，Cookie
   也不能作为 `PerceptionChannel` 的人格合理性证明。
2. Puppeteer/Playwright 模拟登录、SafeLine/验证码求解、逆向签名、设备指纹伪装或
   IP/账号轮换。
3. RSSHub 公共实例。生产只能使用自托管、固定版本和精确 route allowlist 的 gateway，
   并把上游 publisher 与 transport provider 分开记录。
4. Wechat2RSS、搜狗、CareerEngine、Telegram 转发等公众号间接聚合；除非每个具体号和
   中间服务均提供可审计授权。
5. 新闻、帖子、评论、图片、音视频全文的长期缓存、embedding、二次分发或逐字引用；
   `may_quote` 默认 false。
6. 用热搜、热榜、评论量或转发量证明事件真假。趋势排名只证明该平台给出了这个排名。
7. 未经用户明确授权，用用户位置过滤灾害、同城或本地生活源。城市级位置权限必须独立
   于新闻订阅，并可撤销。

## 上线顺序与验收

1. **第一批（现在）**：只启用五个新华网 HTTPS RSS 加人社部一个健康 RSS；关闭原先
   国外普通新闻候选，USGS/NWS 仅保留为灾害交叉验证而不参与日常趋势。
2. **第二批（凭据）**：申请并配置微博官方趋势 API；申请抖音 `hotsearch`/
   `data.external.billboard_hot_video`。凭据未到位时 source 显示
   `credential_missing`，不能回退到 RSSHub 抓取。
3. **第三批（shadow）**：自托管固定提交的 RSSHub，只开放 CNEB/NMC/CEA 三个精确
   route 做隔离质量测试；验证稳定 ID、更新/解除语义、延迟、空结果和修订后，再取得
   使用边界并决定 live。
4. **暂不接**：知乎、B 站、小红书、任意公众号和媒体全文 route。若未来平台提供官方
   public-content API 或取得书面许可，再创建新的 `policy_revision`，不修改旧记录冒充
   一直有权限。

每批验收至少包括：

- 来源与 transport 身份分离，canonical URL 和 revision hash 可审计；
- 同一新闻/预警跨源去重但不合并来源分歧；
- 热榜项在 6 小时后退出候选，不进入长期角色事实；
- 普通新闻每 10 分钟合批，模型可选零条；预警可立即形成考虑机会但不能自动生成消息；
- 新闻正文、媒体和评论没有进入 raw 长期存储或 embedding；
- `/health` 能区分 endpoint 故障、凭据缺失、权利未批准、模型未注意和角色选择沉默；
- 连续 24 小时 shadow 中不存在 RSSHub 登录挑战、验证码、个人 Cookie 或第三方聚合请求。

这套分级保留了国内信息的广度，也避免把“抓得到”误写成“可信、获授权且应该被角色
知道”。真正的多样性由角色模型对少量、有来源、彼此可能冲突的候选自行注意和解释，
不是靠把所有榜单永久塞进上下文。

# 专用媒体渲染器的 Serverless 演进计划

## 状态与边界

状态：已搁置。**当前不接入 Salad 或其他 Serverless 平台；高档路线继续使用 Civitai 的受审阅
Krea2 模板工作流。** 本文件保留为以后需要替换执行平台时的设计与验收依据。

目标不是把所有图片都迁到 GPU 平台。普通生活分享与常规角色媒体继续使用
`ordinary_openai_image2`；只有已经由世界机授权并进入
`adult_suggestive` / `adult_explicit` 能力路线的冻结计划，才是本设计的候选。
平台选择不替代现有的年龄、授权、关系、地域或投递校验。

按秒计费解决的是闲置成本，不能保证低延迟：容器冷启动、模型/LoRA 加载、排队和结果回传都可能比
实际推理更慢。因此本计划首先解决异步、可恢复和不重复计费，再比较 GPU 速度。

## 现状与缺口

当前 Civitai 模板执行器已经会把 `workflow_id` 写入本地 sidecar receipt，且超时后会报告
`workflow_timeout:<id>`，避免盲目重复提交。这是正确的安全下限，但还不是完整的异步系统：调用仍会在
一次请求中轮询，超过等待窗口后需要人工/临时逻辑继续查询。

未来的 Serverless 不能把这一缺口原样搬过去。无论 provider 是 Civitai 还是自管 ComfyUI，均应通过同一份
持久化的 `ExternalRenderJob` 被恢复和对账。

## 目标架构

```text
Frozen MediaPlan + PrivateRenderContract
          │                 （只冻结语义与 render_route）
          ▼
Specialized Render Profile Resolver
          │                 （运行时仅解析已批准的 profile）
          ▼
ExternalRenderJob submit
          │
          ├── Civitai template workflow
          └── Serverless ComfyUI queue worker
          ▼
webhook / reconciliation poller
          ▼
immutable artifact + hash + actual cost
          ▼
MediaGenerated / failure Action → delivery
```

### 职责

- `MediaPlanner`、Hermes 和 World 不认识 GPU 厂商、容器、节点图或 LoRA 权重；它们只产生并冻结媒体语义。
- `render_route` 仍是能力键（如 `adult_suggestive`），不是厂商键。这样迁移不会污染分类矩阵或重写计划。
- `Specialized Render Profile` 绑定经审阅的 provider、区域、容器镜像 digest、ComfyUI/custom node 版本、底模与
  LoRA manifest/hash、工作流模板 hash、输入节点白名单、成本上限和内容合规状态。
- 对一次已提交任务，profile 版本、workflow hash、seed、LoRA 权重与 provider job ID 必须冻结；恢复、回调和
  重试不得重抽 Hermes 候选、重写提示词或改变档位。
- 高档路线仍不接收普通身份参考图。身份由 Krea2 Celia LoRA + 已批准 workflow 负责；不会因为换到
  Serverless 而重新把参考图泄漏进该路线。

## `ExternalRenderJob` 合同

提交成功后立即持久化以下最小信息；不能等图片完成才记录：

| 字段 | 用途 |
|---|---|
| `job_id` / `media_request_id` | 本地稳定标识与回放关联 |
| `provider_kind` / `profile_id@version` | 解释实际使用的执行环境 |
| `workflow_hash` / `input_hash` / `seed` | 保证同一冻结输入可审计 |
| `idempotency_key` | `media_request_id + workflow_hash`；防重复扣费 |
| `remote_job_id` | 后续查询、取消、回调关联 |
| `state` / `state_changed_at` | 生命周期恢复 |
| `attempt` / `cost_quote` / `actual_cost` | 预算与费用对账 |
| `artifact_uri` / `artifact_hash` | 不可变成品归档 |

统一状态为：

```text
not_submitted → submitted → queued → preparing → running
                                      ↘
       succeeded / failed / cancelled / expired / unknown
```

`unknown` 不是失败重投的同义词：只要远端句柄可能已经创建，就必须先对账。只有没有获得句柄且可证明
请求没有被接受的传输失败，才可以用相同幂等键有限重试。不得自动跨 provider、跨 profile 或降到普通图片路线。

Webhook 和 polling 都只调用一个 `ingest_external_result()`；下载 artifact、校验 hash、写入
`MediaGenerated` 必须幂等。这样 Civitai 长时间 `preparing` 与 Serverless 节点中断都不会造成重复生成。

## 平台决策

### 未来高档路线的 PoC 顺序（当前不执行）

当前默认 profile 是 `krea2_celia_realism_template@v1`，由 Civitai 执行，并使用 `high` priority。
不得因为这份候选清单而在运行时注册 Salad、Vast 或其他 provider。

1. **Salad Container Engine + 自管 ComfyUI + Job Queue**：作为首个 PoC。它的形态与现有“固定 workflow
   JSON + Krea2 LoRA stack”最接近，按实际运行秒计费且队列适合长任务。其公开支持文档提及成人性质媒体
   工作负载，但这不足以替代账户、地区和实际模型许可的书面确认。
2. **Vast Serverless**：仅作为相同容器与相同测试集的速度/成本基准。技术能力可行，但当前成人内容政策未
   得到明确许可；确认前不作为生产高档 provider。
3. **Civitai**：现有保守默认与回退前的基线，不因新 PoC 失败而偷偷把任务改用别家。

Runpod、Modal 和 Lambda Cloud 虽然技术上能运行 ComfyUI 或 GPU 推理，但当前公开条款分别限制
pornographic/graphic adult、indecent/obscene、以及 obscene/indecent/illegal pornography；它们不进入
高档生产候选。它们未来若有书面例外，也必须重新进行政策审查。

### 容器与模型资产

首个镜像应只包含经过验证的 ComfyUI、所需 custom nodes 和 API wrapper。Krea2 基座、Celia LoRA、专用 LoRA
及工作流由 immutable manifest 管理：镜像内固定或从私有对象存储以 hash 下载。容器本地磁盘一律视为易失；
输入/输出和大权重缓存应放在受控对象存储或 provider 确认的持久卷中。

Worker 的输入不是自由 Comfy JSON，而是：`job_id`、冻结 prompt、seed、获准 profile version 与少量白名单
slot。Hermes 永远不能触碰 GPU 类型、LoRA 权重、节点图、negative prompt、安全设置或容器环境变量。

## 分阶段实施

### Phase 0 — 先补齐异步执行边界

- 把现有 Civitai sidecar receipt 升级为持久化 `ExternalRenderJob` action/projection；新增 reconciliation
  worker 和明确的取消/过期语义。
- 给生成器加 provider-neutral `submit`、`poll`、`ingest_webhook`、`cancel` 接口；保留当前同步 adapter
  作为临时实现，不改变普通 OpenAI 路线。
- 为未知结果、重复 webhook、进程重启、远端成功但本地下载失败、预算超限、配置错误和 provider policy
  rejection 建立测试。

### Phase 1 — 无生产流量的 Salad 冒烟

- 建立最小私有 ComfyUI 镜像，复用当前已确认的“真实质感”工作流及 Krea2 LoRA manifest。
- 先做 `min=0` 队列；配置 startup/readiness probe，模型未加载完成时不可接任务。
- 跑 20 个**固定、已授权、人工查看**的冻结测试请求，保存全链路时间与费用，不自动投递。
- 在开始前向 Salad 支持确认：账号和部署地域是否允许目标的私有成年虚构图像、模型/LoRA 存储方式、日志和
  artifact 保留期。

### Phase 2 — 受控 A/B

- Civitai 与 Serverless 使用同一批冻结 `MediaPlan`、同一 seed 策略和同一 workflow version。
- 只允许人工预览，比较身份一致性、解剖、提示词合同遵从、队列耗时与总成本；不要求两种模型逐像素一致。
- 满足验收门槛后，将**新提交**的一个 profile 切为 `eligible`。已提交的旧 job 必须继续由原 provider
  对账，不迁移、不重投。

### Phase 3 — 低频生产与保温决策

- 初期仍 `min=0`。只有观测证明冷启动令用户等待不可接受，才在角色活跃时段以 feature flag 试验 1 个
  warm worker，并把空闲秒数纳入每张图的摊销成本。
- 对每个 profile 设置日预算、单 job 预算、并发上限、最大排队年龄和熔断阈值；触发阈值时停在
  `failed/expired`，而不是自动换模型。

## 验收与可观测性

每次 job 记录并按 cold/warm 分类计算：

- `submit → queued`、`queued → running`、`running → artifact` 和总 P50/P95；
- 实际 GPU 秒、冷启动秒、存储/出网费用、单张 all-in 成本；
- provider 终态失败率、`unknown` 对账成功率、重复提交数（目标为 0）；
- 成品的身份/解剖/工作流合同人工抽样结果；
- 队列过期、policy rejection、模型资源加载失败的可归因错误码。

升级为可自动投递至少需要：20 个固定样例无重复收费；所有 `unknown` 可对账；无跨路线 fallback；P95、
单张成本和视觉人工抽样达到事先约定阈值；并且 provider 的账号/地域/模型许可均已确认。

## 参考

平台与条款的官方来源、技术矩阵和链接见
[`serverless-specialized-renderer-options-2026-07.md`](../research/serverless-specialized-renderer-options-2026-07.md)。
现有 Krea2/Civitai profile 的契约见
[`krea2-raw-orchestration.md`](krea2-raw-orchestration.md)。

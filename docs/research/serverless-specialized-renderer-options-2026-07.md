# 专用图片渲染器：按秒计费 Serverless GPU 平台调研

> 调研日期：2026-07-19。仅使用供应商公开的一手文档与条款。本文是架构选型资料，不构成任何平台对具体内容的许可；上线前仍须以账户所在地、实际服务条款与书面确认复核。

## 结论先行

- 对**高档/成人向专用渲染器**，首个 PoC 候选应是 **Salad Container Engine + 自管 ComfyUI + Job Queue**：官方资料明确承认其容器工作负载可以生成成人性质的图像、文本或视频，并提供按秒计费、ComfyUI 部署范例、异步队列、Webhook 与断点/自动重试能力。
- **Vast Serverless** 是技术上的第二候选：按秒、可自定义镜像/ComfyUI、自动伸缩和 API 都足够。但本次未找到其当前官方条款对成人生成的明确允许条款；在取得其书面确认前，只能作为验证候选，不能承诺为生产后端。
- **Runpod Serverless** 很适合普通（非成人）ComfyUI 渲染，但现行条款把 pornography/graphic adult content 列为未授权内容，不能用于高档线路。
- **Modal** 与 **Lambda Cloud** 均不适合这个高档目标：Modal 现行条款禁止 indecent/obscene material；Lambda 是按分钟/小时的 VM 型云而非 Serverless，且 AUP 禁止 obscene/indecent/illegal pornography。
- **Beam** 的技术能力很好，但未找到官方明确的成人内容许可；在政策未书面确认前不纳入高档生产候选。

这里的“高档”仅指项目已定义、经过上游授权与路由的成人虚构角色专用路径；平台选择不替代年龄、同意、地域、版权与账户合规校验。

## 选型矩阵

| 平台 | 计费/伸缩 | 容器与 ComfyUI | 模型与持久化 | 异步 API | 成人内容公开政策 | 本项目判断 |
|---|---|---|---|---|---|---|
| **Salad Container Engine** | 容器实际运行时间按秒；分配/下载冷启动不计费；可由队列缩到 0 | 可部署自定义 Docker；官方提供 ComfyUI + Job Queue 范例 | 工作流范例使用 S3/R2 存输入输出/检查点；容器本身应视作可中断 | Job Queue 状态轮询或 Webhook；节点中断最多自动重试 3 次 | 官方支持文档明确有客户端生成成人性质媒体，且由节点所有者选择是否接收此类工作负载；受当地法律限制 | **首选 PoC**；先向销售/支持确认账户与所在地域的生产许可、GPU 可用性与数据保留 |
| **Vast Serverless** | Serverless worker 按秒，冷热 worker 与 endpoint 状态明列；市场价浮动 | 预制 ComfyUI 模板或自建 Docker/PyWorker | 本地 volume 受物理节点限制；跨主机推荐对象存储 | Endpoint/worker API；自动伸缩；客户端需退避轮询 | 未找到明确允许条款，也未找到官方成人生成政策 | **备选 PoC**；必须先取得书面政策确认 |
| **Runpod Serverless** | 按秒向上取整；Flex 缩 0；FlashBoot/Active 保温 | 官方 worker-comfyui、JSON workflow、自建 Docker/custom nodes | Network Volume、模型缓存、镜像烘入 | `/run`、`/status`、Webhook、取消/重试 | 条款列出 pornography/graphic adult content 为未授权内容 | 仅普通路线可评估；**高档排除** |
| **Modal** | 实际资源按秒、无最低使用时长；可保温或最小实例 | Dockerfile/registry 镜像；可暴露自管 Web/ASGI 服务 | Modal Volume + `enter` 预加载；Memory Snapshot | `FunctionCall.spawn()`/轮询/恢复/取消；无查到平台级完成 Webhook | 条款禁止 indecent or obscene material | 技术优秀但**高档排除** |
| **Beam** | 资源运行时按秒；默认缩 0，`keep_warm_seconds` 可保温 | Python Image/容器环境；应能自管 ComfyUI（需 PoC） | 高可用 Volume 跨任务挂载 | Task Queue、轮询状态、`callback_url` | 本次未查到明确允许成人生成内容 | 政策未确认，不进生产 shortlist |
| **Lambda Cloud** | On-Demand Cloud 是 VM，按小时价格、按 1 分钟计费；不是 serverless | 自管 VM/容器当然可行 | Network filesystem | REST API 管 VM，无托管 job 生命周期/Webhook | AUP 禁止 obscene/indecent/illegal pornography | **排除**：形态与政策均不匹配 |

## 官方依据

### 1. Salad Container Engine — 建议先做单一工作流 PoC

- [计费](https://docs.salad.com/container-engine/explanation/billing-pricing/billing)：实例运行时间按秒追踪；分配或下载时不收费，官方称不为 cold boot 时间付费。
- [ComfyUI 部署与 Job Queue](https://docs.salad.com/container-engine/how-to-guides/ai-machine-learning/deploy-stable-diffusion-comfy)：官方的 ComfyUI 方案以自定义 Docker 同时运行 `comfyui-api` 和 job-queue worker；提交 ComfyUI JSON，结果可轮询或 Webhook 获取。节点中断时队列会在另一节点自动重试，最多 3 次。
- [Job Queue 生命周期](https://docs.salad.com/container-engine/how-to-guides/job-processing/creating-a-job-queue)：队列通过 Public API 创建，容器须准备 readiness/startup probe，避免模型尚未就绪便接单。
- [长任务与 scale-to-zero](https://docs.salad.com/container-engine/explanation/job-processing/long-running-tasks)：对超过网关时限的任务建议队列/Webhook；输出应使用对象存储；队列可按空闲缩为 0。
- [成人工作负载说明](https://support.salad.com/guides/getting-jobs/workload-preferences/)：官方明确写道部分 SaladCloud 客户会产生成人性质的图像、文本或视频，节点所有者可选择是否接收；在持有成人内容违法的国家会禁止此类负载。此页不是面向客户的完整许可协议，因此生产接入前仍应取得支持团队对账号/地区的书面确认。

**适配判断**：最接近目前的“冻结工作流 JSON → 远端 ComfyUI → 异步回执”的形态，也最容易把 Krea2 角色 LoRA 与专用 LoRA 放入自建镜像或受控对象存储。缺点是分布式/可中断节点本身带来重试与冷加载，因此不能依赖单次 HTTP 长连接。

### 2. Vast Serverless — 技术足够，政策待书面澄清

- [Serverless 定价](https://docs.vast.ai/guides/serverless/pricing)：同 marketplace 实例价、按秒；ready/loading worker 会计 GPU、storage、bandwidth，inactive 不计 GPU。
- [Serverless 概念与预热参数](https://docs.vast.ai/guides/serverless/quickstart) 与 [参数](https://docs.vast.ai/guides/serverless/serverless-parameters)：Endpoint 自动管理 worker；可用最小 load/worker 与 cold multiplier 保留容量。
- [ComfyUI Serverless 范例](https://docs.vast.ai/guides/serverless/comfyui-wan-2.2)：官方说明可提交 ComfyUI JSON workflow；模板与自定义 worker 是公开部署路径。
- [模板/API 与 volume](https://docs.vast.ai/api-reference/creating-and-using-templates-with-api)：模板可指定 Docker image 与环境变量，可连接 volume；但官方迁移文档也明确跨主机持久数据应使用对象存储而非把 local volume 当 network volume。
- [API 错误与退避](https://docs.vast.ai/api-reference/rate-limits-and-errors)：无标准 `Retry-After`，调用方应自行退避、降低轮询频率。
- [当前条款](https://console.vast.ai/terms/)：本次未发现明示的成人生成许可；页面版本也很旧。故“没找到禁止”不等于允许。

**适配判断**：可用作成本与速度基准，也有可直接使用的 ComfyUI 工作流路径。风险不是技术，而是市场供给/冷启动波动和政策不确定性。

### 3. Runpod Serverless — 普通图片候选，不可承担高档

- [定价](https://docs.runpod.io/serverless/pricing)：从 worker 启动到完全停止按秒向上取整；Flex 可以 scale-to-zero，Active worker 是常驻；可配 idle timeout，默认 5 秒。
- [端点配置与冷启动](https://docs.runpod.io/serverless/endpoints/endpoint-configurations)：FlashBoot、cached model、Network Volume 均可降低加载时间；volume 需位于同一 data center。
- [ComfyUI Serverless](https://docs.runpod.io/tutorials/serverless/comfyui) 与 [workflow 转 API/custom worker](https://docs.runpod.io/community-solutions/comfyui-to-api/overview)：官方支持 ComfyUI JSON、custom nodes、LoRA、自建 Docker。
- [持久化与模型缓存](https://docs.runpod.io/serverless/storage/overview) 与 [缓存细节](https://docs.runpod.io/serverless/endpoints/model-caching)：container disk 停止即失；Network Volume 可跨 worker 保存模型；非 HuggingFace 私有模型应放 volume 或烘入镜像。
- [异步请求与 Webhook](https://docs.runpod.io/serverless/endpoints/send-requests)：`/run` + `/status`，任务结果保存 30 分钟；Webhook 要返回 200，否则最多两次、每次隔 10 秒重试。状态定义见 [job states](https://docs.runpod.io/serverless/endpoints/job-states)。
- [服务条款](https://www.runpod.io/legal/terms-of-service)：将 “pornography or graphic adult content, images, or other adult products” 明列为 unauthorized content，违规可导致立即终止或终身禁止。

### 4. Modal — 普通推理很强，但高档政策不合适

- [计费](https://modal.com/docs/guide/billing) 与 [概览](https://modal.com/docs/guide)：按实际资源/秒计费、无最低时长。
- [冷启动](https://modal.com/docs/guide/cold-start)：容器本身约一秒启动，但大模型/`enter` 加载仍可能数秒到数分钟；`scaledown_window` 可设 2 秒到 20 分钟，`min_containers`/`buffer_containers` 能减少冷启动但空闲也计费。
- [自定义镜像](https://modal.com/docs/guide/existing-images) 与 [Web Functions](https://modal.com/docs/guide/webhooks)：可用 Dockerfile/registry image 自行暴露服务，技术上能封装 ComfyUI，但需要单独验证镜像的入口和 GPU 依赖。
- [Volume/模型](https://modal.com/docs/guide/model-weights) 与 [Volumes](https://modal.com/docs/guide/volumes)：适合保存权重、以 `enter` 预加载；Volume 写入需要 commit/reload 语义。
- [异步 FunctionCall](https://modal.com/docs/sdk/py/latest/modal.FunctionCall)：`.spawn()` 后可轮询、从 ID 恢复或取消。未找到平台提供的完成回调 POST，因此应由调用方轮询，或在任务内自行回调。
- [服务条款](https://modal.com/legal/terms)：禁止处理 “indecent or obscene material”。

### 5. Beam 与 Lambda Cloud — 作为对照，不进入当前 shortlist

- Beam 的 [定价](https://docs.beam.cloud/v2/resources/pricing-and-billing) 是容器运行时按秒；[保温](https://docs.beam.cloud/v2/endpoint/keep-warm) 可缩 0 或保温；[Volume/Task Queue/callback](https://docs.beam.cloud/v2/reference/py-sdk) 支持跨任务 volume、队列和 `callback_url`，状态见 [Task Status API](https://docs.beam.cloud/v2/reference/api-docs/tasks/tasks-status)。但是 [条款](https://docs.beam.cloud/v2/security/terms-and-conditions) 未提供足以确认高档成人生成的明确许可，故不以“沉默”当成允许。
- Lambda 的 [ODC 说明](https://docs.lambda.ai/public-cloud/on-demand/) 表明它是 GPU VM + filesystem；[Billing](https://docs.lambda.ai/public-cloud/billing/) 说明实例按小时价格、以一分钟为增量，而非按秒 Serverless；[AUP](https://lambda.ai/legal/terms-of-service) 禁止 obscene、indecent 或 illegal pornography。因此排除。

## 推荐的架构演进（先规划，不实施）

不把平台 SDK 直接塞进 `MediaRenderer`。保持现在的冻结计划与 high/private lane 不变，新建可替换的远端执行边界：

```text
Frozen MediaPlan + immutable WorkflowSpec
                 │
                 ▼
 SpecializedRenderProvider
   submit() → ExternalRenderJob(provider, remote_job_id, state)
   poll()/ingest_webhook() → ExternalRenderResult
   cancel() → terminal state
                 │
                 ▼
 MediaGenerated / failure Action（持久化回放）
```

### Provider contract

1. `submit` 必须接收稳定的 `idempotency_key = media_request_id + workflow_hash`；提交成功后**立刻**持久化 provider、remote job ID、提交时间与 workflow hash，再等待结果。
2. 结果状态需统一映射为 `queued`、`preparing`、`running`、`succeeded`、`failed`、`cancelled`、`expired`、`unknown`。网络超时只能是 `unknown`，禁止自动再次 submit，以免重复扣费。
3. Webhook 与 polling 走同一个 `ingest_external_result()`；结果下载、哈希和对象存储归档必须幂等。
4. “重试”只能重试同一冻结 WorkflowSpec，不能重新让 Hermes 选候选、换场景或换档位；对可中断平台，使用 provider job retry 不等同于重新规划。
5. 每个 provider profile 绑定：镜像 digest、ComfyUI/custom-node 版本、基础模型 hash、每个 LoRA 的来源/version/hash/weight、工作流模板 hash 与输入节点映射。这样输出才可追溯。

### 推荐先做的 PoC

1. 用 **Salad** 建一个仅内部使用的 ComfyUI 镜像：固定 Krea2、角色 LoRA、专用 LoRA、当前“真实质感”工作流和 API wrapper；输入仅允许已冻结的 prompt/seed/workflow patch，输出上传私有对象存储。
2. 以 Job Queue 取代同步 HTTP：配置 `min=0`，启用 startup/readiness probe，设置 webhook 回到本项目；同一请求以 idempotency key 去重。
3. 对 20 个已授权的固定测试请求记录：排队时间、冷加载、GPU 推理时间、总成本、一次成功率、身份一致性和失败原因。先用小预算验证，不把生产自动投递切过去。
4. 并行问 Salad 支持：目标区域、当前账户能否运行成年人虚构角色的私有图像工作负载、模型/LoRA 文件存放和输出保留边界。没有书面确认，不启用成人生产流量。
5. 若 Salad 的容量/冷启动不达标，拿完全相同的 Docker 与测试集在 Vast 做基准；只有政策确认后才进行下一阶段接入。

## 暂不做的事

- 不因“按秒”而维持长期 warm GPU；低频自拍/媒体触发更适合 queue + scale-to-zero。只有基于真实流量证明用户等待不可接受时，才试探性保温 1 个 worker，并把 idle 成本纳入预算。
- 不把当前 Civitai 的 `preparing/processing` 不透明状态复制到新 provider：远端 job ID 和每次状态转移必须是 Action 的一部分。
- 不让 Hermes 改容器、LoRA、节点图或 provider 参数；它只选择/补充已经冻结的媒体表达。模型和工作流版本由 render profile 管理。

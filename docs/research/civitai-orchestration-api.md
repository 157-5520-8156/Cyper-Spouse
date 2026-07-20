# Civitai Orchestration API：图片机接入判断

日期：2026-07-17。本文只依据 Civitai 官方开发者文档，服务于项目的事件媒体渲染器；不把平台的“允许 mature content”误写成内容审核豁免。

## 结论

1. **Orchestration API 适合作为图片机的异步云端适配器。**它提供可持久化的 workflow ID、费用预检、轮询/Webhook、失败理由和结果下载，正好能挂在现有 `MediaRenderer → MediaInspection → Delivery` 之间。
2. **不能假定卖家交付的 Krea2“模型/LoRA”能接入官方 Krea v2 路线。**官方 `engine: "fal", model: "krea2"` 明确不接收 LoRA；它只接收 prompt 和最多十张公开可访问的 `imageStyleReferences`。这些参考是风格引导，不能被当成角色身份 LoRA 的正式加载机制。[Krea v2 recipe](https://developer.civitai.com/orchestration/recipes/krea)
3. 因此，Krea2 成果应先被视作**待验证的身份资产**，不是现在就可写死为生产依赖。若卖家交付的是 `.safetensors`，需要拿到它的底模/格式、Civitai model-version ID（或确认其并非 Civitai 可调度资产）后再决定路径。
4. 上述限制只适用于标准 FAL Krea recipe。用户提供的 Civitai workflow 则使用通用
   `imageGen(engine=comfy, ecosystem=krea2)`，其中 `loras` 以 AIR URN + strength 传入。这是另一个
   provider schema，已由实际 workflow 样本验证；项目应为它维护独立 adapter 与 mock request test，不能把
   两条 schema 混为一谈。Custom Comfy 只在 generic imageGen 缺少所需节点时再使用。

## 对 Krea2 交付物的验收门

卖家交付后先记录以下信息，任一项缺失则不接入自动媒体：

| 必要信息 | 验证方式 | 通过条件 |
|---|---|---|
| 文件格式与实际底模 | 卖家书面说明 + 文件元数据 | 明确不是仅能在卖家私有界面使用的黑盒资产 |
| Civitai 模型版本 | `GET /api/v1/model-versions/mini/{id}` | 返回符合预期的 `air`、baseModel，且对当前帐号 `canGenerate: true` |
| 适配的推理入口 | 用官方 schema `whatif=true` 预检 | 请求被解析为可执行，不是仅“上传后可下载” |
| 身份能力 | 固定五场景视觉验收 | 正面、左右三分之四、中远景、低光、自拍均通过身份验收 |
| 成本与成熟内容行为 | `whatif=true` + 小样例 | 有明确 Buzz 预算和失败/审核回退 |

模型版本 API 的 `air` 是供 Orchestration 引用的标准标识；`mini` 响应给出 `canGenerate`。常规模型版本接口不会暴露私有/归档文件，因此私有或未发布文件不能被假定为云端可调度。[Model versions reference](https://developer.civitai.com/site/reference/model-versions)

### 当前可证实的 Krea2 能力与限制

```json
{
  "$type": "imageGen",
  "input": {
    "engine": "fal",
    "model": "krea2",
    "operation": "createImage",
    "size": "medium",
    "prompt": "...",
    "aspectRatio": "2:3",
    "creativity": "low",
    "imageStyleReferences": [
      { "imageUrl": "https://public.example/ref.jpg", "strength": 1.0 }
    ]
  }
}
```

- `imageStyleReferences` 最多 10 张，URL 必须能被 FAL 服务端公开抓取；它不是私有本地文件上传，也不是身份控制承诺。
- Krea2 支持 `medium` 与 `large`，以及固定比例；不支持显式宽高、negative prompt 或 `loras` 字段。
- 官方标价：medium 为 39 Buzz/张（含 style reference 45.5），large 为 78 Buzz/张（含 reference 84.5）。必须以 `whatif=true` 的实际预估为准。[Krea v2 recipe](https://developer.civitai.com/orchestration/recipes/krea)

**架构含义：**标准 FAL Krea2 可先作为“日常高真实感候选渲染器”，但 `identity_binding` 只能标为
`style_reference_unverified`。generic `engine=comfy/ecosystem=krea2` route 则可在 AIR identity LoRA、
`whatif` 预检和五场景验收都通过后标为 `compatible_lora`；两者不能共用 adapter 或能力声明。

## 建议的适配器契约

不把 Civitai workflow 格式泄露给 `MediaPlanner`。图片机内部增加一个窄边界：

```text
Frozen MediaPlan
  → CivitaiOrchestrationRenderer.submit(render_request)
  → Persisted ExternalGeneration(workflow_id, provider_profile, request_hash)
  → Civitai callback / polling
  → downloaded immutable media asset
  → MediaInspection
```

`render_request` 只包含冻结后的 prompt、尺寸/比例、种子、参考资产、provider profile 和预算上限；它不拥有世界事实或投递权。`ExternalGeneration` 应持久化：

- `workflow_id`、`step_name`、提交的 provider profile/version；
- `MediaPlan` 哈希、`request_id`、seed、预估/实际 Buzz transaction；
- 原始终态、失败 `reason` / `blockedReason`、下载后资产哈希；
- 视觉验收结论，而不是只保存临时 blob URL。

官方的 workflow 是多个 step 的容器，step 内部可以展开多个 job；客户端通常跟踪 workflow/step 而非调度 job。生命周期为：

```text
unassigned → preparing → scheduled → processing
                                    ├→ succeeded
                                    ├→ failed
                                    ├→ expired
                                    └→ canceled
```

`preparing` 可能意味着 worker 正在下载模型，不应被误判为失败；终态不会再次变化。[Workflow model and lifecycle](https://developer.civitai.com/orchestration/guide/workflows)

### 提交、回放与回调

- 使用通用 `POST https://orchestration.civitai.com/v2/consumer/workflows`，Bearer token 只存服务端环境变量；`metadata` 写入非敏感的 `request_id`、`media_plan_hash` 和 renderer version，`tags` 用于检索。
- 所有新 provider profile 先用 `whatif=true` 验证 schema、资源解析与预估费用，随后才允许真实生成。`wait` 最多受 100 秒请求超时约束；大批量/训练一律 `wait=0`。[Quick start](https://developer.civitai.com/orchestration/guide/getting-started) / [Submitting work](https://developer.civitai.com/orchestration/guide/submitting-work)
- 生产使用 HTTPS webhook，订阅 `workflow:succeeded`、`workflow:failed`、`workflow:expired`、`workflow:canceled`，并设置 `detailed: true`；接收端快速返回 2xx、按 `(workflowId, status, timestamp)` 幂等。没有公网 callback 时才按 2s、5s、10s、15s、之后 30s 轮询。[Results & webhooks](https://developer.civitai.com/orchestration/guide/results-and-webhooks)
- blob URL 是临时签名 URL；成功后立即下载到项目的受控媒体存储并记录 hash。默认终态 workflow 保留约 30 天，不能将其当永久媒体库。
- 外部 API 的相同请求可能被平台去重/缓存，但文档明确存在未公开例外；本项目仍应以自身 `request_id → workflow_id` 映射保障不重复发图或重复扣预算。

### 失败策略

| 外部结果 | 图片机动作 |
|---|---|
| `400/401/403/404` | 记录为不可重试配置/权限失败；不盲重试 |
| `429/5xx` | 指数退避加随机抖动，最多五次；不得重新规划照片 |
| `failed: no_provider_available` | 记录 provider capability 失效，走已授权的下一渲染器或停止 |
| `failed: blocked` | 不用原 prompt/原参考重试；标记审核阻断，交给安全的降档或不投递策略 |
| `expired/timeout` | 可用相同冻结计划、小 workload 重试一次；仍失败则不投递 |
| 成功但 MediaInspection 不通过 | 仅一次定向修复；不得改变世界事实、关系或隐私上限 |

这些语义来自 Civitai 的错误分类；其中 `blocked` 是内容审核，官方明确建议不要用相同输入重试。[Errors & retries](https://developer.civitai.com/orchestration/guide/errors-and-retries)

## 成熟内容边界

Orchestration 有成熟内容支付/交付控制，但这**不是**模型或审核绕过：

- `allowMatureContent: true` 会使用 Yellow Buzz；Blue/Green 只适用于 SFW。
- 即使允许 mature，step 仍可能以 `reason: "blocked"` 终止；权限不足也可能返回 403。
- `upgradeMode: manual` 可使意外成熟结果暂缓交付；对于本项目，默认应选人工/系统审核后再决定，不能把它变成自动放行。

因此，高张力媒体仍必须先经过本项目的 `privacy / expression_charge / coverage` 合同和 `MediaInspection`。Civitai 只是渲染执行方，不能替代项目安全边界。[Payments and mature-content handling](https://developer.civitai.com/orchestration/guide/submitting-work#payments-buzz)

## 与未来“专业成人底模”路线的关系

当前不应设计“解禁 LoRA”旁路。正确的可扩展契约是把渲染能力显式建模：

```text
ProviderProfile
  base_ecosystem: krea2 | flux1 | flux2 | ...
  identity_asset: none | style_reference | compatible_lora_air
  mature_policy: disallowed | platform_gated
  controls: reference | variant | lora | controlnet | custom_comfy
  verified: false | true
```

- Krea2 FAL profile 的 `identity_asset=style_reference`、`controls=reference`，不应声称支持 LoRA；generic
  Krea2 Comfy profile 的 `identity_asset=compatible_lora_air` 只能在实际 AIR 资源可调度时声明。
- 未来若选择兼容的 Flux checkpoint，则以同生态的 AIR URN LoRA + 明确强度为能力，而不是跨生态叠加文件。
- 是否可用的判断要基于实际 `canGenerate`、`whatif=true`、五场景视觉验收和成本记录，而不是模型名称或卖家口头“可叠工作流”。

## Krea2 成果到手后的最小试验

1. 获取模型格式、完整底模名称、触发词/权重或卖家所用平台工作流。
2. 若声称能在 Civitai Orchestration 中运行，要求 model-version ID；调用 `mini` endpoint 检查 `canGenerate`，再跑 `whatif=true`。
3. 固定相同 `MediaPlan`，Krea2 的 `medium`、`large` 各生成正面近景、三分之四、低光自拍、全身/中景、朋友拍摄共五图；每图保存 workflow ID、Buzz、参考图角色与视觉验收。
4. 只有身份一致性、镜头几何、手机真实性与失败率达到既有门槛，才将该 profile 从 `experimental` 升为 `eligible`；否则继续以 OpenAI/已验证路线作为回退。

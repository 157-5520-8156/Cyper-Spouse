# Krea 2 RAW 专用渲染路线（设计稿）

## 结论与范围

本设计固定两条**不互相降级**的渲染路线：

```text
普通世界媒体 / 常规角色媒体
  → ordinary_openai_image2
  → GPT Image 2 + 已选身份参考

suggestive_private / explicit_private（高档私密渲染）
  → krea2_celia_realism_template@v1
  → Civitai generic imageGen（engine=comfy, ecosystem=krea2）+ 固定 Krea2 AIR LoRA stack
```

普通路径不使用 LoRA；它继续以 GPT Image 2 的画面质量与现有身份锚点为准。高档路径不重写
Media Planner、Interaction Bid、Address Strategy、Camera Geometry、Subject/Embodied Presentation
或真实性合同；它只替换 `MediaRenderer` 内部的执行 adapter。按当前产品决定，高档路径不调用
通用 `MediaInspection`，也不基于视觉结果重绘；它依赖冻结的高档语义合同、经审阅的 Civitai
模板、预算门和 provider/文件完整性错误处理。

此文件不把平台成熟内容设置误作审核绕过。图片机只消费上游已经冻结的 lane 与 provider
capability；授权、关系与投递策略不在此模块重新判定。`explicit_reserved` 保留为历史拒绝值；
新计划使用 `suggestive_private`（`adult_suggestive`）和 `explicit_private`（`adult_explicit`）两个
不可静默降级的 render route，并共享同一份候选、镜头与人物表现链路。

## 为什么采用 generic Civitai Krea2 imageGen

卖家交付的 `Krea 2 RAW` LoRA 必须与云端可调度的 AIR 资源一一对应。Civitai 的**标准 FAL Krea
recipe**确实没有 `loras`，只支持 prompt 与公开的 `imageStyleReferences`；它不能承担角色身份 LoRA。

但用户提供的可运行 workflow 证明另一条不同的 provider schema：通用 `imageGen` step 使用
`engine=comfy`、`ecosystem=krea2`，带有一组固定的 AIR LoRA 权重与 `images` 身份锚点。高档路径采用
`CivitaiTemplateWorkflowImageGenerator`，读取经人工确认的
`configs/civitai-krea2-celia-realism-template.json`。它会**原样保留**模型、采样器、scheduler、LoRA
映射、负提示词、优先级和成熟内容设置，只替换冻结计划编译出的 prompt、尺寸、稳定 seed 与 external ID。
当前 Celia 高档模板是纯 Krea2 LoRA＋工作流提示词：不含 `images` 输入，也不上传 `assets/reference`。
运行时以 `require_reference_free=True` 加载它，未来模板若加入固定或动态图像输入会在启动时拒绝，
防止普通生活照、场景、衣装或姿态作为强参考重新泄漏进高档图。旧模板仍可显式使用 blob 身份占位符，
但不能被注册为这个高档 profile。

Hermes 仍只选择已提供的矩阵候选、证据与 media lane；它不写 workflow JSON，也不能改 LoRA 权重、模型
或 provider 控制项。模板 adapter 是唯一会对 provider JSON 做白名单替换的 Module。

因此，标准 FAL Krea recipe 的合法 Profile 仍是：

```text
identity_binding = style_reference_unverified
capability_status = experimental
automatic_identity_delivery = false
```

generic Krea2 route 中只有 AIR LoRA 成为 `identity_binding = compatible_lora` 的高档 route。裸
`.safetensors` 不是可自动执行的云端资源。Custom Comfy 仍是需要额外节点、ControlNet 或 provider 未公开
控制项时的后备，不是 Krea2 LoRA 的默认前提。

## Media Render Profile 合同

`MediaPlan` 不保存 provider prompt 或 Civitai workflow JSON；它冻结一个 profile 引用与版本。
渲染器读取该 profile 后才知道怎样提交。每个 profile 至少有：

| 字段 | 作用 |
|---|---|
| `profile_id` / `profile_version` | 稳定引用与可回放版本 |
| `model_ecosystem` | `openai_image2` 或 `krea2_raw`，禁止跨生态 LoRA 假设 |
| `route_kind` | `managed_image_edit`、`orchestration_krea2_reference`、`orchestration_custom_comfy` |
| `identity_binding` | `reference_edit`、`style_reference_unverified`、`compatible_lora` |
| `identity_asset_manifest` | 锚点或 LoRA 的版本、哈希、用途；非秘密信息 |
| `capability_status` | `disabled`、`experimental`、`eligible`、`suspended` |
| `allowed_lanes` | 可执行的 Media Lane 闭集 |
| `supported_controls` | reference、LoRA、图生图、尺寸、成熟内容等实际支持的能力 |
| `cost_policy` | 单次预算、预检预算、修复上限 |
| `inspection_contract_version` | 普通路径的身份、几何、真实性与覆盖验收规则；当前高档 direct route 为 `not_applicable` |

### 初始 Profile 登记

| Profile | 当前状态 | 可用范围 |
|---|---|---|
| `ordinary_openai_image2@v1` | `eligible` | 非成人普通/角色媒体；`reference_edit` |
| `krea2_celia_realism_template@v1` | `disabled`，等待运行时显式开启与预检 | `suggestive_private` / `explicit_private`；`compatible_lora`，无图像参考输入 |
| `krea2_raw_custom_comfy@v1` | `experimental` 后备 | 只有 generic route 缺少所需节点时才使用 |
| `civitai_standard_krea2_reference@v1` | `experimental` | 人工比较用；禁止作为身份关键高档自动投递 |

Krea2 profile 在验证前不得注册 `adult_suggestive` 或 `adult_explicit`。这使“模型还没装好”成为明确的、可持久化的
`specialized_private_generator_unavailable`，而不是后台换模型或偷偷降档。

运行时使用受审阅的 recipe 模板；必须显式开启，避免只因 Civitai key 存在就产生高档请求：

```text
CIVITAI_KREA2_ENABLED=true
CIVITAI_KREA2_TEMPLATE_PATH=configs/civitai-krea2-celia-realism-template.json
```

模板缺失、schema 非法、模板包含图像输入或 Civitai 拒绝任一步时，两个高档 route 都 fail closed；不会尝试
普通 `CivitaiWorkflowImageGenerator` 或 GPT Image 2 回退。旧 AIR 环境变量模式保留为历史兼容路径，但不会在
配置了模板时被选中。

## 交付物与预检门

卖家交付时应同时取得：

1. LoRA 文件、精确文件哈希与明确的 Krea 2 RAW 基座/版本，以及两者可调度的 AIR/资源方案；
2. 推荐触发词、权重范围、文本编码器/采样器要求；
3. 可导入的 Custom Comfy workflow，含所有 custom node、底模和 LoRA 的加载位置；
4. 至少一条能在云端复现的示例参数；不能只给网页私有库链接。

开启 profile 前按以下顺序执行；任一步失败都保持 `disabled`：

```text
文件与基座清单
  → Civitai 资源/AIR 与 canGenerate（若 Custom Comfy 需要）
  → whatif=true 预检：工作流 schema、资源、预算
  → 单图冒烟：确认底模 + LoRA 真正加载
  → 五场景人工身份与镜头抽样
  → 从 experimental 升级为 eligible
```

五场景固定使用真实的冻结 `MediaPlan`，而不是临时性感提示词：正面近景、左右三分之四、低光前摄、
中远景/全身、可信的同伴或定时拍摄。每张都保存 External Generation receipt、实际费用、所选参考资产、
人工抽样结论。只有身份、拍摄作者、镜头几何、手机真实性、非露骨覆盖及失败率均达到预期门槛，
才允许自动投递；这项上线前抽样不构成每张高档图片的运行时视觉验收。

## 深模块与调用顺序

`MediaRenderer.render(plan)` 保持唯一公开 Interface；调用者不需要认识 Civitai、Krea、workflow 或 LoRA。
World v2 host 必须通过 `runtime.build_event_media_renderer(settings, generator, inspector, output_dir)` 构造这一个
renderer，并将它注入 `EventMediaExecutionAdapter`；不得在 host 中自行创建 Civitai client 或复制模板逻辑。
内部的 `ProfiledGenerationAdapter` 处理复杂性：profile 校验、预检、提交、轮询/webhook、结果下载、
费用对账、瞬时错误重试及 artifact hash。这样 provider 的复杂性留在一个 Module 内，World 和 Planner
仍只认识冻结的机会、计划和结果。

```text
Frozen MediaPlan + Media Render Profile
  → ProfiledGenerationAdapter.submit
  → ExternalGeneration（workflow_id / request_hash / quote）
  → callback 或 polling
  → immutable artifact + hash
  → World expression / delivery
```

同一冻结计划遇到短暂 provider 故障时只能沿用同一 profile、模板、世界证据、Lane 和身份合同作一次
同输入重试；它不能重新选模型、重抽拍摄候选、换 LoRA 或把高档照片改为普通照片。

## 错误、预算与自动化

| 结果 | 行为 |
|---|---|
| profile 为 `disabled` / `suspended` | `specialized_suggestive_generator_unavailable`，不提交、不降级 |
| `whatif` 或资源解析失败 | 记录配置失败，保持 `disabled` |
| `429` / `5xx` / 短暂网络错误 | 同一 External Generation 按退避有限重试，不重规划 |
| `blocked` / policy / 4xx 配置错误 | 终态失败，不用相同输入重投 |
| 配额不足 | `image_provider_quota`，不自动改用普通模型 |
| 生成成功 | 记录 artifact hash 与 workflow receipt；高档 direct route 不作视觉重绘 |

每次实际提交先受 `cost_policy` 和项目预算闸门约束。生产以 webhook 为主、轮询为补充；收到成功后立即把
临时 blob 下载至受控媒体存储并记录哈希。`request_id → workflow_id` 的本地映射是幂等来源，不能依赖
provider 是否恰好缓存了相同请求。

## 验收与升级规则

在 `eligible` 之前，测试可以生成但不自动发送。升级需同时满足：

- Krea2 RAW 和 LoRA 的实际加载可由 workflow 节点与 receipt 证明；
- 5 场景人工抽样确认身份一致性、发型/面部方向、拍摄物理与镜头几何；
- 高档 lane 仍满足自摄、收件人专属、表达张力和非露骨覆盖合同；
- 费用、超时、审核阻断及重试结果已记录，且没有跨 profile 的隐式回退；
- 角色参考图不会作为无关 pose/场景复制源泄漏到成品。

通过后只把该 profile 从 `experimental` 改为 `eligible`；不会修改 Planner 的分类矩阵或 World 的发送权。

## 官方依据

- [Civitai Krea v2 recipe](https://developer.civitai.com/orchestration/recipes/krea)：标准 Krea route 的受支持输入与限制。
- [Civitai workflow lifecycle](https://developer.civitai.com/orchestration/guide/workflows)：异步 workflow 与终态。
- [Civitai submitting work](https://developer.civitai.com/orchestration/guide/submitting-work)：`whatif`、预算和提交行为。
- [Krea 2 open source / RAW](https://www.krea.ai/krea-2-open-source)：Krea2 RAW 的研究与训练用途。

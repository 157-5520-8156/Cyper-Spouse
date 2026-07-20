# Civitai Custom Comfy 与 Krea2 RAW LoRA：可行性核查

日期：2026-07-17。依据 Civitai 当前消费者 OpenAPI v2 的公开规范；结论只覆盖该 API 明确承诺的能力，不把“可以提交 raw graph”扩大解释为“任意本地模型文件都可以云端运行”。

## 结论

**Civitai Orchestration 可以执行 Custom Comfy。**它把 `customComfy` 定义为一个正式 Workflow step，输入为原始 ComfyUI workflow graph、资源清单与 trace 选项。因此它是 Krea2 RAW 高档路线的合理候选执行器，而不是必须放弃的服务。

但有一项硬前提：**所有 checkpoint、LoRA、VAE，以及运行时安装的 custom nodepack 都必须以 AIR URN 显式出现在 `resources` 中。**Orchestrator 不扫描 workflow 去猜资源；未列出的引用会在 ComfyUI 加载时失败。卖家交付的裸 `.safetensors` 仅是身份资产，不自动等于 Civitai 可调度资源。

来源：[Civitai consumers OpenAPI v2](https://orchestration.civitai.com/openapi/v2-consumers.json)，`/v2/consumer/recipes/customComfy` 与 `CustomComfyInput`。

## 已确认的接口形状

一个异步 workflow 可以使用如下 step：

```json
{
  "$type": "customComfy",
  "input": {
    "resources": [
      "urn:air:...:krea2-raw-base...",
      "urn:air:...:celia-krea2-lora...",
      "urn:air:comfy:nodepack:..."
    ],
    "workflow": { "raw": "ComfyUI API graph" },
    "trace": "logs",
    "minVramGb": 48
  }
}
```

`resources` 是必填、至少一个 AIR URN 的数组；`workflow` 是必填的 raw graph，Orchestrator 将其不透明地交给 worker；`trace` 必填，可取 `none`、`logs` 或 `binary`。可选的 `sessionId` 提示 session affinity，`comfyImage` 可以指定带版本的 OCI Comfy 容器，`minVramGb` 可要求最小显存，`useSageAttention` 可启用对应 ComfyUI 参数。

执行结果含成品/临时 blob、可选 trace URL 与运行时用量；费用按实际 ComfyUI GPU runtime 计算，基础设施失败不收费。recipe endpoint 支持 `whatif`、`experimental`、`allowMatureContent` 与 `ephemeral` query 参数。通用 Workflow endpoint 也登记了同样的 `customComfy` step schema。

## 对本项目的实际含义

### 正确路径

```text
Krea2 RAW base AIR
  + Celia Krea2 LoRA AIR
  + 必要 custom nodepack AIR
  + 卖家可导入 Comfy API graph
      → Civitai CustomComfy workflow（whatif）
      → 单图 smoke test（trace=logs）
      → 5 场景视觉验收
      → Media Render Profile = eligible
```

在完成验收前，`krea2_raw_custom_comfy@v1` 保持 `disabled`。普通 GPT Image 2 路线不受影响；高档机会不允许静默落到标准 Krea recipe、SDXL 或普通路线。

### 不是正确路径

- 把 Krea2 RAW LoRA 的本地路径、文件名或 data URL 写进 Comfy graph，然后期待云端 worker 有该文件；
- 只在 workflow 中写 LoRA loader，而不把 LoRA AIR 和底模 AIR 放入 `resources`；
- 用标准 Krea v2 `imageStyleReferences` 声称已经加载了身份 LoRA；
- 通过 `allowMatureContent` 推断不会被模型或平台阻断。

## 仍未证实、必须由交付物与预检回答的事

1. 卖家能否为这份 Krea2 RAW LoRA 提供可访问的 AIR URN，或给出可被 Civitai worker 下载并登记为 AIR 的正式方案；
2. Civitai 当前 worker 是否能调度该确切 Krea2 RAW base、该 LoRA 格式和所需 nodepack；
3. 如需要专用 Comfy image，是否存在可用的 `comfyImage` AIR；
4. 当前账户是否对这些资源有 `canGenerate` 权限、实际所需显存与每秒 Buzz 成本；
5. 此 route 对高档但非露骨请求的实际 `blocked` 行为。

消费者 OpenAPI 中可见的是资源查询（`GET /v2/resources/{air}`）和 blob 上传（`GET /v2/consumer/blobs/upload` 获取预签名上传地址）；在本次核查的消费者规范中，没有“把任意本地 LoRA 文件发布为可调度 AIR”的写接口。因此不能擅自假定上传一个 blob 后就能作为 Custom Comfy 模型资源。若卖家无法提供 AIR/正式资源方案，Custom Comfy 仍可能需要改用另一云端 Comfy 执行器。

## 模型到手后的最小操作清单

向卖家索取：

1. Krea2 RAW base 的准确版本/AIR（或其 Civitai 可解析资源方式）；
2. LoRA 的文件、SHA256、推荐权重与触发词；
3. 可导入的 **ComfyUI API workflow**，不只是截图或网页操作步骤；
4. 完整 nodepack 列表和模型依赖；
5. 对应的 LoRA AIR，或把该文件注册为可调度 AIR 的正式步骤。

拿到后按顺序执行：

1. `GET /v2/resources/{air}` 检查每个 AIR、格式、权限和 `canGenerate`；
2. 使用同一 raw graph 进行 `whatif=true`，确认 schema、资源解析和估算；
3. 使用 `trace=logs` 运行一张非投递 smoke 图，逐项确认 checkpoint/LoRA/nodepack 实际加载；
4. 以冻结的五份 MediaPlan 做视觉验收；
5. 将 profile 从 `experimental` 改为 `eligible`，才允许自动高档媒体。

## 代码接入边界

后续只新增一个内部 `CivitaiCustomComfyGenerationAdapter`。它的 Interface 是：

```text
submit(frozen MediaPlan, frozen Media Render Profile) -> ExternalGeneration
reconcile(ExternalGeneration) -> immutable artifact | terminal failure
```

它负责 profile/resource 校验、`whatif`、提交、轮询/webhook、trace、下载和费用 receipt；`MediaPlanner`、World v2 和投递层不接触 AIR、Comfy nodes 或 provider graph。这样 Krea2 route 若最终无法调度，也只替换这个 adapter，不动图片分类和世界语义。

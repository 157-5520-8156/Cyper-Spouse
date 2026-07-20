# Civitai Cloud：写实角色、身份保持与私密媒体工作流调研

日期：2026-07-16。范围为**明确成年虚构角色**的写实、私密/暗示性但非露骨媒体；不提供露骨提示词或绕过平台规则的方法。

## 结论先行

当前 `CivitaiWorkflowImageGenerator` 的“单 canonical 图 + `createVariant` + 0.58 denoise”是**弱身份约束**，不是 FaceID 工作流。因此产出长得不像角色、像泛化 AI 写真的现象符合预期，并非只换 CyberRealistic checkpoint 就能修复。

下一轮应把 Civitai 作为可比较的云端候选，而不是马上替代当前 OpenAI 路径：

1. 对 `CyberRealistic XL v10` 先做冻结的低 denoise 变体网格：`0.28 / 0.36 / 0.44`，每档固定同一构图、seed 和一张 canonical 正面参考；验收“身份相似度/动作服从/手机影像感”后才选择默认值。当前 `0.58` 对“换场景、换衣装、换姿态”放得过宽。
2. 将 XL 基线校正为模型作者建议的 `dpmpp_2m_sde + karras`、约 30 steps、CFG 3–5、832×1216 或 896×1152；现有 24 steps、CFG 6 不是该模型的推荐区间。[CyberRealistic XL 模型说明镜像](https://civitaiarchive.com/models/312530?modelVersionId=611386)；其作者站也给出同一建议。[CyberRealistic 官方说明](https://cyberrealistic.org/)
3. 需要可信的指定动作/镜头时，在 Civitai 云端 API 中使用 `controlNets` 的 `openpose` 或 `depthAnythingV2`，把**构图/骨架控制**与**身份参考**分开；不要让一张身份参考同时承担脸、姿势、发型和场景。
4. 若上述网格仍不够像，下一步不是继续堆 prompt，而是验证 Civitai **Custom Comfy** 是否能加载可用的 FaceID/InstantID/IP-Adapter nodepack 与相应模型资源；只有该能力可运行且有端到端验收，才把它写成可选 `identity_binding=face_lock` 能力。否则必须诚实地把云端标准 API 标记为 `identity_binding=variant_only`。

## 官方 API：当前真正可用的控制面

Civitai 现行消费者 OpenAPI 定义了以下两层能力：[OpenAPI v2 consumers](https://orchestration.civitai.com/openapi/v2-consumers.json)。

| 层级 | 已确认能力 | 对本项目的意义 | 限制 |
|---|---|---|---|
| 标准 `imageGen` | SDXL/Flux 的 `createImage`、`createVariant`；`image` 可为 URL/DataURL/Base64，`denoiseStrength` 0–1 | 可把 canonical 身份图作为变体输入 | 没有显式 `face_id`/多参考/人脸锁定字段 |
| 标准 `imageGen` | `loras`、`controlNets`、sampler、scheduler、steps、CFG、seed | 可组合已验证的写实/手机影像 LoRA；可控制姿态和深度 | LoRA 不是角色身份资产，不能作为未训练角色的替代 |
| 标准 `controlNets` | `openpose`、`dwpose`、depth 系列、canny、lineart、inpaint 等 | 解决姿态和画面几何，不改变“脸部绑定较弱”的事实 | 需要合适的控制图；不能凭文字精确锁定脸 |
| `CustomComfy` | 原始 Comfy 图、显式 AIR 资源清单、可声明 `comfy:nodepack` URN | 理论上可使用 FaceID/InstantID/IP-Adapter 等自定义节点 | 是低层能力，需先确认节点包和权重的 AIR URN 可用、能在云端 worker 运行，不能假设现成可用 |

因此，**标准云端 imageGen 已支持姿势/深度 ControlNet，但官方 schema 没有把 IP-Adapter、InstantID 或 FaceID 暴露为开箱即用字段。**它们只能作为 Custom Comfy 的待验证能力，不能据此承诺“全身份锁定”。

## 为什么这张 CyberRealistic XL 看起来泛化且不像角色

1. **变体强度过高。**`createVariant` 的 denoise 上限为 1；值越高越接近重绘。当前 0.58 同时要求更换场景、姿态、衣装后，模型自然更偏向 checkpoint 的默认美女分布。
2. **一个参考承担过多职责。**同一张图携带脸、发型、头向、衣服、光线和自拍构图；模型会复制其中部分表层特征，或为服从新 prompt 而放弃脸部特征。
3. **checkpoint 的审美偏置。**CyberRealistic XL 作者将它定位为“clean photorealistic”，并建议相对低 CFG；干净、锐利的默认审美并不等于手机废片/朋友拍摄的真实感。[作者说明](https://cyberrealistic.org/)
4. **参数没有按模型建议校准。**作者建议 CFG 3–5、30+ steps、SDXL 竖幅分辨率；偏离这些参数并不能解释所有漂移，但会增加不确定性。[模型说明镜像](https://civitaiarchive.com/models/312530?modelVersionId=611386)

## 社区经验：可采纳的部分与证据等级

社区内容是经验而非平台承诺；以下只作为实验假设。

| 经验 | 来源 | 如何落地 | 证据等级 |
|---|---|---|---|
| 少堆 LoRA；约 0.3–0.7 权重、SDXL 约 30 steps、Flux 约 20 steps、低 CFG 是常见起点 | [r/civitai 讨论](https://www.reddit.com/r/civitai/comments/1f05ced/) | 只在基线稳定后逐一加入“手机影像/业余摄影” LoRA，记录版本和权重 | 社区经验 |
| 数据集/LoRA 中存在真实手机影像、自然姿势、不同表情与场景，才更可能摆脱通用 AI 肖像 | [r/StableDiffusion 讨论](https://www.reddit.com/r/StableDiffusion/comments/1fro5z1/) | 将“摄影真实性”作为独立风格资产，不污染身份参考资产 | 社区经验 |
| CyberRealistic 适合高质量写实；不同 checkpoint 会决定真实感上限 | [r/comfyui 讨论](https://www.reddit.com/r/comfyui/comments/1lk94cg/) | 用固定测试矩阵比较 Cyber XL、Cyber Flux 和候选写实 checkpoint，而非单张主观选择 | 社区经验 |
| IP-Adapter 是“一张图条件”的工具，FaceID 需要对应 LoRA；InstantID 依赖人脸模型及 ControlNet，且 SDXL 支持成熟 | [IP-Adapter 官方实现](https://github.com/comfyorg/comfyui-ipadapter)、[InstantID 官方实现](https://github.com/cubiq/ComfyUI_InstantID) | 仅在 Civitai Custom Comfy 验证可用后采用；FaceID 作身份主约束，OpenPose/Depth 作几何约束 | 一手实现文档，非 Civitai 云端可用性证明 |

## 建议的云端验收计划（有限消耗）

### A. 先做标准 API 基线，不碰 Custom Comfy

每个样例保持事件、表达候选、prompt 和 canonical 参考不变，只变：

| 试验 | 模型/参数 | 目的 |
|---|---|---|
| A1 | CyberRealistic XL，30 steps，CFG 4，DPM++ 2M SDE Karras，denoise .28 | 身份优先的上限 |
| A2 | 同 A1，denoise .36 | 身份与动作折中 |
| A3 | 同 A1，denoise .44 | 动作/场景变化上限 |
| B1 | CyberRealistic Flux（仅在同一参考与计划下） | 判断 Flux 对 prompt/动作更好是否以身份为代价 |
| C1 | A2 + OpenPose 或 Depth 控制图 | 判断姿势控制是否降低“泛化时尚片”感 |

每张由现有 `MediaInspection` 记录 identity、camera geometry、真实性和身体/衣装证据，按分数选择，不以单张漂亮与否决定默认。

### B. 身份资产的职责分离

- **canonical face reference：** 正面、中性、清晰、非侧脸；只服务身份。
- **angle anchor：** 左/右三分之四、侧面、全身比例，按 `CameraGeometry` 选；不可单独充当唯一身份图。
- **wardrobe/state evidence：** 仅证明衣装/状态，不能替代脸。
- **pose/depth control：** 仅控制动作和构图，不能拿同一张身份参考重用为姿势控制图。

这比“上传越多参考越好”更可维护。标准 API 只有一张 `createVariant.image` 时，应选与镜头几何最接近的 canonical/angle anchor；多参考 FaceID 融合留给 Custom Comfy 验证。

## 决策边界

- **可以现在做：** 修正 Cyber XL 参数、降低变体 denoise、接入可选 ControlNet、在 `MediaInspection` 中记录 `identity_binding=variant_only` 和实验参数。
- **需要先验证再做：** Custom Comfy 的 IP-Adapter/InstantID/FaceID 资源清单、节点图、稳定成本和安全/失败回退。
- **不应做：** 因单张失败就把全部角色媒体迁到 Civitai；或把“可上传一张变体图”宣传成身份锁定。

## 不确定性

Civitai OpenAPI 能证明标准变体、ControlNet、LoRA、Custom Comfy 图形入口存在；它不能证明某个 FaceID/InstantID nodepack 在当前消费者账户、当前云端 worker 和指定 checkpoint 上可调度。模型卡与论坛建议也不保证你的角色、参考集和私密媒体分布。因此须以小矩阵、冻结输入和视觉验收决定是否推广。

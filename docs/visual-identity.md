# Visual Identity

沈知栀的自拍/生活照不应只靠一句 prompt 随机生成。当前项目采用三阶段路线。

## Phase A: Visual Bible

已加入 `configs/visual_identity.yaml`，固定她的发型、青绿色发夹、穿搭色系、气质和禁用项。`life_image_prompt(..., kind="selfie")` 会自动带入这个锚点。

这一阶段不能保证每次脸完全一致，但可以显著减少漂移，并让人工筛选有统一标准。

## Phase B: Reference Set

v2 参考集是当前唯一的身份基线；旧的 `celia-reference-*` 仅保留作视觉开发对照，不能混进 v2 LoRA 数据。

- `celia-v2-reference-01-canonical.png`: 主脸、青绿色发夹和两颗痣的身份锚点。
- `celia-v2-reference-02-no-hairclip.png`: 移除发夹的单变量检查图。
- `celia-v2-reference-03-angle-10deg.png`: 轻微侧脸检查图。
- `celia-v2-hairstyle-variants/`: 松散低丸子头——它是及肩短发长出后盘起的造型，不是第二个长发身份。

日常自拍使用前三张；亲密关系资料是独立的受限集，不参与基础 LoRA 训练，也不会响应普通自拍请求。

## Phase C: Local Consistency Workflow

如果需要“每次都像同一个人”，应考虑本地 ComfyUI 工作流：

- LoRA/DreamBooth：一致性强，但需要准备多张参考和训练。
- IP-Adapter/FaceID：更轻，适合用参考图做身份约束，但稳定度通常低于训练 LoRA。
- Reference-only prompt：最便宜，但漂移最大，只适合早期试用。

当前实现支持两条逐步切换的渲染路径：

- `IMAGE_BACKEND=openai`：OpenAI 图片编辑接口携带 v2 参考图，适合立即试用。
- `IMAGE_BACKEND=comfyui`：提交本机 ComfyUI API 工作流，适合随后接入 SDXL LoRA、IP-Adapter 或 FaceID。
- `IMAGE_BACKEND=auto`：若配置了本地工作流，先尝试本机；失败后回退 OpenAI。

最小环境配置如下：

```bash
ALLOW_AUTO_IMAGE_GENERATION=true
IMAGE_BACKEND=auto
OPENAI_API_KEY=...
# 等本地 LoRA 可用后再设置：
COMFYUI_BASE_URL=http://127.0.0.1:8188
COMFYUI_WORKFLOW_PATH=configs/comfyui/celia-v2-api-workflow.json
COMFYUI_LORA_PATH=models/loras/celia-v2.safetensors
```

本地工作流是从 ComfyUI 导出的 API JSON；字符串值可使用 `$PROMPT`、`$NEGATIVE_PROMPT`、`$LORA_PATH`、`$REFERENCE_IMAGE_1` 等占位符。这样图节点和 LoRA/IP-Adapter 节点仍由 ComfyUI 管理，而不是硬编码在服务里。

可选设置 `IMAGE_QUALITY_GATE_ENABLED=true`：图片会先经视觉检查，若脸、手、文字水印或主题不合格则重试一次。它会额外产生视觉调用，因此默认关闭。

QQ 适配器使用“文字先到、图片后到”的路径：它会先把角色的文字回复送出，再在后台完成生成并单独发送图片，同时以实际平台发送结果结算媒体 action。HTTP 调试接口仍同步返回图片，方便现有调用方和人工验收。

## Selfie Agency

自拍不是用户一要就发。当前聊天状态机会先判断关系阶段、信任、安全感、心情和用户语气：

- 刚认识、信任低、安全感低时，她会自然拒绝或说以后熟一点再给你看。
- 用户语气有压迫感时，她会守住边界，不生成图片。
- 关系和情绪允许时，才会把自拍请求交给图像生成器。
- 主动消息里，她可以很少地因为“突然想分享当下”发生活照或自拍，但仍受预算闸门限制。

`relationship_private` 目前只允许处于 `lover` 阶段、含有明确私密/亲密语境的自拍请求；若仍有高强度未解决伤害，必须先通过角色的自主 deliberation。生成提示固定为虚构成年人、非露骨、全程着装，不把争执修复或用户施压当作交换图片的机制。

### Relationship-media tiers

`relationship_private` 不是单一强度。用户可在明确的私密自拍请求中写 `soft`、`tender` 或 `bold`（中文“大方/大胆”也会选中 `bold`）；未写时默认 `soft`。

- `soft`：恋人阶段即可，暖光、宽松睡衣、克制的晚安自拍。
- `tender`：需要 closeness ≥ 12、respect ≥ 5、security ≥ 45、boundary < 35；允许更近的构图、肩颈和明确的温柔氛围。
- `bold`：必须由用户明确写出，且需要 closeness ≥ 20、respect ≥ 12、security ≥ 60、boundary ≤ 20，不能有 unresolved 负面情绪。它允许更自信的私密造型，但仍禁止裸露、明确性行为、胁迫或物化镜头。

三个档位分别有提示词、负面提示和参考集；不会因为单纯升级关系阶段而静默提高强度，也不会由主动消息自动触发 `bold`。

## Asset Strategy

表情包和生活图以后分三类处理：

- 固定资产：当前 `assets/stickers/` 里的稳定表情包，适合高频、低成本发送。
- 生成资产：自拍、生活照、食物、路边随手拍等，走视觉身份锚点和预算闸门。
- 检索资产：可以以后加图片搜索，但默认不启用。原因是版权、热链、风格一致性、内容安全和来源稳定性都比生成资产更难控制。

无论是哪一类，都应接受同一个人物形象约束：沈知栀不是随机图库角色，而是一个持续存在的虚拟人物。

## 参考

- RunDiffusion Consistent Character Template: https://learn.rundiffusion.com/consistent-character-template/
- Scenario character consistency guide: https://www.scenario.com/blog/how-to-create-consistent-ai-characters-with-a-single-image
- OpenAI image and vision guide: https://platform.openai.com/docs/guides/images-vision

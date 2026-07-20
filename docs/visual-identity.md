# Visual Identity

沈知栀的自拍/生活照不应只靠一句 prompt 随机生成。当前项目采用三阶段路线。

## Phase A: Visual Bible

已加入 `configs/visual_identity.yaml`，固定她的自然发型、五官特征、穿搭倾向、气质和禁用项。`life_image_prompt(..., kind="selfie")` 会自动带入这个锚点。

这一阶段不能保证每次脸完全一致，但可以显著减少漂移，并让人工筛选有统一标准。

## Phase B: Reference Set

当前十张参考图是唯一身份基线，其中 `08-cafe-phone-canonical.png` 是日常主锚点。其余九张覆盖自然侧脸、回头、坐姿、环境互动、近景私密、运动与游泳状态；它们只定义身份和大致机位，不复制原图衣装、地点、表情、头偏或单侧露耳。

日常路径只使用日常锚点；低强度私密可用床边近景锚点；运动/游泳参考只允许受冻结事件支持的高张力路径，不参与普通生活图。

## Phase C: Local Consistency Workflow

如果需要“每次都像同一个人”，应考虑本地 ComfyUI 工作流：

- LoRA/DreamBooth：一致性强，但需要准备多张参考和训练。
- IP-Adapter/FaceID：更轻，适合用参考图做身份约束，但稳定度通常低于训练 LoRA。
- Reference-only prompt：最便宜，但漂移最大，只适合早期试用。

当前实现支持两条逐步切换的渲染路径：

- `IMAGE_BACKEND=openai`：OpenAI 图片编辑接口携带 v2 参考图，适合立即试用。
- `IMAGE_BACKEND=ark`：方舟 Seedream 图像生成接口携带同一批本地 Base64 参考图；适合规避本机 OpenAI 代理波动并控制单张成本。
- `IMAGE_BACKEND=comfyui`：提交本机 ComfyUI API 工作流，适合随后接入 SDXL LoRA、IP-Adapter 或 FaceID。
- `IMAGE_BACKEND=auto`：若配置了本地工作流，先尝试本机；失败后优先回退方舟，再回退 OpenAI；未配置本地工作流时则优先方舟。

最小环境配置如下：

```bash
ALLOW_AUTO_IMAGE_GENERATION=true
IMAGE_BACKEND=auto
OPENAI_API_KEY=...
OPENAI_PROXY_URL=http://127.0.0.1:7897  # 可选：仅 OpenAI 请求走本机代理
# 或者使用方舟；macOS GUI 环境中的 ARK_API_KEY 也会被本机 daemon 安全读取：
ARK_API_KEY=...
ARK_IMAGE_MODEL=doubao-seedream-4-0-250828
ARK_IMAGE_SIZE=2K
# 等本地 LoRA 可用后再设置：
COMFYUI_BASE_URL=http://127.0.0.1:8188
COMFYUI_WORKFLOW_PATH=configs/comfyui/celia-v2-api-workflow.json
COMFYUI_LORA_PATH=models/loras/celia-v2.safetensors
```

`ARK_IMAGE_MODEL` 必须是当前方舟账号已开通的模型或 Endpoint ID；账号未开通默认 Seedream 模型时，生成器会明确报出 `ModelNotOpen` 的处理方向，而不会静默回退到 OpenAI。

显式设为 `IMAGE_BACKEND=ark` 时，图片生成和投递不再依赖 OpenAI 代理；当前 OpenAI 视觉验收器会自动停用，避免其网络波动阻断已成功生成的方舟图片。后续可在同一验收接口接入方舟视觉验收。

本地工作流是从 ComfyUI 导出的 API JSON；字符串值可使用 `$PROMPT`、`$NEGATIVE_PROMPT`、`$LORA_PATH`、`$REFERENCE_IMAGE_1` 等占位符。这样图节点和 LoRA/IP-Adapter 节点仍由 ComfyUI 管理，而不是硬编码在服务里。

`IMAGE_QUALITY_GATE_ENABLED` 默认开启：图片会先经视觉检查，验收脸、手、文字水印、主题与主身份锚点；不合格时会将失败原因加入纠错提示后重试一次。连续失败或验收不可用时不投递图片。

QQ 适配器使用“文字先到、图片后到”的路径：它会先把角色的文字回复送出，再在后台完成生成并单独发送图片。媒体请求会将目标消息的最小投递上下文写入世界账本；QQ 重连后可恢复仍处于 requested/generated 的媒体任务。后台图片发送必须拿到平台回执才会结算为成功。

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

## Personal media modes

人物照片不再都视为手持自拍。系统会把镜头方式和关系强度分开：`handheld_selfie`、`check_in_timer`（定时/支架打卡）、`mirror`、`candid_life`（仅有世界情境依据的他拍）和 `unfiltered`（自然、不精致但不羞辱的分享）都属于个人媒体。

世界个人媒体在生成前会冻结一份 `MediaShotPlan`：它记录当前活动、逻辑时间、地点、已登记同伴、动作、视线、景别、机位与禁止项，并被写入媒体 Action。后台重试或进程恢复复用同一计划，不能根据后来变化的日程重写照片故事；没有 active 活动时，计划不会把用户请求中的旅行或地点当作已经发生的事实。

`unfiltered` 可以包含困脸、风吹乱发、闭眼、运动后微红或略尴尬的角度，但不能变成畸形、疾病化或贬低角色。close_friend 起可低频主动分享带明确生活事件依据的这一类图片；同类主动分享间隔至少七天。`bold` 永不主动触发。

用户可直接在聊天中控制主动媒体偏好：`不要主动发图` / `可以主动发图`，`不要发丑照` / `可以发丑照`，以及 `照片少一点`（把非精致照冷却期调为 14 天）或 `照片正常`（恢复 7 天）。这些控制只约束角色主动分享，不会拒绝关系规则允许的明确图片请求。

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

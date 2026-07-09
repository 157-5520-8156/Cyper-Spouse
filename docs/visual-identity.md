# Visual Identity

沈知栀的自拍/生活照不应只靠一句 prompt 随机生成。当前项目采用三阶段路线。

## Phase A: Visual Bible

已加入 `configs/visual_identity.yaml`，固定她的发型、青绿色发夹、穿搭色系、气质和禁用项。`life_image_prompt(..., kind="selfie")` 会自动带入这个锚点。

这一阶段不能保证每次脸完全一致，但可以显著减少漂移，并让人工筛选有统一标准。

## Phase B: Reference Set

后续应筛出 6-12 张稳定参考图，覆盖：

- 正脸、半侧脸、不同光线。
- 图书馆、路边、宿舍桌面、咖啡店等日常场景。
- 同一发夹、相近脸型、相近穿搭色系。

这组图作为之后更强一致性工作流的基准。

## Phase C: Local Consistency Workflow

如果需要“每次都像同一个人”，应考虑本地 ComfyUI 工作流：

- LoRA/DreamBooth：一致性强，但需要准备多张参考和训练。
- IP-Adapter/FaceID：更轻，适合用参考图做身份约束，但稳定度通常低于训练 LoRA。
- Reference-only prompt：最便宜，但漂移最大，只适合早期试用。

项目当前先停在 Phase A，避免在没有参考集和预算控制前直接进入高成本图片流。

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

# Visual Identity

沈知栀的自拍/生活照不应只靠一句 prompt 随机生成。当前项目采用三阶段路线。

## Phase A: Visual Bible

已加入 `configs/visual_identity.yaml`，固定她的发型、青绿色发夹、穿搭色系、气质和禁用项。`life_image_prompt(..., kind="selfie")` 会自动带入这个锚点。

这一阶段不能保证每次脸完全一致，但可以显著减少漂移，并让人工筛选有统一标准。

## Phase B: Reference Set

已生成第一组 GPT 参考图，作为 LoRA/IP-Adapter 之前的视觉基准：

- `assets/reference/celia-reference-01-portrait.png`: 图书馆窗边正脸参考。
- `assets/reference/celia-reference-02-campus.png`: 校园半身参考。
- `assets/reference/celia-reference-03-desk-selfie.png`: 桌面前置自拍参考。
- `assets/reference/celia-reference-04-cafe-profile.png`: 咖啡店半侧脸参考。

这组图还不是 LoRA。它的作用是先固定脸型、发夹、发色、穿搭色系和气质，让后续所有自拍/生活照有一个人工筛选基准。真正训练 LoRA 前，建议继续扩到 8-12 张，剔除脸部漂移明显的图，再做裁切和标签。

## Phase C: Local Consistency Workflow

如果需要“每次都像同一个人”，应考虑本地 ComfyUI 工作流：

- LoRA/DreamBooth：一致性强，但需要准备多张参考和训练。
- IP-Adapter/FaceID：更轻，适合用参考图做身份约束，但稳定度通常低于训练 LoRA。
- Reference-only prompt：最便宜，但漂移最大，只适合早期试用。

项目当前处于 Phase B 初版：参考图已经落库，LoRA/FaceID 尚未训练。这样可以先低成本验证“沈知栀长什么样”，再决定是否进入本地训练。

## Selfie Agency

自拍不是用户一要就发。当前聊天状态机会先判断关系阶段、信任、安全感、心情和用户语气：

- 刚认识、信任低、安全感低时，她会自然拒绝或说以后熟一点再给你看。
- 用户语气有压迫感时，她会守住边界，不生成图片。
- 关系和情绪允许时，才会把自拍请求交给图像生成器。
- 主动消息里，她可以很少地因为“突然想分享当下”发生活照或自拍，但仍受预算闸门限制。

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

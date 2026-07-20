# Cost Control

目标预算：每月约 80 元人民币，日硬上限约 3 元人民币，日自动调用软上限约 2 元人民币。

## 默认策略

- 日常文字聊天继续使用 DeepSeek，不把所有消息都转到 OpenAI。
- OpenAI 只用于必要的多模态能力：图片理解、语音转写、少量虚拟生活照/自拍。
- 图片生成默认不自动触发。显式运行 `companion-life-event --generate-image` 会尝试；普通聊天里的图片/自拍请求只有在 `ALLOW_AUTO_IMAGE_GENERATION=true`、人格边界允许、且预算闸门允许时才会自动生成。
- 主动自拍/生活照属于稀有自动行为：必须先由主动状态机决定“她自己想分享”，再通过关系状态和预算闸门。
- 同一个附件 URL 的理解结果会复用本地记忆，避免重复调用视觉/转写。
- 每次 OpenAI 多模态调用都会写入 `usage_events`，记录的是本项目的人民币估算，不是官方账单。

## 当前闸门

- `MONTHLY_BUDGET_CNY=80`
- `DAILY_BUDGET_CNY=3`
- `SOFT_DAILY_BUDGET_CNY=2`
- `MONTHLY_IMAGE_LIMIT=20`
- `MONTHLY_VISION_LIMIT=120`
- `MONTHLY_AUDIO_LIMIT=60`

## 图片渲染计划

人物自拍默认使用 `1024x1536`、`medium` 质量、主身份锚点加一张场景变体。预算会按参考图数量和最多两次尝试预留；成功后按实际尝试次数写入本地账本。

以两张竖版高保真参考图估算，单次约 ¥1.05；启用身份验收并预留一次纠错时，自动调用会先按约 ¥2.10 检查日预算。实际账单仍以 OpenAI 后台为准。

## 其他估算单价

- 图片理解：`0.03 CNY`
- 语音转写：`0.05 CNY`
- 图片生成：由渲染计划动态估算，不再使用固定 `0.35 CNY`

这些数字用于本地拦截和保守规划。真实价格以 OpenAI 后台账单为准，模型或汇率变化时应更新这里和 `src/companion_daemon/budget.py`。

## 参考

- OpenAI cost optimization guide: https://platform.openai.com/docs/guides/cost-optimization
- OpenAI image and vision guide: https://platform.openai.com/docs/guides/images-vision
- OpenAI speech to text guide: https://platform.openai.com/docs/guides/speech-to-text

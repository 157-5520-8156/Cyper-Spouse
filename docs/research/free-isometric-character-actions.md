# 免费等距角色动作复用结论

更新日期：2026-07-11

## 首选

采用 Hormelz 的 [8 Directional 2DHD Pixel Art Animated Character](https://hormelz.itch.io/8-directional-char)。
创作者页面明确说明它是 8 方向、2.5D/等距兼容的像素女性角色，采用 CC0 1.0；每种
动作提供独立下载文件。现有动作包括 walk、run、ready idle、push、jump、roll、block、
boost、近战、受伤和死亡，画布为 256×256。它可直接取代目前自制的行走/待机动作，不应
重新绘制这些基础帧。

来源：<https://hormelz.itch.io/8-directional-char>

## 适配方案

- `walk`、`idle`：直接复用；按脚底坐标参与现有深度排序。
- `push`：复用为整理桌面、推椅子或开门等环境互动。
- `ready idle`：复用为看手机、等消息、发呆的基础姿态。
- `run`、`roll`、`jump`：只在出门或低概率活跃状态使用，不为日常交互额外造动画。

## 已知缺口

该包没有坐书桌、躺床、读书、吃饭或洗漱。这些动作没有找到同风格、免费、可核验的完整
等距生活动作包。后续只为这些高频缺口补最小资产；不重新制作已有的行走、待机和交互帧。

另一个可选的 CC0 基础包是 Supernova Files 的
[Isometric Character Asset Pack](https://supernovafiles.itch.io/isometric-asset-pack)，含 idle、walk、run、
crouch 和 punch 及 Aseprite 源文件，但女性外观和日常交互覆盖不如 Hormelz。

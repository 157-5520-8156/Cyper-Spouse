# Dashboard 小屋 AI 素材接入模板

最后更新：2026-07-12

## 原则

AI 只提供候选美术，不提供房间结构事实。任何新素材都必须先声明 footprint、可达 approach、交互姿态和前后审计点，再进入 Compiler。禁止把 AI 生成的整张房间直接覆盖视觉母版。

固定参考：

- 视觉母版：`assets/dashboard/zhizhi-room-isometric-v2.png`
- 角色比例：`assets/dashboard/zhizhi-iso-walk-v4.png`
- 场景清单：`assets/dashboard/rooms/zhizhi-home/room.json`
- 校准页面：`http://127.0.0.1:8765/dashboard?demo=room-editor`

## 生成前输入清单

- 家具名称和用途。
- 在母版中的目标区域裁切图。
- 左右等距方向；本房间使用约 2:1 的等距菱形网格。
- 目标像素包围框，不能让模型自行决定画布尺寸。
- 光源来自房间右上及室内暖光，阴影方向必须与母版一致。
- 暖棕、灰绿、奶油色的低饱和像素风；禁止平滑 3D、照片纹理和抗锯齿边缘。
- 透明背景，家具之外 alpha 为 0。
- 明确需要 `master-matte` 还是独立 `back/front`。

## 通用提示词模板

```text
参考提供的知栀小屋母版裁切与角色比例，生成一个【家具名称】的 2.5D 等距像素素材。

结构：家具面向【左下/右下】，占用【W × H】个房间网格；画布固定为【像素宽 × 像素高】，家具必须完整落在画布内。
风格：低饱和暖棕、灰绿与奶油色，像素边缘清晰，不使用平滑抗锯齿、照片纹理或写实 3D 材质。
光线：继承参考图右上方暖光与室内灯光，阴影方向和明暗关系不得改变。
输出：透明背景 PNG；家具外 alpha=0；不要生成房间、地板、墙、人物、文字或额外摆件。
分层要求：【见下面的模式要求】。
```

生成时必须把母版裁切和角色比例图作为图像参考一同提供，不能仅靠文字描述“像素风”。

## 模式 A：母版取色 matte

适用：家具已经存在于扁平母版中，只缺角色遮挡轮廓。

提示词追加：

```text
只输出家具靠近观察者、应遮住角色的前侧轮廓。透明区必须保留；不需要准确颜色，最终系统只使用 alpha 通道。不要包含家具后侧、地板、阴影或周围物体。
```

清单示例：

```json
"frontOccluder": {
  "matte": "../../layers/new-chair-front.png",
  "matteCrop": [0, 0, 160, 120],
  "output": "new-chair-front.png",
  "origin": [640, 420],
  "depthBias": 500
}
```

Compiler 会从母版同坐标区域取得 RGB，只沿用 AI 图 alpha，因此不会出现第二套错位家具颜色。

## 模式 B：独立 back/front 分层

适用：母版中原本不存在的新家具，或者角色需要进入家具内部空间。

要求生成两张相同画布、相同原点的透明 PNG：

- `back`：角色永远应该站在其前面的主体和远侧部分。
- `front`：角色经过家具后方时应该盖住角色的近侧边缘、扶手、桌沿或床沿。

两张图叠加后必须还原一件完整家具，不能有重复半透明边缘。

清单示例：

```json
"backLayer": {
  "source": "sources/new-chair-back.png",
  "output": "new-chair-back.png",
  "origin": [640, 420]
},
"frontOccluder": {
  "source": "sources/new-chair-front.png",
  "output": "new-chair-front.png",
  "origin": [640, 420],
  "depthBias": 500
}
```

Compiler 会原样保留独立分层的 RGB 与 alpha；Runtime 按 `back → actor → front` 绘制。

## 接入步骤

1. 在 `room.json` 添加对象 id、`depthTile`、`footprint`、`audit.behind/front`。
2. 添加 interaction 的 `object`、`location`、`action`、`approach`、`posePosition`、`pose`、`facing` 与语义 depth。
3. 将候选图放入房间 `sources/` 或现有 `assets/dashboard/layers/`。
4. 运行 `uv run python scripts/build_dashboard_room.py`。
5. 修复 Compiler 报告的边界、透明度、重名、路径或连通问题；禁止绕过校验直接复制 runtime 文件。
6. 打开 `?demo=room-editor`，检查网格、footprint、交互点、alpha 和 origin。
7. 打开该对象的 `behind/front` 以及所有动作预览。
8. 让 tour 路线实际经过附近区域，确认连续运动中的深度变化。
9. 运行相关测试及完整 pytest，记录到视觉 worklog。

## 拒收条件

- 风格、光向、像素密度或比例与母版明显不一致。
- alpha 全透明、全不透明或带有大块背景。
- `back/front` 叠加后出现重影、缝隙或颜色跳变。
- footprint 与图像体积不符，角色会穿过家具实体。
- approach 不可达，或只能靠 `posePosition` 瞬移掩盖寻路错误。
- 只有正面静态截图正确，但 `behind/front` 或连续 tour 中错误。

## 最小验收记录

```text
素材：
模式：master-matte / independent-layers
参考图：
清单对象 id：
Compiler：通过 / 错误摘要
behind：通过 / 问题
front：通过 / 问题
动作：通过 / 问题
tour：通过 / 问题
控制台：无错误 / 错误摘要
测试：
```

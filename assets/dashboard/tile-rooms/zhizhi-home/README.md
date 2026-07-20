# 知栀格驱动小屋

本场景不读取 `zhizhi-room-isometric-v2.png`，也不从旧母版提取遮挡层。几何、碰撞、寻路、墙体和家具都由 `room.json` 的格坐标与尺寸生成。

坐标单位是 tile；投影固定为 `screen = origin + (x-y)*(64,32) - z*(0,64)`。每件家具的 `transform` 描述视觉盒体，`collider` 描述角色不能进入的格子。

碰撞的唯一规则是地板占用：除 `occupancy: "decor"` 的墙面/悬挂装饰外，每件家具的 `transform` 会自动占用其覆盖的所有整格地板；`collider` 只是导出的、必须与这个占用结果完全一致的清单。编译器和运行时会拒绝不一致的 manifest，A* 只在未占用格移动。

AI 素材只允许作为贴图或姿态 overlay：材质必须无透视、可平铺，角色/装饰必须使用透明背景或 chroma-key 去背。

当前材质样张：`assets/oak-seamless-v1.png`；`assets/oak-seamless-v1-2x2-preview.png` 是 2×2 平铺验收图。它由内置 AI 生图生成，提示词要求无透视、无阴影与四边平铺；运行时只把它作为 oak 材质的低透明度细节层，几何仍完全由格投影生成。

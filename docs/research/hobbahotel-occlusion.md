# Bobba/Habbo HTML5 复刻：遮挡、脚底深度与格占用复核

调研日期：2026-07-13。用户所说的 “HobbaHotel HTML5 复刻版” 在公开源码中最接近的是 **Bobba**（`Josedn/bobba_client`），一个 Habbo r60+ 的 HTML5/PixiJS 复刻客户端。下文把它与专门的房间渲染器 **Shroom** 交叉核对。结论是借鉴空间模型和渲染原则，不复制任何客户端代码、资产或其资源格式。

## 可核验的一手来源

| 项目 | 固定版本 | 与本任务的关系 | 许可证边界 |
| --- | --- | --- | --- |
| [Bobba client](https://github.com/Josedn/bobba_client/tree/8e4e5524f0ece22f1a2725f8bb709d63642a4f2f) | `8e4e552`（仓库已归档） | 用户所称项目最可能的拼写；完整 HTML5 客户端。README 明确是 PixiJS/React/TypeScript 的 Habbo 复刻，列出 walking、sitting、furni animations。 | [GPL-3.0](https://github.com/Josedn/bobba_client/blob/8e4e5524f0ece22f1a2725f8bb709d63642a4f2f/LICENSE)。不可把其实现复制或改写后作为本项目的闭源/非 GPL 模块发布。 |
| [Shroom](https://github.com/jererobles/shroom/tree/e34ae4eb49e2118c37dc9fe8e28d1e0fa93587c7) | `e34ae4e` | 独立房间渲染器，最新源码更适合核对“正确渲染”的模块分界。 | [LGPL-3.0-or-later 声明](https://github.com/jererobles/shroom/blob/e34ae4eb49e2118c37dc9fe8e28d1e0fa93587c7/package.json#L1-L8) 及 [COPYING.LESSER](https://github.com/jererobles/shroom/blob/e34ae4eb49e2118c37dc9fe8e28d1e0fa93587c7/COPYING.LESSER)。本项目仍不应复制源码或 Habbo 资产。 |

Bobba 的 [README](https://github.com/Josedn/bobba_client/blob/8e4e5524f0ece22f1a2725f8bb709d63642a4f2f/README.md#L5-L24) 也说明头像与家具资源不包含在客户端中、需自行提供；这不能构成使用 Habbo 资源的授权。

## 源码实际采用的机制

### 1. 房间坐标、角色脚底与深度值来自同一组格坐标

Bobba 的 `tileToLocal` 将 `(x, y, z)` 投影为 `(x-y)*tileWidth`、`(x+y)*tileHeight-z*tileHeight*2`：[RoomEngine](https://github.com/Josedn/bobba_client/blob/8e4e5524f0ece22f1a2725f8bb709d63642a4f2f/src/bobba/rooms/RoomEngine.ts#L417-L419)。角色位置更新同样先投影脚底格，再每次更新其容器深度；阴影使用地面高度而角色使用角色自己的 `z`：[RoomUser](https://github.com/Josedn/bobba_client/blob/8e4e5524f0ece22f1a2725f8bb709d63642a4f2f/src/bobba/rooms/users/RoomUser.ts#L274-L295)。

深度不是屏幕像素 `y` 的临时比较。Bobba 以 `x+y`、`z` 和绘制优先级组合深度键，角色再加很小的 `z` 偏移以处理同格关系：[深度函数](https://github.com/Josedn/bobba_client/blob/8e4e5524f0ece22f1a2725f8bb709d63642a4f2f/src/bobba/rooms/RoomEngine.ts#L647-L682)。Shroom 也将 avatar 的 `roomX/Y/Z` 和显示位置绑定，移动插值期间每帧重算位置和 z-index：[Avatar](https://github.com/jererobles/shroom/blob/e34ae4eb49e2118c37dc9fe8e28d1e0fa93587c7/src/objects/avatar/Avatar.ts#L621-L670)。

**可借鉴规则：** 角色必须具有唯一的 `foot`（逻辑脚底）锚点；站立、坐下、躺下的视觉 PNG 都以该锚点对齐。动作切换不能以图片裁切宽高决定世界大小，也不能单独改屏幕坐标。否则“互动时人物突然变大/跳位”必然发生。

### 2. 遮挡不是“物体整体 vs. 人物整体”，而是家具的绘制部件共同排序

Bobba 的房间根容器开启 `sortableChildren`：[RoomEngine 初始化](https://github.com/Josedn/bobba_client/blob/8e4e5524f0ece22f1a2725f8bb709d63642a4f2f/src/bobba/rooms/RoomEngine.ts#L45-L61)。更关键的是，每件家具会遍历资源定义给出的多个 layer；每个 layer 各有 texture、像素偏移、透明度、翻转、混色以及 `layer.z`，并被赋予独立 z-index：[RoomItem.updateTextures](https://github.com/Josedn/bobba_client/blob/8e4e5524f0ece22f1a2725f8bb709d63642a4f2f/src/bobba/rooms/items/RoomItem.ts#L159-L225)。

Shroom 的结构相同：房间分别保有 `behindWall`、`wall`、`tile`、`landscape`、`primary`、`masks` 容器，动态物体所在的 primary 层可排序：[RoomModelVisualization](https://github.com/jererobles/shroom/blob/e34ae4eb49e2118c37dc9fe8e28d1e0fa93587c7/src/objects/room/RoomModelVisualization.ts#L42-L115)；家具图层把资源层的 `z` 写成相对于家具基础 z-index 的偏移：[FurnitureVisualizationView](https://github.com/jererobles/shroom/blob/e34ae4eb49e2118c37dc9fe8e28d1e0fa93587c7/src/objects/furniture/FurnitureVisualizationView.ts#L397-L450)。

这解释了它为何能处理沙发扶手、桌沿、床沿遮住角色：这些像素不是用矩形裁掉角色，而是作为透明 PNG 的近景部件，在角色之后绘制。角色走到家具近侧时，脚底深度改变，顺序自然反转。

**可借鉴规则：** 每件可遮挡家具的渲染 Interface 至少是：

```ts
type RenderPart = {
  role: "back" | "body" | "frontOccluder" | "overlay";
  depthAnchor: { x: number; y: number; z: number };
  depthBias: number;
  stableOrder: number;
  draw: CanvasRenderingContext2D => void;
};
```

程序化盒体也必须输出多个 `RenderPart`，不能把 top/left/right 在一个 `drawBox()` 调用里整体画完。对占 `width × depth` 的盒体，近侧（投影后更低、`x+y` 更大）的前沿/前脚应成为 `frontOccluder`，其 `depthAnchor` 取该前沿，而非取家具原点。真正的软装则需透明的前景 overlay（如床沿、沙发扶手），由其 alpha 轮廓决定遮挡边界。

### 3. 墙和门是明确的渲染层，不是悬浮家具

Shroom 会把门格地板放入 `behindWall`，并在 avatar 位于 door tile 时，将 avatar 容器移入 `behindWallContainer`：[门的构造](https://github.com/jererobles/shroom/blob/e34ae4eb49e2118c37dc9fe8e28d1e0fa93587c7/src/objects/room/RoomModelVisualization.ts#L481-L487) 与 [avatar 的容器切换](https://github.com/jererobles/shroom/blob/e34ae4eb49e2118c37dc9fe8e28d1e0fa93587c7/src/objects/avatar/Avatar.ts#L649-L675)。Bobba 同样独立建立墙和地板，并为墙赋独立优先级：[墙/地板生成](https://github.com/Josedn/bobba_client/blob/8e4e5524f0ece22f1a2725f8bb709d63642a4f2f/src/bobba/rooms/RoomEngine.ts#L321-L415)。

**可借鉴规则：** 窗、门、墙挂是 `wallPart`，具有墙面锚点与墙层，不属于有地面 footprint 的家具盒体；墙前、门后等少数语义场景才切换容器层。这样不会把窗渲成漂浮的立方体，也不会让角色渲染到墙的错误一侧。

### 4. 高度图描述“哪格存在及其标高”；它不是家具碰撞的替代品

Bobba 的 `RoomModel` 将 `heightMap[x][y] !== 0` 定义为有效格：[RoomModel](https://github.com/Josedn/bobba_client/blob/8e4e5524f0ece22f1a2725f8bb709d63642a4f2f/src/bobba/rooms/RoomModel.ts#L1-L18)，地板/台阶也按相邻格高度差生成：[setFloor](https://github.com/Josedn/bobba_client/blob/8e4e5524f0ece22f1a2725f8bb709d63642a4f2f/src/bobba/rooms/RoomEngine.ts#L372-L415)。它的客户端点击格后只是发送移动请求给服务端：[handleMouseClick](https://github.com/Josedn/bobba_client/blob/8e4e5524f0ece22f1a2725f8bb709d63642a4f2f/src/bobba/rooms/RoomEngine.ts#L512-L550)，没有可直接搬用的本地 A*。Shroom 的 `Avatar.walk` 注释也明确路径规划需由调用者实现：[Avatar.walk](https://github.com/jererobles/shroom/blob/e34ae4eb49e2118c37dc9fe8e28d1e0fa93587c7/src/objects/avatar/Avatar.ts#L324-L365)。

**可借鉴规则：** 本项目应继续保留本地 A*，但它的唯一输入必须是渲染同源的 `occupancyGrid`：

- `void / wall / reserved`：不可走；
- 有 floor furniture 的每一格：不可走（首版不做可踩桌面/座位）；
- `interaction.approach`：必须是可走格；
- `heightMap` 相邻高度差超过角色可跨越阈值：不可走；
- A*、点击拾取、编辑器红色碰撞提示都读取同一个网格。

这避免了“视觉是床，寻路表却忘了床”和“墙画出来了但角色仍走进墙”的双重真相。

## 应落到 TileRoom 的具体改动

1. **替换单一 depth key / 整体盒体绘制。** 把场景对象编译为 render-part 列表：`floor → wall/backdrop → furniture.back/body → actor.shadow → actor → furniture.frontOccluder → UI`。其中动态列表统一以 `(x+y, z, depthBias, stableOrder)` 稳定排序；不对某件家具或某个动作做名称特判。
2. **给 actor 固定世界尺度和锚点。** `actor.worldHeight` 与 `actor.footOffset` 在所有站/坐/睡帧不变；坐姿使用 `seatAnchor` 改变 `foot.z`，睡姿使用 `bedAnchor` 改变视觉朝向，但不按 PNG 原始宽高缩放人物。
3. **编译家具 occupancy 与 render parts。** `transform.width/depth` 生成整格占用；`occupancy: decor` 才允许不阻挡。wallPart 没有 floor occupancy；每个 furniture 的 `frontOccluder` 以其近侧边界为深度锚点。
4. **增加视觉回归场景。** 至少固定“角色在桌后/桌前、沙发后/沙发前、床后/床前、门后、两件家具同一对角线、坐下与睡下”七张基线。只有这些通过，才扩大装修和资产数量。

## 不能照搬的内容

- 不复制 Bobba/Shroom 任何源码、类结构、资源描述格式或 Habbo 家具/角色 PNG；本项目只独立实现通用的坐标投影、稳定排序、格占用和透明前景层原则。
- Shroom 的景观 mask 是“让窗洞露出景观”的机制，而不是把角色按矩形裁掉。常规家具遮挡应优先用 `frontOccluder` 的 alpha 轮廓。
- Bobba 的 `canPlaceFloorItem` 只在拖放家具时检查房间有效格及 footprint 边界：[RoomEngine](https://github.com/Josedn/bobba_client/blob/8e4e5524f0ece22f1a2725f8bb709d63642a4f2f/src/bobba/rooms/RoomEngine.ts#L249-L271)，不能误当作本项目的角色避障算法。

以上是源码行为层面的研究记录，不构成法律意见；若计划引入任一第三方代码、数据文件或游戏素材，应在引入前单独进行许可证与权利来源审查。

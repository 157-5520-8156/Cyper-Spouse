# Habbo 风格家具遮挡：源码复核（Shroom 与 bobba_client）

调研日期：2026-07-11。范围是公开的原始源码；本文只记录可借鉴的渲染机制，并不授权复制 Habbo 资源或将 GPL/LGPL 项目代码并入本项目。

## 结论

当前画面中人物被遮住时出现“一条水平、平齐的切口”，根源是把遮挡表示成一个矩形裁切区。这个表现**不应继续靠调矩形高度修补**。

但“应该做蒙版”只对了一半：两个 HTML5 复刻源码都没有把“家具遮住 avatar”实现为对 avatar 的通用矩形 `clip`。它们采用的是：

1. 家具资源本身有多个带透明像素的绘制层；
2. 每一层与角色都获得同一空间里的深度值（`zIndex`）；
3. 深度排序后，家具的前沿/扶手/床沿那张**带轮廓 alpha 的 PNG**自然绘制在角色前面。

因此本项目应把每件会遮人的家具拆成 `back` 与 `frontOccluder` 图层。`frontOccluder` 不是矩形，也不是从整张背景中按长方形抠的一块；它应是只保留沙发扶手、靠背前缘、桌沿、床沿等不透明像素的透明 PNG。角色与这些层共同按脚底/格坐标排序。这样露出的轮廓由资产 alpha 决定，切口会沿家具的真实像素边缘走。

## 源码证据

### 1. Shroom：房间容器 + 可排序的主层

Shroom 在 [`RoomModelVisualization.ts`](https://github.com/jankuss/shroom/blob/b34542491bdc571ad482d37b6a98211bbe488b88/src/objects/room/RoomModelVisualization.ts#L44-L113) 创建 `behindWall`、`wall`、`tile`、`landscape`、`primary`、`masks` 等独立 Pixi 容器，并将 `tile` 与 `primary` 设为 `sortableChildren = true`。也就是说，物件不是预先烙在一个背景位图里，而是作为可独立排序的 display object。

Shroom 的 [`getZOrder`](https://github.com/jankuss/shroom/blob/b34542491bdc571ad482d37b6a98211bbe488b88/src/util/getZOrder.ts#L1-L3) 以 `x * 1000 + y * 1000 + z` 生成房间对象的深度；地面家具在 [`FloorFurniture._updatePosition`](https://github.com/jankuss/shroom/blob/b34542491bdc571ad482d37b6a98211bbe488b88/src/objects/furniture/FloorFurniture.ts#L365-L385) 更新位置时采用它。家具精灵再在 [`FurnitureSprite`](https://github.com/jankuss/shroom/blob/b34542491bdc571ad482d37b6a98211bbe488b88/src/objects/furniture/FurnitureSprite.ts#L6-L80) 中把对象基础深度与资源层的 `offsetZIndex` 相加。

家具多层不是猜测：[`FurnitureVisualizationView`](https://github.com/jankuss/shroom/blob/b34542491bdc571ad482d37b6a98211bbe488b88/src/objects/furniture/FurnitureVisualizationView.ts#L122-L153) 会为 draw definition 的每个 part 创建一个 `FurnitureVisualizationLayer`；创建精灵时将该 part 的 `z` 写入 `offsetZIndex`（[同文件](https://github.com/jankuss/shroom/blob/b34542491bdc571ad482d37b6a98211bbe488b88/src/objects/furniture/FurnitureVisualizationView.ts#L405-L450)）。其家具数据接口也明确层具有 `z`、`alpha`、`ink` 等属性（[`IFurnitureVisualizationData.ts`](https://github.com/jankuss/shroom/blob/b34542491bdc571ad482d37b6a98211bbe488b88/src/objects/furniture/data/interfaces/IFurnitureVisualizationData.ts#L36-L55)）。

### 2. Shroom 的 mask：是“透过窗户显示景观”，不是通用家具遮挡

Shroom 的 [`RoomLandscapeMaskSprite`](https://github.com/jankuss/shroom/blob/b34542491bdc571ad482d37b6a98211bbe488b88/src/objects/room/RoomLandscapeMaskSprite.ts#L8-L24) 注释直接说明用途：窗户家具给出黑色 mask 图，遮罩只让 landscape 在 mask 区域显示。实现会把多个 mask sprite 合成 `RenderTexture`，并因 Pixi 使用白色遮罩而应用反色滤镜（[同文件](https://github.com/jankuss/shroom/blob/b34542491bdc571ad482d37b6a98211bbe488b88/src/objects/room/RoomLandscapeMaskSprite.ts#L67-L90)）。[`Landscape`](https://github.com/jankuss/shroom/blob/b34542491bdc571ad482d37b6a98211bbe488b88/src/objects/room/Landscape.ts#L92-L105) 再把该 mask 指给 landscape wall。

所以，若小屋有“窗外雨景只能透过窗洞可见”这类需求，可以借这个 mask 思路；但不要误用它来把人物按一块平面裁掉。常规沙发、桌子、床的遮挡，前景 alpha 图层更简单、也更接近源码的家具逻辑。

### 3. bobba_client：每个家具资源 layer 都单独排序

`bobba_client` 的 [`RoomItem.updateTextures`](https://github.com/Josedn/bobba_client/blob/8e4e5524f0ece22f1a2725f8bb709d63642a4f2f/src/bobba/rooms/items/RoomItem.ts#L159-L225) 遍历 `getLayers(rotation, state, frame)`；每层读取独立 texture、像素偏移、反转、blend mode、alpha 和 tint，并将其容器的 `zIndex` 设为 `calculateZIndex(layer.z, layerIndex)`。因此同一张家具可以让低层在角色后，而让桌沿/扶手那层在角色前。

它的房间根容器启用 `sortableChildren`（[`RoomEngine.ts`](https://github.com/Josedn/bobba_client/blob/8e4e5524f0ece22f1a2725f8bb709d63642a4f2f/src/bobba/rooms/RoomEngine.ts#L50-L60)）。共同的深度函数为 `((x + y) * COMPARABLE_X_Y + z * COMPARABLE_Z) + priority`，角色使用整数格坐标及 `z + 0.001`（[`RoomEngine.ts`](https://github.com/Josedn/bobba_client/blob/8e4e5524f0ece22f1a2725f8bb709d63642a4f2f/src/bobba/rooms/RoomEngine.ts#L647-L682)）。角色每次更新位置都重新计算容器 `zIndex`（[`RoomUser.ts`](https://github.com/Josedn/bobba_client/blob/8e4e5524f0ece22f1a2725f8bb709d63642a4f2f/src/bobba/rooms/users/RoomUser.ts#L283-L295)）。

## 应迁移到本项目的最小模型

```ts
type RenderPart = {
  id: string;
  image: HTMLImageElement; // PNG 自带透明轮廓
  anchor: { x: number; y: number };
  tile: { x: number; y: number; z?: number };
  depthBias: number;       // 同一家具的 back < actor < frontOccluder
  order: number;           // 固定 tie-break，防止同深度闪烁
};
```

每帧：

1. 绘制 ground/墙等固定背景；
2. 构造 `furniture.back`、`actor.shadow`、`actor`、`furniture.frontOccluder` 的 render list；
3. 统一计算 `depth = (tile.x + tile.y) * 10000 + tile.z * 100 + depthBias`，以 `order` 作稳定次排序；
4. 按排序结果直接 `drawImage`。PNG 的 alpha 会保留家具斜线、扶手弧度、镂空等真实轮廓。

示例：茶几的 `back`（桌腿/后沿）可以比人物早画，`frontOccluder` 只含近侧桌沿与前桌腿、比人物晚画。人物从后面走向前面时，只靠其脚底深度变化完成切换；没有“进入茶几矩形就裁掉下半身”的分支。

## 美术交付要求

对当前小屋的每一个可遮挡家具，交付应至少包括：

| 家具 | `back` 图层 | `frontOccluder` 图层 |
| --- | --- | --- |
| 沙发 | 靠背后部、坐垫后部 | 近侧扶手、靠背/坐垫前缘、前脚 |
| 书桌 | 桌面后半、显示器/后侧物件 | 近侧桌沿、桌腿、椅背（如会遮人） |
| 床 | 枕头与远侧床面 | 近侧床沿、被子前缘、床脚 |
| 茶几 | 后桌腿、远侧桌面 | 近侧桌沿、前桌腿、桌上物（按需要） |

切图验收标准不是“人物露出多少像素”，而是：角色后方站位的可见边界沿 `frontOccluder` 的像素轮廓变化；角色走到近侧后，不再被该前景层盖住；连续移动中没有突然的水平切换线。

## 许可边界

Shroom 的仓库为 LGPL-3.0，bobba_client 为 GPL-3.0（以各仓库根目录许可证为准）。本项目应只独立实现上述通用的分层、深度排序与 alpha 资产规范，不复制其源文件、资源格式解析器或 Habbo 资产。

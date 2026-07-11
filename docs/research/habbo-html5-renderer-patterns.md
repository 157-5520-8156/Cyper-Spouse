# Habbo HTML5 renderer：可复用的 2.5D 渲染模式

调研日期：2026-07-11。这里记录的是公开源码的实现方式，不是对 Habbo 美术或资源的授权判断。

## 直接可参考的开源实现

[Shroom](https://github.com/jankuss/shroom) 是最接近本需求的模块化 TypeScript/PixiJS 房间渲染器；它把 room-space、投影、物件层与 avatar 动画明确分开。主仓库提供 [LGPL-3.0 许可证](https://github.com/jankuss/shroom/blob/b34542491bdc571ad482d37b6a98211bbe488b88/COPYING.LESSER)；即便如此，先只借鉴架构，避免直接搬依赖链和 Habbo 资产。

[bobba_client](https://github.com/Josedn/bobba_client) 是一个更完整的 TypeScript、PixiJS、React Habbo Hotel r60+ 复刻客户端；README 明确列出走路、坐下、挥手、家具状态与家具动画，并采用 GPL-3.0。[源码与许可证](https://github.com/Josedn/bobba_client/tree/8e4e5524f0ece22f1a2725f8bb709d63642a4f2f) 都是公开的，但 GPL 不适合把整套代码直接塞进一个不愿以 GPL 方式发布的项目。本项目应借鉴下述通用算法，而非复制客户端或其资产。

## 源码中真正解决问题的部分

| 问题 | 源码做法 | 对小屋的含义 |
| --- | --- | --- |
| 斜向坐标 | Shroom 的 [`getPosition`](https://github.com/jankuss/shroom/blob/b34542491bdc571ad482d37b6a98211bbe488b88/src/objects/room/util/getPosition.ts#L1-L18) 把逻辑 `(x,y,z)` 投影为 `((x-y)*32, (x+y)*16-z*32)`；Bobba 的 [`tileToLocal`](https://github.com/Josedn/bobba_client/blob/8e4e5524f0ece22f1a2725f8bb709d63642a4f2f/src/bobba/rooms/RoomEngine.ts#L417-L419) 是同一公式。 | 人和家具都先有逻辑格坐标，再投影；不能再用背景图上的独立像素锚点直接移动。 |
| 正确遮挡 | Shroom 将 `behindWall / wall / tile / landscape / primary / masks` 设为独立容器，并使 tile、primary 可排序：[`RoomModelVisualization`](https://github.com/jankuss/shroom/blob/b34542491bdc571ad482d37b6a98211bbe488b88/src/objects/room/RoomModelVisualization.ts#L100-L115)。其 [`getZOrder`](https://github.com/jankuss/shroom/blob/b34542491bdc571ad482d37b6a98211bbe488b88/src/util/getZOrder.ts#L1-L3) 和 Bobba 的 [`calculateZIndex`](https://github.com/Josedn/bobba_client/blob/8e4e5524f0ece22f1a2725f8bb709d63642a4f2f/src/bobba/rooms/RoomEngine.ts#L647-L682) 都给每个对象独立深度键。 | 不应只重绘“沙发、书桌”两个裁切矩形。每帧将“角色 + 每件家具的前/后层”排序；角色跨过物件时自然改变前后关系。 |
| 一件家具内部的遮挡 | [`RoomItem.updateTextures`](https://github.com/Josedn/bobba_client/blob/8e4e5524f0ece22f1a2725f8bb709d63642a4f2f/src/bobba/rooms/items/RoomItem.ts#L159-L220) 逐层取家具资源，并让每层 `layer.z` 参与排序。 | 家具资产要拆成至少 `base/back/front` 三层，沙发靠背、床沿、桌沿属于 `front`，不再从一整张 AI 背景中临时裁图。 |
| 房间/可走区域 | [`RoomModel`](https://github.com/Josedn/bobba_client/blob/8e4e5524f0ece22f1a2725f8bb709d63642a4f2f/src/bobba/rooms/RoomModel.ts#L1-L18) 保存长宽、门和二维 `heightMap`，`isValidTile` 判断可走格；[`setFloor`](https://github.com/Josedn/bobba_client/blob/8e4e5524f0ece22f1a2725f8bb709d63642a4f2f/src/bobba/rooms/RoomEngine.ts#L372-L415) 逐格生成地面/台阶。 | 为小屋建立显式 `walkable` 网格、物件 footprint 与交互站位；路径只能落在格上，路线才不会穿床、倒走或停在家具中央。 |
| 角色行走 | Shroom 的 [`Avatar.walk`](https://github.com/jankuss/shroom/blob/b34542491bdc571ad482d37b6a98211bbe488b88/src/objects/avatar/Avatar.ts#L333-L362) 将目标、方向、动作一起交给动画对象；[`ObjectAnimation`](https://github.com/jankuss/shroom/blob/b34542491bdc571ad482d37b6a98211bbe488b88/src/objects/animation/ObjectAnimation.ts#L44-L115) 以 `performance.now()` 的时间比例插值。角色 [`_updatePosition`](https://github.com/jankuss/shroom/blob/b34542491bdc571ad482d37b6a98211bbe488b88/src/objects/avatar/Avatar.ts#L636-L671) 每一帧都重新投影、计算 z-index，进入门格时还实际移到 behind-wall 层。 | 本面板也应保留 `position / target / facing / action` 四个独立字段；用 delta-time 移动，而非按渲染帧或简单“当前位置向锚点挪”。 |
| 方向与动作帧 | [`AvatarContainer.initialize`](https://github.com/Josedn/bobba_client/blob/8e4e5524f0ece22f1a2725f8bb709d63642a4f2f/src/bobba/rooms/users/AvatarContainer.ts#L31-L62) 预载 8 个方向、四帧行走和其他动作；[`RoomUser.updateTexture`](https://github.com/Josedn/bobba_client/blob/8e4e5524f0ece22f1a2725f8bb709d63642a4f2f/src/bobba/rooms/users/RoomUser.ts#L219-L268) 根据 `wlk/sit/wav` 选择确切帧。 | 现在的 Q 角色 sheet 若没有每方向的行走帧，不能靠翻转/上下跳伪造。下一步需要生成或采购一个统一画风、8 方向、每方向至少 4 帧的角色序列，并依移动向量离散选择方向。 |

## 建议落地：先重构渲染数据，不继续修补背景图

当前问题的根源不是速度常数，而是“一张不可分层的 AI 房间图 + 角色贴图 + 少数前景裁切”。这种表示没有家具 footprint、可走区或每物件的前景层，因此不可能稳定得到 Habbo 式遮挡。

建议的最小场景格式如下；它足够支撑一间小屋，也能扩到多场景：

```ts
type Scene = {
  tile: { width: 64; height: 32 };
  walkable: boolean[][];
  objects: Array<{
    id: string;
    tile: { x: number; y: number; z?: number };
    footprint: Array<[number, number]>;
    interactAt?: { x: number; y: number; facing: Direction };
    layers: Array<{ asset: string; depthBias: number }>;
  }>;
};
```

渲染顺序应是：地板 -> 对所有 `object.layer`、角色阴影、角色、交互道具计算 `depthKey` -> 稳定排序 -> 依序绘制。建议在 Canvas 中使用：

```ts
depthKey = (tileX + tileY) * 10_000 + tileZ * 100 + depthBias;
```

`front` 家具层只需给一个恰好位于同格角色之后的 `depthBias`；有多个遮挡物时无需写专门规则。为避免同深度抖动，再以固定的 `objectId/layerIndex` 为最后 tie-breaker。

角色移动则应：

1. A*（或单房间的预设路径）从当前格到 `interactAt`，不把终点设为家具中心；
2. 每段只沿一个格边移动，`facing` 从该段的 `dx/dy` 得出，角色朝向永远由**运动向量**决定；
3. 累积实际走过的格距离，按距离而非渲染帧推进 `walkFrame`；到站后切换对应 `idle/tidy/eat`；
4. 每个动画帧重新计算角色深度，因此穿过沙发、床和桌子会自动前后切换。

这可以直接修复“倒退走路”和遮挡不正确；仅调慢行走或继续添加局部背景裁切都不能解决数据模型缺失。

## 资产边界

参考仓库 README 说明其客户端不附带 Habbo 头像与家具资源，需自行提供资产：[README](https://github.com/Josedn/bobba_client/blob/8e4e5524f0ece22f1a2725f8bb709d63642a4f2f/README.md#L5-L22)。因此可复用的是房间模型、投影、深度键、动作状态机这些算法；本项目仍需使用自有/可授权的 Q 版角色、地砖和可分层家具资产。

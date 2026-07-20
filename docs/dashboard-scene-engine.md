# 2.5D 像素场景引擎

## 目标与边界

小屋是 daemon 生活状态的**只读视觉投影**，不是世界事实的写入口。它采用
2.5D 等距投影和像素美术，用可校验的地图数据解决走位、遮挡和交互。

参考只用于交互语言：斜向等距房间、自动走位、家具前后遮挡和简洁状态提示。
不复制 TinyHouse、Habbo 或其他第三方项目的代码、素材或角色设计。

> TileRoom v1 是新场景的权威实现：固定 `128×64` tile，投影轴为 x `(64,32)`、y
> `(-64,32)`、z `(0,-64)`。本文中早期 layered-room 的 `85×42` 资产包和
> `assets/dashboard/rooms/` 路径仅适用于 legacy 回退场景；新场景必须放在
> `assets/dashboard/tile-rooms/`，不应混用两套契约。

## 已落地的模块

| Module | Interface | 隐藏的实现 |
| --- | --- | --- |
| Room Compiler | `compile_room(manifest_path, output_dir)` | 清单校验、分层图编译、碰撞/路线/交互可达性校验 |
| Room Runtime | `load`、`setActor`、`activatePreview`、`start` | 等距投影、A*、共享深度排序、动作姿态、效果动画、调试预览 |
| Scene Registry | `scene-registry.json` | 可选场景与默认场景；dashboard 不再拥有某个房间的 bundle 路径 |

## 美术生产：规格先行

新场景不用 AI 生成“完整等距房间”。这会把透视、比例、光照和家具位置随机地锁死在
一张图里，之后无法稳定复用。

`assets/dashboard/asset-kits/orthographic-isometric-v1.json` 是所有新素材共用的规格：

- 正交相机：方位角 45°、仰角 30°；一个逻辑 tile 投影为 x 轴 `(85, 42)`、y 轴
  `(-85, 42)`、z 轴 `(0, -84)`，没有消失点。
- 家具必须以 tile 为单位声明 `width`、`depth`、`height`。它描述视觉包络；
  `occupancy` 仍然只描述不可走的碰撞区域，两者不可混用。
- AI 首先生成平视、无透视、无阴影、可四边平铺的 floor/wall 材质；引擎再把它投影到
  斜向 tile。家具按尺寸拆成 `shadow/back/body/front/light`，而不是让 AI 画完整房间。
- Compiler 会拒绝没有正交投影、投影轴和 tile 尺寸不一致、缺少 furniture 尺寸的场景。

资产包内已经包含地板和墙面的可直接复用提示词。生成时不要上传角色参考图给材质
生成；对需要透明的家具部件，先生成纯色 chroma-key 背景，再本地去背和验收。

场景的权威输入是 `assets/dashboard/rooms/<scene-id>/room.json`。编译后由
`runtime/room.bundle.json` 供浏览器读取；不要手改 runtime 目录。

## 渲染顺序

```text
房间母版 / back / body
        ↓
角色与所有 furniture front layer 按统一 depth key 排序
        ↓
light layer / 交互效果 / 状态气泡
```

角色行走结束才切入家具的 `posePosition`；寻路的终点始终是可走的
`approach`，因此不会为了坐下或睡下而穿过家具。`above-front` 动作可显式
把坐姿置于沙发、餐桌或床的前景层之上。

## 增加一个场景

1. 复制 `zhizhi-home/room.json` 为新场景，替换母版、网格、walkable、objects、
   anchors、interactions 和 sprites；不要复制编译产物。
2. 给每件占地家具写 `occupancy`；给可交互家具写一个可达的 `approach` 和需要的
   `posePosition`、`pose`、`facing`、`depth`。
3. 为会遮挡角色的家具提供 `front` layer 与 `behind/front` 审计站位。对于 AI
   素材，优先生成透明轮廓，再由母版取得最终 RGB，避免风格漂移。
4. 运行 `uv run python scripts/build_dashboard_room.py`（新场景增加后可复用该编译器
   入口，或传入新清单和 output 目录）。通过结构测试、路径测试和浏览器审计后，才
   把它加入 `assets/dashboard/rooms/scene-registry.json`。
5. registry 中的 `id` 必须唯一，`bundle` 必须指向该场景的编译产物。dashboard 会
   自动显示场景选择器，`?room=<id>` 可直接打开该场景。

## 素材与动作策略

- 角色：`zhizhi-iso-walk-v4.png` 提供四个 45° 斜向行走方向，也提供同方向的 idle
  首帧；`zhizhi-sprite-sheet-v2.png` 只用于当前的坐姿和睡姿互动。后续新增互动姿态时，
  必须按同一 45° 朝向生成，不能以平视正面图替代。
- 家具：逻辑对象、碰撞 footprint、前景遮挡层、互动锚点互相独立；新增家具不应要求
  在 dashboard 中写位置特判。
- 特殊动画：动作定义可启用手机打字抖动、睡眠浮动标记，以及聚焦、蒸汽、整理闪光、
  出门光和社交闪点等局部效果。之后新增 sprite 动画时只需在场景 manifest 声明 pose
  或 action definition。

## 验收

- `uv run pytest tests/test_room_compiler.py tests/test_dashboard.py`
- `node --test tests/js/room_runtime.test.js`
- `uv run python scripts/build_dashboard_room.py`
- 浏览器检查 `?demo=tour`、每个对象的 `?demo=audit&object=<id>&side=behind|front`，
  以及交互 `?demo=activity&spot=<interaction>`。

视觉基线只能防回归；新素材仍需人工确认比例、透视、遮挡和色调。

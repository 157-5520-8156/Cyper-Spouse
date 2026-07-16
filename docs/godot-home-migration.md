# 小屋迁移：Canvas TileRoom → Godot 4

## 决定

Canvas TileRoom 的实验性 2.5D 渲染不再作为小屋的目标实现。它保留在仓库中，供历史
比较和 daemon scene 契约回归使用，但 dashboard 默认恢复到 legacy 视觉回退；新的小屋
实现位于 `godot/`。

## Seam

daemon 的 `/world-v2/room` 是唯一默认跨运行时的 Interface：

```text
{
  schema_version: "world-v2-dashboard-room.1",
  cursor: { world_revision, ledger_sequence },
  projection_hash,
  route: { scene_id, action_id, availability }
}
```

Godot 只能读取它，不能写入 daemon 或世界事实。它把已声明的公开 route 映射为本地
房间锚点和动作；未知/不可用 route 保持上一帧，不能降级读取旧
`/debug/<user>/context`。`GodotRoom` 把 manifest 读取、投影、导航、遮挡、角色动作和
光照隐藏在一个 Module 中，对主场景只暴露：

```gdscript
load_manifest(path)
set_actor_state(scene_state)
```

## 首次交付顺序

1. 安装并锁定 Godot 4.x，启动 `godot/project.godot` 的空房与 daemon bridge。
2. 以 `zhizhi-room-isometric-v2.png` 为只读布局标尺，用 manifest 声明墙、门窗、家具
   transform、独立 footprint 与 A* 导航数据；参考图本身不进入运行时。
3. 用 `Node3D + Camera3D(Orthogonal)` 和模块化 mesh 重建家具。角色使用开启 alpha
   opaque prepass 的 `Sprite3D`，让 GPU 深度缓冲处理家具与角色遮挡，不迁移 Canvas
   的人工排序算法。
4. 制作统一脚底/座位/床面锚点的四方向 45° 角色动作，并接入 study/eat/relax/phone/
   sleep/wash/tidy。
5. Godot 场景稳定后，删除 dashboard 对 Canvas room runtime 的默认加载路径。

`/world-v2/room` 不会因为读取而创建 WorldStarted、预算或其他写事件；v2 平台宿主尚未
初始化时返回 503，由 Godot 保持已渲染的状态。内部 dashboard/operator 仍可使用
受凭证保护的 `/internal/world-v2/dashboard-room`，但它与 Godot 的只读 public seam 相互独立。

## 固定渲染规范

- 相机方位角 45°、俯角 30°、正交投影；逻辑地格为 `1×1` 世界单位。
- 逻辑高度按 `0.816496` 换算为 Godot Y 轴高度。
- 内部分辨率为 `696×543`，窗口以最近邻整数倍放大至 `1392×1086`。
- `footprint` 是唯一寻路阻挡来源；地毯、桌面摆件和墙饰标记为 `occupancy: decor`。
- AI 资产只允许平铺材质和局部贴图，例如窗外城市；禁止生成整屋背景。

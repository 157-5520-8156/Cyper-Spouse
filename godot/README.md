# Godot 小屋（迁移中）

这是新的小屋运行时；Canvas TileRoom 不再是继续迭代的目标。Godot 工程通过 daemon 的
只读 `/debug/<user>/context` 投影获得 `location/action/expression`，不会写入世界或 daemon。

## 运行

需要 Godot 4.x。导入此目录中的 `project.godot`，运行主场景；按 `F5` 请求一次 daemon
状态。工程内部以 `696×543` 渲染，再用 nearest-neighbor 放大到 `1392×1086`。开发时先启动 daemon：

```sh
.venv/bin/uvicorn companion_daemon.app:app --port 8767
```

## Module / Interface

`GodotRoom` 是深 Module：外部 Interface 只有 `load_manifest(path)` 与
`set_actor_state(scene_state)`；等距投影、导航、GPU 深度遮挡、角色动作和光照都由其
Implementation 隐藏。浏览器 dashboard 与 Godot 都只读取相同的 daemon 场景契约。

当前工程使用 `Node3D + Camera3D(Orthogonal)`、模块化 mesh 家具、A* 格导航与
`Sprite3D` 角色。家具的 `footprint` 是唯一寻路阻挡来源，墙体阻止跨格边界；地毯、桌面
摆件和壁挂装饰不会占用地格。角色透明像素使用 alpha-scissor 写入深度，不迁移 Canvas
的对象排序或家具遮挡特判。

`assets/dashboard/zhizhi-room-isometric-v2.png` 是布局、比例、色彩和密度的视觉验收标尺，
不会作为运行时整屋背景。AI 资产只用于平铺材质或局部 overlay。

## 视觉基线

运行 `scripts/test_godot_visuals.sh` 会依次截取完整房间和七类互动，输出到
`.artifacts/godot-visual-baselines/`。每个状态同时生成一张参考图/实际画面的并排图；这些
图片用于布局、角色比例和遮挡人工验收，不作为逐像素快照测试。

角色动作由 `scenes/zhizhi-actions.json` 声明 texture region、统一尺度和脚底/座位/床面
pivot。增加动作时必须先通过 manifest 校验，不能在 `actor_avatar.gd` 中按裁切框临时猜测
人物大小。

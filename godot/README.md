# Godot 小屋（迁移中）

默认运行时现在是星露谷式俯视 2D 小屋。Godot 工程通过 daemon 的只读
`/debug/<user>/context` 投影获得 `location/action/expression`，不会写入世界或 daemon。
旧的 3D 等距场景保留在 `scenes/main.tscn`，仅作迁移前的视觉回退，不再是默认入口。

## 运行

需要 Godot 4.7。导入此目录中的 `project.godot`，运行主场景；它每三秒读取一次 daemon
状态。工程内部以 `696×543` 渲染，再用 nearest-neighbor 放大到 `1392×1086`。开发时先启动 daemon：

```sh
.venv/bin/uvicorn companion_daemon.app:app --port 8767
```

## 俯视 Module / Interface

`topdown/` 是独立 Module：`TopdownRoomManifest` 负责加载/校验场景 JSON，
`TopdownNavigation` 以家具 footprint 配置 `AStarGrid2D`，`TopdownActor` 将 daemon
状态变成路径、朝向和通用活动标记。浏览器 dashboard 与 Godot 都只读取相同的 daemon 场景契约。

家具的格 footprint 是唯一寻路阻挡来源；地毯和装饰不占用地格。角色和家具以脚底的屏幕
`y` 值排序，因此角色经过家具前后不需要等距场景的人工遮挡特判。每个交互在 JSON 中声明
一个可达 `approach` 格，状态可以映射为 study、eat、relax、phone、sleep、wash 或 tidy。

`topdown/ATTRIBUTION.md` 记录并保留了开放素材的来源和许可证。房屋不会以整张背景图运行，
家具、房间、可走格和交互点均来自 `topdown/scenes/zhizhi-home-topdown.json`。

当前版本首先验证格子、寻路、脚底排序和 daemon 驱动；家具仍是代码绘制的比例占位物，
不是最终美术。下一轮会在不改变 manifest/导航接口的前提下，将其替换为选定开放室内素材包。
若需打开遗留的 3D 对照场景，可在 Godot 编辑器中手动运行 `scenes/main.tscn`。

## 验证

```sh
/Applications/Godot.app/Contents/MacOS/Godot --headless --path godot --script res://tests/test_topdown_runner.gd
/Applications/Godot.app/Contents/MacOS/Godot --headless --path godot --script res://tests/test_topdown_runtime_runner.gd
```

前者检查 manifest、家具碰撞、可达性和状态映射；后者检查真实场景实例化、书桌状态和互动高亮。

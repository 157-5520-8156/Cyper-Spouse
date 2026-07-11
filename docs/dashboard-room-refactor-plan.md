# Dashboard 小屋可扩展重构计划

最后更新：2026-07-12

## 执行状态

- [x] 阶段 0：已用 `freeze=1` 保存 tour 起点与六件家具 behind 的确定性页面基线，并完成五类动作和连续 tour 的迁移前后浏览器视觉对照。
- [x] 阶段 1：`room.json` 已成为图片、网格、家具、交互、审计站位与路线的唯一事实来源；dashboard 从编译 bundle 加载。
- [x] 阶段 2：Room Compiler 已生成 bundle 与六张坐标锁定遮挡层，校验来源、网格、alpha、路线、连通性、动作引用、唯一 id、唯一输出和 footprint 冲突，并以临时目录整体替换 runtime。
- [x] 阶段 3：Room Runtime 已提取，页面只调用 `load / setActor / activatePreview / start`。
- [x] 阶段 4：`?demo=room-editor` 已支持独立显示/隐藏网格、walkable、footprint、approach 与 depth，支持三种显示模式、前后站位、动作选择、拖动 origin 与导出片段。
- [x] 阶段 5：AI 素材模板已固化；Compiler/Runtime 已通过临时分层素材测试打通独立 `back/front` 模式。

## 结论

方案可行，但不能把“AI 生成图片”当作房间结构本身。AI 适合生成候选素材、透明轮廓和姿态草图；房间的坐标、可走区、家具 footprint、交互点与深度关系必须由一份可校验的场景描述决定。

当前方案已经验证了正确方向：以视觉母版提供最终颜色，以 AI 图只提供 alpha 轮廓，再通过共享深度列表绘制角色与家具前景。重构应保留这个原则，并把现在散落在页面、构建脚本和测试中的人工坐标集中起来。

## 目标

- 新增家具或动作时，只修改一份房间清单并放入素材，不再在三处同步坐标。
- 构建阶段自动发现越界、错位、不可达路径、错误遮挡素材和缺失动作。
- 运行时只读取编译完成的房间包，不理解 AI 素材或构建细节。
- 不会美工的人也能通过网格、锚点、透明层和前后站位预览判断结果。
- AI 生成失败或风格不一致时，可以换图重建，不影响房间结构与运行时代码。

## 不做的事

- 不承诺任意 AI 图片都能自动变成正确的等距家具。
- 不从一张扁平母版自动推断完整三维结构；这种推断无法稳定验证。
- 不在第一轮引入远程 AI 接口。现阶段只有“读取本地候选素材”这一种实现，没有必要制造假的 Adapter seam。
- 不先重写整个 dashboard；房间模块应能逐步替换现有实现。

## 核心模型：一份房间清单

建议新增 `assets/dashboard/rooms/zhizhi-home/room.json`，作为唯一事实来源。清单至少包含：

```json
{
  "id": "zhizhi-home",
  "canvas": {"width": 1280, "height": 900},
  "projection": {
    "origin": [640, 236],
    "tileX": [64, 32],
    "tileY": [-64, 32]
  },
  "master": "master.png",
  "walkable": [[0, 1], [0, 2]],
  "objects": [
    {
      "id": "bed",
      "footprint": [[6, 1]],
      "depthTile": [6, 1, 0],
      "frontOccluder": {
        "matte": "sources/bed-front.png",
        "origin": [885, 445]
      },
      "interactions": {
        "sleep": {
          "approach": [7, 1, 0],
          "posePosition": [5, 0.65, 0],
          "pose": "sleep",
          "facing": "upLeft",
          "depth": "above-front"
        }
      }
    }
  ],
  "routes": {"tour": [[0, 1, 0], [0, 2, 0]]}
}
```

具体字段可以调整，但以下不变量必须固定：

- `master` 决定房间最终颜色；AI matte 不直接贡献 RGB。
- `footprint` 只描述占地；`approach` 描述角色能走到的位置，两者不能混用。
- `posePosition` 只控制动作投影；寻路仍以 `approach` 为终点。
- 每件可遮挡家具必须提供 `behind/front` 两个审计站位，或明确声明不需要遮挡审计。
- 深度使用语义值或统一公式，不允许在页面逻辑中继续出现家具专属的 `+100` 特判。

## 模块与 seam

### 1. Room Compiler（Python，深模块）

外部 Interface 只保留一个主要入口：

```python
compile_room(manifest_path, output_dir) -> CompileReport
```

Implementation 负责：

- 解析和校验房间清单。
- 从母版与 matte 构建坐标锁定的 `frontOccluder`。
- 生成运行时 `room.bundle.json` 与派生图片。
- 计算可走图、连通性、路线合法性和审计用例。
- 返回错误、警告与所有产物摘要。

这会替代 `build_dashboard_occluders.py` 内的硬编码 `OCCLUDERS`。删除这个模块后，复杂度会重新散回页面、脚本和测试，因此它有足够的 Depth。

### 2. Room Runtime（浏览器，深模块）

外部 Interface 控制在：

```js
const room = await loadRoom('/assets/.../room.bundle.json')
room.setActor(sceneState)
room.tick(deltaMs)
room.draw(ctx)
room.activatePreview(previewSpec)
```

Implementation 隐藏投影、寻路、共享深度排序、动作姿态、巡回折返与遮挡绘制。dashboard 页面只负责 daemon 状态、控制面板和 canvas 生命周期，不再知道床或沙发的坐标。

### 3. Asset Intake（先作为编译器内部 seam）

输入是本地 PNG matte 或已经分层的家具素材。当前只有这一种真实输入方式，因此先不公开 Adapter 接口。以后若同时支持：

- 本地人工/AI 透明图；
- 自动抠图或远程生成结果；

再把它们实现为两个 Adapter，共同输出规范化的 alpha matte。

## 推荐目录

```text
assets/dashboard/rooms/zhizhi-home/
  room.json                 # 唯一事实来源
  master.png                # 视觉母版
  sources/                  # AI 或人工提供的候选素材，不直接上线
  runtime/                  # 编译产物，不手工修改
    room.bundle.json
    occluders/
src/companion_daemon/static/room/
  runtime.js                # Room Runtime
  renderer.js               # 投影与深度绘制的内部实现
  preview.js                # 审计/动作预览的内部实现
scripts/
  build_dashboard_room.py   # Room Compiler 命令入口
```

如果项目暂时不希望增加静态 JS 文件，也可以先把 Runtime 保留在 Python 模板中；但房间数据必须先移出 `dashboard_ui.py`。

## 素材接入流程

以后增加新素材时统一走以下流程：

1. **确定结构**：先在 `room.json` 中定义家具 id、footprint、approach、动作和审计站位。
2. **生成候选图**：给 AI 提供母版裁切、等距方向、光源、像素密度、目标包围框和透明背景要求。
3. **规范化**：保留 alpha matte；若家具本身就是独立分层素材，则显式提供 `back/front`，不要再从母版取色。
4. **编译**：Compiler 生成运行时素材和报告；任一硬错误都阻止产物更新。
5. **自动验证**：运行结构、像素和路径测试。
6. **视觉验收**：自动打开该家具的 `behind/front`、所有动作及一条经过它的路线；人工只需判断画面，不需要猜代码坐标。
7. **更新基线**：确认画面后才更新截图基线和工作记录。

## 构建期必须自动检查

- 所有素材存在、尺寸有效且位于母版范围内。
- 母版取色型 occluder 的 RGB 与母版对应区域逐像素一致。
- matte 存在有效透明区与有效遮挡区，不能整张全透明或全不透明。
- footprint、approach、anchor 和 route 坐标都在网格范围内。
- approach 不落在 footprint 中，所有交互点从入口可达。
- 巡回路线相邻节点连续，不穿家具、不跳格。
- 每个动作引用的 pose、facing 和 sprite crop 均存在。
- 每件 occluder 都生成 `behind/front` 审计用例。
- 相同 id、输出路径或占地冲突直接报错。

## 视觉工具

新增仅用于开发的 `?demo=room-editor`，不做完整美术软件，只提供高杠杆的校准能力：

- 显示/隐藏等距网格、walkable、footprint、approach 与 depth key。
- 选择家具后拖动 occluder origin，并实时显示整数坐标。
- 一键切换角色到 `behind/front` 和各动作位置。
- 单独显示 alpha、母版 RGB 或最终合成结果。
- 导出该对象对应的清单片段，避免手抄坐标。

这仍然要求人做视觉判断，但把判断变成“对不对”，而不是“该改哪一段代码”。

## 测试策略

测试以两个深模块的 Interface 为表面，不再以搜索 HTML 字符串作为主要保障。

### Room Compiler

- 给定最小合法 fixture，断言编译报告、bundle 和图片产物。
- 给定越界 matte、不可达 interaction、断裂 route，断言明确错误。
- 保留母版 RGB 与 matte alpha 的像素不变量测试。

### Room Runtime

- 使用固定 bundle 测试寻路、动作状态和深度排序结果。
- 对 `behind/front` 与动作预览截取 canvas，不截整个 dashboard。
- 对像素图使用精确基线；若浏览器渲染导致平台差异，只对 canvas 使用小范围容差。
- 现有 HTML 字符串断言逐步删除，只保留页面契约的少量冒烟测试。

## 分阶段迁移

### 阶段 0：锁定基线（小）

- 保存当前六件家具的 canvas 审计图与 tour 基线。
- 记录当前 bundle 等价数据，后续迁移必须保持画面不变。

验收：没有功能改动，现有 517 项测试继续通过。

### 阶段 1：提取单一房间清单（中）

- 建立 `room.json`。
- 将 image paths、walkable、objects、anchors、routes、activity previews 迁入清单。
- 页面先直接读取清单，渲染实现暂不改。

验收：删除页面中的家具专属坐标；视觉基线不变。

### 阶段 2：建立 Room Compiler（中）

- 将 occluder 构建配置迁入清单。
- 输出 bundle、派生图片和结构化报告。
- 增加上述构建期校验和 fixture 测试。

验收：新增家具不需要修改 Python 源码或测试参数表。

### 阶段 3：提取 Room Runtime（大）

- 从 `dashboard_ui.py` 移出投影、寻路、深度排序、动作与预览逻辑。
- 去除床和沙发的深度特判，改成清单里的语义规则。
- dashboard 只把 daemon scene state 传给 Runtime。

验收：Runtime 可用独立测试页加载；dashboard 仍保持相同行为。

### 阶段 4：加入房间校准工具（中）

- 实现网格、遮挡层、站位和动作切换。
- 支持拖动 origin 并导出清单片段。

验收：新家具的坐标校准无需编辑渲染源码。

### 阶段 5：固化 AI 素材模板（小）

- 保存可复用提示词模板和输入清单：母版裁切、风格参考、方向、尺寸、透明背景。
- 明确“母版取色 matte”与“独立 back/front 素材”两种接入模式。
- 用一件新增小家具完整走通生成、编译、审计和上线流程。

验收：换一张 AI 候选图只需替换 source、重新编译和视觉确认。

## 每阶段停止条件

每个阶段都必须满足：

- `git diff --check` 与静态检查通过。
- Compiler/Runtime 对应测试通过，完整 pytest 无新增失败。
- canvas 视觉基线没有未经确认的变化。
- 浏览器控制台无素材加载和绘制错误。
- worklog 记录新增素材来源、清单变化和视觉验收范围。

## 风险与取舍

- **扁平母版是主要限制**：看不见的家具背面无法可靠恢复。需要复杂交互的新家具，最好从一开始就生成独立 `back/front` 分层。
- **AI alpha 不是事实**：它只能加速制作轮廓，最终仍需前后站位确认。
- **截图测试不能代替审美判断**：截图能阻止意外回归，不能判断首次生成的家具是否好看。
- **校准工具应保持克制**：只解决网格、坐标、alpha 和深度，不扩张成通用地图编辑器。
- **先迁数据再迁渲染**：同时更换数据格式和绘制实现会让视觉回归难以定位。

## 推荐执行顺序

先完成阶段 0–2。这三步会立即消除多处硬编码，并建立以后接入 AI 素材所需的单一流程；等 Compiler 稳定后再提取 Runtime 和制作校准工具。这样每一步都能与当前画面逐像素比较，出现偏差时也容易定位。

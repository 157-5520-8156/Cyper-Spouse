# 等距虚拟伴侣小屋：源码与美术调研结论

更新日期：2026-07-11

## 结论（唯一推荐）

不要寻找并整包嵌入一个“现成虚拟伴侣游戏”：截至本次按官方仓库 README、源码和
LICENSE 的核验，没有一个成熟开源项目同时满足 **漂亮的斜向像素室内美术、人物与物件
交互、可切换多场景、且能直接嵌入本项目 Python daemon 面板**。把不相同的游戏直接
抄进来，最后通常会得到两套状态机、两套服务器和不统一的美术。

建议采用一条可由本项目完成的组合路线：**保留 daemon 为唯一生活事实来源，在当前
Canvas 面板内重做一个等距 scene runtime；移植 Pixel Agents 的角色状态机、网格编辑/
家具 manifest 思路和其 MIT 办公室资产作原型；移植 IsoCity 的 MIT 等距深度排序和
分层渲染思路；正式美术由本项目按统一母版生成和人工切片，而非依赖某个不完整的免费
素材包。** 这既不要求用户寻找或绘制素材，也不会把视觉层变成另一套“生活模拟”。

这里的“移植”是指带署名保留 MIT 文本后，选择性改写所需算法/数据结构；不是把两个
完整应用、它们的 Next.js/Fastify/VS Code 扩展或代理协议一并复制过来。

## 已核验的候选与取舍

| 来源 | 官方证据与许可证 | 已具备的可复用部分 | 不适合直接照搬的原因 | 结论 |
| --- | --- | --- | --- | --- |
| [Pixel Agents](https://github.com/pixel-agents-hq/pixel-agents) | [README](https://github.com/pixel-agents-hq/pixel-agents#how-it-works) 明确 Canvas 游戏循环、BFS 寻路、`idle → walk → type/read` 状态机；[布局编辑器](https://github.com/pixel-agents-hq/pixel-agents#layout-editor) 有家具摆放、持久布局、导入/导出；[LICENSE](https://github.com/pixel-agents-hq/pixel-agents/blob/main/LICENSE) 为 MIT，README 的 [Office Assets](https://github.com/pixel-agents-hq/pixel-agents#office-assets) 声明家具、地板、墙均随仓库提供。仓库约 8.5k stars，近期仍更新（调研时读取 GitHub API）。 | 最接近当前 daemon 的“状态 → 动作 → 气泡/通知 → 寻路”可视化；已有的模块化家具和编辑器格式可作为场景资源规范的起点。 | 它是顶视角办公室、不是斜向 2.5D，人物来源另有 Metro City 致谢；默认是一张可编辑办公室而非叙事多场景。不能把它的美术和角色当作本项目最终风格。 | **机制和原型资产首选。** 当前项目已经采用其部分 MIT 资产，详见根目录 `THIRD_PARTY_NOTICES.md`。 |
| [IsoCity](https://github.com/amilich/isometric-city) | [README](https://github.com/amilich/isometric-city#features) 声明 HTML5 Canvas 等距渲染、深度/图层管理、图片与 Canvas sprite、tile 交互、保存多城市；[LICENSE](https://github.com/amilich/isometric-city/blob/main/LICENSE) 为 MIT。仓库约 2.2k stars，近期仍更新（调研时读取 GitHub API）。 | 斜向 tile 坐标换算、按脚底排序、物件层级、镜头和 tile 点击的实现思路；可证明纯浏览器 Canvas 已足够。 | 城市建造 UI/经济和交通模拟远大于需求，视觉不是温馨人物房间；技术栈是 Next.js/TypeScript，本项目面板是 Python 内嵌的原生 JS。 | **只借等距 renderer 模型，不引入其应用框架或资产。** |
| [Agent Town](https://github.com/geezerrrr/agent-town) | [README](https://github.com/geezerrrr/agent-town#key-features) 记录 Phaser 3/Tiled、角色漫游白板/打印机/沙发、按任务回座和气泡；[LICENSE](https://github.com/geezerrrr/agent-town/blob/main/LICENSE) 为 MIT。README 的 [Assets](https://github.com/geezerrrr/agent-town#assets) 要求使用者自行准备兼容素材。 | “任务映射到交互锚点、先走路再进入动作”的状态转换可作交互契约参考。 | 顶视角；多地点只是 roadmap，且不提供可直接复用的成套美术。 | 不作为依赖，仅参考交互状态的拆分。 |
| [AI Town](https://github.com/a16z-infra/ai-town) | [README](https://github.com/a16z-infra/ai-town) 说明 PixiJS 渲染/交互、角色生活与社交；[LICENSE](https://github.com/a16z-infra/ai-town/blob/main/LICENSE) 为 MIT；其 [credits](https://github.com/a16z-infra/ai-town#credits) 明确素材须逐项追溯。 | 角色/关系可视化与即时交互的产品参考。 | 顶视角，且资产许可不是一个可整体搬运的包；其后端架构与现有 daemon 重叠。 | 不采用。 |
| [FreeSO](https://github.com/riperiperi/FreeSO) | [README](https://github.com/riperiperi/FreeSO) 指向 [MPL-2.0](https://github.com/riperiperi/FreeSO/blob/master/LICENSE.md)，官方 [About](https://freeso.org/about/) 明确项目不分发原版版权游戏文件。 | 房间对象交互、多楼层/室内遮挡、“模拟人物先走到对象再执行动作”的领域模型。 | 3D/C#/MonoGame、工程体量大，并且关键美术不能取得。 | 仅借对象交互语义，不使用源码/资产。 |
| [OpenRCT2](https://github.com/OpenRCT2/OpenRCT2) | [README](https://github.com/OpenRCT2/OpenRCT2#introduction) 和 [GPL-3.0](https://github.com/OpenRCT2/OpenRCT2/blob/develop/licence.txt)；README 也说明运行需要原游戏数据。 | 很成熟的 45° tile 遮挡、对象状态、客人寻路范式。 | C++ 重型、GPL 传染性强、需要原作资产，明显超出小屋需求。 | 不采用。 |

## 目标方案：`scene runtime`，而不是游戏引擎重写

现有 [`visual-home-roadmap.md`](../visual-home-roadmap.md) 已正确规定：daemon 是事实来源，
面板只读投影。该边界不改变。新增前端 scene runtime 后，链路应为：

```text
life runtime / mood / phone state（daemon）
  → scene projection（scene_id、activity、attention、emotion）
  → 场景规则（目标物件、可行走格、入场点、动作、遮挡）
  → BFS/A* 路径
  → walk 到 interaction anchor
  → 动作循环 + 物件状态 + 氛围层（仅渲染）
```

### 第一版范围（一个月内的可交付基线）

1. **3 个完整场景，不做无限地图。** `home_evening`、`campus_library`、`cafe_rainy`；每个
   场景 8–12 个交互物件，至少 6 个真实活动锚点。场景通过 `scene_id` 切换，地图 JSON
   和同一套角色动作完全复用。
2. **12 个高频动作。** 以既有路线图的 `idle/walk/read_phone/type_phone/study_laptop/
   curl_video/eat_simple/wash_face/tidy/sleep_side/pout_idle` 为核心，补 `enter/exit`。
   每个动作是 4–8 帧短循环，不让 LLM 自由命名动作。
3. **可见但不可写回的交互。** 比如 `study_laptop`：人物先走到书桌椅子锚点、椅子进入
   occupied、电脑屏幕亮起；通知到达时手机亮/人物转为 `read_phone`。点击物件只展示
   当前已知活动/最近事件，绝不能凭点击创建生活事件。
4. **正确的等距层级。** 背景地面 → 后景物件 → 人物（按脚底 `screenY`）→ 前景遮挡
   → 天气/灯光/通知。墙、床沿、桌子前沿须拆成前景 PNG，不能再是单张背景。
5. **一个调试编辑模式。** 复用 Pixel Agents 的 manifest/JSON 思路，允许在浏览器查看
   walkable 格、碰撞、锚点、脚底坐标和层级；发布版隐藏。这会使后续场景不必修改 JS。

### 推荐数据边界

```json
{
  "id": "home_evening",
  "tile": {"width": 64, "height": 32},
  "layers": ["ground", "objects_back", "actor", "objects_front", "atmosphere"],
  "walkable": ["tile coordinates"],
  "objects": {
    "desk": {
      "anchor": {"tile": [8, 5], "facing": "northwest"},
      "actions": ["study_laptop", "read_phone", "tidy"],
      "states": ["off", "lit", "occupied"],
      "foreground": false
    }
  },
  "activity_map": {"study": "desk.study_laptop", "sleep": "bed.sleep_side"}
}
```

`scene_id` 与活动类别由 daemon 决定；前端只允许把未知活动回退为当前场景的 `idle`，
不允许猜测人物去了哪里。此项比复制一个完整游戏更符合现有状态机和测试边界。

## 不会做美工时的资产策略

没有可验证的、同时“漂亮、完整、等距室内、多场景、人物动作齐全”的成熟开源包；把
不同作者的免费 PNG 拼在一起会立刻破坏比例、投影角度、调色板和人物一致性。因此正式
资产采用 **一套项目专属生成母版 + 可编辑分层切片**，具体工作仍可由本项目完成：

1. 先锁定 `2:1` 菱形（64×32）、固定相机和 24 色左右调色板，基于
   [`configs/visual_identity.yaml`](../../configs/visual_identity.yaml) 统一人物外观。
2. 每场景生成一张“只作为构图参考”的高质量等距像素母版；据此生成/修整地板、墙体、
   家具后景、家具前景、道具状态和人物 sprite sheet。导入时以 nearest-neighbor 保持
   像素边缘，绝不把一张 AI 整图直接当可走地图。
3. 给每件家具提供 `manifest.json`（sprite、脚底、碰撞、遮挡、交互锚点、状态帧），
   沿用 Pixel Agents 已验证的可扩展资源组织方式。生成内容与第三方 MIT 代码/资产分开
   存放并在 `THIRD_PARTY_NOTICES.md` 保留归属。

Pixel Agents 的现有地板/家具可在第一版调试场景中直接用，避免等待素材；但它们是顶视
角资产，正式斜向场景只能作为尺寸/状态设计参考，不能混用进同一画面。

可选的占位资产有 OpenGameArt 作者声明为 CC0 的 [Isometric Furniture and
Walls](https://opengameart.org/content/isometric-furniture-and-walls)、[Isometric Stone
Soup](https://opengameart.org/content/isometric-stone-soup) 与 [Isometric Painted Game
Assets](https://lpc.opengameart.org/content/isometric-painted-game-assets)。它们可用于验证
tile、遮挡和锚点管线；但整体偏地牢/奇幻，不能同 Pixel Agents 或生成的温馨小屋混拼，
也不含同风格的完整日常动作组，故不作为最终方案的视觉来源。

## 实施顺序与验收

1. 从 Pixel Agents 和 IsoCity 的 MIT 源码中抽取必要的寻路、坐标和分层原则，写成当前
   原生 JS 模块；保留两项目的许可证及具体来源记录。
2. 先完成 `home_evening` 的 tile JSON、碰撞、前景遮挡、6 个锚点和 6 个动作，接通
   现有 `scene` 投影；不改 daemon 的生活事实写入路径。
3. 以同一 schema 加 `campus_library` 与 `cafe_rainy`，验证切换场景无需新增特殊 JS。
4. 建立截图回归：人物不会穿墙/桌/床，走到锚点后才切动作，人物被前景正确遮挡，夜间
   灯光不遮住 UI；并对 `scene projection` 做单元测试，确保视觉从不写回事件账本。

完成时的最低验收是：三场景切换稳定、12 个动作中至少 8 个实际接入、每场景 6 个可见
交互物件、通知和活动状态有一致的可视变化、且新场景只增加资源 JSON/PNG 而非复制
渲染逻辑。这样才能称为“多场景、会与环境互动的虚拟人物”，而不是换了背景图。

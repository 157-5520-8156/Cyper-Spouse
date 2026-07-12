# Dashboard 小屋全资产原子化计划

最后更新：2026-07-12

## 执行状态

- [x] 计划与“完全原子化”验收定义落盘。
- [x] 建立 65 项机器可读 inventory：57 项 `planned`、6 项 `partial`、1 项 `verified`、1 项 `excluded`（屋顶吊灯按观测视野规则排除）。
- [x] Compiler 将 inventory 状态编入 bundle，并拒绝非法状态、重复项及无 inventory 所有者的 room 对象。
- [x] Runtime 增加 `?demo=atomization&object=<id>&mode=<hidden|solo>`；隐藏不改变 occupancy 与路径。
- [x] Room Editor 显示 inventory 进度，并提供 hidden/solo 删除测试。
- [x] 波次 0：通用对象 `layers / occupancy / audits / provenance` schema；`teal-stool` 已作为无互动原生对象接入，旧对象经 Compiler 归一化到同一 bundle 契约。
- [ ] 波次 1：主要遮挡家具（进行中：clean shell 与 16 个家具/附属对象已进入可编译 `artDraft`，仍需逐件校准和删除测试）。
- [x] 波次 2：厨房与大型收纳草稿功能闭环（29 个对象、36 个构建资产；全部厨房对象可删除，父子联动、适用遮挡与餐厨动作/路径通过；素材仍保持 `needs-art`，终稿基线在波次 6 统一批准）。
- [ ] 波次 3：地毯、灯具、植物与窗区（进行中：四块地毯、两株落地植物和书桌灯已进入草稿，现为 36 对象、44 个构建资产；所有屋顶对象排除，另外两盏台灯等待承载家具拆分，其余植物和窗区待处理）。
- [ ] 波次 4–6：decor、路径动作与最终验收。

### 波次 0 验收记录

- `teal-stool` 的 manifest 不再使用 `frontOccluder/footprint`，完整声明类别、占地、通用图层、空 interaction、审计能力和 AI 来源。
- Compiler 允许旧输入过渡，但 runtime bundle 只输出统一对象；Runtime 与 Editor 已不读取旧家具字段，也没有家具 id 特判。
- 统一队列已落地为 `shadow/back/body → actor/front → light/effect → overlay`；动作的 `above-front` 深度从对象 front 角色推导。
- `?demo=atomization&mode=layers&role=<shadow|back|body|front|light>`、Editor 图层选择、角色视图、provenance 查看和 origin 导出已接通；hidden/solo/layers 均不改变 occupancy。
- 浏览器实测 `teal-stool` front-only、沙发 behind 和 room-editor；素材加载与控制台无错误，沙发现有遮挡画面未回退。
- clean shell 首次 AI 候选已生成并对齐到母版尺寸，但由于生成器改变过原始输出尺寸且窗洞/地砖边界仍需几何复核，当前只标记 `needs-art`，未替换生产背景。

### 波次 1 草稿装配记录

- `artDraft` 与正式对象共用 Compiler、runtime bundle、通用绘制队列和 Editor，不维护第二套家具渲染器；`?demo=art-draft` 可在 clean shell 上查看当前装配，`?demo=room-editor&art=draft` 可校准图层。
- 当前草稿对象：`desk`、`office-chair`、`sofa`、两个 sofa cushion、`sofa-throw`、`table`、`coffee-table-setting`、`bed`、`bed-bedding`、`dining`、两把 dining chair、`dining-table-setting`、`divider`、`bed-divider-content-cluster`。
- 所有原始 AI 输出保留为 flat chroma source；裁切、key color、despill、resize、origin 与 depthBias 均由 `room.json` 声明并由 Compiler 可复现构建。
- 草稿 URL 位于独立 `artDraft.images`；默认 dashboard 不下载也不依赖任何 `needs-art` 文件，只有显式草稿入口会先懒加载再切换场景。
- 浏览器已实际装配 16 个对象，未出现资源或控制台错误；目前仍存在隔断内容与柜体的局部校准、clean shell 瓷砖/窗洞几何复核及逐对象 hidden/solo/behind/front 证据缺口，因此不提升 inventory 状态。

### 波次 2 收纳与厨房草稿记录

- `tall-bookcase`、`bookcase-content-cluster`、`kitchen-wall-cabinets`、`kitchen-wall-cabinet-decor`、`kitchen-sink-counter`、`kitchen-stove-counter`、`fridge` 已按同一 `artDraft` 契约接入；草稿现有 23 个对象，没有新增专用渲染分支。
- 新增通用 `attachedTo` 依赖：隐藏父对象会关闭全部后代图层；solo 子对象会自动保留完整祖先链。Compiler 拒绝未知父对象和循环依赖。
- Compiler 对 production 与 `artDraft` 分别执行 topology 校验，不允许草稿家具占住 interaction approach；确有座椅占位的交互必须通过 `allowOccupiedBy` 明确列出对象 id，Runtime 寻路只为该次目标排除此占地。
- 浏览器实际验证高书柜与吊柜两组父子对象：父对象 hidden 后主体和附属内容同时消失；内容 cluster solo 时父体保留；高书柜 behind/front 会按角色深度切换完整柜体与内容层；无破图，整屋其余对象不受影响。
- 双轴检查点审查补出一处依赖遗漏：局部 effect 现在与对象图层复用同一可见性集合，隐藏父对象不会残留子对象光效；高书柜也从永远位于角色后的 `body` 改为共享深度队列中的 `front`，并补齐前后审计点。
- 这七件素材仍为 `planned / needs-art`：书柜/吊柜父子 hidden/solo、高书柜前后深度，以及三件下厨资产的 12 项 hidden/solo/behind/front 浏览器矩阵均已通过；厨房完整组合、逐像素风格校准和最终视觉基线尚未完成，不能据此升级 inventory 状态。
- 首次烤箱候选被明确标为 `rejected`：它错误生成了带炉面的独立灶具，而母版需要柜下嵌入式烤箱；被拒素材保留 provenance，但没有进入 manifest 或 runtime。

### 波次 3 地毯与落地植物草稿记录

- 四块区域地毯与两株落地植物已进入统一对象契约；书柜内容裁掉重复植物像素，草稿现为 35 个对象、42 个构建资产。
- 书桌盆栽不占新增路径格，且其不可达后侧明确为 `false`；大型植物占据 `[7,4]`，地毯锚点与 tour 改从 `[7,5]` 绕行。
- AI 生成意图与 reference 已记录在 inventory。对象仍为 `planned / needs-art`；详细定位、视觉审计和动态 tour 证据见 `docs/dashboard-visual-worklog.md`。

## 用户目标

将当前扁平的 2.5D 像素小屋改造成真正可扩展的分层房间：所有可辨认的家装、家具、灯具、植物与装饰，即使暂时没有互动动作，也必须成为可单独隐藏、替换、重新生成、校准和审计的场景对象。后续新增素材继续以当前视觉母版为风格参考，通过 AI 辅助生成，但房间结构、坐标、深度和路径不由 AI 猜测。

## “完全原子化”的定义

### 可以留在壳层中的内容

只有不可移动、不会单独替换的建筑结构可以留在 clean shell：

- 两面墙及其转角、墙脚和房间外轮廓。
- 木地板与厨房地砖的基础表面。
- 窗洞及窗外城市景色的基础画面。
- 固定门洞、不可拆结构线和基础环境光。

窗帘、窗台盆栽、灯、画、柜体、地毯等都不属于壳层。

### 必须成为对象的内容

满足任一条件就必须独立：

- 能被移动、隐藏、替换或重新生成。
- 能遮挡角色，或角色能站在其前后。
- 未来可能拥有动作、状态或外观变化。
- 属于明显可辨认的家具、家电、灯具、植物、软装或墙面装饰。

“独立”不要求每支笔、每本书都成为一个对象；不可单独操作的一组小物件可以形成一个 `decor-cluster`，但该 cluster 必须与桌子、柜子或墙面资产分离。例如桌面文具可以是一件 `desk-stationery-cluster`，不能继续烙在书桌 RGB 中。

### 明确不进入观察画面的内容

- 所有安装或悬挂在屋顶/天花板上的物件一律不生成、不渲染，包括餐厨吊灯；它们会压住俯视房间的观察视野。
- 这类 inventory 项使用 `excluded` 状态并记录产品原因，Compiler 拒绝将其接入 production 或 `artDraft`。
- 墙面灯串、壁挂植物等不属于屋顶对象，仍需按正常对象拆分和审计。

### 原子对象的删除测试

隐藏任一对象时：

1. 只允许该对象、它的局部接触阴影及附属光效消失。
2. 背后必须出现完整、可信的壳层或其他对象，不得留下家具残影、透明洞或重复边缘。
3. 其他对象、角色路径和深度排序不能改变。

只有通过删除测试，才算真正从扁平母版中拆出。

## 当前状态与差距

现有框架已经具备：

- `room.json` 单一场景事实来源。
- 可复现、整体替换 runtime 的 Room Compiler。
- 独立 Room Runtime、共享深度排序和语义动作深度。
- `back/front` 独立素材模式以及母版取色 matte 模式。
- room-editor、21 项视觉矩阵和确定性视觉基线。

但当前母版仍包含绝大多数家具像素。六个已建模对象也主要只有前侧遮挡层，其完整主体尚未从母版移出。因此本计划不是继续添加几个 occluder，而是重建“clean shell + 全对象层”的素材体系。

## 初始资产盘点

下列是第一版 inventory。实施时以原分辨率母版逐区复核，新增或合并对象都要记录理由。

### 建筑与窗口区

- `window-view`：窗外城市景色，可作为固定结构子层。
- `window-frame`：窗框与窗台结构。
- `window-curtains`：窗帘、帘杆与绑带。
- `window-planter-left`、`window-planter-right`：两个窗台植物组。
- `window-hanging-plant`：右墙吊篮植物。
- `window-string-lights`：右墙装饰灯串。
- `wall-art-window-upper`、`wall-art-window-lower`、`wall-art-bedside`：三组墙画。

### 工作与收纳区

- `desk`：书桌主体。
- `office-chair`：办公椅。
- `desk-laptop`、`desk-lamp`、`desk-stationery-cluster`、`desk-book-cluster`：桌面物件。
- `desk-wall-shelf`：左墙搁板。
- `desk-wall-books`、`desk-wall-plants`、`desk-wall-photo-cluster`：搁板及墙面装饰。
- `tall-bookcase`：书桌与冰箱之间的高书柜。
- `bookcase-content-cluster`：书柜内书籍、盒子、灯和小植物。
- `desk-floor-plant`：书桌左下盆栽。
- `desk-rug`：工作区地毯。

### 厨房区

- `kitchen-wall-cabinets`：上部吊柜。
- `kitchen-wall-cabinet-decor`：吊柜顶部与侧边植物、小家电。
- `kitchen-sink-counter`：水槽与左侧台面柜。
- `kitchen-stove-counter`：灶台与右侧台面柜。
- `oven`、`fridge`：两个家电对象。
- `kitchen-shelf`、`kitchen-utensil-rail`：墙面层架与挂具。
- `kitchen-sink-counter-decor`：附着水槽柜的瓶罐与器具 cluster。
- `kitchen-stove-counter-decor`：附着灶台柜的水壶与食物 cluster。
- `kitchen-pendant-light`：餐厨吊灯；按观测视野规则标记 `excluded`，不进入场景。
- `kitchen-bin`：冰箱旁垃圾桶。

### 餐区

- `dining-table`：餐桌主体。
- `dining-chair-left`、`dining-chair-right`：可辨认的餐椅。
- `dining-table-setting`：杯、花瓶、餐垫 cluster。
- `dining-rug`：餐桌下地毯。
- `dining-side-table`：窗边小边柜。
- `dining-side-plant`：边柜盆栽。

### 卧室区

- `bed-frame`：床架与床头板。
- `bed-bedding`：床垫、被褥和枕头；睡眠姿态需要与其分层。
- `bedside-table`：床头柜。
- `teal-stool`：床侧 AI 新增脚凳；原始 chroma source、Compiler 变换、占地和前后审计已完整接通。
- `bedside-lamp`、`bedside-decor-cluster`：台灯及柜面物件。
- `bed-rug`：床侧地毯。
- `bed-divider-bookcase`：床前矮书柜/隔断。
- `bed-divider-content-cluster`：书、灯与植物 cluster。

### 客厅与前景区

- `sofa-frame`：沙发主体。
- `sofa-cushion-green`、`sofa-cushion-pink`、`sofa-throw`：沙发软装。
- `living-rug`：客厅大地毯。
- `coffee-table`：茶几主体。
- `coffee-table-setting`：茶具、书和花瓶 cluster。
- `foreground-console`：前景长柜。
- `foreground-console-plants`、`foreground-table-lamp`：长柜植物与台灯。
- `foreground-ottoman`：前景脚凳。
- `living-floor-lamp`：沙发后侧落地灯。
- `living-large-plant`：床前大型盆栽。

盘点初稿约 60 个对象/cluster。最终数量不是目标；目标是所有非壳层像素都有明确所有者，且删除测试成立。

## 目标场景模型

### 房间清单

`room.json` 扩展为：

```json
{
  "shell": {
    "image": "clean-shell",
    "allowedContent": ["walls", "floors", "window-opening", "ambient-light"]
  },
  "objects": [
    {
      "id": "office-chair",
      "category": "furniture",
      "assetMode": "layered",
      "occupancy": {"kind": "footprint", "tiles": [[1, 6]]},
      "depthTile": [1, 6, 0],
      "layers": [
        {"role": "back", "source": "sources/office-chair-back.png"},
        {"role": "front", "source": "sources/office-chair-front.png"}
      ],
      "interactions": [],
      "audits": {"hidden": true, "solo": true, "behind": true, "front": true},
      "provenance": {"method": "ai-edit", "reference": "zhizhi-room-isometric-v2.png"}
    }
  ]
}
```

### 对象类别

- `furniture`：占地家具，通常有 footprint 和前后深度。
- `appliance`：家电，可能有状态变化。
- `lighting`：灯具，资产与局部光效分开声明。
- `plant`：植物与花器，可为墙挂或落地。
- `soft-furnishing`：地毯、窗帘、被褥、抱枕和毯子。
- `wall-decoration`：画、照片、灯串、墙面层架内容。
- `decor-cluster`：不可单独操作的一组小摆件。
- `structural-sub-layer`：窗景、窗框等允许独立于 clean shell 的固定子层。

### 无互动对象

没有动作不代表没有数据。每个无互动对象仍必须声明：

- `id`、类别、素材来源与层角色。
- `depthTile` 或 `occupancy.kind = "wall/none"`。
- footprint；不占地时必须明确写 `none`，不能省略后让 Runtime 猜。
- `interactions: []`。
- `hidden/solo` 审计；可能遮人时还需 `behind/front`。
- `behind/front` 不是为了凑齐矩阵而强制成对。贴墙、夹在大型家具之间或物理上没有后侧可达空间的窄物，只声明真实可达的一侧；不可达侧必须显式为 `false`，禁止伪造路径让角色穿墙或被整组家具吞没。

### 对象依赖与占用例外

- 放在家具上的软装或 decor cluster 使用 `attachedTo` 指向直接承载对象；依赖必须构成无环图，不能靠 id 命名约定推断。
- hidden 审计按依赖向下级联；solo 审计按依赖保留祖先链。该规则只控制可见图层，不改变房间的事实占地。
- interaction approach 默认不能位于任何 footprint 内。若动作天然需要走到一个可移动座椅所在格，interaction 通过 `allowOccupiedBy` 精确声明允许忽略的对象；禁止全局放宽碰撞。
- production 与 `artDraft` 使用各自对象集合做 topology 校验，避免正式场景通过而草稿装配实际不可走。

## 美术重建策略

### Clean shell

扁平母版中被家具遮住的墙面与地面像素不存在，不能仅靠抠图恢复。需要以母版为严格参考，分区移除所有非结构对象并补全：

- 木地板纹理和厨房地砖网格。
- 两面墙的颜色、颗粒与转角。
- 被柜体、床、沙发和书桌遮挡的地面延伸。
- 被墙画、窗帘、吊柜和搁板遮挡的墙面。

AI 可用于局部修复和补画，但每次只处理一个受控区域；禁止重新生成整间房导致透视、颜色和像素密度漂移。

### 对象素材

优先顺序：

1. 从母版精确提取可见 RGB 和 alpha。
2. 对缺失背面或被其他物体遮住的区域，使用母版裁切作为参考进行局部 AI 补全。
3. 需要角色穿行的家具生成同画布、同 origin 的 `back/front`。
4. 永远在角色后方的墙饰、地毯等使用单层 `body`。
5. 接触阴影属于对象的 `shadow` 层；对象隐藏时阴影一并隐藏。

所有 AI 结果必须经过像素化、调色、透明边缘和原点校准，不能直接进入 runtime。

## Compiler 与 Runtime 重构

### Room Compiler

新增校验：

- inventory 中每个 `required` 对象都存在于 manifest。
- 对象 id、类别、occupancy、layers、provenance 和 audits 完整。
- 每个 layer 的 source、alpha、尺寸、origin 和角色合法。
- `back/body/front/shadow/light` 角色组合符合对象类别。
- wall/none 对象不能意外阻塞寻路；footprint 对象必须位于网格内。
- clean shell 与 runtime 产物可复现、无过期对象文件。
- 对象隐藏时其所有派生层和光效都从 bundle 中一起关闭。

### Room Runtime

统一绘制队列：

```text
clean shell
→ structural sub-layers
→ object shadow/back/body
→ actor
→ object front
→ local light/effect
→ editor/debug overlay
```

Runtime 不包含家具名称特判。层角色、depthTile、occupancy 与 interaction depth 全部来自 bundle。

### Room Editor

扩展为资产审计器：

- inventory 列表与完成状态。
- `hidden / solo / back / body / front / shadow / final` 模式。
- 对象层、origin、depth、footprint 和 provenance 查看。
- 一键隐藏对象执行删除测试。
- 标记 `pass / needs-art / needs-depth / needs-path`，导出审计记录。

## 迭代波次

每个波次都执行同一闭环：盘点 → 生成/提取 → 编译 → hidden/solo → behind/front → 路径 → 文档 → 测试。上一波未通过，不开始下一波。

### 波次 0：事实基线与 schema

- 冻结当前母版、bundle、视觉基线和 inventory。
- 扩展 manifest schema、Compiler 和 Runtime 的通用层角色。
- 增加 `?demo=atomization&object=<id>&mode=<hidden|solo|layers>`。

验收：可以在不改 Runtime 的情况下注册一个无互动分层对象，并通过隐藏/solo 测试。

### 波次 1：主要遮挡家具

- 书桌、办公椅、床架/被褥、沙发主体/软装、茶几、餐桌/餐椅、床前隔断。
- 将当前六个 occluder 兼容对象迁成完整层对象。

验收：这些对象的完整 RGB 不再存在于 clean shell；删除测试、behind/front 和现有五类动作全部通过。

### 波次 2：厨房与大型收纳

- 高书柜、吊柜、两组台面柜、冰箱、烤箱、墙架、挂具和垃圾桶。

验收：厨房区域可以逐对象隐藏；餐厨路径和人物前后关系不受影响。

当前草稿进度：已装配 29 个对象、36 个 runtime 资产。高书柜/内容、吊柜/顶部 decor、水槽柜/台面 decor、灶台柜/台面 decor、冰箱、无炉面的嵌入式烤箱、墙架、挂具与垃圾桶均已进入同一对象契约。两个台面 cluster 分别 attached 到物理父柜，子对象 hidden/solo 和父柜 hidden 级联通过；烤箱通过 behind/front，贴墙垃圾桶仅保留真实可达的 front 审计。餐厨 tour 无新增阻塞，用餐动作改为左侧餐椅坐姿，避免角色落进厨房柜体与餐桌的复合遮挡区。波次 2 草稿功能闭环完成，全部素材仍为 `needs-art`，终稿逐像素基线留到波次 6 统一批准。

### 波次 3：地毯、灯具、植物与窗区

- 四块地毯、所有非屋顶落地/台面灯、主要盆栽、窗帘、窗台植物、吊篮与墙面灯串。

验收：灯具隐藏会同时移除其局部光效；地毯不阻塞路径；窗帘与植物深度正确。

当前进度：四块地毯与两株落地植物已通过草稿检查点。`desk-lamp` 已按 `body + light` 双层对象进入草稿；clean shell 候选移除了烙在墙地面的局部灯斑，光效 source 已单独保留并提取 alpha。餐厨吊灯及所有屋顶对象按用户观测规则排除；床头灯和前景台灯 source 已生成，但因承载家具尚未原子化而不接入 runtime。当前共 36 个草稿对象、44 个构建资产。

### 波次 4：桌面、柜内和墙面 decor clusters

- 桌面用品、书柜内容、茶具、花瓶、书本、画、照片和墙面小装饰。

验收：母版仅剩允许的 clean shell 内容；inventory 100% 有对象归属。

### 波次 5：整屋动作与路径再设计

- 根据拆分后的真实 footprint 重建 walkable 和 tour。
- 为适合互动但当前无动作的对象补候选 interaction anchors，但不虚构 daemon 行为。
- 检查坐、睡、学习、用餐、看手机以及所有家具旁的自然停留姿态。

验收：角色不会穿体、瞬移或站在家具表面；所有可达区域连通。

### 波次 6：最终验收

- 每个对象运行 hidden/solo；遮挡对象运行 behind/front。
- 完整 tour、所有动作、四方向和 room-editor 视觉检查。
- 资源请求、控制台、Compiler、Runtime、专项测试和全量测试记录。
- 双轴代码审查：工程标准与本计划逐项核对。

## 维护文档

每完成一个对象或 cluster，同时更新：

- 本文 inventory 状态。
- `room.json` 与对应 provenance。
- `docs/dashboard-visual-worklog.md`。
- AI 提示词或局部修复说明。
- hidden/solo/behind/front 基线和测试结果。

不得出现“代码已经支持，所以对象算完成”。只有素材、数据、运行时画面和审计证据同时存在，才可标记完成。

## 完成标准

- clean shell 中只含本文允许的建筑结构。
- inventory 覆盖母版全部非结构内容，且每项都有独立对象或明确 decor cluster。
- 所有对象通过 hidden/solo 删除测试；可能遮挡角色的对象通过 behind/front。
- 所有无互动对象也拥有完整资产、类别、occupancy/depth、provenance 和审计记录。
- 当前六件家具、五类动作、路径和视觉风格无回退。
- 新对象可只通过素材与 manifest 接入，不修改 Runtime 家具逻辑。
- Compiler 构建可复现，Runtime 资源完整，浏览器无加载/绘制错误。
- 专项测试通过；全仓既有失败与本项目回归分开记录。
- 代码审查对本计划无缺失或错误实现。

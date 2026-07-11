# 知栀小屋视觉工作记录

最后更新：2026-07-12

## 用户目标

将 daemon 面板的小屋做成可观察的 2.5D 等距虚拟家园：人物会沿斜向地面移动，站定、行走与活动状态可信，并在床、书桌、沙发、茶几等家具前后呈现正确遮挡。

美术基准固定为 `assets/dashboard/zhizhi-room-isometric-v2.png`。后续资产应继承其暖色、细节密度、人物比例和像素风格，不再使用之前生成的高密度房间作为视觉基准。

## 已完成

- 保持 daemon 的 `scene_id / location / action / expression` 契约不变；视觉层不会反写生活事实。
- 接入低密度四方向、四帧等距行走表 `zhizhi-iso-walk-v4.png`：屏幕向下使用正面帧，向上使用背面帧。
- 到站后切换回旧角色 sheet 的独立 idle 姿态，不再冻结在 walk 帧。
- 增加 `?demo=walk` 行走预览，以及 `?demo=audit&axis=...` 四方向巡检入口。
- 增加 `?demo=audit&object=<sofa|desk|bed|table>&side=<behind|front>` 家具前后站位巡检入口。
- 将 AI 生成的单件家具图用作 alpha matte，导出四张坐标锁定的前景层：`runtime/occluders/{desk,bed,sofa,table}-front.png`。前景颜色直接从视觉母版同坐标采样，因此 AI 图只提供不规则遮挡轮廓，不会把错位的家具像素叠回房间。
- 从整张 AI 家具 alpha 中追加导出餐桌与床前书柜前景层；两者同样只沿用 alpha，颜色仍取母版原坐标，因此不会重画家具。
- 渲染改为 Habbo 式共享深度列表：静态房间后层 → 角色 → 家具 `frontOccluder`；不再按 `maskPolygon` 重绘背景。
- 屋内巡回路线扩展到餐桌椅、中央通道、沙发侧、床右侧与前景茶几，并在端点持续折返；床边 `[7,1]` 已纳入可走终点。
- 增加 `?demo=activity&spot=<desk|dining|sofa|bed|phone>`，分别检查书桌、用餐、沙发、睡眠与手机坐姿；床使用角色 sheet 中的横卧姿态，沙发/手机使用坐姿。

## 本轮收口

- 已校准六张 `frontOccluder` alpha matte 的定位与保留范围：前景层只包含实际处于角色前方的家具轮廓，不把整件家具重复叠回画面。
- 已校准六件家具的巡检站位与主要交互锚点；书桌、餐桌使用站姿投影点，沙发使用坐姿，床使用横卧姿态。
- 默认 `?demo=walk` 经过沙发后方；新增的 `?demo=tour` 会覆盖餐桌、中央通道、沙发侧、床边和茶几前景区。
- 巡检页现在在 daemon 的用户列表或状态接口暂不可用时仍会启动：保留 `geoff` 作为本地回退选项，先渲染小屋与 URL 预览，再尝试同步状态。这样 `walk` 与 `audit` 不依赖后端健康状态。
- 本轮从「背景裁切」切换到 alpha 前景层；沙发与床已去掉会错误遮人的后部层，书桌和茶几加入同一深度排序列表。
- 2026-07-12 开始可扩展重构：房间结构已迁移到 `rooms/zhizhi-home/room.json`，页面改为读取 Compiler 生成的 bundle；迁移后的连续巡回画面与原视觉一致。
- Room Compiler 已替代遮挡脚本中的坐标表，并在写产物前校验 alpha、母版边界、路线连续性、交互连通性、对象 id、输出名和 footprint 冲突。
- 房间投影、寻路、动作、共享深度排序和预览已移入独立 Room Runtime；dashboard 页面不再持有家具坐标或渲染实现。
- 新增 `?demo=room-editor` 校准工具，可检查母版/alpha/最终合成、网格、footprint、交互点和 depth，并拖动 origin、导出清单片段。
- Compiler 与 Runtime 已支持独立 `back/front` 分层素材；临时 fixture 已验证 RGB/alpha 原样保留及 `back → actor → front` 顺序。
- 行走方向、帧数、播放速率、显示尺寸和 idle/sit/sleep crop 已迁入 `room.json`；换动作素材不再修改 Runtime。
- Compiler 现在先完成全部校验，再在临时目录构建并整体替换 runtime；重复编译的文件哈希一致，删除对象也不会留下旧素材。

## 本次验证（2026-07-11）

- 浏览器已逐一打开四个家具的 `behind` / `front` 巡检 URL，均显示对应的“不写入 daemon”状态。
- 已检查沙发后方、茶几前方和默认 `?demo=walk` 的实际画面；默认路线会从沙发后方经过。
- 已在浏览器中检查六张 alpha 前景层的 `behind/front` 画面；没有重复家具、平齐横切或 Canvas 资源加载/绘制错误。
- 四条方向轴的巡检入口均可加载，浏览器控制台没有资源加载或绘制错误。
- 完成 21 项室内视觉矩阵：沙发、书桌、床、茶几、餐桌、床前书柜各自 `behind/front`，四条斜向轴，以及书桌/用餐/沙发/睡眠/手机五个动作；控制台无资源或绘制错误。
- 连续巡回已实际跑过餐桌区、中央通道、床右侧与前景茶几区；床边不再是不可达区域，角色会在路线端点折返。
- 睡眠改用角色 sheet 的横卧姿态并绘制在床面层之上；沙发与手机使用坐姿，书桌与餐桌使用独立姿态投影点，避免站进家具体积。
- 完整运行 `uv run pytest -q`：517 项通过，只有 Starlette/httpx 依赖自身的弃用警告。

## 重构验证（2026-07-12）

- 独立 Runtime 迁移后重新完成 21 项矩阵：六件家具各自 `behind/front`、四条斜向轴和五个动作入口全部返回正确预览状态。
- 实际检查连续 tour、沙发坐姿、床上睡眠，以及 room-editor 的最终合成、母版和 alpha 模式；迁移前后视觉一致。
- `room-editor` 已实际切换床对象、alpha 模式和 behind 站位；网格、footprint、交互点、depth 与 origin 框均正常显示。
- 新增 `freeze=1&view=canvas` 确定性 Canvas-only 预览，并保存 tour 起点与全部七件家具 behind/front 的十五张渲染基线及 SHA-256 清单到 `docs/visual-baselines/dashboard-room/`；覆盖集合由 runtime bundle 的对象列表校验。
- 新增 `scripts/verify_dashboard_visual_baselines.py`，将当前截图目录与批准基线做逐像素比较；尺寸、像素或清单不一致即失败。
- 用内置图像生成流程制作 `teal-stool` 候选素材；裁切、缩放、chroma-key 去背与 despill 参数已写入 `room.json` 的 `sourceTransform`，Compiler 直接从原始候选图构建独立 occluder，Runtime 自动获得遮挡巡检入口，无需修改渲染源码。浏览器已确认床侧 behind/front 两种层级。
- Room Compiler/Runtime/Dashboard 相关测试：15 项通过；JS Runtime/Editor 内部 6 项行为测试包含在其中。
- 小屋相关 Ruff、JavaScript 语法检查与 `git diff --check` 通过；Compiler 连续两次构建的 runtime 哈希完全一致。
- 全仓 pytest 当前受并行存在的 world/engine 工作区改动影响：最近一次为 496 项通过、43 项失败，失败集中在 `engine`、`world`、scheduler 与 replay 测试，不经过小屋模块；全仓 Ruff 另有 `engine.py:968` 既有未使用变量。此处不跨范围修改。

## 全资产原子化启动（2026-07-12）

- 新计划见 `docs/dashboard-room-full-asset-atomization-plan.md`，目标由六个遮挡对象扩展为 clean shell 与全部可见家装对象。
- 当前 inventory 记录 64 项：57 项 planned、六项 partial、`teal-stool` 一项 verified；现有书桌、床、沙发、茶几、餐桌和隔断没有被误标为 atomized。
- Compiler 已验证 inventory 所有权与状态，并把 summary 编入 runtime bundle。
- 新增 hidden/solo 删除测试入口与 Editor 资产盘点。实际隐藏 partial 书桌后，主体仍留在扁平母版，确认 clean shell 与完整 RGB 分层尚未完成。

## 全资产原子化 · 波次 0（2026-07-12）

- manifest 的原生对象契约扩展为 `category / assetMode / occupancy / layers / interactions / audits / provenance`；`teal-stool` 是首个不依赖旧字段的无互动对象。
- Compiler 暂时接收六件旧家具输入，但编译后全部对象只有统一 `layers[]` 和 `occupancy` 结构；所有派生图层统一输出到 `runtime/layers/`。
- Runtime 已移除 `footprint / frontOccluder / backLayer` 读取，按通用角色渲染；Editor 支持对象图层选择、hidden/solo、单角色视图、alpha、origin、depth 和 provenance。
- 新增 `mode=layers&role=...` 审计入口；JS 测试确认 hidden、solo 和 layers 视图不改变寻路占地。
- 浏览器复查 `teal-stool` front-only、`sofa/behind` 与 room-editor，控制台无错误；沙发角色切口保持斜向家具轮廓，没有重新出现横向截断。
- clean shell 使用内置图像编辑生成，原始生成尺寸为 1419×1108；项目内仅保留对齐候选 `clean-shell-ai-aligned-v3.png`（母版尺寸 1391×1086）。候选保留空墙、木地板、厨区瓷砖、窗洞/窗景和环境光，但窗洞与瓷砖边界仍需逐像素几何复核，暂不接入 runtime。
- 同一流程生成首批波次 1 chroma 候选：`sofa-frame`、`bed-frame`、`bed-bedding`、`coffee-table`。它们仅是待去背、缩放与 origin 校准的 `needs-art` 源，不计入 inventory 完成度。
- Compiler 新增 shell 与视觉母版尺寸一致校验，防止未经对齐的 AI 底图进入构建。

## 后续扩展规则

- 后续若新增家具或新动作，必须同时添加 `behind/front` 或动作巡检入口，不能只改 daemon 映射。
- 每次视觉更新必须检查 Canvas 控制台无资源加载或绘制错误。
- 提交时只应包含小屋相关改动；当前工作区另有既存改动，因此本轮不代替用户做混合提交。

## 当前技术约束

原始小屋是一张合成背景，并没有原始分层文件。当前以母版像素生成近侧家具的 alpha 前景层，作为过渡性、可复现的分层资产；长期应以原始的 `back / frontOccluder` 美术文件替换这些轮廓导出层。AI 家具层只作为研究素材，不直接叠加到原始背景。

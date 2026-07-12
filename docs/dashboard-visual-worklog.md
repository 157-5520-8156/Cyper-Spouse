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

## 全资产原子化 · 波次 1 草稿装配（2026-07-12）

- 新增可编译 `artDraft`：使用 clean shell 候选作为背景，并让草稿对象复用正式 `layers/occupancy/depth/audits/provenance` 契约；Runtime 仅通过数据切换，不新增家具名称分支。
- `?demo=art-draft` 已在浏览器实际装配 16 个对象：书桌/办公椅、沙发框架/两抱枕/毯子、茶几/茶几摆件、床架/床品、餐桌/两把餐椅/餐桌摆件、床前隔断/内容 cluster。
- 四批内置图像编辑均以视觉母版为 reference，使用 flat green/magenta chroma 背景；项目保存原始候选，由 Compiler 执行 crop、chroma key、despill 和 resize，避免把一次性手工导出当事实来源。
- 浏览器画面显示整体比例、等距角度和暖色像素风格已能形成一致空房装配；控制台无加载/绘制错误。隔断内容仍需向柜体内重新校准，所有对象仍为 `needs-art`，默认生产画面保持旧母版不变。
- Compiler 已验证草稿对象 inventory 所有权、统一 id/output/footprint、source、alpha 轮廓、母版边界、审计能力与 clean shell 尺寸；runtime 整体替换会同时清除过期 draft layers。
- 代码审查发现默认 preload 曾包含草稿图片；现已将候选 URL 隔离到 `artDraft.images` 并按需加载。自动测试确认 production/draft 图片集合不相交，浏览器重新检查默认 sofa behind 画面无错误。

## 全资产原子化 · 波次 2 首批收纳与吊柜（2026-07-12）

- 新增高书柜、书柜内容 cluster、厨房吊柜与吊柜顶部 decor 四件 chroma source；Compiler 依据 manifest 的 crop、chroma key、resize、origin 生成四张 draft layer，`artDraft` 从 16 增至 20 个对象。
- 内容物通过通用 `attachedTo` 归属父家具：书柜内容归属高书柜，吊柜 decor 归属吊柜；床品、沙发软装、桌面摆件和隔断内容也迁入同一依赖契约。
- Runtime 的 hidden 会递归关闭依赖后代，solo 子物件则保留祖先；JS 测试覆盖多级依赖，不以家具 id 写特判。Compiler 同时校验未知父 id 与 attachment cycle。
- `artDraft` 现在和 production 分别验证路径拓扑；书桌交互点被办公椅占用的真实结构使用 `allowOccupiedBy: ["office-chair"]` 显式建模，寻路只在前往该交互点时忽略该椅占地。
- 浏览器检查 `tall-bookcase hidden`、`bookcase-content-cluster solo`、`kitchen-wall-cabinets hidden`、`kitchen-wall-cabinet-decor solo`：两组父子联动正确，其他对象未被误隐藏，页面没有破图。
- 代码审查发现局部 effect 原先未复用父子可见性，以及高书柜 `body` 角色无法参与人物前后排序；现已统一 effect 判断，并将柜体/内容放入共享 `front` 深度队列。浏览器复核高书柜 behind 时角色被柜体轮廓遮住、front 时角色完整位于柜前。
- 自动验证：Room Runtime JS 17 项通过，Room Compiler Python 23 项通过，小屋相关 Ruff 与 `git diff --check` 通过。四件新对象仍处于 `planned / needs-art`，等待厨房剩余对象组合后做 origin、删除测试和视觉基线统一校准。

## 全资产原子化 · 波次 2 厨房下柜与冰箱草稿（2026-07-12）

- 以母版为 reference 生成水槽柜、灶台柜、冰箱和烤箱 chroma 候选；烤箱错误成为带炉面的独立 range，已在 machine-readable `artCandidates` 标记 `rejected`，未接入 runtime。
- 水槽柜、灶台柜和冰箱进入 `artDraft`，对象总数从 20 增至 23，runtime 构建资产从 27 增至 30；三者均声明 category、footprint、depthTile、front layer、hidden/solo/behind/front、provenance 与 audit 点。
- 色键背景存在轻微渐变，主体边界在色差阈值 40–80 内稳定；manifest 使用较宽的 `transparentThreshold=50 / opaqueThreshold=160`，Compiler 仍从原始 source 可复现裁切、去色键和缩放。
- 首次整屋浏览器截图发现灶台柜过大并侵入餐桌；将水槽柜校准为 `165×145 @ [515,285]`，灶台柜校准为 `150×150 @ [655,270]` 后，两组下柜回到厨房后排，冰箱与水槽柜保持前后叠放，餐桌区不再被覆盖，页面无破图。
- 浏览器完成三件对象各自 hidden/solo/behind/front 共 12 项矩阵：hidden 只移除目标，solo 在 clean shell 上独立显示，behind/front 均按家具不规则轮廓切换角色深度；全部页面无破图，console 无 warning/error。
- 三件资产仍为 `planned / needs-art`：删除与深度矩阵已通过，但逐像素风格终稿、嵌入式烤箱、墙架、挂具、台面 decor、垃圾桶和厨房最终视觉基线尚未补齐。

## 全资产原子化 · 波次 2 厨房固定物补齐（2026-07-12）

- 第二版烤箱候选去掉了错误的炉面，作为独立嵌入式烤箱接入；同时新增墙架、挂具和窄垃圾桶。`artDraft` 从 23 增至 27 个对象，runtime 构建资产从 30 增至 34。
- 四件候选全部保留原始 chroma source，并由 manifest 记录 crop、chroma key、resize、origin、category、occupancy、depth、provenance 与 audits；没有把一次性导出图当成事实来源。
- 浏览器完成四件对象的 hidden/solo 删除矩阵：每个 hidden 均为 26 个可见对象且不含目标，每个 solo 均只保留目标，27 个对象和 38 张预加载图的审计元数据一致，无破图。
- 烤箱完成 behind/front：角色后站时由烤箱不规则 alpha 轮廓自然遮挡，前站时完整绘于烤箱前。垃圾桶夹在高书柜与冰箱之间，没有真实的后侧可达空间，因此不伪造 behind 路径，只保留 front 审计。
- Runtime bundle 请求改为 `cache: no-store`，冻结预览暴露 `roomObjectCount / roomVisibleObjects / roomLoadedImageCount / roomRenderReady`，用于浏览器等待确定性首帧；静态 runtime URL 增加版本参数，避免 WebView 延用旧脚本。
- 浏览器截图接口实际返回 JPEG 字节，即便调用方请求 PNG；直接按 PNG 展示会偶发形成黑洞式预览。视觉检查现先按真实格式解码再转无损 PNG，确认 Canvas 本身没有丢层。该现象属于审计传输层，不是房间渲染缺图。
- 自动验证：Room Runtime JS 19 项通过，Room Compiler Python 24 项通过，`git diff --check` 通过。四件新资产仍为 `planned / needs-art`；台面 decor 和厨房最终视觉基线完成前，波次 2 不标记完成。

## 全资产原子化 · 波次 2 台面小物与餐厨动作收口（2026-07-12）

- 原 inventory 的单一 `kitchen-counter-decor` 跨越两组父柜，隐藏任一柜体都会产生所有权歧义；现拆为 `kitchen-sink-counter-decor` 与 `kitchen-stove-counter-decor`，inventory 增至 65 项（58 planned、6 partial、1 verified）。
- 使用房间母版作为风格 reference，分别生成罐瓶/器具组和水壶/面包砧板组的洋红 chroma source；Compiler 依据 manifest 可复现裁切、去色键、缩放并生成两张独立 draft layer。`artDraft` 增至 29 个对象，runtime 构建资产增至 36。
- 两个 cluster 分别 `attachedTo` 水槽柜与灶台柜。浏览器六项矩阵验证：子 hidden 只删除目标，子 solo 保留目标与唯一父柜，父柜 hidden 同时删除父体和对应 decor；另一组厨房对象不受影响。
- 整屋装配中罐瓶组落在水槽柜后沿，水壶/面包板落在灶台上，没有侵入餐桌、冰箱或墙挂具；两组对象都使用 `occupancy.kind=none`，不会改变 tour 路径。
- 餐厨动作复验发现旧 `posePosition=[0.5,0.8]` 会被厨房柜体与餐桌复合遮挡到完全不可见；改为左侧餐椅 `[1,1]` 的 `sit` 姿态，并使用 `above-front` 相对餐桌深度。浏览器确认角色完整坐在椅上，不再出现只剩头部或整人消失。
- 审查发现 Compiler 的 `despill` 原先无论 key 色为何都只压绿色通道，导致洋红 chroma 素材保留细边。现改为先缩放再按实际 key 的主通道去色，并增加洋红 key 回归测试；两张台面小物及既有洋红素材重编译后没有纯洋红半透明边缘，整屋与用餐坐姿复验无回退。
- 波次 2 的草稿数据、父子关系、删除测试、适用遮挡、tour 和用餐动作已闭环；Room Compiler Python 25 项、Room Runtime JS 19 项、Ruff 与差异检查通过，重复构建没有产生未提交差异。所有候选仍保持 `planned / needs-art`，不把功能通过误写成终稿素材完成。最终逐像素批准基线在波次 6 统一生成。

## 全资产原子化 · 波次 3 四区地毯草稿（2026-07-12）

- 以母版为风格、透视与区域配色 reference，分别生成工作区深蓝几何毯、餐区低饱和绿格毯、床侧珊瑚花毯和客厅米绿流苏毯；四张源图独立保留为洋红 chroma source，没有把不同朝向地毯合并成一张资产。
- 四件对象均为 `soft-furnishing`、`body` 图层、`occupancy.kind=none`、空 interactions，避免地毯意外阻塞寻路或进入角色前景队列。`artDraft` 从 29 增至 33 个对象，构建资产从 36 增至 40。
- 首次整屋装配误把 origin/resize 当成 Canvas 坐标，四毯偏左上且过小；按 1391×1086 母版坐标重标定后，工作毯落在书桌/办公椅下，餐毯落在餐桌椅下，客厅毯覆盖沙发/茶几区，床毯缩小并放到床右侧斜向地板内。
- 浏览器八项删除矩阵全部通过：hidden 时 32 个可见对象且不含目标，solo 时仅保留目标；33 个对象、44 张加载图和 render-ready 元数据一致。
- 连续 tour 实际经过工作区与客厅毯，角色仍按书桌、沙发等家具轮廓排序，地毯没有改变路径或遮挡。四件候选仍保持 `planned / needs-art`，等待 Wave 3 其余灯具、植物和窗区组合后统一做终稿基线。

## 全资产原子化 · 波次 3 两株落地植物草稿（2026-07-12）

- 新增 `desk-floor-plant` 与 `living-large-plant` 两张独立洋红 chroma source，Compiler 从 manifest 的 crop、色键、despill、resize 和 origin 生成两张前景层；`artDraft` 从 33 增至 35 个对象，构建资产从 40 增至 42。
- 原书柜内容候选底部混有两株植物，现缩短 `bookcase-content-cluster` 裁切范围，植物像素只归新对象所有；隐藏或替换书柜内容时不会再连带删除/复制落地植物。
- 小盆栽紧贴书柜前侧，所在区域本已由书柜阻挡，因此明确使用 `occupancy.kind=none`；大型植物占据 `[7,4]`，并将地毯锚点移到 `[7,5]`、tour 改由 `[6,4] → [6,5] → [7,5]` 绕过，Compiler 拓扑校验保持床侧与沙发区连通。
- 书桌盆栽夹在已阻挡的书柜前，不存在真实可达的后侧，因此明确 `behind=false`，只保留 front 审计；大型植物的审计点沿等深线横向错开，behind 只遮角色侧边，front 时角色位于叶片前。
- 浏览器运行连续 tour，角色能经过工作区、厨房、客厅和床侧，并沿大型植物的叶片轮廓短暂被遮挡后继续走出；遮挡切口为不规则 alpha 轮廓，不是横向矩形裁切。
- 两件对象仍为 `planned / needs-art`。本检查点只关闭草稿所有权、拓扑、hidden/solo、真实可达侧的 front/behind 与动态游走问题；灯具、其余植物和窗区继续在波次 3 后续处理。

## 全资产原子化 · 波次 3 灯具草稿进行中（2026-07-12）

- 盘点六项 lighting：书桌台灯进入本批；床头灯和前景长柜台灯等待各自承载家具；`living-floor-lamp` 的母版所有者仍需与床前隔断内容复核，窗区灯串留到窗区批次。
- 用户明确排除所有屋顶/天花板安装物，避免遮挡俯视观察视野。已撤回餐厨吊灯对象、光效及生成候选，inventory 改为 `excluded`；Compiler 会拒绝排除项重新进入 runtime。
- 原 clean shell 烙有左墙、餐厨墙角等局部灯斑，无法满足“隐藏灯具同时移除光效”。已生成并对齐一张 neutral-light shell 候选，只保留整体暖色环境基调，移除灯具专属热点。
- 书桌灯声明 `body` 与 `light` 两层、`occupancy.kind=none`、hidden/solo，并附着于书桌。床头灯和前景灯因承载家具尚未原子化，只保留 source 候选，不接入 runtime。草稿暂为 36 个对象、44 个构建资产。
- 半透明光晕直接使用洋红或绿色 chroma 会分别造成灰化或绿边；当前改用蓝幕原始 source，再经标准软蒙版/去色边流程保存独立 alpha source，Compiler 只做裁切和缩放。该结果仍需整屋背景上的颜色与强度检查。
- 浏览器已验证书桌灯 solo 会保留承载书桌，hidden 会同时移除灯具本体与局部光效；整屋落点位于书桌后沿。撤除屋顶对象后的连续 tour 可正常经过工作区、厨房和客厅。
- 自动验证：Room Compiler 26 项、Room Runtime 20 项、Ruff、`git diff --check` 与两次确定性构建均通过；runtime 为 36 个草稿对象、44 个构建资产。

## 全资产原子化 · 床头柜与前景长柜载体草稿（2026-07-12）

- 复核确认现有 `bed-frame / bed-bedding` 不含床头柜，前景长柜也未进入任何草稿对象；因此分别生成无灯具、无植物、无台面 decor 的独立洋红 chroma source。
- `bedside-table` 占据 `[7,0]`，位于床右侧房间边界；真实可达站位不与柜体形成有效遮挡，故只启用 hidden/solo，不伪造 behind/front。
- `foreground-console` 占据本来就不可走的 `[3,7] / [4,7]`，视觉深度单独校准到房间前沿；behind 审计在 `[4.5,7]` 让柜体沿上沿遮住角色下半身，front 因位于房间外而明确为 false。
- 床头灯与前景台灯使用 `front + light`，分别 attached 到直接承载柜；父柜 hidden 会连同灯具和光效消失，子灯 solo 会保留唯一父柜。书桌灯也统一为同一前景灯具层角色。
- 浏览器实测两组父 hidden、子 solo、前景柜 behind 及整屋组合；两件载体没有侵入床、沙发、茶几或路径。草稿增至 40 个对象、50 个构建资产，素材保持 `planned / needs-art`。

## 全资产原子化 · 载体直属 decor 草稿进行中（2026-07-12）

- 新生成床头绿色闹钟/小摆件 cluster 与前景长柜三盆植物 cluster；两张 source 都不含灯具或承载家具，并记录母版 reference 与生成提示。
- `bedside-decor-cluster` attached 到 `bedside-table`；`foreground-console-plants` attached 到 `foreground-console`。两者均为 `occupancy.kind=none`、无互动、只启用 hidden/solo。
- 床头灯缩小并移到柜面右侧，为左侧闹钟留出独立所有权；前景植物放在长柜左/中段，台灯保留右端。草稿暂增至 42 个对象、52 个构建资产。
- Compiler 已生成独立 alpha 层，单层检查无洋红残边。浏览器验证床头摆件右移后落在柜面左侧，前景植物落在长柜左/中段；两组父 hidden 会关闭全部子项，子 solo 只保留直接载体与目标，连续 tour 无变化。

## 全资产原子化 · 窗区软装与墙面植物草稿（2026-07-12）

- 以视觉母版为 reference 分别生成 `window-curtains`、左右窗台 planter、`window-hanging-plant` 和 `window-string-lights`。五张源图保留洋红 chroma 原稿与经标准 helper 生成的 alpha 候选，提示词、reference、原稿及 alpha 路径均写入 machine-readable `artCandidates`。
- 所有权边界明确：窗帘只含成对布帘、绑带和帘杆，不含窗框/玻璃/城市景观；两个 planter 不含窗台；壁挂植物只含花篮、叶片、绳索和垂直墙钩；灯串只含墙钉、导线、灯泡及 1–2 像素局部光晕。
- 用户要求的屋顶排除规则继续作为硬约束。壁挂植物与灯串均固定在右侧垂直墙面，挂点低于屋顶轮廓；没有生成、恢复或渲染任何吊灯、顶灯或屋顶悬挂物。
- 五件对象均声明 `occupancy.kind=wall`、`body` 层、空 interactions、hidden/solo 审计，不认领任何 walkable 格。草稿由 42 增至 47 个对象，构建资产由 52 增至 57。
- 浏览器完成五件 solo 与五件 hidden：solo 不夹带窗框、窗景、墙面或相邻植物，hidden 只删除目标；整屋装配中窗帘包围原窗框，两个 planter 落在窗台，右墙植物位于灯串环内。动态 tour 经过工作区、厨房与客厅，路径没有因窗区资产改变。
- 自动验证：Room Compiler Python 26 项、Room Runtime JS 20 项通过；对象继续保持 `planned / needs-art`，窗框/窗景结构子层与最终逐像素基线尚未收口。

## 全资产原子化 · 窗结构与墙画草稿（2026-07-12）

- 首次无窗 clean-shell 精确编辑仍使目标窗区外约 9.5% 像素产生明显变化，未直接接入。最终候选以既有 neutral-light shell 为底，只合成 AI 生成的局部无窗墙面；右墙窗口区域被补成连续墙体，其他房间像素保持旧 shell。
- 窗景首次使用品红 key，粉紫夕阳与 key 冲突产生大量半透明天空，明确拒绝；v2 改用亮绿色 key 后仅有正常边缘半透明像素。木窗框/中梃/窗台另存独立 alpha 层，窗帘和 planter source 不含其像素。
- `window-view` 与 `window-frame` 保持 sibling，满足隐藏景色时不改变其他对象；attachment DAG 只表达真实承载关系 `window-frame → window-curtains / window-planter-left / window-planter-right`。浏览器确认：隐藏 view 只删除景色；隐藏 frame 会连同窗帘与 planter 关闭；solo 窗帘或 planter 自动保留 frame 祖先。
- `wall-art-window-upper / lower / bedside` 三幅画各自成为单层 `wall-decoration`，分别贴回窄墙上/下位置和床侧右墙；hidden 只删除目标，solo 不夹带墙面或家具。
- 整屋装配为 52 个草稿对象、63 个构建资产（含 Compiler 复现的 art shell）。窗景位于 frame 下层，窗帘包住 frame，窗台植物覆盖窗台，三幅画不侵入灯具、床或窗口。所有新增对象均为 `wall` occupancy；没有生成任何屋顶/天花安装物。

## 全资产原子化 · 工作区桌面与左墙 decor 草稿（2026-07-12）

- 生成并接入 laptop、stationery、desk book cluster、左墙 floating shelf、shelf book cluster、shelf-mounted plant 和 pinned-photo cluster 七张独立资产。每张保留洋红 chroma 原稿，并在 inventory 记录提示、色键、裁切与 resize 参数。
- laptop、stationery 和 book cluster 直接 `attachedTo: desk`，使用 `front` 角色与 desk 的 front 深度分离；desk hidden 会级联关闭三件，子对象 solo 保留 desk 祖先。它们均为 `occupancy.kind=none`，不影响 study approach 或路径。
- shelf 是独立 `wall-decoration`；books 与 plants 只附着 shelf，photo cluster 独立贴墙。浏览器确认 shelf hidden 时 books/plants 一并消失而照片保留，plants solo 时 shelf 祖先保留；四件墙面资产均为 `wall` occupancy。
- 首次整屋、seven-object solo、七件逐一 hidden、desk hidden、shelf hidden、两个子 solo 与连续 tour 均已实际检查；laptop、书和文具没有进入书柜/椅子空间，墙架与照片墙没有侵入角色路线，浏览器控制台无 warning/error。Runtime 测试锁定逐件隐藏不改变路径，desk/shelf 只级联自己的后代，photo cluster 始终独立。草稿累计为 59 个对象、70 个构建资产，继续保持 `planned / needs-art`。
- 无新屋顶/天花素材：壁架植物的承载语义明确为 shelf-mounted。

## 后续扩展规则

- 后续若新增家具或新动作，必须同时添加 `behind/front` 或动作巡检入口，不能只改 daemon 映射。
- 每次视觉更新必须检查 Canvas 控制台无资源加载或绘制错误。
- 提交时只应包含小屋相关改动；当前工作区另有既存改动，因此本轮不代替用户做混合提交。

## 当前技术约束

原始小屋是一张合成背景，并没有原始分层文件。默认生产画面仍以母版像素生成近侧家具 alpha 层；AI 家具目前只进入 clean-shell `artDraft` 装配，不替换默认画面。每件候选必须通过 origin、hidden/solo、behind/front 与删除测试后才能从 `needs-art` 晋级并替换母版内容。

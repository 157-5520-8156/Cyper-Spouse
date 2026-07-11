"""The local visual home. It reads daemon state; it never creates it."""

DASHBOARD_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>知栀的小屋</title>
  <style>
    @font-face { font-family: Pixel; src: url('/assets/pixel_agents/fonts/FSPixelSansUnicode-Regular.ttf'); }
    :root {
      color:#3f342d;
      background:#e8ded0;
      font-family:Pixel,"PingFang SC",system-ui,sans-serif;
      --ink:#3f342d;
      --paper:#f7eedf;
      --line:#684f42;
      --teal:#557f78;
      --rose:#bd766a;
      --gold:#d5a95b;
      --shadow:#b79c84;
    }
    * { box-sizing:border-box; }
    body { margin:0; min-width:320px; background:#d9cdbc; }
    button,select,textarea { font:inherit; }
    button { cursor:pointer; }
    .bar {
      min-height:66px;
      padding:0 24px;
      display:flex;
      align-items:center;
      justify-content:space-between;
      color:#fff8ea;
      background:#4d3b34;
      border-bottom:4px solid #2e2522;
    }
    .brand { display:flex; gap:12px; align-items:center; }
    .brand-icon {
      width:34px;
      height:34px;
      display:grid;
      place-items:center;
      color:#4d3b34;
      background:#f3ce86;
      border:3px solid #fff3d2;
      box-shadow:3px 3px 0 #251d1a;
      font-size:17px;
    }
    h1 { margin:0; font-size:19px; font-weight:400; letter-spacing:0; }
    .brand small,.sync { font-size:12px; color:#e6cfb7; }
    .top-actions { display:flex; align-items:center; gap:10px; }
    .icon {
      width:34px;
      height:34px;
      color:#fff8ea;
      background:#6f8e84;
      border:2px solid #d6c7a5;
      box-shadow:2px 2px 0 #251d1a;
      font-size:20px;
      line-height:1;
    }
    .wrap {
      max-width:1520px;
      margin:0 auto;
      padding:22px;
      display:grid;
      gap:18px;
      grid-template-columns:minmax(600px,1.58fr) minmax(340px,.78fr);
      align-items:start;
    }
    .game-frame {
      background:#2b2422;
      padding:8px;
      border:4px solid #e7c076;
      box-shadow:6px 6px 0 var(--shadow);
    }
    .game-chrome {
      height:38px;
      padding:0 10px;
      display:flex;
      align-items:center;
      justify-content:space-between;
      color:#fff2d6;
      background:#584039;
      font-size:13px;
      border-bottom:3px solid #241b18;
    }
    .live { color:#f3ce86; }
    .live::before {
      content:"";
      display:inline-block;
      width:7px;
      height:7px;
      margin-right:6px;
      background:#df7b70;
      box-shadow:0 0 0 2px #844e4a;
    }
    #roomCanvas {
      display:block;
      width:100%;
      aspect-ratio:1000/760;
      image-rendering:pixelated;
      image-rendering:crisp-edges;
      background:#141115;
    }
    .scene-info {
      min-height:58px;
      padding:10px 12px;
      display:grid;
      grid-template-columns:1fr auto;
      gap:12px;
      align-items:center;
      color:#fff2d6;
      background:#584039;
      border-top:3px solid #241b18;
    }
    .scene-info strong { display:block; font-size:15px; font-weight:400; }
    .scene-info span { display:block; margin-top:4px; color:#e6cfb7; font-size:12px; }
    .tag {
      padding:7px 9px;
      color:#54362f;
      background:#ffd3bd;
      border:2px solid #fff2d6;
      font-size:12px;
      white-space:nowrap;
    }
    .key { margin-top:12px; display:flex; flex-wrap:wrap; gap:7px; }
    .key span {
      padding:5px 7px;
      color:#55463d;
      background:#f6ecdc;
      border:1px solid #bca68e;
      font-size:11px;
    }
    .side { display:grid; gap:12px; }
    .panel {
      padding:15px;
      background:var(--paper);
      border:3px solid var(--line);
      box-shadow:4px 4px 0 var(--shadow);
    }
    .panel h2 { margin:0 0 11px; color:#5a463d; font-size:14px; font-weight:400; }
    .controls { display:grid; grid-template-columns:1fr auto; gap:8px; }
    select {
      min-width:0;
      padding:8px;
      color:#3f342d;
      background:#fff8ea;
      border:2px solid #aa927a;
    }
    .command {
      padding:8px 10px;
      color:#fff8ea;
      background:var(--rose);
      border:2px solid #884d47;
      box-shadow:2px 2px 0 #5b3a36;
    }
    .result { min-height:18px; margin:9px 0 0; color:#795f53; font-size:12px; }
    .stats { display:grid; grid-template-columns:repeat(3,1fr); gap:7px; }
    .stat {
      min-height:78px;
      padding:9px;
      background:#e8f0df;
      border:2px solid #9eb39a;
    }
    .stat b { display:block; color:#466a61; font-size:18px; font-weight:400; }
    .stat span { display:block; margin-top:6px; color:#795f53; font-size:11px; }
    .reason-list { display:flex; flex-wrap:wrap; gap:6px; padding:0; margin:0; list-style:none; }
    .reason-list li {
      padding:6px 8px;
      color:#4e5241;
      background:#edf0dc;
      border:1px solid #b9ba96;
      font-size:11px;
    }
    .timeline { display:grid; gap:0; }
    .timeline-item {
      display:grid;
      grid-template-columns:8px 1fr auto;
      gap:8px;
      padding:9px 0;
      border-bottom:1px solid #decfbd;
    }
    .timeline-item:last-child { border-bottom:0; }
    .dot { width:7px; height:7px; margin-top:4px; background:#6f8e84; }
    .timeline-item.current .dot { background:#df7b70; box-shadow:0 0 0 2px #ffd3bd; }
    .timeline-copy strong { display:block; color:#4c3d36; font-size:12px; font-weight:400; }
    .timeline-copy span,.timeline time { color:#80685b; font-size:11px; }
    .timeline time { white-space:nowrap; }
    .calendar-list { display:grid; gap:6px; }
    .calendar-grid { display:grid; grid-template-columns:repeat(7,1fr); gap:4px; margin-bottom:10px; }
    .calendar-grid button { min-height:44px; padding:4px; color:#4c3d36; background:#fff9ed; border:1px solid #cfb48f; text-align:left; }
    .calendar-grid button.selected { color:#fff8ea; background:#6f8e84; border-color:#3d5a54; }
    .calendar-grid button.today { box-shadow:inset 0 0 0 2px #bd766a; }
    .calendar-grid small { display:block; margin-top:3px; color:inherit; font-size:9px; opacity:.82; }
    .calendar-row { display:grid; grid-template-columns:78px 1fr auto; gap:8px; align-items:center; padding:8px; border:2px solid #cfb48f; background:#fff9ed; }
    .calendar-row time,.calendar-row small { color:#80685b; font-size:10px; }
    .calendar-row strong { display:block; color:#4c3d36; font-size:12px; font-weight:400; }
    .calendar-day { min-height:94px; padding:8px; border:2px solid #cfb48f; background:#fff9ed; }
    .calendar-day.today { border-color:#bd766a; box-shadow:2px 2px 0 #d8a29a; }
    .calendar-day.future { background:#f4eee3; }
    .calendar-day h3 { margin:0 0 5px; color:#5b443a; font-size:12px; font-weight:400; }
    .calendar-day ul { margin:0; padding:0; list-style:none; color:#786055; font-size:10px; line-height:1.45; }
    .calendar-day li::before { content:"· "; color:#bd766a; }
    .calendar-day .planned::before { color:#557f78; }
    .task {
      padding:8px;
      margin-top:7px;
      color:#684b42;
      background:#ffe5d6;
      border-left:4px solid var(--rose);
      font-size:12px;
    }
    .task small { display:block; margin-top:4px; color:#8a7167; }
    details { background:#f2eadb; border:2px solid #b8a48e; }
    summary { padding:10px; color:#5f5147; cursor:pointer; font-size:12px; }
    pre {
      max-height:240px;
      overflow:auto;
      margin:0;
      padding:10px;
      white-space:pre-wrap;
      color:#55463d;
      background:#fff8ea;
      border-top:1px solid #cdbca5;
      font:11px/1.45 ui-monospace,monospace;
    }
    @media (max-width:960px) {
      .wrap { grid-template-columns:1fr; padding:13px; }
      .game-frame { max-width:900px; margin:auto; }
      .side { grid-template-columns:1fr 1fr; }
      .side .wide { grid-column:1/-1; }
    }
    @media (max-width:610px) {
      .bar { padding:0 12px; }
      .brand small { display:none; }
      .sync { display:none; }
      .wrap { padding:9px; }
      .side { grid-template-columns:1fr; }
      .side .wide { grid-column:auto; }
      .game-frame { padding:5px; }
      .game-chrome { height:34px; }
      .scene-info { grid-template-columns:1fr; }
      .scene-info .tag { justify-self:start; }
      .stats { grid-template-columns:repeat(3,1fr); }
      .stat { padding:7px; }
    }
  </style>
</head>
<body>
  <header class="bar"><div class="brand"><div class="brand-icon">栀</div><div><h1>知栀的小屋</h1><small>daemon 生活运行时的可视化投影</small></div></div><div class="top-actions"><span class="sync" id="updated">同步中</span><button class="icon" title="刷新状态" aria-label="刷新状态" onclick="loadContext()">↻</button></div></header>
  <main class="wrap">
    <section>
      <div class="game-frame">
        <div class="game-chrome"><span>沈知栀 · 上海</span><span class="live" id="gameAction">正在进入小屋</span></div>
        <canvas id="roomCanvas" width="1000" height="760" aria-label="知栀会按 daemon 状态行动的等距像素小屋"></canvas>
        <div class="scene-info"><div><strong id="sceneActivity">正在同步生活状态</strong><span id="sceneDetail">动作来自当前活动、手机注意力和情绪投影。</span></div><div class="tag" id="sceneTag">--</div></div>
      </div>
      <div class="key" aria-label="当前投影说明"><span id="sceneLocation">地点：--</span><span id="sceneActionKey">动作：--</span><span id="sceneMood">表情：--</span><span id="scenePhone">手机：--</span></div>
    </section>
    <aside class="side">
      <section class="panel"><div class="controls"><select id="user" aria-label="选择用户"></select><button class="command" onclick="runProactive()">触发一次判断</button></div><p class="result" id="result"></p></section>
      <section class="panel"><h2>现在</h2><div class="stats"><div class="stat"><b id="attention">-</b><span>注意力占用</span></div><div class="stat"><b id="taskCount">-</b><span>社交余波</span></div><div class="stat"><b id="phoneState">-</b><span>手机状态</span></div></div></section>
      <section class="panel wide"><h2>为什么是这个动作</h2><ul class="reason-list" id="reasons"></ul></section>
      <section class="panel wide"><h2>今天的轨迹</h2><div class="timeline" id="timeline"></div></section>
      <section class="panel wide"><h2>时间账本 · 前 15 天 / 后 15 天</h2><div class="calendar-grid" id="calendarDays" aria-label="选择查看日期"></div><div class="calendar-list" id="calendar"></div></section>
      <section class="panel wide"><h2>还没收住的事</h2><div id="tasks"></div></section>
      <details class="wide"><summary>查看原始 daemon 状态</summary><pre id="state"></pre></details>
    </aside>
  </main>
  <script>
    const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    const labels = {desk:'书桌', kitchen:'餐桌', entry:'门口', sofa:'沙发', vanity:'梳妆台', bed:'床边', window:'窗前', rug:'地毯', study:'看书', eat:'吃饭', walk_out:'出门', social:'和同学待着', relax:'放松', tidy:'收拾', sleep:'睡着', gaze:'发呆', idle:'发会儿呆', notice_phone:'收到提醒', glance_phone:'瞄到消息', read_phone:'看消息', type_phone:'组织回复', withdraw:'先不看手机'};
    const expressionLabels = {neutral:'平静', smile:'心情不错', spark:'好奇', soft:'有点想你', worry:'挂心', sleepy:'困', pout:'有点别扭', guarded:'收着', hurt:'受伤'};
    const roomCanvas = document.getElementById('roomCanvas');
    const stage = {w:1000, h:760, scale:1, ox:0, oy:0};
    const images = {};
    const imagePaths = {
      room:'/assets/dashboard/zhizhi-room-isometric-v2.png',
      sprite:'/assets/dashboard/zhizhi-iso-walk-v4.png'
    };
    const sceneDefinitions = {
      'free-bedroom': {
        source:'知栀 · 原始暖色等距小屋 · 项目视觉母版',
        background:'room',
        // Coordinates are room-space tiles, never background-image pixels.
        tile:{width:85,height:42,origin:[714,393]},
        walkable:['1,6','2,6','3,6','4,6','5,6','6,6','7,6','2,5','3,5','4,5','5,5','6,5','7,5','3,4','4,4','5,4','6,4','7,4','4,3','5,3','6,3','7,3','5,2','6,2','7,2','6,1','7,7','6,7','5,7'],
        anchors:{desk:[1,6,0], kitchen:[2,2,0], entry:[7,7,0], sofa:[4,7,0], vanity:[6,3,0], bed:[5,2,0], window:[6,2,0], rug:[5,5,0]},
        objects:[
          {id:'desk', tile:[1,5,0], footprint:[[1,5]], frontCrop:[80,560,360,300], depthBias:35},
          {id:'bed', tile:[6,1,0], footprint:[[6,1],[7,1]], frontCrop:[880,540,420,300], depthBias:28},
          {id:'sofa', tile:[4,6,0], footprint:[[4,6],[5,6]], frontCrop:[380,700,430,280], depthBias:42},
          {id:'table', tile:[5,6,0], footprint:[[5,6]], frontCrop:[600,800,300,220], depthBias:18}
        ]
      }
    };
    let activeScene = sceneDefinitions['free-bedroom'];
    let snapshot = null;
    let loading = false;
    let selectedCalendarDate = null;
    // The generated sheet contains a standing and a stepping pose for each
    // useful direction.  Keep the pace deliberately relaxed: the room is a
    // visual diary, not a game character sprinting between destinations.
    const WALK_SPEED = 1.65; // tiles per second
    const WALK_FRAMES = 4;
    const actor = {position:[...activeScene.anchors.rug], target:null, path:[], action:'idle', activity:'idle', expression:'neutral', scene:null, facing:'front', walked:0, lastTime:0};

    function preload() {
      return Promise.all(Object.entries(imagePaths).map(([key,path]) => new Promise(resolve => {
        const img = new Image();
        img.onload = () => { images[key] = img; resolve(); };
        img.onerror = resolve;
        img.src = path;
      })));
    }
    function project(point) {
      const [x,y,z=0] = point;
      const tile = activeScene.tile;
      return [stage.ox + (tile.origin[0] + (x-y) * tile.width) * stage.scale, stage.oy + (tile.origin[1] + (x+y) * tile.height - z * tile.height * 2) * stage.scale];
    }
    function key(point) { return `${point[0]},${point[1]}`; }
    function depthKey(point, bias=0) { return ((point[0] + point[1]) * 10000) + (point[2] || 0) * 100 + bias; }
    function tileDistance(a,b) { return Math.abs(a[0]-b[0]) + Math.abs(a[1]-b[1]); }
    function directionFor(dx,dy) {
      // Logical grid axes project to screen-space 45° diagonals.
      if (dx > .01) return 'downRight';
      if (dx < -.01) return 'upLeft';
      if (dy > .01) return 'downLeft';
      if (dy < -.01) return 'upRight';
      return actor.facing;
    }
    function pathfind(start, target) {
      const blocked = new Set(activeScene.objects.flatMap(object => object.footprint).map(key));
      const allowed = new Set(activeScene.walkable.filter(tile => !blocked.has(tile)));
      const startKey = key(start), targetKey = key(target);
      if (!allowed.has(targetKey)) return [];
      const open = [[start[0],start[1]]], came = new Map([[startKey,null]]), score = new Map([[startKey,0]]);
      while (open.length) {
        open.sort((a,b) => (score.get(key(a)) + tileDistance(a,target)) - (score.get(key(b)) + tileDistance(b,target)));
        const current = open.shift();
        if (key(current) === targetKey) break;
        for (const [dx,dy] of [[1,0],[-1,0],[0,1],[0,-1]]) {
          const next=[current[0]+dx,current[1]+dy], nextKey=key(next);
          if (!allowed.has(nextKey)) continue;
          const tentative=(score.get(key(current)) || 0)+1;
          if (tentative < (score.get(nextKey) ?? Infinity)) { came.set(nextKey,current); score.set(nextKey,tentative); if (!open.some(p => key(p) === nextKey)) open.push(next); }
        }
      }
      if (!came.has(targetKey)) return [];
      const path=[]; let current=target;
      while (key(current) !== startKey) { path.unshift([current[0],current[1],0]); current=came.get(key(current)); }
      return path;
    }
    function activateScene(scene) {
      const nextScene = sceneDefinitions[scene.scene_id] || sceneDefinitions['free-bedroom'];
      if (nextScene === activeScene) return;
      activeScene = nextScene;
      actor.position = [...(activeScene.anchors.entry || activeScene.anchors.rug)];
      actor.path = [];
    }
    function applyScene(scene) {
      activateScene(scene);
      const target = activeScene.anchors[scene.location] || activeScene.anchors.rug;
      actor.scene = scene;
      actor.expression = scene.expression;
      actor.activity = scene.action || 'idle';
      if (key(actor.path.at(-1) || actor.position) !== key(target)) {
        actor.path = pathfind(actor.position, target);
        actor.target = target;
        actor.action = actor.path.length ? 'walk' : actor.activity;
      }
    }
    function drawImageContain(ctx, img, x, y, w, h) {
      const scale = Math.min(w / img.width, h / img.height);
      const iw = img.width * scale, ih = img.height * scale;
      stage.scale = scale;
      stage.ox = x + (w - iw) / 2;
      stage.oy = y + (h - ih) / 2;
      ctx.drawImage(img, stage.ox, stage.oy, iw, ih);
    }
    function characterAction() {
      return actor.action === 'walk' ? 'walk' : 'idle';
    }
    function spriteCell(action, facing) {
      // Screen-space down means facing the viewer; screen-space up means
      // showing her back. The four source columns follow that exact order.
      const columns = {downRight:0,downLeft:1,upLeft:2,upRight:3};
      return {column:columns[facing] ?? 0, row:action === 'walk' ? Math.floor(actor.walked * 2.4) % WALK_FRAMES : 0};
    }
    function drawActor(ctx, now) {
      if (actor.action === 'sleep') { drawSleep(ctx, now); return; }
      const action = characterAction();
      const sheet = images.sprite;
      if (!sheet) return;
      const [px, py] = project(actor.position);
      const cell = spriteCell(action, actor.facing);
      const cw = sheet.width / 4, ch = sheet.height / 4;
      const sx = cell.column * cw, sy = cell.row * ch;
      // Every v4 cell is baseline-aligned; never add a per-frame y offset.
      const dh = 132, dw = 132;
      const x = px - dw / 2, y = py - dh + 6;
      ctx.save();
      ctx.imageSmoothingEnabled = false;
      ctx.drawImage(sheet, sx, sy, cw, ch, x, y, dw, dh);
      ctx.restore();
      drawPhone(ctx, x, y, now);
      drawStatusMark(ctx, px, y, now);
    }
    function drawPhone(ctx, x, y, now) {
      if (!['notice_phone','glance_phone','read_phone','type_phone'].includes(actor.action)) return;
      const pulse = actor.action === 'type_phone' ? Math.sin(now / 90) * 2 : 0;
      ctx.save(); ctx.fillStyle = '#25343d'; ctx.fillRect(x + 57, y + 50 + pulse, 9, 15);
      ctx.fillStyle = '#9cd9d4'; ctx.fillRect(x + 59, y + 53 + pulse, 5, 7); ctx.restore();
    }
    function drawSleep(ctx, now) {
      const [px,py] = project(actor.position);
      ctx.save(); ctx.fillStyle = '#e9d7bf'; ctx.font = '18px Pixel, sans-serif';
      ctx.fillText('z', px + 15, py - 32 - Math.sin(now / 350) * 4); ctx.restore();
    }
    function drawStatusMark(ctx, x, y, now) {
      const scene = actor.scene || {};
      if (!scene.has_notification && !scene.has_open_task && !['pout','guarded','hurt','worry','soft'].includes(actor.expression)) return;
      const pulse = Math.sin(now / 280) * 1.5;
      ctx.save();
      ctx.imageSmoothingEnabled = false;
      ctx.fillStyle = scene.has_notification ? '#ffe79b' : '#ffd3bd';
      ctx.strokeStyle = '#5b3a36';
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.roundRect(x - 18, y - 18 + pulse, 36, 18, 3);
      ctx.fill();
      ctx.stroke();
      ctx.fillStyle = actor.expression === 'hurt' ? '#bd6b72' : actor.expression === 'soft' ? '#d78288' : '#557f78';
      const mark = actor.expression === 'hurt' ? '…' : actor.expression === 'pout' || actor.expression === 'guarded' ? '!' : scene.has_notification ? '✉' : '·';
      ctx.font = '12px Pixel, sans-serif';
      ctx.textAlign = 'center';
      ctx.fillText(mark, x, y - 5 + pulse);
      ctx.restore();
    }
    function drawRoom(ctx, now) {
      ctx.imageSmoothingEnabled = false;
      ctx.clearRect(0, 0, roomCanvas.width, roomCanvas.height);
      ctx.fillStyle = '#141115';
      ctx.fillRect(0, 0, roomCanvas.width, roomCanvas.height);
      const background = images[activeScene.background];
      if (background) drawImageContain(ctx, background, 18, 14, 964, 720);
      drawActivityLight(ctx, now);
      drawInteractionCue(ctx, now);
      if ((actor.scene || {}).time_of_day === 'night') {
        ctx.fillStyle = 'rgba(20, 17, 30, .22)';
        ctx.fillRect(0, 0, roomCanvas.width, roomCanvas.height);
      }
      drawActor(ctx, now);
      drawForeground(ctx);
      drawSceneRibbon(ctx);
    }
    function drawActivityLight(ctx, now) {
      const scene = actor.scene || {};
      const target = activeScene.anchors[scene.location] || activeScene.anchors.rug;
      const [x,y] = project(target);
      if (['study','read_phone','type_phone'].includes(scene.action)) {
        const pulse = 6 + Math.sin(now / 500) * 2;
        ctx.save();
        ctx.globalAlpha = .20;
        ctx.fillStyle = '#9cd9d4';
        ctx.beginPath();
        ctx.ellipse(x, y - 18, 38 + pulse, 16 + pulse / 3, 0, 0, Math.PI * 2);
        ctx.fill();
        ctx.restore();
      }
    }
    function drawInteractionCue(ctx, now) {
      const scene = actor.scene || {};
      const [x,y] = project(activeScene.anchors[scene.location] || activeScene.anchors.rug);
      const pulse = Math.sin(now / 240);
      ctx.save();
      ctx.imageSmoothingEnabled = false;
      if (scene.action === 'tidy') {
        ctx.fillStyle = '#ffe49a';
        for (const [dx,dy] of [[-15,-42],[5,-54],[19,-36]]) ctx.fillRect(x + dx, y + dy + pulse * 2, 4, 4);
      } else if (scene.action === 'walk_out') {
        ctx.globalAlpha = .28 + pulse * .06;
        ctx.fillStyle = '#f5c77b';
        ctx.fillRect(x - 20, y - 42, 40, 42);
      } else if (scene.action === 'eat') {
        ctx.strokeStyle = '#e9efff';
        ctx.lineWidth = 2;
        ctx.beginPath();
        ctx.arc(x + 9, y - 33, 7, Math.PI * 1.1, Math.PI * 1.9);
        ctx.stroke();
      } else if (['social','relax'].includes(scene.action)) {
        ctx.fillStyle = '#f7e5ba';
        ctx.fillRect(x + 16, y - 44 + pulse * 2, 4, 4);
      }
      ctx.restore();
    }
    function drawForeground(ctx) {
      const background = images[activeScene.background];
      if (!background) return;
      // Data-driven furniture front layers. They are sorted against the actor,
      // replacing location-specific "redraw this sofa" rules.
      const actorDepth = depthKey(actor.position, 20);
      for (const object of activeScene.objects.filter(o => depthKey(o.tile,o.depthBias) > actorDepth).sort((a,b) => depthKey(a.tile,a.depthBias)-depthKey(b.tile,b.depthBias))) {
        const [sx,sy,sw,sh] = object.frontCrop;
        ctx.save(); ctx.imageSmoothingEnabled = false;
        ctx.drawImage(background, sx, sy, sw, sh, stage.ox + sx * stage.scale, stage.oy + sy * stage.scale, sw * stage.scale, sh * stage.scale);
        ctx.restore();
      }
    }
    function drawSceneRibbon(ctx) {
      const scene = actor.scene || {};
      ctx.save();
      ctx.imageSmoothingEnabled = false;
      ctx.fillStyle = 'rgba(58, 43, 36, .82)';
      ctx.fillRect(28, 24, 226, 38);
      ctx.strokeStyle = '#e7c076';
      ctx.lineWidth = 2;
      ctx.strokeRect(28, 24, 226, 38);
      ctx.font = '12px Pixel, sans-serif';
      ctx.fillStyle = '#fff2d6';
      ctx.fillText(labels[scene.location] || '小屋', 42, 41);
      ctx.fillStyle = '#f3ce86';
      ctx.fillText(labels[scene.action] || '同步中', 42, 56);
      ctx.restore();
    }
    function loop(now) {
      const last = actor.lastTime || now;
      const dt = Math.min(.05, (now - last) / 1000);
      actor.lastTime = now;
      if (actor.path.length) {
        const target = actor.path[0];
        const dx = target[0] - actor.position[0], dy = target[1] - actor.position[1];
        const dist = Math.hypot(dx, dy);
        const speed = WALK_SPEED;
        if (dist <= speed * dt) {
          actor.position = target;
          actor.path.shift();
          if (!actor.path.length) actor.action = actor.activity || 'idle';
        } else {
          actor.position = [actor.position[0] + dx / dist * speed * dt, actor.position[1] + dy / dist * speed * dt, 0];
          actor.facing = directionFor(dx,dy);
          actor.walked += speed * dt;
        }
      }
      drawRoom(roomCanvas.getContext('2d'), now);
      requestAnimationFrame(loop);
    }
    const fmtTime = value => { try { return new Intl.DateTimeFormat('zh-CN',{hour:'2-digit',minute:'2-digit'}).format(new Date(value)); } catch { return ''; } };
    const fmtRange = event => `${fmtTime(event.starts_at || event.started_at)} - ${fmtTime(event.ends_at)}`;
    async function init() {
      await preload();
      const users = await fetch('/debug/users').then(r => r.json());
      const select = document.getElementById('user');
      select.innerHTML = (users.users.length ? users.users : ['geoff']).map(u => `<option value="${esc(u)}">${esc(u)}</option>`).join('');
      select.onchange = loadContext;
      await loadContext();
      applyPreviewMode();
      setInterval(loadContext, 20000);
      requestAnimationFrame(loop);
    }
    function applyPreviewMode() {
      // A non-persistent visual check for the dashboard.  It never changes
      // daemon state and is intentionally opt-in via the URL.
      if (new URLSearchParams(location.search).get('demo') !== 'walk') return;
      // Cross the clear central floor so the visual check shows both stepping
      // frames before the activity pose takes over at the destination.
      applyScene({location:'entry', action:'walk_out', expression:'neutral', time_of_day:'day', has_notification:false, has_open_task:false});
      document.getElementById('updated').textContent = '行走动画预览 · 不写入 daemon';
      document.getElementById('gameAction').textContent = '走路 · 书桌';
    }
    async function loadContext() {
      if (loading) return;
      loading = true;
      try {
        const user = document.getElementById('user').value || 'geoff';
        const response = await fetch(`/debug/${user}/context`);
        if (!response.ok) throw new Error(`状态同步失败 (${response.status})`);
        snapshot = await response.json();
        render();
      } catch (error) {
        document.getElementById('updated').textContent = '状态同步失败 · 可稍后重试';
        document.getElementById('gameAction').textContent = '小屋待机中';
        document.getElementById('sceneActivity').textContent = '暂时无法读取 daemon 状态。';
        applyScene({location:'rug', action:'idle', expression:'neutral', time_of_day:'day', has_notification:false, has_open_task:false});
      } finally {
        loading = false;
      }
    }
    function render() {
      const d = snapshot.dashboard, scene = d.scene, runtime = snapshot.life_runtime;
      document.getElementById('updated').textContent = '刚刚同步';
      document.getElementById('gameAction').textContent = `${labels[scene.action]} · ${labels[scene.location]}`;
      document.getElementById('sceneActivity').textContent = d.activity;
      document.getElementById('sceneDetail').textContent = `${d.phone_label}；本段 ${fmtTime(runtime.started_at)} - ${fmtTime(runtime.ends_at)}`;
      document.getElementById('sceneTag').textContent = d.mood_label;
      document.getElementById('sceneLocation').textContent = `地点：${labels[scene.location]}`;
      document.getElementById('sceneActionKey').textContent = `动作：${labels[scene.action]}`;
      document.getElementById('sceneMood').textContent = `表情：${expressionLabels[scene.expression] || scene.expression}`;
      document.getElementById('scenePhone').textContent = `手机：${d.phone_label}`;
      document.getElementById('attention').textContent = `${d.attention}%`;
      document.getElementById('taskCount').textContent = d.active_task_count;
      document.getElementById('phoneState').textContent = d.phone_label;
      document.getElementById('reasons').innerHTML = d.reasons.map(x => `<li>${esc(x)}</li>`).join('');
      document.getElementById('timeline').innerHTML = d.next_plan.map((p,i) => `<div class="timeline-item ${i === 0 ? 'current' : ''}"><i class="dot"></i><div class="timeline-copy"><strong>${esc(p.activity)}</strong><span>${p.adjustment_note ? esc(p.adjustment_note) : (p.interruptible ? '偶尔会看手机' : '不适合被打断')}</span></div><time>${fmtTime(p.starts_at)}</time></div>`).join('') || '<span>今天还没有后续安排。</span>';
      renderCalendar();
      const tasks = snapshot.recent_social_tasks.filter(t => ['pending','claimed'].includes(t.status));
      document.getElementById('tasks').innerHTML = tasks.length ? tasks.map(t => `<div class="task">${esc(t.reason)}<small>${esc(t.status)} · 到 ${fmtTime(t.due_at)}</small></div>`).join('') : '<span class="result">没有挂起的社交事务。</span>';
      document.getElementById('state').textContent = JSON.stringify({life_runtime:runtime, scene, state:snapshot.state}, null, 2);
      applyScene(scene);
    }
    function renderCalendar() {
      const days = snapshot.calendar?.days || [];
      if (!days.length) return;
      if (!selectedCalendarDate || !days.some(day => day.date === selectedCalendarDate)) selectedCalendarDate = days.find(day => day.relative === '今天')?.date || days[0].date;
      document.getElementById('calendarDays').innerHTML = days.map(day => {
        const count = (day.special_events || []).length + (day.events || []).length + (day.plans || []).length;
        const state = `${day.date === selectedCalendarDate ? ' selected' : ''}${day.relative === '今天' ? ' today' : ''}`;
        return `<button class="${state.trim()}" data-date="${esc(day.date)}"><strong>${esc(day.date.slice(5))}</strong><small>${esc(day.relative)}${count ? ` · ${count} 项` : ''}</small></button>`;
      }).join('');
      const day = days.find(item => item.date === selectedCalendarDate) || days[0];
      const special = (day.special_events || []).map(event => ({event, kind:'日历事件'}));
      const plans = (day.plans || []).map(event => ({event:{...event,title:event.activity,details:event.adjustment_note}, kind:'日程'}));
      const lived = (day.events || []).map(event => ({event:{...event,title:event.content,details:event.content}, kind:'生活记录'}));
      const rows = [...special, ...plans, ...lived].sort((a,b) => new Date(a.event.starts_at) - new Date(b.event.starts_at));
      const label = status => ({planned:'计划中',active:'进行中',completed:'已发生',cancelled:'已取消',postponed:'已推迟'})[status] || status;
      document.getElementById('calendar').innerHTML = rows.map(({event,kind}) => {
        const note = event.memory_content || event.details || event.memory_note || '没有额外说明';
        const reason = event.changed_reason ? `；原因：${event.changed_reason}` : '';
        const linked = event.memory_id ? ' · 已关联记忆' : '';
        return `<article class="calendar-row"><time>${esc(fmtRange(event))}<br>${esc(kind)}</time><div><strong>${esc(event.title)}</strong><small>${esc(note + reason + linked)}</small></div><small>${esc(label(event.status))}</small></article>`;
      }).join('') || '<span class="result">这一天没有计划或已发生记录。</span>';
    }
    document.getElementById('calendarDays').onclick = event => {
      const button = event.target.closest('button[data-date]');
      if (!button) return;
      selectedCalendarDate = button.dataset.date;
      renderCalendar();
    };
    async function runProactive() {
      const user = document.getElementById('user').value || 'geoff';
      const res = await fetch(`/proactive/${user}`, {method:'POST'}).then(r => r.json());
      document.getElementById('result').textContent = res.should_send ? '她有一点想说的话，正在走投递流程。' : '这会儿她决定先不打扰。';
      await loadContext();
    }
    init();
  </script>
</body>
</html>"""

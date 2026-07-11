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
      room:'/assets/dashboard/zhizhi-q-home-evening-v1.png',
      sprite:'/assets/dashboard/zhizhi-q-sprite-sheet-v1.png'
    };
    const sceneDefinitions = {
      'free-bedroom': {
        source:'知栀 · Q 版雨夜小屋 · 项目视觉母版',
        background:'room',
        anchors:{
          desk:[520,505], kitchen:[635,520], entry:[1490,570], sofa:[1010,700],
          vanity:[1240,470], bed:[930,500], window:[485,300], rug:[900,655]
        },
        routes:{
          desk:['rug','window'], kitchen:['rug'], entry:['rug'], sofa:['rug'],
          vanity:['rug','bed'], bed:['vanity','rug'], window:['desk','rug'],
          rug:['desk','kitchen','entry','sofa','vanity','bed','window']
        },
        foreground:{
          desk:[282,438,420,126],
          sofa:[720,620,500,230]
        }
      }
    };
    let activeScene = sceneDefinitions['free-bedroom'];
    let anchors = activeScene.anchors;
    let routeGraph = activeScene.routes;
    let snapshot = null;
    let loading = false;
    let selectedCalendarDate = null;
    const actor = {anchor:'rug', pos:[...anchors.rug], path:[], action:'idle', expression:'neutral', scene:null, direction:'front', lastTime:0, blinkUntil:0};

    function preload() {
      return Promise.all(Object.entries(imagePaths).map(([key,path]) => new Promise(resolve => {
        const img = new Image();
        img.onload = () => { images[key] = img; resolve(); };
        img.onerror = resolve;
        img.src = path;
      })));
    }
    function project(point) {
      return [stage.ox + point[0] * stage.scale, stage.oy + point[1] * stage.scale];
    }
    function distance(a,b) {
      const dx = a[0] - b[0], dy = a[1] - b[1];
      return Math.hypot(dx, dy);
    }
    function nearestAnchor(point) {
      return Object.entries(anchors).sort((a,b) => distance(point,a[1]) - distance(point,b[1]))[0][0];
    }
    function pathfind(startAnchor, targetAnchor) {
      if (startAnchor === targetAnchor) return [];
      const queue = [startAnchor];
      const came = new Map([[startAnchor, null]]);
      for (let i = 0; i < queue.length; i++) {
        const node = queue[i];
        if (node === targetAnchor) break;
        for (const next of routeGraph[node] || []) {
          if (!came.has(next)) {
            came.set(next, node);
            queue.push(next);
          }
        }
      }
      if (!came.has(targetAnchor)) return [anchors[targetAnchor]];
      const names = [];
      let cur = targetAnchor;
      while (came.get(cur)) {
        names.unshift(cur);
        cur = came.get(cur);
      }
      return names.map(name => anchors[name]);
    }
    function activateScene(scene) {
      const nextScene = sceneDefinitions[scene.scene_id] || sceneDefinitions['free-bedroom'];
      if (nextScene === activeScene) return;
      activeScene = nextScene;
      anchors = activeScene.anchors;
      routeGraph = activeScene.routes;
      actor.anchor = 'entry' in anchors ? 'entry' : 'rug';
      actor.pos = [...anchors[actor.anchor]];
      actor.path = [];
    }
    function applyScene(scene) {
      activateScene(scene);
      const target = scene.location in anchors ? scene.location : 'rug';
      actor.scene = scene;
      actor.expression = scene.expression;
      if (actor.action !== scene.action || actor.path.length || actor.anchor !== target) {
        actor.anchor = nearestAnchor(actor.pos);
        actor.path = pathfind(actor.anchor, target);
        actor.action = actor.path.length ? 'walk' : scene.action;
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
      return ['walk','walk_out','tidy'].includes(actor.action) ? 'walk' : 'idle';
    }
    function drawActor(ctx, now) {
      if (actor.action === 'sleep') { drawSleep(ctx, now); return; }
      const action = characterAction();
      const sheet = images.sprite;
      if (!sheet) return;
      const [px, py] = project(actor.pos);
      const cells = action === 'walk'
        ? ({left:[1,0], right:[3,1], front:[1,0]})[actor.direction] || [1,0]
        : ({left:[2,0], right:[2,1], front:[0,0]})[actor.direction] || [0,0];
      const sx = cells[0] * 443, sy = cells[1] * 443;
      const dh = 128, dw = 128;
      const x = px - dw / 2, y = py - dh + 7;
      ctx.save();
      ctx.imageSmoothingEnabled = false;
      ctx.shadowColor = 'rgba(34, 25, 20, .35)';
      ctx.shadowBlur = 0;
      ctx.shadowOffsetY = 3;
      ctx.drawImage(sheet, sx, sy, 443, 443, x, y, dw, dh);
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
      const [px,py] = project(actor.pos);
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
      const target = anchors[scene.location] || anchors.rug;
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
      const [x,y] = project(anchors[scene.location] || anchors.rug);
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
      const scene = actor.scene || {};
      const crop = activeScene.foreground?.[scene.location];
      const background = images[activeScene.background];
      if (!crop || !background) return;
      const [sx,sy,sw,sh] = crop;
      ctx.save();
      ctx.imageSmoothingEnabled = false;
      ctx.drawImage(background, sx, sy, sw, sh, stage.ox + sx * stage.scale, stage.oy + sy * stage.scale, sw * stage.scale, sh * stage.scale);
      ctx.restore();
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
        const dx = target[0] - actor.pos[0], dy = target[1] - actor.pos[1];
        const dist = Math.hypot(dx, dy);
        const speed = 210;
        if (dist <= speed * dt) {
          actor.pos = target;
          actor.anchor = nearestAnchor(actor.pos);
          actor.path.shift();
          if (!actor.path.length) actor.action = actor.scene?.action || 'idle';
        } else {
          actor.pos = [actor.pos[0] + dx / dist * speed * dt, actor.pos[1] + dy / dist * speed * dt];
          actor.direction = dx < -8 ? 'left' : dx > 8 ? 'right' : 'front';
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
      setInterval(loadContext, 20000);
      requestAnimationFrame(loop);
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

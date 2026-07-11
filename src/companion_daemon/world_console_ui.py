"""Standalone operator console for the event-sourced virtual world.

It intentionally does not share the pixel-room renderer.  The room is a
separate visual projection maintained independently; this console is for
auditing and submitting explicit world commands.
"""

WORLD_CONSOLE_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>世界控制台 · 知栀</title>
  <style>
    :root { --ink:#282a36; --paper:#f7f5ef; --line:#c9c3b5; --blue:#355c7d; --green:#41735d; --orange:#9c5939; --red:#9b4242; --muted:#68655e; font-family:"PingFang SC",system-ui,sans-serif; color:var(--ink); background:#ebe9e1; }
    * { box-sizing:border-box; } body { margin:0; } button,input,select { font:inherit; }
    header { padding:20px clamp(18px,4vw,54px); color:#fff; background:#293743; display:flex; justify-content:space-between; align-items:center; gap:16px; }
    h1 { margin:0; font-size:20px; font-weight:650; } header p { margin:5px 0 0; color:#d5e2e5; font-size:13px; } a { color:inherit; }
    main { max-width:1440px; margin:0 auto; padding:22px; display:grid; gap:16px; }
    .card { padding:17px; background:var(--paper); border:1px solid var(--line); border-radius:10px; box-shadow:0 2px 10px #211f1912; }
    .hero { display:grid; grid-template-columns:1.4fr .9fr; gap:16px; } h2 { margin:0 0 12px; font-size:15px; } h3 { margin:0 0 8px; font-size:13px; }
    .meta { color:var(--muted); font-size:12px; } .status { padding:3px 7px; border-radius:999px; font-size:12px; background:#e2e0d9; } .status.good { color:#18583d; background:#d8ecdf; } .status.warn { color:#8a451d; background:#f5dfc9; } .status.bad { color:#7a2525; background:#f1d4d4; }
    .clock { display:flex; align-items:baseline; gap:10px; flex-wrap:wrap; } .clock b { font-size:23px; letter-spacing:.3px; } .clock small { color:var(--muted); }
    .controls { display:flex; gap:8px; flex-wrap:wrap; align-items:end; } label { display:grid; gap:4px; color:var(--muted); font-size:12px; } select,input { min-height:36px; padding:7px 9px; color:var(--ink); background:#fff; border:1px solid var(--line); border-radius:6px; }
    button { min-height:36px; padding:7px 11px; color:#fff; background:var(--blue); border:0; border-radius:6px; cursor:pointer; } button:hover { filter:brightness(1.08); } button.secondary { color:var(--ink); background:#dce4e4; } button.danger { background:var(--red); } button:disabled { opacity:.55; cursor:wait; }
    .grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:16px; } .three { grid-template-columns:repeat(3,minmax(0,1fr)); }
    .need { display:grid; grid-template-columns:1fr auto; gap:4px 10px; padding:8px 0; border-bottom:1px solid #e2ded4; font-size:13px; } .need:last-child { border-bottom:0; }
    .bar { grid-column:1/-1; height:5px; background:#e1dfd8; border-radius:4px; overflow:hidden; } .bar i { display:block; height:100%; background:var(--green); }
    .list { margin:0; padding:0; list-style:none; } .row { padding:10px 0; border-bottom:1px solid #e1ddd3; display:grid; gap:4px; } .row:last-child { border-bottom:0; } .row-head { display:flex; gap:8px; justify-content:space-between; align-items:center; } .row strong { font-size:13px; } .row small { color:var(--muted); font-size:12px; } .progress { height:6px; background:#e1ddd3; border-radius:4px; overflow:hidden; } .progress i { display:block; height:100%; background:var(--blue); }
    .timeline { max-height:420px; overflow:auto; } .event { display:grid; grid-template-columns:54px 1fr; gap:9px; padding:8px 0; border-bottom:1px solid #e1ddd3; } .event code { color:var(--blue); font-size:11px; } .event time { color:var(--muted); font-size:11px; }
    .audit { display:flex; align-items:center; gap:9px; flex-wrap:wrap; } .notice { margin:0; color:var(--muted); font-size:12px; line-height:1.5; } .error { color:#8d3030; } .hidden { display:none !important; }
    .action-tools { display:flex; gap:6px; flex-wrap:wrap; align-items:center; margin-top:4px; } .action-tools input { min-height:30px; padding:4px 7px; font-size:12px; } .action-tools button { min-height:30px; padding:4px 7px; font-size:12px; }
    @media (max-width:900px) { .hero,.grid,.three { grid-template-columns:1fr; } header { align-items:flex-start; flex-direction:column; } }
  </style>
</head>
<body>
  <header><div><h1>世界控制台</h1><p>只读账本投影 + 显式世界命令；不直接编辑状态。</p></div><a href="/dashboard">返回小屋面板</a></header>
  <main id="console" class="hidden">
    <section class="hero">
      <article class="card"><h2>逻辑时间</h2><div class="clock"><b id="logicalAt">--</b><span id="clockStatus" class="status">--</span></div><p id="worldMeta" class="meta"></p><div class="controls"><label>时钟模式<select id="clockMode"><option value="paused:0">暂停</option><option value="realtime:1">实时 1×</option><option value="accelerated:2">加速 2×</option><option value="accelerated:4">加速 4×</option><option value="accelerated:8">加速 8×</option></select></label><button onclick="applyClock()">更新模式</button><label>推进至<input id="advanceTarget" type="datetime-local" step="60" /></label><button onclick="advanceClock()">推进世界</button></div></article>
      <article class="card"><h2>审计门禁</h2><div class="audit"><span id="auditStatus" class="status">尚未检查</span><button class="secondary" onclick="runAudit()">重建并审计</button></div><p id="auditDetail" class="notice">审计会从账本重建所有投影；不会生成生活事件或发送消息。</p></article>
    </section>
    <section class="grid three"><article class="card"><h2>角色资源</h2><div id="needs"></div></article><article class="card"><h2>长期目标</h2><ul id="goals" class="list"></ul></article><article class="card"><h2>进行中行动</h2><ul id="actions" class="list"></ul></article></section>
    <section class="grid"><article class="card"><h2>日程与活动</h2><ul id="agenda" class="list"></ul></article><article class="card"><h2>已提交经历</h2><ul id="experiences" class="list"></ul></article></section>
    <section class="card"><h2>最近账本事件</h2><div id="timeline" class="timeline"></div></section>
  </main>
  <main id="disabled"><section class="card"><h2>世界运行时未启用</h2><p class="notice">当前 daemon 使用旧运行时，或尚未为它配置世界纪元。控制台不会创建隐式世界。</p></section></main>
  <script>
    let overview = null;
    const $ = id => document.getElementById(id);
    const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
    const displayAt = value => value ? value.replace('T',' ').replace(/([+-]\d\d:\d\d|Z)$/,'') : '—';
    const status = value => `<span class="status ${['unknown','failed','cancelled','expired'].includes(value) ? 'warn' : ['active','completed','delivered','scheduled','sending'].includes(value) ? 'good' : ''}">${esc(value || 'unknown')}</span>`;
    function renderList(id, rows, render, empty) { $(id).innerHTML = rows.length ? rows.map(render).join('') : `<li class="row"><small>${empty}</small></li>`; }
    function render(data) {
      overview = data; $('disabled').classList.add('hidden'); $('console').classList.remove('hidden');
      $('logicalAt').textContent = displayAt(data.clock.logical_at); $('clockStatus').textContent = `${data.clock.mode} · ${data.clock.rate}×`;
      $('worldMeta').textContent = `${data.protagonist.name || '角色'} · ${data.protagonist.location || '未知地点'} · revision ${data.revision} · ${data.state_hash.slice(0,12)}`;
      $('clockMode').value = `${data.clock.mode}:${data.clock.rate}`; $('advanceTarget').value = data.clock.logical_at.slice(0,16);
      $('needs').innerHTML = Object.entries(data.needs).map(([key,value]) => `<div class="need"><span>${esc(key)}</span><b>${esc(value)}</b><div class="bar"><i style="width:${Math.max(0,Math.min(100,Number(value)))}%"></i></div></div>`).join('');
      renderList('goals', data.goals, goal => { const percent = goal.target ? Math.round(goal.progress / goal.target * 100) : 0; return `<li class="row"><div class="row-head"><strong>${esc(goal.title)}</strong>${status(goal.status)}</div><small>${goal.progress}/${goal.target} · 截止 ${displayAt(goal.deadline)}${goal.next_review_at ? ` · 复核 ${displayAt(goal.next_review_at)}` : ''}</small><div class="progress"><i style="width:${Math.max(0,Math.min(100,percent))}%"></i></div></li>`; }, '没有长期目标');
      renderList('agenda', data.agenda, item => `<li class="row"><div class="row-head"><strong>${esc(item.title)}</strong>${status(item.status)}</div><small>${displayAt(item.starts_at)} — ${displayAt(item.ends_at)} · ${esc(item.location || '地点未设定')}${item.reason ? ` · ${esc(item.reason)}` : ''}</small></li>`, '暂无日程');
      renderList('experiences', data.experiences, item => `<li class="row"><div class="row-head"><strong>${esc(item.content)}</strong>${item.shared ? '<span class="status good">已分享</span>' : '<span class="status">未分享</span>'}</div><small>${displayAt(item.occurred_at)} · ${esc(item.experience_id)}</small></li>`, '尚无可引用经历');
      renderList('actions', data.actions, action => { const uncertainty = action.status === 'unknown' ? '<small class="error">等待受信任适配器提供可验证回执；控制台不能人工宣称送达。</small>' : ''; return `<li class="row"><div class="row-head"><strong>${esc(action.kind || action.message_kind || action.action_id)}</strong>${status(action.status)}</div><small>${esc(action.action_id)} · 截止 ${displayAt(action.expires_at)}${action.reason ? ` · ${esc(action.reason)}` : ''}</small>${uncertainty}</li>`; }, '没有行动记录');
      $('timeline').innerHTML = data.timeline.length ? data.timeline.map(event => `<div class="event"><code>#${event.revision}</code><div><strong>${esc(event.event_type)}</strong>${event.subject ? ` · ${esc(event.subject)}` : ''}<br /><time>${displayAt(event.logical_at)}</time></div></div>`).join('') : '<p class="notice">账本为空。</p>';
    }
    async function load() { try { const response = await fetch('/world-runtime/overview'); const data = await response.json(); if (!response.ok) throw new Error(data.detail || '读取失败'); if (!data.enabled) return; render(data); } catch (error) { $('disabled').classList.remove('hidden'); $('disabled').innerHTML = `<section class="card"><h2>控制台无法读取世界</h2><p class="notice error">${esc(error.message)}</p></section>`; } }
    async function mutate(url, body) { const response = await fetch(url, {method:'POST', headers:{'content-type':'application/json'}, body:JSON.stringify(body)}); const data = await response.json(); if (!response.ok) throw new Error(data.detail || '命令被拒绝'); await load(); return data; }
    async function applyClock() { if (!overview) return; const [mode,rate] = $('clockMode').value.split(':'); try { await mutate(`/world/${overview.world_id}/commands`, {expected_revision:overview.revision, command:{type:'set_clock_mode',mode,rate:Number(rate)}}); } catch (error) { alert(error.message); } }
    function worldOffset() { const match = overview.clock.logical_at.match(/([+-]\d\d:\d\d|Z)$/); return match ? (match[1] === 'Z' ? '+00:00' : match[1]) : '+08:00'; }
    async function advanceClock() { if (!overview || !$('advanceTarget').value) return; try { await mutate(`/world/${overview.world_id}/advance`, {expected_revision:overview.revision, target_logical_at:`${$('advanceTarget').value}:00${worldOffset()}`}); } catch (error) { alert(error.message); } }
    async function runAudit() { const label = $('auditStatus'); label.textContent = '审计中'; label.className = 'status'; try { const response = await fetch('/world-runtime/enablement'); const data = await response.json(); if (!response.ok) throw new Error(data.detail || '审计失败'); label.textContent = data.ready ? '允许启用' : '暂不允许启用'; label.className = `status ${data.ready ? 'good' : 'warn'}`; $('auditDetail').textContent = `开放行动：${data.open_action_ids.length}；待外部对账：${data.unknown_action_ids.length}；投递回执查询：${data.delivery_receipts_supported ? '支持' : '不支持'}。`; await load(); } catch (error) { label.textContent = '审计失败'; label.className = 'status bad'; $('auditDetail').textContent = error.message; } }
    load();
  </script>
</body>
</html>"""

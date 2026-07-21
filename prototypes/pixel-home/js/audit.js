'use strict';

// Audit gallery: renders zoomed-in crops of every interaction scenario so the
// pixel art can be inspected closely in a single page/screenshot.

(async function () {
  await Promise.all([loadSpriteOverrides(), ActorSprites.loadOverrides()]);
  const SCENARIOS = [
    { name: 'eat', label: '吃饭（坐姿+遮挡+蒸汽）' },
    { name: 'study', label: '用电脑（工作位坐姿）' },
    { name: 'sleep', label: '睡觉（床+呼吸起伏）' },
    { name: 'relax', label: '窝沙发' },
    { name: 'phone', label: '刷手机' },
    { name: 'cook', label: '做饭（站姿+蒸汽）' },
    { name: 'wash', label: '洗碗' },
    { name: 'browse', label: '书架找书' },
    { name: 'dress', label: '衣柜挑衣服' },
    { name: 'snack', label: '翻冰箱' },
    { name: 'water', label: '浇水' },
    { name: 'walk-downLeft', label: '走路（朝下左）', walk: 'downLeft' },
    { name: 'walk-downRight', label: '走路（朝下右）', walk: 'downRight' },
    { name: 'walk-upLeft', label: '走路（朝上左）', walk: 'upLeft' },
    { name: 'walk-upRight', label: '走路（朝上右）', walk: 'upRight' },
    { name: 'idle', label: '站立发呆', idle: true },
    // regression: standing in the gap between plantBig (NW of her, must be
    // occluded) and the sofa (SE of her, must occlude her shoulder)
    { name: 'between', label: '夹缝站位（前遮后挡）', idle: true, pos: [5.5, 4.5] },
  ];

  const ZOOM = 4;           // crop zoom factor
  const CROP = 110;         // native px square around the actor

  const gallery = document.getElementById('gallery');

  for (const scenario of SCENARIOS) {
    const host = document.createElement('canvas');
    host.width = 1400; host.height = 920;  // detached canvas -> scale 3
    const engine = new Engine(host, {});
    engine.autoLife = false;
    engine.clock = 12 * 3600;              // neutral daylight

    if (scenario.walk) {
      engine.actor.state = 'walk';
      engine.actor.facing = scenario.walk;
      engine.actor.walked = 0.35;          // mid-step frame
      engine.actor.pos = [4, 5];
      engine.actor.path = [[4, 5]];        // keeps walk state on render
    } else if (scenario.idle) {
      engine.actor.pos = scenario.pos || [4, 5];
    } else {
      const it = engine.interactions[scenario.name];
      if (!it) continue;
      engine.actor.pos = [...it.approach];   // as if she had walked there
      engine.beginActivity(it);
    }

    // a couple of warm-up frames so effects (steam etc.) exist
    for (let i = 0; i < 14; i += 1) {
      engine.spawnEffects(1000 + i * 300);
      engine.render(1000 + i * 300, 0.3);
    }

    // debug marker: red crosshair at the actor's grid position projected to floor
    if (new URLSearchParams(location.search).has('debug')) {
      const hctx = host.getContext('2d');
      hctx.setTransform(engine.scale, 0, 0, engine.scale, engine.offX, engine.offY);
      const [dwx, dwy] = engine.actorWorldPos();
      const [mx, my] = engine.project(dwx, dwy, 0);
      hctx.strokeStyle = '#ff2b2b'; hctx.lineWidth = 1;
      hctx.beginPath(); hctx.moveTo(mx - 6, my); hctx.lineTo(mx + 6, my); hctx.moveTo(mx, my - 6); hctx.lineTo(mx, my + 6); hctx.stroke();
      hctx.setTransform(1, 0, 0, 1, 0, 0);
    }

    // crop around the actor, zoomed
    const [wx, wy] = engine.actorWorldPos();
    const [ax, ay] = engine.project(wx, wy, engine.actor.z);
    const sx = (ax - CROP / 2) * engine.scale + engine.offX;
    const sy = (ay - CROP * 0.62) * engine.scale + engine.offY;
    (window.__auditDebug = window.__auditDebug || []).push({
      name: scenario.name, sx, sy, scale: engine.scale, offX: engine.offX, offY: engine.offY,
      hostW: host.width, hostH: host.height,
      hostSample: [...host.getContext('2d').getImageData(Math.max(0, Math.round(sx + CROP * engine.scale / 2)), Math.max(0, Math.round(sy + CROP * engine.scale / 2)), 1, 1).data],
    });
    const out = document.createElement('canvas');
    out.width = CROP * ZOOM; out.height = CROP * ZOOM;
    const octx = out.getContext('2d');
    octx.imageSmoothingEnabled = false;
    octx.drawImage(host, sx, sy, CROP * engine.scale, CROP * engine.scale, 0, 0, out.width, out.height);

    const fig = document.createElement('figure');
    fig.appendChild(out);
    const cap = document.createElement('figcaption');
    cap.textContent = scenario.label;
    fig.appendChild(cap);
    gallery.appendChild(fig);
  }
})();

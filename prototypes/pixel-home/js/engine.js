'use strict';

// ---------------------------------------------------------------------------
// Engine: scene state, pathfinding, depth-sorted rendering, warm lighting,
// day/night, autonomous life schedule, click-to-walk and interactions.
// ---------------------------------------------------------------------------

// --- furniture catalog -----------------------------------------------------
// Interactions are declared relative to the furniture origin tile.
// approach: walkable tile the actor paths to.  at: exact visual point the
// body occupies during the activity (null = stand at approach).  z: lift.

const CATALOG = {
  bed: {
    name: '床', bake: () => Bakery.bakeBed(), w: 2, d: 3,
    interactions: {
      sleep: { label: '睡觉', approach: [0, 3], at: [0.55, 0.62], z: 0.62, facing: 'upLeft', pose: 'sleep', effect: 'zzz', lumpAt: [0.8, 1.55], lumpAxis: 'y' },
    },
  },
  desk: {
    name: '书桌工作位', bake: () => Bakery.bakeDeskSet(), w: 2, d: 2,
    interactions: {
      study: { label: '用电脑', approach: [2, 1], at: [1.0, 1.55], z: 0.55, facing: 'upRight', pose: 'sit', effect: 'keys' },
    },
  },
  dining: {
    name: '餐桌椅', bake: () => Bakery.bakeDiningSet(), w: 2, d: 2,
    interactions: {
      eat: { label: '吃饭', approach: [2, 1], at: [1.0, 1.56], z: 0.52, facing: 'upRight', pose: 'sit', effect: 'steam', effectAt: [1.0, 0.7, 0.85] },
    },
  },
  chair: {
    name: '椅子', bake: () => Bakery.bakeChair(), w: 1, d: 1,
    interactions: {
      sit: { label: '坐下', approach: [0, 1], at: [0.5, 0.5], z: 0.52, facing: 'downLeft', pose: 'sit' },
    },
  },
  stool: {
    name: '小凳', bake: () => Bakery.bakeStool(), w: 1, d: 1,
    interactions: {
      sit: { label: '坐一会', approach: [0, 1], at: [0.5, 0.5], z: 0.45, facing: 'downLeft', pose: 'sit' },
    },
  },
  bookcase: {
    name: '书架', bake: () => Bakery.bakeBookcase(), w: 1, d: 2,
    interactions: {
      browse: { label: '找书', approach: [1, 0], at: null, z: 0, facing: 'upLeft', pose: 'stand', effect: 'focus' },
    },
  },
  lowshelf: {
    name: '矮书柜', bake: () => Bakery.bakeLowShelf(), w: 2, d: 1,
    interactions: {
      tidy: { label: '整理', approach: [0, 1], at: null, z: 0, facing: 'upRight', pose: 'stand', effect: 'sparkle', effectAt: [1.0, 0.4, 1.0] },
    },
  },
  sofa: {
    name: '沙发', bake: () => Bakery.bakeSofa(), w: 2, d: 1,
    interactions: {
      relax: { label: '窝着休息', approach: [2, 0], at: [1.42, 0.48], z: 0.55, facing: 'downLeft', pose: 'sit', effect: 'music' },
      phone: { label: '刷手机', approach: [-1, 0], at: [1.42, 0.48], z: 0.55, facing: 'downLeft', pose: 'sit', effect: 'phone' },
    },
  },
  coffeeTable: { name: '茶几', bake: () => Bakery.bakeCoffeeTable(), w: 2, d: 1, interactions: {} },
  kitchen: {
    name: '料理台', bake: () => Bakery.bakeKitchen(), w: 3, d: 1,
    interactions: {
      cook: { label: '做饭', approach: [0, 1], at: null, z: 0, facing: 'upRight', pose: 'stand', effect: 'steam', effectAt: [1.05, 0.62, 1.05] },
      wash: { label: '洗碗', approach: [2, 1], at: null, z: 0, facing: 'upRight', pose: 'stand', effect: 'water', effectAt: [2.45, 0.5, 1.05] },
    },
  },
  fridge: {
    name: '冰箱', bake: () => Bakery.bakeFridge(), w: 1, d: 1,
    interactions: {
      snack: { label: '找吃的', approach: [0, 1], at: null, z: 0, facing: 'upRight', pose: 'stand', effect: 'chill', effectAt: [0.5, 1.0, 1.2] },
    },
  },
  wardrobe: {
    name: '衣柜', bake: () => Bakery.bakeWardrobe(), w: 1, d: 2,
    interactions: {
      dress: { label: '挑衣服', approach: [0, 2], at: null, z: 0, facing: 'upRight', pose: 'stand', effect: 'sparkle', effectAt: [0.5, 1.95, 1.4] },
    },
  },
  nightstand: { name: '床头柜', bake: () => Bakery.bakeNightstand(), w: 1, d: 1, interactions: {} },
  plantBig: {
    name: '大绿植', bake: () => Bakery.bakePlantBig(), w: 1, d: 1,
    interactions: {
      water: { label: '浇水', approach: [-1, 0], at: null, z: 0, facing: 'downRight', pose: 'stand', effect: 'water', effectAt: [0.5, 0.5, 1.2] },
    },
  },
  plantSmall: {
    name: '小绿植', bake: () => Bakery.bakePlantSmall(), w: 1, d: 1,
    interactions: {
      water: { label: '浇水', approach: [-1, 0], at: null, z: 0, facing: 'downRight', pose: 'stand', effect: 'water', effectAt: [0.5, 0.5, 0.9] },
    },
  },
  floorLamp: { name: '落地灯', bake: () => Bakery.bakeFloorLamp(), w: 1, d: 1, interactions: {} },
  pendant: { name: '吊灯', bake: () => Bakery.bakePendant(), w: 1, d: 1, decor: true, interactions: {} },
};

// --- default room layout ----------------------------------------------------

const DEFAULT_LAYOUT = {
  grid: { w: 12, d: 10 },
  furniture: [
    { type: 'kitchen', x: 1, y: 0 },
    { type: 'fridge', x: 4, y: 0 },
    { type: 'bookcase', x: 0, y: 1 },
    { type: 'bed', x: 9, y: 0 },
    { type: 'nightstand', x: 11, y: 0 },
    { type: 'lowshelf', x: 7, y: 2 },
    { type: 'dining', x: 4, y: 3 },
    { type: 'pendant', x: 5, y: 3 },
    { type: 'desk', x: 0, y: 4 },
    { type: 'wardrobe', x: 11, y: 5 },
    { type: 'sofa', x: 3, y: 6 },
    { type: 'coffeeTable', x: 3, y: 7 },
    { type: 'floorLamp', x: 6, y: 7 },
    { type: 'plantBig', x: 8, y: 8 },
    { type: 'plantSmall', x: 1, y: 9 },
    { type: 'stool', x: 5, y: 8 },
  ],
  zones: [{ x: 0, y: 0, w: 5, d: 2, material: 'tile' }],
  rugs: [
    { x: 2, y: 6, w: 4, d: 3, inner: '#a9c4b0', border: '#6f9282' },
    { x: 4, y: 2, w: 3, d: 3, inner: '#88a7ad', border: '#5d7f88' },
    { x: 8, y: 4, w: 2, d: 1, inner: '#7f9a92', border: '#5a7570' },
  ],
  wallDecor: [
    { type: 'backsplash', side: 'NE', t: 0.95, len: 3.1 },
    { type: 'upperCabinet', side: 'NE', t: 1.0 },
    { type: 'window', side: 'NE', t: 8.85 },
    { type: 'stringLights', side: 'NE', t: 4.5, len: 4.1, seed: 3 },
    { type: 'clock', side: 'NE', t: 6.6 },
    { type: 'picture', side: 'NE', t: 5.6, seed: 11 },
    { type: 'hangingPlant', side: 'NE', t: 4.6 },
    { type: 'shelf', side: 'NW', t: 1.4 },
    { type: 'picture', side: 'NW', t: 3.35, seed: 8 },
    { type: 'pinboard', side: 'NW', t: 4.15 },
    { type: 'picture', side: 'NW', t: 5.5, seed: 9 },
    { type: 'stringLights', side: 'NW', t: 0.9, len: 5.4, seed: 4 },
    { type: 'door', side: 'NW', t: 7.7 },
    { type: 'hangingPlant', side: 'NW', t: 6.6 },
  ],
  entry: [1, 8],
};

// Schedule of a small domestic life (game hours).
const SCHEDULE = [
  { h: 7.0, act: 'sleep~wake' },
  { h: 7.2, act: 'dress' },
  { h: 7.6, act: 'cook' },
  { h: 8.1, act: 'eat' },
  { h: 8.8, act: 'wash' },
  { h: 9.2, act: 'study' },
  { h: 11.6, act: 'water' },
  { h: 12.0, act: 'cook' },
  { h: 12.5, act: 'eat' },
  { h: 13.1, act: 'browse' },
  { h: 13.5, act: 'relax' },
  { h: 15.0, act: 'study' },
  { h: 17.4, act: 'tidy' },
  { h: 18.0, act: 'cook' },
  { h: 18.6, act: 'eat' },
  { h: 19.3, act: 'wash' },
  { h: 19.8, act: 'phone' },
  { h: 21.4, act: 'snack' },
  { h: 21.8, act: 'relax' },
  { h: 22.6, act: 'sleep' },
];

const ACTIVITY_LABEL = {
  sleep: '睡觉中', 'sleep~wake': '睡觉中', dress: '挑今天的衣服', cook: '做饭',
  eat: '吃饭', wash: '洗碗', study: '用电脑', water: '给植物浇水',
  browse: '在书架前找书', relax: '窝在沙发上', phone: '刷手机', snack: '翻冰箱',
  tidy: '整理柜子', sit: '坐着发呆', walk: '走路', idle: '发呆',
};

const WALL_H = 3.1;

// --- AI sprite overrides -----------------------------------------------------
// assets/ai/manifest.json maps furniture types to pre-rendered pixel sprites
// (route 2: AI-generated art fed through tools/make_sprite.py).  Types without
// an entry keep their procedural bake, so the room degrades gracefully.

const SPRITE_OVERRIDES = {};

async function loadSpriteOverrides(url = 'assets/ai/manifest.json') {
  try {
    const res = await fetch(url, { cache: 'no-store' });
    if (!res.ok) return;
    const manifest = await res.json();
    await Promise.all(Object.entries(manifest).map(([type, meta]) => new Promise(resolve => {
      const img = new Image();
      img.onload = () => {
        const sf = new Surface(img.width, img.height);
        sf.ctx.drawImage(img, 0, 0);
        SPRITE_OVERRIDES[type] = { surface: sf, offset: meta.offset };
        resolve();
      };
      img.onerror = () => resolve();
      img.src = `${meta.url}?t=${Date.now()}`;
    })));
  } catch (e) { /* offline or file:// -> procedural fallback */ }
}

// --- engine -----------------------------------------------------------------

class Engine {
  constructor(canvas, ui) {
    this.canvas = canvas;
    this.ctx = canvas.getContext('2d');
    this.ctx.imageSmoothingEnabled = false;
    this.ui = ui;
    this.sprites = ActorSprites.build();
    this.bakedTypes = {};
    for (const [type, def] of Object.entries(CATALOG)) {
      const baked = def.bake();
      const override = SPRITE_OVERRIDES[type];
      // AI sprites are single-layer: drop the procedural front overlay
      this.bakedTypes[type] = override ? { ...baked, surface: override.surface, offset: override.offset, front: null } : baked;
    }

    this.layout = this.loadLayout() || JSON.parse(JSON.stringify(DEFAULT_LAYOUT));
    this.mode = 'live';
    this.autoLife = true;
    const hourParam = parseFloat(new URLSearchParams(window.location.search).get('hour'));
    this.clock = (Number.isFinite(hourParam) ? ((hourParam % 24) + 24) % 24 : 19.5) * 3600;
    this.timeScale = 60;
    this.lastFrame = 0;
    this.hover = null;
    this.ghost = null;
    this.grabbed = null;
    this.effects = [];
    this.manualUntil = 0;

    this.actor = {
      pos: [...this.layout.entry], z: 0, path: [], facing: 'downLeft',
      state: 'idle', activity: null, walked: 0, pendingAct: null,
    };

    this.rebuild();
    canvas.addEventListener('mousemove', e => this.onMove(e));
    canvas.addEventListener('mouseleave', () => { this.hover = null; });
    canvas.addEventListener('click', e => this.onClick(e));
    canvas.addEventListener('contextmenu', e => { e.preventDefault(); this.onRightClick(e); });
  }

  // --- layout & derived state ----------------------------------------------

  rebuild() {
    const { w, d } = this.layout.grid;
    this.native = { w: (w + d) * HX + 26, h: (w + d) * HY + (WALL_H + 0.9) * HZ + 18 };
    this.origin = [Math.round(this.native.w / 2), Math.round((WALL_H + 0.5) * HZ)];
    this.blocked = new Set();
    this.items = [];
    this.emitters = [];
    for (let i = 0; i < this.layout.furniture.length; i += 1) {
      const f = this.layout.furniture[i];
      const def = CATALOG[f.type], baked = this.bakedTypes[f.type];
      const item = { idx: i, type: f.type, def, baked, x: f.x, y: f.y, w: def.w, d: def.d };
      this.items.push(item);
      if (!def.decor) {
        for (let dx = 0; dx < def.w; dx += 1) for (let dy = 0; dy < def.d; dy += 1) this.blocked.add(`${f.x + dx},${f.y + dy}`);
      }
      for (const em of baked.emitters || []) {
        this.emitters.push({ x: f.x + em.dx, y: f.y + em.dy, z: em.z, r: em.r, color: em.color, pool: em.pool });
      }
    }
    this.interactions = {};
    for (const item of this.items) {
      for (const [key, def] of Object.entries(item.def.interactions || {})) {
        const name = this.interactions[key] ? `${key}@${item.idx}` : key;
        this.interactions[name] = {
          name, key, item, label: def.label,
          approach: [item.x + def.approach[0], item.y + def.approach[1]],
          at: def.at ? [item.x + def.at[0], item.y + def.at[1]] : null,
          z: def.z || 0, facing: def.facing, pose: def.pose || 'stand',
          effect: def.effect || null,
          effectAt: def.effectAt ? [item.x + def.effectAt[0], item.y + def.effectAt[1], def.effectAt[2]] : null,
          lumpAt: def.lumpAt ? [item.x + def.lumpAt[0], item.y + def.lumpAt[1]] : null,
          lumpAxis: def.lumpAxis || 'y',
        };
      }
    }
    this.bakeWindows();
    this.bakeBackground();
    if (this.actor) {
      if (this.actor.activity) { this.actor.activity = null; this.actor.state = 'idle'; this.actor.z = 0; }
      this.actor.path = [];
      this.actor.pendingAct = null;
      const tile = [Math.round(this.actor.pos[0]), Math.round(this.actor.pos[1])];
      if (!this.isWalkable(tile[0], tile[1])) this.actor.pos = [...(this.nearestWalkable(tile) || this.layout.entry)];
      else this.actor.pos = tile;
    }
    if (this.ui.onInteractionsChanged) this.ui.onInteractionsChanged(this.availableInteractions());
  }

  nearestWalkable(from) {
    const { w, d } = this.layout.grid;
    let best = null, bestDist = Infinity;
    for (let y = 0; y < d; y += 1) for (let x = 0; x < w; x += 1) {
      if (!this.isWalkable(x, y)) continue;
      const dist = Math.abs(x - from[0]) + Math.abs(y - from[1]);
      if (dist < bestDist) { best = [x, y]; bestDist = dist; }
    }
    return best;
  }

  availableInteractions() {
    return Object.values(this.interactions).filter(it => this.isWalkable(it.approach[0], it.approach[1]));
  }

  isWalkable(x, y) {
    const { w, d } = this.layout.grid;
    return x >= 0 && y >= 0 && x < w && y < d && !this.blocked.has(`${x},${y}`);
  }

  project(x, y, z = 0) {
    return [this.origin[0] + (x - y) * HX, this.origin[1] + (x + y) * HY - (z || 0) * HZ];
  }

  unproject(px, py) {
    const sx = px - this.origin[0], sy = py - this.origin[1];
    return [(sx / HX + sy / HY) / 2, (sy / HY - sx / HX) / 2];
  }

  zoneAt(gx, gy) {
    for (const z of this.layout.zones || []) {
      if (gx >= z.x && gx < z.x + z.w && gy >= z.y && gy < z.y + z.d) return z.material;
    }
    return 'plank';
  }

  // --- background bake -------------------------------------------------------

  bakeWindows() {
    this.windowSprites = {
      day: Bakery.bakeWindowWall('NE', 'day'),
      dusk: Bakery.bakeWindowWall('NE', 'dusk'),
    };
  }

  bakeWallDecor(entry) {
    switch (entry.type) {
      case 'window': return null; // dynamic, drawn per-frame
      case 'shelf': return Bakery.bakeWallShelf(entry.side);
      case 'picture': return Bakery.bakePicture(entry.side, entry.seed || 8);
      case 'pinboard': return Bakery.bakePinboard(entry.side);
      case 'stringLights': return Bakery.bakeStringLights(entry.side, entry.len || 3, entry.seed || 3);
      case 'hangingPlant': return Bakery.bakeHangingPlant(entry.side);
      case 'upperCabinet': return Bakery.bakeUpperCabinet(entry.side);
      case 'backsplash': return Bakery.bakeBacksplash(entry.side, entry.len || 2);
      case 'clock': return Bakery.bakeWallClock(entry.side);
      case 'door': return Bakery.bakeDoor(entry.side);
      default: return null;
    }
  }

  wallAnchor(side, t, zTop) {
    return side === 'NE' ? this.project(t, 0, zTop) : this.project(0, t, zTop);
  }

  bakeBackground() {
    const { w, d } = this.layout.grid;
    const bg = new Surface(this.native.w, this.native.h);
    const P = (x, y, z = 0) => this.project(x, y, z);

    // plinth: dark platform under the room
    const slab = [P(0, 0, 0), P(w, 0, 0), P(w, d, 0), P(0, d, 0)].map(([x, y]) => [x, y + 7]);
    bg.poly(slab, '#181022');

    // floor tiles by zone
    for (let gy = 0; gy < d; gy += 1) {
      for (let gx = 0; gx < w; gx += 1) {
        const tile = Bakery.bakeFloorTile(gx, gy, this.zoneAt(gx, gy), gy);
        const [px, py] = P(gx, gy);
        bg.blit(tile, px - HX - 1, py - 1);
      }
    }
    // zone seam: brass strip between tile and plank areas
    for (const z of this.layout.zones || []) {
      const y1 = z.y + z.d;
      if (y1 < d) {
        bg.line(...P(z.x, y1), ...P(z.x + z.w, y1), shade(PAL.gold, 0.8));
      }
      const x1 = z.x + z.w;
      if (x1 < w) bg.line(...P(x1, z.y), ...P(x1, z.y + z.d), shade(PAL.gold, 0.8));
    }

    // rugs
    for (const rug of this.layout.rugs || []) {
      const baked = Bakery.bakeRug(rug.w, rug.d, rug.inner, rug.border, (rug.x + 1) * (rug.y + 3));
      const [px, py] = P(rug.x, rug.y);
      bg.blit(baked.surface, px + baked.offset[0], py + baked.offset[1]);
    }

    // floor slab edges
    bg.poly([P(0, d), P(w, d), [P(w, d)[0], P(w, d)[1] + 6], [P(0, d)[0], P(0, d)[1] + 6]], '#493024');
    bg.poly([P(w, 0), P(w, d), [P(w, d)[0], P(w, d)[1] + 6], [P(w, 0)[0], P(w, 0)[1] + 6]], '#38251c');
    bg.line(...P(0, d), ...P(w, d), PAL.outline);
    bg.line(...P(w, 0), ...P(w, d), PAL.outline);

    // walls: outer skins first (they sit behind the interior faces)
    const capD = 0.14;
    const neOut = [P(0, -capD, WALL_H), P(w + capD, -capD, WALL_H), P(w + capD, -capD, -0.1), P(0, -capD, -0.1)];
    bg.poly(neOut, '#241a2e');
    const nwOut = [P(-capD, 0, WALL_H), P(-capD, d + capD, WALL_H), P(-capD, d + capD, -0.1), P(-capD, 0, -0.1)];
    bg.poly(nwOut, '#1e1526');
    const rw = [P(0, 0, WALL_H), P(w, 0, WALL_H), P(w, 0, 0), P(0, 0, 0)];
    bg.poly(rw, PAL.wall);
    const lw = [P(0, 0, WALL_H), P(0, d, WALL_H), P(0, d, 0), P(0, 0, 0)];
    bg.poly(lw, shade(PAL.wall, 0.86));
    // wall vertical gradient: subtly darker near the floor
    for (let band = 0; band < 3; band += 1) {
      const z1 = 0.55 - band * 0.18, z0 = z1 - 0.18;
      bg.poly([P(0, 0, z1), P(w, 0, z1), P(w, 0, Math.max(0, z0)), P(0, 0, Math.max(0, z0))], `rgba(120,84,70,${0.05 + band * 0.03})`);
      bg.poly([P(0, 0, z1), P(0, d, z1), P(0, d, Math.max(0, z0)), P(0, 0, Math.max(0, z0))], `rgba(96,66,60,${0.06 + band * 0.03})`);
    }
    // skirting
    bg.poly([P(0, 0, 0.22), P(w, 0, 0.22), P(w, 0, 0), P(0, 0, 0)], PAL.wallTrim);
    bg.poly([P(0, 0, 0.22), P(0, d, 0.22), P(0, d, 0), P(0, 0, 0)], shade(PAL.wallTrim, 0.82));
    bg.line(...P(0, 0, 0.22), ...P(w, 0, 0.22), shade(PAL.wallTrim, 0.6));
    bg.line(...P(0, 0, 0.22), ...P(0, d, 0.22), shade(PAL.wallTrim, 0.55));
    // picture rail
    bg.line(...P(0, 0, 2.2), ...P(w, 0, 2.2), shade(PAL.wall, 0.86));
    bg.line(...P(0, 0, 2.2), ...P(0, d, 2.2), shade(PAL.wall, 0.78));
    // wall thickness caps: a light top face gives the walls physical depth
    const neCap = [P(0, -capD, WALL_H), P(w + capD, -capD, WALL_H), P(w + capD, 0, WALL_H), P(0, 0, WALL_H)];
    bg.poly(neCap, shade(PAL.wall, 1.12));
    bg.polyLine(neCap, PAL.outline);
    const nwCap = [P(-capD, 0, WALL_H), P(0, 0, WALL_H), P(0, d, WALL_H), P(-capD, d + capD, WALL_H)];
    bg.poly(nwCap, shade(PAL.wall, 1.05));
    bg.polyLine(nwCap, PAL.outline);
    // wall top edges
    bg.line(...P(w, 0, WALL_H), ...P(w, 0, 0), PAL.outline);
    bg.line(...P(0, d, WALL_H), ...P(0, d, 0), PAL.outline);
    bg.line(...P(0, 0, WALL_H), ...P(0, 0, 0), shade(PAL.wall, 0.66));

    // ambient occlusion: floor darkens along the wall bases
    bg.poly([P(0, 0), P(w, 0), P(w, 0.5), P(0, 0.5)].map(p => p), 'rgba(44,26,50,0.16)');
    bg.poly([P(0, 0), P(w, 0), P(w, 0.22), P(0, 0.22)], 'rgba(44,26,50,0.16)');
    bg.poly([P(0, 0), P(0, d), P(0.5, d), P(0.5, 0)], 'rgba(44,26,50,0.18)');
    bg.poly([P(0, 0), P(0, d), P(0.22, d), P(0.22, 0)], 'rgba(44,26,50,0.16)');

    // wall decor
    this.decorGlow = [];
    for (const entry of this.layout.wallDecor || []) {
      if (entry.type === 'window') continue;
      const baked = this.bakeWallDecor(entry);
      if (!baked) continue;
      const zTop = baked.surface.zTop;
      const [ax, ay] = this.wallAnchor(entry.side, entry.t, zTop);
      const bx = ax + baked.offset[0], by = ay + baked.offset[1];
      bg.blit(baked.surface, bx, by);
      for (const g of baked.glowPts || []) {
        this.decorGlow.push({ x: bx - baked.offset[0] + g.x, y: by - baked.offset[1] + g.y, r: g.r, color: g.color });
      }
    }

    this.background = bg;
  }

  // --- persistence ------------------------------------------------------------

  saveLayout() {
    try { localStorage.setItem('pixel-home-layout-v2', JSON.stringify(this.layout)); } catch (e) { /* ignore */ }
  }

  loadLayout() {
    try {
      const raw = localStorage.getItem('pixel-home-layout-v2');
      if (!raw) return null;
      const parsed = JSON.parse(raw);
      if (!parsed.grid || !Array.isArray(parsed.furniture)) return null;
      if (parsed.furniture.some(f => !CATALOG[f.type])) return null;
      return parsed;
    } catch (e) { return null; }
  }

  exportLayout() { return JSON.stringify(this.layout, null, 2); }

  importLayout(text) {
    const parsed = JSON.parse(text);
    if (!parsed.grid || !Array.isArray(parsed.furniture)) throw new Error('缺少 grid/furniture');
    for (const f of parsed.furniture) if (!CATALOG[f.type]) throw new Error(`未知家具类型 ${f.type}`);
    this.layout = parsed;
    this.actor.pos = [...(parsed.entry || [1, 8])];
    this.actor.path = []; this.actor.state = 'idle'; this.actor.activity = null;
    this.rebuild();
    this.saveLayout();
  }

  resetLayout() {
    this.layout = JSON.parse(JSON.stringify(DEFAULT_LAYOUT));
    this.actor.pos = [...this.layout.entry];
    this.actor.path = []; this.actor.state = 'idle'; this.actor.activity = null;
    this.rebuild();
    this.saveLayout();
  }

  // --- pathfinding -------------------------------------------------------------

  pathfind(from, to) {
    const key = (x, y) => `${x},${y}`;
    const start = [Math.round(from[0]), Math.round(from[1])];
    const goal = [Math.round(to[0]), Math.round(to[1])];
    if (!this.isWalkable(goal[0], goal[1])) return null;
    const open = [{ p: start, g: 0, f: 0 }];
    const came = new Map([[key(...start), null]]);
    const gScore = new Map([[key(...start), 0]]);
    const h = p => Math.abs(p[0] - goal[0]) + Math.abs(p[1] - goal[1]);
    while (open.length) {
      let bi = 0;
      for (let i = 1; i < open.length; i += 1) if (open[i].f < open[bi].f) bi = i;
      const { p } = open.splice(bi, 1)[0];
      if (p[0] === goal[0] && p[1] === goal[1]) {
        const path = [];
        let cur = key(...goal), curP = goal;
        while (cur && key(...start) !== cur) { path.unshift(curP); curP = came.get(cur); cur = curP ? key(...curP) : null; }
        return path;
      }
      for (const [dx, dy] of [[1, 0], [-1, 0], [0, 1], [0, -1]]) {
        const n = [p[0] + dx, p[1] + dy], nk = key(...n);
        if (!this.isWalkable(n[0], n[1])) continue;
        const g = gScore.get(key(...p)) + 1;
        if (g < (gScore.get(nk) ?? Infinity)) {
          came.set(nk, p); gScore.set(nk, g);
          open.push({ p: n, g, f: g + h(n) });
        }
      }
    }
    return null;
  }

  // --- actor control -------------------------------------------------------------

  dispatch(interactionName, { manual = false } = {}) {
    const it = this.interactions[interactionName];
    if (!it) return false;
    if (manual) this.manualUntil = performance.now() + 25000;
    if (this.actor.activity) this.endActivity();
    const cur = [Math.round(this.actor.pos[0]), Math.round(this.actor.pos[1])];
    if (cur[0] === it.approach[0] && cur[1] === it.approach[1]) {
      this.beginActivity(it);
      return true;
    }
    const path = this.pathfind(this.actor.pos, it.approach);
    if (!path) return false;
    this.actor.path = path;
    this.actor.state = 'walk';
    this.actor.pendingAct = it;
    return true;
  }

  walkTo(tile, { manual = false } = {}) {
    if (manual) this.manualUntil = performance.now() + 15000;
    if (this.actor.activity) this.endActivity();
    const path = this.pathfind(this.actor.pos, tile);
    if (!path) return false;
    this.actor.path = path;
    this.actor.state = 'walk';
    this.actor.pendingAct = null;
    return true;
  }

  beginActivity(it) {
    this.actor.state = 'act';
    this.actor.activity = it;
    this.actor.path = [];
    this.actor.facing = it.facing || this.actor.facing;
    if (it.at) { this.actor.pos = [...it.at]; this.actor.z = it.z; }
    else this.actor.z = 0;
  }

  endActivity() {
    const it = this.actor.activity;
    if (it && it.at) { this.actor.pos = [...it.approach]; this.actor.z = 0; }
    this.actor.activity = null;
    this.actor.state = 'idle';
  }

  scheduledActivity() {
    const hour = (this.clock / 3600) % 24;
    let chosen = SCHEDULE[SCHEDULE.length - 1];
    for (const slot of SCHEDULE) if (hour >= slot.h) chosen = slot;
    return chosen.act.replace('~wake', '');
  }

  tickLife(now) {
    if (this.mode !== 'live' || !this.autoLife) return;
    if (now < this.manualUntil) return;
    if (this.actor.state === 'walk') return;
    const want = this.scheduledActivity();
    const cur = this.actor.activity;
    if (cur && (cur.key === want || cur.name === want)) return;
    if (this.interactions[want]) this.dispatch(want);
    else if (cur) this.endActivity();
  }

  // --- input -------------------------------------------------------------------

  canvasPoint(e) {
    const rect = this.canvas.getBoundingClientRect();
    const px = ((e.clientX - rect.left) * (this.canvas.width / rect.width) - (this.offX || 0)) / this.scale;
    const py = ((e.clientY - rect.top) * (this.canvas.height / rect.height) - (this.offY || 0)) / this.scale;
    return [px, py];
  }

  tileFromEvent(e) {
    const [px, py] = this.canvasPoint(e);
    const [gx, gy] = this.unproject(px, py).map(Math.floor);
    const { w, d } = this.layout.grid;
    return (gx >= 0 && gy >= 0 && gx < w && gy < d) ? { gx, gy } : null;
  }

  onMove(e) {
    this.hover = this.tileFromEvent(e);
    if (this.hover) {
      const item = this.itemAt(this.hover.gx, this.hover.gy);
      this.canvas.style.cursor = this.mode === 'edit' ? 'pointer' : (item && this.firstInteraction(item) ? 'pointer' : 'default');
    }
  }

  itemAt(gx, gy) {
    // prefer non-decor items when both occupy the tile
    let found = null;
    for (const it of this.items) {
      if (gx >= it.x && gx < it.x + it.w && gy >= it.y && gy < it.y + it.d) {
        if (!it.def.decor) return it;
        found = found || it;
      }
    }
    return found;
  }

  firstInteraction(item) {
    return Object.values(this.interactions).find(it => it.item === item && this.isWalkable(it.approach[0], it.approach[1])) || null;
  }

  onClick(e) {
    const tile = this.tileFromEvent(e);
    if (!tile) return;
    const { gx, gy } = tile;
    if (this.mode === 'edit') { this.editClick(gx, gy); return; }
    const item = this.itemAt(gx, gy);
    if (item && !item.def.decor) {
      const it = this.firstInteraction(item);
      if (it) this.dispatch(it.name, { manual: true });
      return;
    }
    if (this.isWalkable(gx, gy)) this.walkTo([gx, gy], { manual: true });
  }

  onRightClick(e) {
    if (this.mode !== 'edit') return;
    if (this.ghost) { this.ghost = null; return; }
    const tile = this.tileFromEvent(e);
    if (!tile) return;
    const item = this.itemAt(tile.gx, tile.gy);
    if (item) {
      this.layout.furniture.splice(item.idx, 1);
      this.grabbed = null;
      this.rebuild();
      this.saveLayout();
    }
  }

  editClick(gx, gy) {
    if (this.ghost) {
      if (this.canPlace(this.ghost, gx, gy, -1)) {
        this.layout.furniture.push({ type: this.ghost, x: gx, y: gy });
        this.rebuild();
        this.saveLayout();
      }
      return;
    }
    if (this.grabbed !== null) {
      const f = this.layout.furniture[this.grabbed];
      if (this.canPlace(f.type, gx, gy, this.grabbed)) {
        f.x = gx; f.y = gy;
        this.grabbed = null;
        this.rebuild();
        this.saveLayout();
      }
      return;
    }
    const item = this.itemAt(gx, gy);
    if (item) this.grabbed = item.idx;
  }

  canPlace(type, x, y, ignoreIdx) {
    const def = CATALOG[type];
    const { w, d } = this.layout.grid;
    if (x < 0 || y < 0 || x + def.w > w || y + def.d > d) return false;
    if (def.decor) return true;
    for (let i = 0; i < this.layout.furniture.length; i += 1) {
      if (i === ignoreIdx) continue;
      const f = this.layout.furniture[i], fd = CATALOG[f.type];
      if (fd.decor) continue;
      if (x < f.x + fd.w && f.x < x + def.w && y < f.y + fd.d && f.y < y + def.d) return false;
    }
    const ax = Math.round(this.actor.pos[0]), ay = Math.round(this.actor.pos[1]);
    if (ax >= x && ax < x + def.w && ay >= y && ay < y + def.d) return false;
    return true;
  }

  // Visual anchor of the actor: grid tiles render at their center.
  actorWorldPos() {
    const a = this.actor;
    if (a.activity && a.activity.at) return [a.pos[0], a.pos[1]];
    return [a.pos[0] + 0.5, a.pos[1] + 0.5];
  }

  // --- daylight ------------------------------------------------------------------

  daylight() {
    const h = (this.clock / 3600) % 24;
    // keyframes: [hour, tint(rgb), alpha, lampOn]
    const keys = [
      [0.0, [38, 42, 94], 0.52, true],
      [5.4, [38, 42, 94], 0.52, true],
      [7.0, [214, 150, 110], 0.20, false],
      [9.0, [255, 246, 222], 0.03, false],
      [16.0, [255, 238, 205], 0.07, false],
      [17.8, [214, 132, 98], 0.22, true],
      [19.4, [110, 84, 144], 0.34, true],
      [21.0, [58, 58, 108], 0.44, true],
      [24.0, [40, 44, 96], 0.48, true],
    ];
    let a = keys[0], b = keys[keys.length - 1];
    for (let i = 0; i + 1 < keys.length; i += 1) {
      if (h >= keys[i][0] && h <= keys[i + 1][0]) { a = keys[i]; b = keys[i + 1]; break; }
    }
    const t = (h - a[0]) / Math.max(0.001, b[0] - a[0]);
    const mix = (u, v) => u + (v - u) * t;
    return {
      tint: [mix(a[1][0], b[1][0]), mix(a[1][1], b[1][1]), mix(a[1][2], b[1][2])],
      alpha: mix(a[2], b[2]),
      lampOn: (t < 0.5 ? a[3] : b[3]),
      night: h >= 17.6 || h < 6.4,
    };
  }

  // --- depth sorting ----------------------------------------------------------------

  actorInFrontOf(item) {
    const [ax, ay] = this.actorWorldPos();
    const eps = 0.001;
    if (ax >= item.x + item.w - eps) return true;
    if (ay >= item.y + item.d - eps) return true;
    if (ax >= item.x - eps && ax < item.x + item.w && ay >= item.y - eps && ay < item.y + item.d) return true;
    return false;
  }

  actorOn(item) {
    const act = this.actor.activity;
    return Boolean(act && act.item === item && act.at);
  }

  // --- effects ------------------------------------------------------------------------

  spawnEffects(now) {
    const act = this.actor.activity;
    if (!act || !act.effect) return;
    if (!this.lastEffectAt) this.lastEffectAt = 0;
    if (now - this.lastEffectAt < 260) return;
    this.lastEffectAt = now;
    const anchor = act.effectAt
      ? this.project(act.effectAt[0], act.effectAt[1], act.effectAt[2])
      : (() => { const [wx, wy] = this.actorWorldPos(); const [px, py] = this.project(wx, wy, this.actor.z); return [px, py - this.sprites.height - 6]; })();
    const rng = Math.random;
    const mk = (kind, color, vx, vy, life, size = 1) => this.effects.push({ kind, x: anchor[0] + (rng() * 8 - 4), y: anchor[1] + (rng() * 3 - 1), vx, vy, life, t: 0, color, size });
    switch (act.effect) {
      case 'steam': mk('rise', 'rgba(250,244,228,0.95)', (rng() - 0.5) * 3, -9, 1.5, 2); break;
      case 'zzz': if (rng() < 0.5) mk('zzz', '#e7ddc2', 2.5, -6, 2.4); break;
      case 'music': if (rng() < 0.7) mk('note', rng() < 0.5 ? '#e8c568' : '#d9868f', (rng() - 0.5) * 4, -8, 1.8); break;
      case 'keys': mk('spark', '#9fd8cf', (rng() - 0.5) * 6, -2, 0.5); break;
      case 'water': mk('drop', '#7fb4d8', (rng() - 0.5) * 2, 10, 0.8, 1); break;
      case 'sparkle': mk('spark', '#ffe08a', (rng() - 0.5) * 8, -3, 0.9); break;
      case 'chill': mk('spark', '#bfe3f2', (rng() - 0.5) * 5, -3, 0.9); break;
      case 'focus': if (rng() < 0.5) mk('spark', '#f0e2c0', (rng() - 0.5) * 4, -4, 0.9); break;
      default: break;
    }
  }

  drawEffects(ctx, dt) {
    const alive = [];
    for (const fx of this.effects) {
      fx.t += dt;
      if (fx.t < fx.life) alive.push(fx);
      fx.x += fx.vx * dt; fx.y += fx.vy * dt;
      const fade = 1 - fx.t / fx.life;
      ctx.globalAlpha = Math.max(0, Math.min(1, fade * 1.4));
      ctx.fillStyle = fx.color;
      const x = Math.round(fx.x), y = Math.round(fx.y);
      if (fx.kind === 'zzz') {
        ctx.fillRect(x, y, 3, 1); ctx.fillRect(x + 1, y + 1, 1, 1); ctx.fillRect(x, y + 2, 3, 1);
      } else if (fx.kind === 'note') {
        ctx.fillRect(x, y, 1, 4); ctx.fillRect(x - 1, y + 3, 2, 2); ctx.fillRect(x + 1, y, 2, 1);
      } else if (fx.kind === 'drop') {
        ctx.fillRect(x, y, 1, 2);
      } else {
        ctx.fillRect(x, y, fx.size, fx.size);
      }
      ctx.globalAlpha = 1;
    }
    this.effects = alive;
  }

  // --- actor drawing ------------------------------------------------------------------

  drawActor(ctx, now) {
    const a = this.actor;
    const dirs = this.sprites.dirs;
    const act = a.activity;
    const [wx, wy] = this.actorWorldPos();
    const [px, py] = this.project(wx, wy, a.z);

    if (act && act.pose === 'sleep') {
      const head = this.sprites.sleepHead;
      const breath = Math.sin(now / 900);
      if (act.lumpAt) {
        // body bump under the quilt: an iso-aligned rounded strip along the
        // bed axis, drawn as crisp grid-projected polygons
        const [cx, cy] = act.lumpAt;
        const along = act.lumpAxis === 'y' ? [0, 1] : [1, 0];
        const across = act.lumpAxis === 'y' ? [1, 0] : [0, 1];
        const lift = 0.16 + breath * 0.012;
        const pt = (u, v, z) => this.project(cx + along[0] * u + across[0] * v, cy + along[1] * u + across[1] * v, a.z + z);
        const quilt = '#c07a68';   // matches the AI quilt terracotta
        const topPts = [pt(-0.62, -0.2, lift), pt(0.62, -0.2, lift), pt(0.78, 0.06, lift * 0.5), pt(-0.78, 0.06, lift * 0.5)];
        ctx.fillStyle = shade(quilt, 1.04);
        ctx.beginPath(); ctx.moveTo(...topPts[0]); for (const p of topPts.slice(1)) ctx.lineTo(...p); ctx.closePath(); ctx.fill();
        const sidePts = [pt(-0.78, 0.06, lift * 0.5), pt(0.78, 0.06, lift * 0.5), pt(0.66, 0.3, 0.02), pt(-0.66, 0.3, 0.02)];
        ctx.fillStyle = shade(quilt, 0.86);
        ctx.beginPath(); ctx.moveTo(...sidePts[0]); for (const p of sidePts.slice(1)) ctx.lineTo(...p); ctx.closePath(); ctx.fill();
        ctx.strokeStyle = shade(quilt, 0.66); ctx.lineWidth = 1;
        ctx.beginPath(); ctx.moveTo(...topPts[0]); ctx.lineTo(...topPts[1]); ctx.stroke();
      }
      ctx.drawImage(head.canvas, Math.round(px - head.w / 2), Math.round(py - head.h + 2 + breath * 0.4));
      return;
    }

    let sprite;
    const dir = dirs[a.facing] || dirs.downLeft;
    if (act && act.pose === 'sit') {
      sprite = dir.sit;
    } else if (a.state === 'walk') {
      sprite = dir.walk[Math.floor(a.walked * 5.2) % dir.walk.length];
    } else {
      const blink = (now % 3600) < 130;
      sprite = blink ? dir.blink : dir.stand;
    }
    if (!act || !act.at) {
      // crisp diamond shadow at the feet, matching the furniture shadows
      ctx.fillStyle = 'rgba(22,14,32,0.30)';
      ctx.beginPath();
      ctx.moveTo(px - 8, py); ctx.lineTo(px, py - 4); ctx.lineTo(px + 8, py); ctx.lineTo(px, py + 4);
      ctx.closePath(); ctx.fill();
    }
    const sitDrop = act && act.pose === 'sit'
      ? ((this.sprites.sitDrop && this.sprites.sitDrop[a.facing]) ?? 5) : 0;
    const x = Math.round(px - sprite.w / 2);
    const y = Math.round(py - sprite.h + sitDrop);
    ctx.drawImage(sprite.canvas, x, y);

    if (act && act.effect === 'phone') {
      const glow = Math.sin(now / 500) * 0.08 + 0.5;
      ctx.globalAlpha = glow;
      ctx.fillStyle = '#bfe9e2';
      ctx.fillRect(x + sprite.w / 2 - 3, y + Math.round(sprite.h * 0.55), 5, 4);
      ctx.globalAlpha = 1;
    }
  }

  statusText() {
    const a = this.actor;
    if (this.mode === 'edit') return '编辑模式';
    if (a.state === 'walk') return ACTIVITY_LABEL.walk;
    if (a.activity) return ACTIVITY_LABEL[a.activity.key] || a.activity.label;
    return ACTIVITY_LABEL.idle;
  }

  clockText() {
    const h = Math.floor(this.clock / 3600) % 24, m = Math.floor(this.clock / 60) % 60;
    return `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}`;
  }

  // --- main loop --------------------------------------------------------------------------

  frame(now) {
    const dt = Math.min(0.05, (now - (this.lastFrame || now)) / 1000);
    this.lastFrame = now;
    if (this.mode === 'live') this.clock = (this.clock + dt * this.timeScale) % 86400;

    const a = this.actor;
    if (a.path.length) {
      const speed = 3.1;
      const target = a.path[0];
      const dx = target[0] - a.pos[0], dy = target[1] - a.pos[1];
      const dist = Math.hypot(dx, dy);
      if (dist < speed * dt) {
        a.pos = [target[0], target[1]];
        a.path.shift();
        if (!a.path.length) {
          if (a.pendingAct) { this.beginActivity(a.pendingAct); a.pendingAct = null; }
          else a.state = 'idle';
        }
      } else {
        a.pos[0] += dx / dist * speed * dt;
        a.pos[1] += dy / dist * speed * dt;
        a.walked += speed * dt;
        a.facing = Math.abs(dx) > Math.abs(dy) ? (dx > 0 ? 'downRight' : 'upLeft') : (dy > 0 ? 'downLeft' : 'upRight');
      }
    }

    this.tickLife(now);
    this.spawnEffects(now);
    this.render(now, dt);
    if (this.ui.onStatus) this.ui.onStatus(this.clockText(), this.statusText());
    requestAnimationFrame(t => this.frame(t));
  }

  syncCanvasSize() {
    const dpr = window.devicePixelRatio || 1;
    const cssW = this.canvas.clientWidth;
    if (!cssW) {
      this.scale = Math.max(1, Math.floor(this.canvas.width / this.native.w));
      return;
    }
    const scale = Math.max(2, Math.floor((cssW * dpr) / this.native.w));
    const wantW = Math.round(cssW * dpr);
    const wantH = Math.round(this.native.h * scale + 8 * 2);
    if (this.canvas.width !== wantW || this.canvas.height !== wantH) {
      this.canvas.width = wantW;
      this.canvas.height = wantH;
      this.canvas.style.height = `${wantH / dpr}px`;
    }
    this.scale = scale;
  }

  render(now, dt) {
    const ctx = this.ctx;
    this.syncCanvasSize();
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.imageSmoothingEnabled = false;
    ctx.fillStyle = '#120c1a';
    ctx.fillRect(0, 0, this.canvas.width, this.canvas.height);
    const offX = Math.round((this.canvas.width - this.native.w * this.scale) / 2);
    const offY = Math.max(8, Math.round((this.canvas.height - this.native.h * this.scale) / 2));
    this.offX = offX; this.offY = offY;
    ctx.setTransform(this.scale, 0, 0, this.scale, offX, offY);

    const light = this.daylight();

    // 1) static background
    ctx.drawImage(this.background.canvas, 0, 0);

    // 2) window (dynamic sky)
    for (const entry of this.layout.wallDecor || []) {
      if (entry.type !== 'window') continue;
      const sprite = light.night ? this.windowSprites.dusk : this.windowSprites.day;
      const zTop = sprite.surface.zTop;
      const [ax, ay] = this.wallAnchor(entry.side, entry.t, zTop);
      ctx.drawImage(sprite.surface.canvas, Math.round(ax + sprite.offset[0]), Math.round(ay + sprite.offset[1]));
    }

    // 3) depth-sorted furniture + actor, with contact shadows and front overlays
    const sorted = [...this.items].sort((p, q) => (p.x + p.w + p.y + p.d) - (q.x + q.w + q.y + q.d));
    let actorIndex = 0;
    for (let i = 0; i < sorted.length; i += 1) if (this.actorInFrontOf(sorted[i])) actorIndex = i + 1;
    let actorDrawn = false;
    const blitItem = (it, layer) => {
      const [px, py] = this.project(it.x, it.y, 0);
      const baked = layer === 'front' ? it.baked.front : it.baked;
      ctx.drawImage(baked.surface.canvas, Math.round(px + baked.offset[0]), Math.round(py + baked.offset[1]));
    };
    // contact shadows first, clipped to the floor so they never climb walls.
    // The shadow is the furniture footprint itself, slightly inflated toward
    // the viewer — a diamond, not an ellipse, so it hugs the grid.
    {
      const { w: gw, d: gd } = this.layout.grid;
      const floorPts = [this.project(0, 0), this.project(gw, 0), this.project(gw, gd), this.project(0, gd)];
      ctx.save();
      ctx.beginPath();
      ctx.moveTo(...floorPts[0]); for (const p of floorPts.slice(1)) ctx.lineTo(...p);
      ctx.closePath(); ctx.clip();
      const shadowDiamond = (it, grow, alpha) => {
        const pts = [
          this.project(it.x - grow * 0.4, it.y - grow * 0.4),
          this.project(it.x + it.w + grow, it.y - grow * 0.4),
          this.project(it.x + it.w + grow, it.y + it.d + grow),
          this.project(it.x - grow * 0.4, it.y + it.d + grow),
        ];
        ctx.globalAlpha = alpha;
        ctx.fillStyle = '#160e20';
        ctx.beginPath();
        ctx.moveTo(...pts[0]); for (const p of pts.slice(1)) ctx.lineTo(...p);
        ctx.closePath(); ctx.fill();
      };
      for (const it of sorted) {
        if (it.def.decor || it.type === 'floorLamp') continue;
        const ghosted = this.mode === 'edit' && this.grabbed === it.idx;
        shadowDiamond(it, 0.16, ghosted ? 0.1 : 0.18);
        shadowDiamond(it, 0.05, ghosted ? 0.08 : 0.14);
      }
      ctx.restore();
      ctx.globalAlpha = 1;
    }
    for (let i = 0; i < sorted.length; i += 1) {
      const it = sorted[i];
      if (!actorDrawn && i === actorIndex) { this.drawActor(ctx, now); actorDrawn = true; }
      const ghosted = this.mode === 'edit' && this.grabbed === it.idx;
      if (ghosted) ctx.globalAlpha = 0.35;
      blitItem(it, 'body');
      if (it.baked.front) {
        if (!actorDrawn && actorIndex === i + 1 && this.actorOn(it)) {
          ctx.globalAlpha = 1;
          this.drawActor(ctx, now);
          actorDrawn = true;
          if (ghosted) ctx.globalAlpha = 0.35;
        }
        blitItem(it, 'front');
      }
      if (ghosted) ctx.globalAlpha = 1;
    }
    if (!actorDrawn) this.drawActor(ctx, now);

    // 4) effects
    this.drawEffects(ctx, dt);

    // 5) global tint
    if (light.alpha > 0.01) {
      ctx.save();
      ctx.globalCompositeOperation = 'multiply';
      ctx.globalAlpha = light.alpha;
      const [r, g, b] = light.tint;
      ctx.fillStyle = `rgb(${Math.round(r)},${Math.round(g)},${Math.round(b)})`;
      ctx.fillRect(-offX / this.scale, -offY / this.scale, this.canvas.width / this.scale, this.canvas.height / this.scale);
      ctx.restore();
    }

    // 6) warm light sources, clipped to the room silhouette so glow never
    // bleeds into the void around the diorama
    if (light.lampOn) {
      ctx.save();
      const { w: gw, d: gd } = this.layout.grid;
      const capD = 0.14;
      const sil = [
        this.project(-capD, gd + capD, -0.12),
        this.project(-capD, 0, -0.12),
        this.project(-capD, 0, WALL_H),
        this.project(0, -capD, WALL_H),
        this.project(gw + capD, -capD, WALL_H),
        this.project(gw + capD, -capD, -0.12),
        this.project(gw, 0, -0.12),
        this.project(gw, gd, 0),
        [this.project(gw, gd, 0)[0], this.project(gw, gd, 0)[1] + 7],
        [this.project(0, gd, 0)[0], this.project(0, gd, 0)[1] + 7],
      ];
      ctx.beginPath();
      ctx.moveTo(...sil[0]); for (const p of sil.slice(1)) ctx.lineTo(...p);
      ctx.closePath(); ctx.clip();
      ctx.globalCompositeOperation = 'screen';
      for (const em of this.emitters) {
        const [ex, ey] = this.project(em.x, em.y, em.z);
        const pulse = 1 + Math.sin(now / 900 + em.x * 3) * 0.03;
        const r = em.r * pulse;
        const [cr, cg, cb] = em.color;
        const grad = ctx.createRadialGradient(ex, ey, 1, ex, ey, r);
        grad.addColorStop(0, `rgba(${cr},${cg},${cb},0.62)`);
        grad.addColorStop(0.55, `rgba(${cr},${cg},${cb},0.2)`);
        grad.addColorStop(1, 'rgba(0,0,0,0)');
        ctx.fillStyle = grad;
        ctx.fillRect(ex - r, ey - r, r * 2, r * 2);
        // floor pool
        if (em.pool) {
          const [fx, fy] = this.project(em.x, em.y, 0);
          const pr = r * 1.15;
          const pg = ctx.createRadialGradient(fx, fy, 1, fx, fy, pr);
          pg.addColorStop(0, `rgba(${cr},${cg},${cb},0.4)`);
          pg.addColorStop(0.6, `rgba(${cr},${cg},${cb},0.14)`);
          pg.addColorStop(1, 'rgba(0,0,0,0)');
          ctx.fillStyle = pg;
          ctx.save();
          ctx.translate(fx, fy);
          ctx.scale(1, 0.5);
          ctx.beginPath(); ctx.arc(0, 0, pr, 0, Math.PI * 2); ctx.fill();
          ctx.restore();
        }
      }
      // string light bulbs
      for (const g of this.decorGlow || []) {
        const pulse = 0.8 + Math.sin(now / 700 + g.x) * 0.2;
        const [cr, cg, cb] = g.color;
        const grad = ctx.createRadialGradient(g.x, g.y, 0.5, g.x, g.y, g.r * pulse);
        grad.addColorStop(0, `rgba(${cr},${cg},${cb},0.7)`);
        grad.addColorStop(1, 'rgba(0,0,0,0)');
        ctx.fillStyle = grad;
        ctx.fillRect(g.x - g.r, g.y - g.r, g.r * 2, g.r * 2);
      }
      // laptop glow while studying
      const act = this.actor.activity;
      if (act && act.effect === 'keys') {
        const [wx2, wy2] = this.actorWorldPos();
        const [ax, ay] = this.project(wx2, wy2, 0);
        const grad = ctx.createRadialGradient(ax, ay - 16, 2, ax, ay - 16, 22);
        grad.addColorStop(0, 'rgba(150,220,205,0.30)');
        grad.addColorStop(1, 'rgba(0,0,0,0)');
        ctx.fillStyle = grad;
        ctx.fillRect(ax - 24, ay - 40, 48, 48);
      }
      ctx.restore();
    }

    // 7) hover + editor overlay
    this.drawOverlay(ctx, now);
    ctx.setTransform(1, 0, 0, 1, 0, 0);
  }

  drawOverlay(ctx, now) {
    if (!this.hover && !(this.mode === 'edit' && this.ghost)) return;
    if (this.hover) {
      const { gx, gy } = this.hover;
      const pts = [this.project(gx, gy), this.project(gx + 1, gy), this.project(gx + 1, gy + 1), this.project(gx, gy + 1)];
      let color = 'rgba(255,255,255,0.35)';
      if (this.mode === 'edit') {
        const type = this.ghost || (this.grabbed !== null ? this.layout.furniture[this.grabbed].type : null);
        if (type) color = this.canPlace(type, gx, gy, this.grabbed ?? -1) ? 'rgba(140,230,150,0.55)' : 'rgba(235,90,90,0.6)';
      } else {
        const item = this.itemAt(gx, gy);
        color = item && !item.def.decor
          ? (this.firstInteraction(item) ? 'rgba(255,224,130,0.55)' : 'rgba(255,255,255,0.2)')
          : (this.isWalkable(gx, gy) ? 'rgba(160,220,255,0.4)' : 'rgba(235,90,90,0.35)');
      }
      ctx.strokeStyle = color;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(...pts[0]); for (const p of pts.slice(1)) ctx.lineTo(...p);
      ctx.closePath(); ctx.stroke();

      if (this.mode === 'edit') {
        const type = this.ghost || (this.grabbed !== null ? this.layout.furniture[this.grabbed].type : null);
        if (type) {
          const baked = this.bakedTypes[type];
          const [px, py] = this.project(gx, gy);
          ctx.globalAlpha = 0.55;
          ctx.drawImage(baked.surface.canvas, Math.round(px + baked.offset[0]), Math.round(py + baked.offset[1]));
          ctx.globalAlpha = 1;
        }
      }
    }
  }
}

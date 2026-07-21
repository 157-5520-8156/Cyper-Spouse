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
      sleep: { label: '睡觉', approach: [-1, 1], at: [0.55, 0.62], z: 0.62, facing: 'upLeft', pose: 'sleep', effect: 'zzz', lumpAt: [0.8, 1.55], lumpAxis: 'y' },
    },
  },
  desk: {
    // plain-box stand-in until the AI sprite loads (footprint changed in v2)
    name: '书桌', bake: () => Bakery.bakeBox(1, 2, 0.62, PAL.wood, PAL.woodDark), w: 1, d: 2,
    interactions: {
      study: { label: '用电脑', approach: [1, 2], at: [1.5, 1.5], z: 0.55, facing: 'upLeft', pose: 'sit', effect: 'keys' },
    },
  },
  diningTable: {
    name: '餐桌', bake: () => Bakery.bakeBox(2, 1, 0.72, PAL.wood, PAL.woodDark), w: 2, d: 1,
    interactions: {
      eat: { label: '吃饭', approach: [1, 1], at: [0.5, 1.5], z: 0.52, facing: 'upRight', pose: 'sit', effect: 'steam', effectAt: [0.5, 0.5, 0.85] },
    },
  },
  // Chairs are independent items (not baked into table sprites): occlusion
  // against tables and the seated actor comes from the normal depth sort.
  // A layout entry may set flip:true to mirror the sprite (NE-facing chair
  // becomes NW-facing for the desk).
  chair: {
    name: '椅子', bake: () => Bakery.bakeChair(), w: 1, d: 1,
    interactions: {},
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
      browse: { label: '找书', approach: [1, 1], at: null, z: 0, facing: 'upLeft', pose: 'stand', effect: 'focus' },
    },
  },
  lowshelf: {
    name: '矮书柜', bake: () => Bakery.bakeLowShelf(), w: 2, d: 1,
    interactions: {
      tidy: { label: '整理', approach: [1, 1], at: null, z: 0, facing: 'upRight', pose: 'stand', effect: 'sparkle', effectAt: [1.0, 0.4, 1.0] },
    },
  },
  sofa: {
    name: '沙发', bake: () => Bakery.bakeSofa(), w: 2, d: 1,
    interactions: {
      relax: { label: '窝着休息', approach: [2, 0], at: [1.42, 0.46], z: 0.47, facing: 'downLeft', pose: 'sit', effect: 'music' },
      phone: { label: '刷手机', approach: [-1, 0], at: [1.42, 0.46], z: 0.47, facing: 'downLeft', pose: 'sit', effect: 'phone' },
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
      dress: { label: '挑衣服', approach: [1, 1], at: null, z: 0, facing: 'upLeft', pose: 'stand', effect: 'sparkle', effectAt: [0.8, 1.0, 1.2] },
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
      water: { label: '浇水', approach: [1, 0], at: null, z: 0, facing: 'upLeft', pose: 'stand', effect: 'water', effectAt: [0.5, 0.5, 0.9] },
    },
  },
  floorLamp: { name: '落地灯', bake: () => Bakery.bakeFloorLamp(), w: 1, d: 1, interactions: {} },
  pendant: { name: '吊灯', bake: () => Bakery.bakePendant(), w: 1, d: 1, decor: true, interactions: {} },
};

// --- default room layout ----------------------------------------------------

// v8 layout: shallow 3x1 notch at the plan's top-left; kitchen on the LEFT
// (counter under the forward wall, fridge at the jog corner), bed nook on
// the RIGHT under the window, living cluster bottom-right, office (bookcase
// + desk) bottom-left.
// Plan: tools/raw/floorplan-asbuilt.png
const DEFAULT_LAYOUT = {
  grid: { w: 10, d: 8 },
  notch: { x: 0, y: 0, w: 3, d: 1 },
  furniture: [
    // kitchen block top-left: counter fills the forward wall, fridge and
    // wardrobe continue along the deep wall past the jog
    { type: 'kitchen', x: 0, y: 1 },
    { type: 'fridge', x: 3, y: 0 },
    { type: 'wardrobe', x: 4, y: 0 },
    // dining on the tile, by the kitchen
    { type: 'diningTable', x: 2, y: 3 },
    { type: 'chair', x: 2, y: 4 },
    // sleep nook top-right: bed under the window, lowshelf as the divider
    { type: 'nightstand', x: 7, y: 0 },
    { type: 'bed', x: 8, y: 0 },
    { type: 'lowshelf', x: 8, y: 3 },
    { type: 'plantBig', x: 5, y: 3 },
    // living cluster bottom-right
    { type: 'sofa', x: 6, y: 4 },
    { type: 'coffeeTable', x: 6, y: 5 },
    { type: 'stool', x: 5, y: 6 },
    { type: 'floorLamp', x: 8, y: 5 },
    // office bottom-left
    { type: 'bookcase', x: 0, y: 3 },
    { type: 'desk', x: 0, y: 5 },
    { type: 'chair', x: 1, y: 6, flip: true },
    { type: 'plantSmall', x: 0, y: 7 },
  ],
  zones: [
    { x: 0, y: 1, w: 4, d: 2, material: 'tile' },
    { x: 1, y: 3, w: 3, d: 2, material: 'tile' },
  ],
  rugs: [
    { x: 5, y: 4, w: 4, d: 3, inner: '#a9c4b0', border: '#6f9282' },
    { x: 2, y: 3, w: 2, d: 2, inner: '#88a7ad', border: '#5d7f88' },
    { x: 7, y: 1, w: 1, d: 2, inner: '#7f9a92', border: '#5a7570' },
  ],
  wallDecor: [
    // forward NE wall (kitchen, t in 0..3)
    { type: 'backsplash', side: 'NE', t: 0.05, len: 2.95 },
    { type: 'upperCabinet', side: 'NE', t: 0.5 },
    // deep NE wall (bedroom side, t in 3..10)
    { type: 'window', side: 'NE', t: 8.25 },
    { type: 'picture', side: 'NE', t: 5.7, seed: 11 },
    { type: 'hangingPlant', side: 'NE', t: 6.75 },
    { type: 'stringLights', side: 'NE', t: 4.2, len: 5.4, seed: 3 },
    // NW wall: pinboard over the bookcase, shelf over the desk
    { type: 'pinboard', side: 'NW', t: 3.35 },
    { type: 'shelf', side: 'NW', t: 5.5 },
    { type: 'stringLights', side: 'NW', t: 3.1, len: 4.6, seed: 4 },
  ],
  entry: [1, 2],
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

// Load one image with retries: a dropped request must not silently strand
// the room on the procedural fallback art.
function loadImageSurface(url, attempts = 4) {
  return new Promise(resolve => {
    let n = 0;
    const tryOnce = () => {
      n += 1;
      const img = new Image();
      img.onload = () => {
        const sf = new Surface(img.width, img.height);
        sf.ctx.drawImage(img, 0, 0);
        resolve(sf);
      };
      img.onerror = () => {
        if (n >= attempts) { console.warn(`sprite failed after ${n} tries: ${url}`); resolve(null); }
        else setTimeout(tryOnce, 180 * n);
      };
      img.src = `${url}?t=${Date.now()}`;
    };
    tryOnce();
  });
}

async function loadSpriteOverrides(url = 'assets/ai/manifest.json') {
  try {
    const res = await fetch(url, { cache: 'no-store' });
    if (!res.ok) return;
    const manifest = await res.json();
    await Promise.all(Object.entries(manifest).map(async ([type, meta]) => {
      const sf = await loadImageSurface(meta.url);
      if (!sf) return;
      const entry = { surface: sf, offset: meta.offset, front: null };
      // optional camera-side overlay layer (e.g. the chair's back panel,
      // drawn over a seated actor)
      if (meta.front) {
        const ff = await loadImageSurface(meta.front.url);
        if (ff) entry.front = { surface: ff, offset: meta.front.offset };
      }
      SPRITE_OVERRIDES[type] = entry;
    }));
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
      // AI overrides replace the procedural front overlay with their own
      // (or none for single-layer sprites)
      this.bakedTypes[type] = override ? { ...baked, surface: override.surface, offset: override.offset, front: override.front } : baked;
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
    const nn = this.layout.notch;
    const kind = this.notchKind();
    const maxSum = kind === 'south' ? Math.max(nn.x + d, w + nn.y) : w + d;
    const maxXmY = kind === 'top' ? Math.max(nn.x, w - nn.d) : w;
    // with a 'left' notch the deepest existing corner sits min(nn.w, nn.d)
    // half-rows below the nominal north corner; trim the empty sky
    const minSum = kind === 'left' ? Math.min(nn.w, nn.d) : 0;
    this.native = { w: (d + maxXmY) * HX + 26, h: (maxSum - minSum) * HY + (WALL_H + 0.9) * HZ + 18 };
    this.origin = [d * HX + 13, Math.round((WALL_H + 0.5) * HZ) - minSum * HY];
    this.blocked = new Set();
    this.items = [];
    this.emitters = [];
    for (let i = 0; i < this.layout.furniture.length; i += 1) {
      const f = this.layout.furniture[i];
      const def = CATALOG[f.type];
      let baked = this.bakedTypes[f.type];
      // flip:true mirrors the sprite around the screen-vertical axis, which
      // swaps the footprint axes (w x d -> d x w) and the SE/SW faces.
      if (f.flip) {
        const front = baked.front
          ? { surface: baked.front.surface.mirrored(), offset: [-(baked.front.surface.w - 1 + baked.front.offset[0]), baked.front.offset[1]] }
          : null;
        baked = { ...baked, surface: baked.surface.mirrored(), offset: [-(baked.surface.w - 1 + baked.offset[0]), baked.offset[1]], front };
      }
      const iw = f.flip ? def.d : def.w, id = f.flip ? def.w : def.d;
      const item = { idx: i, type: f.type, def, baked, flip: !!f.flip, x: f.x, y: f.y, w: iw, d: id };
      this.items.push(item);
      if (!def.decor) {
        for (let dx = 0; dx < iw; dx += 1) for (let dy = 0; dy < id; dy += 1) this.blocked.add(`${f.x + dx},${f.y + dy}`);
      }
      for (const em of baked.emitters || []) {
        const [ex, ey] = f.flip ? [em.dy, em.dx] : [em.dx, em.dy];
        this.emitters.push({ x: f.x + ex, y: f.y + ey, z: em.z, r: em.r, color: em.color, pool: em.pool });
      }
    }
    this.interactions = {};
    const FLIP_FACING = { downLeft: 'downRight', downRight: 'downLeft', upLeft: 'upRight', upRight: 'upLeft' };
    for (const item of this.items) {
      for (const [key, def] of Object.entries(item.def.interactions || {})) {
        const name = this.interactions[key] ? `${key}@${item.idx}` : key;
        const rel = pt => (item.flip ? [pt[1], pt[0]] : [pt[0], pt[1]]);
        const approach = rel(def.approach), at = def.at ? rel(def.at) : null;
        const effectAt = def.effectAt ? rel(def.effectAt) : null;
        const lumpAt = def.lumpAt ? rel(def.lumpAt) : null;
        const entry = {
          name, key, item, label: def.label,
          approach: [item.x + approach[0], item.y + approach[1]],
          at: at ? [item.x + at[0], item.y + at[1]] : null,
          z: def.z || 0, pose: def.pose || 'stand',
          facing: item.flip ? (FLIP_FACING[def.facing] || def.facing) : def.facing,
          effect: def.effect || null,
          effectAt: effectAt ? [item.x + effectAt[0], item.y + effectAt[1], def.effectAt[2]] : null,
          lumpAt: lumpAt ? [item.x + lumpAt[0], item.y + lumpAt[1]] : null,
          lumpAxis: item.flip ? (def.lumpAxis === 'y' ? 'x' : 'y') : (def.lumpAxis || 'y'),
        };
        // Table sits (dining eat / desk study) bind to an actual CHAIR next
        // to this table instead of the hardcoded offset, so the pose follows
        // the chair wherever the user drags it in the editor.  Sits whose
        // `at` lies inside the item itself (sofa, stool) are left alone.
        const atInside = entry.at
          && entry.at[0] >= item.x && entry.at[0] < item.x + item.w
          && entry.at[1] >= item.y && entry.at[1] < item.y + item.d;
        if (entry.pose === 'sit' && entry.at && !atInside) this.snapSitToChair(entry, item);
        this.interactions[name] = entry;
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

  // Re-anchor a table-sit interaction onto a chair adjacent to the table:
  // at = chair centre, facing = toward the table, approach = a walkable tile
  // next to the chair (preferring the side opposite the table).
  snapSitToChair(entry, table) {
    const chairs = this.items.filter(c => c.type === 'chair');
    const touching = chairs.find(c =>
      (c.x >= table.x - 1 && c.x <= table.x + table.w && c.y >= table.y && c.y < table.y + table.d)
      || (c.y >= table.y - 1 && c.y <= table.y + table.d && c.x >= table.x && c.x < table.x + table.w));
    if (!touching) return;
    const c = touching;
    entry.at = [c.x + 0.5, c.y + 0.5];
    // facing: toward the table along the dominant axis
    const tx = table.x + table.w / 2 - (c.x + 0.5);
    const ty = table.y + table.d / 2 - (c.y + 0.5);
    let dir, opposite;
    if (Math.abs(tx) >= Math.abs(ty)) { dir = tx > 0 ? 'downRight' : 'upLeft'; opposite = tx > 0 ? [-1, 0] : [1, 0]; }
    else { dir = ty > 0 ? 'downLeft' : 'upRight'; opposite = ty > 0 ? [0, -1] : [0, 1]; }
    entry.facing = dir;
    const cands = [opposite, [0, 1], [1, 0], [0, -1], [-1, 0]];
    for (const [dx, dy] of cands) {
      const t = [c.x + dx, c.y + dy];
      if (this.isWalkable(t[0], t[1])) { entry.approach = t; return; }
    }
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

  // Tiles removed from the plan by the L-shape FLOOR notch (layout.notch
  // rect).  Three placements are supported:
  //   left notch  (n.x === 0, n.y === 0): the plan's top-LEFT corner is cut
  //     (the iso view's deep north corner).  The NE wall splits into a near
  //     segment at y = n.d (left) and a deep segment at y = 0 (right wing),
  //     joined by a jog wall at x = n.w.
  //   top notch   (n.y === 0, reaching x = w): the plan's top-right corner is
  //     cut; deep NE on the left, jog at x = n.x, near NE on the right.
  //   south notch (reaching x = w and y = d): the bottom corner is cut; the
  //     walls stay a plain L of two segments.
  inNotch(x, y) {
    const n = this.layout.notch;
    return Boolean(n && x >= n.x && x < n.x + n.w && y >= n.y && y < n.y + n.d);
  }

  notchKind() {
    const n = this.layout.notch;
    if (!n) return 'none';
    if (n.y === 0 && n.x === 0) return 'left';
    return n.y === 0 ? 'top' : 'south';
  }

  isWalkable(x, y) {
    const { w, d } = this.layout.grid;
    return x >= 0 && y >= 0 && x < w && y < d && !this.inNotch(x, y) && !this.blocked.has(`${x},${y}`);
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
    // The jog wall of the L-notch faces the same way as the NW wall
    const side = entry.side === 'JOG' ? 'NW' : entry.side;
    switch (entry.type) {
      case 'window': return null; // dynamic, drawn per-frame
      case 'shelf': return Bakery.bakeWallShelf(side);
      case 'picture': return Bakery.bakePicture(side, entry.seed || 8);
      case 'pinboard': return Bakery.bakePinboard(side);
      case 'stringLights': return Bakery.bakeStringLights(side, entry.len || 3, entry.seed || 3);
      case 'hangingPlant': return Bakery.bakeHangingPlant(side);
      case 'upperCabinet': return Bakery.bakeUpperCabinet(side);
      case 'backsplash': return Bakery.bakeBacksplash(side, entry.len || 2);
      case 'clock': return Bakery.bakeWallClock(side);
      case 'door': return Bakery.bakeDoor(side);
      default: return null;
    }
  }

  wallAnchor(side, t, zTop) {
    const n = this.layout.notch;
    const kind = this.notchKind();
    if (side === 'NE') {
      const y = (kind === 'top' && t >= n.x) || (kind === 'left' && t < n.w) ? n.d : 0;
      return this.project(t, y, zTop);
    }
    if (side === 'JOG') return this.project(n ? (kind === 'left' ? n.w : n.x) : 0, t, zTop);
    return this.project(0, t, zTop);
  }

  bakeBackground() {
    const { w, d } = this.layout.grid;
    const n = this.layout.notch;
    const bg = new Surface(this.native.w, this.native.h);
    const P = (x, y, z = 0) => this.project(x, y, z);

    // floor outline in plan coords
    const kind = this.notchKind();
    const outline = kind === 'left'
      ? [[n.w, 0], [w, 0], [w, d], [0, d], [0, n.d], [n.w, n.d]]
      : kind === 'top'
        ? [[0, 0], [n.x, 0], [n.x, n.d], [w, n.d], [w, d], [0, d]]
        : kind === 'south'
          ? [[0, 0], [w, 0], [w, n.y], [n.x, n.y], [n.x, d], [0, d]]
          : [[0, 0], [w, 0], [w, d], [0, d]];

    // plinth: dark platform under the room
    bg.poly(outline.map(([x, y]) => { const p = P(x, y); return [p[0], p[1] + 7]; }), '#181022');

    // floor tiles by zone
    for (let gy = 0; gy < d; gy += 1) {
      for (let gx = 0; gx < w; gx += 1) {
        if (this.inNotch(gx, gy)) continue;
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

    // floor slab edges along the viewer-facing outline (SW faces along
    // constant-y runs, SE faces along constant-x runs)
    const swEdge = (x0, x1, y) => {
      bg.poly([P(x0, y), P(x1, y), [P(x1, y)[0], P(x1, y)[1] + 6], [P(x0, y)[0], P(x0, y)[1] + 6]], '#493024');
      bg.line(...P(x0, y), ...P(x1, y), PAL.outline);
    };
    const seEdge = (x, y0, y1) => {
      bg.poly([P(x, y0), P(x, y1), [P(x, y1)[0], P(x, y1)[1] + 6], [P(x, y0)[0], P(x, y0)[1] + 6]], '#38251c');
      bg.line(...P(x, y0), ...P(x, y1), PAL.outline);
    };
    if (kind === 'south') {
      swEdge(0, n.x, d);
      seEdge(n.x, n.y, d);
      swEdge(n.x, w, n.y);
      seEdge(w, 0, n.y);
    } else if (kind === 'top') {
      swEdge(0, w, d);
      seEdge(w, n.d, d);
    } else {
      // 'left' keeps the full viewer-facing L of the plain room: the cut
      // corner is behind the walls
      swEdge(0, w, d);
      seEdge(w, 0, d);
    }

    // --- walls ------------------------------------------------------------
    // Two straight interior walls: NE (along y=0) and NW (along x=0).
    const capD = 0.14;

    const neSegment = (x0, x1, y) => {
      bg.poly([P(x0, y - capD, WALL_H), P(x1 + capD, y - capD, WALL_H), P(x1 + capD, y - capD, -0.1), P(x0, y - capD, -0.1)], '#241a2e');
      bg.poly([P(x0, y, WALL_H), P(x1, y, WALL_H), P(x1, y, 0), P(x0, y, 0)], PAL.wall);
      for (let band = 0; band < 3; band += 1) {
        const z1 = 0.55 - band * 0.18, z0 = Math.max(0, z1 - 0.18);
        bg.poly([P(x0, y, z1), P(x1, y, z1), P(x1, y, z0), P(x0, y, z0)], `rgba(120,84,70,${0.05 + band * 0.03})`);
      }
      bg.poly([P(x0, y, 0.22), P(x1, y, 0.22), P(x1, y, 0), P(x0, y, 0)], PAL.wallTrim);
      bg.line(...P(x0, y, 0.22), ...P(x1, y, 0.22), shade(PAL.wallTrim, 0.6));
      bg.line(...P(x0, y, 2.2), ...P(x1, y, 2.2), shade(PAL.wall, 0.86));
      const cap = [P(x0, y - capD, WALL_H), P(x1 + capD, y - capD, WALL_H), P(x1 + capD, y, WALL_H), P(x0, y, WALL_H)];
      bg.poly(cap, shade(PAL.wall, 1.12));
      bg.polyLine(cap, PAL.outline);
      // floor AO along the base
      bg.poly([P(x0, y), P(x1, y), P(x1, y + 0.5), P(x0, y + 0.5)], 'rgba(44,26,50,0.16)');
      bg.poly([P(x0, y), P(x1, y), P(x1, y + 0.22), P(x0, y + 0.22)], 'rgba(44,26,50,0.16)');
    };

    const nwSegment = (x, y0, y1, tone = 0.86, capTone = 1.05) => {
      bg.poly([P(x - capD, y0, WALL_H), P(x - capD, y1 + capD, WALL_H), P(x - capD, y1 + capD, -0.1), P(x - capD, y0, -0.1)], '#1e1526');
      bg.poly([P(x, y0, WALL_H), P(x, y1, WALL_H), P(x, y1, 0), P(x, y0, 0)], shade(PAL.wall, tone));
      for (let band = 0; band < 3; band += 1) {
        const z1 = 0.55 - band * 0.18, z0 = Math.max(0, z1 - 0.18);
        bg.poly([P(x, y0, z1), P(x, y1, z1), P(x, y1, z0), P(x, y0, z0)], `rgba(96,66,60,${0.06 + band * 0.03})`);
      }
      bg.poly([P(x, y0, 0.22), P(x, y1, 0.22), P(x, y1, 0), P(x, y0, 0)], shade(PAL.wallTrim, 0.82));
      bg.line(...P(x, y0, 0.22), ...P(x, y1, 0.22), shade(PAL.wallTrim, 0.55));
      bg.line(...P(x, y0, 2.2), ...P(x, y1, 2.2), shade(PAL.wall, 0.78));
      const cap = [P(x - capD, y0, WALL_H), P(x, y0, WALL_H), P(x, y1, WALL_H), P(x - capD, y1 + capD, WALL_H)];
      bg.poly(cap, shade(PAL.wall, capTone));
      bg.polyLine(cap, PAL.outline);
      bg.poly([P(x, y0), P(x, y1), P(x + 0.5, y1), P(x + 0.5, y0)], 'rgba(44,26,50,0.18)');
      bg.poly([P(x, y0), P(x, y1), P(x + 0.22, y1), P(x + 0.22, y0)], 'rgba(44,26,50,0.16)');
    };

    if (kind === 'left') {
      // deep NE segment (right wing), jog wall at x=n.w, near NE segment
      neSegment(n.w, w, 0);
      nwSegment(n.w, 0, n.d, 0.92, 1.08);
      neSegment(0, n.w, n.d);
      nwSegment(0, n.d, d);
      bg.line(...P(w, 0, WALL_H), ...P(w, 0, 0), PAL.outline);
      bg.line(...P(n.w, 0, WALL_H), ...P(n.w, 0, 0), shade(PAL.wall, 0.7));
      bg.line(...P(0, n.d, WALL_H), ...P(0, n.d, 0), shade(PAL.wall, 0.66));
    } else if (kind === 'top') {
      // deep NE segment (left of the notch), jog wall, near NE segment
      neSegment(0, n.x, 0);
      nwSegment(n.x, 0, n.d, 0.92, 1.08);
      neSegment(n.x, w, n.d);
      nwSegment(0, 0, d);
      bg.line(...P(w, n.d, WALL_H), ...P(w, n.d, 0), PAL.outline);
      bg.line(...P(n.x, 0, WALL_H), ...P(n.x, 0, 0), shade(PAL.wall, 0.7));
      bg.line(...P(0, 0, WALL_H), ...P(0, 0, 0), shade(PAL.wall, 0.66));
    } else {
      neSegment(0, w, 0);
      nwSegment(0, 0, d);
      bg.line(...P(w, 0, WALL_H), ...P(w, 0, 0), PAL.outline);
      bg.line(...P(0, 0, WALL_H), ...P(0, 0, 0), shade(PAL.wall, 0.66));
    }
    bg.line(...P(0, d, WALL_H), ...P(0, d, 0), PAL.outline);

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
    try { localStorage.setItem('pixel-home-layout-v8', JSON.stringify(this.layout)); } catch (e) { /* ignore */ }
  }

  loadLayout() {
    try {
      const raw = localStorage.getItem('pixel-home-layout-v8');
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
    // several furniture pieces can offer the same activity (two plants, two
    // chairs...): go to the nearest instance with a reachable approach tile
    const cands = Object.values(this.interactions)
      .filter(it => it.key === want && this.isWalkable(it.approach[0], it.approach[1]))
      .sort((a, b) => this.dist(a.approach) - this.dist(b.approach));
    if (cands.length) this.dispatch(cands[0].name);
    else if (cur) this.endActivity();
  }

  dist(tile) {
    return Math.abs(tile[0] - this.actor.pos[0]) + Math.abs(tile[1] - this.actor.pos[1]);
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
    const tile = this.tileFromEvent(e);
    // right-click on furniture always deletes it, even while a palette item
    // is selected; right-click on empty floor cancels placement/grab
    const item = tile ? this.itemAt(tile.gx, tile.gy) : null;
    if (item) {
      this.layout.furniture.splice(item.idx, 1);
      this.grabbed = null;
      this.rebuild();
      this.saveLayout();
    } else {
      this.ghost = null;
      this.grabbed = null;
    }
    if (this.ui.onEditStateChanged) this.ui.onEditStateChanged();
  }

  editClick(gx, gy) {
    if (this.ghost) {
      if (this.canPlace(this.ghost, gx, gy, -1, !!this.ghostFlip)) {
        this.layout.furniture.push({ type: this.ghost, x: gx, y: gy, flip: !!this.ghostFlip });
        this.rebuild();
        this.saveLayout();
      }
      return;
    }
    if (this.grabbed !== null) {
      const f = this.layout.furniture[this.grabbed];
      if (this.canPlace(f.type, gx, gy, this.grabbed, !!f.flip)) {
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

  // R in edit mode: mirror the grabbed item (or the palette ghost) around
  // the screen-vertical axis.  One sprite yields two orientations; true
  // 4-way rotation would need per-direction art.
  rotateSelection() {
    if (this.mode !== 'edit') return false;
    if (this.grabbed !== null) {
      const f = this.layout.furniture[this.grabbed];
      if (!this.canPlace(f.type, f.x, f.y, this.grabbed, !f.flip)) return false;
      f.flip = !f.flip;
      this.rebuild();
      this.saveLayout();
      return true;
    }
    if (this.ghost) { this.ghostFlip = !this.ghostFlip; return true; }
    return false;
  }

  canPlace(type, x, y, ignoreIdx, flipped = false) {
    const def = CATALOG[type];
    const pw = flipped ? def.d : def.w, pd = flipped ? def.w : def.d;
    const { w, d } = this.layout.grid;
    if (x < 0 || y < 0 || x + pw > w || y + pd > d) return false;
    for (let dx = 0; dx < pw; dx += 1) for (let dy = 0; dy < pd; dy += 1) {
      if (this.inNotch(x + dx, y + dy)) return false;
    }
    if (def.decor) return true;
    for (let i = 0; i < this.layout.furniture.length; i += 1) {
      if (i === ignoreIdx) continue;
      const f = this.layout.furniture[i], fd = CATALOG[f.type];
      if (fd.decor) continue;
      const fw = f.flip ? fd.d : fd.w, fdd = f.flip ? fd.w : fd.d;
      if (x < f.x + fw && f.x < x + pw && y < f.y + fdd && f.y < y + pd) return false;
    }
    const ax = Math.round(this.actor.pos[0]), ay = Math.round(this.actor.pos[1]);
    if (ax >= x && ax < x + pw && ay >= y && ay < y + pd) return false;
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
      [0.0, [36, 40, 92], 0.56, true],
      [5.4, [36, 40, 92], 0.56, true],
      [7.0, [214, 150, 110], 0.20, false],
      [9.0, [255, 246, 222], 0.03, false],
      [16.0, [255, 238, 205], 0.07, false],
      [17.8, [210, 126, 94], 0.24, true],
      [19.4, [104, 76, 140], 0.38, true],
      [21.0, [52, 50, 104], 0.50, true],
      [24.0, [38, 42, 94], 0.54, true],
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

  // For two disjoint footprint boxes, decide draw order along the separating
  // axis (+x and +y both point toward the camera).  Returns true if b draws
  // in front of a, false if behind, null if undecidable (overlapping decor).
  static drawsAfter(a, b) {
    if (a.x + a.w <= b.x) return true;
    if (b.x + b.w <= a.x) return false;
    if (a.y + a.d <= b.y) return true;
    if (b.y + b.d <= a.y) return false;
    return null;
  }

  // Pairwise occlusion + topological sort.  A single scalar depth key is
  // wrong for L-shaped neighbours (e.g. a 2x3 bed sorts in front of the
  // nightstand beside its head and its art overhang clips the nightstand).
  // The ACTOR joins the sort as a regular node (a small box at her feet):
  // inserting her at a single index in a pre-sorted list is only correct
  // against one neighbour and used to clip when she walked between items.
  sortedForDraw() {
    const nodes = [...this.items, { isActor: true, ...this.actorBox() }];
    const n = nodes.length;
    const key = it => it.x + it.w + it.y + it.d;
    const after = Array.from({ length: n }, () => []);
    const indeg = new Array(n).fill(0);
    for (let i = 0; i < n; i += 1) {
      for (let j = i + 1; j < n; j += 1) {
        const a = nodes[i], b = nodes[j];
        const r = b.isActor ? this.actorDrawsAfter(a, b)
          : a.isActor ? Engine.flip3(this.actorDrawsAfter(b, a))
            : Engine.drawsAfter(a, b);
        if (r === true) { after[i].push(j); indeg[j] += 1; }
        else if (r === false) { after[j].push(i); indeg[i] += 1; }
      }
    }
    const out = [];
    const used = new Array(n).fill(false);
    for (let step = 0; step < n; step += 1) {
      let pick = -1;
      for (let i = 0; i < n; i += 1) {
        if (used[i] || indeg[i] > 0) continue;
        if (pick === -1 || key(nodes[i]) < key(nodes[pick])) pick = i;
      }
      if (pick === -1) { // occlusion cycle: break it at the shallowest node
        for (let i = 0; i < n; i += 1) if (!used[i] && (pick === -1 || key(nodes[i]) < key(nodes[pick]))) pick = i;
      }
      used[pick] = true;
      out.push(nodes[pick]);
      for (const j of after[pick]) indeg[j] -= 1;
    }
    return out;
  }

  static flip3(r) { return r === null ? null : !r; }

  actorBox() {
    const [ax, ay] = this.actorWorldPos();
    return { x: ax - 0.32, y: ay - 0.32, w: 0.64, d: 0.64 };
  }

  // true = actor draws after (in front of) the item, false = behind,
  // null = unordered
  actorDrawsAfter(item, abox) {
    const act = this.actor.activity;
    if (act && act.pose === 'sit' && act.at && item.type === 'chair'
        && Math.floor(act.at[0]) === item.x && Math.floor(act.at[1]) === item.y) {
      // Split-layer chair: she is composited inline (cushion -> her -> back
      // panel), her standalone node draws after the chair body nominally.
      if (item.baked.front) return true;
      // Single-layer chair art: with her back to the camera the whole chair
      // draws over her, facing the camera she covers the seat.
      return !(this.actor.facing === 'upRight' || this.actor.facing === 'upLeft');
    }
    // On top of an item (bed, sofa, stool): she draws over its body sprite.
    if (this.actorOn(item)) return true;
    const r = Engine.drawsAfter(item, abox);
    if (r !== null) return r;
    // Overlapping boxes (walking right past a tile edge): compare the
    // camera-facing corners so the winner is the one closer to the camera.
    return (abox.x + abox.w + abox.y + abox.d) >= (item.x + item.w + item.y + item.d);
  }

  // The item she is currently "on": her seat/bed, including a chair she was
  // snapped onto by a table interaction (act.item is the table then).
  actorOn(item) {
    const act = this.actor.activity;
    if (!act || !act.at) return false;
    if (act.item === item) return true;
    return act.pose === 'sit' && item.type === 'chair'
      && Math.floor(act.at[0]) === item.x && Math.floor(act.at[1]) === item.y;
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
    const mk = (kind, color, vx, vy, life, size = 2) => this.effects.push({ kind, x: anchor[0] + (rng() * 16 - 8), y: anchor[1] + (rng() * 6 - 2), vx: vx * 2, vy: vy * 2, life, t: 0, color, size: size * 2 });
    switch (act.effect) {
      case 'steam': mk('rise', 'rgba(250,244,228,0.95)', (rng() - 0.5) * 3, -9, 1.5, 3); break;
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
        ctx.fillRect(x, y, 5, 2); ctx.fillRect(x + 2, y + 2, 2, 2); ctx.fillRect(x, y + 4, 5, 2);
      } else if (fx.kind === 'note') {
        ctx.fillRect(x, y, 2, 8); ctx.fillRect(x - 2, y + 6, 4, 4); ctx.fillRect(x + 2, y, 4, 2);
      } else if (fx.kind === 'drop') {
        ctx.fillRect(x, y, 2, 4);
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
      const breath = Math.sin(now / 900);
      const overlay = this.sprites.sleepOverlay;
      if (overlay) {
        // AI sleeping figure (duvet + head on pillow), anchored by the head.
        // The art lies head upper-right / feet lower-left, matching a bed
        // whose headboard is toward -y; mirror it for flipped beds.
        const flip = Boolean(act.item && act.item.flip);
        let sf = overlay.surface, hx = overlay.headAt[0];
        if (flip) {
          if (!overlay.mirrored) overlay.mirrored = overlay.surface.mirrored();
          sf = overlay.mirrored;
          hx = sf.w - 1 - overlay.headAt[0];
        }
        const dx = Math.round(px - hx);
        const dy = Math.round(py - overlay.headAt[1] - 8 + breath * 0.8);
        ctx.drawImage(sf.canvas, dx, dy);
        return;
      }
      const head = this.sprites.sleepHead;
      if (act.lumpAt) {
        // procedural fallback: body bump under the quilt as grid polygons
        const [cx, cy] = act.lumpAt;
        const along = act.lumpAxis === 'y' ? [0, 1] : [1, 0];
        const across = act.lumpAxis === 'y' ? [1, 0] : [0, 1];
        const lift = 0.16 + breath * 0.012;
        const pt = (u, v, z) => this.project(cx + along[0] * u + across[0] * v, cy + along[1] * u + across[1] * v, a.z + z);
        const quilt = '#cd937e';   // sampled from the AI quilt's dusty rose
        const topPts = [pt(-0.62, -0.2, lift), pt(0.62, -0.2, lift), pt(0.78, 0.06, lift * 0.5), pt(-0.78, 0.06, lift * 0.5)];
        ctx.fillStyle = shade(quilt, 1.04);
        ctx.beginPath(); ctx.moveTo(...topPts[0]); for (const p of topPts.slice(1)) ctx.lineTo(...p); ctx.closePath(); ctx.fill();
        const sidePts = [pt(-0.78, 0.06, lift * 0.5), pt(0.78, 0.06, lift * 0.5), pt(0.66, 0.3, 0.02), pt(-0.66, 0.3, 0.02)];
        ctx.fillStyle = shade(quilt, 0.86);
        ctx.beginPath(); ctx.moveTo(...sidePts[0]); for (const p of sidePts.slice(1)) ctx.lineTo(...p); ctx.closePath(); ctx.fill();
        ctx.strokeStyle = shade(quilt, 0.66); ctx.lineWidth = 1;
        ctx.beginPath(); ctx.moveTo(...topPts[0]); ctx.lineTo(...topPts[1]); ctx.stroke();
      }
      ctx.drawImage(head.canvas, Math.round(px - head.w / 2), Math.round(py - head.h + 4 + breath * 0.8));
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
      ctx.moveTo(px - 16, py); ctx.lineTo(px, py - 8); ctx.lineTo(px + 16, py); ctx.lineTo(px, py + 8);
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
      ctx.fillRect(x + sprite.w / 2 - 5, y + Math.round(sprite.h * 0.55), 9, 7);
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

    // 3) depth-sorted furniture + actor (as one sort node), with contact
    // shadows and front overlays
    const sorted = this.sortedForDraw();
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
        if (it.isActor || it.def.decor || it.type === 'floorLamp') continue;
        const ghosted = this.mode === 'edit' && this.grabbed === it.idx;
        shadowDiamond(it, 0.16, ghosted ? 0.1 : 0.18);
        shadowDiamond(it, 0.05, ghosted ? 0.08 : 0.14);
      }
      ctx.restore();
      ctx.globalAlpha = 1;
    }
    // procedural seats keep a front overlay (armrest) that must cover the
    // sitter: body -> actor -> front, with the actor's own node skipped
    const seatWithFront = sorted.find(it => !it.isActor && it.baked.front && this.actorOn(it));
    for (const it of sorted) {
      if (it.isActor) { if (!seatWithFront) this.drawActor(ctx, now); continue; }
      const ghosted = this.mode === 'edit' && this.grabbed === it.idx;
      if (ghosted) ctx.globalAlpha = 0.35;
      blitItem(it, 'body');
      if (it.baked.front) {
        if (it === seatWithFront) this.drawActor(ctx, now);
        blitItem(it, 'front');
      }
      if (ghosted) ctx.globalAlpha = 1;
    }

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
      const nn = this.layout.notch;
      const kind = this.notchKind();
      const low = (x, y) => { const p = this.project(x, y, 0); return [p[0], p[1] + 7]; };
      const sil = kind === 'left' ? [
        this.project(-capD, gd + capD, -0.12),
        this.project(-capD, nn.d, -0.12),
        this.project(-capD, nn.d, WALL_H),
        this.project(0, nn.d - capD, WALL_H),
        this.project(nn.w + capD, nn.d - capD, WALL_H),
        this.project(nn.w + capD, -capD, WALL_H),
        this.project(gw + capD, -capD, WALL_H),
        this.project(gw + capD, -capD, -0.12),
        this.project(gw, 0, -0.12),
        low(gw, gd), low(0, gd),
      ] : [
        this.project(-capD, gd + capD, -0.12),
        this.project(-capD, 0, -0.12),
        this.project(-capD, 0, WALL_H),
        this.project(0, -capD, WALL_H),
        ...(kind === 'top' ? [
          this.project(nn.x + capD, -capD, WALL_H),
          this.project(nn.x + capD, nn.d - capD, WALL_H),
          this.project(gw + capD, nn.d - capD, WALL_H),
          this.project(gw + capD, nn.d - capD, -0.12),
          this.project(gw, nn.d, -0.12),
        ] : [
          this.project(gw + capD, -capD, WALL_H),
          this.project(gw + capD, -capD, -0.12),
          this.project(gw, 0, -0.12),
        ]),
        ...(kind === 'south'
          ? [low(gw, nn.y), low(nn.x, nn.y), low(nn.x, gd), low(0, gd)]
          : [low(gw, gd), low(0, gd)]),
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
        const grad = ctx.createRadialGradient(ax, ay - 32, 4, ax, ay - 32, 44);
        grad.addColorStop(0, 'rgba(150,220,205,0.30)');
        grad.addColorStop(1, 'rgba(0,0,0,0)');
        ctx.fillStyle = grad;
        ctx.fillRect(ax - 48, ay - 80, 96, 96);
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
        const flip = this.grabbed !== null ? !!this.layout.furniture[this.grabbed]?.flip : !!this.ghostFlip;
        if (type) color = this.canPlace(type, gx, gy, this.grabbed ?? -1, flip) ? 'rgba(140,230,150,0.55)' : 'rgba(235,90,90,0.6)';
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

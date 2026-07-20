(function () {
  'use strict';

  const DIRECTIONS = [[1, 0], [-1, 0], [0, 1], [0, -1]];

  class TileRoomRuntime {
    static occupancyCells(object) {
      if (object.occupancy === 'decor') return [];
      const t = object.transform || {}, cells = [];
      for (let x = Math.floor(t.x); x < Math.ceil(t.x + t.width); x += 1) {
        for (let y = Math.floor(t.y); y < Math.ceil(t.y + t.depth); y += 1) cells.push([x, y]);
      }
      return cells;
    }

    static edgeKey(a, b) {
      const [first, second] = `${a[0]},${a[1]}` < `${b[0]},${b[1]}` ? [a, b] : [b, a];
      return `${first[0]},${first[1]}|${second[0]},${second[1]}`;
    }

    static wallEdges(scene) {
      const edges = new Set();
      for (const wall of scene.walls || []) {
        const [[x1, y1], [x2, y2]] = [wall.from, wall.to];
        if (y1 === y2 && y1 > 0 && y1 < scene.grid.depth) for (let x = Math.floor(Math.min(x1, x2)); x < Math.ceil(Math.max(x1, x2)); x += 1) edges.add(TileRoomRuntime.edgeKey([x, y1 - 1], [x, y1]));
        else if (x1 === x2 && x1 > 0 && x1 < scene.grid.width) for (let y = Math.floor(Math.min(y1, y2)); y < Math.ceil(Math.max(y1, y2)); y += 1) edges.add(TileRoomRuntime.edgeKey([x1 - 1, y], [x1, y]));
      }
      return edges;
    }

    static async load({canvas, bundleUrl, labels}) {
      const response = await fetch(bundleUrl, {cache:'no-store'});
      if (!response.ok) throw new Error(`格驱动房间资源读取失败 (${response.status})`);
      const scene = await response.json();
      TileRoomRuntime.validate(scene);
      const runtime = new TileRoomRuntime(canvas, scene, labels);
      await runtime.preload();
      return runtime;
    }

    static validate(scene) {
      const errors = [];
      if (scene.renderer !== 'tile-v1') errors.push('renderer must be tile-v1');
      if (!scene.grid || !Number.isInteger(scene.grid.width) || !Number.isInteger(scene.grid.depth)) errors.push('grid must define integer width/depth');
      const projection = scene.projection || {};
      if (!Array.isArray(projection.tile) || projection.tile[0] !== 128 || projection.tile[1] !== 64 || projection.height !== 64) errors.push('projection must use the fixed 2:1 128×64 contract');
      const objects = scene.objects || [], byId = new Map();
      const occupied = new Set();
      for (const object of objects) {
        if (!object.id || byId.has(object.id)) errors.push(`duplicate object ${object.id || '<unknown>'}`);
        byId.set(object.id, object);
        const transform = object.transform || {};
        for (const key of ['x', 'y', 'z', 'width', 'depth', 'height']) if (!Number.isFinite(transform[key])) errors.push(`object ${object.id} has invalid ${key}`);
        if (transform.width <= 0 || transform.depth <= 0 || transform.height <= 0) errors.push(`object ${object.id} must have positive dimensions`);
        if (!scene.materials?.[object.material]) errors.push(`object ${object.id} references unknown material`);
        const derivedCollider = TileRoomRuntime.occupancyCells(object);
        const declaredCollider = object.collider || [];
        if (new Set(declaredCollider.map(point => `${point[0]},${point[1]}`)).size !== new Set(derivedCollider.map(point => `${point[0]},${point[1]}`)).size || declaredCollider.some(point => !derivedCollider.some(cell => cell[0] === point[0] && cell[1] === point[1]))) errors.push(`object ${object.id} collider must match its occupied floor tiles`);
        for (const point of derivedCollider) {
          const key = `${point[0]},${point[1]}`;
          if (point[0] < 0 || point[1] < 0 || point[0] >= scene.grid.width || point[1] >= scene.grid.depth) errors.push(`object ${object.id} collider is outside grid`);
          if (occupied.has(key)) errors.push(`collider overlap at ${key}`);
          occupied.add(key);
        }
      }
      const navigationBlocked = occupied;
      for (const [name, interaction] of Object.entries(scene.interactions || {})) {
        if (!byId.has(interaction.object)) errors.push(`interaction ${name} references unknown object`);
        const [x, y] = interaction.approach || [];
        if (!Number.isInteger(x) || !Number.isInteger(y) || x < 0 || y < 0 || x >= scene.grid.width || y >= scene.grid.depth) errors.push(`interaction ${name} approach is outside grid`);
        if (navigationBlocked.has(`${x},${y}`)) errors.push(`interaction ${name} approach is blocked`);
      }
      for (const [name, route] of Object.entries(scene.routes || {})) {
        if (!Array.isArray(route) || route.length < 2) { errors.push(`route ${name} must contain two or more points`); continue; }
        route.forEach((point, index) => {
          const [x, y] = point || [];
          if (!Number.isInteger(x) || !Number.isInteger(y) || x < 0 || y < 0 || x >= scene.grid.width || y >= scene.grid.depth) errors.push(`route ${name} point ${index} is outside grid`);
          if (navigationBlocked.has(`${x},${y}`)) errors.push(`route ${name} point ${index} is blocked`);
          if (index && Math.abs(x - route[index - 1][0]) + Math.abs(y - route[index - 1][1]) !== 1) errors.push(`route ${name} has a non-adjacent step at ${index}`);
          if (index && TileRoomRuntime.wallEdges(scene).has(TileRoomRuntime.edgeKey(route[index - 1], point))) errors.push(`route ${name} crosses a wall at ${index}`);
        });
      }
      if (errors.length) throw new Error(`Invalid tile room: ${errors.join('; ')}`);
    }

    constructor(canvas, scene, labels) {
      this.canvas = canvas;
      this.ctx = canvas.getContext('2d');
      this.scene = scene;
      this.labels = labels || {};
      this.images = {};
      this.running = false;
      this.frameRequest = null;
      this.editor = null;
      this.actor = {position:[...scene.anchors.rug], path:[], target:null, action:'idle', pose:'idle', facing:'downLeft', interaction:null, scene:{location:'rug', action:'idle'}, walked:0, lastTime:0, tourRoute:null, tourForward:true};
      this.stage = {scale:1, ox:0, oy:0};
      this.objectById = new Map(scene.objects.map(object => [object.id, object]));
      this.blocked = new Set(scene.objects.flatMap(object => TileRoomRuntime.occupancyCells(object)).map(point => this.key(point)));
      this.wallEdges = TileRoomRuntime.wallEdges(scene);
    }

    async preload() {
      const assets = {walk:this.scene.sprites.walk.url};
      for (const [name, pose] of Object.entries(this.scene.sprites.poses || {})) assets[`pose:${name}`] = pose.url;
      for (const [name, material] of Object.entries(this.scene.materials)) if (material.texture) assets[`material:${name}`] = material.texture;
      await Promise.all(Object.entries(assets).map(([key, url]) => this.loadImage(key, url)));
    }

    async loadImage(key, url) {
      if (this.images[key]) return;
      await new Promise((resolve, reject) => {
        const image = new Image();
        image.onload = () => { this.images[key] = image; resolve(); };
        image.onerror = () => reject(new Error(`格驱动房间图片加载失败: ${key}`));
        image.src = url;
      });
    }

    preloadArtDraft() { return Promise.resolve(); }
    key(point) { return `${Math.round(point[0])},${Math.round(point[1])}`; }
    inGrid(point) { return point[0] >= 0 && point[1] >= 0 && point[0] < this.scene.grid.width && point[1] < this.scene.grid.depth; }

    fitStage() {
      const {width, depth} = this.scene.grid, {tile, height} = this.scene.projection;
      const sceneWidth = (width + depth) * tile[0] / 2;
      const sceneHeight = (width + depth) * tile[1] / 2 + height * 2.8;
      // Keep the affine basis on a binary pixel lattice.  Without this small
      // quantization, adding a tile delta to a screen-space origin can round
      // by one ulp; subtracting the two projected points then no longer
      // recovers the declared 128x64 basis exactly.  The lattice is finer
      // than a visible pixel and is shared by scale and origin so inverse
      // projection remains consistent.
      const pixelQuantum = 4096;
      const quantize = value => Math.round(value * pixelQuantum) / pixelQuantum;
      this.stage.scale = quantize(Math.min((this.canvas.width - 64) / sceneWidth, (this.canvas.height - 52) / sceneHeight));
      this.stage.ox = this.canvas.width / 2;
      this.stage.oy = quantize(42 + height * 2.7 * this.stage.scale);
    }

    project(point) {
      const [x, y, z=0] = point, [tileWidth, tileHeight] = this.scene.projection.tile;
      return [this.stage.ox + (x - y) * tileWidth / 2 * this.stage.scale, this.stage.oy + ((x + y) * tileHeight / 2 - z * this.scene.projection.height) * this.stage.scale];
    }

    screenToGrid(point) {
      const [sx, sy] = [(point[0] - this.stage.ox) / this.stage.scale, (point[1] - this.stage.oy) / this.stage.scale];
      return [(sx / 64 + sy / 32) / 2, (sy / 32 - sx / 64) / 2];
    }

    canPlace(object, x, y) {
      if (object.occupancy === 'decor') return this.inGrid([x, y]);
      const own = new Set(TileRoomRuntime.occupancyCells(object).map(point => this.key(point)));
      const width = Math.ceil(object.transform.width), depth = Math.ceil(object.transform.depth);
      for (let dx = 0; dx < width; dx += 1) for (let dy = 0; dy < depth; dy += 1) {
        const point = [x + dx, y + dy], key = this.key(point);
        if (!this.inGrid(point) || (this.blocked.has(key) && !own.has(key))) return false;
      }
      return true;
    }

    // The painter order is deliberately independent of pixel art bounds:
    // world x+y is the sole spatial depth truth; z only changes projection.
    depth(point, bias=0) { return Math.round((point[0] + point[1]) * 1000 + bias); }
    directionFor(dx, dy) { if (dx > .01) return 'downRight'; if (dx < -.01) return 'upLeft'; if (dy > .01) return 'downLeft'; if (dy < -.01) return 'upRight'; return this.actor.facing; }

    pathfind(start, target) {
      const startKey = this.key(start), targetKey = this.key(target);
      if (!this.inGrid(target) || this.blocked.has(targetKey)) return [];
      const open = [[Math.round(start[0]), Math.round(start[1])]], came = new Map([[startKey, null]]), cost = new Map([[startKey, 0]]);
      const heuristic = point => Math.abs(point[0] - target[0]) + Math.abs(point[1] - target[1]);
      while (open.length) {
        open.sort((a, b) => (cost.get(this.key(a)) + heuristic(a)) - (cost.get(this.key(b)) + heuristic(b)));
        const current = open.shift(), currentKey = this.key(current);
        if (currentKey === targetKey) break;
        for (const [dx, dy] of DIRECTIONS) {
          const next = [current[0] + dx, current[1] + dy], nextKey = this.key(next);
          if (!this.inGrid(next) || this.blocked.has(nextKey) || this.wallEdges.has(TileRoomRuntime.edgeKey(current, next))) continue;
          const candidate = cost.get(currentKey) + 1;
          if (candidate < (cost.get(nextKey) ?? Infinity)) { came.set(nextKey, current); cost.set(nextKey, candidate); if (!open.some(point => this.key(point) === nextKey)) open.push(next); }
        }
      }
      if (!came.has(targetKey)) return [];
      const path = []; let current = target;
      while (this.key(current) !== startKey) { path.unshift([...current, 0]); current = came.get(this.key(current)); }
      return path;
    }

    interactionFor(scene) {
      const action = this.scene.actions?.[scene.action] || {};
      return this.scene.interactions?.[action.interaction] || Object.values(this.scene.interactions).find(item => item.location === scene.location && item.action === scene.action) || null;
    }

    setActor(scene) {
      const interaction = this.interactionFor(scene), target = interaction?.approach || this.scene.anchors[scene.location] || this.scene.anchors.rug;
      this.actor.scene = scene; this.actor.interaction = interaction; this.actor.action = scene.action || 'idle'; this.actor.pose = interaction?.pose || 'idle'; this.actor.target = [...target]; this.actor.tourRoute = null;
      if (this.key(this.actor.position) !== this.key(target)) {
        this.actor.path = this.pathfind(this.actor.position, target);
        if (this.actor.path.length) this.actor.action = 'walk';
        else { this.actor.interaction = null; this.actor.action = 'idle'; this.actor.pose = 'idle'; }
      }
      else this.actor.facing = interaction?.facing || this.actor.facing;
    }

    activatePreview(params) {
      const demo = params.get('demo');
      if (demo === 'tile-editor') { if (!this.editor) this.editor = new TileRoomEditor(this); this.editor.mount(); return {status:'格驱动摆放器 · 只改预览', gameAction:'TileRoom Editor · 不写入 daemon'}; }
      const preview = name => ({location:'rug', action:name, expression:'neutral', time_of_day:'day'});
      if (demo === 'activity') { const spot = params.get('spot') || 'study', interaction = this.scene.interactions[spot] || this.scene.interactions.study; this.actor.position = [...interaction.approach, 0]; this.actor.path = []; this.actor.interaction = interaction; this.actor.action = interaction.action; this.actor.pose = interaction.pose; this.actor.facing = interaction.facing; this.actor.scene = preview(interaction.action); return {status:`格驱动动作巡检 · ${spot} · 不写入 daemon`, gameAction:`动作巡检 · ${spot}`}; }
      if (demo === 'tour') { const route = this.scene.routes.tour; this.actor.position = [...route[0], 0]; this.actor.path = route.slice(1).map(point => [...point, 0]); this.actor.action = 'walk'; this.actor.pose = 'idle'; this.actor.tourRoute = route; this.actor.tourForward = true; this.actor.scene = preview('walk_out'); return {status:'格驱动小屋巡回 · 不写入 daemon', gameAction:'格驱动小屋巡回'}; }
      return null;
    }

    start() { if (!this.running) { this.running = true; this.frameRequest = requestAnimationFrame(now => this.loop(now)); } }
    stop() { this.running = false; if (this.frameRequest !== null) cancelAnimationFrame(this.frameRequest); this.frameRequest = null; }

    polygon(points, fill, stroke='#3b2925') { const ctx = this.ctx; ctx.beginPath(); ctx.moveTo(...points[0]); for (const point of points.slice(1)) ctx.lineTo(...point); ctx.closePath(); ctx.fillStyle = fill; ctx.fill(); ctx.strokeStyle = stroke; ctx.lineWidth = Math.max(1, this.stage.scale); ctx.stroke(); }
    topFace(transform) { const {x, y, z, width, depth, height} = transform; return [this.project([x,y,z+height]), this.project([x+width,y,z+height]), this.project([x+width,y+depth,z+height]), this.project([x,y+depth,z+height])]; }

    materialFill(materialName, role, seed=0) {
      const material = this.scene.materials[materialName], color = material[role];
      const ctx = this.ctx;
      ctx.fillStyle = color;
      return material;
    }

    drawFloorTile(x, y) {
      const material = this.materialFill(this.scene.floor.material, 'top');
      const points = [this.project([x,y,0]), this.project([x+1,y,0]), this.project([x+1,y+1,0]), this.project([x,y+1,0])];
      this.polygon(points, material.top, '#6f4938');
      const texture = this.images[`material:${this.scene.floor.material}`];
      if (texture) {
        const ctx = this.ctx; ctx.save(); ctx.beginPath(); ctx.moveTo(...points[0]); for (const point of points.slice(1)) ctx.lineTo(...point); ctx.closePath(); ctx.clip();
        ctx.globalAlpha = .18; ctx.imageSmoothingEnabled = false; ctx.drawImage(texture, points[0][0] - 64 * this.stage.scale, points[0][1] - 32 * this.stage.scale, 128 * this.stage.scale, 64 * this.stage.scale);
        ctx.restore();
      }
      const [cx, cy] = this.project([x+.5, y+.5, .01]);
      this.ctx.save(); this.ctx.fillStyle = material.detail; this.ctx.globalAlpha = .23;
      this.ctx.fillRect(cx - 12 * this.stage.scale, cy - 1, 24 * this.stage.scale, Math.max(1, this.stage.scale)); this.ctx.restore();
    }

    drawBoxFace(transform, material, kind, part) {
      const t = transform, top = this.topFace(t);
      const lowA = this.project([t.x, t.y + t.depth, t.z]), lowB = this.project([t.x + t.width, t.y + t.depth, t.z]);
      const lowC = this.project([t.x + t.width, t.y, t.z]), lowD = this.project([t.x, t.y, t.z]);
      if (part === 'top') { this.polygon(top, material.top); this.drawBoxDetail(top, material.detail, kind); return; }
      if (part === 'left') this.polygon([top[3], top[2], lowB, lowA], material.left);
      if (part === 'right') this.polygon([top[1], top[2], lowB, lowC], material.right);
    }

    boxSlices(start, length) {
      const slices = [];
      for (let offset = 0; offset < length; offset += 1) slices.push([start + offset, Math.min(1, length - offset)]);
      return slices;
    }

    objectRenderParts(object, stableIndex) {
      const t = object.transform, material = this.scene.materials[object.material], parts = [];
      // A face spans a range of x+y values. Split it into tile-sized strips so
      // the avatar can be correctly interleaved with a wide desk, bed or sofa.
      for (const [x, width] of this.boxSlices(t.x, t.width)) for (const [y, depth] of this.boxSlices(t.y, t.depth)) {
        const tile = {...t, x, y, width, depth};
        parts.push({depth:this.depth([x + width / 2, y + depth / 2]), order:1, stableIndex, draw:() => this.drawBoxFace(tile, material, object.kind, 'top')});
      }
      for (const [x, width] of this.boxSlices(t.x, t.width)) {
        const strip = {...t, x, width};
        parts.push({depth:this.depth([x + width, t.y + t.depth]), order:2, stableIndex, draw:() => this.drawBoxFace(strip, material, object.kind, 'left')});
      }
      for (const [y, depth] of this.boxSlices(t.y, t.depth)) {
        const strip = {...t, y, depth};
        parts.push({depth:this.depth([t.x + t.width, y + depth]), order:3, stableIndex, draw:() => this.drawBoxFace(strip, material, object.kind, 'right')});
      }
      return parts;
    }

    drawBoxDetail(top, detail, kind) {
      if (!['desk','table','counter','bed','sofa','vanity'].includes(kind)) return;
      const ctx = this.ctx; ctx.save(); ctx.fillStyle = detail; ctx.globalAlpha = .32;
      const cx = top.reduce((sum, point) => sum + point[0], 0) / 4, cy = top.reduce((sum, point) => sum + point[1], 0) / 4;
      ctx.fillRect(cx - 6 * this.stage.scale, cy - 2 * this.stage.scale, 12 * this.stage.scale, 4 * this.stage.scale); ctx.restore();
    }

    drawWalls() {
      for (const wall of this.scene.walls) {
        const material = this.scene.materials[wall.material], [x1,y1] = wall.from, [x2,y2] = wall.to, z = wall.height;
        const lowA = this.project([x1,y1,0]), lowB = this.project([x2,y2,0]), highB = this.project([x2,y2,z]), highA = this.project([x1,y1,z]);
        this.polygon([lowA, lowB, highB, highA], material.left, '#563d35');
        const detailY = (lowA[1] + lowB[1] + highA[1] + highB[1]) / 4;
        this.ctx.save(); this.ctx.strokeStyle = material.detail; this.ctx.globalAlpha = .25; this.ctx.beginPath(); this.ctx.moveTo(lowA[0], detailY); this.ctx.lineTo(lowB[0], detailY); this.ctx.stroke(); this.ctx.restore();
      }
    }

    drawWindow(object) {
      const t = object.transform, material = this.scene.materials[object.material];
      const face = [this.project([t.x,t.y,t.z]), this.project([t.x+t.width,t.y,t.z]), this.project([t.x+t.width,t.y,t.z+t.height]), this.project([t.x,t.y,t.z+t.height])];
      this.polygon(face, material.top, '#3b5961');
      const ctx = this.ctx; ctx.save(); ctx.strokeStyle = material.detail; ctx.lineWidth = Math.max(1, 2 * this.stage.scale); ctx.beginPath();
      const [leftBottom, rightBottom, rightTop, leftTop] = face;
      ctx.moveTo((leftBottom[0] + rightBottom[0]) / 2, (leftBottom[1] + rightBottom[1]) / 2); ctx.lineTo((leftTop[0] + rightTop[0]) / 2, (leftTop[1] + rightTop[1]) / 2);
      ctx.moveTo((leftBottom[0] + leftTop[0]) / 2, (leftBottom[1] + leftTop[1]) / 2); ctx.lineTo((rightBottom[0] + rightTop[0]) / 2, (rightBottom[1] + rightTop[1]) / 2); ctx.stroke(); ctx.restore();
    }

    actorDepth() {
      // No interaction override: the avatar's feet decide visibility in every state.
      return this.depth(this.actor.position);
    }

    drawActor(now) {
      const actor = this.actor, walking = actor.action === 'walk', pose = this.scene.sprites.poses[actor.pose], position = actor.position;
      let image = this.images.walk, sx = 0, sy = 0, sw, sh, display = this.scene.sprites.walk.display, anchor = this.scene.sprites.walk.anchor || [.5, 1];
      if (!walking && pose && this.images[`pose:${actor.pose}`]) { image = this.images[`pose:${actor.pose}`]; [sx,sy,sw,sh] = pose.crop; display = pose.display; anchor = pose.anchor || anchor; }
      else { const walk = this.scene.sprites.walk, column = walk.columns[actor.facing] ?? 0; sw = image.width / Object.keys(walk.columns).length; sh = image.height / walk.frames; sx = column * sw; sy = (walking ? Math.floor(actor.walked * 8) % walk.frames : 0) * sh; }
      const [px, py] = this.project(position), [dw,dh] = display, x = px - dw * this.stage.scale * anchor[0], y = py - dh * this.stage.scale * anchor[1];
      this.ctx.save(); this.ctx.imageSmoothingEnabled = false; this.ctx.drawImage(image, sx, sy, sw, sh, x, y, dw * this.stage.scale, dh * this.stage.scale); this.ctx.restore();
      this.drawEffect(px, py, now);
    }

    drawEffect(x, y, now) {
      const action = this.actor.scene.action, effect = this.scene.actions[action]?.effect, pulse = Math.sin(now / 220);
      const ctx = this.ctx; ctx.save(); ctx.imageSmoothingEnabled = false;
      if (effect === 'focus') { ctx.fillStyle = '#bde5d8'; ctx.globalAlpha = .28; ctx.fillRect(x - 20, y - 54, 40, 24); }
      if (effect === 'steam') { ctx.strokeStyle = '#f6ead6'; ctx.lineWidth = 2; for (const offset of [-7,7]) { ctx.beginPath(); ctx.arc(x + offset, y - 43 + pulse * 4, 5, 0, Math.PI); ctx.stroke(); } }
      if (effect === 'social' || effect === 'phone') { ctx.fillStyle = effect === 'phone' ? '#9fe5e1' : '#ffe6a4'; ctx.fillRect(x + 18, y - 55 + pulse * 3, 5, 5); }
      if (effect === 'sleep') { ctx.fillStyle = '#f4dbbd'; ctx.font = `${16 * this.stage.scale}px Pixel, sans-serif`; ctx.fillText('z', x + 14, y - 65 + pulse * 4); }
      if (effect === 'water') { ctx.fillStyle = '#aee6e9'; for (const offset of [-4,4]) ctx.fillRect(x + offset, y - 38 + Math.abs(pulse) * 8, 2, 5); }
      if (effect === 'sparkles') { ctx.fillStyle = '#ffe59d'; for (const [dx,dy] of [[-12,-36],[8,-46],[18,-30]]) ctx.fillRect(x+dx, y+dy+pulse*4, 4, 4); }
      ctx.restore();
    }

    drawRibbon() { const ctx = this.ctx, scene = this.actor.scene; ctx.save(); ctx.fillStyle = 'rgba(59,42,36,.88)'; ctx.fillRect(24, 18, 250, 38); ctx.strokeStyle = '#e7c076'; ctx.lineWidth = 2; ctx.strokeRect(24,18,250,38); ctx.fillStyle='#fff2d6'; ctx.font='12px Pixel,sans-serif'; ctx.fillText(this.labels[scene.location] || '格驱动小屋', 37, 35); ctx.fillStyle='#f3ce86'; ctx.fillText(this.labels[scene.action] || scene.action || '待机', 37, 51); ctx.restore(); }

    draw(now) {
      this.fitStage(); const ctx = this.ctx; ctx.imageSmoothingEnabled = false; ctx.clearRect(0,0,this.canvas.width,this.canvas.height); ctx.fillStyle='#201817'; ctx.fillRect(0,0,this.canvas.width,this.canvas.height);
      for (let sum=0; sum < this.scene.grid.width + this.scene.grid.depth - 1; sum += 1) for (let x=0; x<this.scene.grid.width; x += 1) { const y = sum - x; if (y >= 0 && y < this.scene.grid.depth) this.drawFloorTile(x,y); }
      this.drawWalls();
      for (const object of this.scene.objects) if (object.kind === 'window') this.drawWindow(object);
      const parts = [];
      this.scene.objects.forEach((object, index) => { if (object.kind !== 'window') parts.push(...this.objectRenderParts(object, index)); });
      parts.push({depth:this.actorDepth(), order:2.5, stableIndex:-1, draw:() => this.drawActor(now)});
      for (const part of parts.sort((a,b) => a.depth-b.depth || a.order-b.order || a.stableIndex-b.stableIndex)) part.draw();
      this.drawRibbon(); if (this.editor) this.editor.drawOverlay();
      this.canvas.dataset.roomRenderReady = 'true'; this.canvas.dataset.roomObjectCount = String(this.scene.objects.length); this.canvas.dataset.roomVisibleObjects = JSON.stringify(this.scene.objects.map(object => object.id));
    }

    loop(now) {
      const actor = this.actor, dt = Math.min(.05, (now - (actor.lastTime || now)) / 1000); actor.lastTime = now;
      if (actor.path.length) { const target = actor.path[0], dx = target[0]-actor.position[0], dy=target[1]-actor.position[1], distance=Math.hypot(dx,dy), speed=2.4;
        if (distance <= speed*dt) { actor.position = target; actor.path.shift(); if (!actor.path.length && actor.tourRoute) { actor.tourForward=!actor.tourForward; const route=actor.tourForward?actor.tourRoute.slice(1):actor.tourRoute.slice(0,-1).reverse(); actor.path=route.map(point=>[...point,0]); } if (!actor.path.length) { actor.action=actor.scene.action || 'idle'; actor.facing=actor.interaction?.facing || actor.facing; } }
        else { actor.position=[actor.position[0]+dx/distance*speed*dt,actor.position[1]+dy/distance*speed*dt,0]; actor.facing=this.directionFor(dx,dy); actor.walked+=speed*dt; }
      }
      this.draw(now); if (this.running) this.frameRequest=requestAnimationFrame(next=>this.loop(next));
    }
  }

  window.TileRoomRuntime = TileRoomRuntime;
})();

(function () {
  'use strict';

  class DashboardRoomRuntime {
    static async load({canvas, bundleUrl, labels}) {
      const response = await fetch(bundleUrl);
      if (!response.ok) throw new Error(`房间资源读取失败 (${response.status})`);
      const runtime = new DashboardRoomRuntime(canvas, await response.json(), labels);
      await runtime.preload();
      return runtime;
    }

    constructor(canvas, bundle, labels) {
      this.canvas = canvas;
      this.ctx = canvas.getContext('2d');
      this.scene = bundle;
      this.labels = labels;
      this.stage = {scale:1, ox:0, oy:0};
      this.images = {};
      this.walkable = new Set(bundle.walkable.map(point => this.key(point)));
      this.hiddenObjectIds = new Set();
      this.soloObjectId = null;
      this.layerRoleFilter = null;
      this.running = false;
      this.frameRequest = null;
      this.actor = {
        position:[...(bundle.anchors.rug || bundle.anchors.entry)],
        posePosition:null, target:null, targetFacing:null, path:[],
        action:'idle', activity:'idle', pose:'idle', expression:'neutral',
        scene:null, interaction:null, facing:'front', walked:0, lastTime:0,
        tourRoute:null, tourForward:true
      };
    }

    async preload() {
      await Promise.all(Object.entries(this.scene.images).map(([key, path]) => new Promise((resolve, reject) => {
        const image = new Image();
        image.onload = () => { this.images[key] = image; resolve(); };
        image.onerror = () => reject(new Error(`房间图片加载失败: ${key} (${path})`));
        image.src = path;
      })));
    }

    start() {
      if (this.running) return;
      this.running = true;
      this.frameRequest = requestAnimationFrame(now => this.loop(now));
    }

    stop() {
      this.running = false;
      if (this.frameRequest !== null) cancelAnimationFrame(this.frameRequest);
      this.frameRequest = null;
    }

    key(point) { return `${point[0]},${point[1]}`; }

    project(point) {
      const [x, y, z=0] = point;
      const tile = this.scene.tile;
      return [
        this.stage.ox + (tile.origin[0] + (x-y) * tile.width) * this.stage.scale,
        this.stage.oy + (tile.origin[1] + (x+y) * tile.height - z * tile.height * 2) * this.stage.scale
      ];
    }

    depthKey(point, bias=0) {
      return ((point[0] + point[1]) * 10000) + (point[2] || 0) * 100 + bias;
    }

    directionFor(dx, dy) {
      if (dx > .01) return 'downRight';
      if (dx < -.01) return 'upLeft';
      if (dy > .01) return 'downLeft';
      if (dy < -.01) return 'upRight';
      return this.actor.facing;
    }

    pathfind(start, target) {
      const blocked = new Set(this.scene.objects
        .flatMap(object => object.occupancy?.tiles || [])
        .map(point => this.key(point)));
      const allowed = new Set([...this.walkable].filter(tile => !blocked.has(tile)));
      const startKey = this.key(start), targetKey = this.key(target);
      if (!allowed.has(targetKey)) return [];
      const open = [[start[0], start[1]]], came = new Map([[startKey, null]]), score = new Map([[startKey, 0]]);
      const distance = (a, b) => Math.abs(a[0]-b[0]) + Math.abs(a[1]-b[1]);
      while (open.length) {
        open.sort((a, b) => (score.get(this.key(a)) + distance(a, target)) - (score.get(this.key(b)) + distance(b, target)));
        const current = open.shift();
        if (this.key(current) === targetKey) break;
        for (const [dx, dy] of [[1,0],[-1,0],[0,1],[0,-1]]) {
          const next = [current[0]+dx, current[1]+dy], nextKey = this.key(next);
          if (!allowed.has(nextKey)) continue;
          const tentative = (score.get(this.key(current)) || 0) + 1;
          if (tentative < (score.get(nextKey) ?? Infinity)) {
            came.set(nextKey, current); score.set(nextKey, tentative);
            if (!open.some(point => this.key(point) === nextKey)) open.push(next);
          }
        }
      }
      if (!came.has(targetKey)) return [];
      const path = []; let current = target;
      while (this.key(current) !== startKey) {
        path.unshift([current[0], current[1], 0]);
        current = came.get(this.key(current));
      }
      return path;
    }

    visibleObjects() {
      return this.scene.objects.filter(object => (
        !this.hiddenObjectIds.has(object.id)
        && (this.soloObjectId === null || object.id === this.soloObjectId)
      ));
    }

    visibleLayers(object) {
      const layers = object.layers || [];
      return this.layerRoleFilter === null
        ? layers
        : layers.filter(layer => layer.role === this.layerRoleFilter);
    }

    frontLayer(object) {
      return (object.layers || []).find(layer => layer.role === 'front');
    }

    actionDefinition(action) {
      return this.scene.behavior.actionDefinitions[action] || {};
    }

    interactionFor(scene) {
      const interactions = this.scene.interactions;
      const configured = interactions[this.actionDefinition(scene.action).interaction];
      if (configured && configured.location === scene.location) return configured;
      const fallback = interactions[this.scene.behavior.locationFallbackInteractions[scene.location]];
      return Object.values(interactions).find(item => item.location === scene.location && item.action === scene.action)
        || fallback
        || null;
    }

    setActor(scene) {
      const actor = this.actor;
      const interaction = this.interactionFor(scene);
      const target = interaction?.approach || this.scene.anchors[scene.location] || this.scene.anchors.rug;
      actor.scene = scene;
      actor.interaction = interaction;
      actor.expression = scene.expression;
      actor.activity = scene.action || 'idle';
      actor.pose = interaction?.pose || 'idle';
      actor.posePosition = interaction?.posePosition ? [...interaction.posePosition] : null;
      actor.targetFacing = interaction?.facing || this.scene.behavior.locationFacing[scene.location] || actor.facing;
      actor.tourRoute = null;
      if (this.key(actor.path.at(-1) || actor.position) !== this.key(target)) {
        actor.path = this.pathfind(actor.position, target);
        actor.target = target;
        actor.action = actor.path.length ? 'walk' : actor.activity;
      } else actor.action = actor.activity;
    }

    activatePreview(params) {
      const demo = params.get('demo');
      if (demo === 'room-editor') {
        if (!this.editor) this.editor = new DashboardRoomEditor(this);
        this.editor.mount();
        return {status:'房间校准工具 · 只改预览', gameAction:'Room Editor · 不写入 daemon'};
      }
      this.hiddenObjectIds.clear();
      this.soloObjectId = null;
      this.layerRoleFilter = null;
      if (demo === 'atomization') {
        const objectId = params.get('object') || this.scene.objects[0].id;
        const requestedMode = params.get('mode');
        const mode = ['solo', 'layers'].includes(requestedMode) ? requestedMode : 'hidden';
        const object = this.scene.objects.find(item => item.id === objectId) || this.scene.objects[0];
        if (mode === 'solo') this.soloObjectId = object.id;
        else if (mode === 'layers') {
          this.soloObjectId = object.id;
          const requestedRole = params.get('role');
          this.layerRoleFilter = ['shadow', 'back', 'body', 'front', 'light'].includes(requestedRole)
            ? requestedRole : 'front';
        } else this.hiddenObjectIds.add(object.id);
        const inventory = this.scene.inventory.items.find(item => item.id === object.id);
        const actor = this.actor;
        actor.position = [...this.scene.anchors.rug]; actor.path = []; actor.tourRoute = null;
        actor.posePosition = null; actor.targetFacing = null; actor.action = 'idle'; actor.activity = 'idle'; actor.pose = 'idle'; actor.interaction = null;
        actor.scene = {location:'rug', action:'idle', expression:'neutral', time_of_day:'day'};
        return {
          status:`原子化审计 · ${object.id} · ${mode}${this.layerRoleFilter ? `:${this.layerRoleFilter}` : ''} · ${inventory?.status || 'untracked'} · 不写入 daemon`,
          gameAction:`原子化审计 · ${object.id} · ${mode}`
        };
      }
      if (!['walk', 'audit', 'tour', 'activity'].includes(demo)) return null;
      const actor = this.actor;
      const previewScene = (location='rug', action='idle') => ({location, action, expression:'neutral', time_of_day:'day'});
      if (demo === 'audit') {
        const objectId = params.get('object');
        if (objectId) {
          const object = this.scene.objects.find(item => item.id === objectId) || this.scene.objects[0];
          const side = params.get('side') === 'front' ? 'front' : 'behind';
          if (!object.audits?.[side] || !object.audit?.[side]) {
            return {
              status:`遮挡巡检 · ${object.id} · ${side} · 不适用 · 不写入 daemon`,
              gameAction:`遮挡巡检 · ${object.id} · ${side} · 不适用`
            };
          }
          actor.position = [...object.audit[side]];
          actor.path = []; actor.tourRoute = null; actor.posePosition = null; actor.targetFacing = null;
          actor.action = 'idle'; actor.activity = 'idle'; actor.pose = object.auditPose?.[side] || 'idle';
          actor.scene = previewScene(); actor.interaction = null;
          return {status:`遮挡巡检 · ${object.id} · ${side} · 不写入 daemon`, gameAction:`遮挡巡检 · ${object.id} · ${side}`};
        }
        const axis = params.get('axis') || 'downRight';
        const route = this.scene.axisAudits[axis] || this.scene.axisAudits.downRight;
        actor.position = [...route[0]]; actor.path = route.slice(1).map(point => [...point]);
        actor.tourRoute = null; actor.posePosition = null; actor.targetFacing = null;
        actor.action = 'walk'; actor.activity = 'idle'; actor.pose = 'idle'; actor.scene = previewScene(); actor.interaction = null;
        return {status:`斜向巡检 · ${axis} · 不写入 daemon`, gameAction:`巡检 · ${axis}`};
      }
      if (demo === 'tour') {
        const route = this.scene.routes.tour;
        actor.position = [...route[0]]; actor.path = route.slice(1).map(point => [...point]);
        actor.tourRoute = route; actor.tourForward = true; actor.posePosition = null; actor.targetFacing = null;
        actor.action = 'walk'; actor.activity = 'idle'; actor.pose = 'idle'; actor.scene = previewScene('rug', 'walk_out'); actor.interaction = null;
        return {status:'小屋巡回行走 · 不写入 daemon', gameAction:'小屋巡回 · 不写入 daemon'};
      }
      if (demo === 'activity') {
        const spot = params.get('spot') || Object.keys(this.scene.interactions)[0];
        const preview = this.scene.interactions[spot] || Object.values(this.scene.interactions)[0];
        actor.position = [...preview.approach]; actor.posePosition = preview.posePosition ? [...preview.posePosition] : null;
        actor.path = []; actor.tourRoute = null; actor.targetFacing = null; actor.action = preview.action;
        actor.activity = preview.action; actor.pose = preview.pose; actor.facing = preview.facing;
        actor.scene = previewScene(preview.location, preview.action); actor.interaction = preview;
        return {status:`动作巡检 · ${spot} · 不写入 daemon`, gameAction:`动作巡检 · ${spot}`};
      }
      const route = this.scene.routes.walk;
      actor.position = [...route[0]]; actor.path = route.slice(1).map(point => [...point]);
      actor.tourRoute = null; actor.posePosition = null; actor.targetFacing = null;
      actor.action = 'walk'; actor.activity = 'idle'; actor.pose = 'idle'; actor.scene = previewScene('rug', 'walk_out'); actor.interaction = null;
      return {status:'行走动画预览 · 不写入 daemon', gameAction:'绕沙发行走 · 不写入 daemon'};
    }

    drawImageContain(image, x, y, width, height) {
      const scale = Math.min(width / image.width, height / image.height);
      const imageWidth = image.width * scale, imageHeight = image.height * scale;
      this.stage.scale = scale;
      this.stage.ox = x + (width - imageWidth) / 2;
      this.stage.oy = y + (height - imageHeight) / 2;
      this.ctx.drawImage(image, this.stage.ox, this.stage.oy, imageWidth, imageHeight);
    }

    drawActor(now) {
      const actor = this.actor;
      const action = actor.action === 'walk' ? 'walk' : actor.pose;
      const walk = this.scene.sprites.walk;
      const pose = this.scene.sprites.poses[action] || this.scene.sprites.poses.idle;
      const sheet = this.images[action === 'walk' ? walk.image : pose.image];
      if (!sheet) return;
      const renderPosition = action === 'walk' ? actor.position : (actor.posePosition || actor.position);
      const [px, py] = this.project(renderPosition);
      const cell = {column:walk.columns[actor.facing] ?? 0, row:action === 'walk' ? Math.floor(actor.walked * walk.frameRate) % walk.frames : 0};
      const walkSheet = action === 'walk';
      const [cropX, cropY, cropWidth, cropHeight] = pose.crop;
      const cw = walkSheet ? sheet.width / Object.keys(walk.columns).length : cropWidth;
      const ch = walkSheet ? sheet.height / walk.frames : cropHeight;
      const sx = walkSheet ? cell.column * cw : cropX, sy = walkSheet ? cell.row * ch : cropY;
      const [dw, dh] = walkSheet ? walk.display : pose.display;
      const x = px - dw / 2, y = py - dh + 6;
      this.ctx.save(); this.ctx.imageSmoothingEnabled = false;
      this.ctx.drawImage(sheet, sx, sy, cw, ch, x, y, dw, dh); this.ctx.restore();
      this.drawPhone(x, y, now); this.drawStatusMark(px, y, now);
      if (action === 'sleep') this.drawSleepMark(renderPosition, now);
    }

    drawPhone(x, y, now) {
      const actor = this.actor;
      const definition = this.actionDefinition(actor.action);
      if (actor.pose === 'sit' || !definition.phoneProp) return;
      const pulse = definition.phoneAnimation === 'typing' ? Math.sin(now / 90) * 2 : 0;
      this.ctx.save(); this.ctx.fillStyle = '#25343d'; this.ctx.fillRect(x + 57, y + 50 + pulse, 9, 15);
      this.ctx.fillStyle = '#9cd9d4'; this.ctx.fillRect(x + 59, y + 53 + pulse, 5, 7); this.ctx.restore();
    }

    drawSleepMark(position, now) {
      const [x, y] = this.project(position);
      this.ctx.save(); this.ctx.fillStyle = '#e9d7bf'; this.ctx.font = '18px Pixel, sans-serif';
      this.ctx.fillText('z', x + 15, y - 32 - Math.sin(now / 350) * 4); this.ctx.restore();
    }

    drawStatusMark(x, y, now) {
      const actor = this.actor, scene = actor.scene || {};
      if (!scene.has_notification && !scene.has_open_task && !['pout','guarded','hurt','worry','soft'].includes(actor.expression)) return;
      const pulse = Math.sin(now / 280) * 1.5;
      this.ctx.save(); this.ctx.imageSmoothingEnabled = false;
      this.ctx.fillStyle = scene.has_notification ? '#ffe79b' : '#ffd3bd'; this.ctx.strokeStyle = '#5b3a36'; this.ctx.lineWidth = 2;
      this.ctx.beginPath(); this.ctx.roundRect(x - 18, y - 18 + pulse, 36, 18, 3); this.ctx.fill(); this.ctx.stroke();
      this.ctx.fillStyle = actor.expression === 'hurt' ? '#bd6b72' : actor.expression === 'soft' ? '#d78288' : '#557f78';
      const mark = actor.expression === 'hurt' ? '…' : actor.expression === 'pout' || actor.expression === 'guarded' ? '!' : scene.has_notification ? '✉' : '·';
      this.ctx.font = '12px Pixel, sans-serif'; this.ctx.textAlign = 'center'; this.ctx.fillText(mark, x, y - 5 + pulse); this.ctx.restore();
    }

    actorDepth() {
      const actor = this.actor, interactionDepth = actor.interaction?.depth;
      if (typeof interactionDepth === 'object' && interactionDepth.layer === 'above-front') {
        const object = this.scene.objects.find(item => item.id === interactionDepth.relativeTo);
        const front = object && this.frontLayer(object);
        if (front) return this.depthKey(object.tile, front.depthBias + 100);
      }
      return this.depthKey(actor.action === 'walk' ? actor.position : (actor.posePosition || actor.position));
    }

    drawEntities(now) {
      const parts = [
        {depth:this.actorDepth(), order:0, draw:() => this.drawActor(now)},
        ...this.visibleObjects().flatMap((object, objectOrder) => this.visibleLayers(object)
          .filter(layer => layer.role === 'front')
          .map((layer, layerOrder) => ({
            depth:this.depthKey(object.tile, layer.depthBias), order:(objectOrder + 1) * 100 + layerOrder,
            draw:() => this.drawLayer(layer)
          })))
      ];
      for (const part of parts.sort((a, b) => a.depth - b.depth || a.order - b.order)) part.draw();
    }

    drawLayer(layer) {
      const image = this.images[layer.image];
      if (!image) return;
      const [x, y] = layer.origin;
      this.ctx.save(); this.ctx.imageSmoothingEnabled = false;
      this.ctx.drawImage(image, this.stage.ox + x * this.stage.scale, this.stage.oy + y * this.stage.scale, image.width * this.stage.scale, image.height * this.stage.scale);
      this.ctx.restore();
    }

    drawObjectLayers(roles) {
      const allowed = new Set(roles);
      const parts = this.visibleObjects().flatMap((object, objectOrder) => this.visibleLayers(object)
        .filter(layer => allowed.has(layer.role))
        .map((layer, layerOrder) => ({
          depth:this.depthKey(object.tile, layer.depthBias),
          order:objectOrder * 100 + layerOrder,
          layer
        })));
      for (const part of parts.sort((a, b) => a.depth - b.depth || a.order - b.order)) this.drawLayer(part.layer);
    }

    drawEffects(now) {
      const scene = this.actor.scene || {}, target = this.actor.posePosition || this.scene.anchors[scene.location] || this.scene.anchors.rug;
      const effectObjectId = this.actor.interaction?.object;
      if (effectObjectId && (
        this.hiddenObjectIds.has(effectObjectId)
        || (this.soloObjectId !== null && this.soloObjectId !== effectObjectId)
      )) return;
      const [x, y] = this.project(target), pulse = Math.sin(now / 240);
      const effects = {
        focus:() => {
          const size = 6 + Math.sin(now / 500) * 2;
          this.ctx.save(); this.ctx.globalAlpha = .20; this.ctx.fillStyle = '#9cd9d4'; this.ctx.beginPath();
          this.ctx.ellipse(x, y - 18, 38 + size, 16 + size / 3, 0, 0, Math.PI * 2); this.ctx.fill(); this.ctx.restore();
        },
        'tidy-sparkles':() => {
          this.ctx.save(); this.ctx.fillStyle = '#ffe49a';
          for (const [dx,dy] of [[-15,-42],[5,-54],[19,-36]]) this.ctx.fillRect(x + dx, y + dy + pulse * 2, 4, 4);
          this.ctx.restore();
        },
        'exit-glow':() => {
          this.ctx.save(); this.ctx.globalAlpha = .28 + pulse * .06; this.ctx.fillStyle = '#f5c77b'; this.ctx.fillRect(x - 20, y - 42, 40, 42); this.ctx.restore();
        },
        steam:() => {
          this.ctx.save(); this.ctx.strokeStyle = '#e9efff'; this.ctx.lineWidth = 2; this.ctx.beginPath(); this.ctx.arc(x + 9, y - 33, 7, Math.PI * 1.1, Math.PI * 1.9); this.ctx.stroke(); this.ctx.restore();
        },
        'social-spark':() => {
          this.ctx.save(); this.ctx.fillStyle = '#f7e5ba'; this.ctx.fillRect(x + 16, y - 44 + pulse * 2, 4, 4); this.ctx.restore();
        }
      };
      effects[this.actionDefinition(scene.action).effect]?.();
    }

    drawRibbon() {
      const scene = this.actor.scene || {};
      this.ctx.save(); this.ctx.imageSmoothingEnabled = false; this.ctx.fillStyle = 'rgba(58, 43, 36, .82)'; this.ctx.fillRect(28, 24, 226, 38);
      this.ctx.strokeStyle = '#e7c076'; this.ctx.lineWidth = 2; this.ctx.strokeRect(28, 24, 226, 38); this.ctx.font = '12px Pixel, sans-serif';
      this.ctx.fillStyle = '#fff2d6'; this.ctx.fillText(this.labels[scene.location] || '小屋', 42, 41);
      this.ctx.fillStyle = '#f3ce86'; this.ctx.fillText(this.labels[scene.action] || '同步中', 42, 56); this.ctx.restore();
    }

    draw(now) {
      const ctx = this.ctx;
      ctx.imageSmoothingEnabled = false; ctx.clearRect(0, 0, this.canvas.width, this.canvas.height);
      ctx.fillStyle = '#141115'; ctx.fillRect(0, 0, this.canvas.width, this.canvas.height);
      const background = this.images[this.scene.background];
      if (background) this.drawImageContain(background, 18, 14, 964, 720);
      if (this.editor?.mode === 'master') { this.editor.draw(); return; }
      this.drawObjectLayers(['shadow', 'back', 'body']);
      if ((this.actor.scene || {}).time_of_day === 'night') {
        ctx.fillStyle = 'rgba(20, 17, 30, .22)'; ctx.fillRect(0, 0, this.canvas.width, this.canvas.height);
      }
      this.drawEntities(now);
      this.drawObjectLayers(['light']);
      this.drawEffects(now); this.drawRibbon();
      if (this.editor?.mode === 'alpha') this.editor.drawAlpha();
      if (this.editor) this.editor.draw();
    }

    loop(now) {
      const actor = this.actor, last = actor.lastTime || now, dt = Math.min(.05, (now - last) / 1000);
      const walkSpeed = this.scene.movement.walkSpeed;
      actor.lastTime = now;
      if (actor.path.length) {
        const target = actor.path[0], dx = target[0] - actor.position[0], dy = target[1] - actor.position[1];
        const distance = Math.hypot(dx, dy);
        if (distance <= walkSpeed * dt) {
          actor.position = target; actor.path.shift();
          if (!actor.path.length && actor.tourRoute) {
            actor.tourForward = !actor.tourForward;
            const route = actor.tourForward ? actor.tourRoute.slice(1) : actor.tourRoute.slice(0, -1).reverse();
            actor.path = route.map(point => [...point]);
          }
          if (!actor.path.length && actor.targetFacing) actor.facing = actor.targetFacing;
          if (!actor.path.length) actor.action = actor.activity || 'idle';
        } else {
          actor.position = [actor.position[0] + dx / distance * walkSpeed * dt, actor.position[1] + dy / distance * walkSpeed * dt, 0];
          actor.facing = this.directionFor(dx, dy); actor.walked += walkSpeed * dt;
        }
      }
      this.draw(now);
      if (this.running) this.frameRequest = requestAnimationFrame(next => this.loop(next));
    }
  }

  window.DashboardRoomRuntime = DashboardRoomRuntime;
})();

(function () {
  'use strict';

  class DashboardRoomEditor {
    constructor(runtime) {
      this.runtime = runtime;
      this.selectedId = runtime.scene.objects[0].id;
      this.selectedLayerIndex = Math.max(0, runtime.scene.objects[0].layers.findIndex(layer => layer.role === 'front'));
      this.inventorySelectionId = this.selectedId;
      this.mode = 'composite';
      this.visibility = {grid:true, walkable:true, footprints:true, approaches:true, depth:true};
      this.drag = null;
      this.mounted = false;
    }

    get object() {
      return this.runtime.scene.objects.find(item => item.id === this.selectedId);
    }

    get layer() {
      return this.object.layers[this.selectedLayerIndex] || this.object.layers[0];
    }

    mount() {
      if (this.mounted) return;
      this.mounted = true;
      const style = document.createElement('style');
      style.textContent = `
        .room-editor { position:fixed; z-index:20; right:18px; bottom:18px; width:300px; padding:12px; color:#fff2d6; background:rgba(45,34,31,.96); border:2px solid #e7c076; box-shadow:0 6px 20px #0008; font:12px Pixel,sans-serif; }
        .room-editor label { display:grid; grid-template-columns:88px 1fr; align-items:center; gap:7px; margin:6px 0; }
        .room-editor select,.room-editor input,.room-editor button,.room-editor textarea { box-sizing:border-box; width:100%; color:#3d2f2b; background:#fff5dd; border:1px solid #b88b61; font:12px Pixel,sans-serif; }
        .room-editor button { padding:7px; cursor:pointer; }
        .room-editor .row { display:grid; grid-template-columns:1fr 1fr; gap:6px; margin:6px 0; }
        .room-editor textarea { height:82px; padding:5px; resize:vertical; }
        .room-editor small { display:block; color:#e8cda4; line-height:1.5; }
      `;
      document.head.appendChild(style);
      this.panel = document.createElement('section');
      this.panel.className = 'room-editor';
      this.panel.setAttribute('aria-label', '房间校准工具');
      this.panel.innerHTML = `
        <strong>Room Editor · 只改预览</strong>
        <small data-field="inventory-summary"></small>
        <label>资产盘点<select data-field="inventory"></select></label>
        <small data-field="inventory-status"></small>
        <label>对象<select data-field="object"></select></label>
        <label>对象图层<select data-field="layer"></select></label>
        <label>显示模式<select data-field="mode"><option value="composite">最终合成</option><option value="master">仅母版</option><option value="alpha">选中 alpha</option><option value="hidden">隐藏对象</option><option value="solo">Solo 对象</option><option value="shadow">仅 shadow</option><option value="back">仅 back</option><option value="body">仅 body</option><option value="front">仅 front</option><option value="light">仅 light</option></select></label>
        <div class="row"><label><input type="checkbox" data-toggle="grid" checked>网格</label><label><input type="checkbox" data-toggle="walkable" checked>可走区</label></div>
        <div class="row"><label><input type="checkbox" data-toggle="footprints" checked>Footprint</label><label><input type="checkbox" data-toggle="approaches" checked>交互点</label></div>
        <label><input type="checkbox" data-toggle="depth" checked>Depth key 与 origin 框</label>
        <div class="row"><label>X<input type="number" step="1" data-field="x"></label><label>Y<input type="number" step="1" data-field="y"></label></div>
        <div class="row"><button data-action="behind">角色在后</button><button data-action="front">角色在前</button></div>
        <div class="row"><button data-action="hidden">隐藏对象</button><button data-action="solo">Solo 对象层</button></div>
        <small data-field="audit-status">尚未执行对象审计。</small>
        <label>动作<select data-field="interaction"><option value="">选择动作</option></select></label>
        <textarea readonly data-field="snippet" aria-label="房间清单片段"></textarea>
        <button data-action="copy">复制清单片段</button>
        <small>可直接拖动黄色框校准 origin；网格、可走区、footprint、交互点和 depth key 会同步显示。</small>
      `;
      document.body.appendChild(this.panel);
      this.bind();
      this.refresh();
    }

    bind() {
      const objectSelect = this.panel.querySelector('[data-field="object"]');
      objectSelect.innerHTML = this.runtime.scene.objects.map(item => `<option value="${item.id}">${item.id}</option>`).join('');
      objectSelect.addEventListener('change', event => {
        this.selectedId = event.target.value; this.inventorySelectionId = this.selectedId;
        this.selectedLayerIndex = Math.max(0, this.object.layers.findIndex(layer => layer.role === 'front'));
        this.refresh(); this.redraw();
      });
      const inventorySelect = this.panel.querySelector('[data-field="inventory"]');
      inventorySelect.innerHTML = this.runtime.scene.inventory.items.map(item => `<option value="${item.id}">${item.status} · ${item.id}</option>`).join('');
      inventorySelect.addEventListener('change', event => {
        this.inventorySelectionId = event.target.value;
        if (this.runtime.scene.objects.some(item => item.id === event.target.value)) {
          this.selectedId = event.target.value;
          this.selectedLayerIndex = Math.max(0, this.object.layers.findIndex(layer => layer.role === 'front'));
        }
        this.refresh(); this.redraw();
      });
      this.panel.querySelector('[data-field="layer"]').addEventListener('change', event => {
        this.selectedLayerIndex = Number(event.target.value); this.refresh(); this.redraw();
      });
      this.panel.querySelector('[data-field="mode"]').addEventListener('change', event => {
        this.mode = event.target.value;
        this.runtime.hiddenObjectIds.clear(); this.runtime.soloObjectId = null; this.runtime.layerRoleFilter = null;
        if (this.mode === 'hidden') this.runtime.hiddenObjectIds.add(this.object.id);
        else if (this.mode === 'solo') this.runtime.soloObjectId = this.object.id;
        else if (['shadow', 'back', 'body', 'front', 'light'].includes(this.mode)) {
          this.runtime.soloObjectId = this.object.id; this.runtime.layerRoleFilter = this.mode;
        }
        this.redraw();
      });
      for (const name of Object.keys(this.visibility)) this.panel.querySelector(`[data-toggle="${name}"]`).addEventListener('change', event => {
        this.visibility[name] = event.target.checked;
        this.redraw();
      });
      for (const axis of ['x', 'y']) this.panel.querySelector(`[data-field="${axis}"]`).addEventListener('input', event => {
        const index = axis === 'x' ? 0 : 1;
        this.layer.origin[index] = Math.round(Number(event.target.value) || 0);
        this.refreshSnippet();
        this.redraw();
      });
      for (const side of ['behind', 'front']) this.panel.querySelector(`[data-action="${side}"]`).addEventListener('click', () => {
        const result = this.runtime.activatePreview(new URLSearchParams(`demo=audit&object=${this.selectedId}&side=${side}`));
        this.panel.querySelector('[data-field="audit-status"]').textContent = result.status;
        this.redraw();
      });
      for (const mode of ['hidden', 'solo']) this.panel.querySelector(`[data-action="${mode}"]`).addEventListener('click', () => {
        const result = this.runtime.activatePreview(new URLSearchParams(`demo=atomization&object=${this.selectedId}&mode=${mode}`));
        this.panel.querySelector('[data-field="audit-status"]').textContent = result.status;
        this.redraw();
      });
      const interactionSelect = this.panel.querySelector('[data-field="interaction"]');
      interactionSelect.addEventListener('change', event => {
        if (event.target.value) { this.runtime.activatePreview(new URLSearchParams(`demo=activity&spot=${event.target.value}`)); this.redraw(); }
      });
      this.panel.querySelector('[data-action="copy"]').addEventListener('click', async () => {
        const value = this.panel.querySelector('[data-field="snippet"]').value;
        if (navigator.clipboard) await navigator.clipboard.writeText(value);
      });
      this.runtime.canvas.addEventListener('pointerdown', event => this.pointerDown(event));
      this.runtime.canvas.addEventListener('pointermove', event => this.pointerMove(event));
      this.runtime.canvas.addEventListener('pointerup', () => { this.drag = null; });
      this.runtime.canvas.addEventListener('pointerleave', () => { this.drag = null; });
    }

    canvasPoint(event) {
      const rect = this.runtime.canvas.getBoundingClientRect();
      return [(event.clientX - rect.left) * this.runtime.canvas.width / rect.width, (event.clientY - rect.top) * this.runtime.canvas.height / rect.height];
    }

    bounds() {
      const image = this.runtime.images[this.layer.image], stage = this.runtime.stage;
      if (!image) return null;
      const [x, y] = this.layer.origin;
      return {x:stage.ox + x * stage.scale, y:stage.oy + y * stage.scale, width:image.width * stage.scale, height:image.height * stage.scale};
    }

    pointerDown(event) {
      const bounds = this.bounds(); if (!bounds) return;
      const [x, y] = this.canvasPoint(event);
      if (x < bounds.x || x > bounds.x + bounds.width || y < bounds.y || y > bounds.y + bounds.height) return;
      this.drag = {x, y, origin:[...this.layer.origin]};
      this.runtime.canvas.setPointerCapture?.(event.pointerId);
    }

    pointerMove(event) {
      if (!this.drag) return;
      const [x, y] = this.canvasPoint(event), scale = this.runtime.stage.scale || 1;
      this.layer.origin = [
        Math.round(this.drag.origin[0] + (x - this.drag.x) / scale),
        Math.round(this.drag.origin[1] + (y - this.drag.y) / scale)
      ];
      this.refresh();
      this.redraw();
    }

    redraw() {
      if (!this.runtime.running) this.runtime.draw(0);
    }

    refresh() {
      const object = this.object;
      this.panel.querySelector('[data-field="object"]').value = object.id;
      this.panel.querySelector('[data-field="inventory"]').value = this.inventorySelectionId;
      const summary = this.runtime.scene.inventory.summary;
      this.panel.querySelector('[data-field="inventory-summary"]').textContent = `Inventory ${summary.total} · planned ${summary.planned} · partial ${summary.partial} · atomized ${summary.atomized} · verified ${summary.verified} · excluded ${summary.excluded || 0}`;
      const inventoryItem = this.runtime.scene.inventory.items.find(item => item.id === this.inventorySelectionId);
      const provenance = this.inventorySelectionId === object.id && object.provenance
        ? ` / ${object.provenance.method} → ${object.provenance.reference}` : '';
      this.panel.querySelector('[data-field="inventory-status"]').textContent = inventoryItem ? `${inventoryItem.zone} / ${inventoryItem.category} / ${inventoryItem.status}${inventoryItem.interactive ? ' / interactive' : ' / no interaction'}${provenance}` : '未登记';
      const layerSelect = this.panel.querySelector('[data-field="layer"]');
      layerSelect.innerHTML = object.layers.map((layer, index) => `<option value="${index}">${layer.role} · ${layer.image}</option>`).join('');
      layerSelect.value = String(this.selectedLayerIndex);
      this.panel.querySelector('[data-field="x"]').value = this.layer.origin[0];
      this.panel.querySelector('[data-field="y"]').value = this.layer.origin[1];
      for (const side of ['behind', 'front']) {
        this.panel.querySelector(`[data-action="${side}"]`).disabled = !object.audits?.[side] || !object.audit?.[side];
      }
      const interactions = Object.entries(this.runtime.scene.interactions).filter(([, item]) => item.object === object.id);
      this.panel.querySelector('[data-field="interaction"]').innerHTML = '<option value="">选择动作</option>' + interactions.map(([name, item]) => `<option value="${name}">${name} · ${item.action}</option>`).join('');
      this.refreshSnippet();
    }

    manifestSnippet() {
      return JSON.stringify({id:this.object.id, layers:[{role:this.layer.role, origin:this.layer.origin}]}, null, 2);
    }

    refreshSnippet() {
      this.panel.querySelector('[data-field="snippet"]').value = this.manifestSnippet();
    }

    drawTile(point, fill, stroke) {
      const ctx = this.runtime.ctx;
      const corners = [point, [point[0]+1,point[1],0], [point[0]+1,point[1]+1,0], [point[0],point[1]+1,0]].map(item => this.runtime.project(item));
      ctx.save(); ctx.beginPath(); ctx.moveTo(...corners[0]); for (const corner of corners.slice(1)) ctx.lineTo(...corner); ctx.closePath();
      ctx.fillStyle = fill; ctx.strokeStyle = stroke; ctx.lineWidth = 1; ctx.fill(); ctx.stroke(); ctx.restore();
    }

    draw() {
      const ctx = this.runtime.ctx;
      const grid = this.runtime.scene.grid;
      if (this.visibility.grid) for (let x = grid.minX; x < grid.maxX; x += 1) for (let y = grid.minY; y < grid.maxY; y += 1) {
        this.drawTile([x,y,0], 'rgba(255,255,255,.015)', 'rgba(230,220,200,.14)');
      }
      if (this.visibility.walkable) for (const point of this.runtime.scene.walkable) this.drawTile(point, 'rgba(80,190,180,.08)', 'rgba(100,225,210,.42)');
      if (this.visibility.footprints) for (const object of this.runtime.scene.objects) for (const point of object.occupancy?.tiles || []) this.drawTile(point, 'rgba(210,80,90,.18)', 'rgba(255,120,120,.7)');
      if (this.visibility.approaches) for (const [name, interaction] of Object.entries(this.runtime.scene.interactions)) {
          const [x, y] = this.runtime.project(interaction.approach);
          ctx.save(); ctx.fillStyle = '#ffe27b'; ctx.fillRect(x-3, y-3, 6, 6); ctx.fillStyle = '#fff2d6'; ctx.font = '10px Pixel,sans-serif'; ctx.fillText(name, x+5, y-5); ctx.restore();
      }
      const object = this.object, [dx, dy] = this.runtime.project(object.tile), bounds = this.bounds();
      if (this.visibility.depth) {
        ctx.save(); ctx.fillStyle = '#ffe27b'; ctx.font = '10px Pixel,sans-serif'; ctx.fillText(`${object.id}/${this.layer.role} depth=${this.runtime.depthKey(object.tile, this.layer.depthBias)}`, dx+5, dy-5);
        if (bounds) { ctx.strokeStyle = '#ffe27b'; ctx.lineWidth = 2; ctx.strokeRect(bounds.x, bounds.y, bounds.width, bounds.height); }
        ctx.restore();
      }
    }

    drawAlpha() {
      const image = this.runtime.images[this.layer.image], bounds = this.bounds();
      if (!image || !bounds) return;
      const ctx = this.runtime.ctx; ctx.save(); ctx.fillStyle = 'rgba(12,10,15,.86)'; ctx.fillRect(0,0,this.runtime.canvas.width,this.runtime.canvas.height);
      ctx.drawImage(image, bounds.x, bounds.y, bounds.width, bounds.height); ctx.restore();
    }
  }

  window.DashboardRoomEditor = DashboardRoomEditor;
})();

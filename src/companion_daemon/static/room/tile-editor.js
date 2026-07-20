(function () {
  'use strict';

  class TileRoomEditor {
    constructor(runtime) {
      this.runtime = runtime;
      this.selectedId = runtime.scene.objects[0]?.id || null;
      this.dragging = false;
      this.panel = null;
      this.boundPointerMove = event => this.pointerMove(event);
      this.boundPointerUp = () => { this.dragging = false; };
    }

    selected() { return this.runtime.objectById.get(this.selectedId); }

    mount() {
      if (this.panel) { this.panel.hidden = false; return; }
      const panel = document.createElement('aside');
      panel.className = 'tile-room-editor';
      panel.setAttribute('aria-label', '格驱动房间摆放器');
      panel.innerHTML = '<strong>格驱动摆放器</strong><p>拖动物件，按格吸附；仅编辑内存预览。</p><label>家具 <select data-field="object"></select></label><div class="tile-editor-fields"></div><p class="tile-editor-status"></p><button data-action="export">导出 JSON</button><button data-action="close">关闭</button>';
      document.body.append(panel); this.panel = panel;
      panel.addEventListener('input', event => this.input(event));
      panel.addEventListener('click', event => this.click(event));
      this.runtime.canvas.addEventListener('pointerdown', event => this.pointerDown(event));
      window.addEventListener('pointermove', this.boundPointerMove);
      window.addEventListener('pointerup', this.boundPointerUp);
      this.renderPanel();
    }

    unmount() {
      if (!this.panel) return;
      this.panel.hidden = true; this.dragging = false;
    }

    renderPanel() {
      if (!this.panel) return;
      const object = this.selected(), select = this.panel.querySelector('[data-field="object"]');
      select.innerHTML = this.runtime.scene.objects.map(item => `<option value="${item.id}">${item.label}</option>`).join('');
      select.value = this.selectedId;
      const fields = this.panel.querySelector('.tile-editor-fields');
      fields.innerHTML = object ? ['x','y','z','width','depth','height'].map(key => `<label>${key}<input data-field="${key}" type="number" step="${key === 'z' || key === 'height' ? '.05' : '1'}" min="0" value="${object.transform[key]}"></label>`).join('') + `<label>材质 <select data-field="material">${Object.keys(this.runtime.scene.materials).map(name => `<option value="${name}" ${name === object.material ? 'selected' : ''}>${name}</option>`).join('')}</select></label>` : '';
      this.updateStatus();
    }

    updateStatus() {
      if (!this.panel) return;
      const object = this.selected(), status = this.panel.querySelector('.tile-editor-status');
      if (!object) { status.textContent = '未选择家具'; return; }
      const valid = this.runtime.canPlace(object, Math.round(object.transform.x), Math.round(object.transform.y));
      status.textContent = valid ? `可摆放 · ${object.occupancy === 'decor' ? '装饰不阻挡' : `${object.collider.length} 格已占用`}` : '位置越界或与碰撞体重叠';
      status.className = `tile-editor-status ${valid ? 'ok' : 'error'}`;
    }

    input(event) {
      const field = event.target.dataset.field, object = this.selected();
      if (!field) return;
      if (field === 'object') { this.selectedId = event.target.value; this.renderPanel(); return; }
      if (!object) return;
      if (field === 'material') object.material = event.target.value;
      if (field in object.transform) {
        object.transform[field] = Number(event.target.value);
        if (['x', 'y', 'width', 'depth'].includes(field)) this.syncCollider(object);
      }
      this.updateStatus();
    }

    click(event) {
      const action = event.target.dataset.action;
      if (action === 'close') this.unmount();
      if (action === 'export') {
        const blob = new Blob([JSON.stringify(this.runtime.scene, null, 2)], {type:'application/json'});
        const link = document.createElement('a'); link.href = URL.createObjectURL(blob); link.download = `${this.runtime.scene.id}.json`; link.click(); URL.revokeObjectURL(link.href);
      }
    }

    pick(point) {
      const candidates = this.runtime.scene.objects.filter(object => {
        const t = object.transform;
        return point[0] >= t.x && point[0] <= t.x + t.width && point[1] >= t.y && point[1] <= t.y + t.depth;
      });
      return candidates.sort((a,b) => this.runtime.depth([b.transform.x+b.transform.width,b.transform.y+b.transform.depth]) - this.runtime.depth([a.transform.x+a.transform.width,a.transform.y+a.transform.depth]))[0] || null;
    }

    pointerDown(event) {
      if (!this.panel || this.panel.hidden) return;
      const rect = this.runtime.canvas.getBoundingClientRect(), point = this.runtime.screenToGrid([event.clientX - rect.left, event.clientY - rect.top]);
      const picked = this.pick(point);
      if (!picked) return;
      this.selectedId = picked.id; this.dragging = true; this.renderPanel(); event.preventDefault();
    }

    pointerMove(event) {
      if (!this.dragging) return;
      const rect = this.runtime.canvas.getBoundingClientRect(), point = this.runtime.screenToGrid([event.clientX - rect.left, event.clientY - rect.top]);
      const object = this.selected(), x = Math.round(point[0] - object.transform.width / 2), y = Math.round(point[1] - object.transform.depth / 2);
      if (this.runtime.canPlace(object, x, y)) { object.transform.x = x; object.transform.y = y; this.syncCollider(object); this.renderPanel(); }
    }

    syncCollider(object) {
      if (object.occupancy === 'decor') return;
      object.collider = [];
      for (let dx=0; dx<Math.ceil(object.transform.width); dx += 1) for (let dy=0; dy<Math.ceil(object.transform.depth); dy += 1) object.collider.push([object.transform.x+dx, object.transform.y+dy]);
      this.runtime.blocked = new Set(this.runtime.scene.objects.flatMap(item => TileRoomRuntime.occupancyCells(item)).map(point => this.runtime.key(point)));
    }

    drawOverlay() {
      if (!this.panel || this.panel.hidden) return;
      const ctx = this.runtime.ctx, object = this.selected();
      ctx.save(); ctx.globalAlpha = .55; ctx.lineWidth = Math.max(1, this.runtime.stage.scale);
      for (const item of this.runtime.scene.objects) for (const cell of item.collider || []) {
        const points = [[cell[0],cell[1],.02],[cell[0]+1,cell[1],.02],[cell[0]+1,cell[1]+1,.02],[cell[0],cell[1]+1,.02]].map(point => this.runtime.project(point));
        this.runtime.polygon(points, item.id === object?.id ? 'rgba(255,220,125,.38)' : 'rgba(228,95,95,.2)', '#f4d582');
      }
      for (const interaction of Object.values(this.runtime.scene.interactions)) { const [x,y] = this.runtime.project([...interaction.approach,.03]); ctx.fillStyle='#bcefd8'; ctx.fillRect(x-3,y-3,6,6); }
      ctx.restore();
    }
  }

  const style = document.createElement('style');
  style.textContent = '.tile-room-editor{position:fixed;z-index:20;right:18px;bottom:18px;width:230px;padding:13px;background:#2a201f;color:#f7e7c4;border:2px solid #c99b5c;font:12px Pixel,monospace;box-shadow:0 8px 24px #0008}.tile-room-editor p{line-height:1.45}.tile-room-editor label{display:flex;justify-content:space-between;gap:8px;margin:6px 0}.tile-room-editor input,.tile-room-editor select,.tile-room-editor button{font:inherit;max-width:126px}.tile-room-editor button{margin:7px 5px 0 0}.tile-editor-status.ok{color:#bde5c5}.tile-editor-status.error{color:#ffaaa1}';
  document.head.append(style);
  window.TileRoomEditor = TileRoomEditor;
})();

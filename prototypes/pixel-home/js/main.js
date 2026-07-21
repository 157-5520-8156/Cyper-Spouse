'use strict';

// Wire the engine to the page chrome: mode toggle, palette, speed, io.

async function initPixelHome() {
  await Promise.all([loadSpriteOverrides(), ActorSprites.loadOverrides()]);
  const canvas = document.getElementById('stage');
  const clockEl = document.getElementById('clock');
  const statusEl = document.getElementById('status');
  const actionsEl = document.getElementById('actions');
  const paletteEl = document.getElementById('palette');
  const modeBtn = document.getElementById('mode');
  const speedBtn = document.getElementById('speed');
  const exportBtn = document.getElementById('export');
  const importBtn = document.getElementById('import');
  const resetBtn = document.getElementById('reset');
  const ioBox = document.getElementById('iobox');
  const ioText = document.getElementById('iotext');
  const ioApply = document.getElementById('ioapply');
  const ioClose = document.getElementById('ioclose');
  const editHint = document.getElementById('edit-hint');

  const ui = {
    onStatus(clock, status) {
      clockEl.textContent = clock;
      statusEl.textContent = status;
    },
    onInteractionsChanged(list) {
      actionsEl.innerHTML = '';
      for (const it of list) {
        const btn = document.createElement('button');
        btn.textContent = `${it.label}`;
        btn.title = `${it.item.def.name} · ${it.name}`;
        btn.addEventListener('click', () => engine.dispatch(it.name, { manual: true }));
        actionsEl.appendChild(btn);
      }
    },
    onEditStateChanged() {
      [...paletteEl.children].forEach(b => b.classList.toggle('active', b.dataset.type === engine.ghost));
    },
  };

  const engine = new Engine(canvas, ui);
  window.engine = engine;   // console access for debugging / driving demos

  // furniture palette (edit mode)
  for (const [type, def] of Object.entries(CATALOG)) {
    const btn = document.createElement('button');
    btn.textContent = def.name;
    btn.dataset.type = type;
    btn.addEventListener('click', () => {
      engine.ghost = engine.ghost === type ? null : type;
      engine.grabbed = null;
      [...paletteEl.children].forEach(b => b.classList.toggle('active', b.dataset.type === engine.ghost));
    });
    paletteEl.appendChild(btn);
  }

  document.addEventListener('keydown', e => {
    if (e.key === 'r' || e.key === 'R') {
      if (engine.rotateSelection()) e.preventDefault();
    }
  });

  modeBtn.addEventListener('click', () => {
    engine.mode = engine.mode === 'live' ? 'edit' : 'live';
    engine.ghost = null; engine.grabbed = null;
    const editing = engine.mode === 'edit';
    modeBtn.textContent = editing ? '▶ 回到生活' : '✎ 布置房间';
    modeBtn.classList.toggle('editing', editing);
    document.body.classList.toggle('editing', editing);
    if (!editing) [...paletteEl.children].forEach(b => b.classList.remove('active'));
  });

  const speeds = [60, 240, 960];
  let speedIdx = 0;
  speedBtn.addEventListener('click', () => {
    speedIdx = (speedIdx + 1) % speeds.length;
    engine.timeScale = speeds[speedIdx];
    speedBtn.textContent = `时间 ×${speeds[speedIdx] / 60}`;
  });

  exportBtn.addEventListener('click', () => {
    ioText.value = engine.exportLayout();
    ioBox.classList.add('open');
    ioText.select();
  });
  importBtn.addEventListener('click', () => {
    ioText.value = '';
    ioText.placeholder = '把布局 JSON 粘贴到这里，然后点应用';
    ioBox.classList.add('open');
  });
  ioApply.addEventListener('click', () => {
    try {
      engine.importLayout(ioText.value);
      ioBox.classList.remove('open');
    } catch (err) {
      alert(`布局无效：${err.message}`);
    }
  });
  ioClose.addEventListener('click', () => ioBox.classList.remove('open'));
  resetBtn.addEventListener('click', () => { if (confirm('恢复默认布局？')) engine.resetLayout(); });

  requestAnimationFrame(t => engine.frame(t));
}

if (document.readyState === 'loading') window.addEventListener('DOMContentLoaded', initPixelHome);
else initPixelHome();

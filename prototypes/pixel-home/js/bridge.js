'use strict';

// ---------------------------------------------------------------------------
// Dashboard bridge: maps daemon scene-state messages (posted by the World v2
// dashboard host page into this iframe) onto the pixel-home engine.
//
// Standalone opens (file:// or direct /pixel-home/index.html) receive no
// messages, so the built-in SCHEDULE keeps running. Query modes only alter
// page chrome (?embed=1) or enter the existing editor (?edit=1). While daemon
// messages keep arriving the daemon owns the actor (schedule suspended); when
// they stop for TAKEOVER_MS the room hands control back to its own schedule.
//
// Message contract (host -> iframe, same origin):
//   {type:'zhizhi-scene-state', v:1,
//    active:{activity_kind, location_ref}|null,
//    at_home:boolean, local_hour:number|null}
// ---------------------------------------------------------------------------

const PixelHomeBridge = (() => {
  const MESSAGE_TYPE = 'zhizhi-scene-state';
  const MESSAGE_VERSION = 1;
  // Hand control back to the autonomous schedule after three missed daemon
  // polls (the dashboard posts every ~30s).
  const TAKEOVER_MS = 90000;

  function queryModes(search) {
    const params = new URLSearchParams(search || '');
    return {
      embed: params.get('embed') === '1',
      edit: params.get('edit') === '1',
    };
  }

  function applyQueryMode(search, documentRef) {
    const modes = queryModes(search);
    if (documentRef && documentRef.body) {
      documentRef.body.classList.toggle('embed', modes.embed);
    }
    return modes;
  }

  // Reuse the editor's public UI control so mode labels, body classes, and
  // palette state stay in sync with main.js.
  function enterEditMode(engine, editControl) {
    if (!engine || !editControl) return false;
    if (engine.mode !== 'edit') editControl.click();
    return engine.mode === 'edit';
  }

  // daemon activity_kind -> engine interaction keys, tried in order.  Rules
  // match by prefix, first hit wins, so specific ids sit above their
  // category prefix.  Keys resolve against the live CATALOG interactions at
  // dispatch time, which keeps the mapping valid across layout edits.
  const ACTIVITY_KEY_RULES = [
    ['meal.dorm_cooking', ['cook', 'eat']],
    ['meal.make_drink', ['cook', 'eat']],
    ['social.family_call', ['phone', 'relax']],
    ['leisure.digital_browse', ['phone', 'relax']],
    ['shared.', ['phone', 'relax']],
    ['sleep.', ['sleep']],
    ['study.', ['study']],
    ['creative.', ['study']],
    ['meal.', ['eat']],
    ['household.', ['tidy']],
    ['routine.', ['dress', 'tidy']],
    ['recovery.', ['relax', 'sit']],
    ['leisure.', ['relax', 'sit']],
    ['social.', ['relax', 'sit']],
  ];
  // Unmatched kinds (and empty gaps between activities) settle on the sofa.
  const DEFAULT_KEYS = ['relax', 'sit'];

  function activityKeys(activityKind) {
    const kind = String(activityKind || '');
    for (const [prefix, keys] of ACTIVITY_KEY_RULES) {
      if (kind.startsWith(prefix)) {
        return [...keys, ...DEFAULT_KEYS.filter(key => !keys.includes(key))];
      }
    }
    return [...DEFAULT_KEYS];
  }

  // Pure decision step (unit-tested):
  //   null                        -> not a scene-state message, ignore
  //   {goal:'entry'}              -> she is out: wait by the door
  //   {goal:'interaction', keys}  -> she is home: try these interactions
  function directiveFor(message) {
    if (!message || message.type !== MESSAGE_TYPE || message.v !== MESSAGE_VERSION) return null;
    if (message.at_home === false) return { goal: 'entry' };
    return {
      goal: 'interaction',
      keys: activityKeys(message.active && message.active.activity_kind),
    };
  }

  function pickInteraction(engine, keys) {
    for (const key of keys) {
      const candidates = engine.availableInteractions()
        .filter(it => it.key === key)
        .sort((a, b) => engine.dist(a.approach) - engine.dist(b.approach));
      if (candidates.length) return candidates[0];
    }
    return null;
  }

  let scheduleTimer = null;
  let editModeWaiter = null;

  function apply(engine, directive) {
    if (!directive || engine.mode !== 'live') return;
    // Continuous takeover: every message re-suspends the schedule and re-arms
    // the hand-back timer.
    engine.autoLife = false;
    if (scheduleTimer) clearTimeout(scheduleTimer);
    scheduleTimer = setTimeout(() => { engine.autoLife = true; }, TAKEOVER_MS);
    if (scheduleTimer.unref) scheduleTimer.unref(); // never keeps node tests alive
    if (directive.goal === 'entry') {
      // No hide/remove-actor capability in the engine: she waits by the door
      // (layout entry tile) while the daemon says she is out.
      const [ex, ey] = engine.layout.entry;
      const ax = Math.round(engine.actor.pos[0]);
      const ay = Math.round(engine.actor.pos[1]);
      if (engine.actor.activity) engine.endActivity();
      if (ax !== ex || ay !== ey) engine.walkTo([ex, ey], { manual: true });
      return;
    }
    const it = pickInteraction(engine, directive.keys);
    if (!it) {
      if (engine.actor.activity) engine.endActivity();
      return;
    }
    const current = engine.actor.activity || engine.actor.pendingAct;
    if (current && current.name === it.name) return; // already doing/heading there
    engine.dispatch(it.name, { manual: true });
  }

  // main.js builds the engine asynchronously (sprite overrides load first);
  // buffer the newest directive until window.engine exists.
  let pendingDirective = null;
  let engineWaiter = null;

  function onMessage(event) {
    if (event.origin !== window.location.origin) return;
    const directive = directiveFor(event.data);
    if (!directive) return;
    if (window.engine) { apply(window.engine, directive); return; }
    pendingDirective = directive;
    if (!engineWaiter) {
      engineWaiter = setInterval(() => {
        if (!window.engine) return;
        clearInterval(engineWaiter);
        engineWaiter = null;
        const queued = pendingDirective;
        pendingDirective = null;
        apply(window.engine, queued);
      }, 300);
      if (engineWaiter.unref) engineWaiter.unref();
    }
  }

  if (typeof window !== 'undefined' && typeof window.addEventListener === 'function') {
    window.addEventListener('message', onMessage);
  }

  function initializeQueryMode() {
    if (typeof window === 'undefined' || typeof document === 'undefined') return;
    const modes = applyQueryMode(window.location.search, document);
    if (!modes.edit) return;
    const tryEnterEdit = () => enterEditMode(
      window.engine,
      document.getElementById('mode'),
    );
    if (tryEnterEdit()) return;
    editModeWaiter = setInterval(() => {
      if (!tryEnterEdit()) return;
      clearInterval(editModeWaiter);
      editModeWaiter = null;
    }, 50);
    if (editModeWaiter.unref) editModeWaiter.unref();
  }

  if (typeof document !== 'undefined') {
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', initializeQueryMode);
    } else {
      initializeQueryMode();
    }
  }

  return {
    MESSAGE_TYPE,
    MESSAGE_VERSION,
    ACTIVITY_KEY_RULES,
    DEFAULT_KEYS,
    activityKeys,
    directiveFor,
    pickInteraction,
    apply,
    onMessage,
    queryModes,
    applyQueryMode,
    enterEditMode,
  };
})();

if (typeof window !== 'undefined') window.PixelHomeBridge = PixelHomeBridge;

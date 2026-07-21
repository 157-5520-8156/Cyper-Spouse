const assert = require('node:assert/strict');
const test = require('node:test');

global.window = globalThis;
require('../../prototypes/pixel-home/js/bridge.js');
const bridge = window.PixelHomeBridge;

const message = (overrides = {}) => ({
  type: 'zhizhi-scene-state', v: 1,
  active: { activity_kind: 'study.essay_writing', location_ref: 'location:ecnu-dorm-room' },
  at_home: true, local_hour: 20.5,
  ...overrides,
});

function fakeEngine({ interactions = [], entry = [1, 2], pos = [5, 5] } = {}) {
  const calls = { dispatch: [], walkTo: [], ended: 0 };
  return {
    calls,
    mode: 'live',
    autoLife: true,
    layout: { entry },
    actor: { pos: [...pos], activity: null, pendingAct: null },
    availableInteractions: () => interactions,
    dist: tile => Math.abs(tile[0] - pos[0]) + Math.abs(tile[1] - pos[1]),
    dispatch: (name, opts) => { calls.dispatch.push([name, opts]); return true; },
    walkTo: (tile, opts) => { calls.walkTo.push([tile, opts]); return true; },
    endActivity: () => { calls.ended += 1; },
  };
}

test('prefix rules map daemon activity kinds onto engine interaction keys', () => {
  assert.equal(bridge.activityKeys('study.essay_writing')[0], 'study');
  assert.equal(bridge.activityKeys('sleep.prepare_for_bed')[0], 'sleep');
  assert.deepEqual(bridge.activityKeys('meal.dorm_cooking').slice(0, 2), ['cook', 'eat']);
  assert.equal(bridge.activityKeys('meal.canteen_meal')[0], 'eat');
  assert.equal(bridge.activityKeys('household.tidy_small_things')[0], 'tidy');
  assert.deepEqual(bridge.activityKeys('social.family_call').slice(0, 2), ['phone', 'relax']);
});

test('unknown kinds and empty gaps fall back to the sofa defaults', () => {
  assert.deepEqual(bridge.activityKeys('errand.pick_up_parcel'), bridge.DEFAULT_KEYS);
  assert.deepEqual(bridge.activityKeys(null), bridge.DEFAULT_KEYS);
  const directive = bridge.directiveFor(message({ active: null }));
  assert.deepEqual(directive, { goal: 'interaction', keys: bridge.DEFAULT_KEYS });
});

test('foreign or future messages are ignored entirely', () => {
  assert.equal(bridge.directiveFor(null), null);
  assert.equal(bridge.directiveFor({ type: 'other', v: 1 }), null);
  assert.equal(bridge.directiveFor(message({ v: 2 })), null);
});

test('query modes distinguish read-only embeds from the editor page', () => {
  assert.deepEqual(bridge.queryModes('?embed=1&hour=20.5'), { embed: true, edit: false });
  assert.deepEqual(bridge.queryModes('?edit=1'), { embed: false, edit: true });
  assert.deepEqual(bridge.queryModes('?embed=0&edit=yes'), { embed: false, edit: false });
});

test('embed mode applies only the dedicated body class', () => {
  const toggles = [];
  const documentRef = {
    body: { classList: { toggle: (...args) => toggles.push(args) } },
  };
  const modes = bridge.applyQueryMode('?embed=1', documentRef);
  assert.deepEqual(modes, { embed: true, edit: false });
  assert.deepEqual(toggles, [['embed', true]]);
});

test('edit startup reuses the existing mode control', () => {
  const engine = { mode: 'live' };
  let clicks = 0;
  const editControl = {
    click() {
      clicks += 1;
      engine.mode = 'edit';
    },
  };
  assert.equal(bridge.enterEditMode(engine, editControl), true);
  assert.equal(bridge.enterEditMode(engine, editControl), true);
  assert.equal(clicks, 1);
});

test('away from home walks her to the entry and suspends the schedule', () => {
  const engine = fakeEngine();
  bridge.apply(engine, bridge.directiveFor(message({ at_home: false })));
  assert.equal(engine.autoLife, false);
  assert.deepEqual(engine.calls.walkTo, [[[1, 2], { manual: true }]]);
  assert.deepEqual(engine.calls.dispatch, []);
});

test('already waiting at the entry stays put instead of re-walking', () => {
  const engine = fakeEngine({ pos: [1, 2] });
  bridge.apply(engine, bridge.directiveFor(message({ at_home: false })));
  assert.deepEqual(engine.calls.walkTo, []);
});

test('at home dispatches the nearest interaction matching the activity', () => {
  const study = { name: 'study', key: 'study', approach: [1, 6] };
  const relax = { name: 'relax', key: 'relax', approach: [6, 4] };
  const engine = fakeEngine({ interactions: [relax, study] });
  bridge.apply(engine, bridge.directiveFor(message()));
  assert.equal(engine.autoLife, false);
  assert.deepEqual(engine.calls.dispatch, [['study', { manual: true }]]);
});

test('same-activity messages do not re-dispatch while she is already there', () => {
  const study = { name: 'study', key: 'study', approach: [1, 6] };
  const engine = fakeEngine({ interactions: [study] });
  engine.actor.activity = study;
  bridge.apply(engine, bridge.directiveFor(message()));
  assert.deepEqual(engine.calls.dispatch, []);
});

test('no matching interaction leaves her idle instead of guessing a spot', () => {
  const engine = fakeEngine({ interactions: [] });
  engine.actor.activity = { name: 'relax' };
  bridge.apply(engine, bridge.directiveFor(message()));
  assert.deepEqual(engine.calls.dispatch, []);
  assert.equal(engine.calls.ended, 1);
});

test('edit mode is never fought over', () => {
  const engine = fakeEngine();
  engine.mode = 'edit';
  bridge.apply(engine, bridge.directiveFor(message({ at_home: false })));
  assert.equal(engine.autoLife, true);
  assert.deepEqual(engine.calls.walkTo, []);
});

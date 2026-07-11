const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');

global.window = globalThis;
require('../../src/companion_daemon/static/room/editor.js');
require('../../src/companion_daemon/static/room/runtime.js');

const root = path.resolve(__dirname, '../..');
const bundle = JSON.parse(fs.readFileSync(
  path.join(root, 'assets/dashboard/rooms/zhizhi-home/runtime/room.bundle.json'),
  'utf8'
));

function fakeCanvas() {
  const context = new Proxy({}, {get: (target, key) => target[key] || (() => {})});
  return {width:1000, height:760, getContext:() => context};
}

test('runtime paths to manifest interactions without furniture-specific dashboard data', () => {
  const runtime = new DashboardRoomRuntime(fakeCanvas(), bundle, {});

  runtime.setActor({location:'sofa', action:'relax', expression:'neutral', time_of_day:'day'});

  assert.equal(runtime.actor.interaction, bundle.interactions.sofa);
  assert.deepEqual(runtime.actor.target, [5, 7, 0]);
  assert.deepEqual(runtime.actor.path.at(-1), [5, 7, 0]);
});

test('semantic interaction depth places a pose above its furniture front', () => {
  const runtime = new DashboardRoomRuntime(fakeCanvas(), bundle, {});
  runtime.activatePreview(new URLSearchParams('demo=activity&spot=bed'));
  const bed = bundle.objects.find(item => item.id === 'bed');
  const front = bed.layers.find(layer => layer.role === 'front');

  assert.equal(
    runtime.actorDepth(),
    runtime.depthKey(bed.tile, front.depthBias + 100)
  );
});

test('preview routes and object audits come from the room bundle', () => {
  const runtime = new DashboardRoomRuntime(fakeCanvas(), bundle, {});
  const preview = runtime.activatePreview(new URLSearchParams('demo=audit&object=dining&side=behind'));

  assert.deepEqual(runtime.actor.position, bundle.objects.find(item => item.id === 'dining').audit.behind);
  assert.match(preview.status, /dining · behind/);
});

test('actor audit reports not-applicable for a non-occluding object', () => {
  const configuredBundle = structuredClone(bundle);
  const desk = configuredBundle.objects[0];
  desk.audits = {...desk.audits, behind:false, front:false};
  desk.audit = {};
  const runtime = new DashboardRoomRuntime(fakeCanvas(), configuredBundle, {});

  const preview = runtime.activatePreview(new URLSearchParams('demo=audit&object=desk&side=behind'));

  assert.match(preview.status, /不适用/);
});

test('room editor exports the selected origin as a manifest fragment', () => {
  const runtime = new DashboardRoomRuntime(fakeCanvas(), bundle, {});
  const editor = new DashboardRoomEditor(runtime);

  assert.deepEqual(JSON.parse(editor.manifestSnippet()), {
    id:'desk',
    layers:[{role:'front', origin:[35, 445]}]
  });
});

test('generic object layers render by role without legacy furniture fields', () => {
  const layeredBundle = structuredClone(bundle);
  layeredBundle.objects[0].layers.unshift({role:'back', image:'deskBack', origin:[10, 20], depthBias:-200});
  const calls = [];
  const context = new Proxy({drawImage:(...args) => calls.push(args)}, {get:(target, key) => target[key] || (() => {})});
  const runtime = new DashboardRoomRuntime({width:1000, height:760, getContext:() => context}, layeredBundle, {});
  runtime.images.deskBack = {width:12, height:10};

  runtime.drawObjectLayers(['shadow', 'back', 'body']);

  assert.equal(calls.length, 1);
  assert.equal(calls[0][0], runtime.images.deskBack);
});

test('non-front object layers use depth rather than manifest order', () => {
  const layeredBundle = structuredClone(bundle);
  const first = layeredBundle.objects[0], second = layeredBundle.objects[1];
  first.tile = [7, 7, 0]; second.tile = [0, 0, 0];
  first.layers = [{role:'body', image:'farBody', origin:[0, 0], depthBias:0}];
  second.layers = [{role:'body', image:'nearBody', origin:[0, 0], depthBias:0}];
  const calls = [];
  const context = new Proxy({drawImage:image => calls.push(image)}, {get:(target, key) => target[key] || (() => {})});
  const runtime = new DashboardRoomRuntime({width:1000, height:760, getContext:() => context}, layeredBundle, {});
  runtime.images.farBody = {width:1, height:1}; runtime.images.nearBody = {width:1, height:1};

  runtime.drawObjectLayers(['body']);

  assert.deepEqual(calls, [runtime.images.nearBody, runtime.images.farBody]);
});

test('room editor redraws immediately when deterministic preview is frozen', () => {
  const runtime = new DashboardRoomRuntime(fakeCanvas(), bundle, {});
  const editor = new DashboardRoomEditor(runtime);
  let draws = 0;
  runtime.draw = () => { draws += 1; };

  editor.redraw();
  runtime.running = true;
  editor.redraw();

  assert.equal(draws, 1);
});

test('atomization preview hides or solos rendering without changing occupancy', () => {
  const runtime = new DashboardRoomRuntime(fakeCanvas(), bundle, {});
  const beforePath = runtime.pathfind([7, 4, 0], [5, 7, 0]);

  const hidden = runtime.activatePreview(new URLSearchParams('demo=atomization&object=desk&mode=hidden'));
  assert.equal(runtime.visibleObjects().some(item => item.id === 'desk'), false);
  assert.deepEqual(runtime.pathfind([7, 4, 0], [5, 7, 0]), beforePath);
  assert.match(hidden.status, /desk · hidden · partial/);

  runtime.activatePreview(new URLSearchParams('demo=atomization&object=desk&mode=solo'));
  assert.deepEqual(runtime.visibleObjects().map(item => item.id), ['desk']);

  runtime.activatePreview(new URLSearchParams('demo=atomization&object=desk&mode=layers&role=front'));
  assert.deepEqual(runtime.visibleLayers(bundle.objects[0]).map(layer => layer.role), ['front']);
  assert.deepEqual(runtime.pathfind([7, 4, 0], [5, 7, 0]), beforePath);
});

test('runtime path occupancy uses the generic occupancy contract', () => {
  const configuredBundle = structuredClone(bundle);
  assert.equal(configuredBundle.objects.some(item => 'footprint' in item), false);
  const runtime = new DashboardRoomRuntime(fakeCanvas(), configuredBundle, {});

  assert.equal(runtime.pathfind([7, 4, 0], [7, 0, 0]).length, 0);
});

test('action interaction, effects, location facing, and audit poses come from the bundle', () => {
  const configuredBundle = structuredClone(bundle);
  configuredBundle.behavior.actionDefinitions.compose_reply = {
    interaction:'phone', phoneProp:true, effect:'focus'
  };
  const runtime = new DashboardRoomRuntime(fakeCanvas(), configuredBundle, {});

  runtime.setActor({location:'sofa', action:'compose_reply', expression:'neutral', time_of_day:'day'});
  assert.equal(runtime.actor.interaction, configuredBundle.interactions.phone);
  assert.equal(runtime.actionDefinition('compose_reply').effect, 'focus');

  runtime.setActor({location:'window', action:'idle', expression:'neutral', time_of_day:'day'});
  assert.equal(runtime.actor.targetFacing, configuredBundle.behavior.locationFacing.window);

  runtime.activatePreview(new URLSearchParams('demo=audit&object=sofa&side=behind'));
  assert.equal(runtime.actor.pose, configuredBundle.objects.find(item => item.id === 'sofa').auditPose.behind);
});

test('hiding an interaction object also suppresses its local effect', () => {
  const calls = [];
  const context = new Proxy({ellipse:() => calls.push('focus')}, {get:(target, key) => target[key] || (() => {})});
  const runtime = new DashboardRoomRuntime({width:1000, height:760, getContext:() => context}, bundle, {});
  runtime.setActor({location:'sofa', action:'read_phone', expression:'neutral', time_of_day:'day'});
  runtime.hiddenObjectIds.add(runtime.actor.interaction.object);

  runtime.drawEffects(0);

  assert.deepEqual(calls, []);
});

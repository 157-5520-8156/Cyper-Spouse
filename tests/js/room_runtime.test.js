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

  assert.equal(
    runtime.actorDepth(),
    runtime.depthKey(bed.tile, bed.frontOccluder.depthBias + 100)
  );
});

test('preview routes and object audits come from the room bundle', () => {
  const runtime = new DashboardRoomRuntime(fakeCanvas(), bundle, {});
  const preview = runtime.activatePreview(new URLSearchParams('demo=audit&object=dining&side=behind'));

  assert.deepEqual(runtime.actor.position, bundle.objects.find(item => item.id === 'dining').audit.behind);
  assert.match(preview.status, /dining · behind/);
});

test('room editor exports the selected origin as a manifest fragment', () => {
  const runtime = new DashboardRoomRuntime(fakeCanvas(), bundle, {});
  const editor = new DashboardRoomEditor(runtime);

  assert.deepEqual(JSON.parse(editor.manifestSnippet()), {
    id:'desk',
    frontOccluder:{origin:[35, 445]}
  });
});

test('optional back layers render before actor/front depth sorting', () => {
  const layeredBundle = structuredClone(bundle);
  layeredBundle.objects[0].backLayer = {image:'deskBack', origin:[10, 20]};
  const calls = [];
  const context = new Proxy({drawImage:(...args) => calls.push(args)}, {get:(target, key) => target[key] || (() => {})});
  const runtime = new DashboardRoomRuntime({width:1000, height:760, getContext:() => context}, layeredBundle, {});
  runtime.images.deskBack = {width:12, height:10};

  runtime.drawBackLayers();

  assert.equal(calls.length, 1);
  assert.equal(calls[0][0], runtime.images.deskBack);
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

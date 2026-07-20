const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');

global.window = globalThis;
require('../../src/companion_daemon/static/room/tile-runtime.js');

const root = path.resolve(__dirname, '../..');
const scene = JSON.parse(fs.readFileSync(
  path.join(root, 'assets/dashboard/tile-rooms/zhizhi-home/runtime/room.bundle.json'), 'utf8'
));

function fakeCanvas() {
  const context = new Proxy({}, {get:(target, key) => target[key] || (() => {})});
  return {width:1536, height:1080, dataset:{}, getContext:() => context};
}

test('TileRoom locks the 2:1 projection and accepts the zhizhi grid manifest', () => {
  TileRoomRuntime.validate(scene);
  const runtime = new TileRoomRuntime(fakeCanvas(), structuredClone(scene), {});
  runtime.fitStage();
  const origin = runtime.project([0, 0, 0]);
  const x = runtime.project([1, 0, 0]);
  const y = runtime.project([0, 1, 0]);
  const z = runtime.project([0, 0, 1]);

  assert.equal(x[0] - origin[0], 64 * runtime.stage.scale);
  assert.equal(y[0] - origin[0], -64 * runtime.stage.scale);
  assert.equal(x[1] - origin[1], 32 * runtime.stage.scale);
  assert.equal(z[1] - origin[1], -64 * runtime.stage.scale);
});

test('A* does not cross colliders and interactions route through their declared approach tile', () => {
  const runtime = new TileRoomRuntime(fakeCanvas(), structuredClone(scene), {});
  const pathToDesk = runtime.pathfind([6, 9], [1, 3]);

  assert.deepEqual(pathToDesk.at(-1), [1, 3, 0]);
  assert.equal(pathToDesk.some(point => runtime.blocked.has(runtime.key(point))), false);
  runtime.setActor({location:'desk', action:'study', expression:'neutral'});
  assert.equal(runtime.actor.interaction, runtime.scene.interactions.study);
  assert.deepEqual(runtime.actor.target, [1, 3]);
});

test('walls block crossings, without turning their whole adjacent floor row into furniture', () => {
  const configured = structuredClone(scene);
  configured.walls.push({id:'inner-wall', from:[6, 0], to:[6, 10], height:2, material:'cream'});
  const runtime = new TileRoomRuntime(fakeCanvas(), configured, {});

  assert.equal(runtime.blocked.has(runtime.key([6, 0])), false);
  assert.equal(runtime.wallEdges.has(TileRoomRuntime.edgeKey([5, 6], [6, 6])), true);
  assert.deepEqual(runtime.pathfind([5, 6], [6, 6]), []);
});

test('wide furniture is split into local x/y depth strips rather than one front plane', () => {
  const runtime = new TileRoomRuntime(fakeCanvas(), structuredClone(scene), {});
  runtime.activatePreview(new URLSearchParams('demo=activity&spot=relax'));
  const sofa = runtime.objectById.get('sofa');
  const parts = runtime.objectRenderParts(sofa, 0);

  assert.equal(runtime.actorDepth(), runtime.depth(runtime.actor.position));
  assert.ok(parts.some(part => part.depth < runtime.actorDepth()));
  assert.ok(parts.some(part => part.depth > runtime.actorDepth()));
  assert.ok(new Set(parts.map(part => part.depth)).size > 3);
});

test('manifest validation catches a perspective-sized tile and blocked route', () => {
  const invalid = structuredClone(scene);
  invalid.projection.tile = [160, 64];
  invalid.routes.tour[1] = [1, 2];

  assert.throws(() => TileRoomRuntime.validate(invalid), /fixed 2:1 128×64 contract/);
});

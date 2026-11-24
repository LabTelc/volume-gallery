// Crop shapes in an animation: what a view records, how two views blend, and what is given to the renderer.
import test from "node:test";
import assert from "node:assert/strict";

import { applyCrop, hasCrop, interpolateCrop, interpolatePose, isCropped, readCrop, readPose } from "../js/animate.js";
import { axesOf } from "../js/crop-math.js";

const shape = (type, extra = {}) => ({ type, mode: "inside", enabled: true, center: [0.5, 0.5, 0.5], size: [0.25, 0.25, 0.25], rotation: [0, 0, 0], ...extra });
const pose = (extra = {}) => ({ back: [0, 0, 1], up: [0, 1, 0], distance: 5, target: [0, 0, 0], ...extra });
const near = (a, b, eps = 1e-6) => assert.ok(a.every((v, i) => Math.abs(v - b[i]) < eps), `${a} vs ${b}`);

// A stand-in for kiln-render's renderer: keeps what setCropShapes was given.
function fakeRenderer(shapes = []) {
  const r = {
    shapes: shapes.map((s) => structuredClone(s)),
    sets: 0, dirty: 0,
    getCropShapes() { return structuredClone(this.shapes); },
    setCropShapes(list) { this.shapes = structuredClone(list); this.sets++; },
    markDirty() { this.dirty++; },
    resetAccumulation() {},
    clipMin: new Float32Array([0, 0, 0]), clipMax: new Float32Array([1, 1, 1]),
  };
  return r;
}

test("centre, size and rotation of a shape both views have blend", () => {
  const a = [shape("sphere", { center: [0.2, 0.5, 0.5], size: [0.1, 0, 0] })];
  const b = [shape("sphere", { center: [0.6, 0.5, 0.5], size: [0.3, 0, 0] })];
  const mid = interpolateCrop(a, b, 0.5);
  assert.equal(mid.length, 1);
  near(mid[0].center, [0.4, 0.5, 0.5]);
  near(mid[0].size, [0.2, 0, 0]);
  near(interpolateCrop(a, b, 0)[0].center, a[0].center);
  near(interpolateCrop(a, b, 1)[0].center, b[0].center);
});

test("rotation turns the short way and stays a valid rotation", () => {
  const a = [shape("box", { rotation: [0, 0, 0] })];
  const b = [shape("box", { rotation: [0, 0, 90] })];
  const mid = interpolateCrop(a, b, 0.5)[0];
  near(mid.rotation, [0, 0, 45], 1e-4);
  // the local X axis of the half-way shape is 45 degrees between world X and Y
  const [x] = axesOf(mid.rotation);
  near(x, [Math.SQRT1_2, Math.SQRT1_2, 0], 1e-6);
  // 170 -> -170 is a 20 degree turn through 180, not 340 the long way
  const c = interpolateCrop([shape("box", { rotation: [0, 0, 170] })], [shape("box", { rotation: [0, 0, -170] })], 0.5)[0];
  assert.ok(Math.abs(Math.abs(c.rotation[2]) - 180) < 1e-3, `rotation ${c.rotation}`);
});

test("a shape only one view has, or whose side differs, switches half way", () => {
  const sph = shape("sphere");
  assert.equal(interpolateCrop([sph], [], 0.25).length, 1);
  assert.equal(interpolateCrop([sph], [], 0.75).length, 0);
  assert.equal(interpolateCrop([], [sph], 0.25).length, 0);
  assert.equal(interpolateCrop([], [sph], 0.75).length, 1);
  const flip = interpolateCrop([shape("box")], [shape("box", { mode: "outside" })], 0.25)[0];
  assert.equal(flip.mode, "inside");
  assert.equal(interpolateCrop([shape("box")], [shape("box", { mode: "outside" })], 0.75)[0].mode, "outside");
});

test("poses: a view without crop leaves it alone, so the other one is held", () => {
  const a = pose({ crop: [shape("sphere")] });
  const b = pose();
  assert.equal(hasCrop(b), false);
  assert.equal(interpolatePose(a, b, 0.5).crop.length, 1);
  assert.equal(interpolatePose(b, a, 0.5).crop.length, 1);
  assert.equal(interpolatePose(b, b, 0.5).crop, undefined);
  // an empty list is a real answer: no cropping at that view
  const none = pose({ crop: [] });
  assert.equal(hasCrop(none), true);
  assert.equal(isCropped(none), false);
  assert.equal(isCropped(a), true);
  assert.equal(interpolatePose(a, none, 1).crop.length, 0);
});

test("readPose records the shapes that are on, and can leave them out", () => {
  const r = fakeRenderer([shape("plane", { enabled: false }), shape("sphere", { center: [0.123456, 0.5, 0.5] })]);
  const camera = { back: [0, 0, 1], up: [0, 1, 0], distance: 5, target: [0, 0, 0] };
  const p = readPose(camera, r);
  assert.deepEqual(p.crop.map((s) => s.type), ["sphere"]);
  assert.equal(p.crop[0].center[0], 0.1235); // short numbers
  assert.equal(readPose(camera, r, null, { crop: false }).crop, undefined);
  assert.equal(readPose(camera, r, null, { clip: false }).clipMin, undefined);
  assert.equal(readPose(camera).crop, undefined); // no renderer: the camera alone
  assert.equal(readCrop({}), null); // a renderer without crop shapes
});

test("applyCrop sends all four kinds, switches the others off and skips what did not change", () => {
  globalThis.window = globalThis.window ?? new EventTarget();
  globalThis.CustomEvent = globalThis.CustomEvent ?? class extends Event { constructor(t, o) { super(t); this.detail = o?.detail; } };
  const r = fakeRenderer([shape("box", { center: [0.3, 0.3, 0.3] })]);
  let events = 0;
  window.addEventListener("vg-crop-changed", () => events++);
  assert.equal(applyCrop(r, [shape("sphere")]), true);
  assert.deepEqual(r.shapes.map((s) => [s.type, s.enabled]), [["plane", false], ["box", false], ["cylinder", false], ["sphere", true]]);
  assert.deepEqual(r.shapes[1].center, [0.3, 0.3, 0.3]); // a switched-off shape keeps its place
  assert.equal(events, 1);
  assert.equal(applyCrop(r, [shape("sphere")]), false); // same again: nothing is sent
  assert.equal(r.sets, 1);
  assert.equal(applyCrop(r, []), true); // "no cropping" switches everything off
  assert.ok(r.shapes.every((s) => !s.enabled));
  assert.equal(applyCrop({}, [shape("sphere")]), false); // a renderer without crop shapes is left alone
});

test("a bookmark's crop shapes become a key's: only those that are on", async () => {
  const { poseOfBookmark } = await import("../js/animate.js");
  const entry = (options, pose = {}) => ({ name: "b", spec: { pose: pose, options } });
  const base = { back: [0, 0, 1], up: [0, 1, 0], distance: 5, target: [0, 0, 0] };
  const key = poseOfBookmark(entry({ crop: [shape("plane", { enabled: false }), shape("box", { mode: "outside" })] }, base));
  assert.deepEqual(key.crop.map((s) => [s.type, s.mode]), [["box", "outside"]]);
  assert.deepEqual(poseOfBookmark(entry({ crop: [shape("box", { enabled: false })] }, base)).crop, []); // all off: an uncropped view
  assert.equal(poseOfBookmark(entry({}, base)).crop, undefined); // an older bookmark: the cropping is left alone
});

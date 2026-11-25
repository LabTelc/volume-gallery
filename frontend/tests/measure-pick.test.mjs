import test from "node:test";
import assert from "node:assert/strict";

import { brickDecoder, loadCoarseVolume, loadCoarseVolumeFromPlanes, pickVolume, CoarseVolume } from "../js/slice/measure.js";

const brickMeta = { levels: [{ lod: 0, dimensions: [2, 2, 2], brickGrid: [1, 1, 1] }], brickSize: 2, physicalBrickSize: 2, dimensions: [2, 2, 2] };
const load = (data, o) => loadCoarseVolume({ getMetadata: () => brickMeta, loadBrick: async () => ({ data }) }, o);
const half = (x) => { // float16 bits of a few exactly representable numbers
  const t = { 0.25: 0x3400, 0.5: 0x3800, 0.75: 0x3a00, 1: 0x3c00 }; return t[x];
};

test("coarse volume: 16-bit integer data is a fraction of 65535, not float16 bits", async () => {
  const vol = await load(new Uint16Array([0, 65535, 32768, 20000, 0, 0, 0, 0]), { dtype: "uint16" });
  assert.ok(Math.abs(vol.data[1] - 1) < 1e-6);
  assert.ok(Math.abs(vol.data[2] - 32768 / 65535) < 1e-6);
  assert.ok(Math.abs(vol.data[3] - 20000 / 65535) < 1e-6);
  assert.equal(brickDecoder(new Uint8Array(1))(255), 1);
});

test("coarse volume: float data is scaled to 0..1 over the data range, as the renderer does", async () => {
  // float16 data between 0.25 and 1: the renderer shows 0.25 as 0 and 1 as 1
  const bits = new Uint16Array([half(0.25), half(1), half(0.5), half(0.75), 0, 0, 0, 0].map((v) => v ?? 0));
  const vol = await load(bits, { dtype: "float32", dataRange: [0.25, 1] });
  assert.ok(Math.abs(vol.data[0] - 0) < 1e-6);
  assert.ok(Math.abs(vol.data[1] - 1) < 1e-6);
  assert.ok(Math.abs(vol.data[2] - 1 / 3) < 1e-6);
  assert.ok(Math.abs(vol.data[3] - 2 / 3) < 1e-6);
  const f32 = await load(new Float32Array([0.25, 1, 0.5, 0.75, 0, 0, 0, 0]), { dtype: "float32", dataRange: [0.25, 1] });
  assert.ok(Math.abs(f32.data[2] - 1 / 3) < 1e-6);
});

test("pick: a ray through empty space stops at the object, not at the box", () => {
  // 8^3 volume, zero except a solid block in the middle (values 0.8)
  const n = 8, data = new Float32Array(n * n * n);
  for (let z = 3; z < 5; z++) for (let y = 3; y < 5; y++) for (let x = 3; x < 5; x++) data[(z * n + y) * n + x] = 0.8;
  const vol = new CoarseVolume({ dims: { x: n, y: n, z: n }, data });
  const ray = { origin: [-5, 0.01, 0.01], dir: [1, 0, 0] };
  const size = [1, 1, 1], dims = { x: n, y: n, z: n };
  const tf = { points: [{ x: 0, y: 0 }, { x: 0.2, y: 0 }, { x: 0.4, y: 1 }, { x: 1, y: 1 }] };
  for (const opts of [{ mode: "dvr", tf }, { mode: "iso", isoValue: 0.5 }, { mode: "mip" }]) {
    const hit = pickVolume(vol, ray, size, dims, opts);
    assert.ok(hit, opts.mode);
    assert.ok(hit.point.x >= 2.5 && hit.point.x <= 3.6, `${opts.mode}: x ${hit.point.x}`);
  }
});

test("pick: the iso value is in windowed space", () => {
  const n = 8, data = new Float32Array(n * n * n);
  for (let z = 3; z < 5; z++) for (let y = 3; y < 5; y++) for (let x = 3; x < 5; x++) data[(z * n + y) * n + x] = 0.3;
  const vol = new CoarseVolume({ dims: { x: n, y: n, z: n }, data });
  const ray = { origin: [-5, 0.01, 0.01], dir: [1, 0, 0] };
  const dims = { x: n, y: n, z: n };
  const win = { windowCenter: 0.5, windowWidth: 0.5 }; // shows 0.25..0.75: a voxel of 0.3 is 0.1 in the window
  assert.equal(pickVolume(vol, ray, [1, 1, 1], dims, { mode: "iso", isoValue: 0.2, tf: win }), null);
  assert.ok(pickVolume(vol, ray, [1, 1, 1], dims, { mode: "iso", isoValue: 0.05, tf: win }));
});

test("pick: with the camera inside material there is no hit on the camera itself", () => {
  const n = 8, data = new Float32Array(n * n * n).fill(0.6); // solid everywhere
  const vol = new CoarseVolume({ dims: { x: n, y: n, z: n }, data });
  const dims = { x: n, y: n, z: n };
  const inside = { origin: [0, 0, 0], dir: [1, 0, 0] };
  for (const mode of ["iso", "dvr"]) {
    const trace = {};
    assert.equal(pickVolume(vol, inside, [1, 1, 1], dims, { mode, isoValue: 0.1, tf: {}, trace }), null, mode);
    assert.equal(trace.insideAtCamera, true);
  }
  // from outside, the face of a solid volume is the visible surface
  const hit = pickVolume(vol, { origin: [-3, 0, 0], dir: [1, 0, 0] }, [1, 1, 1], dims, { mode: "iso", isoValue: 0.1 });
  assert.ok(hit && hit.point.x < 1);
});

test("coarse volume from the server's planes: raw samples on the scale of the axis", async () => {
  const levels = [{ lod: 0, dimensions: [8, 8, 8] }, { lod: 1, dimensions: [2, 2, 3] }];
  const urls = [];
  const fetchFn = async (url) => {
    urls.push(url);
    const z = Number(/xy\/(\d+)/.exec(url)[1]);
    const plane = new Uint16Array(4).fill(1000 * (z + 1));
    return { ok: true, headers: new Map([["X-Plane-Dtype", "uint16"], ["X-Plane-Width", "2"], ["X-Plane-Height", "2"]]), arrayBuffer: async () => plane.buffer };
  };
  const vol = await loadCoarseVolumeFromPlanes({ baseUrl: "/s/tok/", levels, fullDims: [8, 8, 8], maxVoxels: 20, fetchFn });
  assert.deepEqual(urls.sort(), ["/s/tok/xy/0?level=1", "/s/tok/xy/1?level=1", "/s/tok/xy/2?level=1"]);
  assert.deepEqual(vol.dims, { x: 2, y: 2, z: 3 });
  assert.ok(Math.abs(vol.data[0] - 1000 / 65535) < 1e-6);
  assert.ok(Math.abs(vol.data[11] - 3000 / 65535) < 1e-6);
  assert.equal(vol.factor.x, 4);
  const bad = async () => ({ ok: true, headers: new Map([["X-Plane-Dtype", "uint16"], ["X-Plane-Width", "5"], ["X-Plane-Height", "2"]]), arrayBuffer: async () => new ArrayBuffer(20) });
  await assert.rejects(loadCoarseVolumeFromPlanes({ baseUrl: "/s/", levels, fullDims: [8, 8, 8], maxVoxels: 20, fetchFn: bad }), /expected 2x2/);
});

import test from "node:test";
import assert from "node:assert/strict";

import { niceScaleLength } from "../js/scalebar.js";
import { resampleHistogram, syncHistogram, transferAxis } from "../js/histogram.js";

const sum = (a) => Array.from(a).reduce((s, v) => s + v, 0);

// ------------------------------------------------------------------------------------------ scale bar

test("scale bar: the longest 1, 2 or 5 x 10^n that fits", () => {
  const cases = [
    [1, 1, "1 µm"], [1.9, 1, "1 µm"], [2, 2, "2 µm"], [4.99, 2, "2 µm"], [5, 5, "5 µm"], [9.99, 5, "5 µm"],
    [10, 10, "10 µm"], [130, 100, "100 µm"], [250, 200, "200 µm"], [999, 500, "500 µm"],
  ];
  for (const [max, um, text] of cases) assert.deepEqual(niceScaleLength(max), { um, text }, `max ${max}`);
});

test("scale bar: units switch at 1 mm and 1 m (no cm)", () => {
  assert.equal(niceScaleLength(1000).text, "1 mm");
  assert.equal(niceScaleLength(2500).text, "2 mm");
  assert.equal(niceScaleLength(10_000).text, "10 mm");
  assert.equal(niceScaleLength(5e5).text, "500 mm");
  assert.equal(niceScaleLength(1e6).text, "1 m");
  assert.equal(niceScaleLength(0.5).text, "500 nm");
  assert.equal(niceScaleLength(0.002).text, "2 nm");
});

test("scale bar: never longer than the room it has", () => {
  for (let max = 0.01; max < 1e7; max *= 1.37) assert.ok(niceScaleLength(max).um <= max, `max ${max}`);
});

test("scale bar: bad input gives null", () => {
  for (const bad of [0, -1, NaN, Infinity, -Infinity, undefined, null, "5"]) assert.equal(niceScaleLength(bad), null, String(bad));
});

// ------------------------------------------------------------------------------------------ histogram axis

test("histogram axis: integer types use their whole range", () => {
  assert.deepEqual(transferAxis({}, "uint8"), [0, 255]);
  assert.deepEqual(transferAxis(undefined, "uint16"), [0, 65535]);
});

test("histogram axis: float data uses the range the viewer derived", () => {
  assert.deepEqual(transferAxis({ dataRange: [-1.5, 2.5] }, "float32"), [-1.5, 2.5]);
  assert.deepEqual(transferAxis({ dataRange: ["0", "10"] }, "float32"), [0, 10]);
});

test("histogram axis: unknown until the range is known", () => {
  assert.equal(transferAxis({}, "float32"), null);
  assert.equal(transferAxis(null, "float32"), null);
  assert.equal(transferAxis({ dataRange: [3, 3] }, "float32"), null);
  assert.equal(transferAxis({ dataRange: [5, 1] }, "float32"), null);
  assert.equal(transferAxis({ dataRange: [1] }, "float32"), null);
});

// ------------------------------------------------------------------------------------------ resampling

test("resample: counts are conserved when the data range lies inside the axis", () => {
  const h = { min: 10, max: 20, counts: [5, 0, 7, 3, 1, 0, 0, 9, 2, 4] };
  const out = resampleHistogram(h, 0, 255, 256);
  assert.ok(Math.abs(sum(out) - 31) < 1e-3, `sum ${sum(out)}`);
});

test("resample: the same axis and bin count reproduces the counts", () => {
  const h = { min: 0, max: 8, counts: [1, 2, 3, 4, 5, 6, 7, 8] };
  assert.deepEqual(Array.from(resampleHistogram(h, 0, 8, 8)), h.counts);
});

test("resample: a coarser axis adds neighbouring bins", () => {
  const h = { min: 0, max: 8, counts: [1, 2, 3, 4, 5, 6, 7, 8] };
  assert.deepEqual(Array.from(resampleHistogram(h, 0, 8, 4)), [3, 7, 11, 15]);
});

test("resample: a finer axis shares a source bin in proportion", () => {
  const out = Array.from(resampleHistogram({ min: 0, max: 2, counts: [10, 20] }, 0, 2, 4));
  assert.deepEqual(out, [5, 5, 10, 10]);
});

test("resample: a bin that straddles the axis end is cut and the rest is dropped", () => {
  const out = resampleHistogram({ min: 0, max: 10, counts: [10, 10] }, 0, 2.5, 5);
  assert.ok(Math.abs(sum(out) - 5) < 1e-4, `sum ${sum(out)}`); // half of the first source bin lies inside
  assert.equal(sum(resampleHistogram({ min: 100, max: 200, counts: [4, 4] }, 0, 50, 10)), 0);
});

test("resample: the data keeps its position on the axis", () => {
  const out = resampleHistogram({ min: 100, max: 110, counts: [8] }, 0, 255, 255); // one bin: values 100..110
  const nonzero = Array.from(out).flatMap((v, i) => (v > 0 ? [i] : []));
  assert.deepEqual(nonzero, [100, 101, 102, 103, 104, 105, 106, 107, 108, 109]);
});

test("resample: a constant volume puts everything in one bin", () => {
  const out = resampleHistogram({ min: 128, max: 128, counts: [3, 4, 5] }, 0, 256, 256);
  assert.equal(out[128], 12);
  assert.equal(sum(out), 12);
  assert.equal(sum(resampleHistogram({ min: 999, max: 999, counts: [3] }, 0, 256, 256)), 0); // off the axis
});

test("resample: bad input gives null", () => {
  assert.equal(resampleHistogram(null, 0, 1), null);
  assert.equal(resampleHistogram({ min: 0, max: 1 }, 0, 1), null);
  assert.equal(resampleHistogram({ min: 0, max: 1, counts: "no" }, 0, 1), null);
  assert.equal(resampleHistogram({ min: 0, max: 1, counts: [1] }, 5, 5), null);
  assert.equal(resampleHistogram({ min: 0, max: 1, counts: [1] }, 5, 1), null);
});

test("resample: the default size is the 1024 bins kiln-render expects", () => {
  assert.equal(resampleHistogram({ min: 0, max: 1, counts: [1] }, 0, 1).length, 1024);
});

// ------------------------------------------------------------------------------------------ syncing

const fakeViewer = (metadata) => {
  const calls = [];
  return { metadata, transferFunction: { setHistogram: (bins) => calls.push(bins) }, calls };
};
const data = (dtype = "uint8") => ({ dtype, min: 0, max: 255, counts: Array.from({ length: 256 }, () => 1) });

test("sync: hands the resampled histogram to the transfer function", () => {
  const viewer = fakeViewer({});
  syncHistogram(viewer, data());
  assert.equal(viewer.calls.length, 1);
  assert.equal(viewer.calls[0].length, 1024);
});

test("sync: does the work only once per axis, so calling it every frame is cheap", () => {
  const viewer = fakeViewer({});
  for (let i = 0; i < 20; i++) syncHistogram(viewer, data());
  assert.equal(viewer.calls.length, 1);
});

test("sync: redoes it when the axis changes (float data whose range becomes known)", () => {
  const viewer = fakeViewer({});
  const floats = data("float32");
  syncHistogram(viewer, floats);
  assert.equal(viewer.calls.length, 0); // range not known yet
  viewer.metadata = { dataRange: [0, 1] };
  syncHistogram(viewer, floats);
  syncHistogram(viewer, floats);
  assert.equal(viewer.calls.length, 1);
  viewer.metadata = { dataRange: [0, 2] };
  syncHistogram(viewer, floats);
  assert.equal(viewer.calls.length, 2);
});

test("sync: tolerates missing pieces", () => {
  assert.doesNotThrow(() => syncHistogram(null, data()));
  assert.doesNotThrow(() => syncHistogram({}, data()));
  assert.doesNotThrow(() => syncHistogram(fakeViewer({}), null));
});

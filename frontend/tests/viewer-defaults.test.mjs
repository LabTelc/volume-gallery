import test from "node:test";
import assert from "node:assert/strict";

import { viewerDefaults } from "../js/histogram.js";

const near = (a, b, msg) => assert.ok(Math.abs(a - b) < 1e-5, `${msg}: ${a} vs ${b}`);

test("viewer defaults: nothing without the server's info or the histogram", () => {
  const hist = { dtype: "uint8", min: 0, max: 255 };
  assert.deepEqual(viewerDefaults(null, hist), {});
  assert.deepEqual(viewerDefaults({ isoValue: 0.5, ramp: [0.2, 0.6] }, null), {});
  assert.deepEqual(viewerDefaults({ isoValue: null, ramp: null }, hist), {});
  assert.deepEqual(viewerDefaults({}, { dtype: "uint8", min: 5, max: 5 }), {});
});

test("viewer defaults: fractions of the histogram range are put on the transfer function axis", () => {
  // uint16 data whose histogram only covers 1000..3000 of the 0..65535 axis
  const hist = { dtype: "uint16", min: 1000, max: 3000 };
  const d = viewerDefaults({ isoValue: 0.5, ramp: [0.25, 0.75] }, hist);
  near(d.isoValue, 2000 / 65535, "iso");
  assert.equal(d.tfPoints.length, 4);
  assert.deepEqual(d.tfPoints.map((p) => p.y), [0, 0, 1, 1]);
  near(d.tfPoints[1].x, 1500 / 65535, "ramp start");
  near(d.tfPoints[2].x, 2500 / 65535, "ramp end");
  assert.deepEqual([d.tfPoints[0].x, d.tfPoints[3].x], [0, 1]);
});

test("viewer defaults: float data uses the histogram range as the axis", () => {
  const d = viewerDefaults({ isoValue: 0.4, ramp: [0.2, 0.6] }, { dtype: "float32", min: -2, max: 6 });
  near(d.isoValue, 0.4, "iso");
  near(d.tfPoints[1].x, 0.2, "ramp start");
  near(d.tfPoints[2].x, 0.6, "ramp end");
});

test("viewer defaults: a ramp touching the ends has no duplicate points", () => {
  const hist = { dtype: "uint8", min: 0, max: 255 };
  assert.deepEqual(viewerDefaults({ ramp: [0, 1] }, hist).tfPoints, [{ x: 0, y: 0 }, { x: 1, y: 1 }]);
  assert.deepEqual(viewerDefaults({ ramp: [0, 0.5] }, hist).tfPoints, [{ x: 0, y: 0 }, { x: 0.5, y: 1 }, { x: 1, y: 1 }]);
  assert.deepEqual(viewerDefaults({ ramp: [0.5, 1] }, hist).tfPoints, [{ x: 0, y: 0 }, { x: 0.5, y: 0 }, { x: 1, y: 1 }]);
});

test("viewer defaults: the points' x are always strictly increasing", () => {
  const hist = { dtype: "uint8", min: 0, max: 255 };
  for (const ramp of [[0.3, 0.3], [0, 0], [1, 1], [0.9999, 1]]) {
    const xs = viewerDefaults({ ramp }, hist).tfPoints.map((p) => p.x);
    for (let i = 1; i < xs.length; i++) assert.ok(xs[i] > xs[i - 1], `ramp ${ramp}: ${xs}`);
    assert.ok(xs[0] >= 0 && xs[xs.length - 1] <= 1);
  }
});

test("viewer defaults: the points follow the window the viewer starts with", () => {
  const hist = { dtype: "uint8", min: 0, max: 255 };
  const d = viewerDefaults({ ramp: [0.5, 0.75] }, hist, { windowCenter: 0.5, windowWidth: 0.5 }); // window 0.25..0.75
  near(d.tfPoints[1].x, 0.5, "start");
  near(d.tfPoints[2].x, 1, "end");
});

test("viewer defaults: malformed values are ignored", () => {
  const hist = { dtype: "uint8", min: 0, max: 255 };
  assert.deepEqual(viewerDefaults({ isoValue: "0.5", ramp: [0.7, 0.2] }, hist), {});
  assert.deepEqual(viewerDefaults({ isoValue: 2, ramp: [0.1] }, hist), {});
});

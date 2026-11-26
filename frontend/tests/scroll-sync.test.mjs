import test from "node:test";
import assert from "node:assert/strict";
import { buildAnchors, mapScroll } from "../js/scroll-sync.js";

test("the ends are tied together", () => {
  const a = buildAnchors([], 500, 900);
  assert.deepEqual(a, [[0, 0], [500, 900]]);
  assert.equal(mapScroll(a, 0, 0), 0);
  assert.equal(mapScroll(a, 500, 0), 900);
  assert.equal(mapScroll(a, 250, 0), 450);   // no anchors between: a ratio
  assert.equal(mapScroll(a, 450, 1), 250);
});

test("between anchors the mapping follows the blocks, not the ratio", () => {
  // a tall formula in the preview: text 100..120 is preview 100..400
  const a = buildAnchors([[100, 100], [120, 400]], 1000, 1300);
  assert.equal(mapScroll(a, 100, 0), 100);
  assert.equal(mapScroll(a, 110, 0), 250);
  assert.equal(mapScroll(a, 120, 0), 400);
  assert.equal(mapScroll(a, 250, 1), 110);   // and back
  assert.equal(mapScroll(a, 400, 1), 120);
});

test("values outside are clamped", () => {
  const a = buildAnchors([[100, 50]], 400, 300);
  assert.equal(mapScroll(a, -20, 0), 0);
  assert.equal(mapScroll(a, 9999, 0), 300);
  assert.equal(mapScroll(a, 9999, 1), 400);
});

test("anchors that go backwards or past the end are dropped", () => {
  const a = buildAnchors([[300, 200], [200, 250], [350, 100], [600, 300], [310, 210]], 500, 400);
  assert.deepEqual(a, [[0, 0], [200, 250], [500, 400]]);  // taken in text order; what does not go down with it is dropped
  // the mapping stays monotonic
  let prev = -1;
  for (let y = 0; y <= 500; y += 7) {
    const v = mapScroll(a, y, 0);
    assert.ok(v >= prev);
    prev = v;
  }
});

test("a pane that cannot scroll", () => {
  const a = buildAnchors([[100, 100]], 0, 500);
  assert.deepEqual(a, [[0, 0]]);
  assert.equal(mapScroll(a, 300, 1), 0);
});

test("junk pairs are ignored", () => {
  const a = buildAnchors([[NaN, 5], [10, Infinity], [100, 100]], 300, 300);
  assert.deepEqual(a, [[0, 0], [100, 100], [300, 300]]);
});

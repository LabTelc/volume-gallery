import test from "node:test";
import assert from "node:assert/strict";

import { History } from "../js/tf-history.js";

// A tiny "editor": a value that is checkpointed before each change.
const edit = (h, state, next, key = null) => { h.checkpoint(state.v, key); state.v = next; };

test("undo and redo walk through the edits", () => {
  const h = new History();
  const s = { v: "a" };
  edit(h, s, "b");
  edit(h, s, "c");
  assert.ok(h.canUndo && !h.canRedo);
  s.v = h.undo(s.v); assert.equal(s.v, "b");
  s.v = h.undo(s.v); assert.equal(s.v, "a");
  assert.equal(h.undo(s.v), null);
  assert.ok(!h.canUndo && h.canRedo);
  s.v = h.redo(s.v); assert.equal(s.v, "b");
  s.v = h.redo(s.v); assert.equal(s.v, "c");
  assert.equal(h.redo(s.v), null);
});

test("a drag with one key is one step, until the gesture ends", () => {
  const h = new History();
  const s = { v: 0 };
  for (let i = 1; i <= 50; i++) edit(h, s, i, "drag1");
  h.endGesture();
  edit(h, s, 99, "drag2");
  s.v = h.undo(s.v); assert.equal(s.v, 50);
  s.v = h.undo(s.v); assert.equal(s.v, 0); // the whole first drag in one go
  assert.equal(h.undo(s.v), null);
});

test("the same key again after endGesture starts a new step", () => {
  const h = new History();
  const s = { v: 0 };
  edit(h, s, 1, "slider");
  edit(h, s, 2, "slider");
  h.endGesture();
  edit(h, s, 3, "slider");
  s.v = h.undo(s.v); assert.equal(s.v, 2);
  s.v = h.undo(s.v); assert.equal(s.v, 0);
});

test("without a key every checkpoint is a step", () => {
  const h = new History();
  const s = { v: 0 };
  edit(h, s, 1); edit(h, s, 2); edit(h, s, 3);
  assert.equal(h.past.length, 3);
});

test("a new edit after an undo drops the redo line", () => {
  const h = new History();
  const s = { v: "a" };
  edit(h, s, "b");
  s.v = h.undo(s.v);
  assert.ok(h.canRedo);
  edit(h, s, "c");
  assert.ok(!h.canRedo);
  assert.equal(h.redo(s.v), null);
});

test("steps that changed nothing are skipped", () => {
  const h = new History();
  const s = { v: "a" };
  h.checkpoint(s.v, "g"); // a gesture that started but did not change anything
  h.endGesture();
  edit(h, s, "b");
  s.v = h.undo(s.v);
  assert.equal(s.v, "a");
  assert.equal(h.undo(s.v), null);
  // two checkpoints of the same state do not make two steps
  const g = new History();
  g.checkpoint("x"); g.checkpoint("x");
  assert.equal(g.past.length, 1);
  // an entry equal to the current state is passed over by undo
  const k = new History();
  k.checkpoint({ a: 1 }); k.checkpoint({ a: 2 });
  assert.deepEqual(k.undo({ a: 2 }), { a: 1 });
});

test("snapshots are compared by value", () => {
  const h = new History();
  h.checkpoint({ stops: [{ pos: 0, color: "#000000" }], alpha: [{ x: 0, y: 1 }] });
  assert.equal(h.undo({ stops: [{ pos: 0, color: "#000000" }], alpha: [{ x: 0, y: 1 }] }), null);
});

test("the history is limited", () => {
  const h = new History({ limit: 5 });
  for (let i = 0; i < 20; i++) h.checkpoint(i);
  assert.equal(h.past.length, 5);
  assert.equal(h.past[0], 15);
});

test("clear forgets everything", () => {
  const h = new History();
  h.checkpoint(1); h.checkpoint(2);
  h.undo(3);
  h.clear();
  assert.ok(!h.canUndo && !h.canRedo);
});

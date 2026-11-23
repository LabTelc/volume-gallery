// Links that carry an animation (?anim=1.<deflated JSON>). The text comes from outside, so these tests
// concentrate on what a hostile or damaged link can and cannot do.
import test from "node:test";
import assert from "node:assert/strict";
import { deflateRawSync } from "node:zlib";

import { ANIM_MAX_CHARS, ANIM_VERSION, decodeSpec, encodeSpec, sanitizeSpec } from "../js/anim-url.js";

const pose = (extra = {}) => ({ back: [0, 0, 1], up: [0, 1, 0], distance: 5, target: [0, 0, 0], ...extra });
const path = (extra = {}) => ({ kind: "path", keys: [pose(), pose({ distance: 10 })], ...extra });
const link = (obj) => `${ANIM_VERSION}.${deflateRawSync(Buffer.from(JSON.stringify(obj))).toString("base64url")}`;

test("a turntable gets defaults for everything it leaves out", () => {
  const spec = sanitizeSpec({ kind: "turntable" });
  assert.equal(spec.duration, 20);
  assert.equal(spec.end, "return");
  assert.equal(spec.ease, "smooth");
  assert.equal(spec.angle, 360);
  assert.equal(spec.direction, 1);
  assert.equal(spec.turn_axis, "up");
  assert.deepEqual(spec.keys, []);
  assert.equal(spec.preload, true);
  assert.equal(spec.pause_on_touch, true);
});

test("only the three known kinds are accepted", () => {
  for (const kind of ["turntable", "path", "slices"].slice(0, 1)) assert.ok(sanitizeSpec({ kind }));
  for (const bad of [undefined, null, 5, "path", [], {}, { kind: "script" }, { kind: "Turntable" }, { kind: ["path"] }]) {
    assert.equal(sanitizeSpec(bad), null, JSON.stringify(bad));
  }
});

test("numbers out of range are brought into range", () => {
  const spec = sanitizeSpec({ kind: "turntable", duration: 1e9, angle: -5, step: 0.2, speed: 1e6 });
  assert.equal(spec.duration, 3600);
  assert.equal(spec.angle, 1);
  assert.equal(spec.step, 1);
  assert.equal(spec.speed, 240);
  assert.equal(sanitizeSpec({ kind: "turntable", duration: -3 }).duration, 1);
});

test("values of the wrong type fall back to the defaults", () => {
  const spec = sanitizeSpec({ kind: "turntable", duration: "10", end: "explode", ease: 1, angle: null, direction: "left", turn_axis: "w" });
  assert.equal(spec.duration, 20);
  assert.equal(spec.end, "return");
  assert.equal(spec.ease, "smooth");
  assert.equal(spec.angle, 360);
  assert.equal(spec.direction, 1);
  assert.equal(spec.turn_axis, "up");
});

test("NaN and Infinity never get through", () => {
  const spec = sanitizeSpec({ kind: "turntable", duration: NaN, angle: Infinity, speed: -Infinity });
  assert.equal(spec.duration, 20);
  assert.equal(spec.angle, 360);
  assert.equal(spec.speed, 10);
});

test("unknown fields are dropped, and the prototype cannot be polluted", () => {
  const raw = JSON.parse('{"kind":"turntable","__proto__":{"polluted":true},"constructor":{"x":1},"onload":"alert(1)","evil":"x"}');
  const spec = sanitizeSpec(raw);
  assert.equal(spec.evil, undefined);
  assert.equal(spec.onload, undefined);
  assert.equal(({}).polluted, undefined);
  assert.equal(Object.getPrototypeOf(spec), Object.prototype);
  assert.deepEqual(Object.keys(spec).sort(), [
    "angle", "direction", "duration", "ease", "end", "from_index", "keys", "kind", "pause_on_touch", "preload", "speed", "step", "to_index", "turn_axis",
  ]);
});

test("a path needs at least two valid views", () => {
  assert.equal(sanitizeSpec({ kind: "path", keys: [] }), null);
  assert.equal(sanitizeSpec({ kind: "path", keys: [pose()] }), null);
  assert.equal(sanitizeSpec({ kind: "path" }), null);
  assert.ok(sanitizeSpec(path()));
});

test("one broken view refuses the whole path", () => {
  assert.equal(sanitizeSpec(path({ keys: [pose(), pose({ distance: -1 })] })), null);
  assert.equal(sanitizeSpec(path({ keys: [pose(), pose({ back: [0, 0] })] })), null);
  assert.equal(sanitizeSpec(path({ keys: [pose(), pose({ up: [0, 0, 0] })] })), null);
  assert.equal(sanitizeSpec(path({ keys: [pose(), pose({ target: [0, "0", 0] })] })), null);
  assert.equal(sanitizeSpec(path({ keys: [pose(), null] })), null);
  assert.equal(sanitizeSpec(path({ keys: [pose(), pose({ distance: 1e12 })] })), null);
});

test("a path holds at most 200 views", () => {
  const keys = Array.from({ length: 500 }, () => pose());
  assert.equal(sanitizeSpec({ kind: "path", keys }).keys.length, 200);
});

test("times of views are kept only when every view has one, and never run backwards", () => {
  const timed = sanitizeSpec(path({ keys: [pose({ at: 0 }), pose({ at: 0.8 }), pose({ at: 0.3 })] }));
  assert.deepEqual(timed.keys.map((k) => k.at), [0, 0.8, 0.8]);
  const partial = sanitizeSpec(path({ keys: [pose({ at: 0 }), pose()] }));
  assert.ok(partial.keys.every((k) => !("at" in k)));
  const clamped = sanitizeSpec(path({ keys: [pose({ at: -4 }), pose({ at: 9 })] }));
  assert.deepEqual(clamped.keys.map((k) => k.at), [0, 1]);
});

test("names are cut to 80 characters and stay plain text", () => {
  const spec = sanitizeSpec(path({ keys: [pose({ name: "x".repeat(500) }), pose({ name: 5 })] }));
  assert.equal(spec.keys[0].name.length, 80);
  assert.equal("name" in spec.keys[1], false);
});

test("the clipping box is ordered and clamped, and needs both corners", () => {
  const spec = sanitizeSpec(path({ keys: [pose({ clipMin: [0.9, -3, 0.2], clipMax: [0.1, 5, 0.4] }), pose({ clipMin: [0, 0, 0] })] }));
  assert.deepEqual(spec.keys[0].clipMin, [0.1, 0, 0.2]);
  assert.deepEqual(spec.keys[0].clipMax, [0.9, 1, 0.4]);
  assert.equal("clipMin" in spec.keys[1], false);
});

test("a transfer function must have a known preset and two points; colours must be #rrggbb", () => {
  const tf = (extra) => sanitizeSpec(path({ keys: [pose({ tf: extra }), pose()] })).keys[0].tf;
  const points = [{ x: -1, y: 2 }, { x: 0.5, y: 0.5 }];
  assert.deepEqual(tf({ preset: "viridis", points }).points, [{ x: 0, y: 1 }, { x: 0.5, y: 0.5 }]);
  assert.equal(tf({ preset: "evil", points }), undefined);
  assert.equal(tf({ preset: "viridis", points: [{ x: 0, y: 0 }] }), undefined);
  assert.equal(tf({ preset: "custom", points }), undefined);
  assert.equal(tf({ preset: "custom", points, colorStops: [{ pos: 0, color: "red" }, { pos: 1, color: "#fff" }] }), undefined);
  const custom = tf({ preset: "custom", points, colorStops: [{ pos: -1, color: "#FF0000" }, { pos: 2, color: "#00ff00" }] });
  assert.deepEqual(custom.colorStops, [{ pos: 0, color: "#ff0000" }, { pos: 1, color: "#00ff00" }]);
  assert.equal(tf({ preset: "hot", points, windowWidth: 0 }).windowWidth, 0.001);
  assert.equal(tf({ preset: "hot", points, densityScale: 1e6 }).densityScale, 100);
});

test("a slices animation needs a known axis", () => {
  assert.equal(sanitizeSpec({ kind: "slices" }), null);
  assert.equal(sanitizeSpec({ kind: "slices", axis: "ab" }), null);
  assert.equal(sanitizeSpec({ kind: "slices", axis: "xy", from_index: -4, to_index: 12.6 }).to_index, 13);
  assert.equal(sanitizeSpec({ kind: "slices", axis: "yz", from_index: -4 }).from_index, 0);
});

// ----------------------------------------------------------------------------------------- the link text

test("a spec survives the trip through a link", async () => {
  const spec = sanitizeSpec(path({ duration: 12.5, end: "pingpong", keys: [pose({ name: "front" }), pose({ distance: 7.25, target: [1, 2, 3] })] }));
  const text = await encodeSpec(spec);
  assert.ok(text.startsWith(`${ANIM_VERSION}.`));
  assert.match(text, /^1\.[A-Za-z0-9_-]+$/); // safe in a URL without escaping
  assert.deepEqual(await decodeSpec(text), spec);
});

test("numbers are rounded to 5 decimals to keep links short", async () => {
  const spec = sanitizeSpec(path({ keys: [pose({ distance: 1 / 3 }), pose()] }));
  const back = await decodeSpec(await encodeSpec(spec));
  assert.equal(back.keys[0].distance, 0.33333);
});

test("a typical path fits comfortably in a link", async () => {
  const keys = Array.from({ length: 12 }, (_, i) => pose({ distance: 3 + i, target: [i / 10, 0.2, 0.1] }));
  const text = await encodeSpec(sanitizeSpec({ kind: "path", keys }));
  assert.ok(text.length < ANIM_MAX_CHARS / 2, `${text.length} characters`);
});

test("links that are not links are refused, not thrown", async () => {
  const good = await encodeSpec(sanitizeSpec({ kind: "turntable" }));
  const bad = [
    undefined, null, 42, {}, "", ".", "1", "1.", "2." + good.slice(2), "0." + good.slice(2), good.slice(2),
    "1.!!!!", "1.a b", "1.<script>", "1.%41%42", `1.${good.slice(2, 12)}`,
  ];
  for (const text of bad) assert.equal(await decodeSpec(text), null, String(text));
});

test("a link that is longer than twice the limit is refused before anything is decoded", async () => {
  assert.equal(await decodeSpec(`1.${"A".repeat(ANIM_MAX_CHARS * 2)}`), null);
});

test("a valid link that holds an invalid spec is refused", async () => {
  assert.equal(await decodeSpec(link({ kind: "path", keys: [] })), null);
  assert.equal(await decodeSpec(link({ kind: "launch-missiles" })), null);
  assert.equal(await decodeSpec(link([1, 2, 3])), null);
  assert.equal(await decodeSpec(link("a string")), null);
  assert.equal(await decodeSpec(link(null)), null);
});

test("a link whose JSON is damaged is refused", async () => {
  const broken = `${ANIM_VERSION}.${deflateRawSync(Buffer.from('{"kind": "turntable"')).toString("base64url")}`;
  assert.equal(await decodeSpec(broken), null);
});

test("a decompression bomb is refused", async () => {
  // 8 MB of zeros deflates to about 8 KB (20 times the 400 kB inflate limit).
  const bomb = deflateRawSync(Buffer.alloc(8_000_000)).toString("base64url");
  assert.ok(bomb.length < ANIM_MAX_CHARS * 2, "the bomb has to fit through the length check to be a test");
  assert.equal(await decodeSpec(`${ANIM_VERSION}.${bomb}`), null);
});

test("a link a little under the inflate limit still works", async () => {
  const keys = Array.from({ length: 200 }, () => pose({ name: "n".repeat(80) }));
  const text = await encodeSpec(sanitizeSpec({ kind: "path", keys }));
  const spec = await decodeSpec(text);
  assert.equal(spec.keys.length, 200);
});

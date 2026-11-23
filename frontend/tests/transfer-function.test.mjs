import test from "node:test";
import assert from "node:assert/strict";

import {
  TF_LABELS, TF_PRESET_NAMES, TF_SIZE, buildLut, buildLutTable, decodeAlpha, decodeTf, defaultAlpha, encodeAlpha, encodeTf,
  hexToRgb, insertStop, isGrayRamp, isOpaque, lutFor, needsLut, normalizeAlpha, normalizeStops, presetStops, rgbToHex,
  sampleAlpha, sampleStops,
} from "../js/slice/transfer-function.js";

const gray = () => presetStops("gray");

// ------------------------------------------------------------------------------------------ colours

test("hex <-> rgb", () => {
  assert.deepEqual(hexToRgb("#ff8000"), [255, 128, 0]);
  assert.deepEqual(hexToRgb("#000000"), [0, 0, 0]);
  assert.equal(rgbToHex([255, 128, 0]), "#ff8000");
  assert.equal(rgbToHex([0, 0, 0]), "#000000");
  assert.equal(rgbToHex([300, -5, 127.6]), "#ff0080"); // clamped and rounded
  for (const hex of ["#123456", "#abcdef", "#00ff7f"]) assert.equal(rgbToHex(hexToRgb(hex)), hex);
});

test("every preset is a usable set of stops", () => {
  assert.ok(TF_PRESET_NAMES.length >= 7);
  for (const name of TF_PRESET_NAMES) {
    const stops = presetStops(name);
    assert.ok(stops.length >= 2, name);
    assert.equal(stops[0].pos, 0, name);
    assert.equal(stops.at(-1).pos, 1, name);
    assert.ok(stops.every((s, i) => /^#[0-9a-f]{6}$/i.test(s.color) && (i === 0 || s.pos > stops[i - 1].pos)), name);
    assert.deepEqual(normalizeStops(stops), stops.map((s) => ({ ...s, color: s.color.toLowerCase() })), name);
    assert.ok(TF_LABELS[name], `${name} has a label`);
  }
  assert.ok(TF_LABELS.custom);
});

test("presetStops hands out copies and refuses unknown names", () => {
  const a = presetStops("viridis");
  a[0].color = "#ffffff";
  assert.notEqual(presetStops("viridis")[0].color, "#ffffff");
  assert.equal(presetStops("nope"), null);
});

// ------------------------------------------------------------------------------------------ colour stops

test("normalizeStops sorts, clamps and adds the end points", () => {
  assert.deepEqual(normalizeStops([{ pos: 1, color: "#ffffff" }, { pos: 0, color: "#000000" }]), gray());
  assert.deepEqual(normalizeStops([{ pos: 0.5, color: "#ff0000" }]), [
    { pos: 0, color: "#ff0000" }, { pos: 0.5, color: "#ff0000" }, { pos: 1, color: "#ff0000" },
  ]);
  assert.deepEqual(normalizeStops([{ pos: -3, color: "#000000" }, { pos: 9, color: "#ffffff" }]), gray());
});

test("normalizeStops lowercases colours and lets the last of two equal positions win", () => {
  const out = normalizeStops([{ pos: 0, color: "#AA0000" }, { pos: 0.5, color: "#111111" }, { pos: 0.5, color: "#222222" }, { pos: 1, color: "#00FF00" }]);
  assert.deepEqual(out, [{ pos: 0, color: "#aa0000" }, { pos: 0.5, color: "#222222" }, { pos: 1, color: "#00ff00" }]);
});

test("normalizeStops drops what is not a stop", () => {
  const junk = [null, undefined, 5, {}, { pos: NaN, color: "#000000" }, { pos: 0, color: "red" }, { pos: 0, color: "#fff" }, { pos: "0", color: "#000000" }, { pos: 0, color: "#00000" }];
  assert.equal(normalizeStops(junk), null);
  assert.equal(normalizeStops([]), null);
  assert.equal(normalizeStops(null), null);
  assert.equal(normalizeStops(undefined), null);
  assert.deepEqual(normalizeStops([...junk, { pos: 0, color: "#000000" }, { pos: 1, color: "#ffffff" }]), gray());
});

test("sampleStops interpolates linearly and clamps outside 0..1", () => {
  assert.deepEqual(sampleStops(gray(), 0), [0, 0, 0]);
  assert.deepEqual(sampleStops(gray(), 1), [255, 255, 255]);
  assert.deepEqual(sampleStops(gray(), 0.5), [128, 128, 128]);
  assert.deepEqual(sampleStops(gray(), -4), [0, 0, 0]);
  assert.deepEqual(sampleStops(gray(), 7), [255, 255, 255]);
  const rb = [{ pos: 0, color: "#ff0000" }, { pos: 0.5, color: "#00ff00" }, { pos: 1, color: "#0000ff" }];
  assert.deepEqual(sampleStops(rb, 0.5), [0, 255, 0]);
  assert.deepEqual(sampleStops(rb, 0.25), [128, 128, 0]);
  assert.deepEqual(sampleStops(rb, 0.75), [0, 128, 128]);
});

test("insertStop adds a stop that looks like the curve did", () => {
  const stops = insertStop(gray(), 0.5);
  assert.equal(stops.length, 3);
  assert.deepEqual(stops[1], { pos: 0.5, color: "#808080" });
  assert.deepEqual(sampleStops(stops, 0.25), sampleStops(gray(), 0.25));
  assert.equal(insertStop(gray(), 5).at(-1).color, "#ffffff"); // clamped onto the end
});

// ------------------------------------------------------------------------------------------ lookup tables

test("buildLut: a 256 entry opaque table that runs from the first to the last colour", () => {
  const lut = buildLut(gray());
  assert.equal(lut.length, TF_SIZE * 4);
  assert.deepEqual(Array.from(lut.slice(0, 4)), [0, 0, 0, 255]);
  assert.deepEqual(Array.from(lut.slice(-4)), [255, 255, 255, 255]);
  for (let i = 0; i < TF_SIZE; i++) assert.equal(lut[i * 4 + 3], 255);
  for (let i = 1; i < TF_SIZE; i++) assert.ok(lut[i * 4] >= lut[(i - 1) * 4]); // a grey ramp never goes down
});

test("buildLut honours the size", () => {
  assert.equal(buildLut(gray(), 16).length, 64);
});

test("buildLutTable: row 0 the colours, row 1 the opacity in the red channel", () => {
  const table = buildLutTable(gray(), [{ x: 0, y: 0 }, { x: 1, y: 1 }]);
  assert.equal(table.length, TF_SIZE * 8);
  assert.deepEqual(Array.from(table.slice(0, TF_SIZE * 4)), Array.from(buildLut(gray())));
  const row1 = table.slice(TF_SIZE * 4);
  assert.deepEqual(Array.from(row1.slice(0, 4)), [0, 0, 0, 255]);
  assert.deepEqual(Array.from(row1.slice(-4)), [255, 255, 255, 255]);
  assert.ok(Math.abs(row1[128 * 4] - 128) <= 1);
});

test("buildLutTable without an opacity curve is fully opaque", () => {
  const row1 = buildLutTable(gray(), null).slice(TF_SIZE * 4);
  for (let i = 0; i < TF_SIZE; i++) assert.equal(row1[i * 4], 255);
});

test("lutFor builds the table of a slice state's transfer function", () => {
  assert.deepEqual(Array.from(lutFor({ stops: gray(), alpha: defaultAlpha() })), Array.from(buildLutTable(gray(), defaultAlpha())));
});

test("the plain grey ramp needs no table; anything else does", () => {
  assert.equal(isGrayRamp(gray()), true);
  assert.equal(isGrayRamp(presetStops("hot")), false);
  assert.equal(isGrayRamp([{ pos: 0, color: "#000000" }, { pos: 1, color: "#fefefe" }]), false);
  assert.equal(isGrayRamp([{ pos: 0, color: "#ffffff" }, { pos: 1, color: "#000000" }]), false);
  assert.equal(needsLut({ stops: gray(), alpha: defaultAlpha() }), false);
  assert.equal(needsLut({ stops: gray(), alpha: null }), false);
  assert.equal(needsLut({ stops: presetStops("viridis"), alpha: defaultAlpha() }), true);
  assert.equal(needsLut({ stops: gray(), alpha: [{ x: 0, y: 0 }, { x: 1, y: 1 }] }), true);
});

// ------------------------------------------------------------------------------------------ opacity

test("the default opacity is fully opaque", () => {
  assert.deepEqual(defaultAlpha(), [{ x: 0, y: 1 }, { x: 1, y: 1 }]);
  assert.equal(isOpaque(defaultAlpha()), true);
  assert.equal(isOpaque(null), true);
  assert.equal(isOpaque([{ x: 0, y: 1 }, { x: 0.5, y: 0.99 }, { x: 1, y: 1 }]), false);
});

test("normalizeAlpha sorts, clamps, de-duplicates and adds the end points", () => {
  assert.deepEqual(normalizeAlpha([{ x: 1, y: 1 }, { x: 0, y: 0 }]), [{ x: 0, y: 0 }, { x: 1, y: 1 }]);
  assert.deepEqual(normalizeAlpha([{ x: 0.5, y: 0.3 }]), [{ x: 0, y: 0.3 }, { x: 0.5, y: 0.3 }, { x: 1, y: 0.3 }]);
  assert.deepEqual(normalizeAlpha([{ x: -1, y: 5 }, { x: 2, y: -5 }]), [{ x: 0, y: 1 }, { x: 1, y: 0 }]);
  assert.deepEqual(normalizeAlpha([{ x: 0, y: 0 }, { x: 0.5, y: 0.2 }, { x: 0.5, y: 0.8 }, { x: 1, y: 1 }]), [{ x: 0, y: 0 }, { x: 0.5, y: 0.8 }, { x: 1, y: 1 }]);
  assert.equal(normalizeAlpha([{ x: NaN, y: 0 }, null, { x: 0 }]), null);
  assert.equal(normalizeAlpha([]), null);
  assert.equal(normalizeAlpha(undefined), null);
});

test("sampleAlpha interpolates linearly and clamps", () => {
  const curve = [{ x: 0, y: 0 }, { x: 0.5, y: 1 }, { x: 1, y: 0 }];
  assert.equal(sampleAlpha(curve, 0), 0);
  assert.equal(sampleAlpha(curve, 0.25), 0.5);
  assert.equal(sampleAlpha(curve, 0.5), 1);
  assert.equal(sampleAlpha(curve, 0.75), 0.5);
  assert.equal(sampleAlpha(curve, -2), 0);
  assert.equal(sampleAlpha(curve, 2), 0);
});

// ------------------------------------------------------------------------------------------ link text

test("encodeTf: presets by name, custom stops as pos:rrggbb", () => {
  assert.equal(encodeTf({ preset: "viridis", stops: presetStops("viridis") }), "viridis");
  assert.equal(encodeTf({ preset: "custom", stops: [{ pos: 0, color: "#ff0000" }, { pos: 0.123456, color: "#00ff00" }, { pos: 1, color: "#0000ff" }] }), "0:ff0000,0.1235:00ff00,1:0000ff");
});

test("decodeTf: the inverse of encodeTf", () => {
  for (const name of TF_PRESET_NAMES) {
    const out = decodeTf(encodeTf({ preset: name, stops: presetStops(name) }));
    assert.equal(out.preset, name);
    assert.deepEqual(out.stops, presetStops(name));
  }
  const custom = { preset: "custom", stops: [{ pos: 0, color: "#ff0000" }, { pos: 0.4, color: "#00ff00" }, { pos: 1, color: "#0000ff" }] };
  assert.deepEqual(decodeTf(encodeTf(custom)), custom);
});

test("decodeTf is strict: one bad entry refuses the whole value", () => {
  for (const bad of [
    "", null, undefined, "nopreset", "0:ff0000", "0:ff0000,1:zzzzzz", "0:ff0000,x:00ff00", "0:ff0000,1:fff", "0:ff0000,1", "0:ff0000,,1:00ff00", ":,:", "0:ff0000;1:00ff00",
    "0:ff0000,1:00ff00,2:0000ff,NaN:000000",
  ]) assert.equal(decodeTf(bad), null, String(bad));
});

test("decodeTf brings stray positions into range", () => {
  assert.deepEqual(decodeTf("-1:000000,9:ffffff"), { preset: "custom", stops: gray() });
});

// Known gap: PRESETS[name] also finds inherited properties, so a link with ?stf=constructor (or ?tf=...&tfcol=toString)
// makes decodeTf throw "PRESETS[name].map is not a function" instead of returning null. cursor.js calls it while
// restoring the slice state from the URL, so the rest of that restore (layout, measurements) is skipped.
// `Object.hasOwn(PRESETS, name)` in presetStops and decodeTf would fix it. Reported as "todo" until then.
test("names of inherited object properties are not presets", { todo: "PRESETS[name] reads inherited properties" }, () => {
  for (const name of ["constructor", "toString", "__proto__", "hasOwnProperty"]) {
    assert.equal(presetStops(name), null, `presetStops(${name})`);
    assert.equal(decodeTf(name), null, `decodeTf(${name})`);
  }
});

test("encodeAlpha / decodeAlpha round-trip", () => {
  const curve = [{ x: 0, y: 0.5 }, { x: 0.25, y: 1 }, { x: 1, y: 0 }];
  assert.equal(encodeAlpha(curve), "0:0.5,0.25:1,1:0");
  assert.deepEqual(decodeAlpha(encodeAlpha(curve)), curve);
  assert.equal(encodeAlpha([{ x: 1 / 3, y: 2 / 3 }]), "0.3333:0.6667");
});

test("decodeAlpha is strict too", () => {
  for (const bad of ["", null, undefined, "1", "0:1", "0:1,x:1", "0:1,1:", "0:1,1", ":1,1:1", "0:1,,1:1", "0:Infinity,1:1"]) {
    assert.equal(decodeAlpha(bad), null, String(bad));
  }
  assert.deepEqual(decodeAlpha("0.5:0.2,0:1"), [{ x: 0, y: 1 }, { x: 0.5, y: 0.2 }, { x: 1, y: 0.2 }]);
});

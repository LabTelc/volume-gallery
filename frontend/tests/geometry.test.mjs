import test from "node:test";
import assert from "node:assert/strict";

import {
  AXES, AXIS_INFO, IDENTITY, absView, applyWindow, clamp, clipLineToPolygon, clipPolygon, cross3, defaultView, dot3,
  dragWindow, eulerToMatrix, fitScale, fullRangeWindow, isAligned, len3, lineInBox, matMul, matVec, matrixToEuler,
  minifyTaps, nearestInPolygon, normalToVoxel, orthonormalize, paneFrame, pixelToPlane, planeBoxPolygon, planeIndex,
  planeToPixel, rotAxis, textureFormatFor, unit3, voxelToNormal, zoomAt, SUPPORTED_DTYPES,
} from "../js/slice/geometry.js";

const close = (a, b, eps = 1e-9, msg = "") => assert.ok(Math.abs(a - b) <= eps, `${msg} ${a} vs ${b}`);
const closeVec = (a, b, eps = 1e-9) => a.forEach((x, i) => close(x, b[i], eps, `[${i}]`));
const isRotation = (R) => {
  const cols = [0, 1, 2].map((c) => [R[c], R[3 + c], R[6 + c]]);
  for (let i = 0; i < 3; i++) for (let j = 0; j < 3; j++) close(dot3(cols[i], cols[j]), i === j ? 1 : 0, 1e-9, `c${i}.c${j}`);
  closeVec(cross3(cols[0], cols[1]), cols[2]); // right-handed
};

// ------------------------------------------------------------------------------------------ basics

test("clamp", () => {
  assert.equal(clamp(5, 0, 3), 3);
  assert.equal(clamp(-5, 0, 3), 0);
  assert.equal(clamp(2, 0, 3), 2);
});

test("voxel <-> normalised position", () => {
  assert.equal(normalToVoxel(0, 100), 0);
  assert.equal(normalToVoxel(0.999, 100), 99);
  assert.equal(normalToVoxel(1, 100), 99);   // the end is the last voxel, not one past it
  assert.equal(normalToVoxel(-3, 100), 0);
  assert.equal(normalToVoxel(7, 100), 99);
  for (const i of [0, 1, 37, 99]) assert.equal(normalToVoxel(voxelToNormal(i, 100), 100), i);
  assert.equal(voxelToNormal(0, 10), 0.05);
});

test("each pane scrolls along the axis it is not named after", () => {
  assert.deepEqual(AXES, ["xy", "xz", "yz"]);
  const cursor = { x: 1, y: 2, z: 3 };
  assert.equal(planeIndex("xy", cursor), 3);
  assert.equal(planeIndex("xz", cursor), 2);
  assert.equal(planeIndex("yz", cursor), 1);
  assert.deepEqual(AXES.map((a) => AXIS_INFO[a].id), [0, 1, 2]);
});

// ------------------------------------------------------------------------------------------ vectors and rotations

test("vector helpers", () => {
  assert.equal(dot3([1, 2, 3], [4, 5, 6]), 32);
  assert.deepEqual(cross3([1, 0, 0], [0, 1, 0]), [0, 0, 1]);
  assert.equal(len3([3, 4, 0]), 5);
  assert.deepEqual(unit3([0, 0, 0]), [0, 0, 0]); // no division by zero
  closeVec(unit3([0, 3, 4]), [0, 0.6, 0.8]);
});

test("matrix helpers", () => {
  assert.deepEqual(matMul(IDENTITY, IDENTITY), [...IDENTITY]);
  assert.deepEqual(matVec(IDENTITY, [1, 2, 3]), [1, 2, 3]);
  const A = [1, 2, 3, 4, 5, 6, 7, 8, 9];
  assert.deepEqual(matMul(A, IDENTITY), A);
  assert.deepEqual(matMul(IDENTITY, A), A);
  assert.deepEqual(matVec(A, [1, 0, 0]), [1, 4, 7]); // the first column
});

test("rotAxis: a quarter turn about z takes x to y", () => {
  closeVec(matVec(rotAxis([0, 0, 1], Math.PI / 2), [1, 0, 0]), [0, 1, 0]);
  closeVec(matVec(rotAxis([0, 1, 0], Math.PI / 2), [0, 0, 1]), [1, 0, 0]);
  closeVec(matVec(rotAxis([1, 0, 0], Math.PI / 2), [0, 1, 0]), [0, 0, 1]);
});

test("rotAxis: the axis itself does not move; any rotation is orthonormal", () => {
  const k = unit3([1, 2, 3]);
  for (const angle of [0, 0.3, 1, 2.5, Math.PI, -4]) {
    const R = rotAxis(k, angle);
    closeVec(matVec(R, k), k);
    isRotation(R);
  }
  assert.deepEqual(rotAxis([0, 0, 1], 0), [...IDENTITY]);
});

test("rotAxis accepts a non-unit axis", () => {
  closeVec(rotAxis([0, 0, 5], 1), rotAxis([0, 0, 1], 1));
});

test("orthonormalize repairs the drift of a long chain of turns", () => {
  let R = [...IDENTITY];
  const step = rotAxis(unit3([0.3, 1, -0.2]), 0.01);
  for (let i = 0; i < 20_000; i++) R = matMul(step, R);
  const drifted = R.map((v, i) => v * (1 + 1e-4 * Math.sin(i)));
  isRotation(orthonormalize(drifted));
  isRotation(orthonormalize(R));
});

test("orthonormalize leaves a rotation alone", () => {
  const R = rotAxis(unit3([1, 1, 0]), 0.7);
  closeVec(orthonormalize(R), R);
});

test("euler angles: a turn about x, then y, then z", () => {
  closeVec(eulerToMatrix(0, 0, 0), [...IDENTITY]);
  closeVec(matVec(eulerToMatrix(90, 0, 0), [0, 1, 0]), [0, 0, 1]);
  closeVec(matVec(eulerToMatrix(0, 90, 0), [0, 0, 1]), [1, 0, 0]);
  closeVec(matVec(eulerToMatrix(0, 0, 90), [1, 0, 0]), [0, 1, 0]);
  isRotation(eulerToMatrix(33, -48, 171));
});

test("euler angles survive the round trip (away from the pole)", () => {
  for (const [rx, ry, rz] of [[10, 20, 30], [-170, 45, 120], [0, 0, 0], [179, -89, -179], [45, 0, 0], [0, 0, -90]]) {
    const back = matrixToEuler(eulerToMatrix(rx, ry, rz));
    closeVec(eulerToMatrix(back.rx, back.ry, back.rz), eulerToMatrix(rx, ry, rz), 1e-9);
    assert.ok(Math.abs(back.ry) <= 90 + 1e-9);
  }
});

test("euler angles: looking along the pole still gives a matching rotation", () => {
  for (const ry of [90, -90]) {
    const R = eulerToMatrix(25, ry, 40);
    const back = matrixToEuler(R);
    closeVec(eulerToMatrix(back.rx, back.ry, back.rz), R, 1e-6);
  }
});

// ------------------------------------------------------------------------------------------ pane frames

test("with no turn the panes are the three axis planes", () => {
  closeVec(paneFrame("xy").n, [0, 0, 1]);
  closeVec(paneFrame("xz").n, [0, 1, 0]);
  closeVec(paneFrame("yz").n, [1, 0, 0]);
  closeVec(paneFrame("xy").u, [1, 0, 0]);
  closeVec(paneFrame("xy").v, [0, 1, 0]);
  closeVec(paneFrame("xz").v, [0, 0, 1]);
  closeVec(paneFrame("yz").u, [0, 1, 0]);
});

test("a turn keeps the three planes perpendicular, and every frame orthonormal", () => {
  const R = eulerToMatrix(20, 35, -50);
  const f = Object.fromEntries(AXES.map((a) => [a, paneFrame(a, R, 0.4)]));
  close(dot3(f.xy.n, f.xz.n), 0);
  close(dot3(f.xy.n, f.yz.n), 0);
  close(dot3(f.xz.n, f.yz.n), 0);
  for (const a of AXES) {
    close(len3(f[a].u), 1);
    close(len3(f[a].v), 1);
    close(dot3(f[a].u, f[a].v), 0);
    close(dot3(f[a].u, f[a].n), 0);
    close(dot3(f[a].v, f[a].n), 0);
  }
});

test("the roll turns the picture, not the plane", () => {
  const R = eulerToMatrix(10, 20, 30);
  closeVec(paneFrame("xz", R, 1.1).n, paneFrame("xz", R, 0).n);
  assert.notDeepEqual(paneFrame("xz", R, 1.1).u, paneFrame("xz", R, 0).u);
});

test("isAligned tells axis planes from tilted ones", () => {
  assert.equal(isAligned([0, 0, 1]), true);
  assert.equal(isAligned([0, -1, 0]), true);
  assert.equal(isAligned([0.6, 0, 0.8]), false);
  assert.equal(isAligned(paneFrame("xy", eulerToMatrix(0, 0, 37)).n), true); // a turn about the normal keeps the plane
  assert.equal(isAligned(paneFrame("xy", eulerToMatrix(5, 0, 0)).n), false);
});

// ------------------------------------------------------------------------------------------ polygons and lines

const square = [[0, 0], [4, 0], [4, 4], [0, 4]];

test("clipPolygon keeps the part on one side", () => {
  const half = clipPolygon(square, 1, 0, 2); // a <= 2
  assert.equal(half.length, 4);
  assert.ok(half.every((p) => p[0] <= 2 + 1e-12));
  assert.deepEqual(clipPolygon(square, 1, 0, 10), square);      // everything stays
  assert.deepEqual(clipPolygon(square, 1, 0, -1), []);          // everything goes
  assert.deepEqual(clipPolygon([], 1, 0, 0), []);
});

test("clipLineToPolygon: the part of a line inside a convex polygon", () => {
  assert.deepEqual(clipLineToPolygon([-2, 2], [1, 0], square), [2, 6]);
  assert.deepEqual(clipLineToPolygon([2, -5], [0, 1], square), [5, 9]);
  assert.equal(clipLineToPolygon([-2, 9], [1, 0], square), null);           // misses
  assert.equal(clipLineToPolygon([0, 5], [1, 0], square), null);            // parallel and outside
  assert.equal(clipLineToPolygon([0, 0], [1, 0], [[0, 0], [1, 1]]), null);  // not a polygon
  const reversed = [...square].reverse();                                   // winding does not matter
  assert.deepEqual(clipLineToPolygon([-2, 2], [1, 0], reversed), [2, 6]);
});

test("nearestInPolygon: the point itself inside, the nearest boundary point outside", () => {
  assert.deepEqual(nearestInPolygon(square, 1, 1), [1, 1]);
  assert.deepEqual(nearestInPolygon(square, 9, 2), [4, 2]);
  assert.deepEqual(nearestInPolygon(square, -3, -3), [0, 0]);
  assert.equal(nearestInPolygon([], 1, 1), null);
  assert.deepEqual(nearestInPolygon([...square].reverse(), 1, 1), [1, 1]);
});

test("planeBoxPolygon: an axis plane cuts the box in a rectangle", () => {
  const S = [10, 20, 30];
  const poly = planeBoxPolygon([0, 0, 1], 15, S);
  assert.equal(poly.length, 4);
  assert.ok(poly.every((p) => Math.abs(p[2] - 15) < 1e-9));
  assert.deepEqual(poly.map((p) => p[0]).sort((a, b) => a - b), [0, 0, 10, 10]);
  assert.deepEqual(poly.map((p) => p[1]).sort((a, b) => a - b), [0, 0, 20, 20]);
});

test("planeBoxPolygon: a plane outside the box gives nothing", () => {
  assert.deepEqual(planeBoxPolygon([0, 0, 1], 31, [10, 20, 30]), []);
  assert.deepEqual(planeBoxPolygon([0, 0, 1], -1, [10, 20, 30]), []);
});

test("planeBoxPolygon: a tilted plane gives a convex polygon lying on the plane", () => {
  const n = unit3([1, 1, 1]);
  const S = [10, 10, 10];
  const poly = planeBoxPolygon(n, dot3(n, [5, 5, 5]), S);
  assert.equal(poly.length, 6); // the hexagon of the diagonal cut through the middle of a cube
  for (const p of poly) {
    close(dot3(n, p), dot3(n, [5, 5, 5]), 1e-9);
    assert.ok(p.every((c, i) => c >= -1e-9 && c <= S[i] + 1e-9));
  }
});

test("lineInBox: the part of a line inside the box", () => {
  const [a, b] = lineInBox([-5, 5, 5], [1, 0, 0], [10, 10, 10]);
  closeVec(a, [0, 5, 5]);
  closeVec(b, [10, 5, 5]);
  assert.equal(lineInBox([-5, 15, 5], [1, 0, 0], [10, 10, 10]), null);
  assert.equal(lineInBox([5, 5, 5], [0, 0, 0], [10, 10, 10]) === null, false); // a point inside, no direction
  assert.equal(lineInBox([50, 5, 5], [0, 0, 0], [10, 10, 10]), null);
  const diag = lineInBox([0, 0, 0], [1, 1, 1], [10, 10, 10]);
  closeVec(diag[1], [10, 10, 10]);
});

// ------------------------------------------------------------------------------------------ the pane transform

test("fitScale fits the image into the pane with a margin", () => {
  close(fitScale({ u: 100, v: 50 }, 200, 200), 1.92);
  close(fitScale({ u: 100, v: 400 }, 200, 200), 0.48);
  close(fitScale({ u: 100, v: 100 }, 200, 200, 1), 2);
  assert.equal(fitScale({ u: 0, v: 0 }, 200, 200), 1);          // degenerate: do not return Infinity
  assert.equal(fitScale({ u: 10, v: 10 }, 0, 0), 1);
});

test("absView turns the stored offset from the centre into plane coordinates", () => {
  assert.deepEqual(absView({ zoom: 2, centerU: 3, centerV: -4 }, { width: 100, height: 60 }), { zoom: 2, centerU: 53, centerV: 26 });
  assert.deepEqual(absView(defaultView(), { width: 100, height: 60 }), { zoom: 1, centerU: 50, centerV: 30 });
});

const ppv = { u: 3, v: 3 };
const flips = [{ u: 1, v: 1 }, { u: 1, v: -1 }, { u: -1, v: 1 }];

test("pixelToPlane and planeToPixel are inverses, for every flip", () => {
  const view = { zoom: 2, centerU: 40, centerV: 25 };
  for (const flip of flips) for (const [px, py] of [[0, 0], [100, 80], [37, 111], [200, 150]]) {
    const p = pixelToPlane(px, py, 200, 150, view, ppv, flip);
    const back = planeToPixel(p.u, p.v, 200, 150, view, ppv, flip);
    close(back.x, px, 1e-9);
    close(back.y, py, 1e-9);
  }
});

test("the pane centre shows the view centre", () => {
  const view = { zoom: 1, centerU: 12, centerV: 34 };
  const p = pixelToPlane(100, 75, 200, 150, view, ppv, { u: 1, v: -1 });
  assert.deepEqual(p, { u: 12, v: 34 });
});

test("flip decides which way the axes run on screen", () => {
  const view = { zoom: 1, centerU: 0, centerV: 0 };
  assert.ok(pixelToPlane(150, 75, 200, 150, view, ppv, { u: 1, v: 1 }).u > 0);
  assert.ok(pixelToPlane(150, 75, 200, 150, view, ppv, { u: -1, v: 1 }).u < 0);
  assert.ok(pixelToPlane(100, 120, 200, 150, view, ppv, { u: 1, v: 1 }).v > 0);
  assert.ok(pixelToPlane(100, 120, 200, 150, view, ppv, { u: 1, v: -1 }).v < 0);
});

test("zoomAt keeps the point under the pointer fixed", () => {
  const view = { zoom: 1, centerU: 50, centerV: 40 };
  for (const flip of flips) for (const factor of [1.25, 0.5, 3]) for (const [px, py] of [[20, 30], [180, 120], [100, 75]]) {
    const before = pixelToPlane(px, py, 200, 150, view, ppv, flip);
    const next = zoomAt(view, factor, px, py, 200, 150, ppv, flip);
    const after = pixelToPlane(px, py, 200, 150, next, { u: ppv.u * next.zoom / view.zoom, v: ppv.v * next.zoom / view.zoom }, flip);
    close(after.u, before.u, 1e-9);
    close(after.v, before.v, 1e-9);
  }
});

test("zoom is limited to 0.25 .. 64", () => {
  const view = { zoom: 60, centerU: 0, centerV: 0 };
  assert.equal(zoomAt(view, 10, 0, 0, 100, 100, ppv, flips[0]).zoom, 64);
  assert.equal(zoomAt({ ...view, zoom: 0.3 }, 0.01, 0, 0, 100, 100, ppv, flips[0]).zoom, 0.25);
});

test("minifyTaps: one tap when magnified, up to four when far out", () => {
  assert.equal(minifyTaps({ u: 2, v: 3 }), 1);
  assert.equal(minifyTaps({ u: 1, v: 1 }), 1);
  assert.equal(minifyTaps({ u: 0.5, v: 0.5 }), 2);
  assert.equal(minifyTaps({ u: 0.3, v: 5 }), 4);
  assert.equal(minifyTaps({ u: 0.01, v: 0.01 }), 4);
});

// ------------------------------------------------------------------------------------------ sample formats and windows

test("every supported dtype has a texture format", () => {
  assert.deepEqual(SUPPORTED_DTYPES, ["uint8", "int8", "uint16", "int16", "float32"]);
  for (const d of SUPPORTED_DTYPES) {
    const f = textureFormatFor(d);
    assert.ok(f.format && f.bytes > 0 && f.array.BYTES_PER_ELEMENT === f.bytes, d);
  }
  assert.equal(textureFormatFor("uint32"), null);
  assert.equal(textureFormatFor("float64"), null);
  assert.equal(textureFormatFor(undefined), null);
});

// Known gap: FORMATS[dtype] also finds inherited properties, so "constructor" or "toString" return a function
// instead of null. The dtype comes from the server's own volume.json, so this is not exploitable today, but
// `Object.hasOwn(FORMATS, dtype) ? FORMATS[dtype] : null` would close it. Reported as "todo" until then.
test("texture formats are not looked up on the prototype", { todo: "FORMATS[dtype] reads inherited properties" }, () => {
  for (const name of ["constructor", "toString", "__proto__", "hasOwnProperty"]) assert.equal(textureFormatFor(name), null, name);
});

test("the full-range window", () => {
  assert.deepEqual(fullRangeWindow("uint8"), { center: 127.5, width: 255 });
  assert.deepEqual(fullRangeWindow("int16"), { center: -0.5, width: 65535 });
  assert.deepEqual(fullRangeWindow("nonsense"), { center: 0.5, width: 1 });
});

test("applyWindow maps the window to 0..1", () => {
  assert.equal(applyWindow(50, 50, 100), 0.5);
  assert.equal(applyWindow(0, 50, 100), 0);
  assert.equal(applyWindow(100, 50, 100), 1);
  assert.equal(applyWindow(-1000, 50, 100), 0);
  assert.equal(applyWindow(1000, 50, 100), 1);
  assert.equal(applyWindow(25, 50, 100, true), 0.75);
});

test("dragWindow: horizontal changes the width, vertical the centre, both within limits", () => {
  const win = { center: 100, width: 100 };
  const wider = dragWindow(win, 80, 0, "uint8");
  assert.ok(wider.width > 100 && wider.center === 100);
  const higher = dragWindow(win, 0, 80, "uint8");
  assert.ok(higher.center > 100 && higher.width === 100);
  assert.equal(dragWindow(win, 1e6, 1e6, "uint8").center, 255);
  assert.equal(dragWindow(win, 1e6, 1e6, "uint8").width, 510);
  assert.equal(dragWindow(win, -1e6, -1e6, "uint8").center, 0);
  close(dragWindow(win, -1e6, 0, "uint8").width, 255 / 4096);
});

// Geometry of the crop shapes for mouse editing (no DOM, so it can be tested on its own).
//
// Conventions are those of kiln-render's crop shapes (patch 0007): `center` is a fraction 0..1 of the volume
// box, `size` is in world units (longest volume side = 1), `rotation` is Euler degrees applied as Rz·Ry·Rx, so
// the shape's local axes (the columns of that matrix) are what `axesOf` returns. The cylinder's axis and the
// plane's normal are the local Z axis.

const D2R = Math.PI / 180;
const R2D = 180 / Math.PI;

export const add = (a, b) => [a[0] + b[0], a[1] + b[1], a[2] + b[2]];
export const sub = (a, b) => [a[0] - b[0], a[1] - b[1], a[2] - b[2]];
export const scale = (a, s) => [a[0] * s, a[1] * s, a[2] * s];
export const dot = (a, b) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
export const cross = (a, b) => [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
export const norm = (a) => { const l = Math.hypot(a[0], a[1], a[2]); return l > 0 ? scale(a, 1 / l) : a; };

/** The local X, Y, Z axes in world space (same as kiln-render's cropAxes). */
export function axesOf(rotation) {
  const [rx, ry, rz] = rotation.map((d) => d * D2R);
  const [cx, sx, cy, sy, cz, sz] = [Math.cos(rx), Math.sin(rx), Math.cos(ry), Math.sin(ry), Math.cos(rz), Math.sin(rz)];
  const rot = (v) => {
    let [x, y, z] = v;
    [y, z] = [y * cx - z * sx, y * sx + z * cx];
    [x, z] = [x * cy + z * sy, -x * sy + z * cy];
    [x, y] = [x * cz - y * sz, x * sz + y * cz];
    return [x, y, z];
  };
  return [rot([1, 0, 0]), rot([0, 1, 0]), rot([0, 0, 1])];
}

/** Euler degrees [rx, ry, rz] (R = Rz·Ry·Rx, ry within -90..90) of the rotation whose columns are `axes`. */
export function eulerFromAxes(axes) {
  const m = (i, j) => axes[j][i]; // row i, column j
  const sy = Math.max(-1, Math.min(1, -m(2, 0)));
  const ry = Math.asin(sy);
  let rx, rz;
  if (Math.abs(sy) < 1 - 1e-9) {
    rx = Math.atan2(m(2, 1), m(2, 2));
    rz = Math.atan2(m(1, 0), m(0, 0));
  } else { // gimbal lock: only rz ± rx is defined; keep rx = 0
    rx = 0;
    rz = Math.atan2(-m(0, 1), m(1, 1));
  }
  const clean = (v) => { const d = v * R2D; return Math.abs(d) < 1e-9 ? 0 : d; };
  return [clean(rx), clean(ry), clean(rz)];
}

/** Rodrigues: vector `v` turned by `angle` (radians, right-handed) about the unit vector `axis`. */
export function rotateVec(v, axis, angle) {
  const c = Math.cos(angle), s = Math.sin(angle);
  const k = cross(axis, v);
  const d = dot(axis, v) * (1 - c);
  return [v[0] * c + k[0] * s + axis[0] * d, v[1] * c + k[1] * s + axis[1] * d, v[2] * c + k[2] * s + axis[2] * d];
}

/** Euler degrees of `rotation` after a further turn by `angle` radians about the world axis `axis`. */
export function rotateEuler(rotation, axis, angle) {
  return eulerFromAxes(axesOf(rotation).map((a) => rotateVec(a, norm(axis), angle)));
}

/** Fraction-of-box centre -> world position (the volume box is centred on the origin with extents `size`). */
export const centreToWorld = (c, size) => [(c[0] - 0.5) * size[0], (c[1] - 0.5) * size[1], (c[2] - 0.5) * size[2]];
export const worldToCentre = (w, size) => [w[0] / size[0] + 0.5, w[1] / size[1] + 0.5, w[2] / size[2] + 0.5];

/** Half extents along the local axes as drawn (the plane has no size: a fixed square). */
export function halfExtents(shape) {
  const s = shape.size;
  switch (shape.type) {
    case "box": return [s[0], s[1], s[2]];
    case "cylinder": return [s[0], s[0], s[1]];
    case "sphere": return [s[0], s[0], s[0]];
    default: return [0.4, 0.4, 0.15];
  }
}

/** Which size entry a local axis scales, or -1 if the shape has none for it (plane). */
export function sizeIndexForAxis(type, axis) {
  if (type === "box") return axis;
  if (type === "cylinder") return axis === 2 ? 1 : 0;
  if (type === "sphere") return 0;
  return -1;
}

export const MIN_SIZE = 0.005;
export const MAX_SIZE = 0.75;
/** `size` with the entry of local `axis` set to `value` (clamped); the others stay. */
export function withSize(shape, axis, value) {
  const i = sizeIndexForAxis(shape.type, axis);
  const size = [...shape.size];
  if (i >= 0) size[i] = Math.min(MAX_SIZE, Math.max(MIN_SIZE, value));
  return size;
}

const circle = (c, u, v, r, n = 48) => {
  const pts = [];
  for (let k = 0; k <= n; k++) {
    const a = (k / n) * Math.PI * 2;
    pts.push(add(c, add(scale(u, Math.cos(a) * r), scale(v, Math.sin(a) * r))));
  }
  return pts;
};
const at = (c, ax, l) => add(c, add(scale(ax[0], l[0]), add(scale(ax[1], l[1]), scale(ax[2], l[2]))));

/** The outline of the shape as polylines of world points. */
export function outline(shape, nsize) {
  const c = centreToWorld(shape.center, nsize);
  const ax = axesOf(shape.rotation);
  const [ex, ey, ez] = halfExtents(shape);
  const lines = [];
  switch (shape.type) {
    case "box": {
      const corner = (i, j, k) => at(c, ax, [i * ex, j * ey, k * ez]);
      for (const [a, b] of [[0, 1], [0, 2], [1, 2]]) {
        for (const p of [-1, 1]) for (const q of [-1, 1]) {
          const mk = (t) => { const l = [0, 0, 0]; l[a] = p; l[b] = q; l[3 - a - b] = t; return corner(...l); };
          lines.push([mk(-1), mk(1)]);
        }
      }
      break;
    }
    case "cylinder":
      lines.push(circle(add(c, scale(ax[2], ez)), ax[0], ax[1], ex), circle(add(c, scale(ax[2], -ez)), ax[0], ax[1], ex));
      for (let k = 0; k < 4; k++) {
        const a = (k * Math.PI) / 2;
        const rim = add(scale(ax[0], Math.cos(a) * ex), scale(ax[1], Math.sin(a) * ex));
        lines.push([add(add(c, rim), scale(ax[2], ez)), add(add(c, rim), scale(ax[2], -ez))]);
      }
      break;
    case "sphere":
      lines.push(circle(c, ax[0], ax[1], ex), circle(c, ax[1], ax[2], ex), circle(c, ax[0], ax[2], ex));
      break;
    default: { // plane: a square, its diagonals' cross and the normal
      lines.push([at(c, ax, [ex, ey, 0]), at(c, ax, [-ex, ey, 0]), at(c, ax, [-ex, -ey, 0]), at(c, ax, [ex, -ey, 0]), at(c, ax, [ex, ey, 0])]);
      lines.push([at(c, ax, [-ex, 0, 0]), at(c, ax, [ex, 0, 0])], [at(c, ax, [0, -ey, 0]), at(c, ax, [0, ey, 0])]);
      lines.push([c, add(c, scale(ax[2], 0.15))]);
    }
  }
  return lines;
}

/** Radius of the rotation rings and length of the move arrows, from the shape's extent. */
export function gizmoRadius(shape) {
  const [ex, ey, ez] = halfExtents(shape);
  return Math.max(0.08, Math.hypot(ex, ey, shape.type === "plane" ? 0 : ez) * 1.1);
}

/** The three rotation rings (about local X, Y, Z) as polylines. */
export function rotationRings(shape, nsize) {
  const c = centreToWorld(shape.center, nsize);
  const ax = axesOf(shape.rotation);
  const r = gizmoRadius(shape);
  return [circle(c, ax[1], ax[2], r), circle(c, ax[2], ax[0], r), circle(c, ax[0], ax[1], r)];
}

/** The three move arrows along the world axes: [start, end]. */
export function moveArrows(shape, nsize) {
  const c = centreToWorld(shape.center, nsize);
  const l = gizmoRadius(shape);
  return [[1, 0, 0], [0, 1, 0], [0, 0, 1]].map((e) => [c, add(c, scale(e, l))]);
}

/** Scale handles: world point of each end of each local axis the shape can be scaled along. */
export function scaleHandles(shape, nsize) {
  if (shape.type === "plane") return [];
  const c = centreToWorld(shape.center, nsize);
  const ax = axesOf(shape.rotation);
  const e = halfExtents(shape);
  const out = [];
  for (let a = 0; a < 3; a++) {
    for (const sign of [-1, 1]) out.push({ axis: a, sign, dir: ax[a], point: add(c, scale(ax[a], sign * e[a])) });
  }
  return out;
}

/** Parameter t on the line (p, d) closest to the ray (o, r); null if they are (nearly) parallel. */
export function closestOnLine(p, d, o, r) {
  const w = sub(p, o);
  const a = dot(d, d), b = dot(d, r), c = dot(r, r), dd = dot(d, w), e = dot(r, w);
  const den = a * c - b * b;
  if (Math.abs(den) < 1e-6 * a * c) return null;
  return (b * e - c * dd) / den;
}

/** Point where the ray (o, r) meets the plane through `p` with normal `n`; null if parallel or behind. */
export function rayPlane(o, r, p, n) {
  const den = dot(n, r);
  if (Math.abs(den) < 1e-9) return null;
  const t = dot(n, sub(p, o)) / den;
  return add(o, scale(r, t));
}

/** Distance from pixel point p to the segment a-b (all [x, y]). */
export function distToSegment(p, a, b) {
  const dx = b[0] - a[0], dy = b[1] - a[1];
  const l2 = dx * dx + dy * dy;
  const t = l2 ? Math.max(0, Math.min(1, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / l2)) : 0;
  return Math.hypot(p[0] - (a[0] + t * dx), p[1] - (a[1] + t * dy));
}

/** Distance from pixel point p to a projected polyline (array of [x, y] or null for hidden points). */
export function distToPolyline(p, pts) {
  let best = Infinity;
  for (let i = 0; i + 1 < pts.length; i++) if (pts[i] && pts[i + 1]) best = Math.min(best, distToSegment(p, pts[i], pts[i + 1]));
  return best;
}

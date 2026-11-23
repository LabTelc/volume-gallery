// Editing the crop shapes with the mouse in the 3D view.
//
// The mouse in the 3D view is in one of two modes, which the user switches (the Crop panel, the button in the 3D
// view, Esc to leave):
//   "view": the pointer drives the camera, as always (the shapes are not drawn);
//   "edit": the outlines of the enabled shapes are drawn and a left-button drag edits the selected shape with the
//           tool that is chosen:
//             move    drag an arrow to move along that world axis; drag anywhere else to move in the screen plane
//             rotate  drag a ring to turn about that axis of the shape; drag anywhere else to turn like a trackball
//             scale   drag a square to resize along that axis; drag anywhere else to scale all sizes together
//           A press on the outline of another enabled shape selects that shape. The right and middle buttons and the
//           wheel still go to the camera, so the view can be adjusted without leaving the mode.
// The shapes are the same objects the panel's sliders edit: every change calls `onChange(shape)`, which the page
// uses to send the shapes to the renderer and to move the sliders.

import { multiplyMat4, projectPoint } from "./slice/overlay-3d.js";
import { invert4, pixelRay } from "./slice/measure.js";
import * as M from "./crop-math.js";

const NS = "http://www.w3.org/2000/svg";
const AXIS_COLOR = ["#ff5a5a", "#5ad65a", "#5a9bff"];
const ACCENT = "#ffd24a";
const HIT_PX = 9;      // how close a press must be to a handle or an outline
const ROT_DEG_PER_PX = 0.4;
const show3d = (layout) => layout === "3d" || layout === "quad";

export const TOOLS = ["move", "rotate", "scale"];

export class CropEditor {
  /**
   * @param {object} o
   * @param {HTMLElement} o.host          the element that holds the kiln canvas (positioned)
   * @param {object[]} o.shapes           the shapes (edited in place)
   * @param {() => any} o.getViewer       the kiln viewer (camera, engine.config.normalizedSize)
   * @param {() => string} [o.getLayout]  the page layout ("3d", "quad", ...); the editor works where the 3D view is shown
   * @param {(shape: object) => void} o.onChange  called after the editor changed a shape
   * @param {() => boolean} [o.alive]     false once the panel that owns the editor is gone
   */
  constructor({ host, shapes, getViewer, getLayout = () => "3d", onChange, alive = () => true }) {
    this.host = host;
    this.shapes = shapes;
    this.getViewer = getViewer;
    this.getLayout = getLayout;
    this.onChange = onChange;
    this.alive = alive;
    this.mode = "view";
    this.tool = "move";
    this.selected = null;       // type of the selected shape
    this._ver = 0;
    this._key = "";
    this._drag = null;
    this._pointers = new Set();
    this._listeners = new Set();
    this._disposed = false;

    this.svg = document.createElementNS(NS, "svg");
    this.svg.setAttribute("class", "crop-editor-3d");
    this.svg.style.cssText = "position:absolute;inset:0;width:100%;height:100%;pointer-events:none;overflow:hidden;display:none";
    host.appendChild(this.svg);

    this.pill = document.createElement("div");
    this.pill.className = "crop-mode-pill";
    this.pill.hidden = true;
    this.pill.innerHTML = `<button type="button" data-mode="view" title="The mouse moves the camera">View</button><button type="button" data-mode="edit" title="The mouse edits the selected crop shape (Esc: back to the view)">Crop edit</button>`;
    this.pill.addEventListener("click", (e) => { const b = e.target.closest("button[data-mode]"); if (b) this.setMode(b.dataset.mode); });
    host.appendChild(this.pill);

    this._onDown = (e) => this._down(e);
    this._onMove = (e) => this._move(e);
    this._onUp = (e) => this._up(e);
    this._onCancel = (e) => { this._pointers.delete(e.pointerId); this._endDrag(e); };
    this._onKey = (e) => {
      if (e.key !== "Escape" || this.mode !== "edit") return;
      const t = e.target;
      const typing = t && (t.tagName === "TEXTAREA" || t.isContentEditable || (t.tagName === "INPUT" && !["range", "checkbox", "radio", "button"].includes(t.type)));
      if (typing) return; // Esc belongs to a text field
      this.setMode("view");
    };
    host.addEventListener("pointerdown", this._onDown, true);
    host.addEventListener("pointermove", this._onMove, true);
    host.addEventListener("pointerup", this._onUp, true);
    host.addEventListener("pointercancel", this._onCancel, true);
    window.addEventListener("keydown", this._onKey);
    this._ensureSelected();
    this._syncPill();
    this._raf = requestAnimationFrame(() => this._frame());
  }

  /** fn() after the mode, tool or selection changed; returns an unsubscribe function. */
  on(fn) { this._listeners.add(fn); return () => this._listeners.delete(fn); }
  _emit() { this._listeners.forEach((fn) => fn(this)); }

  setMode(mode) {
    mode = mode === "edit" ? "edit" : "view";
    if (mode === this.mode) return;
    this.mode = mode;
    this._endDrag();
    this.host.classList.toggle("crop-edit-on", mode === "edit");
    this._ensureSelected();
    this._key = "";
    this._syncPill();
    this._emit();
  }
  setTool(tool) {
    if (!TOOLS.includes(tool) || tool === this.tool) return;
    this.tool = tool;
    this._key = "";
    this._emit();
  }
  select(type) {
    const s = this.shapes.find((q) => q.type === type);
    if (!s || !s.enabled || type === this.selected) return;
    this.selected = type;
    this._key = "";
    this._emit();
  }
  /** Call when a shape was changed from outside (a slider, a switch): redraws and keeps the selection valid. */
  refresh() {
    this._ver++;
    const before = this.selected;
    this._ensureSelected();
    this._syncPill();
    if (before !== this.selected) this._emit();
  }
  get selectedShape() { return this.shapes.find((s) => s.type === this.selected && s.enabled) ?? null; }

  _ensureSelected() {
    if (this.selectedShape) return;
    this.selected = this.shapes.find((s) => s.enabled)?.type ?? null;
  }
  _syncPill() {
    const any = this.shapes.some((s) => s.enabled);
    this.pill.hidden = !(any || this.mode === "edit") || !show3d(this.getLayout());
    for (const b of this.pill.querySelectorAll("button")) b.classList.toggle("active", b.dataset.mode === this.mode);
  }

  dispose() {
    if (this._disposed) return;
    this._disposed = true;
    cancelAnimationFrame(this._raf);
    const h = this.host;
    h.removeEventListener("pointerdown", this._onDown, true);
    h.removeEventListener("pointermove", this._onMove, true);
    h.removeEventListener("pointerup", this._onUp, true);
    h.removeEventListener("pointercancel", this._onCancel, true);
    window.removeEventListener("keydown", this._onKey);
    h.classList.remove("crop-edit-on");
    this.svg.remove();
    this.pill.remove();
    this._listeners.clear();
  }

  // ---------------------------------------------------------------------------------------- camera

  _cam() {
    const viewer = this.getViewer();
    const w = this.host.clientWidth, h = this.host.clientHeight;
    if (!viewer?.camera || !w || !h) return null;
    const view = viewer.camera.getViewMatrix();
    const proj = viewer.camera.getProjectionMatrix(w / h);
    const size = viewer.engine?.config?.normalizedSize;
    if (!size) return null;
    return { viewer, view, proj, vp: multiplyMat4(proj, view), w, h, size };
  }
  _ray(cam, x, y) {
    const pos = cam.viewer.camera.position;
    return pixelRay(cam.view, cam.proj, cam.w, cam.h, x, y, pos ? Array.from(pos) : null);
  }
  /** Unit vectors of the screen's right and up and the viewing direction, in world space. */
  _screenAxes(cam) {
    const inv = invert4(cam.vp);
    if (!inv) return null;
    const un = (x, y) => {
      const ndc = [(x / cam.w) * 2 - 1, 1 - (y / cam.h) * 2, 0.5, 1];
      const o = [0, 0, 0, 0].map((_, r) => inv[r] * ndc[0] + inv[4 + r] * ndc[1] + inv[8 + r] * ndc[2] + inv[12 + r]);
      return [o[0] / o[3], o[1] / o[3], o[2] / o[3]];
    };
    const c = un(cam.w / 2, cam.h / 2);
    const right = M.norm(M.sub(un(cam.w / 2 + 10, cam.h / 2), c));
    const up = M.norm(M.sub(un(cam.w / 2, cam.h / 2 - 10), c));
    return { right, up, forward: M.norm(M.cross(right, up)) .map((v) => -v) };
  }
  _local(e) {
    const r = this.host.getBoundingClientRect();
    return [e.clientX - r.left, e.clientY - r.top];
  }
  _px(cam, p) { return projectPoint(cam.vp, p, cam.w, cam.h); }
  _pxLine(cam, pts) { return pts.map((p) => this._px(cam, p)); }

  // ---------------------------------------------------------------------------------------- hit testing

  /** The part of the selected shape's gizmo under pixel `pt` for the current tool, or null. */
  _hitPart(cam, shape, pt) {
    let best = null, bestD = HIT_PX;
    const consider = (d, part) => { if (d < bestD) { bestD = d; best = part; } };
    if (this.tool === "move") {
      M.moveArrows(shape, cam.size).forEach(([a, b], i) => {
        const pa = this._px(cam, a), pb = this._px(cam, b);
        if (pa && pb) consider(M.distToSegment(pt, pa, pb), { kind: "axis", i });
      });
    } else if (this.tool === "rotate") {
      M.rotationRings(shape, cam.size).forEach((ring, i) => consider(M.distToPolyline(pt, this._pxLine(cam, ring)), { kind: "ring", i }));
    } else {
      M.scaleHandles(shape, cam.size).forEach((h) => {
        const p = this._px(cam, h.point);
        if (p) consider(Math.hypot(p[0] - pt[0], p[1] - pt[1]), { kind: "handle", axis: h.axis, sign: h.sign });
      });
    }
    return best;
  }
  /** Another enabled shape whose outline is under `pt`. */
  _hitShape(cam, pt) {
    let best = null, bestD = HIT_PX;
    for (const s of this.shapes) {
      if (!s.enabled || s.type === this.selected) continue;
      for (const line of M.outline(s, cam.size)) {
        const d = M.distToPolyline(pt, this._pxLine(cam, line));
        if (d < bestD) { bestD = d; best = s; }
      }
    }
    return best;
  }

  // ---------------------------------------------------------------------------------------- input

  get active() { return this.mode === "edit" && show3d(this.getLayout()) && !!this.selectedShape; }

  _down(e) {
    this._pointers.add(e.pointerId);
    if (this._drag && this._pointers.size > 1) { this._endDrag(); return; } // a second finger: the camera's pinch
    if (this.mode !== "edit" || e.button !== 0 || this._pointers.size > 1 || e.target?.tagName !== "CANVAS" || !show3d(this.getLayout())) return;
    const cam = this._cam();
    if (!cam) return;
    const pt = this._local(e);
    const take = () => {
      e.stopImmediatePropagation();
      e.preventDefault();
      try { this.host.setPointerCapture(e.pointerId); } catch (_) { /* synthetic pointer */ }
    };
    const shape = this.selectedShape;
    const part = shape ? this._hitPart(cam, shape, pt) : null;
    if (!part) {
      const other = this._hitShape(cam, pt);
      if (other) { take(); this.select(other.type); return; }
      if (!shape || (this.tool === "scale" && shape.type === "plane")) return; // nothing to edit: the camera gets the press
    }
    take();
    const screen = this._screenAxes(cam);
    const ray = this._ray(cam, pt[0], pt[1]);
    if (!screen || !ray) return;
    const d = {
      id: e.pointerId, part, start: pt, last: pt, shape,
      c0: M.centreToWorld(shape.center, cam.size),
      rot0: [...shape.rotation], size0: [...shape.size], centre0: [...shape.center],
      screen, ray0: ray, total: 0,
    };
    if (part?.kind === "axis") {
      d.dir = [[1, 0, 0], [0, 1, 0], [0, 0, 1]][part.i];
      d.t0 = M.closestOnLine(d.c0, d.dir, ray.origin, ray.dir);
    } else if (part?.kind === "handle") {
      d.dir = M.axesOf(d.rot0)[part.axis];
      d.t0 = M.closestOnLine(d.c0, d.dir, ray.origin, ray.dir);
      d.ext0 = M.halfExtents(shape)[part.axis];
    } else if (part?.kind === "ring") {
      d.axis = M.axesOf(d.rot0)[part.i];
      const sc = this._px(cam, d.c0);
      d.sc = sc;
      d.ang0 = sc ? Math.atan2(pt[1] - sc[1], pt[0] - sc[0]) : 0;
      d.sign = Math.sign(M.dot(d.axis, screen.forward)) || 1;
    } else if (this.tool === "move") {
      d.hit0 = M.rayPlane(ray.origin, ray.dir, d.c0, screen.forward);
    }
    this._drag = d;
    this._key = "";
  }

  _move(e) {
    const d = this._drag;
    if (!d || e.pointerId !== d.id) return;
    e.stopImmediatePropagation();
    const cam = this._cam();
    if (!cam) return;
    const pt = this._local(e);
    const shape = d.shape;
    const ray = this._ray(cam, pt[0], pt[1]);
    let changed = false;
    if (d.part?.kind === "axis") {
      const t = ray && d.t0 !== null ? M.closestOnLine(d.c0, d.dir, ray.origin, ray.dir) : null;
      if (t !== null) { this._setCentreWorld(shape, M.add(d.c0, M.scale(d.dir, t - d.t0)), cam.size); changed = true; }
    } else if (d.part?.kind === "handle") {
      const t = ray && d.t0 !== null ? M.closestOnLine(d.c0, d.dir, ray.origin, ray.dir) : null;
      if (t !== null) { shape.size = M.withSize({ type: shape.type, size: d.size0 }, d.part.axis, d.ext0 + d.part.sign * (t - d.t0)); changed = true; }
    } else if (d.part?.kind === "ring") {
      if (d.sc) {
        const a = Math.atan2(pt[1] - d.sc[1], pt[0] - d.sc[0]);
        const prev = Math.atan2(d.last[1] - d.sc[1], d.last[0] - d.sc[0]);
        let step = a - prev;
        step -= Math.round(step / (2 * Math.PI)) * 2 * Math.PI; // (-pi, pi]: a drag may circle the centre many times
        d.total += step;
        shape.rotation = M.rotateEuler(d.rot0, d.axis, d.total * d.sign);
        changed = true;
      }
    } else if (this.tool === "move") {
      const hit = ray && d.hit0 ? M.rayPlane(ray.origin, ray.dir, d.c0, d.screen.forward) : null;
      if (hit) { this._setCentreWorld(shape, M.add(d.c0, M.sub(hit, d.hit0)), cam.size); changed = true; }
    } else if (this.tool === "rotate") {
      const dx = (pt[0] - d.start[0]) * ROT_DEG_PER_PX * (Math.PI / 180);
      const dy = (pt[1] - d.start[1]) * ROT_DEG_PER_PX * (Math.PI / 180);
      const axes = M.axesOf(d.rot0).map((v) => M.rotateVec(M.rotateVec(v, d.screen.up, dx), d.screen.right, dy));
      shape.rotation = M.eulerFromAxes(axes);
      changed = true;
    } else if (this.tool === "scale") {
      const f = Math.exp((d.start[1] - pt[1]) / 150);
      const size = [...d.size0];
      for (let a = 0; a < 3; a++) {
        const i = M.sizeIndexForAxis(shape.type, a);
        if (i >= 0 && d.size0[i] > 0) size[i] = M.withSize({ type: shape.type, size: d.size0 }, a, d.size0[i] * f)[i];
      }
      shape.size = size;
      changed = true;
    }
    d.last = pt;
    if (changed) { this._ver++; this.onChange(shape); }
  }

  _setCentreWorld(shape, w, size) {
    shape.center = M.worldToCentre(w, size).map((v) => Math.min(1, Math.max(0, v)));
  }

  _up(e) {
    this._pointers.delete(e.pointerId);
    if (this._drag && e.pointerId === this._drag.id) { e.stopImmediatePropagation(); this._endDrag(e); }
  }
  _endDrag(e) {
    if (!this._drag) return;
    if (e && e.pointerId !== this._drag.id) return;
    try { this.host.releasePointerCapture(this._drag.id); } catch (_) { /* already released */ }
    this._drag = null;
    this._key = "";
  }

  // ---------------------------------------------------------------------------------------- drawing

  _frame() {
    if (this._disposed) return;
    if (!this.alive()) { this.dispose(); return; }
    this._draw();
    this._raf = requestAnimationFrame(() => this._frame());
  }

  _draw() {
    const visible = this.mode === "edit" && show3d(this.getLayout());
    this._syncPill();
    this.svg.style.display = visible ? "" : "none";
    if (!visible) return;
    const cam = this._cam();
    if (!cam) return;
    const key = `${Array.from(cam.view)}|${Array.from(cam.proj)}|${cam.w}x${cam.h}|${this._ver}|${this.selected}|${this.tool}|${this._drag?.part?.kind ?? ""}`;
    if (key === this._key) return;
    this._key = key;
    const out = [];
    const poly = (pts, color, width, opacity = 1, dash = "") => {
      let d = "", pen = false;
      for (const p of this._pxLine(cam, pts)) {
        if (!p) { pen = false; continue; }
        d += `${pen ? "L" : "M"}${p[0].toFixed(1)} ${p[1].toFixed(1)}`;
        pen = true;
      }
      if (d) out.push(`<path d="${d}" fill="none" stroke="${color}" stroke-width="${width}" stroke-opacity="${opacity}" stroke-linejoin="round" stroke-linecap="round"${dash ? ` stroke-dasharray="${dash}"` : ""}/>`);
    };
    for (const s of this.shapes) {
      if (!s.enabled) continue;
      const sel = s.type === this.selected;
      for (const line of M.outline(s, cam.size)) {
        poly(line, "#000", sel ? 4 : 3, 0.35); // a dark under-stroke keeps the outline readable on bright voxels
        poly(line, sel ? ACCENT : "#fff", sel ? 2 : 1.25, sel ? 1 : 0.7, sel ? "" : "5 4");
      }
    }
    const shape = this.selectedShape;
    if (shape) {
      const activeKind = this._drag?.part;
      if (this.tool === "move") {
        M.moveArrows(shape, cam.size).forEach((seg, i) => {
          const hot = activeKind?.kind === "axis" && activeKind.i === i;
          poly(seg, AXIS_COLOR[i], hot ? 5 : 3);
          const tip = this._px(cam, seg[1]);
          if (tip) out.push(`<circle cx="${tip[0].toFixed(1)}" cy="${tip[1].toFixed(1)}" r="${hot ? 7 : 5}" fill="${AXIS_COLOR[i]}" stroke="#000" stroke-opacity="0.5"/>`);
        });
        const c = this._px(cam, M.centreToWorld(shape.center, cam.size));
        if (c) out.push(`<circle cx="${c[0].toFixed(1)}" cy="${c[1].toFixed(1)}" r="4" fill="#fff" stroke="#000" stroke-opacity="0.6"/>`);
      } else if (this.tool === "rotate") {
        M.rotationRings(shape, cam.size).forEach((ring, i) => {
          const hot = activeKind?.kind === "ring" && activeKind.i === i;
          poly(ring, "#000", 5, 0.3);
          poly(ring, AXIS_COLOR[i], hot ? 4 : 2.5);
        });
      } else {
        for (const h of M.scaleHandles(shape, cam.size)) {
          const p = this._px(cam, h.point);
          if (!p) continue;
          const hot = activeKind?.kind === "handle" && activeKind.axis === h.axis && activeKind.sign === h.sign;
          const r = hot ? 7 : 5.5;
          out.push(`<rect x="${(p[0] - r).toFixed(1)}" y="${(p[1] - r).toFixed(1)}" width="${2 * r}" height="${2 * r}" fill="${AXIS_COLOR[h.axis]}" stroke="#000" stroke-opacity="0.55"/>`);
        }
      }
    }
    this.svg.innerHTML = out.join("");
  }
}

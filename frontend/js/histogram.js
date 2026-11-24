// The data histogram behind the transfer function curve.
//
// The server stores counts over the data's own range [min, max] (backend/app/helpers/histogram.py).
// kiln-render draws a histogram over the transfer function's whole intensity axis, and
// that axis is the data type's full range (uint8: 0..255, uint16: 0..65535) or, for float
// data, the range the viewer derived (metadata.dataRange). So the counts are resampled onto
// that axis before they are handed to transferFunction.setHistogram().

const BINS = 1024; // what kiln-render's preview is designed around

// [lo, hi] of the transfer function's x axis in data units, or null if it is not known.
export function transferAxis(metadata, dtype) {
  if (dtype === "uint8") return [0, 255];
  if (dtype === "uint16") return [0, 65535];
  const r = metadata && metadata.dataRange;
  if (r && r.length >= 2 && Number(r[1]) > Number(r[0])) return [Number(r[0]), Number(r[1])];
  return null;
}

// Redistributes the server's counts onto `bins` equal bins over [axisMin, axisMax].
// A source bin that straddles target bins is shared in proportion to the overlap; counts
// outside the axis are dropped.
export function resampleHistogram(h, axisMin, axisMax, bins = BINS) {
  const span = axisMax - axisMin;
  if (!h || !Array.isArray(h.counts) || !(span > 0)) return null;
  const out = new Float32Array(bins);
  const counts = h.counts;
  if (!(h.max > h.min)) { // constant volume: everything sits at one value
    const j = Math.floor(((h.min - axisMin) / span) * bins);
    if (j >= 0 && j < bins) out[j] = counts.reduce((a, b) => a + b, 0);
    return out;
  }
  const srcW = (h.max - h.min) / counts.length;
  const dstW = span / bins;
  for (let i = 0; i < counts.length; i++) {
    const c = counts[i];
    if (!c) continue;
    const a = h.min + i * srcW;
    const lo = Math.max(a, axisMin);
    const hi = Math.min(a + srcW, axisMax);
    if (hi <= lo) continue;
    const j0 = Math.max(0, Math.floor((lo - axisMin) / dstW));
    const j1 = Math.min(bins - 1, Math.floor((hi - axisMin) / dstW - 1e-9));
    for (let j = j0; j <= j1; j++) {
      const overlap = Math.min(hi, axisMin + (j + 1) * dstW) - Math.max(lo, axisMin + j * dstW);
      if (overlap > 0) out[j] += (c * overlap) / srcW;
    }
  }
  return out;
}

// Where the viewer starts, from the server's slice info ({ isoValue, ramp }: fractions of the histogram's
// [min, max] range, see backend/app/helpers/volume.py) and the histogram (which says what that range is).
// Returns { isoValue, tfPoints } on the transfer function's axis, as the viewer takes them (isoValue a fraction of
// the axis, the points' x relative to the window); a part the server has no value for is left out.
// `windowCenter` / `windowWidth`: the window the viewer starts with (its default is the whole axis).
export function viewerDefaults(info, hist, { windowCenter = 0.5, windowWidth = 1 } = {}) {
  const out = {};
  if (!info || !hist || !(hist.max > hist.min)) return out;
  const [lo, hi] = transferAxis(null, hist.dtype) ?? [hist.min, hist.max];
  const clamp01 = (v) => Math.min(1, Math.max(0, v));
  const onAxis = (f) => clamp01((hist.min + f * (hist.max - hist.min) - lo) / (hi - lo));
  const isFrac = (v) => typeof v === "number" && v >= 0 && v <= 1;
  const round = (v) => Math.round(v * 1e6) / 1e6;
  const ww = Math.max(windowWidth, 1e-6);
  const inWindow = (f) => clamp01((onAxis(f) - (windowCenter - ww / 2)) / ww); // the viewer's iso value and points are in windowed space
  if (isFrac(info.isoValue)) out.isoValue = round(inWindow(info.isoValue));
  const r = info.ramp;
  if (Array.isArray(r) && r.length === 2 && isFrac(r[0]) && isFrac(r[1]) && r[0] <= r[1]) {
    let start = inWindow(r[0]);
    let end = inWindow(r[1]);
    if (end - start < 1e-3) { // (a step, or a ramp outside the window) keep the points apart
      end = Math.min(1, start + 1e-3);
      start = Math.max(0, end - 1e-3);
    }
    // transparent below the ramp, opaque above it
    out.tfPoints = [];
    if (start > 0) out.tfPoints.push({ x: 0, y: 0 });
    out.tfPoints.push({ x: round(start), y: 0 }, { x: round(end), y: 1 });
    if (end < 1) out.tfPoints.push({ x: 1, y: 1 });
  }
  return out;
}

const appliedAxis = new WeakMap(); // viewer -> "lo:hi" of the histogram it currently shows

// Gives the viewer's transfer function the histogram, resampled for its current axis.
// Cheap to call before every redraw: it only does work when the axis changed (for float
// data the range becomes known after the first bricks have loaded).
export function syncHistogram(viewer, data) {
  const tf = viewer && viewer.transferFunction;
  if (!data || !tf) return;
  const axis = transferAxis(viewer.metadata, data.dtype);
  if (!axis) return;
  const key = `${axis[0]}:${axis[1]}`;
  if (appliedAxis.get(viewer) === key) return;
  const bins = resampleHistogram(data, axis[0], axis[1]);
  if (!bins) return;
  tf.setHistogram(bins);
  appliedAxis.set(viewer, key);
}

// Keeps two scrolling panes of different content in step: the report editor's text and its preview.
//
// Both panes show the same document, but a line of text and the block it renders to have different heights (a
// wrapped paragraph, a formula, an image), so the two cannot be tied by a ratio. Instead the page lists anchors:
// pairs [y in the text pane, y in the preview] for the start of each block, and a scroll position of one pane
// is turned into one of the other by straight lines between the anchors around it.
// Pure functions on numbers: tests/scroll-sync.test.mjs.

/** Anchors from raw pairs: sorted, and cut down to those that strictly increase in both panes (a block that
 *  starts no lower than the one before it in one of them adds nothing and would make the mapping jump back). Then
 *  [0, 0] and [textMax, previewMax] are put at the ends, so the panes start and end together. `textMax` and
 *  `previewMax` are the largest scrollTop of each pane. */
export function buildAnchors(pairs, textMax, previewMax) {
  const out = [[0, 0]];
  const sorted = [...pairs].filter(([a, b]) => Number.isFinite(a) && Number.isFinite(b)).sort((p, q) => p[0] - q[0] || p[1] - q[1]);
  for (const [a, b] of sorted) {
    const [la, lb] = out[out.length - 1];
    if (a > la && b > lb && a < textMax && b < previewMax) out.push([a, b]);
  }
  if (textMax > 0 && previewMax > 0) out.push([textMax, previewMax]);
  return out;
}

/** Where `value` of one pane lies in the other. `from` is 0 for the text pane (anchors' first number) and 1 for
 *  the preview. Values outside the anchors are clamped to the ends. */
export function mapScroll(anchors, value, from) {
  const to = 1 - from;
  if (!anchors.length) return value;
  if (value <= anchors[0][from]) return anchors[0][to];
  const last = anchors[anchors.length - 1];
  if (value >= last[from]) return last[to];
  let lo = 0;
  let hi = anchors.length - 1;
  while (hi - lo > 1) {
    const mid = (lo + hi) >> 1;
    if (anchors[mid][from] <= value) lo = mid; else hi = mid;
  }
  const [a, b] = [anchors[lo], anchors[hi]];
  const t = (value - a[from]) / (b[from] - a[from]);
  return a[to] + t * (b[to] - a[to]);
}

// Gets the images of planes from the server (see backend/app/helpers/slices.py) as plans and blocks:
//
//   1. POST .../plan with the plane (origin, u, v, pixel, size) returns the plan: where each pixel samples the
//      volume and which blocks it reads from, the ones most pixels read from first;
//   2. the blocks the browser does not have yet are requested in one POST .../blocks, which streams them back as
//      the server reads them (the server reads the ones most pixels need first);
//   3. a PlaneAssembler (plane-assembler.js) builds the image from the blocks as they arrive; the caller is told
//      after every chunk and can show the image while it fills in.
//
// Blocks are kept in a byte-budgeted LRU, so a plane that overlaps one already seen (scrolling, a crosshair that
// moves, a plane that turns a little) is mostly assembled at once from the cache and only the rest is fetched.
// On a slow link (measured from the block streams, or reported by the browser) the blocks are asked for with the
// plane they are for, and the server sends only the voxels that plane reads (the rest of a block is zero, which
// deflates to nothing): about the plane's own size instead of 10-20 times it. Such blocks are good for that image
// only, so they are not cached, and nothing is prefetched.
//
// A load nobody waits for any more is aborted ("latest wins" while dragging); foreground loads run before
// prefetches, at most `maxInflight` at a time.

import { PlaneAssembler, BlockStreamParser, unpackBlock, canUnpack } from "./plane-assembler.js";
import { PrivateBlockCache } from "./block-cache.js";
import { textureFormatFor } from "./geometry.js";

/** The browser's own idea of the connection, as a starting point before any stream has been timed. */
function guessSlow() {
  try {
    const c = navigator.connection;
    if (!c) return false;
    return !!c.saveData || ["slow-2g", "2g", "3g"].includes(c.effectiveType) || (c.downlink > 0 && c.downlink < 10);
  } catch (_) {
    return false;
  }
}

export class PlaneError extends Error {
  constructor(message, status = 0) {
    super(message);
    this.name = "PlaneError";
    this.status = status;
  }
}

const abortError = () => new DOMException("aborted", "AbortError");

export class PlaneClient {
  /**
   * @param {object} o
   * @param {string} o.baseUrl   ".../slices/<token>/" (ends with "/")
   * @param {Function} [o.fetchFn]
   * @param {(refused: string) => Promise<string>} [o.refreshUrl]  returns a new baseUrl when the token expired (403);
   *   `refused` is the baseUrl that was refused
   * @param {object} [o.cache]  where the blocks are kept (block-cache.js): a PrivateBlockCache of `maxCacheBytes`
   *   by default; the pane workers pass the SharedBlockCache they share
   * @param {boolean} [o.compress]  ask for deflated blocks (default: when the browser can unpack them)
   * @param {"auto"|"slow"|"fast"} [o.link]  "auto" judges the link (bytes per second of the streams; navigator.connection
   *   at the start); "slow" / "fast" force it. A slow link gets blocks cropped to the plane.
   */
  constructor({ baseUrl, fetchFn = (...a) => fetch(...a), refreshUrl = null, maxCacheBytes = 512e6, maxInflight = 3, cache = null, compress = canUnpack(), link = "auto", slowBelow = 1.5e6, fastAbove = 4e6 }) {
    this.baseUrl = baseUrl;
    this.fetchFn = fetchFn;
    this.refreshUrl = refreshUrl;
    this.maxCacheBytes = maxCacheBytes;
    this.maxInflight = maxInflight;
    this.cache = cache ?? new PrivateBlockCache({ maxBytes: maxCacheBytes });
    this.compress = compress;
    this.link = link;
    this.slowBelow = slowBelow; // bytes/s: below this the link is slow (auto) ...
    this.fastAbove = fastAbove; // ... and above this it is fast again (in between it stays as it was)
    this.rate = 0;              // bytes/s, smoothed; 0 = not measured yet
    this._slow = guessSlow();
    this._queue = [];
    this._running = 0;
    this._seq = 0;
    this._info = null;
    this._disposed = false;
    this.counters = { loads: 0, cached: 0, fetched: 0, bytes: 0, aborted: 0, lastMs: 0, lastPlanMs: 0, lastServerPlanMs: 0, lastFirstMs: 0, lastBlocks: 0, cropped: 0 };
  }

  async info() {
    this._info ??= this._request("info").then((r) => r.json());
    try {
      return await this._info;
    } catch (err) {
      this._info = null;
      throw err;
    }
  }

  /**
   * Loads the image of a plane. `spec` is a PaneGeometry.spec(). `onUpdate(assembler)` is called when the
   * cached blocks have been applied and after every chunk of the stream; the promise resolves with
   * { plan, assembler } when the image is complete (assembler is null for a prefetch, which only fills the cache).
   * Aborting `signal` stops it.
   * @param {object} spec
   * @param {{channel?: number, priority?: number, signal?: AbortSignal, underlay?: object, onUpdate?: Function, prefetch?: boolean}} [o]
   */
  loadPlane(spec, { channel = 0, priority = 1, signal, underlay = null, onUpdate = null, prefetch = false } = {}) {
    return new Promise((resolve, reject) => {
      if (this._disposed || signal?.aborted) { reject(abortError()); return; }
      if (prefetch && this.cropping) { resolve({ plan: null, assembler: null }); return; } // what a slow link fetches is for the image on screen only
      const job = { spec, channel, priority: prefetch ? 0 : priority, underlay, onUpdate, prefetch, seq: this._seq++, controller: new AbortController(), resolve, reject, started: false };
      signal?.addEventListener("abort", () => {
        if (job.started) job.controller.abort();
        else {
          const i = this._queue.indexOf(job);
          if (i >= 0) this._queue.splice(i, 1);
          this.counters.aborted++;
          reject(abortError());
        }
      }, { once: true });
      this._queue.push(job);
      this._pump();
    });
  }

  /** Low-priority warm-up of the cache; never throws. */
  prefetchPlane(spec, { channel = 0 } = {}) {
    this.loadPlane(spec, { channel, prefetch: true }).catch(() => {});
  }

  /** Whether the link counts as slow now. */
  get slow() {
    return this.link === "slow" || (this.link === "auto" && this._slow);
  }

  /** Whether blocks are asked for cropped to the plane (a slow link, and a browser that can unpack them). */
  get cropping() {
    return this.compress && this.slow;
  }

  /** Folds the speed of a finished stream (bytes after the first chunk, over the time they took) into the estimate. */
  _measure(bytes, ms) {
    if (bytes < 64e3 || ms < 40) return; // too little to tell the link from latency
    const sample = (bytes / ms) * 1000;
    this.rate = this.rate ? 0.6 * this.rate + 0.4 * sample : sample;
    if (this.rate < this.slowBelow) this._slow = true;
    else if (this.rate > this.fastAbove) this._slow = false;
  }

  stats() {
    return {
      slow: this.slow, mbps: +((this.rate * 8) / 1e6).toFixed(1),
      ...this.cache.stats(),
      inflight: this._running,
      queued: this._queue.length,
      ...this.counters,
    };
  }

  dispose() {
    this._disposed = true;
    for (const job of this._queue.splice(0)) job.reject(abortError());
    if (!this.cache.shared) this.cache.clear(); // (a shared cache belongs to whoever made it)
  }

  // ------------------------------------------------------------------ internals

  /** The best queued job: highest priority, newest first (what is on screen now is what the user is waiting for). */
  _pump() {
    while (!this._disposed && this._running < this.maxInflight && this._queue.length) {
      let best = 0;
      for (let i = 1; i < this._queue.length; i++) {
        const a = this._queue[i], b = this._queue[best];
        if (a.priority > b.priority || (a.priority === b.priority && a.seq > b.seq)) best = i;
      }
      const job = this._queue.splice(best, 1)[0];
      job.started = true;
      this._running++;
      this._run(job).then(job.resolve, (err) => {
        if (err?.name === "AbortError") this.counters.aborted++;
        else if (!job.prefetch) console.warn("[slice] plane request failed:", err);
        job.reject(err?.name === "AbortError" ? abortError() : err);
      }).finally(() => { this._running--; this._pump(); });
    }
  }

  async _request(path, { signal, json } = {}) {
    const init = () => (json === undefined
      ? { signal }
      : { signal, method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(json) });
    const base = this.baseUrl;
    let res = await this.fetchFn(base + path, init());
    if (res.status === 403 && this.refreshUrl) {
      if (this.baseUrl === base) this.baseUrl = await this.refreshUrl(base); // (else a request of ours has renewed it meanwhile)
      res = await this.fetchFn(this.baseUrl + path, init());
    }
    if (!res.ok) {
      let detail = "";
      try {
        detail = (await res.json()).detail ?? "";
        // FastAPI validation errors (422) are a list of objects, not a string
        if (Array.isArray(detail)) detail = detail.map((d) => `${(d.loc ?? []).slice(1).join(".")}: ${d.msg}`).join("; ");
        else if (typeof detail === "object") detail = JSON.stringify(detail);
      } catch (_) { /* not JSON */ }
      throw new PlaneError(`${path.split("?")[0]}: ${detail || res.status}`, res.status);
    }
    return res;
  }

  async _run(job) {
    const pins = []; // the blocks of a shared cache that this image reads from: not replaced until it is done
    try {
      return await this._load(job, pins);
    } finally {
      this.cache.release(pins);
    }
  }

  async _load(job, pins) {
    const { spec, channel, prefetch } = job;
    const signal = job.controller.signal;
    const t0 = performance.now();

    const plan = await (await this._request("plan", { signal, json: spec })).json();
    signal.throwIfAborted(); // (a fetch that does not honour the signal, or a plan that arrived as it was aborted)
    this.counters.lastPlanMs = performance.now() - t0;
    this.counters.lastServerPlanMs = plan.planMs ?? 0;
    const fmt = textureFormatFor(plan.dtype);
    if (!fmt) throw new PlaneError(`Unsupported data type ${plan.dtype}`);
    const asm = prefetch ? null : new PlaneAssembler(plan, { underlay: job.underlay });
    const cache = this.cache;
    const mine = asm ? pins : null; // (a prefetch only fills the cache: it keeps nothing)

    // what the cache has goes in now; the rest is requested
    const missing = [];
    let cached = 0;
    for (let i = 0; i < plan.blocks.length; i += 4) {
      const gx = plan.blocks[i], gy = plan.blocks[i + 1], gz = plan.blocks[i + 2];
      if (!asm) {
        if (cache.has(channel, gx, gy, gz)) cached++;
        else missing.push(gx, gy, gz, plan.blocks[i + 3]);
        continue;
      }
      const data = cache.get(channel, gx, gy, gz, mine);
      if (data !== undefined) { asm.add(gx, gy, gz, data); cached++; }
      else missing.push(gx, gy, gz, plan.blocks[i + 3]);
    }
    this.counters.cached += cached;
    job.onUpdate?.(asm);
    let first = performance.now() - t0;

    if (missing.length) {
      // The server takes a limited number of blocks per request; a tilted plane can need more than that, so the
      // list (best blocks first) goes out in consecutive requests of at most that size.
      const perRequest = Math.max(1, plan.maxBlocksPerRequest ?? 40000) * 4;
      let got = 0;
      for (let from = 0; from < missing.length; from += perRequest) {
        signal.throwIfAborted();
        const crop = !prefetch && this.cropping;
        const res = await this._request("blocks", { signal, json: { blocks: missing.slice(from, from + perRequest), prefetch, enc: this.compress ? "deflate" : "identity", ...(crop ? { plane: spec } : {}) } });
        const partial = crop && res.headers?.get?.("X-Block-Crop") === "1"; // blocks that hold only what this plane reads
        if (partial) this.counters.cropped++;
        let tFirst = 0, bytesFirst = 0, bytesAll = 0;
        const unpacking = [];
        const count = plan.blockSize ** 3;
        const arrive = (gx, gy, gz, data) => {
          // (a cropped block is for this image only: it stays out of the cache)
          const stored = partial ? data : cache.put(channel, gx, gy, gz, data, mine); // (the cache's copy, when it keeps one)
          asm?.add(gx, gy, gz, stored);
          if (!got++) first = performance.now() - t0;
        };
        const parser = new BlockStreamParser({
          Array: fmt.array,
          blockBytes: plan.blockSize ** 3 * fmt.array.BYTES_PER_ELEMENT,
          packed: this.compress,
          onBlock: this.compress
            ? (gx, gy, gz, data) => {
                if (data === null) { arrive(gx, gy, gz, null); return; }
                unpacking.push(unpackBlock(data, fmt.array, count).then((v) => { arrive(gx, gy, gz, v); job.onUpdate?.(asm); }));
              }
            : arrive,
        });
        if (res.body?.getReader) {
          const reader = res.body.getReader();
          try {
            for (;;) {
              const { done, value } = await reader.read();
              if (done) break;
              signal.throwIfAborted();
              this.counters.bytes += value.length;
              bytesAll += value.length;
              if (!tFirst) { tFirst = performance.now(); bytesFirst = value.length; }
              parser.push(value);
              job.onUpdate?.(asm);
            }
          } finally {
            reader.cancel().catch(() => {}); // a no-op once the stream has ended; frees the connection when we stop early
          }
        } else {
          const buf = new Uint8Array(await res.arrayBuffer());
          this.counters.bytes += buf.length;
          parser.push(buf);
          job.onUpdate?.(asm);
        }
        if (tFirst && !prefetch && this.link === "auto") this._measure(bytesAll - bytesFirst, performance.now() - tFirst);
        await Promise.all(unpacking);
        signal.throwIfAborted();
        if (!parser.complete) throw new PlaneError("The block stream ended early");
      }
      this.counters.fetched += got;
    }
    if (asm && !asm.complete) throw new PlaneError(`The image is incomplete: ${asm.pending} blocks missing`);
    if (asm && cache.shared) asm.loaded.clear(); // the blocks it read are views of the shared memory, which is free to go on now
    this.counters.loads++;
    this.counters.lastMs = performance.now() - t0;
    this.counters.lastFirstMs = first;
    this.counters.lastBlocks = plan.blocks.length / 4;
    return { plan, assembler: asm };
  }
}

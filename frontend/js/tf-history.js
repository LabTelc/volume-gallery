// Undo / redo for the transfer function editors (the 3D one in pages/volume.js and the slice views' one in
// js/slice/slice-controls.js).
//
// The history is a stack of snapshots of whatever the editor edits (plain JSON-able data: the editor decides what
// goes in). An editor calls `checkpoint(snapshotBefore)` right before it changes something. A drag makes hundreds of
// changes, so it passes a `key` (the same for the whole gesture): only the first checkpoint of a gesture counts, and
// `endGesture()` (pointer up, slider released) ends it. `undo(current)` / `redo(current)` take the state as it is now
// (it goes on the opposite stack) and return the snapshot to put back, or null when there is nothing to go back to.
//
// The class is pure (no DOM, unit-tested in frontend/tests/). `bindUndoKeys` is the only DOM part: Ctrl / Cmd + Z
// undoes, Ctrl / Cmd + Shift + Z or Ctrl + Y redoes - in the editor that was used last.

const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);

export class History {
  /** @param {{limit?: number}} [o]  how many steps are kept */
  constructor({ limit = 100 } = {}) {
    this.limit = limit;
    this.past = [];
    this.future = [];
    this._key = null;
  }

  get canUndo() { return this.past.length > 0; }
  get canRedo() { return this.future.length > 0; }

  /**
   * Remembers `snapshot` (the state before a change). With a `key`, a second checkpoint with the same key (until
   * endGesture) is ignored: it is the same gesture. Returns whether a step was added.
   */
  checkpoint(snapshot, key = null) {
    if (key !== null && key === this._key) return false;
    this._key = key;
    if (this.past.length && same(this.past[this.past.length - 1], snapshot)) return false; // nothing changed since the last step
    this.past.push(snapshot);
    if (this.past.length > this.limit) this.past.shift();
    this.future.length = 0; // a new edit ends the redo line
    return true;
  }

  /** The current gesture is over: the next checkpoint starts a new step. */
  endGesture() {
    this._key = null;
  }

  _step(from, to, current) {
    this._key = null;
    while (from.length) {
      const s = from.pop();
      if (same(s, current)) continue; // a step that changed nothing: skip it
      to.push(current);
      return s;
    }
    return null;
  }

  /** The snapshot to restore, or null. `current` is the state now (kept for redo). */
  undo(current) { return this._step(this.past, this.future, current); }
  redo(current) { return this._step(this.future, this.past, current); }

  clear() {
    this.past.length = 0;
    this.future.length = 0;
    this._key = null;
  }
}

// ------------------------------------------------------------------------------------------ keyboard

let active = null; // { root, undo, redo }: the editor that was used last
let installed = false;

const isTextField = (el) => {
  if (!el) return false;
  if (el.isContentEditable || el.tagName === "TEXTAREA" || el.tagName === "SELECT") return true;
  return el.tagName === "INPUT" && !["range", "color", "checkbox", "radio", "button"].includes(el.type); // the browser's own undo for typed text
};

function install() {
  if (installed || typeof document === "undefined") return;
  installed = true;
  // a press anywhere outside the editor that was used last ends its claim on Ctrl+Z
  document.addEventListener("pointerdown", (e) => { if (active && !active.root.contains(e.target)) active = null; }, true);
  document.addEventListener("keydown", (e) => {
    if (!active || !active.root.isConnected) { active = null; return; }
    if (!(e.ctrlKey || e.metaKey) || e.altKey || isTextField(e.target)) return;
    const k = e.key.toLowerCase();
    const redo = (k === "z" && e.shiftKey) || (k === "y" && !e.shiftKey);
    const undo = k === "z" && !e.shiftKey;
    if (!undo && !redo) return;
    e.preventDefault();
    (redo ? active.redo : active.undo)();
  });
}

/**
 * Makes Ctrl / Cmd + Z (undo) and Ctrl / Cmd + Shift + Z or Ctrl + Y (redo) act for the editor inside `root`, for as
 * long as it is the one that was used last (a press or focus inside `root`).
 * @returns {() => void} unbind
 */
export function bindUndoKeys(root, { undo, redo }) {
  install();
  const me = { root, undo, redo };
  const claim = () => { active = me; };
  root.addEventListener("pointerdown", claim, true);
  root.addEventListener("focusin", claim);
  return () => {
    root.removeEventListener("pointerdown", claim, true);
    root.removeEventListener("focusin", claim);
    if (active === me) active = null;
  };
}

// How processing tasks and TIFF-stack levels are described to people (the TIFF dialog and the Processing page).
// Pure functions, no DOM: tests/task-format.test.mjs.

const UNITS = { um: "\u00b5m", "\u03bcm": "\u00b5m", nm: "nm", mm: "mm", cm: "cm", m: "m" };

/** "Full resolution", "1/2 resolution", "1/4 resolution" ... for a pyramid level (each level halves every side). */
export function levelLabel(level) {
  return level <= 0 ? "Full resolution" : `1/${2 ** level} resolution`;
}

/** "10 \u00d7 10 \u00d7 20 \u00b5m" from [x, y, z] and the volume's unit ("" when there is no unit). */
export function formatVoxelSize(size, unit) {
  if (!Array.isArray(size) || size.length < 3) return "";
  const num = (v) => String(Number(v.toPrecision(4)));
  const u = unit ? ` ${UNITS[String(unit).toLowerCase()] || unit}` : "";
  const same = size[0] === size[1] && size[1] === size[2];
  return (same ? num(size[0]) : size.slice(0, 3).map(num).join(" \u00d7 ")) + u;
}

/** The level a person most likely wants: the finest whose stack is at most `limit` bytes and may be requested;
 *  when none is, the coarsest that may be. null when nothing may be requested. */
export function pickDefaultLevel(levels, limit = 1024 ** 3) {
  const allowed = levels.filter((l) => l.allowed !== false);
  if (!allowed.length) return null;
  const fits = allowed.filter((l) => l.bytes <= limit);
  return (fits.length ? fits[0] : allowed[allowed.length - 1]).level;
}

/** "23 h 10 min left", "45 min left", "less than a minute left", or "expired" for an ISO time. */
export function timeLeft(iso, now = Date.now()) {
  if (!iso) return "";
  const ms = Date.parse(iso) - now;
  if (!(ms > 0)) return "expired";
  const minutes = Math.floor(ms / 60000);
  if (minutes < 1) return "less than a minute left";
  const days = Math.floor(minutes / 1440);
  const hours = Math.floor((minutes % 1440) / 60);
  if (days >= 1) return `${days} d ${hours} h left`;
  if (hours >= 1) return `${hours} h ${minutes % 60} min left`;
  return `${minutes} min left`;
}

/** 0..100 for a task that reports progress (done / total slices), or null when it does not (yet). */
export function taskPercent(task) {
  if (!task || !(task.progress_total > 0)) return null;
  return Math.max(0, Math.min(100, Math.floor((100 * task.progress_done) / task.progress_total)));
}

const KINDS = { tiff_export: "TIFF stack" };

/** What a task is, for a list: "TIFF stack \u00b7 full resolution". */
export function taskTitle(task) {
  const kind = KINDS[task.kind] || task.kind;
  if (task.kind === "tiff_export") return `${kind} \u00b7 ${levelLabel(Number(task.params && task.params.level) || 0).toLowerCase()}`;
  return kind;
}

/** The last line of a task's log without its time ("Failed: ..."), for the reason a task failed. */
export function lastLogMessage(task) {
  const lines = (task && task.status_log) || [];
  return lines.length ? lines[lines.length - 1].replace(/^\d{2}:\d{2}:\d{2}\s+/, "") : "";
}

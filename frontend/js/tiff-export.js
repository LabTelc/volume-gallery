// "Download as TIFF stack": a dialog to choose the quality (the resolution level the stack is made of) and ask the
// server for it. The stack is not made in the browser: the server decodes the volume's bricks into TIFF slices, packs
// them into a ZIP and keeps it for a while; the Processing page shows it and offers the download when it is ready.
// Used by the report page and the viewer.
import { api, escapeHtml, formatBytes } from "./api.js";
import { openModal } from "./organize.js";
import { levelLabel, formatVoxelSize, pickDefaultLevel } from "./task-format.js";

const STATE_NOTE = { queued: "waiting to be prepared", running: "being prepared", ready: "already prepared" };

/** `volume` is {id, title}. Resolves when the dialog is closed; a request moves on to the Processing page. */
export async function openTiffExportDialog(volume) {
  const { el, close } = openModal(`
    <h2 style="margin-top:0">Download as TIFF stack</h2>
    <p class="dim" style="margin-top:-0.6em">${escapeHtml(volume.title || "")}</p>
    <div id="tiff-body"><p class="dim">Loading\u2026</p></div>
    <div class="error-box" id="tiff-error" style="display:none"></div>
    <div class="modal-actions">
      <button type="button" data-act="cancel">Cancel</button>
      <button type="button" class="primary" data-act="go" disabled>Prepare stack</button>
    </div>`, { className: "tiff-modal" });
  const body = el.querySelector("#tiff-body");
  const errorBox = el.querySelector("#tiff-error");
  const go = el.querySelector('[data-act="go"]');
  const fail = (message) => { errorBox.textContent = message; errorBox.style.display = "block"; };
  el.querySelector('[data-act="cancel"]').addEventListener("click", close);

  let options;
  try {
    options = await api.tiffExportOptions(volume.id);
  } catch (err) {
    body.innerHTML = "";
    fail(err.message);
    return;
  }
  const chosen = pickDefaultLevel(options.levels);
  const rows = options.levels.map((l) => {
    const [w, h, d] = l.dimensions;
    const note = l.reason || (l.task_status ? STATE_NOTE[l.task_status] : "");
    return `
      <label class="tiff-level${l.allowed ? "" : " disabled"}">
        <input type="radio" name="tiff-level" value="${l.level}" ${l.allowed ? "" : "disabled"} ${l.level === chosen ? "checked" : ""} />
        <span class="tiff-level-main">
          <strong>${levelLabel(l.level)}</strong>
          <span class="dim">${w} \u00d7 ${h} voxels, ${d} slices${l.voxel_size ? ` \u00b7 voxel ${escapeHtml(formatVoxelSize(l.voxel_size, options.unit))}` : ""}</span>
        </span>
        <span class="tiff-level-size">${formatBytes(l.bytes)}${note ? `<span class="dim"> \u00b7 ${escapeHtml(note)}</span>` : ""}</span>
      </label>`;
  }).join("");
  const hours = options.retention_hours;
  const keep = hours >= 48 && hours % 24 === 0 ? `${hours / 24} days` : `${Number(hours.toFixed(1))} ${hours === 1 ? "hour" : "hours"}`;
  body.innerHTML = `
    <p class="dim">Choose the quality of the stack: each step down halves every side of the volume and makes the download
      about eight times smaller. Voxel values are written exactly as stored (${escapeHtml(options.data_type)}), one TIFF file per slice.</p>
    <div class="tiff-levels" role="radiogroup" aria-label="Quality">${rows}</div>
    <p class="dim">The server prepares the stack in the background (a large volume takes a while) and keeps the ZIP for
      ${escapeHtml(keep)}. You can follow it, and download it, on the Processing page.</p>`;
  go.disabled = chosen == null;

  go.addEventListener("click", async () => {
    const pick = el.querySelector('input[name="tiff-level"]:checked');
    if (!pick) return;
    go.disabled = true;
    errorBox.style.display = "none";
    try {
      const task = await api.requestTiffExport(volume.id, Number(pick.value));
      window.location.href = `/processing.html#task-${task.id}`;
    } catch (err) {
      fail(err.message);
      go.disabled = false;
    }
  });
}

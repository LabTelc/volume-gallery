// The Processing page: work the server does on volumes in the background, and its results. Today that is making
// TIFF stacks (asked for from a volume's report page or its viewer); other kinds of task will show up here too.
// A task is queued, running, ready (its file can be downloaded until it expires), failed or expired.
import { api, requireLogin, getRole, getUsername, renderShell, wireLogout, escapeHtml, formatBytes, startDownload } from "../js/api.js";
import { pageHint } from "../js/help.js";
import { taskTitle, taskPercent, timeLeft, lastLogMessage } from "../js/task-format.js";

requireLogin();
const role = getRole();
if (role === "guest") window.location.href = "/index.html";
const isEditor = role === "editor" || role === "admin";

document.getElementById("shell").innerHTML = `
  ${renderShell("processing", role, getUsername())}
  <main>
    <div class="toolbar">
      <div>
        <h1>Processing</h1>
        <p class="subtitle">Work the server does on volumes, and what it has made.</p>
      </div>
    </div>
    ${pageHint("processing", role)}
    <div id="error" style="display:none" class="error-box"></div>
    <div id="list"></div>
    <p id="empty" class="dim" hidden>Nothing here yet. To make a TIFF stack, open a volume\u2019s report page from the gallery and choose <strong>Download as TIFF stack</strong>.</p>
  </main>
`;
wireLogout();

const listEl = document.getElementById("list");
const emptyEl = document.getElementById("empty");
const errorEl = document.getElementById("error");
const showError = (message) => { errorEl.textContent = message; errorEl.style.display = message ? "block" : "none"; };

const STATUS = { queued: "waiting", running: "running", ready: "ready", failed: "failed", expired: "expired" };
let tasks = [];
let timer = null;
const highlight = /^#task-(\d+)$/.exec(window.location.hash)?.[1];

function renderTask(t) {
  const pct = taskPercent(t);
  const active = t.status === "queued" || t.status === "running";
  let state = "";
  if (active) {
    state = `<div class="task-progress" role="progressbar" aria-valuemin="0" aria-valuemax="100" ${pct != null ? `aria-valuenow="${pct}"` : ""}>
        <div class="task-progress-bar${pct == null ? " indeterminate" : ""}" ${pct != null ? `style="width:${pct}%"` : ""}></div>
      </div>
      <div class="dim">${t.status === "queued" ? "Waiting for its turn" : `${pct ?? 0}% (${t.progress_done} of ${t.progress_total} slices)`}</div>`;
  } else if (t.status === "ready") {
    state = `<div class="dim">${formatBytes(t.result_bytes || 0)} \u00b7 ${escapeHtml(timeLeft(t.expires_at))}</div>`;
  } else if (t.status === "failed") {
    state = `<div class="log-line failed">${escapeHtml(lastLogMessage(t))}</div>`;
  } else {
    state = `<div class="dim">The result was deleted after its retention time. Ask for it again from the volume.</div>`;
  }
  return `
    <div class="asset-row task-row${highlight && Number(highlight) === t.id ? " highlight" : ""}" data-task="${t.id}" id="task-${t.id}">
      <div class="meta">
        <div class="title">${escapeHtml(taskTitle(t))} <span class="status-badge ${t.status === "failed" ? "failed" : active ? "processing" : ""}">${active ? `<span class="mini-spinner"></span>` : ""}${STATUS[t.status] || t.status}</span></div>
        <div class="slug"><a href="/report.html?id=${t.volume_id}">${escapeHtml(t.volume_title || t.volume_slug)}</a>${t.created_by_name ? ` \u00b7 asked by ${escapeHtml(t.created_by_name)}` : ""} \u00b7 ${escapeHtml(new Date(t.created_at).toLocaleString())}</div>
        ${state}
      </div>
      <div class="actions">
        ${t.status === "ready" ? `<button type="button" class="primary" data-download="${t.id}">Download</button>` : ""}
        ${isEditor && t.status !== "queued" ? `<button type="button" data-delete="${t.id}">${active ? "Stop" : "Delete"}</button>` : ""}
        ${isEditor && t.status === "queued" ? `<button type="button" data-delete="${t.id}">Remove</button>` : ""}
      </div>
    </div>`;
}

function render() {
  emptyEl.hidden = tasks.length > 0;
  listEl.innerHTML = tasks.map(renderTask).join("");
  if (highlight) document.getElementById(`task-${highlight}`)?.scrollIntoView({ block: "nearest" });
}

async function load() {
  try {
    tasks = await api.listTasks();
    showError("");
    render();
  } catch (err) {
    showError(err.message);
  }
  // while something is being made the page follows it; otherwise it only refreshes now and then (expiry times)
  clearTimeout(timer);
  const busy = tasks.some((t) => t.status === "queued" || t.status === "running");
  timer = setTimeout(load, busy ? 2000 : 30000);
}

listEl.addEventListener("click", async (e) => {
  const download = e.target.closest("[data-download]");
  const del = e.target.closest("[data-delete]");
  try {
    if (download) {
      download.disabled = true;
      startDownload(await api.taskAccessUrl(Number(download.dataset.download)));
      setTimeout(() => { download.disabled = false; }, 3000);
    } else if (del) {
      const task = tasks.find((t) => t.id === Number(del.dataset.delete));
      const running = task && task.status === "running";
      const what = task && task.status === "ready" ? "Delete this result? It would have to be made again." : running ? "Stop this task and delete what it has made so far?" : "Remove this task?";
      if (!window.confirm(what)) return;
      del.disabled = true;
      await api.deleteTask(task.id);
      await load();
    }
  } catch (err) {
    showError(err.message);
    if (download) download.disabled = false;
    if (del) del.disabled = false;
  }
});

load();

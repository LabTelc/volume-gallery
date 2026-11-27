// The page script of index.html (it was an inline <script type="module"> there). Bundled by webpack.config.js.
import { api, requireLogin, getRole, getUsername, renderShell, wireLogout } from "../js/api.js";
import { pageHint } from "../js/help.js";
import { makeDescriptionExpandable } from "../js/description.js";
import {
  escapeHtml, compareNames, folderIndex, openEditDialog, openFolderDialog,
} from "../js/organize.js";

await requireLogin();
const role = getRole();
const isEditor = role === "admin" || role === "editor";

const params = new URLSearchParams(window.location.search);
let justUploadedId = params.get("justUploaded");
if (justUploadedId) {
  window.history.replaceState({}, "", "/index.html");
}

// ---- view settings: remembered between visits (the folder is in the address, so Back works) ----
const PREFS_KEY = "vg_gallery_prefs";
const SORTS = {
  newest: "Newest first",
  oldest: "Oldest first",
  "name-asc": "Name A\u2013Z",
  "name-desc": "Name Z\u2013A",
  type: "Type",
  status: "Status (problems first)",
};
const VIEWS = { folders: "Folders", flat: "Flat list", "by-type": "Grouped by type" };
const LAYOUTS = { list: "List", squares: "Squares" };
// Icons for the layout switch: rows (list) and bricks (squares).
const LAYOUT_ICONS = {
  list: `<svg viewBox="0 0 16 16" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" aria-hidden="true"><path d="M5.5 4h8M5.5 8h8M5.5 12h8"/><circle cx="2.4" cy="4" r="0.6" fill="currentColor"/><circle cx="2.4" cy="8" r="0.6" fill="currentColor"/><circle cx="2.4" cy="12" r="0.6" fill="currentColor"/></svg>`,
  squares: `<svg viewBox="0 0 16 16" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round" aria-hidden="true"><rect x="2" y="2" width="5" height="5" rx="0.8"/><rect x="9" y="2" width="5" height="5" rx="0.8"/><rect x="2" y="9" width="5" height="5" rx="0.8"/><rect x="9" y="9" width="5" height="5" rx="0.8"/></svg>`,
};
const TYPES = { all: "All types", volume: "CT volumes", mesh: "Meshes", pointcloud: "Point clouds" };
const TYPE_ORDER = { volume: 0, mesh: 1, pointcloud: 2 };
const TYPE_TITLES = { volume: "CT volumes", mesh: "Meshes", pointcloud: "Point clouds" };
const STATUS_ORDER = { failed: 0, processing: 1, uploaded: 2, ready: 3 };

function loadPrefs() {
  const defaults = { sort: "newest", view: "folders", type: "all", layout: "list" };
  try {
    const saved = JSON.parse(localStorage.getItem(PREFS_KEY) || "{}");
    return {
      sort: saved.sort in SORTS ? saved.sort : defaults.sort,
      view: saved.view in VIEWS ? saved.view : defaults.view,
      type: saved.type in TYPES ? saved.type : defaults.type,
      layout: saved.layout in LAYOUTS ? saved.layout : defaults.layout,
    };
  } catch (_) { return defaults; }
}
const prefs = loadPrefs();
const savePrefs = () => { try { localStorage.setItem(PREFS_KEY, JSON.stringify(prefs)); } catch (_) {} };

const state = {
  assets: [],
  folders: [],
  idx: folderIndex([]),
  search: "",
  tags: new Set(), // lower-case tags that must all be present
  folder: params.get("folder") ? Number(params.get("folder")) : null,
};

const options = (map, current) =>
  Object.entries(map).map(([k, label]) => `<option value="${k}"${k === current ? " selected" : ""}>${escapeHtml(label)}</option>`).join("");

document.getElementById("shell").innerHTML = `
  ${renderShell("gallery", role, getUsername())}
  <main id="gallery-main">
    <div class="toolbar">
      <div>
        <h1>Gallery</h1>
        <p class="subtitle">Assets you can view.</p>
      </div>
      ${isEditor ? `<button type="button" id="new-folder-btn">New folder</button>` : ""}
    </div>
    ${pageHint("gallery", role)}
    <div class="gallery-controls" id="controls" hidden>
      <div class="ctl grow">
        <label for="search">Search</label>
        <input id="search" type="search" placeholder="Title, description, slug or tag" autocomplete="off" />
      </div>
      <div class="ctl">
        <label for="f-type">Show</label>
        <select id="f-type">${options(TYPES, prefs.type)}</select>
      </div>
      <div class="ctl">
        <label for="f-sort">Sort by</label>
        <select id="f-sort">${options(SORTS, prefs.sort)}</select>
      </div>
      <div class="ctl">
        <label for="f-view">Organise</label>
        <select id="f-view">${options(VIEWS, prefs.view)}</select>
      </div>
      <div class="ctl">
        <label id="layout-label">Layout</label>
        <div class="layout-switch" role="radiogroup" aria-labelledby="layout-label">
          ${Object.entries(LAYOUT_ICONS).map(([key, icon]) => `
          <button type="button" role="radio" data-layout="${key}" aria-checked="${key === prefs.layout}" title="${LAYOUTS[key]}" aria-label="${LAYOUTS[key]}">${icon}</button>`).join("")}
        </div>
      </div>
      <span class="gallery-count" id="count"></span>
    </div>
    <div class="tag-bar" id="tag-bar"></div>
    <div id="list"></div>
  </main>
`;
wireLogout();

// Old links ending in #help: the help is a page of its own now.
if (window.location.hash === "#help") window.location.replace("/help.html");

// ---------------------------------------------------------------- one row per asset / folder

function statusBadge(v) {
  if (v.status === "processing") {
    return `<span class="status-badge processing"><span class="mini-spinner"></span>processing</span>`;
  }
  if (v.status === "uploaded") {
    return `<span class="status-badge" title="The upload was not completed">incomplete upload</span>`;
  }
  if (v.status === "failed") {
    return `<span class="status-badge failed">failed</span>`;
  }
  return "";
}

function latestLogLine(v) {
  if (v.status === "ready" || !v.status_log || v.status_log.length === 0) return "";
  const last = v.status_log[v.status_log.length - 1];
  const message = last.replace(/^\d{2}:\d{2}:\d{2}\s+/, "");
  const cls = v.status === "failed" ? "failed" : "";
  return `<div class="log-line ${cls}">${escapeHtml(message)}</div>`;
}

function getSwatchText(assetType) {
  if (assetType === "mesh") return "MSH";
  if (assetType === "pointcloud") return "PTS";
  return "CT"; // default/volume
}

// blob: URLs of the thumbnails fetched so far, so the list's periodic re-render
// (while something is processing) does not fetch them all again.
const thumbCache = new Map();

function renderRow(v, { showPath = false } = {}) {
  const viewable = v.status === "ready";
  const swatch = getSwatchText(v.asset_type);
  const extraMeta = (v.asset_type === "volume" && v.has_mesh) ? " \u00b7 mesh available" : "";
  const path = showPath && v.folder_id != null ? state.idx.pathOf(v.folder_id) : "";
  const cached = thumbCache.get(`${v.asset_type}:${v.id}`);
  return `
    <div class="asset-row" data-asset-row="${v.id}" data-kind="${v.asset_type}"${isEditor ? ` draggable="true"` : ""}>
      <div class="swatch" ${v.has_thumbnail ? `data-thumb="${v.asset_type}:${v.id}"` : ""}>${
        cached ? `<img src="${cached}" alt="" />` : swatch
      }</div>
      <div class="meta">
        <div class="title">
          ${
            viewable
              ? `<a href="/viewers/${v.asset_type}.html?id=${v.id}">${escapeHtml(v.title)}</a>`
              : `<span class="disabled">${escapeHtml(v.title)}</span>`
          }
          ${statusBadge(v)}
        </div>
        <div class="slug">${escapeHtml(v.slug)}${extraMeta}</div>
        ${v.description ? `<div class="desc">${escapeHtml(v.description)}</div>` : ""}
        ${path ? `<div class="where">in ${escapeHtml(path)}</div>` : ""}
        ${(v.tags || []).length
          ? `<div class="tags">${v.tags.map((t) => `<button type="button" class="tag-chip${state.tags.has(t.toLowerCase()) ? " active" : ""}" data-tag="${escapeHtml(t)}">${escapeHtml(t)}</button>`).join("")}</div>`
          : ""}
        ${latestLogLine(v)}
      </div>
      <div class="actions">
        ${viewable ? `<a href="/viewers/${v.asset_type}.html?id=${v.id}"><button>Open</button></a>` : ""}
        ${viewable && v.asset_type === "volume" ? `<a href="/report.html?id=${v.id}"><button title="Report, attachments and downloads of this volume">Report</button></a>` : ""}
        ${isEditor ? `<button data-edit="${v.id}" data-kind="${v.asset_type}">Edit</button>` : ""}
        ${isEditor ? `<button data-manage="${v.id}" data-kind="${v.asset_type}">Manage</button>` : ""}
      </div>
    </div>`;
}

// The squares layout: a card per item, filling the width of the page. The same data attributes as a row, so
// dragging into folders, editing and the highlight of a fresh upload work the same way.
function renderCard(v, { showPath = false } = {}) {
  const viewable = v.status === "ready";
  const cached = thumbCache.get(`${v.asset_type}:${v.id}`);
  const path = showPath && v.folder_id != null ? state.idx.pathOf(v.folder_id) : "";
  const kind = { volume: "CT volume", mesh: "mesh", pointcloud: "point cloud" }[v.asset_type] || v.asset_type;
  const tip = [v.title, v.description, path ? `in ${path}` : "", (v.tags || []).join(", ")].filter(Boolean).join("\n");
  return `
    <div class="asset-card${v.has_thumbnail ? " with-thumb" : ""}" data-asset-row="${v.id}" data-kind="${v.asset_type}"${isEditor ? ` draggable="true"` : ""} title="${escapeHtml(tip)}">
      <div class="card-thumb" ${v.has_thumbnail ? `data-thumb="${v.asset_type}:${v.id}"` : ""}>${
        cached ? `<img src="${cached}" alt="" />` : getSwatchText(v.asset_type)
      }</div>
      ${statusBadge(v) ? `<div class="card-badges">${statusBadge(v)}</div>` : ""}
      <div class="card-actions">
        ${viewable && v.asset_type === "volume" ? `<a href="/report.html?id=${v.id}"><button type="button" title="Report, attachments and downloads of this volume">Report</button></a>` : ""}
        ${isEditor ? `<button type="button" data-edit="${v.id}" data-kind="${v.asset_type}">Edit</button>` : ""}
        ${isEditor ? `<button type="button" data-manage="${v.id}" data-kind="${v.asset_type}">Manage</button>` : ""}
      </div>
      <div class="card-info">
        <div class="card-title">${
          viewable
            ? `<a class="card-link" href="/viewers/${v.asset_type}.html?id=${v.id}">${escapeHtml(v.title)}</a>`
            : `<span class="disabled">${escapeHtml(v.title)}</span>`
        }</div>
        <div class="card-sub">${escapeHtml(kind)}${v.asset_type === "volume" && v.has_mesh ? " \u00b7 mesh" : ""}</div>
        ${latestLogLine(v)}
      </div>
    </div>`;
}

// A folder card shows up to four of the datasets inside it (the folder and everything below it, after the
// filters), preferring ones that have a thumbnail. `preview` = those assets.
function renderFolderCard(f, count, preview = []) {
  const cells = [0, 1, 2, 3].map((i) => {
    const v = preview[i];
    if (!v) return `<div class="ft-cell empty"></div>`;
    const cached = thumbCache.get(`${v.asset_type}:${v.id}`);
    return `<div class="ft-cell" ${v.has_thumbnail ? `data-thumb="${v.asset_type}:${v.id}"` : ""}>${
      cached ? `<img src="${cached}" alt="" />` : getSwatchText(v.asset_type)
    }</div>`;
  }).join("");
  return `
    <div class="folder-card${preview.length ? "" : " is-empty"}" data-folder-row="${f.id}"${isEditor ? ` draggable="true"` : ""}>
      <div class="folder-body" aria-hidden="true">
        <div class="folder-tab"></div>
        <div class="folder-tiles">${cells}</div>
      </div>
      ${isEditor ? `<div class="card-actions"><button type="button" data-edit-folder="${f.id}">Edit</button></div>` : ""}
      <div class="card-info">
        <div class="card-title"><a class="card-link" href="${folderHref(f.id)}" data-open-folder="${f.id}">${escapeHtml(f.name)}</a></div>
        <div class="card-sub">${count} ${count === 1 ? "item" : "items"}</div>
      </div>
    </div>`;
}

// `count` = items in the folder and below it (after the type / search filters)
function renderFolderRow(f, count) {
  return `
    <div class="folder-row" data-folder-row="${f.id}"${isEditor ? ` draggable="true"` : ""}>
      <div class="swatch" aria-hidden="true">\u{1F4C1}</div>
      <div class="meta">
        <div class="title"><a href="${folderHref(f.id)}" data-open-folder="${f.id}">${escapeHtml(f.name)}</a></div>
        <div class="slug">${count} ${count === 1 ? "item" : "items"}</div>
      </div>
      ${isEditor ? `<div class="actions"><button data-edit-folder="${f.id}">Edit</button></div>` : ""}
    </div>`;
}

const folderHref = (id) => (id == null ? "/index.html" : `/index.html?folder=${id}`);

// ----------------------------------------------------------------------- filter, sort, render

function sortAssets(list) {
  const byName = (a, b) => compareNames(a.title, b.title) || a.id - b.id;
  const byDate = (a, b) => (a.created_at < b.created_at ? -1 : a.created_at > b.created_at ? 1 : 0);
  const cmp = {
    newest: (a, b) => -byDate(a, b) || byName(a, b),
    oldest: (a, b) => byDate(a, b) || byName(a, b),
    "name-asc": byName,
    "name-desc": (a, b) => -byName(a, b),
    type: (a, b) => TYPE_ORDER[a.asset_type] - TYPE_ORDER[b.asset_type] || byName(a, b),
    status: (a, b) => (STATUS_ORDER[a.status] ?? 9) - (STATUS_ORDER[b.status] ?? 9) || byName(a, b),
  }[prefs.sort];
  return [...list].sort(cmp);
}

function matchesSearch(a) {
  const q = state.search.trim().toLowerCase();
  if (!q) return true;
  const hay = [a.title, a.description, a.slug, ...(a.tags || []), state.idx.pathOf(a.folder_id)].join("\n").toLowerCase();
  return q.split(/\s+/).every((word) => hay.includes(word));
}

function matchesTags(a) {
  if (!state.tags.size) return true;
  const have = new Set((a.tags || []).map((t) => t.toLowerCase()));
  return [...state.tags].every((t) => have.has(t));
}

const typeOk = (a) => prefs.type === "all" || a.asset_type === prefs.type;

function renderTagBar() {
  const counts = new Map(); // lower-case tag -> [label, count]
  for (const a of state.assets) {
    if (!typeOk(a)) continue;
    for (const t of a.tags || []) {
      const k = t.toLowerCase();
      counts.set(k, [counts.get(k)?.[0] ?? t, (counts.get(k)?.[1] ?? 0) + 1]);
    }
  }
  for (const k of [...state.tags]) if (!counts.has(k)) state.tags.delete(k); // its last item was removed
  const bar = document.getElementById("tag-bar");
  const chips = [...counts.entries()].sort((a, b) => compareNames(a[1][0], b[1][0]));
  bar.innerHTML = chips.length
    ? `<span class="dim" style="font-size:0.8em">Tags:</span>` +
      chips.map(([k, [label, n]]) =>
        `<button type="button" class="tag-chip${state.tags.has(k) ? " active" : ""}" data-tag="${escapeHtml(label)}">${escapeHtml(label)} <span class="n">${n}</span></button>`).join("") +
      (state.tags.size ? `<button type="button" class="tag-chip" data-clear-tags>clear</button>` : "")
    : "";
}

function renderCrumbs() {
  const chain = state.idx.chain(state.folder);
  const parts = [`<a href="${folderHref(null)}" data-open-folder="" data-drop-folder="">Gallery</a>`];
  chain.forEach((f, i) => {
    parts.push(`<span class="sep">/</span>`);
    parts.push(i === chain.length - 1
      ? `<span class="here" data-drop-folder="${f.id}">${escapeHtml(f.name)}</span>`
      : `<a href="${folderHref(f.id)}" data-open-folder="${f.id}" data-drop-folder="${f.id}">${escapeHtml(f.name)}</a>`);
  });
  return `<nav class="crumbs" aria-label="Folder">${parts.join("")}</nav>`;
}

function render() {
  const listEl = document.getElementById("list");
  const controls = document.getElementById("controls");
  controls.hidden = state.assets.length === 0;
  renderTagBar();

  if (state.assets.length === 0) {
    listEl.className = "";
    document.getElementById("count").textContent = "";
    listEl.innerHTML = `<div class="empty-state">
      No assets yet.${role !== "reader" ? " Use \u201cUpload asset\u201d to add one." : " Ask an editor to grant you access."}
    </div>`;
    return;
  }

  // A folder that no longer exists (deleted, or not visible to this user) shows the top level.
  if (state.folder != null && !state.idx.byId.has(state.folder)) state.folder = null;

  const searching = state.search.trim() !== "" || state.tags.size > 0;
  const filtered = state.assets.filter((a) => typeOk(a) && matchesSearch(a) && matchesTags(a));
  document.getElementById("count").textContent =
    `${filtered.length} of ${state.assets.length} ${state.assets.length === 1 ? "item" : "items"}`;

  const squares = prefs.layout === "squares";
  const item = squares ? renderCard : renderRow;
  const folderItem = squares ? renderFolderCard : renderFolderRow;
  listEl.className = squares ? "gallery-grid" : "";

  let html = "";
  if (prefs.view === "folders" && !searching) {
    const inFolder = (a) => (a.folder_id ?? null) === state.folder;
    const here = sortAssets(filtered.filter(inFolder));
    const subfolders = state.idx.kids.get(state.folder) || [];
    const inside = (f) => { const ids = state.idx.subtree(f.id); return filtered.filter((a) => ids.has(a.folder_id)); };
    const previewOf = (list) => {
      const sorted = sortAssets(list);
      return [...sorted.filter((a) => a.has_thumbnail), ...sorted.filter((a) => !a.has_thumbnail)].slice(0, 4);
    };
    html += renderCrumbs();
    html += subfolders.map((f) => { const list = inside(f); return folderItem(f, list.length, squares ? previewOf(list) : undefined); }).join("");
    html += here.map((a) => item(a)).join("");
    if (!subfolders.length && !here.length) {
      html += `<div class="empty-state">${state.folder == null ? "Nothing here." : "This folder is empty."}${
        isEditor && state.folder != null ? " Drag items onto a folder, or use Edit on an item to put it here." : ""}</div>`;
    }
  } else if (prefs.view === "by-type" && !searching) {
    for (const kind of Object.keys(TYPE_TITLES)) {
      const group = sortAssets(filtered.filter((a) => a.asset_type === kind));
      if (!group.length) continue;
      html += `<div class="group-head">${TYPE_TITLES[kind]} (${group.length})</div>` + group.map((a) => item(a, { showPath: true })).join("");
    }
  } else {
    // Flat list; this is also what a search or tag filter shows, across all folders.
    html += searching && prefs.view === "folders"
      ? `<p class="dim" style="font-size:0.85em;margin-top:0">Showing matches from every folder.</p>` : "";
    html += sortAssets(filtered).map((a) => item(a, { showPath: true })).join("");
  }
  if (!filtered.length && searching) {
    html += `<div class="empty-state">No items match.</div>`;
  } else if (!filtered.length && !html.includes("empty-state")) {
    html += `<div class="empty-state">Nothing to show for this filter.</div>`;
  }
  listEl.innerHTML = html;
  wireList(listEl);
  loadThumbnails(listEl);
  highlightJustUploaded(listEl);
}

function loadThumbnails(listEl) {
  listEl.querySelectorAll("[data-thumb]").forEach(async (el) => {
    const key = el.dataset.thumb;
    if (thumbCache.has(key) || el.querySelector("img")) return;
    const [kind, id] = key.split(":");
    const url = await api.thumbnailUrl(kind, id);
    if (!url) return;
    thumbCache.set(key, url);
    el.innerHTML = `<img src="${url}" alt="" />`;
  });
}

function highlightJustUploaded(listEl) {
  if (!justUploadedId) return;
  const matchingRows = Array.from(listEl.querySelectorAll(`[data-asset-row="${justUploadedId}"]`));
  const row = matchingRows.find((r) => r.querySelector(".processing")) || matchingRows[0];
  if (row) {
    row.classList.add("just-uploaded");
    row.scrollIntoView({ behavior: "smooth", block: "center" });
    justUploadedId = null;
  }
}

// ------------------------------------------------------------------------------- interactions

function openFolder(id) {
  state.folder = id;
  window.history.pushState({}, "", folderHref(id));
  render();
}
window.addEventListener("popstate", () => {
  const f = new URLSearchParams(window.location.search).get("folder");
  state.folder = f ? Number(f) : null;
  render();
});

function toggleTag(label) {
  const k = label.toLowerCase();
  if (state.tags.has(k)) state.tags.delete(k); else state.tags.add(k);
  render();
}

async function moveAsset(kind, id, folderId) {
  try {
    await api.updateAsset(kind, id, { folder_id: folderId });
  } catch (err) {
    alert(err.message);
  }
  load();
}

async function moveFolder(id, parentId) {
  if (id === parentId) return;
  try {
    await api.updateFolder(id, { parent_id: parentId });
  } catch (err) {
    alert(err.message);
  }
  load();
}

let drag = null; // { type: "asset" | "folder", kind?, id }

function wireList(listEl) {
  listEl.querySelectorAll("[data-manage]").forEach((btn) => {
    btn.addEventListener("click", () => {
      window.location.href = `/manage.html?id=${btn.dataset.manage}&kind=${btn.dataset.kind}`;
    });
  });
  listEl.querySelectorAll("[data-edit]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const item = state.assets.find((a) => a.asset_type === btn.dataset.kind && a.id === Number(btn.dataset.edit));
      if (item) openEditDialog({ api, kind: item.asset_type, item, idx: state.idx, onSaved: load });
    });
  });
  listEl.querySelectorAll("[data-edit-folder]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const folder = state.idx.byId.get(Number(btn.dataset.editFolder));
      if (folder) openFolderDialog({ api, folder, idx: state.idx, onSaved: load });
    });
  });
  listEl.querySelectorAll("[data-open-folder]").forEach((a) => {
    a.addEventListener("click", (e) => {
      if (e.ctrlKey || e.metaKey || e.shiftKey || e.button !== 0) return; // let "open in new tab" work
      e.preventDefault();
      openFolder(a.dataset.openFolder === "" ? null : Number(a.dataset.openFolder));
    });
  });
  listEl.querySelectorAll("[data-tag]").forEach((chip) => chip.addEventListener("click", () => toggleTag(chip.dataset.tag)));
  listEl.querySelectorAll(".asset-row .desc").forEach((el) => {
    const row = el.closest(".asset-row");
    const item = state.assets.find((a) => a.asset_type === row.dataset.kind && a.id === Number(row.dataset.assetRow));
    if (item) makeDescriptionExpandable(el, { title: item.title, text: item.description });
  });

  if (!isEditor) return;
  // Drag an item (or a folder) onto a folder row, or onto a folder in the path above the list.
  listEl.querySelectorAll("[data-asset-row]").forEach((row) => {
    row.addEventListener("dragstart", (e) => {
      drag = { type: "asset", kind: row.dataset.kind, id: Number(row.dataset.assetRow) };
      e.dataTransfer.effectAllowed = "move";
      e.dataTransfer.setData("text/plain", row.dataset.assetRow);
      row.classList.add("dragging");
    });
    row.addEventListener("dragend", () => { drag = null; row.classList.remove("dragging"); });
  });
  listEl.querySelectorAll("[data-folder-row]").forEach((row) => {
    row.addEventListener("dragstart", (e) => {
      drag = { type: "folder", id: Number(row.dataset.folderRow) };
      e.dataTransfer.effectAllowed = "move";
      e.dataTransfer.setData("text/plain", row.dataset.folderRow);
      row.classList.add("dragging");
    });
    row.addEventListener("dragend", () => { drag = null; row.classList.remove("dragging"); });
  });
  const targets = [
    ...[...listEl.querySelectorAll("[data-folder-row]")].map((el) => [el, Number(el.dataset.folderRow)]),
    ...[...listEl.querySelectorAll("[data-drop-folder]")].map((el) => [el, el.dataset.dropFolder === "" ? null : Number(el.dataset.dropFolder)]),
  ];
  for (const [el, folderId] of targets) {
    el.addEventListener("dragover", (e) => {
      if (!drag || (drag.type === "folder" && drag.id === folderId)) return;
      e.preventDefault();
      e.dataTransfer.dropEffect = "move";
      el.classList.add("drop-target");
    });
    el.addEventListener("dragleave", () => el.classList.remove("drop-target"));
    el.addEventListener("drop", (e) => {
      e.preventDefault();
      el.classList.remove("drop-target");
      if (!drag) return;
      const d = drag;
      drag = null;
      if (d.type === "asset") moveAsset(d.kind, d.id, folderId); else moveFolder(d.id, folderId);
    });
  }
}

document.getElementById("search").addEventListener("input", (e) => { state.search = e.target.value; render(); });
for (const [id, key] of [["f-type", "type"], ["f-sort", "sort"], ["f-view", "view"]]) {
  document.getElementById(id).addEventListener("change", (e) => { prefs[key] = e.target.value; savePrefs(); render(); });
}
document.querySelector(".layout-switch").addEventListener("click", (e) => {
  const btn = e.target.closest("[data-layout]");
  if (!btn || btn.dataset.layout === prefs.layout) return;
  prefs.layout = btn.dataset.layout;
  savePrefs();
  document.querySelectorAll(".layout-switch [data-layout]").forEach((b) =>
    b.setAttribute("aria-checked", String(b.dataset.layout === prefs.layout)));
  render();
});
document.getElementById("tag-bar").addEventListener("click", (e) => {
  const chip = e.target.closest("[data-tag], [data-clear-tags]");
  if (!chip) return;
  if (chip.hasAttribute("data-clear-tags")) { state.tags.clear(); render(); } else toggleTag(chip.dataset.tag);
});
document.getElementById("new-folder-btn")?.addEventListener("click", () => {
  openFolderDialog({ api, parentId: prefs.view === "folders" ? state.folder : null, idx: state.idx, onSaved: load });
});

// ------------------------------------------------------------------------------------- loading

let pollTimer = null;

async function load({ isPoll = false } = {}) {
  const listEl = document.getElementById("list");
  try {
    const [volumes, meshes, pointclouds, folders] = await Promise.all([
      api.listVolumes(),
      api.listMeshes(),
      api.listPointClouds(),
      api.listFolders().catch(() => []), // the gallery still works without folders
    ]);

    volumes.forEach((v) => (v.asset_type = "volume"));
    meshes.forEach((m) => (m.asset_type = "mesh"));
    pointclouds.forEach((p) => (p.asset_type = "pointcloud"));

    state.assets = [...volumes, ...meshes, ...pointclouds];
    state.folders = folders;
    state.idx = folderIndex(folders);
  } catch (err) {
    if (!isPoll) listEl.innerHTML = `<div class="error-box">${escapeHtml(err.message)}</div>`;
    return;
  }

  render();

  const stillProcessing = state.assets.some((v) => v.status === "processing");
  clearTimeout(pollTimer);
  if (stillProcessing) {
    pollTimer = setTimeout(() => load({ isPoll: true }), 1500);
  }
}

load();

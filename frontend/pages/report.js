// The report page of a volume (report.html?id=<volume id>): the text its editors wrote (markdown with images and
// formulas, js/report-markdown.js), and the files attached to the volume. Everybody who can see the volume reads it;
// editors and administrators edit the text and manage the attachments.
import {
  api, requireLogin, getRole, getUsername, renderShell, wireLogout, escapeHtml, formatBytes, startDownload,
} from "../js/api.js";
import { pageHint } from "../js/help.js";
import { renderMarkdown } from "../js/report-markdown.js";
import { wrapSelection, prefixLines, insertText, insertDisplayMath, imageMarkdown } from "../js/markdown-edit.js";
import { buildAnchors, mapScroll } from "../js/scroll-sync.js";

await requireLogin();
const role = getRole();
const isEditor = role === "editor" || role === "admin";
const isGuest = role === "guest";
const volumeId = Number(new URLSearchParams(window.location.search).get("id"));
if (!volumeId) window.location.href = "/index.html";

document.getElementById("shell").innerHTML = `
  ${renderShell("gallery", role, getUsername())}
  <main class="report-page">
    <div class="report-layout">
      <div class="report-col">
        <div class="toolbar report-head">
          <div>
            <h1 id="title">Report</h1>
            <p class="subtitle report-author" id="subtitle"></p>
          </div>
          <div class="report-actions" id="top-actions"></div>
        </div>
        ${pageHint("report", role)}
        <div id="error" style="display:none" class="error-box"></div>
        <section id="report-section" class="report-section"></section>
        <section class="report-section" id="attachments-section"></section>
      </div>
      <aside class="report-nav" id="report-nav" aria-label="Report navigation" hidden></aside>
    </div>
  </main>
`;
wireLogout();

const $ = (id) => document.getElementById(id);
const errorEl = $("error");
const showError = (message) => { errorEl.textContent = message; errorEl.style.display = message ? "block" : "none"; };

const state = { volume: null, report: null, images: [], dirty: false };

// ------------------------------------------------------------------------------------ images in the text

const imageUrls = new Map(); // image id -> Promise of its blob: URL (fetched once; the server wants the sign-in)

function imageUrl(id) {
  if (!imageUrls.has(id)) imageUrls.set(id, api.reportImageUrl(volumeId, id));
  return imageUrls.get(id);
}

function hydrateImages(root) {
  for (const img of root.querySelectorAll("img[data-report-image]")) {
    if (img.getAttribute("src")) continue;
    imageUrl(Number(img.dataset.reportImage)).then((url) => {
      if (url) {
        img.src = url;
      } else {
        const gone = document.createElement("span");
        gone.className = "dim";
        gone.textContent = `[image not available: ${img.alt || img.dataset.reportImage}]`;
        img.replaceWith(gone);
      }
    });
  }
}

// -------------------------------------------------------------------------------------------- the top

// The author of the page: whoever added the volume (the server sends created_by_name when the reader may see it).
const authorOf = (v) => v.created_by_name || v.author_name || "";
const fmtTime = (iso) => { const d = iso ? new Date(iso) : null; return d && !isNaN(d) ? d.toLocaleString() : ""; };

function renderTop() {
  const v = state.volume;
  $("title").textContent = v.title;
  document.title = `${v.title} \u2014 Report`;
  $("subtitle").textContent = authorOf(v);
  const buttons = [];
  if (v.status === "ready") buttons.push(`<a href="/viewers/volume.html?id=${v.id}"><button type="button">Open viewer</button></a>`);
  if (isEditor && !state.editing) buttons.push(`<button type="button" class="primary" id="edit-btn">Edit report</button>`);
  $("top-actions").innerHTML = buttons.join("");
  $("edit-btn")?.addEventListener("click", startEditing);
}

// --------------------------------------------------------------------------------------- reading the text

function renderReading() {
  const text = state.report.markdown;
  $("report-section").innerHTML = text.trim()
    ? `<div class="report-body" id="report-body">${renderMarkdown(text)}</div>`
    : `<p class="dim">${isEditor ? "There is no report for this volume yet. Press <strong>Edit report</strong> to write one: text, images and formulas." : "There is no report for this volume."}</p>`;
  const body = $("report-body");
  if (body) { hydrateImages(body); giveHeadingsIds(body); }
  renderNav();
}

// ------------------------------------------------------------------------ navigation column (right)

// The headings of the text get ids, so the navigation can link to them.
function giveHeadingsIds(root) {
  const used = new Set();
  root.querySelectorAll("h1, h2, h3, h4").forEach((h, i) => {
    let base = h.textContent.trim().toLowerCase().normalize("NFKD").replace(/[\u0300-\u036f]/g, "")
      .replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "") || `section-${i + 1}`;
    let id = `ch-${base}`;
    for (let n = 2; used.has(id); n++) id = `ch-${base}-${n}`;
    used.add(id);
    h.id = id;
  });
}

const PRINT_ICON = `<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M6 9V2h12v7"/><path d="M6 18H4a2 2 0 0 1-2-2v-5a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2v5a2 2 0 0 1-2 2h-2"/><rect x="6" y="14" width="12" height="8"/></svg>`;
const INFO_ICON = `<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><circle cx="12" cy="12" r="10"/><path d="M12 16v-4M12 8h.01"/></svg>`;
let spy = null;

// Chapters (the headings of the text, indented by level), then Attachments. The entry of the chapter being read
// is highlighted while the page scrolls.
function renderNav() {
  const nav = $("report-nav");
  if (!nav) return;
  spy?.disconnect();
  const heads = [...document.querySelectorAll("#report-body h1, #report-body h2, #report-body h3, #report-body h4")];
  const top = heads.length ? Math.min(...heads.map((h) => Number(h.tagName[1]))) : 1;
  const links = heads.map((h) =>
    `<a href="#${h.id}" data-target="${h.id}" class="lvl-${Math.min(3, Number(h.tagName[1]) - top)}">${escapeHtml(h.textContent.trim())}</a>`);
  const n = state.report?.attachments?.length ?? 0;
  const extra = `<a href="#attachments-section" data-target="attachments-section" class="lvl-0 nav-extra">Attachments${n ? ` <span class="dim">(${n})</span>` : ""}</a>`;
  const v = state.volume || {};
  const created = fmtTime(v.created_at);
  const modified = fmtTime(state.report?.updated_at || v.updated_at);
  const modifiedBy = isEditor && state.report?.updated_by_name ? ` \u00b7 ${escapeHtml(state.report.updated_by_name)}` : "";
  nav.hidden = false;
  nav.innerHTML = `
    <div class="nav-title">On this page</div>
    <div class="nav-group">${links.join("")}${extra}</div>
    <div class="nav-export">
      <button type="button" class="primary" id="export-pdf-btn">${PRINT_ICON}<span>Export to PDF</span></button>
      <span class="nav-info" title="Export of the report to PDF is not available yet" aria-label="Export of the report to PDF is not available yet">${INFO_ICON}</span>
    </div>
    <div class="nav-title">Page info</div>
    <dl class="page-info">
      ${created ? `<dt>Created</dt><dd>${escapeHtml(created)}</dd>` : ""}
      ${modified ? `<dt>Last modified</dt><dd>${escapeHtml(modified)}${modifiedBy}</dd>` : ""}
    </dl>`;
  // (the Export to PDF button does nothing yet)

  const targets = [...heads, $("attachments-section")].filter(Boolean);
  const visible = new Set();
  const mark = () => {
    // the first target that is on screen (or, when none is, the last one above the top of the screen)
    let current = targets.find((t) => visible.has(t));
    if (!current) current = [...targets].reverse().find((t) => t.getBoundingClientRect().top < 0) || targets[0];
    nav.querySelectorAll("a").forEach((a) => a.classList.toggle("active", a.dataset.target === current?.id));
  };
  spy = new IntersectionObserver((entries) => {
    for (const e of entries) e.isIntersecting ? visible.add(e.target) : visible.delete(e.target);
    mark();
  }, { rootMargin: "0px 0px -65% 0px" });
  targets.forEach((t) => spy.observe(t));
  mark();
}

document.addEventListener("click", (e) => {
  const a = e.target.closest("#report-nav a[data-target]");
  if (!a) return;
  e.preventDefault();
  const el = document.getElementById(a.dataset.target);
  if (!el) return;
  el.scrollIntoView({ behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "start" });
  history.replaceState(null, "", `#${a.dataset.target}`);
});

// -------------------------------------------------------------------------------------- editing the text

function confirmLeave() {
  return !state.dirty || window.confirm("You have unsaved changes to the report. Leave without saving?");
}
window.addEventListener("beforeunload", (e) => {
  if (state.dirty) { e.preventDefault(); e.returnValue = ""; }
});

const TOOLS = [
  ["bold", "B", "Bold", (v, s, e) => wrapSelection(v, s, e, "**")],
  ["italic", "I", "Italic", (v, s, e) => wrapSelection(v, s, e, "*")],
  ["heading", "H", "Heading", (v, s, e) => prefixLines(v, s, e, "## ")],
  ["bullets", "\u2022 List", "Bulleted list", (v, s, e) => prefixLines(v, s, e, "- ")],
  ["numbers", "1. List", "Numbered list", (v, s, e) => prefixLines(v, s, e, (i) => `${i + 1}. `)],
  ["quote", "\u201c Quote", "Quote", (v, s, e) => prefixLines(v, s, e, "> ")],
  ["code", "</>", "Code", (v, s, e) => wrapSelection(v, s, e, "`")],
  ["link", "Link", "Link", (v, s, e) => wrapSelection(v, s, e, "[", "](https://)", "text")],
  ["math", "$x$", "Formula in a line (LaTeX between dollar signs)", (v, s, e) => wrapSelection(v, s, e, "$", "$", "x^2")],
  ["display", "$$", "Formula on its own lines", (v, s, e) => insertDisplayMath(v, s, e)],
];

// The editor fills the whole screen (an overlay above the page, so the rail is out of the way too): a bar with Save
// and Cancel, the formatting toolbar, and below it the text and its preview side by side, two panes of the same
// size that each scroll on their own and follow each other (js/scroll-sync.js). Images of the report and a short
// markdown help open in a drawer under the toolbar.
const HELP_HTML = `<p class="dim report-help">Markdown: <code># heading</code>, <code>**bold**</code>, <code>*italic*</code>, lists, <code>&gt; quote</code>, tables, <code>[text](https://\u2026)</code>. Formulas are LaTeX: <code>$a^2+b^2=c^2$</code> in a line, <code>$$ \u2026 $$</code> on lines of their own. Images: <code>![caption](img:12)</code> (the Image\u2026 button adds one). A link to a saved view of the volume: the Bookmark\u2026 button. Raw HTML is not shown. Ctrl+S saves.</p>`;

async function startEditing() {
  state.editing = true;
  try {
    state.images = await api.listReportImages(volumeId);
  } catch (err) {
    state.images = [];
    showError(err.message);
  }
  renderTop();

  const overlay = document.createElement("div");
  overlay.className = "report-editor";
  overlay.id = "report-editor";
  overlay.setAttribute("role", "dialog");
  overlay.setAttribute("aria-label", "Edit report");
  overlay.innerHTML = `
    <header class="re-head">
      <div class="re-title">Editing the report of <strong>${escapeHtml(state.volume.title)}</strong></div>
      <span class="dim re-note" id="save-note"></span>
      <div class="re-actions">
        <button type="button" class="primary" id="save-btn">Save report</button>
        <button type="button" id="cancel-btn">Cancel</button>
      </div>
    </header>
    <div class="md-toolbar" role="toolbar" aria-label="Formatting">
      ${TOOLS.map(([id, label, title]) => `<button type="button" data-tool="${id}" title="${escapeHtml(title)}">${escapeHtml(label)}</button>`).join("")}
      <button type="button" id="image-btn" title="Add an image to the text">Image\u2026</button>
      <input type="file" id="image-input" accept="image/png,image/jpeg,image/gif,image/webp" hidden />
      <span class="grow"></span>
      <button type="button" id="bookmark-btn" data-drawer="bookmarks" aria-expanded="false" title="Add a link to a saved view (bookmark) of this volume">Bookmark\u2026</button>
      <button type="button" data-drawer="images" aria-expanded="false">Images (<span id="image-count"></span>)</button>
      <button type="button" data-drawer="help" aria-expanded="false">Help</button>
    </div>
    <div class="re-drawer" id="re-drawer" hidden></div>
    <div class="re-panes">
      <label for="md-text" class="sr-only">Report text (markdown)</label>
      <textarea id="md-text" spellcheck="true" placeholder="Write the report in markdown. Formulas go between dollar signs: $E = mc^2$. Drop or paste an image here to add it."></textarea>
      <div class="report-body report-preview" id="md-preview" aria-label="Preview"></div>
    </div>`;
  document.body.appendChild(overlay);
  document.body.classList.add("report-editing");

  const area = $("md-text");
  const pane = $("md-preview");
  area.value = state.report.markdown;
  state.dirty = false;

  // ---- text and preview follow each other
  let anchors = null;
  let lineTops = null;
  let mirror = null;
  let setting = null; // the scroll position we just set on a pane: its scroll event is ours, not the person's
  const invalidate = () => { anchors = null; lineTops = null; };

  // The y of the top of every line of the text, measured in a hidden copy of the textarea that wraps the same way.
  function measureLines() {
    const lines = area.value.split("\n");
    if (lines.length > 20000) return null; // too long to measure on every change: the panes follow each other by ratio
    const cs = getComputedStyle(area);
    if (!mirror) {
      mirror = document.createElement("div");
      mirror.className = "re-mirror";
      mirror.setAttribute("aria-hidden", "true");
      overlay.appendChild(mirror);
    }
    for (const prop of ["fontFamily", "fontSize", "fontWeight", "lineHeight", "letterSpacing", "tabSize", "paddingTop", "paddingLeft", "paddingRight"]) {
      mirror.style[prop] = cs[prop];
    }
    mirror.style.width = `${area.clientWidth}px`;
    const frag = document.createDocumentFragment();
    for (const line of lines) {
      const row = document.createElement("div");
      row.textContent = line === "" ? "\u200b" : line;
      frag.appendChild(row);
    }
    mirror.replaceChildren(frag);
    const pad = parseFloat(cs.paddingTop) || 0;
    return Array.from(mirror.children, (row) => row.offsetTop + pad);
  }

  function computeAnchors() {
    if (!lineTops) lineTops = measureLines();
    const textMax = area.scrollHeight - area.clientHeight;
    const previewMax = pane.scrollHeight - pane.clientHeight;
    const pairs = [];
    if (lineTops) {
      const paneTop = pane.getBoundingClientRect().top;
      for (const el of pane.querySelectorAll("[data-line]")) {
        const y = lineTops[Number(el.dataset.line)];
        if (y !== undefined) pairs.push([y, el.getBoundingClientRect().top - paneTop + pane.scrollTop]);
      }
    }
    anchors = buildAnchors(pairs, textMax, previewMax);
  }

  function follow(source) {
    if (!anchors) computeAnchors();
    const [from, src, dst] = source === "text" ? [0, area, pane] : [1, pane, area];
    const dstMax = dst.scrollHeight - dst.clientHeight;
    const atEnd = src.scrollTop >= src.scrollHeight - src.clientHeight - 1;
    const top = Math.max(0, Math.min(dstMax, atEnd ? dstMax : mapScroll(anchors, src.scrollTop, from)));
    if (Math.abs(dst.scrollTop - top) < 1) return;
    setting = { el: dst, top };
    dst.scrollTop = top;
  }
  const onScroll = (source, el) => () => {
    if (setting && setting.el === el && Math.abs(el.scrollTop - setting.top) < 2) { setting = null; return; }
    follow(source);
  };
  area.addEventListener("scroll", onScroll("text", area));
  pane.addEventListener("scroll", onScroll("preview", pane));
  pane.addEventListener("load", () => { invalidate(); follow("text"); }, true); // an image got its height
  const resizeWatch = new ResizeObserver(() => invalidate());
  resizeWatch.observe(area);
  resizeWatch.observe(pane);

  const preview = () => {
    pane.innerHTML = renderMarkdown(area.value, { sourceLines: true });
    hydrateImages(pane);
    invalidate();
    follow("text");
  };
  let timer = null;
  area.addEventListener("input", () => {
    state.dirty = area.value !== state.report.markdown;
    lineTops = null;
    clearTimeout(timer);
    timer = setTimeout(preview, 150);
  });
  preview();

  // ---- toolbar
  const apply = (edit) => {
    area.value = edit.value;
    area.focus();
    area.setSelectionRange(edit.selStart, edit.selEnd);
    area.dispatchEvent(new Event("input"));
  };
  overlay.querySelector(".md-toolbar").addEventListener("click", (e) => {
    const tool = TOOLS.find(([id]) => id === e.target.closest("[data-tool]")?.dataset.tool);
    if (tool) apply(tool[3](area.value, area.selectionStart, area.selectionEnd));
  });

  // ---- drawer: the images of this report, and the help
  let drawer = null;
  const drawerEl = $("re-drawer");
  const showDrawer = (which) => {
    drawer = which === drawer ? null : which;
    drawerEl.hidden = drawer === null;
    overlay.querySelectorAll("[data-drawer]").forEach((b) => b.setAttribute("aria-expanded", String(b.dataset.drawer === drawer)));
    if (drawer === "help") drawerEl.innerHTML = HELP_HTML;
    if (drawer === "images") renderImageList();
    if (drawer === "bookmarks") renderBookmarkList();
  };
  overlay.querySelectorAll("[data-drawer]").forEach((b) => b.addEventListener("click", () => showDrawer(b.dataset.drawer)));
  state.refreshImages = () => { $("image-count").textContent = String(state.images.length); if (drawer === "images") renderImageList(); };
  state.refreshImages();

  const addImage = async (file) => {
    if (!file) return;
    $("save-note").textContent = `Uploading ${file.name}\u2026`;
    try {
      const image = await api.uploadReportImage(volumeId, file);
      state.images.push(image);
      state.refreshImages();
      apply(insertText(area.value, area.selectionStart, area.selectionEnd, imageMarkdown(image.id, image.filename)));
      $("save-note").textContent = "";
    } catch (err) {
      $("save-note").textContent = "";
      showError(err.message);
    }
  };
  $("image-btn").addEventListener("click", () => $("image-input").click());
  $("image-input").addEventListener("change", async (e) => {
    for (const file of [...e.target.files]) await addImage(file);
    e.target.value = "";
  });
  area.addEventListener("paste", (e) => {
    const file = [...(e.clipboardData?.files || [])].find((f) => f.type.startsWith("image/"));
    if (file) { e.preventDefault(); addImage(file); }
  });
  area.addEventListener("dragover", (e) => { if ([...(e.dataTransfer?.types || [])].includes("Files")) e.preventDefault(); });
  area.addEventListener("drop", async (e) => {
    const files = [...(e.dataTransfer?.files || [])].filter((f) => f.type.startsWith("image/"));
    if (!files.length) return;
    e.preventDefault();
    for (const file of files) await addImage(file);
  });

  // ---- save, cancel
  $("save-btn").addEventListener("click", async (e) => {
    const btn = e.currentTarget;
    btn.disabled = true;
    showError("");
    try {
      state.report = await api.saveReport(volumeId, area.value);
      state.dirty = false;
      stopEditing();
    } catch (err) {
      showError(err.message);
      $("save-note").textContent = err.message;
      btn.disabled = false;
    }
  });
  $("cancel-btn").addEventListener("click", () => { if (confirmLeave()) { state.dirty = false; stopEditing(); } });
  overlay.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") { e.preventDefault(); $("save-btn").click(); }
  });

  state.closeEditor = () => {
    clearTimeout(timer);
    resizeWatch.disconnect();
    overlay.remove();
    document.body.classList.remove("report-editing");
    state.closeEditor = null;
    state.refreshImages = null;
  };
  area.focus();
}

// Bookmarks saved "for everyone" in the viewer have a link that opens the viewer on that view; the drawer lists them and
// puts such a link into the text. (A bookmark kept only in one browser cannot be linked to.)
async function renderBookmarkList() {
  const box = $("re-drawer");
  if (!box) return;
  box.innerHTML = `<p class="dim">Loading bookmarks\u2026</p>`;
  let list = [];
  try {
    state.bookmarks = list = await api.listVolumeBookmarks(volumeId);
  } catch (err) {
    box.innerHTML = `<p class="dim">Could not load the bookmarks: ${escapeHtml(err.message)}</p>`;
    return;
  }
  box.innerHTML = (list.length ? list.map((b) => `
      <div class="image-row" data-bookmark="${b.id}">
        <span class="bm-thumb">${b.spec?.thumb ? `<img src="${escapeHtml(b.spec.thumb)}" alt="" />` : "<span>no picture</span>"}</span>
        <span class="grow">${escapeHtml(b.name)}${b.created_by_name ? ` <span class="dim">\u00b7 ${escapeHtml(b.created_by_name)}</span>` : ""}</span>
        <button type="button" data-insert-bookmark="${b.id}" title="Insert a link to this view at the cursor">Insert link</button>
      </div>`).join("")
    : `<p class="dim">This volume has no bookmarks for everyone yet.</p>`) +
    `<p class="dim report-help">Bookmarks are made in the viewer: <strong>Bookmarks \u2192 Save for everyone</strong>. ` +
    `<a href="/viewers/volume.html?id=${volumeId}" target="_blank">Open the viewer</a>. Reload this list with the Bookmark button.</p>`;
}

function renderImageList() {
  const box = $("re-drawer");
  if (!box) return;
  box.innerHTML = state.images.length
    ? state.images.map((im) => `
        <div class="image-row" data-image="${im.id}">
          <span class="mono">img:${im.id}</span>
          <span class="grow">${escapeHtml(im.filename)} <span class="dim">\u00b7 ${formatBytes(im.size)}</span></span>
          <button type="button" data-insert="${im.id}">Insert</button>
          <button type="button" data-remove="${im.id}">Delete</button>
        </div>`).join("")
    : `<p class="dim">No images yet. Use <strong>Image\u2026</strong>, or drop or paste one into the text.</p>`;
}

document.addEventListener("click", (e) => {
  const btn = e.target.closest("[data-insert-bookmark]");
  const area = $("md-text");
  if (!btn || !area) return;
  const b = (state.bookmarks || []).find((x) => x.id === Number(btn.dataset.insertBookmark));
  if (!b) return;
  const label = b.name.replace(/[\[\]\\]/g, "");
  const link = `[${label}](/viewers/volume.html?id=${volumeId}&bookmark=${b.id})`;
  const sel = area.value.slice(area.selectionStart, area.selectionEnd);
  const edit = insertText(area.value, area.selectionStart, area.selectionEnd, sel ? link.replace(`[${label}]`, `[${sel}]`) : link);
  area.value = edit.value;
  area.focus();
  area.setSelectionRange(edit.selStart, edit.selEnd);
  area.dispatchEvent(new Event("input"));
});

document.addEventListener("click", async (e) => {
  const insert = e.target.closest("[data-insert]");
  const remove = e.target.closest("[data-remove]");
  const area = $("md-text");
  if (insert && area) {
    const im = state.images.find((i) => i.id === Number(insert.dataset.insert));
    const edit = insertText(area.value, area.selectionStart, area.selectionEnd, imageMarkdown(im.id, im.filename));
    area.value = edit.value;
    area.focus();
    area.setSelectionRange(edit.selStart, edit.selEnd);
    area.dispatchEvent(new Event("input"));
  } else if (remove && area) {
    const id = Number(remove.dataset.remove);
    if (!window.confirm("Delete this image? Where the text uses it, it will show as missing.")) return;
    try {
      await api.deleteReportImage(volumeId, id);
      state.images = state.images.filter((i) => i.id !== id);
      imageUrls.delete(id);
      state.refreshImages?.();
      area.dispatchEvent(new Event("input"));
    } catch (err) {
      showError(err.message);
    }
  }
});

function stopEditing() {
  state.editing = false;
  state.closeEditor?.();
  renderTop();
  renderReading();
}

// ---------------------------------------------------------------------------------------- attachments

function renderAttachments() {
  const items = state.report.attachments;
  const canTake = state.report.can_download;
  const rows = items.map((a) => `
    <div class="attach-row" data-att="${a.id}">
      <div class="attach-main">
        <div class="attach-name">${escapeHtml(a.filename)}</div>
        <div class="dim">${formatBytes(a.size)} \u00b7 ${escapeHtml(new Date(a.created_at).toLocaleDateString())}${a.description ? ` \u00b7 ${escapeHtml(a.description)}` : ""}</div>
      </div>
      <div class="actions">
        <button type="button" data-get="${a.id}" ${canTake ? "" : 'disabled title="This share link does not allow downloads"'}>Download</button>
        ${isEditor ? `<button type="button" data-describe="${a.id}">Edit</button><button type="button" data-drop="${a.id}">Delete</button>` : ""}
      </div>
    </div>`).join("");
  $("attachments-section").innerHTML = `
    <h2>Attachments</h2>
    ${rows || `<p class="dim">${isEditor ? "No files are attached to this volume." : "No files are attached."}</p>`}
    ${isEditor ? `
      <div class="attach-add">
        <button type="button" id="attach-btn">Attach files\u2026</button>
        <input type="file" id="attach-input" multiple hidden />
        <span class="dim" id="attach-note"></span>
      </div>` : ""}`;
  renderNav();
  $("attach-btn")?.addEventListener("click", () => $("attach-input").click());
  $("attach-input")?.addEventListener("change", async (e) => {
    const files = [...e.target.files];
    e.target.value = "";
    const note = $("attach-note");
    let failed = 0;
    for (const [i, file] of files.entries()) {
      try {
        await api.uploadAttachment(volumeId, file, "", (share) => {
          note.textContent = `Uploading ${i + 1} of ${files.length}: ${file.name} (${Math.round(share * 100)}%)`;
        });
      } catch (err) {
        failed++;
        showError(`${file.name}: ${err.message}`);
      }
    }
    note.textContent = failed ? `${files.length - failed} of ${files.length} files attached` : "";
    await reloadAttachments();
  });
}

$("attachments-section").addEventListener("click", async (e) => {
  const get = e.target.closest("[data-get]");
  const describe = e.target.closest("[data-describe]");
  const drop = e.target.closest("[data-drop]");
  try {
    if (get) {
      get.disabled = true;
      startDownload(await api.attachmentAccessUrl(volumeId, Number(get.dataset.get)));
      setTimeout(() => { get.disabled = false; }, 3000);
    } else if (describe) {
      const att = state.report.attachments.find((a) => a.id === Number(describe.dataset.describe));
      const name = window.prompt("File name (as shown and as saved by the browser):", att.filename);
      if (name === null) return;
      const description = window.prompt("Description (optional):", att.description || "");
      if (description === null) return;
      await api.updateAttachment(volumeId, att.id, { filename: name || att.filename, description });
      await reloadAttachments();
    } else if (drop) {
      const att = state.report.attachments.find((a) => a.id === Number(drop.dataset.drop));
      if (!window.confirm(`Delete ${att.filename}? The file is removed from the server.`)) return;
      await api.deleteAttachment(volumeId, att.id);
      await reloadAttachments();
    }
  } catch (err) {
    showError(err.message);
    if (get) get.disabled = false;
  }
});

async function reloadAttachments() {
  state.report.attachments = await api.listAttachments(volumeId);
  renderAttachments();
}

// ------------------------------------------------------------------------------------------------ start

(async () => {
  try {
    [state.volume, state.report] = await Promise.all([api.getVolume(volumeId), api.getReport(volumeId)]);
  } catch (err) {
    showError(/not found/i.test(err.message) ? "This volume does not exist, or you do not have access to it." : err.message);
    return;
  }
  renderTop();
  renderReading();
  renderAttachments();
})();

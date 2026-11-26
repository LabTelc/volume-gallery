// The report page of a volume (report.html?id=<volume id>): the text its editors wrote (markdown with images and
// formulas, js/report-markdown.js), and the files attached to the volume. Everybody who can see the volume reads it;
// editors and administrators edit the text and manage the attachments.
import {
  api, requireLogin, getRole, getUsername, renderShell, wireLogout, escapeHtml, formatBytes, startDownload,
} from "../js/api.js";
import { pageHint } from "../js/help.js";
import { renderMarkdown } from "../js/report-markdown.js";
import { wrapSelection, prefixLines, insertText, insertDisplayMath, imageMarkdown } from "../js/markdown-edit.js";
import { openTiffExportDialog } from "../js/tiff-export.js";

requireLogin();
const role = getRole();
const isEditor = role === "editor" || role === "admin";
const isGuest = role === "guest";
const volumeId = Number(new URLSearchParams(window.location.search).get("id"));
if (!volumeId) window.location.href = "/index.html";

document.getElementById("shell").innerHTML = `
  ${renderShell("gallery", role, getUsername())}
  <main>
    <div class="toolbar">
      <div>
        <h1 id="title">Report</h1>
        <p class="subtitle" id="subtitle"></p>
      </div>
      <div class="report-actions" id="top-actions"></div>
    </div>
    ${pageHint("report", role)}
    <div id="error" style="display:none" class="error-box"></div>
    <section id="report-section" class="report-section"></section>
    <section class="report-section" id="attachments-section"></section>
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

function renderTop() {
  const v = state.volume;
  $("title").textContent = v.title;
  document.title = `${v.title} \u2014 Report`;
  const meta = [];
  if (state.report.updated_at) {
    meta.push(`Report updated ${new Date(state.report.updated_at).toLocaleDateString()}${state.report.updated_by_name && isEditor ? ` by ${state.report.updated_by_name}` : ""}`);
  }
  $("subtitle").textContent = meta.join(" \u00b7 ");
  const buttons = [];
  if (v.status === "ready") buttons.push(`<a href="/viewers/volume.html?id=${v.id}"><button type="button">Open viewer</button></a>`);
  if (v.status === "ready" && !isGuest) buttons.push(`<button type="button" id="tiff-btn" title="Prepare all slices as TIFF files in a ZIP">Download as TIFF stack</button>`);
  if (isEditor && !state.editing) buttons.push(`<button type="button" class="primary" id="edit-btn">Edit report</button>`);
  $("top-actions").innerHTML = buttons.join("");
  $("tiff-btn")?.addEventListener("click", () => openTiffExportDialog(v));
  $("edit-btn")?.addEventListener("click", startEditing);
}

// --------------------------------------------------------------------------------------- reading the text

function renderReading() {
  const text = state.report.markdown;
  $("report-section").innerHTML = text.trim()
    ? `<div class="report-body" id="report-body">${renderMarkdown(text)}</div>`
    : `<p class="dim">${isEditor ? "There is no report for this volume yet. Press <strong>Edit report</strong> to write one: text, images and formulas." : "There is no report for this volume."}</p>`;
  const body = $("report-body");
  if (body) hydrateImages(body);
}

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

async function startEditing() {
  state.editing = true;
  try {
    state.images = await api.listReportImages(volumeId);
  } catch (err) {
    state.images = [];
    showError(err.message);
  }
  renderTop();
  $("report-section").innerHTML = `
    <div class="md-toolbar" role="toolbar" aria-label="Formatting">
      ${TOOLS.map(([id, label, title]) => `<button type="button" data-tool="${id}" title="${escapeHtml(title)}">${escapeHtml(label)}</button>`).join("")}
      <button type="button" id="image-btn" title="Add an image to the text">Image\u2026</button>
      <input type="file" id="image-input" accept="image/png,image/jpeg,image/gif,image/webp" hidden />
    </div>
    <div class="report-edit">
      <div>
        <label for="md-text" class="sr-only">Report text (markdown)</label>
        <textarea id="md-text" spellcheck="true" placeholder="Write the report in markdown. Formulas go between dollar signs: $E = mc^2$. Drop or paste an image here to add it."></textarea>
      </div>
      <div class="report-body report-preview" id="md-preview" aria-label="Preview"></div>
    </div>
    <div class="report-edit-foot">
      <button type="button" class="primary" id="save-btn">Save report</button>
      <button type="button" id="cancel-btn">Cancel</button>
      <span class="dim" id="save-note"></span>
    </div>
    <details class="report-images" id="images-box"><summary>Images of this report (<span id="image-count"></span>)</summary><div id="image-list"></div></details>
    <p class="dim report-help">Markdown: <code># heading</code>, <code>**bold**</code>, <code>*italic*</code>, lists, <code>&gt; quote</code>, tables, <code>[text](https://\u2026)</code>. Formulas are LaTeX: <code>$a^2+b^2=c^2$</code> in a line, <code>$$ \u2026 $$</code> on lines of their own. Raw HTML is not shown.</p>`;
  const area = $("md-text");
  area.value = state.report.markdown;
  state.dirty = false;
  const preview = () => { $("md-preview").innerHTML = renderMarkdown(area.value); hydrateImages($("md-preview")); };
  let timer = null;
  area.addEventListener("input", () => {
    state.dirty = area.value !== state.report.markdown;
    clearTimeout(timer);
    timer = setTimeout(preview, 150);
  });
  preview();
  renderImageList();

  const apply = (edit) => {
    area.value = edit.value;
    area.focus();
    area.setSelectionRange(edit.selStart, edit.selEnd);
    area.dispatchEvent(new Event("input"));
  };
  $("report-section").querySelector(".md-toolbar").addEventListener("click", (e) => {
    const tool = TOOLS.find(([id]) => id === e.target.closest("[data-tool]")?.dataset.tool);
    if (tool) apply(tool[3](area.value, area.selectionStart, area.selectionEnd));
  });

  const addImage = async (file) => {
    if (!file) return;
    $("save-note").textContent = `Uploading ${file.name}\u2026`;
    try {
      const image = await api.uploadReportImage(volumeId, file);
      state.images.push(image);
      renderImageList();
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

  $("save-btn").addEventListener("click", async (e) => {
    e.target.disabled = true;
    showError("");
    try {
      state.report = await api.saveReport(volumeId, area.value);
      state.dirty = false;
      stopEditing();
    } catch (err) {
      showError(err.message);
      e.target.disabled = false;
    }
  });
  $("cancel-btn").addEventListener("click", () => { if (confirmLeave()) { state.dirty = false; stopEditing(); } });
  area.focus();
}

function renderImageList() {
  $("image-count").textContent = String(state.images.length);
  $("image-list").innerHTML = state.images.length
    ? state.images.map((im) => `
        <div class="image-row" data-image="${im.id}">
          <span class="mono">img:${im.id}</span>
          <span class="grow">${escapeHtml(im.filename)} <span class="dim">\u00b7 ${formatBytes(im.size)}</span></span>
          <button type="button" data-insert="${im.id}">Insert</button>
          <button type="button" data-remove="${im.id}">Delete</button>
        </div>`).join("")
    : `<p class="dim">No images yet. Use <strong>Image\u2026</strong>, or drop or paste one into the text.</p>`;
}

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
      renderImageList();
      area.dispatchEvent(new Event("input"));
    } catch (err) {
      showError(err.message);
    }
  }
});

function stopEditing() {
  state.editing = false;
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

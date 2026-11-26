// Turns the markdown text of a report into HTML (report.html shows it; the editor's preview uses the same code).
//
// What a report may contain: the usual markdown (headings, lists, tables, code, quotes, links), formulas written
// as $x^2$ (inline) or $$ ... $$ (on lines of their own) and images the editor uploaded, written as ![caption](img:12).
// Raw HTML is NOT taken: markdown-it is run with html: false, so a tag in the text is shown as text. Together with
// the checks below that is what keeps a report from running anything in the page of whoever reads it:
//   - links: only http(s), mailto, #anchors and paths of this site; they open in a new tab without a referrer
//   - images: only the uploaded ones (img:<id>); the browser fetches them with the reader's own sign-in (hydrate in
//     pages/report.js), an image from another site is shown as a link instead, so a report cannot make a reader's
//     browser contact a third party just by being opened
//   - formulas: KaTeX, whose output is made of plain elements; `trust` stays off (no \href, \includegraphics, ...)
import MarkdownIt from "markdown-it";
import katex from "katex";

const SAFE_LINK = /^(https?:\/\/|mailto:|#|\/(?!\/))/i;
const REPORT_IMAGE = /^img:(\d+)$/;

function renderMath(tex, displayMode) {
  try {
    return katex.renderToString(tex, { displayMode, throwOnError: false, strict: "ignore", trust: false, maxExpand: 1000 });
  } catch (_) {
    return null;
  }
}

const escapeHtml = (s) => s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

// $...$ inside a line. The same rules as the usual markdown-katex plugins, so "costs $5 and $6" stays text:
// the opening $ is followed by something that is not a space, the closing one is not preceded by a space and not
// followed by a digit.
function mathInline(state, silent) {
  const src = state.src;
  const start = state.pos;
  if (src.charCodeAt(start) !== 0x24 /* $ */) return false;
  const next = src.charCodeAt(start + 1);
  if (next === 0x24 || next === 0x20 || next === 0x09 || next === 0x0a || Number.isNaN(next)) return false;
  let end = start + 1;
  for (;;) {
    end = src.indexOf("$", end);
    if (end === -1) return false;
    let backslashes = 0;
    while (src.charCodeAt(end - 1 - backslashes) === 0x5c) backslashes++;
    if (backslashes % 2 === 1) { end++; continue; } // an escaped \$ does not close it
    break;
  }
  const before = src.charCodeAt(end - 1);
  const after = src.charCodeAt(end + 1);
  if (end === start + 1 || before === 0x20 || before === 0x09 || (after >= 0x30 && after <= 0x39)) return false;
  if (!silent) {
    const token = state.push("math_inline", "math", 0);
    token.content = src.slice(start + 1, end);
    token.markup = "$";
  }
  state.pos = end + 1;
  return true;
}

// $$ on a line of its own opens a formula that runs to the line that ends with $$ (or one line: $$ x $$).
function mathBlock(state, startLine, endLine, silent) {
  let pos = state.bMarks[startLine] + state.tShift[startLine];
  let max = state.eMarks[startLine];
  if (state.sCount[startLine] - state.blkIndent >= 4) return false;
  if (pos + 2 > max || state.src.slice(pos, pos + 2) !== "$$") return false;
  if (silent) return true;

  pos += 2;
  let firstLine = state.src.slice(pos, max);
  let lastLine = "";
  let found = false;
  let line = startLine;
  if (firstLine.trim().endsWith("$$")) {
    firstLine = firstLine.trim().slice(0, -2);
    found = true;
  }
  while (!found) {
    line++;
    if (line >= endLine) break;
    pos = state.bMarks[line] + state.tShift[line];
    max = state.eMarks[line];
    if (pos < max && state.tShift[line] < state.blkIndent) break;
    const text = state.src.slice(pos, max);
    if (text.trim().endsWith("$$")) {
      lastLine = text.trim().slice(0, -2);
      found = true;
    }
  }
  if (!found) return false; // never closed: it is not a formula, markdown goes on as usual
  state.line = line + 1;
  const token = state.push("math_block", "math", 0);
  token.block = true;
  const middle = state.getLines(startLine + 1, line, state.tShift[startLine], true);
  token.content = (firstLine.trim() ? firstLine + "\n" : "") + (line > startLine + 1 || !firstLine.trim() ? middle : "") + (lastLine.trim() ? lastLine : "");
  token.map = [startLine, state.line];
  token.markup = "$$";
  return true;
}

function mathPlugin(md) {
  md.inline.ruler.after("escape", "math_inline", mathInline);
  md.block.ruler.after("blockquote", "math_block", mathBlock, { alt: ["paragraph", "reference", "blockquote", "list"] });
  md.renderer.rules.math_inline = (tokens, i) => renderMath(tokens[i].content, false) ?? `<code>${escapeHtml(tokens[i].content)}</code>`;
  md.renderer.rules.math_block = (tokens, i) => {
    const html = renderMath(tokens[i].content.trim(), true);
    return html ? `<div class="math-block">${html}</div>\n` : `<pre><code>${escapeHtml(tokens[i].content)}</code></pre>\n`;
  };
}

function build() {
  const md = new MarkdownIt({ html: false, linkify: false, typographer: false, breaks: false });
  md.validateLink = (url) => SAFE_LINK.test(url.trim()) || REPORT_IMAGE.test(url.trim());
  md.use(mathPlugin);

  const defaultLinkOpen = md.renderer.rules.link_open || ((tokens, i, options, _env, self) => self.renderToken(tokens, i, options));
  md.renderer.rules.link_open = (tokens, i, options, env, self) => {
    const href = tokens[i].attrGet("href") || "";
    if (REPORT_IMAGE.test(href)) {
      tokens[i].attrs = tokens[i].attrs.filter(([name]) => name !== "href"); // img:<id> is for images only
    } else if (!href.startsWith("#")) {
      tokens[i].attrSet("target", "_blank");
      tokens[i].attrSet("rel", "noopener noreferrer nofollow");
    }
    return defaultLinkOpen(tokens, i, options, env, self);
  };

  md.renderer.rules.image = (tokens, i, options, env, self) => {
    const token = tokens[i];
    const src = token.attrGet("src") || "";
    const alt = self.renderInlineAsText(token.children, options, env);
    const m = REPORT_IMAGE.exec(src);
    if (m) {
      // no src yet: pages/report.js fetches the image with the reader's sign-in and sets it
      const title = token.attrGet("title");
      return `<img data-report-image="${m[1]}" alt="${escapeHtml(alt)}"${title ? ` title="${escapeHtml(title)}"` : ""} loading="lazy">`;
    }
    // an image from elsewhere is not loaded: it would tell that site who reads the report
    const label = escapeHtml(alt || src);
    return SAFE_LINK.test(src) && /^https?:/i.test(src)
      ? `<a href="${escapeHtml(src)}" target="_blank" rel="noopener noreferrer nofollow" title="External image (not shown)">[image: ${label}]</a>`
      : `<span title="Only images uploaded to the report are shown">[image: ${label}]</span>`;
  };
  return md;
}

let md = null;

// The HTML for `source` (a markdown string). Safe to put into the page with innerHTML.
export function renderMarkdown(source) {
  md = md || build();
  return md.render(String(source || ""));
}

// The ids of the uploaded images a text refers to (img:12), in order, without repeats.
export function imageIdsIn(source) {
  const ids = [];
  for (const m of String(source || "").matchAll(/\]\(\s*img:(\d+)[\s)]/g)) {
    const id = Number(m[1]);
    if (!ids.includes(id)) ids.push(id);
  }
  return ids;
}

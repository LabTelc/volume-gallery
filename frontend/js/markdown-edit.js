// Text edits of the report editor's toolbar, as pure functions on a string and a selection (the editor applies the
// result to its textarea): tests/markdown-edit.test.mjs. Each returns { value, selStart, selEnd }.

/** Puts `before` and `after` around the selection (or around `placeholder` when nothing is selected, which is
 *  then left selected so the person types over it). */
export function wrapSelection(value, start, end, before, after = before, placeholder = "text") {
  const picked = value.slice(start, end) || placeholder;
  const out = value.slice(0, start) + before + picked + after + value.slice(end);
  return { value: out, selStart: start + before.length, selEnd: start + before.length + picked.length };
}

/** Starts every line the selection touches with `prefix` (a string, or a function of the line's number from 0:
 *  "1. ", "2. ", ...). The same call on lines that already have it takes it off again. */
export function prefixLines(value, start, end, prefix) {
  const from = value.lastIndexOf("\n", start - 1) + 1;
  let to = value.indexOf("\n", end);
  if (to === -1) to = value.length;
  const lines = value.slice(from, to).split("\n");
  const make = typeof prefix === "function" ? prefix : () => prefix;
  const has = lines.every((line, i) => line.startsWith(make(i)));
  const out = lines.map((line, i) => (has ? line.slice(make(i).length) : make(i) + line)).join("\n");
  return { value: value.slice(0, from) + out + value.slice(to), selStart: from, selEnd: from + out.length };
}

/** Inserts `text` at `pos` (replacing the selection when start < end), with the caret after it. */
export function insertText(value, start, end, text) {
  return { value: value.slice(0, start) + text + value.slice(end), selStart: start + text.length, selEnd: start + text.length };
}

/** A display formula on lines of its own, with the caret between the $$ lines. */
export function insertDisplayMath(value, start, end) {
  const before = value.slice(0, start);
  const lead = before === "" || before.endsWith("\n\n") ? "" : before.endsWith("\n") ? "\n" : "\n\n";
  const text = `${lead}$$\n\\int_0^1 x\\,dx\n$$\n\n`;
  const out = before + text + value.slice(end);
  const caret = before.length + lead.length + 3;
  return { value: out, selStart: caret, selEnd: caret + "\\int_0^1 x\\,dx".length };
}

/** The markdown for an uploaded image: ![caption](img:<id>), the caption taken from the file name. */
export function imageMarkdown(id, filename) {
  const caption = String(filename || "image").replace(/\.[A-Za-z0-9]+$/, "").replace(/[\[\]\\]/g, "").replace(/[_-]+/g, " ").trim();
  return `![${caption || "image"}](img:${id})`;
}

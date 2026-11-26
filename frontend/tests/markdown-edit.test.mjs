import test from "node:test";
import assert from "node:assert/strict";
import { wrapSelection, prefixLines, insertText, insertDisplayMath, imageMarkdown } from "../js/markdown-edit.js";

test("wrapping a selection", () => {
  const r = wrapSelection("a bold word", 2, 6, "**");
  assert.equal(r.value, "a **bold** word");
  assert.equal(r.value.slice(r.selStart, r.selEnd), "bold");
});

test("wrapping nothing leaves the placeholder selected", () => {
  const r = wrapSelection("ab", 1, 1, "$", "$", "x");
  assert.equal(r.value, "a$x$b");
  assert.equal(r.value.slice(r.selStart, r.selEnd), "x");
});

test("a link keeps the chosen text as its label", () => {
  const r = wrapSelection("see docs", 4, 8, "[", "](https://)");
  assert.equal(r.value, "see [docs](https://)");
});

test("prefixing lines, and taking the prefix off again", () => {
  const added = prefixLines("one\ntwo\nthree", 2, 6, "- ");
  assert.equal(added.value, "- one\n- two\nthree");
  const removed = prefixLines(added.value, added.selStart, added.selEnd, "- ");
  assert.equal(removed.value, "one\ntwo\nthree");
});

test("numbered lines", () => {
  const r = prefixLines("a\nb", 0, 3, (i) => `${i + 1}. `);
  assert.equal(r.value, "1. a\n2. b");
  assert.equal(prefixLines(r.value, r.selStart, r.selEnd, (i) => `${i + 1}. `).value, "a\nb");
});

test("a heading on the line of the caret", () => {
  assert.equal(prefixLines("x\ntitle\ny", 4, 4, "## ").value, "x\n## title\ny");
});

test("inserting text replaces the selection", () => {
  const r = insertText("abcdef", 2, 4, "XY");
  assert.equal(r.value, "abXYef");
  assert.equal(r.selStart, 4);
});

test("a display formula gets blank lines around it", () => {
  assert.equal(insertDisplayMath("", 0, 0).value.startsWith("$$\n"), true);
  const r = insertDisplayMath("text", 4, 4);
  assert.match(r.value, /^text\n\n\$\$\n/);
  assert.equal(r.value.slice(r.selStart, r.selEnd), "\\int_0^1 x\\,dx");
});

test("image markdown", () => {
  assert.equal(imageMarkdown(7, "Section_A-B.png"), "![Section A B](img:7)");
  assert.equal(imageMarkdown(8, "[x].jpg"), "![x](img:8)");
  assert.equal(imageMarkdown(9, ""), "![image](img:9)");
});

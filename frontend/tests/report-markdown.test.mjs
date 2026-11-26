import test from "node:test";
import assert from "node:assert/strict";
import { renderMarkdown, imageIdsIn } from "../js/report-markdown.js";

test("ordinary markdown", () => {
  const html = renderMarkdown("# Title\n\nSome **bold** text and a [link](https://example.org/a).\n\n- one\n- two\n");
  assert.match(html, /<h1>Title<\/h1>/);
  assert.match(html, /<strong>bold<\/strong>/);
  assert.match(html, /<a href="https:\/\/example\.org\/a" target="_blank" rel="noopener noreferrer nofollow">link<\/a>/);
  assert.match(html, /<li>two<\/li>/);
});

test("tables", () => {
  const html = renderMarkdown("| a | b |\n|---|---|\n| 1 | 2 |\n");
  assert.match(html, /<table>/);
  assert.match(html, /<td>2<\/td>/);
});

test("raw HTML is shown as text, never run", () => {
  const html = renderMarkdown('<script>alert(1)</script>\n\n<img src=x onerror=alert(1)>\n\n<b onclick="x()">b</b>');
  assert.doesNotMatch(html, /<script/i);
  assert.doesNotMatch(html, /<img/i);
  assert.doesNotMatch(html, /<b /i);
  assert.match(html, /&lt;script&gt;/);
});

test("only safe links are links", () => {
  for (const bad of ["javascript:alert(1)", "JaVaScRiPt:alert(1)", "data:text/html,<script>1</script>", "vbscript:x", "//evil.example/x", "file:///etc/passwd"]) {
    const html = renderMarkdown(`[x](${bad})`);
    assert.doesNotMatch(html, /<a /, bad);
  }
  assert.match(renderMarkdown("[x](mailto:a@b.cz)"), /<a href="mailto:a@b\.cz"/);
  assert.match(renderMarkdown("[x](/index.html)"), /<a href="\/index\.html"/);
  const anchor = renderMarkdown("[x](#top)");
  assert.match(anchor, /<a href="#top">/);
  assert.doesNotMatch(anchor, /target=/);
});

test("uploaded images are marked for the page to fill in", () => {
  const html = renderMarkdown('![A "plot"](img:12 "Fig. 1")');
  assert.match(html, /<img data-report-image="12" alt="A &quot;plot&quot;" title="Fig\. 1" loading="lazy">/);
  assert.doesNotMatch(html, / src=/);
});

test("img:<id> is not a link target", () => {
  assert.doesNotMatch(renderMarkdown("[x](img:5)"), /href=/);
});

test("images from elsewhere are not loaded", () => {
  const html = renderMarkdown("![tracker](https://evil.example/p.png)");
  assert.doesNotMatch(html, /<img/);
  assert.match(html, /\[image: tracker\]/);
  assert.doesNotMatch(renderMarkdown("![x](data:image/png;base64,AAAA)"), /<img/);
  assert.doesNotMatch(renderMarkdown("![x](javascript:alert(1))"), /<a |<img/);
});

test("inline formulas", () => {
  const html = renderMarkdown("Energy $E = mc^2$ and $\\alpha$.");
  assert.equal((html.match(/class="katex"/g) || []).length, 2);
  assert.doesNotMatch(html, /\$/);
});

test("display formulas, on one line and on several", () => {
  const one = renderMarkdown("$$ x^2 + y^2 = z^2 $$");
  assert.match(one, /<div class="math-block"><span class="katex-display">/);
  const many = renderMarkdown("Before\n\n$$\n\\int_0^1 x\\,dx\n= \\tfrac12\n$$\n\nAfter");
  assert.match(many, /katex-display/);
  assert.match(many, /<p>Before<\/p>/);
  assert.match(many, /<p>After<\/p>/);
});

test("money is not math", () => {
  const html = renderMarkdown("It costs $5 and then $6, about 5 dollars.");
  assert.doesNotMatch(html, /katex/);
  assert.match(html, /\$5 and then \$6/);
});

test("an escaped dollar and dollars in code stay text", () => {
  assert.doesNotMatch(renderMarkdown("a \\$x\\$ b"), /katex/);
  assert.doesNotMatch(renderMarkdown("`$x$`"), /katex/);
  assert.doesNotMatch(renderMarkdown("```\n$$\nx\n$$\n```"), /katex/);
});

test("an unclosed $$ is plain text", () => {
  const html = renderMarkdown("$$\nnever closed\n\nnext paragraph");
  assert.doesNotMatch(html, /katex/);
  assert.match(html, /next paragraph/);
});

test("a broken formula does not break the page and cannot run a macro that reaches out", () => {
  const html = renderMarkdown("$\\frac{1}{$ and $\\href{javascript:alert(1)}{x}$ and $\\includegraphics{http://evil/x.png}$");
  assert.doesNotMatch(html, /<a /);
  assert.doesNotMatch(html, /<img/);
  assert.doesNotMatch(html, /href=/);
});

test("image ids are listed once, in order", () => {
  assert.deepEqual(imageIdsIn("![a](img:3) text ![b](img:1) ![c](img:3 \"t\") ![d](https://x/y.png)"), [3, 1]);
  assert.deepEqual(imageIdsIn(""), []);
  assert.deepEqual(imageIdsIn(null), []);
});

// Regression tests for the DOM-XSS hardening: share-link fragment validation, safeUrl and escapeHtml.
import test from "node:test";
import assert from "node:assert/strict";

const store = () => { const m = new Map(); return { getItem: (k) => m.get(k) ?? null, setItem: (k, v) => m.set(k, String(v)), removeItem: (k) => m.delete(k) }; };

async function loadApi(hash) {
  globalThis.localStorage = store();
  globalThis.sessionStorage = store();
  globalThis.history = { replaceState() {} };
  globalThis.window = { location: { hash, pathname: "/index.html", search: "", href: "https://gallery.example/index.html" } };
  return import(`../js/api.js?${Math.random()}`); // fresh module: the fragment is consumed on load
}

test("a share fragment with markup is not stored (no DOM XSS through the signed-in name)", async () => {
  const api = await loadApi("#vg_st=aaa.bbb.ccc&vg_sc=%3Cimg%20src%3Dx%20onerror%3Dalert(1)%3E");
  assert.equal(api.getUsername(), "guest");
  assert.equal(sessionStorage.getItem("vg_share_token"), "aaa.bbb.ccc");
});

test("a valid share code and token are kept", async () => {
  const api = await loadApi("#vg_st=aB-_1.cD-_2.eF-_3&vg_sc=Abc_123-x");
  assert.equal(api.getUsername(), "shared:Abc_123-x");
  assert.equal(sessionStorage.getItem("vg_share_token"), "aB-_1.cD-_2.eF-_3");
});

test("a token that is not a JWT shape is dropped", async () => {
  await loadApi('#vg_st="><script>x</script>&vg_sc=ok123456');
  assert.equal(sessionStorage.getItem("vg_share_token"), null);
});

test("safeUrl only lets http(s) through (mailto on request)", async () => {
  const { safeUrl } = await loadApi("");
  assert.equal(safeUrl("https://a.example/x"), "https://a.example/x");
  assert.equal(safeUrl("/rel/path"), "https://gallery.example/rel/path");
  assert.equal(safeUrl("javascript:alert(1)"), "");
  assert.equal(safeUrl("data:text/html,<script>1</script>"), "");
  assert.equal(safeUrl("mailto:a@b.c"), "");
  assert.equal(safeUrl("mailto:a@b.c", { mailto: true }), "mailto:a@b.c");
  assert.equal(safeUrl(null), "https://gallery.example/index.html");
});

test("escapeHtml escapes quotes so attribute values cannot be broken out of", async () => {
  const { escapeHtml } = await loadApi("");
  assert.equal(escapeHtml(`" onfocus="x' <b>&`), "&quot; onfocus=&quot;x&#39; &lt;b&gt;&amp;");
  assert.equal(escapeHtml(undefined), "");
});

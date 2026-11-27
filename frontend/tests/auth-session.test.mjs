// The sign-in is an HttpOnly session cookie, so the frontend never sees a credential: what request() sends, what a 401
// does, how pages find out who is signed in, and that nothing reads or writes a login token any more.
import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const store = () => { const m = new Map(); return { getItem: (k) => m.get(k) ?? null, setItem: (k, v) => m.set(k, String(v)), removeItem: (k) => m.delete(k), keys: () => [...m.keys()] }; };

const setNavigator = (value) => Object.defineProperty(globalThis, "navigator", { value, configurable: true, writable: true });

// A fresh api.js with a fake browser. `server(url, init)` answers every fetch: {status, body}.
async function load({ hash = "", pathname = "/index.html", search = "", server }) {
  const calls = [];
  globalThis.localStorage = store();
  globalThis.sessionStorage = store();
  globalThis.history = { replaceState() {} };
  setNavigator({});
  globalThis.window = { location: { hash, pathname, search, href: `https://gallery.example${pathname}${search}` } };
  globalThis.fetch = async (url, init = {}) => {
    calls.push({ url: String(url), init });
    const { status = 200, body = {} } = await server(String(url), init);
    return {
      status, ok: status >= 200 && status < 300, statusText: String(status),
      headers: { get: (n) => (n.toLowerCase() === "content-type" ? "application/json" : null) },
      json: async () => body, blob: async () => ({}),
    };
  };
  const mod = await import(`../js/api.js?${Math.random()}`);
  return { ...mod, calls, window: globalThis.window };
}

const ME = { username: "erin", role: "editor" };
const signedIn = (url) => (url === "/api/auth/me" ? { body: ME } : { body: {} });
const signedOut = (url) => (url === "/api/auth/me" ? { status: 401, body: { detail: "Not authenticated" } } : { status: 401, body: {} });
const headerOf = (call, name) => Object.entries(call.init.headers || {}).find(([k]) => k.toLowerCase() === name.toLowerCase())?.[1];

test("request() adds the CSRF header on state-changing calls only, and never an Authorization header for a signed-in user", async () => {
  const { api, requireLogin, calls } = await load({ server: signedIn });
  await requireLogin();
  await api.listUsers();
  await api.deleteUser(3);
  await api.updateUser(3, { role: "reader" });
  await api.createUser({ username: "x" });
  const [, list, del, patch, post] = calls;
  assert.equal(headerOf(list, "X-Requested-With"), undefined);
  for (const call of [del, patch, post]) {
    assert.equal(headerOf(call, "X-Requested-With"), "vg", call.url);
    assert.equal(call.init.credentials, "same-origin");
  }
  for (const call of calls) assert.equal(headerOf(call, "Authorization"), undefined, call.url);
});

test("login and the other unauthenticated POSTs carry the CSRF header and use the cookie", async () => {
  const { api, calls } = await load({ server: () => ({ body: ME }) });
  await api.login("erin", "pw", true);
  await api.checkPasswordLink("tok");
  for (const call of calls) {
    assert.equal(call.init.method, "POST");
    assert.equal(headerOf(call, "X-Requested-With"), "vg");
    assert.equal(call.init.credentials, "same-origin");
  }
  assert.match(String(calls[0].init.body), /remember=true/);
});

test("who is signed in comes from /api/auth/me, not from anything the page can decode", async () => {
  const { requireLogin, getRole, getUsername } = await load({ server: signedIn });
  assert.equal(getRole(), null);
  const me = await requireLogin();
  assert.deepEqual(me, ME);
  assert.equal(getRole(), "editor");
  assert.equal(getUsername(), "erin");
});

test("requireLogin() sends nobody to a page without checking: 401 goes to the login with ?next=", async () => {
  const { requireLogin, window } = await load({ server: signedOut, pathname: "/viewers/volume.html", search: "?id=7", hash: "#x" });
  await assert.rejects(requireLogin(), /redirecting to the login page/);
  assert.equal(window.location.href, "/login.html?next=" + encodeURIComponent("/viewers/volume.html?id=7#x"));
});

test("a 401 in the middle of a session clears the user and redirects with next", async () => {
  let ok = true;
  const { api, requireLogin, getRole, window } = await load({
    server: (url) => (url === "/api/auth/me" || ok ? { body: ME } : { status: 401, body: {} }),
    pathname: "/manage.html", search: "?id=2",
  });
  await requireLogin();
  assert.equal(getRole(), "editor");
  ok = false; // e.g. signed out in another tab
  await assert.rejects(api.listUsers(), /Not authenticated/);
  assert.equal(getRole(), null);
  assert.equal(window.location.href, "/login.html?next=" + encodeURIComponent("/manage.html?id=2"));
});

test("a guest share session: bearer header, role guest, no CSRF header on reads; a real sign-in replaces it", async () => {
  const hash = "#vg_st=aB-_1.cD-_2.eF-_3&vg_sc=Abc_123-x";
  const guest = await load({ hash, pathname: "/viewers/volume.html", server: signedOut });
  assert.equal(await guest.requireLogin(), null); // passes as a guest
  assert.equal(guest.getRole(), "guest");
  assert.equal(guest.isGuestSession(), true);
  await assert.rejects(guest.api.listUsers()); // (the fake server says 401 here)
  const read = guest.calls.find((c) => c.url === "/api/users");
  assert.equal(headerOf(read, "Authorization"), "Bearer aB-_1.cD-_2.eF-_3");
  assert.equal(headerOf(guest.calls[0], "Authorization"), undefined); // the /me probe never carries it

  const real = await load({ hash, pathname: "/viewers/volume.html", server: signedIn });
  await real.requireLogin();
  assert.equal(real.getRole(), "editor");
  assert.equal(real.isGuestSession(), false);
  assert.equal(sessionStorage.getItem("vg_share_token"), null); // dropped
  await real.api.listUsers();
  assert.equal(headerOf(real.calls.at(-1), "Authorization"), undefined); // never both
});

test("signOut() asks the server to end the session and clears the caches", async () => {
  const posted = [];
  const { signOut, requireLogin, getRole, calls } = await load({ server: signedIn });
  setNavigator({ serviceWorker: { controller: { postMessage: (m) => posted.push(m) } } });
  await requireLogin();
  await signOut();
  const logout = calls.find((c) => c.url === "/api/auth/logout");
  assert.equal(logout.init.method, "POST");
  assert.equal(headerOf(logout, "X-Requested-With"), "vg");
  assert.equal(getRole(), null);
  assert.deepEqual(posted, [{ type: "CLEAR_BRICK_CACHE" }]);
});

test("plain image URLs for a signed-in user, fetched blobs only for a guest", async () => {
  const user = await load({ server: signedIn });
  await user.requireLogin();
  assert.equal(await user.api.thumbnailUrl("volume", 4), "/api/volumes/4/thumbnail");
  assert.equal(await user.api.reportImageUrl(4, 9), "/api/volumes/4/report/images/9");
});

// ---- nothing reads or writes a login credential any more
const root = path.join(path.dirname(fileURLToPath(import.meta.url)), "..");
const sources = ["js", "pages", "viewers"].flatMap((dir) => {
  const walk = (d) => fs.readdirSync(d, { withFileTypes: true }).flatMap((e) =>
    e.isDirectory() ? walk(path.join(d, e.name)) : /\.(js|html)$/.test(e.name) ? [path.join(d, e.name)] : []);
  return walk(path.join(root, dir));
}).concat(fs.readdirSync(root).filter((f) => f.endsWith(".html")).map((f) => path.join(root, f)));

test("no source mentions the old session token", () => {
  for (const file of sources) {
    const text = fs.readFileSync(file, "utf8");
    for (const gone of ["vg_token", "getToken(", "setSession(", "runtimeToken", "_jwtPayload", "_storedToken"]) {
      assert.ok(!text.includes(gone), `${path.relative(root, file)} still mentions ${gone}`);
    }
    assert.ok(!/(?<![A-Z_])TOKEN_KEY/.test(text), `${path.relative(root, file)} still has the old TOKEN_KEY`);
  }
});

test("the only credential in sessionStorage is the guest share token; no token is put in localStorage", () => {
  const api = fs.readFileSync(path.join(root, "js", "api.js"), "utf8");
  const keys = [...api.matchAll(/^const (\w+_KEY) = "([^"]+)"/gm)].map((m) => m[2]).sort();
  assert.deepEqual(keys, ["vg_branding", "vg_rail_collapsed", "vg_share_code", "vg_share_token", "vg_theme"]); // (UI state, and the guest share token)
  assert.ok(!/atob\(/.test(api), "api.js must not decode tokens");
  for (const file of sources) {
    const text = fs.readFileSync(file, "utf8");
    assert.ok(!/localStorage\.setItem\([^)]*(token|session|jwt)/i.test(text), `${file} stores a credential in localStorage`);
    if (!file.endsWith(`${path.sep}api.js`)) assert.ok(!/["']Authorization["']|Authorization:/.test(text), `${file} sets an Authorization header`);
  }
});

test("every page that needs a sign-in waits for it before drawing", () => {
  for (const f of fs.readdirSync(path.join(root, "pages"))) {
    const text = fs.readFileSync(path.join(root, "pages", f), "utf8");
    if (/\brequireLogin\(/.test(text)) assert.match(text, /^\s*await requireLogin\(\);/m, `pages/${f}`);
  }
});

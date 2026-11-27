# Authentication

How people (and scripts) sign in, how the server tells who is asking, and what the reverse proxy has to do.

## Browser sign-in: a session cookie

`POST /api/auth/login` (and the set-password link, `POST /api/auth/set-password`, and changing your own password)
answer with a session cookie:

```
Set-Cookie: __Host-vg_session=<random id>; Path=/; Secure; HttpOnly; SameSite=Lax
```

* **HttpOnly**: scripts cannot read it (`document.cookie` does not show it), so an XSS cannot steal the sign-in.
* **Secure** + the **`__Host-`** prefix: the browser only sends it over https, only to the exact host that set it, and
  refuses it with a `Domain` attribute or a path other than `/`.
* **SameSite=Lax**: not sent on cross-site POST / PUT / DELETE (see CSRF below).
* No `Max-Age`: a *session cookie* that the browser keeps for as long as the browser runs, in **every tab and window**
  (and across a restored session). "Keep me signed in" on the login page adds `Max-Age=30 days` (`VG_REMEMBER_DAYS`).
* Local development over plain http: set `VG_COOKIE_SECURE=0`. The cookie is then called `vg_session` and has no
  `Secure` flag (browsers refuse `Secure` and `__Host-` cookies on http). Never do this on a real deployment.

### Server-side sessions (decision: opaque id, not the JWT)

The cookie holds a random 256-bit id. The server keeps a row per sign-in (`usersession`: SHA-256 of the id, user,
created / last seen / absolute expiry, "remember", a fingerprint of the password it was started with). A copy of the
database therefore holds nothing that can be put in a cookie.

*Why not the existing signed JWT in the cookie?* A JWT cannot be taken back: logout would only delete the browser's copy,
and a disabled account, a changed role or a stolen cookie would live until `exp`. The database is already there, so a
row per sign-in gives real logout and revocation for the price of one indexed lookup per request. The role is **not**
stored in the session: it is read from the user's row on every request.

A session ends when
* the user signs out (`POST /api/auth/logout`: the row is deleted and the cookie expired with the same attributes);
* it is idle for `VG_SESSION_IDLE_MINUTES` (default 480) or older than `VG_SESSION_ABSOLUTE_HOURS` (default 24); for
  "keep me signed in": idle `VG_REMEMBER_IDLE_DAYS` (7) / absolute `VG_REMEMBER_DAYS` (30). These are checked on the
  server whatever the cookie says;
* the account is disabled or deleted, its **role is changed**, or its **password is changed or reset** (every session of
  the user is deleted; the one that changed its own password gets a fresh cookie, so that browser stays signed in).
  A session is also bound to the password it was started with, so a password changed any other way ends it too;
* the user signs in again in that browser: the session id changes at every sign-in (no session fixation).

The signed dataset URLs that the service worker caches bricks for (`/api/volumes/{id}/dataset/{token}/...`) carry the id
of the session that minted them: when the session ends they stop working too, which is what the worker's re-check
asks the server about.

### `GET /api/auth/me`

Answers `{"username": "...", "role": "..."}` (a guest share session: role `guest`), or 401. It is the only way
the frontend learns who is signed in: it cannot read the cookie and no longer decodes a JWT. The answer is
`Cache-Control: no-store`, like everything under `/api/auth/`.

## CSRF

The cookie is sent automatically, so state-changing requests are checked in two ways besides `SameSite=Lax`
(`backend/app/web_security.py`). Every `POST`, `PUT`, `PATCH` and `DELETE` to `/api/` must

1. carry `X-Requested-With: vg` (browsers do not let another site add a custom header without a CORS preflight, and the
   app never answers one: there is **no CORS** and none may be added), and
2. if it names an origin (`Origin`, else `Referer`), that origin must be the one the request was made to (`Host` with
   `X-Forwarded-Proto`, default ports left out) or one of `VG_ALLOWED_ORIGINS`. `Origin: null` is refused.

Otherwise the answer is `403` and nothing happens. `GET` endpoints never change state. (One deliberate exception:
`/s/<code>` counts the view of a share link. It has no authority: it only mints the link's own scoped token.)

## Security headers

On every response: `X-Content-Type-Options: nosniff`, `Referrer-Policy: same-origin`, a `Content-Security-Policy`:

```
default-src 'self'; script-src 'self' 'wasm-unsafe-eval' 'sha256-<theme snippet>'; style-src 'self' 'unsafe-inline';
img-src 'self' data: blob:; font-src 'self' data:; connect-src 'self' blob: data:; media-src 'self' blob:;
worker-src 'self' blob:; object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'
```

The only inline script is the tiny theme snippet (`js/theme-init.js`, put into every page by webpack); its hash is
computed from the pages that are served, so it follows the snippet. Inline *styles* are still used by the pages. If a
viewer misbehaves after an upgrade, set `VG_CSP=report-only`: nothing is blocked and the browser console lists what would
have been. `Strict-Transport-Security` is sent when `VG_HSTS_MAX_AGE` is set (see [configuration.md](configuration.md);
not with a self-signed certificate: browsers do not let people click through an HSTS host's certificate error).
`X-Frame-Options: DENY`, `Permissions-Policy` and the cross-origin isolation headers come from nginx.

## Sign-in lockout

Failed sign-ins are counted from the audit log over `VG_LOGIN_WINDOW_MINUTES` (15): 5 for one name from one address
(cleared by a successful sign-in from it), 30 from one address, 50 for one name from any address. Beyond that the answer
is `429` (with `Retry-After`) even for the right password, and `auth.login_blocked` is logged. The counts use a keyed hash
of the name that was tried, so an unknown name is locked out exactly like a real one and the name itself is not stored.
The address is `X-Real-IP`, which nginx sets (without nginx anyone can send it: the per-name limit still holds).
nginx additionally limits the request rate of the login endpoint.

## Guest share links

`/s/<code>` redirects to the viewer with `#vg_st=<token>&vg_sc=<code>` in the fragment. That scoped, read-only token
is kept in `sessionStorage` (`vg_share_token`, `vg_share_code`) and sent as `Authorization: Bearer` on viewer, report and
data requests. It is a *guest* (role `guest`): the editor and admin endpoints refuse it. If a valid session cookie is also
present the cookie wins, and the frontend does not send the token once someone is really signed in.

## API / CLI clients

Bearer tokens for normal users are **off by default** (`VG_ALLOW_BEARER_AUTH=0`). Set `VG_ALLOW_BEARER_AUTH=1` while scripts
still use them: the login then also returns `access_token` (valid `VG_ACCESS_TOKEN_MINUTES`) and `Authorization: Bearer <token>`
is accepted for normal users. With `0` only the cookie works, and a script has to keep the
cookie: `curl -c jar -b jar -H 'X-Requested-With: vg' -d username=... -d password=... https://host/api/auth/login`.
Scripts must send `X-Requested-With: vg` on state-changing calls either way.

## Reverse proxy requirements

The proxy in `nginx/nginx.conf.template` already does all of this; any other proxy has to:

* pass `Set-Cookie` and `Cookie` through unchanged (do not strip, rewrite the domain or add a `Domain`);
* pass `Host` with the name and port the browser used (`proxy_set_header Host $host:$server_port`), and set
  `X-Forwarded-Proto https` on https requests. The `Secure` cookie and the origin check depend on them. With a different
  public origin than `Host` shows, list it in `VG_ALLOWED_ORIGINS`;
* set `X-Real-IP` to the client address (and only the proxy may: the app is not reachable except through it);
* not cache `/api/` responses.

## Upgrading (deploy order)

1. Deploy the **backend** (the commit "backend: HttpOnly session cookie auth ...") with `VG_ALLOW_BEARER_AUTH=1`: it
   accepts the cookie and, still, the old bearer tokens, so the old frontend keeps working. The new `usersession` table is
   created at start-up.
2. Deploy the **frontend**. Everybody has to sign in once (their old tab-bound token is not a cookie).
3. Deploy the last commit ("stop accepting bearer tokens by default"), or just set `VG_ALLOW_BEARER_AUTH=0`, unless scripts
   still use bearer tokens.

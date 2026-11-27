"""CSRF defence and security headers for every response. Pure ASGI (not BaseHTTPMiddleware), so that the large
streamed downloads are not buffered or delayed.

CSRF. The session cookie is SameSite=Lax, which already keeps browsers from sending it on cross-site POST / PUT /
PATCH / DELETE. As defence in depth every state-changing request to /api/ (any credential, or none) must also
 - carry `X-Requested-With: vg` (a header a cross-site page cannot add without a CORS preflight, which this app never
   answers: there is no CORS middleware and none must be added), and
 - if it names an origin (Origin, else Referer), that origin must be the one the request was made to (what nginx
   forwards in Host / X-Forwarded-Proto) or one of VG_ALLOWED_ORIGINS. `Origin: null` is refused.
Requests that name no origin at all (curl, scripts) are decided by the header alone. GET / HEAD / OPTIONS are never
checked: no GET endpoint changes state (the one exception is the view counter of /s/<code>, which has no authority).

Headers. nosniff, Referrer-Policy, a Content-Security-Policy, no-store on /api/auth/*, and (VG_HSTS_MAX_AGE) HSTS.
"""
import base64
import functools
import hashlib
import re
from pathlib import Path
from typing import Iterable, List, Optional, Set
from urllib.parse import urlsplit

from starlette.responses import JSONResponse

from .config import settings

CSRF_HEADER = "x-requested-with"
CSRF_VALUE = "vg"
_UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_DEFAULT_PORT = {"http": 80, "https": 443}


def _header(scope, name: str) -> Optional[str]:
    key = name.encode("latin-1")
    for k, v in scope.get("headers", []):
        if k == key:
            return v.decode("latin-1")
    return None


def _origin_of(scheme: str, host: str) -> Optional[str]:
    """scheme://host[:port] with the port of the scheme left out, lower case; None when it does not parse."""
    try:
        parts = urlsplit(f"//{host}")
        name, port = parts.hostname, parts.port
    except ValueError:
        return None
    scheme = (scheme or "").lower()
    if not name or scheme not in _DEFAULT_PORT:
        return None
    if ":" in name:
        name = f"[{name}]"
    suffix = "" if port is None or port == _DEFAULT_PORT[scheme] else f":{port}"
    return f"{scheme}://{name}{suffix}"


def _origin_of_url(url: str) -> Optional[str]:
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if not parts.scheme or not parts.netloc:
        return None
    return _origin_of(parts.scheme, parts.netloc)


def _first(value: Optional[str]) -> str:
    return (value or "").split(",")[0].strip()


def effective_scheme(scope) -> str:
    return (_first(_header(scope, "x-forwarded-proto")) or scope.get("scheme") or "http").lower()


def own_origins(scope) -> Set[str]:
    """The origin(s) a request to this app may come from: the one it was made to (behind nginx: Host with
    X-Forwarded-Proto) and VG_ALLOWED_ORIGINS."""
    origins: Set[str] = set()
    host = _first(_header(scope, "x-forwarded-host")) or _header(scope, "host") or ""
    own = _origin_of(effective_scheme(scope), host)
    if own:
        origins.add(own)
    for extra in settings.ALLOWED_ORIGINS.split(","):
        extra = extra.strip()
        if extra:
            normalised = _origin_of_url(extra)
            if normalised:
                origins.add(normalised)
    return origins


def csrf_error(scope) -> Optional[str]:
    """Why a request must be refused (403), or None."""
    if scope.get("method") not in _UNSAFE_METHODS or not scope.get("path", "").startswith("/api/"):
        return None
    if _header(scope, CSRF_HEADER) != CSRF_VALUE:
        return f"Missing or wrong {CSRF_HEADER.title()} header"
    named = _header(scope, "origin")
    if named is not None:
        origin = _origin_of_url(named) if named != "null" else None
    else:
        referer = _header(scope, "referer")
        if referer is None:
            return None
        named, origin = referer, _origin_of_url(referer)
    if origin is None or origin not in own_origins(scope):
        return "Cross-origin request refused"
    return None


# ----------------------------------------------------------------------------- Content-Security-Policy

_INLINE_SCRIPT = re.compile(r"<script(?P<attrs>[^>]*)>(?P<body>.*?)</script>", re.IGNORECASE | re.DOTALL)


def _inline_script_hashes(pages: Iterable[Path]) -> List[str]:
    hashes: List[str] = []
    for page in pages:
        try:
            html = page.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        for match in _INLINE_SCRIPT.finditer(html):
            body = match.group("body")
            if re.search(r"\bsrc\s*=", match.group("attrs"), re.IGNORECASE) or not body.strip():
                continue
            digest = base64.b64encode(hashlib.sha256(body.encode("utf-8")).digest()).decode("ascii")
            token = f"'sha256-{digest}'"
            if token not in hashes:
                hashes.append(token)
    return hashes


@functools.lru_cache(maxsize=4)
def csp_value(frontend_dir: str) -> str:
    """The policy. The only inline script the build needs is the theme snippet (js/theme-init.js, put into every page
    by webpack): it is allowed by its hash, computed from the pages that are actually served, so it stays right
    whenever the snippet changes. Everything else is 'self'. (Inline <style> and style="" are still used by the pages,
    hence 'unsafe-inline' for styles only.) The result is cached until the process restarts."""
    root = Path(frontend_dir)
    pages = sorted(root.glob("*.html")) + sorted((root / "viewers").glob("*.html"))
    script_src = ["'self'", "'wasm-unsafe-eval'", *_inline_script_hashes(pages)]
    return "; ".join([
        "default-src 'self'",
        "script-src " + " ".join(script_src),
        "style-src 'self' 'unsafe-inline'",
        "img-src 'self' data: blob:",
        "font-src 'self' data:",
        "connect-src 'self' blob: data:",
        "media-src 'self' blob:",
        "worker-src 'self' blob:",
        "object-src 'none'",
        "base-uri 'self'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    ])


def _extra_headers(scope) -> List[tuple]:
    headers = [("x-content-type-options", "nosniff"), ("referrer-policy", "same-origin")]
    mode = settings.CSP_MODE
    if mode in ("enforce", "report-only"):
        name = "content-security-policy" if mode == "enforce" else "content-security-policy-report-only"
        headers.append((name, csp_value(settings.FRONTEND_DIR)))
    if scope.get("path", "").startswith("/api/auth/"):
        headers.append(("cache-control", "no-store"))
        headers.append(("pragma", "no-cache"))
    if settings.HSTS_MAX_AGE > 0 and effective_scheme(scope) == "https":
        headers.append(("strict-transport-security", f"max-age={settings.HSTS_MAX_AGE}"))
    return headers


class SecurityMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                present = {k.lower() for k, _ in headers}
                for name, value in _extra_headers(scope):
                    key = name.encode("latin-1")
                    if name in ("cache-control", "pragma"):  # (the auth endpoints: replaces what the route set)
                        headers = [(k, v) for k, v in headers if k.lower() != key]
                    elif key in present:
                        continue
                    headers.append((key, value.encode("latin-1")))
                message["headers"] = headers
            await send(message)

        error = csrf_error(scope)
        if error:
            await JSONResponse({"detail": error}, status_code=403)(scope, receive, send_with_headers)
            return
        await self.app(scope, receive, send_with_headers)

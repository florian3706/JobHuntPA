"""Security headers on every response (accounts design, Phase L), and the middleware order that keeps them there.

``SecurityHeaders`` adds each header the response doesn't already have, so a route can ask for a stricter one (the
digest's unsubscribe page sets its own ``Content-Security-Policy`` and ``Referrer-Policy: no-referrer``). It runs for
pages, static files, ``/api``, ``/unsubscribe`` and every error answer, the Host allowlist's included.

The Content-Security-Policy is built at start-up from the pages in frontend/, so editing a page can't silently break
it, and the test in tests/test_security_headers.py fails if a page needs more than the policy allows:

* ``script-src``: this site's own files, a ``sha256`` hash for each inline ``<script>`` in the pages (the theme
  script that runs before first paint), and Leaflet from unpkg.com (the exact file, version-pinned).
* ``style-src``: this site's own files, Leaflet's stylesheet (the exact file) and Google Fonts' stylesheet (the
  dyslexia themes load a typeface). No ``'unsafe-inline'``: no page has an inline ``<style>`` or a ``style=""``
  attribute, in its markup or in markup that scripts build (a colour slot is a ``hue-*`` class, see styles.css).
  Scripts set sizes and positions through ``element.style``, which a policy doesn't restrict.
* ``img-src``: this site, ``data:`` (Leaflet's stylesheet embeds a few images), the OpenStreetMap tile servers the
  map draws from, and Leaflet's own images folder on unpkg.com (the default marker).
* ``font-src``: Google Fonts' file server, which the stylesheet above points at.
* ``connect-src 'self'``: the pages only call this server's API (Stripe Checkout is a redirect, not a request).
* ``form-action 'self'``: no form posts elsewhere, Stripe Checkout included.
* ``frame-ancestors 'none'`` (nobody may frame the app: clickjacking), ``base-uri 'none'``, ``object-src 'none'``
  and ``frame-src 'none'`` (the app uses no plug-ins and shows no frames).

Other headers: ``X-Content-Type-Options: nosniff``; ``Referrer-Policy: same-origin``, not ``no-referrer``, because
with no-referrer browsers send ``Origin: null`` on same-site POSTs and the app's same-site check (backend/sessions.py)
would refuse every save, while same-origin still keeps the address of a page (it can hold a job id) from other sites
when someone follows an ad's link (the map tiles are the one exception: OpenStreetMap's servers refuse a request that
doesn't say which site it is for, so the two tile layers in app.js and job.js set ``referrerPolicy: 'origin'``, which
sends this site's address and never the page's); a ``Permissions-Policy`` that turns off every browser feature the app doesn't use
(it copies text, so ``clipboard-write`` stays); ``Strict-Transport-Security`` only over HTTPS (``sessions.is_secure``),
because on plain http it would be ignored and on a local install it could only cause harm.
"""
from __future__ import annotations

import base64
import hashlib
import re
from pathlib import Path

from starlette.datastructures import MutableHeaders

from backend import sessions
from backend.hosts import HostGuard

# What the pages load from other sites. tests/test_security_headers.py checks every page against these.
LEAFLET = "https://unpkg.com/leaflet@1.9.4/dist"
SCRIPT_SOURCES = (f"{LEAFLET}/leaflet.js",)
STYLE_SOURCES = (f"{LEAFLET}/leaflet.css", "https://fonts.googleapis.com")
IMG_SOURCES = ("data:", f"{LEAFLET}/images/", "https://*.tile.openstreetmap.org")  # a, b and c servers
FONT_SOURCES = ("https://fonts.gstatic.com",)

# Everything the app doesn't use, off. clipboard-write is the copy buttons.
PERMISSIONS_POLICY = ", ".join([
    "accelerometer=()", "autoplay=()", "browsing-topics=()", "camera=()", "clipboard-read=()",
    "clipboard-write=(self)", "display-capture=()", "encrypted-media=()", "fullscreen=()", "geolocation=()",
    "gyroscope=()", "hid=()", "magnetometer=()", "microphone=()", "midi=()", "payment=()", "picture-in-picture=()",
    "publickey-credentials-get=()", "screen-wake-lock=()", "serial=()", "usb=()", "xr-spatial-tracking=()",
])

STATIC_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "Permissions-Policy": PERMISSIONS_POLICY,
}
HSTS = "max-age=31536000"

_INLINE_SCRIPT = re.compile(r"<script(?P<attrs>[^>]*)>(?P<body>.*?)</script\s*>", re.IGNORECASE | re.DOTALL)


def script_hash(text: str) -> str:
    """The CSP source for an inline script. Browsers hash the text after the HTML parser has turned CRLF and CR
    into LF, so a page checked out with Windows line endings must hash the same."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return "'sha256-" + base64.b64encode(hashlib.sha256(text.encode("utf-8")).digest()).decode("ascii") + "'"


def inline_scripts(html: str) -> list[str]:
    """The text of each ``<script>`` in a page that has no ``src`` (those are the ones a hash must allow)."""
    return [m.group("body") for m in _INLINE_SCRIPT.finditer(html) if not re.search(r"\bsrc\s*=", m.group("attrs"), re.I)]


def inline_script_hashes(frontend: Path) -> list[str]:
    found: set[str] = set()
    for page in sorted(frontend.glob("*.html")) if frontend.is_dir() else ():
        found.update(script_hash(s) for s in inline_scripts(page.read_text(encoding="utf-8")) if s.strip())
    return sorted(found)


def build_csp(frontend: Path) -> str:
    def join(*groups) -> str:
        return " ".join(source for group in groups for source in group)

    return "; ".join([
        "default-src 'self'",
        "script-src " + join(("'self'",), inline_script_hashes(frontend), SCRIPT_SOURCES),
        "style-src " + join(("'self'",), STYLE_SOURCES),
        "img-src " + join(("'self'",), IMG_SOURCES),
        "font-src " + join(("'self'",), FONT_SOURCES),
        "connect-src 'self'",
        "form-action 'self'",
        "frame-ancestors 'none'",
        "frame-src 'none'",
        "base-uri 'none'",
        "object-src 'none'",
    ])


class SecurityHeaders:
    def __init__(self, app, csp: str):
        self.app = app
        self.headers = {**STATIC_HEADERS, "Content-Security-Policy": csp}

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        secure = sessions.is_secure(scope)

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in self.headers.items():
                    headers.setdefault(name, value)
                if secure:
                    headers.setdefault("Strict-Transport-Security", HSTS)
            await send(message)

        await self.app(scope, receive, send_with_headers)


def install(app, frontend: Path) -> None:
    """Wraps the whole app: the Host check first, then the headers around it (so even the 400 for a bad Host has
    them). Wrapping the finished stack rather than ``add_middleware`` puts both outside Starlette's error handler,
    so an unexpected 500 has them too."""
    csp = build_csp(frontend)
    build = app.build_middleware_stack
    app.build_middleware_stack = lambda: SecurityHeaders(HostGuard(build()), csp)

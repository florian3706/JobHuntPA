#!/usr/bin/env python3
"""WCAG 2.2 AA audit of every page and dialog in a real browser (axe-core + Playwright), on fictional data.

    python3 tools/a11y_audit.py NODE_MODULES [--widths 320,1280] [--themes light,dark,dyslexia,deuteranomaly]
                                [--only index,login] [--json report.json] [--keep]

NODE_MODULES is a folder that holds ``axe-core/axe.min.js`` (``npm install axe-core`` in an empty folder makes it)
and, if the machine can't reach unpkg.com, ``leaflet/dist`` (``npm install leaflet@1.9.4``): the map is then served
from there. This is a developer tool: it needs no entry in requirements.txt, and nothing in the app or the tests
imports it. It needs Playwright with Chromium (setup installs both).

What it does
1. Builds a throwaway demo database (tools/demo_data.py), starts the app on it (like tools/readme_screenshots.py)
   with email going to a small mail catcher inside this script, and a second, empty app for the first-run page.
   Nothing touches data/ or the real .env; the scheduler is off; the LLM, Stripe and the map's tile servers are
   never contacted (the browser's requests to them are answered by this script).
2. Opens each page and each state of it (every tab, card, step and dialog) as the right account, at each width
   and in each theme, and runs axe-core with the WCAG 2.0/2.1/2.2 A and AA tags on it. axe is injected through the
   browser's own channel (so the app's Content-Security-Policy stays as it is).
3. Watches the console on every page: any error, any Content-Security-Policy violation, any failed request and
   any HTTP status of 400 or more that the state doesn't expect is reported.
4. Walks the main flows with the keyboard alone (Tab order, a visible focus ring, no trap, focus back after a
   dialog, Escape closes it) and reports what breaks.

The exit status is 1 when anything is reported. ``--only`` picks scenario groups (see GROUPS), ``--list`` shows them.
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import hmac
import json
import mimetypes
import os
import re
import shutil
import socket
import socketserver
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from email import message_from_bytes, policy
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parent.parent
TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))
import readme_screenshots as shots  # noqa: E402  (its server environment: demo admin, fake LLM, no scheduler)

DEMO_EMAIL, DEMO_PASSWORD = shots.DEMO_EMAIL, shots.DEMO_PASSWORD
PASSWORD = "an audit password 1"
AXE_TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"]
DEFAULT_THEMES = ["light", "dark", "dyslexia", "deuteranomaly"]
DEFAULT_WIDTHS = [320, 1280]
HEIGHT = 900
REAL_DATA = (ROOT / "data").resolve()


# --------------------------------------------------------------------------
# Mail catcher: a minimal SMTP server, so the pages reached by an emailed link can be reached
# --------------------------------------------------------------------------

class MailCatcher:
    """Accepts every message on 127.0.0.1 and keeps it. Just enough SMTP for smtplib with SMTP_SECURITY=none."""

    def __init__(self):
        self.messages: list[dict] = []
        outer = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                w = lambda s: (self.wfile.write(s.encode() + b"\r\n"), self.wfile.flush())  # noqa: E731
                w("220 catcher ready")
                sender, rcpts = "", []
                while True:
                    line = self.rfile.readline()
                    if not line:
                        return
                    cmd = line.decode("latin-1").strip()
                    up = cmd.upper()
                    if up.startswith(("EHLO", "HELO")):
                        w("250 catcher")
                    elif up.startswith("MAIL FROM"):
                        sender, rcpts = cmd, []
                        w("250 ok")
                    elif up.startswith("RCPT TO"):
                        rcpts.append(cmd.split(":", 1)[1].strip().strip("<>"))
                        w("250 ok")
                    elif up == "DATA":
                        w("354 go on")
                        raw = b""
                        while True:
                            data_line = self.rfile.readline()
                            if data_line in (b".\r\n", b".\n", b""):
                                break
                            raw += data_line[1:] if data_line.startswith(b"..") else data_line
                        outer.messages.append({"to": rcpts, "msg": message_from_bytes(raw, policy=policy.default)})
                        w("250 queued")
                    elif up == "QUIT":
                        w("221 bye")
                        return
                    else:
                        w("250 ok")

        class Server(socketserver.ThreadingTCPServer):
            daemon_threads = True
            allow_reuse_address = True

        self.server = Server(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def link(self, to: str, pattern: str, since: int = 0, timeout: float = 20) -> str:
        """The first link matching ``pattern`` in a message to ``to`` that arrived after the first ``since``."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            for item in self.messages[since:]:
                if to.lower() not in [r.lower() for r in item["to"]]:
                    continue
                body = item["msg"].get_body(preferencelist=("plain", "html"))
                m = re.search(pattern, body.get_content() if body else "")
                if m:
                    return m.group(0)
            time.sleep(0.2)
        raise TimeoutError(f"no email to {to} with {pattern!r}")


# --------------------------------------------------------------------------
# The apps under audit
# --------------------------------------------------------------------------

def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# The app, as uvicorn runs it, except that the sign-in and email limits are lifted: an audit signs in and loads the
# reset and confirmation pages dozens of times in a few minutes, which the real limits (10 sign-in requests in 5
# minutes) would refuse. Nothing else differs.
LAUNCH = """
import sys, uvicorn
from backend import ratelimit
for name in ("AUTH", "FORGOT_ADDRESS", "RESET_ACCOUNT", "VERIFY_RESEND", "EMAIL_CHANGE", "EMAIL_TEST", "UNSUBSCRIBE"):
    getattr(ratelimit, name).rules = [(10 ** 6, 60)]
uvicorn.run("backend.app:app", host="127.0.0.1", port=int(sys.argv[1]), log_level="warning")
"""


class App:
    """One running copy of the app on its own throwaway database."""

    def __init__(self, work: Path, name: str, *, demo: bool, mail: MailCatcher):
        self.work = work / name
        self.work.mkdir(parents=True)
        self.db = (self.work / "app.db").resolve()
        self.env_file = (self.work / "app.env").resolve()
        if REAL_DATA in self.db.parents or ROOT in self.env_file.parents:
            sys.exit("Refusing to use the real data folder or .env.")
        self.port = free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.demo = demo
        self.mail = mail
        self.proc: subprocess.Popen | None = None
        self.log_path = self.work / "server.log"

    def env(self) -> dict:
        env = shots.server_env(self.db, self.env_file)
        env.update({"SMTP_HOST": "127.0.0.1", "SMTP_PORT": str(self.mail.port), "SMTP_SECURITY": "none",
                    "SMTP_FROM": "JobHuntPA <jobs@example.com>", "PUBLIC_BASE_URL": self.base,
                    "JOBHUNT_DATA_DIR": str(self.work)})
        if not self.demo:  # an empty install: the first-run page
            env.pop("JOBHUNT_ADMIN_EMAIL", None)
            env.pop("JOBHUNT_ADMIN_PASSWORD", None)
        return env

    def start(self, fresh: bool = True) -> None:
        if self.demo and fresh:
            subprocess.run([sys.executable, str(TOOLS / "demo_data.py"), str(self.db)], check=True, cwd=ROOT,
                           stdout=subprocess.DEVNULL)
        log = open(self.log_path, "w", encoding="utf-8")
        self.proc = subprocess.Popen([sys.executable, "-c", LAUNCH, str(self.port)],
                                     cwd=ROOT, env=self.env(), stdout=log, stderr=subprocess.STDOUT)
        deadline = time.time() + 90
        while time.time() < deadline:
            if self.proc.poll() is not None:
                sys.exit(f"The app stopped at start-up:\n{self.log_path.read_text(encoding='utf-8')[-2000:]}")
            try:
                with urllib.request.urlopen(f"{self.base}/api/health", timeout=2) as r:
                    if r.status == 200:
                        return
            except OSError:
                time.sleep(0.4)
        sys.exit("The app did not start within 90 seconds.")

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    def token_file(self) -> str:
        return (self.work / "first-run.txt").read_text(encoding="utf-8").strip().split()[-1]


class Api:
    """A signed-in HTTP client for setting things up (Playwright's request context keeps the cookie)."""

    def __init__(self, playwright, app: App, email: str, password: str):
        self.app = app
        self.ctx = playwright.request.new_context(base_url=app.base)
        self.email = email
        r = self.ctx.post("/api/auth/login", data={"email": email, "password": password})
        if not r.ok:
            raise RuntimeError(f"sign-in as {email} failed: {r.status} {r.text()[:200]}")
        self.me = self.ctx.get("/api/auth/me").json()
        self.csrf = self.me.get("csrf", "")

    def call(self, method: str, path: str, body=None, ok=True):
        r = self.ctx.fetch(path, method=method, data=body if body is not None else ({} if method != "GET" else None),
                           headers={"X-CSRF-Token": self.csrf})
        if ok and not r.ok:
            raise RuntimeError(f"{method} {path}: {r.status} {r.text()[:300]}")
        try:
            return r.json()
        except Exception:
            return None


def totp(secret: str, at: float | None = None) -> str:
    """The 6-digit code an authenticator app shows (RFC 6238: SHA-1, 30 s)."""
    key = base64.b32decode(secret.replace(" ", "").upper() + "=" * (-len(secret.replace(" ", "")) % 8))
    counter = int((at or time.time()) // 30)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 15
    return f"{(struct.unpack('>I', digest[offset:offset + 4])[0] & 0x7fffffff) % 1000000:06d}"


# --------------------------------------------------------------------------
# Findings
# --------------------------------------------------------------------------

class Findings:
    def __init__(self):
        self.items: list[dict] = []
        self.runs = 0
        self.states: set[str] = set()

    def add(self, **item) -> None:
        self.items.append(item)

    def unique(self) -> list[dict]:
        seen, out = set(), []
        for it in self.items:
            key = (it["kind"], it.get("rule"), it["state"], it["width"], it["theme"], it.get("text") or it.get("target"))
            if key not in seen:
                seen.add(key)
                out.append(it)
        return out


# --------------------------------------------------------------------------
# The browser side
# --------------------------------------------------------------------------

COLLECT_JS = """
window.__audit = { csp: [], errors: [] };
document.addEventListener('securitypolicyviolation', (e) => window.__audit.csp.push(
  e.violatedDirective + ' blocked ' + (e.blockedURI || 'inline') + ' at ' + (e.sourceFile || '').split('/').pop() + ':' + e.lineNumber));
"""

AXE_RUN = """async () => {
  const r = await axe.run(document, { runOnly: { type: 'tag', values: %s }, resultTypes: ['violations', 'incomplete'],
    preload: false /* axe would fetch other sites' stylesheets itself, which the CSP's connect-src refuses */ });
  const cut = (n) => ({ target: n.target.join(' >> '), html: (n.html || '').slice(0, 220),
                        summary: (n.failureSummary || '').split('\\n').slice(0, 4).join(' ').slice(0, 300) });
  const pick = (list) => list.map((v) => ({ id: v.id, impact: v.impact, help: v.help, tags: v.tags.filter((t) => /^wcag\\d/.test(t)),
                                           nodes: v.nodes.map(cut) }));
  return { violations: pick(r.violations), incomplete: pick(r.incomplete) };
}""" % json.dumps(AXE_TAGS)

# axe can't judge some text (a background it can't work out: an element above it, a picture). For those this works
# the contrast out from the computed colours: the text colour (a placeholder's own for an empty field) over its
# ancestors' backgrounds, laid on top of each other. Where a background image or gradient is involved it says so.
MANUAL_CONTRAST_JS = r"""(sel) => {
  const el = document.querySelector(sel);
  if (!el || el.closest('.leaflet-control-attribution, .leaflet-tile-pane')) return null;  // the map library's credit line, over the tiles
  // "rgb(r, g, b)", "rgba(...)" and, for color-mix(), "color(srgb 0.6 0.3 0.01 / 0.14)" (parts 0 to 1)
  const num = (c) => { const m = c.match(/[\d.]+(?:e-?\d+)?/g).map(Number); const k = c.startsWith('color(') ? 255 : 1;
    return [m[0] * k, m[1] * k, m[2] * k, m.length > 3 ? m[3] : 1]; };
  const over = (top, under) => { const a = top[3] + under[3] * (1 - top[3]); return a === 0 ? [0, 0, 0, 0] : [0, 1, 2].map((i) => (top[i] * top[3] + under[i] * under[3] * (1 - top[3])) / a).concat([a]); };
  let bg = [255, 255, 255, 0], image = false;
  for (let n = el; n; n = n.parentElement) {
    const cs = getComputedStyle(n);
    if (cs.backgroundImage !== 'none') image = true;
    bg = over(bg, num(cs.backgroundColor));
    if (bg[3] >= 0.999) break;
  }
  if (bg[3] < 0.999) bg = over(bg, [255, 255, 255, 1]);
  const own = el.matches('input, textarea') && !el.value ? getComputedStyle(el, '::placeholder') : getComputedStyle(el);
  let fg = num(own.color);
  fg = over(fg, bg);
  const lum = (c) => { const f = c.slice(0, 3).map((v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); }); return 0.2126 * f[0] + 0.7152 * f[1] + 0.0722 * f[2]; };
  const [hi, lo] = [lum(fg), lum(bg)].sort((x, y) => y - x);
  const size = parseFloat(getComputedStyle(el).fontSize), bold = parseInt(getComputedStyle(el).fontWeight) >= 700;
  return { ratio: (hi + 0.05) / (lo + 0.05), image, large: size >= 24 || (size >= 18.66 && bold), fg: fg.slice(0, 3).map(Math.round), bg: bg.slice(0, 3).map(Math.round) };
}"""

# 1.4.10 Reflow: at 320 px nothing may need sideways scrolling (a map or a scrolling box that keeps to its own width is fine).
REFLOW_JS = r"""() => {
  const out = [];
  const clipped = (el) => { for (let n = el.parentElement; n && n !== document.body; n = n.parentElement) {
    const o = getComputedStyle(n).overflowX; if (o === 'auto' || o === 'scroll' || o === 'hidden' || o === 'clip') return true; } return false; };
  for (const el of document.querySelectorAll('body *')) {
    const r = el.getBoundingClientRect();
    if (!r.width || !r.height || r.right <= innerWidth + 1 || getComputedStyle(el).position === 'fixed' || clipped(el)) continue;
    if (el.closest('.visually-hidden, .skip-link, [hidden]')) continue;
    out.push(el.tagName.toLowerCase() + (el.id ? '#' + el.id : typeof el.className === 'string' && el.className ? '.' + el.className.trim().split(/\s+/)[0] : '') + ' ends at ' + Math.round(r.right) + 'px');
    if (out.length >= 4) break;
  }
  return { scrollWidth: document.documentElement.scrollWidth, width: innerWidth, wide: out };
}"""

# 1.4.12 Text spacing: with the spacing the criterion names (line height 1.5, letter 0.12em, word 0.16em, paragraphs 2em)
# no text may be cut off by a box that clips, and the page may not grow sideways. The sheet is added through the CSSOM
# (a constructed style sheet), which the app's Content-Security-Policy allows, and taken away again.
SPACING_JS = r"""() => {
  const clips = () => {
    const found = new Set();
    for (const el of document.querySelectorAll('body *')) {
      if (![...el.childNodes].some((n) => n.nodeType === 3 && n.textContent.trim())) continue;
      const cs = getComputedStyle(el), r = el.getBoundingClientRect();
      if (!r.width || !r.height || cs.visibility === 'hidden') continue;
      const cut = (v) => v === 'hidden' || v === 'clip';
      if ((cut(cs.overflowY) && el.scrollHeight > el.clientHeight + 1) || (cut(cs.overflowX) && el.scrollWidth > el.clientWidth + 1)) {
        found.add(el.tagName.toLowerCase() + (el.id ? '#' + el.id : typeof el.className === 'string' && el.className ? '.' + el.className.trim().split(/\s+/)[0] : '') + ' "' + el.textContent.trim().slice(0, 30) + '"');
      }
    }
    return found;
  };
  const before = clips(), wide = document.documentElement.scrollWidth;
  const sheet = new CSSStyleSheet();
  sheet.replaceSync('* { line-height: 1.5 !important; letter-spacing: 0.12em !important; word-spacing: 0.16em !important; } p { margin-bottom: 2em !important; }');
  document.adoptedStyleSheets = [...document.adoptedStyleSheets, sheet];
  const after = clips(), wideAfter = document.documentElement.scrollWidth;
  document.adoptedStyleSheets = document.adoptedStyleSheets.filter((x) => x !== sheet);
  return { newlyCut: [...after].filter((x) => !before.has(x)).slice(0, 5), widened: wideAfter > innerWidth + 1 && wideAfter > wide ? wideAfter : 0 };
}"""

# Leaflet is served from the local copy when asked for; tiles are a grey square; fonts come from the real thing.
GREY_TILE = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYGD4DwABBAEAfbLI3wAAAABJRU5ErkJggg==")


class Auditor:
    def __init__(self, playwright, browser, apps: dict, mail: MailCatcher, node_modules: Path, findings: Findings):
        self.pw, self.browser, self.apps, self.mail = playwright, browser, apps, mail
        self.axe_src = (node_modules / "axe-core" / "axe.min.js").read_text(encoding="utf-8")
        self.leaflet = node_modules / "leaflet" / "dist"
        self.findings = findings
        self.font_cache: dict[str, tuple[int, str, bytes]] = {}
        self.width = self.theme = None
        self.group = ""
        self.fixtures: dict = {}
        self.best_practice = False

    # -- browser contexts -------------------------------------------------------
    def context(self, app: App, *, width: int, theme: str, login: tuple | None = None, extra_ls: dict | None = None,
                reduced_motion: bool = False, touch: bool | None = None):
        """A fresh browser profile, in a theme, ``width`` px wide. ``login=(email, password)`` signs in first (the session
        cookie lands in this profile)."""
        # A phone-sized window is a touch screen too (pointer: coarse: the 44 px controls); a wide one is a mouse.
        touch = width < 500 if touch is None else touch
        ctx = self.browser.new_context(viewport={"width": width, "height": HEIGHT}, locale="en-AU", timezone_id="Australia/Sydney",
                                       reduced_motion="reduce" if reduced_motion else "no-preference", has_touch=touch)
        ls = {"jobhunt.theme": theme, "jobhunt.mapMode": "jobs", **(extra_ls or {})}
        ctx.add_init_script("try { const v = %s; for (const k in v) localStorage.setItem(k, v[k]); } catch (e) {}" % json.dumps(ls))
        ctx.add_init_script(COLLECT_JS)
        ctx.add_init_script(self.axe_src)  # injected by the browser itself: the page's CSP is never in its way
        ctx.route(re.compile(r"https://(unpkg\.com|[abc]\.tile\.openstreetmap\.org|fonts\.googleapis\.com|fonts\.gstatic\.com)/.*"),
                  self.external)
        if login is not None:
            r = ctx.request.post(f"{app.base}/api/auth/login", data={"email": login[0], "password": login[1]})
            if r.ok and r.json().get("mfa_required"):  # login[2] is the account's authenticator secret
                r = ctx.request.post(f"{app.base}/api/auth/login/mfa", data={"code": totp(login[2], time.time() + 30)})  # a step ahead: the setup spent this one
            if not r.ok:
                raise RuntimeError(f"sign-in as {login[0]} failed: {r.status} {r.text()[:200]}")
        return ctx

    def external(self, route) -> None:
        url = route.request.url
        host = urlsplit(url).netloc
        if host == "unpkg.com":
            m = re.match(r"https://unpkg\.com/leaflet@[\d.]+/dist/(.+)", url)
            f = self.leaflet / m.group(1) if m else None
            if f and f.is_file():
                route.fulfill(status=200, body=f.read_bytes(), content_type=mimetypes.guess_type(f.name)[0] or "application/octet-stream")
                return
        elif host.endswith("tile.openstreetmap.org"):
            route.fulfill(status=200, body=GREY_TILE, content_type="image/png")
            return
        elif host.startswith("fonts."):
            hit = self.font_cache.get(url)
            if hit is None:
                try:
                    req = urllib.request.Request(url, headers={"User-Agent": route.request.headers.get("user-agent", "Mozilla/5.0")})
                    with urllib.request.urlopen(req, timeout=10) as r:
                        hit = (r.status, r.headers.get("content-type", ""), r.read())
                except Exception:
                    hit = (200, "text/css", b"") if host == "fonts.googleapis.com" else (404, "text/plain", b"")
                self.font_cache[url] = hit
            route.fulfill(status=hit[0], body=hit[2], content_type=hit[1], headers={"Access-Control-Allow-Origin": "*"})
            return
        route.fulfill(status=404, body=b"")

    @contextlib.contextmanager
    def step(self, name: str):
        """A state that can't be reached is itself a finding, and the scenario goes on with the next one."""
        try:
            yield
        except Exception as exc:
            self.findings.add(kind="scenario-error", state=name, width=self.width, theme=self.theme, group=self.group,
                              text=f"{type(exc).__name__}: {str(exc).splitlines()[0][:260]}")

    def unique(self, name: str) -> str:
        self.counter = getattr(self, "counter", 0) + 1
        return f"{name}-{self.counter}"

    # -- emailed links ---------------------------------------------------------
    def reset_link(self, key: str) -> str:
        """A fresh password-reset link for a fixture account (read from the mail catcher)."""
        user = self.fixtures[key]
        before = len(self.mail.messages)
        r = self.pw.request.new_context().post(f"{self.apps['demo'].base}/api/auth/forgot", data={"email": user["email"]})
        if r.status != 202:
            raise RuntimeError(f"forgot-password answered {r.status}")
        return self.mail.link(user["email"], r"https?://\S+/reset-password\.html#token=[\w-]+", before)

    def verify_link(self) -> str:
        """A fresh email-confirmation link (single use): a new account's own confirmation email."""
        before = len(self.mail.messages)
        user = new_account(self, self.unique("unverified"))
        return self.mail.link(user["email"], r"https?://\S+/verify-email\.html#token=[\w-]+", before)

    # -- one page ---------------------------------------------------------------
    def page(self, ctx, state: str):
        page = ctx.new_page()
        page.set_default_timeout(15000)
        page._audit_state = state
        page._expect = []  # (status, url fragment) pairs this state is meant to cause
        page.on("pageerror", lambda e: self.report("pageerror", page, text=str(e)[:300]))
        page.on("console", lambda m: self.on_console(page, m))
        page.on("requestfailed", lambda r: self.report("requestfailed", page, text=f"{r.url[:140]} {r.failure}")
                if "favicon" not in r.url else None)
        page.on("response", lambda r: self.on_response(page, r))
        return page

    def on_console(self, page, msg) -> None:
        if msg.type not in ("error", "warning"):
            return
        text = msg.text
        if "Failed to load resource" in text:
            return  # the response handler names the address
        self.report("console", page, text=f"{msg.type}: {text[:300]}")

    def on_response(self, page, r) -> None:
        if r.status < 400:
            return
        url = r.url
        if "favicon" in url:
            return
        if any(r.status == s and frag in url for s, frag in page._expect):
            return
        self.report("http", page, text=f"{r.status} {r.request.method} {urlsplit(url).path}")

    def report(self, kind: str, page, **fields) -> None:
        self.findings.add(kind=kind, state=getattr(page, "_audit_state", "?"), width=self.width, theme=self.theme,
                          group=self.group, **fields)

    def manual_contrast(self, page, node) -> bool:
        """True when axe's 'can't tell' contrast result was settled here (and reported if it fails)."""
        if "non-text" in node["summary"] or "too short" in node["summary"] or "third-party" in node["summary"]:
            return False
        if "leaflet" in node["html"]:  # the map library's own attribution and the map's tiles: not ours to restyle
            return False
        try:
            r = page.evaluate(MANUAL_CONTRAST_JS, node["target"].split(" >> ")[0])
        except Exception:
            return False
        if not r or r["image"]:
            return False
        need = 3.0 if r["large"] else 4.5
        if r["ratio"] < need:
            self.report("axe", page, rule="color-contrast (worked out here)", impact="serious", help="text contrast", target=node["target"],
                        html=node["html"], text=f"{r['ratio']:.2f}:1, needs {need}:1 (text {r['fg']} on {r['bg']})")
        return True

    def check(self, page, label: str | None = None, *, expect: list | None = None) -> None:
        """Runs axe on the page as it is now and reports CSP violations seen so far."""
        if label:
            page._audit_state = label
        state = page._audit_state
        self.findings.runs += 1
        self.findings.states.add(state)
        with contextlib.suppress(Exception):
            page.evaluate("() => document.fonts.ready")
        # axe judges a button that is scrolled out of sight against the sticky header ("partially obscured": 8 px of a
        # button far above the window); from the top of the page every position is judged where a person would see it.
        with contextlib.suppress(Exception):
            page.evaluate("() => window.scrollTo({ top: 0, left: 0, behavior: 'instant' })")
        page.wait_for_timeout(250)  # animations settle (reduced motion is not forced: the real thing is audited)
        try:
            result = page.evaluate(AXE_RUN)
        except Exception as exc:  # the page navigated away, or axe could not run
            self.report("axe-error", page, text=str(exc)[:200])
            return
        if self.width <= 400:
            with contextlib.suppress(Exception):
                flow = page.evaluate(REFLOW_JS)
                if flow["wide"]:
                    self.report("reflow", page, text=f"the page needs sideways scrolling at {flow['width']}px: " + "; ".join(flow["wide"]))
        with contextlib.suppress(Exception):
            spacing = page.evaluate(SPACING_JS)
            for text in spacing["newlyCut"]:
                self.report("text-spacing", page, text=f"text is cut off with WCAG's text spacing: {text}")
            if spacing["widened"]:
                self.report("text-spacing", page, text=f"the page grows sideways to {spacing['widened']}px with WCAG's text spacing")
        if self.best_practice:
            with contextlib.suppress(Exception):
                best = page.evaluate(AXE_RUN.replace(json.dumps(AXE_TAGS), '["best-practice"]'))
                for v in best["violations"]:
                    for node in v["nodes"]:
                        self.report("best-practice", page, rule=v["id"], impact=v["impact"], help=v["help"], target=node["target"],
                                    html=node["html"], text=node["summary"])
        for v in result["violations"]:
            for node in v["nodes"]:
                self.report("axe", page, rule=v["id"], impact=v["impact"], help=v["help"], target=node["target"],
                            html=node["html"], text=node["summary"])
        for v in result["incomplete"]:
            for node in v["nodes"]:
                if v["id"] == "color-contrast" and self.manual_contrast(page, node):
                    continue
                self.report("axe-review", page, rule=v["id"], impact=v["impact"], help=v["help"], target=node["target"],
                            html=node["html"], text=node["summary"])
        with contextlib.suppress(Exception):
            for text in page.evaluate("() => window.__audit && window.__audit.csp") or []:
                self.report("csp", page, text=text)
            page.evaluate("() => { if (window.__audit) window.__audit.csp.length = 0; }")


# --------------------------------------------------------------------------
# Scenarios: each opens a page, reaches a state, and calls a.check(page, "name")
# --------------------------------------------------------------------------

GROUPS: dict[str, list] = {}


def scenario(group: str):
    def register(fn):
        GROUPS.setdefault(group, []).append(fn)
        return fn
    return register


# Playwright's string expressions run through eval() in the page, which the app's policy refuses: always pass
# functions ("() => ...") to evaluate() and wait_for_function().
def settle(page, ms: int = 300) -> None:
    with contextlib.suppress(Exception):
        page.wait_for_load_state("networkidle", timeout=8000)
    page.wait_for_timeout(ms)


def admin_ctx(a: Auditor, w: int, theme: str, **kw):
    app = a.apps["demo"]
    return a.context(app, width=w, theme=theme, login=(DEMO_EMAIL, DEMO_PASSWORD),
                     extra_ls={"jobhunt.ws": str(a.fixtures["ws"])}, **kw)


def dashboard(a: Auditor, ctx, hash_: str = "#jobs", state: str = "index"):
    page = a.page(ctx, state)
    page.goto(f"{a.apps['demo'].base}/{hash_}")
    page.wait_for_selector(".job-card")
    page.wait_for_function("""() => document.querySelector('#next-scheduled') && !document.querySelector('#next-scheduled').hidden
        && !document.querySelector('#scorer-status').textContent.includes('checking')""", timeout=30000)
    settle(page)
    return page


# -- the dashboard ---------------------------------------------------------------------------------------

def open_tab(page, tab: str) -> None:
    page.click(f'.tab[data-tab="{tab}"]')
    page.wait_for_selector(f"#tab-{tab}:not([hidden])")
    settle(page, 500)


@scenario("index")
def sc_jobs(a, w, theme):
    ctx = admin_ctx(a, w, theme)
    page = dashboard(a, ctx)
    with a.step("index: Jobs tab"):
        a.check(page, "index: Jobs tab")
    with a.step("index: show excluded/closed/hidden"):
        for box in ("excluded", "closed", "hidden"):
            page.check(f"#filter-show-{box}")
        settle(page, 600)
        a.check(page, "index: Jobs tab, show excluded/closed/hidden ticked")
        for box in ("excluded", "closed", "hidden"):
            page.uncheck(f"#filter-show-{box}")
    with a.step("index: nothing matches"):
        page.fill("#filter-search", "zzzz no such job")
        settle(page, 600)
        a.check(page, "index: Jobs tab, nothing matches")
        page.fill("#filter-search", "")
        settle(page, 400)
    with a.step("index: a job selected (bulk bar)"):
        page.check(".job-card:first-of-type input[type=checkbox]")
        page.wait_for_selector("#bulk-bar:not([hidden])")
        a.check(page, "index: Jobs tab, a job selected")
        page.uncheck(".job-card:first-of-type input[type=checkbox]")
    with a.step("index: fit details open"):
        page.click(".job-card:first-of-type details.reqs summary")
        settle(page, 800)
        a.check(page, "index: a job card's fit details open")
    with a.step("index: run report open"):
        if page.query_selector("#run-report:not([hidden]) summary"):
            page.click("#run-report summary")
            a.check(page, "index: run report open")
    with a.step("index: account menu"):
        page.click("#account-btn")
        page.wait_for_selector("#account-panel:not([hidden])")
        a.check(page, "index: account menu open")
        page.keyboard.press("Escape")
    ctx.close()


@scenario("index")
def sc_jobs_empty(a, w, theme):
    """The known 320 px target-size warning: the three check boxes with an empty list (a workspace with no jobs)."""
    for touch in (None, False):  # a phone (touch) and a narrow window with a mouse
        ctx = a.context(a.apps["demo"], width=w, theme=theme, login=(DEMO_EMAIL, DEMO_PASSWORD), touch=touch,
                        extra_ls={"jobhunt.ws": str(a.fixtures["empty_ws"])})
        name = "index: Jobs tab, empty list" + (", mouse" if touch is False and w < 500 else "")
        page = a.page(ctx, name)
        with a.step(name):
            page.goto(a.apps["demo"].base + "/#jobs")
            page.wait_for_selector("#jobs-empty:not([hidden])")
            settle(page, 600)
            a.check(page, name)
        ctx.close()
        if w >= 500:
            break


@scenario("index")
def sc_tabs(a, w, theme):
    ctx = admin_ctx(a, w, theme)
    page = dashboard(a, ctx)
    with a.step("index: Search setup tab"):
        open_tab(page, "search")
        page.wait_for_selector("#sources-list .source-row")
        a.check(page, "index: Search setup tab")
    with a.step("index: Documents tab"):
        open_tab(page, "docs")
        page.wait_for_selector("#docs-list > *")
        a.check(page, "index: Documents tab")
    with a.step("index: Map tab, jobs"):
        open_tab(page, "map")
        page.wait_for_selector(".map-job-row")
        a.check(page, "index: Map tab, jobs mode")
    with a.step("index: Map tab, pins"):
        page.click("#map-mode-pins")
        page.wait_for_selector("#pin-controls:not([hidden])")
        settle(page, 500)
        a.check(page, "index: Map tab, edit pins")
    with a.step("index: Settings tab"):
        open_tab(page, "settings")
        page.wait_for_selector("#acct-pw-form")
        a.check(page, "index: Settings tab (every card)")
    with a.step("index: Settings, two-step setup"):
        page.click("#acct-mfa-start")
        page.wait_for_selector("#acct-mfa-on-form")
        settle(page, 400)
        a.check(page, "index: Settings, two-step sign-in setup")
        page.click("#acct-mfa-on-cancel")
    with a.step("index: Settings, errors under fields"):
        page.click("#acct-pw-submit")
        page.wait_for_selector("#acct-pw-form .field-error, #acct-pw-form [aria-invalid=true]")
        a.check(page, "index: Settings, password form with errors")
    ctx.close()


@scenario("index")
def sc_dialogs(a, w, theme):
    ctx = admin_ctx(a, w, theme)
    page = dashboard(a, ctx)
    first = ".job-card:first-of-type"
    with a.step("index: description modal"):
        page.click(f'{first} [data-act="desc"]')
        page.wait_for_selector("#modal[open]")
        settle(page, 400)
        a.check(page, "index: job description dialog")
        page.keyboard.press("Escape")
    with a.step("index: cover letter modal"):
        page.click(f'{first} [data-act="letter"]')
        page.wait_for_function("() => document.querySelector('#cl-text') && document.querySelector('#cl-text').value.length > 100")
        settle(page, 400)
        a.check(page, "index: cover letter dialog")
        page.keyboard.press("Escape")
    with a.step("index: company modal"):
        page.click(f'{first} [data-act="company"]')
        page.wait_for_selector("#modal[open]")
        settle(page, 400)
        a.check(page, "index: company info dialog")
        page.keyboard.press("Escape")
    with a.step("index: duplicates modal"):
        page.click("#dupes-open")
        page.wait_for_selector("#modal[open]")
        settle(page, 500)
        a.check(page, "index: duplicates dialog")
        page.keyboard.press("Escape")
    with a.step("index: chat panel"):
        page.click(f'{first} [data-act="chat"]')
        page.wait_for_selector(".chat-msg.assistant")
        settle(page, 400)
        a.check(page, "index: chat panel")
        page.click("#chat-close")
    with a.step("index: confirm your password"):
        page.evaluate("() => { window.__confirm = confirmPassword(); }")  # (a promise would be awaited)
        page.wait_for_selector("dialog.confirm-dialog[open]")
        a.check(page, "index: confirm your password dialog")
        page._expect.append((422, "/api/auth/reauth"))  # the wrong password below
        page.fill("dialog.confirm-dialog input[type=password]", "wrong")
        page.keyboard.press("Enter")
        page.wait_for_selector("dialog.confirm-dialog [role=alert]:not(:empty)")
        a.check(page, "index: confirm your password dialog, wrong password")
        page.keyboard.press("Escape")
    ctx.close()


@scenario("index")
def sc_more_states(a, w, theme):
    """Workspace dialogs, messages, an opened document, a form with a problem, a map popup, a new pin."""
    ctx = admin_ctx(a, w, theme)
    page = dashboard(a, ctx)
    with a.step("index: new workspace dialog"):
        page.click("#ws-new")
        page.wait_for_selector("#modal[open]")
        settle(page, 300)
        a.check(page, "index: new workspace dialog")
        page.click("#modal button[type=submit]")
        page.wait_for_selector("#modal [aria-invalid=true], #modal .field-error")
        a.check(page, "index: new workspace dialog with an error")
        page.keyboard.press("Escape")
    with a.step("index: rename workspace dialog"):
        page.click("#ws-rename")
        page.wait_for_selector("#modal[open]")
        a.check(page, "index: rename workspace dialog")
        page.keyboard.press("Escape")
    with a.step("index: messages in the corner"):
        page.evaluate("() => { toast('Saved your search', 'ok', 60000); toast('Something went wrong', 'err', 60000); }")
        settle(page, 300)
        a.check(page, "index: toast messages")
        page.evaluate("() => document.querySelectorAll('.toast-close').forEach((b) => b.click())")
    with a.step("index: upload full ad dialog"):
        btn = page.query_selector('[data-act="upload-ad"]')
        if btn:
            btn.click()
            page.wait_for_selector("#modal[open]")
            settle(page, 300)
            a.check(page, "index: upload full ad dialog")
            page.keyboard.press("Escape")
    with a.step("index: search setup problems"):
        open_tab(page, "search")
        page.click("#source-add")
        settle(page, 500)
        a.check(page, "index: Search setup, empty source added")
    with a.step("index: a document opened"):
        open_tab(page, "docs")
        page.click('#docs-list [data-act="toggle"] >> nth=0')
        settle(page, 600)
        a.check(page, "index: Documents tab, a document opened")
    with a.step("index: map popup and a new pin"):
        open_tab(page, "map")
        page.wait_for_selector(".map-job-row")
        page.click('.map-job-row [data-act="show"] >> nth=0')
        settle(page, 800)
        a.check(page, "index: Map tab, a job shown (popup)")
        page.click("#map-mode-pins")
        page.wait_for_selector("#pin-controls:not([hidden])")
        page.click("#pin-add-centre")
        page.wait_for_selector("#pins-list:not([hidden]) > *")
        settle(page, 600)
        a.check(page, "index: Map tab, pin added")
        # (put it back: a second pin dropped at the same centre would sit on this one, and two markers on top of each
        # other can't both be 24 px targets: the pins list is the equivalent way to reach each)
        page.click('#pins-list .pin-row:last-child [data-act="del"]')
        settle(page, 500)
    ctx.close()


@scenario("themes")
def sc_theme_sample(a, w, theme):
    """A few key screens, for running every theme (--themes all --only themes): the contrast rule does the work."""
    base = a.apps["demo"].base
    ctx = plain_ctx(a, w, theme)
    page = a.page(ctx, "login: password step")
    with a.step("themes: login"):
        page.goto(f"{base}/login.html")
        page.wait_for_selector("#login-form:not([hidden])")
        settle(page, 400)
        a.check(page, "login: password step")
        page.click("#login-submit")
        page.wait_for_selector("#error-summary:not([hidden])")
        a.check(page, "login: error summary")
    ctx.close()
    ctx = admin_ctx(a, w, theme)
    page = dashboard(a, ctx)
    with a.step("themes: Jobs tab"):
        a.check(page, "index: Jobs tab")
        page.click('.job-card:first-of-type [data-act="desc"]')
        page.wait_for_selector("#modal[open]")
        a.check(page, "index: job description dialog")
        page.keyboard.press("Escape")
    with a.step("themes: Settings tab"):
        open_tab(page, "settings")
        page.wait_for_selector("#acct-pw-form")
        a.check(page, "index: Settings tab (every card)")
    with a.step("themes: Map tab"):
        open_tab(page, "map")
        page.wait_for_selector(".map-job-row")
        a.check(page, "index: Map tab, jobs mode")
    with a.step("themes: admin"):
        page.goto("about:blank")
        page.goto(f"{base}/admin.html")
        page.wait_for_selector("#users-grid")
        settle(page, 800)
        a.check(page, "admin.html: every section")
    ctx.close()


# -- sign-in pages ------------------------------------------------------------------------------------------

def plain_ctx(a: Auditor, w: int, theme: str, app: str = "demo", **kw):
    return a.context(a.apps[app], width=w, theme=theme, **kw)


def fill_submit(page, fields: dict, submit: str) -> None:
    for sel, value in fields.items():
        page.fill(sel, value)
    page.click(submit)


@scenario("auth")
def sc_login(a, w, theme):
    base = a.apps["demo"].base
    ctx = plain_ctx(a, w, theme)
    page = a.page(ctx, "login: password step")
    with a.step("login: password step"):
        page.goto(f"{base}/login.html")
        page.wait_for_selector("#login-form:not([hidden])")
        settle(page)
        a.check(page, "login: password step")
    with a.step("login: errors"):
        page._expect.append((401, "/api/auth/login"))
        page._expect.append((429, "/api/auth/login"))  # the wrong-password lockout, after the same wrong address a few times
        page.click("#login-submit")
        page.wait_for_selector("#error-summary:not([hidden])")
        a.check(page, "login: missing fields error summary")
        fill_submit(page, {"#email": "nobody@example.invalid", "#password": "not the password"}, "#login-submit")
        page.wait_for_function("() => document.querySelector('#error-summary-list li')?.textContent.length > 0")
        settle(page, 300)
        a.check(page, "login: wrong password error")
    with a.step("login: forgot link shown"):
        page.fill("#password", "")
        page.click("#forgot-btn")
        settle(page, 500)
        a.check(page, "login: after Email me a link")
    with a.step("login: show password"):
        page.fill("#password", "something")
        page.click(".pw-toggle")
        a.check(page, "login: password shown")
    ctx.close()


@scenario("auth")
def sc_login_code(a, w, theme):
    base = a.apps["demo"].base
    user = new_account(a, a.unique("code-login"), mfa=True)
    ctx = plain_ctx(a, w, theme)
    page = a.page(ctx, "login: code step")
    with a.step("login: code step"):
        page.goto(f"{base}/login.html")
        fill_submit(page, {"#email": user["email"], "#password": PASSWORD}, "#login-submit")
        page.wait_for_selector("#mfa-form:not([hidden])")
        settle(page, 300)
        a.check(page, "login: two-step code step")
    with a.step("login: wrong code"):
        page._expect.append((422, "/api/auth/login/mfa"))
        page._expect.append((401, "/api/auth/login/mfa"))
        fill_submit(page, {"#code": "000000"}, "#mfa-submit")
        page.wait_for_selector("#error-summary:not([hidden])")
        settle(page, 300)
        a.check(page, "login: wrong code error")
    with a.step("login: recovery code step"):
        page.click("#mfa-switch")
        page.wait_for_selector("#recovery-field:not([hidden])")
        a.check(page, "login: recovery code step")
    ctx.close()


@scenario("auth")
def sc_first_run(a, w, theme):
    app = a.apps["empty"]
    ctx = a.context(app, width=w, theme=theme)
    page = a.page(ctx, "first-run: form")
    with a.step("first-run: form"):
        page.goto(f"{app.base}/first-run.html#token={app.token_file()}")
        page.wait_for_selector("#setup-step:not([hidden])")
        settle(page)
        a.check(page, "first-run: form (setup link)")
    with a.step("first-run: form without the link"):
        page.goto(f"{app.base}/first-run.html")
        page.wait_for_selector("#setup-step:not([hidden])")
        a.check(page, "first-run: form (code field shown)")
    with a.step("first-run: errors"):
        page.click("#setup-submit")
        page.wait_for_selector("#error-summary:not([hidden])")
        a.check(page, "first-run: error summary")
    ctx.close()


@scenario("auth")
def sc_recover(a, w, theme):
    base = a.apps["demo"].base
    ctx = plain_ctx(a, w, theme)
    page = a.page(ctx, "recover: form")
    with a.step("recover: form"):
        page.goto(f"{base}/recover.html")
        page.wait_for_selector("#recover-form:not([hidden])")
        settle(page)
        a.check(page, "recover: form")
    with a.step("recover: errors"):
        page.click("#recover-submit")
        page.wait_for_selector("#error-summary:not([hidden])")
        a.check(page, "recover: error summary")
    with a.step("recover: second factor asked"):
        user = a.fixtures["mfa"]
        page._expect.append((422, "/api/auth/recover"))
        page._expect.append((401, "/api/auth/recover"))
        page._expect.append((403, "/api/auth/recover"))
        fill_submit(page, {"#email": user["email"], "#recovery-code": user["codes"][0], "#password": "another long password"},
                    "#recover-submit")
        page.wait_for_selector("#code-field:not([hidden])")
        settle(page, 300)
        a.check(page, "recover: second code asked for")
    ctx.close()


@scenario("auth")
def sc_signup(a, w, theme):
    app = a.apps["demo"]
    admin: Api = a.fixtures["admin"]
    try:
        for mode, label in (("invite", "needs an invitation"), ("closed", "closed"), ("open", "open")):
            admin.call("PUT", "/api/admin/signup", {"mode": mode})
            ctx = plain_ctx(a, w, theme)
            page = a.page(ctx, f"signup: {label}")
            with a.step(f"signup: {label}"):
                page.goto(f"{app.base}/signup.html")
                page.wait_for_function("() => !document.querySelector('#auth-status').textContent.includes('Checking')")
                settle(page, 300)
                a.check(page, f"signup: {label}")
            if mode == "open":
                with a.step("signup: errors"):
                    page.click("#signup-submit")
                    page.wait_for_selector("#error-summary:not([hidden])")
                    a.check(page, "signup: error summary")
                with a.step("signup: email taken"):
                    page._expect.append((409, "/api/auth/signup"))
                    page._expect.append((422, "/api/auth/signup"))
                    fill_submit(page, {"#email": DEMO_EMAIL, "#password": "a long enough password"}, "#signup-submit")
                    page.wait_for_function("() => document.querySelector('#error-summary-list li')?.textContent.length > 0")
                    a.check(page, "signup: address already used")
            ctx.close()
        invite = admin.call("POST", "/api/admin/invites", {"email": "invited@example.invalid", "send": False})["link"]
        ctx = plain_ctx(a, w, theme)
        page = a.page(ctx, "signup: invitation")
        with a.step("signup: invitation link"):
            page.goto(invite)
            page.wait_for_selector("#setup-step:not([hidden])")
            a.check(page, "signup: invitation link (address filled in)")
        ctx.close()
        ctx = plain_ctx(a, w, theme)
        page = a.page(ctx, "signup: bad invitation")
        with a.step("signup: used or bad invitation"):
            page.goto(f"{app.base}/signup.html?invite=nope")
            page.wait_for_selector("#closed-step:not([hidden]), #setup-step:not([hidden])")
            a.check(page, "signup: bad invitation")
        ctx.close()
    finally:
        admin.call("PUT", "/api/admin/signup", {"mode": "open"})


@scenario("auth")
def sc_codes_step(a, w, theme):
    """The recovery-codes step (first-run.html when signed in with codes pending)."""
    app = a.apps["demo"]
    user = a.fixtures["fresh"]
    api = Api(a.pw, app, user["email"], PASSWORD)
    api.call("POST", "/api/auth/recovery-codes", {})  # new codes are pending
    ctx = plain_ctx(a, w, theme, login=(user["email"], PASSWORD))
    page = a.page(ctx, "first-run: recovery codes")
    with a.step("first-run: recovery codes"):
        page.goto(f"{app.base}/first-run.html")
        page.wait_for_selector("#codes-step:not([hidden])")
        settle(page, 300)
        a.check(page, "first-run: recovery codes step")
        page.click("#codes-continue")
        page.wait_for_selector("#error-summary:not([hidden])")
        a.check(page, "first-run: recovery codes, not ticked")
        with page.expect_download(timeout=15000) as dl:  # saved through a blob: address, which the policy must allow
            page.click("#codes-download")
        if not dl.value.suggested_filename.endswith(".txt"):
            a.report("keyboard", page, text=f"recovery codes: unexpected file {dl.value.suggested_filename}")
        page.evaluate("() => { window.print = () => { window.__printed = true; }; }")
        page.click("#codes-print")
        if not page.evaluate("() => !!window.__printed"):
            a.report("keyboard", page, text="recovery codes: Print does not print")
        page.emulate_media(media="print")
        a.check(page, "first-run: recovery codes, as printed")
        page.emulate_media(media="screen")
    ctx.close()
    api.call("POST", "/api/auth/recovery-codes/ack", {})


# -- the pages an emailed link opens -----------------------------------------------------------------------

@scenario("links")
def sc_reset(a, w, theme):
    app = a.apps["demo"]
    for key, label in (("keeps", "keep data"), ("wipes", "wipe"), ("mfa", "two-step")):
        link = a.reset_link(key)
        ctx = plain_ctx(a, w, theme)
        page = a.page(ctx, f"reset-password: {label}")
        with a.step(f"reset-password: {label}"):
            page.goto(link)
            page.wait_for_selector("#setup-step:not([hidden])")
            settle(page, 300)
            a.check(page, f"reset-password: {label}")
        if key == "wipes":
            with a.step("reset-password: wipe errors"):
                page.click("#reset-submit")
                page.wait_for_selector("#error-summary:not([hidden])")
                a.check(page, "reset-password: wipe, errors")
        ctx.close()
    ctx = plain_ctx(a, w, theme)
    page = a.page(ctx, "reset-password: bad link")
    with a.step("reset-password: bad link"):
        page._expect.append((410, "/api/auth/reset/info"))
        page.goto(f"{app.base}/reset-password.html#token=notarealtoken")
        page.wait_for_selector("#invalid-step:not([hidden])")
        a.check(page, "reset-password: link that doesn't work")
    with a.step("reset-password: no token"):
        page.goto(f"{app.base}/reset-password.html")
        page.wait_for_selector("#invalid-step:not([hidden])")
        a.check(page, "reset-password: incomplete link")
    ctx.close()


@scenario("links")
def sc_verify(a, w, theme):
    app = a.apps["demo"]
    ctx = plain_ctx(a, w, theme)
    page = a.page(ctx, "verify-email: confirmed")
    link = a.verify_link()
    with a.step("verify-email: confirmed"):
        page.goto(link)
        page.wait_for_selector("#verify-result:not([hidden])")
        settle(page, 300)
        a.check(page, "verify-email: confirmed")
    with a.step("verify-email: used link"):
        page._expect.append((410, "/api/auth/verify-email"))
        page.goto("about:blank")  # (a link that differs only by its #fragment would not reload the page)
        page.goto(link)
        page.wait_for_selector("#error-summary:not([hidden])")
        a.check(page, "verify-email: link already used")
    with a.step("verify-email: incomplete link"):
        page.goto(f"{app.base}/verify-email.html")
        page.wait_for_selector("#error-summary:not([hidden])")
        a.check(page, "verify-email: incomplete link")
    ctx.close()


@scenario("links")
def sc_unsubscribe(a, w, theme):
    app = a.apps["demo"]
    token = a.fixtures["unsub_token"]
    ctx = plain_ctx(a, w, theme)
    page = a.page(ctx, "unsubscribe: confirm")
    with a.step("unsubscribe: confirm"):
        page.goto(f"{app.base}/unsubscribe?token={token}")
        settle(page, 200)
        a.check(page, "unsubscribe: confirm page")
    with a.step("unsubscribe: done"):
        page.click("button[type=submit]")
        page.wait_for_selector("text=You are unsubscribed")
        settle(page, 200)
        a.check(page, "unsubscribe: unsubscribed page")
        page.click("button[type=submit]")  # Subscribe again
        page.wait_for_selector("text=You are subscribed again")
        a.check(page, "unsubscribe: subscribed again page")
    with a.step("unsubscribe: bad link"):
        page._expect.append((410, "/unsubscribe"))
        page.goto(f"{app.base}/unsubscribe?token=bad")
        a.check(page, "unsubscribe: invalid link page")
    ctx.close()


# -- the walkthrough ----------------------------------------------------------------------------------------

@scenario("wizard")
def sc_wizard(a, w, theme):
    app = a.apps["demo"]
    user = a.fixtures["wizard"]
    api = Api(a.pw, app, user["email"], PASSWORD)
    ctx = plain_ctx(a, w, theme, login=(user["email"], PASSWORD))
    page = a.page(ctx, "welcome: step 1")
    total = 6
    n = 1
    while n <= total:
        api.call("PUT", "/api/account/onboarding", {"step": n})
        with a.step(f"welcome: step {n}"):
            page.goto("about:blank")
            page.goto(f"{app.base}/welcome.html")
            page.wait_for_selector("#step-title")
            settle(page, 300)
            if n == 1:
                total = page.locator("#wiz-steps li").count()
            title = page.inner_text("#step-title").replace("\n", " ")[:60]
            a.check(page, f"welcome: step {n} of {total} ({title.split(': ')[-1]})")
        n += 1
    with a.step("welcome: step with a problem"):
        api.call("PUT", "/api/account/onboarding", {"step": 2})
        page.goto("about:blank")
        page.goto(f"{app.base}/welcome.html")
        page.wait_for_selector("#step-title")
        page.click("#wiz-next")
        page.wait_for_selector("#error-summary:not([hidden])")
        a.check(page, "welcome: step 2 with an error")
    ctx.close()
    api.call("PUT", "/api/account/onboarding", {"step": 1})


@scenario("wizard")
def sc_wizard_short(a, w, theme):
    """The first admin's short path (theme, then keep or review the settings)."""
    app = a.apps["demo"]
    admin: Api = a.fixtures["admin"]
    ctx = admin_ctx(a, w, theme)
    page = a.page(ctx, "welcome: short path")
    try:
        for n in (1, 2):
            admin.call("PUT", "/api/account/onboarding", {"step": n})
            with a.step(f"welcome: first admin, step {n}"):
                page.goto("about:blank")
                page.goto(f"{app.base}/welcome.html")
                page.wait_for_selector("#step-title")
                settle(page, 300)
                a.check(page, f"welcome: first admin's short path, step {n}")
    finally:
        admin.call("PUT", "/api/account/onboarding", {"done": True})
        ctx.close()


# -- the job page ---------------------------------------------------------------------------------------

@scenario("job")
def sc_job(a, w, theme):
    app = a.apps["demo"]
    ctx = admin_ctx(a, w, theme)
    page = a.page(ctx, "job.html")
    page._expect.append((404, "/api/jobs/"))
    with a.step("job.html"):
        page.goto(f"{app.base}/job.html?id=1&ws={a.fixtures['ws']}")
        page.wait_for_selector("#job-company dl.profile")
        page.wait_for_function("() => !document.querySelector('#job-commute').textContent.includes('Working out')", timeout=60000)
        settle(page, 600)
        a.check(page, "job.html")
    with a.step("job.html: a job that isn't there"):
        page.goto(f"{app.base}/job.html?id=99999&ws={a.fixtures['ws']}")
        page.wait_for_selector("#job-error:not([hidden])")
        a.check(page, "job.html: job not found")
    ctx.close()


# -- billing ------------------------------------------------------------------------------------------------

def mock(page, path: str, body, status: int = 200) -> None:
    page.route(re.compile(re.escape(path)), lambda r: r.fulfill(status=status, content_type="application/json", body=json.dumps(body)))


PRICES = [{"plan": "monthly", "interval": "month", "amount": 900, "currency": "aud"},
          {"plan": "yearly", "interval": "year", "amount": 9000, "currency": "aud"}]


def person(**kw) -> dict:
    base = {"member": False, "reason": None, "free_searches_left": 2, "free_searches_limit": 3, "can_search": True, "message": None,
            "plan": None, "stripe_status": None, "current_period_end": None, "cancel_at_period_end": False, "grace_until": None,
            "free_period_until": None, "can_manage": False, "can_subscribe": True}
    return {**base, **kw}


@scenario("billing")
def sc_billing(a, w, theme):
    app = a.apps["demo"]
    states = {
        "an admin (the real answer)": None,
        "not a member, plans on offer": {"billing": {"enabled": True, "test_mode": True, "prices": PRICES}, "me": person()},
        "free searches used up": {"billing": {"enabled": True, "test_mode": False, "prices": PRICES},
                                  "me": person(free_searches_left=0, can_search=False, message="Free searches used up.")},
        "member with a plan that ends": {"billing": {"enabled": True, "test_mode": False, "prices": PRICES},
                                         "me": person(member=True, reason="subscribed", plan="monthly", stripe_status="active",
                                                      current_period_end="2026-11-07T00:00:00", cancel_at_period_end=True,
                                                      can_manage=True, can_subscribe=False)},
        "payment failed (grace)": {"billing": {"enabled": True, "test_mode": False, "prices": PRICES},
                                   "me": person(member=True, reason="grace", plan="yearly", stripe_status="past_due",
                                                current_period_end="2026-10-01T00:00:00", grace_until="2026-10-14T00:00:00",
                                                can_manage=True, can_subscribe=False)},
        "free period": {"billing": {"enabled": True, "test_mode": False, "prices": PRICES},
                        "me": person(member=True, reason="grant", free_period_until="2026-12-01T00:00:00")},
        "payments off": {"billing": {"enabled": False, "test_mode": False, "prices": []}, "me": person(member=True, reason="billing_off")},
    }
    for name, body in states.items():
        ctx = admin_ctx(a, w, theme)
        page = a.page(ctx, f"billing.html: {name}")
        if body is not None:
            mock(page, "/api/billing/status", body)
        with a.step(f"billing.html: {name}"):
            page.goto(f"{app.base}/billing.html")
            page.wait_for_function("() => document.querySelector('#me-loading').hidden")
            settle(page, 300)
            a.check(page, f"billing.html: {name}")
        ctx.close()
    ctx = admin_ctx(a, w, theme)
    page = a.page(ctx, "billing.html: back from Stripe")
    mock(page, "/api/billing/status", {"billing": {"enabled": True, "test_mode": True, "prices": PRICES}, "me": person()})
    mock(page, "/api/billing/verify", {"detail": "not done", "code": "not_complete"}, 409)
    page._expect.append((409, "/api/billing/verify"))
    with a.step("billing.html: payment not finished"):
        page.goto(f"{app.base}/billing.html?session_id=cs_test_1")
        page.wait_for_selector("#billing-result:not([hidden])")
        settle(page, 300)
        a.check(page, "billing.html: payment not finished")
    with a.step("billing.html: cancelled"):
        page.goto(f"{app.base}/billing.html?cancelled=1")
        page.wait_for_selector("#billing-result:not([hidden])")
        a.check(page, "billing.html: cancelled at Stripe")
    ctx.close()
    ctx = admin_ctx(a, w, theme)
    page = a.page(ctx, "billing.html: error")
    mock(page, "/api/billing/status", {"detail": "boom"}, 500)
    page._expect.append((500, "/api/billing/status"))
    with a.step("billing.html: error"):
        page.goto(f"{app.base}/billing.html")
        page.wait_for_selector("#billing-error:not([hidden])")
        a.check(page, "billing.html: could not load")
    ctx.close()


# -- the admin panel ----------------------------------------------------------------------------------------

@scenario("admin")
def sc_admin(a, w, theme):
    app = a.apps["demo"]
    ctx = admin_ctx(a, w, theme)
    page = a.page(ctx, "admin.html")
    with a.step("admin.html"):
        page.goto(f"{app.base}/admin.html")
        page.wait_for_selector("#users-grid")
        page.wait_for_function("() => document.querySelector('#usage-tables').children.length > 0", timeout=30000)
        settle(page, 800)
        a.check(page, "admin.html: every section")
    with a.step("admin.html: server section opened"):
        page.evaluate("() => document.querySelectorAll('details').forEach((d) => { d.open = true; })")
        settle(page, 500)
        a.check(page, "admin.html: every disclosure open")
    with a.step("admin.html: invitation made"):
        page.fill("#invite-email", "someone@example.invalid") if page.query_selector("#invite-email") else None
        btn = page.query_selector("#invite-create, #invite-form button[type=submit]")
        if btn:
            btn.click()
            page.wait_for_selector("#invite-result:not([hidden]), #invite-result-title")
            settle(page, 400)
            a.check(page, "admin.html: invitation link shown")
    with a.step("admin.html: membership dialog"):
        row = '#users-table [data-act="membership"]'
        page.click(f"{row} >> nth=1")
        page.wait_for_selector("#person-dialog[open]")
        settle(page, 600)
        a.check(page, "admin: membership dialog")
        page.click("#grant-save") if page.query_selector("#grant-save") else None
        settle(page, 400)
        a.check(page, "admin: membership dialog with a problem")
        page.keyboard.press("Escape")
    with a.step("admin.html: confirm dialog"):
        page.click('#users-table [data-act="disable"] >> nth=0')
        page.wait_for_selector("#confirm-dialog[open]")
        settle(page, 300)
        a.check(page, "admin: confirm dialog")
        page.keyboard.press("Escape")
    ctx.close()


@scenario("admin")
def sc_admin_forms(a, w, theme):
    """Every settings form submitted with a problem (the error under the field), and the other confirm dialogs."""
    app = a.apps["demo"]
    ctx = admin_ctx(a, w, theme)
    page = a.page(ctx, "admin.html: forms with problems")
    with a.step("admin.html: forms with problems"):
        page.goto(f"{app.base}/admin.html")
        page.wait_for_selector("#users-grid")
        settle(page, 800)
        page.evaluate("() => document.querySelectorAll('details').forEach((d) => { d.open = true; })")
        page.fill("#llm-model", "")
        page.fill("#llm-base-url", "not an address")
        page.click("#llm-save")
        page.fill("#invite-email", "not an address")
        page._expect.append((422, "/api/admin/invites"))  # (the address is refused, which the error under the field shows)
        page.click("#invite-create")
        for sel in ("form.key-field .key-save",):  # the key forms: nothing pasted
            for btn in page.query_selector_all(sel):
                btn.click()
        settle(page, 600)
        a.check(page, "admin.html: forms with problems")
    with a.step("admin.html: replace the server key dialog"):
        page.click("#rotate-key")
        page.wait_for_selector("#confirm-dialog[open]")
        settle(page, 300)
        a.check(page, "admin: replace the server key dialog")
        page.keyboard.press("Escape")
    with a.step("admin.html: delete unencrypted files dialog"):
        if page.is_visible("#plain-delete"):
            page.click("#plain-delete")
            page.wait_for_selector("#confirm-dialog[open]")
            a.check(page, "admin: delete the unencrypted files dialog")
            page.keyboard.press("Escape")
    ctx.close()


# -- the Settings tab for other kinds of account ----------------------------------------------------------

def user_ctx(a: Auditor, w: int, theme: str, user: dict):
    login = (user["email"], user["password"]) + ((user["secret"],) if user.get("secret") else ())
    return a.context(a.apps["demo"], width=w, theme=theme, login=login)


@scenario("settings")
def sc_settings_two_step(a, w, theme):
    """Two-step sign-in on: the card with the turn-off form, and the recovery-code card that asks for a code."""
    app = a.apps["demo"]
    user = new_account(a, a.unique("settings-mfa"), mfa=True)
    ctx = user_ctx(a, w, theme, user)
    page = a.page(ctx, "settings: two-step on")
    with a.step("settings: two-step sign-in on"):
        page.goto(f"{app.base}/#settings")
        page.wait_for_selector("#acct-mfa-off")
        settle(page, 500)
        a.check(page, "index: Settings, two-step sign-in on")
        page.click("#acct-mfa-off")
        page.wait_for_selector("#acct-mfa-off-form:not([hidden])")
        a.check(page, "index: Settings, turn-off form open")
        page.click("#acct-mfa-off-submit")
        page.wait_for_selector("#acct-mfa-off-form .field-error, #acct-mfa-off-form [aria-invalid=true]")
        a.check(page, "index: Settings, turn-off form with errors")
    ctx.close()


@scenario("settings")
def sc_settings_member(a, w, theme):
    """An ordinary account (no admin link), background access on, the digest on, an unconfirmed address."""
    app = a.apps["demo"]
    user = new_account(a, a.unique("settings-member"), background=True)
    ctx = user_ctx(a, w, theme, user)
    page = a.page(ctx, "settings: ordinary account")
    with a.step("settings: ordinary account"):
        page.goto(f"{app.base}/#settings")
        page.wait_for_selector("#acct-pw-form")
        page.wait_for_selector("#export-btn")
        settle(page, 800)
        a.check(page, "index: Settings, ordinary account (unconfirmed address, background on)")
    with a.step("settings: change address form"):
        page.click("#acct-addr-change")
        page.wait_for_selector("#acct-addr-form:not([hidden])")
        page.click("#acct-addr-send")
        page.wait_for_selector("#acct-addr-form [aria-invalid=true], #acct-addr-form .field-error")
        a.check(page, "index: Settings, change address form with an error")
    with a.step("settings: digest on"):
        page.check("#digest-switch")
        settle(page, 600)
        a.check(page, "index: Settings, digest emails on")
    with a.step("settings: delete account with an error"):
        page.click("#acct-del-submit")
        page.wait_for_selector("#acct-del-form [aria-invalid=true], #acct-del-form .field-error")
        a.check(page, "index: Settings, delete account with an error")
    with a.step("index: Jobs tab of a new account"):
        page.goto("about:blank")
        page.goto(f"{app.base}/#jobs")
        page.wait_for_selector("#jobs-empty:not([hidden]), .job-card")
        settle(page, 800)
        a.check(page, "index: Jobs tab, a new account (no jobs)")
        page.click("#account-btn")
        a.check(page, "index: account menu of an ordinary account")
    ctx.close()


# -- keyboard walkthroughs ------------------------------------------------------------------------------------

ACTIVE_JS = r"""() => {
  const el = document.activeElement;
  if (!el || el === document.body || el === document.documentElement) return null;
  const cs = getComputedStyle(el), r = el.getBoundingClientRect();
  let ring = (cs.outlineStyle !== 'none' && parseFloat(cs.outlineWidth) >= 1) || (cs.boxShadow && cs.boxShadow !== 'none');
  for (let up = el.parentElement; !ring && up && up !== document.body; up = up.parentElement) {  // a ring drawn round the field's box
    if (up.matches(':focus-within')) { const u = getComputedStyle(up); ring = u.outlineStyle !== 'none' && parseFloat(u.outlineWidth) >= 1; }
  }
  // Covered when no line of it (a link that wraps has one box per line) shows at its centre.
  const shown = (b) => { const top = document.elementFromPoint(Math.min(Math.max(b.left + b.width / 2, 0), innerWidth - 1),
                                                                Math.min(Math.max(b.top + b.height / 2, 0), innerHeight - 1));
                         return !top || top === el || el.contains(top) || top.contains(el); };
  const boxes = [...el.getClientRects()].filter((b) => b.width && b.height);
  const covered = boxes.length > 0 && !boxes.some(shown);
  const named = el.getAttribute('aria-label') || (el.textContent || '').trim().slice(0, 30) || el.getAttribute('placeholder') || '';
  const label = (el.id ? '#' + el.id : el.tagName.toLowerCase() + (typeof el.className === 'string' && el.className.trim() ? '.' + el.className.trim().split(/\s+/)[0] : '')) + (named ? ' "' + named.slice(0, 30) + '"' : '');
  const uid = el.id || (el.tagName + ':' + [...document.querySelectorAll(el.tagName)].indexOf(el));
  return { id: el.id, uid, label, tag: el.tagName, ring: !!ring, covered, inView: r.width > 0 && r.height > 0 && r.bottom > 0 && r.top < innerHeight,
           inDialog: !!el.closest('dialog[open]'), text: (el.textContent || '').trim().slice(0, 60), ariaInvalid: el.getAttribute('aria-invalid') };
}"""


def focus_now(page):
    return page.evaluate(ACTIVE_JS)


def key_problem(a: Auditor, page, text: str) -> None:
    a.report("keyboard", page, text=text)


def expect_focus(a: Auditor, page, what: str, ident: str | None = None, *, in_dialog: bool | None = None) -> dict | None:
    """Reports when focus isn't on ``#ident`` (or, with no ident, anywhere real); returns what has focus."""
    page.wait_for_timeout(150)
    info = focus_now(page)
    if info is None:
        key_problem(a, page, f"{what}: focus is lost (on the page's start)")
    elif ident is not None and info["id"] != ident:
        key_problem(a, page, f"{what}: focus is on {info['label']}, expected #{ident}")
    elif in_dialog is not None and info["inDialog"] != in_dialog:
        key_problem(a, page, f"{what}: focus {'is not' if in_dialog else 'is still'} in the dialog (on {info['label']})")
    return info


def walk(a: Auditor, page, what: str, until=None, limit: int = 60, keys: str = "Tab", modal: bool = False, whole_page: bool = False) -> list:
    """Presses Tab (or Shift+Tab) until ``until(info)``; every stop must show a focus ring and not be hidden or covered,
    and no element may come round twice before the end (a trap). With ``modal``, the walk is round an open dialog:
    every stop must stay inside it, and it ends when focus comes back to where it began. ``whole_page`` walks all of a
    page's stops and ends the same way, when the browser wraps round to the start."""
    stops, seen = [], set()
    for _ in range(limit):
        page.keyboard.press(keys)
        info = focus_now(page)
        if info is None:
            stops.append(None)
            break
        stops.append(info)
        if not info["ring"]:
            key_problem(a, page, f"{what}: no visible focus ring on {info['label']}")
        if info["covered"] or not info["inView"]:
            key_problem(a, page, f"{what}: {info['label']} is {'covered' if info['covered'] else 'out of view'} when focused")
        if modal and not info["inDialog"]:
            key_problem(a, page, f"{what}: Tab leaves the open dialog (to {info['label']})")
            break
        if until and until(info):
            break
        if info["uid"] in seen:
            if not (modal or whole_page):
                key_problem(a, page, f"{what}: focus goes round in a loop at {info['label']} (a trap?)")
            break
        seen.add(info["uid"])
    else:
        if until or modal or whole_page:
            key_problem(a, page, f"{what}: Tab never reached the target (or the end of the page) in {limit} presses")
    return stops


def order(stops: list) -> list[str]:
    return [s["id"] or s["label"] for s in stops if s]


def check_order(a: Auditor, page, what: str, stops: list, expected: list[str]) -> None:
    """The ids that must appear in this order among the stops (others may lie between)."""
    got = order(stops)
    pos = -1
    for want in expected:
        try:
            pos = got.index(want, pos + 1)
        except ValueError:
            key_problem(a, page, f"{what}: Tab order: {want} should come after {expected[max(0, expected.index(want) - 1)]}; stops were {got}")
            return


@scenario("keyboard")
def kb_sign_in(a, w, theme):
    app = a.apps["demo"]
    user = new_account(a, a.unique("kb-signin"), mfa=True)
    ctx = plain_ctx(a, w, theme)
    page = a.page(ctx, "keyboard: sign in with a code")
    page._expect += [(401, "/api/auth/me")]
    with a.step("keyboard: sign in with a code"):
        page.goto(f"{app.base}/login.html")
        page.wait_for_selector("#login-form:not([hidden])")
        settle(page, 300)
        expect_focus(a, page, "sign-in: the email field has focus when the page opens", "email")
        stops = walk(a, page, "sign-in, password step", until=lambda i: i["id"] == "password")
        page.keyboard.press("Shift+Tab")
        expect_focus(a, page, "sign-in: Shift+Tab goes back to the email field", "email")
        page.keyboard.type(user["email"])
        page.keyboard.press("Tab")
        page.keyboard.type(user["password"])
        stops = walk(a, page, "sign-in, rest of the form", until=lambda i: i["id"] == "login-submit")
        check_order(a, page, "sign-in", stops, ["remember", "login-submit"])
        page.keyboard.press("Enter")
        page.wait_for_selector("#mfa-form:not([hidden])")
        expect_focus(a, page, "sign-in: after the password, focus goes to the code field", "code")
        a.check(page, "keyboard: sign-in code step (focus in the code field)")
        stops = walk(a, page, "sign-in, code step", until=lambda i: i["id"] == "mfa-restart")
        check_order(a, page, "sign-in code step", stops, ["mfa-submit", "mfa-switch", "mfa-restart"])
        page.keyboard.press("Enter")  # Start again with your password
        page.wait_for_selector("#login-form:not([hidden])")
        expect_focus(a, page, "sign-in: Start again returns focus to the password field", "password")
        page.keyboard.type(user["password"])
        page.keyboard.press("Enter")
        page.wait_for_selector("#mfa-form:not([hidden])")
        page.keyboard.type(totp(user["secret"], time.time() + 30))
        page.keyboard.press("Enter")
        page.wait_for_url(re.compile(r".*/(#.*)?$"), timeout=20000)
        page.wait_for_selector(".job-card, #jobs-empty:not([hidden])")
        settle(page, 500)
        page.keyboard.press("Tab")
        info = focus_now(page)
        if not info or "Skip" not in info["text"]:
            key_problem(a, page, f"dashboard: the first Tab stop is {info and info['label']}, not the skip link")
        page.keyboard.press("Enter")
        page.wait_for_timeout(200)
        if not page.evaluate("() => !!(document.activeElement && document.activeElement.closest('#main'))"):
            key_problem(a, page, "dashboard: the skip link doesn't move focus into the main content")
    ctx.close()


@scenario("keyboard")
def kb_wizard(a, w, theme):
    app = a.apps["demo"]
    user = new_account(a, a.unique("kb-wizard"), onboarding=True)
    ctx = user_ctx(a, w, theme, user)
    page = a.page(ctx, "keyboard: walkthrough")
    with a.step("keyboard: the sign-up walkthrough"):
        page.goto(f"{app.base}/welcome.html")
        page.wait_for_selector("#step-title")
        expect_focus(a, page, "walkthrough: a step's heading takes focus when it appears", "step-title")
        walk(a, page, "walkthrough step 1", until=lambda i: i["id"] == "wiz-next")
        page.keyboard.press("Enter")  # Next
        page.wait_for_function("() => document.title.startsWith('Step 2')", timeout=15000)
        expect_focus(a, page, "walkthrough: step 2's heading takes focus", "step-title")
        walk(a, page, "walkthrough step 2", until=lambda i: i["id"] == "wiz-next")
        page.keyboard.press("Enter")  # no title yet: a problem
        page.wait_for_selector("#error-summary:not([hidden])")
        expect_focus(a, page, "walkthrough: a problem moves focus to the error summary", "error-summary")
        a.check(page, "keyboard: walkthrough step 2, error summary focused")
        page.keyboard.press("Tab")
        page.keyboard.press("Enter")  # the summary's link goes to the field
        expect_focus(a, page, "walkthrough: the summary's link moves focus to the field", "wz-titles")
        page.keyboard.type("Product Manager")
        page.keyboard.press("Enter")  # adds the tag; doesn't submit
        if "Product Manager" not in page.inner_text(".tag-input"):
            key_problem(a, page, "walkthrough: Enter in the title field doesn't add the title")
        walk(a, page, "walkthrough step 2 (after the title)", until=lambda i: i["id"] == "wiz-next")
        for n in (3, 4):
            page.keyboard.press("Enter")  # Next
            page.wait_for_function("(n) => document.title.startsWith('Step ' + n)", arg=n, timeout=15000)
            expect_focus(a, page, f"walkthrough: step {n}'s heading takes focus", "step-title")
            a.check(page, f"keyboard: walkthrough step {n} (focus on its heading)")
            walk(a, page, f"walkthrough step {n}", until=lambda i: i["id"] == "wiz-next")
        stops = walk(a, page, "walkthrough, backwards", until=lambda i: i["id"] == "wiz-back", keys="Shift+Tab", limit=8)
        info = focus_now(page)
        if not info or info["id"] != "wiz-back":
            key_problem(a, page, "walkthrough: Shift+Tab from Next never reaches Back")
        else:
            page.keyboard.press("Enter")
            page.wait_for_timeout(400)
            expect_focus(a, page, "walkthrough: Back puts focus on the earlier step's heading", "step-title")
    ctx.close()


@scenario("keyboard")
def kb_change_password(a, w, theme):
    app = a.apps["demo"]
    user = new_account(a, a.unique("kb-password"))
    ctx = user_ctx(a, w, theme, user)
    page = a.page(ctx, "keyboard: change password")
    page._expect += [(422, "/api/auth/password"), (403, "/api/auth/password")]
    with a.step("keyboard: change password"):
        page.goto(f"{app.base}/#settings")
        page.wait_for_selector("#acct-pw-form")
        settle(page, 500)
        page.focus("#acct-pw-current")
        page.keyboard.type(user["password"])
        stops = walk(a, page, "change password", until=lambda i: i["id"] == "acct-pw-submit")
        check_order(a, page, "change password", stops, ["acct-pw-new", "acct-pw-others", "acct-pw-submit"])
        page.keyboard.press("Enter")  # empty new password
        info = expect_focus(a, page, "change password: an empty field gets focus and an error", "acct-pw-new")
        if info and info["ariaInvalid"] != "true":
            key_problem(a, page, "change password: the field in error is not aria-invalid")
        a.check(page, "keyboard: change password with an error")
        page.keyboard.type("short")
        page.keyboard.press("Enter")
        expect_focus(a, page, "change password: a too-short password keeps focus in the field", "acct-pw-new")
        page.keyboard.press("Control+a")
        page.keyboard.type(user["password"] + " again")
        page.keyboard.press("Enter")
        page.wait_for_function("() => document.querySelector('#acct-pw-status').textContent.includes('Password changed')", timeout=20000)
        expect_focus(a, page, "change password: after success focus is on the card's heading", "acct-pw-heading")
        a.check(page, "keyboard: change password done")
    ctx.close()


@scenario("keyboard")
def kb_two_step_setup(a, w, theme):
    app = a.apps["demo"]
    user = new_account(a, a.unique("kb-twostep"))
    ctx = user_ctx(a, w, theme, user)
    page = a.page(ctx, "keyboard: set up two-step sign-in")
    page._expect += [(422, "/api/auth/mfa/enable")]
    with a.step("keyboard: set up two-step sign-in"):
        page.goto(f"{app.base}/#settings")
        page.wait_for_selector("#acct-mfa-start")
        settle(page, 500)
        page.focus("#acct-mfa-start")
        page.keyboard.press("Enter")
        page.wait_for_selector("#acct-mfa-on-form")
        expect_focus(a, page, "two-step setup: focus goes to the setup heading", "acct-mfa-setup-title")
        stops = walk(a, page, "two-step setup", until=lambda i: i["id"] == "acct-mfa-on-cancel")
        check_order(a, page, "two-step setup", stops, ["acct-mfa-copy", "acct-mfa-on-code", "acct-mfa-on-submit", "acct-mfa-on-cancel"])
        page.keyboard.press("Enter")  # Cancel
        page.wait_for_selector("#acct-mfa-start")
        expect_focus(a, page, "two-step setup: Cancel returns focus to the start button", "acct-mfa-start")
        page.keyboard.press("Enter")
        page.wait_for_selector("#acct-mfa-on-form")
        secret = page.evaluate("() => document.querySelector('.mfa-secret').textContent")
        walk(a, page, "two-step setup (again)", until=lambda i: i["id"] == "acct-mfa-on-code")
        page.keyboard.type("123456")
        page.keyboard.press("Enter")
        page.wait_for_selector("#acct-mfa-on-form [aria-invalid=true], #acct-mfa-on-form .field-error")
        expect_focus(a, page, "two-step setup: a wrong code keeps focus in the field", "acct-mfa-on-code")
        a.check(page, "keyboard: two-step setup, wrong code")
        page.keyboard.press("Control+a")
        page.keyboard.type(totp(secret))
        page.keyboard.press("Enter")
        page.wait_for_function("() => document.querySelector('#acct-mfa-status').textContent.includes('is on')", timeout=20000)
        expect_focus(a, page, "two-step setup: after turning on, focus is on the card's heading", "acct-mfa-heading")
    ctx.close()


@scenario("keyboard")
def kb_job_status(a, w, theme):
    ctx = admin_ctx(a, w, theme)
    page = dashboard(a, ctx)
    page.wait_for_selector(".info-btn")
    settle(page, 1500)
    page._expect += [(404, "/api/")]
    with a.step("keyboard: change and clear a job's status"):
        sel = ".job-card:first-of-type select[data-act=status]"
        before = page.eval_on_selector(sel, "(e) => e.value")
        page.focus(sel)
        expect_focus(a, page, "job status: the status select takes focus", None)
        page.keyboard.press("ArrowDown")
        page.wait_for_timeout(1200)
        info = expect_focus(a, page, "job status: after changing the status focus stays in the list", None)
        if info and info["tag"] == "BODY":
            key_problem(a, page, "job status: focus is lost after the status changed")
        a.check(page, "keyboard: after a job's status was changed")
        # and take the status away again (back to the first choice), with the keyboard alone
        now = page.evaluate("() => document.activeElement && document.activeElement.matches('select[data-act=status]') ? document.activeElement.value : null")
        if now is None:
            key_problem(a, page, "job status: after a change focus is no longer on the status list, so it can't be undone from there")
        elif now == before:
            key_problem(a, page, "job status: ArrowDown did not change the status")
        else:
            page.keyboard.press("ArrowUp")
            page.wait_for_timeout(1200)
            back = page.evaluate("() => document.activeElement && document.activeElement.matches('select[data-act=status]') ? document.activeElement.value : null")
            if back != before:
                key_problem(a, page, f"job status: taking the status away again left '{back}', not '{before}'")
            a.check(page, "keyboard: after a job's status was taken away")
    ctx.close()


@scenario("keyboard")
def kb_admin(a, w, theme):
    app = a.apps["demo"]
    ctx = admin_ctx(a, w, theme)
    page = a.page(ctx, "keyboard: admin")
    with a.step("keyboard: the admin people table and a dialog"):
        page.goto(f"{app.base}/admin.html")
        page.wait_for_selector("#users-grid")
        settle(page, 800)
        stops = walk(a, page, "admin page", until=lambda i: i["id"] == "usage-title" or "Membership" in i["text"], limit=80)
        stops = walk(a, page, "admin people table", until=lambda i: i["text"] == "Membership", limit=80)
        for _ in range(3):  # to a second person's Membership button
            walk(a, page, "admin people table", until=lambda i: i["text"] == "Membership", limit=40)
        opener = focus_now(page)
        page.evaluate("() => { window.__opener = document.activeElement; }")
        page.keyboard.press("Enter")
        page.wait_for_selector("#person-dialog[open]")
        expect_focus(a, page, "membership dialog: focus goes to its heading", "person-title", in_dialog=True)
        walk(a, page, "membership dialog", limit=60, modal=True)
        page.keyboard.press("Shift+Tab")
        page.keyboard.press("Escape")
        page.wait_for_function("() => !document.querySelector(\'#person-dialog\').open")
        back = page.evaluate("() => document.activeElement === window.__opener")
        if not back:
            key_problem(a, page, f"membership dialog: Escape does not return focus to the button that opened it (it is on {focus_now(page) and focus_now(page)['label']})")
        a.check(page, "keyboard: admin, after the membership dialog closed")
        # a confirm dialog from a row's Disable button
        page.focus('#users-table [data-act="disable"]')
        page.evaluate("() => { window.__opener = document.activeElement; }")
        page.keyboard.press("Enter")
        page.wait_for_selector("#confirm-dialog[open]")
        expect_focus(a, page, "confirm dialog: focus goes to its heading", "confirm-title", in_dialog=True)
        walk(a, page, "confirm dialog", limit=30, modal=True)
        page.focus("#confirm-cancel")
        page.keyboard.press("Enter")  # Cancel
        page.wait_for_function("() => !document.querySelector(\'#confirm-dialog\').open")
        if not page.evaluate("() => document.activeElement === window.__opener"):
            key_problem(a, page, "confirm dialog: Cancel does not return focus to the button that opened it")
    ctx.close()


@scenario("keyboard")
def kb_whole_pages(a, w, theme):
    """Tab from the top to the bottom of the main pages: a focus ring on every stop, none hidden under the sticky header."""
    app = a.apps["demo"]
    ctx = admin_ctx(a, w, theme)
    page = a.page(ctx, "keyboard: whole pages")
    pages = [("the Jobs tab", "/#jobs", ".job-card"), ("the Settings tab", "/#settings", "#export-btn"),
             ("the job page", f"/job.html?id=1&ws={a.fixtures['ws']}", "#job-company dl.profile"),
             ("the admin page", "/admin.html", "#users-grid"), ("the billing page", "/billing.html", "#me-loading[hidden]")]
    for name, path, ready in pages:
        with a.step(f"keyboard: Tab through {name}"):
            page.goto("about:blank")
            page.goto(app.base + path)
            page.wait_for_selector(ready, state="attached")  # ("attached": #me-loading[hidden] is never visible)
            settle(page, 1500)
            page.evaluate("() => { document.activeElement && document.activeElement.blur(); window.scrollTo(0, 0); }")
            walk(a, page, f"Tab through {name}", whole_page=True, limit=2500)  # (the admin page lists every account the audit made)
    ctx.close()


@scenario("keyboard")
def kb_map(a, w, theme):
    """The map has keyboard routes for everything: arrows pan, + and - zoom, Tab leaves it, the list is the way to a place."""
    ctx = admin_ctx(a, w, theme)
    page = dashboard(a, ctx)
    page._expect += [(404, "/api/")]
    with a.step("keyboard: map"):
        open_tab(page, "map")
        page.wait_for_selector(".map-job-row")
        settle(page, 800)
        stops = walk(a, page, "map tab", until=lambda i: i["id"] == "map", limit=40)
        if not stops or stops[-1] is None:
            key_problem(a, page, "map: the map region can't be reached with Tab")
        else:
            before = page.evaluate("() => state.map.getZoom()")
            page.keyboard.press("ArrowLeft")
            page.keyboard.press("+")
            page.wait_for_timeout(500)
            if page.evaluate("() => state.map.getZoom()") == before:
                key_problem(a, page, "map: the + key doesn't zoom the focused map")
            walk(a, page, "map, after the map", until=lambda i: i["id"] != "map" and not i["label"].startswith("a"), limit=6)
            page.keyboard.press("Tab")
            info = focus_now(page)
            if info and info["id"] == "map":
                key_problem(a, page, "map: Tab does not leave the map (a trap)")
        page.focus('.map-job-row [data-act="show"]')
        page.keyboard.press("Enter")
        page.wait_for_timeout(600)
        a.check(page, "keyboard: map, Show pressed from the list")
    ctx.close()


@scenario("keyboard")
def kb_export(a, w, theme):
    app = a.apps["demo"]
    user = new_account(a, a.unique("kb-export"))
    ctx = user_ctx(a, w, theme, user)
    page = a.page(ctx, "keyboard: export")
    with a.step("keyboard: download your data"):
        page.goto(f"{app.base}/#settings")
        page.wait_for_selector("#export-btn")
        settle(page, 500)
        page.focus("#export-btn")
        with page.expect_download(timeout=60000) as dl:
            page.keyboard.press("Enter")
        if not dl.value.suggested_filename.endswith(".zip"):
            key_problem(a, page, f"export: unexpected file {dl.value.suggested_filename}")
        page.wait_for_function("() => document.querySelector('#export-status').textContent.length > 0", timeout=20000)
        expect_focus(a, page, "export: focus stays on the button after the download", "export-btn")
        a.check(page, "keyboard: after the export download")
    ctx.close()


@scenario("keyboard")
def kb_dialogs(a, w, theme):
    """Dashboard dialogs: Escape closes, focus returns to the button that opened them; the chat panel too."""
    ctx = admin_ctx(a, w, theme)
    page = dashboard(a, ctx)
    page.wait_for_selector(".info-btn")
    settle(page, 1500)  # the company icons arrive after the cards and redraw them
    first = ".job-card:first-of-type"
    for act, ready in (("desc", "#modal[open]"), ("letter", "#modal[open]"), ("company", "#modal[open]")):
        with a.step(f"keyboard: {act} dialog"):
            page.focus(f'{first} [data-act="{act}"]')
            page.evaluate("() => { window.__opener = document.activeElement; }")
            page.keyboard.press("Enter")
            page.wait_for_selector(ready)
            page.wait_for_timeout(500)
            info = expect_focus(a, page, f"{act} dialog: focus moves into the dialog", None, in_dialog=True)
            walk(a, page, f"{act} dialog", limit=60, modal=True)
            page.keyboard.press("Escape")
            page.wait_for_function("() => !document.querySelector(\'#modal\').open")
            page.wait_for_timeout(200)
            if not page.evaluate("() => document.activeElement === window.__opener"):
                now = focus_now(page)
                key_problem(a, page, f"{act} dialog: Escape does not return focus to the button that opened it (on {now and now['label']})")
    with a.step("keyboard: chat panel"):
        page.focus(f'{first} [data-act="chat"]')
        page.evaluate("() => { window.__opener = document.activeElement; }")
        page.keyboard.press("Enter")
        page.wait_for_selector(".chat-msg.assistant")
        page.wait_for_timeout(400)
        expect_focus(a, page, "chat: focus moves into the chat panel", "chat-input")
        page.keyboard.press("Escape")
        page.wait_for_timeout(300)
        if page.is_visible("#chat-panel"):
            key_problem(a, page, "chat: Escape does not close the chat panel")
        elif not page.evaluate("() => document.activeElement === window.__opener"):
            now = focus_now(page)
            key_problem(a, page, f"chat: closing returns focus to {now and now['label']}, not the Chat button")
    with a.step("keyboard: confirm your password"):
        page.focus("#account-btn")
        page.evaluate("() => { window.__opener = document.activeElement; window.__confirm = confirmPassword(); }")
        page.wait_for_selector("dialog.confirm-dialog[open]")
        page.wait_for_timeout(300)
        expect_focus(a, page, "confirm password: focus moves into the dialog", None, in_dialog=True)
        walk(a, page, "confirm password dialog", limit=30, modal=True)
        page.keyboard.press("Escape")
        page.wait_for_timeout(300)
        if not page.evaluate("() => document.activeElement === window.__opener"):
            now = focus_now(page)
            key_problem(a, page, f"confirm password: Escape returns focus to {now and now['label']}, not the button that opened it")
    ctx.close()


# --------------------------------------------------------------------------
# The run
# --------------------------------------------------------------------------

def report(findings: Findings, show_review: bool) -> int:
    items = findings.unique()
    bad = [i for i in items if i["kind"] != "axe-review"]
    review = [i for i in items if i["kind"] == "axe-review"]
    print(f"\n{findings.runs} axe runs over {len(findings.states)} states.")

    def show(kind_items, heading):
        if not kind_items:
            return
        print(f"\n{heading}")
        groups: dict = {}
        for it in kind_items:
            key = (it["kind"], it.get("rule") or "", it.get("text") if it["kind"] != "axe" else it.get("target"))
            groups.setdefault(key, []).append(it)
        for (kind, rule, what), hits in groups.items():
            where = sorted({f"{h['state']} [{h['width']}px {h['theme']}]" for h in hits})
            head = f"{kind}{' ' + rule if rule else ''}: {what}"
            print(f"  - {head[:400]}")
            first = hits[0]
            if first.get("help"):
                print(f"      {first['help']}  ({first.get('impact')})")
            if first.get("html"):
                print(f"      {first['html'][:200]}")
            if kind == "axe" and first.get("text"):
                print(f"      {first['text'][:260]}")
            print(f"      in {len(where)} place(s), e.g. {'; '.join(where[:3])}")

    show(bad, "PROBLEMS")
    if show_review:
        show(review, "AXE 'NEEDS REVIEW' (could not decide)")
    else:
        rules = sorted({i['rule'] for i in review})
        if review:
            print(f"\n({len(review)} axe 'needs review' results, rules: {', '.join(rules)}; --review shows them)")
    if not bad:
        print("\nNo problems.")
    return 1 if bad else 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("node_modules", type=Path, nargs="?", help="folder with axe-core/ (and leaflet/dist) inside")
    parser.add_argument("--widths", default=",".join(map(str, DEFAULT_WIDTHS)))
    parser.add_argument("--themes", default=",".join(DEFAULT_THEMES), help="comma-separated theme ids, or 'all' (28 themes)")
    parser.add_argument("--only", default="", help="comma-separated scenario groups (--list shows them)")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--best-practice", action="store_true", help="also run axe's best-practice rules (reported separately)")
    parser.add_argument("--review", action="store_true", help="also list axe's 'needs review' results")
    parser.add_argument("--json", type=Path, help="write every finding to this file")
    parser.add_argument("--chrome", type=Path, help="a Chromium executable, if Playwright's own isn't installed")
    parser.add_argument("--keep", action="store_true", help="keep the temporary folder")
    args = parser.parse_args()
    if args.list:
        print("\n".join(f"{g}: {len(f)} scenario(s)" for g, f in GROUPS.items()))
        return
    if not args.node_modules or not (args.node_modules / "axe-core" / "axe.min.js").is_file():
        sys.exit("Give the folder that holds axe-core/axe.min.js (npm install axe-core in an empty folder).")
    widths = [int(x) for x in args.widths.split(",") if x.strip()]
    themes = [x.strip() for x in args.themes.split(",") if x.strip()]
    if themes == ["all"]:  # every theme the picker offers
        common = (ROOT / "frontend" / "common.js").read_text(encoding="utf-8")
        themes = [t for t in re.findall(r"\{ id: '([\w-]+)'", common) if t != "system"]
    wanted = [x.strip() for x in args.only.split(",") if x.strip()] or list(GROUPS)
    unknown = [x for x in wanted if x not in GROUPS]
    if unknown:
        sys.exit(f"Unknown group(s) {', '.join(unknown)}; choose from {', '.join(GROUPS)}")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sys.exit("Playwright is missing: python3 -m pip install playwright && python3 -m playwright install chromium")

    sys.stdout.reconfigure(line_buffering=True)
    work = Path(tempfile.mkdtemp(prefix="jobhunt-a11y-"))
    mail = MailCatcher()
    apps = {"demo": App(work, "demo", demo=True, mail=mail), "empty": App(work, "empty", demo=False, mail=mail)}
    findings = Findings()
    code = 1
    try:
        print(f"Starting the demo app and an empty one in {work} ...")
        for app in apps.values():
            app.start()
        with sync_playwright() as p:
            browser = launch_browser(p, args.chrome)
            a = Auditor(p, browser, apps, mail, args.node_modules, findings)
            a.best_practice = args.best_practice
            setup(a)
            for group in wanted:
                for width in widths:
                    for theme in themes:
                        a.width, a.theme, a.group = width, theme, group
                        for fn in GROUPS[group]:
                            try:
                                fn(a, width, theme)
                            except Exception as exc:  # a scenario that can't reach its state is itself a finding
                                findings.add(kind="scenario-error", state=fn.__name__, width=width, theme=theme, group=group,
                                             text=f"{type(exc).__name__}: {str(exc)[:300]}")
                print(f"  {group}: {widths} px x {len(themes)} theme(s) done ({len(findings.items)} findings so far)")
            browser.close()
        for name, app in apps.items():  # a stopped app explains a run full of "connection refused"
            if app.proc and app.proc.poll() is not None:
                tail = app.log_path.read_text(encoding="utf-8", errors="replace")[-1500:]
                print(f"\nThe {name} app stopped during the run (exit code {app.proc.returncode}); the end of its log:\n{tail}")
                findings.add(kind="app-stopped", state=name, width=0, theme="", group="", text=f"exit code {app.proc.returncode}")
        code = report(findings, args.review)
        if args.json:
            args.json.write_text(json.dumps(findings.items, indent=1), encoding="utf-8")
    finally:
        for app in apps.values():
            app.stop()
        mail.stop()
        if args.keep:
            print(f"Kept {work}")
        else:
            shutil.rmtree(work, ignore_errors=True)
    sys.exit(code)


def launch_browser(p, chrome: Path | None):
    """Playwright's own Chromium; or, when its version doesn't match the installed one, any Chromium that is."""
    if chrome is None:
        try:
            return p.chromium.launch()
        except Exception as exc:
            home = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or Path.home() / ".cache" / "ms-playwright")
            found = sorted(home.glob("chromium*/chrome-linux*/headless_shell")) + sorted(home.glob("chromium*/chrome-linux*/chrome"))
            if not found:
                raise exc
            chrome = found[0]
            print(f"  (using {chrome}: Playwright's own Chromium is not installed)")
    return p.chromium.launch(executable_path=str(chrome))


def new_account(a: Auditor, name: str, *, onboarding: bool = False, mfa: bool = False, background: bool = False) -> dict:
    """Signs up a fictional person through the app's own sign-up (the mode is open), and sets the account up."""
    demo = a.apps["demo"]
    email = f"{name}@example.invalid"
    anon = a.pw.request.new_context()
    r = anon.post(f"{demo.base}/api/auth/signup", data={"email": email, "password": PASSWORD, "name": name.title()})
    if r.status != 201:
        raise RuntimeError(f"sign-up of {email} answered {r.status}: {r.text()[:200]}")
    user = {"email": email, "codes": r.json()["recovery_codes"], "name": name, "password": PASSWORD}
    api = Api(a.pw, demo, email, PASSWORD)
    api.call("POST", "/api/auth/recovery-codes/ack", {})
    if not onboarding:
        api.call("PUT", "/api/account/onboarding", {"done": True})
    if background:
        api.call("POST", "/api/account/background", {"enabled": True})
    if mfa:
        setup_ = api.call("POST", "/api/auth/mfa/setup", {})
        user["secret"] = setup_["secret"]
        api.call("POST", "/api/auth/mfa/enable", {"code": totp(setup_["secret"])})
    return user


def mint_unsubscribe_token(app: App) -> str:
    """The signed token in a digest email's unsubscribe link, made with the app's own key file."""
    code = ("from backend import users\nfrom backend.db import SessionLocal, EmailPrefs\nusers.startup()\n"
            "row = SessionLocal().query(EmailPrefs).order_by(EmailPrefs.user_id).first()\n"
            "print(users.server_keys().sign('unsub', row.unsubscribe_id))")
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=app.env(), capture_output=True, text=True, timeout=60)
    if out.returncode:
        raise RuntimeError(f"could not make an unsubscribe token: {out.stderr[-300:]}")
    return out.stdout.strip().splitlines()[-1]


def setup(a: Auditor) -> None:
    """Accounts and settings the scenarios need."""
    demo = a.apps["demo"]
    admin = Api(a.pw, demo, DEMO_EMAIL, DEMO_PASSWORD)
    admin.call("POST", "/api/auth/recovery-codes/ack", {})
    admin.call("PUT", "/api/account/onboarding", {"done": True})  # the demo admin has finished the walkthrough
    admin.call("PUT", "/api/admin/signup", {"mode": "open"})
    ws = next(w for w in admin.call("GET", "/api/workspaces") if w["name"] == "Dummy workspace")
    a.fixtures["ws"] = ws["id"]
    a.fixtures["empty_ws"] = admin.call("POST", "/api/workspaces", {"name": "Empty audit workspace"})["id"]
    a.fixtures["admin"] = admin
    a.fixtures["mfa"] = new_account(a, "mfa-user", mfa=True)
    a.fixtures["keeps"] = new_account(a, "keeps-data", background=True)
    a.fixtures["wipes"] = new_account(a, "wipes-data")
    a.fixtures["fresh"] = new_account(a, "fresh-codes")
    a.fixtures["wizard"] = new_account(a, "wizard", onboarding=True)
    # A digest subscription, for the unsubscribe page
    Api(a.pw, demo, DEMO_EMAIL, DEMO_PASSWORD).call("PUT", "/api/account/digest", {"enabled": True})
    # (the key file is locked while the app runs: stop it for a moment, make the token, start it on the same database)
    demo.stop()
    a.fixtures["unsub_token"] = mint_unsubscribe_token(demo)
    demo.start(fresh=False)


if __name__ == "__main__":
    main()

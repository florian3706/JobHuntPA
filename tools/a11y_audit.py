#!/usr/bin/env python3
"""WCAG 2.2 AA audit of every page and dialog in a real browser (axe-core + Playwright), on fictional data.

    python3 tools/a11y_audit.py NODE_MODULES [--widths 320,1280] [--themes vanilla-dark-aa,vanilla-light-aa,light,dark,dyslexia,deuteranomaly]
                                [--only index,login] [--json report.json] [--keep]

NODE_MODULES is a folder that holds ``axe-core/axe.min.js`` (``npm install axe-core`` in an empty folder makes it)
and, if the machine can't reach unpkg.com, ``leaflet/dist`` (``npm install leaflet@1.9.4``): the map is then served
from there. This is a developer tool: it needs no entry in requirements.txt, and nothing in the app or the tests
imports it. It needs Playwright with Chromium (setup installs both).

What it does
1. Builds a throwaway demo database (tools/demo_data.py), starts the app on it (like tools/readme_screenshots.py)
   and a second, empty app for the first-run page.
   Nothing touches data/ or the real .env; the scheduler is off; the LLM and the map's tile servers are
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
import json
import mimetypes
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parent.parent
TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))
import readme_screenshots as shots  # noqa: E402  (its server environment: demo account, fake LLM, no scheduler)

DEMO_EMAIL, DEMO_PASSWORD = shots.DEMO_EMAIL, shots.DEMO_PASSWORD
PASSWORD = "an audit password 1"
AXE_TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"]
DEFAULT_THEMES = ["vanilla-dark-aa", "vanilla-light-aa", "light", "dark", "dyslexia", "deuteranomaly"]
DEFAULT_WIDTHS = [320, 1280]
HEIGHT = 900
REAL_DATA = (ROOT / "data").resolve()


# --------------------------------------------------------------------------
# The apps under audit
# --------------------------------------------------------------------------

def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# The app, as uvicorn runs it, except that the sign-in limit is lifted: an audit signs in dozens of times in a few
# minutes, which the real limit (10 sign-in requests in 5 minutes) would refuse. Nothing else differs.
LAUNCH = """
import sys, uvicorn
from backend import ratelimit
for name in ("AUTH",):
    getattr(ratelimit, name).rules = [(10 ** 6, 60)]
uvicorn.run("backend.app:app", host="127.0.0.1", port=int(sys.argv[1]), log_level="warning")
"""


class App:
    """One running copy of the app on its own throwaway database."""

    def __init__(self, work: Path, name: str, *, demo: bool):
        self.work = work / name
        self.work.mkdir(parents=True)
        self.db = (self.work / "app.db").resolve()
        self.env_file = (self.work / "app.env").resolve()
        if REAL_DATA in self.db.parents or ROOT in self.env_file.parents:
            sys.exit("Refusing to use the real data folder or .env.")
        self.port = free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.demo = demo
        self.proc: subprocess.Popen | None = None
        self.log_path = self.work / "server.log"

    def env(self) -> dict:
        env = shots.server_env(self.db, self.env_file)
        env.update({"PUBLIC_BASE_URL": self.base, "JOBHUNT_DATA_DIR": str(self.work)})
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
        r = self.ctx.post("/api/auth/login", data={"name": email, "password": password})
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
  // How many pixels of text each clipping box hides (a box that clips its children's text counts, not only one with text of its own).
  const clips = () => {
    const found = new Map();
    for (const el of document.querySelectorAll('body *')) {
      if (!el.textContent.trim() || el.closest('.visually-hidden')) continue;
      const cs = getComputedStyle(el), r = el.getBoundingClientRect();
      // (a box of a pixel or two is a deliberate "visually hidden" clip, not text a reader loses)
      if (r.width <= 2 || r.height <= 2 || cs.visibility === 'hidden') continue;
      const cut = (v) => v === 'hidden' || v === 'clip';
      const hidden = Math.max(cut(cs.overflowY) ? el.scrollHeight - el.clientHeight : 0, cut(cs.overflowX) ? el.scrollWidth - el.clientWidth : 0);
      if (hidden > 0) {
        found.set(el.tagName.toLowerCase() + (el.id ? '#' + el.id : typeof el.className === 'string' && el.className ? '.' + el.className.trim().split(/\s+/)[0] : '') + ' "' + el.textContent.trim().slice(0, 30) + '"', hidden);
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
  return { newlyCut: [...after].filter(([k, px]) => px > (before.get(k) || 0)).map(([k, px]) => k + ' (' + px + 'px more hidden)').slice(0, 5),
           widened: wideAfter > innerWidth + 1 && wideAfter > wide ? wideAfter : 0 };
}"""

# Leaflet is served from the local copy when asked for; tiles are a grey square; fonts come from the real thing.
GREY_TILE = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYGD4DwABBAEAfbLI3wAAAABJRU5ErkJggg==")


class Auditor:
    def __init__(self, playwright, browser, apps: dict, node_modules: Path, findings: Findings):
        self.pw, self.browser, self.apps = playwright, browser, apps
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
            r = ctx.request.post(f"{app.base}/api/auth/login", data={"name": login[0], "password": login[1]})
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


    # -- one page ---------------------------------------------------------------
    def page(self, ctx, state: str):
        page = ctx.new_page()
        page.set_default_timeout(15000)
        page._audit_state = state
        page._expect = []  # (status, url fragment) pairs this state is meant to cause
        page.on("pageerror", lambda e: self.report("pageerror", page, text=str(e)[:300]))
        page.on("console", lambda m: self.on_console(page, m))
        # (the map cancels tiles it no longer needs when the view changes: ERR_ABORTED on a tile is not a failure)
        page.on("requestfailed", lambda r: self.report("requestfailed", page, text=f"{r.url[:140]} {r.failure}")
                if "favicon" not in r.url and not ("tile.openstreetmap.org" in r.url and r.failure == "net::ERR_ABORTED") else None)
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


def owner_ctx(a: Auditor, w: int, theme: str, **kw):
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
    ctx = owner_ctx(a, w, theme)
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
    with a.step("index: ghost-job circles and a why? list"):
        if not page.query_selector(".job-card details.ghost-why"):
            raise RuntimeError("no job is flagged as a possible ghost job (plant_ghost_signs did not take)")
        if not page.query_selector(".job-card .ghost-dot"):
            raise RuntimeError("the job cards show no ghost-job circle")
        a.check(page, "index: Jobs tab, ghost-job circles")
        for summary in page.query_selector_all("details.ghost-why summary")[:2]:
            summary.click()
        settle(page, 300)
        a.check(page, "index: Jobs tab, Why? lists open")
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
    ctx = owner_ctx(a, w, theme)
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
    with a.step("index: Settings, errors under fields"):
        page.click("#acct-pw-submit")
        page.wait_for_selector("#acct-pw-form .field-error, #acct-pw-form [aria-invalid=true]")
        a.check(page, "index: Settings, password form with errors")
    ctx.close()


@scenario("index")
def sc_dialogs(a, w, theme):
    ctx = owner_ctx(a, w, theme)
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
    ctx = owner_ctx(a, w, theme)
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
        page.click("#starts-form button[type=submit]")  # a starting point with no place typed and no pin chosen
        page.wait_for_selector("#start-place[aria-invalid=true]")
        a.check(page, "index: Search setup, starting point with no place")
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
    ctx = owner_ctx(a, w, theme)
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
        fill_submit(page, {"#name": "nobody@example.invalid", "#password": "not the password"}, "#login-submit")
        page.wait_for_function("() => document.querySelector('#error-summary-list li')?.textContent.length > 0")
        settle(page, 300)
        a.check(page, "login: wrong password error")
    with a.step("login: show password"):
        page.fill("#password", "something")
        page.click(".pw-toggle")
        a.check(page, "login: password shown")
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
    with a.step("recover: a code that isn't right"):
        user = a.fixtures["fresh"]
        page._expect.append((401, "/api/auth/recover"))
        fill_submit(page, {"#name": user["email"], "#recovery-code": "AAAA-BBBB-CCCC-DDDD", "#password": "another long password"},
                    "#recover-submit")
        page.wait_for_function("() => document.querySelector('#error-summary-list li')?.textContent.length > 0")
        settle(page, 300)
        a.check(page, "recover: wrong recovery code")
    ctx.close()


@scenario("auth")
def sc_codes_step(a, w, theme):
    """The recovery-codes step (first-run.html when signed in with codes pending)."""
    app = a.apps["demo"]
    user = a.fixtures["fresh"]
    api = Api(a.pw, app, user["email"], PASSWORD)
    api.call("POST", "/api/auth/recovery-codes", {"password": PASSWORD})  # new codes are pending
    ctx = plain_ctx(a, w, theme, login=(user["email"], PASSWORD))
    page = a.page(ctx, "first-run: recovery codes")
    with a.step("first-run: recovery codes"):
        page.goto(f"{app.base}/first-run.html")
        page.wait_for_selector("#show-step:not([hidden])")  # the codes come back for the password
        settle(page, 300)
        a.check(page, "first-run: recovery codes, password asked")
        page.click("#show-submit")
        page.wait_for_selector("#error-summary:not([hidden])")
        a.check(page, "first-run: recovery codes, password missing")
        page.fill("#show-password", PASSWORD)
        page.click("#show-submit")
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
    """The first account's short path on an existing install (theme, then keep or review the settings)."""
    app = a.apps["demo"]
    owner: Api = a.fixtures["owner"]
    ctx = owner_ctx(a, w, theme)
    page = a.page(ctx, "welcome: short path")
    try:
        for n in (1, 2):
            owner.call("PUT", "/api/account/onboarding", {"step": n})
            with a.step(f"welcome: first account, step {n}"):
                page.goto("about:blank")
                page.goto(f"{app.base}/welcome.html")
                page.wait_for_selector("#step-title")
                settle(page, 300)
                a.check(page, f"welcome: first account's short path, step {n}")
    finally:
        owner.call("PUT", "/api/account/onboarding", {"done": True})
        ctx.close()


# -- the job page ---------------------------------------------------------------------------------------

@scenario("job")
def sc_job(a, w, theme):
    app = a.apps["demo"]
    ctx = owner_ctx(a, w, theme)
    page = a.page(ctx, "job.html")
    page._expect.append((404, "/api/jobs/"))
    with a.step("job.html"):
        page.goto(f"{app.base}/job.html?id=1&ws={a.fixtures['ws']}")
        page.wait_for_selector("#job-company dl.profile")
        page.wait_for_function("() => !document.querySelector('#job-commute').textContent.includes('Listing the commute links')", timeout=60000)
        # the "Is this a real vacancy?" section (planted signs), its reasons collapsed until asked for
        page.wait_for_selector("#job-ghost-section:not([hidden]) .ghost-check.found", state="attached")
        settle(page, 600)
        a.check(page, "job.html")
    with a.step("job.html: why this rating, opened"):
        page.click("#ghost-why summary")
        page.wait_for_selector("#job-ghost-section .ghost-check.found")
        settle(page, 300)
        a.check(page, "job.html: why this rating open")
    with a.step("job.html: a job that isn't there"):
        page.goto(f"{app.base}/job.html?id=99999&ws={a.fixtures['ws']}")
        page.wait_for_selector("#job-error:not([hidden])")
        a.check(page, "job.html: job not found")
    ctx.close()


# -- the Settings tab: this JobHuntPA's settings (they were the admin panel's) ----------------------------------

@scenario("settings")
def sc_install_settings(a, w, theme):
    """The AI provider, AI usage, public address, server key and name cards, then every form submitted
    with a problem (the error under the field)."""
    app = a.apps["demo"]
    ctx = owner_ctx(a, w, theme)
    page = a.page(ctx, "settings: this JobHuntPA")
    with a.step("settings: this JobHuntPA's cards"):
        page.goto(f"{app.base}/#settings")
        page.wait_for_selector("#llm-form")
        page.wait_for_function("() => document.querySelector('#usage-tiles').children.length > 0", timeout=30000)
        settle(page, 800)
        a.check(page, "index: Settings, this JobHuntPA's cards")
    with a.step("settings: forms with problems"):
        page.fill("#llm-model", "")
        page.fill("#llm-base-url", "not an address")
        page.click("#llm-save")
        page._expect.append((422, "/api/settings/address"))  # (refused, which the error under the field shows)
        page.fill("#public-url", "http://not-https.example")
        page.click("#address-save")
        page.fill("#acct-name-input", "")
        page.click("#acct-name-submit")
        settle(page, 600)
        a.check(page, "index: Settings, forms with problems")
    ctx.close()


# -- the Settings tab for other kinds of account ----------------------------------------------------------

def user_ctx(a: Auditor, w: int, theme: str, user: dict):
    return a.context(a.apps["demo"], width=w, theme=theme, login=(user["email"], user["password"]))


@scenario("settings")
def sc_settings_member(a, w, theme):
    """Another account (as an older install may have), background access on."""
    app = a.apps["demo"]
    user = new_account(a, a.unique("settings-member"), background=True)
    ctx = user_ctx(a, w, theme, user)
    page = a.page(ctx, "settings: ordinary account")
    with a.step("settings: ordinary account"):
        page.goto(f"{app.base}/#settings")
        page.wait_for_selector("#acct-pw-form")
        page.wait_for_selector("#export-btn")
        settle(page, 800)
        a.check(page, "index: Settings, ordinary account (background on)")
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
    user = new_account(a, a.unique("kb-signin"))
    ctx = plain_ctx(a, w, theme)
    page = a.page(ctx, "keyboard: sign in")
    page._expect += [(401, "/api/auth/me")]
    with a.step("keyboard: sign in"):
        page.goto(f"{app.base}/login.html")
        page.wait_for_selector("#login-form:not([hidden])")
        settle(page, 300)
        expect_focus(a, page, "sign-in: the name field has focus when the page opens", "name")
        stops = walk(a, page, "sign-in, password step", until=lambda i: i["id"] == "password")
        page.keyboard.press("Shift+Tab")
        expect_focus(a, page, "sign-in: Shift+Tab goes back to the name field", "name")
        page.keyboard.type(user["email"])
        page.keyboard.press("Tab")
        page.keyboard.type(user["password"])
        stops = walk(a, page, "sign-in, rest of the form", until=lambda i: i["id"] == "login-submit")
        check_order(a, page, "sign-in", stops, ["remember", "login-submit"])
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
def kb_job_status(a, w, theme):
    ctx = owner_ctx(a, w, theme)
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
def kb_ghost_why(a, w, theme):
    ctx = owner_ctx(a, w, theme)
    page = dashboard(a, ctx)
    with a.step("keyboard: a ghost-job Why? list"):
        card = page.locator(".job-card:has(details.ghost-why)").first
        summary = card.locator("details.ghost-why summary")
        summary.scroll_into_view_if_needed()
        summary.focus()
        info = expect_focus(a, page, "ghost Why?: the summary takes focus", None)
        if info and not info["ring"]:
            key_problem(a, page, "ghost Why?: no visible focus ring on the summary")
        page.keyboard.press("Enter")
        if not card.locator("details.ghost-why").evaluate("(d) => d.open"):
            key_problem(a, page, "ghost Why?: Enter does not open the list")
        expect_focus(a, page, "ghost Why?: focus stays on the summary after opening", None)
        a.check(page, "keyboard: a ghost-job Why? list opened with Enter")
        page.keyboard.press("Space")
        if card.locator("details.ghost-why").evaluate("(d) => d.open"):
            key_problem(a, page, "ghost Why?: Space does not close the list")
        stops = walk(a, page, "ghost Why?: Tab goes on from the summary", limit=3)
        if any(s is None for s in stops):
            key_problem(a, page, "ghost Why?: Tab from the summary left the page")
    ctx.close()


@scenario("keyboard")
def kb_install_settings(a, w, theme):
    """The Settings tab's cards for this JobHuntPA (AI provider to the server key), by keyboard alone."""
    app = a.apps["demo"]
    ctx = owner_ctx(a, w, theme)
    page = a.page(ctx, "keyboard: this JobHuntPA's settings")
    with a.step("keyboard: this JobHuntPA's settings"):
        page.goto(f"{app.base}/#settings")
        page.wait_for_selector("#llm-form")
        settle(page, 800)
        page.focus("#llm-provider")
        walk(a, page, "AI provider to the server key", until=lambda i: i["id"] == "rotate-key", limit=120)
    ctx.close()


@scenario("keyboard")
def kb_whole_pages(a, w, theme):
    """Tab from the top to the bottom of the main pages: a focus ring on every stop, none hidden under the sticky header."""
    app = a.apps["demo"]
    ctx = owner_ctx(a, w, theme)
    page = a.page(ctx, "keyboard: whole pages")
    pages = [("the Jobs tab", "/#jobs", ".job-card"), ("the Settings tab", "/#settings", "#export-btn"),
             ("the job page", f"/job.html?id=1&ws={a.fixtures['ws']}", "#job-company dl.profile")]
    for name, path, ready in pages:
        with a.step(f"keyboard: Tab through {name}"):
            page.goto("about:blank")
            page.goto(app.base + path)
            page.wait_for_selector(ready, state="attached")
            settle(page, 1500)
            page.evaluate("() => { document.activeElement && document.activeElement.blur(); window.scrollTo(0, 0); }")
            walk(a, page, f"Tab through {name}", whole_page=True, limit=2500)
    ctx.close()


@scenario("keyboard")
def kb_map(a, w, theme):
    """The map has keyboard routes for everything: arrows pan, + and - zoom, Tab leaves it, the list is the way to a place."""
    ctx = owner_ctx(a, w, theme)
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
    ctx = owner_ctx(a, w, theme)
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
    parser.add_argument("--themes", default=",".join(DEFAULT_THEMES), help="comma-separated theme ids, or 'all' (the 30 AA themes; the exact palettes are not held to AA)")
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
    if themes == ["all"]:  # every theme with the AA badge (the exact palettes are offered as they are)
        common = (ROOT / "frontend" / "common.js").read_text(encoding="utf-8")
        themes = [t for t in re.findall(r"\{ id: '([\w-]+)'[^}]*\baa: true\b", common) if t != "system"]
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
    apps = {"demo": App(work, "demo", demo=True), "empty": App(work, "empty", demo=False)}
    findings = Findings()
    code = 1
    try:
        print(f"Starting the demo app and an empty one in {work} ...")
        for app in apps.values():
            app.start()
        with sync_playwright() as p:
            browser = launch_browser(p, args.chrome)
            a = Auditor(p, browser, apps, args.node_modules, findings)
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


def new_account(a: Auditor, name: str, *, onboarding: bool = False, background: bool = False) -> dict:
    """A fictional extra account, written straight into the demo database the way an older install's sign-up left
    one (the app itself makes only the first: one account per install), and set up through the app's API. A small
    process does the writing with the running app's key file, read but not locked (the app holds the lock)."""
    demo = a.apps["demo"]
    email = f"{name}@example.invalid"
    code = (
        "import json\nfrom backend import crypto, keys, users\nfrom backend.db import SessionLocal, Workspace\n"
        "users._server_keys = keys.ServerKeys.load()\ndb = SessionLocal()\n"
        f"account = users._insert_account(db, name={email!r}, password={PASSWORD!r}, created_via='open', "
        "background=False)\n"
        "with crypto.as_user(account.user_id, account.dek):\n"
        "    db.add(Workspace(name=users.DEFAULT_WORKSPACE, owner_id=account.user_id))\n    db.commit()\n"
        "print('CODES ' + json.dumps(account.recovery_codes))\n")
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=demo.env(), capture_output=True, text=True,
                         timeout=60)
    lines = [line for line in out.stdout.splitlines() if line.startswith("CODES ")]
    if out.returncode or not lines:
        raise RuntimeError(f"could not add the account {email}: {out.stderr[-300:]}")
    user = {"email": email, "codes": json.loads(lines[-1][6:]), "name": name, "password": PASSWORD}
    api = Api(a.pw, demo, email, PASSWORD)
    api.call("POST", "/api/auth/recovery-codes/ack", {})
    if not onboarding:
        api.call("PUT", "/api/account/onboarding", {"done": True})
    if background:
        api.call("POST", "/api/account/background", {"enabled": True})
    return user


def plant_ghost_signs(app: App) -> None:
    """Ghost-job signs on a few demo jobs, so the Jobs tab and the job page show circles of each colour,
    the Why? list and the "Is this a real vacancy?" section with signs found: one job open for 100 days with evergreen wording in its ad (likely), two open
    for 50 and 65 days (possible). Written straight to the database while the app is stopped."""
    code = (
        "import json\nfrom datetime import datetime, timedelta\nfrom backend import users, ghost\n"
        "from backend.db import SessionLocal, Job, Posting\nusers.startup()\ndb = SessionLocal()\nnow = datetime.utcnow()\n"
        "for job_id, days, evergreen in ((1, 100, True), (2, 65, False), (3, 50, False)):\n"
        "    pid = db.query(Job.posting_id).filter(Job.id == job_id).scalar()\n    p = db.get(Posting, pid) if pid else None\n"
        "    if p is None:\n        continue\n"
        "    p.first_seen, p.listed_since, p.last_listed_at, p.closed_at = now - timedelta(days=days), None, now, None\n"
        "    if evergreen:\n        p.ghost_json = json.dumps({'text': [ghost.signal('evergreen_wording', 3, 'The ad says it is always hiring',\n"
        "            'Phrases like \"ongoing recruitment\" and \"talent pool\" suggest a standing list, not one vacancy.')]})\n"
        "db.commit()\n")
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=app.env(), capture_output=True, text=True, timeout=60)
    if out.returncode:
        raise RuntimeError(f"could not plant ghost-job signs: {out.stderr[-300:]}")


def setup(a: Auditor) -> None:
    """Accounts and settings the scenarios need."""
    demo = a.apps["demo"]
    owner = Api(a.pw, demo, DEMO_EMAIL, DEMO_PASSWORD)
    owner.call("POST", "/api/auth/recovery-codes/ack", {})
    owner.call("PUT", "/api/account/onboarding", {"done": True})  # the demo account has finished the walkthrough
    ws = next(w for w in owner.call("GET", "/api/workspaces") if w["name"] == "Dummy workspace")
    a.fixtures["ws"] = ws["id"]
    a.fixtures["empty_ws"] = owner.call("POST", "/api/workspaces", {"name": "Empty audit workspace"})["id"]
    a.fixtures["owner"] = owner
    a.fixtures["fresh"] = new_account(a, "fresh-codes")
    a.fixtures["wizard"] = new_account(a, "wizard", onboarding=True)
    # (the key file is locked while the app runs: stop it for a moment, plant the signs, start it on the same database)
    demo.stop()
    plant_ghost_signs(demo)
    demo.start(fresh=False)


if __name__ == "__main__":
    main()

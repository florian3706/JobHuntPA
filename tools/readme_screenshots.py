#!/usr/bin/env python3
"""Take the README screenshots (docs/screenshots/*.png) from a fictional demo database.

    python3 tools/readme_screenshots.py [--out DIR] [--only jobs,map,...] [--keep]

What it does:
1. Builds a NEW temporary database with tools/demo_data.py (a "Dummy workspace" full of made-up
   jobs; nothing from your real data/jobhunt.db).
2. Starts the app on a free local port against that database, with fake LLM settings, no commute
   API keys and the scheduler off, so nothing can reach an LLM or scrape a site.
3. Opens it in headless Chromium (Playwright) and saves the screenshots.
4. Stops the server and deletes the temporary database.

Needs the app's requirements plus Playwright's Chromium (setup installs both) and Pillow
(``pip install pillow``; a developer tool only, so it is not in requirements.txt). The browser
fetches Leaflet (unpkg.com), map tiles (openstreetmap.org) and, for the Dyslexia theme, a font
(Google Fonts); the server asks OSRM's public server for the driving route on the job page.

Images (all dark theme unless noted, 1440x900 at 1x; the job page uses 1240 wide):
    jobs, run-report, job-page, map (light theme), search-setup, search-schedule, company-sources, themes, chat,
    cover-letter
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
TOOLS = Path(__file__).resolve().parent
REAL_DB = (ROOT / "data" / "jobhunt.db").resolve()
DEFAULT_OUT = ROOT / "docs" / "screenshots"

VIEWPORT = {"width": 1440, "height": 900}
JOB_PAGE_VIEWPORT = {"width": 1240, "height": 900}
CONTENT_X, CONTENT_W = 120, 1200          # the dashboard's centred 1200 px column at 1440 wide
SIZE_LIMIT = 400_000                      # bytes; bigger PNGs are reduced to a 256-colour palette
# Words that must never appear in the images (e.g. your suburb), as a case-insensitive regex.
# Kept out of the repo: set JOBHUNT_SCREENSHOT_FORBIDDEN="suburb|street" when you run this.
FORBIDDEN = os.getenv("JOBHUNT_SCREENSHOT_FORBIDDEN", "").strip()
ALLOWED_HOSTS = ("unpkg.com", "tile.openstreetmap.org", "fonts.googleapis.com", "fonts.gstatic.com")

# theme id -> shown in themes.png, in this order
MONTAGE_THEMES = ["light", "catppuccin-mocha", "catppuccin-latte", "dyslexia", "deuteranomaly-dark", "monochromacy"]
ALL_SHOTS = ["jobs", "run-report", "job-page", "map", "search-setup", "search-schedule", "company-sources", "themes",
             "chat", "cover-letter"]


# --------------------------------------------------------------------------
# Server
# --------------------------------------------------------------------------

def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def server_env(db: Path) -> dict:
    """The app's environment: the demo database, fake LLM settings, no commute keys, no scheduler.
    backend/config.py only fills in .env values for keys that are missing here, so setting every
    key keeps the real ones out."""
    env = dict(os.environ)
    env.update({
        "JOBHUNT_DB_PATH": str(db), "DATABASE_URL": f"sqlite:///{db}", "JOBHUNT_SCHEDULER": "off",
        "LLM_API_KEY": "demo-key", "LLM_BASE_URL": "https://llm.example.com/v1", "LLM_MODEL": "example-model",
        "LLM_REASONING_EFFORT": "", "LLM_TIMEOUT_S": "30", "LLM_CONCURRENCY": "1",
        "TFNSW_API_KEY": "", "TOMTOM_API_KEY": "",
    })
    return env


def start_server(db: Path, port: int, log_path: Path) -> subprocess.Popen:
    log = open(log_path, "w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "backend.app:app", "--host", "127.0.0.1", "--port", str(port),
         "--log-level", "warning"],
        cwd=ROOT, env=server_env(db), stdout=log, stderr=subprocess.STDOUT)
    deadline = time.time() + 60
    while time.time() < deadline:
        if proc.poll() is not None:
            sys.exit(f"The server stopped at start-up:\n{log_path.read_text(encoding='utf-8')[-2000:]}")
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=2) as r:
                if r.status == 200:
                    return proc
        except OSError:
            time.sleep(0.4)
    proc.terminate()
    sys.exit("The server did not start within 60 seconds.")


def stop_server(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


COUNTED = ("geocode_cache", "http_cache", "fit_results", "cover_letters", "chat_messages", "search_runs",
           "company_profiles", "app_settings", "jobs", "documents")


def counts(db: Path) -> dict:
    con = sqlite3.connect(db)
    try:
        return {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in COUNTED}
    finally:
        con.close()


# --------------------------------------------------------------------------
# Image helpers
# --------------------------------------------------------------------------

def save_png(img, path: Path) -> int:
    """Save as an optimised PNG; reduce to a 256-colour palette when it is over SIZE_LIMIT."""
    from PIL import Image

    img = img.convert("RGB")
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    data = buf.getvalue()
    if len(data) > SIZE_LIMIT:
        small = io.BytesIO()
        img.quantize(colors=256, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE).save(
            small, "PNG", optimize=True)
        data = small.getvalue()
    path.write_bytes(data)
    return len(data)


# --------------------------------------------------------------------------
# Browser helpers
# --------------------------------------------------------------------------

class Shooter:
    def __init__(self, browser, base: str, ws_id: int, out: Path):
        self.browser, self.base, self.ws_id, self.out = browser, base, ws_id, out
        self.hosts: set[str] = set()
        self.problems: list[str] = []
        self.sizes: dict[str, int] = {}

    def context(self, theme: str = "dark", viewport: dict | None = None):
        """A fresh browser profile. The theme and workspace are set in localStorage before any page
        script runs, so the page starts in them (setting them afterwards would race its start-up)."""
        ctx = self.browser.new_context(viewport=viewport or VIEWPORT, device_scale_factor=1, locale="en-AU",
                                       timezone_id="Australia/Sydney")
        ctx.add_init_script(
            "try { localStorage.setItem('jobhunt.theme', %s); localStorage.setItem('jobhunt.ws', %s);"
            " localStorage.setItem('jobhunt.mapMode', 'jobs'); } catch (e) {}" % (json.dumps(theme), json.dumps(str(self.ws_id))))
        ctx.on("request", lambda r: self.hosts.add(urlsplit(r.url).netloc))
        return ctx

    def page(self, ctx):
        page = ctx.new_page()
        page.on("pageerror", lambda e: self.problems.append(f"page error: {e}"))
        page.on("console", lambda m: self.problems.append(f"console {m.type}: {m.text[:160]}") if m.type == "error" else None)
        page.on("response", lambda r: self.problems.append(f"HTTP {r.status} {r.url[:120]}")
                if r.status >= 400 and "tile.openstreetmap" not in r.url else None)
        return page

    # -- waiting ---------------------------------------------------------------
    def wait_dashboard(self, page) -> None:
        page.wait_for_selector(".job-card")
        page.wait_for_function("""() => document.querySelector('#next-scheduled') && !document.querySelector('#next-scheduled').hidden
            && document.querySelector('#run-status').textContent.length > 0
            && !document.querySelector('#scorer-status').textContent.includes('checking')
            && document.querySelector('.info-btn.ok, .info-btn.warn')""", timeout=30000)
        self.settle(page)

    def settle(self, page, extra_ms: int = 500) -> None:
        page.wait_for_load_state("networkidle")
        page.evaluate("document.fonts.ready")
        page.wait_for_timeout(extra_ms)

    def wait_tiles(self, page) -> None:
        page.wait_for_function("""() => { const t = [...document.querySelectorAll('img.leaflet-tile')];
            return t.length > 0 && t.every(i => i.complete && i.naturalWidth > 0); }""", timeout=45000)
        page.wait_for_timeout(900)  # tile fade-in

    # -- capturing ----------------------------------------------------------------
    def grab(self, page, box: tuple[int, int, int, int], full_page: bool = True):
        """PIL image of a region given in page coordinates (x, y, width, height). The text inside the
        region is checked for anything that must not appear in the public README images."""
        from PIL import Image

        x, y, w, h = box
        self.check_text(page, box)
        png = page.screenshot(full_page=full_page)
        img = Image.open(io.BytesIO(png))
        return img.crop((x, y, min(x + w, img.width), min(y + h, img.height)))

    def check_text(self, page, box: tuple[int, int, int, int]) -> None:
        if not FORBIDDEN:
            return
        x, y, w, h = box
        found = page.evaluate("""([x, y, w, h, pattern]) => {
            const re = new RegExp(pattern, 'i'), hits = new Set();
            for (const el of document.querySelectorAll('body *')) {
              const r = el.getBoundingClientRect();
              if (r.width === 0 && r.height === 0) continue;   // not displayed
              if (r.right + scrollX < x || r.left + scrollX > x + w || r.bottom + scrollY < y || r.top + scrollY > y + h) continue;
              const own = [...el.childNodes].filter(n => n.nodeType === 3).map(n => n.textContent).join(' ');
              for (const t of [own, el.placeholder, el.title, el.value]) if (t && re.test(t)) hits.add(t.trim().slice(0, 80));
            }
            return [...hits]; }""", [x, y, w, h, FORBIDDEN])
        for text in found:
            self.problems.append(f"FORBIDDEN TEXT in an image: {text!r}")

    def rect(self, page, selector: str) -> dict:
        return page.evaluate("""(sel) => { const r = document.querySelector(sel).getBoundingClientRect();
            return { x: r.left + scrollX, y: r.top + scrollY, w: r.width, h: r.height }; }""", selector)

    def save(self, name: str, img) -> None:
        size = save_png(img, self.out / f"{name}.png")
        self.sizes[name] = size
        print(f"  {name}.png  {img.width}x{img.height}  {size / 1024:.0f} KB")


# --------------------------------------------------------------------------
# The screenshots
# --------------------------------------------------------------------------

def shot_jobs(s: Shooter) -> None:
    ctx = s.context()
    page = s.page(ctx)
    page.goto(f"{s.base}/#jobs")
    s.wait_dashboard(page)
    # From the top of the page to the bottom of the first row of job cards.
    bottom = page.evaluate("""() => { const cards = [...document.querySelectorAll('.job-card')];
        const top = cards[0].getBoundingClientRect().top; return Math.max(...cards.filter(c =>
          Math.abs(c.getBoundingClientRect().top - top) < 2).map(c => c.getBoundingClientRect().bottom)) + scrollY; }""")
    s.save("jobs", s.grab(page, (CONTENT_X, 0, CONTENT_W, int(bottom) + 14)))
    ctx.close()


def shot_run_report(s: Shooter) -> None:
    """The run card with the scheduled search's per-source report opened."""
    ctx = s.context()
    page = s.page(ctx)
    page.goto(f"{s.base}/#jobs")
    s.wait_dashboard(page)
    page.click("#run-report summary")
    page.mouse.move(5, 5)
    s.settle(page, 300)
    card = s.rect(page, ".run-card")
    s.save("run-report", s.grab(page, (CONTENT_X, int(card["y"]) - 10, CONTENT_W, int(card["h"]) + 20)))
    ctx.close()


def shot_job_page(s: Shooter) -> None:
    ctx = s.context(viewport=JOB_PAGE_VIEWPORT)
    page = s.page(ctx)
    page.goto(f"{s.base}/job.html?id=1&ws={s.ws_id}")
    page.wait_for_selector("#job-company dl.profile")
    page.wait_for_function("!document.querySelector('#job-commute').textContent.includes('Working out')", timeout=45000)
    s.settle(page)
    s.wait_tiles(page)
    height = page.evaluate("document.documentElement.scrollHeight")
    s.save("job-page", s.grab(page, (0, 0, JOB_PAGE_VIEWPORT["width"], height)))
    ctx.close()


def shot_map(s: Shooter) -> None:
    ctx = s.context("light")  # the thin pin radii show up best in the light theme
    page = s.page(ctx)
    page.goto(f"{s.base}/#jobs")
    s.wait_dashboard(page)
    page.check("#filter-show-excluded")          # grey dots for the jobs the criteria exclude
    page.click('.tab[data-tab="map"]')
    page.wait_for_selector(".map-job-row")
    s.wait_tiles(page)
    page.click(".leaflet-control-zoom-out")      # the pins' radii fit in view
    page.mouse.move(5, 5)
    s.settle(page)
    s.wait_tiles(page)
    bottom = page.evaluate("""() => { const rows = document.querySelectorAll('.map-job-row');
        return rows[Math.min(5, rows.length - 1)].getBoundingClientRect().bottom + scrollY; }""")
    s.save("map", s.grab(page, (CONTENT_X, 0, CONTENT_W, int(bottom) + 14)))
    ctx.close()


def shot_search(s: Shooter) -> None:
    ctx = s.context()
    page = s.page(ctx)
    page.goto(f"{s.base}/#search")
    page.wait_for_selector("#sources-list .source-row")
    page.wait_for_function("document.querySelector('#sched-status').textContent.includes('Next search')")
    s.settle(page)
    form = s.rect(page, "#search-form")
    s.save("search-setup", s.grab(page, (CONTENT_X, 0, CONTENT_W, int(form["y"] + form["h"]) + 14)))
    card = s.rect(page, "#schedule-card")
    s.save("search-schedule", s.grab(page, (CONTENT_X, int(card["y"]) - 10, CONTENT_W, int(card["h"]) + 20)))
    box = page.evaluate("""() => { const r = document.querySelector('#sources-list').closest('.card').getBoundingClientRect();
        return { y: r.top + scrollY, h: r.height }; }""")
    s.save("company-sources", s.grab(page, (CONTENT_X, int(box["y"]) - 10, CONTENT_W, int(box["h"]) + 20)))
    ctx.close()


def shot_chat(s: Shooter) -> None:
    ctx = s.context()
    page = s.page(ctx)
    page.goto(f"{s.base}/#jobs")
    s.wait_dashboard(page)
    page.click('.job-card:first-of-type [data-act="chat"]')
    page.wait_for_selector(".chat-msg.assistant")
    page.evaluate("document.querySelector('#chat-messages').scrollTop = 0; window.scrollTo(0, 0)")
    page.mouse.move(5, 5)
    s.settle(page)
    s.save("chat", s.grab(page, (0, 0, VIEWPORT["width"], VIEWPORT["height"]), full_page=False))
    ctx.close()


def shot_cover_letter(s: Shooter) -> None:
    ctx = s.context()
    page = s.page(ctx)
    page.goto(f"{s.base}/#jobs")
    s.wait_dashboard(page)
    page.click('.job-card:first-of-type [data-act="letter"]')
    page.wait_for_function("document.querySelector('#cl-text') && document.querySelector('#cl-text').value.length > 100")
    page.mouse.move(5, 5)
    s.settle(page)
    box = s.rect(page, ".modal-box")
    pad = 18
    s.save("cover-letter", s.grab(page, (int(box["x"]) - pad, int(box["y"]) - pad, int(box["w"]) + 2 * pad, int(box["h"]) + 2 * pad)))
    ctx.close()


def shot_themes(s: Shooter) -> None:
    """The top of the Jobs tab (header, then the status chips and the first job cards) in six themes."""
    from PIL import Image

    tiles = []
    for theme in MONTAGE_THEMES:
        ctx = s.context(theme, viewport={"width": 760, "height": 520})
        page = s.page(ctx)
        page.goto(f"{s.base}/#jobs")
        s.wait_dashboard(page)
        if theme.startswith("dyslexia"):
            page.wait_for_function("[...document.fonts].some(f => f.family.includes('Atkinson') && f.status === 'loaded')", timeout=20000)
        # Same crop every time: scroll so the status chips sit just under the (sticky) header.
        page.evaluate("""() => { const head = document.querySelector('.app-header').getBoundingClientRect().height;
            window.scrollTo(0, document.querySelector('#status-chips').getBoundingClientRect().top + scrollY - head - 10); }""")
        page.mouse.move(5, 5)
        s.settle(page, 300)
        s.check_text(page, (0, 0, 760, 520))
        label = page.evaluate("(id) => THEMES.find(t => t.id === id).label", theme)
        tiles.append((label, Image.open(io.BytesIO(page.screenshot()))))
        ctx.close()

    tile_w, gap = 464, 18
    cells = []
    for label, img in tiles:
        small = img.convert("RGB").resize((tile_w, round(img.height * tile_w / img.width)), Image.LANCZOS)
        buf = io.BytesIO()
        small.save(buf, "PNG")
        cells.append((label, base64.b64encode(buf.getvalue()).decode()))
    html = f"""<!doctype html><meta charset="utf-8"><style>
      body {{ margin: 0; background: #2b303b; font: 600 17px system-ui, "Segoe UI", Roboto, sans-serif; color: #f1f3f7; }}
      .grid {{ display: grid; grid-template-columns: repeat(2, {tile_w}px); gap: {gap}px; padding: {gap}px; width: max-content; }}
      figure {{ margin: 0; }} img {{ display: block; border-radius: 6px; }}
      figcaption {{ text-align: center; padding: 9px 0 2px; }}
    </style><div class="grid">{''.join(f'<figure><img src="data:image/png;base64,{b}"><figcaption>{l}</figcaption></figure>' for l, b in cells)}</div>"""
    ctx = s.context(viewport={"width": 2 * tile_w + 3 * gap, "height": 800})
    page = ctx.new_page()
    page.set_content(html)
    page.wait_for_timeout(300)
    img = Image.open(io.BytesIO(page.locator(".grid").screenshot()))
    s.save("themes", img)
    ctx.close()


SHOTS = {"jobs": shot_jobs, "run-report": shot_run_report, "job-page": shot_job_page, "map": shot_map, "search": shot_search, "chat": shot_chat,
         "cover-letter": shot_cover_letter, "themes": shot_themes}
GROUP = {"search-setup": "search", "search-schedule": "search", "company-sources": "search"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="folder for the PNGs (default docs/screenshots)")
    parser.add_argument("--only", default="", help=f"comma-separated subset of: {', '.join(ALL_SHOTS)}")
    parser.add_argument("--keep", action="store_true", help="keep the temporary database and server log")
    args = parser.parse_args()
    sys.stdout.reconfigure(line_buffering=True)

    wanted = [x.strip() for x in args.only.split(",") if x.strip()] or ALL_SHOTS
    unknown = [x for x in wanted if x not in ALL_SHOTS]
    if unknown:
        sys.exit(f"Unknown image(s): {', '.join(unknown)}. Choose from: {', '.join(ALL_SHOTS)}")
    try:
        from PIL import Image  # noqa: F401
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        sys.exit(f"Missing {exc.name}: install it with  python3 -m pip install pillow playwright  "
                 "(and  python3 -m playwright install chromium)")

    args.out.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="jobhunt-readme-"))
    db = (work / "demo.db").resolve()
    if db == REAL_DB or REAL_DB.parent in db.parents:
        sys.exit("Refusing to use the real data folder.")
    proc = None
    try:
        print(f"Building the demo database in {work} ...")
        subprocess.run([sys.executable, str(TOOLS / "demo_data.py"), str(db)], check=True, cwd=ROOT)
        before = counts(db)
        port = free_port()
        proc = start_server(db, port, work / "server.log")
        base = f"http://127.0.0.1:{port}"
        workspaces = json.load(urllib.request.urlopen(f"{base}/api/workspaces"))
        ws = next((w for w in workspaces if w["name"] == "Dummy workspace"), None)
        if ws is None or len(workspaces) != 1:
            sys.exit(f"Expected only a 'Dummy workspace', got {[w['name'] for w in workspaces]}")
        print(f"Server on {base}, workspace {ws['id']} ({ws['jobs']} jobs). Taking screenshots ...")

        with sync_playwright() as p:
            browser = p.chromium.launch()
            shooter = Shooter(browser, base, ws["id"], args.out)
            done = set()
            for name in wanted:
                key = GROUP.get(name, name)
                if key in done:
                    continue
                done.add(key)
                SHOTS[key](shooter)
            browser.close()

        after = counts(db)
        print("Checks:")
        stray = [h for h in sorted(shooter.hosts) if not (h.startswith("127.0.0.1") or h.endswith(ALLOWED_HOSTS))]
        print("  browser contacted:", ", ".join(sorted(shooter.hosts)))
        if stray:
            print("  UNEXPECTED hosts:", ", ".join(stray))
        changed = {t: (before[t], after[t]) for t in COUNTED if before[t] != after[t]}
        print("  rows added by the app while browsing (should be none):", changed or "none")
        if shooter.problems:
            print("  browser problems:")
            for line in dict.fromkeys(shooter.problems):
                print("   ", line)
        big = {n: sz for n, sz in shooter.sizes.items() if sz > SIZE_LIMIT}
        if big:
            print("  over the size limit:", {n: f"{sz // 1024} KB" for n, sz in big.items()})
    finally:
        if proc is not None:
            stop_server(proc)
        if args.keep:
            print(f"Kept {work}")
        else:
            shutil.rmtree(work, ignore_errors=True)
    print(f"Done: {args.out}")


if __name__ == "__main__":
    main()

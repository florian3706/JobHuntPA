"""The browser for JavaScript-only careers pages: Playwright's Chromium, downloaded into the data folder when a careers
page first needs it and the person agrees (docs/design/open-source.md, Phase H). Nothing downloads it by itself: setup
doesn't, and neither does a search.

    GET  /api/settings/browser          {available, installing, error, needed, declined, folder, downloaded, system,
                                         using}: ``using`` is "downloaded" (Playwright's own Chromium), "system" (a
                                         Chromium or Chrome installed on this computer) or null
    POST /api/settings/browser/install  starts the download (202 {installing: true}); 200 when it is there already
    POST /api/settings/browser/decline  "Not now": the offer on the Search setup tab goes away; the careers pages that
                                        need it keep saying so (``NEEDED``), and the Settings card can still get it

**Who may call:** signed in (it is a setting of the install, like the AI provider; there are no roles).

Where it lives: ``<data folder>/browsers`` (``PLAYWRIGHT_BROWSERS_PATH`` is set to it for this process once it holds
Chromium). A Chromium an older setup downloaded to Playwright's own folder (``~/.cache/ms-playwright`` and the like)
still counts, and so does a ``PLAYWRIGHT_BROWSERS_PATH`` set by whoever runs the app. When a careers page needs the
browser and it isn't there, ``backend/scraping/http.py`` raises FetchError(``NEEDED``) and records ``needed``, which
makes the dashboard offer the download. The download is ``python -m playwright install chromium`` (Playwright's own
command, from its own servers), run in the background; its output is never logged (it names folders), except the one
line that says Playwright has no build for this system (it names the system, nothing else).

A Chromium or Chrome installed on the computer counts too, when Playwright's own isn't there (``system_browser``: the
usual names on the PATH, and the usual places on a Mac and on Windows): Playwright drives it through ``executable_path``
(``launch_options``). That is what a Debian 11 Raspberry Pi gets, where Playwright has no Chromium build at all, with
Raspberry Pi OS's ``chromium-browser``. Playwright's own build is preferred when both are there: it is the version
Playwright was tested with.
"""
from __future__ import annotations

import json
import logging
import os
import platform
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from backend.db import DATA_DIR, AppSetting, SessionLocal
from backend.sessions import current_user

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/settings/browser", tags=["settings"], dependencies=[Depends(current_user)])

ENV = "PLAYWRIGHT_BROWSERS_PATH"
SETTING = "browser"  # app_settings: {"needed": bool, "declined": bool}
SIZE_MB = 180  # about what Playwright downloads for Chromium and its headless shell
INSTALL_TIMEOUT_S = 30 * 60
NEEDED = ("This careers page shows its jobs only with JavaScript, which needs JobHuntPA's browser component. "
          "Download it in Settings > Browser for JavaScript pages, then test the page again.")
FAILED = ("The browser couldn't be downloaded. Check the internet connection and the free space in the data folder, "
          "then try again.")
FAILED_LINUX = (" On Linux it also needs a few system libraries; if it downloads but doesn't start, install them with "
                "your package manager (the README's Troubleshooting section lists the command).")
UNSUPPORTED = "Playwright has no browser for this system ({system})."
UNSUPPORTED_USING = " JobHuntPA uses the Chromium installed on this computer instead ({path}); nothing more is needed."
UNSUPPORTED_INSTALL = (" Install your system's Chromium or Chrome and JobHuntPA uses that (Debian, Ubuntu and Raspberry Pi OS: "
                       "sudo apt install chromium), then reload this page.")
# Chromium or Chrome as the system installs it: names on the PATH (Linux, and the Windows/macOS launchers when they are
# on it), then the usual places.
SYSTEM_NAMES = ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable", "chrome")
SYSTEM_PLACES = {
    "Darwin": ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
               "/Applications/Chromium.app/Contents/MacOS/Chromium"),
    "Windows": tuple(str(Path(base) / sub) for base, sub in (
        (os.environ.get("PROGRAMFILES", r"C:\Program Files"), r"Google\Chrome\Application\chrome.exe"),
        (os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"), r"Google\Chrome\Application\chrome.exe"),
        (os.environ.get("LOCALAPPDATA", ""), r"Google\Chrome\Application\chrome.exe"),
        (os.environ.get("LOCALAPPDATA", ""), r"Chromium\Application\chrome.exe"))),
}

_lock = threading.Lock()
_installing = False
_error: Optional[str] = None


def folder() -> Path:
    """Where the app downloads the browser: ``browsers`` in the data folder."""
    return DATA_DIR / "browsers"


def _playwright_cache() -> Path:
    """Playwright's own folder, where an older setup downloaded Chromium."""
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "ms-playwright"
    if platform.system() == "Darwin":
        return Path.home() / "Library" / "Caches" / "ms-playwright"
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "ms-playwright"


def _has_chromium(place: Path) -> bool:
    try:
        return any(entry.is_dir() and entry.name.startswith("chromium") for entry in place.iterdir())
    except OSError:
        return False


def downloaded() -> bool:
    """Whether Playwright's own Chromium is there: in the data folder, in Playwright's own folder, or where
    PLAYWRIGHT_BROWSERS_PATH points ("0" means inside the Playwright package, which is taken on trust)."""
    chosen = os.environ.get(ENV, "").strip()
    if chosen == "0":
        return True
    if chosen:
        return _has_chromium(Path(chosen))
    return _has_chromium(folder()) or _has_chromium(_playwright_cache())


def system_browser() -> Optional[str]:
    """A Chromium or Chrome the computer has (its path), else None."""
    for name in SYSTEM_NAMES:
        found = shutil.which(name)
        if found:
            return found
    for place in SYSTEM_PLACES.get(platform.system(), ()):
        if place and Path(place).is_file():
            return place
    return None


def available() -> bool:
    """Whether a browser can be launched: Playwright's own Chromium, else one installed on the computer."""
    return downloaded() or system_browser() is not None


def launch_options() -> dict:
    """What ``chromium.launch`` needs besides ``headless``: nothing for Playwright's own Chromium, the executable
    for the computer's own."""
    if downloaded():
        return {}
    path = system_browser()
    return {"executable_path": path} if path else {}


def use_data_folder() -> None:
    """At start-up: a browser downloaded into the data folder is the one Playwright uses (unless whoever runs the app
    set PLAYWRIGHT_BROWSERS_PATH themselves)."""
    if not os.environ.get(ENV) and _has_chromium(folder()):
        os.environ[ENV] = str(folder())


def _read() -> dict:
    db = SessionLocal()
    try:
        row = db.get(AppSetting, SETTING)
        value = json.loads(row.value_json) if row else {}
        return value if isinstance(value, dict) else {}
    finally:
        db.close()


def _write(**changes) -> None:
    db = SessionLocal()
    try:
        row = db.get(AppSetting, SETTING)
        value = json.loads(row.value_json) if row else {}
        value = {**(value if isinstance(value, dict) else {}), **changes}
        db.merge(AppSetting(key=SETTING, value_json=json.dumps(value)))
        db.commit()
    finally:
        db.close()


def mark_needed() -> None:
    """A careers page needed the browser and it wasn't there: the dashboard offers the download (once declined, it
    doesn't offer it again by itself)."""
    try:
        if not _read().get("needed"):
            _write(needed=True)
    except Exception as exc:  # noqa: BLE001  (a search goes on whatever happens here)
        log.warning("Could not record that the browser is needed: %s", exc.__class__.__name__)


def state() -> dict:
    saved = _read()
    own, system = downloaded(), system_browser()
    ready = own or system is not None
    return {"available": ready, "installing": _installing, "error": _error, "needed": bool(saved.get("needed")) and not ready,
            "declined": bool(saved.get("declined")), "folder": str(folder()), "size_mb": SIZE_MB,
            "downloaded": own, "system": system, "using": "downloaded" if own else "system" if system else None}


def _unsupported(output: bytes) -> Optional[str]:
    """The system Playwright says it has no browser for ("debian11-arm64"), from the install command's output."""
    for line in output.decode("utf-8", "replace").splitlines():
        if "does not support" in line:
            return line.strip().rsplit(" on ", 1)[-1].rstrip(".") or "this system"
    return None


def _download() -> None:
    global _installing, _error
    target = folder()
    try:
        target.mkdir(parents=True, exist_ok=True)
        env = {**os.environ, ENV: str(target)}
        done = subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], env=env,
                              stdin=subprocess.DEVNULL, capture_output=True, timeout=INSTALL_TIMEOUT_S)
        ok = done.returncode == 0 and _has_chromium(target)
        unsupported = None if ok else _unsupported((done.stderr or b"") + (done.stdout or b""))
        if unsupported:
            log.warning("Downloading the browser failed: Playwright has no Chromium for %s", unsupported)
        elif not ok:
            log.warning("Downloading the browser failed (exit status %s)", done.returncode)
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("Downloading the browser failed: %s", exc.__class__.__name__)
        ok, unsupported = False, None
    with _lock:
        if ok:
            os.environ[ENV] = str(target)
            _error = None
        elif unsupported:
            system = system_browser()
            _error = UNSUPPORTED.format(system=unsupported) + (UNSUPPORTED_USING.format(path=system) if system
                                                               else UNSUPPORTED_INSTALL)
        else:
            _error = FAILED + (FAILED_LINUX if platform.system() == "Linux" else "")
        _installing = False
    if ok:
        log.info("The browser for JavaScript-only careers pages is downloaded")
        try:
            _write(needed=False, declined=False)
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not record the browser download: %s", exc.__class__.__name__)


def start_download() -> bool:
    """Starts the download in the background; False when one is already running."""
    global _installing, _error
    with _lock:
        if _installing:
            return False
        _installing, _error = True, None
    threading.Thread(target=_download, name="browser-download", daemon=True).start()
    return True


@router.get("")
def get_browser():
    return state()


@router.post("/install")
def install_browser():
    if available():
        return state()
    start_download()
    return JSONResponse(state(), status_code=202)


@router.post("/decline")
def decline_browser():
    _write(declined=True)
    return state()

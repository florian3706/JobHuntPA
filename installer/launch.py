#!/usr/bin/env python3
"""Start JobHuntPA and open it in the browser.

Run with the environment's Python (start.sh, "Start JobHuntPA.command",
JobHuntPA.exe, the AppImage). If the app is already running it just opens the
browser. Stop it with Ctrl+C or by closing the window.

Until the first account exists, the app's first run asks where the data lives
(only while no data folder is chosen: backend/paths.py) and then for the
account. This makes the one-time setup token, hands it to the app
(JOBHUNT_FIRST_RUN_TOKEN) and opens (or, with --no-browser, prints) the
first-run page with the token in the address's #fragment, so it never reaches
a server log. When the folder step is done the app exits with status 3 and is
started again here, now on the chosen folder; the page carries on by itself.

Options: --port N (default 8000; the next free port is used if taken),
         --no-browser
"""
from __future__ import annotations

import argparse
import functools
import json
import os
import re
import secrets
import signal
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from backend import paths  # noqa: E402  (standard library only: where the data folder is)

print = functools.partial(print, flush=True)  # show messages immediately in the console window
HOST = "127.0.0.1"
RESTART_CODE = 3  # the app chose its data folder and asks to be started again (backend/data_folder.py)
TOKEN_ENV = "JOBHUNT_FIRST_RUN_TOKEN"
TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{32,100}")


def data_folder() -> Optional[Path]:
    """The data folder, or None while the first run hasn't chosen one (backend/paths.py)."""
    return paths.locate().folder


def run_file() -> Optional[Path]:
    """running.json in the data folder: lets the uninstaller and the reset tool find a running app."""
    folder = data_folder()
    return folder / "running.json" if folder else None


def first_run_file() -> Optional[Path]:
    """Where the app writes the first-run token: beside the database (backend/users.py first_run_path), which is in
    the data folder unless JOBHUNT_DB_PATH moves it. None while no data folder is chosen (nothing is written then)."""
    db = os.environ.get("JOBHUNT_DB_PATH", "").strip()
    if not db:
        folder = data_folder()
        return folder / "first-run.txt" if folder else None
    path = Path(db).expanduser()
    return (path if path.is_absolute() else ROOT / path).parent / "first-run.txt"


def setup_required(base: str) -> bool:
    try:
        with urllib.request.urlopen(f"{base}/api/auth/state", timeout=5) as resp:
            return bool(json.loads(resp.read()).get("setup_required"))
    except (OSError, ValueError, AttributeError):
        return False


def first_run_url(base: str, token: Optional[str] = None) -> Optional[str]:
    """The first-run page while no account exists, else None: with the token in first-run.txt, or ``token`` (the one
    this launcher handed to the app it started) while the app says it isn't set up yet."""
    found = None
    path = first_run_file()
    if path is not None:
        try:
            found = path.read_text(encoding="ascii").strip()
        except (OSError, UnicodeError):
            found = None
    if not (found and TOKEN_RE.fullmatch(found)):
        found = token if token and TOKEN_RE.fullmatch(token) and setup_required(base) else None
    return f"{base}/first-run.html#token={found}" if found else None


def open_app(url: str, no_browser: bool, token: Optional[str] = None) -> None:
    """Open JobHuntPA, or its first-run page when no account exists yet."""
    setup = first_run_url(url, token)
    if setup:
        print("First run: choose where your data lives and create your account at" if data_folder() is None
              else "First run: create your account at")
        print(f"  {setup}")
    if not no_browser:
        webbrowser.open(setup or url)


def healthy(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://{HOST}:{port}/api/health", timeout=2) as resp:
            return resp.status == 200 and b'"ok"' in resp.read()
    except OSError:
        return False


def port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex((HOST, port)) != 0


def start_server(port: int, env: dict) -> subprocess.Popen:
    # Proxy headers only from this computer (tailscale serve and other local proxies): the client address for rate
    # limits and the https scheme for the session cookie. The app's own log filter drops query strings.
    return subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "backend.app:app", "--host", HOST, "--port", str(port),
         "--proxy-headers", "--forwarded-allow-ips", "127.0.0.1"],
        cwd=ROOT, env=env,
    )


def wait_until_up(server: subprocess.Popen, port: int) -> Optional[int]:
    """None once the app answers, else the exit status to stop with."""
    for _ in range(120):
        if server.poll() is not None:
            print("JobHuntPA stopped while starting; see the messages above.")
            return server.returncode or 1
        if healthy(port):
            return None
        time.sleep(0.5)
    print("JobHuntPA did not start within 60 seconds.")
    server.terminate()
    return 1


def write_run_file(port: int, server: subprocess.Popen) -> Optional[Path]:
    path = run_file()
    if path is None:
        return None
    try:
        path.write_text(json.dumps({"port": port, "pid": server.pid, "launcher_pid": os.getpid()}))
    except OSError:
        return None
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Start JobHuntPA.")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    if healthy(args.port):
        url = f"http://{HOST}:{args.port}"
        print(f"JobHuntPA is already running at {url}")
        open_app(url, args.no_browser)
        return 0

    port = next((p for p in range(args.port, args.port + 20) if port_free(p)), None)
    if port is None:
        print(f"No free port between {args.port} and {args.port + 19}.")
        return 1
    url = f"http://{HOST}:{port}"

    def stop_on_signal(signum, frame):  # window closed (SIGHUP) or asked to stop (SIGTERM)
        raise KeyboardInterrupt

    for name in ("SIGTERM", "SIGHUP", "SIGBREAK"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), stop_on_signal)
    # The first-run token, made here so the page can be opened with it before anything is written (it is only used
    # while no account exists; backend/users.py). Process environment only, never .env.
    token = os.environ.get(TOKEN_ENV, "").strip()
    if not TOKEN_RE.fullmatch(token):
        token = secrets.token_urlsafe(32)
    env = {**os.environ, TOKEN_ENV: token}
    server = None
    written: Optional[Path] = None
    opened = False
    try:
        while True:
            server = start_server(port, env)
            failed = wait_until_up(server, port)
            if failed is not None:
                return failed
            written = write_run_file(port, server)
            if not opened:
                print("\n" + "=" * 60)
                print(f"  JobHuntPA is running at {url}")
                print("  Keep this window open while you use it.")
                print("  To stop: press Ctrl+C or close this window.")
                print("=" * 60 + "\n")
                open_app(url, args.no_browser, token)
                opened = True
            code = server.wait()
            if written:
                written.unlink(missing_ok=True)
                written = None
            if code != RESTART_CODE:
                return code
            print("The data folder is chosen. Starting JobHuntPA again on it...")
            for _ in range(40):  # the old process's port is released as it exits
                if port_free(port):
                    break
                time.sleep(0.25)
    except KeyboardInterrupt:
        print("\nStopping JobHuntPA...")
        if server is not None:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
        return 0
    finally:
        if written:
            written.unlink(missing_ok=True)


if __name__ == "__main__":
    sys.exit(main())

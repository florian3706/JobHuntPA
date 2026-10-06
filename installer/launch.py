#!/usr/bin/env python3
"""Start JobHuntPA and open it in the browser.

Run with the environment's Python (start.sh, "Start JobHuntPA.command",
JobHuntPA.exe). If the app is already running it just opens the browser.
Stop it with Ctrl+C or by closing the window.

Until the first account exists, the app writes a one-time setup token to
data/first-run.txt; this opens (or, with --no-browser, prints) the page that
creates the admin account, with the token in the address's #fragment so it
never reaches a server log.

Options: --port N (default 8000; the next free port is used if taken),
         --no-browser
"""
from __future__ import annotations

import argparse
import functools
import json
import os
import re
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
print = functools.partial(print, flush=True)  # show messages immediately in the console window
HOST = "127.0.0.1"
RUN_FILE = ROOT / "data" / "running.json"  # lets the uninstaller find a running app


def first_run_file() -> Path:
    """Where the app writes the first-run token: beside the database (backend/users.py first_run_path)."""
    db = os.environ.get("JOBHUNT_DB_PATH", "").strip()
    if not db:
        return ROOT / "data" / "first-run.txt"
    path = Path(db).expanduser()
    return (path if path.is_absolute() else ROOT / path).parent / "first-run.txt"


def first_run_url(base: str) -> Optional[str]:
    """The page that creates the first account, while no account exists (the token file is there), else None."""
    try:
        token = first_run_file().read_text(encoding="ascii").strip()
    except (OSError, UnicodeError):
        return None
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,100}", token):
        return None
    return f"{base}/first-run.html#token={token}"


def open_app(url: str, no_browser: bool) -> None:
    """Open JobHuntPA, or its first-run page when no account exists yet."""
    setup = first_run_url(url)
    if setup:
        print("First run: create your account (you will be its admin) at")
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
    # Proxy headers only from this computer (tailscale serve and other local proxies): the client address for rate
    # limits and the https scheme for the session cookie. The app's own log filter drops query strings.
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "backend.app:app", "--host", HOST, "--port", str(port),
         "--proxy-headers", "--forwarded-allow-ips", "127.0.0.1"],
        cwd=ROOT,
    )
    try:
        for _ in range(120):
            if server.poll() is not None:
                print("JobHuntPA stopped while starting; see the messages above.")
                return server.returncode or 1
            if healthy(port):
                break
            time.sleep(0.5)
        else:
            print("JobHuntPA did not start within 60 seconds.")
            server.terminate()
            return 1
        try:
            RUN_FILE.write_text(json.dumps({"port": port, "pid": server.pid, "launcher_pid": os.getpid()}))
        except OSError:
            pass
        print("\n" + "=" * 60)
        print(f"  JobHuntPA is running at {url}")
        print("  Keep this window open while you use it.")
        print("  To stop: press Ctrl+C or close this window.")
        print("=" * 60 + "\n")
        open_app(url, args.no_browser)
        return server.wait()
    except KeyboardInterrupt:
        print("\nStopping JobHuntPA...")
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
        return 0
    finally:
        RUN_FILE.unlink(missing_ok=True)


if __name__ == "__main__":
    sys.exit(main())

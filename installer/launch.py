#!/usr/bin/env python3
"""Start JobHuntPA and open it in the browser.

Run with the environment's Python (start.sh, "Start JobHuntPA.command",
JobHuntPA.exe). If the app is already running it just opens the browser.
Stop it with Ctrl+C or by closing the window.

Options: --port N (default 8000; the next free port is used if taken),
         --no-browser
"""
from __future__ import annotations

import argparse
import functools
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
print = functools.partial(print, flush=True)  # show messages immediately in the console window
HOST = "127.0.0.1"


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
        if not args.no_browser:
            webbrowser.open(url)
        return 0

    port = next((p for p in range(args.port, args.port + 20) if port_free(p)), None)
    if port is None:
        print(f"No free port between {args.port} and {args.port + 19}.")
        return 1
    url = f"http://{HOST}:{port}"
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "backend.app:app", "--host", HOST, "--port", str(port)],
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
        print("\n" + "=" * 60)
        print(f"  JobHuntPA is running at {url}")
        print("  Keep this window open while you use it.")
        print("  To stop: press Ctrl+C or close this window.")
        print("=" * 60 + "\n")
        if not args.no_browser:
            webbrowser.open(url)
        return server.wait()
    except KeyboardInterrupt:
        print("\nStopping JobHuntPA...")
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
        return 0


if __name__ == "__main__":
    sys.exit(main())

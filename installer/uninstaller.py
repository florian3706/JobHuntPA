#!/usr/bin/env python3
"""JobHuntPA uninstaller (Linux, macOS and Windows).

Run by uninstall.sh / "Uninstall JobHuntPA.command" (Linux/macOS) and
compiled into JobHuntPA-Uninstall.exe (Windows). Standard library only.

Steps:
  1. make sure JobHuntPA isn't running
  2. optionally save a backup zip of your data and settings (data/ + .env)
  3. remove shortcuts (Linux menu entry; Windows Desktop/Start menu and
     the "Apps & features" entry)
  4. optionally remove the Chromium browser setup downloaded (shared by
     any other app that uses Playwright, so kept by default)
  5. delete the app folder

Python itself is left installed (other programs may use it).

Options: --yes (no questions: backup yes, browser kept), --no-backup,
         --remove-browser, --folder PATH
"""
from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from datetime import datetime
from pathlib import Path

IS_WINDOWS = os.name == "nt"
APP_NAME = "JobHuntPA"
REG_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\JobHuntPA"


def say(msg: str = "") -> None:
    print(msg, flush=True)


def ask_yes(prompt: str, default: bool, assume: bool) -> bool:
    if assume:
        return default
    hint = "[Y/n]" if default else "[y/N]"
    try:
        answer = input(f"{prompt} {hint} ").strip().lower()
    except EOFError:
        return default
    return default if not answer else answer.startswith("y")


def is_app_folder(folder: Path) -> bool:
    return (folder / "installer" / "launch.py").exists() and (folder / "backend").is_dir()


def find_app_folder(explicit: str | None) -> Path | None:
    if explicit:
        return Path(explicit).resolve()
    here = Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve().parent
    candidates = [here, here.parent]
    if IS_WINDOWS:
        candidates.append(Path(os.environ.get("LOCALAPPDATA", "")) / APP_NAME)
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_KEY) as key:
                candidates.append(Path(winreg.QueryValueEx(key, "InstallLocation")[0]))
        except OSError:
            pass
    return next((c for c in candidates if is_app_folder(c)), None)


def running_ports(folder: Path) -> list[int]:
    """Ports where JobHuntPA answers: the one launch.py recorded, plus the default range."""
    candidates = list(range(8000, 8020))
    try:
        import json

        recorded = json.loads((folder / "data" / "running.json").read_text()).get("port")
        if isinstance(recorded, int):
            candidates.insert(0, recorded)
    except (OSError, ValueError, AttributeError):
        pass
    ports = []
    for port in dict.fromkeys(candidates):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=0.5) as resp:
                if resp.status == 200 and b'"ok"' in resp.read():
                    ports.append(port)
        except OSError:
            continue
    return ports


def backup(folder: Path) -> Path | None:
    items = [p for p in (folder / "data", folder / ".env") if p.exists()]
    if not items:
        say("  Nothing to back up.")
        return None
    dest_dir = Path.home() / "Documents"
    if not dest_dir.is_dir():
        dest_dir = Path.home()
    dest = dest_dir / f"{APP_NAME}-backup-{datetime.now():%Y%m%d-%H%M%S}.zip"
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
        for item in items:
            paths = [item] if item.is_file() else [p for p in item.rglob("*") if p.is_file()]
            for p in paths:
                if p.name.endswith(("-wal", "-shm")) and p.stat().st_size == 0:
                    continue
                zf.write(p, p.relative_to(folder))
    say(f"  Backup saved: {dest}")
    say("  (It contains your API key in .env; keep it private. To restore, unzip it into a fresh install.)")
    return dest


def remove_shortcuts(folder: Path) -> None:
    system = platform.system()
    if system == "Linux":
        entry = Path.home() / ".local/share/applications/jobhuntpa.desktop"
        if entry.exists() and str(folder) in entry.read_text(encoding="utf-8", errors="replace"):
            entry.unlink()
            say("  Removed the applications-menu entry.")
    elif IS_WINDOWS:
        desktop = Path(os.environ.get("USERPROFILE", Path.home())) / "Desktop"
        programs = Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
        for link in (desktop / f"{APP_NAME}.lnk", programs / f"{APP_NAME}.lnk", programs / f"Uninstall {APP_NAME}.lnk"):
            if link.exists():
                link.unlink()
        try:
            import winreg

            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, REG_KEY)
        except OSError:
            pass
        say("  Removed Desktop and Start-menu shortcuts and the Apps & features entry.")


def playwright_cache() -> Path:
    if IS_WINDOWS:
        return Path(os.environ.get("LOCALAPPDATA", "")) / "ms-playwright"
    if platform.system() == "Darwin":
        return Path.home() / "Library" / "Caches" / "ms-playwright"
    return Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "ms-playwright"


def delete_folder(folder: Path) -> None:
    if IS_WINDOWS:
        # Files of a running program can't be deleted on Windows (this .exe
        # may live in the folder): delete after this window closes.
        script = Path(tempfile.gettempdir()) / "jobhuntpa-remove.cmd"
        script.write_text(
            "@echo off\r\nping 127.0.0.1 -n 4 > nul\r\n"
            f'rmdir /s /q "{folder}"\r\n'
            'del "%~f0"\r\n', encoding="utf-8")
        subprocess.Popen(["cmd", "/c", str(script)], creationflags=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS,
                         close_fds=True)
        say(f"  {folder} will be deleted a few seconds after this window closes.")
    else:
        shutil.rmtree(folder)
        say(f"  Deleted {folder}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Remove JobHuntPA from this computer.")
    parser.add_argument("--yes", action="store_true", help="don't ask; back up data, keep the shared browser")
    parser.add_argument("--no-backup", action="store_true")
    parser.add_argument("--remove-browser", action="store_true")
    parser.add_argument("--folder")
    args = parser.parse_args()
    assume = args.yes or not sys.stdin.isatty()

    folder = find_app_folder(args.folder)
    if folder is None or not is_app_folder(folder):
        say("Couldn't find a JobHuntPA installation" + (f" in {args.folder}" if args.folder else "") + ".")
        return 1
    say(f"This removes JobHuntPA from {folder}")
    say("including your jobs, documents, workspaces, settings and API key.")
    if not ask_yes("Continue?", default=True, assume=assume):
        say("Nothing was changed.")
        return 0

    ports = running_ports(folder)
    while ports:
        say(f"\nJobHuntPA is still running (port {', '.join(map(str, ports))}). "
            "Close its window (or press Ctrl+C in it) first.")
        if assume:
            return 1
        input("Press Enter when it's closed...")
        ports = running_ports(folder)

    say("\n[1/4] Backup")
    if args.no_backup or not ask_yes("Save a backup of your jobs, documents and settings first?", True, assume):
        say("  Skipped.")
    else:
        backup(folder)

    say("\n[2/4] Shortcuts")
    remove_shortcuts(folder)

    say("\n[3/4] Browser component")
    cache = playwright_cache()
    if cache.exists() and (args.remove_browser or ask_yes(
            f"Also remove the Chromium browser setup downloaded ({cache})? Other apps using Playwright share it.",
            False, assume)):
        shutil.rmtree(cache, ignore_errors=True)
        say("  Removed.")
    else:
        say("  Kept.")

    say("\n[4/4] App folder")
    delete_folder(folder)
    say("\nJobHuntPA has been uninstalled. Python was left installed.")
    return 0


if __name__ == "__main__":
    code = main()
    if IS_WINDOWS and getattr(sys, "frozen", False) and sys.stdin and sys.stdin.isatty():
        input("\nPress Enter to close this window.")
    sys.exit(code)

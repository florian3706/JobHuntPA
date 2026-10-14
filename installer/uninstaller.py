#!/usr/bin/env python3
"""JobHuntPA uninstaller (Linux, macOS and Windows).

Run by uninstall.sh / "Uninstall JobHuntPA.command" (Linux/macOS), compiled
into JobHuntPA-Uninstall.exe (Windows), and run by the AppImage with
--uninstall. Standard library only.

Steps:
  1. make sure JobHuntPA isn't running
  2. optionally save a backup zip of your data and settings: the data folder,
     wherever it is (the pointer the first run wrote, JOBHUNT_DATA_DIR, the
     service's, or data/ in the app folder), and an older .env next to the app
  3. remove shortcuts (Linux menu entry; Windows Desktop/Start menu, with
     the Uninstall and Reset entries, and the "Apps & features" entry) and the
     Linux service that service.sh installs
  4. optionally remove the Chromium browser an older setup downloaded (shared
     by any other app that uses Playwright, so kept by default); the one the
     app downloads lives in the data folder and goes with it
  5. delete the data folder (when it is outside the app folder), the pointer,
     and the app folder (the AppImage file, for the AppImage)

Python itself is left installed (other programs may use it).

Options: --yes (no questions: backup yes, browser kept), --no-backup,
         --remove-browser, --folder PATH
"""
from __future__ import annotations

import argparse
import importlib.util
import json
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
SERVICE = "jobhuntpa.service"  # service.sh's systemd user service


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


def load_paths(folder: Path):
    """The app's backend/paths.py (where its data folder is), loaded by file path; None for an app folder from before
    it existed (its data is then in data/)."""
    name = "_jobhunt_paths"
    try:
        spec = importlib.util.spec_from_file_location(name, folder / "backend" / "paths.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        try:
            spec.loader.exec_module(module)
        finally:
            sys.modules.pop(name, None)
        return module
    except Exception:  # noqa: BLE001
        return None


def service_unit() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "systemd" / "user" / SERVICE


def service_ident(folder: Path) -> str:
    """What service.sh writes into the unit to say which install it runs: the AppImage file, or the app folder."""
    return os.environ.get("APPIMAGE") or str(folder)


def service_data_folder(folder: Path) -> Path | None:
    """The data folder service.sh gave the service of this app folder (its JOBHUNT_DATA_DIR line), if there is one."""
    try:
        text = service_unit().read_text(encoding="utf-8")
    except OSError:
        return None
    if service_ident(folder) not in text:
        return None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("Environment=") and "JOBHUNT_DATA_DIR=" in line:
            value = line.split("JOBHUNT_DATA_DIR=", 1)[1].strip().strip('"').replace("%%", "%")
            return Path(value) if value and Path(value).is_absolute() else None
    return None


def data_folders(folder: Path) -> list[Path]:
    """Every data folder of this install: the one the app would use now (JOBHUNT_DATA_DIR, the pointer or data/ next
    to the app), and the service's."""
    found: list[Path] = []
    paths = load_paths(folder)
    if paths is not None:
        place = paths.locate(folder)
        if place.folder is not None:
            found.append(place.folder)
    elif (folder / "data").is_dir():
        found.append(folder / "data")
    service = service_data_folder(folder)
    if service is not None:
        found.append(service)
    unique: list[Path] = []
    for path in found:
        if path.resolve() not in [p.resolve() for p in unique] and path.is_dir():
            unique.append(path)
    return unique


def running_ports(folder: Path) -> list[int]:
    """Ports where JobHuntPA answers: the one launch.py recorded, plus the default range."""
    candidates = list(range(8000, 8020))
    for data in [folder / "data", *data_folders(folder)]:
        try:
            recorded = json.loads((data / "running.json").read_text()).get("port")
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


def backup(folder: Path, datas: list[Path] | None = None) -> Path | None:
    """A zip of the data folder(s) (as data/, data-2/ ...) and an older .env next to the app (as .env)."""
    datas = data_folders(folder) if datas is None else datas
    items = [(d, "data" if i == 0 else f"data-{i + 1}") for i, d in enumerate(datas)]
    if (folder / ".env").is_file():
        items.append((folder / ".env", ".env"))
    if not items:
        say("  Nothing to back up.")
        return None
    dest_dir = Path.home() / "Documents"
    if not dest_dir.is_dir():
        dest_dir = Path.home()
    dest = dest_dir / f"{APP_NAME}-backup-{datetime.now():%Y%m%d-%H%M%S}.zip"
    # Owner-only from the start (0600): it holds the server key file and the API keys. (On Windows the file
    # gets the folder's permissions, which are the user's own in Documents.)
    fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    with os.fdopen(fd, "wb") as raw, zipfile.ZipFile(raw, "w", zipfile.ZIP_DEFLATED) as zf:
        for item, name in items:
            if item.is_file():
                zf.write(item, name)
                continue
            for p in item.rglob("*"):
                rel = p.relative_to(item)
                if not p.is_file() or rel.parts[0] == "browsers":  # (a download, not data)
                    continue
                if p.name.endswith(("-wal", "-shm")) and p.stat().st_size == 0:
                    continue
                if p.name in ("first-run.txt", "running.json") or p.name.endswith(".lock"):  # only meaningful while this copy runs
                    continue
                zf.write(p, Path(name) / rel)
    if not IS_WINDOWS:
        os.chmod(dest, 0o600)
    say(f"  Backup saved: {dest}")
    say("  It contains your encryption key file and API keys; keep it private. To restore, unzip its data folder")
    say("  and choose it at a new install's first run: the database and server-keys.json must stay together,")
    say("  and you still need your password.")
    return dest


def remove_service(folder: Path) -> None:
    """service.sh's systemd user service, when it runs this app folder: stopped, turned off and removed."""
    unit = service_unit()
    try:
        text = unit.read_text(encoding="utf-8")
    except OSError:
        return
    if service_ident(folder) not in text:
        return
    def systemctl(*args: str) -> None:
        try:
            subprocess.run(["systemctl", "--user", *args], check=False, capture_output=True, timeout=60)
        except (OSError, subprocess.SubprocessError):
            pass

    systemctl("disable", "--now", SERVICE)
    unit.unlink(missing_ok=True)
    systemctl("daemon-reload")
    say("  Removed the service that started JobHuntPA at boot.")


def safe_to_delete(data: Path, app: Path) -> bool:
    """Only a folder that holds JobHuntPA data, and never a home folder, a top-level folder, or one that holds the app
    folder or the home folder."""
    try:
        data, app, home = data.resolve(), app.resolve(), Path.home().resolve()
    except OSError:
        return False
    if data in (home, Path(data.anchor)) or data in home.parents or data in app.parents or data == app:
        return False
    if len(data.parts) < 3:
        return False
    return (data / "jobhunt.db").is_file() or (data / "server-keys.json").is_file()


def remove_data_folders(folder: Path, datas: list[Path]) -> None:
    """The data folders outside the app folder (the one inside goes with it), then the pointer."""
    for data in datas:
        try:
            inside = data.resolve().is_relative_to(folder.resolve())
        except OSError:
            inside = False
        if inside:
            continue
        if not safe_to_delete(data, folder):
            say(f"  Left {data} in place: it doesn't look like a JobHuntPA data folder only. Delete it yourself if "
                "it is.")
            continue
        shutil.rmtree(data, ignore_errors=True)
        say(f"  Deleted the data folder {data}")
    paths = load_paths(folder)
    if paths is not None and paths.remove_pointer():
        say("  Removed the note of where the data folder was.")


def remove_shortcuts(folder: Path) -> None:
    system = platform.system()
    if system == "Linux":
        remove_service(folder)
        entry = Path.home() / ".local/share/applications/jobhuntpa.desktop"
        if entry.exists() and str(folder) in entry.read_text(encoding="utf-8", errors="replace"):
            entry.unlink()
            say("  Removed the applications-menu entry.")
    elif IS_WINDOWS:
        desktop = Path(os.environ.get("USERPROFILE", Path.home())) / "Desktop"
        programs = Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
        for link in (desktop / f"{APP_NAME}.lnk", programs / f"{APP_NAME}.lnk", programs / f"Uninstall {APP_NAME}.lnk",
                     programs / f"Reset {APP_NAME}.lnk"):
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
    appimage = os.environ.get("APPIMAGE", "")
    datas = data_folders(folder)
    say(f"This removes JobHuntPA from {appimage or folder}")
    for data in datas:
        say(f"and its data folder {data},")
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
        backup(folder, datas)

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

    say("\n[4/4] Data folder and app")
    remove_data_folders(folder, datas)
    if appimage:  # the app folder is the AppImage's own, read-only and gone when it stops: the file is what to delete
        try:
            Path(appimage).unlink()
            say(f"  Deleted {appimage}")
        except OSError:
            say(f"  Delete {appimage} yourself to finish.")
    else:
        delete_folder(folder)
    say("\nJobHuntPA has been uninstalled. Python was left installed.")
    return 0


if __name__ == "__main__":
    code = main()
    if IS_WINDOWS and getattr(sys, "frozen", False) and sys.stdin and sys.stdin.isatty():
        input("\nPress Enter to close this window.")
    sys.exit(code)

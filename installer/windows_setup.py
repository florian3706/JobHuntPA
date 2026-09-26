"""JobHuntPA-Setup.exe: install or update JobHuntPA on Windows.

Built with PyInstaller by .github/workflows/release.yml; the matching
JobHuntPA.exe is bundled inside. Steps:
  1. choose a folder (default %LOCALAPPDATA%\\JobHuntPA)
  2. download this version of the app from GitHub and unpack it there,
     keeping an existing .env and data folder
  3. find Python 3.10+ or install Python 3.12 for the current user
  4. run the shared setup (installer/installer.py) with that Python
  5. put JobHuntPA.exe in the folder and create Desktop + Start-menu shortcuts
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

REPO = "florian3706/JobHuntPA"
PYTHON_URL = "https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe"
KEEP = {".env", "data", ".venv"}  # never overwritten on update


def bundled(name: str) -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent)) / name


def version() -> str:
    try:
        return bundled("VERSION").read_text().strip()
    except OSError:
        return "dev"


def pause_and_exit(code: int) -> None:
    input("\nPress Enter to close this window.")
    sys.exit(code)


def download(url: str, dest: Path) -> None:
    print(f"  Downloading {url}")
    with urllib.request.urlopen(url, timeout=120) as resp, open(dest, "wb") as out:
        shutil.copyfileobj(resp, out)


def install_app(target: Path, ver: str) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / "app.zip"
        download(f"https://github.com/{REPO}/archive/refs/tags/v{ver}.zip", archive)
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(tmp)
        src = next(p for p in Path(tmp).iterdir() if p.is_dir())
        target.mkdir(parents=True, exist_ok=True)
        for item in src.iterdir():
            dest = target / item.name
            if item.name in KEEP and dest.exists():
                continue
            if item.is_dir():
                shutil.copytree(item, dest, dirs_exist_ok=True)
            else:
                shutil.copy2(item, dest)


def python_version_ok(cmd: list[str]) -> bool:
    try:
        out = subprocess.run(cmd + ["-c", "import sys; print(sys.version_info >= (3, 10))"],
                             capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return out.returncode == 0 and out.stdout.strip() == "True"


def find_python() -> list[str] | None:
    local = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Python"
    candidates = [["py", "-3.12"], ["py", "-3"]]
    candidates += [[str(p)] for p in sorted(local.glob("Python3*/python.exe"), reverse=True)]
    python_on_path = shutil.which("python")
    if python_on_path and "WindowsApps" not in python_on_path:  # skip the Microsoft Store stub
        candidates.append([python_on_path])
    return next((c for c in candidates if python_version_ok(c)), None)


def install_python() -> list[str]:
    print("  Python 3.10+ not found. Installing Python 3.12 for your user account (no admin rights needed)...")
    with tempfile.TemporaryDirectory() as tmp:
        exe = Path(tmp) / "python-installer.exe"
        download(PYTHON_URL, exe)
        subprocess.run([str(exe), "/quiet", "InstallAllUsers=0", "PrependPath=1", "Include_launcher=1",
                        "Include_test=0", "SimpleInstall=1"], check=True)
    found = find_python()
    if not found:
        raise RuntimeError("Python was installed but can't be found; restart your computer and run setup again.")
    return found


def make_shortcut(link: Path, target: Path, workdir: Path) -> None:
    ps = (f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{link}');"
          f"$s.TargetPath='{target}';$s.WorkingDirectory='{workdir}';$s.Description='JobHuntPA';$s.Save()")
    subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps], check=True)


def main() -> None:
    ver = version()
    print("=" * 60)
    print(f"  JobHuntPA setup (version {ver})")
    print("=" * 60)
    default = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "JobHuntPA"
    answer = input(f"\nInstall folder [{default}]: ").strip().strip('"')
    target = Path(answer) if answer else default
    updating = (target / "backend").exists()

    print(f"\n[1/4] {'Updating' if updating else 'Installing'} JobHuntPA in {target} ...")
    install_app(target, ver)

    print("\n[2/4] Checking for Python ...")
    python = find_python() or install_python()
    print(f"  Using: {' '.join(python)}")

    print("\n[3/4] Setting up (packages, browser, LLM settings) ...")
    subprocess.run(python + [str(target / "installer" / "installer.py")], cwd=target, check=True)

    print("\n[4/4] Creating shortcuts ...")
    launcher = target / "JobHuntPA.exe"
    shutil.copy2(bundled("JobHuntPA.exe"), launcher)
    desktop = Path(os.environ.get("USERPROFILE", Path.home())) / "Desktop"
    programs = Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
    for folder in (desktop, programs):
        if folder.exists():
            make_shortcut(folder / "JobHuntPA.lnk", launcher, target)
    print("  Added JobHuntPA to your Desktop and Start menu.")

    print("\nDone.")
    if input("Start JobHuntPA now? [Y/n] ").strip().lower() in ("", "y", "yes"):
        subprocess.Popen([str(launcher)], cwd=target, creationflags=subprocess.CREATE_NEW_CONSOLE)


if __name__ == "__main__":
    try:
        main()
    except (subprocess.CalledProcessError, RuntimeError, OSError) as exc:
        print(f"\nSetup stopped: {exc}")
        pause_and_exit(1)
    except KeyboardInterrupt:
        pause_and_exit(1)
    pause_and_exit(0)

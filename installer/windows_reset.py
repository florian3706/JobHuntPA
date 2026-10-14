"""JobHuntPA-Reset.exe: start JobHuntPA over on Windows (Start menu > Reset JobHuntPA).

Built with PyInstaller by .github/workflows/release.yml, like JobHuntPA.exe. It looks for the app next to itself, then
in %LOCALAPPDATA%\\JobHuntPA, and runs ``python -m backend.reset`` (backend/reset.py) with the app's own Python
environment: that says what will be lost, asks for the word RESET, refuses while JobHuntPA is running, deletes the
account and its data and keeps the settings. Options are passed on (--confirm RESET).
"""
from __future__ import annotations

import subprocess
import sys

from windows_launch import app_folder  # (PyInstaller bundles it: it sits beside this file)


def main() -> int:
    folder = app_folder()
    python = folder / ".venv" / "Scripts" / "python.exe" if folder else None
    if not python or not python.exists():
        print("JobHuntPA isn't set up yet, so there is nothing to reset.")
        return 1
    try:
        return subprocess.call([str(python), "-m", "backend.reset", *sys.argv[1:]], cwd=folder)
    except KeyboardInterrupt:
        print("\nNothing was changed.")
        return 1


if __name__ == "__main__":
    code = main()
    if getattr(sys, "frozen", False) and sys.stdin and sys.stdin.isatty():
        input("\nPress Enter to close this window.")
    sys.exit(code)

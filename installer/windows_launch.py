"""JobHuntPA.exe: start JobHuntPA on Windows and open it in the browser.

Built with PyInstaller by .github/workflows/release.yml. It looks for the
app next to itself, then in %LOCALAPPDATA%\\JobHuntPA, and runs
installer/launch.py with the app's own Python environment. Closing the
window stops JobHuntPA.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def app_folder() -> Path | None:
    here = Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve().parent
    for folder in (here, here.parent, Path(os.environ.get("LOCALAPPDATA", "")) / "JobHuntPA"):
        if (folder / "installer" / "launch.py").exists():
            return folder
    return None


def main() -> int:
    folder = app_folder()
    python = folder / ".venv" / "Scripts" / "python.exe" if folder else None
    if not python or not python.exists():
        print("JobHuntPA isn't set up yet. Run JobHuntPA-Setup.exe first.")
        input("\nPress Enter to close this window.")
        return 1
    try:
        return subprocess.call([str(python), str(folder / "installer" / "launch.py"), *sys.argv[1:]], cwd=folder)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())

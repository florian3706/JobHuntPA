#!/usr/bin/env python3
"""JobHuntPA one-time setup (Linux, macOS and Windows).

Run by setup.sh (Linux/macOS) and by JobHuntPA-Setup.exe (Windows); safe
to run again (it updates dependencies and keeps your settings and data).

Steps:
  1. create a private Python environment in .venv
  2. install the Python packages from requirements.txt
  3. download the Chromium browser used for JavaScript-only careers pages
  4. ask for the LLM settings and write .env (kept if already filled in)
  5. add a menu shortcut (Linux)
  6. check the app imports

Standard library only: this runs before any dependency is installed.

Options:
  --non-interactive   never prompt (LLM settings from LLM_* environment variables, else left blank)
  --reconfigure       ask for the LLM settings again even if .env has them
  --skip-browser      don't download Chromium
"""
from __future__ import annotations

import argparse
import os
import platform
import subprocess
import sys
from pathlib import Path

MIN_PYTHON = (3, 10)
ROOT = Path(__file__).resolve().parent.parent
IS_WINDOWS = os.name == "nt"

LLM_PRESETS = [
    ("Meta Model API (Muse Spark)", "https://api.meta.ai/v1", "muse-spark-1.3"),
    ("OpenAI", "https://api.openai.com/v1", ""),
    ("Another OpenAI-compatible provider", "", ""),
]


def say(msg: str = "") -> None:
    print(msg, flush=True)


def step(n: int, total: int, msg: str) -> None:
    say(f"\n[{n}/{total}] {msg}")


def fail(msg: str) -> None:
    say(f"\nSetup stopped: {msg}")
    sys.exit(1)


def venv_python(root: Path = ROOT) -> Path:
    return root / ".venv" / ("Scripts/python.exe" if IS_WINDOWS else "bin/python")


def run(cmd: list, **kw) -> None:
    subprocess.run([str(c) for c in cmd], check=True, **kw)


# --------------------------------------------------------------------------
# Steps
# --------------------------------------------------------------------------

def create_venv() -> None:
    py = venv_python()
    if py.exists():
        say("  Existing environment found; reusing it.")
        return
    try:
        run([sys.executable, "-m", "venv", ROOT / ".venv"])
    except subprocess.CalledProcessError:
        hint = ""
        if platform.system() == "Linux":
            ver = f"{sys.version_info.major}.{sys.version_info.minor}"
            hint = (f"\nOn Debian/Ubuntu install the venv module first:  sudo apt install python{ver}-venv"
                    "\nthen run setup again.")
        fail("could not create the Python environment." + hint)


def install_requirements() -> None:
    py = venv_python()
    try:
        run([py, "-m", "pip", "install", "--disable-pip-version-check", "--upgrade", "pip"], cwd=ROOT)
        run([py, "-m", "pip", "install", "--disable-pip-version-check", "-r", ROOT / "requirements.txt"], cwd=ROOT)
    except subprocess.CalledProcessError:
        fail("installing packages failed. Check your internet connection and run setup again.")


def install_browser() -> None:
    try:
        run([venv_python(), "-m", "playwright", "install", "chromium"], cwd=ROOT)
    except subprocess.CalledProcessError:
        say("  Warning: the browser for JavaScript-only careers pages could not be installed.")
        say("  Everything else works; a few company sites may return no jobs.")
        if platform.system() == "Linux":
            say("  To fix it later:  .venv/bin/python -m playwright install --with-deps chromium")


def read_env(path: Path) -> tuple[list[str], dict[str, str]]:
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    values = {}
    for line in lines:
        if "=" in line and not line.lstrip().startswith("#"):
            key, _, val = line.partition("=")
            values[key.strip()] = val.strip()
    return lines, values


def write_env(path: Path, lines: list[str], updates: dict[str, str]) -> None:
    out, done = [], set()
    for line in lines:
        key = line.partition("=")[0].strip() if "=" in line and not line.lstrip().startswith("#") else None
        if key in updates:
            out.append(f"{key}={updates[key]}")
            done.add(key)
        else:
            out.append(line)
    if not lines:
        out = ["# LLM used for fit scoring and company research.",
               "# Works with any OpenAI-compatible API. Re-run setup to change these."]
    for key, val in updates.items():
        if key not in done:
            out.append(f"{key}={val}")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        answer = input(f"  {prompt}{suffix}: ").strip()
    except EOFError:
        answer = ""
    return answer or default


def configure_llm(interactive: bool, reconfigure: bool) -> None:
    env_path = ROOT / ".env"
    lines, values = read_env(env_path)
    required = ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL")
    if all(values.get(k) for k in required) and not reconfigure:
        say("  LLM settings already in .env; keeping them (run setup with --reconfigure to change).")
        return
    if not interactive:
        updates = {k: os.environ.get(k, values.get(k, "")) for k in required}
        write_env(env_path, lines, updates)
        missing = [k for k in required if not updates[k]]
        say("  Wrote .env" + (f"; still to fill in: {', '.join(missing)}" if missing else "."))
        return

    say("  JobHuntPA uses an AI model (LLM) to score jobs and research companies.")
    say("  Which provider do you have an API key for?")
    for i, (name, _, _) in enumerate(LLM_PRESETS, 1):
        say(f"    {i}) {name}")
    say("    s) Skip for now (you can re-run setup later)")
    choice = ask("Choose", "1").lower()
    if choice == "s":
        write_env(env_path, lines, {k: values.get(k, "") for k in required})
        say("  Skipped. Scoring and research stay off until the LLM is configured.")
        return
    try:
        _, base, model = LLM_PRESETS[int(choice) - 1]
    except (ValueError, IndexError):
        _, base, model = LLM_PRESETS[-1]
    base = ask("API address (base URL)", base or values.get("LLM_BASE_URL", ""))
    model = ask("Model name", model or values.get("LLM_MODEL", ""))
    key = ask("API key (paste it; it is stored only in the .env file on this computer)")
    write_env(env_path, lines, {"LLM_API_KEY": key, "LLM_BASE_URL": base.rstrip("/"), "LLM_MODEL": model})
    say("  Saved to .env. Reasoning levels can be set in the app (Search setup > LLM).")


def ensure_dirs() -> None:
    for sub in ("data", "data/uploads", "data/cache"):
        (ROOT / sub).mkdir(parents=True, exist_ok=True)


def create_shortcut() -> None:
    if platform.system() != "Linux":
        return  # Windows: the setup .exe makes shortcuts; macOS: double-click "Start JobHuntPA.command"
    apps = Path.home() / ".local/share/applications"
    try:
        apps.mkdir(parents=True, exist_ok=True)
        (apps / "jobhuntpa.desktop").write_text(
            "[Desktop Entry]\nType=Application\nName=JobHuntPA\nComment=Personal job-hunt dashboard\n"
            f"Exec=\"{ROOT / 'start.sh'}\"\nPath={ROOT}\nTerminal=true\nCategories=Office;\n",
            encoding="utf-8",
        )
        say("  Added JobHuntPA to your applications menu.")
    except OSError as exc:
        say(f"  Could not add a menu shortcut ({exc}); use ./start.sh instead.")


def check_app() -> None:
    try:
        run([venv_python(), "-c", "import backend.app"], cwd=ROOT,
            env={**os.environ, "JOBHUNT_DB_PATH": str(ROOT / "data" / "jobhunt.db")})
    except subprocess.CalledProcessError:
        fail("the app failed to load after installing. Please report this with the output above.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Set up JobHuntPA on this computer.")
    parser.add_argument("--non-interactive", action="store_true")
    parser.add_argument("--reconfigure", action="store_true")
    parser.add_argument("--skip-browser", action="store_true")
    args = parser.parse_args()

    if sys.version_info < MIN_PYTHON:
        fail(f"Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} or newer is needed (this is {platform.python_version()}).")
    interactive = not args.non_interactive and sys.stdin.isatty()
    version = (ROOT / "VERSION").read_text().strip() if (ROOT / "VERSION").exists() else "dev"
    say(f"JobHuntPA setup (version {version}) in {ROOT}")

    total = 6
    step(1, total, "Creating a private Python environment...")
    create_venv()
    step(2, total, "Installing packages (this can take a few minutes)...")
    install_requirements()
    step(3, total, "Downloading the browser used for JavaScript careers pages...")
    if args.skip_browser:
        say("  Skipped.")
    else:
        install_browser()
    step(4, total, "LLM settings")
    ensure_dirs()
    configure_llm(interactive, args.reconfigure)
    step(5, total, "Shortcut")
    create_shortcut()
    step(6, total, "Checking the app...")
    check_app()

    say("\nSetup complete.")
    if IS_WINDOWS:
        say("Start JobHuntPA from the Desktop or Start-menu shortcut.")
    elif platform.system() == "Darwin":
        say('Start JobHuntPA by double-clicking "Start JobHuntPA.command" (or run ./start.sh).')
    else:
        say("Start JobHuntPA from your applications menu, or run ./start.sh in this folder.")


if __name__ == "__main__":
    main()

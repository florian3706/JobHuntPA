#!/usr/bin/env python3
"""JobHuntPA one-time setup (Linux, macOS and Windows).

Run by setup.sh (Linux/macOS) and by JobHuntPA-Setup.exe (Windows); safe
to run again (it updates dependencies and keeps your settings and data).

Steps:
  1. create a private Python environment in .venv
  2. install the Python packages from requirements.txt
  3. download the Chromium browser used for JavaScript-only careers pages
  4. ask for the LLM settings and the optional commute keys, and write .env
     (kept if already filled in)
  5. add a menu shortcut (Linux)
  6. check the app imports

Standard library only: this runs before any dependency is installed.

Options:
  --non-interactive   never prompt (settings from LLM_*, TFNSW_API_KEY and TOMTOM_API_KEY
                      environment variables, else left blank)
  --reconfigure       ask for the LLM settings and commute keys again even if .env has them
  --skip-browser      don't download Chromium
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import platform
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

MIN_PYTHON = (3, 10)
ROOT = Path(__file__).resolve().parent.parent
IS_WINDOWS = os.name == "nt"

# The providers offered in the LLM menu come from backend/llm_providers.py, the one list the app itself uses
# (Settings > LLM). It is loaded by file path so nothing of the app has to be installed yet, and so the
# Windows setup program (a frozen build of this file that runs against the downloaded source) needs no package
# import. If that file can't be read, this short list is used instead.
PRESETS_FILE = ROOT / "backend" / "llm_providers.py"
FALLBACK_PRESETS = [
    {"id": "openai", "label": "OpenAI", "base_url": "https://api.openai.com/v1", "models": ["gpt-6-luna"],
     "key_url": "https://platform.openai.com/api-keys", "key_required": True},
    {"id": "anthropic", "label": "Anthropic (Claude)", "base_url": "https://api.anthropic.com",
     "models": ["claude-opus-5-5"], "key_url": "https://platform.claude.com/settings/keys", "key_required": True},
    {"id": "gemini", "label": "Google Gemini", "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
     "models": ["gemini-3.8-flash"], "key_url": "https://aistudio.google.com/apikey", "key_required": True},
    {"id": "meta", "label": "Meta (Muse Spark)", "base_url": "https://api.meta.ai/v1", "models": ["muse-spark-1.3"],
     "key_url": "https://dev.meta.ai/", "key_required": True},
    {"id": "ollama", "label": "Ollama (local)", "base_url": "http://localhost:11434/v1", "models": [],
     "key_url": "https://ollama.com/download", "key_required": False},
    {"id": "custom", "label": "Custom (OpenAI-compatible)", "base_url": "", "models": [], "key_url": "", "key_required": True},
]
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")


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
        out = ["# LLM used for fit scoring, cover letters, job-title suggestions and company research.",
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


def load_presets() -> list[dict]:
    """The LLM providers for the menu: PRESETS from backend/llm_providers.py, else FALLBACK_PRESETS."""
    name = "_jobhunt_llm_providers"
    try:
        spec = importlib.util.spec_from_file_location(name, PRESETS_FILE)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        try:
            spec.loader.exec_module(module)
        finally:
            sys.modules.pop(name, None)
        presets = [dict(p) for p in module.PRESETS]
        for p in presets:  # the fields used below
            str(p["label"]), str(p["base_url"]), list(p["models"]), str(p["key_url"]), bool(p["key_required"])
        if presets:
            return presets
    except Exception:  # a missing file, an older or half-copied version: the built-in list still works
        pass
    return [dict(p) for p in FALLBACK_PRESETS]


def key_needed(base_url: str, presets: list[dict]) -> bool:
    """False for a provider that runs on this computer and takes no key (Ollama)."""
    try:
        got = urlsplit(base_url)
        for p in presets:
            if p["key_required"] or not p["base_url"]:
                continue
            known = urlsplit(p["base_url"])
            same_host = got.hostname == known.hostname or (got.hostname in LOCAL_HOSTS and known.hostname in LOCAL_HOSTS)
            if same_host and got.port == known.port:
                return False
    except ValueError:
        pass
    return True


def configure_llm(interactive: bool, reconfigure: bool) -> None:
    env_path = ROOT / ".env"
    lines, values = read_env(env_path)
    required = ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL")
    presets = load_presets()
    needed = [k for k in required if k != "LLM_API_KEY" or key_needed(values.get("LLM_BASE_URL", ""), presets)]
    if all(values.get(k) for k in needed) and not reconfigure:
        say("  LLM settings already in .env; keeping them (run setup with --reconfigure to change).")
        return
    if not interactive:
        updates = {k: os.environ.get(k, values.get(k, "")) for k in required}
        write_env(env_path, lines, updates)
        missing = [k for k in required if not updates[k] and (k != "LLM_API_KEY" or key_needed(updates["LLM_BASE_URL"], presets))]
        say("  Wrote .env" + (f"; still to fill in: {', '.join(missing)}" if missing else "."))
        return

    say("  JobHuntPA uses an AI model (LLM) to score jobs, draft cover letters, suggest job titles and research companies.")
    say("  Which provider do you have an API key for? (Ollama runs on this computer and needs no key.)")
    for i, preset in enumerate(presets, 1):
        say(f"    {i:>2}) {preset['label']}")
    say("     s) Skip for now (you can re-run setup later)")
    choice = ask("Choose", "1").lower()
    if choice == "s":
        write_env(env_path, lines, {k: values.get(k, "") for k in required})
        say("  Skipped. Scoring and research stay off until the LLM is configured.")
        return
    try:
        preset = presets[int(choice) - 1]
    except (ValueError, IndexError):
        preset = next((p for p in presets if p.get("id") == "custom"), presets[-1])
    if preset["base_url"]:
        base = preset["base_url"]
        say(f"  API address: {base}")
    else:
        base = ask("API address (base URL, e.g. https://api.example.com/v1)", values.get("LLM_BASE_URL", ""))
    base = base.strip().rstrip("/")
    same_server = base == values.get("LLM_BASE_URL", "").rstrip("/")  # reconfiguring the same provider keeps its model and key
    if preset["models"]:
        say(f"  Suggested models: {', '.join(preset['models'])}")
    model = ask("Model name", (values.get("LLM_MODEL", "") if same_server else "") or (preset["models"][0] if preset["models"] else ""))
    if preset["key_required"]:
        if preset["key_url"]:
            say(f"  Get a key at {preset['key_url']}")
        saved_key = values.get("LLM_API_KEY", "") if same_server else ""
        key = ask("API key (paste it; it is stored only in the .env file on this computer"
                  + ("; Enter keeps the saved one)" if saved_key else ")")) or saved_key
    else:
        key = ""
        say("  No API key is needed for this provider.")
    write_env(env_path, lines, {"LLM_API_KEY": key, "LLM_BASE_URL": base, "LLM_MODEL": model})
    say("  Saved to .env. Provider, model, key and reasoning levels can be changed in the app (Settings > LLM).")


COMMUTE_KEYS = (
    ("TFNSW_API_KEY", "Transport for NSW API key, for public-transport commute times (free:",
     "https://opendata.transport.nsw.gov.au, sign up, create an application, copy its API key)"),
    ("TOMTOM_API_KEY", "TomTom API key, for driving times in peak traffic (free:",
     "https://developer.tomtom.com, sign up, copy the default key)"),
)


def configure_commute(interactive: bool, reconfigure: bool) -> None:
    """Optional keys for the commute on each job's page. Asked once: a key
    left blank stays blank (the line in .env records that it was asked)."""
    env_path = ROOT / ".env"
    lines, values = read_env(env_path)
    names = [name for name, _, _ in COMMUTE_KEYS]
    if all(name in values for name in names) and not reconfigure:
        return
    if not interactive:
        write_env(env_path, lines, {n: os.environ.get(n, values.get(n, "")) for n in names})
        return
    say("  Optional: commute times on each job's page. Press Enter to skip; you can add them later in the app (Settings).")
    updates = {}
    for name, what, where in COMMUTE_KEYS:
        say(f"  {what}")
        say(f"    {where}")
        updates[name] = ask(name, values.get(name, ""))
    write_env(env_path, lines, updates)
    say("  Saved to .env.")


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
    step(4, total, "LLM and commute settings")
    ensure_dirs()
    configure_llm(interactive, args.reconfigure)
    configure_commute(interactive, args.reconfigure)
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

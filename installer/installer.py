#!/usr/bin/env python3
"""JobHuntPA one-time setup (Linux, macOS and Windows).

Run by setup.sh (Linux/macOS) and by JobHuntPA-Setup.exe (Windows); safe
to run again (it updates dependencies and keeps your settings and data).

Steps:
  1. create a private Python environment in .venv
  2. install the Python packages from requirements.txt
  3. add a menu shortcut (Linux)
  4. check the app imports

It asks nothing: where the data lives, the account and the AI provider are
the app's first run, in the browser (docs/design/open-source.md §1.5). The
browser for JavaScript-only careers pages is downloaded by the app, into the
data folder, when a careers page first needs it and you agree.

Standard library only: this runs before any dependency is installed.

Options:
  --non-interactive   for CI and scripted installs: the LLM_* environment variables are
                      written to the data folder's .env when the data folder is already
                      known (JOBHUNT_DATA_DIR, or an existing install); the app also reads
                      them from its environment
  --reconfigure       (kept for old scripts; does nothing: change the AI provider in the
                      app, Settings > AI provider)
  --skip-browser      (kept for old scripts; does nothing: setup no longer downloads it)
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import platform
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

MIN_PYTHON = (3, 10)
ROOT = Path(__file__).resolve().parent.parent
IS_WINDOWS = os.name == "nt"

# The providers offered in the LLM menu come from backend/llm_providers.py, the one list the app itself uses
# (the Settings tab, AI provider). It is loaded by file path so nothing of the app has to be installed yet, and so the
# Windows setup program (a frozen build of this file that runs against the downloaded source) needs no package
# import. If that file can't be read, this short list is used instead.
PRESETS_FILE = ROOT / "backend" / "llm_providers.py"
PATHS_FILE = ROOT / "backend" / "paths.py"
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


def load_paths():
    """backend/paths.py (where the data folder is), loaded by file path like the presets; None if it can't be read."""
    name = "_jobhunt_paths"
    try:
        spec = importlib.util.spec_from_file_location(name, PATHS_FILE)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        try:
            spec.loader.exec_module(module)
        finally:
            sys.modules.pop(name, None)
        return module
    except Exception:  # noqa: BLE001  (a missing or half-copied file: nothing is written then)
        return None


def settings_file() -> Path | None:
    """The .env the app will read, when the data folder is already known (JOBHUNT_DATA_DIR, the pointer, or an existing
    install's data folder; an existing install's .env next to the app); None before the first run has chosen the
    folder: nothing may be written before that."""
    paths = load_paths()
    folder = paths.locate(ROOT).folder if paths else None
    if folder is not None:
        return paths.env_file(folder, ROOT)
    return ROOT / ".env" if (ROOT / ".env").is_file() else None


def configure_llm(non_interactive: bool) -> None:
    """No questions: the AI provider is the first run's step 3, in the browser. With --non-interactive (CI), the LLM_*
    environment variables are written to the data folder's .env when it is known."""
    required = ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL")
    given = {k: os.environ.get(k, "").strip() for k in required}
    if not (non_interactive and any(given.values())):
        say("  The AI provider, model and key are asked for in the browser when JobHuntPA first starts (you can skip")
        say("  that and add them later in Settings > AI provider).")
        return
    env_path = settings_file()
    if env_path is None:
        say("  The data folder isn't chosen yet, so nothing is written: JobHuntPA reads the LLM_* variables from its")
        say("  environment when it starts, and its first run asks for them.")
        return
    lines, values = read_env(env_path)
    updates = {k: given[k] or values.get(k, "") for k in required}
    env_path.parent.mkdir(parents=True, exist_ok=True)
    write_env(env_path, lines, updates)
    if not IS_WINDOWS:
        os.chmod(env_path, 0o600)  # it holds the API key
    presets = load_presets()
    missing = [k for k in required if not updates[k] and (k != "LLM_API_KEY" or key_needed(updates["LLM_BASE_URL"], presets))]
    say(f"  Wrote the LLM settings to {env_path}" + (f"; still to fill in: {', '.join(missing)}" if missing else "."))


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
    """Imports the app, which writes nothing (the database, key file and .env are made when it starts). A throwaway data
    folder and settings file are given all the same, so the check can never touch real ones."""
    with tempfile.TemporaryDirectory(prefix="jobhuntpa-check-") as tmp:
        env = {**os.environ, "JOBHUNT_DATA_DIR": tmp, "JOBHUNT_ENV_PATH": str(Path(tmp) / ".env")}
        env.pop("JOBHUNT_DB_PATH", None)
        env.pop("DATABASE_URL", None)
        try:
            run([venv_python(), "-c", "import backend.app"], cwd=ROOT, env=env)
        except subprocess.CalledProcessError:
            fail("the app failed to load after installing. Please report this with the output above.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Set up JobHuntPA on this computer.")
    parser.add_argument("--non-interactive", action="store_true")
    parser.add_argument("--reconfigure", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--skip-browser", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()

    if sys.version_info < MIN_PYTHON:
        fail(f"Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} or newer is needed (this is {platform.python_version()}).")
    version = (ROOT / "VERSION").read_text().strip() if (ROOT / "VERSION").exists() else "dev"
    say(f"JobHuntPA setup (version {version}) in {ROOT}")
    if args.reconfigure:
        say("(--reconfigure isn't needed any more: change the AI provider in the app, Settings > AI provider.)")

    total = 4
    step(1, total, "Creating a private Python environment...")
    create_venv()
    step(2, total, "Installing packages (this can take a few minutes)...")
    install_requirements()
    configure_llm(args.non_interactive)
    step(3, total, "Shortcut")
    create_shortcut()
    step(4, total, "Checking the app...")
    check_app()

    say("\nSetup complete.")
    say("The first time JobHuntPA starts, your browser asks where to keep your data, then for your account (a name and")
    say("a password) and your AI provider. An existing install keeps its data folder and signs in as before. Lost the")
    say("password and every recovery code? The reset tool (reset.sh, or Reset JobHuntPA in the Start menu) starts over")
    say("and keeps the settings.")
    if IS_WINDOWS:
        say("Start JobHuntPA from the Desktop or Start-menu shortcut.")
    elif platform.system() == "Darwin":
        say('Start JobHuntPA by double-clicking "Start JobHuntPA.command" (or run ./start.sh).')
    else:
        say("Start JobHuntPA from your applications menu, or run ./start.sh in this folder.")


if __name__ == "__main__":
    main()

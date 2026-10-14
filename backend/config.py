"""Environment config (.env in the data folder, no extra dependency)."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from backend import paths

PROJECT_ROOT = Path(__file__).resolve().parent.parent
# The .env file the app reads, and the Settings tab writes (backend/app_settings.py): in the data folder, or an older
# install's next to the app (backend/paths.py env_file). Before the first run has chosen the data folder there is none
# to read (an older install's file next to the app is still read) and nothing writes one. JOBHUNT_ENV_PATH points
# tests and tools at a throwaway file, so they never read or change the real one.
ENV_PATH = Path(os.environ.get("JOBHUNT_ENV_PATH") or paths.env_file(paths.current().folder or paths.default_folder()))


# The names load_dotenv took from a .env file rather than from the process environment.
FROM_DOTENV: set[str] = set()


def load_dotenv(path: Optional[Path] = None) -> None:
    """Set KEY=VALUE pairs from ``path`` (default ENV_PATH) that are not already in the environment."""
    path = path or ENV_PATH
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = val
            FROM_DOTENV.add(key)


def process_env(name: str) -> str:
    """``name`` from the environment the process was started with, never from .env (for settings that must not
    be stored in a file, such as the account's password for CI and headless installs)."""
    return "" if name in FROM_DOTENV else os.environ.get(name, "")


load_dotenv()

# Identify ourselves honestly to every site we fetch from.
USER_AGENT = os.getenv(
    "JOBHUNT_USER_AGENT",
    "Mozilla/5.0 (compatible; JobHuntPA/1.0; personal job search tool)",
)
# Product token matched against robots.txt User-agent groups.
ROBOTS_AGENT = "jobhuntpa"

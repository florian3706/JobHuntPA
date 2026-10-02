"""Environment config (.env in the project root, no extra dependency)."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
# The .env file the app reads, and the Settings tab writes (backend/app_settings.py). JOBHUNT_ENV_PATH
# points tests and tools at a throwaway file, so they never read or change the real one.
ENV_PATH = Path(os.environ.get("JOBHUNT_ENV_PATH") or PROJECT_ROOT / ".env")


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


load_dotenv()

# Identify ourselves honestly to every site we fetch from.
USER_AGENT = os.getenv(
    "JOBHUNT_USER_AGENT",
    "Mozilla/5.0 (compatible; JobHuntPA/1.0; personal job search tool)",
)
# Product token matched against robots.txt User-agent groups.
ROBOTS_AGENT = "jobhuntpa"

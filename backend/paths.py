"""Where JobHuntPA keeps its data: the data folder (open-source spec §1.5).

Standard library only, and nothing else from backend/: installer/launch.py and installer/uninstaller.py load this file
by its path, before (or without) anything of the app being importable.

**Which folder**, in this order:
  1. ``JOBHUNT_DATA_DIR``: service installs (service.sh), CI, tests and tools. Used as it is and never asked about (a
     relative path is relative to the app folder).
  2. The ``data`` folder next to the app, **when it already holds JobHuntPA data** (``holds_data``): an install from
     before this version (or one whose first run chose that folder). It is used where it is, nothing moves and nobody
     is asked; it comes before the pointer, which is one per user, so a second copy of the app keeps its own data.
  3. The **pointer**: ``data-folder.json`` in the per-user configuration folder (``config_dir``), written when the
     first run's step 1 chooses a folder. It is used while the folder it names exists; when that folder has gone (an
     unplugged drive, a deleted folder), the first run asks again with that path filled in, rather than quietly
     starting an empty install somewhere.
  4. Otherwise nothing is chosen yet: the app starts in its first run's step 1 (backend/data_folder.py), which offers
     ``default_folder()`` and writes the pointer. Until then nothing but the pointer is written: no database, key file
     or .env (backend/db.py points the database at memory while no folder is chosen).

**Defaults** offered at step 1: Linux ``$XDG_DATA_HOME/JobHuntPA`` (``~/.local/share/JobHuntPA``); Windows and macOS the
``data`` folder in the app's folder, as before.

**In the data folder:** the database (``jobhunt.db``), the server key file (``server-keys.json``), the settings
(``.env``), the uploads, the page cache, the browser for JavaScript-only careers pages (``browsers/``, downloaded only
when the person agrees) and the launcher's ``running.json``. Two older layouts keep working (``env_file`` and
``key_file``): an install's ``.env`` next to the app is still read and written while the data folder has none, and a
key file in the app's ``data`` folder is still used while the data folder has none (an install that had moved only its
database with ``JOBHUNT_DATA_DIR``).
"""
from __future__ import annotations

import json
import os
import platform
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional

APP_NAME = "JobHuntPA"
APP_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR_ENV = "JOBHUNT_DATA_DIR"
CONFIG_DIR_ENV = "JOBHUNT_CONFIG_DIR"  # the per-user configuration folder (tests point it at a throwaway one)
POINTER_NAME = "data-folder.json"
POINTER_VERSION = 1
DB_NAME = "jobhunt.db"
KEY_NAME = "server-keys.json"
ENV_NAME = ".env"
PATH_MAX = 1000

# What a data folder holds. A folder chosen at step 1 must be new, empty, or hold only these (the app's own empty
# "data" folder ships with .gitkeep placeholders and an old sources.json): JobHuntPA's files are never mixed with the
# person's own, so the uninstaller can remove the folder after backing it up.
OWN_NAMES = frozenset({
    DB_NAME, f"{DB_NAME}-wal", f"{DB_NAME}-shm", f"{DB_NAME}.bak", KEY_NAME, f"{KEY_NAME}.new", f"{KEY_NAME}.lock",
    ENV_NAME, "uploads", "cache", "browsers", "leftovers", "first-run.txt", "running.json", "sources.json", ".gitkeep",
})


class FolderProblem(ValueError):
    """A folder that can't be the data folder; the message says why, in plain words."""


@dataclass(frozen=True)
class Place:
    """Where the data is. ``folder`` is None while nothing is chosen; ``how`` is env, pointer, app-folder or "";
    ``missing`` is the folder a pointer names when that folder has gone (step 1 offers it again)."""
    folder: Optional[Path]
    how: str
    missing: Optional[Path] = None


def _system(system: Optional[str]) -> str:
    return system or platform.system()


def _home(env: Mapping[str, str]) -> Path:
    home = env.get("HOME") or env.get("USERPROFILE")
    return Path(home) if home else Path.home()


def config_dir(env: Optional[Mapping[str, str]] = None, system: Optional[str] = None) -> Path:
    """The per-user configuration folder that keeps the pointer: Linux ``$XDG_CONFIG_HOME/JobHuntPA``
    (``~/.config/JobHuntPA``), Windows ``%APPDATA%\\JobHuntPA``, macOS ``~/Library/Application Support/JobHuntPA``.
    ``JOBHUNT_CONFIG_DIR`` overrides it."""
    env = os.environ if env is None else env
    override = (env.get(CONFIG_DIR_ENV) or "").strip()
    if override:
        return Path(override).expanduser()
    system = _system(system)
    if system == "Windows":
        base = env.get("APPDATA")
        return (Path(base) if base else _home(env) / "AppData" / "Roaming") / APP_NAME
    if system == "Darwin":
        return _home(env) / "Library" / "Application Support" / APP_NAME
    xdg = (env.get("XDG_CONFIG_HOME") or "").strip()
    return (Path(xdg) if xdg and Path(xdg).is_absolute() else _home(env) / ".config") / APP_NAME


def pointer_file(env: Optional[Mapping[str, str]] = None, system: Optional[str] = None) -> Path:
    return config_dir(env, system) / POINTER_NAME


def default_folder(root: Optional[Path] = None, env: Optional[Mapping[str, str]] = None,
                   system: Optional[str] = None) -> Path:
    """The folder step 1 offers: Linux ``$XDG_DATA_HOME/JobHuntPA`` (``~/.local/share/JobHuntPA``); Windows and macOS
    the ``data`` folder in the app's folder."""
    env = os.environ if env is None else env
    if _system(system) in ("Windows", "Darwin"):
        return (root or APP_ROOT) / "data"
    xdg = (env.get("XDG_DATA_HOME") or "").strip()
    return (Path(xdg) if xdg and Path(xdg).is_absolute() else _home(env) / ".local" / "share") / APP_NAME


def holds_data(folder: Path) -> bool:
    """Whether ``folder`` already holds a JobHuntPA install's data: its database or its key file."""
    try:
        return (folder / DB_NAME).is_file() or (folder / KEY_NAME).is_file()
    except OSError:
        return False


def read_pointer(env: Optional[Mapping[str, str]] = None, system: Optional[str] = None) -> Optional[Path]:
    """The folder the pointer names, or None (no pointer, or one that can't be read: it is then ignored)."""
    try:
        data = json.loads(pointer_file(env, system).read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return None
    folder = data.get("data_folder") if isinstance(data, dict) else None
    if not isinstance(folder, str) or not folder or len(folder) > PATH_MAX or "\x00" in folder:
        return None
    path = Path(folder)
    return path if path.is_absolute() else None


def write_pointer(folder: Path, env: Optional[Mapping[str, str]] = None, system: Optional[str] = None) -> Path:
    """Remember ``folder`` (absolute) as the data folder: the pointer is written atomically, owner-only, in a
    configuration folder made owner-only. Returns the pointer's path."""
    target = pointer_file(env, system)
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    text = json.dumps({"data_folder": str(folder), "version": POINTER_VERSION}, ensure_ascii=False) + "\n"
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=f".{POINTER_NAME}-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return target


def remove_pointer(env: Optional[Mapping[str, str]] = None, system: Optional[str] = None) -> bool:
    """Delete the pointer (the uninstaller), and the configuration folder when nothing else is in it."""
    target = pointer_file(env, system)
    try:
        target.unlink()
    except FileNotFoundError:
        return False
    try:
        target.parent.rmdir()
    except OSError:
        pass
    return True


def _from_env(env: Mapping[str, str], root: Path) -> Optional[Path]:
    value = (env.get(DATA_DIR_ENV) or "").strip()
    if not value:
        return None
    path = Path(value).expanduser()
    return path if path.is_absolute() else root / path


def locate(root: Optional[Path] = None, env: Optional[Mapping[str, str]] = None,
           system: Optional[str] = None) -> Place:
    """Where the data is (the order in this module's docstring). Reads, never writes. ``root`` is the app's folder
    (``APP_ROOT``)."""
    root = root or APP_ROOT
    env = os.environ if env is None else env
    chosen = _from_env(env, root)
    if chosen is not None:
        return Place(chosen, "env")
    beside = root / "data"
    if holds_data(beside):
        return Place(beside, "app-folder")
    pointed = read_pointer(env, system)
    if pointed is not None and pointed.is_dir():
        return Place(pointed, "pointer")
    return Place(None, "", missing=pointed)


_current: Optional[Place] = None


def current() -> Place:
    """``locate()`` for this process, worked out once (backend.config and backend.db read it at import)."""
    global _current
    if _current is None:
        _current = locate()
    return _current


def env_file(folder: Path, root: Optional[Path] = None) -> Path:
    """The settings file: ``.env`` in the data folder; an older install's ``.env`` next to the app while the data
    folder has none (it keeps being read and written where it is)."""
    inside = folder / ENV_NAME
    beside = (root or APP_ROOT) / ENV_NAME
    if not inside.is_file() and beside.is_file():
        return beside
    return inside


def key_file(folder: Path, root: Optional[Path] = None) -> Path:
    """The server key file: in the data folder; an older install's in the app's ``data`` folder while the data folder
    has none (before this version the key file stayed there when JOBHUNT_DATA_DIR moved the rest)."""
    inside = folder / KEY_NAME
    beside = (root or APP_ROOT) / "data" / KEY_NAME
    if not inside.exists() and beside != inside and beside.is_file():
        return beside
    return inside


# ---------------------------------------------------------------------------
# Step 1 of the first run: checking a folder, and making it
# ---------------------------------------------------------------------------

def _parse(text: object) -> Path:
    if not isinstance(text, str) or not text.strip():
        raise FolderProblem("Enter the folder where JobHuntPA should keep its data.")
    text = text.strip()
    if len(text) > PATH_MAX or any(ord(c) < 32 or ord(c) == 127 for c in text):
        raise FolderProblem("That folder path is too long or has characters a folder name can't have.")
    path = Path(os.path.expanduser(text)) if text.startswith("~") else Path(text)
    if not path.is_absolute():
        raise FolderProblem("Give the whole path of the folder, starting from the top (for example "
                            + str(default_folder()) + ").")
    if ".." in path.parts:
        raise FolderProblem("Give the folder's path without .. in it.")
    return Path(os.path.normpath(path))


def _foreign(folder: Path) -> int:
    """How many entries of ``folder`` aren't JobHuntPA's own."""
    try:
        names = [entry.name for entry in os.scandir(folder)]
    except OSError:
        raise FolderProblem("JobHuntPA can't look inside that folder. Choose another one.") from None
    return sum(1 for name in names if name not in OWN_NAMES and not name.startswith(f"{DB_NAME}.")
               and not (name.startswith(".") and name.endswith(".tmp")))  # (backups; crash-left temp files)


def check_folder(text: object) -> tuple[Path, str]:
    """Whether the folder ``text`` names can be the data folder, without writing anything. Returns (the absolute
    path, its kind): ``existing`` (it holds JobHuntPA data: used as it is), ``empty`` (there, and empty or with only
    JobHuntPA's own placeholders) or ``new`` (to be made). FolderProblem says why a folder can't be used: a relative
    path, a file, a folder holding other things, or one this program may not write to or create."""
    path = _parse(text)
    if path.exists() or path.is_symlink():
        if not path.is_dir():
            raise FolderProblem("That is a file, not a folder. Choose a folder.")
        if holds_data(path):
            if not os.access(path, os.W_OK | os.X_OK):
                raise FolderProblem("That folder holds JobHuntPA data, but JobHuntPA may not write to it.")
            return path, "existing"
        if _foreign(path):
            raise FolderProblem("That folder already holds other files. Choose a new or empty folder, so JobHuntPA's "
                                "data stays apart from your own files.")
        if not os.access(path, os.W_OK | os.X_OK):
            raise FolderProblem("JobHuntPA may not write to that folder. Choose one in your own folders.")
        return path, "empty"
    parent = path.parent
    while not parent.exists():
        if parent == parent.parent:
            break
        parent = parent.parent
    if not parent.is_dir() or not os.access(parent, os.W_OK | os.X_OK):
        raise FolderProblem("JobHuntPA can't create that folder here: it may not write to " + str(parent) + ".")
    return path, "new"


def prepare_folder(path: Path) -> None:
    """Make the chosen folder (owner-only) and check that a file can be written in it (the probe is deleted at once).
    FolderProblem when it can't."""
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, probe = tempfile.mkstemp(dir=path, prefix=".jobhuntpa-check-", suffix=".tmp")
        os.close(fd)
        os.unlink(probe)
    except OSError:
        raise FolderProblem("JobHuntPA couldn't create or write to that folder. Choose another one.") from None
    if os.name == "posix":
        try:
            if stat.S_IMODE(os.stat(path).st_mode) != 0o700:
                os.chmod(path, 0o700)
        except OSError:
            pass

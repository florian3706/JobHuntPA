"""The first run's step 1: where the data lives (open-source spec §1.5; backend/paths.py has the rules).

While no data folder is chosen (``db.FOLDER_CHOSEN`` false: no JOBHUNT_DATA_DIR, no pointer, and no ``data`` folder next
to the app holding JobHuntPA data), the app starts in this step only: the database is in memory and nothing is written,
every API but these, /api/health and /api/auth/state answers 401 ``setup_required`` (backend/sessions.py), and the
first-run page asks for the folder before the account.

    POST /api/auth/data-folder/check  {token, path?}  -> {default, previous, path, kind, problem}: whether ``path``
                                      (default: the folder offered) can be used, without writing anything. ``kind`` is
                                      existing (holds JobHuntPA data: used as it is, then sign in), empty or new.
    POST /api/auth/data-folder        {token, path}   -> {ok, kind, path, restarting: true}: makes the folder, writes
                                      the pointer and restarts the app on it (installer/launch.py starts it again).

**Who may call:** open, with the one-time first-run token, and only while no folder is chosen (409 ``folder_chosen``
afterwards). The token is what binds this to the computer that runs the app: it is printed in the app's own window
and log, and installer/launch.py opens the page with it in the address's #fragment (``JOBHUNT_FIRST_RUN_TOKEN`` hands
the same token to the app, and to the app again after the restart, so the page carries on to the account). Both
endpoints are JSON only, same-site and rate-limited per client address (``ratelimit.FOLDER``), like the account's first
run. A path must be absolute (``~`` is this computer's home folder), without ``..``, and name a folder the app may
create or write to that is new, empty, or already JobHuntPA's (``paths.check_folder``): never a folder of other files.

Nothing here logs a path (it can hold the person's name); the log says the step was done and which kind of folder.
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from backend import db, paths, users
from backend.sessions import AuthProblem

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])

CHECK = "/api/auth/data-folder/check"
CHOOSE = "/api/auth/data-folder"
PATHS = frozenset({CHECK, CHOOSE})
# The exit status that asks installer/launch.py to start the app again (now on the chosen folder).
RESTART_CODE = 3
RESTART_DELAY_S = 0.5  # long enough for the answer to reach the browser

_chosen_lock = threading.Lock()
_chosen = False  # set once a folder is chosen in this process: the restart is on its way


class CheckIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str = Field("", max_length=200)
    path: Optional[str] = Field(None, max_length=2000)


class ChooseIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str = Field("", max_length=200)
    path: str = Field("", max_length=2000)


def _guard(token: str) -> None:
    if db.FOLDER_CHOSEN or _chosen:
        raise AuthProblem(409, "folder_chosen", "JobHuntPA's data folder is already chosen. Reload the page.")
    if not users.check_first_run_token(token):
        raise AuthProblem(403, "bad_token", "That setup code isn't right. Use the link JobHuntPA showed in its window "
                                            "when it started.", field="token")


def _offer() -> dict:
    missing = paths.current().missing
    return {"default": str(missing or paths.default_folder()), "previous": str(missing) if missing else None}


@router.post("/data-folder/check")
def check(body: CheckIn):
    _guard(body.token)
    offer = _offer()
    text = offer["default"] if body.path is None else body.path
    try:
        path, kind = paths.check_folder(text)
    except paths.FolderProblem as problem:
        return {**offer, "path": text, "kind": None, "problem": str(problem)}
    return {**offer, "path": str(path), "kind": kind, "problem": None}


def _restart() -> None:
    """Ends this process with RESTART_CODE: the launcher starts the app again, which now finds the pointer."""
    logging.shutdown()
    os._exit(RESTART_CODE)


def restart_soon() -> None:
    threading.Timer(RESTART_DELAY_S, _restart).start()


@router.post("/data-folder")
def choose(body: ChooseIn):
    global _chosen
    _guard(body.token)
    try:
        path, kind = paths.check_folder(body.path)
    except paths.FolderProblem as problem:
        raise AuthProblem(422, "bad_folder", str(problem), field="path") from None
    with _chosen_lock:
        if _chosen:
            raise AuthProblem(409, "folder_chosen", "JobHuntPA's data folder is already chosen. Reload the page.")
        try:
            paths.prepare_folder(path)
            paths.write_pointer(path)
        except paths.FolderProblem as problem:
            raise AuthProblem(422, "bad_folder", str(problem), field="path") from None
        except OSError as exc:
            log.warning("Could not save where the data folder is: %s", exc.__class__.__name__)
            raise AuthProblem(500, "pointer_failed", "JobHuntPA couldn't save your choice in its settings folder ("
                                                     + str(paths.config_dir()) + "). Check that it can be written "
                                                     "to, then try again.") from None
        _chosen = True
    log.info("First run: the data folder is chosen (%s); restarting on it", kind)
    restart_soon()
    return {"ok": True, "kind": kind, "path": str(path), "restarting": True}

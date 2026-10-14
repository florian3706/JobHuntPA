"""The install's upkeep in the Settings tab: what the AI cost, the unencrypted copies left from before encryption, and
replacing the server key (open-source spec §1.4; these were in the admin panel).

    GET  /api/settings/usage                  AI requests and tokens: in all, the last 7 and 30 days, and by task
    GET  /api/settings/leftovers              {encryption, leftovers: [{path, bytes, kind}]}: plaintext from before
                                              encryption (backups, the old page cache), paths and sizes only
    POST /api/settings/leftovers/delete       (recent sign-in) overwrite and delete them
    POST /api/settings/server-key/rotate      (recent sign-in) replace the server key (backend/key_rotation.py)

**Who may call:** signed in; the two that change something need recent sign-in (the password entered in the last 15
minutes). None of them reads anyone's own data: usage is counted in plaintext columns (provider, model, task, token
counts) for the whole install, whoever made the request. Nothing here resets or wipes anything: starting over is the
reset tool's, run on the computer itself (backend/reset.py).
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Optional

from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlalchemy.orm import Session

from backend import key_rotation, keys, migrate_encryption, users
from backend.db import LlmUsage, get_db, utcnow
from backend.sessions import AuthProblem, AuthUser, current_user, require_recent_auth

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/settings", tags=["settings"], dependencies=[Depends(current_user)])

WINDOWS = {"last_7_days": 7, "last_30_days": 30}


def _hit_rate(cached: int, prompt: int) -> Optional[float]:
    return round(cached / prompt, 4) if prompt else None


def _figures(n: int, i: int, o: int, c: int, w: int) -> dict:
    return {"requests": n, "input": i, "output": o, "cached": c, "cache_write": w, "cache_hit_rate": _hit_rate(c, i)}


def _sums(db: Session, since=None) -> dict[Optional[str], dict]:
    """{task (purpose): figures} for the requests since ``since`` (all of them when None)."""
    zero = lambda col: func.coalesce(func.sum(func.coalesce(col, 0)), 0)  # noqa: E731
    q = db.query(LlmUsage.purpose, func.count(LlmUsage.id), zero(LlmUsage.input_tokens), zero(LlmUsage.output_tokens),
                 zero(LlmUsage.cached_tokens), zero(LlmUsage.cache_write_tokens))
    if since is not None:
        q = q.filter(LlmUsage.created_at >= since)
    return {purpose: _figures(int(n), int(i), int(o), int(c), int(w))
            for purpose, n, i, o, c, w in q.group_by(LlmUsage.purpose)}


def _total(by_task: dict[Optional[str], dict]) -> dict:
    keys_ = ("requests", "input", "output", "cached", "cache_write")
    sums = {k: sum(f[k] for f in by_task.values()) for k in keys_}
    return _figures(*(sums[k] for k in keys_))


@router.get("/usage")
def usage(db: Session = Depends(get_db)):
    """The install's AI use, every account's together: ``totals`` (since the first request), ``last_7_days`` and
    ``last_30_days``, and ``by_task`` (scoring, research, cover letters, chat ...), each with requests, input and
    output tokens, cached tokens and the cache hit rate."""
    now = utcnow()
    every = _sums(db)
    windows = {name: _sums(db, now - timedelta(days=days)) for name, days in WINDOWS.items()}
    first = db.query(func.min(LlmUsage.created_at)).scalar()
    empty = _figures(0, 0, 0, 0, 0)
    tasks = sorted(every, key=lambda t: (-every[t]["requests"], t or ""))
    return {
        "since": first.replace(microsecond=0).isoformat() + "Z" if first else None,
        "totals": _total(every),
        **{name: _total(found) for name, found in windows.items()},
        "by_task": [{"task": task or "other", "totals": every[task],
                     **{name: windows[name].get(task, empty) for name in WINDOWS}} for task in tasks],
    }


def _leftovers() -> list[dict]:
    return [{k: v for k, v in item.items() if not k.startswith("_")} for item in migrate_encryption.leftover_files()]


@router.get("/leftovers")
def leftovers():
    """The files outside the database that still hold plaintext from before encryption (types, paths and sizes only,
    never a value). Empty until the one-time encryption has finished."""
    progress = migrate_encryption.progress()
    return {"encryption": progress["state"], "leftovers": _leftovers() if progress["state"] == "done" else []}


@router.post("/leftovers/delete")
def delete_leftovers(user: AuthUser = Depends(current_user)):
    """Recent sign-in: overwrite and delete the leftover plaintext files (only those ``leftovers`` lists)."""
    require_recent_auth(user)
    if not migrate_encryption.is_done():
        raise AuthProblem(409, "encrypting", "Wait until your data is encrypted.")
    deleted = migrate_encryption.delete_leftovers()
    log.info("user %s deleted %d leftover plaintext file(s)", user.id, len(deleted))
    return {"ok": True, "deleted": deleted, "leftovers": _leftovers()}


@router.post("/server-key/rotate")
def rotate_server_key(user: AuthUser = Depends(current_user)):
    """Recent sign-in: replace the server key (the one that opens background-access keys with nobody signed in) and
    re-wrap everything that uses it. Safe to interrupt: start-up finishes it. Replaces a key, never removes data."""
    require_recent_auth(user)
    server = users.server_keys()
    try:
        moved = key_rotation.rotate(server)
    except keys.RotationIncomplete as exc:
        log.error("Server key rotation incomplete: %d value(s) left", len(exc.refs))
        raise AuthProblem(500, "rotation_incomplete", f"{len(exc.refs)} value(s) couldn't be moved to the new server "
                                                      "key, so the old one was kept. Nothing was lost; check the log.") \
            from None
    except keys.KeyFileError as exc:  # the file was replaced by another process: nothing was written
        raise AuthProblem(409, "key_file_changed", str(exc)) from None
    log.info("user %s replaced the server key (%d value(s) re-wrapped)", user.id, moved)
    return {"ok": True, "rewrapped": moved, "key": server.current_kek_id}

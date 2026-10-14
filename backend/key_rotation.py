"""Rotating the server key (KEK), the same code for the Settings tab's button and ``python -m backend.keys
rotate-kek`` (accounts design §1.3).

The KEK wraps what the server must open with nobody signed in: the data keys of people who allowed background access
(``user_keys.server_wrapped_dek``, label ``srv|{uid}``, with ``server_kek_id`` beside it).

``keys.ServerKeys.rotate_kek`` does the steps; this module says where the wrapped values are and how to store the
new ones:

1. the key file gets a new KEK as current, keeping the old (every blob's kid says which KEK made it, so old wraps
   still open and new ones use the new KEK);
2. every value not under the current KEK is re-wrapped, all in one transaction;
3. only when none is left is the old KEK dropped from the file.

A crash leaves the file with both KEKs and some values still under the old one; ``finish_pending`` (start-up runs it)
carries on from there. A value is stored only if it still holds what was read (a person who turned background access
off meanwhile doesn't get their key written back).
"""
from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any

from sqlalchemy import update

from backend import keys
from backend.db import DATA_DIR, AppSetting, SessionLocal, User, UserKeys, engine, init_db, scrub_wal

log = logging.getLogger(__name__)

def _rows_factory(server: keys.ServerKeys):
    seen: dict[Any, bytes] = {}  # ref -> the blob that was read, so a store can check it hasn't changed

    def rows() -> list[keys.KekWrapped]:
        out: list[keys.KekWrapped] = []
        db = SessionLocal()
        try:
            for uid, blob in db.query(UserKeys.user_id, UserKeys.server_wrapped_dek).filter(
                    UserKeys.server_wrapped_dek.isnot(None)):
                out.append(keys.KekWrapped(("srv", uid), f"srv|{uid}", bytes(blob)))
        finally:
            db.close()
        seen.update({row.ref: row.blob for row in out})
        return out

    return rows, seen


def _store_factory(seen: dict[Any, bytes]):
    def store(todo: list[tuple[Any, str, bytes]]) -> None:
        with engine.begin() as conn:  # one transaction: all of them or none
            for (kind, uid), kek_id, blob in todo:
                old = seen.get((kind, uid))
                conn.execute(update(UserKeys).where(UserKeys.user_id == uid, UserKeys.server_wrapped_dek == old)
                             .values(server_wrapped_dek=blob, server_kek_id=kek_id))
    return store


def rotate(server: keys.ServerKeys) -> int:
    """Rotate the KEK (both steps). Returns how many values were re-wrapped. keys.RotationIncomplete if some value
    couldn't be moved: the old KEK then stays in the key file and nothing is lost."""
    rows, seen = _rows_factory(server)
    moved = server.rotate_kek(rows, _store_factory(seen))
    scrub_wal()  # the values under the retired key must not linger in the write-ahead log
    return moved


def finish_pending(server: keys.ServerKeys) -> int:
    """Carry on a rotation a crash interrupted (the file holds more than one KEK). 0 when none is pending."""
    if not server.kek_rotation_pending:
        return 0
    rows, seen = _rows_factory(server)
    moved = server.finish_kek_rotation(rows, _store_factory(seen))
    scrub_wal()
    return moved


def _process_alive(pid: int) -> bool:
    if os.name == "nt":  # os.kill would end the process there
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True  # not ours to signal, but there
    return True


def refuse_while_running() -> None:
    """KeyFileError when the app is running: these commands replace the key file, and a running app would carry on
    from its own copy of it. The app holds a lock on the key file while it runs (``keys.hold_key_file``, which works
    however it was started); the launcher's ``data/running.json`` is a second check. Where this returns, the lock is
    held by this process, so an app can't start under the command either."""
    keys.hold_key_file()
    try:
        pid = json.loads((DATA_DIR / "running.json").read_text()).get("pid")
    except (OSError, ValueError, AttributeError):
        return
    if isinstance(pid, int) and pid > 0 and pid != os.getpid() and _process_alive(pid):
        raise keys.KeyFileError("JobHuntPA is running. Stop it first, then run this again. (If it isn't running, "
                                "delete data/running.json.)")


def server_keys_for_cli() -> keys.ServerKeys:
    """The key file for a command-line tool: it must belong to this database (the same check as start-up). Refuses
    while the app is running."""
    refuse_while_running()
    init_db()
    db = SessionLocal()
    try:
        has_users = db.query(User.id).first() is not None
        row = db.get(AppSetting, "server_keys_fingerprint")
        stored = json.loads(row.value_json) if row else None
    finally:
        db.close()
    if not has_users:
        raise keys.KeyFileError("There are no accounts yet, so there is nothing to do.")
    return keys.load_or_create(True, stored)


def rotate_kek_command(out=None, err=None) -> int:
    """``python -m backend.keys rotate-kek`` (run with the app stopped)."""
    out, err = out or sys.stdout, err or sys.stderr
    try:
        server = server_keys_for_cli()
        moved = rotate(server)
    except keys.RotationIncomplete as exc:
        print(f"{exc} Nothing was lost; run it again after checking the log.", file=err)
        return 2
    except keys.KeyFileError as exc:
        print(str(exc), file=err)
        return 2
    print(f"Done. The server key is now {server.current_kek_id}; {moved} wrapped value(s) were moved to it.",
          file=out)
    return 0

"""Rotating the server key (KEK), the same code for the admin panel's button and ``python -m backend.keys
rotate-kek`` (accounts design §1.3).

The KEK wraps what the server must open with nobody signed in: the data keys of people who allowed background access
(``user_keys.server_wrapped_dek``, label ``srv|{uid}``, with ``server_kek_id`` beside it) and the two-step sign-in
secrets (``users.totp_secret_enc`` and ``users.totp_pending_enc``, label ``totp|{uid}``).

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

from sqlalchemy import or_, update

from backend import crypto, keys
from backend.db import DATA_DIR, AppSetting, SessionLocal, User, UserKeys, engine, init_db, scrub_wal

log = logging.getLogger(__name__)

# The pending enrolment secret may have been wrapped under a label of its own; the one that opens is used.
_PENDING_LABELS = ("totp|{uid}", "totp_pending|{uid}")


def _pending_label(server: keys.ServerKeys, uid: int, blob: bytes) -> str:
    for template in _PENDING_LABELS:
        label = template.format(uid=uid)
        try:
            server.kek_unwrap(blob, label)
            return label
        except crypto.CryptoError:
            continue
    return _PENDING_LABELS[0].format(uid=uid)


def _rows_factory(server: keys.ServerKeys):
    seen: dict[Any, bytes] = {}  # ref -> the blob that was read, so a store can check it hasn't changed

    def rows() -> list[keys.KekWrapped]:
        out: list[keys.KekWrapped] = []
        db = SessionLocal()
        try:
            for uid, blob in db.query(UserKeys.user_id, UserKeys.server_wrapped_dek).filter(
                    UserKeys.server_wrapped_dek.isnot(None)):
                out.append(keys.KekWrapped(("srv", uid), f"srv|{uid}", bytes(blob)))
            for uid, secret, pending in db.query(User.id, User.totp_secret_enc, User.totp_pending_enc).filter(
                    or_(User.totp_secret_enc.isnot(None), User.totp_pending_enc.isnot(None))):
                if secret is not None:
                    out.append(keys.KekWrapped(("totp", uid), f"totp|{uid}", bytes(secret)))
                if pending is not None:
                    out.append(keys.KekWrapped(("totp_pending", uid), _pending_label(server, uid, bytes(pending)),
                                               bytes(pending)))
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
                if kind == "srv":
                    stmt = (update(UserKeys).where(UserKeys.user_id == uid, UserKeys.server_wrapped_dek == old)
                            .values(server_wrapped_dek=blob, server_kek_id=kek_id))
                else:
                    column = User.totp_secret_enc if kind == "totp" else User.totp_pending_enc
                    stmt = update(User).where(User.id == uid, column == old).values({column.key: blob})
                conn.execute(stmt)
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
    """KeyFileError when the launcher's ``data/running.json`` names a live process: these commands replace the key
    file, and a running app would carry on from its own copy of it."""
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

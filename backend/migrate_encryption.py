"""One-time encryption of the data an install had before accounts (docs/design/accounts.md §5.4).

The first account owns every workspace from before accounts (backend.users.create_account), so everything is
sealed under its data key. That key exists only while it is signed in, so the run starts when it is:

* at the first run (``POST /api/auth/first-run``), with the key the new account was made with;
* at start-up for an account made from the environment (CI, headless servers), in the start-up itself;
* otherwise at their next sign-in, or their next request with a session from before a restart
  (``backend.sessions``). A small database is done within that request; a larger one runs in its own thread while
  every data API answers 423 ``{"code": "encrypting", "done", "total"}`` and the page shows the progress.

**Steps** (state in ``app_settings["encryption_migration"]``: pending, running, failed, done):

1. A plaintext backup with SQLite's online backup API, once: ``<db>.bak-before-encryption``.
2. Uploads, file by file: ``<hex>.sealed`` written beside the original (temp file, fsync, rename), opened again and
   compared by SHA-256 with the original, ``documents.stored_name`` pointed at it and committed, then the original
   deleted. A crash leaves the original, the sealed copy, or both; the next run finishes either case.
3. Tables, in batches of 200 rows per transaction, written with Core ``update()`` (re-saving an unchanged legacy
   value through the ORM issues no UPDATE, accounts design §15). A row is taken while any of its sealed columns
   still holds plaintext (or an always-sealed one SQL NULL), so a run that stops anywhere carries on where it was,
   and running it again changes nothing. ``jobs.url_index`` and ``company_sources.careers_url_index`` are computed on
   the way; a pool link's URL is cleared (its posting has it). Old ``commute_cache`` and ``http_cache`` rows are
   deleted.
4. Check: no plaintext left in any sealed column (``plaintext_audit``), and a sample of each table opens.
5. ``PRAGMA secure_delete=ON``, ``wal_checkpoint(TRUNCATE)``, ``VACUUM`` (if the disk has room), checkpoint again,
   so the old plaintext pages are gone from the database and its WAL.
6. ``done``, with a report (rows and files sealed, the backup, VACUUM's outcome), and strict mode on: from then on a
   plaintext value in a sealed column is refused (backend.crypto). "Done" is also written into the key file
   (``encryption_done_at``, backend.keys), which strict mode trusts over the database: someone who can only write the
   database can't set the state back to pending and have the next run seal planted plaintext as if it were real.
   Once the key file says done the run is never repeated (``MigrationError``).

Afterwards the plaintext copies outside the database (the backup above, older ``*.bak-*`` backups, the unused
``data/cache/*.html`` pages and any upload left unsealed) are listed by ``leftover_files``. They are never deleted
silently: the Settings tab lists them and offers "Delete them" (``POST /api/settings/leftovers/delete``,
backend/maintenance.py: recent sign-in), and a notice on the dashboard points there. The reset tool
(backend/reset.py) deletes them too.

    GET  /api/encryption/status              {state, done, total, error?, report, finished_at, leftovers} (signed in)
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Depends
from sqlalchemy import Table, select, text, update

from backend import crypto, keys
from backend.db import DATA_DIR, DB_PATH, UPLOAD_DIR, Base, engine, online_backup
from backend.sessions import AuthUser, current_user

log = logging.getLogger(__name__)

STATE_KEY = "encryption_migration"
BATCH_ROWS = 200
SMALL_WAIT_S = 20          # a run this short finishes inside the sign-in request (no progress page)
RETRY_AFTER_S = 60         # a failed run is tried again at most this often
SAMPLE_ROWS = 20
SEALED_SUFFIX = ".sealed"  # backend.docs.SEALED_SUFFIX (import cycle)

_WS = "SELECT id FROM workspaces WHERE owner_id = :owner"
# Which rows of each table are the owner's (all data from before accounts is): SQL over plaintext columns only.
OWNED = {
    "workspaces": "owner_id = :owner",
    "jobs": f"workspace_id IN ({_WS})",
    "job_duplicates": f"workspace_id IN ({_WS})",
    "documents": f"workspace_id IN ({_WS})",
    "pins": f"workspace_id IN ({_WS})",
    "company_sources": f"workspace_id IN ({_WS})",
    "chat_threads": f"workspace_id IN ({_WS})",
    "search_runs": f"workspace_id IN ({_WS})",
    "user_profile": f"id IN ({_WS})",
    "dealbreakers": f"id IN ({_WS})",
    "fit_results": f"job_id IN (SELECT id FROM jobs WHERE workspace_id IN ({_WS}))",
    "cover_letters": f"job_id IN (SELECT id FROM jobs WHERE workspace_id IN ({_WS}))",
    "chat_messages": f"thread_id IN (SELECT id FROM chat_threads WHERE workspace_id IN ({_WS}))",
    "search_profile": "1 = 1",  # legacy, unowned: the first account's
}
# Caches: their old rows are deleted, not sealed (commute_cache becomes per user; http_cache is process-keyed).
CACHES = ("commute_cache", "http_cache")
# company_sources columns from an earlier schema: emptied rather than sealed (they're unused).
LEGACY_SOURCE_COLUMNS = ("company", "board", "host", "tenant", "site", "base_url")


class MigrationError(RuntimeError):
    """The run can't go on. The message never holds user data."""


def sealed_tables() -> list[tuple[Table, list]]:
    """Every table with columns under a user's key, with those columns (the caches excluded)."""
    out = []
    for table in Base.metadata.sorted_tables:
        columns = crypto.sealed_columns(table)
        if columns and table.name not in CACHES:
            out.append((table, columns))
    return out


def _pending_sql(column) -> str:
    """SQL: this column still holds what the run must seal (plaintext; SQL NULL in an always-sealed column)."""
    if column.type.always:
        return f"typeof({column.name}) != 'blob'"
    return f"typeof({column.name}) NOT IN ('blob', 'null')"


def _pending_where(table: Table, columns: list) -> str:
    return "(" + " OR ".join(_pending_sql(c) for c in columns) + ")"


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_thread: Optional[threading.Thread] = None
_done = False
_progress = {"done": 0, "total": 0}
_last_failure = 0.0


def _get_state(conn) -> dict:
    raw = conn.execute(text("SELECT value_json FROM app_settings WHERE key = :k"), {"k": STATE_KEY}).scalar()
    try:
        value = json.loads(raw) if raw else {}
    except ValueError:
        value = {}
    return value if isinstance(value, dict) else {}


def _put_state(conn, value: dict) -> None:
    conn.execute(text("INSERT INTO app_settings (key, value_json) VALUES (:k, :v) "
                      "ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json"),
                 {"k": STATE_KEY, "v": json.dumps(value, default=str)})


def _update_state(**values) -> dict:
    with engine.begin() as conn:
        state = {**_get_state(conn), **values}
        _put_state(conn, state)
        return state


def state() -> dict:
    with engine.connect() as conn:
        return _get_state(conn)


def owner_of(value: dict) -> Optional[int]:
    """Whose key the run uses: the first account, recorded when it was made (or the lowest account id)."""
    if isinstance(value.get("owner"), int):
        return value["owner"]
    with engine.connect() as conn:
        return conn.execute(text("SELECT MIN(id) FROM users")).scalar()


def _marked() -> bool:
    """Whether the key file says the run has finished (see the module docstring)."""
    return keys.encryption_marked()


def _mark_done() -> None:
    """Writes "done" into the key file, once (an install from before the marker gets it at its next start). Best
    effort: without the loaded keys (or a key file that can't be written) the database's state still decides."""
    from backend import users

    try:
        if users.server_keys().mark_encryption_done():
            log.info("The key file now records that the data is encrypted")
    except (RuntimeError, keys.KeyFileError, OSError) as exc:
        log.warning("Could not record in the key file that the data is encrypted: %s", exc.__class__.__name__)


def is_done() -> bool:
    """Whether the run has finished on this database (cached once true: it never goes back)."""
    global _done
    if not _done:
        if _marked():
            _done = True
            crypto.set_strict(True)
        elif state().get("state") == "done":
            _done = True
            crypto.set_strict(True)
            _mark_done()
    return _done


def known_done() -> bool:
    """is_done() without asking the database: True once the run is known to have finished."""
    return _done


def is_owner(user_id: int) -> bool:
    """Whether the run needs ``user_id``'s key."""
    return owner_of(state()) == user_id


def progress() -> dict:
    """{state, done, total, error?} for the 423 answer and the status endpoint."""
    value = state()
    out = {"state": value.get("state") or "pending", "done": _progress["done"], "total": _progress["total"]}
    if value.get("state") == "failed":
        out["error"] = value.get("error") or "unknown error"
    return out


def at_startup() -> None:
    """At every start: a run the last process left running resumes at the owner's next sign-in; after the run,
    strict mode is on."""
    value = state()
    if _marked():
        if value.get("state") != "done":  # the database was set back (a restored copy, or tampering): the key file wins
            _update_state(state="done", error=None)
            log.error("The key file says the data was encrypted, but the database said %r. Strict mode stays on: "
                      "plaintext found in it is refused, not sealed.", value.get("state") or "nothing")
    elif value.get("state") == "running":
        _update_state(state="pending", interrupted_at=datetime.utcnow().isoformat(timespec="seconds"))
        log.warning("Encrypting the data was interrupted; it carries on when the account that started it signs in.")
    is_done()


# ---------------------------------------------------------------------------
# Starting it
# ---------------------------------------------------------------------------

def ensure_started(user_id: int, dek: bytes, wait_s: float = SMALL_WAIT_S) -> bool:
    """Start (or resume) the run with ``user_id``'s key, if it's theirs to do and it isn't done or running. The
    request that starts it waits up to ``wait_s``, so a small database is done before it answers; requests that
    find it running wait only a moment. Returns whether it is done."""
    global _thread
    if is_done():
        return True
    with _lock:
        if _thread is not None and _thread.is_alive():
            thread, wait_s = _thread, min(wait_s, 2.0)
        else:
            value = state()
            if value.get("state") == "done" or owner_of(value) != user_id:
                return is_done()
            if value.get("state") == "failed" and time.monotonic() - _last_failure < RETRY_AFTER_S:
                return False
            thread = threading.Thread(target=_run_in_thread, args=(user_id, dek), name="jobhunt-encrypt",
                                      daemon=True)
            _thread = thread
            thread.start()
    thread.join(wait_s)
    return is_done()


def _run_in_thread(user_id: int, dek: bytes) -> None:
    try:
        run(user_id, dek)
    except Exception:  # recorded in the state by run(); the thread just ends
        pass


def run(user_id: int, dek: bytes) -> dict:
    """The whole run, in this thread, under ``user_id``'s key (see the module docstring). Returns the final state.
    Raises MigrationError (or what went wrong) after recording ``failed``."""
    global _last_failure
    with crypto.as_user(user_id, dek):
        try:
            return _run(user_id)
        except Exception as exc:
            _last_failure = time.monotonic()
            reason = str(exc) if isinstance(exc, MigrationError) else exc.__class__.__name__
            _update_state(state="failed", error=reason, failed_at=datetime.utcnow().isoformat(timespec="seconds"))
            log.error("Encrypting the data failed: %s. It is tried again at the account's next sign-in.", reason)
            raise


def _run(owner: int) -> dict:
    started = time.monotonic()
    value = state()
    if value.get("state") == "done":
        return value
    if _marked():  # (the database was set back: the key file wins, and nothing found now is sealed as if real)
        raise MigrationError("The key file says the data is already encrypted, so nothing is sealed again")
    if owner_of(value) != owner:
        raise MigrationError("Only the first account's key can encrypt the data from before accounts")
    _update_state(state="running", owner=owner, error=None, started_at=value.get("started_at")
                  or datetime.utcnow().isoformat(timespec="seconds"))
    from backend import pool_migration

    pool_migration.migrate()  # it reads job rows as plaintext, so it must be done first (a no-op once done)
    # A new install has nothing worth a backup (and a backup would be a leftover plaintext file to delete).
    backup = backup_database() if has_user_data() else None
    files = _documents_with_plaintext_files(owner)
    _progress["done"], _progress["total"] = 0, len(files) + count_pending_rows(owner)
    log.info("Encrypting the data: %d rows and files to seal", _progress["total"])
    report: dict[str, Any] = {"rows": {}, "files_sealed": 0, "files_missing": 0, "values_cleared": 0,
                              "backup": backup.name if backup else None}
    seal_files(owner, files, report)
    seal_tables(owner, report)
    clear_old_caches()
    audit = plaintext_audit()
    if audit["columns"]:
        raise MigrationError("Plaintext is left in " + ", ".join(sorted(audit["columns"])))
    sample_check(owner)
    report["vacuum"] = vacuum()
    report["seconds"] = round(time.monotonic() - started, 1)
    final = _update_state(state="done", error=None, report=report,
                          finished_at=datetime.utcnow().isoformat(timespec="seconds"))
    global _done
    _done = True
    crypto.set_strict(True)
    _mark_done()
    log.info("Encrypting the data is done: %d rows and %d files in %.1f s", sum(report["rows"].values()),
             report["files_sealed"], report["seconds"])
    return final


# ---------------------------------------------------------------------------
# 1. The backup
# ---------------------------------------------------------------------------

def backup_path() -> Path:
    return DB_PATH.parent / f"{DB_PATH.name}.bak-before-encryption"


# Tables that hold what a user made (a new install's first workspace aside).
USER_DATA = ("jobs", "documents", "company_sources", "pins", "user_profile", "dealbreakers", "chat_threads",
             "search_runs", "cover_letters", "search_profile")


def has_user_data() -> bool:
    with engine.connect() as conn:
        return any(conn.execute(text(f"SELECT 1 FROM {name} LIMIT 1")).first() for name in USER_DATA)


def backup_database() -> Optional[Path]:
    """A plaintext copy of the database as it is, once (SQLite's online backup API, safe while it's in use)."""
    if engine.dialect.name != "sqlite" or not DB_PATH.exists():
        return None
    target = backup_path()
    if target.exists():
        return target
    return online_backup(target)


# ---------------------------------------------------------------------------
# 2. Uploads
# ---------------------------------------------------------------------------

def _documents_with_plaintext_files(owner: int) -> list[tuple[int, str, Optional[str]]]:
    """(id, stored_name, filetype) of the owner's documents whose file isn't sealed yet."""
    with engine.connect() as conn:
        rows = conn.execute(text(f"SELECT id, stored_name, filetype FROM documents WHERE {OWNED['documents']} "
                                 f"AND stored_name IS NOT NULL AND stored_name != '' "
                                 f"AND stored_name NOT LIKE '%{SEALED_SUFFIX}' ORDER BY id"), {"owner": owner}).all()
    return [tuple(r) for r in rows]


def _sealed_name(stored_name: str) -> str:
    return Path(stored_name).stem + SEALED_SUFFIX


def _file_label(stored_name: str) -> str:
    return f"file|{stored_name}"  # backend.docs.file_label


def _point_document_at(doc_id: int, stored_name: str) -> None:
    with engine.begin() as conn:
        conn.execute(text("UPDATE documents SET stored_name = :s WHERE id = :i"), {"s": stored_name, "i": doc_id})


def _sealed_copy_matches(path: Path, name: str, digest: bytes) -> bool:
    try:
        return hashlib.sha256(crypto.open_file(path, _file_label(name))).digest() == digest
    except (OSError, crypto.CryptoError):
        return False


def seal_files(owner: int, files: list, report: dict) -> None:
    for doc_id, stored, _filetype in files:
        original = UPLOAD_DIR / stored
        name = _sealed_name(stored)
        target = UPLOAD_DIR / name
        if original.is_file():
            data = original.read_bytes()
            digest = hashlib.sha256(data).digest()
            if not _sealed_copy_matches(target, name, digest):  # (a run that stopped may have written it already)
                crypto.seal_file(data, target, _file_label(name))  # temp file, fsync, rename
                if not _sealed_copy_matches(target, name, digest):
                    raise MigrationError("A sealed upload didn't open back to the original")
            _point_document_at(doc_id, name)
            original.unlink()
            report["files_sealed"] += 1
        elif target.is_file():  # sealed, then stopped before the database was pointed at it
            try:
                crypto.open_file(target, _file_label(name))
            except crypto.CryptoError:
                raise MigrationError("A sealed upload from an earlier run doesn't open") from None
            _point_document_at(doc_id, name)
            report["files_sealed"] += 1
        else:
            report["files_missing"] += 1  # the file was gone before the run: nothing to seal
            _point_document_at(doc_id, None)
        _progress["done"] += 1
    # A plaintext original left beside its sealed copy (stopped after pointing the document at the copy).
    with engine.connect() as conn:
        rows = conn.execute(text(f"SELECT stored_name, filetype FROM documents WHERE {OWNED['documents']} "
                                 f"AND stored_name LIKE '%{SEALED_SUFFIX}'"), {"owner": owner}).all()
    for stored, filetype in rows:
        stem = Path(stored).stem
        for original in UPLOAD_DIR.glob(f"{stem}.*"):
            if original.name != stored and original.is_file() and (UPLOAD_DIR / stored).is_file():
                crypto.open_file(UPLOAD_DIR / stored, _file_label(stored))  # the sealed copy opens: safe to delete
                original.unlink()


# ---------------------------------------------------------------------------
# 3. Tables
# ---------------------------------------------------------------------------

def count_pending_rows(owner: int) -> int:
    total = 0
    with engine.connect() as conn:
        for table, columns in sealed_tables():
            total += conn.execute(text(f"SELECT COUNT(*) FROM {table.name} WHERE {OWNED[table.name]} "
                                       f"AND {_pending_where(table, columns)}"), {"owner": owner}).scalar()
    return total


def _encodable(column, value: Any) -> bool:
    try:
        column.type._encode(value)
        return True
    except (crypto.SealedValueError, TypeError, ValueError):
        return False


def _fallback(column) -> Any:
    """What a NOT NULL column gets in place of a stored value that can't be used: its default, else an empty one."""
    if column.default is not None and not callable(column.default.arg):
        return column.default.arg
    return {crypto.SealedJSON: [], crypto.SealedBool: False, crypto.SealedInt: 0,
            crypto.SealedFloat: 0.0}.get(type(column.type), "")


def _new_value(column, raw: Any, report: dict) -> tuple[bool, Any]:
    """(whether to write this column, the Python value to seal) for one stored value."""
    if isinstance(raw, (bytes, bytearray, memoryview)):
        return False, None  # sealed already (only plaintext and NULL are taken)
    if raw is None:
        return column.type.always, None  # an always-sealed column gets its sealed NULL marker
    value = column.type.coerce_legacy(raw)
    if value is not None and not _encodable(column, value):  # the wrong kind of value: it couldn't have been used
        report["values_cleared"] += 1
        value = None
    if value is None and not column.nullable:
        value = _fallback(column)
    return True, value


def _row_values(table: Table, columns: list, row: Any, posting_urls: dict, report: dict) -> dict:
    values = {}
    for column in columns:
        write, value = _new_value(column, getattr(row, column.name), report)
        if write:
            values[table.c[column.name]] = value
    if table.name == "jobs":
        plain_url = row.url if isinstance(row.url, str) else None
        if row.posting_id is not None:  # a pool link: its URL is its posting's, found again by the blind index
            url = posting_urls.get(row.posting_id) or plain_url
            values[table.c.url] = None
            values[table.c.url_index] = crypto.blind_index("url", url) if url else None
        elif plain_url is not None:  # a private job: its URL is sealed (above) and indexed
            values[table.c.url_index] = crypto.blind_index("url", plain_url) if plain_url else None
    elif table.name == "company_sources":
        for name in LEGACY_SOURCE_COLUMNS:
            if table.c[name] in values:
                values[table.c[name]] = ""  # emptied (unused since an earlier schema)
        if table.c.careers_url in values:
            url = values[table.c.careers_url]
            values[table.c.careers_url_index] = crypto.blind_index("careers_url", url) if url else None
    return values


def _seal_batch(table: Table, columns: list, owner: int, report: dict) -> int:
    """Seal one batch of the owner's rows that still hold plaintext. Returns how many rows."""
    names = ["id"] + [c.name for c in columns]
    if table.name == "jobs":
        names += ["posting_id"]
    with engine.begin() as conn:
        rows = conn.execute(text(f"SELECT {', '.join(dict.fromkeys(names))} FROM {table.name} "
                                 f"WHERE {OWNED[table.name]} AND {_pending_where(table, columns)} "
                                 f"ORDER BY id LIMIT :n"), {"owner": owner, "n": BATCH_ROWS}).all()
        posting_urls = {}
        if table.name == "jobs":
            ids = sorted({r.posting_id for r in rows if r.posting_id is not None})
            if ids:
                posting_urls = dict(conn.execute(select(Base.metadata.tables["postings"].c.id,
                                                        Base.metadata.tables["postings"].c.url)
                                                 .where(Base.metadata.tables["postings"].c.id.in_(ids))).all())
        for row in rows:
            values = _row_values(table, columns, row, posting_urls, report)
            conn.execute(update(table).where(table.c.id == row.id).values(values))
    return len(rows)


def seal_tables(owner: int, report: dict) -> None:
    for table, columns in sealed_tables():
        sealed = 0
        while True:
            n = _seal_batch(table, columns, owner, report)
            if not n:
                break
            sealed += n
            _progress["done"] += n
        report["rows"][table.name] = report["rows"].get(table.name, 0) + sealed


def clear_old_caches() -> None:
    """commute_cache rows from before (shared, plaintext) and every http_cache row (plaintext URLs and pages)."""
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM commute_cache WHERE owner_id IS NULL OR typeof(result_json) != 'blob'"))
        conn.execute(text("DELETE FROM http_cache"))


# ---------------------------------------------------------------------------
# 4. Checks
# ---------------------------------------------------------------------------

def plaintext_audit() -> dict:
    """{"columns": {"table.column": rows that aren't sealed}, "files": [uploads that aren't sealed]}: zero and empty
    once the run is done. Covers every row, whoever owns it: an always-sealed column holding SQL NULL counts, and so
    does a blob that isn't an envelope. Reads only types and first bytes, never a value."""
    found: dict[str, int] = {}
    with engine.connect() as conn:
        for table, columns in sealed_tables():
            for column in columns:
                n = conn.execute(text(f"SELECT COUNT(*) FROM {table.name} WHERE {_pending_sql(column)} OR "
                                      f"(typeof({column.name}) = 'blob' AND substr({column.name}, 1, 1) != X'01')")
                                 ).scalar()
                if n:
                    found[f"{table.name}.{column.name}"] = n
        for name in CACHES:
            n = conn.execute(text(f"SELECT COUNT(*) FROM {name} WHERE "
                                  + ("typeof(body) != 'blob'" if name == "http_cache"
                                     else "owner_id IS NULL OR typeof(result_json) != 'blob'"))).scalar()
            if n:
                found[f"{name}"] = n
        stored = {s for (s,) in conn.execute(text("SELECT stored_name FROM documents WHERE stored_name IS NOT NULL "
                                                  "AND stored_name != ''"))}
    files = sorted(s for s in stored if not s.endswith(SEALED_SUFFIX))
    return {"columns": found, "files": files}


def sample_check(owner: int) -> None:
    """A sample of each table's rows opens under the owner's key."""
    with engine.connect() as conn:
        for table, _columns in sealed_tables():
            try:
                conn.execute(select(table).where(text(OWNED[table.name])).order_by(table.c.id).limit(SAMPLE_ROWS),
                             {"owner": owner}).all()
            except crypto.CryptoError as exc:
                raise MigrationError(f"Rows of {table.name} don't open after sealing ({exc.__class__.__name__})") \
                    from None


# ---------------------------------------------------------------------------
# 5. Wiping the old pages
# ---------------------------------------------------------------------------

def vacuum() -> str:
    """Checkpoint the WAL, rebuild the file and checkpoint again, with secure_delete on, so the plaintext pages
    the run replaced are gone. Best effort: VACUUM needs free disk space about the size of the database."""
    if engine.dialect.name != "sqlite" or not DB_PATH.exists():
        return "skipped (not a SQLite file)"
    size = DB_PATH.stat().st_size
    free = shutil.disk_usage(DB_PATH.parent).free
    raw = engine.raw_connection()
    try:
        conn = raw.driver_connection
        conn.execute("PRAGMA secure_delete=ON")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        if free < 2 * size + 10 * 1024 * 1024:
            return f"skipped: the disk needs about {2 * size // (1024 * 1024) + 10} MB free to rebuild the database"
        try:
            conn.execute("VACUUM")
        except sqlite3.Error as exc:
            return f"failed: {exc.__class__.__name__}"
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return "done"
    finally:
        raw.close()


# ---------------------------------------------------------------------------
# 6. Plaintext copies outside the database
# ---------------------------------------------------------------------------

def leftover_files() -> list[dict]:
    """Files that hold plaintext from before encryption: database backups (``<db>.bak*``, this run's included), the
    old page cache (``data/cache/*.html``, unused for several versions) and uploads that aren't sealed (none is
    used: every document points at a sealed file). [{path (relative to the data folder), bytes, kind}]"""
    out = []

    def add(path: Path, kind: str) -> None:
        try:
            if path.is_file() and not path.is_symlink():
                rel = os.path.relpath(path, DATA_DIR) if path.is_relative_to(DATA_DIR) else str(path)
                out.append({"path": rel, "bytes": path.stat().st_size, "kind": kind, "_abs": path})
        except OSError:
            pass

    for path in sorted(DB_PATH.parent.glob(f"{DB_PATH.name}.bak*")):
        add(path, "backup")
    for path in sorted((DATA_DIR / "cache").glob("*.html")):
        add(path, "cache")
    if UPLOAD_DIR.is_dir():
        with engine.connect() as conn:
            used = {s for (s,) in conn.execute(text("SELECT stored_name FROM documents WHERE stored_name IS NOT NULL"))}
        for path in sorted(UPLOAD_DIR.iterdir()):
            if not path.name.startswith(".") and not path.name.endswith(SEALED_SUFFIX) and path.name not in used:
                add(path, "upload")
    return out


def _overwrite(path: Path) -> None:
    """Zeros over the file before it's deleted (helps on a hard disk; flash storage may keep the old blocks)."""
    size = path.stat().st_size
    with path.open("r+b") as f:
        chunk = b"\0" * (1024 * 1024)
        left = size
        while left > 0:
            f.write(chunk[:min(left, len(chunk))])
            left -= len(chunk)
        f.flush()
        os.fsync(f.fileno())


def delete_leftovers() -> list[str]:
    """Delete the files ``leftover_files`` lists now (never anything else). Returns their paths."""
    deleted = []
    for item in leftover_files():
        path = item["_abs"]
        try:
            _overwrite(path)
            path.unlink()
            deleted.append(item["path"])
        except OSError as exc:
            log.warning("Could not delete a leftover plaintext file (%s)", exc.__class__.__name__)
    if deleted:
        log.info("Deleted %d leftover plaintext file(s)", len(deleted))
    return deleted


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/api/encryption", tags=["encryption"])


def _public(item: dict) -> dict:
    return {k: v for k, v in item.items() if not k.startswith("_")}


@router.get("/status")
def encryption_status(_user: AuthUser = Depends(current_user)):
    """Signed in: the run's progress, its report (counts only) and the leftover plaintext files (paths and sizes)."""
    out = progress()
    value = state()
    out["report"] = value.get("report")
    out["finished_at"] = value.get("finished_at")
    out["leftovers"] = [_public(i) for i in leftover_files()] if out["state"] == "done" else []
    return out

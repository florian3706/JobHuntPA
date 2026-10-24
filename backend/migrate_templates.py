"""Letter templates from a development build, carried into the documents at the owner's sign-in.

A build of 2026-10-23 kept the cover letter template in a table of its own (``letter_templates``: the sealed file
name, the file type and the upload's stored name). Templates are documents of kind ``letter_template`` since the
next day (backend/docs.py). The old rows can't be moved at start-up: a sealed value is bound to its table and column
(``letter_templates.filename`` can't be read as ``documents.filename``), and only the owner's key opens it. So the
move happens when the owner signs in (backend/auth.py ``_after_sign_in``), under their key:

* each of their rows becomes a document (kind ``letter_template``, not for scoring, its text extracted from the
  file as an upload's is), the workspace's current template when it has none; the upload file stays as it is (its
  seal doesn't depend on the table);
* a row whose file is gone, or whose workspace no longer exists, is deleted (with its file, when there is one);
* rows of other accounts' workspaces wait for those accounts; once no row is left the table is dropped and the
  write-ahead log emptied (``db.scrub_wal``), as for every retired table.

Nothing here is logged but counts and the kind of failure.
"""
from __future__ import annotations

import logging
import os
import threading
from datetime import datetime
from typing import Optional

from sqlalchemy import inspect, text

from backend import crypto
from backend.db import LETTER_TEMPLATE_KIND, UPLOAD_DIR, Document, SessionLocal, Workspace, engine, scrub_wal

log = logging.getLogger(__name__)

TABLE = "letter_templates"
_gone = False  # the table is known to be absent: nothing to check at the next sign-in
_lock = threading.Lock()


def pending() -> bool:
    """Whether the old table is still there (a quick check once known gone)."""
    global _gone
    if _gone:
        return False
    if TABLE in inspect(engine).get_table_names():
        return True
    _gone = True
    return False


def run(user_id: int, dek: bytes) -> int:
    """Carries ``user_id``'s templates over, under their key; drops the table once empty. The number moved."""
    with crypto.as_user(user_id, dek), _lock:
        return _run(user_id)


def _when(value) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value)) if value else None
    except ValueError:
        return None


def _delete_file(stored_name: Optional[str]) -> None:
    if stored_name and os.path.basename(stored_name) == stored_name:
        try:
            (UPLOAD_DIR / stored_name).unlink(missing_ok=True)
        except OSError as exc:
            log.warning("Could not delete an old letter template's file: %s", exc.__class__.__name__)


def _run(user_id: int) -> int:
    from backend import docs

    if not pending():
        return 0
    sealed = crypto.SealedText(f"{TABLE}.filename")
    moved, dropped_rows, files = 0, 0, []
    db = SessionLocal()
    try:
        mine = {ws_id for (ws_id,) in db.query(Workspace.id).filter(Workspace.owner_id == user_id)}
        rows = db.execute(text(f"SELECT id, workspace_id, filename, filetype, stored_name, created_at FROM {TABLE} "
                               "ORDER BY id")).fetchall()
        for row in rows:
            if row.workspace_id not in mine:
                if db.query(Workspace.id).filter(Workspace.id == row.workspace_id).first() is None:
                    db.execute(text(f"DELETE FROM {TABLE} WHERE id = :id"), {"id": row.id})  # no workspace: nobody's
                    files.append(row.stored_name)
                    dropped_rows += 1
                continue  # another account's: waits for their sign-in
            try:
                filename = sealed.process_result_value(row.filename, None) or ""
            except crypto.CryptoError:
                filename = ""  # unreadable name: the file still matters
            filename = filename or f"Letter template{row.filetype or ''}"
            try:
                data = docs.read_upload(row.stored_name)
            except Exception:  # the file is gone or unreadable: the row points at nothing
                db.execute(text(f"DELETE FROM {TABLE} WHERE id = :id"), {"id": row.id})
                files.append(row.stored_name)
                dropped_rows += 1
                continue
            try:
                extracted = docs.extract_text(data, row.filetype or "")
            except Exception:
                extracted = ""
            when = _when(row.created_at) or datetime.utcnow()
            doc = Document(workspace_id=row.workspace_id, filename=filename, filetype=row.filetype,
                           stored_name=row.stored_name, text=extracted, kind=LETTER_TEMPLATE_KIND,
                           use_for_scoring=False, created_at=when, updated_at=when)
            db.add(doc)
            db.flush()
            ws = db.get(Workspace, row.workspace_id)
            if ws is not None and ws.letter_template_id is None:
                ws.letter_template_id = doc.id
            db.execute(text(f"DELETE FROM {TABLE} WHERE id = :id"), {"id": row.id})
            moved += 1
        left = db.execute(text(f"SELECT COUNT(*) FROM {TABLE}")).scalar()
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    for name in files:
        _delete_file(name)
    if left == 0:
        with engine.begin() as conn:
            if not conn.connection.driver_connection.in_transaction:
                conn.execute(text("BEGIN IMMEDIATE"))
            conn.execute(text(f"DROP TABLE IF EXISTS {TABLE}"))
        scrub_wal()
        pending()  # remembers that it is gone
    if moved or dropped_rows:
        log.info("Letter templates from the old table: %d carried into the documents, %d without a file or workspace "
                 "deleted, %d left for other accounts", moved, dropped_rows, left)
    return moved

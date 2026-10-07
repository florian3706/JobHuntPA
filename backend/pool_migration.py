"""One-time move of per-workspace job copies into the shared posting pool (docs/design/accounts.md §14.5).

Before the pool, every workspace stored its own copy of each ad in ``jobs``. This moves the public part of those
copies into ``postings`` (one row per URL) and leaves each ``jobs`` row as the workspace's link, keeping its id,
so fit results, cover letters, chats, duplicate decisions and job ids held by the browser all stay valid.

- A backup comes first (``<db>.bak-before-job-pool``, SQLite's online backup API), once.
- Rows are taken in batches of URLs, one transaction per batch; a row is migrated exactly when ``jobs.origin`` is
  set, so a run that stops halfway carries on at the next start, and a finished database is left alone.
- What a user added by hand stays theirs: a page imported from the browser (``source = 'import'``) stays a
  private job, and a SEEK ad with a full description (only an upload makes one: SEEK's job pages are off-limits
  to the scraper) keeps that description on the workspace's link while the posting gets the public fields.

Written with Core SQL, not the ORM: the model's content attributes are views over the posting, and a migration
must write exactly what it means to.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

from sqlalchemy import text

from backend.db import DB_PATH, DETAIL_RANK, JOB_CONTENT, encryption_done, engine

log = logging.getLogger(__name__)

STATE_KEY = "job_pool_migration"
BATCH_URLS = 200
NOT_A_JOB = "not a job posting page"  # backend.pipeline.NOT_A_JOB (import cycle)
# A posting's content columns, as copied from a job row.
POSTING_CONTENT = tuple(c for c in JOB_CONTENT)
# From a SEEK row whose full ad the user uploaded, the posting takes only what the public listing also says.
PUBLIC_FROM_UPLOAD = ("source", "external_id", "company", "title", "location_text", "country", "salary_min",
                      "salary_max", "salary_text", "posted_at", "industry")
FEED_SOURCES = ("workable:", "greenhouse:", "lever:", "ashby:", "smartrecruiters:", "workday:", "apsjobs:")


def backup_path() -> Path:
    return DB_PATH.parent / f"{DB_PATH.name}.bak-before-job-pool"


def _get_state(conn) -> Optional[str]:
    raw = conn.execute(text("SELECT value_json FROM app_settings WHERE key = :k"), {"k": STATE_KEY}).scalar()
    return json.loads(raw).get("state") if raw else None


def _set_state(conn, state: str, **extra) -> None:
    value = json.dumps({"state": state, "at": datetime.utcnow().isoformat(timespec="seconds"), **extra})
    conn.execute(text("INSERT INTO app_settings (key, value_json) VALUES (:k, :v) "
                      "ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json"), {"k": STATE_KEY, "v": value})


def _backup() -> Optional[Path]:
    """A copy of the database as it was, once (SQLite only)."""
    if engine.dialect.name != "sqlite" or not DB_PATH.exists():
        return None
    target = backup_path()
    if target.exists():
        return target
    tmp = target.with_name(target.name + ".tmp")
    raw = engine.raw_connection()
    try:
        dst = sqlite3.connect(str(tmp))
        try:
            raw.driver_connection.backup(dst)
        finally:
            dst.close()
    finally:
        raw.close()
    tmp.replace(target)
    return target


def _kind(row) -> str:
    """private | upload (pool link with a privately uploaded full ad) | public."""
    if not row.url or (row.source or "") == "import":
        return "private"
    if (row.source or "") == "seek" and (row.detail_status or "none") == "full":
        return "upload"
    return "public"


Sources = dict[int, tuple[str, Optional[str], str]]  # source id: (careers URL, label the user typed, fallback)


def load_sources(conn) -> Sources:
    """The workspaces' company sources, with the label the user typed (None when it's the default one)."""
    from backend.sources import default_label

    out = {}
    for i, url, label in conn.execute(text("SELECT id, careers_url, label FROM company_sources")):
        if not isinstance(url or "", str) or not isinstance(label or "", str):
            continue  # sealed already (a source added since): not one of the legacy rows' sources
        fallback = default_label(url or "")
        typed = (label or "").strip()
        out[i] = (url or "", typed if typed and typed != fallback else None, fallback)
    return out


def _labelled(row, sources: Sources) -> Optional[str]:
    """The fallback company name, when the row's company is the label the user typed for its source (the old
    pipeline used the label when an ad named no employer): that label stays the workspace's own."""
    _url, typed, fallback = sources.get(row.source_id, ("", None, ""))
    return fallback if typed and row.company == typed else None


def _listing_key(row, sources: Sources) -> Optional[str]:
    """The listing a legacy row's source stands for (backend.pipeline.listing_key), where it can be told."""
    source = row.source or ""
    if source == "atlassian" or source.startswith(FEED_SOURCES):
        return source
    if source.startswith("site:") and row.source_id in sources:
        return f"{source}|{sources[row.source_id][0]}"
    return None


def _when(value) -> Optional[datetime]:
    if value is None or isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


def _posting_values(rows: list, sources: Sources) -> dict:
    """The posting a group of copies (one URL, any workspaces) stands for: the best public copy's content."""
    from backend.pool import derive

    public = [r for r in rows if _kind(r) == "public"]
    best = max(public or rows, key=lambda r: (DETAIL_RANK.get(r.detail_status or "none", 0),
                                               _when(r.last_seen) or datetime.min))
    if public:
        values = {name: getattr(best, name) for name in POSTING_CONTENT}
        values["company"] = _labelled(best, sources) or values["company"]
    else:  # only uploads: the public fields, the description stays private
        values = {name: (getattr(best, name) if name in PUBLIC_FROM_UPLOAD else None) for name in POSTING_CONTENT}
        values["detail_status"] = "none"
    values["detail_status"] = values["detail_status"] or "none"
    target = SimpleNamespace(**values)
    derive(target)  # office days, work mode, hash: from the posting's own text
    values.update(country=target.country, office_days=target.office_days, work_mode=target.work_mode,
                  description_hash=target.description_hash)

    listed = [r for r in rows if _kind(r) != "private"]
    closed = [_when(r.closed_at) for r in listed]
    values.update(
        url=best.url,
        not_a_job=any(r.excluded_reason == NOT_A_JOB for r in public),
        first_seen=min((_when(r.first_seen) for r in listed if r.first_seen), default=datetime.utcnow()),
        last_listed_at=max((_when(r.last_seen) for r in listed if r.last_seen), default=datetime.utcnow()),
        closed_at=max(closed) if closed and all(closed) else None,
        listing_key=next((k for k in (_listing_key(r, sources) for r in listed) if k), None),
        rev=1,
    )
    values["close_reason"] = "unlisted" if values["closed_at"] else None
    # The office the public ad names, when an LLM read that ad (an "ad" office, or none found).
    read = [r for r in public if r.office_checked_at and (r.office_source or "") in ("ad", "company", "")]
    if read:
        r = max(read, key=lambda r: _when(r.office_checked_at))
        values["ad_office_text"] = r.office_text if r.office_source == "ad" else ""
        values["ad_office_checked_at"] = _when(r.office_checked_at)
    return values


def migrate_url(conn, url: Optional[str], sources: Sources) -> int:
    """Migrate every unmigrated row with this URL (any workspace). Returns how many rows."""
    if url is None:
        return conn.execute(text("UPDATE jobs SET origin = 'private' WHERE origin IS NULL AND url IS NULL")).rowcount
    rows = conn.execute(text("SELECT * FROM jobs WHERE origin IS NULL AND url = :u ORDER BY id"), {"u": url}).fetchall()
    if not rows:
        return 0
    linked = [r for r in rows if _kind(r) != "private"]
    for r in rows:
        if _kind(r) == "private":
            conn.execute(text("UPDATE jobs SET origin = 'private' WHERE id = :i"), {"i": r.id})
    if not linked:
        return len(rows)

    posting = conn.execute(text("SELECT * FROM postings WHERE url = :u"), {"u": url}).first()
    if posting is None:  # (a search since the upgrade may have stored it already: keep that one)
        values = {k: (v.strftime("%Y-%m-%d %H:%M:%S.%f") if isinstance(v, datetime) else v)  # SQLAlchemy's format
                  for k, v in _posting_values(rows, sources).items()}
        cols = ", ".join(values)
        conn.execute(text(f"INSERT INTO postings ({cols}) VALUES ({', '.join(':' + c for c in values)})"), values)
        posting = conn.execute(text("SELECT * FROM postings WHERE url = :u"), {"u": url}).first()

    for r in linked:
        own = {name: None for name in POSTING_CONTENT}
        own["detail_status"] = "none"  # = the posting's
        if _labelled(r, sources) and r.company != posting.company:
            own["company"] = r.company  # the label the user typed for the source
        if _kind(r) == "upload":  # the uploaded ad and what was read from it stay this workspace's
            for name in ("description", "description_hash", "detail_status", "detail_fetched_at", "office_days",
                         "work_mode", "employment_type"):
                value = getattr(r, name)
                if value is not None and value != getattr(posting, name):
                    own[name] = value
        sets = ", ".join(f"{name} = :{name}" for name in own)
        conn.execute(text(f"UPDATE jobs SET {sets}, closed_at = NULL, posting_id = :pid, origin = 'pool', "
                          f"posting_rev = :rev WHERE id = :id"),
                     {**own, "pid": posting.id, "rev": posting.rev, "id": r.id})
    return len(rows)


def migrate(batch_urls: int = BATCH_URLS) -> dict:
    """Run (or resume) the migration. Returns {"state", "rows"}; a no-op once done."""
    with engine.begin() as conn:
        state = _get_state(conn)
        if state == "done":
            return {"state": "done", "rows": 0}
        left = conn.execute(text("SELECT COUNT(*) FROM jobs WHERE origin IS NULL")).scalar()
        if not left:
            _set_state(conn, "done", rows=0)
            return {"state": "done", "rows": 0}
        if encryption_done(conn):
            # The encryption migration runs this one first, so this can't happen; if it did, those rows' content is
            # sealed and can't be read here: they stay the workspace's own (private) jobs.
            n = conn.execute(text("UPDATE jobs SET origin = 'private' WHERE origin IS NULL")).rowcount
            _set_state(conn, "done", rows=n)
            log.warning("job pool migration: %d jobs left after encryption were kept as private jobs", n)
            return {"state": "done", "rows": n}
    if state != "running":
        path = _backup()
        log.info("job pool migration: %d jobs; backup %s", left, "made" if path else "skipped")
    with engine.begin() as conn:
        _set_state(conn, "running")
        sources = load_sources(conn)
    done = 0
    while True:
        with engine.begin() as conn:
            urls = [u for (u,) in conn.execute(
                text("SELECT DISTINCT url FROM jobs WHERE origin IS NULL ORDER BY url LIMIT :n"), {"n": batch_urls})]
            if not urls:
                _set_state(conn, "done", rows=done)
                break
            for url in urls:
                done += migrate_url(conn, url, sources)
    log.info("job pool migration done: %d jobs", done)
    return {"state": "done", "rows": done}

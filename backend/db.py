"""SQLite storage via SQLAlchemy: every table the app uses lives here.

DB file: <project_root>/data/jobhunt.db (override with JOBHUNT_DB_PATH).
``init_db()`` creates missing tables and adds missing columns to existing
ones (SQLite ``ALTER TABLE ADD COLUMN``), so older databases keep working.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    create_engine,
    event,
    inspect,
    text,
)
from sqlalchemy.orm import declarative_base, sessionmaker

log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
UPLOAD_DIR = DATA_DIR / "uploads"
DB_PATH = Path(os.getenv("JOBHUNT_DB_PATH", DATA_DIR / "jobhunt.db"))
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{DB_PATH}")

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False, "timeout": 30},
)


@event.listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_conn, _record) -> None:
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA foreign_keys=ON")
    # WAL lets the UI read while a background search/scoring run writes.
    cur.execute("PRAGMA journal_mode=WAL")
    cur.close()


SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def utcnow() -> datetime:
    return datetime.utcnow()


# ---------------------------------------------------------------------------
# Jobs + fit scores
# ---------------------------------------------------------------------------

class Job(Base):
    __tablename__ = "jobs"

    id = Column(Integer, primary_key=True, index=True)
    source = Column(String, nullable=True)          # "seek", "workable:compass-education", ...
    source_id = Column(Integer, ForeignKey("company_sources.id", ondelete="SET NULL"), nullable=True)
    external_id = Column(String, nullable=True)
    company = Column(String, nullable=True)
    title = Column(String, nullable=True)
    location_text = Column(String, nullable=True)
    country = Column(String, nullable=True)         # ISO-2 when known ("AU")
    lat = Column(Float, nullable=True)
    lng = Column(Float, nullable=True)
    work_mode = Column(String, nullable=True)       # remote | hybrid | onsite | unknown
    salary_min = Column(Integer, nullable=True)
    salary_max = Column(Integer, nullable=True)
    salary_text = Column(String, nullable=True)
    url = Column(String, unique=True, nullable=True)
    description = Column(Text, nullable=True)
    description_hash = Column(String, nullable=True, index=True)
    # none: listing data only; summary: listing teaser only (e.g. SEEK);
    # full: detail page / API detail fetched. Full jobs are never re-fetched.
    detail_status = Column(String, nullable=False, default="none", server_default="none")
    detail_fetched_at = Column(DateTime, nullable=True)
    posted_at = Column(String, nullable=True)
    industry = Column(String, nullable=True)
    distance_km = Column(Float, nullable=True)
    status = Column(String, default="to_review", nullable=False)
    excluded_reason = Column(Text, nullable=True)
    first_seen = Column(DateTime, default=utcnow)
    last_seen = Column(DateTime, default=utcnow)
    closed_at = Column(DateTime, nullable=True)     # no longer listed by its source


class FitResult(Base):
    """Muse Spark fit score per job (written by backend.scorer)."""

    __tablename__ = "fit_results"

    id = Column(Integer, primary_key=True, index=True)
    job_id = Column(Integer, ForeignKey("jobs.id", ondelete="CASCADE"), unique=True, nullable=False, index=True)
    status = Column(String, nullable=False, default="ok", server_default="ok")  # ok | error
    score = Column(Integer, nullable=True)
    evidence_json = Column(Text, nullable=False, default="{}")
    error = Column(Text, nullable=True)
    profile_hash = Column(String, nullable=True)
    model = Column(String, nullable=True)
    created_at = Column(DateTime, default=utcnow)


# ---------------------------------------------------------------------------
# Documents + search profile
# ---------------------------------------------------------------------------

DOCUMENT_KINDS = ("resume", "cover_letter", "other")


class Document(Base):
    __tablename__ = "documents"

    id = Column(Integer, primary_key=True, index=True)
    filename = Column(String, nullable=False)
    filetype = Column(String, nullable=True)
    stored_name = Column(String, nullable=True)
    text = Column(Text, nullable=True)
    kind = Column(String, nullable=True)            # resume | cover_letter | other
    use_for_scoring = Column(Boolean, nullable=False, default=True, server_default="1")
    created_at = Column(DateTime, default=utcnow)


class SearchProfile(Base):
    """Legacy table (unused by the UI, kept so old DBs stay valid)."""

    __tablename__ = "search_profile"

    id = Column(Integer, primary_key=True, index=True)
    cv_text = Column(Text, nullable=True)
    wishes_text = Column(Text, nullable=True)
    home_location = Column(String, nullable=True)
    lat = Column(Float, nullable=True)
    lng = Column(Float, nullable=True)
    radius_km = Column(Float, nullable=True)
    work_modes = Column(String, nullable=True)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)


class UserProfile(Base):
    """Single-row table (id=1): search profile."""

    __tablename__ = "user_profile"

    id = Column(Integer, primary_key=True)
    titles = Column(JSON, default=list, nullable=False)
    keywords_include = Column(JSON, default=list, nullable=False)
    keywords_exclude = Column(JSON, default=list, nullable=False)
    salary_floor = Column(Integer, nullable=True)
    remote_aus_ok = Column(Boolean, default=True, nullable=False)
    remote_global_ok = Column(Boolean, default=False, nullable=False)
    allow_hybrid = Column(Boolean, nullable=False, default=True, server_default="1")
    allow_onsite = Column(Boolean, nullable=False, default=True, server_default="1")
    seek_enabled = Column(Boolean, nullable=False, default=True, server_default="1")
    seek_locations = Column(JSON, nullable=True)
    seek_max_pages = Column(Integer, nullable=False, default=1, server_default="1")  # unused (SEEK pagination is off-limits)


class DealbreakerSet(Base):
    """Single-row table (id=1): exclusion tag arrays."""

    __tablename__ = "dealbreakers"

    id = Column(Integer, primary_key=True)
    industries = Column(JSON, default=list, nullable=False)
    keywords = Column(JSON, default=list, nullable=False)


class Pin(Base):
    """Location pins: home base + acceptable hybrid/onsite workplaces."""

    __tablename__ = "pins"

    id = Column(Integer, primary_key=True, index=True)
    label = Column(String, nullable=False)
    kind = Column(String, nullable=False)  # home | hybrid | onsite
    lat = Column(Float, nullable=False)
    lng = Column(Float, nullable=False)
    radius_km = Column(Float, default=25.0, nullable=False)


# ---------------------------------------------------------------------------
# Company sources
# ---------------------------------------------------------------------------

class CompanySource(Base):
    """One company's careers page / job board, added by URL in the UI."""

    __tablename__ = "company_sources"

    id = Column(Integer, primary_key=True, index=True)
    label = Column(String, nullable=True, default="")
    careers_url = Column(String, unique=True, nullable=True, default="")
    source_type = Column(String, nullable=True, default="generic")
    adapter_key = Column(String, nullable=True, default="")
    enabled = Column(Boolean, nullable=False, default=True)
    last_run = Column(DateTime, nullable=True)
    last_count = Column(Integer, nullable=True, default=0)
    last_error = Column(Text, nullable=True, default=None)
    last_method = Column(Text, nullable=True)
    # Legacy columns from an earlier schema; unused but kept for old DBs.
    company = Column(String, nullable=True, default="")
    url = Column(String, nullable=True, default="")
    board = Column(String, nullable=True, default="")
    host = Column(String, nullable=True, default="")
    tenant = Column(String, nullable=True, default="")
    site = Column(String, nullable=True, default="")
    base_url = Column(String, nullable=True, default="")


# ---------------------------------------------------------------------------
# Background runs + caches
# ---------------------------------------------------------------------------

class SearchRun(Base):
    """One background search or scoring run, polled by the UI."""

    __tablename__ = "search_runs"

    id = Column(Integer, primary_key=True, index=True)
    kind = Column(String, nullable=False, default="search")  # search | score
    state = Column(String, nullable=False, default="queued")  # queued | running | done | failed
    stage = Column(String, nullable=True)
    progress_json = Column(Text, nullable=False, default="{}")
    summary_json = Column(Text, nullable=True)
    error = Column(Text, nullable=True)
    started_at = Column(DateTime, default=utcnow)
    finished_at = Column(DateTime, nullable=True)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "state": self.state,
            "stage": self.stage,
            "progress": json.loads(self.progress_json or "{}"),
            "summary": json.loads(self.summary_json) if self.summary_json else None,
            "error": self.error,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
        }


class HttpCache(Base):
    """Short-lived cache of listing pages / API responses / robots.txt."""

    __tablename__ = "http_cache"

    url = Column(String, primary_key=True)
    status_code = Column(Integer, nullable=False)
    body = Column(Text, nullable=False, default="")
    fetched_at = Column(DateTime, nullable=False, default=utcnow)


class GeocodeCache(Base):
    """Nominatim results, cached forever (their usage policy requires it)."""

    __tablename__ = "geocode_cache"

    query = Column(String, primary_key=True)
    lat = Column(Float, nullable=True)
    lng = Column(Float, nullable=True)
    country = Column(String, nullable=True)
    display_name = Column(String, nullable=True)
    fetched_at = Column(DateTime, nullable=False, default=utcnow)


# ---------------------------------------------------------------------------
# Session + schema management
# ---------------------------------------------------------------------------

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _add_missing_columns() -> None:
    """ALTER TABLE ADD COLUMN for model columns missing from an older DB."""
    insp = inspect(engine)
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if not insp.has_table(table.name):
                continue
            existing = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in existing:
                    continue
                ddl = f"ALTER TABLE {table.name} ADD COLUMN {col.name} {col.type.compile(engine.dialect)}"
                if col.server_default is not None:
                    ddl += f" DEFAULT '{col.server_default.arg}'"
                conn.execute(text(ddl))
                log.info("migrated: added %s.%s", table.name, col.name)


def _rebuild_legacy_fit_results() -> None:
    """The old fit_results had score NOT NULL and stored API failures as 0.

    SQLite can't relax a NOT NULL constraint, so move the old table aside,
    let create_all() build the new one, and copy rows over (failures become
    status='error' rows without a score).
    """
    insp = inspect(engine)
    if not insp.has_table("fit_results"):
        return
    if "status" in {c["name"] for c in insp.get_columns("fit_results")}:
        return
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE fit_results RENAME TO fit_results_legacy"))
        # Renaming keeps the old index names, which the new table needs.
        for (name,) in conn.execute(text(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='fit_results_legacy' "
            "AND sql IS NOT NULL"
        )).fetchall():
            conn.execute(text(f'DROP INDEX "{name}"'))
    Base.metadata.tables["fit_results"].create(bind=engine)
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO fit_results (job_id, status, score, evidence_json, error, created_at) "
            "SELECT job_id, "
            "  CASE WHEN json_extract(evidence_json, '$.summary') LIKE 'ERROR:%' "
            "       OR json_extract(evidence_json, '$.summary') LIKE 'NO_KEY%' THEN 'error' ELSE 'ok' END, "
            "  CASE WHEN json_extract(evidence_json, '$.summary') LIKE 'ERROR:%' "
            "       OR json_extract(evidence_json, '$.summary') LIKE 'NO_KEY%' THEN NULL ELSE score END, "
            "  evidence_json, "
            "  CASE WHEN json_extract(evidence_json, '$.summary') LIKE 'ERROR:%' "
            "       THEN substr(json_extract(evidence_json, '$.summary'), 1, 2000) END, "
            "  created_at "
            "FROM fit_results_legacy WHERE job_id IN (SELECT id FROM jobs)"
        ))
        conn.execute(text("DROP TABLE fit_results_legacy"))
    log.info("migrated fit_results to the new schema")


def _fix_legacy_data() -> None:
    """One-off data repairs for databases written by the previous code."""
    with engine.begin() as conn:
        conn.execute(text("UPDATE documents SET kind='resume' WHERE kind IS NULL OR kind='cv'"))
        # Jobs from the old generic scraper were not linked to their source.
        conn.execute(text(
            "UPDATE jobs SET source_id = (SELECT id FROM company_sources cs "
            "WHERE 'generic:' || cs.label = jobs.source) "
            "WHERE source_id IS NULL AND source LIKE 'generic:%'"
        ))


def init_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    _rebuild_legacy_fit_results()
    Base.metadata.create_all(bind=engine)
    _add_missing_columns()
    _fix_legacy_data()

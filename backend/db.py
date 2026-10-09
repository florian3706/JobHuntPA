"""SQLite storage via SQLAlchemy: every table the app uses lives here.

DB file: <project_root>/data/jobhunt.db (override with JOBHUNT_DB_PATH, or the whole data folder with
JOBHUNT_DATA_DIR).
``init_db()`` creates missing tables and adds missing columns to existing
ones (SQLite ``ALTER TABLE ADD COLUMN``), so older databases keep working.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import stat
from datetime import datetime
from pathlib import Path
from typing import Optional

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    event,
    inspect,
    text,
)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker, validates
from sqlalchemy.schema import CreateTable

from backend import crypto
from backend.crypto import (
    ProcessSealedText,
    SealedAlways,
    SealedBool,
    SealedDateTime,
    SealedFloat,
    SealedInt,
    SealedJSON,
    SealedText,
    always_none,
)

log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
# The data folder: data/ in the app folder. JOBHUNT_DATA_DIR moves it (tests and tools point it at a throwaway
# folder, so they never touch the real uploads); JOBHUNT_DB_PATH moves just the database.
DATA_DIR = Path(os.getenv("JOBHUNT_DATA_DIR") or PROJECT_ROOT / "data")
UPLOAD_DIR = DATA_DIR / "uploads"
DB_PATH = Path(os.getenv("JOBHUNT_DB_PATH") or DATA_DIR / "jobhunt.db")
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{DB_PATH}")

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False, "timeout": 30},
    # A failed statement's error message never carries its bound values: they can be personal data, and those
    # messages end up in logs (accounts design §15). Never set echo=True for the same reason.
    hide_parameters=True,
)


def owner_only(path: Path, mode: int) -> None:
    """Makes ``path`` readable by this account only (``mode`` is 0o700 for a folder, 0o600 for a file), when it
    isn't already. POSIX only: Windows keeps what the folder's permissions say. Never fails."""
    if os.name != "posix":
        return
    try:
        if stat.S_IMODE(os.stat(path).st_mode) != mode:
            os.chmod(path, mode)
    except OSError:  # missing, or not ours to change
        pass


def _protect_database_files() -> None:
    """The database and its write-ahead log files hold wrapped keys and every plaintext column (accounts design §2.5):
    other accounts on the same computer must not read them. SQLite makes them with the umask (usually 0644)."""
    if engine.dialect.name != "sqlite":
        return
    for suffix in ("", "-wal", "-shm"):
        owner_only(DB_PATH.with_name(DB_PATH.name + suffix), 0o600)


@event.listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_conn, _record) -> None:
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA foreign_keys=ON")
    # WAL lets the UI read while a background search/scoring run writes.
    cur.execute("PRAGMA journal_mode=WAL")
    # Freed pages are zeroed, so a deleted or re-sealed value doesn't linger in the file (accounts design §5).
    cur.execute("PRAGMA secure_delete=ON")
    # The log file is cut to nothing each time it is reset, so what a blocked scrub_wal() couldn't empty goes with the
    # next write once the reader that blocked it is done (without a limit SQLite keeps the old frames on disk).
    cur.execute("PRAGMA journal_size_limit=0")
    cur.close()
    _protect_database_files()


SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def online_backup(target: Path) -> Path:
    """A copy of the database as it is, at ``target`` (SQLite's online backup API, safe while it's in use). The copy
    holds everything in plaintext, so it is created owner-only before anything is written to it, and moved into place
    only when complete."""
    tmp = target.with_name(target.name + ".tmp")
    tmp.unlink(missing_ok=True)
    os.close(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
    raw = engine.raw_connection()
    try:
        dst = sqlite3.connect(str(tmp))
        try:
            raw.driver_connection.backup(dst)
        finally:
            dst.close()
    finally:
        raw.close()
    owner_only(tmp, 0o600)
    tmp.replace(target)
    return target


BUSY_TIMEOUT_MS = 30_000  # (connect_args["timeout"] above)
WAL_WAIT_MS = 2_000
_wal_blocked = False


def scrub_wal() -> bool:
    """After deleting a secret (a key copy), empty the write-ahead log: secure_delete only zeroes the main file, and the
    log keeps the old page image until a checkpoint truncates it. Best effort: a reader open at the same moment blocks
    it. Nothing empties the log by itself then, so a blocked scrub is remembered and ``scrub_wal_again`` (start-up and
    the clean-up tick) tries again; ``journal_size_limit`` also cuts the file at the next log reset. Call after the
    commit. True if it was emptied."""
    global _wal_blocked
    try:
        with engine.connect() as conn:
            # Waits a moment for a reader to finish (a request must not stall for the connection's usual 30 s: the
            # clean-up tick tries again), then puts the usual wait back for whoever gets this connection next.
            conn.exec_driver_sql(f"PRAGMA busy_timeout={WAL_WAIT_MS}")
            try:
                row = conn.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            finally:
                conn.exec_driver_sql(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        done = bool(row) and row[0] == 0  # row[0] is 1 when the checkpoint was blocked
        if not done:
            logging.getLogger(__name__).warning("wal checkpoint after deleting a secret was blocked")
    except Exception as exc:  # never fails the change that was already committed
        logging.getLogger(__name__).warning("wal checkpoint after deleting a secret failed: %s", exc.__class__.__name__)
        done = False
    _wal_blocked = not done
    return done


def scrub_wal_again() -> bool:
    """Empties the log again when the last ``scrub_wal`` was blocked (a no-op otherwise). True if one was due."""
    if not _wal_blocked:
        return False
    scrub_wal()
    return True


Base = declarative_base()


def utcnow() -> datetime:
    return datetime.utcnow()


# ---------------------------------------------------------------------------
# Jobs + fit scores
# ---------------------------------------------------------------------------

class Workspace(Base):
    """A separate job search: its own settings, sources, documents, pins and jobs. Each belongs to one account
    (``owner_id``); a workspace from before accounts has none until the first admin claims it (backend.users)."""

    __tablename__ = "workspaces"
    # An id is never given out twice: a request that resolved a workspace before it was deleted (a password reset that
    # wiped the data, an account deletion) must not write into the next account's new one.
    # (_rebuild_workspaces_for_autoincrement does this for a database made without it.)
    __table_args__ = {"sqlite_autoincrement": True}

    id = Column(Integer, primary_key=True, index=True)
    name = Column(SealedText(), nullable=False)
    created_at = Column(DateTime, default=utcnow)
    # Who it belongs to: backend.workspaces.current_workspace answers 404 for anyone else's. Deleting an account
    # deletes its workspaces. (Databases from before accounts get this column without the foreign key, which
    # SQLite can't add later: delete an account's workspaces explicitly, don't rely on the cascade.)
    owner_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True)


def _workspace_fk():
    """A row's workspace. No default: a row written without one is a bug, refused before it reaches the database
    (``_refuse_rows_without_workspace``). Databases made before accounts still carry DEFAULT '1' in their schema,
    which SQLite can't drop, and would otherwise put such a row in the first admin's workspace."""
    return Column(Integer, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True)


DETAIL_RANK = {"none": 0, "summary": 1, "full": 2}


class Posting(Base):
    """One job ad in the shared pool (docs/design/accounts.md §14): what its source publishes, scraped once and
    used by every workspace. Never holds anything a user added or decided: those live on the workspace's ``Job``
    (the link). Written only by ``backend.pool``."""

    __tablename__ = "postings"

    id = Column(Integer, primary_key=True, index=True)
    url = Column(String, nullable=False, unique=True)   # identity: the URL as the adapter produces it
    source = Column(String, nullable=True)              # "seek", "workable:compass-education", "site:acme.com" ...
    external_id = Column(String, nullable=True)
    company = Column(String, nullable=True)
    title = Column(String, nullable=True)
    location_text = Column(String, nullable=True)
    country = Column(String, nullable=True)             # ISO-2 when known ("AU")
    work_mode = Column(String, nullable=True)           # remote | hybrid | onsite | unknown
    employment_type = Column(String, nullable=True)     # backend.employment.EMPLOYMENT_TYPES, None = unknown
    office_days = Column(Integer, nullable=True)        # days/week in the office, when the ad says
    salary_min = Column(Integer, nullable=True)
    salary_max = Column(Integer, nullable=True)
    salary_text = Column(String, nullable=True)
    description = Column(Text, nullable=True)           # as scraped
    description_hash = Column(String, nullable=True, index=True)
    # none: listing data only; summary: listing teaser only (e.g. SEEK); full: detail page / API detail fetched.
    detail_status = Column(String, nullable=False, default="none", server_default="none")
    detail_fetched_at = Column(DateTime, nullable=True)
    posted_at = Column(String, nullable=True)
    industry = Column(String, nullable=True)
    not_a_job = Column(Boolean, nullable=False, default=False, server_default="0")  # its page wasn't a job ad
    # The office the public ad names (read by an LLM from this description): "" = it names none.
    ad_office_text = Column(String, nullable=True)
    ad_office_checked_at = Column(DateTime, nullable=True)
    listing_key = Column(String, nullable=True)         # the listing that last listed it (backend.pool closing rule)
    first_seen = Column(DateTime, default=utcnow)       # first stored in the pool
    last_listed_at = Column(DateTime, default=utcnow, index=True)  # last time any listing showed it
    closed_at = Column(DateTime, nullable=True, index=True)
    close_reason = Column(String, nullable=True)        # gone | unlisted | stale
    rev = Column(Integer, nullable=False, default=1, server_default="1")  # bumped when the content changes
    # Ghost-job signs (backend/ghost.py, docs/design/ghost-jobs.md). All derived from public ads, so plaintext.
    listed_since = Column(DateTime, nullable=True)      # start of the current run of listing (reset when it reopens)
    repost_key = Column(String, nullable=True, index=True)  # normalised company|title: finds earlier ads of the role
    ghost_json = Column(Text, nullable=True)            # {"text": [...signals from the ad], "pool": [...from the pool]}
    ghost_checked_at = Column(DateTime, nullable=True)  # when the pool half was last worked out
    ad_ghost_json = Column(Text, nullable=True)         # JSON list: what an LLM read in the public ad (NULL: not read)
    reopened_from = Column(String, nullable=True)       # close_reason of the run before this one (set when it reopens)


# A job's content: the posting's for a pool link (with this workspace's own value, when it has one, on top), or
# all on the job for a private job. ``Job.<name>`` reads the effective value; assigning writes the workspace's own.
JOB_CONTENT = ("source", "external_id", "company", "title", "location_text", "country", "work_mode", "employment_type",
               "office_days", "salary_min", "salary_max", "salary_text", "description", "description_hash",
               "detail_status", "detail_fetched_at", "posted_at", "industry")


class _NoSql:
    """What ``Job.<python-only property>`` is on the class: any use in SQL or a comparison raises. A plain property
    would compare as a Python object, and ``filter(Job.url == x)`` would silently become ``filter(False)``."""

    __slots__ = ("name",)

    def __init__(self, name: str):
        self.name = name

    def _refuse(self, *_args, **_kwargs):
        raise TypeError(f"Job.{self.name} is a Python property with no SQL form (it reads the posting's value, or "
                        "the workspace's own, which is sealed). Decide in Python, or use Job.url_index.")

    __eq__ = __ne__ = __lt__ = __le__ = __gt__ = __ge__ = __clause_element__ = __bool__ = _refuse
    __hash__ = None
    in_ = is_ = is_not = like = ilike = desc = asc = _refuse


class python_only(property):
    """A property of ``Job`` without an SQL form (see _NoSql)."""

    def __get__(self, obj, objtype=None):
        if obj is None:
            return _NoSql(self.fget.__name__)
        return super().__get__(obj, objtype)


def _content(name: str) -> property:
    own = "_" + name
    inherit = "none" if name == "detail_status" else None  # the own value that means "the posting's"

    def get(self):
        value = getattr(self, own)
        if value is not None and value != inherit:
            return value
        posting = self.posting
        return getattr(posting, name) if posting is not None else inherit

    def set(self, value):
        posting = self.posting
        if posting is not None and value == getattr(posting, name):
            value = inherit  # the same as the shared ad: no override
        setattr(self, own, inherit if value is None else value)

    get.__name__ = name
    return python_only(get, set, doc=f"Effective {name}: this workspace's own value, else the posting's.")


class Job(Base):
    """A job in a workspace: the link from the workspace to a shared ``Posting`` plus everything personal about
    it (status, hidden, exclusion, placement, office, verdict, and own content such as a privately uploaded full
    ad). A private job (``posting_id`` NULL: imported from a saved page) keeps all its content here.

    The content attributes (``title``, ``description``, ... see JOB_CONTENT) are Python properties without an SQL
    form: filter on them in Python. Their own values are the ``_title``... columns (named ``title``... in the
    table, where they were before the pool), NULL meaning "the posting's".

    Sealed (accounts design §2.2 and §14.6): everything personal, under the owner's key. Always sealed, so not even
    whether they're set shows: hidden, excluded_reason, the verdict, the office override and the own content.
    Plaintext: the link itself (workspace, posting, origin, source, duplicate_of, first_seen, posting_rev) and
    ``url_index``, the blind index that finds a job by URL (``backend.pool.job_by_url``).
    """

    __tablename__ = "jobs"
    __table_args__ = (UniqueConstraint("workspace_id", "url", name="uq_jobs_workspace_url"),
                      Index("uq_jobs_workspace_url_index", "workspace_id", "url_index", unique=True))

    id = Column(Integer, primary_key=True, index=True)
    workspace_id = _workspace_fk()
    # A private job's own URL (sealed). A pool link has none: its URL is its posting's (``Job.url``).
    _url = Column("url", SealedText(), nullable=True)
    url_index = Column(LargeBinary, nullable=True)  # crypto.blind_index("url", job.url), set on every write
    posting_id = Column(Integer, ForeignKey("postings.id", ondelete="SET NULL"), nullable=True, index=True)
    posting = relationship(Posting, lazy="joined")
    origin = Column(String, nullable=True)          # pool | private; NULL: not migrated yet (backend.pool_migration)
    posting_rev = Column(Integer, nullable=True)    # the posting's rev this link was last refreshed against
    source_id = Column(Integer, ForeignKey("company_sources.id", ondelete="SET NULL"), nullable=True)
    lat = Column(SealedFloat(), nullable=True)      # placed for this workspace (nearest place to its home)
    lng = Column(SealedFloat(), nullable=True)
    distance_km = Column(SealedFloat(), nullable=True)
    # to_review | shortlisted | applied | interviewing | rejected | not_interested
    status = Column(SealedText(), default="to_review", nullable=False)
    # Never filter on these in SQL (the comparator refuses even IS NULL): decide in Python.
    excluded_reason = Column(SealedAlways(SealedText, pad=256), default=always_none, nullable=True)
    hidden = Column(SealedAlways(SealedBool), nullable=False, default=False)  # hidden by the user
    first_seen = Column(DateTime, default=utcnow)   # first linked to this workspace
    last_seen = Column(SealedDateTime(), default=utcnow)  # last listed by this workspace's own search
    # Set on a copy of an ad that is merged into another job (backend.duplicates):
    # the copy keeps its row so later searches recognise its URL, but only the
    # job it points to is listed, scored and researched.
    duplicate_of = Column(Integer, ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True, index=True)
    # The search run during which this workspace first got the job (NULL for a link made outside a run, or before
    # this column): "new since the last digest" (backend.digest). Plain, not a foreign key: a run is never deleted
    # alone, only with its workspace.
    first_run_id = Column(Integer, nullable=True)
    # Where the office actually is, when the ad's location is only a city
    # (backend.offices). Its coordinates replace the listing's for the
    # location filter and distance.
    office_text = Column(SealedAlways(SealedText, pad=256), default=always_none, nullable=True)  # address or suburb
    office_lat = Column(SealedAlways(SealedFloat), default=always_none, nullable=True)
    office_lng = Column(SealedAlways(SealedFloat), default=always_none, nullable=True)
    office_source = Column(SealedAlways(SealedText), default=always_none, nullable=True)  # ad | company | user
    # ad read for an office (found or not)
    office_checked_at = Column(SealedAlways(SealedDateTime), default=always_none, nullable=True)
    # The user's own call on the location: ok (passes the location filter) | too_far
    location_verdict = Column(SealedAlways(SealedText), default=always_none, nullable=True)

    # Own content (see JOB_CONTENT): overrides of the posting, or a private job's content.
    _source = Column("source", SealedAlways(SealedText), default=always_none, nullable=True)
    _external_id = Column("external_id", SealedAlways(SealedText), default=always_none, nullable=True)
    _company = Column("company", SealedAlways(SealedText), default=always_none, nullable=True)
    _title = Column("title", SealedAlways(SealedText), default=always_none, nullable=True)
    _location_text = Column("location_text", SealedAlways(SealedText), default=always_none, nullable=True)
    _country = Column("country", SealedAlways(SealedText), default=always_none, nullable=True)
    _work_mode = Column("work_mode", SealedAlways(SealedText), default=always_none, nullable=True)
    _employment_type = Column("employment_type", SealedAlways(SealedText), default=always_none, nullable=True)
    _office_days = Column("office_days", SealedAlways(SealedInt), default=always_none, nullable=True)
    _salary_min = Column("salary_min", SealedAlways(SealedInt), default=always_none, nullable=True)
    _salary_max = Column("salary_max", SealedAlways(SealedInt), default=always_none, nullable=True)
    _salary_text = Column("salary_text", SealedAlways(SealedText), default=always_none, nullable=True)
    _description = Column("description", SealedAlways(SealedText), default=always_none, nullable=True)
    _description_hash = Column("description_hash", SealedAlways(SealedText), default=always_none, nullable=True)
    _detail_status = Column("detail_status", SealedAlways(SealedText), nullable=False, default="none")
    _detail_fetched_at = Column("detail_fetched_at", SealedAlways(SealedDateTime), default=always_none, nullable=True)
    _posted_at = Column("posted_at", SealedAlways(SealedText), default=always_none, nullable=True)
    _industry = Column("industry", SealedAlways(SealedText), default=always_none, nullable=True)
    # a private job's; a pool link's is the posting's
    _closed_at = Column("closed_at", SealedAlways(SealedDateTime), default=always_none, nullable=True)
    # What an LLM read in this workspace's own ad (a private upload or private job) that suggests it isn't a current
    # vacancy: a JSON list. Not SealedAlways: a column added to a database that is already in strict mode is NULL
    # in every old row. The public ad's is ``Posting.ad_ghost_json`` (backend.pool.share_ad_fact).
    ad_ghost_json = Column(SealedText(), nullable=True)

    @python_only
    def url(self) -> Optional[str]:
        """The posting's URL for a pool link, else the private job's own."""
        return self.posting.url if self.posting is not None else self._url

    @url.setter
    def url(self, value: Optional[str]) -> None:
        self._url = value  # read only while the job has no posting (backend.pool keeps it empty on pool links)

    source = _content("source")
    external_id = _content("external_id")
    company = _content("company")
    title = _content("title")
    location_text = _content("location_text")
    country = _content("country")
    work_mode = _content("work_mode")
    employment_type = _content("employment_type")
    office_days = _content("office_days")
    salary_min = _content("salary_min")
    salary_max = _content("salary_max")
    salary_text = _content("salary_text")
    description = _content("description")
    description_hash = _content("description_hash")
    detail_status = _content("detail_status")
    detail_fetched_at = _content("detail_fetched_at")
    posted_at = _content("posted_at")
    industry = _content("industry")

    @property
    def closed_at(self):
        """When the ad closed: the posting's (backend.pool closing rule), or a private job's own."""
        return self.posting.closed_at if self.posting is not None else self._closed_at

    @closed_at.setter
    def closed_at(self, value):
        self._closed_at = value

    @property
    def has_own_ad(self) -> bool:
        """This workspace reads its own description (a privately uploaded full ad, or a private job)."""
        return self.posting is None or self._description is not None

    @validates("excluded_reason")
    def _no_empty_reason(self, _key, value):
        """"Not excluded" is NULL, never ""."""
        return value or None



class JobDuplicate(Base):
    """A decision about two jobs that may be the same ad (backend.duplicates).

    status: suggested (likely duplicates, for the user to check) | merged |
    distinct (the user said they are different jobs: never suggested again).
    decided_by: auto (merged by the scan) | user. job_a < job_b.
    """

    __tablename__ = "job_duplicates"
    __table_args__ = (UniqueConstraint("job_a", "job_b", name="uq_job_duplicates_pair"),)

    id = Column(Integer, primary_key=True, index=True)
    workspace_id = _workspace_fk()
    job_a = Column(Integer, ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True)
    job_b = Column(Integer, ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True)
    status = Column(String, nullable=False, default="suggested")
    decided_by = Column(String, nullable=True)
    score = Column(Integer, nullable=True)   # a similarity number, plaintext (accounts design §2.2)
    reasons_json = Column(SealedText(), nullable=False, default="[]")
    created_at = Column(DateTime, default=utcnow)
    decided_at = Column(DateTime, nullable=True)


class FitResult(Base):
    """LLM fit score per job (written by backend.scorer). Score, evidence, status and error are sealed: decide on
    them in Python."""

    __tablename__ = "fit_results"

    id = Column(Integer, primary_key=True, index=True)
    job_id = Column(Integer, ForeignKey("jobs.id", ondelete="CASCADE"), unique=True, nullable=False, index=True)
    status = Column(SealedText(), nullable=False, default="ok")  # ok | error
    score = Column(SealedInt(), nullable=True)
    evidence_json = Column(SealedText(), nullable=False, default="{}")
    error = Column(SealedText(), nullable=True)
    profile_hash = Column(String, nullable=True)
    model = Column(String, nullable=True)
    created_at = Column(DateTime, default=utcnow)


# ---------------------------------------------------------------------------
# Documents + search profile
# ---------------------------------------------------------------------------

# preferences: what the candidate wants from a job (salary, place, kind of work...). Sent to the scorer,
# the chat and title suggestions, but never counted as experience and never sent for a cover letter.
DOCUMENT_KINDS = ("resume", "cover_letter", "preferences", "other")

# Documents without a file (``stored_name`` is empty), told apart by ``filetype``:
NOTE_FILETYPE = "note"          # written in the app: title in ``filename``, body in ``text``
SUMMARY_FILETYPE = "summary"    # the LLM's consolidation of several documents (backend.docs)


class Document(Base):
    __tablename__ = "documents"

    id = Column(Integer, primary_key=True, index=True)
    workspace_id = _workspace_fk()
    filename = Column(SealedText(), nullable=False)  # the file's name, or a note's / summary's title
    filetype = Column(String, nullable=True)        # ".pdf", ".docx", ... | "note" | "summary"
    # The upload in data/uploads: "<random hex>.sealed", the file sealed under the owner's key (backend.docs).
    stored_name = Column(String, nullable=True)
    text = Column(SealedText(), nullable=True)      # extracted once from the file, or the note / summary
    kind = Column(String, nullable=True)            # one of DOCUMENT_KINDS
    use_for_scoring = Column(Boolean, nullable=False, default=True, server_default="1")
    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, nullable=True)    # a note, summary or concise version last edited
    # A shorter rewrite of ``text`` (LLM-written, then editable). ``use_concise`` picks which of the two
    # the LLM is sent (text_for_llm).
    concise_text = Column(SealedText(), nullable=True)
    use_concise = Column(Boolean, nullable=False, default=False, server_default="0")
    concise_model = Column(String, nullable=True)   # model that wrote the concise version
    concise_at = Column(DateTime, nullable=True)
    summary_of = Column(JSON, nullable=True)        # a summary's source document ids

    def text_for_llm(self) -> str:
        """The version of this document the LLM gets: the concise one when chosen (and not empty), else the
        original."""
        concise = (self.concise_text or "").strip()
        return concise if self.use_concise and concise else (self.text or "").strip()


class SearchProfile(Base):
    """Legacy table (unused by the UI, kept so old DBs stay valid). It may hold an old CV, so it is sealed under
    the key of the first admin, who owns all data from before accounts."""

    __tablename__ = "search_profile"

    id = Column(Integer, primary_key=True, index=True)
    cv_text = Column(SealedText(), nullable=True)
    wishes_text = Column(SealedText(), nullable=True)
    home_location = Column(SealedText(), nullable=True)
    lat = Column(SealedFloat(), nullable=True)
    lng = Column(SealedFloat(), nullable=True)
    radius_km = Column(SealedFloat(), nullable=True)
    work_modes = Column(SealedText(), nullable=True)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)


class UserProfile(Base):
    """One row per workspace (id = workspace id): search profile. The five work-mode and SEEK switches stay
    plaintext; the words, places, salary and commute are sealed."""

    __tablename__ = "user_profile"

    id = Column(Integer, primary_key=True)
    titles = Column(SealedJSON(), default=list, nullable=False)
    keywords_include = Column(SealedJSON(), default=list, nullable=False)
    keywords_exclude = Column(SealedJSON(), default=list, nullable=False)
    salary_floor = Column(SealedInt(), nullable=True)
    remote_aus_ok = Column(Boolean, default=True, nullable=False)
    remote_global_ok = Column(Boolean, default=False, nullable=False)
    allow_hybrid = Column(Boolean, nullable=False, default=True, server_default="1")
    max_office_days = Column(SealedInt(), nullable=True)  # hybrid: most office days/week accepted
    allow_onsite = Column(Boolean, nullable=False, default=True, server_default="1")
    seek_enabled = Column(Boolean, nullable=False, default=True, server_default="1")
    seek_locations = Column(SealedJSON(), nullable=True)
    seek_max_pages = Column(Integer, nullable=False, default=1, server_default="1")  # unused (SEEK pagination is off-limits)
    # Commute (backend.commute): public transport starts at these stations
    # (empty = the home pin); peak trips arrive by / leave at these times.
    commute_from = Column(SealedJSON(), nullable=True)
    commute_arrive_by = Column(SealedText(), nullable=False, default="09:00")
    commute_leave_at = Column(SealedText(), nullable=False, default="17:00")


class DealbreakerSet(Base):
    """One row per workspace (id = workspace id): exclusion tag arrays."""

    __tablename__ = "dealbreakers"

    id = Column(Integer, primary_key=True)
    industries = Column(SealedJSON(), default=list, nullable=False)
    keywords = Column(SealedJSON(), default=list, nullable=False)


class Pin(Base):
    """Location pins: home base + acceptable hybrid/onsite workplaces. Only the kind is plaintext."""

    __tablename__ = "pins"

    id = Column(Integer, primary_key=True, index=True)
    workspace_id = _workspace_fk()
    label = Column(SealedText(), nullable=False)
    kind = Column(String, nullable=False)  # home | hybrid | onsite
    lat = Column(SealedFloat(), nullable=False)
    lng = Column(SealedFloat(), nullable=False)
    radius_km = Column(SealedFloat(), default=25.0, nullable=False)


# ---------------------------------------------------------------------------
# Company sources
# ---------------------------------------------------------------------------

class CompanySource(Base):
    """One company's careers page / job board, added by URL in the UI. Its URL and label are sealed; a source is
    found by URL through ``careers_url_index``, a blind index (``backend.sources.create_source``)."""

    __tablename__ = "company_sources"
    __table_args__ = (UniqueConstraint("workspace_id", "careers_url", name="uq_sources_workspace_url"),
                      Index("uq_sources_workspace_url_index", "workspace_id", "careers_url_index", unique=True))

    id = Column(Integer, primary_key=True, index=True)
    label = Column(SealedText(), nullable=True, default="")
    workspace_id = _workspace_fk()
    careers_url = Column(SealedText(), nullable=True, default="")
    careers_url_index = Column(LargeBinary, nullable=True)  # crypto.blind_index("careers_url", ...), set on write
    source_type = Column(String, nullable=True, default="generic")
    adapter_key = Column(SealedText(), nullable=True, default="")
    enabled = Column(Boolean, nullable=False, default=True)
    last_run = Column(DateTime, nullable=True)
    last_count = Column(Integer, nullable=True, default=0)
    last_error = Column(SealedText(), nullable=True, default=None)
    last_method = Column(Text, nullable=True)
    # Legacy columns from an earlier schema; unused but kept for old DBs (some of which made them NOT NULL). The
    # encryption migration empties them; url is still written (the careers URL again).
    company = Column(SealedText(), nullable=True, default="")
    url = Column(SealedText(), nullable=True, default="")
    board = Column(SealedText(), nullable=True, default="")
    host = Column(SealedText(), nullable=True, default="")
    tenant = Column(SealedText(), nullable=True, default="")
    site = Column(SealedText(), nullable=True, default="")
    base_url = Column(SealedText(), nullable=True, default="")


# ---------------------------------------------------------------------------
# Company profiles (filled by backend.research)
# ---------------------------------------------------------------------------

def company_key(name: str) -> str:
    """Normalised company name used to match jobs to profiles."""
    return " ".join((name or "").lower().split())


class CompanyProfile(Base):
    __tablename__ = "company_profiles"

    key = Column(String, primary_key=True)          # company_key(name)
    name = Column(String, nullable=False)           # name as it appears on jobs
    status = Column(String, nullable=False, default="missing")  # done | error | skipped
    official_name = Column(String, nullable=True)
    is_recruiter = Column(Boolean, nullable=False, default=False, server_default="0")
    business_model = Column(Text, nullable=True)
    ownership = Column(Text, nullable=True)
    headquarters = Column(String, nullable=True)
    # News/controversies section (refreshed on its own: backend.research.plan)
    controversies_json = Column(Text, nullable=False, default="[]")
    controversy_note = Column(Text, nullable=True)
    news_researched_at = Column(DateTime, nullable=True)
    sources_json = Column(Text, nullable=False, default="[]")
    employee_count = Column(String, nullable=True)
    # Employee-sentiment section (refreshed on its own)
    glassdoor_url = Column(String, nullable=True)      # verified company page, if found
    sentiment_json = Column(Text, nullable=True)       # employee sentiment summary + sources
    sentiment_researched_at = Column(DateTime, nullable=True)
    research_version = Column(Integer, nullable=False, default=1, server_default="1")
    # [{name, address, city}] offices found by web search (backend.offices)
    offices_json = Column(Text, nullable=True)
    offices_checked_at = Column(DateTime, nullable=True)
    offices_cities = Column(Text, nullable=True)  # JSON list of cities the office search looked in
    unverified_dropped = Column(Integer, nullable=False, default=0, server_default="0")
    error = Column(Text, nullable=True)
    model = Column(String, nullable=True)
    researched_at = Column(DateTime, nullable=True)   # the base profile (business, ownership, offices, ...)

    def to_dict(self) -> dict:
        def iso(value: Optional[datetime]) -> Optional[str]:
            return value.isoformat() if value else None

        return {
            "key": self.key,
            "name": self.name,
            "status": self.status,
            "official_name": self.official_name,
            "is_recruiter": bool(self.is_recruiter),
            "business_model": self.business_model,
            "ownership": self.ownership,
            "headquarters": self.headquarters,
            "controversies": json.loads(self.controversies_json or "[]"),
            "controversy_note": self.controversy_note,
            "sources": json.loads(self.sources_json or "[]"),
            "employee_count": self.employee_count,
            "glassdoor_url": self.glassdoor_url,
            "sentiment": json.loads(self.sentiment_json) if self.sentiment_json else None,
            "research_version": self.research_version or 1,
            "offices": json.loads(self.offices_json) if self.offices_json else None,
            "unverified_dropped": self.unverified_dropped or 0,
            "error": self.error,
            "model": self.model,
            "researched_at": iso(self.researched_at),
            "sentiment_researched_at": iso(self.sentiment_researched_at),
            "news_researched_at": iso(self.news_researched_at),
        }


# ---------------------------------------------------------------------------
# Cover letters + app-wide settings
# ---------------------------------------------------------------------------

class CoverLetter(Base):
    """Latest cover-letter draft per job (written by backend.cover_letters)."""

    __tablename__ = "cover_letters"

    id = Column(Integer, primary_key=True, index=True)
    job_id = Column(Integer, ForeignKey("jobs.id", ondelete="CASCADE"), unique=True, nullable=False, index=True)
    text = Column(SealedText(), nullable=False, default="")
    instructions = Column(SealedText(), nullable=True)
    edited = Column(Boolean, nullable=False, default=False, server_default="0")
    model = Column(String, nullable=True)
    reasoning_effort = Column(String, nullable=True)
    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow)


class ChatThread(Base):
    """A conversation with the LLM about one or more jobs (backend.job_chat)."""

    __tablename__ = "chat_threads"

    id = Column(Integer, primary_key=True, index=True)
    workspace_id = _workspace_fk()
    job_ids_json = Column(Text, nullable=False, default="[]")  # sorted job ids
    title = Column(SealedText(), nullable=True)
    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow)


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id = Column(Integer, primary_key=True, index=True)
    thread_id = Column(Integer, ForeignKey("chat_threads.id", ondelete="CASCADE"), nullable=False, index=True)
    role = Column(String, nullable=False)  # user | assistant
    content = Column(SealedText(), nullable=False, default="")
    sources_json = Column(SealedText(), nullable=True)  # [{title, url}] pages the web search returned
    web_search = Column(Boolean, nullable=False, default=False, server_default="0")
    model = Column(String, nullable=True)
    created_at = Column(DateTime, default=utcnow)


class LlmUsage(Base):
    """One row per answered LLM request (backend.llm_providers.record_usage), for checking that provider
    caching works and for usage statistics. Token counts are what the provider reported (None when it
    reported nothing):

    input_tokens        every prompt token, cached or not (Anthropic's input_tokens + cache reads + cache
                        writes; OpenAI-style prompt_tokens)
    cached_tokens       prompt tokens read from the provider's cache (billed at a discount)
    cache_write_tokens  prompt tokens written to it (Anthropic, OpenAI GPT-5.6+, OpenRouter)
    """

    __tablename__ = "llm_usage"

    id = Column(Integer, primary_key=True, index=True)
    created_at = Column(DateTime, default=utcnow, index=True)
    # Kept when the workspace is deleted: usage happened all the same.
    workspace_id = Column(Integer, ForeignKey("workspaces.id", ondelete="SET NULL"), nullable=True, index=True)
    # Whose request it was (the admin panel's usage per user); NULL once the account is deleted. Older rows have
    # none: the statistics fall back to the workspace's owner.
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    purpose = Column(String, nullable=True)         # scoring | chat | cover_letter | research | concise | summary ...
    provider = Column(String, nullable=True)        # llm_providers.provider_for: openai | anthropic | ...
    model = Column(String, nullable=True)
    input_tokens = Column(Integer, nullable=True)
    output_tokens = Column(Integer, nullable=True)
    cached_tokens = Column(Integer, nullable=True)
    cache_write_tokens = Column(Integer, nullable=True)


class AppSetting(Base):
    """App-wide settings stored as JSON (e.g. reasoning level per LLM task)."""

    __tablename__ = "app_settings"

    key = Column(String, primary_key=True)
    value_json = Column(Text, nullable=False, default="null")


# ---------------------------------------------------------------------------
# Background runs + caches
# ---------------------------------------------------------------------------

class SearchRun(Base):
    """One background search or scoring run, polled by the UI. What it found and how it went (stage, progress,
    summary, error) is sealed; its kind, state and times are not, so the queue works without anyone's key."""

    __tablename__ = "search_runs"

    id = Column(Integer, primary_key=True, index=True)
    workspace_id = _workspace_fk()
    kind = Column(String, nullable=False, default="search")  # search | score
    scheduled = Column(Boolean, nullable=False, default=False, server_default="0")  # started by backend.schedule
    state = Column(String, nullable=False, default="queued")  # queued | running | done | failed
    stage = Column(SealedText(), nullable=True)
    progress_json = Column(SealedText(), nullable=False, default="{}")
    summary_json = Column(SealedText(), nullable=True)
    error = Column(SealedText(), nullable=True)
    error_code = Column(String, nullable=True)  # a short code written without any key, e.g. "interrupted"
    # Queueing this run used one of its owner's free searches (backend.membership gives it back if the run fails).
    free_search = Column(Boolean, nullable=False, default=False, server_default="0")
    started_at = Column(DateTime, default=utcnow)
    finished_at = Column(DateTime, nullable=True)

    # What an error code means, for a run whose error was written without a key.
    ERROR_TEXT = {"interrupted": "interrupted by server restart",
                  "failed": "failed (the details couldn't be saved)"}

    def plain_dict(self) -> dict:
        """The plaintext fields only: needs no key (the run queue, the scheduler)."""
        return {
            "id": self.id,
            "workspace_id": self.workspace_id,
            "kind": self.kind,
            "scheduled": bool(self.scheduled),
            "free_search": bool(self.free_search),
            "state": self.state,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
        }

    def to_dict(self) -> dict:
        return {
            **self.plain_dict(),
            "stage": self.stage,
            "progress": json.loads(self.progress_json or "{}"),
            "summary": json.loads(self.summary_json) if self.summary_json else None,
            "error": self.error or self.ERROR_TEXT.get(self.error_code or ""),
        }


class SearchSchedule(Base):
    """One row per workspace (id = workspace id): when searches run by
    themselves (backend.schedule). No row means the preset schedule."""

    __tablename__ = "search_schedules"

    id = Column(Integer, ForeignKey("workspaces.id", ondelete="CASCADE"), primary_key=True)
    enabled = Column(Boolean, nullable=False, default=True, server_default="1")
    times = Column(JSON, nullable=True)             # ["10:00", ...] local time in the state's time zone
    days = Column(JSON, nullable=True)              # weekdays, Monday = 0
    skip_holidays = Column(Boolean, nullable=False, default=True, server_default="1")
    state = Column(String, nullable=False, default="NSW", server_default="NSW")  # holidays + time zone
    last_slot = Column(DateTime, nullable=True)     # UTC time of the last slot run or passed over
    # Why the last slot was passed over, when the person can do something about it (backend.schedule SKIP_TEXT:
    # "background_off", ...), and which slot. Plain codes: the scheduler writes them with nobody's key.
    skipped_reason = Column(String, nullable=True)
    skipped_slot = Column(DateTime, nullable=True)


class HttpCache(Base):
    """Short-lived cache of listing pages / API responses / robots.txt (shared by everyone). Listing URLs carry
    search terms, so the key is ``crypto.process_index("http", url)`` and the body is sealed with the process key:
    neither can be read after a restart, and startup deletes every row (``purge_http_cache``)."""

    __tablename__ = "http_cache"

    url = Column(String, primary_key=True)
    status_code = Column(Integer, nullable=False)
    body = Column(ProcessSealedText("http_cache.body"), nullable=False, default="")
    fetched_at = Column(DateTime, nullable=False, default=utcnow)


class CommuteCache(Base):
    """Journey planner / routing answers (backend.commute), refreshed weekly. Per user: the origins are their home
    and stations, so ``key`` is ``crypto.blind_index("commute", ...)`` (hex) under the user's key and the answer
    is sealed."""

    __tablename__ = "commute_cache"

    key = Column(String, primary_key=True)
    owner_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True)
    result_json = Column(SealedText(), nullable=False, default="null")
    fetched_at = Column(DateTime, nullable=False, default=utcnow)


class GeocodeCache(Base):
    """Nominatim results, cached forever (their usage policy requires it)."""

    __tablename__ = "geocode_cache"

    query = Column(String, primary_key=True)
    lat = Column(Float, nullable=True)
    lng = Column(Float, nullable=True)
    country = Column(String, nullable=True)
    display_name = Column(String, nullable=True)
    place_type = Column(String, nullable=True)  # Nominatim addresstype ("suburb", "city", ...)
    fetched_at = Column(DateTime, nullable=False, default=utcnow)


# ---------------------------------------------------------------------------
# Accounts (docs/design/accounts.md, section 4). backend/users.py creates them, backend/sessions.py signs in.
# Every *_enc / *_admin / *_self / wrapped column holds a blob made by backend/crypto.py; what opens it is noted
# beside it. Nothing here is a sealed column type: these are opened explicitly, with labels that name the row.
# ---------------------------------------------------------------------------

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    # HMAC-SHA256(pepper, normalised email) (backend.keys): finds the account without decrypting anything.
    email_hash = Column(LargeBinary, nullable=False, unique=True)
    role = Column(String, nullable=False, default="user", server_default="user")          # user | admin
    status = Column(String, nullable=False, default="active", server_default="active")    # active | disabled
    created_at = Column(DateTime, default=utcnow)
    created_via = Column(String, nullable=False)    # first_run | bootstrap | invite | open
    last_active_on = Column(Date, nullable=True)    # by day only, on purpose (design 2.5)
    email_verified = Column(Boolean, nullable=False, default=False, server_default="0")
    onboarding_step = Column(Integer, nullable=True)  # NULL: done (the sign-up wizard, Phase K)
    # Password: argon2id(salt, params) -> verifier (and the KEK that wraps the DEK, never stored).
    kdf_salt = Column(LargeBinary, nullable=False)
    kdf_params = Column(Text, nullable=False)       # crypto.KdfParams.to_json()
    pw_verifier = Column(LargeBinary, nullable=False)
    # Two-step sign-in (Phase E). Secrets under the server KEK, so a reset by email can still ask for a code.
    mfa_enabled = Column(Boolean, nullable=False, default=False, server_default="0")
    totp_secret_enc = Column(LargeBinary, nullable=True)
    totp_pending_enc = Column(LargeBinary, nullable=True)
    totp_last_step = Column(Integer, nullable=True)
    # Not used since Phase L: wrong passwords are counted in memory, per account and per source (backend/ratelimit.py,
    # backend/lockout.py). The columns stay so older databases keep working.
    failed_logins = Column(Integer, nullable=False, default=0, server_default="0")
    locked_until = Column(DateTime, nullable=True)
    # {email, name}: sealed to the admin details key (pii_admin), and under the user's own DEK (pii_self).
    pii_admin = Column(LargeBinary, nullable=True)
    pii_admin_version = Column(Integer, nullable=True)
    pii_self = Column(LargeBinary, nullable=True)
    prefs_enc = Column(LargeBinary, nullable=True)  # DEK: {theme} (Phase E)


class UserKeys(Base):
    """One row per user: the data key (DEK) under each thing that can open it, and the user's X25519 key pair."""

    __tablename__ = "user_keys"

    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    dek_kid = Column(LargeBinary, nullable=False)          # crypto.key_id(DEK)
    pw_wrapped_dek = Column(LargeBinary, nullable=False)   # wrap(KEK_pw, DEK, "pw|{uid}")
    # Background access (Phase G): the DEK under the server KEK, "srv|{uid}|{kek_id}".
    server_wrapped_dek = Column(LargeBinary, nullable=True)
    server_kek_id = Column(String, nullable=True)
    background_since = Column(DateTime, nullable=True)
    x25519_pub = Column(LargeBinary, nullable=False)
    # The X25519 private key, wrapped under the identity key (IK): wrap(IK, priv, "ik-priv|{uid}"). Before the IK existed
    # it was seal(DEK, priv, "user_keys.x25519_priv|{uid}"); that form is read only while ``pw_wrapped_ik`` is NULL, and
    # the next password sign-in replaces it (users.migrate_identity).
    x25519_priv_enc = Column(LargeBinary, nullable=False)
    # The IK under the password key, wrap(KEK_pw, IK, "pw-ik|{uid}"). NULL: the account has no IK yet. The IK is never
    # wrapped under the server KEK, so background access can't open the admin details key (accounts design 1.2).
    pw_wrapped_ik = Column(LargeBinary, nullable=True)
    recovery_salt = Column(LargeBinary, nullable=False)
    # Recovery codes not yet confirmed as saved: wrap(IK, codes, "pending-codes|{uid}") (a code opens the IK, so the DEK,
    # which the server KEK can open, must not hold them). Before the IK existed: seal(DEK, ...).
    pending_codes_enc = Column(LargeBinary, nullable=True)


class RecoveryCode(Base):
    __tablename__ = "recovery_codes"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    wrapped_dek = Column(LargeBinary, nullable=True)       # wrap(HKDF(code, recovery_salt), DEK, "rc|{uid}|{id}")
    # The same code's wrap of the IK, "rc-ik|{uid}|{id}". NULL for a code made before the IK existed.
    wrapped_ik = Column(LargeBinary, nullable=True)
    created_at = Column(DateTime, default=utcnow)
    used_at = Column(DateTime, nullable=True)


class AuthSession(Base):
    """A signed-in browser. The cookie holds a random secret; ``id`` is its SHA-256 and ``wrapped_dek`` the DEK
    under a key derived from it, so the database alone can't unwrap anything (backend.sessions).

    A browser-extension key is a row too (``state = "extension"``, backend/extension.py): ``id`` is a hash of the
    key's secret with a prefix of its own (``crypto.extension_key_id``), ``wrapped_dek`` the DEK under ``ext|{id}``,
    and it has no identity key and no recent sign-in. Being a session row, every "sign out everywhere" (a password
    change or reset, disabling or deleting the account) ends it too."""

    __tablename__ = "sessions"

    id = Column(String, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    # pending_mfa | active | extension
    state = Column(String, nullable=False, default="active", server_default="active")
    wrapped_dek = Column(LargeBinary, nullable=False)
    # The IK under the same cookie-derived key: "sess-ik|{sid}", or "pend-ik|{sid}" while pending_mfa. NULL for a session
    # made before the IK existed (the admin panel then asks to sign in again).
    wrapped_ik = Column(LargeBinary, nullable=True)
    persistent = Column(Boolean, nullable=False, default=False, server_default="0")  # "Keep me signed in"
    created_at = Column(DateTime, default=utcnow)
    last_seen_at = Column(DateTime, default=utcnow)
    idle_expires_at = Column(DateTime, nullable=False)
    absolute_expires_at = Column(DateTime, nullable=False)
    reauth_at = Column(DateTime, nullable=True)     # the password was last entered (sensitive actions need 15 min)
    mfa_failures = Column(Integer, nullable=False, default=0, server_default="0")  # wrong codes while pending_mfa (Phase E)
    # Extension keys only. ``public_id``: the short id the Settings card revokes it by (never the hash that
    # authenticates). ``label_enc``: the name the person gave it, ``crypto.seal``ed under their DEK as
    # ``sessions.label|{id}`` (bound to the row), so only they can read it.
    public_id = Column(String, nullable=True)
    label_enc = Column(LargeBinary, nullable=True)


class AuthToken(Base):
    """Single-use links: password reset, invite, email verification and change (Phases F and H)."""

    __tablename__ = "auth_tokens"

    id = Column(Integer, primary_key=True)
    kind = Column(String, nullable=False)           # reset | invite | verify_email | email_change
    token_hash = Column(LargeBinary, nullable=False, unique=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True)
    created_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime, default=utcnow)
    expires_at = Column(DateTime, nullable=False)
    used_at = Column(DateTime, nullable=True)
    payload_enc = Column(LargeBinary, nullable=True)  # sealed with a key derived from the token itself


class AdminKeyGrant(Base):
    """The admin details key's private half, sealed to one admin's X25519 public key (design 1.4)."""

    __tablename__ = "admin_key_grants"

    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    apk_version = Column(Integer, nullable=False)
    sealed_priv = Column(LargeBinary, nullable=False)  # seal_to(admin's x25519_pub, APK private, "grant|{uid}|{ver}")
    granted_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    granted_at = Column(DateTime, default=utcnow)


class AdminAudit(Base):
    __tablename__ = "admin_audit"

    id = Column(Integer, primary_key=True)
    at = Column(DateTime, default=utcnow, index=True)
    admin_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    action = Column(String, nullable=False)
    target_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    detail_admin = Column(LargeBinary, nullable=True)  # sealed to the admin details key


class Membership(Base):
    """Paid access (Phase I, backend.access and backend.billing). Gating fields are plaintext on purpose."""

    __tablename__ = "memberships"

    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    exempt = Column(Boolean, nullable=False, default=False, server_default="0")
    free_searches_used = Column(Integer, nullable=False, default=0, server_default="0")
    stripe_ref = Column(String, nullable=False, unique=True)  # random, opaque: client_reference_id, metadata.jh_ref
    stripe_status = Column(String, nullable=True)
    plan = Column(String, nullable=True)
    current_period_end = Column(DateTime, nullable=True)
    cancel_at_period_end = Column(Boolean, nullable=False, default=False, server_default="0")
    grace_until = Column(DateTime, nullable=True)  # while past_due: access continues until then (access.GRACE)
    stripe_synced_at = Column(DateTime, nullable=True)
    stripe_mode = Column(String, nullable=True)  # 'test' | 'live': the kind of Stripe key the status above came from
    billing_admin = Column(LargeBinary, nullable=True)  # admin details key
    billing_self = Column(LargeBinary, nullable=True)   # the user's DEK


class MembershipGrant(Base):
    __tablename__ = "membership_grants"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    kind = Column(String, nullable=False)           # free_period | manual
    starts_at = Column(DateTime, nullable=False)
    ends_at = Column(DateTime, nullable=False)
    created_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime, default=utcnow)
    revoked_at = Column(DateTime, nullable=True)
    note_admin = Column(LargeBinary, nullable=True)  # admin details key


class EmailPrefs(Base):
    """Job digest settings (Phase J). Plaintext: the unsubscribe link must work without anyone's key."""

    __tablename__ = "email_prefs"

    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    digest_enabled = Column(Boolean, nullable=False, default=False, server_default="0")
    digest_every_days = Column(Integer, nullable=False, default=1, server_default="1")
    opted_in_at = Column(DateTime, nullable=True)
    last_sent_at = Column(DateTime, nullable=True)
    last_checked_at = Column(DateTime, nullable=True)
    unsubscribe_id = Column(String, nullable=False, unique=True)


# ---------------------------------------------------------------------------
# Session + schema management
# ---------------------------------------------------------------------------

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# Rows that belong to a workspace must say which: no default, and none in the schema of new databases.
_WORKSPACE_ROWS = (Job, JobDuplicate, Document, Pin, CompanySource, ChatThread, SearchRun)


def _refuse_rows_without_workspace(_mapper, _connection, target) -> None:
    if target.workspace_id is None:
        raise ValueError(f"A {type(target).__name__} row needs its workspace_id: rows without one are refused, "
                         "never put in a default workspace")


for _model in _WORKSPACE_ROWS:
    event.listen(_model, "before_insert", _refuse_rows_without_workspace)


# Blind indexes, recomputed on every write under the owner's key (a slip that wrote one under another key would
# only make the row unfindable, never visible): jobs by URL, company sources by careers URL.
def _job_url_index(_mapper, _connection, job: Job) -> None:
    url = job.url
    job.url_index = crypto.blind_index("url", url) if url else None


def _source_url_index(_mapper, _connection, source: CompanySource) -> None:
    url = source.careers_url
    source.careers_url_index = crypto.blind_index("careers_url", url) if url else None


for _when in ("before_insert", "before_update"):
    event.listen(Job, _when, _job_url_index)
    event.listen(CompanySource, _when, _source_url_index)


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


def _rebuild_for_workspaces(table_name: str) -> None:
    """Replace a table whose URL column was globally UNIQUE with the current
    definition (unique per workspace), keeping every row (SQLite can't drop a
    constraint in place). Follows SQLite's create-copy-drop-rename recipe with
    foreign keys off, so rows referencing this table are untouched."""
    insp = inspect(engine)
    if not insp.has_table(table_name):
        return
    if "workspace_id" in {c["name"] for c in insp.get_columns(table_name)}:
        return
    table = Base.metadata.tables[table_name]
    old_cols = {c["name"] for c in insp.get_columns(table_name)}
    cols = ", ".join(c.name for c in table.columns if c.name in old_cols)
    ddl = str(CreateTable(table).compile(engine)).replace(
        f"CREATE TABLE {table_name} ", f"CREATE TABLE {table_name}_new ", 1)
    with engine.connect() as conn:
        conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
        conn.exec_driver_sql(ddl)
        conn.exec_driver_sql(f"INSERT INTO {table_name}_new ({cols}) SELECT {cols} FROM {table_name}")
        conn.exec_driver_sql(f"DROP TABLE {table_name}")
        conn.exec_driver_sql(f"ALTER TABLE {table_name}_new RENAME TO {table_name}")
        for index in table.indexes:
            index.create(conn, checkfirst=True)
        problems = conn.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
        if problems:
            conn.rollback()
            raise RuntimeError(f"workspace migration of {table_name} failed foreign key check: {problems[:5]}")
        conn.commit()
        conn.exec_driver_sql("PRAGMA foreign_keys=ON")
    log.info("migrated %s to per-workspace uniqueness", table_name)


def _rebuild_workspaces_for_autoincrement() -> None:
    """Replace a workspaces table made without AUTOINCREMENT (SQLite can't add it in place): create-copy-drop-rename
    with foreign keys off, as ``_rebuild_for_workspaces`` does, so the rows that reference it are untouched. Ids
    given out before this are not remembered, only the highest one still there."""
    with engine.connect() as conn:
        sql = conn.exec_driver_sql("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'workspaces'").scalar()
    if not sql or "AUTOINCREMENT" in sql.upper():
        return
    table = Base.metadata.tables["workspaces"]
    old_cols = {c["name"] for c in inspect(engine).get_columns("workspaces")}
    cols = ", ".join(c.name for c in table.columns if c.name in old_cols)
    ddl = str(CreateTable(table).compile(engine)).replace("CREATE TABLE workspaces ", "CREATE TABLE workspaces_new ", 1)
    with engine.connect() as conn:
        conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
        try:
            conn.exec_driver_sql("DROP TABLE IF EXISTS workspaces_new")
            conn.exec_driver_sql(ddl)
            conn.exec_driver_sql(f"INSERT INTO workspaces_new ({cols}) SELECT {cols} FROM workspaces")
            conn.exec_driver_sql("DROP TABLE workspaces")
            conn.exec_driver_sql("ALTER TABLE workspaces_new RENAME TO workspaces")
            for index in table.indexes:
                index.create(conn, checkfirst=True)
            problems = conn.exec_driver_sql("PRAGMA foreign_key_check(workspaces)").fetchall()
            if problems:
                conn.rollback()
                raise RuntimeError(f"workspaces migration failed foreign key check: {problems[:5]}")
            conn.commit()
        finally:
            conn.exec_driver_sql("PRAGMA foreign_keys=ON")
    log.info("migrated workspaces to ids that are never reused")


def encryption_done(conn) -> bool:
    """Whether the encryption migration (backend.migrate_encryption) has finished on this database: the key file says
    so (the database alone can't be trusted with it: someone who can write it could set it back), or its state does."""
    from backend import keys

    if keys.encryption_marked():
        return True
    raw = conn.execute(text("SELECT value_json FROM app_settings WHERE key = 'encryption_migration'")).scalar()
    try:
        return bool(raw) and json.loads(raw).get("state") == "done"
    except (ValueError, AttributeError):
        return False


def _fix_legacy_data() -> None:
    """One-off data repairs for databases written by the previous code."""
    with engine.begin() as conn:
        conn.execute(text("UPDATE documents SET kind='resume' WHERE kind IS NULL OR kind='cv'"))
        if not encryption_done(conn):  # these read job columns that are sealed from then on
            conn.execute(text("UPDATE jobs SET excluded_reason = NULL WHERE excluded_reason = ''"))
            # Jobs from the old generic scraper were not linked to their source.
            conn.execute(text(
                "UPDATE jobs SET source_id = (SELECT id FROM company_sources cs "
                "WHERE 'generic:' || cs.label = jobs.source) "
                "WHERE source_id IS NULL AND source LIKE 'generic:%'"
            ))
        # One research used to cover the whole profile: its sections date from it (accounts design §14.7).
        conn.execute(text("UPDATE company_profiles SET news_researched_at = researched_at "
                          "WHERE news_researched_at IS NULL AND status = 'done' AND researched_at IS NOT NULL"))
        conn.execute(text("UPDATE company_profiles SET sentiment_researched_at = researched_at "
                          "WHERE sentiment_researched_at IS NULL AND status = 'done' AND researched_at IS NOT NULL "
                          "AND sentiment_json IS NOT NULL"))


def _ensure_indexes() -> None:
    """Indexes on columns added to existing tables (create_all only indexes new tables)."""
    with engine.begin() as conn:
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_jobs_posting_id ON jobs (posting_id)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_postings_repost_key ON postings (repost_key)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_workspaces_owner_id ON workspaces (owner_id)"))
        conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_jobs_workspace_url_index "
                          "ON jobs (workspace_id, url_index)"))
        conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_sources_workspace_url_index "
                          "ON company_sources (workspace_id, careers_url_index)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_commute_cache_owner_id ON commute_cache (owner_id)"))
        conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_sessions_public_id ON sessions (public_id)"))


def purge_http_cache() -> None:
    """Delete every http_cache row: they are sealed with the key of the process that wrote them, which is gone
    after a restart (and rows from before encryption hold plaintext URLs)."""
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM http_cache"))


def init_db() -> None:
    from backend import pool_migration

    DATA_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    owner_only(DATA_DIR, 0o700)  # (a folder made by an earlier version is 0755)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    _rebuild_legacy_fit_results()
    Base.metadata.tables["users"].create(bind=engine, checkfirst=True)
    Base.metadata.tables["workspaces"].create(bind=engine, checkfirst=True)
    if "owner_id" not in {c["name"] for c in inspect(engine).get_columns("workspaces")}:
        with engine.begin() as conn:  # a database from before accounts (only this column: see _rebuild_for_workspaces)
            conn.execute(text("ALTER TABLE workspaces ADD COLUMN owner_id INTEGER"))
        log.info("migrated: added workspaces.owner_id")
    _rebuild_workspaces_for_autoincrement()
    with engine.begin() as conn:
        # The first workspace, only on a fresh database: the first admin claims it (backend.users). Once there
        # are accounts, every workspace is made by and for one of them.
        conn.execute(text("INSERT OR IGNORE INTO workspaces (id, name, created_at) "
                          "SELECT 1, 'My job search', CURRENT_TIMESTAMP WHERE NOT EXISTS (SELECT 1 FROM users)"))
    _rebuild_for_workspaces("company_sources")
    _rebuild_for_workspaces("jobs")
    Base.metadata.create_all(bind=engine)
    _add_missing_columns()
    _ensure_indexes()
    _fix_legacy_data()
    purge_http_cache()
    pool_migration.migrate()  # per-workspace job copies -> the shared posting pool (once; resumable)
    # Then the encryption migration (backend.migrate_encryption), which needs the first admin's key: it runs when
    # they sign in.

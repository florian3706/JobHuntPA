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
from typing import Optional

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
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

log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
UPLOAD_DIR = DATA_DIR / "uploads"
DB_PATH = Path(os.getenv("JOBHUNT_DB_PATH", DATA_DIR / "jobhunt.db"))
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{DB_PATH}")

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False, "timeout": 30},
    # A failed statement's error message never carries its bound values: they can be personal data, and those
    # messages end up in logs (accounts design §15). Never set echo=True for the same reason.
    hide_parameters=True,
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

class Workspace(Base):
    """A separate job search: its own settings, sources, documents, pins and jobs. Each belongs to one account
    (``owner_id``); a workspace from before accounts has none until the first admin claims it (backend.users)."""

    __tablename__ = "workspaces"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    created_at = Column(DateTime, default=utcnow)
    # Who it belongs to: backend.workspaces.current_workspace answers 404 for anyone else's. Deleting an account
    # deletes its workspaces. (Databases from before accounts get this column without the foreign key, which
    # SQLite can't add later: delete an account's workspaces explicitly, don't rely on the cascade.)
    owner_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True)


def _workspace_fk():
    return Column(Integer, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False,
                  default=1, server_default="1", index=True)


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


# A job's content: the posting's for a pool link (with this workspace's own value, when it has one, on top), or
# all on the job for a private job. ``Job.<name>`` reads the effective value; assigning writes the workspace's own.
JOB_CONTENT = ("source", "external_id", "company", "title", "location_text", "country", "work_mode", "employment_type",
               "office_days", "salary_min", "salary_max", "salary_text", "description", "description_hash",
               "detail_status", "detail_fetched_at", "posted_at", "industry")


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

    return property(get, set, doc=f"Effective {name}: this workspace's own value, else the posting's.")


class Job(Base):
    """A job in a workspace: the link from the workspace to a shared ``Posting`` plus everything personal about
    it (status, hidden, exclusion, placement, office, verdict, and own content such as a privately uploaded full
    ad). A private job (``posting_id`` NULL: imported from a saved page) keeps all its content here.

    The content attributes (``title``, ``description``, ... see JOB_CONTENT) are Python properties without an SQL
    form: filter on them in Python. Their own values are the ``_title``... columns (named ``title``... in the
    table, where they were before the pool), NULL meaning "the posting's".
    """

    __tablename__ = "jobs"
    __table_args__ = (UniqueConstraint("workspace_id", "url", name="uq_jobs_workspace_url"),)

    id = Column(Integer, primary_key=True, index=True)
    workspace_id = _workspace_fk()
    url = Column(String, nullable=True)             # the link key: the posting's URL, or a private job's own
    posting_id = Column(Integer, ForeignKey("postings.id", ondelete="SET NULL"), nullable=True, index=True)
    posting = relationship(Posting, lazy="joined")
    origin = Column(String, nullable=True)          # pool | private; NULL: not migrated yet (backend.pool_migration)
    posting_rev = Column(Integer, nullable=True)    # the posting's rev this link was last refreshed against
    source_id = Column(Integer, ForeignKey("company_sources.id", ondelete="SET NULL"), nullable=True)
    lat = Column(Float, nullable=True)              # placed for this workspace (nearest place to its home)
    lng = Column(Float, nullable=True)
    distance_km = Column(Float, nullable=True)
    # to_review | shortlisted | applied | interviewing | rejected | not_interested
    status = Column(String, default="to_review", nullable=False)
    # Never filter on these two in SQL: Phase D seals them so that even whether they're set doesn't show.
    excluded_reason = Column(Text, nullable=True)
    hidden = Column(Boolean, nullable=False, default=False, server_default="0")  # hidden by the user
    first_seen = Column(DateTime, default=utcnow)   # first linked to this workspace
    last_seen = Column(DateTime, default=utcnow)    # last listed by this workspace's own search
    # Set on a copy of an ad that is merged into another job (backend.duplicates):
    # the copy keeps its row so later searches recognise its URL, but only the
    # job it points to is listed, scored and researched.
    duplicate_of = Column(Integer, ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True, index=True)
    # Where the office actually is, when the ad's location is only a city
    # (backend.offices). Its coordinates replace the listing's for the
    # location filter and distance.
    office_text = Column(String, nullable=True)     # address or suburb
    office_lat = Column(Float, nullable=True)
    office_lng = Column(Float, nullable=True)
    office_source = Column(String, nullable=True)   # ad | company | user
    office_checked_at = Column(DateTime, nullable=True)  # ad read for an office (found or not)
    # The user's own call on the location: ok (passes the location filter) | too_far
    location_verdict = Column(String, nullable=True)

    # Own content (see JOB_CONTENT): overrides of the posting, or a private job's content.
    _source = Column("source", String, nullable=True)
    _external_id = Column("external_id", String, nullable=True)
    _company = Column("company", String, nullable=True)
    _title = Column("title", String, nullable=True)
    _location_text = Column("location_text", String, nullable=True)
    _country = Column("country", String, nullable=True)
    _work_mode = Column("work_mode", String, nullable=True)
    _employment_type = Column("employment_type", String, nullable=True)
    _office_days = Column("office_days", Integer, nullable=True)
    _salary_min = Column("salary_min", Integer, nullable=True)
    _salary_max = Column("salary_max", Integer, nullable=True)
    _salary_text = Column("salary_text", String, nullable=True)
    _description = Column("description", Text, nullable=True)
    _description_hash = Column("description_hash", String, nullable=True, index=True)
    _detail_status = Column("detail_status", String, nullable=False, default="none", server_default="none")
    _detail_fetched_at = Column("detail_fetched_at", DateTime, nullable=True)
    _posted_at = Column("posted_at", String, nullable=True)
    _industry = Column("industry", String, nullable=True)
    _closed_at = Column("closed_at", DateTime, nullable=True)  # a private job's; a pool link's is the posting's

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
    score = Column(Integer, nullable=True)
    reasons_json = Column(Text, nullable=False, default="[]")
    created_at = Column(DateTime, default=utcnow)
    decided_at = Column(DateTime, nullable=True)


class FitResult(Base):
    """LLM fit score per job (written by backend.scorer)."""

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
    filename = Column(String, nullable=False)       # the file's name, or a note's / summary's title
    filetype = Column(String, nullable=True)        # ".pdf", ".docx", ... | "note" | "summary"
    stored_name = Column(String, nullable=True)
    text = Column(Text, nullable=True)              # extracted once from the file, or the note / summary
    kind = Column(String, nullable=True)            # one of DOCUMENT_KINDS
    use_for_scoring = Column(Boolean, nullable=False, default=True, server_default="1")
    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, nullable=True)    # a note, summary or concise version last edited
    # A shorter rewrite of ``text`` (LLM-written, then editable). ``use_concise`` picks which of the two
    # the LLM is sent (text_for_llm).
    concise_text = Column(Text, nullable=True)
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
    """One row per workspace (id = workspace id): search profile."""

    __tablename__ = "user_profile"

    id = Column(Integer, primary_key=True)
    titles = Column(JSON, default=list, nullable=False)
    keywords_include = Column(JSON, default=list, nullable=False)
    keywords_exclude = Column(JSON, default=list, nullable=False)
    salary_floor = Column(Integer, nullable=True)
    remote_aus_ok = Column(Boolean, default=True, nullable=False)
    remote_global_ok = Column(Boolean, default=False, nullable=False)
    allow_hybrid = Column(Boolean, nullable=False, default=True, server_default="1")
    max_office_days = Column(Integer, nullable=True)  # hybrid: most office days/week accepted
    allow_onsite = Column(Boolean, nullable=False, default=True, server_default="1")
    seek_enabled = Column(Boolean, nullable=False, default=True, server_default="1")
    seek_locations = Column(JSON, nullable=True)
    seek_max_pages = Column(Integer, nullable=False, default=1, server_default="1")  # unused (SEEK pagination is off-limits)
    # Commute (backend.commute): public transport starts at these stations
    # (empty = the home pin); peak trips arrive by / leave at these times.
    commute_from = Column(JSON, nullable=True)
    commute_arrive_by = Column(String, nullable=False, default="09:00", server_default="09:00")
    commute_leave_at = Column(String, nullable=False, default="17:00", server_default="17:00")


class DealbreakerSet(Base):
    """One row per workspace (id = workspace id): exclusion tag arrays."""

    __tablename__ = "dealbreakers"

    id = Column(Integer, primary_key=True)
    industries = Column(JSON, default=list, nullable=False)
    keywords = Column(JSON, default=list, nullable=False)


class Pin(Base):
    """Location pins: home base + acceptable hybrid/onsite workplaces."""

    __tablename__ = "pins"

    id = Column(Integer, primary_key=True, index=True)
    workspace_id = _workspace_fk()
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
    __table_args__ = (UniqueConstraint("workspace_id", "careers_url", name="uq_sources_workspace_url"),)

    id = Column(Integer, primary_key=True, index=True)
    label = Column(String, nullable=True, default="")
    workspace_id = _workspace_fk()
    careers_url = Column(String, nullable=True, default="")
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
    text = Column(Text, nullable=False, default="")
    instructions = Column(Text, nullable=True)
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
    title = Column(String, nullable=True)
    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow)


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id = Column(Integer, primary_key=True, index=True)
    thread_id = Column(Integer, ForeignKey("chat_threads.id", ondelete="CASCADE"), nullable=False, index=True)
    role = Column(String, nullable=False)  # user | assistant
    content = Column(Text, nullable=False, default="")
    sources_json = Column(Text, nullable=True)  # [{title, url}] pages the web search returned
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
    """One background search or scoring run, polled by the UI."""

    __tablename__ = "search_runs"

    id = Column(Integer, primary_key=True, index=True)
    workspace_id = _workspace_fk()
    kind = Column(String, nullable=False, default="search")  # search | score
    scheduled = Column(Boolean, nullable=False, default=False, server_default="0")  # started by backend.schedule
    state = Column(String, nullable=False, default="queued")  # queued | running | done | failed
    stage = Column(String, nullable=True)
    progress_json = Column(Text, nullable=False, default="{}")
    summary_json = Column(Text, nullable=True)
    error = Column(Text, nullable=True)
    error_code = Column(String, nullable=True)  # a short code written without any key, e.g. "interrupted"
    started_at = Column(DateTime, default=utcnow)
    finished_at = Column(DateTime, nullable=True)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "workspace_id": self.workspace_id,
            "kind": self.kind,
            "scheduled": bool(self.scheduled),
            "state": self.state,
            "stage": self.stage,
            "progress": json.loads(self.progress_json or "{}"),
            "summary": json.loads(self.summary_json) if self.summary_json else None,
            "error": self.error,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
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


class HttpCache(Base):
    """Short-lived cache of listing pages / API responses / robots.txt."""

    __tablename__ = "http_cache"

    url = Column(String, primary_key=True)
    status_code = Column(Integer, nullable=False)
    body = Column(Text, nullable=False, default="")
    fetched_at = Column(DateTime, nullable=False, default=utcnow)


class CommuteCache(Base):
    """Journey planner / routing answers (backend.commute), refreshed weekly."""

    __tablename__ = "commute_cache"

    key = Column(String, primary_key=True)
    result_json = Column(Text, nullable=False, default="null")
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
    # Lockout: after 5 failures in a row, wait 30 s, doubling up to 15 minutes (backend.ratelimit).
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
    x25519_priv_enc = Column(LargeBinary, nullable=False)  # seal(DEK, "user_keys.x25519_priv|{uid}")
    recovery_salt = Column(LargeBinary, nullable=False)
    pending_codes_enc = Column(LargeBinary, nullable=True)  # DEK: recovery codes not yet confirmed as saved


class RecoveryCode(Base):
    __tablename__ = "recovery_codes"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    wrapped_dek = Column(LargeBinary, nullable=True)       # wrap(HKDF(code, recovery_salt), DEK, "rc|{uid}|{id}")
    created_at = Column(DateTime, default=utcnow)
    used_at = Column(DateTime, nullable=True)


class AuthSession(Base):
    """A signed-in browser. The cookie holds a random secret; ``id`` is its SHA-256 and ``wrapped_dek`` the DEK
    under a key derived from it, so the database alone can't unwrap anything (backend.sessions)."""

    __tablename__ = "sessions"

    id = Column(String, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    state = Column(String, nullable=False, default="active", server_default="active")  # pending_mfa | active
    wrapped_dek = Column(LargeBinary, nullable=False)
    persistent = Column(Boolean, nullable=False, default=False, server_default="0")  # "Keep me signed in"
    created_at = Column(DateTime, default=utcnow)
    last_seen_at = Column(DateTime, default=utcnow)
    idle_expires_at = Column(DateTime, nullable=False)
    absolute_expires_at = Column(DateTime, nullable=False)
    reauth_at = Column(DateTime, nullable=True)     # the password was last entered (sensitive actions need 15 min)


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
    stripe_synced_at = Column(DateTime, nullable=True)
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


def _fix_legacy_data() -> None:
    """One-off data repairs for databases written by the previous code."""
    with engine.begin() as conn:
        conn.execute(text("UPDATE documents SET kind='resume' WHERE kind IS NULL OR kind='cv'"))
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
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_workspaces_owner_id ON workspaces (owner_id)"))


def init_db() -> None:
    from backend import pool_migration

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    _rebuild_legacy_fit_results()
    Base.metadata.tables["users"].create(bind=engine, checkfirst=True)
    Base.metadata.tables["workspaces"].create(bind=engine, checkfirst=True)
    if "owner_id" not in {c["name"] for c in inspect(engine).get_columns("workspaces")}:
        with engine.begin() as conn:  # a database from before accounts (only this column: see _rebuild_for_workspaces)
            conn.execute(text("ALTER TABLE workspaces ADD COLUMN owner_id INTEGER"))
        log.info("migrated: added workspaces.owner_id")
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
    pool_migration.migrate()  # per-workspace job copies -> the shared posting pool (once; resumable)

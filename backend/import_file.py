"""The import file: the one machine-readable file in a data export, from which an import brings the data back.

Its format is written down in docs/design/open-source.md, Part 7. In short: a JSON object with ``format``
("jobhuntpa-import") and ``format_version`` (``FORMAT_VERSION``), the app's version and the time of the export, then
per workspace the search setup (with its starting points), pins, sources, schedule, documents (text, concise version,
flags and the path in the zip of each one's original file), jobs (the ad, status and its dates, hidden, excluded,
office, verdict, fit result, cover letter) and chats, and the research on the companies of those jobs. Never the
password, keys, sessions, extension keys or the AI key: none of them is read here.

``workspace_data`` and ``company_data`` write it (backend/export.py, in the exporting person's key scope).
``ImportFile`` reads it back: every field is typed and bounded, and anything the format doesn't name is ignored, so an
edited or hostile file can't put a value the app doesn't expect into the database (backend/data_import.py).

References between things in one workspace (a job's merged copy, a summary's documents, a chat's jobs, a job's
source) are numbers local to the file (``ref``), never the exporting install's row ids.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Annotated, Any, Literal, Optional

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints
from sqlalchemy.orm import Session

from backend import schedule
from backend.db import (
    JOB_CONTENT,
    PROJECT_ROOT,
    ChatMessage,
    ChatThread,
    CompanyProfile,
    CompanySource,
    CoverLetter,
    DealbreakerSet,
    Document,
    FitResult,
    Job,
    Pin,
    UserProfile,
    Workspace,
)
from backend.employment import EMPLOYMENT_TYPES

FORMAT = "jobhuntpa-import"
FORMAT_VERSION = 1
FILE_NAME = "JobHuntPA import file.json"
# The first release whose export holds an import file (what an import says about an export without one). The next
# release after 1.13.0: docs/design/open-source.md Part 6 plans 2.0.0 for the open-source version.
FIRST_APP_VERSION = "2.0.0"
CHUNK = 200

STATUSES = ("to_review", "shortlisted", "applied", "interviewing", "rejected", "not_interested")


def app_version() -> str:
    try:
        return (PROJECT_ROOT / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def iso(value: Any) -> Optional[str]:
    """A stored time (naive UTC) as ISO 8601 with its ``Z``."""
    return value.replace(tzinfo=None).isoformat() + "Z" if isinstance(value, datetime) else None


def _json(value: Optional[str], fallback: Any = None) -> Any:
    try:
        return json.loads(value) if value else fallback
    except ValueError:
        return fallback


# ---------------------------------------------------------------------------
# Writing (the export)
# ---------------------------------------------------------------------------

def starting_points_out(profile: UserProfile) -> Any:
    """What the search setup stores for its starting points, as it stores it. Today that is the "Commute from"
    stations (a list of text); the format calls the field ``starting_points`` whatever its shape (Part 7)."""
    return list(profile.commute_from or [])


def _search_setup(db: Session, ws: Workspace) -> Optional[dict]:
    profile = db.get(UserProfile, ws.id)
    dealbreakers = db.get(DealbreakerSet, ws.id)
    if profile is None and dealbreakers is None:
        return None
    out: dict[str, Any] = {}
    if profile is not None:
        out.update(titles=list(profile.titles or []), keywords_include=list(profile.keywords_include or []),
                   keywords_exclude=list(profile.keywords_exclude or []), salary_floor=profile.salary_floor,
                   remote_aus_ok=bool(profile.remote_aus_ok), remote_global_ok=bool(profile.remote_global_ok),
                   allow_hybrid=bool(profile.allow_hybrid), max_office_days=profile.max_office_days,
                   allow_onsite=bool(profile.allow_onsite), seek_enabled=bool(profile.seek_enabled),
                   seek_locations=list(profile.seek_locations or []), starting_points=starting_points_out(profile))
    if dealbreakers is not None:
        out["dealbreakers"] = {"industries": list(dealbreakers.industries or []),
                               "keywords": list(dealbreakers.keywords or [])}
    return out


def _ad(job: Job) -> dict:
    out = {"url": job.url}
    for name in JOB_CONTENT:
        value = getattr(job, name)
        out[name] = iso(value) if isinstance(value, datetime) else value
    return out


def _own(job: Job) -> list[str]:
    """The ad's fields that are this workspace's own values (all of a private job's)."""
    if job.posting is None:
        return [name for name in JOB_CONTENT if getattr(job, name) is not None]
    return [name for name in JOB_CONTENT if getattr(job, "_" + name) not in (None, "none" if name == "detail_status" else None)]


def _fit(fit: Optional[FitResult]) -> Optional[dict]:
    if fit is None:
        return None
    evidence = _json(fit.evidence_json, {})
    return {"status": fit.status, "score": fit.score, "evidence": evidence if isinstance(evidence, dict) else {},
            "error": fit.error, "profile_hash": fit.profile_hash, "model": fit.model, "created_at": iso(fit.created_at)}


def _letter(letter: Optional[CoverLetter]) -> Optional[dict]:
    if letter is None:
        return None
    return {"text": letter.text or "", "instructions": letter.instructions, "edited": bool(letter.edited),
            "model": letter.model, "reasoning_effort": letter.reasoning_effort, "created_at": iso(letter.created_at),
            "updated_at": iso(letter.updated_at)}


def workspace_data(db: Session, ws: Workspace, originals: dict[int, str]) -> tuple[dict, set[str]]:
    """One workspace for the import file, and the company keys of its jobs (for the research). ``originals``: each
    document's original file, by document id, as its path inside the export's folder."""
    from backend.db import company_key
    from backend.pool import jobs_query

    sched = schedule.get_schedule(db, ws.id)
    sources = db.query(CompanySource).filter(CompanySource.workspace_id == ws.id).order_by(CompanySource.id).all()
    source_ref = {s.id: n for n, s in enumerate(sources, 1)}
    documents = db.query(Document).filter(Document.workspace_id == ws.id).order_by(Document.id).all()
    doc_ref = {d.id: n for n, d in enumerate(documents, 1)}
    job_ids = [i for (i,) in db.query(Job.id).filter(Job.workspace_id == ws.id).order_by(Job.id)]
    job_ref = {job_id: n for n, job_id in enumerate(job_ids, 1)}
    fits = {f.job_id: f for f in db.query(FitResult).join(Job, Job.id == FitResult.job_id).filter(Job.workspace_id == ws.id)}
    letters = {c.job_id: c for c in db.query(CoverLetter).join(Job, Job.id == CoverLetter.job_id).filter(Job.workspace_id == ws.id)}
    jobs, companies = [], set()
    for start in range(0, len(job_ids), CHUNK):
        ids = job_ids[start:start + CHUNK]
        for job in jobs_query(db, descriptions=True).filter(Job.workspace_id == ws.id, Job.id.in_(ids)).order_by(Job.id):
            if job.duplicate_of is None and company_key(job.company or ""):
                companies.add(company_key(job.company or ""))
            office = None
            if job.office_text is not None or job.office_checked_at is not None:
                office = {"text": job.office_text, "lat": job.office_lat, "lng": job.office_lng,
                          "source": job.office_source, "checked_at": iso(job.office_checked_at)}
            jobs.append({
                "ref": job_ref[job.id], "origin": "pool" if job.posting is not None else "private",
                "ad": _ad(job), "own": _own(job), "status": job.status or "to_review", "hidden": bool(job.hidden),
                "excluded_reason": job.excluded_reason, "first_seen": iso(job.first_seen), "last_seen": iso(job.last_seen),
                "closed_at": iso(job.closed_at), "location_verdict": job.location_verdict, "office": office,
                "lat": job.lat, "lng": job.lng, "distance_km": job.distance_km,
                "duplicate_of": job_ref.get(job.duplicate_of), "source": source_ref.get(job.source_id),
                "fit": _fit(fits.get(job.id)), "cover_letter": _letter(letters.get(job.id)),
            })
    chats = []
    for thread in db.query(ChatThread).filter(ChatThread.workspace_id == ws.id).order_by(ChatThread.id):
        ids = [i for i in _json(thread.job_ids_json, []) or [] if isinstance(i, int)]
        messages = []
        for m in db.query(ChatMessage).filter(ChatMessage.thread_id == thread.id).order_by(ChatMessage.id):
            pages = _json(m.sources_json, None)
            messages.append({"role": m.role, "content": m.content or "", "sources": pages if isinstance(pages, list) else None,
                             "web_search": bool(m.web_search), "model": m.model, "created_at": iso(m.created_at)})
        chats.append({"title": thread.title, "created_at": iso(thread.created_at), "updated_at": iso(thread.updated_at),
                      "jobs": [job_ref[i] for i in ids if i in job_ref], "messages": messages})
    data = {
        "name": ws.name,
        "created_at": iso(ws.created_at),
        "search_setup": _search_setup(db, ws),
        "pins": [{"label": p.label, "kind": p.kind, "lat": p.lat, "lng": p.lng, "radius_km": p.radius_km}
                 for p in db.query(Pin).filter(Pin.workspace_id == ws.id).order_by(Pin.id)],
        "sources": [{"ref": source_ref[s.id], "label": s.label, "url": s.careers_url, "type": s.source_type,
                     "enabled": bool(s.enabled)} for s in sources],
        "schedule": sched.model_dump(),
        "documents": [{
            "ref": doc_ref[d.id], "filename": d.filename, "filetype": d.filetype, "kind": d.kind, "text": d.text,
            "concise_text": d.concise_text, "use_concise": bool(d.use_concise), "use_for_scoring": bool(d.use_for_scoring),
            "concise_model": d.concise_model, "concise_at": iso(d.concise_at), "created_at": iso(d.created_at),
            "updated_at": iso(d.updated_at),
            "summary_of": [doc_ref[i] for i in (d.summary_of or []) if isinstance(i, int) and i in doc_ref],
            "original": originals.get(d.id),
        } for d in documents],
        "jobs": jobs,
        "chats": chats,
    }
    return data, companies


COMPANY_JSON = {"controversies": "controversies_json", "sources": "sources_json", "sentiment": "sentiment_json",
                "offices": "offices_json", "offices_cities": "offices_cities"}
COMPANY_TEXT = ("name", "status", "official_name", "business_model", "ownership", "headquarters", "controversy_note",
                "employee_count", "glassdoor_url", "error", "model")
COMPANY_TIMES = ("researched_at", "news_researched_at", "sentiment_researched_at", "offices_checked_at")


def company_data(p: CompanyProfile) -> dict:
    out: dict[str, Any] = {"key": p.key, "is_recruiter": bool(p.is_recruiter), "research_version": p.research_version or 1,
                           "unverified_dropped": p.unverified_dropped or 0}
    out.update({name: getattr(p, name) for name in COMPANY_TEXT})
    out.update({name: iso(getattr(p, name)) for name in COMPANY_TIMES})
    out.update({name: _json(getattr(p, column)) for name, column in COMPANY_JSON.items()})
    return out


def build(db: Session, workspaces: list[Workspace], originals: dict[int, str], stamp: datetime) -> dict:
    """The whole import file for these workspaces (the exporting person's own)."""
    data, keys = [], set()
    for ws in workspaces:
        one, companies = workspace_data(db, ws, originals)
        data.append(one)
        keys |= companies
    ordered = sorted(keys)
    profiles = [p for start in range(0, len(ordered), 400)
                for p in db.query(CompanyProfile).filter(CompanyProfile.key.in_(ordered[start:start + 400]))]
    return {"format": FORMAT, "format_version": FORMAT_VERSION, "app_version": app_version(),
            "exported_at": iso(stamp), "workspaces": data,
            "companies": [company_data(p) for p in sorted(profiles, key=lambda p: p.key)]}


# ---------------------------------------------------------------------------
# Reading (the import): every field typed and bounded
# ---------------------------------------------------------------------------

SHORT = 2_000          # characters: names, labels, titles, short values
URL = 4_000
LONG = 2_000_000       # an ad, a document, a letter, a chat message
MANY = 100_000         # items in one list (jobs in a workspace, messages in a chat...)


def _naive_utc(value: Optional[datetime]) -> Optional[datetime]:
    if value is not None and value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


Short = Annotated[str, StringConstraints(max_length=SHORT)]
Long = Annotated[str, StringConstraints(max_length=LONG)]
Url = Annotated[str, StringConstraints(max_length=URL)]
When = Annotated[Optional[datetime], AfterValidator(_naive_utc)]
Lat = Annotated[float, Field(ge=-90.0, le=90.0)]
Lng = Annotated[float, Field(ge=-180.0, le=180.0)]
Ref = Annotated[int, Field(ge=1, le=10_000_000)]
Tags = Annotated[list[Short], Field(max_length=1_000)]


class _In(BaseModel):
    model_config = ConfigDict(extra="ignore")


class DealbreakersIn(_In):
    industries: Tags = Field(default_factory=list)
    keywords: Tags = Field(default_factory=list)


class StartingPointIn(_In):
    """A starting point stored as an object (the shape Phase C's search setup may store); today's import keeps its
    place as a "Commute from" station."""
    label: Optional[Short] = None
    place: Optional[Short] = None


class SearchSetupIn(_In):
    titles: Tags = Field(default_factory=list)
    keywords_include: Tags = Field(default_factory=list)
    keywords_exclude: Tags = Field(default_factory=list)
    salary_floor: Optional[Annotated[int, Field(ge=0, le=100_000_000)]] = None
    remote_aus_ok: bool = True
    remote_global_ok: bool = False
    allow_hybrid: bool = True
    max_office_days: Optional[Annotated[int, Field(ge=0, le=5)]] = None
    allow_onsite: bool = True
    seek_enabled: bool = True
    seek_locations: Tags = Field(default_factory=list)
    starting_points: Annotated[list[Short | StartingPointIn], Field(max_length=1_000)] = Field(default_factory=list)
    dealbreakers: Optional[DealbreakersIn] = None


class PinIn(_In):
    label: Short = ""
    kind: Literal["home", "hybrid", "onsite"]
    lat: Lat
    lng: Lng
    radius_km: Annotated[float, Field(gt=0, le=20_000)] = 25.0


class SourceIn(_In):
    ref: Optional[Ref] = None
    label: Optional[Short] = None
    url: Url
    type: Optional[Annotated[str, StringConstraints(max_length=40)]] = None
    enabled: bool = True


class ScheduleIn(schedule.ScheduleSchema):
    model_config = ConfigDict(extra="ignore")


class DocumentIn(_In):
    ref: Ref
    filename: Short
    filetype: Optional[Annotated[str, StringConstraints(max_length=20)]] = None
    kind: Optional[Literal["resume", "cover_letter", "preferences", "other"]] = None
    text: Optional[Long] = None
    concise_text: Optional[Long] = None
    use_concise: bool = False
    use_for_scoring: bool = True
    concise_model: Optional[Short] = None
    concise_at: When = None
    created_at: When = None
    updated_at: When = None
    summary_of: Annotated[list[Ref], Field(max_length=1_000)] = Field(default_factory=list)
    original: Optional[Annotated[str, StringConstraints(max_length=1_000)]] = None


class AdIn(_In):
    url: Optional[Url] = None
    source: Optional[Short] = None
    external_id: Optional[Short] = None
    company: Optional[Short] = None
    title: Optional[Short] = None
    location_text: Optional[Short] = None
    country: Optional[Annotated[str, StringConstraints(max_length=10)]] = None
    work_mode: Optional[Literal["remote", "hybrid", "onsite", "unknown"]] = None
    employment_type: Optional[Literal[EMPLOYMENT_TYPES]] = None
    office_days: Optional[Annotated[int, Field(ge=0, le=7)]] = None
    salary_min: Optional[Annotated[int, Field(ge=0, le=100_000_000)]] = None
    salary_max: Optional[Annotated[int, Field(ge=0, le=100_000_000)]] = None
    salary_text: Optional[Short] = None
    description: Optional[Long] = None
    description_hash: Optional[Annotated[str, StringConstraints(max_length=200)]] = None
    detail_status: Optional[Literal["none", "summary", "full"]] = None
    detail_fetched_at: When = None
    posted_at: Optional[Short] = None
    industry: Optional[Short] = None


class OfficeIn(_In):
    text: Optional[Short] = None
    lat: Optional[Lat] = None
    lng: Optional[Lng] = None
    source: Optional[Literal["ad", "company", "user"]] = None
    checked_at: When = None


class FitIn(_In):
    status: Literal["ok", "error"] = "ok"
    score: Optional[Annotated[int, Field(ge=0, le=100)]] = None
    evidence: dict[str, Any] = Field(default_factory=dict)
    error: Optional[Short] = None
    profile_hash: Optional[Annotated[str, StringConstraints(max_length=200)]] = None
    model: Optional[Short] = None
    created_at: When = None


class LetterIn(_In):
    text: Long = ""
    instructions: Optional[Long] = None
    edited: bool = False
    model: Optional[Short] = None
    reasoning_effort: Optional[Annotated[str, StringConstraints(max_length=40)]] = None
    created_at: When = None
    updated_at: When = None


class JobIn(_In):
    ref: Ref
    origin: Literal["pool", "private"] = "private"
    ad: AdIn = Field(default_factory=AdIn)
    own: Annotated[list[Literal[JOB_CONTENT]], Field(max_length=len(JOB_CONTENT))] = Field(default_factory=list)
    status: Literal[STATUSES] = "to_review"
    hidden: bool = False
    excluded_reason: Optional[Short] = None
    first_seen: When = None
    last_seen: When = None
    closed_at: When = None
    location_verdict: Optional[Literal["ok", "too_far"]] = None
    office: Optional[OfficeIn] = None
    lat: Optional[Lat] = None
    lng: Optional[Lng] = None
    distance_km: Optional[Annotated[float, Field(ge=0, le=50_000)]] = None
    duplicate_of: Optional[Ref] = None
    source: Optional[Ref] = None
    fit: Optional[FitIn] = None
    cover_letter: Optional[LetterIn] = None


class PageIn(_In):
    title: Optional[Short] = None
    url: Optional[Url] = None


class MessageIn(_In):
    role: Literal["user", "assistant"]
    content: Long = ""
    sources: Optional[Annotated[list[PageIn], Field(max_length=200)]] = None
    web_search: bool = False
    model: Optional[Short] = None
    created_at: When = None


class ChatIn(_In):
    title: Optional[Short] = None
    created_at: When = None
    updated_at: When = None
    jobs: Annotated[list[Ref], Field(max_length=1_000)] = Field(default_factory=list)
    messages: Annotated[list[MessageIn], Field(max_length=MANY)] = Field(default_factory=list)


class WorkspaceIn(_In):
    name: Short
    created_at: When = None
    search_setup: Optional[SearchSetupIn] = None
    pins: Annotated[list[PinIn], Field(max_length=10_000)] = Field(default_factory=list)
    sources: Annotated[list[SourceIn], Field(max_length=MANY)] = Field(default_factory=list)
    schedule: Optional[ScheduleIn] = None
    documents: Annotated[list[DocumentIn], Field(max_length=MANY)] = Field(default_factory=list)
    jobs: Annotated[list[JobIn], Field(max_length=MANY)] = Field(default_factory=list)
    chats: Annotated[list[ChatIn], Field(max_length=MANY)] = Field(default_factory=list)


Json = Optional[list | dict]


class CompanyIn(_In):
    key: Short
    name: Short
    status: Annotated[str, StringConstraints(max_length=20)] = "done"
    official_name: Optional[Short] = None
    is_recruiter: bool = False
    business_model: Optional[Long] = None
    ownership: Optional[Long] = None
    headquarters: Optional[Short] = None
    controversies: Json = None
    controversy_note: Optional[Long] = None
    news_researched_at: When = None
    sources: Json = None
    employee_count: Optional[Short] = None
    glassdoor_url: Optional[Url] = None
    sentiment: Json = None
    sentiment_researched_at: When = None
    research_version: Annotated[int, Field(ge=1, le=1_000)] = 1
    offices: Json = None
    offices_checked_at: When = None
    offices_cities: Json = None
    unverified_dropped: Annotated[int, Field(ge=0, le=1_000_000)] = 0
    error: Optional[Long] = None
    model: Optional[Short] = None
    researched_at: When = None


class ImportFile(_In):
    format: Literal["jobhuntpa-import"]
    format_version: Annotated[int, Field(ge=1)]
    app_version: Optional[Annotated[str, StringConstraints(max_length=40)]] = None
    exported_at: When = None
    workspaces: Annotated[list[WorkspaceIn], Field(max_length=1_000)]
    companies: Annotated[list[CompanyIn], Field(max_length=MANY)] = Field(default_factory=list)

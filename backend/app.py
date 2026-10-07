"""FastAPI application.

Run with:  uvicorn backend.app:app --host 127.0.0.1 --port 8000
"""
from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal, Optional

from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from starlette.formparsers import MultiPartParser

from backend import config  # noqa: F401  (loads .env first)
from backend import crypto, llm_providers, migrate_encryption, schedule, tasks, users
from backend.account import router as account_router
from backend.account_email import router as account_email_router
from backend.admin import router as admin_router
from backend.app_settings import router as app_settings_router
from backend.email_settings import router as email_settings_router
from backend.password_reset import router as password_reset_router
from backend.geo_lookup import router as geo_router
from backend.auth import AuthProblem
from backend.auth import router as auth_router
from backend.security import router as security_router
from backend.background import router as background_router
from backend.db import CompanyProfile, CoverLetter, FitResult, Job, SearchRun, SessionLocal, company_key, get_db, init_db
from backend.cover_letters import router as cover_letters_router
from backend.docs import router as docs_router
from backend.export import router as export_router
from backend.commute import router as commute_router
from backend.commute import status_router as commute_status_router
from backend.duplicates import router as duplicates_router
from backend.job_chat import router as chat_router
from backend.filters import softened_job_ids
from backend.llm_settings import router as llm_router
from backend.offices import looks_like_recruiter, office_unknown
from backend.offices import router as offices_router
from backend.title_suggestions import router as titles_router
from backend.profile import router as profile_router
from backend.sessions import AuthMiddleware, AuthUser, current_user, require_admin
from backend.sources import router as sources_router
from backend.workspaces import current_workspace, owned
from backend.workspaces import router as workspaces_router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)

# Log hygiene: URLs and queries can hold search terms, place names and (later) tokens.
for _name in ("httpx", "httpcore"):  # they log every request URL at INFO
    logging.getLogger(_name).setLevel(logging.WARNING)


class StripQueryString(logging.Filter):
    """uvicorn's access log without the query string of the request line."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str):
            record.args = (*args[:2], args[2].split("?", 1)[0], *args[3:])
        return True


logging.getLogger("uvicorn.access").addFilter(StripQueryString())

# Uploads (a CV, a saved job page) stay in memory while a request reads them: Starlette would otherwise spool any
# file over 1 MB to a plaintext temporary file on disk before backend.docs seals it (accounts design §2.2).
MultiPartParser.spool_max_size = 24 * 1024 * 1024


class NoStoreApi:
    """Adds ``Cache-Control: no-store`` to every ``/api`` response, so nothing of it stays in a browser cache."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope.get("path", "").startswith("/api"):
            return await self.app(scope, receive, send)

        async def send_no_store(message):
            if message["type"] == "http.response.start":
                headers = [(k, v) for k, v in message.get("headers", []) if k.lower() != b"cache-control"]
                headers.append((b"cache-control", b"no-store"))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_no_store)


FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
STATUSES = ("to_review", "shortlisted", "applied", "interviewing", "rejected", "not_interested")
Status = Literal["to_review", "shortlisted", "applied", "interviewing", "rejected", "not_interested"]


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    # The server key file, then the first admin from the environment or the first-run token (backend.users).
    # A missing or wrong key file stops start-up here, with what to do.
    users.startup()
    # An encryption run the last process left running waits for its admin's next sign-in; after it, strict mode.
    migrate_encryption.at_startup()
    tasks.mark_interrupted()
    schedule.start_scheduler(lambda ws: start_search_run(ws, scheduled=True))
    yield
    schedule.stop_scheduler()


app = FastAPI(title="JobHuntPA", lifespan=lifespan)
# The job list is several MB of JSON; compressed it is about a seventh of that (slow links, the Pi).
app.add_middleware(GZipMiddleware, minimum_size=2048, compresslevel=5)
# Sign-in, CSRF, same-site checks and the request's key scope (backend/sessions.py); inside NoStoreApi, so its
# refusals aren't cached either.
app.add_middleware(AuthMiddleware)
app.add_middleware(NoStoreApi)
app.include_router(auth_router)
app.include_router(security_router)
app.include_router(password_reset_router)
app.include_router(account_email_router)
app.include_router(account_router)
app.include_router(export_router)
app.include_router(admin_router)
app.include_router(background_router)
app.include_router(geo_router)
app.include_router(titles_router)  # before docs_router: /suggest-titles must not match /{doc_id}
app.include_router(docs_router)
app.include_router(profile_router)
app.include_router(sources_router)
app.include_router(workspaces_router)
app.include_router(llm_router)
app.include_router(app_settings_router)
app.include_router(email_settings_router)
app.include_router(cover_letters_router)
app.include_router(duplicates_router)
app.include_router(chat_router)
app.include_router(offices_router)
app.include_router(commute_router)
app.include_router(commute_status_router)
app.include_router(schedule.router)
app.include_router(migrate_encryption.router)


@app.exception_handler(AuthProblem)
def auth_problem(_request: Request, exc: AuthProblem):
    return exc.response()


@app.exception_handler(crypto.KeyMismatch)
def not_yours(_request: Request, exc: crypto.KeyMismatch):
    """A sealed value that doesn't open under the request's key: another user's row (a slip past the workspace
    checks), or one that was moved or changed in the database. Either way it doesn't exist for this user: 404, the
    same as a row that isn't there (accounts design §2.1 and §4)."""
    log.warning("A sealed value didn't open under the request's key (%s)", exc.__class__.__name__)
    return JSONResponse({"detail": "Not found"}, status_code=404)


@app.get("/api/health")
def health():
    return {"ok": True}


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------

def _fit_dict(fit: Optional[FitResult], profile_hash: str) -> Optional[dict]:
    if fit is None:
        return None
    data = json.loads(fit.evidence_json or "{}") if fit.status == "ok" else {}
    return {
        "status": fit.status,
        "score": fit.score,
        "requirements": data.get("requirements", []),
        "gaps": data.get("gaps", []),
        "summary": data.get("summary", ""),
        "error": fit.error,
        "stale": fit.status == "ok" and fit.profile_hash != profile_hash,
        "model": fit.model,
        "scored_at": fit.created_at.isoformat() if fit.created_at else None,
    }


def job_to_dict(job: Job, fit: Optional[FitResult], profile_hash: str, *, full: bool = False,
                has_letter: bool = False, recruiter: bool = False) -> dict:
    out = {
        "id": job.id,
        "source": job.source,
        "company": job.company,
        "company_key": company_key(job.company or ""),
        "title": job.title,
        "location_text": job.location_text,
        "country": job.country,
        "lat": job.lat,
        "lng": job.lng,
        "work_mode": job.work_mode or "unknown",
        "employment_type": job.employment_type,
        "office_days": job.office_days,
        "salary_text": job.salary_text,
        "salary_min": job.salary_min,
        "salary_max": job.salary_max,
        "url": job.url,
        "distance_km": job.distance_km,
        "status": job.status,
        "excluded_reason": job.excluded_reason,
        "detail_status": job.detail_status,
        "posted_at": job.posted_at,
        "first_seen": job.first_seen.isoformat() if job.first_seen else None,
        "last_seen": job.last_seen.isoformat() if job.last_seen else None,
        "closed": job.closed_at is not None,
        "hidden": bool(job.hidden),
        "fit": _fit_dict(fit, profile_hash),
        "has_cover_letter": has_letter,
        "office_text": job.office_text,
        "office_source": job.office_source,
        "office_placed": job.office_lat is not None,
        "office_unknown": office_unknown(job),
        "location_verdict": job.location_verdict,
        "recruiter": recruiter,
    }
    if full:
        out["description"] = job.description or ""
    return out


def _profile_hash(db: Session, ws: int) -> str:
    from backend.scorer import build_profile

    return build_profile(db, ws)[1]


# Temporarily raise the hybrid office-days limit by this many days (Jobs tab
# what-if). Nothing is saved; jobs it lets through carry "softened_reason".
OfficeSlack = Query(0, ge=0, le=5)


def _soften(out: dict, softened: set[int]) -> dict:
    if out["id"] in softened:
        out["softened_reason"], out["excluded_reason"] = out["excluded_reason"], None
    return out


def _with_copies(out: dict, copies: list[Job]) -> dict:
    """Merged duplicates: the same ad on other sites. The job counts as
    closed only once every copy is."""
    out["also_on"] = [{"id": c.id, "source": c.source, "url": c.url, "title": c.title, "company": c.company,
                       "closed": c.closed_at is not None, "detail_status": c.detail_status} for c in copies]
    if out["closed"] and any(c.closed_at is None for c in copies):
        out["closed"] = False
    return out


@app.get("/api/jobs")
def list_jobs(office_slack: int = OfficeSlack,
              include: str = Query("", pattern=r"^((excluded|hidden)(,(excluded|hidden))?)?$"),
              db: Session = Depends(get_db), ws: int = Depends(current_workspace)):
    """The workspace's jobs (merged copies under ``also_on``). Excluded jobs (by the filters) and
    hidden ones (by the user) are most of them and only sent on demand: ``include=excluded``,
    ``include=hidden`` or both, which the Jobs tab asks for while "Show excluded" / "Show hidden"
    is ticked. ``X-Omitted-Count`` says how many were left out. A job the office-days what-if lets
    through isn't excluded."""
    from backend.pool import jobs_query

    phash = _profile_hash(db, ws)
    softened = softened_job_ids(db, ws, office_slack)
    leave_out = {"excluded", "hidden"} - set(include.split(","))
    # Which jobs are left out is decided in Python from two small columns: hidden and excluded_reason are
    # sealed so that not even whether they're set shows (accounts design §14.6). Only the listed jobs are then
    # loaded in full. A job the what-if lets through isn't excluded.
    states = (db.query(Job.id, Job.hidden, Job.excluded_reason)
              .filter(Job.workspace_id == ws, Job.duplicate_of.is_(None)).all())
    listed_ids = sorted(i for i, hidden, reason in states
                        if not ("excluded" in leave_out and reason and i not in softened)
                        and not ("hidden" in leave_out and hidden))
    omitted_count = len(states) - len(listed_ids)
    listed: list[tuple[Job, Optional[FitResult]]] = []
    copies: dict[int, list[Job]] = {}  # merged copies of the listed jobs, whatever their own state
    for start in range(0, len(listed_ids), 500):
        chunk = listed_ids[start:start + 500]
        fits = {f.job_id: f for f in db.query(FitResult).filter(FitResult.job_id.in_(chunk))}
        listed += [(job, fits.get(job.id))
                   for job in jobs_query(db, card=True).filter(Job.id.in_(chunk)).order_by(Job.id)]
        for copy in (jobs_query(db, card=True).filter(Job.workspace_id == ws, Job.duplicate_of.in_(chunk))
                     .order_by(Job.id)):
            copies.setdefault(copy.duplicate_of, []).append(copy)
    letters = {j for (j,) in db.query(CoverLetter.job_id).join(Job, Job.id == CoverLetter.job_id)
               .filter(Job.workspace_id == ws)}
    profiles = {p.key: p for p in db.query(CompanyProfile).all()}
    # Plain JSON values already: skip FastAPI's per-field encoder, which took most of the time for
    # thousands of jobs on a Raspberry Pi.
    return JSONResponse([_with_copies(_soften(job_to_dict(job, fit, phash, has_letter=job.id in letters,
                                                         recruiter=looks_like_recruiter(job.company or "", profiles)),
                                              softened),
                                      copies.get(job.id, []))
                         for job, fit in listed],
                        headers={"X-Omitted-Count": str(omitted_count)})


@app.get("/api/jobs/{job_id}")
def get_job(job_id: int, office_slack: int = OfficeSlack, db: Session = Depends(get_db),
            ws: int = Depends(current_workspace)):
    from backend.offices import candidates

    job = owned(db, Job, job_id, ws, "Job")
    fit = db.query(FitResult).filter(FitResult.job_id == job_id).first()
    profiles = {p.key: p for p in db.query(CompanyProfile).filter(CompanyProfile.key == company_key(job.company or ""))}
    letter = db.query(CoverLetter.id).filter(CoverLetter.job_id == job_id).first() is not None
    out = _soften(job_to_dict(job, fit, _profile_hash(db, ws), full=True, has_letter=letter,
                              recruiter=looks_like_recruiter(job.company or "", profiles)),
                  softened_job_ids(db, ws, office_slack))
    out["office_candidates"] = candidates(db, job) if out["office_unknown"] or job.office_source == "company" else []
    return _with_copies(out, db.query(Job).filter(Job.duplicate_of == job_id).all())


class StatusUpdate(BaseModel):
    status: Status


class BulkStatusUpdate(StatusUpdate):
    ids: list[int]


@app.patch("/api/jobs/status")
def bulk_update_status(payload: BulkStatusUpdate, db: Session = Depends(get_db),
                       ws: int = Depends(current_workspace)):
    n = (db.query(Job).filter(Job.workspace_id == ws, Job.id.in_(payload.ids))
         .update({Job.status: payload.status}, synchronize_session=False))
    db.commit()
    return {"ok": True, "updated": n}


class HiddenUpdate(BaseModel):
    ids: list[int]
    hidden: bool


@app.patch("/api/jobs/hidden")
def set_hidden(payload: HiddenUpdate, db: Session = Depends(get_db), ws: int = Depends(current_workspace)):
    """Hide (or unhide) jobs. Hidden jobs stay hidden when their source lists them
    again, and are skipped by scoring and company research."""
    n = (db.query(Job).filter(Job.workspace_id == ws, Job.id.in_(payload.ids))
         .update({Job.hidden: payload.hidden}, synchronize_session=False))
    db.commit()
    return {"ok": True, "updated": n, "hidden": payload.hidden}


@app.patch("/api/jobs/{job_id}/status")
def update_status(job_id: int, payload: StatusUpdate, db: Session = Depends(get_db),
                  ws: int = Depends(current_workspace)):
    job = owned(db, Job, job_id, ws, "Job")
    job.status = payload.status
    db.commit()
    return {"ok": True, "id": job_id, "status": job.status}


@app.post("/api/jobs/refilter")
def refilter_jobs(db: Session = Depends(get_db), ws: int = Depends(current_workspace)):
    """Re-apply the current filters to every stored job (no network)."""
    from backend.filters import load_criteria
    from backend.pipeline import refilter

    return {"changed": refilter(db, load_criteria(db, ws), ws)}


# ---------------------------------------------------------------------------
# Background runs
# ---------------------------------------------------------------------------

def _score(ws: int, mode: str, progress, office_slack: int = 0) -> dict:
    from backend.scorer import ScorerError, build_profile, score_jobs, select_jobs

    db = SessionLocal()
    try:
        ids = select_jobs(db, mode, build_profile(db, ws)[1], ws, office_slack)
    finally:
        db.close()
    try:
        return score_jobs(ws, ids, progress)
    except ScorerError as exc:
        return {"aborted": llm_providers.user_message(exc)}


def start_search_run(ws: int, scheduled: bool = False) -> dict:
    """Run search: fetch new jobs, merge duplicate ads, score what's new.
    Also started by backend.schedule at the scheduled times."""
    from backend.pipeline import run_search
    from backend.scorer import config_problem

    def job(progress):
        from backend.duplicates import scan_workspace

        summary = {"search": run_search(ws, progress)}
        # Before scoring, so the same ad on two sites is scored once.
        progress("checking for duplicate ads", {})
        summary["duplicates"] = scan_workspace(ws)
        summary["scoring"] = {"skipped": config_problem()} if config_problem() else _score(ws, "pending", progress)
        return summary

    return tasks.start("search", job, ws, scheduled=scheduled)


@app.post("/api/search/run")
def start_search(ws: int = Depends(current_workspace)):
    return start_search_run(ws)


class ScoreRunRequest(BaseModel):
    mode: Literal["pending", "stale", "all"] = "pending"
    office_slack: int = Field(0, ge=0, le=5)


@app.post("/api/score/run")
def start_scoring(payload: ScoreRunRequest, ws: int = Depends(current_workspace)):
    from backend.scorer import config_problem

    if config_problem():
        raise HTTPException(status_code=400, detail=config_problem())
    return tasks.start("score", lambda progress: {"scoring": _score(ws, payload.mode, progress, payload.office_slack)}, ws)


@app.get("/api/runs/latest")
def latest_run(db: Session = Depends(get_db), ws: int = Depends(current_workspace)):
    run = db.query(SearchRun).filter(SearchRun.workspace_id == ws).order_by(SearchRun.id.desc()).first()
    return run.to_dict() if run else None


@app.get("/api/runs/{run_id}")
def get_run(run_id: int, db: Session = Depends(get_db), ws: int = Depends(current_workspace)):
    return owned(db, SearchRun, run_id, ws, "Run").to_dict()


# ---------------------------------------------------------------------------
# Scoring (single job, status, connection test)
# ---------------------------------------------------------------------------

@app.post("/api/score/test")
def test_scorer(_admin: AuthUser = Depends(require_admin)):
    from backend.scorer import test_connection

    return test_connection()


# What /api/score/status tells someone who isn't an admin about the LLM: whether AI features work and what they
# can do, never the provider, address, model or reasoning level (accounts design §14.8).
NOT_SET_UP = "AI features aren't set up yet. Ask your admin."


def llm_status_for_user(llm: dict) -> dict:
    return {"configured": llm["configured"], "problem": None if llm["configured"] else NOT_SET_UP,
            "capabilities": {"web_search": bool(llm["capabilities"].get("web_search"))}}


@app.get("/api/score/status")
def scorer_status(office_slack: int = OfficeSlack, db: Session = Depends(get_db),
                  ws: int = Depends(current_workspace), user: AuthUser = Depends(current_user)):
    from backend.scorer import build_profile, public_config, select_jobs

    llm = public_config() if user.is_admin else llm_status_for_user(public_config())
    text, phash = build_profile(db, ws)
    pending = len(select_jobs(db, "pending", phash, ws))
    softened_pending = len(select_jobs(db, "pending", phash, ws, office_slack)) - pending if office_slack else 0
    # Counted in Python: a fit's status is sealed (accounts design §14.6).
    statuses = [s for (s,) in db.query(FitResult.status).join(Job, Job.id == FitResult.job_id).filter(Job.workspace_id == ws)]
    return {
        **llm,
        "profile_chars": len(text),
        "pending": pending,
        "softened_pending": softened_pending,
        "stale": len(select_jobs(db, "stale", phash, ws)) - pending,
        "scored": statuses.count("ok"),
        "errors": statuses.count("error"),
    }


@app.post("/api/score/{job_id}")
def score_single(job_id: int, office_slack: int = OfficeSlack, db: Session = Depends(get_db),
                 ws: int = Depends(current_workspace)):
    from backend.scorer import ScorerError, build_profile, config_problem, get_config, score_one

    cfg = {**get_config("scoring"), "workspace_id": ws}
    if config_problem(cfg):
        raise HTTPException(status_code=400, detail=config_problem(cfg))
    owned(db, Job, job_id, ws, "Job")
    text, phash = build_profile(db, ws)
    try:
        score_one(job_id, text, phash, cfg)
    except ScorerError as exc:
        raise HTTPException(status_code=502, detail=llm_providers.user_message(exc))
    db.expire_all()
    return get_job(job_id, office_slack, db, ws)


# ---------------------------------------------------------------------------
# Company profiles (LLM web-search research agents; shared by all workspaces)
# ---------------------------------------------------------------------------

@app.get("/api/companies")
def list_companies(db: Session = Depends(get_db), ws: int = Depends(current_workspace)):
    """Profiles of the companies in this workspace's jobs (the profiles are shared, but which companies
    others researched is not for this workspace to see). Any workspace with a company's job gets its cached
    profile, whoever researched it."""
    from backend.db import Posting

    names = (db.query(Job._company, Posting.company).outerjoin(Posting, Posting.id == Job.posting_id)
             .filter(Job.workspace_id == ws))
    keys = {company_key(own or shared or "") for own, shared in names}  # a job's own name, else the posting's
    return {p.key: p.to_dict() for p in db.query(CompanyProfile).all() if p.key in keys}


class ResearchRequest(BaseModel):
    mode: Literal["missing", "all"] = "missing"
    name: Optional[str] = None           # research just this company
    job_ids: Optional[list[int]] = None  # only these jobs' companies (the ticked jobs)


@app.post("/api/companies/research")
def start_research(payload: ResearchRequest, ws: int = Depends(current_workspace)):
    from backend.research import companies_to_research, job_context, research_companies
    from backend.scorer import ScorerError, config_problem

    if config_problem():
        raise HTTPException(status_code=400, detail=config_problem())

    def job(progress):
        db = SessionLocal()
        try:
            if payload.name:  # "research this company": only the parts that are due (research.plan)
                companies = [job_context(db, ws, payload.name)]
            else:
                companies = companies_to_research(db, ws, payload.mode, payload.job_ids)
        finally:
            db.close()
        progress(f"researching {len(companies)} companies", {"done": 0, "total": len(companies)})
        try:
            summary = {"research": research_companies(companies, progress)}
        except ScorerError as exc:
            return {"research": {"aborted": llm_providers.user_message(exc)}}
        if payload.name:  # one company: place its jobs at the offices research found
            from backend.offices import assign_company

            summary["offices"] = {"from_ads": 0, "from_companies": assign_company(ws, payload.name),
                                  "unknown": 0, "recruiters": 0}
        if not payload.name and not summary["research"].get("aborted"):
            # Fill missing info also finds offices for ads that only name a city.
            from backend.offices import find_offices

            try:
                summary["offices"] = find_offices(ws, payload.job_ids, progress)
            except ScorerError as exc:
                summary["offices"] = {"aborted": llm_providers.user_message(exc)}
        return summary

    return tasks.start("research", job, ws)


# ---------------------------------------------------------------------------
# Import pages saved from the user's own browser (e.g. SEEK)
# ---------------------------------------------------------------------------

@app.post("/api/import/page")
async def import_page(file: UploadFile = File(...), ws: int = Depends(current_workspace)):
    """Jobs from a saved SEEK search or job page, or any page with JobPosting data. They're the user's own:
    jobs the shared pool doesn't have stay private to this workspace (backend.pool.import_private)."""
    from backend.adapters.seek import parse_seek_page
    from backend.pipeline import import_postings
    from backend.scraping.jsonld import find_job_postings, posting_from_jsonld

    from backend.scraping.saved_pages import decode_saved_page

    raw = decode_saved_page(await file.read())
    postings = parse_seek_page(raw)
    source = "seek"
    if not postings:
        postings = [p for p in (posting_from_jsonld(n, "") for n in find_job_postings(raw)) if p and p.url]
        source = "import"
    if not postings:
        raise HTTPException(status_code=422, detail="No job data found. Save a SEEK search/job page, or a page with JobPosting data.")
    from backend.duplicates import scan_workspace

    result = import_postings(postings, source, ws)
    result["duplicates"] = scan_workspace(ws)
    return result


@app.post("/api/jobs/{job_id}/import-page")
async def import_job_page(job_id: int, file: UploadFile = File(...), office_slack: int = OfficeSlack,
                          db: Session = Depends(get_db), ws: int = Depends(current_workspace)):
    """Update one SEEK job with the full ad from its page, saved in the user's browser. The full ad is this
    workspace's own: other workspaces with the same posting keep reading the shared summary."""
    from backend.adapters.seek import parse_job_page, parse_search_page
    from backend.pipeline import update_job_from_posting
    from backend.scraping.saved_pages import decode_saved_page

    job = owned(db, Job, job_id, ws, "Job")
    raw = decode_saved_page(await file.read())
    posting = parse_job_page(raw)
    if posting is None:
        if parse_search_page(raw)[0]:
            raise HTTPException(status_code=422, detail="That's a SEEK search results page. Open the job itself on SEEK, then save that page.")
        raise HTTPException(status_code=422, detail="No SEEK job found in that file. Save the job's page from SEEK (Ctrl+S / Cmd+S) and upload it.")
    if posting.external_id != (job.external_id or "") and posting.url != job.url:
        raise HTTPException(status_code=422, detail=(
            f"That page is a different SEEK job (\"{posting.title}\" at {posting.company or 'unknown'}), "
            f"not \"{job.title}\". Open this job's link and save that page."))
    if not posting.description:
        raise HTTPException(status_code=422, detail="The saved page has no job description. Try saving it again once the ad has fully loaded.")
    update_job_from_posting(db, job, posting)
    return get_job(job_id, office_slack, db, ws)


if FRONTEND_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")

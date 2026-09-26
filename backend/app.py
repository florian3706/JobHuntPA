"""FastAPI application.

Run with:  uvicorn backend.app:app --host 127.0.0.1 --port 8000
"""
from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal, Optional

from fastapi import Depends, FastAPI, File, HTTPException, UploadFile
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy.orm import Session

from backend import config  # noqa: F401  (loads .env first)
from backend import tasks
from backend.db import CompanyProfile, FitResult, Job, SearchRun, SessionLocal, company_key, get_db, init_db
from backend.docs import router as docs_router
from backend.profile import router as profile_router
from backend.sources import router as sources_router
from backend.workspaces import current_workspace, owned
from backend.workspaces import router as workspaces_router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
STATUSES = ("to_review", "applied", "shortlisted", "not_interested")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    tasks.mark_interrupted()
    yield


app = FastAPI(title="JobHuntPA", lifespan=lifespan)
app.include_router(docs_router)
app.include_router(profile_router)
app.include_router(sources_router)
app.include_router(workspaces_router)


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


def job_to_dict(job: Job, fit: Optional[FitResult], profile_hash: str, *, full: bool = False) -> dict:
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
        "fit": _fit_dict(fit, profile_hash),
    }
    if full:
        out["description"] = job.description or ""
    return out


def _profile_hash(db: Session, ws: int) -> str:
    from backend.scorer import build_profile

    return build_profile(db, ws)[1]


@app.get("/api/jobs")
def list_jobs(db: Session = Depends(get_db), ws: int = Depends(current_workspace)):
    phash = _profile_hash(db, ws)
    rows = (db.query(Job, FitResult).outerjoin(FitResult, FitResult.job_id == Job.id)
            .filter(Job.workspace_id == ws).all())
    return [job_to_dict(job, fit, phash) for job, fit in rows]


@app.get("/api/jobs/{job_id}")
def get_job(job_id: int, db: Session = Depends(get_db), ws: int = Depends(current_workspace)):
    job = owned(db, Job, job_id, ws, "Job")
    fit = db.query(FitResult).filter(FitResult.job_id == job_id).first()
    return job_to_dict(job, fit, _profile_hash(db, ws), full=True)


class StatusUpdate(BaseModel):
    status: Literal["to_review", "applied", "shortlisted", "not_interested"]


class BulkStatusUpdate(StatusUpdate):
    ids: list[int]


@app.patch("/api/jobs/status")
def bulk_update_status(payload: BulkStatusUpdate, db: Session = Depends(get_db),
                       ws: int = Depends(current_workspace)):
    n = (db.query(Job).filter(Job.workspace_id == ws, Job.id.in_(payload.ids))
         .update({Job.status: payload.status}, synchronize_session=False))
    db.commit()
    return {"ok": True, "updated": n}


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

def _score(ws: int, mode: str, progress) -> dict:
    from backend.scorer import ScorerError, build_profile, score_jobs, select_jobs

    db = SessionLocal()
    try:
        ids = select_jobs(db, mode, build_profile(db, ws)[1], ws)
    finally:
        db.close()
    try:
        return score_jobs(ws, ids, progress)
    except ScorerError as exc:
        return {"aborted": str(exc)}


@app.post("/api/search/run")
def start_search(ws: int = Depends(current_workspace)):
    from backend.pipeline import run_search
    from backend.scorer import config_problem

    def job(progress):
        summary = {"search": run_search(ws, progress)}
        summary["scoring"] = {"skipped": config_problem()} if config_problem() else _score(ws, "pending", progress)
        return summary

    return tasks.start("search", job, ws)


class ScoreRunRequest(BaseModel):
    mode: Literal["pending", "stale", "all"] = "pending"


@app.post("/api/score/run")
def start_scoring(payload: ScoreRunRequest, ws: int = Depends(current_workspace)):
    from backend.scorer import config_problem

    if config_problem():
        raise HTTPException(status_code=400, detail=config_problem())
    return tasks.start("score", lambda progress: {"scoring": _score(ws, payload.mode, progress)}, ws)


@app.get("/api/runs/latest")
def latest_run(db: Session = Depends(get_db), ws: int = Depends(current_workspace)):
    run = db.query(SearchRun).filter(SearchRun.workspace_id == ws).order_by(SearchRun.id.desc()).first()
    return run.to_dict() if run else None


@app.get("/api/runs/{run_id}")
def get_run(run_id: int, db: Session = Depends(get_db)):
    run = db.get(SearchRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return run.to_dict()


# ---------------------------------------------------------------------------
# Scoring (single job, status, connection test)
# ---------------------------------------------------------------------------

@app.post("/api/score/test")
def test_scorer():
    from backend.scorer import test_connection

    return test_connection()


@app.get("/api/score/status")
def scorer_status(db: Session = Depends(get_db), ws: int = Depends(current_workspace)):
    from backend.scorer import build_profile, public_config, select_jobs

    text, phash = build_profile(db, ws)
    pending = len(select_jobs(db, "pending", phash, ws))
    fits = db.query(FitResult.status).join(Job, Job.id == FitResult.job_id).filter(Job.workspace_id == ws)
    return {
        **public_config(),
        "profile_chars": len(text),
        "pending": pending,
        "stale": len(select_jobs(db, "stale", phash, ws)) - pending,
        "scored": fits.filter(FitResult.status == "ok").count(),
        "errors": fits.filter(FitResult.status == "error").count(),
    }


@app.post("/api/score/{job_id}")
def score_single(job_id: int, db: Session = Depends(get_db), ws: int = Depends(current_workspace)):
    from backend.scorer import ScorerError, build_profile, config_problem, get_config, score_one

    cfg = get_config()
    if config_problem(cfg):
        raise HTTPException(status_code=400, detail=config_problem(cfg))
    owned(db, Job, job_id, ws, "Job")
    text, phash = build_profile(db, ws)
    try:
        score_one(job_id, text, phash, cfg)
    except ScorerError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    db.expire_all()
    return get_job(job_id, db, ws)


# ---------------------------------------------------------------------------
# Company profiles (LLM web-search research agents; shared by all workspaces)
# ---------------------------------------------------------------------------

@app.get("/api/companies")
def list_companies(db: Session = Depends(get_db)):
    return {p.key: p.to_dict() for p in db.query(CompanyProfile).all()}


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
            if payload.name:
                companies = [job_context(db, ws, payload.name)]
            else:
                companies = companies_to_research(db, ws, payload.mode, payload.job_ids)
        finally:
            db.close()
        progress(f"researching {len(companies)} companies", {"done": 0, "total": len(companies)})
        try:
            return {"research": research_companies(companies, progress)}
        except ScorerError as exc:
            return {"research": {"aborted": str(exc)}}

    return tasks.start("research", job, ws)


# ---------------------------------------------------------------------------
# Import pages saved from the user's own browser (e.g. SEEK)
# ---------------------------------------------------------------------------

@app.post("/api/import/page")
async def import_page(file: UploadFile = File(...), ws: int = Depends(current_workspace)):
    from backend.adapters.seek import parse_seek_page
    from backend.pipeline import import_postings
    from backend.scraping.jsonld import find_job_postings, posting_from_jsonld

    raw = (await file.read()).decode("utf-8", errors="replace")
    postings = parse_seek_page(raw)
    source = "seek"
    if not postings:
        postings = [p for p in (posting_from_jsonld(n, "") for n in find_job_postings(raw)) if p and p.url]
        source = "import"
    if not postings:
        raise HTTPException(status_code=422, detail="No job data found. Save a SEEK search/job page, or a page with JobPosting data.")
    return import_postings(postings, source, ws)


if FRONTEND_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")

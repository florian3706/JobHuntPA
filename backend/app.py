"""FastAPI application: CORS, API routers, static frontend, DB init."""
from __future__ import annotations

import json
import logging
import shutil
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

from fastapi import Body, Depends, FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import or_
from sqlalchemy.orm import Session

from .db import Document, FitResult, Job, get_db, init_db
from .docs import ALLOWED_EXTENSIONS, UPLOAD_DIR, _doc_to_dict, extract_text
from .docs import router as docs_router

# Import profile models FIRST so Base.metadata includes them before init_db().
# (profile.py registers UserProfile/DealbreakerSet/Pin on backend.db.Base.)
from .profile import (
    DealbreakersSchema,
    ProfileSchema,
    get_dealbreakers,
    get_profile,
    save_dealbreakers,
    save_profile,
)
from .profile import router as profile_router

# Company sources (registers CompanySource on Base before init_db()).
try:
    from .sources import ensure_company_sources_schema  # noqa: F401
    from .sources import router as sources_router
except Exception:  # pragma: no cover - router optional
    ensure_company_sources_schema = None  # type: ignore[assignment]
    sources_router = None  # type: ignore[assignment]

log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FRONTEND_DIR = PROJECT_ROOT / "frontend"

VALID_STATUSES = {"to_review", "applied", "shortlisted", "not_interested"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    try:
        if ensure_company_sources_schema is not None:
            ensure_company_sources_schema()
    except Exception:
        pass
    yield


app = FastAPI(title="JobHuntPA", lifespan=lifespan)
# Ensure tables exist even if lifespan is skipped (e.g. tests without
# lifespan, import-time checks). Lifespan also calls init_db().
# profile_router import above registered UserProfile/DealbreakerSet/Pin.
# sources import above registered CompanySource.
init_db()
try:
    if ensure_company_sources_schema is not None:
        ensure_company_sources_schema()
except Exception:
    pass
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(docs_router)
app.include_router(profile_router)

# Sources router BEFORE static mount (and before score router) so
# /api/sources/* never falls through to the frontend.
try:
    if sources_router is not None:
        app.include_router(sources_router)
except Exception as exc:  # pragma: no cover
    log.warning("sources router not mounted: %s", exc)

# Score router (POST /api/score/{job_id}); tolerate missing FastAPI at import.
try:
    from .routes_score import router as _score_router

    if _score_router is not None:
        app.include_router(_score_router)
except Exception as exc:  # pragma: no cover
    log.warning("score router not mounted: %s", exc)
    try:
        from .scorer import router as _scorer_router  # type: ignore

        if _scorer_router is not None:
            app.include_router(_scorer_router)
    except Exception:
        pass


# Diag router (GET /api/diag/*); before static mount so diagnostics never
# fall through to the frontend.
# NOTE: POST /api/score/test is canonically served by routes_score
# (test_scorer_endpoint -> {ok, has_key, base_url, model, score, summary,
# result}); diag.py defines no /api/score/* route to avoid shadowing.
try:
    from .diag import router as _diag_router

    if _diag_router is not None:
        app.include_router(_diag_router)
except Exception as exc:  # pragma: no cover
    log.warning("diag router not mounted: %s", exc)


@app.get("/api/health")
def health():
    return {"ok": True}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_fit(job_id: int, db: Session) -> tuple[int, Optional[dict]]:
    """Return (score, fit_dict) for a job from fit_results (0/None if absent)."""
    row = db.query(FitResult).filter(FitResult.job_id == job_id).first()
    if row is None:
        return 0, None
    score = int(row.score or 0)
    fit: Optional[dict] = None
    try:
        fit = json.loads(row.evidence_json or "{}")
    except Exception:
        fit = {"score": score, "summary": row.evidence_json or ""}
    if isinstance(fit, dict) and "score" not in fit:
        fit["score"] = score
    return score, fit


def _job_to_dict(job: Job, db: Session) -> dict:
    score, fit = _parse_fit(job.id, db)
    fit = fit or {}
    reqs = fit.get("requirements", []) if isinstance(fit, dict) else []
    gaps = fit.get("gaps", []) if isinstance(fit, dict) else []
    summary = fit.get("summary", "") if isinstance(fit, dict) else ""
    return {
        "id": job.id,
        "source": job.source,
        "external_id": job.external_id,
        "company": job.company,
        "title": job.title,
        "location_text": job.location_text,
        "location": job.location_text,
        "lat": job.lat,
        "lng": job.lng,
        "work_mode": job.work_mode,
        "salary_min": job.salary_min,
        "salary_max": job.salary_max,
        "salary_text": job.salary_text,
        "salary": job.salary_text,
        "url": job.url,
        "source_url": job.url,
        "description": job.description,
        "description_hash": job.description_hash,
        "industry": job.industry,
        "distance_km": job.distance_km,
        "distance": job.distance_km,
        "status": job.status,
        "excluded_reason": job.excluded_reason,
        "excluded": bool(job.excluded_reason),
        "first_seen": job.first_seen.isoformat() if job.first_seen else None,
        "last_seen": job.last_seen.isoformat() if job.last_seen else None,
        "score": int(fit.get("score", score) or 0) if isinstance(fit, dict) else score,
        "fit": fit or None,
        "requirements": reqs if isinstance(reqs, list) else [],
        "gaps": gaps if isinstance(gaps, list) else [],
        "summary": summary if isinstance(summary, str) else str(summary or ""),
    }


def _unified_search_setup(db: Session) -> dict:
    prof = get_profile(db)
    dbk = get_dealbreakers(db)
    allow_remote = bool(prof.get("remote_aus_ok", True) or prof.get("remote_global_ok", False))
    return {
        "titles": prof.get("titles", []),
        "keywords_include": prof.get("keywords_include", []),
        "keywords_exclude": prof.get("keywords_exclude", []),
        "salary_floor": prof.get("salary_floor"),
        "allow_remote": allow_remote,
        "allow_hybrid": True,
        "allow_onsite": True,
        "remote_aus_ok": prof.get("remote_aus_ok", True),
        "remote_global_ok": prof.get("remote_global_ok", False),
        "dealbreakers": {
            "industries": dbk.get("industries", []),
            "keywords": dbk.get("keywords", []),
        },
    }


# ---------------------------------------------------------------------------
# Jobs: GET /api/jobs with filters
# ---------------------------------------------------------------------------

@app.get("/api/jobs")
def list_jobs(
    status: Optional[str] = Query(default=None),
    min_score: Optional[float] = Query(default=None),
    work_mode: Optional[str] = Query(default=None),
    max_distance: Optional[float] = Query(default=None),
    q: Optional[str] = Query(default=None),
    include_excluded: bool = Query(default=False),
    # camelCase aliases used by some clients
    minScore: Optional[float] = Query(default=None, include_in_schema=False),  # type: ignore[valid-type]
    mode: Optional[str] = Query(default=None, include_in_schema=False),
    maxDistance: Optional[float] = Query(default=None, include_in_schema=False),  # type: ignore[valid-type]
    max_dist: Optional[float] = Query(default=None, include_in_schema=False),
    query: Optional[str] = Query(default=None, include_in_schema=False),
    includeExcluded: Optional[bool] = Query(default=None, include_in_schema=False),
    db: Session = Depends(get_db),
):
    # Resolve aliases.
    if min_score is None and minScore is not None:
        min_score = minScore
    if work_mode is None and mode is not None:
        work_mode = mode
    if max_distance is None:
        max_distance = maxDistance if maxDistance is not None else max_dist
    if q is None and query is not None:
        q = query
    if includeExcluded is not None:
        include_excluded = includeExcluded

    query_obj = db.query(Job)

    if status and status != "all":
        query_obj = query_obj.filter(Job.status == status)

    if not include_excluded:
        query_obj = query_obj.filter(
            or_(Job.excluded_reason.is_(None), Job.excluded_reason == "")
        )

    if work_mode and work_mode != "all":
        wm = str(work_mode).strip().lower()
        if wm in ("remote", "remote_aus", "remote_global", "remote-aus", "remote-global"):
            query_obj = query_obj.filter(Job.work_mode.in_(
                ["remote", "remote_aus", "remote_global"]
            ))
        else:
            query_obj = query_obj.filter(Job.work_mode == wm)

    if max_distance is not None:
        try:
            md = float(max_distance)
            query_obj = query_obj.filter(Job.distance_km.is_not(None)).filter(
                Job.distance_km <= md
            )
        except (TypeError, ValueError):
            pass

    if q and q.strip():
        tokens = [t for t in q.strip().split() if t]
        for tok in tokens:
            like = f"%{tok}%"
            query_obj = query_obj.filter(
                or_(
                    Job.company.ilike(like),
                    Job.title.ilike(like),
                    Job.location_text.ilike(like),
                    Job.description.ilike(like),
                    Job.industry.ilike(like),
                )
            )

    jobs = query_obj.order_by(Job.last_seen.desc().nullslast(), Job.id.desc()).all()

    out: list[dict] = []
    for job in jobs:
        d = _job_to_dict(job, db)
        if min_score is not None:
            try:
                if d["score"] < float(min_score):
                    continue
            except (TypeError, ValueError):
                pass
        out.append(d)
    # Frontend sorts by score desc; keep server order stable but also sort.
    out.sort(key=lambda j: j.get("score", 0), reverse=True)
    return out


# ---------------------------------------------------------------------------
# Jobs: PATCH status (single + batch). Batch defined first to avoid any
# ambiguity with /api/jobs/{id}/status.
# ---------------------------------------------------------------------------

@app.patch("/api/jobs/status")
def bulk_update_job_status(
    payload: dict = Body(...), db: Session = Depends(get_db)
):
    status_val = payload.get("status")
    ids = payload.get("ids", payload.get("job_ids", []))
    if not isinstance(ids, list):
        raise HTTPException(status_code=422, detail="ids must be a list")
    if status_val not in VALID_STATUSES:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid status {status_val!r}. Valid: {sorted(VALID_STATUSES)}",
        )
    # Coerce string ids (frontend sends String(id)) to int where possible.
    norm_ids: list[int] = []
    for raw in ids:
        try:
            norm_ids.append(int(raw))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
    updated = 0
    if norm_ids:
        rows = db.query(Job).filter(Job.id.in_(norm_ids)).all()
        for job in rows:
            job.status = status_val  # any-to-any, instant persist
            updated += 1
        db.commit()
    return {"ok": True, "updated": updated, "status": status_val, "ids": norm_ids}


@app.patch("/api/jobs/{job_id}/status")
def update_job_status(
    job_id: int, payload: dict = Body(...), db: Session = Depends(get_db)
):
    status_val = payload.get("status")
    if status_val not in VALID_STATUSES:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid status {status_val!r}. Valid: {sorted(VALID_STATUSES)}",
        )
    job = db.query(Job).filter(Job.id == job_id).first()
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    job.status = status_val  # any-to-any, instant persist
    db.commit()
    db.refresh(job)
    return _job_to_dict(job, db)


# ---------------------------------------------------------------------------
# Compatibility: /api/search-setup <-> profile
# ---------------------------------------------------------------------------

@app.get("/api/search-setup")
def read_search_setup(db: Session = Depends(get_db)):
    return _unified_search_setup(db)


@app.put("/api/search-setup")
def write_search_setup(
    payload: dict = Body(...), db: Session = Depends(get_db)
):
    existing = get_profile(db)
    titles = payload.get("titles", payload.get("job_titles", []))
    kw_inc = payload.get("keywords_include", payload.get("include", []))
    kw_exc = payload.get("keywords_exclude", payload.get("exclude", []))
    salary_floor = payload.get("salary_floor", payload.get("salaryFloor"))

    if "remote_aus_ok" in payload:
        remote_aus_ok = bool(payload["remote_aus_ok"])
    elif "allow_remote" in payload:
        remote_aus_ok = bool(payload["allow_remote"])
    else:
        remote_aus_ok = bool(existing.get("remote_aus_ok", True))

    if "remote_global_ok" in payload:
        remote_global_ok = bool(payload["remote_global_ok"])
    else:
        # Preserve existing global flag (frontend mirrors this way).
        remote_global_ok = bool(existing.get("remote_global_ok", False))

    prof = save_profile(
        db,
        ProfileSchema(
            titles=list(titles or []),
            keywords_include=list(kw_inc or []),
            keywords_exclude=list(kw_exc or []),
            salary_floor=salary_floor,
            remote_aus_ok=remote_aus_ok,
            remote_global_ok=remote_global_ok,
        ),
    )
    dbk_raw = payload.get("dealbreakers", {})
    if isinstance(dbk_raw, list):
        dbk_raw = {"industries": [], "keywords": dbk_raw}
    if isinstance(dbk_raw, dict):
        save_dealbreakers(
            db,
            DealbreakersSchema(
                industries=list(dbk_raw.get("industries", []) or []),
                keywords=list(dbk_raw.get("keywords", []) or []),
            ),
        )
    unified = _unified_search_setup(db)
    # Echo back UI toggles even though hybrid/onsite are not persisted.
    if "allow_hybrid" in payload:
        unified["allow_hybrid"] = bool(payload["allow_hybrid"])
    if "allow_onsite" in payload:
        unified["allow_onsite"] = bool(payload["allow_onsite"])
    _ = prof
    return unified


# ---------------------------------------------------------------------------
# Compatibility: /api/documents <-> /api/docs
# ---------------------------------------------------------------------------

def _list_docs_compat(db: Session):
    docs = db.query(Document).order_by(Document.created_at.desc()).all()
    return [_doc_to_dict(d) for d in docs]


@app.get("/api/documents")
@app.get("/api/documents/")
def list_documents_compat(db: Session = Depends(get_db)):
    return _list_docs_compat(db)


@app.get("/api/documents/{doc_id}")
def get_document_compat(doc_id: int, db: Session = Depends(get_db)):
    doc = db.query(Document).filter(Document.id == doc_id).first()
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    return _doc_to_dict(doc, include_text=True)


@app.get("/api/documents/{doc_id}/preview")
def preview_document_compat(doc_id: int, db: Session = Depends(get_db)):
    doc = db.query(Document).filter(Document.id == doc_id).first()
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    # Backend stores extracted text (not original bytes); serve as text so
    # the frontend iframe preview still renders.
    return PlainTextResponse(doc.text or "", media_type="text/plain; charset=utf-8")


@app.post("/api/documents")
@app.post("/api/documents/")
def upload_document_compat(
    file: UploadFile = File(...),
    kind: str = Form("cv"),
    db: Session = Depends(get_db),
):
    original_name = file.filename or "upload"
    suffix = Path(original_name).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type {suffix!r}. Allowed: {sorted(ALLOWED_EXTENSIONS)}",
        )
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    stored_name = f"{uuid.uuid4().hex}{suffix}"
    dest = UPLOAD_DIR / stored_name
    with dest.open("wb") as out:
        shutil.copyfileobj(file.file, out)
    try:
        text = extract_text(dest, suffix)
    except Exception as exc:
        dest.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail=f"Could not extract text: {exc}")
    doc = Document(filename=original_name, filetype=suffix, text=text, kind=kind or "cv")
    db.add(doc)
    db.commit()
    db.refresh(doc)
    return _doc_to_dict(doc, include_text=False)


@app.delete("/api/documents/{doc_id}")
def delete_document_compat(doc_id: int, db: Session = Depends(get_db)):
    doc = db.query(Document).filter(Document.id == doc_id).first()
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    db.delete(doc)
    db.commit()
    return {"ok": True, "id": doc_id}


# ---------------------------------------------------------------------------
# Search run: POST /api/search/run -> ingest + score new hashes only
# ---------------------------------------------------------------------------

@app.post("/api/search/run")
def api_search_run(
    payload: Optional[dict] = Body(default=None), db: Session = Depends(get_db)
):
    from .ingest import run_search

    config = payload or {}
    try:
        # Pass the request session so enabled CompanySource rows load from
        # the DB (fallback data/sources.json inside run_search). Never None-out.
        summary = run_search(config, db=db)
    except Exception as exc:
        log.exception("run_search failed")
        raise HTTPException(status_code=500, detail=f"search failed: {exc}")
    if not isinstance(summary, dict):
        summary = {"per_source": []}

    # Score ONLY new hashes (run_search reports needs_scoring urls).
    scored = 0
    scores: dict[Any, Any] = {}
    errors: dict[str, str] = {}
    try:
        needs = summary.get("needs_scoring", []) or []
        if needs:
            from .scorer import has_api_key, load_profile_text, score_new_jobs

            rows = db.query(Job).filter(Job.url.in_(needs)).all()
            if rows:
                profile_text = load_profile_text(db)
                jobs_for_scoring = [
                    {"id": r.id, "description": r.description or ""} for r in rows
                ]
                if has_api_key():
                    results = score_new_jobs(jobs_for_scoring, profile_text)
                    # Backward compat: scores stays {id: score int}; errors carries summaries.
                    for k, v in results.items():
                        try:
                            scores[str(k)] = int(v.get("score", 0))
                        except (TypeError, ValueError):
                            scores[str(k)] = 0
                        summ = str(v.get("summary", "") or "")
                        if summ.startswith("ERROR:") or summ.startswith("NO_KEY"):
                            errors[str(k)] = summ[:500]
                    scored = len(scores) - len(errors)
                else:
                    # Offline-safe: no key -> return stubs without persisting.
                    score_new_jobs(jobs_for_scoring, profile_text)
                    scored = 0
    except Exception as exc:
        # Scoring must never break ingestion.
        log.warning("post-ingest scoring skipped: %s", exc)
    per_source = summary.get("per_source", [])
    if not isinstance(per_source, list):
        per_source = []
    return {**summary, "per_source": per_source, "scored": scored, "scores": scores, "errors": errors}


# Serve frontend/ statically (owned by another agent - do not modify).
# Mounted last so /api/* routes take precedence.
if FRONTEND_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")

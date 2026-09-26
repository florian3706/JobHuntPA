"""Search pipeline: list every source, fetch only what is new, filter, store.

Per source:

1. ``list_postings()`` - one pass over the listing (cached 30-60 min in
   ``http_cache``, so repeated runs are free).
2. Each posting is upserted by URL. A job already stored with a full
   description is never fetched again; the ``jobs`` table is the cache.
3. Cheap listing-stage filters run first (dealbreakers, title, salary,
   location). Only survivors that still lack a description get their
   detail page fetched, capped per run (the rest follow next run).
4. Work mode / geocode / distance enrichment, then the full filter.
5. For sources that list all their jobs, previously seen jobs missing
   from the listing are marked closed.

Scoring is a separate step (``backend.scorer.score_pending``).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional

from sqlalchemy.orm import Session

from backend.adapters.base import Adapter, Posting, SourceError
from backend.adapters.generic import GenericAdapter
from backend.adapters.seek import SeekAdapter
from backend.db import CompanySource, Job, SessionLocal
from backend.filters import Criteria, commute_pins, exclusion_reason, load_criteria
from backend.geo import HOME_COUNTRY, classify_work_mode, geocode, guess_country, home_distance, parse_office_days
from backend.profile import get_profile
from backend.scraping.http import FetchError, PoliteClient, purge_cache
from backend.scraping.text import description_hash

log = logging.getLogger(__name__)

MAX_DETAILS_PER_SOURCE = 60
NOT_A_JOB = "not a job posting page"

Progress = Callable[[str, dict], None]


@dataclass
class SourceReport:
    label: str
    url: str = ""
    method: str = ""
    listed: int = 0
    new: int = 0
    details_fetched: int = 0
    details_deferred: int = 0
    detail_errors: int = 0
    excluded: int = 0
    kept: int = 0
    closed: int = 0
    error: Optional[str] = None
    notes: list[str] = field(default_factory=list)


def build_adapters(db: Session, ws: int, client: PoliteClient) -> list[tuple[Optional[CompanySource], Adapter]]:
    prof = get_profile(db, ws)
    adapters: list[tuple[Optional[CompanySource], Adapter]] = []
    if prof["seek_enabled"]:
        adapters.append((None, SeekAdapter(client, prof["titles"], prof["seek_locations"])))
    sources = (db.query(CompanySource)
               .filter(CompanySource.workspace_id == ws, CompanySource.enabled.is_(True))
               .order_by(CompanySource.id))
    for src in sources:
        adapters.append((src, GenericAdapter(client, src.careers_url, company=src.label or "")))
    return adapters


def _apply_posting(job: Job, p: Posting) -> None:
    """Copy listing/detail data onto the row without losing better data."""
    job.title = p.title or job.title
    job.company = p.company or job.company
    job.external_id = p.external_id or job.external_id
    if p.location_text and p.location_text != job.location_text:
        job.location_text = p.location_text
        job.lat = job.lng = None  # re-geocode
    job.country = p.country or job.country or guess_country(job.location_text or "")
    if p.work_mode and p.work_mode != "unknown":
        job.work_mode = p.work_mode
    elif p.detail_status == "full" and job.detail_status != "full":
        job.work_mode = "unknown"  # re-classify from the new description
    job.salary_text = p.salary_text or job.salary_text
    if p.salary_min:
        job.salary_min = int(p.salary_min)
    if p.salary_max:
        job.salary_max = int(p.salary_max)
    job.posted_at = p.posted_at or job.posted_at
    job.industry = p.industry or job.industry
    rank = {"none": 0, "summary": 1, "full": 2}
    if p.description and rank.get(p.detail_status, 0) >= rank.get(job.detail_status or "none", 0):
        job.description = p.description
        job.detail_status = p.detail_status
        if p.detail_status == "full":
            job.detail_fetched_at = datetime.utcnow()


def enrich(job: Job, crit: Criteria) -> None:
    job.country = job.country or guess_country(job.location_text or "")
    # Only places in the home country need coordinates (pins, distance).
    if job.location_text and job.lat is None and job.country in ("", HOME_COUNTRY):
        hit = geocode(job.location_text)
        if hit:
            job.lat, job.lng = hit["lat"], hit["lng"]
            job.country = job.country or hit["country"]
    job.office_days = parse_office_days(job.title or "", job.location_text or "", job.description or "")
    if (job.work_mode or "unknown") == "unknown":
        job.work_mode = classify_work_mode(job.title or "", job.location_text or "", job.description or "")
    if job.work_mode == "unknown" and job.office_days:
        job.work_mode = "hybrid" if job.office_days < 5 else "onsite"
    job.distance_km = home_distance(job.lat, job.lng, commute_pins(crit.pins))
    job.description_hash = description_hash(job.description or "")


def upsert_posting(db: Session, ws: int, p: Posting, source: str, source_id: Optional[int],
                   crit: Criteria, now: datetime) -> tuple[Job, bool]:
    job = db.query(Job).filter(Job.workspace_id == ws, Job.url == p.url).first()
    is_new = job is None
    if is_new:
        job = Job(workspace_id=ws, url=p.url, status="to_review", first_seen=now, detail_status="none")
        db.add(job)
    job.source = source
    if source_id is not None or is_new:
        job.source_id = source_id
    _apply_posting(job, p)
    job.last_seen, job.closed_at = now, None
    enrich(job, crit)
    return job, is_new


def import_postings(postings: list[Posting], source: str, ws: int) -> dict:
    """Store postings parsed from a page the user saved in their browser."""
    db = SessionLocal()
    try:
        crit = load_criteria(db, ws)
        now = datetime.utcnow()
        new = kept = 0
        for p in postings:
            job, is_new = upsert_posting(db, ws, p, source, None, crit, now)
            if job.excluded_reason == NOT_A_JOB:
                job.excluded_reason = None
            stage = "full" if job.detail_status in ("full", "summary") else "listing"
            job.excluded_reason = exclusion_reason(job, crit, stage=stage)
            new += is_new
            kept += job.excluded_reason is None
            db.commit()
        return {"imported": len(postings), "new": new, "kept": kept, "excluded": len(postings) - kept}
    finally:
        db.close()


def refilter(db: Session, crit: Criteria, ws: int, jobs: Optional[list[Job]] = None) -> int:
    """Re-apply filters to stored jobs (no network). Returns #changed."""
    changed = 0
    for job in jobs if jobs is not None else db.query(Job).filter(Job.workspace_id == ws).all():
        if job.excluded_reason == NOT_A_JOB:
            continue
        stage = "full" if job.detail_status in ("full", "summary") else "listing"
        reason = exclusion_reason(job, crit, stage=stage)
        if reason != job.excluded_reason:
            job.excluded_reason = reason
            changed += 1
    db.commit()
    return changed


def _process_source(db: Session, ws: int, src: Optional[CompanySource], adapter: Adapter,
                    crit: Criteria, rep: SourceReport, progress: Progress) -> None:
    postings = adapter.list_postings()
    rep.method = adapter.method
    rep.listed = len(postings)
    rep.notes.extend(getattr(adapter, "notes", []))
    now = datetime.utcnow()
    seen: set[str] = set()
    detail_budget = MAX_DETAILS_PER_SOURCE

    for i, p in enumerate(postings, 1):
        if not p.url or p.url in seen:
            continue
        seen.add(p.url)
        progress(f"{rep.label}: {i}/{len(postings)}", {"source": rep.label, "done": i, "total": len(postings)})
        if src is not None and src.label:
            p.company = p.company or src.label
        job, is_new = upsert_posting(db, ws, p, adapter.source, src.id if src is not None else None, crit, now)
        rep.new += is_new

        if job.excluded_reason == NOT_A_JOB:
            db.commit()
            continue
        listing_reason = exclusion_reason(job, crit, stage="listing")
        if listing_reason is None and job.detail_status != "full" and adapter.has_details:
            if detail_budget <= 0:
                rep.details_deferred += 1
            else:
                detail_budget -= 1
                try:
                    detailed = adapter.fetch_detail(p)
                except (FetchError, SourceError, ValueError) as exc:
                    rep.detail_errors += 1
                    if len(rep.notes) < 5:
                        rep.notes.append(f"detail {p.url}: {exc}")
                    detailed = p
                else:
                    rep.details_fetched += 1
                    if detailed is None:
                        job.excluded_reason = NOT_A_JOB
                        job.detail_status = "full"
                        db.commit()
                        rep.excluded += 1
                        continue
                _apply_posting(job, detailed)
                enrich(job, crit)

        stage = "full" if job.detail_status in ("full", "summary") else "listing"
        job.excluded_reason = exclusion_reason(job, crit, stage=stage)
        if job.excluded_reason:
            rep.excluded += 1
        else:
            rep.kept += 1
        db.commit()

    if adapter.complete_listing and postings:
        q = db.query(Job).filter(Job.workspace_id == ws, Job.closed_at.is_(None), Job.url.notin_(seen))
        q = q.filter(Job.source_id == src.id) if src is not None else q.filter(Job.source == adapter.source)
        rep.closed = q.update({Job.closed_at: now}, synchronize_session=False)
        db.commit()


def run_search(ws: int, progress: Progress = lambda stage, info: None) -> dict:
    db = SessionLocal()
    client = PoliteClient()
    try:
        crit = load_criteria(db, ws)
        reports: list[SourceReport] = []
        for src, adapter in build_adapters(db, ws, client):
            label = (src.label or src.careers_url) if src is not None else "SEEK"
            rep = SourceReport(label=label, url=src.careers_url if src is not None else "https://www.seek.com.au")
            reports.append(rep)
            progress(f"{label}: listing jobs", {"source": label})
            try:
                _process_source(db, ws, src, adapter, crit, rep, progress)
            except (SourceError, FetchError) as exc:
                db.rollback()
                rep.error = str(exc)
                rep.notes.extend(getattr(adapter, "notes", []))
            except Exception as exc:  # a bug in one source must not stop the others
                db.rollback()
                log.exception("source %s failed", label)
                rep.error = f"unexpected error: {exc.__class__.__name__}: {exc}"
            if src is not None:
                src.last_run = datetime.utcnow()
                src.last_count = rep.listed
                src.last_error = rep.error
                src.last_method = rep.method or None
                db.commit()
        progress("re-applying filters to stored jobs", {})
        refilter(db, crit, ws)
        purge_cache()
        totals = {k: sum(getattr(r, k) for r in reports)
                  for k in ("listed", "new", "details_fetched", "details_deferred", "excluded", "kept", "closed")}
        return {"per_source": [r.__dict__ for r in reports], "totals": totals}
    finally:
        client.close()
        db.close()

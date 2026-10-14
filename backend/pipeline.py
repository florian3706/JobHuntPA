"""Search pipeline: list every source, fetch only what is new, filter, store.

Ads live once in the shared pool (``backend.pool``, accounts design §14); a workspace links to them. Per source:

1. ``list_postings()`` - one pass over the listing (cached 30-60 min in
   ``http_cache``, so repeated runs are free).
2. Each posting is upserted into the pool by URL and linked to the workspace.
3. Cheap listing-stage filters run first (dealbreakers, title, salary,
   location). Only survivors whose posting the pool has no fresh full
   description for get their detail page fetched (``pool.needs_detail``),
   capped per run (the rest follow next run). A posting another search
   already fetched costs nothing.
4. Placement / distance for this workspace, then the full filter.
5. Closing is global (``backend.pool``): a detail page that's gone, a
   complete listing that no longer shows the ad, or no listing showing it
   for weeks. One user's results lacking an ad never close it.

Then open pool postings other searches found are matched against this
workspace's profile (``pool.match_pool``). Scoring is a separate step
(``backend.scorer.score_jobs``).
"""
from __future__ import annotations

import json
import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional

from sqlalchemy.orm import Session

from backend import job_source, pool
from backend.adapters.base import Adapter, Posting, SourceError
from backend.adapters.generic import source_adapter
from backend.adapters.seek import SeekAdapter
from backend.db import CompanySource, Job, SessionLocal
from backend.filters import Criteria, commute_pins, exclusion_reason, load_criteria
from backend.geo import HOME_COUNTRY, geocode, home_distance
from backend.pool import derive
from backend.profile import get_profile
from backend.scraping.http import FetchError, PoliteClient, purge_cache

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


def _labels(src: Optional[CompanySource]) -> tuple[str, Optional[str]]:
    """(the company name a source's jobs fall back to, the user's own label if they typed one). The fallback is
    derived from the URL and may go into the shared pool; a label the user typed is theirs, so it's only ever
    this workspace's own value."""
    from backend.sources import default_label

    if src is None:
        return "", None
    fallback = default_label(src.careers_url or "")
    label = (src.label or "").strip()
    return fallback, (label if label and label != fallback else None)


def build_adapters(db: Session, ws: int, client: PoliteClient) -> list[tuple[Optional[CompanySource], Adapter]]:
    prof = get_profile(db, ws)
    adapters: list[tuple[Optional[CompanySource], Adapter]] = []
    if prof["seek_enabled"]:
        adapters.append((None, SeekAdapter(client, prof["titles"], prof["seek_locations"])))
    sources = (db.query(CompanySource)
               .filter(CompanySource.workspace_id == ws, CompanySource.enabled.is_(True))
               .order_by(CompanySource.id))
    for src in sources:
        adapters.append((src, source_adapter(client, src.careers_url, company=_labels(src)[0], mapping=src.mapping)))
    return adapters


MAX_PLACES = 10


def locate(location_text: str, pins: list[dict]) -> Optional[dict]:
    """Coordinates for a job's location. An ad open in several places
    ("Sydney NSW; Canberra ACT") is placed at the one nearest home; if any of
    them can't be placed (e.g. "anywhere in NSW") the job isn't placed, so
    the location filter can't wrongly rule it out."""
    places = [p.strip() for p in re.split(r"\s*;\s*", location_text or "") if p.strip()]
    if len(places) < 2 or not pins:
        return geocode(location_text)
    hits = [geocode(p) for p in places[:MAX_PLACES]]
    if any(h is None for h in hits):
        return None
    return min(hits, key=lambda h: home_distance(h["lat"], h["lng"], pins))


def enrich(job: Job, crit: Criteria) -> None:
    """The workspace's view of a job: where it is (an office beats the ad's city; an ad open in several places
    goes to the one nearest home) and how far. What follows from the ad's text alone is the posting's
    (``pool.derive``), except for a job that reads its own ad (a private upload, or a private job)."""
    if job.has_own_ad:
        derive(job)
    # A known office (backend.offices) beats the ad's location, which is often just a city.
    if job.office_lat is not None:
        job.lat, job.lng = job.office_lat, job.office_lng
    # Only places in the home country need coordinates (pins, distance).
    elif job.location_text and job.lat is None and (job.country or "") in ("", HOME_COUNTRY):
        hit = locate(job.location_text, commute_pins(crit.pins))
        if hit:
            job.lat, job.lng = hit["lat"], hit["lng"]
            if not job.country:
                job.country = hit["country"]
    job.distance_km = home_distance(job.lat, job.lng, commute_pins(crit.pins))


def job_by_url(db: Session, ws: int, url: Optional[str]) -> Optional[Job]:
    """The workspace's job with this URL, if any (``pool.job_by_url``)."""
    return pool.job_by_url(db, ws, url)


def listing_key(adapter: Adapter) -> str:
    """Which listing an ad came from, for the closing rule (``pool.close_unlisted``): the adapter's source for
    job-board feeds and APSJobs (whose source holds the query); a crawled careers page also by its URL, so two
    users crawling different pages of one site can't close each other's ads."""
    from backend.adapters.generic import GenericAdapter

    if isinstance(adapter, GenericAdapter) and adapter.delegate is None:
        return f"{adapter.source}|{adapter.careers_url}"
    return adapter.source


def upsert_posting(db: Session, ws: int, p: Posting, source: str, source_id: Optional[int],
                   crit: Criteria, now: datetime, key: Optional[str] = None) -> tuple[Job, bool]:
    """A listed ad: stored in the shared pool, linked to the workspace and brought up to date there.
    Returns (the workspace's job, new to the workspace)."""
    posting, _ = pool.upsert_scraped(db, p, source, key, now)
    job, is_new = pool.link(db, ws, posting, now, source_id)
    job.last_seen = now
    if job.posting_rev != posting.rev:
        pool.refresh_link(job, crit)
    else:
        enrich(job, crit)  # the pins may have moved
    return job, is_new


def stage_of(job: Job) -> str:
    """The filter stage a job's description allows: "full" once there is a description (full or summary)."""
    return "full" if job.detail_status in ("full", "summary") else "listing"


def _import(db: Session, ws: int, p: Posting, source: str, crit: Criteria, now: datetime) -> tuple[Job, bool]:
    """One posting from a page the user has (``pool.import_private``), placed and filtered, committed."""
    job, is_new = pool.import_private(db, ws, p, source, now)
    if job.excluded_reason == NOT_A_JOB:
        job.excluded_reason = None
    enrich(job, crit)
    job.excluded_reason = exclusion_reason(job, crit, stage=stage_of(job))
    db.commit()
    return job, is_new


def import_postings(postings: list[Posting], source: str, ws: int) -> dict:
    """Store postings parsed from a page the user saved in their browser. They're the user's own: a job the pool
    doesn't have stays private to this workspace (``pool.import_private``)."""
    db = SessionLocal()
    try:
        crit = load_criteria(db, ws)
        now = datetime.utcnow()
        new = kept = 0
        for p in postings:
            job, is_new = _import(db, ws, p, source, crit, now)
            new += is_new
            kept += job.excluded_reason is None
        return {"imported": len(postings), "new": new, "kept": kept, "excluded": len(postings) - kept}
    finally:
        db.close()


def import_one(posting: Posting, source: str, ws: int) -> tuple[int, bool]:
    """``import_postings`` for one posting (a job sent by the browser extension). Returns (the job's id, new to the
    workspace)."""
    db = SessionLocal()
    try:
        job, is_new = _import(db, ws, posting, source, load_criteria(db, ws), datetime.utcnow())
        return job.id, is_new
    finally:
        db.close()


def add_listed(listed: list[tuple[Posting, str]], ws: int) -> list[tuple[int, bool]]:
    """Jobs from a results page the user had open in their browser (the extension's "add the jobs on this page"),
    each with the source a new private job gets. Only jobs the workspace doesn't have are added
    (``pool.add_listed``), placed and filtered like an import; the ones it has are left as they are. Returns (the
    job's id, new to the workspace) for each, in order."""
    db = SessionLocal()
    try:
        crit = load_criteria(db, ws)
        now = datetime.utcnow()
        out = []
        for p, source in listed:
            job, is_new = pool.add_listed(db, ws, p, source, now)
            if is_new:
                if job.posting is not None:
                    pool.refresh_link(job, crit)  # the pool's ad: placed, its office taken over
                else:
                    enrich(job, crit)
                job.excluded_reason = exclusion_reason(job, crit, stage=stage_of(job))
                db.commit()
            out.append((job.id, is_new))
        return out
    finally:
        db.close()


def name_browser_sources(db: Session, ws: int) -> int:
    """Once per workspace: jobs the browser extension added while their source was plain ``browser`` get the source
    that says how and from which site (``backend.job_source``). How is read from ``first_seen``: the jobs of one
    results page were stored with one timestamp, a job from its own page with its own, so two or more old jobs with
    the same one came in bulk. Returns how many were renamed. Run when the workspace's jobs are next listed
    (``GET /api/jobs``), and by its next search."""
    from backend.db import AppSetting

    key = "browser_source_workspaces"
    row = db.get(AppSetting, key)
    done = set(json.loads(row.value_json)) if row else set()
    if ws in done:
        return 0
    # Decided in Python: a job's own source is sealed.
    old = [(job_id, seen) for job_id, own, seen in db.query(Job.id, Job._source, Job.first_seen)
           .filter(Job.workspace_id == ws) if own == job_source.BROWSER]
    together = Counter(seen for _, seen in old)
    for job_id, seen in old:
        job = db.get(Job, job_id)
        job.source = job_source.browser(job_source.BULK if together[seen] > 1 else job_source.SINGLE, job.url)
    db.merge(AppSetting(key=key, value_json=json.dumps(sorted(done | {ws}))))
    db.commit()
    return len(old)


def seek_addresses(db: Session, ws: int) -> int:
    """Once per workspace: jobs stored under SEEK's newer address (``au.seek.com/job/<id>``, which
    ``extension.seek_job_id`` didn't take for SEEK's before 2026-10-12, so the extension's jobs from there were
    stored as some other site's) become the SEEK jobs they are: SEEK's job number, SEEK as the site of a
    browser-extension source, and the address the searches use. When the workspace already has a job under that
    address, the two are the same ad: they are merged (``backend.duplicates``) and the copy keeps its old address.
    Returns how many jobs were put right. Run like ``name_browser_sources``, after it."""
    from backend.adapters.seek import JOB_URL
    from backend.db import AppSetting
    from backend.duplicates import Ranking, merge, record, root_of
    from backend.extension import seek_job_id

    key = "seek_address_workspaces"
    row = db.get(AppSetting, key)
    done = set(json.loads(row.value_json)) if row else set()
    if ws in done:
        return 0
    # Decided in Python: a private job's address is sealed. (A pool link's address is the pool's, always SEEK's own.)
    old = [(job_id, seek_job_id(url)) for job_id, url in db.query(Job.id, Job._url)
           .filter(Job.workspace_id == ws, Job.posting_id.is_(None)) if url]
    old = [(job_id, seek_id) for job_id, seek_id in old if seek_id]
    crit = ranking = None
    fixed = 0
    for job_id, seek_id in old:
        job = db.get(Job, job_id)
        address = JOB_URL.format(id=seek_id)
        if job.url == address and job.external_id == seek_id:
            continue
        how = job_source.browser_parts(job.source)
        if how is not None:
            job.source = job_source.browser(how[0], address)
        job.external_id = seek_id
        twin = job_by_url(db, ws, address)
        if twin is None:
            job.url = address
            db.flush()  # the next job with this number finds this one
        elif twin.id != job.id:
            a, b = root_of(db, twin), root_of(db, job)
            if a.id != b.id:
                crit, ranking = crit or load_criteria(db, ws), ranking or Ranking(db, ws)
                listed = merge(db, [a, b], crit, ranking)
                record(db, ws, listed.id, b.id if listed.id == a.id else a.id, "merged", "auto")
        fixed += 1
    db.merge(AppSetting(key=key, value_json=json.dumps(sorted(done | {ws}))))
    db.commit()
    return fixed


def update_job_from_posting(db: Session, job: Job, p: Posting) -> None:
    """Replace a stored job's data with a fuller posting (e.g. a saved SEEK job page for a listing we only had a
    summary of), then re-filter it. The page is the user's own: it's kept on this workspace's job, and the shared
    posting (what everyone else reads) keeps the summary."""
    crit = load_criteria(db, job.workspace_id)
    pool.apply_own(job, p, datetime.utcnow())
    enrich(job, crit)
    job.excluded_reason = exclusion_reason(job, crit, stage="full")
    db.commit()


def replace_jobs(db: Session, ws: int, crit: Criteria, queries: set[str]) -> int:
    """Geocode again the jobs whose location was one of these (dropped)
    geocoder queries. Returns how many moved."""
    from backend.geo import _geocode_query

    moved = 0
    for job in db.query(Job).filter(Job.workspace_id == ws):
        if not job.location_text or job.office_lat is not None:  # in Python: office fields are sealed
            continue
        places = re.split(r"\s*;\s*", job.location_text)
        if any(_geocode_query(place).lower() in queries for place in places):
            job.lat = job.lng = None
            enrich(job, crit)
            moved += 1
    db.commit()
    return moved


def repair_geocodes(db: Session, ws: int, crit: Criteria, progress: Progress) -> None:
    """Jobs placed by an older geocoder's mistakes are placed again, once per
    workspace per repair:
    - a council area's centre instead of the suburb (before suburbs were preferred);
    - SEEK's "Suburb, Region STATE" not found, or found as a road or building sharing
      the region's words (before such suburbs were looked up in their state)."""
    from backend.db import AppSetting
    from backend.geo import purge_area_geocodes, purge_seek_geocodes

    def setting(key: str) -> list:
        row = db.get(AppSetting, key)
        return json.loads(row.value_json) if row else []

    for key, purge, what in (("geocode_repair", purge_area_geocodes, "placing suburbs that were mapped to council areas"),
                             ("geocode_repair_seek", purge_seek_geocodes, "placing SEEK suburbs the map search missed")):
        done = set(setting(f"{key}_workspaces"))
        if ws in done:
            continue
        # The cache is shared, so remember what was dropped for the other workspaces.
        queries = set(setting(f"{key}_queries")) | purge()
        progress(what, {})
        replace_jobs(db, ws, crit, queries)
        db.merge(AppSetting(key=f"{key}_queries", value_json=json.dumps(sorted(queries))))
        db.merge(AppSetting(key=f"{key}_workspaces", value_json=json.dumps(sorted(done | {ws}))))
        db.commit()


def refilter(db: Session, crit: Criteria, ws: int, jobs: Optional[list[Job]] = None) -> int:
    """Re-apply filters to stored jobs (no network). Returns #changed."""
    changed = 0
    for job in jobs if jobs is not None else db.query(Job).filter(Job.workspace_id == ws).all():
        if job.posting is not None and job.posting.not_a_job and not job.has_own_ad:
            reason = NOT_A_JOB
        elif job.excluded_reason == NOT_A_JOB:
            continue
        else:
            reason = exclusion_reason(job, crit, stage=stage_of(job))
        if reason != job.excluded_reason:
            job.excluded_reason = reason
            changed += 1
    db.commit()
    return changed


def _linked(db: Session, ws: int, posting_ids: list[int]) -> int:
    """How many of these postings the workspace has: the counts a run reports never include other users' jobs."""
    if not posting_ids:
        return 0
    return db.query(Job.id).filter(Job.workspace_id == ws, Job.posting_id.in_(posting_ids)).count()


def _process_source(db: Session, ws: int, src: Optional[CompanySource], adapter: Adapter,
                    crit: Criteria, rep: SourceReport, progress: Progress, found: set) -> None:
    postings = adapter.list_postings()
    rep.method = adapter.method
    rep.listed = len(postings)
    rep.notes.extend(getattr(adapter, "notes", []))
    now = datetime.utcnow()
    key = listing_key(adapter)
    fallback, own_label = _labels(src)
    seen: set[str] = set()
    closed: list[int] = []
    detail_budget = MAX_DETAILS_PER_SOURCE

    for i, p in enumerate(postings, 1):
        if not p.url or p.url in seen:
            continue
        seen.add(p.url)
        progress(f"{rep.label}: {i}/{len(postings)}", {"source": rep.label, "done": i, "total": len(postings)})
        named_by_label = (p.company or "") in ("", fallback)
        p.company = p.company or fallback
        job, is_new = upsert_posting(db, ws, p, adapter.source, src.id if src is not None else None, crit, now, key)
        if own_label and named_by_label:
            job.company = own_label  # the name the user gave the source: this workspace's own
        rep.new += is_new
        db.flush()  # (a new job has no id until now)
        found.add(job.id)
        posting = job.posting

        if posting.not_a_job:
            job.excluded_reason = NOT_A_JOB
            db.commit()
            continue
        listing_reason = exclusion_reason(job, crit, stage="listing")
        if listing_reason is None and adapter.has_details and pool.needs_detail(posting, now):
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
                    if isinstance(exc, FetchError) and exc.status in (404, 410) and pool.close(posting, "gone", now):
                        closed.append(posting.id)  # the ad's page is gone: closed for everyone
                else:
                    rep.details_fetched += 1
                    if detailed is None:
                        pool.mark_not_a_job(posting, now)
                        job.excluded_reason = NOT_A_JOB
                        db.commit()
                        rep.excluded += 1
                        continue
                    pool.apply_detail(posting, detailed, now)
                    pool.refresh_link(job, crit)

        job.excluded_reason = exclusion_reason(job, crit, stage=stage_of(job))
        if job.excluded_reason:
            rep.excluded += 1
        else:
            rep.kept += 1
        db.commit()

    if adapter.complete_listing and postings:
        closed += pool.close_unlisted(db, key, seen, now)
        db.commit()
    rep.closed = _linked(db, ws, closed)


def refresh_ghost_signs(db: Session, now: datetime) -> None:
    """The ghost-job signs that depend on the pool around each posting (backend.ghost). A bug there must not fail a
    search, so it only costs the signs until the next run."""
    from backend import ghost

    try:
        ghost.refresh(db, now)
        db.commit()
    except Exception as exc:
        db.rollback()
        log.error("ghost-job signs failed: %s", exc.__class__.__name__)


def run_search(ws: int, progress: Progress = lambda stage, info: None) -> dict:
    db = SessionLocal()
    client = PoliteClient()
    try:
        crit = load_criteria(db, ws)
        repair_geocodes(db, ws, crit, progress)
        name_browser_sources(db, ws)
        seek_addresses(db, ws)
        reports: list[SourceReport] = []
        found: set[int] = set()  # jobs this run listed, for scoring just those
        for src, adapter in build_adapters(db, ws, client):
            label = (src.label or src.careers_url) if src is not None else "SEEK"
            rep = SourceReport(label=label, url=src.careers_url if src is not None else "https://www.seek.com.au")
            reports.append(rep)
            progress(f"{label}: listing jobs", {"source": label})
            try:
                _process_source(db, ws, src, adapter, crit, rep, progress, found)
            except (SourceError, FetchError) as exc:
                db.rollback()
                rep.error = str(exc)
                rep.notes.extend(getattr(adapter, "notes", []))
            except Exception as exc:  # a bug in one source must not stop the others
                db.rollback()
                log.error("source %s failed: %s", src.id if src is not None else "seek", exc.__class__.__name__)
                rep.error = f"unexpected error: {exc.__class__.__name__}: {exc}"
            if src is not None:
                src.last_run = datetime.utcnow()
                src.last_count = rep.listed
                src.last_error = rep.error
                src.last_method = rep.method or None
                db.commit()
        progress("checking jobs other searches found", {})
        now = datetime.utcnow()
        stale = pool.sweep(db, now)
        refresh_ghost_signs(db, now)
        matched = pool.match_pool(db, ws, crit, now)
        pool.refresh_links(db, ws, crit)
        db.commit()
        progress("re-applying filters to stored jobs", {})
        refilter(db, crit, ws)
        purge_cache()
        totals = {k: sum(getattr(r, k) for r in reports)
                  for k in ("listed", "new", "details_fetched", "details_deferred", "excluded", "kept", "closed")}
        totals["closed"] += _linked(db, ws, stale)
        totals["pool_matched"] = matched
        return {"per_source": [r.__dict__ for r in reports], "totals": totals, "job_ids": sorted(found)}
    finally:
        client.close()
        db.close()

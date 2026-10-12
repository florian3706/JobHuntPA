"""The shared job pool (docs/design/accounts.md §14).

Every job ad any search scrapes is stored once, as a ``Posting``; each workspace links to the postings it has
through its ``Job`` rows, which hold everything personal (status, hidden, exclusion, placement, office, verdict)
and the workspace's own content (a privately uploaded full ad, details taken from a merged duplicate). Only this
module writes postings, so nothing a user adds or decides can reach the pool.

- ``upsert_scraped``: store what a listing shows (the http cache keeps listings cheap).
- ``needs_detail``: the freshness rule: fetch a detail page when the pool has no full description yet, or its
  full description is older than DETAIL_REFRESH. Two workspaces listing one posting cost one fetch.
- ``link``: the workspace's job for a posting (made when new; a private job with the URL is attached).
- ``match_pool``: open postings other searches found that match a workspace's own titles/keywords and filters.
- Closing (global, on the posting, never because one user's results lacked it): ``close`` (404/410: gone),
  ``close_unlisted`` (missing from a complete listing of the same listing) and ``sweep`` (not listed anywhere for
  STALE_AFTER).
- ``import_private``: postings from a page the user saved: private unless the pool already has them.
- ``add_listed``: a job from a results page the user had open: added when the workspace lacks it, never changed.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, contains_eager, defer, joinedload

from backend import crypto, tasks
from backend.adapters.base import Posting as Scraped
from backend.db import DETAIL_RANK, Job, Posting

DETAIL_REFRESH = timedelta(days=30)     # a full description older than this is fetched again (while listed)
STALE_AFTER = timedelta(days=45)        # not listed anywhere for this long: closed
PURGE_CLOSED_AFTER = timedelta(days=180)  # closed this long and linked by no workspace: deleted
MAX_POOL_MATCHES = 300                  # pool postings linked to one workspace per run (newest listed first)

# What a listing or detail page says about an ad (the scraped fields of a Posting).
SCRAPED = ("title", "company", "external_id", "location_text", "country", "work_mode", "employment_type",
           "salary_text", "salary_min", "salary_max", "posted_at", "industry", "description", "detail_status",
           "detail_fetched_at")


# --------------------------------------------------------------------------
# Posting content (shared by Posting rows and a job's own values)
# --------------------------------------------------------------------------

def merge_posting(target, p: Scraped, now: datetime) -> set[str]:
    """Copy listing/detail data onto ``target`` (a Posting, or a Job whose own values it then sets) without
    losing better data. Returns the names of the fields that changed."""
    from backend.geo import guess_country

    before = {name: getattr(target, name) for name in SCRAPED}
    target.title = p.title or target.title
    target.company = p.company or target.company
    target.external_id = p.external_id or target.external_id
    if p.location_text and p.location_text != target.location_text:
        target.location_text = p.location_text
    target.country = p.country or target.country or guess_country(target.location_text or "")
    if p.work_mode and p.work_mode != "unknown":
        target.work_mode = p.work_mode
    elif p.detail_status == "full" and target.detail_status != "full":
        target.work_mode = "unknown"  # re-classify from the new description
    target.employment_type = p.employment_type or target.employment_type
    target.salary_text = p.salary_text or target.salary_text
    if p.salary_min:
        target.salary_min = int(p.salary_min)
    if p.salary_max:
        target.salary_max = int(p.salary_max)
    target.posted_at = p.posted_at or target.posted_at
    target.industry = p.industry or target.industry
    if p.description and DETAIL_RANK.get(p.detail_status, 0) >= DETAIL_RANK.get(target.detail_status or "none", 0):
        target.description = p.description
        target.detail_status = p.detail_status
        if p.detail_status == "full":
            target.detail_fetched_at = now
    return {name for name in SCRAPED if getattr(target, name) != before[name]}


def derive(target) -> None:
    """Fields that follow from the ad's text alone: the country guess, office days, work mode and the description
    hash. Computed once on a posting; on a job only when it reads its own ad (``Job.has_own_ad``)."""
    from backend.geo import classify_work_mode, guess_country, parse_office_days
    from backend.scraping.text import description_hash

    title, where, text = target.title or "", target.location_text or "", target.description or ""
    target.country = target.country or guess_country(where)
    target.office_days = parse_office_days(title, where, text)
    if (target.work_mode or "unknown") == "unknown":
        target.work_mode = classify_work_mode(title, where, text)
    if target.work_mode == "unknown" and target.office_days:
        target.work_mode = "hybrid" if target.office_days < 5 else "onsite"
    target.description_hash = description_hash(text)


def _changed(posting: Posting) -> None:
    from backend import ghost

    old_hash = posting.description_hash
    derive(posting)
    if posting.description_hash != old_hash:
        posting.ad_ghost_json = None  # the ad's wording changed: the old AI reading no longer applies
    ghost.stamp(posting)  # the key that finds earlier ads of the role, and the signs in the ad's wording
    posting.rev = (posting.rev or 0) + 1


def posting_by_url(db: Session, url: Optional[str]) -> Optional[Posting]:
    return db.query(Posting).filter(Posting.url == url).first() if url else None


def upsert_scraped(db: Session, p: Scraped, source: str, listing_key: Optional[str],
                   now: datetime) -> tuple[Posting, bool]:
    """Store what a listing says about an ad. Returns (posting, new to the pool). A closed posting listed
    again reopens."""
    posting = posting_by_url(db, p.url)
    is_new = posting is None
    if is_new:
        posting = Posting(url=p.url, first_seen=now, listed_since=now, detail_status="none", rev=0)
        db.add(posting)
    changed = merge_posting(posting, p, now)
    if posting.source != source:
        posting.source = source
        changed.add("source")
    if changed or is_new:
        _changed(posting)
    posting.last_listed_at = now
    if listing_key:
        posting.listing_key = listing_key
    if posting.closed_at is not None:
        posting.reopened_from = posting.close_reason  # (a "stale" run isn't a repost: backend.ghost)
        posting.closed_at = posting.close_reason = None
        posting.listed_since, posting.ghost_checked_at = now, None  # listed again: a new run (backend.ghost)
    return posting, is_new


def apply_detail(posting: Posting, detailed: Scraped, now: datetime) -> None:
    """A fetched detail page. Fetched (and found) counts as fresh even when nothing changed."""
    if merge_posting(posting, detailed, now):
        _changed(posting)
    if posting.detail_status == "full":
        posting.detail_fetched_at = now


def needs_detail(posting: Posting, now: datetime) -> bool:
    """The freshness rule: fetch the detail page when the pool has no full description yet (new to the pool,
    deferred, or the fetch failed), or the full description is older than DETAIL_REFRESH."""
    if posting.not_a_job or posting.closed_at is not None:
        return False
    if posting.detail_status != "full":
        return True
    return posting.detail_fetched_at is None or posting.detail_fetched_at < now - DETAIL_REFRESH


def mark_not_a_job(posting: Posting, now: datetime) -> None:
    posting.not_a_job = True
    posting.detail_status, posting.detail_fetched_at = "full", now
    posting.rev = (posting.rev or 0) + 1


def close(posting: Posting, reason: str, now: datetime) -> bool:
    """Close a posting for everyone. Returns whether it was open."""
    if posting.closed_at is not None:
        return False
    posting.closed_at, posting.close_reason = now, reason
    return True


def close_unlisted(db: Session, listing_key: str, seen: set[str], now: datetime) -> list[int]:
    """A complete listing (every open job of that listing, whoever asks) no longer shows these postings: closed.
    Only postings this same listing listed last; returns the ids closed."""
    q = db.query(Posting).filter(Posting.listing_key == listing_key, Posting.closed_at.is_(None))
    return [p.id for p in q.all() if p.url not in seen and close(p, "unlisted", now)]


def sweep(db: Session, now: datetime) -> list[int]:
    """Close postings no listing has shown for STALE_AFTER and delete long-closed ones no workspace links.
    Returns the ids closed."""
    stale = db.query(Posting).filter(Posting.closed_at.is_(None), Posting.last_listed_at < now - STALE_AFTER).all()
    closed = [p.id for p in stale if close(p, "stale", now)]
    db.flush()  # (the session doesn't autoflush) so pool matching right after sees them closed
    linked = select(Job.posting_id).where(Job.posting_id.isnot(None))
    (db.query(Posting).filter(Posting.closed_at < now - PURGE_CLOSED_AFTER, Posting.id.notin_(linked))
     .delete(synchronize_session=False))
    return closed


# --------------------------------------------------------------------------
# Workspace links
# --------------------------------------------------------------------------

# Sealed job columns the job list's cards never read: left out of its query, as each costs a decryption per job.
CARD_UNUSED = (Job._external_id, Job._description_hash, Job._detail_fetched_at, Job._industry, Job.office_checked_at,
               Job.office_lng, Job.ad_ghost_json)  # (ghost.assess_jobs loads the last one itself)


def jobs_query(db: Session, *, descriptions: bool = False, card: bool = False):
    """``db.query(Job)`` with each job's posting loaded alongside (one query), the descriptions left out unless
    asked for (the job list doesn't show them). ``card``: only what a job card shows (``CARD_UNUSED`` is left
    out: reading one of those then costs a query per job)."""
    if descriptions:
        return db.query(Job)
    options = [defer(Job._description), joinedload(Job.posting).defer(Posting.description)]
    if card:
        options += [defer(column) for column in CARD_UNUSED]
    return db.query(Job).options(*options)


def job_states(db: Session, ws: int) -> list[tuple]:
    """(id, hidden, excluded_reason, closed, detail_status) for each of the workspace's listed jobs (not merged
    copies), from a column query: for deciding in Python, cheaply, which jobs to score or count. ``closed`` and
    ``detail_status`` are the effective ones (the workspace's own, else the posting's)."""
    rows = (db.query(Job.id, Job.hidden, Job.excluded_reason, Job.posting_id, Job._closed_at, Job._detail_status,
                     Posting.closed_at, Posting.detail_status)
            .outerjoin(Posting, Posting.id == Job.posting_id)
            .filter(Job.workspace_id == ws, Job.duplicate_of.is_(None)).order_by(Job.id))
    out = []
    for job_id, hidden, reason, posting_id, own_closed, own_detail, closed, detail in rows:
        if posting_id is None:
            closed, detail = own_closed, own_detail
        elif own_detail and own_detail != "none":
            detail = own_detail
        out.append((job_id, bool(hidden), reason, closed is not None, detail or "none"))
    return out


def job_by_url(db: Session, ws: int, url: Optional[str]) -> Optional[Job]:
    """The workspace's job with this URL, if any: found by ``url_index``, the blind index of its URL under the
    owner's key (a private job's URL is sealed; a pool link's is its posting's)."""
    if not url:
        return None
    return db.query(Job).filter(Job.workspace_id == ws, Job.url_index == crypto.blind_index("url", url)).first()


def attach(job: Job, posting: Posting) -> None:
    """A job that kept its content itself (private, or not migrated) gets its posting. Own values equal to the
    posting's are dropped; the others stay this workspace's private overrides."""
    from backend.db import JOB_CONTENT

    own = {name: getattr(job, "_" + name) for name in JOB_CONTENT}
    job.posting, job.origin, job.posting_rev = posting, "pool", None
    job._url = None  # the posting's URL now (the same one)
    job._closed_at = None
    if DETAIL_RANK.get(own["detail_status"] or "none", 0) <= DETAIL_RANK.get(posting.detail_status, 0):
        for name in ("description", "description_hash", "detail_status", "detail_fetched_at", "office_days"):
            own[name] = None  # the pool's ad is as good: read that one
    for name, value in own.items():
        setattr(job, name, value)  # the property drops values equal to the posting's


def link(db: Session, ws: int, posting: Posting, now: datetime,
         source_id: Optional[int] = None) -> tuple[Job, bool]:
    """The workspace's job for this posting: made when new (``first_seen`` now), attached when the workspace had
    the URL as a private job. Returns (job, new to the workspace). A new link made during a run records it
    (``first_run_id``, from ``tasks.current_run_id``); one made outside a run keeps NULL."""
    job = job_by_url(db, ws, posting.url)
    is_new = job is None
    if is_new:
        job = Job(workspace_id=ws, origin="pool", status="to_review", first_seen=now, last_seen=now,
                  first_run_id=tasks.current_run_id.get())
        job.posting = posting
        db.add(job)
    elif job.posting_id != posting.id or job.posting is None:
        attach(job, posting)
    if source_id is not None or is_new:
        job.source_id = source_id
    return job, is_new


def take_ad_office(job: Job) -> bool:
    """The office the public ad names, read once for everyone: copied to a job that reads that ad. Returns whether
    the posting has been read for an office."""
    from backend.offices import SOURCE_RANK, apply_office

    posting = job.posting
    if posting is None or job.has_own_ad or posting.ad_office_checked_at is None:
        return False
    if job.office_checked_at is None:
        job.office_checked_at = posting.ad_office_checked_at
    if posting.ad_office_text and SOURCE_RANK.get(job.office_source or "", 0) < SOURCE_RANK["ad"]:
        apply_office(job, posting.ad_office_text, "ad")
    return True


def share_ad_fact(job: Job, name: str, value) -> None:
    """Something an LLM read from the job's ad (e.g. the employment type): shared when it read the public ad,
    the workspace's own when it read a private one."""
    target = job if job.has_own_ad else job.posting
    setattr(target, name, value)


def refresh_link(job: Job, crit) -> None:
    """Bring a job up to date with its posting after the posting changed (another workspace's search, a detail
    fetch): placed again, the ad's office taken over. Filtering is the caller's."""
    from backend.pipeline import enrich

    posting = job.posting
    if posting is None or job.posting_rev == posting.rev:
        return
    if job.office_lat is None:
        job.lat = job.lng = None  # the location may have changed: place it again (geocodes are cached)
    take_ad_office(job)
    enrich(job, crit)
    job.posting_rev = posting.rev


def refresh_links(db: Session, ws: int, crit) -> int:
    """Refresh the workspace's jobs whose postings changed since it last looked."""
    jobs = (db.query(Job).join(Posting, Job.posting_id == Posting.id).options(contains_eager(Job.posting))
            .filter(Job.workspace_id == ws, or_(Job.posting_rev.is_(None), Job.posting_rev != Posting.rev)).all())
    for job in jobs:
        refresh_link(job, crit)
    return len(jobs)


def target_match(title: str, description: str, crit) -> bool:
    """A positive match on the workspace's own words: a target title in the title, or an include keyword in the
    title or description. Required for pool matches, so a workspace never sees postings that only match other
    users' searches (and one with no titles or keywords sees none)."""
    from backend.filters import first_match

    return bool(first_match(title, crit.titles) or first_match(f"{title}\n{description}", crit.keywords_include))


def match_pool(db: Session, ws: int, crit, now: datetime, limit: int = MAX_POOL_MATCHES) -> int:
    """Link open pool postings that match this workspace's search profile and pass its filters (the postings
    other users' searches found). Returns how many were linked."""
    from backend.filters import exclusion_reason
    from backend.pipeline import enrich

    if not (crit.titles or crit.keywords_include):
        return 0
    linked_ids = {i for (i,) in db.query(Job.posting_id).filter(Job.workspace_id == ws, Job.posting_id.isnot(None))}
    # The workspace's URLs, as blind indexes (a private job's URL is sealed).
    linked_urls = {i for (i,) in db.query(Job.url_index).filter(Job.workspace_id == ws, Job.url_index.isnot(None))}
    candidates = (db.query(Posting).filter(Posting.closed_at.is_(None), Posting.not_a_job.is_(False))
                  .order_by(Posting.last_listed_at.desc(), Posting.id.desc()).all())
    matched = 0
    for posting in candidates:
        if posting.id in linked_ids or posting.closed_at is not None:
            continue
        if not target_match(posting.title or "", posting.description or "", crit):
            continue
        if crypto.blind_index("url", posting.url) in linked_urls:
            continue
        job = Job(workspace_id=ws, origin="pool", status="to_review", first_seen=now,
                  last_seen=posting.last_listed_at, posting_rev=posting.rev, first_run_id=tasks.current_run_id.get())
        job.posting = posting  # still transient: only added if it passes
        take_ad_office(job)
        enrich(job, crit)
        stage = "full" if job.detail_status in ("full", "summary") else "listing"
        if exclusion_reason(job, crit, stage=stage) is not None:
            continue
        db.add(job)
        matched += 1
        if matched >= limit:
            break
    return matched


# --------------------------------------------------------------------------
# Pages the user saved (private)
# --------------------------------------------------------------------------

def apply_own(job: Job, p: Scraped, now: datetime) -> set[str]:
    """A page the user saved, as this workspace's own values (never the pool's). Returns the changed fields."""
    changed = merge_posting(job, p, now)
    if "location_text" in changed and job.office_lat is None:
        job.lat = job.lng = None  # place it again
    return changed


def import_private(db: Session, ws: int, p: Scraped, source: str, now: datetime) -> tuple[Job, bool]:
    """One job from a page the user saved. The workspace's job if it has the URL; else a link to the pool's
    posting if the pool has it; else a private job, which never enters the pool. The page's values are the
    workspace's own either way. Returns (job, new to the workspace)."""
    job = job_by_url(db, ws, p.url)
    is_new = job is None
    if is_new:
        posting = posting_by_url(db, p.url)
        if posting is not None and not posting.not_a_job:
            job, _ = link(db, ws, posting, now)
        else:
            job = Job(workspace_id=ws, url=p.url, origin="private", status="to_review", first_seen=now, last_seen=now)
            job.source = source
            db.add(job)
    apply_own(job, p, now)
    if job.posting is None:
        job.closed_at = None
    return job, is_new


def add_listed(db: Session, ws: int, p: Scraped, source: str, now: datetime) -> tuple[Job, bool]:
    """One job from a results page the user had open (a card: the listing's summary). It only ever adds. The
    workspace's job with the URL stays exactly as it is, since a card says less than what is stored; a posting the
    pool has is linked and read as the pool has it; else a private job with the card's values, which never enters
    the pool. Returns (job, new to the workspace)."""
    job = job_by_url(db, ws, p.url)
    if job is not None:
        return job, False
    posting = posting_by_url(db, p.url)
    if posting is not None and not posting.not_a_job:
        return link(db, ws, posting, now)
    job = Job(workspace_id=ws, url=p.url, origin="private", status="to_review", first_seen=now, last_seen=now)
    job.source = source
    db.add(job)
    apply_own(job, p, now)
    return job, True


def unlinked_count(db: Session) -> int:
    """Postings no workspace links (for tests and housekeeping)."""
    linked = select(Job.posting_id).where(Job.posting_id.isnot(None))
    return db.query(func.count(Posting.id)).filter(Posting.id.notin_(linked)).scalar() or 0


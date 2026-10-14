"""Where a job's office is, when its ad only names a city.

Most ads say "Sydney NSW", which the geocoder places at Town Hall, so the
location filter used to treat every such office as being in the CBD. The
office is looked up, best source first:

- user: the user typed an address or suburb, or picked one of the
  employer's offices (never overwritten);
- ad: the office named in the full ad (read when the job is scored, or by
  Fill missing info for ads scored before);
- company: the employer's office in that city from company research (or,
  if research found none there, a web-search agent looking just for that
  city). With several offices there, the one closest to the user's commute
  pins (home, hybrid and onsite pins) is used; the user can pick another.

Once the employer is researched, the job page lists every office research
knows (``known_offices``, spec §1.9); the one picked is stored as the user's
office, and the page's Google Maps links (backend/commute.py) go to it. An
office research found may be picked even when it is far from the ad's city.

The office's coordinates replace the ad's city for the location filter and
distance. Ads that stay unplaced are flagged "office unknown"; recruitment
agencies rarely name the client, so those are left to the user, who can set
the office or mark the location OK or too far (``location_verdict``).

    PUT /api/jobs/{id}/office   {office?: str | null, verdict?: "ok" | "too_far" | null}
                                signed in; the job must be in the request's workspace (else 404)
"""
from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import Callable, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from backend.db import CompanyProfile, Job, SessionLocal, company_key, get_db
from backend.geo import distance_km, geocode, is_city_only
from backend.threads import submit
from backend.workspaces import current_workspace, owned

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/jobs", tags=["offices"])

SOURCE_RANK = {"company": 1, "ad": 2, "user": 3}
MAX_FROM_CITY_KM = 80        # an office further than this from the ad's city is a wrong match
OFFICES_REFRESH = timedelta(days=90)
_STATE = re.compile(r"\b(NSW|VIC|QLD|WA|SA|TAS|ACT|NT)\b")
_STATE_OR_COUNTRY = re.compile(r"\b(NSW|VIC|QLD|WA|SA|TAS|ACT|NT|New South Wales|Victoria|Queensland|"
                               r"Australia|New Zealand|United Kingdom)\b", re.I)
_RECRUITER = re.compile(
    r"\b(recruit\w*|talent|staffing|personnel|resourcing|executive search|search group|hays|robert half|"
    r"michael page|page personnel|randstad|adecco|peoplebank|paxus|hudson|chandler macleod|robert walters|"
    r"sharp & carter|six degrees|hydrogen group|aurec|davidson|finite group|bluefin|halcyon knights|"
    r"opus recruitment|correlate resources|ttg|public sector people|people2people|perigon|launch recruitment)\b",
    re.I)


def looks_like_recruiter(company: str, profiles: Optional[dict[str, CompanyProfile]] = None) -> bool:
    profile = (profiles or {}).get(company_key(company or ""))
    if profile is not None and (profile.is_recruiter or profile.status == "done"):
        return bool(profile.is_recruiter)  # research (or the office search) knows
    return bool(_RECRUITER.search(company or ""))


def office_unknown(job: Job) -> bool:
    """An ad that only names a city, for a role you'd travel to, with no
    office found and no call made by the user."""
    return ((job.work_mode or "unknown") != "remote" and job.office_lat is None and not job.location_verdict
            and is_city_only(job.location_text or ""))


# --------------------------------------------------------------------------
# Placing an office
# --------------------------------------------------------------------------

def _city_point(job: Job) -> Optional[dict]:
    from backend.pipeline import locate

    return locate(job.location_text or "", []) if job.location_text else None


# Floors and suites: the geocoder knows buildings, not floors.
_UNIT = re.compile(r"\b(level|lvl|suite|ste|floor|fl|unit|shop|tower|building)\s*[\w-]+\b,?|\bL\d+\b,?|"
                   r"\b\d+(st|nd|rd|th)\s+floor\b,?|\b\d+[a-z]?\s*/\s*(?=\d)", re.I)


def office_queries(text: str, job: Job) -> list[str]:
    """Geocoder queries for an address, most precise first: "Suite 2, L1,
    30-32 Market Street, Sydney NSW 2000" -> "30-32 Market Street, Sydney NSW
    2000", "30 Market Street, Sydney NSW 2000", "Sydney NSW 2000"."""
    text = " ".join(_UNIT.sub(" ", text or "").replace("/", " ").split()).strip(" ,")
    text = re.sub(r"(,\s*)+", ", ", text).strip(" ,")
    if not text:
        return []
    if not _STATE_OR_COUNTRY.search(text):
        state = _STATE.search(job.location_text or "")
        text = f"{text}, {state.group(1)}" if state else f"{text}, Australia"
    queries = [text, re.sub(r"\b(\d+)[a-z]?\s*-\s*\d+[a-z]?\b", r"\1", text)]
    parts = [p.strip() for p in text.split(",") if p.strip()]
    if len(parts) > 1 and re.match(r"^\d", parts[0]):
        queries.append(", ".join(parts[1:]))  # the suburb, when the street isn't known
    return list(dict.fromkeys(queries))


def place_office(text: str, job: Job, near_city: bool = True) -> Optional[dict]:
    """{lat, lng} for an office address or suburb, near the ad's city (anywhere, with ``near_city`` False: for an
    office research found, which the user picked)."""
    city = _city_point(job) if near_city else None
    for query in office_queries(text, job):
        hit = geocode(query)
        if hit is None:
            continue
        if city and distance_km(hit["lat"], hit["lng"], city["lat"], city["lng"]) > MAX_FROM_CITY_KM:
            return None  # e.g. a "Mascot" somewhere else
        return {"lat": hit["lat"], "lng": hit["lng"]}
    return None


def _refresh(db: Session, job: Job) -> None:
    """Re-apply coordinates, distance and filters after the office changed."""
    from backend.filters import exclusion_reason, load_criteria
    from backend.pipeline import NOT_A_JOB, enrich

    crit = load_criteria(db, job.workspace_id)
    if job.office_lat is None:
        job.lat = job.lng = None  # back to the ad's own location
    enrich(job, crit)
    if job.excluded_reason != NOT_A_JOB:
        stage = "full" if job.detail_status in ("full", "summary") else "listing"
        job.excluded_reason = exclusion_reason(job, crit, stage=stage)


def apply_office(job: Job, text: str, source: str, near_city: bool = True) -> bool:
    """Record an office on the job (no re-filtering: the caller's). Returns whether it could be placed."""
    point = place_office(text, job, near_city)
    job.office_text, job.office_source = " ".join(text.split())[:200], source
    job.office_lat, job.office_lng = (point["lat"], point["lng"]) if point else (None, None)
    return point is not None


def set_office(db: Session, job: Job, text: Optional[str], source: str, near_city: bool = True) -> bool:
    """Record an office for a job (or clear it with text=None). A source
    never overwrites a better one (user > ad > company). Returns whether the
    office could be placed on the map."""
    current = SOURCE_RANK.get(job.office_source or "", 0)
    if text and SOURCE_RANK[source] < current:
        return job.office_lat is not None
    if not text:
        job.office_text = job.office_lat = job.office_lng = job.office_source = None
        _refresh(db, job)
        return False
    placed = apply_office(job, text, source, near_city)
    _refresh(db, job)
    return placed


# --------------------------------------------------------------------------
# From the ad
# --------------------------------------------------------------------------

OFFICE_FIELD = ('"office_location": "where the candidate would work, as stated in the ad: a street address or '
                'suburb (e.g. \'1 Denison St, North Sydney\' or \'Macquarie Park\'); empty if the ad names only a '
                'city or no place"')

AD_PROMPT = """Read the job ad and say where the job's office is.

Return ONLY a JSON object: {""" + OFFICE_FIELD + """}

Rules: use only what the ad says about where THIS role works (not head office elsewhere, other offices, or clients' sites). Never guess."""

_GENERIC = {"street", "road", "level", "floor", "office", "sydney", "melbourne", "brisbane", "perth", "adelaide",
            "canberra", "australia", "south", "north", "east", "west", "suite", "building", "tower", "the"}


def office_in_ad(office: str, ad: str) -> str:
    """The model's office, if the ad really names it ('' otherwise): its
    first part appears in the ad ("North Sydney", "1 Denison St"), or a
    distinctive word of it does ("Denison")."""
    office = " ".join(str(office or "").split())
    text = " ".join((ad or "").lower().split())
    first = office.split(",")[0].strip().lower()
    words = [w for w in re.findall(r"[a-z]{4,}", office.lower()) if w not in _GENERIC]
    if office and ((len(first) >= 4 and first in text) or any(w in text for w in words)):
        return office
    return ""


def from_ad(db: Session, job: Job, office: str) -> None:
    """Apply the office the scorer (or the ad reader) found in a job's ad. Read from the public ad, it's kept on
    the posting for every workspace (backend.pool.take_ad_office); from a privately uploaded ad, only here."""
    now = datetime.utcnow()
    job.office_checked_at = now
    office = office_in_ad(office, job.description or "")
    if not job.has_own_ad:
        job.posting.ad_office_text, job.posting.ad_office_checked_at = office, now
    if office and job.office_source != "user":
        set_office(db, job, office, "ad")


def read_ad(job: Job, cfg: dict) -> str:
    from backend.scorer import call_model, parse_json

    messages = [{"role": "system", "content": AD_PROMPT},
                {"role": "user", "content": f"Job: {job.title} at {job.company} ({job.location_text})\n\n"
                                            f"{(job.description or '')[:12000]}"}]
    return str(parse_json(call_model(messages, cfg)).get("office_location") or "")


# --------------------------------------------------------------------------
# From the employer's offices (web search)
# --------------------------------------------------------------------------

OFFICES_PROMPT = """Find the offices of ONE organisation in ONE city, using web search.

Return ONLY a JSON object, no markdown:
{"is_recruiter": false, "offices": [{"name": "short label, e.g. 'Head office' or 'Macquarie Park office'", "address": "street address with suburb, e.g. '1 Denison St, North Sydney NSW 2060'", "source_url": "page that states this address"}]}

Rules:
- "is_recruiter": true if the organisation is a recruitment or staffing agency (its ads are for clients, whose offices are elsewhere).
- Only offices in the given city and its suburbs. Use the organisation's own website (contact/locations pages), annual reports, business directories or news.
- Every source_url must be a page your search returned. Never guess an address.
- If you find none, return {"offices": []}."""


def _city_name(job: Job) -> str:
    place = re.split(r"\s*;\s*", job.location_text or "")[0]
    return re.sub(r"\b(NSW|VIC|QLD|WA|SA|TAS|ACT|NT)\b|,", " ", place).split(" - ")[-1].strip() or place


def research_offices(company: str, city: str, cfg: dict) -> dict:
    """{"is_recruiter": bool, "offices": [{name, address, url}]} (verified links only)."""
    from backend import llm_providers
    from backend.research import _norm_url, extract
    from backend.scorer import parse_json

    response = llm_providers.web_search(
        cfg, instructions=OFFICES_PROMPT, input=f"Organisation: {company}\nCity: {city}, Australia",
        context_size="low", what="office search", feature="Finding offices")
    text, seen = extract(response)
    answer = parse_json(text)
    offices = []
    for o in answer.get("offices") or []:
        if not isinstance(o, dict) or not str(o.get("address") or "").strip():
            continue
        url = str(o.get("source_url") or "")
        if _norm_url(url) not in seen:
            continue  # unverifiable: dropped
        offices.append({"name": str(o.get("name") or "").strip()[:80], "address": str(o["address"]).strip()[:200],
                        "url": url})
    return {"is_recruiter": answer.get("is_recruiter") is True, "offices": offices}


def candidates(db: Session, job: Job) -> list[dict]:
    """The employer's known offices near the ad's city: [{name, address, url,
    lat, lng}], closest to the user's commute first."""
    from backend.filters import commute_pins, load_criteria

    profile = db.get(CompanyProfile, company_key(job.company or ""))
    if profile is None or not profile.offices_json:
        return []
    out = []
    for o in json.loads(profile.offices_json):
        point = place_office(o["address"], job)  # None when it's far from the ad's city
        if point:
            out.append({**o, **point})
    pins = commute_pins(load_criteria(db, job.workspace_id).pins)
    if pins:
        out.sort(key=lambda o: min(distance_km(o["lat"], o["lng"], p["lat"], p["lng"]) for p in pins))
    return out


def _stored_offices(db: Session, job: Job) -> list[dict]:
    """The offices research stored for the job's employer: [{name, address, url}]."""
    profile = db.get(CompanyProfile, company_key(job.company or ""))
    if profile is None or not profile.offices_json:
        return []
    try:
        stored = json.loads(profile.offices_json)
    except ValueError:
        return []
    return [{"name": str(o.get("name") or ""), "address": str(o["address"]).strip(), "url": str(o.get("url") or "")}
            for o in stored if isinstance(o, dict) and str(o.get("address") or "").strip()]


def known_offices(db: Session, job: Job) -> list[dict]:
    """Every office research knows for the job's employer, for the job page to pick from (spec §1.9):
    [{name, address, url, near}]. While the office is unknown or came from research, the ones near the ad's city are
    placed (as ``candidates`` does) and listed first, closest to the user's commute pins first, with near True; the
    rest follow with near False. Otherwise nothing is looked up: they are listed as research stored them (near None)."""
    stored = _stored_offices(db, job)
    if not stored or not (office_unknown(job) or job.office_source == "company"):
        return [{**o, "near": None} for o in stored]
    near = [{"name": str(o.get("name") or ""), "address": o["address"], "url": str(o.get("url") or ""), "near": True}
            for o in candidates(db, job)]
    placed = {o["address"].lower() for o in near}
    return near + [{**o, "near": False} for o in stored if o["address"].lower() not in placed]


def is_known_office(db: Session, job: Job, text: str) -> bool:
    """Whether ``text`` is the address of one of the employer's offices research found."""
    wanted = " ".join(text.split()).lower()
    return any(" ".join(o["address"].split()).lower() == wanted for o in _stored_offices(db, job))


def assign_offices(db: Session, jobs: list[Job]) -> int:
    """Give jobs without an office from their ad the employer's office closest
    to the user's commute. Returns how many got one."""
    placed = 0
    for job in jobs:
        if job.office_source in ("ad", "user") or job.location_verdict:
            continue
        found = candidates(db, job)
        if found and set_office(db, job, found[0]["address"], "company"):
            placed += 1
    db.commit()
    return placed


def assign_company(ws: int, company: str) -> int:
    """After researching one company: place its city-only jobs in a workspace."""
    db = SessionLocal()
    try:
        key = company_key(company)
        jobs = [j for j in db.query(Job).filter(Job.workspace_id == ws, Job.duplicate_of.is_(None))
                if company_key(j.company or "") == key and (office_unknown(j) or j.office_source == "company")]
        return assign_offices(db, jobs)
    finally:
        db.close()


# --------------------------------------------------------------------------
# Fill missing info: find offices for the jobs that need one
# --------------------------------------------------------------------------

def jobs_needing_office(db: Session, ws: int, job_ids: Optional[list[int]] = None) -> list[Job]:
    q = db.query(Job).filter(Job.workspace_id == ws, Job.duplicate_of.is_(None))
    if job_ids is not None:
        q = q.filter(Job.id.in_(job_ids))
        return [j for j in q.all() if office_unknown(j)]
    # Excluded, closed and hidden are decided here, not in SQL (sealed fields, accounts design §14.6).
    return [j for j in q.all()
            if not j.excluded_reason and j.closed_at is None and not j.hidden and office_unknown(j)]


def find_offices(ws: int, job_ids: Optional[list[int]] = None,
                 progress: Callable[[str, dict], None] = lambda s, i: None) -> dict:
    """Read unread ads for an office, then look up employers' offices for
    the jobs still without one (recruitment agencies are left to the user)."""
    from backend import llm_providers
    from backend.scorer import ScorerError, get_config

    cfg = {**get_config("research"), "purpose": "offices", "workspace_id": ws}
    ad_cfg = {**get_config("scoring"), "purpose": "office_from_ad", "workspace_id": ws}
    db = SessionLocal()
    out = {"from_ads": 0, "from_companies": 0, "unknown": 0, "recruiters": 0, "errors": 0}
    try:
        from backend.pool import take_ad_office

        unread = [j for j in jobs_needing_office(db, ws, job_ids) if j.detail_status == "full" and not j.office_checked_at]
        shared = [j for j in unread if take_ad_office(j)]  # another workspace already had the public ad read
        for job in shared:
            _refresh(db, job)
            out["from_ads"] += job.office_source == "ad" and job.office_lat is not None
        db.commit()
        unread = [j for j in unread if j not in shared]
        for i, job in enumerate(unread, 1):
            progress(f"reading ads for an office {i}/{len(unread)}", {"done": i, "total": len(unread)})
            try:
                from_ad(db, job, read_ad(job, ad_cfg))
            except ScorerError as exc:
                if exc.auth:
                    raise
                out["errors"] += 1
                job.office_checked_at = datetime.utcnow()
            out["from_ads"] += job.office_source == "ad" and job.office_lat is not None
            db.commit()

        profiles = {p.key: p for p in db.query(CompanyProfile).all()}
        employers = [j for j in jobs_needing_office(db, ws, job_ids)
                     if not looks_like_recruiter(j.company or "", profiles)]
        out["recruiters"] = len(jobs_needing_office(db, ws, job_ids)) - len(employers)
        # Offices company research already found.
        out["from_companies"] += assign_offices(db, employers)

        waiting: dict[tuple[str, str], list[Job]] = {}
        for job in employers:
            if office_unknown(job):
                waiting.setdefault((company_key(job.company or ""), _city_name(job)), []).append(job)

        def searched(key: str, city: str) -> bool:
            p = profiles.get(key)
            cities = json.loads(p.offices_cities) if p is not None and p.offices_cities else []
            return bool(p and city.lower() in cities and p.offices_checked_at
                        and p.offices_checked_at > datetime.utcnow() - OFFICES_REFRESH)

        todo = list(waiting.items())
        lookups = [(key, jobs[0].company) for key, jobs in todo if not searched(*key)]
        if lookups and llm_providers.web_search_problem(cfg, "Finding offices"):
            lookups = []  # this provider has no web search: skip the lookups instead of failing each one
        with ThreadPoolExecutor(max_workers=cfg["concurrency"]) as pool:
            futures = {submit(pool, research_offices, company, key[1], cfg): (key, company) for key, company in lookups}
            for n, fut in enumerate(as_completed(futures), 1):
                (key, city), company = futures[fut]
                progress(f"looking up offices {n}/{len(lookups)} (last: {company})", {"done": n, "total": len(lookups)})
                try:
                    found = fut.result()
                except ScorerError as exc:
                    if exc.auth:
                        raise
                    out["errors"] += 1
                    continue
                # A profile row may not exist yet; it stays "not researched" until Fill missing info runs.
                row = db.get(CompanyProfile, key) or CompanyProfile(key=key, name=company, status="missing")
                known = json.loads(row.offices_json) if row.offices_json else []
                seen = {o["address"].lower() for o in known}
                row.offices_json = json.dumps(known + [o for o in found["offices"] if o["address"].lower() not in seen])
                row.offices_checked_at = datetime.utcnow()
                row.offices_cities = json.dumps(sorted(set(json.loads(row.offices_cities or "[]")) | {city.lower()}))
                row.is_recruiter = bool(row.is_recruiter or found["is_recruiter"])
                db.merge(row)
                db.commit()
                profiles[key] = db.get(CompanyProfile, key)
        db.expire_all()
        for (key, _city), jobs in todo:
            if looks_like_recruiter(jobs[0].company or "", profiles):
                out["recruiters"] += len(jobs)  # its own office isn't where the job is
                continue
            out["from_companies"] += assign_offices(db, jobs)
        out["unknown"] = len(jobs_needing_office(db, ws, job_ids))
        return out
    finally:
        db.close()


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------

class OfficeUpdate(BaseModel):
    office: Optional[str] = Field(default=None, max_length=200)
    verdict: Optional[Literal["ok", "too_far"]] = None


@router.put("/{job_id}/office")
def update_office(job_id: int, payload: OfficeUpdate, db: Session = Depends(get_db),
                  ws: int = Depends(current_workspace)):
    """Set or clear the office (by address or suburb) and/or the user's own
    call on the location. Only the fields sent are changed."""
    from backend.app import get_job

    job = owned(db, Job, job_id, ws, "Job")
    sent = payload.model_fields_set
    if "office" in sent:
        if payload.office and payload.office.strip():
            # One of the employer's own offices may be far from the ad's city; anything typed must be near it.
            near_city = not is_known_office(db, job, payload.office)
            if not set_office(db, job, payload.office, "user", near_city):
                db.rollback()
                where = f" near {job.location_text or 'the job'}" if near_city else ""
                raise HTTPException(status_code=422, detail=(
                    f"Couldn't find \"{payload.office.strip()}\" on the map{where}. "
                    "Try a street address with suburb, or just the suburb."))
        else:
            set_office(db, job, None, "user")
    if "verdict" in sent:
        job.location_verdict = payload.verdict
        _refresh(db, job)
    db.commit()
    return get_job(job_id, 0, db, ws)

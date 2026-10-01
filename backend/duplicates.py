"""Duplicate job ads: the same job advertised on several sites (or twice on one).

A scan compares a workspace's jobs in pairs. Only pairs that share an
employer name or a job title are compared, so it stays fast. Each pair is:

- sure: same employer, same location, same job title and either matching
  ad text or a title specific enough to identify one job ("Implementation
  Manager, Enterprise SaaS", not "Product Manager"). Merged automatically;
  the user can still split them.
- possible: probably the same job, but not certain (generic title, ad text
  only partly alike, several similar ads on one site...). Listed for the
  user to decide.
- otherwise ignored.

Merging keeps every row. The copies get ``duplicate_of`` = the job that
stays listed, so later searches still recognise their URLs and keep them
merged. The listed job is the copy with the best data (full ad first);
the user's status, cover letter and fit score move to it, and details
only a copy has (salary, location, work mode) fill its gaps.

Decisions live in ``job_duplicates``: merged pairs (auto or user) and
distinct pairs (the user said they are different jobs: never suggested
again).

    GET  /api/duplicates              pairs to check + automatic merges
    POST /api/duplicates/scan         look for duplicates now
    POST /api/duplicates/decide       {a, b, decision: merge | distinct | confirm}
    POST /api/jobs/merge              {ids}: merge these jobs into one
    POST /api/jobs/{id}/unmerge       split a merged copy back out
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import datetime
from itertools import combinations
from typing import Iterable, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from backend.db import CoverLetter, FitResult, Job, JobDuplicate, SessionLocal, get_db
from backend.geo import distance_km, guess_country
from backend.workspaces import current_workspace, owned

router = APIRouter(prefix="/api", tags=["duplicates"])

NOT_A_JOB = "not a job posting page"  # backend.pipeline.NOT_A_JOB (import cycle)

# --------------------------------------------------------------------------
# Comparing two ads
# --------------------------------------------------------------------------

_WORD = re.compile(r"[a-z0-9]+")
_COMPANY_NOISE = {"pty", "ltd", "limited", "inc", "incorporated", "llc", "plc", "gmbh", "corp", "corporation",
                  "co", "company", "group", "holdings", "the", "com", "net", "io", "org", "www"}
_TITLE_NOISE = {"and", "the", "of", "for", "in", "a", "an", "to", "with", "at", "on", "or", "ftc", "fixed", "term",
                "contract", "permanent", "perm", "full", "time", "fulltime", "part", "parttime", "temporary", "temp",
                "maternity", "parental", "leave", "cover", "remote", "hybrid", "onsite", "job", "role", "roles",
                "position", "opportunity", "immediate", "start", "urgent", "new", "based", "multiple"}
SENIORITY = {"senior", "sr", "junior", "jr", "lead", "principal", "head", "associate", "graduate", "intern",
             "staff", "chief", "deputy", "assistant", "avp", "vp", "svp", "evp"}
_DURATION = re.compile(r"\b\d+\s*-?\s*(?:months?|mths?|mos?|years?|yrs?|weeks?|wks?)\b")
_PLACE_NOISE = {"nsw", "vic", "qld", "wa", "sa", "tas", "act", "nt", "australia", "australian", "new", "south",
                "wales", "victoria", "queensland", "western", "northern", "territory", "capital", "remote",
                "hybrid", "office", "in", "person", "or", "and", "onsite", "on", "site", "work", "from", "home",
                "anywhere", "greater", "area", "region", "metro", "metropolitan", "cbd", "city", "locations",
                "location", "based", "united", "states", "kingdom", "usa", "more", "countries", "the", "of"}
NEARBY_KM = 60
SAME_PLACE_KM = 5
SHINGLE = 4
MIN_WORDS = 40          # shorter ads (listing summaries) aren't compared word for word
SAME_TEXT_MIN_CHARS = 100


def _words(text: str) -> list[str]:
    return _WORD.findall((text or "").lower())


def company_names(name: str) -> list[tuple[str, ...]]:
    """Ways to write an employer's name, as word tuples: "Commonwealth Scientific
    ... (CSIRO)" -> the full name and "csiro"; "Profitable Tradie - Trades
    Business Specialist" -> that and "profitable tradie"."""
    name = name or ""
    variants = [re.sub(r"\([^)]*\)", " ", name), *re.findall(r"\(([^)]*)\)", name)]
    variants += [re.split(r"\s[-–—|]\s", variants[0])[0]]
    out = []
    for v in variants:
        words = tuple(w for w in _words(v) if w not in _COMPANY_NOISE)
        if words and words not in out:
            out.append(words)
    return out


def same_company(a: list[tuple[str, ...]], b: list[tuple[str, ...]]) -> bool:
    for x in a:
        for y in b:
            if x == y or "".join(x) == "".join(y):
                return True
            small, big = (x, y) if len(x) < len(y) else (y, x)
            if set(small) <= set(big) and max(len(w) for w in small) >= 4:
                return True
    return False


def place_words(location: str) -> set[str]:
    return {w for w in _words(location) if len(w) > 2 and not w.isdigit() and w not in _PLACE_NOISE}


def title_words(title: str, location: str = "") -> frozenset[str]:
    """Words that identify the job: no contract terms, no durations, and not
    the city when a site appends it ("Program Manager - Sydney")."""
    text = _DURATION.sub(" ", (title or "").lower().replace("&", " and "))
    words = [w for w in _words(text) if w not in _TITLE_NOISE]
    places = place_words(location)
    kept = [w for w in words if w not in places]
    return frozenset(kept if kept else words)


def shingles(text: str) -> Optional[set[int]]:
    words = _words(text)
    if len(words) < MIN_WORDS:
        return None
    return {hash(" ".join(words[i:i + SHINGLE])) for i in range(len(words) - SHINGLE + 1)}


class Ad:
    """What a comparison needs from a job, computed once per scan."""

    def __init__(self, job: Job):
        self.job = job
        self.id = job.id
        self.source = job.source or ""
        self.companies = company_names(job.company or "")
        self.title = title_words(job.title or "", job.location_text or "")
        self.places = place_words(job.location_text or "")
        # Only a country the location names: a geocoder's guess for "In-Office
        # or Remote" is no evidence.
        self.country = guess_country(job.location_text or "") or (job.country or "" if self.places else "")
        desc = job.description or ""
        self.text_hash = job.description_hash if len(desc.strip()) >= SAME_TEXT_MIN_CHARS else None
        self._desc = desc
        self._shingles: Optional[set[int]] = None
        self._shingled = False

    @property
    def shingles(self) -> Optional[set[int]]:
        if not self._shingled:
            self._shingles, self._shingled = shingles(self._desc), True
        return self._shingles


def text_similarity(a: Ad, b: Ad) -> Optional[float]:
    """0-1 for two full ads (None when either is too short to tell). Uses the
    overlap with the shorter ad too, so one site adding boilerplate around
    the same text still counts as alike."""
    sa, sb = a.shingles, b.shingles
    if sa is None or sb is None:
        return None
    common = len(sa & sb)
    return max(common / len(sa | sb), 0.85 * common / min(len(sa), len(sb)))


def locations_match(a: Ad, b: Ad) -> Optional[str]:
    """How the two locations agree ("same location", ...), or None if they
    clearly differ (other country, other city)."""
    ja, jb = a.job, b.job
    if a.country and b.country and a.country != b.country:
        return None
    if not a.places or not b.places:
        return "location not stated on one ad"
    if a.places & b.places:
        return "same location"
    if None not in (ja.lat, ja.lng, jb.lat, jb.lng) and distance_km(ja.lat, ja.lng, jb.lat, jb.lng) <= NEARBY_KM:
        return "nearby locations"
    return None


def same_place(a: Ad, b: Ad) -> bool:
    """Stricter than locations_match, for two ads on one site: a site lists
    one ad per office ("Bella Vista, Sydney" and "Chullora, Sydney")."""
    ja, jb = a.job, b.job
    if a.places == b.places:
        return True
    return None not in (ja.lat, ja.lng, jb.lat, jb.lng) and distance_km(ja.lat, ja.lng, jb.lat, jb.lng) <= SAME_PLACE_KM


def _salary_range(job: Job) -> Optional[tuple[int, int]]:
    lo, hi = job.salary_min or job.salary_max, job.salary_max or job.salary_min
    if not lo or lo < 20000:  # hourly/daily rates aren't comparable with annual ones
        return None
    return lo, hi


def salaries_clash(a: Job, b: Job) -> bool:
    ra, rb = _salary_range(a), _salary_range(b)
    return bool(ra and rb and (ra[1] < rb[0] * 0.85 or rb[1] < ra[0] * 0.85))


def compare(a: Ad, b: Ad) -> Optional[dict]:
    """{"tier": "sure" | "possible", "score", "reasons"} or None."""
    if not a.title or not b.title:
        return None
    company = same_company(a.companies, b.companies)
    tsim = len(a.title & b.title) / len(a.title | b.title)
    if tsim < (0.5 if company else 0.85):
        return None
    same_source = a.source == b.source
    if same_source and (tsim < 0.9 or not same_place(a, b)):
        return None  # a site doesn't retitle its own ad; one ad per office = separate listings
    where = locations_match(a, b)
    same_text = bool(a.text_hash and a.text_hash == b.text_hash)
    dsim = 1.0 if same_text else text_similarity(a, b)
    if where is None and not ((dsim or 0) >= 0.9 and tsim >= 0.85):
        return None
    seniority_clash = (a.title & SENIORITY) != (b.title & SENIORITY)
    salary_clash = salaries_clash(a.job, b.job)
    specific = len(a.title - SENIORITY) >= 3

    reasons = ["same employer" if company else "different employer names"]
    reasons.append("identical job titles" if tsim == 1 else f"job titles {round(100 * tsim)}% alike")
    if same_text:
        reasons.append("identical ad text")
    elif dsim is not None:
        reasons.append(f"ad text {round(100 * dsim)}% alike")
    else:
        reasons.append("ad text not comparable (one is only a summary)")
    reasons.append(where or "different locations")
    if seniority_clash:
        reasons.append("different seniority")
    if salary_clash:
        reasons.append("salaries differ")
    if same_source:
        reasons.append("both from the same site")

    clean = where is not None and company and not seniority_clash and not salary_clash
    sure = clean and (
        (same_text and tsim >= 0.7)
        or (not same_source and tsim >= 0.9 and dsim is not None and dsim >= 0.5)
        or (not same_source and tsim == 1 and specific and (dsim is None or dsim >= 0.2))
    )
    possible = sure or same_text or (company and tsim >= 0.75) or (dsim is not None and dsim >= 0.6
                                                                    and (company or tsim >= 0.85))
    if not possible:
        return None
    score = 100 * (0.35 * tsim + 0.25 * company + 0.25 * (dsim if dsim is not None else 0.6 * tsim)
                   + 0.15 * (where is not None))
    score -= 15 * salary_clash + 10 * seniority_clash
    if sure:
        score = max(score, 90)
    return {"tier": "sure" if sure else "possible", "score": max(0, min(100, round(score))), "reasons": reasons}


# --------------------------------------------------------------------------
# Merging
# --------------------------------------------------------------------------

_DETAIL_RANK = {"none": 0, "summary": 1, "full": 2}
# The furthest-along status wins when two copies disagree.
STATUS_PROGRESS = {"to_review": 0, "shortlisted": 1, "not_interested": 2, "applied": 3, "interviewing": 4, "rejected": 5}


class Ranking:
    """Which copy of an ad is listed: the full ad first, then the employer's
    own site over job boards, then one that passes the filters, is still
    open, has a fit score, was seen first."""

    def __init__(self, db: Session, ws: int):
        self.scored = {j for (j,) in db.query(FitResult.job_id).join(Job, Job.id == FitResult.job_id)
                       .filter(Job.workspace_id == ws, FitResult.status == "ok")}
        employers: dict[str, set[str]] = defaultdict(set)
        for source, company in db.query(Job.source, Job.company).filter(Job.workspace_id == ws).distinct():
            employers[source or ""].add((company or "").lower())
        # A source listing several employers is a job board (SEEK, EdTechJobs...).
        self.boards = {s for s, names in employers.items() if len(names) >= 3} | {"seek"}

    def key(self, job: Job) -> tuple:
        first = job.first_seen.timestamp() if job.first_seen else 0
        return (_DETAIL_RANK.get(job.detail_status or "none", 0), (job.source or "") not in self.boards,
                not job.excluded_reason, job.closed_at is None, job.id in self.scored, -first, -job.id)


def group_of(db: Session, root: Job) -> list[Job]:
    return [root, *db.query(Job).filter(Job.duplicate_of == root.id, Job.id != root.id).all()]


def root_of(db: Session, job: Job) -> Job:
    seen = set()
    while job.duplicate_of and job.id not in seen:
        seen.add(job.id)
        parent = db.get(Job, job.duplicate_of)
        if parent is None or parent.workspace_id != job.workspace_id:
            job.duplicate_of = None
            break
        job = parent
    return job


def _fill_gaps(primary: Job, others: Iterable[Job]) -> None:
    """Details only a copy has: salary, coordinates, work mode, posting date."""
    for o in others:
        if not primary.salary_text and o.salary_text:
            primary.salary_text, primary.salary_min, primary.salary_max = o.salary_text, o.salary_min, o.salary_max
        if (primary.work_mode or "unknown") == "unknown" and (o.work_mode or "unknown") != "unknown":
            primary.work_mode = o.work_mode
        if primary.office_days is None and o.office_days is not None:
            primary.office_days = o.office_days
        if not primary.posted_at and o.posted_at:
            primary.posted_at = o.posted_at
        if primary.lat is None and o.lat is not None and (primary.country or "") in ("", o.country or ""):
            primary.lat, primary.lng, primary.distance_km = o.lat, o.lng, o.distance_km
            primary.country = primary.country or o.country


def _refilter(db: Session, job: Job, crit) -> None:
    from backend.filters import exclusion_reason

    if job.excluded_reason != NOT_A_JOB:
        stage = "full" if job.detail_status in ("full", "summary") else "listing"
        job.excluded_reason = exclusion_reason(job, crit, stage=stage)


def merge(db: Session, roots: list[Job], crit=None, ranking: Optional[Ranking] = None) -> Job:
    """Merge the groups of these listed jobs into one and return the job that
    stays listed. Their status, hidden flag, cover letter and fit score are
    the user's view of the job, so they carry over to it."""
    from backend.filters import load_criteria

    ws = roots[0].workspace_id
    crit = crit or load_criteria(db, ws)
    ranking = ranking or Ranking(db, ws)
    members: dict[int, Job] = {}
    for r in roots:
        for m in group_of(db, r):
            members[m.id] = m
    primary = max(members.values(), key=ranking.key)
    others = sorted((m for m in members.values() if m.id != primary.id), key=ranking.key, reverse=True)

    primary.status = max((r.status for r in roots), key=lambda s: STATUS_PROGRESS.get(s, 0))
    primary.hidden = all(r.hidden for r in roots)  # still listed if any copy was
    ids = list(members)
    root_ids = {r.id for r in roots}

    # A cover letter or fit score moves over if the listed job has none:
    # the listed jobs' own first (the user saw those), then the newest.
    if db.query(CoverLetter).filter(CoverLetter.job_id == primary.id).first() is None:
        letters = db.query(CoverLetter).filter(CoverLetter.job_id.in_(ids)).all()
        if letters:
            max(letters, key=lambda r: (r.job_id in root_ids, r.updated_at or datetime.min)).job_id = primary.id
    fit = db.query(FitResult).filter(FitResult.job_id == primary.id).first()
    if fit is None or fit.status != "ok":
        fits = db.query(FitResult).filter(FitResult.job_id.in_(ids), FitResult.status == "ok",
                                          FitResult.job_id != primary.id).all()
        if fits:
            if fit is not None:
                db.delete(fit)
                db.flush()
            max(fits, key=lambda r: (r.job_id in root_ids, r.created_at or datetime.min)).job_id = primary.id
            ranking.scored.add(primary.id)

    primary.duplicate_of = None
    for o in others:
        o.duplicate_of = primary.id
    _fill_gaps(primary, others)
    _refilter(db, primary, crit)
    db.flush()
    return primary


def _pair(a: int, b: int) -> tuple[int, int]:
    return (a, b) if a < b else (b, a)


def record(db: Session, ws: int, a: int, b: int, status: str, by: Optional[str],
           score: Optional[int] = None, reasons: Optional[list[str]] = None) -> JobDuplicate:
    ja, jb = _pair(a, b)
    row = db.query(JobDuplicate).filter(JobDuplicate.job_a == ja, JobDuplicate.job_b == jb).first()
    if row is None:
        row = JobDuplicate(workspace_id=ws, job_a=ja, job_b=jb, created_at=datetime.utcnow())
        db.add(row)
    row.status, row.decided_by = status, by
    if score is not None:
        row.score = score
    if reasons is not None:
        row.reasons_json = json.dumps(reasons)
    row.decided_at = datetime.utcnow() if status != "suggested" else None
    return row


def unmerge(db: Session, job: Job, crit=None) -> None:
    """Split a merged copy back out, and remember the user said it's a
    different job from the rest of its group."""
    from backend.filters import load_criteria

    if job.duplicate_of is None:
        raise HTTPException(status_code=400, detail="This job isn't merged with another one")
    root = root_of(db, job)
    group = group_of(db, root)
    job.duplicate_of = None
    for m in group:
        if m.id != job.id:
            record(db, job.workspace_id, job.id, m.id, "distinct", "user")
    _refilter(db, job, crit or load_criteria(db, job.workspace_id))


# --------------------------------------------------------------------------
# Scanning a workspace
# --------------------------------------------------------------------------

def _candidate_pairs(ads: list[Ad]) -> set[tuple[int, int]]:
    """Pairs worth comparing: same first word of an employer name, or the
    same title words."""
    blocks: dict[str, list[int]] = defaultdict(list)
    for ad in ads:
        for name in ad.companies:
            blocks["c:" + name[0]].append(ad.id)
        if ad.title:
            blocks["t:" + " ".join(sorted(ad.title))].append(ad.id)
    pairs = set()
    for ids in blocks.values():
        for a, b in combinations(sorted(set(ids)), 2):
            pairs.add((a, b))
    return pairs


def _unclear_matches(results: dict[tuple[int, int], dict], ads: dict[int, Ad]) -> set[tuple[int, int]]:
    """Sure pairs that aren't sure after all: an ad that matches two ads on
    another site which could be different openings (same title, same place)."""
    partners: dict[tuple[int, str], list[int]] = defaultdict(list)
    for (a, b), r in results.items():
        if r["tier"] == "sure":
            partners[(a, ads[b].source)].append(b)
            partners[(b, ads[a].source)].append(a)
    unclear = set()
    for (x, _source), ps in partners.items():
        for p, q in combinations(ps, 2):
            twins = results.get(_pair(p, q))
            if twins and twins["tier"] == "sure":
                continue  # the two are copies of each other anyway
            if locations_match(ads[p], ads[q]) is not None:
                unclear.update({_pair(x, p), _pair(x, q)})
    return unclear


def scan(db: Session, ws: int) -> dict:
    """Merge sure duplicates and refresh the list of possible ones."""
    from backend.filters import load_criteria

    crit = load_criteria(db, ws)
    ranking = Ranking(db, ws)
    jobs = [j for j in db.query(Job).filter(Job.workspace_id == ws).all() if j.excluded_reason != NOT_A_JOB]
    by_id = {j.id: j for j in jobs}
    ads = {j.id: Ad(j) for j in jobs}
    decided = db.query(JobDuplicate).filter(JobDuplicate.workspace_id == ws).all()
    distinct = {(d.job_a, d.job_b) for d in decided if d.status == "distinct"}

    def root(job_id: int) -> int:
        seen = set()
        while by_id[job_id].duplicate_of in by_id and job_id not in seen:
            seen.add(job_id)
            job_id = by_id[job_id].duplicate_of
        return job_id

    groups: dict[int, set[int]] = defaultdict(set)
    for j in jobs:
        groups[root(j.id)].add(j.id)

    def kept_apart(ra: int, rb: int) -> bool:
        return any(_pair(x, y) in distinct for x in groups[ra] for y in groups[rb])

    results = {}
    for a, b in _candidate_pairs(list(ads.values())):
        if root(a) == root(b) or (a, b) in distinct:
            continue
        r = compare(ads[a], ads[b])
        if r:
            results[(a, b)] = r
    for p in _unclear_matches(results, ads):
        r = results[p]
        r["tier"] = "possible"
        r["reasons"].append("another similar ad on the same site could be the match")

    merged = 0
    for (a, b), r in sorted(results.items(), key=lambda kv: -kv[1]["score"]):
        if r["tier"] != "sure":
            continue
        ra, rb = root(a), root(b)
        if ra == rb or kept_apart(ra, rb):
            continue
        primary = merge(db, [by_id[ra], by_id[rb]], crit, ranking)
        groups[primary.id] = groups.pop(ra) | groups.pop(rb)
        record(db, ws, a, b, "merged", "auto", r["score"], r["reasons"])
        merged += 1

    # Keep each group's best copy listed (a copy may have gained the full ad
    # or stayed open while the listed one closed) and fill its gaps.
    for rid, ids in list(groups.items()):
        if len(ids) < 2:
            continue
        best = max((by_id[i] for i in ids), key=ranking.key)
        if best.id != rid:
            primary = merge(db, [by_id[rid]], crit, ranking)
            groups[primary.id] = groups.pop(rid)
        else:
            _fill_gaps(by_id[rid], sorted((by_id[i] for i in ids if i != rid), key=ranking.key, reverse=True))
            _refilter(db, by_id[rid], crit)

    # Possible duplicates, between the jobs the user sees (group roots).
    db.query(JobDuplicate).filter(JobDuplicate.workspace_id == ws, JobDuplicate.status == "suggested").delete()
    db.flush()
    taken = {(d.job_a, d.job_b) for d in db.query(JobDuplicate).filter(JobDuplicate.workspace_id == ws)}
    best: dict[tuple[int, int], dict] = {}
    for (a, b), r in results.items():
        ra, rb = root(a), root(b)
        if ra == rb or kept_apart(ra, rb):
            continue
        key = _pair(ra, rb)
        if key in taken or not any(_active(by_id[x]) for x in key):
            continue
        if key not in best or r["score"] > best[key]["score"]:
            best[key] = r
    for (ra, rb), r in best.items():
        record(db, ws, ra, rb, "suggested", None, r["score"], r["reasons"])
    db.commit()
    return {"merged": merged, "suggested": len(best)}


def _active(job: Job) -> bool:
    return not job.excluded_reason and job.closed_at is None and not job.hidden


def scan_workspace(ws: int) -> dict:
    db = SessionLocal()
    try:
        return scan(db, ws)
    finally:
        db.close()


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------

def _job_summary(db: Session, job: Job) -> dict:
    fit = db.query(FitResult).filter(FitResult.job_id == job.id, FitResult.status == "ok").first()
    return {
        "id": job.id, "title": job.title, "company": job.company, "location_text": job.location_text,
        "source": job.source, "url": job.url, "salary_text": job.salary_text, "posted_at": job.posted_at,
        "first_seen": job.first_seen.isoformat() if job.first_seen else None,
        "detail_status": job.detail_status, "status": job.status, "closed": job.closed_at is not None,
        "hidden": bool(job.hidden), "excluded_reason": job.excluded_reason, "duplicate_of": job.duplicate_of,
        "score": fit.score if fit else None, "description": (job.description or "")[:1500],
    }


def _pair_dict(db: Session, row: JobDuplicate) -> Optional[dict]:
    a, b = db.get(Job, row.job_a), db.get(Job, row.job_b)
    if a is None or b is None:
        return None
    return {"a": _job_summary(db, a), "b": _job_summary(db, b), "score": row.score,
            "reasons": json.loads(row.reasons_json or "[]"), "status": row.status, "decided_by": row.decided_by}


@router.get("/duplicates")
def list_duplicates(db: Session = Depends(get_db), ws: int = Depends(current_workspace)):
    rows = db.query(JobDuplicate).filter(JobDuplicate.workspace_id == ws)
    suggested = [_pair_dict(db, r) for r in rows.filter(JobDuplicate.status == "suggested")
                 .order_by(JobDuplicate.score.desc())]
    auto = []
    for r in rows.filter(JobDuplicate.status == "merged", JobDuplicate.decided_by == "auto") \
            .order_by(JobDuplicate.decided_at.desc()):
        a, b = db.get(Job, r.job_a), db.get(Job, r.job_b)
        if a is None or b is None:
            continue
        root = root_of(db, a)
        # Only merges the user would notice: still merged, and listed.
        if root.id == root_of(db, b).id and _active(root):
            auto.append(_pair_dict(db, r))
    return {"suggested": [p for p in suggested if p], "auto_merged": [p for p in auto if p]}


@router.post("/duplicates/scan")
def scan_now(ws: int = Depends(current_workspace)):
    return scan_workspace(ws)


class Decision(BaseModel):
    a: int
    b: int
    decision: Literal["merge", "distinct", "confirm"]


@router.post("/duplicates/decide")
def decide(payload: Decision, db: Session = Depends(get_db), ws: int = Depends(current_workspace)):
    a = owned(db, Job, payload.a, ws, "Job")
    b = owned(db, Job, payload.b, ws, "Job")
    ra, rb = root_of(db, a), root_of(db, b)
    if payload.decision == "merge":
        if ra.id != rb.id:
            merge(db, [ra, rb])
        record(db, ws, a.id, b.id, "merged", "user")
    elif payload.decision == "confirm":
        record(db, ws, a.id, b.id, "merged", "user")
    else:
        if ra.id == rb.id:
            unmerge(db, b if b.duplicate_of is not None else a)
        record(db, ws, a.id, b.id, "distinct", "user")
    db.commit()
    return {"ok": True}


class MergeRequest(BaseModel):
    ids: list[int] = Field(min_length=2)


@router.post("/jobs/merge")
def merge_jobs(payload: MergeRequest, db: Session = Depends(get_db), ws: int = Depends(current_workspace)):
    jobs = [owned(db, Job, i, ws, "Job") for i in dict.fromkeys(payload.ids)]
    roots = list({r.id: r for r in (root_of(db, j) for j in jobs)}.values())
    if len(roots) < 2:
        raise HTTPException(status_code=400, detail="These ads are already merged")
    primary = merge(db, roots)
    for r in roots:
        if r.id != primary.id:
            record(db, ws, primary.id, r.id, "merged", "user")
    db.commit()
    return {"ok": True, "id": primary.id, "merged": len(roots)}


@router.post("/jobs/{job_id}/unmerge")
def unmerge_job(job_id: int, db: Session = Depends(get_db), ws: int = Depends(current_workspace)):
    unmerge(db, owned(db, Job, job_id, ws, "Job"))
    db.commit()
    return {"ok": True}

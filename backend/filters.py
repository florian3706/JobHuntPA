"""Job filtering: decides ``jobs.excluded_reason`` before any LLM scoring.

Rules (all phrase matching is case-insensitive and on word boundaries, so
the dealbreaker "war" does not match "software"):

- Dealbreaker industries: matched against the job's industry, company and
  title. Dealbreaker keywords + excluded keywords: title and description.
- Include: a job passes when its *title* contains one of the target titles,
  or its title/description contains one of the include keywords. (With
  neither configured, everything passes.)
- Salary floor: only applied when the salary is stated and parseable.
- Hybrid office days: when the ad states days in the office and it's more
  than your maximum, the job is excluded (unstated days pass).
- Work mode: remote needs "remote (Australia)" or "remote (global)"; roles
  based outside Australia need "remote (global)". Hybrid/onsite can be
  switched off.
- Location (non-remote): roles outside Australia are excluded. With pins,
  hybrid roles must fall inside a home/hybrid/onsite pin radius, onsite
  roles inside a home/onsite pin radius, unknown-mode roles inside any pin.
  Roles whose location can't be placed are kept. Pins with a radius of
  1000 km or more are ignored here (they would cover everywhere).

``stage="listing"`` runs the subset that works on listing data alone, so
detail pages of obviously irrelevant jobs are never fetched.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Optional

from sqlalchemy.orm import Session

from backend.geo import HOME_COUNTRY, home_distance, pins_in_range


@dataclass
class Criteria:
    titles: list[str] = field(default_factory=list)
    keywords_include: list[str] = field(default_factory=list)
    keywords_exclude: list[str] = field(default_factory=list)
    salary_floor: Optional[int] = None
    remote_aus_ok: bool = True
    remote_global_ok: bool = False
    allow_hybrid: bool = True
    max_office_days: Optional[int] = None
    allow_onsite: bool = True
    dealbreaker_industries: list[str] = field(default_factory=list)
    dealbreaker_keywords: list[str] = field(default_factory=list)
    pins: list[dict] = field(default_factory=list)


def load_criteria(db: Session, ws: int) -> Criteria:
    from backend.profile import get_dealbreakers, get_profile, list_pins

    prof, dbk = get_profile(db, ws), get_dealbreakers(db, ws)
    return Criteria(
        titles=prof["titles"],
        keywords_include=prof["keywords_include"],
        keywords_exclude=prof["keywords_exclude"],
        salary_floor=prof["salary_floor"],
        remote_aus_ok=prof["remote_aus_ok"],
        remote_global_ok=prof["remote_global_ok"],
        allow_hybrid=prof["allow_hybrid"],
        max_office_days=prof["max_office_days"],
        allow_onsite=prof["allow_onsite"],
        dealbreaker_industries=dbk["industries"],
        dealbreaker_keywords=dbk["keywords"],
        pins=list_pins(db, ws),
    )


@lru_cache(maxsize=512)
def _phrase_re(phrase: str) -> re.Pattern:
    return re.compile(r"(?<!\w)" + re.escape(phrase.strip()) + r"(?!\w)", re.I)


def _first_match(text: str, phrases: list[str]) -> Optional[str]:
    for phrase in phrases:
        if phrase.strip() and _phrase_re(phrase).search(text or ""):
            return phrase
    return None


def _get(job: Any, name: str, default: Any = None) -> Any:
    return job.get(name, default) if isinstance(job, dict) else getattr(job, name, default)


# --------------------------------------------------------------------------
# Salary
# --------------------------------------------------------------------------

_PERIODS = [
    (("per hour", "/hour", "/hr", "per hr", "p.h", "hourly", "an hour"), 2080.0),
    (("per day", "/day", "daily", "day rate", "p.d"), 230.0),
    (("per week", "/week", "weekly", "p.w"), 52.0),
    (("per month", "/month", "monthly", "p.m"), 12.0),
]
_NUM_RE = re.compile(r"(\d{1,3}(?:[,\s]\d{3})+|\d+(?:\.\d+)?)\s*([kKmM])?(?![\w])")


def parse_salary(text: str) -> tuple[Optional[float], Optional[float]]:
    """Annualised (min, max) from free text like "$120k - $140k" or "$88 per hour"."""
    low = (text or "").lower()
    multiplier = 1.0
    for hints, mult in _PERIODS:
        if any(h in low for h in hints):
            multiplier = mult
            break
    values = []
    for num, suffix in _NUM_RE.findall(text or ""):
        v = float(re.sub(r"[,\s]", "", num))
        if suffix.lower() == "k":
            v *= 1_000
        elif suffix.lower() == "m":
            v *= 1_000_000
        values.append(v * multiplier)
    plausible = [v for v in values if 10_000 <= v <= 5_000_000]
    if not plausible:
        return None, None
    return min(plausible), max(plausible)


def salary_range(job: Any) -> tuple[Optional[float], Optional[float]]:
    lo, hi = _get(job, "salary_min"), _get(job, "salary_max")
    if lo or hi:
        return lo, hi
    return parse_salary(_get(job, "salary_text") or "")


# --------------------------------------------------------------------------
# Evaluation
# --------------------------------------------------------------------------

def exclusion_reason(job: Any, c: Criteria, stage: str = "full") -> Optional[str]:
    title = _get(job, "title") or ""
    description = _get(job, "description") or ""
    has_description = bool(description.strip())
    text = f"{title}\n{description}"

    ind = _first_match(" | ".join(filter(None, [_get(job, "industry"), _get(job, "company"), title])),
                       c.dealbreaker_industries)
    if ind:
        return f"dealbreaker industry: {ind}"
    kw = _first_match(text, c.dealbreaker_keywords)
    if kw:
        return f"dealbreaker keyword: {kw}"
    kw = _first_match(text, c.keywords_exclude)
    if kw:
        return f"excluded keyword: {kw}"

    if c.titles or c.keywords_include:
        matched = _first_match(title, c.titles) or _first_match(text, c.keywords_include)
        # At listing stage a keyword may still appear in the not-yet-fetched description.
        if not matched and (stage == "full" or has_description or not c.keywords_include):
            return "no target title or include keyword"

    if c.salary_floor:
        lo, hi = salary_range(job)
        top = hi or lo
        if top is not None and top < c.salary_floor:
            return f"salary below floor (${top:,.0f} < ${c.salary_floor:,})"

    return _location_reason(job, c)


MAX_COMMUTE_RADIUS_KM = 1000


def commute_pins(pins: list[dict]) -> list[dict]:
    """Pins that describe a commute. A pin with a radius of 1000+ km covers
    the whole country, which would switch the location filter off; remote
    work is handled by the remote toggles instead, so such pins are ignored."""
    return [p for p in pins if float(p.get("radius_km") or 0) < MAX_COMMUTE_RADIUS_KM]


def _location_reason(job: Any, c: Criteria) -> Optional[str]:
    mode = (_get(job, "work_mode") or "unknown").lower()
    country = (_get(job, "country") or "").upper()
    where = _get(job, "location_text") or "unknown location"
    abroad = bool(country) and country != HOME_COUNTRY

    if mode == "remote":
        if abroad and not c.remote_global_ok:
            return f"remote role based in {country} (remote-global not enabled)"
        if not (c.remote_aus_ok or c.remote_global_ok):
            return "remote roles not wanted"
        return None
    if mode == "hybrid" and not c.allow_hybrid:
        return "hybrid roles not wanted"
    office_days = _get(job, "office_days")
    if mode == "hybrid" and c.max_office_days is not None and office_days and office_days > c.max_office_days:
        return f"hybrid role needs {office_days} office days a week (your max is {c.max_office_days})"
    if mode == "onsite" and not c.allow_onsite:
        return "onsite roles not wanted"
    if abroad:
        return f"based outside Australia ({where})"

    lat, lng = _get(job, "lat"), _get(job, "lng")
    pins = commute_pins(c.pins)
    if not pins or lat is None or lng is None:
        return None
    covered = pins_in_range(lat, lng, pins)
    ok = {
        "hybrid": {"home", "hybrid", "onsite"},
        "onsite": {"home", "onsite"},
    }.get(mode, {"home", "hybrid", "onsite"})
    if covered & ok:
        return None
    dist = home_distance(lat, lng, pins)
    return f"{mode} role in {where} is outside your pin radii ({dist} km from home)"

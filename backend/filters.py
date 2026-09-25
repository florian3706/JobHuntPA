"""Shared pure filter layer for job search.

Pure functions only: no DB, no network, stdlib + ``re`` only.
Import-safe from anywhere (adapters, ingest, API, tests).

Conventions:
- ``job`` may be a ``NormalizedJob`` (or any object with attributes)
  or a plain ``dict``. Field access is duck-typed via :func:`_field`.
- Text matching is case-insensitive substring over ``title + "\\n" +
  description`` (same haystack as ``backend/ingest.py`` dealbreakers).
- Salary parsing prefers explicit ``salary_min`` / ``salary_max`` fields
  and falls back to regex over ``salary_text``.
- Work modes are canonicalised to ``remote | hybrid | onsite | unknown``;
  ``remote_aus`` / ``remote_global`` (backend/profile.py) map to ``remote``.
"""

from __future__ import annotations

import re
from typing import Any, Optional

__all__ = [
    "matches_include",
    "matches_exclude",
    "parse_salary",
    "salary_ok",
    "mode_ok",
    "apply_filters",
    "build_criteria",
]


# ---------------------------------------------------------------------------
# Generic field access (dict or object)
# ---------------------------------------------------------------------------

def _field(job: Any, name: str, default: Any = "") -> Any:
    """Read *name* from a dict or an object. Never raises."""
    try:
        if isinstance(job, dict):
            return job.get(name, default)
        return getattr(job, name, default)
    except Exception:
        return default


def _clean_phrases(values: Any) -> list[str]:
    """Strip tags/phrases; drop empties; preserve order. Never raises."""
    if not isinstance(values, (list, tuple)):
        return []
    out: list[str] = []
    for v in values:
        try:
            s = str(v).strip()
        except Exception:
            continue
        if s:
            out.append(s)
    return out


def _haystack(job: Any) -> str:
    title = _field(job, "title", "") or ""
    desc = _field(job, "description", "") or ""
    try:
        return f"{title}\n{desc}".lower()
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Include / exclude matching
# ---------------------------------------------------------------------------

def matches_include(
    job_or_dict: Any,
    titles: Any,
    keywords_include: Any,
) -> tuple[bool, Optional[str]]:
    """Include filter: True if any title/include phrase hits title+description.

    - Both lists empty (or all blank) -> ``(True, None)`` (no filter).
    - Case-insensitive substring match over ``title + "\\n" + description``.
    - Returns ``(matched, phrase_or_None)`` where *phrase* is the original
      (stripped) phrase from the input lists, or None.
    """
    wanted = _clean_phrases(titles) + _clean_phrases(keywords_include)
    if not wanted:
        return True, None
    # Relaxed: when description is empty/blank, match on title only
    # (still case-insensitive). Otherwise match over title + description.
    try:
        _desc = _field(job_or_dict, "description", "") or ""
        _desc_blank = not str(_desc).strip()
    except Exception:
        _desc_blank = True
    if _desc_blank:
        try:
            _title = _field(job_or_dict, "title", "") or ""
            hay = str(_title).lower()
        except Exception:
            hay = ""
    else:
        hay = _haystack(job_or_dict)
    for phrase in wanted:
        if phrase.lower() in hay:
            return True, phrase
    return False, None


def matches_exclude(job: Any, keywords_exclude: Any) -> Optional[str]:
    """Exclude filter: return the first matching phrase, else None.

    Case-insensitive substring match over ``title + "\\n" + description``.
    Returns the original (stripped) phrase.
    """
    banned = _clean_phrases(keywords_exclude)
    if not banned:
        return None
    hay = _haystack(job)
    for phrase in banned:
        if phrase.lower() in hay:
            return phrase
    return None


# ---------------------------------------------------------------------------
# Salary parsing
# ---------------------------------------------------------------------------

def _coerce_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    # guard against NaN/inf leaking into comparisons
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


_HOURLY_HINTS = (
    "per hour", "/hour", "/hr", "per hr", "p.h.", "hourly", "$/h",
    "per-hour", "hour rate", "hourly rate",
)
_MONTHLY_HINTS = ("per month", "/month", "monthly", "per-month", "month rate")
_WEEKLY_HINTS = ("per week", "/week", "weekly", "per-week")
_DAILY_HINTS = ("per day", "/day", "daily", "per diem", "per-diem", "day rate")

_SALARY_TOKEN_RE = re.compile(
    r"(\d+(?:[,\s]\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?)\s*([kKmM])?\b"
)

_MIN_PLAUSIBLE_ANNUAL = 10_000.0
_MAX_PLAUSIBLE_ANNUAL = 5_000_000.0


def _parse_salary_text(text: str) -> tuple[Optional[float], Optional[float]]:
    low = text.lower()
    hourly = any(h in low for h in _HOURLY_HINTS)
    # hourly wins; otherwise check coarser periods (mutually exclusive)
    monthly = not hourly and any(h in low for h in _MONTHLY_HINTS)
    weekly = not hourly and not monthly and any(h in low for h in _WEEKLY_HINTS)
    daily = (
        not hourly and not monthly and not weekly
        and any(h in low for h in _DAILY_HINTS)
    )

    values: list[float] = []
    for match in _SALARY_TOKEN_RE.finditer(text):
        num_s, suffix = match.group(1), match.group(2)
        cleaned = re.sub(r"[,\s]", "", num_s)
        try:
            v = float(cleaned)
        except ValueError:
            continue
        if suffix:
            if suffix.lower() == "k":
                v *= 1_000.0
            else:  # "M"
                v *= 1_000_000.0
        if hourly:
            v *= 2080.0  # full-time equivalent
        elif monthly:
            v *= 12.0
        elif weekly:
            v *= 52.0
        elif daily:
            v *= 260.0
        values.append(v)

    plausible = [v for v in values if _MIN_PLAUSIBLE_ANNUAL <= v <= _MAX_PLAUSIBLE_ANNUAL]
    if not plausible:
        return None, None
    if len(plausible) == 1:
        return plausible[0], plausible[0]
    return min(plausible), max(plausible)


def parse_salary(job: Any) -> tuple[Optional[float], Optional[float]]:
    """Parse a job's salary -> ``(min, max)`` (either may be None).

    1. Explicit ``salary_min`` / ``salary_max`` fields win when at least one
       is numeric (coerced via ``float()``).
    2. Otherwise regex ``salary_text`` for patterns like ``$120k``,
       ``120,000``, ``100k-140k``, ``AUD ... per year/hour``.
       Hourly rates are annualised with ``* 2080`` (monthly ``*12``,
       weekly ``*52``, daily ``*260``).
    3. ``(None, None)`` when unparseable (callers treat as "pass").
    """
    lo = _coerce_float(_field(job, "salary_min", None))
    hi = _coerce_float(_field(job, "salary_max", None))
    if lo is not None or hi is not None:
        return lo, hi
    text = _field(job, "salary_text", "") or ""
    try:
        text = str(text)
    except Exception:
        return None, None
    if not text.strip():
        return None, None
    try:
        return _parse_salary_text(text)
    except Exception:
        return None, None


def salary_ok(job: Any, floor: Any) -> bool:
    """True if the job meets *floor* (or floor/salary is unknown).

    Pass (True) when *floor* is None, when *floor* is not numeric, or when
    the salary is unparseable. Otherwise True when ``max >= floor`` or
    ``min >= floor``.
    """
    if floor is None:
        return True
    try:
        # allow "" / non-numeric floors to mean "no filter"
        if isinstance(floor, str) and not floor.strip():
            return True
        f = float(floor)
    except (TypeError, ValueError):
        return True
    if f != f:  # NaN floor -> no filter
        return True
    lo, hi = parse_salary(job)
    if lo is None and hi is None:
        return True
    if hi is not None and hi >= f:
        return True
    if lo is not None and lo >= f:
        return True
    return False


# ---------------------------------------------------------------------------
# Work-mode
# ---------------------------------------------------------------------------

_CANONICAL_MODES = ("remote", "hybrid", "onsite")


def _canon_mode(value: Any) -> str:
    try:
        m = str(value or "").strip().lower()
    except Exception:
        return "unknown"
    if not m or m == "unknown":
        return "unknown"
    if m.startswith("remote"):  # remote, remote_aus, remote_global, ...
        return "remote"
    if "hybrid" in m:
        return "hybrid"
    if "onsite" in m or "on-site" in m or "on site" in m:
        return "onsite"
    if m in _CANONICAL_MODES:
        return m
    return "unknown"


def mode_ok(job_work_mode: Any, allowed: Any) -> bool:
    """True if *job_work_mode* is in *allowed*.

    - ``remote_aus`` / ``remote_global`` map to ``remote`` (also any string
      starting with "remote").
    - *allowed* is any collection of mode strings (case-insensitive); a
      single string is treated as one mode.
    - ``allowed`` None (or empty after normalising) -> True (no filter).
    - ``unknown`` job mode -> True (cannot exclude on unknown).
    """
    if allowed is None:
        return True
    if isinstance(allowed, str):
        allowed = [allowed]
    try:
        items = list(allowed)
    except TypeError:
        return True
    norm: set[str] = set()
    for item in items:
        try:
            s = str(item).strip()
        except Exception:
            continue
        if not s:
            continue
        norm.add(_canon_mode(s))
    norm.discard("unknown")
    if not norm:
        return True
    canon = _canon_mode(job_work_mode)
    if canon == "unknown":
        return True
    return canon in norm


# ---------------------------------------------------------------------------
# Dealbreaker check (mirrors backend/ingest.py::check_dealbreaker_excluded)
# ---------------------------------------------------------------------------

def _dealbreaker_reason(job: Any, dealbreakers: Any) -> Optional[str]:
    if not dealbreakers:
        return None
    if isinstance(dealbreakers, list):
        industries: list[str] = []
        keywords = [str(k) for k in dealbreakers if str(k).strip()]
    elif isinstance(dealbreakers, dict):
        industries = []
        keywords = []
        for key in ("industries", "industry", "excluded_industries"):
            vals = dealbreakers.get(key, [])
            if isinstance(vals, str):
                vals = [vals]
            if isinstance(vals, (list, tuple)):
                industries.extend(str(v) for v in vals if str(v).strip())
        for key in ("keywords", "keyword_phrases", "phrases", "excluded_keywords"):
            vals = dealbreakers.get(key, [])
            if isinstance(vals, str):
                vals = [vals]
            if isinstance(vals, (list, tuple)):
                keywords.extend(str(v) for v in vals if str(v).strip())
    else:
        return None

    norm_industries = [s.strip().lower() for s in industries if s.strip()]
    norm_keywords = [s.strip().lower() for s in keywords if s.strip()]

    try:
        job_industry_raw = str(_field(job, "industry", "") or "").strip()
    except Exception:
        job_industry_raw = ""
    if job_industry_raw and job_industry_raw.lower() in norm_industries:
        return f"dealbreaker: industry '{job_industry_raw}' excluded"

    if norm_keywords:
        hay = _haystack(job)
        for phrase in norm_keywords:
            if phrase and phrase in hay:
                return f"dealbreaker: keyword phrase '{phrase}' matched"
    return None


def _resolve_allowed_modes(criteria: dict) -> Optional[set[str]]:
    """Derive the allowed-mode set from *criteria*, or None if no mode filter."""
    if not isinstance(criteria, dict):
        return None
    if criteria.get("allowed_modes") is not None:
        raw = criteria.get("allowed_modes")
        if isinstance(raw, str):
            raw = [raw]
        try:
            modes = {_canon_mode(m) for m in list(raw)}
        except TypeError:
            return None
        modes.discard("unknown")
        return modes or set()

    keys_present = [
        k for k in (
            "allow_remote", "allow_hybrid", "allow_onsite",
            "allow_remote_aus", "allow_remote_global",
            "remote_aus_ok", "remote_global_ok",
        ) if k in criteria
    ]
    if not keys_present:
        return None

    def _bool(key: str, default: bool) -> bool:
        return bool(criteria.get(key, default))

    # remote allowed if any remote flag is truthy
    if "allow_remote" in criteria:
        remote_ok = _bool("allow_remote", True)
    else:
        remote_ok = (
            _bool("allow_remote_aus", True)
            and _bool("allow_remote_global", True)
            and (_bool("remote_aus_ok", True) or _bool("remote_global_ok", False))
        )
        # individual per-scope vetoes still apply when present
        if "allow_remote_aus" in criteria and not criteria.get("allow_remote_aus"):
            if not _bool("allow_remote_global", _bool("remote_global_ok", False)):
                remote_ok = False
        if "allow_remote_global" in criteria and not criteria.get("allow_remote_global"):
            if not _bool("allow_remote_aus", _bool("remote_aus_ok", True)):
                remote_ok = False
    allowed: set[str] = set()
    if remote_ok:
        allowed.add("remote")
    if _bool("allow_hybrid", True):
        allowed.add("hybrid")
    if _bool("allow_onsite", True):
        allowed.add("onsite")
    return allowed


# ---------------------------------------------------------------------------
# Combined filter
# ---------------------------------------------------------------------------

def apply_filters(job: Any, criteria: Any) -> Optional[str]:
    """Apply all filters in order; return ``excluded_reason`` or None.

    Order: dealbreaker -> include -> exclude -> salary -> mode.

    *criteria* keys (all optional)::

        titles, keywords_include, keywords_exclude, salary_floor,
        allow_remote / allow_hybrid / allow_onsite  (bools)
        or allowed_modes (collection of remote/hybrid/onsite),
        dealbreakers ({industries, keywords} or bare keyword list)

    Returns ``"dealbreaker: ..."`` or ``"filter: ..."`` on exclusion,
    else None (job passes).
    """
    if not isinstance(criteria, dict):
        return None

    # 1. dealbreaker (hard exclude, same semantics as ingest)
    reason = _dealbreaker_reason(job, criteria.get("dealbreakers"))
    if reason:
        return reason

    # 2. include (titles + keywords_include)
    titles = criteria.get("titles", [])
    includes = criteria.get("keywords_include", criteria.get("include_keywords", []))
    ok, _ = matches_include(job, titles, includes)
    if not ok:
        return "filter: no include match (titles/keywords_include)"

    # 3. exclude
    excludes = criteria.get(
        "keywords_exclude",
        criteria.get("exclude_keywords", criteria.get("keywords_exclude_list", [])),
    )
    hit = matches_exclude(job, excludes)
    if hit is not None:
        return f"filter: excluded keyword phrase '{hit}' matched"

    # 4. salary
    floor = criteria.get("salary_floor", criteria.get("salary_min"))
    if floor is not None and not salary_ok(job, floor):
        return f"filter: salary below floor {floor}"

    # 5. mode
    allowed = _resolve_allowed_modes(criteria)
    if allowed is not None:
        mode = _field(job, "work_mode", "unknown")
        if not mode_ok(mode, allowed):
            try:
                shown = str(mode or "unknown").strip().lower() or "unknown"
            except Exception:
                shown = "unknown"
            return f"filter: work_mode '{shown}' not allowed"

    return None


# ---------------------------------------------------------------------------
# Criteria builder
# ---------------------------------------------------------------------------

def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple)):
        return _clean_phrases(value)
    return []


def build_criteria(
    profile_dict: Any = None,
    dealbreakers_dict: Any = None,
    pins_list: Any = None,
    config_override: Any = None,
) -> tuple[dict, dict]:
    """Build a filter ``criteria`` dict plus an ``applied_filters`` summary.

    Precedence per key: ``config_override`` > ``profile_dict`` /
    ``dealbreakers_dict`` > defaults. All inputs optional; never raises on
    odd shapes (coerces to clean lists / None).

    Returns ``(criteria, applied_filters)`` where::

        criteria = {
            "titles": [...], "keywords_include": [...],
            "keywords_exclude": [...], "salary_floor": int|None,
            "allow_remote": bool, "allow_hybrid": bool,
            "allow_onsite": bool, "allowed_modes": [...sorted...],
            "dealbreakers": {"industries": [...], "keywords": [...]},
        }
        applied_filters = {
            "include_active": bool, "exclude_active": bool,
            "salary_active": bool, "mode_active": bool,
            "dealbreaker_active": bool, "allowed_modes": [...],
            "salary_floor": ..., "titles_count": int,
            "include_count": int, "exclude_count": int,
            "pins_count": int,
        }
    """
    profile = profile_dict if isinstance(profile_dict, dict) else {}
    override = config_override if isinstance(config_override, dict) else {}

    titles = _as_list(
        override.get("titles", profile.get("titles", []))
    )
    keywords_include = _as_list(
        override.get(
            "keywords_include",
            profile.get("keywords_include", profile.get("include_keywords", [])),
        )
    )
    keywords_exclude = _as_list(
        override.get(
            "keywords_exclude",
            profile.get(
                "keywords_exclude",
                profile.get("exclude_keywords", profile.get("keywords_exclude_list", [])),
            ),
        )
    )

    floor_raw = override.get(
        "salary_floor", profile.get("salary_floor", profile.get("salary_min"))
    )
    salary_floor: Optional[float] = None
    if floor_raw is not None and not (isinstance(floor_raw, str) and not floor_raw.strip()):
        try:
            salary_floor = int(float(floor_raw))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            salary_floor = None

    # -- remote/hybrid/onsite flags --------------------------------------
    if "allowed_modes" in override and override.get("allowed_modes") is not None:
        raw_modes = override.get("allowed_modes")
        if isinstance(raw_modes, str):
            raw_modes = [raw_modes]
        try:
            allowed_modes = sorted(
                {str(m).strip().lower() for m in list(raw_modes) if str(m).strip()}
            )
        except TypeError:
            allowed_modes = ["hybrid", "onsite", "remote"]
        # canonicalise remote_aus/global -> remote
        allowed_modes = sorted({_canon_mode(m) for m in allowed_modes} - {"unknown"})
        allow_remote = "remote" in allowed_modes
        allow_hybrid = "hybrid" in allowed_modes
        allow_onsite = "onsite" in allowed_modes
    else:
        remote_aus = override.get(
            "remote_aus_ok", profile.get("remote_aus_ok", True)
        )
        remote_global = override.get(
            "remote_global_ok", profile.get("remote_global_ok", False)
        )
        if "allow_remote" in override:
            allow_remote = bool(override.get("allow_remote"))
        elif "allow_remote" in profile:
            allow_remote = bool(profile.get("allow_remote"))
        else:
            allow_remote = bool(remote_aus) or bool(remote_global)
        if "allow_remote_aus" in override or "allow_remote_aus" in profile:
            _ = override.get("allow_remote_aus", profile.get("allow_remote_aus"))
        if "allow_remote_global" in override or "allow_remote_global" in profile:
            _ = override.get("allow_remote_global", profile.get("allow_remote_global"))
        allow_hybrid = bool(
            override.get("allow_hybrid", profile.get("allow_hybrid", True))
        )
        allow_onsite = bool(
            override.get("allow_onsite", profile.get("allow_onsite", True))
        )
        allowed_modes = sorted(
            [m for m, ok in (
                ("remote", allow_remote),
                ("hybrid", allow_hybrid),
                ("onsite", allow_onsite),
            ) if ok]
        )

    # -- dealbreakers ------------------------------------------------------
    raw_db = override.get("dealbreakers", dealbreakers_dict)
    if isinstance(raw_db, list):
        dealbreakers = {
            "industries": [],
            "keywords": _clean_phrases(raw_db),
        }
    elif isinstance(raw_db, dict):
        industries: list[str] = []
        keywords: list[str] = []
        for key in ("industries", "industry", "excluded_industries"):
            industries.extend(_as_list(raw_db.get(key, [])))
        for key in ("keywords", "keyword_phrases", "phrases", "excluded_keywords"):
            keywords.extend(_as_list(raw_db.get(key, [])))
        dealbreakers = {"industries": industries, "keywords": keywords}
    else:
        dealbreakers = {"industries": [], "keywords": []}

    criteria = {
        "titles": titles,
        "keywords_include": keywords_include,
        "keywords_exclude": keywords_exclude,
        "salary_floor": salary_floor,
        "allow_remote": allow_remote,
        "allow_hybrid": allow_hybrid,
        "allow_onsite": allow_onsite,
        "allowed_modes": allowed_modes,
        "dealbreakers": dealbreakers,
    }

    try:
        pins_count = len(pins_list) if isinstance(pins_list, (list, tuple)) else 0
    except TypeError:
        pins_count = 0

    applied_filters = {
        "include_active": bool(titles or keywords_include),
        "exclude_active": bool(keywords_exclude),
        "salary_active": salary_floor is not None,
        "mode_active": len(allowed_modes) < 3,
        "dealbreaker_active": bool(
            dealbreakers.get("industries") or dealbreakers.get("keywords")
        ),
        "allowed_modes": list(allowed_modes),
        "salary_floor": salary_floor,
        "titles_count": len(titles),
        "include_count": len(keywords_include),
        "exclude_count": len(keywords_exclude),
        "pins_count": pins_count,
    }
    return criteria, applied_filters

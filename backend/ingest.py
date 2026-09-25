"""Job ingestion orchestration (POST /api/search/run logic).

Pipeline:
  1. load profile + pins + dealbreakers (tolerant of missing backend/db.py
     and backend/profile.py — falls back to JSON files / defaults)
  2. run enabled adapters -> NormalizedJob list
  3. geocode (injectable; offline-safe stub by default)
  4. classify work-mode
  5. compute distance to nearest pin
  6. PRE-LLM dealbreaker hard-exclude:
       industry in list OR keyword phrase in title+description
       (case-insensitive) -> set excluded_reason, hidden by default
  7. dedupe by url UNIQUE + description_hash:
       existing rows keep status, update last_seen;
       only new/changed hashes need scoring
  8. insert new rows with status='to_review'

Only stdlib + backend.adapters are required at import time.
"""

from __future__ import annotations

import json
import logging
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from backend.adapters.base import NormalizedJob, description_hash, normalize_job

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Filtering: backend.filters (optional) with inline fallback
# ---------------------------------------------------------------------------

try:
    from backend.filters import apply_filters, build_criteria  # type: ignore
except Exception:  # backend.filters missing -> inline fallback so import still works

    def build_criteria(
        profile=None,
        dealbreakers=None,
        pins=None,
        config=None,
        overrides=None,
        **kwargs: Any,
    ) -> dict:
        """Build effective filter criteria (inline fallback).

        Merges DB profile/dealbreakers/pins with config overrides:
          config.titles / keywords_include(+include) / keywords_exclude(+exclude)
          / salary_floor / modes(+allowed_modes/work_modes) / dealbreakers / pins.
        Config values win when non-empty; otherwise profile values are used.
        """
        prof = profile if isinstance(profile, dict) else {}
        dbk = dealbreakers if isinstance(dealbreakers, dict) else {}
        cfg = config if isinstance(config, dict) else {}
        ov: dict = {}
        if isinstance(overrides, dict):
            ov = overrides
        elif isinstance(cfg, dict):
            ov = cfg
        if isinstance(kwargs.get("config"), dict) and not ov:
            ov = kwargs["config"]
        if isinstance(kwargs.get("overrides"), dict):
            ov = {**ov, **kwargs["overrides"]}

        def _str_list(*vals: Any) -> list[str]:
            out: list[str] = []
            for v in vals:
                if isinstance(v, (list, tuple)):
                    out.extend(str(x).strip() for x in v if str(x).strip())
                elif isinstance(v, str) and v.strip():
                    out.append(v.strip())
            return out

        titles = _str_list(ov.get("titles")) or _str_list(prof.get("titles"))
        inc = _str_list(ov.get("keywords_include"), ov.get("include")) or _str_list(
            prof.get("keywords_include"), prof.get("include")
        )
        exc = _str_list(ov.get("keywords_exclude"), ov.get("exclude")) or _str_list(
            prof.get("keywords_exclude"), prof.get("exclude")
        )

        salary_floor: Any = ov.get("salary_floor", None)
        if salary_floor is None:
            salary_floor = prof.get("salary_floor", None)
        try:
            if salary_floor is None or (isinstance(salary_floor, str) and not salary_floor.strip()):
                salary_floor = None
            else:
                salary_floor = int(salary_floor)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            salary_floor = None

        modes: Any = ov.get("modes", ov.get("allowed_modes", ov.get("work_modes", None)))
        if modes is None:
            modes = prof.get("modes", prof.get("allowed_modes", prof.get("work_modes", None)))
        if isinstance(modes, str):
            modes = [modes]
        if isinstance(modes, (list, tuple)):
            allowed_modes = [str(m).strip().lower().replace("-", "_") for m in modes if str(m).strip()]
        else:
            remote_ok = bool(prof.get("remote_aus_ok", True) or prof.get("remote_global_ok", False))
            if "remote_aus_ok" in ov or "remote_global_ok" in ov:
                remote_ok = bool(
                    ov.get("remote_aus_ok", prof.get("remote_aus_ok", True))
                    or ov.get("remote_global_ok", prof.get("remote_global_ok", False))
                )
            allowed_modes = (
                ["remote", "remote_aus", "remote_global", "hybrid", "onsite", "unknown"]
                if remote_ok
                else ["hybrid", "onsite", "unknown"]
            )

        # Dealbreakers: prefer already-merged dbk; honour an override when present.
        industries: list[str] = [str(s).strip() for s in (dbk.get("industries") or []) if str(s).strip()]
        keywords: list[str] = [str(s).strip() for s in (dbk.get("keywords") or []) if str(s).strip()]
        ov_dbk = ov.get("dealbreakers", None)
        if isinstance(ov_dbk, (dict, list)):
            try:
                if isinstance(ov_dbk, list):
                    keywords = [str(k).strip() for k in ov_dbk if str(k).strip()]
                elif isinstance(ov_dbk, dict):
                    env: Any = ov_dbk.get("dealbreakers") if isinstance(ov_dbk.get("dealbreakers"), (dict, list)) else ov_dbk
                    if isinstance(env, list):
                        keywords = [str(k).strip() for k in env if str(k).strip()]
                    else:
                        ind: list[str] = []
                        kw: list[str] = []
                        for key in ("industries", "industry", "excluded_industries"):
                            vals = env.get(key, [])
                            if isinstance(vals, list):
                                ind.extend(str(v).strip() for v in vals if str(v).strip())
                            elif isinstance(vals, str) and vals.strip():
                                ind.append(vals.strip())
                        for key in ("keywords", "keyword_phrases", "phrases", "excluded_keywords"):
                            vals = env.get(key, [])
                            if isinstance(vals, list):
                                kw.extend(str(v).strip() for v in vals if str(v).strip())
                            elif isinstance(vals, str) and vals.strip():
                                kw.append(vals.strip())
                        industries, keywords = ind, kw
            except Exception:
                pass

        pins_list = pins if isinstance(pins, list) else []
        if isinstance(ov.get("pins"), list) and ov.get("pins"):
            pins_list = ov["pins"]
        return {
            "titles": titles,
            "keywords_include": inc,
            "include": inc,
            "keywords_exclude": exc,
            "exclude": exc,
            "salary_floor": salary_floor,
            "allowed_modes": allowed_modes,
            "modes": allowed_modes,
            "dealbreakers": {"industries": industries, "keywords": keywords},
            "dealbreaker_industries": industries,
            "dealbreaker_keywords": keywords,
            "pins": pins_list,
        }

    def apply_filters(job: Any, criteria: Any = None, **kwargs: Any) -> Optional[str]:
        """Return excluded_reason when *job* is filtered out, else None (fallback).

        Accepts NormalizedJob or dict + criteria dict (or kwargs). Checks:
          dealbreaker industry/keywords, exclude keywords, titles/include
          (require >=1 match when configured), salary_floor, allowed_modes.
        Never raises.
        """
        try:
            crit = criteria if isinstance(criteria, dict) else {}
            if kwargs:
                for key in ("titles", "keywords_include", "include", "keywords_exclude",
                            "exclude", "salary_floor", "allowed_modes", "modes",
                            "dealbreakers"):
                    if key in kwargs and key not in crit:
                        crit[key] = kwargs[key]

            def _jf(*keys: str, default: Any = "") -> Any:
                try:
                    for k in keys:
                        if isinstance(job, dict):
                            v = job.get(k, None)
                        else:
                            v = getattr(job, k, None)
                        if v not in (None, "", [], {}):
                            return v
                    return default
                except Exception:
                    return default

            title = str(_jf("title", default="") or "")
            desc = str(_jf("description", default="") or "")
            industry = str(_jf("industry", default="") or "")
            work_mode = str(_jf("work_mode", "workMode", "mode", default="unknown") or "unknown")
            salary_min = _jf("salary_min", "salaryMin", default=None)
            salary_max = _jf("salary_max", "salaryMax", default=None)
            salary_text = str(_jf("salary_text", "salaryText", "salary", default="") or "")

            import re as _re

            hay = f"{title}\n{desc}".lower()
            ind_l = industry.strip().lower()

            dbk = crit.get("dealbreakers") if isinstance(crit.get("dealbreakers"), dict) else {}
            db_inds = [str(s).strip().lower() for s in (
                crit.get("dealbreaker_industries") or dbk.get("industries") or []) if str(s).strip()]
            db_keys = [str(s).strip().lower() for s in (
                crit.get("dealbreaker_keywords") or dbk.get("keywords") or []) if str(s).strip()]
            if ind_l and ind_l in db_inds:
                return f"dealbreaker: industry '{industry.strip()}' excluded"
            for phrase in db_keys:
                if phrase and phrase in hay:
                    return f"dealbreaker: keyword phrase '{phrase}' matched"

            def _as_list(v: Any) -> list[str]:
                if isinstance(v, (list, tuple)):
                    return [str(x).strip() for x in v if str(x).strip()]
                if isinstance(v, str) and v.strip():
                    return [v.strip()]
                return []

            excl = _as_list(crit.get("keywords_exclude")) or _as_list(crit.get("exclude"))
            for phrase in excl:
                if phrase.lower() in hay:
                    return f"filter: excluded keyword '{phrase}' matched"

            titles = _as_list(crit.get("titles"))
            incl = _as_list(crit.get("keywords_include")) or _as_list(crit.get("include"))
            need = [t for t in (titles + incl) if t]
            if need and not any(t.lower() in hay for t in need):
                return "filter: no title/keyword match"

            floor = crit.get("salary_floor", None)
            try:
                floor_i = int(floor) if floor is not None and str(floor).strip() != "" else None
            except (TypeError, ValueError):
                floor_i = None
            if floor_i is not None:
                eff: Any = None
                try:
                    eff_max = float(salary_max) if salary_max is not None and str(salary_max).strip() != "" else None
                except (TypeError, ValueError):
                    eff_max = None
                try:
                    eff_min = float(salary_min) if salary_min is not None and str(salary_min).strip() != "" else None
                except (TypeError, ValueError):
                    eff_min = None
                eff = eff_max if eff_max is not None else eff_min
                if eff is None and salary_text.strip():
                    try:
                        nums: list[float] = []
                        for num, suf in _re.findall(r"(\d[\d,\.]*)\s*([kKmM]?)", salary_text):
                            try:
                                base = float(num.replace(",", ""))
                                if suf.lower() == "k":
                                    base *= 1000
                                elif suf.lower() == "m":
                                    base *= 1000000
                                nums.append(base)
                            except ValueError:
                                continue
                        if nums:
                            eff = max(nums)
                    except Exception:
                        eff = None
                if eff is not None and eff < float(floor_i):
                    return f"filter: salary {eff:g} below floor {floor_i}"

            allowed = _as_list(crit.get("allowed_modes")) or _as_list(crit.get("modes"))
            if allowed:
                al = {a.lower().replace("-", "_") for a in allowed}
                jm = work_mode.strip().lower().replace("-", "_")
                if jm not in ("", "unknown", "other"):
                    if jm in ("remote", "remote_aus", "remote_global"):
                        if not ({"remote", "remote_aus", "remote_global"} & al):
                            return f"filter: work mode '{work_mode}' not allowed"
                    elif jm not in al:
                        return f"filter: work mode '{work_mode}' not allowed"
            return None
        except Exception:
            return None


def _default_seek_location(pins: Any, config: Any) -> str:
    """Resolve default SEEK location: home pin -> hybrid pin -> config -> 'Australia'."""
    try:
        for p in (pins or []):
            if isinstance(p, dict) and str(p.get("kind", "")).lower() == "home" and str(p.get("label", "")).strip():
                return str(p.get("label")).strip()
        for p in (pins or []):
            if isinstance(p, dict) and str(p.get("kind", "")).lower() == "hybrid" and str(p.get("label", "")).strip():
                return str(p.get("label")).strip()
        cfg = config if isinstance(config, dict) else {}
        adapters_cfg = cfg.get("adapters", {})
        if isinstance(adapters_cfg, dict):
            seek_cfg = adapters_cfg.get("seek", {})
            if isinstance(seek_cfg, dict):
                loc = seek_cfg.get("location")
                if isinstance(loc, str) and loc.strip():
                    return loc.strip()
        seek_cfg = cfg.get("seek", {})
        if isinstance(seek_cfg, dict):
            loc = seek_cfg.get("location")
            if isinstance(loc, str) and loc.strip():
                return loc.strip()
    except Exception:
        pass
    return "Australia"

_REPO_ROOT = Path(__file__).resolve().parents[1]  # .../JobHuntPA
DATA_DIR = _REPO_ROOT / "data"
DB_PATH = DATA_DIR / "jobhunt.db"

PROFILE_JSON = DATA_DIR / "profile.json"
PINS_JSON = DATA_DIR / "pins.json"
DEALBREAKERS_JSON = DATA_DIR / "dealbreakers.json"
SOURCES_JSON = DATA_DIR / "sources.json"

Geocoder = Callable[[str], Optional[tuple[Optional[float], Optional[float]]]]

# ---------------------------------------------------------------------------
# Loading: profile / pins / dealbreakers
# ---------------------------------------------------------------------------

def _orm_session():
    """Open a backend.db (SQLAlchemy) session if available, else None.

    Never raises: returns None when backend.db / sqlalchemy is missing.
    """
    try:
        from backend.db import SessionLocal, init_db

        try:
            init_db()
        except Exception:
            pass
        return SessionLocal()
    except Exception:
        return None


def _read_json(path: Path, default: Any) -> Any:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("Could not read %s: %s", path, exc)
    return default


def load_profile(path: Path = PROFILE_JSON) -> dict:
    """Load user profile. Prefers backend/profile.py + backend/db.py if present."""
    # ORM path: backend.profile.get_profile(db) needs a Session.
    try:
        from backend import profile as profile_mod

        get_profile = getattr(profile_mod, "get_profile", None)
        if callable(get_profile):
            db = _orm_session()
            if db is not None:
                try:
                    prof = get_profile(db)
                    if isinstance(prof, dict):
                        return prof
                except Exception as exc:
                    log.debug("backend.profile.get_profile failed: %s", exc)
                finally:
                    try:
                        db.close()
                    except Exception:
                        pass
    except Exception:
        pass
    prof = _read_json(path, {})
    return prof if isinstance(prof, dict) else {}


def load_pins(path: Path = PINS_JSON, profile: Optional[dict] = None) -> list[dict]:
    """Load location pins: [{lat, lng, label, kind?, radius_km?}].

    Prefers backend/profile.py::list_pins(db) when backend.db is available.
    """
    try:
        from backend import profile as profile_mod

        list_pins = getattr(profile_mod, "list_pins", None)
        if callable(list_pins):
            db = _orm_session()
            if db is not None:
                try:
                    pins = list_pins(db)
                    if isinstance(pins, list):
                        return [dict(p) for p in pins if isinstance(p, dict)]
                except Exception as exc:
                    log.debug("backend.profile.list_pins failed: %s", exc)
                finally:
                    try:
                        db.close()
                    except Exception:
                        pass
    except Exception:
        pass
    pins: Any = []
    if profile and isinstance(profile.get("pins"), list):
        pins = profile["pins"]
    elif path.exists():
        pins = _read_json(path, [])
    # also accept {"pins": [...]} envelope
    if isinstance(pins, dict) and isinstance(pins.get("pins"), list):
        pins = pins["pins"]
    out: list[dict] = []
    if isinstance(pins, list):
        for p in pins:
            if not isinstance(p, dict):
                continue
            try:
                lat = float(p["lat"]) if p.get("lat") is not None else None
                lng = float(p.get("lng", p.get("lon"))) if p.get("lng", p.get("lon")) is not None else None
            except (TypeError, ValueError):
                continue
            if lat is None or lng is None:
                continue
            out.append(
                {
                    "lat": lat,
                    "lng": lng,
                    "label": str(p.get("label", "")),
                    "kind": str(p.get("kind", "")),
                    "radius_km": p.get("radius_km", p.get("radiusKm", p.get("max_km", p.get("maxKm")))),
                    "max_km": p.get("max_km", p.get("maxKm", p.get("radius_km", p.get("radiusKm")))),
                }
            )
    return out


def load_dealbreakers(
    path: Path = DEALBREAKERS_JSON, profile: Optional[dict] = None
) -> dict:
    """Load dealbreakers as {"industries": [...], "keywords": [...]}.

    Accepts several on-disk shapes:
      {"industries": [...], "keywords": [...]}
      {"industry": [...], "keyword_phrases": [...]}
      {"dealbreakers": {...envelope...}}
      [...] (bare keyword list)

    Prefers backend/profile.py::get_dealbreakers(db) when available.
    """
    try:
        from backend import profile as profile_mod

        get_db_dealbreakers = getattr(profile_mod, "get_dealbreakers", None)
        if callable(get_db_dealbreakers):
            db = _orm_session()
            if db is not None:
                try:
                    raw = get_db_dealbreakers(db)
                    if isinstance(raw, (dict, list)):
                        return normalise_dealbreakers(raw)
                except Exception as exc:
                    log.debug("backend.profile.get_dealbreakers failed: %s", exc)
                finally:
                    try:
                        db.close()
                    except Exception:
                        pass
    except Exception:
        pass
    if profile and isinstance(profile.get("dealbreakers"), (dict, list)):
        raw = profile["dealbreakers"]
    else:
        raw = _read_json(path, {})
    return normalise_dealbreakers(raw)


def normalise_dealbreakers(raw: Any) -> dict:
    industries: list[str] = []
    keywords: list[str] = []
    if isinstance(raw, list):
        keywords = [str(k) for k in raw if str(k).strip()]
    elif isinstance(raw, dict):
        envelope = raw.get("dealbreakers") if isinstance(raw.get("dealbreakers"), (dict, list)) else raw
        if isinstance(envelope, list):
            keywords = [str(k) for k in envelope if str(k).strip()]
        else:
            for key in ("industries", "industry", "excluded_industries"):
                vals = envelope.get(key, [])
                if isinstance(vals, list):
                    industries.extend(str(v) for v in vals if str(v).strip())
                elif isinstance(vals, str) and vals.strip():
                    industries.append(vals)
            for key in ("keywords", "keyword_phrases", "phrases", "excluded_keywords"):
                vals = envelope.get(key, [])
                if isinstance(vals, list):
                    keywords.extend(str(v) for v in vals if str(v).strip())
                elif isinstance(vals, str) and vals.strip():
                    keywords.append(vals)
    return {"industries": industries, "keywords": keywords}


# ---------------------------------------------------------------------------
# Enrichment: work-mode, geocode, distance
# ---------------------------------------------------------------------------

_REMOTE_HINTS = ("remote", "work from home", "wfh", "work-from-home", "fully remote")
_HYBRID_HINTS = ("hybrid", "2-3 days", "3 days in office", "flexible/remote")
_ONSITE_HINTS = ("on-site", "onsite", "in office", "in-office", "office based", "office-based")


def classify_work_mode(
    title: str = "", description: str = "", location_text: str = ""
) -> str:
    """Keyword work-mode classifier -> remote | hybrid | onsite | unknown."""
    blob = f"{title}\n{description}\n{location_text}".lower()
    if any(h in blob for h in _HYBRID_HINTS):
        # "hybrid" mention wins over generic "remote" only when explicit
        if "hybrid" in blob:
            return "hybrid"
    if any(h in blob for h in _REMOTE_HINTS):
        return "remote"
    if any(h in blob for h in _HYBRID_HINTS):
        return "hybrid"
    if any(h in blob for h in _ONSITE_HINTS):
        return "onsite"
    if "remote" in (location_text or "").lower():
        return "remote"
    return "unknown"


def default_geocoder(location_text: str) -> Optional[tuple[None, None]]:
    """Offline-safe no-op geocoder. Pass a real one into run_search()."""
    return None


def haversine_km(
    lat1: float, lon1: float, lat2: float, lon2: float
) -> float:
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def distance_to_pins(
    lat: Optional[float], lng: Optional[float], pins: list[dict]
) -> Optional[float]:
    # Prefer backend.profile.distance_km (same haversine) when importable.
    dist_fn = haversine_km
    try:
        from backend.profile import distance_km as _profile_dist  # type: ignore

        dist_fn = _profile_dist
    except Exception:
        pass
    if lat is None or lng is None or not pins:
        return None
    dists: list[float] = []
    for p in pins:
        try:
            dists.append(dist_fn(float(lat), float(lng), float(p["lat"]), float(p["lng"])))
        except (KeyError, TypeError, ValueError):
            continue
    return min(dists) if dists else None


def smart_classify_work_mode(job: NormalizedJob, pins: list[dict]) -> str:
    """Classify work-mode, preferring backend.profile when available.

    backend.profile.classify_work_mode(job_text, job_lat, job_lng, pins)
    returns remote_aus | remote_global | hybrid | onsite | unknown.
    This ingest module historically used remote | hybrid | onsite | unknown.
    To stay compatible with both, map remote_aus/remote_global -> remote and
    keep the other values as-is. Falls back to the local keyword classifier.
    """
    try:
        from backend.profile import classify_work_mode as _profile_classify  # type: ignore

        mode = _profile_classify(
            f"{job.title}\n{job.description}\n{job.location_text}",
            job.lat,
            job.lng,
            pins,
        )
        if mode in ("remote_aus", "remote_global"):
            return "remote"
        if mode in ("hybrid", "onsite", "unknown"):
            return mode
    except Exception:
        pass
    return classify_work_mode(job.title, job.description, job.location_text)


def smart_geocode(location_text: str) -> Optional[tuple[Optional[float], Optional[float]]]:
    """Geocode via backend.profile.geocode_location when available."""
    try:
        from backend.profile import geocode_location as _profile_geocode  # type: ignore

        res = _profile_geocode(location_text)
        if isinstance(res, dict) and res.get("lat") is not None:
            return (float(res["lat"]), float(res["lng"]))
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# PRE-LLM dealbreaker hard-exclude
# ---------------------------------------------------------------------------

def check_dealbreaker_excluded(job: NormalizedJob, dealbreakers: dict) -> Optional[str]:
    """Return excluded_reason if the job is hard-excluded, else None.

    Rules (case-insensitive):
      - job.industry in dealbreakers["industries"] (exact match, stripped)
      - any keyword phrase substring of title + description
    """
    industries = [str(s).strip().lower() for s in (dealbreakers.get("industries") or []) if str(s).strip()]
    keywords = [str(s).strip().lower() for s in (dealbreakers.get("keywords") or []) if str(s).strip()]

    job_industry = (job.industry or "").strip().lower()
    if job_industry and job_industry in industries:
        return f"dealbreaker: industry '{job.industry.strip()}' excluded"

    if keywords:
        haystack = f"{job.title or ''}\n{job.description or ''}".lower()
        for phrase in keywords:
            if phrase and phrase in haystack:
                return f"dealbreaker: keyword phrase '{phrase}' matched"
    return None


# ---------------------------------------------------------------------------
# Storage (sqlite; url UNIQUE + description_hash)
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT DEFAULT '',
    external_id TEXT DEFAULT '',
    company TEXT DEFAULT '',
    title TEXT DEFAULT '',
    location_text TEXT DEFAULT '',
    lat REAL,
    lng REAL,
    work_mode TEXT DEFAULT 'unknown',
    salary_text TEXT DEFAULT '',
    salary_min REAL,
    salary_max REAL,
    url TEXT UNIQUE NOT NULL,
    description TEXT DEFAULT '',
    description_hash TEXT DEFAULT '',
    industry TEXT DEFAULT '',
    excluded_reason TEXT,
    status TEXT DEFAULT 'to_review',
    distance_km REAL,
    first_seen TEXT DEFAULT '',
    last_seen TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_hash ON jobs(description_hash);
"""


def get_connection(db_path: Path = DB_PATH) -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def list_jobs(
    conn: sqlite3.Connection,
    *,
    include_excluded: bool = False,
    status: Optional[str] = None,
) -> list[sqlite3.Row]:
    """Query helper — excluded rows (excluded_reason set) hidden by default."""
    q = "SELECT * FROM jobs"
    clauses: list[str] = []
    params: list[Any] = []
    if not include_excluded:
        clauses.append("(excluded_reason IS NULL OR excluded_reason = '')")
    if status:
        clauses.append("status = ?")
        params.append(status)
    if clauses:
        q += " WHERE " + " AND ".join(clauses)
    q += " ORDER BY last_seen DESC"
    return list(conn.execute(q, params).fetchall())


def store_jobs(
    conn: sqlite3.Connection, jobs: list[NormalizedJob]
) -> dict:
    """Dedupe + insert.

    - match on url UNIQUE
    - existing rows: keep status, update last_seen (+ description_hash /
      description etc. when changed)
    - only new rows or rows whose description_hash changed need scoring

    Returns {"inserted": n, "updated": n, "needs_scoring": [urls]}.
    """
    inserted = 0
    updated = 0
    needs_scoring: list[str] = []
    now = _utcnow()
    for job in jobs:
        normalize_job(job)
        if not job.url:
            log.debug("Skipping job without url: %s @ %s", job.title, job.company)
            continue
        h = description_hash(job.description)
        row = conn.execute("SELECT * FROM jobs WHERE url = ?", (job.url,)).fetchone()
        if row is None:
            conn.execute(
                """INSERT INTO jobs
                   (source, external_id, company, title, location_text, lat, lng,
                    work_mode, salary_text, salary_min, salary_max, url,
                    description, description_hash, industry, excluded_reason,
                    status, distance_km, first_seen, last_seen)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    job.source, job.external_id, job.company, job.title,
                    job.location_text, job.lat, job.lng, job.work_mode,
                    job.salary_text, job.salary_min, job.salary_max, job.url,
                    job.description, h,
                    job.industry,
                    getattr(job, "excluded_reason", None),
                    "to_review",
                    getattr(job, "distance_km", None),
                    now, now,
                ),
            )
            inserted += 1
            needs_scoring.append(job.url)
        else:
            changed = (row["description_hash"] or "") != h
            conn.execute(
                """UPDATE jobs SET
                       source=COALESCE(NULLIF(?,''),source),
                       company=COALESCE(NULLIF(?,''),company),
                       title=COALESCE(NULLIF(?,''),title),
                       location_text=COALESCE(NULLIF(?,''),location_text),
                       lat=COALESCE(?,lat), lng=COALESCE(?,lng),
                       work_mode=COALESCE(NULLIF(?,''),work_mode),
                       salary_text=COALESCE(NULLIF(?,''),salary_text),
                       description=?, description_hash=?,
                       industry=COALESCE(NULLIF(?,''),industry),
                       excluded_reason=?,
                       distance_km=COALESCE(?,distance_km),
                       last_seen=?
                   WHERE url=?""",
                (
                    job.source, job.company, job.title, job.location_text,
                    job.lat, job.lng, job.work_mode, job.salary_text,
                    job.description, h, job.industry,
                    getattr(job, "excluded_reason", None),
                    getattr(job, "distance_km", None),
                    now, job.url,
                ),
            )
            updated += 1
            if changed:
                needs_scoring.append(job.url)
            # NOTE: status intentionally untouched for existing rows.
    conn.commit()
    return {"inserted": inserted, "updated": updated, "needs_scoring": needs_scoring}


def store_jobs_orm(db, jobs: list[NormalizedJob]) -> dict:
    """ORM version of :func:`store_jobs` using backend.db.Job.

    Same rules: url UNIQUE dedupe, existing rows keep status + update
    last_seen, only new/changed description_hashes need scoring.
    """
    from datetime import datetime as _dt

    from backend.db import Job as _Job

    inserted = 0
    updated = 0
    needs_scoring: list[str] = []
    now = _dt.utcnow()
    for job in jobs:
        normalize_job(job)
        if not job.url:
            continue
        h = description_hash(job.description)
        row = db.query(_Job).filter(_Job.url == job.url).first()
        if row is None:
            db.add(
                _Job(
                    source=job.source,
                    external_id=job.external_id,
                    company=job.company,
                    title=job.title,
                    location_text=job.location_text,
                    lat=job.lat,
                    lng=job.lng,
                    work_mode=job.work_mode,
                    salary_text=job.salary_text,
                    salary_min=int(job.salary_min) if job.salary_min is not None else None,
                    salary_max=int(job.salary_max) if job.salary_max is not None else None,
                    url=job.url,
                    description=job.description,
                    description_hash=h,
                    industry=job.industry,
                    distance_km=getattr(job, "distance_km", None),
                    status="to_review",
                    excluded_reason=getattr(job, "excluded_reason", None),
                    first_seen=now,
                    last_seen=now,
                )
            )
            inserted += 1
            needs_scoring.append(job.url)
        else:
            changed = (row.description_hash or "") != h
            if job.source:
                row.source = job.source
            if job.company:
                row.company = job.company
            if job.title:
                row.title = job.title
            if job.location_text:
                row.location_text = job.location_text
            if job.lat is not None:
                row.lat = job.lat
            if job.lng is not None:
                row.lng = job.lng
            if job.work_mode:
                row.work_mode = job.work_mode
            if job.salary_text:
                row.salary_text = job.salary_text
            row.description = job.description
            row.description_hash = h
            if job.industry:
                row.industry = job.industry
            row.excluded_reason = getattr(job, "excluded_reason", None)
            if getattr(job, "distance_km", None) is not None:
                row.distance_km = getattr(job, "distance_km")
            row.last_seen = now
            # NOTE: row.status intentionally untouched.
            updated += 1
            if changed:
                needs_scoring.append(job.url)
    db.commit()
    return {"inserted": inserted, "updated": updated, "needs_scoring": needs_scoring}


# ---------------------------------------------------------------------------
# Adapter registry
# ---------------------------------------------------------------------------

def build_adapters(config: dict) -> list:
    """Instantiate enabled adapters from config.

    config["adapters"] example:
      {"seek": {"enabled": true, "keywords": ["python"], "location": "Sydney"},
       "greenhouse": {"enabled": true, "boards": ["acme"]},
       "lever": {"enabled": false, "hosts": []},
       "ashby": {"enabled": false, "boards": []},
       "workday": {"enabled": false, "tenants": []}}
    Unknown/missing sections are skipped (never crash).
    """
    adapters: list = []
    cfg = config.get("adapters", config) if isinstance(config, dict) else {}

    def enabled(name: str) -> dict | None:
        section = cfg.get(name)
        if not isinstance(section, dict):
            return None
        if not section.get("enabled", False):
            return None
        return section

    seek_cfg = enabled("seek")
    if seek_cfg is not None:
        try:
            from backend.adapters.seek import SeekAdapter

            kws = seek_cfg.get("keywords", seek_cfg.get("query", ""))
            if isinstance(kws, str):
                kws = [kws]
            adapters.append(
                SeekAdapter(
                    keywords=kws or [],
                    location=seek_cfg.get("location"),
                    max_pages=int(seek_cfg.get("max_pages", 1)),
                )
            )
        except Exception as exc:
            log.warning("Could not build SeekAdapter: %s", exc)

    gh_cfg = enabled("greenhouse")
    if gh_cfg is not None:
        try:
            from backend.adapters.greenhouse import GreenhouseAdapter

            boards = gh_cfg.get("boards", gh_cfg.get("board_tokens", []))
            if isinstance(boards, str):
                boards = [boards]
            for board in boards or []:
                adapters.append(GreenhouseAdapter(board_token=str(board)))
        except Exception as exc:
            log.warning("Could not build GreenhouseAdapter: %s", exc)

    lever_cfg = enabled("lever")
    if lever_cfg is not None:
        try:
            from backend.adapters.lever import LeverAdapter

            hosts = lever_cfg.get("hosts", lever_cfg.get("boards", []))
            if isinstance(hosts, str):
                hosts = [hosts]
            for host in hosts or []:
                adapters.append(LeverAdapter(host=str(host)))
        except Exception as exc:
            log.warning("Could not build LeverAdapter: %s", exc)

    ashby_cfg = enabled("ashby")
    if ashby_cfg is not None:
        try:
            from backend.adapters.ashby import AshbyAdapter

            boards = ashby_cfg.get("boards", [])
            if isinstance(boards, str):
                boards = [boards]
            for board in boards or []:
                adapters.append(AshbyAdapter(board=str(board)))
        except Exception as exc:
            log.warning("Could not build AshbyAdapter: %s", exc)

    wd_cfg = enabled("workday")
    if wd_cfg is not None:
        try:
            from backend.adapters.workday import WorkdayAdapter

            tenants = wd_cfg.get("tenants", [])
            if isinstance(tenants, str):
                tenants = [tenants]
            for t in tenants or [{}]:
                if isinstance(t, dict):
                    adapters.append(WorkdayAdapter(**{k: t.get(k, "") for k in ("tenant", "site", "base_url")}))
                else:
                    adapters.append(WorkdayAdapter(tenant=str(t)))
        except Exception as exc:
            log.warning("Could not build WorkdayAdapter: %s", exc)

    return adapters


# ---------------------------------------------------------------------------
# Company sources (DB CompanySource rows, data/sources.json fallback)
# ---------------------------------------------------------------------------

def _sget(obj: Any, *keys: str, default: Any = "") -> Any:
    """Get the first non-empty field from a dict or ORM object. Never raises."""
    try:
        for key in keys:
            if isinstance(obj, dict):
                val = obj.get(key, None)
            else:
                val = getattr(obj, key, None)
            if val not in (None, "", [], {}):
                return val
    except Exception:
        pass
    return default


def _source_enabled(obj: Any) -> bool:
    """True unless the source is explicitly disabled. Never raises."""
    try:
        if isinstance(obj, dict):
            return obj.get("enabled", True) is not False
        return getattr(obj, "enabled", True) is not False
    except Exception:
        return True


def load_sources_from_json(path: Path = SOURCES_JSON) -> list[dict]:
    """Read raw company-source dicts from data/sources.json. Never raises.

    Accepts a bare list or a {"sources": [...]} envelope. Returns [] when
    the file is missing/unparseable (DB-unavailable fallback).
    """
    try:
        # Prefer the shared backend.sources loader when importable so both
        # modules agree on envelope handling.
        from backend.sources import load_sources_from_json as _shared

        return _shared(path)
    except Exception:
        pass
    return _fallback_read_sources_json(path)


def _fallback_read_sources_json(path: Path) -> list[dict]:
    try:
        raw = _read_json(path, [])
    except Exception:
        return []
    rows: Any = raw.get("sources") if isinstance(raw, dict) else raw
    if not isinstance(rows, list):
        return []
    return [dict(r) for r in rows if isinstance(r, dict)]


def load_company_sources(db=None, path: Path = SOURCES_JSON) -> list[dict]:
    """Load enabled company sources: ORM CompanySource rows, JSON fallback.

    Never raises — returns [] when neither store is available.
    """
    try:
        from backend.sources import load_enabled_sources as _load_enabled

        return _load_enabled(db=db, path=path)
    except Exception as exc:
        log.debug("backend.sources unavailable, JSON fallback: %s", exc)
    try:
        return [r for r in _fallback_read_sources_json(path) if _source_enabled(r)]
    except Exception:
        return []


def build_sources_adapters(
    sources: Optional[list] = None, db=None, path: Path = SOURCES_JSON
) -> list:
    """Instantiate adapters for enabled company sources. Never raises.

    *sources*: explicit list of dicts/ORM rows (overrides auto-load).
    When None, loads via :func:`load_company_sources` (ORM -> JSON fallback).
    Each adapter gets ``_source_label`` / ``_source_url`` metadata used for
    the ``per_source`` summary in :func:`run_search`.
    """
    if sources is None:
        try:
            sources = load_company_sources(db=db, path=path)
        except Exception as exc:
            log.warning("load_company_sources failed: %s", exc)
            sources = []
    adapters: list = []
    for src in sources or []:
        try:
            if not _source_enabled(src):
                continue
            stype = str(_sget(src, "type", "adapter", "kind", "source_type", default="") or "").strip().lower()
            label = str(_sget(src, "label", "company", "name", default="") or "").strip()
            careers_url = str(_sget(src, "careers_url", "url", "href", "link", default="") or "").strip()
            board = str(_sget(src, "board", "board_token", "slug", "handle", default="") or "").strip()
            host = str(_sget(src, "host", default="") or "").strip()
            tenant = str(_sget(src, "tenant", default="") or "").strip()
            site = str(_sget(src, "site", default="") or "").strip()
            base_url = str(_sget(src, "base_url", default="") or "").strip()

            # Infer a type when omitted: board/host hints win, else generic URL.
            if not stype:
                if board and ("ashby" in label.lower() or False):
                    stype = "ashby"
                elif host or (board and False):
                    stype = "lever" if host else "greenhouse"
                elif tenant:
                    stype = "workday"
                elif careers_url:
                    stype = "generic"
                else:
                    stype = "generic"

            adapter = None
            url_meta = careers_url
            if stype in ("greenhouse", "gh"):
                if not board:
                    # Greenhouse entry without a board token -> generic scrape.
                    if not careers_url:
                        continue
                    from backend.adapters.generic import GenericAdapter

                    adapter = GenericAdapter(careers_url=careers_url, company=label)
                else:
                    from backend.adapters.greenhouse import GreenhouseAdapter

                    adapter = GreenhouseAdapter(board_token=board)
                    label = label or board
                    url_meta = careers_url or f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs"
            elif stype == "lever":
                if not host:
                    # Allow {"type": "lever", "board": "<host>"} alias.
                    host = board
                if not host:
                    if not careers_url:
                        continue
                    from backend.adapters.generic import GenericAdapter

                    adapter = GenericAdapter(careers_url=careers_url, company=label)
                else:
                    from backend.adapters.lever import LeverAdapter

                    adapter = LeverAdapter(host=host)
                    label = label or host
                    url_meta = careers_url or f"https://api.lever.co/v0/postings/{host}?mode=json"
            elif stype == "ashby":
                if not board:
                    if not careers_url:
                        continue
                    from backend.adapters.generic import GenericAdapter

                    adapter = GenericAdapter(careers_url=careers_url, company=label)
                else:
                    from backend.adapters.ashby import AshbyAdapter

                    adapter = AshbyAdapter(board=board)
                    label = label or board
                    url_meta = careers_url or f"https://api.ashbyhq.com/posting-api/job-board/{board}"
            elif stype == "workday":
                from backend.adapters.workday import WorkdayAdapter

                adapter = WorkdayAdapter(tenant=tenant, site=site, base_url=base_url)
                label = label or tenant or "workday"
                url_meta = careers_url or base_url or (f"workday:{tenant}/{site}".rstrip("/"))
            else:  # generic / unknown with a URL
                if not careers_url:
                    continue
                from backend.adapters.generic import GenericAdapter

                adapter = GenericAdapter(careers_url=careers_url, company=label)
                label = label or careers_url

            if adapter is None:
                continue
            try:
                adapter._source_label = label or getattr(adapter, "source", type(adapter).__name__)  # type: ignore[attr-defined]
                adapter._source_url = url_meta  # type: ignore[attr-defined]
            except Exception:
                pass
            adapters.append(adapter)
        except Exception as exc:
            log.warning("Could not build source adapter for %r: %s", src, exc)
            continue
    return adapters


def _describe_adapter(adapter: Any) -> tuple[str, str]:
    """Return (label, url) for per_source tracking. Never raises."""
    try:
        label = getattr(adapter, "_source_label", None) or getattr(adapter, "source", None)
        if not label:
            label = type(adapter).__name__
        url = getattr(adapter, "_source_url", None) or ""
        if not url:
            for attr in ("careers_url", "url"):
                val = getattr(adapter, attr, "")
                if isinstance(val, str) and val.startswith("http"):
                    url = val
                    break
        if not url:
            board_token = getattr(adapter, "board_token", "")
            if board_token:
                url = f"https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs"
        if not url:
            host = getattr(adapter, "host", "")
            if host and "lever" in str(getattr(adapter, "source", "")).lower():
                url = f"https://api.lever.co/v0/postings/{host}?mode=json"
        if not url:
            board = getattr(adapter, "board", "")
            if board and "ashby" in str(getattr(adapter, "source", "")).lower():
                url = f"https://api.ashbyhq.com/posting-api/job-board/{board}"
        if not url:
            base_url = getattr(adapter, "base_url", "")
            tenant = getattr(adapter, "tenant", "")
            if base_url:
                url = str(base_url)
            elif tenant:
                url = f"workday:{tenant}"
        return str(label), str(url or "")
    except Exception:
        return type(adapter).__name__, ""


# ---------------------------------------------------------------------------
# Main entry: POST /api/search/run
# ---------------------------------------------------------------------------

def run_search(
    config: Optional[dict] = None,
    *,
    conn: Optional[sqlite3.Connection] = None,
    db=None,
    geocoder: Optional[Geocoder] = None,
    own_connection: bool = True,
) -> dict:
    """Run one ingestion pass. Pure-python core of POST /api/search/run.

    Returns summary: fetched / excluded / inserted / updated / needs_scoring
    plus per_source[{label, url, count, excluded, error}] (one entry per adapter,
    company sources included automatically) and applied_filters
    {titles_n, include_n, exclude_n, salary_floor, allowed_modes, dealbreakers_n}.
    Never raises on adapter/network failure (adapters return []).

    Storage prefers backend.db (SQLAlchemy ``Job``) when importable so the
    FastAPI app and scorer see the same rows; otherwise falls back to the
    stdlib-sqlite ``store_jobs`` path. An explicit ``conn`` (sqlite3) or
    ``db`` (SQLAlchemy session) may be injected by callers/tests.
    """
    config = config or {}
    profile = load_profile()
    pins = load_pins(profile=profile)
    dealbreakers = load_dealbreakers(profile=profile)
    # config may override dealbreakers/pins directly
    if isinstance(config.get("dealbreakers"), (dict, list)):
        dealbreakers = normalise_dealbreakers(config["dealbreakers"])
    if isinstance(config.get("pins"), list):
        pins = load_pins(profile={"pins": config["pins"]})
    # Effective filter criteria: DB profile/dealbreakers/pins merged with
    # config overrides (titles/keywords_include/keywords_exclude/salary_floor/
    # modes/dealbreakers/pins). Keeps existing load_* behaviour above.
    # backend.filters.build_criteria returns (criteria, applied_filters);
    # the inline fallback returns a bare criteria dict — handle both.
    criteria: dict = {}
    try:
        _override_src = config if isinstance(config, dict) else {}
        _norm_override = dict(_override_src)
        # Task-level "modes" alias -> filters-level "allowed_modes".
        if "modes" in _norm_override and "allowed_modes" not in _norm_override:
            _norm_override["allowed_modes"] = _norm_override["modes"]
        _built = build_criteria(profile, dealbreakers, pins, _norm_override)
        if isinstance(_built, tuple) and len(_built) == 2 and isinstance(_built[0], dict):
            criteria = _built[0]
        elif isinstance(_built, dict):
            criteria = _built
    except Exception as exc:
        log.debug("build_criteria failed, using empty criteria: %s", exc)
        criteria = {}
    if not isinstance(criteria, dict):
        criteria = {}
    # profile keywords double as default SEEK keywords when no adapters set
    if not config.get("adapters") and (profile or criteria):
        titles = list(criteria.get("titles") or profile.get("titles") or [])
        kw_inc = list(criteria.get("keywords_include", criteria.get("include", [])) or profile.get("keywords_include") or [])
        if (titles or kw_inc) and "seek" not in config:
            config = {
                **config,
                "adapters": {
                    "seek": {
                        "enabled": True,
                        "keywords": list(titles) + list(kw_inc),
                        "location": _default_seek_location(pins, config),
                    }
                },
            }

    adapters = build_adapters(config)
    # test/injectable adapters (objects with .fetch) may be passed directly
    extra = config.get("_adapters")
    if isinstance(extra, list):
        adapters.extend([a for a in extra if hasattr(a, "fetch")])
    # Company sources are included automatically: explicit config["sources"]
    # list overrides auto-load; {"enabled": False} disables auto-load.
    try:
        cfg_sources = config.get("sources", None)
        if isinstance(cfg_sources, list):
            adapters.extend(build_sources_adapters(sources=cfg_sources, db=db))
        elif isinstance(cfg_sources, dict) and cfg_sources.get("enabled") is False:
            pass
        else:
            adapters.extend(build_sources_adapters(db=db))
    except Exception as exc:
        log.warning("Company sources skipped (isolated): %s", exc)
    # injectable sources adapters for tests
    extra_sources = config.get("_sources_adapters")
    if isinstance(extra_sources, list):
        adapters.extend([a for a in extra_sources if hasattr(a, "fetch")])
    if not adapters:
        log.warning("run_search: no adapters enabled — nothing to fetch.")

    fetched: list[NormalizedJob] = []
    per_source: list[dict] = []
    _jobs_per_source: list[list] = []
    for adapter in adapters:
        label, url = _describe_adapter(adapter)
        try:
            jobs = adapter.fetch() or []
            fetched.extend(jobs)
            per_source.append({"label": label, "url": url, "count": len(jobs), "excluded": 0, "error": None})
            _jobs_per_source.append(list(jobs))
        except Exception as exc:  # belt & braces: adapters must not kill ingest
            log.warning("Adapter %s crashed (isolated): %s", getattr(adapter, "source", "?"), exc)
            per_source.append({"label": label, "url": url, "count": 0, "excluded": 0, "error": str(exc)})
            _jobs_per_source.append([])

    # enrich
    excluded_count = 0
    for job in fetched:
        normalize_job(job)
        # work-mode: keep adapter value unless unknown, else smart classify
        # (prefers backend.profile.classify_work_mode).
        if (job.work_mode or "unknown").lower() in ("", "unknown", "other"):
            job.work_mode = smart_classify_work_mode(job, pins)
        else:
            job.work_mode = job.work_mode.strip().lower()
        # geocode when missing: explicit geocoder wins, else profile geocoder.
        if (job.lat is None or job.lng is None) and job.location_text:
            geo = None
            if geocoder is not None:
                try:
                    geo = geocoder(job.location_text)
                except Exception as exc:
                    log.debug("geocode failed for %r: %s", job.location_text, exc)
            if geo is None:
                try:
                    geo = smart_geocode(job.location_text)
                except Exception:
                    geo = None
            if geo:
                try:
                    lat, lng = geo
                    if job.lat is None and lat is not None:
                        job.lat = float(lat)
                    if job.lng is None and lng is not None:
                        job.lng = float(lng)
                except (TypeError, ValueError):
                    pass
        job.distance_km = distance_to_pins(job.lat, job.lng, pins)  # type: ignore[attr-defined]
        # PRE-LLM hard exclude
        reason = check_dealbreaker_excluded(job, dealbreakers)
        job.excluded_reason = reason  # type: ignore[attr-defined]
        if reason:
            excluded_count += 1
        # Unified filtering: titles/include/exclude/salary/modes (+dealbreakers).
        # Folded into the same excluded count; already-excluded jobs are kept as-is.
        if getattr(job, "excluded_reason", None) is None:
            try:
                filt = apply_filters(job, criteria)
            except Exception as exc:
                log.debug("apply_filters failed: %s", exc)
                filt = None
            filt_reason: Optional[str] = None
            try:
                if isinstance(filt, str):
                    filt_reason = filt.strip() or None
                elif isinstance(filt, dict):
                    raw_r = filt.get("excluded_reason", filt.get("reason"))
                    filt_reason = str(raw_r).strip() or None if raw_r else None
                elif isinstance(filt, (tuple, list)) and len(filt) == 2:
                    a, b = filt
                    if isinstance(a, bool):
                        filt_reason = None if a else (str(b).strip() or "filtered")
                    elif isinstance(b, bool):
                        filt_reason = None if b else (str(a).strip() or "filtered")
                    else:
                        filt_reason = str(b or a).strip() or None
                elif isinstance(filt, bool):
                    filt_reason = None if filt else "filtered"
                elif filt is None or filt == "" or filt is False:
                    filt_reason = None
                elif filt:
                    filt_reason = str(filt).strip() or None
            except Exception:
                filt_reason = None
            if filt_reason:
                job.excluded_reason = filt_reason  # type: ignore[attr-defined]
                excluded_count += 1

    # per-source excluded tallies (parallel to _jobs_per_source).
    try:
        for _idx, _js in enumerate(_jobs_per_source):
            try:
                _exc = sum(1 for _j in _js if getattr(_j, "excluded_reason", None))
            except Exception:
                _exc = 0
            if 0 <= _idx < len(per_source):
                per_source[_idx]["excluded"] = int(_exc)
    except Exception:
        pass

    # store: prefer ORM (backend.db.Job) when available.
    stats: dict
    if conn is not None:
        try:
            stats = store_jobs(conn, fetched)
        finally:
            if own_connection:
                try:
                    conn.close()
                except Exception:
                    pass
    elif db is not None:
        stats = store_jobs_orm(db, fetched)
    else:
        orm_db = _orm_session()
        if orm_db is not None:
            try:
                stats = store_jobs_orm(orm_db, fetched)
            finally:
                try:
                    orm_db.close()
                except Exception:
                    pass
        else:
            sql_conn = get_connection()
            try:
                stats = store_jobs(sql_conn, fetched)
            finally:
                sql_conn.close()

    try:
        _titles = criteria.get("titles") or []
        _inc = criteria.get("keywords_include", criteria.get("include", [])) or []
        _exc_list = criteria.get("keywords_exclude", criteria.get("exclude", [])) or []
        _modes = criteria.get("allowed_modes", criteria.get("modes", [])) or []
        _dbk = criteria.get("dealbreakers") if isinstance(criteria.get("dealbreakers"), dict) else dealbreakers
        _dbk_n = 0
        if isinstance(_dbk, dict):
            _dbk_n = len(_dbk.get("industries") or []) + len(_dbk.get("keywords") or [])
        applied_filters = {
            "titles_n": len(_titles) if isinstance(_titles, list) else 0,
            "include_n": len(_inc) if isinstance(_inc, list) else 0,
            "exclude_n": len(_exc_list) if isinstance(_exc_list, list) else 0,
            "salary_floor": criteria.get("salary_floor"),
            "allowed_modes": list(_modes) if isinstance(_modes, list) else [],
            "dealbreakers_n": int(_dbk_n),
        }
    except Exception:
        applied_filters = {
            "titles_n": 0,
            "include_n": 0,
            "exclude_n": 0,
            "salary_floor": None,
            "allowed_modes": [],
            "dealbreakers_n": 0,
        }

    return {
        "fetched": len(fetched),
        "excluded": excluded_count,
        "inserted": stats["inserted"],
        "updated": stats["updated"],
        "needs_scoring": stats["needs_scoring"],
        "per_source": per_source,
        "applied_filters": applied_filters,
    }


# Optional FastAPI wiring (import-time safe: only when fastapi is installed).
# The app owner can do: ``from backend.ingest import router`` +
# ``app.include_router(router)`` to serve POST /api/search/run.
try:  # pragma: no cover - only active inside the API process
    from fastapi import APIRouter as _APIRouter  # type: ignore

    router = _APIRouter(tags=["search"])

    @router.post("/api/search/run")
    def api_search_run(payload: dict | None = None) -> dict:  # type: ignore[valid-type]
        """POST /api/search/run — run ingestion over enabled adapters."""
        return run_search(payload or {})

except Exception:  # fastapi not installed (unit-test envs) -> skip router
    router = None  # type: ignore[assignment]


if __name__ == "__main__":  # pragma: no cover - manual CLI
    import argparse

    logging.basicConfig(level=logging.INFO)
    ap = argparse.ArgumentParser(description="Run job ingestion (POST /api/search/run core).")
    ap.add_argument("--config", default="", help="JSON config file (adapters/pins/dealbreakers).")
    args = ap.parse_args()
    cfg: dict = {}
    if args.config:
        cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    print(json.dumps(run_search(cfg), indent=2))

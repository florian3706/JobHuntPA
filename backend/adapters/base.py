"""Shared types + helpers for job adapters.

Covers:
- ``NormalizedJob`` dataclass (canonical schema for all sources)
- ``fetch()`` interface (``BaseAdapter``)
- text normalisation + ``description_hash`` (sha256)
- rate-limit helper (``RateLimiter`` + ``rate_limited`` decorator)
- HTML cache in ``data/cache/`` with 7-day TTL
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths / cache config
# ---------------------------------------------------------------------------

# backend/adapters/base.py -> parents[2] == <repo>/JobHuntPA
_REPO_ROOT = Path(__file__).resolve().parents[2]
CACHE_DIR = _REPO_ROOT / "data" / "cache"
CACHE_TTL = timedelta(days=7)


def get_cache_dir() -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR


# ---------------------------------------------------------------------------
# Normalised schema
# ---------------------------------------------------------------------------

@dataclass
class NormalizedJob:
    """Canonical job record produced by every adapter."""

    source: str = ""            # e.g. "seek", "greenhouse:myboard"
    external_id: str = ""       # id within the source board
    company: str = ""
    title: str = ""
    location_text: str = ""
    lat: Optional[float] = None
    lng: Optional[float] = None
    work_mode: str = "unknown"  # remote | hybrid | onsite | unknown
    salary_text: str = ""
    salary_min: Optional[float] = None
    salary_max: Optional[float] = None
    url: str = ""
    description: str = ""
    industry: str = ""

    def __post_init__(self) -> None:
        self.source = normalize_text(self.source)
        self.external_id = normalize_text(self.external_id)
        self.company = normalize_text(self.company)
        self.title = normalize_text(self.title)
        self.location_text = normalize_text(self.location_text)
        self.work_mode = (self.work_mode or "unknown").strip().lower() or "unknown"
        self.salary_text = normalize_text(self.salary_text)
        self.url = (self.url or "").strip()
        self.industry = normalize_text(self.industry)
        # description keeps newlines: only collapse trailing whitespace
        if self.description:
            self.description = normalize_description(self.description)

    @property
    def description_hash(self) -> str:
        return description_hash(self.description)

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "external_id": self.external_id,
            "company": self.company,
            "title": self.title,
            "location_text": self.location_text,
            "lat": self.lat,
            "lng": self.lng,
            "work_mode": self.work_mode,
            "salary_text": self.salary_text,
            "salary_min": self.salary_min,
            "salary_max": self.salary_max,
            "url": self.url,
            "description": self.description,
            "description_hash": self.description_hash,
            "industry": self.industry,
        }


# ---------------------------------------------------------------------------
# Normalisation + hashing
# ---------------------------------------------------------------------------

_WS_RE = re.compile(r"\s+")


def normalize_text(value: Optional[str]) -> str:
    """Collapse all whitespace to single spaces and strip."""
    if not value:
        return ""
    return _WS_RE.sub(" ", str(value)).strip()


def normalize_description(value: Optional[str]) -> str:
    """Normalise a job description for hashing/comparison.

    Strips HTML tags, collapses blank lines, strips trailing spaces.
    Keeps it deterministic so identical postings hash identically.
    """
    if not value:
        return ""
    text = str(value)
    # strip html tags (cheap, no bs4 dependency)
    text = re.sub(r"<[^>]+>", " ", text)
    # unescape a few common entities
    text = (
        text.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
    )
    lines = [normalize_text(line) for line in text.splitlines()]
    # drop leading/trailing blank lines, collapse 3+ blanks to max 1
    out: list[str] = []
    blank = 0
    for line in lines:
        if not line:
            blank += 1
            if blank <= 1 and out:
                out.append("")
            continue
        blank = 0
        out.append(line)
    while out and not out[-1]:
        out.pop()
    return "\n".join(out).strip()


def description_hash(description: Optional[str]) -> str:
    """sha256 hex of the normalised description (stable dedupe key)."""
    norm = normalize_description(description or "")
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


def normalize_job(job: NormalizedJob) -> NormalizedJob:
    """Re-run normalisation in place (post geocode/classify) and return job."""
    job.__post_init__()
    return job


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------

class RateLimiter:
    """Simple thread-safe minimum-interval rate limiter."""

    def __init__(self, min_interval_seconds: float = 1.0):
        self.min_interval = max(0.0, float(min_interval_seconds))
        self._lock = threading.Lock()
        self._last: float = 0.0

    def wait(self) -> float:
        """Sleep until the minimum interval has elapsed. Returns slept secs."""
        with self._lock:
            now = time.monotonic()
            delta = now - self._last
            sleep_for = self.min_interval - delta
            if sleep_for > 0:
                time.sleep(sleep_for)
                self._last = time.monotonic()
                return sleep_for
            self._last = now
            return 0.0


# module-global default limiter (polite 1s between outbound hits)
_default_limiter = RateLimiter(min_interval_seconds=1.0)


def rate_limited(min_interval_seconds: float = 1.0) -> Callable:
    """Decorator applying a per-function RateLimiter."""

    limiter = RateLimiter(min_interval_seconds)

    def deco(fn: Callable) -> Callable:
        def wrapper(*args, **kwargs):
            limiter.wait()
            return fn(*args, **kwargs)

        wrapper.__wrapped__ = fn  # type: ignore[attr-defined]
        return wrapper

    return deco


def polite_sleep(seconds: float = 1.0) -> None:
    """Sleep via the shared default limiter (use before raw httpx calls)."""
    _default_limiter.min_interval = max(_default_limiter.min_interval, seconds)
    _default_limiter.wait()


# ---------------------------------------------------------------------------
# HTML cache (data/cache/, 7-day TTL)
# ---------------------------------------------------------------------------

def _cache_key(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def cache_path_for(url: str) -> Path:
    return get_cache_dir() / (_cache_key(url) + ".html")


def get_cached_html(url: str, ttl: timedelta = CACHE_TTL) -> Optional[str]:
    """Return cached HTML for *url* if fresh, else None."""
    path = cache_path_for(url)
    if not path.exists():
        return None
    try:
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        if datetime.now(timezone.utc) - mtime > ttl:
            return None
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        log.debug("cache read failed for %s: %s", url, exc)
        return None


def set_cached_html(url: str, html: str) -> Path:
    """Write *html* to the cache and return the path."""
    path = cache_path_for(url)
    try:
        path.write_text(html or "", encoding="utf-8")
    except OSError as exc:
        log.debug("cache write failed for %s: %s", url, exc)
    return path


def clear_expired_cache(ttl: timedelta = CACHE_TTL) -> int:
    """Delete stale cache files. Returns number removed."""
    cache_dir = get_cache_dir()
    now = datetime.now(timezone.utc)
    removed = 0
    for child in cache_dir.glob("*.html"):
        try:
            mtime = datetime.fromtimestamp(child.stat().st_mtime, tz=timezone.utc)
            if now - mtime > ttl:
                child.unlink()
                removed += 1
        except OSError:
            continue
    return removed


# ---------------------------------------------------------------------------
# Adapter interface
# ---------------------------------------------------------------------------

class BaseAdapter(ABC):
    """All job adapters implement ``fetch()``."""

    source: str = "base"

    @abstractmethod
    def fetch(self) -> list[NormalizedJob]:
        """Fetch jobs; never raise on network failure (return [])."""
        raise NotImplementedError

    # convenience
    def normalize(self, job: NormalizedJob) -> NormalizedJob:
        return normalize_job(job)


__all__ = [
    "NormalizedJob",
    "BaseAdapter",
    "normalize_text",
    "normalize_description",
    "description_hash",
    "normalize_job",
    "RateLimiter",
    "rate_limited",
    "polite_sleep",
    "CACHE_DIR",
    "CACHE_TTL",
    "get_cache_dir",
    "get_cached_html",
    "set_cached_html",
    "cache_path_for",
    "clear_expired_cache",
]

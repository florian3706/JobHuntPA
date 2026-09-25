"""Generic careers-page scraper adapter.

Best-effort fallback for companies without a Greenhouse/Lever/Ashby/Workday
API: fetch the careers index page, collect job-ish <a> links, fetch up to
``max_jobs`` detail pages, and extract title / description / location with
tolerant regexes (no bs4 dependency).

All network access is wrapped in try/except and returns [] on failure —
the adapter never crashes an ingest run (offline-safe).
"""

from __future__ import annotations

import html as html_lib
import logging
import re
from urllib.parse import urljoin, urlparse

from backend.adapters.base import (
    BaseAdapter,
    NormalizedJob,
    RateLimiter,
    get_cached_html,
    normalize_text,
    set_cached_html,
)

log = logging.getLogger(__name__)

try:  # optional at runtime; missing httpx -> offline degrade to []
    import httpx  # type: ignore
except Exception:  # pragma: no cover - minimal env
    httpx = None  # type: ignore[assignment]

DEFAULT_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

# href substrings that usually indicate a job posting link.
JOB_HREF_HINTS = (
    "job",
    "career",
    "position",
    "opening",
    "vacancy",
    "vacancies",
    "role",
    "posting",
    "apply",
    "greenhouse",
    "lever",
    "ashby",
    "workday",
    "smartrecruiters",
    "breezy",
)

# hrefs that are never job pages.
_SKIP_SUFFIXES = (".css", ".js", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico",
                  ".pdf", ".zip", ".mp4", ".woff", ".woff2")

_ANCHOR_RE = re.compile(
    r'<a\s[^>]*href\s*=\s*["\'](?P<href>[^"\']+)["\'][^>]*>(?P<text>.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)
_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_STYLE_RE = re.compile(
    r"<(script|style)[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL
)
_OG_TITLE_RE = re.compile(
    r'<meta[^>]+property\s*=\s*["\']og:title["\'][^>]*content\s*=\s*["\'](?P<v>[^"\']+)["\']',
    re.IGNORECASE,
)
_OG_TITLE_REV = re.compile(
    r'<meta[^>]+content\s*=\s*["\'](?P<v>[^"\']+)["\'][^>]*property\s*=\s*["\']og:title["\']',
    re.IGNORECASE,
)
_TITLE_RE = re.compile(r"<title[^>]*>(?P<v>.*?)</title>", re.IGNORECASE | re.DOTALL)
_H1_RE = re.compile(r"<h1[^>]*>(?P<v>.*?)</h1>", re.IGNORECASE | re.DOTALL)
_META_LOC_RE = re.compile(
    r'<meta[^>]+(?:name|property)\s*=\s*["\'](?P<k>[^"\']*(?:location|address|locality|job-location|job_location)[^"\']*)["\'][^>]*content\s*=\s*["\'](?P<v>[^"\']+)["\']',
    re.IGNORECASE,
)
_META_LOC_REV = re.compile(
    r'<meta[^>]+content\s*=\s*["\'](?P<v>[^"\']+)["\'][^>]*?(?:name|property)\s*=\s*["\'](?P<k>[^"\']*(?:location|address|locality)[^"\']*)["\']',
    re.IGNORECASE,
)

DESCRIPTION_SLICE = 8000


def _clean(raw: str) -> str:
    text = _TAG_RE.sub(" ", raw or "")
    text = html_lib.unescape(text)
    return normalize_text(text)


def _html_to_text(html: str) -> str:
    """Strip scripts/styles/tags -> single-spaced text."""
    if not html:
        return ""
    text = _SCRIPT_STYLE_RE.sub(" ", html)
    text = _TAG_RE.sub(" ", text)
    text = html_lib.unescape(text)
    return normalize_text(text)


def _fetch_url(url: str, timeout: float = 15.0) -> str | None:
    """GET *url* with UA + cache. Returns HTML text or None. Never raises."""
    try:
        cached = get_cached_html(url)
        if cached is not None:
            return cached
    except Exception:
        pass
    if httpx is None:
        log.warning("httpx not installed; generic fetch skipped for %s", url)
        return None
    try:
        with httpx.Client(
            headers={"User-Agent": DEFAULT_UA, "Accept-Language": "en;q=0.9"},
            timeout=timeout,
            follow_redirects=True,
        ) as client:
            resp = client.get(url)
            resp.raise_for_status()
            text = resp.text or ""
            try:
                set_cached_html(url, text)
            except Exception:
                pass
            return text
    except Exception as exc:
        log.warning("Generic fetch failed for %s: %s", url, exc)
        return None


def _is_job_href(href: str) -> bool:
    h = (href or "").strip()
    if not h or h.startswith(("#", "mailto:", "tel:", "javascript:")):
        return False
    low = h.lower()
    if low.endswith(_SKIP_SUFFIXES):
        return False
    return any(hint in low for hint in JOB_HREF_HINTS)


def extract_job_links(html: str, base_url: str = "") -> list[str]:
    """Collect absolute job-ish URLs from index HTML (deduped, order kept)."""
    seen: set[str] = set()
    out: list[str] = []
    if not html:
        return out
    try:
        for m in _ANCHOR_RE.finditer(html):
            href = (m.group("href") or "").strip()
            if not _is_job_href(href):
                continue
            try:
                abs_url = urljoin(base_url, href).split("#")[0].strip()
            except Exception:
                continue
            if not abs_url.startswith(("http://", "https://")):
                continue
            if abs_url in seen:
                continue
            seen.add(abs_url)
            out.append(abs_url)
    except Exception as exc:
        log.debug("extract_job_links failed: %s", exc)
    return out


def extract_title(html: str) -> str:
    """Title from og:title -> <title> -> <h1>. Returns '' when absent."""
    if not html:
        return ""
    try:
        for rx in (_OG_TITLE_RE, _OG_TITLE_REV):
            m = rx.search(html)
            if m and m.group("v").strip():
                return _clean(m.group("v"))
        m = _TITLE_RE.search(html)
        if m and _clean(m.group("v")):
            return _clean(m.group("v"))
        m = _H1_RE.search(html)
        if m and _clean(m.group("v")):
            return _clean(m.group("v"))
    except Exception as exc:
        log.debug("extract_title failed: %s", exc)
    return ""


def extract_location(html: str) -> str:
    """Location from <meta name/property=*location*/address*> content."""
    if not html:
        return ""
    try:
        for rx in (_META_LOC_RE, _META_LOC_REV):
            m = rx.search(html)
            if m and m.group("v").strip():
                return _clean(m.group("v"))
    except Exception as exc:
        log.debug("extract_location failed: %s", exc)
    return ""


def extract_description(html: str, limit: int = DESCRIPTION_SLICE) -> str:
    """Main text slice of a detail page (tags stripped, capped at *limit*)."""
    try:
        text = _html_to_text(html or "")
        return text[:limit].strip()
    except Exception as exc:
        log.debug("extract_description failed: %s", exc)
        return ""


def _domain_of(url: str) -> str:
    try:
        return urlparse(url or "").netloc.lower()
    except Exception:
        return ""


class GenericAdapter(BaseAdapter):
    """Scrape a generic careers page + up to ``max_jobs`` detail pages."""

    def __init__(
        self,
        careers_url: str,
        company: str = "",
        *,
        timeout: float = 15.0,
        min_interval_seconds: float = 1.0,
        max_jobs: int = 10,
    ):
        self.careers_url = (careers_url or "").strip()
        self.company = (company or "").strip() or _domain_of(self.careers_url)
        domain = _domain_of(self.careers_url)
        self.source = f"generic:{domain}" if domain else "generic"
        self.timeout = timeout
        self.max_jobs = max(1, int(max_jobs or 10))
        self.limiter = RateLimiter(min_interval_seconds)

    def fetch(self) -> list[NormalizedJob]:
        if not self.careers_url:
            log.warning("GenericAdapter: empty careers_url.")
            return []
        try:
            self.limiter.wait()
            index_html = _fetch_url(self.careers_url, timeout=self.timeout)
        except Exception as exc:
            log.warning("Generic index fetch crashed for %s: %s", self.careers_url, exc)
            return []
        if not index_html:
            return []
        try:
            links = extract_job_links(index_html, base_url=self.careers_url)
        except Exception as exc:
            log.warning("Generic link parse failed for %s: %s", self.careers_url, exc)
            return []
        jobs: list[NormalizedJob] = []
        for link in links[: self.max_jobs]:
            try:
                self.limiter.wait()
                detail_html = _fetch_url(link, timeout=self.timeout)
            except Exception as exc:
                log.debug("Generic detail fetch crashed for %s: %s", link, exc)
                continue
            if not detail_html:
                continue
            try:
                title = extract_title(detail_html) or _domain_of(link)
                location = extract_location(detail_html)
                description = extract_description(detail_html)
                if not title and not description:
                    continue
                jobs.append(
                    NormalizedJob(
                        source=self.source,
                        external_id=link,
                        company=self.company,
                        title=title,
                        location_text=location,
                        url=link,
                        description=description,
                    )
                )
            except Exception as exc:
                log.debug("Skipping generic job %s: %s", link, exc)
                continue
        return jobs


__all__ = [
    "GenericAdapter",
    "extract_job_links",
    "extract_title",
    "extract_location",
    "extract_description",
    "DEFAULT_UA",
    "JOB_HREF_HINTS",
]

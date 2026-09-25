"""SEEK (seek.com.au) search-results adapter.

SEEK's robots.txt allows search result pages (``/jobs?keywords=...``) but
disallows every ``/job/<id>`` detail page, so this adapter never fetches
details. Search pages embed the full result set as JSON
(``window.SEEK_REDUX_DATA``): title, advertiser, location, salary, work
arrangement, teaser and bullet points. Those become a *summary*
description (``detail_status="summary"``).

SEEK may refuse automated clients (HTTP 403). The adapter reports that
instead of disguising itself as a browser. Pages saved from your own
browser can be imported instead (``parse_seek_page`` +
``POST /api/import/page``), which also gives full descriptions for saved
job detail pages.
"""
from __future__ import annotations

import json
import re
from datetime import timedelta
from typing import Any, Optional
from urllib.parse import urlencode

from backend.adapters.base import Adapter, Posting, SourceError
from backend.scraping.http import FetchError, PoliteClient
from backend.scraping.text import clean, html_to_text

SEARCH_BASE = "https://au.seek.com"
JOB_URL = "https://www.seek.com.au/job/{id}"
LISTING_TTL = timedelta(hours=1)

_REDUX_RE = re.compile(r"window\.SEEK_REDUX_DATA\s*=\s*(\{.*?\});?\s*\n", re.DOTALL)
_MODES = {"on-site": "onsite", "onsite": "onsite", "hybrid": "hybrid", "remote": "remote"}


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-")


def build_search_url(keywords: str, location: str = "") -> str:
    """SEEK's canonical search URL, e.g. /Product-Manager-jobs/in-All-Sydney-NSW.

    robots.txt disallows every query string except "?keywords" (and SEEK
    redirects that to the path form *with* a query), so only the query-less
    path form is usable. That also means only the first results page.
    """
    url = f"{SEARCH_BASE}/{_slug(keywords)}-jobs"
    if location:
        url += f"/in-{_slug(location)}"
    return url


def _redux(html: str) -> Optional[dict]:
    m = _REDUX_RE.search(html or "")
    if not m:
        return None
    raw = m.group(1).replace(":undefined", ":null")
    try:
        return json.loads(raw)
    except ValueError:
        return None


def _work_mode(labels: list[str]) -> str:
    for label in labels:
        mode = _MODES.get(label.strip().lower())
        if mode:
            return mode
    return "unknown"


def _posting_from_result(job: dict) -> Optional[Posting]:
    job_id = str(job.get("id") or "").strip()
    title = clean(job.get("title"))
    if not job_id or not title:
        return None
    locations = job.get("locations") or []
    loc = locations[0] if locations and isinstance(locations[0], dict) else {}
    arrangements = [
        clean((a.get("label") or {}).get("text"))
        for a in ((job.get("workArrangements") or {}).get("data") or [])
        if isinstance(a, dict)
    ]
    classes = [
        clean((c.get("subclassification") or c.get("classification") or {}).get("description"))
        for c in job.get("classifications") or []
        if isinstance(c, dict)
    ]
    summary_parts = []
    if job.get("teaser"):
        summary_parts.append(clean(job["teaser"]))
    bullets = [clean(b) for b in job.get("bulletPoints") or [] if clean(b)]
    if bullets:
        summary_parts.append("\n".join(f"- {b}" for b in bullets))
    if job.get("workTypes"):
        summary_parts.append("Work type: " + ", ".join(clean(w) for w in job["workTypes"]))
    if classes:
        summary_parts.append("Classification: " + ", ".join(c for c in classes if c))
    return Posting(
        url=JOB_URL.format(id=job_id),
        title=title,
        company=clean(job.get("companyName") or (job.get("advertiser") or {}).get("description")),
        external_id=job_id,
        location_text=clean(loc.get("label")),
        country=clean(loc.get("countryCode")).upper(),
        work_mode=_work_mode(arrangements),
        salary_text=clean(job.get("salaryLabel")),
        description="\n\n".join(summary_parts),
        detail_status="summary",
        posted_at=clean(job.get("listingDate")),
        industry=classes[0] if classes else "",
    )


def parse_search_page(html: str) -> tuple[list[Posting], int]:
    """Postings + total page count from a SEEK search results page."""
    data = _redux(html)
    if data is None:
        return [], 0
    results = data.get("results") or {}
    jobs = ((results.get("results") or {}).get("jobs")) or []
    postings = [p for p in (_posting_from_result(j) for j in jobs if isinstance(j, dict)) if p]
    return postings, int(results.get("totalPages") or 1)


def parse_job_page(html: str) -> Optional[Posting]:
    """Full posting from a saved SEEK job detail page."""
    data = _redux(html)
    job: Any = (((data or {}).get("jobdetails") or {}).get("result") or {}).get("job")
    if not isinstance(job, dict) or not job.get("id"):
        return None
    result = data["jobdetails"]["result"]
    arrangements = [clean((a.get("label") or {}).get("text")) if isinstance(a.get("label"), dict) else clean(a.get("label"))
                    for a in ((result.get("workArrangements") or {}).get("arrangements") or []) if isinstance(a, dict)]
    salary = job.get("salary") or {}
    description = html_to_text(job.get("content") or "")
    return Posting(
        url=JOB_URL.format(id=job["id"]),
        title=clean(job.get("title")),
        company=clean((job.get("advertiser") or {}).get("name")),
        external_id=str(job["id"]),
        location_text=clean((job.get("location") or {}).get("label")),
        country="AU",
        work_mode=_work_mode(arrangements),
        salary_text=clean(salary.get("label") if isinstance(salary, dict) else ""),
        description=description,
        detail_status="full" if description else "none",
        posted_at=clean((job.get("listedAt") or {}).get("dateTimeUtc")),
        industry=clean(((job.get("classifications") or [{}])[0] or {}).get("label")),
    )


def parse_seek_page(html: str) -> list[Posting]:
    """Parse any saved SEEK page (search results or a single job)."""
    single = parse_job_page(html)
    if single is not None:
        return [single]
    postings, _ = parse_search_page(html)
    return postings


class SeekAdapter(Adapter):
    source = "seek"
    complete_listing = False  # search results are a moving window
    has_details = False  # job pages are disallowed by robots.txt
    method = "SEEK search result pages, first page per search (job pages and pagination are off-limits per robots.txt)"

    def __init__(self, client: PoliteClient, keywords: list[str], locations: list[str]):
        self.client = client
        self.keywords = [k for k in (clean(k) for k in keywords) if k]
        self.locations = [l for l in (clean(l) for l in locations) if l] or [""]

    def list_postings(self) -> list[Posting]:
        if not self.keywords:
            raise SourceError("no job titles configured to search SEEK with")
        seen: dict[str, Posting] = {}
        for keyword in self.keywords:
            for location in self.locations:
                url = build_search_url(keyword, location)
                try:
                    resp = self.client.get(url, ttl=LISTING_TTL)
                except FetchError as exc:
                    # A refusal will repeat for every query: stop at once.
                    raise SourceError(f"SEEK: {exc}") from exc
                if _redux(resp.text) is None:
                    raise SourceError("SEEK returned a page without search data (blocked or changed layout)")
                postings, _ = parse_search_page(resp.text)
                for p in postings:
                    seen.setdefault(p.url, p)
        return list(seen.values())

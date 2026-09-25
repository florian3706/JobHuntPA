"""Seek (seek.com.au) search adapter.

- Query by keywords x location via httpx (server-rendered search pages).
- Optional Playwright fallback when static fetch is blocked/empty.
- Parses job cards into :class:`NormalizedJob` with ordered fallbacks:
  (a) data-automation jobTitle regex, (b) JSON-LD JobPosting blocks,
  (c) __NEXT_DATA__ embedded JSON scan, (d) generic /job/ anchor scan.
- Respects rate limits; degrades gracefully offline (returns [] + warning).
"""

from __future__ import annotations

import html as html_lib
import json
import logging
import re
from typing import Iterable, Optional
from urllib.parse import urlencode

from backend.adapters.base import (
    BaseAdapter,
    NormalizedJob,
    RateLimiter,
    get_cached_html,
    normalize_text,
    set_cached_html,
)

log = logging.getLogger(__name__)

try:  # optional dependency
    import httpx  # type: ignore
except Exception:  # pragma: no cover - offline / minimal env
    httpx = None  # type: ignore[assignment]

SEARCH_BASE = "https://www.seek.com.au/jobs"
DEFAULT_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

# (a) very tolerant card matcher: <a ... data-automation="jobTitle" href="...">Title</a>
# plus nearby company/location spans. We parse each job link block instead of
# depending on exact SEEK markup (which changes often).
_JOB_LINK_RE = re.compile(
    r'<a[^>]+data-automation="jobTitle"[^>]*href="(?P<href>[^"]+)"[^>]*>'
    r"(?P<title>.*?)</a>",
    re.IGNORECASE | re.DOTALL,
)
_COMPANY_RE = re.compile(
    r'data-automation="jobCompany"[^>]*>(?P<v>.*?)<', re.IGNORECASE | re.DOTALL
)
_LOCATION_RE = re.compile(
    r'data-automation="jobLocation"[^>]*>(?P<v>.*?)<', re.IGNORECASE | re.DOTALL
)
_SALARY_RE = re.compile(
    r'data-automation="jobSalary"[^>]*>(?P<v>.*?)<', re.IGNORECASE | re.DOTALL
)
_TAG_RE = re.compile(r"<[^>]+>")

# (b) JSON-LD blocks: <script type="application/ld+json">...</script>
_JSONLD_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(?P<json>.*?)</script>',
    re.IGNORECASE | re.DOTALL,
)

# (c) Next.js embedded data
_NEXT_DATA_RE = re.compile(
    r'<script[^>]*id=["\']__NEXT_DATA__["\'][^>]*>(?P<json>.*?)</script>',
    re.IGNORECASE | re.DOTALL,
)
_NEXT_JOBTITLE_RE = re.compile(
    r'"jobTitle"\s*:\s*"(?P<title>(?:\\.|[^"\\])*)"', re.DOTALL
)

# (d) generic anchor scan: any <a href=".../job/<id>...">Text</a>
_GENERIC_JOB_ANCHOR_RE = re.compile(
    r'<a[^>]+href="(?P<href>(?:/job/\d+[^"]*|https?://[^"]*/job/\d+[^"]*))"[^>]*>'
    r"(?P<title>.*?)</a>",
    re.IGNORECASE | re.DOTALL,
)

_SCRIPT_STYLE_RE = re.compile(
    r"<(script|style)[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL
)
_META_LOC_RE = re.compile(
    r'<meta[^>]+(?:name|property)\s*=\s*["\'](?P<k>[^"\']*(?:location|address|locality|job-location|job_location)[^"\']*)["\'][^>]*content\s*=\s*["\'](?P<v>[^"\']+)["\']',
    re.IGNORECASE,
)
_META_LOC_REV = re.compile(
    r'<meta[^>]+content\s*=\s*["\'](?P<v>[^"\']+)["\'][^>]*?(?:name|property)\s*=\s*["\'](?P<k>[^"\']*(?:location|address|locality)[^"\']*)["\']',
    re.IGNORECASE,
)

DETAIL_SLICE = 8000
ENRICH_LIMIT = 10


def _clean(raw: str) -> str:
    text = _TAG_RE.sub(" ", raw or "")
    text = html_lib.unescape(text)
    return normalize_text(text)


def _html_to_text(html: str) -> str:
    if not html:
        return ""
    text = _SCRIPT_STYLE_RE.sub(" ", html)
    text = _TAG_RE.sub(" ", text)
    text = html_lib.unescape(text)
    return normalize_text(text)


def _abs_url(href: str) -> str:
    href = (href or "").strip()
    if href.startswith("http"):
        return href
    if href.startswith("/"):
        return "https://www.seek.com.au" + href
    return href


def build_search_urls(
    keywords: Iterable[str] | str,
    location: Optional[str] = None,
    max_pages: int = 1,
) -> list[str]:
    """Build SEEK search URLs for each keyword (+ optional location)."""
    if isinstance(keywords, str):
        keywords = [keywords]
    kws = [k.strip() for k in keywords if k and k.strip()] or [""]
    urls: list[str] = []
    for kw in kws:
        for page in range(1, max(1, max_pages) + 1):
            params: dict[str, str] = {}
            if kw:
                params["keywords"] = kw
            if location:
                params["where"] = location
            if page > 1:
                params["page"] = str(page)
            urls.append(SEARCH_BASE + ("?" + urlencode(params) if params else ""))
    return urls


# ---------------------------------------------------------------------------
# Parser stages (run IN ORDER, deduped by url)
# ---------------------------------------------------------------------------

def _stage_jobtitle(html: str) -> list[NormalizedJob]:
    """Stage (a): existing data-automation jobTitle regex."""
    jobs: list[NormalizedJob] = []
    for m in _JOB_LINK_RE.finditer(html or ""):
        href = _abs_url(m.group("href"))
        title = _clean(m.group("title"))
        window = html[m.end(): m.end() + 4000]
        company_m = _COMPANY_RE.search(window)
        loc_m = _LOCATION_RE.search(window)
        sal_m = _SALARY_RE.search(window)
        company = _clean(company_m.group("v")) if company_m else ""
        loc = _clean(loc_m.group("v")) if loc_m else ""
        salary = _clean(sal_m.group("v")) if sal_m else ""
        external_id = ""
        id_m = re.search(r"/job/(\d+)", href)
        if id_m:
            external_id = id_m.group(1)
        if not title or not href:
            continue
        jobs.append(
            NormalizedJob(
                source="seek",
                external_id=external_id or href,
                company=company,
                title=title,
                location_text=loc,
                salary_text=salary,
                url=href,
                description="",
            )
        )
    return jobs


def _location_from_jsonld_node(node: object) -> str:
    """Best-effort location string from a JSON-LD jobLocation node."""
    if isinstance(node, str):
        return _clean(node)
    if isinstance(node, dict):
        addr = node.get("address")
        if isinstance(addr, dict):
            parts: list[str] = []
            for k in ("addressLocality", "addressRegion", "addressCountry"):
                v = addr.get(k)
                if isinstance(v, str) and v.strip():
                    parts.append(v.strip())
                elif isinstance(v, dict) and isinstance(v.get("name"), str):
                    if v["name"].strip():
                        parts.append(v["name"].strip())
            if parts:
                return _clean(", ".join(parts))
            name = node.get("name")
            if isinstance(name, str) and name.strip():
                return _clean(name)
            return ""
        if isinstance(addr, str) and addr.strip():
            return _clean(addr)
        for k in ("name", "addressLocality", "addressRegion"):
            v = node.get(k)
            if isinstance(v, str) and v.strip():
                return _clean(v)
    return ""


def _jsonld_dict_to_job(d: dict) -> Optional[NormalizedJob]:
    try:
        if not isinstance(d, dict):
            return None
        dtype = d.get("@type", "")
        if isinstance(dtype, list):
            is_posting = any(
                isinstance(t, str) and t.lower() == "jobposting" for t in dtype
            )
        else:
            is_posting = isinstance(dtype, str) and dtype.lower() == "jobposting"
        if not is_posting:
            return None
        raw_title = d.get("title") or d.get("name") or ""
        title = _clean(str(raw_title)) if raw_title else ""
        if not title:
            return None
        raw_url = d.get("url") or ""
        if isinstance(raw_url, list):
            raw_url = raw_url[0] if raw_url else ""
        url = _abs_url(str(raw_url).strip()) if raw_url else ""
        if not url:
            return None
        company = ""
        org = d.get("hiringOrganization")
        if isinstance(org, dict):
            company = _clean(str(org.get("name", "") or ""))
        elif isinstance(org, list) and org and isinstance(org[0], dict):
            company = _clean(str(org[0].get("name", "") or ""))
        elif isinstance(org, str):
            company = _clean(org)
        loc = ""
        jl = d.get("jobLocation")
        if isinstance(jl, list):
            for node in jl:
                loc = _location_from_jsonld_node(node)
                if loc:
                    break
        elif jl is not None:
            loc = _location_from_jsonld_node(jl)
        external_id = ""
        id_m = re.search(r"/job/(\d+)", url)
        if id_m:
            external_id = id_m.group(1)
        return NormalizedJob(
            source="seek",
            external_id=external_id or url,
            company=company,
            title=title,
            location_text=loc,
            url=url,
            description="",
        )
    except Exception:
        return None


def _iter_jsonld_candidates(obj: object) -> Iterable[dict]:
    """Yield dict candidates from a parsed JSON-LD blob (@graph/list/single)."""
    if isinstance(obj, dict):
        if "@graph" in obj and isinstance(obj["@graph"], list):
            for item in obj["@graph"]:
                if isinstance(item, dict):
                    yield item
        else:
            yield obj
    elif isinstance(obj, list):
        for item in obj:
            if isinstance(item, dict):
                if "@graph" in item and isinstance(item["@graph"], list):
                    for sub in item["@graph"]:
                        if isinstance(sub, dict):
                            yield sub
                else:
                    yield item


def _stage_jsonld(html: str) -> list[NormalizedJob]:
    """Stage (b): JSON-LD <script type="application/ld+json"> JobPosting blocks."""
    jobs: list[NormalizedJob] = []
    if not html:
        return jobs
    for m in _JSONLD_RE.finditer(html):
        raw = (m.group("json") or "").strip()
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except Exception:
            continue
        for cand in _iter_jsonld_candidates(payload):
            job = _jsonld_dict_to_job(cand)
            if job is not None:
                jobs.append(job)
    return jobs


def _collect_next_data_jobs(obj: object, acc: list[NormalizedJob]) -> None:
    """Recursively collect job-like dicts from parsed __NEXT_DATA__ JSON."""
    if isinstance(obj, dict):
        jt = obj.get("jobTitle")
        if isinstance(jt, str) and jt.strip():
            title = _clean(jt)
            url = ""
            for k in (
                "jobUrl", "url", "shareUrl", "link",
                "canonicalUrl", "href", "jobLink", "applyUrl",
            ):
                v = obj.get(k)
                if isinstance(v, str) and v.strip() and (
                    "/job/" in v or v.strip().startswith(("http", "/"))
                ):
                    url = _abs_url(v.strip().split("#")[0])
                    break
            if not url:
                for k in ("jobId", "id", "jobID", "seekJobId", "job_id"):
                    v = obj.get(k)
                    if isinstance(v, (str, int)):
                        s = str(v).strip()
                        mm = re.search(r"(\d+)", s)
                        if mm:
                            url = f"https://www.seek.com.au/job/{mm.group(1)}"
                            break
                        if s.startswith("http") or s.startswith("/job/"):
                            url = _abs_url(s)
                            break
            if title and url:
                company = ""
                for k in (
                    "companyName", "company", "advertiser",
                    "recruiter", "hiringCompany", "employer",
                ):
                    v = obj.get(k)
                    if isinstance(v, dict):
                        nn = v.get("name") or v.get("displayName") or v.get("title") or ""
                        if isinstance(nn, str) and nn.strip():
                            company = _clean(nn)
                            break
                    elif isinstance(v, str) and v.strip():
                        company = _clean(v)
                        break
                loc = ""
                for k in (
                    "location", "jobLocation", "locationName",
                    "suburb", "area", "city", "where",
                ):
                    v = obj.get(k)
                    if isinstance(v, dict):
                        nn = v.get("displayName") or v.get("name") or v.get("label") or ""
                        if isinstance(nn, str) and nn.strip():
                            loc = _clean(nn)
                            break
                    elif isinstance(v, str) and v.strip():
                        loc = _clean(v)
                        break
                eid = ""
                mm2 = re.search(r"/job/(\d+)", url)
                if mm2:
                    eid = mm2.group(1)
                try:
                    acc.append(
                        NormalizedJob(
                            source="seek",
                            external_id=eid or url,
                            company=company,
                            title=title,
                            location_text=loc,
                            url=url,
                            description="",
                        )
                    )
                except Exception:
                    pass
        elif isinstance(obj.get("title"), str) and obj["title"].strip():
            # generic "title" guarded by a job identifier to avoid false positives
            url_candidate = ""
            has_job_id = False
            for k in (
                "jobUrl", "url", "shareUrl", "link",
                "canonicalUrl", "href", "jobLink",
            ):
                v = obj.get(k)
                if isinstance(v, str) and "/job/" in v:
                    url_candidate = _abs_url(v.strip())
                    has_job_id = True
                    break
            if not has_job_id:
                for k in ("jobId", "jobID", "seekJobId"):
                    v = obj.get(k)
                    if isinstance(v, (str, int)) and re.search(r"\d+", str(v)):
                        mm = re.search(r"(\d+)", str(v))
                        if mm:
                            url_candidate = (
                                f"https://www.seek.com.au/job/{mm.group(1)}"
                            )
                            has_job_id = True
                            break
            if has_job_id and url_candidate:
                title = _clean(str(obj["title"]))
                if title:
                    eid = ""
                    mm2 = re.search(r"/job/(\d+)", url_candidate)
                    if mm2:
                        eid = mm2.group(1)
                    try:
                        acc.append(
                            NormalizedJob(
                                source="seek",
                                external_id=eid or url_candidate,
                                company="",
                                title=title,
                                location_text="",
                                url=url_candidate,
                                description="",
                            )
                        )
                    except Exception:
                        pass
        for v in obj.values():
            if isinstance(v, (dict, list)):
                _collect_next_data_jobs(v, acc)
    elif isinstance(obj, list):
        for item in obj:
            if isinstance(item, (dict, list)):
                _collect_next_data_jobs(item, acc)


def _stage_next_data(html: str) -> list[NormalizedJob]:
    """Stage (c): __NEXT_DATA__ embedded JSON scan for jobTitle/job titles."""
    jobs: list[NormalizedJob] = []
    if not html:
        return jobs
    blobs = [m.group("json") for m in _NEXT_DATA_RE.finditer(html)]
    for raw in blobs:
        raw = (raw or "").strip()
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except Exception:
            payload = None
        if payload is not None:
            try:
                _collect_next_data_jobs(payload, jobs)
            except Exception:
                pass
        # regex fallback inside the blob: pair each jobTitle with a nearby /job/<id>
        try:
            for tm in _NEXT_JOBTITLE_RE.finditer(raw):
                title = _clean(
                    tm.group("title").encode("utf-8").decode("unicode_escape", errors="ignore")
                    if "\\" in tm.group("title") else tm.group("title")
                )
                if not title:
                    continue
                start, end = tm.span()
                window = raw[max(0, start - 2000): end + 2000]
                im = re.search(r"/job/(\d+)", window)
                if not im:
                    continue
                url = f"https://www.seek.com.au/job/{im.group(1)}"
                jobs.append(
                    NormalizedJob(
                        source="seek",
                        external_id=im.group(1),
                        company="",
                        title=title,
                        location_text="",
                        url=url,
                        description="",
                    )
                )
        except Exception:
            continue
    return jobs


def _stage_generic_anchors(html: str) -> list[NormalizedJob]:
    """Stage (d): generic anchor scan href=/job/<id> with text."""
    jobs: list[NormalizedJob] = []
    if not html:
        return jobs
    for m in _GENERIC_JOB_ANCHOR_RE.finditer(html):
        href = _abs_url(m.group("href"))
        title = _clean(m.group("title"))
        if not title or not href:
            continue
        external_id = ""
        id_m = re.search(r"/job/(\d+)", href)
        if id_m:
            external_id = id_m.group(1)
        jobs.append(
            NormalizedJob(
                source="seek",
                external_id=external_id or href,
                company="",
                title=title,
                location_text="",
                url=href,
                description="",
            )
        )
    return jobs


def parse_seek_html(html: str, search_url: str = "") -> list[NormalizedJob]:
    """Parse SEEK search HTML into NormalizedJobs (best-effort).

    Fallback order: (a) data-automation jobTitle regex, (b) JSON-LD
    JobPosting blocks, (c) __NEXT_DATA__ embedded JSON scan, (d) generic
    ``/job/<id>`` anchor scan. Results are deduped by url (first wins).
    """
    if not html:
        return []
    ordered: list[NormalizedJob] = []
    try:
        ordered.extend(_stage_jobtitle(html))
    except Exception:
        pass
    try:
        ordered.extend(_stage_jsonld(html))
    except Exception:
        pass
    try:
        ordered.extend(_stage_next_data(html))
    except Exception:
        pass
    try:
        ordered.extend(_stage_generic_anchors(html))
    except Exception:
        pass
    # dedupe by url within page (first stage wins)
    seen: set[str] = set()
    uniq: list[NormalizedJob] = []
    for j in ordered:
        try:
            key = j.url or (j.title + j.company)
        except Exception:
            continue
        if not key or key in seen:
            continue
        seen.add(key)
        uniq.append(j)
    return uniq


def _fetch_with_httpx(url: str, timeout: float = 15.0) -> Optional[str]:
    if httpx is None:
        log.warning("httpx not installed; cannot fetch %s", url)
        return None
    cached = get_cached_html(url)
    if cached is not None:
        return cached
    try:
        with httpx.Client(
            headers={"User-Agent": DEFAULT_UA, "Accept-Language": "en-AU,en;q=0.9"},
            timeout=timeout,
            follow_redirects=True,
        ) as client:
            resp = client.get(url)
            resp.raise_for_status()
            set_cached_html(url, resp.text)
            return resp.text
    except Exception as exc:  # network, DNS, TLS, HTTP errors -> graceful []
        log.warning("SEEK fetch failed for %s: %s", url, exc)
        return None


def _fetch_with_playwright(url: str, timeout_ms: int = 20000) -> Optional[str]:
    """Optional JS-render fallback. Returns None when unavailable/failed."""
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
    except Exception:
        return None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page(user_agent=DEFAULT_UA)
                page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
                page.wait_for_timeout(2500)
                content = page.content()
                set_cached_html(url, content)
                return content
            finally:
                browser.close()
    except Exception as exc:
        log.warning("SEEK playwright fallback failed for %s: %s", url, exc)
        return None


def fetch_job_detail(url: str, timeout: float = 15.0) -> dict:
    """Fetch a SEEK job detail page -> ``{"description": ..., "location": ...}``.

    Uses httpx GET with the HTML cache, strips tags to a text slice of 8000
    chars, and extracts location from meta tags / data-automation markup.
    Never raises — returns empty strings on failure.
    """
    result = {"description": "", "location": ""}
    if not url:
        return result
    try:
        html: Optional[str] = None
        try:
            html = get_cached_html(url)
        except Exception:
            html = None
        if html is None:
            if httpx is None:
                return result
            with httpx.Client(
                headers={"User-Agent": DEFAULT_UA, "Accept-Language": "en-AU,en;q=0.9"},
                timeout=timeout,
                follow_redirects=True,
            ) as client:
                resp = client.get(url)
                resp.raise_for_status()
                html = resp.text or ""
                try:
                    set_cached_html(url, html)
                except Exception:
                    pass
        if not html:
            return result
        loc = ""
        try:
            m = _LOCATION_RE.search(html)
            if m and m.group("v").strip():
                loc = _clean(m.group("v"))
            if not loc:
                for rx in (_META_LOC_RE, _META_LOC_REV):
                    mm = rx.search(html)
                    if mm and mm.group("v").strip():
                        loc = _clean(mm.group("v"))
                        break
        except Exception:
            loc = ""
        try:
            desc = _html_to_text(html)[:DETAIL_SLICE].strip()
        except Exception:
            desc = ""
        result = {"description": desc, "location": loc}
    except Exception as exc:
        log.debug("SEEK detail fetch failed for %s: %s", url, exc)
    return result


class SeekAdapter(BaseAdapter):
    """Keyword x location search over seek.com.au."""

    source = "seek"

    def __init__(
        self,
        keywords: Iterable[str] | str = "",
        location: Optional[str] = None,
        max_pages: int = 1,
        min_interval_seconds: float = 1.5,
        use_playwright_fallback: bool = True,
        timeout: float = 15.0,
    ):
        if isinstance(keywords, str):
            keywords = [keywords] if keywords else []
        self.keywords = list(keywords)
        self.location = location
        self.max_pages = max(1, int(max_pages or 1))
        self.limiter = RateLimiter(min_interval_seconds)
        self.use_playwright_fallback = use_playwright_fallback
        self.timeout = timeout
        self.last_debug: list[dict] = []

    def fetch(self) -> list[NormalizedJob]:
        urls = build_search_urls(self.keywords, self.location, self.max_pages)
        out: list[NormalizedJob] = []
        self.last_debug = []
        for url in urls:
            self.limiter.wait()
            try:
                html = _fetch_with_httpx(url, timeout=self.timeout)
                html_final = html
                jobs = parse_seek_html(html or "", search_url=url)
                if not jobs and self.use_playwright_fallback and html is not None:
                    # static page parsed to nothing -> try JS render
                    self.limiter.wait()
                    html2 = _fetch_with_playwright(url)
                    if html2 is not None:
                        html_final = html2
                        jobs = parse_seek_html(html2 or "", search_url=url)
                try:
                    self.last_debug.append(
                        {
                            "url": url,
                            "html_len": len(html_final or ""),
                            "parse_count": len(jobs),
                        }
                    )
                except Exception:
                    pass
                out.extend(jobs)
            except Exception as exc:  # never crash the ingest run
                log.warning("SEEK adapter error for %s: %s", url, exc)
                try:
                    self.last_debug.append(
                        {"url": url, "html_len": 0, "parse_count": 0}
                    )
                except Exception:
                    pass
                continue
        if not out:
            log.warning(
                "SEEK adapter returned 0 jobs (keywords=%s location=%s); "
                "likely offline or blocked — degrading gracefully.",
                self.keywords,
                self.location,
            )
        # dedupe across keyword pages
        seen: set[str] = set()
        uniq: list[NormalizedJob] = []
        for j in out:
            key = j.url or (j.title + j.company)
            if key in seen:
                continue
            seen.add(key)
            uniq.append(j)
        # enrich up to ENRICH_LIMIT jobs missing description (rate-limited)
        try:
            enriched = 0
            for j in uniq:
                if enriched >= ENRICH_LIMIT:
                    break
                try:
                    if (j.description or "").strip() or not j.url:
                        continue
                except Exception:
                    continue
                try:
                    self.limiter.wait()
                    detail = fetch_job_detail(j.url, timeout=self.timeout)
                except Exception as exc:
                    log.debug("SEEK enrich failed for %s: %s", j.url, exc)
                    continue
                try:
                    if detail.get("description"):
                        j.description = detail["description"]
                    if not (j.location_text or "").strip() and detail.get("location"):
                        j.location_text = detail["location"]
                    enriched += 1
                except Exception:
                    continue
        except Exception as exc:
            log.debug("SEEK enrichment pass failed: %s", exc)
        return uniq


__all__ = ["SeekAdapter", "build_search_urls", "parse_seek_html", "fetch_job_detail"]

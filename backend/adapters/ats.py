"""Adapters for applicant-tracking systems with public job-board feeds.

Each one uses the ATS's public job-board endpoint (the same data the
company's embedded job widget loads). Robots rules still apply through
:class:`~backend.scraping.http.PoliteClient`; a host that disallows us
raises and the caller falls back to plain HTML scraping.
"""
from __future__ import annotations

import html as html_lib
import re
from datetime import timedelta
from typing import Optional
from urllib.parse import quote, urlsplit

from backend.adapters.base import Adapter, Posting, SourceError
from backend.scraping.http import FetchError, PoliteClient
from backend.scraping.text import clean, html_to_text

LISTING_TTL = timedelta(minutes=30)
MAX_LISTING_PAGES = 20


def _mode(value: object) -> str:
    v = str(value or "").lower().replace("_", "-")
    if "remote" in v:
        return "remote"
    if "hybrid" in v:
        return "hybrid"
    if "on-site" in v or "onsite" in v or "office" in v:
        return "onsite"
    return "unknown"


# ---------------------------------------------------------------------------
# Workable
# ---------------------------------------------------------------------------

class WorkableAdapter(Adapter):
    def __init__(self, client: PoliteClient, slug: str, company: str = ""):
        self.client, self.slug = client, slug
        self.company = company or slug
        self.source = f"workable:{slug}"
        self.method = f"Workable job board API (apply.workable.com/{slug})"

    def list_postings(self) -> list[Posting]:
        url = f"https://apply.workable.com/api/v3/accounts/{self.slug}/jobs"
        body: dict = {"query": "", "location": [], "department": [], "worktype": [], "remote": []}
        out: list[Posting] = []
        for _ in range(MAX_LISTING_PAGES):
            data = self.client.post_json(url, body, ttl=LISTING_TTL).json()
            for job in data.get("results") or []:
                shortcode = job.get("shortcode")
                if not shortcode or job.get("state", "published") != "published":
                    continue
                locs = job.get("locations") or [job.get("location") or {}]
                texts = [", ".join(clean(l.get(k)) for k in ("city", "region", "country") if l.get(k)) for l in locs if isinstance(l, dict)]
                first = locs[0] if locs and isinstance(locs[0], dict) else {}
                out.append(Posting(
                    url=f"https://apply.workable.com/{self.slug}/j/{shortcode}/",
                    title=clean(job.get("title")),
                    company=self.company,
                    external_id=shortcode,
                    location_text="; ".join(t for t in texts if t),
                    country=clean(first.get("countryCode")).upper(),
                    work_mode="remote" if job.get("remote") else _mode(job.get("workplace")),
                    posted_at=clean(job.get("published")),
                    industry=", ".join(job.get("department") or []),
                    detail_ref=shortcode,
                ))
            token = data.get("nextPage")
            if not token:
                break
            body = {**body, "token": token}
        return out

    def fetch_detail(self, posting: Posting) -> Optional[Posting]:
        url = f"https://apply.workable.com/api/v2/accounts/{self.slug}/jobs/{posting.detail_ref}"
        data = self.client.get(url).json()
        sections = [("", data.get("description")), ("Requirements", data.get("requirements")),
                    ("Benefits", data.get("benefits"))]
        text = "\n\n".join(
            (f"{head}\n" if head else "") + html_to_text(body)
            for head, body in sections if body
        )
        posting.description = text
        posting.detail_status = "full"
        return posting


# ---------------------------------------------------------------------------
# Greenhouse (listing includes full content)
# ---------------------------------------------------------------------------

class GreenhouseAdapter(Adapter):
    def __init__(self, client: PoliteClient, token: str, company: str = ""):
        self.client, self.token = client, token
        self.company = company or token
        self.source = f"greenhouse:{token}"
        self.method = f"Greenhouse job board API ({token})"

    def list_postings(self) -> list[Posting]:
        data = self.client.get(
            f"https://boards-api.greenhouse.io/v1/boards/{self.token}/jobs?content=true", ttl=LISTING_TTL
        ).json()
        out = []
        for job in data.get("jobs") or []:
            content = html_to_text(html_lib.unescape(job.get("content") or ""))
            loc = (job.get("location") or {}).get("name", "")
            out.append(Posting(
                url=job.get("absolute_url") or "",
                title=clean(job.get("title")),
                company=clean(job.get("company_name")) or self.company,
                external_id=str(job.get("id") or ""),
                location_text=clean(loc),
                work_mode=_mode(loc) if "remote" in loc.lower() else "unknown",
                description=content,
                detail_status="full" if content else "none",
                posted_at=clean(job.get("updated_at")),
                industry=", ".join(clean(d.get("name")) for d in job.get("departments") or [] if isinstance(d, dict)),
            ))
        return [p for p in out if p.url and p.title]


# ---------------------------------------------------------------------------
# Lever (listing includes full content)
# ---------------------------------------------------------------------------

class LeverAdapter(Adapter):
    def __init__(self, client: PoliteClient, host: str, company: str = ""):
        self.client, self.host = client, host
        self.company = company or host
        self.source = f"lever:{host}"
        self.method = f"Lever postings API ({host})"

    def list_postings(self) -> list[Posting]:
        data = self.client.get(f"https://api.lever.co/v0/postings/{self.host}?mode=json", ttl=LISTING_TTL).json()
        out = []
        for job in data if isinstance(data, list) else []:
            cats = job.get("categories") or {}
            parts = [job.get("descriptionPlain") or html_to_text(job.get("description") or "")]
            for block in job.get("lists") or []:
                items = html_to_text(block.get("content") or "")
                parts.append(f"{clean(block.get('text'))}\n{items}")
            parts.append(job.get("additionalPlain") or "")
            desc = "\n\n".join(p.strip() for p in parts if p and p.strip())
            out.append(Posting(
                url=job.get("hostedUrl") or "",
                title=clean(job.get("text")),
                company=self.company,
                external_id=str(job.get("id") or ""),
                location_text=clean(cats.get("location") or ", ".join(cats.get("allLocations") or [])),
                country=clean(job.get("country")).upper(),
                work_mode=_mode(job.get("workplaceType")),
                salary_text=_lever_salary(job.get("salaryRange")),
                description=desc,
                detail_status="full" if desc else "none",
                industry=clean(cats.get("department") or cats.get("team")),
            ))
        return [p for p in out if p.url and p.title]


def _lever_salary(rng: object) -> str:
    if not isinstance(rng, dict) or rng.get("min") is None:
        return ""
    interval = str(rng.get("interval") or "").replace("-", " ")
    return f"{rng.get('currency', '')} {rng['min']:,} - {rng.get('max', rng['min']):,} {interval}".strip()


# ---------------------------------------------------------------------------
# Ashby (listing includes full content)
# ---------------------------------------------------------------------------

class AshbyAdapter(Adapter):
    def __init__(self, client: PoliteClient, board: str, company: str = ""):
        self.client, self.board = client, board
        self.company = company or board
        self.source = f"ashby:{board}"
        self.method = f"Ashby job board API ({board})"

    def list_postings(self) -> list[Posting]:
        data = self.client.get(
            f"https://api.ashbyhq.com/posting-api/job-board/{self.board}?includeCompensation=true", ttl=LISTING_TTL
        ).json()
        out = []
        for job in data.get("jobs") or []:
            if job.get("isListed") is False:
                continue
            addr = ((job.get("address") or {}).get("postalAddress") or {})
            desc = job.get("descriptionPlain") or html_to_text(job.get("descriptionHtml") or "")
            comp = job.get("compensation") or {}
            out.append(Posting(
                url=job.get("jobUrl") or "",
                title=clean(job.get("title")),
                company=self.company,
                external_id=str(job.get("id") or ""),
                location_text=clean(job.get("location")),
                country=_country_code(addr.get("addressCountry")),
                work_mode="remote" if job.get("isRemote") else _mode(job.get("workplaceType")),
                salary_text=clean(comp.get("compensationTierSummary") or comp.get("scrapeableCompensationSalarySummary")),
                description=desc,
                detail_status="full" if desc else "none",
                posted_at=clean(job.get("publishedAt")),
                industry=clean(job.get("department")),
            ))
        return [p for p in out if p.url and p.title]


def _country_code(name: object) -> str:
    n = clean(name)
    if len(n) == 2:
        return n.upper()
    return {"australia": "AU", "united states": "US", "united kingdom": "GB", "new zealand": "NZ"}.get(n.lower(), "")


# ---------------------------------------------------------------------------
# SmartRecruiters (api.smartrecruiters.com currently disallows crawlers in
# robots.txt, so this normally raises RobotsBlocked and the generic HTML
# scraper takes over).
# ---------------------------------------------------------------------------

class SmartRecruitersAdapter(Adapter):
    def __init__(self, client: PoliteClient, company_id: str, company: str = ""):
        self.client, self.company_id = client, company_id
        self.company = company or company_id
        self.source = f"smartrecruiters:{company_id}"
        self.method = f"SmartRecruiters posting API ({company_id})"

    def list_postings(self) -> list[Posting]:
        out, offset = [], 0
        for _ in range(MAX_LISTING_PAGES):
            data = self.client.get(
                f"https://api.smartrecruiters.com/v1/companies/{quote(self.company_id)}/postings?limit=100&offset={offset}",
                ttl=LISTING_TTL,
            ).json()
            content = data.get("content") or []
            for job in content:
                loc = job.get("location") or {}
                mode = "remote" if loc.get("remote") else "hybrid" if loc.get("hybrid") else "unknown"
                out.append(Posting(
                    url=f"https://jobs.smartrecruiters.com/{self.company_id}/{job.get('id')}",
                    title=clean(job.get("name")),
                    company=clean((job.get("company") or {}).get("name")) or self.company,
                    external_id=str(job.get("id")),
                    location_text=clean(loc.get("fullLocation") or ", ".join(filter(None, [loc.get("city"), loc.get("region"), loc.get("country")]))),
                    country=clean(loc.get("country")).upper(),
                    work_mode=mode,
                    posted_at=clean(job.get("releasedDate")),
                    industry=clean((job.get("function") or {}).get("label")),
                    detail_ref=job.get("id"),
                ))
            offset += len(content)
            if not content or offset >= int(data.get("totalFound") or 0):
                break
        return out

    def fetch_detail(self, posting: Posting) -> Optional[Posting]:
        data = self.client.get(
            f"https://api.smartrecruiters.com/v1/companies/{quote(self.company_id)}/postings/{posting.detail_ref}"
        ).json()
        sections = ((data.get("jobAd") or {}).get("sections")) or {}
        parts = [html_to_text(s.get("text") or "") for s in sections.values() if isinstance(s, dict)]
        posting.description = "\n\n".join(p for p in parts if p)
        posting.detail_status = "full"
        return posting


# ---------------------------------------------------------------------------
# Workday (CXS JSON endpoints behind *.myworkdayjobs.com career sites)
# ---------------------------------------------------------------------------

class WorkdayAdapter(Adapter):
    def __init__(self, client: PoliteClient, host: str, tenant: str, site: str, company: str = ""):
        self.client, self.host, self.tenant, self.site = client, host, tenant, site
        self.company = company or tenant
        self.source = f"workday:{tenant}/{site}"
        self.method = f"Workday career site API ({host}/{site})"
        self.api = f"https://{host}/wday/cxs/{tenant}/{site}"

    def list_postings(self) -> list[Posting]:
        out, offset, limit = [], 0, 20
        for _ in range(MAX_LISTING_PAGES):
            data = self.client.post_json(
                f"{self.api}/jobs", {"appliedFacets": {}, "limit": limit, "offset": offset, "searchText": ""},
                ttl=LISTING_TTL,
            ).json()
            postings = data.get("jobPostings") or []
            for job in postings:
                path = job.get("externalPath") or ""
                if not path:
                    continue
                out.append(Posting(
                    url=f"https://{self.host}/{self.site}{path}",
                    title=clean(job.get("title")),
                    company=self.company,
                    external_id=path.rsplit("_", 1)[-1],
                    location_text=clean(job.get("locationsText")),
                    work_mode=_mode(job.get("remoteType")),
                    posted_at=clean(job.get("postedOn")),
                    detail_ref=path,
                ))
            offset += len(postings)
            if not postings or offset >= int(data.get("total") or 0):
                break
        return out

    def fetch_detail(self, posting: Posting) -> Optional[Posting]:
        info = (self.client.get(f"{self.api}{posting.detail_ref}").json().get("jobPostingInfo")) or {}
        posting.description = html_to_text(info.get("jobDescription") or "")
        posting.location_text = clean(info.get("location")) or posting.location_text
        country = (info.get("country") or {}).get("alpha2Code") if isinstance(info.get("country"), dict) else ""
        posting.country = clean(country).upper() or posting.country
        if info.get("remoteType"):
            posting.work_mode = _mode(info["remoteType"])
        posting.detail_status = "full"
        return posting


# ---------------------------------------------------------------------------
# Detection: which ATS (if any) does a URL belong to?
# ---------------------------------------------------------------------------

_ATS_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("workable", re.compile(r"https?://apply\.workable\.com/(?!api/)([A-Za-z0-9_-]+)", re.I)),
    ("workable", re.compile(r"https?://([A-Za-z0-9_-]+)\.workable\.com(?:/|$)", re.I)),
    ("greenhouse", re.compile(r"https?://(?:job-)?boards(?:-api)?\.greenhouse\.io/(?:embed/job_board\?for=|v1/boards/)?([A-Za-z0-9_-]+)", re.I)),
    ("greenhouse", re.compile(r"greenhouse\.io/embed/job_board(?:/js)?\?for=([A-Za-z0-9_-]+)", re.I)),
    ("lever", re.compile(r"https?://jobs\.(?:eu\.)?lever\.co/([A-Za-z0-9_.-]+)", re.I)),
    ("ashby", re.compile(r"https?://jobs\.ashbyhq\.com/([A-Za-z0-9_.%-]+)", re.I)),
    ("smartrecruiters", re.compile(r"https?://(?:jobs|careers)\.smartrecruiters\.com/([A-Za-z0-9_-]+)", re.I)),
    ("workday", re.compile(r"https?://([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([A-Za-z0-9_-]+)", re.I)),
]
_NOT_BOARDS = {"embed", "api", "j", "jobs", "careers", "www", "static", "assets", "oneclick-ui", "apply", "resources"}


def detect_ats(text: str) -> list[tuple[str, tuple[str, ...]]]:
    """Find ATS job boards referenced in a URL or an HTML document.

    Returns ``[(kind, args)]`` in order of appearance, de-duplicated.
    """
    found: dict[tuple[str, tuple[str, ...]], int] = {}
    for kind, rx in _ATS_PATTERNS:
        for m in rx.finditer(text or ""):
            if kind == "workday":
                args = (f"{m.group(1)}.{m.group(2)}.myworkdayjobs.com", m.group(1), m.group(3))
            else:
                board = m.group(1)
                if board.lower() in _NOT_BOARDS:
                    continue
                args = (board,)
            found.setdefault((kind, args), m.start())
    return sorted(found, key=found.get)


def make_ats_adapter(client: PoliteClient, kind: str, args: tuple[str, ...], company: str = "") -> Adapter:
    if kind == "workable":
        return WorkableAdapter(client, args[0], company)
    if kind == "greenhouse":
        return GreenhouseAdapter(client, args[0], company)
    if kind == "lever":
        return LeverAdapter(client, args[0], company)
    if kind == "ashby":
        return AshbyAdapter(client, args[0], company)
    if kind == "smartrecruiters":
        return SmartRecruitersAdapter(client, args[0], company)
    if kind == "workday":
        return WorkdayAdapter(client, *args, company=company)
    raise SourceError(f"unknown ATS {kind}")


def host_of(url: str) -> str:
    return urlsplit(url).netloc.lower()


__all__ = ["detect_ats", "make_ats_adapter", "FetchError"]

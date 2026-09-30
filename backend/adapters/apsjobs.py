"""APSJobs (www.apsjobs.gov.au): the Australian Public Service jobs board.

Its search page loads results from the site's public Salesforce endpoint;
this adapter makes the same call the page makes (robots.txt allows all of
the site except password reset), with the honest User-Agent. Every result
carries the whole ad, so no detail pages are fetched.

A source is an APSJobs search URL:

    https://www.apsjobs.gov.au/s/job-search?department=Services%20Australia
    https://www.apsjobs.gov.au/s/job-search?searchString=product%20manager

``department`` (the agency name exactly as APSJobs lists it) may repeat;
other query parameters are ignored.
"""
from __future__ import annotations

import json
import re
from datetime import timedelta
from typing import Any, Optional
from urllib.parse import parse_qs, quote, urlsplit

from backend.adapters.base import Adapter, Posting, SourceError
from backend.scraping.http import FetchError, PoliteClient
from backend.scraping.text import clean, html_to_text

HOST = "https://www.apsjobs.gov.au"
SEARCH_PAGE = HOST + "/s/job-search"
ENDPOINT = HOST + "/s/sfsites/aura?r=1&aura.ApexAction.execute=1"
LISTING_TTL = timedelta(minutes=30)
MAX_PAGES = 20  # 15 results each

_STATES = r"NSW|VIC|QLD|WA|SA|TAS|ACT|NT"
_FILTER = {
    "searchString": None, "salaryFrom": None, "salaryTo": None, "closingDate": None, "positionInitiative": None,
    "classification": None, "securityClearance": None, "officeArrangement": None, "duration": None,
    "department": None, "category": None, "opportunityType": None, "employmentStatus": None, "state": None,
    "sortBy": None, "offset": 0, "offsetIsLimit": False, "lastVisitedId": None, "daysInPast": None, "name": None,
    "type": None, "notificationsEnabled": None, "savedSearchId": None,
}


def is_apsjobs_url(url: str) -> bool:
    parts = urlsplit(url or "")
    return parts.netloc.lower() in ("www.apsjobs.gov.au", "apsjobs.gov.au") and parts.path.rstrip("/") == "/s/job-search"


def search_url(department: str = "", search: str = "") -> str:
    query = "&".join(f"{k}={quote(v)}" for k, v in (("department", department), ("searchString", search)) if v)
    return f"{SEARCH_PAGE}?{query}"


def job_url(job_id: str) -> str:
    return f"{HOST}/s/job-details?Id={quote(job_id)}"


def split_locations(text: str) -> list[str]:
    """"Brisbane QLD, Sydney NSW" -> ["Brisbane QLD", "Sydney NSW"];
    "Various locations - NSW NSW" -> "NSW" (anywhere in the state)."""
    places = clean(text).split(",")  # every APSJobs place ends in its state
    out = []
    for place in places:
        m = re.fullmatch(rf"Various locations\s*-\s*({_STATES})(?:\s+\1)?", place.strip(), re.I)
        place = m.group(1).upper() if m else place.strip()
        if place and place not in out:
            out.append(place)
    return out


def work_mode(arrangement: str) -> str:
    """APSJobs office arrangement ("Hybrid", "On Site;Flexible", ...) -> work mode."""
    kinds = {k.strip().lower() for k in (arrangement or "").split(";") if k.strip()}
    if "hybrid" in kinds or "flexible" in kinds or ("work from home" in kinds and "on site" in kinds):
        return "hybrid"
    if kinds == {"work from home"}:
        return "remote"
    if kinds == {"on site"}:
        return "onsite"
    return "unknown"


def _money(value: Any) -> Optional[float]:
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return None
    return amount if amount >= 1000 else None  # SES roles report 0, some programs 1


def to_posting(job: dict) -> Optional[Posting]:
    job_id = clean(job.get("jobId"))
    if not job_id:
        return None
    lo, hi = _money(job.get("jobSalaryFrom")), _money(job.get("jobSalaryTo"))
    salary = " - ".join(f"${v:,.0f}" for v in (lo, hi) if v)
    classification = clean(job.get("jobClassification")).replace(";", ", ")
    employment = ", ".join(v for v in (clean(job.get("jobStatus")).replace(";", ", "), clean(job.get("jobType")),
                                        clean(job.get("jobEmploymentTypeDetails"))) if v)
    facts = [
        ("Agency", clean(job.get("departmentName"))),
        ("Classification", classification),
        ("Employment", employment),
        ("Location", clean(job.get("jobLocation"))),
        ("Working arrangements", ", ".join(v for v in (clean(job.get("officeArrangement")).replace(";", ", "),
                                                       clean(job.get("officeArrangementDetails"))) if v)),
        ("Salary", salary),
        ("Closes", clean(job.get("jobCloseDate"))),
    ]
    sections = [("About the agency", job.get("departmentDescription")), ("The role", job.get("jobDescription")),
                ("Duties", job.get("jobDuties")), ("Eligibility", job.get("jobEligibilityRequirements")),
                ("Notes", job.get("jobNotes"))]
    parts = ["\n".join(f"{k}: {v}" for k, v in facts if v)]
    parts += [f"{title}\n{html_to_text(body)}" for title, body in sections if clean(body)]
    return Posting(
        url=job_url(job_id),
        title=clean(job.get("jobName")),
        company=clean(job.get("departmentName")),
        external_id=clean(job.get("vacancyNumber")) or job_id,
        location_text="; ".join(split_locations(job.get("jobLocation") or "")),
        country="AU",
        work_mode=work_mode(job.get("officeArrangement") or ""),
        salary_text=salary,
        salary_min=lo,
        salary_max=hi,
        description="\n\n".join(p for p in parts if p.strip()),
        detail_status="full",
        posted_at=clean(job.get("jobPostedDate")),
        industry="Government",
    )


class _OutOfSync(Exception):
    """The site was redeployed since the page tokens were read."""


class ApsJobsAdapter(Adapter):
    has_details = False

    def __init__(self, client: PoliteClient, url: str, company: str = ""):
        query = parse_qs(urlsplit(url).query)
        self.client, self.company = client, company
        self.departments = [d.strip() for d in query.get("department", []) if d.strip()]
        self.search = (query.get("searchString") or [""])[0].strip()
        self.source = "apsjobs:" + ("|".join(self.departments) or "all") + (f"?{self.search}" if self.search else "")
        what = ", ".join(self.departments) or "all agencies"
        if self.search:
            what += f', "{self.search}"'
        self.method = f"APSJobs search ({what})"
        self.notes: list[str] = []

    def _tokens(self, fresh: bool = False) -> tuple[str, str]:
        page = self.client.get(SEARCH_PAGE, ttl=None if fresh else LISTING_TTL).text
        fwuid = re.search(r'fwuid%22%3A%22([^%]+)%22', page) or re.search(r'"fwuid"\s*:\s*"([^"]+)"', page)
        app = (re.search(r'communityApp%22%3A%22([^%]+)%22', page)
               or re.search(r'siteforce:communityApp"\s*:\s*"([^"]+)"', page))
        if not (fwuid and app):
            raise SourceError("APSJobs changed its page layout; could not find its search settings")
        return fwuid.group(1), app.group(1)

    def _call(self, tokens: tuple[str, str], offset: int) -> dict:
        filt = {**_FILTER, "offset": offset, "searchString": self.search or None, "department": self.departments or None}
        message = {"actions": [{
            "id": "1;a", "descriptor": "aura://ApexActionController/ACTION$execute", "callingDescriptor": "UNKNOWN",
            "params": {"namespace": "", "classname": "aps_jobSearchController", "method": "retrieveJobListings",
                       "params": {"filter": json.dumps(filt)}, "cacheable": False, "isContinuation": False},
        }]}
        context = {"mode": "PROD", "fwuid": tokens[0], "app": "siteforce:communityApp",
                   "loaded": {"APPLICATION@markup://siteforce:communityApp": tokens[1]},
                   "dn": [], "globals": {}, "uad": True}
        resp = self.client.post_form(ENDPOINT, {"message": json.dumps(message), "aura.context": json.dumps(context),
                                                "aura.pageURI": "/s/job-search", "aura.token": "null"}, ttl=LISTING_TTL)
        text = resp.text
        if "clientOutOfSync" in text or "invalidSession" in text:
            raise _OutOfSync()
        try:
            action = json.loads(text.removeprefix("while(1);"))["actions"][0]
        except (ValueError, KeyError, IndexError) as exc:
            raise SourceError("APSJobs returned an unexpected response") from exc
        if action.get("state") != "SUCCESS":
            errors = action.get("error") or [{}]
            raise SourceError(f"APSJobs search failed: {str(errors[0].get('message', errors))[:200]}")
        value = (action.get("returnValue") or {}).get("returnValue")
        return json.loads(value) if isinstance(value, str) else (value or {})

    def list_postings(self) -> list[Posting]:
        try:
            return self._list()
        except _OutOfSync as exc:
            raise SourceError("APSJobs was being updated during the search; try again later") from exc
        except FetchError as exc:
            raise SourceError(f"APSJobs: {exc}") from exc

    def _list(self) -> list[Posting]:
        tokens = self._tokens()
        try:
            first = self._call(tokens, 0)
        except _OutOfSync:
            tokens = self._tokens(fresh=True)
            first = self._call(tokens, 0)

        out: dict[str, Posting] = {}
        data, total = first, int(first.get("jobListingCount") or 0)
        for page in range(MAX_PAGES):
            jobs = data.get("jobListings") or []
            for job in jobs:
                p = to_posting(job)
                if p:
                    p.company = p.company or self.company
                    out.setdefault(p.url, p)
            offset = int(data.get("newOffset") or 0)
            if not jobs or offset >= total or offset <= 0:
                break
            if page == MAX_PAGES - 1:
                self.complete_listing = False  # capped: don't close unseen jobs
                self.notes.append(f"stopped after {len(out)} of {total} jobs")
                break
            data = self._call(tokens, offset)
        return list(out.values())

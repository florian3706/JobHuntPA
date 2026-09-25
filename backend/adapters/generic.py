"""Scraper for any company careers page.

Strategy, in order (the first that yields jobs wins):

1. The careers URL itself, or the careers page, points at a known ATS job
   board (Workable, Greenhouse, Lever, Ashby, SmartRecruiters, Workday):
   use that board's public feed.
2. The page embeds schema.org ``JobPosting`` JSON-LD: use it directly.
3. Plain HTML: collect links that look like individual job postings
   (``/careers/<slug>``, ``/jobs/<id>/<slug>``, ...), reading title and
   location from each link's listing card, following ``?page=N``
   pagination. Detail pages are fetched later, only for new jobs, and
   are parsed via JSON-LD or the main content block around the ``<h1>``.
4. If the static HTML has no job links and looks like a JavaScript app,
   render it in headless Chromium and repeat step 3.

Every request goes through :class:`PoliteClient` (robots.txt, crawl delay,
honest User-Agent).
"""
from __future__ import annotations

import logging
import re
from datetime import timedelta
from typing import Optional
from urllib.parse import parse_qs, urldefrag, urljoin, urlsplit

from bs4 import BeautifulSoup, Tag

from backend.adapters.ats import detect_ats, make_ats_adapter
from backend.adapters.base import Adapter, Posting, SourceError
from backend.geo import guess_country, looks_like_location
from backend.scraping.http import FetchError, PoliteClient, Response
from backend.scraping.jsonld import find_job_postings, posting_from_jsonld
from backend.scraping.text import clean, html_to_text

log = logging.getLogger(__name__)

LISTING_TTL = timedelta(hours=1)
MAX_INDEX_PAGES = 15
MAX_POSTINGS = 400
MAX_DESCRIPTION = 20000

_JOB_PATH_RE = re.compile(
    r"/(?:jobs?|careers?|positions?|vacanc(?:y|ies)|openings?|roles?|opportunit(?:y|ies)|"
    r"postings?|join-us|work-with-us)/(.+)$",
    re.I,
)
_DENY_SEGMENTS = {
    "saved-jobs", "saved", "search", "locations", "location", "teams", "team", "departments",
    "department", "benefits", "faq", "faqs", "culture", "life", "blog", "news", "events",
    "students", "student", "early-careers", "graduates", "graduate", "internships", "internship",
    "careers-and-growth", "career-tools", "apply", "login", "sign-in", "signin", "alerts",
    "job-alerts", "privacy", "about", "why", "category", "categories", "tag", "tags", "page",
    "feed", "rss", "talent-community", "hiring-process", "how-we-hire", "diversity", "perks",
    "values", "all", "index", "home", "en", "open-positions", "openings", "vacancies", "jobs",
    "all-jobs", "all-positions", "job-search", "search-jobs", "listings",
}
_GENERIC_LINK_TEXT = re.compile(r"^(learn more|read more|view( job| role| details)?|apply( now)?|see details|more|details|find out more)$", re.I)
_JOBISH_RE = re.compile(
    r"\b(responsibilit|requirement|qualification|experience|about the role|about you|"
    r"what you.?ll|you will|skills|key duties|the role)\w*", re.I)
_NOISE_SELECTORS = [
    "script", "style", "noscript", "svg", "iframe", "template", "nav", "header", "footer",
    "aside", "form", "[role=navigation]", "[role=banner]", "[role=contentinfo]",
    "[class*=cookie]", "[id*=cookie]", "[class*=breadcrumb]", "[class*=share]",
    "[class*=related]", "[class*=similar]", "[class*=newsletter]", "[class*=modal]",
]


def _site_key(host: str) -> str:
    return host.lower().removeprefix("www.")


def _path_segments(path: str) -> list[str]:
    return [s for s in path.split("/") if s]


def is_job_link(url: str, index_url: str) -> bool:
    u, idx = urlsplit(url), urlsplit(index_url)
    if u.scheme not in ("http", "https"):
        return False
    if _site_key(u.netloc) != _site_key(idx.netloc):
        return False
    if u.path.rstrip("/") == idx.path.rstrip("/"):
        return False
    m = _JOB_PATH_RE.search(u.path.rstrip("/"))
    if not m:
        return False
    rest = _path_segments(m.group(1))
    if not rest or any(seg.lower() in _DENY_SEGMENTS for seg in rest):
        return False
    last = rest[-1]
    # A posting slug/id: "senior-pm", "12345", "12345-senior-pm"
    return bool(re.search(r"\d", last) or "-" in last or "_" in last)


def _shape(url: str) -> str:
    """Path pattern with slugs/ids and locale prefixes wildcarded:
    /us/company/acme/jobs/pm-1 -> /*/company/*/jobs/*"""
    segs = _path_segments(urlsplit(url).path)
    if segs and re.fullmatch(r"[a-z]{2}(-[a-z]{2})?", segs[0], re.I):
        segs = segs[1:]  # locale prefix: /us/..., /en-au/...
    out = []
    for i, seg in enumerate(segs):
        prev = segs[i - 1].lower() if i else ""
        keep = seg.lower() in _SHAPE_WORDS and not _JOB_PATH_RE.search("/" + prev + "/x")
        out.append(seg.lower() if keep else "*")
    return "/" + "/".join(out)


_SHAPE_WORDS = {"company", "companies", "job", "jobs", "career", "careers", "position", "positions",
                "vacancy", "vacancies", "opening", "openings", "role", "roles", "posting", "postings"}


def _dominant_shape(anchors: list[tuple[Tag, str]]) -> list[tuple[Tag, str]]:
    """Job boards mix postings with category/filter links (/jobs/product-owner/).
    Keep link shapes that account for a real share of the distinct URLs."""
    by_shape: dict[str, set[str]] = {}
    for _, url in anchors:
        by_shape.setdefault(_shape(url), set()).add(url)
    if len(by_shape) <= 1:
        return anchors
    top = max(len(v) for v in by_shape.values())
    keep = {k for k, v in by_shape.items() if len(v) >= max(2, top / 3)}
    # Aggregators: postings live under the employer (/company/<x>/jobs/<y>);
    # sibling /jobs/<y> links are category filters.
    scoped = {k for k in keep if "/company/" in k or "/companies/" in k}
    if scoped:
        keep = scoped
    return [(a, u) for a, u in anchors if _shape(u) in keep]


def _is_pagination_link(url: str, index_url: str) -> bool:
    u, idx = urlsplit(url), urlsplit(index_url)
    if _site_key(u.netloc) != _site_key(idx.netloc):
        return False
    same_path = u.path.rstrip("/") == idx.path.rstrip("/")
    q = parse_qs(u.query)
    if same_path and any(k in q for k in ("page", "pg", "p", "offset", "start")):
        first = [v for k in ("page", "pg", "p") for v in q.get(k, [])]
        return first not in (["1"], ["0"])  # page 1 is the index itself
    return bool(re.fullmatch(re.escape(idx.path.rstrip("/")) + r"/page/\d+/?", u.path))


def _card_for(anchor: Tag, candidate_urls: set[str], base: str) -> Tag:
    """Largest ancestor of ``anchor`` that contains no other job link."""
    el = anchor
    for _ in range(6):
        parent = el.parent
        if parent is None or parent.name in ("body", "html", "main"):
            break
        urls = {
            urldefrag(urljoin(base, a.get("href", "")))[0]
            for a in parent.find_all("a", href=True)
        } & candidate_urls
        if len(urls) > 1:
            break
        el = parent
    return el


def _title_from_url(url: str) -> str:
    slug = _path_segments(urlsplit(url).path)[-1]
    slug = re.sub(r"^\d+[-_]?", "", slug)
    return clean(slug.replace("-", " ").replace("_", " ")).title()


def parse_listing(html: str, page_url: str, company: str) -> tuple[list[Posting], list[str]]:
    """Job-posting stubs + pagination URLs from a careers index page."""
    postings: list[Posting] = []
    ld = [posting_from_jsonld(n, page_url) for n in find_job_postings(html)]
    for p in ld:
        if p and p.url:
            p.company = p.company or company
            postings.append(p)

    soup = BeautifulSoup(html, "lxml")
    anchors = []
    for a in soup.find_all("a", href=True):
        url = urldefrag(urljoin(page_url, a["href"].strip()))[0]
        if is_job_link(url, page_url):
            anchors.append((a, url))
    anchors = _dominant_shape(anchors)
    candidate_urls = {u for _, u in anchors}
    seen = {p.url for p in postings}
    for a, url in anchors:
        if url in seen:
            continue
        seen.add(url)
        card = _card_for(a, candidate_urls, page_url)
        # Cards often put department/location inside the link: first line is the title.
        link_lines = [clean(t) for t in a.get_text("\n").split("\n") if clean(t)]
        title = link_lines[0] if link_lines else ""
        if not title or _GENERIC_LINK_TEXT.match(title) or len(title) > 150:
            heading = card.find(["h1", "h2", "h3", "h4"])
            title = clean(heading.get_text(" ")) if heading else ""
        title = title or clean(a.get("title")) or _title_from_url(url)
        lines = [clean(t) for t in card.get_text("\n").split("\n")]
        # Job boards name the employer in the card.
        employer = next((
            text for el in card.select('[class*=company], [data-id*=company], a[href*="/company/"], a[href*="/companies/"]')
            if el is not a and a not in el.parents and not el.find("a", href=a["href"])
            and (text := clean(el.get_text(" "))) and text != title and len(text) <= 80
        ), company)
        location = next((l for l in lines if l and l not in (title, employer) and looks_like_location(l)), "")
        postings.append(Posting(
            url=url,
            title=title,
            company=employer,
            location_text=location,
            country=guess_country(location),
        ))

    pages = []
    for a in soup.find_all("a", href=True):
        url = urldefrag(urljoin(page_url, a["href"].strip()))[0]
        if _is_pagination_link(url, page_url) and url != page_url:
            pages.append(url)
    return postings, list(dict.fromkeys(pages))


def parse_detail(html: str, stub: Posting) -> Optional[Posting]:
    """Fill ``stub`` from a job detail page; None if it is not a job posting."""
    for node in find_job_postings(html):
        p = posting_from_jsonld(node, stub.url)
        if p and len(p.description) > 200:
            stub.title = p.title or stub.title
            stub.description = p.description[:MAX_DESCRIPTION]
            stub.location_text = p.location_text or stub.location_text
            stub.country = p.country or stub.country or guess_country(stub.location_text)
            stub.salary_text = p.salary_text or stub.salary_text
            stub.salary_min, stub.salary_max = p.salary_min or stub.salary_min, p.salary_max or stub.salary_max
            stub.work_mode = p.work_mode if p.work_mode != "unknown" else stub.work_mode
            stub.posted_at = p.posted_at or stub.posted_at
            # Job boards/aggregators: the posting names the real employer.
            stub.company = p.company or stub.company
            stub.detail_status = "full"
            return stub

    soup = BeautifulSoup(html, "lxml")
    h1 = soup.find("h1")
    h1_text = clean(h1.get_text(" ")) if h1 else ""
    info_bits = [
        clean(el.get_text(" "))
        for el in soup.select("[class*=location], [class*=job-info], [class*=job-meta], [class*=jobmeta], [class*=job-details] li")
        if len(clean(el.get_text(" "))) <= 80
    ]
    info_bits.sort(key=len)  # most specific element first
    for sel in _NOISE_SELECTORS:
        for el in soup.select(sel):
            el.decompose()
    h1 = soup.find("h1")
    container: Optional[Tag] = None
    if h1 is not None:
        node = h1
        while node.parent is not None and node.parent.name not in ("body", "html"):
            node = node.parent
            if len(node.get_text(" ", strip=True)) >= 800:
                break
        container = node
    container = container or soup.select_one("main, article, [role=main]") or soup.body
    if container is None:
        return None
    text = html_to_text(str(container))[:MAX_DESCRIPTION]
    if len(text) < 300 or not _JOBISH_RE.search(text):
        return None

    stub.title = h1_text or stub.title
    if not stub.location_text:
        m = re.search(r"\blocation\s*[:\-]\s*([^\n]{2,80})", text, re.I)
        candidates = ([m.group(1)] if m else []) + info_bits
        stub.location_text = next((clean(c) for c in candidates if looks_like_location(c)), "")
    stub.country = stub.country or guess_country(stub.location_text)
    stub.description = text
    stub.detail_status = "full"
    return stub


class GenericAdapter(Adapter):
    def __init__(self, client: PoliteClient, careers_url: str, company: str = ""):
        self.client = client
        self.careers_url = careers_url.strip()
        host = urlsplit(self.careers_url).netloc.lower()
        self.company = company or _site_key(host)
        self.source = f"site:{_site_key(host)}"
        self.method = f"HTML careers page ({_site_key(host)})"
        self.delegate: Optional[Adapter] = None
        self.render_details = False
        self.notes: list[str] = []

    # -- listing -------------------------------------------------------------
    def list_postings(self) -> list[Posting]:
        # 1a. the URL itself is an ATS job board
        direct = detect_ats(self.careers_url)
        if direct:
            got = self._try_ats(direct, found_on="")
            if got is not None:
                return got

        try:
            index = self.client.get(self.careers_url, ttl=LISTING_TTL)
        except FetchError as exc:
            raise SourceError(str(exc)) from exc

        # 1b. the careers page links to / embeds ONE ATS job board and lists
        # no jobs itself. Job boards/aggregators link to many companies'
        # boards; those must be scraped as HTML, not hijacked by one link.
        refs = [r for r in detect_ats(index.text) if r not in direct]
        own_links, _ = parse_listing(index.text, index.url, self.company)
        if len(refs) == 1 and len(own_links) < 3:
            got = self._try_ats(refs, found_on=" (linked from careers page)")
            if got is not None:
                return got

        # 2 + 3. JSON-LD / HTML job links, following pagination
        postings = self._crawl_listing(index)
        if not postings:
            # 4. JavaScript-rendered listing
            rendered = self.client.render(self.careers_url)
            refs = detect_ats(rendered.text)
            if len(refs) == 1:
                got = self._try_ats(refs, found_on=" (found in rendered page)")
                if got is not None:
                    return got
            postings = self._crawl_listing(rendered, render=True)
            self.render_details = True
            self.method += ", rendered with headless browser"
        if not postings:
            detail = "; ".join(self.notes)
            raise SourceError("no job postings found on the careers page" + (f" ({detail})" if detail else ""))
        return postings

    def _try_ats(self, refs, found_on: str) -> Optional[list[Posting]]:
        for kind, args in refs:
            adapter = make_ats_adapter(self.client, kind, args, company=self.company)
            try:
                postings = adapter.list_postings()
            except (FetchError, SourceError, ValueError) as exc:
                self.notes.append(f"{adapter.method}: {exc}")
                continue
            self.delegate = adapter
            self.complete_listing = adapter.complete_listing
            self.source = adapter.source
            self.method = adapter.method + found_on
            return postings
        return None

    def _crawl_listing(self, first: Response, render: bool = False) -> list[Posting]:
        postings: dict[str, Posting] = {}
        queue, visited = [first], {first.url}
        pages = 0
        while queue and pages < MAX_INDEX_PAGES and len(postings) < MAX_POSTINGS:
            page = queue.pop(0)
            pages += 1
            found, next_pages = parse_listing(page.text, page.url, self.company)
            for p in found:
                postings.setdefault(p.url, p)
            for url in next_pages:
                if url in visited:
                    continue
                visited.add(url)
                try:
                    queue.append(self.client.render(url) if render else self.client.get(url, ttl=LISTING_TTL))
                except FetchError as exc:
                    self.notes.append(f"pagination stopped at {url}: {exc}")
                    break
        if queue or len(postings) >= MAX_POSTINGS or any(n.startswith("pagination stopped") for n in self.notes):
            self.complete_listing = False  # don't mark unseen jobs closed
        if pages > 1:
            self.method += f", {pages} listing pages"
        return list(postings.values())[:MAX_POSTINGS]

    # -- detail --------------------------------------------------------------
    def fetch_detail(self, posting: Posting) -> Optional[Posting]:
        if self.delegate is not None:
            return self.delegate.fetch_detail(posting)
        if posting.detail_status == "full":
            return posting
        resp = self.client.render(posting.url) if self.render_details else self.client.get(posting.url)
        return parse_detail(resp.text, posting)

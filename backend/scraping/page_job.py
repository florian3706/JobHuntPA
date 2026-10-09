"""A job read from a page the user has open in their own browser (the browser extension, backend/extension.py).

The extension sends what the browser shows: the address, the tab's title, the page's HTML, its visible text and
any text the user selected. Nothing is fetched here (the server never asks the site for anything, so the scraping
rules in backend/scraping/http.py don't come into it), and no AI is asked. In order:

1. a SEEK job page (``window.SEEK_REDUX_DATA``, ``backend.adapters.seek.parse_job_page``);
2. schema.org ``JobPosting`` JSON-LD: the posting whose ``url`` is this page, else the first one
   (``backend.scraping.jsonld``);
3. heuristics that work on most job pages: the title from ``og:title``, the first ``<h1>`` or the tab's title (a
   " | Site name" ending taken off), the employer from JSON-LD ``hiringOrganization``, ``og:site_name`` or the
   host name, and the description from the page's main content (a job-description container, ``<article>``,
   ``<main>``), else the visible text.

Text the user selected always becomes the description. What the user typed into the extension's form wins over all
of it (the caller applies that: ``apply_fields``).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from backend.adapters.base import Posting
from backend.adapters.seek import parse_job_page
from backend.employment import normalise_employment_type
from backend.scraping.jsonld import find_job_postings, posting_from_jsonld
from backend.scraping.text import clean, html_to_text, normalize_block

# Longest values accepted (characters). The extension cuts the page down to these before it sends it.
LIMITS = {"url": 2048, "page_title": 1000, "html": 3_000_000, "text": 300_000, "selection": 100_000}
FIELD_LIMITS = {"title": 500, "company": 500, "location_text": 500, "salary_text": 500, "employment_type": 40,
                "work_mode": 40}
WORK_MODES = ("onsite", "hybrid", "remote", "unknown")
MAX_DESCRIPTION = 60_000   # a job ad is a few thousand characters; the rest of a page is noise
FULL_AD_CHARS = 200        # a description at least this long counts as the full ad (like the JSON-LD importer)
SHORT_VALUE = 120          # a location or salary read from the page is a short line, never a paragraph

_SEP = re.compile(r"(\s+(?:\||–|—|-|·|•|::|»|/)\s+)")
_PREFIX = re.compile(r"^(?:job application for|apply (?:now )?for|job:|vacancy:|careers?:)\s+", re.I)
_SITE_WORDS = re.compile(r"\b(?:careers?|jobs?|job board|hiring|recruitment|vacancies|work with us|join us)\b", re.I)
_HOST_NOISE = {"www", "com", "co", "net", "org", "au", "uk", "nz", "io", "gov", "edu", "jobs", "careers", "career",
               "apply", "boards", "job", "app", "en", "de", "us", "ca", "my", "hr"}
_DESCRIPTION = re.compile(r"(?:job|position|vacancy|posting|role|ad)[-_ ]?(?:description|details?|content|body|desc|text)"
                          r"|^description$|jobdescription|job-?ad\b|posting-?description", re.I)
_LOCATION = re.compile(r"(?<!re)location|job-?place|workplace", re.I)
_SALARY = re.compile(r"salary|compensation|remuneration|pay-?range", re.I)
_EMPLOYMENT = re.compile(r"employment[-_]?type|job[-_]?type|work[-_]?type|contract[-_]?type", re.I)
_JUNK = ("script", "style", "noscript", "svg", "template", "nav", "header", "footer", "aside", "form", "button",
         "iframe", "select")


@dataclass
class PageJob:
    """What a page says about its job: how it was read (``seek``, ``jsonld`` or ``page``), the posting, where its
    description came from (``selection``, ``structured``, ``page`` or "" for none) and notes for the user."""
    method: str
    posting: Posting
    description_from: str
    notes: list[str] = field(default_factory=list)


def _norm_url(url: str) -> str:
    """An address for comparing: no fragment, no "www.", no trailing slash, the host in lower case."""
    try:
        parts = urlsplit(url or "")
    except ValueError:
        return ""
    host = parts.netloc.lower().removeprefix("www.")
    path = parts.path.rstrip("/")
    return f"{host}{path}" + (f"?{parts.query}" if parts.query else "")


def _same_host(a: str, b: str) -> bool:
    try:
        return urlsplit(a).netloc.lower().removeprefix("www.") == urlsplit(b).netloc.lower().removeprefix("www.")
    except ValueError:
        return False


def _posting_url(own: str, page_url: str) -> str:
    """The address to store: the structured data's own, when it is a full http(s) address on the same site (often
    the page's canonical address, without tracking parameters), else the page's."""
    if own:
        try:
            joined = urljoin(page_url, own.strip())
            parts = urlsplit(joined)
        except ValueError:
            return page_url
        if parts.scheme in ("http", "https") and parts.netloc and _same_host(joined, page_url):
            return joined
    return page_url


def _matching_node(nodes: list[dict], url: str) -> Optional[dict]:
    """The JobPosting about this page (its ``url`` is the page's, with or without the query), else the first."""
    if not nodes:
        return None
    want = _norm_url(url)
    want_path = want.split("?", 1)[0]
    for node in nodes:
        own = node.get("url")
        own = own[0] if isinstance(own, list) and own else own
        if isinstance(own, str) and own.strip():
            got = _norm_url(urljoin(url, own.strip()))
            if got == want or got.split("?", 1)[0] == want_path:
                return node
    return nodes[0]


def _host_names(url: str) -> set[str]:
    """The words of a host name that name the site ("example" for careers.example.com.au)."""
    try:
        host = urlsplit(url).netloc.lower().split(":")[0]
    except ValueError:
        return set()
    return {label for label in host.split(".") if len(label) >= 3 and label not in _HOST_NOISE}


def _simple(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", (text or "").lower()))


def _is_site_name(part: str, names: set[str], generic: bool = True) -> bool:
    """Whether a piece of a page title names the site rather than the job ("Example Careers", "Jobs at Example").
    ``generic``: also a short piece with words such as "Careers" or "Jobs" in it (for a piece after a separator)."""
    simple = _simple(part)
    if not simple:
        return True
    words = simple.split()
    for name in names:
        name = _simple(name)
        if not name:
            continue
        if simple == name or (name in simple and len(words) <= len(name.split()) + 3):
            return True
        if name.replace(" ", "") in simple.replace(" ", "") and len(words) <= 4:
            return True
    return generic and len(words) <= 4 and bool(_SITE_WORDS.search(part))


def clean_title(raw: str, names: set[str], company: str = "") -> str:
    """A page or og:title without the site's name: "Senior Analyst | Example" -> "Senior Analyst". Pieces that name
    the site are taken off the end; of what is left, the part before a " | " is the job."""
    title = _PREFIX.sub("", clean(raw))
    if not title:
        return ""
    names = {n for n in names | ({company} if company else set()) if n}
    for name in sorted(names, key=len, reverse=True):  # "Data Lead at Example"
        title = re.sub(r"\s+at\s+" + re.escape(name) + r"\s*$", "", title, flags=re.I)
    tokens = _SEP.split(title)  # piece, separator, piece, ...
    pieces, seps = tokens[0::2], tokens[1::2]
    while len(pieces) > 1 and _is_site_name(pieces[-1], names):
        pieces.pop()
        seps.pop()
    while len(pieces) > 1 and _is_site_name(pieces[0], names, generic=False):  # "Example - Senior Analyst"
        pieces.pop(0)
        seps.pop(0)
    if _is_site_name(pieces[0], names, generic=len(tokens) > 1):
        return ""  # the whole title is the site's name
    if "|" in [s.strip() for s in seps]:
        cut = [s.strip() for s in seps].index("|")
        pieces, seps = pieces[:cut + 1], seps[:cut]
    return (pieces[0] + "".join(sep + piece for sep, piece in zip(seps, pieces[1:]))).strip()


def _soup(html: str) -> Optional[BeautifulSoup]:
    if not html or "<" not in html:
        return None
    return BeautifulSoup(html, "lxml")


def _meta(soup: Optional[BeautifulSoup], *names: str) -> str:
    if soup is None:
        return ""
    for name in names:
        tag = soup.find("meta", attrs={"property": name}) or soup.find("meta", attrs={"name": name})
        if tag is not None and clean(tag.get("content")):
            return clean(tag.get("content"))
    return ""


def _marked(tag, pattern: re.Pattern) -> bool:
    """Whether an element's id, any of its classes or its itemprop matches ``pattern``."""
    if not getattr(tag, "attrs", None):
        return False
    values = [tag.get("id") or "", tag.get("itemprop") or "", tag.get("data-testid") or "",
              tag.get("data-automation") or ""]
    classes = tag.get("class") or []
    values += classes if isinstance(classes, list) else [classes]
    return any(isinstance(v, str) and v and pattern.search(v) for v in values)


def _short_text(soup: Optional[BeautifulSoup], pattern: re.Pattern, *, digits: bool = False) -> str:
    """The text of the first small element marked with ``pattern`` (a location or salary line)."""
    if soup is None:
        return ""
    for tag in soup.find_all(lambda t: _marked(t, pattern)):
        if tag.name in ("script", "style", "meta", "link", "input", "select", "option", "button", "body", "html"):
            continue
        text = clean(tag.get_text(" "))
        if 2 <= len(text) <= SHORT_VALUE and (not digits or re.search(r"\d", text)):
            return text
    return ""


def _main_text(soup: Optional[BeautifulSoup]) -> str:
    """The text of the page's main content: a job-description container, else ``<article>``, ``<main>`` or
    ``role="main"``; the first with a paragraph's worth of text, else the longest."""
    if soup is None:
        return ""
    body = soup.body or soup
    candidates = [t for t in body.find_all(lambda t: _marked(t, _DESCRIPTION)) if t.name not in ("meta", "script")]
    candidates += body.find_all("article") + body.find_all("main") + body.find_all(attrs={"role": "main"})
    best = ""
    for tag in candidates:
        text = _content_text(tag)
        if len(text) >= FULL_AD_CHARS:
            return text
        if len(text) > len(best):
            best = text
    return best


def _content_text(tag) -> str:
    """An element's readable text, without navigation, forms, scripts and the like."""
    copy = BeautifulSoup(str(tag), "lxml")
    for junk in copy.find_all(_JUNK):
        junk.decompose()
    return html_to_text(str(copy))


def _org_name(nodes: list[dict]) -> str:
    for node in nodes:
        org = node.get("hiringOrganization")
        name = org.get("name") if isinstance(org, dict) else org if isinstance(org, str) else ""
        if clean(name):
            return clean(name)
    return ""


def _heuristic_posting(soup: Optional[BeautifulSoup], nodes: list[dict], url: str, page_title: str) -> Posting:
    from backend.sources import default_label

    site = _meta(soup, "og:site_name", "application-name")
    company = _org_name(nodes) or site or default_label(url)
    names = _host_names(url) | ({site} if site else set())
    title = ""
    h1 = soup.find("h1") if soup is not None else None
    tag_title = clean(soup.title.get_text()) if soup is not None and soup.title is not None else ""
    for raw in (_meta(soup, "og:title", "twitter:title"), clean(h1.get_text(" ")) if h1 is not None else "",
                page_title, tag_title):
        title = clean_title(raw, names, company)[:300]
        if title:
            break
    location = _short_text(soup, _LOCATION)
    return Posting(url=url, title=title, company=company, location_text=location,
                   salary_text=_short_text(soup, _SALARY, digits=True),
                   employment_type=normalise_employment_type(_short_text(soup, _EMPLOYMENT)))


def read_page(url: str, page_title: str = "", html: str = "", text: str = "", selection: str = "") -> PageJob:
    """The job on a page (see the module docstring). The posting's title may be empty: the caller asks for one."""
    notes: list[str] = []
    posting: Optional[Posting] = None
    method = "page"
    if html and "SEEK_REDUX_DATA" in html:
        seek = parse_job_page(html)
        if seek is not None and seek.title:
            posting, method = seek, "seek"
    nodes = find_job_postings(html) if html else []
    if posting is None and nodes:
        node = _matching_node(nodes, url)
        found = posting_from_jsonld(node, url) if node is not None else None
        if found is not None:
            found.url = _posting_url(found.url if found.url != url else "", url)
            posting, method = found, "jsonld"
    soup = None
    if posting is None:
        soup = _soup(html)
        posting = _heuristic_posting(soup, nodes, url, page_title)
    description_from = "structured" if posting.description else ""
    if not posting.description and not selection.strip():
        if soup is None:
            soup = _soup(html)
        main = _main_text(soup)
        if len(main) >= FULL_AD_CHARS or not text.strip():
            posting.description = main
        else:
            posting.description = normalize_block(text)
            if posting.description:
                notes.append("The whole page's text is used as the description. To use only the ad, select its "
                             "text on the page first.")
        description_from = "page" if posting.description else ""
    if selection.strip():
        posting.description = normalize_block(selection)
        description_from = "selection"
        notes.append("Selected text will be used as the description.")
    if len(posting.description) > MAX_DESCRIPTION:
        posting.description = posting.description[:MAX_DESCRIPTION].rstrip()
        notes.append("The description was cut to its first 60,000 characters.")
    set_detail_status(posting)
    if method == "seek":
        notes.append("Read as a SEEK job ad.")
    elif method == "jsonld":
        notes.append("Read from the job data the page includes for search engines.")
    if not posting.title:
        notes.append("No job title was found. Type one in.")
    return PageJob(method=method, posting=posting, description_from=description_from, notes=notes)


def set_detail_status(posting: Posting) -> None:
    """``full`` for a description long enough to be the ad, ``summary`` for a shorter one, ``none`` without."""
    length = len(posting.description or "")
    posting.detail_status = "full" if length >= FULL_AD_CHARS else "summary" if length else "none"


def apply_fields(posting: Posting, fields: dict) -> None:
    """The user's own values (the extension's form) over what the page said; empty ones change nothing. The caller
    has checked them (lengths, ``work_mode``)."""
    for name in ("title", "company", "location_text", "salary_text"):
        value = clean(fields.get(name))
        if value:
            setattr(posting, name, value)
    employment = clean(fields.get("employment_type"))
    if employment:
        posting.employment_type = normalise_employment_type(employment)
    mode = clean(fields.get("work_mode")).lower()
    if mode in WORK_MODES and mode != "unknown":
        posting.work_mode = mode

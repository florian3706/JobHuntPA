"""A job read from a page the user has open in their own browser (the browser extension, backend/extension.py).

The extension sends what the browser shows: the address, the tab's title, the page's HTML, its visible text and
any text the user selected. Nothing is fetched here (the server never asks the site for anything, so the scraping
rules in backend/scraping/http.py don't come into it), and no AI is asked. In order:

1. a SEEK job page (``window.SEEK_REDUX_DATA``, ``backend.adapters.seek.parse_job_page``), only on SEEK's own sites
   (seek.com.au, seek.co.nz) and only with a numeric job id: the job is stored under SEEK's address for that id;
2. schema.org ``JobPosting`` JSON-LD: the posting whose ``url`` is this page, else the first one
   (``backend.scraping.jsonld``);
3. heuristics that work on most job pages: the title from ``og:title``, the first ``<h1>`` or the tab's title (a
   " | Site name" ending taken off), the employer from JSON-LD ``hiringOrganization``, ``og:site_name`` or the
   host name, and the description from the page's main content (a job-description container, ``<article>``,
   ``<main>``), else the visible text.

Text the user selected always becomes the description. What the user typed into the extension's form wins over all
of it (the caller applies that: ``apply_fields``).

Any page the user opens can send hostile HTML, so every step here takes time in proportion to the page (one pass, a
bounded number of candidates, titles cut short before they are cleaned), never in proportion to its square; the
caller also limits how many pages are read at once (backend/extension.py).
"""
from __future__ import annotations

import itertools
import re
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urljoin, urlsplit

from lxml import etree

from backend.adapters.base import Posting
from backend.adapters.seek import parse_job_page
from backend.employment import normalise_employment_type
from backend.scraping.jsonld import find_job_postings, posting_from_jsonld
from backend.scraping.text import BLOCK_TAGS, clean, normalize_block

# Longest values accepted (characters). The extension cuts the page down to these before it sends it.
LIMITS = {"url": 2048, "page_title": 1000, "html": 3_000_000, "text": 300_000, "selection": 100_000}
FIELD_LIMITS = {"title": 500, "company": 500, "location_text": 500, "salary_text": 500, "employment_type": 40,
                "work_mode": 40}
WORK_MODES = ("onsite", "hybrid", "remote", "unknown")
MAX_DESCRIPTION = 60_000   # a job ad is a few thousand characters; the rest of a page is noise
FULL_AD_CHARS = 200        # a description at least this long counts as the full ad (like the JSON-LD importer)
SHORT_VALUE = 120          # a location or salary read from the page is a short line, never a paragraph
TITLE_INPUT = 1000         # a title or site name is cut to this many characters before it is cleaned
MAX_CANDIDATES = 50        # containers tried for the main content (none inside one already tried)
SHORT_CANDIDATES = 50      # elements tried for a location, salary or employment type line
SHORT_WALK = 400           # elements and strings looked at in each of those before it counts as too long
SEEK_HOSTS = ("seek.com.au", "seek.co.nz")

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


def _site(url: str) -> Optional[tuple[str, Optional[int]]]:
    """The host (without "www.") and port of an address with no user name or password in it, else None."""
    try:
        parts = urlsplit(url)
        host, port = parts.hostname, parts.port
    except ValueError:
        return None
    if not host or "@" in parts.netloc:
        return None
    return host.removeprefix("www."), port


def _same_host(a: str, b: str) -> bool:
    """Whether two addresses are on the same site: the same host (with or without "www.") and port."""
    site = _site(a)
    return site is not None and site == _site(b)


def _on_seek(url: str) -> bool:
    """Whether the page is on one of SEEK's own sites (or a subdomain of one)."""
    site = _site(url)
    return site is not None and any(site[0] == d or site[0].endswith("." + d) for d in SEEK_HOSTS)


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


def _is_site_name(part: str, names: list[tuple[str, str, int]], generic: bool = True) -> bool:
    """Whether a piece of a page title names the site rather than the job ("Example Careers", "Jobs at Example").
    ``names``: the site's names, simplified once (``_simple``, without spaces, number of words). ``generic``: also a
    short piece with words such as "Careers" or "Jobs" in it (for a piece after a separator)."""
    simple = _simple(part)
    if not simple:
        return True
    words = simple.split()
    joined = simple.replace(" ", "")
    for name, name_joined, name_words in names:
        if simple == name or (name in simple and len(words) <= name_words + 3):
            return True
        if name_joined in joined and len(words) <= 4:
            return True
    return generic and len(words) <= 4 and bool(_SITE_WORDS.search(part))


def clean_title(raw: str, names: set[str], company: str = "") -> str:
    """A page or og:title without the site's name: "Senior Analyst | Example" -> "Senior Analyst". Pieces that name
    the site are taken off the end; of what is left, the part before a " | " is the job. The title and the names are
    cut to ``TITLE_INPUT`` characters first (a title is never longer; a hostile page's would take long to clean)."""
    title = _PREFIX.sub("", clean(raw)[:TITLE_INPUT])
    if not title:
        return ""
    names = {n[:TITLE_INPUT] for n in names | ({company} if company else set()) if n}
    for name in sorted(names, key=len, reverse=True):  # "Data Lead at Example"
        title = re.sub(r"\s+at\s+" + re.escape(name) + r"\s*$", "", title, flags=re.I)
    simple_names = [(name, name.replace(" ", ""), len(name.split())) for name in map(_simple, names) if name]
    tokens = _SEP.split(title)  # piece, separator, piece, ...
    pieces, seps = tokens[0::2], tokens[1::2]
    while len(pieces) > 1 and _is_site_name(pieces[-1], simple_names):
        pieces.pop()
        seps.pop()
    while len(pieces) > 1 and _is_site_name(pieces[0], simple_names, generic=False):  # "Example - Senior Analyst"
        pieces.pop(0)
        seps.pop(0)
    if _is_site_name(pieces[0], simple_names, generic=len(tokens) > 1):
        return ""  # the whole title is the site's name
    if "|" in [s.strip() for s in seps]:
        cut = [s.strip() for s in seps].index("|")
        pieces, seps = pieces[:cut + 1], seps[:cut]
    return (pieces[0] + "".join(sep + piece for sep, piece in zip(seps, pieces[1:]))).strip()


_CONTAINERS = ("script", "style", "template", "rt", "rp")  # BeautifulSoup's string containers


class TooLong(Exception):
    """More of an element than was asked for."""


class _Page:
    """A page's HTML, parsed once with lxml (a BeautifulSoup tree of a 3 MB page took seconds to build and a quarter
    of a gigabyte). An element's text reads as BeautifulSoup's ``get_text()`` read it: a string inside a
    ``<script>``, ``<style>``, ``<template>``, ``<rt>`` or ``<rp>`` is of that kind (the innermost one's), and is
    part of the text of an element of that kind only (and other strings only of other elements)."""

    def __init__(self, root):
        self.root = root
        self.owner: dict = {}  # element -> the kind of its own strings (its innermost container's tag), if any
        for outer in root.iter(*_CONTAINERS):
            if outer in self.owner:
                continue  # inside another, already done
            stack = [(outer, outer.tag)]
            while stack:
                el, owner = stack.pop()
                self.owner[el] = owner
                for child in el:
                    stack.append((child, child.tag if child.tag in _CONTAINERS else owner))

    def first(self, tag: str):
        return next(self.root.iter(tag), None)

    def elements(self, scope=None):
        """Every element of the page in order (of ``scope``'s descendants when given), no comments."""
        for el in (scope.iterdescendants() if scope is not None else self.root.iter()):
            if isinstance(el.tag, str):
                yield el

    def strings(self, el, limit: Optional[int] = None):
        """The text of ``el`` piece by piece, in order. With ``limit``: after that many elements and strings,
        ``TooLong`` (instead of the rest)."""
        mine = el.tag if el.tag in _CONTAINERS else None
        owner = self.owner.get
        stack: list = [el]
        steps = 0
        while stack:
            steps += 1
            if limit is not None and steps > limit:
                raise TooLong
            node = stack.pop()
            if isinstance(node, str):
                yield node
                continue
            ours = owner(node) == mine
            for child in reversed(node):
                if child.tail and ours:
                    stack.append(child.tail)
                stack.append(child)
            if node.text and ours and isinstance(node.tag, str):
                yield node.text

    def text(self, el, sep: str = "") -> str:
        return sep.join(self.strings(el))


def _parse(html: str) -> Optional[_Page]:
    """The page's HTML parsed, or None when there is none (or nothing lxml can read)."""
    if not html or "<" not in html:
        return None
    # huge_tree: elements nested up to 2,048 deep are kept (without it, whatever is deeper than 255 is dropped)
    parser = etree.HTMLParser(collect_ids=False, huge_tree=True)
    try:
        parser.feed(html)
        root = parser.close()
    except (etree.LxmlError, ValueError):
        return None
    return _Page(root) if root is not None else None


def _meta(doc: Optional[_Page], *names: str) -> str:
    if doc is None:
        return ""
    for name in names:
        tag = next((m for m in doc.root.iter("meta") if m.get("property") == name), None)
        if tag is None:
            tag = next((m for m in doc.root.iter("meta") if m.get("name") == name), None)
        if tag is not None and clean(tag.get("content")):
            return clean(tag.get("content"))
    return ""


def _marked(tag, pattern: re.Pattern) -> bool:
    """Whether an element's id, any of its classes or its itemprop matches ``pattern``."""
    if not len(tag.attrib):
        return False
    values = [tag.get("id") or "", tag.get("itemprop") or "", tag.get("data-testid") or "",
              tag.get("data-automation") or ""]
    values += (tag.get("class") or "").split()
    return any(v and pattern.search(v) for v in values)


_NOT_SHORT = ("script", "style", "meta", "link", "input", "select", "option", "button", "body", "html")
_NEWLINE = object()
_RUBY = ("rt", "rp")  # ruby annotations: their text isn't part of get_text()'s


def _short_line(doc: _Page, tag) -> str:
    """``clean(get_text(" "))`` of an element when that is at most ``SHORT_VALUE`` characters, else "". Stops as soon
    as the text is too long, or after ``SHORT_WALK`` elements and strings (a deep or wide element is never a short
    line)."""
    parts: list[str] = []
    size = 0
    try:
        for string in doc.strings(tag, SHORT_WALK):
            piece = clean(string)
            if piece:
                size += len(piece) + (1 if parts else 0)
                if size > SHORT_VALUE:
                    return ""
                parts.append(piece)
    except TooLong:
        return ""
    return " ".join(parts)


def _short_text(doc: Optional[_Page], pattern: re.Pattern, *, digits: bool = False) -> str:
    """The text of the first small element marked with ``pattern`` (a location or salary line), of the first
    ``SHORT_CANDIDATES`` so marked."""
    if doc is None:
        return ""
    marked = (t for t in doc.elements() if t.tag not in _NOT_SHORT and _marked(t, pattern))
    for tag in itertools.islice(marked, SHORT_CANDIDATES):
        text = _short_line(doc, tag)
        if 2 <= len(text) <= SHORT_VALUE and (not digits or re.search(r"\d", text)):
            return text
    return ""


def _main_text(doc: Optional[_Page]) -> str:
    """The text of the page's main content: a job-description container, else ``<article>``, ``<main>`` or
    ``role="main"``; the first with a paragraph's worth of text, else the longest. One that is inside a container
    already tried is skipped (its text is part of that one's, which was too short), and at most
    ``MAX_CANDIDATES`` are tried, so no part of the page is read more than a few times."""
    if doc is None:
        return ""
    body = doc.first("body")
    scope = body if body is not None and (len(body) or body.text) else None  # an empty body: the whole page

    def each(test):
        return (t for t in doc.elements(scope) if test(t))

    candidates = itertools.chain(
        each(lambda t: t.tag not in ("meta", "script") and _marked(t, _DESCRIPTION)),
        each(lambda t: t.tag == "article"), each(lambda t: t.tag == "main"), each(lambda t: t.get("role") == "main"))
    best, read, tried = "", set(), 0
    for tag in candidates:
        if tag in read:
            continue
        tried += 1
        if tried > MAX_CANDIDATES:
            break
        text = _content_text(tag, read)
        if len(text) >= FULL_AD_CHARS:
            return text
        if len(text) > len(best):
            best = text
    return best


def _content_text(tag, read: Optional[set] = None) -> str:
    """An element's readable text, without navigation, forms, scripts and the like, laid out as
    ``backend.scraping.text.html_to_text`` lays out the element's HTML (paragraphs, "- " bullets), in one pass over
    the element. Like that, it reads the element on its own: only ``<rt>``/``<rp>`` inside it hide text (``<script>``,
    ``<style>`` and ``<template>`` are left out with the rest of ``_JUNK``). ``read`` collects the elements read."""
    out: list[str] = []
    stack: list = [(tag, False)]
    while stack:
        node, hidden = stack.pop()
        if node is _NEWLINE:
            out.append("\n")
            continue
        if isinstance(node, str):  # a tail, after its element
            out.append(node)
            continue
        name = node.tag
        if not isinstance(name, str) or name in _JUNK:  # a comment, or left out with all it holds
            continue
        if read is not None:
            read.add(node)
        if name == "li":
            out.append("\n- ")
        elif name in BLOCK_TAGS:
            out.append("\n")
            stack.append((_NEWLINE, hidden))
        inner = hidden or name in _RUBY
        for child in reversed(node):
            if child.tail and not inner:
                stack.append((child.tail, inner))
            stack.append((child, inner))
        if node.text and not inner:
            out.append(node.text)
    return normalize_block("".join(out))


def _org_name(nodes: list[dict]) -> str:
    for node in nodes:
        org = node.get("hiringOrganization")
        name = org.get("name") if isinstance(org, dict) else org if isinstance(org, str) else ""
        if clean(name):
            return clean(name)
    return ""


def _heuristic_posting(doc: Optional[_Page], nodes: list[dict], url: str, page_title: str) -> Posting:
    from backend.sources import default_label

    site = _meta(doc, "og:site_name", "application-name")
    company = _org_name(nodes) or site or default_label(url)
    names = _host_names(url) | ({site[:TITLE_INPUT]} if site else set())
    title = ""
    h1 = doc.first("h1") if doc is not None else None
    title_tag = doc.first("title") if doc is not None else None
    tag_title = clean(doc.text(title_tag)) if title_tag is not None else ""
    for raw in (_meta(doc, "og:title", "twitter:title"), clean(doc.text(h1, " ")) if h1 is not None else "",
                page_title, tag_title):
        title = clean_title(raw, names, company[:TITLE_INPUT])[:300]
        if title:
            break
    location = _short_text(doc, _LOCATION)
    return Posting(url=url, title=title, company=company, location_text=location,
                   salary_text=_short_text(doc, _SALARY, digits=True),
                   employment_type=normalise_employment_type(_short_text(doc, _EMPLOYMENT)))


def read_page(url: str, page_title: str = "", html: str = "", text: str = "", selection: str = "") -> PageJob:
    """The job on a page (see the module docstring). The posting's title may be empty: the caller asks for one."""
    notes: list[str] = []
    posting: Optional[Posting] = None
    method = "page"
    if html and "SEEK_REDUX_DATA" in html and _on_seek(url):
        seek = parse_job_page(html)  # stored under SEEK's address for its id: only on SEEK's own sites
        if seek is not None and seek.title and re.fullmatch(r"[0-9]{1,20}", seek.external_id or ""):
            posting, method = seek, "seek"
    nodes = find_job_postings(html) if html else []
    if posting is None and nodes:
        node = _matching_node(nodes, url)
        found = posting_from_jsonld(node, url) if node is not None else None
        if found is not None:
            found.url = _posting_url(found.url if found.url != url else "", url)
            posting, method = found, "jsonld"
    doc = None
    if posting is None:
        doc = _parse(html)
        posting = _heuristic_posting(doc, nodes, url, page_title)
    description_from = "structured" if posting.description else ""
    if not posting.description and not selection.strip():
        if doc is None:
            doc = _parse(html)
        main = _main_text(doc)
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

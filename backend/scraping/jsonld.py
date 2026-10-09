"""schema.org ``JobPosting`` extraction from JSON-LD blocks."""
from __future__ import annotations

import json
import re
from typing import Any, Iterable, Optional

from backend.adapters.base import Posting
from backend.employment import normalise_employment_type
from backend.scraping.text import clean, html_to_text

# A JSON-LD block is ``<script ... type="application/ld+json" ...>(.*?)</script>`` (any case). It is found by
# scanning (``_jsonld_blocks``), not by one regular expression over the page: on a page with many unclosed
# ``<script`` tags that would rescan the rest of the page from each one (quadratic; pages come from the browser
# extension too).
_SCRIPT_OPEN = re.compile(r"<script", re.IGNORECASE)
_SCRIPT_CLOSE = re.compile(r"</script>", re.IGNORECASE)
_LD_TYPE = re.compile(r'type=["\']application/ld\+json["\']', re.IGNORECASE)


def _jsonld_blocks(html: str) -> Iterable[str]:
    """The contents of the page's JSON-LD script blocks, in order. Each part of the page is looked at once."""
    pos = 0
    while True:
        start = _SCRIPT_OPEN.search(html, pos)
        if start is None:
            return
        attrs_at = start.end()
        tag_end = html.find(">", attrs_at)
        if tag_end < 0:
            return  # no tag after this one can end either
        # The type is an attribute after at least one other character (``<script type=...``, not ``<scripttype=``).
        # A ``<script`` inside this tag's attributes ends at the same ">", so it can't be a JSON-LD block when this
        # one isn't: carry on after the tag.
        if _LD_TYPE.search(html, attrs_at + 1, tag_end) is None:
            pos = tag_end + 1
            continue
        close = _SCRIPT_CLOSE.search(html, tag_end + 1)
        if close is None:
            return  # nor can any later block be closed
        yield html[tag_end + 1:close.start()]
        pos = close.end()


def _walk(node: Any) -> Iterable[dict]:
    if isinstance(node, list):
        for item in node:
            yield from _walk(item)
    elif isinstance(node, dict):
        yield node
        graph = node.get("@graph")
        if isinstance(graph, list):
            yield from _walk(graph)
        for key in ("itemListElement", "item"):
            if key in node:
                yield from _walk(node[key])


def _is_type(node: dict, name: str) -> bool:
    t = node.get("@type")
    types = t if isinstance(t, list) else [t]
    return any(isinstance(x, str) and x.lower() == name.lower() for x in types)


def find_job_postings(html: str) -> list[dict]:
    out: list[dict] = []
    for raw in _jsonld_blocks(html or ""):
        try:
            data = json.loads(raw.strip())
            found = [n for n in _walk(data) if _is_type(n, "JobPosting")]
        except (ValueError, RecursionError):  # not JSON, or nested too deep to read
            continue
        out.extend(found)
    return out


def _place_text(place: Any) -> tuple[str, str]:
    """(location text, ISO country) from a schema.org Place / address."""
    if isinstance(place, list):
        texts, country = [], ""
        for p in place:
            t, c = _place_text(p)
            if t:
                texts.append(t)
            country = country or c
        return "; ".join(dict.fromkeys(texts)), country
    if isinstance(place, str):
        return clean(place), ""
    if not isinstance(place, dict):
        return "", ""
    addr = place.get("address", place)
    if isinstance(addr, str):
        return clean(addr), ""
    if not isinstance(addr, dict):
        return clean(place.get("name")), ""
    country = addr.get("addressCountry") or ""
    if isinstance(country, dict):
        country = country.get("name") or ""
    parts = [addr.get("addressLocality"), addr.get("addressRegion"), country]
    text = ", ".join(clean(p) for p in parts if isinstance(p, str) and p.strip())
    iso = country.strip().upper() if isinstance(country, str) and len(country.strip()) == 2 else ""
    return text or clean(place.get("name")), iso


def _salary(node: dict) -> tuple[str, Optional[float], Optional[float]]:
    base = node.get("baseSalary")
    if not isinstance(base, dict):
        return "", None, None
    value = base.get("value")
    currency = base.get("currency") or ""
    if isinstance(value, dict):
        lo, hi = value.get("minValue"), value.get("maxValue")
        single = value.get("value")
        unit = value.get("unitText") or ""
        try:
            lo = float(lo) if lo is not None else (float(single) if single is not None else None)
            hi = float(hi) if hi is not None else lo
        except (TypeError, ValueError):
            return "", None, None
        if lo is None:
            return "", None, None
        text = f"{currency} {lo:,.0f}" + (f" - {hi:,.0f}" if hi and hi != lo else "") + (f" per {unit.lower()}" if unit else "")
        if unit.upper() == "YEAR" or not unit:
            return text.strip(), lo, hi
        return text.strip(), None, None
    return "", None, None


def posting_from_jsonld(node: dict, page_url: str) -> Optional[Posting]:
    title = clean(node.get("title") or node.get("name"))
    if not title:
        return None
    url = node.get("url") or page_url
    if isinstance(url, list):
        url = url[0] if url else page_url
    org = node.get("hiringOrganization")
    company = clean(org.get("name") if isinstance(org, dict) else org if isinstance(org, str) else "")
    location, country = _place_text(node.get("jobLocation"))
    loc_type = str(node.get("jobLocationType") or "").upper()
    work_mode = "remote" if "TELECOMMUTE" in loc_type else "unknown"
    if not country:
        req = node.get("applicantLocationRequirements")
        _, country = _place_text(req)
    salary_text, smin, smax = _salary(node)
    description = html_to_text(str(node.get("description") or ""))
    return Posting(
        url=str(url),
        title=title,
        company=company,
        external_id=clean(node.get("identifier", {}).get("value") if isinstance(node.get("identifier"), dict) else node.get("identifier") or ""),
        location_text=location,
        country=country,
        work_mode=work_mode,
        employment_type=normalise_employment_type(node.get("employmentType")),
        salary_text=salary_text,
        salary_min=smin,
        salary_max=smax,
        description=description,
        detail_status="full" if len(description) > 200 else "none",
        posted_at=clean(node.get("datePosted")),
        industry=clean(node.get("industry") if isinstance(node.get("industry"), str) else ""),
    )

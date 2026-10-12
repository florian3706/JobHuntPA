"""Where a job's stored ad came from, as ``jobs.source`` says it, and the site that stands for.

The values: ``seek`` (the app's SEEK search), ``<board>:<company>`` (a job board's feed, e.g.
``workable:compass-education``), ``site:<host>`` (a crawled careers page), ``import`` (a saved page), and, for a job
the browser extension added, ``browser:<how>:<site>``. ``<how>`` is ``single`` (read from the job's own page) or
``bulk`` (taken from a card on a page of search results); ``<site>`` is the host of the address the job is stored
under, without "www." (``seek.com.au``). Plain ``browser`` is what the extension stored before it said how and
where: ``pipeline.name_browser_sources`` renames those, once per workspace.

What people read is made from these values in two places that follow one rule: ``sourceLabel`` in
frontend/common.js and ``email_templates.source_label`` ("SEEK - browser extension (bulk)"). No imports from the
app here, so anything may use it.
"""
from __future__ import annotations

from typing import Optional
from urllib.parse import urlsplit

BROWSER = "browser"
SINGLE, BULK = "single", "bulk"
# Sites named by more than their address: the host (or a host under it) and the name people know.
SITE_NAMES = {"seek.com.au": "SEEK", "seek.co.nz": "SEEK", "seek.com": "SEEK", "linkedin.com": "LinkedIn",
              "indeed.com": "Indeed"}


def host_of(url: Optional[str]) -> str:
    """The host of an address, lower case and without "www."; "" when there is none."""
    try:
        host = (urlsplit(url or "").hostname or "").lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def browser(how: str, url: Optional[str]) -> str:
    """``jobs.source`` of a job the browser extension added ``how`` (``SINGLE`` or ``BULK``), stored under ``url``."""
    return f"{BROWSER}:{how}:{host_of(url)}"


def browser_parts(source: Optional[str]) -> Optional[tuple[str, str]]:
    """(how, site) of a ``browser:<how>:<site>`` source, else None (also for the old plain ``browser``)."""
    kind, _, rest = (source or "").partition(":")
    if kind != BROWSER or not rest:
        return None
    how, _, site = rest.partition(":")
    return how, site


def site_name(host: str) -> str:
    """The name people know a site by ("SEEK"), else its host."""
    for known, name in SITE_NAMES.items():
        if host == known or host.endswith("." + known):
            return name
    return host


def site_of(source: Optional[str]) -> str:
    """What two jobs "on the same site" share (backend.duplicates): a job the extension added counts as its site's,
    and SEEK's as ``seek`` like the searches' own; any other source stands for itself."""
    parts = browser_parts(source)
    if parts is None:
        return source or ""
    return "seek" if site_name(parts[1]) == "SEEK" else parts[1] or BROWSER

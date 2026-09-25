"""Adapter contract shared by every job source.

An adapter works in two phases so the pipeline can skip work it has
already done:

1. ``list_postings()`` - one cheap pass over the source's listing (search
   results page, ATS job list API, careers index). Returns
   :class:`Posting` stubs with whatever the listing exposes.
2. ``fetch_detail(posting)`` - fetches the full description for ONE
   posting. The pipeline only calls it for postings that are not already
   stored with a full description and that survive the cheap pre-filters.
   Returns ``None`` when the page turned out not to be a job posting.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class Posting:
    url: str
    title: str
    company: str = ""
    external_id: str = ""
    location_text: str = ""
    country: str = ""            # ISO-2 upper case when known
    work_mode: str = "unknown"   # remote | hybrid | onsite | unknown
    salary_text: str = ""
    salary_min: Optional[float] = None
    salary_max: Optional[float] = None
    description: str = ""
    detail_status: str = "none"  # none | summary | full
    posted_at: str = ""
    industry: str = ""
    # Adapter-private data needed by fetch_detail (not persisted).
    detail_ref: Any = field(default=None, repr=False)


class Adapter:
    #: stable id stored on jobs.source, e.g. "workable:compass-education"
    source: str = ""
    #: human-readable description of how this source is scraped
    method: str = ""
    #: True when list_postings() returns every open job of the source, so
    #: previously seen jobs missing from it can be marked closed.
    complete_listing: bool = True
    #: False when fetch_detail() can never add anything (e.g. SEEK)
    has_details: bool = True

    def list_postings(self) -> list[Posting]:
        raise NotImplementedError

    def fetch_detail(self, posting: Posting) -> Optional[Posting]:
        return posting


class SourceError(Exception):
    """A source could not be scraped; the message is shown in the UI."""

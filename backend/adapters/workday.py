"""Workday (CXS) fallback stub.

Workday has no single public postings API: every tenant exposes its own
``/wday/cxs/<tenant>/<site>/jobs`` endpoint with slightly different
payloads. This stub keeps the ingest pipeline uniform — configure one
instance per company and it degrades gracefully (returns []) until a
tenant-specific parser is implemented.
"""

from __future__ import annotations

import logging

from backend.adapters.base import BaseAdapter, NormalizedJob

log = logging.getLogger(__name__)


class WorkdayAdapter(BaseAdapter):
    """Generic Workday tenant stub. Always returns [] with a warning."""

    source = "workday"

    def __init__(self, tenant: str = "", site: str = "", base_url: str = ""):
        self.tenant = (tenant or "").strip()
        self.site = (site or "").strip()
        self.base_url = (base_url or "").strip()
        if self.tenant:
            self.source = f"workday:{self.tenant}"

    def fetch(self) -> list[NormalizedJob]:
        log.warning(
            "WorkdayAdapter stub (tenant=%s site=%s): no generic parser; "
            "returning [] — implement tenant-specific CXS parsing as needed.",
            self.tenant or "?",
            self.site or "?",
        )
        return []


__all__ = ["WorkdayAdapter"]

"""Ashby postings API adapter.

Endpoint: GET https://api.ashbyhq.com/posting-api/job-board/{board}
"""

from __future__ import annotations

import logging

from backend.adapters.base import BaseAdapter, NormalizedJob, RateLimiter

log = logging.getLogger(__name__)

try:
    import httpx  # type: ignore
except Exception:  # pragma: no cover
    httpx = None  # type: ignore[assignment]

API_BASE = "https://api.ashbyhq.com/posting-api/job-board"


class AshbyAdapter(BaseAdapter):
    """Fetch jobs for one Ashby job board slug."""

    def __init__(
        self,
        board: str,
        min_interval_seconds: float = 1.0,
        timeout: float = 15.0,
    ):
        self.board = (board or "").strip()
        self.source = f"ashby:{self.board}" if self.board else "ashby"
        self.limiter = RateLimiter(min_interval_seconds)
        self.timeout = timeout

    def fetch(self) -> list[NormalizedJob]:
        if httpx is None:
            log.warning("httpx not installed; Ashby fetch skipped.")
            return []
        if not self.board:
            log.warning("AshbyAdapter: empty board.")
            return []
        url = f"{API_BASE}/{self.board}"
        self.limiter.wait()
        try:
            with httpx.Client(timeout=self.timeout, follow_redirects=True) as client:
                resp = client.get(url)
                resp.raise_for_status()
                payload = resp.json()
        except Exception as exc:
            log.warning("Ashby fetch failed for board %s: %s", self.board, exc)
            return []
        items = payload.get("jobs", []) or []
        jobs: list[NormalizedJob] = []
        for item in items:
            try:
                loc = item.get("locationName", "") or ""
                jobs.append(
                    NormalizedJob(
                        source=self.source,
                        external_id=str(item.get("id", "")),
                        company=self.board,
                        title=item.get("title", ""),
                        location_text=loc,
                        work_mode=item.get("workplaceType", "") or "",
                        salary_text=(item.get("compensationTierSummary", "") or ""),
                        url=item.get("jobUrl", "") or "",
                        description=item.get("descriptionPlain", "")
                        or item.get("descriptionHtml", "")
                        or "",
                        industry=item.get("departmentName", "") or "",
                    )
                )
            except Exception as exc:
                log.debug("Skipping ashby job: %s", exc)
                continue
        return jobs


__all__ = ["AshbyAdapter", "API_BASE"]

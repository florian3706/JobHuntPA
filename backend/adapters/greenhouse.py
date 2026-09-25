"""Greenhouse boards API adapter.

Docs: https://developers.greenhouse.io/harvest.html (job board variant)
Endpoint: GET https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs
"""

from __future__ import annotations

import logging

from backend.adapters.base import BaseAdapter, NormalizedJob, RateLimiter, normalize_text

log = logging.getLogger(__name__)

try:
    import httpx  # type: ignore
except Exception:  # pragma: no cover
    httpx = None  # type: ignore[assignment]

API_BASE = "https://boards-api.greenhouse.io/v1/boards"


class GreenhouseAdapter(BaseAdapter):
    """Fetch jobs for one Greenhouse board token (company)."""

    def __init__(
        self,
        board_token: str,
        min_interval_seconds: float = 1.0,
        timeout: float = 15.0,
    ):
        self.board_token = (board_token or "").strip()
        self.source = f"greenhouse:{self.board_token}" if self.board_token else "greenhouse"
        self.limiter = RateLimiter(min_interval_seconds)
        self.timeout = timeout

    def fetch(self) -> list[NormalizedJob]:
        if httpx is None:
            log.warning("httpx not installed; Greenhouse fetch skipped.")
            return []
        if not self.board_token:
            log.warning("GreenhouseAdapter: empty board token.")
            return []
        url = f"{API_BASE}/{self.board_token}/jobs?content=true"
        self.limiter.wait()
        try:
            with httpx.Client(timeout=self.timeout, follow_redirects=True) as client:
                resp = client.get(url)
                resp.raise_for_status()
                payload = resp.json()
        except Exception as exc:
            log.warning("Greenhouse fetch failed for board %s: %s", self.board_token, exc)
            return []
        jobs: list[NormalizedJob] = []
        for item in payload.get("jobs", []) or []:
            try:
                loc = item.get("location") or {}
                jobs.append(
                    NormalizedJob(
                        source=self.source,
                        external_id=str(item.get("id", "")),
                        company=normalize_text(
                            (item.get("company_name") or "") or self.board_token
                        ),
                        title=item.get("title", ""),
                        location_text=(loc.get("name", "") if isinstance(loc, dict) else ""),
                        url=item.get("absolute_url", ""),
                        description=item.get("content", "") or "",
                        industry="",
                    )
                )
            except Exception as exc:
                log.debug("Skipping greenhouse job: %s", exc)
                continue
        return jobs


__all__ = ["GreenhouseAdapter", "API_BASE"]

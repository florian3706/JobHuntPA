"""Lever postings API adapter.

Endpoint: GET https://api.lever.co/v0/postings/{host}?mode=json
"""

from __future__ import annotations

import logging

from backend.adapters.base import BaseAdapter, NormalizedJob, RateLimiter

log = logging.getLogger(__name__)

try:
    import httpx  # type: ignore
except Exception:  # pragma: no cover
    httpx = None  # type: ignore[assignment]

API_BASE = "https://api.lever.co/v0/postings"


class LeverAdapter(BaseAdapter):
    """Fetch jobs for one Lever company host (e.g. ``duolingo``)."""

    def __init__(
        self,
        host: str,
        min_interval_seconds: float = 1.0,
        timeout: float = 15.0,
    ):
        self.host = (host or "").strip()
        self.source = f"lever:{self.host}" if self.host else "lever"
        self.limiter = RateLimiter(min_interval_seconds)
        self.timeout = timeout

    def fetch(self) -> list[NormalizedJob]:
        if httpx is None:
            log.warning("httpx not installed; Lever fetch skipped.")
            return []
        if not self.host:
            log.warning("LeverAdapter: empty host.")
            return []
        url = f"{API_BASE}/{self.host}?mode=json"
        self.limiter.wait()
        try:
            with httpx.Client(timeout=self.timeout, follow_redirects=True) as client:
                resp = client.get(url)
                resp.raise_for_status()
                payload = resp.json()
        except Exception as exc:
            log.warning("Lever fetch failed for host %s: %s", self.host, exc)
            return []
        if isinstance(payload, dict):
            items = payload.get("data", [])
        else:
            items = payload or []
        jobs: list[NormalizedJob] = []
        for item in items:
            try:
                cats = item.get("categories") or {}
                jobs.append(
                    NormalizedJob(
                        source=self.source,
                        external_id=str(item.get("id", "")),
                        company=self.host,
                        title=item.get("text", ""),
                        location_text=cats.get("location", "") or "",
                        work_mode=(cats.get("commitment", "") or ""),
                        salary_text="",
                        url=item.get("hostedUrl", "") or "",
                        description=item.get("description", "") or "",
                        industry=cats.get("department", "") or "",
                    )
                )
            except Exception as exc:
                log.debug("Skipping lever job: %s", exc)
                continue
        return jobs


__all__ = ["LeverAdapter", "API_BASE"]

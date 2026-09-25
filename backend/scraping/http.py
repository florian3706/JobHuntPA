"""Polite HTTP fetching shared by every adapter.

- Honest User-Agent (``backend.config.USER_AGENT``); no browser spoofing.
- robots.txt is checked for every URL (including each redirect hop).
  Disallowed URLs raise :class:`RobotsBlocked` instead of being fetched.
- Per-host minimum interval between requests (robots ``Crawl-delay``
  honoured, capped at 30 s).
- Optional DB cache (``http_cache`` table) with a caller-chosen TTL, used
  for listing pages / API listings so repeated runs within the TTL cost
  nothing. Job detail pages are not cached here: the ``jobs`` table is
  their cache (see ``backend.pipeline``).
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Optional
from urllib.parse import urljoin, urlsplit

import httpx

from backend.config import ROBOTS_AGENT, USER_AGENT
from backend.db import HttpCache, SessionLocal
from backend.scraping.robots import RobotsRules

log = logging.getLogger(__name__)

DEFAULT_INTERVAL_S = 2.0
MAX_CRAWL_DELAY_S = 30.0
ROBOTS_TTL = timedelta(hours=24)
MAX_REDIRECTS = 5


class FetchError(Exception):
    """A fetch failed; ``str(exc)`` is shown to the user per source."""

    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


class RobotsBlocked(FetchError):
    pass


@dataclass
class Response:
    url: str
    status: int
    text: str
    from_cache: bool = False

    def json(self) -> Any:
        return json.loads(self.text)


def _origin(url: str) -> str:
    p = urlsplit(url)
    return f"{p.scheme}://{p.netloc}"


class PoliteClient:
    def __init__(self, timeout: float = 25.0, min_interval_s: float = DEFAULT_INTERVAL_S):
        self._client = httpx.Client(
            headers={"User-Agent": USER_AGENT, "Accept-Language": "en-AU,en;q=0.9"},
            timeout=timeout,
            follow_redirects=False,
        )
        self._min_interval = min_interval_s
        self._robots: dict[str, RobotsRules] = {}
        self._last_hit: dict[str, float] = {}
        self._lock = threading.Lock()

    def close(self) -> None:
        self._client.close()

    # -- robots ------------------------------------------------------------
    def robots_for(self, url: str) -> RobotsRules:
        origin = _origin(url)
        if origin in self._robots:
            return self._robots[origin]
        robots_url = origin + "/robots.txt"
        cached = _cache_get(robots_url, ROBOTS_TTL)
        if cached is not None:
            rules = RobotsRules(cached.text if cached.status == 200 else "", ROBOTS_AGENT)
        else:
            rules = self._fetch_robots(robots_url)
        self._robots[origin] = rules
        return rules

    def _fetch_robots(self, robots_url: str) -> RobotsRules:
        url = robots_url
        try:
            for _ in range(MAX_REDIRECTS):
                self._throttle(url, DEFAULT_INTERVAL_S)
                resp = self._client.get(url)
                if resp.is_redirect and resp.headers.get("location"):
                    url = urljoin(url, resp.headers["location"])
                    continue
                break
        except httpx.HTTPError as exc:
            raise FetchError(f"robots.txt unreachable ({exc.__class__.__name__}); not crawling {_origin(robots_url)}")
        if resp.status_code >= 500:
            # RFC 9309: an unreachable robots.txt means "assume disallow all".
            raise FetchError(f"robots.txt returned HTTP {resp.status_code}; not crawling {_origin(robots_url)}")
        body = resp.text if resp.status_code == 200 else ""
        _cache_put(robots_url, resp.status_code, body)
        return RobotsRules(body, ROBOTS_AGENT)

    def check_allowed(self, url: str) -> None:
        if not self.robots_for(url).allowed(url):
            raise RobotsBlocked(f"robots.txt disallows {urlsplit(url).path or '/'} on {urlsplit(url).netloc}")

    # -- throttling --------------------------------------------------------
    def _throttle(self, url: str, interval: float) -> None:
        host = urlsplit(url).netloc
        with self._lock:
            wait = self._last_hit.get(host, 0.0) + interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last_hit[host] = time.monotonic()

    def _interval_for(self, url: str) -> float:
        delay = self.robots_for(url).crawl_delay
        if delay is None:
            return self._min_interval
        return min(max(delay, self._min_interval), MAX_CRAWL_DELAY_S)

    # -- requests ----------------------------------------------------------
    def get(self, url: str, *, ttl: Optional[timedelta] = None) -> Response:
        return self.request("GET", url, ttl=ttl)

    def post_json(self, url: str, payload: Any, *, ttl: Optional[timedelta] = None) -> Response:
        return self.request("POST", url, json_body=payload, ttl=ttl)

    def request(
        self,
        method: str,
        url: str,
        *,
        json_body: Any = None,
        ttl: Optional[timedelta] = None,
    ) -> Response:
        cache_key = url if json_body is None else url + "#" + hashlib.sha256(
            json.dumps(json_body, sort_keys=True).encode()).hexdigest()[:16]
        if ttl is not None:
            cached = _cache_get(cache_key, ttl)
            if cached is not None:
                return cached

        current = url
        for _ in range(MAX_REDIRECTS + 1):
            self.check_allowed(current)
            self._throttle(current, self._interval_for(current))
            try:
                resp = self._client.request(method, current, json=json_body)
            except httpx.HTTPError as exc:
                raise FetchError(f"{exc.__class__.__name__} fetching {current}") from exc
            if resp.is_redirect and resp.headers.get("location"):
                current = urljoin(current, resp.headers["location"])
                continue
            break
        else:
            raise FetchError(f"too many redirects from {url}")

        if resp.status_code in (401, 403):
            raise FetchError(f"HTTP {resp.status_code}: {urlsplit(current).netloc} refused automated access", resp.status_code)
        if resp.status_code == 429:
            raise FetchError(f"HTTP 429: {urlsplit(current).netloc} is rate limiting us; try again later", 429)
        if resp.status_code >= 400:
            raise FetchError(f"HTTP {resp.status_code} for {current}", resp.status_code)

        out = Response(url=current, status=resp.status_code, text=resp.text)
        if ttl is not None:
            _cache_put(cache_key, out.status, out.text)
        return out

    # -- JS rendering ------------------------------------------------------
    def render(self, url: str, wait_ms: int = 2500) -> Response:
        """Render a JS-heavy page in headless Chromium (robots + throttle apply)."""
        self.check_allowed(url)
        self._throttle(url, self._interval_for(url))
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise FetchError("page needs JavaScript but Playwright is not installed") from exc
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                try:
                    page = browser.new_page(user_agent=USER_AGENT)
                    resp = page.goto(url, timeout=30000, wait_until="domcontentloaded")
                    page.wait_for_timeout(wait_ms)
                    status = resp.status if resp else 200
                    html = page.content()
                    final_url = page.url
                finally:
                    browser.close()
        except Exception as exc:
            raise FetchError(f"browser render failed: {exc}") from exc
        if status in (401, 403, 429) or status >= 400:
            raise FetchError(f"HTTP {status} rendering {url}", status)
        return Response(url=final_url, status=status, text=html)


def _cache_get(key: str, ttl: timedelta) -> Optional[Response]:
    db = SessionLocal()
    try:
        row = db.get(HttpCache, key)
        if row is None or datetime.utcnow() - row.fetched_at > ttl:
            return None
        return Response(url=key.split("#", 1)[0], status=row.status_code, text=row.body, from_cache=True)
    finally:
        db.close()


def _cache_put(key: str, status: int, body: str) -> None:
    db = SessionLocal()
    try:
        row = db.get(HttpCache, key)
        if row is None:
            row = HttpCache(url=key)
            db.add(row)
        row.status_code = status
        row.body = body
        row.fetched_at = datetime.utcnow()
        db.commit()
    finally:
        db.close()


def purge_cache(older_than: timedelta = timedelta(days=2)) -> int:
    db = SessionLocal()
    try:
        n = db.query(HttpCache).filter(HttpCache.fetched_at < datetime.utcnow() - older_than).delete()
        db.commit()
        return n
    finally:
        db.close()

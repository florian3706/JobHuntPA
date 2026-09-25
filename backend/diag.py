"""Diagnostics endpoints (read-only, never raise).

Endpoints:
    GET  /api/diag/seek  - build first SEEK search URL, httpx GET, parse check.
    GET  /api/diag/score - scorer config status + DB counts + recent errors.

NOTE: POST /api/score/test is canonically served by backend.routes_score
(test_scorer_endpoint -> {ok, has_key, base_url, model, score, summary,
result}). diag.py intentionally defines NO /api/score/* route to avoid
FastAPI shadowing (first-registered wins); frontend/app.js consumes the
routes_score shape (with fallback to result.summary).

All handlers catch every exception and return a degraded payload instead of
raising, so diagnostics never break the app.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from .adapters.base import get_cached_html, set_cached_html
from .adapters.seek import DEFAULT_UA, build_search_urls, parse_seek_html
from .db import Document, FitResult, get_db
from .scorer import get_config, load_profile_text

log = logging.getLogger(__name__)

router = APIRouter(tags=["diag"])


def _playwright_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright  # type: ignore  # noqa: F401

        return True
    except Exception:
        return False


@router.get("/api/diag/seek")
def diag_seek(
    keywords: str = Query(default="backend"),
    location: str = Query(default="Sydney"),
    use_playwright: bool = Query(default=False),
    bypass_cache: bool = Query(default=False),
) -> dict:
    """Fetch the first SEEK search URL and report HTTP + parse health."""
    playwright_available = _playwright_available()
    try:
        urls = build_search_urls(keywords or "backend", location or None, max_pages=1)
        search_url = urls[0] if urls else ""
    except Exception as exc:
        return {
            "search_url": "",
            "http_status": None,
            "html_len": 0,
            "parse_count": 0,
            "sample_titles": [],
            "playwright_available": playwright_available,
            "cached": False,
            "error": str(exc)[:500],
        }
    if not search_url:
        return {
            "search_url": "",
            "http_status": None,
            "html_len": 0,
            "parse_count": 0,
            "sample_titles": [],
            "playwright_available": playwright_available,
            "cached": False,
            "error": "could not build search URL",
        }

    cached = False
    html: Optional[str] = None
    http_status: Optional[int] = None
    error: Optional[str] = None

    # 1) Cache lookup (skipped when bypass_cache=true).
    if not bypass_cache:
        try:
            html = get_cached_html(search_url)
            cached = html is not None
        except Exception as exc:
            log.debug("diag cache read failed: %s", exc)
            html = None
            cached = False

    # 2) Fresh httpx GET when no usable cache entry.
    if html is None:
        try:
            try:
                import httpx  # type: ignore
            except Exception:
                httpx = None  # type: ignore[assignment]
            if httpx is None:
                raise RuntimeError("httpx not installed; cannot fetch")
            with httpx.Client(
                headers={"User-Agent": DEFAULT_UA, "Accept-Language": "en-AU,en;q=0.9"},
                timeout=15.0,
                follow_redirects=True,
            ) as client:
                resp = client.get(search_url)
                http_status = resp.status_code
                resp.raise_for_status()
                html = resp.text
            cached = False
            try:
                set_cached_html(search_url, html or "")
            except Exception:
                pass
        except Exception as exc:
            return {
                "search_url": search_url,
                "http_status": http_status,
                "html_len": len(html or ""),
                "parse_count": 0,
                "sample_titles": [],
                "playwright_available": playwright_available,
                "cached": False,
                "error": str(exc)[:1000],
            }

    # 3) Parse (best-effort, never raise).
    parse_count = 0
    sample_titles: list[str] = []
    try:
        jobs = parse_seek_html(html or "", search_url=search_url)
        parse_count = len(jobs)
        sample_titles = [j.title for j in jobs[:5] if j.title]
    except Exception as exc:
        error = str(exc)[:1000]

    # 4) Optional Playwright fallback when static parse found nothing.
    if use_playwright and parse_count == 0 and playwright_available:
        try:
            from .adapters.seek import _fetch_with_playwright  # type: ignore

            html2 = _fetch_with_playwright(search_url)
            if html2:
                html = html2
                cached = False
                try:
                    jobs = parse_seek_html(html or "", search_url=search_url)
                    parse_count = len(jobs)
                    sample_titles = [j.title for j in jobs[:5] if j.title]
                except Exception as exc:
                    error = str(exc)[:1000]
        except Exception as exc:
            error = str(exc)[:1000]

    return {
        "search_url": search_url,
        "http_status": http_status,
        "html_len": len(html or ""),
        "parse_count": parse_count,
        "sample_titles": sample_titles,
        "playwright_available": playwright_available,
        "cached": cached,
        "error": error,
    }


@router.get("/api/diag/score")
def diag_score(db: Session = Depends(get_db)) -> dict:
    """Report scorer config + DB health. Never echoes the API key."""
    try:
        cfg = get_config()
        has_key = bool(cfg.get("api_key"))
        base_url = str(cfg.get("base_url") or "")
        model = str(cfg.get("model") or "")
        key_source = str(cfg.get("key_source") or "none") or "none"
    except Exception:
        has_key, base_url, model, key_source = False, "", "", "none"
    try:
        from . import scorer as _scorer_mod

        _valid_fn = getattr(_scorer_mod, "is_valid_model", None)
        _valid_models = getattr(_scorer_mod, "VALID_MODELS", None)
        if _valid_models is None:
            _valid_models = (
                "muse-spark-1.1",
                "muse-spark-1.2",
                "muse-spark-1.2-contributor",
                "muse-spark-1.3",
                "muse-spark-1.3-contributor",
            )
        valid_models = sorted(str(m) for m in list(_valid_models or []))
        if callable(_valid_fn):
            try:
                model_valid = bool(_valid_fn(model))
            except Exception:
                model_valid = (model in valid_models) if valid_models else True
        else:
            model_valid = (model in valid_models) if valid_models else True
    except Exception:
        valid_models = sorted(
            [
                "muse-spark-1.1",
                "muse-spark-1.2",
                "muse-spark-1.2-contributor",
                "muse-spark-1.3",
                "muse-spark-1.3-contributor",
            ]
        )
        model_valid = True
    try:
        docs_count = db.query(Document).count()
    except Exception:
        docs_count = 0
    try:
        profile_text_len = len(load_profile_text(db) or "")
    except Exception:
        profile_text_len = 0
    recent_errors: list[str] = []
    try:
        rows = (
            db.query(FitResult)
            .order_by(FitResult.id.desc())
            .limit(20)
            .all()
        )
        for row in rows:
            try:
                obj = json.loads(row.evidence_json or "{}")
            except Exception:
                obj = {"summary": row.evidence_json or ""}
            summary = obj.get("summary", "") if isinstance(obj, dict) else str(obj)
            if isinstance(summary, str) and (
                summary.startswith("ERROR:") or summary.startswith("NO_KEY")
            ):
                recent_errors.append(summary)
            if len(recent_errors) >= 5:
                break
    except Exception:
        recent_errors = []
    return {
        "has_key": has_key,
        "key_source": key_source,
        "base_url": base_url,
        "model": model,
        "model_valid": model_valid,
        "valid_models": valid_models,
        "docs_count": docs_count,
        "profile_text_len": profile_text_len,
        "recent_errors": recent_errors,
    }

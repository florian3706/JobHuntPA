"""Company sources registry (DB + JSON fallback).

A "company source" is one company's job feed: a Greenhouse/Lever/Ashby/
Workday board or a generic careers page scraped by GenericAdapter.

Storage:
  - Primary: ``company_sources`` table via ``backend.db.Base`` (created by
    ``backend.db.init_db()`` once this module is imported).
  - Fallback: ``data/sources.json`` when the DB is unavailable — a JSON list
    (or ``{"sources": [...]}`` envelope) of source dicts.

Canonical schema (superset of both predecessor agents):
  {id, label, careers_url UNIQUE, source_type, adapter_key, enabled,
   last_run, last_count, last_error}
plus compat aliases: company (=label), url (=careers_url), board, host
(and workday tenant/site/base_url, used by backend.ingest).

Source dict shape (both stores; tolerant of aliases):
  {"label": "Acme", "type": "greenhouse", "board": "acme", "enabled": true}
  {"label": "Acme", "type": "lever", "host": "acme", "enabled": true}
  {"label": "Acme", "type": "ashby", "board": "acme", "enabled": true}
  {"label": "Acme", "type": "workday", "tenant": "acme", "site": "jobs",
   "base_url": "https://...", "enabled": true}
  {"label": "Acme", "type": "generic", "url": "https://acme.com/careers",
   "enabled": true}

Aliases accepted: type <- adapter/kind/source_type; url <- careers_url/href/
link; board <- board_token/slug/handle; host <- board (lever).
``enabled`` defaults to True unless explicitly False.

All helpers never raise — they return [] / {} on failure (offline-safe).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

log = logging.getLogger(__name__)

_REPOS_ROOT_FALLBACK = Path(__file__).resolve().parents[1]
SOURCES_JSON = _REPOS_ROOT_FALLBACK / "data" / "sources.json"

try:
    from backend.db import Base
    from sqlalchemy import Boolean, Column, DateTime, Integer, String, Text

    class CompanySource(Base):  # type: ignore[no-redef]
        """One company's job feed (board or careers page).

        Superset of both predecessor schemas:
        canonical {id, label, careers_url UNIQUE, source_type, adapter_key,
        enabled, last_run, last_count, last_error} + compat aliases
        (company, url, board, host) + workday (tenant, site, base_url).
        """

        __tablename__ = "company_sources"

        id = Column(Integer, primary_key=True, index=True)
        label = Column(String, nullable=True, default="")
        careers_url = Column(String, unique=True, nullable=True, default="")
        source_type = Column(String, nullable=True, default="generic")
        adapter_key = Column(String, nullable=True, default="")
        enabled = Column(Boolean, nullable=False, default=True)
        last_run = Column(DateTime, nullable=True)
        last_count = Column(Integer, nullable=True, default=0)
        last_error = Column(Text, nullable=True, default=None)
        # Compat aliases (old agent schema).
        company = Column(String, nullable=True, default="")
        url = Column(String, nullable=True, default="")
        board = Column(String, nullable=True, default="")
        host = Column(String, nullable=True, default="")
        # Workday details (used by backend.ingest.build_sources_adapters).
        tenant = Column(String, nullable=True, default="")
        site = Column(String, nullable=True, default="")
        base_url = Column(String, nullable=True, default="")

except Exception as exc:  # pragma: no cover - sqlalchemy missing
    log.debug("CompanySource ORM unavailable: %s", exc)
    CompanySource = None  # type: ignore[assignment,misc]


# ---------------------------------------------------------------------------
# Source-type detection (case-insensitive)
# ---------------------------------------------------------------------------

def detect_source(url: Optional[str]) -> str:
    """Detect the source/adapter type from a careers URL. Never raises.

    Case-insensitive substring match -> one of
    ``greenhouse`` | ``lever`` | ``ashby`` | ``workday`` | ``generic``.
    """
    try:
        u = (url or "").strip().lower()
        if not u:
            return "generic"
        if "greenhouse" in u:
            return "greenhouse"
        if "lever" in u:
            return "lever"
        if "ashby" in u:
            return "ashby"
        if "workday" in u or "myworkday" in u:
            return "workday"
        return "generic"
    except Exception:
        return "generic"


def _domain_of(url: str) -> str:
    try:
        return urlparse(url or "").netloc.lower()
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Schema migration: existing DBs may predate the superset (either agent's
# table). Add any missing columns + backfill aliases. Never raises.
# ---------------------------------------------------------------------------

_COMPAT_COLUMNS: dict[str, str] = {
    "careers_url": "VARCHAR",
    "source_type": "VARCHAR",
    "adapter_key": "VARCHAR",
    "last_run": "DATETIME",
    "last_count": "INTEGER",
    "last_error": "TEXT",
    "company": "VARCHAR",
    "url": "VARCHAR",
    "board": "VARCHAR",
    "host": "VARCHAR",
    "tenant": "VARCHAR",
    "site": "VARCHAR",
    "base_url": "VARCHAR",
    "label": "VARCHAR",
    "enabled": "BOOLEAN",
}


def ensure_company_sources_schema() -> None:
    """Create ``company_sources`` + add missing superset columns. Never raises."""
    try:
        from backend.db import engine, init_db

        try:
            init_db()
        except Exception:
            pass
        import sqlalchemy as _sa

        with engine.connect() as conn:
            try:
                existing = {
                    r[1]
                    for r in conn.exec_driver_sql(
                        "PRAGMA table_info(company_sources)"
                    ).fetchall()
                }
            except Exception:
                return
            for col, ddl in _COMPAT_COLUMNS.items():
                if col in existing or col == "id":
                    continue
                try:
                    conn.exec_driver_sql(
                        f"ALTER TABLE company_sources ADD COLUMN {col} {ddl}"
                    )
                except Exception as exc:
                    log.debug("migration ADD COLUMN %s skipped: %s", col, exc)
            try:
                conn.exec_driver_sql(
                    "UPDATE company_sources SET careers_url = url "
                    "WHERE (careers_url IS NULL OR careers_url = '') "
                    "AND url IS NOT NULL AND url != ''"
                )
            except Exception:
                pass
            try:
                conn.exec_driver_sql(
                    "UPDATE company_sources SET url = careers_url "
                    "WHERE (url IS NULL OR url = '') "
                    "AND careers_url IS NOT NULL AND careers_url != ''"
                )
            except Exception:
                pass
            try:
                conn.exec_driver_sql(
                    "UPDATE company_sources SET company = label "
                    "WHERE (company IS NULL OR company = '') "
                    "AND label IS NOT NULL AND label != ''"
                )
            except Exception:
                pass
            try:
                conn.exec_driver_sql(
                    "UPDATE company_sources SET label = company "
                    "WHERE (label IS NULL OR label = '') "
                    "AND company IS NOT NULL AND company != ''"
                )
            except Exception:
                pass
            try:
                conn.commit()
            except Exception:
                pass
    except Exception as exc:
        log.debug("ensure_company_sources_schema skipped: %s", exc)


# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------

def source_to_dict(obj: Any) -> dict:
    """Normalise an ORM row or plain dict to a source dict. Never raises."""
    try:
        if isinstance(obj, dict):
            get = obj.get
            careers = (
                get("careers_url") or get("url") or get("href") or get("link") or ""
            )
            label = get("label") or get("company") or get("name") or ""
            stype = (
                get("source_type")
                or get("type")
                or get("adapter")
                or get("adapter_key")
                or get("kind")
                or detect_source(careers if isinstance(careers, str) else "")
                or "generic"
            )
            last_run = get("last_run")
            if isinstance(last_run, datetime):
                last_run = last_run.isoformat()
            return {
                "id": get("id"),
                "label": label,
                "company": get("company") or label,
                "careers_url": careers,
                "url": get("url") or careers,
                "source_type": stype,
                "type": stype,
                "adapter_key": get("adapter_key") or get("adapter") or stype,
                "adapter": get("adapter") or get("adapter_key") or stype,
                "board": get("board") or get("board_token") or get("slug") or get("handle") or "",
                "host": get("host") or "",
                "tenant": get("tenant") or "",
                "site": get("site") or "",
                "base_url": get("base_url") or "",
                "enabled": get("enabled", True),
                "last_run": last_run,
                "last_count": get("last_count", 0),
                "last_error": get("last_error"),
            }
        get = getattr
        careers = get(obj, "careers_url", "") or get(obj, "url", "") or ""
        label = get(obj, "label", "") or get(obj, "company", "") or get(obj, "name", "")
        stype = (
            get(obj, "source_type", None)
            or get(obj, "type", None)
            or get(obj, "adapter", None)
            or get(obj, "adapter_key", None)
            or get(obj, "kind", None)
            or detect_source(careers if isinstance(careers, str) else "")
            or "generic"
        )
        last_run = get(obj, "last_run", None)
        if isinstance(last_run, datetime):
            last_run = last_run.isoformat()
        return {
            "id": get(obj, "id", None),
            "label": label,
            "company": get(obj, "company", "") or label,
            "careers_url": careers,
            "url": get(obj, "url", "") or careers,
            "source_type": stype,
            "type": stype,
            "adapter_key": get(obj, "adapter_key", "") or stype,
            "adapter": get(obj, "adapter_key", "") or stype,
            "board": get(obj, "board", "") or get(obj, "board_token", "") or "",
            "host": get(obj, "host", "") or "",
            "tenant": get(obj, "tenant", "") or "",
            "site": get(obj, "site", "") or "",
            "base_url": get(obj, "base_url", "") or "",
            "enabled": get(obj, "enabled", True),
            "last_run": last_run,
            "last_count": get(obj, "last_count", 0),
            "last_error": get(obj, "last_error", None),
        }
    except Exception as exc:
        log.debug("source_to_dict failed: %s", exc)
        return {}


def is_enabled(obj: Any) -> bool:
    """True unless the source is explicitly disabled. Never raises."""
    try:
        if isinstance(obj, dict):
            return obj.get("enabled", True) is not False
        return getattr(obj, "enabled", True) is not False
    except Exception:
        return True


# ---------------------------------------------------------------------------
# JSON fallback
# ---------------------------------------------------------------------------

def load_sources_from_json(path: Optional[Path | str] = None) -> list[dict]:
    """Read raw source dicts from ``data/sources.json``. Never raises."""
    p = Path(path) if path else SOURCES_JSON
    try:
        if not p.exists():
            return []
        raw = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("Could not read %s: %s", p, exc)
        return []
    rows: Any = raw.get("sources") if isinstance(raw, dict) else raw
    if not isinstance(rows, list):
        return []
    return [dict(r) for r in rows if isinstance(r, dict)]


def load_enabled_sources_from_json(path: Optional[Path | str] = None) -> list[dict]:
    """Enabled source dicts from JSON fallback. Never raises."""
    try:
        return [r for r in load_sources_from_json(path) if is_enabled(r)]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# ORM primary + JSON fallback
# ---------------------------------------------------------------------------

def _orm_session():
    """Open a backend.db session (importing this module registers the table)."""
    try:
        from backend.db import SessionLocal, init_db

        try:
            init_db()
        except Exception:
            pass
        try:
            ensure_company_sources_schema()
        except Exception:
            pass
        return SessionLocal()
    except Exception:
        return None


def load_enabled_sources(db=None, path: Optional[Path | str] = None) -> list[dict]:
    """Load enabled company sources: ORM first, JSON fallback. Never raises.

    *db* may be an injected SQLAlchemy session (caller-owned, not closed).
    Otherwise a short-lived session is opened when possible.
    """
    # ORM path.
    try:
        if CompanySource is not None:
            session = db
            own = False
            if session is None:
                session = _orm_session()
                own = session is not None
            if session is not None:
                try:
                    rows = session.query(CompanySource).all()
                    enabled = [source_to_dict(r) for r in rows if is_enabled(r)]
                    return enabled
                except Exception as exc:
                    log.debug("CompanySource ORM query failed, JSON fallback: %s", exc)
                finally:
                    if own:
                        try:
                            session.close()
                        except Exception:
                            pass
    except Exception as exc:
        log.debug("CompanySource ORM load failed, JSON fallback: %s", exc)
    # JSON fallback (DB unavailable).
    return load_enabled_sources_from_json(path)


# ---------------------------------------------------------------------------
# FastAPI router: GET/POST /api/sources, PATCH+PUT /api/sources/{id},
# DELETE /api/sources/{id}, POST /api/sources/{id}/test.
# ---------------------------------------------------------------------------

try:
    from fastapi import APIRouter, Body, Depends, HTTPException
    from sqlalchemy.orm import Session as _Session

    from backend.db import get_db as _get_db

    router = APIRouter(prefix="/api/sources", tags=["sources"])

    def _all_rows(db: _Session) -> list:
        try:
            ensure_company_sources_schema()
        except Exception:
            pass
        return (
            db.query(CompanySource).order_by(CompanySource.id).all()
            if CompanySource is not None
            else []
        )

    @router.get("")
    @router.get("/")
    def list_sources(db: _Session = Depends(_get_db)):
        try:
            return [source_to_dict(r) for r in _all_rows(db)]
        except Exception as exc:
            log.warning("list_sources failed: %s", exc)
            return []

    @router.post("")
    @router.post("/")
    def create_source(
        payload: dict = Body(...), db: _Session = Depends(_get_db)
    ):
        careers_url = (
            payload.get("careers_url")
            or payload.get("url")
            or payload.get("href")
            or payload.get("link")
            or ""
        )
        if isinstance(careers_url, str):
            careers_url = careers_url.strip()
        if not careers_url:
            raise HTTPException(
                status_code=422, detail="careers_url (or url) is required"
            )
        raw_type = (
            payload.get("source_type")
            or payload.get("type")
            or payload.get("adapter")
            or payload.get("adapter_key")
            or payload.get("kind")
            or ""
        )
        stype = (
            str(raw_type).strip().lower()
            or detect_source(careers_url)
            or "generic"
        )
        adapter_key = str(
            payload.get("adapter_key") or payload.get("adapter") or stype
        ).strip().lower() or stype
        label = str(
            payload.get("label") or payload.get("company") or payload.get("name") or ""
        ).strip() or _domain_of(careers_url) or careers_url
        enabled = payload.get("enabled", True)
        enabled = False if enabled is False else True
        try:
            ensure_company_sources_schema()
        except Exception:
            pass
        # Unique guard (friendly 409 instead of raw IntegrityError).
        try:
            existing = (
                db.query(CompanySource)
                .filter(CompanySource.careers_url == careers_url)
                .first()
            )
            if existing is not None:
                raise HTTPException(
                    status_code=409,
                    detail=f"source already exists: {careers_url}",
                )
        except HTTPException:
            raise
        except Exception as exc:
            log.debug("duplicate check skipped: %s", exc)
        row = CompanySource(
            label=label,
            careers_url=careers_url,
            source_type=stype,
            adapter_key=adapter_key,
            enabled=enabled,
            last_count=0,
            company=label,
            url=careers_url,
            board=str(
                payload.get("board")
                or payload.get("board_token")
                or payload.get("slug")
                or payload.get("handle")
                or ""
            ),
            host=str(payload.get("host") or ""),
            tenant=str(payload.get("tenant") or ""),
            site=str(payload.get("site") or ""),
            base_url=str(payload.get("base_url") or ""),
        )
        db.add(row)
        try:
            db.commit()
        except Exception as exc:
            db.rollback()
            raise HTTPException(status_code=409, detail=f"could not save: {exc}")
        db.refresh(row)
        return source_to_dict(row)

    def _apply_update(row, payload: dict) -> None:
        if "careers_url" in payload or "url" in payload or "href" in payload or "link" in payload:
            new_url = (
                payload.get("careers_url")
                or payload.get("url")
                or payload.get("href")
                or payload.get("link")
                or ""
            )
            if isinstance(new_url, str) and new_url.strip():
                new_url = new_url.strip()
                row.careers_url = new_url
                try:
                    row.url = new_url
                except Exception:
                    pass
        if "label" in payload or "company" in payload or "name" in payload:
            new_label = (
                payload.get("label") or payload.get("company") or payload.get("name") or ""
            )
            if isinstance(new_label, str) and new_label.strip():
                row.label = new_label.strip()
                try:
                    row.company = new_label.strip()
                except Exception:
                    pass
        for key, col in (
            ("source_type", "source_type"),
            ("type", "source_type"),
            ("kind", "source_type"),
            ("adapter_key", "adapter_key"),
            ("adapter", "adapter_key"),
            ("board", "board"),
            ("board_token", "board"),
            ("host", "host"),
            ("tenant", "tenant"),
            ("site", "site"),
            ("base_url", "base_url"),
        ):
            if key in payload and payload[key] is not None:
                try:
                    setattr(row, col, str(payload[key]))
                except Exception:
                    pass
        if "enabled" in payload:
            row.enabled = False if payload["enabled"] is False else True

    def _update_source(source_id: int, payload: dict, db: _Session) -> dict:
        try:
            ensure_company_sources_schema()
        except Exception:
            pass
        row = (
            db.query(CompanySource).filter(CompanySource.id == source_id).first()
        )
        if row is None:
            raise HTTPException(status_code=404, detail=f"source {source_id} not found")
        _apply_update(row, payload or {})
        try:
            db.commit()
        except Exception as exc:
            db.rollback()
            raise HTTPException(status_code=409, detail=f"could not update: {exc}")
        db.refresh(row)
        return source_to_dict(row)

    @router.patch("/{source_id}")
    def patch_source(
        source_id: int, payload: dict = Body(...), db: _Session = Depends(_get_db)
    ):
        return _update_source(source_id, payload or {}, db)

    @router.put("/{source_id}")
    def put_source(
        source_id: int, payload: dict = Body(...), db: _Session = Depends(_get_db)
    ):
        return _update_source(source_id, payload or {}, db)

    @router.delete("/{source_id}")
    def delete_source(source_id: int, db: _Session = Depends(_get_db)):
        try:
            ensure_company_sources_schema()
        except Exception:
            pass
        row = (
            db.query(CompanySource).filter(CompanySource.id == source_id).first()
        )
        if row is None:
            raise HTTPException(status_code=404, detail=f"source {source_id} not found")
        db.delete(row)
        db.commit()
        return {"ok": True, "id": source_id}

    @router.post("/{source_id}/test")
    def test_source(source_id: int, db: _Session = Depends(_get_db)):
        """Offline-safe adapter smoke test. Never raises (count 0 on failure)."""
        try:
            ensure_company_sources_schema()
        except Exception:
            pass
        row = (
            db.query(CompanySource).filter(CompanySource.id == source_id).first()
        )
        if row is None:
            raise HTTPException(status_code=404, detail=f"source {source_id} not found")
        info = source_to_dict(row)
        count = 0
        error = None
        try:
            from backend.ingest import build_sources_adapters

            adapters = build_sources_adapters(sources=[info])
            if not adapters:
                error = "no adapter built for source"
            else:
                try:
                    jobs = adapters[0].fetch() or []
                    count = len(jobs)
                except Exception as exc:
                    error = str(exc)
                    count = 0
        except Exception as exc:
            error = str(exc)
            count = 0
        try:
            row.last_run = datetime.utcnow()
            row.last_count = int(count)
            row.last_error = str(error) if error else None
            db.commit()
            db.refresh(row)
            info = source_to_dict(row)
        except Exception as exc:
            log.debug("test_source persist skipped: %s", exc)
            try:
                db.rollback()
            except Exception:
                pass
        return {
            "ok": error is None,
            "id": info.get("id", source_id),
            "label": info.get("label"),
            "url": info.get("careers_url") or info.get("url"),
            "count": count,
            "error": error,
        }

except Exception as exc:  # pragma: no cover - FastAPI/SQLAlchemy missing
    log.debug("sources router unavailable: %s", exc)
    router = None  # type: ignore[assignment]


__all__ = [
    "CompanySource",
    "SOURCES_JSON",
    "detect_source",
    "ensure_company_sources_schema",
    "source_to_dict",
    "is_enabled",
    "load_sources_from_json",
    "load_enabled_sources_from_json",
    "load_enabled_sources",
    "router",
]

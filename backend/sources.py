"""Company sources API: careers pages / job boards added by URL.

    GET    /api/sources
    POST   /api/sources              {url, label?}
    PATCH  /api/sources/{id}         {enabled?, label?}
    DELETE /api/sources/{id}
    POST   /api/sources/{id}/test    dry run: list postings, store nothing
"""
from __future__ import annotations

from typing import Optional
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from backend.adapters.base import SourceError
from backend.adapters.generic import GenericAdapter
from backend.db import CompanySource, get_db
from backend.workspaces import current_workspace, owned
from backend.scraping.http import FetchError, PoliteClient

router = APIRouter(prefix="/api/sources", tags=["sources"])


class SourceCreate(BaseModel):
    url: str
    label: Optional[str] = None


class SourceUpdate(BaseModel):
    enabled: Optional[bool] = None
    label: Optional[str] = None


def normalize_url(url: str) -> str:
    url = (url or "").strip()
    if url and "://" not in url:
        url = "https://" + url
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise HTTPException(status_code=422, detail=f"Not a valid URL: {url!r}")
    return url


def default_label(url: str) -> str:
    host = urlsplit(url).netloc.lower().removeprefix("www.")
    path = [s for s in urlsplit(url).path.split("/") if s]
    # apply.workable.com/<company>, jobs.lever.co/<company>, ...
    if host.split(".")[0] in ("apply", "jobs", "boards", "job-boards", "careers") and path:
        return path[0].replace("-", " ").title()
    name = host.split(".")[0]
    return name.replace("-", " ").title()


def source_to_dict(s: CompanySource) -> dict:
    return {
        "id": s.id,
        "label": s.label or default_label(s.careers_url or ""),
        "url": s.careers_url,
        "enabled": bool(s.enabled),
        "last_run": s.last_run.isoformat() if s.last_run else None,
        "last_count": s.last_count or 0,
        "last_error": s.last_error,
        "last_method": s.last_method,
    }


@router.get("")
def list_sources(db: Session = Depends(get_db), ws: int = Depends(current_workspace)):
    rows = db.query(CompanySource).filter(CompanySource.workspace_id == ws).order_by(CompanySource.id)
    return [source_to_dict(s) for s in rows]


@router.post("", status_code=201)
def create_source(payload: SourceCreate, db: Session = Depends(get_db), ws: int = Depends(current_workspace)):
    url = normalize_url(payload.url)
    if db.query(CompanySource).filter(CompanySource.workspace_id == ws, CompanySource.careers_url == url).first():
        raise HTTPException(status_code=409, detail="That URL is already a source")
    src = CompanySource(workspace_id=ws, careers_url=url, url=url, label=(payload.label or "").strip() or default_label(url),
                        source_type="auto", enabled=True)
    db.add(src)
    db.commit()
    return source_to_dict(src)


@router.patch("/{source_id}")
def update_source(source_id: int, payload: SourceUpdate, db: Session = Depends(get_db),
                 ws: int = Depends(current_workspace)):
    src = owned(db, CompanySource, source_id, ws, "Source")
    if payload.enabled is not None:
        src.enabled = payload.enabled
    if payload.label is not None and payload.label.strip():
        src.label = payload.label.strip()
    db.commit()
    return source_to_dict(src)


@router.delete("/{source_id}")
def delete_source(source_id: int, db: Session = Depends(get_db),
                 ws: int = Depends(current_workspace)):
    src = owned(db, CompanySource, source_id, ws, "Source")
    db.delete(src)
    db.commit()
    return {"ok": True, "id": source_id}


@router.post("/{source_id}/test")
def test_source(source_id: int, db: Session = Depends(get_db),
               ws: int = Depends(current_workspace)):
    src = owned(db, CompanySource, source_id, ws, "Source")
    client = PoliteClient()
    adapter = GenericAdapter(client, src.careers_url, company=src.label or "")
    try:
        postings = adapter.list_postings()
    except (SourceError, FetchError) as exc:
        return {"ok": False, "error": str(exc), "notes": adapter.notes}
    finally:
        client.close()
    return {
        "ok": True,
        "method": adapter.method,
        "count": len(postings),
        "sample": [{"title": p.title, "location": p.location_text, "url": p.url} for p in postings[:8]],
        "notes": adapter.notes,
    }

"""Workspaces: separate job searches.

Each workspace has its own search settings, dealbreakers, SEEK settings,
company sources, documents, map pins, jobs (with their scores) and runs.
Company profiles, the geocode cache and the HTTP cache are shared.

The frontend sends the current workspace in the ``X-Workspace`` header;
requests without it use workspace 1.

    GET    /api/workspaces
    POST   /api/workspaces          {name, copy_from?, copy_documents?}
    PATCH  /api/workspaces/{id}     {name}
    DELETE /api/workspaces/{id}     (not the last one)
"""
from __future__ import annotations

import shutil
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from backend.db import (
    UPLOAD_DIR,
    CompanySource,
    DealbreakerSet,
    Document,
    Job,
    Pin,
    UserProfile,
    Workspace,
    get_db,
)

router = APIRouter(prefix="/api/workspaces", tags=["workspaces"])


def current_workspace(
    x_workspace: Optional[int] = Header(default=None),
    db: Session = Depends(get_db),
) -> int:
    ws = x_workspace or 1
    if db.get(Workspace, ws) is None:
        raise HTTPException(status_code=404, detail=f"Workspace {ws} not found")
    return ws


def owned(db: Session, model, obj_id: int, ws: int, what: str):
    """Fetch a row by id, 404 unless it belongs to workspace ``ws``."""
    row = db.get(model, obj_id)
    if row is None or row.workspace_id != ws:
        raise HTTPException(status_code=404, detail=f"{what} {obj_id} not found")
    return row


class WorkspaceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    copy_from: Optional[int] = None       # copy settings, sources and pins from this workspace
    copy_documents: bool = False          # ...and its documents


class WorkspaceUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=80)


def _to_dict(ws: Workspace, db: Session) -> dict:
    return {
        "id": ws.id,
        "name": ws.name,
        "jobs": db.query(Job).filter(Job.workspace_id == ws.id).count(),
        "created_at": ws.created_at.isoformat() if ws.created_at else None,
    }


@router.get("")
def list_workspaces(db: Session = Depends(get_db)):
    return [_to_dict(w, db) for w in db.query(Workspace).order_by(Workspace.id).all()]


@router.post("", status_code=201)
def create_workspace(payload: WorkspaceCreate, db: Session = Depends(get_db)):
    ws = Workspace(name=payload.name.strip())
    db.add(ws)
    db.flush()
    src_id = payload.copy_from
    if src_id is not None:
        if db.get(Workspace, src_id) is None:
            raise HTTPException(status_code=404, detail=f"Workspace {src_id} not found")
        _copy_settings(db, src_id, ws.id, payload.copy_documents)
    db.commit()
    return _to_dict(ws, db)


def _copy_settings(db: Session, src: int, dst: int, documents: bool) -> None:
    prof = db.get(UserProfile, src)
    if prof is not None:
        cols = {c.name: getattr(prof, c.name) for c in UserProfile.__table__.columns if c.name != "id"}
        db.add(UserProfile(id=dst, **cols))
    dbk = db.get(DealbreakerSet, src)
    if dbk is not None:
        db.add(DealbreakerSet(id=dst, industries=list(dbk.industries or []), keywords=list(dbk.keywords or [])))
    for pin in db.query(Pin).filter(Pin.workspace_id == src):
        db.add(Pin(workspace_id=dst, label=pin.label, kind=pin.kind, lat=pin.lat, lng=pin.lng, radius_km=pin.radius_km))
    for s in db.query(CompanySource).filter(CompanySource.workspace_id == src):
        db.add(CompanySource(workspace_id=dst, label=s.label, careers_url=s.careers_url, url=s.careers_url,
                             source_type=s.source_type, enabled=s.enabled))
    if documents:
        for d in db.query(Document).filter(Document.workspace_id == src):
            stored = None
            if d.stored_name and (UPLOAD_DIR / d.stored_name).is_file():
                stored = f"{uuid.uuid4().hex}{d.filetype or ''}"
                shutil.copyfile(UPLOAD_DIR / d.stored_name, UPLOAD_DIR / stored)
            db.add(Document(workspace_id=dst, filename=d.filename, filetype=d.filetype, stored_name=stored,
                            text=d.text, kind=d.kind, use_for_scoring=d.use_for_scoring))


@router.patch("/{ws_id}")
def rename_workspace(ws_id: int, payload: WorkspaceUpdate, db: Session = Depends(get_db)):
    ws = db.get(Workspace, ws_id)
    if ws is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    ws.name = payload.name.strip()
    db.commit()
    return _to_dict(ws, db)


@router.delete("/{ws_id}")
def delete_workspace(ws_id: int, db: Session = Depends(get_db)):
    ws = db.get(Workspace, ws_id)
    if ws is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    if db.query(Workspace).count() <= 1:
        raise HTTPException(status_code=409, detail="You can't delete the only workspace")
    for d in db.query(Document).filter(Document.workspace_id == ws_id):
        if d.stored_name:
            (UPLOAD_DIR / d.stored_name).unlink(missing_ok=True)
    for model in (UserProfile, DealbreakerSet):
        row = db.get(model, ws_id)
        if row is not None:
            db.delete(row)
    db.delete(ws)  # jobs (+ scores), documents, pins, sources and runs cascade
    db.commit()
    return {"ok": True, "id": ws_id}

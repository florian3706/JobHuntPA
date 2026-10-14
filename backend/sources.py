"""Company sources API: careers pages / job boards added by URL.

    GET    /api/sources
    POST   /api/sources              {url, label?}
    PATCH  /api/sources/{id}         {enabled?, label?}
    DELETE /api/sources/{id}
    POST   /api/sources/{id}/test    dry run: list postings, store nothing
    POST   /api/sources/bulk/preview {text}: the rows of a pasted text or file, flagged; fetches and stores nothing
    POST   /api/sources/bulk         {rows: [{company, url}]}: adds each through add_source, a result per row
    GET    /api/sources/export       the workspace's sources as CSV (company,url), the bulk format

Who may call them: whoever is signed in, on a workspace of their own (``current_workspace`` answers 404 for another
account's). An extension key may list, add and test sources one at a time (``sessions.KEY_ENDPOINTS``), but not use
the bulk endpoints or the export.
"""
from __future__ import annotations

from typing import Optional
from urllib.parse import parse_qs, urlsplit

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from backend import crypto, source_bulk
from backend.adapters.base import SourceError
from backend.adapters.generic import source_adapter
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


class BulkText(BaseModel):
    text: str


class BulkRow(BaseModel):
    company: str = ""
    url: str


class BulkRows(BaseModel):
    rows: list[BulkRow]


def normalize_url(url: str) -> str:
    url = (url or "").strip()
    if url and "://" not in url:
        url = "https://" + url
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise HTTPException(status_code=422, detail=f"Not a valid URL: {url!r}")
    return url


def default_label(url: str) -> str:
    from backend.adapters.apsjobs import is_apsjobs_url

    if is_apsjobs_url(url):  # APSJobs search: name it after the agency or search
        q = parse_qs(urlsplit(url).query)
        return ", ".join(q.get("department", [])) or f"APSJobs: {(q.get('searchString') or ['all jobs'])[0]}"
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


def add_source(db: Session, ws: int, url: str, label: Optional[str]) -> Optional[CompanySource]:
    """The way a source is added (``POST /api/sources`` and each row of the bulk add): the new row (added to ``db``,
    not committed), or None when the workspace has that URL already. Raises 422 for an address that isn't a web
    link. Nothing is fetched: the scraping rules apply when a search or Test reads the page."""
    url = normalize_url(url)
    # Found by its blind index: the URL itself is sealed (accounts design §2.3).
    if db.query(CompanySource.id).filter(CompanySource.workspace_id == ws, CompanySource.careers_url_index
                                         == crypto.blind_index("careers_url", url)).first():
        return None
    src = CompanySource(workspace_id=ws, careers_url=url, url=url, label=(label or "").strip() or default_label(url),
                        source_type="auto", enabled=True)
    db.add(src)
    db.flush()
    return src


@router.post("", status_code=201)
def create_source(payload: SourceCreate, db: Session = Depends(get_db), ws: int = Depends(current_workspace)):
    src = add_source(db, ws, payload.url, payload.label)
    if src is None:
        raise HTTPException(status_code=409, detail="That URL is already a source")
    db.commit()
    return source_to_dict(src)


# -- in bulk (docs/design/open-source.md §1.14) ------------------------------------------------------------------

def _known(db: Session, ws: int) -> set[bytes]:
    """The blind indexes of the workspace's source URLs (plaintext column, scoped by the plaintext workspace_id)."""
    return {i for (i,) in db.query(CompanySource.careers_url_index).filter(CompanySource.workspace_id == ws) if i}


def _flags(check: source_bulk.AddressCheck) -> list[dict]:
    return [{"code": f.code, "reason": f.reason} for f in check.flags]


@router.post("/bulk/preview")
def bulk_preview(payload: BulkText, db: Session = Depends(get_db), ws: int = Depends(current_workspace)):
    """The rows of a pasted text or file, each with its flags and what adding it would do. Signed in, own workspace.
    Nothing is fetched, looked up or stored: the flags are worked out from the addresses' shape alone."""
    try:
        rows = source_bulk.parse_csv(payload.text)
    except source_bulk.FormatError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    known, seen = _known(db, ws), {}
    out = []
    for row in rows:
        item = {"line": row.line, "company": row.company, "url": row.url, "host": "", "flags": [],
                "blocked": True, "status": "unreadable", "problem": row.problem, "repeat_of": None}
        if not row.problem:
            check = source_bulk.address_flags(row.url)
            item.update(url=check.url, host=check.host, flags=_flags(check), blocked=check.blocked)
            if check.blocked:
                item.update(status="refused")
            else:
                index = crypto.blind_index("careers_url", check.url)
                if index in known:
                    item.update(status="already")
                elif check.url in seen:
                    item.update(status="repeat", repeat_of=seen[check.url])
                else:
                    seen[check.url] = row.line
                    item.update(status="new")
                item["label"] = row.company or default_label(check.url)
        out.append(item)
    return {"rows": out, "max_rows": source_bulk.MAX_ROWS}


@router.post("/bulk")
def bulk_add(payload: BulkRows, db: Session = Depends(get_db), ws: int = Depends(current_workspace)):
    """Adds the rows the person kept, each through :func:`add_source`, in one transaction. Signed in, own workspace.
    A row whose address is not a web link or is on the local network is refused whatever the preview said; one the
    workspace has is left as it is; a repeat of an earlier row is ignored. Every row gets a result: added, already,
    repeat, refused or unreadable."""
    if len(payload.rows) > source_bulk.MAX_ROWS:
        raise HTTPException(status_code=413, detail=f"That is more than {source_bulk.MAX_ROWS} rows. "
                                                    f"Add them in smaller groups.")
    seen: set[str] = set()
    results = []
    for row in payload.rows:
        company = source_bulk.one_line(row.company)
        result = {"company": company, "url": row.url, "result": "unreadable", "detail": "", "id": None}
        results.append(result)
        if len(row.url) > source_bulk.MAX_URL or len(company) > source_bulk.MAX_COMPANY:
            result["detail"] = "The address or the company name is too long."
            continue
        check = source_bulk.address_flags(row.url)
        result["url"] = check.url
        if check.blocked:
            result.update(result="refused", detail=" ".join(f.reason for f in check.flags
                                                            if f.code in source_bulk.BLOCKING))
            continue
        if check.url in seen:
            result["result"] = "repeat"
            continue
        seen.add(check.url)
        try:
            src = add_source(db, ws, check.url, company)
        except HTTPException as exc:
            result["detail"] = str(exc.detail)
            continue
        if src is None:
            result["result"] = "already"
        else:
            result.update(result="added", id=src.id, company=src.label)
    db.commit()
    counts = {k: sum(1 for r in results if r["result"] == k) for k in ("added", "already", "repeat", "refused",
                                                                        "unreadable")}
    return {"results": results, "counts": counts}


@router.get("/export")
def export_sources(db: Session = Depends(get_db), ws: int = Depends(current_workspace)):
    """The workspace's sources in the bulk format (``company,url``), as a download. Signed in, own workspace."""
    rows = db.query(CompanySource).filter(CompanySource.workspace_id == ws).order_by(CompanySource.id)
    text = source_bulk.to_csv([(s.label or default_label(s.careers_url or ""), s.careers_url or "") for s in rows])
    return Response(content=text.encode("utf-8"), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="sources.csv"',
                             "Cache-Control": "no-store"})


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
    adapter = source_adapter(client, src.careers_url, company=src.label or "")
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

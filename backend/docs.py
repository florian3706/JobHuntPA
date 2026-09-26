"""Documents API: upload PDF/DOCX/ODF, extract text, choose what the scorer sees.

    GET    /api/documents
    POST   /api/documents            multipart: file, kind
    PATCH  /api/documents/{id}       {kind?, use_for_scoring?}
    GET    /api/documents/{id}/text
    DELETE /api/documents/{id}
"""
from __future__ import annotations

import shutil
import uuid
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy.orm import Session

from backend.db import DOCUMENT_KINDS, UPLOAD_DIR, Document, get_db
from backend.workspaces import current_workspace, owned

router = APIRouter(prefix="/api/documents", tags=["documents"])

ALLOWED_EXTENSIONS = {".pdf", ".docx", ".odt", ".ods", ".odp", ".txt", ".md"}


def _extract_pdf_text(path: Path) -> str:
    from pypdf import PdfReader

    return "\n".join((page.extract_text() or "") for page in PdfReader(str(path)).pages).strip()


def _extract_docx_text(path: Path) -> str:
    import docx

    doc = docx.Document(str(path))
    parts = [p.text for p in doc.paragraphs]
    for table in doc.tables:
        for row in table.rows:
            parts.append(" | ".join(cell.text.strip() for cell in row.cells))
    return "\n".join(parts).strip()


def _extract_odf_text(path: Path) -> str:
    from odf import teletype
    from odf import text as odf_text
    from odf.opendocument import load

    doc = load(str(path))
    return "\n".join(teletype.extractText(p) for p in doc.getElementsByType(odf_text.P)).strip()


def extract_text(path: Path, suffix: str) -> str:
    suffix = suffix.lower()
    if suffix == ".pdf":
        return _extract_pdf_text(path)
    if suffix == ".docx":
        return _extract_docx_text(path)
    if suffix in {".odt", ".ods", ".odp"}:
        return _extract_odf_text(path)
    if suffix in {".txt", ".md"}:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    raise ValueError(f"Unsupported file type: {suffix}")


def doc_to_dict(d: Document) -> dict:
    return {
        "id": d.id,
        "filename": d.filename,
        "filetype": d.filetype,
        "kind": d.kind or "other",
        "use_for_scoring": bool(d.use_for_scoring),
        "created_at": d.created_at.isoformat() if d.created_at else None,
        "text_len": len(d.text or ""),
    }


class DocumentUpdate(BaseModel):
    kind: Optional[str] = None
    use_for_scoring: Optional[bool] = None


@router.get("")
def list_documents(db: Session = Depends(get_db), ws: int = Depends(current_workspace)):
    rows = db.query(Document).filter(Document.workspace_id == ws).order_by(Document.created_at.desc())
    return [doc_to_dict(d) for d in rows]


@router.post("")
def upload_document(file: UploadFile = File(...), kind: str = Form("resume"), db: Session = Depends(get_db),
                    ws: int = Depends(current_workspace)):
    if kind not in DOCUMENT_KINDS:
        raise HTTPException(status_code=422, detail=f"kind must be one of {DOCUMENT_KINDS}")
    original = file.filename or "upload"
    suffix = Path(original).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"Unsupported file type {suffix!r}. Allowed: {sorted(ALLOWED_EXTENSIONS)}")
    stored = f"{uuid.uuid4().hex}{suffix}"
    dest = UPLOAD_DIR / stored
    with dest.open("wb") as out:
        shutil.copyfileobj(file.file, out)
    try:
        text = extract_text(dest, suffix)
    except Exception as exc:
        dest.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail=f"Could not extract text: {exc}")
    if not text:
        dest.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="No text found in the file (scanned PDF?)")
    doc = Document(workspace_id=ws, filename=original, filetype=suffix, stored_name=stored, text=text, kind=kind)
    db.add(doc)
    db.commit()
    return doc_to_dict(doc)


@router.patch("/{doc_id}")
def update_document(doc_id: int, payload: DocumentUpdate, db: Session = Depends(get_db),
                    ws: int = Depends(current_workspace)):
    doc = owned(db, Document, doc_id, ws, "Document")
    if payload.kind is not None:
        if payload.kind not in DOCUMENT_KINDS:
            raise HTTPException(status_code=422, detail=f"kind must be one of {DOCUMENT_KINDS}")
        doc.kind = payload.kind
    if payload.use_for_scoring is not None:
        doc.use_for_scoring = payload.use_for_scoring
    db.commit()
    return doc_to_dict(doc)


@router.get("/{doc_id}/text")
def document_text(doc_id: int, db: Session = Depends(get_db), ws: int = Depends(current_workspace)):
    doc = owned(db, Document, doc_id, ws, "Document")
    return {"id": doc.id, "filename": doc.filename, "text": doc.text or ""}


@router.delete("/{doc_id}")
def delete_document(doc_id: int, db: Session = Depends(get_db), ws: int = Depends(current_workspace)):
    doc = owned(db, Document, doc_id, ws, "Document")
    if doc.stored_name:
        (UPLOAD_DIR / doc.stored_name).unlink(missing_ok=True)
    db.delete(doc)
    db.commit()
    return {"ok": True, "id": doc_id}
